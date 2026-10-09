"""Post-interview review with the real review model (PAID, ~$0.04; run only when asked):
python -m tests.smoke_review [source_session_id] [pack file]
Reuses the transcript and final checklist of an earlier real-gate session (default: the M3 smoke run), so no gate
or cue calls are made. Prints every kept item, every rejected one with the reason, and the cost."""
import asyncio
import json
import sys

from app.config import ROOT, load_settings
from app.models import TopicState, Utterance
from app.pack import load_pack
from app.pipeline import Session

SOURCE = "20261008-033226-1b67"


async def main(source: str, pack_file: str | None = None, label: str = "m5-review-smoke"):
    src = ROOT / "sessions" / source
    settings = load_settings().model_copy(update={"experiment_label": label, **({"pack": pack_file} if pack_file else {})})
    sent = []

    async def send(m):
        sent.append(m)

    s = Session(settings, load_pack(ROOT / settings.pack), ROOT / "sessions", send)
    s.decider = s.analyst = None
    for l in (src / "utterances.jsonl").read_text().splitlines():
        s.store.append(Utterance(**json.loads(l)).model_copy(update={"id": "", "session_id": s.id}))
    for l in (src / "events.jsonl").read_text().splitlines():
        e = json.loads(l)
        if e["kind"] == "topic_update":
            s.checklist[e["state"]["item_id"]] = TopicState(**e["state"])
    await s.run_review()
    await s.close()
    if s.draft is None:
        raise SystemExit(f"review failed: {s.review_error}")
    for item in s.draft["items"]:
        q = "; ".join(f"{e['utterance_id']}: {e['quote']!r}" for e in item["evidence"])
        print(f"[{item['section']}] {item['label']}: {item['value']}\n      {q}")
    print("\nrejected:")
    for l in (s.dir / "events.jsonl").read_text().splitlines():
        e = json.loads(l)
        if e["kind"] == "review_rejected":
            print(f"  [{e['section']}] {e['reason']}: {json.dumps(e['entry'])[:220]}")
    print("\nchecklist (computed):", json.dumps(s.draft["checklist"])[:600])
    for r in map(json.loads, (s.dir / "ledger.jsonl").read_text().splitlines()):
        print(f"ledger: {r['role']} {r['model']} in={r['input_tokens']} out={r['output_tokens']} "
              f"{r['latency_ms']} ms ${r['cost_usd']:.4f} ok={r['ok']} {r['error'] or ''}")
    print("session:", s.id)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else SOURCE, sys.argv[2] if len(sys.argv) > 2 else None,
                     "m10-job-review" if len(sys.argv) > 2 else "m5-review-smoke"))
