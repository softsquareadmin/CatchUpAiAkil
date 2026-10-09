"""Deterministic offline adapters for tests and UI work. They record ledger rows like real calls."""
from app.audit import AuditLog
from app.ledger import Ledger
from app.models import ChecklistItem, Cue, Evidence, GateResult, TopicState, TopicUpdate, Utterance

FAKE_PRICES = {"fake:fake": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0,
                             "audio_usd_per_hour": 0.36, "as_of": "2026-10-07", "source": "test"}}


class FakeTranscriber:
    """Emits the given utterances as one partial then one final each."""

    def __init__(self, utterances: list[Utterance]):
        self.utterances = utterances
        self.stopped = False

    async def start(self, session_id: str, emit) -> None:
        for u in self.utterances:
            await emit(u.model_copy(update={"session_id": session_id, "is_final": False}))
            await emit(u.model_copy(update={"session_id": session_id, "is_final": True}))

    async def send_audio(self, chunk: bytes) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True


class FakeDecider:
    """Returns scripted updates keyed by the last utterance id."""

    def __init__(self, ledger: Ledger, audit: AuditLog, script: dict[str, list[TopicUpdate]] | None = None):
        self.ledger, self.audit, self.script = ledger, audit, script or {}
        self.calls: list[list[str]] = []
        self.last_error = None

    async def evaluate(self, window: list[Utterance], checklist_state: dict[str, TopicState]) -> GateResult:
        self.ledger.check_budget()
        self.calls.append([u.id for u in window])
        updates = self.script.get(window[-1].id, []) if window else []
        self.ledger.record("gate", "fake", "fake", input_tokens=100 * len(window), output_tokens=20, latency_ms=1)
        self.audit.write("system", "gate_evaluated", updates=[u.item_id for u in updates])
        return GateResult(updates=updates)


class FakeAnalyst:
    """Writes a fixed, open-ended cue quoting the last utterance."""

    def __init__(self, ledger: Ledger, audit: AuditLog, question: str = "Can you tell me more about that?"):
        self.ledger, self.audit, self.question = ledger, audit, question
        self.last_rejections: list[dict] = []
        self.calls: list[tuple[str, str]] = []  # (topic id, audience hint)

    async def write_cue(self, topic: ChecklistItem, state: TopicState, window: list[Utterance],
                        audience: str | None = None) -> Cue | None:
        self.ledger.check_budget()
        self.calls.append((topic.id, audience))
        self.ledger.record("cue", "fake", "fake", input_tokens=200, output_tokens=40, latency_ms=1)
        if not window:
            return None
        last = window[-1]
        cue = Cue(id=f"cue-{topic.id}-{last.id}", topic_id=topic.id, kind="follow_up",
                  question_or_note=self.question,
                  evidence=Evidence(utterance_id=last.id, quote=last.text), created_at_ms=last.t_end_ms)
        self.audit.write("system", "cue_written", cue_id=cue.id)
        return cue

    async def review(self, transcript: list[Utterance], checklist_state: dict[str, TopicState]) -> dict:
        self.ledger.check_budget()
        self.ledger.record("review", "fake", "fake", input_tokens=1000, output_tokens=200, latency_ms=1)
        return {"summary": [], "draft_fields": []}
