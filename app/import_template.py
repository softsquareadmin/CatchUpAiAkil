"""Convert a CatchUpAI configuration template into a pack (deterministic, no model call):
python -m app.import_template <catchupai_template.json> --out packs/<id>.yaml [--id ID] [--version 0.1.0]
Template format: catchupai/templates.py and catchupai/models.py (CatchUpAI d547231). The result is tier 1 only
(no definitions); it is validated with the pack loader and not written if invalid. Every warning is printed."""
import argparse
import json
import sys
from pathlib import Path

import yaml

from app.models import GENERIC_PERSONA as DEFAULT_PERSONA
from app.pack import PackError, make_slug, pack_from_dict

FIELD_TYPES = ("text", "list", "status", "boolean", "score")  # catchupai/models.py FIELD_TYPES
BUILTIN_DUPLICATES = {"summary": "summary", "next_steps": "next_steps", "next_step": "next_steps",
                      "next_action": "next_steps", "recommended_next_action": "next_steps",
                      "recommended_next_actions": "next_steps", "recommended_next_steps": "next_steps"}


class TemplateError(ValueError):
    pass


def _need(cond: bool, message: str) -> None:
    if not cond:
        raise TemplateError(message)


def check_template(doc) -> dict:
    """The parts of CatchUp's validate_config this importer relies on; returns the config object."""
    _need(isinstance(doc, dict), "The template must be a JSON object.")
    config = doc.get("config", doc)  # a saved template wraps the config; a bare config also works
    _need(isinstance(config, dict), "The template has no 'config' object.")
    conv = config.get("conversation")
    _need(isinstance(conv, dict), "The template has no 'conversation' section.")
    _need(isinstance(conv.get("purpose"), str) and conv["purpose"].strip(), "Purpose must not be empty.")
    speakers = conv.get("speakers")
    _need(isinstance(speakers, list) and len(speakers) >= 2, "The template needs at least two speakers.")
    for s in speakers:
        _need(isinstance(s, dict) and str(s.get("role", "")).strip(), "Every speaker needs a role.")
    topics = conv.get("topics_to_cover", [])
    _need(isinstance(topics, list) and topics, "The template needs at least one topic to cover.")
    for t in topics:
        _need(isinstance(t, dict) and str(t.get("label", "")).strip(), "Every topic needs a label.")
        crit = t.get("criteria")
        _need(isinstance(crit, list) and crit and all(isinstance(c, str) and c.strip() for c in crit),
              f"Topic '{t.get('label')}' needs at least one coverage criterion.")
    report = config.get("report")
    _need(isinstance(report, dict) and isinstance(report.get("sections"), list), "The template has no report sections.")
    for s in report["sections"]:
        _need(isinstance(s, dict) and str(s.get("label", "")).strip() and str(s.get("instruction", "")).strip(),
              "Every report section needs a label and an instruction.")
        _need(s.get("type") in FIELD_TYPES, f"Report section '{s.get('label')}' has an unsupported type: {s.get('type')}.")
    return config


def convert(doc, pack_id: str | None = None, version: str = "0.1.0") -> tuple[dict, list[str]]:
    """(pack as a dict, warnings). Raises TemplateError or PackError with a clear message."""
    config = check_template(doc)
    conv, warnings = config["conversation"], []
    name = (doc.get("name") if isinstance(doc, dict) else None) or "Imported interview"
    roles, used = [], set()
    for i, s in enumerate(conv["speakers"]):
        rid = make_slug(s["role"], used, prefix="role")
        used.add(rid)
        roles.append({"id": rid, "label": s["role"].strip(), "kind": "user" if i == 0 else "subject"})
    warnings.append(f"Roles: '{roles[0]['label']}' was marked as the person using the tool (kind: user) and the others "
                    "as interviewees (kind: subject), because CatchUp templates do not say which is which. "
                    "Check the roles before using this pack.")
    topic_ids: set[str] = set()
    checklist = []
    for t in conv["topics_to_cover"]:
        tid = make_slug(t["label"], topic_ids)
        topic_ids.add(tid)
        checklist.append({"id": tid, "label": t["label"].strip(), "criteria": [c.strip() for c in t["criteria"]],
                          "required": True})
    section_ids: set[str] = set()
    custom = []
    for s in config["report"]["sections"]:
        sid = make_slug(s["label"], section_ids, prefix="section")  # catchupai make_section_id: unique, in order
        section_ids.add(sid)
        out = {"id": sid, "label": s["label"].strip(), "type": s["type"], "instruction": s["instruction"].strip(),
               "required": bool(s.get("required", True))}
        for key in ("options", "min", "max", "max_items"):
            if key in s:
                out[key] = s[key]
        custom.append(out)
        if sid in BUILTIN_DUPLICATES:
            warnings.append(f"Report section '{s['label']}' repeats the built-in review section "
                            f"'{BUILTIN_DUPLICATES[sid]}'. Both were kept; drop one if you do not need both.")
    pack = {
        "id": pack_id or make_slug(name, prefix="pack"), "version": version, "pack_format": 2, "name": name.strip(),
        "purpose": conv["purpose"].strip(), "pay_attention_to": [p.strip() for p in conv.get("pay_attention_to", [])],
        "persona": DEFAULT_PERSONA, "roles": roles, "checklist": checklist,
        "review": {"sections": ["summary", "topics_not_covered", "follow_ups", "next_steps"], "custom_sections": custom},
    }
    pack_from_dict(pack, f"{pack['id']}.yaml")  # refuse anything the engine cannot run
    return pack, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template")
    ap.add_argument("--out", required=True)
    ap.add_argument("--id")
    ap.add_argument("--version", default="0.1.0")
    a = ap.parse_args(argv)
    try:
        doc = json.loads(Path(a.template).read_text(encoding="utf-8"))
        pack, warnings = convert(doc, a.id, a.version)
    except (OSError, json.JSONDecodeError, TemplateError, PackError) as e:
        print(f"Not imported: {e}", file=sys.stderr)
        return 1
    for w in warnings:
        print(f"Warning: {w}")
    out = Path(a.out)
    if out.exists():
        print(f"Not imported: {out} already exists (packs are never overwritten; choose another name).", file=sys.stderr)
        return 1
    header = (f"# Imported from a CatchUpAI template ({Path(a.template).name}) by app.import_template.\n"
              "# Tier 1 only: the engine builds the topic text from label and criteria. Check the roles.\n")
    out.write_text(header + yaml.safe_dump(pack, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    print(f"Wrote {out} ({pack['id']}@{pack['version']}, {len(pack['checklist'])} topics, "
          f"{len(pack['review']['custom_sections'])} report sections)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
