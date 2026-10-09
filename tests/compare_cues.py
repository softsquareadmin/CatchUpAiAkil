"""Side-by-side cue writers (PAID; run only when asked): python -m tests.compare_cues
Replays the fixture at 1x with the real gate. Each time the session asks for a follow-up card, two writers get the
same input: the session's cue model (shown in the session) and a second model (logged only). Prints the pairs."""
import asyncio
import json
import time
from datetime import datetime

from app.adapters.analysts.llm import LLMAnalyst
from app.config import ROOT, load_settings
from app.main import FIXTURE
from app.pack import load_pack
from app.pipeline import Session

OTHER = ("google/gemini-3.8-flash", "minimal")


async def main():
    settings = load_settings().model_copy(update={"experiment_label": "m3-cue-compare"})
    pack = load_pack(ROOT / settings.pack)
    sent = []

    async def send(m):
        sent.append(m)

    s = Session(settings, pack, ROOT / "sessions", send)
    if s.decider is None or s.analyst is None:
        raise SystemExit(f"off: gate '{s.gate_off_reason}' cue '{s.cue_off_reason}'")
    main_w = s.analyst
    other = LLMAnalyst(pack, main_w.client, "openrouter", OTHER[0], s.ledger, reasoning_effort=OTHER[1])
    pairs = []

    async def timed(w, *a):
        t0 = time.monotonic()
        cue = await w.write_cue(*a)
        return {"model": w.model, "question": cue.question_or_note if cue else None,
                "quote": cue.evidence.quote if cue else None, "ms": int((time.monotonic() - t0) * 1000),
                "rejections": w.last_rejections, "error": w.last_error}

    class Both:
        client, model = main_w.client, main_w.model
        last_rejections, last_error = [], None

        async def write_cue(self, topic, state, window, audience):
            a, b = await asyncio.gather(timed(main_w, topic, state, window, audience),
                                        timed(other, topic, state, window, audience))
            pairs.append({"topic": topic.id, "level": state.follow_up, "reason": state.follow_up_reason,
                          "after": f"{window[-1].id} {window[-1].speaker}: {window[-1].text}", "audience": audience,
                          "writers": [a, b]})
            self.last_rejections, self.last_error = main_w.last_rejections, main_w.last_error
            return main_w._last_cue

    # keep the shown cue from the session's writer
    orig = main_w.write_cue

    async def keep(*a):
        main_w._last_cue = await orig(*a)
        return main_w._last_cue
    main_w.write_cue = keep
    s.analyst = Both()

    await s.start_replay(json.loads(FIXTURE.read_text()), 1, "replay")
    await s.replay_task
    await s.gate_idle()
    await s.cue_idle()
    await s.close()

    rows = [json.loads(l) for l in (s.dir / "ledger.jsonl").read_text().splitlines()]
    cost = {}
    for r in rows:
        cost[(r["role"], r["model"])] = cost.get((r["role"], r["model"]), 0) + r["cost_usd"]
    out = ROOT / "sessions" / f"cue-compare-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"session": s.id, "pairs": pairs, "cost": {f"{k[0]}:{k[1]}": v for k, v in cost.items()},
                               "shown": [m for m in sent if m["type"] in ("cue", "before_leave", "consent_check")]}, indent=1))
    for p in pairs:
        print(f"\n## {p['topic']} ({p['level']}: {p['reason']})  after {p['after'][:90]}")
        for w in p["writers"]:
            print(f"- {w['model']} {w['ms']} ms: {w['question']!r}  quote={w['quote']!r}"
                  + (f"  rejected={[r['reason'] for r in w['rejections']]}" if w["rejections"] else "")
                  + (f"  error={w['error'][:80]}" if w["error"] else ""))
    print("\ncards shown:", [(m["cue"]["kind"], m["cue"]["topic_id"], m["cue"]["question_or_note"]) for m in sent if m["type"] == "cue"])
    print("other prompts:", [m["type"] for m in sent if m["type"] in ("before_leave", "consent_check")])
    print("cost:", {f"{k[0]}:{k[1]}": round(v, 5) for k, v in cost.items()}, "total", round(sum(cost.values()), 4))
    print("full results:", out.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(main())
