# M13 checks (README; SPEC-M7 M11 section + SPEC-M11 M13)

Clean copy: the project folder copied to a scratch folder without `.venv`, `.env`, `sessions/`, `audit_logs/`, `docs/reference/` and caches (2026-10-08). Commands run there, as written in the README (servers on ports 8765 / 8766 instead of 8000 to avoid a clash):

| command (from README) | result |
|---|---|
| `uv venv --python 3.12 .venv` | created |
| `uv pip install --python .venv/bin/python -r requirements-dev.txt` | installed |
| `cp .env.example .env` | empty keys |
| `.venv/bin/python -m playwright install chromium` | downloaded the headless shell |
| `.venv/bin/python -m pytest -q` | `322 passed` |
| offline demo `GATE_MODEL=fake:keyword CUE_MODEL=fake:template REVIEW_MODEL=fake:template .venv/bin/python -m uvicorn app.main:app` | `/health` ok, all keys false, ffmpeg true; `/`, `/sessions`, `/config`, `/costs` 200 |
| walkthrough in headless Chromium on that server: CPS Replay Instant -> "Before you leave" -> End interview -> Open the review; then Job interview pack -> Replay -> End interview -> review | CPS review with 11 topic cards; Job interview review with 5 (the job sample does not end with an ending line, so End interview opens the prompt; README says so) |
| real-model command `.venv/bin/python -m uvicorn app.main:app` with no keys | starts; `/health` shows the OpenRouter gate and every key false (no paid call made) |
| `python -m app.report`, `--health`, `--experiment default --csv out.csv` | "(no rows)": offline calls are left out (README now says so) |
| `python -m app.import_template tests/catchup_templates/all_section_types.json --out packs/my_type.yaml` | written with the roles warning; run again: refused, exit 1, "packs are never overwritten" |
| copy `tests/packs/tiny_pack.yaml` into `packs/` | listed in the picker (`/api/packs`) as "Garden club intake" |
| `python -m tests.make_sample_reports` | byte-identical to the committed samples |
| `python -m app.golden` | "(no rows)" (no saved CPS runs in a clean copy) |
| `python -m tests.check_ui_basics` | 0 failures |
| `python -m tests.make_screenshots --out <dir>` / `python -m tests.screenshot_pages <dir>` | 15 / 18 images |
| paid scripts (`tests.smoke_*`, `compare_*`, `make_real_reports`, ...) | **not run** (paid; run only when asked) |

| requirement | verdict | evidence |
|---|---|---|
| Two parts: using the tool (no coding) and setup / extending | pass | `README.md` Part 1, Part 2, table of contents |
| Pages tour with URLs; report and export section (coverage meaning, topic cards, what the user controls, print to PDF, offline-safe file) | pass | README "Pages", "Review and report" |
| Feature list: every feature exists and is tested or visible | pass | each item maps to a milestone test (M1-M12) or a screenshot; drafting topics from a document is listed as not built |
| Speaker labels and how to fix them | pass | README "Speaker labels" |
| Settings reference incl. `review.show_coverage_score`, `guardrails.allow_overall_recommendation` with the demo-only warning; override order | pass | README "Settings" (every field of `Settings` and `Limits`) |
| Pack format reference with an annotated example; how to add a type (UI, copy, import) | pass | README "Adding an interview type"; copy and import run on the clean copy |
| Rate limits in the developer part and troubleshooting, only observed limits with the date | pass | README "Rate limits" (20/min, 2026-10-08, from `docs/provider-notes.md`) |
| Numbers only from the ledger, with dates | pass | README "Measured in this repo" (2026-10-08 rows from PROGRESS M3, M10, M11) |
| Limits and roadmap (topic-level coverage only; score is not an assessment; not built: Salesforce, PolicyBot, local models, affidavits) | pass | README "Limits and roadmap" |
| Sample reports linked, marked fictional | pass | README "Review and report" |
| Screenshots from the final UI by a script, fake adapters, fictional data, laptop + one tablet live screen; adds config (Advanced collapsed), sessions, costs, review with coverage and an expanded topic card, report top and topic card, export options | pass | `python -m tests.make_screenshots --costs-from sessions/20261008-162858-3490 sessions/20261008-170131-820c` -> `docs/screenshots/` (15 images); the two copied sessions are the M11 real-model runs on the fictional fixtures, so the cost page has real rows |
| Each screenshot checked: no keys, no `.env`, no paths with a user name, fictional data only | pass | all 15 opened and checked by eye (config page shows only the pack file name) |
| Second document: design decisions that differ from CatchUpAI, with reasons | pass | `docs/design-decisions-vs-catchup.md` (14 decisions; CatchUp facts only from the permitted files at d547231) |
| Every README file link resolves | pass | all 21 `docs/...` links checked |
| Earlier tests pass | pass | `.venv/bin/python -m pytest -q` -> `322 passed` |

# M12 checks (SPEC-M11-report-and-readme.md)

| requirement | verdict | evidence |
|---|---|---|
| Every page route returns 200 with the fake adapters and renders with no console errors | pass | `test_every_page_loads_with_no_console_errors_and_no_offsite_requests` (headless Chromium), `test_every_page_route_is_served` |
| Flow: choose a pack, replay the CPS script, see cards; end, open the review, export HTML; `/sessions`, `/costs` | pass | `test_interview_review_export_sessions_and_costs_flow` |
| `/config`: add a topic with tier 1 fields only, save as a new version, see it in the picker | pass | `test_config_add_topic_save_new_version_and_see_it_in_the_picker` (packs copied to a temp folder) |
| Navigation-away warning during a running interview only; the interview keeps running if the person stays; reload resumes | pass | `test_leave_warning_only_while_an_interview_runs_and_reload_resumes`; `test_resume_of_an_unknown_session_says_so`, `test_resume_of_an_unknown_session_is_reported` |
| Pack locked after start; editing on `/config` does not change a running session | pass | same leave test (picker disabled); `test_a_running_interview_keeps_its_pack_when_the_pack_is_edited` |
| Review page works for a live and a past session; decisions audited | pass | `test_review_decisions_over_http_for_a_live_and_a_past_session`; `test_a_running_review_is_not_closed_when_the_browser_leaves` (fails without the fix) |
| Transcript, cards and checklist visible without scrolling on a 13-inch laptop and iPad landscape; no horizontal scroll | pass | `test_live_screen_shows_transcript_cards_and_checklist_without_scrolling[*]` (1280x800, 1180x820) |
| Touch targets >= 44 px; contrast of token pairs | pass | `python -m tests.check_ui_basics` -> 0 failures (after fixing 15: bars, borders, selectors) |
| Info icons work on tap and keyboard, not hover only | pass | `test_info_icon_works_on_tap_and_keyboard` |
| No network request leaves localhost | pass | asserted on every page and the full flow (`Watch.offsite`) |
| Screenshots at laptop and tablet widths for the owner | pass | `python -m tests.screenshot_pages <dir>` (18 images) |
| Framework decision recorded; no library added | pass | PROGRESS (plain JS; Playwright test-only) |
| Live loop tests and CPS golden comparison unchanged; diff review of non-UI changes | pass | `.venv/bin/python -m pytest -q` -> `322 passed`; list in PROGRESS |
| Owner approved the look | owner | screenshots to review |

# M11 checks (SPEC-M11-report-and-readme.md)

| requirement | verdict | evidence |
|---|---|---|
| Coverage score in code: covered 100 / partial 50 / not covered 0, required topics only, optional excluded with a note, no required topics = no score, counts | pass | `test_score_table[*]` (all covered, all not covered, mixed), `test_score_rounds_half_up_and_leaves_optional_and_not_applicable_topics_out`, `test_no_required_topics_shows_no_score`, `test_hidden_score_keeps_counts` |
| `show_coverage_score: false` shows no number on screen, HTML or JSON | pass | `test_hidden_score_shows_no_number_on_screen_html_or_json` (draft, `pack_info`, report JSON, HTML body); live ring and review ring honour the flag (`coverageOf` in app.js) |
| Every topic gets a result: status, summary, quotes, missing, one follow-up | pass | `test_every_topic_gets_a_result_and_bad_parts_are_dropped`, `test_fake_review_gives_every_topic_a_result_and_logs_a_fabricated_quote`, `test_review_schema_always_asks_for_topic_results` |
| Fabricated quote dropped and logged; summary failing the note lint dropped, card still renders; follow-up through the question lint | pass | same two tests (`quote_not_in_transcript` in `review_rejected` events; lint drops; the card keeps its quote) |
| Review may change a status only with a reason (and a quote to raise it); both shown | pass | `test_review_may_change_a_status_only_with_a_reason_and_a_quote`; the HTML shows "Changed by the review from X to Y: reason" |
| User can accept / edit / reject summary and follow-up, change the status (audited, marked) | pass | `test_topic_actions_and_status_set_by_the_user`, `test_status_change_by_user_is_audited_and_marked_in_the_export`; browser check: accept, status change (ring 41% -> 50%, cards re-sorted), no page errors |
| HTML report: single file, contents in the 11.4 order, `<details>` without JS, print stylesheet, dark friendly, deterministic, < 200 KB | pass | `test_sample_reports_match_the_generator_byte_for_byte` (golden files, sizes 17.3 / 15.8 KB); `app/export.py` |
| Escaping (`<script>`, `&`, quotes, long strings) | pass | `test_everything_from_the_transcript_and_model_is_escaped` (transcript, note, reviewer name) |
| No external requests (src / href / @import checked, not a string search) | pass | `test_report_makes_no_external_requests` (only `#u....` links into the appendix; one inline script, no src) |
| Opens with no network in a headless browser; screenshot non-empty; ring text present | pass | `test_report_opens_offline_in_a_headless_browser` (headless shell, proxy to a closed port, host resolution off) |
| Review states: rejected excluded and counted; unreviewed excluded by default, included on request and marked; edited marked | pass | `test_review_states_in_the_export` |
| Overall assessment block only when the pack allows it (screen, HTML, JSON) | pass | `test_overall_assessment_only_when_the_pack_allows_it`; screen: `test_job_pack_via_picker_runs_with_interviewer_labels_and_no_cps_wording` |
| Tiny pack report uses its own labels and no CPS wording | pass | `test_tiny_pack_report_uses_its_own_labels_and_no_cps_wording` |
| Audit: `report_viewed`, `report_exported` (format, unreviewed, transcript, cost), status changed by the user; no transcript text | pass | `test_ws_report_viewed_exported_and_options_audited`, `test_status_change_by_user_is_audited_and_marked_in_the_export` (event named `topic_status_changed_by_user`: "worker" is banned in app/) |
| Grep check (no parent / child / worker / abuse in app/) | pass | `test_engine_code_has_no_cps_words` |
| Sample reports exist, made with fake adapters, watermarked | pass | `docs/sample_reports/*.html`, `python -m tests.make_sample_reports` |
| Items about what was not said rest on the checklist status instead of a quote (owner decision); bad quotes never excused; "walk me through" is open | pass | `test_an_item_about_what_was_not_said_stands_on_the_checklist_status`, `test_overall_assessment_gaps_may_rest_on_the_checklist_but_strengths_need_quotes`, `test_walk_me_through_is_an_open_question`; real job run: 0 parts dropped (was 5) |
| A section lost to the checks is asked for once more (owner decision after measuring: job summary lost in 2/5 reviews) | pass | `test_lost_sections_and_the_retry_schema`, `test_a_section_lost_to_the_checks_is_asked_for_once_more[*]`, `test_no_retry_when_nothing_was_lost`; real job reviews x5 with the retry: fired 2/5, recovered 2/2, 0 sections lost |
| Earlier tests pass | pass | `.venv/bin/python -m pytest -q` -> `308 passed` |
| Review cost before / after (fake and real) recorded | pass | CPS script review $0.093 / 45-49 s (M10) -> $0.114 / 68 s (M11, 2026-10-08); job fixture review $0.063 / 36 s |
| CPS regression and job interview audio re-run once after M11 | pending | Needs the owner's approval of the spend |
| Owner can open the sample HTML and tell which quotes support which sentence | owner | Quotes are listed under each topic's summary with role, time and line id |

# M10a checks (SPEC-M11-report-and-readme.md)

| requirement | verdict | evidence |
|---|---|---|
| Errors classified; only rate_limited, server, timeout retried; auth and bad request never, with a clear message | pass | `test_http_errors_are_classified[*]` (429 + Retry-After, 401 "OpenRouter rejected the key", 400, 503, 504), `test_error_inside_a_200_body_is_classified`, `test_auth_and_bad_request_are_not_retried[*]`, `test_jev_retries_server_errors_but_not_auth` |
| Wait Retry-After, else exponential backoff with jitter; deadline per role; attempt cap; ledger row per attempt (429 ok=false, no cost) | pass | `test_retry_after_header_is_used_then_success`, `test_backoff_without_retry_after_doubles`, `test_live_call_gives_up_inside_its_deadline` (8 s), `test_attempt_cap_holds_even_with_a_long_deadline` (4), `test_llm_gate_retries_provider_error_once` (status / attempt in rows) |
| Budget check before every attempt, including retries | pass | `test_budget_cap_stops_before_any_attempt_and_during_retries` |
| Per-model limiter shared by roles; priority live cue > gate > review > batch; 100 calls at 18/min spread correctly | pass | `test_limiter_spreads_100_calls_at_18_per_minute` (never more than 18 in any 60 s; last call after 5 min), `test_roles_on_one_model_share_a_bucket`, `test_live_cue_goes_ahead_of_a_queued_evaluation_call`, `test_limiter_respects_a_deadline`, `test_batch_sessions_queue_behind_live_ones` |
| Evaluation scripts use the limiter instead of their own spacing | pass | `tests/compare_gates.py` (hand spacing removed, `Session(batch=True)`); "Test this template" uses `batch=True` |
| A burst of calls against an intermittently failing (429) provider finishes without lost updates or a crash | pass | `test_burst_with_intermittent_429s_loses_no_updates` (30 lines, every other call 429: all lines read, nothing skipped, the update applied) |
| The WebSocket sends the health states to the UI | pass | `test_websocket_carries_the_health_state` (hello carries health; "delayed" then "skipped" arrive over the socket) |
| A permanent 429 shows "Analysis skipped" on screen, in the ledger and in the review; the live interview never freezes | pass | `test_permanent_429_skips_visibly_and_the_interview_continues` (delayed then skipped health messages, `analysis_skipped` + `provider_rate_limited` audit rows with no transcript text, 429 rows in the ledger, review notice naming the lines, the next utterance still processed, all inside the deadlines on a fake clock); `test_no_review_notice_when_nothing_was_skipped` |
| Cost report shows per-session calls, 429s, retries, total wait, longest queue wait, skipped runs | pass | `python -m app.report --health` (queue and retry waits logged separately) (e.g. the old unspaced `g38-short` session: 32 calls, 18 HTTP 429) |
| No test sleeps in real time; earlier tests pass | pass | fake clock and sleep throughout; `.venv/bin/python -m pytest -q` -> `277 passed` in about 4 s |
| Paid check: CPS script at instant speed with the limiter on (0 or near-0 429s), then off (429s come back) | partial | 2026-10-08, $0.141: limiter on 0 x 429 (replay 6 calls, burst 23). Limiter off also 0 x 429: gate latency p50 14 s kept the burst at ~6/min, under the limit, so "429s come back" is not confirmed (needs concurrent calls). |
| PROGRESS records the final defaults and the owner's OpenRouter findings | partly | Defaults recorded (18/min, deadlines, 4 attempts, no fallback). Owner task (dashboard / support) still open. |
