"""Resolve: an aggregator listing (The Muse, Hacker News, RemoteOK, ...) is not an application form; the
form is one or two links away on the employer's site. Follow the listing's outbound Apply link (or, for a
Hacker News post, the links in its text), and if that lands on a careers index rather than the posting,
pick the link that best matches the posting's title: deterministically first, the model choosing among
the candidates only as a fallback. The result is cached per posting.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

AGGREGATORS = ("themuse.com", "remoteok.com", "remoteok.io", "remotive.com", "news.ycombinator.com", "builtin.com",
               "weworkremotely.com", "wellfound.com", "ycombinator.com/companies")
_APPLY_TEXT = re.compile(r"\bapply\b|view (the )?job|company site|original (post|listing)|see (the )?job", re.I)
_SKIP_HOST = re.compile(r"ycombinator\.com|themuse\.com|remoteok|remotive|builtin\.com|weworkremotely|linkedin\.com|"
                        r"indeed\.com|twitter\.com|x\.com|facebook\.com|google\.|glassdoor", re.I)

LINKS_JS = """() => Array.from(document.querySelectorAll('a[href]')).filter(a => a.offsetWidth || a.offsetHeight)
  .map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
              href: a.href}))"""


def is_aggregator(url: str) -> bool:
    u = (url or "").lower()
    return any(a in u for a in AGGREGATORS)


def _tokens(s: str) -> set:
    stop = {"the", "and", "for", "with", "senior", "sr", "staff", "remote", "engineer", "of", "a", "an", "to", "in"}
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in stop and len(w) > 1}


def _best_by_title(links: list[dict], title: str) -> Optional[str]:
    want = _tokens(title)
    if not want:
        return None
    scored = []
    for ln in links:
        got = _tokens(ln["text"]) | _tokens(urlparse(ln["href"]).path.replace("-", " ").replace("_", " "))
        score = len(want & got) / len(want)
        if score >= 0.6:
            scored.append((score, ln["href"]))
    scored.sort(reverse=True)
    return scored[0][1] if scored else None


def _is_form(page) -> bool:
    from .forms import SUPPORTED, detect_ats, embedded_ats
    from .read import looks_like_application, read_form
    if detect_ats(page.url) in SUPPORTED + ("workday",) or embedded_ats(page):
        return True
    try:
        return looks_like_application(read_form(page))
    except Exception:  # noqa: BLE001
        return False


def _goto(page, url: str) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        return True
    except Exception:  # noqa: BLE001
        return False


def resolve(page, job: dict, log: Callable[[str], None] = print, call: Optional[Callable[[str], Optional[str]]] = None,
            aggregator: Optional[bool] = None) -> tuple[Optional[str], str]:
    """(url of the page to apply on, how it was found) or (None, why not). Leaves `page` on the result."""
    start = job.get("apply_url") or job.get("url") or ""
    if not (is_aggregator(start) if aggregator is None else aggregator):
        return start, "direct"
    if "ycombinator.com" in start:
        text = job.get("description") or ""
        cands = [u.rstrip(".,)") for u in re.findall(r"https?://[^\s<>\"']+", text) if not _SKIP_HOST.search(u)]
        how = "a link in the Hacker News post"
    else:
        if not _goto(page, start):
            return None, f"could not open the listing {start}"
        links = page.evaluate(LINKS_JS)
        here = urlparse(page.url)
        applies = [ln["href"] for ln in links if _APPLY_TEXT.search(ln["text"]) and ln["href"].startswith(("http", "file"))
                   and ln["href"].split("#")[0] != page.url.split("#")[0]]
        offsite = [u for u in applies if (urlparse(u).netloc != here.netloc or here.scheme == "file") and not _SKIP_HOST.search(u)]
        # a same-site redirect (remoteok.com/l/123) leaves the site once followed
        redirects = [u for u in applies if urlparse(u).netloc == here.netloc and here.scheme != "file"]
        cands = offsite + redirects
        how = "the listing's Apply link"
        if not cands:
            opened = _click_apply_button(page)       # The Muse: a button that opens the employer's site from script
            if opened:
                cands, how = [opened], "the listing's Apply button"
    seen = []
    for url in dict.fromkeys(cands):
        if len(seen) >= 3:
            break
        seen.append(url)
        if not _goto(page, url) or is_aggregator(page.url):
            continue                  # still on an aggregator (a redirect that did not leave): not a result
        if _is_form(page) or _apply_button(page):
            return page.url, how
        links = [ln for ln in page.evaluate(LINKS_JS) if not is_aggregator(ln["href"]) and not _SKIP_HOST.search(ln["href"])]
        pick = _best_by_title(links, job.get("title") or "")
        pick_how = "the careers page link matching the title"
        if pick is None and call is not None:
            pick, pick_how = _ask(call, links, job), "the careers page link the model chose"
        if pick and _goto(page, urljoin(page.url, pick)) and not is_aggregator(page.url):
            if _is_form(page) or _apply_button(page):
                return page.url, f"{how}, then {pick_how}"
    return None, (f"no application form behind {how} ({len(seen)} link(s) tried)" if seen else f"no {how} found")


def _click_apply_button(page) -> Optional[str]:
    """Click a visible Apply button and return the URL it opens (a new tab or this one), or None."""
    try:
        btns = page.get_by_role("button", name=_APPLY_TEXT)
        n = min(btns.count(), 4)
    except Exception:  # noqa: BLE001
        return None
    before = page.url
    for i in range(n):
        b = btns.nth(i)
        try:
            if not b.is_visible():
                continue
            try:
                with page.context.expect_page(timeout=8000) as pi:
                    b.click(timeout=3000)
                new = pi.value
                new.wait_for_load_state("domcontentloaded", timeout=15000)
                url = new.url
                new.close()
                if url and url != "about:blank":
                    return url
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(1500)
                if page.url != before:
                    return page.url
        except Exception:  # noqa: BLE001
            continue
    return None


def _apply_button(page) -> bool:
    """A posting page with an Apply button still counts: the navigator (navigate.py) clicks through it."""
    try:
        return bool(page.get_by_role("button", name=re.compile(r"^\s*apply", re.I)).count()
                    or page.get_by_role("link", name=re.compile(r"^\s*apply", re.I)).count())
    except Exception:  # noqa: BLE001
        return False


def _ask(call, links: list[dict], job: dict) -> Optional[str]:
    rows = [ln for ln in links if ln["text"]][:80]
    if not rows:
        return None
    prompt = (f"Which link on this careers page leads to the posting for \"{job.get('title')}\" at {job.get('company')}? "
              "Reply with JSON only: {\"index\": <number>} or {\"index\": null} if none does.\n\n"
              + "\n".join(f"{i}. {ln['text']} -> {ln['href'][:120]}" for i, ln in enumerate(rows)))
    try:
        m = re.search(r"\{.*\}", call(prompt) or "", re.S)
        i = json.loads(m.group(0)).get("index") if m else None
        return rows[int(i)]["href"] if i is not None and 0 <= int(i) < len(rows) else None
    except Exception:  # noqa: BLE001
        return None


class Cache:
    def __init__(self, path: Optional[Path]):
        self.path = Path(path) if path else None
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path and self.path.exists() else {}

    def get(self, job_id: str) -> Optional[dict]:
        return self.data.get(job_id)

    def put(self, job_id: str, url: Optional[str], how: str) -> None:
        if not self.path:
            return
        self.data[job_id] = {"url": url, "how": how, "at": datetime.now().isoformat(timespec="seconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)
