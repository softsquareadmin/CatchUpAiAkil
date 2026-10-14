"""SPEC-report-readability (2026-10-14): the HTML report's layout. Rendering only; built from a hand-made draft so every
section is present (the offline sample's fake review writes no accounts or next steps)."""
import re

from app.config import ROOT
from app.export import build_report, render_html
from app.pack import check, load_pack

CPS = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
JOB = load_pack(ROOT / "packs" / "job_interview_v1.yaml")


def utt(i, speaker, text):
    return {"id": f"u{i:04d}", "session_id": "s", "t_start_ms": i * 10000, "t_end_ms": i * 10000 + 900,
            "speaker": speaker, "text": text, "is_final": True, "source": "mic"}


LINES = [utt(1, "worker", "The report says Dave hit her with a cup."), utt(2, "parent", "She tripped on the stairs."),
         utt(3, "child", "I am afraid of him."), utt(4, "parent", "I am too upset to continue."),
         utt(5, "worker", "May I use that tool?")]


def ev(i, quote):
    return [{"utterance_id": f"u{i:04d}", "quote": quote}]


def it(iid, section, label, value, evidence=(), basis=(), status="proposed", **kw):
    return {"id": iid, "section": section, "label": label, "value": value, "evidence": list(evidence),
            "basis_topics": list(basis), "status": status, "ai_value": value, "field_id": kw.get("field_id"),
            "topic_id": kw.get("topic_id"), "source": kw.get("source")}


def topic(tid, label, status, evidence=(), follow_up=None):
    return {"topic_id": tid, "label": label, "required": True, "status": status, "live_status": status,
            "review_status": None, "status_reason": "", "status_by": "live", "evidence": list(evidence),
            "summary": {"value": f"About {label}.", "status": "proposed"}, "missing": {"value": ["a detail"], "status": "proposed"},
            "follow_up": {"value": follow_up, "status": "proposed"} if follow_up else None}


def report(pack=CPS, flags=True, **opts):
    topics = [topic("child_safety_feelings", "Child's feelings of safety", "partial", ev(3, "afraid of him"),
                    "Can you tell me about a time you felt scared?"),
              topic("prior_injuries", "Prior or other injuries", "not_covered", (), "Any other marks?"),
              topic("medical_attention", "Medical attention for injury", "covered", ev(2, "tripped on the stairs"))]
    items = [
        it("custom.safety_flags.1", "custom.safety_flags", "Safety Flags", "Jill said she is afraid of Dave.", ev(3, "afraid of him")),
        it("custom.child_direct_statements.1", "custom.child_direct_statements", "Child's Direct Statements",
           "I am afraid of him.", ev(3, "I am afraid of him")),
        it("summary.1", "summary", "Worker said", "The worker asked to use the tool.", ev(5, "May I use that tool?")),
        it("summary.2", "summary", "Parent said", "She tripped.", ev(2, "tripped on the stairs")),
        it("timeline.1", "timeline", "not stated · parent", "She tripped.", ev(2, "tripped")),
        it("accounts.1", "accounts", "How the injury happened · parent", "She tripped on the stairs.",
           ev(2, "tripped on the stairs"), topic_id="parent_account", source="parent"),
        it("accounts.2", "accounts", "How the injury happened · the report", "Dave hit her with a cup.",
           ev(1, "Dave hit her with a cup"), topic_id="parent_account", source="report"),
        it("accounts.3", "accounts", "How the injury happened · child", "She is afraid.", ev(3, "afraid"),
           topic_id="child_account", source="child"),
        it("missing_accounts.1", "missing_accounts", "Account not yet heard from Dave Mercer", "Named in the report.",
           ev(1, "Dave")),
        it("next_steps.1", "next_steps", "Option", "Consider asking about other marks.", (),
           [{"id": "prior_injuries", "label": "Prior or other injuries", "status": "not_covered"}]),   # repeats a topic
        it("next_steps.2", "next_steps", "Option", "Decide whether to continue given the distress.", ev(4, "too upset"),
           [{"id": "prior_injuries", "label": "Prior or other injuries", "status": "not_covered"}]),   # new line: kept
        it("next_steps.3", "next_steps", "Option", "Ask about a time she felt scared.", ev(3, "afraid of him"),
           [{"id": "child_safety_feelings", "label": "Child's feelings of safety", "status": "partial"}]),  # required: kept
        it("next_steps.4", "next_steps", "Option", "Seek an account from Dave Mercer.", ev(1, "Dave"), status="accepted"),
        it("form.1", "form", "Child age", "7", ev(3, "afraid"), field_id="child_age"),  # an old draft's form item
    ]
    titles = [{"id": "summary", "title": "Attributed summary"}, {"id": "form", "title": "Form fields"},
              {"id": "timeline", "title": "Timeline (as reported)"}, {"id": "accounts", "title": "Accounts side by side"},
              {"id": "missing_accounts", "title": "Accounts not yet heard"},
              {"id": "next_steps", "title": "Possible next steps (options for the worker)"},
              {"id": "custom.safety_flags", "title": "Safety Flags"},
              {"id": "custom.child_direct_statements", "title": "Child's Direct Statements"}]
    draft = {"topics": topics, "items": items, "sections": titles, "checklist": {"open_topics": [
        {"id": "child_safety_feelings", "follow_up": "required"}, {"id": "prior_injuries", "follow_up": "none"}]}}
    events = [{"kind": "cue", "cue": {"id": "c001", "topic_id": "general", "kind": "flag", "question_or_note": n,
                                      "evidence": e, "created_at_ms": 0, "state": "active"}}
              for n, e in (("The child said she is afraid.", ev(3, "afraid of him")[0]),           # already listed
                           ("The parent asked to stop.", ev(4, "too upset")[0]))] if flags else []
    data = {"session_id": "20261014-000000-test", "draft": draft, "utterances": LINES, "audit": [], "events": events,
            "ledger": []}
    r = build_report(data, pack, include_unreviewed=True, **opts)
    return r, render_html(r)


def pos(page, needle):
    i = page.find(needle)
    assert i >= 0, needle
    return i


def test_lead_sections_come_right_after_the_coverage_and_live_flags_fold_into_safety_flags():
    r, page = report()
    assert pos(page, "Checklist coverage") < pos(page, 'id="sec-custom.safety_flags" open') < \
        pos(page, 'id="sec-custom.child_direct_statements" open') < pos(page, 'id="topics"')
    safety = page[pos(page, 'id="sec-custom.safety_flags"'):pos(page, 'id="sec-custom.child_direct_statements"')]
    assert "The parent asked to stop." in safety and "raised during the interview" in safety
    assert "The child said she is afraid." not in page  # its line is already a Safety Flags item
    assert "Flags raised during the interview" not in page
    assert "<b>Safety Flags:</b>" not in page and "<b>Option:</b>" not in page  # no lead-in repeating the heading
    assert "<b>Worker said:</b>" in page


def test_a_pack_without_layout_settings_keeps_its_flags_section():
    r, page = report(JOB)
    assert "Flags raised during the interview" in page and r["layout"]["lead"] == []


def test_form_fields_are_not_shown_but_stay_in_the_json():
    r, page = report()
    assert "Form fields" not in page and "form" not in {s["id"] for s in r["custom_and_other_sections"]}
    assert r["form_fields"] and r["form_fields"][0]["field_id"] == "child_age"
    assert "form" not in CPS.review.sections and check(CPS) == []


def test_accounts_are_a_table_with_a_column_per_source_and_the_unheard_person():
    r, page = report()
    table = page[pos(page, '<table class="accounts">'):pos(page, "</table>")]
    heads = re.findall(r'<th scope="col"[^>]*>([^<]*)</th>', table)
    assert heads == ["Topic", "Parent", "Child", "The report", "Dave Mercer"]
    rows = re.findall(r'<th scope="row">([^<]*)</th>', table)
    assert rows == ["How the injury happened"]  # the pack's account group joins parent_account and child_account
    assert table.count("Not yet heard") == 1 and 'data-label="The report"' in table
    assert "Dave Mercer: not yet heard." in page and "Accounts not yet heard" not in page


def test_a_full_quote_appears_in_its_main_place_and_elsewhere_only_as_a_link():
    r, page = report()
    assert page.count("<q>afraid of him</q>") == 2  # main places only: the Safety Flags item and its topic
    steps = page[pos(page, 'id="sec-next_steps"'):]
    steps = steps[:steps.find("</details>")]
    assert '<a href="#item-custom.safety_flags.1"' in steps and '<a href="#flag-2"' in steps  # links to main places
    assert "<q>afraid of him</q>" not in steps and "<q>too upset</q>" not in steps
    assert "<q>Dave</q>" in steps  # u0001 has no main place (accounts only): the quote stays
    summary = page[pos(page, 'id="sec-summary"'):]
    summary = summary[:summary.find("</details>")]
    assert '<a href="#topic-medical_attention"' in summary  # u0002's main place is that topic
    assert "<q>May I use that tool?</q>" in summary  # no main place and no transcript: the quote stays
    r, page = report(transcript=True)
    summary = page[pos(page, 'id="sec-summary"'):]
    summary = summary[:summary.find("</details>")]
    assert '<a href="#u0005"' in summary and "<q>" not in summary  # with the transcript every quote links to its line


def test_topics_are_grouped_required_first_with_the_follow_up_in_the_row():
    r, page = report()
    topics = page[pos(page, 'id="topics"'):]
    assert pos(topics, "Needs follow-up (2)") < pos(topics, "Covered (1)")
    assert '<details class="group" open><summary><h3>Needs follow-up' in topics
    assert '<details class="group"><summary><h3>Covered' in topics
    assert pos(topics, "topic-child_safety_feelings") < pos(topics, "topic-prior_injuries")  # required first
    row = topics[pos(topics, 'id="topic-child_safety_feelings"'):]
    row = row[:row.find("</summary>")]
    assert "Required follow-up" in row and "Can you tell me about a time you felt scared?" in row
    assert '<ul class="items unrev">' in page and 'class="items" class=' not in page  # one class attribute


def test_next_steps_that_only_repeat_a_topic_become_links_and_the_section_counts():
    r, page = report()
    steps = page[pos(page, 'id="sec-next_steps"'):]
    steps = steps[:steps.find("</details>")]
    assert "Consider asking about other marks." not in steps
    assert 'Also see the suggested follow-ups for: <a href="#topic-prior_injuries">' in steps
    for kept in ("Decide whether to continue", "Ask about a time she felt scared", "Seek an account from Dave Mercer"):
        assert kept in steps
    assert "· 3 options" in steps and 'id="sec-next_steps" open' in page  # 4 or fewer: open


def test_collapsed_sections_show_counts_and_a_thin_timeline_is_hidden():
    r, page = report(transcript=True)
    assert 'id="sec-summary"><summary><h2>Attributed summary</h2><span class="count"> · 2 statements' in page
    assert 'id="sec-transcript"><summary><h2>Transcript</h2><span class="count"> · 5 lines' in page
    assert "Timeline (as reported)" not in page  # no event with a stated date
    jump = re.findall(r'<ul class="jump">(.*?)</ul>', page)[0]
    assert re.findall(r'href="#([^"]+)"', jump) == [
        "sec-custom.safety_flags", "topics", "sec-accounts", "sec-next_steps", "sec-transcript"]


def test_one_review_count_line_and_badges_only_for_what_the_user_did():
    r, page = report()
    assert "Not reviewed" not in page
    assert f'1 of {r["reviewed_count"]["total"]} items reviewed.' in page  # next_steps.4 accepted
    assert page.count('<span class="mark">accepted</span>') == 1
    assert "Speaker labels come from automatic transcription" in page  # mic input


def test_print_and_expand_cover_every_section():
    r, page = report()
    assert "details::details-content{content-visibility:visible;display:block}" in page
    assert "querySelectorAll('details')" in page and "beforeprint" in page



def test_an_accepted_option_is_never_turned_into_a_link():
    import copy
    r, _ = report()
    r = copy.deepcopy(r)
    step = next(i for s in r["custom_and_other_sections"] if s["id"] == "next_steps" for i in s["items"]
                if i["id"] == "next_steps.1")
    step["status"] = "accepted"
    assert "Consider asking about other marks." in render_html(r)


def test_the_accounts_link_and_section_without_account_items_or_with_accounts_as_a_lead():
    import copy
    r, _ = report()
    no_accounts = copy.deepcopy(r)
    no_accounts["custom_and_other_sections"] = [s for s in r["custom_and_other_sections"] if s["id"] != "accounts"]
    page = render_html(no_accounts)
    assert 'href="#sec-missing_accounts"' in page and 'id="sec-missing_accounts"' in page
    lead = copy.deepcopy(r)
    lead["layout"]["lead"] = ["accounts", *r["layout"]["lead"]]
    assert render_html(lead).count('id="sec-accounts"') == 1  # always the table, once


def test_form_items_of_an_old_draft_are_not_counted_as_hidden():
    draft = {"topics": [], "items": [it("form.1", "form", "Child age", "7", ev(3, "afraid"), field_id="child_age")],
             "sections": [{"id": "form", "title": "Form fields"}], "checklist": {}}
    r = build_report({"session_id": "x", "draft": draft, "utterances": LINES, "audit": [], "events": [], "ledger": []}, CPS)
    assert r["not_included"] == {"rejected": 0, "not_reviewed": 0} and r["form_fields"] == []
    assert r["reviewed_count"] == {"reviewed": 0, "total": 0}
