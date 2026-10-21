"""What the model makes of a posting, as a dozen signed ratings rather than one verdict.

The old fit pass asked for a single 0-10 and a sentence. That number then chose a score band outright,
which made it the only thing that mattered and left nothing to tune. Here the model rates the
role attributes in relevance.ROLE_DIMS from -10 to +10, and the weights decide what to do with them.

Judgements live in `data/judgements.json`, apart from the old `fit.json` so the two can run side by
side while the new scoring is still being trusted. Invalidation is **per posting and per dimension**:
each record carries the revision of every dimension it was rated under and a hash of the text it was
rated from. Adding a new attribute therefore re-rates one slot per posting rather than throwing
the corpus away, which is what the single global prompt version in fit.py does today.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import llm
from .relevance import ROLE_DIMS

JUDGEMENTS_FILE = "judgements.json"
SCHEMA = 1

# The model's single read of the whole role, rated alongside the role attributes but kept OUT of
# ROLE_DIMS on purpose: it is scored as its own term at its own weight, not as one of the family.
# It still has to be scheduled for rating, hence ALL_DIMS.
OVERALL_DIM = "overall_fit"
OVERALL_RUBRIC = ("+10 they should apply today; 0 possible but unremarkable; "
                  "-10 not worth their time. Your whole-role verdict, not an average of the rest.")
ALL_DIMS: list[str] = list(ROLE_DIMS) + [OVERALL_DIM]


def rubric(dim: str) -> str:
    return OVERALL_RUBRIC if dim == OVERALL_DIM else ROLE_DIMS[dim][1]


# Bump one dimension's revision when you reword what it means; only that slot is re-rated.
DIM_REVS: dict[str, int] = {name: 1 for name in ALL_DIMS}
DIM_REVS["seniority_fit"] = 2       # 20 Oct 2026: staff +10, senior +5, anything else -10
# domain, work_shape and enterprise_software are new on 20 Oct 2026 (rev 1); the attributes they
# replaced (agentic_llm, autonomy_robotics, mission_domain, variety, toil, independence,
# forward_deployed, algorithmic_depth) are no longer in ALL_DIMS, so their stored ratings are ignored.
BATCH = 4
MAX_FAILURES = 3
DESC_CHARS = 3000


def text_sha(rec: dict) -> str:
    """Identifies the text a rating was made from, so an edited posting re-rates itself alone."""
    body = f"{rec.get('title')}\n{rec.get('company')}\n{(rec.get('description') or '')[:DESC_CHARS]}"
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]


def prompt_sha() -> str:
    """Identifies the wording the model was given. Reworded rubrics already invalidate their own
    slot through DIM_REVS; this is for the audit trail, which has to be able to say which prompt
    produced a number without inferring it from a date."""
    body = PROMPT + "|" + "|".join(f"{d}:{rubric(d)}" for d in ALL_DIMS)
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:12]


def load(data_dir: Path) -> dict:
    p = Path(data_dir) / JUDGEMENTS_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("postings") or {} if int(data.get("schema") or 0) == SCHEMA else {}


def save(data_dir: Path, postings: dict) -> None:
    p = Path(data_dir) / JUDGEMENTS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": SCHEMA, "dims": DIM_REVS,
                               "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                               "postings": postings}, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(p)


def stale_dims(rec: dict, judged: Optional[dict], facts_sha: Optional[str] = None) -> list[str]:
    """Which attributes of this posting need rating.

    Everything, if it has never been read, if the posting's own text has changed, or if the facts the
    model was told have changed -- a rating is a judgement of a posting AGAINST a person, so editing
    the person invalidates it exactly as editing the posting does. Otherwise only the slots whose
    rubric was reworded since, which is what keeps adding a new attribute from costing a full
    re-read of the corpus.

    `facts_sha=None` means "do not check the facts", which is what a caller that has no profile in
    hand passes; ratings made before this field existed carry no facts_sha and are not re-read for
    the lack of one."""
    if not judged:
        return list(ALL_DIMS)
    if judged.get("text_sha") != text_sha(rec):
        return list(ALL_DIMS)
    if facts_sha and judged.get("facts_sha") and judged["facts_sha"] != facts_sha:
        return list(ALL_DIMS)
    revs = judged.get("rev") or {}
    have = judged.get("v") or {}
    return [d for d in ALL_DIMS if revs.get(d) != DIM_REVS[d] or d not in have]


def attach(jobs: dict, cache: dict) -> None:
    """Put each posting's rated attributes on its record, or take them off if they have gone."""
    for jid, rec in jobs.items():
        got = cache.get(jid)
        if got and got.get("v"):
            rec["role"] = dict(got["v"])
            rec["role_why"] = got.get("why", "")
        else:
            rec.pop("role", None)
            rec.pop("role_why", None)


PROMPT = """You are rating job postings for {name}, whose own description of themselves is below.

For EACH of the {n} postings, rate every attribute from -10 to +10 and give one short sentence.

    +10  strongly true of this role, and good for them
      0  the attribute is ABSENT from the posting, or simply neutral
    -10  strongly true in the direction that is bad for them

Zero is not a hedge. Use it when the posting says nothing about the attribute. Only go negative when
the posting positively shows the bad end: a role that never mentions hours is 0 for pace, while a
role that says "wartime" and weekends is strongly negative.

Rate the ROLE, not the company's fame, and not where the job is. Location, pay, clearance and travel
are measured separately from the posting and are not your job.

THE ATTRIBUTES
{dims}

OUTPUT
A JSON array, one object per posting, nothing else:
{{"i": <posting number>, {example}, "why": "<one short sentence>"}}

ABOUT {name}
{facts}

POSTINGS
{postings}"""


def _prompt(name: str, facts: str, postings: list[dict], dims: list[str]) -> str:
    dim_lines = "\n".join(f"  {d}: {rubric(d)}" for d in dims)
    example = ", ".join(f'"{d}": <-10..10>' for d in dims[:3]) + (", ..." if len(dims) > 3 else "")
    body = "\n\n".join(
        f"{i + 1}. {p.get('title')} — {p.get('company')}\n{(p.get('description') or '')[:DESC_CHARS]}"
        for i, p in enumerate(postings))
    return PROMPT.format(name=name, n=len(postings), dims=dim_lines, example=example,
                         facts=(facts or "")[:6000], postings=body)


def judge(llm_cfg: dict, facts: str, postings: list[dict], dims: Optional[list[str]] = None,
          batch: int = BATCH, log: Callable[[str], None] = lambda _s: None,
          name: str = "the applicant",
          on_batch: Optional[Callable[[dict], None]] = None) -> dict:
    """postings: [{id, title, company, description}]. Returns {id: {dim: measure, ...}} for what parsed.

    `on_batch` is handed each batch's results as they land, so the caller can bank them. Rating a
    corpus is the expensive part of a scan and a batch that has been paid for should survive the
    process dying on the next one."""
    dims = list(dims or ALL_DIMS)
    out: dict = {}
    if (llm_cfg.get("backend") or "none").lower() == "none":
        return out
    failures = 0
    for start in range(0, len(postings), batch):
        chunk = postings[start:start + batch]
        items = None
        for _attempt in range(2):
            raw = llm._call(llm_cfg, _prompt(name, facts, chunk, dims), max_tokens=350 * len(chunk))
            m = re.search(r"\[.*\]", raw or "", re.S)
            if not m:
                continue
            try:
                items = json.loads(m.group(0))
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(items, list):
            failures += 1
            log(f"  judge: batch {start // batch + 1} returned nothing usable")
            if failures >= MAX_FAILURES:
                log("  judge: stopping early, the model is not answering; the rest waits for next time")
                break
            continue
        failures = 0
        got: dict = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                idx = int(it.get("i")) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(chunk):
                continue
            vals = {}
            for d in dims:
                try:
                    vals[d] = max(-10, min(10, int(round(float(it[d])))))
                except (KeyError, TypeError, ValueError, OverflowError):   # json accepts Infinity; NaN too
                    continue
            if vals:
                got[chunk[idx]["id"]] = {"v": vals, "why": re.sub(r"\s+", " ", str(it.get("why") or "")).strip()[:240]}
        out.update(got)
        if on_batch and got:
            on_batch(got)
        log(f"  judge: rated {min(start + batch, len(postings))}/{len(postings)}")
    return out


FIT_FILE = "fit.json"
MIGRATED_FILE = "fit.migrated.json"


def migrate_fit(data_dir: Path, jobs: dict) -> int:
    """Carry the old fit pass's 0-10 ratings over as `overall_fit`, once, then retire fit.json.

    The two passes asked the same question of the same text, so re-reading 1,000 postings to get a
    number we already have would be waste. 0-10 maps onto -10..+10 as `(fit - 5) * 2`: a 5 was the old
    rubric's "possible but with a real concern", which is this scale's honest zero.

    The one thing that does not survive is the text the rating was made from -- fit.json never stored
    a hash -- so a migrated rating is stamped with the posting's text as it stands now. A posting
    edited between the fit pass and the migration therefore keeps a rating made from the older text.
    The window is one run, and everything else about the posting is re-rated anyway, because the ten
    role attributes are missing from the record and come back stale.

    Returns how many ratings moved. Idempotent: once fit.json is renamed there is nothing to do."""
    src = Path(data_dir) / FIT_FILE
    if not src.exists():
        return 0
    try:
        old = json.loads(src.read_text(encoding="utf-8")).get("postings") or {}
    except (OSError, json.JSONDecodeError):
        old = {}
    cache = load(data_dir)
    moved = 0
    for jid, rec in old.items():
        try:
            measure = max(-10, min(10, int(round((float(rec["fit"]) - 5.0) * 2.0))))
        except (KeyError, TypeError, ValueError):
            continue
        job = jobs.get(jid)
        cur = cache.get(jid) or {}
        if (cur.get("v") or {}).get(OVERALL_DIM) is not None:
            continue                       # already rated on the new scale; the old number is stale
        cache[jid] = {
            "v": {**(cur.get("v") or {}), OVERALL_DIM: measure},
            "rev": {**(cur.get("rev") or {}), OVERALL_DIM: DIM_REVS[OVERALL_DIM]},
            "why": cur.get("why") or rec.get("why", ""),
            "model": cur.get("model"),
            "text_sha": cur.get("text_sha") or (text_sha(job) if job else ""),
            "at": cur.get("at") or rec.get("at"),
            "from": "fit.json",            # provenance: this one was not rated under the new prompt
        }
        moved += 1
    if moved:
        save(data_dir, cache)
    try:
        src.replace(Path(data_dir) / MIGRATED_FILE)
    except OSError:
        pass
    return moved
