"""Issues and Responses: how the apply run asks, and how you answer.

Both are JSON lines in the run folder, so the dashboard, the CLI (`jobhunt.py issues`) or a text editor
can answer them:

    issues.jsonl      the run's open issues, rewritten after every fill pass
    responses.jsonl   your answers, appended; consumed (moved to responses.done.jsonl) on resume

Issue kinds (docs/apply-design.md):
    question        nothing to go on
    confirm_match   close to an approved bank question; is it the same?
    confirm_choice  a proposal from your profile; is it right?
    review_draft    a model draft; edit it
    blocked         captcha, AI-use question, a value that did not stick, an unsupported form

Response actions: answer | edit (use this value), approve (the proposal is right), not_same (the match is
wrong), skip (leave it for me in the window).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

KINDS = ("question", "confirm_match", "confirm_choice", "review_draft", "blocked")
ACTIONS = ("answer", "edit", "approve", "not_same", "skip")


@dataclass
class Issue:
    id: str
    kind: str
    label: str
    why: str = ""
    required: bool = False
    blocking: bool = False          # keeps the run from being ready
    options: list = field(default_factory=list)
    proposal: Optional[str] = None
    key: str = ""                   # the answer-bank entry it resolves, when there is one

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def blocked_id(label: str) -> str:
    return "blocked:" + hashlib.sha1(label.encode("utf-8")).hexdigest()[:10]


def from_question(q: dict) -> Issue:
    tier, source = q.get("tier") or "unknown", q.get("source") or ""
    if tier == "inferred":
        kind = "confirm_match" if source.startswith("bank-fuzzy:") else "confirm_choice"
    elif tier == "drafted":
        kind = "review_draft"
    else:
        kind = "question"
    return Issue(id=q["key"], kind=kind, label=q["label"], why=q.get("why") or "", required=bool(q.get("required")),
                 blocking=bool(q.get("required")), options=list(q.get("options") or []),
                 proposal=q.get("draft"), key=q["key"])


def blocked(label: str, why: str) -> Issue:
    return Issue(id=blocked_id(label), kind="blocked", label=label, why=why, required=True, blocking=True)


# ----------------------------------------------------------------------------- files

def issues_path(run_dir: Path) -> Path:
    return Path(run_dir) / "issues.jsonl"


def responses_path(run_dir: Path) -> Path:
    return Path(run_dir) / "responses.jsonl"


def write_issues(run_dir: Optional[Path], issues: list[Issue]) -> None:
    if run_dir is None:
        return
    p = issues_path(run_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i.to_dict()) + "\n" for i in issues), encoding="utf-8")
    tmp.replace(p)


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_issues(run_dir: Path) -> list[Issue]:
    out = []
    for d in _read_jsonl(issues_path(run_dir)):
        try:
            out.append(Issue(**{k: v for k, v in d.items() if k in Issue.__dataclass_fields__}))
        except TypeError:
            continue
    return out


def respond(run_dir: Path, issue_id: str, action: str, value: Optional[str] = None, scope: str = "global") -> dict:
    """Append one response. The run applies it on its next resume."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r} (one of {', '.join(ACTIONS)})")
    if action in ("answer", "edit") and not (value or "").strip():
        raise ValueError(f"{action} needs a value")
    rec = {"issue_id": issue_id, "action": action, "value": value, "scope": scope, "at": _now()}
    with responses_path(run_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def take_responses(run_dir: Optional[Path]) -> list[dict]:
    """Read and consume pending responses (moved to responses.done.jsonl, so nothing is applied twice
    and nothing is lost)."""
    if run_dir is None:
        return []
    p = responses_path(run_dir)
    recs = _read_jsonl(p)
    if recs:
        with (Path(run_dir) / "responses.done.jsonl").open("a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        p.unlink(missing_ok=True)
    return recs


def apply_responses(responses: list[dict], issues: list[Issue], bank) -> dict:
    """Teach the bank from your responses. Returns {"skipped": [issue ids], "not_same": [...],
    "applied": n}. An approved answer is exact and approved, so the same question is certain on every
    later form: this is how the tool asks each thing once."""
    by_id = {i.id: i for i in issues}
    skipped, not_same, applied = [], [], 0
    for r in responses:
        issue = by_id.get(r.get("issue_id"))
        action = r.get("action")
        if action == "skip":
            skipped.append(r.get("issue_id"))
            continue
        if issue is None or not issue.key or bank is None:
            continue
        if action == "not_same":
            bank.set(issue.key, status="pending")
            not_same.append(issue.id)
            continue
        value = r.get("value") if action in ("answer", "edit") else issue.proposal
        if value and bank.set(issue.key, answer=value, status="approved") is not None:
            applied += 1
    if bank is not None:
        bank.save()
    return {"skipped": skipped, "not_same": not_same, "applied": applied}
