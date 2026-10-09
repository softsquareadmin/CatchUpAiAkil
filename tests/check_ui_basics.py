"""UI basics check (M12 12.4): python -m tests.check_ui_basics
1. Contrast of the colour-token pairs the pages use, light and dark (WCAG AA: 4.5:1 for text, 3:1 for status bars
   and other non-text marks), read from app/static/css/tokens.css.
2. Touch targets: on every page at tablet size with touch, the main controls (buttons, links styled as buttons,
   selects, text inputs, checkbox labels) are at least 44 x 44 px.
Prints every failure and exits 1 if there is any; nothing is hidden."""
import re
import sys
import tempfile
from pathlib import Path

from app.config import ROOT

TOKENS = ROOT / "app" / "static" / "css" / "tokens.css"
# (foreground, background, minimum ratio, where it is used)
PAIRS = [
    ("ink", "bg", 4.5, "body text"), ("ink", "surface", 4.5, "text on cards"), ("ink-2", "surface", 4.5, "secondary text"),
    ("muted", "surface", 4.5, "hints on cards"), ("muted", "bg", 4.5, "hints on the page"), ("muted", "surface-2", 4.5, "table headers"),
    ("ink", "surface-2", 4.5, "quotes"), ("accent", "surface", 4.5, "links, quiet buttons, user role"),
    ("accent-ink", "accent", 4.5, "primary buttons"), ("accent-soft-ink", "accent-soft", 4.5, "current page, banner"),
    ("ok", "ok-soft", 4.5, "covered badge"), ("warn", "warn-soft", 4.5, "partial / suggested badge"),
    ("neutral", "neutral-soft", 4.5, "not covered badge"), ("danger", "danger-soft", 4.5, "error badge, problems"),
    ("danger-ink", "danger", 4.5, "danger buttons, required badge, error toast"), ("flag", "flag-soft", 4.5, "flag badge and card"),
    ("ok", "surface", 4.5, "coverage count"), ("warn", "surface", 4.5, "coverage count, role colour"),
    ("neutral", "surface", 4.5, "coverage count"), ("flag", "surface", 4.5, "role colour"), ("surface", "ink", 4.5, "toast"),
    ("surface", "ok", 4.5, "saved toast"), ("ink", "accent-soft", 4.5, "selected transcript line"),
    ("ok-bar", "surface", 3.0, "covered bar (with text label)"), ("warn-bar", "surface", 3.0, "partial bar (with text label)"),
    ("neutral-bar", "surface", 3.0, "not covered bar (with text label)"), ("danger", "surface", 3.0, "required bar"),
    ("line-strong", "surface", 3.0, "input borders"),
]


def tokens() -> dict[str, dict[str, str]]:
    css = TOKENS.read_text()
    light = re.search(r":root\s*{(.*?)}", css, re.S).group(1)
    dark = re.search(r"prefers-color-scheme:\s*dark\)\s*{\s*:root\s*{(.*?)}", css, re.S).group(1)
    parse = lambda block: dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))  # noqa: E731
    base = parse(light)
    return {"light": base, "dark": {**base, **parse(dark)}}


def luminance(hex_: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check_contrast() -> list[str]:
    fails = []
    for theme, t in tokens().items():
        for fg, bg, need, where in PAIRS:
            r = ratio(t[fg], t[bg])
            line = f"{theme:5} {fg:>16} on {bg:<12} {r:5.2f}:1 (need {need}) {where}"
            print(("FAIL " if r < need else "ok   ") + line)
            if r < need:
                fails.append(line)
    return fails


CONTROLS = ("button:visible, a.btn:visible, select:visible, input[type=text]:visible, input:not([type]):visible, "
            "textarea:visible, label.check:visible, .nav a:visible")


def check_targets() -> list[str]:
    from playwright.sync_api import sync_playwright

    from tests.ui_server import chromium_path, running_app
    fails = []
    with tempfile.TemporaryDirectory() as tmp, running_app(Path(tmp)) as base, sync_playwright() as p:
        b = p.chromium.launch(executable_path=chromium_path())
        ctx = b.new_context(viewport={"width": 1180, "height": 820}, has_touch=True)
        page = ctx.new_page()
        # a session with a review, so the review page has its controls
        page.goto(base + "/")
        page.wait_for_selector("#checklist li")
        page.select_option("#speed", "0")
        page.click("#startBtn")
        page.click("#replayBtn")
        page.wait_for_selector("#dialog:not([hidden])", timeout=20000)
        page.click("#dialogYes")
        page.wait_for_selector("#reviewBanner:not([hidden])")
        sid = page.evaluate("sessionStorage.getItem('interview.session')")
        for path in ["/", f"/review/{sid}", "/config", "/sessions", "/costs"]:
            page.goto(base + path)
            page.wait_for_timeout(1500)
            small = page.evaluate(f"""() => [...document.querySelectorAll('{CONTROLS}'.replaceAll(':visible', ''))]
              .filter((e) => e.offsetParent !== null && !e.closest('.coverage-ring'))
              .map((e) => {{ const r = e.getBoundingClientRect(); return [e.tagName.toLowerCase() + (e.id ? '#' + e.id : '')
                + (e.className && typeof e.className === 'string' ? '.' + e.className.split(' ')[0] : ''),
                (e.textContent || e.getAttribute('aria-label') || '').trim().slice(0, 30), Math.round(r.width), Math.round(r.height)]; }})
              .filter(([, , w, h]) => w > 0 && (h < 44 || w < 44))""")
            print(f"{path}: {len(small)} control(s) under 44 px")
            for tag, text, w, h in small:
                line = f"{path} {tag} '{text}' {w}x{h}"
                print("FAIL " + line)
                fails.append(line)
        b.close()
    return fails


if __name__ == "__main__":
    fails = check_contrast() + check_targets()
    print(f"\n{len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
