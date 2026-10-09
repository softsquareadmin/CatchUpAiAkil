// Configuration page (/config, M9): tier 1 fields for a manager; advanced settings read-only; save = a new version.
// Works without an interview and without API keys; "Test this template" runs offline unless real models are chosen.
"use strict";

mountHeader("config");

const cfg = { file: null, base: null, edit: null, editing: false, problems: [], timer: null };
const HELP = {
  name: "The name people see when they choose an interview type.",
  purpose: "One short paragraph: what this interview is for. The assistant reads it on every turn.",
  focus: "Optional. Things to listen out for, one per line. This steers what the assistant notices; it does not raise anything by itself.",
  flags: "Optional. Each line is a situation that must raise a flag card during the interview, with the line it came from. Flags are notes for you, not decisions. Write situations, not conclusions (\"The person says they want to stop\", not \"The person is hiding something\").",
  roles: "Who speaks. Exactly one person uses the tool (asks the questions); answers from the others are checked against the topics.",
  label: "A short name for the topic, as it should appear on the checklist.",
  criteria: "What a complete answer includes, one thing per line. The topic is covered when all of them have been said.",
  follow: "Optional. When a follow-up must be asked (for example: the person says they feel unsafe but not why).",
  required: "Required topics are listed in 'Before you leave' if they are still open.",
  definition: "Advanced. Full rules in prose (partial, covered, follow-up cases). When filled in, it replaces the lines above.",
  examples: "Advanced. Invented examples only, one per line. Never copy lines from a real or test interview.",
};

// Starter rules offered on the Configuration page: generic wording for any interview type, never from a test script.
const FLAG_STARTERS = [
  "Someone asks to stop, pause or leave the interview",
  "Someone mentions a person who should also be spoken to",
  "An answer contradicts something the same person said earlier",
  "Someone says an earlier statement was not true or has changed",
];
const BUILT_IN_FLAGS = ["A concern for someone's safety", "Two accounts of the same event differ (between people, or with a report read out)"];

const linesOf = (text) => text.split("\n").map((l) => l.trim()).filter(Boolean);
const clone = (o) => JSON.parse(JSON.stringify(o));
let fieldSeq = 0;

async function loadCfgPacks(select) {
  let data;
  try { data = await fetchJSON("/api/packs"); }
  catch (e) {
    $("cfgForm").replaceChildren(emptyState("Packs could not be loaded", e.message));
    return;
  }
  const sel = $("cfgPack");
  const ok = data.packs.filter((p) => p.ok);
  if (!ok.length) {
    sel.replaceChildren(new Option("No valid packs", ""));
    $("cfgForm").replaceChildren(emptyState("No interview types yet", "Import a CatchUp template to create the first one."));
    return;
  }
  const wanted = select || new URLSearchParams(location.search).get("pack");
  const current = ok.some((p) => p.file === wanted) ? wanted : cfg.file || data.default;
  sel.replaceChildren(...ok.map((p) => new Option(`${p.name} (${p.version})`, p.file, false, p.file === current)));
  if (cfg.file !== sel.value || select) await openPack(sel.value);
}
$("cfgPack").addEventListener("change", () => openPack($("cfgPack").value));

async function openPack(file) {
  let data;
  try { data = await fetchJSON(`/api/packs/${encodeURIComponent(file)}/raw`); }
  catch (e) { showError(`Could not open ${file}: ${e.message}`); return; }
  Object.assign(cfg, { file, base: data.raw, edit: clone(data.raw), editing: false, problems: data.problems });
  history.replaceState(null, "", `/config?pack=${encodeURIComponent(file)}`);
  $("cfgWarnings").hidden = true;
  $("cfgNote").textContent = `Viewing ${file}. Press Edit to change it; saving makes a new version and keeps this file.`;
  fillScripts(data.scripts);
  renderCfg();
}

function fillScripts(scripts) {
  $("cfgScript").replaceChildren(...scripts.map((s) => new Option(`${s.file} (${s.lines} lines)`, s.file)));
  const none = !scripts.length;
  $("cfgTestFake").disabled = $("cfgTestReal").disabled = none;
  if (none) $("cfgScript").replaceChildren(new Option("no sample whose speakers match these roles", ""));
}

$("cfgEdit").addEventListener("click", () => { cfg.editing = !cfg.editing; renderCfg(); });

// One labelled field with help text behind an info icon (tap or hover).
function field(key, labelText, value, onInput, { multiline = false, loc = "", rows = 3 } = {}) {
  const id = `f${++fieldSeq}`;
  const help = el("p", { className: "help", id: `${id}-help`, hidden: true }, HELP[key]);
  const input = el(multiline ? "textarea" : "input", { id, "aria-describedby": `${id}-help` });
  if (!multiline) input.type = "text";
  else input.rows = rows;
  input.value = value || "";
  input.disabled = !cfg.editing;
  input.addEventListener("input", () => { onInput(input.value); queueValidate(); });
  return el("div", { className: "field", dataset: { loc } }, el("label", { for: id }, labelText, infoButton(HELP[key], help)), help, input);
}

function card(title, ...parts) {
  return el("section", { className: "card cfg-block" }, el("h2", {}, title), parts);
}

function renderCfg() {
  const e = cfg.edit;
  $("cfgEdit").textContent = cfg.editing ? "Stop editing" : "Edit";
  $("cfgEdit").className = cfg.editing ? "" : "btn-primary";
  $("cfgEdit").hidden = !cfg.file;  // an import is always in edit mode
  $("cfgSaveBox").hidden = !cfg.editing;
  $("cfgVersion").placeholder = `after ${e.version || "0.1.0"}`;

  const basics = card("Interview type",
    field("name", "Name", e.name, (v) => { e.name = v; }, { loc: "name" }),
    field("purpose", "Purpose", e.purpose, (v) => { e.purpose = v; }, { multiline: true, loc: "purpose" }));

  // Two lists in one card, stored apart: "pay attention to" only steers; "raise a flag when" must produce a flag card.
  const flagField = field("flags", "Raise a flag when (one per line)", (e.flag_when || []).join("\n"),
    (v) => { e.flag_when = linesOf(v); }, { multiline: true, loc: "flag_when", rows: 4 });
  const flagBox = flagField.querySelector("textarea");
  const builtIn = el("div", { className: "cfg-builtin" }, el("span", { className: "hint" }, "Always flagged, for every interview type:"),
    el("ul", {}, [...BUILT_IN_FLAGS, ...(e.special_topics && e.special_topics.consent_topic ? ["Someone refuses consent to recording"] : [])]
      .map((t) => el("li", {}, "🔒 ", t))));
  const starters = cfg.editing ? el("div", { className: "cfg-starters" }, el("span", { className: "hint" }, "Add a common rule:"),
    FLAG_STARTERS.map((t) => {
      const b = el("button", { type: "button", className: "btn-quiet" }, `+ ${t}`);
      b.disabled = (e.flag_when || []).includes(t);
      b.addEventListener("click", () => {
        e.flag_when = [...(e.flag_when || []), t];
        flagBox.value = e.flag_when.join("\n");
        b.disabled = true;
        queueValidate();
      });
      return b;
    })) : null;
  const watch = card("What the assistant watches for",
    field("focus", "Pay attention to (one per line)", (e.pay_attention_to || []).join("\n"),
      (v) => { e.pay_attention_to = linesOf(v); }, { multiline: true, loc: "pay_attention_to" }),
    flagField, builtIn, starters);

  const rolesHelp = el("p", { className: "help", hidden: true }, HELP.roles);
  const rolesBox = el("div", { className: "field", dataset: { loc: "roles" } },
    el("div", { className: "field-label" }, "Roles", infoButton(HELP.roles, rolesHelp)), rolesHelp);
  for (const r of e.roles || []) {
    const name = el("input", { type: "text", value: r.label, disabled: !cfg.editing, "aria-label": `Name of role ${r.id}` });
    name.addEventListener("input", () => { r.label = name.value; queueValidate(); });
    const user = el("input", { type: "radio", name: "cfgUser", checked: r.kind === "user", disabled: !cfg.editing });
    user.addEventListener("change", () => {
      for (const x of e.roles) x.kind = x === r ? "user" : (x.kind === "user" ? "subject" : x.kind);
      queueValidate();
    });
    rolesBox.append(el("div", { className: "cfg-role" }, name, el("label", { className: "check" }, user, "uses the tool"),
      el("small", { className: "hint" }, `id: ${r.id}`)));
  }
  const roles = card("People in the interview", rolesBox);

  const topics = card(`Topics (${e.checklist.length})`, e.checklist.map((t, n) => topicCard(t, n)));
  if (!e.checklist.length) topics.append(el("p", { className: "empty-inline" }, "No topics yet."));
  if (cfg.editing) {
    const add = el("button", { type: "button", id: "cfgAddTopic" }, "+ Add topic");
    add.addEventListener("click", () => {
      e.checklist.push({ label: "New topic", criteria: [], required: true });
      renderCfg();
      queueValidate();
    });
    topics.append(add);
  }

  const adv = el("details", { className: "card cfg-block cfg-adv" }, el("summary", {}, "Advanced (read-only here)"),
    el("p", { className: "hint" }, "Guardrails, special topics, review sections, form fields and prompts can only be changed in the pack file, with a review, because a wrong edit could weaken the safety rules."));
  for (const [key, title] of [["guardrails", "Guardrails"], ["special_topics", "Special topics"], ["review", "Review sections"],
    ["form_schema", "Form fields"], ["live_prompts", "Live prompts"], ["final_prompts", "Review prompts"], ["persona", "Persona"]]) {
    if (e[key] === undefined) continue;
    adv.append(el("h3", { style: "margin-top:12px;font-size:15px" }, title),
      el("pre", {}, typeof e[key] === "string" ? e[key] : JSON.stringify(e[key], null, 2)));
  }
  if (e.guardrails === undefined) adv.append(para("No guardrails block: the built-in generic rules apply."));
  $("cfgForm").replaceChildren(basics, roles, watch, topics, adv);
  showProblems();
}

function topicCard(t, n) {
  const e = cfg.edit;
  const head = el("div", { className: "cfg-topic-head" }, field("label", `Topic ${n + 1}`, t.label, (v) => { t.label = v; }, { loc: `checklist.${n}.label` }));
  if (cfg.editing) {
    const move = (d) => { const j = n + d; if (j < 0 || j >= e.checklist.length) return; [e.checklist[n], e.checklist[j]] = [e.checklist[j], e.checklist[n]]; renderCfg(); };
    for (const [txt, title, fn, cls] of [["↑", "Move up", () => move(-1), ""], ["↓", "Move down", () => move(1), ""], ["×", "Remove topic", () => {
      showDialog("Remove this topic?", [para(`"${t.label}" will not be in the new version. The original file keeps it.`)],
        "Remove", "Keep it", () => { e.checklist.splice(n, 1); renderCfg(); queueValidate(); }, () => {}, { danger: true, focusButton: true });
    }, "btn-danger"]]) {
      const b = el("button", { type: "button", title, "aria-label": `${title}: ${t.label}`, className: cls }, txt);
      b.addEventListener("click", fn);
      head.append(b);
    }
  }
  const reqHelp = el("p", { className: "help", hidden: true }, HELP.required);
  const cb = el("input", { type: "checkbox", checked: t.required !== false, disabled: !cfg.editing });
  cb.addEventListener("change", () => { t.required = cb.checked; });
  const reqBox = el("div", { className: "field" }, el("div", { className: "field-label" },
    el("label", { className: "check" }, cb, "Required"), infoButton(HELP.required, reqHelp)), reqHelp);
  const adv = el("details", {}, el("summary", {}, "Advanced" + (t.definition ? " (has a full definition)" : "")),
    field("definition", "Full definition", t.definition, (v) => { t.definition = v; }, { multiline: true, loc: `checklist.${n}.definition`, rows: 5 }),
    field("examples", "Examples (invented, one per line)", (t.examples || []).join("\n"), (v) => { t.examples = linesOf(v); }, { multiline: true, loc: `checklist.${n}.examples` }));
  adv.open = !!(t.definition || (t.examples || []).length) && cfg.editing;
  return el("div", { className: "cfg-topic" }, head,
    field("criteria", "A complete answer includes (one per line)", (t.criteria || []).join("\n"), (v) => { t.criteria = linesOf(v); },
      { multiline: true, loc: `checklist.${n}.criteria` }),
    field("follow", "Follow up if", t.follow_up_when, (v) => { t.follow_up_when = v; }, { loc: `checklist.${n}.follow_up_when` }),
    reqBox, adv);
}

function queueValidate() {
  clearTimeout(cfg.timer);
  cfg.timer = setTimeout(async () => {
    try {
      cfg.problems = (await postJSON("/api/packs/validate", { base_file: cfg.file, pack: cfg.edit })).problems || [];
      showProblems();
    } catch (e) { showError(`Could not check the template: ${e.message}`); }
  }, 350);
}

// Messages beside the field they belong to; anything else in a list at the top.
function showProblems() {
  document.querySelectorAll("#cfgForm .field-error").forEach((x) => x.remove());
  const loose = [];
  for (const p of cfg.problems) {
    const boxes = [...document.querySelectorAll("#cfgForm [data-loc]")].filter((b) => b.dataset.loc && (p.loc === b.dataset.loc || p.loc.startsWith(`${b.dataset.loc}.`)));
    const target = boxes.sort((a, b) => b.dataset.loc.length - a.dataset.loc.length)[0];
    if (target) target.append(el("p", { className: "field-error" }, `✕ ${p.msg}`));
    else loose.push(p.loc ? `${p.loc}: ${p.msg}` : p.msg);
  }
  $("cfgProblems").replaceChildren(...loose.map((t) => el("li", {}, t)));
  $("cfgProblems").hidden = !loose.length;
  $("cfgSave").disabled = cfg.problems.length > 0;
}

$("cfgSave").addEventListener("click", async () => {
  let out;
  try { out = await postJSON("/api/packs/save", { base_file: cfg.file, pack: cfg.edit, version: $("cfgVersion").value }); }
  catch (e) { $("cfgSaveMsg").textContent = ""; showError(`Not saved: ${e.message}`); return; }
  if (!out.ok) { cfg.problems = out.problems || []; showProblems(); $("cfgSaveMsg").textContent = "Not saved: see the messages."; return; }
  $("cfgVersion").value = "";
  await loadCfgPacks(out.file);
  $("cfgSaveMsg").textContent = "";
  toast(`Saved as ${out.file} (${out.pack}). It is in the Interview type list now.`, "ok");
});

$("cfgImport").addEventListener("change", async () => {
  const f = $("cfgImport").files[0];
  if (!f) return;
  let doc;
  try { doc = JSON.parse(await f.text()); } catch { showError("That file is not JSON."); return; }
  $("cfgImport").value = "";
  let out;
  try { out = await postJSON("/api/packs/import", doc); } catch (e) { showError(`Not imported: ${e.message}`); return; }
  Object.assign(cfg, { file: null, base: null, edit: out.pack, editing: true, problems: [] });
  $("cfgNote").textContent = `Imported from ${f.name}. Check it, then Save to create the pack.`;
  $("cfgWarnings").replaceChildren(...out.warnings.map((w) => el("li", {}, w)));
  $("cfgWarnings").hidden = !out.warnings.length;
  fillScripts([]);
  renderCfg();
});

async function runTest(real, confirm = false) {
  const body = { base_file: cfg.file, pack: cfg.edit, script: $("cfgScript").value, real, confirm };
  $("cfgResult").replaceChildren(el("p", { className: "loading" }, real ? "Running with real models…" : "Running offline…"));
  let out;
  try { out = await postJSON("/api/packs/test", body); }
  catch (e) { $("cfgResult").replaceChildren(el("p", { className: "field-error" }, `The test did not run: ${e.message}`)); return; }
  if (out.needs_confirm) {
    const est = out.estimate;
    $("cfgResult").replaceChildren();
    showDialog("Run with real models?", [para(est.usd == null ? `Cost estimate unavailable (${est.basis}).` : `Estimated cost: about $${est.usd.toFixed(3)}.`),
      para(est.basis), para("This uses the paid models in settings and is logged in the cost ledger.")],
      "Run it", "Cancel", () => runTest(true, true), () => {}, { focusButton: true });
    return;
  }
  if (!out.ok) {
    cfg.problems = out.problems || [];
    showProblems();
    $("cfgResult").replaceChildren(para(out.error || out.detail || "The template has problems: see the messages."));
    return;
  }
  const items = out.items;
  const list = el("ul", { className: "result-topics" }, gapsFirst(items, out.state).map((i) => {
    const t = out.state[i.id];
    return el("li", {}, el("span", {}, i.label), el("span", { className: "badges" },
      t.follow_up !== "none" ? el("span", { className: `badge ${t.follow_up === "required" ? "badge-solid-danger" : "badge-warn"}` }, `follow-up ${t.follow_up}`) : null,
      statusBadge(t.status)));
  }));
  // flags the sample raised, with their quote: shows what the "raise a flag when" rules do before saving
  const flags = out.cards.filter((c) => c.kind === "flag");
  const flagList = el("div", { className: "result-flags" }, el("h3", {}, `Flags raised (${flags.length})`),
    flags.length ? el("ul", {}, flags.map((f) => el("li", {}, el("span", { className: "badge badge-flag" }, icon("flag"), "Flag"), " ",
      f.question_or_note, el("q", {}, f.evidence.quote))))
      : para(out.real ? "None on this sample." : "None on this sample. The offline models only recognise a few keywords; run with real models to see what your rules do."));
  $("cfgResult").replaceChildren(el("div", { style: "margin-top:12px" }, coverageBlock(items, out.state)),
    el("p", { className: "hint", style: "margin-top:8px" }, `${out.real ? "Real models" : "Offline models"} · ${out.utterances.length} lines · ${out.cards.length} cards · cost $${out.cost_usd.toFixed(4)} · session ${out.session}`),
    list, flagList);
}
$("cfgTestFake").addEventListener("click", () => runTest(false));
$("cfgTestReal").addEventListener("click", () => runTest(true));

loadCfgPacks();
