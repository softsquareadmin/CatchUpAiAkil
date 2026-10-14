// Review page (/review/<session>): the post-interview review and report (M5, M11). Works for a running session and
// for a past one read from disk; every decision is one POST to /api/sessions/<id>/review.
"use strict";

const SID = decodeURIComponent(location.pathname.split("/").pop());
const API = `/api/sessions/${encodeURIComponent(SID)}`;
const st = { review: null, pack: { roles: [] }, utterances: [], editing: null, open: new Map(), main: new Set(), x: {}, viewed: false, busy: false };
const ITEM_STATUS = { proposed: "", accepted: "✓ Accepted", edited: "✎ Edited", rejected: "✕ Rejected" };
const ITEM_BADGE = { proposed: "badge-neutral", accepted: "badge-ok", edited: "badge-accent", rejected: "badge-danger" };

mountHeader("sessions");

const roleLabel = (id) => (st.pack.roles.find((r) => r.id === id) || { label: "Unknown" }).label;

async function load() {
  let data;
  try { data = await fetchJSON(API + "/review"); }
  catch (e) {
    $("reviewBody").replaceChildren(emptyState("Session not found", `There is no session ${SID} on this server.`,
      el("a", { className: "btn btn-primary", href: "/sessions" }, "All sessions")));
    $("reviewState").hidden = true;
    return;
  }
  st.pack = data.pack;
  st.utterances = data.utterances;
  setExtras(data);
  renderTranscript();
  $("title").textContent = `Review · ${data.pack.name}`;
  document.title = `Review · ${data.pack.name} · Interview Assistant`;
  $("subtitle").textContent = `Session ${SID} · ${data.pack.ref} · ${data.utterances.length} lines${data.live ? " · interview still open" : ""}`;
  setReview(data.review);
  if (data.review.state === "running") setTimeout(load, 2000);  // the review model is still writing
}

function setExtras(data) {  // what the page needs beyond the draft to match the report
  st.x = { layout: data.layout, flags: data.flags, notes: data.notes, consent: data.consent,
    follow_up_levels: data.follow_up_levels, missing_prefix: data.missing_prefix, not_stated: data.not_stated };
}

function renderTranscript() {
  const list = $("transcript");
  if (!st.utterances.length) { list.replaceChildren(el("li", { className: "empty-inline" }, "No transcript.")); return; }
  list.replaceChildren(...st.utterances.map((u) => {
    const idx = st.pack.roles.findIndex((r) => r.id === u.speaker);
    return el("li", { dataset: { id: u.id } },
      el("span", { className: `who k-${idx < 0 ? "unknown" : st.pack.roles[idx].kind} r${idx}` }, roleLabel(u.speaker)),
      el("span", {}, el("small", { className: "muted" }, `${clock(u.t_start_ms)} · ${u.id}`), el("br"), u.text));
  }));
}

function jumpTo(id) {
  const li = $("transcript").querySelector(`li[data-id="${CSS.escape(id)}"]`);
  if (!li) return;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  li.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  li.classList.remove("flash");
  void li.offsetWidth;
  li.classList.add("flash");
}

function setReview(r) {
  st.review = r;
  const badge = $("reviewState");
  const [text, cls] = { running: ["Writing the review…", "badge-accent loading"], ready: ["✓ Ready", "badge-ok"],
    failed: ["✕ Review failed", "badge-danger"], none: ["No review", "badge-neutral"] }[r.state];
  badge.textContent = text;
  badge.className = `badge ${cls}`;
  badge.title = r.error || r.model || "";
  $("exportBox").hidden = r.state !== "ready";
  exportLinks();
  $("reviewNotice").textContent = (r.draft && r.draft.notice) || "";
  $("reviewNotice").hidden = !(r.draft && r.draft.notice);
  if (r.state === "ready" && !st.viewed) { st.viewed = true; postJSON(API + "/review", { kind: "viewed" }).catch(() => {}); }
  renderReview();
}

async function decide(body) {
  if (st.busy) return;
  st.busy = true;
  try {
    const out = await postJSON(API + "/review", body);
    st.editing = null;
    if (out.layout) setExtras(out);  // consent and follow-up levels follow the decision
    setReview(out.review);
  } catch (e) { showError(`Not saved: ${e.message}`); }
  finally { st.busy = false; }
}

// Export options (M11): unreviewed items, transcript appendix and cost are off unless ticked; the name is optional.
function exportLinks() {
  const q = new URLSearchParams();
  if ($("optUnreviewed").checked) q.set("unreviewed", "true");
  if ($("optTranscript").checked) q.set("transcript", "true");
  if ($("optCost").checked) q.set("cost", "true");
  if ($("optReviewer").value.trim()) q.set("reviewer", $("optReviewer").value.trim());
  const qs = q.toString();
  $("exportJson").href = `${API}/export.json${qs ? `?${qs}` : ""}`;
  $("exportHtml").href = `${API}/export.html${qs ? `?${qs}` : ""}`;
  q.set("view", "true");
  $("openReport").href = `${API}/export.html?${q}`;
}
for (const id of ["optUnreviewed", "optTranscript", "optCost", "optReviewer"]) $(id).addEventListener("input", exportLinks);

// ---- layout (2026-10-14): the same order and sections as the exported report (app/export.py render_html), with the
// review actions on every item. Every section is a <details> whose open state survives the re-render after a decision.
const NOUNS = { summary: "statements", timeline: "events", next_steps: "options" };  // count shown on a closed section
const NO_LEAD_IN = ["next_steps"];  // sections whose item label ("Option") only repeats the heading
const level = (topicId) => (st.x.follow_up_levels || {})[topicId] || "none";

function fold(id, cls, summary, body, openByDefault) {
  const d = el("details", { className: cls, id });
  d.open = st.open.has(id) ? st.open.get(id) : openByDefault;
  d.addEventListener("toggle", () => st.open.set(id, d.open));
  d.append(el("summary", {}, summary), ...[body].flat());
  return d;
}

function sec(id, title, body, openByDefault, count = null, noun = "items") {
  return fold(`sec-${id}`, "review-section", [el("h2", {}, title),
    count == null ? null : el("span", { className: "count" }, ` · ${count} ${noun}`)], el("div", { className: "secbody" }, body),
  openByDefault);
}

// open the sections around a target and bring it into view
function openTo(id) {
  const n = $(id);
  if (!n) return;
  for (const d of document.querySelectorAll("#reviewBody details")) if (d.contains(n)) { d.open = true; st.open.set(d.id, true); }
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  n.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
}

// Each full quote once, in its main place (lead sections, then topics); elsewhere a short link to the line.
function refs(evidence) {
  return evidence.map((ev) => {
    if (!st.main.has(ev.utterance_id)) return quoteButton(ev, jumpTo);
    const u = st.utterances.find((x) => x.id === ev.utterance_id);
    const b = el("button", { type: "button", className: "ref", title: `${u ? roleLabel(u.speaker) : ""}: ${ev.quote}` },
      u ? `${clock(u.t_start_ms)} · ${ev.utterance_id}` : ev.utterance_id);
    b.addEventListener("click", () => jumpTo(ev.utterance_id));
    return b;
  });
}

function statusTag(status) {  // only what the user did is marked; an unreviewed item has a dashed outline instead
  return status === "proposed" ? null : el("span", { className: `badge ${ITEM_BADGE[status]}` }, ITEM_STATUS[status]);
}

function actionsFor(status, key, act, rejectText = "Reject") {
  const actions = el("div", { className: "actions" });
  const btn = (text, fn, cls = "") => { const b = el("button", { type: "button", className: cls }, text); b.addEventListener("click", fn); actions.append(b); };
  if (status !== "accepted" && status !== "edited") btn("Accept", () => act("accept"), "btn-primary");
  btn("Edit", () => { st.editing = key; renderReview(); });
  if (status !== "rejected") btn(rejectText, () => act("reject"));
  if (status !== "proposed") btn("Undo", () => act("reset"), "btn-quiet");
  return actions;
}

function editor(key, text, onSave, label) {
  const ta = el("textarea", { "aria-label": label });
  ta.value = text;
  const actions = el("div", { className: "actions" });
  const save = el("button", { type: "button", className: "btn-primary" }, "Save");
  save.addEventListener("click", () => onSave(ta.value));
  const cancel = el("button", { type: "button" }, "Cancel");
  cancel.addEventListener("click", () => { st.editing = null; renderReview(); });
  actions.append(save, cancel);
  setTimeout(() => ta.focus(), 0);  // the person pressed Edit
  return [ta, actions];
}

function resultPart(t, part, title, p) {
  const key = `${t.topic_id}.${part}`;
  const box = el("div", { className: "ritem", dataset: { status: p.status } },
    el("div", { className: "rlabel" }, el("span", {}, title), statusTag(p.status)));
  const act = (action, value) => decide({ kind: "topic", topic_id: t.topic_id, part, action, value });
  if (st.editing === key) {
    box.append(...editor(key, Array.isArray(p.value) ? p.value.join("\n") : p.value, (v) => act("edit", v),
      part === "missing" ? "Still missing, one item per line" : title));
  } else {
    box.append(Array.isArray(p.value) ? el("ul", { className: "rvalue" }, p.value.map((x) => el("li", {}, x)))
      : el("p", { className: "rvalue" }, p.value));
    box.append(actionsFor(p.status, key, act, part === "follow_up" ? "Remove" : "Reject"));
  }
  return box;
}

// opts.lead: the label shown above the value ("" for none); opts.full: full quotes (main place) or links
function reviewItem(it, { lead = it.label, full = true } = {}) {
  const label = el("span", {}, lead);
  if (it.section === "overall_assessment") label.append(" ", el("span", { className: "badge badge-warn" }, "Draft for human review"));
  const box = el("div", { className: "ritem", id: `item-${it.id}`, dataset: { status: it.status } });
  if (lead || it.status !== "proposed" || it.section === "overall_assessment") box.append(el("div", { className: "rlabel" }, label, statusTag(it.status)));
  const act = (action, value) => decide({ kind: "item", id: it.id, action, value });
  if (st.editing === it.id) box.append(...editor(it.id, it.value, (v) => act("edit", v), it.label));
  else box.append(el("p", { className: "rvalue" }, it.value));
  box.append(...(full ? it.evidence.map((ev) => quoteButton(ev, jumpTo)) : refs(it.evidence)));
  if ((it.basis_topics || []).length) {  // an item about what was not said: grounded in checklist statuses
    box.append(el("p", { className: "basis" }, `Based on the checklist: ${it.basis_topics.map((x) => `${x.label} (${STATUS_LABEL[x.status]})`).join("; ")}`));
  }
  if (st.editing !== it.id) box.append(actionsFor(it.status, it.id, act));
  return box;
}

const leadIn = (it, sid, title) => (it.label === title || NO_LEAD_IN.includes(sid) ? "" : it.label);

// topic: one row (status, title, required badge, suggested follow-up); opening it shows summary, quotes, still missing
function resultCard(t) {
  const sel = el("select", { "aria-label": `Status of ${t.label}` });
  for (const k of ["not_covered", "partial", "covered"]) sel.add(new Option(STATUS_TEXT[k], k, false, t.status === k));
  sel.addEventListener("click", (e) => e.preventDefault());
  sel.addEventListener("change", () => decide({ kind: "status", topic_id: t.topic_id, status: sel.value }));
  const fu = t.follow_up && t.follow_up.status !== "rejected" ? t.follow_up : null;
  const summary = [statusBadge(t.status), el("strong", {}, t.label + (t.required ? "" : " (optional)")),
    level(t.topic_id) === "required" ? el("span", { className: "badge badge-danger" }, "Required follow-up") : null,
    t.status_by === "user" ? el("span", { className: "badge badge-accent" }, `set by ${roleLabel(st.pack.user_role).toLowerCase()}`) : null,
    sel, fu ? el("p", { className: "fq", dataset: { status: fu.status } }, el("b", {}, "Suggested follow-up: "), fu.value) : null];
  const body = el("div", { className: "topic-body" });
  if (t.status_by === "review") body.append(para(`The review changed this from ${STATUS_LABEL[t.live_status]} to ${STATUS_LABEL[t.review_status]}: ${t.status_reason}`));
  if (t.status_by === "user") body.append(para(`You set this status; the AI status was ${STATUS_LABEL[t.review_status || t.live_status]}.`));
  for (const [part, title] of [["summary", "Summary"], ["missing", "Still missing"], ["follow_up", "Suggested follow-up"]]) {
    const p = t[part];
    if (!p) {
      if (part === "summary") body.append(el("p", { className: "muted" }, t.status === "not_covered" && !t.evidence.length ? "Not discussed." : "No summary."));
      continue;
    }
    body.append(resultPart(t, part, title, p));
    if (part === "summary" && t.evidence.length) body.append(...t.evidence.map((ev) => quoteButton(ev, jumpTo)));
  }
  if (!t.summary && t.evidence.length) body.append(...t.evidence.map((ev) => quoteButton(ev, jumpTo)));
  const d = fold(`topic-${t.topic_id}`, "result-card", summary, body, false);
  d.dataset.status = t.status;
  return d;
}

function topicsView(topics) {
  const rank = (t) => RANK[t.status];
  const sorted = topics.map((t, n) => [t, n]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([t]) => t);
  const need = sorted.filter((t) => t.status !== "covered").map((t, n) => [t, n])  // required follow-ups first
    .sort((a, b) => (level(a[0].topic_id) !== "required") - (level(b[0].topic_id) !== "required") || a[1] - b[1]).map(([t]) => t);
  const done = sorted.filter((t) => t.status === "covered");
  const groups = [["need", "Needs follow-up", need, true], ["done", "Covered", done, false]].filter(([, , g]) => g.length)
    .map(([id, title, g, open]) => fold(`group-${id}`, "group", el("h3", {}, `${title} (${g.length})`), g.map(resultCard), open));
  return el("section", { className: "topics", id: "topics" }, el("h2", {}, "Topics"), groups);
}

function accountsView(accounts, missing) {  // a table: one row per topic (or account group), one column per source
  const lay = st.x.layout, items = accounts.items;
  let cols = lay.account_columns.filter((c) => items.some((i) => i.source === c.id));
  for (const i of items) if (i.source && !cols.some((c) => c.id === i.source)) cols.push({ id: i.source, label: i.source });
  const prefix = st.x.missing_prefix;
  const unheard = (missing ? missing.items : []).map((i) => [i, i.label.startsWith(prefix) ? i.label.slice(prefix.length) : i.label]);
  const rows = new Map();
  for (const i of items) {
    const t = (st.review.draft.topics || []).find((x) => x.topic_id === i.topic_id);
    const g = lay.account_groups[i.topic_id] || (t && t.label) || i.label;
    if (!rows.has(g)) rows.set(g, []);
    rows.get(g).push(i);
  }
  const table = el("table", { className: "accounts" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Topic"), cols.map((c) => el("th", { scope: "col" }, c.label)),
      unheard.map(([, n]) => el("th", { scope: "col", className: "unh" }, n)))),
    el("tbody", {}, [...rows].map(([g, its]) => el("tr", {}, el("th", { scope: "row" }, g),
      cols.map((c) => {
        const here = its.filter((i) => i.source === c.id);
        return el("td", { dataset: { label: c.label } }, here.length ? here.map((i) => reviewItem(i, { lead: "", full: false }))
          : el("span", { className: "empty" }, "Nothing said"));
      }),
      unheard.map(([, n]) => el("td", { className: "unh", dataset: { label: n } }, el("span", { className: "empty" }, "Not yet heard")))))));
  return sec(accounts.id, accounts.title, [el("div", { className: "table-wrap" }, table),
    unheard.map(([i, n]) => reviewItem(i, { lead: `${n}: not yet heard`, full: false }))], true);
}

function renderReview() {
  const body = $("reviewBody");
  const r = st.review;
  $("reviewTools").hidden = !(r && r.state === "ready");
  if (!r || r.state === "none") {
    body.replaceChildren(emptyState("No review yet", "The review is written when the interview is ended. End the interview on the Interview page.",
      el("a", { className: "btn", href: "/" }, "Interview page")));
    return;
  }
  if (r.state === "running") { body.replaceChildren(emptyState("Writing the review…", "The review model is reading the whole interview. This can take up to a minute; the page updates by itself.")); return; }
  if (r.state === "failed") {
    body.replaceChildren(emptyState("The review failed", r.error || "Unknown error. The transcript is kept; see the audit trail.",
      el("a", { className: "btn", href: "/sessions" }, "All sessions")));
    return;
  }
  const d = r.draft, lay = st.x.layout, user = roleLabel(st.pack.user_role).toLowerCase();
  const topics = d.topics || [];
  const secs = new Map();
  for (const s of d.sections || []) {
    if (s.id === "form") continue;  // older drafts: form fields are no longer shown (2026-10-14)
    secs.set(s.id, { ...s, items: d.items.filter((i) => i.section === s.id) });
  }
  for (const [id, s] of secs) if (!s.items.length) secs.delete(id);
  const flags = st.x.flags || [];
  const lead = lay.lead.filter((sid) => (secs.has(sid) || (sid === lay.flags_into && flags.length)) && !["accounts", "missing_accounts"].includes(sid));

  // main places of the quotes, and the live flags that add something to the section they are folded into
  st.main = new Set();
  const folded = [];
  for (const sid of lead) {
    const its = secs.has(sid) ? secs.get(sid).items : [];
    for (const i of its) for (const q of i.evidence) st.main.add(q.utterance_id);
    if (sid === lay.flags_into) {
      const listed = new Set(its.flatMap((i) => i.evidence.map((q) => q.utterance_id)));
      for (const f of flags) if (!f.evidence.every((q) => listed.has(q.utterance_id))) {
        folded.push(f);
        for (const q of f.evidence) st.main.add(q.utterance_id);
      }
    }
  }
  for (const t of topics) for (const q of t.evidence) st.main.add(q.utterance_id);

  const out = [];
  let total = 0, done = 0;  // what the person can accept, edit or reject (not the live flags)
  for (const t of topics) for (const part of ["summary", "missing", "follow_up"]) if (t[part]) { total++; done += t[part].status !== "proposed"; }
  for (const s of secs.values()) for (const i of s.items) { total++; done += i.status !== "proposed"; }
  out.push(el("p", { className: "hint review-count" }, `${done} of ${total} items reviewed.`));
  if (lay.speaker_note) out.push(el("p", { className: "hint" }, `Speaker labels come from automatic transcription and can be wrong; the ${user} corrects them.`));

  const accounts = secs.get("accounts"), missing = secs.get("missing_accounts");
  if (topics.length) {
    const cov = d.coverage || {};
    out.push(el("section", { className: "review-cov" }, el("h2", {}, cov.label || "Checklist coverage"), coverageView(d.coverage),
      el("p", { className: "hint", style: "margin-top:8px" }, "How much of the checklist the conversation covered. It is not a rating of anyone.")));
  }
  const jumps = lead.slice(0, 1).map((sid) => [`sec-${sid}`, secs.has(sid) ? secs.get(sid).title : sid]);
  if (topics.length) jumps.push(["topics", "Topics"]);
  if (accounts || missing) jumps.push([accounts ? "sec-accounts" : "sec-missing_accounts", "Accounts"]);
  if (secs.has("next_steps")) jumps.push(["sec-next_steps", "Next steps"]);
  jumps.push(["transcript", "Transcript"]);
  out.push(el("ul", { className: "review-jump" }, jumps.map(([id, text]) => {
    const a = el("a", { href: `#${id}` }, text);
    a.addEventListener("click", (e) => { e.preventDefault(); openTo(id); });
    return el("li", {}, a);
  })));

  for (const sid of lead) {  // right after the coverage, open; the flags raised live are folded into one of them
    const s = secs.get(sid) || { id: sid, title: sid, items: [] };
    const parts = s.items.map((i) => reviewItem(i, { lead: leadIn(i, sid, s.title) }));
    if (sid === lay.flags_into) {
      parts.push(...folded.map((f) => el("div", { className: "ritem live-flag" },
        el("div", { className: "rlabel" }, el("span", {}, "Raised during the interview")), el("p", { className: "rvalue" }, f.note),
        f.evidence.map((ev) => quoteButton(ev, jumpTo)))));
    }
    out.push(sec(sid, s.title, parts, true));
  }

  if (topics.length) out.push(topicsView(topics));

  const rest = [...secs.values()].filter((s) => !lead.includes(s.id) && !["accounts", "missing_accounts", "overall_assessment"].includes(s.id));
  if (accounts) out.push(accountsView(accounts, missing));
  else if (missing) rest.unshift(missing);

  const byId = Object.fromEntries(topics.map((t) => [t.topic_id, t]));
  for (const s of rest) {
    const sid = s.id;
    if (sid === "next_steps") {  // an option that only repeats a topic's suggested follow-up: a link to the topic
      const keep = [], see = [];
      for (const i of s.items) {
        const b = i.basis_topics || [];
        const t = b.length === 1 ? byId[b[0].id] : null;
        const tq = new Set(t ? t.evidence.map((q) => q.utterance_id) : []);
        if (t && t.follow_up && level(t.topic_id) !== "required" && i.status === "proposed" && i.evidence.every((q) => tq.has(q.utterance_id))) see.push([i, t]);
        else keep.push(i);
      }
      const parts = keep.map((i) => reviewItem(i, { lead: "", full: false }));
      if (see.length) {
        const seen = [...new Map(see.map(([, t]) => [t.topic_id, t])).values()];
        parts.push(el("p", { className: "see" }, "Also see the suggested follow-ups for: ", seen.map((t, n) => {
          const a = el("a", { href: `#topic-${t.topic_id}` }, t.label);
          a.addEventListener("click", (e) => { e.preventDefault(); openTo(`topic-${t.topic_id}`); });
          return [n ? ", " : "", a];
        }), "."));
        parts.push(fold("repeats-next_steps", "group", el("span", {}, `Options that repeat a topic's follow-up (${see.length})`),
          see.map(([i]) => reviewItem(i, { lead: "", full: false })), false));
      }
      out.push(sec(sid, s.title, parts, keep.length <= 4, keep.length, NOUNS[sid]));
    } else if (NOUNS[sid]) {
      const parts = s.items.map((i) => reviewItem(i, { lead: leadIn(i, sid, s.title), full: false }));
      const when = (l) => (l.includes(" · ") ? l.slice(0, l.lastIndexOf(" · ")) : l).toLowerCase();  // "<when> · <source>"
      if (sid === "timeline" && s.items.filter((i) => !when(i.label).includes(st.x.not_stated)).length < 2) {
        parts.unshift(el("p", { className: "hint" }, "Left out of the report: fewer than 2 events have a stated date."));
      }
      out.push(sec(sid, s.title, parts, false, s.items.length, NOUNS[sid]));
    } else out.push(sec(sid, s.title, s.items.map((i) => reviewItem(i, { lead: leadIn(i, sid, s.title) })), true));
  }
  if (secs.has("overall_assessment")) {
    const s = secs.get("overall_assessment");
    const box = sec("overall_assessment", s.title, s.items.map((i) => reviewItem(i, { lead: leadIn(i, s.id, s.title) })), true);
    box.classList.add("overall");
    out.push(box);
  }

  const cl = d.checklist || {};
  const follow = (cl.follow_ups || []);
  const open = topics.length ? [] : (cl.open_topics || []);  // the topic rows already show open topics
  if (("follow_ups" in cl && follow.length) || open.length) {
    const more = follow.filter((f) => !open.some((t) => t.id === f.id));
    const ul = el("ul", {}, open.map((t) => el("li", {}, `${t.label}: ${STATUS_LABEL[t.status]}${t.follow_up === "required" ? ", follow-up required" : ""}`)),
      more.map((f) => el("li", {}, `${f.label}: follow-up ${f.follow_up}${f.reason ? ` (${f.reason})` : ""}`)));
    // closed (owner, 2026-10-14): not in the report, and it partly repeats the topic rows
    out.push(sec("checklist", "From the checklist (not AI-written)", ul, false, open.length + more.length));
  }

  const loose = lay.flags_into ? [] : flags, notes = st.x.notes || [], c = st.x.consent;
  if (notes.length || loose.length || c) {
    const parts = [];
    if (notes.length) parts.push(el("h3", {}, `Notes by the ${user}`), el("ul", {}, notes.map((n) => el("li", {}, `${clock(n.t_ms)} ${n.text}`))));
    if (loose.length) parts.push(el("h3", {}, "Flags raised during the interview"), loose.map((f) => el("div", { className: "ritem live-flag" },
      el("p", { className: "rvalue" }, f.note), f.evidence.map((ev) => quoteButton(ev, jumpTo)))));
    if (c) {
      const who = c.recorded == null ? "" : ` Recorded by the ${user}: ${c.recorded.join(", ") || "nobody"}.`;
      parts.push(el("h3", {}, "Consent"), para(`${c.topic}: ${STATUS_TEXT[c.status]}.${who} Refusal check: ${c.refusal_check}.`));
    }
    out.push(sec("notes", `Notes${loose.length ? ", flags" : ""} and consent`, parts, true));
  }
  if (d.rejected) out.push(el("p", { className: "hint", style: "margin-top:16px" }, `${d.rejected} AI item(s) were left out because their quotes did not check out or they stated a conclusion.`));
  body.replaceChildren(...out);
  expandLabel();
}

// Expand all / Collapse all: every section, group and topic on the page
function expandLabel() {
  const all = [...document.querySelectorAll("#reviewBody details")];
  $("expandAll").textContent = all.some((d) => !d.open) ? "Expand all" : "Collapse all";
}
$("expandAll").addEventListener("click", () => {
  const all = [...document.querySelectorAll("#reviewBody details")];
  const open = all.some((d) => !d.open);
  for (const d of all) { d.open = open; st.open.set(d.id, open); }
  expandLabel();
});
$("reviewBody").addEventListener("toggle", expandLabel, true);
window.addEventListener("beforeprint", () => { for (const d of document.querySelectorAll("#reviewBody details")) d.open = true; });

$("auditBtn").addEventListener("click", async () => {
  const list = $("auditList");
  if (!list.hidden) { list.hidden = true; $("auditBtn").setAttribute("aria-expanded", "false"); return; }
  try {
    const rows = await fetchJSON(`${API}/audit`);
    list.replaceChildren(...rows.map((r) => el("li", {}, `${r.ts.slice(11, 19)} ${r.actor}: ${r.action}${r.details && r.details.item_id ? ` (${r.details.item_id})` : ""}`)));
    list.hidden = false;
    $("auditBtn").setAttribute("aria-expanded", "true");
  } catch (e) { showError(`Audit trail not available: ${e.message}`); }
});

exportLinks();
load();
