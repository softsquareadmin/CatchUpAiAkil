"""Compare gate setups on the fixture (PAID; run only when asked): python -m tests.compare_gates [setup ...]
Each setup gets its own session, fed one final utterance at a time (one gate call each). Calls go through the
app's request limiter (M10a, `limits.requests_per_min`, 18/min for OpenRouter by default) instead of hand spacing;
the session runs at "batch" priority so a live interview on the same key goes first. The checklist the worker would see is compared
after every line with tests/fixtures/timeline_v3.json (coverage + follow-up marker).
Earlier results (v1 pack, single needs_follow_up status): see docs/dev-notes/PROGRESS.md (scored with an earlier answer key, since removed)."""
import asyncio
import json
import statistics
import sys
from datetime import datetime

from app.config import ROOT, load_settings
from app.main import FIXTURE
from app.models import Utterance
from app.pack import load_pack
from app.pipeline import Session

SETUPS = {
    "g38": {"models.gate": "openrouter:google/gemini-3.8-flash", "gate_adapter": "llm"},
    "g38-t0": {"models.gate": "openrouter:google/gemini-3.8-flash", "gate_adapter": "llm", "gate_temperature": 0.0},
    # M10 item 5: the tier-1-only CPS pack (same topic ids, same answer key)
    "g38-simple": {"models.gate": "openrouter:google/gemini-3.8-flash", "gate_adapter": "llm",
                   "pack": "packs/cps_interview_v2_simple.yaml"},
    "hybrid": {"models.gate": "openrouter:google/gemini-3.8-flash", "gate_adapter": "hybrid"},
    "hybrid-0.7": {"models.gate": "openrouter:google/gemini-3.8-flash", "gate_adapter": "hybrid",
                   "hybrid_min_confidence": 0.7},
    "g35lite": {"models.gate": "openrouter:google/gemini-3.5-flash-lite", "gate_adapter": "llm"},
    "gpt54-mini": {"models.gate": "openrouter:openai/gpt-5.4-mini", "gate_adapter": "llm"},
    "luna-pro": {"models.gate": "openrouter:openai/gpt-5.6-luna-pro", "gate_adapter": "llm"},
    "jev-choice": {"gate_adapter": "jev", "jev_questions": "choice"},
}
TIMELINE = json.loads((ROOT / "tests/fixtures/timeline_v3.json").read_text())["topics"]


def truth_after(topic: str, line: int) -> tuple[str, str]:
    """(status, follow_up) expected after fixture line `line`."""
    entries = [(st, fu) for ln, st, fu in TIMELINE.get(topic, []) if ln <= line]
    return entries[-1] if entries else ("not_covered", "none")


def matches(shown: tuple[str, str], expected: tuple[str, str]) -> bool:
    """Strict on coverage and on *required* follow-ups. *Suggested* is advisory and up to the caseworker
    (owner, 2026-10-08), so suggested vs none is not an error; it is counted separately."""
    return shown[0] == expected[0] and (shown[1] == "required") == (expected[1] == "required")


def score(snapshots: list[dict]) -> dict:
    """snapshots[i][topic] = [status, follow_up] after line i+1."""
    n = both = status_ok = req_missed = req_false = sugg_diff = 0
    for i, snap in enumerate(snapshots, start=1):
        for topic, (st, fu) in snap.items():
            t_st, t_fu = truth_after(topic, i)
            n += 1
            status_ok += st == t_st
            both += matches((st, fu), (t_st, t_fu))
            req_missed += t_fu == "required" and fu != "required"
            req_false += fu == "required" and t_fu != "required"
            sugg_diff += (fu == "suggested") != (t_fu == "suggested") and "required" not in (fu, t_fu)
    last, final = snapshots[-1], {k: truth_after(k, len(snapshots)) for k in snapshots[-1]}
    return {"line_acc": round(both / n, 3), "line_status_acc": round(status_ok / n, 3),
            "required_missed": req_missed, "required_false": req_false, "suggested_differences": sugg_diff,
            "final_status_agree": sum(last[k][0] == final[k][0] for k in last),
            "final_both_agree": sum(matches(tuple(last[k]), final[k]) for k in last)}


async def run(label: str, overrides: dict) -> dict:
    base = load_settings()
    models = dict(base.models)
    upd = {"experiment_label": f"gate-compare:{label}"}
    for k, v in overrides.items():
        if k.startswith("models."):
            models[k.split(".", 1)[1]] = v
        else:
            upd[k] = v
    settings = base.model_copy(update={**upd, "models": models})
    pack = load_pack(ROOT / settings.pack)

    async def send(m):
        pass

    s = Session(settings, pack, ROOT / "sessions", send, batch=True)
    s.analyst = None  # gate only (since M10): no cue calls in comparison runs
    if s.decider is None:
        return {"label": label, "error": s.gate_off_reason}
    jev = getattr(s.decider, "jev", s.decider)  # the hybrid gate wraps a JevDecider
    jev_calls, inner = [], s.decider.evaluate

    async def recording_evaluate(window, state, **kw):
        r = await inner(window, state, **kw)
        if getattr(jev, "last_answers", None):  # every Jev call, for offline analysis
            jev_calls.append({"window": [w.id for w in window], "answers": jev.last_answers})
        return r

    s.decider.evaluate = recording_evaluate
    snapshots = []  # [status, follow_up] per topic after each line: re-scorable offline if the key changes
    for ln in json.loads(FIXTURE.read_text()):
        await s.on_utterance(Utterance(id="", session_id=s.id, t_start_ms=ln["t_ms"], t_end_ms=ln["t_end_ms"],
                                       speaker=ln["speaker"], text=ln["text"], is_final=True, source="replay"))
        await s.gate_idle()
        snapshots.append({k: [v.status, v.follow_up] for k, v in s.checklist.items()})
    await s.close()

    rows = [json.loads(l) for l in (s.dir / "ledger.jsonl").read_text().splitlines()]
    events = [json.loads(l) for l in (s.dir / "events.jsonl").read_text().splitlines()]
    ok = [r for r in rows if r["ok"]]
    upd_lat = [e for e in events if e["kind"] == "topic_update"]
    return {
        "label": label, "session": s.id, "model": s.gate_status()["model"], "pack": pack.ref,
        "calls": len(rows), "ok": len(ok),
        "invalid_json": sum(1 for r in rows if (r["error"] or "").startswith("invalid_json")),
        "errors": [r["error"][:120] for r in rows if not r["ok"] and not (r["error"] or "").startswith("invalid_json")],
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 5),
        "rejected": sum(1 for e in events if e["kind"] == "rejected"),
        "by_model": {m: {"calls": len(rs), "p50_ms": statistics.median([r["latency_ms"] for r in rs])}
                     for m in sorted({r["model"] for r in ok}) for rs in [[r for r in ok if r["model"] == m]]},
        "update_ms": {st: {"n": len(ls), "p50": statistics.median(ls), "max": max(ls)}
                      for st in ("fast", "final") for ls in [[e["latency_ms"] for e in upd_lat if e.get("stage") == st]] if ls},
        "final": snapshots[-1], "snapshots": snapshots, "jev_calls": jev_calls or None,
        **score(snapshots),
    }


async def main(labels):
    results = []
    for label in labels:
        print(f"running {label} ...", flush=True)
        results.append(await run(label, SETUPS[label]))
    out = ROOT / "sessions" / f"gate-compare-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(results, indent=1))

    ok = [r for r in results if "error" not in r]
    for r in results:
        if "error" in r:
            print(f"{r['label']}: gate off: {r['error']}")
    print("\n| setup | calls ok | invalid JSON | final status /11 | final status+required /11 | line acc (status+required) "
          "| required missed | false required | suggested differences | update p50 ms (fast / final) | cost $ |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in ok:
        u = r["update_ms"]
        lat = " / ".join(f"{u[st]['p50']:.0f} (n={u[st]['n']})" for st in ("fast", "final") if st in u)
        print(f"| {r['label']} | {r['ok']}/{r['calls']} | {r['invalid_json']} | {r['final_status_agree']} | "
              f"{r['final_both_agree']} | {r['line_acc']} | {r['required_missed']} | {r['required_false']} | "
              f"{r['suggested_differences']} | {lat} | {r['cost_usd']} |")
    keys = list(ok[0]["final"]) if ok else []
    print("\n| topic | expected | " + " | ".join(r["label"] for r in ok) + " |")
    print("|---|---|" + "---|" * len(ok))
    for k in keys:
        exp = truth_after(k, 23)
        cells = [("" if matches(tuple(r["final"][k]), exp) else "**") + "/".join(r["final"][k]) +
                 ("" if matches(tuple(r["final"][k]), exp) else "**") for r in ok]
        print(f"| {k} | {'/'.join(exp)} | " + " | ".join(cells) + " |")
    for r in ok:
        if r["errors"]:
            print(f"\n{r['label']} errors: {r['errors'][:3]}")
    print(f"\nfull results: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(SETUPS)))
