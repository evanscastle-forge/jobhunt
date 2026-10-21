"""Profiles: one folder per person under profiles/<name>/.

    profiles/<name>/search.toml    what this person is looking for (layered over config.toml)
    profiles/<name>/profile.toml   identity + answer bank for application forms
    profiles/<name>/notes/about.md the person's facts (third person, one per bullet); the only text the LLM hooks read
    profiles/<name>/notes/answers.md the answer bank for application forms (approved answers are reused)
    profiles/<name>/data/          jobs.json, state.json, learned.json, runs/<run_id>/ (one folder per apply run)

The shared config.toml holds everything that is the same for everyone: location vocabularies, the
list of company boards to fetch, source toggles, LLM settings. A profile's search.toml overrides
any of those tables key-by-key and may add its own [[companies]].
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .store import Store

PROFILES_DIR = "profiles"
LAYERED_TABLES = ("search", "location", "travel", "profile", "weights", "sources", "llm")


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_env(root: Path) -> dict:
    """Minimal .env reader (KEY=value lines). Values are returned, never logged."""
    out: dict = {}
    p = Path(root) / ".env"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v[:1] == v[-1:] and v[:1] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


LEGACY_MILES = ("local_miles", "commute_miles", "regional_miles")


def migrate_location(loc: dict) -> dict:
    """Translate one layer's distance keys from the old names to the reach names. Idempotent.

    It has to happen per layer, BEFORE the merge, and that is the whole point. `merge_config` layers
    [location] key by key, so once config.toml declares `monthly_miles` and a profile still says
    `commute_miles`, the merged table holds both and nothing can tell which author meant what. No
    precedence rule fixes that, because the information needed -- which keys one person wrote
    together -- is destroyed by the merge.

    The two shapes differ, which is why a plain rename would be wrong:

        local_miles present   the author separated home from the wider commute range, so
                              daily = local_miles and monthly = commute_miles.
        local_miles absent    commute_miles WAS the daily drive, so daily = monthly = commute_miles.

    `regional_miles` is dropped: `quarterly` now runs from `monthly_miles` to `radius_miles`."""
    if not isinstance(loc, dict) or not any(k in loc for k in LEGACY_MILES):
        return loc
    out = {k: v for k, v in loc.items() if k not in LEGACY_MILES}
    if "daily_miles" in loc or "monthly_miles" in loc:
        return out                          # already migrated; the legacy keys are leftovers
    commute = loc.get("commute_miles")
    if loc.get("local_miles"):
        out["daily_miles"] = int(loc["local_miles"])
        if commute is not None:
            out["monthly_miles"] = int(commute)
    elif commute is not None:
        out["daily_miles"] = out["monthly_miles"] = int(commute)
    return out


def merge_config(shared: dict, override: dict) -> dict:
    """Layer a profile's search.toml over config.toml.

    Top-level tables are merged one key at a time (so a profile can change `search.title_tier1`
    without repeating `location.non_us`); nested tables such as `profile.keywords` are replaced
    whole when the profile provides them. `[[companies]]` lists are concatenated.

    Each layer's [location] distance keys are migrated first, separately -- see migrate_location.
    """
    shared = {**shared, "location": migrate_location(shared.get("location") or {})} if shared.get("location") else shared
    override = {**override, "location": migrate_location(override.get("location") or {})} if override.get("location") else override
    merged = json.loads(json.dumps(shared))
    for key, val in override.items():
        if key == "companies":
            continue
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    merged["companies"] = list(shared.get("companies") or []) + list(override.get("companies") or [])
    return merged


@dataclass
class Profile:
    name: str
    dir: Path
    search: dict                 # merged config (shared + this profile's search.toml)
    answers: dict                # profile.toml
    store: Store
    notes: str = ""
    display_name: str = ""
    enabled: bool = True         # `enabled = false` in search.toml parks a profile without deleting it
    errors: list = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return self.dir / "data"

    @property
    def resume_path(self) -> Optional[Path]:
        r = (self.answers.get("documents") or {}).get("resume")
        if not r:
            return None
        p = Path(r)
        return p if p.is_absolute() else (self.dir / p)

    def companies(self) -> list[dict]:
        """Company boards this profile wants, honoring `enabled` and per-company `profiles = [...]`.

        `profiles` names a profile folder or one of its `board_tags`, so the shared list can say
        "engineer" rather than a real person's name."""
        tags = self.board_tags
        out = []
        for c in self.search.get("companies", []):
            if not c.get("enabled", True):
                continue
            allowed = c.get("profiles")
            if allowed and not tags & {a.lower() for a in allowed}:
                continue
            out.append(c)
        return out

    @property
    def board_tags(self) -> set[str]:
        """The names a [[companies]] `profiles = [...]` list may use for this profile."""
        return {self.name} | {str(t).lower() for t in self.search.get("board_tags") or []}

    @property
    def board_tag(self) -> str:
        """The tag a board this profile discovered is written under: its first board_tag, else its name."""
        return next((str(t).lower() for t in self.search.get("board_tags") or []), self.name)

    def board_names(self) -> set[str]:
        return {c.get("name") or c.get("board") or c.get("slug") or "" for c in self.companies()}

    def wants_source(self, source: str) -> bool:
        src = self.search.get("sources", {})
        if source == "hn":
            return bool(src.get("hn_who_is_hiring", False))
        return True

    def answers_for_apply(self) -> dict:
        """profile.toml with document paths resolved relative to the profile folder."""
        p = json.loads(json.dumps(self.answers))
        docs = p.setdefault("documents", {})
        for key in ("resume", "cover_letter"):
            v = docs.get(key)
            if v and not Path(v).is_absolute():
                docs[key] = str((self.dir / v).resolve())
        return p


def _read_notes(d: Path) -> str:
    """The profile's facts: notes/about.md and nothing else, so the About page shows exactly what the
    LLM hooks see. Uploads under notes/uploads/ are kept for reference only."""
    p = d / "notes" / "about.md"
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


EXAMPLE_PREFIX = "example-"


def is_example(name: str) -> bool:
    """Profiles named example-* are documentation of the file format, nothing more.

    They must never fetch a board or spend a model call. Enforced by the name rather than by an
    `enabled` flag, because a flag is one careless edit away from quietly putting a demo profile into
    every scan. Copying profiles/example-engineer/ to profiles/<your-name>/ works exactly as before:
    the copy no longer carries the prefix."""
    return name.startswith(EXAMPLE_PREFIX)


def list_profile_names(root: Path, include_examples: bool = False) -> list[str]:
    pd = Path(root) / PROFILES_DIR
    if not pd.is_dir():
        return []
    return sorted(p.name for p in pd.iterdir()
                  if p.is_dir() and not p.name.startswith(".")
                  and (include_examples or not is_example(p.name))
                  and ((p / "search.toml").exists() or (p / "profile.toml").exists()))


def load_profile(root: Path, name: str, shared: Optional[dict] = None) -> Profile:
    root = Path(root)
    shared = shared if shared is not None else load_toml(root / "config.toml")
    d = root / PROFILES_DIR / name
    if not d.is_dir():
        raise FileNotFoundError(f"no profile folder {d}")
    errors: list[str] = []
    override: dict = {}
    if (d / "search.toml").exists():
        try:
            override = load_toml(d / "search.toml")
        except (tomllib.TOMLDecodeError, OSError) as e:
            errors.append(f"search.toml: {e}")
    answers: dict = {}
    if (d / "profile.toml").exists():
        try:
            answers = load_toml(d / "profile.toml")
        except (tomllib.TOMLDecodeError, OSError) as e:
            errors.append(f"profile.toml: {e}")
    search = merge_config(shared, override)
    ident = answers.get("identity") or {}
    display = override.get("display_name") or ident.get("first_name") or name.title()
    return Profile(name=name, dir=d, search=search, answers=answers, store=Store(d / "data"),
                   notes=_read_notes(d), display_name=display,
                   enabled=bool(override.get("enabled", True)), errors=errors)


def load_all(root: Path, include_disabled: bool = False) -> tuple[dict, dict[str, Profile]]:
    """Returns (shared config, {name: Profile}) — profiles ordered with the default first.

    A profile with `enabled = false` in its search.toml is left out entirely: its boards are not
    fetched, its postings are not scored and the dashboard does not list it. Nothing on disk is
    touched, so flipping the flag back restores it exactly as it was. `include_disabled` is for
    tooling that needs to report what is parked."""
    root = Path(root)
    shared = load_toml(root / "config.toml")
    shared = add_discovered(root, shared)
    names = list_profile_names(root, include_examples=include_disabled)
    default = (shared.get("default_profile") or (names[0] if names else "")).lower()
    names.sort(key=lambda n: (n != default, n))
    loaded = ((n, load_profile(root, n, shared)) for n in names)
    return shared, {n: p for n, p in loaded if include_disabled or p.enabled}


DISCOVERED_FILE = "companies.discovered.toml"


def add_discovered(root: Path, shared: dict) -> dict:
    """Append the boards `jobhunt.py discover` found to the shared company list.

    Kept in its own generated file rather than appended to config.toml, which is 1,700 hand-curated
    lines. Deleting the file resets discovery; the registry remembers what was already probed, so a
    re-run does not re-guess dead tokens. A board already in config.toml wins and is not duplicated,
    which `pipeline.feed_key` would catch anyway but is cheaper to avoid here."""
    path = Path(root) / DISCOVERED_FILE
    if not path.exists():
        return shared
    try:
        found = load_toml(path).get("companies") or []
    except (OSError, ValueError):
        return shared
    have = set()
    for c in shared.get("companies") or []:
        ats = (c.get("ats") or "").lower()
        have.add((ats, (c.get("board") or c.get("slug") or "").lower()))
    extra = [c for c in found
             if ((c.get("ats") or "").lower(), (c.get("board") or c.get("slug") or "").lower()) not in have]
    return {**shared, "companies": list(shared.get("companies") or []) + extra}


def disabled_names(root: Path) -> list[str]:
    """Profiles parked with `enabled = false`, so the CLI can say they are still there."""
    _shared, everything = load_all(root, include_disabled=True)
    return [n for n, p in everything.items() if not p.enabled]


def pick_profile(profiles: dict[str, Profile], name: Optional[str], shared: dict,
                 disabled: Optional[list[str]] = None) -> Profile:
    if not profiles:
        raise SystemExit("no profiles found under profiles/ — create profiles/<name>/search.toml")
    if name:
        key = name.lower()
        if key not in profiles:
            if key in (disabled or []):
                raise SystemExit(f"profile {name!r} is parked; set enabled = true in "
                                 f"profiles/{key}/search.toml to bring it back (nothing was deleted)")
            raise SystemExit(f"unknown profile {name!r}; available: {', '.join(profiles)}")
        return profiles[key]
    default = (shared.get("default_profile") or "").lower()
    return profiles.get(default) or next(iter(profiles.values()))
