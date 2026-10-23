"""The client-level test set (eval/jobs/) and the Gate 1 runner: a job's trial balance scored against the one worked
out by hand, and the runner's flow through the API, driven by scripted answers. No database or model."""
import json
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from job_scoring import compare, net_by_account
import run_eval
import run_job_eval

EVAL_DIR = Path(__file__).parent.parent / "eval"


def test_net_by_account_is_debit_minus_credit_without_zeros():
    snapshot = {"lines": [{"code": "1200", "debit": "0.00", "credit": "72.00"},
                           {"code": "7502", "debit": "60.00", "credit": "0.00"},
                           {"code": "2201", "debit": "12.00", "credit": "0.00"},
                           {"code": "9998", "debit": "0.00", "credit": "0.00"}]}
    assert net_by_account(snapshot) == {"1200": D("-72.00"), "7502": D("60.00"), "2201": D("12.00")}


def test_compare_lists_every_account_that_differs():
    assert compare({"7502": D("60.00"), "1200": D("-72.00")}, {"7500": D("60.00"), "1200": D("-72.00")}) == [
        {"code": "7500", "expected": "0.00", "actual": "60.00", "difference": "60.00"},
        {"code": "7502", "expected": "60.00", "actual": "0.00", "difference": "-60.00"}]


def test_every_case_is_complete_and_its_expected_trial_balance_balances():
    for case in sorted((EVAL_DIR / "jobs").iterdir()):
        assert {"client.json", "period.json", "expected_trial_balance.json", "notes.md"} <= {p.name for p in case.iterdir()}
        assert any((case / "documents").iterdir())
        expected = json.loads((case / "expected_trial_balance.json").read_text())
        assert sum(D(v) for v in expected.values()) == 0, case.name


def test_net_by_account_keeps_two_places_and_adds_up_repeated_codes():
    snapshot = {"lines": [{"code": "7502", "debit": "60", "credit": "0"},
                           {"code": "7502", "debit": "0.5", "credit": "0.00"}]}
    assert {code: str(net) for code, net in net_by_account(snapshot).items()} == {"7502": "60.50"}


# ─── The runner, against scripted answers ──────────────────────────────────────

CLIENT = {"name": "Test Co Ltd", "business_type": "limited_company", "contact_name": "Test Contact",
          "contact_email": "test-co@example.com", "vat_registered": True}
PERIOD = {"period_start": "2026-09-01", "period_end": "2026-09-30"}
EXPECTED = {"1200": "-72.00", "2201": "12.00", "7502": "60.00"}
GATE = {"passed": False, "checks": [
    {"code": "trial_balance_builds", "passed": True, "detail": "", "row_ids": []},
    {"code": "no_suspense", "passed": False, "detail": "1 booked row is in 9998 Suspense.", "row_ids": [3]}]}


def lines(*rows):
    return [{"code": code, "name": "", "type": "", "debit": debit, "credit": credit} for code, debit, credit in rows]


RIGHT = lines(("1200", "0.00", "72.00"), ("7502", "60.00", "0.00"), ("2201", "12.00", "0.00"))
WRONG = lines(("1200", "0.00", "72.00"), ("7500", "60.00", "0.00"), ("2201", "12.00", "0.00"))


def snapshot(version, stage, rows, problems=()):
    return {"id": f"snap-{version}", "job_id": "job-1", "version": version, "stage": stage,
            "is_balanced": not problems, "lines": [] if problems else rows, "totals": {}, "problems": list(problems)}


class Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class FakeApi:
    """The API as the runner reaches it through run_eval._call, which the tests point at `call`: each request is
    answered from a script by its method and path, and kept in `calls`. `statuses` is what each poll of the agent
    job says, the last one repeated."""

    def __init__(self, statuses, snapshots=(), exceptions=(), job_error=None, readable=True):
        self.statuses, self.snapshots, self.exceptions = list(statuses), list(snapshots), list(exceptions)
        self.job_error, self.readable = job_error, readable
        self.calls: list[tuple[str, str, dict]] = []
        self._reads = 0

    # The runner passes these to _call (client.get and so on), and `call` answers in their place by their names.
    def get(self, *args, **kwargs):
        raise AssertionError("called without run_eval._call")

    def post(self, *args, **kwargs):
        raise AssertionError("called without run_eval._call")

    def delete(self, *args, **kwargs):
        raise AssertionError("called without run_eval._call")

    def _job(self, status):
        return {"id": "job-1", "client_id": 7, **PERIOD, "status": status,
                "stage": "classify" if status == "preparing" else "done", "fix_rounds": 2,
                "gate": GATE if status == "awaiting_review" else None,
                "error": self.job_error if status == "failed" else None}

    def call(self, method, path, retries=None, **kwargs):
        verb = method.__name__.upper()
        self.calls.append((verb, path, kwargs))
        if (verb, path) == ("GET", "/api/health"):
            return Resp(200, {"model_available": True, "model": "fake-model"})
        if (verb, path) == ("POST", "/api/clients"):
            return Resp(201, {"id": 7, **kwargs["json"]})
        if (verb, path) == ("POST", "/api/analyze"):
            self._reads += 1
            return Resp(202, {"job_id": f"read-{self._reads}", "status": "queued"})
        if verb == "GET" and path.startswith("/api/jobs/read-"):
            if not self.readable:
                return Resp(200, {"status": "failed", "error": {"code": "unreadable_file"}})
            return Resp(200, {"status": "succeeded", "result": {"transactions": [{"gross": "72.00"}], "upload_id": 1}})
        if (verb, path) == ("POST", "/api/clients/7/agent-jobs"):
            return Resp(202, self._job("preparing"))
        if (verb, path) == ("GET", "/api/agent-jobs/job-1"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return Resp(200, self._job(status))
        if (verb, path) == ("GET", "/api/agent-jobs/job-1/snapshots"):
            return Resp(200, self.snapshots)
        if (verb, path) == ("GET", "/api/agent-jobs/job-1/exceptions"):
            return Resp(200, self.exceptions)
        if (verb, path) == ("POST", "/api/agent-jobs/job-1/cancel"):
            return Resp(200, {**self._job("preparing"), "cancel_requested": True})
        raise AssertionError(f"unexpected {verb} {path}")

    def sent(self, verb, path):
        return [kwargs for v, p, kwargs in self.calls if (v, p) == (verb, path)]


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    """One case folder in a test set of its own, and a results folder; the runner polls without waiting."""
    case = tmp_path / "jobs" / "ltd-test"
    (case / "documents").mkdir(parents=True)
    (case / "documents" / "bank.csv").write_text("Date,Description,Amount\n01/09/2026,BT,-72.00\n")
    (case / "documents" / "note.txt").write_text("Expenses 01/09/2026\nTaxi £10.00\n")
    (case / "client.json").write_text(json.dumps(CLIENT))
    (case / "period.json").write_text(json.dumps(PERIOD))
    (case / "expected_trial_balance.json").write_text(json.dumps(EXPECTED))
    (case / "notes.md").write_text("Worked out by hand.\n")
    monkeypatch.setattr(run_job_eval, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(run_job_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_job_eval, "JOB_POLL_SECONDS", 0)
    monkeypatch.setattr(run_eval, "POLL_SECONDS", 0)
    return tmp_path


def run_main(api, jobs, monkeypatch):
    """Runs the runner's command line against the scripted API; its exit code and the case it reported."""
    monkeypatch.setattr(run_eval, "_call", api.call)
    code = run_job_eval.main(["--label", "test"])
    report = json.loads(next((jobs / "results").glob("*-jobs-test.json")).read_text())
    return code, report["cases"][0]


def test_a_case_whose_trial_balance_matches_passes(jobs, monkeypatch):
    # Only the latest after_gate snapshot is scored: never an after_posting one, nor an older after_gate one.
    api = FakeApi(["preparing", "preparing", "awaiting_review"],
                  snapshots=[snapshot(1, "after_posting", WRONG), snapshot(2, "after_gate", WRONG),
                             snapshot(3, "after_gate", RIGHT), snapshot(4, "after_posting", WRONG)],
                  exceptions=[{"id": "e1"}, {"id": "e2"}])
    code, case = run_main(api, jobs, monkeypatch)
    assert code == 0
    assert (case["case"], case["matched"], case["differences"], case["error"]) == ("ltd-test", True, [], None)
    assert (case["status"], case["snapshot_version"], case["exceptions"], case["fix_rounds"]) == (
        "awaiting_review", 3, 2, 2)
    assert case["gate_checks"] == [{"code": c["code"], "passed": c["passed"], "detail": c["detail"]}
                                   for c in GATE["checks"]]
    assert case["run_time_s"] >= 0
    # The client is added as client.json says, each document is read for it, and the job covers period.json.
    pages = {"X-LedgerSync": "1"}
    assert api.sent("POST", "/api/clients") == [{"json": CLIENT, "headers": pages}]
    reads = api.sent("POST", "/api/analyze")
    assert [r["data"]["client_id"] for r in reads] == ["7", "7"] and all(r["headers"] == pages for r in reads)
    assert reads[0]["files"]["file"][0] == "bank.csv" and reads[1]["data"]["text"].startswith("Expenses")
    assert api.sent("POST", "/api/clients/7/agent-jobs") == [{"json": PERIOD, "headers": pages}]


def test_a_case_whose_trial_balance_differs_fails_with_the_differences(jobs, monkeypatch):
    api = FakeApi(["awaiting_review"],
                  snapshots=[snapshot(1, "after_posting", RIGHT), snapshot(2, "after_gate", WRONG)])
    code, case = run_main(api, jobs, monkeypatch)
    assert code != 0
    assert case["matched"] is False and case["error"] is None
    assert case["differences"] == [
        {"code": "7500", "expected": "0.00", "actual": "60.00", "difference": "60.00"},
        {"code": "7502", "expected": "60.00", "actual": "0.00", "difference": "-60.00"}]


def test_a_trial_balance_that_could_not_be_built_fails_with_its_problems(jobs, monkeypatch):
    # Its totals are all 0.00 and it has no lines: the problems say why, so they are what is reported.
    problems = ["#3: This account isn't in the chart."]
    api = FakeApi(["awaiting_review"], snapshots=[snapshot(2, "after_gate", RIGHT, problems=problems)])
    code, case = run_main(api, jobs, monkeypatch)
    assert code != 0
    assert (case["matched"], case["problems"], case["is_balanced"]) == (False, problems, False)


def test_a_job_that_fails_fails_the_case(jobs, monkeypatch):
    api = FakeApi(["preparing", "failed"], job_error="The model could not be reached.")
    code, case = run_main(api, jobs, monkeypatch)
    assert code != 0
    assert (case["matched"], case["status"], case["error"]) == (False, "failed", "job_failed")
    assert case["error_detail"] == "The model could not be reached."
    assert api.sent("GET", "/api/agent-jobs/job-1/snapshots") == []   # nothing to score


def test_a_document_that_cannot_be_read_stops_the_case_before_the_job(jobs, monkeypatch):
    api = FakeApi(["awaiting_review"], readable=False)
    code, case = run_main(api, jobs, monkeypatch)
    assert code != 0
    assert (case["matched"], case["error"]) == (False, "analysis_failed")
    assert case["documents"][0] == {"name": "bank.csv", "error": "unreadable_file", "rows": 0}
    assert api.sent("POST", "/api/clients/7/agent-jobs") == []


def test_a_job_still_preparing_after_30_minutes_is_cancelled_and_fails(jobs, monkeypatch):
    now = [0.0]

    def sleep(seconds):     # time passes only when the runner waits
        now[0] += max(seconds, 60)

    api = FakeApi(["preparing"])
    monkeypatch.setattr(run_eval, "_call", api.call)
    report = run_job_eval.run_all(api, run_job_eval.load_cases(run_job_eval.JOBS_DIR), jobs / "results" / "t.json",
                                  label="t", api="http://test", sleep=sleep, clock=lambda: now[0])
    case = report["cases"][0]
    assert (case["matched"], case["status"], case["error"]) == (False, "preparing", "eval_timeout")
    assert now[0] >= 30 * 60 and case["run_time_s"] >= 30 * 60
    assert api.sent("POST", "/api/agent-jobs/job-1/cancel") == [{"headers": {"X-LedgerSync": "1"}}]
    assert run_job_eval.exit_code(report) != 0


def test_help_names_the_label_and_case_options():
    shown = subprocess.run([sys.executable, str(EVAL_DIR / "run_job_eval.py"), "--help"], capture_output=True,
                           text=True, check=True).stdout
    assert "--label" in shown and "--case" in shown
