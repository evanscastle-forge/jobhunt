"""Find companies worth watching, rather than fetching the ones you already named.

The scan is company-first: you give it a board token and it fetches that board. For Greenhouse,
Lever and Ashby the company name is an INPUT, never an output, so the tool could only ever find work
at employers someone had typed in. This is the inverse.

    harvest   every source that names an employer  ->  company names
    resolve   name -> candidate ATS tokens (slug variants)
    probe     token -> is there a live board?
    assess    does that board carry work you want and could reach?
    promote   yes -> companies.discovered.toml; no -> parked, rechecked later

Nothing here scrapes. Token guessing is cheap and self-verifying: a Greenhouse board returns
`company_name` on every posting, so a wrong guess identifies itself instead of silently mislabelling
a board. Probing `scaleai` answers "Scale AI".

The registry at data/companies.json is the durable memory of what has been checked and what was
tried, which is what stops next month's run re-guessing this month's dead tokens.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..classify.learn import canonical_name
from ..jsonfile import _read_json, _write_json
from . import sources

REGISTRY_FILE = "companies.json"
DISCOVERED_FILE = "companies.discovered.toml"
SCHEMA = 1

# new       harvested, never probed
# promoted  live board with work you want; in the daily scan
# parked    live board, nothing relevant yet -- recheck later, it costs one request
# dead      nothing answered on any token we tried
# excluded  on company_exclude; never probed
STATES = ("new", "promoted", "parked", "dead", "excluded")

PROBES: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}",
    "breezy": "https://{token}.breezy.hr/json",
    "recruitee": "https://{token}.recruitee.com/api/offers",
    "bamboohr": "https://{token}.bamboohr.com/careers/list",
    "rippling": "https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs",
}

# Words that never help identify a board token.
_NOISE = ("the", "inc", "llc", "ltd", "corp", "company", "co", "group", "holdings", "technologies",
          "technology", "labs", "systems", "solutions", "software", "digital")


def _now() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ----------------------------------------------------------------------------- registry

def load(data_dir: Path) -> dict:
    """{canonical name: record}. Empty when the file is missing or written by another schema."""
    data = _read_json(Path(data_dir) / REGISTRY_FILE, {})
    if int(data.get("schema") or 0) != SCHEMA:
        return {}
    return data.get("companies") or {}


def save(data_dir: Path, companies: dict) -> None:
    _write_json(Path(data_dir) / REGISTRY_FILE,
                {"schema": SCHEMA, "updated_at": _now(), "companies": companies}, compact=True)


def due(rec: dict, today: Optional[str] = None) -> bool:
    """Is this company worth spending requests on right now?

    A company already in the daily scan is not. One that has never been probed is. One that answered
    but had nothing relevant is, once its recheck date passes -- a parked board costs a single
    request and catches the day it posts the role you wanted."""
    if rec.get("state") in ("promoted", "excluded"):
        return False
    if rec.get("state") == "new":
        return True
    return (rec.get("recheck_after") or "") <= (today or _now())


# ----------------------------------------------------------------------------- harvest

def harvest(names: Iterable[tuple[str, str, int]], registry: dict) -> tuple[dict, int]:
    """Fold (company name, where it was seen, relevant postings) into the registry.

    Returns (registry, how many are new). Keyed on canonical_name, so the six spellings of one
    employer are one record rather than six."""
    added = 0
    for name, source, relevant in names:
        key = canonical_name(name)
        if not key:
            continue
        rec = registry.get(key)
        if rec is None:
            rec = {"name": name, "canonical": key, "seen_in": [], "relevant_titles": 0,
                   "tried": {}, "resolved": None, "state": "new", "checked_at": None,
                   "recheck_after": None}
            registry[key] = rec
            added += 1
        if source and source not in rec["seen_in"]:
            rec["seen_in"].append(source)
        rec["relevant_titles"] = max(int(rec.get("relevant_titles") or 0), int(relevant))
        if len(name) > len(rec.get("name") or ""):
            rec["name"] = name          # keep the fullest spelling seen, for display
    return registry, added


def names_from_cache(jobs: Iterable[dict], wanted: Callable[[str], bool]) -> list[tuple[str, str, int]]:
    """Company names from raw fetched postings, with how many carried a title you want.

    The richest free source there is: a scan already pulls tens of thousands of postings naming
    hundreds of employers, and the ATS boards for most of them were never configured."""
    tally: dict[str, list] = {}
    for j in jobs:
        name = (j.get("company") or "").strip()
        if not name or name == "?":
            continue
        row = tally.setdefault(canonical_name(name), [name, j.get("source") or "", 0])
        if len(name) > len(row[0]):
            row[0] = name
        if wanted(j.get("title") or ""):
            row[2] += 1
    return [(n, s, c) for n, s, c in tally.values()]


def names_from_candidates(path: Path) -> list[tuple[str, str, int]]:
    """The hand-written candidate list, so nothing already curated is lost."""
    import tomllib
    if not Path(path).exists():
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return []
    return [(c.get("name") or "", "candidates", 0) for c in data.get("candidates") or [] if c.get("name")]


# ----------------------------------------------------------------------------- resolve

def candidate_slugs(path: Path) -> dict[str, list[str]]:
    """{canonical name: slugs} from the candidate list: known board tokens a name alone would not guess."""
    import tomllib
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    return {canonical_name(c["name"]): list(c.get("slugs") or []) for c in data.get("candidates") or []
            if c.get("name") and c.get("slugs")}


def acronym_for(name: str) -> Optional[str]:
    """"Booz Allen Hamilton" -> "bah". Workday tenants are very often the initials, and no amount of
    squashing the full name will ever produce one. Kept away from the feed ATSes on purpose: a
    three-letter Greenhouse token is far more likely to be some unrelated company's board."""
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
             if w and w not in _NOISE]
    if len(words) < 2:
        return None
    return "".join(w[0] for w in words)[:6]


def tokens_for(name: str) -> list[str]:
    """Board tokens a company of this name plausibly uses, best guess first.

    Greenhouse and Ashby tokens are almost always the name squashed to lowercase; Lever's are the
    same with hyphens. Dropping a trailing noise word catches "Liftoff Mobile" -> "liftoff"."""
    base = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    words = [w for w in base.split() if w]
    if not words:
        return []
    out: list[str] = []

    def add(t: str) -> None:
        if t and t not in out and 1 < len(t) <= 40:
            out.append(t)

    add("".join(words))
    add("-".join(words))
    core = [w for w in words if w not in _NOISE] or words
    add("".join(core))
    add("-".join(core))
    if len(core) > 1:
        add(core[0])
    return out[:5]


# ----------------------------------------------------------------------------- probe

def looks_like(guess: str, reported: Optional[str]) -> bool:
    """Does the board the feed handed back actually belong to the company we guessed?

    Greenhouse names itself on every posting, so a token that resolves to somebody else can be
    caught instead of quietly entering the scan as the wrong employer. One name being contained in
    the other is enough -- "Scale" guessing into "Scale AI" is a hit, "Apex" into "Bytedance" is not."""
    if not reported:
        return True                     # Lever and Ashby carry no name; the token is the identity
    a, b = canonical_name(guess), canonical_name(reported)
    if not (a and b):
        return False
    if a == b:
        return True
    # containment is evidence only when the shorter name is distinctive: "Amp" is inside "AMP
    # Robotics" and is a different company in Toronto; "Scale" inside "Scale AI" is the same one
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 5 and short in long_


def probe(ats: str, token: str) -> Optional[dict]:
    """One board, one request. {postings: [{title, location, remote}], company_name} or None.

    None means "we learned nothing from this token", which covers two cases that look different and
    are not. Greenhouse and Lever 404 an unknown board. SmartRecruiters and Workable answer 200 with
    an empty list, so a wrong guess is indistinguishable from a real board that is not hiring today.

    Both return None, and the difference matters: `probe_due` stops at the first token that answers,
    so treating an empty board as a hit would park the guess and never try the platform the company
    is actually on. A real employer with a genuinely empty board is found on the next pass, when it
    has something to post -- which is exactly when we want to hear about it."""
    url = PROBES[ats].format(token=urllib.parse.quote(token, safe=""))
    try:
        data = sources.http_json(url, retries=0, timeout=20)
    except sources.FetchError:
        return None
    if ats == "greenhouse":
        jobs = data.get("jobs") or []
        if not jobs:
            return None
        name = next((j.get("company_name") for j in jobs if j.get("company_name")), None)
        return {"company_name": name,
                "postings": [{"title": j.get("title") or "",
                              "location": (j.get("location") or {}).get("name") or ""} for j in jobs]}
    if ats == "lever":
        jobs = data if isinstance(data, list) else []
        if not jobs:
            return None
        return {"company_name": None,
                "postings": [{"title": j.get("text") or "",
                              "location": (j.get("categories") or {}).get("location") or ""} for j in jobs]}
    if ats == "workable":
        jobs = (data or {}).get("jobs") or []
        if not isinstance(data, dict) or not jobs:
            return None
        return {"company_name": data.get("name") or None,
                "postings": [{"title": j.get("title") or "",
                              "location": ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x),
                              "remote": bool(j.get("telecommuting"))} for j in jobs]}
    if ats == "breezy":
        jobs = data if isinstance(data, list) else []
        if not jobs:
            return None
        def _nm(v):
            return (v or {}).get("name") if isinstance(v, dict) else v
        out = []
        for j in jobs:
            loc = j.get("location") or {}
            out.append({"title": j.get("name") or "",
                        "location": ", ".join(str(x) for x in
                                              (_nm(loc.get("city")), _nm(loc.get("state")), _nm(loc.get("country"))) if x),
                        "remote": bool(loc.get("is_remote"))})
        return {"company_name": None, "postings": out}
    if ats == "recruitee":
        offers = (data or {}).get("offers") or []
        if not isinstance(data, dict) or not offers:
            return None
        return {"company_name": None,
                "postings": [{"title": j.get("title") or "",
                              "location": ", ".join(x for x in (j.get("city"), j.get("country_name")) if x),
                              "remote": bool(j.get("remote"))} for j in offers]}
    if ats == "bamboohr":
        jobs = (data or {}).get("result") or [] if isinstance(data, dict) else []
        if not jobs:
            return None
        # the list carries no company name, and short tokens collide ("svt" is an Alaska health
        # provider, not SVT Robotics): the board must prove whose it is, from its company-info record
        try:
            info = sources.http_json(f"https://{urllib.parse.quote(token, safe='')}.bamboohr.com/careers/company-info",
                                     headers={"Accept": "application/json"}, retries=0, timeout=20)
            owner = ((info or {}).get("result") or {}).get("name") or ""
        except sources.FetchError:
            owner = ""
        return {"company_name": owner or "(unknown owner)",
                "postings": [{"title": j.get("jobOpeningName") or "",
                              "location": ", ".join(x for x in ((j.get("location") or {}).get("city"),
                                                                (j.get("location") or {}).get("state")) if x),
                              "remote": str(j.get("isRemote")).lower() == "true"} for j in jobs]}
    if ats == "rippling":
        jobs = data if isinstance(data, list) else []
        if not jobs:
            return None
        # likewise: the board's own page title names its owner ("amp" is a Toronto company)
        try:
            page = sources.http_text(f"https://ats.rippling.com/{urllib.parse.quote(token, safe='')}/jobs",
                                     headers={"Accept": "text/html"}, retries=0, timeout=20)
            m = re.search(r"<title[^>]*>([^<|]+)", page, re.I)
            owner = m.group(1).strip() if m else ""
        except sources.FetchError:
            owner = ""
        return {"company_name": owner or "(unknown owner)",
                "postings": [{"title": j.get("name") or "",
                              "location": (j.get("workLocation") or {}).get("label") or "",
                              "remote": "remote" in ((j.get("workLocation") or {}).get("label") or "").lower()}
                             for j in jobs]}
    if ats == "smartrecruiters":
        jobs = (data or {}).get("content") or []
        if not isinstance(data, dict) or "content" not in data or not jobs:
            return None
        name = next((((j.get("company") or {}).get("name")) for j in jobs
                     if (j.get("company") or {}).get("name")), None)
        out = []
        for j in jobs:
            loc = j.get("location") or {}
            bits = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
            out.append({"title": j.get("name") or "",
                        "location": ", ".join(b for b in bits if b) or (loc.get("fullLocation") or ""),
                        "remote": bool(loc.get("remote"))})
        return {"company_name": name, "postings": out}
    jobs = (data or {}).get("jobs") or []
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    if not jobs:
        return None
    # Ashby's posting API carries no company name, so for Ashby and Lever the token IS the identity.
    return {"company_name": None,
            "postings": [{"title": j.get("title") or "",
                          "location": j.get("location") or "",
                          "remote": bool(j.get("isRemote"))} for j in jobs if j.get("isListed", True)]}




# ----------------------------------------------------------------------------- careers pages

# A careers page that links to a job board is the most reliable way to learn a company's ATS: the
# company itself says where its jobs are. (platform, pattern whose group 1 is the board token)
BOARD_LINKS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("breezy", r"([A-Za-z0-9-]+)\.breezy\.hr"),
    ("recruitee", r"([A-Za-z0-9-]+)\.recruitee\.com"),
    ("bamboohr", r"([A-Za-z0-9-]+)\.bamboohr\.com/(?:careers|jobs)"),
    ("rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
]
_NOT_TOKENS = {"www", "api", "embed", "jobs", "careers", "static", "assets", "cdn", "help", "support", "app"}


def boards_linked(html_text: str) -> list[tuple[str, str]]:
    """(platform, token) for every job board a careers page links to, each once."""
    out: list[tuple[str, str]] = []
    for ats, pat in BOARD_LINKS:
        for tok in re.findall(pat, html_text or "", re.I):
            if tok.lower() not in _NOT_TOKENS and (ats, tok) not in out:
                out.append((ats, tok))
    return out


def board_from_careers_page(url: str) -> Optional[tuple[str, str, dict]]:
    """Read a company's careers page and return the first linked board that answers a probe, as
    (platform, token, probe result), or None when the page links to none (a page-watcher company)."""
    try:
        page = sources.http_text(url, headers={"Accept": "text/html"}, retries=0, timeout=20)
    except sources.FetchError:
        return None
    for ats, tok in boards_linked(page):
        if ats not in PROBES:
            continue
        got = probe(ats, tok)
        if got is not None:
            return ats, tok, got
    return None


# ----------------------------------------------------------------------------- assess

def assess(result: dict, matcher, now=None) -> dict:
    """Would this board produce anything the daily scan would keep?

    Runs the real gate, not an approximation of it. `Matcher.gate` is what the scan applies, so a
    board promoted here cannot produce postings the scan then throws away -- and a board rejected
    here was never going to contribute anything. Title, location and reachability all count.

    Returns {hits, sample} where `sample` is the first posting that passed, for the audit line."""
    from ..fetch.sources import Job
    hits, sample = 0, None
    for i, pst in enumerate(result.get("postings") or []):
        job = Job(id=f"probe:{i}", source="probe", company="", title=pst.get("title") or "",
                  url="", apply_url="", location=pst.get("location") or "",
                  remote_signal="remote" if pst.get("remote") else None,
                  posted_at=None, description="")
        if matcher.gate(job).get("keep"):
            hits += 1
            if sample is None:
                sample = f"{job.title} ({job.location or 'no location'})"
    return {"hits": hits, "sample": sample}


# ----------------------------------------------------------------------------- promote

def render_discovered(records: Iterable[dict], profile: str = "engineer") -> str:
    """The discovered boards as a TOML fragment the loader concatenates.

    Written to its own file rather than appended to config.toml, which is 1,677 hand-curated lines.
    Resetting discovery is then deleting one file, and reviewing what it added is reading one."""
    out = ["# Boards found by `jobhunt.py discover`. Generated -- edit config.toml instead.",
           "# Delete this file to reset discovery; the registry in data/companies.json remembers",
           "# what was already probed, so a re-run will not re-guess dead tokens.", ""]
    for r in sorted(records, key=lambda r: (r.get("name") or "").lower()):
        res = r.get("resolved") or {}
        name = (res.get("company_name") or r.get("name") or "").replace('"', "'")
        if res.get("ats") == "workday" and res.get("tenant"):
            out += ["[[companies]]", f'name = "{name}"', 'ats = "workday"',
                    f'tenant = "{res["tenant"]}"', f'wd = "{res["wd"]}"', f'site = "{res["site"]}"',
                    'queries = ["machine learning", "artificial intelligence", "forward deployed", "agentic"]',
                    "max_details = 300", f'profiles = ["{profile}"]', ""]
            continue
        if not res.get("ats") or not res.get("token"):
            continue
        key = "slug" if res["ats"] == "lever" else "board"
        out += ["[[companies]]", f'name = "{name}"', f'ats = "{res["ats"]}"',
                f'{key} = "{res["token"]}"', f'profiles = ["{profile}"]', ""]
    return "\n".join(out) + "\n"


def write_discovered(root: Path, registry: dict, profile: str = "engineer") -> int:
    """Rewrite the discovered-boards file from the registry. Returns how many boards it HOLDS.

    Not how many records are promoted: most of those are companies already in config.toml, marked
    promoted so they are never probed again, and they carry no resolved token because there was
    nothing to resolve. Counting them here reported 273 boards written to a file with 46 in it."""
    text = render_discovered([r for r in registry.values() if r.get("state") == "promoted"], profile)
    Path(root, DISCOVERED_FILE).write_text(text, encoding="utf-8")
    return text.count("[[companies]]")


# ----------------------------------------------------------------------------- the run

def harvest_all(root: Path, profile, log: Callable[[str], None] = print) -> tuple[dict, int]:
    """Fold every free source of company names into the registry. No network.

    The richest one is the posting cache a scan already wrote: tens of thousands of postings naming
    hundreds of employers, most of whose boards were never configured."""
    from ..classify.filters import Matcher
    from ..pipeline import load_raw
    registry = load(profile.data_dir)
    matcher = Matcher(profile.search)

    jobs, _errs, _at = load_raw(root)
    got = names_from_cache((j.to_dict() for j in jobs), lambda t: matcher.title_tier(t)[0] != 0)
    registry, added = harvest(got, registry)
    log(f"  cache: {len(got)} companies named, {added} new")

    cand = names_from_candidates(Path(root) / "tools" / "board_candidates.toml")
    registry, n2 = harvest(cand, registry)
    for key, slugs in candidate_slugs(Path(root) / "tools" / "board_candidates.toml").items():
        if key in registry:
            registry[key]["slugs"] = slugs          # tried before the guesses from the name
    log(f"  candidate list: {len(cand)} names, {n2} new")

    # Anything already configured is already in the scan and needs no probe; anything on
    # company_exclude must never be probed at all. Configured records keep `resolved = None`, which
    # is what keeps them out of the generated file -- they already live in config.toml.
    names = {canonical_name(c.get("name") or "") for c in profile.search.get("companies") or []}
    excluded = [t.lower() for t in ((profile.search.get("search") or {}).get("company_exclude") or [])]
    for key, rec in registry.items():
        if any(t in key for t in excluded):
            rec["state"], rec["resolved"] = "excluded", None
        elif rec["state"] == "new" and key in names:
            rec["state"], rec["checked_at"] = "promoted", _now()
    save(profile.data_dir, registry)
    return registry, added + n2


def probe_due(registry: dict, matcher, limit: int = 0, recheck_days: int = 90,
              log: Callable[[str], None] = print, save_every: int = 25,
              data_dir: Optional[Path] = None) -> dict:
    """Probe the companies worth spending requests on, and record what came back.

    Checkpoints into the registry as it goes: a probe run is thousands of requests and being
    interrupted must not throw them away, exactly as the judge pass banks each batch."""
    todo = [r for r in registry.values() if due(r)]
    todo.sort(key=lambda r: (-int(r.get("relevant_titles") or 0), r.get("canonical") or ""))
    if limit > 0:
        todo = todo[:limit]
    stats = {"probed": 0, "promoted": 0, "parked": 0, "dead": 0, "requests": 0}
    log(f"  probing {len(todo)} companies")
    for i, rec in enumerate(todo, 1):
        found = None
        for ats in PROBES:
            tried = rec["tried"].setdefault(ats, [])
            for token in list(dict.fromkeys(list(rec.get("slugs") or []) + tokens_for(rec["name"]))):
                if token in tried:
                    continue
                tried.append(token)
                stats["requests"] += 1
                res = probe(ats, token)
                if res is not None and looks_like(rec["name"], res.get("company_name")):
                    found = (ats, token, res)
                    break
            if found:
                break
        stats["probed"] += 1
        rec["checked_at"] = _now()
        if not found:
            rec["state"] = "dead"
            rec["recheck_after"] = (datetime.now(timezone.utc) + timedelta(days=recheck_days * 4)).date().isoformat()
            stats["dead"] += 1
        else:
            ats, token, res = found
            verdict = assess(res, matcher)
            rec["resolved"] = {"ats": ats, "token": token, "company_name": res.get("company_name"),
                               "postings": len(res.get("postings") or [])}
            if verdict["hits"]:
                rec["state"] = "promoted"
                rec["recheck_after"] = None
                rec["sample"] = verdict["sample"]
                stats["promoted"] += 1
                log(f"  + {rec['name']}: {ats}/{token}, {verdict['hits']} relevant — {verdict['sample']}")
            else:
                rec["state"] = "parked"
                rec["recheck_after"] = (datetime.now(timezone.utc) + timedelta(days=recheck_days)).date().isoformat()
                stats["parked"] += 1
        if data_dir and i % save_every == 0:
            save(data_dir, registry)
            log(f"  {i}/{len(todo)} probed, {stats['promoted']} promoted")
    if data_dir:
        save(data_dir, registry)
    return stats


def apply_cap(registry: dict, max_boards: int, log: Callable[[str], None] = print) -> int:
    """Hold the daily scan to a size it can actually finish.

    A 160-board scan writes a 155MB cache in seven minutes; the cache is one JSON blob read and
    written whole, so this does not scale forever. Promotions past the cap are parked by fewest
    relevant postings first, and the fact that it happened is reported rather than swallowed."""
    promoted = [r for r in registry.values() if r.get("state") == "promoted"]
    if max_boards <= 0 or len(promoted) <= max_boards:
        return 0
    promoted.sort(key=lambda r: (-int(r.get("relevant_titles") or 0),
                                 -int((r.get("resolved") or {}).get("postings") or 0)))
    for rec in promoted[max_boards:]:
        rec["state"] = "parked"
        rec["recheck_after"] = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    cut = len(promoted) - max_boards
    log(f"  capped at {max_boards} boards: {cut} parked for next month (raise [discovery] max_daily_boards)")
    return cut


# ----------------------------------------------------------------------------- workday

# Workday has no public feed, only the JSON endpoint its own front-end calls, addressed by tenant AND
# site. There is no way to ask "which site does this tenant use", so finding one is a sweep. That is
# expensive -- 4 hosts x 14 site names -- which is why it is opt-in and only spent on companies
# already known to post work you want. It matters because most large government contractors are on
# Workday and none of them answer a Greenhouse-style feed.
WD_HOSTS = ("wd1", "wd5", "wd3", "wd12")
WD_SITES = ("External", "Careers", "careers", "External_Career_Site", "ExternalCareerSite", "Search",
            "search", "jobs", "External_Careers", "US_External_Career_Site", "{t}_Careers",
            "{t}Careers", "{t}_External", "{t}")


def probe_workday(tenant: str, wd: str, site: str) -> int:
    """How many postings this tenant/host/site reports, or -1 when the combination is wrong."""
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = json.dumps({"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}).encode()
    try:
        data = sources.http_json(url, method="POST", body=body, retries=0, timeout=20)
    except sources.FetchError:
        return -1
    total = data.get("total") if isinstance(data, dict) else None
    return total if isinstance(total, int) else -1


def find_workday(name: str, log: Callable[[str], None] = lambda _s: None) -> Optional[dict]:
    """Sweep for a tenant/site that answers. Returns {tenant, wd, site, postings} or None."""
    guesses = tokens_for(name)[:2]
    acro = acronym_for(name)
    if acro and acro not in guesses:
        guesses.append(acro)                            # BAH, LMT, GDIT: tenants are often initials
    for tenant in guesses:
        for wd in WD_HOSTS:
            for site in WD_SITES:
                n = probe_workday(tenant, wd, site.replace("{t}", tenant))
                if n > 0:
                    return {"tenant": tenant, "wd": wd, "site": site.replace("{t}", tenant),
                            "postings": n}
    return None


def workday_pass(registry: dict, matcher, limit: int = 0, log: Callable[[str], None] = print,
                 data_dir: Optional[Path] = None, min_relevant: int = 5) -> dict:
    """Second chance for the companies the feed ATSes said nothing about.

    Only worth running on companies already known to post work you want -- the sweep costs up to 56
    requests each -- so it is opt-in and gated on `min_relevant`. Deloitte, Atlassian, PwC and most
    large contractors land here: they are real employers with real ML roles and no public feed."""
    todo = [r for r in registry.values()
            if r.get("state") == "dead" and int(r.get("relevant_titles") or 0) >= min_relevant
            and "workday" not in (r.get("tried") or {})]
    todo.sort(key=lambda r: -int(r.get("relevant_titles") or 0))
    if limit > 0:
        todo = todo[:limit]
    stats = {"swept": 0, "found": 0}
    log(f"  sweeping Workday for {len(todo)} companies that no feed answered for")
    for i, rec in enumerate(todo, 1):
        rec.setdefault("tried", {})["workday"] = tokens_for(rec["name"])[:2]
        stats["swept"] += 1
        got = find_workday(rec["name"], log=log)
        rec["checked_at"] = _now()
        if got:
            rec["resolved"] = {"ats": "workday", **got}
            rec["state"] = "promoted"
            rec["recheck_after"] = None
            stats["found"] += 1
            log(f"  + {rec['name']}: workday {got['tenant']}/{got['wd']}/{got['site']}, {got['postings']} postings")
        if data_dir and i % 5 == 0:
            save(data_dir, registry)
    if data_dir:
        save(data_dir, registry)
    return stats


def reassess(registry: dict, matcher, log: Callable[[str], None] = print,
             data_dir: Optional[Path] = None, recheck_days: int = 90) -> dict:
    """Re-run the gate over boards discovery already promoted, and demote what no longer passes.

    The gate changes. When it does, every board admitted under the old one is a claim nobody has
    checked since -- and a board admitted by a bug stays in the scan forever, quietly contributing
    postings you did not want. This re-probes only what discovery found; hand-written entries in
    config.toml are somebody's deliberate choice and are left alone."""
    todo = [r for r in registry.values()
            if r.get("state") == "promoted" and (r.get("resolved") or {}).get("token")]
    stats = {"checked": 0, "demoted": 0}
    log(f"  re-checking {len(todo)} discovered boards against the current gate")
    for i, rec in enumerate(todo, 1):
        res = rec["resolved"]
        got = probe(res["ats"], res["token"])
        stats["checked"] += 1
        if got is None:
            continue                       # gone or unreachable today; leave it, a scan will notice
        verdict = assess(got, matcher)
        if verdict["hits"]:
            rec["sample"] = verdict["sample"]
            continue
        rec["state"] = "parked"
        rec["recheck_after"] = (datetime.now(timezone.utc) + timedelta(days=recheck_days)).date().isoformat()
        stats["demoted"] += 1
        log(f"  - {rec['name']}: nothing the gate would keep any more")
        if data_dir and i % 10 == 0:
            save(data_dir, registry)
    if data_dir:
        save(data_dir, registry)
    return stats
