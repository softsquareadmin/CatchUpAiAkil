"""Passcode sign-in for a shared deployment (SPEC-deploy). Off unless APP_PASSCODE is set.

A pure ASGI middleware, so it covers the /ws websocket as well as pages and /api (Starlette's BaseHTTPMiddleware
does not see websockets). Signing in sets a cookie signed with SESSION_SECRET; /health, /login and /static stay
open (static files hold no data). Failed sign-ins are slowed down and limited per address and overall."""
import asyncio
import hashlib
import hmac
import html
import time
from collections import deque
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote

COOKIE = "ia_access"
MAX_AGE_S = 7 * 24 * 3600
OPEN_PATHS = ("/health", "/login", "/logout")
OPEN_PREFIXES = ("/static/",)
LOGIN_PAGE = Path(__file__).parent / "static" / "login.html"
FAIL_WINDOW_S = 600
FAILS_PER_ADDRESS = 5     # per address in FAIL_WINDOW_S
FAILS_OVERALL = 20        # all addresses together (behind a proxy many people can share one address)
FAIL_DELAY_S = 1.0


class AccessConfigError(ValueError):
    pass


def access_from_env(env: dict[str, str]) -> tuple[str, str] | None:
    """(passcode, secret), or None when sign-in is off. A passcode without a usable secret is an error at startup."""
    passcode = (env.get("APP_PASSCODE") or "").strip()
    if not passcode:
        return None
    secret = (env.get("SESSION_SECRET") or "").strip()
    if len(secret) < 16:
        raise AccessConfigError("APP_PASSCODE is set, so SESSION_SECRET must be set too (at least 16 characters; "
                                "for example: python -c \"import secrets; print(secrets.token_urlsafe(32))\")")
    return passcode, secret


class PasscodeGate:
    def __init__(self, app, passcode: str, secret: str, clock=time.time, fail_delay_s: float = FAIL_DELAY_S):
        self.app, self.passcode, self.clock, self.fail_delay_s = app, passcode, clock, fail_delay_s
        # the passcode is part of the signing key: changing it signs everyone out
        self.key = hmac.new(secret.encode(), hashlib.sha256(passcode.encode()).digest(), hashlib.sha256).digest()
        self.fails: deque[tuple[float, str]] = deque()

    # ---- tokens ----
    def token(self, now: float | None = None) -> str:
        expires = int((now or self.clock()) + MAX_AGE_S)
        return f"{expires}.{hmac.new(self.key, str(expires).encode(), hashlib.sha256).hexdigest()}"

    def valid(self, token: str | None) -> bool:
        expires, _, sig = (token or "").partition(".")
        if not expires.isdigit() or int(expires) < self.clock():
            return False
        return hmac.compare_digest(sig, hmac.new(self.key, expires.encode(), hashlib.sha256).hexdigest())

    # ---- ASGI ----
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if scope["type"] == "http" and path == "/login":
            return await self.login(scope, receive, send)
        if scope["type"] == "http" and path == "/logout":
            return await respond(send, 303, b"", [(b"location", b"/login"), (b"set-cookie", self.cookie("", scope, 0))])
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or self.valid(cookie_of(scope)):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            return await send({"type": "websocket.close", "code": 1008, "reason": "sign in first"})
        if path.startswith("/api/"):
            return await respond(send, 401, b'{"detail":"Sign in first: open the app\'s home page."}',
                                 [(b"content-type", b"application/json")])
        target = path + ("?" + scope["query_string"].decode("latin-1") if scope.get("query_string") else "")
        return await respond(send, 303, b"", [(b"location", ("/login?next=" + quote(target, safe="")).encode())])

    async def login(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if scope["method"] != "POST":
            return await self.page(send, safe_next(query.get("next", ["/"])[0]), "")
        body = await read_body(receive)
        form = parse_qs(body.decode("utf-8", "replace"))
        nxt = safe_next(form.get("next", ["/"])[0])
        address = client_address(scope)
        now = self.clock()
        while self.fails and self.fails[0][0] < now - FAIL_WINDOW_S:
            self.fails.popleft()
        mine = sum(1 for _, a in self.fails if a == address)
        if mine >= FAILS_PER_ADDRESS or len(self.fails) >= FAILS_OVERALL:
            return await self.page(send, nxt, "Too many wrong passcodes. Wait ten minutes and try again.", 429)
        given = (form.get("passcode", [""])[0]).strip()
        if hmac.compare_digest(given.encode(), self.passcode.encode()):
            return await respond(send, 303, b"", [(b"location", nxt.encode()),
                                                  (b"set-cookie", self.cookie(self.token(now), scope, MAX_AGE_S))])
        self.fails.append((now, address))
        await asyncio.sleep(self.fail_delay_s)
        return await self.page(send, nxt, "That passcode is not right.", 401)

    async def page(self, send, nxt: str, error: str, status: int = 200):
        text = LOGIN_PAGE.read_text(encoding="utf-8")
        text = text.replace("{{next}}", html.escape(nxt, quote=True)).replace(
            "{{error}}", f'<p class="field-error" role="alert">{html.escape(error)}</p>' if error else "")
        await respond(send, status, text.encode(), [(b"content-type", b"text/html; charset=utf-8"),
                                                     (b"cache-control", b"no-store")])

    def cookie(self, value: str, scope, max_age: int) -> bytes:
        secure = "; Secure" if is_https(scope) else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}".encode()


def headers_of(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def cookie_of(scope) -> str | None:
    jar = SimpleCookie()
    try:
        jar.load(headers_of(scope).get("cookie", ""))
    except Exception:  # a malformed cookie header is treated as no cookie
        return None
    return jar[COOKIE].value if COOKIE in jar else None


def is_https(scope) -> bool:
    return scope.get("scheme") in ("https", "wss") or headers_of(scope).get("x-forwarded-proto", "") == "https"


def client_address(scope) -> str:
    forwarded = headers_of(scope).get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (scope.get("client") or ("?",))[0]


def safe_next(value: str) -> str:
    """Only a path on this site (no //host or scheme), so the sign-in page cannot send people elsewhere."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"


async def read_body(receive, limit: int = 4096) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > limit or not message.get("more_body"):
            return body[:limit]


async def respond(send, status: int, body: bytes, headers: list[tuple[bytes, bytes]]):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})
