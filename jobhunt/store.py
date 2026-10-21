"""The per-profile files on disk and the decisions recorded against them.

jobs.json is what the scanner found, state.json is what you decided, learned.json is what
those decisions taught us. Store owns reading and writing them safely when a cron scan, the
dashboard and an apply subprocess are all live at once."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .classify import learn
from .jsonfile import _read_json, _write_json, now_iso

STATUSES = ["new", "saved", "applied", "done", "skip"]
# older state files used a longer pipeline; the tool now stops tracking after "applied"
LEGACY_STATUS = {"shortlist": "saved", "interviewing": "applied", "offer": "applied", "rejected": "done"}
DONE_REASONS = ["no offer", "withdrew"]


class Store:
    """Per-profile files: jobs.json (what the scanner found), state.json (what you decided),
    learned.json (what your decisions taught us)."""

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jobs_path = self.dir / "jobs.json"
        self.state_path = self.dir / "state.json"
        self.learned_path = self.dir / "learned.json"
        self.jobs: dict = _read_json(self.jobs_path, {"scanned_at": None, "jobs": {}})
        self.state: dict = _read_json(self.state_path, {"jobs": {}})
        self._mtimes = (self._mtime(self.jobs_path), self._mtime(self.state_path))
        if self._migrate_statuses():
            _write_json(self.state_path, self.state)
            self._mtimes = (self._mtimes[0], self._mtime(self.state_path))

    def _migrate_statuses(self) -> bool:
        changed = False
        for st in self.state.get("jobs", {}).values():
            old = st.get("status")
            if old in LEGACY_STATUS:
                st["status"] = LEGACY_STATUS[old]
                changed = True
        return changed

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def refresh(self) -> None:
        """Re-read files another process changed (a cron scan, an apply run) since we loaded them."""
        m = (self._mtime(self.jobs_path), self._mtime(self.state_path))
        if m[0] != self._mtimes[0]:
            self.jobs = _read_json(self.jobs_path, {"scanned_at": None, "jobs": {}})
        if m[1] != self._mtimes[1]:
            self.state = _read_json(self.state_path, {"jobs": {}})
            self._migrate_statuses()
        self._mtimes = m

    def save(self) -> None:
        _write_json(self.jobs_path, self.jobs, compact=True)
        _write_json(self.state_path, self.state)
        self._mtimes = (self._mtime(self.jobs_path), self._mtime(self.state_path))

    def save_jobs(self) -> None:
        _write_json(self.jobs_path, self.jobs, compact=True)
        self._mtimes = (self._mtime(self.jobs_path), self._mtimes[1])

    # -- learned model ------------------------------------------------------
    def learned(self) -> Optional[dict]:
        return _read_json(self.learned_path, {}) if self.learned_path.exists() else None

    def save_learned(self, model: dict) -> None:
        _write_json(self.learned_path, model)

    # -- state -------------------------------------------------------------
    def get_state(self, job_id: str, refresh: bool = True) -> dict:
        if refresh:
            self.refresh()
        return self.state["jobs"].get(job_id, {"status": "new", "notes": ""})

    def set_state(self, job_id: str, status: Optional[str] = None, notes: Optional[str] = None,
                  skip_reason: Optional[str] = None, done_reason: Optional[str] = None, **extra) -> dict:
        st = dict(self.get_state(job_id))  # refreshes first, so we never clobber another process's write
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"bad status {status!r}")
            st.pop("archived", None)          # deciding again brings an archived decision back into play
            if status != st.get("status", "new"):
                hist = list(st.get("history") or [])
                hist.append({"status": st.get("status", "new"), "skip_reason": st.get("skip_reason"),
                             "done_reason": st.get("done_reason")})
                st["history"] = hist[-5:]
            st["status"] = status
            if status == "applied" and not st.get("applied_at"):
                st["applied_at"] = now_iso()
            if status != "skip":
                st.pop("skip_reason", None)
            if status != "done":
                st.pop("done_reason", None)
        if done_reason is not None:
            if done_reason and done_reason not in DONE_REASONS:
                raise ValueError(f"bad done reason {done_reason!r}")
            if done_reason:
                st["done_reason"] = done_reason
        if skip_reason is not None:
            if skip_reason and skip_reason not in learn.SKIP_REASONS:
                raise ValueError(f"bad skip reason {skip_reason!r}")
            if skip_reason:
                st["skip_reason"] = skip_reason
            else:
                st.pop("skip_reason", None)
        if notes is not None:
            st["notes"] = notes
        st.update(extra)
        st["updated_at"] = now_iso()
        self.state["jobs"][job_id] = st
        _write_json(self.state_path, self.state)
        self._mtimes = (self._mtimes[0], self._mtime(self.state_path))
        return st

    def undo(self, job_id: str) -> dict:
        """Put a posting back where it was before the last status change (or 'new')."""
        st = dict(self.get_state(job_id))
        hist = list(st.get("history") or [])
        prev = hist.pop() if hist else {"status": "new", "skip_reason": None, "done_reason": None}
        st["status"] = prev.get("status") or "new"
        for k in ("skip_reason", "done_reason"):
            if prev.get(k):
                st[k] = prev[k]
            else:
                st.pop(k, None)
        st["history"] = hist
        st["updated_at"] = now_iso()
        self.state["jobs"][job_id] = st
        _write_json(self.state_path, self.state)
        self._mtimes = (self._mtimes[0], self._mtime(self.state_path))
        return st

    def reset(self, stamp: str) -> dict:
        """Clear what is in play, keep what was learned. Postings you never decided on leave jobs.json (the
        next scan brings back the live ones, their ratings reused). Saved decisions are stamped archived
        rather than deleted: they still teach the learned model and still count as retune's evidence,
        and deleting them would change the auto facts in about.md and force a full re-rate. Skipped,
        applied and done decisions are untouched."""
        self.refresh()
        decided = {jid for jid, st in self.state["jobs"].items() if st.get("status", "new") != "new"}
        before = len(self.jobs["jobs"])
        self.jobs["jobs"] = {jid: rec for jid, rec in self.jobs["jobs"].items() if jid in decided}
        archived = 0
        for st in self.state["jobs"].values():
            if st.get("status") == "saved":
                # re-stamp saves an earlier reset archived, too: a scan since then has seen them again,
                # which is what brings an archived posting back as New, so this reset must clear them again
                st["archived"] = stamp
                archived += 1
        self.save()
        return {"postings_cleared": before - len(self.jobs["jobs"]), "saved_archived": archived}

    # -- views --------------------------------------------------------------
    def merged(self, include_inactive: bool = False) -> list[dict]:
        """Deprecated: presentation, not storage. Use report.views.rows_for(store)."""
        from .report.views import rows_for       # imported here: report sits above store
        return rows_for(self, include_inactive)

    def get(self, job_id: str) -> Optional[dict]:
        self.refresh()
        job = self.jobs["jobs"].get(job_id)
        if not job:
            return None
        row = dict(job)
        row["state"] = self.get_state(job_id)
        return row
