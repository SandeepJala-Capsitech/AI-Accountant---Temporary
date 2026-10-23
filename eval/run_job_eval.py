"""Gate 1: runs the client-level test set (eval/jobs/) through a running LedgerSync API, treated as a black box, and
scores each agent job's trial balance against the one worked out by hand, account by account, to the penny.

  .venv/bin/python server.py                                      # in one terminal; needs MongoDB and an agent model
  .venv/bin/python eval/run_job_eval.py --label gate1             # in another
  .venv/bin/python eval/run_job_eval.py --label gate1 --case ltd- # only the cases whose folder name contains "ltd-"

For each case it adds the client (client.json), reads every document in documents/ for it, starts an agent job for
period.json and waits while the job is preparing (30 minutes at most), then scores the job's latest after_gate
snapshot against expected_trial_balance.json. Writes eval/results/<date>-jobs-<label>.json and exits 0 only when
every case matches."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import run_eval
from job_scoring import compare, net_by_account

EVAL_DIR = Path(__file__).parent
JOBS_DIR = EVAL_DIR / "jobs"
RESULTS_DIR = EVAL_DIR / "results"
JOB_TIMEOUT = 30 * 60       # seconds a job may stay preparing before the run gives up on it
JOB_POLL_SECONDS = 5.0
ANALYSIS_TIMEOUT = 600      # seconds allowed to read each document, as run_eval allows


def load_cases(root: Path, pattern: str = "") -> list[Path]:
    """The case folders under root whose name contains pattern, by name."""
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "client.json").exists() and pattern in p.name)


def _read(case: Path, name: str):
    return json.loads((case / name).read_text())


def _documents(case: Path) -> list[Path]:
    """The case's documents, by name, without hidden files such as .DS_Store."""
    return sorted(p for p in (case / "documents").iterdir() if p.is_file() and not p.name.startswith("."))


def _new_outcome(case: Path) -> dict:
    """A case's result before anything has run. differences stays None until the trial balance is compared."""
    return {"case": case.name, "matched": False, "error": None, "error_detail": None, "client_id": None,
            "job_id": None, "status": None, "documents": [], "snapshot_version": None, "is_balanced": None,
            "problems": [], "differences": None, "gate_passed": None, "gate_checks": [], "exceptions": None,
            "fix_rounds": None, "job_time_s": None, "run_time_s": None}


def _wait_for_job(client, job: dict, *, sleep, clock, timeout: float) -> Optional[dict]:
    """Polls the job while it is preparing, for at most `timeout` seconds. The job as it ended, or as it stood when
    time ran out; None when the API stopped knowing it."""
    deadline = clock() + timeout
    while job["status"] == "preparing" and clock() < deadline:
        sleep(JOB_POLL_SECONDS)
        resp = run_eval._call(client.get, f"/api/agent-jobs/{job['id']}")
        if resp.status_code != 200:
            return None
        job = resp.json()
    return job


def _score(client, case: Path, job_id: str, outcome: dict) -> None:
    """Compares the job's latest after_gate snapshot with the expected trial balance. A trial balance that couldn't
    be built has no lines and all-zero totals, so its problems are reported instead and it is not compared."""
    resp = run_eval._call(client.get, f"/api/agent-jobs/{job_id}/snapshots")
    if resp.status_code != 200:
        outcome.update(error="snapshots_unavailable", error_detail=run_eval._error_code(resp))
        return
    after_gate = [s for s in resp.json() if s.get("stage") == "after_gate"]
    if not after_gate:
        outcome.update(error="no_after_gate_snapshot")
        return
    latest = max(after_gate, key=lambda s: s["version"])
    outcome.update(snapshot_version=latest["version"], is_balanced=latest["is_balanced"],
                   problems=list(latest.get("problems") or []))
    if outcome["problems"]:
        return
    expected = {code: Decimal(net) for code, net in _read(case, "expected_trial_balance.json").items()}
    outcome["differences"] = compare(expected, net_by_account(latest))


def _run(client, case: Path, outcome: dict, *, sleep, clock, job_timeout: float, analysis_timeout: float) -> None:
    resp = run_eval._call(client.post, "/api/clients", json=_read(case, "client.json"), headers=run_eval.PAGES)
    if resp.status_code != 201:
        outcome.update(error="client_not_added", error_detail=run_eval._error_code(resp))
        return
    client_id = outcome["client_id"] = resp.json()["id"]

    for path in _documents(case):
        result, error = run_eval.run_document(client, path, analysis_timeout, client_id=client_id)
        outcome["documents"].append({"name": path.name, "error": error,
                                     "rows": len((result or {}).get("transactions") or [])})
    unread = [d["name"] for d in outcome["documents"] if d["error"] or not d["rows"]]
    if unread:   # the job would work on rows short of the documents, so its trial balance can't match
        outcome.update(error="analysis_failed", error_detail=f"No rows saved from: {', '.join(unread)}")
        return

    resp = run_eval._call(client.post, f"/api/clients/{client_id}/agent-jobs", json=_read(case, "period.json"),
                          headers=run_eval.PAGES)
    if resp.status_code != 202:
        outcome.update(error="job_not_started", error_detail=run_eval._error_code(resp))
        return
    job_id = outcome["job_id"] = resp.json()["id"]
    started = clock()
    job = _wait_for_job(client, resp.json(), sleep=sleep, clock=clock, timeout=job_timeout)
    outcome["job_time_s"] = round(clock() - started, 1)
    if job is None:
        outcome.update(error="job_not_found")
        return

    gate = job.get("gate") or {}
    outcome.update(status=job["status"], fix_rounds=job.get("fix_rounds"), gate_passed=gate.get("passed"),
                   gate_checks=[{"code": c["code"], "passed": c["passed"], "detail": c["detail"]}
                                for c in gate.get("checks", [])])
    resp = run_eval._call(client.get, f"/api/agent-jobs/{job_id}/exceptions")
    if resp.status_code == 200:
        outcome["exceptions"] = len(resp.json())

    if job["status"] == "preparing":
        try:   # stop the work, so a later run doesn't share the API with it
            run_eval._call(client.post, f"/api/agent-jobs/{job_id}/cancel", headers=run_eval.PAGES, retries=1)
        except run_eval.ApiUnreachable:
            pass
        outcome.update(error="eval_timeout", error_detail=f"Still preparing after {job_timeout:.0f} s; cancelled.")
    elif job["status"] != "awaiting_review":
        outcome.update(error=f"job_{job['status']}", error_detail=job.get("error"))
    else:
        _score(client, case, job_id, outcome)


def run_case(client, case: Path, *, sleep=time.sleep, clock=time.monotonic, job_timeout: float = JOB_TIMEOUT,
             analysis_timeout: float = ANALYSIS_TIMEOUT) -> dict:
    """Adds the case's client, reads its documents, runs an agent job and scores the job's trial balance. It
    matches only when every step worked and the latest after_gate trial balance was built, balances, and agrees
    with the expected one in every account."""
    outcome = _new_outcome(case)
    started = clock()
    try:
        _run(client, case, outcome, sleep=sleep, clock=clock, job_timeout=job_timeout,
             analysis_timeout=analysis_timeout)
    except run_eval.ApiUnreachable as exc:
        outcome.update(error="api_unreachable", error_detail=str(exc))
    outcome["run_time_s"] = round(clock() - started, 1)
    outcome["matched"] = (outcome["error"] is None and outcome["is_balanced"] is True and not outcome["problems"]
                          and outcome["differences"] == [])
    return outcome


def _write_report(out_file: Path, results: list[dict], **meta) -> dict:
    report = {"date": date.today().isoformat(), **meta, "job_timeout_s": JOB_TIMEOUT,
              "summary": {"cases": len(results), "matched": sum(r["matched"] for r in results),
                          "failed": [r["case"] for r in results if not r["matched"]]},
              "cases": results}
    tmp = out_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(out_file)   # never leave a half-written report behind
    return report


def _verdict(outcome: dict) -> str:
    if outcome["matched"]:
        return "matches"
    if outcome["error"]:
        return f"{outcome['error']}" + (f": {outcome['error_detail']}" if outcome["error_detail"] else "")
    if outcome["problems"]:
        return f"trial balance not built: {'; '.join(outcome['problems'][:3])}"
    if outcome["is_balanced"] is not True:
        return "trial balance does not balance"
    return f"{len(outcome['differences'])} account(s) differ"


def run_all(client, cases: list[Path], out_file: Path, *, label: str, api: str, model: Optional[str] = None,
            sleep=time.sleep, clock=time.monotonic) -> dict:
    """Runs the cases one after another and saves the report after each, so an interrupted run keeps what it
    finished."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    meta = {"label": label, "api": api, "model": model}
    for n, case in enumerate(cases, start=1):
        print(f"[{n}/{len(cases)}] {case.name} ...", end=" ", flush=True)
        outcome = run_case(client, case, sleep=sleep, clock=clock)
        results.append(outcome)
        print(_verdict(outcome), f"({outcome['run_time_s']} s)", flush=True)
        _write_report(out_file, results, **meta)
    return _write_report(out_file, results, **meta)


def exit_code(report: dict) -> int:
    """0 only when there were cases and every one matched."""
    return 0 if report["cases"] and all(c["matched"] for c in report["cases"]) else 1


def _gate_text(case: dict) -> str:
    """The gate as the summary shows it: passed, the checks that failed, or n/a when it didn't run."""
    if not case["gate_checks"]:
        return "n/a"
    failing = [c["code"] for c in case["gate_checks"] if not c["passed"]]
    return f"failed {', '.join(failing)}" if failing else "passed"


def _print_summary(report: dict) -> None:
    print(f"\n{report['summary']['matched']} of {report['summary']['cases']} case(s) match.")
    for case in report["cases"]:
        print(f"  {case['case']}: {_verdict(case)}; gate {_gate_text(case)}; exceptions {case['exceptions']}; "
              f"fix rounds {case['fix_rounds']}; {case['run_time_s']} s")
        for diff in case["differences"] or []:
            print(f"      {diff['code']}: expected {diff['expected']}, got {diff['actual']} ({diff['difference']})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8085")
    parser.add_argument("--label", required=True, help="name for this run, e.g. gate1")
    parser.add_argument("--case", default="", help="only cases whose folder name contains this text")
    parser.add_argument("--overwrite", action="store_true", help="replace today's report for this label")
    args = parser.parse_args(argv)

    import httpx2

    cases = load_cases(JOBS_DIR, args.case)
    if not cases:
        print("No cases found.", file=sys.stderr)
        return 1
    out_file = RESULTS_DIR / f"{date.today().isoformat()}-jobs-{args.label}.json"
    if out_file.exists() and not args.overwrite:
        print(f"{out_file} already exists. Use --overwrite to replace it, or another --label.", file=sys.stderr)
        return 1
    # A fresh connection per request, as run_eval makes them: a long poll must not reuse a socket the API closed.
    with httpx2.Client(base_url=args.api, timeout=180,
                       limits=httpx2.Limits(max_keepalive_connections=0)) as client:
        try:
            health = run_eval._call(client.get, "/api/health", retries=1).json()
        except run_eval.ApiUnreachable:
            print(f"Cannot reach the LedgerSync API at {args.api}. Start it with: .venv/bin/python server.py",
                  file=sys.stderr)
            return 2
        problem = run_eval.check_ready(health)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        report = run_all(client, cases, out_file, label=args.label, api=args.api, model=health.get("model"))
    _print_summary(report)
    print(f"\nWrote {out_file}")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
