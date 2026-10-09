"""Real-model gate smoke test (paid). Run only when asked: python -m tests.smoke_gate
Feeds the fixture one final utterance at a time (one gate call each) and checks: every call returned
valid JSON, every shown evidence quote validates, no status is null. Prints latency and cost."""
import asyncio
import json
import statistics

from app.config import ROOT, load_settings
from app.evidence import validate
from app.main import FIXTURE
from app.models import Evidence, Utterance
from app.pack import load_pack
from app.pipeline import Session


async def main():
    settings = load_settings().model_copy(update={"experiment_label": "m2-smoke"})
    pack = load_pack(ROOT / settings.pack)
    shown = []

    async def send(m):
        if m["type"] == "topic_update":
            shown.append(m["state"])

    s = Session(settings, pack, ROOT / "sessions", send)
    if s.decider is None:
        raise SystemExit(f"gate is off: {s.gate_off_reason}")
    for i, ln in enumerate(json.loads(FIXTURE.read_text())):
        await s.on_utterance(Utterance(id="", session_id=s.id, t_start_ms=ln["t_ms"], t_end_ms=ln["t_end_ms"],
                                       speaker=ln["speaker"], text=ln["text"], is_final=True, source="replay"))
        await s.gate_idle()
    await s.close()

    rows = [json.loads(l) for l in (s.dir / "ledger.jsonl").read_text().splitlines()]
    transcript = {u.id: u for u in s.store.utterances}
    bad_ev = [st for st in shown if not all(validate(Evidence(**e), transcript) for e in st["evidence"])]
    rejected = [json.loads(l) for l in (s.dir / "events.jsonl").read_text().splitlines()
                if json.loads(l)["kind"] == "rejected"]
    lat = sorted(r["latency_ms"] for r in rows if r["ok"])
    p95 = lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] if lat else None
    print(json.dumps({
        "session": s.id, "model": settings.models["gate"],
        "calls": len(rows), "ok_calls": sum(r["ok"] for r in rows),
        "invalid_json": sum(1 for r in rows if (r["error"] or "").startswith("invalid_json")),
        "provider_errors": sum(1 for r in rows if not r["ok"] and not (r["error"] or "").startswith("invalid_json")),
        "updates_shown": len(shown), "shown_with_invalid_evidence": len(bad_ev),
        "null_statuses": sum(1 for v in s.checklist.values() if v.status is None),
        "rejected_by_validator": len(rejected),
        "latency_ms_p50": statistics.median(lat) if lat else None, "latency_ms_p95": p95,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 5),
        "cost_sources": sorted({r["cost_source"] for r in rows}),
    }, indent=1))
    print("final statuses:")
    for k, v in s.checklist.items():
        print(f"  {k:24} {v.status:16} {v.evidence[-1].quote if v.evidence else ''}")
    for r in rejected:
        print("rejected:", json.dumps(r)[:200])


if __name__ == "__main__":
    asyncio.run(main())
