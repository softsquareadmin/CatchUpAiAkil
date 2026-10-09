# What is not in the repo, and where it goes

Some files are kept out of git on purpose: secrets, data from past runs, and recordings of real people. The app and
the tests work without any of them (see "Works without anything extra" below). This page says what each one is, why
it is not committed, and where to put it if a teammate sends it to you.

All paths are relative to the project folder (`voicenotes-poc/`, the folder with `app/` and `README.md` in it).

## Overview

| Item | Path | Why it is not in git | Get it by |
|---|---|---|---|
| API keys | `.env` | Secrets | Create your own (below) |
| Python environment | `.venv/` | Built per machine | `uv venv` + `uv pip install` (README, "Requirements and install") |
| Past interviews and test runs | `sessions/` | Run data, can contain transcripts | Ask for a copy, or make your own by running the app |
| Configuration save log | `audit_logs/` | Run data | Created on the first save from the Configuration page |
| Test recordings | `tests/fixtures/audio/` | Recordings of real people | Ask for a copy, or use your own recording |
| CatchUp reference report | `docs/reference/` | Contains content from a real recording; used once for a comparison | Not needed |

## `.env`: your own keys

Copy the template and fill in what you need:

```bash
cp .env.example .env
```

| Key | For |
|---|---|
| `OPENROUTER_API_KEY` | Real models: checklist, follow-up cards, review |
| `ASSEMBLYAI_API_KEY` | Mic and Audio file modes |
| `OPENAI_API_KEY`, `GOOGLE_API_KEY` | Not used by the app yet (reserved). `tools/make_test_audio.py` uses `OPENAI_API_KEY` from your shell to make synthetic test audio |

Costs go to your accounts, under your accounts' rate limits. Each session stops paid calls at `max_session_cost_usd`
(`config/settings.yaml`, default $1.00). Never commit `.env` and never paste keys into chat or tickets.

## `sessions/`: past interviews and test runs

One folder per session (`sessions/<YYYYMMDD-HHMMSS-xxxx>/`) with `utterances.jsonl` (transcript), `events.jsonl`,
`audit.jsonl`, `ledger.jsonl` (cost), and `draft_note.json` / `report.html` once reviewed. The evaluation scripts
also write result files here (`gate-compare-*.json`, `eval-job-audio-*.json`, `cue-compare-*.json`).

**If someone sends you a copy:** unzip it so the session folders sit directly in `sessions/`:

```
voicenotes-poc/
  app/
  sessions/
    20261008-162858-3490/
      utterances.jsonl
      ...
    gate-compare-20261008-131300.json
```

Reload the page (no restart needed: the folder is read on each request). The Sessions and Costs pages, each
session's review page and report, `python -m app.report` and `python -m app.golden` then include them. Review
decisions you make on a copied session are written into that session's folder, like any other.

**Without it:** the Sessions and Costs pages start empty, and `app.report` / `app.golden` print "(no rows)". Session
ids quoted in `docs/dev-notes/PROGRESS.md` cannot be opened, but the numbers recorded there stand on their own.

## `audit_logs/`

Holds `config.jsonl`: one line per interview type saved from the Configuration page. Created automatically. Copy it
to `audit_logs/config.jsonl` only if you want that history; nothing else reads it.

## `tests/fixtures/audio/`: test recordings

The whole folder is ignored, because the recordings used so far are of real people. Nothing in the automated tests
needs audio.

**If someone sends you recordings:** put them in `tests/fixtures/audio/` (create the folder). File names used by
the scripts:

| File | Used by |
|---|---|
| `tests/fixtures/audio/test.mp3` | Default input of `python -m tests.eval_job_audio` (a job interview, about 6 minutes) |
| any `.mp3`, `.wav` or `.m4a` | Audio file mode on the Interview page (choose the file in the browser; it does not need to be in this folder) |

**With your own recording:** `python -m tests.eval_job_audio path/to/your_file.mp3` (paid: speech, checklist, cards
and review; about $0.35 for 6 minutes). Use only recordings of people who agreed, and keep them out of git.

**Synthetic audio:** `tools/make_test_audio.py` turns a script fixture into one audio file with a different
text-to-speech voice per role (needs `OPENAI_API_KEY` and ffmpeg). Note: AssemblyAI could not tell synthetic voices
apart reliably in our tests, so use it for testing the pipeline, not speaker labels.

## Scripts that expect local data

| Script | Needs | Without it |
|---|---|---|
| `tests.smoke_review [session_id]` | A saved real-model session (default: one that only exists on the original machine) | Pass the id of one of your own real-model sessions |
| `tests.measure_review_drops <session_id> <pack> <runs>` | A saved real-model session | Same |
| `tests.eval_job_audio [audio]` | `tests/fixtures/audio/test.mp3` by default | Pass your own file |
| `tests.make_screenshots --costs-from sessions/<id> ...` | Real-model sessions, so the Costs page has rows | Leave out `--costs-from`: the Costs screenshot shows its empty state |

## Works without anything extra

- The app with the free offline models (README, "Run modes" 1), including Replay of the fictional CPS and job
  interview scripts, the Review page and reports, and the Configuration page.
- All automated tests: `.venv/bin/python -m pytest -q` (browser tests need `python -m playwright install chromium`;
  Audio file tests need ffmpeg).
- The sample reports in `docs/sample_reports/` and the screenshots in `docs/screenshots/`.
- Paid scripts that create their own sessions: `tests.smoke_gate`, `tests.smoke_cues`, `tests.compare_gates`,
  `tests.compare_cues`, `tests.make_real_reports`, `tests.check_limiter` (keys needed; each prints its cost).
