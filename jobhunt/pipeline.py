"""The pipeline: fetch the boards once, then classify and merge for each profile.

    scan     network -> data/postings.json -> per-profile scoring
    rescore  the same, replayed from the cache with no network

Everything here takes a Profile, which is why it lives apart from store.py. Keeping the two in one
module is what forced core.py and profiles.py to import each other."""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
from pathlib import Path
from typing import Callable, Optional

from . import audit
from .classify import facts as factsmod
from .classify import judge as judgemod
from .classify import relevance as relmod
from .classify import learn
from .classify.filters import Matcher
from .jsonfile import _read_json, _write_json, now_iso
from .profiles import Profile
from .fetch.sources import FetchError, Job, fetch_company, fetch_hn_who_is_hiring

RAW_FILE = "postings.json"
PARTIAL_FILE = ".fetch-partial.json"

# A scan is these, in order. Each one begins and ends somewhere nameable, can be run on its own, and
# reports its own progress. `scan` runs them all, or the subset `--stage` names, replaying the rest
# from the last run's files; `rescore` starts at `gate` from the cache with no network.
STAGES = ("plan", "fetch", "cache", "gate", "measure", "judge", "score", "store")
# The stages that read the fetched postings. Everything else works from the profile's own store,
# which is what lets `--stage judge` run without touching the 160MB cache at all.
NEEDS_POSTINGS = frozenset({"gate", "measure", "store"})


class Progress:
    """Where a run has got to, as structure rather than a wall of log lines.

    The dashboard used to show `log.join('\n')` and nothing else, so a scan was an opaque ten minutes.
    Every stage reports what it is and how far through, which is the difference between "still going"
    and "judging, 340 of 900"."""

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.stage: Optional[str] = None
        self.done = 0
        self.total = 0
        self.note = ""
        self.finished: list[dict] = []

    def begin(self, stage: str, total: int = 0, note: str = "") -> None:
        if self.stage:
            self.end()
        self.stage, self.done, self.total, self.note = stage, 0, total, note
        self.log(f"[{stage}] {note}" if note else f"[{stage}]")

    def step(self, n: int = 1, note: str = "") -> None:
        self.done += n
        if note:
            self.note = note
            self.log(f"  {note}")

    def end(self, note: str = "") -> None:
        if not self.stage:
            return
        self.finished.append({"stage": self.stage, "done": self.done, "total": self.total,
                              "note": note or self.note})
        if note:
            self.log(f"  {note}")
        self.stage = None

    def snapshot(self) -> dict:
        return {"stage": self.stage, "done": self.done, "total": self.total, "note": self.note,
                "finished": list(self.finished)}


def stage_plan(profiles: dict[str, Profile]) -> dict:
    """STAGE: what this run will touch — which boards, and the two predicates fetching needs.

    Pure: no network, no disk. Everything later in the run keys off what it decides here."""
    matchers = [Matcher(p.search) for p in profiles.values()]

    def any_title_ok(title: str) -> bool:
        return any(m.title_tier(title)[0] != 0 for m in matchers)

    def worth_caching(j: Job) -> bool:
        """Anything a profile could plausibly want later: everything but plainly foreign postings.
        Deliberately NOT the title gate -- see save_raw."""
        return any(m.location_fit(j)[0] != "non_us" for m in matchers)

    return {
        "companies": union_companies(profiles),
        "profiles": list(profiles),
        "title_filter": any_title_ok,
        "keep_for_cache": worth_caching,
        "hn": any(p.wants_source("hn") for p in profiles.values()),
        "hn_max": max([int(p.search.get("sources", {}).get("hn_max_comments", 3000))
                       for p in profiles.values()] or [3000]),
    }


# ----------------------------------------------------------------------------- fetching

def fetch_all(companies: list[dict], log: Callable[[str], None] = print, workers: int = 6,
              title_filter: Optional[Callable[[str], bool]] = None, hn: bool = False,
              hn_max_comments: int = 3000, env: Optional[dict] = None,
              root: Optional[Path] = None, resume: bool = True,
              prog: Optional[Progress] = None,
              quota_cfg: Optional[dict] = None) -> tuple[list[Job], dict[str, str]]:
    """STAGE: fetch every board once. Returns (jobs, {board name: error}).

    Boards are flushed to a partial file as they land, so an interrupted fetch resumes instead of
    throwing away ten minutes of network. The partial is deleted once the cache is written."""
    fetched: list[Job] = []
    errors: dict[str, str] = {}
    env = env or {}
    partial = (Path(root) / "data" / PARTIAL_FILE) if root else None
    done_boards: dict[str, list] = {}
    if partial and resume and partial.exists():
        try:
            saved = json.loads(partial.read_text(encoding="utf-8"))
            done_boards = saved.get("boards") or {}
            errors.update(saved.get("errors") or {})
            if done_boards:
                log(f"  resuming: {len(done_boards)} boards already fetched")
        except (OSError, json.JSONDecodeError):
            done_boards = {}

    def one(entry: dict):
        name = entry.get("name") or entry.get("board") or entry.get("slug")
        try:
            jobs = fetch_company(entry, title_filter=title_filter, env=env,
                                 quota_dir=(Path(root) / "data") if root else None,
                                 quota_cfg=quota_cfg)
            log(f"  {name}: {len(jobs)} postings")
            return name, jobs, None
        except FetchError as e:
            log(f"  {name}: ERROR {e}")
            return name, [], str(e)
        except Exception as e:  # noqa: BLE001
            log(f"  {name}: ERROR {type(e).__name__}: {e}")
            return name, [], f"{type(e).__name__}: {e}"

    todo = [c for c in companies if (c.get("name") or c.get("board") or c.get("slug")) not in done_boards]
    for name, rows in done_boards.items():
        fetched.extend(Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__}) for d in rows)
    log(f"Fetching {len(todo)} boards...")
    if prog:
        prog.begin("fetch", total=len(todo), note=f"{len(todo)} boards")
    since_flush = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for name, jobs, err in ex.map(one, todo):
            fetched.extend(jobs)
            done_boards[name] = [j.to_dict() for j in jobs]
            if err:
                errors[name] = err
            if prog:
                prog.step()
            since_flush += 1
            if partial and since_flush >= 10:          # checkpoint: a stopped fetch keeps its work
                _write_json(partial, {"boards": done_boards, "errors": errors}, compact=True)
                since_flush = 0
    if partial:
        _write_json(partial, {"boards": done_boards, "errors": errors}, compact=True)

    if hn:
        try:
            hn_jobs = fetch_hn_who_is_hiring(max_comments=hn_max_comments)
            log(f"  HN Who is hiring: {len(hn_jobs)} postings")
            fetched.extend(hn_jobs)
        except Exception as e:  # noqa: BLE001
            errors["hn"] = str(e)
            log(f"  HN: ERROR {e}")
    return fetched, errors


def save_raw(root: Path, jobs: list[Job], errors: dict, started: str,
             keep: Optional[Callable[[Job], bool]] = None) -> Path:
    """Cache the fetch for `rescore`.

    The cache is deliberately WIDE: everything that is not plainly outside the US is written, titles
    included that no profile's vocabulary wants. The title gate belongs at scoring time, where
    `rescore` can replay it offline. Caching post-gate, as this used to, meant that widening a
    keyword could never recover the postings it would have matched without a fresh network scan --
    roughly 15,000 of 18,500 postings a scan, gone. The file is ~100MB instead of a few MB, which is
    the right trade for a search that must not miss anything."""
    path = Path(root) / "data" / RAW_FILE
    recs = []
    for j in jobs:
        if keep and not keep(j):
            continue
        d = j.to_dict()
        d["description"] = d["description"][:12000]
        recs.append(d)
    _write_json(path, {"scanned_at": started, "errors": errors, "jobs": recs})
    partial = Path(root) / "data" / PARTIAL_FILE     # the fetch is banked; its resume file is spent
    if partial.exists():
        try:
            partial.unlink()
        except OSError:
            pass
    return path


def load_raw(root: Path) -> tuple[list[Job], dict, Optional[str]]:
    path = Path(root) / "data" / RAW_FILE
    if not path.exists():
        return [], {}, None
    data = _read_json(path, {"jobs": []})
    jobs = [Job(**{k: v for k, v in d.items() if k in Job.__dataclass_fields__}) for d in data.get("jobs", [])]
    return jobs, data.get("errors") or {}, data.get("scanned_at")


# ----------------------------------------------------------------------------- per-profile merge

def merge_into_store(profile: Profile, fetched: list[Job], started: str, errors: dict[str, str],
                     log: Callable[[str], None] = print, track_missing: bool = True,
                     prog: Optional[Progress] = None) -> dict:
    """Gate, measure, score and store every fetched posting for one profile."""
    prog = prog or Progress(log)
    prog.begin("gate", total=len(fetched), note=f"{profile.name}: {len(fetched)} postings")
    store = profile.store
    # Re-read first: a scan can run for an hour, and a decision made in the dashboard meanwhile must
    # not be overwritten by the copy of state.json this process loaded when it started.
    store.refresh()
    matcher = Matcher(profile.search)
    boards = profile.board_names()
    wants_hn = profile.wants_source("hn")
    model = learn.build_model(store.jobs["jobs"], store.state["jobs"])
    profile_facts(profile, model)        # refreshes the auto facts on the About page

    kept = dropped = 0
    new_ids: list[str] = []
    seen_now: set[str] = set()
    for job in fetched:
        prog.step()          # counted, not logged: 26,000 log lines would be worse than none
        if job.source == "hn":
            if not wants_hn:
                continue
        elif (job.board or job.company) not in boards:
            continue
        prev_rec = store.jobs["jobs"].get(job.id)
        cls = matcher.classify(job, first_seen=(prev_rec or {}).get("first_seen") or started)
        if not cls["keep"]:
            dropped += 1
            if prev_rec is not None and prev_rec.get("active", True):   # the search config changed: retire it now
                prev_rec["active"] = False
                prev_rec["drop_reason"] = cls.get("drop_reason")
            continue
        kept += 1
        seen_now.add(job.id)
        rec = job.to_dict()
        rec["snippet"] = rec["description"][:700]
        rec["description"] = rec["description"][:12000]
        rec.update({k: v for k, v in cls.items() if k not in ("keep", "drop_reason")})
        prev = store.jobs["jobs"].get(job.id)
        rec["first_seen"] = prev["first_seen"] if prev else started
        rec["last_seen"] = started
        rec["active"] = True
        rec["missing_scans"] = 0
        if not prev:
            new_ids.append(job.id)
        store.jobs["jobs"][job.id] = rec

    retire_excluded_companies(profile)
    collapse_variants(store.jobs["jobs"], seen_now, store.state["jobs"])

    # postings that vanished from their feed for 2 scans in a row are considered closed
    for jid, rec in store.jobs["jobs"].items():
        if jid in seen_now or not track_missing:
            continue
        src_name = rec.get("board") or rec.get("company")
        if src_name in errors or (rec.get("source") == "hn" and "hn" in errors):
            continue  # don't punish a posting because its board failed to load
        rec["missing_scans"] = rec.get("missing_scans", 0) + 1
        if rec["missing_scans"] >= 2:
            rec["active"] = False

    prog.end(f"{kept} kept, {dropped} dropped")
    prog.begin("score", note=profile.name)
    graded = score_relevance(profile)
    prog.end(f"{graded} graded")
    prog.begin("store", note=profile.name)
    store.save_learned(model)
    store.jobs["scanned_at"] = started
    summary = {"scanned_at": started, "profile": profile.name, "fetched": len(fetched), "kept": kept,
               "dropped": dropped, "new": len(new_ids), "errors": errors}
    store.jobs["last_summary"] = summary
    store.save()
    prog.end(f"{kept} kept, {dropped} filtered out, {len(new_ids)} new")
    log(f"  [{profile.name}] {kept} kept, {dropped} filtered out, {len(new_ids)} new")
    return {"new_ids": new_ids, **summary}


def collapse_variants(jobs: dict[str, dict], ids: set[str], state: Optional[dict] = None) -> None:
    """Companies often post one role once per city. Group postings with the same company and
    normalized title; one becomes the primary and lists the others as `variants`, the others point
    back with `duplicate_of` and are hidden from the default view.

    The primary is the posting you acted on (saved, skipped, applied...; the most recently updated if
    several), so a group stays in the tab you put it in across scans. Otherwise the best relevance
    wins, and postings the model has not read yet fall behind ones it has -- an unrated posting has
    no relevance at all, and treating that as a zero would let it beat a genuinely mediocre one. The
    final tiebreak is `first_seen`, so a group of equally unrated variants still picks the same
    primary every run instead of shuffling on id."""
    state = state or {}
    groups: dict[str, list[dict]] = {}
    for jid in ids:
        rec = jobs.get(jid)
        if rec:
            groups.setdefault(learn.dup_key(rec.get("company"), rec.get("title")), []).append(rec)

    for recs in groups.values():
        recs.sort(key=lambda r: (r.get("relevance") is None, -(r.get("relevance") or 0),
                                 r.get("first_seen") or "", r["id"]))
        acted = [r for r in recs if (state.get(r["id"]) or {}).get("status", "new") != "new"]
        if acted:
            acted.sort(key=lambda r: (state.get(r["id"]) or {}).get("updated_at") or "", reverse=True)
            primary = acted[0]
            rest = [r for r in recs if r is not primary]
        else:
            primary, rest = recs[0], recs[1:]
        primary["duplicate_of"] = None
        primary["variants"] = [{"id": r["id"], "location": r.get("location"), "location_fit": r.get("location_fit"),
                                "grade": r.get("grade"), "url": r.get("url"), "apply_url": r.get("apply_url")} for r in rest]
        for r in rest:
            r["duplicate_of"] = primary["id"]
            r["variants"] = []


def profile_facts(profile: Profile, model: Optional[dict] = None) -> list:
    """The profile's facts, with the auto facts refreshed from `model` when one is given."""
    facts = factsmod.read_facts(profile.dir)
    if model is not None:
        auto = factsmod.auto_facts(model.get("companies", {}), profile.display_name,
                                   factsmod.load_suppressed(profile.data_dir))
        facts, changed = factsmod.merge_auto_facts(facts, auto)
        if changed:
            factsmod.write_facts(profile.dir, facts)
    return facts


def _note_facts(profile: Profile, facts: list) -> None:
    """Record an About-page change. Detected by comparing against the last recorded hash rather than
    by hooking the writers, so editing about.md in a text editor is caught just the same."""
    sha = factsmod.facts_sha(facts)
    prev = audit.last(profile.data_dir, "facts_changed")
    if prev and prev.get("sha") == sha:
        return
    before = [factsmod.Fact(t) for t in (prev or {}).get("texts") or []]
    diff = factsmod.facts_diff(before, facts) if prev else {"added": [f.text for f in facts], "removed": []}
    audit.append(profile.data_dir, "facts_changed", sha=sha, was=(prev or {}).get("sha"),
                 n=len(facts), added=diff["added"], removed=diff["removed"],
                 texts=[f.text for f in facts])


def retire_excluded_companies(profile: Profile) -> int:
    """Deactivate stored postings whose company is on `company_exclude`.

    `classify` only ever sees postings that came back in the latest fetch, so adding an employer to
    the list (or disabling its board) would otherwise strand everything already stored: still active,
    never re-evaluated, never removed. Returns how many were retired."""
    excluded = [t.lower() for t in ((profile.search.get("search") or {}).get("company_exclude") or [])]
    if not excluded:
        return 0
    n = 0
    for rec in profile.store.jobs["jobs"].values():
        if not rec.get("active", True):
            continue
        company = (rec.get("company") or "").lower()
        hit = next((t for t in excluded if t in company), None)
        if hit:
            rec["active"] = False
            rec["drop_reason"] = f"excluded company ({hit})"
            n += 1
    return n


ENTERPRISE_RATING = 7
ENTERPRISE_PAY_FLOOR = 0          # off until a profile sets [enterprise] pay_floor


def enterprise_cfg(search_cfg: dict) -> tuple[int, int]:
    """[enterprise] rating and pay_floor; either at 0 turns the gate off."""
    e = search_cfg.get("enterprise") or {}
    return int(e.get("rating", ENTERPRISE_RATING)), int(e.get("pay_floor", ENTERPRISE_PAY_FLOOR))


def retire_enterprise(profile: Profile) -> int:
    """The one gate that fires on a model reading: a posting the model rates
    `enterprise_software` at `rating` or above is retired unless it states a base above `pay_floor`.

    It runs after the ratings are attached, so it fires in scan, rescore and relearn alike. Silence is
    0 on that attribute and never trips it; unstated pay is not above the floor. Decided
    postings are retired the way every gate retires them: they stay in the tab you put them in.
    Weight 0 on the attribute means the same reading can never also demote a posting it kept."""
    rating, floor = enterprise_cfg(profile.search)
    if rating <= 0 or floor <= 0:
        return 0
    n = 0
    for rec in profile.store.jobs["jobs"].values():
        if not rec.get("active", True):
            continue
        v = (rec.get("role") or {}).get("enterprise_software")
        if v is None or float(v) < rating:
            continue
        top = (rec.get("salary") or {}).get("max")
        if top and int(top) > floor:
            continue
        rec["active"] = False
        rec["drop_reason"] = f"enterprise software (rated {int(round(float(v)))}), no stated base above ${floor // 1000}k"
        line = f"dropped: {rec['drop_reason']}"
        rec["reasons"] = [x for x in (rec.get("reasons") or []) if x != line] + [line]
        n += 1
    return n


def relearn(profile: Profile) -> dict:
    """Rebuild the learned model from the profile's decisions, refresh the auto facts, re-grade.

    This does NOT re-read postings. Editing a fact changes what the model should have been told, and
    only a judge pass can act on that: the scan reports how many ratings went stale and reads them
    again. What happens here is arithmetic, plus the auto facts the decisions imply."""
    store = profile.store
    store.refresh()
    retire_excluded_companies(profile)
    model = learn.build_model(store.jobs["jobs"], store.state["jobs"])
    profile_facts(profile, model)
    for rec in store.jobs["jobs"].values():
        rec.pop("facts", None)           # the regex scorer's leftovers; nothing reads them now
    score_relevance(profile)
    store.save_learned(model)
    store.save_jobs()
    return model


# Bands used to be named for distance; they are named for how often you could show up. A stored
# posting only gets re-banded when the fetch that produced it comes round again, and a board that
# fails, or a posting that has since closed, would otherwise keep a name nothing understands --
# `quarterly()` would not match it, so it would sit in the New tab forever.
OLD_BANDS = {"local": "daily", "commutable": "monthly", "commutable_onsite": "monthly",
             "regional": "quarterly", "in_radius": "quarterly", "us_onsite": "relocate",
             "remote_us": "remote"}


def migrate_bands(jobs: dict) -> int:
    """Rename any band left over from the distance vocabulary. Idempotent."""
    n = 0
    for rec in jobs.values():
        new = OLD_BANDS.get(rec.get("location_fit"))
        if new:
            rec["location_fit"] = new
            n += 1
        for v in rec.get("variants") or []:
            alt = OLD_BANDS.get(v.get("location_fit"))
            if alt:
                v["location_fit"] = alt
    return n


def score_relevance(profile: Profile) -> int:
    """Attach the model's ratings, then give every posting a relevance total and a grade.

    Reads the ratings cache rather than trusting what is attached to the records, because the cache
    is authoritative and a judging run checkpoints into it: this stays correct while one is still
    going. Also the one place that records a weight change or a curve refit in the audit."""
    renamed = migrate_bands(profile.store.jobs["jobs"])
    if renamed:
        print(f"  [{profile.name}] renamed {renamed} postings from the old distance bands")
    moved = judgemod.migrate_fit(profile.data_dir, profile.store.jobs["jobs"])
    if moved:
        print(f"  [{profile.name}] migrated {moved} fit ratings into judgements as overall_fit")
    judgemod.attach(profile.store.jobs["jobs"], judgemod.load(profile.data_dir))
    retired = retire_enterprise(profile)
    if retired:
        print(f"  [{profile.name}] retired {retired} enterprise-software postings with no stated base above the floor")
    weights, _notes = relmod.load_weights(profile.search)
    audit.note_change(profile.data_dir, "weights_changed", "sha", relmod.weights_sha(weights),
                      weights=weights)
    before, _had = relmod.load_curve(profile.data_dir)
    graded, cuts = relmod.apply_to(profile.store.jobs["jobs"], profile.search, profile.data_dir)
    if cuts and list(cuts) != list(before):
        # no posting's relevance moved; what a given relevance is CALLED moved
        audit.append(profile.data_dir, "curve_refit", cutoffs=[round(c, 1) for c in cuts],
                     was=[round(c, 1) for c in before] or None, n=graded,
                     weights_sha=relmod.weights_sha(weights))
    return graded


def judge_candidates(jobs: dict, state: dict, cache: dict, limit: int = 0,
                     search_cfg: Optional[dict] = None,
                     facts_sha: Optional[str] = None) -> list[tuple[dict, list[str]]]:
    """(posting, attributes needing a rating) for everything worth reading, best prospect first.

    A posting already rated under the current revisions is skipped entirely; one whose text changed,
    or which predates a reworded attribute, comes back with only the slots that actually went stale.

    Order matters because `--judge-limit` cuts the tail off, so the head has to be the postings most
    worth spending a model call on. It used to be the old additive `score`, which is a rating the new
    pipeline is in the middle of deleting -- and which, for an unjudged posting, was mostly the LLM
    fit term being absent. The practical measures are the honest choice: location, pay, clearance,
    travel, freshness and coast are all read off the posting deterministically, so they are fully
    known before any rating exists, and they are already weighted the way the final score weights
    them. Recency stays the first key, because a stale posting is often already filled."""
    weights, _n = relmod.load_weights(search_cfg or {})
    order = {"fresh": 0, "mid": 1, "stale": 2}
    rows = []
    for jid, rec in jobs.items():
        if not rec.get("active", True) or rec.get("duplicate_of"):
            continue
        if (state.get(jid) or {}).get("status", "new") not in ("new", "saved", "applied"):
            continue
        stale = judgemod.stale_dims(rec, cache.get(jid), facts_sha=facts_sha)
        if stale:
            rows.append((rec, stale))

    def prospect(rec: dict) -> float:
        parts = relmod.practical_measures(rec, search_cfg or {})
        return sum(weights["practical"].get(d, 0) * m for d, (m, _w) in parts.items())

    rows.sort(key=lambda r: (order.get((r[0].get("recency") or {}).get("level"), 1),
                             -prospect(r[0]), r[0]["id"]))
    return rows[:limit] if limit > 0 else rows


def judge_pass(profile: Profile, log: Callable[[str], None] = print, limit: int = 0,
               llm_cfg: Optional[dict] = None, dry_run: bool = False) -> dict:
    """STAGE: rate the role attributes of every posting that needs it, and cache the result.

    The only pass that asks the model anything. What comes back is eleven signed measures per
    posting, which `score` turns into a relevance total; nothing here decides whether a posting is
    shown, and nothing here writes a score."""
    t0 = time.monotonic()
    store = profile.store
    store.refresh()
    llm_cfg = llm_cfg if llm_cfg is not None else (profile.search.get("llm") or {})
    judgemod.migrate_fit(profile.data_dir, store.jobs["jobs"])
    cache = judgemod.load(profile.data_dir)
    facts_list = profile_facts(profile)
    facts_now = factsmod.facts_sha(facts_list)
    _note_facts(profile, facts_list)
    restale = sum(1 for jid, j in cache.items()
                  if j.get("facts_sha") and j["facts_sha"] != facts_now and jid in store.jobs["jobs"])
    if restale:
        log(f"  [{profile.name}] the About page changed: {restale} ratings were made under older "
            f"facts and will be read again")
    todo = judge_candidates(store.jobs["jobs"], store.state["jobs"], cache, limit=limit,
                            search_cfg=profile.search, facts_sha=facts_now)
    slots = sum(len(dims) for _r, dims in todo)
    if dry_run or not todo:
        return {"postings": len(todo), "slots": slots, "rated": 0, "dry_run": dry_run}
    if (llm_cfg.get("backend") or "none").lower() == "none":
        log(f"  [{profile.name}] judge pass skipped: no LLM backend")
        return {"postings": len(todo), "slots": slots, "rated": 0, "skipped": True}
    log(f"  [{profile.name}] judging {len(todo)} postings ({slots} attribute slots)...")
    rated = 0
    by_dims: dict[tuple, list[dict]] = {}
    for rec, dims in todo:
        by_dims.setdefault(tuple(dims), []).append(rec)
    by_id = {r["id"]: r for r, _d in todo}

    def bank(got: dict) -> None:
        """Write one batch's ratings straight to disk. Rating a corpus is the expensive part of a
        scan, so a batch that has been paid for has to survive the process dying on the next one."""
        nonlocal rated
        now = judgemod.datetime.now(judgemod.timezone.utc).replace(microsecond=0).isoformat()
        for jid, new in got.items():
            r = by_id.get(jid)
            if r is None:
                continue
            prev = cache.get(jid) or {}
            cache[jid] = {"v": {**(prev.get("v") or {}), **new["v"]},
                          "rev": {**(prev.get("rev") or {}),
                                  **{d: judgemod.DIM_REVS[d] for d in new["v"]}},
                          "why": new.get("why") or prev.get("why", ""),
                          "model": llm_cfg.get("model"), "text_sha": judgemod.text_sha(r),
                          "facts_sha": facts_now, "prompt_sha": judgemod.prompt_sha(), "at": now}
            rated += 1
        judgemod.save(profile.data_dir, cache)
        log(f"  [{profile.name}] {rated}/{len(todo)} rated")

    # Postings needing the same attributes share a prompt, so one reworded attribute costs a slot
    # per posting rather than a whole re-read.
    for dims, recs in by_dims.items():
        posts = [{"id": r["id"], "title": r.get("title"), "company": r.get("company"),
                  "description": r.get("description") or r.get("snippet") or ""} for r in recs]
        judgemod.judge(llm_cfg, profile.notes, posts, dims=list(dims), log=log,
                       name=profile.display_name, on_batch=bank)
    judgemod.attach(store.jobs["jobs"], cache)
    store.save_jobs()
    audit.append(profile.data_dir, "judged", postings=rated, of=len(todo), slots=slots,
                 model=llm_cfg.get("model"), backend=llm_cfg.get("backend"),
                 prompt_sha=judgemod.prompt_sha(), facts_sha=facts_now,
                 seconds=round(time.monotonic() - t0, 1))
    log(f"  [{profile.name}] judged {rated} postings")
    return {"postings": len(todo), "slots": slots, "rated": rated}


# ----------------------------------------------------------------------------- orchestration

def feed_key(c: dict) -> tuple:
    """What actually identifies a feed, so the same board is fetched once.

    It used to be a JSON blob of seven fields including `name`, which meant the same board listed
    under two spellings -- "Scale AI" and "Scale AI, Inc." -- was two boards and got fetched twice.
    Harmless at 241 hand-written entries; not once discovery is adding them by the hundred. The
    address is the identity: for the feed ATSes that is the token, for Workday the tenant and site,
    and for a keyword source the queries it runs."""
    ats = (c.get("ats") or "").lower()
    if ats == "lever":
        return (ats, (c.get("slug") or c.get("board") or "").lower())
    if ats == "page":
        return (ats, (c.get("url") or "").lower().rstrip("/"))
    if c.get("board"):
        # every per-employer feed (greenhouse, ashby, smartrecruiters, workable, breezy, recruitee,
        # bamboohr, rippling): the board is the address. Keying only greenhouse and ashby by board let
        # every other platform's boards collapse onto one key, so only the first was ever fetched.
        return (ats, (c.get("board") or "").lower())
    if ats == "workday":
        return (ats, (c.get("tenant") or "").lower(), (c.get("wd") or "").lower(), (c.get("site") or "").lower())
    # aggregators and usajobs: one entry per distinct query set
    return (ats, json.dumps(c.get("queries") or [], sort_keys=True), json.dumps(c.get("locations") or [], sort_keys=True))


def union_companies(profiles: dict[str, Profile]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for p in profiles.values():
        for c in p.companies():
            seen.setdefault(feed_key(c), c)
    return list(seen.values())


def scan(root: Path, profiles: dict[str, Profile], log: Callable[[str], None] = print, workers: int = 12,
         env: Optional[dict] = None, judge: bool = True, judge_limit: int = 0,
         stages: Optional[tuple[str, ...]] = None, prog: Optional[Progress] = None) -> dict:
    """A full pipeline run: plan, fetch, cache, then gate/measure/judge/score/store per profile.

    Every stage is a named function you can call on its own; this only puts them in order. `stages`
    restricts the run to a subset of STAGES, which is what `--stage` passes; the stages it leaves out
    fall back to whatever the last run left on disk."""
    want = set(stages or STAGES)
    prog = prog or Progress(log)
    started = now_iso()

    prog.begin("plan")
    plan = stage_plan(profiles)
    prog.end(f"{len(plan['companies'])} boards for {', '.join(plan['profiles'])}")

    if "fetch" in want:
        # Workday and USAJOBS charge a request per posting for the description, so they keep the
        # title filter as a volume control; every other source is cached whole.
        fetched, errors = fetch_all(plan["companies"], log=log, workers=workers,
                                    title_filter=plan["title_filter"], hn=plan["hn"],
                                    hn_max_comments=plan["hn_max"], env=env, root=root, prog=prog,
                                    quota_cfg=next(iter(profiles.values())).search if profiles else None)
        prog.end(f"{len(fetched)} postings, {len(errors)} boards failed")
        if "cache" in want:
            prog.begin("cache")
            save_raw(root, fetched, errors, started, keep=plan["keep_for_cache"])
            prog.end()
    elif NEEDS_POSTINGS & want:
        # only read the cache back when a stage in this run actually consumes postings; it is
        # ~160MB, and `--stage judge` works entirely from what is already in the store
        fetched, errors, cached_at = load_raw(root)
        started = cached_at or started
        log(f"  replaying {len(fetched)} cached postings (fetched {cached_at})")
    else:
        fetched, errors = [], {}

    results: dict[str, dict] = {name: {} for name in profiles}
    if NEEDS_POSTINGS & want:
        for name, p in profiles.items():
            results[name] = merge_into_store(p, fetched, started, errors, log=log, prog=prog,
                                             track_missing="fetch" in want)
    if judge and "judge" in want:
        for name, p in profiles.items():
            prog.begin("judge", note=name)
            results[name]["judged"] = judge_pass(p, log=log, limit=judge_limit)["rated"]
            prog.end()
            # the ratings that just landed are what `score` exists to weigh, so weigh them
            prog.begin("score", note=f"{name}: after judging")
            prog.end(f"{score_relevance(p)} graded")
            p.store.save_jobs()
    prog.end()
    for name, p in profiles.items():
        audit.append(p.data_dir, "scan", scanned_at=started, boards=len(plan["companies"]),
                     fetched=len(fetched), failed=sorted(errors),
                     stages=sorted(want - (set() if judge else {"judge"})),
                     counts={k: v for k, v in results.get(name, {}).items()
                             if isinstance(v, (int, str))})
    log("Done.")
    return {"scanned_at": started, "fetched": len(fetched), "errors": errors, "profiles": results,
            "progress": prog.snapshot()}


def rescore(root: Path, profiles: dict[str, Profile], log: Callable[[str], None] = print,
            prog: Optional[Progress] = None) -> dict:
    """Re-classify from the cached raw postings (no network). Use after editing search.toml.

    Takes a Progress for the same reason `scan` does: reading a 180MB cache and re-gating 26,000
    postings takes long enough that the page has to be able to say so."""
    prog = prog or Progress(log)
    prog.begin("cache", note="reading the posting cache")
    fetched, errors, scanned_at = load_raw(root)
    prog.end(f"{len(fetched)} postings")
    if not fetched:
        log("no cached postings in data/postings.json — run a scan first")
        return {"error": "no cached postings"}
    log(f"Rescoring {len(fetched)} cached postings (fetched {scanned_at})...")
    results = {}
    for name, p in profiles.items():
        results[name] = merge_into_store(p, fetched, scanned_at or now_iso(), errors, log=log,
                                         track_missing=False, prog=prog)
    prog.end()
    return {"scanned_at": scanned_at, "fetched": len(fetched), "errors": errors, "profiles": results,
            "progress": prog.snapshot()}
