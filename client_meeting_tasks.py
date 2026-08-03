#!/usr/bin/env python3
"""client-meeting-tasks — create Things prep/notes tasks for upcoming client meetings.

Pipeline:
  1. Fetch active clients from Airtable (Contacts table: name + email addresses).
  2. Fetch calendar events for the target date range from the Google Calendar API.
  3. A "client meeting" = an event with at least one attendee whose email belongs
     to an active client. (Email matching only — title matching is too fuzzy.)
  4. For each meeting, plan two Things tasks in the configured project:
        "Prep - <first name>"   deadline = day before the meeting
        "Notes - <first name>"  deadline = day of the meeting
  5. Read existing to-dos in the Things project and skip anything that already
     exists (see duplicate rules below), then create the rest.

Things access is read-via-sqlite / write-via-things:/// URL scheme, not
AppleScript: an Apple Events grant is attributed to the controlling terminal
app, so it cannot be granted under launchd. See the Things section below.

Date range semantics (all dates in the configured timezone):
  * No --start/--end given:
      - Mon-Thu: today through Friday of the current week.
      - Fri/Sat/Sun: next Monday through next Friday ("the upcoming week").
  * --start/--end (YYYY-MM-DD, inclusive) override the default entirely.

Duplicate rules (against existing to-dos in the project, any status):
  * Same title + same deadline -> duplicate.
  * Same title + deadline within CMT_DEDUP_WINDOW_DAYS days -> duplicate
    (catches meetings that moved by a day or two; the tool never edits
    existing tasks, it just reports the mismatch).
  * Same title, open, no deadline -> duplicate (manually created task).

Designed to be wrapped by an agent skill: stdlib-only, single file, --json
output, non-zero exit codes on failure. See README.md.

Subcommands: run, range, clients, auth.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
AIRTABLE_URL = "https://api.airtable.com/v0/{base}/{table}"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_API = 2
EXIT_THINGS = 3


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    calendar_id: str = "primary"
    airtable_api_key: str = ""
    airtable_base_id: str = ""
    contacts_table: str = "Contacts"
    email_field: str = "Email"
    alt_email_field: str = "Email (other addresses)"
    name_field: str = "Name"
    status_field: str = "Status Client"
    active_statuses: tuple = ("Active-coaching", "Active-advising")
    things_project: str = "Client Ops"
    timezone: str = "America/Los_Angeles"
    dedup_window_days: int = 3

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def parse_env_file(path: str) -> dict:
    """Parse a simple KEY=VALUE env file (supports `export`, quotes, comments)."""
    result = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                if key:
                    result[key] = value
    except OSError:
        pass
    return result


def load_config(env_files=None) -> Config:
    """Build config from process env, with ~/.env and ./.env as fallbacks."""
    env = {}
    files = env_files if env_files is not None else [
        os.path.expanduser("~/.env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in files:
        env.update(parse_env_file(path))
    env.update(os.environ)  # process env wins

    def get(key, default=""):
        return env.get(key, default) or default

    statuses = tuple(
        s.strip() for s in get("CMT_ACTIVE_STATUSES", "Active-coaching,Active-advising").split(",")
        if s.strip()
    )
    return Config(
        google_client_id=get("GOOGLE_OAUTH_CLIENT_ID"),
        google_client_secret=get("GOOGLE_OAUTH_CLIENT_SECRET"),
        google_refresh_token=get("GOOGLE_OAUTH_REFRESH_TOKEN"),
        calendar_id=get("CMT_GOOGLE_CALENDAR_ID", "primary"),
        airtable_api_key=get("AIRTABLE_API_KEY"),
        airtable_base_id=get("CMT_AIRTABLE_BASE_ID"),
        contacts_table=get("CMT_AIRTABLE_CONTACTS_TABLE", "Contacts"),
        email_field=get("CMT_AIRTABLE_EMAIL_FIELD", "Email"),
        alt_email_field=get("CMT_AIRTABLE_ALT_EMAIL_FIELD", "Email (other addresses)"),
        name_field=get("CMT_AIRTABLE_NAME_FIELD", "Name"),
        status_field=get("CMT_AIRTABLE_STATUS_FIELD", "Status Client"),
        active_statuses=statuses,
        things_project=get("CMT_THINGS_PROJECT", "Client Ops"),
        timezone=get("CMT_TIMEZONE", "America/Los_Angeles"),
        dedup_window_days=int(get("CMT_DEDUP_WINDOW_DAYS", "3")),
    )


# ---------------------------------------------------------------------------
# Date logic
# ---------------------------------------------------------------------------

def compute_default_range(today: date) -> tuple:
    """Default date range for a run, per Rick's semantics.

    Mon-Thu -> (today, Friday of this week): "current day and the rest of
    the current week days".
    Fri/Sat/Sun -> (next Monday, next Friday): "the upcoming week".
    Both endpoints inclusive.
    """
    weekday = today.weekday()  # Mon=0 .. Sun=6
    if weekday <= 3:  # Mon-Thu
        start = today
        end = today + timedelta(days=4 - weekday)  # this week's Friday
    else:  # Fri, Sat, Sun
        start = today + timedelta(days=7 - weekday)  # next Monday
        end = start + timedelta(days=4)  # next Friday
    return start, end


def clamp_deadline(deadline: date, meeting_date: date, today: date) -> date:
    """Never create an already-overdue deadline for a meeting that hasn't
    happened yet (e.g. a same-day meeting would get a prep deadline of
    yesterday). Deliberately-backdated runs (meeting itself in the past)
    are left untouched."""
    if deadline < today <= meeting_date:
        return today
    return deadline


# ---------------------------------------------------------------------------
# Names / titles
# ---------------------------------------------------------------------------

def first_name(full_name: str) -> str:
    parts = full_name.strip().split()
    return parts[0] if parts else full_name.strip()


def display_names(full_names) -> dict:
    """Map full name -> display name for task titles. Uses the first name;
    when two distinct clients share a first name, disambiguates with the
    last-name initial ("Brett L.")."""
    by_first = {}
    for name in full_names:
        by_first.setdefault(first_name(name).casefold(), set()).add(name)
    result = {}
    for name in full_names:
        collisions = by_first[first_name(name).casefold()]
        if len(collisions) == 1:
            result[name] = first_name(name)
        else:
            parts = name.strip().split()
            if len(parts) > 1:
                result[name] = f"{parts[0]} {parts[-1][0]}."
            else:
                result[name] = name.strip()
    return result


_DASHES = re.compile(r"[‐-―]")  # hyphen/en/em dash variants
_WS = re.compile(r"\s+")


def norm_title(title: str) -> str:
    return _WS.sub(" ", _DASHES.sub("-", title)).strip().casefold()


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only)
# ---------------------------------------------------------------------------

def http_json(url: str, headers=None, data=None, method="GET", retries=3):
    """GET/POST returning parsed JSON. Retries on network errors and 5xx."""
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")[:500]
            if err.code >= 500 and attempt < retries - 1:
                last_err = f"HTTP {err.code}: {body}"
            else:
                raise RuntimeError(f"HTTP {err.code} from {url.split('?')[0]}: {body}") from err
        except (urllib.error.URLError, TimeoutError) as err:
            last_err = str(err)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Request to {url.split('?')[0]} failed after {retries} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Airtable: active clients
# ---------------------------------------------------------------------------

@dataclass
class Client:
    name: str
    emails: set = field(default_factory=set)
    status: str = ""


def fetch_active_clients(cfg: Config) -> list:
    if not cfg.airtable_api_key:
        raise SystemExit2(EXIT_CONFIG, "AIRTABLE_API_KEY is not set")
    if not cfg.airtable_base_id:
        raise SystemExit2(EXIT_CONFIG, "CMT_AIRTABLE_BASE_ID is not set")

    url_base = AIRTABLE_URL.format(
        base=cfg.airtable_base_id, table=urllib.parse.quote(cfg.contacts_table)
    )
    headers = {"Authorization": f"Bearer {cfg.airtable_api_key}"}
    params = [
        ("pageSize", "100"),
        ("fields[]", cfg.email_field),
        ("fields[]", cfg.alt_email_field),
        ("fields[]", cfg.name_field),
        ("fields[]", cfg.status_field),
    ]
    records, offset = [], None
    while True:
        query = params + ([("offset", offset)] if offset else [])
        data = http_json(url_base + "?" + urllib.parse.urlencode(query), headers=headers)
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break

    active = {s.casefold() for s in cfg.active_statuses}
    clients = {}  # full name -> Client (a client can have several contact rows)
    for rec in records:
        f = rec.get("fields", {})
        status = f.get(cfg.status_field) or ""
        if isinstance(status, dict):  # select fields can come back as objects
            status = status.get("name", "")
        if status.casefold() not in active:
            continue
        name = (f.get(cfg.name_field) or "").strip()
        if not name:
            continue
        client = clients.setdefault(name, Client(name=name, status=status))
        for key in (cfg.email_field, cfg.alt_email_field):
            value = f.get(key) or ""
            for email in re.split(r"[,;\s]+", value):
                if "@" in email:
                    client.emails.add(email.strip().casefold())
    return list(clients.values())


# ---------------------------------------------------------------------------
# Google Calendar: events in range
# ---------------------------------------------------------------------------

@dataclass
class Meeting:
    client: Client
    summary: str
    meeting_date: date
    start_display: str  # e.g. "Wed 2026-08-05 11:00"
    event_id: str


def google_access_token(cfg: Config) -> str:
    for key, value in (
        ("GOOGLE_OAUTH_CLIENT_ID", cfg.google_client_id),
        ("GOOGLE_OAUTH_CLIENT_SECRET", cfg.google_client_secret),
        ("GOOGLE_OAUTH_REFRESH_TOKEN", cfg.google_refresh_token),
    ):
        if not value:
            raise SystemExit2(EXIT_CONFIG, f"{key} is not set (run the `auth` subcommand for the refresh token)")
    body = urllib.parse.urlencode({
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "refresh_token": cfg.google_refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    data = http_json(GOOGLE_TOKEN_URL, method="POST", data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    token = data.get("access_token")
    if not token:
        raise SystemExit2(EXIT_API, f"Google token refresh failed: {data}")
    return token


def parse_event_start(event: dict, tz: ZoneInfo):
    """Return (meeting_date, display_string) in the configured timezone."""
    start = event.get("start", {})
    if start.get("dateTime"):
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(tz)
        return dt.date(), dt.strftime("%a %Y-%m-%d %H:%M")
    if start.get("date"):
        d = date.fromisoformat(start["date"])
        return d, d.strftime("%a %Y-%m-%d (all day)")
    return None, None


def fetch_events(cfg: Config, start: date, end: date) -> list:
    token = google_access_token(cfg)
    tz = cfg.tz
    time_min = datetime.combine(start, datetime.min.time(), tzinfo=tz).isoformat()
    time_max = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz).isoformat()
    url_base = GOOGLE_EVENTS_URL.format(cal=urllib.parse.quote(cfg.calendar_id))
    headers = {"Authorization": f"Bearer {token}"}
    events, page_token = [], None
    while True:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        }
        if page_token:
            params["pageToken"] = page_token
        data = http_json(url_base + "?" + urllib.parse.urlencode(params), headers=headers)
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


def event_is_relevant(event: dict) -> bool:
    if event.get("status") == "cancelled":
        return False
    if event.get("eventType", "default") != "default":
        return False  # focusTime / outOfOffice / workingLocation / birthday / fromGmail
    for attendee in event.get("attendees", []):
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return False  # Rick declined it
    return True


def find_client_meetings(events, clients, start: date, end: date, tz: ZoneInfo) -> list:
    by_email = {}
    for client in clients:
        for email in client.emails:
            by_email[email] = client
    meetings = []
    for event in events:
        if not event_is_relevant(event):
            continue
        meeting_date, display = parse_event_start(event, tz)
        if meeting_date is None or not (start <= meeting_date <= end):
            continue
        matched = {}
        for attendee in event.get("attendees", []):
            client = by_email.get((attendee.get("email") or "").casefold())
            if client:
                matched[client.name] = client
        for client in matched.values():
            meetings.append(Meeting(
                client=client,
                summary=event.get("summary", "(no title)"),
                meeting_date=meeting_date,
                start_display=display,
                event_id=event.get("id", ""),
            ))
    meetings.sort(key=lambda m: (m.meeting_date, m.client.name))
    return meetings


# ---------------------------------------------------------------------------
# Things (sqlite read + things:/// URL scheme write)
#
# Deliberately NOT AppleScript. osascript needs an Apple Events "Automation"
# grant, and macOS attributes that grant to the *terminal app* driving the
# script — so it cannot be granted for a launchd run, where there is no
# terminal in the process ancestry. Reading the sqlite database directly and
# writing through the URL scheme needs neither, so the same code path works
# interactively and headless.
# ---------------------------------------------------------------------------

THINGS_DB_GLOB = ("~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/"
                  "ThingsData-*/Things Database.thingsdatabase/main.sqlite")

# TMTask.type / TMTask.status as stored by Things 3.
THINGS_TYPE_TODO = 0
THINGS_TYPE_PROJECT = 1
THINGS_STATUS = {0: "open", 2: "canceled", 3: "completed"}

# things:/// is fire-and-forget, so creates are confirmed by re-reading the DB.
CREATE_POLL_TIMEOUT = 20.0
CREATE_POLL_INTERVAL = 0.3


def decode_things_date(value) -> Optional[date]:
    """Things packs deadline/startDate into an integer — NOT a unix timestamp.

    Layout: bits 16+ = year, bits 12-15 = month, bits 7-11 = day.
    """
    if not value:
        return None
    year, month, day = value >> 16, (value >> 12) & 0xF, (value >> 7) & 0x1F
    try:
        return date(year, month, day)
    except ValueError:
        return None


def things_db_path() -> str:
    matches = sorted(glob.glob(os.path.expanduser(THINGS_DB_GLOB)))
    if not matches:
        raise SystemExit2(EXIT_THINGS,
                          "Things 3 database not found — is Things 3 installed for this user?")
    return matches[-1]


def things_connect():
    """Read-only connection. Never writes: creation goes through things:///."""
    uri = "file:" + urllib.parse.quote(things_db_path()) + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as err:
        raise SystemExit2(EXIT_THINGS, f"cannot open the Things database: {err}")


def find_project_uuid(con, name: str) -> str:
    rows = con.execute(
        "SELECT uuid, status FROM TMTask WHERE type = ? AND trashed = 0 AND title = ?",
        (THINGS_TYPE_PROJECT, name),
    ).fetchall()
    active = [uuid for uuid, status in rows if status == 0]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise SystemExit2(EXIT_THINGS,
                          f"more than one active Things project is named {name!r} — "
                          "rename one, or set CMT_THINGS_PROJECT to an unambiguous name")
    if rows:
        raise SystemExit2(EXIT_THINGS,
                          f"Things project {name!r} exists but is completed or canceled")
    hint = ""
    needle = norm_title(name)
    near = [t for (t,) in con.execute(
        "SELECT title FROM TMTask WHERE type = ? AND trashed = 0 AND status = 0",
        (THINGS_TYPE_PROJECT,)).fetchall()
        if t and (needle in norm_title(t) or norm_title(t) in needle)]
    if near:
        hint = " — did you mean: " + ", ".join(repr(t) for t in sorted(near)[:5]) + "?"
    raise SystemExit2(EXIT_THINGS,
                      f"Things project {name!r} not found (set CMT_THINGS_PROJECT){hint}")


def _project_todo_rows(con, project_uuid: str):
    """To-dos directly in the project OR nested under one of its headings.

    A to-do filed under a heading has project = NULL and heading = <heading uuid>,
    so the join is required — without it such tasks are invisible to dedup and
    the tool would create duplicates alongside them.
    """
    return con.execute(
        "SELECT t.uuid, t.title, t.deadline, t.status "
        "FROM TMTask t LEFT JOIN TMTask h ON t.heading = h.uuid "
        "WHERE t.type = ? AND t.trashed = 0 "
        "AND (t.project = ? OR h.project = ?)",
        (THINGS_TYPE_TODO, project_uuid, project_uuid),
    ).fetchall()


@dataclass
class ExistingTodo:
    title: str
    deadline: Optional[date]
    status: str  # open / completed / canceled


def read_existing_todos(cfg: Config) -> list:
    con = things_connect()
    try:
        project_uuid = find_project_uuid(con, cfg.things_project)
        return [
            ExistingTodo(title=title or "",
                         deadline=decode_things_date(deadline),
                         status=THINGS_STATUS.get(status, "open"))
            for _uuid, title, deadline, status in _project_todo_rows(con, project_uuid)
        ]
    finally:
        con.close()


def _matching_uuids(project_uuid: str, title: str, deadline: date) -> set:
    """UUIDs of non-trashed to-dos in the project with exactly this title+deadline."""
    con = things_connect()
    try:
        return {
            uuid for uuid, existing_title, existing_deadline, _status
            in _project_todo_rows(con, project_uuid)
            if (existing_title or "") == title and decode_things_date(existing_deadline) == deadline
        }
    finally:
        con.close()


def create_todo(cfg: Config, title: str, deadline: date, notes: str) -> str:
    """Create one to-do via things:/// and confirm it landed, returning its uuid.

    The URL scheme returns nothing, so "did it work?" is answered by diffing the
    project's to-dos before and after rather than by trusting the open(1) exit
    code — which only reports that the URL was handed off.
    """
    con = things_connect()
    try:
        project_uuid = find_project_uuid(con, cfg.things_project)
    finally:
        con.close()
    before = _matching_uuids(project_uuid, title, deadline)

    url = "things:///add?" + urllib.parse.urlencode({
        "title": title,
        "notes": notes,
        "deadline": deadline.isoformat(),
        "list-id": project_uuid,   # id, not name — immune to emoji/renaming
    }, quote_via=urllib.parse.quote)
    try:
        proc = subprocess.run(["open", "-g", url], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SystemExit2(EXIT_THINGS, "open(1) not found — this tool must run on macOS")
    except subprocess.TimeoutExpired:
        raise SystemExit2(EXIT_THINGS, f"timed out handing {title!r} to Things via things:///")
    if proc.returncode != 0:
        raise SystemExit2(EXIT_THINGS,
                          f"things:/// URL rejected for {title!r}: {proc.stderr.strip()}")

    give_up_at = time.monotonic() + CREATE_POLL_TIMEOUT
    while time.monotonic() < give_up_at:
        time.sleep(CREATE_POLL_INTERVAL)
        appeared = _matching_uuids(project_uuid, title, deadline) - before
        if appeared:
            return sorted(appeared)[0]
    raise SystemExit2(EXIT_THINGS,
                      f"handed {title!r} to Things but it did not appear in the database "
                      f"within {CREATE_POLL_TIMEOUT:.0f}s — is Things 3 running?")


# ---------------------------------------------------------------------------
# Task planning + dedup
# ---------------------------------------------------------------------------

# Hardcoded rather than strftime("%a") so a launchd run under a different
# locale cannot produce differently-spelled titles (which would defeat dedup).
WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def task_title(kind: str, display: str, meeting_date: date) -> str:
    """"Prep - Marissa (Wed)" — the suffix is the weekday of the MEETING.

    Both tasks in a pair carry the same suffix, so a Wednesday meeting yields
    "Prep - Marissa (Wed)" (due Tuesday) and "Notes - Marissa (Wed)" (due
    Wednesday). This matches the convention already used in the Things project;
    diverging from it would mean the dedup check never matches the tasks that
    are already there, and every run would create a second pair.
    """
    return f"{'Prep' if kind == 'prep' else 'Notes'} - {display} ({WEEKDAY_ABBR[meeting_date.weekday()]})"


@dataclass
class PlannedTask:
    title: str
    deadline: date
    kind: str  # "prep" | "notes"
    meeting: Meeting
    notes: str


def plan_tasks(meetings, today: date) -> list:
    names = display_names({m.client.name for m in meetings})
    planned, seen = [], set()
    for meeting in meetings:
        display = names[meeting.client.name]
        stamp = (f"Auto-created by client-meeting-tasks — meeting: {meeting.start_display} "
                 f"with {meeting.client.name} ({meeting.summary})")
        for kind, deadline in (
            ("prep", clamp_deadline(meeting.meeting_date - timedelta(days=1), meeting.meeting_date, today)),
            ("notes", meeting.meeting_date),
        ):
            title = task_title(kind, display, meeting.meeting_date)
            key = (norm_title(title), deadline)
            if key in seen:  # same client, same day, two events
                continue
            seen.add(key)
            planned.append(PlannedTask(title=title, deadline=deadline, kind=kind,
                                       meeting=meeting, notes=stamp))
    return planned


def find_duplicate(task_title: str, task_deadline: date, existing, window_days: int):
    """Return (existing_todo, reason) if the planned task already exists."""
    wanted = norm_title(task_title)
    for todo in existing:
        if norm_title(todo.title) != wanted:
            continue
        if todo.deadline is None:
            if todo.status == "open":
                return todo, "open task with same title (no deadline)"
            continue
        delta = abs((todo.deadline - task_deadline).days)
        if delta == 0:
            return todo, "same title and deadline"
        if delta <= window_days:
            return todo, (f"same title, deadline {todo.deadline.isoformat()} within "
                          f"{window_days}d of {task_deadline.isoformat()} — meeting may have moved; "
                          f"adjust the existing task manually if so")
    return None, None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

class SystemExit2(Exception):
    """Exit with a code and message (kept out of stdout so --json stays clean)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def cmd_range(cfg: Config, args) -> int:
    today = date.fromisoformat(args.date) if args.date else datetime.now(cfg.tz).date()
    start, end = compute_default_range(today)
    print(f"today: {today.strftime('%a %Y-%m-%d')} ({cfg.timezone})")
    print(f"range: {start.strftime('%a %Y-%m-%d')} -> {end.strftime('%a %Y-%m-%d')} (inclusive)")
    return EXIT_OK


def cmd_clients(cfg: Config, args) -> int:
    clients = fetch_active_clients(cfg)
    for client in sorted(clients, key=lambda c: c.name):
        print(f"{client.name}  [{client.status}]  {', '.join(sorted(client.emails))}")
    print(f"({len(clients)} active clients)")
    return EXIT_OK


def resolve_range(cfg: Config, args):
    today = datetime.now(cfg.tz).date()
    if bool(args.start) != bool(args.end):
        raise SystemExit2(EXIT_CONFIG, "--start and --end must be given together")
    if args.start:
        start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
        if end < start:
            raise SystemExit2(EXIT_CONFIG, f"--end {end} is before --start {start}")
    else:
        start, end = compute_default_range(today)
    return today, start, end


def cmd_run(cfg: Config, args) -> int:
    today, start, end = resolve_range(cfg, args)
    log = (lambda msg: print(msg, file=sys.stderr)) if (args.verbose or not args.json) else (lambda msg: None)
    log(f"Range: {start.strftime('%a %Y-%m-%d')} -> {end.strftime('%a %Y-%m-%d')} ({cfg.timezone})")

    clients = fetch_active_clients(cfg)
    log(f"Active clients: {len(clients)}")
    events = fetch_events(cfg, start, end)
    meetings = find_client_meetings(events, clients, start, end, cfg.tz)
    log(f"Client meetings in range: {len(meetings)}")
    for meeting in meetings:
        log(f"  * {meeting.client.name} — {meeting.start_display} — {meeting.summary}")

    existing = read_existing_todos(cfg)
    log(f"Existing to-dos in '{cfg.things_project}': {len(existing)}")

    planned = plan_tasks(meetings, today)
    created, skipped, errors = [], [], []
    for task in planned:
        dup, reason = find_duplicate(task.title, task.deadline, existing, cfg.dedup_window_days)
        if dup:
            skipped.append({
                "title": task.title,
                "deadline": task.deadline.isoformat(),
                "reason": reason,
                "existing_deadline": dup.deadline.isoformat() if dup.deadline else None,
                "existing_status": dup.status,
            })
            log(f"  - skipped \"{task.title}\" ({task.deadline.isoformat()}): {reason}")
            continue
        if args.dry_run:
            created.append({"title": task.title, "deadline": task.deadline.isoformat(),
                            "client": task.meeting.client.name, "dry_run": True})
            log(f"  + would create \"{task.title}\" (deadline {task.deadline.isoformat()})")
            continue
        try:
            todo_id = create_todo(cfg, task.title, task.deadline, task.notes)
            created.append({"title": task.title, "deadline": task.deadline.isoformat(),
                            "client": task.meeting.client.name, "things_id": todo_id})
            log(f"  + created \"{task.title}\" (deadline {task.deadline.isoformat()})")
        except SystemExit2 as err:
            errors.append({"title": task.title, "error": err.message})
            log(f"  ! failed \"{task.title}\": {err.message}")

    result = {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "timezone": cfg.timezone},
        "dry_run": bool(args.dry_run),
        "meetings": [
            {"client": m.client.name, "date": m.meeting_date.isoformat(),
             "start": m.start_display, "summary": m.summary}
            for m in meetings
        ],
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        verb = "would create" if args.dry_run else "created"
        print(f"Done: {len(meetings)} client meeting(s), {verb} {len(created)} task(s), "
              f"skipped {len(skipped)} duplicate(s), {len(errors)} error(s).")
    return EXIT_THINGS if errors else EXIT_OK


def cmd_auth(cfg: Config, args) -> int:
    """One-time interactive OAuth loopback flow to obtain a Google refresh token."""
    import http.server
    import secrets
    import webbrowser

    if not cfg.google_client_id or not cfg.google_client_secret:
        raise SystemExit2(EXIT_CONFIG, "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first (see README)")
    port = args.port
    redirect_uri = f"http://localhost:{port}/"
    state = secrets.token_urlsafe(16)
    auth_url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cfg.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_CAL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    print("Open this URL in a browser (on this machine) and approve access:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)

    code_holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            if query.get("state", [""])[0] != state:
                self.wfile.write(b"State mismatch - ignore this window and retry.")
                return
            code_holder["code"] = query.get("code", [""])[0]
            self.wfile.write(b"Authorized. You can close this window.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", port), Handler)
    print(f"Waiting for the OAuth redirect on {redirect_uri} ...")
    while "code" not in code_holder:
        server.handle_request()
    server.server_close()
    if not code_holder["code"]:
        raise SystemExit2(EXIT_API, "No authorization code received")

    body = urllib.parse.urlencode({
        "client_id": cfg.google_client_id,
        "client_secret": cfg.google_client_secret,
        "code": code_holder["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    data = http_json(GOOGLE_TOKEN_URL, method="POST", data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    refresh = data.get("refresh_token")
    if not refresh:
        raise SystemExit2(EXIT_API, f"No refresh token in response: {data}")
    print("\nSuccess. Add this to ~/.env :\n")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client-meeting-tasks",
        description="Create Things prep/notes tasks for upcoming client meetings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="find client meetings and create Things tasks")
    run.add_argument("--start", metavar="YYYY-MM-DD", help="range start (inclusive); requires --end")
    run.add_argument("--end", metavar="YYYY-MM-DD", help="range end (inclusive); requires --start")
    run.add_argument("--dry-run", action="store_true", help="report what would be created, create nothing")
    run.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    run.add_argument("--verbose", action="store_true", help="progress detail on stderr")

    rng = sub.add_parser("range", help="print the default date range (date-logic sanity check)")
    rng.add_argument("--date", metavar="YYYY-MM-DD", help="pretend today is this date")

    sub.add_parser("clients", help="list active clients from Airtable")

    auth = sub.add_parser("auth", help="one-time Google OAuth flow to get a refresh token")
    auth.add_argument("--port", type=int, default=8321, help="loopback port (default 8321)")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        handler = {"run": cmd_run, "range": cmd_range, "clients": cmd_clients, "auth": cmd_auth}[args.command]
        return handler(cfg, args)
    except SystemExit2 as err:
        print(f"error: {err.message}", file=sys.stderr)
        return err.code
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_API


if __name__ == "__main__":
    sys.exit(main())
