"""The resume term bank: what a per-posting resume variant may use (jobhunt/apply/tailor.py).

profiles/<name>/resume/terms.md, one entry per heading, three kinds:

    ## synonyms: agentic system = AI agent
    - status: pending                 approved | pending
    - terms: agentic system | agentic systems | AI agent | AI agents
    - by: tailor                      "" = you; "tailor" = proposed by the tool
    - from: greenhouse:acme:123 · Acme · Forward Deployed AI Engineer · 2026-10-20
    - note: posting: "…build AI agents…" · resume: "Built an agentic assistant…"

    ## skill: Python                  a skill you have; the SKILLS line lists every approved one
    - status: approved
    - aliases: python3 | python 3     other spellings a posting may use

    ## headline: Forward Deployed Engineer       a title you have held, or an honest equivalent
    - status: approved
    - matches: forward deployed | FDE            phrases in a posting title that call for it

A variant only ever uses an *approved* entry. The tool may propose one (by: tailor); it stays pending,
and unused, until you approve it here or on the dashboard's Answers page (docs/apply-design.md).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .answers import normalize

KINDS = ("synonyms", "skill", "headline")
STATUSES = ("approved", "pending")
TERMS_FILE = "terms.md"
RESUME_DIR = "resume"

HEADER = ("# Resume terms\n\n"
          "What a per-posting resume variant may change, and nothing else: approved synonym groups\n"
          "(a phrase swapped for the posting's word for the same thing), the skills vocabulary for the\n"
          "SKILLS line, and headlines (titles you have held). A *pending* entry is a proposal and is never\n"
          "used. Edit here or on the dashboard's Answers page.\n\n")


@dataclass
class TermEntry:
    kind: str
    name: str
    status: str = "pending"
    terms: list = field(default_factory=list)      # synonyms: the members, two or more
    aliases: list = field(default_factory=list)    # skill: other spellings
    matches: list = field(default_factory=list)    # headline: phrases tested against the posting title
    by: str = ""                                   # "" = you; "tailor" = a proposal you have not approved
    source: str = ""                               # the posting that proposed it
    note: str = ""                                 # the evidence

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.kind}:{normalize(self.name)}".encode("utf-8")).hexdigest()[:12]

    @property
    def members(self) -> list[str]:
        """Every spelling this entry answers to."""
        if self.kind == "synonyms":
            return list(self.terms)
        if self.kind == "skill":
            return [self.name] + list(self.aliases)
        return list(self.matches)

    @property
    def approved(self) -> bool:
        if self.status != "approved" or not self.name.strip():
            return False
        if self.kind == "synonyms":
            return len([t for t in self.terms if t.strip()]) >= 2
        return True

    def to_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "name": self.name, "status": self.status,
                "terms": list(self.terms), "aliases": list(self.aliases), "matches": list(self.matches),
                "by": self.by, "source": self.source, "note": self.note, "approved": self.approved}


def resume_dir(profile_dir: Path) -> Path:
    return Path(profile_dir) / RESUME_DIR


def terms_path(profile_dir: Path) -> Path:
    return resume_dir(profile_dir) / TERMS_FILE


def _split(v: str) -> list[str]:
    seen, out = set(), []
    for part in (v or "").split("|"):
        s = " ".join(part.split())
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


# ----------------------------------------------------------------------------- parse / write

_HEAD_RE = re.compile(r"^##\s+(synonyms|skill|headline)\s*:\s*(.+?)\s*$")
_META_RE = re.compile(r"^-\s*(status|terms|aliases|matches|by|from|note)\s*:\s*(.*)$")


def parse_terms(md: str) -> list[TermEntry]:
    out: list[TermEntry] = []
    cur: Optional[TermEntry] = None
    for raw in (md or "").splitlines():
        s = raw.strip()
        m = _HEAD_RE.match(s)
        if m:
            cur = TermEntry(kind=m.group(1), name=m.group(2))
            out.append(cur)
            continue
        if cur is None:
            continue
        m = _META_RE.match(s)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "status":
            cur.status = v if v in STATUSES else "pending"
        elif k == "terms":
            cur.terms = _split(v)
        elif k == "aliases":
            cur.aliases = _split(v)
        elif k == "matches":
            cur.matches = _split(v)
        elif k == "by":
            cur.by = v
        elif k == "from":
            cur.source = v
        elif k == "note":
            cur.note = v
    for e in out:
        if e.kind == "synonyms" and not e.terms:
            e.terms = _split(e.name.replace(" = ", "|"))
        if e.kind == "headline" and not e.matches:
            e.matches = [e.name]
    return [e for e in out if e.name.strip()]


def render_terms(entries: list[TermEntry]) -> str:
    lines = [HEADER]
    for e in entries:
        lines.append(f"## {e.kind}: {e.name}\n")
        lines.append(f"- status: {e.status}\n")
        if e.kind == "synonyms":
            lines.append(f"- terms: {' | '.join(e.terms)}\n")
        elif e.kind == "skill" and e.aliases:
            lines.append(f"- aliases: {' | '.join(e.aliases)}\n")
        elif e.kind == "headline":
            lines.append(f"- matches: {' | '.join(e.matches)}\n")
        if e.by:
            lines.append(f"- by: {e.by}\n")
        if e.source:
            lines.append(f"- from: {' '.join(e.source.split())}\n")
        if e.note:
            lines.append(f"- note: {' '.join(e.note.split())}\n")
        lines.append("\n")
    return "".join(lines)


def read_terms(profile_dir: Path) -> list[TermEntry]:
    p = terms_path(profile_dir)
    if not p.exists():
        return []
    try:
        return parse_terms(p.read_text(encoding="utf-8"))
    except OSError:
        return []


def write_terms(profile_dir: Path, entries: list[TermEntry]) -> None:
    p = terms_path(profile_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(render_terms(entries), encoding="utf-8")
    tmp.replace(p)


# ----------------------------------------------------------------------------- the bank

class TermBank:
    """In-memory view of resume/terms.md. `propose` adds pending entries; `save` merges this process's
    changes under a lock, as answers.Bank does, because several apply processes may propose at once."""

    def __init__(self, profile_dir: Optional[Path], entries: Optional[list[TermEntry]] = None):
        self.dir = Path(profile_dir) if profile_dir else None
        self.entries: list[TermEntry] = list(entries) if entries is not None else (read_terms(self.dir) if self.dir else [])
        self.dirty = False
        self._touched: list[TermEntry] = []
        self._removed: set[str] = set()

    def _mark(self, e: TermEntry) -> None:
        if not any(x is e for x in self._touched):
            self._touched.append(e)
        self.dirty = True

    def get(self, key: str) -> Optional[TermEntry]:
        return next((e for e in self.entries if e.key == key), None)

    def find(self, kind: str, name: str) -> Optional[TermEntry]:
        q = normalize(name)
        return next((e for e in self.entries if e.kind == kind and normalize(e.name) == q), None)

    def approved(self, kind: Optional[str] = None) -> list[TermEntry]:
        return [e for e in self.entries if e.approved and (kind is None or e.kind == kind)]

    def pending(self) -> list[TermEntry]:
        return [e for e in self.entries if not e.approved]

    def group_for(self, *phrases: str) -> Optional[TermEntry]:
        """The synonym group (any status) that already holds every one of these phrases."""
        want = {normalize(p) for p in phrases if p}
        for e in self.entries:
            if e.kind == "synonyms" and want <= {normalize(t) for t in e.terms}:
                return e
        return None

    def skill_for(self, phrase: str) -> Optional[TermEntry]:
        q = normalize(phrase)
        for e in self.entries:
            if e.kind == "skill" and q in {normalize(m) for m in e.members}:
                return e
        return None

    def propose(self, kind: str, name: str, *, terms=(), aliases=(), matches=(), source: str = "",
                note: str = "", by: str = "tailor", status: str = "pending") -> Optional[TermEntry]:
        """Add an entry unless one like it exists. Never changes an existing entry, approved or not."""
        if kind not in KINDS or not name.strip():
            return None
        name = " ".join(name.split())
        if self.find(kind, name) is not None:
            return None
        if kind == "synonyms" and (len(_split("|".join(terms))) < 2 or self.group_for(*terms) is not None):
            return None
        if kind == "skill" and self.skill_for(name) is not None:
            return None
        e = TermEntry(kind=kind, name=name, status=status if status in STATUSES else "pending",
                      terms=_split("|".join(terms)), aliases=_split("|".join(aliases)),
                      matches=_split("|".join(matches)) or ([name] if kind == "headline" else []),
                      by=by, source=source, note=note)
        self.entries.append(e)
        self._mark(e)
        return e

    def set(self, key: str, status: Optional[str] = None, by: Optional[str] = None, note: Optional[str] = None,
            terms=None, aliases=None, matches=None) -> Optional[TermEntry]:
        """Approving without naming `by` means you approved it: it becomes yours."""
        e = self.get(key)
        if e is None:
            return None
        if status in STATUSES:
            e.status = status
        if by is not None:
            e.by = by
        elif status == "approved":
            e.by = ""
        if note is not None:
            e.note = note
        if terms is not None:
            e.terms = _split("|".join(terms))
        if aliases is not None:
            e.aliases = _split("|".join(aliases))
        if matches is not None:
            e.matches = _split("|".join(matches))
        self._mark(e)
        return e

    def remove(self, key: str) -> bool:
        n = len(self.entries)
        self.entries = [e for e in self.entries if e.key != key]
        if len(self.entries) != n:
            self._removed.add(key)
            self._touched = [x for x in self._touched if x.key != key]
            self.dirty = True
        return len(self.entries) != n

    def save(self) -> None:
        if not (self.dir and self.dirty):
            return
        import fcntl
        lockp = resume_dir(self.dir) / ".lock"
        lockp.parent.mkdir(parents=True, exist_ok=True)
        with open(lockp, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            merged = [e for e in read_terms(self.dir) if e.key not in self._removed]
            at = {e.key: i for i, e in enumerate(merged)}
            for e in self._touched:
                if e.key in self._removed:
                    continue
                if e.key in at:
                    merged[at[e.key]] = e
                else:
                    at[e.key] = len(merged)
                    merged.append(e)
            self.entries = merged
            write_terms(self.dir, self.entries)
        self.dirty, self._touched, self._removed = False, [], set()

    def counts(self) -> dict:
        return {"approved": sum(1 for e in self.entries if e.approved),
                "pending": sum(1 for e in self.entries if not e.approved)}

    def sha(self) -> str:
        """Identifies the approved entries a variant was built with."""
        text = render_terms(sorted(self.approved(), key=lambda e: (e.kind, normalize(e.name))))
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
