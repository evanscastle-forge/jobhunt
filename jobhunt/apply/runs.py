"""Apply runs: one folder per attempt under profiles/<name>/data/runs/<run_id>/.

    run.json      the state machine (below) plus what was filled, flagged and asked
    steps.jsonl   one line per step, in order, with the screenshot taken at that step
    NN-*.png      screenshots
    log.txt       the raw output of the apply process
    control.json  written by the dashboard: {"action": "resume" | "abandon"}; the run consumes it
    issues.jsonl / responses.jsonl   what the run asks and what you answer (issues.py)

States:  queued -> running -> needs_you -> (running again after resume) -> submitted | failed | abandoned
         queued -> running -> rehearsed   (filled and verified, then closed; never submits)

The run never ends silently: a pause always carries a `reason`, a failure an `error`, and a process
that dies without writing a final state is marked failed by the dashboard (see `reap`).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATES = ("queued", "running", "needs_you", "submitted", "failed", "abandoned", "rehearsed")
TERMINAL = {"submitted", "failed", "abandoned", "rehearsed"}
RUNS_DIR = "runs"

# why a run is waiting on you (run.json "reason")
REASONS = {
    "review_and_submit": "ready: every required field is certain and verified; read it and press Submit",
    "fix_fields": "some fields did not take or need you; fix them in the open window",
    "verify_email": "the employer's site sent a verification email: click its link, then Resume",
    "vault": "Workday needs an account per employer: run `uv run python jobhunt.py vault init` once, then Retry",
    "login": "could not sign in or create the account on the employer's site; see the note",
    "questions": "questions without an approved answer; answer them, then Resume",
    "unsupported_form": "this form is not automated; complete it in the open window",
    "captcha": "a captcha is on the page; solve it and finish in the open window",
    "error": "something went wrong; the window is still open",
    "window_closed": "the browser window closed before a confirmation was seen; was it submitted?",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s or "")[:80]


def runs_dir(data_dir: Path) -> Path:
    return Path(data_dir) / RUNS_DIR


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class Run:
    def __init__(self, path: Path, data: Optional[dict] = None):
        self.path = Path(path)
        self.data: dict = data if data is not None else self._load()

    # -- files --------------------------------------------------------------
    def _load(self) -> dict:
        p = self.path / "run.json"
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"id": self.path.name, "state": "failed", "error": "run.json unreadable"}

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = now_iso()
        _write_json(self.path / "run.json", self.data)

    @property
    def id(self) -> str:
        return self.data.get("id") or self.path.name

    @property
    def state(self) -> str:
        return self.data.get("state", "queued")

    def get(self, k, default=None):
        return self.data.get(k, default)

    # -- state machine ------------------------------------------------------
    def set_state(self, state: str, reason: Optional[str] = None, note: str = "", **fields) -> None:
        if state not in STATES:
            raise ValueError(f"bad run state {state!r}")
        self.data["state"] = state
        self.data["reason"] = reason if state == "needs_you" else None
        self.data["reason_text"] = (REASONS.get(reason, reason) if reason else None) if state == "needs_you" else None
        self.data.update(fields)
        self.data.setdefault("history", []).append({"at": now_iso(), "state": state, "reason": reason, "note": note})
        self.step(f"state -> {state}" + (f" ({reason})" if reason else "") + (f": {note}" if note else ""))
        self.save()

    def pause(self, reason: str, note: str = "", **fields) -> None:
        self.set_state("needs_you", reason=reason, note=note, **fields)

    def fail(self, error: str) -> None:
        self.set_state("failed", error=error, note=error)

    # -- steps + screenshots ------------------------------------------------
    def step(self, msg: str, screenshot: Optional[str] = None) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.path / "steps.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now_iso(), "msg": msg, "screenshot": screenshot}, ensure_ascii=False) + "\n")

    def steps(self) -> list[dict]:
        p = self.path / "steps.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def screenshot(self, page, name: str) -> Optional[str]:
        """Save a screenshot named NN-name.png; returns the file name (None on failure)."""
        self.path.mkdir(parents=True, exist_ok=True)
        n = len(list(self.path.glob("*.png"))) + 1
        fname = f"{n:02d}-{_safe(name)}.png"
        try:
            page.screenshot(path=str(self.path / fname), full_page=True)
        except Exception:  # noqa: BLE001
            try:
                page.screenshot(path=str(self.path / fname))
            except Exception:  # noqa: BLE001
                return None
        self.data.setdefault("screenshots", []).append(fname)
        self.step(name, screenshot=fname)
        self.save()
        return fname

    def log_path(self) -> Path:
        return self.path / "log.txt"

    # -- control (dashboard -> run) ----------------------------------------
    def read_control(self) -> Optional[str]:
        p = self.path / "control.json"
        if not p.exists():
            return None
        try:
            action = json.loads(p.read_text(encoding="utf-8")).get("action")
        except (OSError, json.JSONDecodeError):
            action = None
        try:
            p.unlink()
        except OSError:
            pass
        return action

    def write_control(self, action: str) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        _write_json(self.path / "control.json", {"action": action, "at": now_iso()})

    def files(self) -> list[str]:
        return sorted(p.name for p in self.path.iterdir() if p.is_file()) if self.path.exists() else []

    def summary(self) -> dict:
        d = {k: v for k, v in self.data.items() if k not in ("report", "history", "questions", "issues")}
        rep = self.data.get("report") or {}
        qs = self.data.get("questions") or []
        d["filled"] = len(rep.get("filled") or []) + len(rep.get("uploaded") or [])
        d["flagged"] = len(rep.get("flagged") or [])
        d["open_questions"] = len([q for q in qs if not q.get("answered")])
        d["required_open"] = len([q for q in qs if not q.get("answered") and q.get("required")])
        d["issues"] = len(self.data.get("issues") or [])
        d["ready"] = bool(rep.get("ready"))
        return d


class NullRun:
    """Stands in when run_apply is called without a run folder (tests, ad-hoc CLI use)."""
    id = ""
    path = None
    data: dict = {}

    def save(self): pass
    def set_state(self, state, reason=None, note="", **fields):
        self.data = {**self.data, "state": state, "reason": reason, **fields}
    def pause(self, reason, note="", **fields): self.set_state("needs_you", reason, note, **fields)
    def fail(self, error): self.set_state("failed", error=error)
    def step(self, msg, screenshot=None): pass
    def screenshot(self, page, name): return None
    def read_control(self): return None
    def get(self, k, default=None): return self.data.get(k, default)
    @property
    def state(self): return self.data.get("state", "running")


# ----------------------------------------------------------------------------- listing

def new_run(data_dir: Path, job: dict, profile: str, ats: str = "", url: str = "") -> Run:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rid = f"{ts}-{_safe(job.get('id') or 'job')}"
    path = runs_dir(data_dir) / rid
    n = 1
    while path.exists():
        n += 1
        path = runs_dir(data_dir) / f"{rid}-{n}"
    run = Run(path, {"id": path.name, "job_id": job.get("id"), "company": job.get("company"), "title": job.get("title"),
                     "profile": profile, "ats": ats, "url": url, "state": "queued", "reason": None, "reason_text": None,
                     "created_at": now_iso(), "pid": None, "questions": [], "report": {}, "screenshots": [],
                     "confirmation": None, "error": None, "history": []})
    run.set_state("queued")
    return run


def load_run(data_dir: Path, run_id: str) -> Optional[Run]:
    if not run_id or "/" in run_id or run_id.startswith("."):
        return None
    p = runs_dir(data_dir) / run_id
    return Run(p) if (p / "run.json").exists() else None


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:  # noqa: BLE001  (ESRCH, EPERM, overflow, bad value: all mean "not ours / not alive")
        return False


def reap(run: Run) -> bool:
    """A run whose process died without a final state is surfaced, loudly. Returns True if changed."""
    if run.state in TERMINAL or run.state == "queued":
        return False
    if run.get("pid") and not pid_alive(run.get("pid")):
        if run.state == "needs_you":
            # the window is gone; you may have submitted by hand, so ask rather than fail
            run.pause("window_closed", note="the apply process ended while waiting for you", pid=None)
        else:
            run.fail("the apply process exited without reporting a result")
        return True
    return False


def list_runs(data_dir: Path, job_id: Optional[str] = None, reap_dead: bool = True) -> list[Run]:
    d = runs_dir(data_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.iterdir(), reverse=True):
        if not (p / "run.json").exists():
            continue
        r = Run(p)
        if job_id and r.get("job_id") != job_id:
            continue
        if reap_dead:
            reap(r)
        out.append(r)
    return out


def counts(runs: list[Run]) -> dict:
    c = {s: 0 for s in STATES}
    for r in runs:
        c[r.state] = c.get(r.state, 0) + 1
    c["open"] = sum(1 for r in runs if r.state not in TERMINAL)
    return c


def archive_all(data_dir: Path, dest: Path) -> dict:
    """Abandon every open run and move every run folder (and the batch reports) under dest. Moved, not
    deleted: the captured forms stay available to `jobhunt.py replay`."""
    import shutil
    src = runs_dir(data_dir)
    out = {"abandoned": 0, "moved": 0}
    if not src.exists():
        return out
    for run in list_runs(data_dir):
        if run.state not in TERMINAL:
            if pid_alive(run.get("pid")):
                run.write_control("abandon")         # the window closes itself
            run.set_state("abandoned", note="cleared by reset")
            out["abandoned"] += 1
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.iterdir()):
        shutil.move(str(p), str(dest / p.name))
        out["moved"] += 1
    return out
