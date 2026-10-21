"""Replay: re-run the read and answer stages against every real form ever captured, offline, and diff the
decisions against what was recorded. A rule change is checked against all of them in seconds, without a
live posting, a network or a model call.

Each run folder holds `form.html` (and `form-frameN.html` for embedded frames) as the page stood after
filling, and `fields.json`, the decision made for every field. The page's own scripts are disabled on
replay: the captured DOM is already rendered, and nothing may change it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .answer import Answerer
from .read import read_form

TYPED = {"filled", "typed_for_review", "autofill_ok", "corrected_autofill"}


def _frames(run_dir: Path) -> list[tuple[int, Path]]:
    out = [(0, run_dir / "form.html")] if (run_dir / "form.html").exists() else []
    for p in sorted(run_dir.glob("form-frame*.html")):
        m = re.search(r"frame(\d+)", p.name)
        if m:
            out.append((int(m.group(1)), p))
    return out


def replay_run(page, run_dir: Path, profile: dict, job: dict, bank=None) -> Optional[dict]:
    """Decisions now vs then for one captured form. None when the run captured no form."""
    frames = _frames(run_dir)
    rec_path = run_dir / "fields.json"
    if not frames or not rec_path.exists():
        return None
    recorded = {(r["label"], r["kind"]): r for r in json.loads(rec_path.read_text(encoding="utf-8"))}
    ans = Answerer(profile, job, bank=bank)
    diffs, same = [], 0
    for idx, path in frames:
        page.set_content(path.read_text(encoding="utf-8"))
        for f in read_form(page):
            f.frame = idx
            old = recorded.get((f.label, f.kind))
            if old is None or old["action"] in ("hidden", "prefilled") or f.kind == "file":
                continue      # uploads are checked live (a captured DOM holds no file)
            a = ans.answer(f)
            was_typed = old["action"] in TYPED and old.get("tier") == "certain"
            now_typed = a.certain
            old_val = old.get("answer") if not isinstance(old.get("answer"), str) or not old["answer"].startswith("__file__") else "file"
            new_val = a.value if not isinstance(a.value, str) or not a.value.startswith("__file__") else "file"
            value_changed = f.kind != "file" and now_typed and str(old_val) != str(new_val)   # uploads: tier only
            if was_typed != now_typed or value_changed or (old.get("tier") != a.tier and not a.skip):
                diffs.append({"label": f.label, "kind": f.kind, "then": {"action": old["action"], "tier": old.get("tier"),
                              "answer": old_val}, "now": {"tier": a.tier, "answer": new_val, "source": a.source, "skip": a.skip}})
            else:
                same += 1
    return {"run": run_dir.name, "same": same, "diffs": diffs}


def replay(run_dirs: list[Path], profile: dict, jobs: dict, bank=None) -> list[dict]:
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(java_script_enabled=False).new_page()
        for d in run_dirs:
            meta = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").exists() else {}
            job = jobs.get(meta.get("job_id")) or {"company": meta.get("company"), "title": meta.get("title")}
            try:
                got = replay_run(page, d, profile, job, bank)
            except Exception as e:  # noqa: BLE001
                got = {"run": d.name, "same": 0, "diffs": [], "error": f"{type(e).__name__}: {e}"}
            if got is not None:
                out.append(got)
        browser.close()
    return out


def render(results: list[dict], limit: int = 40) -> str:
    total_same = sum(r["same"] for r in results)
    total_diff = sum(len(r["diffs"]) for r in results)
    lines = [f"replayed {len(results)} captured form(s): {total_same} decision(s) unchanged, {total_diff} changed"]
    shown = 0
    for r in results:
        if r.get("error"):
            lines.append(f"  ! {r['run']}: {r['error']}")
        for d in r["diffs"]:
            if shown >= limit:
                break
            lines.append(f"  {r['run'][16:56]:40} {d['label'][:50]!r}: then {d['then']['tier']}/{str(d['then']['answer'])[:30]!r}"
                         f" -> now {d['now']['tier']}/{str(d['now']['answer'])[:30]!r} ({d['now']['source']})")
            shown += 1
    if total_diff > shown:
        lines.append(f"  ... {total_diff - shown} more")
    return "\n".join(lines)
