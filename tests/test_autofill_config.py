import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from helpers import base_raw, make_cfg

from autofill_config import (
    ConfigError, load_dotenv, parse_clock, parse_config, parse_duration,
)


class Durations(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_duration("15m"), 15)
        self.assertEqual(parse_duration("1h"), 60)
        self.assertEqual(parse_duration("1h30m"), 90)
        self.assertEqual(parse_duration("0m"), 0)
        self.assertEqual(parse_duration(0), 0)

    def test_invalid(self):
        for bad in ("", "h", "90", "1.5h", "30 m", "m30", 45):
            with self.assertRaises(ConfigError, msg=bad):
                parse_duration(bad)

    def test_clock(self):
        self.assertEqual(parse_clock("09:00"), 540)
        self.assertEqual(parse_clock(750), 750)   # unquoted 12:30 in YAML 1.1
        with self.assertRaises(ConfigError):
            parse_clock("9am")


class Validation(unittest.TestCase):
    def test_base_config_parses(self):
        cfg = make_cfg()
        self.assertEqual(cfg.target, 480)
        self.assertEqual(cfg.skip_dates, {date(2026, 11, 11)})
        self.assertEqual(cfg.items[2].days, [0])
        self.assertTrue(cfg.items[2].anchored)
        self.assertEqual(cfg.items[0].days, [0, 1, 2, 3, 4])

    def test_window_smaller_than_target_stops(self):
        with self.assertRaises(ConfigError) as ctx:
            make_cfg(day_end="17:00")
        self.assertIn("less than the target", str(ctx.exception))

    def test_unknown_field_stops(self):
        with self.assertRaises(ConfigError) as ctx:
            make_cfg(targte="8h")
        self.assertIn("targte", str(ctx.exception))

    def test_url_in_config_is_refused(self):
        with self.assertRaises(ConfigError):
            make_cfg(calendars=[{"name": "work", "url": "https://x", "url_env": "X"}])

    def test_bad_weekday_and_action(self):
        items = [{"desc": "x", "project": "P", "task": "T", "dur": "15m", "days": ["mondey"]}]
        with self.assertRaises(ConfigError):
            make_cfg(items=items)
        with self.assertRaises(ConfigError):
            make_cfg(event_rules=[{"match": "x", "action": "maybe"}])

    def test_project_task_pairs_resolve_rule_defaults(self):
        cfg = make_cfg(event_rules=[{"match": "standup", "action": "accept", "task": "Standup"},
                                    {"match": "focus", "action": "ignore"}])
        pairs = {(p, t) for p, t, _ in cfg.project_task_pairs()}
        self.assertIn(("PIP", "Standup"), pairs)          # project from meetings_default
        self.assertIn(("PIP", "Development"), pairs)
        self.assertEqual(len([w for _, _, w in cfg.project_task_pairs() if "focus" in w]), 0)

    def test_yaml_file_with_dates_and_unquoted_times(self):
        text = (
            "target: 8h\nday_start: \"09:00\"\nday_end: 17:30\nbreaks: [{at: 12:30, dur: 30m}]\n"
            "filler: {project: P, task: T}\nskip_dates: [2026-12-24]\n"
        )
        import yaml
        cfg = parse_config(yaml.safe_load(text), Path("x.yaml"))
        self.assertEqual((cfg.day_end, cfg.breaks[0].at), (17 * 60 + 30, 12 * 60 + 30))
        self.assertEqual(cfg.skip_dates, {date(2026, 12, 24)})


class DotEnv(unittest.TestCase):
    def test_reads_values_without_overriding_real_env(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("# c\nAF_TEST_A=1\nexport AF_TEST_B='two'\nAF_TEST_C=\"x=y\"\n", encoding="utf-8")
            os.environ["AF_TEST_A"] = "real"
            try:
                load_dotenv(p, Path(d) / "missing.env")
                self.assertEqual(os.environ["AF_TEST_A"], "real")
                self.assertEqual(os.environ["AF_TEST_B"], "two")
                self.assertEqual(os.environ["AF_TEST_C"], "x=y")
            finally:
                for k in ("AF_TEST_A", "AF_TEST_B", "AF_TEST_C"):
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
