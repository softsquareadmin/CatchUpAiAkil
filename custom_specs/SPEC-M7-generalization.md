# SPEC addendum: generalize to other interview types (M7 to M11)

Read `SPEC.md` and `PROGRESS.md` first. This addendum adds milestones after M6. The working agreement in SPEC section 0 still applies: one milestone at a time, stop for review after each, update `PROGRESS.md` and `checks.md`, fake adapters for tests, paid runs only when asked, respect `max_session_cost_usd`.

## Why

The team wants one tool that runs any interview type from a configuration, with child welfare (CPS) as one of them. A second type, a job interview, will prove it. Testing on new material happens **after** the improvements below, in M10, not before. First round of testing: the existing CPS script (`script_v2.json`) and the job-interview audio. Rhonda's monthly visit script (`monthly_visit_v1.json`) is held back as a later check once it has its own pack; her abuser script is still to be supplied.

**A manager, not an engineer, will fill in the configuration** for an interview type before a caseworker's visit. The configuration therefore has to be small and plain, and the engine has to behave sensibly without domain-specific rules. The current CPS pack was tuned against one script (its definitions were revised after test runs on it, and the expected-status key is partly a model's draft; see `PROGRESS.md`), so there is a real risk of overfitting. The rules below are there to prevent that.

## Ground rules

1. **No change to CPS behavior.** Moving CPS rules from code into the CPS pack must not change what the CPS pack does. Prove it (M7 acceptance).
2. **Generic rules stay in code; domain rules move to the pack.** In code (all packs): assist-only, transcript text is untrusted data, evidence must be a validated quote, no conclusions about people beyond what was said. In the pack: who the speakers are, what counts as a conclusion to avoid, what makes a question unacceptable, the CPS child-safety follow-up rule.
3. Keep the pack file as the single source of truth for an interview type. No interview-type logic keyed on topic IDs or speaker names in code.
4. Do not touch the live-loop design (gate, cue writer, ledger, audit, replay, typed, mic).
5. **Simple by default.** A topic needs only a label and a few lines of what a complete answer includes (section "Configuration model"). Everything else is optional and hidden under "advanced" in the UI.
6. **The engine owns generic behavior.** Handling of vague, evasive, incomplete and contradictory answers is built into the gate and cue prompts for every pack. A pack adds only what is specific to its domain.
7. **No overfitting.** See "Evaluation rules" below. In particular, do not change any CPS definition in this work except to move text; no new tuning against `script_v2.json`.

## Where CPS is hard-coded today (starting list, found by reading the zip; verify)

- `app/models.py`: `Speaker = Literal["worker","parent","child","unknown"]`.
- `app/pipeline.py`: topic ID `recording_consent` (consent flag and stop prompt); `child_age_hint(...)`; speaker whitelist in `set_speaker` (about line 555); audit actor `"worker"`.
- `app/cues.py`: `HARM`, `JUDGE_QUESTION`, `JUDGE_NOTE` word lists; `said` taken from `parent` and `child` speakers; `child_age_hint(topic_id="child_age_grade")`; `ENDING` phrases.
- `app/adapters/deciders/llm.py`: prompt text about "child afraid or unsafe", "parent or child answer", abuse wording (about lines 14 to 43).
- `app/adapters/deciders/jev.py` and `fake.py`: parent / child / worker references.
- `app/adapters/analysts/llm.py`: `<child_age>` block, act and name rules, "caseworker".
- `app/review.py`: `SOURCES`, `SPEAKER_OF`, `ACCOUNT_GROUP`, `ACCOUNT_LABEL`, hard-coded sections (`timeline`, `accounts`, `missing_accounts`), "Parent said / Child said / Worker said".
- `app/adapters/transcribers/replay.py`: `SPEAKERS` and the alias table.
- `app/static/*` and `assemblyai.py`: Worker / Parent / Child labels and label mapping.

## Configuration model: two tiers

**Tier 1, what a manager fills in** (small, plain language, one screen):
- Interview type name, purpose (one paragraph), roles (names, and which one is the person using the tool).
- Topics, each with:
  - **label**
  - **A complete answer includes** (`criteria`): a few short lines, one thing per line
  - **Follow up if** (`follow_up_when`, optional): one or two sentences
  - **required** (yes by default)

Example (topic from the CPS pack, rewritten at tier 1): label "Child's feelings of safety"; criteria "Whether the child feels safe at home", "Why, or when, if not"; follow up if "The child says they feel unsafe or afraid without saying why or when".

**Tier 2, advanced** (optional, collapsed in the UI, may only be edited by someone who understands the effect): `definition` (prose rules with partial / required / follow-up cases, replaces the generated text when present), `examples` (invented, never taken from a test script), `policy_ref`, `special_topics`, `guardrails`, `review` sections, `form_schema`, prompts.

**What the engine does without any tier 2:**
- Builds the topic text for the gate from the label, criteria and `follow_up_when` ("Complete when all of: ...").
- Applies built-in generic follow-up rules for every pack: an answer that is vague, evasive, incomplete or contradicts an earlier statement raises a *suggested* follow-up; a pack can make something *required* only through `follow_up_when` or an advanced definition.
- Uses generic guardrails (assist only, no conclusions about people, open-ended questions only, evidence quotes) when the pack has none.

**Template and visit are separate layers.** A pack (template) is written once by a manager for a visit type and versioned. Facts about one visit (child's name and age, case history) are visit context and will come from the case later (Salesforce, out of scope now). Design the pack format so that visit context can be added to a session later without changing the pack. Do not build it now, but do not put visit-specific facts in packs.

## Evaluation rules (apply to M7 to M10)

1. **Definitions come from experts and policy, not from test lines.** Do not edit a topic definition, criterion or `follow_up_when` to fix a miss on a specific line of a test script. If a miss exposes a real ambiguity, write it in `PROGRESS.md` as a question for the owner (and Rhonda); do not resolve it yourself.
2. **Expected statuses come from a person** (Rhonda or the owner), not from you or another model.
3. **Tune on one, test on others.** Settings and prompts may be tuned on one script; report results on scripts they were not tuned on. Say which is which in every comparison.
4. **Definition change log.** Every change to any pack text gets a line in `PROGRESS.md`: what changed, why, and the source of the reasoning that is not a test line. A change you cannot explain without quoting a test line is reverted.
5. **Automated overlap check.** A test fails if any pack definition, criterion, follow-up text or example shares six or more consecutive words with any text in `tests/fixtures/`. (Examples in the pack stay invented.)
6. **Simple versus advanced.** Create `packs/cps_interview_v2_simple.yaml`: the same CPS topics expressed only at tier 1 (label, criteria, `follow_up_when`), written from the existing v2 definitions without looking at test results, and **not tuned afterwards**. M10 compares it with the advanced pack on the same script, so we learn how much the advanced rules actually add.

## M7: pack format v2 and engine generalization

Add `pack_format: 2` to the pack schema (keep loading v1/CPS files; migrate `cps_interview_v2.yaml` to the new shape, version bump).

```yaml
id: job_interview_v1
version: "0.1.0"
pack_format: 2
name: Job interview
purpose: >            # one paragraph; shown in the UI and given to the gate and cue writer
pay_attention_to: [..]   # optional list of focus areas
persona: >
roles:                # replaces the fixed Speaker type
  - {id: interviewer, label: Interviewer, kind: user}        # the person using the tool; cues are written for them
  - {id: candidate,   label: Candidate,   kind: subject}     # answers are judged against checklist definitions
  # kind: user | subject | other. Exactly one user role. At least one subject role.
checklist:
  - id: ...                     # generated from the label if omitted (unique slug)
    label: ...
    criteria: [..]              # tier 1: what a complete answer includes, one line each
    follow_up_when: ...         # tier 1, optional
    required: true              # tier 1, default true
    definition: ...             # tier 2, optional: prose rules; replaces the generated text when present
    examples: [..]              # tier 2, optional, invented only
    policy_ref: null            # tier 2, optional
guardrails:
  prompt_rules: [..]            # extra rules inserted into gate, cue and review prompts
  question_lint:
    open_ended_only: true
    blocked_terms: [..]         # questions containing these are rejected
    act_terms: [..]             # must not appear unless a subject role already said them (CPS: HARM list)
    name_with_act_rule: true    # CPS rule: a name next to an act must have been said together by a subject
  note_blocked_patterns: [..]   # replaces JUDGE_NOTE
  follow_up_required_when: >    # replaces the hard-coded "child afraid or unsafe" text; may be empty
special_topics:
  consent_topic: recording_consent   # optional. Enables the consent flag and stop prompt
  audience_hint_topic: child_age_grade   # optional. Replaces child_age_hint; also names what the hint is for (age)
ending_phrases: [..]    # optional; default generic list when absent
review:
  sections: [summary, topics_not_covered, follow_ups, form, next_steps]   # optional extras: timeline, accounts, missing_accounts
  custom_sections:      # optional, CatchUp-style custom report sections
    - {id: important_facts, label: Important Facts, type: list, instruction: "...", required: true}
    # type: text | list | status | boolean | score. Optional per type: options (status), min and max (score), max_items (list)
form_schema: [...]      # unchanged
live_prompts: {gate: ..., cue: ...}
final_prompts: {...}
```

Work to do:
1. **Roles.** Replace `Speaker` with a string validated against the pack's role IDs plus `"unknown"`. The UI speaker toggle, the AssemblyAI label mapping, the paste mapping and the replay alias matching all read roles from the pack. Role labels replace "Parent said / Worker said". Utterances saved with the pack's role IDs; the existing CPS fixtures keep working because the CPS pack keeps the IDs `worker`, `parent`, `child`.
2. **Guardrails from the pack.** Build the lint from `guardrails`; keep the CPS word lists, moved into `cps_interview_v2.yaml`. The prompt text that is CPS-specific (child afraid or unsafe, abuse wording, `<child_age>`) becomes pack-supplied text; keep the generic rules in code. For a pack with no guardrails block, use a generic default (open-ended questions only, no tag questions, no conclusions about people).
3. **Special topics.** Replace the hard-coded IDs with `special_topics`. When absent, the feature is simply off (no consent prompt, no hint).
4. **Review sections from the pack.** Build the review schema and prompt from `review.sections`. CPS keeps `timeline`, `accounts`, `missing_accounts` as optional section types; the generic set works for any pack. Attribution uses pack roles.
   Each `custom_section` becomes review items like the others: text gives one item, list gives one item per entry, status / boolean / score give one item holding the value. Every item still needs a validated evidence quote (drop and log the item if it has none), is shown to the worker to accept, edit or reject, and passes the same note lint.
5. **Tier 1 topics.** A topic with only `label`, `criteria` (and optionally `follow_up_when`) must work end to end. The gate gets "Complete when all of: <criteria>. Follow up when: <follow_up_when>" plus the built-in generic follow-up rules; if `definition` exists it is used instead. No per-criterion status yet. Show the criteria as a list in the checklist chip's expanded view. Move the generic follow-up wording (vague, evasive, incomplete, contradictory) into the gate and cue prompts so no pack has to repeat it, and keep the CPS-specific rule (child afraid or unsafe without why or when) in the CPS pack only.
6. **Template import.** `python -m app.import_template <catchupai_template.json> --out packs/<id>.yaml`. Deterministic, no model call. Mapping:
   - `conversation.purpose` -> `purpose`; `pay_attention_to` -> `pay_attention_to`.
   - `conversation.speakers[]` (`id`, `role`) -> `roles` (`id` = slug of the role name, `label` = the role). CatchUp has no kind, so mark the **first** speaker `user` and the rest `subject`, and always print a warning telling the owner to check. (In the sample templates the "Interview" template lists Interviewer then Candidate, which fits; the other two use "Participant 1" and "Participant 2", where the order says nothing.)
   - `topics_to_cover[]` -> checklist items: `id` = unique slug of the label, `label`, `criteria` = the lines, `required: true`. No `definition` (tier 1 only; the engine generates the text).
   - `report.sections[]` -> `review.custom_sections`, keeping `label`, `type`, `instruction`, `required` and the optional `options`, `min`, `max`, `max_items`. CatchUp's five types are text, list, status, boolean and score; the three sample templates use only text and list (the default Summary, Important Facts, Questions Not Yet Asked, Questions Needing Clarification and Recommended Next Action). Section IDs are made unique the same way CatchUp does it. Sections that duplicate a built-in review section (for example a "Summary") are kept as custom sections and flagged in a warning so the owner can drop one.
   - Validate with the pack loader; refuse to write an invalid pack; print every warning.
   Sample templates are in the CatchUp repo under `configuration_templates/` (three job-interview examples). The owner will supply one.
7. **Job interview pack.** Convert the CatchUp "Interview" template (5 topics) with the importer, then hand-edit: roles interviewer / candidate, a short persona, and guardrails suited to hiring: assist only; questions open-ended; never ask about, infer or use protected characteristics (the engine rejects such questions and notes); `allow_overall_recommendation: true` for this demo pack (see below). Reword the imported purpose so it does not say the tool decides suitability, for example: "Track the interview against its goals, capture the candidate's experience and skills, identify topics that remain unanswered or need clarification, and suggest follow-up questions. Any overall assessment is a draft for the interviewer to verify." Mark the guardrails and the reworded purpose "owner to confirm". Keep the imported "Recommended Next Action" section; its instruction must say it covers follow-up questions and unfinished topics, and an overall recommendation only when `allow_overall_recommendation` is on.
   **Overall recommendation setting.** Add `guardrails.allow_overall_recommendation` (boolean, default `false`). When `false`, the gate, cue writer and review never state an overall verdict about a person and the note lint rejects one. When `true` (job interview demo pack only), the review may include an "Overall assessment (draft for human review)" item, subject to all of the following:
   - It is grounded in evidence: strengths and gaps per topic, each with a validated quote; no verdict without them. Drop and log it if any supporting quote fails validation.
   - It is labelled "Draft for human review" on screen and in the export, shown to the worker to accept, edit or reject like every other item, and audited (`ai_output_shown`, then the worker's action).
   - Protected characteristics are never asked about, inferred or used in it. The question lint and note lint reject them, with the term list in the pack.
   - The live gate and cue writer never show a verdict during the interview; it appears only in the post-interview review.
   - **It may be turned on for any pack, including CPS** (owner decision 2026-10-08, so the team can try it and give feedback). Every pack ships with it `false` except the job interview demo pack. It is a file setting (guardrails are not editable in the UI); switching it on for a CPS pack is a pack change that goes in the change log with the owner's name and date, and is audited at session start (`recommendation_enabled: true`, pack id and version).
   - **For a CPS pack the following still apply when it is on, because they are separate rules that the setting does not touch:** the overall assessment is worded as options for the worker to consider, never as a verdict; it never states or implies whether abuse, maltreatment or neglect occurred, who is credible, or what should happen to the child's placement or custody; each item rests on evidence quotes; the abuse, credibility and act-term lints stay active. (The RFP review found a requirement against AI conclusions about abuse; this keeps within it.)
   - Not for real hiring use, and not for real case use: the README states this is a demo, that hiring is a regulated area for AI in some places, and that any use of the setting on real child welfare cases needs the owner's and the agency's approval first.
8. **Pack picker.** List `packs/*.yaml` in the UI; choose before the interview starts (locked once it starts); no restart. Record `pack_id@version` in the audit log and on each ledger row.

**Done when**
- All existing tests pass; the CPS golden comparison (`tests/compare_gates.py`, same settings as the last recorded run) gives the same final coverage within the run-to-run variation already noted in `PROGRESS.md`; the CPS pack's question lint rejects the same cases as before (add tests for the moved rules).
- A topic with only `label` and `criteria` works end to end offline; the overlap check (Evaluation rules, item 5) passes; `packs/cps_interview_v2_simple.yaml` exists and loads.
- A **third, tiny test pack** (2 topics, 2 roles named differently, no special topics, no guardrails block) runs end to end offline with the fake adapters, with no code change, and its role labels appear in the UI and review.
- The importer converts a CatchUp template, refuses an invalid one with a clear message, and prints the warnings listed above. Test it with the three sample templates and with a made-up template that uses all five section types.
- The job interview pack loads, and a fake-model run shows interviewer / candidate labels and no CPS wording anywhere on screen or in the review.
- With `allow_overall_recommendation` off, a fake-model review contains no overall verdict; with it on (job interview pack), the verdict appears only in the review, labelled "Draft for human review", with evidence for every strength and gap, and is audited; a CPS pack with the setting turned on (test copy) still blocks abuse, credibility and placement conclusions in questions, notes and the review, and the shipped CPS packs have it off.
- No file in `app/` contains the strings `parent`, `child`, `worker` or `abuse` outside the CPS pack, fixtures and tests (check with grep; list any that remain and why).

## M8: audio file mode

A fifth input mode, "Audio file".
- Accept mp3, wav and m4a. Decode with ffmpeg to 16 kHz mono PCM (`tests/smoke_mic.py` already does this for synthetic voices). Check that ffmpeg exists at startup and show a clear message if not.
- Stream through the existing AssemblyAI adapter (same speaker labels, reconnect, ledger rows) at real-time pace. **Verify in the AssemblyAI docs whether faster than real time is allowed**; if it is, offer a speed option; if not, 1x only. Record the finding in `docs/provider-notes.md`.
- Speaker mapping works as in mic mode, using the pack's roles.
- Audio is not stored unless `store_audio` is on (audit it as before).
- **Owner decision, default if not answered:** streaming at 1x. A faster "transcribe first, then replay" option through AssemblyAI's pre-recorded API (cheaper, better speaker labels, no live behavior) can follow if wanted.

**Done when:** a short synthetic audio file produces the same kind of events as the mic path (partials, finals, labels, ledger row with audio seconds); a failure to decode or a dropped connection gives a clear message and no lost lines; the test uses a fake transcriber except for one paid smoke test run only when asked.

## M9: configuration screen and visual refresh

Keep the plain-JavaScript page (no Streamlit).
- A **Configuration** page written for a manager. Tier 1 fields are the page: interview type name, purpose, roles (and which one uses the tool), topics with label, "A complete answer includes" (one line each) and "Follow up if". Short plain-language help text beside each field (hover info icons as in CatchUp). Tier 2 (definition, examples, guardrails, review sections, form fields) is under a collapsed "Advanced" area, read-only for guardrails.
- Read-only view first, then editing: add or remove topics, reorder, edit text, edit role labels, **Save as a new pack version** (never overwrite the file in place; write a new file or bump the version and keep the old file). Validation messages beside the field; the loader's rules are the rules.
- **Test this template:** run the selected pack on a chosen sample fixture in Replay at instant speed and show the resulting checklist, so a manager sees what the template does before using it. Free with the fake adapters; with real models, show the estimated cost from the ledger and ask for confirmation first.
- **Optional, only if time remains after the rest of M9 (owner decision):** draft topics from an uploaded document (policy or a visit checklist), like CatchUp's document upload. The model proposes topics and criteria; the manager reviews and edits every one before anything is saved. Follow the same untrusted-document handling as CatchUp's `document_topics.py` (read it under the reference rules).
- Template import from the UI (upload a CatchUp template, show the warnings, save as a pack).
- Visual refresh toward the CatchUp style: coverage score ring, counts of covered / partial / not covered, expandable topic cards sorted so gaps come first. Behavior of the live screen stays as is.
- Guardrails are shown but **not editable** in the first version (a wrong edit could weaken the safety rules); editing them is a file change plus review.

**Done when:** a pack edited in the UI loads in a new session and runs with fake adapters; invalid edits are refused with a message; the original pack file is unchanged; "Test this template" works offline with the fake adapters on both the CPS and the job interview packs; a new topic can be added using tier 1 fields only.

**Owner task (not for Opus):** once M9 is done, ask Rhonda or another non-engineer to fill in a template for the job interview and for a monthly visit using only the tier 1 fields. Record how long it took and what confused them. If it takes more than about 15 minutes or they get stuck, the format is too complex and M9 is reopened.

## M10: evaluation on new material (after M7 to M9 are reviewed)

Only when asked, paid runs. Record every result in the ledger with an experiment label.
First round (owner priority): items 1, 3 and 5. Item 2 follows once Rhonda's topics for the monthly visit exist. The abuser script is still to be supplied.
1. **CPS regression:** the existing script, as in M7 (a re-check after M8 and M9).
2. **Rhonda's monthly visit script** (`tests/fixtures/monthly_visit_v1.json`, 12 lines, roles worker and child) with its own pack written from topics Rhonda supplies; expected statuses come from Rhonda or the owner, not from you. Report agreement per topic. Treat this as a hold-out: no tuning on it.
3. **Job interview audio** (`test.mp3`, about 6 minutes 11 seconds, supplied by the owner) through the Audio file mode with the job interview pack. CatchUp produced a report for the same file (5 topics: 2 covered, 2 partial, 1 not covered, 60%); compare per-topic statuses and the quality of the evidence quotes side by side. Flag disagreements, do not "fix" them.
4. Latency and cost per interview for each, from the ledger report.
5. **Simple versus advanced:** run `cps_interview_v2_simple` and `cps_interview_v2` on the same script, several runs each, and report the difference in final status agreement and in follow-ups (required and suggested). Do not edit either pack afterwards to close the gap; report it.

## M11: README

Write `README.md` at the repo root **last**, after M10, so it describes what exists. Two audiences, two parts.

**Part 1: Using the tool (for an interviewer, no coding)**
- What it is, in a short paragraph, and what it is not: it assists the person doing the interview, never decides, never judges credibility, and the demo data is fictional. State plainly that it is a proof of concept and not for real case data.
- A walkthrough of one session: choose a pack, choose an input mode (Mic, Replay, Typed, Paste, Audio file), start, read the live screen, use follow-up cards (Asked / Dismiss), add notes, the consent prompt and the "Before you leave" prompt, End interview, review the draft (accept, edit, reject), export.
- A **feature list**, grouped: live (transcript with speaker labels, checklist with statuses and evidence quotes, follow-up levels suggested / required, follow-up cards, flags, notes, consent handling, replay speeds), after the interview (review, source-linked items, accept / edit / reject, export as JSON and HTML), configuration (packs, pack picker, configuration screen, template import), records (audit log, cost ledger and report). For each feature one or two lines and a screenshot where one applies (see the screenshot rules below).
- Speaker labels: how the AssemblyAI voice labels are mapped to roles, and how to fix a wrong mapping.

**Part 2: Setup and extending (for a developer)**
- Requirements (Python version, ffmpeg for audio files), install, `.env` (secrets only, which keys are needed for which feature, which are optional), `config/settings.yaml` (every setting explained, the override order yaml, then `.env`, then environment variables).
- Run modes: a **free offline demo** (fake gate and cue models, replay) first, then the paid real-model run; the exact commands.
- How to add a new interview type: copy a pack, edit roles, topics, definitions, criteria, guardrails, review sections; or import a CatchUp template; validate; select it in the picker. Include the pack format reference (the schema from M7) with one annotated example.
- Cost and latency: how to read the cost report, what the experiment label is for, how to compare two configurations. Quote only numbers measured in this repo's ledger or comparison files, with the date.
- Tests: how to run them, what is offline and what is paid, the comparison scripts.
- Repo layout (short), troubleshooting (missing key, rate limits, ffmpeg missing, microphone permission, speaker labels wrong), known limits, and the roadmap (Salesforce, PolicyBot, local speech-to-text, on-device models, court affidavits).

**Screenshots (required)**
- Capture them into `docs/screenshots/` and reference them from the README by relative path.
- Screens: the live screen mid-interview; a checklist chip expanded to show its evidence quote; a follow-up card; the consent prompt; the "Before you leave" prompt; the review screen; the pack picker; the configuration screen; the cost report.
- Produce them with a script (`python -m tests.make_screenshots` or similar) using headless Chromium, the fake adapters and the fictional CPS script, so they cost nothing and can be regenerated when the UI changes. Put the script's command in the README.
- Fictional data only. No API keys, `.env` contents, file paths with a username, or other secrets visible in any screenshot. Check each image before closing the milestone.

**Rules for writing it**
- Every command and path in the README is run once on a clean checkout (or a clean copy of the folder without `.venv`, `.env` and `sessions/`) before the milestone is closed. Quote what you ran in `checks.md`.
- Every feature listed must exist in the code and be covered by a test or visible in the UI; remove anything that does not.
- No secrets, no real case data, no unmeasured claims about speed, accuracy or cost.
- Keep it scannable: short sections, a table of contents, tables only for settings and modes.
- From M7 onward, when a milestone changes how the tool is used or configured, add a one-line note to a `## README notes` list at the end of `PROGRESS.md` so M11 does not have to rediscover it.

**Done when:** following only the README, a person who has not seen the code can install the tool, run the offline demo, replay the CPS script, load the job interview pack, run the tests and read the cost report; and every command in it ran successfully on the clean copy.

## Reference material: the CatchUpAI code

The team's CatchUpAI repo sits next to this project (assumed `../catchupai`; confirm the folder name). Opus may read it as a **reference** under these rules:
- **Read only these files, nothing else:** `catchupai/models.py` (configuration shape and validation), `catchupai/templates.py` (template JSON format, atomic save), `catchupai/topic_coverage.py` (coverage score ring and topic card markup and CSS), `configuration_templates/*.json` (sample templates for the importer tests), `README.md` (how the configuration screen behaves).
- **Never edit it, never import from it, never read its `.env`.** Copy small pieces into this project's own code with a one-line comment naming the source file.
- **Pin the version.** Record the CatchUpAI commit you read in `PROGRESS.md`. The reference point is `d547231`; if the folder is at a different commit, say so and check that the files above still match what this addendum describes.
- Reuse ideas, formats and CSS, not Streamlit code.
- Do not read other files "just in case"; if something else seems needed, ask the owner first.

## Owner decisions to confirm before M7 starts (defaults in brackets)


1. Is `user` (the person using the tool) exactly one role per pack? [yes]
2. Draft overall recommendation (`allow_overall_recommendation`): on for the job interview demo pack, available but off by default for CPS packs, with the conditions above and the protected-characteristics rule? [yes, marked "owner to confirm"; the alternative is evidence-based assessment per topic with no overall verdict]
3. Per-criterion status and evidence (CatchUp style) instead of criteria folded into the definition? [no for now; revisit after M10 if topic-level coverage looks too coarse]
4. Audio file mode default: streaming at 1x or transcribe first? [streaming at 1x]
5. Guardrails editable in the UI? [no, file only]
6. Draft topics from an uploaded document in M9? [optional, only if time remains]
7. Who runs the manager usability check after M9? [owner, with Rhonda or another non-engineer]

## Out of scope

Salesforce, PolicyBot, document upload to topics, small on-device models, AWS Transcribe, authentication, deployment.
