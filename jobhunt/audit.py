"""An append-only record of every mutation to the scoring model, per profile.

A grade is the product of four things: the posting's text, the facts the model was told, the prompt
it was asked, and the weights the answer was multiplied by. Three of those change over time, and
without a record there is no way to answer "why did this rank differently last week" except to guess.
data/audit.jsonl answers it. `jobhunt.py audit` reads it back.

The pathways are deliberately NOT uniform, and pretending otherwise would be the lie:

    facts_changed    a fact changes a score by changing what the model reads. Nothing is re-scored
                     when it lands; the ratings made under the old facts go stale and the next judge
                     pass reads those postings again.
    judged           the model read N postings and filled M attribute slots, under this prompt,
                     these facts and this model name.
    weights_changed  arithmetic. Every stored rating keeps its value and every total moves at once.
    curve_refit      the grade boundaries moved. No posting's relevance changed; what a given
                     relevance is CALLED changed.
    scan             what the run touched: boards, failures, counts per stage.
    discovered       companies probed and boards promoted into the scan. Not a scoring change: it
                     alters which employers are looked at, never how a posting is judged.

Writing is best-effort and never raises: an audit line failing must not lose a scan. Detection is by
comparison against the last line of the same kind rather than by hooking every writer, so a fact
edited in a text editor outside the dashboard is still recorded on the next run.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

AUDIT_FILE = "audit.jsonl"
KINDS = ("facts_changed", "judged", "weights_changed", "curve_refit", "scan", "discovered")
MAX_LINE = 20000


def path_for(data_dir: Path) -> Path:
    return Path(data_dir) / AUDIT_FILE


def append(data_dir: Path, kind: str, **fields) -> Optional[dict]:
    """Add one line. Returns what was written, or None if it could not be."""
    rec = {"at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "kind": kind, **fields}
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    if len(line) > MAX_LINE:                 # a pathological diff must not make the log unreadable
        rec = {**rec, "truncated": True}
        for k, v in list(rec.items()):
            if isinstance(v, list) and len(v) > 20:
                rec[k] = v[:20]
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))[:MAX_LINE]
    try:
        p = path_for(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        return None
    return rec


def read(data_dir: Path, kind: Optional[str] = None, limit: int = 0) -> list[dict]:
    """Lines oldest first, optionally of one kind. A corrupt line is skipped, not fatal."""
    out = [r for r in _iter(data_dir) if kind is None or r.get("kind") == kind]
    return out[-limit:] if limit > 0 else out


def _iter(data_dir: Path) -> Iterator[dict]:
    p = path_for(data_dir)
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def last(data_dir: Path, kind: str) -> Optional[dict]:
    got = read(data_dir, kind, limit=1)
    return got[0] if got else None


def note_change(data_dir: Path, kind: str, field: str, value, **fields) -> Optional[dict]:
    """Record `kind` only if `field` differs from the last line of that kind.

    This is how facts and weights are tracked: by comparing the current state against what was last
    recorded, rather than by hooking every place that could write them. Editing about.md in a text
    editor is then caught on the next run, which hooking the dashboard would miss."""
    prev = last(data_dir, kind)
    if prev is not None and prev.get(field) == value:
        return None
    return append(data_dir, kind, **{field: value, "was": (prev or {}).get(field)}, **fields)
