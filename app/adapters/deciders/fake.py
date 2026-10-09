"""Offline keyword gate for UI work and demos with no key (GATE_MODEL=fake:keyword). Not a real classifier.
Keywords come from each topic's optional `demo` hints in the pack, or else from the words of its label and
criteria. Quotes are copied from the utterance text, so they pass evidence validation."""
import re

from app.ledger import Ledger
from app.models import AnswerQuality, ChecklistItem, Evidence, Flag, GateResult, TopicState, TopicUpdate, UseCasePack, Utterance

VAGUE = ("don't know", "don't remember", "not sure", "just")
REFUSE = ("do not consent", "don't consent", "not consent", "you may not")
NEXT = {"not_covered": "partial", "partial": "covered", "covered": "covered"}
STOP = {"about", "their", "there", "which", "would", "could", "should", "where", "when", "what", "with", "from",
        "that", "this", "have", "were", "they", "them", "your", "whether", "other", "after", "before", "including"}


def sentence_with(text: str, keyword: str) -> str:
    for s in re.split(r"(?<=[.!?])\s+", text):
        if keyword in s.lower():
            return s
    return text


def topic_rule(c: ChecklistItem, pack: UseCasePack) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(speakers whose lines count, keywords, words that make the follow-up required)."""
    d = c.demo
    speakers = tuple(d.speakers) if d and d.speakers else tuple(pack.subject_ids)
    if d and d.keywords:
        words = tuple(k.lower() for k in d.keywords)
    else:  # generic: longer words from the label and the criteria
        text = " ".join([c.label, *c.criteria]).lower()
        words = tuple(dict.fromkeys(w for w in re.findall(r"[a-z]{5,}", text) if w not in STOP))
    return speakers, words, tuple(d.required_words) if d else ()


class KeywordDecider:
    def __init__(self, pack: UseCasePack, ledger: Ledger):
        self.pack = pack
        self.rules = {c.id: topic_rule(c, pack) for c in pack.checklist}
        self.ledger, self.seen = ledger, set()
        self.last_error: str | None = None

    async def evaluate(self, window: list[Utterance], checklist_state: dict[str, TopicState]) -> GateResult:
        result, status = GateResult(), {k: v.status for k, v in checklist_state.items()}
        consent = self.pack.special_topics.consent_topic
        for u in (u for u in window if u.id not in self.seen):
            self.seen.add(u.id)
            low = u.text.lower()
            subject = u.speaker in self.pack.subject_ids
            vague = subject and any(v in low for v in VAGUE)
            for topic, (speakers, words, required) in self.rules.items():
                hit = next((w for w in words if w in low), None)
                if u.speaker not in speakers or not hit:
                    continue
                new = "partial" if vague else NEXT[status.get(topic, "not_covered")]
                req = next((w for w in required if w in low), None)
                follow = "required" if req else "suggested" if vague else "none"
                status[topic] = new
                result.updates.append(TopicUpdate(item_id=topic, status=new, follow_up=follow,
                                                  follow_up_reason="" if follow == "none" else "keyword: vague or concern",
                                                  rationale_short=f"keyword '{hit}'",
                                                  evidence=[Evidence(utterance_id=u.id, quote=sentence_with(u.text, hit))]))
                if req:
                    result.flags.append(Flag(topic_id=topic, note=f"The {self.pack.role_label(u.speaker).lower()} said "
                                                                  f"'{req}'.",
                                             evidence=Evidence(utterance_id=u.id, quote=sentence_with(u.text, req))))
            if consent and subject and (hit := next((w for w in REFUSE if w in low), None)):
                result.flags.append(Flag(topic_id=consent, note=f"The {self.pack.role_label(u.speaker).lower()} "
                                                                 "refused consent to recording.",
                                         evidence=Evidence(utterance_id=u.id, quote=sentence_with(u.text, hit))))
            if subject:
                result.answer_quality = AnswerQuality(utterance_id=u.id, quality="vague" if vague else "ok")
        self.ledger.record("gate", "fake", "keyword", latency_ms=0)
        return result
