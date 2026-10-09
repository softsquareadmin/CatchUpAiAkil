"""Hybrid gate (spec 6.2): Jev and the LLM gate run at the same time on every gate run.
Fast stage (~0.2 s): Jev may only move a topic from not covered to partial ("this topic has come up") when its
confidence >= min_confidence and it picked a supporting utterance. Jev never marks a topic covered and never
sets or clears a follow-up. Final stage (LLM latency): the LLM gate sees every topic and is authoritative for
coverage, follow-ups and flags.
Redesigned 2026-10-08 for the follow-up split: an earlier version asked the LLM only about topics Jev was unsure
of, which could skip a required follow-up whenever Jev was confidently wrong."""
import asyncio
from typing import Awaitable, Callable

from app.adapters.deciders.jev import JevDecider
from app.adapters.deciders.llm import LLMDecider
from app.models import GateResult, TopicState, TopicUpdate, Utterance

OnPartial = Callable[[GateResult], Awaitable[None]]


class HybridDecider:
    staged = True  # the pipeline passes on_partial so the fast stage is shown before the LLM finishes

    def __init__(self, jev: JevDecider, llm: LLMDecider, min_confidence: float):
        self.jev, self.llm, self.min_confidence = jev, llm, min_confidence
        self.client = jev.client  # shared OpenRouter client, closed by the session
        self.last_error: str | None = None
        self.last_fast: list[str] = []

    @property
    def gave_up(self) -> bool:  # the LLM gate is authoritative: if it gave up, the run is skipped
        return self.llm.gave_up

    def fast_updates(self, answers: dict, window: list[Utterance], state: dict[str, TopicState]) -> GateResult:
        fast = GateResult()
        for d in self.jev.decisions(answers, window):
            current = state[d.topic_id].status if d.topic_id in state else "not_covered"
            if (current == "not_covered" and d.status in ("partial", "covered")
                    and d.confidence >= self.min_confidence and d.evidence is not None):
                fast.updates.append(TopicUpdate(item_id=d.topic_id, status="partial", evidence=[d.evidence],
                                                rationale_short=f"topic came up (fast check, conf {d.confidence:.2f})"))
        return fast

    async def evaluate(self, window: list[Utterance], checklist_state: dict[str, TopicState],
                       on_partial: OnPartial | None = None) -> GateResult:
        self.last_error, self.last_fast = None, []
        llm_task = asyncio.create_task(self.llm.evaluate(window, checklist_state))
        try:
            answers = await self.jev.ask(window, checklist_state)
        except BaseException:
            llm_task.cancel()
            raise
        fast = self.fast_updates(answers, window, checklist_state) if answers else GateResult()
        self.last_fast = [u.item_id for u in fast.updates]
        if on_partial is not None:
            await on_partial(fast)
        slow = await llm_task
        self.last_error = self.llm.last_error or self.jev.last_error
        if on_partial is None:  # single result: the LLM's updates come last so they win
            slow = GateResult(updates=fast.updates + slow.updates, flags=slow.flags, answer_quality=slow.answer_quality)
        return slow
