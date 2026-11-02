"""
Clockify operations the autofill command needs beyond what spp_export.py has.

Builds on the existing client in spp_export.py (api_get, the SSL context,
die) rather than a second client. Converts between Clockify's UTC and local
minutes-since-midnight at this edge.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from spp_export import API_BASE, _get_ssl_context, api_get, die, eprint
from autofill_plan import Block

# The Free plan allows 30 API requests per hour, so every call here is kept to
# one request: name-filtered lookups, hydrated entry listing, bulk delete.
FREE_PLAN_HOURLY_LIMIT = 30


# ---------------------------------------------------------------------------
# Writes (api_get only does GET)
# ---------------------------------------------------------------------------

def api_send(method: str, path: str, api_key: str, body: dict | None = None,
             params: dict | None = None):
    """POST/PUT/DELETE to the Clockify API. Returns parsed JSON or None."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_get_ssl_context()) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        die(f"Clockify API error {e.code} on {method} {path}: {detail}")
    except urllib.error.URLError as e:
        die(f"Network error calling Clockify API: {e.reason}")


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------

def local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def to_utc_iso(day: date, minutes: int, tz: ZoneInfo) -> str:
    dt = local_midnight(day, tz) + timedelta(minutes=minutes)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Operations, bound to the key/workspace from spp_config.json
# ---------------------------------------------------------------------------

class ClockifyOps:
    def __init__(self, api_key: str, workspace_id: str, tz: ZoneInfo):
        self.api_key = api_key
        self.ws = workspace_id
        self.tz = tz
        self.requests = 0
        self._user_id = None
        self._catalog: dict[str, dict] = {}
        self._tags: dict[str, str | None] = {}

    def _get(self, path: str, params: dict | None = None):
        self.requests += 1
        return api_get(path, self.api_key, params)

    def _send(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        self.requests += 1
        return api_send(method, path, self.api_key, body, params)

    @property
    def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = self._get("/user")["id"]
        return self._user_id

    # --- projects and tasks ---------------------------------------------

    def project_catalog(self, project_names: set[str]) -> dict[str, dict]:
        """
        {project_name: {"id", "client", "tasks": {task_name: task_id}}} for the
        named active projects that exist, with their active tasks.
        Two requests per project name.
        """
        catalog = {}
        for name in sorted(project_names):
            found = self._get(f"/workspaces/{self.ws}/projects",
                              {"name": name, "strict-name-search": "true", "archived": "false",
                               "page-size": 50})
            match = next((p for p in found if p.get("name") == name), None)
            if match is None:
                continue
            tasks = self._get(f"/workspaces/{self.ws}/projects/{match['id']}/tasks",
                              {"is-active": "true", "page-size": 500})
            catalog[name] = {
                "id": match["id"],
                "client": match.get("clientName") or "",
                "tasks": {t["name"]: t["id"] for t in tasks},
            }
        self._catalog = catalog
        return catalog

    # --- tags -------------------------------------------------------------

    def find_tag(self, name: str) -> str | None:
        if name not in self._tags:
            tags = self._get(f"/workspaces/{self.ws}/tags",
                             {"name": name, "strict-name-search": "true", "page-size": 50})
            self._tags[name] = next((t["id"] for t in tags if t.get("name") == name), None)
        return self._tags[name]

    def find_or_create_tag(self, name: str) -> str:
        tag_id = self.find_tag(name)
        if tag_id:
            return tag_id
        eprint(f"Creating Clockify tag '{name}'...")
        self._tags[name] = self._send("POST", f"/workspaces/{self.ws}/tags", {"name": name})["id"]
        return self._tags[name]

    # --- entries ----------------------------------------------------------

    def list_entries(self, first: date, last: date) -> list[dict]:
        """Raw entries of the current user starting between local first 00:00 and last 24:00."""
        start = local_midnight(first, self.tz).astimezone(timezone.utc)
        end = local_midnight(last + timedelta(days=1), self.tz).astimezone(timezone.utc)
        out, page = [], 1
        while True:
            batch = self._get(
                f"/workspaces/{self.ws}/user/{self.user_id}/time-entries",
                {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "hydrated": "true", "page": page, "page-size": 500},
            )
            out.extend(batch)
            if len(batch) < 500:
                break
            page += 1
        return out

    def create_entry(self, day: date, block: Block, project_id: str, task_id: str,
                     tag_ids: list[str]) -> None:
        self._send("POST", f"/workspaces/{self.ws}/time-entries", {
            "start": to_utc_iso(day, block.start, self.tz),
            "end": to_utc_iso(day, block.end, self.tz),
            "description": block.desc,
            "projectId": project_id,
            "taskId": task_id,
            "tagIds": tag_ids,
        })

    def delete_entries(self, entry_ids: list[str]) -> None:
        """Bulk delete in one request."""
        if entry_ids:
            self._send("DELETE", f"/workspaces/{self.ws}/user/{self.user_id}/time-entries",
                       params={"time-entry-ids": entry_ids})

    # --- conversion -------------------------------------------------------

    def entries_by_day(self, raw: list[dict], tag_id: str | None
                       ) -> dict[date, tuple[list[Block], list[Block]]]:
        """Group raw entries by local start date into (manual, generated) blocks."""
        projects = {p["id"]: name for name, p in self._catalog.items()}
        tasks = {tid: tname for p in self._catalog.values() for tname, tid in p["tasks"].items()}

        def name_of(e: dict, kind: str, known: dict) -> str:
            # Hydrated entries carry the project/task object; fall back to the catalog, then the id.
            obj = e.get(kind) if isinstance(e.get(kind), dict) else {}
            ident = e.get(f"{kind}Id")
            return obj.get("name") or known.get(ident) or ident or ""

        now = datetime.now(timezone.utc)
        out: dict[date, tuple[list[Block], list[Block]]] = {}
        for e in raw:
            ti = e.get("timeInterval") or {}
            if not ti.get("start"):
                continue
            start = _parse_utc(ti["start"]).astimezone(self.tz)
            end = (_parse_utc(ti["end"]) if ti.get("end") else now).astimezone(self.tz)
            day = start.date()
            midnight = local_midnight(day, self.tz)
            block = Block(
                start=round((start - midnight).total_seconds() / 60),
                end=round((end - midnight).total_seconds() / 60),
                project=name_of(e, "project", projects) or "(no project)",
                task=name_of(e, "task", tasks),
                desc=(e.get("description") or "").strip(),
                source="manual",
                action="keep",
                entry_id=e.get("id"),
            )
            manual, generated = out.setdefault(day, ([], []))
            (generated if tag_id and tag_id in (e.get("tagIds") or []) else manual).append(block)
        return out


def recent_filler_default(by_day: dict[date, tuple[list[Block], list[Block]]], before: date,
                          project: str, task: str, lookback_days: int = 14) -> str | None:
    """Descriptions of the most recent generated filler entries within the lookback window."""
    for offset in range(1, lookback_days + 1):
        day = before - timedelta(days=offset)
        _, generated = by_day.get(day, ([], []))
        descs = []
        for b in sorted(generated, key=lambda b: b.start):
            if b.project == project and b.task == task and b.desc and b.desc not in descs:
                descs.append(b.desc)
        if descs:
            return "; ".join(descs)
    return None
