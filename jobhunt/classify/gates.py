"""The keep/drop decisions, in one place.

A gate answers "should this posting exist at all"; scoring answers "how high should it sit". Keeping
them apart matters more than it sounds: a weight can then never quietly act as an exclusion, and an
exclusion can never be argued away by a good score somewhere else.

Each gate is a pure function returning `None` to keep, or `(drop_reason, line)` to drop, where
`drop_reason` is stored on the record and `line` is what the dashboard shows. They take plain values
rather than a Matcher so they can be tested on their own.
"""
from __future__ import annotations

from typing import Optional

Drop = Optional[tuple[str, str]]


def company_gate(company: Optional[str], excluded: list[str]) -> Drop:
    """Employers you have ruled out, matched as a substring so one entry covers every legal variant.

    Checked before anything else: a company you will not work for is not worth measuring, and the
    same employer reaches a scan from its own board and from several aggregators at once."""
    low = (company or "").lower()
    hit = next((t for t in excluded if t in low), None)
    return (f"excluded company ({hit})", f"dropped: {company} is on the company_exclude list") if hit else None


def title_gate(tier: int, why: Optional[str], require_title_match: bool) -> Drop:
    """No tier-1 or tier-2 term, or an excluded term. `title_tier` decides; this only acts on it."""
    if tier == 0 and require_title_match:
        return (why or "title matched no target keywords", why or "title matched no target keywords")
    return None


def location_gate(fit: str, mode: str, keep_us_onsite: bool, radius_miles: int,
                  miles: Optional[float] = None) -> Drop:
    """Outside the US, or an office too far away to consider at all.

    Under `remote_or_commutable`, inside `radius_miles` nothing is dropped for distance alone: those
    postings are kept and shown in their own tab, because "too far to visit weekly" is not the same as
    "not worth knowing about". Whether you could actually turn up as often as the job needs is
    `reach_gate`'s question.

    Under `remote_or_daily` the only offices that exist are the ones inside `daily_miles`: remote,
    or the home metro. Any office further away is one you would have to travel to, however rarely,
    and goes. `unknown` is still kept: silence never drops anything."""
    if fit == "non_us" and mode != "any":
        return ("non-US location", "dropped: non-US")
    onsite = ("relocate", "quarterly", "monthly", "daily")
    if mode == "remote_only" and fit in onsite and not keep_us_onsite:
        return ("not remote", "dropped: not remote")
    if mode == "remote_or_daily" and fit in ("relocate", "quarterly", "monthly"):
        where = f"an office {int(round(miles))} miles away" if miles is not None else "an office"
        return ("office beyond a daily drive", f"dropped: {where} you would have to visit")
    if fit == "relocate" and not keep_us_onsite:
        return (f"onsite beyond {radius_miles} miles", f"dropped: onsite beyond {radius_miles} miles")
    return None


SAY = {"daily": "every day", "weekly": "several days a week",
       "monthly": "a day or so a month", "quarterly": "a few times a year"}


def reach_gate(reach: Optional[str], need: Optional[str], cadence: list[str]) -> Drop:
    """Could you be there as often as they require?

    `reach` is the most often you could show up, which distance alone decides; `None` means the role
    has no office to reach and the question does not arise. `need` is how often the posting says you
    must be there, which only the employer can state -- and which is unknown for most postings.
    Unknown never drops anything: a gate deletes, so it fires only on a cadence actually stated.

    This is a gate and not a weight on purpose. A role that needs you in a Washington DC office three
    days a week is not a worse job from three hours away, it is one you cannot hold, and no amount of being
    excellent in other respects changes that. Weights decide order among jobs you could take."""
    if not reach or not need or reach not in cadence or need not in cadence:
        return None
    if cadence.index(need) <= cadence.index(reach):
        return None
    return (f"needs you on site {SAY.get(need, need)}",
            f"dropped: needs you on site {SAY.get(need, need)}, "
            f"and you could manage {SAY.get(reach, reach)}")
