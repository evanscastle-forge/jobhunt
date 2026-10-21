"""Localhost dashboard: a tiny stdlib HTTP server over the per-profile JSON files.

Binds to 127.0.0.1 only. No external network is needed to use the UI; the "Scan" button is the
only thing that reaches out (to the public job feeds), and only when you click it.

Every /api route takes ?profile=<name> (or "profile" in a POST body); the default profile is used
when it is absent. Scans always cover every profile, since fetching is shared.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import API_VERSION
from ..classify import relevance as relmod
from . import views
from . import about
from ..apply import answers as bankmod
from ..classify import facts as factsmod
from ..classify import judge as judgemod
from ..apply import runs as runsmod
from ..apply import issues as issuesmod
from ..apply import tailor
from ..apply import terms as termsmod
from ..apply.forms import apply_page_url, detect_ats, learn_from_submit
from ..core import DONE_REASONS, STATUSES, TABS, judge_pass, profile_facts, relearn, rescore, scan
from ..pipeline import STAGES, Progress
from ..profiles import Profile, load_all, load_env, pick_profile

HERE = Path(__file__).resolve().parent
DEFAULT_MAX_OPEN = 6


class AppState:
    def __init__(self, root: Path, launch_runs: bool = True):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.scan_thread: threading.Thread | None = None
        self.scan_log: list[str] = []
        self.scan_result: dict | None = None
        self.scan_mode: str = ""
        # Live, so the page can say WHICH stage is running and how far in, rather than only that
        # something is happening. Held here rather than returned at the end, because the end is the
        # one moment a progress report is no longer useful.
        self.scan_progress: Progress | None = None
        self.apply_procs: dict[str, subprocess.Popen] = {}     # run id -> process
        self.launch_runs = launch_runs                          # False in tests: queue only, never open a browser
        self.pump_thread: threading.Thread | None = None
        # background relearn passes, keyed "relearn:<profile>"
        self.derive_threads: dict[str, threading.Thread] = {}
        self.relearn_lock = threading.Lock()
        self.relearn_pending: set[str] = set()
        self.shared: dict = {}
        self.profiles: dict[str, Profile] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read config.toml and every profile folder (cheap; stores keep their in-memory state)."""
        old = self.profiles
        self.shared, fresh = load_all(self.root)
        for name, p in fresh.items():
            if name in old:
                p.store = old[name].store   # keep the already-loaded store (it refreshes itself on mtime)
        self.profiles = fresh

    def profile(self, name: str | None) -> Profile:
        return pick_profile(self.profiles, name, self.shared)

    def profiles_summary(self) -> list[dict]:
        out = []
        for name, p in self.profiles.items():
            rows = p.store.merged()
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["state"]["status"]] = counts.get(r["state"]["status"], 0) + 1
            out.append({"name": name, "display_name": p.display_name, "counts": counts,
                        "scanned_at": p.store.jobs.get("scanned_at"), "errors": p.errors,
                        "resume": bool(p.resume_path and p.resume_path.exists())})
        return out

    # -- scanning ---------------------------------------------------------------
    def start_scan(self, mode: str = "scan") -> bool:
        with self.lock:
            if self.scan_thread and self.scan_thread.is_alive():
                return False
            self.scan_log = []
            self.scan_result = None
            self.scan_mode = mode
            prog = Progress(self.scan_log.append)
            self.scan_progress = prog

            def run():
                try:
                    self.reload()
                    if mode == "rescore":
                        self.scan_result = rescore(self.root, self.profiles,
                                                   log=self.scan_log.append, prog=prog)
                    elif mode == "judge":
                        out = {}
                        for n, p in self.profiles.items():
                            prog.begin("judge", note=n)
                            out[n] = {"judged": judge_pass(p, log=self.scan_log.append)}
                            prog.end()
                        self.scan_result = {"profiles": out}
                    else:
                        self.scan_result = scan(self.root, self.profiles, log=self.scan_log.append,
                                                env=load_env(self.root), prog=prog)
                except Exception as e:  # noqa: BLE001
                    self.scan_log.append(f"{mode} failed: {type(e).__name__}: {e}")
                    self.scan_result = {"error": str(e)}

            self.scan_thread = threading.Thread(target=run, daemon=True)
            self.scan_thread.start()
            return True

    def relearn_later(self, p: Profile) -> None:
        """Re-score in the background so About-page edits answer instantly; the list picks it up on reload.
        An edit that lands while a pass is already running queues another one instead of being dropped,
        so the last edit always reaches the scores."""
        key = f"relearn:{p.name}"

        def run():
            while True:
                with self.relearn_lock:
                    if p.name not in self.relearn_pending:
                        return
                    self.relearn_pending.discard(p.name)
                relearn(p)

        with self.relearn_lock:
            self.relearn_pending.add(p.name)
            t = self.derive_threads.get(key)
            if t and t.is_alive():
                return          # the running pass will pick the edit up before it exits
            th = threading.Thread(target=run, daemon=True)
            self.derive_threads[key] = th
            th.start()

    def scan_status(self) -> dict:
        """Enough for the page to draw a progress bar without opening the log.

        `progress` is the live Progress snapshot: which stage, how far through it, and the stages
        already finished. A scan takes minutes, and closing the log used to leave no sign at all
        that one was under way."""
        running = bool(self.scan_thread and self.scan_thread.is_alive())
        prog = self.scan_progress
        return {"running": running, "mode": self.scan_mode, "log": self.scan_log[-300:],
                "result": self.scan_result, "stages": list(STAGES),
                "progress": prog.snapshot() if prog else None}

    # -- applying ---------------------------------------------------------------
    # A click on Apply only *queues* a run (a folder under data/runs/). The pump thread launches
    # queued runs as `jobhunt.py apply <job> --run <id>` subprocesses while fewer than
    # [apply].max_open_windows windows are waiting for you; each subprocess owns its own window and
    # writes its state to the run folder, so the dashboard never has to guess what happened.

    def max_open(self, p: Profile) -> int:
        try:
            return max(0, int((p.search.get("apply") or {}).get("max_open_windows", DEFAULT_MAX_OPEN)))
        except (TypeError, ValueError):
            return DEFAULT_MAX_OPEN

    def _poll_procs(self) -> None:
        """Collect finished apply processes so a crashed one does not linger as a zombie that still
        looks alive to `reap`."""
        for rid, proc in list(self.apply_procs.items()):
            if proc.poll() is not None:
                self.apply_procs.pop(rid, None)

    def list_runs(self, p: Profile, job_id: str | None = None) -> list[runsmod.Run]:
        self._poll_procs()
        return runsmod.list_runs(p.data_dir, job_id=job_id)

    def open_runs(self, p: Profile) -> list[runsmod.Run]:
        return [r for r in self.list_runs(p) if r.state not in runsmod.TERMINAL]

    def start_apply(self, p: Profile, job_id: str) -> dict:
        job = p.store.get(job_id)
        if not job:
            return {"ok": False, "error": "unknown job id"}
        if not (p.resume_path and p.resume_path.exists()):
            return {"ok": False, "error": f"no resume file at {p.resume_path}; set documents.resume in profile.toml"}
        for r in self.open_runs(p):
            if r.get("job_id") == job_id:
                return {"ok": False, "error": f"a run is already open for this posting ({r.state})", "run_id": r.id}
        url = apply_page_url(job)
        run = runsmod.new_run(p.data_dir, job, p.name, ats=detect_ats(url), url=url)
        self.ensure_pump()
        return {"ok": True, "run_id": run.id, "state": run.state}

    def queue_all(self, p: Profile, status: str = "saved") -> dict:
        queued, skipped = [], []
        for r in p.store.merged():
            if r["state"]["status"] != status:
                continue
            res = self.start_apply(p, r["id"])
            (queued if res.get("ok") else skipped).append(r["id"])
        return {"ok": True, "queued": queued, "skipped": skipped}

    def ensure_pump(self) -> None:
        if self.pump_thread and self.pump_thread.is_alive():
            return
        self.pump_thread = threading.Thread(target=self._pump, daemon=True)
        self.pump_thread.start()

    def _pump(self) -> None:
        idle = 0
        while idle < 150:          # stop after ~5 quiet minutes; the next Apply click restarts it
            launched = self.pump_once()
            idle = 0 if launched else idle + 1
            threading.Event().wait(2)

    def pump_once(self) -> int:
        """Launch queued runs while there is room. Returns how many were launched."""
        launched = 0
        for p in list(self.profiles.values()):
            with self.lock:
                runs = self.list_runs(p)                        # also reaps dead processes
                waiting = [r for r in runs if r.state in ("running", "needs_you")]
                queued = sorted((r for r in runs if r.state == "queued"), key=lambda r: r.get("created_at") or "")
                room = self.max_open(p) - len(waiting)
                for run in queued[:max(0, room)]:
                    if self.launch_run(p, run):
                        launched += 1
        return launched

    def launch_run(self, p: Profile, run: runsmod.Run) -> bool:
        if not self.launch_runs:
            return False
        cmd = [sys.executable, str(self.root / "jobhunt.py"), "--root", str(self.root), "--profile", p.name,
               "apply", run.get("job_id") or "", "--run", run.id]
        try:
            logf = open(run.log_path(), "a", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=str(self.root))
        except OSError as e:
            run.fail(f"could not start the apply process: {e}")
            return False
        self.apply_procs[run.id] = proc
        fresh = runsmod.load_run(run.path.parent.parent, run.id) or run   # the process may already have written
        if fresh.state == "queued":
            fresh.set_state("running", pid=proc.pid, note="process started")
        return True

    def run_control(self, p: Profile, run_id: str, action: str) -> dict:
        run = runsmod.load_run(p.data_dir, run_id)
        if not run:
            return {"ok": False, "error": "unknown run"}
        self._poll_procs()
        runsmod.reap(run)
        alive = runsmod.pid_alive(run.get("pid"))
        if action == "submitted":
            if run.state in runsmod.TERMINAL and run.state != "submitted":
                return {"ok": False, "error": f"run is already {run.state}"}
            if alive:
                run.write_control("abandon")   # the window is no longer needed
            run.set_state("submitted", note="marked submitted by you", confirmation=run.get("confirmation") or "marked by hand")
            try:     # remember what you submitted, from the window's last snapshot
                learned = learn_from_submit(bankmod.Bank(p.dir), (run.get("report") or {}).get("review") or [],
                                            run.get("window_edits"), questions=run.get("questions") or [],
                                            job={"company": run.get("company"), "title": run.get("title")})
                if any(learned.values()):
                    run.step(f"bank learned from submit: {learned}")
            except Exception as e:  # noqa: BLE001
                run.step(f"could not learn from the submit: {e}")
            if run.get("job_id") and p.store.get(run.get("job_id")):
                p.store.set_state(run.get("job_id"), status="applied")
                relearn(p)
        elif action == "abandon":
            if run.state in runsmod.TERMINAL:
                return {"ok": False, "error": f"run is already {run.state}"}
            if alive:
                run.write_control("abandon")
                run.step("abandon requested from the dashboard")
            else:
                run.set_state("abandoned", note="from the dashboard")
        elif action == "resume":
            if run.state != "needs_you":
                return {"ok": False, "error": f"cannot resume a run that is {run.state}"}
            if not alive:
                return {"ok": False, "error": "the window is gone; queue a new run for this posting"}
            run.write_control("resume")
            run.step("resume requested from the dashboard")
        elif action == "retry":
            if run.state not in runsmod.TERMINAL or not run.get("job_id"):
                return {"ok": False, "error": "retry only applies to a finished run"}
            return self.start_apply(p, run.get("job_id"))
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}
        return {"ok": True, "run": run.summary()}

    def run_answers(self, p: Profile, run_id: str, answers: list) -> dict:
        """Your answers to a run's open questions, from the dashboard: saved to the bank as yours, then the
        open window fills them in (resume)."""
        run = runsmod.load_run(p.data_dir, run_id)
        if not run:
            return {"ok": False, "error": "unknown run"}
        open_keys = {q.get("key"): q for q in (run.get("questions") or []) if not q.get("answered")}
        bank = bankmod.Bank(p.dir)
        saved = []
        for a in answers or []:
            key, value = str(a.get("key") or ""), str(a.get("value") or "").strip()
            if not value:
                continue
            if key not in open_keys:
                return {"ok": False, "error": f"no open question {key!r} on this run"}
            if bank.set(key, answer=value, status="approved", by="", note="answered by you on the dashboard") is None:
                return {"ok": False, "error": f"question {key!r} is not in the answer bank"}
            saved.append(key)
        if not saved:
            return {"ok": False, "error": "nothing to send: every answer was blank"}
        bank.save()
        self._poll_procs()
        resumed = run.state == "needs_you" and runsmod.pid_alive(run.get("pid"))
        if resumed:
            run.write_control("resume")
        run.step(f"{len(saved)} answer(s) from the dashboard" + ("; filling them in" if resumed else ""))
        return {"ok": True, "saved": len(saved), "resumed": resumed}

    def run_respond(self, p: Profile, run_id: str, issue_id: str, action: str, value=None) -> dict:
        """Answer one of a run's issues (issues.py). An open window resumes and applies it at once."""
        run = runsmod.load_run(p.data_dir, run_id)
        if not run:
            return {"ok": False, "error": "unknown run"}
        if issue_id not in {i.id for i in issuesmod.read_issues(run.path)}:
            return {"ok": False, "error": f"no issue {issue_id!r} on this run"}
        try:
            issuesmod.respond(run.path, issue_id, action, value)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        self._poll_procs()
        if run.state == "needs_you" and runsmod.pid_alive(run.get("pid")):
            run.write_control("resume")
            run.step(f"{action} on {issue_id} from the dashboard; resuming")
            return {"ok": True, "resumed": True}
        return {"ok": True, "resumed": False}

    def run_payload(self, p: Profile, run_id: str) -> dict | None:
        run = runsmod.load_run(p.data_dir, run_id)
        if not run:
            return None
        self._poll_procs()
        runsmod.reap(run)
        log = run.log_path()
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        return {**run.data, "steps": run.steps(), "files": run.files(), "log": text[-20000:],
                "alive": runsmod.pid_alive(run.get("pid"))}

    def apply_status(self, p: Profile, job_id: str) -> dict:
        runs = self.list_runs(p, job_id=job_id)
        if not runs:
            return {"running": False, "log": "", "run": None}
        run = runs[0]
        log = run.log_path()
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        return {"running": run.state in ("queued", "running"), "log": text[-20000:], "run": run.summary()}

    # -- answer bank ------------------------------------------------------------
    def answers_payload(self, p: Profile) -> dict:
        bank = bankmod.Bank(p.dir)
        rows = [e.to_dict() for e in bank.entries]
        rows.sort(key=lambda e: (e["approved"], -(e["seen"] or 0), e["question"].lower()))
        return {"profile": p.name, "display_name": p.display_name, "entries": rows, "counts": bank.counts(),
                "answers_path": str(bankmod.bank_path(p.dir))}

    # -- resume variants ----------------------------------------------------------
    def resume_terms_payload(self, p: Profile) -> dict:
        bank = termsmod.TermBank(p.dir)
        rows = [e.to_dict() for e in bank.entries]
        rows.sort(key=lambda e: (e["approved"], e["kind"], e["name"].lower()))
        return {"profile": p.name, "entries": rows, "counts": bank.counts(),
                "terms_path": str(termsmod.terms_path(p.dir)), "has_master": tailor.load_master(p.dir) is not None}


class Handler(BaseHTTPRequestHandler):
    app: AppState  # set by serve()

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (args[0] if args else ""):
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------------
    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _profile(self, q: dict, body: dict | None = None) -> Profile | None:
        name = (body or {}).get("profile") or q.get("profile", [None])[0]
        try:
            return self.app.profile(name)
        except SystemExit as e:
            self._json({"error": str(e)}, 404)
            return None

    # -- routes -----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            html = (HERE / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return
        if u.path == "/api/profiles":
            return self._json({"profiles": self.app.profiles_summary(), "api_version": API_VERSION,
                               "default": self.app.profile(None).name if self.app.profiles else None,
                               "statuses": STATUSES, "done_reasons": DONE_REASONS})
        if u.path == "/api/scan/status":
            return self._json(self.app.scan_status())
        p = self._profile(q)
        if p is None:
            return
        if u.path == "/api/jobs":
            tab = q.get("tab", ["new"])[0]
            if tab not in TABS:
                return self._json({"error": f"unknown tab {tab!r}"}, 400)
            limit = max(0, int(q.get("limit", ["50"])[0] or 0))
            offset = max(0, int(q.get("offset", ["0"])[0] or 0))
            text = (q.get("q", [""])[0] or "").strip().lower()
            # untouched postings that closed or are location variants are invisible everywhere;
            # anything you acted on stays visible in its tab even after the posting closes
            rows = [r for r in views.rows_for(p.store, include_inactive=True, search_cfg=p.search)
                    if r["state"]["status"] != "new" or (r.get("active", True) and not r.get("duplicate_of"))]
            hidden_only = q.get("lowfit", ["0"])[0] == "1"   # the hidden bucket: under the grade floor
            counts = {s: 0 for s in TABS}
            counts["hidden"] = counts["unjudged"] = 0
            for r in rows:
                if r["state"]["status"] != "new":
                    counts[r["state"]["status"]] = counts.get(r["state"]["status"], 0) + 1
                elif views.below_bar(r, p.search):
                    counts["hidden"] += 1
                    if not r.get("role"):
                        counts["unjudged"] += 1
                else:
                    counts["new"] += 1

            if tab == "new":
                tab_rows = [r for r in rows if r["state"]["status"] == "new"
                            and views.below_bar(r, p.search) == hidden_only]
            else:
                tab_rows = [r for r in rows if r["state"]["status"] == tab]
            if text:
                tab_rows = [r for r in tab_rows if text in f"{r['title']} {r['company']} {r['location']}".lower()]
            total = len(tab_rows)
            tab_rows = tab_rows[offset:offset + limit] if limit else tab_rows[offset:]
            lean = []
            for r in tab_rows:
                row = {k: v for k, v in r.items() if k not in ("description", "variants", "snippet", "meta")}
                row["snippet"] = (r.get("snippet") or "")[:300]
                row["variant_count"] = len(r.get("variants") or [])
                lean.append(row)
            self._json({"profile": p.name, "display_name": p.display_name, "tab": tab, "lowfit": hidden_only, "jobs": lean,
                        "min_grade": relmod.min_grade(p.search),
                        "floor_label": views.floor_label(p.search),
                        "total": total, "offset": offset, "limit": limit, "counts": counts, "statuses": STATUSES,
                        "radius_miles": (p.search.get("location", {}) or {}).get("radius_miles", 500),
                        "done_reasons": DONE_REASONS,
                        "scanned_at": p.store.jobs.get("scanned_at"), "last_summary": p.store.jobs.get("last_summary"),
                        })
        elif u.path == "/api/about":
            self._json(self._about_payload(p))
        elif u.path == "/api/job":
            job = p.store.get(q.get("id", [""])[0])
            self._json(job or {"error": "not found"}, 200 if job else 404)
        elif u.path == "/api/apply/status":
            self._json(self.app.apply_status(p, q.get("id", [""])[0]))
        elif u.path == "/api/runs":
            which = q.get("state", ["open"])[0]
            job = q.get("job", [None])[0]
            runs = self.app.list_runs(p, job_id=job)
            rows = [r for r in runs if which == "all" or r.state not in runsmod.TERMINAL]
            self._json({"profile": p.name, "runs": [r.summary() for r in rows], "counts": runsmod.counts(runs),
                        "reasons": runsmod.REASONS, "max_open": self.app.max_open(p)})
        elif u.path == "/api/run":
            payload = self.app.run_payload(p, q.get("id", [""])[0])
            self._json(payload or {"error": "unknown run"}, 200 if payload else 404)
        elif u.path == "/api/run/file":
            run = runsmod.load_run(p.data_dir, q.get("id", [""])[0])
            name = q.get("name", [""])[0]
            if not run or name not in run.files() or not name.endswith(".png"):
                return self._json({"error": "not found"}, 404)
            data = (run.path / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/resume/file":
            # served from the index, never from a client-supplied path
            name = q.get("name", [""])[0]
            kind = q.get("kind", ["pdf"])[0]
            row = tailor.lookup(p.dir, name)
            files = {"pdf": (name, "application/pdf"), "diff": ("diff.md", "text/markdown; charset=utf-8"),
                     "html": ("variant.html", "text/html; charset=utf-8"), "tweaks": ("tweaks.json", "application/json")}
            if not row or kind not in files or "/" in name or ".." in name:
                return self._json({"error": "not found"}, 404)
            path = termsmod.resume_dir(p.dir) / row.get("folder", "") / files[kind][0]
            if not path.is_file():
                return self._json({"error": "not found"}, 404)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", files[kind][1])
            self.send_header("Content-Disposition", f'inline; filename="{files[kind][0]}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/resume/terms":
            self._json(self.app.resume_terms_payload(p))
        elif u.path == "/api/answers":
            self._json(self.app.answers_payload(p))
        elif u.path == "/api/config":
            self._json({"profile": p.name, "companies": sorted(p.board_names()),
                        "location_mode": p.search.get("location", {}).get("mode"),
                        "daily_miles": p.search.get("location", {}).get("daily_miles", 0),
                        "monthly_miles": p.search.get("location", {}).get("monthly_miles", 0),
                        "profile_dir": str(p.dir), "errors": p.errors})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        body = self._read_json()
        if u.path == "/api/scan":
            return self._json({"ok": True, "started": self.app.start_scan("scan")})
        if u.path == "/api/rescore":
            return self._json({"ok": True, "started": self.app.start_scan("rescore")})
        if u.path == "/api/judge":
            return self._json({"ok": True, "started": self.app.start_scan("judge")})
        if u.path == "/api/reload":
            self.app.reload()
            return self._json({"ok": True, "profiles": list(self.app.profiles)})
        p = self._profile(q, body)
        if p is None:
            return
        if u.path == "/api/state":
            jid = body.get("id")
            if not jid or not p.store.get(jid):
                return self._json({"error": "unknown job id"}, 404)
            extra = {}
            if body.get("status") in ("saved", "applied", "skip"):
                extra = views.decision_context(p.store, jid, p.search)
            try:
                st = p.store.set_state(jid, status=body.get("status"), notes=body.get("notes"),
                                       skip_reason=body.get("skip_reason"),
                                       done_reason=body.get("done_reason"), **extra)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            if body.get("status") is not None or body.get("skip_reason") is not None:
                relearn(p)   # decisions change the ranking right away
            self._json({"ok": True, "state": st})
        elif u.path == "/api/apply":
            self._json(self.app.start_apply(p, body.get("id", "")))
        elif u.path == "/api/apply/all":
            status = body.get("status") or "saved"
            if status not in STATUSES:
                return self._json({"error": f"unknown status {status!r}"}, 400)
            self._json(self.app.queue_all(p, status))
        elif u.path == "/api/run/control":
            self._json(self.app.run_control(p, str(body.get("id") or ""), str(body.get("action") or "")))
        elif u.path == "/api/reset":
            from ..reset import reset as do_reset
            with self.app.lock:
                self._json({"ok": True, **do_reset(p, self.app.root)})
        elif u.path == "/api/run/answers":
            self._json(self.app.run_answers(p, str(body.get("id") or ""), body.get("answers") or []))
        elif u.path == "/api/run/respond":
            self._json(self.app.run_respond(p, str(body.get("id") or ""), str(body.get("issue_id") or ""),
                                            str(body.get("action") or ""), body.get("value")))
        elif u.path == "/api/answers/set":
            bank = bankmod.Bank(p.dir)
            key = str(body.get("key") or "")
            status = body.get("status")
            if status is not None and status not in bankmod.STATUSES:
                return self._json({"error": f"status must be one of {bankmod.STATUSES}"}, 400)
            if key:
                old = bank.get(key)
                edited = old is not None and body.get("answer") is not None and \
                    str(body.get("answer")).strip() != (old.answer or "").strip()
                # an answer you edited, or approved, is yours; the agent's note no longer describes it
                mine = edited or status == "approved"
                e = bank.set(key, answer=body.get("answer"), status=status, question=body.get("question"),
                             by="" if mine else None, note="" if edited else None)
                if e is None:
                    return self._json({"error": "unknown question"}, 404)
            else:
                qtext = str(body.get("question") or "").strip()
                if not qtext:
                    return self._json({"error": "question is required"}, 400)
                bank.entries.append(bankmod.Entry(question=qtext, kind=str(body.get("kind") or "text"),
                                                  answer=str(body.get("answer") or "").strip(),
                                                  status=status or "pending"))
                bank.dirty = True
            bank.save()
            self._json({"ok": True, **self.app.answers_payload(p)})
        elif u.path == "/api/answers/delete":
            bank = bankmod.Bank(p.dir)
            if not bank.remove(str(body.get("key") or "")):
                return self._json({"error": "unknown question"}, 404)
            bank.save()
            self._json({"ok": True, **self.app.answers_payload(p)})
        elif u.path == "/api/resume/terms/set":
            bank = termsmod.TermBank(p.dir)
            status = body.get("status")
            if status is not None and status not in termsmod.STATUSES:
                return self._json({"error": f"status must be one of {termsmod.STATUSES}"}, 400)
            if bank.set(str(body.get("key") or ""), status=status) is None:
                return self._json({"error": "unknown entry"}, 404)
            bank.save()
            self._json({"ok": True, **self.app.resume_terms_payload(p)})
        elif u.path == "/api/resume/terms/delete":
            bank = termsmod.TermBank(p.dir)
            if not bank.remove(str(body.get("key") or "")):
                return self._json({"error": "unknown entry"}, 404)
            bank.save()
            self._json({"ok": True, **self.app.resume_terms_payload(p)})
        elif u.path == "/api/resume/terms/add":
            bank = termsmod.TermBank(p.dir)
            kind, name = str(body.get("kind") or ""), str(body.get("name") or "").strip()
            if kind not in termsmod.KINDS or not name:
                return self._json({"error": "kind and name are required"}, 400)
            split = lambda v: [t.strip() for t in str(v or "").replace(" = ", "|").split("|") if t.strip()]
            if kind == "synonyms":
                parts = split(body.get("terms") or name)
                e = bank.propose("synonyms", " = ".join(parts[:2]), terms=parts, by="", status="approved")
            elif kind == "skill":
                e = bank.propose("skill", name, aliases=split(body.get("aliases")), by="", status="approved")
            else:
                e = bank.propose("headline", name, matches=split(body.get("matches")) or [name], by="", status="approved")
            if e is None:
                return self._json({"error": "an entry like it exists, or a synonym group needs two members"}, 400)
            bank.save()
            self._json({"ok": True, **self.app.resume_terms_payload(p)})
        elif u.path == "/api/about":
            facts = body.get("facts")
            if not isinstance(facts, list):
                return self._json({"error": "facts must be a list"}, 400)
            before = factsmod.read_facts(p.dir)
            kept_auto = {f.get("text") for f in facts if isinstance(f, dict) and f.get("auto")}
            removed = [factsmod.auto_company(f) for f in before if f.auto and f.text not in kept_auto]
            if any(removed):
                sup = factsmod.load_suppressed(p.data_dir) | {c.lower() for c in removed if c}
                factsmod.save_suppressed(p.data_dir, sup)
            factsmod.write_facts(p.dir, facts)
            self.app.reload()
            p2 = self.app.profile(p.name)
            self.app.relearn_later(p2)
            self._json({"ok": True, **self._about_payload(p2)})
        elif u.path == "/api/about/add":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json({"error": "empty fact"}, 400)
            factsmod.write_facts(p.dir, factsmod.read_facts(p.dir) + [factsmod.Fact(text, False)])
            self.app.reload()
            p2 = self.app.profile(p.name)
            self.app.relearn_later(p2)
            self._json({"ok": True, **self._about_payload(p2)})
        elif u.path == "/api/about/upload":
            try:
                res = about.ingest_upload(p.dir, str(body.get("name") or "upload.txt"), body.get("content_b64") or "",
                                          p.search.get("llm") or {}, (p.answers.get("identity") or {}).get("first_name", ""))
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
            self.app.reload()
            p2 = self.app.profile(p.name)
            self.app.relearn_later(p2)
            self._json({"ok": True, "result": res, **self._about_payload(p2)})
        elif u.path == "/api/state/undo":
            jid = body.get("id")
            if not jid or not p.store.get(jid):
                return self._json({"error": "unknown job id"}, 404)
            st = p.store.undo(jid)
            relearn(p)
            self._json({"ok": True, "state": st})
        else:
            self._json({"error": "not found"}, 404)

    def _about_payload(self, p: Profile) -> dict:
        facts = profile_facts(p)
        judged = judgemod.load(p.data_dir)
        now = factsmod.facts_sha(facts)
        # How many ratings were made under a different set of facts. The page says so plainly,
        # because the answer is "the next scan re-reads them", not "this is already applied".
        stale = sum(1 for j in judged.values() if j.get("facts_sha") and j["facts_sha"] != now)
        return {"profile": p.name, "display_name": p.display_name,
                "facts": [f.to_dict() for f in facts],
                "about_path": str(factsmod.about_path(p.dir)),
                "facts_sha": now, "rated": len(judged), "stale_ratings": stale,
                "uploads": about.list_uploads(p.dir),
                "llm_backend": (p.search.get("llm") or {}).get("backend", "none")}


def serve(root: Path, port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1"):
    app = AppState(root)
    Handler.app = app
    app.ensure_pump()   # pick up runs queued before a restart
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"jobhunt dashboard: {url}   profiles: {', '.join(app.profiles) or '(none)'}   (Ctrl+C to stop)")
    # Only auto-open a browser on a local GUI session. Over SSH (or in an editor's remote terminal,
    # which sets $BROWSER to a helper that opens URLs on the *client* machine) just print the URL.
    remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY") or os.environ.get("BROWSER"))
    if open_browser and not remote:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    elif open_browser:
        print("(remote session detected: open the URL yourself on the Mac, e.g. through NoMachine)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return app
