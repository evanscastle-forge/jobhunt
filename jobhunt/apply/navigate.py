"""The generic navigator: from a page with no recognised form (an employer's own careers site), reach the
application form. Obvious buttons ("Apply", "Apply for this job") are clicked without asking; otherwise
the model is shown the page's clickable elements and picks one per step, up to a handful of steps. It
stops at a form, at a login wall, or when nothing more can be done, and every step is logged.

Runs it produces are marked supervised: the form they reach is filled by the ordinary stages, but no
playbook vouches for the path.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from .read import looks_like_application, read_form

CLICKABLES_JS = """() => {
  const els = Array.from(document.querySelectorAll('a[href], button, [role=button], input[type=submit], input[type=button]'))
    .filter(e => (e.offsetWidth || e.offsetHeight) && getComputedStyle(e).visibility !== 'hidden');
  return els.slice(0, 150).map((e, i) => { e.dataset.jhNav = String(i);
    return {i, tag: e.tagName.toLowerCase(), text: (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
            href: e.href || '', target: e.target || ''}; });
}"""
_OBVIOUS = re.compile(r"^\s*(apply( now| for this (job|role|position))?|start (your )?application|submit (an )?application|"
                      r"i'?m interested|apply to this job)\s*[›>→]?\s*$", re.I)


def login_wall(page) -> bool:
    try:
        return bool(page.locator("input[type=password]:visible").count())
    except Exception:  # noqa: BLE001
        return False


def navigate(page, job: dict, call: Optional[Callable[[str], Optional[str]]] = None,
             log: Callable[[str], None] = print, max_steps: int = 6) -> tuple[bool, list]:
    """(reached a form, the steps taken)."""
    steps: list = []
    tried: set = set()
    for _ in range(max_steps):
        if looks_like_application(read_form(page)):
            return True, steps
        if login_wall(page):
            steps.append({"stop": "login wall"})
            return False, steps
        try:
            els = page.evaluate(CLICKABLES_JS)
        except Exception:  # noqa: BLE001
            return False, steps
        pick = next((e for e in els if _OBVIOUS.match(e["text"]) and (e["text"], e["href"]) not in tried), None)
        why = "obvious apply control"
        if pick is None and call is not None:
            pick, why = _ask(call, els, job, tried), "chosen by the model"
        if pick is None:
            steps.append({"stop": "nothing left to try"})
            return False, steps
        tried.add((pick["text"], pick["href"]))
        steps.append({"click": pick["text"], "href": pick["href"][:160], "why": why})
        log(f"navigator: {why}: {pick['text']!r}")
        try:
            if pick["href"] and (pick["target"] == "_blank" or pick["tag"] == "a"):
                page.goto(pick["href"], wait_until="domcontentloaded", timeout=30000)
            else:
                page.locator(f'[data-jh-nav="{pick["i"]}"]').first.click(timeout=4000)
            page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001
            steps.append({"error": f"{type(e).__name__}: {str(e)[:160]}"})
    return looks_like_application(read_form(page)), steps


def _ask(call, els: list, job: dict, tried: set) -> Optional[dict]:
    rows = [e for e in els if e["text"] and (e["text"], e["href"]) not in tried][:100]
    if not rows:
        return None
    prompt = (f"A browser is on an employer's careers site and must reach the application form for \"{job.get('title')}\" "
              f"at {job.get('company')}. Which ONE element should it click next? Never choose sign-in, sign-up, "
              "LinkedIn/Indeed apply, or social links. Reply with JSON only: {\"i\": <number>} or {\"i\": null}.\n\n"
              + "\n".join(f"{e['i']}. [{e['tag']}] {e['text']}" + (f" -> {e['href'][:100]}" if e["href"] else "") for e in rows))
    try:
        m = re.search(r"\{.*\}", call(prompt) or "", re.S)
        i = json.loads(m.group(0)).get("i") if m else None
        return next((e for e in rows if e["i"] == int(i)), None) if i is not None else None
    except Exception:  # noqa: BLE001
        return None
