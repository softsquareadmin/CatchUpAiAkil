import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.adapters.transcribers.replay import (ReplayTranscriber, load_script, parse_paste, paste_to_script,
                                              suggest_mapping)
from app.adapters.transcribers.typed import TypedTranscriber
from app.config import ROOT, load_settings
from app.main import FIXTURE, create_app
from app.models import Utterance
from app.pack import load_pack
from app.store import TranscriptStore

CPS = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
SCRIPT = load_script(FIXTURE, CPS.role_ids)
LABELS = {"worker": "Worker", "parent": "Mom", "child": "Jill"}


def collect(transcriber, session_id="s1"):
    out = []

    async def emit(u):
        out.append(u)

    asyncio.run(transcriber.start(session_id, emit))
    return out


# ---- replay ----

def test_fixture_has_23_lines_with_known_speakers():
    assert len(SCRIPT) == 23
    assert {ln["speaker"] for ln in SCRIPT} == {"worker", "parent", "child"}


@pytest.mark.parametrize("speed,factor", [(1, 1.0), (2, 0.5), (0, 0.0)])
def test_replay_timing_follows_end_of_turn(speed, factor):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    out = collect(ReplayTranscriber(SCRIPT, speed, sleep=fake_sleep))
    assert [(u.speaker, u.text) for u in out] == [(ln["speaker"], ln["text"]) for ln in SCRIPT]
    assert all(u.is_final and u.source == "replay" for u in out)
    if speed:
        assert sum(waits) == pytest.approx(SCRIPT[-1]["t_end_ms"] / 1000 * factor)
        assert waits[1] == pytest.approx((SCRIPT[1]["t_end_ms"] - SCRIPT[0]["t_end_ms"]) / 1000 * factor)
    else:
        assert waits == []


def test_replay_stop_halts_emission():
    t = ReplayTranscriber(SCRIPT, 1)
    out = []

    async def emit(u):
        out.append(u)
        if len(out) == 3:
            await t.stop()

    async def fast_sleep(_):
        pass

    t.sleep = fast_sleep
    asyncio.run(t.start("s1", emit))
    assert len(out) == 3


# ---- paste ----

def test_parse_paste_labels_continuations_and_mapping():
    turns, labels = parse_paste("Caseworker: Hello.\nMom: Hi there.\n  and welcome\n\nKid: Hi.\nMs. X: ok")
    assert turns[1] == ("Mom", "Hi there. and welcome")
    assert labels == ["Caseworker", "Mom", "Kid", "Ms. X"]
    assert suggest_mapping(labels, CPS.roles) == {"Caseworker": "worker", "Mom": "parent", "Kid": "child", "Ms. X": "unknown"}


def test_paste_script_timing_is_increasing():
    lines = paste_to_script([("A", "one two three"), ("B", "four")], {"A": "worker", "B": "nobody"}, CPS.role_ids)
    assert lines[0]["speaker"] == "worker" and lines[1]["speaker"] == "unknown"
    assert lines[0]["t_end_ms"] < lines[1]["t_ms"] < lines[1]["t_end_ms"]


# ---- typed and store ----

def test_typed_emits_one_final_utterance_and_ignores_blank():
    out = []

    async def run():
        async def emit(u):
            out.append(u)
        t = TypedTranscriber()
        await t.start("s1", emit)
        await t.submit("child", "  He just scares me. ", 500)
        await t.submit("child", "   ", 600)

    asyncio.run(run())
    assert len(out) == 1
    assert (out[0].speaker, out[0].text, out[0].is_final, out[0].source) == ("child", "He just scares me.", True, "typed")


def test_store_assigns_sequential_ids_and_appends(tmp_path):
    store = TranscriptStore(tmp_path)
    base = dict(session_id="s1", t_start_ms=0, t_end_ms=1, speaker="worker", is_final=True, source="typed")
    a = store.append(Utterance(id="", text="one", **base))
    b = store.append(Utterance(id="x", text="two", **base))
    assert (a.id, b.id) == ("u0001", "u0002")
    rows = [json.loads(l) for l in (tmp_path / "utterances.jsonl").read_text().splitlines()]
    assert [r["text"] for r in rows] == ["one", "two"]
    with pytest.raises(ValueError):
        store.append(Utterance(id="", text="partial", **{**base, "is_final": False}))


# ---- end to end over the WebSocket ----

def client(tmp_path):
    settings = load_settings(env_file=ROOT / ".env.example", environ={})
    return TestClient(create_app(settings, sessions_dir=tmp_path))


def run_ws(tmp_path, messages, expect):
    """Send messages, then read until `expect` utterances arrive. Returns (session_id, utterances)."""
    with client(tmp_path).websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session_id"]
        for m in messages:
            ws.send_json(m)
        got = []
        while len(got) < expect:
            msg = ws.receive_json()
            assert msg["type"] != "error", msg
            if msg["type"] == "utterance":
                got.append(msg["utterance"])
    return sid, got


def strip(u):
    return {k: u[k] for k in ("id", "speaker", "text", "is_final")}


def test_ws_replay_instant_shows_23_lines_and_saves_them(tmp_path):
    sid, got = run_ws(tmp_path, [{"type": "mode", "mode": "replay"}, {"type": "replay", "speed": 0}], 23)
    assert [(u["speaker"], u["text"]) for u in got] == [(ln["speaker"], ln["text"]) for ln in SCRIPT]
    saved = [json.loads(l) for l in (tmp_path / sid / "utterances.jsonl").read_text().splitlines()]
    assert [strip(u) for u in saved] == [strip(u) for u in got]
    audit = (tmp_path / sid / "audit.jsonl").read_text()
    assert '"input_mode"' in audit and '"replay_start"' in audit


def test_ws_typed_and_paste_produce_same_events_as_replay(tmp_path):
    _, replayed = run_ws(tmp_path, [{"type": "replay", "speed": 0}], 23)
    typed_msgs = [{"type": "typed", "speaker": ln["speaker"], "text": ln["text"]} for ln in SCRIPT]
    _, typed = run_ws(tmp_path, typed_msgs, 23)
    paste_text = "\n".join(f"{LABELS[ln['speaker']]}: {ln['text']}" for ln in SCRIPT)
    mapping = {v: k for k, v in LABELS.items()}
    _, pasted = run_ws(tmp_path, [{"type": "paste_play", "text": paste_text, "mapping": mapping, "speed": 0}], 23)

    assert [strip(u) for u in typed] == [strip(u) for u in replayed]
    assert [strip(u) for u in pasted] == [strip(u) for u in replayed]
    assert {u["source"] for u in typed} == {"typed"} and {u["source"] for u in pasted} == {"paste"}
    assert set(typed[0]) == set(replayed[0]) == set(pasted[0])


def test_ws_paste_parse_and_bad_input(tmp_path):
    with client(tmp_path).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "paste_parse", "text": "Mom: hi\nWorker: hello"})
        msg = ws.receive_json()
        assert msg == {"type": "paste_labels", "labels": ["Mom", "Worker"],
                       "mapping": {"Mom": "parent", "Worker": "worker"}}
        ws.send_json({"type": "typed", "speaker": "dog", "text": "x"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "replay", "speed": 5})
        assert "speed" in ws.receive_json()["message"]
        ws.send_json({"type": "gate", "adapter": "hybrid"})  # gate choice is config-only
        assert "unknown message type" in ws.receive_json()["message"]


def test_index_and_static_served(tmp_path):
    c = client(tmp_path)
    assert "Interview Assistant" in c.get("/").text
    for p in ("/static/js/common.js", "/static/js/live.js", "/static/css/tokens.css", "/static/css/components.css", "/static/css/pages.css"):
        assert c.get(p).status_code == 200, p
