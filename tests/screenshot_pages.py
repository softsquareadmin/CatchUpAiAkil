"""Screenshots of every page (M12 owner review; M13 README): python -m tests.screenshot_pages <out_dir>
Offline models and the fictional CPS script, in a temporary sessions folder. Sizes: 13-inch laptop 1280x800,
iPad landscape 1180x820 and portrait 820x1180; the live and review pages also in dark mode."""
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.ui_server import chromium_path, running_app

SIZES = {"laptop": (1280, 800), "tablet-landscape": (1180, 820), "tablet-portrait": (820, 1180)}


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, running_app(Path(tmp)) as base, sync_playwright() as p:
        b = p.chromium.launch(executable_path=chromium_path())
        page = b.new_page(viewport={"width": 1280, "height": 800})
        # one interview with live cards mid-way, then ended and reviewed
        page.goto(base + "/")
        page.wait_for_selector("#checklist li")
        page.select_option("#speed", "0")
        page.click("#startBtn")
        page.click("#replayBtn")
        page.wait_for_selector("#dialog:not([hidden])", timeout=20000)
        page.click("#dialogNo")  # keep going: the live screen with cards, as during an interview
        page.wait_for_timeout(5000)  # suggested cards wait for the end of the answer (cue_hold_s, 4 s)
        sid = page.evaluate("sessionStorage.getItem('interview.session')")
        for name, (w, h) in SIZES.items():
            for scheme in (["light", "dark"] if name == "laptop" else ["light"]):
                ctx = b.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, has_touch=name != "laptop")
                pg = ctx.new_page()
                pg.add_init_script(f"sessionStorage.setItem('interview.session', '{sid}')")  # resume the interview
                suffix = f"{name}{'-dark' if scheme == 'dark' else ''}"
                pg.goto(base + "/")
                pg.wait_for_selector("#transcript li.final")
                pg.wait_for_timeout(500)
                pg.screenshot(path=str(out / f"interview-{suffix}.png"))
                if scheme == "light":
                    for path, slug in [("/sessions", "sessions"), ("/config", "config"), ("/costs", "costs")]:
                        pg.goto(base + path)
                        pg.wait_for_timeout(800)
                        pg.screenshot(path=str(out / f"{slug}-{suffix}.png"), full_page=True)
                ctx.close()
        page.on("dialog", lambda d: d.accept())  # the leave prompt on reload
        page.reload()  # the screenshot windows resumed the interview; take it back to end it here
        page.wait_for_selector("#transcript li.final")
        page.click("#endBtn")
        page.wait_for_selector("#dialog:not([hidden])")
        page.click("#dialogYes")
        page.wait_for_selector("#reviewBanner:not([hidden])")
        page.screenshot(path=str(out / "interview-ended-laptop.png"))
        for name, (w, h) in SIZES.items():
            for scheme in (["light", "dark"] if name == "laptop" else ["light"]):
                ctx = b.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, has_touch=name != "laptop")
                pg = ctx.new_page()
                pg.goto(f"{base}/review/{sid}")
                pg.wait_for_selector(".result-card", timeout=20000)
                pg.screenshot(path=str(out / f"review-{name}{'-dark' if scheme == 'dark' else ''}.png"))
                ctx.close()
        b.close()
    print(f"screenshots in {out}")
    for f in sorted(out.glob("*.png")):
        print(" ", f.name)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
