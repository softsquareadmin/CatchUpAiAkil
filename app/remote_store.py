"""Optional copy of the app's small text files in one Supabase table (ia_files: path, content), so a host whose disk is
wiped on every restart keeps its sessions, saved pack versions and configuration log (SPEC-deploy).

The app still reads and writes local files only. `Sync.start` pulls the table into the local folders; `Sync.flush`
pushes files whose size or modification time changed. Off unless SYNC_ENABLED=true, so a local run with the Supabase
keys in .env never uploads anything by accident. Stdlib only, modelled on CatchUp's SupabaseStore.request."""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import dotenv_values
from pydantic import SecretStr

from app.config import ROOT

log = logging.getLogger(__name__)

TABLE = "/rest/v1/ia_files"
PAGE = 500
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_BATCH_BYTES = 2 * 1024 * 1024
SKIP_NAMES = {"audio.pcm"}  # store_audio stays local (and off by default)


class RemoteError(OSError):
    pass


def read_env(env_file: str | Path | None = ROOT / ".env", environ: dict[str, str] | None = None) -> dict[str, str]:
    """Same override order as settings: .env, then the process environment."""
    values: dict[str, str] = {}
    if env_file and Path(env_file).exists():
        values.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    values.update(os.environ if environ is None else environ)
    return values


class Client:
    """PostgREST calls on ia_files with the server secret key (never sent to the browser)."""

    def __init__(self, url: str, key: str | SecretStr, timeout: float = 20):
        url = (url or "").strip().rstrip("/")
        if url.endswith("/rest/v1"):  # the dashboard shows the REST URL; the project URL is accepted too
            url = url[: -len("/rest/v1")]
        parsed = urlparse(url)
        local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")  # the tests' fake server
        if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.query or parsed.path.strip("/"):
            raise RemoteError("SUPABASE_URL must be the HTTPS project URL, for example https://abcd.supabase.co")
        self.url, self.timeout = url.rstrip("/"), timeout
        self.key = key if isinstance(key, SecretStr) else SecretStr(key or "")
        if not self.key.get_secret_value():
            raise RemoteError("SUPABASE_SECRET_KEY is empty")
        self.host = parsed.hostname

    def request(self, method: str, params: dict | None = None, body=None, headers: dict | None = None):
        key = self.key.get_secret_value()
        h = {"apikey": key, "Accept": "application/json"}
        if not key.startswith("sb_secret_"):  # legacy service-role JWTs also need Bearer auth
            h["Authorization"] = "Bearer " + key
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        target = self.url + TABLE + ("?" + urlencode(params) if params else "")
        try:
            with urlopen(Request(target, data=data, headers=h, method=method), timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except HTTPError as e:
            code = None
            try:
                code = json.loads(e.read()).get("code")
            except (ValueError, AttributeError):
                pass
            if e.code in (401, 403) or code == "42501":
                msg = "Supabase rejected the secret key or the table permissions (use the secret / service_role key)"
            elif e.code == 404 or code in ("PGRST205", "42P01"):
                msg = "Table ia_files not found. Run ia_files.sql in the Supabase SQL Editor"
            else:
                msg = f"Supabase HTTP {e.code}" + (f" ({code})" if code else "")
            raise RemoteError(msg) from None
        except (URLError, TimeoutError, ConnectionError, OSError) as e:
            raise RemoteError(f"Could not reach Supabase at {self.host}: {getattr(e, 'reason', e)}") from None

    def pull(self, prefix: str) -> dict[str, str]:
        """Every row whose path starts with `prefix`, paged."""
        out, offset = {}, 0
        while True:
            page = self.request("GET", {"select": "path,content", "path": f"like.{prefix}*", "order": "path",
                                        "limit": PAGE, "offset": offset})
            out.update({r["path"]: r["content"] for r in page})
            if len(page) < PAGE:
                return out
            offset += len(page)

    def get(self, path: str) -> str | None:
        rows = self.request("GET", {"select": "content", "path": f"eq.{path}"})
        return rows[0]["content"] if rows else None

    def push(self, rows: list[dict]) -> None:
        """Insert or replace whole files ({path, content})."""
        now = datetime.now(timezone.utc).isoformat()
        self.request("POST", {"on_conflict": "path"}, [{**r, "updated_at": now} for r in rows],
                     {"Prefer": "resolution=merge-duplicates,return=minimal"})


def client_from_env(env: dict[str, str]) -> Client:
    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        raise RemoteError("SUPABASE_URL and SUPABASE_SECRET_KEY must both be set")
    return Client(url, key)


def sync_requested(env: dict[str, str]) -> bool:
    return env.get("SYNC_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


class Sync:
    """Mirror of three local folders. Remote paths: sessions/<id>/<file>, audit_logs/<file>, packs/<id>__<ver>.yaml.

    Never pushed: session folders (and other files) that were already on disk at startup and are not in the table,
    so turning sync on where real recordings live does not upload them. Repo packs (no "__" in the name) are never
    pulled or pushed: the repo copy is the source of truth."""

    def __init__(self, client: Client | None, sessions_dir: Path, audit_dir: Path, packs_dir: Path):
        self.client = client
        self.roots = {"sessions": Path(sessions_dir), "audit_logs": Path(audit_dir), "packs": Path(packs_dir)}
        self.status = "supabase" if client else "local"   # shown in /health
        self.error: str | None = None
        self.pulled = False
        self.held: set[str] = set()                         # remote keys never pushed ("sessions/<id>" or a file)
        self.seen: dict[str, tuple[int, int]] = {}          # remote path -> (size, mtime_ns) last pushed or pulled
        self.lock = threading.Lock()
        self._existing: set[str] | None = None             # local paths found before the first pull
        self.on_pull = None                                  # optional callback after a pull wrote files (cost total)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ---- paths ----
    def _local(self, path: str) -> Path | None:
        top, _, rest = path.partition("/")
        root = self.roots.get(top)
        if root is None or not rest or ".." in rest.split("/") or rest.startswith("/"):
            return None
        if top == "packs" and ("/" in rest or "__" not in rest or not rest.endswith(".yaml")):
            return None
        if top == "sessions" and rest.count("/") != 1:
            return None
        return root / rest

    def _unit(self, path: str) -> str:
        parts = path.split("/")
        return "/".join(parts[:2]) if parts[0] == "sessions" else path

    def local_files(self) -> dict[str, Path]:
        out = {}
        for top, root in self.roots.items():
            if not root.is_dir():
                continue
            if top == "packs":
                files = (p for p in root.glob("*__*.yaml"))
            elif top == "sessions":
                files = (p for d in root.iterdir() if d.is_dir() for p in d.iterdir())
            else:
                files = (p for p in root.iterdir())
            for p in files:
                if p.is_file() and p.name not in SKIP_NAMES and not p.name.startswith("."):
                    out[f"{top}/{p.relative_to(root).as_posix()}"] = p
        return out

    # ---- pull ----
    def start(self) -> None:
        """At startup: note what is already on disk, then pull. A failed pull is retried by `flush`."""
        if self.enabled:
            self._pull()

    def _pull(self) -> None:
        if self._existing is None:
            self._existing = set(self.local_files())
        try:
            remote: dict[str, str] = {}
            for prefix in ("sessions/", "audit_logs/", "packs/"):
                remote.update(self.client.pull(prefix))
        except RemoteError as e:
            self._fail(f"startup pull failed: {e}")
            return
        written = 0
        for path, content in remote.items():
            local = self._local(path)
            if local is None:
                continue
            if local.exists():
                mine = local.read_text(encoding="utf-8", errors="replace")
                if mine == content or path.startswith("packs/"):  # a pack already on disk is never replaced
                    continue
                if path.endswith(".jsonl"):  # both sides appended: keep the remote lines, then any new local ones
                    have = set(content.splitlines())
                    extra = [l for l in mine.splitlines() if l not in have]
                    content = content + ("" if content.endswith("\n") or not content else "\n") + \
                        "".join(l + "\n" for l in extra)
                else:
                    continue  # local wins and is pushed
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(content, encoding="utf-8")
            written += 1
        remote_units = {self._unit(p) for p in remote}
        self.held = {self._unit(p) for p in self._existing} - remote_units
        files = self.local_files()
        for path in remote:
            if path in files:
                st = files[path].stat()
                if files[path].read_text(encoding="utf-8", errors="replace") == remote[path]:
                    self.seen[path] = (st.st_size, st.st_mtime_ns)
        self.pulled = True
        self._ok()
        if self.on_pull is not None:
            self.on_pull()
        log.info("storage: pulled %d files from Supabase (%d written); %d local folders/files kept local",
                 len(remote), written, len(self.held))

    # ---- push ----
    def changed(self) -> list[tuple[str, Path, tuple[int, int]]]:
        out = []
        for path, p in self.local_files().items():
            if self._unit(path) in self.held:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime_ns)
            if st.st_size <= MAX_FILE_BYTES and self.seen.get(path) != sig:
                out.append((path, p, sig))
        return out

    def flush(self) -> int:
        """Push every changed file. Returns how many were pushed. Safe to call from a thread; one flush at a time."""
        if not self.enabled:
            return 0
        with self.lock:
            if not self.pulled:
                self._pull()  # never push before the table's copy is on disk, or a log could be overwritten
                if not self.pulled:
                    return 0
            batch, size, pushed = [], 0, 0
            for path, p, sig in self.changed():
                try:
                    content = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    self.seen[path] = sig  # not a text file; skip until it changes
                    continue
                batch.append((path, content, sig))
                size += len(content)
                if size >= MAX_BATCH_BYTES:
                    if not self._send(batch):
                        return pushed
                    pushed, batch, size = pushed + len(batch), [], 0
            if batch:
                if not self._send(batch):
                    return pushed
                pushed += len(batch)
            return pushed

    def _send(self, batch: list[tuple[str, str, tuple[int, int]]]) -> bool:
        try:
            self.client.push([{"path": path, "content": content} for path, content, _ in batch])
        except RemoteError as e:
            self._fail(f"push failed: {e}")
            return False
        for path, _, sig in batch:
            self.seen[path] = sig
        self._ok()
        return True

    def _ok(self) -> None:
        if self.status != "supabase":
            log.warning("storage: Supabase reachable again")
        self.status, self.error = "supabase", None

    def _fail(self, message: str) -> None:
        if self.error != message:
            log.error("storage: %s (files stay on this disk and are retried)", message)
        self.status, self.error = "error", message


def from_env(sessions_dir: Path, audit_dir: Path, packs_dir: Path, env: dict[str, str] | None = None) -> Sync:
    """Sync as configured: off (status "local") unless SYNC_ENABLED is true. A misconfiguration is an error at
    startup, never a silent fallback to local files."""
    env = read_env() if env is None else env
    client = client_from_env(env) if sync_requested(env) else None
    return Sync(client, sessions_dir, audit_dir, packs_dir)
