"""Pydantic data models (SPEC section 4). Status fields are always explicit, never null."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Speaker = str  # a role id from the pack, or "unknown" (validated against the pack at the input boundaries)
Source = Literal["mic", "replay", "typed", "paste", "file"]
TopicStatus = Literal["not_covered", "partial", "covered"]
FollowUp = Literal["none", "suggested", "required"]  # separate from coverage (owner decision 2026-10-08)


class Utterance(BaseModel):
    id: str
    session_id: str
    t_start_ms: int
    t_end_ms: int
    speaker: Speaker
    text: str
    is_final: bool
    source: Source


class Evidence(BaseModel):
    utterance_id: str
    quote: str = Field(min_length=1)


class DemoHints(BaseModel):
    """Tier 2, offline demo gate only (GATE_MODEL=fake:keyword); real models ignore it."""
    keywords: list[str] = []
    speakers: list[str] = []         # role ids whose lines count; default: subject roles
    required_words: list[str] = []   # a matching line marks the follow-up required and raises a flag


class ChecklistItem(BaseModel):
    """Tier 1: label, criteria, follow_up_when, required. Tier 2 (optional): definition, examples, policy_ref, demo."""
    id: str = Field(default="", pattern=r"^([a-z][a-z0-9_]*)?$")  # generated from the label when empty (pack loader)
    label: str = Field(min_length=1)
    criteria: list[str] = []
    follow_up_when: str = ""
    required: bool = True
    definition: str = ""             # tier 2: replaces the generated text when present
    examples: list[str] = []         # tier 2: invented, never taken from a test script
    policy_ref: str | None = None
    demo: DemoHints | None = None

    def gate_text(self) -> str:
        """What the gate is told about this topic: the advanced definition, or text built from tier 1."""
        if self.definition.strip():
            text = self.definition  # sent exactly as written
        else:
            text = "Complete when all of: " + "; ".join(c.strip().rstrip(".") for c in self.criteria) + "."
            if self.follow_up_when.strip():
                text += f" Follow-up required when: {self.follow_up_when.strip()}"
        if self.examples:
            text += " Examples (invented): " + " | ".join(self.examples)
        return text


class TopicState(BaseModel):
    item_id: str
    status: TopicStatus = "not_covered"
    follow_up: FollowUp = "none"
    follow_up_reason: str = ""
    evidence: list[Evidence] = []
    updated_at_ms: int = 0
    rationale_short: str = ""


class TopicUpdate(BaseModel):
    """A delta against stored TopicState, returned by every Decider adapter."""
    item_id: str
    status: TopicStatus
    follow_up: FollowUp = "none"
    follow_up_reason: str = ""
    evidence: list[Evidence] = []
    rationale_short: str = ""


class Flag(BaseModel):
    """An important statement worth the user's attention. topic_id is "general" when no topic fits."""
    topic_id: str
    note: str
    evidence: Evidence


class AnswerQuality(BaseModel):
    utterance_id: str
    quality: Literal["ok", "vague", "evasive", "incomplete", "contradiction"]


class GateResult(BaseModel):
    updates: list[TopicUpdate] = []
    flags: list[Flag] = []
    answer_quality: AnswerQuality | None = None


class Cue(BaseModel):
    id: str
    topic_id: str
    kind: Literal["follow_up", "flag"]
    question_or_note: str
    evidence: Evidence
    created_at_ms: int
    state: Literal["active", "dismissed", "asked"] = "active"
    updated: bool = False  # rewritten after later lines changed what is missing (replaces an earlier card)


class FormField(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str
    type: Literal["text", "choice", "date", "number"]
    choices: list[str] | None = None
    source_topic_ids: list[str] = []


class DraftField(BaseModel):
    field_id: str
    value: str
    evidence: list[Evidence]
    status: Literal["proposed", "accepted", "edited", "rejected"] = "proposed"


class Role(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    kind: Literal["user", "subject", "other"]  # user = the person using the tool; subject = answers are judged
    aliases: list[str] = []  # words that identify this role in pasted transcripts ("Mum:" -> a role)


class QuestionLintConfig(BaseModel):
    open_ended_only: bool = True
    blocked_terms: list[str] = []   # a question containing one is rejected; "word*" matches any ending
    act_terms: list[str] = []       # must not appear unless a subject role already said it
    name_with_act_rule: bool = False  # a name next to an act must have been said together by a subject role


class Guardrails(BaseModel):
    """Tier 2, file only (not editable in the UI). Generic rules always apply in code; these add domain rules."""
    prompt_rules: list[str] = []    # extra rules inserted into the gate, cue and review prompts
    question_lint: QuestionLintConfig = QuestionLintConfig()
    note_blocked_patterns: list[str] = []  # regexes on normalised text; flags and review items matching one are dropped
    follow_up_required_when: str = ""      # pack-wide rule for a *required* follow-up; empty = topic rules only
    allow_overall_recommendation: bool = False
    overall_assessment_instruction: str = ""  # how to word the draft assessment when allowed


class SpecialTopics(BaseModel):
    consent_topic: str | None = None        # enables the consent flag and the stop prompt
    audience_hint_topic: str | None = None  # quotes from this topic are given to the cue writer ...
    audience_hint_instruction: str = ""     # ... with this instruction (for example wording suited to an age)


class CustomSection(BaseModel):
    """CatchUp-style report section (catchupai/models.py ReportSection)."""
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    type: Literal["text", "list", "status", "boolean", "score"]
    instruction: str = Field(min_length=1)
    required: bool = True
    options: list[str] | None = None
    min: float | None = None
    max: float | None = None
    max_items: int | None = None


class ExtraSource(BaseModel):
    """A source that is not a role, spoken by a role (for example a report the user reads out)."""
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    spoken_by: str
    description: str = ""


class AccountGroup(BaseModel):
    topics: list[str] = Field(min_length=1)  # accounts for these topics are shown together under the first id
    label: str


REVIEW_SECTIONS = ("summary", "topics_not_covered", "follow_ups", "form", "timeline", "accounts", "missing_accounts",
                   "next_steps")
DEFAULT_REVIEW_SECTIONS = ["summary", "topics_not_covered", "follow_ups", "form", "next_steps"]


class ReviewConfig(BaseModel):
    sections: list[Literal["summary", "topics_not_covered", "follow_ups", "form", "timeline", "accounts",
                           "missing_accounts", "next_steps"]] = DEFAULT_REVIEW_SECTIONS
    custom_sections: list[CustomSection] = []
    instructions: dict[str, str] = {}     # per built-in section: replaces the generic instruction
    extra_sources: list[ExtraSource] = []
    account_groups: list[AccountGroup] = []
    show_coverage_score: bool = True      # false: counts and statuses only, no number (screen, HTML, JSON)


GENERIC_PERSONA = ("You assist the person running an interview. You assist; you never decide. You never judge "
                   "credibility and never state conclusions about a person beyond what was said.")


class UseCasePack(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    pack_format: Literal[1, 2] = 1
    name: str = ""
    purpose: str = ""
    pay_attention_to: list[str] = []
    flag_when: list[str] = []              # tier 1: this type's flag triggers, one per line (the gate must flag these)
    persona: str = GENERIC_PERSONA
    roles: list[Role] = []
    checklist: list[ChecklistItem]
    guardrails: Guardrails | None = None   # None = generic defaults only
    special_topics: SpecialTopics = SpecialTopics()
    ending_phrases: list[str] = []         # empty = generic default list
    review: ReviewConfig = ReviewConfig()
    form_schema: list[FormField] = []
    live_prompts: dict[str, str] = {}
    final_prompts: dict[str, str] = {}
    sample_script: str | None = None       # demo transcript for Replay (path from the repo root)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def user_role(self) -> Role:
        return next(r for r in self.roles if r.kind == "user")

    @property
    def subject_ids(self) -> list[str]:
        return [r.id for r in self.roles if r.kind == "subject"]

    @property
    def role_ids(self) -> list[str]:
        return [r.id for r in self.roles]

    def role_label(self, role_id: str) -> str:
        return next((r.label for r in self.roles if r.id == role_id), "Unknown")

    @property
    def rails(self) -> Guardrails:
        return self.guardrails or Guardrails()


class ReviewItem(BaseModel):
    """One AI-proposed item on the review screen (M5). The user accepts, edits or rejects each one; nothing is
    final until accepted. Form items carry the pack's field id as `field_id` (the DraftField of SPEC 4)."""
    id: str
    section: str              # a built-in section, "overall_assessment", or "custom.<id>"
    label: str
    value: str
    evidence: list[Evidence] = []
    # An item about something not yet said has no quote; instead it names the checklist topics it is about, and
    # each of them was not or only partly covered at review time (checked in app.review). One of the two is required.
    basis_topics: list[dict] = []  # [{"id", "label", "status"}]
    status: Literal["proposed", "accepted", "edited", "rejected"] = "proposed"
    ai_value: str = ""            # what the model proposed, kept when the user edits
    field_id: str | None = None   # form items only
    topic_id: str | None = None   # accounts only (side-by-side grouping)
    source: str | None = None     # a role id or an extra source id (accounts, timeline, summary)

    @model_validator(mode="after")
    def _grounded(self):
        if not self.evidence and not self.basis_topics:
            raise ValueError("a review item needs a quote or the checklist topics it is about")
        return self
