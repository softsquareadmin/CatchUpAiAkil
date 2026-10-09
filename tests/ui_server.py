"""Run the app in a background thread for browser tests and screenshots (M12): offline models, a temporary sessions
folder and a copy of packs/ (saving a pack from /config never touches the real one). Also the browser to use:
Playwright's cached Chromium, else Playwright's default."""
import contextlib
import glob
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import uvicorn

from app import main
from app.config import ROOT, load_settings

FAKE = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"}


def chromium_path() -> str | None:
    for pattern in ("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*",
                    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"):
        found = sorted(glob.glob(os.path.expanduser(pattern)))
        if found:
            return found[-1]
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def running_app(tmp: Path, environ: dict | None = None):
    """Yields the base URL. Patches main.PACKS_DIR to a copy for the life of the server."""
    packs = tmp / "packs"
    shutil.copytree(ROOT / "packs", packs)
    old = main.PACKS_DIR
    main.PACKS_DIR = packs
    settings = load_settings(env_file=None, environ={**FAKE, **(environ or {})})
    app = main.create_app(settings, sessions_dir=tmp / "sessions")
    (tmp / "sessions").mkdir(exist_ok=True)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        main.PACKS_DIR = old
