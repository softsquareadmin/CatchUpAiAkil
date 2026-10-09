import asyncio
import json
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.adapters.transcribers.assemblyai import AssemblyAITranscriber
from app.config import ROOT, load_settings
from app.ledger import Ledger
from app.main import create_app
from app.pack import load_pack
from app.models import Utterance
from app.pipeline import Session

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")


def turn(text, final, label="A", start=100, end=900, order=0):
    return {"type": "Turn", "turn_order": order, "end_of_turn": final, "transcript": text, "speaker_label": label,
            "speaker_confidence": 0.9, "words": [{"text": w, "start": start, "end": end} for w in text.split()]}


class FakeAAI:
    """Scripted AssemblyAI socket. "DROP" raises as a dropped connection would; at the end of the script it waits
    for Terminate and answers with Termination."""

    def __init__(self, script):
        self.script, self.sent, self.closed = list(script), [], False
        self.terminated = asyncio.Event()

    async def send(self, data):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["type"] == "Terminate":
            self.terminated.set()

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        for item in self.script:
            await asyncio.sleep(0.01)
            if item == "DROP":
                raise ConnectionError("dropped")
            yield json.dumps(item)
        await self.terminated.wait()
        yield json.dumps({"type": "Termination", "audio_duration_seconds": 1.0, "session_duration_seconds": 3.0})

    async def close(self):
        self.closed = True


def make(tmp_path, sockets, **kw):
    calls, emitted, statuses = [], [], []

    async def connect(url, headers):
        calls.append((url, headers))
        s = sockets.pop(0)
        if isinstance(s, Exception):
            raise s
        return s

    async def emit(u):
        emitted.append(u)

    async def on_status(state, detail=""):
        statuses.append(state)

    ledger = Ledger(tmp_path, "s", "x", 1.0)  # real prices.json
    t = AssemblyAITranscriber("secret-key", "universal-3-6-pro", ledger, speaker_for=lambda l: {"A": "worker"}.get(l, "unknown"),
                              elapsed_ms=lambda: 5000, on_status=on_status, connect=connect, backoff_s=(0.01,), **kw)
    return t, calls, emitted, statuses, ledger


def test_turns_become_partial_then_final_with_mapped_speaker_and_timing(tmp_path):
    sock = FakeAAI([turn("I am", False), turn("I am John.", True), turn("Yes you may.", True, label="B")])
    t, calls, emitted, statuses, ledger = make(tmp_path, [sock])

    async def go():
        await t.start("s1", emit=lambda u: _append(emitted, u))
        await asyncio.sleep(0.1)
        await t.send_audio(b"\x00\x01" * 1600)
        await t.stop()
    asyncio.run(go())
    assert [(u.text, u.is_final, u.speaker) for u in emitted] == [
        ("I am", False, "worker"), ("I am John.", True, "worker"), ("Yes you may.", True, "unknown")]
    assert emitted[1].t_start_ms == 5100 and emitted[1].t_end_ms == 5900 and emitted[1].source == "mic"
    url, headers = calls[0]
    q = parse_qs(urlparse(url).query)
    assert url.startswith("wss://streaming.assemblyai.com/v3/ws")
    assert q["speech_model"] == ["universal-3-6-pro"] and q["sample_rate"] == ["16000"] and q["encoding"] == ["pcm_s16le"]
    assert q["speaker_labels"] == ["true"] and q["max_speakers"] == ["3"]
    assert headers == {"Authorization": "secret-key"} and "secret-key" not in url
    assert b"\x00\x01" * 1600 in sock.sent and json.loads(sock.sent[-1]) == {"type": "Terminate"}
    assert statuses[:2] == ["connecting", "listening"] and statuses[-1] == "stopped"
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["role"] == "stt" and rows[0]["model"] == "universal-3-6-pro+speaker_labels"
    assert rows[0]["audio_seconds"] >= 3.0  # billed by connection time: the larger of wall time and the provider's figure
    assert abs(rows[0]["cost_usd"] - rows[0]["audio_seconds"] * 0.57 / 3600) < 1e-9


async def _append(lst, u):
    lst.append(u)


def test_reconnects_after_drop_and_keeps_audio_sent_meanwhile(tmp_path):
    first = FakeAAI([turn("Hello.", True), "DROP"])
    second = FakeAAI([turn("Still here.", True)])
    t, calls, emitted, statuses, ledger = make(tmp_path, [first, OSError("refused"), second])

    async def go():
        await t.start("s1", emit=lambda u: _append(emitted, u))
        while not any(u.text == "Hello." for u in emitted):
            await asyncio.sleep(0.01)
        while t.ws is not None:  # wait for the drop
            await asyncio.sleep(0.005)
        await t.send_audio(b"gap-audio")  # arrives while reconnecting
        while "listening" not in statuses[2:]:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await t.stop()
    asyncio.run(go())
    assert [u.text for u in emitted if u.is_final] == ["Hello.", "Still here."]
    assert "reconnecting" in statuses and len(calls) == 3
    assert b"gap-audio" in second.sent
    rows = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and not rows[0]["ok"] and "dropped" in rows[0]["error"] and rows[1]["ok"]


def test_gives_up_after_max_retries(tmp_path):
    t, calls, emitted, statuses, ledger = make(tmp_path, [OSError("no")] * 3, max_retries=3)

    async def go():
        await t.start("s1", emit=lambda u: _append(emitted, u))
        await t.task
    asyncio.run(go())
    assert statuses[-1] == "failed" and len(calls) == 3 and not t.running
    assert not (tmp_path / "ledger.jsonl").exists() or not (tmp_path / "ledger.jsonl").read_text()


# ---- session ----

def session(tmp_path, **env):
    sent = []

    async def send(m):
        sent.append(m)
    s = Session(load_settings(env_file=None, environ=env), PACK, tmp_path, send, decider=None, analyst=object())
    s.decider = s.analyst = None
    return s, sent


def test_speaker_choice_manual_then_mapping_then_unknown(tmp_path):
    s, sent = session(tmp_path)

    async def go():
        assert s.speaker_for("A") == "unknown"
        await asyncio.sleep(0)
        assert await s.set_speaker("A", "parent") is None
        assert s.speaker_for("A") == "parent" and s.speaker_for("PENDING") == "unknown"
        await s.set_speaker(None, "child")
        assert s.speaker_for("A") == "child"
        await s.set_speaker(None, None)
        assert s.speaker_for("A") == "parent"
        assert await s.set_speaker("A", "judge")
    asyncio.run(go())
    assert s.labels_seen == ["A"]
    assert any(m["type"] == "mic_status" and m["mic"]["labels"] == ["A"] for m in sent)
    assert "speaker_set" in (tmp_path / s.id / "audit.jsonl").read_text()


def test_start_mic_without_key_explains(tmp_path):
    s, _ = session(tmp_path)
    assert "ASSEMBLYAI_API_KEY is not set" in asyncio.run(s.start_mic())


def test_mic_session_stores_finals_logs_labels_and_audio_when_enabled(tmp_path):
    s, sent = session(tmp_path, ASSEMBLYAI_API_KEY="k", STORE_AUDIO="true")
    sock = FakeAAI([turn("I consent", False, label="B"), turn("I consent.", True, label="B")])

    async def connect(url, headers):
        return sock
    real = s.build_mic

    def build():
        mic, reason = real()
        mic.connect = connect
        return mic, reason
    s.build_mic = build

    async def go():
        assert await s.start_mic() is None
        await s.mic_audio(b"\x01\x02" * 800)
        await asyncio.sleep(0.1)
        await s.set_speaker("B", "child")
        await s.stop_mic()
    asyncio.run(go())
    # mapped after the line was spoken: the line takes the mapping (a correction record; the file stays append-only)
    assert [(u.text, u.speaker) for u in s.store.utterances] == [("I consent.", "child")]
    from app.store import read_utterances
    assert [u["speaker"] for u in read_utterances(tmp_path / s.id)] == ["child"]
    assert json.loads((tmp_path / s.id / "utterances.jsonl").read_text())["speaker"] == "unknown"
    assert any(m["type"] == "utterance_update" and m["utterance"]["speaker"] == "child" for m in sent)
    assert any(m["type"] == "utterance" and not m["utterance"]["is_final"] for m in sent)  # partial shown first
    events = (tmp_path / s.id / "events.jsonl").read_text()
    assert '"stt_label"' in events and '"label": "B"' in events
    assert (tmp_path / s.id / "audio.pcm").read_bytes() == b"\x01\x02" * 800
    audit = (tmp_path / s.id / "audit.jsonl").read_text()
    for action in ("audio_stored", "mic_start", "mic_stop", "speaker_relabelled"):
        assert action in audit
    assert s.mic is None


# ---- browser reconnect ----

def test_browser_reconnect_resumes_session_with_transcript(tmp_path):
    app = create_app(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template"}),
                     sessions_dir=tmp_path)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session_id"]
        ws.send_json({"type": "typed", "speaker": "parent", "text": "Yes, you may use that tool."})
        while ws.receive_json()["type"] != "gate_status":
            pass
        ws.send_json({"type": "note", "text": "first note"})
        while ws.receive_json()["type"] != "note":
            pass
        ws.send_bytes(b"\x00" * 3200)  # audio with no mic running is ignored
    with client.websocket_connect(f"/ws?resume={sid}") as ws:
        hello = ws.receive_json()
        assert hello["session_id"] == sid
        assert [u["text"] for u in hello["utterances"]] == ["Yes, you may use that tool."]
        assert hello["state"]["recording_consent"]["status"] == "partial"
        assert hello["notes"][0]["text"] == "first note" and hello["mic"]["state"] == "off"
    with client.websocket_connect("/ws?resume=unknown") as ws:
        assert ws.receive_json()["session_id"] != sid
    audit = (tmp_path / sid / "audit.jsonl").read_text()
    assert "browser_disconnected" in audit and "browser_reconnected" in audit
    assert len([l for l in (tmp_path / sid / "utterances.jsonl").read_text().splitlines()]) == 1


def test_unfinished_turn_audio_is_resent_after_drop_but_finished_audio_is_not(tmp_path):
    first = FakeAAI([])  # no messages until we drop it
    second = FakeAAI([])
    t, calls, emitted, statuses, ledger = make(tmp_path, [first, second])

    async def go():
        await t.start("s1", emit=lambda u: _append(emitted, u))
        while t.ws is None:
            await asyncio.sleep(0.005)
        for i in range(4):  # 4 x 100 ms
            await t.send_audio(bytes([i]) * 3200)
        await t._turn(turn("done part", True, start=0, end=200), 0)  # first 200 ms finalized
        first.terminated.set()  # the server ends the first connection unasked (as on a drop): reconnect
        while "listening" not in statuses[2:]:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await t.stop()
    asyncio.run(go())
    resent = [c for c in second.sent if isinstance(c, bytes)]
    assert resent == [bytes([2]) * 3200, bytes([3]) * 3200]  # only the audio after the last final turn
    assert "reconnecting" in statuses


def test_label_mapping_resets_when_assemblyai_reconnects(tmp_path):
    s, sent = session(tmp_path)

    async def go():
        s.speaker_for("A")
        await s.set_speaker("A", "parent")
        await s._mic_status("listening", "reconnected")
    asyncio.run(go())
    assert s.speaker_map == {} and s.labels_seen == []
    assert any("labels reset" in m.get("detail", "") for m in sent if m["type"] == "mic_status")
    assert "speaker_labels_reset" in (tmp_path / s.id / "audit.jsonl").read_text()


def test_old_connection_closing_late_does_not_detach_the_resumed_session(tmp_path):
    app = create_app(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template"}),
                     sessions_dir=tmp_path)
    client = TestClient(app)
    with client.websocket_connect("/ws") as old:
        sid = old.receive_json()["session_id"]
        with client.websocket_connect(f"/ws?resume={sid}") as new:
            assert new.receive_json()["session_id"] == sid
            old.close()  # the old socket ends after the browser has already resumed
            new.send_json({"type": "typed", "speaker": "parent", "text": "Still connected."})
            while (m := new.receive_json())["type"] != "utterance":
                pass
            assert m["utterance"]["text"] == "Still connected."
            session, closer = app.state.live[sid]
            assert closer is None  # not scheduled to close


# ---- turns holding two speakers are split on the word-level labels ----
def labelled(spec, start=0, step=300):
    """spec: [(label, "words ...")] -> (formatted text, words with speaker and timing)."""
    words, t = [], start
    for label, text in spec:
        for w in text.split():
            words.append({"text": w.strip(".,?").lower(), "start": t, "end": t + step - 50, "speaker": label})
            t += step
    return " ".join(text for _, text in spec), words


def test_split_by_speaker_cuts_at_the_change_and_keeps_formatting():
    from app.adapters.transcribers.assemblyai import split_by_speaker
    text, words = labelled([("B", "So those are the things that appeal to me."),
                            ("A", "Okay, awesome. That's good to know. Could you tell me about a problem?")])
    parts = split_by_speaker(text, words)
    assert [(l, t) for l, t, _ in parts] == [("B", "So those are the things that appeal to me."),
                                            ("A", "Okay, awesome. That's good to know. Could you tell me about a problem?")]
    assert parts[1][2][0]["start"] == 9 * 300


def test_split_by_speaker_ignores_flicker_pending_and_single_speakers():
    from app.adapters.transcribers.assemblyai import split_by_speaker
    text, words = labelled([("A", "I worked at the power company"), ("B", "for"), ("A", "two years on billing")])
    assert split_by_speaker(text, words) == []  # a one-word change is label noise
    text, words = labelled([("A", "I think that is all."), ("PENDING", "Yeah, absolutely."), ("B", "First of all, thank you.")])
    assert [(l, t) for l, t, _ in split_by_speaker(text, words)] == [  # PENDING at a change goes with the new voice
        ("A", "I think that is all."), ("B", "Yeah, absolutely. First of all, thank you.")]
    text, words = labelled([("PENDING", "Hi,"), ("A", "I'm the interviewer here."), ("B", "Thanks for having me"),
                            ("PENDING", "today.")])
    assert [(l, t) for l, t, _ in split_by_speaker(text, words)] == [
        ("A", "Hi, I'm the interviewer here."), ("B", "Thanks for having me today.")]
    text, words = labelled([("A", "One speaker only here.")])
    assert split_by_speaker(text, words) == []
    assert split_by_speaker("no labels here", [{"text": "no", "start": 0, "end": 1}]) == []
    text, words = labelled([("B", "Yes."), ("A", "Okay, so the next question is about teams.")])
    assert split_by_speaker(text, words) == []  # a short opening run joins the run after it: nothing left to split


def test_split_by_speaker_uses_word_text_when_the_formatted_text_does_not_line_up():
    from app.adapters.transcribers.assemblyai import split_by_speaker
    _, words = labelled([("A", "it was twenty twenty two"), ("B", "okay and what happened next")])
    parts = split_by_speaker("It was 2022. Okay, and what happened next?", words)
    assert [t for _, t, _ in parts] == ["it was twenty twenty two", "okay and what happened next"]


def test_a_final_turn_with_two_speakers_becomes_two_lines(tmp_path):
    text, words = labelled([("B", "So those are the things that appeal to me."),
                            ("A", "Okay, awesome. Could you tell me about a problem?")], start=100)
    mixed = {"type": "Turn", "turn_order": 0, "end_of_turn": True, "transcript": text, "speaker_label": "A",
             "speaker_confidence": 0.6, "words": words}
    for split, expect in [(True, [("candidate", "So those are the things that appeal to me."),
                                  ("interviewer", "Okay, awesome. Could you tell me about a problem?")]),
                          (False, [("interviewer", text)])]:
        emitted = []
        t, *_ = make(tmp_path, [FakeAAI([mixed])], split_turns=split)
        t.speaker_for = lambda l: {"A": "interviewer", "B": "candidate"}.get(l, "unknown")

        async def go():
            await t.start("s1", emit=lambda u: _append(emitted, u))
            await asyncio.sleep(0.1)
            await t.stop()
        asyncio.run(go())
        assert [(u.speaker, u.text) for u in emitted] == expect
        if split:
            assert emitted[0].t_start_ms == 5100 and emitted[1].t_start_ms == 5000 + 100 + 9 * 300
            assert all(u.is_final for u in emitted)
    # voices not mapped yet: still split (both lines unknown until the user maps them)
    emitted = []
    t, *_ = make(tmp_path, [FakeAAI([mixed])])
    t.speaker_for = lambda l: "unknown"

    async def go_unmapped():
        await t.start("s1", emit=lambda u: _append(emitted, u))
        await asyncio.sleep(0.1)
        await t.stop()
    asyncio.run(go_unmapped())
    assert [u.speaker for u in emitted] == ["unknown", "unknown"] and len(emitted[0].text.split()) == 9
    # both labels mapped to one person (or the manual toggle): one line, as before
    emitted = []
    t, *_ = make(tmp_path, [FakeAAI([mixed])])
    t.speaker_for = lambda l: "interviewer"

    async def go_one():
        await t.start("s1", emit=lambda u: _append(emitted, u))
        await asyncio.sleep(0.1)
        await t.stop()
    asyncio.run(go_one())
    assert [u.text for u in emitted] == [text]


def test_split_lines_log_their_own_label_and_part(tmp_path):
    text, words = labelled([("B", "Those are the things that appeal to me."), ("A", "Okay, tell me about a problem.")])
    mixed = {"type": "Turn", "turn_order": 0, "end_of_turn": True, "transcript": text, "speaker_label": "A",
             "speaker_confidence": 0.6, "words": words}
    settings = load_settings(env_file=None, environ={"ASSEMBLYAI_API_KEY": "k", "GATE_MODEL": "fake:keyword",
                                                     "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"})

    async def go():
        sent = []

        async def send(m):
            sent.append(m)
        s = Session(settings, PACK, tmp_path, send)
        sock = FakeAAI([mixed])

        async def connect(url, headers):
            return sock
        mic, _ = s.build_mic()
        mic.connect = connect
        s.mic, s.label_source = mic, mic
        await s.set_speaker("A", "worker")
        await s.set_speaker("B", "parent")
        await mic.start(s.id, emit=s.on_utterance)
        await asyncio.sleep(0.1)
        await mic.stop()
        await s.close()
        return s
    s = asyncio.run(go())
    labels = [json.loads(l) for l in (tmp_path / s.id / "events.jsonl").read_text().splitlines()
              if json.loads(l)["kind"] == "stt_label"]
    assert [(e["label"], e["speaker"], e.get("part")) for e in labels] == [("B", "parent", "1/2"), ("A", "worker", "2/2")]
    assert [u.speaker for u in s.store.utterances] == ["parent", "worker"]
    assert labels[0]["words"][:2] == [["those", "B"], ["are", "B"]] and labels[1]["words"][0] == ["okay", "A"]


def test_relabel_only_touches_unknown_lines_from_this_voice_on_this_connection(tmp_path):
    s, sent = session(tmp_path)

    class Source:
        connections, last_label, last_part, last_words = 2, None, None, []
    s.label_source = Source()

    async def go():
        for i, (label, conn, speaker) in enumerate([("B", 1, "unknown"), ("B", 2, "unknown"), ("A", 2, "unknown"),
                                                    ("B", 2, "worker")], 1):
            u = s.store.append(Utterance(id="", session_id=s.id, t_start_ms=i, t_end_ms=i + 1, speaker=speaker,
                                         text=f"line {i}", is_final=True, source="mic"))
            s.line_labels[u.id] = (label, conn)
        await s.set_speaker("B", "parent")
    asyncio.run(go())
    assert [u.speaker for u in s.store.utterances] == ["unknown", "parent", "unknown", "worker"]
    assert [m["utterance"]["id"] for m in sent if m["type"] == "utterance_update"] == ["u0002"]


def test_user_corrects_one_lines_speaker(tmp_path):
    s, sent = session(tmp_path)

    async def go():
        u = s.store.append(Utterance(id="", session_id=s.id, t_start_ms=0, t_end_ms=1, speaker="worker",
                                     text="When he yells.", is_final=True, source="mic"))
        assert await s.set_line_speaker(u.id, "child") is None
        assert await s.set_line_speaker(u.id, "child") is None  # no change: nothing written
        assert await s.set_line_speaker(u.id, "nobody") and await s.set_line_speaker("u9999", "child")
    asyncio.run(go())
    from app.store import read_utterances
    assert s.store.utterances[0].speaker == "child" and read_utterances(tmp_path / s.id)[0]["speaker"] == "child"
    assert len([m for m in sent if m["type"] == "utterance_update"]) == 1
    audit = [json.loads(l) for l in (tmp_path / s.id / "audit.jsonl").read_text().splitlines()]
    assert [a["details"] for a in audit if a["action"] == "line_speaker_changed"] == \
        [{"utterance_id": "u0001", "old": "worker", "new": "child"}]
