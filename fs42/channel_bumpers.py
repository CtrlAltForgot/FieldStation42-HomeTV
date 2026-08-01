"""Persistent per-channel bumper folders and seasonal eligibility."""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

FOLDERS = (
    "general", "promos", "spring", "summer", "halloween",
    "thanksgiving", "christmas", "new-years",
)


def bumper_root() -> Path:
    return Path(os.environ.get(
        "FS42_CHANNEL_BUMPER_DIR", "catalog/channel_bumpers"
    )).resolve()


def channel_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "Channel"


def ensure_channel_folders(network_name: str) -> dict[str, Path]:
    root = bumper_root() / channel_slug(network_name)
    folders = {}
    for name in FOLDERS:
        folders[name] = root / name
        folders[name].mkdir(parents=True, exist_ok=True)
    return folders


def active_seasons(when: dt.datetime | None = None) -> tuple[str, ...]:
    when = when or dt.datetime.now()
    current = (when.month, when.day)
    active = ["general", "promos"]
    if current >= (12, 26) or current <= (1, 7): active.append("new-years")
    if (11, 20) <= current <= (11, 30): active.append("thanksgiving")
    if (12, 1) <= current <= (12, 25): active.append("christmas")
    if (10, 1) <= current <= (10, 31): active.append("halloween")
    if (3, 1) <= current <= (5, 31): active.append("spring")
    if (6, 1) <= current <= (8, 31): active.append("summer")
    return tuple(active)


def active_channel_folders(network_name: str, when: dt.datetime | None = None):
    folders = ensure_channel_folders(network_name)
    return [folders[name] for name in active_seasons(when)]
