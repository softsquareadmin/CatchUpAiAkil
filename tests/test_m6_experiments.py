import asyncio
import json

from fastapi.testclient import TestClient

from app import golden
from app.audit import AuditLog
from app.config import ROOT, load_settings
from app.ledger import Ledger
from app.main import FIXTURE, create_app
from app.pack import load_pack
from app.pipeline import Session
from app.store import EventLog, TranscriptStore
from app.models import Utterance
from tests.fakes import FAKE_PRICES

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
SCRIPT = json.loads(FIXTURE.read_text())


def fake_run(root, sid, experiment, final: dict, gate=("openrouter", "google/gemini-3.8-flash"), cues=()):
    """A saved session as the app would write it: full fixture transcript, topic updates, ledger, audit."""
    d = root / sid
    store, events = TranscriptStore(d), EventLog(d)
    for ln in SCRIPT:
        store.append(Utterance(id="", session_id=sid, t_start_ms=ln["t_ms"], t_end_ms=ln["t_end_ms"],
                               speaker=ln["speaker"], text=ln["text"], is_final=True, source="replay"))
    for topic, (status, follow) in final.items():
        events.write("topic_update", state={"item_id": topic, "status": status, "follow_up": follow,
                                            "follow_up_reason": "", "evidence": [], "updated_at_ms": 0, "rationale_short": ""})
    for c in cues:
        events.write("cue", cue=c)
    AuditLog(d, sid).write("system", "session_start", experiment=experiment, pack=f"{PACK.id}@{PACK.version}")
    led = Ledger(d, sid, experiment, 1.0, prices={f"{gate[0]}:{gate[1]}": {"input_usd_per_mtok": 1.0}})
    led.record("gate", gate[0], gate[1], input_tokens=1000, latency_ms=3000)


PERFECT = {"recording_consent": ("covered", "none"), "child_age_grade": ("covered", "none"),
           "parent_account": ("covered", "none"), "child_account": ("partial", "suggested"),
           "household_members": ("covered", "suggested"), "caregivers": ("covered", "none"),
           "child_safety_feelings": ("covered", "suggested"), "medical_attention": ("covered", "none")}


def test_golden_scores_runs_per_combination_and_topic(tmp_path):
    fake_run(tmp_path, "20261009-000001-aaaa", "expA", PERFECT)
    fake_run(tmp_path, "20261009-000002-bbbb", "expA", {**PERFECT, "child_safety_feelings": ("covered", "none"),
                                                         "prior_injuries": ("partial", "none")})
    fake_run(tmp_path, "20261009-000003-cccc", "expB", PERFECT, gate=("fake", "keyword"))
    lead = {"id": "c001", "topic_id": "child_safety_feelings", "kind": "follow_up", "question_or_note": "Did Dave hit you?",
            "evidence": {"utterance_id": "u0020", "quote": "he just scares me"}, "created_at_ms": 0, "state": "active"}
    fake_run(tmp_path, "20261009-000004-dddd", "expC", PERFECT, cues=[lead])
    (tmp_path / "20261009-000005-eeee").mkdir()  # not a fixture run: ignored
    g = golden.build(tmp_path)
    rows = {r["experiment"]: r for r in g["rows"]}
    assert set(rows) == {"expA", "expC"}  # the offline keyword gate is left out by default
    a = rows["expA"]
    assert a["runs"] == 2 and a["final_status_of_11"] == 10.5 and a["status_and_required_of_11"] == 10.5
    assert a["required_missed"] == 0  # suggested vs none is advisory
    name = next(n for n in g["per_topic"] if n.startswith("expA"))
    assert g["per_topic"][name]["prior_injuries"] == "1/2" and g["per_topic"][name]["child_safety_feelings"] == "2/2"
    assert rows["expC"]["lint_failures"] == 1 and g["review"][0]["lint"].startswith("yes/no")
    assert {r["experiment"] for r in golden.build(tmp_path, include_fake=True)["rows"]} == {"expA", "expB", "expC"}
    golden.main(["--sessions", str(tmp_path)])  # prints without error


def test_golden_skips_older_packs_unless_asked(tmp_path):
    fake_run(tmp_path, "20261009-000001-aaaa", "old", PERFECT)
    audit = tmp_path / "20261009-000001-aaaa" / "audit.jsonl"
    audit.write_text(audit.read_text().replace(f"{PACK.id}@{PACK.version}", "cps_interview_v2@0.2.0"))
    assert golden.build(tmp_path)["rows"] == []
    assert golden.build(tmp_path, all_packs=True)["rows"][0]["pack"] == "cps_interview_v2@0.2.0"


def session(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    s = Session(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword"}), PACK, tmp_path, send)
    s.ledger.prices = FAKE_PRICES
    return s, sent


def test_experiment_bar_stats_and_label_rules(tmp_path):
    s, sent = session(tmp_path)

    async def go():
        assert await s.set_experiment("bad/label") is not None
        assert await s.set_experiment("gate=flash cue=flash") is None
        s.ledger.record("gate", "fake", "fake", input_tokens=10, latency_ms=3000)
        s.ledger.record("gate", "fake", "fake", ok=False, error="x")
        s.ledger.record("cue", "fake", "fake", input_tokens=10, latency_ms=2000)
        s.ledger.record("stt", "fake", "fake", audio_seconds=30)
        await asyncio.sleep(0)
        assert await s.set_experiment("later") is not None  # rows exist: label fixed
    asyncio.run(go())
    x = s.experiment_status()
    assert x["label"] == "gate=flash cue=flash" and not x["editable"]
    assert x["roles"]["gate"]["calls"] == 2 and x["roles"]["gate"]["failed"] == 1 and x["roles"]["gate"]["p50_ms"] == 3000
    assert x["roles"]["stt"]["seconds"] == 30 and x["total_usd"] > 0
    rows = [json.loads(l) for l in (tmp_path / s.id / "ledger.jsonl").read_text().splitlines()]
    assert {r["experiment"] for r in rows} == {"gate=flash cue=flash"}
    assert any(m["type"] == "experiment" and m["experiment"]["roles"].get("cue") for m in sent)
    start = json.loads((tmp_path / s.id / "audit.jsonl").read_text().splitlines()[0])["details"]
    assert start["pack"] == f"{PACK.id}@{PACK.version}" and start["models"]["review"]


def test_every_role_switches_with_env_only():
    s = load_settings(env_file=None, environ={"GATE_MODEL": "openrouter:a/b", "CUE_MODEL": "openrouter:c/d",
                                              "REVIEW_MODEL": "openrouter:e/f", "STT_MODEL": "assemblyai:universal-streaming-english",
                                              "GATE_ADAPTER": "hybrid", "EXPERIMENT_LABEL": "x"})
    assert s.models == {"gate": "openrouter:a/b", "cue": "openrouter:c/d", "review": "openrouter:e/f",
                        "stt": "assemblyai:universal-streaming-english"}
    assert s.gate_adapter == "hybrid" and s.experiment_label == "x"


def test_ws_experiment_label_and_bar(tmp_path):
    app = create_app(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template"}),
                     sessions_dir=tmp_path)
    with TestClient(app).websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["experiment"]["editable"] and hello["experiment"]["label"] == "default"
        ws.send_json({"type": "experiment", "label": "demo-1"})
        while (m := ws.receive_json())["type"] != "experiment":
            pass
        assert m["experiment"]["label"] == "demo-1"
        ws.send_json({"type": "typed", "speaker": "parent", "text": "I consent"})
        while not ((m := ws.receive_json())["type"] == "experiment" and m["experiment"]["roles"].get("gate")):
            pass
        assert not m["experiment"]["editable"]
        ws.send_json({"type": "experiment", "label": "demo-2"})
        while (m := ws.receive_json())["type"] != "error":
            pass
        sid = hello["session_id"]
    rows = [json.loads(l) for l in (tmp_path / sid / "ledger.jsonl").read_text().splitlines()]
    assert rows and all(r["experiment"] == "demo-1" for r in rows)
