"""How relevant a posting is: a weighted sum over attributes, shown as a grade.

Every attribute is a **signed measure** in -10..+10. The sign says whether it helps or hurts, and
**zero means the attribute is absent, neutral, or unknown**. Every weight is 0..10 and says how much
that attribute matters to you.

    relevance = sum(effective weight * measure)

Signed measures are the point of the design. A job can be excellent in some respects and bad in
others and those must subtract: a posting that requires being in an office loses relevance rather
than merely gaining less than a remote one. A 0..10 "desirability" axis cannot express that, because
absent and terrible would have to share an end of the scale. Zero meaning absent keeps them apart, so
an attribute nobody mentioned costs a posting nothing.

The total has no natural units, so it is never shown as a score. `grade()` maps it onto A+..F using
cutoffs fitted to the distribution actually observed, which is the only honest way to read a number
whose scale moves every time a weight changes.

Two kinds of attribute:

    role       what the work is. The model reads the posting and rates these (see judge.py).
    practical  where it is, what it pays, what it demands. Measured here, never guessed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


# name -> (default weight, what the model should mean by +10 and by -10)
ROLE_DIMS: dict[str, tuple[int, str]] = {
    "product_ownership": (10, "+10 owns something users touch and ships it; -10 internal plumbing nobody sees"),
    "domain":            (6, "+10 agentic AI or LLM systems that take actions, autonomy, perception, robotics, "
                             "simulation, computer vision, defense, government, space, manufacturing, healthcare, "
                             "financial infrastructure, trading or financial crime, audio, materials or chemistry, "
                             "or any software answering a direct real-world need; 0 none of that; "
                             "-10 consumer apps or consumer lending"),
    "feedback_loop":     (6, "+10 you change something and see the result within hours or days (vision, audio or "
                             "signal output, agent traces, a customer who tells you tomorrow); -10 the outcome is "
                             "weeks or quarters away (training from scratch, offline research, platform work "
                             "judged by quarterly metrics)"),
    "seniority_fit":     (6, "+10 staff, lead or principal scope as an individual contributor; +5 senior individual "
                             "contributor; 0 the level is not stated; -10 junior or mid-level, or people management"),
    "work_shape":        (2, "+10 a broad, changing, greenfield remit where you find your own approach; -10 one "
                             "narrow lane of release chores, patching, ticket queues or on-call, on a plan someone "
                             "else wrote"),
    "pace":              (2, "+10 a steady, sustainable pace the posting states or implies (40-50 hour weeks, "
                             "deliberate, long-tenured team); -10 hair-on-fire: 'wartime', 'scrappy', 'move fast', "
                             "weekends, 60-hour weeks; 0 nothing said"),
    "equity_stage":      (2, "+10 a venture-backed Series A-C company offering meaningful equity; 0 stage unknown "
                             "or not mentioned; -10 public, private-equity-owned, bootstrapped services, or "
                             "contract work with no upside"),
    # Weight 0 on purpose: this one is read by a GATE (pipeline.retire_enterprise), never by the
    # score, so it can retire a posting but can never also demote one. See AGENTS.md.
    "enterprise_software": (0, "+10 the employer's product is business software sold to other companies to run "
                               "their business (CRM, ERP, HR, ITSM, analytics, data or developer platforms, "
                               "security, martech, sales or support tooling); 0 cannot tell; -10 consumer, "
                               "hardware, defense, robotics, a research lab, or software that runs a physical or "
                               "real-world system"),
}

# The 20 Oct 2026 consolidation: pay, remote, ownership and travel on top, freshness just under.
# agentic_llm, autonomy_robotics and mission_domain collapsed into `domain`; variety, toil and
# independence into `work_shape`; forward_deployed and algorithmic_depth removed; seniority_fit
# reworded to a near-binary (staff, would accept senior). enterprise_software was added for the gate.
# feedback_loop, pace and equity_stage are things a posting can show about the work: a change you can
# see land within days, a sustainable pace, and a stake with upside.

# Role attributes each count on their own (weight x measure), so a second-tier attribute can carry
# real weight; they used to be averaged into one family term, which capped every one of them near
# +-12 and made the table read as if 10 x 8 were 11.9. role_weight is a multiplier on the whole role
# family: 10 means "as weighted".
DEFAULT_ROLE_WEIGHT = 10
DEFAULT_OVERALL_WEIGHT = 10

# What the posting's circumstances are worth, against the work itself. 20 Oct 2026: pay, location and
# travel sit at the top with ownership and the holistic read; freshness just under them, because a
# posting under a week old is the one worth the hour and a month-old one is probably filled. Travel is
# never positive now (any travel is a cost) but stays a weight and not a gate: silence is common and
# must cost nothing. east_coast went: under remote_or_daily only remote and the home metro survive the
# location gate, so there is no office timezone to weigh.
PRACTICAL_DIMS: dict[str, int] = {
    "pay": 10, "location": 10, "travel": 10, "freshness": 8, "clearance": 3,
}

GRADES = ("A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D", "F")


def default_weights() -> dict:
    return {"role": {k: w for k, (w, _why) in ROLE_DIMS.items()},
            "practical": dict(PRACTICAL_DIMS),
            "role_weight": DEFAULT_ROLE_WEIGHT, "overall_fit": DEFAULT_OVERALL_WEIGHT}


def load_weights(search_cfg: Optional[dict]) -> tuple[dict, list[str]]:
    """Effective weights, and any complaints about the config.

    Always starts from the defaults and overlays what is configured. `merge_config` replaces nested
    tables wholesale, so a profile that names three of the ten role weights would otherwise silently
    zero the other seven."""
    out = default_weights()
    notes: list[str] = []
    cfg = ((search_cfg or {}).get("scoring") or {})
    for family in ("role", "practical"):
        for key, raw in (cfg.get(family) or {}).items():
            if key not in out[family]:
                notes.append(f"[scoring.{family}] {key} is not an attribute; ignored")
                continue
            try:
                w = int(round(float(raw)))                  # 7.9 means 8, not 7
            except (TypeError, ValueError, OverflowError):
                notes.append(f"[scoring.{family}] {key} = {raw!r} is not a number; using {out[family][key]}")
                continue
            if not 0 <= w <= 10:
                notes.append(f"[scoring.{family}] {key} = {w} is outside 0..10; clamped")
                w = max(0, min(10, w))
            out[family][key] = w
    for key, default in (("role_weight", DEFAULT_ROLE_WEIGHT), ("overall_fit", DEFAULT_OVERALL_WEIGHT)):
        if key in cfg:
            try:
                v = int(round(float(cfg[key])))
            except (TypeError, ValueError, OverflowError):
                notes.append(f"[scoring] {key} = {cfg[key]!r} is not a number; using {default}")
                continue
            if not 0 <= v <= 10:
                notes.append(f"[scoring] {key} = {v} is outside 0..10; clamped")
            out[key] = max(0, min(10, v))
    return out, notes


# ----------------------------------------------------------------- practical measures

def _clamp(v: float) -> float:
    return max(-10.0, min(10.0, v))


def _office_days(rec: dict, loc_cfg: dict) -> tuple[float, str]:
    """(office days a week, how we know). Stated days first, then the stated cadence, then the board's
    hybrid flag, then the configured default for an office posting that says nothing (3, [location]
    assumed_office_days)."""
    if rec.get("onsite_days"):
        return float(rec["onsite_days"]), "stated"
    need = rec.get("onsite_need")
    if need == "daily":
        return 5.0, "stated: daily"
    if need == "weekly":
        return 3.0, "stated: several days a week"
    default = float(loc_cfg.get("assumed_office_days", 3))
    if (rec.get("remote_signal") or "").lower() == "hybrid":
        return default, "hybrid, days assumed"
    return default, "assumed"


def _location(rec: dict, loc_cfg: dict) -> tuple[float, str]:
    """What the commute would cost: office days a week, weighted by how long each trip takes.

    The anchors: remote is best; no commute is ideal; a 30-minute daily commute is
    acceptable; more than an hour each way, more than once a week, is strongly negative. Each trip costs
    (minutes / 30)^2 -- 1 at half an hour, 4 at an hour -- times the days, so a long drive costs far
    more than a short one made more often. Whether you could turn up as often as a job REQUIRES is
    still `gates.reach_gate`'s question; this only orders the jobs you could take."""
    fit, miles = rec.get("location_fit"), rec.get("location_miles")
    if fit == "remote":
        return 10.0, "remote in the US"
    if fit == "remote_unspecified":
        return 6.0, "remote, region unstated"
    if miles is None:
        return 0.0, "location unclear"
    mph = max(10.0, float(loc_cfg.get("commute_mph", 45)))
    minutes = float(miles) * 60.0 / mph
    days, how = _office_days(rec, loc_cfg)
    if minutes <= 5:
        return 9.0, f"no real commute ({miles} mi)"
    burden = days * (minutes / 30.0) ** 2
    m = 10.0 - 1.2 * burden
    if days > 1 and minutes > 60:
        m = min(m, -8.0)                  # more than an hour each way, more than once a week
    m = max(-10.0, min(9.0, m))
    d = f"{days:g} day{'s' if days != 1 else ''} a week" + ("" if how == "stated" else f" ({how})")
    return round(m, 1), f"{d} × {minutes:.0f} min each way ({miles} mi)"


def _pay(rec: dict, pay_cfg: dict) -> tuple[float, str]:
    top = (rec.get("salary") or {}).get("max")
    if not top:
        return 0.0, "pay not stated"
    floor = int(pay_cfg.get("floor", 120_000))
    target = int(pay_cfg.get("target", 150_000))
    if target <= floor:
        return 0.0, "pay range misconfigured"
    m = _clamp(-10.0 + 20.0 * (top - floor) / (target - floor))
    return round(m, 1), f"tops out at ${int(top) // 1000}k"


def _clearance(rec: dict, holds_one: bool = False) -> tuple[float, str]:
    """A posting that needs an ACTIVE clearance is closed to someone who does not hold one, and an
    open door to someone who does. `holds_one` is [scoring] clearance_active in the config; it used
    to be hardcoded false here, which silently encoded one person's situation for everybody."""
    c = rec.get("clearance") or {}
    if not c.get("mentioned"):
        return 0.0, "clearance not mentioned"
    if c.get("active_required"):
        return (8.0, "needs an active clearance, which you hold") if holds_one \
            else (-10.0, "needs a clearance already active")
    return 3.0, "clearance eligibility only"


TRAVEL_FLOOR = 2.5       # a stated share of max_pct_ok x this measures -10


def _travel(rec: dict, max_pct_ok: int) -> tuple[float, str]:
    """Any travel is a cost. Up to `max_pct_ok` it is -4, the same as "light" or
    "occasional" wording; from there it falls to -10 at TRAVEL_FLOOR times that. A stated 0% is 0, and
    so is silence or the bare word "travel", which cannot be read either way."""
    t = rec.get("travel") or {}
    pct, level = t.get("pct"), t.get("level")
    if pct is not None:
        if pct <= 0:
            return 0.0, "no travel"
        ok = max(1, max_pct_ok)
        if pct <= ok:
            return -4.0, f"{pct}% travel"
        return round(_clamp(-4.0 - 6.0 * (pct - ok) / (ok * (TRAVEL_FLOOR - 1))), 1), f"{pct}% travel"
    return {"light": (-4.0, "light travel"), "moderate": (-7.0, "moderate travel"),
            "heavy": (-10.0, "heavy travel")}.get(level, (0.0, "travel not stated"))


RECENCY_KNEES = {"peak_days": 7, "fresh_days": 14, "stale_days": 30}


def _freshness(rec: dict, recency_cfg: Optional[dict] = None) -> tuple[float, str]:
    """Age in days, from the board's posted date or from when we first saw an undated posting
    (filters.recency_of). +10 under `peak_days`, falling to 0 at `fresh_days` and to -10 at
    `stale_days`, where it stays. A posting with no age at all is 0: silence costs nothing."""
    k = {**RECENCY_KNEES, **{n: int(v) for n, v in (recency_cfg or {}).items() if n in RECENCY_KNEES}}
    age = (rec.get("recency") or {}).get("age_days")
    if age is None:
        return 0.0, "no posting date"
    age = max(0, int(age))
    peak, zero, stale = k["peak_days"], max(k["fresh_days"], k["peak_days"] + 1), k["stale_days"]
    stale = max(stale, zero + 1)
    if age < peak:
        m = 10.0
    elif age < zero:
        m = 10.0 * (zero - age) / (zero - peak)
    elif age < stale:
        m = -10.0 * (age - zero) / (stale - zero)
    else:
        m = -10.0
    return round(m, 1), f"{age} day{'s' if age != 1 else ''} old"


def practical_measures(rec: dict, search_cfg: dict) -> dict[str, tuple[float, str]]:
    """{attribute: (measure, evidence)} for everything we can read off the posting ourselves."""
    loc = search_cfg.get("location") or {}
    return {
        "location": _location(rec, loc),
        "pay": _pay(rec, (search_cfg.get("scoring") or {}).get("pay") or {}),
        "clearance": _clearance(rec, bool((search_cfg.get("scoring") or {}).get("clearance_active", False))),
        "travel": _travel(rec, int((search_cfg.get("travel") or {}).get("max_pct_ok", 10))),
        "freshness": _freshness(rec, search_cfg.get("recency") or {}),
    }


# ----------------------------------------------------------------- the dot product

def score(role: Optional[dict], practical: dict[str, tuple[float, str]], weights: dict,
          overall: Optional[float] = None) -> tuple[Optional[float], list[dict]]:
    """(relevance, parts). Returns None when the model has not read the posting yet — an unjudged
    posting has no relevance, and imputing one would quietly rank it against judged ones.

    Every attribute contributes `w_eff * m`, and `w_eff` is stored on the part, so what the table shows
    is exactly the arithmetic: practical and overall fit at their weight, role attributes at their
    weight times role_weight / 10. Parts are ordered by importance (w_eff), then name."""
    parts: list[dict] = []

    def add(dim: str, kind: str, m: float, w: float, w_eff: float, why: str) -> None:
        parts.append({"dim": dim, "kind": kind, "m": round(float(m), 1), "w": w, "w_eff": round(w_eff, 2),
                      "points": round(w_eff * float(m), 1), "why": why})

    for name, (m, why) in practical.items():
        w = weights["practical"].get(name, 0)
        add(name, "practical", m, w, float(w), why)
    if not role:                                  # unjudged (None) or an empty rating set: no relevance
        parts.sort(key=lambda p: (-p["w_eff"], p["dim"]))
        return None, parts
    if overall is not None:
        w = weights.get("overall_fit", DEFAULT_OVERALL_WEIGHT)
        add("overall_fit", "overall", overall, w, float(w), "the model's read of the whole role")
    family = weights.get("role_weight", DEFAULT_ROLE_WEIGHT) / 10.0
    for name in ROLE_DIMS:
        if role.get(name) is None:
            continue                              # not rated: contributes nothing, and is not a zero
        w = weights["role"].get(name, 0)
        add(name, "role", float(role[name]), w, w * family, "")
    parts.sort(key=lambda p: (-p["w_eff"], p["dim"]))
    return round(sum(p["points"] for p in parts), 1), parts


# ----------------------------------------------------------------- grades

# Grade boundaries in standard deviations from the mean. Measured on 200 rated postings the
# distribution came out near-normal (skew -0.16, excess kurtosis -0.37), so these mean what they look
# like they mean. Deliberately NOT quantiles: quantiles hand out the same share of A+ every week no
# matter how poor the week was, whereas a sigma cutoff lets a bad crop genuinely produce no A grades.
GRADE_SIGMAS = (2.0, 1.5, 1.0, 0.6, 0.2, -0.2, -0.6, -1.0, -1.5, -2.0)


def fit_curve(totals: list[float]) -> list[float]:
    """Cutoffs for A+..D from the distribution observed, as mean + k*sd."""
    if len(totals) < 2:
        return []
    mean = sum(totals) / len(totals)
    sd = (sum((x - mean) ** 2 for x in totals) / len(totals)) ** 0.5
    if sd <= 0:
        return []
    return [round(mean + k * sd, 1) for k in GRADE_SIGMAS]


def grade(total: Optional[float], cutoffs: list[float]) -> Optional[str]:
    if total is None or not cutoffs:
        return None
    for name, lo in zip(GRADES, cutoffs):
        if total >= lo:
            return name
    return GRADES[-1]


def at_least(g: Optional[str], floor: str) -> bool:
    """Is grade `g` at least as good as `floor`? Ungraded postings are never good enough."""
    if g is None or g not in GRADES or floor not in GRADES:
        return False
    return GRADES.index(g) <= GRADES.index(floor)


# ----------------------------------------------------------------- the frozen curve

CURVE_FILE = "grade_curve.json"


def weights_sha(weights: dict) -> str:
    """Identifies a set of weights, so a grade can say which tuning generation produced it."""
    return hashlib.sha1(json.dumps(weights, sort_keys=True).encode()).hexdigest()[:12]


def load_curve(data_dir: Path) -> tuple[list[float], str]:
    p = Path(data_dir) / CURVE_FILE
    if not p.exists():
        return [], ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], ""
    return list(d.get("cutoffs") or []), str(d.get("weights") or "")


def save_curve(data_dir: Path, cutoffs: list[float], sha: str, n: int) -> None:
    p = Path(data_dir) / CURVE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"weights": sha, "n": n, "cutoffs": cutoffs}, indent=1), encoding="utf-8")


def overall_of(rec: dict) -> Optional[float]:
    """The model's one holistic read, as a -10..+10 measure.

    Rated with the other attributes and stored in `role`. Ratings made by the old fit pass reach it
    the same way: judge.migrate_fit moved them into judgements.json as `overall_fit`."""
    v = (rec.get("role") or {}).get("overall_fit")
    return None if v is None else float(v)


def apply_to(jobs: dict, search_cfg: dict, data_dir: Optional[Path] = None,
             refit: bool = False) -> tuple[int, list[float]]:
    """Give every posting a relevance total, its parts, and a grade. Returns (graded, cutoffs).

    The curve is held still between runs and refitted only when the weights change or `retune` asks.
    Otherwise a posting's grade would drift because other postings arrived, which is not something a
    reader can be expected to understand."""
    weights, _notes = load_weights(search_cfg)
    for rec in jobs.values():
        total, parts = score(rec.get("role"), practical_measures(rec, search_cfg), weights,
                             overall=overall_of(rec))
        rec["relevance"], rec["score_parts"] = total, parts
    totals = [r["relevance"] for r in jobs.values()
              if r.get("relevance") is not None and r.get("active", True) and not r.get("duplicate_of")]
    sha = weights_sha(weights)
    cutoffs, had = (load_curve(data_dir) if data_dir else ([], ""))
    if refit or not cutoffs or had != sha:
        cutoffs = fit_curve(totals)
        if data_dir and cutoffs:
            save_curve(data_dir, cutoffs, sha, len(totals))
    for rec in jobs.values():
        rec["grade"] = grade(rec.get("relevance"), cutoffs)
    return sum(1 for r in jobs.values() if r.get("grade")), cutoffs


def min_grade(search_cfg: Optional[dict]) -> str:
    g = str(((search_cfg or {}).get("scoring") or {}).get("min_grade") or "B-")
    return g if g in GRADES else "B-"

