"""Workday: one playbook for every employer tenant, because Workday's markup carries stable
data-automation-id attributes (adventureButton, autofillWithResume, email, password, createAccountSubmitButton,
bottom-navigation-next-button, progressBarActiveStep ...).

    job page      Apply -> "Autofill with Resume" (the employer's own autofill goes first), else Apply Manually
    account       sign in with the vault's login for this tenant, or create one; a verification email is a
                  pause (verify_email) for you, then Resume carries on
    resume        upload it to the autofill step and let Workday parse it
    pages         My Information, My Experience, Application Questions, Voluntary Disclosures, Self Identify:
                  each filled, checked and advanced by pages.walk, whose values Workday parsed are reviewed
    review        stop. Never Submit.
"""
from __future__ import annotations

import re
from typing import Callable, Optional
from urllib.parse import urlparse

from ..pages import page_errors, walk
from ..read import read_form

NEXT = '[data-automation-id="bottom-navigation-next-button"], [data-automation-id="pageFooterNextButton"]'
VERIFY_RE = re.compile(r"verify your (email|account)|verification (email|link)|check your (email|inbox)|activate your account", re.I)


def aid(name: str) -> str:
    return f'[data-automation-id="{name}"]'


def tenant(url: str) -> str:
    """bah.wd1.myworkdayjobs.com/BAH_Jobs: one candidate account per employer site."""
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    return f"{u.netloc}/{parts[0]}" if parts else u.netloc


def _visible(page, sel: str) -> bool:
    try:
        loc = page.locator(sel).first
        return bool(loc.count()) and loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _click(page, sel: str, wait: int = 2500) -> bool:
    try:
        page.locator(sel).first.click(timeout=5000, force=True)   # force: a click_filter overlay sits on top
        page.wait_for_timeout(wait)
        return True
    except Exception:  # noqa: BLE001
        return False


def active_step(page) -> str:
    try:
        return page.locator(aid("progressBarActiveStep")).first.inner_text(timeout=1500)
    except Exception:  # noqa: BLE001
        return ""


def is_review(page) -> bool:
    return "review" in active_step(page).lower()


def run(page, filler, vault, email: str, resume: Optional[str], log: Callable[[str], None]) -> dict:
    """Returns {"outcome": review|end|blocked|stuck|pause, "reason": runs.REASONS key or "", "note": str}."""
    if _visible(page, aid("adventureButton")):
        _click(page, aid("adventureButton"))
    if _visible(page, aid("autofillWithResume")):
        _click(page, aid("autofillWithResume"), 3500)
        filler.report.autofill = {"how": "Workday: Autofill with Resume", "filled": 0, "kept": 0, "corrected": [], "unconfirmed": 0}
    elif _visible(page, aid("applyManually")):
        _click(page, aid("applyManually"), 3500)

    if _visible(page, aid("email")) and _visible(page, aid("password")):
        got = _account(page, vault, email, log)
        if got:
            return got

    if resume and _visible(page, 'input[type="file"]') and not _visible(page, aid("progressBarActiveStep") + ":has-text('My Information')"):
        try:
            page.locator('input[type="file"]').first.set_input_files(resume)
            page.wait_for_timeout(6000)             # Workday parses the resume into the later pages
            filler.report.uploaded.append("Resume (Workday autofill)")
            log("workday: resume uploaded for Workday's own autofill")
        except Exception as e:  # noqa: BLE001
            log(f"workday: could not upload the resume: {e}")

    def on_page(pg) -> None:
        """Values present when a page opens came from Workday's parse or the account, not from you: review them."""
        for f in read_form(pg):
            if f.has_value() and f.ident not in filler.expected and f.kind != "file":
                filler.autofilled.setdefault(f.ident, f.value)
        log(f"workday page: {active_step(pg).strip()[:60] or '?'}")

    outcome, note = walk(page, filler, log, is_review=is_review, next_selector=NEXT, on_page=on_page)
    return {"outcome": outcome, "reason": "", "note": note}


def _account(page, vault, email: str, log) -> Optional[dict]:
    """Sign in, or create the account. Returns a pause dict, or None to carry on."""
    if vault is None:
        return {"outcome": "pause", "reason": "vault",
                "note": "Workday needs an account per employer; run `uv run python jobhunt.py vault init` once, then Retry"}
    site = tenant(page.url)
    rec = vault.get(site)
    creating = rec is None
    if not creating and _visible(page, aid("verifyPassword")) and _visible(page, aid("signInLink")):
        _click(page, aid("signInLink"))
    if creating:
        rec = vault.create(site, email)
        log(f"workday: creating an account for {site} (password kept in the vault, never shown)")
    try:
        page.locator(aid("email")).first.fill(rec["email"], timeout=4000)
        page.locator(aid("password")).first.fill(rec["password"], timeout=4000)
        if creating and _visible(page, aid("verifyPassword")):
            page.locator(aid("verifyPassword")).first.fill(rec["password"], timeout=4000)
        if creating and _visible(page, aid("createAccountCheckbox")):
            page.locator(aid("createAccountCheckbox")).first.check(force=True, timeout=3000)
    except Exception as e:  # noqa: BLE001
        return {"outcome": "pause", "reason": "error", "note": f"workday account form: {type(e).__name__}: {e}"}
    submit = aid("createAccountSubmitButton") if creating else aid("signInSubmitButton")
    if not _click(page, submit, 5000):
        _click(page, aid("click_filter"), 5000)
    body = ""
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        pass
    if VERIFY_RE.search(body):
        vault.mark(site, verified=False)
        return {"outcome": "pause", "reason": "verify_email",
                "note": f"{site} sent a verification email to {rec['email']}: click its link, then Resume"}
    if _visible(page, aid("password")) and _visible(page, aid("email")):
        errs = page_errors(page)
        return {"outcome": "pause", "reason": "login",
                "note": f"could not {'create an account' if creating else 'sign in'} at {site}: {'; '.join(errs[:3]) or 'no reason shown'}"}
    vault.mark(site, verified=True)
    log(f"workday: signed in at {site}")
    return None
