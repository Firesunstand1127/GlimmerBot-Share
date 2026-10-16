"""Local-only setup for the share distribution. No login credentials are stored."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

APP_NAME = "MaaShanShuoShare"
DEFAULT_MUMU = r"C:\Program Files\Netease\MuMu Player 12"


def settings_path():
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        / APP_NAME
        / "share_settings.json"
    )


def defaults():
    return {
        "accounts": {},
        "server": {"id": "", "region": "", "name": ""},
        "device": "127.0.0.1:5557",
        "mumu_path": DEFAULT_MUMU,
        "mumu_index": 1,
        "galaxy_target": "",
    }


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("本地设置格式错误")
    if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", str(config.get("device", ""))):
        raise ValueError("设备地址需为 127.0.0.1:端口")
    port = int(config["device"].split(":")[1])
    if not 1 <= port <= 65535:
        raise ValueError("设备端口必须在1～65535之间")
    if not isinstance(config.get("mumu_path"), str) or not config["mumu_path"].strip():
        raise ValueError("请填写 MuMu 12 安装目录")
    index = config.get("mumu_index")
    if type(index) is not int or index < 0:
        raise ValueError("模拟器实例编号必须为非负整数")
    server = config.get("server")
    if not isinstance(server, dict) or not all(
        isinstance(server.get(k), str) and server[k].strip() for k in ("id", "region", "name")
    ):
        raise ValueError("请完整填写区服ID、分区和服务器名称")
    if not re.fullmatch(r"S[0-9]+", server["id"]):
        raise ValueError("区服ID格式示例：S1234")
    accounts = config.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("请至少配置一个账号")
    aliases, roles = set(), set()
    for key, account in accounts.items():
        if not re.fullmatch(r"account_[a-z0-9]+", key) or not isinstance(account, dict):
            raise ValueError("账号记录格式错误")
        if not all(
            isinstance(account.get(k), str) and account[k].strip()
            for k in ("label", "sdk_label", "role_name")
        ):
            raise ValueError("每个账号都需要 SDK 显示名和角色名")
        role = re.sub(r"[\W_丶]", "", account["role_name"])
        if not role or role in roles:
            raise ValueError("角色名不能为空或重复")
        roles.add(role)
        labels = account.get("sdk_labels", [account["sdk_label"]])
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(x, str) and x.strip() for x in labels)
        ):
            raise ValueError("OCR别名格式错误")
        normalized = {re.sub(r"\s+", "", x) for x in [account["sdk_label"], *labels]}
        if aliases & normalized:
            raise ValueError("不同账号的 SDK 显示名/OCR别名不能重复")
        aliases.update(normalized)
    if not isinstance(config.get("galaxy_target", ""), str):
        raise ValueError("星河目标应为文本")
    return config


def load_settings(*, required=False):
    path = settings_path()
    if not path.exists():
        if required:
            raise ValueError("请先双击程序完成本地设置，再运行命令行任务")
        return defaults()
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError, TypeError) as exc:
        raise ValueError(f"本地设置无法读取：{exc}") from exc


def save_settings(config):
    validate(config)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def pipeline_overrides(config):
    validate(config)
    labels = {
        label
        for a in config["accounts"].values()
        for label in [a["sdk_label"], *a.get("sdk_labels", [])]
    }
    pattern = "^(?:" + "|".join(re.escape(label) for label in sorted(labels)) + ")$"
    roles = "^(?:" + "|".join(re.escape(a["role_name"]) for a in config["accounts"].values()) + ")$"
    return {
        "TeamArenaHomeRoleFeature": {"expected": roles},
        "AccountServerSettingsScan": {"expected": "^" + re.escape(config["server"]["id"]) + "$"},
        "AccountSwitchListReady": {"expected": pattern},
        "AccountSwitchSelectAccount": {"expected": pattern},
        "LoginSavedAccountAlias": {"expected": pattern},
    }


def settings_dialog(parent=None):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
    )

    try:
        config = load_settings()
    except ValueError as exc:
        QMessageBox.warning(parent, "本地设置", str(exc))
        config = defaults()
    dialog = QDialog(parent)
    dialog.setStyleSheet(
        "QWidget { color:#202226; background:#f2f3f5; font-size:14px; }"
        "QLineEdit, QTableWidget { background:white; color:#202226; }"
        "QPushButton { background:#e6e7eb; color:#202226; padding:7px; }"
        "QHeaderView::section { background:#e6e7eb; color:#202226; }"
    )
    dialog.setWindowTitle("MaaShanShuo 分享版 · 本地设置")
    dialog.resize(820, 680)
    layout = QVBoxLayout(dialog)
    hint = QLabel(
        "填写游戏中已登录的账号信息，无需密码。所有设置只保存在本机。\n多个账号需在同一区服；更改后重启程序生效。"
    )
    hint.setWordWrap(True)
    layout.addWidget(hint)
    form = QFormLayout()
    fields = {}
    for key, title, value in (
        ("mumu_path", "MuMu 12 安装目录", config["mumu_path"]),
        ("mumu_index", "MuMu 实例编号", str(config["mumu_index"])),
        ("device", "ADB 设备地址", config["device"]),
        ("id", "区服ID（如 S1234）", config["server"]["id"]),
        ("region", "分区名称（游戏原文）", config["server"]["region"]),
        ("name", "服务器名称（游戏原文）", config["server"]["name"]),
        ("galaxy_target", "星河目标（选填）", config["galaxy_target"]),
    ):
        fields[key] = QLineEdit(value)
        form.addRow(title, fields[key])
    layout.addLayout(form)
    layout.addWidget(
        QLabel("账号列表：SDK显示名是登录浮窗中保存的昵称/掩码账号；角色名是主城左上角名称。")
    )
    table = QTableWidget(0, 3)
    table.setHorizontalHeaderLabels(["SDK 显示名", "角色名", "OCR别名（选填，逗号分隔）"])
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    layout.addWidget(table, 1)

    def add_account(key=None, account=None):
        account = account or {}
        row = table.rowCount()
        table.insertRow(row)
        values = [
            account.get("sdk_label", ""),
            account.get("role_name", ""),
            ",".join(x for x in account.get("sdk_labels", []) if x != account.get("sdk_label")),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, key or "account_" + uuid.uuid4().hex)
            table.setItem(row, column, item)

    for key, account in config["accounts"].items():
        add_account(key, account)
    if not table.rowCount():
        add_account()
    buttons = QHBoxLayout()
    add = QPushButton("添加账号")
    add.clicked.connect(lambda: add_account())
    remove = QPushButton("移除选中账号")
    remove.clicked.connect(
        lambda: table.removeRow(table.currentRow()) if table.currentRow() >= 0 else None
    )
    buttons.addWidget(add)
    buttons.addWidget(remove)
    buttons.addStretch()
    layout.addLayout(buttons)
    controls = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
    )
    controls.button(QDialogButtonBox.StandardButton.Save).setText("保存")
    controls.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")

    def save():
        try:
            updated = {
                k: fields[k].text().strip() for k in ("mumu_path", "device", "galaxy_target")
            }
            updated["mumu_index"] = int(fields["mumu_index"].text().strip())
            updated["server"] = {k: fields[k].text().strip() for k in ("id", "region", "name")}
            updated["accounts"] = {}
            for row in range(table.rowCount()):
                sdk, role, aliases = [table.item(row, col).text().strip() for col in range(3)]
                key = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                old = config["accounts"].get(key)
                if old and (
                    (old["sdk_label"], old["role_name"]) != (sdk, role)
                    or config["server"] != updated["server"]
                ):
                    key = "account_" + uuid.uuid4().hex
                updated["accounts"][key] = {
                    "label": sdk,
                    "sdk_label": sdk,
                    "role_name": role,
                    "sdk_labels": list(
                        dict.fromkeys(
                            [sdk, *[x.strip() for x in re.split("[,，]", aliases) if x.strip()]]
                        )
                    ),
                }
            save_settings(updated)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(dialog, "无法保存", str(exc))
            return
        dialog.accept()

    controls.accepted.connect(save)
    controls.rejected.connect(dialog.reject)
    layout.addWidget(controls)
    return dialog.exec() == QDialog.DialogCode.Accepted


def bootstrap():
    """Run before task modules import settings; CLI never opens a blocking setup dialog."""
    global _application
    configure = "--configure" in sys.argv
    try:
        load_settings(required=True)
        if not configure:
            return True
    except ValueError as exc:
        if any(arg.startswith("--") and arg != "--configure" for arg in sys.argv[1:]):
            for flag in ("--run-result", "--self-test"):
                if flag in sys.argv and sys.argv.index(flag) + 1 < len(sys.argv):
                    out = Path(sys.argv[sys.argv.index(flag) + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(str(exc), encoding="utf-8")
            return False
    from PySide6.QtWidgets import QApplication

    _application = QApplication.instance() or QApplication(sys.argv)
    accepted = settings_dialog()
    return accepted and not configure
