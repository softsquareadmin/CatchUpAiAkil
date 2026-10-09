import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.analysts import build_analyst
from app.adapters.analysts.llm import LLMAnalyst
from app.adapters.providers.openrouter import OpenRouterClient
from app.config import ROOT, load_settings
from app.cues import CueBoard, Lint, audience_hint, open_required_topics
from app.evidence import validate
from app.ledger import Ledger
from app.main import create_app
from app.models import Cue, Evidence, Flag, GateResult, TopicState, TopicUpdate, Utterance
from app.pack import load_pack
from app.pipeline import Session
from app import report
from tests.fakes import FAKE_PRICES, FakeAnalyst, FakeDecider

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
ITEMS = {c.id: c for c in PACK.checklist}
LINT = Lint(PACK)  # the CPS rules, now in the pack (M7)
lint_question, lint_note, seems_ending = LINT.question, LINT.note, LINT.seems_ending


def child_age_hint(state, transcript):
    return audience_hint(PACK, state, transcript)


def utt(i, speaker, text):
    return Utterance(id=f"u{i:04d}", session_id="s", t_start_ms=i * 1000, t_end_ms=i * 1000 + 900, speaker=speaker,
                     text=text, is_final=True, source="typed")


def settings(**env):
    return load_settings(env_file=None, environ=env)


WINDOW = [
    utt(9, "worker", "It was reported that a man named Dave hit her in the eye with a cup he threw at her."),
    utt(17, "child", "I don't remember what happened. Dave is sometime mean and I am afraid of him."),
    utt(20, "child", "I don't know, he just scares me."),
]


# ---- question lint ----

@pytest.mark.parametrize("q,ok", [
    ("Can you tell me about a time you felt scared?", True),
    ("What happens at home when you feel afraid?", True),
    ("Tell me about the last time he scared you.", True),
    ("Who is at home when you feel scared?", True),
    ("How did you get hurt?", True),                                # "hurt" names no act or cause
    ("Did Dave hit you?", False),                                   # yes/no
    ("Was it Dave who did this?", False),                           # yes/no, names a person
    ("Are you telling me everything?", False),                      # yes/no
    ("He scares you, right?", False),                               # tag question
    ("What happened when Dave hit you?", False),                    # only the worker's report says "hit"
    ("What happened when he threw the cup?", False),                # "threw" only in the worker's words
    ("What makes you think your mom is lying?", False),             # credibility
    ("How did the abuse start?", False),                            # abuse conclusion
    ("Tell me more about that.", True),                             # open statement, no question mark needed
    ("He was angry", False),                                        # not a question
])
def test_lint_question(q, ok):
    assert (lint_question(q, WINDOW) is None) is ok, lint_question(q, WINDOW)


def test_lint_allows_acts_and_names_the_child_said_together():
    w = WINDOW + [utt(30, "child", "Dave hit me with a cup.")]
    assert lint_question("What happened after Dave hit you?", w) is None
    w2 = WINDOW + [utt(30, "child", "Somebody hit me.")]
    assert "names Dave" in lint_question("What happened after Dave hit you?", w2)


def test_lint_note_blocks_conclusions_but_not_reports():
    assert lint_note("Mom said Dave is not abusive; the child said she is afraid of him.") is None
    assert lint_note("The child may be lying.")
    assert lint_note("Abuse occurred at home.")


def test_ending_detection():
    assert seems_ending("Please leave now, I am to upset to continue.")
    assert seems_ending("Thank you for your time today.")
    assert not seems_ending("I consent")
    assert not seems_ending("Dave is my boyfriend and lives here.")


def test_open_required_topics_and_age_hint():
    state = {c.id: TopicState(item_id=c.id) for c in PACK.checklist}
    state["recording_consent"] = TopicState(item_id="recording_consent", status="covered")
    state["child_safety_feelings"] = TopicState(item_id="child_safety_feelings", status="covered", follow_up="required")
    state["child_age_grade"] = TopicState(item_id="child_age_grade", status="covered",
                                          evidence=[Evidence(utterance_id="u0008", quote="I'm seven. I'm in second grade.")])
    ids = [o["id"] for o in open_required_topics(PACK, state)]
    assert "recording_consent" not in ids and "child_age_grade" not in ids
    assert "child_safety_feelings" in ids and "safety_plan" in ids
    hint = child_age_hint(state, {"u0008": utt(8, "child", "I'm seven. I'm in second grade.")})
    assert "I'm seven" in hint
    assert child_age_hint(state, {}) == "unknown"


def test_board_one_active_per_topic_and_cooldown():
    b = CueBoard(60)
    assert b.can_write("t", 0)
    c = b.add(Cue(id="", topic_id="t", kind="follow_up", question_or_note="?", evidence=Evidence(utterance_id="u1", quote="x"),
                  created_at_ms=0), "required")
    assert not b.can_write("t", 0)
    assert b.resolve(c.id, "asked", now_s=100).state == "asked"
    assert not b.can_write("t", 159) and b.can_write("t", 160)
    assert b.resolve(c.id, "dismissed", 200) is None  # already resolved
    assert b.is_new_flag("t", "u1") and not b.is_new_flag("t", "u1")


# ---- session: cue writing, worker actions, consent, ending ----

def make_session(tmp_path, script=None, **env):
    """The suggested-card hold is off unless a test turns it on (its own tests below)."""
    sent = []
    env = {"CUE_HOLD_SUGGESTED": "false", **env}

    async def send(m):
        sent.append(m)

    s = Session(settings(**env), PACK, tmp_path, send, decider=object(), analyst=object())
    s.ledger.prices = FAKE_PRICES
    s.decider = FakeDecider(s.ledger, s.audit, script or {})
    s.analyst = FakeAnalyst(s.ledger, s.audit, question="Can you tell me about a time you felt scared?")
    s.now = 0.0
    s.clock = lambda: s.now
    return s, sent


async def say(s, *lines):
    for i, sp, text in lines:
        await s.on_utterance(utt(i, sp, text))
        await s.gate_idle()
        await s.cue_idle()


def upd(topic, status, follow, uid, quote, reason="why"):
    return [TopicUpdate(item_id=topic, status=status, follow_up=follow, follow_up_reason=reason if follow != "none" else "",
                        evidence=[Evidence(utterance_id=uid, quote=quote)])]


AFRAID = (1, "child", "Dave is sometime mean and I am afraid of him.")
SCARES = (2, "child", "I don't know, he just scares me.")


def test_follow_up_writes_one_card_with_valid_evidence_and_age_hint(tmp_path):
    script = {"u0001": upd("child_safety_feelings", "partial", "required", "u0001", "I am afraid of him"),
              "u0002": upd("child_safety_feelings", "partial", "required", "u0002", "he just scares me")}
    s, sent = make_session(tmp_path, script)
    s.checklist["child_age_grade"] = TopicState(item_id="child_age_grade", status="covered",
                                                evidence=[Evidence(utterance_id="u0001", quote="Dave is sometime mean")])

    async def go():
        await say(s, AFRAID, SCARES)
    asyncio.run(go())
    cues = [m for m in sent if m["type"] == "cue"]
    assert len(cues) == 1  # same level on the next line: no second card
    cue = cues[0]["cue"]
    assert cue["topic_id"] == "child_safety_feelings" and cue["kind"] == "follow_up" and cues[0]["level"] == "required"
    assert validate(Evidence(**cue["evidence"]), {u.id: u for u in s.store.utterances})
    assert "Dave is sometime mean" in s.analyst.calls[0][1]  # age hint passed from the age topic's quotes
    rows = [json.loads(l) for l in (tmp_path / s.id / "ledger.jsonl").read_text().splitlines()]
    assert [r["role"] for r in rows].count("cue") == 1
    assert "ai_output_shown" in (tmp_path / s.id / "audit.jsonl").read_text()


def test_asked_clears_suggested_but_not_required(tmp_path):
    script = {"u0001": upd("child_account", "partial", "suggested", "u0001", "I am afraid of him") +
              upd("child_safety_feelings", "partial", "required", "u0001", "I am afraid of him")}
    s, sent = make_session(tmp_path, script)

    async def go():
        await say(s, AFRAID)
        by_topic = {m["cue"]["topic_id"]: m["cue"]["id"] for m in sent if m["type"] == "cue"}
        assert await s.cue_action(by_topic["child_account"], "asked") is None
        assert await s.cue_action(by_topic["child_safety_feelings"], "asked") is None
        assert await s.cue_action(by_topic["child_safety_feelings"], "asked") == "unknown or inactive card"
    asyncio.run(go())
    assert s.checklist["child_account"].follow_up == "none"
    assert s.checklist["child_safety_feelings"].follow_up == "required"
    audit = (tmp_path / s.id / "audit.jsonl").read_text()
    assert "cue_asked" in audit and "follow_up_cleared" in audit


def test_dismiss_clears_required_and_gate_cannot_reraise_on_old_evidence(tmp_path):
    old = upd("child_safety_feelings", "partial", "required", "u0001", "I am afraid of him")
    script = {"u0001": old, "u0002": old, "u0003": upd("child_safety_feelings", "partial", "required", "u0003", "he yells")}
    s, sent = make_session(tmp_path, script)

    async def go():
        await say(s, AFRAID)
        cue_id = next(m["cue"]["id"] for m in sent if m["type"] == "cue")
        await s.cue_action(cue_id, "dismissed")
        assert s.checklist["child_safety_feelings"].follow_up == "none"
        await say(s, (2, "parent", "Dave is not mean."))           # gate repeats the old evidence: ignored
        assert s.checklist["child_safety_feelings"].follow_up == "none"
        await say(s, (3, "child", "I get scared when he yells."))  # new evidence: allowed again
        assert s.checklist["child_safety_feelings"].follow_up == "required"
        assert len([m for m in sent if m["type"] == "cue"]) == 1     # still cooling down: no new card
        s.now = 61
        await s.on_utterance(utt(4, "worker", "Thank you."))
        await s.gate_idle()
        s.schedule_cues()
        await s.cue_idle()
        assert len([m for m in sent if m["type"] == "cue"]) == 2     # after the cooldown
    asyncio.run(go())


def test_level_change_withdraws_card(tmp_path):
    script = {"u0001": upd("child_safety_feelings", "partial", "required", "u0001", "I am afraid of him"),
              "u0002": upd("child_safety_feelings", "covered", "suggested", "u0002", "When he yells")}
    s, sent = make_session(tmp_path, script)
    asyncio.run(say(s, AFRAID, (2, "child", "When he yells.")))
    cues = [m for m in sent if m["type"] == "cue"]
    withdrawn = [m for m in sent if m["type"] == "cue_withdrawn"]
    assert [c["level"] for c in cues] == ["required", "suggested"]
    assert withdrawn and withdrawn[0]["id"] == cues[0]["cue"]["id"]
    assert s.board.active == {"child_safety_feelings": cues[1]["cue"]["id"]}


def test_failed_cue_is_retried_on_next_line_at_most_twice(tmp_path):
    script = {f"u000{i}": upd("child_safety_feelings", "partial", "required", "u0001", "I am afraid of him") for i in (1, 2, 3)}
    s, sent = make_session(tmp_path, script)
    s.analyst.question = "Did Dave hit you?"  # the fake does not lint; the pipeline's evidence check still runs

    async def bad(*a, **k):
        s.analyst.calls.append(("x", ""))
        return None
    s.analyst.write_cue = bad
    asyncio.run(say(s, AFRAID, (2, "parent", "No."), (3, "parent", "No.")))
    assert len(s.analyst.calls) == 2 and not [m for m in sent if m["type"] == "cue"]  # no 60 s wait, but capped
    assert "cue_skipped" in (tmp_path / s.id / "events.jsonl").read_text()


def test_flags_become_cards_deduped_and_linted(tmp_path):
    s, sent = make_session(tmp_path)
    ev = Evidence(utterance_id="u0001", quote="I am afraid of him")

    async def go():
        await say(s, AFRAID)
        good = Flag(topic_id="child_safety_feelings", note="The child said she is afraid of Dave.", evidence=ev)
        bad = Flag(topic_id="general", note="The child may be lying.", evidence=Evidence(utterance_id="u0001", quote="Dave"))
        for _ in range(2):
            await s.apply_gate_result(GateResult(flags=[good, bad]), s.store.utterances, 1)
    asyncio.run(go())
    flags = [m["cue"] for m in sent if m["type"] == "cue" and m["cue"]["kind"] == "flag"]
    assert [f["question_or_note"] for f in flags] == ["The child said she is afraid of Dave."]
    assert "lint: conclusion or blocked wording" in (tmp_path / s.id / "events.jsonl").read_text()


def test_consent_refusal_prompts_and_stop_needs_worker_confirmation(tmp_path):
    s, sent = make_session(tmp_path)
    flag = Flag(topic_id="recording_consent", note="The parent refused consent.",
                evidence=Evidence(utterance_id="u0001", quote="I do not consent"))

    async def go():
        await say(s, (1, "parent", "No, I do not consent to that."))
        await s.apply_gate_result(GateResult(flags=[flag]), s.store.utterances, 1)
        assert any(m["type"] == "consent_check" for m in sent) and not s.stopped  # waits for the worker
        assert not [m for m in sent if m["type"] == "cue"]                        # not shown as an ordinary flag
        await s.consent_decision(stop=False)
        assert not s.stopped
        await s.consent_decision(stop=True)
        assert s.stopped == "consent refused"
        assert await s.submit_typed("parent", "more") and len(s.store.utterances) == 1
    asyncio.run(go())
    audit = (tmp_path / s.id / "audit.jsonl").read_text()
    for action in ("consent_refusal_detected", "consent_refusal_not_confirmed", "consent_refusal_confirmed", "tool_stopped"):
        assert action in audit
    assert any(m["type"] == "stopped" for m in sent)


def test_ending_prompts_before_you_leave_once(tmp_path):
    s, sent = make_session(tmp_path)

    async def go():
        await say(s, (1, "parent", "Please leave now, I am to upset to continue."), (2, "parent", "Goodbye."))
        await s.cue_idle()
    asyncio.run(go())
    bl = [m for m in sent if m["type"] == "before_leave"]
    assert len(bl) == 1 and bl[0]["reason"] == "ending_detected" and bl[0]["utterance"]["id"] == "u0001"
    assert {o["id"] for o in bl[0]["open"]} == {c.id for c in PACK.checklist}
    assert "before_leave_shown" in (tmp_path / s.id / "audit.jsonl").read_text()


def test_notes_are_saved_and_audited(tmp_path):
    s, sent = make_session(tmp_path)
    assert asyncio.run(s.add_note("  ")) == "empty note"
    asyncio.run(s.add_note("Child looked at the floor."))
    assert sent[-1]["type"] == "note" and sent[-1]["note"]["text"] == "Child looked at the floor."
    assert "Child looked at the floor." in (tmp_path / s.id / "events.jsonl").read_text()
    assert "note_added" in (tmp_path / s.id / "audit.jsonl").read_text()


# ---- LLM cue writer (mocked HTTP) ----

def mocked_analyst(tmp_path, contents):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        c = contents[len(bodies) - 1]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(c)}}],
                                         "usage": {"prompt_tokens": 500, "completion_tokens": 30, "cost": 0.0001}})

    client = OpenRouterClient("k", transport=httpx.MockTransport(handler))
    ledger = Ledger(tmp_path, "s", "x", 1.0, prices=FAKE_PRICES)
    return LLMAnalyst(PACK, client, "openrouter", "anthropic/claude-haiku-5.5", ledger), bodies, ledger


def test_llm_cue_retries_after_leading_question_and_bad_quote(tmp_path):
    good = {"question": "Can you tell me about a time he scared you?", "evidence": {"utterance_id": "u0020", "quote": "he just scares me"}}
    leading = {"question": "Did Dave hit you?", "evidence": {"utterance_id": "u0020", "quote": "he just scares me"}}
    a, bodies, ledger = mocked_analyst(tmp_path, [leading, good])
    st = TopicState(item_id="child_safety_feelings", status="partial", follow_up="required", follow_up_reason="no why or when")
    cue = asyncio.run(a.write_cue(ITEMS["child_safety_feelings"], st, WINDOW, 'Said in the interview: "I\'m seven."'))
    assert cue.question_or_note == good["question"] and cue.kind == "follow_up"
    assert a.last_rejections[0]["reason"].startswith("lint: yes/no")
    assert "previous question was rejected" in bodies[1]["messages"][1]["content"]
    user = bodies[0]["messages"][1]["content"]
    assert "<transcript>" in user and "</transcript>" in user and "I'm seven" in user and "no why or when" in user
    assert bodies[0]["response_format"]["json_schema"]["schema"]["properties"]["evidence"]["properties"]["utterance_id"]["enum"] == \
        ["u0009", "u0017", "u0020"]

    bad_quote = {"question": "What happens when he scares you?", "evidence": {"utterance_id": "u0020", "quote": "Dave hit me"}}
    a2, _, ledger2 = mocked_analyst(tmp_path / "b", [bad_quote, leading])
    assert asyncio.run(a2.write_cue(ITEMS["child_safety_feelings"], st, WINDOW, "unknown")) is None
    assert [r["reason"].split(":")[0] for r in a2.last_rejections] == ["evidence_not_in_transcript", "lint"]
    rows = [json.loads(l) for l in (tmp_path / "b" / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and all(r["role"] == "cue" for r in rows)  # one retry at most


def test_cue_model_switch_needs_only_env():
    s = settings(CUE_MODEL="openrouter:openai/gpt-5.4-mini", OPENROUTER_API_KEY="k")
    analyst, reason = build_analyst(s, PACK, Ledger(ROOT / "sessions" / "_unused", "s", "x", 1.0, prices=FAKE_PRICES))
    assert reason == "" and analyst.model == "openai/gpt-5.4-mini" and analyst.reasoning_effort == "minimal"
    assert build_analyst(settings(CUE_MODEL="fake:template"), PACK, analyst.ledger)[0].__class__.__name__ == "TemplateAnalyst"
    assert "not set" in build_analyst(settings(), PACK, analyst.ledger)[1]


# ---- end to end, offline (keyword gate + template cues) ----

def test_ws_replay_shows_follow_up_card_for_vague_answer_and_before_you_leave(tmp_path):
    app = create_app(settings(GATE_MODEL="fake:keyword", CUE_MODEL="fake:template"), sessions_dir=tmp_path)
    with TestClient(app).websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["cue"]["enabled"]
        ws.send_json({"type": "replay", "speed": 0})
        msgs = []
        while not any(m["type"] == "before_leave" for m in msgs):
            msgs.append(ws.receive_json())
        cue = next(m["cue"] for m in msgs if m["type"] == "cue" and m["cue"]["topic_id"] == "child_safety_feelings")
        ws.send_json({"type": "cue_action", "id": cue["id"], "action": "asked"})
        while (m := ws.receive_json())["type"] != "cue_update":
            pass
        assert m["cue"]["state"] == "asked"
        ws.send_json({"type": "end_confirm"})
        while (m := ws.receive_json())["type"] != "stopped":
            pass
        sid = hello["session_id"]
    transcript = {u["id"]: Utterance(**u) for u in map(json.loads, (tmp_path / sid / "utterances.jsonl").read_text().splitlines())}
    for c in (m["cue"] for m in msgs if m["type"] == "cue"):
        assert validate(Evidence(**c["evidence"]), transcript)
        if c["kind"] == "follow_up":
            assert lint_question(c["question_or_note"], list(transcript.values())) is None
    assert any(m["type"] == "cue" and m["cue"]["kind"] == "flag" for m in msgs)
    bl = next(m for m in msgs if m["type"] == "before_leave")
    assert bl["utterance"]["id"] == "u0023" and bl["open"]
    audit = (tmp_path / sid / "audit.jsonl").read_text()
    assert "cue_asked" in audit and "interview_ended" in audit


def test_ws_consent_refusal_with_typed_input(tmp_path):
    app = create_app(settings(GATE_MODEL="fake:keyword", CUE_MODEL="fake:template"), sessions_dir=tmp_path)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "typed", "speaker": "parent", "text": "No. I do not consent to that."})
        while (m := ws.receive_json())["type"] != "consent_check":
            pass
        assert m["evidence"]["quote"]
        ws.send_json({"type": "consent", "decision": "stop"})
        while (m := ws.receive_json())["type"] != "stopped":
            pass
        assert m["reason"] == "consent refused"
        ws.send_json({"type": "typed", "speaker": "parent", "text": "hello"})
        assert ws.receive_json()["type"] == "error"


# ---- report ----

def test_report_groups_by_experiment_and_role(tmp_path):
    for sid, exp in (("a", "exp1"), ("b", "exp2")):
        led = Ledger(tmp_path / sid, sid, exp, 1.0, prices=FAKE_PRICES)
        led.record("gate", "fake", "fake", input_tokens=1000, output_tokens=100, latency_ms=2000)
        led.record("gate", "fake", "fake", ok=False, error="invalid_json: x", latency_ms=0)
        led.record("cue", "fake", "fake", input_tokens=500, output_tokens=50, latency_ms=900)
        (tmp_path / sid / "utterances.jsonl").write_text(json.dumps(utt(1, "child", "x").model_dump()) + "\n" +
                                                         json.dumps(utt(61, "child", "y").model_dump()) + "\n")
        (tmp_path / sid / "events.jsonl").write_text('{"kind": "cue"}\n{"kind": "rejected", "reason": "no_valid_evidence"}\n')
    rows, totals = report.build(tmp_path)
    assert {(r["experiment"], r["role"]) for r in rows} == {("exp1", "gate"), ("exp1", "cue"), ("exp2", "gate"), ("exp2", "cue")}
    gate = next(r for r in rows if r["experiment"] == "exp1" and r["role"] == "gate")
    assert gate["calls"] == 2 and gate["invalid_json_rate"] == 0.5 and gate["p50_ms"] == 2000
    t = next(t for t in totals if t["experiment"] == "exp1")
    assert t["cues_shown"] == 1 and t["evidence_failures"] == 1 and t["interview_minutes"] == 1.01
    rows1, _ = report.build(tmp_path, "exp2")
    assert {r["experiment"] for r in rows1} == {"exp2"}
    report.main(["--sessions", str(tmp_path), "--csv", str(tmp_path / "out.csv")])
    assert "experiment,role" in (tmp_path / "out.csv").read_text()


def test_gitignore_keeps_secrets_and_sessions_out():
    lines = (ROOT / ".gitignore").read_text().split()
    assert {".env", "sessions/", "audit_logs/"} <= set(lines)


# ---- suggested cards wait for the end of the answer; out-of-date cards are rewritten (owner, 2026-10-08) ----

def test_suggested_card_waits_for_the_users_next_line_but_required_does_not(tmp_path):
    script = {"u0001": upd("child_account", "partial", "suggested", "u0001", "I am afraid of him") +
              upd("child_safety_feelings", "partial", "required", "u0001", "I am afraid of him")}
    s, sent = make_session(tmp_path, script, CUE_HOLD_SUGGESTED="true", CUE_HOLD_S="30")

    async def go():
        await say(s, AFRAID)  # the child is still answering
        assert [m["level"] for m in sent if m["type"] == "cue"] == ["required"]
        await say(s, (2, "worker", "Thank you for telling me."))  # the answer is over
        assert sorted(m["level"] for m in sent if m["type"] == "cue") == ["required", "suggested"]
    asyncio.run(go())


def test_held_card_is_dropped_if_the_rest_of_the_answer_covers_it(tmp_path):
    script = {"u0001": upd("child_account", "partial", "suggested", "u0001", "I am afraid of him"),
              "u0002": upd("child_account", "covered", "none", "u0002", "he just scares me")}
    s, sent = make_session(tmp_path, script, CUE_HOLD_SUGGESTED="true", CUE_HOLD_S="30")
    asyncio.run(say(s, AFRAID, SCARES, (3, "worker", "Thank you.")))
    assert not [m for m in sent if m["type"] == "cue"] and not s.analyst.calls  # never written, no cost


def test_held_card_appears_after_the_silence(tmp_path):
    script = {"u0001": upd("child_account", "partial", "suggested", "u0001", "I am afraid of him")}
    s, sent = make_session(tmp_path, script, CUE_HOLD_SUGGESTED="true", CUE_HOLD_S="0.2")
    s.clock = time.monotonic  # real time for this one: the timer looks again after the hold

    async def go():
        await say(s, AFRAID)
        assert not [m for m in sent if m["type"] == "cue"]
        await asyncio.sleep(0.4)
        await s.cue_idle()
        assert [m["level"] for m in sent if m["type"] == "cue"] == ["suggested"]
    asyncio.run(go())


def test_out_of_date_card_is_rewritten_once_there_is_new_evidence(tmp_path):
    first = upd("child_account", "partial", "suggested", "u0001", "I am afraid of him", reason="what happened")
    script = {"u0001": first,
              "u0002": upd("child_account", "partial", "suggested", "u0001", "I am afraid of him", reason="when, reworded"),
              "u0003": upd("child_account", "partial", "suggested", "u0003", "at the park", reason="who else was there")}
    s, sent = make_session(tmp_path, script)

    async def go():
        await say(s, AFRAID)
        await say(s, (2, "worker", "Thank you."))  # reason reworded, no new evidence: card kept
        assert len([m for m in sent if m["type"] == "cue"]) == 1
        s.analyst.question = "Who else was there?"
        await say(s, (3, "child", "It was at the park."))  # new evidence and a new reason: rewritten
    asyncio.run(go())
    cues = [m["cue"] for m in sent if m["type"] == "cue"]
    assert [c["updated"] for c in cues] == [False, True] and cues[1]["question_or_note"] == "Who else was there?"
    assert [m["id"] for m in sent if m["type"] == "cue_withdrawn"] == [cues[0]["id"]]
    assert s.board.active == {"child_account": cues[1]["id"]}
    assert '"reason": "rewritten"' in (tmp_path / s.id / "events.jsonl").read_text()


def test_rewrite_of_a_suggested_card_waits_for_the_end_of_the_answer(tmp_path):
    script = {"u0001": upd("child_account", "partial", "suggested", "u0001", "I am afraid of him", reason="what"),
              "u0003": upd("child_account", "partial", "suggested", "u0003", "at the park", reason="who else")}
    s, sent = make_session(tmp_path, script, CUE_HOLD_SUGGESTED="true", CUE_HOLD_S="30")

    async def go():
        await say(s, AFRAID, (2, "worker", "Go on."))
        assert len([m for m in sent if m["type"] == "cue"]) == 1
        await say(s, (3, "child", "It was at the park."))
        assert len([m for m in sent if m["type"] == "cue"]) == 1  # the child is still answering
        await say(s, (4, "worker", "Thank you."))
        assert [m["cue"]["updated"] for m in sent if m["type"] == "cue"] == [False, True]
    asyncio.run(go())


def test_a_second_flag_on_the_same_line_shows_only_when_it_names_another_role(tmp_path):
    from app.cues import roles_named
    assert roles_named(PACK, "Child stated, while Mom said Dave is not mean.") == {"child", "parent"}
    s, sent = make_session(tmp_path)
    ev = Evidence(utterance_id="u0001", quote="I am afraid of him")
    notes = ["The child said she is afraid of Dave.",
             "The child stated she is afraid of Dave.",                                  # reworded: dropped
             "The child said she is afraid of Dave, while the parent said he is not mean.",  # names the parent: shown
             "The child said Dave scares her, while the parent disputes it."]           # no new role: dropped

    async def go():
        await say(s, AFRAID)
        for n in notes:
            await s.apply_gate_result(GateResult(flags=[Flag(topic_id="child_safety_feelings", note=n, evidence=ev)]),
                                      s.store.utterances, 1)
    asyncio.run(go())
    assert [m["cue"]["question_or_note"] for m in sent if m["type"] == "cue" and m["cue"]["kind"] == "flag"] == \
        [notes[0], notes[2]]


def test_consent_prompt_still_opens_once_per_line(tmp_path):
    s, sent = make_session(tmp_path)
    ev = Evidence(utterance_id="u0001", quote="I am afraid of him")

    async def go():
        await say(s, AFRAID)
        for n in ("The parent refused consent.", "The child and the parent refused consent."):
            await s.apply_gate_result(GateResult(flags=[Flag(topic_id="recording_consent", note=n, evidence=ev)]),
                                      s.store.utterances, 1)
    asyncio.run(go())
    assert len([m for m in sent if m["type"] == "consent_check"]) == 1
