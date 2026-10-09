"""M11: checklist coverage score, topic results in the review, the HTML / JSON report and its review states."""
import asyncio
import json
import re
from html.parser import HTMLParser

import pytest

from app.adapters.analysts.fake import TemplateAnalyst
from app.config import ROOT
from app.coverage import coverage
from app.export import build_report, render_html
from app.models import Utterance
from app.pack import load_pack, pack_from_dict
from app.review import apply_topic_action, build_topic_results, review_schema, set_topic_status
from tests.browser import find_browser, render_offline
from tests.make_sample_reports import OUT, fake_session, make, report_for
from tests.test_m7_generalization import CPS_WORDS

CPS = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
JOB = load_pack(ROOT / "packs" / "job_interview_v1.yaml")
TINY = load_pack(ROOT / "tests" / "packs" / "tiny_pack.yaml")


def run(coro):
    return asyncio.run(coro)


def pack_with(topics: list[tuple[str, bool]], show=True):
    return pack_from_dict({"id": "p", "version": "1", "pack_format": 2, "name": "P", "purpose": "Test.",
                           "roles": [{"id": "host", "label": "Host", "kind": "user"},
                                     {"id": "guest", "label": "Guest", "kind": "subject"}],
                           "checklist": [{"id": i, "label": i.title(), "required": req, "criteria": ["x"]} for i, req in topics],
                           "review": {"show_coverage_score": show}})


def utt(i, speaker, text):
    return Utterance(id=f"u{i:04d}", session_id="s", t_start_ms=i * 1000, t_end_ms=i * 1000 + 500, speaker=speaker,
                     text=text, is_final=True, source="typed")


# ---- 11.1 score ----

@pytest.mark.parametrize("statuses,score,counts", [
    ({"a": "covered", "b": "covered"}, 100, (2, 0, 0)),
    ({}, 0, (0, 0, 2)),
    ({"a": "covered", "b": "partial"}, 75, (1, 1, 0)),
    ({"a": "partial", "b": "not_covered"}, 25, (0, 1, 1)),
])
def test_score_table(statuses, score, counts):
    c = coverage(pack_with([("a", True), ("b", True)]), statuses)
    assert c["score"] == score and tuple(c["counts"].values()) == counts and c["required"] == 2
    assert c["label"] == "Checklist coverage" and c["note"] == ""


def test_score_rounds_half_up_and_leaves_optional_and_not_applicable_topics_out():
    p = pack_with([("a", True), ("b", True), ("c", True), ("o", False)])
    c = coverage(p, {"a": "covered", "b": "partial", "c": "not_covered", "o": "not_covered"})
    assert c["score"] == 50 and c["required"] == 3
    assert c["optional"] == [{"id": "o", "label": "O", "status": "not_covered"}]
    assert c["note"] == "1 optional topic is shown with its status but not counted in the score."
    assert coverage(p, {"a": "covered", "b": "covered", "c": "partial"})["score"] == 83  # 83.33
    assert coverage(pack_with([("a", True), ("b", True), ("c", True)]), {"a": "covered", "b": "partial"})["score"] == 50
    na = coverage(p, {"a": "covered", "b": "partial", "c": "not_covered"}, frozenset({"c"}))
    assert na["score"] == 75 and na["not_applicable"] == [{"id": "c", "label": "C"}] and "Not applicable" in na["note"]


def test_no_required_topics_shows_no_score():
    c = coverage(pack_with([("o", False)]), {"o": "covered"})
    assert c["score"] is None and c["required"] == 0 and c["note"].startswith("No required topics")


def test_hidden_score_keeps_counts():
    c = coverage(pack_with([("a", True)], show=False), {"a": "covered"})
    assert c["score"] is None and c["show_score"] is False and c["counts"]["covered"] == 1


# ---- 11.2 topic results ----

T = {u.id: u for u in [utt(1, "interviewer", "Tell me about a hard problem you solved."),
                       utt(2, "candidate", "I rebuilt the stock planner and cut late orders by a third."),
                       utt(3, "candidate", "I led a team of six analysts for a year.")]}


def topic(job_id):
    return next(c for c in JOB.checklist if c.id == job_id)


def test_review_schema_always_asks_for_topic_results():
    for pk in (CPS, JOB, TINY):
        tr = review_schema(pk, [])["properties"]["topic_results"]["items"]
        assert tr["properties"]["topic_id"]["enum"] == [c.id for c in pk.checklist]
        assert set(tr["required"]) == {"topic_id", "status", "status_reason", "summary", "missing",
                                       "suggested_follow_up", "evidence"}


def test_every_topic_gets_a_result_and_bad_parts_are_dropped():
    ps, lead = JOB.checklist[1].id, topic("leadership_and_team_management").id
    raw = {"topic_results": [
        {"topic_id": ps, "status": "covered", "status_reason": "", "summary": "Candidate said they rebuilt the stock planner.",
         "missing": [], "suggested_follow_up": "",
         "evidence": [{"utterance_id": "u0002", "quote": "I rebuilt the stock planner"},
                      {"utterance_id": "u0002", "quote": "and doubled revenue"}]},           # fabricated quote
        {"topic_id": lead, "status": "partial", "status_reason": "", "summary": "The candidate is a liar about the team.",
         "missing": ["How conflicts were handled"], "suggested_follow_up": "Did you enjoy it?",  # closed question
         "evidence": [{"utterance_id": "u0003", "quote": "I led a team of six analysts"}]},
        {"topic_id": "nope", "status": "covered"}]}
    state = {ps: __import__("app.models").models.TopicState(item_id=ps, status="covered")}
    lead_state = __import__("app.models").models.TopicState(item_id=lead, status="partial")
    out, rej = build_topic_results(raw, JOB, T, {**state, lead: lead_state})
    assert [t["topic_id"] for t in out] == [c.id for c in JOB.checklist]
    first = out[1]
    assert first["summary"]["value"].startswith("Candidate said") and len(first["evidence"]) == 1
    second = next(t for t in out if t["topic_id"] == lead)
    assert second["summary"] is None and second["follow_up"] is None and second["missing"]["value"] == ["How conflicts were handled"]
    assert second["evidence"] == [{"utterance_id": "u0003", "quote": "I led a team of six analysts"}]  # card still has its quote
    reasons = {(r["section"], r.get("part"), r["reason"].split(":")[0]) for r in rej}
    assert (f"topic.{ps}", "evidence", "quote_not_in_transcript") in reasons
    assert (f"topic.{lead}", "summary", "lint") in reasons and (f"topic.{lead}", "follow_up", "lint") in reasons
    assert ("topics", None, "unknown_topic") in reasons
    untouched = next(t for t in out if t["topic_id"] == JOB.checklist[0].id)
    assert untouched["status"] == "not_covered" and untouched["summary"] is None


def test_a_quote_inside_another_from_the_same_line_is_shown_once():
    ps = JOB.checklist[1].id
    ev = [{"utterance_id": "u0002", "quote": "I rebuilt the stock planner"},
          {"utterance_id": "u0002", "quote": "I rebuilt the stock planner and cut late orders by a third."},
          {"utterance_id": "u0003", "quote": "I led a team"}]
    raw = {"topic_results": [{"topic_id": ps, "status": "not_covered", "status_reason": "", "summary": "",
                              "missing": [], "suggested_follow_up": "", "evidence": ev}]}
    out, _ = build_topic_results(raw, JOB, T, {})
    assert out[1]["evidence"] == ev[1:]


def test_review_may_change_a_status_only_with_a_reason_and_a_quote():
    ps = JOB.checklist[1].id
    base = {"topic_id": ps, "summary": "", "missing": [], "suggested_follow_up": "", "evidence": []}
    out, rej = build_topic_results({"topic_results": [{**base, "status": "covered", "status_reason": "Answered at u0002."}]},
                                   JOB, T, {})
    assert out[1]["status"] == "not_covered" and rej[0]["reason"] == "status_raised_without_evidence"
    ok = {**base, "status": "partial", "status_reason": "A result was described at u0002.",
          "evidence": [{"utterance_id": "u0002", "quote": "cut late orders by a third"}]}
    out, rej = build_topic_results({"topic_results": [ok]}, JOB, T, {})
    t = out[1]
    assert (t["status"], t["live_status"], t["status_by"]) == ("partial", "not_covered", "review") and not rej
    out, rej = build_topic_results({"topic_results": [{**ok, "status_reason": ""}]}, JOB, T, {})
    assert out[1]["status"] == "not_covered" and rej[0]["reason"] == "status_change_without_reason"


def test_topic_actions_and_status_set_by_the_user():
    t = {"topic_id": "a", "status": "partial", "live_status": "partial", "review_status": None, "status_by": "live",
         "summary": {"value": "S", "ai_value": "S", "status": "proposed"}, "missing": None,
         "follow_up": {"value": "Q?", "ai_value": "Q?", "status": "proposed"}}
    topics = [t]
    assert apply_topic_action(topics, "a", "summary", "edit", "S2")["status"] == "edited"
    assert apply_topic_action(topics, "a", "follow_up", "reject")["status"] == "rejected"
    assert apply_topic_action(topics, "a", "follow_up", "reset") == {"value": "Q?", "ai_value": "Q?", "status": "proposed"}
    assert apply_topic_action(topics, "a", "missing", "accept") == "this topic has no missing"
    assert apply_topic_action(topics, "a", "summary", "edit", " ") == "an edit needs a value"
    assert set_topic_status(topics, "a", "covered")["status_by"] == "user"
    assert set_topic_status(topics, "a", "partial")["status_by"] == "live"
    assert set_topic_status(topics, "a", "done") == "status must be one of: covered, partial, not_covered"


# ---- whole fake runs ----

def test_fake_review_gives_every_topic_a_result_and_logs_a_fabricated_quote(tmp_path, monkeypatch):
    real = TemplateAnalyst.review

    async def with_fabricated(self, transcript, state, case_context=None):
        raw = await real(self, transcript, state, case_context)
        raw["topic_results"][0]["evidence"].append({"utterance_id": transcript[1].id, "quote": "words nobody said"})
        return raw
    monkeypatch.setattr(TemplateAnalyst, "review", with_fabricated)
    lines = json.loads((ROOT / "tests/fixtures/job_interview_sample.json").read_text())
    s = run(fake_session(JOB, lines, tmp_path))
    assert s.review_state == "ready"
    assert [t["topic_id"] for t in s.draft["topics"]] == [c.id for c in JOB.checklist]
    assert s.draft["coverage"]["label"] == "Checklist coverage"
    events = [json.loads(l) for l in (tmp_path / s.id / "events.jsonl").read_text().splitlines()]
    assert any(e["kind"] == "review_rejected" and e["reason"] == "quote_not_in_transcript" for e in events)
    assert all("words nobody said" not in json.dumps(t["evidence"]) for t in s.draft["topics"])


def test_status_change_by_user_is_audited_and_marked_in_the_export(tmp_path):
    lines = json.loads((ROOT / "tests/fixtures/job_interview_sample.json").read_text())

    async def go():
        s = await fake_session(JOB, lines, tmp_path)
        tid = s.draft["topics"][0]["topic_id"]
        before = s.draft["coverage"]["score"]
        await s.topic_status(tid, "covered")
        assert s.draft["coverage"]["score"] > before
        assert await s.topic_status(tid, "done") is not None
        await s.close()
        return s, tid
    s, tid = run(go())
    audit = [json.loads(l) for l in (tmp_path / s.id / "audit.jsonl").read_text().splitlines()]
    row = next(a for a in audit if a["action"] == "topic_status_changed_by_user")
    assert row["details"] == {"topic_id": tid, "old": "not_covered", "new": "covered", "ai_status": "not_covered"}
    from app.export import load_session
    page = render_html(build_report(load_session(tmp_path / s.id), JOB))
    assert "set by interviewer" in page and "the AI status was Not covered" in page


# ---- 11.4 export ----

def test_sample_reports_match_the_generator_byte_for_byte():
    """Golden files: docs/sample_reports/*.html (regenerate with python -m tests.make_sample_reports)."""
    for name in ("cps_replay_sample", "job_interview_sample"):
        _, page = run(make(name))
        assert page == (OUT / f"{name}.html").read_text(encoding="utf-8"), f"{name}: rerun tests.make_sample_reports"
        assert "Sample, fictional data" in page and len(page.encode()) < 200_000


class Attrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls, self.tags, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.urls += [v for k, v in attrs if k in ("src", "href", "action", "srcset", "poster", "data") and v]

    def handle_data(self, data):
        self.text.append(data)


def test_report_makes_no_external_requests():
    _, page = run(make("cps_replay_sample", transcript=True))
    p = Attrs()
    p.feed(page)
    assert p.urls and all(u.startswith("#") for u in p.urls)  # only links into the transcript appendix
    css = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    assert "@import" not in css and "url(" not in css
    assert p.tags.count("script") == 1 and "src=" not in re.search(r"<script[^>]*>", page).group(0)
    assert "<link" not in page and "<img" not in page and "<iframe" not in page


def test_everything_from_the_transcript_and_model_is_escaped():
    nasty = '<script>alert("x")</script> & "quoted" \'single\' ' + "x" * 300
    lines = [{"speaker": "host", "text": "Which plants are you growing?", "t_ms": 1000, "t_end_ms": 2000},
             {"speaker": "guest", "text": f"My plants are tomatoes. {nasty}", "t_ms": 3000, "t_end_ms": 4000}]

    async def go(tmp):
        s = await fake_session(TINY, lines, tmp)
        await s.add_note(nasty)
        for i in s.draft["items"]:
            await s.review_action(i["id"], "accept")
        for t in s.draft["topics"]:
            if t["summary"]:
                await s.topic_action(t["topic_id"], "summary", "accept")
        await s.close()
        return s
    import tempfile
    from pathlib import Path

    from app.export import load_session
    with tempfile.TemporaryDirectory() as tmp:
        s = run(go(Path(tmp)))
        report = build_report(load_session(Path(tmp) / s.id), TINY, transcript=True, reviewer='<b>Ann</b> & "Co"')
    page = render_html(report)
    assert "<script>alert" not in page and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;quoted&quot;" in page
    assert "&lt;b&gt;Ann&lt;/b&gt; &amp; &quot;Co&quot;" in page
    assert page.count("<script>") == 1  # the optional expand-all script only
    assert "overflow-wrap:anywhere" in page  # long unbroken strings wrap


@pytest.mark.skipif(find_browser() is None, reason="no Chrome or Chromium installed")
def test_report_opens_offline_in_a_headless_browser(tmp_path):
    page = OUT / "job_interview_sample.html"
    dom = render_offline(page, tmp_path / "shot.png")
    assert (tmp_path / "shot.png").stat().st_size > 10_000
    assert "Checklist coverage" in dom and re.search(r"<strong>\d+%</strong>", dom)
    assert "Collapse all" in dom or "Expand all" in dom  # the optional script ran


def test_review_states_in_the_export():
    reviewed, page = run(report_for("packs/job_interview_v1.yaml", "tests/fixtures/job_interview_sample.json", "x"))
    assert reviewed["not_included"]["rejected"] == 1 and "1 item rejected by the interviewer, not shown." in page
    assert ">edited<" in page and "What happened next, in your own words?" in page
    fresh, page = run(report_for("packs/job_interview_v1.yaml", "tests/fixtures/job_interview_sample.json", "x",
                                 review=False))
    assert fresh["not_included"]["not_reviewed"] > 0 and "not reviewed, not shown." in page
    assert not fresh["custom_and_other_sections"] and all(t["summary"] is None for t in fresh["topics"])
    assert "Not reviewed" not in page and "Not yet reviewed by the interviewer. Not a decision." in page
    incl, page = run(report_for("packs/job_interview_v1.yaml", "tests/fixtures/job_interview_sample.json", "x",
                                review=False, include_unreviewed=True))
    assert incl["not_included"]["not_reviewed"] == 0 and incl["custom_and_other_sections"]
    assert 'class="unrev"' in page and ">Not reviewed<" in page
    assert all(i["status"] == "proposed" for s in incl["custom_and_other_sections"] for i in s["items"])


def test_overall_assessment_only_when_the_pack_allows_it():
    job, page = run(report_for("packs/job_interview_v1.yaml", "tests/fixtures/job_interview_sample.json", "x"))
    assert job["overall_assessment"]["label"] == "Draft for human review" and "Draft for human review" in page
    cps, page = run(report_for("packs/cps_interview_v2.yaml", "tests/fixtures/script_v2.json", "x"))
    assert cps["overall_assessment"] is None and "Draft for human review" not in page
    assert "Draft for human review" not in json.dumps(cps)


def test_hidden_score_shows_no_number_on_screen_html_or_json(tmp_path):
    hidden = TINY.model_copy(update={"review": TINY.review.model_copy(update={"show_coverage_score": False})})
    lines = json.loads((ROOT / "tests/fixtures/tiny_sample.json").read_text())

    async def go():
        s = await fake_session(hidden, lines, tmp_path)
        await s.close()
        return s
    s = run(go())
    assert s.draft["coverage"]["score"] is None and s.pack_info()["show_coverage_score"] is False
    from app.export import load_session
    report = build_report(load_session(tmp_path / s.id), hidden)
    page = render_html(report)
    assert report["coverage"]["score"] is None and report["coverage"]["counts"]
    body = page.split("</style>", 1)[1]
    assert 'class="ring"' not in body and not re.search(r"\d+%", body) and "Required topics" in body


def test_tiny_pack_report_uses_its_own_labels_and_no_cps_wording():
    report, page = run(report_for("tests/packs/tiny_pack.yaml", "tests/fixtures/tiny_sample.json", "x",
                                  transcript=True, include_unreviewed=True))
    assert "Coordinator (user)" in page and "New member" in page
    assert not CPS_WORDS.search(page) and not CPS_WORDS.search(json.dumps(report))


def test_ws_report_viewed_exported_and_options_audited(tmp_path):
    from tests.test_m7_generalization import run_ws
    client, hello, msgs = run_ws(tmp_path, pack_query="job_interview_v1.yaml")
    sid = hello["session_id"]
    assert hello["pack"]["show_coverage_score"] is True
    draft = msgs[-1]["review"]["draft"]
    assert draft["topics"] and draft["coverage"]["label"] == "Checklist coverage"
    r = client.get(f"/api/sessions/{sid}/export.html?unreviewed=true&transcript=true&reviewer=Sam")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "Not yet reviewed by Sam." in r.text and 'id="u0001"' in r.text and (tmp_path / sid / "report.html").exists()
    js = client.get(f"/api/sessions/{sid}/export.json?cost=true").json()
    assert js["report_format_version"] == 1 and "cost_usd" in js and "transcript" not in js
    view = client.get(f"/api/sessions/{sid}/export.html?view=true")
    assert "content-disposition" not in view.headers
    audit = client.get(f"/api/sessions/{sid}/audit").json()
    rows = [a for a in audit if a["action"] in ("report_exported", "report_viewed")]
    assert [a["action"] for a in rows] == ["report_exported", "report_exported", "report_viewed"]
    assert rows[0]["details"]["included_unreviewed"] and rows[0]["details"]["transcript_appendix"]
    assert rows[1]["details"]["cost"] and not rows[1]["details"]["transcript_appendix"]
    texts = [json.loads(l)["text"] for l in (tmp_path / sid / "utterances.jsonl").read_text().splitlines()]
    blob = json.dumps(audit)
    assert not any(t[:40] in blob for t in texts)  # ids and counts only, no transcript content


# ---- items about what was not said (owner, 2026-10-08): a quote or the checklist status, two separate checks ----

def test_walk_me_through_is_an_open_question():
    from app.cues import Lint
    lint = Lint(JOB)
    assert lint.question("Can you walk me through a time when coordinating the two interns was difficult?", []) is None
    assert lint.question("Walk me through how you planned the migration.", []) is None
    assert lint.question("Can you confirm you led the team?", []) is not None  # still closed


def test_an_item_about_what_was_not_said_stands_on_the_checklist_status():
    from app.review import build_items
    weak, ethics, bg = "weakness_and_professional_improvement", "handling_ethical_dilemmas", "professional_background_and_experience"
    statuses = {weak: "not_covered", ethics: "partial", bg: "covered"}
    raw = {"next_steps": [
        {"option": "Consider asking about a professional weakness.", "topic_ids": [weak], "evidence": []},       # kept
        {"option": "Consider asking about their background.", "topic_ids": [bg], "evidence": []},              # covered
        {"option": "Consider asking more.", "topic_ids": [], "evidence": []},                                   # no topic
        {"option": "Consider asking about ethics.", "topic_ids": [ethics],
         "evidence": [{"utterance_id": "u0002", "quote": "words nobody said"}]}],                               # bad quote
        "custom": [{"section": "questions_not_yet_asked", "value": "Ethical dilemmas were not asked about.",
                    "topic_ids": [ethics], "evidence": []}]}
    items, rej = build_items(raw, JOB, T, statuses)
    kept = {i.value: i for i in items}
    assert kept["Consider asking about a professional weakness."].basis_topics == [
        {"id": weak, "label": topic(weak).label, "status": "not_covered"}]
    assert kept["Ethical dilemmas were not asked about."].evidence == []
    assert sorted(r["reason"] for r in rej) == ["no_quote_and_no_topics", "no_quote_and_topic_covered", "no_valid_evidence"]
    items, rej = build_items(raw, JOB, T)  # without statuses every item needs a quote
    assert not items and {r["reason"] for r in rej} == {"no_valid_evidence"}


def test_overall_assessment_gaps_may_rest_on_the_checklist_but_strengths_need_quotes():
    from app.review import build_items
    weak, ps = "weakness_and_professional_improvement", JOB.checklist[1].id
    strength = {"topic_id": ps, "point": "Described rebuilding the stock planner.",
                "evidence": [{"utterance_id": "u0002", "quote": "I rebuilt the stock planner"}]}
    gap = {"topic_id": weak, "point": "A professional weakness was not discussed.", "evidence": []}
    draft = {"text": "Draft: one problem-solving example given; weakness not yet discussed.", "evidence": []}
    oa = {"strengths": [strength], "gaps": [gap], "draft": draft}
    items, rej = build_items({"overall_assessment": oa}, JOB, T, {weak: "not_covered", ps: "covered"})
    assert len(items) == 3 and not rej
    assert items[1].basis_topics[0]["id"] == weak and {t["id"] for t in items[2].basis_topics} == {ps, weak}
    _, rej = build_items({"overall_assessment": oa}, JOB, T, {weak: "covered", ps: "covered"})
    assert rej[0]["reason"] == "missing_evidence"  # the gap's topic is covered, so it needs a quote
    _, rej = build_items({"overall_assessment": {**oa, "strengths": [{**strength, "evidence": []}]}}, JOB, T,
                         {weak: "not_covered", ps: "covered"})
    assert rej[0]["reason"] == "missing_evidence"
    page = render_html(build_report({"session_id": "s", "draft": {"items": [
        {**i.model_dump(), "status": "accepted"} for i in items], "checklist": {}},
        "utterances": [u.model_dump() for u in T.values()], "audit": [], "events": [], "ledger": []}, JOB))
    assert f"Based on the checklist: {topic(weak).label} (not covered)" in page


def test_checklist_basis_and_quotes_are_shown_together_with_topic_quotes_as_a_fallback():
    from app.review import build_items
    lead, weak = "leadership_and_team_management", "weakness_and_professional_improvement"
    statuses = {lead: "partial", weak: "not_covered"}
    topic_quotes = {lead: [{"utterance_id": "u0003", "quote": "I led a team of six analysts"}]}
    raw = {"next_steps": [
        {"option": "Consider asking how the team was run.", "topic_ids": [lead],
         "evidence": [{"utterance_id": "u0003", "quote": "for a year"}]},                     # quoted: both shown
        {"option": "Consider asking about conflicts in the team.", "topic_ids": [lead], "evidence": []},  # topic quote added
        {"option": "Consider asking about a weakness.", "topic_ids": [weak], "evidence": []}]}           # checklist only
    items, rej = build_items(raw, JOB, T, statuses, topic_quotes)
    assert not rej
    quoted, attached, bare = items
    assert [e.quote for e in quoted.evidence] == ["for a year"] and quoted.basis_topics[0]["status"] == "partial"
    assert [e.quote for e in attached.evidence] == ["I led a team of six analysts"] and attached.basis_topics
    assert bare.evidence == [] and bare.basis_topics[0]["id"] == weak


# ---- one retry for sections that came back empty after the checks (owner, 2026-10-08) ----

def test_lost_sections_and_the_retry_schema():
    from app.review import build_items, lost_sections, retry_note
    raw = {"custom": [{"section": "summary", "value": "Candidate described their degree.", "topic_ids": [], "evidence": []},
                      {"section": "important_facts", "value": "Candidate said they rebuilt the stock planner.",
                       "topic_ids": [], "evidence": [{"utterance_id": "u0002", "quote": "I rebuilt the stock planner"}]}],
           "next_steps": []}
    items, rej = build_items(raw, JOB, T, {})
    lost = lost_sections(JOB, raw, items, rej)
    assert lost == {"custom.summary": ["no_quote_and_no_topics"]}  # empty next_steps was never written: not lost
    assert "custom.summary: no_quote_and_no_topics" in retry_note(lost)
    schema = review_schema(JOB, [], only=list(lost))
    assert set(schema["properties"]) == {"custom"}
    assert schema["properties"]["custom"]["items"]["properties"]["section"]["enum"] == ["summary"]
    assert set(review_schema(JOB, [], only=["overall_assessment", "next_steps"])["properties"]) == {
        "overall_assessment", "next_steps"}


@pytest.mark.parametrize("second_try_quotes,expected", [(True, "recovered"), (False, "empty")])
def test_a_section_lost_to_the_checks_is_asked_for_once_more(tmp_path, monkeypatch, second_try_quotes, expected):
    real, calls = TemplateAnalyst.review, []

    async def flaky(self, transcript, state, case_context=None, only=None, note=""):
        calls.append((only, note))
        answer = next(u for u in transcript if u.speaker == "candidate")
        ev = [{"utterance_id": answer.id, "quote": answer.text}] if (only and second_try_quotes) else []
        summary = [{"section": "summary", "value": "Candidate gave their background.", "topic_ids": [], "evidence": ev}]
        if only:
            return {"custom": summary}
        raw = await real(self, transcript, state, case_context)
        raw["custom"] = [e for e in (raw.get("custom") or []) if e.get("section") != "summary"] + summary \
            if isinstance(raw.get("custom"), list) else summary
        return raw
    monkeypatch.setattr(TemplateAnalyst, "review", flaky)
    lines = json.loads((ROOT / "tests/fixtures/job_interview_sample.json").read_text())
    s = run(fake_session(JOB, lines, tmp_path))
    assert len(calls) == 2 and calls[1][0] == ["custom.summary"] and "<retry>" in calls[1][1]
    assert s.draft["retried_sections"] == {"custom.summary": expected}
    got = [i for i in s.draft["items"] if i["section"] == "custom.summary"]
    assert bool(got) == second_try_quotes
    kinds = [json.loads(l)["kind"] for l in (tmp_path / s.id / "events.jsonl").read_text().splitlines()]
    assert "review_retry" in kinds and kinds.count("review_raw") == 2


def test_no_retry_when_nothing_was_lost(tmp_path, monkeypatch):
    real, calls = TemplateAnalyst.review, []

    async def counting(self, *a, **kw):
        calls.append(kw.get("only"))
        return await real(self, *a, **kw)
    monkeypatch.setattr(TemplateAnalyst, "review", counting)
    s = run(fake_session(JOB, json.loads((ROOT / "tests/fixtures/job_interview_sample.json").read_text()), tmp_path))
    assert calls == [None] and s.draft["retried_sections"] == {}
