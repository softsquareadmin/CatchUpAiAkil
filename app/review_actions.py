"""The user's review decisions on a saved draft (M5, M11; moved here in M12 so the review page works for a live
session and for a past one read from disk): accept / edit / reject / reset an item or a topic part, set a topic's
status. Each call changes the draft in place, writes the audit row and returns the message for the screen, or an
error. The caller saves the draft."""
from app.audit import AuditLog
from app.models import ReviewItem, UseCasePack
from app.review import apply_action, apply_topic_action, set_topic_status, topic_coverage


def item_action(draft: dict, audit: AuditLog, item_id: str, action: str, value=None) -> tuple[str | None, dict | None]:
    items = [ReviewItem(**i) for i in draft["items"]]
    before = next((i for i in items if i.id == item_id), None)
    out = apply_action(items, item_id, action, value)
    if isinstance(out, str):
        return out, None
    draft["items"] = [i.model_dump() for i in items]
    audit.write("user", f"review_{action}", item_id=item_id, section=out.section, status=out.status,
                **({"old_value": before.value, "new_value": out.value} if action == "edit" else {}))
    return None, {"type": "review_item", "item": out.model_dump()}


def topic_action(draft: dict, audit: AuditLog, topic_id: str, part: str, action: str,
                 value=None) -> tuple[str | None, dict | None]:
    if "topics" not in draft:
        return "this review has no topic results", None
    before = next((dict(t[part]) for t in draft["topics"] if t["topic_id"] == topic_id and t.get(part)), None)
    out = apply_topic_action(draft["topics"], topic_id, part, action, value)
    if isinstance(out, str):
        return out, None
    audit.write("user", f"review_{action}", item_id=f"topic.{topic_id}.{part}", section="topics", status=out["status"],
                **({"old_value": before["value"], "new_value": out["value"]} if action == "edit" else {}))
    topic = next(t for t in draft["topics"] if t["topic_id"] == topic_id)
    return None, {"type": "review_topic", "topic": topic, "coverage": draft["coverage"]}


def topic_status(draft: dict, audit: AuditLog, pack: UseCasePack, topic_id: str,
                 status: str) -> tuple[str | None, dict | None]:
    if "topics" not in draft:
        return "this review has no topic results", None
    old = next((t["status"] for t in draft["topics"] if t["topic_id"] == topic_id), None)
    out = set_topic_status(draft["topics"], topic_id, status)
    if isinstance(out, str):
        return out, None
    draft["coverage"] = topic_coverage(pack, draft["topics"])
    audit.write("user", "topic_status_changed_by_user", topic_id=topic_id, old=old, new=status,
                ai_status=out["review_status"] or out["live_status"])
    return None, {"type": "review_topic", "topic": out, "coverage": draft["coverage"]}


def apply(draft: dict, audit: AuditLog, pack: UseCasePack, body: dict) -> tuple[str | None, dict | None]:
    """One decision from the review page: {"kind": "item" | "topic" | "status", ...}."""
    kind = body.get("kind")
    if kind == "item":
        return item_action(draft, audit, str(body.get("id")), str(body.get("action")), body.get("value"))
    if kind == "topic":
        return topic_action(draft, audit, str(body.get("topic_id")), str(body.get("part")), str(body.get("action")),
                            body.get("value"))
    if kind == "status":
        return topic_status(draft, audit, pack, str(body.get("topic_id")), str(body.get("status")))
    return "kind must be item, topic or status", None
