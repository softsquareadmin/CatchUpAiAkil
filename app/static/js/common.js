// Shared by every page (M12): header and navigation, toasts, the confirmation dialog, info icons, status badges,
// the checklist coverage ring, quotes, small DOM helpers. Plain JavaScript, no framework, no network beyond this app.
"use strict";

const $ = (id) => document.getElementById(id);

// el("p", { className: "x" }, "text", node) -> element; strings become text nodes (never HTML).
function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("aria-") || k === "role" || k === "for") node.setAttribute(k, v);
    else node[k] = v;
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) node.append(c instanceof Node ? c : String(c));
  return node;
}
function para(text) { return el("p", {}, text); }

// ---- icons: inline SVG, a small fixed set ----
const ICONS = {
  mic: "M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z",
  play: "M8 5v14l11-7z",
  stop: "M7 7h10v10H7z",
  flag: "M5 3h2v18H5zM8 4h10l-2 4 2 4H8z",
  check: "M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z",
  alert: "M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z",
  doc: "M6 2h9l5 5v15H6zM14 3v5h5",
  list: "M4 6h16v2H4zm0 5h16v2H4zm0 5h10v2H4z",
  gear: "M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.3 2.9a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 1L11 21h4l.3-2.9a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5zM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z",
  coins: "M12 3C7 3 3 4.8 3 7v10c0 2.2 4 4 9 4s9-1.8 9-4V7c0-2.2-4-4-9-4zm0 2c4.4 0 7 1.4 7 2s-2.6 2-7 2-7-1.4-7-2 2.6-2 7-2z",
  external: "M14 3h7v7h-2V6.4l-9.3 9.3-1.4-1.4L17.6 5H14zM5 5h6v2H7v10h10v-4h2v6H5z",
  download: "M11 3h2v9.6l3.3-3.3 1.4 1.4L12 16.4l-5.7-5.7 1.4-1.4L11 12.6zM5 19h14v2H5z",
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "currentColor");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name] || "");
  svg.append(path);
  return svg;
}

// ---- header with the pages; `active` is the page's key ----
const PAGES = [["interview", "/", "Interview", "mic"], ["sessions", "/sessions", "Sessions", "list"],
  ["config", "/config", "Configuration", "gear"], ["costs", "/costs", "Costs", "coins"]];
function mountHeader(active, endNodes = []) {
  const bar = $("appbar");
  const nav = el("nav", { className: "nav", "aria-label": "Pages" }, PAGES.map(([key, href, label, ic]) => {
    const a = el("a", { href }, icon(ic), label);
    if (key === active) a.setAttribute("aria-current", "page");
    return a;
  }));
  const brand = el("a", { className: "brand", href: "/" }, el("span", { className: "logo", "aria-hidden": "true" }, icon("mic")),
    "Interview Assistant");
  bar.replaceChildren(brand, nav, el("div", { className: "appbar-end", id: "appbarEnd" }, endNodes));
}

// ---- toasts: polite, never take focus ----
function toast(text, kind = "info", ms = 6000) {
  let box = $("toasts");
  if (!box) {
    box = el("div", { id: "toasts", className: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(box);
  }
  const t = el("div", { className: `toast ${kind}` }, el("span", {}, text));
  const close = el("button", { type: "button", "aria-label": "Close message" }, "×");
  close.addEventListener("click", () => t.remove());
  t.append(close);
  box.append(t);
  if (ms) setTimeout(() => t.remove(), ms);
  return t;
}
function showError(text) { toast(text, "error"); }

// ---- the one confirmation dialog. focusButton: only for dialogs the user opened (system prompts such as the
// consent check never take focus from the interviewer) ----
function showDialog(title, body, yes, no, onYes, onNo, { danger = false, focusButton = false } = {}) {
  let ov = $("dialog");
  if (!ov) {
    ov = el("div", { id: "dialog", className: "overlay", hidden: true },
      el("div", { className: "dialog", role: "alertdialog", "aria-modal": "true", "aria-labelledby": "dialogTitle" },
        el("h2", { id: "dialogTitle" }), el("div", { id: "dialogBody" }),
        el("div", { className: "dialog-buttons" }, el("button", { id: "dialogNo", type: "button" }),
          el("button", { id: "dialogYes", type: "button", className: "btn-primary" }))));
    document.body.append(ov);
  }
  $("dialogTitle").textContent = title;
  $("dialogBody").replaceChildren(...body);
  $("dialogYes").textContent = yes;
  $("dialogYes").className = danger ? "btn-danger" : "btn-primary";
  $("dialogNo").textContent = no;
  const opener = document.activeElement;
  const close = (fn) => { ov.hidden = true; if (focusButton && opener && opener.focus) opener.focus(); fn(); };
  $("dialogYes").onclick = () => close(onYes);
  $("dialogNo").onclick = () => close(onNo);
  ov.onkeydown = (e) => { if (e.key === "Escape") close(onNo); };
  ov.hidden = false;
  if (focusButton) $("dialogNo").focus();
}
const confirmDialog = (title, body, yes, no, opts) =>
  new Promise((resolve) => showDialog(title, body, yes, no, () => resolve(true), () => resolve(false), { focusButton: true, ...opts }));

// ---- info icon: tap or click toggles the help text; hover shows the same text as a tooltip ----
function infoButton(text, helpEl) {
  const b = el("button", { type: "button", className: "info", title: text, "aria-label": "More information",
    "aria-expanded": "false" }, el("span", { "aria-hidden": "true" }, "i"));
  b.addEventListener("click", () => {
    const open = helpEl.hidden;
    helpEl.hidden = !open;
    b.setAttribute("aria-expanded", String(open));
  });
  return b;
}

// ---- status ----
const STATUS_LABEL = { not_covered: "not covered", partial: "partial", covered: "covered" };
const STATUS_TEXT = { not_covered: "Not covered", partial: "Partial", covered: "Covered" };
const STATUS_SYMBOL = { covered: "✓", partial: "◐", not_covered: "○" };
function statusBadge(status, extraClass = "") {
  return el("span", { className: `badge status ${extraClass}`, dataset: { status } },
    el("span", { "aria-hidden": "true" }, STATUS_SYMBOL[status]), STATUS_TEXT[status]);
}

// Gaps first (CatchUp style): not covered, then partial, then covered; a required follow-up goes to the top of its group.
const RANK = { not_covered: 0, partial: 1, covered: 2 };
function gapsFirst(items, topics) {
  const r = (i) => { const t = topics[i.id] || { status: "not_covered" }; return RANK[t.status] * 2 + (t.follow_up === "required" ? 0 : 1); };
  return items.map((i, n) => [i, n]).sort((a, b) => r(a[0]) - r(b[0]) || a[1] - b[1]).map(([i]) => i);
}

// Checklist coverage, the same formula as app/coverage.py (covered 100, partial 50, not covered 0; averaged over
// required topics; the number hidden when the pack sets review.show_coverage_score: false).
function coverageOf(items, topics, showScore = true) {
  const req = items.filter((i) => i.required !== false);
  const counts = { covered: 0, partial: 0, not_covered: 0 };
  for (const i of req) counts[(topics[i.id] || {}).status || "not_covered"]++;
  const score = req.length ? Math.floor((counts.covered * 100 + counts.partial * 50) / req.length + 0.5) : null;
  const opt = items.length - req.length;
  const note = [!req.length ? "No required topics, so there is no coverage score." : "",
    opt ? `${opt} optional topic${opt > 1 ? "s are" : " is"} shown with ${opt > 1 ? "their" : "its"} status but not counted in the score.` : ""].filter(Boolean).join(" ");
  return { label: "Checklist coverage", score: showScore ? score : null, counts, required: req.length, note };
}

function coverageView(cov) {
  const box = el("div", { className: "coverage" });
  if (!cov) return box;
  if (cov.score != null) {
    const ring = el("div", { className: "coverage-ring", role: "img", "aria-label": `Checklist coverage ${cov.score}%` },
      el("div", {}, el("strong", {}, `${cov.score}%`), el("span", {}, "Checklist coverage")));
    ring.style.setProperty("--coverage", `${cov.score}%`);
    box.append(ring);
  }
  box.append(el("div", { className: "coverage-stats" }, [["covered", "Covered"], ["partial", "Partial"], ["not_covered", "Not covered"]]
    .map(([k, label]) => el("div", { className: `coverage-stat ${k}` }, el("b", {}, cov.counts[k]),
      el("span", {}, `${STATUS_SYMBOL[k]} ${label}`)))));
  if (cov.note) box.append(el("p", { className: "coverage-note" }, cov.note));
  return box;
}

function coverageBlock(items, topics, showScore = true) {
  return items.length ? coverageView(coverageOf(items, topics, showScore)) : coverageView(null);
}

// A quote; tapping it jumps to the line (the page passes how).
function quoteButton(ev, onJump) {
  const q = el("button", { type: "button", className: "quote", title: "Show this line in the transcript" },
    el("span", {}, `“${ev.quote}”`), el("small", {}, ev.utterance_id));
  q.addEventListener("click", () => onJump && onJump(ev.utterance_id));
  return q;
}

// ---- data ----
async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new Error((body && (body.detail || body.error)) || `${r.status} ${r.statusText}`);
  return body;
}
const postJSON = (url, data) => fetchJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function clock(ms) {
  const s = Math.floor((ms || 0) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function emptyState(title, text, ...actions) {
  return el("div", { className: "empty-state" }, el("h2", {}, title), el("p", {}, text), actions.length ? el("div", { className: "btn-row", style: "justify-content:center" }, actions) : null);
}
