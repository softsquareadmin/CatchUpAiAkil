// Cost report page (/costs): the ledger report (app/report.py) per experiment and role, with the experiment filter.
// The ledger is an estimate; provider invoices are the source of truth.
"use strict";

mountHeader("costs");

const money = (v) => (v == null ? "" : `$${Number(v).toFixed(4)}`);
const ms = (v) => (v == null ? "" : `${v} ms`);

async function load(experiment = new URLSearchParams(location.search).get("experiment") || "") {
  const main = $("content");
  const head = el("div", { className: "page-head" }, el("h1", {}, "Cost report"));
  main.replaceChildren(head, el("p", { className: "loading" }, "Loading the ledgers"));
  let data;
  try { data = await fetchJSON(`/api/costs${experiment ? `?experiment=${encodeURIComponent(experiment)}` : ""}`); }
  catch (e) { main.replaceChildren(head, emptyState("The cost report could not be loaded", e.message)); return; }
  if (!data.experiments.length) {
    main.replaceChildren(head, emptyState("No model calls yet", "Costs appear here after the first interview that uses a model. Offline models are free and do not appear."));
    return;
  }
  const filter = el("select", { id: "expFilter" }, new Option("All experiments", ""),
    data.experiments.map((x) => new Option(x, x, false, x === experiment)));
  filter.addEventListener("change", () => {
    history.replaceState(null, "", filter.value ? `/costs?experiment=${encodeURIComponent(filter.value)}` : "/costs");
    load(filter.value);
  });
  main.replaceChildren(head,
    el("div", { className: "filters" }, el("label", { className: "inline-field" }, "Experiment", filter),
      el("span", { className: "hint" }, "Estimates from the session ledgers; provider invoices are the source of truth.")),
    section("Per experiment", table(["Experiment", "Sessions", "Minutes", "Cost", "Cost per minute", "Cards shown", "Evidence failures"],
      data.totals.map((t) => [t.experiment, t.sessions, t.interview_minutes, money(t.cost_usd), money(t.cost_per_minute_usd), t.cues_shown, t.evidence_failures]),
      [1, 2, 3, 4, 5, 6])),
    section("Per experiment and role", table(["Experiment", "Role", "Models", "Calls", "Failed", "Cost", "p50", "p95"],
      data.rows.map((r) => [r.experiment, r.role, r.models, r.calls, r.failed, money(r.cost_usd), ms(r.p50_ms), ms(r.p95_ms)]),
      [3, 4, 5, 6, 7])),
    section("Provider health per session", data.health.length ? table(["Session", "Calls", "HTTP 429", "Retries", "Total wait", "Skipped gate runs", "Skipped cards"],
      data.health.map((h) => [h.session, h.calls, h.http_429, h.retries, `${h.total_wait_s} s`, h.gate_runs_skipped, h.cues_skipped]),
      [1, 2, 3, 4, 5, 6]) : el("p", { className: "empty-inline" }, "No sessions with model calls.")));
}

function section(title, body) {
  return el("section", { className: "costs-section" }, el("h2", {}, title), body);
}

function table(headers, rows, numeric = []) {
  return el("div", { className: "table-wrap" }, el("table", { className: "table" },
    el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { scope: "col", className: numeric.includes(i) ? "num" : "" }, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { className: numeric.includes(i) ? "num" : "" }, c == null ? "" : String(c))))))));
}

load();
