"""Headless Chrome / Chromium with the network cut off (SPEC-M11 export test): open a local file, screenshot it and
dump the rendered DOM. Finds Google Chrome or a Playwright-downloaded Chromium; returns None if neither is installed."""
import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CANDIDATES = [  # the headless shell first: desktop Chrome in --headless=new can hang after writing the screenshot
    *sorted(glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell"))),
    *sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell"))),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    *sorted(glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"))),
    *sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))),
]


def find_browser() -> str | None:
    for c in CANDIDATES:
        if Path(c).is_file():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser")


# No network: every request goes to a closed local port, and host names do not resolve.
OFFLINE = ["--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>", "--host-resolver-rules=MAP * ~NOTFOUND"]


def render_offline(page: Path, screenshot: Path, size: str = "900,2400") -> str | None:
    """Screenshot `page` and return its rendered DOM, or None when no browser is installed."""
    browser = find_browser()
    if browser is None:
        return None
    with tempfile.TemporaryDirectory() as profile:
        headless = [] if "headless-shell" in browser else ["--headless=new"]
        common = [browser, *headless, "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                  f"--user-data-dir={profile}", *OFFLINE, f"--window-size={size}"]
        subprocess.run([*common, f"--screenshot={screenshot}", page.resolve().as_uri()], capture_output=True, timeout=90)
        dom = subprocess.run([*common, "--dump-dom", page.resolve().as_uri()], capture_output=True, text=True, timeout=90)
    return dom.stdout
