"""Provider rate limits (M10a): one request limiter per provider model, shared by every role and session in this
process, and a caller that retries rate-limited, server and timeout errors with Retry-After or exponential backoff
inside a per-role deadline. Never retries auth or bad-request errors. Clock and sleep are injectable for tests."""
import asyncio
import heapq
import itertools
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.adapters.providers.openrouter import ProviderError

RETRYABLE = ("rate_limited", "server", "timeout")
PRIORITY = {"cue": 0, "gate": 1, "review": 2, "batch": 3}  # live cue first; tests and evaluations last
WINDOW_S = 60.0


class GaveUp(ProviderError):
    """The call did not succeed inside its deadline or attempt cap (the caller skips this run and says so)."""


class Limiter:
    """At most `per_min` requests in any 60 s window for one model; waiting callers are served by priority, then
    arrival. per_min 0 = unlimited."""

    def __init__(self, per_min: int, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self.per_min, self.clock, self.sleep = per_min, clock, sleep
        self.granted: deque[float] = deque()
        self.waiting: list[tuple[int, int]] = []
        self._seq = itertools.count()

    async def acquire(self, priority: int, deadline: float | None = None, on_wait=None) -> float:
        """Wait for a slot; returns the time waited. Raises GaveUp if the slot would come after `deadline`.
        on_wait(expected_s) is awaited once, when the caller first has to wait (so the UI can say so at once)."""
        if self.per_min <= 0:
            return 0.0
        start = self.clock()
        ticket = (priority, next(self._seq))
        heapq.heappush(self.waiting, ticket)
        told = False
        try:
            while True:
                now = self.clock()
                while self.granted and self.granted[0] <= now - WINDOW_S:
                    self.granted.popleft()
                if self.waiting[0] == ticket and len(self.granted) < self.per_min:
                    heapq.heappop(self.waiting)
                    self.granted.append(now)
                    return now - start
                free_at = self.granted[0] + WINDOW_S if len(self.granted) >= self.per_min else now
                if deadline is not None and free_at > deadline:
                    raise GaveUp(f"no request slot before the deadline ({self.per_min}/min limit)", "rate_limited")
                if on_wait is not None and not told:
                    told = True
                    await on_wait(max(0.0, free_at - now))
                await self.sleep(max(0.01, min(free_at - now, 0.25)))
        finally:
            if ticket in self.waiting:
                self.waiting.remove(ticket)
                heapq.heapify(self.waiting)


_limiters: dict[str, Limiter] = {}


class _Unlimited:
    """Limits for adapters built without settings (tests, scripts): no request limit, same retry rules."""
    requests_per_min: dict = {}
    deadline_s = {"gate": 8.0, "cue": 8.0, "review": 60.0, "batch": 60.0}
    max_attempts, backoff_start_s, backoff_cap_s = 4, 1.0, 8.0


UNLIMITED = _Unlimited()


def limiter_for(provider: str, model: str, limits) -> Limiter:
    """Shared per model: "provider:model" in limits.requests_per_min wins, then "provider", else unlimited."""
    key = f"{provider}:{model}"
    per_min = limits.requests_per_min.get(key, limits.requests_per_min.get(provider, 0))
    lim = _limiters.get(key)
    if lim is None or lim.per_min != per_min:
        lim = _limiters[key] = Limiter(per_min)
    return lim


@dataclass
class CallInfo:
    attempt: int = 0
    queue_wait_s: float = 0.0
    retry_wait_s: float = 0.0
    status_codes: list[int] = field(default_factory=list)


Listener = Callable[[str, dict], Awaitable[None]]  # ("waiting" | "gave_up", details)


class Caller:
    """One per role and adapter. `call(fn)` runs fn() with the limiter, retries and deadline; records a ledger row
    for every failed attempt (ok=false, no cost); the adapter records the successful one with `info`."""

    def __init__(self, role: str, provider: str, model: str, ledger, limits, *, batch: bool = False,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep, limiter: Limiter | None = None):
        self.role, self.provider, self.model, self.ledger, self.limits = role, provider, model, ledger, limits
        self.priority = PRIORITY["batch" if batch else role]
        self.deadline_s = limits.deadline_s.get("batch" if batch else role, 60.0)
        self.clock, self.sleep = clock, sleep
        self.limiter = limiter or limiter_for(provider, model, limits)
        self.listener: Listener | None = None

    async def _tell(self, kind: str, details: dict) -> None:
        if self.listener:
            await self.listener(kind, {"role": self.role, "model": f"{self.provider}:{self.model}", **details})

    async def call(self, fn: Callable[[], Awaitable[dict]]) -> tuple[dict, CallInfo]:
        info, start = CallInfo(), self.clock()
        deadline = start + self.deadline_s
        last: ProviderError | None = None
        while info.attempt < self.limits.max_attempts:
            info.attempt += 1
            self.ledger.check_budget()  # before every attempt, retries included
            try:
                waited = await self.limiter.acquire(self.priority, deadline, on_wait=lambda s: self._tell(
                    "waiting", {"wait_s": round(s, 1), "reason": "queued"}))
            except GaveUp as e:
                last = e
                break
            info.queue_wait_s += waited
            if waited > 0:
                await self._tell("queued", {"wait_s": round(waited, 2)})  # measured queue wait, for the report
            try:
                return await fn(), info
            except ProviderError as e:
                last = e
                if e.status:
                    info.status_codes.append(e.status)
                self.ledger.record(self.role, self.provider, self.model, ok=False, error=str(e)[:300], status=e.status,
                                   attempt=info.attempt, waited_s=round(info.queue_wait_s + info.retry_wait_s, 2))
                if e.kind not in RETRYABLE:
                    raise
                wait = e.retry_after_s if e.retry_after_s is not None else min(
                    self.limits.backoff_cap_s, self.limits.backoff_start_s * 2 ** (info.attempt - 1)) * random.uniform(0.8, 1.2)
                if self.clock() + wait > deadline or info.attempt >= self.limits.max_attempts:
                    break
                await self._tell("waiting", {"wait_s": round(wait, 1), "attempt": info.attempt, "status": e.status,
                                             "reason": e.kind})
                await self.sleep(wait)
                info.retry_wait_s += wait
        detail = f"{last}" if last else "no attempt made"
        await self._tell("gave_up", {"attempts": info.attempt, "reason": (last.kind if last else "deadline")})
        raise GaveUp(f"gave up after {info.attempt} attempt(s) in {self.clock() - start:.1f} s: {detail}"[:300],
                     "rate_limited" if last is not None and last.kind == "rate_limited" else (last.kind if last else "other"),
                     last.status if last else None)
