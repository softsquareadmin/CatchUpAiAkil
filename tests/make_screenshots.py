"""README screenshots (M13): python -m tests.make_screenshots [--out docs/screenshots] [--costs-from sessions/<id> ...]
Offline models, the fictional CPS script and a temporary sessions folder, so it costs nothing and can be re-run when
the UI changes. --costs-from copies saved session folders (fictional fixtures only) into the temporary folder so the
cost report has rows; without it that page shows its empty state. Laptop 1280x800; the live screen also at tablet
width (iPad landscape 1180x820)."""
import argparse
import shutil
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.config import ROOT
from tests.ui_server import chromium_path, running_app

LAPTOP, TABLET = (1280, 800), (1180, 820)


def main(out: Path, costs_from: list[Path]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, running_app(Path(tmp)) as base, sync_playwright() as p:
        for d in costs_from:
            shutil.copytree(d, Path(tmp) / "sessions" / d.name)
        b = p.chromium.launch(executable_path=chromium_path())
        shot = lambda pg, name, **kw: pg.screenshot(path=str(out / f"{name}.png"), **kw)  # noqa: E731

        # consent prompt: a typed line refusing consent (its own session)
        ctx = b.new_context(viewport={"width": LAPTOP[0], "height": LAPTOP[1]})
        pg = ctx.new_page()
        pg.goto(base + "/")
        pg.wait_for_selector("#checklist li")
        pg.click('.modes button[data-mode="typed"]')
        pg.click("#startBtn")
        pg.click('#typedSpeaker button[data-speaker="parent"]')
        pg.fill("#typedText", "I do not consent to recording.")
        pg.press("#typedText", "Enter")
        pg.wait_for_selector("#dialog:not([hidden])", timeout=20000)
        pg.wait_for_timeout(300)
        shot(pg, "consent-prompt")
        pg.click("#dialogYes")  # "Stop the tool": this session ends here
        pg.wait_for_timeout(300)
        ctx.close()

        # one interview replayed: "Before you leave", the live screen with cards, then ended and reviewed
        ctx = b.new_context(viewport={"width": LAPTOP[0], "height": LAPTOP[1]})
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_selector("#checklist li")
        shot(page, "pack-picker", clip={"x": 0, "y": 0, "width": LAPTOP[0], "height": 124})
        page.select_option("#speed", "0")
        page.click("#startBtn")
        page.click("#replayBtn")
        page.wait_for_selector("#dialog:not([hidden])", timeout=20000)
        page.wait_for_timeout(300)
        shot(page, "before-you-leave")
        page.click("#dialogNo")  # keep going: the live screen as during an interview
        page.wait_for_timeout(5000)  # suggested cards wait for the end of the answer (cue_hold_s, 4 s)
        shot(page, "live-laptop")
        page.locator("#cues li.cue.required").first.screenshot(path=str(out / "follow-up-card.png"))
        chip = page.locator('#checklist li.chip[data-status="covered"]').first
        chip.locator("button").first.click()
        page.wait_for_timeout(300)
        chip.screenshot(path=str(out / "checklist-quote.png"))
        sid = page.evaluate("sessionStorage.getItem('interview.session')")

        tab = b.new_context(viewport={"width": TABLET[0], "height": TABLET[1]}, has_touch=True)
        tp = tab.new_page()
        tp.add_init_script(f"sessionStorage.setItem('interview.session', '{sid}')")  # resume the same interview
        tp.goto(base + "/")
        tp.wait_for_selector("#transcript li.final")
        tp.wait_for_timeout(500)
        shot(tp, "live-tablet")
        tab.close()

        page.on("dialog", lambda d: d.accept())  # the leave prompt on reload
        page.reload()  # the tablet window resumed the interview; take it back to end it here
        page.wait_for_selector("#transcript li.final")
        page.click("#endBtn")
        page.wait_for_selector("#dialog:not([hidden])")
        page.click("#dialogYes")
        page.wait_for_selector("#reviewBanner:not([hidden])")

        page.goto(f"{base}/review/{sid}")
        page.wait_for_selector(".result-card", timeout=20000)
        page.wait_for_timeout(300)
        shot(page, "review")
        card = page.locator('.result-card[data-status="partial"]').first
        if card.get_attribute("open") is None:
            card.locator("summary").click()
        card.screenshot(path=str(out / "review-topic-card.png"))
        page.locator("#exportBox").scroll_into_view_if_needed()
        page.locator("#exportBox").screenshot(path=str(out / "export-options.png"))

        # the reviewer accepts everything (as if each card had been read), so the report has content
        api = f"{base}/api/sessions/{sid}/review"
        draft = page.request.get(api).json()["review"]["draft"]
        for item in draft["items"]:
            page.request.post(api, data={"kind": "item", "id": item["id"], "action": "accept"})
        for t in draft["topics"]:
            for part in ("summary", "missing", "follow_up"):
                if t.get(part):
                    page.request.post(api, data={"kind": "topic", "topic_id": t["topic_id"], "part": part, "action": "accept"})
        page.goto(f"{base}/api/sessions/{sid}/export.html?view=true")
        page.wait_for_selector("details")
        shot(page, "report-top")
        page.locator("details.topic.partial").first.screenshot(path=str(out / "report-topic-card.png"))

        for path, name in [("/config", "config"), ("/sessions", "sessions"), ("/costs", "costs")]:
            page.goto(base + path)
            page.wait_for_timeout(1000)
            shot(page, name)
        b.close()
    print(f"screenshots in {out}")
    for f in sorted(out.glob("*.png")):
        print(" ", f.name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    ap.add_argument("--costs-from", type=Path, nargs="*", default=[])
    a = ap.parse_args()
    main(a.out, a.costs_from)
