"""Ceilings on the sources that charge, so a nightly scan cannot spend a month's allowance in a week.

A quota is not a rate limit. The per-host throttle in sources.py paces requests so a server is never
hit too fast; this is about a budget that does not refill. Three of the keyed sources have one:

    Adzuna    25/min · 250/day · 1,000/week · 2,500/month
    SerpApi   250 searches/month on the free tier
    Jooble    500 requests, LIFETIME, per key

Measured against the configuration as written, a nightly scan would exhaust Adzuna's week in five
days and Jooble permanently in under a week. A source whose allowance does not refill can never be a
per-scan source, however useful it is -- which is why Jooble is parked and used once, for discovery.

Counters live in data/quota.json, keyed by source and by UTC day. Spending is recorded BEFORE the
request, so a crash costs the budget rather than silently overspending it; the alternative loses
count of what was already sent.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..jsonfile import _read_json, _write_json

QUOTA_FILE = "quota.json"
_lock = threading.Lock()


class QuotaExceeded(RuntimeError):
    """Raised instead of making a request that would go over budget."""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def load(data_dir: Path) -> dict:
    return _read_json(Path(data_dir) / QUOTA_FILE, {"days": {}, "lifetime": {}})


def spent(data_dir: Path, source: str, day: Optional[str] = None) -> tuple[int, int]:
    """(spent today, spent ever) for one source."""
    q = load(data_dir)
    return (int((q.get("days") or {}).get(day or _today(), {}).get(source, 0)),
            int((q.get("lifetime") or {}).get(source, 0)))


def take(data_dir: Path, source: str, n: int, per_day: int = 0, lifetime: int = 0) -> None:
    """Record `n` requests against `source`, or raise QuotaExceeded before any are made.

    Both ceilings are optional and 0 means no ceiling. The check and the write are one critical
    section so two threads cannot each see room for the last request."""
    with _lock:
        q = load(data_dir)
        days = q.setdefault("days", {})
        today = days.setdefault(_today(), {})
        ever = q.setdefault("lifetime", {})
        used_today, used_ever = int(today.get(source, 0)), int(ever.get(source, 0))
        if per_day and used_today + n > per_day:
            raise QuotaExceeded(
                f"{source}: {used_today}/{per_day} requests used today, {n} more would go over. "
                f"Raise [quota] {source}.per_day, or let it reset at midnight UTC.")
        if lifetime and used_ever + n > lifetime:
            raise QuotaExceeded(
                f"{source}: {used_ever}/{lifetime} of a LIFETIME allowance used. "
                f"This key does not refill; the remainder is worth spending deliberately.")
        today[source] = used_today + n
        ever[source] = used_ever + n
        # keep a fortnight of days, which is enough to explain a surprise and not enough to grow
        for old in sorted(days)[:-14]:
            days.pop(old, None)
        _write_json(Path(data_dir) / QUOTA_FILE, q, compact=True)


def limits_for(cfg: dict, source: str) -> tuple[int, int]:
    """(per_day, lifetime) from [quota] in the config, 0 for no ceiling."""
    q = ((cfg or {}).get("quota") or {}).get(source) or {}
    try:
        return int(q.get("per_day", 0) or 0), int(q.get("lifetime", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0
