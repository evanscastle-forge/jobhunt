"""Reading and writing the small JSON files the tool keeps on disk, atomically.

A leaf: it imports nothing from jobhunt, so every other module may depend on it."""
from __future__ import annotations

import json
import os
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _read_json(path: Path, default: dict) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            path.replace(path.with_suffix(".corrupt.json"))
    return json.loads(json.dumps(default))


def _write_json(path: Path, data, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)

