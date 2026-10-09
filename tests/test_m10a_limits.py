"""M10a: provider error classes, retry with Retry-After / backoff inside a deadline, the shared per-model limiter
and visible skipped analysis. All offline; time is a fake clock, nothing sleeps for real."""
import asyncio
import json

import httpx
import pytest

from app.adapters.deciders.llm import LLMDecider
from app.adapters.providers.openrouter import OpenRouterClient, ProviderError
from app.config import ROOT, Limits, load_settings
from app.ledger import BudgetExceeded, Ledger
from app.limits import PRIORITY, Caller, GaveUp, Limiter, limiter_for
from app.models import Utterance
from app.pack import load_pack
from app.pipeline import Session
from tests.fakes import FAKE_PRICES

PACK = load_pack(ROOT / "packs" / "cps_interview_v2.yaml")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        await asyncio.sleep(0)


def caller(tmp_path, role="gate", per_min=0, clock=None, deadline=None, cap=1.0, batch=False):
    clock = clock or Clock()
    limits = Limits(requests_per_min={"openrouter": per_min}, deadline_s={**Limits().deadline_s, **(deadline or {})})
    ledger = Ledger(tmp_path, "s", "x", cap, prices=FAKE_PRICES)
    c = Caller(role, "openrouter", "m", ledger, limits, batch=batch, clock=clock, sleep=clock.sleep,
               limiter=Limiter(per_min, clock, clock.sleep))
    return c, clock, ledger


def replies(*items):
    seq = list(items)

    async def fn():
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    fn.left = seq
    return fn


def rows(tmp_path):
    p = tmp_path / "ledger.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ---- classification ----

@pytest.mark.parametrize("status,kind,retry", [(429, "rate_limited", "7"), (401, "auth", None), (400, "bad_request", None),
                                               (503, "server", None), (504, "timeout", None)])
def test_http_errors_are_classified(status, kind, retry):
    headers = {"retry-after": retry} if retry else {}
    c = OpenRouterClient("k", transport=httpx.MockTransport(lambda req: httpx.Response(status, text="x", headers=headers)))
    with pytest.raises(ProviderError) as e:
        asyncio.run(c.chat_json("m", [], "s", {}))
    assert (e.value.kind, e.value.status) == (kind, status)
    assert e.value.retry_after_s == (7.0 if retry else None)
    if kind == "auth":
        assert "OpenRouter rejected the key" in str(e.value)


def test_error_inside_a_200_body_is_classified():
    c = OpenRouterClient("k", transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"error": {"code": 429, "message": "Rate limit exceeded"}})))
    with pytest.raises(ProviderError) as e:
        asyncio.run(c.chat_json("m", [], "s", {}))
    assert e.value.kind == "rate_limited"


# ---- retry and deadline ----

def test_retry_after_header_is_used_then_success(tmp_path):
    c, clock, _ = caller(tmp_path, role="review")
    fn = replies(ProviderError("429", "rate_limited", 429, retry_after_s=5.0), {"ok": 1})
    r, info = asyncio.run(c.call(fn))
    assert r == {"ok": 1} and info.attempt == 2 and info.retry_wait_s == 5.0 and clock.t == 5.0
    assert [(x["ok"], x["status"], x["cost_usd"], x["attempt"]) for x in rows(tmp_path)] == [(False, 429, 0.0, 1)]


def test_backoff_without_retry_after_doubles(tmp_path):
    c, clock, _ = caller(tmp_path, role="review")
    fn = replies(*(ProviderError("429", "rate_limited", 429) for _ in range(3)), {"ok": 1})
    r, info = asyncio.run(c.call(fn))
    assert info.attempt == 4 and 0.8 + 1.6 + 3.2 <= info.retry_wait_s <= 1.2 + 2.4 + 4.8
    assert [x["attempt"] for x in rows(tmp_path)] == [1, 2, 3]


@pytest.mark.parametrize("kind,status", [("auth", 401), ("bad_request", 400)])
def test_auth_and_bad_request_are_not_retried(tmp_path, kind, status):
    c, clock, _ = caller(tmp_path)
    fn = replies(ProviderError("no", kind, status), {"ok": 1})
    with pytest.raises(ProviderError) as e:
        asyncio.run(c.call(fn))
    assert e.value.kind == kind and fn.left == [{"ok": 1}] and clock.t == 0


def test_live_call_gives_up_inside_its_deadline(tmp_path):
    c, clock, _ = caller(tmp_path, role="gate")  # 8 s deadline
    fn = replies(*(ProviderError("429", "rate_limited", 429) for _ in range(10)))
    with pytest.raises(GaveUp):
        asyncio.run(c.call(fn))
    assert clock.t <= 8.0 and len(rows(tmp_path)) <= 4


def test_attempt_cap_holds_even_with_a_long_deadline(tmp_path):
    c, clock, _ = caller(tmp_path, role="review", deadline={"review": 10_000})
    fn = replies(*(ProviderError("503", "server", 503) for _ in range(10)))
    with pytest.raises(GaveUp):
        asyncio.run(c.call(fn))
    assert len(rows(tmp_path)) == 4


def test_budget_cap_stops_before_any_attempt_and_during_retries(tmp_path):
    c, clock, ledger = caller(tmp_path, cap=0.0005)
    ledger.record("gate", "x", "y", provider_cost_usd=0.001)
    fn = replies({"ok": 1})
    with pytest.raises(BudgetExceeded):
        asyncio.run(c.call(fn))
    assert fn.left == [{"ok": 1}]
    c2, _, ledger2 = caller(tmp_path / "b", role="review", cap=0.0005)

    async def first_fails():  # the first attempt spends money (as a failed paid call would), then the cap is passed
        ledger2.record("review", "x", "y", provider_cost_usd=0.001)
        raise ProviderError("429", "rate_limited", 429, retry_after_s=1)
    with pytest.raises(BudgetExceeded):
        asyncio.run(c2.call(first_fails))


# ---- limiter ----

def test_limiter_spreads_100_calls_at_18_per_minute():
    clock = Clock()
    lim = Limiter(18, clock, clock.sleep)
    times = []

    async def go():
        for _ in range(100):
            await lim.acquire(PRIORITY["batch"])
            times.append(clock.t)
    asyncio.run(go())
    assert all(sum(1 for t in times if a <= t < a + 60) <= 18 for a in times)  # never more than 18 in any minute
    assert 5 * 60 <= times[-1] < 6 * 60  # 100 calls need a bit over 5 minutes


def test_roles_on_one_model_share_a_bucket(tmp_path):
    limits = Limits(requests_per_min={"openrouter": 18})
    a = Caller("gate", "openrouter", "shared-model", None, limits)
    b = Caller("cue", "openrouter", "shared-model", None, limits)
    assert a.limiter is b.limiter and a.limiter.per_min == 18
    assert limiter_for("fake", "keyword", limits).per_min == 0  # unlimited where no limit is set


def test_live_cue_goes_ahead_of_a_queued_evaluation_call():
    clock = Clock()
    lim = Limiter(2, clock, clock.sleep)
    order = []

    async def take(name, prio):
        await lim.acquire(prio)
        order.append(name)

    async def go():
        await take("first", 1)
        await take("second", 1)  # bucket full for the next 60 s
        batch = asyncio.create_task(take("evaluation", PRIORITY["batch"]))
        await asyncio.sleep(0)
        cue = asyncio.create_task(take("live cue", PRIORITY["cue"]))
        await asyncio.gather(batch, cue)
    asyncio.run(go())
    assert order == ["first", "second", "live cue", "evaluation"]


def test_limiter_respects_a_deadline():
    clock = Clock()
    lim = Limiter(1, clock, clock.sleep)

    async def go():
        await lim.acquire(1)
        with pytest.raises(GaveUp):
            await lim.acquire(1, deadline=clock.t + 8)
    asyncio.run(go())


# ---- session: skipped analysis is visible and the interview continues ----

class Always429:
    def __init__(self):
        self.calls = 0

    async def chat_json(self, *a, **k):
        self.calls += 1
        raise ProviderError("HTTP 429", "rate_limited", 429)

    async def close(self):
        pass


def session_with_429_gate(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    settings = load_settings(env_file=None, environ={"CUE_MODEL": "fake:template", "REVIEW_MODEL": "fake:template"})
    s = Session(settings, PACK, tmp_path, send, decider=object())
    clock = Clock()
    d = LLMDecider(PACK, Always429(), "openrouter", "m", s.ledger, limits=settings.limits)
    d.caller = Caller("gate", "openrouter", "m", s.ledger, settings.limits, clock=clock, sleep=clock.sleep,
                      limiter=Limiter(0, clock, clock.sleep))
    s.decider = d
    s._wire_callers(d)
    return s, sent, clock


def utt(i, sp, text):
    return Utterance(id="", session_id="s", t_start_ms=i, t_end_ms=i, speaker=sp, text=text, is_final=True, source="typed")


def test_permanent_429_skips_visibly_and_the_interview_continues(tmp_path):
    s, sent, clock = session_with_429_gate(tmp_path)

    async def go():
        await s.on_utterance(utt(1, "parent", "Yes, you may use that tool."))
        await s.gate_idle()
        await s.on_utterance(utt(2, "child", "I consent"))  # still accepted and stored
        await s.gate_idle()
        await s.end_interview()
        await s.cue_idle()
    asyncio.run(go())
    assert len(s.store.utterances) == 2 and clock.t <= 16
    health = [m["health"] for m in sent if m["type"] == "health"]
    assert any(h["delayed_s"] for h in health)  # "Analysis delayed" while retrying
    assert health[-1]["skipped_lines"] == 2 and health[-1]["skipped_ranges"] == [["u0001", "u0001"], ["u0002", "u0002"]]
    audit = (tmp_path / s.id / "audit.jsonl").read_text()
    assert '"analysis_skipped"' in audit and '"provider_rate_limited"' in audit and "consent" not in audit.split("analysis_skipped")[1][:200]
    statuses = [r["status"] for r in map(json.loads, (tmp_path / s.id / "ledger.jsonl").read_text().splitlines()) if r["role"] == "gate"]
    assert statuses and set(statuses) == {429}
    assert "u0001" in s.draft["notice"] and "read the whole transcript" in s.draft["notice"]
    assert s.hello()["health"]["skipped_lines"] == 2


def test_no_review_notice_when_nothing_was_skipped(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    s = Session(load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword", "REVIEW_MODEL": "fake:template"}),
                PACK, tmp_path, send)

    async def go():
        await s.on_utterance(utt(1, "parent", "Yes, you may use that tool."))
        await s.gate_idle()
        await s.end_interview()
        await s.cue_idle()
    asyncio.run(go())
    assert s.draft["notice"] is None and not [m for m in sent if m["type"] == "health" and m["health"]["skipped_lines"]]


def test_batch_sessions_queue_behind_live_ones(tmp_path):
    settings = load_settings(env_file=None, environ={"OPENROUTER_API_KEY": "k"})
    live = Session(settings, PACK, tmp_path, lambda m: None)
    batch = Session(settings, PACK, tmp_path, lambda m: None, batch=True)
    assert live.decider.caller.priority == PRIORITY["gate"] and live.decider.caller.deadline_s == 8
    assert batch.decider.caller.priority == PRIORITY["batch"] and batch.decider.caller.deadline_s == 60
    assert live.decider.caller.limiter is batch.decider.caller.limiter  # one bucket per model, shared


# ---- done-when: a burst against intermittent 429s loses no updates; health reaches the browser ----

class Flaky:
    """Every other call is rejected with 429 (no Retry-After), the rest answer with one update for the last line."""

    def __init__(self):
        self.calls = 0

    async def chat_json(self, model, messages, name, schema, **k):
        self.calls += 1
        if self.calls % 2:
            raise ProviderError("HTTP 429", "rate_limited", 429)
        uid = schema["properties"]["updates"]["items"]["properties"]["evidence"]["items"]["properties"]["utterance_id"]["enum"][-1]
        body = {"updates": [{"item_id": "recording_consent", "status": "partial", "follow_up": "none", "follow_up_reason": "",
                             "evidence": [{"utterance_id": uid, "quote": "consent"}], "rationale_short": "x"}],
                "flags": [], "answer_quality": {"utterance_id": uid, "quality": "ok"}}
        return {"content": json.dumps(body), "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0001, "latency_ms": 1}

    async def close(self):
        pass


def test_burst_with_intermittent_429s_loses_no_updates(tmp_path):
    sent = []

    async def send(m):
        sent.append(m)
    settings = load_settings(env_file=None, environ={})
    s = Session(settings, PACK, tmp_path, send, decider=object())
    clock = Clock()
    d = LLMDecider(PACK, Flaky(), "openrouter", "m", s.ledger, limits=settings.limits)
    d.caller = Caller("gate", "openrouter", "m", s.ledger, settings.limits, clock=clock, sleep=clock.sleep,
                      limiter=Limiter(18, clock, clock.sleep))
    s.decider = d
    s._wire_callers(d)

    async def go():
        for i in range(30):  # a burst: every line arrives before the gate catches up
            await s.on_utterance(utt(i, "parent", f"I consent, line {i}."))
        await s.gate_idle()
    asyncio.run(go())
    rows = [json.loads(l) for l in (tmp_path / s.id / "ledger.jsonl").read_text().splitlines()]
    assert s.gate_read == 30 and not s.health["skipped_lines"]  # every line was read; nothing skipped
    assert s.checklist["recording_consent"].status == "partial"
    assert sum(r["status"] == 429 for r in rows) >= 1 and all(r["ok"] for r in rows if r["status"] is None)
    events = (tmp_path / s.id / "events.jsonl").read_text()
    assert '"provider_wait"' in events


def test_websocket_carries_the_health_state(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings = load_settings(env_file=None, environ={"GATE_MODEL": "fake:keyword"})
    app = create_app(settings, sessions_dir=tmp_path)
    with TestClient(app).websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["health"] == {"delayed_s": None, "skipped_lines": 0, "skipped_ranges": [], "skipped_cues": 0}
        s = app.state.live[hello["session_id"]][0]
        clock = Clock()
        d = LLMDecider(PACK, Always429(), "openrouter", "m", s.ledger, limits=settings.limits)
        d.caller = Caller("gate", "openrouter", "m", s.ledger, settings.limits, clock=clock, sleep=clock.sleep,
                          limiter=Limiter(0, clock, clock.sleep))
        s.decider = d
        s._wire_callers(d)
        ws.send_json({"type": "typed", "speaker": "parent", "text": "hello"})
        seen = []
        while not (seen and seen[-1]["health"]["skipped_lines"]):
            m = ws.receive_json()
            if m["type"] == "health":
                seen.append(m)
    assert any(m["health"]["delayed_s"] for m in seen) and seen[-1]["health"]["skipped_ranges"] == [["u0001", "u0001"]]
