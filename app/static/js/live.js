// Interview page (live screen). One WebSocket per interview session; the server owns all state.
// The session id is kept in this tab (sessionStorage), so a reload reconnects to the same interview.
"use strict";

const SESSION_KEY = "interview.session";
const state = { ws: null, sessionStart: null, mode: "replay", speaker: null, stick: true, replaying: false, retry: 0,
  pack: { roles: [], consent_topic: null }, packFile: null, started: false, begun: false, items: [], topics: {}, open: new Set(),
  selected: null, cues: new Map(), seenCues: new Set(), stopped: "", consentAsk: false, review: null,
  sessionId: storedSession(), micWanted: false, mic: { state: "off", labels: [], mapping: {}, manual: null }, audio: null };

function storedSession() { try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; } }
function storeSession(id) { try { id ? sessionStorage.setItem(SESSION_KEY, id) : sessionStorage.removeItem(SESSION_KEY); } catch { /* private mode */ } }

// ---- header status badges: text + symbol, never colour alone ----
const KIND = { ok: "badge-ok", down: "badge-danger", "": "badge-neutral", warn: "badge-warn" };
function setBadge(id, text, kind = "", title = "") {
  const b = $(id);
  b.textContent = text;
  b.className = `badge ${KIND[kind] || kind}`;
  b.title = title;
}
mountHeader("interview", [
  el("span", { id: "rec", className: "badge", title: "Microphone" }, "Mic off"),
  el("span", { id: "consent", className: "badge", title: "Consent to recording, from the checklist" }, "Consent: not yet"),
  el("span", { id: "conn", className: "badge", title: "Connection to the server" }, "connecting"),
  el("span", { id: "health", className: "badge badge-ok", role: "status", "aria-live": "polite", title: "Live analysis" }, "✓ Analysis OK"),
]);

// ---- connection ----
function connect() {
  // A reload or a drop resumes this tab's session (the server keeps it for 2 minutes after the page goes away).
  // A new session uses the pack chosen in the picker.
  const resume = state.sessionId ? `?resume=${encodeURIComponent(state.sessionId)}`
    : state.packFile ? `?pack=${encodeURIComponent(state.packFile)}` : "";
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${resume}`);
  state.ws = ws;
  setBadge("conn", "connecting…");
  ws.onopen = () => { state.retry = 0; setBadge("conn", "● Connected", "ok"); };
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    setBadge("conn", "✕ Disconnected, retrying", "down", "The connection to the server dropped. Reconnecting…");
    setReplaying(false);
    stopCapture();  // the server stops AssemblyAI too; the mic restarts after resume if it was on
    const delay = Math.min(10000, 500 * 2 ** state.retry++);
    setTimeout(connect, delay);
  };
}

function send(msg) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(msg));
  else showError("Not connected to the server.");
}

function onMessage(msg) {
  switch (msg.type) {
    case "session":
      if (msg.resume_failed) {
        toast("The earlier interview in this tab could not be resumed (the server closed it). Its transcript and review are on the Sessions page. This is a new interview.", "error", 12000);
      }
      // the clock runs from Start interview (also right after a reload); before that it shows 00:00
      state.begun = !!msg.begun;
      state.sessionStart = state.begun ? Date.now() - (msg.elapsed_ms || 0) : null;
      tick();
      state.sessionId = msg.session_id;
      storeSession(msg.session_id);
      setPack(msg.pack);
      setStarted(state.begun || (msg.utterances || []).length > 0 || Object.keys((msg.experiment || {}).roles || {}).length > 0);
      $("transcript").replaceChildren();
      for (const u of msg.utterances || []) addUtterance(u);
      state.items = msg.checklist;
      state.topics = msg.state;
      state.open.clear();
      state.selected = null;
      renderChecklist();
      setGate(msg.gate);
      setCueState(msg.cue);
      state.cues.clear();
      for (const c of msg.cues || []) { state.cues.set(c.cue.id, c); state.seenCues.add(c.cue.id); }
      renderCards();
      $("notes").replaceChildren();
      (msg.notes || []).forEach(addNote);
      setStopped(msg.stopped || "");
      setMic(msg.mic);
      state.ffmpeg = msg.ffmpeg;
      setHealth(msg.health);
      setFile(msg.audio_file);
      setReview(msg.review);
      setExperiment(msg.experiment);
      send({ type: "mode", mode: state.mode });
      if (state.micWanted && !msg.stopped) startMic();
      break;
    case "started": setBegun(); break;
    case "audio_file": setFile(msg.audio_file); break;
    case "health": setHealth(msg.health); break;
    case "mic_status":
      setMic(msg.mic, msg.detail);
      if (msg.detail && msg.detail.includes("labels reset")) showError(`Mic ${msg.detail}.`);
      break;
    case "utterance": addUtterance(msg.utterance); setStarted(true); break;
    case "utterance_update": {  // a voice was mapped after its lines were spoken: they now show the role
      const old = document.querySelector(`#transcript li[data-id="${msg.utterance.id}"]`);
      if (old) old.replaceWith(row(msg.utterance));
      break;
    }
    case "topic_update":
      state.topics[msg.state.item_id] = msg.state;
      renderChecklist(msg.state.item_id);
      renderConsent();
      break;
    case "gate_status": setGate(msg.gate); break;
    case "cue_status": setCueState(msg.cue); break;
    case "cue":
      state.cues.set(msg.cue.id, { cue: msg.cue, level: msg.level || "" });
      renderCards();
      if (msg.latency_ms != null) setBadge("cueState", `cue ${msg.latency_ms} ms`, "ok", $("cueState").title);
      break;
    case "cue_update": state.cues.set(msg.cue.id, { ...state.cues.get(msg.cue.id), cue: msg.cue }); renderCards(); break;
    case "cue_withdrawn": state.cues.delete(msg.id); renderCards(); break;
    case "note": addNote(msg.note); break;
    case "consent_check": askConsent(msg); break;
    case "before_leave": beforeLeave(msg); break;
    case "stopped": setStopped(msg.reason); break;
    case "review": setReview(msg.review); break;
    case "experiment": setExperiment(msg.experiment); break;
    case "replay_done": setReplaying(false); break;
    case "paste_labels": showPasteMap(msg.labels, msg.mapping); break;
    case "error": showError(msg.message); break;
  }
}

// ---- transcript ----
function addUtterance(u) {
  const list = $("transcript");
  let partial = list.querySelector("li.partial");
  if (!u.is_final) {
    if (!partial) { partial = row(u); list.append(partial); }
    else partial.querySelector(".text").textContent = u.text;
  } else {
    if (partial) partial.remove();
    list.append(row(u));
  }
  if (state.stick) list.scrollTop = list.scrollHeight;
}

function row(u) {
  const idx = state.pack.roles.findIndex((r) => r.id === u.speaker);
  const cls = `who k-${idx < 0 ? "unknown" : state.pack.roles[idx].kind} r${idx}`;
  // a finished line's speaker can be corrected: tap the name, pick the role (labels can be wrong on short answers)
  const who = u.is_final
    ? el("button", { type: "button", className: `${cls} who-btn`, title: "Change who said this",
      "aria-label": `${roleLabel(u.speaker)}. Change who said this line` }, roleLabel(u.speaker))
    : el("span", { className: cls }, roleLabel(u.speaker));
  const li = el("li", { className: u.is_final ? "final" : "partial", dataset: { id: u.id } }, who,
    el("span", { className: "text" }, u.text));
  if (u.is_final) {
    li.addEventListener("click", () => selectUtterance(u.id));
    who.addEventListener("click", (e) => { e.stopPropagation(); pickSpeaker(who, u); });
  }
  return li;
}

function pickSpeaker(button, u) {
  const sel = el("select", { className: "who-pick", "aria-label": "Who said this line" });
  for (const r of [...state.pack.roles, { id: "unknown", label: "Unknown" }]) sel.add(new Option(r.label, r.id, false, r.id === u.speaker));
  let done = false;
  const finish = (speaker) => {
    if (done) return;
    done = true;
    if (speaker && speaker !== u.speaker) send({ type: "line_speaker", id: u.id, speaker });  // the server sends the new row
    else sel.replaceWith(button);
  };
  sel.addEventListener("click", (e) => e.stopPropagation());
  sel.addEventListener("change", () => finish(sel.value));
  sel.addEventListener("blur", () => finish(null));
  sel.addEventListener("keydown", (e) => { if (e.key === "Escape") { finish(null); button.focus(); } });
  button.replaceWith(sel);
  sel.focus();
}

// Clicking an utterance highlights the checklist items it supports.
function selectUtterance(id) {
  state.selected = state.selected === id ? null : id;
  document.querySelectorAll("#transcript li").forEach((li) => li.classList.toggle("selected", li.dataset.id === state.selected));
  renderChecklist();
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

$("transcript").addEventListener("scroll", () => {
  const t = $("transcript");
  state.stick = t.scrollTop + t.clientHeight >= t.scrollHeight - 40;
  $("jump").hidden = state.stick;
});
$("jump").addEventListener("click", () => { const t = $("transcript"); t.scrollTop = t.scrollHeight; });

// ---- checklist ----
function renderChecklist(changedId) {
  $("coverage").replaceWith(Object.assign(coverageBlock(state.items, state.topics, state.pack.show_coverage_score !== false), { id: "coverage" }));
  $("checklist").replaceChildren(...gapsFirst(state.items, state.topics).map((item) => chip(item, state.topics[item.id], item.id === changedId)));
}

function chip(item, ts, changed) {
  ts = ts || { status: "not_covered", follow_up: "none", evidence: [] };
  const li = el("li", { className: "chip", dataset: { status: ts.status, follow: ts.follow_up } });
  if (changed) li.classList.add("changed");
  if (state.selected && ts.evidence.some((e) => e.utterance_id === state.selected)) li.classList.add("supports");
  const badges = el("span", { className: "badges" });
  if (ts.follow_up !== "none") {
    badges.append(el("span", { className: `badge follow ${ts.follow_up === "required" ? "badge-solid-danger" : "badge-warn"}` },
      ts.follow_up === "required" ? "! Follow-up required" : "? Follow-up suggested"));
  }
  badges.append(statusBadge(ts.status));
  const head = el("button", { type: "button", "aria-expanded": String(state.open.has(item.id)) }, el("span", {}, item.label), badges);
  head.addEventListener("click", () => {
    state.open.has(item.id) ? state.open.delete(item.id) : state.open.add(item.id);
    renderChecklist();
  });
  li.append(head);
  if (state.open.has(item.id)) {
    const detail = el("div", { className: "detail" },
      el("p", { className: "why" }, ts.evidence.length ? ts.rationale_short : (item.definition || "A complete answer includes:")));
    if (item.criteria && item.criteria.length) {  // tier 1 topics: what a complete answer includes
      detail.append(el("ul", { className: "criteria" }, item.criteria.map((c) => el("li", {}, c))));
    }
    if (ts.follow_up !== "none" && ts.follow_up_reason) {
      detail.append(el("p", { className: "why follow-reason" }, `Follow-up ${ts.follow_up}: ${ts.follow_up_reason}`));
    }
    detail.append(...ts.evidence.map((ev) => quoteButton(ev, jumpTo)));
    li.append(detail);
  }
  return li;
}

function setGate(g) {
  if (!g.enabled) {
    setBadge("gate", "✕ Checklist off", "down", g.reason);
    if (g.reason) showError(`Checklist updates are off: ${g.reason}`);
    return;
  }
  const label = g.stage === "fast" ? "fast check" : "gate";
  setBadge("gate", g.latency_ms != null ? `${label} ${g.latency_ms} ms` : "gate ready", g.error ? "down" : "ok",
    `${g.model}${g.cost_usd != null ? ` · $${g.cost_usd}` : ""}${g.error ? ` · ${g.error}` : ""}`);
}

// ---- follow-up cards and flags ----
const labelOf = (id) => (state.items.find((i) => i.id === id) || { label: "General" }).label;

function renderCards() {
  const lists = { follow_up: ["cues", "cueCount", "No follow-up questions right now."],
    flag: ["flags", "flagCount", "No flags."] };
  for (const [kind, [listId, countId, emptyText]] of Object.entries(lists)) {
    // newest first, but required cards always on top: later suggestions must not push a required one down
    const active = [...state.cues.values()].filter((c) => c.cue.kind === kind && c.cue.state === "active").reverse()
      .sort((x, y) => (y.level === "required") - (x.level === "required"));
    $(countId).textContent = active.length;
    $(listId).replaceChildren(...(active.length ? active.map(card) : [el("li", { className: "empty-inline" }, emptyText)]));
  }
}

function card({ cue, level }) {
  const isFlag = cue.kind === "flag";
  const li = el("li", { className: `cue ${isFlag ? "flag" : level}` });
  if (!state.seenCues.has(cue.id)) { li.classList.add("new"); state.seenCues.add(cue.id); }
  const head = el("div", { className: "cue-head" });
  if (isFlag) head.append(el("span", { className: "badge badge-flag" }, icon("flag"), "Flag"), labelOf(cue.topic_id));
  else {
    head.append(el("span", { className: `badge ${level === "required" ? "badge-solid-danger" : "badge-warn"}` },
      level === "required" ? "! Required" : "? Suggested"), labelOf(cue.topic_id));
    if (cue.updated) head.append(el("span", { className: "badge badge-neutral", title: "Rewritten after later answers changed what is missing" }, "↻ Updated"));
  }
  const actions = el("div", { className: "actions" });
  const buttons = isFlag ? [["dismissed", "Noted"]] : [["asked", "Asked"], ["dismissed", "Dismiss"]];
  for (const [action, text] of buttons) {
    const b = el("button", { type: "button", className: action === "asked" ? "btn-primary" : "" }, text);
    b.disabled = !!state.stopped;
    b.addEventListener("click", () => send({ type: "cue_action", id: cue.id, action }));
    actions.append(b);
  }
  li.append(head, el("p", { className: "ask" }, cue.question_or_note), quoteButton(cue.evidence, jumpTo), actions);
  return li;
}

function setCueState(c) {
  setBadge("cueState", c.enabled ? "cues ready" : "✕ Cues off", c.enabled ? "ok" : "down", c.enabled ? c.model : c.reason);
  if (!c.enabled && c.reason) showError(`Follow-up cards are off: ${c.reason}`);
}

// ---- notes ----
$("noteForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("noteText").value.trim();
  if (!text) return;
  send({ type: "note", text });
  $("noteText").value = "";
});

function addNote(n) {
  const s = Math.floor(n.t_ms / 1000);
  $("notes").prepend(el("li", {}, el("small", {}, `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`), n.text));
}

// ---- consent, before you leave, stop ----
function renderConsent() {
  const b = $("consent");
  b.hidden = !state.pack.consent_topic;  // only packs with a consent topic
  const st = (state.topics[state.pack.consent_topic] || {}).status;
  if (state.stopped === "consent refused") { b.textContent = "✕ Consent refused"; b.className = "badge badge-solid-danger"; return; }
  if (state.consentAsk) { b.textContent = "! Consent: refused?"; b.className = "badge badge-solid-danger"; return; }
  const [text, cls] = { covered: ["✓ Consent given", "badge-ok"], partial: ["◐ Consent partly given", "badge-warn"] }[st]
    || ["○ Consent: not yet", "badge-neutral"];
  b.textContent = text;
  b.className = `badge ${cls}`;
}

function askConsent(msg) {
  state.consentAsk = true;
  renderConsent();
  const done = (decision) => { state.consentAsk = false; send({ type: "consent", decision }); renderConsent(); };
  showDialog("Consent may have been refused",
    [para(msg.note), quoteButton(msg.evidence, jumpTo), para("If consent was refused, stop the tool. Nothing more will be transcribed.")],
    "Stop the tool", "Continue: consent was not refused", () => done("stop"), () => done("continue"), { danger: true });
}

function beforeLeave(msg) {
  const body = [];
  if (msg.utterance) body.push(para(`The interview may be ending: “${msg.utterance.text}”`));
  if (msg.open.length) {
    body.push(para("Required topics still open:"));
    body.push(el("ul", {}, msg.open.map((t) => {
      const parts = [STATUS_LABEL[t.status]];
      if (t.follow_up === "required") parts.push(`follow-up required${t.follow_up_reason ? `: ${t.follow_up_reason}` : ""}`);
      return el("li", {}, `${t.label} (${parts.join(", ")})`);
    })));
  } else body.push(para("All required topics are covered."));
  showDialog("Before you leave", body, "End interview", "Keep going", () => send({ type: "end_confirm" }), () => {});
}

function setRec() {
  const b = $("rec");
  const on = ["connecting", "listening", "reconnecting"].includes(state.mic.state);
  const file = state.mic.source === "file";
  b.textContent = state.stopped ? "■ Stopped" : on ? (file ? "● Transcribing file" : "● Mic on") : "○ Mic off";
  b.className = on && !state.stopped ? "badge rec-on" : "badge badge-neutral";
  b.title = state.stopped ? `The tool is stopped: ${state.stopped}` : on ? "Audio is being transcribed" : "Microphone is off";
}

function setStopped(reason) {
  state.stopped = reason;
  setRec();
  syncControls();
  if (reason) { setReplaying(false); stopCapture(); state.micWanted = false; }
  renderConsent();
  renderCards();
}

// Before Start interview: the interview type and input mode can be chosen, nothing records and the clock waits.
// After it: inputs on, End interview instead of Start. After the end: New interview.
function syncControls() {
  const off = !state.begun || !!state.stopped;
  const why = state.begun || state.stopped ? "" : "Press Start interview first";
  for (const id of ["pastePlay", "typedText", "micBtn"]) { $(id).disabled = off; $(id).title = why; }
  $("replayBtn").disabled = off || !state.pack.has_sample;
  $("replayBtn").title = why || (state.pack.has_sample ? "" : "This interview type has no sample script");
  $("fileBtn").disabled = off || state.ffmpeg === false;
  $("fileBtn").title = why;
  $("startBtn").hidden = state.begun || !!state.stopped;
  $("endBtn").hidden = !state.begun || !!state.stopped;
  $("newBtn").hidden = !state.stopped;
}

function setBegun() {
  if (state.begun) return;
  state.begun = true;
  state.sessionStart = Date.now();
  tick();
  setStarted(true);
  syncControls();
}

$("startBtn").addEventListener("click", () => send({ type: "start" }));
$("endBtn").addEventListener("click", () => send({ type: "end" }));
$("newBtn").addEventListener("click", () => newInterview());

function newInterview() {
  state.sessionId = null;
  storeSession(null);
  state.retry = 0;
  state.seenCues.clear();
  if (state.ws) state.ws.close();  // onclose reconnects without a resume id: a new session
}

// ---- the review is written on the server; it opens on its own page ----
function setReview(r) {
  state.review = r;
  const on = r && r.state !== "none";
  $("reviewBanner").hidden = !on;
  if (!on) return;
  $("reviewLink").href = `/review/${encodeURIComponent(state.sessionId)}`;
  $("reviewBanner").classList.toggle("error", r.state === "failed");
  $("reviewBannerText").textContent = { running: "The interview has ended. The review is being written (up to a minute).",
    ready: "The interview has ended. The review is ready.", failed: `The review failed: ${r.error || "unknown error"}.` }[r.state];
  $("reviewLink").textContent = r.state === "failed" ? "Open the session" : "Open the review";
}

// Leaving the page while an interview runs asks first; the interview keeps running if the person stays.
window.addEventListener("beforeunload", (e) => {
  if (state.started && !state.stopped) { e.preventDefault(); e.returnValue = ""; }
});

// ---- mic: browser captures 16 kHz mono PCM16 and streams it to the server, which relays to AssemblyAI ----
const WORKLET = `class Pcm16 extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(1600); this.n = 0; }  // 100 ms at 16 kHz
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.buffer.slice(0)); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("pcm16", Pcm16);`;

async function startCapture() {
  if (state.audio) return;
  const stream = await navigator.mediaDevices.getUserMedia(
    { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  const ctx = new AudioContext({ sampleRate: 16000 });  // the browser resamples the mic to 16 kHz
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" })));
  const node = new AudioWorkletNode(ctx, "pcm16");
  node.port.onmessage = (e) => { if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(e.data); };
  ctx.createMediaStreamSource(stream).connect(node);
  state.audio = { stream, ctx, node };
}

function stopCapture() {
  const a = state.audio;
  if (!a) return;
  state.audio = null;
  a.node.port.onmessage = null;
  a.stream.getTracks().forEach((t) => t.stop());
  a.ctx.close();
}

async function startMic() {
  state.micWanted = true;
  send({ type: "mic_start" });
  try { await startCapture(); }
  catch (e) { showError(`Microphone not available: ${e.message}`); stopMic(); }
}

function stopMic() {
  state.micWanted = false;
  stopCapture();
  send({ type: "mic_stop" });
}

$("micBtn").addEventListener("click", () => (state.micWanted ? stopMic() : startMic()));

function setMic(m, detail) {
  if (!m) return;
  state.mic = m;
  if (m.state === "failed") { state.micWanted = false; stopCapture(); showError(`Mic connection failed: ${detail || ""}`); }
  const text = { off: "○ mic off", connecting: "connecting…", listening: "● listening", reconnecting: "reconnecting…",
    failed: "✕ mic failed", stopped: "○ mic off" }[m.state] || m.state;
  setBadge("micState", text, m.state === "listening" ? "ok" : ["reconnecting", "failed"].includes(m.state) ? "down" : "", m.model);
  $("micBtn").textContent = state.micWanted ? "Stop mic" : "Start mic";
  document.querySelectorAll("#manualSpeaker button").forEach((b) =>
    b.setAttribute("aria-pressed", String((b.dataset.speaker || null) === (m.manual || null))));
  const box = $("labelMap");
  box.replaceChildren();
  for (const label of m.labels) {
    const sel = el("select", { "aria-label": `Voice ${label} is` });
    sel.add(new Option("not set", "", false, !m.mapping[label]));
    for (const r of state.pack.roles) sel.add(new Option(r.label, r.id, false, m.mapping[label] === r.id));
    sel.addEventListener("change", () => send({ type: "speaker", label, speaker: sel.value || null }));
    box.append(el("span", {}, `Voice ${label}`), sel);
  }
  setRec();
}

// ---- pack: roles drive every speaker control; the picker chooses the interview type before it starts ----
const roleLabel = (id) => (state.pack.roles.find((r) => r.id === id) || { label: "Unknown" }).label;

function speakerButtons(box, withAuto, onPick) {
  const opts = [...(withAuto ? [{ id: "", label: "Auto" }] : []), ...state.pack.roles];
  box.replaceChildren(...opts.map((r) => {
    const b = el("button", { type: "button", dataset: { speaker: r.id }, "aria-pressed": "false" }, r.label);
    b.addEventListener("click", () => onPick(r.id, b));
    return b;
  }));
}

function setPack(p) {
  state.pack = p;
  document.title = `${p.name || "Interview"} · Interview Assistant`;
  speakerButtons($("manualSpeaker"), true, (id) => send({ type: "speaker", label: null, speaker: id || null }));
  speakerButtons($("typedSpeaker"), false, (id, b) => {
    state.speaker = id;
    $("typedSpeaker").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("typedText").focus();  // the person just picked a speaker to type for
  });
  if (!state.pack.roles.some((r) => r.id === state.speaker)) state.speaker = p.user_role;
  $("typedSpeaker").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.speaker === state.speaker)));
  syncControls();
  if (p.file) state.packFile = p.file;
  loadPacks();
  renderConsent();
}

async function loadPacks() {
  let data;
  try { data = await fetchJSON("/api/packs"); } catch { return; }
  const sel = $("packSel");
  const current = state.pack.file || data.default;
  const ok = data.packs.filter((p) => p.ok);
  if (!ok.length) { sel.replaceChildren(new Option("No valid interview types", "")); sel.disabled = true; return; }
  sel.replaceChildren(...ok.map((p) => new Option(`${p.name} (${p.version})`, p.file, false, p.file === current)));
  sel.disabled = state.started;
}
// A pack saved on the Configuration page shows up here without a restart.
window.addEventListener("focus", () => { if (!state.started) loadPacks(); });

function setStarted(on) {
  state.started = on;
  $("packSel").disabled = on;
  $("packSel").title = on ? "Locked: this interview has started. Use New interview after ending it." : "";
}

$("packSel").addEventListener("change", () => {
  if (state.started) return;
  state.packFile = $("packSel").value;
  newInterview();  // a new session with the chosen pack
});

// ---- experiment bar: label, models, this session's cost and latency per role ----
function setExperiment(x) {
  if (!x) return;
  $("expLabel").textContent = x.label;
  $("expCost").textContent = `$${x.total_usd.toFixed(4)} of $${x.cap_usd.toFixed(2)} cap`;
  if (document.activeElement !== $("expInput")) $("expInput").value = x.label;
  $("expInput").disabled = !x.editable;
  $("expForm").querySelector("button").disabled = !x.editable;
  $("expHint").textContent = x.editable ? "Set before the first model call." : "Fixed for this session (calls already logged).";
  const ms = (v) => (v == null ? "" : `${v} ms`);
  $("expRows").replaceChildren(...["stt", "gate", "cue", "review"].map((role) => {
    const r = x.roles[role] || {};
    const model = role === "gate" && x.gate_adapter !== "llm" ? `${x.gate_adapter}: ${x.models.gate}` : x.models[role];
    const cells = [role, model, r.calls || 0, r.failed || 0, `$${(r.cost_usd || 0).toFixed(4)}`,
      role === "stt" ? `${(r.seconds || 0).toFixed(0)} s connected` : ms(r.p50_ms), role === "stt" ? "" : ms(r.p95_ms)];
    return el("tr", {}, cells.map((c, i) => el("td", { className: i > 1 ? "num" : "" }, String(c))));
  }));
}

$("expForm").addEventListener("submit", (e) => {
  e.preventDefault();
  send({ type: "experiment", label: $("expInput").value });
});

// ---- live analysis health (M10a): never takes focus, text plus symbol (not colour alone) ----
function setHealth(h) {
  if (!h) return;
  if (h.delayed_s != null) {
    setBadge("health", `⏳ Analysis delayed (~${Math.round(h.delayed_s)} s)`, "warn",
      "The model provider is rate limiting or slow; the checklist will catch up.");
  } else if (h.skipped_lines || h.skipped_cues) {
    const parts = [h.skipped_lines ? `${h.skipped_lines} line${h.skipped_lines === 1 ? "" : "s"}` : "",
      h.skipped_cues ? `${h.skipped_cues} card${h.skipped_cues === 1 ? "" : "s"}` : ""].filter(Boolean);
    setBadge("health", `⚠ Analysis skipped for ${parts.join(" and ")}`, "down",
      "The provider did not answer in time. The review after the interview reads the whole transcript.");
  } else setBadge("health", "✓ Analysis OK", "ok", "Live analysis");
}

// ---- audio file (M8): upload, the server decodes it and streams it at real-time pace ----
function fmtTime(s) { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

function setFile(info) {
  state.fileInfo = info || { state: "idle" };
  const i = state.fileInfo;
  let text = { idle: "no file", uploading: "decoding…", streaming: `● ${fmtTime(i.done_s)} / ${fmtTime(i.total_s)}`,
    done: `✓ done (${fmtTime(i.total_s)})`, stopped: `■ stopped at ${fmtTime(i.done_s)}`, failed: "✕ failed" }[i.state] || i.state;
  if (state.ffmpeg === false) text = "ffmpeg missing";
  setBadge("fileState", text, i.state === "streaming" ? "ok" : i.state === "failed" ? "down" : "", i.name || "");
  const busy = ["uploading", "streaming"].includes(i.state);
  $("fileBtn").textContent = busy ? "Stop" : "Transcribe file";
  syncControls();
  $("fileInput").disabled = busy || state.ffmpeg === false;
  if (i.state === "failed" && i.detail) showError(`Audio file: ${i.detail}. Lines already transcribed are kept.`);
  followFile(i);
}

// "Play the audio here": the browser plays its own copy of the chosen file, following the server's progress
// (sent every second), so a demo can be heard. It pauses when the server pauses (reconnecting) or stops.
let fileUrl = null;
function followFile(i) {
  const a = $("fileAudio");
  if (i.state !== "streaming" || !$("filePlay").checked || !fileUrl) { a.pause(); return; }
  if (a.src !== fileUrl) a.src = fileUrl;
  if (Math.abs(a.currentTime - (i.done_s || 0)) > 1.5) a.currentTime = i.done_s || 0;
  if (a.paused) a.play().catch(() => showError("The browser blocked playback. Untick and tick “Play the audio here” to allow it."));
}
$("filePlay").addEventListener("change", () => followFile(state.fileInfo || {}));
$("fileInput").addEventListener("change", () => {
  if (fileUrl) URL.revokeObjectURL(fileUrl);
  const f = $("fileInput").files[0];
  fileUrl = f ? URL.createObjectURL(f) : null;
});

$("fileBtn").addEventListener("click", async () => {
  if (["uploading", "streaming"].includes((state.fileInfo || {}).state)) { send({ type: "file_stop" }); return; }
  const f = $("fileInput").files[0];
  if (!f) { showError("Choose an mp3, wav or m4a file first."); return; }
  if ($("filePlay").checked && fileUrl) {  // unlock playback inside the click; it starts with the transcription
    const a = $("fileAudio");
    a.src = fileUrl;
    a.play().then(() => a.pause()).catch(() => {});
  }
  setFile({ state: "uploading", name: f.name });
  try {
    const r = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/audio?name=${encodeURIComponent(f.name)}`,
      { method: "POST", body: f });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      setFile({ state: "idle" });
      showError(body.detail || `Upload failed (${r.status})`);
    }
  } catch (e) { setFile({ state: "idle" }); showError(`Upload failed: ${e.message}`); }
});

// ---- modes ----
document.querySelectorAll(".modes button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".modes button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
  $("typedPanel").hidden = mode !== "typed";
  $("pastePanel").hidden = mode !== "paste";
  $("replayControls").hidden = mode !== "replay";
  $("micPanel").hidden = mode !== "mic";
  $("filePanel").hidden = mode !== "file";
  $("voicePanel").hidden = mode !== "mic" && mode !== "file";
  if (mode !== "mic" && state.micWanted) stopMic();  // AssemblyAI bills while connected
  if (mode !== "file" && state.fileInfo && state.fileInfo.state === "streaming") send({ type: "file_stop" });
  send({ type: "mode", mode });
  if (mode === "typed") $("typedText").focus();  // the person chose to type
}

function setReplaying(on) {
  state.replaying = on;
  $("replayBtn").replaceChildren(icon(on ? "stop" : "play"), on ? "Stop" : "Start replay");
  $("pastePlay").textContent = on ? "Stop" : "Play in";
}

const speed = () => Number($("speed").value);

$("replayBtn").addEventListener("click", () => {
  if (state.replaying) { send({ type: "stop" }); setReplaying(false); }
  else { send({ type: "replay", speed: speed() }); setReplaying(true); }
});

// typed (speaker buttons are built from the pack's roles in setPack)
$("typedPanel").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("typedText").value.trim();
  if (!text) return;
  send({ type: "typed", speaker: state.speaker, text });
  $("typedText").value = "";
});

// paste
$("pasteParse").addEventListener("click", () => send({ type: "paste_parse", text: $("pasteText").value }));

function showPasteMap(labels, mapping) {
  const box = $("pasteMap");
  box.replaceChildren();
  for (const label of labels) {
    const sel = el("select", { dataset: { label }, "aria-label": `${label} is` });
    for (const r of [...state.pack.roles, { id: "unknown", label: "Unknown" }]) sel.add(new Option(r.label, r.id, false, mapping[label] === r.id));
    box.append(el("span", {}, label), sel);
  }
  $("pastePlay").hidden = labels.length === 0;
}

$("pastePlay").addEventListener("click", () => {
  if (state.replaying) { send({ type: "stop" }); setReplaying(false); return; }
  const mapping = {};
  $("pasteMap").querySelectorAll("select").forEach((s) => { mapping[s.dataset.label] = s.value; });
  send({ type: "paste_play", text: $("pasteText").value, mapping, speed: speed() });
  setReplaying(true);
});

function tick() {
  if (!state.sessionStart) { $("elapsed").textContent = "00:00"; return; }
  const s = Math.floor((Date.now() - state.sessionStart) / 1000);
  $("elapsed").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
setInterval(tick, 1000);

setReplaying(false);
connect();
