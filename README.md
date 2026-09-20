# Job Finder

Finds jobs worth applying to, screens them against a profile, writes a tailored
cover letter for the good ones, and sends the ones you approve.

Nothing is sent without your explicit approval.

## How it works

```
sources  ->  filters  ->  scoring  ->  drafting  ->  YOU APPROVE  ->  delivery
  7 APIs     regex       Claude       Claude         web UI          email (SMTP)
  ~3400      ~50 left    0-100        letter                         ATS (browser)
                                                                     manual
```

Each stage is cheaper than the one after it, on purpose. The regex filters throw
away about 98% of what the sources return, so the LLM only ever reads postings
that are plausibly worth your time.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # for ATS form filling

cp .env.example .env                              # keys and safety caps
cp profile/profile.example.yaml profile/profile.yaml
```

Then edit `profile/profile.yaml`: who you are, what counts as a good job, and
how letters should read. It is gitignored, because it holds your phone number,
tax id and day rate. Everything else in the pipeline reads from it.

The only value you really need is `ANTHROPIC_API_KEY`. Without it the pipeline
still runs, but scoring drops to a keyword heuristic that cannot judge fit, and
no cover letters get written.

To send email applications, add a Gmail app password as `SMTP_PASSWORD`
(https://myaccount.google.com/apppasswords - your normal password will not work).

## Daily use

```bash
alias jf='cd /home/edgar/sweden/job_finder && PYTHONPATH=src .venv/bin/python -m jobfinder.cli'

jf fetch      # pull every configured source, dedupe, apply hard filters
jf score      # screen survivors against profile/profile.yaml
jf draft      # write cover letters for anything scoring 65+
jf ui         # the ops view at http://127.0.0.1:8420 (run stages, watch, approve)
jf ats <id>   # fill a Greenhouse/Lever/Ashby form in a browser and screenshot it
jf send       # send everything you approved (add --dry-run to see it first)
jf status     # counts
jf list       # the queue, as text
```

`./run_daily.sh` chains fetch, score and draft. Put it in cron; it never sends.

## The ops view

```bash
jf ui          # http://127.0.0.1:8420
```

One page, three columns, everything live over a websocket:

- **Left rail** - the funnel with live counts (seen, filtered out, scored,
  drafts, approved, sent), buttons to run each stage, per-source posting
  counts, why things were filtered out, and the state of every safety gate.
- **Middle** - the queue. Each card shows the score, what the screen liked
  (green) and flagged (red), the full reasoning, the cover letter, the model's
  own critique of that letter, and the buttons: Approve, Reject, Write letter,
  Fill form & screenshot, Send now.
- **Right** - the live log. Every source as it returns, every job as it is
  scored with its verdict, every letter as it is written.

Clicking **Fill form & screenshot** drives a real browser through the ATS form
and embeds the resulting screenshot in the card, along with any question it
refused to answer. It never submits. The screenshot survives a reload.

The stage buttons disable while something is running, and only one stage runs
at a time, because they share the database.

You can still drive everything from the CLI; the UI calls the same code.

## The files that matter

| File | What it decides |
|---|---|
| `profile/profile.yaml` | Who you are, what counts as a good job, how letters are written. Edit this first. |
| `profile/companies.yaml` | Company ATS boards to watch. Add companies you would actually work for. |
| `src/jobfinder/filters.py` | The free rejections. Change these to widen or narrow the funnel. |
| `src/jobfinder/scoring.py` | The screening prompt. |
| `src/jobfinder/drafting.py` | The cover-letter prompt and its rules. |
| `src/jobfinder/delivery/ats_forms.py` | How a stranger's form maps onto your profile. |
| `src/jobfinder/delivery/ats_browser.py` | The browser driver. Submit is gated here. |
| `src/jobfinder/ui/server.py` | The ops view: snapshot, websocket, actions. |
| `src/jobfinder/ui/static/` | The page itself. Plain HTML/CSS/JS, no build step. |
| `.env` | Keys, caps, and the approval switch. |

## Sources

| Source | Key | Notes |
|---|---|---|
| Greenhouse / Lever / Ashby | no | ~60 company boards from `companies.yaml`. The best source by far. |
| JobTech (Arbetsformedlingen) | no | Official Swedish government API. |
| RemoteOK | no | Remote-only. Takes a SINGLE `tag`; plural `tags=` returns an empty feed. |
| Remotive | no | Remote-only. States which countries a role can hire from. |
| Jobicy | no | Remote-only, reports seniority. |
| Himalayas | no | Remote-only. |
| Arbeitnow | no | EU, Germany-heavy. Mostly German-language, so most of it is filtered out. |
| JSearch (RapidAPI) | yes | The only legitimate route into LinkedIn and Indeed. **200 requests/month** on the free plan, so its queries are chosen, not sprayed, and it stops at 20 remaining. |
| Adzuna | yes | EU. No Sweden endpoint, so it runs gb/fr/de/nl/es. |

Neither LinkedIn nor Indeed has a public jobs API, and scraping them is a ToS
violation that costs the account you actually need. JSearch is the wrapper.

### Resolving an aggregator listing to the real form

Aggregators link to their own page, not the employer's form, and their outbound
links often redirect in a loop. But most of those employers still run hiring on
an ATS, so the form is reachable if the board can be guessed:

```bash
jf reroute     # re-check every "manual" listing for a real form behind it
```

It slugifies the company name, probes the three platforms, then matches job
titles so a board full of unrelated roles cannot produce a wrong link. On a real
run this upgraded **37 of 86** manual listings into forms the browser can fill.

The case it was written for: RemoteOK listed an AI agent role at Sticker Mule
with a dead redirect, while `jobs.ashbyhq.com/stickermule` was live the whole
time.

### Company boards

When a company runs its hiring on Greenhouse, Lever or Ashby, its whole job
list is readable at a fixed URL with no key. That beats an aggregator: you pick
the companies, listings are current and complete, and the apply path is a real
form `jf ats` can fill.

```bash
jf add-company legora                                  # by name
jf add-company https://jobs.ashbyhq.com/lovable/abc    # or any posting URL
```

It detects the platform, checks the board is live, and writes it into
`profile/companies.yaml`. Adding boards is still the highest-leverage change
you can make to the funnel.

## Safety rails

These are enforced in code, not in a prompt, so no model can talk its way past them:

- `REQUIRE_APPROVAL=true` - `send_application` raises unless the status is literally `approved`.
- `MAX_SENDS_PER_DAY` - counted from the database, checked before every send.
- Only `channel == "email"` applications actually send. ATS and manual ones are
  prepared and parked for you to submit.
- The letter writer is told never to state anything not in `profile.yaml`, and
  em dashes are stripped mechanically afterwards rather than trusted to the model.

## ATS browser automation

Greenhouse, Lever and Ashby forms are filled by a real browser (Playwright,
Chromium). All three are supported and tested against live postings.

```bash
jf ats 0147a37b            # fill the form, screenshot it, do NOT submit
jf ats 0147a37b --show     # same, with a visible browser so you can watch
jf ats 0147a37b --submit   # actually submit (needs an approved application)
```

How a form gets filled:

1. Every control on the page is tagged and read into a schema: label, type,
   required, and the real options behind each dropdown (each combobox is opened
   to harvest them, because a model guessing at option strings fills nothing).
2. Identity fields (name, email, phone, location, links, CV) are filled from
   `profile.yaml` by rule. These are facts and are never left to a model.
3. Everything else - the company's own questions - goes to Claude with the
   profile, and it may answer `null`. A null on a required field aborts the run.
4. The page is re-read and the answers re-keyed by field identity before
   filling, because the LLM call takes long enough for a React form to
   re-render and drop the tags.
5. After filling, the DOM is read back. A field that reports as filled but is
   actually empty counts as a failure, whatever the fill call returned.
6. A screenshot is taken. Only then can a submit happen.

Rules that are enforced in code, not in the prompt:

- **`ATS_SUBMIT=false` by default.** Approving an application queues it. It does
  not authorise a robot to click Submit on a company's website. That is a
  separate switch you turn on yourself.
- **Demographic questions are never answered.** Gender, race, veteran status and
  disability are skipped, or set to the decline-to-answer option when required.
- **Conditional questions are not auto-filled.** "Have you worked with us
  before? If yes, give the email you signed up with" gets `No`, never an email.
  Filling that field asserts a relationship that does not exist. An identity
  rule matching `/e-?mail/` did exactly this during development, which is why
  identity rules now only fire on short form labels, never on prose questions.
- **Application instructions in a posting ARE followed.** Employers often ask
  applicants to mention a keyword or include a reference tag to prove they read
  the post. The letter complies, on its own plain line, and says so in the
  notes. What it will not do is let a posting make it state something untrue,
  change the rate or availability, or override the rules above.
- **Nothing is invented.** The letter writer and the form filler both run
  against `profile.yaml` with instructions to return nothing rather than guess.
  Answers it gave honestly in testing: "have you been employed by Spotify?" ->
  No; "are you open to relocating to London or Stockholm?" -> No; "years of
  Android experience" -> 0-4.

## What it deliberately does not do

It does not automate LinkedIn Easy Apply or Indeed. Both ban it, both detect it,
and losing those accounts costs more than the applications are worth.

## Tests

```bash
PYTHONPATH=src .venv/bin/python tests/test_filters.py     # 31 cases
PYTHONPATH=src .venv/bin/python tests/test_ats_forms.py   # 16 cases
```

Both cover the places where a silent bug does real damage: a filter that drops
a job you wanted, or a form answer that tells a company something untrue.

Bugs these caught in practice: an `/e-?mail/` rule pasting Edgar's address into
"if yes, give the email you signed up with"; `\butvecklare\b` never matching
inside the Swedish compound "Fullstackutvecklare"; RemoteOK returning an empty
feed for two months because of a plural query parameter.

## Layout

```
profile/profile.yaml        you: skills, preferences, writing rules   (gitignored)
profile/companies.yaml      ~60 company job boards to watch
src/jobfinder/
  sources/                  one adapter per job source
  filters.py                the free rejections, before anything costs a token
  scoring.py                LLM screening against the profile
  drafting.py               cover letters
  ats_resolver.py           aggregator listing -> real application form
  delivery/                 email, and the Playwright ATS driver
  ui/                       the ops view
tests/                      50 cases over the two riskiest layers
```

## History

This started as an n8n workflow that emailed a daily job digest. It is archived
in `legacy/`. The filter logic in `filters.py` is ported from its code node.
