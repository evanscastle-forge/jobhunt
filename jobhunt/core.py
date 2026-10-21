"""Compatibility facade for the modules core.py was split into.

The split separated four concerns that had grown together: file helpers (jsonfile), the per-profile
store (store), what the reader is shown (views) and the scan orchestration (pipeline). Everything
core.py used to export is re-exported here so callers did not all have to move at once.

New code should import from the real module. This file goes away once nothing points at it."""
from __future__ import annotations

from .jsonfile import _read_json, _write_json, load_toml, now_iso
from .pipeline import (RAW_FILE, collapse_variants, fetch_all, judge_pass, load_raw, merge_into_store,
                       profile_facts, relearn, rescore, retire_excluded_companies, save_raw, scan,
                       union_companies)
from .store import DONE_REASONS, LEGACY_STATUS, STATUSES, Store
from .report.views import TABS

__all__ = [
    "DONE_REASONS", "LEGACY_STATUS", "RAW_FILE", "STATUSES", "Store", "TABS",
    "_read_json", "_write_json", "collapse_variants", "fetch_all", "judge_pass",
    "load_raw", "load_toml", "merge_into_store", "now_iso", "profile_facts", "relearn",
    "rescore", "retire_excluded_companies", "save_raw", "scan", "union_companies",
]
