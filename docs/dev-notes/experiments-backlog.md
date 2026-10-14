# Experiments and changes under discussion (2026-10-14)

Team feedback and the owner's own questions after the merge meeting, with what is already measured, what is not,
and a proposed experiment for each. Decisions are marked **Decided**. Facts that could not be confirmed from a primary
source are marked *unconfirmed*.

## Decided and built (2026-10-14)

1. **Speaker roles worked out from what was said, user can always override** (section 4). Built. Real-model check on the CPS script (fed as mic lines, worker A, parent B, child C, nobody mapped): **all three voices guessed right on the first line each spoke** (lines 1, 3 and 6), no wrong guesses, no line left unknown, checklist 11/11 and line accuracy 0.996, $0.032 (session `20261014-012856-fc2b`). Caveats: clean single-speaker lines, one script; real audio adds mixed turns and label noise.
2. **Prompt caching for the live checklist model** (section 6). Built and confirmed live: **-57% per gate call**.

## Found while checking the latest run (session `20261014-003826-3b49`)

- **A suggested follow-up with no card.**
  - What happened: on the last line, "Child's feelings of safety" dropped from *required* to *suggested*. The required card was withdrawn. The new suggested card was held until the interviewer's next line (`cue_hold_suggested`), but the interview ended first, so nothing was written.
  - Why it vanished everywhere: the review drops follow-ups for covered topics.
  - **Fixed (owner, 2026-10-14):** when the level drops from required to suggested, the card stays and is relabelled as suggested. Other level changes still withdraw it.
- **The review lost the child's statements.** Voices were mapped after End interview. The child's voice (label C) was mapped after the review had already started, so the review saw the child's lines as "unknown". It then dropped 8 child items as `evidence_not_from_child` (4 summary, 3 accounts, 1 timeline). Role inference (section 4) reduces this. **Fixed (owner, 2026-10-14) with (a):** the "Before you leave" dialog lists every voice that is guessed or still has no speaker, with a sample line each, and the user's choices are applied before the review starts. The options were:
  - (a) at End interview, ask for any voices still unmapped before the review starts; or
  - (b) mark the review out of date after a speaker change and offer a re-run.
- **A mixed turn.** u0023 holds the child's "When he yells" and the parent's "please leave, I'm too upset" under one voice label, so the leave flag named the child. Role inference does not fix mixed turns.

- **Guessed roles reach the review and report unconfirmed.** A guess relabels lines the same way a user mapping does, and "Guess ✓" shows only in the voice panel. **Fixed by the same dialog:** guesses are confirmed or changed before the review starts.

- **The clock** now stops at End interview (owner, 2026-10-14), also after a reload.

- **Speaker guessing guards and short-line guesses** (2026-10-14): built and kept. All voice guesses were right on 5 real-audio runs and replays, including the transcript where a mislabelled line once made a whole voice wrong. Short-line guesses were 14 of 16 right. Details in PROGRESS.md.

## 1. Last 12 lines vs the full conversation

**Today:** the gate sees the last 12 lines, each topic's status and follow-up level, the checklist, and the flags already raised. It never sees earlier quotes. The review at the end reads the whole transcript.

**Pros of the window:**
- fast;
- cheap;
- cost grows linearly with interview length.

**Cons of the window:**
- It can miss long-range contradictions: it sees "partial" but not what was said 20 minutes ago.
- Lines are re-read about 12 times, which caused the repeated flags (fixed 2026-10-14 with `flags_raised`).
- Jev, which has no state, forgot consent and age once those lines scrolled out.
- Not tested: the 23-line script fits in about two windows, so its 11/11 result **does not show that the window works for long interviews**.

**The team's "full audio each interval" idea:**
- CatchUp's live mode keeps the realtime session's default truncation on; only uploaded recordings turn it off.
- The realtime model's listed context window is 32k tokens.
- So full context in a long interview is not guaranteed there either. *Unconfirmed how many audio tokens a minute of speech uses.*

**Cost of a full transcript on every line**, for a 45-minute interview at about 5 lines a minute (about 225 gate calls):

| Approach | Input tokens | Gate cost |
|---|---|---|
| Window (today) | ~0.53M | ~$0.55 |
| Full transcript, no caching | ~1.2M | ~$1.05 (above the $1 per-session cap) |
| Full transcript, cached prefix | mostly cached | ~$0.20-0.25 |

Latency should barely change, because it follows output length.

**Middle grounds:**
- per-topic evidence summaries in the state block;
- a periodic full-transcript consistency pass;
- the full transcript with caching.

**Experiment:**
- Write a fictional script of 60+ lines with a contradiction planted 40 lines apart.
- Run it with `window_utterances: 12` and with the full transcript.
- Cost: about $0.10-0.20 per setup.

## 2. Speaker recognition: AssemblyAI vs GPT realtime

**What we know:**
- gpt-realtime returns no speaker labels.
- CatchUp's prompt says roles are "context, not verified speaker identities". Its report evidence is a free-text excerpt with no speaker field, so **CatchUp never attributes statements to people.** There is nothing to compare directly.
- *Unconfirmed:* whether the realtime API offers diarization. It can transcribe input; see OpenAI's out-of-band transcription cookbook.

**What we measured for AssemblyAI:**
- The real 2-person job interview was labelled correctly.
- Synthetic voices failed (not a fair test).
- The CPS script recorded by the owner: one mixed turn (above).
- A real test with three people in a room is still not done.

**Experiment:** run the per-turn realtime prototype (section 5), asking it for `{role, quote}` per turn. Score it and AssemblyAI on the same real recording against an answer key. A script read aloud by 2-3 real people gives the answer key for free.

## 3. Salesforce Service Console layout

**Question first:** will the tool run *inside* the Service Console (a Lightning component, Canvas app or utility bar) or stand alone? If embedded, a standalone look-alike is wasted work. Needed: a screenshot of the target layout.

## 4. Not hand-labelling speakers

**Decided:**
- The model works out roles from content.
- The user can always override.
- A manual mapping always wins and is never replaced by a guess.

**Other options, for later, most reliable first:**
1. A separate mic or channel for the user. The user is the one fixed person in every interview. *Unconfirmed:* how AssemblyAI's streaming multichannel mode is set up.
2. Voice enrolment for the user. *Unconfirmed:* AssemblyAI streaming has no known-speaker feature that we could find.
3. Names and roles preloaded from Salesforce.

**Keep:**
- tap-to-correct on any line;
- guessed roles shown as guesses until confirmed.

## 5. GPT realtime without the 30-second interval

**Idea:** keep one realtime session open and let the API detect turns instead of using a timer. After each turn, ask for a small change-only result, like our gate, not the whole report, because latency follows output length.

**Risks:**
- No checkable quotes unless input transcription is turned on too.
- The context fills up over a long interview, so truncation becomes a risk.
- Audio input costs $32/M tokens, or $0.40/M cached (OpenAI model page).

**How to build it:** a standalone script in this POC, using this POC's own `OPENAI_API_KEY`. No CatchUp code imported, and CatchUp's `.env` untouched. Make a cost estimate before the first run.

## 6. A cheaper decision model

**Prompt caching (decided, built):**
- About 75% of gate cost was input.
- **There was no caching before:** cost equalled the full token price on every call.
- The fix needed two things:
  1. An explicit `cache_control` breakpoint on the unchanging part (system text plus checklist).
  2. **Gemini pinned to one provider.** OpenRouter spreads Gemini over Google AI Studio and Vertex, and the cache lives at one of them. Only AI Studio hit the cache; Vertex never did.
- Measured live: 2,185 of about 2,500 input tokens cached. **$0.0011 instead of $0.0025 per call (-57%)**, at 2.5-4.7 s.
- Data-routing note: Gemini calls (the gate and the follow-up card writer) now go to **Google AI Studio, not Vertex**. Check its data terms before real data.

**Skipping the gate on the user's own lines (built as `gate_skip_user_lines`, off by default; tested 2026-10-14: 13 instead of 23 calls, -31% gate cost, 11/11, all 4 flags kept):**
- The user's lines **stay in the window**: questions, a report read aloud and the consent request are context the gate needs.
- The idea is only not to *call* the gate when the newest line is the user's. That line is read with the next answer.
- Trade-off: a flag or consent prompt triggered by a user line arrives one turn later.
- It saves about 40% of calls (10 of the 23 script lines are the worker's).
- It cannot apply while a line's speaker is unknown. **Owner: only worth doing if the user's voice is identified reliably.** The guess check found the worker's voice on line 1, but on clean lines; check it on real audio first.

**Other models (owner: avoid Chinese models).** All of these are cheaper per token than Gemini 3.8 Flash ($0.75/M input, $3.75/M output):

| Model | Input $/M | Output $/M |
|---|---|---|
| `openai/gpt-6-luna` (**tested 2026-10-14: 8/11, 17 false required follow-ups, 6.7 s; not adopted**) | 0.10 | 0.50 |
| `openai/gpt-6-luna-pro` | 0.10 | 0.50 |
| `inception/mercury-2.5` | 0.04 | 0.15 |
| `liquid/lfm-2.5-24b-a2b` | 0.05 | 0.70 |
| `ibm-granite/granite-4.2-8b` | 0.06 | 0.25 |
| `cohere/command-a-plus` | 0.30 | 1.50 |
| `mistralai/mistral-large-4-0` | 0.68 | 2.09 |

- Origin not checked: nex-agi, prism-ml, inference-net.
- Excluded: deepseek, xiaomi, qwen, inclusionai.
- **Question:** is the concern where the model comes from, or where the data is processed?

**Rough cost per gate call:**

| Setup | Cost per call |
|---|---|
| Gemini, uncached | ~$0.0027 |
| Gemini, prefix cached (measured) | ~$0.0011 |
| gpt-6-luna | ~$0.0004 |

Every cheap model tested so far failed the safety check: it showed the child's feelings of safety as covered when a follow-up was required. That check decides any switch.

**Experiment:** `compare_gates` on 3-4 of the models above, at about $0.05-0.08 each, after caching is in.

## 7. Why "Child's feelings of safety" asks for a follow-up after "I am afraid of him"

This is the pack definition, from the owner's answer to Q7 (2026-10-08), pending John Smith's review:
- A child saying they are afraid or scared **without saying why or when** makes the follow-up *required*.
- Once the child gives even a short reason or time ("When he yells"), the topic is covered and the follow-up becomes *suggested*.
- The fear itself is already raised as a flag.
- The follow-up exists because "why" and "when" are what the worker needs to assess safety.

Changing this is a definition change. It needs:
- an expert's input;
- an entry in the pack change log;
- a regression run.

## Suggested order

| # | Item | Cost |
|---|---|---|
| 1 | ~~Prompt caching~~ done, -57% per gate call | spent ~$0.05 |
| 2 | ~~Role inference check~~ done: 3 of 3 voices right on first line, 11/11 | spent $0.032 |
| 3 | ~~The two fixes found above~~ done (card relabel; voice check before the review) | free |
| 4 | Cheap model comparison | ~$0.30 |
| 5 | Long script: window vs full transcript | ~$0.30 |
| 6 | Realtime per-turn prototype vs AssemblyAI | estimate first, probably $1-3 |
| 7 | Service Console layout | after the embed question |
