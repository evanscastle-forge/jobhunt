"""apply-stats: how close applying is to one click, measured on the runs that exist.

The single-click rate is the share of runs that reached *ready* with no issues at all and, for the ones
you submitted, no edits in the window before you pressed Submit. Rehearsals count toward readiness but
cannot show window edits, so the two are reported separately. A change to answering rules is judged by
this number, the way `retune` judges a weight.
"""
from __future__ import annotations

from collections import Counter

from .schema import TIERS


def apply_stats(runs: list) -> dict:
    """runs: runs.Run objects (anything with .state and .get). Pure over what the run folders hold."""
    filled = [r for r in runs if (r.get("report") or {}).get("tiers")]
    tiers = Counter()
    kinds = Counter()
    labels = Counter()
    for r in filled:
        tiers.update((r.get("report") or {}).get("tiers") or {})
        for i in r.get("issues") or []:
            kinds[i.get("kind")] += 1
            labels[i.get("label")] += 1

    def clean(r) -> bool:
        """Ready, and nothing blocking. Optional questions and notes (an invisible captcha) do not stop
        the one click, so they do not count against it."""
        return bool((r.get("report") or {}).get("ready")) and not any(i.get("blocking") for i in (r.get("issues") or []))

    def untouched(r) -> bool:
        e = r.get("window_edits") or {}
        return not e.get("changed") and not e.get("filled_by_you")

    fills = [(r.get("report") or {}).get("fill") or {} for r in filled]
    ratios = [f["ratio"] for f in fills if f.get("required")]
    rehearsals = [r for r in filled if r.state == "rehearsed"]
    submitted = [r for r in filled if r.state == "submitted"]
    return {
        "runs": len(runs),
        "filled": len(filled),
        "fill_measured": len(ratios),
        "fill_90": sum(1 for x in ratios if x >= 0.9),
        "fill_median": sorted(ratios)[len(ratios) // 2] if ratios else None,
        "review_items": sum(len((r.get("report") or {}).get("review") or []) for r in filled),
        "tiers": {t: tiers.get(t, 0) for t in TIERS},
        "issues_by_kind": dict(kinds.most_common()),
        "top_issues": labels.most_common(10),
        "rehearsals": len(rehearsals),
        "rehearsals_clean": sum(1 for r in rehearsals if clean(r)),
        "submitted": len(submitted),
        "submitted_single_click": sum(1 for r in submitted if clean(r) and untouched(r)),
        "window_edits": sum(len((r.get("window_edits") or {}).get("changed") or []) for r in submitted),
        "filled_by_you": sum(len((r.get("window_edits") or {}).get("filled_by_you") or []) for r in submitted),
    }


def render(s: dict) -> str:
    def pct(a: int, b: int) -> str:
        return f"{a}/{b} ({100 * a // b}%)" if b else "0/0"
    lines = [f"{s['runs']} run(s), {s['filled']} reached a form",
             f"THE TARGET, forms filled >= 90% with no help: {pct(s['fill_90'], s['fill_measured'])}"
             + (f", median {s['fill_median']:.0%} of required fields" if s["fill_median"] is not None else ""),
             f"answers typed for your review: {s['review_items']}",
             "fields by tier: " + ", ".join(f"{t} {n}" for t, n in s["tiers"].items()),
             f"rehearsals ready for one click (nothing blocking): {pct(s['rehearsals_clean'], s['rehearsals'])}",
             f"submitted with one click (ready, nothing blocking, no edits in the window): "
             f"{pct(s['submitted_single_click'], s['submitted'])}",
             f"edits you made in the window: {s['window_edits']} changed, {s['filled_by_you']} filled by you"]
    if s["issues_by_kind"]:
        lines.append("issues by kind: " + ", ".join(f"{k} {n}" for k, n in s["issues_by_kind"].items()))
    if s["top_issues"]:
        lines.append("most frequent issues:")
        lines += [f"  {n:3d}  {lab[:90]}" for lab, n in s["top_issues"]]
    return "\n".join(lines)


def rehearsal_report(runs: list, title: str = "Rehearsal") -> str:
    """A readable account of a batch of runs, for a person and for whoever fixes the next rule: what
    each form asked, what was decided, what broke, and where the evidence is on disk."""
    out = [f"# {title}", "", render(apply_stats(runs)), ""]
    for r in runs:
        rep = r.get("report") or {}
        out += [f"## {r.get('company') or ''} · {r.get('title') or ''}", "",
                f"- run: `{r.path}`", f"- form: {r.get('url') or ''} ({rep.get('ats') or r.get('ats') or '?'})",
                f"- result: {r.state}" + (f" / {r.get('reason')}" if r.get("reason") else "")
                + (" · READY" if rep.get("ready") else ""),
                "- fields: " + ", ".join(f"{k} {v}" for k, v in (rep.get("tiers") or {}).items())
                + (f" · required filled {rep['fill']['filled']}/{rep['fill']['required']} ({rep['fill']['ratio']:.0%})"
                   if rep.get("fill") else "")]
        for rv in rep.get("review") or []:
            out.append(f"  - typed for review [{rv['tier']}] {rv['label'][:80]!r} <- {str(rv['value'])[:100]!r}")
        af = rep.get("autofill") or {}
        if af.get("how"):
            out.append(f"- employer autofill: {af['how']}; page filled {af.get('filled', 0)}, kept {af.get('kept', 0)}, "
                       f"corrected {len(af.get('corrected') or [])}, to confirm {af.get('unconfirmed', 0)}")
            for c in af.get("corrected") or []:
                out.append(f"  - corrected {c['label']!r}: page had {c['page']!r}, now {c['now']!r}")
        elif rep.get("tiers"):
            out.append("- employer autofill: none offered")
        for b in rep.get("blockers") or []:
            out.append(f"- **blocked:** {b}")
        if r.get("error"):
            out.append(f"- **error:** {r.get('error')}")
        if r.get("error_trace"):
            out += ["", "```", r.get("error_trace").rstrip()[-3000:], "```"]
        for m in rep.get("verify") or []:
            out.append(f"- **did not stick:** {m['label']!r}: wanted {m['expected']!r}, page has {m['got']!r}")
        for lab in rep.get("invalid") or []:
            out.append(f"- **page marks invalid:** {lab!r}")
        issues = r.get("issues") or []
        if issues:
            out.append("- issues:")
            for i in issues:
                prop = f" → proposal {i['proposal']!r}" if i.get("proposal") else ""
                out.append(f"  - [{i['kind']}{'!' if i.get('blocking') else ''}] {i['label'][:100]}{prop}"
                           + (f" — {i['why']}" if i.get("why") else ""))
        ev = r.path / "page_events.jsonl"
        if ev.exists():
            n = sum(1 for _ in ev.open(encoding="utf-8"))
            out.append(f"- page events (console errors, failed requests): {n} in `page_events.jsonl`")
        out.append("")
    return "\n".join(out)


def needs_you(runs: list) -> str:
    """The prepare digest: only what still needs you, each question once however many postings ask it."""
    from collections import defaultdict
    asks: dict = defaultdict(list)
    pauses, stuck, ready = [], [], []
    for r in runs:
        rep = r.get("report") or {}
        who = f"{r.get('company') or ''} · {(r.get('title') or '')[:50]}"
        if r.get("reason") in ("verify_email", "vault", "login"):
            pauses.append((who, r.get("reason"), (rep.get("blockers") or [""])[0]))
            continue
        if rep.get("ready"):
            ready.append((who, len(rep.get("review") or [])))
        for i in r.get("issues") or []:
            if not i.get("blocking"):
                continue
            if i.get("kind") == "blocked":
                stuck.append((who, i.get("label"), i.get("why")))
            else:
                asks[(i.get("label") or "").strip().lower()[:120]].append((who, i))
        if not rep.get("tiers") and rep.get("blockers"):
            stuck.append((who, "form", rep["blockers"][0]))
    out = [f"# What still needs you ({len(runs)} posting(s) prepared)", ""]
    out.append(f"**Ready for one click: {len(ready)}** (open, read the orange-outlined answers, press Submit)")
    out += [f"- {w}" + (f" · {n} answer(s) to check" if n else "") for w, n in ready]
    out += ["", f"**Questions: {len(asks)} across {len({w for v in asks.values() for w, _ in v})} posting(s)** "
                "(answer once on the Answers page, or add the fact to about.md)"]
    for label, rows in sorted(asks.items(), key=lambda kv: -len(kv[1])):
        i = rows[0][1]
        out.append(f"- {i.get('label')[:110]}  ({len(rows)}×)" + (f": {i['why'][:140]}" if i.get("why") else ""))
    if pauses:
        out += ["", f"**Accounts: {len(pauses)}**"] + [f"- {w}: {why}: {note[:140]}" for w, why, note in pauses]
    if stuck:
        out += ["", f"**Could not be filled: {len(stuck)}**"] + [f"- {w}: {lab}: {why[:140]}" for w, lab, why in stuck]
    return "\n".join(out) + "\n"
