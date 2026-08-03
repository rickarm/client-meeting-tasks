"""Unit tests for the pure logic in client_meeting_tasks (no network, no Things).

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client_meeting_tasks as cmt


class TestDefaultRange(unittest.TestCase):
    """2026-08-03 is a Monday; every weekday of that week is exercised."""

    def test_monday(self):
        # Mon -> today through this Friday
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 3)),
                         (date(2026, 8, 3), date(2026, 8, 7)))

    def test_tuesday(self):
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 4)),
                         (date(2026, 8, 4), date(2026, 8, 7)))

    def test_wednesday(self):
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 5)),
                         (date(2026, 8, 5), date(2026, 8, 7)))

    def test_thursday(self):
        # Thu -> just Thu and Fri left
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 6)),
                         (date(2026, 8, 6), date(2026, 8, 7)))

    def test_friday_means_next_week(self):
        # Fri -> next Mon through next Fri
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 7)),
                         (date(2026, 8, 10), date(2026, 8, 14)))

    def test_saturday_means_next_week(self):
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 8)),
                         (date(2026, 8, 10), date(2026, 8, 14)))

    def test_sunday_means_next_week(self):
        self.assertEqual(cmt.compute_default_range(date(2026, 8, 9)),
                         (date(2026, 8, 10), date(2026, 8, 14)))

    def test_month_boundary(self):
        # Fri 2026-07-31 -> next week crosses into August
        self.assertEqual(cmt.compute_default_range(date(2026, 7, 31)),
                         (date(2026, 8, 3), date(2026, 8, 7)))

    def test_year_boundary(self):
        # Thu 2026-12-31 -> through Fri 2027-01-01
        self.assertEqual(cmt.compute_default_range(date(2026, 12, 31)),
                         (date(2026, 12, 31), date(2027, 1, 1)))


class TestClampDeadline(unittest.TestCase):
    def test_normal_future_meeting_untouched(self):
        # Meeting Wed, prep deadline Tue, today Mon -> no clamp
        self.assertEqual(cmt.clamp_deadline(date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 3)),
                         date(2026, 8, 4))

    def test_same_day_meeting_clamps_prep_to_today(self):
        # Meeting today -> prep deadline would be yesterday -> clamp to today
        self.assertEqual(cmt.clamp_deadline(date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 3)),
                         date(2026, 8, 3))

    def test_deliberate_backdated_run_untouched(self):
        # Meeting itself in the past (explicit --start/--end): leave dates alone
        self.assertEqual(cmt.clamp_deadline(date(2026, 7, 20), date(2026, 7, 21), date(2026, 8, 3)),
                         date(2026, 7, 20))


class TestNames(unittest.TestCase):
    def test_first_name(self):
        self.assertEqual(cmt.first_name("Bob Moore"), "Bob")
        self.assertEqual(cmt.first_name("Jerome de Lafargue"), "Jerome")
        self.assertEqual(cmt.first_name("  Madonna  "), "Madonna")

    def test_no_collision(self):
        names = cmt.display_names({"Bob Moore", "Leslie Thomas"})
        self.assertEqual(names["Bob Moore"], "Bob")
        self.assertEqual(names["Leslie Thomas"], "Leslie")

    def test_collision_gets_last_initial(self):
        names = cmt.display_names({"Brett Levenson", "Brett Taylor"})
        self.assertEqual(names["Brett Levenson"], "Brett L.")
        self.assertEqual(names["Brett Taylor"], "Brett T.")


class TestDecodeThingsDate(unittest.TestCase):
    """Things packs deadline/startDate as bits, not a unix timestamp.

    The two raw values below were read out of a live Things database next to
    their known calendar dates, so they pin the layout against real data.
    """

    def test_known_raw_values_from_a_live_database(self):
        self.assertEqual(cmt.decode_things_date(132809600), date(2026, 8, 7))
        self.assertEqual(cmt.decode_things_date(132809728), date(2026, 8, 8))

    def test_round_trips_across_month_and_year_boundaries(self):
        for expected in (date(2026, 1, 1), date(2026, 12, 31), date(2027, 2, 28),
                         date(2026, 8, 31), date(2030, 6, 15)):
            packed = (expected.year << 16) | (expected.month << 12) | (expected.day << 7)
            self.assertEqual(cmt.decode_things_date(packed), expected)

    def test_missing_and_zero_are_none(self):
        self.assertIsNone(cmt.decode_things_date(None))
        self.assertIsNone(cmt.decode_things_date(0))

    def test_nonsense_packing_is_none_not_a_crash(self):
        # month 15 / day 0 cannot be a real date; must not raise.
        self.assertIsNone(cmt.decode_things_date((2026 << 16) | (15 << 12) | (1 << 7)))
        self.assertIsNone(cmt.decode_things_date((2026 << 16) | (1 << 12) | (0 << 7)))

    def test_not_confused_with_a_unix_timestamp(self):
        """A epoch-seconds value must not silently decode to a plausible date."""
        self.assertIsNone(cmt.decode_things_date(1754179200))


class TestNormTitle(unittest.TestCase):
    def test_dash_and_whitespace_variants_match(self):
        self.assertEqual(cmt.norm_title("Prep – Bob"), cmt.norm_title("prep - bob"))
        self.assertEqual(cmt.norm_title("Notes  -  Bob "), cmt.norm_title("Notes - Bob"))


class TestDedup(unittest.TestCase):
    def existing(self, title, deadline, status="open"):
        return cmt.ExistingTodo(title=title, deadline=deadline, status=status)

    def test_exact_match_is_duplicate(self):
        dup, reason = cmt.find_duplicate("Prep - Bob", date(2026, 8, 4),
                                         [self.existing("Prep - Bob", date(2026, 8, 4))], 3)
        self.assertIsNotNone(dup)
        self.assertIn("same title and deadline", reason)

    def test_within_window_is_duplicate(self):
        dup, _ = cmt.find_duplicate("Prep - Bob", date(2026, 8, 4),
                                    [self.existing("Prep - Bob", date(2026, 8, 5))], 3)
        self.assertIsNotNone(dup)

    def test_outside_window_is_not_duplicate(self):
        # Weekly cadence: last week's task (7 days earlier) must NOT block this week's
        dup, _ = cmt.find_duplicate("Prep - Bob", date(2026, 8, 11),
                                    [self.existing("Prep - Bob", date(2026, 8, 4))], 3)
        self.assertIsNone(dup)

    def test_open_task_without_deadline_is_duplicate(self):
        dup, reason = cmt.find_duplicate("Prep - Bob", date(2026, 8, 4),
                                         [self.existing("Prep - Bob", None)], 3)
        self.assertIsNotNone(dup)
        self.assertIn("no deadline", reason)

    def test_completed_task_without_deadline_is_ignored(self):
        dup, _ = cmt.find_duplicate("Prep - Bob", date(2026, 8, 4),
                                    [self.existing("Prep - Bob", None, status="completed")], 3)
        self.assertIsNone(dup)

    def test_completed_task_same_deadline_is_duplicate(self):
        # Rick already did the prep and checked it off — don't recreate it
        dup, _ = cmt.find_duplicate("Prep - Bob", date(2026, 8, 4),
                                    [self.existing("Prep - Bob", date(2026, 8, 4), status="completed")], 3)
        self.assertIsNotNone(dup)

    def test_different_client_is_not_duplicate(self):
        dup, _ = cmt.find_duplicate("Prep - Bob", date(2026, 8, 4),
                                    [self.existing("Prep - Leslie", date(2026, 8, 4))], 3)
        self.assertIsNone(dup)


class TestPlanTasks(unittest.TestCase):
    def meeting(self, name, when, summary="1:1"):
        client = cmt.Client(name=name, emails={f"{name.split()[0].lower()}@example.com"})
        return cmt.Meeting(client=client, summary=summary, meeting_date=when,
                           start_display=when.strftime("%a %Y-%m-%d 10:00"), event_id="evt")

    def test_pair_per_meeting_with_correct_deadlines(self):
        today = date(2026, 8, 3)
        planned = cmt.plan_tasks([self.meeting("Bob Moore", date(2026, 8, 5))], today)
        # 2026-08-05 is a Wednesday; BOTH tasks carry the meeting's weekday,
        # so Prep is "(Wed)" even though it is due on the Tuesday.
        self.assertEqual([(t.title, t.deadline) for t in planned],
                         [("Prep - Bob (Wed)", date(2026, 8, 4)),   # day BEFORE the meeting
                          ("Notes - Bob (Wed)", date(2026, 8, 5))])  # day OF the meeting

    def test_same_day_meeting_prep_clamped_to_today(self):
        today = date(2026, 8, 3)
        planned = cmt.plan_tasks([self.meeting("Bob Moore", date(2026, 8, 3))], today)
        self.assertEqual(planned[0].deadline, today)

    def test_two_events_same_client_same_day_deduped_in_run(self):
        today = date(2026, 8, 3)
        planned = cmt.plan_tasks([self.meeting("Bob Moore", date(2026, 8, 5), "call A"),
                                  self.meeting("Bob Moore", date(2026, 8, 5), "call B")], today)
        self.assertEqual(len(planned), 2)  # one Prep + one Notes, not four

    def test_two_meetings_same_client_different_days_get_two_pairs(self):
        today = date(2026, 8, 3)
        planned = cmt.plan_tasks([self.meeting("Bob Moore", date(2026, 8, 4)),
                                  self.meeting("Bob Moore", date(2026, 8, 6))], today)
        self.assertEqual(len(planned), 4)

    def test_first_name_collision_disambiguated(self):
        today = date(2026, 8, 3)
        planned = cmt.plan_tasks([self.meeting("Brett Levenson", date(2026, 8, 4)),
                                  self.meeting("Brett Taylor", date(2026, 8, 5))], today)
        titles = {t.title for t in planned}
        self.assertIn("Prep - Brett L. (Tue)", titles)
        self.assertIn("Prep - Brett T. (Wed)", titles)

    def test_suffix_is_the_meeting_weekday_not_the_deadline_weekday(self):
        """A Monday meeting preps on Sunday but is still labelled (Mon)."""
        planned = cmt.plan_tasks([self.meeting("Bob Moore", date(2026, 8, 10))],
                                 date(2026, 8, 3))
        prep = planned[0]
        self.assertEqual(prep.title, "Prep - Bob (Mon)")
        self.assertEqual(prep.deadline, date(2026, 8, 9))       # a Sunday
        self.assertEqual(prep.deadline.strftime("%a"), "Sun")

    def test_titles_match_the_existing_project_convention(self):
        """Guards the exact shape already present in Things (e.g. 'Prep - Marissa (Wed)')."""
        planned = cmt.plan_tasks([self.meeting("Marissa Dent", date(2026, 8, 12))],
                                 date(2026, 8, 3))
        self.assertEqual([t.title for t in planned],
                         ["Prep - Marissa (Wed)", "Notes - Marissa (Wed)"])

    def test_weekday_abbreviations_are_locale_independent(self):
        for i, expected in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            self.assertEqual(cmt.WEEKDAY_ABBR[i], expected)


class TestEventFiltering(unittest.TestCase):
    TZ = ZoneInfo("America/Los_Angeles")

    def test_parse_timed_event_converts_to_local_date(self):
        # 02:30 UTC on Aug 6 is Aug 5 in Los Angeles
        event = {"start": {"dateTime": "2026-08-06T02:30:00Z"}}
        meeting_date, display = cmt.parse_event_start(event, self.TZ)
        self.assertEqual(meeting_date, date(2026, 8, 5))
        self.assertIn("19:30", display)

    def test_parse_all_day_event(self):
        meeting_date, display = cmt.parse_event_start({"start": {"date": "2026-08-05"}}, self.TZ)
        self.assertEqual(meeting_date, date(2026, 8, 5))
        self.assertIn("all day", display)

    def test_declined_and_non_default_events_excluded(self):
        self.assertFalse(cmt.event_is_relevant(
            {"attendees": [{"self": True, "responseStatus": "declined"}]}))
        self.assertFalse(cmt.event_is_relevant({"eventType": "focusTime"}))
        self.assertFalse(cmt.event_is_relevant({"status": "cancelled"}))
        self.assertTrue(cmt.event_is_relevant(
            {"eventType": "default", "attendees": [{"self": True, "responseStatus": "accepted"}]}))

    def test_match_by_attendee_email_only(self):
        bob = cmt.Client(name="Bob Moore", emails={"bob@crossbeam.com"})
        events = [
            {"summary": "Bob Moore <> Rick", "start": {"dateTime": "2026-08-03T11:00:00-07:00"},
             "attendees": [{"email": "rick@rickarmbrust.com"}, {"email": "BOB@crossbeam.com"}]},
            {"summary": "Bob HOLD", "start": {"dateTime": "2026-08-04T11:00:00-07:00"}},  # no attendees
        ]
        meetings = cmt.find_client_meetings(events, [bob], date(2026, 8, 3), date(2026, 8, 7), self.TZ)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].client.name, "Bob Moore")
        self.assertEqual(meetings[0].meeting_date, date(2026, 8, 3))

    def test_event_outside_range_excluded(self):
        bob = cmt.Client(name="Bob Moore", emails={"bob@crossbeam.com"})
        events = [{"summary": "x", "start": {"dateTime": "2026-08-10T11:00:00-07:00"},
                   "attendees": [{"email": "bob@crossbeam.com"}]}]
        meetings = cmt.find_client_meetings(events, [bob], date(2026, 8, 3), date(2026, 8, 7), self.TZ)
        self.assertEqual(meetings, [])


class TestEnvFile(unittest.TestCase):
    def test_parse_env_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write("# comment\nexport FOO=bar\nQUOTED=\"a b\"\nEMPTY=\nBAD LINE\n")
            path = fh.name
        try:
            env = cmt.parse_env_file(path)
            self.assertEqual(env["FOO"], "bar")
            self.assertEqual(env["QUOTED"], "a b")
            self.assertEqual(env["EMPTY"], "")
            self.assertNotIn("BAD LINE", env)
        finally:
            os.unlink(path)

    def test_missing_file_is_empty(self):
        self.assertEqual(cmt.parse_env_file("/nonexistent/.env"), {})


class TestGoogleCredentialAliases(unittest.TestCase):
    """GCAL_* is accepted so an existing credential need not be duplicated."""

    def config_from(self, contents):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(contents)
            path = fh.name
        try:
            return cmt.load_config(env_files=[path])
        finally:
            os.unlink(path)

    def test_gcal_names_are_accepted(self):
        cfg = self.config_from("GCAL_CLIENT_ID=cid\nGCAL_CLIENT_SECRET=sec\n"
                               "GCAL_REFRESH_TOKEN=tok\n")
        self.assertEqual(cfg.google_client_id, "cid")
        self.assertEqual(cfg.google_client_secret, "sec")
        self.assertEqual(cfg.google_refresh_token, "tok")

    def test_google_oauth_names_win_when_both_are_set(self):
        cfg = self.config_from("GOOGLE_OAUTH_CLIENT_ID=canonical\nGCAL_CLIENT_ID=alias\n")
        self.assertEqual(cfg.google_client_id, "canonical")

    def test_empty_canonical_falls_through_to_the_alias(self):
        cfg = self.config_from("GOOGLE_OAUTH_CLIENT_ID=\nGCAL_CLIENT_ID=alias\n")
        self.assertEqual(cfg.google_client_id, "alias")

    def test_neither_set_is_empty_not_an_error(self):
        self.assertEqual(self.config_from("UNRELATED=1\n").google_client_id, "")


if __name__ == "__main__":
    unittest.main()
