"""Optional LLM hooks. Off by default ([llm] backend = "none" in config.toml).

Two uses, both opt-in:
  * the applicant agent (jobhunt/apply/persona.py), which answers application questions from a
    cited knowledge base or declines, and
  * a one-paragraph "why this fits / doesn't" note for new postings (jobhunt.py scan --notes).

Backends: "claude-cli" shells out to the Claude Code CLI (`claude -p`), "anthropic" calls the
Messages API with ANTHROPIC_API_KEY. No SDKs required.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from typing import Optional


def task_cfg(cfg: dict, task: Optional[str] = None) -> dict:
    """[llm] with [llm.<task>] laid over it, so one job (the resume tailor, say) runs on its own model and
    effort while everything else keeps the default. Every task table is dropped from the result; _call
    only reads backend, model and effort."""
    base = {k: v for k, v in (cfg or {}).items() if not isinstance(v, dict)}
    over = (cfg or {}).get(task) if task else None
    if isinstance(over, dict):
        base.update({k: v for k, v in over.items() if not isinstance(v, dict)})
    return base


def label(cfg: dict) -> str:
    """'claude-fable-5-1 (low)': the model a record names as having made a call."""
    m = str((cfg or {}).get("model") or "default model")
    e = (cfg or {}).get("effort")
    return f"{m} ({e})" if e and (cfg or {}).get("backend") == "claude-cli" else m


def _call(cfg: dict, prompt: str, max_tokens: int = 400) -> Optional[str]:
    backend = (cfg.get("backend") or "none").lower()
    if backend == "none":
        return None
    if backend == "claude-cli":
        # A fresh, standalone CLI run: drop any CLAUDE* variables so a jobhunt started from inside a
        # Claude Code session does not attach to that session and answer the wrong conversation.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CLAUDE")}
        try:
            # the model and effort come from config.toml, never from whatever the person's Claude Code default is:
            # a rating is recorded as made by cfg["model"], so that must be the model that made it
            args = ["claude", "-p", prompt, "--output-format", "text"]
            if cfg.get("model"):
                args += ["--model", str(cfg["model"])]
            if cfg.get("effort"):
                args += ["--effort", str(cfg["effort"])]
            out = subprocess.run(args,
                                 capture_output=True, text=True, timeout=240, env=env, stdin=subprocess.DEVNULL)
            return out.stdout.strip() or None
        except Exception:  # noqa: BLE001
            return None
    if backend == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return None
        body = json.dumps({"model": cfg.get("model", "claude-sonnet-4-5"), "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            return "".join(b.get("text", "") for b in data.get("content", [])).strip() or None
        except Exception:  # noqa: BLE001
            return None
    return None


def _background(profile: dict, notes: str) -> str:
    """The applicant's facts: exactly the text of notes/about.md (what the About page shows)."""
    return (notes or "")[:8000]


def fit_note(cfg: dict, profile: dict, job: dict, notes: str = "") -> Optional[str]:
    llm = cfg.get("llm") or {}
    prompt = (
        "In 3 short sentences, say whether this job fits the applicant described below and why, then name the "
        "single biggest concern (travel, onsite, clearance, seniority, pay). Be blunt.\n\n"
        f"{job.get('company')} - {job.get('title')} ({job.get('location')})\n{(job.get('description') or '')[:3000]}\n\n"
        "About the applicant:\n" + _background(profile, notes)
    )
    return _call(llm, prompt, max_tokens=250)
