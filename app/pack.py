"""Pack loader (interview type). Validates the file and rejects anything the engine cannot run.
pack_format 1 = the original layout (still loads); pack_format 2 adds name, purpose, roles, tier 1 topics
(label + criteria), guardrails, special topics and review sections. See SPEC-M7-generalization.md."""
import math
import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.models import UseCasePack


class PackError(ValueError):
    pass


def make_slug(label: str, used=(), prefix: str = "topic") -> str:
    """Unique lowercase id from a label (same rule as catchupai/models.py make_section_id)."""
    ascii_label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", ascii_label.lower()).strip("_") or prefix
    if base[0].isdigit():
        base = f"{prefix}_{base}"
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def load_pack(path: str | Path) -> UseCasePack:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PackError(f"Pack file not found: {path}") from None
    except yaml.YAMLError as e:
        raise PackError(f"Pack file {path.name} is not valid YAML: {e}") from None
    return pack_from_dict(raw, path.name)


def pack_from_dict(raw, name: str = "pack") -> UseCasePack:
    if not isinstance(raw, dict):
        raise PackError(f"Pack file {name} must be a mapping at the top level")
    raw = dict(raw)
    used: set[str] = {t["id"] for t in raw.get("checklist") or [] if isinstance(t, dict) and t.get("id")}
    topics = []
    for t in raw.get("checklist") or []:
        if isinstance(t, dict) and not t.get("id") and t.get("label"):
            t = {**t, "id": make_slug(t["label"], used)}
            used.add(t["id"])
        topics.append(t)
    raw["checklist"] = topics
    try:
        pack = UseCasePack.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise PackError(f"Pack file {name} is invalid: {problems}") from None
    if problems := check(pack):
        raise PackError(f"Pack file {name} is invalid: " + "; ".join(problems))
    return pack


def check(pack: UseCasePack) -> list[str]:
    problems = []
    if not pack.checklist:
        problems.append("checklist is empty")
    topic_ids = [c.id for c in pack.checklist]
    for n, i in enumerate(topic_ids):
        if topic_ids.count(i) > 1 and topic_ids.index(i) != n:
            problems.append(f"checklist.{n}.id: duplicate checklist id '{i}'")
    for n, c in enumerate(pack.checklist):
        if not c.definition.strip() and not [x for x in c.criteria if x.strip()]:
            problems.append(f"checklist.{n}.criteria: topic '{c.label}' needs at least one line under "
                            "'A complete answer includes' (criteria)")
    # roles
    role_ids = [r.id for r in pack.roles]
    if pack.pack_format == 2 or pack.roles:
        if not pack.roles:
            problems.append("roles: add the roles (exactly one 'user', at least one 'subject')")
        users = [r for r in pack.roles if r.kind == "user"]
        if pack.roles and len(users) != 1:
            problems.append(f"roles: exactly one role must be kind 'user' (the person using the tool), found {len(users)}")
        if pack.roles and not pack.subject_ids:
            problems.append("roles: at least one role must be kind 'subject'")
        problems += [f"duplicate role id '{i}'" for i in sorted({i for i in role_ids if role_ids.count(i) > 1})]
        if "unknown" in role_ids:
            problems.append("role id 'unknown' is reserved")
    else:
        problems.append("roles: missing (needed to know who uses the tool and whose answers count)")
    if pack.pack_format == 2:
        for key in ("name", "purpose"):
            if not getattr(pack, key).strip():
                problems.append(f"{key}: {key} is empty")
    # special topics and review settings must point at real topics and roles
    for key in ("consent_topic", "audience_hint_topic"):
        t = getattr(pack.special_topics, key)
        if t and t not in topic_ids:
            problems.append(f"special_topics.{key}: unknown topic '{t}'")
    rv = pack.review
    for g in rv.account_groups:
        problems += [f"review.account_groups: unknown topic '{t}'" for t in g.topics if t not in topic_ids]
    for x in rv.extra_sources:
        if x.spoken_by not in role_ids:
            problems.append(f"review.extra_sources '{x.id}': spoken_by '{x.spoken_by}' is not a role")
        if x.id in role_ids:
            problems.append(f"review.extra_sources '{x.id}': id clashes with a role")
    problems += [f"review.instructions: unknown section '{k}'" for k in rv.instructions if k not in rv.sections]
    problems += custom_section_problems([s.model_dump() for s in rv.custom_sections])
    # forms
    field_ids = [f.id for f in pack.form_schema]
    problems += [f"duplicate form field id '{i}'" for i in sorted({i for i in field_ids if field_ids.count(i) > 1})]
    for f in pack.form_schema:
        for t in f.source_topic_ids:
            if t not in topic_ids:
                problems.append(f"form field '{f.id}' references unknown topic '{t}'")
        if f.type == "choice" and not f.choices:
            problems.append(f"form field '{f.id}' is type 'choice' but has no choices")
    # guardrail patterns must compile
    for pat in pack.rails.note_blocked_patterns:
        try:
            re.compile(pat)
        except re.error as e:
            problems.append(f"guardrails.note_blocked_patterns: '{pat}' is not a valid pattern ({e})")
    return problems


def custom_section_problems(sections: list[dict]) -> list[str]:
    """Rules from catchupai/models.py validate_report_sections (d547231)."""
    problems, ids = [], set()
    for i, s in enumerate(sections, 1):
        where = f"review.custom_sections {i} ({s.get('label', '?')})"
        if s["id"] in ids:
            problems.append(f"{where}: duplicate id '{s['id']}'")
        ids.add(s["id"])
        kind = s["type"]
        if kind == "status":
            opts = s.get("options") or []
            if len(opts) < 2 or len(set(opts)) != len(opts) or not all(str(o).strip() for o in opts):
                problems.append(f"{where}: status needs at least two different, non-empty options")
        if kind == "score":
            lo, hi = s.get("min"), s.get("max")
            if lo is None or hi is None or not (math.isfinite(lo) and math.isfinite(hi)) or lo >= hi:
                problems.append(f"{where}: score needs finite min and max, with min smaller than max")
        if kind == "list" and s.get("max_items") is not None and s["max_items"] < 1:
            problems.append(f"{where}: max_items must be a positive whole number")
        extra = {"status": "options", "score": "min max", "list": "max_items"}
        for key in ("options", "min", "max", "max_items"):
            if s.get(key) is not None and key not in extra.get(kind, "").split():
                problems.append(f"{where}: '{key}' is not used by type {kind}")
    return problems


def list_packs(packs_dir: Path) -> list[dict]:
    """Every packs/*.yaml with its id, version and name, or the reason it does not load."""
    out = []
    for p in sorted(Path(packs_dir).glob("*.yaml")):
        try:
            pk = load_pack(p)
            out.append({"file": p.name, "id": pk.id, "version": pk.version, "name": pk.name or pk.id, "ok": True})
        except PackError as e:
            out.append({"file": p.name, "ok": False, "error": str(e)})
    return out
