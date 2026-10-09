#!/usr/bin/env python3
"""Turn a script fixture (JSON list of lines) into one test audio file plus a matching timing file.

For each line: synthesize with the speaker's voice (OpenAI text-to-speech), trim leading/trailing silence,
measure the real length, then place the clips one after another with a gap. The timing JSON has the measured
t_ms / t_end_ms, so Replay of the same script matches the audio.

Needs: Python 3.9+, ffmpeg on PATH, and OPENAI_API_KEY in the environment (not needed with --fake or --dry-run).
No third-party Python packages.

Examples:
  python make_test_audio.py --script tests/fixtures/script_v2.json --out tests/fixtures/audio --name cps_script_v2 --dry-run
  python make_test_audio.py --script tests/fixtures/script_v2.json --out tests/fixtures/audio --name cps_script_v2
  python make_test_audio.py --out tests/fixtures/audio --script x --name x --audition   # hear every voice first
  python make_test_audio.py --script tests/fixtures/script_v2.json --out /tmp/try --name try --fake   # no API, tones only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

API_URL = "https://api.openai.com/v1/audio/speech"
SAMPLE_RATE = 16000  # what the notetaker's audio mode wants: 16 kHz, mono, 16-bit

# Default voices per role. Change with --voices worker=marin,parent=nova,child=coral. Listen and adjust:
# Use --audition to hear every voice first. The three voices must sound clearly different, or speaker labels will merge them.
DEFAULT_VOICES = {"worker": "marin", "parent": "nova", "child": "coral"}
# Pitch factor per role (1.0 = unchanged). OpenAI has no child voices, so the child is a higher voice raised a little.
DEFAULT_PITCH = {"child": 1.25}
ALL_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"]

PACE = "Speak at a natural conversational pace, not slowly. Do not add words."
DEFAULT_STYLE = {
    "worker": "Calm, professional and warm. " + PACE,
    "parent": "Guarded and tense, sometimes upset, but still speaking clearly. " + PACE,
    "child": "Soft, quiet and a little hesitant. " + PACE,
}
GENERIC_STYLE = "Natural, conversational. " + PACE


def parse_voices(text: str | None) -> dict[str, str]:
    voices = dict(DEFAULT_VOICES)
    if text:
        for pair in text.split(","):
            role, _, voice = pair.partition("=")
            if not role.strip() or not voice.strip():
                sys.exit(f"Bad --voices entry: {pair!r}. Use role=voice, comma separated.")
            voices[role.strip()] = voice.strip()
    return voices


def parse_pitch(text: str | None) -> dict[str, float]:
    pitch = dict(DEFAULT_PITCH)
    if text:
        for pair in text.split(","):
            role, _, val = pair.partition("=")
            try:
                pitch[role.strip()] = float(val)
            except ValueError:
                sys.exit(f"Bad --pitch entry: {pair!r}. Use role=factor, e.g. child=1.25")
    for role, f in pitch.items():
        if not 0.7 <= f <= 1.6:
            sys.exit(f"Pitch factor for {role} must be between 0.7 and 1.6 (got {f}).")
    return pitch


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Command failed: {' '.join(cmd)}\n{r.stderr[-800:]}")


def synthesize(text: str, voice: str, model: str, instructions: str, api_key: str, out_wav: Path) -> None:
    body = {"model": model, "input": text, "voice": voice, "response_format": "wav"}
    if instructions and model.startswith("gpt-4o"):  # the page I checked shows `instructions` only with gpt-4o-mini-tts
        body["instructions"] = instructions
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    delay = 2.0
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out_wav.write_bytes(r.read())
            return
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                wait = float(e.headers.get("Retry-After") or delay)
                print(f"  HTTP {e.code}, waiting {wait:.0f}s (attempt {attempt}/4)")
                time.sleep(wait)
                delay *= 2
                continue
            sys.exit(f"OpenAI TTS error {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            sys.exit(f"Network error: {e}")


def fake_clip(text: str, out_wav: Path, role_index: int) -> None:
    """A tone whose length follows the word count, with a different pitch per role. For testing the stitching only."""
    seconds = max(0.6, len(text.split()) / 2.8)
    freq = 220 + 110 * role_index
    n = int(seconds * SAMPLE_RATE)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))) for i in range(n)))


def normalise_and_trim(src: Path, dst: Path, pitch: float = 1.0) -> None:
    """16 kHz mono 16-bit, trim silence at both ends (leave about 50 ms so words are not clipped)."""
    trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    chain = f"{trim},areverse,{trim},areverse"
    if abs(pitch - 1.0) > 1e-6:  # raise pitch by resampling, then restore the speed (also lifts the voice, which suits a child)
        chain = f"aresample={SAMPLE_RATE},asetrate={int(SAMPLE_RATE * pitch)},aresample={SAMPLE_RATE},atempo={1 / pitch:.5f}," + chain
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-af", chain, "-c:a", "pcm_s16le", str(dst)])


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, SAMPLE_RATE, 2), "unexpected clip format"
        return w.readframes(w.getnframes())


def ms_to_bytes(ms: int) -> int:
    return int(round(ms * SAMPLE_RATE / 1000)) * 2


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", required=True, help="script JSON: list of {line, speaker, text, ...}")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--name", required=True, help="base name for the outputs, e.g. cps_script_v2")
    ap.add_argument("--pitch", help="role=factor pitch changes, e.g. child=1.25 (default child=1.25; 1.0 = none)")
    ap.add_argument("--audition", action="store_true",
                    help="speak one short sample in every voice into <out>/audition/ so you can pick voices (a few cents), then exit")
    ap.add_argument("--model", default="gpt-4o-mini-tts", help="default gpt-4o-mini-tts (check OpenAI docs for current)")
    ap.add_argument("--voices", help="role=voice pairs, e.g. worker=marin,parent=cedar,child=coral")
    ap.add_argument("--gap-ms", type=int, default=500, help="pause between different speakers (default 500)")
    ap.add_argument("--same-speaker-gap-ms", type=int, default=300, help="pause when the same speaker continues (default 300)")
    ap.add_argument("--lead-in-ms", type=int, default=1000, help="silence before the first line (default 1000)")
    ap.add_argument("--tail-ms", type=int, default=1000, help="silence after the last line (default 1000)")
    ap.add_argument("--force", action="store_true", help="re-synthesize even if a cached clip exists")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and character count; no API call, no files")
    ap.add_argument("--fake", action="store_true", help="no API: tones instead of speech, to test the stitching")
    args = ap.parse_args()

    lines = json.loads(Path(args.script).read_text())
    if not isinstance(lines, list) or not lines:
        sys.exit("The script file must be a non-empty JSON list.")
    voices = parse_voices(args.voices)
    pitch = parse_pitch(args.pitch)
    roles = sorted({ln["speaker"] for ln in lines})
    missing = [r for r in roles if r not in voices]
    if missing:
        sys.exit(f"No voice for role(s) {missing}. Add them with --voices {missing[0]}=<voice>.")

    chars = sum(len(ln["text"]) for ln in lines)
    print(f"{len(lines)} lines, {sum(len(ln['text'].split()) for ln in lines)} words, {chars} characters, "
          f"roles: {', '.join(f'{r}={voices[r]}' for r in roles)}, model: {args.model}")
    if args.audition:
        if not shutil.which("ffmpeg"):
            sys.exit("ffmpeg not found. On a Mac: brew install ffmpeg")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            sys.exit("Set OPENAI_API_KEY first (export OPENAI_API_KEY=...).")
        adir = Path(args.out) / "audition"
        adir.mkdir(parents=True, exist_ok=True)
        sample = "Hello, my name is Sam. Can you tell me what happened at home last night?"
        with tempfile.TemporaryDirectory() as tmp:
            for v in ALL_VOICES:
                raw = Path(tmp) / f"{v}.wav"
                print(f"  {v}")
                synthesize(sample, v, args.model, GENERIC_STYLE, api_key, raw)
                normalise_and_trim(raw, adir / f"{v}.wav")
                if v == voices.get("child") and pitch.get("child", 1.0) != 1.0:
                    normalise_and_trim(raw, adir / f"{v}_pitch_{pitch['child']}.wav", pitch["child"])
        print(f"\nSamples are in {adir}. Listen, pick, then use --voices and --pitch.")
        return
    if args.dry_run:
        print("Dry run: nothing written, nothing sent. Check the current price for the model, then run without --dry-run.")
        return
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found. On a Mac: brew install ffmpeg")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.fake:
        sys.exit("Set OPENAI_API_KEY first (export OPENAI_API_KEY=...), or use --fake to test without the API.")

    out = Path(args.out)
    cache = out / f"{args.name}_clips"
    cache.mkdir(parents=True, exist_ok=True)

    clips: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        for ln in lines:
            role, text = ln["speaker"], ln["text"].strip()
            style = ln.get("tts_instructions") or DEFAULT_STYLE.get(role, GENERIC_STYLE)
            key = hashlib.sha1(f"{args.model}|{voices[role]}|{style}|{pitch.get(role, 1.0)}|{text}|{args.fake}".encode()).hexdigest()[:10]
            trimmed = cache / f"{int(ln.get('line', len(clips) + 1)):02d}_{role}_{key}.wav"
            if args.force or not trimmed.exists():
                raw = Path(tmp) / "raw.wav"
                label = f"line {ln.get('line', len(clips) + 1)} ({role})"
                print(f"  synthesizing {label}: {text[:60]}{'...' if len(text) > 60 else ''}")
                if args.fake:
                    fake_clip(text, raw, roles.index(role))
                else:
                    synthesize(text, voices[role], args.model, style, api_key, raw)
                normalise_and_trim(raw, trimmed, pitch.get(role, 1.0))
            else:
                print(f"  cached line {ln.get('line', len(clips) + 1)} ({role})")
            clips.append(read_pcm(trimmed))

    # Place the clips on a timeline using their real lengths.
    pcm = bytearray(b"\x00" * ms_to_bytes(args.lead_in_ms))
    timing, prev_speaker = [], None
    for i, (ln, clip) in enumerate(zip(lines, clips)):
        if i > 0:
            gap = ln.get("pause_before_ms")
            if gap is None:
                gap = args.same_speaker_gap_ms if ln["speaker"] == prev_speaker else args.gap_ms
            pcm += b"\x00" * ms_to_bytes(int(gap))
        start_ms = round(len(pcm) / 2 / SAMPLE_RATE * 1000)
        pcm += clip
        end_ms = round(len(pcm) / 2 / SAMPLE_RATE * 1000)
        row = dict(ln)
        row["t_ms"], row["t_end_ms"] = start_ms, end_ms
        timing.append(row)
        prev_speaker = ln["speaker"]
    pcm += b"\x00" * ms_to_bytes(args.tail_ms)

    wav_path, mp3_path, json_path = out / f"{args.name}.wav", out / f"{args.name}.mp3", out / f"{args.name}_timing.json"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(pcm))
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-ac", "1", "-b:a", "96k", str(mp3_path)])
    json_path.write_text(json.dumps(timing, indent=2, ensure_ascii=False) + "\n")

    total_s = len(pcm) / 2 / SAMPLE_RATE
    print(f"\nDone. {total_s:.1f} s of audio.\n  {wav_path}\n  {mp3_path}\n  {json_path}")
    print("Listen to it before using it. If two voices sound alike, change --voices and re-run (cached clips are reused).")


if __name__ == "__main__":
    main()