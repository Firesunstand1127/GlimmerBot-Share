"""Verify the real cleanup button uses temporary logs and preserves active evidence."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "ui"), str(ROOT / "ui/runtime")]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from app import AlasMainWindow, QApplication
from log_retention import EvidenceRecord
from PySide6.QtTest import QTest


class LogStorageUiTests(unittest.TestCase):
    def test_cleanup_button_and_usage_without_device(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.dict(os.environ, LOCALAPPDATA=temporary),
                patch("app.base_dir", return_value=root),
            ):
                window = AlasMainWindow(check_device_on_start=False)
                window.team_arena_timer.stop()
                window.show()
                try:
                    old = root / "logs/daily_activity/old/frame.png"
                    old.parent.mkdir(parents=True)
                    old.write_bytes(b"old")
                    os.utime(old, (time.time() - 4 * 86400,) * 2)
                    live = EvidenceRecord(root / "logs", "galaxy_temple", "A")
                    (live.folder / "frame.png").write_bytes(b"live")
                    settings = root / "MaaShanShuoShare/galaxy_guard_settings.json"
                    settings.parent.mkdir(exist_ok=True)
                    settings.write_text('{"interval_seconds": 2}', encoding="utf-8")
                    before = settings.read_bytes()
                    window.clean_logs_button.click()
                    for _ in range(200):
                        app.processEvents()
                        if "/ 1024 MB" in window.log_storage_label.text():
                            break
                        QTest.qWait(10)
                    self.assertFalse(old.exists())
                    self.assertTrue((live.folder / "frame.png").exists())
                    self.assertEqual(settings.read_bytes(), before)
                    self.assertIn("/ 1024 MB", window.log_storage_label.text())
                finally:
                    window.close()
                    app.processEvents()


if __name__ == "__main__":
    unittest.main()
