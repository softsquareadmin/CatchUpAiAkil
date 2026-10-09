import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditLog
from app.config import ROOT, load_settings
from app.ledger import Ledger, LedgerRow
from app.main import create_app
from app.models import Evidence, TopicState, TopicUpdate, Utterance
from app.pack import PackError, load_pack
from tests.fakes import FAKE_PRICES, FakeAnalyst, FakeDecider, FakeTranscriber


def utt(i, speaker, text):
    return Utterance(id=f"u{i}", session_id="", t_start_ms=i * 1000, t_end_ms=i * 1000 + 900,
                     speaker=speaker, text=text, is_final=True, source="replay")


def test_fake_call_writes_ledger_and_audit_rows(tmp_path):
    ledger = Ledger(tmp_path, "s1", "fake-exp", 1.0, prices=FAKE_PRICES)
    audit = AuditLog(tmp_path, "s1")
    u = utt(1, "parent", "She is eight and in third grade.")
    upd = TopicUpdate(item_id="child_age_grade", status="covered",
                      evidence=[Evidence(utterance_id="u1", quote="eight and in third grade")])
    decider = FakeDecider(ledger, audit, script={"u1": [upd]})

    assert asyncio.run(decider.evaluate([u], {})).updates == [upd]
    row = LedgerRow.model_validate_json((tmp_path / "ledger.jsonl").read_text().strip())
    assert row.role == "gate" and row.experiment == "fake-exp" and row.ok
    assert json.loads((tmp_path / "audit.jsonl").read_text())["action"] == "gate_evaluated"


def test_fake_transcriber_emits_partial_then_final():
    events = []

    async def emit(u):
        events.append((u.id, u.is_final, u.session_id))

    t = FakeTranscriber([utt(1, "worker", "Hello."), utt(2, "child", "Hi.")])
    asyncio.run(t.start("s9", emit))
    asyncio.run(t.stop())
    assert events == [("u1", False, "s9"), ("u1", True, "s9"), ("u2", False, "s9"), ("u2", True, "s9")]
    assert t.stopped


def test_fake_analyst_cue_quotes_window(tmp_path):
    ledger = Ledger(tmp_path, "s1", "x", 1.0, prices=FAKE_PRICES)
    analyst = FakeAnalyst(ledger, AuditLog(tmp_path, "s1"))
    topic = load_pack(ROOT / "packs" / "cps_interview_v1.yaml").checklist[6]
    cue = asyncio.run(analyst.write_cue(topic, TopicState(item_id=topic.id), [utt(5, "child", "He just scares me.")]))
    assert cue.evidence.quote == "He just scares me." and cue.state == "active"


def test_app_starts_with_env_example_only():
    settings = load_settings(env_file=ROOT / ".env.example", environ={})
    client = TestClient(create_app(settings))
    body = client.get("/health").json()
    assert body["ok"] and body["pack"] == "cps_interview_v2@0.4.0"  # default pack since 2026-10-08
    assert not any(body["keys_present"].values())


def test_app_rejects_invalid_pack(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: x\nversion: '1'\n")
    settings = load_settings(env_file=None, environ={}).model_copy(update={"pack": str(bad)})
    with pytest.raises(PackError, match="Pack file bad.yaml is invalid"):
        create_app(settings)
