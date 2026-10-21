"""Measure normalized jobs against the search config. No scoring happens here.

Two stages, and the split is the point. `gate` answers whether a posting exists at all for this
profile -- company, title, location -- and `measure` reads what the posting states about itself:
travel, pay, clearance, seniority, age. What any of that is WORTH is relevance.py's business, and
keeping the two apart is what stops a weight quietly acting as an exclusion.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from . import gates
from .. import geo
from ..fetch.sources import Job

US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
             "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_US_STATE_RE = re.compile(r"(?:,\s*|\b)(" + "|".join(US_STATES) + r")\b")  # case-sensitive on purpose
_US_CITIES = [
    "san francisco", "new york", "nyc", "seattle", "austin", "boston", "chicago", "denver", "los angeles",
    "atlanta", "palo alto", "mountain view", "sunnyvale", "menlo park", "san jose", "oakland", "redwood city",
    "irvine", "costa mesa", "santa ana", "el segundo", "san diego", "portland", "salt lake", "phoenix", "dallas",
    "houston", "miami", "philadelphia", "pittsburgh", "raleigh", "durham", "charlotte", "nashville",
    "minneapolis", "detroit", "columbus", "st. louis", "colorado springs", "honolulu", "huntsville", "boulder",
    "bellevue", "kirkland", "tampa", "orlando", "omaha", "fort collins", "santa monica", "brooklyn",
    "bay area", "silicon valley",
]
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed|virtual)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
_TRAVEL_PCT_RE = re.compile(
    r"(?:travel[^.\n]{0,60}?(\d{1,3})\s*(?:%|percent))|(?:(\d{1,3})\s*(?:%|percent)[^.\n]{0,40}?travel)", re.I)
_TRAVEL_HEAVY_RE = re.compile(
    r"\b(frequent|extensive|significant|substantial|heavy|regular|weekly|constant)\b[^.\n]{0,25}\btravel"
    r"|\btravel\w*\b[^.\n]{0,15}\b(frequently|extensively|significantly|heavily|regularly|weekly|often|constantly)\b", re.I)
_TRAVEL_LIGHT_RE = re.compile(
    r"\b(occasional|minimal|limited|some|light|infrequent|little|periodic|rare)\b[^.\n]{0,25}\btravel"
    r"|\btravel\w*\b[^.\n]{0,15}\b(occasionally|minimally|rarely|infrequently|lightly|periodically)\b"
    r"|travel\s+(?:is|will be|would be)?\s*(?:minimal|limited|occasional|rare|infrequent|light)"
    r"|travel[^.\n]{0,40}?(?:once|one time|1x)[^.\n]{0,15}?(?:every|per|a)\s+(\d+)?[- ]?(\d+)?\s*(?:weeks|months|quarter)", re.I)
_TRAVEL_ANY_RE = re.compile(r"\btravel\b", re.I)
# How often a posting says someone must be in one of ITS OWN buildings. Deliberately narrow: this
# feeds a gate, and a gate deletes, so it fires only on a cadence the employer stated in words that
# cannot reasonably mean anything else. Travelling to customer sites is not office attendance and is
# matched by the _TRAVEL_ regexes above, not here.
_ONSITE_DAILY_RE = re.compile(
    r"\b(?:5|five)\s*days?\s*(?:a|per|each)\s*week\b[^.\n]{0,40}(?:office|on-?site|in[- ]person)"
    r"|(?:office|on-?site|in[- ]person)[^.\n]{0,40}\b(?:5|five)\s*days?\s*(?:a|per|each)\s*week"
    r"|\bfull[- ]time\s+(?:in[- ]the[- ])?(?:office|on-?site)"
    r"|\b(?:fully|100%|entirely)\s+(?:in[- ]office|on-?site|in[- ]person)"
    r"|\bthis (?:role|position) is (?:fully |100% )?(?:in[- ]office|on-?site)", re.I)
_ONSITE_WEEKLY_RE = re.compile(
    r"\b([2-4])\+?\s*(?:or more\s*)?days?\s*(?:a|per|each)\s*week\b[^.\n]{0,40}(?:office|on-?site|in[- ]person)"
    r"|(?:office|on-?site|in[- ]person)[^.\n]{0,40}\b([2-4])\+?\s*(?:or more\s*)?days?\s*(?:a|per|each)\s*week"
    r"|\bhybrid\b[^.\n]{0,40}\b([2-4])\+?\s*days?\s*(?:a|per|each)\s*week"
    r"|\bweekly\s+(?:in[- ]office|on-?site)\s+(?:presence|attendance|requirement)", re.I)
# How many days a week, when a posting says so. Read only for the location score (the reach gate keeps
# using the daily/weekly cadence above); a number is taken only with office wording within a phrase of
# it, so "5 days of PTO" or "25% travel" can never count as office days.
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "once": 1, "twice": 2}
_DAYS_PHRASE = (r"(?:(?P<n>[1-5]|one|two|three|four|five)\+?\s*(?:or more\s*)?(?:days?|x|times)\s*(?:a|per|each|/|every)\s*(?:week|wk)"
                r"|(?P<w>once|twice)\s*(?:a|per|each|every)\s*week)")
_OFFICE_WORDS = r"(?:office|on[- ]?site|in[- ]person|hybrid|in the building|at our (?:hq|headquarters|facility))"
_ONSITE_DAYS_RE = re.compile(_DAYS_PHRASE + r"[^.\n]{0,50}" + _OFFICE_WORDS + r"|" + _OFFICE_WORDS + r"[^.\n]{0,50}"
                             + _DAYS_PHRASE.replace("?P<n>", "?P<n2>").replace("?P<w>", "?P<w2>"), re.I)


def office_days_stated(text: str) -> Optional[int]:
    """Office days a week the posting states, 1..5, or None."""
    m = _ONSITE_DAYS_RE.search(text or "")
    if not m:
        return None
    raw = (m.group("n") or m.group("w") or m.group("n2") or m.group("w2") or "").lower()
    return int(raw) if raw.isdigit() else _NUM.get(raw)


_SALARY_RE = re.compile(r"\$\s?(\d{2,3})(?:,(\d{3}))?(?:\.\d+)?\s*([kK])?\b")
_CLEARANCE_RE = re.compile(r"\b(ts/?sci|top secret|secret clearance|security clearance|clearance|polygraph|public trust)\b", re.I)
_CLEARANCE_ACTIVE_RE = re.compile(r"\b(active|current|existing)\b[^.\n]{0,40}\b(ts/?sci|top secret|secret|clearance)\b"
                                  r"|\b(ts/?sci|top secret|secret|clearance)\b[^.\n]{0,20}\b(required|is required|must)\b", re.I)
_CLEARANCE_ELIG_RE = re.compile(r"\b(eligib\w*|ability to obtain|able to obtain|obtain and maintain|obtainable|willing to obtain)\b", re.I)
_SENIORITY_RE = {
    "principal": re.compile(r"\bprincipal\b", re.I),
    "staff": re.compile(r"\bstaff\b", re.I),
    "senior": re.compile(r"\b(senior|sr\.?)\b", re.I),
    "lead": re.compile(r"\blead\b", re.I),
}


def _window(text: str, start: int, end: int, pad: int) -> str:
    """A snippet around [start, end) widened to word boundaries and whitespace-normalized."""
    a, b = max(0, start - pad), min(len(text), end + pad)
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    while b < len(text) and not text[b].isspace():
        b += 1
    return re.sub(r"\s+", " ", text[a:b]).strip()


def _term_regex(term: str, prefix_ok: bool = False) -> re.Pattern:
    """Word-boundary regex for a config term. prefix_ok lets 'agent' match 'agents' / 'agentic'."""
    t = term.strip().lower()
    esc = re.escape(t)
    left = r"\b" if re.match(r"\w", t) else ""
    right = (r"\w*" if prefix_ok else r"\b") if re.search(r"\w$", t) else ""
    return re.compile(left + esc + right, re.I)


class Matcher:
    """Compiles the config's title and location vocabularies once and classifies jobs."""

    def __init__(self, cfg: dict):
        s = cfg.get("search", {})
        self.tier1 = [_term_regex(t, prefix_ok=True) for t in s.get("title_tier1", [])]
        self.tier2 = [_term_regex(t, prefix_ok=True) for t in s.get("title_tier2", [])]
        self.exclude = [(t, _term_regex(t)) for t in s.get("title_exclude", [])]
        self.require_title = bool(s.get("require_title_match", True))
        # Companies to drop outright, matched as a substring of the company name so one entry covers
        # "Acme", "Acme Services, LLC" and so on. Checked before anything else, because
        # a company you will not work for is not worth scoring, and the same employer reaches the
        # scan from several sources at once (its own board plus every aggregator that lists it).
        self.company_exclude = [t.lower() for t in s.get("company_exclude", []) if str(t).strip()]

        loc = cfg.get("location", {})
        self.mode = loc.get("mode", "remote_or_commutable")
        self.commutable_terms = [t.lower() for t in (loc.get("commutable") or loc.get("dc_area") or [])]
        self.non_us = [_term_regex(t) for t in loc.get("non_us", [])]
        self.us_markers = [t.lower() for t in loc.get("us_markers", [])]
        self.keep_us_onsite = bool(loc.get("keep_us_onsite", True))
        # Distance from a home point decides how OFTEN you could show up; `commutable` above is only
        # the fallback vocabulary for strings the gazetteer cannot place ("Hampton Roads", "NOVA").
        self.origin = geo.origin_point(str(loc.get("origin") or "")) if loc.get("origin") else None
        # The two thresholds that name a reach band. `daily_miles` is the drive you would make every
        # working day; `monthly_miles` the one you would make for a day on site, once or twice a
        # month. Beyond that and inside `radius_miles` is a trip you would take a few times a year.
        self.daily_miles = int(loc.get("daily_miles", 0))
        self.monthly_miles = int(loc.get("monthly_miles", 90))
        self.radius_miles = int(loc.get("radius_miles", 500))

        self.max_travel = int(cfg.get("travel", {}).get("max_pct_ok", 25))

        # Age bands: fresh under fresh_days, mid under stale_days, stale after. `mid_days` is the old
        # name for stale_days and still accepted. What age is WORTH is relevance._freshness, which
        # reads the same keys (plus peak_days) from [recency].
        r = dict(cfg.get("recency", {}) or {})
        if "mid_days" in r:                 # only a profile still says mid_days; config.toml does not
            r["stale_days"] = r["mid_days"]
        self.recency = {"peak_days": 7, "fresh_days": 14, "stale_days": 30}
        self.recency.update({k: int(v) for k, v in r.items() if k in self.recency})

    # ------------------------------------------------------------------ pieces

    def title_tier(self, title: str) -> tuple[int, Optional[str]]:
        t = title.lower()
        for term, rx in self.exclude:
            if rx.search(t):
                return 0, f"title excluded ({term})"
        for rx in self.tier1:
            if rx.search(t):
                return 1, None
        for rx in self.tier2:
            if rx.search(t):
                return 2, None
        return 0, "title matched no target keywords"

    _last_miles: Optional[float] = None      # distance of the segment location_fit chose

    @staticmethod
    def _coarse(seg: str) -> bool:
        """Is this segment only a state or a country, with no place inside it?

        "Virginia" resolves to the state centroid, which can be a few dozen miles from a Virginia origin and band it as a
        daily commute. It is not an address: it means somewhere in Virginia. A coarse match must
        never claim to be near, so callers floor it at the day-trip band."""
        low = re.sub(r"[^a-z ]+", " ", (seg or "").lower())
        low = re.sub(r"\b(united states|usa|us|u s a|remote|hybrid|onsite|on site)\b", " ", low)
        rest = re.sub(r"\s+", " ", low).strip()
        return bool(rest) and (rest.upper() in geo.STATE_POINTS or rest in geo.STATES)

    def miles_to(self, seg: str) -> Optional[float]:
        """Miles from the configured origin to the nearest place named in `seg`. Falls back to the
        `commutable` vocabulary for strings the gazetteer cannot place, and returns None when the
        segment names nowhere at all ("Remote", "United States")."""
        if not self.origin:
            return None
        d = geo.nearest_miles(seg, self.origin)
        if d is None and any(t in f" {seg.lower()} " for t in self.commutable_terms):
            # A regional shorthand the gazetteer does not carry. It used to return 0.0 -- "at my
            # front door" -- which put eight real offices 95 to 145 miles away (Tysons, Chantilly,
            # Herndon, Reston, Arlington, Annapolis Junction) in the nearest band, scoring them above
            # a correctly-placed DC office. A name we cannot resolve means "somewhere in the region",
            # so it bands as the far edge of a day trip and is never mistaken for home.
            return float(self.monthly_miles)
        return d

    def _segment_fit(self, seg: str) -> tuple[str, Optional[float]]:
        """(fit, miles from origin). Distance decides how near a US posting is; remote beats
        everything except an actually-local office."""
        s = seg.strip()
        if not s:
            return "unknown", None
        low = s.lower()
        deny = any(rx.search(low) for rx in self.non_us)
        us_marker = any(m in f" {low} " for m in self.us_markers)
        remote = bool(_REMOTE_RE.search(low))
        if deny and not us_marker:
            # Checked before the gazetteer on purpose: foreign cities share names with small
            # American ones, and London, Kentucky is not what "London, UK" means.
            return "non_us", None
        miles = self.miles_to(s)
        coarse = self._coarse(s)
        if miles is not None and self.daily_miles and miles <= self.daily_miles and not coarse:
            return "daily", miles
        if coarse and miles is not None and miles <= self.monthly_miles:
            return "monthly", max(miles, float(self.daily_miles) + 1.0)
        if miles is not None and miles <= self.monthly_miles:
            return "monthly", miles
        if remote:
            return ("remote" if (us_marker or miles is not None) else "remote_unspecified"), miles
        if miles is not None:
            if miles <= self.radius_miles:
                return "quarterly", miles
            return "relocate", miles
        if us_marker or bool(_US_STATE_RE.search(s)) or any(c in low for c in _US_CITIES):
            return "relocate", None
        return "unknown", None

    def location_fit(self, job: Job) -> tuple[str, list[str]]:
        reasons: list[str] = []
        segs = [x for x in re.split(r"\s*[;|/]\s*|\s+\bor\b\s+", job.location or "") if x.strip()]
        pairs = [self._segment_fit(s) for s in segs] or [("unknown", None)]
        # Remote sits above every office that is not at home, and level with one that is.
        order = ["daily", "remote", "monthly", "remote_unspecified", "quarterly",
                 "relocate", "unknown", "non_us"]
        best, miles = min(pairs, key=lambda p: order.index(p[0]))
        # A posting whose only named places are foreign is foreign, even when it also says "remote".
        # "Brampton, Canada; Flexible / Remote" is a Canadian office advertising flexibility, not a
        # job open from Virginia. Only an actually US-anchored segment rescues it.
        us_anchored = ("daily", "monthly", "quarterly", "relocate", "remote")
        if any(f == "non_us" for f, _m in pairs) and not any(f in us_anchored for f, _m in pairs):
            best, miles = "non_us", None
        self._last_miles = miles
        if job.remote_signal == "remote" and best != "non_us":
            # Structured remote flag (Ashby isRemote, Lever workplaceType, Greenhouse metadata) wins
            # over an office address, including a near one: nine postings read "Remote (Any State);
            # 507 RESTON VA" and the like, and calling those a daily commute is simply false.
            # "Remote - India" stays non_us because the segment rule already denied it.
            if best not in ("remote", "remote_unspecified"):
                reasons.append("posting flagged remote")
            best = "remote"
        if best == "non_us" and self.mode != "any":
            reasons.append("location outside the US")
        return best, reasons

    # The most often you could show up, given only the distance. The band IS the answer, and a band
    # with no office (remote, or a place we could not resolve) maps to None: there is nothing to
    # reach, so the question does not arise and the gate stays out of it.
    REACH = {"daily": "daily", "monthly": "monthly", "quarterly": "quarterly",
             "relocate": "quarterly"}
    # Ordered from least demanding to most, so a plain comparison decides feasibility.
    CADENCE = ["quarterly", "monthly", "weekly", "daily"]

    def onsite_need(self, text: str) -> dict:
        """How often the posting says someone must be in one of the employer's own buildings.

        {"need": "daily" | "weekly" | None, "quote": str}. None means the posting did not say, which
        is the common case -- only about 110 of 960 postings state a cadence at all. It is read from
        the FULL description rather than the truncated copy the model sees, because 898 of 960
        descriptions are longer than that cut."""
        days = office_days_stated(text)
        for need, rx in (("daily", _ONSITE_DAILY_RE), ("weekly", _ONSITE_WEEKLY_RE)):
            m = rx.search(text)
            if m:
                lo = max(0, m.start() - 60)
                return {"need": need, "days": days,
                        "quote": re.sub(r"\s+", " ", text[lo:m.end() + 60]).strip()[:200]}
        if days is not None:          # a number the cadence patterns do not cover ("1 day a week in office")
            m = _ONSITE_DAYS_RE.search(text)
            lo = max(0, m.start() - 60)
            return {"need": None, "days": days, "quote": re.sub(r"\s+", " ", text[lo:m.end() + 60]).strip()[:200]}
        return {"need": None, "days": None, "quote": None}

    def travel(self, text: str) -> dict:
        pcts = []
        for m in _TRAVEL_PCT_RE.finditer(text):
            for g in m.groups():
                if g:
                    try:
                        v = int(g)
                        if 0 < v <= 100:
                            pcts.append(v)
                    except ValueError:
                        pass
        quote = None
        m = _TRAVEL_PCT_RE.search(text) or _TRAVEL_HEAVY_RE.search(text) or _TRAVEL_LIGHT_RE.search(text) or _TRAVEL_ANY_RE.search(text)
        if m:
            quote = _window(text, m.start(), m.end(), 60)
        if pcts:
            pct = max(pcts)
            level = "light" if pct <= self.max_travel else ("moderate" if pct <= 50 else "heavy")
            return {"pct": pct, "level": level, "quote": quote}
        if _TRAVEL_HEAVY_RE.search(text):
            return {"pct": None, "level": "heavy", "quote": quote}
        if _TRAVEL_LIGHT_RE.search(text):
            return {"pct": None, "level": "light", "quote": quote}
        if _TRAVEL_ANY_RE.search(text):
            return {"pct": None, "level": "mentioned", "quote": quote}
        return {"pct": None, "level": "none", "quote": None}

    @staticmethod
    def salary(text: str, salary_text: Optional[str]) -> dict:
        vals = []
        for src in (salary_text or "", text):
            for m in _SALARY_RE.finditer(src):
                whole, thousands, k = m.groups()
                if thousands:
                    v = int(whole) * 1000 + int(thousands)
                elif k:
                    v = int(whole) * 1000
                else:
                    continue
                if 50_000 <= v <= 1_500_000:
                    vals.append(v)
        if not vals:
            return {"min": None, "max": None, "text": salary_text}
        return {"min": min(vals), "max": max(vals), "text": salary_text}

    @staticmethod
    def clearance(text: str) -> dict:
        m = _CLEARANCE_RE.search(text)
        if not m:
            return {"mentioned": False, "level": None, "active_required": False, "eligibility_ok": False, "quote": None}
        quote = _window(text, m.start(), m.end(), 80)
        low = text.lower()
        level = "TS/SCI" if re.search(r"ts/?sci", low) else ("Top Secret" if "top secret" in low else (
            "Secret" if "secret" in low else ("Public Trust" if "public trust" in low else "unspecified")))
        elig = bool(_CLEARANCE_ELIG_RE.search(text))
        active = bool(_CLEARANCE_ACTIVE_RE.search(text)) and not elig
        return {"mentioned": True, "level": level, "active_required": active, "eligibility_ok": elig, "quote": quote}

    def recency_of(self, posted_at: Optional[str], first_seen: Optional[str] = None,
                   now: Optional[datetime] = None) -> dict:
        """How old a posting is: fresh / mid / stale. A measurement, not a penalty -- what age is
        worth is the `freshness` weight in relevance.py. Undated postings age from when we first saw
        them, which is the only date we have for them."""
        now = now or datetime.now(timezone.utc)
        ts = posted_at or first_seen
        if not ts:
            return {"level": "unknown", "age_days": None, "dated": False}
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return {"level": "unknown", "age_days": None, "dated": False}
        age = max(0, (now - dt).days)
        if age < self.recency["fresh_days"]:
            return {"level": "fresh", "age_days": age, "dated": bool(posted_at)}
        if age < self.recency["stale_days"]:
            return {"level": "mid", "age_days": age, "dated": bool(posted_at)}
        return {"level": "stale", "age_days": age, "dated": bool(posted_at)}

    def seniority(self, title: str) -> Optional[str]:
        """Which seniority word the title uses, or None. `seniority_fit` is what decides whether
        that suits the reader, and the model rates it from the whole posting."""
        for name, rx in _SENIORITY_RE.items():
            if rx.search(title):
                return name
        return None

    # ------------------------------------------------------------------ the two stages

    def gate(self, job: Job) -> dict:
        """STAGE: does this posting exist at all for this profile?

        Company, title and location, in that order, cheapest first. Location has to be measured to be
        gated on, so its measurement comes back here rather than in `measure`. Everything else waits
        until a posting has earned it -- there is no point reading the salary out of a posting that
        is about to be dropped."""
        out: dict = {"keep": True, "drop_reason": None, "reasons": []}
        drop = gates.company_gate(job.company, self.company_exclude)
        if drop:
            return {**out, "keep": False, "drop_reason": drop[0], "reasons": [drop[1]]}

        tier, why = self.title_tier(job.title)
        out["title_tier"] = tier
        drop = gates.title_gate(tier, why, self.require_title)
        if drop:
            return {**out, "keep": False, "drop_reason": drop[0], "reasons": [drop[1]]}

        fit, loc_reasons = self.location_fit(job)
        miles = self._last_miles
        out["location_fit"] = fit
        out["location_miles"] = None if miles is None else int(round(miles))
        out["reasons"] = list(loc_reasons)
        drop = gates.location_gate(fit, self.mode, self.keep_us_onsite, self.radius_miles, miles=miles)
        if drop:
            return {**out, "keep": False, "drop_reason": drop[0],
                    "reasons": out["reasons"] + [drop[1]]}

        # Can you actually be there as often as they need? A job you cannot turn up to is not a job
        # that should rank lower -- it is one you cannot do, which makes it a gate.
        reach = self.REACH.get(fit)
        need = self.onsite_need(f"{job.title}\n{job.description}")
        if job.remote_signal == "onsite" and not need["need"]:
            need = {"need": "daily", "quote": "the board lists this role as onsite"}
        out["reach"] = reach
        out["onsite_need"] = need["need"]
        out["onsite_days"] = need.get("days")
        out["onsite_quote"] = need["quote"]
        drop = gates.reach_gate(reach, need["need"], self.CADENCE)
        if drop:
            return {**out, "keep": False, "drop_reason": drop[0],
                    "reasons": out["reasons"] + [drop[1]]}
        return out

    def measure(self, job: Job, first_seen: Optional[str] = None,
                now: Optional[datetime] = None) -> dict:
        """STAGE: read the facts a posting states about itself.

        Pure measurement, no judgement: how much travel, what it pays, what clearance it needs, how
        senior it is, how old it is. What any of that is worth is relevance.py's business."""
        text = f"{job.title}\n{job.description}"
        return {"travel": self.travel(text), "salary": self.salary(text, job.salary_text),
                "clearance": self.clearance(text), "seniority": self.seniority(job.title),
                "recency": self.recency_of(job.posted_at, first_seen, now=now)}

    def classify(self, job: Job, first_seen: Optional[str] = None, now: Optional[datetime] = None) -> dict:
        """gate, then measure what survives. Kept as one call for the places that want both."""
        out = self.gate(job)
        if not out["keep"]:
            return out
        out.update(self.measure(job, first_seen, now))
        return out
