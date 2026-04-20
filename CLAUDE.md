# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Exports Clockify time entries and formats them for manual entry into SPP (the official timesheet system). Entries are grouped by **Project + Task + Day** to match SPP's structure, with duplicate descriptions merged. Output is a text report and an interactive HTML file with one-click copy-to-clipboard.

## Running the Script

```bash
cd src
python spp_export.py last-week          # Last Mon–Sun
python spp_export.py this-week          # Current week so far
python spp_export.py 2025-06-02 2025-06-08  # Custom date range
python spp_export.py last-week --open   # Auto-open HTML in browser
python spp_export.py last-week --stdout # Print to stdout only, no file saved
python spp_export.py last-week --csv clockify_export.csv  # Use CSV instead of API
```

Windows one-click shortcuts in `scripts/run-last-week.bat` and `scripts/run-this-week.bat` — these run the export and open the HTML report automatically.

## Configuration

`src/spp_config.json` (gitignored — never committed). Required fields:

```json
{
  "api_key": "YOUR_CLOCKIFY_API_KEY",
  "workspace_id": "YOUR_WORKSPACE_ID",
  "ignored_project_ids": [],
  "ignored_client_names": ["Triage"],
  "output_dir": "./spp_reports"
}
```

`ignored_client_names` is case-insensitive and works with both API and CSV. `ignored_project_ids` only works with the API.

## Dependencies

**Python 3.10+ only. No third-party packages required** — stdlib only (`urllib`, `json`, `csv`, `argparse`, `datetime`, `ssl`). Optionally `pip install certifi` if Windows SSL errors occur.

## Architecture

All logic lives in a single file: `src/spp_export.py` (~1090 lines).

Data flow:
1. **Config** — `load_config()` reads `spp_config.json`, validates required fields
2. **Date range** — `resolve_date_range()` converts `this-week`/`last-week`/custom args to UTC datetimes
3. **Fetch** — Clockify API (paginated: user → projects → tasks → entries) or CSV fallback via `load_entries_csv()`; both paths produce the same normalized list of `{project, task, date, hours, description}` dicts
4. **Aggregate** — `aggregate()` groups by `(project, task, date)` and merges duplicate descriptions
5. **Format** — `format_spp()` produces the text report; `format_html()` produces the interactive HTML (self-contained, inline CSS+JS, no external dependencies at runtime)
6. **Output** — saved to `spp_reports/week_YYYY-MM-DD/` (organized by Monday of the week), and optionally opened in browser

The HTML report includes click-to-copy for hours and notes, a progress tracker, and a detail modal — all embedded inline in the generated file.
