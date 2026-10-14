"""Audit log: who or what did what, when (SPEC 9). Append-only JSONL."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


class AuditLog:
    """`held=True`: rows wait in memory until `open()` (a live session's files appear only once its interview begins,
    so opening the page and leaving creates nothing)."""

    def __init__(self, session_dir: Path, session_id: str, held: bool = False):
        self.path = Path(session_dir) / "audit.jsonl"
        self.session_id = session_id
        self.waiting: list[dict] | None = [] if held else None

    def open(self) -> None:
        """Write the held rows and write straight to the file from now on."""
        rows, self.waiting = self.waiting or [], None
        for row in rows:
            self._append(row)

    def write(self, actor: Literal["user", "system"], action: str, **details: Any) -> dict:
        row = {"ts": datetime.now(timezone.utc).isoformat(), "session_id": self.session_id,
               "actor": actor, "action": action, "details": details}
        if self.waiting is not None:
            self.waiting.append(row)
        else:
            self._append(row)
        return row

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
