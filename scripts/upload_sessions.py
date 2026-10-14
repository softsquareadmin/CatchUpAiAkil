"""Copy chosen local sessions to the deployment's Supabase table, so the deployed app shows them after its next
restart (it pulls the table at startup). Reads SUPABASE_URL and SUPABASE_SECRET_KEY from .env; does not need
SYNC_ENABLED. Shows what it would upload unless --yes is given.

    python -m scripts.upload_sessions --from 20261013 --to 20261014
    python -m scripts.upload_sessions --from 20261005 --to 20261012 --pack job_interview --include-recordings --yes
    python -m scripts.upload_sessions 20261014-103017-1e34 --yes

Only sessions shown on the Sessions page (with an audit log) and with at least one transcript line. Sessions that
used the Mic or an Audio file can hold recordings of real people: they are skipped unless --include-recordings.
Everyone with the deployment's passcode can read what is uploaded, and rows cannot be deleted with this key.
Each uploaded session gets an `imported.json` marker, so its past cost does not count toward MAX_TOTAL_COST_USD."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import ROOT
from app.ledger import IMPORTED_MARKER
from app.remote_store import MAX_FILE_BYTES, SKIP_NAMES, RemoteError, client_from_env, read_env

SESSION_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


def describe(d: Path) -> dict | None:
    audit = [json.loads(l) for l in (d / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (d / "audit.jsonl").exists() else []
    start = next((a for a in audit if a["action"] == "session_start"), None)
    if start is None:
        return None
    actions = {a["action"] for a in audit}
    files = sorted({a["details"].get("name", "") for a in audit if a["action"] == "audio_file_start"})
    source = "mic" if "mic_start" in actions else f"audio file {', '.join(files)}" if files else "replay/typed/paste"
    lines = len((d / "utterances.jsonl").read_text(encoding="utf-8").splitlines()) if (d / "utterances.jsonl").exists() else 0
    return {"id": d.name, "pack": start["details"].get("pack") or "", "source": source, "lines": lines,
            "recording": source != "replay/typed/paste"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="session ids (default: all that match the filters)")
    ap.add_argument("--from", dest="start", help="first day, YYYYMMDD")
    ap.add_argument("--to", dest="end", help="last day, YYYYMMDD")
    ap.add_argument("--pack", default="", help="only packs whose id contains this")
    ap.add_argument("--include-recordings", action="store_true", help="also Mic and Audio file sessions")
    ap.add_argument("--sessions-dir", default=str(ROOT / "sessions"))
    ap.add_argument("--yes", action="store_true", help="upload (otherwise only list)")
    args = ap.parse_args()

    chosen, skipped = [], []
    for d in sorted(Path(args.sessions_dir).iterdir()):
        if not d.is_dir() or not SESSION_ID.match(d.name) or (args.ids and d.name not in args.ids):
            continue
        if (args.start and d.name[:8] < args.start) or (args.end and d.name[:8] > args.end):
            continue
        info = describe(d)
        if info is None or info["lines"] == 0 or args.pack not in info["pack"]:
            continue
        (chosen if args.include_recordings or not info["recording"] else skipped).append(info)
    for s in chosen:
        print(f"  {s['id']}  {s['pack']:<26} {s['lines']:>4} lines  {s['source']}")
    print(f"{len(chosen)} sessions to upload" + (f"; {len(skipped)} Mic/Audio file sessions skipped "
                                                 "(--include-recordings)" if skipped else ""))
    if not chosen or not args.yes:
        if chosen:
            print("Nothing uploaded. Add --yes to upload these.")
        return 0

    try:
        client = client_from_env(read_env())
        when = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for s in chosen:
            d = Path(args.sessions_dir) / s["id"]
            rows = [{"path": f"sessions/{s['id']}/{p.name}", "content": p.read_text(encoding="utf-8")}
                    for p in sorted(d.iterdir())
                    if p.is_file() and p.name not in SKIP_NAMES and p.name != IMPORTED_MARKER
                    and not p.name.startswith(".") and p.stat().st_size <= MAX_FILE_BYTES]
            rows.append({"path": f"sessions/{s['id']}/{IMPORTED_MARKER}",
                         "content": json.dumps({"imported_at": when, "from": "local", "source": s["source"]})})
            client.push(rows)
            print(f"ok  {s['id']}  ({len(rows)} files)")
    except (RemoteError, UnicodeDecodeError) as e:
        print(f"FAIL  {e}")
        return 1
    print(f"Uploaded {len(chosen)} sessions to {client.host}. Restart the Render service to load them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
