"""M10a paid check (10a.6; PAID, run only when asked): python -m tests.check_limiter
1. The CPS script in Replay at instant speed, live session (real gate and cue), limiter at its configured rate.
2. One gate call per line, back to back (the pattern that hit 429s before M10a), limiter on.
3. The same burst with the limiter off, on a throwaway session, to confirm the 429s come back.
Reports from each session's ledger and events: calls, 429s, retries, waits, skipped runs, wall time."""
import asyncio
import json
import time

from app.config import ROOT, Limits, load_settings
from app.main import FIXTURE
from app.pack import load_pack
from app.pipeline import Session
from app.report import session_health
from tests.compare_gates import SETUPS, run


def health_of(session_id: str) -> dict:
    return next(h for h in session_health(ROOT / "sessions") if h["session"] == session_id)


async def instant_replay() -> dict:
    settings = load_settings().model_copy(update={"experiment_label": "limiter-check:replay"})

    async def send(m):
        pass

    s = Session(settings, load_pack(ROOT / settings.pack), ROOT / "sessions", send)
    t0 = time.monotonic()
    await s.start_replay(json.loads(FIXTURE.read_text()), 0, "replay")
    await s.replay_task
    await s.gate_idle()
    await s.cue_idle()
    wall = time.monotonic() - t0
    await s.close()
    return {"label": "instant replay (live, limiter on)", "session": s.id, "wall_s": round(wall, 1)}


async def burst(label: str, limits: Limits) -> dict:
    t0 = time.monotonic()
    r = await run("g38", {**SETUPS["g38"], "limits": limits, "experiment_label": f"limiter-check:{label}"})
    return {"label": label, "session": r["session"], "wall_s": round(time.monotonic() - t0, 1),
            "final_status_agree": r["final_status_agree"], "cost_usd": r["cost_usd"]}


async def main():
    on = load_settings().limits
    results = [await instant_replay(),
               await burst("burst, limiter on", on),
               await burst("burst, limiter off", on.model_copy(update={"requests_per_min": {}}))]
    for r in results:
        r.update({k: v for k, v in health_of(r["session"]).items() if k not in ("session", "experiment")})
        print(json.dumps(r))


if __name__ == "__main__":
    asyncio.run(main())
