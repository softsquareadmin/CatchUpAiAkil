"""Session loop: input -> transcript store -> gate (serialised, batched) -> validated checklist updates
-> cue writer (only for topics with a follow-up) -> follow-up cards; flags, notes, consent and end checks."""
import asyncio
import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from app.adapters.analysts import Analyst, build_analyst
from app.adapters.deciders import Decider, build_decider
from app.adapters.transcribers.assemblyai import AssemblyAITranscriber
from app.adapters.transcribers.replay import ReplayTranscriber
from app.adapters.transcribers.typed import TypedTranscriber
from app.audit import AuditLog
from app.config import Settings
from app.cues import CueBoard, Lint, audience_hint, open_required_topics, roles_named
from app.audio_file import BYTES_PER_SECOND
from app.evidence import validate
from app.ledger import BudgetExceeded, Ledger
from app.limits import PRIORITY
from app import review_actions
from app.review import (EmptyCaseContext, build_items, build_topic_results, checklist_summary, lost_sections, retry_note,
                        section_titles, topic_coverage)
from app.models import (ChecklistItem, Cue, Flag, GateResult, ReviewItem, Speaker, TopicState, TopicUpdate,
                        UseCasePack, Utterance)
from app.store import EventLog, TranscriptStore

log = logging.getLogger(__name__)
Send = Callable[[dict], Awaitable[None]]
CUE_WINDOW = 8  # utterances the cue writer sees


def new_session_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def apply_updates(state: dict[str, TopicState], updates: list[TopicUpdate], transcript: dict[str, Utterance],
                  now_ms: int) -> tuple[list[TopicState], list[dict]]:
    """Apply gate deltas to state in place. Evidence that does not validate is dropped; an update left
    with no valid evidence is dropped. Returns (changed states, rejections to log)."""
    changed, rejected = [], []
    for up in updates:
        old = state.get(up.item_id)
        if old is None:
            rejected.append({"item_id": up.item_id, "reason": "unknown_topic"})
            continue
        if up.status == "not_covered":
            rejected.append({"item_id": up.item_id, "reason": "cannot_set_not_covered"})
            continue
        good = []
        for ev in up.evidence:
            if validate(ev, transcript):
                good.append(ev)
            else:
                rejected.append({"item_id": up.item_id, "reason": "evidence_not_in_transcript",
                                 "evidence": ev.model_dump()})
        if not good:
            rejected.append({"item_id": up.item_id, "status": up.status, "reason": "no_valid_evidence"})
            continue
        new_ev = [e for e in good if e not in old.evidence]
        if up.status == old.status and up.follow_up == old.follow_up and not new_ev:
            continue
        state[up.item_id] = TopicState(item_id=up.item_id, status=up.status, follow_up=up.follow_up,
                                       follow_up_reason=up.follow_up_reason if up.follow_up != "none" else "",
                                       evidence=old.evidence + new_ev, updated_at_ms=now_ms,
                                       rationale_short=up.rationale_short)
        changed.append(state[up.item_id])
    return changed, rejected


def pack_info(pk: UseCasePack, file: str | None = None) -> dict:
    """What the screens need to know about a pack (live screen, review page)."""
    return {"id": pk.id, "version": pk.version, "ref": pk.ref, "name": pk.name or pk.id, "purpose": pk.purpose,
            "roles": [r.model_dump() for r in pk.roles], "user_role": pk.user_role.id,
            "consent_topic": pk.special_topics.consent_topic, "has_sample": bool(pk.sample_script),
            "show_coverage_score": pk.review.show_coverage_score, "file": file}


class Session:
    def __init__(self, settings: Settings, pack: UseCasePack, sessions_dir: Path, send: Send,
                 decider: Decider | None = None, session_id: str | None = None, analyst: Analyst | None = None,
                 batch: bool = False):
        self.id = session_id or new_session_id()
        self.settings, self.pack, self.send = settings, pack, send
        self.dir = Path(sessions_dir) / self.id
        self.store = TranscriptStore(self.dir)
        self.events = EventLog(self.dir)
        self.audit = AuditLog(self.dir, self.id)
        self.ledger = Ledger(self.dir, self.id, settings.experiment_label, settings.max_session_cost_usd, pack=pack.ref)
        self.lint = Lint(pack)
        self.started = time.monotonic()  # reset when the interview begins (Start interview, or the first input)
        self.begun = False
        self.typed = TypedTranscriber()
        self.replay: ReplayTranscriber | None = None
        self.replay_task: asyncio.Task | None = None
        self.checklist = {c.id: TopicState(item_id=c.id) for c in pack.checklist}
        self.flags: list[Flag] = []
        if decider is None:
            decider, self.gate_off_reason = build_decider(settings, pack, self.ledger)
        else:
            self.gate_off_reason = ""
        self.decider = decider
        self.gate_task: asyncio.Task | None = None
        self.gate_pending = False
        self.gate_read = 0  # number of stored utterances already sent to the gate
        if analyst is None:
            analyst, self.cue_off_reason = build_analyst(settings, pack, self.ledger)
        else:
            self.cue_off_reason = ""
        self.analyst = analyst
        self.batch = batch  # tests and evaluations: lowest priority for the request limiter, longer deadline (M10a)
        self.health: dict = {"delayed_s": None, "skipped_lines": 0, "skipped_ranges": [], "skipped_cues": 0}
        self._wire_callers(self.decider, self.analyst)
        self.board = CueBoard(settings.cue_cooldown_seconds)
        self.clock = time.monotonic  # cue cooldown clock (tests replace it)
        self.cue_tasks: set[asyncio.Task] = set()
        self.cleared_at: dict[str, int] = {}  # topic -> transcript length when the user cleared its follow-up
        self.cue_basis: dict[str, tuple[str, int]] = {}  # card id -> (follow-up reason, transcript length) when written
        self.last_line_at: float | None = None  # clock() at the last final line: the suggested-card hold
        self.hold_task: asyncio.Task | None = None
        self.line_labels: dict[str, tuple[str | None, int]] = {}  # line id -> (voice label, AssemblyAI connection no.)
        self.notes: list[dict] = []
        self.stopped = ""  # why the tool was stopped; "" while running
        self.ending_prompted = False
        self.mic: AssemblyAITranscriber | None = None
        self.label_source: AssemblyAITranscriber | None = None  # last mic, also while it is stopping
        self.connection: object | None = None  # the browser connection that currently owns this session
        self.mic_state = "off"
        self.speaker_map: dict[str, Speaker] = {}  # AssemblyAI label (A, B, ...) -> role id, set by the user
        self.labels_seen: list[str] = []
        self.manual_speaker: Speaker | None = None  # user's toggle; overrides labels while set
        self.audio_file = None
        self.mic_source = "mic"  # "file" while an audio file streams through the same transcriber (M8)
        self.file_task: asyncio.Task | None = None
        self.file_info: dict = {"state": "idle"}
        self.file_pace = 1.0  # 1 = real time (AssemblyAI requires it); tests set 0
        self.draft: dict | None = None  # post-interview review (draft_note.json)
        self.review_state = "none"  # none | running | ready | failed
        self.review_error: str | None = None
        self.ledger.on_record = lambda row: self._spawn(self.send({"type": "experiment", "experiment": self.experiment_status()}))
        self.audit.write("system", "session_start", experiment=settings.experiment_label,
                         gate_model=settings.models["gate"], gate_off_reason=self.gate_off_reason or None,
                         pack=pack.ref, models=dict(settings.models), gate_adapter=settings.gate_adapter,
                         recommendation_enabled=pack.rails.allow_overall_recommendation)

    def elapsed_ms(self) -> int:
        """Interview clock: 0 until the interview begins."""
        return int((time.monotonic() - self.started) * 1000) if self.begun else 0

    async def begin(self, how: str = "start_button") -> None:
        """The interview begins: the clock starts at 0. The page connects on load but waits for Start interview; any
        input (replay, typed, paste, mic, file) also begins it. Does nothing the second time."""
        if self.begun:
            return
        self.begun, self.started = True, time.monotonic()
        self.audit.write("user" if how == "start_button" else "system", "interview_started", how=how)
        await self.send({"type": "started"})

    def pack_info(self) -> dict:
        return pack_info(self.pack, getattr(self, "pack_file", None))

    def started_input(self) -> bool:
        """Locks the pack choice: true once the interview began, anything was said or any model was called."""
        return bool(self.begun or self.store.utterances or self.ledger.rows)

    def hello(self) -> dict:
        return {"type": "session", "session_id": self.id, "pack": self.pack_info(), "elapsed_ms": self.elapsed_ms(),
                "begun": self.begun,
                "checklist": [c.model_dump() for c in self.pack.checklist],
                "state": {k: v.model_dump() for k, v in self.checklist.items()},
                "gate": self.gate_status(), "cue": self.cue_status(),
                # everything the screen shows, so a reconnecting browser can rebuild it
                "utterances": [u.model_dump() for u in self.store.utterances],
                "cues": [{"cue": c.model_dump(), "level": self.board.level.get(c.id, "")} for c in self.board.cues.values()
                         if c.state == "active" and (c.kind == "flag" or self.board.active.get(c.topic_id) == c.id)],
                "notes": self.notes, "stopped": self.stopped, "mic": self.mic_status(),
                "review": self.review_message(), "experiment": self.experiment_status(), "audio_file": self.file_info,
                "health": self.health_snapshot()}

    def mic_status(self) -> dict:
        return {"state": self.mic_state, "model": self.settings.models["stt"], "labels": self.labels_seen,
                "mapping": self.speaker_map, "manual": self.manual_speaker,
                "speaker_labels": self.settings.stt_speaker_labels, "source": self.mic_source}

    # ---- provider health (M10a): delays and skipped analysis are shown, logged and audited ----
    def _wire_callers(self, *adapters) -> None:
        for a in adapters:
            for c in [getattr(a, "caller", None), getattr(getattr(a, "jev", None), "caller", None),
                      getattr(getattr(a, "llm", None), "caller", None)]:
                if c is not None:
                    c.listener = self._provider_event
                    if self.batch:
                        c.priority = PRIORITY["batch"]
                        c.deadline_s = self.settings.limits.deadline_s.get("batch", 60.0)

    async def _provider_event(self, kind: str, d: dict) -> None:
        if kind == "queued":  # the measured wait for a request slot (the report's "longest queue wait")
            self.events.write("provider_queue", role=d["role"], model=d["model"], wait_s=d["wait_s"])
            return
        if kind == "waiting":
            if d.get("reason") != "queued":  # retry waits; queue waits are logged when measured ("provider_queue")
                self.events.write("provider_wait", role=d["role"], model=d["model"], wait_s=d["wait_s"],
                                  reason=d.get("reason"), attempt=d.get("attempt"))
            if d.get("reason") == "rate_limited":
                self.audit.write("system", "provider_rate_limited", role=d["role"], model=d["model"],
                                 wait_s=d["wait_s"], attempt=d.get("attempt"))
            self.health["delayed_s"] = d["wait_s"]
            await self.send({"type": "health", "health": self.health_snapshot()})

    def health_snapshot(self) -> dict:
        return {**self.health, "skipped_ranges": [list(r) for r in self.health["skipped_ranges"]]}

    async def _analysis_ok(self) -> None:
        if self.health["delayed_s"] is not None:
            self.health["delayed_s"] = None
            await self.send({"type": "health", "health": self.health_snapshot()})

    async def _skipped(self, role: str, reason: str, first: str | None = None, last: str | None = None,
                       lines: int = 0) -> None:
        self.audit.write("system", "analysis_skipped", role=role, reason=reason[:120], first=first, last=last)
        self.events.write("analysis_skipped", role=role, reason=reason[:300], first=first, last=last, lines=lines)
        if role == "gate":
            self.health["skipped_lines"] += lines
            self.health["skipped_ranges"].append([first, last])
        else:
            self.health["skipped_cues"] += 1
        self.health["delayed_s"] = None
        await self.send({"type": "health", "health": self.health_snapshot()})

    def experiment_status(self) -> dict:
        """For the experiment bar: label, models, and this session's running cost and latency per role."""
        roles = {}
        for r in self.ledger.rows:
            x = roles.setdefault(r.role, {"calls": 0, "failed": 0, "cost_usd": 0.0, "latencies": [], "seconds": 0.0})
            x["calls"] += 1
            x["failed"] += not r.ok
            x["cost_usd"] += r.cost_usd
            x["seconds"] += r.audio_seconds
            if r.ok and r.latency_ms:
                x["latencies"].append(r.latency_ms)
        for x in roles.values():
            lat = sorted(x.pop("latencies"))
            x.update(cost_usd=round(x["cost_usd"], 4), p50_ms=lat[len(lat) // 2] if lat else None,
                     p95_ms=lat[min(len(lat) - 1, round(0.95 * (len(lat) - 1)))] if lat else None)
        return {"label": self.ledger.experiment, "editable": not self.ledger.rows, "models": dict(self.settings.models),
                "gate_adapter": self.settings.gate_adapter, "roles": roles, "total_usd": round(self.ledger.total_usd, 4),
                "cap_usd": self.settings.max_session_cost_usd}

    async def set_experiment(self, label: str) -> str | None:
        """The UI's experiment label. Only before the first call, so every ledger row of a session has one label."""
        label = label.strip()
        if self.ledger.rows:
            return "The experiment label can only be changed before the first model call of a session."
        if not label or len(label) > 60 or not all(c.isalnum() or c in " ._:-=+" for c in label):
            return "Label: 1-60 letters, digits, spaces or . _ : - = +"
        self.ledger.experiment = label
        self.audit.write("user", "experiment_label", label=label)
        await self.send({"type": "experiment", "experiment": self.experiment_status()})
        return None

    def cue_status(self) -> dict:
        return {"enabled": self.analyst is not None, "reason": self.cue_off_reason, "model": self.settings.models["cue"]}

    def gate_status(self, **extra) -> dict:
        s = self.settings
        llm = f"{s.models['gate']} ({s.gate_prompt} prompt)"
        model = {"jev": f"jev:{s.jev_model} ({s.jev_questions})",
                 "hybrid": f"jev:{s.jev_model} fast check (conf >= {s.hybrid_min_confidence}) + {llm}",
                 }.get(s.gate_adapter, llm)
        return {"enabled": self.decider is not None, "reason": self.gate_off_reason, "model": model,
                "adapter": s.gate_adapter, **extra}

    # ---- transcript ----
    async def on_utterance(self, u: Utterance) -> None:
        if self.stopped:
            return
        await self.begin("first_line")  # normally already begun by Start interview or the input that made the line
        if u.is_final:
            u = self.store.append(u)
        await self.send({"type": "utterance", "utterance": u.model_dump()})
        if u.is_final:
            self.last_line_at = self.clock()
        if u.is_final and u.source in ("mic", "file") and self.label_source is not None:
            self.line_labels[u.id] = (self.label_source.last_label, self.label_source.connections)
            self.events.write("stt_label", utterance_id=u.id, label=self.label_source.last_label,
                              confidence=self.label_source.last_label_confidence, speaker=u.speaker,
                              **({"part": self.label_source.last_part} if self.label_source.last_part else {}),
                              words=self.label_source.last_words)
        if u.is_final:
            self.request_gate()
            if u.speaker == self.pack.user_role.id:
                self.schedule_cues()  # the answer is over: held suggested cards are written now, not after the gate
            if not self.ending_prompted and self.lint.seems_ending(u.text):
                self.ending_prompted = True
                self._spawn(self._before_leave_after_gate(u))

    async def _before_leave_after_gate(self, u: Utterance) -> None:
        await self.gate_idle()  # so the list reflects this last line
        await self.before_leave("ending_detected", u)

    # ---- gate: never two runs at once; utterances arriving mid-run are batched into the next run ----
    def request_gate(self) -> None:
        if self.decider is None:
            return
        self.gate_pending = True
        if self.gate_task is None or self.gate_task.done():
            self.gate_task = asyncio.create_task(self._gate_loop())

    async def gate_idle(self) -> None:
        while self.gate_task and not self.gate_task.done():
            await self.gate_task

    async def _gate_loop(self) -> None:
        while self.gate_pending and self.decider is not None and not self.stopped:
            # Every utterance must appear in some window: take the next unread ones (at most N),
            # padded with earlier ones for context. Loop again if more are still unread.
            n, total = self.settings.window_utterances, len(self.store.utterances)
            start = self.gate_read
            end = min(total, self.gate_read + n)
            window = self.store.utterances[max(0, end - n):end]
            self.gate_read = end
            self.gate_pending = end < total
            t0 = time.monotonic()
            ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
            try:
                if getattr(self.decider, "staged", False):
                    async def on_partial(fast: GateResult) -> None:
                        await self.apply_gate_result(fast, window, ms(), stage="fast")
                    result = await self.decider.evaluate(window, dict(self.checklist), on_partial=on_partial)
                else:
                    result = await self.decider.evaluate(window, dict(self.checklist))
            except BudgetExceeded as e:
                self.decider, self.gate_off_reason = None, str(e)
                self.audit.write("system", "budget_exceeded", message=str(e))
                await self.send({"type": "gate_status", "gate": self.gate_status()})
                return
            except Exception as e:  # keep the live loop alive; the error is logged and shown
                log.exception("gate run failed")
                self.events.write("gate_error", error=repr(e)[:300])
                await self.send({"type": "gate_status", "gate": self.gate_status(error=repr(e)[:200])})
                continue
            if getattr(self.decider, "gave_up", False):  # the provider would not answer in time: say so, carry on
                new = self.store.utterances[start:end]
                await self._skipped("gate", self.decider.last_error or "gave up", new[0].id if new else None,
                                    new[-1].id if new else None, len(new))
            else:
                await self._analysis_ok()
            await self.apply_gate_result(result, window, ms(), stage="final")

    async def apply_gate_result(self, result: GateResult, window: list[Utterance], latency_ms: int,
                                stage: str = "final") -> None:
        transcript = {u.id: u for u in self.store.utterances}
        self._keep_user_clears(result.updates)
        changed, rejected = apply_updates(self.checklist, result.updates, transcript, self.elapsed_ms())
        flags = [f for f in result.flags if validate(f.evidence, transcript)]
        rejected += [{"flag": f.model_dump(), "reason": "evidence_not_in_transcript"}
                     for f in result.flags if f not in flags]
        self.flags += flags
        for r in rejected:
            self.events.write("rejected", **r)
        for s in changed:
            self.events.write("topic_update", state=s.model_dump(), stage=stage, latency_ms=latency_ms)
            self.audit.write("system", "ai_output_shown", kind="topic_update", item_id=s.item_id, status=s.status)
            await self.send({"type": "topic_update", "state": s.model_dump()})
        for f in flags:
            self.events.write("flag", flag=f.model_dump())
            await self.on_flag(f)
        if result.answer_quality:
            self.events.write("answer_quality", **result.answer_quality.model_dump())
        await self.send({"type": "gate_status", "gate": self.gate_status(
            latency_ms=latency_ms, stage=stage, error=getattr(self.decider, "last_error", None),
            fast_topics=getattr(self.decider, "last_fast", None) if stage == "fast" else None,
            window_last=window[-1].id if window else None, cost_usd=round(self.ledger.total_usd, 4))})
        await self.after_topic_changes(changed)

    def _keep_user_clears(self, updates: list[TopicUpdate]) -> None:
        """After the user clears a follow-up, the gate may raise it again only with evidence from a later line."""
        pos = {u.id: i for i, u in enumerate(self.store.utterances)}
        for up in updates:
            since = self.cleared_at.get(up.item_id)
            if since is not None and up.follow_up != "none" and \
                    not any(pos.get(e.utterance_id, -1) >= since for e in up.evidence):
                up.follow_up, up.follow_up_reason = "none", ""

    # ---- flags and cues ----
    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self.cue_tasks.add(t)
        t.add_done_callback(self.cue_tasks.discard)

    async def on_flag(self, f: Flag) -> None:
        consent = f.topic_id == self.pack.special_topics.consent_topic  # one consent prompt per line, whatever it names
        roles = frozenset() if consent else roles_named(self.pack, f.note)
        if not self.board.is_new_flag(f.topic_id, f.evidence.utterance_id, roles) or self.stopped:
            return
        if f.topic_id == self.pack.special_topics.consent_topic:  # the pack asks the gate to flag a refusal here
            self.audit.write("system", "consent_refusal_detected", note=f.note, evidence=f.evidence.model_dump())
            await self.send({"type": "consent_check", "note": f.note, "evidence": f.evidence.model_dump()})
            return
        if lint := self.lint.note(f.note):
            self.events.write("rejected", flag=f.model_dump(), reason=f"lint: {lint}")
            return
        cue = self.board.add(Cue(id="", topic_id=f.topic_id, kind="flag", question_or_note=f.note,
                                 evidence=f.evidence, created_at_ms=self.elapsed_ms()))
        self.events.write("cue", cue=cue.model_dump())
        self.audit.write("system", "ai_output_shown", kind="flag", cue_id=cue.id, topic_id=cue.topic_id)
        await self.send({"type": "cue", "cue": cue.model_dump()})

    async def after_topic_changes(self, changed: list[TopicState]) -> None:
        for s in changed:  # an active card no longer matches its topic's follow-up level
            cue_id = self.board.active.get(s.item_id)
            if cue_id and self.board.level.get(cue_id) != s.follow_up:
                self.board.withdraw(s.item_id)
                self.events.write("cue_withdrawn", cue_id=cue_id, reason=f"follow-up now {s.follow_up}")
                await self.send({"type": "cue_withdrawn", "id": cue_id})
        self.schedule_cues()

    def answer_open(self) -> bool:
        """An interviewee may still be answering: the last line is not the user's and it came less than cue_hold_s
        ago. Suggested cards wait meanwhile (AssemblyAI ends a turn at every short pause, so the first part of an
        answer often looks incomplete); required cards never wait."""
        if not self.settings.cue_hold_suggested or self.last_line_at is None or not self.store.utterances:
            return False
        if self.store.utterances[-1].speaker == self.pack.user_role.id:
            return False
        return self.clock() - self.last_line_at < self.settings.cue_hold_s

    def _stale(self, cue_id: str, s: TopicState) -> bool:
        """A card is out of date when the follow-up reason changed and the topic has evidence from a line that came
        after the card was written (a reworded reason alone is not enough)."""
        reason, lines = self.cue_basis.get(cue_id, (s.follow_up_reason, len(self.store.utterances)))
        pos = {u.id: i for i, u in enumerate(self.store.utterances)}
        return bool(s.follow_up_reason) and s.follow_up_reason != reason and \
            any(pos.get(e.utterance_id, -1) >= lines for e in s.evidence)

    def schedule_cues(self) -> None:
        """Write a card for every topic with a follow-up and no active card, unless it is cooling down; rewrite an
        active card that is out of date. Suggested cards (new or rewritten) wait while an answer is still open."""
        if self.analyst is None or self.stopped:
            return
        now, held = self.clock(), False
        for item in self.pack.checklist:
            s = self.checklist[item.id]
            level = s.follow_up
            if level == "none" or item.id in self.board.inflight:
                continue
            cue_id = self.board.active.get(item.id)
            if cue_id and not self._stale(cue_id, s):
                continue
            if not cue_id and not self.board.can_write(item.id, now, level):
                continue
            if level == "suggested" and self.answer_open():
                held = True
                continue
            self.board.inflight.add(item.id)
            self._spawn(self._write_cue(item, level, replaces=cue_id))
        if held:
            self._hold_timer()

    def _hold_timer(self) -> None:
        """Look again once the hold time has passed without a new line (a new line re-runs the gate anyway)."""
        if self.hold_task and not self.hold_task.done():
            return

        async def later():
            await asyncio.sleep(max(0.0, self.settings.cue_hold_s - (self.clock() - (self.last_line_at or 0))) + 0.05)
            self.schedule_cues()
        self.hold_task = asyncio.create_task(later())

    async def _write_cue(self, item: ChecklistItem, level: str, replaces: str | None = None) -> None:
        t0, cue = time.monotonic(), None
        transcript = {u.id: u for u in self.store.utterances}
        window = self.store.utterances[-CUE_WINDOW:]
        analyst = self.analyst
        try:
            cue = await analyst.write_cue(item, self.checklist[item.id], window,
                                          audience_hint(self.pack, self.checklist, transcript))
        except BudgetExceeded as e:
            self.analyst, self.cue_off_reason = None, str(e)
            self.audit.write("system", "budget_exceeded", message=str(e))
            await self.send({"type": "cue_status", "cue": self.cue_status()})
        except Exception as e:  # keep the live loop alive
            log.exception("cue writer failed")
            self.events.write("cue_error", topic_id=item.id, error=repr(e)[:300])
        finally:
            self.board.inflight.discard(item.id)
        for r in getattr(analyst, "last_rejections", []):
            self.events.write("cue_rejected", **r)
        if getattr(analyst, "gave_up", False):
            await self._skipped("cue", analyst.last_error or "gave up")
        s = self.checklist[item.id]
        if cue is None or not validate(cue.evidence, transcript):
            if replaces:  # keep the card on screen; do not try again until the reason changes again
                self.cue_basis[replaces] = (s.follow_up_reason, len(self.store.utterances))
            else:
                self.board.failed(item.id, level)  # retried on the next gate run, a limited number of times
            self.events.write("cue_skipped", topic_id=item.id, level=level, error=getattr(analyst, "last_error", None),
                              **({"replaces": replaces} if replaces else {}))
            return
        if self.stopped or s.follow_up != level or (replaces and self.board.active.get(item.id) != replaces):
            self.events.write("cue_discarded", topic_id=item.id, reason="follow-up or card changed while writing")
            self.schedule_cues()
            return
        if replaces:
            self.board.withdraw(item.id)
            self.events.write("cue_withdrawn", cue_id=replaces, reason="rewritten")
            await self.send({"type": "cue_withdrawn", "id": replaces})
        latency = int((time.monotonic() - t0) * 1000)
        cue = self.board.add(cue.model_copy(update={"created_at_ms": self.elapsed_ms(), "updated": bool(replaces)}),
                             level)
        self.cue_basis[cue.id] = (s.follow_up_reason, len(self.store.utterances))
        self.events.write("cue", cue=cue.model_dump(), level=level, latency_ms=latency,
                          since_utterance_ms=self.elapsed_ms() - window[-1].t_end_ms)
        self.audit.write("system", "ai_output_shown", kind="cue", cue_id=cue.id, topic_id=cue.topic_id,
                         **({"replaces": replaces} if replaces else {}))
        await self.send({"type": "cue", "cue": cue.model_dump(), "level": level, "latency_ms": latency})

    async def cue_idle(self) -> None:
        while pending := [t for t in self.cue_tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- user actions ----
    async def cue_action(self, cue_id: str, action: str) -> str | None:
        """Asked or dismissed. Asked clears a suggested follow-up; dismissed clears suggested or required."""
        if action not in ("asked", "dismissed"):
            return "action must be asked or dismissed"
        cue = self.board.resolve(cue_id, action, self.clock())
        if cue is None:
            return "unknown or inactive card"
        self.events.write("cue_state", cue_id=cue.id, state=action)
        self.audit.write("user", f"cue_{action}", cue_id=cue.id, topic_id=cue.topic_id, kind=cue.kind)
        await self.send({"type": "cue_update", "cue": cue.model_dump()})
        s = self.checklist.get(cue.topic_id)
        if cue.kind == "follow_up" and s and (s.follow_up == "suggested" or
                                              (s.follow_up == "required" and action == "dismissed")):
            new = s.model_copy(update={"follow_up": "none", "follow_up_reason": "", "updated_at_ms": self.elapsed_ms()})
            self.checklist[s.item_id] = new
            self.cleared_at[s.item_id] = len(self.store.utterances)
            self.events.write("topic_update", state=new.model_dump(), stage="user", latency_ms=0)
            self.audit.write("user", "follow_up_cleared", item_id=s.item_id, was=s.follow_up, via=action)
            await self.send({"type": "topic_update", "state": new.model_dump()})
        return None

    async def add_note(self, text: str) -> str | None:
        text = text.strip()
        if not text:
            return "empty note"
        note = {"id": f"n{len(self.notes) + 1:03d}", "text": text[:2000], "t_ms": self.elapsed_ms()}
        self.notes.append(note)
        self.events.write("note", **note)
        self.audit.write("user", "note_added", note_id=note["id"])
        await self.send({"type": "note", "note": note})
        return None

    async def consent_decision(self, stop: bool) -> None:
        self.audit.write("user", "consent_refusal_confirmed" if stop else "consent_refusal_not_confirmed")
        if stop:
            await self.stop_tool("consent refused")

    async def before_leave(self, reason: str, u: Utterance | None = None) -> None:
        if self.stopped:
            return
        open_ = open_required_topics(self.pack, self.checklist)
        self.audit.write("system", "before_leave_shown", reason=reason, open=[o["id"] for o in open_])
        await self.send({"type": "before_leave", "reason": reason, "open": open_,
                         "utterance": u.model_dump() if u else None})

    async def end_interview(self) -> None:
        self.audit.write("user", "interview_ended")
        await self.stop_tool("interview ended")
        if self.review_state == "none":
            self.review_state = "running"
            await self.send({"type": "review", "review": self.review_message()})
            self._spawn(self.run_review())

    # ---- post-interview review (M5) ----
    def review_message(self) -> dict:
        return {"state": self.review_state, "model": self.settings.models["review"], "draft": self.draft,
                "error": self.review_error}

    async def run_review(self) -> None:
        reviewer, reason = build_analyst(self.settings, self.pack, self.ledger, role="review")
        self._wire_callers(reviewer)
        raw, err, retried, topics = None, reason or None, {}, None
        by_id = {u.id: u for u in self.store.utterances}
        if reviewer is not None:
            try:
                raw = await reviewer.review(self.store.utterances, dict(self.checklist), EmptyCaseContext().get())
                err = None if raw is not None else (getattr(reviewer, "last_error", None) or "no draft returned")
                if raw is not None:
                    self.events.write("review_raw", draft=raw)  # kept so validation changes can be re-checked
                    topics, items, rejected = self._check_review(raw, by_id)
                    if lost := lost_sections(self.pack, raw, items, rejected):
                        retried = await self._retry_sections(reviewer, lost, topics, by_id, items, rejected)
            except BudgetExceeded as e:
                err = None if raw is not None else str(e)
            except Exception as e:  # shown to the user; the transcript and checklist stay available
                log.exception("review failed")
                err = None if raw is not None else repr(e)[:300]
            finally:
                client = getattr(reviewer, "client", None)
                if client is not None:
                    await client.close()
        if raw is not None and topics is None:  # the draft came back but could not be checked
            raw, err = None, err or "the review draft could not be checked"
        if raw is None:
            self.review_state, self.review_error = "failed", err
            self.audit.write("system", "review_failed", error=err)
            await self.send({"type": "review", "review": self.review_message()})
            return
        for r in rejected:
            self.events.write("review_rejected", **r)
        self.draft = {"model": self.settings.models["review"], "pack": self.pack.ref,
                      "topics": topics, "coverage": topic_coverage(self.pack, topics),
                      "sections": section_titles(self.pack), "items": [i.model_dump() for i in items],
                      "notice": self.skipped_notice(), "retried_sections": retried,
                      "checklist": checklist_summary(self.pack, self.checklist), "rejected": len(rejected)}
        self.review_state = "ready"
        self.save_draft()
        self.audit.write("system", "ai_output_shown", kind="review", items=len(items), rejected=len(rejected))
        await self.send({"type": "review", "review": self.review_message()})

    def _check_review(self, raw: dict, by_id: dict) -> tuple[list[dict], list, list[dict]]:
        topics, rejected = build_topic_results(raw, self.pack, by_id, dict(self.checklist))
        items, more = build_items(raw, self.pack, by_id, {t["topic_id"]: t["status"] for t in topics},
                                  {t["topic_id"]: t["evidence"] for t in topics})
        return topics, items, rejected + more

    async def _retry_sections(self, reviewer, lost: dict, topics, by_id, items, rejected) -> dict:
        """One more call for the sections that came back empty after the checks (owner, 2026-10-08). Whatever passes
        the same checks is added; a section that fails again stays empty. Returns {section: "recovered" | "empty"}."""
        self.events.write("review_retry", sections=lost)
        try:
            raw2 = await reviewer.review(self.store.utterances, dict(self.checklist), EmptyCaseContext().get(),
                                         only=list(lost), note=retry_note(lost))
        except BudgetExceeded:
            raw2 = None
        if raw2 is None:
            return {sec: "empty" for sec in lost}
        self.events.write("review_raw", draft=raw2, retry=True)
        again, more = build_items(raw2, self.pack, by_id, {t["topic_id"]: t["status"] for t in topics},
                                  {t["topic_id"]: t["evidence"] for t in topics})
        items += [i for i in again if i.section in lost]
        rejected += [{**r, "retry": True} for r in more]
        return {sec: "recovered" if any(i.section == sec for i in again) else "empty" for sec in lost}

    def skipped_notice(self) -> str | None:
        """Review notice when live analysis was skipped (M10a). The review reads the whole transcript, so no extra
        model call is made for the skipped part."""
        ranges = self.health["skipped_ranges"]
        if not ranges:
            return None
        spans = ", ".join(f"{a} to {b}" if a != b else a for a, b in ranges)
        return (f"The live checklist may be incomplete for lines {spans}: the provider did not answer in time. "
                "The review below read the whole transcript, including those lines.")

    def save_draft(self) -> None:
        (self.dir / "draft_note.json").write_text(json.dumps(self.draft, indent=1), encoding="utf-8")

    async def review_action(self, item_id: str, action: str, value: str | None = None) -> str | None:
        return await self.review_decision({"kind": "item", "id": item_id, "action": action, "value": value})

    async def topic_action(self, topic_id: str, part: str, action: str, value=None) -> str | None:
        """accept / edit / reject / reset a topic result's summary, missing list or follow-up (M11)."""
        return await self.review_decision({"kind": "topic", "topic_id": topic_id, "part": part, "action": action,
                                           "value": value})

    async def topic_status(self, topic_id: str, status: str) -> str | None:
        return await self.review_decision({"kind": "status", "topic_id": topic_id, "status": status})

    async def review_decision(self, body: dict) -> str | None:
        """One decision from the live screen or the review page (app/review_actions.py)."""
        if self.draft is None:
            return "no review yet"
        err, msg = review_actions.apply(self.draft, self.audit, self.pack, body)
        if err:
            return err
        self.save_draft()
        await self.send(msg)
        return None

    def report_viewed(self, where: str) -> None:
        """Audited once per screen: the review screen was shown with a ready draft (M11 11.6)."""
        if self.draft is not None and not getattr(self, "_viewed", False):
            self._viewed = True
            self.audit.write("user", "report_viewed", where=where, topics=len(self.draft.get("topics", [])))

    async def stop_tool(self, reason: str) -> None:
        """Stop listening: no more input, gate or cue calls for this session."""
        if self.stopped:
            return
        self.stopped = reason
        await self.stop_replay()
        await self.stop_audio_file()
        await self.stop_mic()
        for t in list(self.cue_tasks):
            if t is not asyncio.current_task():
                t.cancel()
        self.audit.write("system", "tool_stopped", reason=reason)
        await self.send({"type": "stopped", "reason": reason})

    # ---- inputs ----
    def set_mode(self, mode: str) -> None:
        self.audit.write("user", "input_mode", mode=mode)

    async def start_typed(self) -> None:
        await self.typed.start(self.id, self.on_utterance)

    async def submit_typed(self, speaker: Speaker, text: str) -> str | None:
        if self.stopped:
            return f"The tool is stopped ({self.stopped})."
        if speaker not in self.pack.role_ids:
            return f"speaker must be one of: {', '.join(self.pack.role_ids)}"
        await self.begin("typed")
        await self.typed.submit(speaker, text, self.elapsed_ms())
        return None

    async def start_replay(self, lines: list[dict], speed: float, source: str) -> str | None:
        if self.stopped:
            return f"The tool is stopped ({self.stopped})."
        await self.stop_replay()
        await self.begin(source)
        self.replay = ReplayTranscriber(lines, speed, source=source, offset_ms=self.elapsed_ms())
        self.audit.write("user", "replay_start", source=source, speed=speed, lines=len(lines))

        async def run():
            await self.replay.start(self.id, self.on_utterance)
            await self.send({"type": "replay_done"})

        self.replay_task = asyncio.create_task(run())
        return None

    # ---- mic (AssemblyAI through the server) ----
    def speaker_for(self, label: str | None) -> Speaker:
        """Manual toggle first, then the user's label mapping, else unknown."""
        if label and label != "PENDING" and label not in self.labels_seen:
            self.labels_seen.append(label)
            self._spawn(self.send({"type": "mic_status", "mic": self.mic_status()}))
        if self.manual_speaker:
            return self.manual_speaker
        return self.speaker_map.get(label or "", "unknown")

    def build_mic(self, source: str = "mic") -> tuple[AssemblyAITranscriber | None, str]:
        provider, model = self.settings.model_for("stt")
        if provider != "assemblyai":
            return None, f"stt provider '{provider}' is not supported (use assemblyai:...)"
        if not self.settings.has_key("ASSEMBLYAI_API_KEY"):
            return None, "ASSEMBLYAI_API_KEY is not set"
        mic = AssemblyAITranscriber(
            self.settings.secrets["ASSEMBLYAI_API_KEY"].get_secret_value(), model, self.ledger,
            speaker_for=self.speaker_for, elapsed_ms=self.elapsed_ms,
            speaker_labels=self.settings.stt_speaker_labels, max_speakers=self.settings.stt_max_speakers,
            source=source, split_turns=self.settings.stt_split_turns)
        mic.on_status = lambda state, detail="": self._mic_status(state, detail, mic)
        return mic, ""

    async def _mic_status(self, state: str, detail: str = "", source=None) -> None:
        if source is not None and self.mic is not None and source is not self.mic:
            return  # a mic that is shutting down; a newer one is running
        self.mic_state = state
        if detail == "reconnected" and (self.speaker_map or self.labels_seen):
            # AssemblyAI labels restart on a new connection: "A" may now be someone else
            self.audit.write("system", "speaker_labels_reset", mapping=self.speaker_map)
            self.speaker_map, self.labels_seen = {}, []
            detail = "reconnected: voice labels reset, please map voices again"
        if state in ("reconnecting", "failed"):
            self.audit.write("system", f"mic_{state}", detail=detail)
        await self.send({"type": "mic_status", "mic": self.mic_status(), "detail": detail})

    async def start_mic(self) -> str | None:
        if self.stopped:
            return f"The tool is stopped ({self.stopped})."
        if self.file_playing():
            return "An audio file is being transcribed. Stop it first."
        if self.mic is not None:
            return None
        await self.begin("mic")
        mic, reason = self.build_mic()
        if mic is None:
            return f"Mic is unavailable: {reason}"
        try:
            self.ledger.check_budget()
        except BudgetExceeded as e:
            return str(e)
        await self.stop_replay()
        self.mic = self.label_source = mic
        self.mic_source = "mic"
        if self.settings.store_audio and self.audio_file is None:
            self.audio_file = (self.dir / "audio.pcm").open("ab")
            self.audit.write("system", "audio_stored", path="audio.pcm", format="pcm_s16le 16 kHz mono")
        self.audit.write("user", "mic_start", model=self.settings.models["stt"],
                         speaker_labels=self.settings.stt_speaker_labels)
        await mic.start(self.id, self.on_utterance)
        return None

    async def mic_audio(self, chunk: bytes) -> None:
        if self.mic is None or self.stopped:
            return
        if self.audio_file is not None:
            self.audio_file.write(chunk)
        await self.mic.send_audio(chunk)

    async def stop_mic(self) -> None:
        mic, self.mic = self.mic, None  # a new mic may start while this one finishes
        if mic is None:
            return
        await mic.stop()  # waits for the last final turn, which still goes through on_utterance
        if self.audio_file is not None:
            self.audio_file.close()
            self.audio_file = None
        self.audit.write("user", "mic_stop", connections=mic.connections)

    # ---- audio file (M8): the decoded file goes through the same transcriber as the mic, at real-time pace ----
    def file_playing(self) -> bool:
        return self.file_task is not None and not self.file_task.done()

    async def _file_status(self, **info) -> None:
        self.file_info = info
        await self.send({"type": "audio_file", "audio_file": info})

    async def start_audio_file(self, pcm: bytes, name: str) -> str | None:
        """Start streaming decoded PCM16 16 kHz mono. Returns an error message or None."""
        if self.stopped:
            return f"The tool is stopped ({self.stopped})."
        if self.file_playing():
            return "An audio file is already being transcribed."
        await self.begin("audio_file")
        mic, reason = self.build_mic(source="file")
        if mic is None:
            return f"Audio files are unavailable: {reason}"
        try:
            self.ledger.check_budget()
        except BudgetExceeded as e:
            return str(e)
        await self.stop_replay()
        await self.stop_mic()
        self.mic = self.label_source = mic
        self.mic_source = "file"
        total = round(len(pcm) / BYTES_PER_SECOND, 1)
        if self.settings.store_audio:
            (self.dir / "audio_file.pcm").write_bytes(pcm)
            self.audit.write("system", "audio_stored", path="audio_file.pcm", format="pcm_s16le 16 kHz mono")
        self.audit.write("user", "audio_file_start", name=Path(name).name[:120], seconds=total,
                         model=self.settings.models["stt"], speaker_labels=self.settings.stt_speaker_labels)
        await self._file_status(state="streaming", name=Path(name).name[:120], done_s=0.0, total_s=total)
        await mic.start(self.id, self.on_utterance)
        self.file_task = asyncio.create_task(self._stream_file(mic, pcm, Path(name).name[:120], total))
        return None

    async def _stream_file(self, mic: AssemblyAITranscriber, pcm: bytes, name: str, total: float) -> None:
        chunk, sent, last_report = BYTES_PER_SECOND // 10, 0, -1.0  # 100 ms frames
        clock = time.monotonic()
        state, detail = "done", ""
        try:
            while sent < len(pcm):
                if not mic.running:
                    state, detail = "failed", "the transcription connection could not be restored"
                    break
                if mic.ws is None:  # (re)connecting: pause playback so no audio is dropped, then carry on
                    await asyncio.sleep(0.05)
                    clock = time.monotonic() - (sent / BYTES_PER_SECOND) * self.file_pace
                    continue
                await mic.send_audio(pcm[sent:sent + chunk])
                sent += chunk
                done = sent / BYTES_PER_SECOND
                if done - last_report >= 1 or sent >= len(pcm):
                    last_report = done
                    await self._file_status(state="streaming", name=name, done_s=round(min(done, total), 1), total_s=total)
                if self.file_pace:
                    await asyncio.sleep(max(0.0, clock + done * self.file_pace - time.monotonic()))
                else:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            state = "stopped"
        finally:
            if self.mic is mic:
                await self.stop_mic()  # Terminate: the last final turn still arrives
            self.audit.write("system" if state != "stopped" else "user", f"audio_file_{state}", name=name,
                             seconds_sent=round(sent / BYTES_PER_SECOND, 1), detail=detail or None)
            await self._file_status(state=state, name=name, done_s=round(min(sent / BYTES_PER_SECOND, total), 1),
                                    total_s=total, detail=detail)

    async def stop_audio_file(self) -> None:
        if self.file_playing():
            self.file_task.cancel()
            try:
                await self.file_task
            except asyncio.CancelledError:
                pass

    async def set_speaker(self, label: str | None, speaker: str | None) -> str | None:
        """label given: map an AssemblyAI label; label None: the manual toggle (speaker None = automatic)."""
        if speaker is not None and speaker not in self.pack.role_ids + ["unknown"]:
            return f"speaker must be one of: {', '.join(self.pack.role_ids)} or unknown"
        if label is None:
            self.manual_speaker = speaker
        elif speaker is None:
            self.speaker_map.pop(label, None)
        else:
            self.speaker_map[label] = speaker
        self.audit.write("user", "speaker_set", label=label, speaker=speaker)
        await self.send({"type": "mic_status", "mic": self.mic_status()})
        if label is not None and speaker not in (None, "unknown"):
            await self._relabel_past(label, speaker)
        return None

    async def set_line_speaker(self, utterance_id: str, speaker: str) -> str | None:
        """The user corrects who said one line (speaker labels can be wrong, most often on a short answer right after
        a question). Same correction record as a relabel; the live checklist is not re-run, the review reads it."""
        if speaker not in self.pack.role_ids + ["unknown"]:
            return f"speaker must be one of: {', '.join(self.pack.role_ids)} or unknown"
        i = next((i for i, u in enumerate(self.store.utterances) if u.id == utterance_id), None)
        if i is None:
            return "unknown line"
        old = self.store.utterances[i]
        if old.speaker == speaker:
            return None
        self.store.utterances[i] = u = old.model_copy(update={"speaker": speaker})
        self.events.write("speaker_corrected", utterance_id=u.id, old=old.speaker, new=speaker, by="user")
        self.audit.write("user", "line_speaker_changed", utterance_id=u.id, old=old.speaker, new=speaker)
        await self.send({"type": "utterance_update", "utterance": u.model_dump()})
        return None

    async def _relabel_past(self, label: str, speaker: str) -> None:
        """Earlier lines from this voice that still say unknown take the new mapping. Only lines from the current
        AssemblyAI connection (labels restart on a new one). The transcript file stays append-only: each change is a
        `speaker_corrected` event, applied wherever the saved transcript is read (store.read_utterances)."""
        conn = self.label_source.connections if self.label_source is not None else None
        changed = []
        for i, u in enumerate(self.store.utterances):
            if u.speaker == "unknown" and self.line_labels.get(u.id) == (label, conn):
                self.store.utterances[i] = u = u.model_copy(update={"speaker": speaker})
                self.events.write("speaker_corrected", utterance_id=u.id, old="unknown", new=speaker, label=label)
                await self.send({"type": "utterance_update", "utterance": u.model_dump()})
                changed.append(u.id)
        if changed:
            self.audit.write("user", "speaker_relabelled", label=label, speaker=speaker, utterance_ids=changed)

    async def stop_replay(self) -> None:
        if self.replay_task and not self.replay_task.done():
            await self.replay.stop()
            self.replay_task.cancel()
            self.audit.write("user", "replay_stop")
        self.replay_task = None

    async def close(self) -> None:
        await self.stop_replay()
        await self.stop_audio_file()
        await self.stop_mic()
        await self.typed.stop()
        if self.gate_task and not self.gate_task.done():
            self.gate_task.cancel()
        for t in list(self.cue_tasks) + [self.hold_task] * bool(self.hold_task):
            t.cancel()
        for client in {getattr(self.decider, "client", None), getattr(self.analyst, "client", None)} - {None}:
            await client.close()
        self.audit.write("system", "session_stop", utterances=len(self.store.utterances),
                         cost_usd=round(self.ledger.total_usd, 4))
