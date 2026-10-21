"""Which browser a run gets.

A persistent real-Chrome profile under profiles/<name>/data/browser/ keeps cookies, portal logins and
browsing history between runs, which is what a person's browser looks like to reCAPTCHA scoring. No
stealth patches: defeating a control the employer chose is a terms problem (docs/apply-design.md).

Chrome allows one process per profile directory, and the dashboard runs several apply windows at
once, so the directory holds numbered slots and each run takes the first free one under a file lock
held for the life of the process.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Callable, Optional

VIEWPORT = {"width": 1280, "height": 950}
MAX_SLOTS = 12


def _take_slot(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    for n in range(MAX_SLOTS):
        lock = open(root / f"slot-{n}.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            continue
        _clear_stale(root / f"slot-{n}")
        return root / f"slot-{n}", lock
    raise RuntimeError(f"all {MAX_SLOTS} browser profile slots under {root} are in use")


def _clear_stale(slot: Path) -> None:
    """A run that was killed leaves its Chrome running on the slot, and a new Chrome on the same profile
    hands off to it and never answers Playwright: the run hangs (seen 26 Sept). We hold the slot's lock, so
    no live run owns it: any Chrome still using it is an orphan."""
    import signal
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", f"--user-data-dir={slot}"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return
    for pid in out.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (slot / name).unlink()
        except OSError:
            pass


def open_browser(pw, headless: bool, profile_dir: Optional[Path] = None, channel: str = "chrome",
                 log: Callable[[str], None] = print):
    """Returns (context, close). With profile_dir, a persistent context in a free slot; without it, a
    throwaway one (tests). A missing Chrome falls back to Playwright's bundled Chromium, and says so."""
    lock = None
    try:
        if profile_dir is not None:
            slot, lock = _take_slot(Path(profile_dir))
            try:
                ctx = pw.chromium.launch_persistent_context(str(slot), channel=channel or None, headless=headless,
                                                            slow_mo=40, viewport=VIEWPORT)
            except Exception as e:  # noqa: BLE001
                if not channel:
                    raise
                log(f"could not start {channel} ({type(e).__name__}); using the bundled Chromium")
                ctx = pw.chromium.launch_persistent_context(str(slot), headless=headless, slow_mo=40, viewport=VIEWPORT)
            log(f"browser profile: {slot}")
            browser = None
        else:
            browser = pw.chromium.launch(headless=headless, slow_mo=40)
            ctx = browser.new_context(viewport=VIEWPORT)
    except Exception:
        if lock is not None:
            lock.close()
        raise

    def close() -> None:
        for fn in (ctx.close, browser.close if browser else None):
            try:
                if fn:
                    fn()
            except Exception:  # noqa: BLE001
                pass
        if lock is not None:
            lock.close()

    return ctx, close


def first_page(ctx):
    """A persistent context opens with a blank tab; reuse it rather than leaving it behind."""
    return ctx.pages[0] if ctx.pages else ctx.new_page()
