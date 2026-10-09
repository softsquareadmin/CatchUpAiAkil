"""M12 server side of the separate pages: page routes, the sessions list, review decisions over HTTP (live session and
a past one read from disk), the cost report API, resume failure, and a review that survives the browser leaving."""
import asyncio
import json

from fastapi.testclient import TestClient

from app import main
from app.config import load_settings
from app.main import create_app

FAKE = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"}


def app_for(tmp_path):
    return create_app(load_settings(env_file=None, environ=FAKE), sessions_dir=tmp_path)


def run_interview(client) -> str:
    """Replay the CPS script, end it, wait for the review; returns the session id."""
    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session_id"]
        ws.send_json({"type": "replay", "speed": 0})
        while ws.receive_json()["type"] != "replay_done":
            pass
        ws.send_json({"type": "end_confirm"})
        while not ((m := ws.receive_json())["type"] == "review" and m["review"]["state"] == "ready"):
            pass
    return sid


def test_every_page_route_is_served(tmp_path):
    client = TestClient(app_for(tmp_path))
    sid = run_interview(client)
    for path, marker in [("/", "live.js"), (f"/review/{sid}", "review.js"), ("/config", "config.js"),
                         ("/sessions", "sessions.js"), ("/costs", "costs.js")]:
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text, path
        assert "http://" not in r.text.split("<body", 1)[1] and "https://" not in r.text.split("<body", 1)[1]
    assert client.get("/review/20990101-000000-ffff").status_code == 404
    assert client.get("/review/..%2F..%2Fetc").status_code == 404


def test_sessions_list_and_costs(tmp_path):
    client = TestClient(app_for(tmp_path))
    sid = run_interview(client)
    rows = client.get("/api/sessions").json()
    row = next(r for r in rows if r["id"] == sid)
    assert row["pack"] == "cps_interview_v2@0.4.0" and row["has_review"] and row["lines"] > 20
    assert row["status"] == "review ready" and row["duration_s"] > 60 and not row["exported"]
    costs = client.get("/api/costs").json()
    assert set(costs) == {"rows", "totals", "experiments", "health"}  # fake calls cost nothing and are left out
    assert client.get("/api/costs?experiment=nope").json()["rows"] == []


def test_review_decisions_over_http_for_a_live_and_a_past_session(tmp_path):
    client = TestClient(app_for(tmp_path))
    sid = run_interview(client)
    got = client.get(f"/api/sessions/{sid}/review").json()
    assert got["review"]["state"] == "ready" and got["pack"]["id"] == "cps_interview_v2" and got["utterances"]
    item = got["review"]["draft"]["items"][0]
    r = client.post(f"/api/sessions/{sid}/review", json={"kind": "item", "id": item["id"], "action": "accept"})
    assert r.status_code == 200 and r.json()["review"]["draft"]["items"][0]["status"] == "accepted"
    assert client.post(f"/api/sessions/{sid}/review", json={"kind": "item", "id": "nope", "action": "accept"}).status_code == 400

    # a restart: a new app on the same folder has no live sessions, so the decisions work on the saved draft
    client2 = TestClient(app_for(tmp_path))
    got = client2.get(f"/api/sessions/{sid}/review").json()
    assert not got["live"] and got["review"]["state"] == "ready" and got["review"]["draft"]["items"][0]["status"] == "accepted"
    first = got["review"]["draft"]["topics"][0]
    topic, new = first["topic_id"], "partial" if first["status"] != "partial" else "covered"
    out = client2.post(f"/api/sessions/{sid}/review", json={"kind": "status", "topic_id": topic, "status": new}).json()
    t = next(x for x in out["review"]["draft"]["topics"] if x["topic_id"] == topic)
    assert t["status_by"] == "user"
    assert client2.post(f"/api/sessions/{sid}/review", json={"kind": "viewed"}).json() == {"ok": True}
    saved = json.loads((tmp_path / sid / "draft_note.json").read_text())
    assert next(x for x in saved["topics"] if x["topic_id"] == topic)["status"] == new
    actions = [json.loads(l)["action"] for l in (tmp_path / sid / "audit.jsonl").read_text().splitlines()]
    assert {"review_accept", "topic_status_changed_by_user", "report_viewed"} <= set(actions)
    assert next(r for r in client2.get("/api/sessions").json() if r["id"] == sid)["status"] == "reviewed"


def test_resume_of_an_unknown_session_is_reported(tmp_path):
    client = TestClient(app_for(tmp_path))
    with client.websocket_connect("/ws?resume=20990101-000000-ffff") as ws:
        hello = ws.receive_json()
    assert hello["resume_failed"] == "20990101-000000-ffff" and hello["session_id"] != "20990101-000000-ffff"
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["resume_failed"] is None


def test_the_interview_begins_on_start_or_on_the_first_input(tmp_path):
    client = TestClient(app_for(tmp_path))
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["begun"] is False and hello["elapsed_ms"] == 0
        ws.send_json({"type": "start"})
        assert ws.receive_json() == {"type": "started"}
        ws.send_json({"type": "start"})  # a second press does nothing
        ws.send_json({"type": "typed", "speaker": hello["pack"]["user_role"], "text": "Hello."})
        while (m := ws.receive_json())["type"] != "utterance":
            assert m["type"] != "started"
        sid = hello["session_id"]
    with client.websocket_connect(f"/ws?resume={sid}") as ws:
        assert ws.receive_json()["begun"] is True
    with client.websocket_connect("/ws") as ws:  # no Start press: the first input begins it (scripts, older clients)
        hello = ws.receive_json()
        ws.send_json({"type": "typed", "speaker": hello["pack"]["user_role"], "text": "Hello."})
        assert ws.receive_json() == {"type": "started"}
    actions = [json.loads(l) for l in (tmp_path / sid / "audit.jsonl").read_text().splitlines()]
    assert [a["details"]["how"] for a in actions if a["action"] == "interview_started"] == ["start_button"]


def test_a_running_review_is_not_closed_when_the_browser_leaves(tmp_path, monkeypatch):
    """The browser goes away while the review is being written; the delayed close (RESUME_GRACE_S, 0 here) waits for
    the review instead of cancelling it, so the review page can still show it. A real server in a thread: TestClient
    cancels tasks started inside a WebSocket handler when the socket closes."""
    import threading
    import time

    import httpx
    from websockets.sync.client import connect

    from app.pipeline import Session
    from tests.ui_server import running_app
    monkeypatch.setattr(main, "RESUME_GRACE_S", 0)
    release, real_review = threading.Event(), Session.run_review

    async def slow_review(self):
        while not release.is_set():
            await asyncio.sleep(0.02)
        await real_review(self)
    monkeypatch.setattr(Session, "run_review", slow_review)
    with running_app(tmp_path) as base:
        with connect(base.replace("http", "ws") + "/ws") as ws:
            hello = json.loads(ws.recv())
            sid = hello["session_id"]
            ws.send(json.dumps({"type": "typed", "speaker": hello["pack"]["user_role"], "text": "Thanks for coming in."}))
            while json.loads(ws.recv())["type"] != "utterance":
                pass
            ws.send(json.dumps({"type": "end_confirm"}))
            while not ((m := json.loads(ws.recv()))["type"] == "review" and m["review"]["state"] == "running"):
                pass
        time.sleep(0.6)  # browser gone, grace period over
        got = httpx.get(f"{base}/api/sessions/{sid}/review").json()
        assert got["live"] and got["review"]["state"] == "running"
        release.set()
        for _ in range(100):
            got = httpx.get(f"{base}/api/sessions/{sid}/review").json()
            if not got["live"]:
                break
            time.sleep(0.05)
        assert not got["live"]  # closed once the review was done
        assert got["review"]["state"] == "ready" and got["review"]["draft"]["topics"]
