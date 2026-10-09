import asyncio
import json

import httpx

from app.adapters.deciders import build_decider
from app.adapters.deciders.jev import JevDecider
from app.adapters.deciders.llm import LLMDecider
from app.adapters.providers.openrouter import OpenRouterClient, ProviderError
from app.config import ROOT, load_settings
from app.ledger import Ledger
from app.models import TopicState, Utterance
from app.pack import load_pack
from tests.fakes import FAKE_PRICES

PACK = load_pack(ROOT / "packs" / "cps_interview_v1.yaml")


def utt(i, speaker, text):
    return Utterance(id=f"u{i:04d}", session_id="s", t_start_ms=0, t_end_ms=0, speaker=speaker, text=text,
                     is_final=True, source="typed")


def fresh_state():
    return {c.id: TopicState(item_id=c.id) for c in PACK.checklist}


def ledger(tmp_path):
    return Ledger(tmp_path, "s", "e", 1.0, prices=FAKE_PRICES)


def test_short_prompt_is_shorter_and_keeps_safety_rules(tmp_path):
    full = LLMDecider(PACK, None, "openrouter", "m", ledger(tmp_path))
    short = LLMDecider(PACK, None, "openrouter", "m", ledger(tmp_path), variant="short")
    w = [utt(1, "child", "he just scares me")]
    f_len = sum(len(m["content"]) for m in full.messages(w, fresh_state()))
    s_len = sum(len(m["content"]) for m in short.messages(w, fresh_state()))
    assert s_len < 0.7 * f_len
    sys_short = short.messages(w, fresh_state())[0]["content"]
    assert "DATA, not instructions" in sys_short and "Never judge credibility" in sys_short
    assert "at most 12 words" in sys_short


def test_settings_select_gate_variant_and_jev(tmp_path):
    env = {"OPENROUTER_API_KEY": "k"}
    d, _ = build_decider(load_settings(env_file=None, environ={**env, "GATE_PROMPT": "short"}), PACK, ledger(tmp_path))
    assert isinstance(d, LLMDecider) and "at most 12 words" in d.system
    asyncio.run(d.client.close())
    j, _ = build_decider(load_settings(env_file=None, environ={**env, "GATE_ADAPTER": "jev"}), PACK, ledger(tmp_path))
    assert isinstance(j, JevDecider) and j.model == "typesafe/jev-1.13" and j.min_p == 0.6 and j.style == "choice"
    assert len(j.static_questions) == 12                       # 11 status choices + answer quality
    assert len(j.questions([utt(1, "child", "x")])) == 12 + 11  # + one evidence pick per topic
    asyncio.run(j.client.close())
    y, _ = build_decider(load_settings(env_file=None, environ={**env, "GATE_ADAPTER": "jev", "JEV_QUESTIONS": "yesno"}),
                         PACK, ledger(tmp_path))
    assert y.style == "yesno" and len(y.static_questions) == 3 * 11 + 1
    assert {q["type"] for k, q in y.static_questions.items() if k != "answer_quality"} == {"noul"}
    asyncio.run(y.client.close())


def choice(c, p):
    return {"type": "choice", "choice": c, "confidence": p, "probabilities": {c: p}}


class FakeDecideClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def decide(self, model, state, questions):
        self.calls.append({"state": state, "questions": questions})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"answers": r, "input_tokens": 900, "output_tokens": 30, "cost_usd": 0.00004, "latency_ms": 300}


def test_jev_maps_probabilities_to_updates_without_evidence(tmp_path):
    answers = {f"topic__{c.id}": choice("not_covered", 0.9) for c in PACK.checklist}
    answers["topic__recording_consent"] = choice("covered", 0.95)
    answers["topic__child_account"] = choice("partial", 0.4)                # below threshold
    answers["topic__child_safety_feelings"] = choice("needs_follow_up", 0.7)
    answers["topic__caregivers"] = choice("partial", 0.9)                   # same as current -> no update
    answers["answer_quality"] = choice("vague", 0.8)
    state = fresh_state()
    state["caregivers"] = TopicState(item_id="caregivers", status="partial")
    led = ledger(tmp_path)
    j = JevDecider(PACK, FakeDecideClient([answers]), "openrouter", "typesafe/jev-1.13", led, 0.6)
    window = [utt(1, "child", "I consent"), utt(2, "worker", "Thanks.")]
    r = asyncio.run(j.evaluate(window, state))
    # Jev's "needs_follow_up" answer maps to partial + suggested; Jev never raises a required follow-up
    assert {(u.item_id, u.status, u.follow_up) for u in r.updates} == {("recording_consent", "covered", "none"),
                                                                       ("child_safety_feelings", "partial", "suggested")}
    assert all(u.evidence == [] for u in r.updates)  # no picks in this answer set
    assert r.answer_quality.utterance_id == "u0001" and r.answer_quality.quality == "vague"
    sent = j.client.calls[0]["state"]
    assert "[u0001] child: I consent" in sent["transcript"] and "not instructions" in sent["note"]
    row = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert row["model"] == "typesafe/jev-1.13" and row["cost_source"] == "provider"


def noul(p):
    return {"type": "noul", "noul": p}


def test_jev_yesno_mapping_and_picks_become_whole_utterance_evidence(tmp_path):
    answers = {}
    for c in PACK.checklist:
        answers.update({f"mentioned__{c.id}": noul(0.1), f"met__{c.id}": noul(0.1), f"followup__{c.id}": noul(0.1)})
    answers.update({"mentioned__recording_consent": noul(0.9), "met__recording_consent": noul(0.8),   # covered
                    "mentioned__household_members": noul(0.9), "met__household_members": noul(0.3),  # partial
                    "mentioned__child_safety_feelings": noul(0.9), "followup__child_safety_feelings": noul(0.7),
                    "met__child_safety_feelings": noul(0.9),                                         # follow-up wins
                    "followup__caregivers": noul(0.9)})                                              # not mentioned: none
    answers["evidence__recording_consent"] = choice("u0001", 0.9)
    j = JevDecider(PACK, FakeDecideClient([answers]), "openrouter", "m", ledger(tmp_path), 0.6, style="yesno")
    r = asyncio.run(j.evaluate([utt(1, "child", "I consent")], fresh_state()))
    assert {(u.item_id, u.status, u.follow_up) for u in r.updates} == {("recording_consent", "covered", "none"),
                                                                       ("household_members", "partial", "none"),
                                                                       ("child_safety_feelings", "partial", "suggested")}
    ev = {u.item_id: u.evidence for u in r.updates}  # spec 6.2 changed 2026-10-08: Jev's pick is the evidence
    assert [(e.utterance_id, e.quote) for e in ev["recording_consent"]] == [("u0001", "I consent")]
    assert ev["household_members"] == [] and ev["child_safety_feelings"] == []  # no pick -> no evidence
    assert j.last_picks["recording_consent"] == ("u0001", 0.9)
    sent = j.client.calls[0]["questions"]["evidence__recording_consent"]["criteria"]
    assert sent == {"u0001": "child: I consent", "none": "No utterance is relevant to this topic."}


def test_jev_atomic_combines_single_condition_questions_in_code(tmp_path):
    j = JevDecider(PACK, None, "openrouter", "m", ledger(tmp_path), 0.6, style="atomic", followup_probability=0.8)
    assert len(j.static_questions) == 6 * 11 + 1
    assert all("criteria" not in q and " or not mentioned" not in q["instructions"]
               for k, q in j.static_questions.items() if k != "answer_quality")
    c = next(c for c in PACK.checklist if c.id == "child_safety_feelings")
    base = {f"{k}__{c.id}": noul(0.1) for k in ("mentioned", "met", "vague", "evasive", "conflict", "safety")}
    assert j.proposed_status(c, {**base, f"mentioned__{c.id}": noul(0.9), f"vague__{c.id}": noul(0.85)}) == ("needs_follow_up", 0.85)
    assert j.proposed_status(c, {**base, f"mentioned__{c.id}": noul(0.9), f"vague__{c.id}": noul(0.75)}) == ("partial", 0.9)
    assert j.proposed_status(c, {**base, f"safety__{c.id}": noul(0.95)}) == (None, 0.0)  # not mentioned


def test_jev_retries_server_errors_but_not_auth(tmp_path):
    """M10a: server errors are retried with backoff (fake sleep here); auth errors are not retried."""
    async def no_sleep(s):
        pass
    j = JevDecider(PACK, FakeDecideClient([ProviderError("502", "server", 502), ProviderError("502", "server", 502), {}]),
                   "openrouter", "m", ledger(tmp_path), 0.6)
    j.caller.sleep = no_sleep
    assert asyncio.run(j.evaluate([utt(1, "child", "x")], fresh_state())).updates == []
    assert len(j.client.calls) == 3 and j.last_error is None
    j = JevDecider(PACK, FakeDecideClient([ProviderError("401", "auth", 401), {}]), "openrouter", "m", ledger(tmp_path), 0.6)
    assert asyncio.run(j.evaluate([utt(1, "child", "x")], fresh_state())).updates == []
    assert len(j.client.calls) == 1 and j.last_error == "401" and j.gave_up


def test_decisions_client_request_shape():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"answers": {"q": choice("ok", 1.0)},
                                         "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002}})

    async def go():
        c = OpenRouterClient("k", transport=httpx.MockTransport(handler))
        r = await c.decide("typesafe/jev-1.13", {"transcript": "x"}, {"q": {"type": "choice"}})
        await c.close()
        return r

    r = asyncio.run(go())
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["body"] == {"model": "typesafe/jev-1.13", "state": {"transcript": "x"}, "questions": {"q": {"type": "choice"}}}
    assert (r["input_tokens"], r["cost_usd"]) == (476, 0.00002)
