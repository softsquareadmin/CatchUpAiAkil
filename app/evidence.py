"""Evidence validation (safety rule 4): a quote must appear in the utterance it cites,
after normalising case, whitespace and punctuation, and must match on word boundaries."""
import re
import unicodedata

from app.models import Evidence, Utterance

_APOSTROPHES = re.compile(r"['‘’`]")
_NON_WORD = re.compile(r"[^\w]+")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = _APOSTROPHES.sub("", text)  # "I'm" -> "im", so it matches with or without the apostrophe
    return " ".join(_NON_WORD.sub(" ", text).split())


def quote_in(quote: str, text: str) -> bool:
    q = normalise(quote)
    return bool(q) and f" {q} " in f" {normalise(text)} "


def validate(ev: Evidence, transcript: dict[str, Utterance]) -> bool:
    u = transcript.get(ev.utterance_id)
    return u is not None and u.is_final and quote_in(ev.quote, u.text)
