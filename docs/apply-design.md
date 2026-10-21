# Applying: the design

This is the contract for how an application gets filled in. It plays the part for `jobhunt/apply/`
that `AGENTS.md` plays for scoring: if you change a rule here, change this file in the same commit.
Written 24 September 2026, as a design review of the code that existed then.

## Scope (27 September 2026)

- **Unattended:** only the 2 am scan (fetch and score new postings for the dashboard) and the always-on
  dashboard. Nothing applies on its own.
- **On your click:** Apply fills the form as completely as it can and leaves the window open. You review
  and submit.
- **Learning:** what the run could not answer is listed under "Needs you" on the run's dashboard panel.
  Whatever you answer there ("Send to form"), or types into the window and submits, is remembered.
  Answers about one company go to `notes/voice.md` rather than being reused.
- `rehearse`, `prepare` and `replay` are manual tools for development, not automation.

## The north star

For most postings, you click **Apply** on the dashboard, a browser window opens with every field
already answered correctly from your own record, and the one thing left for you to do is read it and
press the employer's own **Submit** button.

Three rules come with it:

- **The tool never presses Submit.** The single click is yours, in the employer's window. It is also what makes browser automation
  survivable against bot scoring (see below).
- **Anything short of certain goes to you.** "Certain" is defined below, mechanically. It is not
  something the tool feels.
- **Apply is its own subsystem.** It reads a posting and an applicant, and writes a run folder. It
  imports nothing from the pipeline or the dashboard, and the dashboard is one of two clients of the run
  folder, the CLI being the other.

## Why a browser, and not the ATS APIs

This was checked rather than assumed. The short answer is that the APIs are not an option for a
candidate: every documented submission endpoint needs a key that only the employer holds.

| ATS | submit | question schema |
|---|---|---|
| Greenhouse | `POST /applications` needs the employer's Basic-auth key ([docs](https://docs.greenhouse.io/job-board.html)); an open-source project confirmed 401 without it | **public**: `GET /v1/boards/{token}/jobs/{id}?questions=true` returns every question, its type, options and required flag |
| Lever | employer key from a Super Admin ([postings-api](https://github.com/lever/postings-api)) | public GET omits custom questions; read the hosted `/apply` page |
| Ashby | `applicationForm.submit` needs `candidatesWrite` | `jobPosting.info` needs `jobsRead`; the public posting API gives only `applyUrl` |
| SmartRecruiters | employer `X-SmartToken` | postings list is public |
| Workable | `w_candidates` scope | behind a token |
| Workday, iCIMS, Taleo, SuccessFactors | no candidate API; Workday needs an account per employer | none |

So submission happens in a browser, and the public schema endpoints are used to **plan** the answers
before the browser opens (see *Planning ahead*).

A browser is not free of trouble either, and the design has to meet what is known:

- Greenhouse boards run invisible reCAPTCHA, which scores mouse and keyboard behaviour and can demand
  email verification when the score is low
  ([Greenhouse](https://support.greenhouse.io/hc/en-us/articles/115005448066-Invisible-reCAPTCHA)).
  Lever shows hCaptcha to "suspicious" sessions.
- Playwright always sets `navigator.webdriver`, and headless Chromium has further tells.
- One project reports Lever rejecting a CDP-attached submission even after a human solved the captcha
  ([JobTomatik #539](https://github.com/TheHighBrid/JobTomatik/issues/539)).

The response is not stealth patching, which defeats a control the employer chose and is a terms problem.
It is: a headed, **persistent** real-Chrome profile, so the session carries history and logins like a
person's; and the final action, Submit, done by the person. If evidence from real runs shows scores still
blocking, the next step is an extension-based filler inside your own Chrome (the Simplify approach),
decided on that evidence and not before.

Never: LinkedIn Easy Apply or Indeed (their terms forbid automation), captcha solving, or anyone's API
key but our own.

## The shape of it

An application is eight stages, each a named function with an explicit contract, in the same spirit as
the eight stages of a scan:

```
resolve   an aggregator listing -> the employer's real form (resolve.py)    browser, cached
open      go there; a custom careers site is walked to its form (navigate.py); Workday signs in or
          creates the account (playbooks/workday.py, vault.py)             browser
autofill  the employer's own resume parsing fills what it can               browser
read      page -> FormSchema                                                browser, pure over the DOM
answer    FormSchema x Applicant -> one Answer per field                    pure, plus the model
fill      Answers -> the page, through widget adapters                      browser
verify    read every value back, collect validation errors, screenshot      browser
hand-off  ready -> press Submit; otherwise -> Issues                      run folder
```

Multi-page forms (Workday, "Step 1 / 3 ... Continue") repeat read -> answer -> fill -> verify per page
(`pages.walk`); advancing must change the page, a refusal is answered by reading the page's own errors
and filling once more, and nothing matching Submit is ever clicked. A widget no adapter can set is
repaired by a model recipe in a closed vocabulary, verified by read-back and cached per widget
signature (`heal.py`), so the same widget on the next form costs nothing.

**The employer's autofill goes first.** Most ATSes parse an uploaded
resume into the form: Lever does it on the resume field itself, Ashby has a separate "Autofill from
resume" box, Workday offers "Autofill with Resume". Using it first means the tool's job becomes checking
and correcting rather than typing everything. What the page filled is never trusted as if you had typed
it: a value we know for certain is kept when it agrees and corrected when it does not, and a value we
cannot check (a school, a date the parser guessed) is a proposal you confirm once. Login-based autofill
(MyGreenhouse, LinkedIn, Indeed) is never used. A value you type in the open window is still never
overwritten; only the page's own autofill, captured right after it runs, is reviewed.

Multi-page forms (Workday, iCIMS) loop `read -> answer -> fill -> verify` per page, and advancing to the
next page is a playbook step that must itself verify it landed where expected.

The key separation is **answer from fill**. Today `Answers.value_for` (`jobhunt/apply/forms.py:262`) is a
single regex cascade that decides what a question is, what its answer is and how confident to be, all at
once, so fixing one rule quietly breaks another. Deciding is pure and testable without a browser. Acting
is mechanical. They do not share code.

### FormSchema

One entry per logical question, not per DOM control:

```
Field {key, label, help, section, kind, options[], required, frame, locator, prefilled}
kind  text | textarea | select | combobox | radio | checkgroup | checkbox | file | date | unknown
```

`key` is the normalised, company-generalised label (`answers.normalize` / `answers.generalize` already
do this). `frame` exists because employers embed Greenhouse on their own career sites inside an iframe;
today's field scan (`FIELD_SCAN_JS`, `forms.py:420`) runs only in the main frame, so those forms look
empty. `read` walks every frame.

Fields are located by label and accessibility role, never by CSS written for one ATS. Forms have to
label their fields for screen readers, which is exactly why label-based location generalises and
selectors do not. The label resolution order in `FIELD_SCAN_JS` is sound and is kept.

## Generality: adapters per widget, playbooks per platform, never code per form

- **Widget adapters** know how to set and read back one kind of control: native input, select, radio
  group, checkbox group, react-select, Workday's combobox and date widgets, file upload. What
  `forms.py:513-683` already knows moves into one adapter each. An adapter must implement `read_back`,
  or `verify` cannot trust it.
- **Platform playbooks** know only navigation: where the Apply button is, how to log in, how to advance a
  page, what the confirmation looks like. They never know about questions.

Platforms fall into three tiers:

| tier | platforms | what's needed |
|---|---|---|
| T1 hosted forms | Greenhouse, Lever, Ashby, SmartRecruiters, Workable | the generic loop |
| T2 account portals | Workday, iCIMS, Taleo, SuccessFactors | the vault, a playbook, per-page loop |
| T3 unknown sites | employer career sites, anything else | the generic loop plus a model proposing the next navigation step |

A T3 run is supervised from start to finish and can never reach *ready* on its own.

## Certainty is defined, not felt

Every answer carries a tier, and the tier decides what happens to it:

| tier | where it came from | what happens |
|---|---|---|
| **certain** | a profile field, or an approved bank entry matched exactly, for a question that field was written for | typed |
| **inferred** | a near match: a loose option match, a profile value on a neighbouring question, a fuzzy bank match | typed, outlined, listed for review |
| **agent** | the applicant agent's grounded answer (`persona.py`), or a proposal it stored in the bank | typed, outlined, listed for review |
| **unknown** | nothing to go on, and the agent declined | asked |

The middle two are policy, made to reach 90% of forms filled 90% without
help: you read every form before pressing Submit, so an answer typed and outlined in orange is still
supervised. It is listed in the run's `review`, and the run still counts as ready.

**Never typed unless certain** (`answer.SENSITIVE_RE`): self-identification, salary, restrictive agreements
and non-competes, conflicts of interest, criminal history, background and drug checks, signatures and
certifications. AI-use attestations are never answered at all.

**A submit teaches the bank** (`forms.learn_from_submit`). Every reviewed answer you submit unchanged
becomes an approved entry (`by: approved-by-submit`); one you edited in the window becomes your edited
answer. Answers that name the company, or questions about `{company}`, are never reused elsewhere.

A run is **ready** when every required field holds a verified value (by any typed tier), nothing sensitive
or declined is open, no widget is unrecognised, and there is no visible captcha. An invisible captcha is a
note, not a blocker. **Silence is not certainty**: an optional field nothing covers is left empty and listed.

**Profile fields are certain only for the question they were written for.** An early real run
answered "Clearance eligibility" with `No` from `clearance_option`, written for "do you hold
an *active* clearance?". A regex matching a word is not evidence the question is the one the field answers;
a near miss is inferred, and on a sensitive question it is asked.

The metric: **the share of forms whose required fields were at least 90%
filled with no help** (`report.fill`, `apply-stats`).

## Issues: the interface for corrections

An Issue is how the tool asks, and a Response is how you answer. Both are plain JSON lines in the run
folder, so the dashboard, the CLI (`jobhunt.py issues`) or a text editor can answer them.

```
runs/<id>/issues.jsonl     {id, kind, field, label, options, proposal, why, screenshot}
runs/<id>/responses.jsonl  {issue_id, action, value, scope, at}
```

| kind | the tool is saying |
|---|---|
| `question` | I have nothing for this |
| `confirm_match` | I think this is the same question as "Are you authorized to work in the US?" |
| `confirm_choice` | from your facts I would pick "Yes, eligible to reinstate" |
| `review_draft` | here is a draft in your voice |
| `blocked` | captcha, login, unsupported widget, or a page I don't understand |
| `correction` | (raised by you) this value you filled was wrong |

| action | means |
|---|---|
| `answer` / `edit` | use this value |
| `approve` | the proposal is right |
| `not_same` | the match is wrong; ask me properly |
| `skip` | leave it for me in the window |

`scope` is `global` or `company`, because "History with Acme" is Acme's question and nobody else's.

**Every response teaches the bank.** An approved `confirm_match` becomes an alias, and the same question
is *certain* on every future form. An approved draft becomes a bank entry. A `correction` replaces a bank
entry and is logged. This loop is how the single-click rate rises: the tool asks each thing once.

`control.json` (resume, abandon) stays as it is; it is about the run, not about answers.

## The answer bank

The bank stays a Markdown file you can edit by hand (`notes/answers.md`, `answers.py`). Each entry
gains:

- `aliases`: phrasings you have confirmed mean the same thing;
- `scope`: `global` or a company;
- `kind`: `fact` (literal), `choice` (a canonical value like `yes` that is mapped onto each form's own
  option wording by `_pick_option`), or `prose`;
- provenance: who approved it and when.

Matching is exact, then confirmed alias, then a model that *proposes* a match. A proposal raises
`confirm_match`; it never fills anything. The current `difflib` ratio of 0.92 (`answers.py:204`) is kept
only as a cheap way to surface candidates for that proposal.

## The resume: one variant per posting (20 October 2026)

Automated screens match a resume against the posting's own words, and a resume that says "agentic
system" to a screen looking for "AI agent" is read as not having it. So when you click Apply, the run
builds a PDF variant of your resume keyed to that posting's vocabulary and uploads it in place of the
base file. The content stays yours. Exactly four operations are permitted, each only from an entry you
have approved in `profiles/<name>/resume/terms.md` (`terms.py`):

1. **Term alignment**: a phrase in a bullet is swapped for the posting's term when an approved synonym
   group says they mean the same thing. Only bullets; never the name, headings, employers or degrees.
   A group applies only when the posting uses exactly one of its members.
2. **Skills line**: a SKILLS section listing your approved skills vocabulary, every one of it, ordered so
   the ones the posting names come first.
3. **Headline**: one line under your name, from an approved list of titles you have held or honest
   equivalents, chosen because one of its phrases matches the posting's title. Never the posting's title
   itself.
4. **Bullet order**: within one job, the bullet naming the most of the posting's terms is moved first
   when it beats the current first bullet. Its text does not change. A job marked `data-fixed` keeps
   its order (two-bullet narratives).

Nothing else: no new sentences, no numbers, no claims. The model may *propose* a synonym group, a
skill or a headline (`tailor.read_posting`); a proposal is pending until you approve it on the
Answers page or with `jobhunt.py resume approve`, and only approved entries are ever applied. A
synonym or skill proposal is stored only when the phrase it names is already on your resume; what the
posting asks for that the resume does not say is listed in `diff.md` as a gap, for you to read, never
proposed. Early variants may be identical in text to the base resume; only the file name differs.
That one call runs on `[llm.resume]` in `config.toml` (Fable at low effort, 20 October 2026: reading a
posting for its terms is extraction, not judgement, and the call sits between the click and the
window), and `tweaks.json`, `diff.md` and the run record name the model that read the posting.

The source is `resume/master.html`, transcribed verbatim from the base PDF (text, page count and link
annotations pinned by `test_master_matches_original`; `jobhunt.py resume check --pin` records the
baseline). Variants render it to PDF in a headless browser of their own, before the apply window
opens, and are kept under `resume/variants/<day>-<tag>/` with the PDF, the exact HTML, a
human-readable `diff.md` (what changed, why, and the posting line that justified it) and
`tweaks.json`. `resume/index.jsonl` maps each variant name to its posting and run; the dashboard
serves variant files only through that index. The file name `<Prefix>_rev_<YY-MM-DD>-<4 hex>.pdf`
carries the build day and a tag from the posting id, so a recruiter's copy traces back to its posting.

The applicant agent reads the base resume, never a variant: it must cite your words. `reset` never
touches `resume/`. No master, a failed build, or a variant that spills past the master's page count:
the base resume is uploaded and the run's steps say so. `jobhunt.py resume variant <id>` builds one
without applying; `resume propose` reads the saved postings ahead of time so proposals can be approved
in one sitting, in the spirit of the next section. `apply --no-tailor` and `[apply] tailor_resume =
false` turn it off.

## Planning ahead: clarify before the click

The single click is only single if the questions were asked earlier. So when a posting is saved:

1. `plan` fetches its question schema: the Greenhouse GET today, and for other platforms the FormSchema
   cached from the first time `read` saw that board's form (forms on one board rarely differ much).
2. `answer` runs offline against it.
3. Whatever is not *certain* becomes an Issue on a batch, not on a run: "7 new questions across 12 saved
   postings".

You answer the batch in one sitting. When you later click Apply, the form fills to *ready*.

## The applicant agent: answering in your voice

The tool never drafts your resume. It may re-key an approved copy of it for one posting, within
exactly four operations (see *The resume: one variant per posting*). Application answers are
different: they are proposals you approve or edit, and your edits are the most valuable signal the
system receives.

`persona.py` holds the applicant agent (`applicant_agent`; it works for any profile). It is fed a
question and answers as the applicant, **or declines**. It answers only from a numbered knowledge base
of what the applicant has actually said:

| ids | source |
|---|---|
| `F*` | `notes/about.md`, the same facts the scorer reads |
| `P*` | `profile.toml` identity, links and answers |
| `B*` | approved answer-bank entries |
| `R*` | the resume, extracted from the PDF (the base resume, never a per-posting variant) |

and writes in the voice of `notes/voice.md`: your own short answers, real writing (resume bullets
verbatim), how you type in chat, and the corrections you made to earlier drafts.

Two model calls, each returning JSON, with deterministic checks between them:

1. **Answer**: every line relied on is cited by id; deduction is allowed (a U.S. citizen is a U.S. person
   under export rules), invention is not. Preferences and motivations ("why Acme?") need a line that
   states them, and the job description is never evidence about the applicant.
2. **Check** (prose only): given the answer and only the lines it cited, list unsupported claims and the
   tells of model prose; revise away tells without adding anything.

Between and after: cited ids must exist, a choice must be one of the offered options (strong match), em
dashes go, a stock-enthusiasm word is flagged unless it is your own word in the cited lines. **An
unsupported claim is a decline**, and a decline names the fact that would let it answer, so the gap is
closed in `about.md` or the bank rather than papered over.

`jobhunt.py ask "question"` asks it directly; `jobhunt.py ask --pending` runs it over the pending bank
questions and stores each result as a proposal (`by: applicant_agent`, still pending) or a decline in the
entry's note. A run shows a stored proposal on the Needs-you panel as a draft; nothing the agent writes
is typed into a form until you approve it, at which point the entry becomes yours (`by` is cleared).

`notes/voice.md` is the one place voice lives. Nothing model-written goes in it except drafts you have
already rewritten, and marked tool notes.

## Evidence, not taste

What real forms have taught so far, and where every run's logs live, is in
[`apply-rehearsals.md`](apply-rehearsals.md).

Rules about applying are checked against real forms, as weights are checked against real picks.

- **Rehearsal mode** runs `open` to `verify` against a real posting and closes the window. It never
  submits, so it is safe to run against every saved posting.
- Every run records: fields by tier, Issues by kind, and **what you changed in the window** before the
  confirmation appeared (the attend loop polls field values; a value that differs from what was filled is
  a miss the tool did not know about).
- **The single-click rate** is the share of runs that reached *ready* with no Issues and no edits in the
  window. `jobhunt.py apply-stats` prints it, and a change to answering rules is judged by it.

## What exists and what changes

Kept: the run folder and state machine (`runs.py`), `control.json`, the Markdown bank and its
generalise/specialise helpers (`answers.py`), label resolution in `FIELD_SCAN_JS`, option matching
(`_pick_option`), the react-select handling (`_set_combobox`), the attend loop's confirmation detection,
and the layering pinned by `test_no_import_cycles`.

Changed:

| today | becomes |
|---|---|
| one regex cascade decides and acts (`value_for`) | `answer` (pure, tiered) and `fill` (adapters) |
| typed means done | `verify` reads everything back |
| fresh Chromium per run (`forms.py:855`) | persistent real-Chrome profile under `profiles/<name>/data/browser/` |
| main frame only | every frame |
| one pause kind, "questions" | typed Issues and Responses |
| fuzzy text match | exact, confirmed alias, proposed match |
| one-shot drafter, voice unwired | the applicant agent (`persona.py`): grounded in a cited knowledge base, declines otherwise |
| fixtures only | rehearsal runs on real postings, single-click rate |

## Order of work

- 24 Sept: the stages (`schema`, `read`, `answer`, `widgets`, `verify`, `issues`, `browser`, `stats`).
- 26 Sept: the applicant agent (`persona.py`), then the audit toward 90% of forms filled 90% with no help:
  the typing policy, self-healing (`heal.py`), capture and `replay`, aggregator resolution
  (`resolve.py`), the generic navigator (`navigate.py`), multi-page forms (`pages.py`), Workday
  (`playbooks/workday.py`, `vault.py`), and `prepare`.
- Next, by evidence: bank v2 (company scope, confirmed aliases), section context for repeated education
  and experience blocks, and whatever the nightly `prepare` digest keeps listing.

Every change is checked with `jobhunt.py replay` (offline, every captured form) and a rehearsal of the
fixed 40-posting sample in `profiles/<name>/data/apply_sample.json`.
