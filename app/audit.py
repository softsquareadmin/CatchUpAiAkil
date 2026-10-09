"""Audit log: who or what did what, when (SPEC 9). Append-only JSONL."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


class AuditLog:
    def __init__(self, session_dir: Path, session_id: str):
        self.path = Path(session_dir) / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id

    def write(self, actor: Literal["user", "system"], action: str, **details: Any) -> dict:
        row = {"ts": datetime.now(timezone.utc).isoformat(), "session_id": self.session_id,
               "actor": actor, "action": action, "details": details}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row
