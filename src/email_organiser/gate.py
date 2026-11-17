"""The gate: should a change to Claude's instructions be kept? (milestone 7b)

Runs Claude on every hand-labelled email RUNS times with the instructions at a git ref
(default: main, i.e. what's live) and RUNS times with the working copy, same batches, then
applies learn.kept(): judged per email, because totals wobble run to run.

    python3 -m uv run python -m email_organiser.gate            (compares working copy vs main)
    python3 -m uv run python -m email_organiser.gate --ref m7-done --runs 3

Costs 2 x RUNS x (emails / 20) Claude calls (about 30 for 87 emails). Read-only.
"""
import argparse
import subprocess

from . import classify, learn, store
from .labels import LABELS


def prompt_at(ref: str) -> str:
    """Claude's instructions as they were at a git ref (labels.py is where they change)."""
    source = subprocess.run(["git", "show", f"{ref}:src/email_organiser/labels.py"],
                            capture_output=True, text=True, check=True).stdout
    old = {}
    exec(compile(source, "labels.py", "exec"), old)   # our own file from git history
    prompt = classify.SYSTEM_PROMPT
    for name, meaning in LABELS.items():
        prompt = prompt.replace(meaning, old["LABELS"].get(name, meaning))
    return prompt


def runs(items: list, prompt: str, n: int) -> list[list[bool]]:
    classify.SYSTEM_PROMPT = prompt
    out = []
    for i in range(n):
        answers, _ = learn.run_test(items)
        s = learn.score(items, answers)
        print(f"  run {i + 1}: type {s['correct']}/{s['total']}, job {s['job_correct']}/{s['job_total']}", flush=True)
        out.append([bool(a) and learn.right(it, a) for it, a in zip(items, answers)])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="main")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    new_prompt = classify.SYSTEM_PROMPT
    old_prompt = prompt_at(args.ref)
    if old_prompt == new_prompt:
        print(f"No change to Claude's instructions since {args.ref}: nothing to judge.")
        return
    items = [i for i in learn.test_set(store.open_db()) if i["set"] == "hand"]
    print(f"Old instructions ({args.ref}):")
    old = runs(items, old_prompt, args.runs)
    print("New instructions (working copy):")
    new = runs(items, new_prompt, args.runs)
    ok, why = learn.kept(old, new, [i["label"] == "job" for i in items])
    print(f"\n{'KEEP' if ok else 'REJECT'}: {why}")


if __name__ == "__main__":
    main()
