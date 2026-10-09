# SPEC addendum 2: report upgrade, visual pass and README (M11, M12, M13)

Read `SPEC.md`, `PROGRESS.md` and `SPEC-M7-generalization.md` first. This file adds four milestones after M10 and **replaces the old M11 (README)**: M10a is rate-limit robustness, M11 is the report upgrade, M12 is the visual pass and separate pages, and the README is now M13 and describes what M11 and M12 add. The working agreement in SPEC section 0 still applies: one milestone at a time, stop for review after each, update `PROGRESS.md` and `checks.md`, fake adapters for tests, paid runs only when asked, respect `max_session_cost_usd`.

**This file overrides the "single page" constraint** in `SPEC.md` and in M9 of the earlier addendum. The app may have several pages (see M12). Record the change in `PROGRESS.md` and update any text in `SPEC.md` that says one page.

**Order.** The sequence is M10, then **M10a (provider rate limits, below)**, then M11, M12, M13. M10 should additionally record, for every paid run, how many calls got HTTP 429 and how many gate or cue runs were skipped (read them from the ledger; do not change code for this). M10a comes before M11 because the M11 re-runs and the "Test this template" button send calls quickly.

Finish M10 and record its results **before** starting M11. M11 changes the review prompt and output, so M10 numbers must come from the review as it was. After M11, re-run the CPS regression and the job interview audio once (owner approves the spend) and record what changed. M12 changes the look and page structure, not behavior; it comes after M11 so the new review and report screens are styled once. The README (M13) comes last, because its screenshots must show the final UI.

## Why

The team liked CatchUp's report: a coverage score, a short summary per topic, and an HTML file you can open and share. Today the review gives a list of items to accept, edit or reject, and an export as JSON and (per the M7 README text) HTML, but the HTML layout, a per-topic summary and a coverage score were never specified. This addendum specifies them, without losing what the tool does better: every statement rests on a validated evidence quote, and a person reviews it first.

## Ground rules for M11 to M13

1. **Coverage is not a rating of a person.** The score measures how much of the checklist was covered in the conversation. It is never shown as a score for the candidate, parent, child or family. Label it "Checklist coverage" everywhere (screen, HTML, JSON). The README says what it does and does not mean.
2. **The score is computed in code, not by a model.** It comes from the final topic statuses the live loop and review already hold.
3. **Every sentence the model writes in the report passes the same checks as other review items:** a validated evidence quote (or an explicit "not discussed" for gaps), the note lint, and the pack's guardrails. If a topic summary fails, drop it, log it, and show the topic without a summary.
4. **The worker decides what leaves the tool.** The HTML and JSON exports contain only what the worker accepted or edited, plus clearly marked unreviewed items if the worker chooses to include them. Nothing in the export is silently added after review.
5. **The export is a safe file.** Everything from a transcript or a model is HTML-escaped. No external requests (fonts, scripts, images, trackers). It opens correctly offline and prints cleanly.
6. **Fictional data only** in tests, samples and screenshots.

## M10a: provider rate limits and failure visibility

Why: `PROGRESS.md` records that new OpenRouter accounts are limited to 20 requests per minute per model (not in OpenRouter's docs; how it lifts is unconfirmed), that one fast run got HTTP 429 on 18 of 32 calls, and that the fix so far was spacing calls by hand. As the code stands, a failed gate call is retried once immediately, with no wait and no `Retry-After` handling, and after two failures the gate returns an empty result and the interview carries on without telling anyone. Gate and cue use the same model, so they share one per-model limit. Instant replay, "Test this template", the M10 evaluations and several people trying the tool on one key will all hit it. Fix this in the tool, not by hand-spacing runs.

Do not change prompts, lints, statuses or review logic in this milestone.

### 10a.1 Classify provider errors

- `ProviderError` carries `kind` (`rate_limited`, `server`, `timeout`, `auth`, `bad_request`, `other`), `status` and `retry_after_s` when the response has it. Applies to the OpenRouter chat and decisions calls and the Jev call.
- Retry only `rate_limited`, `server` (5xx) and `timeout`. Never retry `auth` or `bad_request` (they will not succeed and may cost money); surface a clear message ("OpenRouter rejected the key", and so on).
- This transport retry is separate from the existing single retry for invalid JSON. Keep the invalid-JSON retry as it is.

### 10a.2 Wait and retry with a deadline

- Wait `Retry-After` when given, otherwise exponential backoff with jitter (start 1 s, doubling, capped).
- Each role has a **deadline** in `config/settings.yaml`: for the live gate and cue about 8 s in total (a cue that arrives after the conversation has moved on is useless, so give up and say so); for the review, test runs and evaluation scripts about 60 s.
- A budget check (`max_session_cost_usd`) runs before every attempt, as now. Every attempt gets a ledger row with the status code, whether it was waited on, and the wait time. A request that was rejected with 429 is recorded as `ok=false` with no cost.
- Hard cap on attempts per call (default 4) so a bug cannot loop paid calls.

### 10a.3 Per-model request limiter

- A limiter keyed by provider and model, shared by every role that uses that model (so gate, cue and review calls on the same model share one bucket). Setting: `limits.requests_per_min` per model, default 18 for OpenRouter models (below the observed 20), `0` or absent means unlimited (fake adapters, the Jev endpoint unless it needs one).
- Priority when calls are waiting: live cue first, then live gate, then review, then test and evaluation runs. A fast run slows itself down and never starves a live interview.
- Gate runs are already serialized and batched per session; when a gate call has waited past a threshold, batch the queued utterances into one run (existing batching) rather than sending several stale calls.
- The evaluation scripts (`tests/compare_gates.py` and the others) use the limiter instead of their own spacing, so results are comparable and nobody has to remember to space calls.
- Optional, ask the owner first: a per-role `fallback_model` (for example a direct Google key) used after repeated 429s on the primary. Off by default. If no direct adapter exists, do not build one in this milestone; only record in `PROGRESS.md` what it would take.

### 10a.4 Make failures visible

- Live screen: a small status indicator for analysis health with three states: normal; "Analysis delayed" (rate limited or retrying, with the wait); "Analysis skipped for N lines" (the gate or cue gave up). Use the same non-color status pattern as elsewhere, and `aria-live` polite. It must not take focus or add a modal.
- If gate runs were skipped, the review screen says so at the top ("The live checklist may be incomplete for lines X to Y") and the review pass is told to cover that part of the transcript. If the review pass already reads the whole transcript, say so in the notice and do not add a second model call.
- The cost report (and the Sessions list once M12 exists) shows per session: calls, 429s, retries, total wait, skipped gate runs, skipped cues, longest queue wait.
- Audit events: `provider_rate_limited` (role, model, wait_s, attempt), `analysis_skipped` (role, reason, utterance id range). No transcript text in them.

### 10a.5 Tests (all offline, no real sleeping)

- Fake provider that returns 429 with and without `Retry-After`, then success: the wait uses the header, uses backoff when absent, and a ledger row exists per attempt.
- 401 and 400 are not retried and give a clear message.
- Deadline: a provider that always returns 429 makes a live gate call give up inside the deadline (injectable clock), emit `analysis_skipped`, and the interview continues.
- Limiter: 100 calls through one model bucket at 18 per minute spread correctly on a fake clock; two roles on the same model share the bucket; the live cue goes ahead of a queued evaluation call.
- Budget cap still stops calls before any attempt once exceeded, including during retries.
- The WebSocket sends the health states to the UI; the review notice appears when a gate run was skipped and not otherwise.
- Existing tests still pass and no test sleeps in real time.

### 10a.6 One small paid check (only when the owner asks)

- Run the CPS script in Replay at instant speed with the real gate, with the limiter at 18 per minute, and report from the ledger: calls, 429s, total wait, skipped runs, and total time compared with the earlier hand-spaced run. Then run it once with the limiter off, on a throwaway session, to confirm the 429s come back (this confirms the limit and that the limiter is what prevents them). Record the observed limit and the dates in `docs/provider-notes.md`.

### Owner tasks (not for Opus)

- Check the OpenRouter dashboard for any per-key or per-model limit shown or adjustable, and whether adding credits changes it; if the limit stays at 20, ask OpenRouter support how it lifts. Tell Opus what you find so the default can change.
- Decide whether a direct Google key should be a fallback (Owner decision 10).

### Done when

- A burst of calls against the fake 429 provider finishes without lost updates or a crash; a permanent 429 shows "Analysis skipped" on screen, in the ledger and in the review; the live interview never freezes.
- The paid check (if run) shows zero or near-zero 429s with the limiter on.
- `PROGRESS.md` records the final defaults and what the owner found on the OpenRouter side.

## M11: topic summary, coverage score and HTML report

### 11.1 Coverage score (code)

- Per topic, from the final status: `covered` = 100, `partial` = 50, `not_covered` = 0. (Use the status names the code already has; map them if they differ.)
- Overall coverage = average over **required** topics, rounded to a whole number. Optional topics (`required: false`) are shown with their status but excluded from the average, and the report says so in one line.
- A topic marked "not applicable" by the worker (if the UI has that action; if it does not, do not add one) is excluded from the average and listed.
- Also report counts: covered, partial, not covered, and total required.
- Add `review.show_coverage_score` to the pack (default `true`). When `false`, the screen and exports show counts and statuses but no number. The CPS packs ship `true` for the demo; the owner can switch it off after team feedback.
- Put the formula in one function with a test table (all covered, all not covered, mixed, optional topics excluded, zero required topics shows "no required topics" and no score).

### 11.2 Topic results in the review

For every checklist topic (not only gaps), the review produces a **topic result**:
- `status` (from the live loop, as confirmed or changed by the review pass; if the review changes a status, show both and say why in one line).
- `summary`: one to three plain sentences on what was actually said about the topic, in the pack's role names, no conclusions about people. Empty if nothing was said.
- `evidence`: one to three validated quotes, each with speaker role and timestamp. Each quote links to its place in the transcript.
- `missing`: a short list of what a complete answer still lacks, drawn from the topic's criteria (tier 1) or definition (tier 2). No per-criterion status yet (owner decision 3 in the earlier addendum stays "no"). This is a plain list of what is absent.
- `suggested_follow_up`: at most one open-ended question for a partial or not-covered topic, passed through the existing question lint. For a not-covered topic, this may be the same question the live cue offered.
- The worker can accept, edit or reject the summary, and edit or remove the suggested follow-up, like any other review item. Status can be changed by the worker; a changed status is audited and flagged "set by worker" in the export.

Implementation notes:
- Extend the review schema and prompt, built from the pack as in M7. Use the generic review sections; do not add CPS wording.
- Cost: one more output field per topic. Measure the review-call cost before and after on the same fixture with the fake and the real model and record the difference in `PROGRESS.md`.
- Statuses and evidence always come from validated live-loop data first. The review model may add the summary and `missing`; it may not invent a quote. Reject and log any quote that is not found in the transcript.

### 11.3 Review screen

- Coverage block at the top: score ring (when `show_coverage_score` is on), counts, and the optional-topics note. Reuse the CatchUp markup and CSS idea from `catchupai/topic_coverage.py` under the reference rules in the earlier addendum, copied in small pieces with a one-line source comment.
- Topic cards below, sorted gaps first (not covered, then partial, then covered), each expandable, showing summary, quotes, missing and suggested follow-up, with accept / edit / reject on the summary and follow-up.
- The overall assessment (when `allow_overall_recommendation` is on) stays a separate, clearly labelled "Draft for human review" block under the topic cards, with the conditions already specified in M7.
- Custom sections and form fields appear below as they do now.

### 11.4 HTML report export

A single self-contained `.html` file written to `sessions/<id>/report.html` and offered as a download. Add "Export HTML" next to the existing JSON export; the JSON export gets the same fields (topic results, score, counts, review states) with a `report_format_version`.

Contents, in order:
1. Header: report title (pack name), pack `id@version`, date and time, duration, roles and their labels, input mode, session id.
2. A visible banner: "AI-assisted draft. Reviewed by <worker name or 'the interviewer'> on <date>. Not a decision." (The worker name field is optional; if empty, use the generic text.)
3. Coverage block: score ring and counts (or counts only), optional-topics note.
4. Topic cards, gaps first, expandable with `<details>` (works without JavaScript): status chip, summary, quotes with role and timestamp, missing, suggested follow-up. Items the worker edited carry an "edited" mark; changed statuses carry "set by worker".
5. Custom sections and form fields, in pack order, each item with its quotes.
6. Overall assessment block, only when the pack allows it, labelled "Draft for human review", with its per-topic strengths and gaps and their quotes.
7. Flags and notes the worker added, and the consent record if the pack uses consent.
8. Optional appendix (toggle at export time, default off): the full transcript with roles and timestamps. Off by default because it is the most sensitive part.
9. Footer: models used for gate, cue and review (ids as recorded in the audit log), generated-on date, and the README's one-line statement of what the tool is and is not. Cost is **not** shown unless the worker ticks "include cost" (default off); the cost report stays a separate file.

Handling of review states:
- Accepted and edited items are included. Rejected items are excluded and counted in one line ("3 items rejected by the worker, not shown").
- Items the worker has not reviewed are excluded by default, with a visible line ("2 items not reviewed"). A checkbox at export time includes them, marked "Not reviewed" in a distinct style.

Format rules:
- Inline CSS only, system fonts, no JavaScript required (small optional script for "expand all" is fine if the page works without it). Light and dark friendly, and a print stylesheet (hidden controls, page-break-safe cards, ring prints in black and white).
- All text escaped; test with transcript lines containing `<script>`, `&`, quotes and long unbroken strings.
- Deterministic output for the same input (stable ordering, no random ids), so the golden test below works.
- File size stays small (target under 200 KB without the transcript appendix); report the size of the sample.

### 11.5 Sample reports

- Generate `docs/sample_reports/cps_replay_sample.html` and `docs/sample_reports/job_interview_sample.html` from the fictional fixtures with the **fake adapters** (no paid call), by a script (`python -m tests.make_sample_reports`). Clearly watermarked "Sample, fictional data" in the header. The job interview sample uses a fictional transcript, not the owner's `test.mp3` content.
- Add the real-model report for `test.mp3` only if the owner asks, and keep it out of git by default (the audio's origin is not confirmed).

### 11.6 Audit

New audit events: `report_viewed`, `report_exported` (format, included-unreviewed flag, transcript-appendix flag, cost flag), `topic_status_changed_by_worker`. No transcript content in the audit log, only ids and counts, as before.

### Tests

- Score function table (see 11.1).
- Review with the fake model: every topic has a result; a fabricated quote is dropped and logged; a summary that trips the note lint is dropped and the card still renders; a pack with `show_coverage_score: false` shows no number anywhere (screen, HTML, JSON).
- Export: golden-file test of the HTML from the fake run; escaping test; no `http://` or `https://` URLs in the file other than ones inside transcript text (assert on `src`, `href` and `@import` attributes instead of a blunt string search); opens with no network (headless Chromium with network disabled, take a screenshot, check it is non-empty and the ring text is present).
- Review states: rejected items excluded, unreviewed excluded by default and included on request with the "Not reviewed" mark, edited items marked.
- The job interview pack with the overall assessment on shows the "Draft for human review" block in screen, HTML and JSON; with it off, none of the three contains it.
- The third tiny test pack from M7 exports a report with its own role labels and no CPS wording.
- Grep check from M7 (no `parent`, `child`, `worker`, `abuse` in `app/` outside the CPS pack, fixtures and tests) still passes, including the new report code and templates.

### Done when

- All earlier tests pass; the new tests pass; the sample HTML reports exist and open offline; the CPS regression and job interview runs are repeated once after M11 with the owner's approval, and the change in statuses, cost and review latency is recorded in `PROGRESS.md` (report differences, do not tune to match the earlier numbers).
- The owner can open the sample HTML, read a topic card, and tell which quotes support which sentence.

## M12: visual pass and separate pages

Goal: the tool looks professional and is pleasant to use on a laptop and on a tablet, and a manager can fill in a template on its own page. **No behavior changes** to the live loop, gate, cue writer, review logic, lints, ledger or audit log. If a behavior change seems needed, stop and ask the owner.

### 12.1 Technology (decision made)

- **No frontend framework and no build step.** Keep plain JavaScript, HTML and CSS served by FastAPI. Reason: no Node requirement, simple offline demo, simple screenshot script, and no rewrite risk for the working WebSocket and mic code.
- **Optional, config page only:** one small reactive library (Preact with htm, or Alpine) if the topic editor (add, remove, reorder) gets awkward in plain JavaScript. It must be **vendored into the repo** (a pinned file under `app/static/vendor/`, with its version and license noted in `docs/provider-notes.md`), with no CDN and no network call at runtime. Ask the owner before adding it; if plain JavaScript is fine, do not add it.
- **No other new dependencies** for the UI (no CSS framework, no icon font from a CDN). Inline SVG for icons.

### 12.2 Pages

Separate pages, shared header and navigation, each reachable by URL:
- `/` **Interview**: pack picker, input mode, live screen (as today).
- `/review/<session>` **Review**: the review and report screen from M11.
- `/config` **Configuration**: the manager's page from M9 (tier 1 fields as the page, Advanced collapsed), template import, "Test this template", pack versions.
- `/sessions` **Sessions**: list of past sessions with date, pack, duration, status (reviewed or not), links to the review, the HTML report and the JSON.
- `/costs` **Cost report**: the existing ledger report with the experiment label filter.

Rules:
- Navigation away from a running interview shows a confirmation, and the interview keeps running if the person stays. A running interview survives a page reload (it already persists to disk; reconnect to it, do not start a new one) or says clearly that it cannot.
- The pack is locked once an interview starts; editing a pack on `/config` never changes a running session. A pack saved as a new version appears in the picker without a restart.
- The config page is usable without an interview and does not require API keys. "Test this template" works with the fake adapters and asks for confirmation before any paid run.
- Plain links and `history` navigation; no client-side router library.

### 12.3 Visual pass

- **Design tokens** in one CSS file: colors (including status colors that also work without color, using icons and text labels), spacing scale, type scale, radius, shadows, focus ring. Light mode first; dark mode if it is cheap with tokens (owner can skip).
- **Components** used everywhere: buttons (primary, secondary, quiet, danger), cards, chips and status badges, tabs, tables, form fields with help text and hover info icons, toasts, empty states, a confirmation dialog. One implementation of each; no per-page copies.
- **Live screen**: keep the layout and speed. Clear hierarchy: transcript, checklist and follow-up cards visible without scrolling on a 13-inch laptop and on an iPad in landscape; follow-up cards are the most prominent thing during the interview; flags and the consent prompt cannot be missed. Respect the existing rule that nothing steals focus from the worker.
- **Touch and tablet**: touch targets at least 44 px, no hover-only actions (every hover info icon also works on tap), readable at tablet width, no horizontal scrolling.
- **Accessibility basics**: keyboard operable, visible focus, labels on every input, sufficient contrast (check text and status colors), `aria-live` for new follow-up cards and flags (polite, not assertive), status never conveyed by color alone, reduced-motion respected.
- **Style reference**: CatchUp's report (see the reference list below). Take layout ideas, spacing and ring and card styling; do not copy its text. The result should look like one product with the HTML report from M11 (same tokens and status colors where practical; the HTML report still keeps its own inline CSS).
- Empty, loading and error states for every page (no pack, no sessions, no API key, WebSocket dropped, failed save).

### 12.4 Tests and checks

- Every page route returns 200 with the fake adapters and renders with no console errors (headless Chromium, `tests/test_ui_pages.py`).
- Playwright flows, offline: choose a pack, replay the CPS script, see follow-up cards; end the interview, open the review, export HTML; open `/config`, add a topic with tier 1 fields only, save as a new version, see it in the picker; open `/sessions` and `/costs`.
- Navigation-away warning appears during a running interview and not otherwise.
- Pack locked after start; editing on `/config` while an interview runs does not change it.
- Touch targets and contrast checked by a script (`tests/check_ui_basics.py`: minimum target size on the main controls, contrast ratios for the token pairs). Report anything that fails; do not hide it.
- Screenshots of every page at laptop and tablet widths saved to a scratch folder for the owner to review (the README screenshots come later in M13).
- All earlier tests still pass. A diff review confirms no change under the live-loop, lint, ledger and audit code except what the page split required; list those changes.

### Done when

- All pages exist and work offline with the fake adapters; the owner has looked at the screenshots and approved the look (or listed changes); the live loop tests and the CPS golden comparison give the same results as before the pass.
- No new network request at runtime from any page (assert in the Playwright run that all requests go to localhost).
- `PROGRESS.md` records the decision on the framework (plain JavaScript, optional vendored library) and any library added with version and license.

## M13: README (replaces the old M11)

All the README content in the earlier addendum's M11 section still applies (two parts, run modes, screenshot rules, rules for writing, done-when). Additions and changes:

- **Pages and navigation** in Part 1: a short tour of Interview, Review, Configuration, Sessions and Cost report, with the URL of each.
- **Screenshots** come from the final UI after M12, taken with the screenshot script at laptop width (and one at tablet width for the live screen), fictional data only. Add the Configuration page (tier 1 view, and the Advanced section collapsed), the Sessions page and the Cost report page to the earlier list.

- **Report and export section** in Part 1: what the coverage score means and does not mean, how topic cards work, what is in the HTML report, what the worker controls (accept, edit, reject, include unreviewed, transcript appendix, cost), how to print to PDF from the browser, and that the file is self-contained and safe to open offline.
- **Feature list** gains: topic summaries and coverage score, HTML report export, sample reports, audio file mode, configuration screen, template import. Each listed feature exists in the code and is covered by a test or visible in the UI.
- **Screenshots** gain: the review screen with the coverage block and an expanded topic card; the exported HTML report opened in the browser (top of page, and one expanded topic card); the export options dialog (include unreviewed, transcript appendix, cost). Generated by the same screenshot script with fake adapters and fictional data.
- **Sample reports**: link `docs/sample_reports/*.html` from the README and say they use fictional data.
- **Rate limits** in the developer part and in troubleshooting: what the per-model request limit setting does, the "Analysis delayed" and "Analysis skipped" indicators, and what to do about them (lower the limit, raise credits or ask the provider, route to another key). Quote only limits observed and recorded in `docs/provider-notes.md`, with the date.
- **Settings reference** includes `review.show_coverage_score` and `guardrails.allow_overall_recommendation`, with the demo-only warning from M7.
- **Limits and roadmap**: state that coverage is topic-level only (no per-criterion status yet), that the score is progress through the checklist and not an assessment, and list what is not built (Salesforce, PolicyBot, local models, affidavits).
- The README is written **last**, after the M11 re-run and the M12 visual pass, and quotes only numbers from the ledger with dates.
- Add a second document outlining different design decisions from CatchUpAI with an explanation of why those decisions were made

## Reference material added by this addendum

Added to the read-only reference list in `SPEC-M7-generalization.md` (same rules: never edit, never import, copy small pieces with a one-line source comment, record what you read in `PROGRESS.md`):
- `docs/reference/catchup_report.html`: CatchUp's sample HTML report for the job interview audio. **Use it for layout, spacing, ring and card styling and print behavior only.** Do not copy its text, topic names or quotes; it contains content from the owner's job interview recording. It lives under `docs/reference/`, **not** `tests/fixtures/`, so the six-word overlap check does not scan it; add `docs/reference/` to `.gitignore` unless the owner says it can be committed. Before using any CSS from it, check it makes no external requests (fonts, scripts, images) and strip any that it does.
- The screenshots of CatchUp's configuration screen, if the owner supplies them, in `docs/reference/` as well, for layout ideas on `/config`.

## Owner decisions (defaults in brackets)

1. Show the coverage number for CPS packs, or counts only? number shown for the demo, switchable per pack with `show_coverage_score`; revisit after team feedback
2. Export includes unreviewed items? no by default, checkbox at export time
3. Transcript appendix in the HTML? off by default, toggle at export time
4. Cost in the HTML? off by default; separate cost report
5. Worker name in the banner? optional field at export time; generic text if empty
6. Frontend approach for M12? plain JavaScript and CSS, no framework, optional vendored Preact or Alpine for the config page only if needed
7. Dark mode? only if cheap with design tokens
8. Is `docs/reference/catchup_report.html` committed or gitignored? gitignored
9. Default `limits.requests_per_min` for OpenRouter models? [18, until the owner confirms the real limit]
10. Allow a fallback model or direct Google key after repeated 429s? [off; owner decides after checking the OpenRouter dashboard]
11. Add per-criterion status later? [revisit after M10 and M11 results; the topic card layout already leaves a place for it]

## Out of scope

PDF generation by the server (use the browser's print), emailing or uploading the report, Salesforce, any change to guardrails editing in the UI, per-criterion status, scoring people.
