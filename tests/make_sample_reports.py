"""Sample HTML reports from the fictional fixtures with the fake adapters (no paid call; SPEC-M11 11.5):
python -m tests.make_sample_reports  ->  docs/sample_reports/cps_replay_sample.html, job_interview_sample.html.
The review decisions are simulated the same way every time (accept all, edit one follow-up, reject one item) and the
session id and dates are fixed, so the output is byte-for-byte stable (tests/test_m11_report.py compares it)."""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import ROOT, load_settings
from app.export import build_report, load_session, render_html
from app.models import Utterance
from app.pack import load_pack
from app.pipeline import Session

SAMPLES = {"cps_replay_sample": ("packs/cps_interview_v2.yaml", "tests/fixtures/script_v2.json"),
           "job_interview_sample": ("packs/job_interview_v1.yaml", "tests/fixtures/job_interview_sample.json")}
OUT = ROOT / "docs" / "sample_reports"
FIXED = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
FAKE = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"}


async def fake_session(pack, lines: list[dict], sessions: Path, settings=None) -> Session:
    """Feed the lines one at a time (fixed timestamps), end the interview and wait for the review (fake models
    unless `settings` says otherwise)."""
    async def send(m):
        pass
    s = Session(settings or load_settings(env_file=None, environ=FAKE), pack, sessions, send)
    for ln in lines:
        await s.on_utterance(Utterance(id="", session_id=s.id, t_start_ms=ln["t_ms"], t_end_ms=ln["t_end_ms"],
                                       speaker=ln["speaker"], text=ln["text"], is_final=True, source="replay"))
        await s.gate_idle()
        await s.cue_idle()
    await s.end_interview()
    while s.review_state == "running":
        await asyncio.sleep(0.01)
    return s


async def simulate_review(s: Session) -> None:
    for t in s.draft["topics"]:
        for part in ("summary", "missing", "follow_up"):
            if t.get(part):
                await s.topic_action(t["topic_id"], part, "accept")
    gap = next((t for t in s.draft["topics"] if t.get("follow_up")), None)
    if gap:
        await s.topic_action(gap["topic_id"], "follow_up", "edit", "What happened next, in your own words?")
    items = s.draft["items"]
    for i in items:
        await s.review_action(i["id"], "accept")
    if items:
        await s.review_action(items[-1]["id"], "reject")


def fixed(data: dict, name: str) -> dict:
    """Stable ids and dates, so the same fixture always gives the same file."""
    data["session_id"] = name
    for a in data["audit"]:
        a["ts"] = FIXED.isoformat()
    return data


async def report_for(pack_file: str, fixture: str, name: str, review: bool = True, **opts) -> tuple[dict, str]:
    """A fixed-id report from a fake run; review=False leaves every item unreviewed."""
    pack = load_pack(ROOT / pack_file)
    with tempfile.TemporaryDirectory() as tmp:
        s = await fake_session(pack, json.loads((ROOT / fixture).read_text()), Path(tmp))
        if review:
            await simulate_review(s)
        await s.close()
        data = fixed(load_session(Path(tmp) / s.id), name)
    report = build_report(data, pack, now=FIXED, **opts)
    return report, render_html(report)


async def make(name: str, **opts) -> tuple[dict, str]:
    return await report_for(*SAMPLES[name], name, sample=True, **opts)


def main(names: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names:
        _, page = asyncio.run(make(name))
        path = OUT / f"{name}.html"
        path.write_text(page, encoding="utf-8")
        print(f"{path.relative_to(ROOT)}: {len(page.encode()) / 1024:.1f} KB")


if __name__ == "__main__":
    main(sys.argv[1:] or list(SAMPLES))
