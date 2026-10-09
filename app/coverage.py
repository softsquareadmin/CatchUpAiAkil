"""Checklist coverage (SPEC-M11 11.1): how much of the checklist the conversation covered, computed in code from the
final topic statuses. It is never a rating of a person. Points as in catchupai/topic_coverage.py (covered 100,
partial 50, not covered 0); the average is over required topics only."""
import math

from app.models import UseCasePack

LABEL = "Checklist coverage"
POINTS = {"covered": 100, "partial": 50, "not_covered": 0}


def coverage(pack: UseCasePack, statuses: dict[str, str], not_applicable: frozenset[str] = frozenset()) -> dict:
    """statuses: topic id -> final status (missing = not covered). Topics in `not_applicable` are listed and left out
    of the average. score is None when the pack hides the number or there are no required topics."""
    required = [c for c in pack.checklist if c.required and c.id not in not_applicable]
    status = {c.id: statuses.get(c.id) if statuses.get(c.id) in POINTS else "not_covered" for c in pack.checklist}
    counts = {k: sum(status[c.id] == k for c in required) for k in POINTS}
    score = math.floor(sum(POINTS[status[c.id]] for c in required) / len(required) + 0.5) if required else None
    optional = [{"id": c.id, "label": c.label, "status": status[c.id]} for c in pack.checklist
                if not c.required and c.id not in not_applicable]
    na = [{"id": c.id, "label": c.label} for c in pack.checklist if c.id in not_applicable]
    notes = []
    if not required:
        notes.append("No required topics, so there is no coverage score.")
    if optional:
        n = len(optional)
        notes.append(f"{n} optional topic{'s are' if n > 1 else ' is'} shown with {'their' if n > 1 else 'its'} "
                     "status but not counted in the score.")
    if na:
        notes.append(f"Not applicable, not counted: {', '.join(t['label'] for t in na)}.")
    show = pack.review.show_coverage_score
    return {"label": LABEL, "show_score": show, "score": score if show else None, "counts": counts,
            "required": len(required), "optional": optional, "not_applicable": na, "note": " ".join(notes)}
