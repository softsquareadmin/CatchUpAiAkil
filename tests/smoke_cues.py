"""M3 smoke test with real models (PAID; run only when asked): python -m tests.smoke_cues
Replays the fixture at 1x with the configured gate and cue models, as the UI would, and prints every card,
flag and prompt with timing, plus the cost per role."""
import asyncio
import json
import time

from app.config import ROOT, load_settings
from app.cues import Lint
from app.evidence import validate
from app.main import FIXTURE
from app.models import Evidence
from app.pack import load_pack
from app.pipeline import Session


async def main():
    settings = load_settings().model_copy(update={"experiment_label": "m3-smoke"})
    pack = load_pack(ROOT / settings.pack)
    t0, shown = time.monotonic(), []

    async def send(m):
        if m["type"] in ("cue", "cue_withdrawn", "before_leave", "consent_check"):
            shown.append((round(time.monotonic() - t0, 1), m))

    s = Session(settings, pack, ROOT / "sessions", send)
    if s.decider is None or s.analyst is None:
        raise SystemExit(f"off: gate '{s.gate_off_reason}' cue '{s.cue_off_reason}'")
    await s.start_replay(json.loads(FIXTURE.read_text()), 1, "replay")
    await s.replay_task
    await s.gate_idle()
    await s.cue_idle()
    await s.close()

    transcript = {u.id: u for u in s.store.utterances}
    for t, m in shown:
        if m["type"] == "cue":
            c = m["cue"]
            ok = validate(Evidence(**c["evidence"]), transcript)
            lint = Lint(s.pack).question(c["question_or_note"], s.store.utterances) if c["kind"] == "follow_up" else None
            print(f"{t:6}s {c['kind']:9} {c['topic_id']:22} {m.get('level', ''):9} {c['question_or_note']!r} "
                  f"[{c['evidence']['utterance_id']}: {c['evidence']['quote']!r}] quote_ok={ok} lint={lint} "
                  f"write_ms={m.get('latency_ms', '')}")
        elif m["type"] == "cue_withdrawn":
            print(f"{t:6}s withdrawn {m['id']}")
        else:
            print(f"{t:6}s {m['type']} {[o['id'] for o in m.get('open', [])]}")
    events = [json.loads(l) for l in (s.dir / "events.jsonl").read_text().splitlines()]
    print("cue rejections:", [(e["topic_id"], e["reason"]) for e in events if e["kind"] == "cue_rejected"])
    print("cue skipped:", [(e["topic_id"], e.get("level"), (e.get("error") or "")[:60]) for e in events if e["kind"] == "cue_skipped"])
    rows = [json.loads(l) for l in (s.dir / "ledger.jsonl").read_text().splitlines()]
    for role in ("gate", "cue"):
        rs = [r for r in rows if r["role"] == role]
        lat = sorted(r["latency_ms"] for r in rs if r["ok"])
        print(f"{role}: calls {len(rs)}, ok {sum(r['ok'] for r in rs)}, p50 {lat[len(lat)//2] if lat else None} ms, "
              f"cost ${sum(r['cost_usd'] for r in rs):.4f}")
    print("session:", s.id)


if __name__ == "__main__":
    asyncio.run(main())
