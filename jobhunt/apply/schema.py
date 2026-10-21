"""What a form asks, and what we would answer. Pure data; no browser, no bank.

A Field is one logical question on the page (a radio group or a checkbox group is one Field, not one
per control). An Answer is what the answer stage proposes for a Field, with a tier that says how far
it may be trusted:

    certain   from a profile field or an approved bank entry, for a question that field was written
              for. Typed into the form without asking.
    inferred  a near match: an option picked by a loose rule, a bank entry matched fuzzily, a profile
              value applied to a question it was not written for. Typed and listed for your review,
              unless the question is sensitive.
    agent     the applicant agent's grounded answer (persona.py). Typed and listed for your review,
              unless the question is sensitive.
    drafted   a model draft kept as a suggestion only.
    unknown   nothing to go on. Asked.

See docs/apply-design.md, "Certainty is defined, not felt".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

TIERS = ("certain", "inferred", "agent", "drafted", "unknown")
KINDS = ("text", "textarea", "select", "combobox", "radio", "checkbox", "checkgroup", "file")

FILE_PREFIX = "__file__:"
CHECK = "__check__"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def clean_label(label: str) -> str:
    """The label as a question: lowercased, required markers and extra whitespace removed."""
    lab = re.sub(r"[\*✱]+|\(required\)|\(optional\)", " ", norm(label))
    return re.sub(r"\s+", " ", lab).strip()


@dataclass
class Field:
    label: str
    kind: str
    options: list = field(default_factory=list)
    required: bool = False
    visible: bool = True
    value: Any = ""                 # what the page holds now (text, option text, list for checkgroups)
    frame: int = 0                  # index into page.frames at read time
    raw: dict = field(default_factory=dict)   # the scan record: id, name, type, dom_index, option_indexes
    context: str = ""               # for a follow-up ("If yes, please explain"): the question it follows, and its answer
    help: str = ""                  # text around the question that is not its label (option definitions, instructions)

    @property
    def ident(self) -> str:
        """Stable across re-reads of the same page (dom_index is not: conditional fields shift it)."""
        r = self.raw
        return f"{self.frame}:{r.get('id') or ''}:{r.get('name') or ''}:{r.get('type') or ''}:{self.label}"

    def has_value(self) -> bool:
        v = self.value
        if isinstance(v, list):
            return bool(v)
        if isinstance(v, bool):
            return v
        return bool(str(v or "").strip())


@dataclass
class Answer:
    value: Any = None               # str, list[str] (checkgroup), CHECK, or FILE_PREFIX + path
    tier: str = "unknown"
    source: str = ""                # "profile:identity.email", "bank:<key>", "rule:consent", "model"
    why: Optional[str] = None       # shown to you when the field is not filled, or filled with a caveat
    skip: bool = False              # deliberately left alone (not a question: a hint, a preferred name)
    ask: bool = False               # a real question to put to you (and record in the answer bank)

    @property
    def certain(self) -> bool:
        return self.tier == "certain" and self.value not in (None, "", [])


def _yes_no(options: list[str], want_yes: bool) -> Optional[str]:
    for o in options:
        lo = norm(o)
        if want_yes and re.match(r"^(yes|y)\b", lo):
            return o
        if not want_yes and re.match(r"^(no|n)\b", lo) and not re.match(r"^n\s*/\s*a\b", lo):
            return o
    return None


def _extra(option: str, value: str) -> str:
    """An option that says more than the answer ("No - I am interested in obtaining one") makes a claim
    the answer did not: only a weak match. A few words of restatement ("Yes, I am") are fine."""
    rest = re.findall(r"\w+", norm(option)[len(norm(value)):]) if norm(option).startswith(norm(value)[:1]) else []
    return "strong" if len(rest) <= 3 else "weak"


def match_option(options: list[str], value: str) -> tuple[Optional[str], str]:
    """Best option for a desired value, and how strong the match is: "strong" or "weak".

    strong: equal; the option begins with the value as whole words ("Yes" -> "Yes, I am");
            a yes/no answer mapped onto a yes/no option.
    weak:   the value appears as whole words inside the option, or the option's words are all in
            the value ("Company careers page" -> "Careers page").
    Whole words matter: "No" must never pick "Not applicable" or "None of the above".
    """
    if not options:
        return None, ""
    v = norm(value)
    if not v:
        return None, ""
    for o in options:
        if norm(o) == v:
            return o, "strong"
    word = re.compile(r"(?<!\w)" + re.escape(v) + r"(?!\w)")
    for o in options:
        if norm(o).startswith(v) and word.match(norm(o)):
            return o, _extra(o, v)
    if v in ("yes", "true"):
        o = _yes_no(options, True)
        return (o, _extra(o, "yes")) if o else (None, "")
    if v in ("no", "false"):
        o = _yes_no(options, False)
        return (o, _extra(o, "no")) if o else (None, "")
    m = re.match(r"^(yes|no)\b", v)   # "Yes, occasional travel is fine" -> the Yes option
    if m:
        o = _yes_no(options, m.group(1) == "yes")
        if o:
            return o, _extra(o, m.group(1))
    for o in options:
        if word.search(norm(o)):
            return o, "weak"
    vw = set(re.findall(r"\w+", v))
    for o in options:
        ow = set(re.findall(r"\w+", norm(o)))
        if ow and ow <= vw and len(ow) >= 2:
            return o, "weak"
    return None, ""


def pick_option(options: list[str], value: str) -> Optional[str]:
    return match_option(options, value)[0]
