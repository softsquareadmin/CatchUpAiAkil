// Sessions page (/sessions): past interviews with links to the review, the HTML report and the JSON.
"use strict";

mountHeader("sessions");

const STATUS = { running: ["● Running", "badge-accent"], reviewed: ["✓ Reviewed", "badge-ok"], "review ready": ["Review ready", "badge-warn"],
  "review failed": ["✕ Review failed", "badge-danger"], ended: ["Ended, no review", "badge-neutral"] };

async function load() {
  const main = $("content");
  main.replaceChildren(el("div", { className: "page-head" }, el("h1", {}, "Sessions")), el("p", { className: "loading" }, "Loading sessions"));
  let rows;
  try { rows = await fetchJSON("/api/sessions"); }
  catch (e) {
    main.replaceChildren(el("div", { className: "page-head" }, el("h1", {}, "Sessions")),
      emptyState("Sessions could not be loaded", e.message, retryButton()));
    return;
  }
  const head = el("div", { className: "page-head" }, el("h1", {}, "Sessions"), el("a", { className: "btn btn-primary", href: "/" }, icon("mic"), "New interview"));
  if (!rows.length) {
    main.replaceChildren(head, emptyState("No sessions yet", "Interviews appear here once they start. Run one on the Interview page, or replay the sample script.",
      el("a", { className: "btn btn-primary", href: "/" }, "Go to the Interview page")));
    return;
  }
  const packs = [...new Set(rows.map((r) => r.pack))].sort();
  const filter = el("select", { id: "packFilter" }, new Option("All interview types", ""), packs.map((p) => new Option(p, p)));
  const tbody = el("tbody");
  const render = () => {
    const shown = rows.filter((r) => !filter.value || r.pack === filter.value);
    tbody.replaceChildren(...shown.map(rowFor));
    count.textContent = `${shown.length} of ${rows.length}`;
  };
  const count = el("span", { className: "hint" });
  filter.addEventListener("change", render);
  const table = el("div", { className: "table-wrap" }, el("table", { className: "table" },
    el("thead", {}, el("tr", {}, ["Started", "Interview type", "Duration", "Lines", "Status", "Cost", "Open"].map((h, i) =>
      el("th", { scope: "col", className: [2, 3, 5].includes(i) ? "num" : "" }, h)))), tbody));
  main.replaceChildren(head, el("div", { className: "filters" }, el("label", { className: "inline-field" }, "Show", filter), count), table);
  render();
}

function rowFor(r) {
  const [text, cls] = STATUS[r.status] || [r.status, "badge-neutral"];
  const api = `/api/sessions/${encodeURIComponent(r.id)}`;
  const links = el("div", { className: "links" });
  if (r.has_review || r.status === "running" || r.status === "review failed") {
    links.append(el("a", { className: "btn btn-primary", href: `/review/${encodeURIComponent(r.id)}` }, "Review"));
  }
  if (r.has_review) {
    links.append(el("a", { className: "btn", href: `${api}/export.html?view=true`, target: "_blank", rel: "noopener" }, icon("external"), "Report"),
      el("a", { className: "btn", href: `${api}/export.json`, download: "" }, icon("download"), "JSON"));
  }
  if (!links.children.length) links.append(el("span", { className: "hint" }, "No review"));
  return el("tr", {},
    el("td", {}, el("div", {}, fmtDate(r.started_at)), el("small", { className: "muted" }, r.id)),
    el("td", {}, r.pack || "", r.experiment && r.experiment !== "default" ? el("div", {}, el("small", { className: "muted" }, `experiment: ${r.experiment}`)) : null),
    el("td", { className: "num" }, clock(r.duration_s * 1000)),
    el("td", { className: "num" }, r.lines),
    el("td", {}, el("span", { className: `badge ${cls}` }, text), r.exported ? el("div", {}, el("small", { className: "muted" }, "exported")) : null),
    el("td", { className: "num" }, `$${r.cost_usd.toFixed(4)}`),
    el("td", {}, links));
}

function retryButton() {
  const b = el("button", { type: "button" }, "Try again");
  b.addEventListener("click", load);
  return b;
}

load();
