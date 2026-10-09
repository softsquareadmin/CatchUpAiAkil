"""Report export (SPEC-M11 11.4): the JSON export and a single self-contained HTML file built from a session folder.
Only what the user accepted or edited goes in; unreviewed items only on request, marked "Not reviewed". Everything
from a transcript or a model is HTML-escaped; no external requests; the same input gives the same file."""
import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from app.models import UseCasePack
from app.review import OVERALL, TOPIC_PARTS, section_titles, topic_coverage
from app.store import read_utterances

REPORT_FORMAT_VERSION = 1
TOOL_STATEMENT = ("This tool listens to an interview, tracks which checklist topics were covered and suggests "
                  "follow-up questions. It does not judge credibility, make findings or decide anything; a person "
                  "reviews every item.")
STATUS = {"covered": "Covered", "partial": "Partial", "not_covered": "Not covered"}
RANK = {"not_covered": 0, "partial": 1, "covered": 2}
INPUTS = {"typed": "Typed", "replay": "Replay", "paste": "Pasted transcript", "mic": "Microphone", "file": "Audio file"}
KEPT = ("accepted", "edited")


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


def build_report(data: dict, pack: UseCasePack, *, include_unreviewed: bool = False, transcript: bool = False,
                 cost: bool = False, reviewer: str = "", sample: bool = False, now: datetime | None = None) -> dict:
    """The JSON export. The HTML is rendered from exactly this."""
    draft, now = data["draft"], now or datetime.now(timezone.utc)
    utts = {u["id"]: u for u in data["utterances"]}
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

    def quotes(evidence: list[dict]) -> list[dict]:
        out = []
        for e in evidence:
            u = utts.get(e["utterance_id"], {})
            out.append({"utterance_id": e["utterance_id"], "quote": e["quote"], "speaker": u.get("speaker"),
                        "speaker_label": role.get(u.get("speaker"), u.get("speaker") or "unknown"),
                        "t_ms": u.get("t_start_ms")})
        return out

    topics = []
    for t in sorted(draft.get("topics", []), key=lambda t: RANK[t["status"]]):  # stable: pack order inside a group
        row = {k: t[k] for k in ("topic_id", "label", "required", "status", "status_by", "live_status",
                                 "review_status", "status_reason")}
        row["evidence"] = quotes(t["evidence"])
        for part in TOPIC_PARTS:
            p = t.get(part)
            row[part] = {"value": p["value"], "status": p["status"]} if p and keep(p["status"]) else None
        topics.append(row)
    cov = topic_coverage(pack, draft.get("topics", [])) if draft.get("topics") else None

    titles = {s["id"]: s["title"] for s in draft.get("sections") or section_titles(pack)}
    sections, overall, form_fields = [], None, []
    for sec, title in titles.items():
        rows = [{"label": i["label"], "value": i["value"], "status": i["status"], "evidence": quotes(i["evidence"]),
                 "basis_topics": i.get("basis_topics") or [],
                 **({"field_id": i["field_id"]} if i.get("field_id") else {})}
                for i in draft["items"] if i["section"] == sec and keep(i["status"])]
        if sec == OVERALL:
            overall = {"title": title, "label": "Draft for human review", "items": rows} if rows else None
        elif rows:
            sections.append({"id": sec, "title": title, "items": rows})
        if sec == "form":
            form_fields = [{"field_id": r["field_id"], "value": r["value"], "evidence": r["evidence"],
                            "status": r["status"]} for r in rows]

    audit, events = data["audit"], data["events"]
    start = next((a for a in audit if a["action"] == "session_start"), {})
    models = (start.get("details") or {}).get("models", {})
    review_acts = [a["ts"] for a in audit if a["actor"] == "user" and
                   (a["action"].startswith("review_") or a["action"] == "topic_status_changed_by_user")]
    notes = [{"t_ms": e["t_ms"], "text": e["text"]} for e in events if e.get("kind") == "note"]
    flags = [{"topic_id": e["cue"]["topic_id"], "note": e["cue"]["question_or_note"],
              "evidence": quotes([e["cue"]["evidence"]]), "status": "proposed"}
             for e in events if e.get("kind") == "cue" and e["cue"]["kind"] == "flag"]
    counts["not_reviewed"] += 0 if include_unreviewed else len(flags)
    consent = None
    if (ct := pack.special_topics.consent_topic) and draft.get("topics"):
        t = next((t for t in draft["topics"] if t["topic_id"] == ct), None)
        acts = [a["action"] for a in audit if a["action"].startswith("consent_refusal")]
        check = ("the user stopped the tool" if "consent_refusal_confirmed" in acts else
                 "raised; the user chose to continue" if "consent_refusal_not_confirmed" in acts else
                 "raised, no answer recorded" if "consent_refusal_detected" in acts else "none raised")
        consent = {"topic": t["label"] if t else ct, "status": t["status"] if t else "not_covered", "refusal_check": check}

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
        "notes": notes, "flags": flags if include_unreviewed else [], "consent": consent,
        "notice": draft.get("notice"),
        "models": {k: models[k] for k in ("gate", "cue", "review") if k in models},
        "not_included": counts, "user_label": user_label,
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
CSS = """
:root{--bg:#fff;--fg:#15285a;--muted:#5d6f94;--line:#dde6f4;--panel:#f7f9fd;--accent:#087bff;--track:#eaf0f8;
--cov:#08a353;--cov-bg:#dff9eb;--par:#a35f00;--par-bg:#fff0d7;--not:#c8102e;--not-bg:#ffe5eb;--mark:#7a4d00;--mark-bg:#fff1d6}
@media (prefers-color-scheme:dark){:root{--bg:#10151f;--fg:#e6ecf7;--muted:#9fb0cf;--line:#2c3850;--panel:#171e2b;
--accent:#5aa6ff;--track:#2c3850;--cov:#5bd497;--cov-bg:#163626;--par:#f2b24c;--par-bg:#3a2a10;--not:#ff7a8c;
--not-bg:#3d1720;--mark:#f2c879;--mark-bg:#3a2e14}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:860px;padding:24px 16px 48px;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;overflow-wrap:anywhere}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:28px 0 10px}h3{font-size:16px;margin:0}
h4{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 0 4px}
.meta{color:var(--muted);font-size:13px;margin:0}.meta b{color:var(--fg);font-weight:600}
.sample{display:inline-block;background:var(--mark-bg);color:var(--mark);border-radius:999px;padding:2px 10px;
font-weight:700;font-size:13px;margin-bottom:8px}
.banner{border:2px solid var(--fg);border-radius:8px;padding:10px 14px;margin:16px 0;font-weight:600}
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
details.topic{--c:var(--muted);--cb:var(--panel);border:1px solid var(--line);border-left:6px solid var(--c);
border-radius:8px;margin:10px 0;break-inside:avoid;page-break-inside:avoid}
.topic.covered{--c:var(--cov);--cb:var(--cov-bg)}.topic.partial{--c:var(--par);--cb:var(--par-bg)}
.topic.not_covered{--c:var(--not);--cb:var(--not-bg)}
.topic>summary{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:10px 14px;cursor:pointer}
.topic>summary h3{flex:1;min-width:12em}
.sym{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;
background:var(--c);color:var(--bg);font-weight:700}
.chip{color:var(--c);background:var(--cb);border:1px solid var(--c);padding:2px 10px;border-radius:999px;
font-size:12px;white-space:nowrap}
.pts{font-size:13px;color:var(--muted)}
.mark{background:var(--mark-bg);color:var(--mark);border-radius:999px;padding:1px 8px;font-size:12px;
font-weight:600;white-space:nowrap}
.unrev{border:1px dashed var(--mark);border-radius:6px;padding:4px 8px}
.tbody{padding:0 14px 12px}.empty{color:var(--muted);font-style:italic}
.changed{font-size:13px;background:var(--panel);border-radius:6px;padding:6px 10px;margin:8px 0 0}
ol.quotes,ul.items{margin:4px 0;padding-left:22px}ol.quotes li,ul.items li{margin:4px 0}
q{font-style:italic}.who{color:var(--muted);font-size:12px;white-space:nowrap}
.overall{border:2px dashed var(--mark);border-radius:9px;padding:4px 16px 12px;margin-top:28px}
.transcript p{margin:4px 0}.transcript .who{display:inline-block;min-width:9em}
footer{margin-top:36px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
#expand{float:right;font:inherit;font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;
background:var(--panel);color:var(--fg);cursor:pointer}
@media (max-width:640px){.coverage{flex-direction:column;align-items:stretch}.ring{align-self:center}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media print{:root{--bg:#fff;--fg:#000;--muted:#333;--line:#999;--panel:#fff;--accent:#000;--track:#ccc;--cov:#000;
--cov-bg:#fff;--par:#000;--par-bg:#fff;--not:#000;--not-bg:#fff;--mark:#000;--mark-bg:#fff}
body{max-width:none;padding:0}#expand{display:none}.ring{print-color-adjust:exact;-webkit-print-color-adjust:exact}
details.topic::details-content{content-visibility:visible;display:block}a{color:inherit;text-decoration:none}}
"""

SCRIPT = ("<script>(function(){var b=document.getElementById('expand');b.hidden=false;var d=document.querySelectorAll("
          "'details.topic');b.onclick=function(){var o=[].some.call(d,function(x){return !x.open});[].forEach.call(d,"
          "function(x){x.open=o});b.textContent=o?'Collapse all':'Expand all'};window.addEventListener('beforeprint',"
          "function(){[].forEach.call(d,function(x){x.open=true})})})();</script>")
SYMBOL = {"covered": "&#10003;", "partial": "&#9684;", "not_covered": "!"}


def render_html(r: dict) -> str:
    e = escape
    anchors = bool(r.get("transcript"))
    user = r["user_label"]

    def mark(status: str) -> str:
        return {"edited": ' <span class="mark">edited</span>',
                "proposed": ' <span class="mark">Not reviewed</span>'}.get(status, "")

    def unrev(status: str) -> str:
        return ' class="unrev"' if status == "proposed" else ""

    def basis(item: dict) -> str:
        """An item about what was not said rests on checklist statuses instead of quotes."""
        topics = item.get("basis_topics") or []
        return ('<p class="who">Based on the checklist: ' + "; ".join(
            f'{e(t["label"])} ({STATUS[t["status"]].lower()})' for t in topics) + "</p>") if topics else ""

    def quote_list(evidence: list[dict]) -> str:
        if not evidence:
            return ""
        li = []
        for q in evidence:
            ref = (f'<a href="#{e(q["utterance_id"])}">{e(q["utterance_id"])}</a>' if anchors else e(q["utterance_id"]))
            when = f', {clock(q["t_ms"])}' if q.get("t_ms") is not None else ""
            li.append(f'<li><q>{e(q["quote"])}</q> <span class="who">{e(q["speaker_label"])}{when} · {ref}</span></li>')
        return f'<ol class="quotes">{"".join(li)}</ol>'

    started = (r["started_at"] or "")[:16].replace("T", " ")
    roles = ", ".join(f'{x["label"]}{" (user)" if x["kind"] == "user" else ""}' for x in r["roles"])
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{e(r["pack_name"])} report {e(r["session_id"])}</title><style>{CSS}</style></head><body>',
           '<header><button id="expand" type="button" hidden>Expand all</button>']
    if r["sample"]:
        out.append('<p class="sample">Sample, fictional data</p>')
    out.append(f'<h1>{e(r["pack_name"])}</h1><p class="meta"><b>Pack</b> {e(r["pack"])} · <b>Date</b> {e(started)} UTC'
               f' · <b>Duration</b> {clock(r["duration_s"] * 1000)} · <b>Input</b> {e(", ".join(r["input"]) or "none")}'
               f'</p><p class="meta"><b>Roles</b> {e(roles)} · <b>Session</b> {e(r["session_id"])}</p></header>')
    reviewed = (f'Reviewed by {e(r["reviewed_by"])} on {e(r["reviewed_on"])}.' if r["reviewed_on"] else
                f'Not yet reviewed by {e(r["reviewed_by"])}.')
    out.append(f'<p class="banner">AI-assisted draft. {reviewed} Not a decision.</p>')
    ni = r["not_included"]
    if ni["rejected"]:
        out.append(f'<p class="omitted">{ni["rejected"]} item{"s" if ni["rejected"] != 1 else ""} rejected by the '
                   f'{e(user)}, not shown.</p>')
    if ni["not_reviewed"]:
        out.append(f'<p class="omitted">{ni["not_reviewed"]} item{"s" if ni["not_reviewed"] != 1 else ""} not reviewed, '
                   'not shown.</p>')
    if r.get("notice"):
        out.append(f'<p class="omitted">{e(r["notice"])}</p>')

    cov = r["coverage"]
    if cov:
        out.append(f'<h2>{e(cov["label"])}</h2><section class="coverage">')
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

        out.append("<h2>Topics</h2>")
        for t in r["topics"]:
            st = t["status"]
            pts = {"covered": 100, "partial": 50, "not_covered": 0}[st]
            extra = "" if t["required"] else ' <span class="pts">optional</span>'
            score = f'<span class="pts">{pts}%</span>' if cov["score"] is not None and t["required"] else ""
            by = f' <span class="mark">set by {e(user)}</span>' if t["status_by"] == "user" else ""
            out.append(f'<details class="topic {st}"{" open" if st != "covered" else ""}><summary>'
                       f'<span class="sym" aria-hidden="true">{SYMBOL[st]}</span><h3>{e(t["label"])}{extra}</h3>'
                       f'{score}<span class="chip">{STATUS[st]}</span>{by}</summary><div class="tbody">')
            if t["status_by"] == "review":
                out.append(f'<p class="changed">Changed by the review from {STATUS[t["live_status"]]} to {STATUS[st]}: '
                           f'{e(t["status_reason"])}</p>')
            elif t["status_by"] == "user":
                ai = t["review_status"] or t["live_status"]
                out.append(f'<p class="changed">Set by the {e(user)}; the AI status was {STATUS[ai]}.</p>')
            out.append("<h4>Summary</h4>")
            if t["summary"]:
                out.append(f'<p{unrev(t["summary"]["status"])}>{e(t["summary"]["value"])}{mark(t["summary"]["status"])}</p>')
            elif st == "not_covered" and not t["evidence"]:
                out.append('<p class="empty">Not discussed.</p>')
            else:
                out.append('<p class="empty">No summary.</p>')
            if t["evidence"]:
                out.append("<h4>Quotes</h4>" + quote_list(t["evidence"]))
            if t["missing"]:
                m = t["missing"]
                out.append(f'<h4>Still missing{mark(m["status"])}</h4><ul class="items"{unrev(m["status"])}>'
                           + "".join(f"<li>{e(x)}</li>" for x in m["value"]) + "</ul>")
            if t["follow_up"]:
                f = t["follow_up"]
                out.append(f'<h4>Suggested follow-up</h4><p{unrev(f["status"])}>{e(f["value"])}{mark(f["status"])}</p>')
            out.append("</div></details>")

    for s in r["custom_and_other_sections"]:
        out.append(f'<h2>{e(s["title"])}</h2><ul class="items">')
        out += [f'<li{unrev(i["status"])}><b>{e(i["label"])}:</b> {e(i["value"])}{mark(i["status"])}'
                f'{quote_list(i["evidence"])}{basis(i)}</li>' for i in s["items"]]
        out.append("</ul>")
    if oa := r["overall_assessment"]:
        out.append(f'<section class="overall"><h2>{e(oa["title"])} <span class="mark">{e(oa["label"])}</span></h2>'
                   '<ul class="items">')
        out += [f'<li{unrev(i["status"])}><b>{e(i["label"])}:</b> {e(i["value"])}{mark(i["status"])}'
                f'{quote_list(i["evidence"])}{basis(i)}</li>' for i in oa["items"]]
        out.append("</ul></section>")

    if r["notes"] or r["flags"] or r["consent"]:
        out.append("<h2>Notes, flags and consent</h2>")
        if r["notes"]:
            out.append(f'<h4>Notes by the {e(user)}</h4><ul class="items">'
                       + "".join(f'<li><span class="who">{clock(n["t_ms"])}</span> {e(n["text"])}</li>' for n in r["notes"])
                       + "</ul>")
        if r["flags"]:
            out.append('<h4>Flags raised during the interview</h4><ul class="items">'
                       + "".join(f'<li class="unrev">{e(f["note"])}{mark("proposed")}{quote_list(f["evidence"])}</li>'
                                 for f in r["flags"]) + "</ul>")
        if c := r["consent"]:
            out.append(f'<h4>Consent</h4><p>{e(c["topic"])}: {STATUS[c["status"]]}. Refusal check: '
                       f'{e(c["refusal_check"])}.</p>')
    if r.get("transcript"):
        out.append('<h2>Transcript</h2><section class="transcript">')
        out += [f'<p id="{e(u["id"])}"><span class="who">{clock(u["t_ms"])} {e(u["speaker_label"])}</span> '
                f'{e(u["text"])}</p>' for u in r["transcript"]]
        out.append("</section>")
    models = " · ".join(f"{k}: {e(v)}" for k, v in r["models"].items())
    out.append(f'<footer><p>Models: {models or "not recorded"}. Generated on {e(r["exported_at"][:10])}.'
               + (f' Model cost for this session: ${r["cost_usd"]:.4f}.' if "cost_usd" in r else "")
               + f'</p><p>{e(TOOL_STATEMENT)}</p></footer>{SCRIPT}</body></html>')
    return "\n".join(out)
