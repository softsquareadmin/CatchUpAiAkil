"""Deployment pieces (SPEC-deploy): Supabase file sync against a fake PostgREST server, the passcode gate for pages,
/api and the websocket, and the overall spend cap. No network, no real keys."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import remote_store
from app.access import AccessConfigError, PasscodeGate, access_from_env, safe_next
from app.config import ROOT, load_settings
from app.ledger import TOTALS, BudgetExceeded, Ledger
from app.main import create_app
from app.remote_store import Client, RemoteError, Sync

SECRET = "x" * 32


class FakeSupabase:
    """Enough of PostgREST for ia_files: GET with path=like./eq. filters and paging, POST upsert of a list."""

    def __init__(self):
        self.rows: dict[str, str] = {}
        self.down = False
        self.posts = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if fake.down:
                    return self.reply(503, {"code": "down"})
                if self.headers.get("apikey") != "sb_secret_test":
                    return self.reply(401, {"code": "401"})
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                rows = sorted(fake.rows.items())
                f = q.get("path", "")
                if f.startswith("like."):
                    rows = [r for r in rows if r[0].startswith(f[5:].rstrip("*"))]
                elif f.startswith("eq."):
                    rows = [r for r in rows if r[0] == f[3:]]
                off, lim = int(q.get("offset", 0)), int(q.get("limit", 10 ** 6))
                self.reply(200, [{"path": p, "content": c} for p, c in rows[off:off + lim]])

            def do_POST(self):
                if fake.down:
                    return self.reply(503, {"code": "down"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert "merge-duplicates" in self.headers.get("Prefer", "")
                for r in body:
                    fake.rows[r["path"]] = r["content"]
                fake.posts += 1
                self.reply(201)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        return Client(self.url, "sb_secret_test", timeout=5)


@pytest.fixture
def supa():
    s = FakeSupabase()
    yield s
    s.server.shutdown()


def dirs(tmp_path, name="host"):
    base = tmp_path / name
    return base / "sessions", base / "audit_logs", base / "packs"


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_push_then_a_fresh_disk_pulls_everything_back(tmp_path, supa):
    sessions, audit, packs = dirs(tmp_path)
    sync = Sync(supa.client(), sessions, audit, packs)
    sync.start()
    write(sessions / "20261014-101500-ab12" / "ledger.jsonl", '{"cost_usd": 0.01}\n')
    write(sessions / "20261014-101500-ab12" / "audio.pcm", "not synced")
    write(audit / "config.jsonl", '{"action": "pack_saved"}\n')
    write(packs / "demo__0-2-0.yaml", "id: demo\n")
    write(packs / "demo.yaml", "id: demo  # repo pack\n")
    assert sync.flush() == 3 and sync.status == "supabase"
    assert set(supa.rows) == {"sessions/20261014-101500-ab12/ledger.jsonl", "audit_logs/config.jsonl",
                              "packs/demo__0-2-0.yaml"}
    assert sync.flush() == 0  # unchanged files are not sent again

    restarted = dirs(tmp_path, "after-restart")
    Sync(supa.client(), *restarted).start()
    assert (restarted[0] / "20261014-101500-ab12" / "ledger.jsonl").read_text() == '{"cost_usd": 0.01}\n'
    assert (restarted[1] / "config.jsonl").exists() and (restarted[2] / "demo__0-2-0.yaml").exists()
    again = Sync(supa.client(), *restarted)
    again.start()
    assert again.flush() == 0  # pulled files count as synced


def test_sessions_already_on_disk_at_startup_are_never_uploaded(tmp_path, supa):
    sessions, audit, packs = dirs(tmp_path)
    write(sessions / "20261001-090000-aaaa" / "utterances.jsonl", '{"text": "a real recording"}\n')
    sync = Sync(supa.client(), sessions, audit, packs)
    sync.start()
    write(sessions / "20261014-101500-bbbb" / "utterances.jsonl", '{"text": "new"}\n')
    write(sessions / "20261001-090000-aaaa" / "draft_note.json", "{}")  # reviewed later: still local only
    sync.flush()
    assert list(supa.rows) == ["sessions/20261014-101500-bbbb/utterances.jsonl"]


def test_pull_never_replaces_a_pack_on_disk_and_ignores_repo_pack_names(tmp_path, supa):
    sessions, audit, packs = dirs(tmp_path)
    write(packs / "demo__0-2-0.yaml", "id: demo  # newer copy in the repo\n")
    supa.rows.update({"packs/demo__0-2-0.yaml": "id: demo  # stale\n", "packs/demo.yaml": "id: hijack\n",
                      "packs/../../x__1.yaml": "x", "sessions/../escape.txt": "x"})
    Sync(supa.client(), sessions, audit, packs).start()
    assert (packs / "demo__0-2-0.yaml").read_text() == "id: demo  # newer copy in the repo\n"
    assert not (packs / "demo.yaml").exists() and not (tmp_path / "x__1.yaml").exists()
    assert not (sessions.parent / "escape.txt").exists()


def test_unreachable_supabase_keeps_files_and_retries(tmp_path, supa):
    sessions, audit, packs = dirs(tmp_path)
    sync = Sync(supa.client(), sessions, audit, packs)
    sync.start()
    supa.down = True
    write(sessions / "20261014-101500-ab12" / "events.jsonl", "{}\n")
    assert sync.flush() == 0 and sync.status == "error" and "503" in sync.error
    supa.down = False
    assert sync.flush() == 1 and sync.status == "supabase" and sync.error is None


def test_no_push_before_a_successful_pull_and_logs_are_merged(tmp_path, supa):
    sessions, audit, packs = dirs(tmp_path)
    supa.rows["audit_logs/config.jsonl"] = '{"n": 1}\n'
    supa.down = True
    sync = Sync(supa.client(), sessions, audit, packs)
    sync.start()
    assert sync.status == "error" and not sync.pulled
    write(audit / "config.jsonl", '{"n": 2}\n')  # saved while Supabase was down
    assert sync.flush() == 0 and supa.posts == 0
    supa.down = False
    sync.flush()
    assert supa.rows["audit_logs/config.jsonl"] == '{"n": 1}\n{"n": 2}\n'


def test_client_rejects_bad_urls_and_maps_errors(supa):
    with pytest.raises(RemoteError, match="HTTPS"):
        Client("http://example.com", "k")
    assert Client("https://abcd.supabase.co/rest/v1/", "k").url == "https://abcd.supabase.co"
    with pytest.raises(RemoteError, match="secret key"):
        Client(supa.url, "sb_secret_wrong").pull("sessions/")


def test_sync_is_off_unless_enabled_and_shows_in_health(tmp_path, supa):
    env = {"SUPABASE_URL": supa.url, "SUPABASE_SECRET_KEY": "sb_secret_test"}
    settings = load_settings(env_file=ROOT / ".env.example", environ={})
    with TestClient(create_app(settings, tmp_path / "s1", env=env)) as c:
        assert c.get("/health").json()["storage"] == "local"
    with pytest.raises(RemoteError):
        create_app(settings, tmp_path / "s2", env={"SYNC_ENABLED": "true"})  # never a silent fallback
    with TestClient(create_app(settings, tmp_path / "s3", env={**env, "SYNC_ENABLED": "true"})) as c:
        h = c.get("/health").json()
        assert h["storage"] == "supabase" and h["access"] == "open"
        c.post("/api/packs/test", json={"base_file": "job_interview_v1.yaml", "pack": {},
                                        "script": "job_interview_sample.json"})
    assert any(p.startswith("sessions/") for p in supa.rows)  # flushed at shutdown at the latest


# ---- passcode gate ----
def gated_app(tmp_path):
    settings = load_settings(env_file=ROOT / ".env.example", environ={})
    app = create_app(settings, tmp_path, env={"APP_PASSCODE": "open sesame", "SESSION_SECRET": SECRET})
    for m in app.user_middleware:
        if m.cls is PasscodeGate:
            m.kwargs["fail_delay_s"] = 0
    return app


def test_gate_blocks_pages_api_and_websocket_until_signed_in(tmp_path):
    c = TestClient(gated_app(tmp_path))
    assert c.get("/health").json()["access"] == "passcode"
    assert c.get("/static/css/tokens.css").status_code == 200
    r = c.get("/sessions?x=1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fsessions%3Fx%3D1"
    assert c.get("/api/sessions").status_code == 401
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
    assert e.value.code == 1008
    page = c.get("/login?next=/sessions").text
    assert 'value="/sessions"' in page and "{{" not in page

    assert c.post("/login", data={"passcode": "wrong", "next": "/"}).status_code == 401
    r = c.post("/login", data={"passcode": "open sesame", "next": "/sessions"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=Lax" in r.headers["set-cookie"]
    assert c.get("/api/sessions").status_code == 200
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "session"
    c.get("/logout")
    assert c.get("/api/sessions").status_code == 401


def test_gate_throttles_wrong_passcodes_and_rejects_forged_cookies(tmp_path):
    c = TestClient(gated_app(tmp_path))
    for _ in range(5):
        assert c.post("/login", data={"passcode": "nope"}).status_code == 401
    assert c.post("/login", data={"passcode": "open sesame"}).status_code == 429  # even the right one, for now
    c.cookies.set("ia_access", "9999999999.deadbeef")
    assert c.get("/api/sessions").status_code == 401


def test_gate_tokens_expire_and_need_the_same_passcode():
    gate = PasscodeGate(None, "a", SECRET, clock=lambda: 1000.0)
    token = gate.token()
    assert gate.valid(token) and not PasscodeGate(None, "b", SECRET, clock=lambda: 1000.0).valid(token)
    assert not PasscodeGate(None, "a", SECRET, clock=lambda: 1000.0 + 8 * 24 * 3600).valid(token)


def test_passcode_without_secret_is_a_startup_error_and_next_stays_on_site():
    assert access_from_env({}) is None
    with pytest.raises(AccessConfigError):
        access_from_env({"APP_PASSCODE": "x", "SESSION_SECRET": "short"})
    assert safe_next("//evil.example") == "/" and safe_next("https://evil.example") == "/"
    assert safe_next("/review/1") == "/review/1"


# ---- overall spend cap ----
def test_overall_cap_stops_paid_calls_and_real_template_tests(tmp_path):
    write(tmp_path / "20261014-101500-ab12" / "ledger.jsonl", json.dumps({"cost_usd": 0.6}) + "\n")
    settings = load_settings(env_file=ROOT / ".env.example", environ={"MAX_TOTAL_COST_USD": "0.5"})
    c = TestClient(create_app(settings, tmp_path, env={}))
    assert c.get("/health").json()["spend"] == {"total_usd": 0.6, "cap_usd": 0.5}
    out = c.post("/api/packs/test", json={"base_file": "job_interview_v1.yaml", "pack": {},
                                          "script": "job_interview_sample.json", "real": True}).json()
    assert out["ok"] is False and "limit" in out["error"]
    ledger = Ledger(tmp_path / "20261014-120000-cdef", "x", "default", 1.0, prices={})
    with pytest.raises(BudgetExceeded, match=r"\$0\.60 of its \$0\.50"):
        ledger.check_budget()
    offline = c.post("/api/packs/test", json={"base_file": "job_interview_v1.yaml", "pack": {},
                                              "script": "job_interview_sample.json"}).json()
    assert offline["ok"] is True  # the offline models still work


def test_no_cap_by_default(tmp_path):
    settings = load_settings(env_file=ROOT / ".env.example", environ={})
    create_app(settings, tmp_path, env={})
    assert settings.max_total_cost_usd == 0 and tmp_path.resolve() not in TOTALS
