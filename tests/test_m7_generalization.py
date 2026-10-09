import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.adapters.analysts.llm import LLMAnalyst
from app.adapters.deciders.llm import LLMDecider
from app.config import ROOT, load_settings
from app.cues import Lint
from app.evidence import normalise, validate
from app.import_template import TemplateError, convert, main as import_main
from app.main import create_app
from app.models import Evidence, Utterance
from app.pack import PackError, load_pack, pack_from_dict
from app.review import build_items, review_messages, review_schema

CPS = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
JOB = load_pack(ROOT / "packs" / "job_interview_v1.yaml")
TINY_FILE = ROOT / "tests" / "packs" / "tiny_pack.yaml"
TINY = load_pack(TINY_FILE)
TEMPLATES = ROOT / "tests" / "catchup_templates"
CPS_WORDS = re.compile(r"\b(parent|parents|child|children|worker|caseworker|abuse|cps|mom|jill|dave)\b", re.I)


def utt(i, speaker, text):
    return Utterance(id=f"u{i:04d}", session_id="s", t_start_ms=i, t_end_ms=i, speaker=speaker, text=text,
                     is_final=True, source="typed")


# ---- the CPS lint moved into the pack rejects exactly what the old code rejected ----

OLD_CLOSED = re.compile(  # app/cues.py before M7, copied unchanged
    r"^(did|didn'?t|does|doesn'?t|do|don'?t|is|isn'?t|was|wasn'?t|were|weren'?t|are|aren'?t|has|hasn'?t|have|"
    r"haven'?t|had|will|would|should|shall|could(?! you (tell|describe|help|say|share|show|remember|think of))|"
    r"can(?! you (tell|describe|help|say|share|show|remember|think of)))\b", re.I)
OLD_OPEN = re.compile(r"^(tell me|describe|help me understand)\b", re.I)
OLD_TAG = re.compile(r"(,\s*(right|correct|yes|no|ok|okay)|isn'?t (it|that|he|she)|wasn'?t (it|he|she)|"
                     r"didn'?t (he|she|they|you)|don'?t you think)\s*\?\s*$", re.I)
OLD_HARM = re.compile(r"\b(hit|hits|hitting|beat|beats|beating|punch\w*|kick\w*|push\w*|shov\w*|"
                      r"threw|throw\w*|slap\w*|grab\w*|chok\w*|smack\w*|whip\w*|spank\w*|touch\w*|abus\w*)\b")
OLD_JUDGE_Q = re.compile(r"\b(lie|lies|lied|lying|liar|credib\w*|believable|truthful|truth|honest\w*|"
                         r"made (it|that|this) up|making (it|that|this) up|exaggerat\w*|maltreat\w*|abus\w*|neglect\w*)\b")
OLD_JUDGE_N = re.compile(r"\b(lie|lies|lied|lying|liar|credib\w*|believable|truthful|made (it|that|this) up|"
                         r"making (it|that|this) up|exaggerat\w*|(abuse|maltreatment|neglect) (occurred|happened|took place)|"
                         r"was (abused|maltreated|neglected))\b")
OLD_ENDING = re.compile(r"\b(please leave|leave now|you (should|need to|have to) (go|leave)|good ?bye|bye|we are done|"
                        r"were done|thats all|end this|stop (this|the) interview|to continue|wrap (this )?up|"
                        r"thank you for your time|i (have|need) to go)\b")


def old_question(q, window):
    q = q.strip()
    if not q or not (q.endswith("?") or OLD_OPEN.match(q)) or OLD_CLOSED.match(q) or OLD_TAG.search(q):
        return "x"
    low = normalise(q)
    if OLD_JUDGE_Q.search(low):
        return "x"
    said = [normalise(u.text) for u in window if u.speaker in ("parent", "child")]
    for m in OLD_HARM.finditer(low):
        if not any(re.search(rf"\b{re.escape(m.group(0))}\b", s) for s in said):
            return "x"
        for name in [w for w in re.findall(r"[A-Za-z][\w']*", q)[1:] if w[0].isupper() and w not in ("I", "I'm", "I've", "I'd")]:
            n = normalise(name)
            if not any(re.search(rf"\b{re.escape(n)}\b", s) and re.search(rf"\b{re.escape(m.group(0))}\b", s) for s in said):
                return "x"
    return None


WINDOW = [utt(1, "worker", "It was reported that a man named Dave hit her in the eye with a cup he threw at her."),
          utt(2, "child", "I don't remember what happened. Dave is sometime mean and I am afraid of him."),
          utt(3, "child", "Dave hit me with a cup."), utt(4, "parent", "She was touching the stove.")]
QUESTIONS = [
    "Can you tell me about a time you felt scared?", "What happens at home when you feel afraid?",
    "Tell me about the last time he scared you.", "Who is at home when you feel scared?", "How did you get hurt?",
    "Did Dave hit you?", "Was it Dave who did this?", "Are you telling me everything?", "He scares you, right?",
    "What happened when Dave hit you?", "What happened when he threw the cup?", "What happened after he hit you?",
    "What makes you think your mom is lying?", "How did the abuse start?", "Tell me more about that.", "He was angry",
    "What did Dave do after he pushed you?", "When were you touching the stove?", "What is the truth about that day?",
    "Could you describe the kitchen?", "Could Dave have done it?", "Can you say more about that?",
    "Isn't it true that he yells?", "What did you see, didn't you?", "Who made it up?", "How honest was that?",
    "What happened when Mom grabbed you?", "Tell me what happened when he kicked the door.", "Describe your room.",
    "What do you exaggerate about?", "What does neglect mean to you?", "Who touched you?",
]
NOTES = ["Mom said Dave is not abusive; the child said she is afraid of him.", "The child may be lying.",
         "Abuse occurred at home.", "She was abused.", "Jill said she made it up.", "The parent seems credible.",
         "Mom said Jill fell on the stairs.", "The child exaggerated.", "Neglect happened before.", "Dave was not interviewed."]
ENDINGS = ["Please leave now, I am to upset to continue.", "Thank you for your time today.", "I consent",
           "Dave is my boyfriend and lives here.", "Goodbye.", "We're done here.", "I have to go now.", "Bye!",
           "You need to leave.", "Let's wrap this up.", "That's all.", "Stop the interview please."]


@pytest.mark.parametrize("q", QUESTIONS)
def test_cps_question_lint_unchanged(q):
    assert (Lint(CPS).question(q, WINDOW) is None) == (old_question(q, WINDOW) is None)


@pytest.mark.parametrize("note", NOTES)
def test_cps_note_lint_unchanged(note):
    assert (Lint(CPS).note(note) is None) == (OLD_JUDGE_N.search(normalise(note)) is None)


@pytest.mark.parametrize("line", ENDINGS)
def test_ending_phrases_unchanged(line):
    assert Lint(CPS).seems_ending(line) == bool(OLD_ENDING.search(normalise(line)))


def test_cps_pack_adds_placement_rules_and_shipped_packs_keep_recommendation_off():
    lint = Lint(CPS)
    assert lint.question("Who should have custody of you?", WINDOW)
    assert lint.note("Jill should be removed from the home.") and lint.note("She must be placed in foster care.")
    for f in ("cps_interview_v1.yaml", "cps_interview_v2.yaml", "cps_interview_v2_simple.yaml"):
        assert not load_pack(ROOT / "packs" / f).rails.allow_overall_recommendation
    assert JOB.rails.allow_overall_recommendation


def test_generic_lint_for_a_pack_without_guardrails():
    lint = Lint(TINY)
    assert TINY.guardrails is None
    assert lint.question("Did you water them?", []) and lint.question("You like beans, right?", [])
    assert lint.question("Why are you lying about the beans?", [])
    assert lint.question("What would make you a strong candidate here?", [])  # verdicts are generic
    assert lint.question("How often do you visit the plot?", []) is None
    assert lint.question("What did the abuse look like?", []) is None  # a CPS word is not a generic rule
    assert lint.note("She is a strong candidate.") and lint.note("He is lying.")


def test_job_lint_blocks_protected_characteristics():
    lint = Lint(JOB)
    for q in ("How old are you?", "What year of birth should I note?", "Do you have children?",
              "Which church do you attend?", "What are your family plans?", "Where is your accent from?"):
        assert lint.question(q, []), q
    assert lint.question("What did you change after the migration failed?", []) is None
    assert lint.note("She is young and energetic, aged about 25.")


# ---- prompts ----

def test_gate_prompts_take_domain_rules_from_the_pack():
    cps = LLMDecider(CPS, None, "openrouter", "m", None)
    assert "the child mentions feeling afraid or unsafe without saying why or when" in cps.system
    assert "Never state or imply whether abuse, maltreatment or neglect occurred." in cps.system
    assert json.loads(cps.checklist_json)[6]["definition"] == CPS.checklist[6].definition  # unchanged text
    tiny = LLMDecider(TINY, None, "openrouter", "m", None)
    assert not CPS_WORDS.search(tiny.system + tiny.checklist_json)
    assert "Complete when all of: How often the member can help; Which days suit them." in tiny.checklist_json
    assert "Follow-up required when: The member gives no days or times at all" in tiny.checklist_json
    assert "vague, evasive, incomplete" in tiny.system and "the new member" in tiny.system
    job = LLMDecider(JOB, None, "openrouter", "m", None)
    assert not CPS_WORDS.search(job.system + job.checklist_json)
    assert "Never ask about, infer or use protected characteristics" in job.system


def test_cue_prompt_audience_block_only_with_a_hint_topic():
    cps, job = LLMAnalyst(CPS, None, "openrouter", "m", None), LLMAnalyst(JOB, None, "openrouter", "m", None)
    assert "<audience>" in cps.system and "child's age" in cps.system
    assert "<audience>" not in job.system and not CPS_WORDS.search(job.system)
    st = TINY.checklist[0]
    msgs = LLMAnalyst(TINY, None, "openrouter", "m", None).messages(st, __import__("app.models").models.TopicState(item_id=st.id), [], None)
    assert "<audience>" not in msgs[1]["content"]


def test_review_prompt_and_schema_follow_pack_sections():
    t = [utt(1, "interviewer", "Tell me about a hard problem."), utt(2, "candidate", "I wrote a script to flag duplicates.")]
    sys_job = review_messages(JOB, t, {}, {})[0]["content"]
    assert not CPS_WORDS.search(sys_job) and "custom.important_facts" in sys_job and "overall_assessment" in sys_job
    schema = review_schema(JOB, ["u0001", "u0002"])
    assert set(schema["properties"]) == {"topic_results", "summary", "next_steps", "custom", "overall_assessment"}
    assert schema["properties"]["custom"]["items"]["properties"]["section"]["enum"][:2] == ["summary", "important_facts"]
    assert schema["properties"]["summary"]["items"]["properties"]["speaker"]["enum"] == ["interviewer", "candidate"]
    sys_cps = review_messages(CPS, [utt(1, "worker", "hi")], {}, {})[0]["content"]
    assert "Put every account of how the injury happened under topic_id \"parent_account\"" in sys_cps
    assert "overall_assessment" not in json.dumps(review_schema(CPS, ["u0001"]))


# ---- review items: custom sections and the overall assessment ----

JT = {u.id: u for u in [utt(1, "interviewer", "Tell me about a hard problem."),
                         utt(2, "candidate", "The stock migration kept failing overnight."),
                         utt(3, "candidate", "I wrote a script to flag duplicate codes."),
                         utt(4, "candidate", "Not formally. I sometimes coordinated two interns.")]}


def ev(uid, quote):
    return [{"utterance_id": uid, "quote": quote}]


def job_raw(**over):
    raw = {"summary": [{"speaker": "candidate", "text": "The candidate said the migration kept failing.",
                        "evidence": ev("u0002", "migration kept failing")}],
           "next_steps": [{"option": "Consider asking what the result was.", "evidence": ev("u0003", "flag duplicate codes")}],
           "custom": {"summary": {"value": "A migration problem was described.", "evidence": ev("u0002", "migration")},
                      "important_facts": [{"value": "Wrote a script.", "evidence": ev("u0003", "wrote a script")}],
                      "questions_not_yet_asked": [], "questions_needing_clarification": [],
                      "recommended_next_action": {"value": "Ask about leadership.", "evidence": ev("u0004", "Not formally")}},
           "overall_assessment": {
               "strengths": [{"topic_id": "problem_solving_and_measurable_results", "point": "Described own actions.",
                              "evidence": ev("u0003", "I wrote a script")}],
               "gaps": [{"topic_id": "leadership_and_team_management", "point": "No formal leadership example.",
                         "evidence": ev("u0004", "Not formally")}],
               "draft": {"text": "Draft: a strong candidate for problem solving; leadership not shown. To be verified.",
                         "evidence": ev("u0003", "I wrote a script")}}}
    raw.update(over)
    return raw


def test_job_review_builds_custom_and_overall_items_with_evidence():
    items, rejected = build_items(job_raw(), JOB, JT)
    secs = [i.section for i in items]
    assert "custom.important_facts" in secs and "custom.recommended_next_action" in secs
    oa = [i for i in items if i.section == "overall_assessment"]
    assert [i.label for i in oa] == ["Strength · Problem-Solving and Measurable Results",
                                     "Gap · Leadership and Team Management", "Overall assessment (draft for human review)"]
    assert all(i.evidence and all(validate(e, JT) for e in i.evidence) for i in oa)
    assert not rejected
    # the same verdict outside the assessment is dropped
    raw = job_raw(next_steps=[{"option": "She is a strong candidate.", "evidence": ev("u0003", "wrote a script")}])
    items, rejected = build_items(raw, JOB, JT)
    assert not any(i.section == "next_steps" for i in items) and rejected[0]["reason"].startswith("lint: overall verdict")


def test_overall_assessment_dropped_whole_if_any_quote_fails():
    raw = job_raw()
    raw["overall_assessment"]["gaps"][0]["evidence"] = ev("u0004", "I led a team of ten")
    items, rejected = build_items(raw, JOB, JT)
    assert not any(i.section == "overall_assessment" for i in items)
    assert any(r["section"] == "overall_assessment" and r["reason"].startswith("evidence") for r in rejected)
    raw = job_raw()
    raw["overall_assessment"]["strengths"][0]["evidence"] = ev("u0001", "Tell me about a hard problem")  # interviewer
    assert not any(i.section == "overall_assessment" for i in build_items(raw, JOB, JT)[0])


def test_overall_assessment_ignored_when_not_allowed():
    off = pack_from_dict({**yaml.safe_load((ROOT / "packs" / "job_interview_v1.yaml").read_text()),
                          "guardrails": {**JOB.rails.model_dump(), "allow_overall_recommendation": False}})
    items, _ = build_items(job_raw(), off, JT)
    assert not any(i.section == "overall_assessment" for i in items)


def test_cps_copy_with_recommendation_on_still_blocks_abuse_credibility_and_placement():
    data = yaml.safe_load((ROOT / "packs" / "cps_interview_v2.yaml").read_text())
    data["guardrails"]["allow_overall_recommendation"] = True
    on = pack_from_dict(data, "test copy")
    t = {u.id: u for u in [utt(1, "parent", "Jill told me she fell and hit her face."),
                            utt(2, "child", "I don't remember what happened.")]}
    base = {"strengths": [{"topic_id": "parent_account", "point": "Mom described what she was told.",
                           "evidence": ev("u0001", "Jill told me she fell")}],
            "gaps": [{"topic_id": "child_account", "point": "Jill did not describe what happened.",
                      "evidence": ev("u0002", "I don't remember")}],
            "draft": {"text": "", "evidence": ev("u0001", "Jill told me")}}
    ok_text = "Options to consider: ask Jill once more, gently, what she remembers."
    kept, _ = build_items({"overall_assessment": {**base, "draft": {**base["draft"], "text": ok_text}}}, on, t)
    assert [i.label for i in kept][-1] == "Overall assessment (draft for human review)"
    for bad in ("Jill was abused at home.", "Mom is lying about the stairs.", "Jill should be removed from the home.",
                "Jill must be placed in foster care."):
        items, rejected = build_items({"overall_assessment": {**base, "draft": {**base["draft"], "text": bad}}}, on, t)
        assert not any(i.section == "overall_assessment" for i in items), bad
    lint = Lint(on)
    assert lint.question("How did the abuse start?", []) and lint.question("Who should have custody of you?", [])


# ---- end to end, offline ----

def run_ws(tmp_path, pack_query=None, settings_pack=None, end=True):
    env = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"}
    settings = load_settings(env_file=None, environ=env)
    if settings_pack:
        settings = settings.model_copy(update={"pack": settings_pack})
    app = create_app(settings, sessions_dir=tmp_path)
    client = TestClient(app)
    msgs = []
    url = f"/ws?pack={pack_query}" if pack_query else "/ws"
    with client.websocket_connect(url) as ws:
        hello = ws.receive_json()
        ws.send_json({"type": "replay", "speed": 0})
        while not any(m["type"] == "replay_done" for m in msgs):
            msgs.append(ws.receive_json())
        if end:
            ws.send_json({"type": "end_confirm"})
            while not (msgs[-1]["type"] == "review" and msgs[-1]["review"]["state"] in ("ready", "failed")):
                msgs.append(ws.receive_json())
    return client, hello, msgs


def test_tiny_pack_runs_end_to_end_with_its_own_roles(tmp_path):
    client, hello, msgs = run_ws(tmp_path, settings_pack="tests/packs/tiny_pack.yaml")
    assert [r["label"] for r in hello["pack"]["roles"]] == ["Coordinator", "New member"]
    assert hello["pack"]["consent_topic"] is None
    assert {c["id"] for c in hello["checklist"]} == {"plants_grown", "availability"}
    updates = [m["state"] for m in msgs if m["type"] == "topic_update"]
    assert {u["item_id"] for u in updates} >= {"plants_grown"}
    review = msgs[-1]["review"]
    assert review["state"] == "ready"
    labels = {i["label"] for i in review["draft"]["items"]}
    assert "New member said" in labels
    assert not CPS_WORDS.search(json.dumps([hello, msgs]))
    assert not any(m["type"] == "consent_check" for m in msgs)


def test_job_pack_via_picker_runs_with_interviewer_labels_and_no_cps_wording(tmp_path):
    client, hello, msgs = run_ws(tmp_path, pack_query="job_interview_v1.yaml")
    sid = hello["session_id"]
    assert hello["pack"]["id"] == "job_interview_v1" and hello["pack"]["file"] == "job_interview_v1.yaml"
    assert [r["label"] for r in hello["pack"]["roles"]] == ["Interviewer", "Candidate"]
    draft = msgs[-1]["review"]["draft"]
    sections = [s["id"] for s in draft["sections"]]
    assert "overall_assessment" in sections and "custom.important_facts" in sections
    oa = [i for i in draft["items"] if i["section"] == "overall_assessment"]
    assert oa and oa[-1]["label"] == "Overall assessment (draft for human review)" and all(i["evidence"] for i in oa)
    assert not any(m["type"] in ("cue", "topic_update") and "assessment" in json.dumps(m).lower() for m in msgs)
    page = client.get(f"/api/sessions/{sid}/export.html").text
    assert not CPS_WORDS.search(json.dumps([hello, msgs]) + page)
    audit = [json.loads(l) for l in (tmp_path / sid / "audit.jsonl").read_text().splitlines()]
    start = next(a for a in audit if a["action"] == "session_start")["details"]
    assert start["pack"] == "job_interview_v1@0.1.0" and start["recommendation_enabled"] is True
    assert any(a["action"] == "ai_output_shown" and a["details"].get("kind") == "review" for a in audit)
    rows = [json.loads(l) for l in (tmp_path / sid / "ledger.jsonl").read_text().splitlines()]
    assert rows and {r["pack"] for r in rows} == {"job_interview_v1@0.1.0"}


def test_overall_assessment_user_action_is_audited(tmp_path):
    env = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"}
    app = create_app(load_settings(env_file=None, environ=env), sessions_dir=tmp_path)
    with TestClient(app).websocket_connect("/ws?pack=job_interview_v1.yaml") as ws:
        sid = ws.receive_json()["session_id"]
        ws.send_json({"type": "replay", "speed": 0})
        while ws.receive_json()["type"] != "replay_done":
            pass
        ws.send_json({"type": "end_confirm"})
        while not ((m := ws.receive_json())["type"] == "review" and m["review"]["state"] == "ready"):
            pass
        item = next(i for i in m["review"]["draft"]["items"] if i["section"] == "overall_assessment")
        ws.send_json({"type": "review_action", "id": item["id"], "action": "reject"})
        while ws.receive_json()["type"] != "review_item":
            pass
    audit = (tmp_path / sid / "audit.jsonl").read_text()
    assert '"review_reject"' in audit and item["id"] in audit


def test_cps_offline_run_has_no_overall_verdict(tmp_path):
    client, hello, msgs = run_ws(tmp_path)
    draft = msgs[-1]["review"]["draft"]
    assert hello["pack"]["id"] == "cps_interview_v2"
    assert not any(i["section"] == "overall_assessment" for i in draft["items"])
    assert "overall_assessment" not in [s["id"] for s in draft["sections"]]


def test_picker_lists_packs_and_bad_names_fall_back(tmp_path):
    env = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template"}
    client = TestClient(create_app(load_settings(env_file=None, environ=env), sessions_dir=tmp_path))
    data = client.get("/api/packs").json()
    assert data["default"] == "cps_interview_v2.yaml"
    assert {p["file"] for p in data["packs"]} >= {"cps_interview_v2.yaml", "cps_interview_v2_simple.yaml", "job_interview_v1.yaml"}
    with client.websocket_connect("/ws?pack=../config/settings.yaml") as ws:
        assert ws.receive_json()["pack"]["id"] == "cps_interview_v2"
    with client.websocket_connect("/ws?pack=job_interview_v1.yaml") as ws:
        hello = ws.receive_json()
        assert hello["pack"]["id"] == "job_interview_v1"
    assert not (tmp_path / hello["session_id"]).exists() or "session_stop" in (tmp_path / hello["session_id"] / "audit.jsonl").read_text()


def test_typed_speaker_must_be_a_pack_role(tmp_path):
    env = {"GATE_MODEL": "fake:keyword", "CUE_MODEL": "fake:template"}
    client = TestClient(create_app(load_settings(env_file=None, environ=env), sessions_dir=tmp_path))
    with client.websocket_connect("/ws?pack=job_interview_v1.yaml") as ws:
        ws.receive_json()
        ws.send_json({"type": "typed", "speaker": "parent", "text": "hello"})
        assert "interviewer, candidate" in ws.receive_json()["message"]


# ---- loader rules ----

@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.update(roles=[{"id": "a", "label": "A", "kind": "subject"}, {"id": "b", "label": "B", "kind": "subject"}]),
     "exactly one role must be kind 'user'"),
    (lambda d: d.update(roles=[{"id": "a", "label": "A", "kind": "user"}]), "at least one role must be kind 'subject'"),
    (lambda d: d.update(special_topics={"consent_topic": "nope"}), "special_topics.consent_topic: unknown topic 'nope'"),
    (lambda d: d["checklist"].append({"label": "Empty"}), "needs at least one line"),
    (lambda d: d.update(review={"custom_sections": [{"id": "s", "label": "S", "type": "status", "instruction": "x",
                                                     "options": ["one"]}]}), "status needs at least two"),
    (lambda d: d.update(review={"custom_sections": [{"id": "s", "label": "S", "type": "score", "instruction": "x",
                                                     "min": 5, "max": 1}]}), "min smaller than max"),
    (lambda d: d.pop("purpose"), "purpose is empty"),
])
def test_loader_rejects_bad_packs(mutate, msg):
    d = yaml.safe_load(TINY_FILE.read_text())
    mutate(d)
    with pytest.raises(PackError, match=re.escape(msg)):
        pack_from_dict(d)


def test_simple_cps_pack_loads_tier_1_only():
    p = load_pack(ROOT / "packs" / "cps_interview_v2_simple.yaml")
    assert len(p.checklist) == 11 and all(not c.definition and c.criteria for c in p.checklist)
    assert p.guardrails is None and p.special_topics.consent_topic is None
    assert [c.id for c in p.checklist] == [c.id for c in CPS.checklist]


# ---- evaluation rule 5: no pack text shares six consecutive words with a test fixture ----

def _ngrams(text: str, n: int = 6) -> set[tuple[str, ...]]:
    w = normalise(text).split()
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def test_pack_text_does_not_overlap_test_fixtures():
    fixture_grams = set()
    for f in (ROOT / "tests" / "fixtures").rglob("*.json"):
        for s in _strings(json.loads(f.read_text())):
            fixture_grams |= _ngrams(s)
    offenders = []
    for pf in [*(ROOT / "packs").glob("*.yaml"), TINY_FILE]:
        p = load_pack(pf)
        for c in p.checklist:
            for text in [c.definition, c.follow_up_when, *c.criteria, *c.examples]:
                if hits := _ngrams(text) & fixture_grams:
                    offenders.append((pf.name, c.id, " ".join(sorted(hits)[0])))
    assert not offenders, offenders


# ---- importer ----

@pytest.mark.parametrize("name", ["82fd886a2f9948f9936d9ecd142ce03b.json", "9c7b4690e3224e11b3d0a6a6efd843b6.json",
                                  "a93b650fd80e44eb9c40a12d65b32301.json"])
def test_importer_converts_sample_templates(name):
    doc = json.loads((TEMPLATES / name).read_text())
    pack, warnings = convert(doc)
    cfg = doc["config"]
    assert [c["label"] for c in pack["checklist"]] == [t["label"] for t in cfg["conversation"]["topics_to_cover"]]
    assert [c["criteria"] for c in pack["checklist"]] == [t["criteria"] for t in cfg["conversation"]["topics_to_cover"]]
    assert all("definition" not in c for c in pack["checklist"])
    assert pack["roles"][0]["kind"] == "user" and all(r["kind"] == "subject" for r in pack["roles"][1:])
    assert pack["roles"][0]["label"] == cfg["conversation"]["speakers"][0]["role"]
    assert any("Check the roles" in w for w in warnings)
    assert any("'Summary' repeats the built-in review section 'summary'" in w for w in warnings)
    assert [s["id"] for s in pack["review"]["custom_sections"]] == [s["id"] for s in cfg["report"]["sections"]]
    pack_from_dict(pack)


def test_importer_keeps_all_five_section_types():
    pack, _ = convert(json.loads((TEMPLATES / "all_section_types.json").read_text()))
    secs = {s["id"]: s for s in pack["review"]["custom_sections"]}
    assert [s["type"] for s in secs.values()] == ["text", "list", "status", "boolean", "score"]
    assert secs["open_items"]["max_items"] == 3 and secs["call_outcome"]["options"] == ["Resolved", "Follow-up needed"]
    assert (secs["clarity_of_answers"]["min"], secs["clarity_of_answers"]["max"]) == (1, 5) and secs["visit_booked"]["required"] is False
    p = pack_from_dict(pack)
    assert [r.id for r in p.roles] == ["agent", "customer"]


def test_custom_section_values_are_validated_by_type():
    p = pack_from_dict(convert(json.loads((TEMPLATES / "all_section_types.json").read_text()))[0])
    t = {u.id: u for u in [utt(1, "agent", "Did the repair hold?"), utt(2, "customer", "Yes, it works now.")]}
    e = ev("u0002", "it works now")
    raw = {"custom": {"summary": {"value": "Repair works.", "evidence": e},
                      "open_items": [{"value": f"item {i}", "evidence": e} for i in range(5)],
                      "call_outcome": {"value": "Maybe", "evidence": e},
                      "visit_booked": {"value": False, "evidence": e},
                      "clarity_of_answers": {"value": 9, "evidence": e}}}
    items, rejected = build_items(raw, p, t)
    got = {i.section: i.value for i in items}
    assert got["custom.visit_booked"] == "No" and sum(i.section == "custom.open_items" for i in items) == 3
    assert sorted(r["reason"] for r in rejected) == ["not_a_listed_option", "over_max_items", "over_max_items",
                                                     "score_out_of_range"]


@pytest.mark.parametrize("mutate,msg", [
    (lambda c: c["conversation"].update(speakers=[{"id": "s", "role": "Only one"}]), "at least two speakers"),
    (lambda c: c["conversation"].update(topics_to_cover=[{"id": "t", "label": "T", "criteria": []}]), "coverage criterion"),
    (lambda c: c["report"]["sections"][0].update(type="table"), "unsupported type: table"),
    (lambda c: c["conversation"].update(purpose=" "), "Purpose must not be empty"),
])
def test_importer_refuses_invalid_templates(mutate, msg):
    doc = json.loads((TEMPLATES / "all_section_types.json").read_text())
    mutate(doc["config"])
    with pytest.raises(TemplateError, match=msg):
        convert(doc)


def test_importer_cli_writes_once_and_never_overwrites(tmp_path, capsys):
    out = tmp_path / "x.yaml"
    assert import_main([str(TEMPLATES / "all_section_types.json"), "--out", str(out)]) == 0
    assert load_pack(out).id == "made_up_service_check_in"
    assert "Warning: Roles" in capsys.readouterr().out
    assert import_main([str(TEMPLATES / "all_section_types.json"), "--out", str(out)]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert import_main([str(bad), "--out", str(tmp_path / "y.yaml")]) == 1 and not (tmp_path / "y.yaml").exists()
    assert "Not imported" in capsys.readouterr().err


# ---- no interview-type words left in the engine ----

def test_engine_code_has_no_cps_words():
    out = subprocess.run(["grep", "-rniE", "parent|child|worker|abuse", "app"], cwd=ROOT, capture_output=True, text=True).stdout
    allowed = re.compile(r"\.parent\b|parents=True|replaceChildren|\.children\b|__pycache__|Binary file")
    left = [l for l in out.splitlines() if not allowed.search(l)]
    assert not left, left


def test_flat_custom_section_values_are_typed_and_checked():
    p = pack_from_dict(convert(json.loads((TEMPLATES / "all_section_types.json").read_text()))[0])
    t = {u.id: u for u in [utt(1, "agent", "Did the repair hold?"), utt(2, "customer", "Yes, it works now.")]}
    e = ev("u0002", "it works now")
    raw = {"custom": [{"section": "visit_booked", "value": "yes", "evidence": e},
                      {"section": "clarity_of_answers", "value": "4", "evidence": e},
                      {"section": "call_outcome", "value": "Resolved", "evidence": e},
                      {"section": "call_outcome", "value": "Follow-up needed", "evidence": e},
                      {"section": "open_items", "value": "one", "evidence": e}, {"section": "open_items", "value": "two", "evidence": e}]}
    items, rejected = build_items(raw, p, t)
    got = {(i.section, i.value) for i in items}
    assert {("custom.visit_booked", "Yes"), ("custom.clarity_of_answers", "4 (scale 1 to 5)"),
            ("custom.call_outcome", "Resolved")} <= got and sum(i.section == "custom.open_items" for i in items) == 2
    assert [r["reason"] for r in rejected] == ["more_than_one_value"]
