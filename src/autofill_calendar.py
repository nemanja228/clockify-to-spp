"""
Calendar import for the Clockify autofill command.

Reads published ICS feeds, expands recurring events (moved and cancelled
occurrences included), converts times to the configured zone and drops
events that should never become entries. A feed that cannot be fetched or
parsed produces a warning and is skipped. Feed URLs are credentials: they are
never printed, only the feed name.
"""

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

from spp_export import _get_ssl_context, eprint
from autofill_config import Calendar, Config

try:  # Installed with recurring-ical-events; normalises Google's X-WR-TIMEZONE feeds.
    import x_wr_timezone
except ImportError:  # pragma: no cover
    x_wr_timezone = None


@dataclass
class CalEvent:
    feed: str
    day: date
    start: int          # minutes since local midnight of `day`
    end: int
    title: str


@dataclass
class FeedReport:
    name: str
    ok: bool = False
    events: int = 0             # timed, in range, before filtering
    with_attendees: int = 0     # events that list any ATTENDEE
    with_my_status: int = 0     # events where one of my_emails has a PARTSTAT
    declined: int = 0
    all_day: int = 0
    cancelled: int = 0
    problem: str = ""


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def feed_url(cal: Calendar, spp_cfg: dict | None) -> str | None:
    """The environment (including .env) wins; then a key of the same name in spp_config.json."""
    return os.environ.get(cal.url_env) or (spp_cfg or {}).get(cal.url_env) or None


def fetch_feed(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "clockify-autofill"})
    with urllib.request.urlopen(req, timeout=30, context=_get_ssl_context()) as resp:
        return resp.read()


def safe_error(e: Exception) -> str:
    """Describe a failure without echoing anything that could contain the URL."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network error ({type(e.reason).__name__})"
    return type(e).__name__


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _to_local(value, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:            # floating time: read as local
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _attendees(component) -> list:
    att = component.get("ATTENDEE")
    if att is None:
        return []
    return att if isinstance(att, list) else [att]


def parse_feed(data: bytes, name: str, first: date, last: date, tz: ZoneInfo,
               my_emails: list[str]) -> tuple[list[CalEvent], FeedReport]:
    report = FeedReport(name=name)
    cal = icalendar.Calendar.from_ical(data)
    if x_wr_timezone is not None:
        cal = x_wr_timezone.to_standard(cal)
    start = datetime.combine(first, time(0, 0), tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), time(0, 0), tzinfo=tz)
    mine = {e.strip().lower() for e in my_emails}

    events = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        if not isinstance(dtstart.dt, datetime):
            report.all_day += 1
            continue
        report.events += 1
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            report.cancelled += 1
            continue

        attendees = _attendees(comp)
        if attendees:
            report.with_attendees += 1
        declined, has_status = False, False
        for a in attendees:
            email = str(a).lower().removeprefix("mailto:").strip()
            if email in mine and "PARTSTAT" in getattr(a, "params", {}):
                has_status = True
                declined = declined or str(a.params["PARTSTAT"]).upper() == "DECLINED"
        report.with_my_status += has_status
        if declined:
            report.declined += 1
            continue

        s = _to_local(dtstart.dt, tz)
        if comp.get("DTEND") is not None:
            e = _to_local(comp["DTEND"].dt, tz)
        elif comp.get("DURATION") is not None:
            e = s + comp["DURATION"].dt
        else:
            e = s
        if e <= s:
            continue
        midnight = datetime.combine(s.date(), time(0, 0), tzinfo=tz)
        events.append(CalEvent(
            feed=name,
            day=s.date(),
            start=round((s - midnight).total_seconds() / 60),
            end=round((e - midnight).total_seconds() / 60),
            title=str(comp.get("SUMMARY", "")).strip() or "(no title)",
        ))
    report.ok = True
    return events, report


def dedupe(events: list[CalEvent]) -> list[CalEvent]:
    """Same title (trimmed, lowercased) and same start = same event; keep the first."""
    seen, out = set(), []
    for ev in events:
        key = (ev.title.strip().lower(), ev.day, ev.start)
        if key not in seen:
            seen.add(key)
            out.append(ev)
    return out


def load_calendars(cfg: Config, spp_cfg: dict | None, first: date, last: date,
                   fetch=fetch_feed) -> tuple[list[CalEvent], list[FeedReport]]:
    """Fetch every configured feed once for the whole range. Failures warn and continue."""
    events, reports = [], []
    for cal in cfg.calendars:
        url = feed_url(cal, spp_cfg)
        if not url:
            eprint(f"Warning: calendar '{cal.name}': {cal.url_env} is not set "
                   f"(environment, .env or spp_config.json); continuing without it.")
            reports.append(FeedReport(cal.name, problem=f"{cal.url_env} not set"))
            continue
        try:
            data = fetch(url)
        except Exception as e:
            eprint(f"Warning: calendar '{cal.name}' could not be fetched ({safe_error(e)}); "
                   f"continuing without it.")
            reports.append(FeedReport(cal.name, problem=f"fetch failed: {safe_error(e)}"))
            continue
        try:
            evs, report = parse_feed(data, cal.name, first, last, cfg.tz, cfg.my_emails)
        except Exception as e:
            eprint(f"Warning: calendar '{cal.name}' could not be parsed ({type(e).__name__}); "
                   f"continuing without it.")
            reports.append(FeedReport(cal.name, problem=f"parse failed: {type(e).__name__}"))
            continue
        events.extend(evs)
        reports.append(report)
    events.sort(key=lambda ev: (ev.day, ev.start, ev.end))
    return dedupe(events), reports
