import unittest
from datetime import date

from helpers import hm, make_cfg, manual

from autofill_plan import (
    Block, Event, allocate_filler, free_gaps, parse_filler, plan_day,
)

MON = date(2026, 11, 2)
TUE = date(2026, 11, 3)


def creates(plan):
    return [(b.start, b.end, b.desc, b.source) for b in plan.creates]


def overlaps(a, b):
    return a.start < b.end and b.start < a.end


class EmptyDay(unittest.TestCase):
    def test_monday_totals_target_with_expected_layout(self):
        plan = plan_day(MON, make_cfg(), [], [], [], "dev work")
        self.assertEqual(plan.total, 480)
        self.assertEqual(plan.diff, 0)
        self.assertFalse(plan.past_day_end)
        self.assertEqual(creates(plan), [
            (hm("09:00"), hm("09:45"), "PR reviews", "template"),
            (hm("09:45"), hm("10:00"), "dev work", "filler"),
            (hm("10:00"), hm("10:30"), "Weekly planning", "template"),
            (hm("10:30"), hm("11:30"), "Support / email", "template"),
            (hm("11:30"), hm("12:30"), "dev work", "filler"),
            (hm("13:00"), hm("17:30"), "dev work", "filler"),
        ])

    def test_anchored_item_only_on_its_weekdays(self):
        plan = plan_day(TUE, make_cfg(), [], [], [], "dev")
        self.assertNotIn("Weekly planning", [b.desc for b in plan.creates])
        self.assertEqual(plan.total, 480)

    def test_entries_carry_configured_project_and_task(self):
        plan = plan_day(TUE, make_cfg(), [], [], [], "dev")
        pairs = {(b.desc if b.source != "filler" else "filler", b.project, b.task) for b in plan.creates}
        self.assertIn(("PR reviews", "PIP", "Code review"), pairs)
        self.assertIn(("filler", "PIP", "Development"), pairs)


class Rerun(unittest.TestCase):
    def test_second_run_keeps_matching_generated_entries_and_writes_nothing(self):
        cfg = make_cfg()
        first = plan_day(MON, cfg, [], [], [], "dev")
        generated = [Block(b.start, b.end, b.project, b.task, b.desc, "manual", "keep", entry_id=f"g{i}")
                     for i, b in enumerate(first.creates)]
        second = plan_day(MON, cfg, [], generated, [], "dev")
        self.assertEqual(second.creates, [])
        self.assertEqual(second.deletes, [])
        kept = [(b.start, b.end, b.desc, b.source) for b in second.rows if b.action == "keep"]
        self.assertEqual(kept, creates(first))
        self.assertEqual(sorted(b.entry_id for b in second.rows), sorted(b.entry_id for b in generated))
        self.assertEqual(second.total, 480)

    def test_changed_plan_deletes_only_stale_generated_entries(self):
        cfg = make_cfg()
        first = plan_day(TUE, cfg, [], [], [], "dev")
        generated = [Block(b.start, b.end, b.project, b.task, b.desc, "manual", "keep", entry_id=f"g{i}")
                     for i, b in enumerate(first.creates)]
        second = plan_day(TUE, cfg, [], generated, [], "other work")   # filler text changed
        self.assertEqual({b.desc for b in second.deletes}, {"dev"})
        self.assertEqual({b.desc for b in second.creates}, {"other work"})
        self.assertEqual({b.desc for b in second.rows if b.action == "keep"}, {"PR reviews", "Support / email"})
        self.assertEqual(second.total, 480)


class ManualEntries(unittest.TestCase):
    def test_manual_entries_kept_and_filler_shrinks(self):
        m = manual("14:00", "16:00")
        plan = plan_day(TUE, make_cfg(), [m], [], [], "dev")
        kept = [b for b in plan.rows if b.action == "keep"]
        self.assertEqual([(b.start, b.end, b.entry_id) for b in kept], [(m.start, m.end, "m1")])
        self.assertFalse(any(b.action == "delete" for b in plan.rows))
        filler = sum(b.dur for b in plan.creates if b.source == "filler")
        self.assertEqual(filler, 480 - 120 - 45 - 60)
        self.assertEqual(plan.total, 480)
        for b in plan.creates:
            self.assertFalse(overlaps(b, m), b)

    def test_unaligned_manual_times_still_total_exactly(self):
        plan = plan_day(TUE, make_cfg(), [manual("09:07", "09:52")], [], [], "dev")
        self.assertEqual(plan.total, 480)
        for b in plan.creates:
            if b.source == "template":
                self.assertEqual(b.start % 5, 0)

    def test_anchored_item_overlapping_manual_is_dropped_and_listed(self):
        plan = plan_day(MON, make_cfg(), [manual("09:50", "10:20")], [], [], "dev")
        self.assertNotIn("Weekly planning", [b.desc for b in plan.creates])
        self.assertEqual(len(plan.dropped), 1)
        self.assertIn("Weekly planning", plan.dropped[0][0])
        self.assertIn("manual", plan.dropped[0][1])
        self.assertEqual(plan.total, 480)


class AlreadyLogged(unittest.TestCase):
    def test_template_items_logged_by_hand_are_not_added_again(self):
        logged = [manual("08:00", "08:40", desc="pr reviews "), manual("16:00", "16:30", desc="Weekly planning")]
        plan = plan_day(MON, make_cfg(), logged, [], [], "dev")
        template = [b.desc for b in plan.creates if b.source == "template"]
        self.assertEqual(template, ["Support / email"])
        self.assertEqual(len(plan.dropped), 2)
        for name in ("PR reviews", "Weekly planning"):
            self.assertTrue(any(name in what and why == "already logged manually" for what, why in plan.dropped),
                            (name, plan.dropped))
        self.assertEqual(plan.total, 480)


class MinimumEntryLength(unittest.TestCase):
    def cfg15(self, **kw):
        return make_cfg(round_to="15m", **kw)

    def assert_no_short_entries(self, plan, minimum=15):
        for b in plan.creates:
            if b.source != "calendar":
                self.assertGreaterEqual(b.dur, minimum, b)

    def test_short_gaps_stay_empty_and_total_still_exact(self):
        # Manual 09:50-10:52 leaves 09:45..09:50 and 10:52..11:00 slivers.
        plan = plan_day(TUE, self.cfg15(), [manual("09:52", "10:52")], [], [], "dev")
        self.assert_no_short_entries(plan)
        self.assertEqual(plan.total, 480)

    def test_task_tail_never_shorter_than_round_to(self):
        # 2h40m + 1h05m odd remainders across several gaps.
        ms = [manual("09:07", "09:52"), manual("11:10", "11:47"), manual("14:03", "14:31")]
        for text in ("a 2h40m; b", "a; b; c", "x"):
            plan = plan_day(TUE, self.cfg15(), ms, [], [], text)
            self.assert_no_short_entries(plan)
            self.assertEqual(plan.total, 480, text)

    def test_less_than_round_to_left_means_no_filler(self):
        plan = plan_day(TUE, self.cfg15(), [manual("08:00", "14:10")], [], [], "dev")
        self.assertFalse([b for b in plan.creates if b.source == "filler"])
        self.assertTrue(any("no filler entry" in w for w in plan.warnings))
        self.assertEqual(plan.diff, -5)

    def test_meetings_keep_their_exact_short_length(self):
        ev = Event(hm("10:07"), hm("10:12"), "Quick sync", "PIP", "Meetings")
        plan = plan_day(TUE, self.cfg15(), [], [], [ev], "dev")
        sync = next(b for b in plan.creates if b.source == "calendar")
        self.assertEqual(sync.dur, 5)
        self.assert_no_short_entries(plan)
        self.assertEqual(plan.total, 480)

    def test_jitter_moves_in_15_minute_steps(self):
        cfg = self.cfg15(jitter="15m")
        seen = set()
        for d in [date(2026, 11, 2) + __import__("datetime").timedelta(days=i) for i in range(28)]:
            plan = plan_day(d, cfg, [], [], [], "dev")
            for b in plan.creates:
                if b.desc == "PR reviews":
                    seen.add(b.dur)
        self.assertTrue(seen <= {30, 45, 60}, seen)
        self.assertGreater(len(seen), 1)


class OverTarget(unittest.TestCase):
    def test_day_over_target_gets_no_filler_and_is_flagged(self):
        plan = plan_day(TUE, make_cfg(), [manual("08:00", "16:00")], [], [], "dev")
        self.assertFalse([b for b in plan.creates if b.source == "filler"])
        self.assertEqual(plan.diff, 480 + 45 + 60 - 480)
        self.assertGreater(plan.diff, 0)

    def test_dash_means_no_filler_and_day_is_under(self):
        plan = plan_day(TUE, make_cfg(), [], [], [], None)
        self.assertFalse([b for b in plan.creates if b.source == "filler"])
        self.assertEqual(plan.diff, -(480 - 105))


class Skips(unittest.TestCase):
    def test_weekend_skipped(self):
        for d in (date(2026, 11, 7), date(2026, 11, 8)):
            plan = plan_day(d, make_cfg(), [], [], [], "dev")
            self.assertEqual(plan.skipped, "weekend")
            self.assertEqual(plan.rows, [])

    def test_skip_dates_skipped(self):
        plan = plan_day(date(2026, 11, 11), make_cfg(), [], [], [], "dev")
        self.assertEqual(plan.skipped, "skip_dates")
        self.assertEqual(plan.rows, [])


class BreaksAndDayEnd(unittest.TestCase):
    def test_breaks_stay_free(self):
        lunch = Block(hm("12:30"), hm("13:00"), "", "", "lunch", "break", "keep")
        for day in (MON, TUE):
            plan = plan_day(day, make_cfg(), [manual("09:00", "10:30")], [], [], "dev")
            for b in plan.creates:
                self.assertFalse(overlaps(b, lunch), b)

    def test_anchored_template_item_over_a_break_is_dropped(self):
        items = [{"desc": "Lunch talk", "project": "PIP", "task": "Meetings", "at": "12:45", "dur": "30m"}]
        plan = plan_day(TUE, make_cfg(items=items), [], [], [], "dev")
        self.assertEqual(plan.creates and [b.desc for b in plan.creates if b.source == "template"], [])
        self.assertEqual(plan.dropped[0][1], "overlaps a break")

    def test_item_that_does_not_fit_goes_after_last_block_and_is_flagged(self):
        plan = plan_day(TUE, make_cfg(), [manual("09:00", "16:00")], [], [], "dev")
        support = next(b for b in plan.creates if b.desc == "Support / email")
        self.assertEqual((support.start, support.end), (hm("16:45"), hm("17:45")))
        self.assertTrue(plan.past_day_end)

    def test_filler_that_does_not_fit_before_day_end_overflows_and_is_flagged(self):
        # Window equals the target; a 09:02 manual end wastes 3 minutes to rounding (next start 09:05).
        plan = plan_day(TUE, make_cfg(), [manual("09:00", "09:02")], [], [], "dev")
        self.assertEqual(plan.total, 480)
        self.assertTrue(plan.past_day_end)
        last = max(plan.creates, key=lambda b: b.end)
        self.assertEqual((last.source, last.start), ("filler", hm("17:30")))


class FillerParsing(unittest.TestCase):
    def test_spec_example(self):
        self.assertEqual(parse_filler("EXTITANS-2529 quick drill 5h; research caching"),
                         [("EXTITANS-2529 quick drill", 300), ("research caching", None)])

    def test_forms(self):
        self.assertEqual(parse_filler("a 1h30m; b 45m;  ; c"), [("a", 90), ("b", 45), ("c", None)])
        self.assertEqual(parse_filler("fix 2 bugs"), [("fix 2 bugs", None)])
        self.assertEqual(parse_filler("-"), [])
        self.assertEqual(parse_filler(""), [])
        self.assertEqual(parse_filler(None), [])

    def test_allocation_explicit_then_equal_shares(self):
        alloc, warnings = allocate_filler([("a", 300), ("b", None), ("c", None)], 403, 5)
        self.assertEqual(alloc, [("a", 300), ("b", 50), ("c", 53)])
        self.assertEqual(warnings, [])

    def test_allocation_with_no_time_left_warns(self):
        alloc, warnings = allocate_filler([("a", 300), ("b", None)], 200, 5)
        self.assertEqual(alloc, [("a", 300)])
        self.assertTrue(warnings)

    def test_multiple_filler_tasks_become_separate_entries_and_total_exactly(self):
        plan = plan_day(TUE, make_cfg(), [], [], [], "EXTITANS-2529 quick drill 2h; research caching")
        by_desc = {}
        for b in plan.creates:
            if b.source == "filler":
                by_desc[b.desc] = by_desc.get(b.desc, 0) + b.dur
        self.assertEqual(by_desc["EXTITANS-2529 quick drill"], 120)
        self.assertEqual(by_desc["research caching"], 480 - 105 - 120)
        self.assertEqual(plan.total, 480)

    def test_filler_never_overlaps_anything(self):
        plan = plan_day(MON, make_cfg(), [manual("11:00", "11:20")], [], [], "a; b; c")
        blocks = [b for b in plan.rows if b.action != "delete"]
        for i, a in enumerate(blocks):
            for b in blocks[i + 1:]:
                self.assertFalse(overlaps(a, b), (a, b))


class CalendarEventsInPlanner(unittest.TestCase):
    def test_accepted_event_keeps_exact_times_and_beats_overlapping_template(self):
        ev = Event(hm("10:07"), hm("10:22"), "Standup", "PIP", "Meetings")
        plan = plan_day(MON, make_cfg(), [], [], [ev], "dev")
        cal = [b for b in plan.creates if b.source == "calendar"]
        self.assertEqual([(b.start, b.end, b.desc) for b in cal], [(hm("10:07"), hm("10:22"), "Standup")])
        self.assertIn("Weekly planning", plan.dropped[0][0])
        self.assertEqual(plan.total, 480)

    def test_event_overlapping_manual_is_dropped(self):
        ev = Event(hm("14:00"), hm("15:00"), "Sync", "PIP", "Meetings")
        plan = plan_day(TUE, make_cfg(), [manual("14:30", "15:30")], [], [ev], "dev")
        self.assertFalse([b for b in plan.creates if b.source == "calendar"])
        self.assertIn("Sync", plan.dropped[0][0])


class Gaps(unittest.TestCase):
    def test_free_gaps(self):
        self.assertEqual(free_gaps([(600, 630), (620, 700), (780, 800)], 540, 1050),
                         [(540, 600), (700, 780), (800, 1050)])
        self.assertEqual(free_gaps([(500, 1100)], 540, 1050), [])


if __name__ == "__main__":
    unittest.main()
