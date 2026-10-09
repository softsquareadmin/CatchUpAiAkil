"""Hybrid gate (redesigned 2026-10-08): Jev and the LLM run in parallel; Jev may only move a topic from
not covered to partial; the LLM is authoritative for coverage and follow-ups on every topic."""
import asyncio
import json

from app.adapters.deciders import build_decider
from app.adapters.deciders.hybrid import HybridDecider
from app.adapters.deciders.jev import JevDecider
from app.adapters.deciders.llm import LLMDecider
from app.adapters.providers.openrouter import ProviderError
from app.config import ROOT, load_settings
from app.ledger import Ledger
from app.models import TopicState, Utterance
from app.pack import load_pack
from app.pipeline import Session
from tests.fakes import FAKE_PRICES

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")
WINDOW = [Utterance(id="u0001", session_id="s", t_start_ms=0, t_end_ms=0, speaker="child",
                    text="I'm seven. I'm in second grade.", is_final=True, source="typed"),
          Utterance(id="u0002", session_id="s", t_start_ms=0, t_end_ms=0, speaker="child",
                    text="He just scares me.", is_final=True, source="typed")]


def choice(c, p, conf=None):
    return {"type": "choice", "choice": c, "confidence": p if conf is None else conf, "probabilities": {c: p}}


def answers(**topics):
    """topics: id -> (Jev status answer, confidence, picked utterance id or None)."""
    a = {f"topic__{c.id}": choice("not_covered", 0.99) for c in PACK.checklist}
    a.update({f"evidence__{c.id}": choice("none", 0.99) for c in PACK.checklist})
    for tid, (status, conf, pick) in topics.items():
        a[f"topic__{tid}"] = choice(status, conf)
        if pick:
            a[f"evidence__{tid}"] = choice(pick, 0.95)
    a["answer_quality"] = choice("vague", 0.8)
    return a


def llm_reply(*updates):
    return json.dumps({"updates": list(updates), "flags": [],
                       "answer_quality": {"utterance_id": "u0002", "quality": "vague"}})


SAFETY = {"item_id": "child_safety_feelings", "status": "partial", "follow_up": "required",
          "follow_up_reason": "afraid, no reason", "rationale_short": "scared",
          "evidence": [{"utterance_id": "u0002", "quote": "scares me"}]}
AGE = {"item_id": "child_age_grade", "status": "covered", "follow_up": "none", "follow_up_reason": "",
       "rationale_short": "age and grade", "evidence": [{"utterance_id": "u0001", "quote": "I'm seven"}]}


class Client:
    """Fake OpenRouter client serving both Jev (decide) and the LLM gate (chat_json)."""

    def __init__(self, jev_replies, llm_replies, llm_delay=0.0):
        self.jev_replies, self.llm_replies, self.llm_calls, self.llm_delay = list(jev_replies), list(llm_replies), [], llm_delay
        self.order = []

    async def decide(self, model, state, questions):
        await asyncio.sleep(0.01)  # a real Jev call takes ~0.2 s
        self.order.append("jev-done")
        r = self.jev_replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"answers": r, "input_tokens": 900, "output_tokens": 0, "cost_usd": 0.00003, "latency_ms": 200}

    async def chat_json(self, model, messages, schema_name, schema, reasoning_effort=None, max_tokens=2000):
        self.order.append("llm-start")
        self.llm_calls.append({"messages": messages, "schema": schema})
        await asyncio.sleep(self.llm_delay)
        return {"content": self.llm_replies.pop(0), "input_tokens": 2000, "output_tokens": 100, "cost_usd": 0.002,
                "latency_ms": 2000}

    async def close(self):
        pass


def hybrid(tmp_path, client, tau=0.9):
    led = Ledger(tmp_path, "s", "e", 1.0, prices=FAKE_PRICES)
    return HybridDecider(JevDecider(PACK, client, "openrouter", "jev", led, 0.6),
                         LLMDecider(PACK, client, "openrouter", "gemini", led), tau)


def fresh():
    return {c.id: TopicState(item_id=c.id) for c in PACK.checklist}


def run(h, state, collect=True):
    partials = []

    async def on_partial(r):
        partials.append(r)

    slow = asyncio.run(h.evaluate(WINDOW, state, on_partial=on_partial if collect else None))
    return partials, slow


def test_fast_stage_only_marks_partial_with_whole_utterance_evidence(tmp_path):
    c = Client([answers(child_age_grade=("covered", 0.97, "u0001"),          # Jev says covered: shown as partial
                        household_members=("partial", 0.95, None),           # no pick: not shown
                        caregivers=("partial", 0.8, "u0001"))],              # below 0.9: not shown
               [llm_reply(AGE, SAFETY)])
    h = hybrid(tmp_path, c)
    [fast], slow = run(h, fresh())
    assert [(u.item_id, u.status, u.follow_up) for u in fast.updates] == [("child_age_grade", "partial", "none")]
    assert fast.updates[0].evidence[0].quote == "I'm seven. I'm in second grade."
    assert h.last_fast == ["child_age_grade"]
    assert {(u.item_id, u.status, u.follow_up) for u in slow.updates} == {("child_age_grade", "covered", "none"),
                                                                         ("child_safety_feelings", "partial", "required")}


def test_llm_always_runs_on_every_topic_in_parallel_with_jev(tmp_path):
    c = Client([answers(child_age_grade=("covered", 0.99, "u0001"))], [llm_reply(SAFETY)])
    h = hybrid(tmp_path, c)
    _, slow = run(h, fresh())
    assert len(c.llm_calls) == 1 and c.order.index("llm-start") < c.order.index("jev-done")  # parallel
    enum = c.llm_calls[0]["schema"]["properties"]["updates"]["items"]["properties"]["item_id"]["enum"]
    assert len(enum) == 11 and "<topics_to_evaluate>" not in c.llm_calls[0]["messages"][1]["content"]
    assert [(u.item_id, u.follow_up) for u in slow.updates] == [("child_safety_feelings", "required")]


def test_jev_never_touches_topics_already_partial_or_covered_or_follow_ups(tmp_path):
    state = fresh()
    state["recording_consent"] = TopicState(item_id="recording_consent", status="covered")
    state["child_safety_feelings"] = TopicState(item_id="child_safety_feelings", status="partial", follow_up="required")
    c = Client([answers(recording_consent=("partial", 0.99, "u0001"),
                        child_safety_feelings=("covered", 0.99, "u0002"))], [llm_reply()])
    h = hybrid(tmp_path, c)
    [fast], slow = run(h, state)
    assert fast.updates == [] and slow.updates == []


def test_jev_failure_still_gives_the_llm_result(tmp_path):
    c = Client([ProviderError("502"), ProviderError("502")], [llm_reply(SAFETY)])
    h = hybrid(tmp_path, c)
    [fast], slow = run(h, fresh())
    assert fast.updates == [] and [u.item_id for u in slow.updates] == ["child_safety_feelings"]


def test_single_result_without_on_partial_lets_llm_win(tmp_path):
    c = Client([answers(child_age_grade=("covered", 0.99, "u0001"))], [llm_reply(AGE)])
    h = hybrid(tmp_path, c)
    _, r = run(h, fresh(), collect=False)
    assert [(u.item_id, u.status) for u in r.updates] == [("child_age_grade", "partial"), ("child_age_grade", "covered")]


def test_session_shows_fast_stage_before_final(tmp_path):
    c = Client([answers(child_age_grade=("covered", 0.97, "u0001"))], [llm_reply(AGE, SAFETY)], llm_delay=0.05)

    async def go():
        sent = []

        async def send(m):
            sent.append(m)

        s = Session(load_settings(env_file=None, environ={}), PACK, tmp_path, send, decider=object())
        s.decider = hybrid(tmp_path, c)
        for u in WINDOW:
            s.store.append(u.model_copy(update={"id": ""}))
        s.request_gate()
        await s.gate_idle()
        return s, [m for m in sent if m["type"] in ("topic_update", "gate_status")]

    s, msgs = asyncio.run(go())
    seq = [(m["type"], (m["state"]["item_id"], m["state"]["status"]) if m["type"] == "topic_update" else m["gate"]["stage"])
           for m in msgs]
    assert seq == [("topic_update", ("child_age_grade", "partial")), ("gate_status", "fast"),
                   ("topic_update", ("child_age_grade", "covered")),
                   ("topic_update", ("child_safety_feelings", "partial")), ("gate_status", "final")]
    assert msgs[1]["gate"]["fast_topics"] == ["child_age_grade"]
    assert s.checklist["child_safety_feelings"].follow_up == "required"


def test_gate_choice_comes_from_config_only(tmp_path):
    """Gemini vs hybrid is a settings/env choice (owner, 2026-10-08), not a UI control."""
    async def go(env):
        async def send(m):
            pass

        s = Session(load_settings(env_file=None, environ={"OPENROUTER_API_KEY": "k", **env}), PACK, tmp_path, send)
        kind = type(s.decider)
        await s.close()
        return kind, s.gate_status()["adapter"]

    assert asyncio.run(go({})) == (LLMDecider, "llm")
    assert asyncio.run(go({"GATE_ADAPTER": "hybrid"})) == (HybridDecider, "hybrid")


def test_settings_build_hybrid(tmp_path):
    led = Ledger(tmp_path, "s", "e", 1.0, prices=FAKE_PRICES)
    d, _ = build_decider(load_settings(env_file=None, environ={"OPENROUTER_API_KEY": "k", "GATE_ADAPTER": "hybrid",
                                                              "HYBRID_MIN_CONFIDENCE": "0.8"}), PACK, led)
    assert isinstance(d, HybridDecider) and d.min_confidence == 0.8 and d.llm.model == "google/gemini-3.8-flash"
    asyncio.run(d.client.close())
