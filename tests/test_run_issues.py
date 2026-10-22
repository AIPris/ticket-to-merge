"""Regression tests for skills/issue-loop/run_issues.py.

Run from the repository root:  python -m unittest discover -s tests

Nothing here talks to GitHub, Claude or a remote: every external call is mocked, and the supervisor's state
(logs, lock, status file) is redirected into a temporary directory.
"""
import contextlib
import importlib.util
import json
import subprocess
import sys
import tempfile
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
            (repo / run_issues.CONFIG_FILE).write_text(json.dumps(config), encoding="utf-8")
        return repo


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


if __name__ == "__main__":
    unittest.main()
