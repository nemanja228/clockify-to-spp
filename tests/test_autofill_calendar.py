import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from helpers import hm, make_cfg

import autofill
from autofill_calendar import CalEvent, load_calendars, parse_feed
from autofill_config import append_learned_rule, learned_rule_for, load_learned_rules
from autofill_plan import match_rule, plan_day

MON, TUE, WED, THU, FRI = (date(2026, 11, d) for d in range(2, 7))
TZ = make_cfg().tz

WORK = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//work//EN
BEGIN:VEVENT
UID:standup@test
SUMMARY:Daily Standup
DTSTART;TZID=Europe/Belgrade:20261102T093000
DTEND;TZID=Europe/Belgrade:20261102T100000
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
EXDATE;TZID=Europe/Belgrade:20261104T093000
END:VEVENT
BEGIN:VEVENT
UID:standup@test
RECURRENCE-ID;TZID=Europe/Belgrade:20261105T093000
SUMMARY:Daily Standup
DTSTART;TZID=Europe/Belgrade:20261105T110000
DTEND;TZID=Europe/Belgrade:20261105T113000
END:VEVENT
BEGIN:VEVENT
UID:standup@test
RECURRENCE-ID;TZID=Europe/Belgrade:20261106T093000
SUMMARY:Daily Standup
STATUS:CANCELLED
DTSTART;TZID=Europe/Belgrade:20261106T093000
DTEND;TZID=Europe/Belgrade:20261106T100000
END:VEVENT
BEGIN:VEVENT
UID:allday@test
SUMMARY:Conference
DTSTART;VALUE=DATE:20261103
DTEND;VALUE=DATE:20261104
END:VEVENT
BEGIN:VEVENT
UID:cancelled@test
SUMMARY:Cancelled sync
STATUS:CANCELLED
DTSTART;TZID=Europe/Belgrade:20261103T140000
DTEND;TZID=Europe/Belgrade:20261103T143000
END:VEVENT
BEGIN:VEVENT
UID:declined@test
SUMMARY:Declined invite
DTSTART;TZID=Europe/Belgrade:20261103T150000
DTEND;TZID=Europe/Belgrade:20261103T153000
ATTENDEE;PARTSTAT=DECLINED:mailto:Me@Example.com
ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com
END:VEVENT
BEGIN:VEVENT
UID:one@test
SUMMARY:1:1 (Ana) [weekly]
DTSTART;TZID=Europe/Belgrade:20261103T160000
DTEND;TZID=Europe/Belgrade:20261103T163000
ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@example.com
END:VEVENT
BEGIN:VEVENT
UID:sprint@test
SUMMARY:Sprint review
DTSTART:20261103T120000Z
DTEND:20261103T130000Z
END:VEVENT
END:VCALENDAR
"""

GOOGLE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//google//EN
X-WR-TIMEZONE:Europe/Belgrade
BEGIN:VEVENT
UID:dup@google
SUMMARY:  sprint REVIEW
DTSTART:20261103T120000Z
DTEND:20261103T130000Z
END:VEVENT
BEGIN:VEVENT
UID:dentist@google
SUMMARY:Dentist
DTSTART:20261104T150000Z
DTEND:20261104T160000Z
END:VEVENT
END:VCALENDAR
"""

CALS = [{"name": "outlook", "url_env": "AF_TEST_CAL_OUTLOOK"}, {"name": "google", "url_env": "AF_TEST_CAL_GOOGLE"}]


def cfg_with(**kw):
    kw.setdefault("calendars", CALS)
    kw.setdefault("my_emails", ["me@example.com"])
    return make_cfg(**kw)


def parse(data=WORK):
    return parse_feed(data, "work", MON, FRI, TZ, ["me@example.com"])


class Parsing(unittest.TestCase):
    def test_recurring_meeting_on_correct_days_with_moved_and_cancelled_occurrences(self):
        events, _ = parse()
        standups = sorted((e.day, e.start, e.end) for e in events if e.title == "Daily Standup")
        self.assertEqual(standups, [
            (MON, hm("09:30"), hm("10:00")),
            (TUE, hm("09:30"), hm("10:00")),
            (THU, hm("11:00"), hm("11:30")),     # moved; Wed removed by EXDATE, Fri cancelled
        ])

    def test_all_day_cancelled_and_declined_are_excluded(self):
        events, report = parse()
        titles = {e.title for e in events}
        self.assertNotIn("Conference", titles)
        self.assertNotIn("Cancelled sync", titles)
        self.assertNotIn("Declined invite", titles)
        self.assertIn("1:1 (Ana) [weekly]", titles)
        self.assertEqual(report.all_day, 1)
        self.assertEqual(report.declined, 1)
        self.assertGreaterEqual(report.cancelled, 1)
        self.assertEqual(report.with_my_status, 2)

    def test_utc_times_convert_to_the_configured_zone(self):
        events, _ = parse()
        sprint = next(e for e in events if e.title == "Sprint review")
        self.assertEqual((sprint.day, sprint.start, sprint.end), (TUE, hm("13:00"), hm("14:00")))

    def test_feed_without_attendees_skips_the_declined_check(self):
        events, report = parse(GOOGLE)
        self.assertEqual(report.with_attendees, 0)
        self.assertEqual(len(events), 2)


class Loading(unittest.TestCase):
    def setUp(self):
        os.environ["AF_TEST_CAL_OUTLOOK"] = "https://secret.example/outlook-token.ics"
        os.environ.pop("AF_TEST_CAL_GOOGLE", None)

    def tearDown(self):
        os.environ.pop("AF_TEST_CAL_OUTLOOK", None)

    def test_duplicates_across_feeds_are_removed_and_json_supplies_a_missing_variable(self):
        feeds = {"https://secret.example/outlook-token.ics": WORK, "https://json.example/g.ics": GOOGLE}
        spp_cfg = {"AF_TEST_CAL_GOOGLE": "https://json.example/g.ics"}
        events, reports = load_calendars(cfg_with(), spp_cfg, MON, FRI, fetch=lambda u: feeds[u])
        sprints = [e for e in events if e.title.strip().lower() == "sprint review"]
        self.assertEqual(len(sprints), 1)
        self.assertEqual(sprints[0].feed, "outlook")
        self.assertIn("Dentist", {e.title for e in events})
        self.assertTrue(all(r.ok for r in reports))

    def test_failing_feed_warns_by_name_without_url_and_the_rest_continues(self):
        def fetch(url):
            if "outlook" in url:
                raise OSError(f"boom for {url}")
            return GOOGLE

        err = io.StringIO()
        with redirect_stderr(err):
            events, reports = load_calendars(cfg_with(), {"AF_TEST_CAL_GOOGLE": "https://g"}, MON, FRI, fetch=fetch)
        self.assertIn("calendar 'outlook' could not be fetched", err.getvalue())
        self.assertNotIn("secret.example", err.getvalue())
        self.assertNotIn("outlook-token", err.getvalue())
        self.assertEqual({e.feed for e in events}, {"google"})

    def test_unparseable_feed_warns_and_continues(self):
        err = io.StringIO()
        with redirect_stderr(err):
            events, _ = load_calendars(cfg_with(calendars=CALS[:1]), None, MON, FRI, fetch=lambda u: b"not ics")
        self.assertIn("could not be parsed", err.getvalue())
        self.assertEqual(events, [])

    def test_missing_variable_warns_and_continues(self):
        err = io.StringIO()
        with redirect_stderr(err):
            load_calendars(cfg_with(), None, MON, FRI, fetch=lambda u: WORK)
        self.assertIn("AF_TEST_CAL_GOOGLE is not set", err.getvalue())


class Rules(unittest.TestCase):
    def test_config_rules_then_learned_then_default(self):
        cfg = cfg_with(event_rules=[{"match": "standup", "action": "accept", "task": "Standup"},
                                    {"match": "lunch|focus", "action": "ignore"}])
        cfg.learned_rules = [learned_rule_for("Daily Standup", "ignore"), learned_rule_for("Dentist", "ignore")]
        self.assertEqual(match_rule("Daily STANDUP", cfg)[1], "accept")     # config wins over learned
        self.assertEqual(match_rule("Focus time", cfg)[1], "ignore")
        self.assertEqual(match_rule("Dentist", cfg)[1], "ignore")           # learned
        self.assertEqual(match_rule("Something new", cfg), (None, "ask"))    # default

    def test_learned_rule_matches_the_exact_title_with_regex_characters_escaped(self):
        rule = learned_rule_for("1:1 (Ana) [weekly]", "accept")
        self.assertTrue(rule.pattern.search("1:1 (Ana) [weekly]"))
        self.assertTrue(rule.pattern.search("1:1 (ana) [WEEKLY]"))
        self.assertFalse(rule.pattern.search("1:1 (Ana) w"))
        self.assertFalse(rule.pattern.search("Re: 1:1 (Ana) [weekly]"))

    def test_accepted_event_uses_rule_project_task_or_meetings_default(self):
        cfg = cfg_with(event_rules=[{"match": "standup", "action": "accept", "task": "Standup"}])
        ev = CalEvent("work", TUE, hm("09:30"), hm("10:00"), "Daily Standup")
        out = autofill.resolve_events([TUE], cfg, [ev, CalEvent("work", TUE, 600, 630, "Other")], True,
                                      lambda p: "y")
        got = {(e.title, e.project, e.task) for e in out[TUE]}
        self.assertEqual(got, {("Daily Standup", "PIP", "Standup"), ("Other", "PIP", "Meetings")})


class RuleDuration(unittest.TestCase):
    def test_rule_dur_keeps_the_start_and_sets_the_length(self):
        cfg = cfg_with(event_rules=[{"match": "CBR / BPR", "action": "accept", "task": "Support", "dur": "15m"},
                                    {"match": "standup", "action": "accept"}])
        evs = [CalEvent("google", TUE, hm("10:00"), hm("10:30"), "CBR / BPR Daily Standup"),
               CalEvent("google", TUE, hm("09:30"), hm("10:00"), "PIP Daily Standup")]
        out = autofill.resolve_events([TUE], cfg, evs, True, lambda p: "n")
        got = {e.title: (e.start, e.end, e.task) for e in out[TUE]}
        self.assertEqual(got["CBR / BPR Daily Standup"], (hm("10:00"), hm("10:15"), "Support"))
        self.assertEqual(got["PIP Daily Standup"], (hm("09:30"), hm("10:00"), "Meetings"))

    def test_bad_rule_dur_stops_at_startup(self):
        from autofill_config import ConfigError
        with self.assertRaises(ConfigError):
            cfg_with(event_rules=[{"match": "x", "action": "accept", "dur": "0m"}])


class Prompts(unittest.TestCase):
    EVENTS = [CalEvent("work", TUE, 600, 630, "Vendor call"), CalEvent("work", TUE, 700, 730, "Random sync"),
              CalEvent("work", WED, 600, 630, "Vendor call")]

    def run_resolve(self, cfg, answers, dry_run=False):
        answers = list(answers)
        asked = []

        def fake_input(p):
            asked.append(p)
            return answers.pop(0)

        with redirect_stdout(io.StringIO()):
            out = autofill.resolve_events([TUE, WED], cfg, self.EVENTS, dry_run, fake_input)
        return out, asked

    def test_always_and_never_are_not_asked_on_the_next_run(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_with()
            cfg.path = Path(d) / "autofill.yaml"
            out, asked = self.run_resolve(cfg, ["a", "x"])
            self.assertEqual(len(asked), 2)               # Wednesday's "Vendor call" not asked again
            self.assertEqual({e.title for evs in out.values() for e in evs}, {"Vendor call"})
            self.assertEqual(len(out[WED]), 1)

            cfg2 = cfg_with()
            cfg2.path = cfg.path
            cfg2.learned_rules = load_learned_rules(cfg.learned_rules_path)
            out2, asked2 = self.run_resolve(cfg2, [])
            self.assertEqual(asked2, [])
            self.assertEqual({e.title for evs in out2.values() for e in evs}, {"Vendor call"})
            self.assertFalse(Path(d, "autofill.yaml").exists())     # config file never written

    def test_y_and_n_do_not_save_rules_and_dry_run_saves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = cfg_with()
            cfg.path = Path(d) / "autofill.yaml"
            self.run_resolve(cfg, ["y", "n", "n"])
            self.assertFalse(cfg.learned_rules_path.exists())
            self.run_resolve(cfg_with(), ["a", "x"], dry_run=True)
            self.assertFalse(cfg.learned_rules_path.exists())

    def test_invalid_answer_is_asked_again(self):
        cfg = cfg_with()
        err = io.StringIO()
        with redirect_stderr(err):
            out, asked = self.run_resolve(cfg, ["maybe", "y", "n", "n"], dry_run=True)
        self.assertEqual(len(asked), 4)

    def test_append_keeps_existing_rules(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.learned.yaml"
            append_learned_rule(p, learned_rule_for("A (x)", "accept"))
            append_learned_rule(p, learned_rule_for("B", "ignore"))
            rules = load_learned_rules(p)
            self.assertEqual([(r.action, bool(r.pattern.search("A (x)"))) for r in rules],
                             [("accept", True), ("ignore", False)])


class CalendarIntoPlan(unittest.TestCase):
    def test_accepted_standup_from_calendar_lands_in_the_plan_with_exact_times(self):
        cfg = cfg_with(event_rules=[{"match": "standup", "action": "accept"}])
        events, _ = parse()
        resolved = autofill.resolve_events([THU], cfg, events, True, lambda p: "n")
        plan = plan_day(THU, cfg, [], [], resolved.get(THU, []), "dev")
        cal = [(b.start, b.end, b.desc) for b in plan.creates if b.source == "calendar"]
        self.assertEqual(cal, [(hm("11:00"), hm("11:30"), "Daily Standup")])
        self.assertEqual(plan.total, 480)


if __name__ == "__main__":
    unittest.main()
