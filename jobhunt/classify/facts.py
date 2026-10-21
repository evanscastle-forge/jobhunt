"""The facts are what the model reads. They are not themselves a score.

notes/about.md holds one fact per bullet, in the first person:

    - I want to work on developer tools.
    - I do not want to be on call.
    - (auto) I tend to skip Acme postings (3 skipped, 0 saved).

That file is the input surface: the About page writes it, a scan reads it, and every rating the
model makes is stamped with `facts_sha` so a changed fact re-rates the corpus instead of leaving
stale numbers behind. AGENTS.md documents how a fact is supposed to reach a score.

WHAT USED TO BE HERE. Each fact carried a weight ([1] nice to have, [2] default, [3] absolute) and
an LLM-derived machine *check* -- a bag of regex terms plus a field to test them against -- and the
matches were summed into a points total on every posting. Three things were wrong with it. The
weights were a second scoring system beside the real weights in relevance.py, tuned separately and
agreeing with them by accident. The checks reduced a sentence to a term list, so "I do not want to
do release engineering" and "I have done release engineering" produced the same terms and the same
score. And the total saturated: a third of postings sat on the clamp, which is why tuning the
weights changed nothing. The model now reads the sentences, which is what sentences are for.

Old `[1]/[2]/[3]` prefixes still parse -- they are stripped and ignored -- so an about.md written
under the previous format loads without anyone editing it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ABOUT_FILE = "about.md"
SUPPRESSED_FILE = "auto_facts_suppressed.json"
AUTO_TAG = "(auto)"

_LEGACY_WEIGHT_RE = re.compile(r"^\[[123]\]\s*")

HEADER = ("# About\n\n"
          "One fact per bullet, in the first person. These are the only thing the model is told about\n"
          "you: it reads them against each posting before rating it, and the drafting hooks read them\n"
          "too. Write them as sentences rather than keywords -- \"I do not want to be on call\" and \"I\n"
          "have run an on-call rotation\" mean opposite things and a term list cannot tell them apart.\n"
          "Edit them on the dashboard's About page. Changing one re-rates every posting on the next scan.\n\n")


@dataclass
class Fact:
    text: str
    auto: bool = False

    @property
    def key(self) -> str:
        return fact_key(self.text)

    def to_dict(self) -> dict:
        return {"text": self.text, "auto": self.auto, "key": self.key}


def fact_key(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


# ----------------------------------------------------------------------------- parse / write

def parse_facts(md: str) -> list[Fact]:
    """One fact per '- ' bullet; indented continuation lines join the bullet above."""
    out: list[Fact] = []
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")):
            text = _LEGACY_WEIGHT_RE.sub("", s[2:].strip())
            auto = text.startswith(AUTO_TAG)
            if auto:
                text = text[len(AUTO_TAG):].strip()
            if text:
                out.append(Fact(text=text, auto=auto))
        elif s and out and line[:1].isspace() and not s.startswith("#"):
            out[-1].text += " " + s
    return out


def render_facts(facts: list[Fact]) -> str:
    lines = [HEADER]
    for f in facts:
        lines.append(f"- {AUTO_TAG} {f.text}\n" if f.auto else f"- {f.text}\n")
    return "".join(lines)


def clean_facts(facts: list) -> list[Fact]:
    """Normalise whitespace, drop empties and duplicates (keeping the first occurrence)."""
    out: list[Fact] = []
    seen: set[str] = set()
    for f in facts:
        if isinstance(f, dict):
            f = Fact(text=str(f.get("text") or ""), auto=bool(f.get("auto")))
        text = re.sub(r"\s+", " ", f.text or "").strip()
        if not text or fact_key(text) in seen:
            continue
        seen.add(fact_key(text))
        out.append(Fact(text=text, auto=bool(f.auto)))
    return out


def about_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / "notes" / ABOUT_FILE


def read_facts(profile_dir: Path) -> list[Fact]:
    p = about_path(profile_dir)
    if not p.exists():
        return []
    return parse_facts(p.read_text(encoding="utf-8"))


def write_facts(profile_dir: Path, facts: list) -> list[Fact]:
    clean = clean_facts(facts)
    p = about_path(profile_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_facts(clean), encoding="utf-8")
    return clean


def facts_sha(facts: list[Fact]) -> str:
    """Identifies the set of facts a rating was made under, so editing one re-rates the corpus.

    Order-independent on purpose: moving a bullet up the page does not change what the model was
    told, and re-reading 1,000 postings because someone reordered the list would be waste. Auto facts
    count, because the model reads them alongside the rest."""
    keys = sorted(fact_key(f.text) for f in facts if (f.text or "").strip())
    return hashlib.sha1("|".join(keys).encode("utf-8")).hexdigest()[:16]


def facts_diff(before: list[Fact], after: list[Fact]) -> dict:
    """{added, removed} as plain text, for the audit trail. An edit shows up as both."""
    b = {fact_key(f.text): f.text for f in before}
    a = {fact_key(f.text): f.text for f in after}
    return {"added": [a[k] for k in a if k not in b], "removed": [b[k] for k in b if k not in a]}


# ----------------------------------------------------------------------------- auto facts

def load_suppressed(data_dir: Path) -> set[str]:
    p = Path(data_dir) / SUPPRESSED_FILE
    if not p.exists():
        return set()
    try:
        return {str(x).lower() for x in json.loads(p.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def save_suppressed(data_dir: Path, names: set[str]) -> None:
    p = Path(data_dir) / SUPPRESSED_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(names)), encoding="utf-8")


def auto_facts(company_stats: dict, display_name: str = "", suppressed: set[str] = frozenset(),
               min_net: int = 2) -> list[Fact]:
    """company_stats: {name: {"pos": n, "neg": n}} from learn.build_model. One fact per company with
    a clear lean, unless the person deleted that fact before (suppressed). First person, like every
    other fact; display_name is kept for compatibility and unused."""
    out: list[Fact] = []
    for name, c in sorted(company_stats.items()):
        if name.lower() in suppressed:
            continue
        pos, neg = int(c.get("pos", 0)), int(c.get("neg", 0))
        net = pos - neg
        if net >= min_net:
            out.append(Fact(f"I tend to save {name} postings ({pos} saved or applied to, {neg} skipped).", True))
        elif -net >= min_net:
            out.append(Fact(f"I tend to skip {name} postings ({neg} skipped, {pos} saved).", True))
    return out


def auto_company(fact: Fact) -> Optional[str]:
    m = re.search(r"tends? to (?:save|skip) (.+?) postings", fact.text)
    return m.group(1).strip() if m else None


def merge_auto_facts(facts: list[Fact], new_auto: list[Fact]) -> tuple[list[Fact], bool]:
    """Replace the auto facts in `facts` with `new_auto`, keeping human facts and their order.
    Returns (merged, changed)."""
    human = [f for f in facts if not f.auto]
    old_auto = [f for f in facts if f.auto]
    merged = human + new_auto
    changed = [f.text for f in old_auto] != [f.text for f in new_auto]
    return merged, changed
