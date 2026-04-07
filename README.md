# SPP Export

Transforms Clockify time entries into copy-paste-ready text for SPP timesheet entry.

## Why this exists

After our acquisition, SPP became the sole official time tracking system. But SPP's manual entry UI doesn't support the kind of granular, throughout-the-day logging that fragmented work demands — especially when you're touching 2-4 projects daily with a mix of meetings, coding, review, and discovery.

This script bridges the gap: keep logging in Clockify (fast input, running timers, mobile), then run a single command on Monday to get SPP-formatted output with aggregated hours and notes per project/task/day.

## What it does

- Pulls time entries from the Clockify API (or reads a CSV export as fallback)
- Groups entries by **Project + Task + Day** — matching SPP's "Client: Project" and "Task" fields exactly
- Merges duplicate descriptions within the same group
- Produces a text report with:
  - Per-entry detail blocks (project, task, day, hours, itemized notes) ready to copy into SPP
  - A weekly summary table for sanity-checking totals before entry
- Saves reports to a local folder as an audit trail

## Setup

**Requirements:** Python 3.10+ (stdlib only, no pip dependencies)

1. Clone or copy the files into a folder
2. Copy the example config:
   ```
   cp spp_config.example.json spp_config.json
   ```
3. Edit `spp_config.json` with your credentials:
   ```json
   {
     "api_key": "YOUR_CLOCKIFY_API_KEY",
     "workspace_id": "YOUR_WORKSPACE_ID",
     "ignored_project_ids": [],
     "ignored_client_names": [],
     "output_dir": "./spp_reports"
   }
   ```
4. Get your API key: Clockify → Profile icon → Profile Settings → scroll to API section → Generate
5. Get your workspace ID: Clockify → Settings → the ID is in the URL, or via `GET /api/v1/workspaces`

`spp_config.json` is gitignored by default. Don't commit it.

## Usage

You must always specify a time period. There is no default.

```bash
# Last week (Monday-Sunday) — the typical Monday morning workflow
python spp_export.py last-week

# Current week so far
python spp_export.py this-week

# Explicit date range (inclusive)
python spp_export.py 2025-06-02 2025-06-08

# Print to stdout only, don't save a file
python spp_export.py last-week --stdout

# Use a CSV export instead of the API
python spp_export.py last-week --csv clockify_export.csv

# Use a different config file
python spp_export.py last-week --config /path/to/other_config.json
```

**Default behavior:** writes to `./spp_reports/spp_YYYY-MM-DD_YYYY-MM-DD.txt` AND prints to stdout.

## Output format

Each project/task/day combination produces a block like this:

```
Project: PIP
Task: Medical PIP
Day: Monday 2025-06-02
Hours: 3.5

- Sprint planning meeting (0.5h)
- Implement claims validation logic (2h)
- Discovery: evaluate caching strategy options (1h)
```

Copy the relevant fields into SPP:
- **Project** and **Task** lines map to SPP's two selection fields
- **Hours** is your total for that row
- The bullet list is your SPP note

A summary table is appended at the bottom:

```
Project / Task                  Mon   Tue   Wed   Thu   Fri   TOTAL
-------------------------------------------------------------------
PIP / Dental PIP                1.5   1.0     —   1.0   1.5     5.0
PIP / Medical PIP               2.5   3.2     —   2.2   1.5     9.5
Platform / Platform Backend       —     —   2.0     —   1.5     3.5
-------------------------------------------------------------------
TOTAL                           4.0   4.2   2.0   3.2   4.5    18.0
```

## Configuration

| Field | Required | Description |
|---|---|---|
| `api_key` | Yes | Your personal Clockify API key |
| `workspace_id` | Yes | Clockify workspace ID |
| `ignored_project_ids` | No | Array of Clockify project IDs to exclude from output |
| `ignored_client_names` | No | Array of Clockify client names to exclude (case-insensitive) |
| `output_dir` | No | Where to save reports. Default: `./spp_reports` next to the script |

### Finding IDs to ignore

**Project IDs:** Clockify → project settings → the ID is in the URL. Or use the API:
```bash
curl -H "X-Api-Key: YOUR_KEY" \
  "https://api.clockify.me/api/v1/workspaces/YOUR_WS_ID/projects" | python -m json.tool
```

**Client names:** use the exact name as it appears in Clockify (matching is case-insensitive).

## CSV fallback

If the Clockify API is unreachable or you prefer offline use:

1. Go to Clockify → Reports → Detailed
2. Set the date range
3. Export as CSV
4. Run with `--csv`:
   ```
   python spp_export.py 2025-06-02 2025-06-08 --csv clockify_export.csv
   ```

Note: `ignored_project_ids` won't work in CSV mode (the CSV uses names, not IDs). `ignored_client_names` works in both modes — it matches against the CSV's `Client` column.

## File structure

```
├── spp_export.py              # The script
├── spp_config.example.json    # Template — copy to spp_config.json
├── spp_config.json            # Your config (gitignored)
├── .gitignore                 # Ignores config and reports
└── spp_reports/               # Generated reports (gitignored)
    ├── spp_2025-06-02_2025-06-08.txt
    ├── spp_2025-06-09_2025-06-15.txt
    └── ...
```

## Clockify conventions that matter

The script assumes your Clockify is structured to mirror SPP:

- **Clockify Project** = SPP "Client: Project" dropdown
- **Clockify Task** = SPP "Task" dropdown
- **Clockify Description** = free text that becomes the SPP note

If your descriptions for the same activity vary ("standup" vs "Standup" vs "Daily standup"), they won't merge — they'll appear as separate line items. Pick a convention and stick with it.

## Timezone handling

The Clockify API returns timestamps in UTC. The script converts to your system's local timezone for date grouping, so an entry at 23:30 UTC will land on the correct local day. This works automatically as long as your OS timezone is set correctly.

## Troubleshooting

**SSL error on Windows (`ASN1 nested asn1 error`)**
Your Windows certificate store has a malformed certificate that Python can't parse. Fix: `pip install certifi` — the script will detect and use it automatically.

## Roadmap / potential improvements

**Short-term (if the copy-paste workflow proves annoying):**
- `--interactive` mode: walks through each SPP block one at a time, copying each to clipboard, waiting for Enter before advancing. Turns 5 minutes of reading-and-copying into 2 minutes of tab-paste-enter.
- `--day` flag for single-day output (mid-week spot checks)

**Medium-term (if SPP integration access is granted):**
- Direct SPP API submission — skip the copy-paste entirely
- Dry-run mode that shows what would be submitted without actually doing it
- Diff mode: compare what's already in SPP vs what Clockify says, flag discrepancies

**Medium-term (if SPP integration access is NOT granted):**
- Playwright browser automation to fill SPP forms directly
- Would need to handle: SSO/auth flow, project/task dropdown selection, hours + notes entry per row
- Feasibility depends on SPP's UI stability — aria labels and clean selectors make this viable; frequent UI changes make it fragile
- Session/cookie caching to avoid re-authenticating every run

**Nice-to-haves (no urgency):**
- Structured JSON output (`--format json`) for downstream tooling
- Monthly summary mode for the 1st-of-month SPP deadline
- Detect and warn on days with suspiciously low/high total hours
- Description templates or aliases (e.g., `su` expands to "Standup" in output)