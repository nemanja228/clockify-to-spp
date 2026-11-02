"""Shared fixtures for the autofill tests. No network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from autofill_config import parse_config  # noqa: E402
from autofill_plan import Block  # noqa: E402


def base_raw(**overrides) -> dict:
    raw = {
        "timezone": "Europe/Belgrade",
        "target": "8h",
        "day_start": "09:00",
        "day_end": "17:30",
        "round_to": "5m",
        "jitter": "0m",
        "generated_tag": "auto",
        "breaks": [{"at": "12:30", "dur": "30m"}],
        "items": [
            {"desc": "PR reviews", "project": "PIP", "task": "Code review", "dur": "45m"},
            {"desc": "Support / email", "project": "PIP", "task": "Support", "dur": "1h"},
            {"desc": "Weekly planning", "project": "PIP", "task": "Meetings", "at": "10:00",
             "dur": "30m", "days": ["mon"], "jitter": "0m"},
        ],
        "filler": {"project": "PIP", "task": "Development"},
        "meetings_default": {"project": "PIP", "task": "Meetings"},
        "skip_dates": ["2026-11-11"],
        "event_rules": [],
        "default_action": "ask",
    }
    raw.update(overrides)
    return raw


def make_cfg(**overrides):
    return parse_config(base_raw(**overrides), Path("autofill.yaml"))


def hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def manual(start: str, end: str, desc: str = "manual work", project: str = "Other", task: str = "Stuff",
           entry_id: str = "m1") -> Block:
    return Block(hm(start), hm(end), project, task, desc, "manual", "keep", entry_id=entry_id)
