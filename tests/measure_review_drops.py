"""How often the review drops parts (PAID; run only when asked):
python -m tests.measure_review_drops <session_id> <pack file> <runs>
Re-runs only the review, N times, on one saved session (same transcript, same final checklist state), so the spread
comes from the review model alone. Reports per run: parts dropped and why, sections that came back empty, cost and
latency; then the drop rate. Each run gets its own ledger under sessions/<id>/review_runs/."""
import asyncio
import json
import statistics
import sys
from collections import Counter

from app.adapters.analysts import build_analyst
from app.config import ROOT, load_settings
from app.ledger import Ledger
from app.models import TopicState, Utterance
from app.pack import load_pack
from app.review import build_items, build_topic_results, lost_sections, retry_note, section_titles
from app.store import read_utterances


def load(session_id: str):
    d = ROOT / "sessions" / session_id
    utts = [Utterance(**u) for u in read_utterances(d)]
    state = {}
    for line in (d / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["kind"] == "topic_update":
            state[e["state"]["item_id"]] = TopicState(**e["state"])
    return d, utts, state


async def one(n: int, d, utts, state, pack, settings) -> dict:
    ledger = Ledger(d / "review_runs" / f"run{n}", f"{d.name}-r{n}", settings.experiment_label,
                    settings.max_session_cost_usd, pack=pack.ref)
    reviewer, reason = build_analyst(settings, pack, ledger, role="review")
    if reviewer is None:
        return {"run": n, "error": reason}
    by_id = {u.id: u for u in utts}
    retried = {}
    try:
        raw = await reviewer.review(utts, dict(state), {})
        if raw:  # the same one retry as Session.run_review
            topics, rej = build_topic_results(raw, pack, by_id, state)
            st, tq = {t["topic_id"]: t["status"] for t in topics}, {t["topic_id"]: t["evidence"] for t in topics}
            items, rej2 = build_items(raw, pack, by_id, st, tq)
            if lost := lost_sections(pack, raw, items, rej + rej2):
                raw2 = await reviewer.review(utts, dict(state), {}, only=list(lost), note=retry_note(lost))
                again = build_items(raw2, pack, by_id, st, tq)[0] if raw2 else []
                retried = {sec: "recovered" if any(i.section == sec for i in again) else "empty" for sec in lost}
                if raw2:  # replace the lost sections in the raw draft with the retry's
                    for k, v in raw2.items():
                        raw[k] = [e for e in raw.get(k) or [] if not (k == "custom" and f"custom.{e.get('section')}" in lost)] + v \
                            if k == "custom" else v
    finally:
        await reviewer.client.close()
    rows = ledger.rows
    out = {"run": n, "cost": round(sum(r.cost_usd or 0 for r in rows), 4),
           "latency_s": round(max((r.latency_ms for r in rows if r.ok), default=0) / 1000, 1),
           "calls": len(rows), "retried": retried, "error": None if raw else (reviewer.last_error or "no draft")}
    if not raw:
        return out
    (d / "review_runs" / f"run{n}" / "review_raw.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    topics, rej = build_topic_results(raw, pack, by_id, state)
    items, rej2 = build_items(raw, pack, by_id, {t["topic_id"]: t["status"] for t in topics},
                              {t["topic_id"]: t["evidence"] for t in topics})
    rej += rej2
    expected = [s["id"] for s in section_titles(pack)]
    out.update({"items": len(items), "dropped": [(r["section"], r.get("part"), r["reason"]) for r in rej],
                "dropped_text": [r["entry"] if isinstance(r["entry"], str) else "" for r in rej],
                "empty_sections": [s for s in expected if not any(i.section == s for i in items)],
                "topic_summaries": sum(1 for t in topics if t["summary"]),
                "statuses": {t["topic_id"]: t["status"] for t in topics}})
    return out


async def main(session_id: str, pack_file: str, runs: int):
    d, utts, state = load(session_id)
    pack = load_pack(ROOT / "packs" / pack_file)
    settings = load_settings().model_copy(update={"experiment_label": f"review-drops:{pack.id}"})
    results = await asyncio.gather(*(one(n, d, utts, state, pack, settings) for n in range(1, runs + 1)))
    reasons = Counter()
    for r in results:
        if r.get("error"):
            print(f"run {r['run']}: FAILED {r['error'][:200]}")
            continue
        reasons.update(f"{s} {p or ''} {why.split(':')[0]}".replace("  ", " ") for s, p, why in r["dropped"])
        print(f"run {r['run']}: {r['items']} items, {len(r['dropped'])} dropped, empty sections {r['empty_sections']}, "
              f"{r['topic_summaries']} topic summaries, {r['latency_s']} s, ${r['cost']}"
              + (f", retry {r['retried']}" if r["retried"] else ""))
        for (s, p, why), text in zip(r["dropped"], r["dropped_text"]):
            print(f"    - {s}{' ' + p if p else ''}: {why}{'  <- ' + repr(text[:160]) if text else ''}")
    ok = [r for r in results if not r.get("error")]
    total_parts = sum(r["items"] + len(r["dropped"]) for r in ok)
    dropped = sum(len(r["dropped"]) for r in ok)
    print(f"\n{pack.id}: {len(ok)}/{runs} reviews ok; dropped {dropped} of {total_parts} parts "
          f"({dropped / total_parts:.1%}); runs with any drop: {sum(bool(r['dropped']) for r in ok)}/{len(ok)}; "
          f"runs with an empty section: {sum(bool(r['empty_sections']) for r in ok)}/{len(ok)}")
    print(f"statuses identical across runs: {len({json.dumps(r['statuses'], sort_keys=True) for r in ok}) == 1}")
    if ok:
        print(f"latency p50 {statistics.median(r['latency_s'] for r in ok)} s, cost total ${sum(r['cost'] for r in ok):.3f}")
    print("drop reasons:", dict(reasons))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
