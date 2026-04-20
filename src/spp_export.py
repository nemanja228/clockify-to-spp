#!/usr/bin/env python3
"""
Transform Clockify time entries into SPP-ready weekly time entries.

Setup:
    1. Copy spp_config.example.json to spp_config.json
    2. Fill in your api_key and workspace_id
    3. Optionally add project IDs to ignore

Usage:
    python spp_export.py this-week
    python spp_export.py last-week
    python spp_export.py 2025-06-02 2025-06-08
    python spp_export.py last-week --stdout
    python spp_export.py last-week --config other.json
    python spp_export.py last-week --csv clockify_export.csv

Mapping:
    Clockify Project  -> SPP "Client: Project"
    Clockify Task     -> SPP "Task"
"""

import csv
import json
import re
import sys
import argparse
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_NAME = "spp_config.json"

def find_config(override: str | None) -> Path:
    """Locate config file: explicit path > next to script > cwd."""
    if override:
        p = Path(override)
        if not p.exists():
            die(f"Config file not found: {override}")
        return p
    # Next to the script itself
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir / DEFAULT_CONFIG_NAME, Path.cwd() / DEFAULT_CONFIG_NAME]
    for c in candidates:
        if c.exists():
            return c

    # Auto-create from example template on first run
    example = script_dir / "spp_config.example.json"
    dest = script_dir / DEFAULT_CONFIG_NAME
    if example.exists():
        import shutil
        shutil.copy(example, dest)
        die(
            f"No config found — created {dest} from the example template.\n"
            f"Open it and replace YOUR_API_KEY and YOUR_WORKSPACE_ID, then re-run."
        )

    die(
        f"Config file not found. Looked in:\n"
        f"  {candidates[0]}\n  {candidates[1]}\n"
        f"Create one from spp_config.example.json or use --config <path>."
    )


def load_config(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in config file {path}: {e}")

    required = ["api_key", "workspace_id"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        die(f"Config file {path} is missing required fields: {missing}")

    cfg.setdefault("ignored_project_ids", [])
    cfg.setdefault("ignored_client_names", [])
    cfg.setdefault("output_dir", str(Path(__file__).resolve().parent / "spp_reports"))
    return cfg


# ---------------------------------------------------------------------------
# Date range resolution
# ---------------------------------------------------------------------------

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def resolve_date_range(args) -> tuple[datetime, datetime]:
    """
    Return (start, end) as UTC datetimes from CLI args.
    - this-week: current Monday 00:00 to Sunday 23:59:59
    - last-week: previous Monday 00:00 to Sunday 23:59:59
    - two dates: start 00:00 to end 23:59:59
    """
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)

    if args.period == "this-week":
        start = monday
        end = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    elif args.period == "last-week":
        start = monday - timedelta(days=7)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    else:
        # First positional is a date, second must also be a date
        if not args.end_date:
            die("Date range requires two dates: <start> <end> (YYYY-MM-DD)")
        try:
            start = datetime.strptime(args.period, "%Y-%m-%d")
            end = datetime.strptime(args.end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59
            )
        except ValueError:
            die(f"Invalid date format. Use YYYY-MM-DD. Got: {args.period} / {args.end_date}")
        if end < start:
            die(f"End date {args.end_date} is before start date {args.period}")

    return start, end


# ---------------------------------------------------------------------------
# Clockify API client (stdlib only, no requests dependency)
# ---------------------------------------------------------------------------

API_BASE = "https://api.clockify.me/api/v1"


def _build_ssl_context():
    """
    Build an SSL context for HTTPS requests.
    On Windows, Python's default cert loading can fail if the Windows
    certificate store contains a malformed certificate (ASN1 nested error).
    This tries the default path first, then falls back to certifi if
    installed, then falls back to an unverified context as last resort.
    """
    import ssl

    # Try 1: default context (works on most systems)
    try:
        ctx = ssl.create_default_context()
        # Force-load to trigger the error now rather than on first request
        ctx.load_default_certs()
        return ctx
    except ssl.SSLError:
        pass

    # Try 2: use certifi bundle if available
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx
    except ImportError:
        pass

    # Try 3: build context without the Windows store, using only bundled certs
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        # Try loading just the certifi-style path that some Python installs bundle
        import _ssl
        if hasattr(ssl, "get_default_verify_paths"):
            paths = ssl.get_default_verify_paths()
            if paths.cafile:
                ctx.load_verify_locations(paths.cafile)
                return ctx
    except Exception:
        pass

    # Last resort: warn and disable verification
    eprint(
        "Warning: Could not load SSL certificates. HTTPS requests will proceed "
        "without certificate verification. To fix this, install certifi:\n"
        "  pip install certifi"
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Module-level SSL context (built once)
_SSL_CTX = None


def _get_ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _build_ssl_context()
    return _SSL_CTX


def api_get(path: str, api_key: str, params: dict | None = None) -> list | dict:
    """GET from Clockify API. Returns parsed JSON."""
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_get_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        die(f"Clockify API error {e.code} on {path}: {body}")
    except urllib.error.URLError as e:
        die(f"Network error calling Clockify API: {e.reason}")


def get_user_id(api_key: str) -> str:
    """Get the authenticated user's ID."""
    user = api_get("/user", api_key)
    return user["id"]


def get_projects(workspace_id: str, api_key: str) -> dict[str, str]:
    """
    Fetch all projects. Returns:
        name_map:   {id: name}
        client_map: {id: client_name or ""}
    Handles pagination, includes archived projects.
    """
    name_map = {}
    client_map = {}

    for archived in ("false", "true"):
        page = 1
        while True:
            projects = api_get(
                f"/workspaces/{workspace_id}/projects",
                api_key,
                {"page": page, "page-size": 500, "archived": archived},
            )
            for p in projects:
                name_map[p["id"]] = p["name"]
                client_map[p["id"]] = p.get("clientName") or ""
            if len(projects) < 500:
                break
            page += 1

    return name_map, client_map


def get_tasks_for_project(workspace_id: str, project_id: str, api_key: str) -> dict[str, str]:
    """Fetch all tasks for a project, return {id: name}."""
    mapping = {}
    page = 1
    while True:
        tasks = api_get(
            f"/workspaces/{workspace_id}/projects/{project_id}/tasks",
            api_key,
            {"page": page, "page-size": 500, "is-active": "true"},
        )
        for t in tasks:
            mapping[t["id"]] = t["name"]
        if len(tasks) < 500:
            break
        page += 1
    # Also inactive tasks
    page = 1
    while True:
        tasks = api_get(
            f"/workspaces/{workspace_id}/projects/{project_id}/tasks",
            api_key,
            {"page": page, "page-size": 500, "is-active": "false"},
        )
        for t in tasks:
            mapping[t["id"]] = t["name"]
        if len(tasks) < 500:
            break
        page += 1
    return mapping


def get_all_tasks(workspace_id: str, project_ids: set[str], api_key: str) -> dict[str, str]:
    """Fetch tasks for all relevant projects. Returns {task_id: task_name}."""
    all_tasks = {}
    for pid in project_ids:
        tasks = get_tasks_for_project(workspace_id, pid, api_key)
        all_tasks.update(tasks)
    return all_tasks


def parse_iso_duration(iso: str) -> float:
    """Parse ISO 8601 duration like PT2H30M15S into decimal hours."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not match:
        return 0.0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return round(h + m / 60 + s / 3600, 2)


def fetch_time_entries(cfg: dict, start: datetime, end: datetime) -> list[dict]:
    """
    Pull time entries from Clockify API, resolve IDs to names,
    return list of entry dicts ready for aggregation.
    """
    api_key = cfg["api_key"]
    ws_id = cfg["workspace_id"]
    ignored = set(cfg.get("ignored_project_ids", []))

    user_id = get_user_id(api_key)
    eprint(f"Fetching entries for user {user_id}...")

    # Fetch time entries (paginated)
    raw_entries = []
    page = 1
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        batch = api_get(
            f"/workspaces/{ws_id}/user/{user_id}/time-entries",
            api_key,
            {
                "start": start_iso,
                "end": end_iso,
                "page": page,
                "page-size": 500,
            },
        )
        raw_entries.extend(batch)
        if len(batch) < 500:
            break
        page += 1

    eprint(f"Fetched {len(raw_entries)} raw entries.")

    if not raw_entries:
        return []

    # Filter ignored projects by ID
    raw_entries = [e for e in raw_entries if e.get("projectId") not in ignored]

    # Resolve project names and client names
    eprint("Resolving project names...")
    project_map, client_map = get_projects(ws_id, api_key)

    # Filter ignored clients by name (case-insensitive)
    ignored_clients = {c.lower() for c in cfg.get("ignored_client_names", [])}
    if ignored_clients:
        before = len(raw_entries)
        raw_entries = [
            e for e in raw_entries
            if client_map.get(e.get("projectId"), "").lower() not in ignored_clients
        ]
        diff = before - len(raw_entries)
        if diff:
            eprint(f"Filtered out {diff} entries from ignored clients.")

    # Collect unique project IDs
    project_ids = {e["projectId"] for e in raw_entries if e.get("projectId")}

    # Resolve task names (only for projects that have tasks in our entries)
    task_project_map = {}  # task_id -> project_id (to know which project to query)
    for e in raw_entries:
        if e.get("taskId") and e.get("projectId"):
            task_project_map[e["taskId"]] = e["projectId"]

    projects_to_query = set(task_project_map.values())
    eprint(f"Resolving tasks across {len(projects_to_query)} projects...")
    task_map = get_all_tasks(ws_id, projects_to_query, api_key)

    # Build entries
    entries = []
    for e in raw_entries:
        project_id = e.get("projectId")
        task_id = e.get("taskId")
        project_name = project_map.get(project_id, project_id or "(no project)")
        task_name = task_map.get(task_id, "") if task_id else ""

        ti = e.get("timeInterval", {})
        start_str = ti.get("start", "")
        duration_str = ti.get("duration", "")

        # Parse start date
        if start_str:
            # Clockify returns ISO 8601: 2025-06-02T09:00:00Z
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            # Convert to local date for grouping
            dt_local = dt.astimezone(tz=None)
        else:
            continue

        hours = parse_iso_duration(duration_str)
        if hours <= 0:
            continue

        entries.append({
            "project": project_name,
            "task": task_name,
            "description": e.get("description", "").strip() or "(no description)",
            "date": dt_local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None),
            "hours": hours,
        })

    eprint(f"Processed {len(entries)} entries after filtering.")
    return entries


# ---------------------------------------------------------------------------
# CSV fallback (kept from original script)
# ---------------------------------------------------------------------------

def parse_csv_duration(raw: str) -> float:
    raw = raw.strip()
    if not raw:
        return 0.0
    if ":" in raw:
        parts = raw.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return round(h + m / 60 + s / 3600, 2)
    return round(float(raw), 2)


def find_duration_column(headers: list[str]) -> str | None:
    candidates = [
        "Duration (decimal)", "Duration (h)", "Duration",
        "Time (decimal)", "Time (h)",
    ]
    header_lower = {h.lower().strip(): h for h in headers}
    for c in candidates:
        if c.lower() in header_lower:
            return header_lower[c.lower()]
    return None


def parse_csv_date(raw: str) -> datetime | None:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def load_entries_csv(
    csv_path: str, start: datetime, end: datetime,
    ignored_ids: set[str], ignored_client_names: set[str],
) -> list[dict]:
    """Load from Clockify detailed CSV export."""
    path = Path(csv_path)
    if not path.exists():
        die(f"CSV file not found: {csv_path}")

    raw = path.read_bytes()
    encoding = "utf-8-sig" if raw[:3] == b"\xef\xbb\xbf" else "utf-8"

    entries = []
    with open(path, newline="", encoding=encoding) as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        expected = {"Project", "Description", "Start Date"}
        missing = expected - set(h.strip() for h in headers)
        if missing:
            die(f"CSV missing expected columns: {missing}\nFound: {headers}")

        dur_col = find_duration_column(headers)
        if not dur_col:
            die(f"No duration column found. Headers: {headers}")

        for row in reader:
            project = (row.get("Project") or "").strip()
            task = (row.get("Task") or "").strip()
            client = (row.get("Client") or "").strip()
            description = (row.get("Description") or "").strip()
            date_raw = (row.get("Start Date") or "").strip()
            dur_raw = (row.get(dur_col) or "").strip()

            if not project or not date_raw:
                continue

            # Filter by client name (case-insensitive)
            if ignored_client_names and client.lower() in ignored_client_names:
                continue

            dt = parse_csv_date(date_raw)
            if dt is None:
                eprint(f"Warning: Could not parse date '{date_raw}', skipping")
                continue

            if dt < start or dt > end:
                continue

            hours = parse_csv_duration(dur_raw)
            if hours <= 0:
                continue

            entries.append({
                "project": project,
                "task": task,
                "description": description or "(no description)",
                "date": dt,
                "hours": hours,
            })

    return entries


# ---------------------------------------------------------------------------
# Aggregation & formatting (shared by both data sources)
# ---------------------------------------------------------------------------

def aggregate(entries: list[dict]) -> dict:
    """Group by (project, task, date). Merge duplicate descriptions."""
    groups = defaultdict(lambda: {"total_hours": 0.0, "items": []})

    for e in entries:
        key = (e["project"], e["task"], e["date"])
        groups[key]["total_hours"] += e["hours"]
        groups[key]["items"].append((e["description"], e["hours"]))

    for key, data in groups.items():
        merged = defaultdict(float)
        for desc, hrs in data["items"]:
            merged[desc] += hrs
        data["items"] = [(desc, round(hrs, 2)) for desc, hrs in merged.items()]
        data["total_hours"] = round(data["total_hours"], 2)

    return dict(sorted(groups.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])))


def spp_label(project: str, task: str) -> str:
    return f"{project} – {task}" if task else project


def format_spp(groups: dict, start: datetime, end: datetime) -> str:
    """Produce SPP-ready text output."""
    lines = []
    lines.append(f"SPP Time Report: {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 60)
    lines.append("")

    current_pt = None

    for (project, task, date), data in groups.items():
        pt = (project, task)
        if pt != current_pt:
            if current_pt is not None:
                lines.append("=" * 60)
                lines.append("")
            current_pt = pt

        day_name = date.strftime("%A")
        date_str = date.strftime("%Y-%m-%d")

        lines.append(f"Project: {project}")
        if task:
            lines.append(f"Task: {task}")
        lines.append(f"Day: {day_name} {date_str}")
        lines.append(f"Hours: {data['total_hours']:.2f}")
        lines.append("")
        for desc, hrs in sorted(data["items"], key=lambda x: x[1], reverse=True):
            lines.append(f"{hrs:.2f}h - {desc}")
        lines.append("")

    # --- Summary table ---
    lines.append("=" * 60)
    lines.append("WEEKLY SUMMARY")
    lines.append("=" * 60)
    lines.append("")

    row_totals = defaultdict(lambda: {"days": defaultdict(float), "total": 0.0})
    all_dates = set()
    for (project, task, date), data in groups.items():
        label = spp_label(project, task)
        day_name = date.strftime("%A")
        row_totals[label]["days"][day_name] += data["total_hours"]
        row_totals[label]["total"] += data["total_hours"]
        all_dates.add(date)

    active_days = sorted(
        set(d.strftime("%A") for d in all_dates),
        key=lambda d: DAY_ORDER.index(d) if d in DAY_ORDER else 99,
    )

    col_w = 7
    proj_w = max((len(p) for p in row_totals), default=20) + 2
    header = (
        f"{'Project – Task':<{proj_w}}"
        + "".join(f"{d[:3]:>{col_w}}" for d in active_days)
        + f"{'TOTAL':>{col_w + 2}}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    grand_total = 0.0
    for label in sorted(row_totals):
        row = f"{label:<{proj_w}}"
        for day in active_days:
            hrs = row_totals[label]["days"].get(day, 0)
            row += f"{hrs:>{col_w}.2f}" if hrs else f"{'—':>{col_w}}"
        row += f"{row_totals[label]['total']:>{col_w + 2}.2f}"
        lines.append(row)
        grand_total += row_totals[label]["total"]

    lines.append("-" * len(header))
    total_row = f"{'TOTAL':<{proj_w}}"
    for day in active_days:
        day_sum = sum(rt["days"].get(day, 0) for rt in row_totals.values())
        total_row += f"{day_sum:>{col_w}.2f}"
    total_row += f"{grand_total:>{col_w + 2}.2f}"
    lines.append(total_row)

    return "\n".join(lines)


def format_html(groups: dict, start: datetime, end: datetime) -> str:
    """Build self-contained interactive HTML report."""

    # --- Build REPORT_DATA JSON from groups ---
    # Collect all unique dates and (project, task) pairs
    all_dates = sorted({date for (_, _, date) in groups})
    all_pts = []
    seen = set()
    for (project, task, _) in groups:
        key = (project, task)
        if key not in seen:
            seen.add(key)
            all_pts.append(key)

    day_names = [d.strftime("%A") for d in all_dates]
    date_strs = [d.strftime("%Y-%m-%d") for d in all_dates]

    rows_json = []
    for (project, task) in all_pts:
        cells = {}
        for date in all_dates:
            data = groups.get((project, task, date))
            if not data:
                continue
            day_name = date.strftime("%A")
            note_lines = []
            for desc, hrs in sorted(data["items"], key=lambda x: x[1], reverse=True):
                note_lines.append(f"{hrs:.2f}h - {desc}")
            cells[day_name] = {
                "hours": round(data["total_hours"], 2),
                "notes": "\n".join(note_lines),
            }
        rows_json.append({
            "project": project,
            "task": task,
            "cells": cells,
        })

    report_data = {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "days": day_names,
        "dates": date_strs,
        "rows": rows_json,
    }

    data_json = json.dumps(report_data, ensure_ascii=False, indent=2)

    return HTML_TEMPLATE.replace("__REPORT_DATA_PLACEHOLDER__", data_json)


# The HTML template with __REPORT_DATA_PLACEHOLDER__ where the JSON goes.
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SPP Time Report</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;1,9..40,400&family=JetBrains+Mono:wght@400;500&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  :root {
    --bg: #f6f5f1;
    --surface: #ffffff;
    --border: #e2e0db;
    --border-strong: #ccc9c1;
    --text: #1a1917;
    --text-secondary: #6b6860;
    --text-muted: #9b978e;
    --accent: #2d6a4f;
    --accent-light: #d4e7dd;
    --accent-hover: #1b4332;
    --cell-hover: #f0eeea;
    --copied-hrs: #e8f0fe;
    --copied-hrs-border: #a4c4f4;
    --copied-notes: #edf6f0;
    --copied-notes-border: #95d5ab;
    --modal-overlay: rgba(26, 25, 23, 0.4);
    --shadow-sm: 0 1px 2px rgba(0,0,0,0.06);
    --shadow-lg: 0 12px 48px rgba(0,0,0,0.15);
    --radius: 6px;
    --radius-lg: 10px;
  }

  body {
    font-family: 'DM Sans', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  .container { max-width: 1200px; margin: 0 auto; padding: 40px 24px; }

  .header { margin-bottom: 32px; }
  .header h1 { font-size: 22px; font-weight: 600; letter-spacing: -0.02em; margin-bottom: 6px; }
  .header .meta { font-size: 13px; color: var(--text-secondary); }
  .header .meta span + span::before { content: "\b7"; margin: 0 8px; color: var(--text-muted); }

  .progress-wrap { margin-top: 16px; display: flex; gap: 20px; align-items: center; }
  .progress-item { display: flex; align-items: center; gap: 8px; flex: 1; }
  .progress-item-label { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap; }
  .progress-item-label.hrs-label { color: #4a7ab5; }
  .progress-item-label.notes-label { color: var(--accent); }
  .progress-track { flex: 1; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 2px; transition: width 0.3s ease; width: 0%; }
  .progress-fill.hrs-fill { background: #7aade0; }
  .progress-fill.notes-fill { background: var(--accent); }
  .progress-count { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; white-space: nowrap; min-width: 52px; text-align: right; }

  .grid-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow-x: auto; box-shadow: var(--shadow-sm); }
  table { width: 100%; border-collapse: collapse; }

  thead th { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); padding: 12px 8px; text-align: center; border-bottom: 1px solid var(--border); background: var(--bg); white-space: nowrap; }
  thead th:first-child { text-align: left; padding-left: 20px; min-width: 200px; }
  thead th.col-total { background: transparent; font-weight: 600; color: var(--text-secondary); }

  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:last-child { border-bottom: none; }
  tbody td { padding: 0; vertical-align: middle; border-right: 1px solid var(--border); }
  tbody td:last-child { border-right: none; }
  tbody td:first-child { text-align: left; padding: 12px 20px; border-right: 1px solid var(--border-strong); }

  .row-label { font-size: 13px; line-height: 1.3; }
  .row-label-project { font-weight: 600; }
  .row-label-sep { color: var(--text-muted); font-weight: 400; margin: 0 2px; }
  .row-label-task { font-weight: 400; color: var(--text-secondary); }

  .entry-cell { position: relative; min-height: 52px; display: flex; align-items: stretch; }
  .entry-hrs { flex: 1; display: flex; align-items: center; justify-content: center; font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 500; color: var(--text); cursor: pointer; transition: background 0.1s; user-select: none; padding: 4px 2px 4px 8px; border-radius: 3px 0 0 3px; }
  .entry-hrs:hover { background: var(--cell-hover); }
  .entry-hrs[data-copied="true"] { background: var(--copied-hrs); }

  .entry-actions { display: flex; flex-direction: column; width: 30px; min-width: 30px; border-left: 1px solid var(--border); }
  .entry-btn-notes { flex: 1; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; background: transparent; font-size: 13px; color: var(--text-muted); transition: all 0.1s; padding: 0; }
  .entry-btn-notes:hover { background: var(--cell-hover); color: var(--text); }
  .entry-btn-notes[data-copied="true"] { background: var(--copied-notes); color: var(--accent); }
  .entry-btn-detail { display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; border-top: 1px solid var(--border); background: transparent; font-size: 11px; color: var(--text-muted); transition: all 0.1s; padding: 3px 0; line-height: 1; }
  .entry-btn-detail:hover { background: var(--cell-hover); color: var(--text); }

  .cell-empty { display: flex; align-items: center; justify-content: center; min-height: 52px; color: var(--text-muted); font-size: 12px; }

  td.col-total { background: var(--bg); font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 500; padding: 12px 10px; text-align: center; color: var(--text-secondary); }

  .row-total td { background: var(--bg); border-top: 1px solid var(--border-strong); padding: 12px 10px; font-weight: 600; font-size: 13px; }
  .row-total td:first-child { padding-left: 20px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-secondary); }
  .row-total td.col-total-num { font-family: 'JetBrains Mono', monospace; text-align: center; color: var(--text); }

  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(80px); background: var(--text); color: white; padding: 10px 20px; border-radius: var(--radius); font-size: 13px; font-weight: 500; opacity: 0; transition: all 0.2s ease; pointer-events: none; z-index: 200; white-space: nowrap; }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  .modal-overlay { display: none; position: fixed; inset: 0; background: var(--modal-overlay); z-index: 100; align-items: center; justify-content: center; backdrop-filter: blur(2px); }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); width: 520px; max-width: calc(100vw - 48px); max-height: calc(100vh - 80px); overflow: hidden; animation: modal-in 0.15s ease-out; position: relative; }
  @keyframes modal-in { from { opacity: 0; transform: translateY(8px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
  .modal-header { padding: 20px 24px 16px; border-bottom: 1px solid var(--border); }
  .modal-header h2 { font-size: 15px; font-weight: 600; margin-bottom: 2px; }
  .modal-header .modal-sub { font-size: 12px; color: var(--text-secondary); }
  .modal-body { padding: 16px 24px 20px; }
  .modal-field { margin-bottom: 16px; }
  .modal-field:last-child { margin-bottom: 0; }
  .modal-field-label { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 6px; }
  .modal-field-value { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; font-family: 'JetBrains Mono', monospace; font-size: 13px; line-height: 1.7; white-space: pre-wrap; }
  .modal-field-value.hours-value { display: inline-block; padding: 8px 14px; font-size: 18px; font-weight: 500; }
  .modal-actions { padding: 16px 24px; border-top: 1px solid var(--border); display: flex; gap: 10px; justify-content: flex-end; }
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: var(--radius); font-family: 'DM Sans', sans-serif; font-size: 13px; font-weight: 500; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; transition: all 0.12s; }
  .btn:hover { background: var(--bg); border-color: var(--border-strong); }
  .btn-primary { background: var(--accent); color: white; border-color: var(--accent); }
  .btn-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
  .btn .icon { font-size: 14px; line-height: 1; }
  .btn-copied { background: var(--accent-light) !important; border-color: var(--copied-notes-border) !important; color: var(--accent) !important; }
  .close-btn { position: absolute; top: 16px; right: 16px; background: none; border: none; font-size: 18px; color: var(--text-muted); cursor: pointer; padding: 4px; line-height: 1; }
  .close-btn:hover { color: var(--text); }
  .footer { margin-top: 24px; font-size: 12px; color: var(--text-muted); text-align: center; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>SPP Time Report</h1>
    <div class="meta"><span id="dateRange"></span><span id="generated"></span></div>
    <div class="progress-wrap">
      <div class="progress-item">
        <span class="progress-item-label hrs-label">Hours</span>
        <div class="progress-track"><div class="progress-fill hrs-fill" id="hrsFill"></div></div>
        <span class="progress-count" id="hrsCount">0 / 0</span>
      </div>
      <div class="progress-item">
        <span class="progress-item-label notes-label">Notes</span>
        <div class="progress-track"><div class="progress-fill notes-fill" id="notesFill"></div></div>
        <span class="progress-count" id="notesCount">0 / 0</span>
      </div>
    </div>
  </div>
  <div class="grid-wrap"><table id="grid"></table></div>
  <div class="footer">Click hours to copy time &middot; &#x1f4cb; copies notes &middot; &hellip; opens detail</div>
</div>
<div class="toast" id="toast"></div>
<div class="modal-overlay" id="overlay">
  <div class="modal">
    <button class="close-btn" id="closeBtn">&times;</button>
    <div class="modal-header"><h2 id="modalTitle"></h2><div class="modal-sub" id="modalSub"></div></div>
    <div class="modal-body">
      <div class="modal-field"><div class="modal-field-label">Hours</div><div class="modal-field-value hours-value" id="modalHours"></div></div>
      <div class="modal-field"><div class="modal-field-label">Notes</div><div class="modal-field-value" id="modalNotes"></div></div>
    </div>
    <div class="modal-actions">
      <button class="btn" id="mCopyHours"><span class="icon">&#x23f1;</span> Copy Hours</button>
      <button class="btn btn-primary" id="mCopyNotes"><span class="icon">&#x1f4cb;</span> Copy Notes</button>
    </div>
  </div>
</div>
<script>
const REPORT_DATA = __REPORT_DATA_PLACEHOLDER__;
const { days, rows } = REPORT_DATA;
let totalCells = 0;
const copiedHrs = new Set();
const copiedNotes = new Set();
document.getElementById('dateRange').textContent = `${REPORT_DATA.start} \u2192 ${REPORT_DATA.end}`;
document.getElementById('generated').textContent = `Generated ${REPORT_DATA.generated}`;

function buildGrid() {
  const table = document.getElementById('grid');
  rows.forEach(r => { days.forEach(d => { if (r.cells[d]) totalCells++; }); });
  let html = '<thead><tr><th>Project \u2013 Task</th>';
  days.forEach(d => { html += `<th>${d.slice(0,3)}</th>`; });
  html += '<th class="col-total">Total</th></tr></thead><tbody>';
  rows.forEach((r, ri) => {
    html += '<tr><td><div class="row-label">';
    html += `<span class="row-label-project">${esc(r.project)}</span>`;
    if (r.task) html += `<span class="row-label-sep"> \u2013 </span><span class="row-label-task">${esc(r.task)}</span>`;
    html += '</div></td>';
    let rowTotal = 0;
    days.forEach((d, di) => {
      const cell = r.cells[d];
      if (cell) {
        rowTotal += cell.hours;
        const key = `${ri}-${di}`;
        html += `<td><div class="entry-cell">`;
        html += `<div class="entry-hrs" data-key="${key}" data-action="hrs" title="Copy hours">${cell.hours.toFixed(2)}</div>`;
        html += `<div class="entry-actions">`;
        html += `<button class="entry-btn-notes" data-key="${key}" data-action="notes" title="Copy notes">\ud83d\udccb</button>`;
        html += `<button class="entry-btn-detail" data-key="${key}" data-action="detail" title="View detail">\u22ef</button>`;
        html += `</div></div></td>`;
      } else {
        html += '<td><div class="cell-empty">\u2014</div></td>';
      }
    });
    html += `<td class="col-total">${rowTotal.toFixed(2)}</td></tr>`;
  });
  html += '<tr class="row-total"><td>Total</td>';
  let grandTotal = 0;
  days.forEach(d => {
    let s = 0;
    rows.forEach(r => { if (r.cells[d]) s += r.cells[d].hours; });
    grandTotal += s;
    html += `<td class="col-total-num">${s ? s.toFixed(2) : '\u2014'}</td>`;
  });
  html += `<td class="col-total-num">${grandTotal.toFixed(2)}</td></tr></tbody>`;
  table.innerHTML = html;
  updateProgress();
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function parseKey(key) { const [ri, di] = key.split('-').map(Number); return { ri, di, day: days[di], row: rows[ri], cell: rows[ri].cells[days[di]] }; }

let toastTimer = null;
function showToast(msg) { const t = document.getElementById('toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 1400); }

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { const ta = document.createElement('textarea'); ta.value = text; ta.style.cssText = 'position:fixed;opacity:0'; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
}

function updateProgress() {
  const hPct = totalCells ? Math.round((copiedHrs.size / totalCells) * 100) : 0;
  const nPct = totalCells ? Math.round((copiedNotes.size / totalCells) * 100) : 0;
  document.getElementById('hrsFill').style.width = hPct + '%';
  document.getElementById('hrsCount').textContent = `${copiedHrs.size} / ${totalCells}`;
  document.getElementById('notesFill').style.width = nPct + '%';
  document.getElementById('notesCount').textContent = `${copiedNotes.size} / ${totalCells}`;
}

document.getElementById('grid').addEventListener('click', async (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const key = el.dataset.key;
  const action = el.dataset.action;
  const { ri, di, day, row, cell } = parseKey(key);
  const label = row.task ? `${row.project} \u2013 ${row.task}` : row.project;
  if (action === 'hrs') {
    const val = cell.hours.toFixed(2);
    await copyText(val);
    copiedHrs.add(key);
    el.dataset.copied = "true";
    showToast(`${val}h copied`);
    updateProgress();
  } else if (action === 'notes') {
    await copyText(cell.notes);
    copiedNotes.add(key);
    el.dataset.copied = "true";
    showToast(`Notes copied \u2013 ${label}, ${day.slice(0,3)}`);
    updateProgress();
  } else if (action === 'detail') {
    openModal(ri, di);
  }
});

const overlay = document.getElementById('overlay');
let modalKey = null;
function openModal(ri, di) {
  const key = `${ri}-${di}`;
  modalKey = key;
  const { day, row, cell } = parseKey(key);
  const date = REPORT_DATA.dates[di];
  const label = row.task ? `${row.project} \u2013 ${row.task}` : row.project;
  document.getElementById('modalTitle').textContent = label;
  document.getElementById('modalSub').textContent = `${day} ${date}`;
  document.getElementById('modalHours').textContent = cell.hours.toFixed(2);
  document.getElementById('modalNotes').textContent = cell.notes;
  resetModalBtn('mCopyHours', '\u23f1', 'Copy Hours');
  resetModalBtn('mCopyNotes', '\ud83d\udccb', 'Copy Notes');
  overlay.classList.add('open');
}
function closeModal() { overlay.classList.remove('open'); modalKey = null; }
overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
document.getElementById('closeBtn').addEventListener('click', closeModal);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

document.getElementById('mCopyHours').addEventListener('click', async () => {
  if (!modalKey) return;
  const { cell } = parseKey(modalKey);
  const val = cell.hours.toFixed(2);
  await copyText(val);
  copiedHrs.add(modalKey);
  const hrsEl = document.querySelector(`.entry-hrs[data-key="${modalKey}"]`);
  if (hrsEl) hrsEl.dataset.copied = "true";
  flashModalBtn('mCopyHours', '\u23f1', 'Copy Hours');
  showToast(`${val}h copied`);
  updateProgress();
});
document.getElementById('mCopyNotes').addEventListener('click', async () => {
  if (!modalKey) return;
  const { cell } = parseKey(modalKey);
  await copyText(cell.notes);
  copiedNotes.add(modalKey);
  const notesEl = document.querySelector(`.entry-btn-notes[data-key="${modalKey}"]`);
  if (notesEl) notesEl.dataset.copied = "true";
  flashModalBtn('mCopyNotes', '\ud83d\udccb', 'Copy Notes');
  showToast('Notes copied');
  updateProgress();
});
function flashModalBtn(id, icon, label) { const btn = document.getElementById(id); btn.classList.add('btn-copied'); btn.innerHTML = `<span class="icon">\u2713</span> Copied`; setTimeout(() => resetModalBtn(id, icon, label), 1500); }
function resetModalBtn(id, icon, label) { const btn = document.getElementById(id); btn.classList.remove('btn-copied'); btn.innerHTML = `<span class="icon">${icon}</span> ${label}`; }

buildGrid();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def die(msg: str):
    eprint(f"Error: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Transform Clockify time entries into SPP-ready format.",
        epilog="Examples:\n"
        "  %(prog)s this-week\n"
        "  %(prog)s last-week\n"
        "  %(prog)s 2025-06-02 2025-06-08\n"
        "  %(prog)s last-week --stdout\n"
        "  %(prog)s last-week --csv export.csv\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "period",
        help="'this-week', 'last-week', or start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "end_date",
        nargs="?",
        default=None,
        help="End date (YYYY-MM-DD) when using date range mode",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to config JSON (default: {DEFAULT_CONFIG_NAME} next to script)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        metavar="FILE",
        help="Use CSV file instead of Clockify API",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout only, don't write file",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the HTML report in the default browser after generating",
    )
    args = parser.parse_args()

    # Load config (needed even for CSV mode, for output_dir and ignored_ids)
    config_path = find_config(args.config)
    cfg = load_config(config_path)

    # Resolve date range
    start, end = resolve_date_range(args)
    eprint(f"Date range: {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}")

    # Fetch entries
    if args.csv:
        ignored = set(cfg.get("ignored_project_ids", []))
        ignored_clients = {c.lower() for c in cfg.get("ignored_client_names", [])}
        entries = load_entries_csv(args.csv, start, end, ignored, ignored_clients)
    else:
        entries = fetch_time_entries(cfg, start, end)

    if not entries:
        die("No entries found for the specified period.")

    # Aggregate and format
    groups = aggregate(entries)
    output_txt = format_spp(groups, start, end)
    output_html = format_html(groups, start, end)

    # Output
    if args.stdout:
        print(output_txt)
    else:
        # Subfolder per week, named by the Monday of the start date's week
        week_monday = start - timedelta(days=start.weekday())
        week_folder = f"week_{week_monday.strftime('%Y-%m-%d')}"

        raw_out = Path(cfg["output_dir"])
        out_dir = (Path(__file__).resolve().parent / raw_out if not raw_out.is_absolute() else raw_out) / week_folder
        out_dir.mkdir(parents=True, exist_ok=True)
        base = f"spp_{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"

        txt_path = out_dir / f"{base}.txt"
        txt_path.write_text(output_txt, encoding="utf-8")

        html_path = out_dir / f"{base}.html"
        html_path.write_text(output_html, encoding="utf-8")

        eprint(f"Written to {txt_path}")
        eprint(f"Written to {html_path}")
        print(output_txt)

        if args.open:
            import webbrowser
            webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()