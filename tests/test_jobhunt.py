"""Run with:  uv run python -m unittest discover -s tests -v   (from the project root)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobhunt import geo, llm  # noqa: E402
from jobhunt.classify import facts, judge as judgemod, learn  # noqa: E402
from jobhunt.classify.learn import canonical_name  # noqa: E402
from jobhunt.fetch import sources  # noqa: E402
from jobhunt.report import about  # noqa: E402
from jobhunt.apply import answers as bankmod  # noqa: E402
from jobhunt.apply import runs as runsmod  # noqa: E402
from jobhunt.core import Store, load_toml, profile_facts, relearn, rescore, scan  # noqa: E402
from jobhunt.classify.filters import Matcher  # noqa: E402
from jobhunt.profiles import load_all, load_profile, merge_config  # noqa: E402
from jobhunt.fetch.sources import Job  # noqa: E402
from jobhunt import pipeline  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
# The suite runs against the tracked example profiles, never a real one: real profiles hold contact
# details and demographics and are gitignored, so a fresh clone must still be able to test.
ENGINEER = ROOT / "profiles" / "example-engineer"
ANALYST = ROOT / "profiles" / "example-analyst"


def _fixture_toml(path: Path, *required: str) -> dict:
    """Load a tracked config the whole suite is built on.

    These run at import time, so a malformed file would otherwise fail collection of all 48 tests
    with a bare KeyError and no clue which file was wrong."""
    try:
        cfg = load_toml(path)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"tests cannot start: {path.relative_to(ROOT)} did not parse: {e}") from e
    missing = [k for k in required if k not in cfg]
    if missing:
        raise SystemExit(f"tests cannot start: {path.relative_to(ROOT)} is missing {', '.join(missing)}")
    return cfg


SHARED = _fixture_toml(ROOT / "config.toml", "location", "scoring", "companies")
ENGINEER_SEARCH = _fixture_toml(ENGINEER / "search.toml", "search", "location")
ANALYST_SEARCH = _fixture_toml(ANALYST / "search.toml", "search", "location")
CFG = merge_config(SHARED, ENGINEER_SEARCH)          # the engineer profile's effective config
PROFILE = _fixture_toml(ENGINEER / "profile.toml", "identity", "documents", "answers")
FULL_NAME = f"{PROFILE['identity']['first_name']} {PROFILE['identity']['last_name']}"

GH_FEED = {"jobs": [
    {"id": 1, "title": "Applied AI Engineer, Enterprise", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/1",
     "location": {"name": "Remote - United States"}, "updated_at": "2026-09-01T10:00:00-04:00",
     "first_published": "2026-08-20T10:00:00-04:00", "metadata": [{"name": "Location Type", "value": "Remote"}],
     "content": "&lt;p&gt;Build agentic LLM applications with customers. Travel up to 20% to customer sites. "
                "The annual salary range is $180,000 - $240,000.&lt;/p&gt;"},
    {"id": 2, "title": "Applied AI Architect, Public Sector", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/2",
     "location": {"name": "Washington, DC"}, "updated_at": "2026-09-01T10:00:00-04:00",
     "content": "This position requires U.S. citizenship and an active TS/SCI security clearance. Travel frequently to customer sites."},
    {"id": 3, "title": "Applied AI Architect", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/3",
     "location": {"name": "London, UK"}, "updated_at": "2026-09-01T10:00:00-04:00", "content": "London role"},
    {"id": 4, "title": "Account Executive, AI Native", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/4",
     "location": {"name": "New York City, NY"}, "updated_at": "2026-09-01T10:00:00-04:00", "content": "Sales"},
    {"id": 5, "title": "Forward Deployed Engineer - Federal", "absolute_url": "https://www.example.com/careers?gh_jid=5",
     "location": {"name": "Maryland; Virginia; Washington, D.C."}, "updated_at": "2026-09-01T10:00:00-04:00",
     "content": "This role can be remote. Willing to travel once every 4-8 weeks to see customers. U.S. citizenship and "
                "eligibility for a U.S. government secret clearance are required. $182,000—$250,208 USD"},
    {"id": 6, "title": "Senior Data Scientist, Health Outcomes", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/6",
     "location": {"name": "Columbus, OH"}, "updated_at": "2026-09-01T10:00:00-04:00",
     "metadata": [{"name": "Workplace Type", "value": "Hybrid"}],
     "content": "Biostatistics, survey methodology, R and SAS. Causal inference on public health data. $110,000 - $140,000."},
    {"id": 7, "title": "Applied AI Engineer, Enterprise", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/7",
     "location": {"name": "New York, NY"}, "updated_at": "2026-09-01T10:00:00-04:00",
     "content": "Same role as posting 1, posted for the New York office."},
    {"id": 8, "title": "Deployment Strategist, Public Sector", "absolute_url": "https://job-boards.greenhouse.io/ex/jobs/8",
     "location": {"name": "Philadelphia, PA"}, "updated_at": "2026-09-01T10:00:00-04:00",
     "content": "Onsite in Philadelphia: inside the search radius but well beyond a daily commute."},
]}
LEVER_FEED = [
    {"id": "abc", "text": "Forward Deployed Software Engineer", "hostedUrl": "https://jobs.lever.co/ex/abc",
     "applyUrl": "https://jobs.lever.co/ex/abc/apply", "categories": {"location": "Washington, D.C.", "team": "Deployments",
     "commitment": "Full-time", "allLocations": ["Washington, D.C.", "New York, NY"]}, "workplaceType": "hybrid",
     "descriptionPlain": "Expect 25% travel with up to 50% travel as necessary.", "lists": [{"text": "What you'll do",
     "content": "<li>Embed with customers</li>"}], "additionalPlain": "", "createdAt": 1756000000000},
    {"id": "def", "text": "Backend Software Engineer", "hostedUrl": "https://jobs.lever.co/ex/def",
     "categories": {"location": "London, United Kingdom"}, "workplaceType": "on-site", "descriptionPlain": "x", "createdAt": 1756000000000},
]
ASHBY_FEED = {"jobs": [
    {"id": "u1", "title": "Enterprise Solutions Engineer - North America", "department": "GTM", "team": "Solutions",
     "employmentType": "FullTime", "location": "United States", "secondaryLocations": [], "publishedAt": "2026-08-30T00:00:00.000+00:00",
     "isListed": True, "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/ex/u1", "descriptionPlain": "Remote-first. Expect regular travel for customer onsites.",
     "compensation": {"scrapeableCompensationSalarySummary": "$150K – $200K • Offers Equity"}},
    {"id": "u2", "title": "Software Engineer, Platform", "location": "San Francisco", "secondaryLocations": [], "isListed": True,
     "isRemote": False, "jobUrl": "https://jobs.ashbyhq.com/ex/u2", "descriptionPlain": "Onsite in SF."},
    {"id": "u3", "title": "Software Engineer, Agents Platform", "location": "Remote", "secondaryLocations": [], "isListed": True,
     "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/ex/u3", "descriptionPlain": "U.S. Person status is required. Build agent frameworks, LangGraph, evals."},
]}
HN_STORIES = {"hits": [{"objectID": "111", "title": "Ask HN: Who is hiring? (September 2026)", "created_at": "2026-09-01T15:01:17Z"}]}
HN_COMMENTS = {"nbPages": 1, "hits": [
    {"objectID": "222", "parent_id": 111, "story_id": 111, "created_at": "2026-09-02T00:00:00Z", "author": "x",
     "comment_text": "Example Robotics | Forward Deployed Engineer | Remote (US) | Full-time<p>We build autonomy for ground robots. Python, RL."},
    {"objectID": "333", "parent_id": 222, "story_id": 111, "created_at": "2026-09-02T00:00:00Z", "author": "y", "comment_text": "reply, not a posting"},
]}
USAJOBS_FEED = {"SearchResult": {"UserArea": {"NumberOfPages": "1"}, "SearchResultItems": [
    {"MatchedObjectId": "800001", "MatchedObjectDescriptor": {
        "PositionID": "CB-2026-1", "PositionTitle": "Survey Statistician", "PositionURI": "https://www.usajobs.gov/job/800001",
        "ApplyURI": ["https://www.usajobs.gov/job/800001?PostingChannelID=RESTAPI"],
        "PositionLocationDisplay": "Anywhere in the U.S. (remote job)", "PositionLocation": [{"LocationName": "Anywhere in the U.S. (remote job)"}],
        "OrganizationName": "Example Statistics Office", "DepartmentName": "Department of Commerce",
        "PositionRemuneration": [{"MinimumRange": "99200.0", "MaximumRange": "153354.0", "RateIntervalCode": "Per Year"}],
        "PublicationStartDate": "2026-09-10T00:00:00.0000", "ApplicationCloseDate": "2026-09-30T23:59:59.9970",
        "UserArea": {"Details": {"JobSummary": "Design samples and weighting for household surveys.", "MajorDuties": ["Sampling", "Imputation"],
                                 "RemoteIndicator": True, "TeleworkEligible": True, "LowGrade": "12", "HighGrade": "13", "HiringPath": ["public"]}}}},
]}}


SMARTRECRUITERS_FEED = {"offset": 0, "limit": 100, "totalFound": 2, "content": [
    {"id": "s1", "name": "Applied AI Engineer", "company": {"identifier": "ex", "name": "Ex Smart"},
     "releasedDate": "2026-09-02T10:00:00.000Z", "postingUrl": "https://jobs.smartrecruiters.com/ex/s1",
     "applyUrl": "https://jobs.smartrecruiters.com/ex/s1?oga=true",
     "location": {"city": "Columbus", "region": "OH", "country": "us", "remote": False, "hybrid": True},
     "department": {"label": "Engineering"}},
    {"id": "s2", "name": "Forward Deployed Engineer", "company": {"identifier": "ex", "name": "Ex Smart"},
     "releasedDate": "2026-09-05T10:00:00.000Z", "postingUrl": "https://jobs.smartrecruiters.com/ex/s2",
     "location": {"city": "Austin", "region": "TX", "country": "us", "remote": True, "hybrid": False}}]}

WORKABLE_FEED = {"name": "Ex Workable", "jobs": [
    {"shortcode": "W1", "title": "Machine Learning Engineer", "city": "Remote", "state": "", "country": "United States",
     "telecommuting": True, "published_on": "2026-09-08", "url": "https://apply.workable.com/ex/j/W1/",
     "application_url": "https://apply.workable.com/ex/j/W1/apply/", "department": "Eng",
     "description": "<p>Train models.</p>", "requirements": "<p>PyTorch.</p>"}]}

BREEZY_FEED = [
    {"id": "b1", "friendly_id": "b1-fde", "name": "Forward Deployed Engineer",
     "url": "https://ex.breezy.hr/p/b1-fde", "published_date": "2026-09-10T00:00:00.000Z",
     "type": {"name": "Full-Time"}, "department": {"name": "Engineering"},
     "location": {"country": {"name": "United States", "id": "US"}, "state": {"name": "Ohio"},
                  "city": "Columbus", "is_remote": False}}]

RECRUITEE_FEED = {"offers": [
    {"id": 991, "title": "Applied AI Engineer", "city": "Boston", "country_name": "United States",
     "remote": True, "published_at": "2026-09-11", "careers_url": "https://ex.recruitee.com/o/applied-ai",
     "careers_apply_url": "https://ex.recruitee.com/o/applied-ai/c/new", "department": "Eng",
     "description": "<p>Agents.</p>", "requirements": "<p>Python.</p>"}]}


def fake_fetcher(url, method, body, headers):
    if "greenhouse.io/v1/boards/ex/jobs" in url:
        return json.dumps(GH_FEED)
    if "greenhouse.io/v1/boards/broken" in url:
        raise sources.FetchError("HTTP 404")
    if "lever.co/v0/postings/ex" in url:
        return json.dumps(LEVER_FEED)
    if "ashbyhq.com/posting-api/job-board/ex" in url:
        return json.dumps(ASHBY_FEED)
    if "smartrecruiters.com/v1/companies/ex/postings/s2" in url:
        return json.dumps({"postingUrl": "https://jobs.smartrecruiters.com/ex/s2", "jobAd": {"sections": {
            "jobDescription": {"text": "<p>Build agents with customers.</p>"},
            "qualifications": {"text": "<p>Python, LLMs.</p>"},
            "additionalInformation": {"text": "<p>Up to 20% travel.</p>"}}}})
    if "smartrecruiters.com/v1/companies/ex/postings" in url:
        return json.dumps(SMARTRECRUITERS_FEED)
    if "smartrecruiters.com/v1/companies/empty/postings" in url:
        return json.dumps({"offset": 0, "limit": 100, "totalFound": 0, "content": []})
    if "apply.workable.com/api/v1/widget/accounts/ex" in url:
        return json.dumps(WORKABLE_FEED)
    if "apply.workable.com/api/v1/widget/accounts/empty" in url:
        return json.dumps({"name": "Nobody", "jobs": []})
    if "ex.breezy.hr/json" in url:
        return json.dumps(BREEZY_FEED)
    if "ex.recruitee.com/api/offers" in url:
        return json.dumps(RECRUITEE_FEED)
    if "ex.bamboohr.com/careers/list" in url:
        return json.dumps({"meta": {"totalCount": 2}, "result": [
            {"id": "7", "jobOpeningName": "Robotics Software Engineer", "departmentLabel": "Engineering",
             "employmentStatusLabel": "Full-Time", "location": {"city": "Dayton", "state": "Ohio"}, "isRemote": None},
            {"id": "9", "jobOpeningName": "Perception Engineer", "location": {"city": None, "state": None}, "isRemote": True}]})
    if "ex.bamboohr.com/careers/7/detail" in url:
        return json.dumps({"result": {"jobOpening": {"description": "<p>Build autonomy stacks.</p>",
                                                     "datePosted": "2026-09-20", "compensation": "$150K - $190K"}}})
    if "ex.bamboohr.com/careers/9/detail" in url:
        raise sources.FetchError("HTTP 500")
    if "api.rippling.com/platform/api/ats/v1/board/ex/jobs/r1" in url:
        return json.dumps({"uuid": "r1", "description": {"company": "<p>We build robots.</p>", "role": "<p>Own perception.</p>"}})
    if "api.rippling.com/platform/api/ats/v1/board/ex/jobs" in url:
        return json.dumps([
            {"uuid": "r1", "name": "Perception Lead", "department": {"label": "Eng"}, "url": "https://ats.rippling.com/ex/jobs/r1",
             "workLocation": {"label": "Columbus, OH"}},
            {"uuid": "r1", "name": "Perception Lead", "workLocation": {"label": "Remote (United States)"}}])
    if url == "https://robots.example/careers/":
        return ('<nav><a href="/careers">Join Our Team</a><a href="/software-engineer">Software Engineer</a>'
                '<a href="/about">About</a></nav><main><a href="/software-engineer">Software Engineer (Autonomy)</a>'
                '<a href="/qa">QA Engineer/Trainer</a><a href="https://other.example/x">Staff Engineer elsewhere</a>'
                '<a href="/news">News</a><a href="https://boards.greenhouse.io/robots">see all jobs</a></main>')
    if url == "https://robots.example/software-engineer":
        return "<html><nav>menu</nav><p>Work on autonomous vehicles. Remote friendly.</p></html>"
    if url == "https://robots.example/qa":
        return "<p>Test vehicles on site.</p>"
    if "author_whoishiring" in url:
        return json.dumps(HN_STORIES)
    if "story_111" in url:
        return json.dumps(HN_COMMENTS)
    if "remotive.com/api/remote-jobs" in url:
        return json.dumps({"jobs": [{"id": 1680495, "url": "https://remotive.com/remote-jobs/x", "title": "Remote ML Engineer", "company_name": "Coalition ",
                                     "category": "Software Development", "job_type": "full_time", "publication_date": "2026-09-11T20:16:48",
                                     "candidate_required_location": "Worldwide", "salary": "$150k", "description": "<p>Build agents</p>"}]})
    if "remoteok.com/api" in url:
        return json.dumps([{"legal": "notice"}, {"id": "1137391", "date": "2026-09-14T00:00:12+00:00", "company": "Sleek", "position": "Junior Payroll Assistant",
                            "description": "<p>x</p>", "location": "", "apply_url": "https://remoteOK.com/remote-jobs/x", "url": "https://remoteOK.com/remote-jobs/x",
                            "salary_min": "0", "salary_max": "0", "tags": []}])
    if "arbeitnow.com/api/job-board-api" in url:
        return json.dumps({"data": [{"slug": "lead-1", "company_name": "GeneralMind", "title": "Lead Full Stack Engineer", "description": "<p>x</p>",
                                     "remote": False, "url": "https://www.arbeitnow.com/jobs/companies/generalmind/lead-1", "location": "Berlin", "created_at": 1789498829}],
                           "links": {"next": None}})
    if "himalayas.app/jobs/api" in url:
        return json.dumps({"jobs": [{"title": "AML Specialist", "companyName": "Sia Partners", "locationRestrictions": ["Singapore"], "pubDate": 1789497825,
                                     "description": "<p>x</p>", "applicationLink": "https://himalayas.app/companies/sia/jobs/aml", "guid": "https://himalayas.app/companies/sia/jobs/aml",
                                     "minSalary": None, "maxSalary": None},
                                    {"title": "Data Scientist", "companyName": "US Co", "locationRestrictions": ["United States"], "pubDate": 1789497825,
                                     "description": "<p>x</p>", "applicationLink": "https://himalayas.app/companies/usco/jobs/ds", "guid": "https://himalayas.app/companies/usco/jobs/ds",
                                     "minSalary": None, "maxSalary": None}], "totalCount": 2})
    if "themuse.com/api/public/jobs" in url:
        return json.dumps({"page": 1, "page_count": 1, "results": [{"id": 18400647, "name": "EHS Compliance Analyst", "contents": "<p>x</p>",
                           "publication_date": "2025-04-03T11:40:40Z", "locations": [{"name": "Brampton, Canada"}, {"name": "Flexible / Remote"}],
                           "levels": [{"name": "Mid Level"}], "refs": {"landing_page": "https://www.themuse.com/jobs/equinixinc/ehs"}, "company": {"name": "Equinix"}}]})
    if "api.adzuna.com" in url:
        return json.dumps({"results": [{"id": 555, "title": "Data Scientist", "description": "Stats work", "redirect_url": "https://adzuna/555",
                                        "location": {"display_name": "Columbus, OH"}, "company": {"display_name": "Acme"}, "created": "2026-09-10T00:00:00Z",
                                        "salary_min": 120000, "salary_max": 150000, "category": {"label": "IT Jobs"}}]})
    if "jooble.org/api/" in url:
        return json.dumps({"jobs": [{"id": 77, "title": "Statistician (Remote)", "location": "Columbus, OH", "snippet": "x", "salary": "", "source": "s",
                                     "type": "Full-time", "link": "https://jooble/77", "company": "Acme Stats", "updated": "2026-09-10T00:00:00"}]})
    if "data.usajobs.gov/api/search" in url:
        assert headers.get("Authorization-Key") == "k" and headers.get("User-Agent") == "e@x"
        return json.dumps(USAJOBS_FEED)
    raise sources.FetchError(f"unexpected url {url}")


# ----------------------------------------------------------------------------- helpers

def _toml_dump(cfg: dict) -> str:
    """Tiny TOML writer sufficient for our config shape (strings, ints, bools, lists, dict-of-ints, list-of-tables)."""
    def key(k):
        return k if k.replace("_", "a").replace("-", "a").isalnum() else json.dumps(k)

    def val(v, path="?"):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x, path) for x in v) + "]"
        raise TypeError(f"_toml_dump cannot write {path} = {v!r} ({type(v).__name__}). It handles "
                        f"scalars, flat lists, [table], [table.subtable] and [[array]] only.")
    out, tables, arrays = [], [], []
    for k, v in cfg.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            arrays.append((k, v))
        else:
            out.append(f"{key(k)} = {val(v, k)}")
    for k, v in tables:
        sub = {kk: vv for kk, vv in v.items() if isinstance(vv, dict)}
        out.append(f"[{k}]")
        out += [f"{key(kk)} = {val(vv, f'{k}.{kk}')}" for kk, vv in v.items() if not isinstance(vv, dict)]
        for sk, sv in sub.items():
            out.append(f"[{k}.{sk}]")
            out += [f"{key(kk)} = {val(vv, f'{k}.{sk}.{kk}')}" for kk, vv in sv.items()]
    for k, rows in arrays:
        for row in rows:
            out.append(f"[[{k}]]")
            out += [f"{key(kk)} = {val(vv, f'{k}[].{kk}')}" for kk, vv in row.items()]
    return "\n".join(out) + "\n"


def make_root(tmp: Path, companies=None, hn=True) -> Path:
    """A throwaway project root with the real vocabularies, fake boards, and two profiles."""
    shared = json.loads(json.dumps(SHARED))
    shared["companies"] = companies if companies is not None else [
        {"name": "Ex GH", "ats": "greenhouse", "board": "ex"},
        {"name": "Ex Lever", "ats": "lever", "slug": "ex", "profiles": ["engineer"]},
        {"name": "Ex Ashby", "ats": "ashby", "board": "ex", "profiles": ["engineer"]},
        {"name": "Broken", "ats": "greenhouse", "board": "broken"},
        {"name": "USAJOBS", "ats": "usajobs", "queries": ["statistician"], "locations": [], "remote": True, "profiles": ["analyst"]},
    ]
    shared["sources"] = {"hn_who_is_hiring": hn, "hn_max_comments": 100}
    shared["llm"] = {"backend": "none"}     # never call a real model from the tests
    shared["scoring"] = {"min_grade": "F"}   # no floor unless a test sets one
    shared["default_profile"] = "engineer"
    (tmp / "config.toml").write_text(_toml_dump(shared), encoding="utf-8")
    for name, search in (("engineer", ENGINEER_SEARCH), ("analyst", ANALYST_SEARCH)):
        d = tmp / "profiles" / name
        (d / "notes").mkdir(parents=True)
        # the fixtures exercise onsite-elsewhere scoring, variant grouping and multi-profile behaviour,
        # whatever the live profiles happen to be set to
        search = {**search, "enabled": True,
                  "location": {**search.get("location", {}), "keep_us_onsite": True}}
        (d / "search.toml").write_text(_toml_dump(search), encoding="utf-8")
        (d / "notes" / "about.md").write_text(f"# About\n\n- [2] notes for {name}\n", encoding="utf-8")
        (d / "notes" / "other.md").write_text("secret extra context that must not be read\n", encoding="utf-8")
        shutil.copy((ENGINEER if name == "engineer" else ANALYST) / "profile.toml", d / "profile.toml")
    (tmp / "profiles" / "analyst" / "search.toml").write_text(
        _toml_dump({**ANALYST_SEARCH, "enabled": True, "sources": {"hn_who_is_hiring": False},
                    "location": {**ANALYST_SEARCH.get("location", {}), "keep_us_onsite": True}}), encoding="utf-8")
    (tmp / ".env").write_text("USAJOBS_API_KEY=k\nUSAJOBS_EMAIL=e@x\n", encoding="utf-8")
    return tmp


# ----------------------------------------------------------------------------- sources

class SourcesTest(unittest.TestCase):
    def setUp(self):
        sources.FETCHER = fake_fetcher

    def tearDown(self):
        sources.FETCHER = None

    def test_greenhouse(self):
        jobs = sources.fetch_greenhouse("Ex", "ex")
        self.assertEqual(len(jobs), 8)
        j = {x.id: x for x in jobs}
        self.assertEqual(j["greenhouse:ex:1"].remote_signal, "remote")
        self.assertEqual(j["greenhouse:ex:6"].remote_signal, "hybrid")
        self.assertIn("Build agentic LLM applications", j["greenhouse:ex:1"].description)
        self.assertNotIn("&lt;", j["greenhouse:ex:1"].description)
        self.assertEqual(j["greenhouse:ex:5"].apply_url, "https://boards.greenhouse.io/embed/job_app?for=ex&token=5")
        self.assertTrue(j["greenhouse:ex:1"].posted_at.startswith("2026-08-20"))

    def test_lever(self):
        jobs = sources.fetch_lever("Ex", "ex")
        self.assertEqual(jobs[0].remote_signal, "hybrid")
        self.assertEqual(jobs[0].apply_url, "https://jobs.lever.co/ex/abc/apply")
        self.assertIn("Embed with customers", jobs[0].description)
        self.assertEqual(jobs[0].location, "Washington, D.C.; New York, NY")

    def test_ashby(self):
        jobs = sources.fetch_ashby("Ex", "ex")
        self.assertEqual(jobs[0].remote_signal, "remote")
        self.assertEqual(jobs[0].salary_text, "$150K – $200K • Offers Equity")
        self.assertEqual(jobs[0].apply_url, "https://jobs.ashbyhq.com/ex/u1/application")

    def test_hn(self):
        jobs = sources.fetch_hn_who_is_hiring()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Example Robotics")
        self.assertEqual(jobs[0].remote_signal, "remote")
        self.assertIn("Remote (US)", jobs[0].location)

    def test_usajobs(self):
        with self.assertRaises(sources.FetchError):
            sources.fetch_usajobs("USAJOBS", ["statistician"], api_key="", email="", locations=[])
        jobs = sources.fetch_usajobs("USAJOBS", ["statistician"], api_key="k", email="e@x", locations=["Columbus, Ohio"])
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j.id, "usajobs:800001")
        self.assertEqual(j.company, "Example Statistics Office")
        self.assertEqual(j.remote_signal, "remote")
        self.assertEqual(j.location, "Anywhere in the U.S. (remote job)")
        self.assertIn("Sampling", j.description)
        self.assertEqual(j.salary_text, "$99200.0 - $153354.0 Per Year")
        self.assertEqual(j.apply_url, "https://www.usajobs.gov/job/800001?PostingChannelID=RESTAPI")

    def test_public_boards_and_aggregators(self):
        jobs = sources.fetch_remotive("Remotive", ["software-dev"])
        self.assertEqual(jobs[0].id, "remotive:1680495"); self.assertEqual(jobs[0].remote_signal, "remote"); self.assertIn("Worldwide", jobs[0].location)
        jobs = sources.fetch_remoteok("RemoteOK")
        self.assertEqual([j.title for j in jobs], ["Junior Payroll Assistant"]); self.assertEqual(jobs[0].apply_url, "https://remoteOK.com/remote-jobs/x")
        jobs = sources.fetch_arbeitnow("Arbeitnow", max_pages=1)
        self.assertEqual(jobs[0].location, "Berlin"); self.assertIsNone(jobs[0].remote_signal)
        # Fetchers no longer decide where a posting is. They return everything and the location gate
        # drops what is foreign, so `rescore` can replay that decision from the cache.
        m = Matcher(CFG)
        himalayas = sources.fetch_himalayas("Himalayas", max_pages=1, page_size=100)
        self.assertEqual(sorted(j.company for j in himalayas), ["Sia Partners", "US Co"])
        by_company = {j.company: j for j in himalayas}
        singapore = by_company["Sia Partners"]                             # restricted to Singapore
        self.assertEqual(m.location_fit(singapore)[0], "non_us")
        self.assertFalse(m.classify(singapore)["keep"])
        muse = sources.fetch_themuse("The Muse", ["Data and Analytics"], max_pages=1)
        self.assertTrue(muse)                                             # anchored to Brampton, Canada
        self.assertTrue(all(m.location_fit(j)[0] == "non_us" for j in muse))
        self.assertTrue(all(not m.classify(j)["keep"] for j in muse))
        with self.assertRaises(sources.FetchError):
            sources.fetch_adzuna("Adzuna", ["x"], "", "")
        jobs = sources.fetch_adzuna("Adzuna", ["data scientist"], "id", "key", max_pages=1)
        self.assertEqual(jobs[0].id, "adzuna:555"); self.assertEqual(jobs[0].salary_text, "$120000 - $150000")
        jobs = sources.fetch_jooble("Jooble", ["statistician"], "k", max_pages=1)
        self.assertEqual(jobs[0].company, "Acme Stats"); self.assertEqual(jobs[0].remote_signal, "remote")
        stamped = sources.fetch_company({"name": "RemoteOK", "ats": "remoteok"})
        self.assertEqual(stamped[0].board, "RemoteOK")

    def test_smartrecruiters(self):
        """The best-structured ATS feed: explicit remote/hybrid booleans, and the board names itself."""
        jobs = sources.fetch_smartrecruiters("Ex", "ex", max_details=1,
                                             title_filter=lambda t: "Forward" in t)
        self.assertEqual([j.title for j in jobs], ["Applied AI Engineer", "Forward Deployed Engineer"])
        self.assertEqual(jobs[0].id, "smartrecruiters:s1")
        self.assertEqual(jobs[0].company, "Ex Smart")      # from the feed, not the config name
        self.assertEqual(jobs[0].location, "Columbus, OH, US")
        self.assertEqual(jobs[0].remote_signal, "hybrid")  # structured, not read out of a string
        self.assertEqual(jobs[1].remote_signal, "remote")
        self.assertTrue(jobs[0].posted_at.startswith("2026-09-02"))
        # the detail budget is spent on postings the title filter wants, and only those
        self.assertEqual(jobs[0].description, "")
        self.assertIn("Build agents with customers.", jobs[1].description)
        self.assertIn("Up to 20% travel.", jobs[1].description)
        self.assertEqual(jobs[1].apply_url, "https://jobs.smartrecruiters.com/ex/s2")

    def test_bamboohr_rippling_and_the_careers_page_watcher(self):
        b = sources.fetch_bamboohr("Ex Robotics", "ex")
        self.assertEqual([j.id for j in b], ["bamboohr:ex:7", "bamboohr:ex:9"])     # ids carry the board: "7" is per company
        self.assertEqual(b[0].location, "Dayton, Ohio")
        self.assertIn("Build autonomy stacks.", b[0].description)
        self.assertEqual(b[0].salary_text, "$150K - $190K")
        self.assertTrue(b[0].posted_at.startswith("2026-09-20"))
        self.assertEqual((b[1].remote_signal, b[1].description), ("remote", ""))   # a failed detail is not fatal
        r = sources.fetch_rippling("Ex", "ex")
        self.assertEqual(len(r), 1)                                                 # one posting, listed once per place
        self.assertEqual(r[0].location, "Columbus, OH; Remote (United States)")
        self.assertIn("Own perception.", r[0].description)
        pg = sources.fetch_page("Robots", "https://robots.example/careers/", "Dayton, OH")
        self.assertEqual(sorted(j.title for j in pg), ["QA Engineer/Trainer", "Software Engineer (Autonomy)"])  # longest text, same site
        se = next(j for j in pg if j.title.startswith("Software"))
        self.assertEqual((se.location, se.remote_signal), ("Dayton, OH", "remote"))
        self.assertIn("autonomous vehicles", se.description); self.assertNotIn("menu", se.description)
        self.assertEqual(se.id, sources.fetch_page("Robots", "https://robots.example/careers/")[0].id)   # stable ids

    def test_a_short_name_inside_a_longer_one_is_not_proof(self):
        from jobhunt.fetch import discover
        self.assertFalse(discover.looks_like("AMP Robotics", "Amp"))              # a Toronto company
        self.assertFalse(discover.looks_like("SVT Robotics", "Seldovia Village Tribe IRA"))
        self.assertTrue(discover.looks_like("Scale", "Scale AI"))
        self.assertTrue(discover.looks_like("Capacity", "Capacity"))
        self.assertTrue(discover.looks_like("Torc Robotics", "Torc Robotics Inc"))

    def test_a_careers_page_says_which_board_a_company_uses(self):
        from jobhunt.fetch import discover
        html = open(FIX / "careers_index.html").read() + '<a href="https://job-boards.greenhouse.io/acme/jobs/1">x</a>' \
            + '<a href="https://acme.bamboohr.com/careers/3">y</a><a href="https://ats.rippling.com/acme-co/jobs">z</a>'
        self.assertEqual(discover.boards_linked(html), [("greenhouse", "acme"), ("bamboohr", "acme"), ("rippling", "acme-co")])
        self.assertIn("bamboohr", discover.PROBES); self.assertIn("rippling", discover.PROBES)

    def test_workable_breezy_recruitee(self):
        w = sources.fetch_workable("Ex", "ex")
        self.assertEqual(w[0].id, "workable:W1")
        self.assertEqual(w[0].remote_signal, "remote")
        self.assertEqual(w[0].location, "Remote, United States")
        self.assertIn("Train models.", w[0].description)
        self.assertIn("PyTorch.", w[0].description)      # requirements are part of the description
        # an account that does not exist answers 200 with an empty list, not 404
        self.assertEqual(sources.fetch_workable("Ex", "empty"), [])

        b = sources.fetch_breezy("Ex", "ex")
        self.assertEqual(b[0].id, "breezy:b1-fde")
        self.assertEqual(b[0].location, "Columbus, Ohio, United States")   # objects, not strings
        self.assertIsNone(b[0].remote_signal)
        self.assertEqual(b[0].url, "https://ex.breezy.hr/p/b1-fde")

        r = sources.fetch_recruitee("Ex", "ex")
        self.assertEqual(r[0].id, "recruitee:991")
        self.assertEqual(r[0].location, "Boston, United States")
        self.assertEqual(r[0].remote_signal, "remote")
        self.assertEqual(r[0].apply_url, "https://ex.recruitee.com/o/applied-ai/c/new")

    def test_an_empty_board_is_a_miss_not_a_discovery(self):
        """SmartRecruiters and Workable answer 200 with an empty list for a token that does not
        exist. probe_due stops at the first token that answers, so treating that as a hit would park
        the guess and never try the platform the company is actually on."""
        from jobhunt.fetch import discover
        for ats, tok in (("smartrecruiters", "empty"), ("workable", "empty")):
            self.assertIsNone(discover.probe(ats, tok), ats)
        self.assertIsNotNone(discover.probe("smartrecruiters", "ex"))
        self.assertIsNotNone(discover.probe("workable", "ex"))
        # and every platform the fetchers support is one discovery knows how to look for
        self.assertEqual(set(discover.PROBES),
                         {"greenhouse", "lever", "ashby", "smartrecruiters", "workable",
                          "breezy", "recruitee", "bamboohr", "rippling"})

    def test_serpapi_reaches_the_boards_that_have_no_api(self):
        """LinkedIn, Indeed, ZipRecruiter and Glassdoor all close their APIs. Google indexes all
        four, and this reads Google."""
        orig = sources.FETCHER
        sources.FETCHER = lambda url, m, b, h: json.dumps({"jobs_results": [
            {"job_id": "g1", "title": "Forward Deployed Engineer", "company_name": "Acme",
             "location": "Columbus, OH", "via": "via LinkedIn", "description": "<p>Ship agents.</p>",
             "detected_extensions": {"posted_at": "2026-09-12", "work_from_home": True, "salary": "$200K"},
             "apply_options": [{"title": "Acme", "link": "https://acme.com/careers/g1"},
                               {"title": "LinkedIn", "link": "https://linkedin.com/jobs/g1"}]}]})
        try:
            with self.assertRaises(sources.FetchError):
                sources.fetch_serpapi("G", ["x"], "")          # no key is a clear failure
            jobs = sources.fetch_serpapi("G", ["fde"], "k", ["Columbus, Ohio"])
            self.assertEqual(len(jobs), 1)
            j = jobs[0]
            self.assertEqual(j.id, "serpapi:g1")
            self.assertEqual(j.company, "Acme")
            self.assertEqual(j.remote_signal, "remote")
            self.assertEqual(j.salary_text, "$200K")
            self.assertEqual(j.meta["via"], "via LinkedIn")    # which board it really came from
            self.assertEqual(j.url, "https://acme.com/careers/g1")  # the employer's own link wins
            self.assertIn("Ship agents.", j.description)
            self.assertEqual(sources.planned_requests(
                {"ats": "serpapi", "queries": ["a", "b", "c"], "locations": ["x", "y"], "max_pages": 1}), 6)
        finally:
            sources.FETCHER = orig

    def test_a_metered_source_is_stopped_before_it_spends(self):
        """A quota is not a rate limit. Adzuna's free tier is 1,000 a week and Jooble's is 500 for
        the life of the key, so a nightly scan would have spent both before anyone noticed."""
        from jobhunt.fetch import quota
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = {"quota": {"adzuna": {"per_day": 30}, "jooble": {"lifetime": 20}}}
        entry = {"name": "Adzuna", "ats": "adzuna", "queries": ["a", "b"],
                 "locations": ["x", "y"], "max_pages": 5}
        self.assertEqual(sources.planned_requests(entry), 20)
        self.assertEqual(sources.planned_requests({"ats": "greenhouse", "board": "x"}), 0)

        orig = sources.FETCHER
        sources.FETCHER = lambda *a, **k: json.dumps({"results": []})
        try:
            env = {"ADZUNA_APP_ID": "i", "ADZUNA_APP_KEY": "k"}
            sources.fetch_company(entry, env=env, quota_dir=tmp, quota_cfg=cfg)
            self.assertEqual(quota.spent(tmp, "adzuna")[0], 20)
            # the next run would take it to 40 against a ceiling of 30, so it never starts
            with self.assertRaises(sources.FetchError) as cm:
                sources.fetch_company(entry, env=env, quota_dir=tmp, quota_cfg=cfg)
            self.assertIn("20/30 requests used today", str(cm.exception))
            self.assertEqual(quota.spent(tmp, "adzuna")[0], 20)   # refusing costs nothing

            # a lifetime allowance says so, because it will not come back tomorrow
            jb = {"name": "Jooble", "ats": "jooble", "queries": ["a"], "max_pages": 30}
            with self.assertRaises(sources.FetchError) as cm:
                sources.fetch_company(jb, env={"JOOBLE_KEY": "k"}, quota_dir=tmp, quota_cfg=cfg)
            self.assertIn("LIFETIME", str(cm.exception))

            # a source with no key makes no requests, so it is charged for none. Six keyless
            # scans used to spend 216 of Adzuna's daily 250 without sending anything.
            before = quota.spent(tmp, "adzuna")[0]
            with self.assertRaises(sources.FetchError):
                sources.fetch_company(entry, env={}, quota_dir=tmp, quota_cfg=cfg)
            self.assertEqual(quota.spent(tmp, "adzuna")[0], before)
            self.assertFalse(sources.has_credentials("adzuna", {"ADZUNA_APP_ID": "i"}))
            self.assertTrue(sources.has_credentials("adzuna", {"ADZUNA_APP_ID": "i", "ADZUNA_APP_KEY": "k"}))

            # an ATS feed is an employer publishing their own jobs: nothing to ration
            gh = {"name": "Ex GH", "ats": "greenhouse", "board": "ex"}
            sources.FETCHER = fake_fetcher
            self.assertTrue(sources.fetch_company(gh, quota_dir=tmp, quota_cfg=cfg))
            self.assertEqual(quota.spent(tmp, "greenhouse"), (0, 0))
        finally:
            sources.FETCHER = orig

    def test_fetch_company_stamps_board(self):
        jobs = sources.fetch_company({"name": "Ex GH", "ats": "greenhouse", "board": "ex"})
        self.assertTrue(all(j.board == "Ex GH" for j in jobs))
        jobs = sources.fetch_company({"name": "USAJOBS", "ats": "usajobs", "queries": ["x"], "locations": []},
                                     env={"USAJOBS_API_KEY": "k", "USAJOBS_EMAIL": "e@x"})
        self.assertEqual((jobs[0].board, jobs[0].company), ("USAJOBS", "Example Statistics Office"))

    def test_strip_html(self):
        self.assertEqual(sources.strip_html("&lt;p&gt;Hi&lt;/p&gt;&lt;ul&gt;&lt;li&gt;a&lt;/li&gt;&lt;/ul&gt;"), "Hi\na")
        self.assertEqual(sources.strip_html("<b>x</b> &amp; y<br>z"), "x & y\nz")
        self.assertEqual(sources.strip_html("Acme | FDE | Remote<p>Details here"), "Acme | FDE | Remote\nDetails here")


# ----------------------------------------------------------------------------- filters

def _job(**kw) -> Job:
    base = dict(id="t:1", source="greenhouse", company="Ex", title="Applied AI Engineer", url="", apply_url="",
                location="Remote - United States", remote_signal=None, posted_at=None, description="")
    base.update(kw)
    return Job(**base)


class FiltersTest(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def job(self, **kw) -> Job:
        return _job(**kw)

    def test_company_exclude_drops_an_employer_from_every_source(self):
        """An employer you have ruled out reaches the scan from its own board and from aggregators."""
        cfg = merge_config(SHARED, {**ENGINEER_SEARCH,
                                    "search": {**ENGINEER_SEARCH["search"], "company_exclude": ["acme"]}})
        m = Matcher(cfg)
        for company, source in (("Acme", "workday"), ("Acme Services, LLC", "themuse"),
                                ("acme financial", "greenhouse")):
            c = m.classify(_job(title="Staff AI Engineer", company=company, location="Remote - United States",
                                source=source))
            self.assertFalse(c["keep"], company)
            self.assertIn("excluded company", c["drop_reason"])
            self.assertNotIn("seniority", c)       # a dropped posting is never measured
        # a perfect title and location does not rescue it, and nobody else is affected
        kept = m.classify(_job(title="Staff AI Engineer", company="Capital Group",
                               location="Remote - United States"))
        self.assertTrue(kept["keep"])
        self.assertTrue(Matcher(merge_config(SHARED, ENGINEER_SEARCH)).classify(
            _job(title="Staff AI Engineer", company="Acme", location="Remote - United States"))["keep"])

    def test_title_tiers_and_excludes(self):
        self.assertEqual(self.m.title_tier("Forward Deployed Engineer")[0], 1)
        self.assertEqual(self.m.title_tier("Senior Software Engineer - Agents Platform")[0], 2)
        self.assertEqual(self.m.title_tier("Engineering Manager, AI Platform")[0], 0)
        self.assertEqual(self.m.title_tier("Forward Deployed Product Manager")[0], 0)
        self.assertEqual(self.m.title_tier("Applied AI Architect, Industries")[0], 1)
        self.assertEqual(self.m.title_tier("Backend Software Engineer")[0], 0)

    def test_geo_resolves_places_and_distance(self):
        rich = geo.origin_point("Columbus, Ohio")
        self.assertIsNotNone(rich)
        self.assertAlmostEqual(rich[0], 39.98, delta=0.1); self.assertAlmostEqual(rich[1], -82.98, delta=0.1)
        # the state pins an ambiguous city name; without it the nearest candidate wins
        self.assertLess(geo.nearest_miles("Columbus, OH", rich), 5)
        self.assertGreater(geo.nearest_miles("Columbus, GA", rich), 500)
        self.assertGreater(geo.nearest_miles("Seattle, Washington", rich), 2000)
        self.assertGreater(geo.nearest_miles("Arlington, TX", rich), 900)
        self.assertLess(geo.nearest_miles("Arlington, VA", rich), 340)
        # an explicit state beats a same-named town elsewhere: Springfield OH, not Springfield IL
        self.assertLess(geo.nearest_miles("Springfield, OH", rich), 60)
        self.assertGreater(geo.nearest_miles("Springfield, IL", rich), 300)
        for spelling in ("Washington, DC", "Washington, D.C.", "Washington D.C.",
                         "Washington, District of Columbia, United States; Washington, DC"):
            self.assertAlmostEqual(geo.nearest_miles(spelling, rich), 326, delta=10, msg=spelling)
        # a multi-city posting resolves to its nearest office
        self.assertLess(geo.nearest_miles("San Francisco, CA; New York, NY", rich), 500)
        # shorthands the gazetteer does not carry, and strings that name nowhere
        self.assertLess(geo.nearest_miles("New York City", rich), 500)
        for nowhere in ("Remote", "Remote - United States", "United States", "Flexible / Remote"):
            self.assertIsNone(geo.nearest_miles(nowhere, rich), nowhere)
        # a city the table spells differently still beats the state centroid
        self.assertAlmostEqual(geo.nearest_miles("New York City, NY", rich),
                               geo.nearest_miles("New York, NY", rich), delta=5)
        # a bare city name must clear the prominence gate, or "Remote-Friendly" becomes a
        # 94-person town outside Washington and the posting looks commutable
        self.assertIsNone(geo.nearest_miles("Remote-Friendly (Travel-Required)", rich))
        self.assertIsNone(geo.nearest_miles("Friendly", rich))
        self.assertLess(geo.nearest_miles("Columbus", rich), 5)          # the populous one wins
        # a small place is still findable once its state is named
        self.assertLess(geo.nearest_miles("Worthington, OH", rich), 20)
        self.assertGreater(geo.haversine((39.98, -82.98), (40.66, -73.94)), 250)
        self.assertEqual(round(geo.haversine((39.98, -82.98), (39.98, -82.98))), 0)

    def test_location_fits(self):
        self.assertEqual(self.m.location_fit(self.job(location="Remote - United States"))[0], "remote")
        self.assertEqual(self.m.location_fit(self.job(location="Remote - India"))[0], "non_us")
        self.assertEqual(self.m.location_fit(self.job(location="London, UK"))[0], "non_us")
        self.assertEqual(self.m.location_fit(self.job(location="London, UK; New York, NY"))[0], "quarterly")
        self.assertEqual(self.m.location_fit(self.job(location="Cincinnati, OH"))[0], "monthly")
        self.assertEqual(self.m.location_fit(self.job(location="Kentucky; Ohio; Cincinnati, OH"))[0], "monthly")
        self.assertEqual(self.m.location_fit(self.job(location="San Francisco, CA"))[0], "relocate")
        self.assertEqual(self.m.location_fit(self.job(location="San Francisco", remote_signal="remote"))[0], "remote")
        self.assertEqual(self.m.location_fit(self.job(location="Remote"))[0], "remote_unspecified")
        self.assertEqual(self.m.location_fit(self.job(location="Dayton, OH (Hybrid)"))[0], "monthly")
        # a structured remote flag beats ANY office address, including a near one: nine real postings
        # read like "Remote (Any State); 507 RESTON VA", and calling those a daily commute is false
        self.assertEqual(self.m.location_fit(self.job(location="Columbus, OH", remote_signal="remote"))[0], "remote")
        self.assertEqual(self.m.location_fit(self.job(location="Washington, DC", remote_signal="remote"))[0], "remote")
        # distance, not substrings: the old city vocabulary called these commutable from home
        self.assertEqual(self.m.location_fit(self.job(location="Seattle, Washington"))[0], "relocate")
        self.assertEqual(self.m.location_fit(self.job(location="Charleston, West Virginia"))[0], "quarterly")
        self.assertEqual(self.m.location_fit(self.job(location="Arlington, TX"))[0], "relocate")
        self.assertEqual(self.m.location_fit(self.job(location="Arlington, VA"))[0], "quarterly")
        self.assertEqual(self.m.location_fit(self.job(location="Columbus, GA"))[0], "relocate")
        # inside the 500-mile radius, kept rather than discarded
        for where in ("Philadelphia, PA", "Charlotte, NC", "New York, NY", "Chicago, IL"):
            self.assertEqual(self.m.location_fit(self.job(location=where))[0], "quarterly", where)
        # ambiguous two-letter codes used to defeat the non-US drop by looking like US states
        for foreign in ("Munich, DE", "Toronto, CA", "Vancouver, BC, CA", "Dublin, IE", "Tel Aviv, IL"):
            self.assertEqual(self.m.location_fit(self.job(location=foreign))[0], "non_us", foreign)
        # a country spelled the way its own residents spell it. Board discovery found a Brazilian
        # retailer whose postings read "Minas Gerais, Brasil" -- unknown location, so it passed the
        # gate -- and whose Portuguese titles matched an English keyword by prefix ("agente" for
        # "agent"). Getting the country right is what stops the title ever getting a chance.
        for native in ("Pocos De Caldas, Minas Gerais, Brasil", "Rio de Janeiro, Brasil",
                       "Berlin, Deutschland", "Madrid, España", "Zurich, Schweiz"):
            self.assertEqual(self.m.location_fit(self.job(location=native))[0], "non_us", native)
        # the rest of the world, once board discovery started surfacing employers everywhere
        for far in ("Yerevan, Armenia", "Montenegro", "Reykjavik, Iceland", "Ljubljana, Slovenia",
                    "Tashkent, Uzbekistan", "Nairobi, Kenya"):
            self.assertEqual(self.m.location_fit(self.job(location=far))[0], "non_us", far)
        # ...but only the names no American town also carries. A state code cannot rescue those
        # towns, because "Munich, DE" and "Toronto, CA" look like a US state too, so the list simply
        # leaves the collisions out and names the capital instead where it matters.
        for town in ("Lebanon, NH", "Panama City, FL", "Cuba, MO", "Malta, NY", "Jordan, MN",
                     "Trinidad, CO", "Atlanta, Georgia"):
            self.assertNotEqual(self.m.location_fit(self.job(location=town))[0], "non_us", town)
        self.assertEqual(self.m.location_fit(self.job(location="Beirut, Lebanon"))[0], "non_us")
        self.assertEqual(self.m.location_fit(self.job(location="Podgorica, Montenegro"))[0], "non_us")
        # the distance itself is recorded for the dashboard
        c = self.m.classify(self.job(title="Applied AI Engineer", location="New York, NY"))
        self.assertTrue(c["keep"]); self.assertEqual(c["location_fit"], "quarterly")
        self.assertTrue(470 <= c["location_miles"] <= 490, c["location_miles"])
        far = self.m.classify(self.job(title="Applied AI Engineer", location="San Francisco, CA"))
        self.assertFalse(far["keep"]); self.assertIn("500 miles", far["drop_reason"])

    def test_signals_extracted_but_not_scored(self):
        t = self.m.travel("Willing to travel once every 4-8 weeks to see customers (as needed).")
        self.assertEqual(t["level"], "light")
        t = self.m.travel("Expect 25% travel with up to 50% travel as necessary.")
        self.assertEqual((t["pct"], t["level"]), (50, "moderate"))
        self.assertEqual(self.m.travel("Travel frequently to customer sites.")["level"], "heavy")
        s = self.m.salary("The annual salary range is $182,000—$250,208 USD.", None)
        self.assertEqual((s["min"], s["max"]), (182000, 250208))
        c = self.m.clearance("This position requires U.S. citizenship and an active TS/SCI security clearance")
        self.assertTrue(c["active_required"]); self.assertEqual(c["level"], "TS/SCI")
        c = self.m.clearance("eligibility for a U.S. government secret clearance are required")
        self.assertFalse(c["active_required"]); self.assertTrue(c["eligibility_ok"])
        # measuring is not scoring: travel, pay and clearance come back as facts about the posting,
        # and what they are worth is decided in relevance.py against the reader's weights
        a = self.m.classify(self.job(description="travel up to 20%; $180,000 - $240,000"))
        b = self.m.classify(self.job(description="requires an active TS/SCI clearance. Travel frequently. $90,000"))
        self.assertTrue(b["clearance"]["active_required"]); self.assertEqual(b["travel"]["level"], "heavy")
        self.assertTrue(a["keep"]); self.assertTrue(b["keep"])
        self.assertNotIn("score", a); self.assertNotIn("score", b)
        dropped = self.m.classify(self.job(location="London, UK"))
        self.assertFalse(dropped["keep"]); self.assertEqual(dropped["drop_reason"], "non-US location")
        self.assertFalse(self.m.classify(self.job(title="Account Executive"))["keep"])


class RecencyTest(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)
        from datetime import datetime, timedelta, timezone
        self.now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.ago = lambda d: (self.now - timedelta(days=d)).isoformat()

    def score(self, **kw):
        first_seen = kw.pop("first_seen", None)
        return self.m.classify(_job(**kw), first_seen=first_seen, now=self.now)

    def test_age_buckets_are_measured_not_penalised(self):
        """Age is a measurement now. What it costs is the `freshness` weight, which lives in
        relevance.py where every other weight lives."""
        fresh = self.score(posted_at=self.ago(3))
        mid = self.score(posted_at=self.ago(20))
        stale = self.score(posted_at=self.ago(30))          # a month is stale
        self.assertEqual(fresh["recency"]["level"], "fresh")
        self.assertEqual(mid["recency"]["level"], "mid")
        self.assertEqual(stale["recency"]["level"], "stale")
        self.assertNotIn("penalty", fresh["recency"])
        self.assertEqual(mid["recency"]["age_days"], 20)
        self.assertTrue(mid["recency"]["dated"])
        # undated postings age from when we first saw them, and say so
        undated = self.score(posted_at=None, first_seen=self.ago(60))
        self.assertEqual(undated["recency"]["level"], "stale")
        self.assertFalse(undated["recency"]["dated"])
        self.assertEqual(self.score(posted_at=None)["recency"]["level"], "unknown")
        # profile override, under the old key name as well as the new one
        m2 = Matcher(merge_config(CFG, {"recency": {"mid_days": 15}}))
        self.assertEqual(m2.classify(_job(posted_at=self.ago(20)), now=self.now)["recency"]["level"], "stale")
        m3 = Matcher(merge_config(CFG, {"recency": {"stale_days": 45}}))
        self.assertEqual(m3.classify(_job(posted_at=self.ago(40)), now=self.now)["recency"]["level"], "mid")

    def test_workday_posted_on(self):
        from datetime import datetime, timezone
        now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        self.assertTrue(sources.workday_posted_on_to_iso("Posted 30+ Days Ago", now).startswith("2026-08-16"))
        self.assertTrue(sources.workday_posted_on_to_iso("Posted 3 Days Ago", now).startswith("2026-09-12"))
        self.assertTrue(sources.workday_posted_on_to_iso("Posted Today", now).startswith("2026-09-15"))
        self.assertTrue(sources.workday_posted_on_to_iso("Posted Yesterday", now).startswith("2026-09-14"))
        self.assertIsNone(sources.workday_posted_on_to_iso(None, now))
        self.assertIsNone(sources.workday_posted_on_to_iso("whenever", now))


class AnalystFiltersTest(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(merge_config(SHARED, ANALYST_SEARCH))

    def test_titles(self):
        self.assertEqual(self.m.title_tier("Senior Data Scientist, Health Outcomes")[0], 1)
        self.assertEqual(self.m.title_tier("Survey Statistician")[0], 1)
        self.assertEqual(self.m.title_tier("Data Analyst, Institutional Research")[0], 2)
        self.assertEqual(self.m.title_tier("Forward Deployed Engineer")[0], 0)
        self.assertEqual(self.m.title_tier("Machine Learning Engineer")[0], 0)
        self.assertEqual(self.m.title_tier("Director of Data Science")[0], 0)

    def test_home_is_a_daily_drive_and_cincinnati_is_a_quarterly_trip(self):
        hybrid = self.m.classify(_job(title="Data Scientist", location="Columbus, OH", remote_signal="hybrid"))
        self.assertEqual(hybrid["location_fit"], "daily")
        remote = self.m.classify(_job(title="Data Scientist", location="Remote - United States", remote_signal="remote"))
        self.assertEqual(remote["location_fit"], "remote")
        # Cincinnati is 100 miles: a quarterly trip for Sam (40-mile daily drive, no wider monthly
        # band), a monthly one for Alex, whose monthly_miles is 120
        self.assertEqual(self.m.location_fit(_job(location="Cincinnati, OH"))[0], "quarterly")


# ----------------------------------------------------------------------------- profiles

class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_root(Path(tempfile.mkdtemp()))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_layering_and_company_filter(self):
        shared, profiles = load_all(self.tmp)
        self.assertEqual(list(profiles), ["engineer", "analyst"])       # default first
        engineer, analyst = profiles["engineer"], profiles["analyst"]
        self.assertEqual(analyst.search["location"]["non_us"], shared["location"]["non_us"])   # inherited
        self.assertIn("westerville", analyst.search["location"]["commutable"])                      # overridden
        self.assertNotIn("westerville", engineer.search["location"]["commutable"])
        self.assertEqual(engineer.board_names(), {"Ex GH", "Ex Lever", "Ex Ashby", "Broken"})
        self.assertEqual(analyst.board_names(), {"Ex GH", "Broken", "USAJOBS"})
        self.assertTrue(engineer.wants_source("hn")); self.assertFalse(analyst.wants_source("hn"))
        self.assertIn("notes for analyst", analyst.notes)
        self.assertNotIn("secret extra context", analyst.notes)
        self.assertEqual(analyst.display_name, "Sam")
        self.assertTrue(str(engineer.answers_for_apply()["documents"]["resume"]).startswith(str(self.tmp.resolve())))


# ----------------------------------------------------------------------------- facts

class GroundworkTest(unittest.TestCase):
    """The three things discovery needs before it can add companies by the hundred."""

    def test_one_company_one_key(self):
        from jobhunt.classify.learn import canonical_name, dup_key
        same = ["Scale AI", "Scale AI, Inc.", "SCALE AI", "  Scale  AI  ", "Scale AI LLC"]
        self.assertEqual(len({canonical_name(x) for x in same}), 1, [canonical_name(x) for x in same])
        self.assertEqual(canonical_name("Samsara Inc."), "samsara")
        self.assertEqual(canonical_name("Alight Solutions LLC"), "alight solutions")
        self.assertEqual(canonical_name("Equinix, Inc"), "equinix")
        self.assertEqual(canonical_name("Acme Co Ltd"), "acme")      # stripped whole, no stray "ltd"
        # conservative on purpose
        self.assertEqual(canonical_name("Co"), "co")                 # a suffix that IS the name
        self.assertEqual(canonical_name("Bank of America"), "bank of america")
        self.assertNotEqual(canonical_name("Meta"), canonical_name("Facebook"))   # no alias guessing
        self.assertEqual(canonical_name(""), "")
        self.assertEqual(canonical_name(None), "")
        # and it reaches duplicate detection, which is what makes two spellings one role
        self.assertEqual(dup_key("Scale AI, Inc.", "Forward Deployed Engineer"),
                         dup_key("Scale AI", "Forward Deployed Engineer"))

    def test_a_feed_is_identified_by_its_address_not_its_label(self):
        """The same board under two spellings used to be two boards and got fetched twice."""
        from jobhunt.pipeline import feed_key
        a = {"name": "Scale AI", "ats": "greenhouse", "board": "scaleai"}
        b = {"name": "Scale AI, Inc.", "ats": "greenhouse", "board": "scaleai", "profiles": ["analyst"]}
        self.assertEqual(feed_key(a), feed_key(b))
        self.assertNotEqual(feed_key(a), feed_key({"ats": "greenhouse", "board": "scale"}))
        self.assertNotEqual(feed_key(a), feed_key({"ats": "ashby", "board": "scaleai"}))
        self.assertEqual(feed_key({"ats": "lever", "slug": "palantir"}),
                         feed_key({"ats": "lever", "slug": "Palantir", "name": "other"}))
        wd = {"ats": "workday", "tenant": "bah", "wd": "wd1", "site": "BAH_Jobs"}
        self.assertEqual(feed_key(wd), feed_key({**wd, "name": "BAH"}))
        self.assertNotEqual(feed_key(wd), feed_key({**wd, "site": "Other"}))
        # a keyword source is identified by what it asks for
        q1 = {"ats": "adzuna", "queries": ["machine learning"], "locations": ["Virginia"]}
        self.assertEqual(feed_key(q1), feed_key({**q1, "name": "Adzuna US"}))
        self.assertNotEqual(feed_key(q1), feed_key({**q1, "queries": ["agentic"]}))

    def test_requests_to_one_host_are_paced(self):
        """Discovery probes thousands of tokens against three hosts. The old probe tool did that at
        twelve workers with no delay, which is rude and the fastest way to get blocked."""
        import time as _t
        from jobhunt.fetch import sources as src
        was = src.REQUESTS_PER_SECOND
        src._next_ok.clear()
        try:
            src.REQUESTS_PER_SECOND = 20.0                 # 50ms apart
            t0 = _t.monotonic()
            for _ in range(4):
                src._throttle("https://boards-api.greenhouse.io/x")
            one_host = _t.monotonic() - t0
            self.assertGreater(one_host, 0.10)             # three gaps of 50ms
            t0 = _t.monotonic()
            for h in ("a", "b", "c", "d"):
                src._throttle(f"https://{h}.example.com/x")
            self.assertLess(_t.monotonic() - t0, 0.05)     # different hosts do not queue
            src.REQUESTS_PER_SECOND = 0                    # and it can be turned off
            t0 = _t.monotonic()
            for _ in range(50):
                src._throttle("https://boards-api.greenhouse.io/x")
            self.assertLess(_t.monotonic() - t0, 0.05)
        finally:
            src.REQUESTS_PER_SECOND = was
            src._next_ok.clear()

    def test_the_test_fetcher_never_sleeps(self):
        """FETCHER short-circuits before the throttle, so the suite is not paced by it."""
        import time as _t
        from jobhunt.fetch import sources as src
        was, wasf = src.REQUESTS_PER_SECOND, src.FETCHER
        try:
            src.REQUESTS_PER_SECOND = 0.5                  # two seconds apart if it applied
            src.FETCHER = lambda *a, **k: "{}"
            t0 = _t.monotonic()
            for _ in range(5):
                src.http_text("https://boards-api.greenhouse.io/x")
            self.assertLess(_t.monotonic() - t0, 0.05)
        finally:
            src.REQUESTS_PER_SECOND, src.FETCHER = was, wasf


class DiscoveryTest(unittest.TestCase):
    """Finding companies rather than fetching the ones you already named."""

    def setUp(self):
        from jobhunt.fetch import discover
        self.d = discover
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    # -- harvest ---------------------------------------------------------
    def test_six_spellings_of_one_employer_are_one_record(self):
        got = self.d.names_from_cache([
            {"company": "Scale AI", "source": "themuse", "title": "Applied AI Engineer"},
            {"company": "Scale AI, Inc.", "source": "remotive", "title": "Chief Revenue Officer"},
            {"company": "SCALE AI", "source": "themuse", "title": "Forward Deployed Engineer"},
            {"company": "?", "source": "hn", "title": "Applied AI Engineer"},
            {"company": "", "source": "hn", "title": "Applied AI Engineer"},
        ], wanted=lambda t: "Engineer" in t)
        self.assertEqual(len(got), 1)
        name, _src, relevant = got[0]
        self.assertEqual(name, "Scale AI, Inc.")          # the fullest spelling, for display
        self.assertEqual(relevant, 2)                     # the CRO does not count
        reg, added = self.d.harvest(got, {})
        self.assertEqual(added, 1)
        self.assertEqual(list(reg), ["scale ai"])
        self.assertEqual(reg["scale ai"]["state"], "new")
        # harvesting again adds nothing but merges where it was seen
        reg, added = self.d.harvest([("Scale AI", "remoteok", 5)], reg)
        self.assertEqual(added, 0)
        self.assertEqual(sorted(reg["scale ai"]["seen_in"]), ["remoteok", "themuse"])
        self.assertEqual(reg["scale ai"]["relevant_titles"], 5)

    def test_an_excluded_company_is_never_probed_and_config_is_not_reharvested(self):
        """Acme is the profile's current employer. It must not cost a request, and it must never
        find its way back into the scan by a side door."""
        from jobhunt.profiles import load_all
        tmp = make_root(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sources.FETCHER = fake_fetcher
        self.addCleanup(lambda: setattr(sources, "FETCHER", None))
        _sh, profiles = load_all(tmp)
        ev = profiles["engineer"]
        ev.search["search"] = {**(ev.search.get("search") or {}), "company_exclude": ["acme"]}
        reg, _n = self.d.harvest([("Acme Services, LLC", "themuse", 40),
                                  ("Ex GH", "themuse", 3),
                                  ("Vannevar Labs", "themuse", 2)], {})
        self.d.save(ev.data_dir, reg)
        reg, _added = self.d.harvest_all(tmp, ev, log=lambda _s: None)
        key = canonical_name
        self.assertEqual(reg[key("Acme Services, LLC")]["state"], "excluded")
        self.assertFalse(self.d.due(reg[key("Acme Services, LLC")]))
        self.assertEqual(reg[key("Ex GH")]["state"], "promoted")   # already a configured board
        self.assertIsNone(reg[key("Ex GH")]["resolved"])           # so it generates no new entry
        self.assertEqual(reg[key("Vannevar Labs")]["state"], "new")  # the one worth probing
        self.assertNotIn(key("Acme Services, LLC"),
                         {r["canonical"] for r in reg.values() if self.d.due(r)})

    # -- resolve ---------------------------------------------------------
    def test_token_guesses_are_plausible_and_bounded(self):
        self.assertEqual(self.d.tokens_for("Anthropic"), ["anthropic"])
        self.assertIn("scaleai", self.d.tokens_for("Scale AI"))
        self.assertIn("scale-ai", self.d.tokens_for("Scale AI"))
        self.assertIn("liftoff", self.d.tokens_for("Liftoff Mobile"))   # trailing noise dropped
        self.assertEqual(self.d.tokens_for(""), [])
        self.assertEqual(self.d.tokens_for("   "), [])
        for name in ("Booz Allen Hamilton", "A" * 200, "!!!"):
            self.assertLessEqual(len(self.d.tokens_for(name)), 5, name)
            self.assertTrue(all(1 < len(t) <= 40 for t in self.d.tokens_for(name)), name)

    # -- probe -----------------------------------------------------------
    def test_a_board_says_whose_it_is_and_a_miss_says_nothing(self):
        orig = sources.FETCHER
        def fake(url, method, body, headers):
            if "greenhouse.io/v1/boards/scaleai" in url:
                return json.dumps({"jobs": [{"title": "Forward Deployed Engineer",
                                             "location": {"name": "Remote - US"},
                                             "company_name": "Scale AI"}]})
            if "lever.co/v0/postings/palantir" in url:
                return json.dumps([{"text": "FDSE", "categories": {"location": "Washington, DC"}}])
            if "ashbyhq.com/posting-api/job-board/openai" in url:
                return json.dumps({"apiVersion": "1", "jobs": [
                    {"title": "Research Engineer", "location": "SF", "isRemote": False, "isListed": True},
                    {"title": "Hidden", "location": "SF", "isListed": False}]})
            raise sources.FetchError("HTTP 404")
        sources.FETCHER = fake
        try:
            gh = self.d.probe("greenhouse", "scaleai")
            self.assertEqual(gh["company_name"], "Scale AI")     # the board identifies itself
            self.assertEqual(len(gh["postings"]), 1)
            self.assertIsNone(self.d.probe("greenhouse", "nope"))
            self.assertEqual(len(self.d.probe("lever", "palantir")["postings"]), 1)
            ashby = self.d.probe("ashby", "openai")
            self.assertEqual(len(ashby["postings"]), 1)          # unlisted postings do not count
            self.assertIsNone(ashby["company_name"])             # Ashby's feed carries no name
        finally:
            sources.FETCHER = orig

    def test_a_token_that_resolves_to_someone_else_is_not_a_hit(self):
        """Greenhouse names itself on every posting. Without checking it, a lucky token guess enters
        the scan as the wrong employer and every posting is filed under a company you never chose."""
        self.assertTrue(self.d.looks_like("Scale", "Scale AI"))
        self.assertTrue(self.d.looks_like("Scale AI, Inc.", "Scale AI"))
        self.assertTrue(self.d.looks_like("Figma", None))        # Lever and Ashby carry no name
        self.assertFalse(self.d.looks_like("Apex", "Bytedance"))
        self.assertFalse(self.d.looks_like("", "Anything"))

        calls = []
        orig = sources.FETCHER
        def fake(url, method, body, headers):
            calls.append(url)
            if "greenhouse.io/v1/boards/apex" in url:
                return json.dumps({"jobs": [{"title": "Applied AI Engineer",
                                             "location": {"name": "Remote - US"},
                                             "company_name": "Bytedance"}]})
            raise sources.FetchError("HTTP 404")
        sources.FETCHER = fake
        try:
            reg = {"apex": {"name": "Apex", "canonical": "apex", "state": "new", "tried": {},
                            "relevant_titles": 3, "seen_in": [], "resolved": None}}
            self.d.probe_due(reg, Matcher(CFG), log=lambda _s: None)
            self.assertEqual(reg["apex"]["state"], "dead")       # someone else's board is not ours
            self.assertIsNone(reg["apex"]["resolved"])
        finally:
            sources.FETCHER = orig

    # -- assess ----------------------------------------------------------
    def test_promotion_uses_the_real_gate(self):
        """A board promoted here must not produce postings the scan then throws away, so it runs
        Matcher.gate rather than an approximation of it."""
        m = Matcher(CFG)
        good = {"postings": [{"title": "Chief Revenue Officer", "location": "Remote - US"},
                             {"title": "Applied AI Engineer", "location": "Remote - United States"}]}
        v = self.d.assess(good, m)
        self.assertEqual(v["hits"], 1)                       # the CRO fails the title gate
        self.assertIn("Applied AI Engineer", v["sample"])
        # location counts too: the same title abroad, or somewhere unreachable, is not a hit
        self.assertEqual(self.d.assess({"postings": [{"title": "Applied AI Engineer",
                                                      "location": "London, UK"}]}, m)["hits"], 0)
        self.assertEqual(self.d.assess({"postings": []}, m)["hits"], 0)

    # -- registry --------------------------------------------------------
    def test_the_registry_remembers_what_was_tried(self):
        reg = {"a": {"state": "new"},
               "b": {"state": "promoted"},
               "c": {"state": "excluded"},
               "d": {"state": "parked", "recheck_after": "2026-01-01"},
               "e": {"state": "parked", "recheck_after": "2099-01-01"}}
        self.assertTrue(self.d.due(reg["a"]))                # never probed
        self.assertFalse(self.d.due(reg["b"]))               # already in the scan
        self.assertFalse(self.d.due(reg["c"]))               # excluded, never probe
        self.assertTrue(self.d.due(reg["d"], today="2026-09-24"))   # recheck is overdue
        self.assertFalse(self.d.due(reg["e"], today="2026-09-24"))  # not yet
        self.d.save(self.dir, reg)
        self.assertEqual(self.d.load(self.dir), reg)
        # a registry from another schema is ignored rather than misread
        raw = json.loads((self.dir / self.d.REGISTRY_FILE).read_text())
        raw["schema"] = 99
        (self.dir / self.d.REGISTRY_FILE).write_text(json.dumps(raw))
        self.assertEqual(self.d.load(self.dir), {})

    # -- promote ---------------------------------------------------------
    def test_discovered_boards_render_as_loadable_toml(self):
        import tomllib
        reg = {"a": {"name": "Scale AI", "state": "promoted",
                     "resolved": {"ats": "greenhouse", "token": "scaleai", "company_name": "Scale AI"}},
               "b": {"name": "Palantir", "state": "promoted",
                     "resolved": {"ats": "lever", "token": "palantir", "company_name": None}},
               "c": {"name": "Nope", "state": "parked", "resolved": {"ats": "ashby", "token": "nope"}},
               "d": {"name": "Unresolved", "state": "promoted", "resolved": None}}
        text = self.d.render_discovered([r for r in reg.values() if r["state"] == "promoted"])
        got = tomllib.loads(text)["companies"]
        self.assertEqual([c["name"] for c in got], ["Palantir", "Scale AI"])
        # the count is what the file holds, not how many records are promoted: most of those are
        # companies already in config.toml, which carry no token because there was nothing to resolve
        tmp2 = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp2, ignore_errors=True)
        self.assertEqual(self.d.write_discovered(tmp2, reg), 2)
        self.assertEqual(got[0]["slug"], "palantir")         # lever uses slug
        self.assertEqual(got[1]["board"], "scaleai")         # greenhouse uses board
        self.assertTrue(all(c["profiles"] == ["engineer"] for c in got))
        # the profile that ran discovery owns what it found, not a hardcoded name
        analyst = tomllib.loads(self.d.render_discovered([reg["a"]], profile="analyst"))["companies"][0]
        self.assertEqual(analyst["profiles"], ["analyst"])
        # a quote in a company name cannot break the file
        odd = self.d.render_discovered([{"name": 'Bob"s AI', "state": "promoted",
                                         "resolved": {"ats": "ashby", "token": "bobs"}}])
        self.assertEqual(tomllib.loads(odd)["companies"][0]["name"], "Bob's AI")

    def test_discovered_boards_reach_the_scan_without_touching_config(self):
        """The whole point of the separate file: config.toml stays hand-curated, and deleting the
        generated one resets discovery."""
        from jobhunt.profiles import add_discovered, load_all, DISCOVERED_FILE
        from jobhunt.pipeline import union_companies
        tmp = make_root(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sources.FETCHER = fake_fetcher
        self.addCleanup(lambda: setattr(sources, "FETCHER", None))
        _shared, before = load_all(tmp)
        n_before = len(union_companies(before))
        cfg_before = (tmp / "config.toml").read_text()

        (tmp / DISCOVERED_FILE).write_text(
            '[[companies]]\nname = "Found Co"\nats = "greenhouse"\nboard = "foundco"\nprofiles = ["engineer"]\n')
        _shared, after = load_all(tmp)
        boards = union_companies(after)
        self.assertEqual(len(boards), n_before + 1)
        self.assertIn("Found Co", {c.get("name") for c in boards})
        self.assertEqual((tmp / "config.toml").read_text(), cfg_before, "config.toml must not be touched")

        # a board already in config.toml is not added twice under a different label
        shared = {"companies": [{"name": "Ex GH", "ats": "greenhouse", "board": "ex"}]}
        (tmp / DISCOVERED_FILE).write_text(
            '[[companies]]\nname = "Ex GH Again"\nats = "greenhouse"\nboard = "ex"\n')
        self.assertEqual(len(add_discovered(tmp, shared)["companies"]), 1)
        # and a corrupt generated file is ignored rather than breaking every load
        (tmp / DISCOVERED_FILE).write_text("this is not toml [[[")
        self.assertEqual(add_discovered(tmp, shared)["companies"], shared["companies"])

        # deleting the file resets discovery
        (tmp / DISCOVERED_FILE).unlink()
        _shared, back = load_all(tmp)
        self.assertEqual(len(union_companies(back)), n_before)

    def test_workday_is_a_second_chance_and_costs_enough_to_be_opt_in(self):
        """Most large government contractors are on Workday and answer no public feed, so they land
        in `dead`. The sweep that finds them is 4 hosts x 14 site names, which is why it only runs on
        companies already known to post work you want."""
        import tomllib
        calls = []
        orig = sources.FETCHER
        def fake(url, method, body, headers):
            calls.append(url)
            if "bah.wd1.myworkdayjobs.com/wday/cxs/bah/External/jobs" in url:
                return json.dumps({"total": 412})
            raise sources.FetchError("HTTP 404")
        sources.FETCHER = fake
        try:
            reg = {"a": {"name": "Booz Allen Hamilton", "state": "dead", "relevant_titles": 30, "tried": {}},
                   "b": {"name": "Tiny Co", "state": "dead", "relevant_titles": 1, "tried": {}},
                   "c": {"name": "Already", "state": "promoted", "relevant_titles": 99, "tried": {}}}
            stats = self.d.workday_pass(reg, Matcher(CFG), min_relevant=5, log=lambda _s: None)
            self.assertEqual((stats["swept"], stats["found"]), (1, 1))   # only the one worth sweeping
            self.assertEqual(reg["a"]["state"], "promoted")
            self.assertEqual(reg["a"]["resolved"]["tenant"], "bah")
            self.assertEqual(reg["a"]["resolved"]["postings"], 412)
            self.assertEqual(reg["b"]["state"], "dead")                  # below min_relevant
            self.assertEqual(reg["c"]["state"], "promoted")              # not re-swept
            self.assertTrue(all("myworkdayjobs" in u for u in calls))
            # it does not sweep the same company twice
            self.assertEqual(self.d.workday_pass(reg, Matcher(CFG), min_relevant=5, log=lambda _s: None)["swept"], 0)
        finally:
            sources.FETCHER = orig
        self.assertEqual(self.d.acronym_for("Booz Allen Hamilton"), "bah")
        self.assertIsNone(self.d.acronym_for("Anthropic"))       # one word, no initials to take
        # and a Workday record renders with tenant/site, not a board token
        text = self.d.render_discovered([reg["a"]])
        entry = tomllib.loads(text)["companies"][0]
        self.assertEqual(entry["ats"], "workday")
        self.assertEqual((entry["tenant"], entry["wd"], entry["site"]), ("bah", "wd1", "External"))
        self.assertNotIn("board", entry)
        self.assertTrue(entry["queries"])

    def test_a_gate_change_can_be_rechecked_against_what_it_already_let_in(self):
        """A board admitted by a bug stays in the scan forever, quietly contributing postings you
        never wanted. Discovery found a Brazilian retailer that way. Hand-written entries in
        config.toml are somebody's deliberate choice and are left alone."""
        orig = sources.FETCHER
        def fake(url, method, body, headers):
            if "boards/stillgood" in url:
                return json.dumps({"jobs": [{"title": "Applied AI Engineer",
                                             "location": {"name": "Remote - United States"}}]})
            if "boards/gonebad" in url:
                return json.dumps({"jobs": [{"title": "Agente Fit Vendedor",
                                             "location": {"name": "Minas Gerais, Brasil"}}]})
            raise sources.FetchError("HTTP 404")
        sources.FETCHER = fake
        try:
            reg = {"good": {"name": "Still Good", "state": "promoted",
                            "resolved": {"ats": "greenhouse", "token": "stillgood"}},
                   "bad": {"name": "Gone Bad", "state": "promoted",
                           "resolved": {"ats": "greenhouse", "token": "gonebad"}},
                   "hand": {"name": "Hand Written", "state": "promoted", "resolved": None},
                   "vanished": {"name": "Vanished", "state": "promoted",
                                "resolved": {"ats": "greenhouse", "token": "vanished"}}}
            st = self.d.reassess(reg, Matcher(CFG), log=lambda _s: None)
            self.assertEqual((st["checked"], st["demoted"]), (3, 1))
            self.assertEqual(reg["good"]["state"], "promoted")
            self.assertEqual(reg["bad"]["state"], "parked")      # the gate no longer keeps anything
            self.assertIsNotNone(reg["bad"]["recheck_after"])
            self.assertEqual(reg["hand"]["state"], "promoted")   # configured by hand, not touched
            self.assertEqual(reg["vanished"]["state"], "promoted")  # unreachable today is not a verdict
        finally:
            sources.FETCHER = orig

    def test_the_cap_parks_the_least_promising_and_says_so(self):
        reg = {str(i): {"name": str(i), "state": "promoted", "relevant_titles": i,
                        "resolved": {"ats": "greenhouse", "token": str(i), "postings": 1}}
               for i in range(10)}
        said = []
        self.assertEqual(self.d.apply_cap(reg, 4, log=said.append), 6)
        kept = {k for k, r in reg.items() if r["state"] == "promoted"}
        self.assertEqual(kept, {"9", "8", "7", "6"})         # most relevant titles survive
        self.assertTrue(any("capped at 4" in s for s in said))
        self.assertEqual(self.d.apply_cap(reg, 0, log=said.append), 0)   # 0 means no cap


class FactsTest(unittest.TestCase):
    """Facts are sentences the model reads, not a scoring system of their own.

    The weights and the derived regex checks are gone. They were a second scorer beside the real
    weights in relevance.py, they could not tell "I do not want to do X" from "I have done X",
    and their total saturated on a third of postings, which is why tuning them changed nothing."""

    def test_bullets_parse_as_plain_sentences(self):
        md = ("# About\n\n- Plain fact.\n- Must be remote.\n"
              "- (auto) I tend to skip Acme postings (3 skipped, 0 saved).\n- Wrapped\n  continuation.\n")
        fs = facts.parse_facts(md)
        self.assertEqual([(f.text, f.auto) for f in fs],
                         [("Plain fact.", False), ("Must be remote.", False),
                          ("I tend to skip Acme postings (3 skipped, 0 saved).", True),
                          ("Wrapped continuation.", False)])
        again = facts.parse_facts(facts.render_facts(fs))
        self.assertEqual([f.to_dict() for f in again], [f.to_dict() for f in fs])
        self.assertNotIn("weight", fs[0].to_dict())

    def test_an_old_weighted_about_file_still_loads(self):
        """Nobody has to edit their about.md for the format change: the [N] prefix is stripped."""
        md = "# About\n\n- [3] Must be remote.\n- [1] (auto) I tend to skip Acme postings (3 skipped, 0 saved).\n"
        fs = facts.parse_facts(md)
        self.assertEqual([(f.text, f.auto) for f in fs],
                         [("Must be remote.", False),
                          ("I tend to skip Acme postings (3 skipped, 0 saved).", True)])
        self.assertNotIn("[3]", facts.render_facts(fs))     # and it is rewritten without them

    def test_clean_facts_drops_empties_and_duplicates(self):
        clean = facts.clean_facts([{"text": " a "}, {"text": "a"}, {"text": ""}, {"text": "b"},
                                   facts.Fact("  b  "), facts.Fact("c", True)])
        self.assertEqual([(f.text, f.auto) for f in clean], [("a", False), ("b", False), ("c", True)])

    def test_the_facts_hash_ignores_order_but_not_content(self):
        """It decides whether 1,000 postings get read again, so it has to change for the right
        reasons. Moving a bullet up the page does not change what the model was told."""
        a = [facts.Fact("one"), facts.Fact("two"), facts.Fact("three", True)]
        self.assertEqual(facts.facts_sha(a), facts.facts_sha(list(reversed(a))))
        self.assertNotEqual(facts.facts_sha(a), facts.facts_sha(a[:2]))
        self.assertNotEqual(facts.facts_sha(a), facts.facts_sha([facts.Fact("one"), facts.Fact("two."),
                                                                 facts.Fact("three", True)]))
        # an auto fact counts: the model reads it alongside the rest
        self.assertNotEqual(facts.facts_sha(a), facts.facts_sha(a[:2] + [facts.Fact("four", True)]))
        self.assertEqual(facts.facts_sha([]), facts.facts_sha([facts.Fact("  ")]))

    def test_a_diff_names_what_changed(self):
        before = [facts.Fact("kept"), facts.Fact("dropped")]
        after = [facts.Fact("kept"), facts.Fact("added")]
        self.assertEqual(facts.facts_diff(before, after), {"added": ["added"], "removed": ["dropped"]})

    def test_auto_facts_from_actions_and_suppression(self):
        stats = {"Acme": {"pos": 0, "neg": 3}, "Databricks": {"pos": 2, "neg": 0},
                 "Meh": {"pos": 1, "neg": 1}, "Gone": {"pos": 0, "neg": 5}}
        af = facts.auto_facts(stats, "Alex", {"gone"})
        self.assertEqual([f.text for f in af], ["I tend to skip Acme postings (3 skipped, 0 saved).",
                                                 "I tend to save Databricks postings (2 saved or applied to, 0 skipped)."])
        self.assertTrue(all(f.auto for f in af))
        self.assertEqual(facts.auto_company(af[0]), "Acme")
        human = [facts.Fact("human one"), facts.Fact("old auto", True)]
        merged, changed = facts.merge_auto_facts(human, af)
        self.assertTrue(changed); self.assertEqual([f.text for f in merged][:1], ["human one"])
        self.assertEqual(len(merged), 3)
        self.assertFalse(facts.merge_auto_facts(merged, af)[1])

    def test_the_regex_scorer_is_gone(self):
        for name in ("FactScorer", "TermIndex", "signals_from", "evaluate_fact", "fact_points",
                     "facts_signature", "score_fact", "score_facts", "derive_checks",
                     "validate_check", "describe_check", "load_checks", "save_checks",
                     "missing_checks", "WEIGHT_POINTS", "CHECKS_FILE"):
            self.assertFalse(hasattr(facts, name), f"facts.{name} should be gone")


class AboutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = make_root(Path(tempfile.mkdtemp()))
        self.pdir = self.tmp / "profiles" / "engineer"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_docx_txt_and_heuristic(self):
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                       '<w:body><w:p><w:r><w:t>Analyst at Example Statistics Office, 2016-2021</w:t></w:r></w:p>'
                       '<w:p><w:r><w:t>hello</w:t></w:r></w:p></w:body></w:document>')
        text = about.extract_text("cv.docx", buf.getvalue())
        self.assertIn("Example Statistics Office, 2016-2021", text)
        self.assertEqual(about.extract_text("n.txt", b"abc"), "abc")
        with self.assertRaises(ValueError):
            about.extract_text("x.exe", b"xx")
        got = about.heuristic_digest("Contact\nMA Economics, Example State University 2015\nlorem ipsum dolor sit amet\nLed a team of 4 analysts on survey weighting\nPage 2\nSAS and R daily")
        self.assertEqual(got, ["MA Economics, Example State University 2015", "Led a team of 4 analysts on survey weighting", "SAS and R daily"])

    def test_ingest_uses_llm_when_available_else_heuristic(self):
        import base64
        calls = []
        orig = llm._call
        llm._call = lambda cfg, prompt, max_tokens=400: (calls.append(prompt) or "- Sam spent five years at Example Statistics Office.\n- Sam holds an MA in Economics.\n")
        try:
            res = about.ingest_upload(self.pdir, "resume.txt", base64.b64encode(b"Analyst 2016-2021 at Example").decode(),
                                      {"backend": "claude-cli"}, "Sam")
        finally:
            llm._call = orig
        self.assertTrue(res["used_llm"]); self.assertEqual(len(calls), 1); self.assertIn("Sam", calls[0]); self.assertIn("FIRST person", calls[0])
        got = facts.read_facts(self.pdir)
        self.assertEqual([f.text for f in got][-2:],
                         ["Sam spent five years at Example Statistics Office. (from resume.txt)",
                          "Sam holds an MA in Economics. (from resume.txt)"])
        self.assertEqual(about.list_uploads(self.pdir), ["resume.md"])
        res2 = about.ingest_upload(self.pdir, "notes.md", base64.b64encode(b"- Built a dashboard in 2025\n- random words").decode(), {"backend": "none"})
        self.assertFalse(res2["used_llm"]); self.assertEqual(res2["added"], ["Built a dashboard in 2025 (from notes.md)"])
        with self.assertRaises(ValueError):
            about.ingest_upload(self.pdir, "e.txt", "", {"backend": "none"})

    def test_claude_cli_runs_the_configured_model_and_effort(self):
        from unittest import mock
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout="ok")
            llm._call({"backend": "claude-cli", "model": "claude-sonnet-5", "effort": "medium"}, "hi")
        args = run.call_args[0][0]
        self.assertEqual(args[args.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(args[args.index("--effort") + 1], "medium")

    def test_a_task_table_lays_over_llm(self):
        """[llm.resume] gives the resume tailor its own model and effort; everything else keeps [llm], and the
        task tables never reach _call."""
        from unittest import mock
        cfg = {"backend": "claude-cli", "model": "claude-sonnet-5", "effort": "medium",
               "resume": {"model": "claude-fable-5-1", "effort": "low"}, "other": {"model": "x"}}
        self.assertEqual(llm.task_cfg(cfg), {"backend": "claude-cli", "model": "claude-sonnet-5", "effort": "medium"})
        self.assertEqual(llm.task_cfg(cfg, "resume"),
                         {"backend": "claude-cli", "model": "claude-fable-5-1", "effort": "low"})
        self.assertEqual(llm.task_cfg(cfg, "nope"), llm.task_cfg(cfg))
        self.assertEqual(llm.label(llm.task_cfg(cfg, "resume")), "claude-fable-5-1 (low)")
        self.assertEqual(llm.label({"backend": "anthropic", "model": "m", "effort": "low"}), "m")
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout="ok")
            llm._call(llm.task_cfg(cfg, "resume"), "hi")
        args = run.call_args[0][0]
        self.assertEqual((args[args.index("--model") + 1], args[args.index("--effort") + 1]), ("claude-fable-5-1", "low"))

    def test_the_resume_tailor_is_configured_for_fable_low(self):
        import tomllib
        cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))["llm"]
        self.assertEqual((cfg["resume"]["model"], cfg["resume"]["effort"]), ("claude-fable-5-1", "low"))

    def test_llm_context_is_exactly_about_md(self):
        facts.write_facts(self.pdir, [facts.Fact("Alex builds agents."), facts.Fact("Alex lives in Columbus.")])
        prof = load_profile(self.tmp, "engineer")
        self.assertIn("- Alex lives in Columbus.", prof.notes)
        self.assertNotIn("secret extra context", prof.notes)
        prof.answers.setdefault("answers", {})["facts"] = ["hidden fact"]
        self.assertEqual(llm._background(prof.answers, prof.notes), prof.notes)

    def test_voice_samples_are_not_a_custom_pattern(self):
        # A key written after [answers.custom] belongs to that table, which made voice_samples a regex
        # pattern and hid it from the drafter. Pin the tracked examples, which real profiles are copied from.
        for pdir in (ENGINEER, ANALYST):
            answers = load_toml(pdir / "profile.toml")["answers"]
            self.assertIn("voice_samples", answers, pdir.name)
            self.assertNotIn("voice_samples", answers.get("custom", {}), pdir.name)


# ----------------------------------------------------------------------------- learning

class LearnTest(unittest.TestCase):
    def rec(self, jid, company, title, source="greenhouse"):
        return {"id": jid, "company": company, "title": title, "source": source, "score": 50}

    def test_company_stats_and_duplicates(self):
        jobs = {"a": self.rec("a", "Databricks", "Forward Deployed Engineer, Federal"),
                "b": self.rec("b", "Databricks", "AI Engineer, Forward Deployed"),
                "c": self.rec("c", "Acme", "Solutions Architect, Retail"),
                "d": self.rec("d", "Acme", "Solutions Architect, Healthcare"),
                "n1": self.rec("n1", "Acme", "Solutions Architect, Retail"),
                "n2": self.rec("n2", "Databricks", "AI Engineer, Forward Deployed (NYC)")}
        state = {"a": {"status": "saved"}, "b": {"status": "applied"},
                 "c": {"status": "skip", "skip_reason": "role"}, "d": {"status": "skip", "skip_reason": "company"}}
        model = learn.build_model(jobs, state)
        self.assertEqual((model["n_pos"], model["n_neg"]), (2, 2))
        self.assertEqual(model["companies"], {"Databricks": {"pos": 2, "neg": 0}, "Acme": {"pos": 0, "neg": 2}})
        # what your decisions still do: they become auto facts, which reach the score by being part
        # of what the model reads. The additive penalties they used to apply are gone with the engine.
        self.assertFalse(hasattr(learn, "apply_model"))
        self.assertFalse(hasattr(learn, "final_score"))
        self.assertFalse(hasattr(learn, "adjust"))
        # dup_key survives, because collapse_variants still asks whether two postings are one role
        self.assertEqual(learn.dup_key("Acme", "Solutions Architect, Retail"),
                         learn.dup_key("acme", "Solutions Architect - Retail"))
        self.assertNotEqual(learn.dup_key("Acme", "Solutions Architect"),
                            learn.dup_key("Databricks", "Solutions Architect"))


# ----------------------------------------------------------------------------- the fit migration

class FitMigrationTest(unittest.TestCase):
    """The old fit pass is gone; its 1,000 cached 0-10 ratings are not. They move across as
    `overall_fit` rather than being re-bought from the model."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.jobs = {"a": {"id": "a", "title": "T", "company": "C", "description": "d"},
                     "b": {"id": "b", "title": "U", "company": "C", "description": "e"}}

    def _write_fit(self, postings):
        (self.dir / "fit.json").write_text(json.dumps({"prompt_version": 2, "postings": postings}))

    def test_zero_to_ten_becomes_minus_ten_to_ten_around_a_middling_five(self):
        self._write_fit({"a": {"fit": 9, "why": "great"}, "b": {"fit": 5, "why": "meh"}})
        self.assertEqual(judgemod.migrate_fit(self.dir, self.jobs), 2)
        cache = judgemod.load(self.dir)
        self.assertEqual(cache["a"]["v"]["overall_fit"], 8)      # (9 - 5) * 2
        self.assertEqual(cache["b"]["v"]["overall_fit"], 0)      # the old rubric's honest zero
        self.assertEqual(cache["a"]["why"], "great")
        self.assertEqual(cache["a"]["from"], "fit.json")

    def test_a_migrated_posting_still_owes_every_role_attribute(self):
        self._write_fit({"a": {"fit": 9}})
        judgemod.migrate_fit(self.dir, self.jobs)
        stale = judgemod.stale_dims(self.jobs["a"], judgemod.load(self.dir)["a"])
        self.assertEqual(stale, list(judgemod.ROLE_DIMS))        # every role attribute, and only those
        self.assertNotIn(judgemod.OVERALL_DIM, stale)

    def test_it_runs_once_and_never_overwrites_a_real_rating(self):
        judgemod.save(self.dir, {"a": {"v": {"overall_fit": -6}, "rev": dict(judgemod.DIM_REVS),
                                       "text_sha": judgemod.text_sha(self.jobs["a"])}})
        self._write_fit({"a": {"fit": 10}})
        self.assertEqual(judgemod.migrate_fit(self.dir, self.jobs), 0)
        self.assertEqual(judgemod.load(self.dir)["a"]["v"]["overall_fit"], -6)
        self.assertFalse((self.dir / "fit.json").exists())       # spent, and renamed out of the way
        self.assertTrue((self.dir / judgemod.MIGRATED_FILE).exists())
        self.assertEqual(judgemod.migrate_fit(self.dir, self.jobs), 0)

    def test_nothing_to_migrate_is_not_an_error(self):
        self.assertEqual(judgemod.migrate_fit(self.dir, self.jobs), 0)


class FloorTest(unittest.TestCase):
    """The floor is a grade. A numeric floor would mean something different after every tuning pass,
    because the relevance total has no units and its scale moves whenever a weight moves."""

    def test_the_floor_comes_from_config_or_the_default(self):
        from jobhunt.classify import relevance as rel
        from jobhunt.report import views
        self.assertEqual(rel.min_grade({}), "B-")
        self.assertEqual(rel.min_grade({"scoring": {"min_grade": "A-"}}), "A-")
        self.assertEqual(rel.min_grade({"scoring": {"min_grade": "zzz"}}), "B-")
        cfg = {"scoring": {"min_grade": "B"}}
        self.assertEqual(views.floor_label(cfg), "B")
        self.assertTrue(views.below_bar({"grade": "C+"}, cfg))
        self.assertFalse(views.below_bar({"grade": "A-"}, cfg))
        self.assertFalse(views.below_bar({"grade": "B"}, cfg))
        self.assertTrue(views.below_bar({"grade": None}, cfg))   # never rated, never shown


# ----------------------------------------------------------------------------- scan + store

# ----------------------------------------------------------------------------- relevance

class RelevanceTest(unittest.TestCase):
    """The signed dot product: measures carry the direction, weights carry only the magnitude."""

    def setUp(self):
        from jobhunt.classify import relevance
        self.rel = relevance
        self.w, _notes = relevance.load_weights(CFG)

    def test_weights_start_from_defaults_so_a_partial_override_cannot_zero_the_rest(self):
        partial = {"scoring": {"role": {"pace": 3}}}
        w, notes = self.rel.load_weights(partial)
        self.assertEqual(w["role"]["pace"], 3)
        self.assertEqual(w["role"]["domain"], self.rel.ROLE_DIMS["domain"][0])
        self.assertEqual(len(w["role"]), len(self.rel.ROLE_DIMS))
        self.assertEqual(notes, [])

    def test_bad_weights_are_reported_not_silently_accepted(self):
        w, notes = self.rel.load_weights({"scoring": {"role": {"pace": 99, "nonsense": 4},
                                                      "practical": {"pay": "lots"}}})
        self.assertEqual(w["role"]["pace"], 10)                         # clamped
        self.assertNotIn("nonsense", w["role"])
        self.assertEqual(w["practical"]["pay"], self.rel.PRACTICAL_DIMS["pay"])
        self.assertEqual(len(notes), 3, notes)
        self.assertTrue(any("nonsense" in n for n in notes))

    def test_every_band_the_matcher_emits_is_one_relevance_knows(self):
        """Dead band names pass silently: _location falls through to the miles ramp for anything it
        does not recognise, so a stale name scores plausibly and nothing fails. Pin the vocabulary."""
        from jobhunt.classify.filters import Matcher
        bands = set(Matcher.REACH) | {"remote", "remote_unspecified", "unknown", "non_us"}
        self.assertEqual(bands, {"remote", "remote_unspecified", "daily", "monthly", "quarterly",
                                 "relocate", "unknown", "non_us"})
        # the two the measure treats specially must be spelled the way the matcher emits them
        self.assertEqual(self.rel.practical_measures({"location_fit": "remote"}, CFG)["location"][0], 10.0)
        self.assertEqual(self.rel.practical_measures({"location_fit": "remote_unspecified"}, CFG)["location"][0], 6.0)
        # and the dashboard has a label for every one of them
        html = (ROOT / "jobhunt" / "report" / "dashboard.html").read_text(encoding="utf-8")
        line = next(x for x in html.split("\n") if x.startswith("function fitLabel("))
        for b in bands:
            self.assertIn(f"{b}:", line, b)

    def test_zero_means_absent_and_costs_nothing(self):
        """The whole reason measures are signed: silence must not be punished."""
        bare = {"location_fit": "unknown", "recency": {"level": "mid"}}
        parts = self.rel.practical_measures(bare, CFG)
        for dim in ("pay", "clearance", "travel"):
            self.assertEqual(parts[dim][0], 0.0, dim)
        total, _ = self.rel.score({d: 0 for d in self.rel.ROLE_DIMS}, parts, self.w)
        self.assertEqual(total, 0.0)

    def test_a_bad_attribute_subtracts_rather_than_merely_failing_to_add(self):
        parts = self.rel.practical_measures({"location_fit": "unknown"}, CFG)
        absent, _ = self.rel.score({"work_shape": 0}, parts, self.w)
        present, _ = self.rel.score({"work_shape": -8}, parts, self.w)
        self.assertLess(present, absent)
        # it moves the total by exactly its applied multiplier times the change in measure
        self.assertEqual(present - absent, -8 * self.w["role"]["work_shape"] * self.w["role_weight"] / 10)

    def test_role_attributes_count_individually(self):
        """Each role attribute adds weight x measure on its own, so a
        second-tier attribute (product ownership, mission, variety) carries real weight. They used to
        be averaged into one family term, capping every one of them near +-12."""
        parts = self.rel.practical_measures({"location_fit": "unknown"}, CFG)
        base = {d: 0 for d in self.rel.ROLE_DIMS}
        t0, _ = self.rel.score(base, parts, self.w)
        for d in self.rel.ROLE_DIMS:
            t1, _ = self.rel.score({**base, d: 10}, parts, self.w)
            self.assertAlmostEqual(t1 - t0, 10 * self.w["role"][d] * self.w["role_weight"] / 10, places=6)

    def test_the_holistic_read_is_a_weighted_attribute_like_any_other(self):
        parts = self.rel.practical_measures({"location_fit": "unknown"}, CFG)
        role = {d: 0 for d in self.rel.ROLE_DIMS}
        low, _ = self.rel.score(role, parts, self.w, overall=-10.0)
        high, shown = self.rel.score(role, parts, self.w, overall=10.0)
        self.assertEqual(high - low, 20 * self.w["overall_fit"])
        self.assertIn("overall_fit", [x["dim"] for x in shown])
        self.assertIsNone(self.rel.overall_of({}))
        self.assertEqual(self.rel.overall_of({"role": {"overall_fit": 10}}), 10.0)
        self.assertIsNone(self.rel.overall_of({"role": {"agentic_llm": 9}}))   # rated, but not on this

    def test_whether_a_clearance_helps_or_hurts_is_config_not_code(self):
        """It was hardcoded at -10, which silently encoded one person's situation for
        everybody. Someone who holds one should see those postings rise, not fall."""
        needs = {"clearance": {"mentioned": True, "active_required": True}}
        elig = {"clearance": {"mentioned": True, "active_required": False, "eligibility_ok": True}}
        silent = {"clearance": {"mentioned": False}}
        def measure(rec, holds):
            return self.rel.practical_measures(rec, {"scoring": {"clearance_active": holds}})["clearance"][0]
        self.assertEqual(measure(needs, False), -10.0)
        self.assertEqual(measure(needs, True), 8.0)
        self.assertEqual(measure(elig, False), 3.0)        # a door you could open either way
        self.assertEqual(measure(elig, True), 3.0)
        self.assertEqual(measure(silent, False), 0.0)      # never punished for staying silent
        self.assertEqual(measure(silent, True), 0.0)
        self.assertEqual(measure(needs, False), measure(needs, "")) # absent config means not held

    def test_remote_beats_any_office_that_is_not_home(self):
        far = CFG.get("location", {}).get("radius_miles", 500)
        remote = self.rel.practical_measures({"location_fit": "remote"}, CFG)["location"][0]
        home = self.rel.practical_measures({"location_fit": "daily", "location_miles": 5}, CFG)["location"][0]
        dc = self.rel.practical_measures({"location_fit": "monthly", "location_miles": 98}, CFG)["location"][0]
        edge = self.rel.practical_measures({"location_fit": "quarterly", "location_miles": far}, CFG)["location"][0]
        self.assertGreater(remote, home)
        self.assertGreater(home, dc)
        self.assertGreaterEqual(dc, edge)              # both past Alex's red line (an hour, 3 days assumed)
        self.assertEqual(edge, -10.0)                  # so both sit at the floor

    def test_an_unjudged_posting_has_no_relevance_at_all(self):
        parts = self.rel.practical_measures({"location_fit": "remote"}, CFG)
        total, shown = self.rel.score(None, parts, self.w)
        self.assertIsNone(total)                       # never imputed, so it cannot outrank a real one
        self.assertTrue(shown)                         # but the practical terms are still explainable

    def test_parts_explain_the_total_exactly(self):
        parts = self.rel.practical_measures({"location_fit": "remote", "recency": {"level": "fresh"}}, CFG)
        role = {d: 4 for d in self.rel.ROLE_DIMS}
        total, shown = self.rel.score(role, parts, self.w)
        self.assertAlmostEqual(total, sum(p["points"] for p in shown), places=1)
        self.assertEqual(len(shown), len(self.rel.ROLE_DIMS) + len(self.rel.PRACTICAL_DIMS))
        self.assertGreaterEqual(abs(shown[0]["points"]), abs(shown[-1]["points"]))   # biggest first

    def test_grades_come_from_the_distribution_and_keep_their_order(self):
        totals = [float(x) for x in range(-200, 201, 4)]
        cuts = self.rel.fit_curve(totals)
        self.assertEqual(len(cuts), len(self.rel.GRADES) - 1)
        self.assertEqual(cuts, sorted(cuts, reverse=True))
        mean = sum(totals) / len(totals)
        sd = (sum((x - mean) ** 2 for x in totals) / len(totals)) ** 0.5
        self.assertEqual(self.rel.grade(mean + 3 * sd, cuts), "A+")
        self.assertEqual(self.rel.grade(mean - 3 * sd, cuts), "F")
        self.assertEqual(self.rel.grade(mean, cuts), "B-")            # the middle of the pack
        self.assertIsNone(self.rel.grade(None, cuts))
        self.assertTrue(self.rel.at_least("A", "B-"))
        self.assertFalse(self.rel.at_least("C+", "B-"))
        self.assertFalse(self.rel.at_least(None, "F"))                # ungraded is never good enough

    def test_a_weak_crop_earns_no_top_grades(self):
        """Sigma cutoffs, not quantiles: a bad week must not manufacture an A+ out of the least bad."""
        flat = [100.0 + (i % 3) for i in range(60)]                   # no real spread, no outliers
        cuts = self.rel.fit_curve(flat)
        self.assertNotIn(self.rel.grade(max(flat), cuts), ("A+", "A"))


class EngineSwitchTest(unittest.TestCase):
    """There is one engine. The switch that chose between them is gone, and so is everything it
    selected: nothing may reintroduce a second way of ordering the list."""

    def setUp(self):
        from jobhunt.classify import relevance
        from jobhunt.report import views
        self.rel, self.views = relevance, views

    def test_there_is_no_engine_flag_left_to_set(self):
        from jobhunt.classify import learn
        for gone in ("engine", "FIT_BANDS", "final_score"):
            self.assertFalse(hasattr(self.rel, gone), gone)
            self.assertFalse(hasattr(learn, gone), gone)
        # and a leftover flag in someone's config is inert, not a second code path
        cfg = {"scoring": {"engine": "bands", "min_score": 60, "min_grade": "B"}}
        self.assertEqual(self.views.floor_label(cfg), "B")
        self.assertTrue(self.views.below_bar({"score": 99, "grade": "C"}, cfg))

    def test_grading_a_store_freezes_the_curve_until_the_weights_move(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            jobs = {f"j{i}": {"active": True, "role": {d: (i % 21) - 10 for d in self.rel.ROLE_DIMS},
                              "location_fit": "remote", "recency": {"level": "fresh"}}
                    for i in range(40)}
            cfg = {"scoring": {"engine": "relevance"}, "location": {"radius_miles": 500}}
            graded, cuts = self.rel.apply_to(jobs, cfg, tmp)
            self.assertEqual(graded, 40)
            self.assertTrue(all(j.get("relevance") is not None for j in jobs.values()))
            self.assertTrue(all(j.get("score_parts") for j in jobs.values()))
            again, cuts2 = self.rel.apply_to(jobs, cfg, tmp)
            self.assertEqual(cuts, cuts2)                       # held still between runs
            heavier = {**cfg, "scoring": {**cfg["scoring"], "role": {"pace": 0}}}
            _n, cuts3 = self.rel.apply_to(jobs, heavier, tmp)
            self.assertNotEqual(cuts, cuts3)                    # a weight change refits it
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class JudgeTest(unittest.TestCase):
    """Per-attribute invalidation: a reworded attribute must not discard the whole corpus."""

    def setUp(self):
        from jobhunt.classify import judge
        self.judge = judge
        self.tmp = Path(tempfile.mkdtemp())
        self.rec = {"id": "x:1", "title": "Applied AI Engineer", "company": "Ex", "description": "agents"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rated(self):
        return {"v": {d: 5 for d in self.judge.ALL_DIMS},
                "rev": dict(self.judge.DIM_REVS), "text_sha": self.judge.text_sha(self.rec)}

    def test_a_fully_rated_posting_needs_nothing(self):
        self.assertEqual(self.judge.stale_dims(self.rec, self._rated()), [])
        self.assertEqual(self.judge.stale_dims(self.rec, None), list(self.judge.ALL_DIMS))

    def test_rewording_one_attribute_restates_only_that_slot(self):
        rated = self._rated()
        moved = next(iter(self.judge.ALL_DIMS))
        rated["rev"][moved] += 1                       # as if DIM_REVS had been bumped past it
        self.assertEqual(self.judge.stale_dims(self.rec, rated), [moved])

    def test_an_edited_posting_is_rated_again_from_scratch(self):
        rated = self._rated()
        changed = {**self.rec, "description": "release chores and patching"}
        self.assertEqual(self.judge.stale_dims(changed, rated), list(self.judge.ALL_DIMS))

    def test_the_cache_round_trips_and_a_schema_change_discards_it(self):
        self.judge.save(self.tmp, {"x:1": self._rated()})
        self.assertEqual(set(self.judge.load(self.tmp)), {"x:1"})
        p = self.tmp / self.judge.JUDGEMENTS_FILE
        raw = json.loads(p.read_text()); raw["schema"] = self.judge.SCHEMA + 1
        p.write_text(json.dumps(raw))
        self.assertEqual(self.judge.load(self.tmp), {})

    def test_ratings_are_parsed_clamped_and_attached(self):
        posts = [{"id": f"p{i}", "title": "t", "company": "c", "description": "d"} for i in range(2)]
        dims = list(self.judge.ROLE_DIMS)[:3]
        reply = json.dumps([{"i": 1, **{d: 99 for d in dims}, "why": "over"},
                            {"i": 2, **{d: -99 for d in dims}, "why": "under"}])
        orig = llm._call
        llm._call = lambda cfg, prompt, max_tokens=400: reply
        try:
            got = self.judge.judge({"backend": "claude-cli"}, "facts", posts, dims=dims, batch=4)
        finally:
            llm._call = orig
        self.assertEqual(got["p0"]["v"][dims[0]], 10)      # clamped to the scale
        self.assertEqual(got["p1"]["v"][dims[0]], -10)
        self.assertEqual(self.judge.judge({"backend": "none"}, "f", posts), {})
        jobs = {"p0": {}, "p1": {}, "p2": {}}
        self.judge.attach(jobs, got)
        self.assertEqual(jobs["p0"]["role"][dims[0]], 10)
        self.assertNotIn("role", jobs["p2"])

    def test_overall_fit_is_scheduled_for_rating_but_never_averaged_in(self):
        """The trap: relevance averages ROLE_DIMS. Putting the holistic read in that family would
        dilute it to a tenth of itself; leaving it out of the scheduling list would mean it is never
        rated at all. It has to be in one list and not the other."""
        from jobhunt.classify import relevance as rel
        self.assertNotIn(self.judge.OVERALL_DIM, rel.ROLE_DIMS)          # not averaged
        self.assertIn(self.judge.OVERALL_DIM, self.judge.ALL_DIMS)       # but rated
        self.assertIn(self.judge.OVERALL_DIM, self.judge.DIM_REVS)       # and invalidatable
        self.assertEqual(self.judge.stale_dims({"title": "t"}, None)[-1], self.judge.OVERALL_DIM)
        self.assertIn("whole-role verdict", self.judge.rubric(self.judge.OVERALL_DIM))
        # it reaches the score as its own term, at its own weight
        w, _n = rel.load_weights(CFG)
        parts = rel.practical_measures({"location_fit": "unknown"}, CFG)
        role = {d: 0 for d in rel.ROLE_DIMS}
        low, _ = rel.score(role, parts, w, overall=rel.overall_of({"role": {**role, "overall_fit": -10}}))
        high, _ = rel.score(role, parts, w, overall=rel.overall_of({"role": {**role, "overall_fit": 10}}))
        self.assertEqual(high - low, 20 * w["overall_fit"])

    def test_the_prompt_says_zero_means_absent(self):
        """The one instruction the whole signed design rests on."""
        self.assertIn("ABSENT", self.judge.PROMPT)
        self.assertIn("Zero is not a hedge", self.judge.PROMPT)

    def test_the_consolidated_attributes_reach_the_model(self):
        """domain, work_shape and enterprise_software are rated, worded as specified;
        seniority_fit was reworded to the near-binary and re-rates its own slot and nothing else; the
        attributes that were collapsed or removed are no longer asked for at all."""
        text = self.judge._prompt("Alex", "", [{"title": "t", "company": "c"}], self.judge.ALL_DIMS)
        for dim, phrase in (("feedback_loop", "hours or days"), ("pace", "hair-on-fire"),
                            ("equity_stage", "Series A-C"), ("domain", "agentic AI"),
                            ("domain", "consumer apps"), ("seniority_fit", "+5 senior"),
                            ("work_shape", "greenfield"), ("enterprise_software", "sold to other companies")):
            self.assertIn(f"  {dim}: ", text)
            self.assertIn(phrase, text)
        for gone in ("forward_deployed", "algorithmic_depth", "agentic_llm", "autonomy_robotics",
                     "mission_domain", "variety", "toil", "independence"):
            self.assertNotIn(f"  {gone}: ", text)
            self.assertNotIn(gone, self.judge.ALL_DIMS)
        self.assertNotIn("toil", self.judge.PROMPT)                       # the zero example names pace now
        self.assertEqual(self.judge.DIM_REVS["seniority_fit"], 2)
        from jobhunt.classify import relevance
        self.assertEqual(relevance.ROLE_DIMS["enterprise_software"][0], 0)  # a gate reads it, the score never
        # a posting rated under the old rubric owes exactly the reworded slot and the new ones
        old_dims = ["product_ownership", "feedback_loop", "seniority_fit", "pace", "equity_stage",
                    "mission_domain", "variety", "toil", self.judge.OVERALL_DIM]
        old = {"text_sha": self.judge.text_sha({"title": "t", "company": "c"}),
               "rev": {d: 1 for d in old_dims}, "v": {d: 0 for d in old_dims}}
        self.assertEqual(sorted(self.judge.stale_dims({"title": "t", "company": "c"}, old)),
                         ["domain", "enterprise_software", "seniority_fit", "work_shape"])


# ----------------------------------------------------------------------------- pipeline stages

class StagesTest(unittest.TestCase):
    """Each stage of a scan, on its own. Most of these had no direct test before."""

    def setUp(self):
        sources.FETCHER = fake_fetcher
        self.tmp = make_root(Path(tempfile.mkdtemp()))
        self.shared, self.profiles = load_all(self.tmp)

    def tearDown(self):
        sources.FETCHER = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_is_pure_and_names_what_the_run_will_touch(self):
        from jobhunt import pipeline
        plan = pipeline.stage_plan(self.profiles)
        self.assertEqual(sorted(plan["profiles"]), sorted(self.profiles))
        self.assertTrue(plan["companies"])
        self.assertTrue(plan["title_filter"]("Applied AI Engineer"))
        self.assertFalse(plan["title_filter"]("Chief Revenue Officer"))
        self.assertFalse(plan["keep_for_cache"](_job(location="London, UK")))
        self.assertTrue(plan["keep_for_cache"](_job(location="Remote - United States")))
        # planning must not touch the network or the disk
        self.assertEqual(pipeline.stage_plan(self.profiles)["companies"], plan["companies"])

    def test_progress_reports_stage_and_position(self):
        from jobhunt.pipeline import Progress
        lines = []
        prog = Progress(lines.append)
        prog.begin("fetch", total=3, note="3 boards")
        prog.step(); prog.step()
        snap = prog.snapshot()
        self.assertEqual((snap["stage"], snap["done"], snap["total"]), ("fetch", 2, 3))
        prog.begin("cache")                       # beginning another closes the first
        self.assertEqual(prog.snapshot()["finished"][0]["stage"], "fetch")
        prog.end("written")
        self.assertIsNone(prog.snapshot()["stage"])
        self.assertEqual([f["stage"] for f in prog.snapshot()["finished"]], ["fetch", "cache"])
        self.assertTrue(any("[fetch]" in x for x in lines))

    def test_fetch_resumes_instead_of_refetching(self):
        from jobhunt import pipeline
        plan = pipeline.stage_plan(self.profiles)
        first, errors = pipeline.fetch_all(plan["companies"], log=lambda _m: None,
                                           title_filter=plan["title_filter"], root=self.tmp)
        partial = self.tmp / "data" / pipeline.PARTIAL_FILE
        self.assertTrue(partial.exists(), "a fetch should leave a resume point behind")
        # a second fetch with every board already banked must not call the network at all
        sources.FETCHER = lambda *a, **k: self.fail("resumed fetch hit the network")
        again, _errs = pipeline.fetch_all(plan["companies"], log=lambda _m: None,
                                          title_filter=plan["title_filter"], root=self.tmp)
        sources.FETCHER = fake_fetcher
        self.assertEqual(sorted(j.id for j in again), sorted(j.id for j in first))
        pipeline.save_raw(self.tmp, first, errors, "2026-09-23T00:00:00+00:00")
        self.assertFalse(partial.exists(), "banking the cache should spend the resume point")

    def test_a_job_you_could_not_turn_up_to_is_retired_not_demoted(self):
        """The whole point. An office 100 miles away that wants you in three days a week is not a
        worse job, it is one you cannot hold, so it is a gate and not a weight."""
        m = Matcher(CFG)                       # daily_miles 60, monthly_miles 120: Cincinnati is 100
        def go(desc, **kw):
            return m.gate(_job(title="Applied AI Engineer", location="Cincinnati, OH",
                               description=desc, **kw))
        # stated cadence you could not meet -> dropped, and the reason names both sides
        out = go("You will work out of our Cincinnati office 3 days per week.")
        self.assertFalse(out["keep"])
        self.assertEqual(out["onsite_need"], "weekly")
        self.assertIn("several days a week", out["drop_reason"])
        self.assertTrue(any("could manage a day or so a month" in r for r in out["reasons"]))
        self.assertIn("3 days per week", out["onsite_quote"])
        # the board's structured onsite flag counts as daily presence
        flagged = go("A great role.", remote_signal="onsite")
        self.assertFalse(flagged["keep"]); self.assertEqual(flagged["onsite_need"], "daily")
        # a cadence you COULD meet is kept
        ok = go("Expect to visit the Cincinnati office once a month.")
        self.assertTrue(ok["keep"]); self.assertEqual(ok["reach"], "monthly")
        # silence is not evidence: most postings say nothing, and a gate must not act on a guess
        quiet = go("A great role with no arrangement stated.")
        self.assertTrue(quiet["keep"]); self.assertIsNone(quiet["onsite_need"])
        # and the same demand is fine when the office is a daily drive
        near = m.gate(_job(title="Applied AI Engineer", location="Columbus, OH",
                           description="You will work out of our office 3 days per week."))
        self.assertTrue(near["keep"]); self.assertEqual(near["reach"], "daily")
        # a remote role is never gated on presence, whatever its description mentions
        rem = m.gate(_job(title="Applied AI Engineer", location="Remote - United States",
                          description="Our HQ runs 5 days a week in office."))
        self.assertTrue(rem["keep"])

    def test_remote_or_daily_keeps_only_remote_and_home(self):
        """An office you would have to travel to, however rarely, goes. Remote and a
        daily drive stay; a place the gazetteer cannot put anywhere stays too (silence never drops)."""
        m = Matcher(merge_config(CFG, {"location": {"mode": "remote_or_daily"}}))
        go = lambda **kw: m.gate(_job(title="Applied AI Engineer", **kw))   # noqa: E731
        for where in ("Cincinnati, OH", "Cleveland, OH", "New York, NY", "San Francisco, CA"):
            out = go(location=where)
            self.assertFalse(out["keep"], where)
            self.assertEqual(out["drop_reason"], "office beyond a daily drive")
            self.assertTrue(any("miles away you would have to visit" in r for r in out["reasons"]), out["reasons"])
        self.assertIn("100 miles", go(location="Cincinnati, OH")["reasons"][-1])
        for kw in (dict(location="Columbus, OH"), dict(location="Remote - United States"),
                   dict(location="Remote"), dict(location="Washington, DC", remote_signal="remote"),
                   dict(location="Our offices")):
            self.assertTrue(go(**kw)["keep"], kw)
        self.assertEqual(go(location="Columbus, OH")["location_fit"], "daily")
        self.assertEqual(go(location="Our offices")["location_fit"], "unknown")
        # the shared default is untouched: the same Cincinnati office is a kept monthly band there
        self.assertTrue(Matcher(CFG).gate(_job(title="Applied AI Engineer", location="Cincinnati, OH"))["keep"])
        from jobhunt.classify import gates
        self.assertEqual(gates.location_gate("monthly", "remote_or_daily", False, 500)[1],
                         "dropped: an office you would have to visit")

    def test_a_place_we_cannot_pin_down_never_claims_to_be_near(self):
        """Two coarse matches used to read as "at my front door": the fallback vocabulary returned
        0.0 miles, and a bare state name resolves to its centroid, which can sit 30 miles from home."""
        m = Matcher(CFG)
        for seg in ("Central Ohio", "Short North", "Polaris Parkway"):
            fit, _r = m.location_fit(_job(location=seg))
            self.assertEqual(fit, "monthly", seg)
            self.assertGreater(m._last_miles, m.daily_miles, seg)
        for seg in ("Ohio", "ohio, united states"):
            fit, _r = m.location_fit(_job(location=seg))
            self.assertNotEqual(fit, "daily", seg)
        # a real place inside the daily drive still bands daily
        self.assertEqual(m.location_fit(_job(location="Westerville, OH"))[0], "daily")

    def test_gate_and_measure_split_cleanly(self):
        m = Matcher(CFG)
        dropped = m.gate(_job(title="Head of Marketing"))
        self.assertFalse(dropped["keep"])
        self.assertNotIn("salary", dropped)          # nothing is measured for a posting being dropped
        kept = m.gate(_job(title="Applied AI Engineer", location="Remote - United States"))
        self.assertTrue(kept["keep"])
        self.assertEqual(kept["location_fit"], "remote")
        self.assertIsNone(kept["reach"])             # no office to reach, so the gate stays out of it
        measured = m.measure(_job(title="Staff Applied AI Engineer",
                                  description="Up to 10% travel. $200,000—$250,000 USD."))
        self.assertEqual(set(measured), {"travel", "salary", "clearance", "seniority", "recency"})
        self.assertEqual(measured["seniority"], "staff")
        self.assertEqual(measured["salary"]["max"], 250000)
        # and composed they still equal what classify returns
        job = _job(title="Applied AI Engineer", location="Remote - United States", description="x")
        whole = m.classify(job)
        self.assertEqual(whole["location_fit"], m.gate(job)["location_fit"])
        self.assertEqual(whole["seniority"], m.measure(job)["seniority"])

    def _llm_on(self):
        """Point the profile at a mocked model and hand back what it was asked."""
        prompts = []
        rated = ", ".join(f'"{d}": {i - 5}' for i, d in enumerate(judgemod.ALL_DIMS))
        orig = llm._call
        llm._call = lambda cfg, prompt, max_tokens=400: (
            prompts.append(prompt) or "[" + ",".join(
                '{"i": %d, %s, "why": "rated"}' % (i + 1, rated) for i in range(4)) + "]")
        self.addCleanup(lambda: setattr(llm, "_call", orig))
        for p in self.profiles.values():
            p.search.setdefault("llm", {})["backend"] = "claude-cli"
        return prompts

    def test_judge_is_a_stage_of_a_scan_and_its_ratings_reach_the_score(self):
        """The whole point of the phase: a scan now produces its own ratings. Before this, judge_pass
        had no caller anywhere, so every rating in the corpus had been made by hand."""
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan
        prompts = self._llm_on()
        scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp))
        self.assertTrue(prompts, "a scan with a backend configured must ask the model something")
        cache = judgemod.load(self.profiles["engineer"].data_dir)
        self.assertTrue(cache)
        rec = self.profiles["engineer"].store.jobs["jobs"][next(iter(cache))]
        self.assertEqual(rec["role"]["overall_fit"], len(judgemod.ALL_DIMS) - 6)
        self.assertIsNotNone(rec.get("relevance"), "a rated posting must come out of the scan scored")

    def test_no_judge_leaves_the_model_alone(self):
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan
        prompts = self._llm_on()
        scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
        self.assertEqual(prompts, [])
        self.assertEqual(judgemod.load(self.profiles["engineer"].data_dir), {})

    def test_a_stage_subset_replays_the_cache_instead_of_fetching(self):
        """`--stage` exists so re-tuning costs no network. Leaving `fetch` out must not silently mean
        fetching nothing -- it means replaying what the last fetch banked."""
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan
        first = scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
        sources.FETCHER = lambda *a, **k: self.fail("a stage subset without fetch hit the network")
        again = scan(self.tmp, self.profiles, log=lambda _m: None,
                     stages=("gate", "measure", "score", "store"), judge=False)
        sources.FETCHER = fake_fetcher
        from jobhunt.pipeline import load_raw
        cached, _errs, _at = load_raw(self.tmp)
        # the replay sees the cache, which is everything the fetch kept: wide, but not the plainly
        # foreign postings save_raw drops on the way in
        self.assertEqual(again["fetched"], len(cached))
        self.assertLessEqual(again["fetched"], first["fetched"])
        self.assertEqual(again["scanned_at"], first["scanned_at"])
        stages = {f["stage"] for f in again["progress"]["finished"]}
        self.assertNotIn("fetch", stages); self.assertNotIn("cache", stages)
        self.assertIn("gate", stages); self.assertIn("store", stages)

    def test_judge_order_is_the_practical_measures_not_the_old_score(self):
        """What gets read first when --judge-limit bites. The old `score` is on its way out, and for
        an unjudged posting it was mostly the absent fit term anyway."""
        from jobhunt.pipeline import judge_candidates
        base = {"active": True, "recency": {"level": "fresh"}}
        jobs = {
            "far":    {**base, "id": "far", "title": "t", "company": "c", "description": "d",
                       "score": 999, "location_fit": "quarterly", "location_miles": 480},
            "remote": {**base, "id": "remote", "title": "t", "company": "c", "description": "d",
                       "score": 1, "location_fit": "remote"},
            "stale":  {"active": True, "recency": {"level": "stale"}, "id": "stale", "title": "t",
                       "company": "c", "description": "d", "score": 998, "location_fit": "remote"},
        }
        order = [r["id"] for r, _d in judge_candidates(jobs, {}, {}, search_cfg=CFG)]
        self.assertEqual(order[0], "remote")   # best circumstances, despite the worst old score
        self.assertEqual(order[-1], "stale")   # recency still outranks everything
        self.assertEqual([r["id"] for r, _d in judge_candidates(jobs, {}, {}, limit=1,
                                                                search_cfg=CFG)], ["remote"])

    def test_variants_collapse_onto_the_best_rated_of_the_group(self):
        """One role posted once per city. The primary used to be picked by `score`, a field that no
        longer exists -- which would have tied every group and fallen back to id order."""
        from jobhunt.pipeline import collapse_variants
        def rec(jid, rel=None, seen="2026-09-01"):
            return {"id": jid, "company": "Acme", "title": "Forward Deployed Engineer",
                    "relevance": rel, "first_seen": seen, "grade": "B" if rel else None}
        jobs = {"a": rec("a", 10.0), "b": rec("b", 90.0), "c": rec("c", 50.0)}
        collapse_variants(jobs, set(jobs))
        self.assertIsNone(jobs["b"]["duplicate_of"])              # the best relevance leads
        self.assertEqual(jobs["a"]["duplicate_of"], "b")
        self.assertEqual({v["id"] for v in jobs["b"]["variants"]}, {"a", "c"})
        self.assertEqual(jobs["a"]["variants"], [])
        self.assertIn("grade", jobs["b"]["variants"][0])          # what the row shows, not a score

        # an unrated posting must not beat a rated one by having its absent total read as zero
        jobs = {"rated": rec("rated", -40.0), "unrated": rec("unrated", None)}
        collapse_variants(jobs, set(jobs))
        self.assertIsNone(jobs["rated"]["duplicate_of"])
        # all unrated: first_seen decides, so the same primary is picked every run
        jobs = {"late": rec("late", None, "2026-09-09"), "early": rec("early", None, "2026-09-02")}
        collapse_variants(jobs, set(jobs))
        self.assertIsNone(jobs["early"]["duplicate_of"])
        # a posting you acted on keeps the group in the tab you put it in, whatever it scores
        jobs = {"best": rec("best", 90.0), "acted": rec("acted", 1.0)}
        collapse_variants(jobs, set(jobs), {"acted": {"status": "saved", "updated_at": "2026-09-10"}})
        self.assertIsNone(jobs["acted"]["duplicate_of"])
        self.assertEqual(jobs["best"]["duplicate_of"], "acted")

    def test_excluding_a_company_retires_what_is_already_stored(self):
        """classify only ever sees the latest fetch, so adding an employer to the list would strand
        everything already in the store: still active, never re-evaluated, never removed."""
        from jobhunt.pipeline import retire_excluded_companies
        p = self.profiles["engineer"]
        p.store.jobs["jobs"] = {
            "keep": {"id": "keep", "company": "Ex GH", "active": True},
            "drop": {"id": "drop", "company": "Acme Services, LLC", "active": True},
            "already": {"id": "already", "company": "Acme", "active": False},
        }
        self.assertEqual(retire_excluded_companies(p), 0)        # nothing excluded yet
        self.assertTrue(p.store.jobs["jobs"]["drop"]["active"])
        p.search["search"] = {**(p.search.get("search") or {}), "company_exclude": ["acme"]}
        self.assertEqual(retire_excluded_companies(p), 1)        # matched as a substring
        self.assertFalse(p.store.jobs["jobs"]["drop"]["active"])
        self.assertIn("acme", p.store.jobs["jobs"]["drop"]["drop_reason"])
        self.assertTrue(p.store.jobs["jobs"]["keep"]["active"])
        self.assertEqual(retire_excluded_companies(p), 0)        # idempotent

    def test_enterprise_software_is_retired_unless_the_base_clears_the_floor(self):
        """The one gate driven by a model reading: rated 7+ on enterprise_software
        and no stated base above the profile's floor. Silence (0) never trips it, unstated pay never clears it, a
        decided posting is retired like any other and keeps its status, and 0 in [enterprise] turns
        the gate off."""
        from jobhunt.pipeline import retire_enterprise, score_relevance
        p = self.profiles["engineer"]
        gate = {"rating": 7, "pay_floor": 250_000}        # the shipped config leaves the gate off
        p.search["enterprise"] = dict(gate)
        def rec(i, rating, top=None, **kw):
            return {"id": i, "title": "t", "company": "c", "active": True, "location_fit": "remote",
                    "role": {"enterprise_software": rating, "overall_fit": 1},
                    "salary": {"max": top, "min": None, "text": None}, "reasons": ["kept"], **kw}
        p.store.jobs["jobs"] = {
            "vendor": rec("vendor", 8),
            "vendor_paid": rec("vendor_paid", 9, top=320_000),
            "vendor_at_floor": rec("vendor_at_floor", 7, top=250_000),
            "borderline": rec("borderline", 6),
            "silent": rec("silent", 0),
            "unrated": {"id": "unrated", "title": "t", "company": "c", "active": True, "role": {}},
            "saved": rec("saved", 10),
            "gone": rec("gone", 10, active=False, drop_reason="non-US location"),
        }
        p.store.state["jobs"]["saved"] = {"status": "saved"}
        self.assertEqual(retire_enterprise(p), 3)
        j = p.store.jobs["jobs"]
        for i in ("vendor", "vendor_at_floor", "saved"):
            self.assertFalse(j[i]["active"], i)
            self.assertEqual(j[i]["drop_reason"], f"enterprise software (rated {j[i]['role']['enterprise_software']}), no stated base above $250k")
            self.assertEqual(j[i]["reasons"][-1], "dropped: " + j[i]["drop_reason"])
        for i in ("vendor_paid", "borderline", "silent", "unrated"):
            self.assertTrue(j[i]["active"], i)
        self.assertEqual(p.store.state["jobs"]["saved"]["status"], "saved")   # stays in its tab
        self.assertEqual(j["gone"]["drop_reason"], "non-US location")          # already retired: untouched
        self.assertEqual(retire_enterprise(p), 0)                              # idempotent
        # the gate can be turned off, and its thresholds moved, from [enterprise]
        p.store.jobs["jobs"]["vendor"]["active"] = True
        p.search["enterprise"] = {"rating": 0}
        self.assertEqual(retire_enterprise(p), 0)
        p.search["enterprise"] = {"rating": 9, "pay_floor": 400_000}
        self.assertEqual(retire_enterprise(p), 1)                              # vendor_paid now falls short
        self.assertFalse(j["vendor_paid"]["active"]); self.assertTrue(j["vendor"]["active"])
        # score_relevance applies it, so scan, rescore and relearn all do
        p.search["enterprise"] = dict(gate)
        p.store.jobs["jobs"] = {"v": rec("v", 8)}
        judgemod.save(p.data_dir, {"v": {"v": {"enterprise_software": 8, "overall_fit": 1},
                                         "rev": {"enterprise_software": 1, "overall_fit": 1}}})
        score_relevance(p)
        self.assertFalse(p.store.jobs["jobs"]["v"]["active"])

    def test_score_attaches_ratings_from_the_cache_and_grades(self):
        """`score` reads the ratings cache rather than what is attached to the records, so it stays
        correct while a judging run is still checkpointing into it."""
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan, score_relevance
        scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
        p = self.profiles["engineer"]
        jobs = p.store.jobs["jobs"]
        self.assertEqual(score_relevance(p), 0)                  # nothing read yet, nothing graded
        self.assertTrue(all(r.get("relevance") is None for r in jobs.values()))
        self.assertTrue(all(r.get("grade") is None for r in jobs.values()))
        # 7 on everything would trip the enterprise gate; that attribute stays 0 here
        cache = {jid: {"v": {**{d: 7 for d in judgemod.ALL_DIMS}, "enterprise_software": 0},
                       "rev": dict(judgemod.DIM_REVS), "why": "cached", "text_sha": judgemod.text_sha(rec)}
                 for jid, rec in jobs.items()}
        judgemod.save(p.data_dir, cache)
        self.assertEqual(score_relevance(p), len(jobs))
        one = jobs[next(iter(jobs))]
        self.assertEqual(one["role"]["overall_fit"], 7)
        self.assertEqual(one["role_why"], "cached")
        self.assertIsNotNone(one["relevance"]); self.assertIsNotNone(one["grade"])
        self.assertEqual(round(sum(x["points"] for x in one["score_parts"]), 1), one["relevance"])

    def test_an_interrupted_judge_pass_keeps_what_it_already_paid_for(self):
        """Rating 900 postings is the expensive part of a scan. Losing it to a crash at posting 880,
        or to the model rate-limiting halfway, would mean buying the whole thing again."""
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan, judge_pass, judge_candidates
        from jobhunt.classify import judge as jm
        scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
        p = self.profiles["engineer"]
        p.search.setdefault("llm", {})["backend"] = "claude-cli"
        rated = ", ".join(f'"{d}": 4' for d in jm.ALL_DIMS)
        calls = []

        def flaky(cfg, prompt, max_tokens=400):
            calls.append(prompt)
            if len(calls) > 1:          # the first batch lands, the second never returns
                raise KeyboardInterrupt("the run is stopped part-way")
            return "[" + ",".join('{"i": %d, %s, "why": "ok"}' % (i + 1, rated) for i in range(4)) + "]"

        orig = jm.llm._call
        jm.llm._call = flaky
        try:
            with self.assertRaises(KeyboardInterrupt):
                judge_pass(p, log=lambda _m: None)
        finally:
            jm.llm._call = orig
        # some ratings were banked before the interruption, and they are complete ratings
        banked = jm.load(p.data_dir)
        self.assertTrue(banked, "an interrupted judge pass banked nothing")
        one = next(iter(banked.values()))
        self.assertEqual(sorted(one["v"]), sorted(jm.ALL_DIMS))
        # and a second run asks only for what is still missing
        left = judge_candidates(p.store.jobs["jobs"], p.store.state["jobs"], banked,
                                search_cfg=p.search)
        self.assertNotIn(next(iter(banked)), {r["id"] for r, _d in left})

    def test_stored_postings_keep_up_with_a_band_rename(self):
        """A stored posting is only re-banded when the fetch that produced it comes round again. A
        board that fails, or a posting that has closed, would otherwise keep a name nothing
        understands -- it would never match the Quarterly tab and would sit in New forever."""
        from jobhunt.pipeline import migrate_bands
        jobs = {"a": {"location_fit": "in_radius", "variants": [{"location_fit": "regional"}]},
                "b": {"location_fit": "commutable_onsite", "variants": []},
                "c": {"location_fit": "remote", "variants": []},
                "d": {"location_fit": "local"}}
        self.assertEqual(migrate_bands(jobs), 3)          # c is already current
        self.assertEqual(jobs["a"]["location_fit"], "quarterly")
        self.assertEqual(jobs["a"]["variants"][0]["location_fit"], "quarterly")
        self.assertEqual(jobs["b"]["location_fit"], "monthly")
        self.assertEqual(jobs["c"]["location_fit"], "remote")
        self.assertEqual(jobs["d"]["location_fit"], "daily")
        self.assertEqual(migrate_bands(jobs), 0)          # idempotent

    def test_a_scan_does_not_clobber_a_decision_made_while_it_runs(self):
        """A scan can run for an hour. Saving a posting in the dashboard meanwhile must survive it."""
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan
        scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
        engineer = self.profiles["engineer"].store
        jid = next(iter(engineer.jobs["jobs"]))
        # another process (the dashboard) records a decision after this one loaded its copy
        other = Store(self.tmp / "profiles" / "engineer" / "data")
        other.set_state(jid, status="saved")
        scan(self.tmp, self.profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
        self.assertEqual(engineer.get_state(jid)["status"], "saved")


# ----------------------------------------------------------------------------- golden output

GOLDEN = FIX / "golden_scores.json"
# Everything the scoring pipeline derives, frozen. The refactor moves code between modules; this is
# what proves the moves changed nothing. Regenerate deliberately (JOBHUNT_WRITE_GOLDEN=1) and read
# the diff, never to make a red test go green.
GOLDEN_FIELDS = ("relevance", "grade", "title_tier", "location_fit", "location_miles", "seniority",
                 "active", "duplicate_of", "drop_reason")
FROZEN_NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenClock(datetime):
    """`recency_of` ages a posting against the wall clock, so the fixture drifts from fresh to mid to
    stale as real weeks pass. Freeze it, or the golden file rots on its own."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


class ModuleLayoutTest(unittest.TestCase):
    """The refactor's structural promises, asserted rather than hoped for."""

    def test_core_facade_still_exports_everything_it_used_to(self):
        """core.py is a compatibility shim. Shrinking it silently breaks callers that have not moved."""
        from jobhunt import core
        expected = {
            "DONE_REASONS", "LEGACY_STATUS", "RAW_FILE", "STATUSES", "Store", "TABS",
            "_read_json", "_write_json", "collapse_variants", "fetch_all", "judge_pass",
            "load_raw", "load_toml", "merge_into_store", "now_iso", "profile_facts",
            "relearn", "rescore", "retire_excluded_companies", "save_raw", "scan", "union_companies",
        }
        self.assertEqual(set(core.__all__), expected)
        for name in expected:
            self.assertTrue(hasattr(core, name), f"core.{name} is advertised but missing")

    def test_example_profiles_never_scan(self):
        """They document the file format. A scan must not fetch a board or spend a call on them."""
        from jobhunt.pipeline import union_companies
        from jobhunt.profiles import is_example, list_profile_names, load_all
        tmp = Path(tempfile.mkdtemp())
        try:
            make_root(tmp)
            # a demo profile that is enabled in every way, and still must not scan
            d = tmp / "profiles" / "example-demo"
            (d / "notes").mkdir(parents=True)
            (d / "search.toml").write_text(_toml_dump({**ENGINEER_SEARCH, "enabled": True}), encoding="utf-8")
            (d / "notes" / "about.md").write_text("# About\n\n- [2] demo\n", encoding="utf-8")
            _shared, active = load_all(tmp)
            self.assertNotIn("example-demo", active)
            self.assertNotIn("example-demo", list_profile_names(tmp))
            self.assertIn("example-demo", list_profile_names(tmp, include_examples=True))
            self.assertTrue(is_example("example-demo"))
            self.assertFalse(is_example("engineer"))
            # and it cannot change what a scan would fetch
            boards = union_companies(active)
            (d / "search.toml").write_text(
                _toml_dump({**ENGINEER_SEARCH, "enabled": True,
                            "companies": [{"name": "Sneaky", "ats": "greenhouse", "board": "sneaky"}]}),
                encoding="utf-8")
            _s2, again = load_all(tmp)
            self.assertEqual(len(union_companies(again)), len(boards))
            self.assertNotIn("Sneaky", [c.get("name") for c in union_companies(again)])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_import_cycles(self):
        """Each module may only reach modules below it. Without this the layering decays quietly."""
        import subprocess
        layers = {"jsonfile": 0, "llm": 0, "geo": 0, "fetch.sources": 1, "classify.learn": 1,
                  "apply.answers": 1, "apply.terms": 1, "apply.tailor": 2, "apply.runs": 1, "apply.schema": 1, "apply.issues": 1, "apply.browser": 1,
                  "apply.answer": 2, "apply.persona": 2, "apply.read": 2, "apply.heal": 3, "apply.replay": 3, "apply.resolve": 3, "apply.navigate": 3, "apply.pages": 3, "apply.vault": 1, "apply.widgets": 2, "apply.verify": 2, "apply.stats": 2,
                  "classify.filters": 2, "classify.facts": 2,
                  "report.about": 2, "store": 3, "report.views": 3,
                  "profiles": 4, "pipeline": 5, "apply.forms": 6, "reset": 6, "report.server": 7}
        for mod, level in sorted(layers.items(), key=lambda kv: kv[1]):
            code = (f"import importlib, sys; importlib.import_module('jobhunt.{mod}'); "
                    "print(' '.join(sorted(m[len('jobhunt.'):] for m in sys.modules "
                    "if m.startswith('jobhunt.'))))")
            got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 cwd=str(ROOT))
            self.assertEqual(got.returncode, 0, f"jobhunt.{mod} failed to import: {got.stderr}")
            pulled = [m for m in got.stdout.split() if m in layers and m != mod]
            higher = [m for m in pulled if layers[m] > level]
            self.assertEqual(higher, [], f"jobhunt.{mod} (layer {level}) pulls in {higher}, above it")


class AuditTest(unittest.TestCase):
    """The trail from a fact to a score. Not uniform on purpose: the pathways differ."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_a_change_is_recorded_once_and_a_repeat_is_not(self):
        from jobhunt import audit
        self.assertIsNotNone(audit.note_change(self.dir, "weights_changed", "sha", "aaa", weights={"x": 1}))
        self.assertIsNone(audit.note_change(self.dir, "weights_changed", "sha", "aaa", weights={"x": 1}))
        rec = audit.note_change(self.dir, "weights_changed", "sha", "bbb", weights={"x": 2})
        self.assertEqual(rec["was"], "aaa")
        self.assertEqual([r["sha"] for r in audit.read(self.dir, "weights_changed")], ["aaa", "bbb"])
        self.assertEqual(audit.last(self.dir, "weights_changed")["sha"], "bbb")
        self.assertIsNone(audit.last(self.dir, "judged"))

    def test_a_corrupt_line_is_skipped_rather_than_fatal(self):
        from jobhunt import audit
        audit.append(self.dir, "scan", fetched=1)
        with audit.path_for(self.dir).open("a", encoding="utf-8") as f:
            f.write("{not json\n\n[]\n")
        audit.append(self.dir, "scan", fetched=2)
        self.assertEqual([r["fetched"] for r in audit.read(self.dir, "scan")], [1, 2])

    def test_a_pathological_entry_cannot_make_the_log_unreadable(self):
        from jobhunt import audit
        audit.append(self.dir, "facts_changed", sha="x", added=[f"fact {i} " + "y" * 200 for i in range(500)])
        got = audit.read(self.dir, "facts_changed")
        self.assertEqual(len(got), 1)
        self.assertLessEqual(len(audit.path_for(self.dir).read_text()), audit.MAX_LINE + 2)

    def test_a_scan_records_facts_weights_and_the_run(self):
        from jobhunt import audit
        from jobhunt.profiles import load_env
        from jobhunt.pipeline import scan
        sources.FETCHER = fake_fetcher
        tmp = make_root(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(lambda: setattr(sources, "FETCHER", None))
        _shared, profiles = load_all(tmp)
        scan(tmp, profiles, log=lambda _m: None, env=load_env(tmp), judge=False)
        d = profiles["engineer"].data_dir
        self.assertEqual({r["kind"] for r in audit.read(d)}, {"weights_changed", "scan"})
        run = audit.last(d, "scan")
        self.assertGreater(run["fetched"], 0)
        self.assertIn("gate", run["stages"])
        # a second scan changes nothing about the model, so nothing new is claimed
        scan(tmp, profiles, log=lambda _m: None, env=load_env(tmp), judge=False)
        self.assertEqual(len(audit.read(d, "weights_changed")), 1)
        self.assertEqual(len(audit.read(d, "scan")), 2)
        # editing a fact is recorded with what moved, and by comparison, not by hooking the writer
        from jobhunt import pipeline
        pipeline._note_facts(profiles["engineer"], pipeline.profile_facts(profiles["engineer"]))   # baseline
        first = audit.last(d, "facts_changed")
        self.assertIn("notes for engineer", first["added"]); self.assertEqual(first["removed"], [])
        facts.write_facts(profiles["engineer"].dir, [facts.Fact("I want defense work.")])
        pipeline._note_facts(profiles["engineer"], pipeline.profile_facts(profiles["engineer"]))
        got = audit.last(d, "facts_changed")
        self.assertEqual(got["added"], ["I want defense work."])
        self.assertIn("notes for engineer", got["removed"])

    def test_methodology_is_written_down(self):
        """AGENTS.md is the contract. A rule that lives only in someone's head is not a rule."""
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for claim in ("Gates decide existence", "zero means absent", "counts on its own", "w_eff",
                      "has no units", "What invalidates a rating", "facts_sha", "audit.jsonl",
                      "One gate fires on a model reading", "remote_or_daily", "There is no age gate",
                      "This repository is public: no personal data"):
            self.assertIn(claim, text, claim)


class PublicRepoTest(unittest.TestCase):
    """The repository is public (AGENTS.md). Real profiles are gitignored, and nothing in them may
    leak into a tracked file by way of a test value, a fixture or a comment."""

    def test_boards_are_tagged_by_role_not_by_person(self):
        """`profiles = ["engineer"]` in the shared config reaches a profile named anything, once its
        search.toml lists "engineer" in board_tags, so config.toml never has to name a real person."""
        tmp = make_root(Path(tempfile.mkdtemp())); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        d = tmp / "profiles" / "pat"
        shutil.copytree(tmp / "profiles" / "engineer", d)
        (d / "search.toml").write_text(_toml_dump({**ENGINEER_SEARCH, "enabled": True, "board_tags": ["Engineer"]}),
                                       encoding="utf-8")
        _shared, profiles = load_all(tmp)
        names = lambda p: {c["name"] for c in p.companies()}   # noqa: E731
        self.assertIn("Ex Lever", names(profiles["pat"]))       # tagged "engineer", reached by the alias
        self.assertNotIn("USAJOBS", names(profiles["pat"]))     # tagged "analyst"
        self.assertEqual(profiles["pat"].board_tag, "engineer")
        self.assertEqual(profiles["analyst"].board_tag, "analyst")   # no board_tags: the folder name

    # public census data: every place name in the country, real people's towns included
    EXEMPT = {"jobhunt/data/us_places.csv"}

    @staticmethod
    def _identifying(name: str, prof: dict) -> dict[str, str]:
        """{value: which field it came from}. The label is what a failure prints, never the value."""
        ident, links = prof.get("identity") or {}, prof.get("links") or {}
        out = {}
        first, last = (ident.get("first_name") or "").strip(), (ident.get("last_name") or "").strip()
        if first and last:
            for sep in (" ", "_", ".", "-"):
                out[f"{first}{sep}{last}"] = f"{name} full name"
        for k in ("email", "postal_code", "address_line1", "city", "current_company"):
            out[str(ident.get(k) or "").strip()] = f"{name} identity.{k}"
        for k, v in links.items():
            if str(v).strip().lower() not in ("", "none"):
                out[str(v).strip()] = f"{name} links.{k}"
        resume = Path(((prof.get("documents") or {}).get("resume") or "").strip()).name
        if first and last and (first.lower() in resume.lower() or last.lower() in resume.lower()):
            out[resume] = f"{name} documents.resume"      # "resume.pdf" names nobody
        return {v: label for v, label in out.items() if len(v) >= 4}

    def test_no_personal_data_is_tracked(self):
        """Exact values only: the contact details, home town, employer, links and resume file name of
        every real profile on this machine. Skips on a fresh clone, which has no real profiles."""
        import subprocess
        real = [d for d in sorted((ROOT / "profiles").iterdir())
                if d.is_dir() and not d.name.startswith("example-") and (d / "profile.toml").exists()]
        if not real:
            raise unittest.SkipTest("no real profiles on this machine")
        try:
            tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            raise unittest.SkipTest("not a git checkout")
        values, phones = {}, {}
        for d in real:
            prof = load_toml(d / "profile.toml")
            values.update(self._identifying(d.name, prof))
            digits = "".join(c for c in str((prof.get("identity") or {}).get("phone") or "") if c.isdigit())
            if len(digits) >= 7:
                phones[digits[-10:]] = f"{d.name} identity.phone"
        found = []
        for rel in tracked.decode().split("\0"):
            path = ROOT / rel
            if not rel or rel in self.EXEMPT or not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore")
            low, digits = text.lower(), "".join(c for c in text if c.isdigit())
            found += [f"{rel}: {label}" for v, label in values.items() if v.lower() in low]
            found += [f"{rel}: {label}" for ph, label in phones.items() if ph in digits]
        self.assertEqual(found, [], "personal data in tracked files (see AGENTS.md)")


class GoldenScoresTest(unittest.TestCase):
    """A scan over the fixture boards must produce byte-identical scores until scoring changes."""

    def setUp(self):
        sources.FETCHER = fake_fetcher
        self.tmp = make_root(Path(tempfile.mkdtemp()))

    def tearDown(self):
        sources.FETCHER = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snapshot(self) -> dict:
        from jobhunt import core
        from jobhunt.classify import filters
        from jobhunt.profiles import load_env
        frozen_iso = FROZEN_NOW.replace(microsecond=0).isoformat()
        real_dt, real_now = filters.datetime, core.now_iso
        filters.datetime, core.now_iso = _FrozenClock, (lambda: frozen_iso)
        try:
            _shared, profiles = load_all(self.tmp)
            scan(self.tmp, profiles, log=lambda _m: None, env=load_env(self.tmp), judge=False)
            # The judge stage needs a model, so stand in for it with ratings derived from the
            # posting id. Arbitrary, but fixed and spread across the whole -10..+10 range, which is
            # what makes the relevance arithmetic and the grade curve testable at all.
            for p in profiles.values():
                p.store.refresh()
                cache = {}
                for jid, rec in p.store.jobs["jobs"].items():
                    seed = int(hashlib.sha1(jid.encode()).hexdigest()[:8], 16)
                    cache[jid] = {"v": {d: ((seed >> (3 * i)) % 21) - 10
                                        for i, d in enumerate(judgemod.ALL_DIMS)},
                                  "rev": dict(judgemod.DIM_REVS), "why": "",
                                  "text_sha": judgemod.text_sha(rec)}
                judgemod.save(p.data_dir, cache)
                pipeline.score_relevance(p)
                p.store.save_jobs()
            out = {}
            for name, p in sorted(profiles.items()):
                p.store.refresh()
                out[name] = {jid: {k: rec.get(k) for k in GOLDEN_FIELDS}
                             for jid, rec in sorted(p.store.jobs["jobs"].items())}
                for jid, rec in sorted(p.store.jobs["jobs"].items()):
                    out[name][jid]["recency"] = (rec.get("recency") or {}).get("level")
            return out
        finally:
            filters.datetime, core.now_iso = real_dt, real_now

    def test_scores_match_the_golden_file(self):
        got = self._snapshot()
        text = json.dumps(got, indent=1, sort_keys=True) + "\n"
        if os.environ.get("JOBHUNT_WRITE_GOLDEN") or not GOLDEN.exists():
            GOLDEN.write_text(text, encoding="utf-8")
            self.skipTest(f"wrote {GOLDEN.name} ({sum(len(v) for v in got.values())} postings) — review the diff")
        self.assertEqual(text, GOLDEN.read_text(encoding="utf-8"),
                         "scoring output moved. If that was intended, rerun with JOBHUNT_WRITE_GOLDEN=1 "
                         "and review the diff; if not, the refactor was not behaviour-preserving.")

    def test_snapshot_is_deterministic(self):
        self.assertEqual(self._snapshot(), self._snapshot())


class ScanAndStoreTest(unittest.TestCase):
    def setUp(self):
        sources.FETCHER = fake_fetcher
        self.tmp = make_root(Path(tempfile.mkdtemp()))
        self.shared, self.profiles = load_all(self.tmp)

    def tearDown(self):
        sources.FETCHER = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self, log=None):
        from jobhunt.profiles import load_env
        return scan(self.tmp, self.profiles, log=(log or []).append, env=load_env(self.tmp), judge=False)

    def test_scan_is_per_profile(self):
        log = []
        res = self._scan(log)
        engineer, analyst = self.profiles["engineer"].store, self.profiles["analyst"].store
        ids = set(engineer.jobs["jobs"])
        self.assertIn("greenhouse:ex:1", ids); self.assertIn("greenhouse:ex:5", ids)
        self.assertIn("lever:ex:abc", ids); self.assertIn("ashby:ex:u1", ids); self.assertIn("ashby:ex:u3", ids)
        self.assertIn("hn:111:222", ids)
        self.assertNotIn("greenhouse:ex:3", ids)   # London
        self.assertNotIn("greenhouse:ex:4", ids)   # Account Executive
        self.assertNotIn("ashby:ex:u2", ids)       # plain SWE title
        self.assertNotIn("greenhouse:ex:6", ids)   # data scientist: not Alex's title vocabulary
        self.assertNotIn("usajobs:800001", ids)    # USAJOBS is Sam-only
        jids = set(analyst.jobs["jobs"])
        self.assertIn("greenhouse:ex:6", jids); self.assertIn("usajobs:800001", jids)
        self.assertNotIn("lever:ex:abc", jids)     # Lever board is Alex-only
        self.assertNotIn("hn:111:222", jids)       # HN off for Sam
        self.assertEqual(res["profiles"]["engineer"]["new"], len(ids))
        self.assertIn("Broken", res["errors"])
        self.assertTrue((self.tmp / "data" / "postings.json").exists())
        one, seven = engineer.jobs["jobs"]["greenhouse:ex:1"], engineer.jobs["jobs"]["greenhouse:ex:7"]
        self.assertEqual(seven["duplicate_of"], "greenhouse:ex:1")
        self.assertEqual([v["id"] for v in one["variants"]], ["greenhouse:ex:7"])
        shown = {r["id"] for r in engineer.merged()}
        self.assertIn("greenhouse:ex:1", shown); self.assertNotIn("greenhouse:ex:7", shown)
        raw = json.loads((self.tmp / "data" / "postings.json").read_text())["jobs"]
        raw_ids = {r["id"] for r in raw}
        # the cache is wide: a title no profile wants is still cached, so widening the vocabulary
        # later is a rescore rather than a fresh network scan. It stays out of the profile's store.
        self.assertIn("greenhouse:ex:4", raw_ids)
        self.assertNotIn("greenhouse:ex:4", set(engineer.jobs["jobs"]))
        self.assertNotIn("greenhouse:ex:3", raw_ids)     # London: plainly non-US, never cached
        self.assertNotIn("facts", one)     # the regex fact scorer is gone; facts reach the model, not the record
        # acting on the hidden variant makes it the group's row; the group stays put on the next scan
        engineer.set_state("greenhouse:ex:7", status="skip")
        self._scan()
        one, seven = engineer.jobs["jobs"]["greenhouse:ex:1"], engineer.jobs["jobs"]["greenhouse:ex:7"]
        self.assertIsNone(seven["duplicate_of"]); self.assertEqual(one["duplicate_of"], "greenhouse:ex:7")
        shown = {r["id"]: r["state"]["status"] for r in engineer.merged(include_inactive=True) if not r.get("duplicate_of")}
        self.assertEqual(shown.get("greenhouse:ex:7"), "skip"); self.assertNotIn("greenhouse:ex:1", shown)
        self.assertNotIn("greenhouse:ex:1", {r["id"] for r in engineer.merged()})   # sibling does not resurface as New
        engineer.undo("greenhouse:ex:7")
        # second scan: nothing new, states preserved
        engineer.set_state("greenhouse:ex:1", status="saved", notes="recruiter: Sam")
        res2 = self._scan(log)
        self.assertEqual(res2["profiles"]["engineer"]["new"], 0)
        self.assertEqual(engineer.get("greenhouse:ex:1")["state"]["status"], "saved")
        first = GH_FEED["jobs"].pop(0)
        try:
            self._scan(); self._scan()
            self.assertFalse(engineer.jobs["jobs"]["greenhouse:ex:1"]["active"])
            self.assertIsNotNone(engineer.get("greenhouse:ex:1"))  # still visible: it's saved
        finally:
            GH_FEED["jobs"].insert(0, first)

    def test_editing_a_fact_marks_every_rating_stale(self):
        """The About page is the input surface, so a scan has to notice when it changes.

        Facts reach a score by being part of what the model reads before it rates a posting, so a
        changed fact does not re-score anything by arithmetic -- it invalidates the readings. The
        pass that can act on it is the judge pass, and this is what tells it to."""
        from jobhunt import pipeline
        self._scan()
        prof = self.profiles["engineer"]
        sha_before = facts.facts_sha(pipeline.profile_facts(prof))
        cache = {jid: {"v": {d: 5 for d in judgemod.ALL_DIMS}, "rev": dict(judgemod.DIM_REVS),
                       "text_sha": judgemod.text_sha(rec), "facts_sha": sha_before}
                 for jid, rec in prof.store.jobs["jobs"].items()}
        judgemod.save(prof.data_dir, cache)
        self.assertEqual(pipeline.judge_candidates(prof.store.jobs["jobs"], prof.store.state["jobs"],
                                                   cache, search_cfg=prof.search,
                                                   facts_sha=sha_before), [])
        facts.write_facts(prof.dir, [facts.Fact("I want to work in defense technology.")])
        sha_after = facts.facts_sha(pipeline.profile_facts(prof))
        self.assertNotEqual(sha_after, sha_before)
        todo = pipeline.judge_candidates(prof.store.jobs["jobs"], prof.store.state["jobs"], cache,
                                         search_cfg=prof.search, facts_sha=sha_after)
        self.assertTrue(todo)
        for _rec, dims in todo:                 # everything, not one slot: the whole reading is stale
            self.assertEqual(dims, list(judgemod.ALL_DIMS))
        # a rating made before facts_sha existed is not re-read just for lacking one
        old = {jid: {k: v for k, v in j.items() if k != "facts_sha"} for jid, j in cache.items()}
        self.assertEqual(pipeline.judge_candidates(prof.store.jobs["jobs"], prof.store.state["jobs"],
                                                   old, search_cfg=prof.search,
                                                   facts_sha=sha_after), [])

    def test_decisions_become_auto_facts_and_a_rescore_keeps_them(self):
        self._scan()
        prof = self.profiles["engineer"]; engineer = prof.store
        engineer.set_state("greenhouse:ex:1", status="skip")
        engineer.set_state("greenhouse:ex:2", status="skip")
        relearn(prof)
        got = facts.read_facts(prof.dir)
        self.assertTrue(any(f.auto and "tend to skip Ex GH postings" in f.text for f in got),
                        [f.text for f in got])
        # a search.toml change that drops a kept posting retires it on the next rescore (no two-scan wait)
        sources.FETCHER = lambda *a, **k: (_ for _ in ()).throw(sources.FetchError("offline"))
        sp = self.tmp / "profiles" / "engineer" / "search.toml"; sp_text = sp.read_text()
        self.assertIn("title_exclude = [", sp_text)
        sp.write_text(sp_text.replace("title_exclude = [", 'title_exclude = ["architect", ', 1))
        _, profiles = load_all(self.tmp); rescore(self.tmp, profiles)
        two = profiles["engineer"].store.get("greenhouse:ex:2")
        self.assertFalse(two["active"]); self.assertEqual(two["drop_reason"], "title excluded (architect)")
        self.assertNotIn("greenhouse:ex:2", {r["id"] for r in profiles["engineer"].store.merged()})
        sp.write_text(sp_text); _, profiles = load_all(self.tmp); rescore(self.tmp, profiles)
        self.assertTrue(profiles["engineer"].store.get("greenhouse:ex:2")["active"])
        with self.assertRaises(ValueError):
            engineer.set_state("ashby:ex:u1", status="skip", skip_reason="bogus")

    def test_undo_history(self):
        self._scan()
        st = self.profiles["engineer"].store
        st.set_state("greenhouse:ex:1", status="saved")
        st.set_state("greenhouse:ex:1", status="applied")
        st.set_state("greenhouse:ex:1", status="done", done_reason="no offer")
        self.assertEqual([h["status"] for h in st.get_state("greenhouse:ex:1")["history"]], ["new", "saved", "applied"])
        self.assertEqual(st.undo("greenhouse:ex:1")["status"], "applied")
        self.assertNotIn("done_reason", st.get_state("greenhouse:ex:1"))
        self.assertEqual(st.undo("greenhouse:ex:1")["status"], "saved")
        self.assertEqual(st.undo("greenhouse:ex:1")["status"], "new")
        self.assertEqual(st.undo("greenhouse:ex:1")["status"], "new")      # nothing left: stays new
        st.set_state("greenhouse:ex:1", status="skip", skip_reason="pay")
        st.set_state("greenhouse:ex:1", status="new")
        self.assertEqual(st.undo("greenhouse:ex:1")["skip_reason"], "pay")

    def test_legacy_statuses_migrate(self):
        d = self.tmp / "profiles" / "engineer" / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({"jobs": {
            "a": {"status": "shortlist"}, "b": {"status": "interviewing"}, "c": {"status": "offer"},
            "d": {"status": "rejected"}, "e": {"status": "skip", "skip_reason": "pay"}}}), encoding="utf-8")
        st = Store(d)
        got = {k: v["status"] for k, v in st.state["jobs"].items()}
        self.assertEqual(got, {"a": "saved", "b": "applied", "c": "applied", "d": "done", "e": "skip"})
        self.assertNotIn("shortlist", (d / "state.json").read_text())
        st.set_state("a", status="done", done_reason="no offer")
        self.assertEqual(st.get_state("a")["done_reason"], "no offer")
        with self.assertRaises(ValueError):
            st.set_state("a", status="done", done_reason="ghosted")
        with self.assertRaises(ValueError):
            st.set_state("a", status="shortlist")


# ----------------------------------------------------------------------------- server

class ServerTest(unittest.TestCase):
    def setUp(self):
        sources.FETCHER = fake_fetcher
        self.tmp = make_root(Path(tempfile.mkdtemp()))
        from http.server import ThreadingHTTPServer
        from jobhunt.report.server import AppState, Handler
        self.app = AppState(self.tmp, launch_runs=False)   # queue runs, never open a browser
        Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        sources.FETCHER = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def api(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"} if body is not None else {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def wait_threads(self):
        for t in list(self.app.derive_threads.values()):
            t.join(timeout=20)

    def wait_scan(self):
        for _ in range(100):
            st = self.api("/api/scan/status")
            if not st["running"]:
                return st
            time.sleep(0.1)
        self.fail("scan did not finish")

    def seed_ratings(self, name="engineer", measure=6):
        """Stand in for the judge stage, which needs a model these tests do not have.

        An unrated posting has no grade and is always below the floor, so without this the New tab
        is legitimately empty -- which is what the dashboard shows when a judge pass has not run."""
        p = self.app.profile(name)
        p.store.refresh()
        sha = facts.facts_sha(facts.read_facts(p.dir))
        cache = {jid: {"v": {d: measure for d in judgemod.ALL_DIMS}, "rev": dict(judgemod.DIM_REVS),
                       "why": "seeded", "text_sha": judgemod.text_sha(rec), "facts_sha": sha}
                 for jid, rec in p.store.jobs["jobs"].items()}
        judgemod.save(p.data_dir, cache)
        self.assertTrue(self.api("/api/rescore", {})["started"]); self.wait_scan()
        return cache

    def test_api_flow(self):
        html = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10).read().decode()
        self.assertIn("<title>jobhunt</title>", html)
        self.assertIn('id="profile"', html)
        pr = self.api("/api/profiles")
        self.assertEqual([p["name"] for p in pr["profiles"]], ["engineer", "analyst"])
        self.assertEqual(pr["default"], "engineer")
        self.assertNotIn("skip_reasons", pr)
        self.assertEqual(pr["done_reasons"], ["no offer", "withdrew"])
        self.assertGreaterEqual(pr["api_version"], 3)
        self.assertEqual(self.api("/api/jobs")["jobs"], [])
        self.assertTrue(self.api("/api/scan", {})["started"])
        self.wait_scan()
        # a scan with no model rates nothing, and an unrated posting has no grade: the New tab is
        # empty until something has read the postings
        self.assertEqual(self.api("/api/jobs?tab=new")["jobs"], [])
        self.seed_ratings("engineer"); self.seed_ratings("analyst")
        engineer = self.api("/api/jobs?tab=new")
        analyst = self.api("/api/jobs?profile=analyst&tab=new")
        self.assertTrue(engineer["jobs"] and analyst["jobs"])
        self.assertEqual(engineer["total"], len(engineer["jobs"])); self.assertEqual(engineer["counts"]["new"], engineer["total"])
        self.assertNotEqual({j["id"] for j in engineer["jobs"]}, {j["id"] for j in analyst["jobs"]})
        self.assertNotIn("description", engineer["jobs"][0]); self.assertIn("variant_count", engineer["jobs"][0])
        self.assertIn("description", self.api(f"/api/job?id={engineer['jobs'][0]['id']}"))
        totals = [j["relevance"] for j in engineer["jobs"]]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertTrue(all(j["grade"] for j in engineer["jobs"]))
        two = self.api("/api/jobs?tab=new&limit=2")
        self.assertEqual(len(two["jobs"]), 2); self.assertEqual((two["offset"], two["limit"]), (0, 2))
        page2 = self.api("/api/jobs?tab=new&limit=2&offset=2")
        self.assertEqual([j["id"] for j in page2["jobs"]], [j["id"] for j in engineer["jobs"][2:4]])
        self.assertEqual(self.api("/api/jobs?tab=new&limit=2&offset=999")["jobs"], [])
        first, third_before = two["jobs"][0]["id"], self.api("/api/jobs?tab=new&limit=3")["jobs"][2]["id"]
        r = self.api("/api/state", {"id": first, "status": "skip"})
        self.assertEqual(r["state"]["status"], "skip"); self.assertNotIn("skip_reason", r["state"])
        self.assertEqual(self.api("/api/state", {"id": first, "notes": "too far"})["state"]["notes"], "too far")   # optional comment
        two = self.api("/api/jobs?tab=new&limit=2")
        self.assertEqual(two["total"], engineer["total"] - 1); self.assertEqual(two["jobs"][1]["id"], third_before)
        self.assertEqual(self.api("/api/jobs?tab=skip")["jobs"][0]["id"], first)
        self.assertEqual(self.api("/api/jobs?profile=analyst&tab=skip")["jobs"], [])
        # undo puts it back in New
        r = self.api("/api/state/undo", {"id": first})
        self.assertEqual(r["state"]["status"], "new"); self.assertNotIn("skip_reason", r["state"])
        self.assertEqual(self.api("/api/jobs?tab=new")["total"], engineer["total"])
        q = self.api("/api/jobs?tab=new&q=forward")
        self.assertTrue(q["jobs"] and all("forward" in j["title"].lower() for j in q["jobs"]))
        jid = two["jobs"][0]["id"]
        self.api("/api/state", {"id": jid, "status": "saved"})
        self.api("/api/state", {"id": jid, "status": "applied"})
        self.assertTrue(self.api("/api/jobs?tab=applied")["jobs"][0]["state"]["applied_at"])
        r = self.api("/api/state", {"id": jid, "status": "done", "done_reason": "no offer"})
        self.assertEqual(r["state"]["done_reason"], "no offer")
        r = self.api("/api/state/undo", {"id": jid})
        self.assertEqual(r["state"]["status"], "applied")      # back to In progress, not New
        self.assertEqual(self.api("/api/jobs?tab=applied")["jobs"][0]["id"], jid)
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/jobs?tab=bogus")
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/jobs?profile=nobody")
        self.assertEqual(self.api("/api/config")["companies"], ["Broken", "Ex Ashby", "Ex GH", "Ex Lever"])
        self.assertEqual(self.api("/api/config?profile=analyst")["daily_miles"], 40)
        self.assertTrue(self.api("/api/rescore", {})["started"])
        self.assertIn("Rescoring", "\n".join(self.wait_scan()["log"]))
        # about: plain sentences, no weights, no derived checks
        ab = self.api("/api/about")
        self.assertEqual(ab["facts"][0]["text"], "notes for engineer")
        self.assertNotIn("weight", ab["facts"][0]); self.assertNotIn("points", ab)
        self.assertNotIn("check", ab["facts"][0]); self.assertNotIn("pending_checks", ab)
        was = ab["facts_sha"]
        ab = self.api("/api/about/add", {"text": "He wants defense work."})
        self.assertEqual([f["text"] for f in ab["facts"]][-1], "He wants defense work.")
        self.assertNotEqual(ab["facts_sha"], was)      # adding a fact invalidates the ratings
        ab = self.api("/api/about", {"facts": [{"text": "only this"}]})
        self.assertEqual([f["text"] for f in ab["facts"]], ["only this"])
        self.assertIn("about.md", ab["about_path"])
        import base64
        up = self.api("/api/about/upload", {"name": "n.txt", "content_b64": base64.b64encode(b"Led the Libra redesign in 2025").decode()})
        self.assertTrue(up["ok"]); self.assertEqual(up["result"]["added"], ["Led the Libra redesign in 2025 (from n.txt)"])
        self.assertEqual(up["uploads"], ["n.md"])
        # editing a fact does not silently re-score: it marks the ratings stale and says so, and
        # only a judge pass can act on it, because the change is in what the model should have read
        self.seed_ratings()
        self.assertEqual(self.api("/api/about")["stale_ratings"], 0)
        self.api("/api/about/add", {"text": "He wants to build agents."}); self.wait_threads()
        after = self.api("/api/about")
        self.assertEqual(after["stale_ratings"], after["rated"])
        self.assertGreater(after["rated"], 0)
        # judge pass: the mocked model rates the role attributes of the postings it is shown
        from jobhunt.pipeline import judge_candidates
        p_eng = self.app.profile("engineer")
        judgemod.save(p_eng.data_dir, {})     # drop the seeded ratings so the pass has work to do
        st = p_eng.store; st.refresh()
        top = judge_candidates(st.jobs["jobs"], st.state["jobs"], {},
                               search_cfg=p_eng.search)[0][0]["id"]
        rated = ", ".join(f'"{d}": {-8 if i == 0 else 7}' for i, d in enumerate(judgemod.ALL_DIMS))
        orig = llm._call
        llm._call = lambda cfg, prompt, max_tokens=400: (
            '[{"i": 1, ' + rated + ', "why": "wrong kind of role"}]')
        cfg_path = self.tmp / "config.toml"; cfg_text = cfg_path.read_text()
        try:
            cfg_path.write_text(cfg_text.replace('backend = "none"', 'backend = "claude-cli"'))   # the scan thread reloads config
            self.assertTrue(self.api("/api/judge", {})["started"]); self.wait_scan()
        finally:
            llm._call = orig
            cfg_path.write_text(cfg_text); self.app.reload()
        j = self.api(f"/api/job?id={top}")
        self.assertEqual(j["role"][judgemod.ALL_DIMS[0]], -8)
        self.assertEqual(j["role"]["overall_fit"], 7)
        self.assertEqual(j["role_why"], "wrong kind of role")
        newtab = self.api("/api/jobs?tab=new")
        self.assertEqual(newtab["min_grade"], "F")
        # a profile-level floor hides everything under it and counts what the model has not judged
        sp = self.tmp / "profiles" / "engineer" / "search.toml"; sp_text = sp.read_text()
        try:
            sp.write_text(sp_text + '\n[scoring]\nmin_grade = "A+"\n'); self.app.reload()
            floored = self.api("/api/jobs?tab=new")
            self.assertEqual(floored["jobs"], []); self.assertEqual(floored["min_grade"], "A+")
            self.assertEqual(floored["counts"]["hidden"],
                             newtab["counts"]["new"] + newtab["counts"]["hidden"])
            # "unjudged" is the part of the hidden bucket the model has not read yet, so it is the
            # hidden count less the hidden postings that carry a rating -- not less every rating on
            # file, since a posting you have already acted on is counted under its own status.
            cache = judgemod.load(self.tmp / "profiles" / "engineer" / "data")
            self.assertGreaterEqual(len(cache), 1)
            st = self.app.profile("engineer").store; st.refresh()
            rated_and_hidden = sum(
                1 for jid, rec in st.jobs["jobs"].items()
                if jid in cache and rec.get("active", True) and not rec.get("duplicate_of")
                and st.state["jobs"].get(jid, {}).get("status", "new") == "new")
            self.assertEqual(floored["counts"]["unjudged"],
                             floored["counts"]["hidden"] - rated_and_hidden)
            # everything hidden is reachable from the one tab that has a hidden bucket
            seen = len(self.api("/api/jobs?tab=new&lowfit=1&limit=0")["jobs"])
            self.assertEqual(seen, floored["counts"]["hidden"])
        finally:
            sp.write_text(sp_text); self.app.reload()
        # deleting an auto fact suppresses it
        st = self.app.profile("engineer").store
        for i in (1, 2, 5):
            st.set_state(f"greenhouse:ex:{i}", status="skip", skip_reason="company")
        self.assertTrue(self.api("/api/rescore", {})["started"]); self.wait_scan()
        ab = self.api("/api/about")
        auto = [x for x in ab["facts"] if x["auto"]]
        self.assertTrue(auto and "tend to skip Ex GH" in auto[0]["text"])
        ab = self.api("/api/about", {"facts": [x for x in ab["facts"] if not x["auto"]]})
        self.assertFalse([x for x in ab["facts"] if x["auto"]])
        self.assertIn("ex gh", facts.load_suppressed(self.tmp / "profiles" / "engineer" / "data"))

    def test_distant_onsite_roles_compete_in_one_list(self):
        """They used to get a tab of their own, because nothing knew how often a job needed you and
        a 300-mile office would have crowded the shortlist. Reachability is a gate now, so anything
        that survives is a job you could take and it ranks on merit with the rest."""
        self.assertTrue(self.api("/api/scan", {})["started"]); self.wait_scan()
        st = self.app.profile("engineer").store
        # Philadelphia is 415 miles from Columbus: inside the radius, far past a day trip
        philly = st.jobs["jobs"]["greenhouse:ex:8"]
        self.assertEqual(philly["location_fit"], "quarterly")
        self.assertTrue(400 <= philly["location_miles"] <= 430, philly["location_miles"])
        self.seed_ratings()                    # make everything visible: rate it all alike

        new_tab = self.api("/api/jobs?tab=new&limit=0")
        self.assertNotIn("quarterly", new_tab["counts"])          # the tab is gone
        self.assertIn("greenhouse:ex:8", {j["id"] for j in new_tab["jobs"]})
        self.assertEqual(new_tab["total"], new_tab["counts"]["new"])
        # the band still rides on the row, so the reader can see what kind of trip it would be
        row = next(j for j in new_tab["jobs"] if j["id"] == "greenhouse:ex:8")
        self.assertEqual(row["location_fit"], "quarterly")
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/jobs?tab=quarterly")
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/jobs?tab=nonsense")

    def test_parked_profile_is_skipped_without_losing_anything(self):
        """`enabled = false` hides a profile and drops its boards from the scan; nothing is deleted."""
        from jobhunt.profiles import disabled_names, load_all, pick_profile
        from jobhunt.core import union_companies
        sp = self.tmp / "profiles" / "analyst" / "search.toml"
        before_text = sp.read_text()
        _, both = load_all(self.tmp)
        self.assertEqual(sorted(both), ["analyst", "engineer"])
        boards_before = len(union_companies(both))
        try:
            self.assertIn("enabled = true", before_text)   # top level, before any [table] header
            sp.write_text(before_text.replace("enabled = true", "enabled = false", 1))
            _, active = load_all(self.tmp)
            self.assertEqual(list(active), ["engineer"])                    # not loaded
            self.assertEqual(disabled_names(self.tmp), ["analyst"])        # but still on disk
            _, everything = load_all(self.tmp, include_disabled=True)
            self.assertEqual(sorted(everything), ["analyst", "engineer"])
            self.assertFalse(everything["analyst"].enabled)
            self.assertLess(len(union_companies(active)), boards_before)  # its boards are not fetched
            self.assertTrue((self.tmp / "profiles" / "analyst" / "data").exists())   # data untouched
            with self.assertRaises(SystemExit) as cm:
                pick_profile(active, "analyst", {}, disabled=disabled_names(self.tmp))
            self.assertIn("parked", str(cm.exception))
        finally:
            sp.write_text(before_text)
        self.assertEqual(sorted(load_all(self.tmp)[1]), ["analyst", "engineer"])   # flipping back restores it

    def test_a_decision_records_the_weights_that_showed_it(self):
        """Otherwise a later comparison is circular: the ranking that surfaced a posting gets the
        credit for you having saved it. Recording the weights hash is what lets `retune` say how many
        of your own picks were chosen from a list sorted by different weights."""
        from jobhunt.classify import relevance as rel
        self.assertTrue(self.api("/api/scan", {})["started"]); self.wait_scan()
        self.seed_ratings()
        rows = self.api("/api/jobs?tab=new&limit=2")["jobs"]
        st = self.api("/api/state", {"id": rows[0]["id"], "status": "saved"})["state"]
        sha = rel.weights_sha(rel.load_weights(self.app.profile("engineer").search)[0])
        self.assertEqual(st["decided_under"], sha)
        self.assertIsInstance(st["decided_rank"], int)
        self.assertGreaterEqual(st["decided_of"], st["decided_rank"])
        self.assertIsNotNone(st.get("decided_relevance"))
        self.assertIsNotNone(st.get("decided_grade"))
        st2 = self.api("/api/state", {"id": rows[-1]["id"], "status": "skip",
                                      "skip_reason": "travel"})["state"]
        self.assertEqual(st2["decided_under"], sha)

    def test_scan_status_says_which_stage_without_opening_the_log(self):
        """A scan takes minutes and the log dialog was the only sign one was running, so closing it
        left the page looking idle while the server was ten minutes deep."""
        st = self.api("/api/scan/status")
        self.assertIn("progress", st)
        self.assertEqual(st["stages"][:3], ["plan", "fetch", "cache"])
        self.assertFalse(st["running"])

        from jobhunt.pipeline import Progress
        prog = Progress(lambda _m: None)
        self.app.scan_progress = prog
        prog.begin("fetch", total=160, note="160 boards")
        prog.step(40)
        snap = self.api("/api/scan/status")["progress"]
        self.assertEqual((snap["stage"], snap["done"], snap["total"]), ("fetch", 40, 160))
        prog.begin("gate")                       # beginning the next stage banks the last
        snap = self.api("/api/scan/status")["progress"]
        self.assertEqual(snap["stage"], "gate")
        self.assertEqual([f["stage"] for f in snap["finished"]], ["fetch"])
        self.assertEqual(snap["total"], 0)       # a stage with no count says so, rather than lying

    def test_relearn_queues_an_edit_that_lands_mid_pass(self):
        """An About edit made while a re-score is running used to be dropped, leaving stale scores."""
        from jobhunt.report import server as srv
        p = self.app.profile("engineer")
        calls, running, release = [], threading.Event(), threading.Event()

        def fake_relearn(profile):
            calls.append(profile.name)
            running.set()
            release.wait(timeout=10)

        orig = srv.relearn
        srv.relearn = fake_relearn
        try:
            self.app.relearn_later(p)
            self.assertTrue(running.wait(timeout=10))   # the first pass is under way
            self.app.relearn_later(p)                   # the edit lands while it runs
            release.set()
            self.app.derive_threads[f"relearn:{p.name}"].join(timeout=20)
        finally:
            srv.relearn = orig
            release.set()
        self.assertEqual(calls, ["engineer", "engineer"])       # the second edit still reaches the scores

    def test_resume_routes(self):
        import urllib.error
        t = self.api("/api/resume/terms")
        self.assertEqual((t["counts"], t["has_master"], t["entries"]), ({"approved": 0, "pending": 0}, False, []))
        r = self.api("/api/resume/terms/add", {"kind": "skill", "name": "Python", "aliases": "python3"})
        self.assertEqual(r["counts"], {"approved": 1, "pending": 0})
        key = r["entries"][0]["key"]
        self.assertEqual(r["entries"][0]["aliases"], ["python3"])
        r = self.api("/api/resume/terms/add", {"kind": "synonyms", "name": "agentic system | AI agent"})
        self.assertEqual([e["name"] for e in r["entries"] if e["kind"] == "synonyms"], ["agentic system = AI agent"])
        self.assertEqual(self.api("/api/resume/terms/set", {"key": key, "status": "pending"})["counts"], {"approved": 1, "pending": 1})
        self.assertEqual(self.api("/api/resume/terms/delete", {"key": key})["counts"], {"approved": 1, "pending": 0})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api("/api/resume/file?name=nope.pdf")
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/resume/terms/set", {"key": "nope", "status": "approved"})

    def test_runs_and_answers_api(self):
        self.assertTrue(self.api("/api/scan", {})["started"]); self.wait_scan()
        self.seed_ratings()
        jid = self.api("/api/jobs?tab=new&limit=1")["jobs"][0]["id"]
        self.assertIn("no resume", self.api("/api/apply", {"id": jid}).get("error", ""))
        (self.tmp / "profiles" / "engineer" / PROFILE["documents"]["resume"]).write_bytes(b"%PDF-1.4\n")
        self.app.reload()
        self.assertIn("unknown job", self.api("/api/apply", {"id": "nope"}).get("error", ""))
        r = self.api("/api/apply", {"id": jid})
        self.assertTrue(r["ok"]); self.assertEqual(r["state"], "queued")
        self.assertIn("already open", self.api("/api/apply", {"id": jid}).get("error", ""))
        runs = self.api("/api/runs")
        self.assertEqual([x["id"] for x in runs["runs"]], [r["run_id"]]); self.assertEqual(runs["counts"]["open"], 1)
        self.assertEqual(runs["max_open"], 6)
        one = self.api("/api/run?id=" + r["run_id"])
        self.assertEqual(one["state"], "queued"); self.assertEqual(one["job_id"], jid); self.assertIn("run.json", one["files"])
        self.assertEqual(self.app.pump_once(), 0)                      # tests never launch a browser
        st = self.api("/api/apply/status?id=" + jid)
        self.assertTrue(st["running"]); self.assertEqual(st["run"]["state"], "queued")
        # queue every saved posting: none saved yet -> nothing; save two -> one new (the other has an open run)
        self.assertEqual(self.api("/api/apply/all", {"status": "saved"})["queued"], [])
        other = self.api("/api/jobs?tab=new&limit=2")["jobs"][1]["id"]
        self.api("/api/state", {"id": jid, "status": "saved"}); self.api("/api/state", {"id": other, "status": "saved"})
        qa = self.api("/api/apply/all", {"status": "saved"})
        self.assertEqual((qa["queued"], qa["skipped"]), ([other], [jid]))
        # control: resume needs a live window; abandon on a queued run; mark submitted moves the posting on
        self.assertIn("cannot resume", self.api("/api/run/control", {"id": r["run_id"], "action": "resume"}).get("error", ""))
        oid = [x for x in self.api("/api/runs")["runs"] if x["job_id"] == other][0]["id"]
        self.assertEqual(self.api("/api/run/control", {"id": oid, "action": "abandon"})["run"]["state"], "abandoned")
        self.assertIn("already abandoned", self.api("/api/run/control", {"id": oid, "action": "abandon"}).get("error", ""))
        self.assertIn("unknown action", self.api("/api/run/control", {"id": r["run_id"], "action": "x"}).get("error", ""))
        self.assertIn("unknown run", self.api("/api/run/control", {"id": "nope", "action": "abandon"}).get("error", ""))
        # issues: a response is validated against the run's issues and recorded for the next resume
        from jobhunt.apply import issues as issuesmod
        rdir = runsmod.load_run(self.app.profiles[next(iter(self.app.profiles))].data_dir, r["run_id"]).path
        issuesmod.write_issues(rdir, [issuesmod.Issue(id="k1", kind="question", label="Why us?", key="k1")])
        self.assertIn("no issue", self.api("/api/run/respond", {"id": r["run_id"], "issue_id": "zz", "action": "skip"}).get("error", ""))
        self.assertIn("needs a value", self.api("/api/run/respond", {"id": r["run_id"], "issue_id": "k1", "action": "answer"}).get("error", ""))
        self.assertEqual(self.api("/api/run/respond", {"id": r["run_id"], "issue_id": "k1", "action": "skip"}),
                         {"ok": True, "resumed": False})
        self.assertEqual([x["action"] for x in issuesmod.take_responses(rdir)], ["skip"])
        # answers from the dashboard: validated against the run's open questions, saved to the bank as yours
        pdir = self.app.profiles[next(iter(self.app.profiles))].dir
        bank = bankmod.Bank(pdir)
        kq = bank.record("Undergraduate GPA", "text", [], True, {}).key
        kw = bank.record("Which ones?", "text", [], True, {}).key
        bank.save()
        run = runsmod.load_run(self.app.profiles[next(iter(self.app.profiles))].data_dir, r["run_id"])
        run.data["questions"] = [{"label": "Undergraduate GPA *", "key": kq, "answered": False},
                                 {"label": "Which ones?", "key": kw, "answered": False}]
        run.data["window_edits"] = {"changed": [], "filled_by_you": [{"label": "Which ones?", "value": "Python, Go", "ident": "x"}]}
        run.save()
        self.assertIn("no open question", self.api("/api/run/answers", {"id": r["run_id"], "answers": [{"key": "zz", "value": "1"}]}).get("error", ""))
        self.assertIn("blank", self.api("/api/run/answers", {"id": r["run_id"], "answers": [{"key": kq, "value": " "}]}).get("error", ""))
        got = self.api("/api/run/answers", {"id": r["run_id"], "answers": [{"key": kq, "value": "3.9"}]})
        self.assertEqual((got["ok"], got["saved"]), (True, 1))
        self.assertEqual((bankmod.Bank(pdir).get(kq).answer, bankmod.Bank(pdir).get(kq).status), ("3.9", "approved"))
        # Mark submitted learns what you typed in the window for a question the tool asked
        self.api("/api/run/control", {"id": r["run_id"], "action": "submitted"})
        self.assertEqual((bankmod.Bank(pdir).get(kw).answer, bankmod.Bank(pdir).get(kw).status), ("Python, Go", "approved"))
        cleanup = bankmod.Bank(pdir); cleanup.remove(kq); cleanup.remove(kw); cleanup.save()
        done = self.api("/api/run/control", {"id": r["run_id"], "action": "submitted"})
        self.assertEqual(done["run"]["state"], "submitted")
        self.assertEqual(self.api("/api/job?id=" + jid)["state"]["status"], "applied")
        self.assertEqual(self.api("/api/runs")["runs"], [])
        self.assertEqual(self.api("/api/runs?state=all")["counts"]["submitted"], 1)
        # a retry queues a fresh run for the same posting
        self.assertTrue(self.api("/api/run/control", {"id": r["run_id"], "action": "retry"})["ok"])
        self.assertEqual(self.api("/api/runs")["counts"]["queued"], 1)
        # a run whose process died while waiting is surfaced, not hidden
        dead = runsmod.new_run(self.tmp / "profiles" / "engineer" / "data", {"id": jid, "company": "Ex", "title": "T"}, "engineer")
        dead.set_state("running", pid=4000000000); dead.pause("review_and_submit")
        got = [x for x in self.api("/api/runs")["runs"] if x["id"] == dead.id][0]
        self.assertEqual(got["reason"], "window_closed"); self.assertIn("was it submitted", got["reason_text"])
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/run/file?id=" + dead.id + "&name=run.json")     # only screenshots are served
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/run?id=nope")
        # the answer bank over the API
        self.assertEqual(self.api("/api/answers")["entries"], [])
        a = self.api("/api/answers/set", {"question": "Why {company}?", "answer": "Because.", "status": "approved"})
        self.assertEqual(a["counts"], {"approved": 1, "pending": 0}); key = a["entries"][0]["key"]
        self.assertIn("answers.md", a["answers_path"])
        a = self.api("/api/answers/set", {"key": key, "answer": "Because {company} builds.", "status": "pending"})
        self.assertEqual(a["entries"][0]["status"], "pending"); self.assertFalse(a["entries"][0]["approved"])
        self.assertIn("Because {company} builds.", (self.tmp / "profiles" / "engineer" / "notes" / "answers.md").read_text())
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/answers/set", {"key": "nope", "answer": "x"})
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/answers/set", {"key": key, "status": "maybe"})
        self.assertEqual(self.api("/api/answers/delete", {"key": key})["entries"], [])
        with self.assertRaises(urllib.error.HTTPError):
            self.api("/api/answers/delete", {"key": key})
        # reset: runs archived, notes and answers byte-identical
        notes = {f.name: f.read_bytes() for f in (self.app.profiles[next(iter(self.app.profiles))].dir / "notes").iterdir() if f.is_file()}
        got = self.api("/api/reset", {})
        self.assertTrue(got["ok"]); self.assertGreaterEqual(got["moved"], 1)
        self.assertEqual(self.api("/api/runs")["runs"], [])
        self.assertEqual({f.name: f.read_bytes() for f in (self.app.profiles[next(iter(self.app.profiles))].dir / "notes").iterdir() if f.is_file()}, notes)
        self.assertEqual(self.api("/api/jobs?tab=saved")["counts"].get("saved", 0), 0)


# ----------------------------------------------------------------------------- answer bank + runs

class AnswerBankTest(unittest.TestCase):
    def test_parse_render_round_trip(self):
        e1 = bankmod.Entry("Why do you want to work at {company}?", status="approved", kind="textarea",
                           answer="Because {company} builds things.\nSecond line.", seen=3, last_job="gh:x:1", last_seen="2026-09-17")
        e2 = bankmod.Entry("Which office?", kind="select", options=["Columbus", "Dayton | Metro"], required=True)
        md = bankmod.render_bank([e1, e2])
        back = bankmod.parse_bank(md)
        self.assertEqual([e.question for e in back], [e1.question, e2.question])
        self.assertEqual(back[0].answer, e1.answer); self.assertEqual(back[0].status, "approved"); self.assertEqual(back[0].seen, 3)
        self.assertEqual(back[0].last_job, "gh:x:1"); self.assertEqual(back[0].last_seen, "2026-09-17")
        self.assertEqual(back[1].options, ["Columbus", "Dayton", "Metro"]); self.assertTrue(back[1].required)
        self.assertEqual(back[1].status, "pending"); self.assertEqual(back[1].answer, "")
        self.assertTrue(back[0].approved); self.assertFalse(back[1].approved)
        self.assertEqual(bankmod.parse_bank("# nothing\n\nprose only\n"), [])

    def test_generalize_specialize_and_lookup(self):
        job = {"id": "gh:initech:1", "company": "Initech", "title": "ML Engineer"}
        self.assertEqual(bankmod.generalize("Why do you want to work at Initech? *", job), "Why do you want to work at {company}?")
        self.assertEqual(bankmod.specialize("I like {company} and the {title} role.", job), "I like Initech and the ML Engineer role.")
        bank = bankmod.Bank(None, [bankmod.Entry("Why do you want to work at {company}?", status="approved", answer="x")])
        self.assertIsNotNone(bank.find("Why do you want to work at Globex?", {"company": "Globex"}))
        self.assertIsNotNone(bank.find("why do you want to work at {company}", {}))          # case / punctuation
        self.assertIsNotNone(bank.find("Why do you want to work at {company}? ", {}))
        self.assertIsNone(bank.find("Why should we hire you?", {}))
        self.assertIsNone(bank.find("", {}))

    def test_record_bumps_seen_and_set_approves(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            bank = bankmod.Bank(tmp)
            e = bank.record("Which office? *", "select", ["Columbus", "Dayton"], True, {"id": "j1", "company": "Ex"})
            self.assertEqual((e.status, e.seen, e.required, e.options), ("pending", 1, True, ["Columbus", "Dayton"]))
            bank.record("Which office?", "select", [], False, {"id": "j2"})
            self.assertEqual(len(bank.entries), 1); self.assertEqual(bank.entries[0].seen, 2)
            self.assertEqual(bank.entries[0].last_job, "j2"); self.assertTrue(bank.entries[0].required)
            bank.save()
            self.assertTrue((tmp / "notes" / "answers.md").exists())
            again = bankmod.Bank(tmp)
            self.assertEqual(again.counts(), {"approved": 0, "pending": 1})
            self.assertIsNotNone(again.set(e.key, answer="Columbus", status="approved"))
            self.assertIsNone(again.set("nope", answer="x"))
            again.save()
            self.assertEqual(bankmod.Bank(tmp).counts(), {"approved": 1, "pending": 0})
            self.assertTrue(bankmod.Bank(tmp).remove(e.key)); self.assertFalse(bankmod.Bank(tmp).remove("nope"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RelevanceAuditTest(unittest.TestCase):
    """Every measure, every branch, and the arithmetic the dashboard shows (the relevance audit)."""

    def setUp(self):
        from jobhunt.classify import relevance as rel
        self.rel = rel
        self.w = rel.default_weights()

    def test_location_is_office_days_weighted_by_commute(self):
        """The anchors: remote best, no commute ideal, 30 min daily acceptable, more than an hour
        each way more than once a week strongly negative. 45 mph, 3 days assumed when unstated."""
        cfg = {"commute_mph": 45, "assumed_office_days": 3}
        mi = lambda minutes: minutes * 45 / 60                                       # noqa: E731
        f = lambda **r: self.rel._location(r, cfg)[0]                                # noqa: E731
        self.assertEqual(f(location_fit="remote"), 10.0)
        self.assertEqual(f(location_fit="remote_unspecified"), 6.0)
        self.assertEqual(f(location_fit="monthly"), 0.0)                             # distance unknown: no evidence
        self.assertEqual(f(location_fit="daily", location_miles=2, onsite_days=5), 9.0)   # no commute: ideal
        self.assertEqual(f(location_fit="daily", location_miles=mi(30), onsite_days=5), 4.0)  # acceptable
        self.assertEqual(f(location_fit="daily", location_miles=mi(30)), 6.4)       # 3 days assumed
        self.assertEqual(f(location_fit="monthly", location_miles=mi(60), onsite_days=1), 5.2)
        self.assertEqual(f(location_fit="monthly", location_miles=mi(60), onsite_days=3), -4.4)
        self.assertLessEqual(f(location_fit="monthly", location_miles=mi(61), onsite_days=2), -8.0)   # the red line
        self.assertGreater(f(location_fit="monthly", location_miles=mi(61), onsite_days=1), -8.0)     # once a week is not
        self.assertEqual(f(location_fit="monthly", location_miles=70), -10.0)       # 70 miles, 3 days
        self.assertEqual(f(location_fit="monthly", location_miles=100, onsite_days=1), -10.0)   # DC weekly
        # more days or a longer drive never scores better
        for days in (1, 2, 3, 4, 5):
            vals = [f(location_fit="monthly", location_miles=m, onsite_days=days) for m in range(5, 120, 5)]
            self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertIn("3 days a week (assumed) × 30 min", self.rel._location({"location_fit": "daily", "location_miles": mi(30)}, cfg)[1])

    def test_office_days_fall_back_in_order(self):
        od = lambda **r: self.rel._office_days(r, {"assumed_office_days": 3})       # noqa: E731
        self.assertEqual(od(onsite_days=2, onsite_need="daily"), (2.0, "stated"))
        self.assertEqual(od(onsite_need="daily")[0], 5.0)
        self.assertEqual(od(onsite_need="weekly")[0], 3.0)
        self.assertEqual(od(remote_signal="hybrid"), (3.0, "hybrid, days assumed"))
        self.assertEqual(od(), (3.0, "assumed"))

    def test_office_days_are_read_only_beside_office_words(self):
        from jobhunt.classify.filters import office_days_stated as f
        self.assertEqual(f("This is a hybrid role, 3 days a week in office."), 3)
        self.assertEqual(f("In-office 2x per week at our HQ"), 2)
        self.assertEqual(f("We expect you on-site four days per week."), 4)
        self.assertEqual(f("on site once a week"), 1)
        self.assertEqual(f("hybrid (2 days/week)"), 2)
        self.assertIsNone(f("Up to 25% travel. 5 days of PTO."))
        self.assertIsNone(f("Remote. We meet 2 days a week on Zoom."))
        self.assertIsNone(f("Fully remote"))

    def test_pay(self):
        cfg = {"floor": 110000, "target": 150000}
        f = lambda top: self.rel._pay({"salary": {"max": top}} if top else {}, cfg)[0]   # noqa: E731
        self.assertEqual(f(None), 0.0)
        self.assertEqual(f(110000), -10.0)
        self.assertEqual(f(150000), 10.0)
        self.assertEqual(f(130000), 0.0)
        self.assertEqual(f(400000), 10.0)
        self.assertEqual(f(90000), -10.0)
        self.assertEqual(self.rel._pay({"salary": {"max": 200000}}, {"floor": 200000, "target": 150000})[0], 0.0)

    def test_any_travel_is_a_negative(self):
        """-4 up to what you accept, -10 at 2.5x that; nothing is ever a plus, and
        silence or a stated 0% costs nothing."""
        f = lambda **t: self.rel._travel({"travel": t}, 10)[0]   # noqa: E731
        self.assertEqual(f(pct=0), 0.0)
        self.assertEqual(f(pct=5), -4.0)
        self.assertEqual(f(pct=10), -4.0)
        self.assertEqual(f(pct=25), -10.0)
        self.assertEqual(f(pct=50), -10.0)
        self.assertEqual(f(pct=15), -6.0)
        self.assertEqual(f(level="light"), -4.0)
        self.assertEqual(f(level="moderate"), -7.0)
        self.assertEqual(f(level="heavy"), -10.0)
        self.assertEqual(f(level="mentioned"), 0.0)
        self.assertEqual(self.rel._travel({}, 10)[0], 0.0)
        for pct in range(0, 60):
            self.assertLessEqual(f(pct=pct), 0.0, pct)

    def test_freshness_is_the_postings_age(self):
        """+10 under a week, 0 at two weeks, -10 at a month and beyond; no age at all is 0 whatever
        the band says. An undated posting carries the age the scan gave it from
        first_seen, so the same curve applies."""
        fr = lambda age, cfg=None: self.rel._freshness({"recency": {"age_days": age, "level": "x"}}, cfg)[0]   # noqa: E731
        self.assertEqual((fr(0), fr(6)), (10.0, 10.0))
        self.assertEqual(fr(7), 10.0)
        self.assertAlmostEqual(fr(10), 10.0 * 4 / 7, places=1)
        self.assertEqual(fr(14), 0.0)
        self.assertEqual(fr(22), -5.0)
        self.assertEqual((fr(30), fr(60), fr(400)), (-10.0, -10.0, -10.0))
        self.assertEqual(fr(7, {"peak_days": 10}), 10.0)
        self.assertEqual(fr(20, {"fresh_days": 20}), 0.0)
        self.assertEqual(self.rel._freshness({"recency": {"level": "fresh"}})[0], 0.0)
        self.assertEqual(self.rel._freshness({})[0], 0.0)
        for a in range(0, 100):
            self.assertGreaterEqual(fr(a), fr(a + 1), a)                       # never gets better with age

    def test_clearance(self):
        c = self.rel._clearance
        self.assertEqual(c({})[0], 0.0)
        self.assertEqual(c({"clearance": {"mentioned": True}})[0], 3.0)
        self.assertEqual(c({"clearance": {"mentioned": True, "active_required": True}})[0], -10.0)
        self.assertEqual(c({"clearance": {"mentioned": True, "active_required": True}}, holds_one=True)[0], 8.0)
        self.assertNotIn("east_coast", self.rel.PRACTICAL_DIMS)      # removed: under remote_or_daily only remote and the home metro survive

    def test_the_arithmetic_the_table_shows_is_the_arithmetic(self):
        practical = {"location": (8.0, ""), "pay": (-4.0, ""), "travel": (2.0, "")}
        role = {d: ((i * 7) % 21) - 10 for i, d in enumerate(self.rel.ROLE_DIMS)}
        for rw in (10, 5):
            w = {**self.w, "role_weight": rw}
            total, parts = self.rel.score(role, practical, w, overall=6.0)
            for part in parts:
                self.assertAlmostEqual(part["points"], round(part["m"] * part["w_eff"], 1), places=6)
                if part["kind"] == "role":
                    self.assertAlmostEqual(part["w_eff"], part["w"] * rw / 10)
            self.assertAlmostEqual(total, round(sum(p["points"] for p in parts), 1))

    def test_parts_are_ordered_by_importance_then_name(self):
        _t, parts = self.rel.score({d: 1 for d in self.rel.ROLE_DIMS}, self.rel.practical_measures({}, {}), self.w, overall=1)
        keys = [(-p["w_eff"], p["dim"]) for p in parts]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(parts[0]["dim"], "location")      # five share the top weight; ties fall to name

    def test_shipped_order_of_importance(self):
        """The shipped order: pay, remote, travel, ownership and the overall read on top; freshness just under;
        domain, feedback loop and seniority next; the rest low; enterprise_software at 0 (a gate)."""
        eff = {p["dim"]: p["w_eff"] for p in self.rel.score({d: 1 for d in self.rel.ROLE_DIMS},
                                                             self.rel.practical_measures({}, {}), self.w, overall=1)[1]}
        tier1 = ("pay", "location", "travel", "product_ownership", "overall_fit")
        tier2 = ("freshness",)
        tier3 = ("domain", "feedback_loop", "seniority_fit")
        top = min(eff[d] for d in tier1)
        rest = [v for d, v in eff.items() if d not in tier1 + tier2 + tier3]
        self.assertGreater(top, eff["freshness"])
        self.assertGreater(eff["freshness"], max(eff[d] for d in tier3))
        self.assertGreater(min(eff[d] for d in tier3), max(rest))
        self.assertEqual(eff["enterprise_software"], 0)
        self.assertEqual(set(eff), set(self.rel.ROLE_DIMS) | set(self.rel.PRACTICAL_DIMS) | {"overall_fit"})
        # and the shipped config.toml says the same as the defaults
        from jobhunt.classify.relevance import load_weights
        live, notes = load_weights({"scoring": load_toml(ROOT / "config.toml")["scoring"]})
        self.assertEqual(notes, [])
        self.assertEqual(live, self.w)

    def test_unjudged_has_no_relevance(self):
        for role in (None, {}):
            total, parts = self.rel.score(role, self.rel.practical_measures({}, {}), self.w)
            self.assertIsNone(total)
        self.assertFalse(self.rel.at_least(self.rel.grade(None, [1.0] * 10), "F"))

    def test_an_unrated_attribute_is_not_a_zero(self):
        p = self.rel.practical_measures({}, {})
        a, parts = self.rel.score({"pace": 5}, p, self.w)
        self.assertEqual([x["dim"] for x in parts if x["kind"] == "role"], ["pace"])
        self.assertEqual(a, 5 * self.w["role"]["pace"])

    def test_weights_config(self):
        lw = self.rel.load_weights
        w, notes = lw({"scoring": {"role": {"pace": 7.9, "nope": 3, "work_shape": 40}, "role_weight": 12}})
        self.assertEqual(w["role"]["pace"], 8)                            # rounded, not truncated
        self.assertEqual(w["role"]["work_shape"], 10)
        self.assertEqual(w["role_weight"], 10)
        self.assertEqual(w["role"]["domain"], self.w["role"]["domain"])   # untouched keys keep defaults
        self.assertEqual(len(notes), 3)                                    # unknown key, work_shape clamped, role_weight clamped

    def test_judge_parser_clamps_and_drops(self):
        items = [{"i": 1, **{d: v for d, v in zip(judgemod.ALL_DIMS, [25, -40, "x", 3.6, None])}, "why": "w"}]
        with mock.patch.object(judgemod.llm, "_call", return_value=json.dumps(items).replace("25", "Infinity", 1)):
            got = judgemod.judge({"backend": "claude-cli"}, "- facts",
                                 [{"id": "a", "title": "t", "company": "c", "description": "d"}],
                                 dims=list(judgemod.ALL_DIMS), name="Alex")
        v = got["a"]["v"]
        d = list(judgemod.ALL_DIMS)
        self.assertNotIn(d[0], v)                    # Infinity dropped, not a crash
        self.assertEqual(v[d[1]], -10)               # clamped
        self.assertNotIn(d[2], v)                    # not a number
        self.assertEqual(v[d[3]], 4)                 # rounded
        self.assertNotIn(d[4], v)                    # null: not rated

    def test_curve_is_monotonic_frozen_and_refits_on_new_weights(self):
        cuts = self.rel.fit_curve([float(x) for x in range(-50, 51)])
        self.assertEqual(cuts, sorted(cuts, reverse=True))
        self.assertEqual(self.rel.grade(cuts[0], cuts), "A+")              # inclusive at the boundary
        self.assertEqual(self.rel.grade(cuts[-1] - 0.1, cuts), "F")
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        jobs = {str(i): {"role": {"pace": (i % 21) - 10}, "active": True} for i in range(40)}
        cfg = {"scoring": {}}
        _n, c1 = self.rel.apply_to(jobs, cfg, tmp)
        jobs["new"] = {"role": {"pace": 10}, "active": True}
        _n, c2 = self.rel.apply_to(jobs, cfg, tmp)
        self.assertEqual(c1, c2)                                           # frozen while the weights hold
        _n, c3 = self.rel.apply_to(jobs, {"scoring": {"role": {"pace": 3}}}, tmp)
        self.assertNotEqual(c1, c3)                                        # refit when they change
        jobs["dup"] = {"role": {"pace": 10}, "active": True, "duplicate_of": "new"}
        self.rel.apply_to(jobs, {"scoring": {"role": {"pace": 3}}}, tmp, refit=True)
        self.assertIsNotNone(jobs["dup"]["grade"])                         # graded, but not in the curve

    def test_the_dashboard_and_explain_show_the_applied_multiplier(self):
        html = (ROOT / "jobhunt" / "report" / "dashboard.html").read_text()
        self.assertIn("p.w_eff", html)
        self.assertNotIn("x${p.w}</td>", html)
        self.assertIn("part.get('w_eff'", (ROOT / "jobhunt.py").read_text())


class FeedKeyTest(unittest.TestCase):
    def test_every_board_is_its_own_feed(self):
        """Keying only greenhouse and ashby by board collapsed every workable, breezy, recruitee,
        smartrecruiters (and new bamboohr, rippling, page) board onto one key per platform."""
        from jobhunt.pipeline import feed_key
        entries = [{"ats": a, "board": b} for a in ("workable", "breezy", "recruitee", "smartrecruiters", "bamboohr", "rippling",
                                                    "greenhouse", "ashby") for b in ("one", "two")]
        entries += [{"ats": "page", "url": "https://a.example/careers"}, {"ats": "page", "url": "https://b.example/careers/"},
                    {"ats": "lever", "slug": "x"}, {"ats": "lever", "slug": "y"}]
        self.assertEqual(len({feed_key(e) for e in entries}), len(entries))
        self.assertEqual(feed_key({"ats": "workable", "board": "One"}), feed_key({"ats": "workable", "board": "one"}))


class ResetTest(unittest.TestCase):
    def test_reset_clears_in_play_but_learning_and_retune_evidence_survive(self):
        from jobhunt.report import views
        from jobhunt.store import Store
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = Store(tmp)
        rec = lambda i, c: {"id": i, "company": c, "title": "ML Engineer", "source": "greenhouse", "active": True,
                            "last_seen": "2026-09-20T00:00:00+00:00"}
        s.jobs["jobs"] = {i: rec(i, c) for i, c in (("a", "Acme"), ("b", "Beta"), ("c", "Core"), ("d", "Delta"))}
        s.save()
        s.set_state("a", status="saved"); s.set_state("b", status="skip"); s.set_state("c", status="applied")
        before = learn.build_model(s.jobs["jobs"], s.state["jobs"])
        got = s.reset("2026-09-27T12:00:00+00:00")
        self.assertEqual(got, {"postings_cleared": 1, "saved_archived": 1})          # "d" was never decided on
        self.assertEqual(set(s.jobs["jobs"]), {"a", "b", "c"})
        self.assertEqual(learn.build_model(s.jobs["jobs"], s.state["jobs"]), before)  # learning unchanged
        shown = {r["id"]: r["state"]["status"] for r in views.rows_for(s)}
        self.assertEqual(shown, {"b": "skip", "c": "applied"})                        # Saved is empty, history kept
        s.jobs["jobs"]["a"]["last_seen"] = "2026-09-28T02:00:00+00:00"; s.save()      # a scan sees it again
        self.assertEqual({r["id"]: r["state"]["status"] for r in views.rows_for(s)}["a"], "new")
        s.reset("2026-09-29T00:00:00+00:00")                                          # a later reset clears it again
        self.assertNotIn("a", {r["id"] for r in views.rows_for(s)})
        s.set_state("a", status="saved")                                              # save it again: back in play
        self.assertNotIn("archived", s.get_state("a"))


class BankConcurrencyTest(unittest.TestCase):
    def test_two_writers_merge_instead_of_overwriting(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seed = bankmod.Bank(tmp); a0 = seed.record("Q one?", "text", [], False, {}); seed.record("Q two?", "text", [], False, {}); seed.save()
        one, two = bankmod.Bank(tmp), bankmod.Bank(tmp)          # both loaded the same file
        one.set(a0.key, answer="A", status="approved")
        two.record("Q three?", "text", [], True, {})
        two.remove(bankmod.question_key("Q two?"))
        one.save(); two.save()                                    # two's copy is stale about one's change
        got = {e.question: e for e in bankmod.read_bank(tmp)}
        self.assertEqual(set(got), {"Q one?", "Q three?"})
        self.assertEqual((got["Q one?"].answer, got["Q one?"].status), ("A", "approved"))


class RunsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_state_machine_and_folder(self):
        job = {"id": "greenhouse:ex:1", "company": "Ex", "title": "Role"}
        run = runsmod.new_run(self.tmp, job, "engineer", ats="greenhouse", url="https://x")
        self.assertEqual(run.state, "queued"); self.assertTrue(run.id.endswith("greenhouse_ex_1"))
        self.assertTrue((run.path / "run.json").exists())
        run.set_state("running", pid=os.getpid())
        run.pause("questions", note="2 open")
        again = runsmod.load_run(self.tmp, run.id)
        self.assertEqual(again.state, "needs_you"); self.assertEqual(again.get("reason"), "questions")
        self.assertIn("approved answer", again.get("reason_text"))
        self.assertEqual([h["state"] for h in again.get("history")], ["queued", "running", "needs_you"])
        self.assertEqual(len(again.steps()), 3)
        with self.assertRaises(ValueError):
            run.set_state("bogus")
        run.write_control("resume")
        self.assertEqual(run.read_control(), "resume"); self.assertIsNone(run.read_control())   # consumed
        self.assertIsNone(runsmod.load_run(self.tmp, "../etc")); self.assertIsNone(runsmod.load_run(self.tmp, "nope"))
        two = runsmod.new_run(self.tmp, job, "engineer")
        self.assertNotEqual(two.id, run.id)

    def test_reap_never_lets_a_dead_process_fail_silently(self):
        job = {"id": "j", "company": "Ex", "title": "Role"}
        dead = 4000000000                                   # never a live pid
        r1 = runsmod.new_run(self.tmp, job, "engineer"); r1.set_state("running", pid=dead)
        r2 = runsmod.new_run(self.tmp, job, "engineer"); r2.set_state("running", pid=dead); r2.pause("review_and_submit")
        r3 = runsmod.new_run(self.tmp, job, "engineer")                                        # queued: no pid yet
        r4 = runsmod.new_run(self.tmp, job, "engineer"); r4.set_state("running", pid=os.getpid())   # alive
        runs = {r.id: r for r in runsmod.list_runs(self.tmp)}
        self.assertEqual(runs[r1.id].state, "failed"); self.assertIn("exited without", runs[r1.id].get("error"))
        self.assertEqual((runs[r2.id].state, runs[r2.id].get("reason")), ("needs_you", "window_closed"))
        self.assertEqual(runs[r3.id].state, "queued"); self.assertEqual(runs[r4.id].state, "running")
        c = runsmod.counts(list(runs.values()))
        self.assertEqual((c["failed"], c["needs_you"], c["queued"], c["running"], c["open"]), (1, 1, 1, 1, 3))
        self.assertEqual([r.id for r in runsmod.list_runs(self.tmp, job_id="other")], [])
        self.assertEqual(runs[r2.id].summary()["open_questions"], 0)


# ----------------------------------------------------------------------------- apply

class ApplyTest(unittest.TestCase):
    """Drives the form filler against local copies of the three ATS form layouts."""

    @classmethod
    def setUpClass(cls):
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.resume = cls.tmp / "resume.pdf"
        cls.resume.write_bytes(b"%PDF-1.4\n%fake\n")
        cls.profile = json.loads(json.dumps(PROFILE))
        cls.profile["documents"]["resume"] = str(cls.resume)
        cls.profile["links"]["linkedin"] = "https://www.linkedin.com/in/engineer"
        cls.profile["answers"]["salary_expectation"] = "150000"
        cls.profile["answers"]["custom"] = {"why do you want to work": "Because {company} builds things I want to build."}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_fixture(self, name, ats, bank=None, run=None):
        from jobhunt.apply.forms import run_apply
        job = {"id": "t", "company": "Example", "title": "Role", "apply_url": (FIX / name).as_uri(), "url": ""}
        log = []
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, log=log.append, ats=ats,
                        bank=bank, run=run)
        return rep, log

    def test_greenhouse_layout_blocks_on_ai_question(self):
        rep, log = self.run_fixture("greenhouse_form.html", "greenhouse")
        filled = dict(rep.filled)
        self.assertEqual(filled.get("First Name *"), PROFILE["identity"]["first_name"])
        self.assertEqual(filled.get("Email *"), PROFILE["identity"]["email"])
        self.assertEqual(filled.get("Are you legally authorized to work in the United States? *"), "Yes")
        self.assertEqual(filled.get("Will you now or in the future require sponsorship for employment visa status?"), "No")
        self.assertEqual(filled.get("Gender"), "Decline To Self Identify")
        self.assertEqual(filled.get("Current Company"), PROFILE["identity"]["current_company"])
        self.assertIn("Because Example builds", filled.get("Why do you want to work at Example Labs? *", ""))
        self.assertIn("Resume/CV *", rep.uploaded)
        self.assertTrue(any("AI use" in why for _, why in rep.flagged), rep.flagged)
        self.assertTrue(any("AI-use" in b for b in rep.blockers), rep.blockers)
        self.assertFalse(rep.submitted)  # blocked, so auto-submit must not fire
        self.assertIn("consent", " ".join(l for l, _ in rep.filled).lower())
        self.assertEqual(filled.get("Which languages do you speak fluently? *"), "English (ENG)")
        self.assertEqual(filled.get("Please select all that apply: *"), "None of the above")
        self.assertNotIn("Over the past 18 months, what percentage of your work has involved hands-on coding?", filled)
        self.assertFalse(any(l in ("Spanish", "English", "None of the above") for l, _ in rep.flagged), rep.flagged)
        self.assertNotIn("", [l for l, _ in rep.flagged])      # react-select's hidden requiredInput is not a field

    def test_variant_is_what_gets_uploaded(self):
        """A built variant set as documents.resume reaches the form, and the trace names the file."""
        from jobhunt.apply import tailor, terms as termsmod
        prof = self.tmp / "prof-variant"
        (prof / "resume").mkdir(parents=True)
        (prof / "resume" / "master.html").write_text((FIX / "resume_master.html").read_text(encoding="utf-8"), encoding="utf-8")
        termsmod.write_terms(prof, [termsmod.TermEntry("skill", "Python", status="approved")])
        job = {"id": "lever:ex:9", "company": "Example", "title": "Role", "description": "Python please"}
        v = tailor.build_variant(prof, job, base=Path("Pat_Example_Resume.pdf"), log=lambda s: None)
        self.assertTrue(v.pdf.exists())
        profile = json.loads(json.dumps(self.profile))
        profile["documents"]["resume"] = str(v.pdf)
        from jobhunt.apply.forms import run_apply
        job["apply_url"] = (FIX / "lever_form.html").as_uri(); job["url"] = ""
        run = runsmod.new_run(self.tmp / "data-variant", job, "engineer", ats="lever")
        rep = run_apply(job, profile, headless=True, wait_for_user=False, log=lambda s: None, ats="lever", run=run)
        self.assertIn("Resume/CV ✱", rep.uploaded)
        self.assertIsNone(rep.error)
        fields = json.loads((run.path / "fields.json").read_text(encoding="utf-8"))
        details = [f.get("detail") or "" for f in (fields.values() if isinstance(fields, dict) else fields) if f.get("kind") == "file"]
        self.assertTrue(any(d.startswith(f"uploaded {v.name}") for d in details), details)

    def test_lever_layout_pauses_for_review_never_submits(self):
        data = self.tmp / "data-lever"
        job = {"id": "lever:ex:1", "company": "Example", "title": "Role"}
        run = runsmod.new_run(data, job, "engineer", ats="lever")
        rep, log = self.run_fixture("lever_form.html", "lever", bank=bankmod.Bank(self.tmp / "bank-lever"), run=run)
        filled = dict(rep.filled)
        self.assertEqual(filled.get("Full name ✱"), FULL_NAME)
        self.assertEqual(filled.get("Are you legally authorized to work in the United States? ✱"), "Yes")
        self.assertEqual(filled.get("Do you currently hold an active security clearance?"), "No")
        self.assertEqual(filled.get("Current company"), PROFILE["identity"]["current_company"])
        self.assertEqual(filled.get("LinkedIn URL"), "https://www.linkedin.com/in/engineer")
        self.assertIn("Resume/CV ✱", rep.uploaded)
        self.assertEqual(rep.blockers, [])
        self.assertFalse(rep.submitted, (rep.summary(), log))      # submission is always yours
        self.assertEqual(rep.reason, "review_and_submit")
        # the run folder is the audit trail
        again = runsmod.load_run(data, run.id)
        self.assertEqual((again.state, again.get("reason")), ("needs_you", "review_and_submit"))
        self.assertEqual(again.get("pid"), os.getpid())
        self.assertTrue(any(f.endswith("-filled.png") for f in again.get("screenshots")), again.get("screenshots"))
        self.assertTrue((again.path / again.get("screenshots")[0]).exists())
        self.assertEqual(again.get("report")["filled"], [list(x) for x in rep.filled])
        self.assertIn("state -> needs_you (review_and_submit): ready", [x["msg"] for x in again.steps()])
        self.assertTrue(rep.ready and again.get("report")["ready"])
        # a required-marker span is not the question: the label is the card's text
        self.assertIn("Are you willing to work in a SCIF?✱", [q["label"] for q in rep.questions])
        self.assertEqual(rep.verify, [])      # every filled value read back as written

    def test_ashby_layout_records_unknown_question_then_fills_it_once_approved(self):
        bank_dir = self.tmp / "bank-ashby"
        bank = bankmod.Bank(bank_dir)
        rep, log = self.run_fixture("ashby_form.html", "ashby", bank=bank)
        filled = dict(rep.filled)
        self.assertEqual(filled.get("Name *"), FULL_NAME)
        self.assertEqual(filled.get("Do you now, or will you in the future, require sponsorship for employment visa status? *"), "No")
        # "Company careers page" only loosely matches the option "Careers page": typed, listed for review
        heard = [x for x in rep.review if x["label"] == "How did you hear about us?"][0]
        self.assertEqual((heard["tier"], heard["value"]), ("inferred", "Careers page"))
        self.assertEqual(filled.get("How did you hear about us?"), "Careers page")
        self.assertEqual(filled.get("What is your expected salary range?"), "150000")
        self.assertIn("Resume *", rep.uploaded)
        self.assertFalse(rep.submitted)
        q = "Describe a system you deployed to production and what you learned."
        # the open-ended question is optional -> not filled, not a blocker, but recorded as pending
        self.assertNotIn(q, filled)
        self.assertEqual(rep.blockers, [])
        self.assertEqual(rep.reason, "review_and_submit")
        pending = [x for x in rep.questions if x["label"] == q]
        self.assertEqual(len(pending), 1, rep.questions)
        self.assertFalse(pending[0]["required"]); self.assertFalse(pending[0]["answered"])
        saved = bankmod.read_bank(bank_dir)
        self.assertEqual(sorted(e.question for e in saved), sorted(["How did you hear about us?", q]))
        saved_q = next(e for e in saved if e.question == q)
        self.assertEqual(saved_q.status, "pending")
        self.assertEqual(saved_q.seen, 1)           # two fill passes, counted once
        # approve it (as the Answers page would) and run again: it is filled verbatim
        bank2 = bankmod.Bank(bank_dir)
        bank2.set(pending[0]["key"], answer="I shipped Beacon at {company}.", status="approved")
        bank2.set(heard["key"], answer=heard["value"], status="approved"); bank2.save()
        rep2, _ = self.run_fixture("ashby_form.html", "ashby", bank=bankmod.Bank(bank_dir))
        self.assertEqual(dict(rep2.filled).get(q), "I shipped Beacon at Example.")
        self.assertEqual(dict(rep2.filled).get("How did you hear about us?"), "Careers page")   # approved: certain now
        self.assertEqual([x for x in rep2.questions if not x["answered"]], [])
        self.assertEqual(next(e for e in bankmod.read_bank(bank_dir) if e.question == q).seen, 2)

    def test_drafts_are_suggestions_not_form_input(self):
        bank = bankmod.Bank(self.tmp / "bank-draft")
        from jobhunt.apply.forms import run_apply
        job = {"id": "t", "company": "Example", "title": "Role", "apply_url": (FIX / "ashby_form.html").as_uri(), "url": ""}
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, log=lambda s: None, ats="ashby",
                        bank=bank, drafter=lambda label, j: "A model-written paragraph.")
        q = [x for x in rep.questions if x["label"].startswith("Describe a system")][0]
        self.assertEqual(q["draft"], "A model-written paragraph.")
        self.assertNotIn(q["label"], dict(rep.filled))
        describe = [e for e in bankmod.read_bank(self.tmp / "bank-draft") if e.question.startswith("Describe a system")][0]
        self.assertEqual(describe.answer, "")   # the bank stays yours

    def test_embedded_form_in_an_iframe_is_read_and_filled(self):
        # an employer career page embedding the ATS form: the fields live in a child frame
        rep, log = self.run_fixture("embedded_careers.html", "lever")
        filled = dict(rep.filled)
        self.assertEqual(filled.get("Full name ✱"), FULL_NAME)
        self.assertIn("Resume/CV ✱", rep.uploaded)
        self.assertEqual(rep.verify, [])
        self.assertTrue(rep.ready, rep.summary())

    def test_a_value_that_does_not_stick_and_near_misses_keep_the_run_from_ready(self):
        rep, _ = self.run_fixture("sabotage_form.html", "greenhouse", bank=bankmod.Bank(self.tmp / "bank-sabotage"))
        filled = dict(rep.filled)
        self.assertEqual(filled.get("First Name *"), PROFILE["identity"]["first_name"])
        # the adapter typed the phone number; the page threw it away; verify noticed
        self.assertEqual([m["label"] for m in rep.verify], ["Phone *"])
        # clearance_option answers "active clearance?", not "eligibility": asked, not typed
        self.assertNotIn("Clearance eligibility *", filled)
        # a general travel answer for a 50% question is typed, but outlined and listed for review
        travel = [r for r in rep.review if r["label"].startswith("Are you willing to travel")][0]
        self.assertEqual((travel["tier"], travel["value"]), ("inferred", "Yes"))
        # "worked for a government agency" is not a question about this employer
        self.assertNotEqual(filled.get("Have you ever worked for a government agency? *"), "No")
        self.assertFalse(rep.ready)
        self.assertEqual(rep.reason, "questions")
        kinds = {i.kind for i in rep.issues}
        self.assertTrue({"blocked", "question"} <= kinds, kinds)
        self.assertLess(rep.fill["ratio"], 1.0)

    def test_the_agent_types_grounded_answers_but_never_sensitive_ones(self):
        from jobhunt.apply.persona import AgentAnswer
        asked = []

        class Agent:       # answers everything it is asked; the policy decides what reaches the form
            def ask(self, q, kind="textarea", options=None, job=None):
                asked.append(q)
                return AgentAnswer(question=q, answer=("No" if options else "3.9"), evidence=["F1"])
        from jobhunt.apply.forms import run_apply
        job = {"id": "t", "company": "Example", "title": "Role", "apply_url": (FIX / "sabotage_form.html").as_uri()}
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, log=lambda s: None, ats="greenhouse",
                        bank=bankmod.Bank(self.tmp / "bank-agent"), agent=Agent())
        review = {r["label"]: r for r in rep.review}
        self.assertEqual(review["What was your undergraduate GPA? *"]["tier"], "agent")
        self.assertEqual(dict(rep.filled)["What was your undergraduate GPA? *"], "3.9")
        # a restrictive-agreement question is never typed from the agent, nor its follow-up
        self.assertNotIn("Are you bound by any agreements with a current or former employer? *", dict(rep.filled))
        self.assertNotIn("If yes, please explain", dict(rep.filled))
        self.assertFalse(any("bound by" in q for q in asked), asked)
        # the follow-up is stored with the question it follows
        qs = [q["question"] for q in rep.questions if q["label"] == "If yes, please explain"]
        self.assertTrue(qs and "follow-up to: Are you bound by any agreements" in qs[0], qs)
        self.assertGreater(rep.fill["ratio"], 0.5)

    def test_a_widget_no_adapter_knows_is_healed_once_then_from_cache(self):
        from jobhunt.apply.forms import run_apply
        calls = []

        def model(prompt):
            calls.append(prompt)
            return '{"steps": [{"do": "click", "css": ".picker-open"}, {"do": "pick", "text": "{value}"}]}'
        recipes = self.tmp / "recipes.json"
        job = {"id": "t", "company": "Example", "title": "Role", "apply_url": (FIX / "broken_widget_form.html").as_uri()}
        kw = dict(headless=True, wait_for_user=False, log=lambda s: None, ats="greenhouse", heal_call=model, recipes_path=recipes)
        rep = run_apply(job, self.profile, **kw)
        self.assertEqual(dict(rep.filled).get("Current company *"), PROFILE["identity"]["current_company"])
        self.assertTrue(any(h["ok"] and h["from"] == "model" for h in rep.heals), rep.heals)
        self.assertEqual(len(calls), 1)
        rep2 = run_apply(job, self.profile, **kw)                    # same widget, next form: no model call
        self.assertEqual(dict(rep2.filled).get("Current company *"), PROFILE["identity"]["current_company"])
        self.assertTrue(any(h["ok"] and h["from"] == "cache" for h in rep2.heals), rep2.heals)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rep2.verify, [])

    def test_captured_fixtures_are_read_as_they_were_labelled(self):
        """Every field captured from a real form with `jobhunt.py fixture` must keep reading with its label and kind."""
        from playwright.sync_api import sync_playwright
        from jobhunt.apply.read import read_form
        cases = sorted((FIX / "captured").glob("*.json")) if (FIX / "captured").exists() else []
        if not cases:
            self.skipTest("no captured fixtures yet")
        with sync_playwright() as pw:
            page = pw.chromium.launch(headless=True).new_context(java_script_enabled=False).new_page()
            for c in cases:
                want = json.loads(c.read_text())
                page.set_content(c.with_suffix(".html").read_text())
                got = [(f.label, f.kind) for f in read_form(page)]
                self.assertIn((want["label"], want["kind"]), got, f"{c.name}: {got}")

    def test_replay_finds_no_change_when_rules_are_unchanged(self):
        from jobhunt.apply.forms import run_apply
        from jobhunt.apply import replay as replaymod
        data = self.tmp / "data-replay"
        job = {"id": "lever:ex:9", "company": "Example", "title": "Role", "apply_url": (FIX / "lever_form.html").as_uri()}
        run = runsmod.new_run(data, job, "engineer", ats="lever")
        run_apply(job, self.profile, headless=True, run=run, rehearse=True, log=lambda s: None, ats="lever")
        self.assertTrue((run.path / "form.html").exists())
        got = replaymod.replay([run.path], self.profile, {"lever:ex:9": job})
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0]["same"], 5)
        self.assertEqual(got[0]["diffs"], [], got[0]["diffs"])

    def test_an_aggregator_listing_resolves_to_the_employer_posting(self):
        from playwright.sync_api import sync_playwright
        from jobhunt.apply.resolve import resolve
        job = {"id": "themuse:1", "company": "Example", "title": "Machine Learning Platform Role",
               "apply_url": (FIX / "aggregator_listing.html").as_uri()}
        with sync_playwright() as pw:
            page = pw.chromium.launch(headless=True).new_page()
            url, how = resolve(page, job, log=lambda s: None, aggregator=True)
        self.assertTrue(url and url.endswith("careers_posting.html"), (url, how))
        self.assertIn("matching the title", how)

    def test_the_navigator_clicks_through_to_a_form_on_a_custom_site(self):
        from jobhunt.apply.forms import run_apply
        job = {"id": "t", "company": "Example", "title": "Machine Learning Platform Role",
               "apply_url": (FIX / "careers_posting.html").as_uri()}
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, log=lambda s: None)
        self.assertEqual(rep.ats, "generic")
        self.assertTrue(rep.supervised)
        self.assertEqual(rep.navigation[0]["why"], "obvious apply control")
        self.assertEqual(dict(rep.filled).get("First name *"), PROFILE["identity"]["first_name"])
        self.assertIn("Resume *", rep.uploaded)
        self.assertTrue(any(i.id == "note:supervised" for i in rep.issues))

    def _vault(self):
        from cryptography.fernet import Fernet
        from jobhunt.apply.vault import Vault
        os.environ["JOBHUNT_VAULT_KEY"] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, "JOBHUNT_VAULT_KEY", None)
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return Vault(d / "vault.enc", d)

    def test_workday_flow_creates_the_account_walks_the_pages_and_stops_at_review(self):
        from jobhunt.apply.forms import run_apply
        v = self._vault()
        job = {"id": "workday:x:1", "company": "Example", "title": "ML", "apply_url": (FIX / "workday_flow.html").as_uri()}
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, ats="workday", vault=v, log=lambda s: None)
        self.assertEqual(rep.navigation[-1]["workday"]["outcome"], "review")
        self.assertTrue(rep.ready, rep.summary())
        filled = dict(rep.filled)
        self.assertEqual(filled.get("Country*"), "United States")          # a listbox button, read and set
        self.assertEqual(filled.get("Are you legally authorized to work in the United States?*"), "Yes")
        self.assertEqual(len(v.sites()), 1)                                  # the login is in the vault
        self.assertNotIn(v.get(v.sites()[0])["password"], json.dumps(rep.to_dict()))   # and nowhere else

    def test_workday_pauses_without_a_vault_and_for_email_verification(self):
        from jobhunt.apply.forms import run_apply
        job = {"id": "workday:x:2", "company": "Example", "title": "ML", "apply_url": (FIX / "workday_flow.html").as_uri()}
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, ats="workday", vault=None, log=lambda s: None)
        self.assertEqual(rep.reason, "vault")
        job["apply_url"] += "?verify=1"
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, ats="workday", vault=self._vault(), log=lambda s: None)
        self.assertEqual(rep.reason, "verify_email")

    def test_submit_is_never_a_next_button(self):
        from playwright.sync_api import sync_playwright
        from jobhunt.apply.pages import next_button
        from jobhunt.apply.playbooks.workday import NEXT, is_review
        with sync_playwright() as pw:
            page = pw.chromium.launch(headless=True).new_page()
            page.goto((FIX / "workday_flow.html").as_uri())
            page.evaluate("show('review', 'Review')")
            self.assertTrue(is_review(page))
            self.assertIsNone(next_button(page, NEXT))
            page.goto((FIX / "multistep_form.html").as_uri())
            page.evaluate("go(3)")
            self.assertIsNone(next_button(page))

    def test_a_generic_multistep_form_is_filled_page_by_page(self):
        from jobhunt.apply.forms import run_apply
        job = {"id": "t", "company": "Example", "title": "Engineer", "apply_url": (FIX / "multistep_form.html").as_uri()}
        rep = run_apply(job, self.profile, headless=True, wait_for_user=False, log=lambda s: None)
        self.assertEqual(rep.ats, "generic")
        filled = dict(rep.filled)
        self.assertEqual(filled.get("Full name *"), FULL_NAME)                # placeholder, not "Step 1 / 3"
        self.assertIn("Resume *", rep.uploaded)
        self.assertEqual(filled.get("Are you legally authorized to work in the United States? *"), "Yes")
        self.assertEqual(rep.navigation[-1]["pages"], "end")

    def test_rehearsal_fills_verifies_and_closes(self):
        data = self.tmp / "data-rehearse"
        job = {"id": "lever:ex:2", "company": "Example", "title": "Role"}
        run = runsmod.new_run(data, job, "engineer", ats="lever")
        from jobhunt.apply.forms import run_apply
        j = {"id": "t", "company": "Example", "title": "Role", "apply_url": (FIX / "lever_form.html").as_uri()}
        rep = run_apply(j, self.profile, headless=True, run=run, rehearse=True, log=lambda s: None, ats="lever")
        again = runsmod.load_run(data, run.id)
        self.assertEqual(again.state, "rehearsed")
        self.assertIn("rehearsed", runsmod.TERMINAL)
        self.assertTrue(again.get("report")["ready"])
        self.assertFalse(rep.submitted)
        self.assertTrue((again.path / "issues.jsonl").exists())
        from jobhunt.apply.stats import apply_stats
        s = apply_stats([again])
        self.assertEqual((s["rehearsals"], s["rehearsals_clean"]), (1, 1))

    def test_employer_autofill_first_then_its_values_are_checked(self):
        # the page parses the resume into the form; what it got right is kept, what it got wrong is
        # corrected, and what we cannot check is a proposal for you
        rep, _ = self.run_fixture("autofill_form.html", "lever", bank=bankmod.Bank(self.tmp / "bank-autofill"))
        self.assertEqual(rep.autofill["how"], "resume uploaded to 'Resume *'")
        self.assertEqual(rep.autofill["kept"], 1)                                   # the name
        corrected = {c["label"]: c for c in rep.autofill["corrected"]}
        self.assertEqual(corrected["Current company"]["now"], PROFILE["identity"]["current_company"])
        self.assertEqual(corrected["Current company"]["page"], "Vandelay Industries")
        school = [q for q in rep.questions if q["label"] == "School"][0]
        self.assertEqual((school["tier"], school["draft"]), ("inferred", "Example State University"))
        self.assertEqual(rep.verify, [])
        self.assertTrue(rep.ready, rep.summary())


class ApplyStagesTest(unittest.TestCase):
    """The answer, verify and issue stages, without a browser."""

    def test_prepare_digest_lists_each_question_once(self):
        from jobhunt.apply.stats import needs_you

        class R:
            def __init__(self, **d):
                self.d, self.state = d, "rehearsed"

            def get(self, k, default=None):
                return self.d.get(k, default)
        q = {"kind": "question", "label": "Undergraduate GPA", "blocking": True, "why": "the agent declined"}
        runs = [R(company="A", title="x", report={"ready": True, "review": [{}], "tiers": {"certain": 3}}, issues=[]),
                R(company="B", title="y", report={"ready": False, "tiers": {"certain": 3}}, issues=[q]),
                R(company="C", title="z", report={"ready": False, "tiers": {"certain": 3}}, issues=[q]),
                R(company="D", title="w", reason="verify_email", report={"blockers": ["check your email"]}, issues=[])]
        text = needs_you(runs)
        self.assertIn("Ready for one click: 1", text)
        self.assertIn("Questions: 1 across 2 posting(s)", text)
        self.assertIn("Undergraduate GPA  (2×)", text)
        self.assertIn("Accounts: 1", text)

    def test_what_you_answer_in_the_window_is_remembered_and_company_answers_become_voice(self):
        from jobhunt.apply.forms import learn_from_submit
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bank = bankmod.Bank(tmp)
        kg = bank.record("GPA?", "text", [], True, {}).key
        kw = bank.record("Why {company}?", "textarea", [], True, {"company": "Acme"}).key
        bank.save()
        questions = [{"label": "GPA? *", "key": kg, "question": "GPA?"}, {"label": "Why Acme?", "key": kw, "question": "Why {company}?"}]
        edits = {"changed": [], "filled_by_you": [{"label": "GPA? *", "value": "3.9", "ident": "a"},
                                                  {"label": "Why Acme?", "value": "Because Acme builds robots.", "ident": "b"}]}
        got = learn_from_submit(bank, [], edits, questions=questions, job={"company": "Acme"})
        self.assertEqual((got["answered"], got["voice"]), (1, 1))
        b = bankmod.Bank(tmp)
        self.assertEqual((b.get(kg).answer, b.get(kg).status, b.get(kg).by), ("3.9", "approved", ""))
        self.assertFalse(b.get(kw).approved)                                  # not reused at the next company
        voice = (tmp / "notes" / "voice.md").read_text()
        self.assertIn("## Answers I wrote", voice); self.assertIn("Because Acme builds robots.", voice)

    def test_a_submit_teaches_the_bank(self):
        from jobhunt.apply.forms import learn_from_submit
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bank = bankmod.Bank(tmp)
        k1 = bank.record("GPA?", "text", [], True, {}).key
        k2 = bank.record("Years of Python?", "text", [], True, {}).key
        k3 = bank.record("Why {company}?", "textarea", [], True, {}).key
        bank.save()
        review = [{"label": "GPA", "value": "3.9", "tier": "agent", "key": k1},
                  {"label": "Years", "value": "7", "tier": "agent", "key": k2},
                  {"label": "Why", "value": "Because Acme.", "tier": "agent", "key": k3, "company_specific": True}]
        got = learn_from_submit(bank, review, {"changed": [{"label": "Years", "got": "8"}]})
        self.assertEqual((got["approved"], got["edited"]), (1, 1))
        b = bankmod.Bank(tmp)
        self.assertEqual((b.get(k1).answer, b.get(k1).status, b.get(k1).by), ("3.9", "approved", "approved-by-submit"))
        self.assertEqual((b.get(k2).answer, b.get(k2).by), ("8", ""))          # your edit is the answer, and yours
        self.assertFalse(b.get(k3).approved)                                     # never reused at another company

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profile = json.loads(json.dumps(PROFILE))

    def field(self, label, kind="select", options=("Yes", "No"), required=True):
        from jobhunt.apply.schema import Field
        return Field(label=label, kind=kind, options=list(options) if kind in ("select", "radio", "combobox", "checkgroup") else [],
                     required=required)

    def test_no_never_picks_not_applicable(self):
        from jobhunt.apply.schema import match_option
        self.assertEqual(match_option(["Not applicable", "No", "Yes"], "No"), ("No", "strong"))
        self.assertEqual(match_option(["N/A", "Yes"], "No"), (None, ""))
        self.assertEqual(match_option(["None of the above", "Yes"], "No"), (None, ""))
        self.assertEqual(match_option(["Yes", "No"], "Yes, occasional travel is fine"), ("Yes", "strong"))
        self.assertEqual(match_option(["Careers page", "Referral"], "Company careers page"), ("Careers page", "weak"))

    def test_profile_values_are_certain_only_for_their_own_question(self):
        from jobhunt.apply.answer import Answerer
        a = Answerer(self.profile, {"company": "Initech", "title": "Engineer"})
        active = a.answer(self.field("Do you currently hold an active security clearance?"))
        self.assertEqual((active.value, active.tier), ("No", "certain"))
        elig = a.answer(self.field("Clearance eligibility"))
        self.assertEqual(elig.tier, "unknown"); self.assertIsNone(elig.value); self.assertTrue(elig.ask)
        travel = a.answer(self.field("Are you willing to travel up to 50%?"))
        self.assertEqual(travel.tier, "inferred")
        email = a.answer(self.field("Email", kind="text"))
        self.assertEqual((email.value, email.tier), (PROFILE["identity"]["email"], "certain"))
        prev = a.answer(self.field("Have you ever been employed by Initech?"))
        self.assertEqual(prev.source, "profile:answers.previously_employed")

    def test_fuzzy_bank_match_is_a_proposal_and_approving_it_makes_it_certain(self):
        from jobhunt.apply.answer import Answerer
        bank = bankmod.Bank(self.tmp)
        e = bank.record("Why do you want to work at Example?", "textarea", [], True, {"company": "Example"})
        bank.set(e.key, answer="Because.", status="approved")
        a = Answerer(self.profile, {"company": "Example"}, bank=bank)
        f = self.field("Why do you want to work at Example?!", kind="textarea")
        exact_ish = a.answer(f)
        f2 = self.field("Why do you want to work for Example?", kind="textarea")
        near = a.answer(f2)
        self.assertEqual(exact_ish.tier, "certain")
        self.assertEqual((near.tier, near.value), ("inferred", "Because."))
        q = a.ask(f2, near)
        self.assertNotEqual(q["key"], e.key)            # the new phrasing gets its own entry
        from jobhunt.apply import issues as issuesmod
        issue = issuesmod.from_question(q)
        self.assertEqual((issue.kind, issue.proposal), ("confirm_match", "Because."))
        run_dir = self.tmp / "run"; run_dir.mkdir()
        issuesmod.write_issues(run_dir, [issue])
        issuesmod.respond(run_dir, issue.id, "approve")
        got = issuesmod.apply_responses(issuesmod.take_responses(run_dir), issuesmod.read_issues(run_dir), bank)
        self.assertEqual(got["applied"], 1)
        self.assertEqual(issuesmod.take_responses(run_dir), [])     # consumed once
        again = Answerer(self.profile, {"company": "Example"}, bank=bankmod.Bank(self.tmp)).answer(f2)
        self.assertEqual((again.tier, again.value), ("certain", "Because."))

    def test_rules_found_by_the_first_rehearsal(self):
        """Each of these was a wrong answer or a false alarm on a real form (September 2026)."""
        from jobhunt.apply.answer import Answerer
        from jobhunt.apply.schema import match_option
        from jobhunt.apply import verify
        a = Answerer(self.profile, {"company": "Umbrella"})
        # "preferred" contains "referred": pronouns are not a how-did-you-hear question
        pron = a.answer(self.field("What are your preferred pronouns?", kind="combobox", options=["She/Her", "He/Him"], required=False))
        self.assertTrue(pron.skip or pron.source == "rule:eeo", pron)
        # a preferred name is yours to give, not a copy of the first name
        self.assertTrue(a.answer(self.field("Preferred First Name", kind="text", required=False)).skip)
        # a travel question that mentions a location is still a travel question
        trav = a.answer(self.field("Umbrella requires employees to travel twice a quarter to headquarters or another designated location. Are you able?"))
        self.assertEqual(trav.source, "profile:answers.travel")
        # an option that adds a claim is not a strong match for a bare "No"
        self.assertEqual(match_option(["Yes", "No - I am interested in obtaining one."], "No")[1], "weak")
        clr = a.answer(self.field("Do you have a security clearance?", options=["Yes", "No - I am interested in obtaining one."]))
        self.assertEqual(clr.tier, "inferred")
        # second rehearsal
        self.assertEqual(a.answer(self.field("Please tell us how you heard about this opportunity.", options=["Careers page", "Other"])).source,
                         "profile:answers.how_heard")
        note = a.answer(self.field("As part of our interview process, we may use AI notetakers to transcribe conversations. Do you consent?",
                                   options=["Yes, I consent", "No, I do not consent"]))
        self.assertNotEqual(note.source, "rule:ai-use")
        pick = a.answer(self.field("Please select the active US Federal Government security clearance you hold", kind="text"))
        self.assertEqual(pick.tier, "unknown")
        self.assertTrue(a.answer(self.field("+263", kind="text")).skip)
        from jobhunt.apply.widgets import best_suggestion
        subs = ["Columbus, GA, USA", "Columbus, OH, USA", "Columbus, IN, USA"]
        self.assertEqual(best_suggestion(subs, "Columbus, OH"), "Columbus, OH, USA")
        self.assertIsNone(best_suggestion(["Columbus, GA, USA", "Columbus, IN, USA"], "Columbus, OH"))
        self.assertEqual(best_suggestion(["Columbus, Indiana, United States", "Columbus, Ohio, United States"], "Columbus, OH"),
                         "Columbus, Ohio, United States")      # Greenhouse spells the state out
        # a phone field that formats itself still holds the number
        self.assertTrue(verify.matches("text", "5550100100", "(555) 010-0100"))
        # a file input swapped for a chip showing the file name
        self.assertEqual(verify.settle_files([{"label": "Resume", "expected": "cv.pdf", "got": None}], {"Resume": "file"}, "cv.pdf ✓"), [])

    def test_an_upload_labelled_by_its_button_is_known_by_its_id(self):
        from jobhunt.apply.read import file_label
        self.assertEqual(file_label({"id": "resume"}, "Attach file"), "Resume/CV")
        self.assertEqual(file_label({"id": "cover_letter"}, "Attach"), "Cover Letter")
        self.assertEqual(file_label({"id": "resume"}, "Resume/CV *"), "Resume/CV *")

    def test_a_careers_home_page_is_not_an_application(self):
        from jobhunt.apply.read import looks_like_application
        from jobhunt.apply.schema import Field
        self.assertFalse(looks_like_application([Field("Search jobs", "text"), Field("Department", "checkgroup")]))
        self.assertTrue(looks_like_application([Field("Full name", "text"), Field("Email", "text")]))
        self.assertFalse(looks_like_application([Field("Full name", "text", visible=False), Field("Email", "text", visible=False)]))
        # the signal for a closed posting is the redirect: Initech's landed on a careers page whose
        # talent-community box even had a resume upload
        from jobhunt.apply.forms import off_the_form

        class Page:
            def __init__(self, url, frames=()):
                self.url, self.frames = url, [self] + [type("F", (), {"url": u})() for u in frames]
        self.assertIn("redirected", off_the_form(Page("https://www.anduril.com/open-roles"), "greenhouse"))
        self.assertIsNone(off_the_form(Page("https://job-boards.greenhouse.io/oklo/jobs/1"), "greenhouse"))
        self.assertIsNone(off_the_form(Page("https://stripe.com/jobs/1?gh_jid=1", ["https://job-boards.greenhouse.io/embed/job_app?for=stripe"]), "greenhouse"))

    def test_stated_facts_answer_by_rule(self):
        """Self-identification, office reach and past employers are answered from what the applicant
        stated, and the office and employer answers depend on the posting."""
        from jobhunt.apply.answer import Answerer
        prof = self.profile
        prof["identity"].update({"preferred_name": "Alex", "pronouns": "She/Her", "address_line1": "1 Main St",
                                 "city": "Grove City", "state": "OH", "state_name": "Ohio", "postal_code": "43000"})
        prof["answers"].update({"eeo": "answer", "onsite_only_in": ["Columbus"], "past_employers": ["Globex"],
                                "eeo_self": {"gender": "Female", "race": "Asian", "hispanic": "No", "veteran": "No", "disability": "No"}})
        dc = Answerer(prof, {"company": "Scale AI", "location": "Washington, DC"})
        rva = Answerer(prof, {"company": "Globex", "location": "Columbus, OH"})
        office = self.field("Are you able to be in office 1-2x per week?")
        self.assertEqual((dc.answer(office).value, dc.answer(office).tier), ("No", "certain"))
        self.assertEqual(rva.answer(office).value, "Yes")
        norfolk = self.field("This role requires working onsite in Cleveland, OH at least three days per week. Which of the following applies?",
                             options=["I currently live nearby", "I am willing to relocate", "None of the above"])
        self.assertEqual(dc.answer(norfolk).value, "None of the above")
        hist = self.field("HISTORY WITH Globex")
        self.assertEqual(dc.answer(hist).value, "No"); self.assertEqual(rva.answer(hist).value, "Yes")
        spacex = self.field("SpaceX Employment History", options=["I have never worked for SpaceX", "I am a former SpaceX employee"])
        self.assertEqual(dc.answer(spacex).value, "I have never worked for SpaceX")
        eeo = lambda label, opts, kind="select": dc.answer(self.field(label, kind=kind, options=opts, required=False))
        self.assertEqual(eeo("Gender", ["Female", "Male", "Decline To Self Identify"]).value, "Female")
        self.assertEqual(eeo("Are you Hispanic/Latino?", ["Yes", "No", "Decline To Self Identify"]).value, "No")
        self.assertEqual(eeo("Veteran Status", ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]).value,
                         "I am not a protected veteran")
        self.assertEqual(eeo("Disability Status", ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"]).value,
                         "No, I do not have a disability")
        self.assertEqual(eeo("Two or More Races", ["Hispanic or Latino", "Asian (Not Hispanic or Latino)", "Black or African American (Not Hispanic or Latino)"], "radio").value,
                         "Asian (Not Hispanic or Latino)")
        self.assertEqual(eeo("What are your preferred pronouns?", ["She/Her/Hers", "He/Him/His", "They/Them/Theirs"], "combobox").value, "She/Her/Hers")
        self.assertEqual(dc.answer(self.field("Preferred First Name", kind="text")).value, "Alex")
        self.assertEqual(dc.answer(self.field("Zip Code", kind="text")).value, "43000")
        self.assertEqual(dc.answer(self.field("State", options=["Texas", "Ohio"])).value, "Ohio")
        self.assertEqual(dc.answer(self.field("City", kind="text")).value, "Grove City")
        prof["links"] = {"website": "none", "github": "none"}
        for label in ("Portfolio URL", "Website", "GitHub URL"):
            self.assertTrue(Answerer(prof, {}).answer(self.field(label, kind="text", required=False)).skip, label)

    def test_responses_are_validated(self):
        from jobhunt.apply import issues as issuesmod
        with self.assertRaises(ValueError):
            issuesmod.respond(self.tmp, "x", "answer", "")
        with self.assertRaises(ValueError):
            issuesmod.respond(self.tmp, "x", "delete")

    def test_verify_compares_what_a_person_would_read(self):
        from jobhunt.apply import verify
        from jobhunt.apply.schema import Field
        now = [Field("Phone", "text", value="", raw={"id": "p"}), Field("City", "combobox", value="Columbus, Ohio, United States", raw={"id": "c"}),
               Field("Langs", "checkgroup", value=["English"], raw={"name": "l"}), Field("Resume", "file", value="resume.pdf", raw={"id": "r"})]
        exp = {now[0].ident: ("Phone", "text", "555"), now[1].ident: ("City", "combobox", "Columbus, Ohio"),
               now[2].ident: ("Langs", "checkgroup", ["English"]), now[3].ident: ("Resume", "file", "resume.pdf"),
               "0:gone::text:Gone": ("Gone", "text", "x")}
        self.assertEqual(sorted(m["label"] for m in verify.check(now, exp)), ["Gone", "Phone"])
        edits = verify.window_edits([Field("Phone", "text", value="999", raw={"id": "p"})], {now[0].ident: ("Phone", "text", "555")}, set())
        self.assertEqual(edits["changed"][0]["got"], "999")



class PersonaTest(unittest.TestCase):
    """The applicant agent answers only from its knowledge base, and declines otherwise."""

    def agent(self, *replies):
        from jobhunt.apply import persona
        kb = persona.KnowledgeBase()
        kb.add("F", ["I built Beacon at Acme.", "I am a U.S. citizen."])
        seen = []
        queue = list(replies)

        def call(prompt):
            seen.append(prompt)
            return queue.pop(0) if queue else None
        return persona.ApplicantAgent(kb, "short sentences", call, name="Alex"), seen

    def test_a_grounded_choice_is_one_of_the_options(self):
        a, seen = self.agent('{"decline": false, "answer": "A United States citizen", "evidence": ["F2"]}')
        res = a.ask("Export status", "select", ["A United States citizen", "None of the above"])
        self.assertEqual((res.answer, res.evidence, res.declined), ("A United States citizen", ["F2"], False))
        self.assertEqual(len(seen), 1)            # choices need no prose check

    def test_invented_evidence_or_options_are_declines(self):
        a, _ = self.agent('{"decline": false, "answer": "Yes", "evidence": ["F9"]}')
        self.assertTrue(a.ask("Q?", "select", ["Yes", "No"]).declined)
        a, _ = self.agent('{"decline": false, "answer": "Maybe", "evidence": ["F1"]}')
        self.assertTrue(a.ask("Q?", "select", ["Yes", "No"]).declined)
        a, _ = self.agent("not json at all")
        self.assertTrue(a.ask("Q?").declined)

    def test_prose_is_checked_and_an_unsupported_claim_is_a_decline(self):
        a, _ = self.agent('{"decline": false, "answer": "I built Beacon and led a team of 40.", "evidence": ["F1"]}',
                          '{"unsupported": ["led a team of 40"], "tells": [], "revised": null}')
        res = a.ask("Tell us about leadership.")
        self.assertTrue(res.declined); self.assertIn("team of 40", res.needs)

    def test_an_unsupported_claim_is_dropped_once_and_rechecked(self):
        a, seen = self.agent('{"decline": false, "answer": "I built Beacon. It won an award.", "evidence": ["F1"]}',
                             '{"unsupported": ["It won an award"], "tells": [], "revised": "I built Beacon."}',
                             '{"unsupported": [], "tells": [], "revised": null}')
        res = a.ask("What have you built?")
        self.assertEqual(res.answer, "I built Beacon."); self.assertIn("award", res.reason)
        self.assertEqual(len(seen), 3)

    def test_prose_passes_cleaned_and_own_words_are_not_tells(self):
        a, _ = self.agent('{"decline": false, "answer": "I built Beacon \u2014 at Acme.", "evidence": ["F1"]}',
                          '{"unsupported": [], "tells": [], "revised": null}')
        res = a.ask("What have you built?")
        self.assertEqual(res.answer, "I built Beacon, at Acme.")
        self.assertEqual(res.reason, "")

    def test_the_model_can_decline(self):
        a, _ = self.agent('{"decline": true, "reason": "no reason given for Hooli", "needs": "why Hooli"}')
        res = a.ask("Why Hooli?")
        self.assertEqual((res.declined, res.needs), (True, "why Hooli"))
        self.assertTrue(res.note().startswith("declined"))

    def test_private_pay_facts_never_reach_the_agent(self):
        from jobhunt.apply import persona
        notes = ("- I built Beacon.\n- I am targeting $150,000 and would take $130,000 for a great fit.\n"
                 "- My salary floor is private.\n- I like pottery.\n")
        prof = {"answers": {"salary_expectation": "150000", "agent_private": ["pottery"]}}
        kb = persona.KnowledgeBase.build(prof, notes)
        text = kb.render()
        self.assertIn("Beacon", text); self.assertIn("salary_expectation: 150000", text)
        self.assertNotIn("130", text); self.assertNotIn("floor", text); self.assertNotIn("pottery", text)

    def test_proposals_are_stored_pending_and_shown_never_typed(self):
        from jobhunt.apply import persona
        from jobhunt.apply.answer import Answerer
        from jobhunt.apply.schema import Field
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bank = bankmod.Bank(tmp)
        e = bank.record("What have you built?", "textarea", [], True, {})
        a, _ = self.agent('{"decline": false, "answer": "I built Beacon.", "evidence": ["F1"]}',
                          '{"unsupported": [], "tells": [], "revised": null}')
        self.assertEqual(persona.propose_pending(a, bank, log=lambda s: None), {"proposed": 1, "declined": 0})
        saved = bankmod.Bank(tmp).get(e.key)
        self.assertEqual((saved.status, saved.by, saved.answer), ("pending", "applicant_agent", "I built Beacon."))
        ans = Answerer(json.loads(json.dumps(PROFILE)), {}, bank=bankmod.Bank(tmp))
        f = Field("What have you built?", "textarea", required=True)
        got = ans.answer(f)
        self.assertNotEqual(got.tier, "certain")                    # a proposal is never typed
        q = ans.ask(f, got)
        self.assertEqual((q["draft"], q["tier"]), ("I built Beacon.", "drafted"))
        bank2 = bankmod.Bank(tmp); bank2.set(e.key, status="approved"); bank2.save()
        self.assertEqual(bankmod.Bank(tmp).get(e.key).by, "")      # approving makes it yours


# ----------------------------------------------------------------------------- resume variants

from jobhunt.apply import tailor, terms as termsmod  # noqa: E402
from datetime import date  # noqa: E402

MASTER_FIX = (FIX / "resume_master.html").read_text(encoding="utf-8")


def _bank(*entries):
    return termsmod.TermBank(None, entries=list(entries))


def _syn(*terms, status="approved"):
    return termsmod.TermEntry("synonyms", " = ".join(terms[:2]), status=status, terms=list(terms))


def _skill(name, *aliases, status="approved"):
    return termsmod.TermEntry("skill", name, status=status, aliases=list(aliases))


def _headline(name, *matches, status="approved"):
    return termsmod.TermEntry("headline", name, status=status, matches=list(matches) or [name])


def _posting(title="Senior AI Engineer", desc="We build AI agents in Python. Ray experience is a plus."):
    return {"id": "greenhouse:acme:1", "company": "Acme", "title": title, "description": desc, "url": "https://x/1"}


class TermsTest(unittest.TestCase):
    def test_parse_render_round_trip(self):
        md = termsmod.render_terms([_syn("agentic system", "AI agent", "AI agents"), _skill("Python", "python3"),
                                    _headline("Forward Deployed Engineer", "forward deployed", "FDE", status="pending")])
        got = termsmod.parse_terms(md)
        self.assertEqual([(e.kind, e.name, e.status) for e in got],
                         [("synonyms", "agentic system = AI agent", "approved"), ("skill", "Python", "approved"),
                          ("headline", "Forward Deployed Engineer", "pending")])
        self.assertEqual(got[0].terms, ["agentic system", "AI agent", "AI agents"])
        self.assertEqual(got[1].members, ["Python", "python3"])
        self.assertEqual(got[2].matches, ["forward deployed", "FDE"])
        self.assertEqual(termsmod.render_terms(got), md)
        # a hand-written group without a terms line takes its members from the heading
        e = termsmod.parse_terms("## synonyms: LLM = large language model\n- status: approved\n")[0]
        self.assertEqual(e.terms, ["LLM", "large language model"])
        self.assertTrue(e.approved)

    def test_propose_dedupes_and_never_alters(self):
        b = _bank(_syn("agentic system", "AI agent"), _skill("Python"))
        self.assertIsNone(b.propose("synonyms", "AI agent = agentic system", terms=["AI agent", "agentic system"]))
        self.assertIsNone(b.propose("skill", "python"))
        self.assertIsNone(b.propose("synonyms", "x = y", terms=["x"]))          # a group needs two members
        e = b.propose("skill", "Ray", source="s", note="n")
        self.assertEqual((e.status, e.by, b.counts()), ("pending", "tailor", {"approved": 2, "pending": 1}))
        self.assertIsNone(b.propose("skill", "Ray"))
        self.assertEqual(b.entries[0].status, "approved")
        self.assertEqual(len(b.entries), 3)

    def test_approving_makes_it_yours_and_sha_tracks_approved_only(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertEqual(termsmod.read_terms(tmp), [])
        b = termsmod.TermBank(tmp)
        e = b.propose("skill", "Ray")
        before = b.sha()
        b.save()
        self.assertTrue(termsmod.terms_path(tmp).exists())
        again = termsmod.TermBank(tmp)
        self.assertEqual(again.sha(), before)                       # a pending entry does not move it
        again.set(e.key, status="approved"); again.save()
        saved = termsmod.TermBank(tmp).get(e.key)
        self.assertEqual((saved.status, saved.by), ("approved", ""))
        self.assertNotEqual(termsmod.TermBank(tmp).sha(), before)
        # a concurrent proposal from another process survives this one's save
        other = termsmod.TermBank(tmp); other.propose("skill", "Slack"); other.save()
        mine = termsmod.TermBank(tmp)
        mine.set(e.key, note="edited"); mine.save()
        self.assertEqual(sorted(x.name for x in termsmod.TermBank(tmp).entries), ["Ray", "Slack"])
        self.assertTrue(mine.remove(e.key)); mine.save()
        self.assertEqual([x.name for x in termsmod.TermBank(tmp).entries], ["Slack"])


class TailorTest(unittest.TestCase):
    def read(self, bank, **kw):
        return tailor.read_posting(_posting(**kw), bank, MASTER_FIX)

    def test_master_fixture_shape(self):
        self.assertEqual(tailor.check_shape(MASTER_FIX), [])
        bad = MASTER_FIX.replace('<p data-slot="headline"></p>', '<p data-slot="headline">x</p>')
        self.assertTrue(tailor.check_shape(bad))

    def test_term_swap_respects_word_boundaries_and_case(self):
        bank = _bank(_syn("agentic system", "AI agent"))
        p = self.read(bank)
        html, edits = tailor.apply_terms(MASTER_FIX, p, bank)
        self.assertEqual([e.op for e in edits], ["term", "term"])
        self.assertIn("Created Lantern, an AI agent for resolving", html)
        self.assertIn("Wrote the AI agent that plans", html)
        self.assertEqual(edits[0].where, "acme/bullet 1")
        self.assertEqual(edits[1].where, "globex/bullet 5")
        self.assertIn("AI agents", edits[0].quote)
        # word boundaries and case: 'Agentic systems' is not a member, 'nonagentic system' is not a word match
        src = MASTER_FIX.replace("an agentic system", "an Agentic system").replace("the agentic system", "a nonagentic system")
        html, edits = tailor.apply_terms(src, self.read(bank), bank)
        self.assertIn("an AI agent for", html)                       # first-letter case follows the source
        self.assertIn("a nonagentic system", html)
        self.assertEqual(len(edits), 1)
        # text outside bullets is never touched
        src = MASTER_FIX.replace("<h3>Senior Machine Learning Engineer, Acme", "<h3>Agentic system lead, Acme")
        html, _ = tailor.apply_terms(src, self.read(bank), bank)
        self.assertIn("<h3>Agentic system lead, Acme", html)

    def test_term_swap_only_approved_and_only_when_posting_uses_one_member(self):
        pending = _bank(_syn("agentic system", "AI agent", status="pending"))
        self.assertEqual(tailor.apply_terms(MASTER_FIX, self.read(pending), pending)[1], [])
        both = _bank(_syn("agentic system", "AI agent"))
        p = self.read(both, desc="We build AI agents and agentic systems.")
        self.assertEqual(tailor.apply_terms(MASTER_FIX, p, both)[1], [])
        none = self.read(both, desc="We build compilers.")
        self.assertEqual(tailor.apply_terms(MASTER_FIX, none, both)[1], [])
        # html entities inside a bullet survive the round trip
        src = MASTER_FIX.replace("reads context from Github", "reads context &amp; agentic system logs from Github")
        html, edits = tailor.apply_terms(src, self.read(both), both)
        self.assertIn("context &amp; AI agent logs", html)

    def test_skills_line_only_approved_vocabulary_posting_first(self):
        bank = _bank(_skill("Python", "python3"), _skill("Slack"), _skill("Ray"), _skill("Rust", status="pending"))
        p = self.read(bank)
        self.assertEqual(p.skills, ["Python", "Ray"])
        html, edits = tailor.apply_skills(MASTER_FIX, p, bank)
        self.assertIn('<section data-slot="skills"><h2>SKILLS</h2><p>Python, Ray, Slack</p></section>', html)
        self.assertEqual(edits[0].after, "Python, Ray, Slack")
        self.assertNotIn("Rust", html)
        self.assertEqual(tailor.apply_skills(MASTER_FIX, p, _bank())[1], [])
        self.assertIn('<section data-slot="skills"></section>', tailor.apply_skills(MASTER_FIX, p, _bank())[0])

    def test_headline_only_from_approved_list_matching_title(self):
        bank = _bank(_headline("AI Engineer", "AI engineer"), _headline("Senior AI Engineer", "senior AI engineer"),
                     _headline("Staff Engineer", "staff", status="pending"))
        p = self.read(bank, title="Senior AI Engineer (Agents)")
        p.model_title = "Principal Agent Architect"
        html, edits = tailor.apply_headline(MASTER_FIX, p, bank)
        self.assertIn('<p data-slot="headline">Senior AI Engineer</p>', html)      # longest match wins
        self.assertEqual(edits[0].entry, bank.entries[1].key)
        p = self.read(bank, title="Staff Engineer")
        self.assertEqual(tailor.apply_headline(MASTER_FIX, p, bank)[1], [])         # pending never applies
        p = self.read(bank, title="Data Scientist")
        self.assertEqual(tailor.apply_headline(MASTER_FIX, p, bank)[1], [])         # the model's title is never used

    def test_reorder_is_stable_single_move_and_honours_fixed(self):
        bank = _bank(_skill("Python"), _skill("Ray"))
        p = self.read(bank)                                   # posting names Python and Ray: bullet 3 of acme wins
        html, edits = tailor.reorder_bullets(MASTER_FIX, p)
        self.assertEqual([e.op for e in edits], ["reorder"])
        self.assertEqual(edits[0].where, "acme")
        self.assertEqual(tailor.bullets(html)[:3], [tailor.bullets(MASTER_FIX)[2], tailor.bullets(MASTER_FIX)[0], tailor.bullets(MASTER_FIX)[1]])
        self.assertEqual(sorted(tailor.bullets(html)), sorted(tailor.bullets(MASTER_FIX)))   # text untouched
        self.assertEqual(tailor.bullets(html)[3:], tailor.bullets(MASTER_FIX)[3:])             # data-fixed left alone
        tie = self.read(bank, desc="Slack and Github and reinforcement learning.")
        self.assertEqual(tailor.reorder_bullets(MASTER_FIX, tie)[1], [])                        # a tie keeps order
        self.assertEqual(tailor.reorder_bullets(MASTER_FIX, self.read(_bank(), desc="nothing"))[1], [])

    def test_tailor_identical_without_approved_entries(self):
        html, edits = tailor.tailor(MASTER_FIX, self.read(_bank()), _bank())
        self.assertEqual(edits, [])
        self.assertEqual(tailor.html_text(html), tailor.html_text(MASTER_FIX))

    def test_read_posting_model_proposals_are_verified(self):
        bank = _bank(_skill("Python"))
        reply = json.dumps({"title": "Senior AI Engineer", "terms": ["AI agents", "Python", "Kubernetes"],
                            "skills_mentioned": ["Python", "Go"],
                            "synonyms": [{"posting_term": "AI agents", "resume_phrase": "agentic system", "quote": "We build AI agents"},
                                         {"posting_term": "AI agents", "resume_phrase": "robot fleet", "quote": ""},
                                         {"posting_term": "Kubernetes", "resume_phrase": "Ray", "quote": ""}],
                            "skills_new": ["Ray", "Kubernetes"]})
        p = tailor.read_posting(_posting(desc="We build AI agents in Python with Ray on Kubernetes."), bank, MASTER_FIX,
                                call=lambda prompt: reply, log=lambda s: None, model="claude-fable-5-1 (low)")
        self.assertEqual((p.source, p.model, p.to_dict()["model"]), ("model", "claude-fable-5-1 (low)", "claude-fable-5-1 (low)"))
        self.assertEqual(p.skills, ["Python"])                                   # "Go" is not in the vocabulary
        self.assertEqual(p.terms, ["AI agents", "Python", "Kubernetes"])     # the model's order
        kinds = sorted((e.kind, e.name, e.status, e.by) for e in p.proposals)
        # 'Ray = Kubernetes' is nonsense, but both phrases exist, so it is Alex's to reject: that is what pending is for
        self.assertEqual(kinds, [("headline", "Senior AI Engineer", "pending", "tailor"),
                                 ("skill", "Ray", "pending", "tailor"),
                                 ("synonyms", "Ray = Kubernetes", "pending", "tailor"),
                                 ("synonyms", "agentic system = AI agents", "pending", "tailor")])
        self.assertTrue(any("robot fleet" in g for g in p.gaps), p.gaps)
        self.assertTrue(any("Kubernetes" in g for g in p.gaps), p.gaps)
        self.assertEqual(bank.counts(), {"approved": 1, "pending": 4})
        # pending proposals change no text: only the approved skill and a reorder (which needs no bank entry) apply
        html, edits = tailor.tailor(MASTER_FIX, p, bank)
        self.assertEqual(sorted(e.op for e in edits), ["reorder", "skills"])
        self.assertIn("an agentic system", html)
        self.assertIn('<p data-slot="headline"></p>', html)
        self.assertIn("<p>Python</p>", html)
        # the deterministic pass alone, and a failed call
        q = tailor.read_posting(_posting(), bank, MASTER_FIX, call=lambda prompt: None, log=lambda s: None)
        self.assertEqual((q.source, q.skills, q.proposals), ("regex", ["Python"], []))

    def test_variant_name_and_index(self):
        base = Path("/x/Pat_Example_Resume_rev_26-09-15.pdf")
        day = date(2026, 10, 20)
        name, tag = tailor.variant_name(base, "greenhouse:acme:1", day)
        self.assertEqual(len(tag), 4)
        self.assertEqual(name, f"Pat_Example_Resume_rev_26-10-20-{tag}.pdf")
        self.assertEqual(tailor.variant_name(base, "greenhouse:acme:1", day, {name: "greenhouse:acme:1"}), (name, tag))
        name2, tag2 = tailor.variant_name(base, "greenhouse:acme:1", day, {name: "someone-else"})
        self.assertEqual(len(tag2), 5)
        self.assertTrue(tag2.startswith(tag))
        self.assertEqual(tailor.prefix_for(None), "Resume")
        self.assertEqual(tailor.prefix_for(Path("Sam_Okafor_Resume.pdf")), "Sam_Okafor_Resume")
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.assertIsNone(tailor.build_variant(tmp, _posting()))                  # no master: nothing written
        self.assertFalse((tmp / "resume").exists())
        tailor.append_index(tmp, {"name": "a.pdf", "job_id": "j1"})
        tailor.append_index(tmp, {"name": "a.pdf", "job_id": "j1", "run_id": "r2"})
        self.assertEqual(tailor.lookup(tmp, "a.pdf")["run_id"], "r2")
        self.assertIsNone(tailor.lookup(tmp, "nope.pdf"))

    def test_build_variant_without_rendering_writes_the_folder_and_bank(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "resume").mkdir()
        (tmp / "resume" / "master.html").write_text(MASTER_FIX, encoding="utf-8")
        termsmod.write_terms(tmp, [_syn("agentic system", "AI agent"), _skill("Python")])
        reply = json.dumps({"title": "Senior AI Engineer", "terms": [], "skills_mentioned": [], "synonyms": [], "skills_new": ["Ray"]})
        v = tailor.build_variant(tmp, _posting(), base=Path("Pat_Example_Resume_rev_1.pdf"), call=lambda p: reply,
                                 run_id="run1", today=date(2026, 10, 20), render=False, log=lambda s: None)
        self.assertEqual(v.by_op(), {"term": 2, "skills": 1, "headline": 0, "reorder": 1})
        self.assertEqual(v.name, f"Pat_Example_Resume_rev_26-10-20-{v.tag}.pdf")
        self.assertEqual(v.folder, tmp / "resume" / "variants" / f"26-10-20-{v.tag}")
        self.assertEqual(sorted(f.name for f in v.folder.iterdir()), ["diff.md", "tweaks.json", "variant.html"])
        tw = json.loads((v.folder / "tweaks.json").read_text())
        self.assertEqual((tw["run_id"], tw["identical"], len(tw["edits"]), tw["posting"]["source"]), ("run1", False, 4, "model"))
        self.assertEqual([p["name"] for p in tw["proposals"]], ["Ray", "Senior AI Engineer"])
        diff = (v.folder / "diff.md").read_text()
        self.assertIn("acme/bullet", diff)
        self.assertIn("## Pending proposals", diff)
        self.assertIn("-Created Lantern, an agentic system", diff)
        self.assertEqual(tailor.read_index(tmp)[0]["job_id"], "greenhouse:acme:1")
        self.assertEqual(termsmod.TermBank(tmp).counts(), {"approved": 2, "pending": 2})
        self.assertIn("2 proposals pending", v.summary())
        self.assertEqual(v.record()["edits"], 4)


class TailorRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright not installed")

    def test_render_matches_text_and_keeps_links(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = tmp / "m.pdf"
        self.assertGreaterEqual(tailor.render_pdf(MASTER_FIX, out), 1)
        self.assertEqual(tailor.text_norm(tailor.pdf_text(out)), tailor.text_norm(tailor.html_text(MASTER_FIX)))
        self.assertEqual(tailor.pdf_uris(out), {"https://example.com/portfolio"})

    def test_build_variant_end_to_end(self):
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "resume").mkdir()
        (tmp / "resume" / "master.html").write_text(MASTER_FIX, encoding="utf-8")
        termsmod.write_terms(tmp, [_skill("Python")])
        v = tailor.build_variant(tmp, _posting(), base=Path("Pat_Example_Resume.pdf"), today=date(2026, 10, 20), log=lambda s: None)
        self.assertTrue(v.pdf.exists())
        self.assertEqual(v.pages, 1)
        self.assertIn("SKILLS", tailor.pdf_text(v.pdf))
        self.assertEqual(sorted(f.name for f in v.folder.iterdir()), sorted([v.name, "diff.md", "tweaks.json", "variant.html"]))
        # a variant that spills past the master's page count is not offered
        (tmp / "resume" / "transcription.json").write_text(json.dumps({"pages": 0}))
        self.assertIsNone(tailor.build_variant(tmp, _posting(), base=Path("Pat_Example_Resume.pdf"), log=lambda s: None))
        self.assertTrue(tailor.read_index(tmp)[-1]["overflow"])

    def test_master_matches_original(self):
        """The real profile's transcription against its base PDF: text, page count and link annotations. Both files
        are gitignored, so this runs only where they exist; once the master moves past the transcription
        (once edited), it skips and `resume check` shows the diff instead."""
        real = [d for d in sorted((ROOT / "profiles").iterdir())
                if d.is_dir() and not d.name.startswith("example-") and (d / "resume" / "master.html").exists()]
        if not real:
            raise unittest.SkipTest("no real profile with a master resume on this machine")
        prof = real[0]
        master = tailor.load_master(prof)
        try:
            base = load_profile(ROOT, prof.name, SHARED).resume_path
        except Exception:  # noqa: BLE001
            base = None
        if master is None or not (base and base.exists()):
            raise unittest.SkipTest("no real master or base resume on this machine")
        pin = prof / "resume" / tailor.TRANSCRIPTION
        if pin.exists() and json.loads(pin.read_text()).get("master_sha") != master.sha:
            raise unittest.SkipTest("master has moved past the transcription; `jobhunt.py resume check` shows the diff")
        self.assertEqual(tailor.check_shape(master.html), [])
        self.assertEqual(tailor.text_norm(tailor.html_text(master.html)), tailor.text_norm(tailor.pdf_text(base)))
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = tmp / "m.pdf"
        self.assertEqual(tailor.render_pdf(master.html, out), tailor.pdf_pages(base))
        self.assertEqual(tailor.text_norm(tailor.pdf_text(out)), tailor.text_norm(tailor.pdf_text(base)))
        self.assertEqual(tailor.pdf_uris(out), tailor.pdf_uris(base))


if __name__ == "__main__":
    unittest.main()
