import io
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import date

from helpers import hm, make_cfg, manual

import autofill
from autofill_plan import Block
from autofill_clockify import recent_filler_default

MON = date(2026, 11, 2)
CATALOG = {
    "PIP": {"id": "p1", "client": "ProfitOptics",
            "tasks": {"Code review": "t1", "Support": "t2", "Meetings": "t3", "Development": "t4"}},
}


class FakeOps:
    """Stands in for ClockifyOps. Records writes; reads come from a fixed dict."""

    def __init__(self, by_day=None, tag_id="tag-auto", forbid_writes=False):
        self.by_day = by_day or {}
        self.tag_id = tag_id
        self.forbid_writes = forbid_writes
        self.calls = []

    def find_tag(self, name):
        return self.tag_id

    def list_entries(self, first, last):
        return []

    def entries_by_day(self, raw, tag_id):
        return self.by_day

    def _write(self, *call):
        if self.forbid_writes:
            raise AssertionError(f"write attempted: {call}")
        self.calls.append(call)

    def find_or_create_tag(self, name):
        self._write("tag", name)
        return "tag-" + name

    def delete_entries(self, entry_ids):
        if entry_ids:
            self._write("delete", tuple(entry_ids))

    def create_entry(self, day, block, project_id, task_id, tag_ids):
        self._write("create", day, block.start, block.end, block.desc, project_id, task_id, tuple(tag_ids))


def run(cmd, days, ops, answers, dry_run=False):
    answers = list(answers)
    out = io.StringIO()
    with redirect_stdout(out):
        cmd(days, make_cfg(), ops, CATALOG, Namespace(dry_run=dry_run, no_calendar=True),
            lambda prompt: answers.pop(0))
    return out.getvalue()


class DryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        old = Block(hm("09:00"), hm("17:00"), "PIP", "Development", "old", "manual", "keep", entry_id="g1")
        ops = FakeOps(by_day={MON: ([], [old])}, forbid_writes=True)
        out = run(autofill.cmd_fill, [MON], ops, ["dev"], dry_run=True)
        self.assertIn("Nothing written", out)
        self.assertIn("delete", out)
        self.assertIn("Total 8:00 / target 8:00  OK", out)

    def test_clear_dry_run_writes_nothing(self):
        old = Block(hm("09:00"), hm("10:00"), "PIP", "Development", "old", "manual", "keep", entry_id="g1")
        ops = FakeOps(by_day={MON: ([], [old])}, forbid_writes=True)
        out = run(autofill.cmd_clear, [MON], ops, [], dry_run=True)
        self.assertIn("Nothing written", out)


class Apply(unittest.TestCase):
    def test_confirm_deletes_generated_first_then_creates_with_tag(self):
        old = Block(hm("09:00"), hm("17:00"), "PIP", "Development", "old", "manual", "keep", entry_id="g1")
        keep = manual("14:00", "15:00", entry_id="m1")
        ops = FakeOps(by_day={MON: ([keep], [old])})
        run(autofill.cmd_fill, [MON], ops, ["dev", "y"])
        kinds = [c[0] for c in ops.calls]
        self.assertEqual(kinds[0], "tag")
        self.assertEqual([c for c in ops.calls if c[0] == "delete"], [("delete", ("g1",))])
        self.assertLess(kinds.index("delete"), kinds.index("create"))
        created = [c for c in ops.calls if c[0] == "create"]
        self.assertEqual(sum(c[3] - c[2] for c in created), 480 - 60)
        self.assertTrue(all(c[7] == ("tag-auto",) for c in created))

    def test_declining_confirmation_writes_nothing(self):
        ops = FakeOps(forbid_writes=True)
        out = run(autofill.cmd_fill, [MON], ops, ["dev", "n"])
        self.assertIn("Cancelled", out)


class Prompts(unittest.TestCase):
    def test_previous_answer_is_a_hint_only_and_dash_means_none(self):
        prompts = []
        # Mon typed; Tue Enter re-asks, then typed; Wed "-"; Thu typed; Fri typed.
        answers = ["EXTITANS-1 a", "", "EXTITANS-1 a", "-", "b", "c"]

        def fake_input(p):
            prompts.append(p)
            return answers.pop(0)

        days = autofill.range_days("week", MON, 5)
        out = io.StringIO()
        with redirect_stdout(out):
            autofill.cmd_fill(days, make_cfg(), FakeOps(), CATALOG,
                              Namespace(dry_run=True, no_calendar=True), fake_input)
        self.assertEqual(prompts, [
            "Mon 2026-11-02  development: ",
            "Tue 2026-11-03  development [last: EXTITANS-1 a]: ",
            "Tue 2026-11-03  development [last: EXTITANS-1 a]: ",     # Enter alone asks again
            "Wed 2026-11-04  development [last: EXTITANS-1 a]: ",
            "Thu 2026-11-05  development: ",
            "Fri 2026-11-06  development [last: b]: ",
        ])
        self.assertIn("UNDER", out.getvalue())   # Wednesday has no filler

    def test_clockify_default_from_recent_generated_filler(self):
        f1 = Block(600, 700, "PIP", "Development", "EXTITANS-9 x", "manual", "keep")
        f2 = Block(800, 900, "PIP", "Development", "research", "manual", "keep")
        other = Block(500, 550, "PIP", "Code review", "PR reviews", "manual", "keep")
        by_day = {date(2026, 10, 29): ([], [other, f2, f1]), date(2026, 10, 20): ([], [f1])}
        self.assertEqual(recent_filler_default(by_day, MON, make_cfg()), "EXTITANS-9 x; research")
        self.assertIsNone(recent_filler_default({date(2026, 10, 1): ([], [f1])}, MON, make_cfg()))

    def test_default_skips_template_items_and_meetings_on_the_filler_task_but_finds_routed_filler(self):
        # Template item, accepted standup and filler all on PIP / Development; GPO filler routed elsewhere.
        items = [{"desc": "PR reviews", "project": "PIP", "task": "Development", "dur": "1h"}]
        cfg = make_cfg(items=items, event_rules=[{"match": "standup", "action": "accept", "task": "Development"}],
                       filler_rules=[{"match": "GPO-1", "task": "GPO"}])
        gen = [Block(480, 540, "PIP", "Development", "PR reviews", "manual", "keep"),
               Block(570, 600, "PIP", "Development", "PIP Daily Standup", "manual", "keep"),
               Block(600, 700, "PIP", "Development", "EXTITANS-9", "manual", "keep"),
               Block(700, 800, "PIP", "GPO", "GPO-1 epic", "manual", "keep")]
        self.assertEqual(recent_filler_default({date(2026, 10, 30): ([], gen)}, MON, cfg), "EXTITANS-9; GPO-1 epic")


class Mappings(unittest.TestCase):
    def test_all_mapped(self):
        self.assertEqual(autofill.check_spp_mappings(make_cfg(), CATALOG, set(), set()), [])

    def test_missing_task_project_and_ignored_client_are_named(self):
        catalog = {"PIP": {"id": "p1", "client": "ProfitOptics", "tasks": {"Development": "t4"}}}
        errors = autofill.check_spp_mappings(make_cfg(), catalog, set(), set())
        self.assertTrue(any("task 'Code review'" in e and "PR reviews" in e for e in errors))
        self.assertTrue(autofill.check_spp_mappings(make_cfg(), {}, set(), set())[0].count("'PIP'"))
        ignored = autofill.check_spp_mappings(make_cfg(), CATALOG, {"profitoptics"}, set())
        self.assertTrue(ignored and "ignored by the SPP export" in ignored[0])


if __name__ == "__main__":
    unittest.main()
