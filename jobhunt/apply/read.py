"""The read stage: a page -> FormSchema (a list of schema.Field), across every frame.

Fields are found by label and accessibility attributes, never by selectors written for one ATS: forms
have to label their fields for screen readers, which is why this generalises. Employers that embed
Greenhouse on their own career site put the form in an iframe, so every frame is read.

Nothing here imports Playwright; it is handed a page.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from .schema import Field

FIELD_SCAN_JS = r"""
() => {
  const textOf = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const esc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^\w-])/g, '\\$1');
  const optionLabel = r => {
    const l = r.id ? document.querySelector('label[for="' + esc(r.id) + '"]') : null;
    const w = l || r.closest('label');
    const t = w ? textOf(w) : '';
    return t || r.value || '';
  };
  // Walk up to 5 ancestors; at each level take the nearest label-ish element that precedes the
  // control in document order and is not an option label (a <label> wrapping/for another input).
  const precedingLabel = (el, isRadio) => {
    const isOptionLabel = n => n.tagName === 'LABEL' && (n.querySelector('input') ||
      (n.htmlFor && document.getElementById(n.htmlFor) && document.getElementById(n.htmlFor) !== el));
    let node = el.parentElement;
    for (let d = 0; node && d < 5 && node !== document.body; d++, node = node.parentElement) {
      const cands = Array.from(node.querySelectorAll('.application-label, legend, label, [class*="label"], [class*="Label"], h3, h4, h5, strong, p, span'))
        .filter(n => !n.contains(el) && !isOptionLabel(n) && /[A-Za-z0-9]/.test(textOf(n)) && textOf(n).length < 300 &&
                     (n.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (cands.length) return textOf(cands[cands.length - 1]);
      if (isRadio && d >= 1 && node.matches('fieldset, .application-question, [role="radiogroup"], [role="group"]')) break;
    }
    return '';
  };
  const groupLabel = el => precedingLabel(el, true) || (el.name || '');
  const labelFor = el => {
    if (el.id) { const l = document.querySelector('label[for="' + esc(el.id) + '"]'); if (l) return textOf(l); }
    const al = el.getAttribute('aria-labelledby');
    if (al) { const t = al.split(/\s+/).map(i => textOf(document.getElementById(i))).filter(Boolean).join(' '); if (t) return t; }
    const ar = el.getAttribute('aria-label'); if (ar) return ar.trim();
    const wrap = el.closest('label'); if (wrap) return textOf(wrap);
    const pl = precedingLabel(el, false); if (pl) return pl;
    return (el.placeholder || el.name || '').trim();
  };
  // What the control holds now, as a person would read it: the selected option's text, the checked
  // radio's label, a custom dropdown's displayed value, an uploaded file's name.
  const readValue = (el, type) => {
    if (type === 'radio') { const r = Array.from(document.querySelectorAll('input[type=radio][name="' + esc(el.name) + '"]')).find(x => x.checked); return r ? optionLabel(r) : ''; }
    if (type === 'checkbox') return !!el.checked;
    if (type === 'file') return el.files && el.files.length ? el.files[0].name : '';
    if (el.tagName === 'BUTTON') { const tx = (el.innerText || el.value || '').replace(/\s+/g, ' ').trim(); return /^(select one|select|choose one|--)$/i.test(tx) ? '' : tx; }
    if (el.tagName === 'SELECT') { const o = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null; return o && o.value !== '' ? o.text.trim() : ''; }
    if (el.value) return el.value;
    if ((el.getAttribute('role') || '') === 'combobox' || /react-select/.test(el.id || '')) {
      let node = el.parentElement;
      for (let d = 0; node && d < 5; d++, node = node.parentElement) {
        const sv = node.querySelector('[class*="singleValue"], [class*="single-value"], [class*="multiValue"], [class*="multi-value"]');
        if (sv) return Array.from(node.querySelectorAll('[class*="singleValue"], [class*="single-value"], [class*="multiValue__label"], [class*="multi-value__label"]')).map(textOf).filter(Boolean).join(', ') || textOf(sv);
      }
    }
    return '';
  };
  // Workday (and other design systems) render dropdowns as a button that opens a listbox
  const controls = Array.from(document.querySelectorAll('input, select, textarea, button[aria-haspopup="listbox"]'));
  // The text around a question that is not its label: definitions of options ("A: a U.S. person..."),
  // instructions, the range a salary question refers to. The agent needs it; the label alone can be "A, B or C?".
  const helpOf = (el, label, options) => {
    const c = el.closest('fieldset, .application-question, [class*="question"], [class*="field-wrapper"], [class*="form-field"], [data-automation-id^="formField"]');
    if (!c) return '';
    let tx = textOf(c);
    [label, ...(options || []).filter(o => o.length > 3)].forEach(s => { if (s) tx = tx.split(s).join(' '); });
    tx = tx.replace(/\s+/g, ' ').trim();
    return tx.length >= 20 ? tx.slice(0, 700) : '';
  };
  // A stable handle on each control, so the fill stage finds exactly the element that was read. Position
  // (nth) is not stable: Playwright's selector engine counts elements the page's own query does not.
  const tagEl = el => { if (!el.dataset.jh) { window.__jhSeq = (window.__jhSeq || 0) + 1; el.dataset.jh = String(window.__jhSeq); } return el.dataset.jh; };
  const isVisible = el => { const st = window.getComputedStyle(el); return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && st.visibility !== 'hidden'; };
  // Checkbox groups ("languages you speak", "citizenship status", ...): checkboxes that share a name,
  // or sit together in a container holding only checkboxes. Each group becomes ONE field with options.
  const cbs = controls.filter(el => (el.type || '').toLowerCase() === 'checkbox');
  const cbIndex = new Map(cbs.map((el, i) => [el, i]));
  const groupOf = new Map(); let gseq = 0; const groups = new Map();
  const containerKey = el => {
    let node = el.parentElement;
    for (let d = 0; node && d < 5 && node !== document.body && node.tagName !== 'FORM'; d++, node = node.parentElement) {
      const n = node.querySelectorAll('input[type=checkbox]').length;
      const others = node.querySelectorAll('input:not([type=checkbox]):not([type=hidden]), select, textarea').length;
      if (n > 1 && others === 0) { if (!node.__gk) node.__gk = 'c' + (++gseq); return node.__gk; }
      if (others > 0) return null;
    }
    return null;
  };
  cbs.forEach(el => {
    const byName = el.name && cbs.filter(x => x.name === el.name).length > 1 ? 'n:' + el.name : null;
    const key = byName || containerKey(el);
    if (key) { groupOf.set(el, key); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(el); }
  });
  const out = []; const seenRadio = new Set(); const emittedGroups = new Set();
  controls.forEach((el, i) => {
    const type = el.tagName === 'BUTTON' ? 'listbox' : (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) return;
    // a custom dropdown's hidden stand-in for form validation (react-select's requiredInput) shadows the real control
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0) return;
    // honeypots are for bots: filling one flags the application (Workday's beecatcher)
    if (/beecatcher|honeypot|hp[_-]?field|leave.?(this)?.?blank/i.test((el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('data-automation-id') || ''))) return;
    const visible = (type === 'file') || isVisible(el);
    if (type === 'radio') { if (!el.name || seenRadio.has(el.name)) return; seenRadio.add(el.name); }
    if (type === 'checkbox' && groupOf.has(el)) {
      const key = groupOf.get(el); if (emittedGroups.has(key)) return; emittedGroups.add(key);
      const members = groups.get(key);
      const glabel = precedingLabel(el, true) || el.name || '';
      out.push({ dom_index: i, jh: tagEl(el), option_jh: members.map(tagEl), tag: 'input', type: 'checkgroup', id: '', name: el.name || '', label: glabel,
                 options: members.map(optionLabel), option_indexes: members.map(m => cbIndex.get(m)), role: '',
                 visible: members.some(isVisible),
                 required: members.some(m => m.required || m.getAttribute('aria-required') === 'true') || /\*\s*$/.test(glabel),
                 value: members.filter(m => m.checked).map(optionLabel) });
      return;
    }
    let options = [];
    if (el.tagName === 'SELECT') options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
    if (type === 'radio') options = Array.from(document.querySelectorAll('input[type=radio][name="' + esc(el.name) + '"]')).map(optionLabel);
    const role = el.getAttribute('role') || '';
    const label = (type === 'radio') ? groupLabel(el) : labelFor(el);
    out.push({ dom_index: i, jh: tagEl(el), tag: el.tagName.toLowerCase(), type, id: el.id || '', name: el.name || '',
               label, options, role, visible, help: helpOf(el, label, options), placeholder: (el.placeholder || '').trim(),
               required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)),
               value: readValue(el, type) });
  });
  return out;
}
"""


def kind_of(raw: dict) -> str:
    if raw["tag"] == "select":
        return "select"
    if raw["tag"] == "textarea":
        return "textarea"
    t = raw["type"]
    if t in ("file", "checkbox", "checkgroup", "radio"):
        return t
    if t == "listbox":
        return "combobox"
    if raw.get("role") == "combobox" or "react-select" in (raw.get("id") or ""):
        return "combobox"
    return "text"


_GENERIC_FILE_LABEL = re.compile(r"^(attach|upload|choose|browse|drop|select|add)\b|^$", re.I)


def file_label(raw: dict, label: str) -> str:
    """Upload widgets often label the input with their button ("Attach file"). The input's id or name
    ("resume", "cover_letter") then says what it is for."""
    if not _GENERIC_FILE_LABEL.search((label or "").strip()):
        return label
    hint = f"{raw.get('id') or ''} {raw.get('name') or ''}".lower()
    if re.search(r"resume|\bcv\b|curriculum", hint):
        return "Resume/CV"
    if re.search(r"cover", hint):
        return "Cover Letter"
    return f"{label} ({hint.strip()})" if hint.strip() else label


def read_form(page) -> list[Field]:
    """Every field on the page, in every frame that answers. A frame that cannot be read (detached,
    navigating) is skipped rather than failing the read."""
    out: list[Field] = []
    for i, frame in enumerate(page.frames):
        try:
            raws = frame.evaluate(FIELD_SCAN_JS)
        except Exception:  # noqa: BLE001
            continue
        for r in raws:
            label = r.get("label") or r.get("name") or r.get("id") or ""
            if kind_of(r) == "file":
                label = file_label(r, label)
            out.append(Field(label=label, kind=kind_of(r), help=r.get("help") or "",
                             options=list(r.get("options") or []), required=bool(r.get("required")),
                             visible=bool(r.get("visible")), value=r.get("value"), frame=i, raw=r))
    return _unshare_labels(out)


def _unshare_labels(fields: list[Field]) -> list[Field]:
    """Text shared by several controls ("Step 1 / 3", "Contact information") is a section heading the label
    search walked up to, not a label: where each control has its own placeholder, that is its label."""
    from collections import Counter
    counts = Counter((f.frame, f.label) for f in fields if f.kind in ("text", "textarea") and f.label)
    for f in fields:
        ph = (f.raw.get("placeholder") or "").strip()
        if ph and counts.get((f.frame, f.label), 0) > 1 and ph != f.label:
            f.raw["label_was"] = f.label
            f.label = ph + (" *" if f.required and not ph.endswith("*") else "")
    return fields


def count_form_controls(page) -> int:
    n = 0
    for frame in page.frames:
        try:
            n += frame.locator("input:visible:not([type=hidden]), textarea:visible, select:visible").count()
        except Exception:  # noqa: BLE001
            continue
    return n


def wait_for_form(page, ats: str, log: Callable[[str], None], timeout: float = 20) -> bool:
    """Wait until some frame holds a form with 3+ controls, clicking an Apply button on Greenhouse job
    pages that hide the form behind one. Returns whether a form was found."""
    page.wait_for_load_state("domcontentloaded")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_form_controls(page) >= 3:
            return True
        if ats == "greenhouse":
            btn = page.locator("a:has-text('Apply'), button:has-text('Apply')").first
            try:
                if btn.count() and btn.is_visible():
                    btn.click(timeout=2000)
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(700)
    log("warning: could not find an application form with 3+ fields; continuing anyway")
    return False


def looks_like_application(fields: list[Field]) -> bool:
    """An application form asks for a resume, or at least a name and an email. A job-search page, a
    talent-community signup or a redirect to the careers home page does not, and must never be filled
    as if it were one (a closed posting often redirects there)."""
    import re as _re
    # hidden fields do not count (a form behind an Apply button is not reached yet); upload inputs are
    # usually hidden behind a styled button, so a resume upload counts once some field is visible
    shown = [(_re.sub(r"\s+", " ", f.label or "").lower(), f.kind) for f in fields if f.visible and f.kind != "file"]
    uploads = [_re.sub(r"\s+", " ", f.label or "").lower() for f in fields if f.kind == "file"]
    resume = bool(shown) and any(_re.search(r"resume|cv\b|curriculum", l) for l in uploads)
    email = any(_re.search(r"\be-?mail\b", l) for l, _ in shown)
    name = any(_re.search(r"\bname\b", l) for l, _ in shown)
    return resume or (email and name)
