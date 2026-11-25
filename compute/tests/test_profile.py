import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from neurogrid_node.profile import ContributorProfile, ProfileStore, WorkerStatus


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profile.json"
        self.store = ProfileStore(self.path)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "neurogrid_node.client", "config", "--profile", str(self.path), *args],
            text=True, capture_output=True, timeout=15,
        )

    def test_migrate_once_and_read_from_another_process(self):
        with (
            patch("neurogrid_node.profile.default_profile_path", return_value=self.path),
            patch("neurogrid_node.profile.legacy_settings", return_value={
                "site": "https://test.example/", "threads": "3", "duty": "55", "autostart": True,
            }) as legacy,
        ):
            self.assertEqual(ProfileStore().read(), ContributorProfile(
                site="https://test.example", threads=3, duty=55, autostart=True,
            ))
            self.assertEqual(ProfileStore().read().threads, 3)
            legacy.assert_called_once()
        result = self.run_cli("show")
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(result.stdout)
        self.assertEqual((settings["path"], settings["threads"], settings["duty"]), (str(self.path), 3, 55))
        self.assertNotIn("secret", settings)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_reject_invalid_updates_and_preserve_previous_settings(self):
        self.store.update({"threads": 1, "duty": 40})
        previous = self.path.read_bytes()
        result = self.run_cli("set", "--threads", "0", "--duty", "99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CPU threads must be between", result.stderr)
        for changes in (
            {"duty": True}, {"site": "https://user:password@example.com"},
            {"site": "https://example.com:90000"}, {"site": "https://example.com/?"},
            {"version": 2}, {"key": "secret"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.update(changes)
        self.assertEqual(self.path.read_bytes(), previous)
        with patch.object(Path, "replace", side_effect=OSError("write interrupted")), self.assertRaises(OSError):
            self.store.update({"duty": 42})
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertFalse(list(self.path.parent.glob(".profile-*")))

    def test_cli_partial_updates_and_pause_persist(self):
        self.store.update({"threads": 3})
        result = self.run_cli("set", "--duty", "30", "--pause")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = ProfileStore(self.path).read()
        self.assertTrue(saved.paused)
        self.assertEqual((saved.threads, saved.duty), (3, 30))
        result = self.run_cli("set", "--resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.store.read().paused)

    def test_worker_ownership_is_exclusive_and_released_on_exit(self):
        with self.store.worker():
            with self.assertRaisesRegex(ValueError, "worker ownership"):
                with ProfileStore(self.path).worker():
                    self.fail("Another worker already owns this profile")
            self.store.update({"duty": 30})
        with ProfileStore(self.path).worker():
            self.assertEqual(self.store.read().duty, 30)

    def test_status_reports_saved_and_observed_values_separately(self):
        self.store.update({"threads": 3, "duty": 40})
        command = [sys.executable, "-m", "neurogrid_node.client", "status", "--profile", str(self.path)]
        with self.store.worker():
            self.store.report_worker(WorkerStatus(
                state="computing", threads=1, duty=75, shard=0, last_heartbeat=1_000,
            ))
            result = subprocess.run(command, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertTrue(status["running"])
            self.assertEqual((status["saved"]["threads"], status["last_observed"]["threads"]), (3, 1))
            self.assertEqual(status["last_observed"]["last_heartbeat"], 1_000)
            self.assertEqual(ProfileStore(self.path.with_suffix(".other")).worker_status(), (False, None))
        self.assertFalse(self.store.worker_status()[0])
