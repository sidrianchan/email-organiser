"""Milestone 2: how often does Claude agree with you?

Run:  python3 -m uv run python -m email_organiser.evaluate

Only emails that BOTH you and Claude labelled count.
"""
from collections import Counter

from . import store
from .labels import NAMES


def main() -> None:
    db = store.open_db()
    rows = db.execute("""
        SELECT m.sender, m.subject, t.label AS mine, c.label AS claude, c.reason,
               t.needs_reply AS my_reply, c.needs_reply AS claude_reply
        FROM my_labels t
        JOIN claude_labels c USING (account, uidvalidity, uid)
        JOIN messages m USING (account, uidvalidity, uid)
        WHERE t.label IN ({}) AND t.needs_reply IS NOT NULL AND c.needs_reply IS NOT NULL
    """.format(",".join("?" * len(NAMES))), NAMES).fetchall()
    if not rows:
        print("Nothing to compare yet: label some emails by hand and run classify first.")
        return

    right = sum(r["mine"] == r["claude"] for r in rows)
    print(f"Type: Claude agreed with you on {right}/{len(rows)} = {right / len(rows):.0%}")

    # The reply flag. Recall matters most: a missed "needs reply" means no draft.
    yes_mine = sum(r["my_reply"] == 1 for r in rows)
    yes_claude = sum(r["claude_reply"] == 1 for r in rows)
    yes_both = sum(r["my_reply"] == 1 and r["claude_reply"] == 1 for r in rows)
    same = sum(r["my_reply"] == r["claude_reply"] for r in rows)
    print(f"Needs reply: agreed on {same}/{len(rows)}. You said yes {yes_mine}x, "
          f"Claude {yes_claude}x, both {yes_both}x "
          f"(recall {yes_both / yes_mine:.0%})" if yes_mine else
          f"Needs reply: agreed on {same}/{len(rows)}. You said yes 0x, Claude {yes_claude}x")
    print()

    # Per label. "Precision" = when Claude says X, how often is it really X?
    #            "Recall"    = of the real X emails, how many did Claude catch?
    # For 'job', recall matters most: a missed interview invite is the expensive mistake.
    mine_count = Counter(r["mine"] for r in rows)
    claude_count = Counter(r["claude"] for r in rows)
    both = Counter(r["mine"] for r in rows if r["mine"] == r["claude"])
    print(f"{'label':<12}{'yours':>6}{'claude':>8}{'precision':>11}{'recall':>8}")
    for name in NAMES:
        if mine_count[name] or claude_count[name]:
            p = f"{both[name] / claude_count[name]:.0%}" if claude_count[name] else "-"
            rc = f"{both[name] / mine_count[name]:.0%}" if mine_count[name] else "-"
            print(f"{name:<12}{mine_count[name]:>6}{claude_count[name]:>8}{p:>11}{rc:>8}")

    print("\nDisagreements (yours -> Claude's: Claude's reason):")
    for r in rows:
        if r["mine"] != r["claude"]:
            print(f"  type  {r['mine']:>10} -> {r['claude']:<10} {r['subject'][:45]!r}: {r['reason']}")
        if r["my_reply"] != r["claude_reply"]:
            yn = {1: "yes", 0: "no"}
            print(f"  reply {yn[r['my_reply']]:>10} -> {yn[r['claude_reply']]:<10} "
                  f"{r['subject'][:45]!r}: {r['reason']}")


if __name__ == "__main__":
    main()
