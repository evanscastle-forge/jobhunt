"""How far away a posting is, in miles.

Before this module, "near home" was a substring test against a hand-written city list, which
said Seattle, Washington and Charleston, West Virginia were commutable because the list contained
"washington" and "virginia". Distance is now measured properly:

    "Seattle, Washington"   -> (47.62, -122.35) -> 2,326 miles
    "New York, NY"          -> (40.66,  -73.94) ->   288 miles
    "Reston, VA"            -> (38.95,  -77.34) ->    93 miles

`data/us_places.csv` is the US Census Bureau's 2024 national place gazetteer joined to its 2024
population estimates (both public domain): state, place name, the place's internal point and the
population. 32,229 places, so essentially every city, town and CDP a posting can name. No network,
no API key, no geocoding service.

A location string is usually a list ("San Francisco, CA; New York, NY"), so `places_in` returns
every place it can resolve and callers take the nearest. A name that arrives WITHOUT its state has
to clear MIN_BARE_POP first, because the table's long tail of tiny places is a minefield: strip the
keyword out of "Remote-Friendly" and what is left matches Friendly, Maryland, population 94, close
enough to Washington to look like a commute. Strings the gazetteer cannot place at all
("Hampton Roads", "NOVA", "RVA") fall back to the profile's `commutable` vocabulary in filters.py.
"""
from __future__ import annotations

import csv
import functools
import math
import re
from pathlib import Path
from typing import Iterable, Optional

DATA = Path(__file__).resolve().parent / "data" / "us_places.csv"

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC", "dc": "DC",
    "puerto rico": "PR",
}
ABBREV = set(STATES.values())

# Where a state-only string ("Virginia", "Remote - Colorado") is taken to be. Population-weighted
# centres would be better; these are close enough to sort a state into a distance band.
STATE_POINTS = {
    "AL": (32.81, -86.79), "AK": (61.37, -152.40), "AZ": (33.73, -111.43), "AR": (34.97, -92.37),
    "CA": (36.12, -119.68), "CO": (39.06, -105.31), "CT": (41.60, -72.76), "DE": (39.32, -75.51),
    "FL": (27.77, -81.69), "GA": (33.04, -83.64), "HI": (21.09, -157.50), "ID": (44.24, -114.48),
    "IL": (40.35, -88.99), "IN": (39.85, -86.26), "IA": (42.01, -93.21), "KS": (38.53, -96.73),
    "KY": (37.67, -84.67), "LA": (31.17, -91.87), "ME": (44.69, -69.38), "MD": (39.06, -76.80),
    "MA": (42.23, -71.53), "MI": (43.33, -84.54), "MN": (45.69, -93.90), "MS": (32.74, -89.68),
    "MO": (38.46, -92.29), "MT": (46.92, -110.45), "NE": (41.13, -98.27), "NV": (38.31, -117.06),
    "NH": (43.45, -71.56), "NJ": (40.30, -74.52), "NM": (34.84, -106.25), "NY": (42.17, -74.95),
    "NC": (35.63, -79.81), "ND": (47.53, -99.78), "OH": (40.39, -82.76), "OK": (35.57, -96.93),
    "OR": (44.57, -122.07), "PA": (40.59, -77.21), "RI": (41.68, -71.51), "SC": (33.86, -80.95),
    "SD": (44.30, -99.44), "TN": (35.75, -86.69), "TX": (31.05, -97.56), "UT": (40.15, -111.86),
    "VT": (44.05, -72.71), "VA": (37.77, -78.17), "WA": (47.40, -121.49), "WV": (38.49, -80.95),
    "WI": (44.27, -89.62), "WY": (42.76, -107.30), "DC": (38.90, -77.03), "PR": (18.22, -66.59),
}

EARTH_MILES = 3958.7613
_CLEAN = re.compile(r"\b(remote|hybrid|onsite|on-site|in office|in-office|flexible|greater|metro|"
                    r"area|region|hq|headquarters|office|based|usa?|united states)\b", re.I)
_PAREN = re.compile(r"\([^)]*\)")
_SPLIT = re.compile(r"\s*[;|/,]\s*|\s+\bor\b\s+|\s*[-–—]\s+")


# A city named without its state has to earn it. The table carries every CDP in the country, so
# without a floor "Remote-Friendly" cleans down to "Friendly" and matches a 94-person town outside
# Washington -- which is exactly how 48 Bay Area postings once looked commutable from home.
MIN_BARE_POP = 50_000


@functools.lru_cache(maxsize=1)
def _rows() -> list[tuple[str, str, float, float, int]]:
    """(state, lowercase place, lat, lon, population), loaded once."""
    out: list[tuple[str, str, float, float, int]] = []
    try:
        with DATA.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out.append((row["state"], row["place"].lower(), float(row["lat"]), float(row["lon"]),
                                int(row.get("pop") or 0)))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return []
    return out


@functools.lru_cache(maxsize=1)
def _table() -> dict[tuple[str, str], tuple[float, float]]:
    """{(state, lowercase place): (lat, lon)}. Used when the string names its state, so any size."""
    return {(st, name): (lat, lon) for st, name, lat, lon, _p in _rows()}


@functools.lru_cache(maxsize=1)
def _by_name() -> dict[str, tuple[float, float]]:
    """{lowercase place: (lat, lon)} for strings that name no state: only places above
    MIN_BARE_POP, and the most populous one wins the name ("Columbus" is the Ohio one)."""
    best: dict[str, tuple[int, float, float]] = {}
    for _st, name, lat, lon, pop in _rows():
        if pop < MIN_BARE_POP:
            continue
        if name not in best or pop > best[name][0]:
            best[name] = (pop, lat, lon)
    return {name: (lat, lon) for name, (_p, lat, lon) in best.items()}


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle miles between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_MILES * math.asin(min(1.0, math.sqrt(h)))


def _state_of(token: str) -> Optional[str]:
    t = token.strip().strip(".").lower()
    if not t:
        return None
    if token.strip().upper() in ABBREV and len(token.strip()) == 2:
        return token.strip().upper()
    return STATES.get(t.replace(".", "")) or STATES.get(t)


def _tokens(text: str) -> list[str]:
    cleaned = _PAREN.sub(" ", text or "")
    out: list[str] = []
    for part in _SPLIT.split(cleaned):
        # Workday and a few boards write "United States-Virginia-Dulles" with no spaces, which the
        # ordinary separators miss. Split on the hyphens only when a piece is a US state, so
        # "Winston-Salem" and "Remote-Friendly" stay in one piece.
        if "-" in part:
            pieces = [x.strip() for x in part.split("-")]
            if len(pieces) > 1 and any(_state_of(x) for x in pieces):
                out.extend(x for x in pieces if x)
                continue
        out.append(part)
    return [x for x in (p.strip(" \t.-–—") for p in out) if x]


# Metro shorthands a national place gazetteer does not carry, plus the two city names that collide
# with state names. A posting saying "New York" means the city; one saying "Virginia" means the state.
ALIASES = {
    "new york city": (40.6627, -73.9387), "nyc": (40.6627, -73.9387), "new york": (40.6627, -73.9387),
    "manhattan": (40.7831, -73.9712), "brooklyn": (40.6782, -73.9442), "queens": (40.7282, -73.7949),
    "bay area": (37.7749, -122.4194), "sf bay area": (37.7749, -122.4194), "silicon valley": (37.3875, -122.0575),
    "hampton roads": (36.9230, -76.2446), "rva": (37.5314, -77.4760), "nova": (38.9214, -77.2276),
    "northern virginia": (38.9214, -77.2276), "dmv": (38.9042, -77.0165), "dc metro": (38.9042, -77.0165),
    "research triangle": (35.9940, -78.8986), "rtp": (35.9940, -78.8986), "triangle": (35.9940, -78.8986),
    "twin cities": (44.9778, -93.2650), "socal": (34.0522, -118.2437), "norcal": (37.7749, -122.4194),
}


def _clean_token(text: str) -> str:
    return _CLEAN.sub(" ", text or "").strip(" ,.-–—").strip()


def point_of(text: str) -> Optional[tuple[float, float]]:
    """The single best (lat, lon) for one already-split fragment, or None."""
    return next(iter(points_of(text)), None)


def points_of(text: str) -> list[tuple[float, float]]:
    """Every (lat, lon) a single fragment could mean. A bare city name that exists in several
    states yields one point per state, so callers can take the nearest."""
    bare = _clean_token(text)
    if not bare:
        return []
    key = bare.lower()
    if key in ALIASES:
        return [ALIASES[key]]
    st = _state_of(bare)
    if st:                                   # the fragment is just a state
        pt = STATE_POINTS.get(st)
        return [pt] if pt else []
    hit = _by_name().get(key)
    return [hit] if hit else []


def places_in(text: str, origin: Optional[tuple[float, float]] = None) -> list[tuple[float, float]]:
    """Every point named anywhere in a location string.

    A city fragment is held back until the next fragment is read, so "Columbus, Georgia" pins to the
    Columbus in Georgia rather than the ones in Ohio and Indiana. When a city name is
    ambiguous and no state follows it, every candidate is kept and `nearest_miles` takes the closest
    to `origin`. Call this only after ruling the string out as non-US: plenty of foreign cities share
    a name with a small American one (London, Kentucky)."""
    toks = _tokens(text)
    out: list[tuple[float, float]] = []
    pending: Optional[tuple[str, list[tuple[float, float]]]] = None   # a city awaiting its state

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            out.extend(pending[1])
            pending = None

    for tok in toks:
        key = _clean_token(tok).lower()
        if not key:
            continue
        st = _state_of(key)
        if st and key not in ALIASES:
            if pending is not None:                      # "<city>, <state>": pin the held city
                held, cand = pending
                pending = None
                pinned = _table().get((st, held))
                if pinned:
                    out.append(pinned)
                    continue
                if cand:
                    # The table spells it differently ("New York City, NY" is filed as New York),
                    # but the city itself resolved. A named place beats a state centroid.
                    out.extend(cand)
                    continue
                # Nothing but the state to go on, and the state is explicit, so trust it rather
                # than a same-named town elsewhere.
            pt = STATE_POINTS.get(st)
            if pt:
                out.append(pt)
            continue
        flush()
        # Held even when it resolves to nothing on its own: a small place fails the bare-name
        # prominence gate but is still perfectly findable once the next token names its state.
        pending = (key, points_of(tok))
    flush()
    if origin and out:
        out.sort(key=lambda p: haversine(origin, p))
    return out


def nearest_miles(text: str, origin: tuple[float, float]) -> Optional[float]:
    """Miles from `origin` to the closest place named in `text`, or None if nothing resolved."""
    pts = places_in(text, origin=origin)
    if not pts:
        return None
    return min(haversine(origin, p) for p in pts)


def origin_point(text: str) -> Optional[tuple[float, float]]:
    """Resolve a configured origin like "Columbus, Ohio" to a point."""
    pts = places_in(text)
    return pts[0] if pts else None


def resolvable(texts: Iterable[str]) -> int:
    """How many of these strings the gazetteer can place. Used by tests and diagnostics."""
    return sum(1 for t in texts if places_in(t))
