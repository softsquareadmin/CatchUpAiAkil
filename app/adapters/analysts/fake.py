"""Offline cue writer and reviewer for UI work and demos with no key (CUE_MODEL / REVIEW_MODEL=fake:template).
The cue is a fixed open question; its evidence is the topic's latest quote, so it passes validation. The review is
built from the transcript and checklist evidence for the sections the pack asks for: real quotes, no model."""
from app.evidence import validate
from app.ledger import Ledger
from app.models import ChecklistItem, Cue, TopicState, UseCasePack, Utterance

QUESTION = "Can you tell me more about that?"


class TemplateAnalyst:
    def __init__(self, pack: UseCasePack, ledger: Ledger):
        self.pack, self.ledger = pack, ledger
        self.last_rejections: list[dict] = []
        self.last_error: str | None = None

    async def write_cue(self, topic: ChecklistItem, state: TopicState, window: list[Utterance],
                        audience: str | None = None) -> Cue | None:
        self.last_rejections = []
        self.ledger.record("cue", "fake", "template", latency_ms=0)
        transcript = {u.id: u for u in window}
        ev = next((e for e in reversed(state.evidence) if validate(e, transcript)), None)
        if ev is None:
            return None
        return Cue(id="", topic_id=topic.id, kind="follow_up", question_or_note=QUESTION, evidence=ev, created_at_ms=0)

    async def review(self, transcript: list[Utterance], checklist_state: dict[str, TopicState],
                     case_context: dict | None = None, only: list[str] | None = None, note: str = "") -> dict | None:
        self.ledger.record("review", "fake", "template", latency_ms=0)
        pk, by_id = self.pack, {u.id: u for u in transcript}
        answers = [u for u in transcript if u.speaker in pk.subject_ids]

        def ev(u: Utterance) -> list[dict]:
            return [{"utterance_id": u.id, "quote": u.text}]
        out: dict = {"topic_results": []}
        for c in pk.checklist:  # live status kept; summary from the topic's first quote; criteria as what is missing
            st = checklist_state.get(c.id) or TopicState(item_id=c.id)
            quotes = [e for e in st.evidence if validate(e, by_id)][:1]
            u = by_id[quotes[0].utterance_id] if quotes else None
            gap = st.status != "covered"
            out["topic_results"].append({
                "topic_id": c.id, "status": st.status, "status_reason": "",
                "summary": f"{pk.role_label(u.speaker)} said: {quotes[0].quote}" if u else "",
                "evidence": [q.model_dump() for q in quotes],
                "missing": (c.criteria[:3] or [f"A full answer on {c.label.lower()}"]) if gap else [],
                "suggested_follow_up": QUESTION if gap else ""})
        if "summary" in pk.review.sections:
            out["summary"] = [{"speaker": u.speaker, "text": f"Said: {u.text}", "evidence": ev(u)} for u in answers]
        if "form" in pk.review.sections and pk.form_schema:
            out["form_fields"] = []
            for f in pk.form_schema:
                quotes = [e for t in f.source_topic_ids for e in checklist_state.get(t, TopicState(item_id=t)).evidence
                          if e.utterance_id in by_id]
                if quotes and f.type == "text":
                    out["form_fields"].append({"field_id": f.id, "value": quotes[0].quote, "evidence": [quotes[0].model_dump()]})
        for sec in ("timeline", "accounts", "missing_accounts", "next_steps"):
            if sec in pk.review.sections:
                out[sec] = []
        if pk.review.custom_sections and answers:
            first = answers[0]
            out["custom"] = {}
            for c in pk.review.custom_sections:
                value = {"text": f"As said: {first.text}", "status": (c.options or [""])[0], "boolean": True,
                         "score": c.min if c.min is not None else 0}.get(c.type)
                out["custom"][c.id] = ([{"value": f"As said: {u.text}", "evidence": ev(u)} for u in answers[:2]]
                                       if c.type == "list" else {"value": value, "evidence": ev(first)})
        if pk.rails.allow_overall_recommendation:
            covered = [(c, checklist_state[c.id]) for c in pk.checklist
                       if checklist_state.get(c.id) and checklist_state[c.id].evidence]
            strengths = [{"topic_id": c.id, "point": f"Answered: {c.label}", "evidence": [s.evidence[0].model_dump()]}
                         for c, s in covered if s.status == "covered" and by_id.get(s.evidence[0].utterance_id)
                         and by_id[s.evidence[0].utterance_id].speaker in pk.subject_ids]
            gaps = [{"topic_id": c.id, "point": f"Only partly answered: {c.label}", "evidence": [s.evidence[0].model_dump()]}
                    for c, s in covered if s.status == "partial" and by_id.get(s.evidence[0].utterance_id)
                    and by_id[s.evidence[0].utterance_id].speaker in pk.subject_ids]
            parts = strengths + gaps
            out["overall_assessment"] = {"strengths": strengths, "gaps": gaps, "draft": {
                "text": f"Draft view: {len(strengths)} topic(s) answered in full, {len(gaps)} partly. To be verified.",
                "evidence": parts[0]["evidence"] if parts else []}}
        return out
