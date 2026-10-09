"""Golden-run comparison (SPEC 12, M6): python -m app.golden [--experiment X] [--include-fake] [--all] [--sessions DIR]
Finds saved sessions whose transcript is the fixture script, reads each one's final checklist from events.jsonl, and
compares it with the expected answer key (tests/fixtures/timeline_v3.json, after the last line). No model calls.
Prints agreement per model combination and per topic, plus every follow-up question shown, with the leading-question
lint verdict, for manual review. Disagreement is information, not failure.
Only sessions run with the current pack are scored (the key belongs to it). Sessions record their pack from
2026-10-08; older ones are taken as current only if they started at or after FIRST_CURRENT_PACK_SESSION."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.config import ROOT, load_settings
from app.cues import Lint
from app.evidence import normalise
from app.models import Utterance
from app.pack import load_pack

FIXTURE = ROOT / "tests" / "fixtures" / "script_v2.json"
KEY = ROOT / "tests" / "fixtures" / "timeline_v3.json"
FIRST_CURRENT_PACK_SESSION = "20261008-022245"  # first run on cps_interview_v2@0.3.1 (before pack logging)
# Pack versions with the same topic definitions and answer key, so their runs are compared in one table (the pack
# column keeps them apart). 0.4.0 moved CPS rules from code into the pack without changing a definition (M7).
SAME_DEFINITIONS = {"cps_interview_v2@0.4.0": {"cps_interview_v2@0.3.1"}}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def expected_final(key: dict, topics: list[str], lines: int) -> dict[str, tuple[str, str]]:
    out = {}
    for topic in topics:  # topics missing from the key are expected to stay not covered
        hits = [(st, fu) for ln, st, fu in key["topics"].get(topic, []) if ln <= lines]
        out[topic] = hits[-1] if hits else ("not_covered", "none")
    return out


def matches(shown: tuple[str, str], expected: tuple[str, str]) -> bool:
    """Same rule as tests/compare_gates.py: strict on coverage and on required follow-ups; suggested is advisory."""
    return shown[0] == expected[0] and (shown[1] == "required") == (expected[1] == "required")


def load_run(d: Path, script: list[str], topics: list[str]) -> dict | None:
    """A scored run, or None if this session is not a full fixture run with the current status model."""
    utts = _jsonl(d / "utterances.jsonl")
    if [normalise(u["text"]) for u in utts] != script:
        return None
    final = {t: ("not_covered", "none") for t in topics}
    for e in _jsonl(d / "events.jsonl"):
        if e.get("kind") == "topic_update":
            st = e["state"]
            if "follow_up" not in st or st.get("status") not in ("partial", "covered"):
                return None  # recorded before the follow-up split (status "needs_follow_up"): not comparable
            final[st["item_id"]] = (st["status"], st["follow_up"])
    ledger = _jsonl(d / "ledger.jsonl")
    if not any(r["role"] == "gate" for r in ledger):
        return None  # no gate ran (gate off, or a review-only session)
    start = next((a["details"] for a in _jsonl(d / "audit.jsonl") if a["action"] == "session_start"), {})
    models = defaultdict(set)
    for r in ledger:
        models[r["role"]].add(f"{r['provider']}:{r['model']}")
    utterances = [Utterance(**u) for u in utts]
    cues = [e["cue"] for e in _jsonl(d / "events.jsonl") if e.get("kind") == "cue"]
    return {"session": d.name, "experiment": ledger[0]["experiment"] if ledger else start.get("experiment", "?"),
            "pack": start.get("pack", "not logged"),
            "gate": " + ".join(sorted(models["gate"])) or start.get("gate_model", "?"),
            "cue": " + ".join(sorted(models["cue"])) or "-", "final": final, "cues": cues, "utterances": utterances,
            "cost": sum(r["cost_usd"] for r in ledger)}


def build(sessions_dir: Path, experiment: str | None = None, include_fake: bool = False,
          all_packs: bool = False) -> dict:
    pack = load_pack(ROOT / load_settings(env_file=None, environ={}).pack)
    current = f"{pack.id}@{pack.version}"
    script = [normalise(l["text"]) for l in json.loads(FIXTURE.read_text(encoding="utf-8"))]
    key = json.loads(KEY.read_text(encoding="utf-8"))
    expected = expected_final(key, [c.id for c in pack.checklist], len(script))
    lint = Lint(pack)
    runs = []
    for d in sorted(p for p in Path(sessions_dir).iterdir() if p.is_dir()):
        r = load_run(d, script, list(expected))
        if r is None or (experiment and r["experiment"] != experiment):
            continue
        if not include_fake and r["gate"].startswith("fake:"):
            continue
        logged = r["pack"] if r["pack"] != "not logged" else (
            "cps_interview_v2@0.3.1" if d.name >= FIRST_CURRENT_PACK_SESSION else "older")
        if not all_packs and logged != current and logged not in SAME_DEFINITIONS.get(current, ()):
            continue
        r["pack"] = logged
        runs.append(r)
    combos: dict[tuple, list[dict]] = defaultdict(list)
    for r in runs:
        combos[(r["experiment"], r["gate"], r["cue"], r["pack"])].append(r)
    rows, per_topic, review = [], {}, []
    for (exp, gate, cue, pack), rs in combos.items():
        n = len(rs)
        status = [sum(r["final"][t][0] == expected[t][0] for t in expected) for r in rs]
        both = [sum(matches(r["final"][t], expected[t]) for t in expected) for r in rs]
        missed = sum(expected[t][1] == "required" and r["final"][t][1] != "required" for r in rs for t in expected)
        name = f"{exp} · {gate}" + (f" · cue {cue}" if cue != "-" else "")
        lint_fail = 0
        for r in rs:
            for c in r["cues"]:
                v = (lint.question(c["question_or_note"], r["utterances"]) if c["kind"] == "follow_up"
                     else lint.note(c["question_or_note"]))
                lint_fail += v is not None
                review.append({"combination": name, "session": r["session"], "kind": c["kind"], "topic": c["topic_id"],
                               "text": c["question_or_note"], "lint": v or "ok"})
        rows.append({"experiment": exp, "gate": gate, "cue": cue, "pack": pack, "runs": n,
                     "final_status_of_11": round(sum(status) / n, 2), "status_and_required_of_11": round(sum(both) / n, 2),
                     "required_missed": missed, "cues_and_flags": sum(len(r["cues"]) for r in rs),
                     "lint_failures": lint_fail, "cost_per_run_usd": round(sum(r["cost"] for r in rs) / n, 4)})
        per_topic[name] = {t: f"{sum(matches(r['final'][t], expected[t]) for r in rs)}/{n}" for t in expected}
    return {"rows": rows, "per_topic": per_topic, "expected": expected, "review": review}


def main(argv: list[str] | None = None) -> None:
    from app.report import table
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--experiment")
    ap.add_argument("--include-fake", action="store_true", help="also score offline keyword-gate sessions")
    ap.add_argument("--all", action="store_true", help="also score sessions run with older packs")
    ap.add_argument("--sessions", default=str(ROOT / "sessions"))
    a = ap.parse_args(argv)
    g = build(Path(a.sessions), a.experiment, a.include_fake, a.all)
    print("Golden run: final checklist vs the answer key (timeline_v3), per model combination\n" + table(g["rows"]))
    if g["per_topic"]:
        names = list(g["per_topic"])
        print("\nPer topic (runs agreeing on status and required follow-up), columns = combinations above in order")
        print(table([{"topic": t, "expected": "/".join(e), **{f"#{i + 1}": g["per_topic"][n][t] for i, n in enumerate(names)}}
                     for t, e in g["expected"].items()]))
    if g["review"]:
        print("\nCards and flags shown, for manual review (lint = automatic leading-question / conclusion check)")
        print(table(g["review"]))


if __name__ == "__main__":
    main()
