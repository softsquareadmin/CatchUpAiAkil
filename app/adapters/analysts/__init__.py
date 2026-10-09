"""Analyst interface: cue writer and post-interview review (M5). Output is always schema-validated."""
from typing import Protocol

from app.config import Settings
from app.ledger import Ledger
from app.models import ChecklistItem, Cue, TopicState, UseCasePack, Utterance


class Analyst(Protocol):
    last_rejections: list[dict]

    async def write_cue(self, topic: ChecklistItem, state: TopicState, window: list[Utterance],
                        audience: str | None = None) -> Cue | None:
        """One follow-up cue whose evidence validates against `window` and which passes the lint, or None.
        Rejected attempts are left in `last_rejections` for the caller to log."""
        ...

    async def review(self, transcript: list[Utterance], checklist_state: dict[str, TopicState],
                     case_context: dict | None = None) -> dict | None:
        """Raw post-interview draft matching app.review.review_schema, or None; validated by app.review."""
        ...


def build_analyst(settings: Settings, pack: UseCasePack, ledger: Ledger, role: str = "cue") -> tuple[Analyst | None, str]:
    """Return (analyst for the role "cue" or "review", "") or (None, reason it is off). Never raises for a missing key."""
    provider, model = settings.model_for(role)
    if provider == "fake":
        from app.adapters.analysts.fake import TemplateAnalyst
        return TemplateAnalyst(pack, ledger), ""
    if provider != "openrouter":
        return None, f"cue provider '{provider}' has no client yet (use openrouter:...)"
    if not settings.has_key("OPENROUTER_API_KEY"):
        return None, "OPENROUTER_API_KEY is not set"
    from app.adapters.analysts.llm import LLMAnalyst
    from app.adapters.providers.openrouter import OpenRouterClient
    client = OpenRouterClient(settings.secrets["OPENROUTER_API_KEY"].get_secret_value())
    effort = settings.cue_reasoning_effort if role == "cue" else None  # review: no reasoning setting sent
    return LLMAnalyst(pack, client, provider, model, ledger, reasoning_effort=effort, role=role, limits=settings.limits), ""
