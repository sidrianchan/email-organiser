"""The labels, and what each one means. Shared by Claude's prompt and your hand-labelling.

Every email gets TWO answers:
  1. ONE type from LABELS below. If several fit, the FIRST in the list wins.
  2. A separate yes/no: does it need a reply? (REPLY_RULE)
"Needs a reply" is an action, not a type: an interview invite is a 'job' email
AND needs a reply. Milestone 4 drafts replies for every email marked yes.
"""

LABELS = {
    "job":         "An employer or recruiter did something about Sid's OWN job hunt: "
                   "viewed or updated an application, rejected it, asked for information, "
                   "invited him to an interview or assessment, made an offer, or contacted him. "
                   "ALSO job: any later message about an application already in progress, even "
                   "if automated (status updates, 'we've received your assessment', next steps, "
                   "reminders to complete a stage), and any message in a thread with a recruiter, "
                   "including replies to his questions. NOT job: only the very first 'we received "
                   "your application' / 'application submitted' confirmation ('fyi'), and "
                   "job-listing alerts or recommendations ('newsletter').",
    "spam":        "Unwanted junk, scams, or anything that looks like phishing.",
    "read":        "Worth reading today, no reply needed: security alerts, bills, "
                   "deliveries, personal news, account changes.",
    "fyi":         "Routine automatic notices that are safe to skim or skip: receipts, "
                   "confirmations, notifications he triggered himself.",
    "promo":       "Marketing, sales, offers, discounts.",
    "newsletter":  "Content he subscribed to: newsletters, digests, job-listing alerts, "
                   "social media notifications.",
}

REPLY_RULE = ("needs_reply = true if a real person or organisation expects Sid to answer "
              "or send something back by email. Automatic notifications are false.")

JOB_ALERT_RULE = ("job_alert = true if the email lists one or more specific job openings "
                  "(job alerts, job recommendations, 'X is hiring'). Profile or engagement "
                  "reminders, career tips, hiring statistics and application updates are false.")

NAMES = list(LABELS)  # ["job", "spam", ...] in priority order


def describe() -> str:
    """The label list as text, numbered, for prompts and menus."""
    return "\n".join(f"{i}. {name}: {meaning}"
                     for i, (name, meaning) in enumerate(LABELS.items(), start=1))
