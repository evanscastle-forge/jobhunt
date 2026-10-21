# Apply rehearsals: what real forms taught us

A running log of what rehearsals against real postings found, and what was changed because of it.
Rehearsal fills and verifies a real form, then closes it; nothing is ever submitted. Add to this file
whenever a rehearsal finds something, so the next person fixing the filler starts from evidence.

## Where the evidence is

Everything is local and gitignored (it holds your contact details):

| file | what it holds |
|---|---|
| `profiles/<name>/data/runs/rehearsal-<stamp>.md` | the batch report: per posting, the result, what blocked it, every issue, errors with tracebacks |
| `runs/<run>/fields.json` | **the main debugging file**: every field read, its label, kind, options, control attributes, and what was decided (`filled`, `asked`, `flagged`, `skipped`, `hidden`, `fill_error`, `did_not_stick`) with the rule that decided it |
| `runs/<run>/log.txt` | the process output, including Playwright call logs and tracebacks |
| `runs/<run>/page_events.jsonl` | what the page complained about: console errors, uncaught exceptions, failed requests |
| `runs/<run>/issues.jsonl` | the questions and blockers the run raised |
| `runs/<run>/run.json` | state, reason, report, `error_trace` on a crash |
| `runs/<run>/NN-*.png` | screenshots: `opened`, `filled`, `not-a-form`, `error` |

To find why a field went wrong: open the batch report, find the posting, open its `fields.json`, search
for the label. `source` names the rule; `control` says which element was touched.

`jobhunt.py rehearse [job ids] [--limit N]` runs a batch; `jobhunt.py apply-stats` summarises every run.

## 26 September 2026: first rehearsals (5 postings, then 12)

Before the fixes, 0 of 5 forms were free of mechanical problems; after them, 1 of 12 had any
mechanical problem, and everything else blocking a run was a question only the applicant can answer.

Fixed:

| found on | what happened | fix |
|---|---|---|
| Employer A | the posting redirected to the careers home page; its job-alert signup (with a resume upload) was filled as the application | `off_the_form`: a page that left its ATS domain, without an embedded ATS frame, is not the form |
| Greenhouse, all | invisible reCAPTCHA counted as a captcha to solve, so no run could ever be ready | `captcha_state` separates a visible challenge (blocks) from the invisible scorer (a non-blocking note) |
| Greenhouse | phone fields format as you type, so `(555) 010-0100` failed verify | digit comparison for number-like values |
| Greenhouse | a control with no id or name was found by position; Playwright counts differently and typed into a search box | every control is tagged `data-jh` when read and found by that tag |
| Greenhouse | the hidden search box inside the phone-country picker was answered as "Phone" | a control Playwright does not see as visible is skipped (`hidden` in fields.json) |
| Greenhouse | react-select's hidden `requiredInput` showed up as an unlabeled required field | controls that are `aria-hidden` with negative tabindex are not fields |
| Lever | questions were labelled `✱`, the required-marker span | a label candidate must contain a letter or digit |
| Lever | the resume input is replaced by a chip after upload, so verify said "field is gone" | the file name on the page counts as the read-back |
| Lever | location picker only searches on keystrokes and discards unpicked text; "Columbus" first matched Columbus, GA | type the city, choose the suggestion matching the whole value; the first match alone is never taken |
| Employer B | "What are your preferred pronouns?" answered as how-did-you-hear ("p**referred**" contains "referred") | word boundaries on `referral`; pronouns are demographic |
| Employer B | a travel question mentioning "location" got the location answer | the location fallback is for short text fields only, never when the question is about travel |
| Employer C | "Preferred First Name" got the first name | preferred name / nickname / pronunciation are skipped, checked before first name |
| Employer C | "No - I am interested in obtaining one." chosen for a bare "No": it adds a claim | an option that says more than three words beyond the answer is only a weak match (a proposal) |
| Employer C | the clearance sentence was typed into a "select the clearance you hold" field | the sentence is only for describe/explain fields |
| Employer D | "how you heard about this opportunity" missed the how-heard rule | broader phrasing |
| Employer D | consent to AI notetakers in interviews treated as an AI-use attestation | notetaker/transcription questions are ordinary questions |

Open:

- **Employer E (Ashby)**: `Location` is a `role=combobox` autocomplete and `_set_combobox` found no matching suggestion for "Columbus, OH". Look at `fields.json` in that run and
  the widget's DOM; the picker probably needs the keystroke path or a different option selector.
- **Profile gaps, not bugs**: LinkedIn and website are empty in `profile.toml` (asked on 8 of 11 forms);
  salary is blank; how-heard "Company careers page" rarely matches an option exactly.
- 0 of 11 were ready, all for unanswered questions. The bank now holds them as pending entries on the
  Answers page; answering them is what moves the single-click rate.

## 26 September 2026, evening: stated facts, employer autofill, the resume

- **The resume was never uploaded on any Greenhouse form**, and nothing said so. Greenhouse labels the
  file input with its button ("Attach file"), so the resume rule never matched and the field was
  silently skipped as an unrecognised optional upload. Fixed two ways: `read.file_label` names an upload
  from its id/name (`resume`, `cover_letter`) when the label is button text, and a form with any upload
  field that ends without a resume is now a blocking issue. A miss like this must never be silent.
- **Employer autofill first** (the rule): the resume is uploaded before anything else, and what the
  page's parser fills is reviewed: kept when it matches a certain answer, corrected when not, proposed
  when it cannot be checked. Lever's parser filled 5 fields on Employer D; Greenhouse boards offer only
  login-based autofill (MyGreenhouse), which is never used.
- Greenhouse forms take 1.5 to 3.5 minutes each in rehearsal. Log lines now carry `[mm:ss]` elapsed time
  so the slow step is visible in `log.txt`.
- Stated facts became rules rather than bank answers where the answer depends on the posting:
  office attendance (Yes only for home-metro postings, `onsite_only_in`), prior employment
  (`past_employers`), and self-identification (`eeo = "answer"`, `[answers.eeo_self]`).
- Greenhouse's "Location (City)" (react-select over a places search) and Employer E's Ashby location both
  search on keystrokes and spell the state out ("Columbus, Ohio, United States"). The combobox adapter
  now types, and `best_suggestion` expands "OH" to "Ohio"; it still never takes the first "Columbus"
  on its own.
- The "ready" metric counted non-blocking notes (the invisible captcha) and optional questions as issues,
  so it could never move. It now counts only what blocks.

End of day, 12 saved postings: **2 of 11 ready for one click** (Employer F, Employer G),
**no mechanical failures**; every other blocker is a required question waiting in the answer bank. The
applicant agent proposed 16 bank answers and declined 31, each decline naming the fact it needs.

## 26 September 2026, late: the resume as the main record

- Resume hyperlinks are not in the PDF's page text; `persona.resume_links` now reads the link
  annotations with the resume line each is attached to (a project line links to its public
  announcement), so the agent can see them.
- The agent was declining questions the resume settles by simple deduction ("years with Python" when
  Python is the core tool in every ML role on the resume). Its prompt now names the resume as the main record
  and spells out the deductions it should make. It still cites every line.
- A draft with one unsupported claim was thrown away whole. The checker now proposes a revision without
  the claim, which is checked again from scratch; only if that fails is it a decline. (Seen: a draft
  that read an impact claim out of a URL slug.)
- Re-asked, 6 of 31 earlier declines became proposals. What is left are facts only the applicant has, and
  follow-up fields ("If yes, please explain", "Please specify") that the bank stores without the
  question they follow, which no one can answer out of context: record the parent question with them.

## 26 September 2026, night: the audit (goal: 90% of forms filled 90% with no help)

Baseline on a fixed 40-posting sample of B+ postings (Greenhouse 17, Ashby 10, aggregators 7, Workday 5,
Lever 1; `profiles/<name>/data/apply_sample.json`): 20 of 26 reachable forms at >= 90%, median 93%, but only 26
of 40 were reachable at all.

Found and fixed during the audit:

- A killed rehearsal leaves Chrome running on its profile slot; the next run on that slot hung for good.
  `browser._clear_stale` kills orphans on a slot it holds the lock for, and `rehearse --workers` runs each
  posting in its own process group with a timeout.
- "Have you ever worked for a government agency?" got the past-employer answer. That rule now fires only
  for questions about this company ("us", its name, "history with").
- "Have you signed a non-compete with your current employer?" got "Acme": the current-company rule
  now ignores yes/no questions.
- The Muse's Apply is a `<button>` that opens the employer's site from script; RemoteOK links through its
  own redirect. Both handled in `resolve.py` (RemoteOK's redirect still returns to the listing without a
  real click: 2 of 231 postings, left alone). Two Muse listings led to Workday.
- A custom careers form labelled every input "Step 1 / 3": a label shared by several controls is a
  section heading, and each control's placeholder is used instead.
- The navigator believed a hidden form was reachable: `looks_like_application` counts visible fields only.
- Workday's honeypot (`beecatcher`) is never filled; its dropdowns are `<button aria-haspopup=listbox>`
  and are now read and set like comboboxes.
- A multi-page single-page-app keeps every step in the DOM; "did the page advance" compares visible
  fields only.
- Employer B's "A, B or C?" export question defines A/B/C in text beside it: fields now carry that help text to
  the agent.

What is left is mostly facts only the applicant has (GPA, family at the company, consent to
an employer's AI policy), which the prepare digest lists once each.

## Resume variants (20 October 2026)

From this date a run uploads a per-posting variant of the resume (`docs/apply-design.md`, "The resume:
one variant per posting") rather than the base PDF. Whether a variant parses as well as the base is an
empirical question: the employer-autofill report (`report.autofill.kept` / `corrected`) on the first
real runs is the evidence, and `fields.json` now names the file uploaded (`uploaded <name>`).
