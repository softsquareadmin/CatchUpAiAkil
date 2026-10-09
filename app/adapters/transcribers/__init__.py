"""Transcriber interface. Adapters: assemblyai, replay, typed (later: local)."""
from typing import Awaitable, Callable, Protocol

from app.models import Utterance

Emit = Callable[[Utterance], Awaitable[None]]  # receives partial (is_final=False) and final utterances


class Transcriber(Protocol):
    async def start(self, session_id: str, emit: Emit) -> None: ...
    async def send_audio(self, chunk: bytes) -> None: ...
    async def stop(self) -> None: ...
