# Where software jobs are posted in the US, and which of them we can read

An audit of the job-board landscape, written 23 September 2026. The question behind it is coverage:
*is anything posted anywhere that this tool cannot see?*

Every endpoint marked **verified** was called during the audit and answered. Everything else is
marked as unconfirmed, because a board that answered somebody else's blog post two years ago is not
evidence.

## The short answer

Most software jobs reach the public through one of two routes, and they overlap heavily:

1. **The employer's own applicant tracking system.** Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters, Workable and a dozen others each publish a per-employer JSON board with no key,
   no quota and no terms to accept. This is the best source there is: it is the employer speaking
   directly, it carries the full description, and it never expires.
2. **Aggregators**, which index route 1 and add the employers who post nowhere else. These are where
   the API keys, quotas and costs live.

Route 1 is free and unlimited. **The cheapest available improvement to coverage is supporting more
ATS platforms, not buying more aggregator access.**

---

## Free, no key, no quota

One board per employer. You have to know the employer's token, which is what `jobhunt.py discover`
is for.

| platform | endpoint | state |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true` | wired |
| Lever | `api.lever.co/v0/postings/{t}?mode=json` | wired |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{t}` | wired |
| Workday | `{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs` | wired · tenant+site sweep |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | **wired** · verified, 4,817 postings at Bosch alone |
| Workable | `apply.workable.com/api/v1/widget/accounts/{a}?details=true` | **wired** · endpoint verified, no live token found |
| Breezy | `{t}.breezy.hr/json` | **wired** · verified |
| Recruitee | `{t}.recruitee.com/api/offers` | **wired** · from documentation; every token tried 404'd |
| Personio | `{t}.jobs.personio.de/xml?language=en` | not built — XML rather than JSON, and some accounts use `.com`. Rate-limited the audit. |
| JazzHR, Teamtailor, iCIMS, Taleo, SuccessFactors | — | key or partner agreement required |

Note the trailing slash on Recruitee: `/api/offers` works, `/api/offers/` returns 404.

**SmartRecruiters is the best-structured source in this table.** Its `location` object carries
explicit `remote` and `hybrid` booleans, which map straight onto `remote_signal`, and
`company.name` means a board names itself — so a guessed token that resolves to the wrong employer
identifies itself rather than entering the scan under a false name. Greenhouse is the only other
source that does this.

**Workable has a trap.** An account that does not exist returns `200 {"jobs": []}` rather than 404,
so token guessing cannot tell "no such board" from "board with nothing on it" unless an empty board
is explicitly treated as a miss.

---

## Free, but keyed and quota-limited

| source | free quota | what it costs | state |
|---|---|---|---|
| USAJOBS | unlimited | free, email registration | coded · needs key |
| Adzuna | 25/min · 250/day · 1,000/week · 2,500/month | free, instant | coded · needs key |
| SerpApi (Google Jobs) | 250 searches/month | free | **coded** · needs key |
| Jooble | **500 requests, lifetime** | free, reviewed form | coded · **parked**, see below |
| National Labor Exchange | — | application reviewed by a governance board | not pursued |

Adzuna's terms permit "personal research", which is this use; ongoing commercial use needs a licence.

**A quota is not a rate limit.** Jooble's 500 is a lifetime allowance per key, which means it can
never be a per-scan source: at 90 requests a scan it would be permanently spent inside a week. It is
a one-shot discovery tool. Adzuna at 1,000 a week supports roughly four scans a week, not a nightly
one. Any source in this table needs a budget before it gets a key, not after — which is what `[quota]` in
`config.toml` and `jobhunt/fetch/quota.py` now do. Ceilings are checked before the first request
rather than discovered through 429s, and spending is recorded before the request so a crash costs the
budget rather than silently overspending it.

As configured today: Adzuna 36 requests a scan against a 200/day ceiling, SerpApi 6 a run against 8
a day, Jooble parked.

---

## No public API at any price

| board | why it matters | what reaches it |
|---|---|---|
| LinkedIn | the largest professional job index | Google Jobs, via SerpApi |
| Indeed | the largest US aggregate | Google Jobs, via SerpApi |
| ZipRecruiter | large US aggregate | Google Jobs, via SerpApi |
| Glassdoor | large US aggregate | Google Jobs, via SerpApi |
| **ClearanceJobs** | **the cleared/defence board** | **nothing** |
| Dice | tech-specific | nothing |
| Built In | tech hubs | nothing |

All seven close their APIs to the public. Third-party scrapers exist and are sold commercially; they
break the boards' terms and are not used here — see *Notes on etiquette* in the README, and the
finding that scraping LinkedIn risks the account a job search depends on.

**ClearanceJobs is the one real gap.** It is the board for security-cleared work, it is owned by DHI
Group alongside Dice, and neither has a public feed. The mitigation is structural rather than
technical: the defence primes that post there — Booz Allen, Leidos, SAIC, CACI, GDIT, Anduril, Shield
AI, Palantir — all run their own ATS, and `discover` reaches those directly. What is genuinely lost
is postings from small cleared contractors who advertise nowhere else.

---

## Paid, for the record

Not used. Recorded so the question does not have to be researched again.

| service | price | what it gives |
|---|---|---|
| SerpApi Starter | $25/month · 1,000 searches | Google Jobs at scan frequency |
| SerpApi Developer | $75/month · 5,000 searches | the same, comfortably |
| JobsPipe | $49/month | 30+ sources behind one API |
| ScrapingDog | $30+/month | scraping-as-a-service |
| Coresignal | ~$1,000+/month | bulk firmographic and jobs data |

---

## Why the keys cannot be registered automatically

Not a technical limit. Every registration requires an email confirmation, and accepting terms of
service is a legal act binding the account holder. Several require a phone number; the National
Labor Exchange requires an application reviewed by a governance board. And a credential should reach
`.env` directly rather than passing through a conversation transcript.

What *can* be automated is everything after that: the fetcher, the quota budget, the scheduling, and
the measurement of what each source actually adds.

## Re-checking this

Endpoints move and terms change. Re-run the verification in the table above before trusting a row
that is more than a few months old, and re-read the quota figures before relying on them — they are
the numbers most likely to have shifted.

---

## Small companies (added 28 September 2026)

The scan was finding mostly large companies, because every employer source needs a known board and
new employers were discovered only from names already seen in fetched postings. What changed:

- **BambooHR and Rippling** are fetched and probed: the commonest small-company ATSs with public,
  key-free feeds (`{co}.bamboohr.com/careers/list`, `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs`).
  Neither feed names its owner, so discovery asks separately (BambooHR `/careers/company-info`, the
  Rippling board page title): short tokens collide (`svt`, `amp` were other companies).
- **Careers-page watcher** (`ats = "page"`): for a company with no feed, the careers page is read once
  a scan and its job-title links become postings. Perrone Robotics is the first; its
  four roles come through with full descriptions. A careers page that links to a known board is used to
  find the board instead (`discover.board_from_careers_page`).
- **Adzuna** is retuned for small local employers: robotics / autonomy / perception / computer vision /
  ML / forward-deployed / applied-AI queries around a profile's own anchors at 75 miles, about 50
  requests a night against 1,000 a week. It stays dark until `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` are in
  `.env`; its employer names then feed `discover` automatically. USAJOBS likewise needs its key.
- **A one-time seed** of regional autonomy, robotics, defense-tech and AI companies (Built In's regional
  robotics, defense-startup and AI lists, the yc-oss YC directory) went into
  `tools/board_candidates.toml` and through `discover`: Torc Robotics, Shift5 and Liquid Robotics
  promoted; Auterion, Raft, Tactiq and Rebellion Defense parked (live boards, nothing matching today);
  ten have no findable board.

Still unreachable, by terms or login: Indeed, LinkedIn, Glassdoor, Wellfound, ClearanceJobs, Handshake.
