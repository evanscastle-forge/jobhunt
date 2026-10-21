#!/usr/bin/env python3
"""jobhunt — scan public job feeds, review them in a local dashboard, and prefill applications.

  python jobhunt.py scan                    the whole pipeline: fetch, gate, measure, judge, score
  python jobhunt.py scan --stage score      run some stages; the rest replay the last run's files
  python jobhunt.py rescore                 re-score cached postings after editing a search.toml (offline)
  python jobhunt.py retune                  re-grade from stored ratings (no network, no model)
  python jobhunt.py config                  the effective weights, and complaints about them
  python jobhunt.py explain <job_id>        every term, its measure, weight, points and evidence
  python jobhunt.py audit                   every recorded change to the scoring model
  python jobhunt.py serve                   dashboard at http://127.0.0.1:8765 (all profiles)
  python jobhunt.py profiles                list profiles and their counts
  python jobhunt.py --profile <name> list --min-grade B
  python jobhunt.py --profile <name> apply <job_id> [--run id] [--headless] [--no-hold]
  python jobhunt.py --profile <name> runs [--all]     apply runs waiting on you (data/runs/)
  python jobhunt.py --profile <name> status <job_id> skip --reason location
  python jobhunt.py --profile <name> status <job_id> done --done-reason "no offer"
  python jobhunt.py --profile <name> open <job_id>
  python jobhunt.py --profile <name> export jobs.csv
  python jobhunt.py schedule --write        write launchd plists to deploy/ (never loads them)

Layout: config.toml (shared boards + vocab), profiles/<name>/{search.toml, profile.toml, notes/, data/},
data/postings.json (last raw fetch, shared). AGENTS.md is the scoring methodology.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import webbrowser
from pathlib import Path

def _audit_line(r: dict) -> str:
    """One readable line per event. The kinds are not uniform because the pathways are not: a fact
    changes a score by changing what the model reads, a weight changes it by arithmetic."""
    k = r.get("kind")
    if k == "facts_changed":
        bits = []
        for sign, key in (("+", "added"), ("-", "removed")):
            for t in (r.get(key) or [])[:3]:
                bits.append(f"{sign}{t[:58]}")
        more = len(r.get("added") or []) + len(r.get("removed") or []) - len(bits)
        return (f"{r.get('n')} facts, now {r.get('sha')}"
                + (("   " + "; ".join(bits)) if bits else "")
                + (f" (+{more} more)" if more > 0 else ""))
    if k == "judged":
        return (f"{r.get('postings')} of {r.get('of')} postings, {r.get('slots')} slots, "
                f"{r.get('seconds')}s   model {r.get('model') or r.get('backend')}   "
                f"prompt {r.get('prompt_sha')}   facts {r.get('facts_sha')}")
    if k == "weights_changed":
        w = r.get("weights") or {}
        return (f"now {r.get('sha')} (was {r.get('was') or 'nothing'})   role x{w.get('role_weight')}"
                f"  overall x{w.get('overall_fit')}   practical "
                + " ".join(f"{n}:{v}" for n, v in sorted((w.get('practical') or {}).items())))
    if k == "curve_refit":
        c, was = r.get("cutoffs") or [], r.get("was") or []
        return (f"{r.get('n')} postings   A+ at {c[0] if c else '?'}, B- at {c[5] if len(c) > 5 else '?'}"
                + (f"   (was A+ {was[0]}, B- {was[5] if len(was) > 5 else '?'})" if was else ""))
    if k == "discovered":
        return (f"{r.get('probed')} companies probed in {r.get('requests')} requests   "
                f"+{r.get('promoted')} promoted, {r.get('parked')} parked, {r.get('dead')} dead   "
                f"{r.get('boards_now')} boards now")
    if k == "scan":
        f = r.get("failed") or []
        return (f"{r.get('fetched')} postings from {r.get('boards')} boards"
                + (f", {len(f)} failed" if f else "")
                + f"   stages {','.join(r.get('stages') or [])}")
    return json.dumps({k2: v for k2, v in r.items() if k2 not in ("at", "kind")})[:120]


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from jobhunt.classify import learn  # noqa: E402
from jobhunt.core import DONE_REASONS, STATUSES, relearn, rescore, scan  # noqa: E402
from jobhunt.pipeline import STAGES  # noqa: E402
from jobhunt import audit as auditmod  # noqa: E402
from jobhunt.llm import fit_note  # noqa: E402
from jobhunt.profiles import disabled_names, load_all, load_env, pick_profile  # noqa: E402

PLIST_SERVE = """<?xml version="1.0" encoding="UTF-8"?>
<!-- jobhunt dashboard, always on at http://127.0.0.1:{port}/
     install:  cp deploy/local.jobhunt-serve.plist ~/Library/LaunchAgents/
               launchctl load ~/Library/LaunchAgents/local.jobhunt-serve.plist -->
<plist version="1.0">
<dict>
    <key>Label</key><string>local.jobhunt-serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{root}/jobhunt.py</string>
        <string>serve</string>
        <string>--no-browser</string>
        <string>--port</string><string>{port}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path}</string>
    </dict>
    <key>WorkingDirectory</key><string>{root}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{root}/data/serve.log</string>
    <key>StandardErrorPath</key><string>{root}/data/serve.log</string>
</dict>
</plist>
"""

PLIST_SCAN = """<?xml version="1.0" encoding="UTF-8"?>
<!-- jobhunt daily scan at {hour:02d}:{minute:02d} local time (the Mac's clock; keep it on Eastern)
     install:  cp deploy/local.jobhunt-scan.plist ~/Library/LaunchAgents/
               launchctl load ~/Library/LaunchAgents/local.jobhunt-scan.plist -->
<plist version="1.0">
<dict>
    <key>Label</key><string>local.jobhunt-scan</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{root}/jobhunt.py</string>
        <string>scan</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>{path}</string>
    </dict>
    <key>WorkingDirectory</key><string>{root}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{hour}</integer>
        <key>Minute</key><integer>{minute}</integer>
    </dict>
    <key>StandardOutPath</key><string>{root}/data/scan.log</string>
    <key>StandardErrorPath</key><string>{root}/data/scan.log</string>
</dict>
</plist>
"""


def apply_browser(p) -> dict:
    """[apply] browser / persistent_profile -> run_apply's browser arguments (jobhunt/apply/browser.py)."""
    cfg = p.search.get("apply") or {}
    persistent = cfg.get("persistent_profile", True)
    return {"browser_dir": (p.data_dir / "browser") if persistent else None, "channel": cfg.get("browser", "chrome")}


def heal_args(p, root: Path) -> dict:
    """Self-healing widgets (jobhunt/apply/heal.py): recipes are about widgets, not people, so they are
    shared across profiles under data/apply/recipes.json. No LLM backend: cached recipes only."""
    from jobhunt import llm
    cfg = p.search.get("llm") or {}
    call = None if (cfg.get("backend") or "none").lower() == "none" else (lambda prompt: llm._call(cfg, prompt, max_tokens=700))
    from jobhunt.apply.vault import Vault, VaultError
    try:
        vault = Vault(p.data_dir / "vault.enc", root)
    except VaultError:
        vault = None                  # Workday runs pause with reason "vault" until you run `vault init`
    return {"heal_call": call, "recipes_path": Path(root) / "data" / "apply" / "recipes.json",
            "resolve_cache": Path(root) / "data" / "apply" / "resolved.json", "vault": vault}


def llm_cfg(p, task: str = None) -> dict:
    """[llm], with [llm.<task>] laid over it when the task has its own table."""
    from jobhunt import llm
    return llm.task_cfg(p.search.get("llm") or {}, task)


def llm_call(p, max_tokens: int = 900, task: str = None):
    """A model call from [llm] (or [llm.<task>]), or None when the backend is off."""
    from jobhunt import llm
    cfg = llm_cfg(p, task)
    if (cfg.get("backend") or "none").lower() == "none":
        return None
    return lambda prompt: llm._call(cfg, prompt, max_tokens=max_tokens)


def resume_call(p):
    """The tailor's one call and the label a variant records for it. [llm.resume] names the model: Fable at
    low effort (20 Oct 2026), because reading a posting for its terms is extraction, not judgement."""
    from jobhunt import llm
    return llm_call(p, 1500, task="resume"), llm.label(llm_cfg(p, "resume"))


def tailor_resume(p, job, run, profile: dict, use_model: bool = True):
    """A per-posting resume variant (jobhunt/apply/tailor.py) replaces documents.resume for this run. No
    resume/master.html, or anything going wrong: the base resume is uploaded and the run says so. Runs
    before run_apply starts its browser, because rendering needs a headless Chromium of its own."""
    if not (p.search.get("apply") or {}).get("tailor_resume", True):
        return None
    from jobhunt.apply import tailor
    if tailor.load_master(p.dir) is None:
        run.step("resume: no resume/master.html for this profile; uploading the base resume")
        return None
    call, model = resume_call(p) if use_model else (None, "")
    run.step("tailoring the resume to the posting" + (f" (one call to {model})" if call else ""))
    try:
        v = tailor.build_variant(p.dir, job, base=p.resume_path, call=call, model=model, run_id=run.id, log=print)
    except Exception as e:  # noqa: BLE001
        run.step(f"resume: could not build a variant ({type(e).__name__}: {e}); uploading the base resume")
        return None
    if v is None:
        run.step("resume: the variant overflowed the master's page count; uploading the base resume")
        return None
    profile.setdefault("documents", {})["resume"] = str(v.pdf)
    run.data["resume"] = v.record()
    run.save()
    run.step(f"resume: {v.name}: {v.summary()}")
    print(f"resume: {v.name} ({v.summary()}) -> {v.folder}")
    return v


def rehearse_parallel(p, root: Path, ids: list, args) -> list:
    """Each posting in its own `rehearse --child` process, `args.workers` at a time. A process that outlives
    `args.timeout` is killed with its whole process group (its Chrome too) and its run marked failed, so
    one stuck form can neither stall the batch nor leave an orphan browser on a profile slot."""
    import os
    import signal
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    from jobhunt.apply import runs as runsmod

    def one(jid: str):
        cmd = [sys.executable, str(root / "jobhunt.py"), "--root", str(root), "--profile", p.name, "rehearse", jid, "--child"]
        cmd += ["--headless"] if args.headless else []
        cmd += ["--no-agent"] if args.no_agent else []
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
        rid = next((ln.split()[1] for ln in out.splitlines() if ln.startswith("RUN ")), None)
        run = runsmod.load_run(p.data_dir, rid) if rid else None
        if run is not None and run.state in ("running", "queued"):
            run.fail(f"timed out after {args.timeout}s; killed with its browser")
        print(f"-- {jid}: {run.state if run else 'no run'}" + (f" ({run.get('reason')})" if run and run.get("reason") else ""),
              flush=True)
        return run

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        return [r for r in pool.map(one, ids) if r is not None]


def apply_stats_text(p) -> str:
    from jobhunt.apply import runs as runsmod
    from jobhunt.apply.stats import apply_stats, render
    return render(apply_stats(runsmod.list_runs(p.data_dir)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jobhunt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(HERE), help="project folder (contains config.toml and profiles/)")
    ap.add_argument("--profile", default=None, help="profile name (folder under profiles/); default from config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="fetch and score postings for every profile")
    s.add_argument("--notes", action="store_true", help="also ask the LLM (if enabled) for a fit note on new postings")
    s.add_argument("--workers", type=int, default=12)
    s.add_argument("--no-judge", action="store_true", help="skip the LLM pass that rates role attributes")
    s.add_argument("--judge-limit", type=int, default=0, help="cap on postings per profile the LLM rates per run (0 = all)")
    s.add_argument("--stage", action="append", choices=list(STAGES), default=None,
                   help="run only these stages (repeatable); the rest replay from the last run's files")

    sub.add_parser("rescore", help="re-score cached postings (no network)")
    sub.add_parser("profiles", help="list profiles")

    v = sub.add_parser("serve", help="run the local dashboard")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-browser", action="store_true")

    a = sub.add_parser("apply", help="fill an application form, then pause for your review (never submits)")
    a.add_argument("job_id")
    a.add_argument("--run", default=None, help="existing run id under data/runs/ (the dashboard passes this)")
    a.add_argument("--headless", action="store_true", help="no visible browser (fill and report only)")
    a.add_argument("--no-hold", action="store_true", help="close the browser as soon as filling is done")
    a.add_argument("--ats", choices=["greenhouse", "lever", "ashby"], default=None,
                   help="force the form kind (e.g. a careers page that embeds Greenhouse)")
    a.add_argument("--rehearse", action="store_true", help="fill and verify, then close: never submits, never waits")
    a.add_argument("--no-tailor", action="store_true", help="upload documents.resume as is (no per-posting variant)")

    rh = sub.add_parser("rehearse", help="fill and verify real forms without applying, to measure apply-stats")
    rh.add_argument("job_ids", nargs="*", help="postings to rehearse (default: saved postings on supported forms)")
    rh.add_argument("--status", default="saved", help="which postings when none are named (default: saved)")
    rh.add_argument("--limit", type=int, default=10)
    rh.add_argument("--headless", action="store_true")
    rh.add_argument("--no-agent", action="store_true", help="do not ask the applicant agent (faster; measures rules alone)")
    rh.add_argument("--workers", type=int, default=1, help="rehearse this many postings at once, each in its own process")
    rh.add_argument("--timeout", type=int, default=600, help="seconds before a stuck posting is killed")
    rh.add_argument("--child", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("apply-stats", help="how close applying is to one click, from the run folders")

    pr = sub.add_parser("prepare", help="rehearse saved postings and list only what still needs you")
    pr.add_argument("--status", default="saved")
    pr.add_argument("--limit", type=int, default=50)
    pr.add_argument("--workers", type=int, default=4)
    pr.add_argument("--timeout", type=int, default=600)

    rp = sub.add_parser("replay", help="re-run the answer rules against every captured form, offline, and diff")
    rp.add_argument("run_ids", nargs="*", help="runs to replay (default: every run with a captured form)")
    rp.add_argument("--limit", type=int, default=40, help="how many changed decisions to print")

    rs = sub.add_parser("reset", help="clear postings, Saved and apply runs between testing and real use (never notes or answers)")
    rs.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    vt = sub.add_parser("vault", help="portal logins (Workday): init | list | show <site>")
    vt.add_argument("action", choices=["init", "list", "show"])
    vt.add_argument("site", nargs="?")

    fx = sub.add_parser("fixture", help="turn a field captured in a run into a regression fixture under tests/")
    fx.add_argument("run_id")
    fx.add_argument("label", help="part of the field's label")

    ak = sub.add_parser("ask", help="the applicant agent: answer an application question as you, or decline")
    ak.add_argument("question", nargs="?", help="the question (omit with --pending)")
    ak.add_argument("--options", default="", help="choices, separated by |")
    ak.add_argument("--job", default=None, help="a posting id, for company/role context")
    ak.add_argument("--pending", action="store_true", help="propose answers for pending answer-bank questions")
    ak.add_argument("--limit", type=int, default=0)
    ak.add_argument("--show-kb", action="store_true", help="print the knowledge base the agent answers from")

    rv = sub.add_parser("resume", help="per-posting resume variants: init | check | terms | approve | reject | add | variant | propose")
    rv.add_argument("action", choices=["init", "check", "terms", "approve", "reject", "add", "variant", "propose"])
    rv.add_argument("args", nargs="*", help="keys (approve/reject), the entry text (add), or a posting id (variant)")
    rv.add_argument("--pin", action="store_true", help="check: record the master as the transcription baseline")
    rv.add_argument("--pending", action="store_true", help="terms: only pending proposals")
    rv.add_argument("--no-model", action="store_true", help="variant/propose: the deterministic pass only")
    rv.add_argument("--kind", choices=["synonyms", "skill", "headline"], default=None, help="add: what kind of entry")
    rv.add_argument("--aliases", default="", help="add --kind skill: other spellings, separated by |")
    rv.add_argument("--matches", default="", help="add --kind headline: posting-title phrases, separated by |")
    rv.add_argument("--status", default="saved", help="propose: which postings (default: saved)")
    rv.add_argument("--limit", type=int, default=0)

    iss = sub.add_parser("issues", help="list a run's issues, or answer one (the CLI side of the Needs-you tab)")
    iss.add_argument("run_id", nargs="?", help="run id (default: the newest open run)")
    iss.add_argument("--answer", nargs=2, metavar=("ISSUE", "TEXT"))
    iss.add_argument("--approve", metavar="ISSUE")
    iss.add_argument("--skip", metavar="ISSUE")
    iss.add_argument("--not-same", dest="not_same", metavar="ISSUE")

    rn = sub.add_parser("runs", help="list apply runs for the profile (open ones by default)")
    rn.add_argument("--all", action="store_true")

    o = sub.add_parser("open", help="open the posting URL")
    o.add_argument("job_id")

    ex = sub.add_parser("explain", help="show, term by term, why a posting ranks where it does")
    ex.add_argument("job_id")

    rt = sub.add_parser("retune", help="re-grade from stored ratings after a weight change (no network, no model)")
    rt.add_argument("--top", type=int, default=15, help="how many of the best to print")

    cc = sub.add_parser("config", help="show the effective scoring config and complain about it")
    cc.add_argument("what", nargs="?", default="check", choices=["check"])

    dc = sub.add_parser("discover", help="find companies worth watching and add their boards")
    dc.add_argument("--harvest", action="store_true", help="only collect company names (no network)")
    dc.add_argument("--limit", type=int, default=0, help="cap how many companies to probe this run")
    dc.add_argument("--recheck-days", type=int, default=None, help="how long a parked board waits")
    dc.add_argument("--status", action="store_true", help="just report the registry")
    dc.add_argument("--reassess", action="store_true",
                    help="re-run the gate over boards already discovered, and demote what fails now")
    dc.add_argument("--workday", action="store_true",
                    help="also sweep Workday for companies no feed answered for (slow, opt-in)")

    au = sub.add_parser("audit", help="every recorded change to the scoring model, newest last")
    au.add_argument("--kind", choices=list(auditmod.KINDS), default=None)
    au.add_argument("--limit", type=int, default=40)
    au.add_argument("--full", action="store_true", help="print each line's raw JSON as well")

    ls = sub.add_parser("list", help="print postings at or above the profile's floor")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--min-grade", default=None, help="default: [scoring] min_grade; F shows everything")
    ls.add_argument("--limit", type=int, default=50)

    ex = sub.add_parser("export", help="write a CSV")
    ex.add_argument("path")

    st = sub.add_parser("status", help="set a status: jobhunt.py status <job_id> <status> [--reason r]")
    st.add_argument("job_id")
    st.add_argument("status", choices=STATUSES)
    st.add_argument("--reason", choices=learn.SKIP_REASONS, default=None, help="why (for skip)")
    st.add_argument("--done-reason", choices=DONE_REASONS, default=None, help="why (for done)")

    sc = sub.add_parser("schedule", help="write launchd plists to deploy/ (does not load them)")
    sc.add_argument("--write", action="store_true")
    sc.add_argument("--hour", type=int, default=2)
    sc.add_argument("--minute", type=int, default=0)
    sc.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    shared, profiles = load_all(root)
    parked = disabled_names(root)

    if args.cmd == "profiles":
        for name, p in profiles.items():
            rows = p.store.merged()
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["state"]["status"]] = counts.get(r["state"]["status"], 0) + 1
            flag = " (default)" if p is pick_profile(profiles, None, shared) else ""
            print(f"{name:<12} {p.display_name:<12} {len(rows):4d} postings  {counts}{flag}"
                  + (f"  ERRORS: {p.errors}" if p.errors else ""))
        from jobhunt.profiles import is_example
        for name in parked:
            why = ("documentation only; profiles named example-* never scan"
                   if is_example(name) else f"parked (enabled = false in profiles/{name}/search.toml)")
            print(f"{name:<16} {'—':<10} {why}")
        return 0

    if args.cmd == "scan":
        result = scan(root, profiles, workers=args.workers, env=load_env(root),
                      judge=not args.no_judge, judge_limit=args.judge_limit,
                      stages=tuple(args.stage) if args.stage else None)
        if args.notes:
            for name, p in profiles.items():
                for jid in result["profiles"][name].get("new_ids", [])[:25]:
                    job = p.store.get(jid)
                    note = fit_note(p.search, p.answers, job, notes=p.notes) if job else None
                    if note:
                        p.store.set_state(jid, notes=note)
        return 0 if not result.get("error") else 1

    if args.cmd == "rescore":
        result = rescore(root, profiles)
        return 0 if not result.get("error") else 1

    if args.cmd == "serve":
        from jobhunt.report.server import serve
        serve(root, port=args.port, open_browser=not args.no_browser)
        return 0

    if args.cmd == "schedule":
        python = root / ".venv" / "bin" / "python"
        if not python.exists():
            python = Path(sys.executable)
        # launchd starts jobs with a bare PATH: without this the `claude` CLI (the LLM backend) is not found and
        # the scan's rating step and the apply agent silently get no model
        import shutil
        dirs = [str(Path(p).parent) for p in (shutil.which("claude"), shutil.which("uv")) if p]
        path = ":".join(dict.fromkeys(dirs + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
        files = {
            "local.jobhunt-serve.plist": PLIST_SERVE.format(python=python, root=root, port=args.port, path=path),
            "local.jobhunt-scan.plist": PLIST_SCAN.format(python=python, root=root, hour=args.hour, minute=args.minute,
                                                                    path=path),
        }
        out = root / "deploy"
        out.mkdir(exist_ok=True)
        for name, body in files.items():
            if args.write:
                (out / name).write_text(body, encoding="utf-8")
                print(f"wrote {out / name}")
            else:
                print(f"--- {name}\n{body}")
        print("\nNothing was installed. To enable, copy the plist(s) to ~/Library/LaunchAgents/ and `launchctl load` them "
              "(see deploy/README.md).")
        return 0

    # ---- profile-scoped commands
    p = pick_profile(profiles, args.profile, shared, disabled=parked)
    store = p.store

    if args.cmd == "apply":
        from jobhunt.apply import answers as bankmod
        from jobhunt.apply import runs as runsmod
        from jobhunt.apply.forms import apply_page_url, detect_ats, run_apply
        job = store.get(args.job_id)
        if not job:
            print(f"unknown job id {args.job_id!r} for profile {p.name} (run `jobhunt.py --profile {p.name} list`)")
            return 2
        run = runsmod.load_run(p.data_dir, args.run) if args.run else None
        if args.run and run is None:
            print(f"unknown run id {args.run!r}")
            return 2
        if run is None:
            url = apply_page_url(job)
            run = runsmod.new_run(p.data_dir, job, p.name, ats=detect_ats(url), url=url)
        print(f"run folder: {run.path}")
        from jobhunt.apply import persona
        agent = persona.for_profile(p)
        # prose questions nothing covers are drafted by the applicant agent with the posting in hand
        # (grounded, cited, or declined); a draft is a suggestion on the run's panel, never typed
        drafter = (lambda label, j: agent.ask(label, "textarea", None, j).answer) if agent else None
        ats = args.ats or (run.get("ats") if run.get("ats") in ("greenhouse", "lever", "ashby") else None)
        profile = p.answers_for_apply()
        if not args.no_tailor and not args.rehearse:
            tailor_resume(p, job, run, profile)
        report = run_apply(job, profile, headless=args.headless, wait_for_user=not args.no_hold,
                           drafter=drafter, run=run, bank=bankmod.Bank(p.dir), ats=ats, rehearse=args.rehearse, agent=agent,
                           on_submitted=lambda: store.set_state(args.job_id, status="applied"),
                           **apply_browser(p), **heal_args(p, root))
        if report.submitted:
            relearn(p)
        print(f"run {run.id}: {run.state}" + (f" ({run.get('reason')})" if run.get("reason") else ""))
        return 0 if not report.error else 1

    if args.cmd == "rehearse":
        from jobhunt.apply import answers as bankmod
        from jobhunt.apply import runs as runsmod
        from jobhunt.apply.forms import SUPPORTED, apply_page_url, detect_ats, run_apply
        ids = args.job_ids or [r["id"] for r in store.merged() if r["state"]["status"] == args.status][:args.limit]
        if not ids:
            print(f"nothing to rehearse: no {args.status} postings on {', '.join(SUPPORTED)} forms")
            return 0
        from datetime import datetime
        from jobhunt.apply import persona
        from jobhunt.apply.stats import rehearsal_report
        if not args.child and len(ids) > 1:
            done = rehearse_parallel(p, root, ids, args)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = runsmod.runs_dir(p.data_dir) / f"rehearsal-{stamp}.md"
            out.write_text(rehearsal_report(done, f"Rehearsal {stamp}"), encoding="utf-8")
            print("\n" + apply_stats_text(p))
            print(f"\nreport: {out}")
            return 0
        agent = None if args.no_agent else persona.for_profile(p)
        done = []
        for jid in ids:
            job = store.get(jid)
            if not job:
                print(f"unknown job id {jid!r}")
                continue
            url = apply_page_url(job)
            run = runsmod.new_run(p.data_dir, job, p.name, ats=detect_ats(url), url=url)
            print(f"\n== {job.get('company')} · {job.get('title')}  (run {run.id})")
            print(f"RUN {run.id}", flush=True)
            logf = run.log_path().open("a", encoding="utf-8")
            import time as _time
            t0 = _time.monotonic()

            def tee(msg, _f=logf, _t0=t0):
                line = f"[{int(_time.monotonic() - _t0) // 60:02d}:{int(_time.monotonic() - _t0) % 60:02d}] {msg}"
                print(line, flush=True)
                _f.write(line + "\n")
                _f.flush()
            try:
                run_apply(job, p.answers_for_apply(), headless=args.headless, wait_for_user=False, run=run,
                          bank=bankmod.Bank(p.dir), rehearse=True, log=tee, agent=agent, **apply_browser(p),
                          **heal_args(p, root))
            except Exception as e:  # noqa: BLE001  one broken form must not stop the batch
                import traceback
                tee(traceback.format_exc())
                run.data["error_trace"] = traceback.format_exc()
                run.fail(f"{type(e).__name__}: {e}")
            finally:
                logf.close()
            done.append(runsmod.load_run(p.data_dir, run.id) or run)
        if args.child:
            return 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = runsmod.runs_dir(p.data_dir) / f"rehearsal-{stamp}.md"
        out.write_text(rehearsal_report(done, f"Rehearsal {stamp}"), encoding="utf-8")
        print("\n" + apply_stats_text(p))
        print(f"\nreport: {out}")
        return 0

    if args.cmd == "resume":
        from jobhunt.apply import tailor
        from jobhunt.apply import terms as termsmod
        rdir = termsmod.resume_dir(p.dir)
        if args.action == "init":
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / tailor.VARIANTS).mkdir(exist_ok=True)
            if not termsmod.terms_path(p.dir).exists():
                termsmod.write_terms(p.dir, [])
            link = rdir / "original.pdf"
            if p.resume_path and p.resume_path.exists() and not link.exists():
                link.symlink_to(os.path.relpath(p.resume_path, rdir))
            print(f"{rdir}\n  terms.md        the bank (empty)\n  original.pdf    -> {p.resume_path}\n  variants/       one folder per build")
            if tailor.load_master(p.dir) is None:
                print(f"next: transcribe {p.resume_path.name if p.resume_path else 'your resume'} into {rdir / tailor.MASTER} "
                      f"(see tests/fixtures/resume_master.html for the shape), then `resume check`")
            return 0
        if args.action == "check":
            master = tailor.load_master(p.dir)
            if master is None:
                print(f"no {rdir / tailor.MASTER}; run `resume init` first")
                return 2
            for prob in tailor.check_shape(master.html):
                print(f"shape: {prob}")
            out = rdir / tailor.MASTER_PDF
            pages = tailor.render_pdf(master.html, out)
            orig = p.resume_path
            orig_ok = bool(orig and orig.exists())
            same = uris_same = None
            if orig_ok:
                a, b = tailor.text_norm(tailor.pdf_text(orig)), tailor.text_norm(tailor.pdf_text(out))
                same = a == b
                uris_same = tailor.pdf_uris(orig) == tailor.pdf_uris(out)
                print(f"rendered {out}: {pages} page(s); original {orig.name}: {tailor.pdf_pages(orig)} page(s)")
                print(f"text {'identical to' if same else 'DIFFERS from'} the original; links {'match' if uris_same else 'DIFFER'}")
                if not same:
                    import difflib
                    aw, bw = tailor.pdf_text(orig).split(), tailor.html_text(master.html).split()
                    for line in difflib.unified_diff(aw, bw, "original", "master", lineterm="", n=3):
                        print("  " + line)
            else:
                print(f"rendered {out}: {pages} page(s); no original to compare with (documents.resume)")
            if args.pin:
                import hashlib
                import datetime as _dt
                rec = {"original": str(orig) if orig_ok else None,
                       "original_sha": hashlib.sha1(orig.read_bytes()).hexdigest()[:12] if orig_ok else None,
                       "master_sha": master.sha, "pages": pages, "matches_original": same, "links_match": uris_same,
                       "checked_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}
                (rdir / tailor.TRANSCRIPTION).write_text(json.dumps(rec, indent=1), encoding="utf-8")
                print(f"pinned: {rdir / tailor.TRANSCRIPTION} (variants may run to {pages} page(s))")
            return 0 if (same is not False) else 1
        bank = termsmod.TermBank(p.dir)
        if args.action == "terms":
            rows = bank.pending() if args.pending else bank.entries
            if not rows:
                print("nothing pending" if args.pending else f"no entries ({termsmod.terms_path(p.dir)})")
            for e in rows:
                members = " | ".join(e.members)
                print(f"{e.key}  {e.status:8s} {e.kind:9s} {e.name}" + (f"  [{members}]" if e.kind != "headline" or members != e.name else "")
                      + (f"\n{'':14s}  by {e.by}" if e.by else "") + (f" · from {e.source}" if e.source else "")
                      + (f"\n{'':14s}  {e.note}" if e.note else ""))
            c = bank.counts()
            print(f"{c['approved']} approved, {c['pending']} pending")
            return 0
        if args.action in ("approve", "reject"):
            if not args.args:
                print("which keys? (`resume terms --pending` lists them)")
                return 2
            for key in args.args:
                e = bank.get(key)
                if e is None:
                    print(f"{key}: unknown key")
                    continue
                if args.action == "approve":
                    bank.set(key, status="approved")
                    print(f"{key}: approved {e.kind} {e.name!r}")
                else:
                    bank.remove(key)
                    print(f"{key}: removed {e.kind} {e.name!r}")
            bank.save()
            return 0
        if args.action == "add":
            text = " ".join(args.args).strip()
            if not args.kind or not text:
                print('usage: resume add --kind synonyms "agentic system | AI agent" | --kind skill "Python" [--aliases "python3"] '
                      '| --kind headline "Senior Machine Learning Engineer" [--matches "machine learning engineer|ML engineer"]')
                return 2
            if args.kind == "synonyms":
                parts = [t.strip() for t in text.replace(" = ", "|").split("|") if t.strip()]
                e = bank.propose("synonyms", " = ".join(parts[:2]), terms=parts, by="", status="approved")
            elif args.kind == "skill":
                e = bank.propose("skill", text, aliases=args.aliases.split("|") if args.aliases else (), by="", status="approved")
            else:
                e = bank.propose("headline", text, matches=args.matches.split("|") if args.matches else (text,), by="", status="approved")
            if e is None:
                print("not added: an entry like it exists, or a synonym group needs two members")
                return 1
            bank.save()
            print(f"{e.key}: approved {e.kind} {e.name!r}")
            return 0
        if args.action == "variant":
            if not args.args:
                print("which posting? `resume variant <job id>`")
                return 2
            job = store.get(args.args[0])
            if not job:
                print(f"unknown job id {args.args[0]!r}")
                return 2
            if tailor.load_master(p.dir) is None:
                print(f"no {rdir / tailor.MASTER}; run `resume init` first")
                return 2
            call, model = (None, "") if args.no_model else resume_call(p)
            v = tailor.build_variant(p.dir, job, base=p.resume_path, call=call, model=model)
            if v is None:
                print("no variant: it overflowed the master's page count (see the newest index line)")
                return 1
            print(f"{v.name}: {v.summary()}\n{v.folder}\n")
            print((v.folder / "diff.md").read_text(encoding="utf-8"))
            return 0
        if args.action == "propose":
            if tailor.load_master(p.dir) is None:
                print(f"no {rdir / tailor.MASTER}; run `resume init` first")
                return 2
            rows = [r for r in store.merged() if r["state"]["status"] == args.status]
            if args.limit:
                rows = rows[:args.limit]
            call, model = (None, "") if args.no_model else resume_call(p)
            total = 0
            for r in rows:
                job = store.get(r["id"]) or r
                posting = tailor.propose_only(p.dir, job, call, model=model)
                n = len(posting.proposals) if posting else 0
                total += n
                print(f"{r['id']}: {n} proposal(s)" + (f", {len(posting.gaps)} gap(s)" if posting and posting.gaps else ""))
            print(f"{total} proposal(s) pending across {len(rows)} posting(s); `resume terms --pending` lists them")
            return 0

    if args.cmd == "ask":
        from jobhunt.apply import answers as bankmod
        from jobhunt.apply import persona
        agent = persona.for_profile(p)
        if agent is None:
            print("no LLM backend: set [llm] backend in config.toml")
            return 2
        if args.show_kb:
            print(agent.kb.render())
            return 0
        if args.pending:
            got = persona.propose_pending(agent, bankmod.Bank(p.dir), limit=args.limit)
            print(f"{got['proposed']} proposed, {got['declined']} declined; review them on the Answers page "
                  f"({bankmod.bank_path(p.dir)})")
            return 0
        if not args.question:
            print("give a question, or --pending")
            return 2
        job = store.get(args.job) if args.job else {}
        opts = [o.strip() for o in args.options.split("|") if o.strip()]
        res = agent.ask(args.question, "select" if opts else "textarea", opts, job or {})
        if res.declined:
            print(f"DECLINED: {res.reason}" + (f"\nwould need: {res.needs}" if res.needs else ""))
        else:
            print(res.answer)
            print(f"\n[evidence: {', '.join(res.evidence)}]" + (f" [{res.reason}]" if res.reason else ""))
            for i, text in agent.kb.get(res.evidence):
                print(f"  {i}: {text[:140]}")
        return 0

    if args.cmd == "prepare":
        from datetime import datetime
        from jobhunt.apply import answers as bankmod
        from jobhunt.apply import persona
        from jobhunt.apply import runs as runsmod
        from jobhunt.apply.stats import needs_you
        ids = [r["id"] for r in store.merged() if r["state"]["status"] == args.status][:args.limit]
        if not ids:
            print(f"nothing to prepare: no {args.status} postings")
            return 0
        args.headless, args.no_agent = True, False
        done = rehearse_parallel(p, root, ids, args)
        agent = persona.for_profile(p)
        if agent is not None:
            persona.propose_pending(agent, bankmod.Bank(p.dir), log=lambda s: None)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = runsmod.runs_dir(p.data_dir) / f"prepare-{stamp}.md"
        out.write_text(needs_you(done), encoding="utf-8")
        print(needs_you(done))
        print(apply_stats_text(p))
        print(f"\ndigest: {out}")
        return 0

    if args.cmd == "reset":
        from jobhunt import reset as resetmod
        print("Reset clears:\n" + "\n".join(f"  - {c}" for c in resetmod.CLEARS))
        print("and keeps:\n" + "\n".join(f"  - {k}" for k in resetmod.KEEPS))
        if not args.yes and input(f"\nReset profile {p.name}? [y/N] ").strip().lower() != "y":
            print("nothing changed")
            return 1
        got = resetmod.reset(p, root)
        print(f"\n{got['postings_cleared']} posting(s) cleared, {got['saved_archived']} saved decision(s) archived, "
              f"{got.get('abandoned', 0)} open run(s) abandoned, {got.get('moved', 0)} run folder(s) archived to {got['archive']}"
              + ("; raw posting cache cleared" if got["raw_cache_cleared"] else ""))
        return 0

    if args.cmd == "vault":
        from jobhunt.apply import vault as vaultmod
        if args.action == "init":
            wrote = vaultmod.init_key(root)
            print("vault key written to .env (never printed)" if wrote else "a vault key is already in .env")
            return 0
        try:
            v = vaultmod.Vault(p.data_dir / "vault.enc", root)
        except vaultmod.VaultError as e:
            print(e)
            return 2
        if args.action == "list":
            for s in v.sites():
                rec = v.get(s)
                print(f"{s:60} {rec['email']:32} created {rec['created']}  {'verified' if rec.get('verified') else 'not verified'}")
            return 0
        rec = v.get(args.site or "")
        if not rec:
            print("no login for that site; `vault list` shows the sites")
            return 2
        print(f"{args.site}\n  email: {rec['email']}\n  password: {rec['password']}")
        return 0

    if args.cmd == "replay":
        from jobhunt.apply import answers as bankmod
        from jobhunt.apply import replay as replaymod
        from jobhunt.apply import runs as runsmod
        base = runsmod.runs_dir(p.data_dir)
        dirs = [base / r for r in args.run_ids] if args.run_ids else sorted(d for d in base.iterdir() if (d / "form.html").exists())
        jobs = {}
        for d in dirs:
            meta = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").exists() else {}
            if meta.get("job_id") and store.get(meta["job_id"]):
                jobs[meta["job_id"]] = store.get(meta["job_id"])
        results = replaymod.replay(dirs, p.answers_for_apply(), jobs, bank=bankmod.Bank(p.dir))
        print(replaymod.render(results, limit=args.limit))
        return 0

    if args.cmd == "fixture":
        import re as _re
        from jobhunt.apply import runs as runsmod
        d = runsmod.runs_dir(p.data_dir) / args.run_id
        recs = json.loads((d / "fields.json").read_text(encoding="utf-8")) if (d / "fields.json").exists() else []
        rec = next((r for r in recs if args.label.lower() in r["label"].lower() and r.get("html")), None)
        if rec is None:
            print("no captured field with that label (only fields that failed or were asked keep their HTML)")
            return 2
        slug = _re.sub(r"[^a-z0-9]+", "-", f"{d.name[16:40]}-{rec['label'][:40]}".lower()).strip("-")
        out = root / "tests" / "fixtures" / "captured"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{slug}.html").write_text(f"<!doctype html><html><body><form>{rec['html']}</form></body></html>\n", encoding="utf-8")
        (out / f"{slug}.json").write_text(json.dumps({"label": rec["label"], "kind": rec["kind"], "from_run": d.name,
                                                      "action_then": rec["action"]}, indent=1) + "\n", encoding="utf-8")
        print(f"wrote tests/fixtures/captured/{slug}.html; ApplyTest.test_captured_fixtures reads it on every run")
        return 0

    if args.cmd == "apply-stats":
        print(apply_stats_text(p))
        return 0

    if args.cmd == "issues":
        from jobhunt.apply import issues as issuesmod
        from jobhunt.apply import runs as runsmod
        run = runsmod.load_run(p.data_dir, args.run_id) if args.run_id else next(
            (r for r in runsmod.list_runs(p.data_dir) if r.state == "needs_you"), None)
        if run is None:
            print("no such run" if args.run_id else "no run is waiting on you")
            return 2
        act = (("answer", *args.answer) if args.answer else ("approve", args.approve, None) if args.approve
               else ("skip", args.skip, None) if args.skip else ("not_same", args.not_same, None) if args.not_same else None)
        if act:
            known = {i.id for i in issuesmod.read_issues(run.path)}
            if act[1] not in known:
                print(f"no issue {act[1]!r} on run {run.id}")
                return 2
            issuesmod.respond(run.path, act[1], act[0], act[2])
            if run.state == "needs_you" and runsmod.pid_alive(run.get("pid")):
                run.write_control("resume")
                print(f"{act[0]} recorded; the open window is resuming")
            else:
                print(f"{act[0]} recorded; it applies the next time this run resumes (the window is gone, so "
                      f"approved answers reach the bank on the next run for this posting)")
                if act[0] in ("answer", "edit", "approve"):
                    from jobhunt.apply import answers as bankmod
                    issuesmod.apply_responses(issuesmod.take_responses(run.path),
                                              issuesmod.read_issues(run.path), bankmod.Bank(p.dir))
            return 0
        rows = issuesmod.read_issues(run.path)
        print(f"run {run.id}: {run.state}" + (f" ({run.get('reason')})" if run.get("reason") else "") +
              f" · {run.get('company') or ''} · {run.get('title') or ''}")
        for i in rows:
            flag = "!" if i.blocking else " "
            print(f" {flag} {i.id:<22} {i.kind:<15} {i.label[:70]}")
            if i.proposal:
                print(f"   {'':<22} proposal: {i.proposal[:100]}")
            if i.why:
                print(f"   {'':<22} {i.why[:100]}")
        if not rows:
            print("  no issues: " + ("ready, press Submit in the window" if (run.get("report") or {}).get("ready") else "nothing recorded"))
        return 0

    if args.cmd == "runs":
        from jobhunt.apply import runs as runsmod
        rows = runsmod.list_runs(p.data_dir)
        shown = 0
        for r in rows:
            if not args.all and r.state in runsmod.TERMINAL:
                continue
            s = r.summary()
            print(f"{r.id:<48} {r.state:<10} {(r.get('reason') or ''):<18} {s['open_questions']:2d} open q  "
                  f"{(r.get('company') or '')[:20]:<20} {(r.get('title') or '')[:40]}")
            shown += 1
        print(f"({shown} shown; {len(rows)} total; folders under {runsmod.runs_dir(p.data_dir)})")
        return 0

    if args.cmd == "open":
        job = store.get(args.job_id)
        if not job:
            print("unknown job id")
            return 2
        webbrowser.open(job["url"])
        return 0

    if args.cmd == "config":
        from jobhunt.classify import judge as judgemod
        from jobhunt.classify import relevance as rel
        weights, notes = rel.load_weights(p.search)
        print(f"profile {p.name}   floor = {rel.min_grade(p.search)}")
        if notes:
            print("\nproblems:")
            for n in notes:
                print(f"  {n}")
        else:
            print("\nno problems with the weights")
        pay = (p.search.get("scoring") or {}).get("pay") or {}
        print(f"\npay: -10 at ${int(pay.get('floor', 150000)) // 1000}k, "
              f"+10 at ${int(pay.get('target', 150000)) // 1000}k")
        print(f"\nrole attributes are scaled by {weights['role_weight']}/10 (each counts on its own, "
              f"weight x measure)\nthe model's holistic read counts {weights['overall_fit']}/10")
        for family in ("role", "practical"):
            print(f"\n[scoring.{family}]")
            for name, w in sorted(weights[family].items(), key=lambda kv: -kv[1]):
                share = 100.0 * w / max(1, sum(weights[family].values()))
                bar = "#" * int(round(w))
                why = rel.ROLE_DIMS[name][1] if family == "role" else ""
                print(f"  {name:<18}{w:>3} {bar:<10} {share:>4.0f}% of this family   {why[:58]}")
        cache = judgemod.load(p.data_dir)
        p.store.refresh()
        live = [jid for jid, r in p.store.jobs["jobs"].items()
                if r.get("active", True) and not r.get("duplicate_of")]
        print(f"\n{sum(1 for jid in live if jid in cache)} of {len(live)} active postings rated by "
              f"the model ({len(cache)} ratings on file, including retired and duplicate postings)")
        curve, sha = rel.load_curve(p.data_dir)
        if curve:
            print(f"grade curve fitted for weights {sha}: A+ at {curve[0]:.0f}, B- at {curve[5]:.0f}, D at {curve[-1]:.0f}")
        else:
            print("no grade curve yet — run retune once postings are rated")
        return 0

    if args.cmd == "discover":
        import collections
        from jobhunt.fetch import sources as srcmod
        srcmod.REQUESTS_PER_SECOND = float((p.search.get("discovery") or {}).get("requests_per_second", 4))
        from jobhunt.classify.filters import Matcher
        from jobhunt.fetch import discover as disc
        dcfg = (p.search.get("discovery") or {})
        if args.status:
            reg = disc.load(p.data_dir)
            if not reg:
                print("nothing harvested yet — run `jobhunt.py discover --harvest`")
                return 0
            by = collections.Counter(r.get("state") for r in reg.values())
            print(f"{len(reg)} companies known for {p.name}")
            for st in disc.STATES:
                if by[st]:
                    print(f"  {st:9} {by[st]:>5}")
            ready = sum(1 for r in reg.values() if disc.due(r))
            print(f"\n{ready} due for a probe. Best unprobed, by how many relevant titles they posted:")
            top = sorted((r for r in reg.values() if r.get("state") == "new"),
                         key=lambda r: -int(r.get("relevant_titles") or 0))[:12]
            for r in top:
                print(f"  {int(r.get('relevant_titles') or 0):>4}  {r['name'][:38]:40} seen via {','.join(r.get('seen_in') or [])}")
            return 0

        if args.reassess:
            reg = disc.load(p.data_dir)
            st = disc.reassess(reg, Matcher(p.search), data_dir=p.data_dir)
            n = disc.write_discovered(root, reg, p.board_tag)
            print(f"re-checked {st['checked']} discovered boards, demoted {st['demoted']}; {n} remain")
            return 0

        print(f"harvesting company names for {p.name}...")
        reg, added = disc.harvest_all(root, p)
        print(f"{len(reg)} companies known, {added} new")
        if args.harvest:
            print("run `jobhunt.py discover` without --harvest to probe them")
            return 0

        stats = disc.probe_due(reg, Matcher(p.search), limit=args.limit,
                               recheck_days=args.recheck_days or int(dcfg.get("recheck_days", 90)),
                               data_dir=p.data_dir)
        if args.workday:
            wd = disc.workday_pass(reg, Matcher(p.search), limit=args.limit, data_dir=p.data_dir)
            print(f"workday sweep: {wd['swept']} companies, {wd['found']} boards found")
        cut = disc.apply_cap(reg, int(dcfg.get("max_daily_boards", 600)))
        disc.save(p.data_dir, reg)
        n = disc.write_discovered(root, reg, p.board_tag)
        auditmod.append(p.data_dir, "discovered", probed=stats["probed"], promoted=stats["promoted"],
                        parked=stats["parked"] + cut, dead=stats["dead"],
                        requests=stats["requests"], boards_now=n)
        print(f"\nprobed {stats['probed']} companies in {stats['requests']} requests: "
              f"{stats['promoted']} promoted, {stats['parked']} parked, {stats['dead']} dead")
        print(f"{n} discovered boards now in {disc.DISCOVERED_FILE}; they join the next scan")
        return 0

    if args.cmd == "audit":
        rows = auditmod.read(p.data_dir, kind=args.kind, limit=args.limit)
        if not rows:
            print(f"nothing recorded yet for {p.name} ({auditmod.path_for(p.data_dir)})")
            return 0
        print(f"{len(rows)} entries for {p.name}   {auditmod.path_for(p.data_dir)}\n")
        for r in rows:
            when = str(r.get("at", ""))[:16].replace("T", " ")
            kind = r.get("kind", "?")
            print(f"{when}  {kind:<16}{_audit_line(r)}")
            if args.full:
                print(f"{'':18}{json.dumps({k: v for k, v in r.items() if k not in ('at', 'kind')})[:400]}")
        return 0

    if args.cmd in ("explain", "retune"):
        from jobhunt.classify import judge as judgemod
        from jobhunt.classify import relevance as rel
        weights, notes = rel.load_weights(p.search)
        for n in notes:
            print(f"  config: {n}")
        store = p.store
        store.refresh()
        # Read the ratings cache rather than trusting what is attached: the cache is authoritative and
        # a judging run checkpoints into it, so this works while one is still going.
        judgemod.attach(store.jobs["jobs"], judgemod.load(p.data_dir))
        rows = [r for r in store.jobs["jobs"].values() if r.get("active", True) and not r.get("duplicate_of")]
        scored = []
        for r in rows:
            total, parts = rel.score(r.get("role"), rel.practical_measures(r, p.search), weights,
                                     overall=rel.overall_of(r))
            scored.append((total, parts, r))
        curve = rel.fit_curve([t for t, _p, _r in scored if t is not None])

        if args.cmd == "retune":
            graded = sorted([x for x in scored if x[0] is not None], key=lambda x: -x[0])
            print(f"{len(graded)} postings graded, {len(scored) - len(graded)} not yet read by the model")
            if not graded:
                return 0
            counts: dict = {}
            for total, _parts, _r in graded:
                counts[rel.grade(total, curve)] = counts.get(rel.grade(total, curve), 0) + 1
            print("  " + "  ".join(f"{g} {counts.get(g, 0)}" for g in rel.GRADES if counts.get(g)))
            print()
            for total, _parts, r in graded[:args.top]:
                st = store.get_state(r["id"], refresh=False).get("status", "new")
                print(f"  {rel.grade(total, curve):>2}  {total:>7.0f}  {st:<8} {r['company'][:22]:<22} {r['title'][:46]}")
            # The only labels that exist are what the person actually did with a posting. If a weight
            # change buries their own saves, it made the ranking worse however good the reasoning sounded.
            import statistics as _st
            rank = {r["id"]: i for i, (_t, _p, r) in enumerate(graded, 1)}
            picks = [r["id"] for _t, _p, r in graded
                     if store.get_state(r["id"], refresh=False).get("status") in ("saved", "applied")]
            if picks:
                rs = sorted(rank[j] for j in picks)
                print(f"\nWHERE YOUR OWN {len(picks)} CHOICES RANK")
                print(f"  median {_st.median(rs):.0f} of {len(graded)}   "
                      f"top-50 {sum(1 for r in rs if r <= 50)}/{len(rs)}   worst {rs[-1]}")
                confounded = sum(1 for j in picks if (store.get_state(j, refresh=False).get("decided_under")
                                                      or "") != rel.weights_sha(weights))
                if confounded:
                    print(f"  {confounded} of them were chosen from a list sorted by different weights,")
                    print("  so this ranking gets unearned credit for having shown them to you at all.")
            return 0

        match = next((x for x in scored if x[2]["id"] == args.job_id), None)
        if match is None:
            print("unknown job id (or it is inactive)")
            return 2
        total, parts, r = match
        print(f"{r['company']} — {r['title']}")
        print(f"{r.get('location') or 'location unknown'}   {r.get('url') or ''}")
        if r.get("role_why"):
            print(f"\nthe model's read: {r['role_why']}")
        print(f"\n{'attribute':<20}{'measure':>9}{'×':>8}{'points':>9}   evidence   (× is the multiplier applied)")
        for part in parts:
            ev = part["why"] or ("absent from the posting" if part["m"] == 0 else "")
            print(f"  {part['dim']:<18}{part['m']:>9}{part.get('w_eff', part['w']):>8}{part['points']:>9}   {ev}")
        if total is None:
            print("\nno relevance yet: the model has not read this posting")
        else:
            print(f"\n{'relevance':<20}{'':>17}{total:>9}   grade {rel.grade(total, curve)}")
        return 0

    if args.cmd == "list":
        from jobhunt.classify import relevance as rel
        rows = store.merged()
        floor = args.min_grade or rel.min_grade(p.search)
        n = 0
        for r in rows:
            if args.status and r["state"]["status"] != args.status:
                continue
            if not rel.at_least(r.get("grade"), floor):
                continue
            print(f"{(r.get('grade') or '—'):>3}  {r['state']['status']:<12} {r['company'][:22]:<22} {r['title'][:55]:<55} "
                  f"{(r.get('location_fit') or ''):<18} {r['id']}")
            n += 1
            if n >= args.limit:
                break
        print(f"({n} shown; {len(rows)} total; profile {p.name})")
        return 0

    if args.cmd == "export":
        rows = store.merged(include_inactive=True)
        cols = ["grade", "relevance", "overall_fit", "status", "skip_reason", "done_reason", "company", "title", "location",
                "location_fit", "travel_level", "salary_min", "salary_max", "clearance", "posted_at", "first_seen",
                "active", "url", "apply_url", "id", "notes"]
        with open(args.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get("grade"), r.get("relevance"), (r.get("role") or {}).get("overall_fit"),
                            r["state"]["status"], r["state"].get("skip_reason", ""), r["state"].get("done_reason", ""),
                            r["company"], r["title"],
                            r["location"], r.get("location_fit"), (r.get("travel") or {}).get("level"),
                            (r.get("salary") or {}).get("min"), (r.get("salary") or {}).get("max"),
                            (r.get("clearance") or {}).get("level"), r.get("posted_at"), r.get("first_seen"),
                            r.get("active"), r["url"], r["apply_url"], r["id"], r["state"].get("notes", "")])
        print(f"wrote {len(rows)} rows to {args.path}")
        return 0

    if args.cmd == "status":
        if not store.get(args.job_id):
            print("unknown job id")
            return 2
        store.set_state(args.job_id, status=args.status, skip_reason=(args.reason or "") if args.status == "skip" else None,
                        done_reason=(args.done_reason or "") if args.status == "done" else None)
        relearn(p)
        print("ok")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
