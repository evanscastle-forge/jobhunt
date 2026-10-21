"""The fill stage's hands: one adapter per kind of control. No decisions are made here.

`set_value(page, field, value)` puts an already-decided value into one Field and returns what it
selected (the option text actually clicked, which can differ in case or wording from the answer) or
None when it could not. verify.py then reads the page back to confirm it stuck: an adapter's own report
of success is not trusted on its own.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .schema import CHECK, FILE_PREFIX, Field, norm, pick_option

_OPTION_SELS = ["[role=option]", "[data-automation-id=promptOption]", "[class*='__option']", "[role=listbox] li", "[id*='-option-']",
                "[class*='dropdown-location']", ".dropdown-results > *", ".pac-item"]


def css_escape(s: str) -> str:
    return re.sub(r"([^\w-])", r"\\\1", s)


def frame_of(page, f: Field):
    frames = page.frames
    return frames[f.frame] if 0 <= f.frame < len(frames) else page.main_frame


def locator(page, f: Field):
    r, fr = f.raw, frame_of(page, f)
    if r.get("jh") and r.get("type") != "radio":
        return fr.locator(f'[data-jh="{r["jh"]}"]')
    if r.get("id"):
        return fr.locator(f"#{css_escape(r['id'])}")
    if r.get("name") and r.get("type") != "radio":
        return fr.locator(f"[name=\"{r['name']}\"]").first
    return fr.locator("input, select, textarea").nth(r.get("dom_index", 0))


def visible_options(page, f: Optional[Field] = None) -> list:
    """Visible menu options of an open custom dropdown: [(text, locator)]. The most specific selector
    goes first so a hidden phone-country list elsewhere on the page does not drown out the open menu."""
    root = frame_of(page, f) if f is not None else page
    for sel in _OPTION_SELS:
        out = []
        try:
            opts = root.locator(sel + ":visible")
            for i in range(min(opts.count(), 400)):
                o = opts.nth(i)
                try:
                    t = re.sub(r"\s+", " ", o.inner_text(timeout=500)).strip()
                except Exception:  # noqa: BLE001
                    continue
                if t and len(t) <= 120 and all(t != x for x, _ in out):
                    out.append((t, o))
        except Exception:  # noqa: BLE001
            continue
        if out:
            return out
    return []


def combobox_options(page, f: Field) -> list[str]:
    """Open a custom dropdown, read its option texts, close it again."""
    try:
        locator(page, f).click(timeout=3000)
        page.wait_for_timeout(350)
        texts = [t for t, _ in visible_options(page, f)]
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        return texts
    except Exception:  # noqa: BLE001
        return []


def _set_combobox(page, f: Field, value: str) -> Optional[str]:
    """Static menus (react-select yes/no lists) are opened and read; autocompletes (location pickers)
    are fed the head of the value ("Columbus") because they search on what was typed."""
    loc = locator(page, f)
    head = re.split(r"[,(]", value)[0].strip()
    try:
        loc.click(timeout=3000)
        page.wait_for_timeout(350)
        opts = visible_options(page, f)
        if not opts:                      # nothing until you type: an autocomplete, which searches on keystrokes
            try:
                loc.press_sequentially(head or value, delay=60, timeout=6000)
            except Exception:  # noqa: BLE001
                page.keyboard.type(head or value, delay=60)
            page.wait_for_timeout(1800)
            opts = visible_options(page, f)
        elif len(opts) > 12:              # long static list: type to filter
            try:
                loc.fill(value, timeout=2000)
            except Exception:  # noqa: BLE001
                page.keyboard.type(value)
            page.wait_for_timeout(600)
            opts = visible_options(page, f) or opts
        if not opts:
            page.keyboard.press("Escape")
            return None
        picked = best_suggestion([t for t, _ in opts], value)
        if picked:
            dict(opts)[picked].click(timeout=3000)
            return picked
        page.keyboard.press("Escape")
        return None
    except Exception:  # noqa: BLE001
        return None


def _set_checkgroup(page, f: Field, picks: list[str]) -> list[str]:
    done = []
    boxes = frame_of(page, f).locator("input[type=checkbox]")
    norm_opts = [norm(o) for o in f.options]
    for want in picks:
        try:
            idx = norm_opts.index(norm(want))
        except ValueError:
            continue
        jh = (f.raw.get("option_jh") or [None] * len(f.options))[idx]
        box = frame_of(page, f).locator(f'[data-jh="{jh}"]') if jh else boxes.nth(f.raw["option_indexes"][idx])
        try:
            box.check(force=True, timeout=3000)
        except Exception:  # noqa: BLE001
            try:  # custom-styled (hidden) checkbox: click it through the DOM
                box.evaluate("el => { if (!el.checked) el.click(); }")
            except Exception:  # noqa: BLE001
                continue
        done.append(f.options[idx])
    return done


def _set_radio(page, f: Field, option_text: str) -> Optional[str]:
    try:
        idx = [norm(o) for o in f.options].index(norm(option_text))
    except ValueError:
        return None
    fr = frame_of(page, f)
    radios = fr.locator(f"input[type=radio][name=\"{f.raw.get('name', '')}\"]")
    try:
        radios.nth(idx).check(force=True, timeout=3000)
        return f.options[idx]
    except Exception:  # noqa: BLE001
        try:   # custom-styled radios: click the label instead
            rid = radios.nth(idx).get_attribute("id", timeout=1000) or ""
            fr.locator(f"label[for=\"{css_escape(rid)}\"]").first.click(timeout=2000)
            return f.options[idx]
        except Exception:  # noqa: BLE001
            return None


def set_value(page, f: Field, value: Any) -> Any:
    """Put `value` into the field. Returns what was set (option text, list, "checked", a file name) or
    None. Raises only on Playwright errors the caller reports as "could not fill"."""
    loc = locator(page, f)
    if isinstance(value, str) and value.startswith(FILE_PREFIX):
        path = Path(value[len(FILE_PREFIX):]).expanduser()
        if not path.exists():
            return None
        loc.set_input_files(str(path))
        return path.name
    if value == CHECK or f.kind == "checkbox":
        if not f.value:
            try:
                loc.check(force=True, timeout=3000)
            except Exception:  # noqa: BLE001  custom-styled (hidden) checkbox: click it through the DOM
                loc.evaluate("el => { if (!el.checked) el.click(); }")
        return "checked"
    if f.kind == "select":
        loc.select_option(label=value, timeout=3000)
        return value
    if f.kind == "combobox":
        return _set_combobox(page, f, value)
    if f.kind == "radio":
        return _set_radio(page, f, value)
    if f.kind == "checkgroup":
        done = _set_checkgroup(page, f, value if isinstance(value, list) else [value])
        return done or None
    if re.search(r"\b(location|city)\b", f.label or "", re.I):
        picked = _type_and_pick(page, f, loc, value)
        if picked:
            return picked
    loc.fill(value, timeout=4000)
    return value


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def _variants(value: str) -> list[str]:
    """"Columbus, OH" is also "Columbus, Ohio": pickers spell the state out."""
    out = [value]
    m = re.search(r",\s*([A-Z]{2})\b", value)
    if m and m.group(1) in US_STATES:
        out.append(value[:m.start(1)] + US_STATES[m.group(1)] + value[m.end(1):])
    return out


def best_suggestion(texts: list[str], value: str) -> Optional[str]:
    """The suggestion for "Columbus, OH": the one matching the whole value ("Columbus, OH, USA" or
    "Columbus, Ohio, United States"), else the only one containing the city. Never the first
    "Columbus" in the list, which may be in Georgia."""
    for v in _variants(value):
        picked = pick_option(texts, v)
        if picked:
            return picked
    head = re.split(r"[,(]", value)[0].strip().lower()
    hits = [t for t in texts if head and head in t.lower()]
    return hits[0] if len(hits) == 1 else None


def _type_and_pick(page, f: Field, loc, value: str) -> Optional[str]:
    """Location pickers on plain inputs (Lever) search on keystrokes, not on a filled value, and throw
    the text away on blur unless a suggestion is chosen. Type the city, choose the matching suggestion."""
    head = re.split(r"[,(]", value)[0].strip()
    try:
        loc.fill("", timeout=2000)
        loc.press_sequentially(head, delay=60, timeout=6000)
        page.wait_for_timeout(1500)
        opts = visible_options(page, f)
        picked = best_suggestion([t for t, _ in opts], value)
        if not picked:
            return None
        dict(opts)[picked].click(timeout=3000)
        page.wait_for_timeout(300)
        return loc.input_value(timeout=1000) or picked
    except Exception:  # noqa: BLE001
        return None
