# Email Organiser

A personal AI email assistant. It reads my three mailboxes (Yahoo, iCloud, Gmail), has Claude label every new email, and puts a short summary in my inbox at 08:00 and 18:00: job emails first, then what needs a reply, then what's worth reading. The rest is just counted. For emails that need a reply, Claude writes a draft and leaves it in that account's Drafts folder, threaded under the original, with [gaps] for anything only I know. Spam, promos and newsletters move out of the inbox into an `Assistant-Review` folder. Job-alert emails are screened first: the jobs in them are rated against my profile, and the ones worth applying to head the summary.

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

## Things I found along the way

- **A privacy leak in my own setup.** By default, `claude -p` loaded my personal instruction files (including financial notes) into every labelling call. I caught it by asking the model what it could see. Fixed with `--setting-sources ""`, which also cut input from about 5,500 to about 1,000 tokens per call.
- **A bad label rule, not a bad model.** My first "job" label caught "application submitted" confirmations, which would have fired the alert ~25 times a week. I narrowed it to *an employer did something*.
- **A design fix from the data.** One email was both "job" and "needs a reply", and a single label forced a false choice. "Needs reply" became a separate yes/no flag.
- **Duplicates beat losses.** Emails are only marked as summarised *after* the summary is delivered. A failed delivery means a repeat next time, never a silently lost email.
- IMAP's `SINCE` search only understands dates, not times, so the code over-fetches and trims. Yahoo also rejects `HEADER` searches.
- **Every provider is different.** The Drafts folder is `Draft` on Yahoo, `Drafts` on iCloud (found by name, because iCloud doesn't tag it) and `[Gmail]/Drafts` on Gmail. Yahoo returns an empty answer when asked for just two header fields (`HEADER.FIELDS`), so the first live test addressed a draft to the sender instead of the Reply-To address. The fix fetches the whole header. Yahoo also drops the `\Draft` flag on saved drafts; Mail.app still treats them as drafts because of the folder.
- **iCloud can't MOVE.** Yahoo and Gmail support the IMAP `MOVE` command; iCloud doesn't. There, a move is copy → flag → `UID EXPUNGE` of exactly those emails. A plain `EXPUNGE` would also wipe anything else flagged as deleted, so the code refuses to run on a server without `UID EXPUNGE`.
- **Job alerts are mostly links.** A Jobsdb alert is ~14,700 characters, mostly tracking URLs. The screener swaps every link for a tag like `[L3]` before Claude reads it; Claude answers with tags and the code puts the real link back, only if it points at the site that sent the email. Claude can't invent or redirect a link.
- **Sender lists are brittle.** LinkedIn sends job alerts from three different addresses; the dry run caught one I'd missed (a "… are hiring" email) before anything moved.

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

The Telegram ping carries counts only ("1 job, 2 to read"), never senders or subjects,
because bot chats are not end-to-end encrypted. It is sent every run, even with no new
mail, so a missing ping means the schedule broke. If Telegram fails, the summary is unaffected.

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
| 5b | Weekly unsubscribe from senders I tick | next |
| 6 | Instant alert for recruiter and interview mail | |
| 7 | Learns from my corrections, re-measured weekly | |

## How this was built

Built with an AI pair-programmer (Claude Code). Each milestone ended with me explaining its design back, answering questions on why it works the way it does, before it was merged.

## License

MIT
