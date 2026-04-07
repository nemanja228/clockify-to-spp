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
    return f"{project} / {task}" if task else project


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
        lines.append(f"Hours: {data['total_hours']}")
        lines.append("")
        for desc, hrs in data["items"]:
            lines.append(f"- {desc} ({hrs}h)")
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

    col_w = 6
    proj_w = max((len(p) for p in row_totals), default=20) + 2
    header = (
        f"{'Project / Task':<{proj_w}}"
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
            row += f"{hrs:>{col_w}.1f}" if hrs else f"{'—':>{col_w}}"
        row += f"{row_totals[label]['total']:>{col_w + 2}.1f}"
        lines.append(row)
        grand_total += row_totals[label]["total"]

    lines.append("-" * len(header))
    total_row = f"{'TOTAL':<{proj_w}}"
    for day in active_days:
        day_sum = sum(rt["days"].get(day, 0) for rt in row_totals.values())
        total_row += f"{day_sum:>{col_w}.1f}"
    total_row += f"{grand_total:>{col_w + 2}.1f}"
    lines.append(total_row)

    return "\n".join(lines)


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
    output = format_spp(groups, start, end)

    # Output
    if args.stdout:
        print(output)
    else:
        out_dir = Path(cfg["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"spp_{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}.txt"
        out_path = out_dir / filename
        out_path.write_text(output, encoding="utf-8")
        eprint(f"Written to {out_path}")
        # Also print to stdout for convenience
        print(output)


if __name__ == "__main__":
    main()