"""The verify stage: typed is not the same as done. Read every filled field back and compare.

A custom dropdown can swallow a click, an autocomplete can replace "Columbus, OH" with nothing, a
conditional section can re-render and wipe a value. None of those raise. The only way to know a field
holds what we meant is to read the page again, the same way the read stage does.
"""
from __future__ import annotations

import re
from typing import Any

from .schema import Field, norm


def matches(kind: str, want: Any, got: Any) -> bool:
    if kind == "checkbox":
        return got is True
    if kind == "checkgroup":
        have = {norm(x) for x in (got or [])}
        return bool(want) and all(norm(w) in have for w in want)
    if kind == "file":
        return bool(got) and norm(str(want)) in norm(str(got))
    g, w = norm(str(got or "")), norm(str(want or ""))
    if not g:
        return False
    dg, dw = re.sub(r"\D", "", g), re.sub(r"\D", "", w)
    if len(dw) >= 7 and dg == dw:     # a phone field that formats as you type: (555) 010-0100
        return True
    if kind == "combobox":
        # autocompletes display more than the option label ("Columbus, Ohio, United States")
        return g == w or w in g or g in w
    return g == w


def check(now: list[Field], expected: dict[str, tuple]) -> list[dict]:
    """expected: Field.ident -> (label, kind, value we set). Returns the fields that do not hold it."""
    by_ident = {f.ident: f for f in now}
    out = []
    for ident, (label, kind, want) in expected.items():
        f = by_ident.get(ident)
        if f is None:
            out.append({"label": label, "expected": want, "got": None, "why": "field is gone from the page"})
        elif not matches(kind, want, f.value):
            out.append({"label": label, "expected": want, "got": f.value, "why": "value did not stick"})
    return out


INVALID_JS = r"""
() => Array.from(document.querySelectorAll('[aria-invalid="true"]'))
  .filter(el => el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  .map(el => {
    const l = el.id ? document.querySelector('label[for="' + el.id.replace(/"/g, '\\"') + '"]') : null;
    return ((l && l.innerText) || el.getAttribute('aria-label') || el.name || el.id || '').replace(/\s+/g, ' ').trim();
  })
"""


def page_text(page) -> str:
    out = []
    for frame in page.frames:
        try:
            out.append(frame.locator("body").inner_text(timeout=2000))
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(out)


def settle_files(mismatches: list[dict], kinds: dict[str, str], text: str) -> list[dict]:
    """Some forms (Lever) swap the file input for a "resume.pdf ✓" chip once the upload is parsed, so
    the field is gone. The file name shown on the page is the read-back."""
    low = text.lower()
    return [m for m in mismatches if not (kinds.get(m["label"]) == "file" and m["got"] is None
                                          and str(m["expected"]).lower() in low)]


def invalid_fields(page) -> list[str]:
    """Labels of fields the page itself marks invalid (aria-invalid), across frames."""
    out: list[str] = []
    for frame in page.frames:
        try:
            out.extend(x for x in frame.evaluate(INVALID_JS) if x)
        except Exception:  # noqa: BLE001
            continue
    return list(dict.fromkeys(out))


def window_edits(now: list[Field], expected: dict[str, tuple], left_empty: set[str]) -> dict:
    """What you changed in the window: values that differ from what we filled, and fields we left
    empty that you filled. This is the miss the tool did not know about."""
    changed = [m for m in check(now, expected) if m["got"] not in (None, "", [], False)]
    by_ident = {f.ident: f for f in now}
    filled_by_you = [{"label": by_ident[i].label, "value": by_ident[i].value, "ident": i}
                     for i in left_empty if i in by_ident and by_ident[i].has_value() and by_ident[i].kind != "file"]
    return {"changed": changed, "filled_by_you": filled_by_you}
