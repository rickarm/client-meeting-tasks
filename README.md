# client-meeting-tasks

Creates **Things 3** prep/notes tasks for upcoming client meetings, by cross-referencing
**Google Calendar** (meetings) with **Airtable** (who counts as an active client).

For every client meeting in the target date range it creates two to-dos in the
configured Things project (default **Client Ops**):

| Task | Deadline |
|------|----------|
| `Prep - <first name> (<Ddd>)` | the day **before** the meeting |
| `Notes - <first name> (<Ddd>)` | the day **of** the meeting |

`<Ddd>` is the weekday of the **meeting**, and both tasks in a pair carry the same
one — a Wednesday meeting produces `Prep - Marissa (Wed)` due Tuesday and
`Notes - Marissa (Wed)` due Wednesday. This matches the convention already used
in the project; the suffix is part of the title the duplicate check compares, so
changing its shape would make every run create a second pair alongside the
existing tasks.

It reads the existing to-dos in the project first and never creates duplicates.

Single stdlib-only Python file, made to be wrapped by an agent (OpenClaw, Claude, a
skill, launchd, …): predictable CLI, `--dry-run`, `--json` output, meaningful exit codes.

## Requirements

- macOS with Things 3 installed (see [Things access](#things-access) below)
- Python 3.9+ (the system `/usr/bin/python3` is fine — no packages to install)
- A Google OAuth client + refresh token with `calendar.readonly` scope (setup below)
- An Airtable personal access token with read access to your clients base

## Configuration

All configuration comes from environment variables. The tool also reads `~/.env` and
`./.env` (simple `KEY=VALUE` lines; process environment wins). See `.env.example`.

| Variable | Required | Default | Meaning |
|----------|----------|---------|---------|
| `GOOGLE_OAUTH_CLIENT_ID` | yes | — | OAuth client id (Desktop app type). Alias: `GCAL_CLIENT_ID` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | yes | — | OAuth client secret. Alias: `GCAL_CLIENT_SECRET` |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | yes | — | From the one-time `auth` subcommand. Alias: `GCAL_REFRESH_TOKEN` |
| `AIRTABLE_API_KEY` | yes | — | Airtable personal access token |
| `CMT_AIRTABLE_BASE_ID` | yes | — | Base id (`app…`) holding the contacts table |
| `CMT_GOOGLE_CALENDAR_ID` | no | `primary` | Calendar to scan |
| `CMT_AIRTABLE_CONTACTS_TABLE` | no | `Contacts` | Table with client contacts |
| `CMT_AIRTABLE_EMAIL_FIELD` | no | `Email` | Primary email field |
| `CMT_AIRTABLE_ALT_EMAIL_FIELD` | no | `Email (other addresses)` | Extra email field |
| `CMT_AIRTABLE_NAME_FIELD` | no | `Name` | Client full-name field |
| `CMT_AIRTABLE_STATUS_FIELD` | no | `Status Client` | Status (single select) field |
| `CMT_ACTIVE_STATUSES` | no | `Active-coaching,Active-advising` | Comma-separated statuses that count as an active client |
| `CMT_THINGS_PROJECT` | no | `Client Ops` | Things project that receives the tasks |
| `CMT_TIMEZONE` | no | `America/Los_Angeles` | Timezone for **all** date math |
| `CMT_DEDUP_WINDOW_DAYS` | no | `3` | Duplicate window, see below |

### One-time Google setup

Skip this entirely if you already have a Google OAuth client and refresh token with
the `calendar.readonly` scope in `~/.env` under `GCAL_CLIENT_ID` /
`GCAL_CLIENT_SECRET` / `GCAL_REFRESH_TOKEN` — those names are read as aliases, so
the credential does not need to be duplicated under a second name.

1. In [Google Cloud Console](https://console.cloud.google.com/), create (or reuse) a
   project, enable the **Google Calendar API**, and create an OAuth client of type
   **Desktop app**. Put its id/secret in `~/.env` as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET`.
2. On the machine that will run the tool: `./client_meeting_tasks.py auth`
   — approve in the browser, then copy the printed `GOOGLE_OAUTH_REFRESH_TOKEN=…`
   line into `~/.env`.

## Usage

```bash
./client_meeting_tasks.py run                 # default range (see below)
./client_meeting_tasks.py run --dry-run       # show what would be created
./client_meeting_tasks.py run --json          # machine-readable result on stdout
./client_meeting_tasks.py run --start 2026-08-10 --end 2026-08-14   # explicit range

./client_meeting_tasks.py range               # print the default range for today
./client_meeting_tasks.py range --date 2026-08-07   # …for a pretend "today"
./client_meeting_tasks.py clients             # list active clients from Airtable
```

Exit codes: `0` success, `1` configuration error, `2` Google/Airtable API error,
`3` Things error (also used when some creates failed).

## Things access

Reads go straight to the Things 3 sqlite database (read-only); writes go through
the `things:///add` URL scheme. **Not AppleScript** — `osascript` needs an Apple
Events "Automation" grant, and macOS attributes that grant to the *terminal app*
driving the script, so it can never be granted for a `launchd` run, where there is
no terminal in the process ancestry. The sqlite + URL-scheme path needs no TCC
grant beyond the Full Disk Access the runner already needs, so the same code works
interactively and headless.

Because the URL scheme is fire-and-forget, every create is confirmed by re-reading
the database (up to 20s) before being reported as created; a task that never shows
up is reported in `errors`, not `created`.

`CMT_THINGS_PROJECT` must match the project title **exactly**, including any emoji
(e.g. `🌱 Client Ops`, not `Client Ops`). A wrong name exits `3` and suggests near
matches. To-dos filed under a heading inside the project are included in the
duplicate check.

## Date semantics (read this before trusting it)

All date math happens in `CMT_TIMEZONE` — never UTC, never the server's local time.
The default range depends on the weekday of "today" in that timezone:

| Run on | Range |
|--------|-------|
| Mon–Thu | today → Friday of the **current** week |
| Fri, Sat, Sun | Monday → Friday of the **next** week |

Both endpoints inclusive. `--start`/`--end` (both required together) override this
entirely — that is the runtime input for the timeframe.

Deadline details, chosen deliberately:

- A meeting's **date** is its start time converted to `CMT_TIMEZONE` (a Zoom call at
  02:30 UTC Thursday is a Wednesday-evening meeting in California, and is treated
  as Wednesday).
- `Prep` deadline = calendar day before the meeting (a Monday meeting gets a Sunday
  prep deadline).
- If the meeting is **today**, the prep deadline is clamped to today rather than
  created already-overdue.
- The tool never edits or moves existing tasks — it only creates and reports.

## What counts as a "client meeting"

An event in the range where **at least one attendee's email** belongs to a contact
whose status is in `CMT_ACTIVE_STATUSES`. Title matching is deliberately not used
(too many false positives — holds, mentions, similarly-named people). Additionally
skipped: cancelled events, declined-by-you events, and non-default event types
(focus time, OOO, working location, birthdays).

If two *different* active clients share a first name in the same run, titles are
disambiguated with the last-name initial (`Prep - Brett L. (Tue)` /
`Prep - Brett T. (Wed)`).

## Duplicate rules

Existing to-dos in the Things project (any status, including completed) block
creation when:

1. same title **and** same deadline; or
2. same title and a deadline within `CMT_DEDUP_WINDOW_DAYS` days (default 3) —
   this catches a meeting that moved by a day or two after tasks were created.
   The skip reason says so; adjust the existing task's deadline manually if the
   meeting really moved. Set `CMT_DEDUP_WINDOW_DAYS=0` if a client genuinely has
   multiple meetings per week and you want a pair per meeting; or
3. same title on an **open** task with no deadline (a manually created task).

Title comparison is case-insensitive and treats `-`, `–`, `—` as equivalent.
The 3-day window intentionally does **not** collide with weekly (7-day) or
bi-weekly (14-day) recurring sessions.

## Wrapping it in an agent skill

The agent surface is small on purpose. A minimal skill only needs:

```markdown
When Rick asks to set up client meeting prep tasks:

1. Optional timeframe: if he named dates, pass `--start YYYY-MM-DD --end YYYY-MM-DD`
   (inclusive); otherwise pass nothing — the tool applies his weekday rules itself.
2. Run: `~/scripts/../client-meeting-tasks/client_meeting_tasks.py run --json`
3. Report from the JSON: `meetings` found, `created`, `skipped` (with reasons),
   `errors`. A skip mentioning "meeting may have moved" deserves a callout so the
   existing task's deadline can be fixed.
Never create Things tasks directly for this — always go through the tool.
```

`run --dry-run --json` is safe to call any time (creates nothing) if the agent wants
to preview before committing.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers the weekday range rules (including month/year boundaries), deadline
clamping, timezone conversion of event starts, name disambiguation, duplicate
detection, and env-file parsing. No network or Things access needed.

## License

MIT — see `LICENSE`. This repo contains no credentials or client data; everything
sensitive stays in `~/.env` on the machine that runs it.
