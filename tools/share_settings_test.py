"""Offline setup and recognition checks; synthetic identities, no device access."""

import copy
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from share_settings import (
    defaults,
    load_settings,
    pipeline_overrides,
    save_settings,
    settings_path,
    validate,
)


def sample():
    config = defaults()
    config["accounts"] = {
        "account_demo": {
            "label": "示例[用户]",
            "sdk_label": "示例[用户]",
            "sdk_labels": ["示例[用户]", "示例别名"],
            "role_name": "测试丶角色",
        }
    }
    config["server"] = {"id": "S1234", "region": "示例分区", "name": "示例服务器"}
    config["galaxy_target"] = "测试｜目标"
    return config


class ShareSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, LOCALAPPDATA=self.tmp.name)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_local_save_and_missing_setup(self):
        with self.assertRaises(ValueError):
            load_settings(required=True)
        save_settings(sample())
        self.assertEqual(load_settings(), sample())
        self.assertEqual(settings_path().parent.name, "MaaShanShuoShare")
        self.assertFalse((Path(self.tmp.name) / "MaaShanShuo").exists())

    def test_invalid_and_ambiguous_identity_rejected(self):
        for key, value in [
            ("device", "remote:5555"),
            ("device", "127.0.0.1:99999"),
            ("mumu_index", -1),
            ("accounts", {}),
            ("server", {}),
        ]:
            config = sample()
            config[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate(config)
        config = sample()
        config["accounts"]["account_second"] = copy.deepcopy(config["accounts"]["account_demo"])
        with self.assertRaises(ValueError):
            validate(config)

    def test_pipeline_exact_aliases_and_server(self):
        override = pipeline_overrides(sample())
        pattern = override["LoginSavedAccountAlias"]["expected"]
        self.assertIsNotNone(re.fullmatch(pattern, "示例[用户]"))
        self.assertIsNotNone(re.fullmatch(pattern, "示例别名"))
        self.assertIsNone(re.fullmatch(pattern, "其他示例[用户]"))
        self.assertIsNone(re.fullmatch(pattern, "示例用"))
        self.assertEqual(override["AccountServerSettingsScan"]["expected"], "^S1234$")
        role_pattern = override["TeamArenaHomeRoleFeature"]["expected"]
        self.assertIsNotNone(re.fullmatch(role_pattern, "测试丶角色"))
        self.assertIsNone(re.fullmatch(role_pattern, "测试丶其他角色"))

    def test_blank_target_never_matches_or_starts(self):
        import galaxy_temple

        with patch.object(galaxy_temple, "TARGET", ""):
            self.assertFalse(galaxy_temple.target_name(""))
            with self.assertRaises(ValueError):
                galaxy_temple.GalaxyTempleRunner(None)
        with patch.object(galaxy_temple, "TARGET", "测试｜目标"):
            self.assertTrue(galaxy_temple.target_name("测试丨目 标"))
            self.assertFalse(galaxy_temple.target_name("其他测试目标"))

    def test_unconfigured_cli_exits_before_runtime(self):
        output = Path(self.tmp.name) / "result.txt"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "ui/app.py"),
                "--galaxy-guard",
                "--run-result",
                str(output),
            ],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("本地设置", output.read_text(encoding="utf-8"))

    def test_first_run_dialog_save_and_cancel(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QTableWidget
        from share_settings import settings_dialog

        app = QApplication.instance() or QApplication([])

        def fill():
            dialog = app.activeModalWidget()
            values = [
                defaults()["mumu_path"],
                "1",
                "127.0.0.1:5557",
                "S1234",
                "示例分区",
                "示例服务器",
                "测试｜目标",
            ]
            for widget, value in zip(dialog.findChildren(QLineEdit), values):
                widget.setText(value)
            table = dialog.findChild(QTableWidget)
            for column, value in enumerate(["示例[用户]", "测试丶角色", "示例别名"]):
                table.item(0, column).setText(value)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).click()

        QTimer.singleShot(30, fill)
        self.assertTrue(settings_dialog())
        saved = load_settings()
        self.assertEqual(saved["server"], sample()["server"])
        self.assertEqual(next(iter(saved["accounts"].values()))["role_name"], "测试丶角色")
        before = settings_path().read_bytes()
        QTimer.singleShot(30, lambda: app.activeModalWidget().reject())
        self.assertFalse(settings_dialog())
        self.assertEqual(settings_path().read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
