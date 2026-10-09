"""Decider (gate) interface. Adapters: llm (default), jev, hybrid (Jev then llm), fake (offline demo)."""
from typing import Protocol

from app.config import Settings
from app.ledger import Ledger
from app.models import GateResult, TopicState, UseCasePack, Utterance


class Decider(Protocol):
    async def evaluate(self, window: list[Utterance], checklist_state: dict[str, TopicState]) -> GateResult: ...


def build_decider(settings: Settings, pack: UseCasePack, ledger: Ledger) -> tuple[Decider | None, str]:
    """Return (decider, "") or (None, reason the gate is off). Never raises for a missing key."""
    provider, model = settings.model_for("gate")
    if provider == "fake":
        from app.adapters.deciders.fake import KeywordDecider
        return KeywordDecider(pack, ledger), ""
    if settings.gate_adapter in ("llm", "hybrid") and provider != "openrouter":
        return None, f"gate provider '{provider}' has no client yet (use openrouter:...)"
    if not settings.has_key("OPENROUTER_API_KEY"):
        return None, "OPENROUTER_API_KEY is not set"
    from app.adapters.providers.openrouter import OpenRouterClient
    client = OpenRouterClient(settings.secrets["OPENROUTER_API_KEY"].get_secret_value())
    from app.adapters.deciders.jev import JevDecider
    from app.adapters.deciders.llm import LLMDecider
    llm = LLMDecider(pack, client, provider, model, ledger, variant=settings.gate_prompt,
                     temperature=settings.gate_temperature, limits=settings.limits)
    if settings.gate_adapter == "llm":
        return llm, ""
    jev = JevDecider(pack, client, "openrouter", settings.jev_model, ledger, settings.jev_min_probability,
                     style=settings.jev_questions, followup_probability=settings.jev_followup_probability,
                     limits=settings.limits)
    if settings.gate_adapter == "jev":
        return jev, ""
    from app.adapters.deciders.hybrid import HybridDecider
    return HybridDecider(jev, llm, settings.hybrid_min_confidence), ""
