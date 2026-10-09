"""AssemblyAI streaming (v3) through the server, so the API key never reaches the browser (docs/provider-notes.md).
The browser sends 16 kHz mono PCM16 frames; this adapter relays them and turns Turn messages into partial and
final utterances. If the AssemblyAI connection drops, it reconnects with backoff; finalized utterances are already
stored by the session, and the audio of the unfinished turn is sent again on the new connection, so speech in
progress at the drop is not lost either. Speaker labels restart on a new connection (the session resets the
user's label mapping). Billing is by connection time, so each connection gets a ledger
row with its open seconds. A finished turn that holds two speakers (AssemblyAI ends a turn at a pause, not at a change
of speaker, so a quick reply lands in the same turn) is split where its word-level speaker labels change."""
import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable
from urllib.parse import urlencode

from app.adapters.transcribers import Emit
from app.ledger import Ledger
from app.models import Speaker, Utterance

log = logging.getLogger(__name__)
URL = "wss://streaming.assemblyai.com/v3/ws"
BUFFER_SECONDS = 5  # audio kept while reconnecting; older audio is dropped
RESEND_SECONDS = 20  # audio since the last final turn, re-sent after a reconnect
BYTES_PER_MS = 32  # 16 kHz PCM16 mono
Status = Callable[[str, str], Awaitable[None]]  # (state, detail): connecting | listening | reconnecting | failed | stopped
MIN_SPLIT_WORDS = 3  # a speaker change shorter than this stays with a neighbour (word labels flicker)


def word_labels(words: list[dict]) -> list:
    """[[word, speaker label or None], ...]: logged with each final line so speaker changes can be checked later."""
    return [[str(w.get("text", "")), w.get("speaker")] for w in words]


def split_by_speaker(text: str, words: list[dict], min_words: int = MIN_SPLIT_WORDS) -> list[tuple[str, str, list[dict]]]:
    """Cut a finished turn where its word-level speaker labels change: [(label, text, words)] in order. Returns []
    when there is nothing to split (no word labels, or one speaker). Words without a usable label (missing or PENDING)
    go with the words after them (at a change of voice AssemblyAI needs a moment of the new voice before it labels
    it), or with the words before them at the end of the turn. A run shorter than `min_words` joins the run after it (a reply often opens with
    "Okay, great."), or the one before when it is last. The turn's formatted text is cut at the same word positions
    when it lines up with the words one to one; otherwise each part is the words' own text."""
    labels = [w.get("speaker") if w.get("speaker") not in (None, "", "PENDING") else None for w in words]
    if not any(labels):
        return []
    after = None
    for i in range(len(labels) - 1, -1, -1):  # unlabelled: the next labelled word's speaker ...
        labels[i] = after = labels[i] or after
    before = None
    for i, l in enumerate(labels):  # ... or, at the end of the turn, the previous one's
        labels[i] = before = l or before
    runs: list[list] = []  # [label, count]
    for l in labels:
        if runs and runs[-1][0] == l:
            runs[-1][1] += 1
        else:
            runs.append([l, 1])
    while len(runs) > 1:
        short = next((i for i, r in enumerate(runs) if r[1] < min_words), None)
        if short is None:
            break
        into = short + 1 if short + 1 < len(runs) else short - 1
        runs[into][1] += runs[short][1]
        del runs[short]
        merged = []  # neighbours that now share a label become one run
        for r in runs:
            if merged and merged[-1][0] == r[0]:
                merged[-1][1] += r[1]
            else:
                merged.append(r)
        runs = merged
    if len(runs) < 2:
        return []
    tokens = text.split()
    aligned = len(tokens) == len(words)
    parts, i = [], 0
    for label, n in runs:
        ws = words[i:i + n]
        parts.append((label, " ".join(tokens[i:i + n]) if aligned else " ".join(str(w.get("text", "")) for w in ws), ws))
        i += n
    return parts


async def _connect(url: str, headers: dict):
    from websockets.asyncio.client import connect
    return await connect(url, additional_headers=headers, open_timeout=10, max_size=2**22)


class AssemblyAITranscriber:
    def __init__(self, api_key: str, model: str, ledger: Ledger, *, speaker_for: Callable[[str | None], Speaker],
                 elapsed_ms: Callable[[], int], on_status: Status | None = None, sample_rate: int = 16000,
                 speaker_labels: bool = True, max_speakers: int = 3, max_retries: int = 5,
                 backoff_s: tuple[float, ...] = (0.5, 1, 2, 4, 8), connect=_connect, source: str = "mic",
                 split_turns: bool = True):
        self.api_key, self.model, self.ledger = api_key, model, ledger
        self.speaker_for, self.elapsed_ms = speaker_for, elapsed_ms
        self.on_status = on_status
        self.sample_rate, self.speaker_labels, self.max_speakers = sample_rate, speaker_labels, max_speakers
        self.max_retries, self.backoff_s, self.connect = max_retries, backoff_s, connect
        self.source = source  # "mic" or "file" (M8): stamped on the utterances
        self.split_turns = split_turns  # cut a final turn where the word-level speaker changes
        self.session_id, self.emit = "", None
        self.ws = None
        self.task: asyncio.Task | None = None
        self.running = False
        self.buffer: deque[bytes] = deque()
        self.buffered_bytes = 0
        self.last_label: str | None = None
        self.last_label_confidence: float | None = None
        self.last_part: str | None = None  # "1/2" when the last final came from a split turn
        self.last_words: list = []  # [[word, speaker label], ...] of the last final, logged to check the split
        self.connections = 0
        self.unfinished: deque[tuple[float, bytes]] = deque()  # (end ms on this connection, chunk) since the last final
        self.sent_ms = 0.0

    @property
    def ledger_model(self) -> str:
        return f"{self.model}+speaker_labels" if self.speaker_labels else self.model

    def url(self) -> str:
        q = {"speech_model": self.model, "sample_rate": self.sample_rate, "encoding": "pcm_s16le"}
        if self.speaker_labels:
            q.update(speaker_labels="true", max_speakers=self.max_speakers)
        return f"{URL}?{urlencode(q)}"

    async def _status(self, state: str, detail: str = "") -> None:
        if self.on_status:
            await self.on_status(state, detail)

    # ---- Transcriber interface ----
    async def start(self, session_id: str, emit: Emit) -> None:
        self.session_id, self.emit, self.running = session_id, emit, True
        self.task = asyncio.create_task(self._run())

    async def send_audio(self, chunk: bytes) -> None:
        if not self.running:
            return
        ws = self.ws
        if ws is not None:
            try:
                await ws.send(chunk)
                self._sent(chunk)
                return
            except Exception:  # connection dropping: keep the audio for the next connection
                pass
        self.buffer.append(chunk)
        self.buffered_bytes += len(chunk)
        while self.buffered_bytes > BUFFER_SECONDS * self.sample_rate * 2 and self.buffer:
            self.buffered_bytes -= len(self.buffer.popleft())

    def _sent(self, chunk: bytes) -> None:
        self.sent_ms += len(chunk) / BYTES_PER_MS
        self.unfinished.append((self.sent_ms, chunk))
        while self.unfinished and self.sent_ms - self.unfinished[0][0] > RESEND_SECONDS * 1000:
            self.unfinished.popleft()

    async def stop(self) -> None:
        """Terminate gracefully (so the last final turn arrives), then close."""
        if not self.running:
            return
        self.running = False
        ws = self.ws
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "Terminate"}))
            except Exception:
                pass
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=5)
            except (asyncio.TimeoutError, Exception):
                self.task.cancel()
                if self.ws is not None:
                    await self.ws.close()
        await self._status("stopped")

    # ---- connection loop ----
    async def _run(self) -> None:
        failures = 0
        while self.running:
            await self._status("connecting" if self.connections == 0 else "reconnecting",
                               f"attempt {failures + 1}" if failures else "")
            opened, termination, error = None, None, None
            try:
                self.ws = await self.connect(self.url(), {"Authorization": self.api_key})
                # resend the unfinished turn from the old connection, then audio captured while reconnecting
                backlog = [c for _, c in self.unfinished] + list(self.buffer)
                self.unfinished.clear()
                self.buffer.clear()
                self.buffered_bytes, self.sent_ms = 0, 0.0
                opened = time.monotonic()
                offset = self.elapsed_ms() - int(sum(map(len, backlog)) / BYTES_PER_MS)
                self.connections += 1
                failures = 0
                await self._status("listening", "reconnected" if self.connections > 1 else "")
                for chunk in backlog:
                    await self.ws.send(chunk)
                    self._sent(chunk)
                async for raw in self.ws:
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "Turn":
                        await self._turn(msg, offset)
                    elif kind == "Termination":
                        termination = msg
                        break
                    elif kind == "Error" or "error" in msg:
                        error = str(msg)[:300]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # connection refused, dropped or closed with an error code
                error = repr(e)[:300]
            finally:
                ws, self.ws = self.ws, None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                if opened is not None:
                    self._record(opened, termination, error)
            if not self.running:
                break  # stopped by us; otherwise the server closed the session or the link dropped: reconnect
            if opened is None:
                failures += 1
            if failures >= self.max_retries:
                self.running = False
                await self._status("failed", error or "could not connect")
                return
            log.warning("AssemblyAI connection lost (%s); reconnecting", error)
            await asyncio.sleep(self.backoff_s[min(failures, len(self.backoff_s) - 1)])

    def _record(self, opened: float, termination: dict | None, error: str | None) -> None:
        wall = time.monotonic() - opened
        billed = max(wall, float((termination or {}).get("session_duration_seconds") or 0))
        self.ledger.record("stt", "assemblyai", self.ledger_model, audio_seconds=round(billed, 2),
                           latency_ms=0, ok=error is None, error=error)

    async def _turn(self, msg: dict, offset_ms: int) -> None:
        text = (msg.get("transcript") or "").strip()
        if not text or self.emit is None:
            return
        words = msg.get("words") or []
        start = offset_ms + int(words[0]["start"]) if words else self.elapsed_ms()
        end = offset_ms + int(words[-1]["end"]) if words else self.elapsed_ms()
        final = bool(msg.get("end_of_turn"))
        if final and words:  # this audio is transcribed: no need to resend it after a drop
            done = float(words[-1]["end"])
            while self.unfinished and self.unfinished[0][0] <= done:
                self.unfinished.popleft()
        if final and self.split_turns and words:
            parts = split_by_speaker(text, words)
            mapped = {self.speaker_for(label) for label, _, _ in parts}
            # one known role for every part = the manual toggle or one mapping for both voices: keep one line. Voices
            # not mapped yet are still split (both show as unknown until the user maps them).
            if parts and not (len(mapped) == 1 and mapped != {"unknown"}):
                for n, (label, part, ws) in enumerate(parts, 1):
                    self.last_label, self.last_label_confidence, self.last_part = label, None, f"{n}/{len(parts)}"
                    self.last_words = word_labels(ws)
                    await self.emit(Utterance(id="", session_id=self.session_id, t_start_ms=offset_ms + int(ws[0]["start"]),
                                              t_end_ms=offset_ms + int(ws[-1]["end"]), speaker=self.speaker_for(label),
                                              text=part, is_final=True, source=self.source))
                return
        if final:
            self.last_label = msg.get("speaker_label")
            self.last_label_confidence = msg.get("speaker_confidence")
            self.last_part = None
            self.last_words = word_labels(words)
        await self.emit(Utterance(id="", session_id=self.session_id, t_start_ms=start, t_end_ms=end,
                                  speaker=self.speaker_for(msg.get("speaker_label")), text=text, is_final=final,
                                  source=self.source))
