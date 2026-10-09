"""Typed transcriber: each submitted line becomes one final utterance."""
from app.adapters.transcribers import Emit
from app.models import Speaker, Utterance


class TypedTranscriber:
    def __init__(self):
        self.session_id, self.emit = "", None

    async def start(self, session_id: str, emit: Emit) -> None:
        self.session_id, self.emit = session_id, emit

    async def submit(self, speaker: Speaker, text: str, t_ms: int) -> None:
        if self.emit is None:
            raise RuntimeError("TypedTranscriber not started")
        if text.strip():
            await self.emit(Utterance(id="", session_id=self.session_id, t_start_ms=t_ms, t_end_ms=t_ms,
                                      speaker=speaker, text=text.strip(), is_final=True, source="typed"))

    async def send_audio(self, chunk: bytes) -> None:
        pass

    async def stop(self) -> None:
        self.emit = None
