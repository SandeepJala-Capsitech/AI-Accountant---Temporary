"""Puts each hand-worked agent case (eval/agent_cases/<case>/, and the demo, demo/northbridge/) through the real chat
API and scores it: the files uploaded in a "Review these documents" message, the case's answers applied through the
answer routes, then

- the posted trial balance against the expected one, to the penny (the demo: before the answers and after them);
- the decisions, a rule for each row, against expected_decisions.json;
- the questions open or waiting after the review turn and after the answers, a kind and a row each, against
  expected_questions.json;

and a run fails if it has an error event, a failed step, an answer that could not be applied or changes left unposted.

  .venv/bin/python eval/run_agent_eval.py                                   # every case and the demo, on the real models
  .venv/bin/python eval/run_agent_eval.py --case clean-month --read rows    # one case, files read from their .rows.json
  .venv/bin/python eval/run_agent_eval.py --runs 3 --case demo

It spends OpenRouter credit (the agent's model, and the vision model unless --read rows) and needs the MongoDB
cluster; it uses its own database (--db), dropped by nobody. Writes eval/results/<date>-agent.json (every run of
every case) and prints a table; exits 0 only when every run passed all three scores.

The scoring functions take plain data, so a test can score a case worked with no model."""
from __future__ import annotations

import argparse
import datetime as dt
import dataclasses
import json
import mimetypes
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

EVAL_DIR = Path(__file__).parent
ROOT = EVAL_DIR.parent
sys.path[:0] = [str(ROOT), str(EVAL_DIR)]   # run as a script, neither is on the path

import run_scripted_resolutions as scripted   # noqa: E402
from job_scoring import compare, net_by_account   # noqa: E402

CASES_DIR = EVAL_DIR / "agent_cases"
RESULTS_DIR = EVAL_DIR / "results"
DEMO_DIR = ROOT / "demo" / "northbridge"
MESSAGE = "Review these documents"


# ─── Scoring: plain data in, a list of differences out (empty when it passes) ─────────────────────────────────────

def expected_trial_balances(case_dir: Path) -> tuple[Optional[dict], dict]:
    """(the trial balance expected after the review turn, the one after the answers). The first is None for a case
    that has only the final one."""
    case_dir = Path(case_dir)
    read = lambda name: json.loads((case_dir / name).read_text())
    if (case_dir / "expected_trial_balance_before_answers.json").exists():
        return read("expected_trial_balance_before_answers.json"), read("expected_trial_balance_after_answers.json")
    return None, read("expected_trial_balance.json")


def score_trial_balance(expected: dict, posted: dict[str, Decimal]) -> list[dict]:
    """Every account whose net differs, as job_scoring.compare gives it. posted: net_by_account of the posted
    trial balance."""
    return compare(expected, posted)


def _key(name: dict) -> tuple[str, str]:
    return (name["file"], name["description"])


def _missing_and_extra(expected: list, actual: list) -> list[dict]:
    """What the expected list has and the actual lacks, and the reverse, each as a difference."""
    left = list(actual)
    differences = []
    for item in expected:
        if item in left:
            left.remove(item)
        else:
            differences.append({"missing": item})
    return differences + [{"unexpected": item} for item in left]


def score_decisions(expected: dict, decisions: list[dict], names: dict[int, dict]) -> list[dict]:
    """The rule applied to each row against expected_decisions.json (its resolutions, automatic and from_answers).
    decisions: the year's decisions as the route gives them, those applied counted; a decision is on the row it names
    first. names: each row's {"file", "description"} by id."""
    wanted = [(*_key(e["row"]), e["rule"]) for part in ("resolutions", "automatic", "from_answers")
              for e in expected[part]]
    made, unmapped = [], []
    for d in decisions:
        if d["status"] != "applied":
            continue
        if d["row_ids"] and d["row_ids"][0] in names:
            made.append((*_key(names[d["row_ids"][0]]), d["rule"]))
        else:   # on no row, or one the year doesn't hold: never expected
            unmapped.append({"unexpected": {"row_ids": d["row_ids"], "rule": d["rule"]}})
    return _missing_and_extra(sorted(wanted), sorted(made)) + unmapped


def named_questions(questions: list[dict], names: dict[int, dict]) -> list[dict]:
    """Questions as the route gives them, as {"kind", "row"} with the row named."""
    return [{"kind": q["kind"], "row": names[q["row_ids"][0]] if q["row_ids"] else None} for q in questions]


def score_questions(expected: list[dict], asked: list[dict]) -> list[dict]:
    """The questions asked, each a kind and a row, against those expected (their status ignored)."""
    shape = lambda q: (q["kind"], *(_key(q["row"]) if q["row"] else ("", "")))
    return _missing_and_extra(sorted(shape(q) for q in expected), sorted(shape(q) for q in asked))


# ─── Reading the files from their prepared rows, so only the agent is real ────────────────────────────────────────

class PreparedRows:
    """Replaces the vision model: ledgersync.pipeline.analyze finds which file it was given and returns nothing, and
    adapter.to_transactions then returns that file's rows from its .rows.json. Files are known as the server's
    intake.load_upload reads them: by their bytes (pdf, image) or their text (text, table, a spreadsheet's cells
    comma-separated). `use` says which case's files are in play, since two cases may hold a file of the same name
    with different rows."""

    def __init__(self):
        self.by_bytes: dict[bytes, Path] = {}
        self.by_text: dict[str, Path] = {}
        self.current = threading.local()
        self._analyze = self._to_transactions = None

    def use(self, files: list[Path]) -> None:
        from ledgersync import intake
        self.by_bytes, self.by_text = {}, {}
        for path in files:
            item = intake.load_upload(path.name, path.read_bytes(), 1000)
            known, key = (self.by_bytes, item.data) if item.data is not None else (self.by_text, item.text.strip())
            if known.get(key, path) != path:
                raise ValueError(f"{path.name} and {known[key].name} read the same, so their rows can't be told apart.")
            known[key] = path

    def _find(self, item) -> Path:
        path = None
        if item.data is not None:
            path = self.by_bytes.get(item.data)
        elif item.text is not None:
            path = self.by_text.get(item.text.strip())
        if path is None:
            raise ValueError("--read rows: this file is not one of the case's documents.")
        return path

    def install(self) -> None:
        from ledgersync import adapter, pipeline
        from ledgersync.extractor import TransactionExtractionResult
        from ledgersync.models import Transaction
        self._analyze, self._to_transactions = pipeline.analyze, adapter.to_transactions

        def analyze(item, reader, ctx):
            self.current.path = self._find(item)
            ctx.report(f"Reading {self.current.path.name} from its prepared rows")
            return TransactionExtractionResult(data=[], model="prepared rows")

        def to_transactions(rows, source, settings, **kw):
            path, self.current.path = getattr(self.current, "path", None), None
            if path is None:
                return self._to_transactions(rows, source, settings, **kw)
            read = json.loads(path.with_name(f"{path.name}.rows.json").read_text())
            return [Transaction.model_validate(t) for t in read["transactions"]]

        pipeline.analyze, adapter.to_transactions = analyze, to_transactions

    def uninstall(self) -> None:
        from ledgersync import adapter, pipeline
        pipeline.analyze, adapter.to_transactions = self._analyze, self._to_transactions


# ─── The run ──────────────────────────────────────────────────────────────────────────────────────────────────────

def events_of(body: str) -> list[tuple[str, dict]]:
    """The server-sent events of a stream, as (type, data)."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def case_files(case_dir: Path) -> list[Path]:
    """Every file a case reads: its documents, and the files its answers upload."""
    uploaded = [case_dir / a["answer"]["file"] for a in scripted.answers(case_dir) if a["answer"]["kind"] == "upload"]
    return scripted._documents(case_dir) + uploaded


def _multipart(path: Path) -> tuple[str, bytes, str]:
    return (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def _check(response) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(f"{response.request.method} {response.request.url.path}: {response.status_code} "
                           f"{response.text[:300]}")
    return response.json()


def _add_client(api, store, records, case_dir: Path) -> int:
    """The case's client, as the chat starts from it: the demo reset, any other case added by the API."""
    if case_dir.resolve() == DEMO_DIR.resolve():
        from ledgersync import demo
        return demo.reset(store, records, store.client[f"{store.db_name}_checkpoints"])
    client_id = _check(api.post("/api/clients", json=json.loads((case_dir / "client.json").read_text())))["id"]
    if (case_dir / "opening_balances.json").exists():
        _check(api.put(f"/api/clients/{client_id}/opening-balances",
                       json=json.loads((case_dir / "opening_balances.json").read_text())))
    return client_id


def _posted(api, client_id: int, year_start: dt.date) -> dict:
    """The year's posted trial balance as the route gives it."""
    return _check(api.get(f"/api/clients/{client_id}/years/{year_start}/posted-trial-balance"))


def _apply_answer(api, store, client_id: int, thread_id: str, case_dir: Path, item: dict) -> Optional[str]:
    """Answers the question item names, through its route; why it couldn't be, or None."""
    asked, given = item["question"], item["answer"]
    try:
        row_id = scripted._row_id(store, client_id, asked["row"])
    except LookupError as exc:
        return str(exc)
    question = next((q for q in _check(api.get(f"/api/clients/{client_id}/questions"))
                     if q["kind"] == asked["kind"] and q["row_ids"][:1] == [row_id]), None)
    if question is None:
        return f"No {asked['kind']} question is open on {asked['row']}."
    base = f"/api/chat/threads/{thread_id}/questions/{question['id']}"
    if given["kind"] == "upload":
        response = api.post(f"{base}/document", files={"file": _multipart(case_dir / given["file"])})
    else:
        response = api.post(f"{base}/answer", json=given)
    return None if response.status_code < 400 else f"{response.status_code} {response.text[:300]}"


def run_case(api, store, records, case_dir: Path, rows: Optional[PreparedRows], model: str) -> dict:
    """One run of a case: its scores, and what the turn cost (seconds, steps, failed steps, error events, refused
    decisions)."""
    case_dir = Path(case_dir)
    expected_decisions = json.loads((case_dir / "expected_decisions.json").read_text())
    expected_questions = json.loads((case_dir / "expected_questions.json").read_text())
    before_tb, after_tb = expected_trial_balances(case_dir)
    if rows is not None:
        rows.use(case_files(case_dir))
    started = time.monotonic()
    client_id = _add_client(api, store, records, case_dir)
    year_start = dt.date.fromisoformat(_check(api.get(f"/api/clients/{client_id}/years"))[0]["start"])
    thread = _check(api.post("/api/chat/threads", json={"client_id": client_id}))

    sent = api.post(f"/api/chat/threads/{thread['id']}/messages", data={"text": MESSAGE},
                    files=[("files", _multipart(p)) for p in scripted._documents(case_dir)])
    if sent.status_code != 200:
        raise RuntimeError(f"The message was refused: {sent.status_code} {sent.text[:300]}")
    events = events_of(sent.text)
    steps: dict[str, dict] = {}
    for kind, data in events:
        if kind == "step":
            steps[data["id"]] = data
    names = scripted._names(store, client_id)
    asked = named_questions(_check(api.get(f"/api/clients/{client_id}/questions")), names)
    review_posted = _posted(api, client_id, year_start)
    review_tb = net_by_account(review_posted)

    unapplied = [problem for item in scripted.answers(case_dir)
                 if (problem := _apply_answer(api, store, client_id, thread["id"], case_dir, item))]
    seconds = round(time.monotonic() - started, 1)
    names = scripted._names(store, client_id)   # an answer's document adds rows
    asked_after = named_questions(_check(api.get(f"/api/clients/{client_id}/questions")), names)
    final_posted = _posted(api, client_id, year_start)
    final_tb = net_by_account(final_posted)
    decisions = _check(api.get(f"/api/clients/{client_id}/decisions?year_start={year_start}"))

    tb_diff = score_trial_balance(after_tb, final_tb)
    if before_tb is not None:
        tb_diff = [{"stage": "after answers", **d} for d in tb_diff] + \
                  [{"stage": "before answers", **d} for d in score_trial_balance(before_tb, review_tb)]
    q_diff = [{"stage": "before answers", **d}
              for d in score_questions(expected_questions["before_answers"], asked)]
    if "after_answers" in expected_questions:
        q_diff += [{"stage": "after answers", **d}
                   for d in score_questions(expected_questions["after_answers"], asked_after)]
    d_diff = score_decisions(expected_decisions, decisions, names)
    failed = [s["text"] for s in steps.values() if s["state"] == "failed"]
    errors = [data for kind, data in events if kind == "error"]
    problems = ([f"error event: {e.get('code')}: {e.get('message')}" for e in errors]
                + [f"failed step: {t}" for t in failed] + [f"answer not applied: {u}" for u in unapplied]
                + ([f"{final_posted['unposted_changes']} changes are not posted"]
                   if final_posted["unposted_changes"] else []))
    return {
        "case": case_dir.name, "model": model, "seconds": seconds,   # the review turn and the answers
        "trial_balance": {"passed": not tb_diff, "differences": tb_diff},
        "decisions": {"passed": not d_diff, "differences": d_diff},
        "questions": {"passed": not q_diff, "differences": q_diff},
        "problems": problems,   # a run with any of these fails, however its scores come out
        "posted_problems": final_posted["problems"],   # why the books couldn't be fully worked out; not gating
        "steps": len(steps), "failed_steps": failed, "error_events": errors,
        "refused_decisions": [{"rule": d["rule"], "reason": d["reason"]} for d in decisions
                              if d["status"] == "refused"],
        "answers_not_applied": unapplied,
    }


def passed(result: dict) -> bool:
    """Whether a run passed all three scores and had no problems."""
    return all(result[s]["passed"] for s in ("trial_balance", "decisions", "questions")) and not result["problems"]


def _table(runs: list[dict]) -> str:
    rows = [("case", "run", "TB", "decisions", "questions", "clean", "seconds")]
    for r in runs:
        mark = lambda score: "pass" if r[score]["passed"] else "FAIL"
        rows.append((r["case"], str(r["run"]), mark("trial_balance"), mark("decisions"), mark("questions"),
                     "yes" if not r["problems"] else "NO", str(r["seconds"])))
    widths = [max(len(row[i]) for row in rows) for i in range(7)]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The accountant chat on every hand-worked case, scored.")
    parser.add_argument("--case", action="append", help='a case folder name, or "demo"; repeat for several')
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--read", choices=["model", "rows"], default="model",
                        help="rows: files read from their .rows.json, so only the agent is real")
    parser.add_argument("--db", default="ledgersync_agent_eval")
    args = parser.parse_args(argv)

    available = {p.name: p for p in sorted(CASES_DIR.iterdir()) if (p / "client.json").exists()}
    available["demo"] = DEMO_DIR
    chosen = args.case or list(available)
    unknown = [c for c in chosen if c not in available]
    if unknown:
        parser.error(f"unknown case {unknown}; choose from {sorted(available)}")

    from fastapi.testclient import TestClient
    from ledgersync.config import Settings
    from ledgersync.records import AgentRecords
    from ledgersync.store_mongo import MongoStore
    from server import create_app

    settings = dataclasses.replace(Settings.from_env(), warmup=False, mongodb_db_name=args.db)
    rows = PreparedRows() if args.read == "rows" else None
    store = MongoStore(settings.mongodb_uri, args.db, max_pool=settings.mongodb_max_pool)
    runs = []
    try:
        if rows:
            rows.install()
        with TestClient(create_app(settings, store=store), headers={"X-LedgerSync": "1"}) as api:
            records = AgentRecords(store.db)
            for name in chosen:
                for n in range(1, args.runs + 1):
                    try:
                        result = run_case(api, store, records, available[name], rows, settings.agent_model)
                    except Exception as exc:   # a case that can't run is a failed run, not the end of the eval
                        result = {"case": name, "model": settings.agent_model, "seconds": 0,
                                  **{s: {"passed": False, "differences": []} for s in
                                     ("trial_balance", "decisions", "questions")},
                                  "problems": [f"the run failed: {type(exc).__name__}: {exc}"], "posted_problems": [],
                                  "steps": 0, "failed_steps": [], "error_events": [], "refused_decisions": [],
                                  "answers_not_applied": []}
                    result["run"] = n
                    runs.append(result)
                    print(f"{name} run {n}: " + ("passed" if passed(result) else "FAILED"), flush=True)
    finally:
        if rows and rows._analyze:
            rows.uninstall()
        store.close()

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{dt.date.today().isoformat()}-agent.json"
    out.write_text(json.dumps({"date": dt.date.today().isoformat(), "read": args.read, "model": settings.agent_model,
                               "runs": runs}, indent=2, ensure_ascii=False) + "\n")
    print("\n" + _table(runs) + f"\n\nWrote {out.relative_to(ROOT)}")
    return 0 if all(passed(r) for r in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
