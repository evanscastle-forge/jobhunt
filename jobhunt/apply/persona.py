"""The applicant agent ("applicant_agent"): answers an application question as the applicant, or declines.

It answers only from a knowledge base of what the applicant has actually said, with every line numbered:

    F*  notes/about.md facts             (the same facts the scorer reads)
    P*  profile.toml answers and identity
    B*  approved answer-bank entries
    R*  the resume, extracted from the PDF

and writes in the applicant's voice from notes/voice.md (their own short answers, real writing, chat
register, and the corrections they made to earlier drafts).

Two model calls, each returning JSON:

    1. answer   question + knowledge base + voice -> {answer | decline, evidence ids, needs}
    2. check    the answer + only the lines it cited -> is every claim supported? any tells of model prose?

and deterministic checks between them: cited ids must exist, a choice must be one of the options, em
dashes are removed. An unsupported claim is a decline, never a softer answer. A decline names the fact
that would let it answer, so the gap can be closed in about.md or the bank.

Nothing the agent writes is ever typed into a form on its own: its answers are proposals (tier
"inferred" for a choice, "drafted" for prose) that the applicant approves. See docs/apply-design.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import answers as bankmod
from .schema import match_option

AGENT = "applicant_agent"
# Facts about pay are private to the scorer: a form gets answers.salary_expectation, and nothing else the
# applicant has said about money (a comfort level, a floor) may reach an application. More patterns can
# be added per profile as answers.agent_private.
_PAY = r"\$\s?\d|\bsalary\b|\bcompensation\b|\bbase pay\b|\bfloor\b"
_TELLS = re.compile(r"\b(passionate|thrilled|excited to|leverag\w*|synerg\w*|cutting[- ]edge|dynamic|spearhead\w*|"
                    r"delve|tapestry|testament|seamless\w*|robust|deeply (committed|passionate))\b", re.I)


@dataclass
class AgentAnswer:
    question: str
    answer: Optional[str] = None
    declined: bool = False
    reason: str = ""                 # why it declined, or what the check changed
    needs: str = ""                  # the fact that would let it answer
    evidence: list = field(default_factory=list)   # knowledge-base ids the answer rests on
    tells: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def note(self) -> str:
        if self.declined:
            return f"declined: {self.reason}" + (f" · needs: {self.needs}" if self.needs else "")
        return "evidence: " + ", ".join(self.evidence) + (f" · {self.reason}" if self.reason else "")


# ----------------------------------------------------------------------------- knowledge base

def resume_text(path: Optional[Path]) -> str:
    """Text of the resume PDF (pypdf's layout mode keeps bullets whole), '' when unreadable."""
    if not path or not Path(path).exists():
        return ""
    if str(path).lower().endswith((".md", ".txt")):
        return Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        import pypdf
        pages = pypdf.PdfReader(str(path)).pages
        return "\n".join(pg.extract_text(extraction_mode="layout") for pg in pages)
    except Exception:  # noqa: BLE001
        return ""


def resume_links(path: Optional[Path]) -> list[str]:
    """Hyperlinks in the resume PDF, with the text they are attached to. They are not part of the page
    text, so without this the agent cannot see that a project name links to its public announcement."""
    if not path or not str(path).lower().endswith(".pdf") or not Path(path).exists():
        return []
    try:
        import pypdf
        found: dict[str, str] = {}          # uri -> the longest text it is attached to (a wrapped line has two)
        for page in pypdf.PdfReader(str(path)).pages:
            frags: list[tuple] = []
            # a fragment's page position is its text matrix carried through the current transform
            page.extract_text(visitor_text=lambda text, cm, tm, fd, fs: frags.append(
                (tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5], text)))
            for a in page.get("/Annots") or []:
                o = a.get_object()
                uri = (o.get("/A") or {}).get("/URI")
                if not uri:
                    continue
                x0, y0, x1, y1 = (float(v) for v in o.get("/Rect"))
                near = " ".join(s.strip() for x, y, s in frags if y0 - 3 <= y <= y1 + 3 and s.strip())
                label = re.sub(r"\s+", " ", near).strip().lstrip("●•*- ").replace(" .", ".")[:200]
                if len(label) > len(found.get(uri, "")):
                    found[uri] = label
                found.setdefault(uri, "")
        return [f"Resume link: {uri}" + (f" (on the resume line: {label})" if label else "") for uri, label in found.items()]
    except Exception:  # noqa: BLE001
        return []


def _resume_lines(text: str) -> list[str]:
    """One line per bullet or heading: a long line that does not end a sentence was wrapped, so the next
    line continues it (headings, in capitals, never join)."""
    out: list[str] = []
    for raw in text.splitlines():
        s = re.sub(r"\s+", " ", raw).strip().lstrip("●•*- ").strip()
        if not s:
            continue
        prev = out[-1] if out else ""
        heading = lambda x: bool(re.fullmatch(r"[A-Z][A-Z &/]+", x))   # noqa: E731
        if prev and len(prev) > 40 and not prev.endswith((".", ")", ":")) and not heading(prev) and not heading(s):
            out[-1] = prev + " " + s
        else:
            out.append(s)
    return out


def _flatten(d: dict, prefix: str = "") -> list[str]:
    out = []
    for k, v in d.items():
        if k in ("custom", "choices", "voice_samples", "agent_private"):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out += _flatten(v, key + ".")
        elif v not in ("", None, []):
            out.append(f"{key}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
    return out


@dataclass
class KnowledgeBase:
    lines: list = field(default_factory=list)      # (id, text)

    def add(self, prefix: str, texts: list[str]) -> None:
        n = sum(1 for i, _ in self.lines if i.startswith(prefix))
        for t in texts:
            n += 1
            self.lines.append((f"{prefix}{n}", t))

    def with_job(self, job: Optional[dict]) -> "KnowledgeBase":
        """This knowledge base plus the posting, one sentence per J line: evidence about the company and
        the role (never about the applicant)."""
        kb = KnowledgeBase(list(self.lines))
        text = re.sub(r"\s+", " ", (job or {}).get("description") or (job or {}).get("snippet") or "")
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if 25 <= len(s.strip()) <= 400]
        head = [f"The company is {job.get('company')}; the role is {job.get('title')}."] if job and job.get("company") else []
        kb.add("J", head + sents[:60])
        return kb

    def ids(self) -> set:
        return {i for i, _ in self.lines}

    def get(self, ids: list[str]) -> list[tuple]:
        want = set(ids)
        return [(i, t) for i, t in self.lines if i in want]

    def render(self, only: Optional[list[str]] = None) -> str:
        rows = self.get(only) if only is not None else self.lines
        return "\n".join(f"[{i}] {t}" for i, t in rows)

    @classmethod
    def build(cls, profile: dict, notes: str = "", bank: Optional[bankmod.Bank] = None,
              resume: Optional[Path] = None) -> "KnowledgeBase":
        kb = cls()
        private = [re.compile(x, re.I) for x in [_PAY] + list((profile.get("answers") or {}).get("agent_private") or [])]
        kb.add("F", [ln[2:].strip() for ln in (notes or "").splitlines()
                     if ln.startswith("- ") and not any(rx.search(ln) for rx in private)])
        prof = []
        for section in ("identity", "links", "answers"):
            prof += _flatten(profile.get(section) or {}, f"{section}.")
        kb.add("P", [p for p in prof if not p.endswith(": none")])
        if bank is not None:
            kb.add("B", [f"Q: {e.question} A: {e.answer}" for e in bank.entries if e.approved])
        kb.add("R", _resume_lines(resume_text(resume)) + resume_links(resume))
        return kb


# ----------------------------------------------------------------------------- the agent

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


def _clean(text: str) -> str:
    text = re.sub(r"\s*[—–]\s*", ", ", text)        # em and en dashes read as model prose
    return re.sub(r"\s+\n", "\n", text).strip()


class ApplicantAgent:
    """ask(question) -> AgentAnswer. `call` is llm._call bound to the config (injectable for tests)."""

    def __init__(self, kb: KnowledgeBase, voice: str, call: Callable[[str], Optional[str]], name: str = "the applicant"):
        self.kb, self.voice, self.call, self.name = kb, voice, call, name

    def _answer_prompt(self, question: str, kind: str, options: list[str], job: dict, kb: "KnowledgeBase") -> str:
        choice = bool(options)
        return (
            f"You answer one job-application question as {self.name}, in the first person, using ONLY the numbered "
            "knowledge base below. The resume (R lines, with its dates and links) is the main record and should settle "
            "most questions: cross-reference it with the facts. Deduce what the record implies, the way the applicant "
            "would: dates give years (a role from 2018 to 2024 is about six years); a tool named as a core tool in every "
            "ML role counts for the span of those roles; a U.S. citizen is a U.S. person under export rules; someone who "
            "never worked at a company has no history with it. Every fact you use must still be a line in the knowledge "
            "base, and you cite the ids of every line you rely on. If the record does not settle the answer, DECLINE: "
            "a wrong answer on an application is far worse than no answer. Never invent numbers, employers, projects, "
            "dates, feelings or motivations. J lines are the posting: they are evidence about the company and the role, "
            "never about the applicant. A reason for wanting a job (why this company, what excites you) must join a "
            "stated interest or goal of the applicant (an F, P, B or R line) to something the posting says (a J line), "
            "citing both; say nothing about the applicant that only the posting supports.\n\n"
            + ("This is a multiple-choice question. Answer with the exact text of ONE option, or decline.\n"
               if choice else
               "Write the answer in the applicant's voice (samples below): their word choice and directness, normal "
               "capitalization and punctuation, no em dashes, no bullet lists, no marketing words, no opener that restates "
               "the question. As short as the question allows; at most 150 words.\n")
            + "\nReply with JSON only:\n"
            '{"decline": false, "answer": "...", "evidence": ["F3", "R12"]}\n'
            'or {"decline": true, "reason": "what is missing", "needs": "the one fact that would let me answer"}\n\n'
            f"Company: {job.get('company') or ''}\nRole: {job.get('title') or ''}\n"
            + f"\nQuestion ({kind}): {question}\n"
            + ("Options:\n" + "\n".join(f"- {o}" for o in options) + "\n" if choice else "")
            + f"\nKnowledge base:\n{kb.render()}\n"
            + ("" if choice else f"\nHow the applicant writes:\n{self.voice[:6000]}\n")
        )

    def _check_prompt(self, question: str, answer: str, evidence: list[str], kb: "KnowledgeBase") -> str:
        return (
            "You check one answer written for a job application against the only lines it may rely on. List every "
            "claim in the answer that those lines do not support (a deduction that follows directly is supported; "
            "a number, employer, project, feeling or motivation not in the lines is not; J lines describe the company "
            "and the role only, so a claim about the applicant resting only on a J line is unsupported). Also list "
            "phrases that read "
            "as written by a language model (stock enthusiasm, 'leverage', tidy three-part lists, restating the "
            "question). If anything is unsupported or reads as model prose, give a revision that removes it and adds "
            "nothing new, as long as what is left still answers the question; otherwise revised is null.\n\n"
            "Reply with JSON only:\n"
            '{"unsupported": ["..."], "tells": ["..."], "revised": null or "..."}\n\n'
            f"Question: {question}\nAnswer: {answer}\n\nLines:\n{kb.render(evidence)}\n"
        )

    def ask(self, question: str, kind: str = "textarea", options: Optional[list[str]] = None,
            job: Optional[dict] = None) -> AgentAnswer:
        options = [o for o in (options or []) if o.strip()]
        out = AgentAnswer(question=question)
        kb = self.kb.with_job(job) if job else self.kb
        got = _json(self.call(self._answer_prompt(question, kind, options, job or {}, kb)))
        if got is None:
            return self._decline(out, "the model gave no readable answer", "")
        if got.get("decline") or not str(got.get("answer") or "").strip():
            return self._decline(out, str(got.get("reason") or "not in the knowledge base"), str(got.get("needs") or ""))
        answer = str(got["answer"]).strip()
        evidence = [str(e) for e in (got.get("evidence") or [])]
        known = kb.ids()
        bad = [e for e in evidence if e not in known]
        if not evidence or bad:
            return self._decline(out, f"cited lines that do not exist: {bad}" if bad else "cited no evidence", "")
        out.evidence = evidence
        if options:
            picked, strength = match_option(options, answer)
            if not picked or strength != "strong":
                return self._decline(out, f"answer {answer!r} is not one of the options", "")
            out.answer = picked
            return out
        check = _json(self.call(self._check_prompt(question, answer, evidence, kb)))
        if check is None:
            return self._decline(out, "the grounding check gave no readable result", "")
        unsupported = [str(u) for u in (check.get("unsupported") or []) if str(u).strip()]
        if unsupported:
            # one chance: the checker's revision without the unsupported claims, checked again from scratch
            revised = check.get("revised")
            again = _json(self.call(self._check_prompt(question, revised, evidence, kb))) \
                if isinstance(revised, str) and revised.strip() else None
            if again is None or [u for u in (again.get("unsupported") or []) if str(u).strip()]:
                return self._decline(out, "unsupported claims: " + "; ".join(unsupported)[:300],
                                     "a fact in about.md that states: " + unsupported[0][:160])
            answer, check = revised.strip(), again
            out.reason = "removed unsupported: " + "; ".join(unsupported)[:200]
        out.tells = [str(x) for x in (check.get("tells") or [])]
        revised = check.get("revised")
        if out.tells and isinstance(revised, str) and revised.strip():
            answer = revised.strip()
            out.reason = (out.reason + " · " if out.reason else "") + "revised to remove: " + "; ".join(out.tells)[:200]
        answer = _clean(answer)
        cited = " ".join(text for _, text in kb.get(evidence)).lower()
        left = [m.group(0) for m in _TELLS.finditer(answer) if m.group(0).lower() not in cited]   # your own words are not tells
        if left:
            out.reason = (out.reason + " · " if out.reason else "") + "still reads as model prose: " + ", ".join(sorted(set(left)))
        out.answer = answer
        return out

    @staticmethod
    def _decline(out: AgentAnswer, reason: str, needs: str) -> AgentAnswer:
        out.declined, out.reason, out.needs, out.answer = True, reason, needs, None
        return out


def voice_text(profile_dir: Path, profile: dict) -> str:
    p = Path(profile_dir) / "notes" / "voice.md"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    samples = (profile.get("answers") or {}).get("voice_samples") or []
    return text + ("\n\nMore samples:\n" + "\n".join(f"- {s}" for s in samples) if samples else "")


def for_profile(p, call: Optional[Callable[[str], Optional[str]]] = None) -> Optional[ApplicantAgent]:
    """The agent for a loaded profile, or None when no LLM backend is configured."""
    from .. import llm
    cfg = (p.search.get("llm") or {})
    if call is None:
        if (cfg.get("backend") or "none").lower() == "none":
            return None
        call = lambda prompt: llm._call(cfg, prompt, max_tokens=900)   # noqa: E731
    bank = bankmod.Bank(p.dir)
    kb = KnowledgeBase.build(p.answers, p.notes, bank, p.resume_path)
    name = ((p.answers.get("identity") or {}).get("first_name") or p.name).strip()
    return ApplicantAgent(kb, voice_text(p.dir, p.answers), call, name=name)


def propose_pending(agent: ApplicantAgent, bank: bankmod.Bank, limit: int = 0,
                    log: Callable[[str], None] = print) -> dict:
    """Run the agent over pending bank entries that have no proposal yet, and store what it says as a
    proposal (`by: applicant_agent`, still pending) or a decline in the note. Returns counts."""
    todo = [e for e in bank.entries if not e.approved and not e.answer.strip() and e.by != AGENT]
    if limit:
        todo = todo[:limit]
    done = {"proposed": 0, "declined": 0}
    for e in todo:
        res = agent.ask(e.question, e.kind, e.options if e.kind in ("select", "combobox", "radio", "checkgroup") else None)
        if res.declined:
            bank.set(e.key, by=AGENT, note=res.note())
            done["declined"] += 1
            log(f"  declined  {e.question[:80]}\n            {res.note()[:160]}")
        else:
            bank.set(e.key, answer=res.answer, status="pending", by=AGENT, note=res.note())
            done["proposed"] += 1
            log(f"  proposed  {e.question[:80]}\n            -> {res.answer[:160]}")
        bank.save()
    return done
