"""Replay transcriber: plays scripted lines as final utterances at their scheduled end-of-turn times.
Also parses pasted transcripts ("Speaker: text" per line) into the same line format."""
import asyncio
import json
import re
from pathlib import Path

from app.adapters.transcribers import Emit
from app.models import Role, Utterance

LABEL_RE = re.compile(r"^\s*([^:]{1,40}):\s*(.*)$")


def load_script(path: str | Path, role_ids: list[str]) -> list[dict]:
    """A script's speakers must be the pack's role ids (or "unknown")."""
    allowed = [*role_ids, "unknown"]
    lines = json.loads(Path(path).read_text(encoding="utf-8"))
    for i, ln in enumerate(lines):
        if ln.get("speaker") not in allowed or not ln.get("text") or "t_ms" not in ln:
            raise ValueError(f"Script line {i + 1} needs speaker (one of {', '.join(allowed)}), text and t_ms")
    return lines


def parse_paste(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ([(label, text)], distinct labels in order). Unlabelled lines continue the previous line."""
    turns: list[tuple[str, str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = LABEL_RE.match(raw)
        if m and m.group(2).strip():
            turns.append((m.group(1).strip(), m.group(2).strip()))
        elif turns:
            label, prev = turns[-1]
            turns[-1] = (label, f"{prev} {raw.strip()}")
        else:
            turns.append(("Unknown", raw.strip()))
    labels = list(dict.fromkeys(label for label, _ in turns))
    return turns, labels


def suggest_mapping(labels: list[str], roles: list[Role]) -> dict[str, str]:
    """Match pasted speaker labels to roles by the role's id, label or aliases (first match in role order)."""
    out = {}
    for label in labels:
        low = label.lower()
        out[label] = next((r.id for r in roles if any(h.lower() in low for h in [r.id, r.label, *r.aliases] if h)),
                          "unknown")
    return out


def paste_to_script(turns: list[tuple[str, str]], mapping: dict[str, str], role_ids: list[str]) -> list[dict]:
    """Give pasted turns realistic timing: about 0.4 s per word (min 1 s) plus a 1.2 s gap."""
    lines, t = [], 1000
    for label, text in turns:
        speaker = mapping.get(label, "unknown")
        dur = max(1000, 400 * len(text.split()))
        lines.append({"speaker": speaker if speaker in role_ids else "unknown", "text": text,
                      "t_ms": t, "t_end_ms": t + dur})
        t += dur + 1200
    return lines


class ReplayTranscriber:
    """speed: 1.0, 2.0, ... or 0 for instant. `sleep` is injectable for tests."""

    def __init__(self, lines: list[dict], speed: float, source: str = "replay",
                 offset_ms: int = 0, sleep=asyncio.sleep):
        self.lines, self.speed, self.source, self.offset_ms, self.sleep = lines, speed, source, offset_ms, sleep
        self._stopped = False

    async def start(self, session_id: str, emit: Emit) -> None:
        clock = 0
        for ln in self.lines:
            if self._stopped:
                return
            t_end = ln.get("t_end_ms", ln["t_ms"] + 1000)
            if self.speed > 0 and t_end > clock:
                await self.sleep((t_end - clock) / 1000 / self.speed)
                clock = t_end
            if self._stopped:
                return
            await emit(Utterance(id="", session_id=session_id, t_start_ms=self.offset_ms + ln["t_ms"],
                                 t_end_ms=self.offset_ms + t_end, speaker=ln["speaker"], text=ln["text"],
                                 is_final=True, source=self.source))

    async def send_audio(self, chunk: bytes) -> None:
        pass

    async def stop(self) -> None:
        self._stopped = True
