"""Sample reports with the REAL models (PAID; run only when asked): python -m tests.make_real_reports [name ...]
Same fictional fixtures as tests.make_sample_reports, fed one line at a time as in a live interview (one gate call
per line). Nothing is reviewed by a person, so the report includes every item marked "Not reviewed", plus the
transcript and the cost. Writes docs/sample_reports/<name>_real_models.html and prints latency and cost per role."""
import asyncio
import json
import statistics
import sys
import time

from app.config import ROOT, load_settings
from app.export import build_report, load_session, render_html
from app.pack import load_pack
from tests.make_sample_reports import OUT, SAMPLES, fake_session


async def make(name: str) -> None:
    pack_file, fixture = SAMPLES[name]
    pack = load_pack(ROOT / pack_file)
    settings = load_settings().model_copy(update={"experiment_label": f"m11-real-report:{name}"})
    t0 = time.monotonic()
    s = await fake_session(pack, json.loads((ROOT / fixture).read_text()), ROOT / "sessions", settings)
    await s.close()
    wall = time.monotonic() - t0
    data = load_session(ROOT / "sessions" / s.id)
    report = build_report(data, pack, include_unreviewed=True, transcript=True, cost=True, sample=True)
    page = render_html(report)
    path = OUT / f"{name}_real_models.html"
    path.write_text(page, encoding="utf-8")
    rows = data["ledger"]
    print(f"\n{name}: session {s.id}, review {s.review_state}{' (' + str(s.review_error) + ')' if s.review_error else ''}, "
          f"{wall:.0f} s wall, {path.relative_to(ROOT)} {len(page.encode()) / 1024:.1f} KB")
    for role in ("gate", "cue", "review"):
        rs = [r for r in rows if r["role"] == role]
        ok = [r for r in rs if r["ok"]]
        if rs:
            lat = [r["latency_ms"] for r in ok] or [0]
            print(f"  {role:6} {len(ok)}/{len(rs)} ok, p50 {statistics.median(lat) / 1000:.1f} s, max {max(lat) / 1000:.1f} s, "
                  f"${sum(r['cost_usd'] or 0 for r in rs):.4f}")
    print(f"  total ${report['cost_usd']:.4f}; coverage {report['coverage']['score']}% {report['coverage']['counts']}")
    for t in report["topics"]:
        print(f"  {t['status']:12} {t['status_by']:6} {t['label']}")
    rej = [e for e in data["events"] if e.get("kind") == "review_rejected"]
    print(f"  review parts dropped by the checks: {len(rej)} {[r['reason'] for r in rej][:8]}")


if __name__ == "__main__":
    for n in sys.argv[1:] or list(SAMPLES):
        asyncio.run(make(n))
