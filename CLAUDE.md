# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single stdlib-only Python file (`client_meeting_tasks.py`, ~900 lines) that creates paired
`Prep - <Name> (<Ddd>)` / `Notes - <Name> (<Ddd>)` to-dos in Things 3 for upcoming client
meetings, by cross-referencing Google Calendar against active clients in Airtable.

It is designed to be **wrapped by an agent** (Claude skill, OpenClaw/Mandy, launchd), so the
CLI contract is the API: stable subcommands, `--dry-run`, `--json`, and meaningful exit codes.
Treat that contract as load-bearing — changing output shape or exit codes breaks callers.

## Commands

```bash
python3 -m unittest discover -s tests -v          # full suite (47 tests, no network/Things needed)
python3 -m unittest tests.test_client_meeting_tasks.TestDefaultRange.test_monday   # single test
python3 -m py_compile client_meeting_tasks.py     # what CI runs first

./client_meeting_tasks.py run --dry-run --json    # safe preview, creates nothing
./client_meeting_tasks.py run --json              # live
./client_meeting_tasks.py range                   # print today's default range
./client_meeting_tasks.py clients                 # list active clients from Airtable
./client_meeting_tasks.py auth                    # one-time Google OAuth (interactive only)
```

**Run from the repo root.** Config falls back to `~/.env` then `./.env` *resolved against the
current working directory*. `CMT_THINGS_PROJECT=🌱 Client Ops` lives only in the repo's `.env`,
not `~/.env` — so running from any other directory exits `3` with
`Things project 'Client Ops' not found`. Either `cd` here first or pass the variable explicitly.

Python floor is **3.9** (`ZoneInfo`); the CI matrix is 3.9 + 3.12, where 3.9 deliberately mirrors
the Mac mini's system `python3`. No third-party dependencies — keep it that way.

Exit codes: `0` ok · `1` config error · `2` Google/Airtable API error · `3` Things error *or*
some creates failed. Note `run` returns `3` when any single create fails, even if others
succeeded — callers must read `errors` in the JSON, not just the code.

## Architecture

`cmd_run` is the whole pipeline, in order: load config → fetch active clients (Airtable) →
fetch events (Google) → match meetings → read existing to-dos (Things sqlite) → plan tasks →
per task, dedup-check then create. Everything before the create loop is read-only, which is why
`--dry-run` is safe and exercises every integration except the write.

The file is sectioned by banner comments in that same order (config, dates, names, HTTP,
Airtable, Google, Things, planning, commands, CLI). Pure logic — range computation, deadline
clamping, name disambiguation, title normalization, duplicate matching — is deliberately split
from I/O so the test suite needs no network and no Things.

**Things access is sqlite read + `things:///` URL write, never AppleScript.** This is the
central design constraint, not a preference: `osascript` needs an Apple Events grant that macOS
attributes to the *terminal app* driving the script, so it can never be granted under `launchd`
where no terminal exists in the process ancestry. The sqlite + URL-scheme path needs no grant
beyond the Full Disk Access the runner already has, so identical code works interactively and
headless. Do not reintroduce AppleScript here.

Because `things:///` is fire-and-forget, `create_todo` confirms every write by re-polling the
database (up to 20s) and diffing the project's to-do UUIDs before and after — it never trusts
`open(1)`'s exit status. A task that never appears is reported in `errors`, not `created`.

Verified headless: the OpenClaw gateway *can* read the Things database (the Group Container
path is normally the TCC class that never persists, but this read works). The write path has
not yet been exercised headless — a first real create from a non-interactive runner is
unproven, and would surface as `errors` + exit `3` rather than silent success.

## Domain rules that look arbitrary but aren't

- **Title format is part of the dedup key.** `<Ddd>` is the weekday of the *meeting*, and both
  tasks in a pair carry it (a Wednesday meeting → `Prep - Marissa (Wed)` due Tuesday,
  `Notes - Marissa (Wed)` due Wednesday). Changing the suffix shape makes every run create a
  second pair alongside existing tasks.
- **All date math is in `CMT_TIMEZONE`**, never UTC or server-local. An event's date is its
  start converted to that zone — a 02:30 UTC Thursday call is a Wednesday meeting in California.
- Default range depends on weekday: Mon–Thu → today through Friday of *this* week; Fri–Sun →
  Monday through Friday of *next* week. `--start`/`--end` override entirely and must be passed
  together.
- Prep deadline is the day before the meeting, **clamped to today** if the meeting is today, so
  it is never born overdue. A deliberately backdated run is left untouched.
- **Meetings are matched by attendee email only**, never by event title — titles produce too
  many false positives (holds, mentions, similarly-named people). Cancelled, declined, and
  non-default event types (focus time, OOO, working location, birthdays) are skipped.
- Dedup blocks on: same title + same deadline; same title within `CMT_DEDUP_WINDOW_DAYS`
  (default 3, meaning "the meeting moved"); or same title on an open task with no deadline.
  The 3-day window is chosen so it cannot collide with weekly (7) or bi-weekly (14) recurring
  sessions. Comparison is case-insensitive and treats `-`, `–`, `—` as equivalent.
- The tool only ever **creates**. It never edits, moves, or completes existing tasks — a moved
  meeting is reported as a skip for a human to resolve.
- Google credentials are read under `GOOGLE_OAUTH_*` **or** legacy `GCAL_*` aliases, so an
  existing credential in `~/.env` need not be duplicated.

## CI

`.github/workflows/ci.yml` runs compile + unittest on the 3.9/3.12 matrix. The three `claude*`
workflows are the shared automation template: `claude-label.yml` (label-triggered, write perms,
can open PRs) and `claude.yml` / `claude-code-review.yml` (`@claude` mention + auto PR review,
read-only). All authenticate with the `CLAUDE_CODE_OAUTH_TOKEN` secret.
