"""The gate for screener changes: does a new prompt or model rate jobs more like you do?

Answer key = your taps on the job buttons (feedback.py): "Would apply" and "Applied" mean the
job should be rated high; "Not qualified" and "Not interested" mean it shouldn't.
Test set = the original alert emails behind those jobs, saved once with --fetch, so every
version reads exactly what the live screener read. Both live in ~/.email-organiser, never the repo.

    python3 -m uv run python -m email_organiser.screener_gate --fetch           (save the alerts)
    python3 -m uv run python -m email_organiser.screener_gate --save-baseline   (freeze today's prompt)
    python3 -m uv run python -m email_organiser.screener_gate --run old --baseline   (2 runs, saved)
    python3 -m uv run python -m email_organiser.screener_gate --run new              (working copy)
    python3 -m uv run python -m email_organiser.screener_gate --run new-sonnet --model claude-sonnet-5-5
    python3 -m uv run python -m email_organiser.screener_gate --compare old new
Runs are saved in ~/.email-organiser/screener_runs/, so a comparison never re-runs anything.

Kept only if (Sid, 20 Nov): no job you'd apply to is lost consistently (rated high in every
baseline run, not high in every new run), and more jobs are fixed consistently than broken.
Jobs that flip between runs are noise and count either way. Read-only: stores nothing in mail.db.
Cost: about (alerts / 4) Claude calls per run.
"""
import argparse
import email
import json
from datetime import timedelta
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

from . import classify, paths, screener, store

TESTSET = paths.HOME / "screener_testset.json"
BASELINE = paths.HOME / "screener_baseline.json"
YES = {"apply", "applied"}


def answer_key(db) -> list[dict]:
    """One entry per job you marked: what it is, which alert it came from, and the right answer."""
    return [dict(r) | {"should_be_high": r["verdict"] in YES} for r in db.execute("""
        SELECT j.job_key, j.title, j.company, f.verdict, f.fit_then, m.message_id
        FROM job_feedback f JOIN jobs j USING (job_key)
        JOIN messages m USING (account, uidvalidity, uid) ORDER BY j.job_key""")]


def fetch(db) -> int:
    """Save the alerts behind the answer key (read-only, BODY.PEEK). Looks in INBOX and
    Assistant-Review, matching on Message-ID (Yahoo can't search headers, so by sender + date)."""
    from .mail import connect, load_accounts
    rows = db.execute("""SELECT DISTINCT m.account, m.message_id, m.sender, m.subject, m.date
        FROM job_feedback f JOIN jobs j USING (job_key) JOIN messages m USING (account, uidvalidity, uid)""").fetchall()
    saved = json.loads(TESTSET.read_text()) if TESTSET.exists() else {}
    accounts = {a["name"]: a for a in load_accounts()}
    for name in sorted({r["account"] for r in rows}):
        todo = [r for r in rows if r["account"] == name and r["message_id"] not in saved]
        if not todo:
            continue
        with connect(accounts[name]) as c:
            for folder in ("INBOX", "Assistant-Review"):
                c.select_folder(folder, readonly=True)
                for r in todo:
                    if r["message_id"] in saved:
                        continue
                    d = parsedate_to_datetime(r["date"])
                    uids = c.search(["FROM", parseaddr(r["sender"])[1], "SINCE", (d - timedelta(days=1)).date(),
                                     "BEFORE", (d + timedelta(days=2)).date()])
                    env = c.fetch(uids, ["ENVELOPE"]) if uids else {}
                    hit = [u for u in uids if (env[u][b"ENVELOPE"].message_id or b"").decode() == r["message_id"]]
                    if hit:
                        raw = c.fetch(hit[:1], ["BODY.PEEK[]"])[hit[0]][b"BODY[]"]
                        text, links = screener.tag_links(email.message_from_bytes(raw, policy=policy.default))
                        saved[r["message_id"]] = {"sender": r["sender"], "subject": r["subject"],
                                                  "text": text, "links": links}
    TESTSET.write_text(json.dumps(saved, ensure_ascii=False))
    return len(saved)


def same_job(job: dict, marked: dict) -> bool:
    if screener.job_key(job) == marked["job_key"]:
        return True
    first = lambda c: (screener.norm(c).split() or [""])[0]
    return first(job["company"]) == first(marked["company"]) and screener.same_title(job["title"], marked["title"])


def run_once(key: list[dict], alerts: dict, prompt: str, schema: dict, model: str | None) -> list[str | None]:
    """The fit given to each marked job in one run (None = not found in the alert)."""
    ids = sorted({k["message_id"] for k in key if k["message_id"] in alerts})
    batch = [{"row": {"sender": alerts[i]["sender"], "subject": alerts[i]["subject"], "message_id": i},
              "text": alerts[i]["text"][:screener.MAX_CHARS], "links": alerts[i]["links"]} for i in ids]
    jobs = screener.screen(batch, paths.PROFILE.read_text(), prompt=prompt, schema=schema, model=model)
    out = []
    for k in key:
        found = [j for j in jobs if j["row"]["message_id"] == k["message_id"] and same_job(j, k)]
        out.append(found[0]["fit"] if found else None)
    return out


def score(key: list[dict], fits: list) -> dict:
    yes = [f for k, f in zip(key, fits) if k["should_be_high"]]
    no = [f for k, f in zip(key, fits) if not k["should_be_high"]]
    high = sum(f == "high" for f in fits)
    caught = sum(f == "high" for f in yes)
    return {"right": sum((f == "high") == k["should_be_high"] for k, f in zip(key, fits)), "total": len(key),
            "caught": caught, "yes": len(yes), "high": high, "wrongly_high": sum(f == "high" for f in no),
            "no": len(no), "missing": sum(f is None for f in fits)}


def line(s: dict) -> str:
    precision = f"{s['caught'] / s['high']:.0%}" if s["high"] else "n/a"
    return (f"right {s['right']}/{s['total']}; your yeses rated high {s['caught']}/{s['yes']}; "
            f"your noes rated high {s['wrongly_high']}/{s['no']}; high that you'd apply to {precision}; "
            f"not found {s['missing']}")


def verdict(key: list[dict], old: list[list], new: list[list]) -> tuple[bool, list[str]]:
    right = lambda runs, n: [(r[n] == "high") == key[n]["should_be_high"] for r in runs]
    lost, gained = [], []
    for n, k in enumerate(key):
        o, w = right(old, n), right(new, n)
        if all(o) and not any(w):
            lost.append(k)
        elif all(w) and not any(o):
            gained.append(k)
    yes_lost = [k for k in lost if k["should_be_high"]]
    ok = not yes_lost and len(gained) > len(lost)
    notes = [f"{'gained' if k in gained else 'LOST'}: {k['verdict']:<14} {k['title'][:50]} | {k['company'][:25]}"
             for k in gained + lost]
    return ok, [f"consistently fixed {len(gained)}, broken {len(lost)} ({len(yes_lost)} you'd apply to)"] + notes


RUNS = paths.HOME / "screener_runs"


def save_runs(name: str, key: list[dict], runs: list[list], model: str, baseline: bool) -> None:
    RUNS.mkdir(exist_ok=True)
    (RUNS / f"{name}.json").write_text(json.dumps({"jobs": [k["job_key"] for k in key], "fits": runs,
                                                   "model": model, "baseline": baseline}))


def load_runs(name: str, key: list[dict]) -> list[list]:
    data = json.loads((RUNS / f"{name}.json").read_text())
    if data["jobs"] != [k["job_key"] for k in key]:
        raise SystemExit(f"{name}: the answer key changed since this run; run it again")
    return data["fits"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--run", metavar="NAME", help="run and save under NAME")
    parser.add_argument("--baseline", action="store_true", help="with --run: the saved baseline prompt")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--model", help="default: the baseline's model, or the live screener's")
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"))
    args = parser.parse_args()
    db = store.open_db()
    if args.fetch:
        print(f"{fetch(db)} alerts saved in {TESTSET}")
        return
    if args.save_baseline:
        BASELINE.write_text(json.dumps({"prompt": screener.SYSTEM_PROMPT, "schema": screener.SCHEMA,
                                        "model": classify.MODEL}))
        print(f"Baseline saved: {BASELINE}")
        return
    key, alerts = answer_key(db), json.loads(TESTSET.read_text())
    key = [k for k in key if k["message_id"] in alerts]
    if args.compare:
        old, new = (load_runs(n, key) for n in args.compare)
        for name, runs in zip(args.compare, (old, new)):
            for n, fits in enumerate(runs):
                print(f"{name} run {n + 1}: {line(score(key, fits))}")
        ok, notes = verdict(key, old, new)
        print(f"\n{'KEEP' if ok else 'REJECT'}: " + "\n  ".join(notes))
        return
    if args.run:
        base = json.loads(BASELINE.read_text())
        prompt, schema = (base["prompt"], base["schema"]) if args.baseline else (screener.SYSTEM_PROMPT, screener.SCHEMA)
        model = args.model or (base["model"] if args.baseline else classify.SCREEN_MODEL)
        print(f"{args.run}: {len(key)} marked jobs from {len({k['message_id'] for k in key})} alerts, {model}")
        runs = []
        for n in range(args.runs):
            runs.append(run_once(key, alerts, prompt, schema, model))
            print(f"  run {n + 1}: {line(score(key, runs[-1]))}", flush=True)
        save_runs(args.run, key, runs, model, args.baseline)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
