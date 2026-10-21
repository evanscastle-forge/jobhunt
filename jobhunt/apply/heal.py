"""Self-healing widgets: when an adapter cannot set a value, or verify says it did not stick, ask the model
for a recipe, run it, and read the value back. A recipe that verifies is cached by widget signature, so the
next form with the same widget needs no model call and no code change.

Recipes use a closed vocabulary, never free code:

    {"do": "click", "css": "..."}      click an element ("self" = the field itself)
    {"do": "type", "text": "..."}      type into the focused element, key by key
    {"do": "fill", "css": "...", "text": "..."}
    {"do": "press", "key": "Enter"}
    {"do": "pick", "text": "..."}      click the visible element whose text is exactly this (a menu option)
    {"do": "wait", "ms": 500}

"{value}" and "{head}" (the value up to its first comma) are substituted, so a cached recipe works for
any value. Every attempt is written to fields.json by the caller.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .read import read_form
from .schema import Field
from .verify import matches
from .widgets import frame_of, locator

CONTAINER_JS = """el => {
  const c = el.closest('fieldset, .application-question, .select__container, [data-automation-id], [class*="field"], [class*="question"]')
            || el.parentElement && el.parentElement.parentElement || el;
  return c.outerHTML.replace(/<svg[\\s\\S]*?<\\/svg>/g, '<svg/>').replace(/\\s+/g, ' ').slice(0, 5000);
}"""


def container_html(page, f: Field) -> str:
    try:
        return locator(page, f).evaluate(CONTAINER_JS, timeout=1500)
    except Exception:  # noqa: BLE001
        return ""


def signature(ats: str, f: Field, html: str) -> str:
    """What makes two widgets the same widget: the ATS, the control, and the stems of its container's
    classes (generated hashes like remix-css-1a0ro4n dropped)."""
    classes = re.findall(r'class="([^"]*)"', html[:1500])
    stems = sorted({re.sub(r"[-_]?[a-z0-9]*\d[a-z0-9]*$", "", c) for cl in classes for c in cl.split()
                    if c and not re.search(r"\d{3,}|css-", c)})[:12]
    r = f.raw
    return f"{ats}|{f.kind}|{r.get('tag')}|{r.get('type')}|{r.get('role') or ''}|{'.'.join(stems)}"


class Healer:
    def __init__(self, call: Optional[Callable[[str], Optional[str]]], path: Optional[Path], ats: str,
                 log: Callable[[str], None] = print):
        self.call, self.path, self.ats, self.log = call, Path(path) if path else None, ats, log
        self.cache: dict = {}
        if self.path and self.path.exists():
            try:
                self.cache = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self.cache = {}

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def heal(self, page, f: Field, value: Any, error: str) -> tuple[Optional[Any], dict]:
        """Returns (what the page now shows, a record for fields.json). None when it could not heal."""
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else ", ".join(value)
        value = str(value)
        html = container_html(page, f)
        sig = signature(self.ats, f, html)
        rec = {"signature": sig, "error": error[:300], "from": None, "steps": None, "ok": False}
        cached = self.cache.get(sig)
        if cached:
            got = self._run(page, f, cached["steps"], value)
            if got is not None:
                cached["uses"] = cached.get("uses", 0) + 1
                self._save()
                return got, {**rec, "from": "cache", "steps": cached["steps"], "ok": True}
        if self.call is None:
            return None, {**rec, "from": "no model"}
        steps = self._ask(f, value, error, html)
        rec.update(**{"from": "model", "steps": steps})
        if not steps:
            return None, rec
        got = self._run(page, f, steps, value)
        if got is None:
            return None, rec
        self.cache[sig] = {"steps": _generalise(steps, value), "label": f.label[:120], "uses": 1}
        self._save()
        self.log(f"healed {f.label[:60]!r} ({f.kind}); recipe cached for {sig}")
        return got, {**rec, "ok": True}

    def _ask(self, f: Field, value: str, error: str, html: str) -> Optional[list]:
        prompt = (
            "A browser automation tool must set a form field to a value and its standard method failed. Give the "
            "shortest sequence of steps that sets it, using ONLY these step types (JSON):\n"
            '{"do":"click","css":"<selector or self>"} {"do":"type","text":"..."} {"do":"fill","css":"...","text":"..."} '
            '{"do":"press","key":"Enter"} {"do":"pick","text":"<exact visible option text>"} {"do":"wait","ms":500}\n'
            "\"self\" is the field element. Selectors must match inside the HTML shown (menus may render elsewhere on "
            "the page: use pick for options). Write {value} for the value and {head} for its part before the first "
            "comma, never the literal value, so the recipe works for other values.\n\n"
            'Reply with JSON only: {"steps": [...]}\n\n'
            f"Field label: {f.label}\nField kind: {f.kind}\nValue to set: {value}\nWhat failed: {error}\n\n"
            f"HTML around the field:\n{html}\n")
        try:
            out = self.call(prompt)
        except Exception:  # noqa: BLE001
            return None
        m = re.search(r"\{.*\}", out or "", re.S)
        try:
            steps = json.loads(m.group(0)).get("steps") if m else None
        except ValueError:
            return None
        ok = isinstance(steps, list) and 0 < len(steps) <= 8 and all(
            isinstance(s, dict) and s.get("do") in ("click", "type", "fill", "press", "pick", "wait") for s in steps)
        return steps if ok else None

    def _run(self, page, f: Field, steps: list, value: str) -> Optional[Any]:
        head = re.split(r"[,(]", value)[0].strip()
        sub = lambda s: str(s).replace("{value}", value).replace("{head}", head)   # noqa: E731
        fr = frame_of(page, f)
        try:
            for s in steps:
                do = s.get("do")
                target = (lambda css: locator(page, f) if css in ("self", "", None) else fr.locator(css).first)
                if do == "click":
                    target(s.get("css")).click(timeout=3000)
                elif do == "fill":
                    target(s.get("css")).fill(sub(s.get("text", "")), timeout=3000)
                elif do == "type":
                    page.keyboard.type(sub(s.get("text", "")), delay=50)
                elif do == "press":
                    page.keyboard.press(str(s.get("key") or "Enter"))
                elif do == "pick":
                    want = sub(s.get("text", ""))
                    opt = fr.get_by_text(want, exact=True).locator("visible=true").first
                    if not opt.count():
                        opt = page.get_by_role("option", name=want).first
                    opt.click(timeout=3000)
                elif do == "wait":
                    page.wait_for_timeout(min(3000, int(s.get("ms") or 300)))
            page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            return None
        now = next((x for x in read_form(page) if x.ident == f.ident), None)
        if now is None or not now.has_value():
            return None
        return now.value if matches(f.kind, value, now.value) else None     # the wrong option is not a heal


def _generalise(steps: list, value: str) -> list:
    """Replace a literal value the model wrote anyway with the placeholder, so the cached recipe is general."""
    head = re.split(r"[,(]", value)[0].strip()
    out = []
    for s in steps:
        s = dict(s)
        for k in ("text", "css"):
            if isinstance(s.get(k), str) and value and value in s[k]:
                s[k] = s[k].replace(value, "{value}")
            elif isinstance(s.get(k), str) and len(head) > 2 and head in s[k]:
                s[k] = s[k].replace(head, "{head}")
        out.append(s)
    return out
