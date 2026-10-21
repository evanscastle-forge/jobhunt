"""The answer stage: FormSchema x Applicant -> one Answer per Field. No browser.

Every rule says not only what the answer is but how far it may be trusted (schema.TIERS). The rule of
thumb: a profile value is *certain* only for the question it was written for. `clearance_option` answers
"do you hold an active clearance?"; applied to "clearance eligibility" it is wrong, which is exactly what
the first real run did. Where a regex matches loosely, the answer is *inferred*: proposed to you, never
typed, and once you approve it the bank makes that phrasing certain for every later form.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from . import answers as bankmod
from .schema import CHECK, FILE_PREFIX, Answer, Field, clean_label, match_option, norm

_AI_RE = re.compile(r"\b(ai|a\.i\.|artificial intelligence|llm|chatgpt|generative|language model)\b", re.I)
_AI_USE_RE = re.compile(r"\b(use|used|using|usage|assist|assisted|help|helped|generat\w*|wrote|written|rely)\b", re.I)
_EEO_RE = re.compile(r"\b(gender|races?|ethnicit\w*|hispanic|latino|veteran|disabilit\w*|sexual orientation|"
                     r"transgender|pronouns?|self[- ]identif\w*|demographic|eeo)\b", re.I)
_DECLINE_RE = re.compile(r"decline|prefer not|do not wish|don't wish|rather not|choose not|don't want|do not want|"
                         r"not (to )?(answer|say|disclose|specify)|wish not", re.I)
_CONSENT_RE = re.compile(r"\b(consent|agree|acknowledg\w*|privacy|terms|certify|confirm that|i understand|"
                         r"i have read|accurate and complete|opt[- ]in)\b", re.I)
_CHOICE = ("select", "combobox", "radio")
# Questions whose answers carry legal or personal weight: typed only when certain (a profile field or an
# approved bank answer), never from an inference or the applicant agent (docs/apply-design.md).
SENSITIVE_RE = re.compile(
    r"\b(gender|races?|ethnicit\w*|hispanic|latino|veteran|disabilit\w*|sexual orientation|transgender|"
    r"salary|compensation|pay (range|expectation)|desired pay|non-?compete|non-?solicit\w*|restrictive covenant|"
    r"agreements? with|bound by|conflicts? of interest|criminal|convicted|conviction|felony|misdemeanou?r|"
    r"background check|drug (test|screen)\w*|signature|sign (here|below)|certify|attest\w*|"
    r"accurate and complete)\b", re.I)     # AI-use attestations have their own never-answer rule (rule:ai-use)

AI_USE_WHY = "asks about AI use in the application — answer this yourself"


def _yes_no_options(options: list[str]) -> bool:
    low = [norm(o) for o in options if norm(o) not in ("", "select...", "select", "please select")]
    return bool(low) and len(low) <= 3 and any(o.startswith("yes") for o in low) and any(o.startswith("no") for o in low)


def _looks_like_race(options: list[str]) -> bool:
    low = " | ".join(options).lower()
    return "white" in low and ("black" in low or "asian" in low)


class Answerer:
    """Maps a Field to an Answer from profile.toml and the answer bank."""

    def __init__(self, profile: dict, job: Optional[dict] = None,
                 drafter: Optional[Callable[[str, dict], Optional[str]]] = None,
                 bank: Optional[bankmod.Bank] = None):
        self.p = profile
        self.job = job or {}
        self.ident = profile.get("identity", {})
        self.links = profile.get("links", {})
        self.docs = profile.get("documents", {})
        self.a = profile.get("answers", {})
        self.custom = {k.lower(): v for k, v in (self.a.get("custom") or {}).items() if isinstance(v, str)}
        self.drafter = drafter
        self.bank = bank
        self.questions: list[dict] = []    # every question put to you this run
        self._used: list = []               # approved entries used this run (counted once)
        first, last = self.ident.get("first_name", ""), self.ident.get("last_name", "")
        self.full_name = self.ident.get("full_name") or f"{first} {last}".strip()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def qtext(f: Field) -> str:
        """The question as the bank and the agent see it: a follow-up carries the question it follows, so
        "If yes, please explain" after a non-compete question is not the same entry as after a clearance one."""
        return f"{f.label} (follow-up to: {f.context})" if f.context else f.label

    @staticmethod
    def sensitive(f: Field) -> bool:
        return bool(SENSITIVE_RE.search(clean_label(f.label) + " " + (f.context or "").lower()))

    def stored_proposal(self, f: Field) -> Optional[tuple]:
        """A pending bank entry that already holds a proposal (the agent's, or imputed): (value, entry)."""
        if self.bank is None:
            return None
        e, how = self.bank.match(self.qtext(f), self.job)
        if e is None or how != "exact" or e.approved or not e.answer.strip() or "{company}" in e.question:
            return None
        val = self._sub(e.answer)
        if f.kind in _CHOICE and f.options:
            picked, strength = match_option(f.options, val)
            if not picked or strength != "strong":
                return None
            val = picked
        return val, e

    def _sub(self, text: str) -> str:
        return bankmod.specialize(text, self.job)

    def _value(self, f: Field, val, source: str, tier: str = "certain", why: Optional[str] = None) -> Answer:
        """A profile value applied to a field. An empty value is a question; a choice field takes the
        matching option, and a loose match drops the answer to inferred."""
        if val is None or val == "" or val == []:
            return Answer(tier="unknown", source=source, ask=True,
                          why=f"profile.toml has no value for {source.split(':', 1)[-1]}")
        if f.kind in _CHOICE:
            if not f.options:
                return Answer(str(val), tier, source, why)
            picked, strength = match_option(f.options, str(val))
            if not picked:
                return Answer(tier="unknown", source=source, ask=True, why=f"no option matching {val!r}")
            if strength == "weak":
                return Answer(picked, "inferred", source, ask=True,
                              why=f"closest option to your {val!r}; confirm it")
            return Answer(picked, tier, source, why, ask=tier != "certain")
        return Answer(str(val), tier, source, why, ask=tier != "certain")

    def _inferred(self, f: Field, val, source: str, why: str) -> Answer:
        a = self._value(f, val, source, tier="inferred", why=why)
        if a.tier == "certain":      # _value never upgrades, but be explicit
            a.tier = "inferred"
        a.ask = True
        return a

    def _from_bank(self, f: Field) -> Optional[Answer]:
        """An approved bank entry. Exact matches are certain; fuzzy ones are a proposal."""
        if self.bank is None:
            return None
        e, how = self.bank.match(self.qtext(f), self.job)
        if not e or not e.approved:
            return None
        tier = "certain" if how == "exact" else "inferred"
        source = f"bank{'' if how == 'exact' else '-fuzzy'}:{e.key}"
        why = None if how == "exact" else f"close to the approved question {e.question!r}; confirm it"
        if how == "exact" and not any(u is e for u in self._used):
            self._used.append(e)
            self.bank.touch(e, self.job)
        val = self._sub(e.answer)
        if f.kind in _CHOICE:
            if not f.options:
                return Answer(val, tier, source, why, ask=tier != "certain")
            picked, strength = match_option(f.options, val)
            if not picked:
                return Answer(tier="unknown", source=source, ask=True, why=f"bank answer {val!r} matches no option")
            if strength == "weak":
                tier, why = "inferred", f"bank answer {val!r} only loosely matches {picked!r}; confirm it"
            return Answer(picked, tier, source, why, ask=tier != "certain")
        if f.kind == "checkgroup":
            wants = [w.strip() for w in re.split(r"\s*\|\s*|\n", val) if w.strip()]
            picked = [p for p in (match_option(f.options, w)[0] for w in wants) if p]
            if not picked:
                return Answer(tier="unknown", source=source, ask=True, why=f"bank answer {val!r} matches no option")
            return Answer(picked, tier, source, why, ask=tier != "certain")
        if f.kind == "checkbox":
            return Answer(CHECK if norm(val) in ("yes", "true", "check", "checked") else None, tier, source, why,
                          skip=norm(val) not in ("yes", "true", "check", "checked"))
        if f.kind == "file":
            return None
        return Answer(val, tier, source, why, ask=tier != "certain")

    # ------------------------------------------------------------------ rules that need the posting
    def _onsite(self, f: Field, lab: str) -> Optional[Answer]:
        """"Can you be in the office N days a week?" depends on where the office is: Yes only when the
        posting is somewhere you can reach (answers.onsite_only_in), otherwise No. Both are your
        stated position, so both are certain."""
        places = self.a.get("onsite_only_in")
        if not places or f.kind not in _CHOICE:
            return None
        if not re.search(r"\b(in[- ]office|in the office|on-?site|in person|hybrid|commute|report to)\b|days? (a|per) week", lab):
            return None
        if not re.search(r"\b(able|willing|can you|could you|comfortable|agree|acknowledge|open to)\b|\?$", lab) \
                and not _yes_no_options(f.options):
            return None
        where = " ".join(str(self.job.get(k) or "") for k in ("location", "title")).lower()
        near = any(str(p).lower() in where for p in places)
        if not _yes_no_options(f.options):
            none = next((o for o in f.options if re.search(r"none of (the above|these)", o, re.I)), None)
            if none and not near:
                return Answer(none, "certain", "profile:answers.onsite_only_in")
            return None
        return self._value(f, "Yes" if near else "No", "profile:answers.onsite_only_in")

    def _about_this_company(self, lab: str) -> bool:
        """"Have you worked for us / Acme / this company" is about the employer; "have you worked for a
        government agency" is not, and must not get the past-employer answer."""
        comp = norm(self.job.get("company") or "")
        words = [w for w in re.findall(r"\w+", comp) if len(w) >= 4]
        return bool(re.search(r"\b(us|our|this (company|organi[sz]ation|employer))\b|history with|employment history", lab)) \
            or (bool(comp) and (comp in lab or any(w in lab for w in words)))

    def _past_employer(self, f: Field) -> Answer:
        """"Have you worked for {company} before?" is Yes only for an employer you have worked for."""
        past = [norm(x) for x in (self.a.get("past_employers") or [])]
        if not past:
            return self._value(f, self.a.get("previously_employed"), "profile:answers.previously_employed")
        company = norm(self.job.get("company") or "")
        worked = bool(company) and any(p == company or p in company or company in p for p in past)
        if f.kind in _CHOICE and f.options and not _yes_no_options(f.options):
            want = r"\b(former|previous|previously|have worked|was employed)\b" if worked else r"\b(never|have not|haven't|no)\b"
            hits = [o for o in f.options if re.search(want, o, re.I)]
            if len(hits) == 1:
                return Answer(hits[0], "certain", "profile:answers.past_employers")
            return Answer(tier="unknown", source="profile:answers.past_employers", ask=True,
                          why=f"you {'have' if worked else 'have not'} worked for {self.job.get('company')}; pick the option")
        return self._value(f, "Yes" if worked else "No", "profile:answers.past_employers")

    def _eeo(self, f: Field, lab: str) -> Answer:
        """Voluntary self-identification from answers.eeo_self. Certain only when exactly one option
        says what you said; anything else (sexual orientation, a category not given) is declined where
        a decline option exists, and asked otherwise."""
        me = self.a.get("eeo_self") or {}
        opts = f.options
        text = lab + " " + " ".join(opts).lower()
        if re.search(r"pronoun", lab):
            first = norm(str(self.ident.get("pronouns") or "").split("/")[0])
            cat, want = "pronouns", first and (r"^" + re.escape(first) + r"\b")
        elif re.search(r"hispanic|latino", lab) and not re.search(r"\brace\b", lab):
            cat, want = "hispanic", {"no": r"^no\b|not hispanic", "yes": r"^yes\b"}.get(norm(me.get("hispanic", "")))
        elif re.search(r"\brac(e|es)\b|ethnicit", lab) or _looks_like_race(opts):
            race = norm(me.get("race", ""))
            cat, want = "race", race and (r"^" + re.escape(race) + r"\b")
        elif re.search(r"\bgender\b|\bsex\b", lab) and not re.search(r"orientation", lab):
            cat, want = "gender", {"male": r"^(male|man)\b", "female": r"^(female|woman)\b"}.get(norm(me.get("gender", "")))
        elif re.search(r"veteran", text):
            cat, want = "veteran", {"no": r"not a (protected )?veteran|^no\b|i am not"}.get(norm(me.get("veteran", "")))
        elif re.search(r"disabilit", text):
            cat, want = "disability", {"no": r"^no\b|(do not|don't) have a disability"}.get(norm(me.get("disability", "")))
        else:
            cat, want = "other", None
        if f.kind in ("text", "textarea"):
            val = {"pronouns": self.ident.get("pronouns"), "gender": me.get("gender"), "race": me.get("race")}.get(cat)
            return self._value(f, val, f"profile:answers.eeo_self.{cat}") if val else \
                Answer(tier="unknown", source="rule:eeo", skip=not f.required, why="demographic question left for you" if f.required else None)
        if want:
            hits = [o for o in opts if re.search(want, norm(o)) and not _DECLINE_RE.search(o)]
            if cat == "race" and len(hits) > 1:      # "White" vs "White (Not Hispanic or Latino)": take the plain one
                hits = [min(hits, key=len)]
            if len(hits) == 1:
                return Answer([hits[0]] if f.kind == "checkgroup" else hits[0], "certain", f"profile:answers.eeo_self.{cat}")
        decline = next((o for o in opts if _DECLINE_RE.search(o)), None)
        if cat == "other" and decline:
            return Answer([decline] if f.kind == "checkgroup" else decline, "certain", "profile:answers.eeo")
        return Answer(tier="unknown", source="rule:eeo", ask=f.required or bool(want), skip=not f.required and not want,
                      why=f"demographic question ({cat}): no option matches what you said")

    # ------------------------------------------------------------------ checkbox groups
    def _checkgroup(self, f: Field, lab: str) -> Answer:
        options = f.options
        text = lab + " | " + " | ".join(options).lower()
        if _EEO_RE.search(text) or _looks_like_race(options):
            if norm(self.a.get("eeo", "skip")) == "answer":
                return self._eeo(f, lab)
            if norm(self.a.get("eeo", "skip")) == "decline":
                for o in options:
                    if _DECLINE_RE.search(o):
                        return Answer([o], "certain", "profile:answers.eeo")
                return Answer(tier="unknown", source="rule:eeo", why="demographic question (no 'decline' option found)")
            return Answer(tier="unknown", source="rule:eeo", skip=not f.required,
                          why="demographic question left for you" if f.required else None)
        hit = self._from_bank(f)
        if hit is not None and hit.value is not None:
            return hit
        for pat, want in (self.a.get("choices") or {}).items():
            try:
                found = re.search(pat, lab, re.I)
            except re.error:
                found = pat.lower() in lab
            if found:
                wants = want if isinstance(want, list) else [want]
                picked = [p for p in (match_option(options, str(w))[0] for w in wants) if p]
                if picked:
                    return Answer(picked, "certain", f"profile:answers.choices[{pat}]")
                return Answer(tier="unknown", source="profile:answers.choices", ask=True, why=f"no option matching {want!r}")
        low = [norm(o) for o in options]
        looks_like_languages = any(o.startswith("english") for o in low) and (
            re.search(r"language|fluent|speak", text) or any(o.startswith(("spanish", "french", "german", "mandarin")) for o in low))
        if looks_like_languages:
            picked = [p for p in (match_option(options, str(x))[0] for x in (self.a.get("languages") or ["English"])) if p]
            if picked:
                return Answer(picked, "certain", "profile:answers.languages")
        if norm(str(self.a.get("citizenship", ""))) in ("yes", "true", "us citizen", "u.s. citizen"):
            us = next((o for o in options if re.match(r"^\s*u\.?s\.?\s+citizen\b", o, re.I)), None)
            if us and re.search(r"citizen", text):
                return Answer([us], "certain", "profile:answers.citizenship")
        if re.search(r"cuba|iran|north korea|syria|russia|belarus|crimea|sanction|embargo|export control|denied part", text):
            none = next((o for o in options if re.search(r"none of (the above|these)|not applicable|do not apply|n/a", o, re.I)), None)
            if none and norm(str(self.a.get("citizenship", ""))) in ("yes", "true"):
                return Answer([none], "certain", "rule:export-attestation")
        return Answer(tier="unknown", source="", ask=True,
                      why="no approved answer yet (Answers page)" if f.required else "optional · no approved answer yet")

    # ------------------------------------------------------------------ the rules
    def answer(self, f: Field) -> Answer:
        lab = clean_label(f.label)
        kind, options = f.kind, f.options
        if kind == "checkgroup":
            return self._checkgroup(f, lab)
        if not lab:
            return Answer(tier="unknown", skip=not f.required,
                          why="no label found (check this field yourself)" if f.required else None)
        if re.search(r"use up and down|press enter to select|type to search|to choose options", lab) \
                or re.fullmatch(r"\+\d{1,4}", lab):    # the search box inside a phone country picker
            return Answer(skip=True)     # a screen-reader hint on a custom dropdown, not a question

        # never touch: AI-use attestations, demographics (unless opted in)
        if _AI_RE.search(lab) and _AI_USE_RE.search(lab) and not re.search(r"notetak|note-tak|transcri|record", lab):
            return Answer(tier="unknown", source="rule:ai-use", why=AI_USE_WHY)
        if _EEO_RE.search(lab) or _looks_like_race(options):
            if norm(self.a.get("eeo", "skip")) == "answer":
                return self._eeo(f, lab)
            if norm(self.a.get("eeo", "skip")) == "decline" and kind in _CHOICE:
                for o in options:
                    if _DECLINE_RE.search(o):
                        return Answer(o, "certain", "profile:answers.eeo")
                return Answer(tier="unknown", source="rule:eeo", why="demographic question (no 'decline' option found)")
            return Answer(tier="unknown", source="rule:eeo", skip=not f.required,
                          why="demographic question left for you" if f.required else None)

        # an approved answer in the bank wins over the heuristics below (it is yours, verbatim)
        hit = self._from_bank(f)
        if hit is not None and hit.value is not None:
            return hit

        ident, links, a = self.ident, self.links, self.a

        # identity -------------------------------------------------------------------
        if re.search(r"\bpreferred (first )?name\b", lab) and ident.get("preferred_name"):
            return self._value(f, ident.get("preferred_name"), "profile:identity.preferred_name")
        if re.search(r"\bpreferred (first )?name\b|\bnickname\b|\bpronunciation\b", lab):
            return Answer(skip=True)
        if re.search(r"\bfirst name\b|\bgiven name\b", lab):
            return self._value(f, ident.get("first_name"), "profile:identity.first_name")
        if re.search(r"\blast name\b|\bsurname\b|\bfamily name\b", lab):
            return self._value(f, ident.get("last_name"), "profile:identity.last_name")
        if re.fullmatch(r"(full |legal )?name|your name", lab) or re.search(r"\b(full|legal) name\b", lab):
            return self._value(f, self.full_name, "profile:identity.full_name")
        if re.search(r"\be-?mail\b", lab):
            return self._value(f, ident.get("email"), "profile:identity.email")
        if re.search(r"\bphone\b|\bmobile\b|\btelephone\b", lab):
            return self._value(f, ident.get("phone"), "profile:identity.phone")
        if re.search(r"\blinkedin\b", lab):
            return self._value(f, links.get("linkedin"), "profile:links.linkedin")
        if re.search(r"\bgithub\b", lab):
            if norm(links.get("github") or "") == "none":
                return Answer(skip=not f.required, tier="unknown", why="you have no public GitHub" if f.required else None)
            return self._value(f, links.get("github"), "profile:links.github")
        if re.search(r"\bportfolio\b|\bwebsite\b|\bpersonal (site|url)\b", lab):
            if norm(links.get("website") or "") == "none":
                return Answer(skip=not f.required, tier="unknown", why="you have no portfolio or website" if f.required else None)
            return self._value(f, links.get("website") or links.get("portfolio"), "profile:links.website")
        if re.search(r"\burl\b|\blink to\b", lab):
            return self._inferred(f, links.get("website") or links.get("portfolio") or links.get("github"),
                                  "profile:links.website", "a link was asked for; confirm which one")
        if re.search(r"\bcountry\b", lab) and not re.search(r"authori|eligib|work|citizen|resident|sponsor|relocat", lab):
            return self._value(f, ident.get("country") or "United States", "profile:identity.country")
        if re.search(r"\b(current|most recent|present) (company|employer)\b|^(company|employer)( name)?$", lab) \
                and not re.match(r"(have|has|do|does|are|is|did|will|would|can)\b", lab):   # a yes/no question about them is not their name
            return self._value(f, ident.get("current_company"), "profile:identity.current_company")
        if re.search(r"\b(current|most recent)\b[^?]{0,20}\b(title|role|position)\b|^(job )?title$", lab):
            return self._value(f, ident.get("current_title"), "profile:identity.current_title")
        if re.search(r"\b(street|address line 1|address 1|mailing address|home address)\b|^address$", lab) and kind == "text":
            return self._value(f, ident.get("address_line1"), "profile:identity.address_line1")
        if re.search(r"\baddress line 2\b|\bapt\b|\bsuite\b", lab):
            return Answer(skip=not f.required, tier="unknown", why="address line 2" if f.required else None)
        if re.search(r"\b(zip|postal)( code)?\b", lab):
            return self._value(f, ident.get("postal_code"), "profile:identity.postal_code")
        if re.fullmatch(r"(state|state/province|province|state or province)", lab):
            val = ident.get("state_name") if kind in _CHOICE and ident.get("state_name") else ident.get("state")
            got = self._value(f, val, "profile:identity.state")
            if got.value is None and kind in _CHOICE and ident.get("state"):
                got = self._value(f, ident.get("state"), "profile:identity.state")
            return got
        if re.fullmatch(r"city", lab) and ident.get("city"):
            return self._value(f, ident.get("city"), "profile:identity.city")
        if re.search(r"^(city|location|current location|city, state|location \(city\))$|\bwhere (are|do) you (live|based|located|reside)\b|"
                     r"\b(current|home) (city|location)\b|\bbased in\b", lab) \
                and not re.search(r"relocat|office|preferred (work )?location|which (office|location)", lab):
            return self._value(f, ident.get("location"), "profile:identity.location")
        if re.search(r"\b(city|location|address)\b", lab) and len(lab) < 60 and kind in ("text", "combobox") \
                and not re.search(r"relocat|office|preferred|which|travel", lab):
            return self._inferred(f, ident.get("location"), "profile:identity.location",
                                  "your location, for a question that may want something more specific")

        # documents ------------------------------------------------------------------
        if kind == "file":
            if re.search(r"resume|cv\b|curriculum", lab):
                path = self.docs.get("resume")
                return Answer(FILE_PREFIX + str(path), "certain", "profile:documents.resume") if path \
                    else Answer(tier="unknown", why="no resume path in profile.toml")
            if re.search(r"cover letter", lab):
                if self.docs.get("cover_letter"):
                    return Answer(FILE_PREFIX + str(self.docs["cover_letter"]), "certain", "profile:documents.cover_letter")
                return Answer(tier="unknown", skip=not f.required, why="cover letter file required" if f.required else None)
            return Answer(tier="unknown", skip=not f.required, why="unrecognised file upload" if f.required else None)
        if re.search(r"cover letter", lab) and kind in ("textarea", "text"):
            v = self.custom.get("cover letter")
            if v:
                return Answer(self._sub(v), "certain", "profile:answers.custom[cover letter]")
            return Answer(tier="unknown", skip=not f.required, ask=f.required,
                          why="cover letter text requested" if f.required else None)

        # eligibility / logistics ----------------------------------------------------
        if re.search(r"sponsor", lab):
            return self._value(f, a.get("sponsorship"), "profile:answers.sponsorship")
        if re.search(r"authori[sz]ed|authori[sz]ation|legally (able|permitted|eligible)|eligible to work|"
                     r"work (in|within) the (us|u\.s\.|united states)|right to work", lab):
            return self._value(f, a.get("work_authorization"), "profile:answers.work_authorization")
        if re.search(r"citizen", lab):
            return self._value(f, a.get("citizenship"), "profile:answers.citizenship")
        if re.search(r"clearance", lab):
            if kind in _CHOICE:
                if re.search(r"\bactive\b|\bcurrent(ly)?\b|\bdo you (hold|have)\b|\bhold an?\b", lab) \
                        and not re.search(r"eligib|able to (obtain|get)|willing|ever held|previously|reinstat", lab):
                    return self._value(f, a.get("clearance_option"), "profile:answers.clearance_option")
                return Answer(tier="unknown", source="profile:answers.clearance_option", ask=True,
                              why="clearance_option answers 'do you hold an active clearance?'; this asks something else")
            if kind == "textarea" or re.search(r"\b(describe|explain|details?|tell us)\b", lab):
                return self._value(f, a.get("clearance"), "profile:answers.clearance")
            return Answer(tier="unknown", source="profile:answers.clearance", ask=True,
                          why="a short clearance field; your clearance sentence is written for 'describe'")
        if re.search(r"salary|compensation|pay (range|expectation)|desired (pay|rate)|expected (pay|salary)", lab):
            return self._value(f, a.get("salary_expectation"), "profile:answers.salary_expectation")
        if re.search(r"how (did )?you (hear|heard|find|found|learn\w*)|\breferr(al|ed)\b|\bsource\b", lab):
            got = self._value(f, a.get("how_heard"), "profile:answers.how_heard")
            if got.value is not None or kind not in _CHOICE:
                return got
            if norm(str(a.get("how_heard") or "")) == "other":
                others = [o for o in options if norm(o).startswith("other")]
                if others:
                    return Answer(others[0], "inferred", "rule:how-heard", ask=True,
                                  why=f"your how_heard is 'Other'; the form splits it, and {others[0]!r} is the first such option")
            for cand in ("Company website", "Careers page", "Job board", "Other"):
                picked = match_option(options, cand)[0]
                if picked:
                    return Answer(picked, "inferred", "rule:how-heard", ask=True,
                                  why=f"no option matches your how_heard {a.get('how_heard')!r}; closest is {picked!r}")
            return Answer(tier="unknown", source="rule:how-heard", ask=True, why="no matching 'how did you hear' option")
        if re.search(r"start date|available to start|when can you start|earliest", lab):
            return self._value(f, a.get("start_date"), "profile:answers.start_date")
        if re.search(r"notice period", lab):
            return self._value(f, a.get("notice_period"), "profile:answers.notice_period")
        if re.search(r"relocat", lab):
            return self._value(f, a.get("relocation"), "profile:answers.relocation")
        if re.search(r"\btravel\b", lab):
            return self._inferred(f, a.get("travel"), "profile:answers.travel",
                                  "your general travel answer; check it against the amount this posting asks")
        onsite = self._onsite(f, lab)
        if onsite is not None:
            return onsite
        if re.search(r"remote|hybrid|on-?site|in[- ]office|work (location|arrangement)|which (office|location)", lab):
            return self._inferred(f, a.get("remote_preference"), "profile:answers.remote_preference",
                                  "your remote preference, for a question about this job's arrangement")
        if re.search(r"years of (\w+ )?experience|how many years", lab):
            generic = re.fullmatch(r"(how many )?years of (professional |relevant |work |industry |total )?experience( do you have)?\??", lab)
            if generic:
                return self._value(f, a.get("years_experience"), "profile:answers.years_experience")
            return self._inferred(f, a.get("years_experience"), "profile:answers.years_experience",
                                  "your total years, for a question that may ask about one skill")
        if re.search(r"\b(18|eighteen)\b[^.?]{0,30}\b(years? (of age|old|or older)|or older|age)\b|over the age of|at least 18|legal (working )?age", lab):
            return self._value(f, "Yes", "rule:age")
        if re.search(r"previously (worked|been employed|employed)|former employee|worked (at|for) .* before|"
                     r"ever (been )?(employed by|worked (at|for))|history with|employment history", lab) \
                and self._about_this_company(lab):
            return self._past_employer(f)
        if re.search(r"conflicts? of interest", lab) and a.get("conflict_of_interest"):
            return self._value(f, a.get("conflict_of_interest"), "profile:answers.conflict_of_interest")
        if re.fullmatch(r"(discipline|field of study|major|area of study)", lab) and a.get("discipline"):
            return self._value(f, a.get("discipline"), "profile:answers.discipline")
        if re.search(r"non-?compete|restrictive covenant", lab):
            if "noncompete" in a:
                return self._value(f, a.get("noncompete"), "profile:answers.noncompete")
            return self._inferred(f, "No", "rule:noncompete", "profile.toml has no noncompete answer")
        if re.search(r"\b(highest )?(degree|level of education)\b", lab) and kind in _CHOICE:
            return self._value(f, a.get("highest_degree"), "profile:answers.highest_degree")
        if re.search(r"pronoun", lab):
            return Answer(skip=True)
        if re.fullmatch(r"(today'?s )?date|date signed|signature date", lab) and kind == "text":
            from datetime import date
            return Answer(date.today().strftime("%m/%d/%Y"), "certain", "rule:today")

        # consents -------------------------------------------------------------------
        if kind == "checkbox":
            if _CONSENT_RE.search(lab):
                return Answer(CHECK, "certain", "rule:consent")
            return Answer(tier="unknown", skip=not f.required,
                          why="unrecognised checkbox" if f.required else None)

        # free-text answers from profile.toml [answers.custom] ---------------------------
        for pat, text in self.custom.items():
            try:
                found = re.search(pat, lab)
            except re.error:
                found = pat in lab
            if found:
                return self._value(f, self._sub(text), f"profile:answers.custom[{pat}]")

        # a real question nothing covers
        if kind in ("text", "textarea") + _CHOICE and len(lab) > 4:
            return Answer(tier="unknown", ask=True,
                          why="no approved answer yet (Answers page)" if f.required else "optional · no approved answer yet")
        return Answer(tier="unknown", skip=not f.required,
                      why="no answer in profile.toml for this question" if f.required else None)

    # ------------------------------------------------------------------ putting it to you
    def ask(self, f: Field, ans: Answer) -> Optional[dict]:
        """Record a non-certain answer as a question for you: a pending bank entry, plus the proposal
        (inferred) or model draft (drafted) as a suggestion. Nothing here is typed into the form."""
        if self.bank is None:
            return None
        fuzzy = ans.source.startswith("bank-fuzzy:")
        e, how = self.bank.match(self.qtext(f), self.job)
        key = e.key if e is not None and (how == "exact" or not fuzzy) else None
        for q in self.questions:            # second fill pass: already recorded this run
            if key and q["key"] == key:
                return q
        entry = self.bank.record(self.qtext(f), f.kind, f.options, f.required, self.job, exact=fuzzy)
        q = {"label": f.label, "kind": f.kind, "options": list(f.options or [])[:40], "required": bool(f.required),
             "key": entry.key, "question": entry.question, "answered": False, "draft": None,
             "tier": ans.tier, "source": ans.source, "why": ans.why}
        stored = self.bank.get(entry.key)
        if ans.tier == "inferred" and ans.value not in (None, "", []):
            q["draft"] = ", ".join(ans.value) if isinstance(ans.value, list) else str(ans.value)
            q["proposal"] = True
        elif stored is not None and not stored.approved and stored.answer.strip():
            # a proposal already in the bank (the applicant agent's, or imputed): shown, never typed
            val = self._sub(stored.answer)
            if f.kind in ("select", "combobox", "radio") and f.options:
                val = match_option(f.options, val)[0]
            if val:
                q["draft"], q["proposal"] = val, True
                q["tier"] = "drafted" if f.kind in ("text", "textarea") else "inferred"
                q["why"] = f"proposed by {stored.by or 'the bank'}: {stored.note}"[:300]
        elif stored is not None and stored.note.startswith("declined"):
            q["why"] = f"{stored.by} {stored.note}"[:300]
        if q.get("draft"):
            pass
        elif f.kind in ("textarea", "text") and self.drafter and len(f.label) > 25:
            try:
                q["draft"] = self.drafter(f.label, self.job)
            except Exception:  # noqa: BLE001
                q["draft"] = None
            if q["draft"]:
                q["tier"] = "drafted"
        if not any(x["key"] == q["key"] for x in self.questions):
            self.questions.append(q)
        return q

    def save(self) -> None:
        if self.bank is not None:
            self.bank.save()
