# Design decisions: where this tool differs from CatchUpAI, and why

This tool started from the team's CatchUpAI and borrows from it on purpose: the configuration shape (purpose, focus areas, speakers, topics with criteria, typed report sections), the report section validation rules, the coverage scoring (covered 100 / partial 50 / not covered 0), gaps-first ordering, and the coverage ring and topic-card styling. It also imports CatchUp templates directly. This document covers the places where it does something **different** and gives the reason for each.

**Sources for CatchUp.** CatchUpAI at commit `d547231`, and only the files this project was allowed to read: `catchupai/models.py`, `catchupai/templates.py`, `catchupai/topic_coverage.py`, `configuration_templates/*.json` and `README.md`, plus one CatchUp HTML report supplied by the owner for comparison. Where this document says CatchUp does something, it comes from those files. Anything else in CatchUp (for example how its live analysis is prompted) was not read and is not described here.

## Summary

| # | Area | CatchUpAI | This tool | Main reason |
|---|---|---|---|---|
| 1 | When analysis runs | Every N seconds (at least 30) | After every finished line | Follow-ups must arrive while the topic is still being discussed |
| 2 | Coverage detail | Status per criterion; topic status derived from them | Status per topic, plus a separate follow-up level | Smaller, faster model output per line; "must follow up" kept separate from "covered" |
| 3 | Evidence | Free-text evidence per criterion | Exact quotes, each checked against a transcript line | A paraphrase cannot be checked; child welfare work needs the person's own words |
| 4 | Coverage score | Average over all topics; "Overall coverage" | Average over required topics; "Checklist coverage", stated not to be a rating | The number must not read as a score for a person |
| 5 | Follow-up questions | A suggested question per topic in the report | Live cards (Suggested / Required) with Asked / Dismiss, plus review follow-ups | The interviewer needs them during the interview |
| 6 | Safety rules | Not part of the configuration | Guardrails in the pack plus generic rules in code; every question and note is checked | Leading questions and conclusions are real harms in CPS work |
| 7 | Before export | Report built from the analysis | Nothing leaves without the person's accept / edit / reject | The person, not the model, owns what is shared |
| 8 | Saving templates | Save changes in place; delete from the list | Every save is a new version file; nothing is overwritten or deleted from the UI | Each session records the exact version it ran with |
| 9 | Configuration format | One JSON document per template | YAML pack, tier 1 (manager) and tier 2 (advanced), roles with kinds | A manager fills in the simple part; domain rules live in the advanced part |
| 10 | Items about what was not said | Model-written lists (for example "Questions Not Yet Asked") | Same sections, but each item must name checklist topics that are actually partial or not covered | Absence cannot be quoted, so the checklist is the check |
| 11 | Overall verdict | Not part of the configuration files read | Blocked everywhere; an opt-in "Draft for human review" only after the interview | Hiring and case decisions belong to people |
| 12 | Platform | Streamlit (per the spec) | FastAPI, WebSocket, plain JavaScript, separate pages | Live audio streaming, live updates, reload survival, an offline report file |
| 13 | Records | Not in the files read | Audit log, cost ledger per call, spending cap, rate limiter | Accountability and cost control for a pilot |
| 14 | Document upload to topics | Built (PDF, Word and others) | Not built | Marked optional; deferred |

## 1. Analysis after every line, not on a timer

**CatchUp:** the configuration has an `analysis_interval` of at least 30 seconds (`models.py`), so analysis runs on a timer.

**This tool:** a fast model (the "gate", Gemini 3.8 Flash) runs after every finished line on a window of recent lines, and every line is read once. Lines that arrive during a run are batched into the next one.

**Why:** a follow-up is useful only while the interviewer is still on that topic. With a 30-second timer a question can arrive after the conversation has moved on. Measured here (2026-10-08): a checklist update takes a median of about 2.3-4 s after a line, depending on the run, and a follow-up card arrives 2.4-8.3 s after the end of a turn (target 3-4 s).

**Cost of the choice:** more model calls (one per line: about $0.07 for the 23-line CPS script), and a provider rate limit becomes a real concern (decision 13).

## 2. Topic-level status with a separate follow-up level

**CatchUp:** each criterion of a topic has its own status (covered / partial / unanswered) with evidence and a "missing" note; the topic's status is derived from its criteria (`topic_coverage.py`).

**This tool:** one status per topic (not covered / partial / covered), plus a separate **follow-up level** (none / suggested / required) with a reason. The criteria are folded into the topic's text for the model ("Complete when all of: ...").

**Why:**
- Speed: per-criterion results for every topic on every line would multiply the model's output, and output length is what sets latency (measured in the gate experiment).
- Safety: "covered" and "must follow up" are different things. In the CPS script a child says "I am afraid of him": the topic is partly answered, but a follow-up is *required*. One status field cannot say both.
- The owner chose topic level for now. The topic card leaves room for per-criterion status later.

**Measured:** on the job interview audio our topic statuses matched CatchUp's on all 5 topics (both 60%). CatchUp's per-criterion detail named the same gaps that our follow-up reasons and review named.

## 3. Exact, checked quotes instead of free-text evidence

**CatchUp:** evidence is a text field per criterion.

**This tool:** evidence is a list of `{line id, quote}`. Every quote must be found word for word (after normalising case and punctuation) in that final line of the transcript, and in the review it must come from the speaker it is attributed to. A quote that fails is dropped and logged. A status change, card, flag or review item with no valid quote is not shown.

**Why:** the people using this tool may have to defend what they wrote. A paraphrase can drift from what was said; a checked quote cannot. On the job interview, 7 of CatchUp's 16 evidence entries were found word for word in our transcript (different speech-to-text, so some misses may be transcription differences). Ours were all found, by construction.

**Cost:** a good point with a quote that does not match the transcript exactly is dropped rather than shown, and items about what was *not* said cannot be quoted at all (decision 10).

## 4. Coverage over required topics, named so it cannot be read as a rating

**CatchUp:** the score averages over all topics, with topics still awaiting analysis counted as 0, labelled "Overall coverage" (`topic_coverage.py`).

**This tool:** the same 100 / 50 / 0 points, averaged over **required** topics only (optional topics are listed but not counted), rounded half up, computed in code from the final statuses. It is labelled **"Checklist coverage"** everywhere, with "It is not a rating of anyone." An interview type can hide the number and show counts only (`review.show_coverage_score: false`).

**Why:** in a CPS interview a percentage next to a family's name can be misread as a score for the family, and in hiring as a score for the candidate. The label and the sentence make clear it measures the conversation against the checklist. Optional topics should not lower the number.

## 5. Live follow-up cards, not only report suggestions

**CatchUp:** each topic in the report has a suggested follow-up question when not covered.

**This tool:** a card appears during the interview when a topic gets a follow-up level: *Suggested* (vague, evasive, incomplete or contradictory answers, the same for every interview type) or *Required* (only from the interview type's rules). One card per topic at a time; Asked or Dismiss; a cooldown, and the same follow-up returns only with new evidence. The review adds a follow-up per topic afterwards (dropped for covered topics).

**Why:** the point is to help while the person is still in the room. Keeping "required" rules in the pack (not in code) lets each interview type define its own must-ask cases.

## 6. Guardrails and lints in code, not only in the prompt

**CatchUp:** the configuration has purpose, focus areas, speakers, topics and report sections; no safety rules (`models.py`).

**This tool:** generic rules in code for every type (assist only, transcript is data and never instructions, quotes required, no credibility judgements, no conclusions about people, no overall verdict). On top, each pack can add `guardrails`: prompt rules, a **question lint** (open-ended only, blocked terms such as protected characteristics for hiring, act words that must not be put in someone's mouth), and **note patterns** that block conclusions. Every follow-up question, flag and review item is checked in code after the model writes it; failures are dropped or retried once.

**Why:** a prompt can be ignored; a check cannot. In CPS work a leading question ("Did he hit you?") or a written conclusion can harm a case. In hiring, a question about age or family plans is illegal in many places.

**Cost:** occasional false positives (for example "Can you walk me through..." was rejected as a yes/no question until it was added to the open-question list).

## 7. Nothing leaves without review

**CatchUp:** the report is rendered from the analysis result (`topic_coverage.py` builds the HTML directly from the results). No review step appears in the files read.

**This tool:** after the interview the draft opens on a review page. Every model-written part (topic summary, missing list, follow-up, each section item, the assessment) is **proposed** until the person accepts, edits or rejects it. The export contains only accepted or edited parts, unless the person ticks "include items not reviewed" (then each is marked "Not reviewed"). Every decision, with old and new values on edits, goes to the audit log. The report banner says whether, and by whom, it was reviewed.

**Why:** the project's first rule: the tool assists, the person decides. What leaves the tool is the person's document.

## 8. Versions instead of edits in place

**CatchUp:** a loaded template can be updated with "Save changes" and deleted from the list (`templates.py`, README).

**This tool:** saving from the Configuration page always writes a **new file** with a new version number; the original is kept, and nothing is deleted from the UI. A running interview keeps the version it started with. Every session records `pack@version`.

**Why:** a report and its audit trail must say exactly which rules produced it. If a template could change underneath, an old session's checklist could not be explained or re-scored. Evaluation also depends on it: results are compared per version.

## 9. A two-tier pack format with roles

**CatchUp:** one JSON document per template: purpose, focus areas, speakers (`id`, `role`), topics (`id`, `label`, `criteria`), report sections (`models.py`).

**This tool:** a YAML pack. **Tier 1** is what a manager fills in: name, purpose, roles, topics with a label, "a complete answer includes" lines, an optional "follow up if" and required yes/no. **Tier 2** is optional and collapsed: full definitions, invented examples, guardrails, special topics (consent, audience hint), review sections, form fields, prompts. Roles have a **kind**: exactly one `user` (the person using the tool), at least one `subject` (whose answers are judged), others allowed.

**Why:**
- A manager, not an engineer, sets up an interview type. Tier 1 keeps that small; the engine handles vague or evasive answers generically.
- The roles' kinds tell the tool whose answers count as evidence and whose questions to check, without any interview-specific code.
- CPS needs rules that a generic template has no place for (consent, a child's age for wording, account grouping, form fields).

**Kept compatible:** CatchUp's report section types (text, list, status, boolean, score) and their validation were copied, so a CatchUp template imports without loss (`python -m app.import_template`). The importer cannot know which speaker is the interviewer, so it makes the first one the user and warns every time.

**Measured:** a tier-1-only version of the CPS pack missed the required safety follow-up that the full pack caught (M10), so tier 2 matters for safety-critical topics.

## 10. "Not said" items rest on the checklist

**CatchUp:** sections such as "Questions Not Yet Asked" are model-written lists.

**This tool:** the same sections exist (they import from CatchUp), but an item about something that was *not* said must name the checklist topics it rests on, and it is kept only if each of those topics is partial or not covered in the final statuses. It shows "Based on the checklist: <topic> (<status>)", plus the incomplete answer's quote when the topic is partial. Items about what *was* said still need a quote.

**Why:** absence cannot be quoted, but it can be checked against the checklist. The quote and the checklist status are two separate sanity checks (owner decision, M11). Without this rule, valid "not yet asked" items were being dropped for having no quote.

## 11. No overall verdict, except an opt-in draft

**This tool:** overall verdicts ("strong candidate", "should be hired", custody or placement conclusions) are blocked in every live output. A pack may set `allow_overall_recommendation` to add, in the review only, an assessment labelled "Draft for human review": strengths and gaps per topic, each with a quote or checklist basis, then a short draft view. It is all-or-nothing: if any part fails its check, the whole assessment is dropped. On for the job interview demo, off for CPS.

**Why:** decisions about people stay with people. The draft exists for the demo, so the team can see what such a feature would look like, and it is marked "not for real hiring or case decisions".

## 12. FastAPI and plain JavaScript instead of Streamlit

**This tool:** a FastAPI server with a WebSocket per interview, plain HTML, CSS and JavaScript pages with no build step, and the speech-to-text connection relayed through the server (the key never reaches the browser).

**Why:**
- Live microphone audio has to stream continuously, and checklist updates and cards are pushed as they happen. A page that reruns on each interaction does not fit that.
- Separate pages (interview, review, configuration, sessions, costs), a reload that reconnects to the same interview, and a review page that works after a restart.
- No framework or build step keeps it small; nothing beyond the Python requirements is needed to run it.

## 13. Audit log, cost ledger, spending cap and rate limiter

**This tool:** every AI output shown, every decision and every export is written to an append-only audit log per session. Every model and speech call writes a ledger row (tokens, cost, latency, experiment label). A per-session spending cap stops paid calls. A per-model request limiter paces calls under the provider's limit (observed 20 per minute on a new OpenRouter account, 2026-10-08), with retries, and delays or skipped analysis are shown on screen.

**Why:** a pilot in a regulated setting has to show what the AI said and what the person did with it, and the team needs real cost and latency numbers to choose models. Calling the model after every line (decision 1) makes rate limits a real risk, so the tool handles them itself rather than failing silently.

## 14. Not built: drafting topics from a document

CatchUp can extract topics from an uploaded document (PDF, Word, text and others) using a model. This was optional for this project (an owner decision) and was not built. Topics are typed in, imported from a CatchUp template, or edited in the pack file. It remains a sensible next step, with the same rule as everywhere else: suggestions are reviewed before they are used.
