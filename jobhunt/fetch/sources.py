"""Fetch job postings from public ATS feeds and normalize them into Job records.

Supported sources (all public, no auth, no scraping of logged-in pages):
  * greenhouse  - https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true
  * lever       - https://api.lever.co/v0/postings/<slug>?mode=json
  * ashby       - https://api.ashbyhq.com/posting-api/job-board/<board>
  * workday     - POST https://<tenant>.<wd>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs  (experimental)
  * hn          - Hacker News "Who is hiring?" monthly thread via the Algolia API
  * usajobs     - https://data.usajobs.gov/api/search  (free API key + email, read from .env)
  * remotive / remoteok / arbeitnow / himalayas / themuse - public JSON job boards (no key)
  * adzuna / jooble - aggregators with free API keys (ADZUNA_APP_ID + ADZUNA_APP_KEY, JOOBLE_KEY in .env)

Only the Python standard library is used.
"""
from __future__ import annotations

import html
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from . import quota

USER_AGENT = "jobhunt/0.1 (personal job-search tool; stdlib urllib)"

# Test hook: set to a callable(url, method, body, headers) -> str to bypass the network.
FETCHER: Optional[Callable[[str, str, Optional[bytes], dict], str]] = None


class FetchError(RuntimeError):
    pass


# Politeness. Every request goes through one token bucket per hostname, so a thread pool cannot put
# twelve simultaneous requests on one company's board API. It matters more than it used to: board
# discovery probes thousands of tokens against three hosts, and the old probe tool did that at
# twelve workers with no delay at all, which is both rude and the fastest way to get blocked.
#
# Set REQUESTS_PER_SECOND to 0 to turn it off. The test fetcher bypasses it entirely, so the suite
# never sleeps.
REQUESTS_PER_SECOND = 4.0     # [discovery] requests_per_second overrides this at startup
_rate_lock = threading.Lock()
_next_ok: dict[str, float] = {}


def _throttle(url: str) -> None:
    """Block until this host is allowed another request."""
    if REQUESTS_PER_SECOND <= 0:
        return
    host = urllib.parse.urlsplit(url).hostname or ""
    gap = 1.0 / REQUESTS_PER_SECOND
    while True:
        with _rate_lock:
            now = time.monotonic()
            due = _next_ok.get(host, 0.0)
            if now >= due:
                _next_ok[host] = now + gap
                return
            wait = due - now
        time.sleep(wait)


def http_text(url: str, method: str = "GET", body: Optional[bytes] = None,
              headers: Optional[dict] = None, timeout: int = 40, retries: int = 2) -> str:
    """GET/POST a URL and return the response body as text, with small retries."""
    headers = dict(headers or {})
    if FETCHER is not None:
        return FETCHER(url, method, body, headers)
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept", "application/json, text/plain, */*")
    _throttle(url)
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:  # 4xx/5xx
            last_err = e
            if e.code in (404, 410, 401, 403):
                raise FetchError(f"HTTP {e.code} for {url}") from e
        except Exception as e:  # noqa: BLE001 - network errors of any kind
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"{type(last_err).__name__}: {last_err} for {url}")


def http_json(url: str, **kw):
    text = http_text(url, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"non-JSON response from {url}: {text[:120]!r}") from e


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<\s*(br|p|/p|li|/li|/div|/h\d|/tr|/ul|/ol)\b[^>]*>", re.I)


def strip_html(s: Optional[str]) -> str:
    """Convert HTML (possibly entity-escaped, as Greenhouse does) to readable plain text."""
    if not s:
        return ""
    s = html.unescape(s)
    if "<" in s and ">" in s:
        s = _BLOCK_RE.sub("\n", s)
        s = _TAG_RE.sub(" ", s)
        s = html.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def to_iso(value) -> Optional[str]:
    """Normalize assorted timestamp formats to an ISO-8601 UTC string."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e11:  # milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        s = str(value).strip()
        if re.fullmatch(r"\d{10,13}", s):
            return to_iso(int(s))
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


@dataclass
class Job:
    id: str
    source: str
    company: str
    title: str
    url: str
    apply_url: str
    location: str
    remote_signal: Optional[str]      # "remote" | "hybrid" | "onsite" | None (from structured fields only)
    posted_at: Optional[str]
    description: str
    salary_text: Optional[str] = None
    meta: dict = field(default_factory=dict)
    board: str = ""                   # config entry name this posting came from

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- Greenhouse

def fetch_greenhouse(company: str, board: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true"
    data = http_json(url)
    jobs: list[Job] = []
    for j in data.get("jobs", []):
        jid = str(j.get("id"))
        loc = (j.get("location") or {}).get("name") or ""
        offices = [o.get("name", "") for o in (j.get("offices") or []) if isinstance(o, dict)]
        remote_signal = None
        for m in j.get("metadata") or []:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name", "")).lower()
            val = m.get("value")
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            val = str(val or "").lower()
            if any(k in name for k in ("location type", "workplace", "remote", "work type", "office type")):
                if "remote" in val:
                    remote_signal = "remote"
                elif "hybrid" in val:
                    remote_signal = "hybrid"
                elif "on-site" in val or "onsite" in val or "in office" in val or "in-office" in val:
                    remote_signal = "onsite"
        if remote_signal is None:
            ltxt = (loc + " " + " ".join(offices)).lower()
            if "remote" in ltxt:
                remote_signal = "remote"
            elif "hybrid" in ltxt:
                remote_signal = "hybrid"
        absolute_url = j.get("absolute_url") or ""
        # The absolute_url may point at the company's own site (which embeds Greenhouse);
        # the embed URL below always renders the posting + application form directly.
        apply_url = absolute_url if "greenhouse.io" in absolute_url else (
            f"https://boards.greenhouse.io/embed/job_app?for={board}&token={jid}")
        desc = strip_html(j.get("content"))
        jobs.append(Job(
            id=f"greenhouse:{board}:{jid}",
            source="greenhouse",
            company=company,
            title=(j.get("title") or "").strip(),
            url=absolute_url,
            apply_url=apply_url,
            location="; ".join([x for x in [loc] + [o for o in offices if o and o not in loc] if x]),
            remote_signal=remote_signal,
            posted_at=to_iso(j.get("first_published") or j.get("updated_at")),
            description=desc,
            salary_text=None,
            meta={"updated_at": j.get("updated_at"),
                  "departments": [d.get("name") for d in (j.get("departments") or []) if isinstance(d, dict)]},
        ))
    return jobs


# ----------------------------------------------------------------------------- Lever

def fetch_lever(company: str, slug: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json"
    data = http_json(url)
    if not isinstance(data, list):
        raise FetchError(f"unexpected Lever payload for {slug}")
    jobs: list[Job] = []
    for j in data:
        jid = str(j.get("id"))
        cats = j.get("categories") or {}
        locs = [cats.get("location") or ""] + list(cats.get("allLocations") or [])
        locs = [l for i, l in enumerate(locs) if l and l not in locs[:i]]
        wt = str(j.get("workplaceType") or "").lower()
        remote_signal = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(wt)
        parts = [j.get("descriptionPlain") or strip_html(j.get("description"))]
        for lst in j.get("lists") or []:
            if isinstance(lst, dict):
                parts.append((lst.get("text") or "") + "\n" + strip_html(lst.get("content")))
        parts.append(j.get("additionalPlain") or strip_html(j.get("additional")))
        salary = None
        sr = j.get("salaryRange")
        if isinstance(sr, dict) and sr.get("min") and sr.get("max"):
            salary = f"{sr.get('currency', '')} {sr['min']}-{sr['max']} {sr.get('interval', '')}".strip()
        hosted = j.get("hostedUrl") or ""
        jobs.append(Job(
            id=f"lever:{slug}:{jid}",
            source="lever",
            company=company,
            title=(j.get("text") or "").strip(),
            url=hosted,
            apply_url=j.get("applyUrl") or (hosted.rstrip("/") + "/apply"),
            location="; ".join(locs),
            remote_signal=remote_signal,
            posted_at=to_iso(j.get("createdAt")),
            description="\n".join(p for p in parts if p),
            salary_text=salary,
            meta={"team": cats.get("team"), "department": cats.get("department"),
                  "commitment": cats.get("commitment"), "country": j.get("country")},
        ))
    return jobs


# ----------------------------------------------------------------------------- Ashby

def fetch_ashby(company: str, board: str) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
    data = http_json(url)
    jobs: list[Job] = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        jid = str(j.get("id"))
        locs = [j.get("location") or ""]
        for sl in j.get("secondaryLocations") or []:
            if isinstance(sl, dict) and sl.get("location"):
                locs.append(sl["location"])
        remote_signal = None
        if j.get("isRemote") is True:
            remote_signal = "remote"
        wt = str(j.get("workplaceType") or "").lower()
        if wt in ("hybrid", "onsite", "on-site"):
            remote_signal = "hybrid" if wt == "hybrid" else "onsite"
        comp = j.get("compensation") or {}
        salary = None
        if isinstance(comp, dict):
            salary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
        job_url = j.get("jobUrl") or ""
        jobs.append(Job(
            id=f"ashby:{board}:{jid}",
            source="ashby",
            company=company,
            title=(j.get("title") or "").strip(),
            url=job_url,
            apply_url=j.get("applyUrl") or (job_url.rstrip("/") + "/application"),
            location="; ".join(l for l in locs if l),
            remote_signal=remote_signal,
            posted_at=to_iso(j.get("publishedAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            salary_text=salary,
            meta={"department": j.get("department"), "team": j.get("team"),
                  "employment_type": j.get("employmentType")},
        ))
    return jobs


# ----------------------------------------------------------------------------- Workday (experimental)

_POSTED_ON_RE = re.compile(r"(\d+)\+?\s*day", re.I)


def workday_posted_on_to_iso(text: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    low = text.lower()
    if "today" in low:
        days = 0
    elif "yesterday" in low:
        days = 1
    else:
        m = _POSTED_ON_RE.search(low)
        if not m:
            return None
        days = int(m.group(1))
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def fetch_smartrecruiters(company: str, board: str, max_details: int = 400,
                          max_results: int = 1000, title_filter=None) -> list[Job]:
    """https://api.smartrecruiters.com/v1/companies/{id}/postings — public, no key.

    The best-structured of the ATS feeds. `location` carries explicit `remote` and `hybrid` booleans
    rather than leaving it to be read out of a location string, and `company.name` means the board
    names itself, so a guessed token that resolves to the wrong employer can be caught. Greenhouse is
    the only other source that does either.

    Descriptions live one request away, like Workday, so `max_details` is a real budget: a posting
    past it is kept with an EMPTY description rather than dropped, because an untitled posting still
    tells you the company is hiring."""
    base = f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(board)}/postings"
    jobs: list[Job] = []
    details = 0
    offset = 0
    while offset < max_results:
        data = http_json(f"{base}?limit=100&offset={offset}")
        batch = data.get("content") or []
        if not batch:
            break
        for j in batch:
            title = j.get("name") or ""
            jid = str(j.get("id") or j.get("uuid") or "")
            if not jid or not title:
                continue
            loc = j.get("location") or {}
            bits = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
            where = ", ".join(b for b in bits if b) or (loc.get("fullLocation") or "")
            remote_signal = "remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else None)
            url = j.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board}/{jid}"
            desc = ""
            # The list view has no description. Spend the budget on postings that could matter.
            if details < max_details and (title_filter is None or title_filter(title)):
                try:
                    full = http_json(f"{base}/{urllib.parse.quote(jid)}")
                    details += 1
                    secs = ((full.get("jobAd") or {}).get("sections") or {})
                    desc = "\n\n".join(
                        strip_html((secs.get(k) or {}).get("text") or "")
                        for k in ("jobDescription", "qualifications", "additionalInformation"))
                    url = full.get("postingUrl") or url
                except FetchError:
                    pass
            jobs.append(_job("smartrecruiters", (j.get("company") or {}).get("name") or company, jid,
                             title, url, j.get("applyUrl") or url, where, remote_signal,
                             to_iso(j.get("releasedDate")), desc.strip(), None,
                             {"department": (j.get("department") or {}).get("label"),
                              "experience": (j.get("experienceLevel") or {}).get("label")}))
        offset += len(batch)
        if offset >= int(data.get("totalFound") or 0):
            break
    return jobs


def fetch_workable(company: str, board: str) -> list[Job]:
    """https://apply.workable.com/api/v1/widget/accounts/{a}?details=true — public, no key.

    `details=true` is what makes descriptions come back in the list, so unlike SmartRecruiters this
    needs no per-posting budget. An account that does not exist answers 200 with an empty list rather
    than 404, which is why discover.probe treats an empty board as a miss."""
    url = f"https://apply.workable.com/api/v1/widget/accounts/{urllib.parse.quote(board)}?details=true"
    data = http_json(url)
    jobs: list[Job] = []
    for j in data.get("jobs") or []:
        jid = str(j.get("shortcode") or j.get("id") or "")
        title = j.get("title") or ""
        if not jid or not title:
            continue
        where = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x) or (j.get("location") or "")
        remote = j.get("telecommuting")
        link = j.get("url") or j.get("shortlink") or ""
        jobs.append(_job("workable", company, jid, title, link, j.get("application_url") or link,
                         where, "remote" if remote else None, to_iso(j.get("published_on") or j.get("created_at")),
                         strip_html(j.get("description") or "") + "\n\n" + strip_html(j.get("requirements") or ""),
                         None, {"department": j.get("department"), "type": j.get("employment_type")}))
    return jobs


def fetch_breezy(company: str, board: str) -> list[Job]:
    """https://{t}.breezy.hr/json — public, no key. A list, not an envelope.

    Location is an object of country/state/city rather than a string, and the list carries no
    description; the posting URL is where a human reads it."""
    data = http_json(f"https://{urllib.parse.quote(board)}.breezy.hr/json")
    jobs: list[Job] = []
    for j in data if isinstance(data, list) else []:
        jid = str(j.get("friendly_id") or j.get("id") or "")
        title = j.get("name") or ""
        if not jid or not title:
            continue
        loc = j.get("location") or {}
        bits = [(loc.get("city") or {}).get("name") if isinstance(loc.get("city"), dict) else loc.get("city"),
                (loc.get("state") or {}).get("name") if isinstance(loc.get("state"), dict) else loc.get("state"),
                (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country")]
        where = ", ".join(str(b) for b in bits if b)
        url = j.get("url") or f"https://{board}.breezy.hr/p/{jid}"
        jobs.append(_job("breezy", company, jid, title, url, url, where,
                         "remote" if loc.get("is_remote") else None, to_iso(j.get("published_date")),
                         strip_html(j.get("description") or ""), None,
                         {"department": (j.get("department") or {}).get("name") if isinstance(j.get("department"), dict) else j.get("department"),
                          "type": (j.get("type") or {}).get("name") if isinstance(j.get("type"), dict) else j.get("type")}))
    return jobs


def fetch_recruitee(company: str, board: str) -> list[Job]:
    """https://{t}.recruitee.com/api/offers — public, no key. No trailing slash: that 404s."""
    data = http_json(f"https://{urllib.parse.quote(board)}.recruitee.com/api/offers")
    jobs: list[Job] = []
    for j in data.get("offers") or []:
        jid = str(j.get("id") or "")
        title = j.get("title") or ""
        if not jid or not title:
            continue
        where = ", ".join(x for x in (j.get("city"), j.get("state_name") or j.get("state_code"),
                                      j.get("country_name") or j.get("country_code")) if x) or (j.get("location") or "")
        remote = str(j.get("remote") or "").lower() in ("true", "1") or bool(j.get("remote"))
        link = j.get("careers_url") or j.get("careers_apply_url") or ""
        jobs.append(_job("recruitee", company, jid, title, link, j.get("careers_apply_url") or link,
                         where, "remote" if remote else None, to_iso(j.get("published_at") or j.get("created_at")),
                         strip_html(j.get("description") or "") + "\n\n" + strip_html(j.get("requirements") or ""),
                         None, {"department": j.get("department"), "type": j.get("employment_type")}))
    return jobs


def fetch_bamboohr(company: str, board: str, max_details: int = 60) -> list[Job]:
    """https://{t}.bamboohr.com/careers/list, details at /careers/{id}/detail -- public, no key.

    A small company's commonest ATS. The list has title, department and location but no description, so
    each posting's detail is fetched up to a budget. Ids are per company ("160"), so the posting id
    carries the board to stay unique. An unknown company redirects to bamboohr.com (HTML), which
    http_json reports as a FetchError: a miss, not an empty board."""
    base = f"https://{urllib.parse.quote(board)}.bamboohr.com/careers"
    data = http_json(f"{base}/list", headers={"Accept": "application/json"})
    jobs: list[Job] = []
    for n, j in enumerate((data or {}).get("result") or []):
        jid, title = str(j.get("id") or ""), j.get("jobOpeningName") or ""
        if not jid or not title:
            continue
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
        remote = str(j.get("isRemote")).lower() == "true" or str(j.get("locationType")) == "1"
        desc, posted, pay = "", None, None
        if n < max_details:
            try:
                d = (http_json(f"{base}/{jid}/detail", headers={"Accept": "application/json"}).get("result") or {}).get("jobOpening") or {}
                desc, posted, pay = strip_html(d.get("description") or ""), d.get("datePosted"), d.get("compensation")
            except FetchError:
                pass
        url = f"{base}/{jid}"
        jobs.append(_job("bamboohr", company, f"{board}:{jid}", title, url, url, where, "remote" if remote else None,
                         posted, desc, pay or None,
                         {"department": j.get("departmentLabel"), "type": j.get("employmentStatusLabel")}))
    return jobs


def fetch_rippling(company: str, board: str, max_details: int = 60) -> list[Job]:
    """https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs -- Rippling's documented public
    job-board API, no key. The list repeats a posting once per location and carries no description;
    each distinct posting's detail (/jobs/{uuid}) is fetched up to a budget."""
    base = f"https://api.rippling.com/platform/api/ats/v1/board/{urllib.parse.quote(board)}/jobs"
    data = http_json(base)
    seen: dict[str, dict] = {}
    for j in data if isinstance(data, list) else []:
        uid = str(j.get("uuid") or "")
        if not uid or not j.get("name"):
            continue
        row = seen.setdefault(uid, {**j, "places": []})
        place = (j.get("workLocation") or {}).get("label")
        if place and place not in row["places"]:
            row["places"].append(place)
    jobs: list[Job] = []
    for n, (uid, j) in enumerate(seen.items()):
        where = "; ".join(j["places"])
        desc = ""
        if n < max_details:
            try:
                d = http_json(f"{base}/{uid}")
                parts = (d.get("description") or {}) if isinstance(d, dict) else {}
                desc = "\n\n".join(strip_html(v) for v in parts.values() if isinstance(v, str))
            except FetchError:
                pass
        url = j.get("url") or f"https://ats.rippling.com/{board}/jobs/{uid}"
        jobs.append(_job("rippling", company, uid, j["name"], url, url, where,
                         "remote" if "remote" in where.lower() else None, None, desc, None,
                         {"department": (j.get("department") or {}).get("label")}))
    return jobs


_JOB_WORD = re.compile(r"\b(engineer|scientist|developer|technician|technologist|architect|analyst|designer|researcher|"
                       r"specialist|manager|director|lead|intern|operator|trainer|administrator|programmer|consultant)s?\b", re.I)
_NAV_WORD = re.compile(r"^(careers?|jobs?|join (our|the) team|apply( now)?|view (all|jobs|openings)|open (positions|roles)|"
                       r"back|contact|home|about)\b", re.I)
_LINK_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.S | re.I)


def page_job_links(html_text: str, base_url: str) -> list[tuple[str, str]]:
    """(title, absolute url) for the links on a careers page that look like job postings: link text that
    names a role (engineer, technician, ...), not navigation, same site, each URL once (the longest text
    wins, since sites repeat their menus)."""
    base = urllib.parse.urlparse(base_url)
    found: dict[str, str] = {}
    for href, inner in _LINK_RE.findall(html_text or ""):
        text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner))).replace("\xa0", " ").strip()
        if not (3 <= len(text) <= 90) or not _JOB_WORD.search(text) or _NAV_WORD.match(text):
            continue
        url = urllib.parse.urljoin(base_url, href.strip())
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or u.netloc != base.netloc or url.rstrip("/") == base_url.rstrip("/"):
            continue
        if len(text) > len(found.get(url, "")):
            found[url] = text
    return [(title, url) for url, title in found.items()]


def fetch_page(company: str, url: str, location: str = "", max_details: int = 25) -> list[Job]:
    """A company that publishes no feed (Perrone Robotics: a careers page with one page per role). Read
    the careers page once per scan, politely, take the links that look like job postings, and use each
    linked page as the description. The company entry supplies the location when a page does not."""
    page = http_text(url, headers={"Accept": "text/html,application/xhtml+xml"})
    jobs: list[Job] = []
    for n, (title, link) in enumerate(page_job_links(page, url)):
        desc = ""
        if n < max_details:
            try:
                desc = strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ",
                                         http_text(link, headers={"Accept": "text/html"})))[:12000]
            except FetchError:
                pass
        jid = hashlib.sha1(link.encode()).hexdigest()[:12]
        jobs.append(_job("page", company, jid, title, link, link, location,
                         "remote" if re.search(r"\bremote\b", desc[:3000], re.I) else None, None, desc))
    return jobs


def fetch_workday(company: str, tenant: str, wd: str, site: str, queries: list[str],
                  max_details: int = 400, max_results: int = 1000,
                  title_filter: Optional[Callable[[str], bool]] = None) -> list[Job]:
    """Search a Workday career site. Workday has no public feed, but its career sites expose
    the JSON endpoints their own front-end uses. We run a few searchText queries, then fetch
    details (description, remote type) for postings that pass the optional title filter.

    Workday charges one request per posting for the description, so `max_details` is a real budget:
    a posting past it is kept with an EMPTY description, which the model then has to judge on its
    title alone. Both caps are configurable per company for exactly that reason -- the old defaults
    (40 details, 200 results) silently blanked two thirds of a mid-sized board."""
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    seen: dict[str, dict] = {}
    for q in queries:
        offset = 0
        while offset < max_results:
            body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}).encode()
            data = http_json(f"{api}/jobs", method="POST", body=body,
                             headers={"Accept": "application/json", "Content-Type": "application/json"})
            postings = data.get("jobPostings") or []
            for p in postings:
                path = p.get("externalPath")
                if path and path not in seen:
                    seen[path] = p
            total = int(data.get("total") or 0)
            offset += 20
            if offset >= total or not postings:
                break
    jobs: list[Job] = []
    details_fetched = 0
    for path, p in seen.items():
        title = (p.get("title") or "").strip()
        if title_filter and not title_filter(title):
            continue
        desc, remote_signal, loc = "", None, p.get("locationsText") or ""
        if details_fetched < max_details:   # past the budget the posting keeps an empty description
            try:
                d = http_json(f"{api}{path}")
                info = d.get("jobPostingInfo") or {}
                desc = strip_html(info.get("jobDescription"))
                loc = "; ".join(x for x in [info.get("location") or loc] +
                                list(info.get("additionalLocations") or []) if x)
                rt = str(info.get("remoteType") or "").lower()
                if "remote" in rt:
                    remote_signal = "remote"
                elif "hybrid" in rt:
                    remote_signal = "hybrid"
                elif rt:
                    remote_signal = "onsite"
                details_fetched += 1
            except FetchError:
                pass
        jid = path.rsplit("_", 1)[-1] if "_" in path else path.strip("/").replace("/", "-")
        jobs.append(Job(
            id=f"workday:{tenant}:{jid}",
            source="workday",
            company=company,
            title=title,
            url=f"{base}/en-US/{site}{path}",
            apply_url=f"{base}/en-US/{site}{path}",
            location=loc,
            remote_signal=remote_signal,
            posted_at=workday_posted_on_to_iso(p.get("postedOn")),
            description=desc,
            salary_text=None,
            meta={"posted_on": p.get("postedOn"), "bullets": p.get("bulletFields")},
        ))
    return jobs


# ----------------------------------------------------------------------------- Hacker News

def fetch_hn_who_is_hiring(max_comments: int = 3000, month_hint: Optional[str] = None) -> list[Job]:
    """Top-level comments of the latest 'Ask HN: Who is hiring?' thread, as Job records."""
    q = urllib.parse.quote('"Who is hiring"')
    stories = http_json(f"https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story,author_whoishiring&hitsPerPage=5")
    hits = [h for h in stories.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
    if month_hint:
        hits = [h for h in hits if month_hint.lower() in (h.get("title") or "").lower()] or hits
    if not hits:
        return []
    story = hits[0]
    story_id = story["objectID"]
    jobs: list[Job] = []
    page = 0
    while len(jobs) < max_comments:
        data = http_json(f"https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{story_id}&hitsPerPage=1000&page={page}")
        comments = data.get("hits") or []
        if not comments:
            break
        for c in comments:
            if str(c.get("parent_id")) != str(story_id):
                continue  # replies, not postings
            text = strip_html(c.get("comment_text"))
            if not text:
                continue
            first_line = text.split("\n", 1)[0].strip()
            segs = [s.strip() for s in re.split(r"\s*\|\s*", first_line) if s.strip()]
            company = segs[0][:80] if segs else "HN poster"
            title = first_line[:160]
            loc_bits = [s for s in segs[1:] if re.search(r"remote|onsite|on-site|hybrid|\b(us|usa|united states|nyc|sf)\b|,\s*[A-Z]{2}\b", s, re.I)]
            jobs.append(Job(
                id=f"hn:{story_id}:{c.get('objectID')}",
                source="hn",
                company=company,
                title=title,
                url=f"https://news.ycombinator.com/item?id={c.get('objectID')}",
                apply_url=f"https://news.ycombinator.com/item?id={c.get('objectID')}",
                location="; ".join(loc_bits) or first_line[:120],
                remote_signal="remote" if re.search(r"\bremote\b", first_line, re.I) else None,
                posted_at=to_iso(c.get("created_at")),
                description=text,
                salary_text=None,
                meta={"author": c.get("author"), "thread": story.get("title")},
            ))
        page += 1
        if page >= int(data.get("nbPages") or 1):
            break
    return jobs



# ----------------------------------------------------------------------------- USAJOBS

def fetch_usajobs(company: str, queries: list[str], api_key: str, email: str, locations: list[str],
                  radius_miles: int = 50, remote: bool = True,
                  title_filter: Optional[Callable[[str], bool]] = None) -> list[Job]:
    """Federal postings via the USAJOBS search API. Needs USAJOBS_API_KEY and USAJOBS_EMAIL in .env
    (free at https://developer.usajobs.gov/apirequest/). Runs each query once for remote-eligible
    postings and once per configured location."""
    if not api_key or not email:
        raise FetchError("USAJOBS_API_KEY / USAJOBS_EMAIL not set in .env (free key: developer.usajobs.gov)")
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": api_key}
    seen: dict[str, dict] = {}
    searches: list[dict] = []
    for q in queries:
        if remote:
            searches.append({"Keyword": q, "RemoteIndicator": "True"})
        for loc in locations:
            searches.append({"Keyword": q, "LocationName": loc, "Radius": str(radius_miles)})
    for params in searches:
        page = 1
        while page <= 5:
            qs = urllib.parse.urlencode({**params, "ResultsPerPage": "250", "Page": str(page)})
            data = http_json(f"https://data.usajobs.gov/api/search?{qs}", headers=headers)
            sr = data.get("SearchResult") or {}
            items = sr.get("SearchResultItems") or []
            for it in items:
                d = it.get("MatchedObjectDescriptor") or {}
                pid = str(it.get("MatchedObjectId") or d.get("PositionID") or "")
                if pid and pid not in seen:
                    seen[pid] = d
            total_pages = int(sr.get("UserArea", {}).get("NumberOfPages") or 1)
            if page >= total_pages or not items:
                break
            page += 1
    jobs: list[Job] = []
    for pid, d in seen.items():
        title = (d.get("PositionTitle") or "").strip()
        if title_filter and not title_filter(title):
            continue
        ua = (d.get("UserArea") or {}).get("Details") or {}
        remote_flag = str(ua.get("RemoteIndicator") or "").lower() == "true"
        telework = str(ua.get("TeleworkEligible") or "").lower() == "true"
        pos_locs = [l for l in (d.get("PositionLocation") or []) if isinstance(l, dict)]
        locs = [l.get("LocationName", "") for l in pos_locs]
        # the API hands us real coordinates per location; geo.py would otherwise have to guess
        coords = [(float(l["Latitude"]), float(l["Longitude"])) for l in pos_locs
                  if l.get("Latitude") not in (None, "") and l.get("Longitude") not in (None, "")][:6]
        parts_loc: list[str] = []
        for x in [d.get("PositionLocationDisplay") or ""] + locs[:6]:
            if x and x not in parts_loc:
                parts_loc.append(x)
        loc = "; ".join(parts_loc)
        if remote_flag and "remote" not in loc.lower():
            loc = "Remote (US); " + loc
        salary = None
        for r in d.get("PositionRemuneration") or []:
            if isinstance(r, dict) and r.get("MinimumRange"):
                salary = f"${r.get('MinimumRange')} - ${r.get('MaximumRange')} {r.get('RateIntervalCode', '')}".strip()
                break
        parts = [strip_html(ua.get("JobSummary")), strip_html(ua.get("MajorDuties") if isinstance(ua.get("MajorDuties"), str)
                 else "\n".join(ua.get("MajorDuties") or [])), strip_html(ua.get("Requirements")),
                 strip_html(ua.get("Education")), strip_html(ua.get("Evaluations"))]
        agency = d.get("OrganizationName") or d.get("DepartmentName") or company
        jobs.append(Job(
            id=f"usajobs:{pid}",
            source="usajobs",
            company=agency,
            title=title,
            url=d.get("PositionURI") or "",
            apply_url=(d.get("ApplyURI") or [d.get("PositionURI") or ""])[0],
            location=loc,
            remote_signal="remote" if remote_flag else ("hybrid" if telework else None),
            posted_at=to_iso(d.get("PublicationStartDate")),
            description="\n".join(p for p in parts if p),
            salary_text=salary,
            meta={"department": d.get("DepartmentName"), "grade": ua.get("LowGrade") and f"{ua.get('LowGrade')}-{ua.get('HighGrade')}",
                  "hiring_paths": ua.get("HiringPath"), "closes": d.get("ApplicationCloseDate"), "telework": telework,
                  "coords": coords},
        ))
    return jobs



# ----------------------------------------------------------------------------- public job boards

# Where a posting is, is decided once, by classify.gates.location_gate, against the profile's own
# vocabulary and the place gazetteer. Fetchers used to re-decide it here with their own regexes,
# which meant a foreign posting was dropped before it reached the cache and `rescore` could never
# replay that decision. They now return everything and let the gate do its job.

def _job(source: str, company: str, jid: str, title: str, url: str, apply_url: str, location: str,
         remote_signal: Optional[str], posted_at, description: str, salary_text: Optional[str] = None, meta=None) -> Job:
    return Job(id=f"{source}:{jid}", source=source, company=(company or "").strip() or "?", title=(title or "").strip(),
               url=url or "", apply_url=apply_url or url or "", location=location or "", remote_signal=remote_signal,
               posted_at=to_iso(posted_at), description=description or "", salary_text=salary_text, meta=meta or {})


def fetch_remotive(company: str, categories: Optional[list[str]] = None, limit: int = 2000) -> list[Job]:
    """https://remotive.com/api/remote-jobs — remote jobs; location field says which countries qualify."""
    jobs: list[Job] = []
    for cat in categories or ["software-dev", "data", "product"]:
        data = http_json(f"https://remotive.com/api/remote-jobs?category={urllib.parse.quote(cat)}&limit={limit}")
        for j in data.get("jobs", []):
            loc = j.get("candidate_required_location") or ""
            jobs.append(_job("remotive", j.get("company_name"), str(j.get("id")), j.get("title"), j.get("url"), j.get("url"),
                             f"Remote ({loc})" if loc else "Remote", "remote", j.get("publication_date"),
                             strip_html(j.get("description")), j.get("salary") or None,
                             {"category": j.get("category"), "tags": j.get("tags"), "job_type": j.get("job_type")}))
    return jobs


def fetch_remoteok(company: str) -> list[Job]:
    """https://remoteok.com/api — the first element is a legal notice, the rest are postings."""
    data = http_json("https://remoteok.com/api")
    jobs: list[Job] = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue
        smin, smax = j.get("salary_min"), j.get("salary_max")
        salary = f"${smin} - ${smax}" if smin and smax and str(smin) != "0" else None
        loc = j.get("location") or ""
        jobs.append(_job("remoteok", j.get("company"), str(j.get("id")), j.get("position"), j.get("url"), j.get("apply_url") or j.get("url"),
                         f"Remote ({loc})" if loc else "Remote", "remote", j.get("date"), strip_html(j.get("description")), salary,
                         {"tags": j.get("tags")}))
    return jobs


def fetch_arbeitnow(company: str, max_pages: int = 8) -> list[Job]:
    """https://www.arbeitnow.com/api/job-board-api — mostly European; the location filter drops those later."""
    jobs: list[Job] = []
    for page in range(1, max_pages + 1):
        data = http_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        for j in data.get("data", []):
            remote = str(j.get("remote")).lower() == "true"
            jobs.append(_job("arbeitnow", j.get("company_name"), j.get("slug"), j.get("title"), j.get("url"), j.get("url"),
                             (j.get("location") or "") + (" (Remote)" if remote else ""), "remote" if remote else None,
                             j.get("created_at"), strip_html(j.get("description")), None, {"tags": j.get("tags")}))
        if not (data.get("links") or {}).get("next"):
            break
    return jobs


def fetch_himalayas(company: str, max_pages: int = 20, page_size: int = 100) -> list[Job]:
    """https://himalayas.app/jobs/api — remote jobs with per-country restrictions."""
    jobs: list[Job] = []
    offset = 0
    for _ in range(max_pages):
        data = http_json(f"https://himalayas.app/jobs/api?limit={page_size}&offset={offset}")
        items = data.get("jobs") or []
        for j in items:
            countries = [c for c in (j.get("locationRestrictions") or []) if isinstance(c, str)]
            loc = "Remote (" + ", ".join(countries[:4]) + ")" if countries else "Remote"
            smin, smax = j.get("minSalary"), j.get("maxSalary")
            salary = f"{j.get('currency') or ''} {smin}-{smax} {j.get('salaryPeriod') or ''}".strip() if smin and smax else None
            jobs.append(_job("himalayas", j.get("companyName"), (j.get("guid") or j.get("applicationLink") or "").rsplit("/", 1)[-1],
                             j.get("title"), j.get("applicationLink") or j.get("guid"), j.get("applicationLink") or j.get("guid"),
                             loc, "remote", j.get("pubDate"), strip_html(j.get("description")) or j.get("excerpt") or "", salary,
                             {"seniority": j.get("seniority"), "categories": j.get("categories")}))
        if len(items) < page_size:
            break
        offset += page_size
    return jobs


def fetch_themuse(company: str, categories: Optional[list[str]] = None, max_pages: int = 20,
                  locations: Optional[list[str]] = None) -> list[Job]:
    """https://www.themuse.com/api/public/jobs — paged by category and location.

    `locations` is asked of the API one at a time. It used to be hardcoded to "Flexible / Remote",
    which meant a Muse job in any named city was never even requested."""
    jobs: list[Job] = []
    wanted = locations or ["Flexible / Remote"]
    for cat in categories or ["Data and Analytics", "Software Engineering", "Data Science"]:
      for where in wanted:
        for page in range(1, max_pages + 1):
            q = urllib.parse.urlencode({"page": page, "category": cat, "location": where})
            data = http_json(f"https://www.themuse.com/api/public/jobs?{q}")
            for j in data.get("results", []):
                locs = [l.get("name", "") for l in (j.get("locations") or []) if isinstance(l, dict)]
                remote = any("remote" in l.lower() for l in locs)
                comp = (j.get("company") or {}).get("name") or ""
                url = ((j.get("refs") or {}).get("landing_page")) or ""
                jobs.append(_job("themuse", comp, str(j.get("id")), j.get("name"), url, url, "; ".join(locs),
                                 "remote" if remote else None, j.get("publication_date"), strip_html(j.get("contents")), None,
                                 {"levels": [l.get("name") for l in (j.get("levels") or []) if isinstance(l, dict)],
                                  "category": cat, "muse_location": where}))
            if page >= int(data.get("page_count") or 1):
                break
    return jobs


# ----------------------------------------------------------------------------- aggregators with keys

def fetch_adzuna(company: str, queries: list[str], app_id: str, app_key: str, country: str = "us",
                 max_pages: int = 4, per_page: int = 50, locations: Optional[list[str]] = None,
                 distance_miles: int = 0) -> list[Job]:
    """https://developer.adzuna.com — descriptions are truncated by the API; the link opens the source.

    `locations` + `distance_miles` map to Adzuna's own `where`/`distance` parameters, which is how a
    keyword sweep gets anchored to a region instead of the whole country."""
    if not app_id or not app_key:
        raise FetchError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env (free key: developer.adzuna.com)")
    jobs: list[Job] = []
    seen: set[str] = set()
    for q in queries:
      for where in (locations or [None]):
        for page in range(1, max_pages + 1):
            fields = {"app_id": app_id, "app_key": app_key, "what": q, "results_per_page": per_page,
                      "content-type": "application/json"}
            if where:
                fields["where"] = where
                if distance_miles:
                    fields["distance"] = distance_miles
            params = urllib.parse.urlencode(fields)
            data = http_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?{params}")
            results = data.get("results") or []
            for j in results:
                jid = str(j.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                loc = (j.get("location") or {}).get("display_name") or ""
                smin, smax = j.get("salary_min"), j.get("salary_max")
                salary = f"${int(smin)} - ${int(smax)}" if smin and smax else None
                jobs.append(_job("adzuna", (j.get("company") or {}).get("display_name"), jid, j.get("title"),
                                 j.get("redirect_url"), j.get("redirect_url"), loc,
                                 "remote" if "remote" in (loc + " " + (j.get("title") or "")).lower() else None,
                                 j.get("created"), strip_html(j.get("description")), salary,
                                 {"query": q, "where": where, "category": (j.get("category") or {}).get("label")}))
            if len(results) < per_page:
                break
    return jobs


def fetch_jooble(company: str, queries: list[str], key: str,
                 locations: Optional[list[str]] = None, max_pages: int = 3) -> list[Job]:
    """https://jooble.org/api/about — POST per query and location; snippets only, link opens the source.

    Each location is asked separately; "United States" alone returns a national sample that badly
    under-covers any particular region."""
    if not key:
        raise FetchError("JOOBLE_KEY not set in .env (free key: jooble.org/api/about)")
    jobs: list[Job] = []
    seen: set[str] = set()
    for q in queries:
      for location in (locations or ["United States"]):
        for page in range(1, max_pages + 1):
            body = json.dumps({"keywords": q, "location": location, "page": page}).encode()
            data = http_json(f"https://jooble.org/api/{key}", method="POST", body=body)
            items = data.get("jobs") or []
            for j in items:
                jid = str(j.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                loc = j.get("location") or ""
                jobs.append(_job("jooble", j.get("company"), jid, j.get("title"), j.get("link"), j.get("link"), loc,
                                 "remote" if "remote" in (loc + " " + (j.get("title") or "")).lower() else None,
                                 j.get("updated"), strip_html(j.get("snippet")), j.get("salary") or None, {"query": q, "type": j.get("type")}))
            if not items:
                break
    return jobs


# ----------------------------------------------------------------------------- dispatch

# Sources whose allowance is finite and does not refill on its own. The ATS feeds are absent on
# purpose: they are an employer publishing their own jobs, and there is nothing to ration.
METERED = ("adzuna", "jooble", "serpapi", "usajobs")

# What each metered source needs in .env before it can make a request at all. Budget is only spent
# once the credentials exist: without them the fetcher raises and nothing is sent, and charging for
# that would retire a source that had never been used. Six keyless scans used to spend 216 of
# Adzuna's daily 250.
CREDENTIALS = {"adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"), "jooble": ("JOOBLE_KEY",),
               "serpapi": ("SERPAPI_KEY",), "usajobs": ("USAJOBS_API_KEY", "USAJOBS_EMAIL")}


def has_credentials(ats: str, env: dict) -> bool:
    return all(env.get(k) for k in CREDENTIALS.get(ats, ()))


def planned_requests(entry: dict) -> int:
    """How many requests this entry will make, before making any of them.

    Every metered source is a product of its query list, its location list and its page count, so
    the cost is knowable in advance -- which is the only point at which refusing is useful. An
    estimate that is slightly high is the safe direction."""
    ats = (entry.get("ats") or "").lower()
    if ats not in METERED:
        return 0
    queries = max(1, len(entry.get("queries") or [1]))
    places = max(1, len(entry.get("locations") or [1]))
    pages = int(entry.get("max_pages", 5 if ats == "usajobs" else 3))
    return queries * places * max(1, pages)


def fetch_serpapi(company: str, queries: list[str], key: str,
                  locations: Optional[list[str]] = None, max_pages: int = 1) -> list[Job]:
    """https://serpapi.com — Google Jobs, which is the only legitimate route to the four largest US
    boards.

    LinkedIn, Indeed, ZipRecruiter and Glassdoor all close their APIs to the public; Google indexes
    all four, and SerpApi reads Google. Each posting carries `via` saying which board it came from,
    and `apply_options` usually links straight to the employer's own posting, which is the link
    worth following.

    The free tier is 250 searches a MONTH, so this is deliberately small: one page per query by
    default, and [quota] serpapi.per_day holds it to roughly eight a day."""
    if not key:
        raise FetchError("SERPAPI_KEY not set in .env (free tier, 250 searches/month: serpapi.com)")
    jobs: list[Job] = []
    seen: set[str] = set()
    for q in queries:
      for where in (locations or ["United States"]):
        for page in range(max_pages):
            params = {"engine": "google_jobs", "q": q, "location": where,
                      "hl": "en", "gl": "us", "api_key": key}
            if page:
                params["start"] = page * 10
            data = http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
            if data.get("error"):
                raise FetchError(f"serpapi: {data['error']}")
            results = data.get("jobs_results") or []
            for j in results:
                jid = str(j.get("job_id") or "")[:120]
                title = j.get("title") or ""
                if not jid or not title or jid in seen:
                    continue
                seen.add(jid)
                opts = j.get("apply_options") or []
                link = (opts[0].get("link") if opts and isinstance(opts[0], dict) else "") or j.get("share_link") or ""
                ext = j.get("detected_extensions") or {}
                jobs.append(_job("serpapi", j.get("company_name"), jid, title, link, link,
                                 j.get("location") or where,
                                 "remote" if ext.get("work_from_home") else None,
                                 to_iso(ext.get("posted_at")), strip_html(j.get("description") or ""),
                                 ext.get("salary"),
                                 {"via": j.get("via"), "query": q, "where": where}))
            if not results:
                break
    return jobs


def fetch_company(entry: dict, title_filter: Optional[Callable[[str], bool]] = None,
                  env: Optional[dict] = None, quota_dir: Optional[Path] = None,
                  quota_cfg: Optional[dict] = None) -> list[Job]:
    ats = (entry.get("ats") or "").lower()
    name = entry.get("name") or entry.get("board") or entry.get("slug") or "?"
    env = env or {}
    # Check the budget before the first request rather than discovering it through 429s. A source
    # over its ceiling raises FetchError, which fetch_all already logs and steps past.
    if quota_dir is not None and ats in METERED and has_credentials(ats, env):
        n = planned_requests(entry)
        per_day, lifetime = quota.limits_for(quota_cfg or {}, ats)
        if n and (per_day or lifetime):
            try:
                quota.take(quota_dir, ats, n, per_day=per_day, lifetime=lifetime)
            except quota.QuotaExceeded as e:
                raise FetchError(str(e)) from e
    if ats == "greenhouse":
        jobs = fetch_greenhouse(name, entry["board"])
    elif ats == "lever":
        jobs = fetch_lever(name, entry.get("slug") or entry["board"])
    elif ats == "ashby":
        jobs = fetch_ashby(name, entry["board"])
    elif ats == "smartrecruiters":
        jobs = fetch_smartrecruiters(name, entry["board"],
                                     max_details=int(entry.get("max_details", 400)),
                                     max_results=int(entry.get("max_results", 1000)),
                                     title_filter=title_filter)
    elif ats == "workable":
        jobs = fetch_workable(name, entry["board"])
    elif ats == "breezy":
        jobs = fetch_breezy(name, entry["board"])
    elif ats == "recruitee":
        jobs = fetch_recruitee(name, entry["board"])
    elif ats == "bamboohr":
        jobs = fetch_bamboohr(name, entry["board"], int(entry.get("max_details", 60)))
    elif ats == "rippling":
        jobs = fetch_rippling(name, entry["board"], int(entry.get("max_details", 60)))
    elif ats == "page":
        jobs = fetch_page(name, entry["url"], entry.get("location") or "", int(entry.get("max_details", 25)))
    elif ats == "workday":
        jobs = fetch_workday(name, entry["tenant"], entry.get("wd", "wd5"), entry["site"],
                             entry.get("queries") or ["forward deployed", "solutions architect", "applied AI"],
                             max_details=int(entry.get("max_details", 400)),
                             max_results=int(entry.get("max_results", 1000)),
                             title_filter=title_filter)
    elif ats == "remotive":
        jobs = fetch_remotive(name, entry.get("categories"))
    elif ats == "remoteok":
        jobs = fetch_remoteok(name)
    elif ats == "arbeitnow":
        jobs = fetch_arbeitnow(name, int(entry.get("max_pages", 8)))
    elif ats == "himalayas":
        jobs = fetch_himalayas(name, int(entry.get("max_pages", 20)))
    elif ats == "themuse":
        jobs = fetch_themuse(name, entry.get("categories"), int(entry.get("max_pages", 20)),
                             locations=entry.get("locations"))
    elif ats == "adzuna":
        jobs = fetch_adzuna(name, entry.get("queries") or ["machine learning engineer"], env.get("ADZUNA_APP_ID", ""),
                            env.get("ADZUNA_APP_KEY", ""), entry.get("country", "us"), int(entry.get("max_pages", 4)),
                            locations=entry.get("locations"), distance_miles=int(entry.get("distance_miles", 0)))
    elif ats == "serpapi":
        jobs = fetch_serpapi(name, entry.get("queries") or ["machine learning engineer"],
                             env.get("SERPAPI_KEY", ""), entry.get("locations"),
                             int(entry.get("max_pages", 1)))
    elif ats == "jooble":
        jobs = fetch_jooble(name, entry.get("queries") or ["machine learning engineer"], env.get("JOOBLE_KEY", ""),
                            entry.get("locations") or [entry.get("location", "United States")],
                            int(entry.get("max_pages", 3)))
    elif ats == "usajobs":
        jobs = fetch_usajobs(name, entry.get("queries") or ["data scientist"], api_key=env.get("USAJOBS_API_KEY", ""),
                             email=env.get("USAJOBS_EMAIL", ""), locations=entry.get("locations") or [],
                             radius_miles=int(entry.get("radius_miles", 50)), remote=bool(entry.get("remote", True)),
                             title_filter=title_filter)
    else:
        raise FetchError(f"unknown ats {ats!r} for {name}")
    for j in jobs:
        j.board = name
    return jobs
