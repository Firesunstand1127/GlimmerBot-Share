"""Exercise destructive retention only inside temporary log directories."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
from log_retention import EvidenceRecord, LogRetention, append_log, recorded, start_record


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "logs"
        self.root.mkdir()
        self.cleaner = LogRetention(self.root)

    def file(self, name, size=40, age=600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, (time.time() - age,) * 2)
        return path

    def record(self, name, status, *, account="A", feature="galaxy_temple", age=600, **extra):
        path = self.file(f"{feature}/{name}/frame.png", age=age).parent
        self.file(f"{feature}/{name}/frame.json", age=age)
        (path / "record.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "account": account,
                    "finished": time.time() - age,
                    "pid": os.getpid(),
                    **extra,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_success_keeps_latest_per_account_feature_and_failure(self):
        old = self.record("old", "success", age=900)
        latest = self.record("new", "success")
        other_account = self.record("B", "success", account="B")
        other_feature = self.record("daily", "success", feature="daily_activity")
        failure = self.record("failure", "failed")
        self.cleaner.cleanup()
        self.assertFalse(old.exists())
        for path in (latest, other_account, other_feature, failure):
            self.assertTrue(path.exists())

    def test_noop_does_not_replace_success(self):
        win = self.record("win", "success")
        absent = self.record("absent", "noop", age=900)
        latest = self.record("absent-new", "noop")
        self.cleaner.cleanup()
        self.assertTrue(win.exists())
        self.assertTrue(latest.exists())
        self.assertFalse(absent.exists())

    def test_last_three_diagnostics_and_three_days(self):
        paths = [
            self.record(str(i), status, age=600 + i * 10)
            for i, status in enumerate(["failed", "incomplete", "unknown", "exhausted", "failed"])
        ]
        expired = self.record("expired", "failed", account="B", age=4 * 86400)
        self.cleaner.cleanup()
        self.assertEqual([path.exists() for path in paths], [True, True, True, False, False])
        self.assertFalse(expired.exists())

    def test_live_and_pending_survive_age_and_quota(self):
        active = self.record("live", "active", age=4 * 86400)
        pending = self.record("battle", "incomplete", age=4 * 86400, pending=True)
        self.cleaner.limit = 1
        report = self.cleaner.cleanup()
        self.assertTrue(active.exists())
        self.assertTrue(pending.exists())
        self.assertTrue(report["over_limit"])

    def test_dead_process_active_is_incomplete_not_success(self):
        dead = self.record("dead", "active", age=4 * 86400)
        with patch("log_retention.process_alive", return_value=False):
            self.cleaner.cleanup()
        self.assertFalse(dead.exists())

    def test_quota_counts_text_and_removes_png_sidecar_together(self):
        picture = self.file("failures/old.png", size=100)
        sidecar = self.file("failures/old.json", size=30)
        text = self.file("framework/debug/maafw.bak.old.log", size=100, age=500)
        self.cleaner.limit = 120
        report = self.cleaner.cleanup()
        self.assertFalse(picture.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(text.exists())
        self.assertEqual(report["after"], 100)

    def test_legacy_running_session_is_protected_but_old_files_cleaned(self):
        self.cleaner.protect_legacy(os.getpid(), time.time() - 1000)
        active = self.file("galaxy_temple/current/frame.png", age=500)
        old = self.file("daily_activity/old/frame.png", age=4 * 86400)
        self.cleaner.cleanup()
        self.assertTrue(active.exists())
        self.assertFalse(old.exists())

    def test_dry_run_and_changed_file_preserve_data(self):
        path = self.file("failures/old.png", age=4 * 86400)
        before = path.read_bytes()
        report = self.cleaner.cleanup(dry_run=True)
        self.assertGreater(report["freed"], 0)
        self.assertEqual(path.read_bytes(), before)
        unit = self.cleaner._inventory(time.time())[0]
        path.write_bytes(b"new evidence")
        self.assertEqual(self.cleaner._remove(unit), (0, 0))
        self.assertTrue(path.exists())

    def test_settings_unknown_files_and_outside_paths_untouched(self):
        settings = self.file("framework/config/maa_option.json", age=4 * 86400)
        unknown = self.file("account_settings.json", age=4 * 86400)
        outside = Path(self.temp.name) / "account_ledger.json"
        outside.write_text("ledger", encoding="utf-8")
        self.cleaner.cleanup()
        self.assertTrue(settings.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse(self.cleaner._safe(outside))
        self.assertFalse(self.cleaner._safe(self.root))
        self.assertEqual(outside.read_text(), "ledger")

    def test_old_native_debug_migrates_without_touching_acceptance(self):
        native = self.file("debug/maafw.bak.old.log", age=4 * 86400)
        acceptance = self.file("acceptance-060/proof.png", age=4 * 86400)
        self.cleaner.cleanup()
        self.assertFalse(native.exists())
        self.assertTrue(acceptance.exists())

    def test_symlink_cannot_redirect_deletion(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        proof = outside / "proof.png"
        proof.write_bytes(b"preserve")
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside), str(self.root / "galaxy_temple"))
        else:
            (self.root / "galaxy_temple").symlink_to(outside, target_is_directory=True)
        self.cleaner.cleanup()
        self.assertEqual(proof.read_bytes(), b"preserve")

    def test_new_file_in_record_prevents_deletion(self):
        path = self.record("old", "failed", age=4 * 86400)
        unit = self.cleaner._inventory(time.time())[0]
        (path / "new-frame.png").write_bytes(b"new frame")
        self.assertEqual(self.cleaner._remove(unit), (0, 0))
        self.assertTrue((path / "frame.png").exists())

    def test_locked_picture_keeps_manifest_and_reports_error(self):
        path = self.record("old", "failed", age=4 * 86400)
        original = Path.unlink

        def unlink(file, *args, **kwargs):
            if file.name == "frame.png":
                raise PermissionError("locked picture")
            return original(file, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            report = self.cleaner.cleanup()
        self.assertTrue(report["errors"])
        self.assertTrue((path / "record.json").exists())
        self.assertTrue((path / "frame.png").exists())

    def test_log_rotation_is_bounded(self):
        path = self.root / "run-small.log"
        for number in range(30):
            append_log(path, f"line {number}\n", max_bytes=30, backups=2)
        files = list(self.root.glob("run-small.log*"))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(file.stat().st_size <= 30 for file in files))
        self.assertIn("line 29", path.read_text())

    def test_ui_cleanup_request_does_not_wait_for_busy_disk_worker(self):
        from log_retention import _LOCK

        acquired, release = threading.Event(), threading.Event()

        def hold():
            with _LOCK:
                acquired.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        acquired.wait(5)
        try:
            started = time.monotonic()
            self.cleaner.maintain(force=True)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join()

    def test_empty_round_cannot_remove_last_complete_success(self):
        old = self.record("win", "success")
        record = EvidenceRecord(self.root, "galaxy_temple", "A")
        record.finish("success")
        self.assertTrue(old.exists())
        self.assertFalse(record.folder.exists())

    def test_plain_return_unknown_cancel_incomplete_and_pending_protected(self):
        class Runner:
            def __init__(inner):
                inner.runtime = NS(log_dir=self.root)
                inner.account = "A"
                inner.pending = False
                start_record(inner, "galaxy_temple")

            @recorded
            def run(inner, error=None):
                (inner.folder / "proof.png").write_bytes(b"proof")
                if error:
                    raise error
                return True

        runner = Runner()
        runner.run()
        self.assertEqual(
            json.loads((runner.folder / "record.json").read_text())["status"], "unknown"
        )
        runner.pending = True
        with self.assertRaises(RuntimeError):
            runner.run(RuntimeError("unresolved"))
        data = json.loads((runner.folder / "record.json").read_text())
        self.assertEqual(data["status"], "failed")
        self.assertTrue(data["pending"])

    def test_galaxy_round_results_and_failed_confirmation(self):
        from galaxy_temple import GalaxyTempleRunner

        runtime = NS(log_dir=self.root)
        with patch("galaxy_temple.TARGET", "示例目标"):
            runner = GalaxyTempleRunner(runtime)
        runner.account = "A"
        for outcome, status in [("won", "success"), ("absent", "noop"), ("lost", "failed")]:

            def cycle(outcome=outcome):
                (runner.folder / "proof.png").write_bytes(b"proof")
                return outcome

            runner.cycle = cycle
            runner.run_round()
            data = json.loads((runner.folder / "record.json").read_text())
            self.assertEqual(data["status"], status)
        successful = [
            path
            for path in (self.root / "galaxy_temple").glob("*/record.json")
            if json.loads(path.read_text())["status"] == "success"
        ]
        runner.pending = True
        runner.cycle = lambda: (_ for _ in ()).throw(RuntimeError("confirmation failed"))
        with self.assertRaises(RuntimeError):
            runner.run_round()
        self.assertTrue(successful[0].exists())
        self.assertTrue(json.loads((runner.folder / "record.json").read_text())["pending"])


if __name__ == "__main__":
    unittest.main()
