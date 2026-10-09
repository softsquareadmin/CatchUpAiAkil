"""AssemblyAI streaming smoke test (PAID, about $0.01; run only when asked): python -m tests.smoke_mic [lines]
Speaks the first fixture lines with three macOS voices (`say`, worker / parent / child), converts them to 16 kHz
mono PCM (ffmpeg), streams them in real time through a Session's mic path, drops the AssemblyAI connection
halfway to test reconnect, then reports partials, finals, speaker labels and the stt ledger rows.
Gate and cue writer are off, so only speech-to-text is billed."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import ROOT, load_settings
from app.evidence import normalise
from app.main import FIXTURE
from app.pack import load_pack
from app.pipeline import Session

VOICES = {"worker": "Samantha", "parent": "Moira", "child": "Junior"}
CHUNK = 3200  # 100 ms of 16 kHz PCM16


def synthesize(lines: list[dict], tmp: Path) -> bytes:
    pcm = b""
    silence = b"\x00\x00" * 16000  # 1 s between speakers
    for i, ln in enumerate(lines):
        aiff, raw = tmp / f"{i}.aiff", tmp / f"{i}.pcm"
        subprocess.run(["say", "-v", VOICES[ln["speaker"]], "-o", str(aiff), ln["text"]], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000",
                        "-f", "s16le", str(raw)], check=True)
        pcm += raw.read_bytes() + silence
    return pcm + silence * 2


async def main(n: int):
    lines = json.loads(FIXTURE.read_text())[:n]
    with tempfile.TemporaryDirectory() as d:
        pcm = synthesize(lines, Path(d))
    settings = load_settings().model_copy(update={"experiment_label": "m4-mic-smoke"})
    sent = []

    async def send(m):
        sent.append(m)

    s = Session(settings, load_pack(ROOT / settings.pack), ROOT / "sessions", send)
    s.decider = s.analyst = None  # speech-to-text only
    if err := await s.start_mic():
        raise SystemExit(err)
    dropped = False
    for i in range(0, len(pcm), CHUNK):
        await s.mic_audio(pcm[i:i + CHUNK])
        await asyncio.sleep(0.1)  # real time
        if not dropped and i > len(pcm) // 2 and s.mic.ws is not None:
            dropped = True
            print(f"-- dropping the AssemblyAI connection at {i / 32000:.1f} s of audio")
            await s.mic.ws.close()
    await asyncio.sleep(2)
    await s.stop_mic()
    await s.close()

    partials = [m for m in sent if m["type"] == "utterance" and not m["utterance"]["is_final"]]
    statuses = [m["mic"]["state"] for m in sent if m["type"] == "mic_status"]
    labels = {json.loads(l)["utterance_id"]: json.loads(l)["label"]
              for l in (s.dir / "events.jsonl").read_text().splitlines() if json.loads(l)["kind"] == "stt_label"}
    print(f"audio {len(pcm) / 32000:.1f} s, partials shown {len(partials)}, mic states {statuses}")
    print("finals (label, text):")
    for u in s.store.utterances:
        print(f"  {u.id} {u.t_start_ms / 1000:6.1f}s label={labels.get(u.id)} {u.text}")
    print("script (speaker, text):")
    for ln in lines:
        print(f"  {ln['speaker']:6} {ln['text']}")
    said = normalise(" ".join(ln["text"] for ln in lines)).split()
    heard = normalise(" ".join(u.text for u in s.store.utterances)).split()
    print(f"words: script {len(said)}, transcript {len(heard)}, script words found {sum(w in set(heard) for w in said)}")
    for r in map(json.loads, (s.dir / "ledger.jsonl").read_text().splitlines()):
        print(f"ledger: {r['role']} {r['model']} seconds={r['audio_seconds']} cost=${r['cost_usd']:.5f} ok={r['ok']} {r['error'] or ''}")
    print("session:", s.id)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 10))
