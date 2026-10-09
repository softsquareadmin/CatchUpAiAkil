import asyncio
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import audio_file
from app.audio_file import AudioFileError, decode
from app.config import ROOT, load_settings
from app.main import create_app
from app.pack import load_pack
from app.pipeline import Session
from tests.test_m4_mic import FakeAAI, turn

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def make_audio(tmp_path, ext, seconds=2.0):
    out = tmp_path / f"tone{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-ac", "2", "-ar", "44100", str(out)], check=True)
    return out


# ---- decoding ----

@needs_ffmpeg
@pytest.mark.parametrize("ext", [".wav", ".mp3", ".m4a"])
def test_decodes_mp3_wav_m4a_to_16k_mono_pcm(tmp_path, ext):
    f = make_audio(tmp_path, ext)
    pcm = asyncio.run(decode(f.read_bytes(), f"interview{ext}"))
    assert abs(len(pcm) / 32000 - 2.0) < 0.15  # 16 kHz x 2 bytes, mono
    assert not list(tmp_path.glob("tmp*"))


@pytest.mark.parametrize("data,name,msg", [
    (b"abc", "notes.txt", "Use an mp3, wav or m4a file"),
    (b"", "a.wav", "The file is empty"),
])
def test_decode_rejects_bad_input(data, name, msg):
    with pytest.raises(AudioFileError, match=msg):
        asyncio.run(decode(data, name, ffmpeg="ffmpeg"))


@needs_ffmpeg
def test_decode_failure_has_a_clear_message():
    with pytest.raises(AudioFileError, match="Could not read the audio file"):
        asyncio.run(decode(b"this is not audio at all" * 100, "talk.mp3"))


def test_missing_ffmpeg_is_explained(monkeypatch):
    monkeypatch.setattr(audio_file, "ffmpeg_path", lambda: None)
    with pytest.raises(AudioFileError, match="ffmpeg is not installed"):
        asyncio.run(decode(b"x", "a.wav"))


# ---- streaming through the transcriber (fake AssemblyAI socket) ----

def session(tmp_path, sockets, **env):
    sent = []

    async def send(m):
        sent.append(m)
    s = Session(load_settings(env_file=None, environ={"ASSEMBLYAI_API_KEY": "k", **env}), PACK, tmp_path, send,
                decider=object(), analyst=object())
    s.decider = s.analyst = None
    s.file_pace = 0
    real = s.build_mic

    def build(source="mic"):
        mic, reason = real(source)

        async def connect(url, headers):
            sock = sockets.pop(0)
            if isinstance(sock, Exception):
                raise sock
            return sock
        mic.connect, mic.backoff_s, mic.max_retries = connect, (0.01,), 3
        return mic, reason
    s.build_mic = build
    return s, sent


async def until_done(s):
    while s.file_playing():
        await asyncio.sleep(0.01)


def test_file_gives_the_same_events_as_the_mic(tmp_path):
    sock = FakeAAI([turn("I am", False), turn("I am John.", True, label="A"), turn("Yes you may.", True, label="B")])
    s, sent = session(tmp_path, [sock])
    pcm = b"\x01\x00" * 16000 * 3  # 3 s

    async def go():
        assert await s.start_audio_file(pcm, "/home/x/visit.mp3") is None
        await until_done(s)
    asyncio.run(go())
    assert [(u.text, u.source) for u in s.store.utterances] == [("I am John.", "file"), ("Yes you may.", "file")]
    assert any(m["type"] == "utterance" and not m["utterance"]["is_final"] for m in sent)  # partial first
    assert s.labels_seen == ["A", "B"]
    assert sum(len(c) for c in sock.sent if isinstance(c, bytes)) == len(pcm)
    files = [m["audio_file"] for m in sent if m["type"] == "audio_file"]
    assert files[0]["state"] == "streaming" and files[-1] == {**files[-1], "state": "done", "done_s": 3.0, "total_s": 3.0}
    assert any(m["type"] == "mic_status" and m["mic"]["source"] == "file" for m in sent)
    rows = [json.loads(l) for l in (tmp_path / s.id / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["role"] == "stt" and rows[0]["audio_seconds"] > 0
    audit = (tmp_path / s.id / "audit.jsonl").read_text()
    assert '"audio_file_start"' in audit and '"audio_file_done"' in audit and '"name": "visit.mp3"' in audit
    assert '"stt_label"' in (tmp_path / s.id / "events.jsonl").read_text()
    assert not (tmp_path / s.id / "audio_file.pcm").exists()  # not stored by default
    assert s.mic is None


def test_dropped_connection_pauses_the_file_and_loses_no_lines(tmp_path):
    first = FakeAAI([turn("First line.", True), "DROP"])
    second = FakeAAI([turn("Second line.", True)])
    s, sent = session(tmp_path, [first, second])
    s.file_pace = 0.05  # 20x real time, so the drop happens mid-file
    pcm = b"\x02\x00" * 16000 * 4

    async def go():
        await s.start_audio_file(pcm, "a.wav")
        await until_done(s)
    asyncio.run(go())
    assert [u.text for u in s.store.utterances] == ["First line.", "Second line."]
    got = sum(len(c) for sock in (first, second) for c in sock.sent if isinstance(c, bytes))
    assert got >= len(pcm)  # everything reached AssemblyAI (the unfinished turn is sent again)
    assert [m["audio_file"]["state"] for m in sent if m["type"] == "audio_file"][-1] == "done"
    assert any(m["type"] == "mic_status" and m["mic"]["state"] == "reconnecting" for m in sent)


def test_lost_connection_fails_clearly_and_keeps_lines(tmp_path):
    first = FakeAAI([turn("Kept line.", True), "DROP"])
    s, sent = session(tmp_path, [first, OSError("down"), OSError("down"), OSError("down")])
    s.file_pace = 0.05

    async def go():
        await s.start_audio_file(b"\x00\x00" * 16000 * 30, "a.wav")
        await until_done(s)
    asyncio.run(go())
    last = [m["audio_file"] for m in sent if m["type"] == "audio_file"][-1]
    assert last["state"] == "failed" and "could not be restored" in last["detail"]
    assert [u.text for u in s.store.utterances] == ["Kept line."]
    assert '"audio_file_failed"' in (tmp_path / s.id / "audit.jsonl").read_text()


def test_user_can_stop_the_file_and_mic_is_blocked_meanwhile(tmp_path):
    s, sent = session(tmp_path, [FakeAAI([])])
    s.file_pace = 1.0  # real time, so it is still playing when we stop it

    async def go():
        await s.start_audio_file(b"\x00\x00" * 16000 * 60, "long.m4a")
        await asyncio.sleep(0.3)
        assert "Stop it first" in await s.start_mic()
        assert "already being transcribed" in await s.start_audio_file(b"\x00\x00", "b.wav")
        await s.stop_audio_file()
    asyncio.run(go())
    last = [m["audio_file"] for m in sent if m["type"] == "audio_file"][-1]
    assert last["state"] == "stopped" and last["done_s"] < 5
    assert '"audio_file_stopped"' in (tmp_path / s.id / "audit.jsonl").read_text() and s.mic is None


def test_store_audio_keeps_the_decoded_file_and_audits_it(tmp_path):
    s, _ = session(tmp_path, [FakeAAI([])], STORE_AUDIO="true")
    pcm = b"\x03\x00" * 1600

    async def go():
        await s.start_audio_file(pcm, "a.wav")
        await until_done(s)
    asyncio.run(go())
    assert (tmp_path / s.id / "audio_file.pcm").read_bytes() == pcm
    assert '"audio_stored"' in (tmp_path / s.id / "audit.jsonl").read_text()


def test_without_assemblyai_key_the_file_mode_explains(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    s = Session(load_settings(env_file=None, environ={}), PACK, tmp_path, send, decider=object(), analyst=object())
    assert "ASSEMBLYAI_API_KEY is not set" in asyncio.run(s.start_audio_file(b"\x00\x00", "a.wav"))


# ---- upload endpoint ----

@needs_ffmpeg
def test_upload_endpoint_decodes_and_hands_pcm_to_the_session(tmp_path):
    """Streaming itself is tested above: TestClient runs each HTTP request in its own short-lived event loop, so a
    task started by the request would not outlive it here (uvicorn has one loop)."""
    env = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "ASSEMBLYAI_API_KEY": "k"}
    app = create_app(load_settings(env_file=None, environ=env), sessions_dir=tmp_path)
    client = TestClient(app)
    assert client.get("/health").json()["ffmpeg"] is True
    wav = make_audio(tmp_path, ".wav", 1.0).read_bytes()
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        sid = hello["session_id"]
        assert hello["ffmpeg"] is True and hello["audio_file"] == {"state": "idle"}
        s = app.state.live[sid][0]
        got = []

        async def fake_start(pcm, name):
            got.append((len(pcm), name))
            return "busy" if len(got) > 1 else None
        s.start_audio_file = fake_start
        bad = client.post(f"/api/sessions/{sid}/audio?name=notes.txt", content=b"x")
        assert bad.status_code == 400 and "mp3, wav or m4a" in bad.json()["detail"]
        r = client.post(f"/api/sessions/{sid}/audio?name=clip.wav", content=wav)
        assert r.status_code == 200 and abs(r.json()["seconds"] - 1.0) < 0.1
        assert got[0][1] == "clip.wav" and abs(got[0][0] / 32000 - 1.0) < 0.1
        assert client.post(f"/api/sessions/{sid}/audio?name=clip.wav", content=wav).status_code == 409
    assert client.post("/api/sessions/20990101-000000-ffff/audio?name=a.wav", content=wav).status_code == 404
    assert '"audio_file_rejected"' in (tmp_path / sid / "audit.jsonl").read_text()
