"""Milestone 2: YOU label emails, to build the test set we measure Claude against.

Run in the Terminal app (it waits for your keypresses):
    cd ~/projects/email-organiser
    python3 -m uv run python -m email_organiser.label_by_hand

For each email you give a type (a number) and then answer: needs a reply? (y/n)

It first finishes any emails you labelled before the reply question existed,
then continues with new ones. It never shows Claude's answer: if you could see
Claude's guess you'd tend to agree with it (anchoring), and the test would end up
measuring Claude against itself. Stop any time with q; progress is saved.
"""
from . import store
from .labels import NAMES, REPLY_RULE, describe

TARGET = 50


def show(r) -> None:
    print("-" * 70)
    print(f"[{r['account']}] From: {r['sender']}")
    print(f"Subject: {r['subject']}")
    print(f"{(r['snippet'] or '')[:300]}")


def ask_type() -> str | None:
    """Return a label name, 'skip', or None for quit."""
    while True:
        choice = input(f"Type 1-{len(NAMES)} / s / q: ").strip().lower()
        if choice == "q":
            return None
        if choice == "s":
            return "skip"
        if choice.isdigit() and 1 <= int(choice) <= len(NAMES):
            return NAMES[int(choice) - 1]


def ask_reply() -> int | None:
    """Return 1 (yes), 0 (no), or None for quit."""
    while True:
        choice = input("Needs a reply? y / n / q: ").strip().lower()
        if choice in ("y", "n"):
            return 1 if choice == "y" else 0
        if choice == "q":
            return None


def main() -> None:
    db = store.open_db()
    print(describe())
    print(f"\nNeeds a reply: {REPLY_RULE}\n")

    # 1. Emails you labelled before the reply question existed. Your old type is kept,
    #    unless it was the old 'needs_reply' type, which no longer exists: then you
    #    pick a type again.
    old = db.execute("""
        SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.snippet, t.label
        FROM my_labels t JOIN messages m USING (account, uidvalidity, uid)
        WHERE t.needs_reply IS NULL
    """).fetchall()
    if old:
        print(f"First, {len(old)} emails you already labelled need the reply answer.\n")
    for r in old:
        show(r)
        label = r["label"]
        if label not in NAMES:
            print(f"(You chose '{label}' before, which is now a separate question. Pick a type.)")
            label = ask_type()
            if label in (None, "skip"):
                if label is None:
                    return
                continue
        else:
            print(f"Your type: {label}")
        reply = ask_reply()
        if reply is None:
            return
        db.execute("UPDATE my_labels SET label = ?, needs_reply = ? "
                   "WHERE account = ? AND uidvalidity = ? AND uid = ?",
                   (label, reply, r["account"], r["uidvalidity"], r["uid"]))
        db.commit()

    # 2. New emails, until you've done TARGET in total.
    have = db.execute("SELECT COUNT(*) FROM my_labels WHERE needs_reply IS NOT NULL").fetchone()[0]
    rows = db.execute("""
        SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.snippet
        FROM messages m
        LEFT JOIN my_labels t USING (account, uidvalidity, uid)
        WHERE t.label IS NULL
        ORDER BY m.first_seen DESC
    """).fetchall()
    for r in rows:
        if have >= TARGET:
            break
        show(r)
        label = ask_type()
        if label is None:
            break
        if label == "skip":
            continue
        reply = ask_reply()
        if reply is None:
            break
        db.execute("INSERT OR REPLACE INTO my_labels (account, uidvalidity, uid, label, needs_reply) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (r["account"], r["uidvalidity"], r["uid"], label, reply))
        db.commit()  # saved immediately
        have += 1
        print(f"  -> {label}, reply: {'yes' if reply else 'no'}  ({have}/{TARGET})")

    print(f"\nComplete labels by you: {have}. Target {TARGET}.")


if __name__ == "__main__":
    main()
