"""Audio file mode smoke test (PAID, about $0.01; run only when asked): python -m tests.smoke_audio_file [lines]
Speaks the first fixture lines with three macOS voices, saves them as an mp3, decodes it with the app's decoder (as an
upload would), streams it at 1x through Session.start_audio_file, drops the AssemblyAI connection halfway, and reports
partials, finals, speaker labels and the stt ledger rows. Gate and cue writer are off: only speech-to-text is billed."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from app.audio_file import BYTES_PER_SECOND, decode
from app.config import ROOT, load_settings
from app.evidence import normalise
from app.main import FIXTURE
from app.pack import load_pack
from app.pipeline import Session
from tests.smoke_mic import synthesize


async def main(n: int):
    lines = json.loads(FIXTURE.read_text())[:n]
    with tempfile.TemporaryDirectory() as d:
        pcm_in = synthesize(lines, Path(d))
        raw, mp3 = Path(d) / "in.pcm", Path(d) / "interview.mp3"
        raw.write_bytes(pcm_in)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", str(raw), str(mp3)],
                       check=True)
        pcm = await decode(mp3.read_bytes(), mp3.name)
    settings = load_settings().model_copy(update={"experiment_label": "m8-audio-file-smoke"})
    sent = []

    async def send(m):
        sent.append(m)

    s = Session(settings, load_pack(ROOT / settings.pack), ROOT / "sessions", send)
    s.decider = s.analyst = None
    if err := await s.start_audio_file(pcm, mp3.name):
        raise SystemExit(err)
    total, dropped = len(pcm) / BYTES_PER_SECOND, False
    while s.file_playing():
        await asyncio.sleep(0.25)
        done = s.file_info.get("done_s", 0)
        if not dropped and done > total / 2 and s.mic is not None and s.mic.ws is not None:
            dropped = True
            print(f"-- dropping the AssemblyAI connection at {done:.1f} s of {total:.1f} s")
            await s.mic.ws.close()
    await s.close()

    partials = [m for m in sent if m["type"] == "utterance" and not m["utterance"]["is_final"]]
    states = [m["audio_file"]["state"] for m in sent if m["type"] == "audio_file"]
    labels = {json.loads(l)["utterance_id"]: json.loads(l)["label"]
              for l in (s.dir / "events.jsonl").read_text().splitlines() if json.loads(l)["kind"] == "stt_label"}
    print(f"file {total:.1f} s, partials {len(partials)}, file states {states[0]} -> {states[-1]}, "
          f"mic states {[m['mic']['state'] for m in sent if m['type'] == 'mic_status']}")
    for u in s.store.utterances:
        print(f"  {u.id} {u.t_start_ms / 1000:6.1f}s label={labels.get(u.id)} source={u.source} {u.text}")
    said = normalise(" ".join(ln["text"] for ln in lines)).split()
    heard = set(normalise(" ".join(u.text for u in s.store.utterances)).split())
    print(f"words: script {len(said)}, found {sum(w in heard for w in said)}")
    for r in map(json.loads, (s.dir / "ledger.jsonl").read_text().splitlines()):
        print(f"ledger: {r['role']} {r['model']} seconds={r['audio_seconds']} cost=${r['cost_usd']:.5f} ok={r['ok']} {r['error'] or ''}")
    print("session:", s.id)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8))
