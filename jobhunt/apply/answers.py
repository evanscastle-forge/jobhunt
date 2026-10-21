"""The answer bank: every application question the assistant has met, with the answer it may reuse.

notes/answers.md, one entry per question:

    ## Why do you want to work at {company}?
    - status: approved            approved | pending
    - kind: textarea              text | textarea | select | combobox | radio | checkgroup
    - options: Yes | No           (choice questions only; what the form offered)
    - required: yes
    - seen: 3 · greenhouse:anduril:123 · 2026-09-17
    - answer: Because {company} builds ...
      further lines of the answer are indented
    - by: applicant_agent         who wrote the answer: you (default) | applicant_agent (a proposal)
    - note: F3, R12 · ...         the agent's evidence, or why it declined

The assistant only ever fills a question from an *approved* entry (or from profile.toml); anything
else is recorded here as *pending* and the run pauses for you. {company} and {title} are substituted
when the answer is pasted, so one entry covers "Why Acme?" and "Why Globex?" alike. A pending
entry may carry a proposal from the applicant agent (persona.py, `by: applicant_agent`); it is never used
until you approve it.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

ANSWERS_FILE = "answers.md"
STATUSES = ("approved", "pending")
FUZZY_MIN = 0.92

HEADER = ("# Application answers\n\n"
          "Questions the apply assistant has met on application forms. An *approved* answer is pasted\n"
          "verbatim ({company} and {title} substituted); a *pending* one waits for you on the dashboard's\n"
          "Answers page and the run pauses. Edit here or there.\n\n")


@dataclass
class Entry:
    question: str
    status: str = "pending"
    kind: str = "text"
    options: list = field(default_factory=list)
    answer: str = ""
    seen: int = 0
    last_job: str = ""
    last_seen: str = ""
    required: bool = False
    by: str = ""                    # "" = you; "applicant_agent" = a proposal you have not approved
    note: str = ""                  # evidence behind a proposal, or why the agent declined

    @property
    def key(self) -> str:
        return question_key(self.question)

    @property
    def approved(self) -> bool:
        return self.status == "approved" and bool(self.answer.strip())

    def to_dict(self) -> dict:
        return {"key": self.key, "question": self.question, "status": self.status, "kind": self.kind,
                "options": list(self.options), "answer": self.answer, "seen": self.seen,
                "last_job": self.last_job, "last_seen": self.last_seen, "required": self.required,
                "approved": self.approved, "by": self.by, "note": self.note}


def normalize(text: str) -> str:
    t = re.sub(r"[\*✱]+|\(required\)|\(optional\)", " ", (text or "").lower())
    t = re.sub(r"[^\w{}%$?.,'/-]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def question_key(question: str) -> str:
    return hashlib.sha1(normalize(question).encode("utf-8")).hexdigest()[:12]


def generalize(label: str, job: Optional[dict]) -> str:
    """'Why do you want to work at Acme?' -> 'Why do you want to work at {company}?'"""
    out = re.sub(r"\s+", " ", (label or "")).strip()
    out = re.sub(r"[\*✱]+\s*$", "", out).strip()
    for token, key in (("{company}", "company"), ("{title}", "title")):
        val = (job or {}).get(key) or ""
        if len(val) >= 3:
            out = re.sub(re.escape(val), token, out, flags=re.I)
    return out


def specialize(text: str, job: Optional[dict]) -> str:
    j = job or {}
    return (text or "").replace("{company}", j.get("company") or "the company") \
                       .replace("{title}", j.get("title") or "this role")


# ----------------------------------------------------------------------------- parse / write

_META_RE = re.compile(r"^-\s*(status|kind|options|seen|answer|required|by|note):\s*(.*)$")


def parse_bank(md: str) -> list[Entry]:
    out: list[Entry] = []
    cur: Optional[Entry] = None
    in_answer = False
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("## "):
            cur = Entry(question=s[3:].strip())
            out.append(cur)
            in_answer = False
            continue
        if cur is None:
            continue
        m = _META_RE.match(s)
        if m and not (in_answer and line[:1].isspace()):
            k, v = m.group(1), m.group(2).strip()
            in_answer = False
            if k == "status":
                cur.status = v if v in STATUSES else "pending"
            elif k == "kind":
                cur.kind = v or "text"
            elif k == "options":
                cur.options = [o.strip() for o in v.split("|") if o.strip()]
            elif k == "required":
                cur.required = v.lower() in ("yes", "true", "1")
            elif k == "seen":
                parts = [p.strip() for p in v.split("·")]
                try:
                    cur.seen = int(parts[0])
                except (ValueError, IndexError):
                    cur.seen = 0
                cur.last_job = parts[1] if len(parts) > 1 else ""
                cur.last_seen = parts[2] if len(parts) > 2 else ""
            elif k == "by":
                cur.by = v
            elif k == "note":
                cur.note = v
            elif k == "answer":
                cur.answer = v
                in_answer = True
            continue
        if in_answer and line[:1].isspace() and s:
            cur.answer = (cur.answer + "\n" + s) if cur.answer else s
        elif not s:
            in_answer = False
    return [e for e in out if e.question]


def render_bank(entries: list[Entry]) -> str:
    lines = [HEADER]
    for e in entries:
        lines.append(f"## {e.question}\n")
        lines.append(f"- status: {e.status}\n")
        lines.append(f"- kind: {e.kind}\n")
        if e.options:
            lines.append(f"- options: {' | '.join(e.options)}\n")
        if e.required:
            lines.append("- required: yes\n")
        lines.append(f"- seen: {e.seen} · {e.last_job} · {e.last_seen}\n")
        ans = (e.answer or "").strip().splitlines()
        if ans:
            lines.append(f"- answer: {ans[0]}\n")
            lines.extend(f"  {l}\n" for l in ans[1:])
        else:
            lines.append("- answer:\n")
        if e.by:
            lines.append(f"- by: {e.by}\n")
        if e.note:
            lines.append(f"- note: {' '.join(e.note.split())}\n")
        lines.append("\n")
    return "".join(lines)


def bank_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / "notes" / ANSWERS_FILE


def read_bank(profile_dir: Path) -> list[Entry]:
    p = bank_path(profile_dir)
    if not p.exists():
        return []
    try:
        return parse_bank(p.read_text(encoding="utf-8"))
    except OSError:
        return []


def write_bank(profile_dir: Path, entries: list[Entry]) -> None:
    p = bank_path(profile_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(render_bank(entries), encoding="utf-8")
    tmp.replace(p)


# ----------------------------------------------------------------------------- lookup

class Bank:
    """In-memory view of notes/answers.md for one apply run; `record` notes new questions, `save` writes."""

    def __init__(self, profile_dir: Optional[Path], entries: Optional[list[Entry]] = None):
        self.dir = Path(profile_dir) if profile_dir else None
        self.entries: list[Entry] = list(entries) if entries is not None else (read_bank(self.dir) if self.dir else [])
        self.dirty = False
        self._touched: list[Entry] = []      # entries this process changed: merged into the file on save
        self._removed: set[str] = set()      # keys this process deleted

    def _mark(self, e: Entry) -> None:
        if not any(x is e for x in self._touched):
            self._touched.append(e)
        self.dirty = True

    def reload(self) -> None:
        if self.dir:
            self.entries = read_bank(self.dir)
            self.dirty, self._touched, self._removed = False, [], set()

    def get(self, key: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.key == key), None)

    def find(self, label: str, job: Optional[dict] = None) -> Optional[Entry]:
        """Exact match on the generalized question, else a close fuzzy match (>= FUZZY_MIN)."""
        return self.match(label, job)[0]

    def match(self, label: str, job: Optional[dict] = None) -> tuple[Optional[Entry], str]:
        """(entry, "exact" | "fuzzy" | ""). Only an exact match is certain enough to type; a fuzzy one
        is a proposal (docs/apply-design.md)."""
        q = normalize(generalize(label, job))
        if not q:
            return None, ""
        for e in self.entries:
            if normalize(e.question) == q:
                return e, "exact"
        best, score = None, 0.0
        for e in self.entries:
            r = difflib.SequenceMatcher(None, q, normalize(e.question)).ratio()
            if r > score:
                best, score = e, r
        return (best, "fuzzy") if best is not None and score >= FUZZY_MIN else (None, "")

    def touch(self, e: Entry, job: Optional[dict]) -> None:
        """Count a use of an approved entry (so the Answers page shows what gets reused)."""
        e.seen += 1
        e.last_job = (job or {}).get("id") or ""
        e.last_seen = date.today().isoformat()
        self._mark(e)

    def record(self, label: str, kind: str, options: list[str], required: bool, job: Optional[dict],
               exact: bool = False) -> Entry:
        """Remember that a question was met (creating a pending entry when it is new). With exact, a
        phrasing that only fuzzily matches an entry gets its own entry, so approving it makes that
        phrasing certain."""
        e = self.match(label, job)
        e = e[0] if (e[1] == "exact" or (e[1] and not exact)) else None
        if e is None:
            e = Entry(question=generalize(label, job), kind=kind, options=list(options or [])[:40])
            self.entries.append(e)
        e.seen += 1
        e.last_job = (job or {}).get("id") or ""
        e.last_seen = date.today().isoformat()
        e.required = e.required or bool(required)
        if options and not e.options:
            e.options = list(options)[:40]
        self._mark(e)
        return e

    def save(self) -> None:
        """Write under a file lock, merging this process's changes into the file as it is now. Several
        apply windows, the dashboard and the CLI all write the bank; writing a stale copy whole would
        silently undo whatever the others saved since this one loaded (it happened, 26 Sept)."""
        if not (self.dir and self.dirty):
            return
        import fcntl
        lockp = bank_path(self.dir).with_suffix(".lock")
        lockp.parent.mkdir(parents=True, exist_ok=True)
        with open(lockp, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._touched or self._removed:
                merged = [e for e in read_bank(self.dir) if e.key not in self._removed]
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
            write_bank(self.dir, self.entries)
        self.dirty, self._touched, self._removed = False, [], set()

    def set(self, key: str, answer: Optional[str] = None, status: Optional[str] = None,
            question: Optional[str] = None, by: Optional[str] = None, note: Optional[str] = None) -> Optional[Entry]:
        """Change an entry. Approving without naming `by` means you approved it: it becomes yours."""
        e = self.get(key)
        if e is None:
            return None
        if answer is not None:
            e.answer = answer.strip()
        if status in STATUSES:
            e.status = status
        if by is not None:
            e.by = by
        elif status == "approved":
            e.by = ""
        if note is not None:
            e.note = note
        if question and question.strip() != e.question:
            self._removed.add(e.key)          # the key follows the question text
            e.question = question.strip()
            self._removed.discard(e.key)
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

    def counts(self) -> dict:
        return {"approved": sum(1 for e in self.entries if e.approved),
                "pending": sum(1 for e in self.entries if not e.approved)}
