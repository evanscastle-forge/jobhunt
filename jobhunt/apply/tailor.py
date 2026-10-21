"""One resume variant per posting (docs/apply-design.md, "The resume: one variant per posting").

Automated screens match a resume against the posting's own words. When you click Apply, the run
builds a PDF of your resume keyed to that posting's vocabulary and uploads it in place of the base
file. The content stays yours: exactly four operations, each only from an entry you approved in
resume/terms.md (terms.py):

    term alignment   a phrase in a bullet swapped for the posting's word for the same thing
    skills line      a SKILLS section of your approved skills, the posting's first
    headline         a title you have held, under your name, when it matches the posting's title
    bullet order     within one job, the bullet most relevant to the posting moved first

Nothing else: no new sentences, numbers or claims. The model may *propose* a synonym group, a skill
or a headline; a proposal is pending until approved and is stored only when the phrase is already on
your resume. Early variants may be identical in text to the master; only the file name differs.

The source is resume/master.html (transcribed from the base PDF, pinned by a test). Everything a
build produces lives under resume/ for review:

    resume/master.html                       the source; yours to edit
    resume/master.pdf, transcription.json    `jobhunt.py resume check [--pin]`
    resume/terms.md                          the bank
    resume/index.jsonl                       one line per build: name -> posting, run, what changed
    resume/variants/<YY-MM-DD>-<tag>/        the PDF, variant.html, diff.md, tweaks.json

The file name <Prefix>_rev_<YY-MM-DD>-<tag>.pdf looks like a revision; the tag comes from the posting
id, so a recruiter's copy traces back to its posting through the index.
"""
from __future__ import annotations

import difflib
import hashlib
import html as htmlmod
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

from .terms import TermBank, TermEntry, resume_dir

MASTER = "master.html"
MASTER_PDF = "master.pdf"
INDEX = "index.jsonl"
VARIANTS = "variants"
TRANSCRIPTION = "transcription.json"
DEFAULT_PAGES = 2
DESC_CHARS = 6000
MAX_TERMS = 25
OPS = ("term", "skills", "headline", "reorder")

# the master's microformat (a test pins it)
_SLOT_HEADLINE = re.compile(r'(<p\b[^>]*data-slot="headline"[^>]*>)(.*?)(</p>)', re.S)
_SLOT_SKILLS = re.compile(r'(<section\b[^>]*data-slot="skills"[^>]*>)(.*?)(</section>)', re.S)
_ARTICLE = re.compile(r'<article\b[^>]*data-job="([^"]*)"[^>]*>.*?</article>', re.S)
_UL = re.compile(r"(<ul\b[^>]*>)(.*?)(</ul>)", re.S)
_LI = re.compile(r"(<li\b[^>]*>)(.*?)(</li>)", re.S)
_TAG_SPLIT = re.compile(r"(<[^>]+>)")
_BULLET_GLYPHS = "●•·▪◦‣-–"


def _sha(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def word_re(term: str, plural: bool = False) -> re.Pattern:
    """Word-boundary, case-insensitive match for a phrase (the shape of filters._term_regex). With
    plural, 'AI agent' also matches 'AI agents': a posting is read loosely, the resume is edited strictly."""
    t = " ".join(term.split()).lower()
    esc = re.escape(t).replace(r"\ ", r"\s+")
    left = r"(?<!\w)" if re.match(r"\w", t) else ""
    right = ((r"(?:e?s)?" if plural else "") + r"(?!\w)") if re.search(r"\w$", t) else ""
    return re.compile(left + esc + right, re.I)


def _json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def posting_sha(job: dict) -> str:
    return hashlib.sha1(f"{job.get('title') or ''}\n{job.get('company') or ''}\n"
                        f"{(job.get('description') or '')[:3000]}".encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------- text helpers

class _Text(HTMLParser):
    BLOCKS = {"h1", "h2", "h3", "h4", "p", "li", "section", "article", "header", "div", "ul", "br", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "title", "head"):
            self.skip += 1
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "title", "head"):
            self.skip = max(0, self.skip - 1)
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    """The text of the page, one line per block: what diff.md shows and the fast fidelity test reads."""
    p = _Text()
    p.feed(html or "")
    lines = [" ".join(l.split()) for l in "".join(p.parts).splitlines()]
    return "\n".join(l for l in lines if l)


def text_norm(s: str) -> str:
    """Comparison form: NFKC, bullet glyphs and all whitespace dropped. 'Modulo whitespace'."""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(rf"(?m)^\s*[{re.escape(_BULLET_GLYPHS)}]\s*", "", s)
    return re.sub(r"\s+", "", s)


def pdf_text(path) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return ""


def pdf_pages(path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return 0


def pdf_uris(path) -> set[str]:
    out: set[str] = set()
    try:
        from pypdf import PdfReader
        for pg in PdfReader(str(path)).pages:
            for a in pg.get("/Annots") or []:
                try:
                    uri = a.get_object().get("/A", {}).get("/URI")
                except Exception:  # noqa: BLE001
                    uri = None
                if uri:
                    out.add(str(uri))
    except Exception:  # noqa: BLE001
        pass
    return out


def _unescape(s: str) -> str:
    return htmlmod.unescape(s)


def _escape(s: str) -> str:
    return htmlmod.escape(s, quote=False)


def _li_text(inner: str) -> str:
    return " ".join(_unescape(_TAG_SPLIT.sub(lambda m: "" if m.group(0).startswith("<") else m.group(0), inner)).split())


def bullets(html: str) -> list[str]:
    """Every bullet's text, in page order."""
    return [_li_text(m.group(2)) for m in _LI.finditer(html or "")]


# ----------------------------------------------------------------------------- the master

@dataclass
class Master:
    html: str
    path: Path
    sha: str


def master_path(profile_dir: Path) -> Path:
    return resume_dir(profile_dir) / MASTER


def load_master(profile_dir: Path) -> Optional[Master]:
    p = master_path(profile_dir)
    if not p.exists():
        return None
    html = p.read_text(encoding="utf-8")
    return Master(html=html, path=p, sha=_sha(html))


def check_shape(html: str) -> list[str]:
    """What is wrong with a master's microformat (empty = fine)."""
    problems = []
    for name, rx in (("headline", _SLOT_HEADLINE), ("skills", _SLOT_SKILLS)):
        hits = rx.findall(html)
        if len(hits) != 1:
            problems.append(f"expected exactly one data-slot=\"{name}\" element, found {len(hits)}")
        elif hits[0][1].strip():
            problems.append(f"the {name} slot must be empty in the master")
    if not _ARTICLE.search(html):
        problems.append("no <article data-job=...> blocks")
    for m in _LI.finditer(html):
        inner = m.group(2)
        if "<ul" in inner or "<li" in inner:
            problems.append("nested lists are not allowed inside <li>")
            break
        if re.search(r"<(?!/?(a|b|i|strong|em)\b)[a-z]", inner):
            problems.append(f"only <a>, <b>, <i> may appear inside a bullet: {_li_text(inner)[:50]!r}")
            break
    return problems


# ----------------------------------------------------------------------------- the posting

@dataclass
class Posting:
    title: str = ""                 # the posting's title, as fetched (what headlines match)
    text: str = ""                  # title + description, what terms are tested against
    terms: list = field(default_factory=list)      # phrases the posting uses, deduped, posting order
    skills: list = field(default_factory=list)     # approved skills the posting names, bank spelling
    quotes: dict = field(default_factory=dict)     # term (lower) -> ~120 chars of context
    source: str = "regex"           # "regex" | "model"
    model: str = ""                 # which model read it, e.g. "claude-fable-5-1 (low)"; "" for regex
    model_title: str = ""
    proposals: list = field(default_factory=list)  # TermEntry objects added to the bank, pending
    gaps: list = field(default_factory=list)       # what the model saw that the resume does not say

    def uses(self, phrase: str) -> bool:
        return bool(phrase and word_re(phrase, plural=True).search(self.text))

    def quote(self, phrase: str) -> str:
        q = self.quotes.get((phrase or "").lower())
        if q is None:
            q = _context(self.text, phrase)
            self.quotes[(phrase or "").lower()] = q
        return q

    def _add_term(self, term: str) -> None:
        t = " ".join((term or "").split())
        if t and t.lower() not in {x.lower() for x in self.terms}:
            self.terms.append(t)

    def to_dict(self) -> dict:
        return {"title": self.title, "model_title": self.model_title, "terms": list(self.terms),
                "skills": list(self.skills), "source": self.source, "model": self.model}


def _context(text: str, phrase: str, width: int = 120) -> str:
    m = word_re(phrase, plural=True).search(text or "") if phrase else None
    if not m:
        return ""
    a, b = max(0, m.start() - width // 2), min(len(text), m.end() + width // 2)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


def _prompt(job: dict, bank: TermBank, master_html: str) -> str:
    vocab = ", ".join(e.name for e in bank.approved("skill")) or "(none yet)"
    bl = "\n".join(f"- {b}" for b in bullets(master_html))
    desc = " ".join((job.get("description") or "").split())[:DESC_CHARS]
    return (
        "You are matching a resume's wording to a job posting so automated screens find what is already there. "
        "You must not invent, strengthen or add any claim about the applicant.\n\n"
        f"POSTING TITLE: {job.get('title') or ''}\nCOMPANY: {job.get('company') or ''}\n\nPOSTING:\n{desc}\n\n"
        f"APPROVED SKILLS VOCABULARY (use exactly this spelling): {vocab}\n\n"
        f"RESUME BULLETS (verbatim):\n{bl}\n\n"
        "Reply with JSON only, no prose:\n"
        "{\"title\": \"the role's generic title as a one-line headline (e.g. 'Forward Deployed Engineer'), without team, "
        "department, location or bracketed qualifiers\",\n"
        f" \"terms\": [\"up to {MAX_TERMS} short phrases the posting uses for skills, technologies, practices and "
        "responsibilities, spelled exactly as the posting spells them\"],\n"
        " \"skills_mentioned\": [\"the subset of the approved vocabulary the posting names or clearly asks for, in the vocabulary's spelling\"],\n"
        " \"synonyms\": [{\"posting_term\": \"a phrase copied from the posting\", \"resume_phrase\": \"a phrase copied verbatim from one resume bullet that means the same thing\", \"quote\": \"the posting sentence containing posting_term\"}],\n"
        " \"skills_new\": [\"skills the posting names that appear verbatim in the resume bullets but are not in the vocabulary\"]}\n\n"
        "Rules: resume_phrase must appear verbatim in a bullet; posting_term must appear verbatim in the posting; pair phrases "
        "only when swapping one for the other would not change what the applicant claims; when in doubt leave it out. "
        "An empty list is a fine answer.")


def read_posting(job: dict, bank: TermBank, master_html: str, call: Optional[Callable[[str], Optional[str]]] = None,
                 log: Callable[[str], None] = print, model: str = "") -> Posting:
    """What the posting says, in the bank's terms. The deterministic pass always runs; the model adds to it
    and may propose bank entries, which stay pending. A failed call changes nothing."""
    p = Posting(title=" ".join((job.get("title") or "").split()),
                text=f"{job.get('title') or ''}\n{job.get('description') or ''}")
    master_text = html_text(master_html)
    for e in bank.approved("synonyms"):
        for t in e.terms:
            if p.uses(t):
                p._add_term(t)
                p.quote(t)
    for e in bank.approved("skill"):
        for m in e.members:
            if p.uses(m):
                if e.name not in p.skills:
                    p.skills.append(e.name)
                p.quote(m)
                break
    if call is None:
        return p
    reply = _json(call(_prompt(job, bank, master_html)))
    if not reply:
        log("resume: the model gave no usable answer; using the bank alone")
        return p
    p.source = "model"
    p.model = model
    p.model_title = " ".join(str(reply.get("title") or "").split())[:80]
    for t in (reply.get("terms") or [])[:MAX_TERMS]:
        if isinstance(t, str) and p.uses(t):
            p._add_term(t)
    vocab = {e.name.lower(): e for e in bank.approved("skill")}
    for s in reply.get("skills_mentioned") or []:
        e = vocab.get(str(s).strip().lower()) if isinstance(s, str) else None
        if e is not None and e.name not in p.skills and any(p.uses(m) for m in e.members):
            p.skills.append(e.name)
    source = f"{job.get('id') or ''} · {job.get('company') or ''} · {p.title} · {date.today().isoformat()}"
    on_resume = lambda phrase: bool(phrase) and bool(word_re(phrase).search(master_text))
    for s in reply.get("synonyms") or []:
        if not isinstance(s, dict):
            continue
        pt, rp = " ".join(str(s.get("posting_term") or "").split()), " ".join(str(s.get("resume_phrase") or "").split())
        if not pt or not rp or pt.lower() == rp.lower():
            continue
        if not p.uses(pt):
            continue
        if not on_resume(rp):
            p.gaps.append(f"{pt!r} (the model paired it with {rp!r}, which is not on the resume)")
            continue
        e = bank.propose("synonyms", f"{rp} = {pt}", terms=[rp, pt], source=source,
                         note=f"posting: \"{str(s.get('quote') or p.quote(pt))[:160]}\" · resume: \"{_bullet_with(master_html, rp)[:160]}\"")
        if e is not None:
            p.proposals.append(e)
    for s in reply.get("skills_new") or []:
        if not isinstance(s, str) or not s.strip():
            continue
        s = " ".join(s.split())
        if not p.uses(s):
            continue
        if not on_resume(s):
            p.gaps.append(f"{s!r} (named by the posting, not on the resume)")
            continue
        e = bank.propose("skill", s, source=source, note=f"posting: \"{p.quote(s)[:160]}\"")
        if e is not None:
            p.proposals.append(e)
    if p.model_title and not _headline_for(p, bank):
        e = bank.propose("headline", p.model_title, matches=[p.model_title], source=source,
                         note=f"posting title: \"{p.title}\". Approve only if this is a title you have held or an honest equivalent.")
        if e is not None:
            p.proposals.append(e)
    return p


def _bullet_with(master_html: str, phrase: str) -> str:
    rx = word_re(phrase)
    return next((b for b in bullets(master_html) if rx.search(b)), "")


# ----------------------------------------------------------------------------- the four operations

@dataclass
class Edit:
    op: str
    where: str
    before: str
    after: str
    why: str
    quote: str = ""
    entry: str = ""

    def to_dict(self) -> dict:
        return {"op": self.op, "where": self.where, "before": self.before, "after": self.after,
                "why": self.why, "quote": self.quote, "entry": self.entry}


def _match_case(src: str, repl: str) -> str:
    if src[:1].isupper() and not repl[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def _swap_table(posting: Posting, bank: TermBank) -> list[tuple[str, str, TermEntry]]:
    """(source phrase on the resume, target phrase the posting uses, group), longest source first."""
    out = []
    taken: set[str] = set()
    for e in bank.approved("synonyms"):
        used = [t for t in e.terms if posting.uses(t)]
        if len(used) != 1:
            continue             # the posting uses none, or more than one: nothing to key to
        target = used[0]
        for src in e.terms:
            if src.lower() != target.lower() and src.lower() not in taken:
                taken.add(src.lower())
                out.append((src, target, e))
    out.sort(key=lambda x: -len(x[0]))
    return out


def _articles(html: str):
    """(job slug, span) for every article, so an edit can say where it happened."""
    return [(m.group(1), m.span()) for m in _ARTICLE.finditer(html)]


def _where(html: str, pos: int, n: int) -> str:
    for slug, (a, b) in _articles(html):
        if a <= pos < b:
            return f"{slug}/bullet {n}"
    return f"bullet {n}"


def apply_terms(html: str, posting: Posting, bank: TermBank) -> tuple[str, list[Edit]]:
    table = _swap_table(posting, bank)
    if not table:
        return html, []
    by_src = {src.lower(): (target, e) for src, target, e in table}
    alt = "|".join(re.escape(" ".join(src.split()).lower()).replace(r"\ ", r"\s+") for src, _, _ in table)
    rx = re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.I)
    edits: list[Edit] = []
    counter = {"n": 0}

    def swap_li(m: re.Match) -> str:
        counter["n"] += 1
        inner = m.group(2)
        hits: list[tuple[str, str, TermEntry]] = []

        def seg(s: str) -> str:
            if s.startswith("<"):
                return s
            text = _unescape(s)

            def rep(mm: re.Match) -> str:
                key = " ".join(mm.group(0).split()).lower()
                target, e = by_src[key]
                hits.append((mm.group(0), target, e))
                return _match_case(mm.group(0), target)
            return _escape(rx.sub(rep, text))
        new_inner = "".join(seg(s) for s in _TAG_SPLIT.split(inner))
        if not hits:
            return m.group(0)
        for src, target, e in hits:
            edits.append(Edit("term", _where(html, m.start(), counter["n"]), _li_text(inner), _li_text(new_inner),
                              f"the posting says {target!r}; the resume said {src!r} (approved group {e.name!r})",
                              posting.quote(target), e.key))
        return m.group(1) + new_inner + m.group(3)
    return _LI.sub(swap_li, html), edits


def apply_skills(html: str, posting: Posting, bank: TermBank) -> tuple[str, list[Edit]]:
    approved = bank.approved("skill")
    m = _SLOT_SKILLS.search(html)
    if not approved or m is None:
        return html, []
    first = [s for s in posting.skills if any(e.name == s for e in approved)]
    rest = [e.name for e in approved if e.name not in first]
    order = first + rest
    line = ", ".join(order)
    filled = m.group(1) + "<h2>SKILLS</h2><p>" + _escape(line) + "</p>" + m.group(3)
    why = (f"{len(first)} of the posting's terms first ({', '.join(first)}), then the rest of your approved skills"
           if first else "your approved skills, in bank order (the posting names none of them)")
    quote = posting.quote(first[0]) if first else ""
    return html[:m.start()] + filled + html[m.end():], [Edit("skills", "skills", "", line, why, quote)]


def _headline_for(posting: Posting, bank: TermBank) -> Optional[TermEntry]:
    best = None
    for i, e in enumerate(bank.approved("headline")):
        for phrase in e.matches:
            if phrase and word_re(phrase).search(posting.title or ""):
                cand = (-len(phrase), i, e)
                if best is None or cand < best:
                    best = cand
    return best[2] if best else None


def apply_headline(html: str, posting: Posting, bank: TermBank) -> tuple[str, list[Edit]]:
    m = _SLOT_HEADLINE.search(html)
    e = _headline_for(posting, bank)
    if m is None or e is None:
        return html, []
    filled = m.group(1) + _escape(e.name) + m.group(3)
    return html[:m.start()] + filled + html[m.end():], [
        Edit("headline", "headline", "", e.name, f"the posting's title is {posting.title!r} (approved headline {e.name!r})",
             posting.title, e.key)]


def reorder_bullets(html: str, posting: Posting) -> tuple[str, list[Edit]]:
    phrases = [t for t in posting.terms] + [s for s in posting.skills]
    if not phrases:
        return html, []
    rxs = [word_re(p) for p in phrases]
    edits: list[Edit] = []

    def score(text: str) -> int:
        return sum(1 for rx in rxs if rx.search(text))

    def fix_article(am: re.Match) -> str:
        block = am.group(0)
        slug = am.group(1)

        def fix_ul(um: re.Match) -> str:
            if "data-fixed" in um.group(1):
                return um.group(0)
            items = list(_LI.finditer(um.group(2)))
            if len(items) < 2:
                return um.group(0)
            scores = [score(_li_text(i.group(2))) for i in items]
            best = max(range(len(items)), key=lambda i: (scores[i], -i))
            if best == 0 or scores[best] <= scores[0]:
                return um.group(0)
            order = [best] + [i for i in range(len(items)) if i != best]
            inner = um.group(2)
            head = inner[:items[0].start()]
            seps = [inner[items[i].end():items[i + 1].start()] for i in range(len(items) - 1)]
            tail = inner[items[-1].end():]
            parts = [head]
            for n, i in enumerate(order):
                parts.append(items[i].group(0))
                if n < len(seps):
                    parts.append(seps[n])
            parts.append(tail)
            edits.append(Edit("reorder", slug, _li_text(items[0].group(2)), _li_text(items[best].group(2)),
                              f"bullet {best + 1} names {scores[best]} of the posting's terms; bullet 1 named {scores[0]}",
                              posting.quote(next((p for p, rx in zip(phrases, rxs) if rx.search(_li_text(items[best].group(2)))), ""))))
            return um.group(1) + "".join(parts) + um.group(3)
        return _UL.sub(fix_ul, block)
    return _ARTICLE.sub(fix_article, html), edits


def tailor(master_html: str, posting: Posting, bank: TermBank) -> tuple[str, list[Edit]]:
    """The four operations, in order: reorder first (so a swapped bullet is scored as written), then
    headline, skills, terms."""
    html, edits = reorder_bullets(master_html, posting)
    for fn in (apply_headline, apply_skills, apply_terms):
        html, more = fn(html, posting, bank)
        edits.extend(more)
    return html, edits


# ----------------------------------------------------------------------------- naming, folders, index

def prefix_for(base: Optional[Path]) -> str:
    stem = Path(base).stem if base else ""
    stem = re.sub(r"_rev_.*$", "", stem, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return stem or "Resume"


def variant_name(base: Optional[Path], job_id: str, day: date, taken: Optional[dict] = None) -> tuple[str, str]:
    """(file name, tag). The tag is the first four hex digits of sha1(job id), longer only when the
    index already holds that name for a different posting on the same day."""
    taken = taken or {}
    full = hashlib.sha1((job_id or "").encode("utf-8")).hexdigest()
    prefix = prefix_for(base)
    for n in range(4, len(full) + 1):
        tag = full[:n]
        name = f"{prefix}_rev_{day:%y-%m-%d}-{tag}.pdf"
        if name not in taken or taken[name] == job_id:
            return name, tag
    return f"{prefix}_rev_{day:%y-%m-%d}-{full}.pdf", full


def index_path(profile_dir: Path) -> Path:
    return resume_dir(profile_dir) / INDEX


def read_index(profile_dir: Path) -> list[dict]:
    p = index_path(profile_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append_index(profile_dir: Path, line: dict) -> None:
    p = index_path(profile_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def lookup(profile_dir: Path, name: str) -> Optional[dict]:
    """The newest index line for a variant file name (what the dashboard serves from)."""
    rows = [r for r in read_index(profile_dir) if r.get("name") == name]
    return rows[-1] if rows else None


def baseline_pages(profile_dir: Path) -> int:
    p = resume_dir(profile_dir) / TRANSCRIPTION
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get("pages")
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_PAGES
    return v if isinstance(v, int) and v >= 0 else DEFAULT_PAGES


# ----------------------------------------------------------------------------- render

def render_pdf(html: str, out: Path) -> int:
    """HTML -> PDF in a headless bundled Chromium of its own (page.pdf needs headless, and the apply run's
    browser is a headed persistent Chrome). Returns the page count."""
    from playwright.sync_api import sync_playwright
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        try:
            pg = b.new_page()
            pg.set_content(html, wait_until="load")
            pg.emulate_media(media="print")
            pg.pdf(path=str(out), format="Letter", prefer_css_page_size=True, print_background=True)
        finally:
            b.close()
    return pdf_pages(out)


# ----------------------------------------------------------------------------- a variant

@dataclass
class Variant:
    name: str
    tag: str
    pdf: Path
    folder: Path
    edits: list
    posting: Posting
    master_sha: str
    bank_sha: str
    pages: int = 0
    overflow: bool = False

    @property
    def identical(self) -> bool:
        return not self.edits

    def by_op(self) -> dict:
        return {op: sum(1 for e in self.edits if e.op == op) for op in OPS}

    def summary(self) -> str:
        if self.identical:
            s = "identical in text to the master"
        else:
            parts = []
            b = self.by_op()
            if b["term"]:
                parts.append(f"{b['term']} term{'s' if b['term'] != 1 else ''}")
            if b["skills"]:
                parts.append("skills line")
            if b["headline"]:
                parts.append("headline")
            if b["reorder"]:
                parts.append(f"{b['reorder']} reorder{'s' if b['reorder'] != 1 else ''}")
            s = f"{len(self.edits)} edit{'s' if len(self.edits) != 1 else ''} ({', '.join(parts)})"
        if self.posting.proposals:
            s += f", {len(self.posting.proposals)} proposal{'s' if len(self.posting.proposals) != 1 else ''} pending"
        return s

    def record(self) -> dict:
        """What run.json keeps."""
        return {"name": self.name, "tag": self.tag, "pdf": str(self.pdf), "folder": str(self.folder),
                "edits": len(self.edits), "by_op": self.by_op(), "proposals": len(self.posting.proposals),
                "identical": self.identical, "source": self.posting.source, "model": self.posting.model,
                "master_sha": self.master_sha,
                "bank_sha": self.bank_sha, "pages": self.pages, "summary": self.summary()}


def diff_md(job: dict, v: Variant, master_html: str, variant_html: str, run_id: str) -> str:
    L = [f"# {v.name}", "",
         f"Posting: **{job.get('company') or ''}** · {job.get('title') or ''}  ",
         f"<{job.get('url') or ''}>  ",
         f"id `{job.get('id') or ''}`" + (f" · run `{run_id}`" if run_id else ""), "",
         f"Built {datetime.now(timezone.utc).replace(microsecond=0).isoformat()} from master `{v.master_sha}`, "
         f"bank `{v.bank_sha}`, posting read by {v.posting.source}"
         + (f" ({v.posting.model})" if v.posting.model else "") + ".", ""]
    L.append("## What changed")
    L.append("")
    if not v.edits:
        L.append("Identical in text to the master; only the file name differs.")
    for e in v.edits:
        if e.op == "term":
            L.append(f"- **{e.where}**: {e.why}.")
            L.append(f"  - was: {e.before}")
            L.append(f"  - now: {e.after}")
        elif e.op == "skills":
            L.append(f"- **SKILLS line**: {e.after}")
            L.append(f"  - {e.why}")
        elif e.op == "headline":
            L.append(f"- **Headline**: {e.after}")
            L.append(f"  - {e.why}")
        else:
            L.append(f"- **{e.where}**: moved first: {e.after}")
            L.append(f"  - {e.why}")
        if e.quote:
            L.append(f"  - posting: \"{e.quote}\"")
    L.append("")
    if v.posting.proposals:
        L.append("## Pending proposals from this posting")
        L.append("")
        L.append("Approve on the Answers page, or `jobhunt.py resume approve KEY`. Nothing below was used.")
        L.append("")
        for e in v.posting.proposals:
            L.append(f"- `{e.key}` {e.kind}: {e.name}" + (f" · {e.note}" if e.note else ""))
        L.append("")
    if v.posting.gaps:
        L.append("## Posting terms not on your resume")
        L.append("")
        L.extend(f"- {g}" for g in v.posting.gaps)
        L.append("")
    if v.posting.terms:
        L.append("## Posting terms read")
        L.append("")
        L.append(", ".join(v.posting.terms) + (f" · skills: {', '.join(v.posting.skills)}" if v.posting.skills else ""))
        L.append("")
    L.append("## Text diff")
    L.append("")
    L.append("```diff")
    L.extend(l.rstrip("\n") for l in difflib.unified_diff(html_text(master_html).splitlines(), html_text(variant_html).splitlines(),
                                                           "master", v.name, lineterm="", n=1))
    L.append("```")
    L.append("")
    return "\n".join(L)


def write_variant(folder: Path, v: Variant, job: dict, master_html: str, variant_html: str, run_id: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "variant.html").write_text(variant_html, encoding="utf-8")
    (folder / "diff.md").write_text(diff_md(job, v, master_html, variant_html, run_id), encoding="utf-8")
    rec = {"job": {k: job.get(k) for k in ("id", "company", "title", "url")}, "run_id": run_id,
           "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "name": v.name, "tag": v.tag, "master_sha": v.master_sha, "bank_sha": v.bank_sha,
           "posting_sha": posting_sha(job), "posting": v.posting.to_dict(),
           "edits": [e.to_dict() for e in v.edits],
           "proposals": [{"kind": e.kind, "name": e.name, "key": e.key, "status": e.status} for e in v.posting.proposals],
           "gaps": list(v.posting.gaps), "identical": v.identical, "pages": v.pages, "overflow": v.overflow}
    (folder / "tweaks.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")


def build_variant(profile_dir: Path, job: dict, *, base: Optional[Path] = None,
                  call: Optional[Callable[[str], Optional[str]]] = None, run_id: str = "",
                  today: Optional[date] = None, log: Callable[[str], None] = print,
                  render: bool = True, model: str = "") -> Optional[Variant]:
    """The whole thing: master -> posting -> four operations -> PDF -> folder + index. None when there is
    no master, or when the variant spills past the master's page count (then the base resume is the
    better upload). Proposals the model made are saved to the bank, pending."""
    profile_dir = Path(profile_dir)
    master = load_master(profile_dir)
    if master is None:
        return None
    bank = TermBank(profile_dir)
    posting = read_posting(job, bank, master.html, call=call, log=log, model=model)
    html, edits = tailor(master.html, posting, bank)
    day = today or date.today()
    taken = {r["name"]: r.get("job_id") for r in read_index(profile_dir) if r.get("name")}
    name, tag = variant_name(base, job.get("id") or "", day, taken)
    folder = resume_dir(profile_dir) / VARIANTS / f"{day:%y-%m-%d}-{tag}"
    v = Variant(name=name, tag=tag, pdf=folder / name, folder=folder, edits=edits, posting=posting,
                master_sha=master.sha, bank_sha=bank.sha())
    if render:
        v.pages = render_pdf(html, v.pdf)
        limit = baseline_pages(profile_dir)
        if v.pages > limit:
            v.overflow = True
            log(f"resume: the variant runs to {v.pages} pages (master: {limit}); not using it")
    write_variant(folder, v, job, master.html, html, run_id)
    append_index(profile_dir, {"name": name, "tag": tag, "day": day.isoformat(), "job_id": job.get("id") or "",
                               "company": job.get("company") or "", "title": job.get("title") or "",
                               "url": job.get("url") or "", "run_id": run_id,
                               "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                               "folder": str(folder.relative_to(resume_dir(profile_dir))),
                               "edits": v.by_op(), "proposals": len(posting.proposals), "identical": v.identical,
                               "overflow": v.overflow, "master_sha": master.sha, "bank_sha": v.bank_sha})
    if bank.dirty:
        bank.save()
    return None if v.overflow else v


def propose_only(profile_dir: Path, job: dict, call: Optional[Callable[[str], Optional[str]]],
                 log: Callable[[str], None] = print, model: str = "") -> Optional[Posting]:
    """Read a posting and bank its proposals without building anything (`resume propose`)."""
    profile_dir = Path(profile_dir)
    master = load_master(profile_dir)
    if master is None:
        return None
    bank = TermBank(profile_dir)
    p = read_posting(job, bank, master.html, call=call, log=log, model=model)
    if bank.dirty:
        bank.save()
    return p
