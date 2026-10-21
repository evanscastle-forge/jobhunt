# Scoring methodology

Applying has its own contract, [`docs/apply-design.md`](docs/apply-design.md). This file is about
grades.

This is the contract for how a posting gets a grade. It exists so the answer is the same whoever, or
whatever, is working on the code next. If you change a rule here, change this file in the same
commit.

## This repository is public: no personal data

Nothing about a real person goes into a tracked file, a test, a fixture, a comment, a docstring or a
commit message. That covers a name tied to any fact, contact details, home city or suburb, employers
past or present, job titles, schools, resume text, pay figures or targets, clearance history,
demographics or EEO answers, health or family, companies applied to or interviewed with, counts from
someone's own pipeline ("ranked 40th of 900"), and dated "X decided" history.

- Real people live only in `profiles/<name>/`, which is gitignored: `profile.toml` (contact,
  answers), `search.toml` (titles, home `origin`, pay, gate settings, boards anchored on a place),
  `notes/` (facts, and any decision log worth keeping).
- `config.toml` holds neutral defaults. Anything that is one person's choice goes in that person's
  `search.toml`, which layers over it. Boards are tagged by kind of search (`profiles = ["engineer"]`,
  matched against a profile's `board_tags`), never by a person's name.
- Tests, fixtures and docs use the `example-*` profiles and invented values: 555 phone numbers,
  `example.com`, "Acme", "Example Robotics", the example city. Never copy a real value into a test
  to make it pass, and never disguise a real person as an example.
- Explain a rule by what it does, not by who asked for it. The reason for a rule belongs here; the
  person and the date do not.
- `test_no_personal_data_is_tracked` checks every tracked file against the real profiles present
  on the machine. It catches exact values only; the rest is on whoever writes the change.

## The shape of it

A scan is eight stages, in order, each a named function with an explicit contract:

```
plan     which boards, for which profiles                 pure
fetch    network -> list[Job]                             checkpointed per board
cache    list[Job] -> data/postings.json                   atomic, deliberately wide
gate     which postings exist at all for this profile      pure, gates.py
measure  what the posting states about itself              pure, filters.py
judge    the model rates eight attributes                    checkpointed, resumable
score    dot product + grade curve                         pure arithmetic
store    variants, retirement, persist                     atomic
```

`jobhunt.py scan` runs all of them. `--stage` runs a subset and replays the rest from the last run's
files. Nothing outside these stages may write a score.

## Gates decide existence, weights decide order

A gate answers "should this posting exist at all for this person": the company is excluded, the title
matches nothing they are looking for, the location is outside the country or beyond the radius, or
**they could not be on site as often as the job requires**. A gate returns a drop reason and the
posting is retired.

That last one is the case most likely to be got wrong, because it looks like a preference and is not.
A role needing three days a week in an office three hours away is not a worse role; it is one the
person cannot hold. Ranking it lower leaves them reading a shortlist a quarter full of jobs
they could not take. `gates.reach_gate` compares **reach** (the most often they could show up, which
distance decides) against **need** (how often the posting says they must, which only the employer can
state). Need exceeds reach, and the posting goes.

Because a gate deletes, it fires only on evidence that cannot reasonably be misread: the board's
structured onsite flag, or a cadence stated in words. Only about a tenth of postings state one at all,
and **silence never drops anything** — the model's reading of how office-bound a role feels may demote
a posting, but it must never retire one.

A profile may run the location gate in `remote_or_daily` mode: a posting is remote, or its office is
inside `daily_miles`, or it goes. An office an hour and a half away that could be visited once a
month is then not a worse job; it is one the person has decided not to hold. Travel on a remote role
is still a demotion and not a gate.

**One gate fires on a model reading**: `pipeline.retire_enterprise`. The judge rates
`enterprise_software` like every other attribute, and a posting rated at or above `[enterprise]
rating` (7) is retired unless it states a base above `[enterprise] pay_floor`, which a profile sets
(0, the shipped default, leaves the gate off). It exists for someone who will not work for an
enterprise software vendor below some figure, which no weight could express as "never, unless". The
rule keeps the shape of the others: silence is 0 on that attribute and never trips
it; unstated pay is not above the floor; the attribute carries weight 0, so the same reading can never
also demote a posting it kept. It runs after the ratings are attached, so `scan`, `rescore` and
`relearn` all apply it, and a decided posting is retired the way every gate retires one: it stays in
the tab it was put in.

A weight only decides where a posting sits in the list. **A weight may never act as an exclusion, and
a gate may never be argued away by a good score somewhere else.** This is why they live in separate
modules (`gates.py`, `relevance.py`) and why the floor is applied to the finished grade rather than
mixed into the arithmetic.

## Measures are signed, and zero means absent

Every attribute is a number from -10 to +10.

- **+10** strongly true of this posting, and good for this person.
- **0** the attribute is ABSENT from the posting, or genuinely neutral.
- **-10** strongly true in the direction that is bad for them.

Zero is not a hedge. A posting that never mentions hours is 0 for `pace`, not -3. A posting that says
"wartime" and weekends is strongly negative. This matters because a posting must never be punished for
being brief: silence is not evidence.

Weights are 0..10 and say only how much someone cares. They are never negative — the sign lives in
the measure, where the evidence is. A weight of 0 turns an attribute off.

## Every attribute counts on its own, at the weight shown

Each attribute contributes **weight × measure** to the total: the practical attributes, the model's
holistic `overall_fit`, and each of the seven weighted role attributes (`ROLE_DIMS`), which also scale
by `role_weight / 10` as a family multiplier. Every part stores `w_eff`, the multiplier actually applied, and
the dashboard and `explain` print exactly that, ordered by `w_eff` and then by name. **What the table shows
is the arithmetic**: `measure × w_eff = points`, always, and a test pins it.

This replaced averaging the role family. Averaging stopped ten correlated attributes from out-voting
the one that matters, but it capped every role attribute near ±12 and made the table read
"10 ×8 +11.9". The shipped tiers, in order:

1. `pay`, `location`, `travel`, `product_ownership`, `overall_fit` (10 each)
2. `freshness` (8)
3. `domain`, `feedback_loop`, `seniority_fit` (6 each)
4. everything else (2 or 3); `enterprise_software` at 0, read by the gate above

A test pins that order against the live `config.toml`. The cost of summing is that a posting broadly on
theme across the low-weight role attributes adds up; tier 4 is kept at 2 each so that breadth cannot
outrun the top tier.

How the attributes were consolidated into the current eight: `agentic_llm`, `autonomy_robotics` and
`mission_domain` became one `domain` rating (agentic AI, robotics, perception, simulation, defense and
the other real-world domains on the plus side; consumer on the minus; the "generic B2B vendor" pole
moved to the enterprise gate). `variety`, `toil` and `independence` became `work_shape`.
`forward_deployed`, `algorithmic_depth` and the practical `east_coast` were removed (the last is moot
under `remote_or_daily`). `seniority_fit` is a near-binary: staff, lead
or principal IC +10, senior +5, nothing stated 0, anything else -10. Travel is never positive: -4 up
to `[travel] max_pct_ok`, -10 at 2.5× that, and 0 for silence or a stated 0%. Freshness is the
posting's age, +10 under `[recency] peak_days` (7), 0 at `fresh_days` (14), -10 at `stale_days` (30)
and beyond; an undated posting ages from the day the scan first saw it. There is no age gate.

`feedback_loop`, `pace` and `equity_stage` are things a posting can show about the work: a change
you can see land within days, a sustainable pace, mid-stage equity. Neither travel nor an active
clearance requirement is a gate: both stay demotions, because a posting that says nothing about
either must cost nothing, and a stated figure is still a figure a person may choose to negotiate.

When weights change, picks made under the old weights are not independent evidence for the new ones
(see "Evidence, not taste"); `retune` is a sanity check until new picks exist.

## The total has no units

Never show the relevance total as a number a reader is meant to interpret, and never put a numeric
floor on it. Its scale moves whenever a weight moves, so "above 60" means something different after
every tuning pass.

Grades are cutoffs at fixed distances from the mean of what was actually scored — A+ at two standard
deviations up, and so on down `GRADE_SIGMAS`. Deliberately **not** quantiles: quantiles hand out the
same share of A+ every week no matter how poor the week was, whereas a sigma cutoff lets a bad crop
honestly produce no A grades. The distribution was measured before this was chosen (skew -0.16,
excess kurtosis -0.37 over 200 rated postings), so the cutoffs mean what they look like they mean.

The curve is frozen between runs and refitted only when the weights change or `retune` asks.
Otherwise a posting's grade would drift because other postings arrived, which is not something a
reader can be expected to understand.

**An unrated posting has no relevance and no grade**, and is always below the floor. Imputing a total
would quietly rank it against postings the model has actually read.

## How a fact reaches a score

`notes/about.md` holds one fact per bullet, in the first person. Facts are **not** a scoring system.
They are the only thing the model is told about the person, and they reach a score by changing what
the model reads before it rates a posting.

Write them as sentences. "I do not want to do release engineering" and "I have done release
engineering" mean opposite things, and any term-matching scheme collapses them into the same thing.
That is exactly what the previous design did, and why it is gone.

Facts have no weights. What an attribute is worth is a weight in `config.toml` under `[scoring]`,
which is the one place weights live.

## What invalidates a rating

A rating is a judgement of a **posting** against a **person**, made by a **model** under a **prompt**.
Change any of the four and the rating is stale:

| changes | recorded as | effect |
|---|---|---|
| the posting's text | `text_sha` | that posting is re-rated in full |
| the person's facts | `facts_sha` | every posting is re-rated in full |
| one attribute's wording | `DIM_REVS[dim]` | that one slot is re-rated, on every posting |
| the prompt as a whole | `prompt_sha` | recorded, not acted on; bump the revisions to act |

Per-dimension invalidation is the point: adding a new attribute costs one slot per posting, not
a re-read of the corpus.

Ratings written before `facts_sha` existed carry none, and are not re-read merely for lacking one.

## The audit trail

Every mutation to the scoring model appends a line to `data/audit.jsonl`, readable with
`jobhunt.py audit`. The pathways are not uniform and the log does not pretend they are:

- `facts_changed` — what the model will read changed. Nothing re-scores; ratings go stale.
- `judged` — the model read N postings, under this prompt, these facts, this model name.
- `weights_changed` — arithmetic. Every rating keeps its value and every total moves at once.
- `curve_refit` — no posting's relevance changed; what a given relevance is *called* changed.
- `scan` — what the run touched.

Detection is by comparison against the last line of the same kind, not by hooking every writer, so a
fact edited in a text editor outside the dashboard is still recorded on the next run.

## Which companies are even looked at

`jobhunt.py discover` decides what enters the scan, and it is a **gate decision**, so it obeys the
same rule as the others: a board is promoted only when `Matcher.gate` would keep at least one of its
postings. Never approximate that test — reimplementing it means a board can be promoted whose every
posting the scan then discards, or rejected while it was carrying exactly the role wanted.

Three properties that are easy to lose:

- **A board must prove whose it is.** Greenhouse returns `company_name`; check it against the guess.
  A token that resolves to somebody else is a miss, not a discovery. Platforms whose feed carries no
  name must be asked some other way: BambooHR's `/careers/company-info`, Rippling's board page title.
  Short tokens collide (`svt` on BambooHR is an Alaska health provider, `amp` on Rippling a Toronto
  company), so a name contained in another counts only when the shorter one has five letters or more.
- **A company with no feed gets a page watcher** (`ats = "page"`), never a guessed board, and a page
  that links to a real board means the board is used instead (`discover.board_from_careers_page`).
- **Nothing relevant is parked, not dead.** A live board with no matching work today costs one
  request to recheck later. Throwing the token away means finding it again from scratch.
- **The registry remembers what was tried.** Without that, every run re-guesses the same dead tokens,
  and the cost grows with the number of companies rather than the number of new ones.

Discovery adds companies; it never changes how a posting is scored. If a change here moves the golden
file, something is wrong.

## A metered source is never a per-scan source

Some sources charge, in money or in a finite allowance. Adzuna gives 1,000 requests a week on its
free tier; Jooble gives **500 for the life of the key**, which does not refill. A nightly scan that
spends 90 of those a run has destroyed the key inside a week and nobody notices until it returns
errors.

So: any source with a finite allowance gets a `[quota]` ceiling before it gets a key, the ceiling is
checked **before** the first request rather than discovered through 429s, and spending is recorded
before the request rather than after — a crash should cost the budget, not silently overspend it.

The ATS feeds are deliberately exempt. An employer publishing their own jobs is not a resource to
ration, and treating it like one would be the wrong kind of caution.

## Evidence, not taste

When you change a weight or a rule, check it against the only labels that exist: the postings the
person actually saved or applied to. `jobhunt.py retune` prints where those rank.

Say plainly when the evidence is circular. Picks made from a list the old weights sorted are not
independent evidence for the new weights, which is why a decision records the weights hash that
produced the list it was made from, and why `retune` counts how many of the picks are confounded.

## Only real profiles do any work

A profile folder named `example-*` is documentation. `load_all` skips it regardless of what its
`search.toml` says, so it can never fetch a board or spend a model call. Copying
`profiles/example-engineer/` to `profiles/<your-name>/` works exactly as the README says, because the
copy no longer carries the reserved prefix.
