"""Cost and latency report from the session ledgers: python -m app.report [--experiment X] [--csv out.csv]
[--sessions DIR]. Groups by experiment and role. The ledger is an estimate; provider invoices are the source of truth."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from app.config import ROOT


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, round(q * (len(v) - 1)))]


def build(sessions_dir: Path, experiment: str | None = None) -> tuple[list[dict], list[dict]]:
    """Return (rows per experiment and role, totals per experiment)."""
    calls: dict[tuple[str, str], list[dict]] = defaultdict(list)
    per_exp: dict[str, dict] = defaultdict(lambda: {"sessions": set(), "minutes": 0.0, "cues_shown": 0,
                                                    "evidence_failures": 0, "cost_usd": 0.0})
    for d in sorted(p for p in Path(sessions_dir).iterdir() if p.is_dir()):
        rows = _jsonl(d / "ledger.jsonl")
        if not rows:
            continue
        exps = {r["experiment"] for r in rows}
        if experiment and experiment not in exps:
            continue
        exp = rows[0]["experiment"]  # one experiment label per session
        for r in rows:
            calls[(r["experiment"], r["role"])].append(r)
        e = per_exp[exp]
        e["sessions"].add(d.name)
        e["cost_usd"] += sum(r["cost_usd"] for r in rows)
        utts = _jsonl(d / "utterances.jsonl")
        if utts:
            e["minutes"] += (max(u["t_end_ms"] for u in utts) - min(u["t_start_ms"] for u in utts)) / 60000
        events = _jsonl(d / "events.jsonl")
        e["cues_shown"] += sum(1 for ev in events if ev.get("kind") == "cue")
        e["evidence_failures"] += sum(1 for ev in events if "evidence_not_in_transcript" in str(ev.get("reason", ""))
                                      or ev.get("reason") == "no_valid_evidence")
    rows = []
    for (exp, role), rs in sorted(calls.items()):
        if experiment and exp != experiment:
            continue
        lat = [r["latency_ms"] for r in rs if r["ok"] and r["latency_ms"]]
        rows.append({"experiment": exp, "role": role, "models": ", ".join(sorted({f"{r['provider']}:{r['model']}" for r in rs})),
                     "calls": len(rs), "failed": sum(1 for r in rs if not r["ok"]),
                     "invalid_json_rate": round(sum(1 for r in rs if (r["error"] or "").startswith("invalid_json")) / len(rs), 3),
                     "cost_usd": round(sum(r["cost_usd"] for r in rs), 5),
                     "p50_ms": _pct(lat, .5), "p95_ms": _pct(lat, .95)})
    totals = [{"experiment": exp, "sessions": len(e["sessions"]), "interview_minutes": round(e["minutes"], 2),
               "cost_usd": round(e["cost_usd"], 5),
               "cost_per_minute_usd": round(e["cost_usd"] / e["minutes"], 5) if e["minutes"] else None,
               "cues_shown": e["cues_shown"], "evidence_failures": e["evidence_failures"]}
              for exp, e in sorted(per_exp.items()) if not experiment or exp == experiment]
    return rows, totals


def session_health(sessions_dir: Path, experiment: str | None = None) -> list[dict]:
    """Per session (M10a): calls, HTTP 429s, retries, total and longest wait, skipped gate runs and cues."""
    out = []
    for d in sorted(p for p in Path(sessions_dir).iterdir() if p.is_dir()):
        rows = [r for r in _jsonl(d / "ledger.jsonl") if r["provider"] != "fake"]
        if not rows or (experiment and rows[0]["experiment"] != experiment):
            continue
        events = _jsonl(d / "events.jsonl")
        waits = [e["wait_s"] for e in events if e.get("kind") == "provider_wait"]  # retry waits
        queue = [e["wait_s"] for e in events if e.get("kind") == "provider_queue"]  # waits for a request slot
        skipped = [e for e in events if e.get("kind") == "analysis_skipped"]
        out.append({"session": d.name, "experiment": rows[0]["experiment"], "calls": len(rows),
                    "http_429": sum(r.get("status") == 429 or "HTTP 429" in (r.get("error") or "") for r in rows),
                    "retries": sum(r.get("attempt", 1) > 1 for r in rows),
                    "total_wait_s": round(sum(waits) + sum(queue), 1), "longest_queue_wait_s": max(queue, default=0),
                    "longest_retry_wait_s": max(waits, default=0),
                    "gate_runs_skipped": sum(e.get("role") == "gate" for e in skipped),
                    "cues_skipped": sum(e.get("role") == "cue" for e in skipped)})
    return out


def table(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    keys = list(rows[0])
    lines = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    lines += ["| " + " | ".join("" if r[k] is None else str(r[k]) for k in keys) + " |" for r in rows]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experiment")
    ap.add_argument("--csv")
    ap.add_argument("--sessions", default=str(ROOT / "sessions"))
    ap.add_argument("--health", action="store_true", help="also list provider health per session (429s, waits, skips)")
    a = ap.parse_args(argv)
    rows, totals = build(Path(a.sessions), a.experiment)
    print("Per experiment and role\n" + table(rows) + "\n\nPer experiment\n" + table(totals))
    if a.health:
        print("\nProvider health per session\n" + table(session_health(Path(a.sessions), a.experiment)))
    if a.csv and rows:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {a.csv}")


if __name__ == "__main__":
    main()
