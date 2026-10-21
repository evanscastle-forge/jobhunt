"""What your actions teach the ranking.

Statuses:  saved / applied / done -> positive,  skip -> negative,  new -> neutral.

Companies you keep saving or skipping become auto facts on the About page ("I tend to skip
Acme postings ..."), which reach the score the way every other fact does: by being part of what
the model reads before it rates a posting.

What used to live here and does not any more is the additive score itself -- the fit bands, the
support term, the near-duplicate penalty and the 0-100 arithmetic. relevance.py replaced all of it.
`dup_key` survives because collapse_variants still needs to know when two postings are the same
role, which is a question about identity and not about scoring."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone


POSITIVE = {"saved", "applied", "done"}
NEGATIVE = {"skip"}
SKIP_REASONS = ["role", "company", "location", "pay", "travel", "other"]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)           # drop parenthetical locations / req ids
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Legal-form suffixes that say nothing about who a company is. Ordered longest-first so "co ltd"
# is stripped whole rather than leaving a stray "ltd".
_SUFFIXES = ("co ltd", "pty ltd", "incorporated", "corporation", "limited", "holdings", "group",
             "gmbh", "s a s", "b v", "n v", "plc", "llc", "llp", "ltd", "inc", "corp", "lp", "ag",
             "sa", "bv", "nv", "co")


def canonical_name(company: str) -> str:
    """One company, one key.

    The feeds spell the same employer half a dozen ways -- "Scale AI", "Scale AI, Inc.",
    "Samsara Inc." -- and every one of them used to be a separate company for affinity counts, auto
    facts and duplicate detection. Casefold, drop punctuation, drop the legal form, collapse spaces.

    Deliberately conservative: it does not try to guess that "Meta" and "Facebook" are the same, and
    it never strips a suffix that is the whole name, so a company actually called "Co" survives."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (company or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    changed = True
    while changed and t:
        changed = False
        for suf in _SUFFIXES:
            if t.endswith(" " + suf):
                t = t[: -len(suf) - 1].strip()
                changed = True
                break
    return t


def dup_key(company: str, title: str) -> str:
    return f"{canonical_name(company)}|{normalize_title(title)}"


def build_model(jobs: dict[str, dict], state: dict[str, dict]) -> dict:
    """Company affinity from your own decisions. jobs: id -> stored posting; state: id -> {status, ...}.

    It also used to index every posting you skipped or applied to by `dup_key`, so a near-duplicate
    could be docked 20 points. That penalty went with the additive score, and nothing read the
    indexes afterwards, so they are not built any more."""
    comp_pos: Counter = Counter()
    comp_neg: Counter = Counter()
    n_pos = n_neg = 0
    for jid, st in state.items():
        job = jobs.get(jid)
        if not job:
            continue
        status = st.get("status", "new")
        company = job.get("company") or ""
        if status in POSITIVE:
            n_pos += 1
            comp_pos[company] += 1
        elif status in NEGATIVE:
            n_neg += 1
            comp_neg[company] += 1
    companies = {c: {"pos": comp_pos[c], "neg": comp_neg[c]} for c in set(comp_pos) | set(comp_neg)}
    return {"built_at": _now(), "n_pos": n_pos, "n_neg": n_neg, "companies": companies}
