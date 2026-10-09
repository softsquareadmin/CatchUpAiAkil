"""Cue rules shared by every cue writer: question and note lint (safety rules 2-3), de-duplication and cooldown,
the "Before you leave" topic list, ending detection and the audience hint.
Generic rules live here; domain word lists come from the pack's `guardrails` (CPS: cps_interview_v2.yaml)."""
import re

from app.evidence import normalise
from app.models import Cue, Guardrails, TopicState, UseCasePack, Utterance

# A question for the user must be open-ended. "Can/could you tell me ..." is open; other yes/no starts are not.
_OPEN_CAN = r"(?! you (tell|describe|help|say|share|show|remember|think of|walk me through))"
CLOSED_START = re.compile(
    r"^(did|didn'?t|does|doesn'?t|do|don'?t|is|isn'?t|was|wasn'?t|were|weren'?t|are|aren'?t|has|hasn'?t|have|"
    rf"haven'?t|had|will|would|should|shall|could{_OPEN_CAN}|can{_OPEN_CAN})\b", re.I)
OPEN_STATEMENT = re.compile(r"^(tell me|describe|help me understand|walk me through)\b", re.I)
TAG_QUESTION = re.compile(r"(,\s*(right|correct|yes|no|ok|okay)|isn'?t (it|that|he|she)|wasn'?t (it|he|she)|"
                          r"didn'?t (he|she|they|you)|don'?t you think)\s*\?\s*$", re.I)

# Generic defaults for a pack with no guardrails block: no credibility judgements about people.
GENERIC_BLOCKED_TERMS = ["lie", "lies", "lied", "lying", "liar", "credib*", "believable", "truthful", "dishonest*",
                         "made it up", "made that up", "made this up", "making it up", "making that up", "making this up"]
GENERIC_NOTE_PATTERNS = [r"\b(lie|lies|lied|lying|liar|credib\w*|believable|truthful|dishonest\w*|"
                         r"made (it|that|this) up|making (it|that|this) up)\b"]
# Overall verdicts about a person: blocked everywhere unless the pack allows a draft overall assessment, and even
# then only inside that assessment in the post-interview review (never live).
VERDICT_PATTERNS = [
    r"\b(strong|weak|good|poor|excellent|ideal|perfect|bad|great|top) (candidate|fit|hire|match)\b",
    r"\b(should|must|would|will) (not )?be (hired|rejected|offered|shortlisted|advanced)\b",
    r"\b(recommend|recommends|recommended|recommending) (hiring|rejecting|against|not hiring|the candidate|him|her|them)\b",
    r"\b(un)?suitable for the (role|position|job)\b",
    r"\b(do not|don ?t|no) hire\b",
    r"\boverall (verdict|rating|recommendation)\b",
]

ENDING_DEFAULT = ["please leave", "leave now", "you should go", "you need to go", "you have to go", "you should leave",
                  "you need to leave", "you have to leave", "goodbye", "good bye", "bye", "we are done", "were done",
                  "thats all", "end this", "stop this interview", "stop the interview", "to continue", "wrap up",
                  "wrap this up", "thank you for your time", "i have to go", "i need to go"]


def _norm(text: str) -> str:
    return normalise(text)


def _terms(terms: list[str]) -> re.Pattern | None:
    """Words or phrases matched as whole words on normalised text; a trailing * matches any ending."""
    parts = []
    for t in terms:
        words = [w for w in t.lower().replace("'", "").split() if w]
        if words:
            parts.append(" ".join(re.escape(w.rstrip("*")) + (r"\w*" if w.endswith("*") else "") for w in words))
    return re.compile(r"\b(" + "|".join(parts) + r")\b") if parts else None


class Lint:
    """Question and note checks for one pack. A simple net for obvious cases; the prompts carry the full rules."""

    def __init__(self, pack: UseCasePack):
        rails = pack.guardrails
        self.subjects = set(pack.subject_ids)
        self.allow_assessment = bool(rails and rails.allow_overall_recommendation)
        ql = (rails or Guardrails()).question_lint
        self.open_ended_only = ql.open_ended_only
        self.blocked = _terms(ql.blocked_terms if rails else GENERIC_BLOCKED_TERMS)
        self.acts = _terms(ql.act_terms)
        self.name_with_act = ql.name_with_act_rule
        self.note_patterns = [re.compile(p) for p in (rails.note_blocked_patterns if rails else GENERIC_NOTE_PATTERNS)]
        self.verdicts = [re.compile(p) for p in VERDICT_PATTERNS]
        self.ending = _terms(pack.ending_phrases or ENDING_DEFAULT)

    def question(self, question: str, window: list[Utterance]) -> str | None:
        """Why a suggested question breaks the rules, or None if it passes."""
        q = question.strip()
        if not q:
            return "empty"
        if self.open_ended_only:
            if not (q.endswith("?") or OPEN_STATEMENT.match(q)):
                return "not a question"
            if CLOSED_START.match(q):
                return "yes/no question (not open-ended)"
            if TAG_QUESTION.search(q):
                return "tag question (suggests the answer)"
        low = _norm(q)
        if self.blocked and (m := self.blocked.search(low)):
            return f"blocked wording: '{m.group(0)}'"
        if v := self._verdict(low):
            return v
        if self.acts:
            said = [_norm(u.text) for u in window if u.speaker in self.subjects]
            for m in self.acts.finditer(low):
                act = rf"\b{re.escape(m.group(0))}\b"
                if not any(re.search(act, s) for s in said):
                    return f"mentions '{m.group(0)}', which no interviewee has said"
                if self.name_with_act:  # a person named next to an act must have been named with it by a subject
                    for name in names_in(q):
                        n = rf"\b{re.escape(_norm(name))}\b"
                        if not any(re.search(n, s) and re.search(act, s) for s in said):
                            return f"names {name} with '{m.group(0)}', which no interviewee has said together"
        return None

    def note(self, text: str, assessment: bool = False) -> str | None:
        """Flags and review items. `assessment`: the draft overall assessment, where a verdict may be allowed."""
        low = _norm(text)
        for p in self.note_patterns:
            if m := p.search(low):
                return f"conclusion or blocked wording: '{m.group(0)}'"
        if not (assessment and self.allow_assessment):
            if v := self._verdict(low):
                return v
        return None

    def _verdict(self, low: str) -> str | None:
        for p in self.verdicts:
            if m := p.search(low):
                return f"overall verdict about a person: '{m.group(0)}'"
        return None

    def seems_ending(self, text: str) -> bool:
        return bool(self.ending and self.ending.search(_norm(text)))


def names_in(text: str) -> list[str]:
    """Capitalised words other than the first word and 'I' (rough person-name detector)."""
    words = re.findall(r"[A-Za-z][\w']*", text)
    return [w for w in words[1:] if w[0].isupper() and w not in ("I", "I'm", "I've", "I'd")]


def open_required_topics(pack: UseCasePack, state: dict[str, TopicState]) -> list[dict]:
    """Required topics not yet covered, or with a required follow-up still open."""
    out = []
    for c in pack.checklist:
        s = state[c.id]
        if c.required and (s.status != "covered" or s.follow_up == "required"):
            out.append({"id": c.id, "label": c.label, "status": s.status, "follow_up": s.follow_up,
                        "follow_up_reason": s.follow_up_reason})
    return out


def audience_hint(pack: UseCasePack, state: dict[str, TopicState], transcript: dict[str, Utterance]) -> str | None:
    """Validated quotes from the pack's audience hint topic (for example an age, for suitable wording), or None when the
    pack has no such topic."""
    topic_id = pack.special_topics.audience_hint_topic
    if not topic_id:
        return None
    ts = state.get(topic_id)
    quotes = [f'"{e.quote}"' for e in (ts.evidence if ts else []) if e.utterance_id in transcript]
    return "Said in the interview: " + " ".join(quotes) if quotes else "unknown"


def roles_named(pack: UseCasePack, text: str) -> set[str]:
    """Role ids whose label, id or alias appears as a word in the text (case-insensitive)."""
    low = text.lower()
    return {r.id for r in pack.roles
            if any(re.search(rf"\b{re.escape(w.lower())}\b", low) for w in [r.id, r.label, *r.aliases] if w)}


class CueBoard:
    """At most one active follow-up cue per topic; a cooldown after one is dismissed or asked. A failed write is
    retried on the next gate run, at most MAX_FAILS times per topic and follow-up level. Flags are de-duplicated
    by (topic, utterance), except that a later flag on the same line is shown when it names a role the earlier
    ones did not (a comparison such as "..., while the other person said ..." quotes the same line as the first concern)."""

    MAX_FAILS = 2

    def __init__(self, cooldown_s: float):
        self.cooldown_s = cooldown_s
        self.cues: dict[str, Cue] = {}
        self.active: dict[str, str] = {}   # topic id -> active follow-up cue id
        self.level: dict[str, str] = {}    # cue id -> follow-up level it was written for
        self.inflight: set[str] = set()
        self.cool_until: dict[str, float] = {}
        self.flag_keys: dict[tuple[str, str], set[str]] = {}  # (topic, utterance) -> roles named by shown flags
        self.fails: dict[tuple[str, str], int] = {}
        self._n = 0

    def can_write(self, topic_id: str, now_s: float, level: str = "") -> bool:
        return (topic_id not in self.active and topic_id not in self.inflight
                and now_s >= self.cool_until.get(topic_id, 0.0)
                and self.fails.get((topic_id, level), 0) < self.MAX_FAILS)

    def failed(self, topic_id: str, level: str) -> None:
        self.fails[(topic_id, level)] = self.fails.get((topic_id, level), 0) + 1

    def cool(self, topic_id: str, now_s: float) -> None:
        self.cool_until[topic_id] = now_s + self.cooldown_s

    def add(self, cue: Cue, level: str = "") -> Cue:
        self._n += 1
        cue = cue.model_copy(update={"id": f"c{self._n:03d}", "state": "active"})
        self.cues[cue.id] = cue
        if cue.kind == "follow_up":
            self.active[cue.topic_id] = cue.id
            self.level[cue.id] = level
        return cue

    def is_new_flag(self, topic_id: str, utterance_id: str, roles: set[str] | frozenset = frozenset()) -> bool:
        """roles: the roles the flag's note names (see roles_named). Records the flag when it is new."""
        key = (topic_id, utterance_id)
        if key in self.flag_keys and not set(roles) - self.flag_keys[key]:
            return False
        self.flag_keys.setdefault(key, set()).update(roles)
        return True

    def resolve(self, cue_id: str, new_state: str, now_s: float) -> Cue | None:
        cue = self.cues.get(cue_id)
        if cue is None or cue.state != "active":
            return None
        cue = self.cues[cue_id] = cue.model_copy(update={"state": new_state})
        if cue.kind == "follow_up" and self.active.get(cue.topic_id) == cue_id:
            del self.active[cue.topic_id]
            self.cool(cue.topic_id, now_s)
        return cue

    def withdraw(self, topic_id: str) -> Cue | None:
        """Remove the active follow-up cue for a topic (its follow-up changed); no cooldown."""
        cue_id = self.active.pop(topic_id, None)
        return self.cues.get(cue_id) if cue_id else None
