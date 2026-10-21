"""Reset: clear what is in play between testing and real use, never personal data.

Clears: postings you never decided on, the Saved tab (decisions archived in place, not deleted), apply
runs (abandoned, folders moved to an archive), the aggregator resolution cache, and the shared raw
posting cache. Never touches: notes/ (about, voice, answers), profile.toml, the resume, the vault,
skipped / applied / done decisions, ratings, the grade curve, the audit log, widget recipes, browser
profiles, or config.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from . import audit
from .apply import runs as runsmod
from .jsonfile import now_iso

CLEARS = ["postings you never decided on (the next scan brings back the live ones)",
          "the Saved tab (the decisions are archived, so learning and retune still use them)",
          "apply runs (open ones abandoned, folders moved to data/archive/)",
          "the cache of where aggregator listings lead",
          "the raw posting cache (the next scan refetches)"]
KEEPS = ["about.md, voice.md, the answer bank, profile.toml, your resume (master, terms and variants) and the vault",
         "skipped, applied and done decisions",
         "ratings, the grade curve, the audit log, learned preferences",
         "widget recipes, browser logins, config"]


def reset(profile, root: Path) -> dict:
    stamp = now_iso()
    folder = profile.data_dir / "archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"archive": str(folder)}
    out.update(profile.store.reset(stamp))
    out.update(runsmod.archive_all(profile.data_dir, folder / "runs"))
    logs = profile.data_dir / "apply_logs"
    if logs.exists():
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(logs), str(folder / "apply_logs"))
    resolved = Path(root) / "data" / "apply" / "resolved.json"
    if resolved.exists():
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(resolved), str(folder / "resolved.json"))
    raw = Path(root) / "data" / "postings.json"
    out["raw_cache_cleared"] = raw.exists()
    raw.unlink(missing_ok=True)
    audit.append(profile.data_dir, "reset", **{k: v for k, v in out.items() if k != "archive"}, archive=str(folder))
    return out
