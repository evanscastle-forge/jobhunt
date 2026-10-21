"""How stored postings are presented: which tab a posting belongs to.

Kept apart from store.py on purpose. Store answers what is on disk; this answers what the
reader is shown, which is a different question and changes for different reasons."""
from __future__ import annotations

from typing import Optional

from ..classify import relevance as rel
from ..store import STATUSES

# One list. Distant onsite roles used to get a tab of their own, because nothing knew how often a
# job actually needed you and a 300-mile office would otherwise have crowded the shortlist. That is
# a gate now: anything that survives is a job you could take, so it competes on merit like the rest,
# and the frequency column says what kind of trip it would be.
TABS = list(STATUSES)


def rows_for(store, include_inactive: bool = False, search_cfg: Optional[dict] = None) -> list[dict]:
    """Stored postings as the reader should see them: joined to your decisions, ordered by score.

    Three presentation rules live here rather than in the store, because they are about what is worth
    showing rather than what is on disk: a closed posting you never touched disappears, a location
    variant hides behind the posting that carries it, and the best score comes first."""
    store.refresh()
    rows = []
    for jid, job in store.jobs["jobs"].items():
        if not include_inactive and not job.get("active", True):
            if store.get_state(jid, refresh=False).get("status") in ("new", "skip"):
                continue
        row = dict(job)
        row["state"] = shown_state(job, store.get_state(jid, refresh=False))
        if row["state"] is None:
            continue
        if job.get("duplicate_of") and row["state"].get("status", "new") == "new" and not include_inactive:
            continue
        rows.append(row)
    # Order by whatever the engine in charge actually ranks on. Sorting by the old score while
    # showing grades would put a B- above a B+ and make the page look broken.
    rows.sort(key=lambda r: (r.get("relevance") is None, -(r.get("relevance") or 0),
                             r.get("posted_at") or ""))
    return rows


def shown_state(job: dict, st: dict) -> Optional[dict]:
    """A decision archived by a reset is not shown as it was: the posting is hidden until a scan sees it
    again after the reset, and then it is simply new. The stored decision is unchanged (it still teaches
    the learned model and counts for retune)."""
    stamp = st.get("archived")
    if not stamp:
        return st
    if (job.get("last_seen") or "") > stamp:
        return {**st, "status": "new", "archived_status": st.get("status")}
    return None


def below_bar(rec: dict, search_cfg: dict) -> bool:
    """Is this posting beneath the floor?

    A grade, not a number. The relevance total has no units -- its scale moves whenever a weight
    moves -- so a numeric floor would mean something different after every tuning pass. An unrated
    posting has no grade and is always below the bar: it has not earned a place, and imputing one
    would rank it against postings the model has actually read."""
    return not rel.at_least(rec.get("grade"), rel.min_grade(search_cfg))


def floor_label(search_cfg: dict) -> str:
    """What the floor is, in the units the reader is actually shown."""
    return rel.min_grade(search_cfg)


def decision_context(store, job_id: str, search_cfg: dict) -> dict:
    """What the reader was looking at when they decided, so a later comparison is not circular.

    A saved posting proves nothing about a ranking if that ranking is the one that put it in front of
    the reader. Recording the weights in force, and the posting's place in the list at that moment,
    is what turns "they saved it" into evidence a later tuning pass can be tested against."""
    rec = store.jobs["jobs"].get(job_id) or {}
    weights, _notes = rel.load_weights(search_cfg)
    rows = [r for r in store.jobs["jobs"].values()
            if r.get("active", True) and not r.get("duplicate_of")
            and store.get_state(r["id"], refresh=False).get("status", "new") == "new"]
    ordered = sorted((r for r in rows if r.get("relevance") is not None),
                     key=lambda r: -r["relevance"])
    rank = next((i for i, r in enumerate(ordered, 1) if r.get("id") == job_id), None)
    return {"decided_under": rel.weights_sha(weights), "decided_rank": rank,
            "decided_of": len(ordered), "decided_grade": rec.get("grade"),
            "decided_relevance": rec.get("relevance")}
