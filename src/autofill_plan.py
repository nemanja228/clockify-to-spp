"""
Planner for the Clockify autofill command.

Pure logic: takes a day's existing entries, accepted calendar events, the
config and the filler tasks, and returns a plan. No network calls, so it can
be tested with fixed inputs. Times are minutes since local midnight.
"""

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import date

from autofill_config import Config, Item, Rule, fmt_clock, fmt_minutes, merge_intervals, parse_duration


@dataclass
class Block:
    start: int
    end: int
    project: str
    task: str
    desc: str
    source: str                 # template | calendar | filler | manual | generated
    action: str                 # create | delete | keep
    tags: list[str] = field(default_factory=list)
    entry_id: str | None = None

    @property
    def dur(self) -> int:
        return self.end - self.start

    def overlaps(self, start: int, end: int) -> bool:
        return self.start < end and start < self.end


@dataclass
class Event:
    """An accepted calendar event, already resolved to a project and task."""
    start: int
    end: int
    title: str
    project: str
    task: str
    tags: list[str] = field(default_factory=list)


@dataclass
class DayPlan:
    day: date
    target: int
    skipped: str | None = None
    rows: list[Block] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)   # (what, why)
    past_day_end: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(b.dur for b in self.rows if b.action in ("keep", "create"))

    @property
    def diff(self) -> int:
        """Positive = over target, negative = under."""
        return self.total - self.target

    @property
    def creates(self) -> list[Block]:
        return [b for b in self.rows if b.action == "create"]

    @property
    def deletes(self) -> list[Block]:
        return [b for b in self.rows if b.action == "delete"]


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

def round_near(minutes: int, step: int) -> int:
    return int((minutes + step / 2) // step) * step


def round_up(minutes: int, step: int) -> int:
    return -(-minutes // step) * step


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------

def jitter_minutes(day: date, desc: str, spread: int, round_to: int) -> int:
    """
    Deterministic change in [-spread, +spread], rounded to round_to.
    Seeded from the date and description, so a rerun of the same day matches.
    """
    if spread <= 0:
        return 0
    # sha256 rather than hash(): str hashes change between Python processes.
    seed = int.from_bytes(hashlib.sha256(f"{day.isoformat()}|{desc}".encode("utf-8")).digest()[:8], "big")
    change = random.Random(seed).uniform(-spread, spread)
    return max(-spread, min(spread, round_near(change, round_to)))


def jittered_duration(day: date, item: Item, cfg: Config) -> int:
    spread = cfg.jitter if item.jitter is None else item.jitter
    dur = round_near(item.dur, cfg.round_to) + jitter_minutes(day, item.desc, spread, cfg.round_to)
    return max(cfg.round_to, round_near(dur, cfg.round_to))


# ---------------------------------------------------------------------------
# Filler tasks
# ---------------------------------------------------------------------------

_TRAILING_DUR = re.compile(r"^(.*\S)\s+(\d+h(?:\d+m)?|\d+m)$", re.IGNORECASE)


def parse_filler(text: str | None) -> list[tuple[str, int | None]]:
    """'EXTITANS-2529 quick drill 5h; research caching' -> [(desc, 300), (desc, None)]."""
    if not text or text.strip() == "-":
        return []
    tasks = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        m = _TRAILING_DUR.match(part)
        if m:
            tasks.append((m.group(1).strip(), parse_duration(m.group(2))))
        else:
            tasks.append((part, None))
    return tasks


def allocate_filler(tasks: list[tuple[str, int | None]], available: int, round_to: int
                    ) -> tuple[list[tuple[str, int]], list[str]]:
    """Give explicit durations as given; split what is left equally among the rest."""
    warnings = []
    explicit = sum(d for _, d in tasks if d is not None)
    shared = [desc for desc, d in tasks if d is None]
    remaining = available - explicit
    shares: dict[str, int] = {}
    if shared:
        # Each share is at least round_to; tasks that don't fit get none and a warning.
        fit = min(len(shared), max(0, remaining) // round_to)
        if fit < len(shared):
            warnings.append(f"no filler time left for: {', '.join(shared[fit:])}")
        if fit:
            each = max(round_to, (remaining // fit) // round_to * round_to)
            for i in range(fit):
                shares[i] = each if i < fit - 1 else remaining - each * (fit - 1)
    out, si = [], 0
    for desc, d in tasks:
        if d is None:
            minutes = shares.get(si, 0)
            si += 1
        else:
            minutes = d
        if minutes > 0:
            out.append((desc, minutes))
    return out, warnings


# ---------------------------------------------------------------------------
# Event rules
# ---------------------------------------------------------------------------

def match_rule(title: str, cfg: Config) -> tuple[Rule | None, str]:
    """Config rules, then learned rules, then default_action. First match wins."""
    for rule in cfg.event_rules + cfg.learned_rules:
        if rule.pattern.search(title):
            return rule, rule.action
    return None, cfg.default_action


def accepted_event(start: int, end: int, title: str, rule: Rule | None, cfg: Config) -> Event:
    """An accepted event uses the rule's project/task where given, meetings_default otherwise."""
    project, task = cfg.rule_target(rule) if rule else (cfg.meetings_default.project,
                                                        cfg.meetings_default.task)
    tags = (rule.tags if rule and rule.tags else cfg.meetings_default.tags)
    if rule and rule.dur:
        end = start + rule.dur
    return Event(start, end, title, project, task, list(tags))


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def free_gaps(busy: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    gaps, cursor = [], start
    for s, e in merge_intervals(busy):
        if e <= cursor:
            continue
        if s >= end:
            break
        if s > cursor:
            gaps.append((cursor, min(s, end)))
        cursor = max(cursor, e)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def plan_day(day: date, cfg: Config, manual: list[Block], generated: list[Block],
             events: list[Event], filler_text: str | None) -> DayPlan:
    plan = DayPlan(day=day, target=cfg.target)
    if day.weekday() >= 5:
        plan.skipped = "weekend"
        return plan
    if day in cfg.skip_dates:
        plan.skipped = "skip_dates"
        return plan

    step = cfg.round_to
    plan.rows.extend(Block(b.start, b.end, b.project, b.task, b.desc, "manual", "keep", b.tags, b.entry_id)
                     for b in manual)
    # A template item whose description was already logged by hand that day is not added again.
    logged = {m.desc.strip().lower() for m in manual if m.desc.strip()}

    breaks = [(b.at, b.at + b.dur) for b in cfg.breaks]
    placed: list[Block] = []     # new entries, in placement order

    def busy() -> list[tuple[int, int]]:
        return [(b.start, b.end) for b in manual] + breaks + [(b.start, b.end) for b in placed]

    # Anchored: calendar events first (exact times), then anchored template items.
    anchored = [Block(e.start, e.end, e.project, e.task, e.title, "calendar", "create", e.tags)
                for e in sorted(events, key=lambda e: (e.start, e.end))]
    for item in cfg.items:
        if item.anchored and day.weekday() in item.days:
            start = round_near(item.at, step)
            anchored.append(Block(start, start + (round_near(item.dur, step) or step),
                                  item.project, item.task, item.desc, "template", "create", item.tags))
    for cand in anchored:
        label = f"{fmt_clock(cand.start)}-{fmt_clock(cand.end)} {cand.desc} ({cand.source})"
        if cand.source == "template" and cand.desc.strip().lower() in logged:
            plan.dropped.append((label, "already logged manually"))
            continue
        hit = next((m for m in manual if m.overlaps(cand.start, cand.end)), None)
        if hit:
            plan.dropped.append((label, f"overlaps manual entry '{hit.desc}'"))
            continue
        if cand.source == "template" and any(s < cand.end and cand.start < e for s, e in breaks):
            plan.dropped.append((label, "overlaps a break"))
            continue
        other = next((p for p in placed if p.overlaps(cand.start, cand.end)), None)
        if other:
            plan.dropped.append((label, f"overlaps '{other.desc}'"))
            continue
        placed.append(cand)

    # Flexible template items, in config order, into the earliest gap that fits whole.
    for item in cfg.items:
        if item.anchored or day.weekday() not in item.days:
            continue
        if item.desc.strip().lower() in logged:
            plan.dropped.append((f"{item.desc} (template)", "already logged manually"))
            continue
        dur = jittered_duration(day, item, cfg)
        start = None
        for gs, ge in free_gaps(busy(), cfg.day_start, cfg.day_end):
            s = round_up(gs, step)
            if s + dur <= ge:
                start = s
                break
        if start is None:
            start = round_up(max([cfg.day_start] + [e for _, e in busy()]), step)
            plan.past_day_end = True
        placed.append(Block(start, start + dur, item.project, item.task, item.desc,
                            "template", "create", item.tags))

    # Filler: whatever is left up to the target.
    used = sum(b.dur for b in manual) + sum(b.dur for b in placed)
    available = cfg.target - used
    tasks = parse_filler(filler_text)
    if tasks and 0 < available < step:
        plan.warnings.append(f"only {fmt_minutes(available)} left for the filler, less than "
                             f"{fmt_minutes(step)}; no filler entry created")
    elif available > 0 and tasks:
        alloc, warnings = allocate_filler(tasks, available, step)
        plan.warnings.extend(warnings)
        # No filler piece shorter than round_to: short gaps stay empty, and a task is
        # never split so that its tail is shorter than round_to.
        gaps = [(round_up(s, step), e) for s, e in free_gaps(busy(), cfg.day_start, cfg.day_end)]
        gaps = [(s, e) for s, e in gaps if e - s >= step]
        gi = 0
        for desc, minutes in alloc:
            while minutes > 0:
                if gi < len(gaps):
                    gs, ge = gaps[gi]
                    take = min(minutes, ge - gs)
                    if 0 < minutes - take < step:
                        take = minutes - step
                    if take < min(step, minutes):
                        gi += 1
                        continue
                    placed.append(Block(gs, gs + take, cfg.filler.project, cfg.filler.task, desc,
                                        "filler", "create", cfg.filler.tags))
                    minutes -= take
                    rest = round_up(gs + take, step)
                    if ge - rest < step:
                        gi += 1
                    else:
                        gaps[gi] = (rest, ge)
                else:
                    start = round_up(max([cfg.day_start] + [e for _, e in busy()]), step)
                    placed.append(Block(start, start + minutes, cfg.filler.project, cfg.filler.task,
                                        desc, "filler", "create", cfg.filler.tags))
                    plan.past_day_end = True
                    minutes = 0

    # Generated entries that already match the plan are kept rather than rebuilt.
    stale = list(generated)
    for b in placed:
        key = (b.start, b.end, b.desc, b.project, b.task, sorted(b.tags))
        match = next((g for g in stale
                      if (g.start, g.end, g.desc, g.project, g.task, sorted(g.tags)) == key), None)
        if match:
            stale.remove(match)
            b.action, b.entry_id = "keep", match.entry_id
    plan.rows.extend(placed)
    plan.rows.extend(Block(g.start, g.end, g.project, g.task, g.desc, "generated", "delete", g.tags, g.entry_id)
                     for g in stale)
    if any(b.end > cfg.day_end for b in placed):
        plan.past_day_end = True
    order = {"keep": 0, "create": 0, "delete": 1}
    plan.rows.sort(key=lambda b: (order[b.action], b.start, b.end))
    return plan
