"""
Configuration for the Clockify autofill command (autofill.py).

Loads and validates the YAML template, the learned event rules and the
optional .env file. No network calls here.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


DEFAULT_CONFIG_NAME = "autofill.yaml"
LEARNED_RULES_NAME = "rules.learned.yaml"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ACTIONS = ("ignore", "accept", "ask")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_DUR_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")


def parse_duration(value, where: str = "duration") -> int:
    """Parse '15m', '1h', '1h30m' (or 0) into whole minutes."""
    if isinstance(value, int) and not isinstance(value, bool) and value == 0:
        return 0
    text = str(value).strip().lower()
    m = _DUR_RE.match(text)
    if not text or not m or (m.group(1) is None and m.group(2) is None):
        raise ConfigError(f"{where}: invalid duration {value!r} (use forms like 15m, 1h, 1h30m)")
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def parse_clock(value, where: str = "time") -> int:
    """Parse 'HH:MM' into minutes since midnight."""
    # Unquoted 12:30 in YAML 1.1 is a base-60 integer (750), which is already minutes.
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 24 * 60:
            return value
        raise ConfigError(f"{where}: invalid time {value!r} (use \"HH:MM\")")
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(value).strip())
    if not m or int(m.group(1)) > 24 or int(m.group(2)) > 59:
        raise ConfigError(f"{where}: invalid time {value!r} (use \"HH:MM\")")
    return int(m.group(1)) * 60 + int(m.group(2))


def fmt_minutes(minutes: int) -> str:
    """Duration as H:MM, with a sign for negatives."""
    sign = "-" if minutes < 0 else ""
    minutes = abs(int(minutes))
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def fmt_clock(minutes: int) -> str:
    """Minutes since midnight as HH:MM (past 24:00 stays e.g. 24:15)."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

@dataclass
class Item:
    desc: str
    project: str
    task: str
    dur: int
    at: int | None = None           # anchored when set
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    jitter: int | None = None       # None = use the config default
    tags: list[str] = field(default_factory=list)

    @property
    def anchored(self) -> bool:
        return self.at is not None


@dataclass
class Break:
    at: int
    dur: int


@dataclass
class Target:
    project: str
    task: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Calendar:
    name: str
    url_env: str


@dataclass
class Rule:
    match: str
    action: str
    project: str | None = None
    task: str | None = None
    tags: list[str] = field(default_factory=list)
    learned: bool = False

    def __post_init__(self):
        self.pattern = re.compile(self.match, re.IGNORECASE)


@dataclass
class Config:
    path: Path
    timezone: str
    tz: ZoneInfo
    target: int
    day_start: int
    day_end: int
    round_to: int
    jitter: int
    generated_tag: str
    breaks: list[Break]
    items: list[Item]
    filler: Target
    meetings_default: Target
    skip_dates: set[date]
    calendars: list[Calendar]
    my_emails: list[str]
    event_rules: list[Rule]
    default_action: str
    learned_rules: list[Rule] = field(default_factory=list)

    @property
    def learned_rules_path(self) -> Path:
        return self.path.parent / LEARNED_RULES_NAME

    def project_task_pairs(self) -> list[tuple[str, str, str]]:
        """Every (project, task, where) the config can write entries to."""
        pairs = [(i.project, i.task, f"item '{i.desc}'") for i in self.items]
        pairs.append((self.filler.project, self.filler.task, "filler"))
        pairs.append((self.meetings_default.project, self.meetings_default.task, "meetings_default"))
        for r in self.event_rules + self.learned_rules:
            if r.action == "accept":
                p, t = self.rule_target(r)
                pairs.append((p, t, f"event rule '{r.match}'"))
        return pairs

    def rule_target(self, rule: Rule) -> tuple[str, str]:
        return (rule.project or self.meetings_default.project,
                rule.task or self.meetings_default.task)


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

_KNOWN_KEYS = {
    "timezone", "target", "day_start", "day_end", "round_to", "jitter", "generated_tag",
    "breaks", "items", "filler", "meetings_default", "skip_dates", "calendars",
    "my_emails", "event_rules", "default_action",
}


def _require(d: dict, key: str, where: str):
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field '{key}'")
    return d[key]


def _tags(d: dict, where: str) -> list[str]:
    tags = d.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ConfigError(f"{where}: 'tags' must be a list of names")
    return tags


def _target(d, where: str) -> Target:
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: must be a mapping with project and task")
    return Target(str(_require(d, "project", where)), str(_require(d, "task", where)), _tags(d, where))


def _parse_date(value, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{where}: invalid date {value!r} (use YYYY-MM-DD)")


def _parse_rule(d, where: str, learned: bool = False) -> Rule:
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: must be a mapping")
    match = str(_require(d, "match", where))
    action = str(_require(d, "action", where))
    if action not in ACTIONS:
        raise ConfigError(f"{where}: action must be one of {', '.join(ACTIONS)}, got {action!r}")
    try:
        return Rule(match, action, d.get("project"), d.get("task"), _tags(d, where), learned)
    except re.error as e:
        raise ConfigError(f"{where}: invalid regular expression {match!r}: {e}")


def parse_config(raw: dict, path: Path) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown field(s): {', '.join(sorted(unknown))}")

    tz_name = str(raw.get("timezone") or "Europe/Belgrade")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"timezone: unknown time zone {tz_name!r}")

    target = parse_duration(_require(raw, "target", "config"), "target")
    day_start = parse_clock(raw.get("day_start", "09:00"), "day_start")
    day_end = parse_clock(raw.get("day_end", "17:30"), "day_end")
    if day_end <= day_start:
        raise ConfigError("day_end must be after day_start")
    round_to = parse_duration(raw.get("round_to", "5m"), "round_to")
    if round_to <= 0:
        raise ConfigError("round_to must be at least 1m")
    jitter = parse_duration(raw.get("jitter", 0), "jitter")

    breaks = []
    for i, b in enumerate(raw.get("breaks") or []):
        where = f"breaks[{i}]"
        if not isinstance(b, dict):
            raise ConfigError(f"{where}: must be a mapping with at and dur")
        breaks.append(Break(parse_clock(_require(b, "at", where), f"{where}.at"),
                            parse_duration(_require(b, "dur", where), f"{where}.dur")))

    items = []
    for i, d in enumerate(raw.get("items") or []):
        where = f"items[{i}]"
        if not isinstance(d, dict):
            raise ConfigError(f"{where}: must be a mapping")
        desc = str(_require(d, "desc", where))
        where = f"item '{desc}'"
        days = [0, 1, 2, 3, 4]
        if d.get("days") is not None:
            names = d["days"] if isinstance(d["days"], list) else [d["days"]]
            full = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            try:
                days = [WEEKDAYS.index(n) if n in WEEKDAYS else full.index(n)
                        for n in (str(x).strip().lower() for x in names)]
            except ValueError:
                raise ConfigError(f"{where}: days must be weekday names like mon, tue, got {d['days']!r}")
        items.append(Item(
            desc=desc,
            project=str(_require(d, "project", where)),
            task=str(_require(d, "task", where)),
            dur=parse_duration(_require(d, "dur", where), f"{where}.dur"),
            at=parse_clock(d["at"], f"{where}.at") if d.get("at") is not None else None,
            days=days,
            jitter=parse_duration(d["jitter"], f"{where}.jitter") if d.get("jitter") is not None else None,
            tags=_tags(d, where),
        ))
        if items[-1].dur <= 0:
            raise ConfigError(f"{where}: dur must be more than 0")

    calendars = []
    for i, c in enumerate(raw.get("calendars") or []):
        where = f"calendars[{i}]"
        if not isinstance(c, dict):
            raise ConfigError(f"{where}: must be a mapping with name and url_env")
        if "url" in c:
            raise ConfigError(f"{where}: never put a feed URL in the config; name an environment variable in url_env")
        calendars.append(Calendar(str(_require(c, "name", where)), str(_require(c, "url_env", where))))

    default_action = str(raw.get("default_action", "ask"))
    if default_action not in ACTIONS:
        raise ConfigError(f"default_action must be one of {', '.join(ACTIONS)}, got {default_action!r}")

    cfg = Config(
        path=path,
        timezone=tz_name,
        tz=tz,
        target=target,
        day_start=day_start,
        day_end=day_end,
        round_to=round_to,
        jitter=jitter,
        generated_tag=str(raw.get("generated_tag") or "auto"),
        breaks=breaks,
        items=items,
        filler=_target(_require(raw, "filler", "config"), "filler"),
        meetings_default=_target(raw.get("meetings_default") or raw["filler"], "meetings_default"),
        skip_dates={_parse_date(v, "skip_dates") for v in (raw.get("skip_dates") or [])},
        calendars=calendars,
        my_emails=[str(e).strip().lower() for e in (raw.get("my_emails") or [])],
        event_rules=[_parse_rule(r, f"event_rules[{i}]") for i, r in enumerate(raw.get("event_rules") or [])],
        default_action=default_action,
    )
    check_day_window(cfg)
    return cfg


def working_window(cfg: Config) -> int:
    """Minutes between day_start and day_end, minus the parts covered by breaks."""
    covered = 0
    for start, end in merge_intervals([(b.at, b.at + b.dur) for b in cfg.breaks]):
        covered += max(0, min(end, cfg.day_end) - max(start, cfg.day_start))
    return cfg.day_end - cfg.day_start - covered


def check_day_window(cfg: Config) -> None:
    window = working_window(cfg)
    if window < cfg.target:
        raise ConfigError(
            f"day_start to day_end minus breaks is {fmt_minutes(window)}, "
            f"less than the target {fmt_minutes(cfg.target)}. "
            f"Move day_start or day_end, or shorten breaks."
        )


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Copy autofill.example.yaml to {DEFAULT_CONFIG_NAME} and edit it, or pass --config."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}")
    cfg = parse_config(raw, path)
    cfg.learned_rules = load_learned_rules(cfg.learned_rules_path)
    return cfg


# ---------------------------------------------------------------------------
# Learned rules
# ---------------------------------------------------------------------------

def load_learned_rules(path: Path) -> list[Rule]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}")
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: must be a list of rules")
    return [_parse_rule(r, f"{path.name}[{i}]", learned=True) for i, r in enumerate(raw)]


def learned_rule_for(title: str, action: str) -> Rule:
    """A learned rule matches the exact title (escaped, anchored, case-insensitive)."""
    return Rule(f"^{re.escape(title.strip())}$", action, learned=True)


def append_learned_rule(path: Path, rule: Rule) -> None:
    """Add one rule to rules.learned.yaml. The hand-edited config is never touched."""
    raw = []
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    raw.append({"match": rule.match, "action": rule.action})
    path.write_text(
        "# Written by autofill.py when you answer a (always) or x (never). Safe to edit.\n"
        + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

def load_dotenv(*paths: Path) -> None:
    """Read KEY=VALUE lines into os.environ. Real environment variables win."""
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
