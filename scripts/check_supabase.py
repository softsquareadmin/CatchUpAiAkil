"""Check the Supabase table the deployed app syncs to (ia_files.sql). Reads SUPABASE_URL and SUPABASE_SECRET_KEY
from .env or the environment; does not need SYNC_ENABLED.

    python -m scripts.check_supabase

Checks: the table answers; a row can be inserted, then replaced (the app's write-through needs both); reading it back
gives the new content; the prefix pull used at startup finds it; and how many synced files the table holds. The check
row is `_check/ping.txt`: it stays in the table (the key may not delete rows, on purpose), is overwritten on every
run, and the app never pulls it (only sessions/, audit_logs/ and packs/ are pulled)."""
import sys
from datetime import datetime, timezone

from app.remote_store import RemoteError, client_from_env, read_env, sync_requested

CHECK_PATH = "_check/ping.txt"


def main() -> int:
    env = read_env()
    try:
        client = client_from_env(env)
    except RemoteError as e:
        print(f"FAIL  configuration: {e}")
        return 1
    print(f"Project: {client.host}   (SYNC_ENABLED here: {'yes' if sync_requested(env) else 'no'})")
    steps = []
    try:
        client.request("GET", {"select": "path", "limit": 0})
        steps.append("table ia_files answers")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        client.push([{"path": CHECK_PATH, "content": f"first {stamp}"}])
        client.push([{"path": CHECK_PATH, "content": f"second {stamp}"}])
        steps.append("insert and replace (upsert) work")
        got = client.get(CHECK_PATH)
        if got != f"second {stamp}":
            raise RemoteError(f"read back {got!r}, expected the second write")
        steps.append("read back gives the replaced content")
        if client.pull("_check/").get(CHECK_PATH) != f"second {stamp}":  # the prefix filter the startup pull uses
            raise RemoteError("the prefix filter (path=like.<prefix>*) did not return the check row")
        steps.append("prefix pull (as at startup) finds the check row")
        counts = {p: len(client.pull(p)) for p in ("sessions/", "audit_logs/", "packs/")}
        steps.append("synced files: " + ", ".join(f"{p} {n}" for p, n in counts.items()))
    except RemoteError as e:
        for s in steps:
            print(f"ok    {s}")
        print(f"FAIL  {e}")
        return 1
    for s in steps:
        print(f"ok    {s}")
    print(f"Supabase is ready. The check row {CHECK_PATH} stays in the table (overwritten each run, never pulled).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
