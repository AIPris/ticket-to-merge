"""Regression tests for skills/issue-loop/run_issues.py.

Run from the repository root:  python -m unittest discover -s tests

Nothing here talks to GitHub, Claude or a remote: every external call is mocked, and the supervisor's state
(logs, lock, status file) is redirected into a temporary directory.
"""
import contextlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "issue-loop" / "run_issues.py"
spec = importlib.util.spec_from_file_location("run_issues", SCRIPT)
run_issues = importlib.util.module_from_spec(spec)
sys.modules["run_issues"] = run_issues  # the ClaudeRun dataclass looks its module up here
spec.loader.exec_module(run_issues)


class SupervisorTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        restore_globals = mock.patch.dict(run_issues.__dict__)  # configure() rebinds many module globals
        restore_globals.start()
        self.addCleanup(restore_globals.stop)
        # A fresh install: neither the state folder nor the logs folder exists yet.
        run_issues.STATE_DIR = self.tmp / "state" / "issue-loop"
        run_issues.LOG_DIR = run_issues.STATE_DIR / "logs" / "repo"
        run_issues.LOCK_FILE = run_issues.STATE_DIR / "repo.lock"
        run_issues.STATUS_FILE = run_issues.STATE_DIR / "repo.status.json"
        run_issues.say = lambda msg="": None
        run_issues.set_status = lambda **fields: None

    def make_repo(self, config=None) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        if config is not None:
            self.write_config(repo, config)
        return repo

    @staticmethod
    def write_config(repo: Path, config: dict) -> None:
        (repo / run_issues.CONFIG_FILE).write_text(json.dumps(config), encoding="utf-8")


class LogDirectoryTests(SupervisorTestCase):
    """Issue #1: the logs folder must exist before anything writes into it."""

    def test_configure_creates_state_and_log_folders_on_a_fresh_install(self):
        repo = self.make_repo()
        run_issues.configure(str(repo))
        self.assertEqual(run_issues.LOG_DIR, run_issues.STATE_DIR / "logs" / "repo")
        self.assertTrue(run_issues.LOG_DIR.is_dir())

    def test_configure_creates_the_log_folder_when_only_the_state_folder_exists(self):
        run_issues.STATE_DIR.mkdir(parents=True)
        run_issues.configure(str(self.make_repo()))
        self.assertTrue(run_issues.LOG_DIR.is_dir())

    def test_configure_keeps_existing_logs(self):
        old_log = run_issues.STATE_DIR / "logs" / "repo" / "earlier.log.jsonl"
        old_log.parent.mkdir(parents=True)
        old_log.write_text("kept", encoding="utf-8")
        run_issues.configure(str(self.make_repo()))
        self.assertEqual(old_log.read_text(encoding="utf-8"), "kept")

    def test_pr_body_is_written_when_no_claude_session_ran_first(self):
        # A resumed branch with "review": false reaches ensure_pr without any Claude session.
        run_issues.configure(str(self.make_repo()))
        pr = {"number": 7, "url": "https://example.test/pull/7"}
        with mock.patch.object(run_issues, "find_open_pr", side_effect=[None, pr]), \
                mock.patch.object(run_issues, "gh") as gh, \
                mock.patch.object(run_issues, "change_message", return_value=("Subject", "")):
            self.assertEqual(run_issues.ensure_pr({"number": 3}, "agent/issue-3", "summary"), pr)
        body = (run_issues.LOG_DIR / "pr-body-3.md").read_text(encoding="utf-8")
        self.assertIn("Closes #3", body)
        self.assertIn("--body-file", gh.call_args.args)


def result_event(text, is_error=False):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error, "result": text}) + "\n"


class FiresOnStartTimer:
    """Stands in for threading.Timer: the session deadline passes the moment the timer starts."""

    def __init__(self, interval, callback):
        self.callback = callback

    def start(self):
        self.callback()

    def cancel(self):
        pass


class SessionTests(SupervisorTestCase):
    """Issue #5: a session whose deadline passed must never be accepted. Also the neighbouring outcomes."""

    ISSUE = {"number": 1, "title": "t", "url": "https://example.test/1", "body": "b"}

    def setUp(self):
        super().setUp()
        run_issues.LOG_DIR.mkdir(parents=True)
        run_issues.exe = lambda name: name

    @contextlib.contextmanager
    def fake_session(self, lines, *, code=0, timer=None):
        """Replace the claude process: it prints `lines`, then exits with `code`."""
        proc = mock.Mock(stdout=iter(lines), pid=4242)
        proc.wait.return_value = code
        with mock.patch.object(run_issues.subprocess, "Popen", return_value=proc), \
                mock.patch.object(run_issues, "kill_tree"), \
                mock.patch.object(run_issues.threading, "Timer", timer or run_issues.threading.Timer):
            yield

    def run_session(self, lines, **kwargs):
        with self.fake_session(lines, **kwargs):
            return run_issues._run_claude_once("prompt", label="test", max_turns=5, read_only=False)

    def test_a_successful_result_without_a_timeout_is_accepted(self):
        run = self.run_session([result_event("Fixed.\nRESULT: DONE")])
        self.assertIn("RESULT: DONE", run.result)

    def test_a_successful_result_after_the_timeout_fired_is_rejected(self):
        with self.assertRaisesRegex(run_issues.SupervisorError, "timed out"):
            self.run_session([result_event("Fixed.\nRESULT: DONE")], timer=FiresOnStartTimer)

    def test_a_reviewer_approval_after_the_timeout_fired_is_not_an_approval(self):
        report = "## Blocking findings\nNone.\n\nREVIEW: APPROVE\nRESULT: DONE"
        with self.fake_session([result_event(report)], timer=FiresOnStartTimer), \
                mock.patch.object(run_issues, "head_sha", return_value="abc"), \
                mock.patch.object(run_issues, "is_dirty", return_value=False):
            verdict, _ = run_issues.run_review(self.ISSUE, "base", "base", None, "review1")
        self.assertEqual(verdict, "FAILED")

    def test_a_nonzero_exit_still_fails(self):
        with self.assertRaisesRegex(run_issues.SupervisorError, "exit code 1"):
            self.run_session([result_event("RESULT: DONE")], code=1)

    def test_a_usage_limit_is_still_retried_later(self):
        limit = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 123}})
        with self.assertRaises(run_issues.UsageLimitReached) as caught:
            self.run_session([limit + "\n", result_event("limit", is_error=True)], code=1)
        self.assertEqual(caught.exception.resets_at, 123)

    def test_a_timeout_is_not_mistaken_for_a_usage_limit(self):
        limit = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 123}})
        with self.assertRaisesRegex(run_issues.SupervisorError, "timed out"):
            self.run_session([limit + "\n", result_event("limit", is_error=True)], code=1, timer=FiresOnStartTimer)


class ConfigValidationTests(SupervisorTestCase):
    """Issue #4: counts come from a committed file, so a bad value must stop the preflight, not skip CI."""

    COUNTS = {"ci_repair_attempts": 0, "local_repair_attempts": 0, "review_rounds": 0}
    TURN_LIMITS = ("worker_max_turns", "ci_repair_max_turns", "review_max_turns")

    def assert_rejected(self, repo, key, bad):
        self.write_config(repo, {key: bad})
        with self.subTest(key=key, value=bad), self.assertRaisesRegex(run_issues.SupervisorError, key):
            run_issues.configure(str(repo))

    def test_bad_counts_are_rejected_before_anything_runs(self):
        repo = self.make_repo()
        for key in self.COUNTS:
            for bad in (-1, 2.5, "3", True, False, None):
                self.assert_rejected(repo, key, bad)

    def test_bad_turn_limits_are_rejected(self):
        repo = self.make_repo()
        for key in self.TURN_LIMITS:
            for bad in (0, -5, 1.5, "10", True):
                self.assert_rejected(repo, key, bad)

    def test_allow_no_ci_defaults_to_off_and_can_be_switched_on(self):
        repo = self.make_repo()
        run_issues.configure(str(repo))
        self.assertIs(run_issues.ALLOW_NO_CI, False)
        self.write_config(repo, {"allow_no_ci": True})
        run_issues.configure(str(repo))
        self.assertIs(run_issues.ALLOW_NO_CI, True)

    def test_allow_no_ci_must_be_a_real_boolean(self):
        repo = self.make_repo()
        for bad in ("false", "yes", 1, 0, None):
            self.write_config(repo, {"allow_no_ci": bad})
            with self.subTest(value=bad), self.assertRaisesRegex(run_issues.SupervisorError, "allow_no_ci"):
                run_issues.configure(str(repo))

    def test_zero_is_a_valid_count(self):
        run_issues.configure(str(self.make_repo(self.COUNTS)))
        self.assertEqual((run_issues.CI_REPAIR_ATTEMPTS, run_issues.LOCAL_REPAIR_ATTEMPTS, run_issues.REVIEW_ROUNDS),
                         (0, 0, 0))

    def test_valid_values_and_defaults_are_kept(self):
        run_issues.configure(str(self.make_repo({"ci_repair_attempts": 5, "worker_max_turns": 7})))
        self.assertEqual((run_issues.CI_REPAIR_ATTEMPTS, run_issues.WORKER_MAX_TURNS), (5, 7))
        self.assertEqual((run_issues.LOCAL_REPAIR_ATTEMPTS, run_issues.REVIEW_ROUNDS), (2, 2))


class CiBudgetTests(SupervisorTestCase):
    """The repair budget only limits repairs: the first CI check always happens before a merge."""

    def run_pipeline(self, attempts, statuses):
        run_issues.CI_REPAIR_ATTEMPTS = attempts
        self.patched = {}
        for name in ("prepare_branch", "run_verify", "commit_all", "push_reviewed", "ensure_pr", "comment_review",
                     "wait_for_checks", "work_until_verified", "merge_pr", "finish_issue", "notify", "ahead_count",
                     "fetch_issue"):
            patcher = mock.patch.object(run_issues, name)
            self.patched[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.patched["fetch_issue"].return_value = {"number": 9, "title": "t", "url": "https://example.test/9"}
        self.patched["ahead_count"].return_value = 1  # a resumed branch: skips the first worker session
        self.patched["run_verify"].return_value = (True, "")
        self.patched["commit_all"].return_value = True
        self.patched["push_reviewed"].return_value = ("sha", "")
        self.patched["ensure_pr"].return_value = {"number": 4, "url": "https://example.test/pull/4"}
        self.patched["wait_for_checks"].side_effect = statuses
        run_issues.process_issue(9)

    def test_a_zero_budget_still_checks_ci_once_and_refuses_to_merge_a_failure(self):
        with self.assertRaises(run_issues.SupervisorError):
            self.run_pipeline(0, [("fail", ["tests"])])
        self.assertEqual(self.patched["wait_for_checks"].call_count, 1)
        self.patched["work_until_verified"].assert_not_called()
        self.patched["merge_pr"].assert_not_called()

    def test_a_zero_budget_merges_after_green_ci(self):
        self.run_pipeline(0, [("pass", [])])
        self.assertEqual(self.patched["wait_for_checks"].call_count, 1)
        self.patched["merge_pr"].assert_called_once()

    def test_a_positive_budget_is_the_first_check_plus_that_many_repairs(self):
        self.run_pipeline(2, [("fail", ["a"]), ("fail", ["a"]), ("pass", [])])
        self.assertEqual(self.patched["wait_for_checks"].call_count, 3)
        self.assertEqual(self.patched["work_until_verified"].call_count, 2)
        self.patched["merge_pr"].assert_called_once()

    def test_an_exhausted_budget_does_not_merge(self):
        with self.assertRaises(run_issues.SupervisorError):
            self.run_pipeline(1, [("fail", ["a"]), ("fail", ["a"])])
        self.assertEqual(self.patched["work_until_verified"].call_count, 1)
        self.patched["merge_pr"].assert_not_called()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


PASSING = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
FAILING = [{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": "https://example.test/run"}]


class CheckRegistrationTests(SupervisorTestCase):
    """Issue #3: an empty check list is only "no CI" when no workflow is known to run on pull requests."""

    PR_WORKFLOW = "name: CI\non: pull_request\njobs: {}\n"

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.workflows = self.repo / ".github" / "workflows"
        run_issues.REPO_DIR = self.repo
        run_issues.ALLOW_NO_CI = False
        self.clock = FakeClock()
        self.checks_appear_at, self.checks = float("inf"), PASSING
        for patcher in (
            mock.patch.object(run_issues, "time", types.SimpleNamespace(time=self.clock.time, sleep=self.clock.sleep)),
            mock.patch.object(run_issues, "gh_json", side_effect=self.pr_view),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def pr_view(self, *args, **kwargs):
        rollup = self.checks if self.clock.now >= self.checks_appear_at else []
        return {"state": "OPEN", "headRefOid": "sha", "statusCheckRollup": rollup}

    def add_workflow(self, text, name="ci.yml"):
        self.workflows.mkdir(parents=True, exist_ok=True)
        (self.workflows / name).write_text(text, encoding="utf-8")

    def wait(self, expect_checks=False):
        return run_issues.wait_for_checks(4, "sha", expect_checks)

    def test_a_pull_request_workflow_with_no_reported_checks_stops_instead_of_merging(self):
        self.add_workflow(self.PR_WORKFLOW)
        with self.assertRaisesRegex(run_issues.SupervisorError, "no checks"):
            self.wait()

    def test_every_pull_request_trigger_form_counts(self):
        forms = {
            "ci.yml": "on: pull_request\n",
            "ci.yaml": "on: pull_request\n",
            "list.yml": "on: [push, pull_request]\n",
            "target.yml": "on:\n  pull_request_target:\n    branches: [main]\n",
            "filtered.yml": "on:\n  pull_request:\n    paths: ['src/**']\n",
        }
        for name, text in forms.items():
            with self.subTest(workflow=name):
                shutil.rmtree(self.workflows, ignore_errors=True)
                self.clock.now = 0
                self.add_workflow(text, name)
                with self.assertRaisesRegex(run_issues.SupervisorError, "no checks"):
                    self.wait()

    def test_checks_that_register_late_are_still_waited_for(self):
        self.add_workflow(self.PR_WORKFLOW)
        self.checks_appear_at = 130  # well past the 90 s + settle time after which "none" used to be returned
        self.assertEqual(self.wait(), ("pass", []))

    def test_a_late_failure_is_reported_as_a_failure(self):
        self.add_workflow(self.PR_WORKFLOW)
        self.checks_appear_at, self.checks = 130, FAILING
        status, failures = self.wait()
        self.assertEqual(status, "fail")
        self.assertIn("tests (FAILURE)", failures[0])

    def test_workflows_that_never_run_on_pull_requests_mean_no_checks(self):
        self.add_workflow("name: Deploy\non:\n  push:\n    branches: [main]\n  schedule:\n    - cron: '0 3 * * *'\n")
        self.assertEqual(self.wait(), ("none", []))

    def test_a_comment_mentioning_pull_request_is_not_a_trigger(self):
        self.add_workflow("# not run on pull_request, only on push\non: push\n")
        self.assertEqual(self.wait(), ("none", []))

    def test_a_repository_without_workflows_has_no_checks(self):
        self.assertEqual(self.wait(), ("none", []))

    def test_allow_no_ci_restores_the_old_behaviour_for_filtered_workflows(self):
        self.add_workflow(self.PR_WORKFLOW)
        run_issues.ALLOW_NO_CI = True
        self.assertEqual(self.wait(), ("none", []))

    def test_checks_that_vanish_after_being_seen_still_stop(self):
        with self.assertRaisesRegex(run_issues.SupervisorError, "Checks existed before"):
            self.wait(expect_checks=True)


if __name__ == "__main__":
    unittest.main()
