import os
import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

from helpers import base_raw, hm, make_cfg, manual

from autofill_plan import jitter_minutes, plan_day

SRC = Path(__file__).resolve().parent.parent / "src"


def weekdays(n: int, start: date = date(2026, 11, 2)) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5 and d != date(2026, 11, 11):
            days.append(d)
        d += timedelta(days=1)
    return days


def durations(plan) -> dict[str, int]:
    return {b.desc: b.dur for b in plan.creates if b.source == "template"}


class JitterFunction(unittest.TestCase):
    def test_within_range_and_on_the_rounding_grid(self):
        for d in weekdays(40):
            j = jitter_minutes(d, "PR reviews", 15, 5)
            self.assertLessEqual(abs(j), 15)
            self.assertEqual(j % 5, 0)

    def test_zero_range_means_no_change(self):
        self.assertEqual(jitter_minutes(date(2026, 11, 3), "x", 0, 5), 0)

    def test_same_value_in_a_new_process_with_a_different_hash_seed(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from datetime import date; "
                "from autofill_plan import jitter_minutes; "
                "print([jitter_minutes(date(2026, 11, d), 'PR reviews', 15, 5) for d in range(2, 7)])")
        outs = set()
        for seed in ("1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            outs.add(subprocess.run([sys.executable, "-c", code, str(SRC)], env=env,
                                    capture_output=True, text=True, check=True).stdout.strip())
        here = str([jitter_minutes(date(2026, 11, d), "PR reviews", 15, 5) for d in range(2, 7)])
        self.assertEqual(outs, {here})


class JitterInPlans(unittest.TestCase):
    def cfg(self, **kw):
        return make_cfg(jitter="15m", **kw)

    def test_identical_across_reruns_of_the_same_day(self):
        cfg = self.cfg()
        for d in weekdays(5):
            a = plan_day(d, cfg, [], [], [], "dev")
            b = plan_day(d, cfg, [], [], [], "dev")
            self.assertEqual([(x.start, x.end, x.desc) for x in a.creates],
                             [(x.start, x.end, x.desc) for x in b.creates])

    def test_durations_differ_between_days(self):
        cfg = self.cfg()
        seen = {durations(plan_day(d, cfg, [], [], [], "dev"))["PR reviews"] for d in weekdays(10)}
        self.assertGreater(len(seen), 1, seen)

    def test_daily_totals_still_match_target(self):
        cfg = self.cfg()
        for d in weekdays(20):
            for manuals in ([], [manual("14:00", "15:10")], [manual("09:03", "09:41")]):
                plan = plan_day(d, cfg, manuals, [], [], "dev; research")
                self.assertEqual(plan.total, 480, (d, manuals))

    def test_never_shorter_than_round_to(self):
        items = [{"desc": "Tiny", "project": "PIP", "task": "Support", "dur": "5m", "jitter": "30m"}]
        cfg = make_cfg(items=items)
        for d in weekdays(30):
            tiny = durations(plan_day(d, cfg, [], [], [], "dev"))["Tiny"]
            self.assertGreaterEqual(tiny, 5)
            self.assertEqual(tiny % 5, 0)

    def test_item_jitter_overrides_default_and_anchored_items_never_jitter(self):
        raw = base_raw(jitter="15m")
        raw["items"][0]["jitter"] = "0m"                       # PR reviews pinned
        raw["items"][2].pop("jitter")                          # Weekly planning anchored, no override
        from autofill_config import parse_config
        cfg = parse_config(raw, Path("x.yaml"))
        for d in weekdays(10):
            plan = plan_day(d, cfg, [], [], [], "dev")
            got = durations(plan)
            self.assertEqual(got["PR reviews"], 45)
            if d.weekday() == 0:
                weekly = next(b for b in plan.creates if b.desc == "Weekly planning")
                self.assertEqual((weekly.start, weekly.end), (hm("10:00"), hm("10:30")))


if __name__ == "__main__":
    unittest.main()
