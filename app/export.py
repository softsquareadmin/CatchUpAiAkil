"""Report export (SPEC-M11 11.4): the JSON export and a single self-contained HTML file built from a session folder.
Only what the user accepted or edited goes in; unreviewed items only on request, marked "Not reviewed". Everything
from a transcript or a model is HTML-escaped; no external requests; the same input gives the same file."""
import base64
import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from app.models import UseCasePack
from app.review import MISSING_PREFIX, NOT_STATED, OVERALL, TOPIC_PARTS, section_titles, topic_coverage
from app.store import read_utterances

REPORT_FORMAT_VERSION = 2  # 2: layout, review counts, item ids and sources (2026-10-14, report readability)
TOOL_STATEMENT = ("This tool listens to an interview, tracks which checklist topics were covered and suggests "
                  "follow-up questions. It does not judge credibility, make findings or decide anything; a person "
                  "reviews every item.")
STATUS = {"covered": "Covered", "partial": "Partial", "not_covered": "Not covered"}
RANK = {"not_covered": 0, "partial": 1, "covered": 2}
INPUTS = {"typed": "Typed", "replay": "Replay", "paste": "Pasted transcript", "mic": "Microphone", "file": "Audio file"}
KEPT = ("accepted", "edited")
# Softsquare logo, embedded so the exported file stays self-contained (owner, 2026-10-14)
LOGO = Path(__file__).parent / "static" / "img" / "softsquare-agentic.png"


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []


def load_session(d: Path) -> dict:
    """Everything the report needs from sessions/<id>/ (works after a restart)."""
    return {"session_id": d.name, "draft": json.loads((d / "draft_note.json").read_text(encoding="utf-8")),
            "utterances": read_utterances(d), "audit": _jsonl(d / "audit.jsonl"),
            "events": _jsonl(d / "events.jsonl"), "ledger": _jsonl(d / "ledger.jsonl")}


def clock(ms: int) -> str:
    s = int(ms or 0) // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def quoter(utterances: list[dict], pack: UseCasePack):
    """Evidence -> quotes with the speaker and time of each line."""
    utts = {u["id"]: u for u in utterances}
    role = {r.id: r.label for r in pack.roles}

    def quotes(evidence: list[dict]) -> list[dict]:
        out = []
        for e in evidence:
            u = utts.get(e["utterance_id"], {})
            out.append({"utterance_id": e["utterance_id"], "quote": e["quote"], "speaker": u.get("speaker"),
                        "speaker_label": role.get(u.get("speaker"), u.get("speaker") or "unknown"),
                        "t_ms": u.get("t_start_ms")})
        return out
    return quotes


def follow_up_levels(draft: dict) -> dict[str, str]:
    """Topic id -> the live follow-up level ("required" gets a badge and goes first)."""
    return {o["id"]: o.get("follow_up", "none") for o in (draft.get("checklist") or {}).get("open_topics", [])}


def layout(pack: UseCasePack, utterances: list[dict]) -> dict:
    """Where sections go, shared by the report and the review page (2026-10-14, report readability)."""
    return {"lead": list(pack.review.lead_sections), "flags_into": pack.review.live_flags_into,
            "account_columns": [{"id": x.id, "label": x.label} for x in pack.roles if x.kind == "subject"]
            + [{"id": x.id, "label": x.label} for x in pack.review.extra_sources],
            "account_groups": {t: g.label for g in pack.review.account_groups for t in g.topics},
            "speaker_note": any(u.get("source") in ("mic", "file") for u in utterances)}


def live_flags(events: list[dict], quotes) -> list[dict]:
    """The flags shown during the interview, with the quotes of later lines the gate marked as repeats."""
    added: dict[str, list[dict]] = {}  # flag cue id -> quotes from later lines
    for e in events:
        if e.get("kind") == "flag_added_to":
            added.setdefault(e["cue_id"], []).append(e["evidence"])
    return [{"topic_id": e["cue"]["topic_id"], "note": e["cue"]["question_or_note"],
             "evidence": quotes([e["cue"]["evidence"], *added.get(e["cue"]["id"], [])]), "status": "proposed"}
            for e in events if e.get("kind") == "cue" and e["cue"]["kind"] == "flag"]


def consent_line(draft: dict, audit: list[dict], pack: UseCasePack) -> dict | None:
    if not ((ct := pack.special_topics.consent_topic) and draft.get("topics")):
        return None
    t = next((t for t in draft["topics"] if t["topic_id"] == ct), None)
    acts = [a["action"] for a in audit if a["action"].startswith("consent_refusal")]
    check = ("the user stopped the tool" if "consent_refusal_confirmed" in acts else
             "raised; the user chose to continue" if "consent_refusal_not_confirmed" in acts else
             "raised, no answer recorded" if "consent_refusal_detected" in acts else "none raised")
    rec = [a for a in audit if a["action"] == "consent_recorded"]
    return {"topic": t["label"] if t else ct, "status": t["status"] if t else "not_covered", "refusal_check": check,
            "recorded": [pack.role_label(r) for r in rec[-1]["details"]["roles"]] if rec else None}


def notes_of(events: list[dict]) -> list[dict]:
    return [{"t_ms": e["t_ms"], "text": e["text"]} for e in events if e.get("kind") == "note"]


def review_page_extras(d: Path, draft: dict | None, pack: UseCasePack, utterances: list[dict]) -> dict:
    """What the review page needs beyond the draft to lay itself out like the report."""
    events, audit = _jsonl(d / "events.jsonl"), _jsonl(d / "audit.jsonl")
    return {"layout": layout(pack, utterances), "flags": live_flags(events, quoter(utterances, pack)),
            "notes": notes_of(events), "consent": consent_line(draft, audit, pack) if draft else None,
            "follow_up_levels": follow_up_levels(draft) if draft else {},
            "missing_prefix": MISSING_PREFIX, "not_stated": NOT_STATED}


def build_report(data: dict, pack: UseCasePack, *, include_unreviewed: bool = False, transcript: bool = False,
                 cost: bool = False, reviewer: str = "", sample: bool = False, now: datetime | None = None) -> dict:
    """The JSON export. The HTML is rendered from exactly this."""
    draft, now = data["draft"], now or datetime.now(timezone.utc)
    role = {r.id: r.label for r in pack.roles}
    user_label = pack.user_role.label.lower()
    show = KEPT + (("proposed",) if include_unreviewed else ())
    counts = {"rejected": 0, "not_reviewed": 0}

    def keep(status: str) -> bool:
        if status == "rejected":
            counts["rejected"] += 1
        elif status == "proposed" and not include_unreviewed:
            counts["not_reviewed"] += 1
        return status in show

    quotes = quoter(data["utterances"], pack)
    levels = follow_up_levels(draft)
    review_total = review_done = 0  # model-written parts the user can accept, edit or reject (not live flags)
    topics = []
    for t in sorted(draft.get("topics", []), key=lambda t: RANK[t["status"]]):  # stable: pack order inside a group
        row = {k: t[k] for k in ("topic_id", "label", "required", "status", "status_by", "live_status",
                                 "review_status", "status_reason")}
        row["evidence"] = quotes(t["evidence"])
        row["follow_up_level"] = levels.get(t["topic_id"], "none")  # the live level: "required" gets a badge
        for part in TOPIC_PARTS:
            if t.get(part):
                review_total += 1
                review_done += t[part]["status"] != "proposed"
        for part in TOPIC_PARTS:
            p = t.get(part)
            row[part] = {"value": p["value"], "status": p["status"]} if p and keep(p["status"]) else None
        topics.append(row)
    cov = topic_coverage(pack, draft.get("topics", [])) if draft.get("topics") else None

    titles = {s["id"]: s["title"] for s in draft.get("sections") or section_titles(pack)}
    sections, overall, form_fields = [], None, []
    for sec, title in titles.items():
        if sec != "form":  # the report no longer shows form fields (2026-10-14); the JSON keeps form_fields
            for i in draft["items"]:
                if i["section"] == sec:
                    review_total += 1
                    review_done += i["status"] != "proposed"
        rows = [{"id": i["id"], "label": i["label"], "value": i["value"], "status": i["status"],
                 "evidence": quotes(i["evidence"]), "basis_topics": i.get("basis_topics") or [],
                 "source": i.get("source"), "topic_id": i.get("topic_id"),
                 **({"field_id": i["field_id"]} if i.get("field_id") else {})}
                for i in draft["items"] if i["section"] == sec and (sec == "form" or keep(i["status"]))]
        rows = [i for i in rows if sec != "form" or i["status"] in show]  # form: JSON only, not in the counts
        if sec == OVERALL:
            overall = {"title": title, "label": "Draft for human review", "items": rows} if rows else None
        elif rows and sec != "form":
            sections.append({"id": sec, "title": title, "items": rows})
        if sec == "form":
            form_fields = [{"field_id": r["field_id"], "value": r["value"], "evidence": r["evidence"],
                            "status": r["status"]} for r in rows]

    audit, events = data["audit"], data["events"]
    start = next((a for a in audit if a["action"] == "session_start"), {})
    models = (start.get("details") or {}).get("models", {})
    review_acts = [a["ts"] for a in audit if a["actor"] == "user" and
                   (a["action"].startswith("review_") or a["action"] == "topic_status_changed_by_user")]
    notes = notes_of(events)
    flags = live_flags(events, quotes)
    counts["not_reviewed"] += 0 if include_unreviewed else len(flags)
    consent = consent_line(draft, audit, pack)

    report = {
        "report_format_version": REPORT_FORMAT_VERSION, "sample": sample,
        "session_id": data["session_id"], "pack": pack.ref, "pack_name": pack.name or pack.id,
        "started_at": start.get("ts"), "duration_s": max((u["t_end_ms"] for u in data["utterances"]), default=0) // 1000,
        "roles": [{"id": r.id, "label": r.label, "kind": r.kind} for r in pack.roles],
        "input": sorted({INPUTS.get(u.get("source"), u.get("source") or "unknown") for u in data["utterances"]}),
        "reviewed_by": reviewer.strip() or f"the {user_label}",
        "reviewed_on": max(review_acts)[:10] if review_acts else None,  # None: nobody has reviewed anything yet
        "exported_at": now.isoformat(timespec="seconds"),
        "options": {"include_unreviewed": include_unreviewed, "transcript": transcript, "cost": cost},
        "coverage": cov, "topics": topics, "custom_and_other_sections": sections, "overall_assessment": overall,
        "notes": notes, "flags": flags if include_unreviewed else [], "consent": consent,  # flags: shown live
        "notice": draft.get("notice"),
        "models": {k: models[k] for k in ("gate", "cue", "review") if k in models},
        "not_included": counts, "user_label": user_label,
        "reviewed_count": {"reviewed": review_done, "total": review_total},
        "layout": layout(pack, data["utterances"]),
        # kept from the M5 export so earlier readers still work
        "form_fields": form_fields, "sections": {s["id"]: s["items"] for s in sections if s["id"] != "form"},
        "section_titles": titles, "checklist": draft.get("checklist", {}),
    }
    if transcript:
        report["transcript"] = [{"id": u["id"], "speaker": u["speaker"], "speaker_label": role.get(u["speaker"], u["speaker"]),
                                 "t_ms": u["t_start_ms"], "text": u["text"]} for u in data["utterances"]]
    if cost:
        report["cost_usd"] = round(sum(r.get("cost_usd") or 0 for r in data["ledger"]), 4)
    return report


# Ring and card styling adapted from catchupai/topic_coverage.py COVERAGE_CSS (layout only; colours as tokens).
# Layout (2026-10-14, report readability): every section is a native <details> (open or closed by default), so the
# content stays in the page for find, works without JavaScript, and opens fully for printing.
CSS = """
:root{--bg:#fff;--fg:#15285a;--muted:#5d6f94;--line:#dde6f4;--panel:#f7f9fd;--accent:#087bff;--track:#eaf0f8;
--cov:#08a353;--cov-bg:#dff9eb;--par:#a35f00;--par-bg:#fff0d7;--not:#c8102e;--not-bg:#ffe5eb;--mark:#7a4d00;--mark-bg:#fff1d6}
@media (prefers-color-scheme:dark){:root{--bg:#10151f;--fg:#e6ecf7;--muted:#9fb0cf;--line:#2c3850;--panel:#171e2b;
--accent:#5aa6ff;--track:#2c3850;--cov:#5bd497;--cov-bg:#163626;--par:#f2b24c;--par-bg:#3a2a10;--not:#ff7a8c;
--not-bg:#3d1720;--mark:#f2c879;--mark-bg:#3a2e14}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:860px;padding:24px 16px 48px;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;overflow-wrap:anywhere}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0}h3{font-size:16px;margin:0}
h4{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 0 4px}
a{color:var(--accent)}
.logo{display:block;height:52px;width:auto;margin:0 0 14px}
@media (prefers-color-scheme:dark){.logo{background:#fff;border-radius:6px;padding:6px 8px;height:64px}}
.meta{color:var(--muted);font-size:13px;margin:0}.meta b{color:var(--fg);font-weight:600}
.sample{display:inline-block;background:var(--mark-bg);color:var(--mark);border-radius:999px;padding:2px 10px;
font-weight:700;font-size:13px;margin-bottom:8px}
.banner{border:2px solid var(--fg);border-radius:8px;padding:10px 14px;margin:16px 0 6px;font-weight:600}
.omitted{color:var(--muted);font-size:13px;margin:4px 0}
.coverage{display:flex;align-items:center;gap:28px;padding:16px;border:1px solid var(--line);border-radius:9px;
background:var(--panel)}
.ring{width:112px;height:112px;flex-shrink:0;padding:12px;border-radius:50%;
background:conic-gradient(var(--accent) var(--p),var(--track) 0);transform:rotate(-90deg)}
.ring>div{height:100%;border-radius:50%;background:var(--panel);display:flex;flex-direction:column;
justify-content:center;align-items:center;transform:rotate(90deg)}
.ring strong{font-size:26px;line-height:1.2}.ring span{font-size:11px;color:var(--muted);text-align:center}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;flex:1}
.stat b{display:block;font-size:26px}.stat{font-size:13px;color:var(--muted)}
.stat.covered b{color:var(--cov)}.stat.partial b{color:var(--par)}.stat.not_covered b{color:var(--not)}
.covnote{font-size:13px;color:var(--muted);margin:8px 0 0}
.jump{display:flex;flex-wrap:wrap;gap:6px 14px;margin:12px 0 0;padding:0;list-style:none;font-size:14px}
.cov-title{margin:28px 0 10px}
details.sec{margin:26px 0 0}details.sec>summary,details.group>summary{cursor:pointer;list-style-position:outside}
details.sec>summary h2,details.group>summary h3{display:inline}
.count{color:var(--muted);font-size:14px;font-weight:400}
.secbody{padding:4px 0 0}
section.topics>h2{margin:26px 0 6px}details.group{margin:8px 0 0}details.group>summary{padding:6px 0}
details.topic{--c:var(--muted);--cb:var(--panel);border:1px solid var(--line);border-left:6px solid var(--c);
border-radius:8px;margin:10px 0;break-inside:avoid;page-break-inside:avoid}
.topic.covered{--c:var(--cov);--cb:var(--cov-bg)}.topic.partial{--c:var(--par);--cb:var(--par-bg)}
.topic.not_covered{--c:var(--not);--cb:var(--not-bg)}
.topic>summary{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 14px;cursor:pointer}
.topic>summary h3{flex:1;min-width:12em}
.fq{flex-basis:100%;margin:0;font-size:14px}.fq b{color:var(--muted);font-weight:600}
.sym{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;
background:var(--c);color:var(--bg);font-weight:700}
.chip{color:var(--c);background:var(--cb);border:1px solid var(--c);padding:2px 10px;border-radius:999px;
font-size:12px;white-space:nowrap}
.req{color:var(--not);background:var(--not-bg);border:1px solid var(--not);padding:2px 10px;border-radius:999px;
font-size:12px;font-weight:700;white-space:nowrap}
.pts{font-size:13px;color:var(--muted)}
.mark{background:var(--mark-bg);color:var(--mark);border-radius:999px;padding:1px 8px;font-size:12px;
font-weight:600;white-space:nowrap}
.unrev{border:1px dashed var(--mark);border-radius:6px;padding:4px 8px}
.tbody{padding:0 14px 12px}.empty{color:var(--muted);font-style:italic}
.changed{font-size:13px;background:var(--panel);border-radius:6px;padding:6px 10px;margin:8px 0 0}
ol.quotes,ul.items{margin:4px 0;padding-left:22px}ol.quotes li,ul.items li{margin:6px 0}
q{font-style:italic}.who{color:var(--muted);font-size:12px;white-space:nowrap}
.refs{font-size:12px;margin-left:6px}.refs a{white-space:nowrap}.refs a+a{margin-left:8px}p.who{white-space:normal}
table.accounts{width:100%;border-collapse:collapse;font-size:14px;margin:6px 0}
.accounts th,.accounts td{border:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}
.accounts thead th{background:var(--panel);font-size:13px}.accounts tbody th{width:18%}
.cell+.cell{margin-top:8px}.accounts .unh{white-space:nowrap;overflow-wrap:normal}.unheard{font-size:14px;margin:8px 0}
.see{font-size:14px;color:var(--muted)}
.overall{border:2px dashed var(--mark);border-radius:9px;padding:4px 16px 12px;margin-top:28px}
.notes h2{margin:28px 0 6px}
.transcript p{margin:4px 0}.transcript .who{display:inline-block;min-width:9em}
footer{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
#expand{float:right;font:inherit;font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--panel);color:var(--fg);cursor:pointer}
@media (max-width:640px){.coverage{flex-direction:column;align-items:stretch}.ring{align-self:center}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}
.accounts thead{display:none}.accounts,.accounts tbody,.accounts tr,.accounts th,.accounts td{display:block;width:100%}
.accounts tr{margin:0 0 10px}.accounts tbody th{width:100%;background:var(--panel)}
.accounts td::before{content:attr(data-label);display:block;font-size:12px;font-weight:700;color:var(--muted)}}
@media print{:root{--bg:#fff;--fg:#000;--muted:#333;--line:#999;--panel:#fff;--accent:#000;--track:#ccc;--cov:#000;
--cov-bg:#fff;--par:#000;--par-bg:#fff;--not:#000;--not-bg:#fff;--mark:#000;--mark-bg:#fff}
body{max-width:none;padding:0}#expand,.jump{display:none}.ring{print-color-adjust:exact;-webkit-print-color-adjust:exact}
details::details-content{content-visibility:visible;display:block}a{color:inherit;text-decoration:none}}
"""

# Expand / collapse every section; a link opens the sections around its target; printing opens everything.
SCRIPT = ("<script>(function(){var b=document.getElementById('expand');b.hidden=false;function all(){return "
          "document.querySelectorAll('details')}b.onclick=function(){var d=all(),o=[].some.call(d,function(x){return "
          "!x.open});[].forEach.call(d,function(x){x.open=o});b.textContent=o?'Collapse all':'Expand all'};function "
          "show(id){var n=document.getElementById(id);if(n)[].forEach.call(all(),function(x){if(x.contains(n))x.open=true})}"
          "document.addEventListener('click',function(ev){var a=ev.target.closest&&ev.target.closest('a[href^=\"#\"]');"
          "if(a)show(decodeURIComponent(a.getAttribute('href').slice(1)))});window.addEventListener('hashchange',"
          "function(){show(decodeURIComponent(location.hash.slice(1)))});if(location.hash)show(decodeURIComponent("
          "location.hash.slice(1)));window.addEventListener('beforeprint',function(){[].forEach.call(all(),function(x)"
          "{x.open=true})})})();</script>")
SYMBOL = {"covered": "&#10003;", "partial": "&#9684;", "not_covered": "!"}
NOUNS = {"summary": "statements", "timeline": "events", "next_steps": "options"}  # count shown on a closed section
NO_LEAD_IN = ("next_steps",)  # sections whose item label ("Option") only repeats the heading


def logo_img() -> str:
    data = base64.b64encode(LOGO.read_bytes()).decode()
    return f'<img class="logo" src="data:image/png;base64,{data}" alt="Softsquare, The Agentic AI Experts">'


def render_html(r: dict) -> str:
    e = escape
    anchors = bool(r.get("transcript"))
    user = r["user_label"]
    lay = r.get("layout") or {"lead": [], "flags_into": "", "account_columns": [], "account_groups": {},
                              "speaker_note": False}
    secs = {s["id"]: s for s in r["custom_and_other_sections"]}
    topics = {t["topic_id"]: t for t in r["topics"]}
    lead = [sid for sid in lay["lead"] if (sid in secs or (sid == lay["flags_into"] and r["flags"]))
            and sid not in ("accounts", "missing_accounts")]  # those are always the accounts table

    def cls(*names: str) -> str:
        names = [n for n in names if n]
        return f' class="{" ".join(names)}"' if names else ""

    def mark(status: str) -> str:  # only what the user did is marked; unreviewed items keep the dashed outline
        return {"accepted": ' <span class="mark">accepted</span>', "edited": ' <span class="mark">edited</span>'
                }.get(status, "")

    def unrev(status: str) -> str:
        return "unrev" if status == "proposed" else ""

    def basis(item: dict) -> str:
        """An item about what was not said rests on checklist statuses instead of quotes."""
        topics_ = item.get("basis_topics") or []
        return ('<p class="who">Based on the checklist: ' + "; ".join(
            f'{e(t["label"])} ({STATUS[t["status"]].lower()})' for t in topics_) + "</p>") if topics_ else ""

    def quote_list(evidence: list[dict]) -> str:
        if not evidence:
            return ""
        li = []
        for q in evidence:
            ref = (f'<a href="#{e(q["utterance_id"])}">{e(q["utterance_id"])}</a>' if anchors else e(q["utterance_id"]))
            when = f', {clock(q["t_ms"])}' if q.get("t_ms") is not None else ""
            li.append(f'<li><q>{e(q["quote"])}</q> <span class="who">{e(q["speaker_label"])}{when} · {ref}</span></li>')
        return f'<ol class="quotes">{"".join(li)}</ol>'

    # Each full quote is shown in its main place (the lead sections, then the topics); elsewhere only a link to it,
    # or to the transcript line when the transcript is included. A quote with no main place stays in full.
    main: dict[str, str] = {}
    folded: list[tuple[str, dict]] = []  # (anchor, flag): live flags shown in the flags_into section
    for sid in lead:
        items_ = secs[sid]["items"] if sid in secs else []
        for i in items_:
            for q in i["evidence"]:
                main.setdefault(q["utterance_id"], f'item-{i["id"]}')
        if sid == lay["flags_into"]:
            listed = {q["utterance_id"] for i in items_ for q in i["evidence"]}
            for n, f in enumerate(r["flags"], 1):  # a flag whose lines are all already listed here repeats it
                if not all(q["utterance_id"] in listed for q in f["evidence"]):
                    folded.append((f"flag-{n}", f))
                    for q in f["evidence"]:
                        main.setdefault(q["utterance_id"], f"flag-{n}")
    for t in r["topics"]:
        for q in t["evidence"]:
            main.setdefault(q["utterance_id"], f'topic-{t["topic_id"]}')

    def refs(evidence: list[dict]) -> str:
        links, full = [], []
        for q in evidence:
            target = q["utterance_id"] if anchors else main.get(q["utterance_id"])
            if target is None:
                full.append(q)
                continue
            text = f'{clock(q["t_ms"])} · {q["utterance_id"]}' if q.get("t_ms") is not None else q["utterance_id"]
            links.append(f'<a href="#{e(target)}" title="{e(q["speaker_label"])}: {e(q["quote"])}">{e(text)}</a>')
        return (f' <span class="refs">{"".join(links)}</span>' if links else "") + quote_list(full)

    def item(i: dict, sid: str, title: str, full_quotes: bool) -> str:
        lead_in = "" if i["label"] == title or sid in NO_LEAD_IN else f'<b>{e(i["label"])}:</b> '
        body = quote_list(i["evidence"]) if full_quotes else refs(i["evidence"])
        return (f'<li id="item-{e(i["id"])}"{cls(unrev(i["status"]))}>{lead_in}{e(i["value"])}{mark(i["status"])}'
                f'{body}{basis(i)}</li>')

    def section(sid: str, title: str, inner: str, is_open: bool, count: int | None = None, noun: str = "items") -> str:
        n = f'<span class="count"> · {count} {noun}</span>' if count is not None else ""
        return (f'<details class="sec" id="sec-{e(sid)}"{" open" if is_open else ""}><summary><h2>{e(title)}</h2>{n}'
                f'</summary><div class="secbody">{inner}</div></details>')

    started = (r["started_at"] or "")[:16].replace("T", " ")
    roles = ", ".join(f'{x["label"]}{" (user)" if x["kind"] == "user" else ""}' for x in r["roles"])
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{e(r["pack_name"])} report {e(r["session_id"])}</title><style>{CSS}</style></head><body>',
           '<header><button id="expand" type="button" hidden>Expand all</button>', logo_img()]
    if r["sample"]:
        out.append('<p class="sample">Sample, fictional data</p>')
    out.append(f'<h1>{e(r["pack_name"])}</h1><p class="meta"><b>Pack</b> {e(r["pack"])} · <b>Date</b> {e(started)} UTC'
               f' · <b>Duration</b> {clock(r["duration_s"] * 1000)} · <b>Input</b> {e(", ".join(r["input"]) or "none")}'
               f'</p><p class="meta"><b>Roles</b> {e(roles)} · <b>Session</b> {e(r["session_id"])}</p></header>')
    reviewed = (f'Reviewed by {e(r["reviewed_by"])} on {e(r["reviewed_on"])}.' if r["reviewed_on"] else
                f'Not yet reviewed by {e(r["reviewed_by"])}.')
    out.append(f'<p class="banner">AI-assisted draft. {reviewed} Not a decision.</p>')
    if rc := r.get("reviewed_count"):
        out.append(f'<p class="omitted">{rc["reviewed"]} of {rc["total"]} items reviewed.</p>')
    if lay["speaker_note"]:
        out.append(f'<p class="omitted">Speaker labels come from automatic transcription and can be wrong; the {e(user)} '
                   'corrects them.</p>')
    ni = r["not_included"]
    if ni["rejected"]:
        out.append(f'<p class="omitted">{ni["rejected"]} item{"s" if ni["rejected"] != 1 else ""} rejected by the '
                   f'{e(user)}, not shown.</p>')
    if ni["not_reviewed"]:
        out.append(f'<p class="omitted">{ni["not_reviewed"]} item{"s" if ni["not_reviewed"] != 1 else ""} not reviewed, '
                   'not shown.</p>')
    if r.get("notice"):
        out.append(f'<p class="omitted">{e(r["notice"])}</p>')

    # the sections below the topics, in the order they are shown
    accounts, missing = secs.get("accounts"), secs.get("missing_accounts")
    rest = [s for s in r["custom_and_other_sections"]
            if s["id"] not in lead and s["id"] not in ("accounts", "missing_accounts") and s["id"] != "form"]
    timeline = secs.get("timeline")
    stated = sum(1 for i in (timeline["items"] if timeline else [])
                 if NOT_STATED not in i["label"].rsplit(" · ", 1)[0].lower())
    rest = [s for s in rest if s["id"] != "timeline" or stated >= 2]  # hidden when fewer than 2 dated events

    cov = r["coverage"]
    if cov:
        out.append(f'<h2 class="cov-title">{e(cov["label"])}</h2><section class="coverage">')
        if cov["score"] is not None:
            out.append(f'<div class="ring" style="--p:{cov["score"]}%"><div><strong>{cov["score"]}%</strong>'
                       f'<span>{e(cov["label"])}</span></div></div>')
        c = cov["counts"]
        out.append('<div class="stats">' + "".join(
            f'<div class="stat {k}"><b>{c[k]}</b>{STATUS[k]}</div>' for k in ("covered", "partial", "not_covered"))
            + f'<div class="stat"><b>{cov["required"]}</b>Required topics</div></div></section>')
        out.append('<p class="covnote">How much of the checklist the conversation covered (covered 100, partial 50, '
                   'not covered 0, averaged over required topics). It is not a rating of anyone.'
                   + (f' {e(cov["note"])}' if cov["note"] else "") + '</p>')
        jumps = [(f"sec-{sid}", secs[sid]["title"] if sid in secs else sid) for sid in lead[:1]]
        jumps.append(("topics", "Topics"))
        if accounts or missing:
            jumps.append(("sec-accounts" if accounts else "sec-missing_accounts", "Accounts"))
        if "next_steps" in secs:
            jumps.append(("sec-next_steps", "Next steps"))
        if anchors:
            jumps.append(("sec-transcript", "Transcript"))
        out.append('<ul class="jump">' + "".join(f'<li><a href="#{e(h)}">{e(t)}</a></li>' for h, t in jumps) + "</ul>")

    # lead sections: right after the coverage, open; the flags raised live are folded into one of them
    for sid in lead:
        s = secs.get(sid) or {"id": sid, "title": sid, "items": []}
        lis = [item(i, sid, s["title"], True) for i in s["items"]]
        if sid == lay["flags_into"]:
            lis += [f'<li id="{a}" class="unrev">{e(f["note"])} <span class="who">raised during the interview</span>'
                    f'{quote_list(f["evidence"])}</li>' for a, f in folded]
        out.append(section(sid, s["title"], f'<ul class="items">{"".join(lis)}</ul>', True))

    if cov:
        need = sorted((t for t in r["topics"] if t["status"] != "covered"),  # required follow-ups first
                      key=lambda t: t.get("follow_up_level") != "required")
        done = [t for t in r["topics"] if t["status"] == "covered"]
        out.append('<section class="topics" id="topics"><h2>Topics</h2>')
        for title, group, is_open in (("Needs follow-up", need, True), ("Covered", done, False)):
            if not group:
                continue
            out.append(f'<details class="group"{" open" if is_open else ""}><summary><h3>{title} ({len(group)})</h3>'
                       '</summary>')
            for t in group:
                st = t["status"]
                pts = {"covered": 100, "partial": 50, "not_covered": 0}[st]
                extra = "" if t["required"] else ' <span class="pts">optional</span>'
                score = f'<span class="pts">{pts}%</span>' if cov["score"] is not None and t["required"] else ""
                req = '<span class="req">Required follow-up</span>' if t.get("follow_up_level") == "required" else ""
                by = f' <span class="mark">set by {e(user)}</span>' if t["status_by"] == "user" else ""
                fq = ""
                if f := t["follow_up"]:
                    fq = (f'<p{cls("fq", unrev(f["status"]))}><b>Suggested follow-up:</b> {e(f["value"])}'
                          f'{mark(f["status"])}</p>')
                out.append(f'<details class="topic {st}" id="topic-{e(t["topic_id"])}"><summary>'
                           f'<span class="sym" aria-hidden="true">{SYMBOL[st]}</span><h3>{e(t["label"])}{extra}</h3>'
                           f'{req}{score}<span class="chip">{STATUS[st]}</span>{by}{fq}</summary><div class="tbody">')
                if t["status_by"] == "review":
                    out.append(f'<p class="changed">Changed by the review from {STATUS[t["live_status"]]} to '
                               f'{STATUS[st]}: {e(t["status_reason"])}</p>')
                elif t["status_by"] == "user":
                    ai = t["review_status"] or t["live_status"]
                    out.append(f'<p class="changed">Set by the {e(user)}; the AI status was {STATUS[ai]}.</p>')
                out.append("<h4>Summary</h4>")
                if t["summary"]:
                    sm = t["summary"]
                    out.append(f'<p{cls(unrev(sm["status"]))}>{e(sm["value"])}{mark(sm["status"])}</p>')
                elif st == "not_covered" and not t["evidence"]:
                    out.append('<p class="empty">Not discussed.</p>')
                else:
                    out.append('<p class="empty">No summary.</p>')
                if t["evidence"]:
                    out.append("<h4>Quotes</h4>" + quote_list(t["evidence"]))
                if m := t["missing"]:
                    out.append(f'<h4>Still missing{mark(m["status"])}</h4><ul{cls("items", unrev(m["status"]))}>'
                               + "".join(f"<li>{e(x)}</li>" for x in m["value"]) + "</ul>")
                out.append("</div></details>")
            out.append("</details>")
        out.append("</section>")

    if accounts:  # a table: one row per topic (or account group), one column per source, unheard people last
        items_ = accounts["items"]
        cols = [c for c in lay["account_columns"] if any(i["source"] == c["id"] for i in items_)]
        cols += [{"id": i["source"], "label": i["source"]} for i in items_
                 if i["source"] and i["source"] not in {c["id"] for c in lay["account_columns"]}]
        cols = list({c["id"]: c for c in cols}.values())
        unheard = [(i, i["label"][len(MISSING_PREFIX):] if i["label"].startswith(MISSING_PREFIX) else i["label"])
                   for i in (missing["items"] if missing else [])]
        rows: dict[str, list[dict]] = {}
        for i in items_:
            g = lay["account_groups"].get(i["topic_id"]) or topics.get(i["topic_id"], {}).get("label") or i["label"]
            rows.setdefault(g, []).append(i)
        head = "".join(f'<th scope="col">{e(c["label"])}</th>' for c in cols) + "".join(
            f'<th scope="col" class="unh">{e(n)}</th>' for _, n in unheard)
        body = []
        for g, its in rows.items():
            tds = []
            for c in cols:
                here = [i for i in its if i["source"] == c["id"]]
                cell = "".join(f'<div{cls("cell", unrev(i["status"]))}>{e(i["value"])}{mark(i["status"])}'
                               f'{refs(i["evidence"])}</div>' for i in here) or '<span class="empty">Nothing said</span>'
                tds.append(f'<td data-label="{e(c["label"])}">{cell}</td>')
            tds += [f'<td data-label="{e(n)}" class="unh"><span class="empty">Not yet heard</span></td>' for _, n in unheard]
            body.append(f'<tr><th scope="row">{e(g)}</th>{"".join(tds)}</tr>')
        inner = (f'<table class="accounts"><thead><tr><th scope="col">Topic</th>{head}</tr></thead>'
                 f'<tbody>{"".join(body)}</tbody></table>')
        inner += "".join(f'<p id="item-{e(i["id"])}"{cls("unheard", unrev(i["status"]))}><b>{e(n)}: not yet heard.</b> '
                         f'{e(i["value"])}{mark(i["status"])}{refs(i["evidence"])}</p>' for i, n in unheard)
        out.append(section("accounts", accounts["title"], inner, True))
    elif missing:
        rest.insert(0, missing)

    for s in rest:
        sid, its = s["id"], s["items"]
        if sid == "next_steps":  # an option that only repeats a topic's suggested follow-up becomes a link to it
            keep, see = [], []
            for i in its:
                b = i["basis_topics"]
                t = topics.get(b[0]["id"]) if len(b) == 1 else None
                if (t and t["follow_up"] and t.get("follow_up_level") != "required" and i["status"] == "proposed"
                        and {q["utterance_id"] for q in i["evidence"]} <= {q["utterance_id"] for q in t["evidence"]}):
                    see.append(t)
                else:
                    keep.append(i)
            inner = f'<ul class="items">{"".join(item(i, sid, s["title"], False) for i in keep)}</ul>'
            if see:
                links = ", ".join(f'<a href="#topic-{e(t["topic_id"])}">{e(t["label"])}</a>'
                                  for t in {t["topic_id"]: t for t in see}.values())
                inner += f'<p class="see">Also see the suggested follow-ups for: {links}.</p>'
            out.append(section(sid, s["title"], inner, len(keep) <= 4, len(keep), NOUNS[sid]))
        elif sid in NOUNS:
            out.append(section(sid, s["title"], f'<ul class="items">{"".join(item(i, sid, s["title"], False) for i in its)}'
                                                '</ul>', False, len(its), NOUNS[sid]))
        else:
            out.append(section(sid, s["title"], f'<ul class="items">{"".join(item(i, sid, s["title"], True) for i in its)}'
                                                '</ul>', True))
    if oa := r["overall_assessment"]:
        out.append(f'<section class="overall"><h2>{e(oa["title"])} <span class="mark">{e(oa["label"])}</span></h2>'
                   '<ul class="items">')
        out += [item(i, OVERALL, oa["title"], True) for i in oa["items"]]
        out.append("</ul></section>")

    flags = r["flags"] if not lay["flags_into"] else []
    if r["notes"] or flags or r["consent"]:
        out.append('<section class="notes"><h2>Notes' + (", flags" if flags else "") + " and consent</h2>")
        if r["notes"]:
            out.append(f'<h4>Notes by the {e(user)}</h4><ul class="items">'
                       + "".join(f'<li><span class="who">{clock(n["t_ms"])}</span> {e(n["text"])}</li>' for n in r["notes"])
                       + "</ul>")
        if flags:
            out.append('<h4>Flags raised during the interview</h4><ul class="items">'
                       + "".join(f'<li class="unrev">{e(f["note"])}{quote_list(f["evidence"])}</li>' for f in flags)
                       + "</ul>")
        if c := r["consent"]:
            who = "" if c.get("recorded") is None else \
                f' Recorded by the {e(user)}: {e(", ".join(c["recorded"]) or "nobody")}.'
            out.append(f'<h4>Consent</h4><p>{e(c["topic"])}: {STATUS[c["status"]]}.{who} Refusal check: '
                       f'{e(c["refusal_check"])}.</p>')
        out.append("</section>")
    if r.get("transcript"):
        lines = "".join(f'<p id="{e(u["id"])}"><span class="who">{clock(u["t_ms"])} {e(u["speaker_label"])}</span> '
                        f'{e(u["text"])}</p>' for u in r["transcript"])
        out.append(section("transcript", "Transcript", f'<div class="transcript">{lines}</div>', False,
                           len(r["transcript"]), "lines"))
    models = " · ".join(f"{k}: {e(v)}" for k, v in r["models"].items())
    out.append(f'<footer><p>Models: {models or "not recorded"}. Generated on {e(r["exported_at"][:10])}.'
               + (f' Model cost for this session: ${r["cost_usd"]:.4f}.' if "cost_usd" in r else "")
               + f'</p><p>{e(TOOL_STATEMENT)}</p></footer>{SCRIPT}</body></html>')
    return "\n".join(out)
