# Email Organiser

A personal AI email assistant. It reads my three mailboxes (Yahoo, iCloud, Gmail), has Claude label every new email, and puts a short summary in my inbox at 08:00 and 18:00: job emails first, then what needs a reply, then what's worth reading. The rest is just counted. For emails that need a reply, Claude writes a draft and leaves it in that account's Drafts folder, threaded under the original, with [gaps] for anything only I know. Spam, promos and newsletters move out of the inbox into an `Assistant-Review` folder. Job-alert emails are screened first: the jobs in them are rated against my profile, and the ones worth applying to head the summary. Every 15 minutes a quick check pings my phone (silently at night) when a job or needs-reply email arrives, with the reply draft already written. It also learns from what I do: an email I drag back out of Review means that sender is never moved again, and once a week Claude re-labels my hand-labelled test set so the accuracy is measured, not assumed. To unsubscribe, I drag an email into an `Unsubscribe` folder (on the Mac or my phone); the next quick check does the standard one-click unsubscribe and moves the email to Review.

**It cannot send or delete email.** There is no sending code anywhere in the project. Mailboxes are opened read-only. The only things it ever writes are the summary (placed in my own inbox) and reply drafts (placed in Drafts), and the only thing it changes is which folder junk sits in. I read, edit and send each draft myself, or delete it. A test fails if any sending code (`smtplib`, SMTP) ever appears.

```
Mail summary Tue 08:00: 1 job, 1 to reply, 3 to read

JOB · 1
  Acme Ltd has viewed your application for Data Analyst          [needs reply]
  Jobsdb · yahoo · 07:41
  Employer viewed the application and asked for availability.

WORTH READING · 3
  Your parcel is out for delivery
  Courier Co · icloud · 06:58
  Google · 2 emails · gmail · latest 07:12
  Security alert / 2-Step Verification turned on

Everything else: 14 fyi · 6 newsletter · 2 promo
```
*(The example is made up. Real summaries are HTML with colour-coded sections.)*

## Results

Measured against **50 of my own emails that I labelled by hand**, labelling blind so I never saw Claude's answer first:

| What | Result |
|---|---|
| Type agrees with mine | **44 / 50 (88%)** |
| Job emails caught (recall) | **6 / 6**, no false alarms |
| "Needs a reply" caught | **2 / 2**, 50 / 50 agreement |
| Important emails pushed into a low-priority label | **0** |

Every disagreement is on the blurry line between "worth reading", "fyi", "newsletter" and "promo". 50 emails is a small test, so treat 88% as roughly 76–96%. The test set keeps growing as the project continues.

## How it works

```
 Yahoo ─┐
 iCloud ├─ IMAP, read-only ─▶ fetch ─▶ SQLite ─▶ classify ─▶ digest ─▶ IMAP APPEND
 Gmail ─┘                              (seen?)   (claude -p,   (group,    into my
                                                  no tools)     format)    inbox
                                                     │
                                                  drafts ─▶ IMAP APPEND into each
                                               (claude -p,  account's Drafts folder
                                                no tools)
                                                     │
                                                  cleanup ─▶ spam / promo / newsletter
                                                             moved to Assistant-Review
                     launchd runs the whole chain at 08:00 and 18:00
```

| File | Job |
|---|---|
| `fetch.py` | Downloads the last day of mail without marking it read; remembers what it has seen |
| `classify.py` | Sends emails to Claude in batches of 20; gets back a type, a needs-reply flag and a reason |
| `drafts.py` | Writes reply drafts for emails that need one and places them in each account's Drafts folder |
| `screener.py` | Reads job-alert emails, has Claude list and rate every job against my profile, keeps the high-fit ones (links checked, duplicates and jobs I've applied to removed) |
| `cleanup.py` | Moves recent spam, promos and newsletters to `Assistant-Review` (never job mail, anything needing a reply, or job-board alerts) |
| `digest.py` | Builds the summary (HTML + plain text) and places it in the inbox |
| `agenda.py` | Reads macOS Calendar (read-only) for the 08:00 summary: today's events, plus anything in the next 7 days that needs preparing. Claude judges each event once (true/false + a prep note), so most mornings make no Claude call |
| `reminders.py`, `weather.py` | The rest of the morning brief: Apple Reminders due today or overdue (read-only), and the Hong Kong Observatory's forecast and plan-changing warning signals (open data API, only while the Mac is set to Hong Kong time). No Claude calls |
| `shorten.py` | Short labels for long titles in the brief ("Flight JL026 to Tokyo" instead of a booking string). One remembered Claude call per new title; a label may not contain any number that isn't in the original, and an event's label never repeats the time |
| `watch.py` | The 15-minute check: new job or needs-reply mail → draft → instant counts-only ping. No new mail = no Claude call |
| `runlock.py` | Makes sure the quick check and the summary never run at the same time |
| `slots.py` | Records whether each 08:00 / 18:00 summary was delivered, so the quick check can run a missed one (catch-up) |
| `gate.py`, `grow_testset.py` | The keep-or-reject check for instruction changes; picking old job-season emails for me to label |
| `learn.py` | Notices my corrections (rescued from Review, moved to junk, replied when Claude said no reply), turns them into sender rules, and runs the weekly accuracy check |
| `unsub_folder.py` | Each quick check: emails I dragged into an `Unsubscribe` folder get the RFC 8058 one-click unsubscribe (or a reason to do it by hand), then move to Review |
| `unsubscribe.py` | The safety checks for one-click unsubscribe (only when the sender's DKIM signature vouches for the link), plus a Terminal fallback that lists junk senders to tick |
| `label_by_hand.py`, `evaluate.py` | The blind hand-labelling tool and the accuracy report |
| `store.py`, `paths.py` | The SQLite database, kept in `~/.email-organiser/`, outside the code |

## Design rules

1. **Dry run by default.** Every command shows what it *would* do. `--live` is needed to change anything.
2. **Read first, write last.** Milestones climb a risk ladder: read → label → summarise → drafts → moving mail.
3. **No sending code exists.** "It sent something by mistake" is impossible, not just unlikely.
4. **Nothing is ever deleted.** Junk moves to an `Assistant-Review` folder that I empty myself; dragging an email back undoes a move. At most 30 moves per run, so a bug can't empty an inbox.
5. **The model labels; the code acts.** Claude only returns a label. Plain Python decides what happens. Claude runs with every tool and connector switched off, so a malicious email can at worst earn itself a wrong label (prompt injection has nothing to grab).
6. **Secrets never sit in files.** App passwords live in the macOS Keychain. Personal data lives outside the repo.
7. **Send the model as little as possible.** Sender, subject and the first 1,500 characters of the body.
8. **Drafts never decide for me.** A draft never accepts, declines or promises anything; those become [gaps] I fill. Plain text only, no links. Junk (spam, promo, newsletter) never gets a draft, whatever the model says.
9. **Unsubscribing needs my say-so (dragging the email into `Unsubscribe`, or a typed yes), and only uses the vouched-for link.** One-click (an HTTPS POST of `List-Unsubscribe=One-Click`) only when my mail provider verified a DKIM signature covering both unsubscribe headers. No cookies, no login, redirects not followed. Spam is never unsubscribed from: it tells a spammer the address is live. Nor is any address that has ever sent me job or needs-reply mail: one wrong unsubscribe there could silently stop an interview email.
10. **Learning changes what the system knows, never what it may do.** Corrections become sender rules and test cases; the learning code only opens folders read-only. Asymmetric thresholds: one rescue protects a sender for good (being wrong leaves junk in the inbox); labelling a sender junk without Claude takes three corrections (being wrong hides real mail). A change is kept only if the weekly score doesn't drop, and the job score never.

## Things I found along the way

- **A privacy leak in my own setup.** By default, `claude -p` loaded my personal instruction files (including financial notes) into every labelling call. I caught it by asking the model what it could see. Fixed with `--setting-sources ""`, which also cut input from about 5,500 to about 1,000 tokens per call.
- **A bad label rule, not a bad model.** My first "job" label caught "application submitted" confirmations, which would have fired the alert ~25 times a week. I narrowed it to *an employer did something*.
- **A design fix from the data.** One email was both "job" and "needs a reply", and a single label forced a false choice. "Needs reply" became a separate yes/no flag.
- **Duplicates beat losses.** Emails are only marked as summarised *after* the summary is delivered. A failed delivery means a repeat next time, never a silently lost email.
- **A failure that looked like a quiet morning.** One day Claude Code was logged out and every call failed, using 0 tokens. The summary only showed labelled emails, so it reported "nothing needs you" while hiding 5 emails. Now unlabelled mail leads the summary under a red "Not labelled" heading, the ping says so loudly, and a one-word health check before each summary pings me to run `/login`. The model is also pinned: it had silently switched with the account default.
- IMAP's `SINCE` search only understands dates, not times, so the code over-fetches and trims. Yahoo also rejects `HEADER` searches.
- **Every provider is different.** The Drafts folder is `Draft` on Yahoo, `Drafts` on iCloud (found by name, because iCloud doesn't tag it) and `[Gmail]/Drafts` on Gmail. Yahoo returns an empty answer when asked for just two header fields (`HEADER.FIELDS`), so the first live test addressed a draft to the sender instead of the Reply-To address. The fix fetches the whole header. Yahoo also drops the `\Draft` flag on saved drafts; Mail.app still treats them as drafts because of the folder.
- **iCloud can't MOVE.** Yahoo and Gmail support the IMAP `MOVE` command; iCloud doesn't. There, a move is copy → flag → `UID EXPUNGE` of exactly those emails. A plain `EXPUNGE` would also wipe anything else flagged as deleted, so the code refuses to run on a server without `UID EXPUNGE`.
- **Job alerts are mostly links.** A Jobsdb alert is ~14,700 characters, mostly tracking URLs. The screener swaps every link for a tag like `[L3]` before Claude reads it; Claude answers with tags and the code puts the real link back, only if it points at the site that sent the email. Claude can't invent or redirect a link.
- **One sender address, two kinds of mail.** Jobsdb sends my application updates *and* its recommendations from the same address; Workday sends application status with an unsubscribe link too. So unsubscribe candidates are grouped by name *and* address, and any sender that ever sent job or needs-reply mail is never offered.
- **A sender could fake my provider's verdict.** The code trusted a "DKIM passed" stamp from any of Yahoo, Google or iCloud. Each provider deletes fakes in its *own* name (RFC 8601 s5), but not in another provider's, so a fake "Yahoo says passed" in mail to Gmail counted. Now the provider that wrote the *first* stamp is the receiver (RFC 8601: it must be added at the top), and only its stamps count. Checked against 74 real one-click emails: none changed. A first attempt ("only the unbroken top block") broke every iCloud email, because iCloud puts other lines between its six stamps.
- **Google doesn't follow the header format.** The standard wraps unsubscribe links in `<...>`; Google sends them bare. The first real dry run listed Google Health as "no unsubscribe header" until the parser accepted both.
- **A sleeping Mac skips checks.** `launchd`'s `StartInterval` drops a check that falls due while the Mac is asleep (it says so in `man launchd.plist`), unlike the calendar schedule, which runs on wake. The quick check looks back 3 hours, and the summary 26, so nothing is lost.
- **"Runs on wake" isn't enough on battery.** A sleeping MacBook wakes for ~20 seconds at a time with no network (a "dark wake"). launchd fired the missed 08:00 summary in one, the Mac slept mid-run, and the run died on a DNS error. launchd never retries, so that morning had no summary and no ping. Now every summary run is recorded, and the 15-minute check runs a summary that is 10+ minutes overdue. Network errors are retried at every check; 3 real failures send one ping and stop until the next summary.
- **Retries can burn quota.** An email that keeps failing would be retried by a 15-minute job 96 times a day. The quick check only labels the last 3 hours and only drafts what it's alerting about; the twice-daily summary retries the rest.
- **Calendar's date search misses repeating events.** Asking Calendar.app for one day's events only returns a repeating event on its first date, so a weekly class would vanish after week one. The summary reads every event with its repeat rule and works out the dates itself (~90 s, fine once a day). Single changed dates needed a real test event: a cancelled date is listed in the event's `excluded dates`; a moved date is an extra copy with the same ID at its new time, visible only when all events are read in one go (asking one by one returns the series' details for every copy). A failed read is printed in the summary, so a blank calendar never looks like a free day.
- **The first real correction found itself.** On its first look at my Sent folders, the learning step found I'd replied to a game-prize email Claude had marked "no reply needed".
- **Measured, not assumed.** The automated weekly check re-labelled my 50 hand-labelled emails and got 44/50 (88%), the same as the hand-scored test two days earlier. Job emails 6/6.
- **Six job emails proved nothing.** "Job 6/6" sounds perfect, but its honest 95% range was 54–100%. I hand-labelled 37 more from my job season, picked by plain rules plus a random sample (never by Claude, which would only pick the job emails it already recognises). Job recall became 16/19, and two real misses surfaced: a live application update and a recruiter thread, both filed as routine.
- **Scores wobble, so the gate judges per email.** The same 50 emails scored 44/50 one night and 43/50 an hour later. Re-running only the job emails gave the same answer three times, so the wobble comes from which other emails share a batch. A total-based "keep if the score doesn't drop" rule would have rejected a fix that caught 19/19 job emails in every run. `gate.py` now runs old and new instructions 3 times each and keeps a change only if no job email is lost in every run and at least one is gained in every run.
- **Sender lists are brittle.** LinkedIn sends job alerts from three different addresses; the dry run caught one I'd missed (a "… are hiring" email) before anything moved.
- **The only real reply draft answered a no-reply address.** After five days live, the one draft written for real mail replied to "WorkdaySystemDoNotReply", an assessment invitation that needed an action, not a reply. The address itself looked ordinary (the company's name at the Workday domain); only the display name said so. Drafts now skip any sender or Reply-To whose name or address says no-reply.

## Setup

Requirements: macOS, [uv](https://docs.astral.sh/uv/), and [Claude Code](https://claude.com/claude-code) logged in. I run it on my personal Claude subscription. If you use this, use your own Claude login or an API key; see Anthropic's terms for what each plan covers.

```bash
# 1. Accounts: names, servers, addresses (no passwords)
mkdir -p ~/.email-organiser
cp accounts.example.toml ~/.email-organiser/accounts.toml   # then edit it

# 2. One app-specific password per account, stored in the Keychain (hidden prompt)
uv run python -m email_organiser.store_password yahoo

# 3. Check everything connects
uv run python -m email_organiser.check

# 4. Try a summary without delivering it
uv run python -m email_organiser.digest            # dry run
uv run python -m email_organiser.digest --sample   # preview the HTML in a browser
uv run python -m email_organiser.drafts --sample   # preview reply drafts for older emails

# 5. Schedule it (08:00 and 18:00) on a stable copy of the code
scripts/install-schedule.sh

# 6. Optional: a Telegram ping after each summary. Create a bot with @BotFather,
#    send it "hi", then (token goes to the Keychain via a hidden prompt):
uv run python -m email_organiser.notify setup
```

The Telegram ping's mail line carries counts only ("1 job, 2 to read"), never senders or
subjects, because bot chats are not end-to-end encrypted. It is sent every run, even with no
new mail, so a missing ping means the schedule broke. If Telegram fails, the summary is unaffected.
At 08:00 the ping also carries the day's brief, one section each for the calendar, to-dos,
what's coming up, the weather and mail, with short labels for long titles. Those are names and
dates I chose to allow; email text never goes to Telegram. The summary email follows Mail's dark mode.

Unsubscribing: drag the email into the `Unsubscribe` folder of any account (Mail.app or iPhone).
Within 15 minutes a ping gives the counts, and the next summary lists what happened. Fallback:
`scripts/unsubscribe.sh` shows a list of junk senders without changing anything;
`scripts/unsubscribe.sh --live` asks which to unsubscribe from, then for a typed "yes".

Tests run offline, with no mail server or Claude needed: `uv run pytest`.

## Status

| # | Milestone | |
|---|---|---|
| 0 | Connect to all mailboxes securely | ✓ |
| 1 | Read new mail, read-only, without repeats | ✓ |
| 2 | Claude labels mail; blind 50-email test set | ✓ |
| 3 | Twice-daily summary, scheduled | ✓ |
| 3.5 | Telegram ping after each summary (counts only) | ✓ |
| 4 | Reply drafts in the Drafts folder (never sent) | ✓ |
| 5a | Junk to a review folder (job alerts kept) | ✓ |
| 5c | Job-alert screener: jobs worth applying to, in the summary | ✓ |
| 5b | Unsubscribe by dragging an email into a folder | ✓ |
| 6 | Instant alert for recruiter and interview mail (every 15 min) | ✓ |
| 7 | Learns from my corrections, re-measured weekly (prompt examples and reply style once ~10 corrections exist) | ✓ |
| 8 | Calendar in the 08:00 summary: today, plus a 7-day countdown for events that need preparing | ✓ |
| 8.5 | Catch-up: a summary missed while the Mac slept is run at the next quick check | ✓ |
| 8.6 | Morning brief: Apple Reminders due + Hong Kong weather warnings, in the summary and the 08:00 Telegram message; dark-mode summary; no drafts to no-reply senders; daily database backup | ✓ |

## How this was built

Built with an AI pair-programmer (Claude Code): Claude wrote and tested the code; I made the design decisions, labelled the test sets and ran it on my own mail. I'm explaining each milestone back, unaided, as I go. So far: milestones 0, 1 and 3 and the Telegram ping (with corrections); 4, 5b and 6 partly; the rest are still to do.

## License

MIT
