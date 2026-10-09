# Provider notes

Checked once; reuse these instead of re-looking things up. Re-verify if a call starts failing.

## OpenRouter (all language-model roles for now) — checked 2026-10-08

- Endpoint: `POST https://openrouter.ai/api/v1/chat/completions`, `Authorization: Bearer $OPENROUTER_API_KEY`. OpenAI-compatible body.
- Structured output (https://openrouter.ai/docs/features/structured-outputs):
  `"response_format": {"type": "json_schema", "json_schema": {"name": ..., "strict": true, "schema": {...}}}`.
  Some providers behind one model may not support it, so also send `"provider": {"require_parameters": true}`.
  Strict enforcement varies by upstream provider: we still validate every response ourselves.
- Cost (https://openrouter.ai/docs/use-cases/usage-accounting): `usage.cost` is always included, in credits
  (1 credit = 1 USD). Also `usage.prompt_tokens`, `usage.completion_tokens`. Ledger uses `usage.cost` with
  `cost_source: provider`; falls back to `config/prices.json` if missing.
- Reasoning (https://openrouter.ai/docs/use-cases/reasoning-tokens): `"reasoning": {"effort": ...}`.
  Gemini 3 models cannot turn reasoning off; lowest is `"minimal"`. The gate uses `minimal` for latency.
- Model IDs and prices: from the public models API `GET https://openrouter.ai/api/v1/models`
  (`pricing.prompt` / `pricing.completion` are USD per token). All below list `structured_outputs` support.

| role | model id | USD / M input | USD / M output |
|---|---|---|---|
| gate (default) | `google/gemini-3.8-flash` | 0.75 | 3.75 |
| gate (cheaper alt) | `google/gemini-3.5-flash-lite` | 0.30 | 2.50 |
| cue (default from 2026-10-08, owner) | `google/gemini-3.8-flash` (reasoning `minimal`) | 0.75 | 3.75 |
| cue (compared, not used) | `anthropic/claude-haiku-5.5` | 0.10 | 0.50 |
| review (default, used from M5; owner's choice 2026-10-08) | `anthropic/claude-sonnet-5.5` | 2.00 | 10.00 |
| review (stronger alts) | `anthropic/claude-opus-5.5` / `anthropic/claude-fable-5.1` | 4.00 / 10.00 | 20.00 / 50.00 |

Note: the spec suggested Claude Haiku 4.5 for cues; Haiku 5.5 is the current Haiku on OpenRouter (approved 2026-10-08).

## Rate limits (M10a, 2026-10-08)
- Observed: new OpenRouter accounts get **20 requests/min per model** (2026-10-08: an unspaced run got HTTP 429 on
  18 of 32 calls). Not in OpenRouter's docs (https://openrouter.ai/docs/api-reference/limits); how it lifts is
  unconfirmed (owner task: check the dashboard / ask support).
- App default: `limits.requests_per_min: {openrouter: 18}` (per model, shared by gate, cue and review on that model and
  by every session in the process). Retries: 429, 5xx and timeouts only, `Retry-After` honoured, else backoff from
  1 s doubling (cap 8 s, +-20% jitter), at most 4 attempts, inside per-role deadlines (gate and cue 8 s, review and
  batch runs 60 s). 401/402/403 and 400/404/413/422 are never retried. OpenRouter can also return 200 with an error
  object carrying a code; that code is classified the same way.
- Paid check (10a.6, 2026-10-08 15:37-15:46, `python -m tests.check_limiter`, $0.141): instant Replay of the CPS
  script, live session: 6 calls (2 gate, 4 cue; 23 lines batched into 2 gate runs), 0 x 429, 0 waits, 0 skips,
  23.5 s. Gate burst, one call per line back to back, limiter on: 23 calls, 0 x 429, 0 queue wait, 235 s, 11/11 final.
  Same burst, limiter off: 23 calls, 0 x 429, 242 s, 11/11. Gemini 3.8 Flash latency that day was p50 14 s per gate
  call (3-4 s in earlier runs), so a serial burst made only ~6 requests/min and never reached the limit: the run did
  **not** reproduce the 429s, so the 20/min figure is still from the earlier run only. Confirming it needs concurrent
  calls (not run).
- Fallback model (owner decision 10, off): a direct Google Gemini key would need a second provider client (the Gemini
  API's own endpoint and structured-output format, usage and cost fields), a price entry, a `fallback_model` per role,
  and tests. Not built.

## Jev (TypeSafe) via OpenRouter Decisions API — checked 2026-10-08
- Docs: https://openrouter.ai/docs/guides/community/jev-tutorial.md and https://openrouter.ai/docs/guides/community/jev.md
- `POST https://openrouter.ai/api/alpha/decisions` (alpha), Bearer OpenRouter key. Body `{model, state, questions}`.
  `state` is an object of named strings. `questions` is an object keyed by question id, each
  `{"type": "noul"|"choice"|"score", "instructions": str, "criteria": {...} or [...]}`
  (noul: `{"true": ..., "false": ...}`; choice: `{option: description}`; score: ordered list).
- Response `answers[id]`: noul `{"noul": p}`; choice `{"choice", "confidence", "probabilities": {option: p}}`;
  score `{"score", "confidence", "probabilities", "legend"}`. `usage: {input_tokens, output_tokens, cost}` (USD).
- Billing: input tokens only, output free. Not in `/api/v1/models` price list; example in the tutorial gives
  ~$0.042/M input (fallback only, `usage.cost` is used). Context 32k tokens. Alias `~typesafe/jev-latest`.
- Model id `typesafe/jev-1.13` as in the spec.

## Temperature (Gemini 3) — checked 2026-10-08
- Google's Gemini 3 developer guide strongly recommends keeping `temperature` at the default 1.0; below 1.0 may
  cause looping or degraded performance (https://ai.google.dev/gemini-api/docs/gemini-3). The gate sends no
  temperature unless `gate_temperature` is set.

## Google Gemini direct / OpenAI direct
- Not used yet: one provider client (OpenRouter) covers every role. `GOOGLE_API_KEY` / `OPENAI_API_KEY`
  stay unused until a direct client is needed (for example to compare latency without OpenRouter).

## AssemblyAI streaming (v3) — checked 2026-10-08
- Docs: https://www.assemblyai.com/docs/api-reference/streaming-api/streaming-api.md,
  https://www.assemblyai.com/docs/streaming/label-speakers-and-separate-channels.md, https://www.assemblyai.com/pricing
- `wss://streaming.assemblyai.com/v3/ws`. Auth: `Authorization: <api key>` header (no `Bearer`); the server relays,
  so the key never reaches the browser (a `token` query param with a temporary token exists for browsers; not used).
- Query params: `speech_model` (required: `universal-3-6-pro` (default), `universal-3-5-pro`,
  `universal-streaming-english`, `universal-streaming-multilingual`), `sample_rate` (default 16000),
  `encoding` (default `pcm_s16le`), `speaker_labels` (bool), `max_speakers` (1-10), `format_turns`
  (Universal-Streaming only), `inactivity_timeout`. Unknown params are ignored silently, not rejected.
- Audio: binary frames of mono 16-bit PCM, 50-1000 ms each. Client messages: `{"type":"Terminate"}`,
  `ForceEndpoint`, `KeepAlive`, `UpdateConfiguration`.
- Server messages: `Begin {id, expires_at}`; `Turn {turn_order, end_of_turn, turn_is_formatted, transcript,
  words[{text,start,end,confidence,word_is_final,speaker}], speaker_label, speaker_confidence}`;
  `SpeakerRevision`; `Termination {audio_duration_seconds, session_duration_seconds}`.
- Speaker labels: `speaker_label` "A", "B", ...; `"PENDING"` for short turns (< ~1 s) or unclear stretches;
  less stable early in a session; overlapping speech gets one label. Supported on all streaming models.
- Price: Universal-3.6 Pro Realtime $0.45/hr, Universal-Streaming $0.15/hr, streaming diarization +$0.12/hr.
  **Billed by connection (session) duration, idle time included**: close the socket when not listening.
- Limits: sessions auto-close after 3 h; new-session rate limit 5 for free accounts.
- **Pre-recorded files (checked 2026-10-08 for M8):** send audio at the pace it was recorded. Faster than real time
  can degrade accuracy or close the session; the API answers error code 4029 "Client sent audio too fast"
  (https://www.assemblyai.com/docs/streaming/guides/stream_prerecorded_file_realtime.md,
  https://www.assemblyai.com/docs/streaming/rate-limits.md). So the Audio file mode streams at 1x only, with no
  speed option. A faster "transcribe first, then replay" path would use the pre-recorded (async) API instead.
