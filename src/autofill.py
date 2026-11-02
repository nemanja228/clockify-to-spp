#!/usr/bin/env python3
"""
Fill a day or a week of Clockify time entries from a template.

Companion to spp_export.py. Reads the Clockify API key and workspace from
spp_config.json (CLOCKIFY_API_KEY in the environment or .env overrides the
key), and the template from autofill.yaml. Each calendar feed URL is read from
the variable its url_env names: the environment, then .env, then a key of the
same name in spp_config.json. Feed URLs are never printed.

Usage:
    python autofill.py fill day [DATE] [--dry-run] [--no-calendar]
    python autofill.py fill week [DATE] [--dry-run] [--no-calendar]
    python autofill.py clear day [DATE]
    python autofill.py clear week [DATE]
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import spp_export
from spp_export import die, eprint
from autofill_config import (
    Config, ConfigError, DEFAULT_CONFIG_NAME, append_learned_rule, fmt_clock, fmt_minutes,
    learned_rule_for, load_config, load_dotenv,
)
from autofill_plan import Block, DayPlan, Event, accepted_event, match_rule, plan_day
from autofill_calendar import CalEvent, load_calendars
from autofill_clockify import FREE_PLAN_HOURLY_LIMIT, ClockifyOps, recent_filler_default


SCRIPT_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------

def check_spp_mappings(cfg: Config, catalog: dict[str, dict],
                       ignored_clients: set[str], ignored_project_ids: set[str]) -> list[str]:
    """
    The SPP export passes Clockify project and task names through unchanged and
    drops ignored clients and projects. So a configured project/task is mapped
    when it exists in Clockify and the export does not ignore it.
    """
    errors, seen = [], set()
    for project, task, where in cfg.project_task_pairs():
        if (project, task) in seen:
            continue
        seen.add((project, task))
        p = catalog.get(project)
        if p is None:
            errors.append(f"{where}: project '{project}' does not exist in the Clockify workspace")
        elif p["id"] in ignored_project_ids or (p["client"] or "").lower() in ignored_clients:
            errors.append(f"{where}: project '{project}' is ignored by the SPP export "
                          f"(client '{p['client']}' or project id in spp_config.json)")
        elif task not in p["tasks"]:
            errors.append(f"{where}: task '{task}' does not exist in project '{project}'")
    return errors


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        die(f"Invalid date {value!r}. Use YYYY-MM-DD.")


def range_days(scope: str, anchor: date, weekdays: int) -> list[date]:
    if scope == "day":
        return [anchor]
    monday = anchor - timedelta(days=anchor.weekday())
    return [monday + timedelta(days=i) for i in range(weekdays)]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def render_day(plan: DayPlan, cfg: Config) -> str:
    title = f"{plan.day.strftime('%a')} {plan.day.isoformat()}"
    if plan.skipped:
        return f"{title}  skipped ({plan.skipped})\n"
    header = ("Start", "End", "Dur", "Project / Task", "Description", "Source", "Action")
    rows = [(fmt_clock(b.start), fmt_clock(b.end), fmt_minutes(b.dur),
             f"{b.project} / {b.task}" if b.task else b.project,
             b.desc or "(no description)", b.source, b.action) for b in plan.rows]
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: "  " + "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()

    out = [title, line(header), line(tuple("-" * w for w in widths))]
    out += [line(r) for r in rows] or ["  (nothing)"]
    status = "OK"
    if plan.diff > 0:
        status = f"OVER by {fmt_minutes(plan.diff)}"
    elif plan.diff < 0:
        status = f"UNDER by {fmt_minutes(-plan.diff)}"
    out.append(f"  Total {fmt_minutes(plan.total)} / target {fmt_minutes(plan.target)}  {status}")
    if plan.past_day_end:
        out.append(f"  ! Runs past day end ({fmt_clock(cfg.day_end)})")
    for w in plan.warnings:
        out.append(f"  ! {w}")
    for what, why in plan.dropped:
        out.append(f"  Dropped: {what}: {why}")
    return "\n".join(out) + "\n"


def render_preview(plans: list[DayPlan], cfg: Config) -> str:
    return "\n".join(render_day(p, cfg) for p in plans)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def ask(prompt: str, input_fn) -> str:
    try:
        return input_fn(prompt)
    except EOFError:
        die("No input available for the prompt (stdin closed).")


def prompt_filler(day: date, cfg: Config, default: str | None, input_fn) -> str | None:
    label = cfg.filler.task.lower()
    shown = f" [{default}]" if default else ""
    while True:
        answer = ask(f"{day.strftime('%a')} {day.isoformat()}  {label}{shown}: ", input_fn).strip()
        if answer == "-":
            return None
        if answer:
            return answer
        if default:
            return default
        eprint("  Type the filler tasks, or - for no filler this day.")


def confirm(prompt: str, input_fn) -> bool:
    return ask(prompt, input_fn).strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def prompt_event(ev: CalEvent, input_fn) -> str:
    while True:
        answer = ask(f"  {fmt_clock(ev.start)}-{fmt_clock(ev.end)}  {ev.title}  [{ev.feed}]  "
                     f"add? [y/n/a=always/x=never]: ", input_fn).strip().lower()
        if answer in ("y", "n", "a", "x"):
            return answer
        eprint("  Answer y (yes), n (no), a (always) or x (never).")


def resolve_events(working: list[date], cfg: Config, events: list[CalEvent], dry_run: bool,
                   input_fn) -> dict[date, list[Event]]:
    """Apply event rules day by day; ask about events whose action is 'ask'."""
    out: dict[date, list[Event]] = {}
    for day in working:
        todays = [ev for ev in events if ev.day == day]
        header_shown = False
        for ev in todays:
            rule, action = match_rule(ev.title, cfg)
            if action == "ask":
                if not header_shown:
                    print(f"{day.strftime('%a')} {day.isoformat()}  calendar events:")
                    header_shown = True
                answer = prompt_event(ev, input_fn)
                if answer in ("a", "x"):
                    rule = learned_rule_for(ev.title, "accept" if answer == "a" else "ignore")
                    cfg.learned_rules.append(rule)      # not asked again in this run
                    if dry_run:
                        print(f"    (dry run: rule for '{ev.title}' not saved)")
                    else:
                        append_learned_rule(cfg.learned_rules_path, rule)
                action = "accept" if answer in ("y", "a") else "ignore"
            if action == "accept":
                out.setdefault(day, []).append(accepted_event(ev.start, ev.end, ev.title, rule, cfg))
    return out


def cmd_fill(days: list[date], cfg: Config, ops: ClockifyOps, catalog: dict, args, input_fn,
             spp_cfg: dict | None = None) -> None:
    working = [d for d in days if d.weekday() < 5 and d not in cfg.skip_dates]
    tag_id = ops.find_tag(cfg.generated_tag)
    first = min(days) - timedelta(days=14)
    by_day = ops.entries_by_day(ops.list_entries(first, max(days)), tag_id)

    events_by_day: dict[date, list[Event]] = {}
    if working and cfg.calendars and not args.no_calendar:
        cal_events, _ = load_calendars(cfg, spp_cfg, min(working), max(working))
        events_by_day = resolve_events(working, cfg, cal_events, args.dry_run, input_fn)

    fillers: dict[date, str | None] = {}
    previous: str | None = None
    for i, day in enumerate(working):
        if i > 0:
            default = previous
        else:
            default = recent_filler_default(by_day, day, cfg.filler.project, cfg.filler.task)
        fillers[day] = prompt_filler(day, cfg, default, input_fn)
        previous = fillers[day]

    plans = []
    for day in days:
        manual, generated = by_day.get(day, ([], []))
        plans.append(plan_day(day, cfg, manual, generated, events_by_day.get(day, []), fillers.get(day)))

    print()
    print(render_preview(plans, cfg))
    apply_plans(plans, cfg, ops, catalog, args.dry_run, input_fn)


def cmd_clear(days: list[date], cfg: Config, ops: ClockifyOps, catalog: dict, args, input_fn) -> None:
    tag_id = ops.find_tag(cfg.generated_tag)
    by_day = ops.entries_by_day(ops.list_entries(min(days), max(days)), tag_id) if tag_id else {}
    plans = []
    for day in days:
        _, generated = by_day.get(day, ([], []))
        plan = DayPlan(day=day, target=cfg.target)
        plan.rows = [Block(b.start, b.end, b.project, b.task, b.desc, "generated", "delete",
                           entry_id=b.entry_id) for b in sorted(generated, key=lambda b: b.start)]
        if plan.rows:
            plans.append(plan)
    if not plans:
        print(f"No entries tagged '{cfg.generated_tag}' in this range. Nothing to clear.")
        return
    print(render_preview(plans, cfg))
    apply_plans(plans, cfg, ops, catalog, args.dry_run, input_fn)


def apply_plans(plans: list[DayPlan], cfg: Config, ops: ClockifyOps, catalog: dict,
                dry_run: bool, input_fn) -> None:
    deletes = [b for p in plans for b in p.deletes]
    creates = [(p.day, b) for p in plans for b in p.creates]
    tag_names = [cfg.generated_tag] + sorted({t for _, b in creates for t in b.tags} - {cfg.generated_tag})
    # One bulk delete, one request per created entry, up to two per tag (find, then maybe create).
    write_requests = (1 if deletes else 0) + len(creates) + (2 * len(tag_names) if creates else 0)
    used = getattr(ops, "requests", 0)
    summary = f"delete {len(deletes)} generated and create {len(creates)} entries"
    if dry_run:
        print(f"Dry run: would {summary} (up to {write_requests} Clockify requests). Nothing written.")
        return
    if not deletes and not creates:
        print("Nothing to change.")
        return
    if used + write_requests > FREE_PLAN_HOURLY_LIMIT:
        eprint(f"Warning: this run has used {used} Clockify requests and the writes need up to "
               f"{write_requests} more. The Free plan allows {FREE_PLAN_HOURLY_LIMIT} per hour, so the "
               f"apply may stop partway. Rerunning the fill later rebuilds the generated entries.")
    if not confirm(f"{summary[0].upper()}{summary[1:]}? [y/N] ", input_fn):
        print("Cancelled. Nothing written.")
        return

    tag_ids = {}
    if creates:
        for t in tag_names:
            tag_ids[t] = ops.find_or_create_tag(t)

    ops.delete_entries([b.entry_id for b in deletes])
    if deletes:
        print(f"Deleted {len(deletes)} generated entries.")
    for i, (day, b) in enumerate(creates, 1):
        p = catalog[b.project]
        ops.create_entry(day, b, p["id"], p["tasks"][b.task],
                         [tag_ids[cfg.generated_tag]] + [tag_ids[t] for t in b.tags])
        print(f"Created {i}/{len(creates)}: {day.isoformat()} {fmt_clock(b.start)}-{fmt_clock(b.end)} {b.desc}")
    print(f"Done: deleted {len(deletes)}, created {len(creates)}.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fill Clockify time entries from a template (companion to spp_export.py).",
        epilog="Examples:\n"
        "  %(prog)s fill day\n"
        "  %(prog)s fill week 2026-11-02 --dry-run\n"
        "  %(prog)s clear day 2026-11-03\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["fill", "clear"])
    parser.add_argument("scope", choices=["day", "week"])
    parser.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Show the preview, write nothing")
    parser.add_argument("--no-calendar", action="store_true", help="Skip calendar import")
    parser.add_argument("--config", default=None,
                        help=f"Path to the autofill YAML (default: {DEFAULT_CONFIG_NAME} next to this script)")
    return parser


def main(argv: list[str] | None = None, input_fn=input) -> None:
    args = build_parser().parse_args(argv)

    config_path = Path(args.config) if args.config else SCRIPT_DIR / DEFAULT_CONFIG_NAME
    load_dotenv(SCRIPT_DIR.parent / ".env", config_path.resolve().parent / ".env")
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        die(str(e))

    spp_cfg = spp_export.load_config(spp_export.find_config(None))
    api_key = os.environ.get("CLOCKIFY_API_KEY") or spp_cfg["api_key"]
    ops = ClockifyOps(api_key, spp_cfg["workspace_id"], cfg.tz)

    catalog = ops.project_catalog({p for p, _, _ in cfg.project_task_pairs()})
    errors = check_spp_mappings(
        cfg, catalog,
        {c.lower() for c in spp_cfg.get("ignored_client_names", [])},
        set(spp_cfg.get("ignored_project_ids", [])),
    )
    if errors:
        die("Configuration names projects or tasks the SPP export cannot use:\n  " + "\n  ".join(errors))

    anchor = parse_day(args.date)
    if args.command == "fill":
        cmd_fill(range_days(args.scope, anchor, 5), cfg, ops, catalog, args, input_fn, spp_cfg)
    else:
        cmd_clear(range_days(args.scope, anchor, 7), cfg, ops, catalog, args, input_fn)
    eprint(f"Clockify requests this run: {ops.requests}")


if __name__ == "__main__":
    main()
