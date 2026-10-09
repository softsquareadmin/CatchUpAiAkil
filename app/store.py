"""Append-only session files: utterances.jsonl (transcript) and events.jsonl (topic updates, flags, rejections)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.models import Utterance


class TranscriptStore:
    def __init__(self, session_dir: Path):
        self.path = Path(session_dir) / "utterances.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.utterances: list[Utterance] = []

    def append(self, u: Utterance) -> Utterance:
        """Persist a final utterance and assign its session-wide id (u0001, u0002, ...)."""
        if not u.is_final:
            raise ValueError("Only final utterances are stored")
        u = u.model_copy(update={"id": f"u{len(self.utterances) + 1:04d}"})
        with self.path.open("a", encoding="utf-8") as f:
            f.write(u.model_dump_json() + "\n")
        self.utterances.append(u)
        return u


def read_utterances(session_dir: Path) -> list[dict]:
    """The saved transcript with the user's speaker corrections applied. utterances.jsonl stays append-only; a
    relabelled line (a voice mapped after it was spoken) is a `speaker_corrected` row in events.jsonl."""
    d = Path(session_dir)
    rows = [json.loads(l) for l in (d / "utterances.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (d / "utterances.jsonl").exists() else []
    events = d / "events.jsonl"
    fixes = {}
    if events.exists():
        for l in events.read_text(encoding="utf-8").splitlines():
            e = json.loads(l) if l.strip() else {}
            if e.get("kind") == "speaker_corrected":
                fixes[e["utterance_id"]] = e["new"]
    return [{**u, "speaker": fixes.get(u["id"], u["speaker"])} for u in rows]


class EventLog:
    def __init__(self, session_dir: Path):
        self.path = Path(session_dir) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **data) -> None:
        row = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
