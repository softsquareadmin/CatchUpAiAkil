"""M12 browser checks (headless Chromium, offline models, no network): every page loads with no console errors,
the main flows work, the leave warning, the pack lock, reload resume, and no request leaves localhost."""
import json
import tempfile
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from tests.ui_server import chromium_path, running_app  # noqa: E402

PAGES = ["/", "/sessions", "/config", "/costs"]


@pytest.fixture(scope="module")
def app_url():
    with tempfile.TemporaryDirectory() as tmp, running_app(Path(tmp)) as base:
        yield base, Path(tmp)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=chromium_path())
        except Exception as e:  # no browser installed
            pytest.skip(f"no Chromium: {e}")
        yield b
        b.close()


class Watch:
    """Console errors, page errors and every request URL of one page."""

    def __init__(self, page, base):
        self.errors, self.requests, self.base = [], [], base
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: self.requests.append(r.url))

    def offsite(self):
        return [u for u in self.requests if not (u.startswith(self.base) or u.startswith("data:")
                                                 or u.startswith(self.base.replace("http", "ws")))]


def new_page(browser, base, width=1280, height=800):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    page = ctx.new_page()
    return page, Watch(page, base)


def start(page):
    page.click("#startBtn")
    page.wait_for_selector("#endBtn:not([hidden])")


def replay_and_end(page):
    page.wait_for_selector("#checklist li")
    page.select_option("#speed", "0")
    start(page)
    page.click("#replayBtn")
    page.wait_for_selector(".cue", timeout=20000)
    page.wait_for_selector("#dialog:not([hidden])", timeout=20000)  # the script's last line triggers "Before you leave"
    page.click("#dialogYes")
    page.wait_for_selector("#reviewBanner:not([hidden])")


def test_every_page_loads_with_no_console_errors_and_no_offsite_requests(app_url, browser):
    base, _ = app_url
    for path in PAGES:
        page, w = new_page(browser, base)
        r = page.goto(base + path)
        assert r.status == 200, path
        page.wait_for_selector("#appbar .nav a[aria-current=page]")
        page.wait_for_timeout(400)
        assert not w.errors, (path, w.errors)
        assert not w.offsite(), (path, w.offsite())
        page.context.close()


def test_interview_review_export_sessions_and_costs_flow(app_url, browser):
    base, _ = app_url
    page, w = new_page(browser, base)
    page.goto(base + "/")
    page.wait_for_selector("#packSel option", state="attached")
    assert page.input_value("#packSel") == "cps_interview_v2.yaml"
    replay_and_end(page)
    assert page.locator("#cues .cue").count() >= 1
    sid = page.evaluate("sessionStorage.getItem('interview.session')")
    page.click("#reviewLink")
    page.wait_for_url(f"**/review/{sid}")
    page.wait_for_selector(".result-card", timeout=20000)
    first = page.locator(".result-card .ritem").first
    first.locator("button:has-text('Accept')").click()
    page.wait_for_selector(".result-card .ritem[data-status=accepted]")
    with page.expect_download() as dl:
        page.click("#exportHtml")
    html = Path(dl.value.path()).read_text()
    assert "Checklist coverage" in html and "AI-assisted draft" in html
    page.click(".nav a[href='/sessions']")
    page.wait_for_selector(f"tr:has-text('{sid}')")
    row = page.locator(f"tr:has-text('{sid}')")
    assert "Reviewed" in row.inner_text() and row.locator("a:has-text('Report')").count() == 1
    page.click(".nav a[href='/costs']")
    page.wait_for_selector("h1:has-text('Cost report')")
    assert not w.errors, w.errors
    assert not w.offsite(), w.offsite()
    page.context.close()


def test_config_add_topic_save_new_version_and_see_it_in_the_picker(app_url, browser):
    base, tmp = app_url
    page, w = new_page(browser, base)
    page.goto(base + "/config?pack=job_interview_v1.yaml")
    page.wait_for_selector(".cfg-topic")
    n = page.locator(".cfg-topic").count()
    page.click("#cfgEdit")
    page.click("#cfgAddTopic")
    topic = page.locator(".cfg-topic").nth(n)
    topic.locator("input[type=text]").first.fill("Teamwork under pressure")
    topic.locator("textarea").first.fill("A specific situation\nWhat they did\nThe result")
    page.wait_for_timeout(600)  # validation runs after a short pause
    assert page.is_enabled("#cfgSave")
    page.click("#cfgSave")
    page.wait_for_selector(".toast.ok")
    saved = sorted(p.name for p in (tmp / "packs").glob("job_interview_v1__*.yaml"))
    assert saved, "no new version written"
    page.goto(base + "/")
    page.wait_for_selector("#packSel option", state="attached")
    options = page.locator("#packSel option").all_inner_texts()
    assert any("Job interview" in o and "0.1.1" in o for o in options), options
    assert not w.errors, w.errors
    page.context.close()


def test_info_icon_works_on_tap_and_keyboard(app_url, browser):
    base, _ = app_url
    page, _ = new_page(browser, base)
    page.goto(base + "/config")
    info = page.locator(".field .info").first
    info.wait_for()
    help_id = page.locator(".field .help").first
    assert help_id.is_hidden()
    info.click()
    assert help_id.is_visible() and info.get_attribute("aria-expanded") == "true"
    info.focus()
    page.keyboard.press("Enter")
    assert help_id.is_hidden()
    page.context.close()


def test_leave_warning_only_while_an_interview_runs_and_reload_resumes(app_url, browser):
    base, _ = app_url
    page, _ = new_page(browser, base)
    page.goto(base + "/")
    page.wait_for_selector("#checklist li")
    seen, accept = [], [False]
    page.on("dialog", lambda d: (seen.append(d.type), d.accept() if accept[0] else d.dismiss()))
    page.click(".modes button[data-mode=typed]")
    start(page)
    page.fill("#typedText", "Thanks for coming in today.")
    page.press("#typedText", "Enter")
    page.wait_for_selector("#transcript li.final")
    sid = page.evaluate("sessionStorage.getItem('interview.session')")
    page.click(".nav a[href='/sessions']")  # the person stays: the interview keeps running
    page.wait_for_timeout(500)
    assert seen == ["beforeunload"] and page.url.rstrip("/") == base
    accept[0] = True  # this time the person confirms leaving: the reload resumes the same session
    page.reload()
    page.wait_for_selector("#transcript li.final")
    accept[0] = False
    assert page.evaluate("sessionStorage.getItem('interview.session')") == sid
    assert page.is_disabled("#packSel")  # locked after start
    page.click("#endBtn")
    page.wait_for_selector("#dialog:not([hidden])")
    page.click("#dialogYes")
    page.wait_for_selector("#newBtn:not([hidden])")
    seen.clear()
    page.click(".nav a[href='/sessions']")  # stopped: no warning
    page.wait_for_url("**/sessions")
    assert seen == []
    page.context.close()


def test_nothing_starts_until_start_interview(app_url, browser):
    """Opening the page waits: clock at 00:00, inputs off, the interview type can still change, no leave warning.
    Start interview starts the clock, turns the inputs on, locks the type and shows End interview; a reload keeps it."""
    base, _ = app_url
    page, _ = new_page(browser, base)
    page.goto(base + "/")
    page.wait_for_selector("#checklist li")
    page.wait_for_timeout(1500)
    assert page.inner_text("#elapsed") == "00:00"
    for sel in ("#replayBtn", "#typedText", "#micBtn", "#fileBtn"):
        assert page.is_disabled(sel), sel
    assert page.is_visible("#startBtn") and page.is_hidden("#endBtn") and page.is_enabled("#packSel")
    seen = []
    page.on("dialog", lambda d: (seen.append(d.type), d.accept()))
    page.reload()  # not started: leaving does not ask
    page.wait_for_selector("#checklist li")
    assert seen == []
    start(page)
    assert page.is_hidden("#startBtn") and page.is_disabled("#packSel") and page.is_enabled("#replayBtn")
    page.wait_for_function("document.querySelector('#elapsed').textContent !== '00:00'", timeout=3000)
    page.reload()  # started: asks (accepted here), then resumes as started
    page.wait_for_selector("#endBtn:not([hidden])")
    assert seen == ["beforeunload"] and page.is_enabled("#replayBtn")
    page.context.close()


def test_a_lines_speaker_can_be_corrected_by_tapping_it(app_url, browser):
    base, _ = app_url
    page, _ = new_page(browser, base)
    page.goto(base + "/")
    page.wait_for_selector("#checklist li")
    page.click(".modes button[data-mode=typed]")
    start(page)
    page.click('#typedSpeaker button[data-speaker="worker"]')
    page.fill("#typedText", "When he yells.")
    page.press("#typedText", "Enter")
    page.wait_for_selector("#transcript li.final .who-btn")
    page.click("#transcript li.final .who-btn")
    page.select_option("#transcript li.final select.who-pick", "child")
    page.wait_for_function("(document.querySelector('#transcript li.final .who-btn') || {}).textContent === 'Child'")
    assert not page.locator("#transcript li.selected").count()  # tapping the name does not select the line
    page.reload()  # the correction is kept
    page.wait_for_selector("#transcript li.final .who-btn")
    assert page.inner_text("#transcript li.final .who-btn") == "Child"
    page.context.close()


def test_a_running_interview_keeps_its_pack_when_the_pack_is_edited(app_url, browser):
    base, tmp = app_url
    page, _ = new_page(browser, base)
    page.goto(base + "/")
    page.wait_for_selector("#checklist li")
    page.click(".modes button[data-mode=typed]")
    start(page)
    page.fill("#typedText", "Hello.")
    page.press("#typedText", "Enter")
    page.wait_for_selector("#transcript li.final")
    before = page.locator("#checklist li").count()
    raw = page.evaluate("fetch('/api/packs/cps_interview_v2.yaml/raw').then(r => r.json())")["raw"]
    raw["checklist"].append({"label": "Extra topic", "criteria": ["Something"], "required": True})
    out = page.evaluate("(b) => fetch('/api/packs/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
                        "body: JSON.stringify(b)}).then(r => r.json())",
                        {"base_file": "cps_interview_v2.yaml", "pack": raw, "version": ""})
    assert out["ok"]
    page.reload()
    page.wait_for_selector("#transcript li.final")
    assert page.locator("#checklist li").count() == before  # the running session keeps the version it started with
    page.context.close()


def test_resume_of_an_unknown_session_says_so(app_url, browser):
    base, _ = app_url
    page, _ = new_page(browser, base)
    page.goto(base + "/")
    page.evaluate("sessionStorage.setItem('interview.session', '20990101-000000-ffff')")
    page.reload()
    page.wait_for_selector(".toast.error")
    assert "could not be resumed" in page.inner_text(".toast.error")
    assert page.evaluate("sessionStorage.getItem('interview.session')") != "20990101-000000-ffff"
    page.context.close()


@pytest.mark.parametrize("size", [(1280, 800), (1180, 820)])  # 13-inch laptop; iPad landscape
def test_live_screen_shows_transcript_cards_and_checklist_without_scrolling(app_url, browser, size):
    base, _ = app_url
    page, _ = new_page(browser, base, *size)
    page.goto(base + "/")
    page.wait_for_selector("#checklist li")
    for sel in ("#transcript", "#cues", "#checklist"):
        box = page.locator(sel).bounding_box()
        assert box and box["y"] < size[1] - 60 and box["height"] > 20, (sel, box)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no horizontal scrolling
    assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight + 1")
    page.context.close()
