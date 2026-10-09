"""Configuration screen backend (M9): read a pack for the editor, apply a manager's tier 1 edits (plus a topic's
definition and examples) onto the original, validate with the loader, and save as a new file with a new version.
The original file is never changed. Guardrails, special topics, review settings, form fields and prompts are not
editable here: they are always taken from the original pack, whatever the browser sends."""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.models import UseCasePack
from app.pack import PackError, check, make_slug

EDITABLE_TOP = ("name", "purpose", "pay_attention_to", "flag_when")
EDITABLE_TOPIC = ("id", "label", "criteria", "follow_up_when", "required", "definition", "examples")
FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.yaml$")
PLAIN = {"string_too_short": "This cannot be empty.", "missing": "This is required.",
         "string_pattern_mismatch": "Use lowercase letters, digits and underscores, starting with a letter."}


def read_raw(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def problems_of(raw: dict) -> list[dict]:
    """Every problem as {loc, msg}; loc is a dotted path such as "checklist.2.label" ("" when it is pack-wide)."""
    try:
        pack = UseCasePack.model_validate(with_ids(raw))
    except ValidationError as e:
        return [{"loc": ".".join(map(str, err["loc"])), "msg": PLAIN.get(err["type"], err["msg"])} for err in e.errors()]
    out = []
    for p in check(pack):
        m = re.match(r"^([a-z_]+(?:\.[a-z0-9_]+)*): (.*)$", p)
        out.append({"loc": m.group(1), "msg": m.group(2)} if m else {"loc": "", "msg": p})
    return out


def with_ids(raw: dict) -> dict:
    """Topic ids for new topics, made from the label (kept stable for existing topics)."""
    raw = dict(raw)
    used = {t.get("id") for t in raw.get("checklist") or [] if isinstance(t, dict) and t.get("id")}
    out = []
    for t in raw.get("checklist") or []:
        if isinstance(t, dict) and not t.get("id") and t.get("label"):
            t = {**t, "id": make_slug(t["label"], used)}
            used.add(t["id"])
        out.append(t)
    raw["checklist"] = out
    return raw


def merge(base: dict | None, edits: dict) -> dict:
    """The new pack: the original with the editable fields replaced. Without a base (an import), the edits are
    the whole pack, but they still pass the same loader."""
    if base is None:
        return with_ids(edits)
    new = dict(base)
    for key in EDITABLE_TOP:
        if key in edits:
            new[key] = edits[key]
    if isinstance(edits.get("roles"), list):  # labels and which role uses the tool; ids stay (scripts use them)
        by_id = {r["id"]: r for r in edits["roles"] if isinstance(r, dict) and "id" in r}
        new["roles"] = [{**r, **{k: by_id[r["id"]][k] for k in ("label", "kind") if k in by_id.get(r["id"], {})}}
                        for r in base.get("roles", [])]
    if isinstance(edits.get("checklist"), list):
        old = {t.get("id"): t for t in base.get("checklist", []) if isinstance(t, dict)}
        topics = []
        for t in edits["checklist"]:
            if not isinstance(t, dict):
                continue
            kept = {k: v for k, v in old.get(t.get("id"), {}).items() if k not in EDITABLE_TOPIC}  # e.g. demo hints
            item = {**kept, **{k: t[k] for k in EDITABLE_TOPIC if k in t}}
            item["criteria"] = [c.strip() for c in item.get("criteria", []) if str(c).strip()]
            item["examples"] = [e.strip() for e in item.get("examples", []) if str(e).strip()]
            for k in ("definition", "follow_up_when"):
                if k in item and not str(item[k]).strip():
                    item.pop(k)
            topics.append({k: v for k, v in item.items() if v not in ([], None)})
        new["checklist"] = topics
    return with_ids(new)


def next_version(version: str, taken: set[str]) -> str:
    parts = re.findall(r"\d+", version) or ["0", "1", "0"]
    nums = [int(x) for x in (parts + ["0", "0"])[:3]]
    while True:
        nums[2] += 1
        v = ".".join(map(str, nums))
        if v not in taken:
            return v


def versions_on_disk(packs_dir: Path, pack_id: str) -> set[str]:
    out = set()
    for p in Path(packs_dir).glob("*.yaml"):
        try:
            raw = read_raw(p)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict) and raw.get("id") == pack_id:
            out.add(str(raw.get("version")))
    return out


def save_new(packs_dir: Path, base_file: str | None, edits: dict, version: str | None, audit_dir: Path) -> dict:
    """Validate and write a new pack file. Returns {ok, file, version} or {ok: False, problems}."""
    base = None
    if base_file:
        if not FILE_RE.match(base_file) or not (Path(packs_dir) / base_file).is_file():
            return {"ok": False, "problems": [{"loc": "", "msg": "unknown pack file"}]}
        base = read_raw(Path(packs_dir) / base_file)
    raw = merge(base, edits)
    raw["pack_format"] = 2 if raw.get("roles") else raw.get("pack_format", 1)
    taken = versions_on_disk(packs_dir, str(raw.get("id", "")))
    wanted = (version or "").strip() or str(raw.get("version") or "0.1.0")
    if wanted in taken:  # never reuse a version that is already on disk (the original's included)
        wanted = next_version(wanted, taken)
    raw["version"] = wanted
    if problems := problems_of(raw):
        return {"ok": False, "problems": problems}
    pack_id = raw["id"]
    name = f"{pack_id}__{wanted.replace('.', '-')}.yaml"
    out = Path(packs_dir) / name
    if out.exists():
        return {"ok": False, "problems": [{"loc": "version", "msg": f"{name} already exists; choose another version"}]}
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (f"# Saved from the configuration screen {when}" + (f", based on {base_file}" if base_file else ", imported") +
              ".\n# The original file is unchanged. Guardrails and other advanced settings were copied, not edited.\n")
    out.write_text(header + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    Path(audit_dir).mkdir(parents=True, exist_ok=True)
    with (Path(audit_dir) / "config.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": when, "actor": "user", "action": "pack_saved", "file": name, "pack": f"{pack_id}@{wanted}",
                            "based_on": base_file}) + "\n")
    return {"ok": True, "file": name, "version": wanted, "pack": f"{pack_id}@{wanted}"}


def list_scripts(fixtures_dir: Path, role_ids: list[str]) -> list[dict]:
    """Sample transcripts whose speakers all belong to the pack (shown in "Test this template")."""
    out = []
    for p in sorted(Path(fixtures_dir).glob("*.json")):
        try:
            lines = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(lines, list) and lines and all(isinstance(l, dict) and "text" in l and "t_ms" in l for l in lines):
            speakers = {l.get("speaker") for l in lines}
            if speakers <= set(role_ids) | {"unknown"}:
                out.append({"file": p.name, "lines": len(lines)})
    return out


def estimate_cost(sessions_dir: Path, models: dict, lines: int, topics: int, window: int) -> dict:
    """Rough cost of a real-model template test from this repo's ledger: average cost per call of the configured
    gate and cue models, times the expected calls (an instant replay batches lines: about lines / window gate calls,
    rounded up and doubled for safety; at most one card per topic)."""
    per_call: dict[str, list[float]] = {"gate": [], "cue": []}
    for f in Path(sessions_dir).glob("*/ledger.jsonl"):
        for l in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(l)
            if r["role"] in per_call and r["ok"] and f"{r['provider']}:{r['model']}" == models.get(r["role"]):
                per_call[r["role"]].append(r["cost_usd"])
    gate_calls = 2 * math.ceil(lines / max(1, window))
    cue_calls = topics
    avg = {k: (sum(v) / len(v) if v else None) for k, v in per_call.items()}
    if avg["gate"] is None:
        return {"usd": None, "basis": "no earlier calls of this gate model in the ledger"}
    usd = avg["gate"] * gate_calls + (avg["cue"] or 0) * cue_calls
    return {"usd": round(usd, 4), "basis": f"average of {len(per_call['gate'])} gate and {len(per_call['cue'])} cue calls in "
                                          f"the ledger; about {gate_calls} gate and up to {cue_calls} cue calls"}


async def run_template_test(settings, pack: UseCasePack, lines: list[dict], sessions_dir: Path, real: bool) -> dict:
    """Replay a sample transcript at instant speed with this pack and return the resulting checklist and cards.
    Offline models unless `real` (the caller has already shown the estimate and had it confirmed)."""
    from app.pipeline import Session
    models = dict(settings.models)
    if not real:
        models.update(gate="fake:keyword", cue="fake:template")
    s = settings.model_copy(update={"experiment_label": "template-test" + ("" if real else "-offline"), "models": models})
    sent: list[dict] = []

    async def send(m):
        sent.append(m)
    session = Session(s, pack, sessions_dir, send, batch=True)
    if session.decider is None:
        await session.close()
        return {"ok": False, "error": f"checklist model is off: {session.gate_off_reason}"}
    await session.start_replay(lines, 0, "replay")
    await session.replay_task
    await session.gate_idle()
    await session.cue_idle()
    await session.close()
    return {"ok": True, "session": session.id, "real": real, "cost_usd": round(session.ledger.total_usd, 4),
            "items": [c.model_dump() for c in pack.checklist],
            "state": {k: v.model_dump() for k, v in session.checklist.items()},
            "cards": [m["cue"] for m in sent if m["type"] == "cue"],
            "utterances": [u.model_dump() for u in session.store.utterances]}
