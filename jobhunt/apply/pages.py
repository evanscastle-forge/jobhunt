"""Multi-page forms: fill a page, check it, advance, repeat, and stop before anything that submits.

Used by the Workday playbook and by generic multi-step forms ("Step 1 / 3 ... Continue"). Advancing is
itself verified: the page must change. When it does not, the page's own error messages are read and the
page filled once more with them in mind (the rule: let the page say what is wrong, then fix that).
Nothing matching SUBMIT is ever clicked.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .read import read_form

NEXT = re.compile(r"^\s*(next|continue|save and continue|save & continue|next step|proceed|continue to .+)\s*[›>→]?\s*$", re.I)
SUBMIT = re.compile(r"submit|send( my)? application|finish|complete( my)? application|^\s*apply\s*$|confirm", re.I)

ERRORS_JS = r"""() => {
  const vis = e => e.offsetWidth || e.offsetHeight || e.getClientRects().length;
  const out = [];
  document.querySelectorAll('[role=alert], [aria-live=assertive], [data-automation-id=errorMessage], [data-automation-id=errorBanner], .error, .errors, [class*="error-message"], [class*="errorMessage"], [class*="field-error"]')
    .forEach(e => { if (vis(e)) { const t = (e.innerText || '').replace(/\s+/g, ' ').trim(); if (t && t.length < 300) out.push(t); } });
  return Array.from(new Set(out)).slice(0, 20);
}"""


def page_errors(page) -> list[str]:
    out: list[str] = []
    for fr in page.frames:
        try:
            out += fr.evaluate(ERRORS_JS)
        except Exception:  # noqa: BLE001
            continue
    return list(dict.fromkeys(out))


def _fingerprint(page) -> str:
    try:
        fields = read_form(page)
    except Exception:  # noqa: BLE001
        fields = []
    # visible fields only: single-page apps keep every step in the DOM and just show one of them
    return page.url + "|" + "|".join(sorted(f.label for f in fields if f.visible))[:4000]


def next_button(page, selector: Optional[str] = None):
    """The control that advances without submitting, or None."""
    if selector:
        loc = page.locator(selector).first
        try:
            if loc.count() and loc.is_visible() and not SUBMIT.search(loc.inner_text(timeout=1000) or ""):
                return loc
        except Exception:  # noqa: BLE001
            pass
        return None
    for role in ("button", "link"):
        try:
            cands = page.get_by_role(role, name=NEXT)
            for i in range(min(cands.count(), 4)):
                c = cands.nth(i)
                if c.is_visible() and not SUBMIT.search(c.inner_text(timeout=1000) or ""):
                    return c
        except Exception:  # noqa: BLE001
            continue
    return None


def has_submit(page) -> bool:
    try:
        return bool(page.get_by_role("button", name=SUBMIT).count())
    except Exception:  # noqa: BLE001
        return False


def advance(page, selector: Optional[str] = None, wait_ms: int = 2500) -> tuple[bool, list[str], bool]:
    """(moved, the page's error messages, whether there was a next control at all)."""
    btn = next_button(page, selector)
    if btn is None:
        return False, [], False
    before = _fingerprint(page)
    try:
        btn.click(timeout=4000, force=True)       # force: Workday lays a click_filter over its buttons
    except Exception as e:  # noqa: BLE001
        return False, [f"could not click next: {type(e).__name__}"], True
    page.wait_for_timeout(wait_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:  # noqa: BLE001
        pass
    return _fingerprint(page) != before, page_errors(page), True


def walk(page, filler, log: Callable[[str], None], is_review: Callable[[object], bool], next_selector: Optional[str] = None,
         max_pages: int = 10, on_page: Optional[Callable[[object], None]] = None) -> tuple[str, str]:
    """Fill page after page. Returns (outcome, note): review | end | blocked | stuck."""
    for n in range(max_pages):
        if is_review(page):
            return "review", f"reached the review page after {n} page(s)"
        if on_page is not None:
            on_page(page)
        filler.fill(page)
        filler.page_verify(page)
        moved, errors, had_next = advance(page, next_selector)
        if not had_next:
            return "end", "no further page: the next step is Submit, which is yours"
        if not moved:
            log(f"page did not advance; the page says: {errors[:5]}")
            filler.required_from_errors(page, errors)
            filler.fill(page)
            filler.page_verify(page)
            moved, errors2, _ = advance(page, next_selector)
            if not moved:
                for e in (errors2 or errors or ["the page would not advance and gave no reason"])[:8]:
                    filler.block("page", e)
                return "stuck", "; ".join((errors2 or errors)[:3]) or "would not advance"
        log(f"advanced to page {n + 2}")
    return "stuck", f"more than {max_pages} pages"
