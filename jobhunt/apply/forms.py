"""Application assistant: the orchestrator. The contract is docs/apply-design.md.

    open      navigate, find the form (in any frame), wait for it
    read      read.py      page -> Fields
    answer    answer.py    Field -> Answer with a tier (certain / inferred / drafted / unknown)
    fill      widgets.py   only *certain* answers are typed
    verify    verify.py    every filled value is read back; the page's own invalid markers are collected
    hand-off  a run is *ready* only when nothing blocks; otherwise the issues say what does

Submission is never automatic: the one click is yours, on the employer's own Submit button. While the
window waits, the run watches for a confirmation (you submitted), for resume/abandon from the dashboard
or CLI, for the window closing, and for what you change in the window before submitting.

Rehearsal (`rehearse=True`) stops after verify and closes the window, so it is safe to run against any
real posting: it is how the single-click rate is measured without applying anywhere.
"""
from __future__ import annotations

import json
from datetime import datetime
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import answers as bankmod
from . import issues as issuesmod
from . import verify as verifymod
from .answer import AI_USE_WHY, Answerer
from .browser import first_page, open_browser
from .navigate import navigate
from .pages import next_button, walk
from .read import looks_like_application, read_form, wait_for_form
from .resolve import Cache as ResolveCache
from .resolve import is_aggregator, resolve
from .runs import NullRun
from .schema import FILE_PREFIX, TIERS
from .widgets import combobox_options, locator, set_value

SUPPORTED = ("greenhouse", "lever", "ashby")

# ----------------------------------------------------------------------------- ATS detection

def detect_ats(url: str) -> str:
    u = (url or "").lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "jobs.lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "myworkdayjobs.com" in u or "myworkdaysite.com" in u or ".workday.com/" in u and "/job/" in u:
        return "workday"
    return "unknown"


def apply_page_url(job: dict) -> str:
    ats = detect_ats(job.get("apply_url") or job.get("url") or "")
    url = job.get("apply_url") or job.get("url") or ""
    if ats == "lever" and not url.rstrip("/").endswith("/apply"):
        url = url.rstrip("/") + "/apply"
    if ats == "ashby" and not url.rstrip("/").endswith("/application"):
        url = url.rstrip("/") + "/application"
    return url


def embedded_ats(page) -> Optional[str]:
    """An employer career page that embeds a supported ATS form in an iframe."""
    for fr in page.frames[1:]:
        ats = detect_ats(fr.url or "")
        if ats in SUPPORTED:
            return ats
    return None


def off_the_form(page, ats: str) -> Optional[str]:
    """The posting's page redirected somewhere that is not its ATS form: a closed posting often lands
    on the employer's careers home page, whose job-alert signup must never be filled as the application.
    Returns why, or None. A company page that embeds the ATS in an iframe is still the form."""
    if file_url(page.url) or detect_ats(page.url) == ats or embedded_ats(page) == ats:
        return None
    return f"the posting redirected to {page.url}, which is not its {ats} form: it may be closed"


def file_url(url: str) -> bool:
    return (url or "").startswith("file:")


# ----------------------------------------------------------------------------- report

@dataclass
class Report:
    ats: str = ""
    url: str = ""
    filled: list = field(default_factory=list)     # (label, value) -- certain answers only
    uploaded: list = field(default_factory=list)   # labels
    flagged: list = field(default_factory=list)    # (label, reason)
    questions: list = field(default_factory=list)  # {label, kind, options, required, key, tier, why, draft, answered}
    blockers: list = field(default_factory=list)   # strings
    issues: list = field(default_factory=list)     # issues.Issue
    verify: list = field(default_factory=list)     # {label, expected, got, why}
    invalid: list = field(default_factory=list)    # labels the page marks aria-invalid
    tiers: dict = field(default_factory=dict)      # tier -> count of fields
    trace: list = field(default_factory=list)      # one decision per field, written to fields.json
    autofill: dict = field(default_factory=dict)   # the employer's own autofill: how, filled, kept, corrected
    review: list = field(default_factory=list)     # typed but not certain: {label, value, tier, source, why, key}
    fill: dict = field(default_factory=dict)       # {"required": n, "filled": k, "ratio": k/n}: the 90%-filled metric
    heals: list = field(default_factory=list)      # self-healing attempts: signature, recipe, from cache or model, ok
    resolved: dict = field(default_factory=dict)   # an aggregator listing followed to the real form: from, to, how
    navigation: list = field(default_factory=list) # the generic navigator's steps to reach a form
    supervised: bool = False                       # reached by the navigator: no playbook vouches for the path
    page_events: list = field(default_factory=list)  # console errors, page errors, failed requests
    ready: bool = False
    submitted: bool = False
    confirmation: Optional[str] = None
    error: Optional[str] = None
    reason: Optional[str] = None                   # why the run paused (runs.REASONS key)

    def open_required(self) -> list[dict]:
        return [q for q in self.questions if q.get("required") and not q.get("answered") and not q.get("skipped")]

    def to_dict(self) -> dict:
        return {"ats": self.ats, "url": self.url, "filled": [list(x) for x in self.filled], "uploaded": list(self.uploaded),
                "flagged": [list(x) for x in self.flagged], "blockers": list(self.blockers),
                "verify": list(self.verify), "invalid": list(self.invalid), "tiers": dict(self.tiers),
                "autofill": dict(self.autofill), "review": list(self.review), "fill": dict(self.fill),
                "heals": list(self.heals), "resolved": dict(self.resolved), "navigation": list(self.navigation),
                "supervised": self.supervised,
                "ready": self.ready, "submitted": self.submitted, "confirmation": self.confirmation,
                "error": self.error, "reason": self.reason}

    def summary(self) -> str:
        lines = [f"ATS: {self.ats}   URL: {self.url}"]
        if self.tiers:
            lines.append("Fields by tier: " + ", ".join(f"{t} {self.tiers.get(t, 0)}" for t in TIERS))
        lines.append(f"Filled {len(self.filled)} field(s):")
        for lab, val in self.filled:
            v = (val if isinstance(val, str) else str(val))
            lines.append(f"  - {lab[:60]!r} <- {v[:70]!r}")
        for lab in self.uploaded:
            lines.append(f"  - uploaded file for {lab[:60]!r}")
        if self.flagged:
            lines.append(f"Flagged {len(self.flagged)} field(s) for you:")
            for lab, why in self.flagged:
                lines.append(f"  ! {lab[:80]!r}: {why}")
        for m in self.verify:
            lines.append(f"  x {m['label'][:60]!r}: {m['why']} (wanted {m['expected']!r}, page has {m['got']!r})")
        if self.questions:
            lines.append(f"Questions for you: {len(self.questions)} ({len(self.open_required())} required still open)")
        if self.fill:
            lines.append(f"Required fields filled: {self.fill['filled']}/{self.fill['required']} ({self.fill['ratio']:.0%})")
        for r in self.review:
            v = r["value"] if isinstance(r["value"], str) else str(r["value"])
            lines.append(f"  ? {r['label'][:60]!r} <- {v[:90]!r}  [{r['tier']}: check it]")
        lines.append(("READY: press Submit in the window" + (f" after checking {len(self.review)} outlined answer(s)."
                                                              if self.review else ".")) if self.ready
                     else "Needs you: " + "; ".join(self.blockers))
        if self.submitted:
            lines.append(f"SUBMITTED. Confirmation: {self.confirmation or '(no confirmation text found)'}")
        if self.error:
            lines.append(f"ERROR: {self.error}")
        return "\n".join(lines)


CONFIRM_RE = re.compile(r"thank you|thanks for applying|application (has been|was|is) (submitted|received|sent)|"
                        r"we('ve| have) received your application|successfully submitted|application submitted", re.I)


CAPTCHA_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
                      return r.width > 30 && r.height > 30 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const frames = Array.from(document.querySelectorAll("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], iframe[title*='challenge' i]"));
  const invisible = frames.some(f => /size=invisible/.test(f.src)) || !!document.querySelector('.grecaptcha-badge, [data-size="invisible"]');
  // a checkbox or a challenge you would have to act on: visible, and not the invisible badge
  const visible = frames.some(f => vis(f) && !/size=invisible/.test(f.src) && !f.closest('.grecaptcha-badge'));
  return {visible, invisible: invisible || (frames.length > 0 && !visible)};
}
"""


def captcha_state(page) -> dict:
    """{"visible": a checkbox or challenge you must solve, "invisible": a scorer that runs on Submit}.
    Invisible reCAPTCHA sits on nearly every Greenhouse board; it is not something to solve, only a
    reason a challenge may appear when you press Submit, so it does not block ready."""
    out = {"visible": False, "invisible": False}
    for fr in page.frames:
        try:
            got = fr.evaluate(CAPTCHA_JS)
        except Exception:  # noqa: BLE001
            continue
        out["visible"] |= bool(got.get("visible"))
        out["invisible"] |= bool(got.get("invisible"))
    return out


def has_captcha(page) -> bool:
    return captcha_state(page)["visible"]


def confirmation_text(page) -> Optional[str]:
    """The confirmation the page shows after a submit, or None."""
    try:
        if re.search(r"confirmation|thanks|success", page.url or "", re.I):
            return f"redirected to {page.url}"
        body = page.locator("body").inner_text(timeout=2000)
    except Exception:  # noqa: BLE001
        return None
    m = CONFIRM_RE.search(body)
    if not m:
        return None
    a, b = max(0, m.start() - 40), min(len(body), m.end() + 80)
    return re.sub(r"\s+", " ", body[a:b]).strip()


# ----------------------------------------------------------------------------- the run

class Filler:
    """State of one application across fill passes and resumes."""

    def __init__(self, answerer: Answerer, report: Report, log: Callable[[str], None], agent=None, healer=None):
        self.answerer, self.report, self.log, self.agent, self.healer = answerer, report, log, agent, healer
        self.expected: dict[str, tuple] = {}     # ident -> (label, kind, value set): what verify checks
        self.tier_of: dict[str, str] = {}        # ident -> tier, counted once per field
        self.left_empty: set[str] = set()        # idents we did not fill (to spot what you fill yourself)
        self.skipped: set[str] = set()           # issue ids you said to leave for the window
        self.blocked: list[issuesmod.Issue] = []
        self.trace: dict[str, dict] = {}         # ident -> what was read and decided (fields.json)
        self.file_fields = 0                     # upload fields seen: a form with any must end up with the resume
        self.autofilled: dict[str, object] = {}  # ident -> value the employer's autofill put there (not you)
        self.reviewed: set[str] = set()
        self.heals: list[dict] = []              # every self-healing attempt, for fields.json and the report
        self.verify_acc: list[dict] = []         # multi-page forms: what failed verify on pages already left
        self.verified: set[str] = set()          # idents verified on an earlier page (gone from the page now)
        self.force_required: set[str] = set()    # labels or idents the page itself said were missing

    def _trace(self, f, action: str, a=None, detail=None) -> None:
        """Record what the tool read and decided for one field: the evidence for fixing a rule."""
        self.trace[f.ident] = {
            "label": f.label, "kind": f.kind, "required": f.required, "frame": f.frame,
            "options": list(f.options or [])[:25], "page_value": f.value if f.kind != "file" else bool(f.value),
            "action": action, "tier": a.tier if a else None, "source": a.source if a else None,
            "answer": (None if a is None or (isinstance(a.value, str) and a.value.startswith(FILE_PREFIX))
                       else a.value), "why": (a.why if a else None), "detail": detail,
            "control": {k: f.raw.get(k) for k in ("tag", "type", "id", "name", "role")}}

    def block(self, label: str, why: str) -> None:
        self._flag(label, why, True)

    def required_from_errors(self, page, errors: list[str]) -> None:
        """The page refused to advance. Fields it marks invalid, or that its messages name, are required
        now, whatever their markup said."""
        text = " ".join(errors).lower()
        for f in read_form(page):
            invalid = False
            try:
                invalid = locator(page, f).get_attribute("aria-invalid", timeout=500) == "true"
            except Exception:  # noqa: BLE001
                pass
            lab = re.sub(r"[*✱]", "", f.label or "").strip().lower()
            if invalid or (len(lab) > 3 and lab in text):
                self.force_required.add(f.ident)
                self.expected.pop(f.ident, None)          # a value the page rejects gets another attempt
                self.trace.pop(f.ident, None)

    def page_verify(self, page) -> None:
        """Verify what was filled on this page before leaving it (multi-page forms)."""
        now = read_form(page)
        present = {f.ident for f in now}
        here = {i: v for i, v in self.expected.items() if i in present and i not in self.verified}
        bad = verifymod.check(now, here)
        if bad and self.healer is not None:
            by = {f.ident: f for f in now}
            for i, (label, kind, want) in list(here.items()):
                if label in {m["label"] for m in bad} and kind != "file":
                    healed, rec = self.healer.heal(page, by[i], want, "verify: the value did not stick")
                    self.heals.append({"label": label, **rec})
                    if healed is not None:
                        self.expected[i] = (label, kind, healed)
                        here[i] = self.expected[i]
            bad = verifymod.check(read_form(page), here)
        self.verify_acc += bad
        self.verified |= set(here)

    def _flag(self, label: str, why: str, blocking: bool) -> None:
        self.report.flagged.append((label, why))
        if blocking:
            self.blocked.append(issuesmod.blocked(label, why))

    def pass_(self, page) -> None:
        """Read, answer, fill (certain only). Safe to repeat: filled fields are never touched again."""
        rep, ans = self.report, self.answerer
        fields = read_form(page)
        self.log(f"found {len(fields)} form control(s) in {len({f.frame for f in fields}) or 1} frame(s)")
        self.file_fields = max(self.file_fields, sum(1 for f in fields if f.kind == "file"))
        parent = None
        for f in fields:
            if not f.visible and f.kind not in ("file", "radio", "checkbox", "checkgroup"):
                continue
            if _FOLLOWUP_RE.search(f.label or "") and parent is not None:
                pv = self.expected.get(parent.ident, (None, None, parent.value))[2]
                f.context = f"{parent.label.strip()[:160]} = {pv if pv not in (None, '', [], False) else '(unanswered)'}"
            elif f.label and f.kind not in ("file",):
                parent = f
            if f.ident in self.expected:
                continue
            if f.ident in self.force_required:
                f.required = True
            if f.has_value():
                self.left_empty.discard(f.ident)
                if f.ident in self.autofilled and f.ident not in self.reviewed:
                    self._review_autofill(page, f)
                elif f.ident not in self.trace:
                    self._trace(f, "prefilled")
                continue     # typed by you in the open window: never overwrite
            if f.kind not in ("file", "radio", "checkbox", "checkgroup") and not _interactable(page, f):
                self._trace(f, "hidden", detail="the page reports it visible, Playwright does not")
                continue     # e.g. the search box inside a closed phone-country picker
            if f.kind == "combobox" and not f.options:
                f.options = combobox_options(page, f)   # custom dropdowns hide their options until opened
            a = ans.answer(f)
            if a.skip:
                self._trace(f, "skipped", a)
                continue
            self.tier_of[f.ident] = a.tier
            if a.source == "rule:ai-use":
                self._flag(f.label, AI_USE_WHY, True)
                self.left_empty.add(f.ident)
                self._trace(f, "flagged", a)
                continue
            if not a.certain:
                if self._type_for_review(page, f, a):
                    continue
                self.left_empty.add(f.ident)
                q = ans.ask(f, a) if a.ask else None
                self._trace(f, "asked" if q is not None else "flagged", a)
                if q is not None and q["key"] in self.skipped:
                    q["skipped"] = True
                if q is None and a.why:
                    self._flag(f.label, a.why, f.required)
                continue
            shown, err = self._set(page, f, a.value)
            if shown is None and err.startswith("error:"):
                self._flag(f.label, f"could not fill: {err[6:][:80]}", True)
                self.left_empty.add(f.ident)
                self._trace(f, "fill_error", a, err[6:][:400])
                continue
            if shown is None:
                what = a.value[len(FILE_PREFIX):] if isinstance(a.value, str) and a.value.startswith(FILE_PREFIX) else a.value
                self._flag(f.label, f"could not set {what!r}", True)
                self.left_empty.add(f.ident)
                self._trace(f, "fill_error", a, "the adapter could not set the value")
                continue
            self.expected[f.ident] = (f.label, f.kind, shown)
            self.left_empty.discard(f.ident)
            self._trace(f, "filled", a, shown if f.kind != "file" else f"uploaded {shown}")
            if f.kind == "file":
                rep.uploaded.append(f.label)
            else:
                rep.filled.append((f.label, ", ".join(shown) if isinstance(shown, list) else shown))
            if a.why:
                rep.flagged.append((f.label, a.why))

    def _set(self, page, f, value) -> tuple:
        """set_value, then self-healing when the adapter fails. Returns (shown, error text)."""
        try:
            shown = set_value(page, f, value)
            err = "" if shown is not None else "the adapter could not set the value"
        except Exception as e:  # noqa: BLE001
            shown, err = None, f"error:{type(e).__name__}: {str(e)[:300]}"
        if shown is None and self.healer is not None and not (isinstance(value, str) and value.startswith(FILE_PREFIX)):
            healed, rec = self.healer.heal(page, f, value, err)
            self.heals.append({"label": f.label, **rec})
            if healed is not None:
                return healed, ""
        return shown, err

    def _type_for_review(self, page, f, a) -> bool:
        """The typing policy (docs/apply-design.md): an answer that is not certain is still typed when it is
        grounded (an inferred rule, a stored proposal, or the applicant agent with the posting in hand), the
        question is not sensitive, and the value verifies. It is outlined in the window and listed in
        report.review for you to check before you press Submit. A decline falls through to a question."""
        from .schema import Answer
        ans = self.answerer
        if ans.sensitive(f) or f.kind in ("file", "checkbox") or a.source == "rule:ai-use":
            return False
        value, tier, source, why = None, None, "", a.why
        if a.tier == "inferred" and a.value not in (None, "", []):
            value, tier, source = a.value, "inferred", a.source
        elif f.kind in ("text", "textarea", "select", "combobox", "radio", "checkgroup"):
            stored = ans.stored_proposal(f)
            if stored:
                value, tier, source = stored[0], "agent", f"bank-proposal:{stored[1].key}"
                why = stored[1].note or "a proposal in the answer bank"
            elif self.agent is not None and len((f.label or "").strip()) > 3 and f.kind != "checkgroup":
                try:
                    q = ans.qtext(f) + (f"\n(Text on the form around this question: {f.help})" if f.help else "")
                    res = self.agent.ask(q, f.kind, f.options if f.kind in ("select", "combobox", "radio") else None, ans.job)
                except Exception as e:  # noqa: BLE001
                    res = None
                    self.log(f"applicant agent failed on {f.label[:60]!r}: {type(e).__name__}: {e}")
                if res is not None and not res.declined and res.answer:
                    value, tier, source = res.answer, "agent", "agent:" + ",".join(res.evidence)
                    why = res.reason or "written by the applicant agent from the cited lines"
                elif res is not None:
                    a.why = f"the applicant agent declined: {res.reason[:200]}" + (f" · needs: {res.needs[:160]}" if res.needs else "")
        if value in (None, "", []):
            return False
        shown, err = self._set(page, f, value)
        if shown is None:
            self._trace(f, "fill_error", a, f"typing for review: {err[:300]}")
            return False
        typed = Answer(value, tier, source, why, ask=True)
        q = ans.ask(f, typed)
        key = q["key"] if q else ""
        if q and ans.bank is not None:
            e = ans.bank.get(key)
            if e is not None and not e.approved and not e.answer.strip():
                ans.bank.set(key, answer=", ".join(shown) if isinstance(shown, list) else str(shown),
                             by="applicant_agent" if tier == "agent" else "rule", note=why or "")
        self.expected[f.ident] = (f.label, f.kind, shown)
        self.left_empty.discard(f.ident)
        self.tier_of[f.ident] = tier
        shown_s = ", ".join(shown) if isinstance(shown, list) else shown
        self.report.filled.append((f.label, shown_s))
        self.report.review.append({"label": f.label, "value": shown_s, "tier": tier, "source": source,
                                   "why": why, "key": key, "company_specific": _company_specific(q, shown_s, ans.job)})
        _outline(page, f)
        self._trace(f, "typed_for_review", typed, shown)
        return True

    def employer_autofill(self, page, resume: Optional[str]) -> None:
        """Let the employer's own tools fill the form first, then treat what they filled as values to
        check rather than as yours. Most ATSes parse an uploaded resume into the form (Lever does it on
        the resume field itself; Ashby has a separate "Autofill from resume" box). Login-based autofill
        (MyGreenhouse, LinkedIn, Indeed) is never used."""
        rep = self.report
        rep.autofill = {"how": None, "filled": 0, "kept": 0, "corrected": [], "unconfirmed": 0}
        if not resume or not Path(resume).exists():
            return
        fields = read_form(page)
        target = next((f for f in fields if f.kind == "file" and _AUTOFILL_RE.search(f.label)), None) \
            or next((f for f in fields if f.kind == "file" and re.search(r"resume|cv\b|curriculum", f.label, re.I)), None)
        how = None
        if target is not None:
            try:
                shown = set_value(page, target, FILE_PREFIX + str(resume))
            except Exception as e:  # noqa: BLE001
                self.log(f"employer autofill: could not upload to {target.label!r}: {e}")
                shown = None
            if shown:
                how = f"resume uploaded to {target.label!r}"
                if not _AUTOFILL_RE.search(target.label):     # the resume field itself: it counts as filled
                    self.expected[target.ident] = (target.label, "file", shown)
                    rep.uploaded.append(target.label)
                    from .schema import Answer
                    self._trace(target, "filled", Answer(FILE_PREFIX + str(resume), "certain", "profile:documents.resume"),
                                f"uploaded {shown} first, so the employer can parse it")
        if how is None:
            how = _click_autofill_button(page, str(resume), self.log)
        if how is None:
            self.log("employer autofill: none offered")
            return
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        for f in read_form(page):
            if f.has_value() and f.ident not in self.expected and f.kind != "file":
                self.autofilled[f.ident] = f.value
        rep.autofill.update(how=how, filled=len(self.autofilled))
        self.log(f"employer autofill: {how}; the page filled {len(self.autofilled)} field(s), which will be checked")

    def _review_autofill(self, page, f) -> None:
        """A value the employer's autofill put in a field. Where we know the answer for certain, keep it if
        it agrees and correct it if not; where we do not, it is a proposal you confirm once."""
        from .schema import Answer
        self.reviewed.add(f.ident)
        rep, ans = self.report, self.answerer
        a = ans.answer(f)
        if a.certain and not (isinstance(a.value, str) and a.value.startswith(FILE_PREFIX)):
            want = ", ".join(a.value) if isinstance(a.value, list) else a.value
            if verifymod.matches(f.kind, a.value if f.kind == "checkgroup" else want, f.value):
                self.expected[f.ident] = (f.label, f.kind, f.value)
                rep.autofill["kept"] += 1
                self._trace(f, "autofill_ok", a, f.value)
                return
            try:
                if f.kind in ("text", "textarea"):
                    locator(page, f).fill("", timeout=3000)
                shown = set_value(page, f, a.value)
            except Exception as e:  # noqa: BLE001
                shown = None
                self._trace(f, "fill_error", a, f"correcting autofill: {type(e).__name__}: {str(e)[:300]}")
            if shown:
                self.expected[f.ident] = (f.label, f.kind, shown)
                rep.filled.append((f.label, ", ".join(shown) if isinstance(shown, list) else shown))
                rep.autofill["corrected"].append({"label": f.label, "page": f.value, "now": shown})
                self._trace(f, "corrected_autofill", a, {"page": f.value, "now": shown})
            else:
                self._flag(f.label, f"the page's autofill put {f.value!r} here and it could not be corrected to {want!r}", True)
            return
        if a.skip:
            self._trace(f, "autofill_kept", a, f.value)
            return
        proposal = Answer(f.value, "inferred", "page:autofill", ask=True,
                          why=f"the employer's autofill put {f.value!r} here; confirm it")
        q = ans.ask(f, proposal)
        rep.autofill["unconfirmed"] += 1
        self._trace(f, "autofill_unconfirmed", proposal, f.value)
        if q is None:
            self._flag(f.label, proposal.why, f.required)

    def fill(self, page) -> None:
        self.pass_(page)
        page.wait_for_timeout(600)       # conditional fields appear after the first answers
        before = len(self.expected)
        self.pass_(page)
        if len(self.expected) > before:
            self.log("filled additional fields that appeared after the first pass")
        rep = self.report
        rep.flagged = list(dict.fromkeys(rep.flagged))
        rep.filled = list(dict.fromkeys(rep.filled))
        rep.uploaded = list(dict.fromkeys(rep.uploaded))
        filled_labels = {lab for lab, _ in rep.filled}
        for q in self.answerer.questions:
            q["answered"] = q["label"] in filled_labels
        rep.questions = list(self.answerer.questions)
        rep.flagged = [(lab, why) for lab, why in rep.flagged if lab not in filled_labels]   # filled: no longer a flag
        self.answerer.save()

    def check(self, page) -> None:
        """verify + the hand-off decision."""
        rep = self.report
        pending = {i: v for i, v in self.expected.items() if i not in self.verified}
        rep.verify = verifymod.check(read_form(page), pending)
        if rep.verify and self.healer is not None:
            now = {f.ident: f for f in read_form(page)}
            for ident, (label, kind, want) in list(self.expected.items()):
                if label in {m["label"] for m in rep.verify} and ident in now and kind not in ("file",):
                    healed, rec = self.healer.heal(page, now[ident], want, "verify: the value did not stick")
                    self.heals.append({"label": label, **rec})
                    if healed is not None:
                        self.expected[ident] = (label, kind, healed)
            rep.verify = verifymod.check(read_form(page), {i: v for i, v in self.expected.items() if i not in self.verified})
        rep.verify = self.verify_acc + rep.verify
        if any(m["got"] is None for m in rep.verify):
            kinds = {lab: kind for lab, kind, _ in self.expected.values()}
            rep.verify = verifymod.settle_files(rep.verify, kinds, verifymod.page_text(page))
        bad = {m["label"]: m for m in rep.verify}
        for rec in self.trace.values():
            if rec["action"] == "filled" and rec["label"] in bad:
                rec["action"], rec["detail"] = "did_not_stick", bad[rec["label"]]
        problem = {i for i, r in self.trace.items() if r["action"] in _PROBLEM_ACTIONS and "html" not in r}
        if problem:
            from .heal import container_html
            for f in read_form(page):
                if f.ident in problem:
                    self.trace[f.ident]["html"] = container_html(page, f)   # evidence for a fix, and for `fixture`
        rep.trace = list(self.trace.values())
        req = [r for r in rep.trace if r["required"] and r["action"] not in ("hidden",)]
        done = [r for r in req if r["action"] in _FILLED_ACTIONS]
        if self.file_fields and not any(r["kind"] == "file" and r["required"] for r in req):
            req_n, done_n = len(req) + 1, len(done) + (1 if rep.uploaded else 0)   # the resume always counts
        else:
            req_n, done_n = len(req), len(done)
        rep.fill = {"required": req_n, "filled": done_n, "ratio": round(done_n / req_n, 3) if req_n else 1.0}
        rep.invalid = verifymod.invalid_fields(page)
        rep.heals = list(self.heals)
        rep.tiers = {t: sum(1 for v in self.tier_of.values() if v == t) for t in TIERS}
        issues = [issuesmod.from_question(q) for q in rep.questions if not q.get("answered")]
        for q, i in zip([q for q in rep.questions if not q.get("answered")], issues):
            if q.get("skipped"):
                i.blocking = False
        issues += list({i.id: i for i in self.blocked}.values())
        issues += [issuesmod.blocked(m["label"], f"{m['why']}: wanted {m['expected']!r}, page has {m['got']!r}")
                   for m in rep.verify]
        issues += [issuesmod.blocked(lab, "the page marks this field invalid") for lab in rep.invalid]
        if self.file_fields and not rep.uploaded:
            issues.append(issuesmod.blocked("resume", "the form has an upload field but no resume went in: "
                                                      "no file field was recognised as the resume"))
        cap = captcha_state(page)
        captcha = cap["visible"]
        if captcha:
            issues.append(issuesmod.Issue(id="blocked:captcha", kind="blocked", label="captcha",
                                          why="solve it in the window", blocking=True))
        elif cap["invisible"]:
            issues.append(issuesmod.Issue(id="note:invisible-captcha", kind="blocked", label="invisible captcha",
                                          why="scores your Submit; a challenge may appear when you press it",
                                          blocking=False))
        if rep.supervised:
            issues.append(issuesmod.Issue(id="note:supervised", kind="blocked", label="navigation",
                                          why="the navigator found this form on the employer's own site; check it is the right posting",
                                          blocking=False))
        rep.issues = [i for i in issues if i.id not in self.skipped]

        rep.blockers = []
        if captcha:
            rep.blockers.append("captcha present")
        if any(why == AI_USE_WHY for _, why in rep.flagged):
            rep.blockers.append("AI-use attestation question: answer it yourself")
        open_req = rep.open_required()
        if open_req:
            rep.blockers.append(f"{len(open_req)} required question(s) without an approved answer")
        by_hand = [i for i in rep.issues if i.kind == "blocked" and i.blocking and i.id != "blocked:captcha"
                   and i.why != AI_USE_WHY]
        if by_hand:
            rep.blockers.append(f"{len(by_hand)} field(s) to check or fill by hand")
        rep.ready = not rep.blockers
        rep.reason = "captcha" if captcha else "questions" if open_req else "review_and_submit" if rep.ready else "fix_fields"


_PROBLEM_ACTIONS = {"asked", "flagged", "fill_error", "did_not_stick", "typed_for_review", "autofill_unconfirmed"}
_FILLED_ACTIONS = {"filled", "autofill_ok", "corrected_autofill", "prefilled", "typed_for_review", "autofill_kept"}
_FOLLOWUP_RE = re.compile(r"^\s*(which ones?\??\s*[*:]*\s*$|if (yes|no|so|other|applicable|you (selected|answered|chose))|please (specify|explain|describe|"
                          r"elaborate|provide)|(explain|specify|details?|other)\s*[*:✱]*\s*$|if you selected|"
                          r"additional details)", re.I)


def _company_specific(q: Optional[dict], value, job: dict) -> bool:
    """An answer that names the company, or a question about it, is not reusable at the next company."""
    comp = (job or {}).get("company") or ""
    text = f"{(q or {}).get('question') or ''} {value}"
    return "{company}" in text or (len(comp) >= 3 and comp.lower() in str(value).lower())


def capture_form(page, run_dir) -> None:
    """Keep the form as it stood after filling (every frame's DOM), so rules can be replayed offline
    against every real form ever seen (`jobhunt.py replay`)."""
    if run_dir is None:
        return
    for i, fr in enumerate(page.frames):
        try:
            html = fr.content()
        except Exception:  # noqa: BLE001
            continue
        if i == 0 or "<form" in html or "<input" in html:
            (Path(run_dir) / ("form.html" if i == 0 else f"form-frame{i}.html")).write_text(html, encoding="utf-8")


def learn_from_submit(bank, review: list[dict], edits: Optional[dict], questions: Optional[list] = None,
                      job: Optional[dict] = None) -> dict:
    """You pressed Submit (or Mark submitted). Remember what you submitted, so the next form is certain:

    - an answer typed for your review that you left alone becomes approved (`by: approved-by-submit`);
      one you changed in the window becomes your edited answer;
    - a question the tool asked you, that you answered in the window, becomes your approved answer;
    - an answer about this company (a {company} question, or one naming it) is not reused elsewhere: it is
      kept in notes/voice.md under "Answers I wrote", as your words for the voice of future drafts.
    """
    if bank is None:
        return {"approved": 0, "edited": 0, "answered": 0, "voice": 0}
    changed = {c["label"]: c["got"] for c in (edits or {}).get("changed") or []}
    yours = {c["label"]: c["value"] for c in (edits or {}).get("filled_by_you") or [] if isinstance(c, dict)}
    out = {"approved": 0, "edited": 0, "answered": 0, "voice": 0}
    flat = lambda v: ", ".join(v) if isinstance(v, list) else str(v)   # noqa: E731
    voice = []
    for r in review:
        if not r.get("key"):
            continue
        if r.get("company_specific"):
            if r["label"] in changed:
                voice.append((r["label"], flat(changed[r["label"]])))
            continue
        if r["label"] in changed:
            bank.set(r["key"], answer=flat(changed[r["label"]]), status="approved", by="",
                     note="edited by you in the window, then submitted")
            out["edited"] += 1
        else:
            bank.set(r["key"], answer=str(r["value"]), status="approved", by="approved-by-submit",
                     note=f"typed as {r['tier']} and submitted unchanged")
            out["approved"] += 1
    for q in questions or []:
        if q.get("label") not in yours or not q.get("key"):
            continue
        value = flat(yours[q["label"]])
        if _company_specific(q, value, job or {}):
            voice.append((q["label"], value))
            continue
        if bank.set(q["key"], answer=value, status="approved", by="", note="answered by you in the window") is not None:
            out["answered"] += 1
    if voice and bank.dir:
        _append_voice(Path(bank.dir) / "notes" / "voice.md", voice, job or {})
        out["voice"] = len(voice)
    bank.save()
    return out


def _append_voice(path: Path, rows: list, job: dict) -> None:
    """Your own answers about one company: the best voice samples there are, never reused verbatim."""
    head = "## Answers I wrote"
    text = path.read_text(encoding="utf-8") if path.exists() else "# Voice\n"
    if head not in text:
        text = text.rstrip() + f"\n\n{head}\n\nCaptured when I submitted an application; my words, for the voice of drafts.\n"
    stamp = datetime.now().strftime("%Y-%m-%d")
    for label, value in rows:
        text = text.rstrip() + f"\n\n### {label.strip()} ({job.get('company') or '?'}, {stamp})\n\n{value.strip()}\n"
    path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")


def _outline(page, f) -> None:
    """Mark a field typed for your review, so the window shows exactly what to check."""
    try:
        locator(page, f).evaluate(
            "el => { const t = el.closest('fieldset, .application-question, .select__container, [class*=\"field\"]') || el;"
            " t.style.outline = '3px solid #f5a623'; t.style.outlineOffset = '3px';"
            " t.title = 'Typed by the apply assistant for your review'; }", timeout=1500)
    except Exception:  # noqa: BLE001
        pass


_AUTOFILL_RE = re.compile(r"auto-?fill|autocomplete (from|with)|parse|import (from )?(your )?resume", re.I)
_LOGIN_AUTOFILL_RE = re.compile(r"linkedin|indeed|mygreenhouse|google|sign in|log in|login|account", re.I)


def _resume_path(profile: dict) -> Optional[str]:
    r = (profile.get("documents") or {}).get("resume")
    return str(Path(r).expanduser()) if r else None


def _click_autofill_button(page, resume: str, log) -> Optional[str]:
    """An "Autofill with resume" button that opens a file picker. Returns how it was used, or None."""
    for fr in page.frames:
        try:
            btns = fr.locator("button, [role=button], a, label").filter(has_text=_AUTOFILL_RE)
            n = min(btns.count(), 5)
        except Exception:  # noqa: BLE001
            continue
        for i in range(n):
            b = btns.nth(i)
            try:
                text = re.sub(r"\s+", " ", b.inner_text(timeout=1000)).strip()
                if _LOGIN_AUTOFILL_RE.search(text) or not b.is_visible():
                    continue
                with page.expect_file_chooser(timeout=3000) as fc:
                    b.click(timeout=2000)
                fc.value.set_files(resume)
                return f"resume given to the {text[:60]!r} button"
            except Exception:  # noqa: BLE001
                continue
    return None


def _interactable(page, f) -> bool:
    try:
        return locator(page, f).is_visible(timeout=1000)
    except Exception:  # noqa: BLE001
        return False


def _record(run, report: Report) -> None:
    run.data["report"] = report.to_dict()
    run.data["questions"] = report.questions
    run.data["issues"] = [i.to_dict() for i in report.issues]
    run.save()
    run_dir = getattr(run, "path", None)
    issuesmod.write_issues(run_dir, report.issues)
    if run_dir is not None:
        (run_dir / "fields.json").write_text(json.dumps(report.trace, indent=1, ensure_ascii=False, default=str),
                                             encoding="utf-8")
        if report.page_events:
            (run_dir / "page_events.jsonl").write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in report.page_events[-300:]), encoding="utf-8")


def run_apply(job: dict, profile: dict, headless: bool = False, wait_for_user: bool = True,
              log: Callable[[str], None] = print,
              drafter: Optional[Callable[[str, dict], Optional[str]]] = None,
              on_submitted: Optional[Callable[[], None]] = None, ats: Optional[str] = None,
              run=None, bank: Optional[bankmod.Bank] = None, rehearse: bool = False,
              browser_dir=None, channel: str = "chrome", agent=None, heal_call=None, recipes_path=None,
              resolve_cache=None, vault=None) -> Report:
    """Fill the form, then wait (see _attend) or, when rehearsing, close. `run` is a runs.Run (or None:
    no folder, nothing persisted). `browser_dir` selects a persistent browser profile (browser.py)."""
    run = run if run is not None else NullRun()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        r = Report(error="Playwright is not installed. Run: uv sync && uv run playwright install chromium")
        log(r.error)
        run.fail(r.error)
        return r

    url = apply_page_url(job)
    ats = ats or detect_ats(url)
    report = Report(ats=ats, url=url)
    rcache = ResolveCache(resolve_cache)
    filler = Filler(Answerer(profile, job, drafter=drafter, bank=bank), report, log, agent=agent)
    run.set_state("running", pid=os.getpid(), ats=ats, url=url, rehearsal=rehearse)
    log(f"{'rehearsing' if rehearse else 'opening'} {url}")

    try:
        pw = sync_playwright().start()
        ctx, close = open_browser(pw, headless=headless, profile_dir=browser_dir, channel=channel, log=log)
    except Exception as e:  # noqa: BLE001
        report.error = f"could not start the browser: {type(e).__name__}: {e}"
        log(report.error)
        run.fail(report.error)
        return report
    page = first_page(ctx)
    _watch(page, report)
    try:
        try:
            # resolve: an aggregator listing is not the form; the form is a link or two away
            unresolved = None
            if is_aggregator(url):
                hit = rcache.get(job.get("id") or url)
                if hit and hit.get("url"):
                    found, how = hit["url"], "cached: " + hit["how"]
                else:
                    found, how = resolve(page, job, log, heal_call)
                    rcache.put(job.get("id") or url, found, how)
                report.resolved = {"from": url, "to": found, "how": how}
                log(f"resolve: {url} -> {found or 'nothing'} ({how})")
                if found:
                    url = found
                    report.url, report.ats = url, detect_ats(url)
                    run.data.update(url=url, ats=report.ats)
                else:
                    unresolved = how
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            run.screenshot(page, "opened")
            if report.ats not in SUPPORTED:
                wait_for_form(page, report.ats, lambda s: None, timeout=8)
                report.ats = embedded_ats(page) or report.ats
            if report.ats not in SUPPORTED and report.ats != "workday" and not unresolved:
                ok, steps = navigate(page, job, heal_call, log)
                report.navigation = steps
                if ok:
                    report.ats = detect_ats(page.url) if detect_ats(page.url) in SUPPORTED else (embedded_ats(page) or "generic")
                    report.supervised = True
                    log(f"navigator reached a form at {page.url} ({report.ats})")
            if report.ats in SUPPORTED + ("generic",):
                wait_for_form(page, report.ats, log)
            if heal_call is not None or recipes_path is not None:
                from .heal import Healer
                filler.healer = Healer(heal_call, recipes_path, report.ats, log)
            if report.ats == "workday" and not unresolved:
                from .playbooks import workday
                page.wait_for_timeout(2500)
                out = workday.run(page, filler, vault, (profile.get("identity") or {}).get("email") or "",
                                  _resume_path(profile), log)
                report.navigation.append({"workday": out})
                if out["outcome"] == "pause":
                    report.blockers.append(out["note"])
                    report.reason = out["reason"]
                    run.screenshot(page, "paused")
                    capture_form(page, getattr(run, "path", None))
                    log(out["note"])
                    _record(run, report)
                    run.pause(out["reason"], note=out["note"])
                else:
                    filler.check(page)
                    if out["outcome"] == "stuck" and report.ready:
                        report.ready, report.reason = False, "fix_fields"
                        report.blockers.append(out["note"])
                    run.screenshot(page, "filled")
                    capture_form(page, getattr(run, "path", None))
                    log(report.summary())
                    _record(run, report)
                    run.pause(report.reason or "review_and_submit", note="; ".join(report.blockers) or "ready: at Workday's review page")
            elif unresolved or report.ats not in SUPPORTED + ("generic",):
                why = (f"could not find the application behind the listing: {unresolved}" if unresolved
                       else f"{report.ats} forms are not automated yet; complete this one by hand"
                       + (f" (navigator: {report.navigation[-1]})" if report.navigation else ""))
                report.blockers.append(why)
                report.reason = "unsupported_form"
                log(report.summary())
                _record(run, report)
                run.pause("unsupported_form", note=why)
            elif (report.ats != "generic" and off_the_form(page, report.ats)) or not looks_like_application(read_form(page)):
                note = (report.ats != "generic" and off_the_form(page, report.ats)) or (
                    f"the page at {page.url} does not look like an application form (no resume upload, no "
                    f"name and email): the posting may be closed or redirected")
                report.blockers.append(note)
                report.reason = "unsupported_form"
                log(note)
                run.screenshot(page, "not-a-form")
                _record(run, report)
                run.pause("unsupported_form", note=note)
            else:
                filler.employer_autofill(page, _resume_path(profile))
                if report.ats == "generic" and next_button(page) is not None:
                    outcome, note = walk(page, filler, log, is_review=lambda pg: False)   # "Step 1 / 3 ... Continue"
                    report.navigation.append({"pages": outcome, "note": note})
                else:
                    filler.fill(page)
                filler.check(page)
                run.screenshot(page, "filled")
                capture_form(page, getattr(run, "path", None))
                log(report.summary())
                _record(run, report)
                run.pause(report.reason or "review_and_submit", note="; ".join(report.blockers) or "ready")
        except Exception as e:  # noqa: BLE001
            report.error = f"{type(e).__name__}: {e}"
            report.reason = "error"
            trace = traceback.format_exc()
            log(report.error)
            log(trace)
            run.data["error_trace"] = trace
            run.screenshot(page, "error")
            _record(run, report)
            run.pause("error", note=report.error, error=report.error)
        if rehearse:
            run.set_state("rehearsed", note=("ready" if report.ready else "; ".join(report.blockers)) or report.reason or "")
        elif wait_for_user and not headless:
            _attend(page, run, filler, log, on_submitted)
    finally:
        close()
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return report


def _watch(page, report: Report) -> None:
    """Keep what the page itself complains about: console errors, uncaught exceptions, failed requests.
    A form that breaks under automation usually says so here first."""
    def add(kind: str, text: str) -> None:
        if len(report.page_events) < 1000:
            report.page_events.append({"at": time.strftime("%H:%M:%S"), "kind": kind, "text": text[:600]})
    page.on("console", lambda m: add(f"console.{m.type}", m.text) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: add("pageerror", str(e)))
    page.on("requestfailed", lambda r: add("requestfailed", f"{r.method} {r.url[:200]} ({r.failure})")
            if r.resource_type in ("document", "xhr", "fetch", "script") else None)


def _resume(page, run, filler: Filler, log: Callable[[str], None]) -> None:
    """Apply your responses, re-read the bank, fill what it now covers, check again."""
    rep, answerer = filler.report, filler.answerer
    run_dir = getattr(run, "path", None)
    got = issuesmod.apply_responses(issuesmod.take_responses(run_dir), rep.issues, answerer.bank)
    filler.skipped |= set(got["skipped"])
    if answerer.bank is not None:
        answerer.bank.reload()
    answerer.questions = []
    rep.flagged, rep.questions, filler.blocked = [], [], []
    filler.fill(page)
    filler.check(page)


def _attend(page, run, filler: Filler, log: Callable[[str], None],
            on_submitted: Optional[Callable[[], None]]) -> None:
    """Keep the window open and watch it: a confirmation means you submitted; the dashboard or CLI may
    ask for a resume or an abandon; closing the window or pressing Enter in a terminal ends the run
    without a result. Every few seconds it notes what you changed in the window, the miss the tool did
    not know about, which apply-stats counts."""
    report = filler.report
    log("Window left open for your review. Press Submit in the browser when ready. The dashboard's "
        "Needs-you tab can Resume (after you answer questions) or Abandon; close the window "
        "(or press Enter here) to stop without a result.")
    stdin_done = None
    if sys.stdin and sys.stdin.isatty():
        import threading
        stdin_done = threading.Event()
        threading.Thread(target=lambda: (sys.stdin.readline(), stdin_done.set()), daemon=True).start()
    last_check = last_edits = 0.0
    while True:
        if stdin_done is not None and stdin_done.is_set():
            run.pause("window_closed", note="stopped from the terminal")
            return
        try:
            if page.is_closed():
                run.pause("window_closed", note="you closed the window")
                return
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            run.pause("window_closed", note="the browser went away")
            return
        if time.time() - last_check >= 2:
            last_check = time.time()
            conf = confirmation_text(page)
            if conf:
                report.submitted, report.confirmation = True, conf
                log(f"submitted: {conf}")
                run.screenshot(page, "confirmation")
                _record(run, report)
                run.set_state("submitted", confirmation=conf, note=conf)
                try:
                    learned = learn_from_submit(filler.answerer.bank, report.review, run.get("window_edits"),
                                                questions=filler.answerer.questions, job=filler.answerer.job)
                    if any(learned.values()):
                        log(f"answer bank learned from the submit: {learned}")
                        run.step(f"bank learned from submit: {learned}")
                except Exception as e:  # noqa: BLE001
                    log(f"could not update the answer bank from the submit: {e}")
                if on_submitted:
                    try:
                        on_submitted()
                    except Exception as e:  # noqa: BLE001
                        log(f"could not record the submission: {e}")
                page.wait_for_timeout(2500)
                return
        if time.time() - last_edits >= 5:
            last_edits = time.time()
            try:
                now = read_form(page)
                if now:     # the confirmation page has no form; keep the last snapshot
                    edits = verifymod.window_edits(now, filler.expected, filler.left_empty)
                    if edits != run.get("window_edits"):
                        run.data["window_edits"] = edits
                        run.save()
            except Exception:  # noqa: BLE001
                pass
        action = run.read_control()
        if action == "abandon":
            log("abandoned from the dashboard")
            run.set_state("abandoned", note="from the dashboard")
            return
        if action == "resume":
            log("resuming: applying your responses, re-reading the answer bank, filling what it now covers")
            run.set_state("running", note="resume")
            try:
                _resume(page, run, filler, log)
                run.screenshot(page, "resumed")
                log(report.summary())
                _record(run, report)
                run.pause(report.reason or "review_and_submit", note="; ".join(report.blockers) or "ready")
            except Exception as e:  # noqa: BLE001
                report.error = f"{type(e).__name__}: {e}"
                log(report.error)
                _record(run, report)
                run.pause("error", note=report.error, error=report.error)
