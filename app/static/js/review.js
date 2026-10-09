// Review page (/review/<session>): the post-interview review and report (M5, M11). Works for a running session and
// for a past one read from disk; every decision is one POST to /api/sessions/<id>/review.
"use strict";

const SID = decodeURIComponent(location.pathname.split("/").pop());
const API = `/api/sessions/${encodeURIComponent(SID)}`;
const st = { review: null, pack: { roles: [] }, utterances: [], editing: null, openTopics: new Set(), viewed: false, busy: false };
const ITEM_STATUS = { proposed: "Not reviewed", accepted: "✓ Accepted", edited: "✎ Edited", rejected: "✕ Rejected" };
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
  renderTranscript();
  $("title").textContent = `Review · ${data.pack.name}`;
  document.title = `Review · ${data.pack.name} · Interview Assistant`;
  $("subtitle").textContent = `Session ${SID} · ${data.pack.ref} · ${data.utterances.length} lines${data.live ? " · interview still open" : ""}`;
  setReview(data.review);
  if (data.review.state === "running") setTimeout(load, 2000);  // the review model is still writing
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

// ---- topic results (M11): gaps first, each expandable; summary, missing and follow-up are reviewed like items ----
function resultCard(t) {
  const d = el("details", { className: "result-card", dataset: { status: t.status } });
  d.open = t.status !== "covered" || st.openTopics.has(t.topic_id);
  d.addEventListener("toggle", () => st.openTopics[d.open ? "add" : "delete"](t.topic_id));
  const sel = el("select", { "aria-label": `Status of ${t.label}` });
  for (const k of ["not_covered", "partial", "covered"]) sel.add(new Option(STATUS_TEXT[k], k, false, t.status === k));
  sel.addEventListener("click", (e) => e.preventDefault());
  sel.addEventListener("change", () => decide({ kind: "status", topic_id: t.topic_id, status: sel.value }));
  const sum = el("summary", {}, statusBadge(t.status), el("strong", {}, t.label + (t.required ? "" : " (optional)")), sel);
  if (t.status_by === "user") sum.append(el("span", { className: "badge badge-accent" }, `set by ${roleLabel(st.pack.user_role).toLowerCase()}`));
  d.append(sum);
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
  d.append(body);
  return d;
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
    el("div", { className: "rlabel" }, el("span", {}, title), el("span", { className: `badge ${ITEM_BADGE[p.status]}` }, ITEM_STATUS[p.status])));
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

function reviewItem(it) {
  const label = el("span", {}, it.label);
  if (it.section === "overall_assessment") label.append(" ", el("span", { className: "badge badge-warn" }, "Draft for human review"));
  const box = el("div", { className: "ritem", dataset: { status: it.status } },
    el("div", { className: "rlabel" }, label, el("span", { className: `badge ${ITEM_BADGE[it.status]}` }, ITEM_STATUS[it.status])));
  const act = (action, value) => decide({ kind: "item", id: it.id, action, value });
  if (st.editing === it.id) box.append(...editor(it.id, it.value, (v) => act("edit", v), it.label));
  else box.append(el("p", { className: "rvalue" }, it.value));
  box.append(...it.evidence.map((ev) => quoteButton(ev, jumpTo)));
  if ((it.basis_topics || []).length) {  // an item about what was not said: grounded in checklist statuses
    box.append(el("p", { className: "basis" }, `Based on the checklist: ${it.basis_topics.map((x) => `${x.label} (${STATUS_LABEL[x.status]})`).join("; ")}`));
  }
  if (st.editing !== it.id) box.append(actionsFor(it.status, it.id, act));
  return box;
}

function section(title, ...parts) {
  return el("section", { className: "review-section" }, el("h2", {}, title), parts);
}

function renderReview() {
  const body = $("reviewBody");
  const r = st.review;
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
  const d = r.draft;
  const out = [];
  const topics = d.topics || [];
  if (topics.length) {
    out.push(section((d.coverage || {}).label || "Checklist coverage", coverageView(d.coverage),
      el("p", { className: "hint", style: "margin-top:8px" }, "How much of the checklist the conversation covered. It is not a rating of anyone."),
      gapsFirst(topics.map((t) => ({ ...t, id: t.topic_id })), Object.fromEntries(topics.map((t) => [t.topic_id, t]))).map(resultCard)));
  }
  const order = [...(d.sections || [])];
  const oa = order.findIndex((s) => s.id === "overall_assessment");
  if (oa > 0) order.unshift(...order.splice(oa, 1));  // the draft assessment sits right under the topic cards
  for (const { id: sec, title } of order) {
    const items = d.items.filter((i) => i.section === sec);
    if (!items.length) continue;
    if (sec === "accounts") {  // one row per topic, sources next to each other, never ranked
      const parts = [...new Set(items.map((i) => i.topic_id))].map((topic) => [
        el("h3", { style: "margin:12px 0 8px" }, items.find((i) => i.topic_id === topic).label.split(" · ")[0]),
        el("div", { className: "side-by-side" }, items.filter((i) => i.topic_id === topic).map(reviewItem))]);
      out.push(section(title, parts));
    } else out.push(section(title, items.map(reviewItem)));
  }
  const cl = d.checklist || {};
  const follow = (cl.follow_ups || []);
  const open = topics.length ? [] : (cl.open_topics || []);  // topic cards already show open topics
  if (("follow_ups" in cl && follow.length) || open.length) {
    const ul = el("ul", {}, open.map((t) => el("li", {}, `${t.label}: ${STATUS_LABEL[t.status]}${t.follow_up === "required" ? ", follow-up required" : ""}`)),
      follow.filter((f) => !open.some((t) => t.id === f.id)).map((f) => el("li", {}, `${f.label}: follow-up ${f.follow_up}${f.reason ? ` (${f.reason})` : ""}`)));
    out.push(section("From the checklist (not AI-written)", ul));
  }
  if (d.rejected) out.push(el("p", { className: "hint", style: "margin-top:16px" }, `${d.rejected} AI item(s) were left out because their quotes did not check out or they stated a conclusion.`));
  body.replaceChildren(...out);
}

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
