# jobhunt

A small, local job-search toolkit for more than one person. No accounts, no cloud, no database:
a folder of plain files and a dashboard on `localhost`.

```
scan     →  pull postings from public job-board feeds once, score them for every profile
serve    →  review / sort / track them in a local dashboard (profile switcher in the header)
apply    →  open the application form in a browser, fill it from that profile and its answer bank,
            then pause: the Needs-you tab says what it is waiting for; you press Submit
```

Every decision you make in the dashboard (save, apply, skip with a reason) is remembered: companies
you keep saving or skipping become facts on the About page, and `retune` uses your own choices to
check whether a weight change made the ranking better or worse.

## Layout

```
config.toml                  shared: location vocabularies, company boards, source toggles, LLM settings
profiles/<name>/search.toml  what this person wants (titles, keywords, pay, clearance, how far you'd travel)
profiles/<name>/profile.toml identity + answer bank for application forms (never leaves this machine)
profiles/<name>/<resume>.pdf the PDF the apply assistant uploads (documents.resume in profile.toml)
profiles/<name>/resume/      master.html (the resume as HTML), terms.md (approved wording the apply
                             assistant may key to a posting), variants/ (one PDF per application, with
                             what changed and why), index.jsonl (which variant went to which posting)
profiles/<name>/notes/about.md  facts about the person (first person); the only text the model is told
profiles/<name>/notes/answers.md the answer bank: every form question met, with the answer you approved
profiles/<name>/data/        jobs.json (found), state.json (decided), judgements.json (what the model
                             read), grade_curve.json (the frozen cutoffs), audit.jsonl (every change
                             to the scoring model), learned.json (what your decisions taught us)
profiles/<name>/data/runs/   one folder per apply run: run.json (state), steps.jsonl, screenshots, log.txt
data/postings.json           raw cache of the last fetch, shared, so `rescore` works offline (~100MB:
                             deliberately wider than the title gate, so widening a keyword later is
                             a rescore rather than a fresh network scan)
deploy/                      launchd templates (not installed; see deploy/README.md)
jobhunt/                     the code, laid out as the pipeline runs:
                             fetch/    sources.py (the feeds)
                             classify/ gates.py (keep or drop) · filters.py (measure) ·
                                       judge.py (the model reads) · relevance.py (the arithmetic) ·
                                       facts.py (the About page) · learn.py (your decisions)
                             report/   server.py + dashboard.html · views.py (which tab) · about.py
                             apply/    forms.py (form filler) · answers.py · runs.py
                             pipeline.py (the stages) · store.py · profiles.py (layering) ·
                             audit.py · jsonfile.py · geo.py · llm.py (optional) · core.py (shim)
tests/                       uv run python -m unittest discover -s tests
```

A profile's `search.toml` is layered over `config.toml`: any table it defines overrides the shared
one key-by-key, and its `[[companies]]` are added. A shared company with `profiles = ["analyst"]` is
scored only for a profile named `analyst` or listing it in `board_tags`. Add a person by creating
`profiles/<name>/` with those two files.

**Getting started.** Two example profiles ship with the repo, `profiles/example-engineer/` and
`profiles/example-analyst/`. Copy one to `profiles/<your-name>/` and edit its three files. Real
profiles are gitignored, because `profile.toml` holds contact details and EEO demographics and
`notes/about.md` holds a person's history — none of that belongs in a repo. The `profiles = [...]`
tags on `[[companies]]` in `config.toml` name a kind of search (`engineer`, `analyst`); set
`board_tags = ["engineer"]` in your `search.toml` to see those boards. Your home `origin`, pay
target and floor, and any source anchored on a place go in your `search.toml` too, never in
`config.toml`.

**Parking a profile.** `enabled = false` at the top of a `search.toml` takes it out of circulation:
its boards are not fetched, its postings are not scored and the dashboard does not list it. Nothing
under `profiles/<name>/` is touched, so setting it back to `true` picks the search up exactly where
it left off. `jobhunt.py profiles` still lists parked profiles so they never look like lost data.

## Setup (once)

Python 3.12+ via [uv](https://docs.astral.sh/uv/). Playwright is only needed for `apply`.

```bash
cd job-hunt
uv sync
uv run playwright install chromium
```

Then, per profile: put the resume PDF in the profile folder, fill in `profile.toml` (contact details,
salary expectation, start date, free-text answers in your own words), and skim `search.toml`.

Optional federal postings (USAJOBS) need a free API key: request one at
https://developer.usajobs.gov/apirequest/ and add `USAJOBS_API_KEY=...` and `USAJOBS_EMAIL=...`
to a `.env` file in this folder. Without it, that board just logs a one-line error on scan.

## Daily use

```bash
uv run python jobhunt.py serve          # http://127.0.0.1:8765 — pick a profile top-left
```

The landing page is the **New** tab: postings sorted by relevance and shown as grades, 50 per page with Prev/Next. Act on a
row and the next one fills in. **Save** parks a posting in the Saved tab; **Skip** drops it into Skipped, where you can add an
optional comment. **Undo** on any row puts it back where it was before. A group of location variants
follows the one you acted on, so a saved or skipped posting stays put across scans. **Apply** on a
saved row (or **Apply to all saved**) starts an apply run; the **Needs you** tab tracks every run
until you have submitted it (see below). Once a run sees the confirmation page, or you press **Mark
applied**, the posting moves to **In progress**. That tab has two buttons, **No offer** and
**Withdrew**, which move a posting to **Done**; the tool does not track interviews, that is yours
from there. **Scan now** runs the whole pipeline: it fetches every board once (a few minutes for 160
boards), rates whatever the model has not read yet, and re-grades.

From the terminal:

```bash
uv run python jobhunt.py scan
uv run python jobhunt.py rescore
uv run python jobhunt.py profiles
uv run python jobhunt.py --profile <name> list --min-grade B
uv run python jobhunt.py --profile <name> apply greenhouse:databricks:8760167002
uv run python jobhunt.py --profile <name> runs            # apply runs waiting on you
uv run python jobhunt.py --profile <name> status <id> skip --reason travel
uv run python jobhunt.py --profile <name> status <id> done --done-reason "no offer"
uv run python jobhunt.py --profile <name> export jobs.csv
```

## How the scanner works

Sources are the public JSON feeds that company job boards expose — the same ones their own careers
pages load. Seven applicant tracking systems are read directly: **Greenhouse, Lever, Ashby,
SmartRecruiters, Workable, Breezy and Recruitee**, plus **Workday** through the endpoint its own
front end calls. These need no key, have no quota, and carry the full description, which makes them
the best source there is. `jobhunt.py discover` finds which one a company uses.

Public aggregators (Remotive, RemoteOK, Himalayas, The Muse) and keyed ones (Adzuna, Jooble,
USAJOBS, and Google Jobs via SerpApi) widen the net. The Hacker News "Who is hiring?" thread catches
startups you would never find otherwise.

**Keyed sources have budgets, not just rate limits.** Adzuna's free tier is 1,000 requests a week and
Jooble's is 500 for the life of the key, so `[quota]` in `config.toml` caps each one and the cap is
checked before the first request. A source whose allowance does not refill can never be a per-scan
source, which is why Jooble is parked and used once, for discovery.

`docs/job-boards.md` is the full audit: every board carrying US software jobs, whether it has an API,
what it costs, and what reaches the four big boards that have none.

Each board is fetched once per scan. A posting is **kept** for a profile when its company is not on
that profile's `company_exclude` list, its title matches a tier-1 or tier-2 term in that profile's
`[search]` and no exclude term, and its location is inside the US and within `radius_miles` of home.
`company_exclude` is matched as a substring of the company name, so one entry covers every legal
variant, and it applies whichever source listed the job — your current employer typically reaches
the scan from its own board and from two or three aggregators at once.

A kept posting is then **measured**: how far away it is, what it pays, whether it needs a clearance,
how much travel it states, how old it is. Measuring is not scoring. What any of that is worth is a
weight, and weights live in one place (`[scoring]` in `config.toml`). Gates decide whether a posting
exists at all; weights only decide where it sits. A weight can never act as an exclusion, and an
exclusion can never be argued away by a good score somewhere else.

The **judge** stage then asks the model to read your About page against each posting and rate eight
attributes from -10 to +10. That is the only thing in the pipeline that calls a model. Ratings are
cached per posting in `data/judgements.json` and re-read only when something they depend on changes
(see *What invalidates a rating* in `AGENTS.md`). `scan --no-judge` skips it; `scan --judge-limit
200` caps one run and the rest is read next time.

The **score** stage is arithmetic over those ratings. See *Relevance* below.

**The model does not judge where the job is.** Location is settled before a posting reaches it:
anything past `radius_miles` is dropped, anything asking for more presence than you could give is
retired, and what survives is a job you could actually take. Letting the model apply the location
preference again buried every distant role with reasons like "breaks the absolute location
requirement", which made a whole tab permanently empty. It rates the work; `location` is a measured
attribute with its own weight.

### Stages

A scan is eight named stages, each individually runnable and individually tested:

```
plan     which boards, for which profiles                 pure
fetch    network -> list[Job]                             checkpointed per board
cache    list[Job] -> data/postings.json                   atomic, deliberately wide
gate     which postings exist at all for this profile      pure
measure  what the posting states about itself              pure
judge    the model rates fourteen attributes                 checkpointed, resumable
score    dot product + grade curve                         pure arithmetic
store    variants, retirement, persist                     atomic
```

```bash
uv run python jobhunt.py scan                       # all of them
uv run python jobhunt.py scan --stage score --stage store   # replay the cache, no network
```

An interrupted `fetch` resumes from the boards it had already banked instead of throwing away ten
minutes of network, and a long `judge` checkpoints every 40 postings.

**New shows only postings at or above `min_grade`** (`[scoring]` in `config.toml`, default `B-`; a
profile's `search.toml` can override it). Everything below sits behind the *hidden* link at the
bottom of the tab, which also says how many the model has not read yet. A posting the model has not
read has no grade and is always below the floor: imputing one would rank it against postings that
were actually read. `list --min-grade F` prints everything rated.

## Finding companies you have never heard of

The scan is company-first: you give it a board token and it fetches that board. For Greenhouse, Lever
and Ashby the company name is an **input**, never an output, so on its own the tool can only find work
at employers someone already typed in.

`jobhunt.py discover` inverts that.

```
harvest   every source that names an employer  ->  company names
resolve   name -> candidate board tokens
probe     token -> is there a live board?
assess    does that board carry work you want and could reach?
promote   yes -> companies.discovered.toml; no -> parked, rechecked in 90 days
```

```bash
uv run python jobhunt.py discover --harvest   # collect names, no network
uv run python jobhunt.py discover             # probe them and promote what qualifies
uv run python jobhunt.py discover --workday   # sweep Workday for the ones no feed answered for
uv run python jobhunt.py discover --status    # what is known, and what is due for a probe
```

**Nothing is scraped.** Names come from the posting cache a scan already wrote, from
`tools/board_candidates.toml`, from HN Who-is-hiring, and from the aggregators that report an
employer — The Muse, Remotive, RemoteOK, Himalayas, and (once their free keys are in `.env`) Adzuna
and Jooble, the only two sources that are both keyword-queryable and name the employer. The first run
needs no new network source at all: the existing 155 MB cache named 597 companies, and most had no
configured board.

**Token guessing is self-verifying.** A Greenhouse board returns `company_name` on every posting, so
probing `scaleai` answers "Scale AI" and a wrong guess identifies itself rather than quietly entering
your scan as the wrong employer. Ashby and Lever carry no name, so there the token is the identity.

**Promotion runs the real gate.** A board is kept only when `Matcher.gate` would keep at least one of
its postings — the same title, location and reachability test the daily scan applies — so a promoted
board cannot produce postings the scan then throws away.

**A board that answers but has nothing relevant is parked, not discarded.** Its token is known and
rechecking costs one request, which catches the day it posts the role you were waiting for.
`data/companies.json` remembers every token tried, so next month does not re-guess this month's dead
ones.

**Workday is a separate, slower pass.** It has no public feed, only the endpoint its own front-end
calls, addressed by tenant *and* site with no way to ask which site a tenant uses — so finding one is
a sweep of four hosts against fourteen site names. That is why `--workday` is opt-in and only spent on
companies already known to post relevant work. It matters because most large government contractors
live there and answer nothing else.

Discovered boards land in `companies.discovered.toml`, loaded beside `config.toml` but never written
into it, so the hand-curated file stays readable and deleting the generated one resets discovery.
`[discovery] max_daily_boards` caps the daily scan — 160 boards is already a 155 MB cache and seven
minutes — parking the least promising past the cap and saying so.

## Can you actually get to it?

That is a **gate**, not a score. A job that needs you in an office three hours away three days a week
is not a worse job — it is one you cannot hold, and being excellent in every other respect
does not change that. Gates decide whether a posting exists at all; weights only decide the order of
the ones you could take.

Two facts are compared. **Reach** is the most often you could show up, which distance alone decides:

| band | distance | under `remote_or_commutable` | under `remote_or_daily` |
|---|---|---|---|
| `remote` | no office to reach | **New** | **New** |
| `daily` | ≤ `daily_miles` (60) | **New**, level with remote | **New** |
| `monthly` | ≤ `monthly_miles` (120) | **New**, ranked below remote | dropped: an office you would have to visit |
| `quarterly` | ≤ `radius_miles` (500) | **New** | dropped |
| `relocate` | further | dropped when `keep_us_onsite = false` | dropped |

`[location] mode` picks the column. `remote_or_daily` means remote, or the home metro, and nothing
you would have to travel to however rarely. A location the gazetteer cannot
place is kept under either mode, because silence never drops anything.

**Need** is how often the posting says you must be there. It comes from the board's structured onsite
flag, or from a cadence the description states in words ("3 days per week in the office"), read from
the full text rather than the truncated copy the model sees. If need exceeds reach, the posting is
retired with a reason that names both sides: *needs you on site several days a week, and you could
manage a day or so a month*.

**Silence never drops anything.** Only about 110 of 960 postings state a cadence at all, and a gate
deletes — so it fires only on evidence that cannot reasonably be misread. A posting that says nothing
is kept.

Anything you have already acted on stays in the tab you put it in, so nothing is lost when the gate
retires it — it just stops crowding New.

Everything that survives the gate goes in **one list**. Distant onsite roles used to get a tab of
their own, because nothing knew how often a job needed you and a 300-mile office would otherwise have
crowded the shortlist. That is what the gate is for now, so they rank on merit with the rest and the
**frequency** column says what kind of trip each one would be.

Location is measured, not pattern-matched: each string is resolved against the US Census place
gazetteer bundled at `jobhunt/data/us_places.csv` (32,000 places, public domain, no network) and
compared with `[location] origin`. Two kinds of match are deliberately coarse and can never claim to
be near you — a name the gazetteer cannot place (a regional nickname listed in `commutable`) bands as
`monthly_miles` away rather than zero, and a bare state name resolves to a centroid that may happen
to sit near you, so "Ohio" alone can never read as a daily commute.

Among the jobs you could take, distance is an ordinary weighted attribute: remote and an office at
home rank together at the top, and every other distance costs more with the miles.

Postings that vanish from their feed for two scans in a row are marked closed and drop out of the
default view (tick *show closed*; anything you shortlisted or applied to stays visible). A board
that fails to load is named under the scan time on the dashboard, because a dead board contributes
nothing and is otherwise invisible.

## Relevance: how a posting is ranked

Every attribute is measured from **-10 to +10**. The sign says whether it helps or hurts, and **zero
means the attribute is absent**, so a posting is never punished for staying silent about something.
Every weight is 0..10 and says only how much you care; 0 turns an attribute off.

    relevance = overall_fit x w + sum(role attribute x w x role_weight/10) + sum(practical attribute x w)

Signed measures are the point. A job can be excellent in some respects and bad in others, and those
have to subtract: a role that requires being in an office should *lose* relevance, not merely gain
less than a remote one. A 0..10 desirability scale cannot express that, because "no on-call
mentioned" and "this job is mostly on-call" would share an end of it.

Seven **role** attributes are rated by the model reading the posting against your About page
(`[scoring.role]`): product ownership, domain, feedback loop, seniority, work shape, pace and equity
stage, plus `enterprise_software`, which is read by a gate (below) and carries no weight. Five
**practical** ones are measured from the posting itself and never guessed (`[scoring.practical]`):
pay, distance, travel, freshness and clearance. One **overall_fit** attribute is the model's single
read of the whole role.

**Every attribute counts on its own:** its measure times the multiplier shown in the posting's table
(a role attribute's weight is also scaled by `role_weight / 10`). The table is ordered by that
multiplier, so the first rows are what matters most. The shipped order: pay,
remote, travel, ownership and the overall read on top; freshness just under; domain, feedback loop
and seniority next; everything else low. See AGENTS.md for why the role attributes are no longer
averaged.

**Travel is never a plus** (any travel is a cost): up to `[travel] max_pct_ok` it measures -4, falling
to -10 at 2.5x that; a stated 0% or nothing said is 0. **Freshness is the posting's age**: +10 under a
week, 0 at two weeks, -10 at a month and beyond (`[recency]`); an undated posting ages from the day the
scan first saw it, so nothing stays fresh by saying nothing. There is no age gate: a stale posting
sinks, it is not removed.

**Enterprise software is a gate**, the only one driven by the model: a posting rated 7 or more on
`enterprise_software` is retired unless it states a base above `[enterprise] pay_floor` (0, off, until a profile sets it).
Silence on the attribute is 0 and never trips it; unstated pay does not clear the floor.

**Location is the commute's cost:** office days a week (stated in the posting when it says, else 3)
times a per-trip cost that grows with the square of the drive (minutes from distance at 45 mph).
Remote is +10, no commute +9, a 30-minute daily commute +4, and more than an hour each way more than
once a week is -8 or worse. `[location] commute_mph` and `assumed_office_days` set the two assumptions.

**Grades, not numbers.** The total has no units, so it is never shown as a score and there is no
numeric floor — its scale moves whenever a weight moves. Grades are cutoffs at fixed distances from
the mean of what was actually scored, A+ two standard deviations up. The distribution was checked
before choosing that: across 200 rated postings it came out near-normal (skew -0.16, excess kurtosis
-0.37). Quantiles were rejected deliberately, because they hand out the same share of A+ every week
no matter how poor the week was. The curve is frozen and refitted only when the weights change, so a
posting's grade cannot drift because other postings arrived.

```bash
uv run python jobhunt.py explain <job_id>   # every term, its measure, weight, points and evidence
uv run python jobhunt.py retune             # re-grade from stored ratings: no network, no model
uv run python jobhunt.py config             # the effective weights, and complaints about them
uv run python jobhunt.py audit              # every recorded change to the scoring model
```

`retune` also prints where your own saved and applied postings rank, which is the only label that
exists. It says how many of them were chosen from a list that different weights had sorted, because
those are not independent evidence — a decision records the weights hash that produced the list it
was made from, so the comparison can be honest about being circular.

Ratings live in `data/judgements.json`, keyed per posting with the revision of every attribute, a
hash of the text they were read from and a hash of the facts the model was told. Rewording one
attribute re-rates one slot per posting rather than discarding the corpus; editing a fact re-rates
everything, because a rating is a judgement of a posting *against a person*. Batching was checked
before being trusted: rating the same postings alone and in batches of four disagreed by 0.68 points
on average, against a 1.5 bar.

`AGENTS.md` is the full methodology — what the attributes mean, what invalidates a rating, and the
rules a change has to keep.

## The facts are what the model reads

`notes/about.md` holds one fact per bullet, in the first person. They are **not** a scoring system.
They are the only thing the model is told about you, and they reach a score by changing what it
reads before it rates a posting.

Write them as sentences rather than keywords. "I do not want to do release engineering" and "I have
done release engineering" mean opposite things, and a term list collapses them into the same thing.
That is what the previous design did — each fact carried a weight and an LLM-derived bag of regex
terms — and it is why that design is gone: the weights were a second scoring system beside the real
one, and the total saturated on a third of postings, so tuning it changed nothing.

Editing a fact does not re-score anything by arithmetic. It marks every rating made under the older
facts as stale, the About page says how many, and the next scan reads those postings again.

Your actions feed back as facts too: companies you keep saving or skipping become facts tagged
"learned from your actions" (delete one and it stays gone).

## The audit trail

Every mutation to the scoring model appends a line to `profiles/<name>/data/audit.jsonl`:
`facts_changed`, `judged`, `weights_changed`, `curve_refit`, `scan`. Read it with
`jobhunt.py audit [--kind judged] [--full]`.

The pathways are deliberately not uniform and the log does not pretend they are — a fact changes a
score by changing what the model reads, a weight changes it by arithmetic, and a curve refit changes
neither but renames every grade. Changes are detected by comparison against the last entry of the
same kind rather than by hooking every writer, so editing `about.md` in a text editor is recorded on
the next run just the same.

## How the apply assistant works

**The flow:** press **Apply** on a posting. The form opens and fills as completely as it can. The
run's panel lists what still **needs you**; answer there and press **Send to form**, or type into the
window yourself. Read the orange-outlined answers, then press the employer's Submit. Everything you
answered is remembered for the next form (on submit, or when you press **Mark submitted**).

The design this is moving toward, and the rules any change must keep, are in
[`docs/apply-design.md`](docs/apply-design.md).

**Apply** opens the posting's application page in a visible Chromium window, reads every form
field, and answers the ones it can map to the profile's `profile.toml` (name, email, phone,
location, current company/title, links, resume upload, the standard eligibility questions, consent
boxes, `[answers.custom]` patterns) or to an **approved** entry in the answer bank. Then it
**pauses**. It never presses Submit: you read the form in the open window and submit it yourself.

Every attempt is a **run**, a folder under `profiles/<name>/data/runs/<run_id>/` holding
`run.json` (the state and everything that was filled, flagged or asked), `steps.jsonl`, a screenshot
at each step and the process log. The **Needs you** tab lists the open runs and why each one is
waiting:

| reason | what it means |
|---|---|
| review and submit | **ready**: every required field is certain and read back correctly; read the window and press Submit |
| fix fields | a value did not stick, the page marks a field invalid, or a field could not be filled |
| questions | a required question has no approved answer; answer it on the run's panel, then **Resume** |
| unsupported form | Workday, USAJOBS or a custom portal: the window is open for you to fill by hand |
| captcha | solve it and finish in the window |
| error | something broke after the page opened; the window is still there |
| window closed | the window went away before a confirmation was seen: **Mark submitted** or **Abandon** |

While a run waits, it watches the window. When it sees the confirmation page it records the
confirmation text, moves the posting to **In progress** and closes the window. **Resume** re-reads
the answer bank and fills what it now covers, without touching anything already typed. An apply
process that dies is marked failed with an explicit error on the next dashboard load; nothing fails
silently. `[apply] max_open_windows` in `config.toml` caps how many windows wait at once; the rest
stay queued.

Answers from your profile or approved bank entries are typed as they are; near misses and the applicant agent's answers are typed for your review (see below). After filling, every value is read back from the page, so a dropdown that swallowed a click is caught rather than assumed.

Each run also writes `issues.jsonl`, the same questions in a form any client can answer:
`jobhunt.py issues [run]` lists them and `--answer ID TEXT`, `--approve ID`, `--skip ID` or
`--not-same ID` answers one (an open window resumes at once).

Windows open in your installed Chrome with a persistent profile under `profiles/<name>/data/browser/`
(`[apply] browser`, `persistent_profile`), so logins and cookies carry over between runs. Forms
embedded in an iframe on an employer's own career page are read like any other.

**Rehearsal** fills and verifies a real form, then closes it without submitting:
`jobhunt.py rehearse` runs it over your saved postings (or `apply <id> --rehearse` for one), and
`jobhunt.py apply-stats` reports how close applying is to one click: the share of runs that were
ready with no issues, and, for submitted ones, whether you changed anything in the window first.
Each batch writes `runs/rehearsal-<stamp>.md`, and each run a `fields.json` recording every field
read and every decision made; [`docs/apply-rehearsals.md`](docs/apply-rehearsals.md) says how to use them.

**Where the form is.** Aggregator listings (The Muse, Hacker News, RemoteOK) are followed to the
employer's real form; a custom careers site is walked to its application; Workday signs in with, or
creates, an account per employer (passwords generated and kept in an encrypted vault: run
`uv run python jobhunt.py vault init` once; a verification email pauses the run until you click it).
Multi-page forms are filled page by page and stop at the review page.

**Answers the tool is not certain of** are still typed when they are grounded (a near match, or the
applicant agent's answer with its evidence), outlined in orange in the window and listed as "typed for
your review". Sensitive questions (demographics, salary, non-competes, criminal history,
certifications) are only typed when certain. Submitting without changing them approves them for
future forms; what you edit becomes the answer.

**Nightly, optionally:** `jobhunt.py prepare` rehearses every saved posting and writes a digest of only
what still needs you. `jobhunt.py replay` re-checks the rules against every form ever captured, offline.

**The employer's autofill goes first.** Where the form parses an uploaded resume (Lever, Ashby), the
resume is uploaded before anything else and what the page filled is checked: kept when it matches your
profile, corrected when it does not, proposed to you when it cannot be checked.

**The applicant agent** (`jobhunt.py ask "question"`) answers a question as you from a numbered
knowledge base (about.md, profile.toml, approved bank answers, your resume), citing the lines it used,
and declines, naming what it would need, when those do not settle it. `jobhunt.py ask --pending`
proposes answers for the pending questions in the bank; they stay pending until you approve them.

### The answer bank

Every question a form asks that `profile.toml` cannot answer is written to
`profiles/<name>/notes/answers.md` as a **pending** entry, with the company name replaced by
`{company}` so one entry covers every employer. The **Answers** page (header) is where you answer
them; an approved answer is pasted verbatim on every future form that asks the same or a very
similar question. Model drafts, when an LLM backend is on, appear only as a suggestion on the run's
panel for you to edit and approve: nothing model-written is typed into a form until you have
approved it, and the bank stays yours. The file is plain Markdown and can be edited by hand.

It refuses on purpose to answer questions about AI use in the application and demographic/EEO
questions (unless `eeo = "decline"`, in which case it picks the decline option). Checkbox groups
("languages you speak", citizenship and export-control attestations) are treated as one
multi-choice question: languages come from `languages`, US-citizen and "none of the above"
attestations follow `citizenship`, and anything else can be mapped in `[answers.choices]` or the
bank. Custom dropdowns (react-select) are opened and read before answering. Supported forms:
Greenhouse, Lever, Ashby. Workday, USAJOBS and custom portals open for you to complete by hand, and
the run still tracks them.

### Reset

**Reset** (dashboard header, or `jobhunt.py reset`) clears what is in play between testing and real use:
postings you never decided on, the Saved tab, apply runs and the raw posting cache. Saved decisions are
archived rather than deleted, so learning and `retune` still use them; a posting comes back as New if a
later scan sees it. It never touches your About facts, answers, voice, profile, resume (nor its master,
terms and variants), logins, ratings, or anything you skipped or applied to. Old run folders move to `profiles/<name>/data/archive/`.

## The About page

**About <name>** in the header opens the profile's About page: the facts list, written in
the first person, and a file upload (PDF, DOCX, TXT, MD) that is saved under `notes/uploads/` for
reference and digested into new facts. The page says how many ratings were made under older facts,
which the next scan will read again. The facts are also the only text the LLM hooks read.

### LLM hooks

`[llm] backend = "claude-cli"` (the Claude Code CLI you're logged into) or `"anthropic"` (uses
`ANTHROPIC_API_KEY`) in `config.toml`; `"none"` turns them off, in which case uploads are digested
by a keyword heuristic instead. Uncovered free-text questions are drafted by the applicant agent
(`jobhunt.py ask`), from a cited knowledge base or not at all, and shown as suggestions on the
Needs-you panel, never typed into a form. `scan --notes` writes a short fit note for new postings.
A task can run on its own model and effort with a sub-table, `[llm.resume]` for the call that reads a
posting for the resume variant; whatever it leaves out comes from `[llm]`.

## Running unattended

Nothing is scheduled by default. `uv run python jobhunt.py schedule --write` regenerates the two
launchd templates in `deploy/`; `deploy/README.md` has the install commands.

## Notes on etiquette

The scanner only reads feeds that companies publish for exactly this purpose, once per scan, with
an honest User-Agent. The apply assistant fills the company's own application form, one posting at
a time, with real information you approved, and you press Submit: a faster version of you typing,
not a spray-and-pray bot. It never solves captchas and does not touch LinkedIn Easy Apply or
Indeed, whose terms forbid automation.
