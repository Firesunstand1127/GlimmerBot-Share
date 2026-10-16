from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path

if __name__ == "__main__":
    from share_settings import bootstrap

    if not bootstrap():
        sys.exit(2)

from accounts import AccountTaskLedger
from log_retention import EvidenceRecord, append_log, manager
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from storage import DailyProgress, read_json, write_json
from task_config import (
    ACCOUNT_PRESETS,
    ADB,
    APP_NAME,
    DAILY_ACCOUNT_KEYS,
    DEVICE,
    FIXED_SERVER,
    GALAXY_TARGET,
    GAME_ACTIVITY,
    GAME_PACKAGE,
    MUMU_INDEX,
    MUMU_PATH,
    PUBLIC_ENTRIES,
    SHARE_CONFIG,
    TASK_COMPLETION_REVISIONS,
    TASK_PRIORITY,
    TASKS,
)
from version import APP_VERSION

STYLE = """
QWidget { color: #e8eaf0; font-family: "Microsoft YaHei UI"; font-size: 14px; }
QMainWindow, QWidget#root { background: #17191f; }
QFrame#topbar { background: #202226; border-bottom: 1px solid #353941; }
QFrame#sidebar { background: #292c32; border-right: 1px solid #373b44; }
QFrame#card, QFrame#deviceCard, QFrame#logCard, QFrame#statusCard { background: #2b2e34; border: 1px solid #3b4049; border-radius: 2px; }
QFrame#card:hover { border-color: #7266ba; background: #2e3239; }
QLabel#title { font-size: 21px; font-weight: 700; }
QLabel#pageTitle { font-size: 24px; font-weight: 700; }
QLabel#sectionTitle { font-size: 17px; font-weight: 600; }
QLabel#muted { color: #9da4b2; }
QLabel#statusGood { color: #55d6a6; font-weight: 600; }
QLabel#statusBad { color: #ef6b73; font-weight: 600; }
QLabel#statusWarn { color: #f0b45c; font-weight: 600; }
QLabel#taskName { font-size: 15px; font-weight: 600; color: #e9ebf2; }
QLabel#taskTime { font-size: 12px; color: #aab1bf; }
QPushButton { background: #333843; border: 1px solid #444a57; border-radius: 3px; padding: 9px 16px; }
QPushButton:hover { background: #3d4350; }
QPushButton:pressed { background: #292d35; }
QPushButton#primary { background: #8172da; border-color: #9589e6; font-weight: 700; }
QPushButton#primary:hover { background: #9081e7; }
QPushButton#danger { background: #55323a; border-color: #864752; color: #ffb7be; }
QPushButton#nav { border: none; border-radius: 0; background: transparent; padding: 11px 15px; text-align: left; color: #c7cbd4; }
QPushButton#nav:hover { background: #333740; color: #ffffff; }
QPushButton#navActive { border: none; border-left: 3px solid #9180ed; border-radius: 0; background: #32363e; padding: 11px 12px; text-align: left; color: #9f93ef; font-weight: 700; }
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 20px; height: 20px; border: 1px solid #626979; border-radius: 4px; background: #202329; }
QCheckBox::indicator:checked { background: #8172da; border-color: #a496f0; image: none; }
QLineEdit { background: #202329; color: #e8eaf0; border: 1px solid #626979; border-radius: 3px; padding: 6px; selection-background-color: #6558b4; }
QPlainTextEdit, QTextEdit { background: #202329; border: none; border-radius: 1px; color: #cbd0db; selection-background-color: #6558b4; font-family: "Cascadia Mono", "Microsoft YaHei UI"; font-size: 12px; padding: 8px; }
QScrollArea { border: none; background: transparent; }
QWidget#scrollBody, QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { width: 10px; background: #20232a; margin: 0; }
QScrollBar::handle:vertical { background: #4b515e; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_dir(root: Path) -> Path:
    packaged = root / "resource"
    return packaged if packaged.exists() else root / "assets" / "resource"


def runtime_log_dir(root: Path) -> Path:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    folder = local / APP_NAME / "logs" if getattr(sys, "frozen", False) else root / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = local / APP_NAME / "logs"
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def app_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#8172da"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 15, 15)
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont("Microsoft YaHei UI", 25, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "闪")
    painter.end()
    return QIcon(pixmap)


class Bridge(QObject):
    log = Signal(str, str)
    finished = Signal(bool, str)
    device = Signal(bool, str)
    task_state = Signal(str, str, str)
    cli_done = Signal(int)
    galaxy_status = Signal(str)
    log_storage = Signal(object)


class RunCancelled(RuntimeError):
    """Raised when the UI requests cancellation of the active scheduler run."""


class ReadyUnavailable(RuntimeError):
    """Global navigation failed after bounded recovery; stop the shared queue."""


@dataclass
class TaskRunState:
    entry: str
    title: str
    enabled: bool = True
    status: str = "waiting"
    updated_at: str = ""
    queued_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    next_run: str = ""
    message: str = "等待调度"

    def touch(self) -> str:
        self.updated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.updated_at


class MaaRuntime:
    def __init__(self, root: Path, emit, state_emit=None):
        self.root = root
        self.log_dir = runtime_log_dir(root)
        self.run_log = self.log_dir / ("run-" + str(time.time_ns()) + ".log")
        self._emit_callback = emit
        self.emit = self._record
        self.state_emit = state_emit or (lambda entry, state, message="": None)
        self.tasker = None
        self.controller = None
        self._ladder_own_power = None
        self._arena_own_power = None
        self._sink = None
        self._entry_failed = None
        self._entry_started_at = 0.0
        self._cancel_event = threading.Event()
        self._completion_messages: dict[str, str] = {}
        self._completion_statuses: dict[str, str] = {}
        self._parent_deadline = None
        self._progress_store = None
        self._retention_enabled = False

    def _record(self, level, message):
        append_log(
            self.run_log,
            f"{dt.datetime.now().isoformat(timespec='seconds')} [{level}] {message}\n",
        )
        self._emit_callback(level, message)
        if self._retention_enabled:
            manager(self.log_dir).maintain()

    def _check_cancelled(self):
        if self._cancel_event.is_set():
            raise RunCancelled("用户已停止任务")
        if self._parent_deadline and time.monotonic() >= self._parent_deadline:
            raise RuntimeError("父任务超过总时限，已停止")

    def _progress(self):
        if getattr(self, "_progress_store", None) is None:
            self._progress_store = DailyProgress(AccountTaskLedger().current() or None)
        return self._progress_store

    def _defer(self, entry, message):
        self._completion_statuses[entry] = "deferred"
        self._completion_messages[entry] = message
        self.emit("INFO", message)

    def _wait_job(self, job, seconds=180):
        deadline = time.monotonic() + seconds
        try:
            while not job.done:
                self._check_cancelled()
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Maa任务超过{seconds}秒时限")
                self._cancel_event.wait(0.1)
            self._check_cancelled()
        except Exception:
            self.tasker.post_stop().wait()
            raise

    @staticmethod
    def _verify_attempts(before, after, label):
        if not 0 <= after < before:
            raise RuntimeError(f"{label}次数未减少：{before}→{after}，停止重复挑战")

    @staticmethod
    def _notification_names(details):
        """Yield pipeline/node names from MaaFramework's nested event payload."""
        if isinstance(details, dict):
            for key, value in details.items():
                if key in {"name", "entry"} and isinstance(value, str):
                    yield value
                else:
                    yield from MaaRuntime._notification_names(value)
        elif isinstance(details, list):
            for value in details:
                yield from MaaRuntime._notification_names(value)

    def _latest_error_screenshot(self):
        candidates = []
        for item in self.log_dir.rglob("*.png"):
            if "on_error" not in str(item.parent).lower() and not item.name.lower().startswith(
                "on_error"
            ):
                continue
            if item.stat().st_mtime >= self._entry_started_at:
                candidates.append(item)
        return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None

    def _save_failure_frame(self, entry):
        """Preserve the failed screen before navigation recovery changes it."""
        try:
            from PIL import Image

            frame = self.controller.post_screencap().get(wait=True)
            if frame is None:
                return
            account = getattr(self._progress_store, "account", None)
            record = EvidenceRecord(self.log_dir, "failures", account)
            folder = record.folder
            path = folder / f"{entry}-{time.time_ns()}.png"
            Image.fromarray(frame[:, :, ::-1]).save(path)
            record.update(feature_entry=entry)
            record.finish("failed")
            self.emit("INFO", f"失败现场：{path}")
        except Exception as exc:
            self.emit("WARN", f"失败现场保存未成功：{exc}")

    def _load(self):
        if self.tasker is not None and self.tasker.inited:
            return
        from share_settings import pipeline_overrides, validate

        validate(SHARE_CONFIG)
        from maa.controller import AdbController
        from maa.define import MaaAdbInputMethodEnum, MaaAdbScreencapMethodEnum
        from maa.resource import Resource
        from maa.tasker import Tasker, TaskerEventSink
        from maa.toolkit import Toolkit

        retention = manager(self.log_dir)
        retention.protect_running_apps()
        retention.maintain()
        self._retention_enabled = True
        # Native retention is isolated from plane and daily acceptance evidence.
        Toolkit.init_option(self.log_dir / "framework", {"logging": True})
        emit = self.emit
        runtime = self

        class Sink(TaskerEventSink):
            def on_tasker_task(self, tasker, noti_type, detail):
                if noti_type.name != "Succeeded":
                    return
                task_detail = tasker.get_task_detail(detail.task_id)
                if task_detail is None:
                    return
                recovery = next(
                    (node.name for node in task_detail.nodes if node.name.endswith("StopSafely")),
                    None,
                )
                if recovery:
                    runtime._entry_failed = recovery
                    emit("ERROR", f"任务由异常恢复节点结束：{recovery}")

            def on_raw_notification(self, tasker, msg, details):
                node = details.get("name") or details.get("entry") or details.get("task_id", "")
                names = tuple(runtime._notification_names(details))
                recovery = next((name for name in names if name.endswith("StopSafely")), None)
                if recovery:
                    runtime._entry_failed = recovery
                    emit("ERROR", f"进入异常恢复节点：{recovery}")
                if msg == "Node.PipelineNode.Starting":
                    emit("STEP", f"{msg}: {node}")
                elif msg in {"Node.Action.Succeeded", "Tasker.Task.Succeeded"}:
                    emit("OK", f"{msg}: {node}")
                elif msg in {"Node.PipelineNode.Failed", "Tasker.Task.Failed"}:
                    emit("ERROR", f"{msg}: {node}")

        self._sink = Sink()
        res = Resource()
        resources = resource_dir(self.root)
        res_job = res.post_bundle(str(resources))
        res_job.wait()
        if not res_job.succeeded:
            raise RuntimeError("MaaFramework 资源加载失败，请查看 logs 文件夹")

        if not res.override_pipeline(pipeline_overrides(SHARE_CONFIG)):
            raise RuntimeError("本地账号与区服识别配置应用失败")

        controller = AdbController(
            adb_path=str(ADB),
            address=DEVICE,
            # Strict MuMu IPC capture, equivalent to ALAS's nemu_ipc path.
            # Deliberately no ADB screencap fallback: a broken IPC setup must fail
            # visibly instead of silently falling back to a slow capture method.
            screencap_methods=MaaAdbScreencapMethodEnum.EmulatorExtras,
            input_methods=MaaAdbInputMethodEnum.Maatouch,
            config={
                "extras": {
                    "mumu": {
                        "enable": True,
                        "path": MUMU_PATH,
                        "index": MUMU_INDEX,
                    }
                }
            },
        )
        con_job = controller.post_connection()
        con_job.wait()
        if not con_job.succeeded:
            raise RuntimeError(f"无法连接设备 {DEVICE}")

        # MaaFramework uses a 720x1280 coordinate base. Pinning it explicitly
        # keeps every ROI/template/action consistent with the normalized assets.
        controller.set_screenshot_target_long_side(1280)
        self.controller = controller
        screenshot = controller.post_screencap().get(wait=True)
        if screenshot is None or getattr(screenshot, "size", 0) == 0:
            raise RuntimeError(f"设备 {DEVICE} 已连接，但 MuMu IPC 截图失败")
        height, width = screenshot.shape[:2]
        if (width, height) != (720, 1280):
            raise RuntimeError(f"MuMu IPC 截图尺寸异常：{width}×{height}，期望 720×1280")
        self.tasker = Tasker()
        self.tasker.add_sink(self._sink)
        self.tasker.bind(res, controller)
        if not self.tasker.inited:
            raise RuntimeError("MaaFramework Tasker 初始化失败")
        available_nodes = set(res.node_list)
        missing = sorted(entry for entry in PUBLIC_ENTRIES if entry not in available_nodes)
        if missing:
            raise RuntimeError("资源入口缺失：" + "、".join(missing))
        emit(
            "OK",
            f"MaaFramework 已连接：{DEVICE}（MuMu IPC 截图 {width}×{height}，MaaTouch）",
        )

    def _post_checked(self, entry: str, pipeline_override: dict | None = None):
        self._check_cancelled()
        self._entry_failed = None
        self._entry_started_at = time.time()
        job = self.tasker.post_task(entry, pipeline_override or {})
        battle_entries = {
            "CrossServerLadderResolveBattle",
            "TeamArenaBattleResolve",
            "SuperRankBattleResolve",
            "WorldArenaBattleResolve",
            "ElementTempleChallengeOne",
        }
        # The first three unskippable 15-round battles can exceed five minutes.
        timeout = (
            900
            if entry
            in {"TeamArenaBattleResolve", "SuperRankBattleResolve", "WorldArenaBattleResolve"}
            else (300 if entry in battle_entries else 180)
        )
        self._wait_job(job, timeout)
        self._check_cancelled()
        detail = job.get()
        if detail is not None:
            recovery = next(
                (node.name for node in detail.nodes if node.name.endswith("StopSafely")),
                None,
            )
            self._entry_failed = self._entry_failed or recovery
        if not job.succeeded or self._entry_failed:
            reason = self._entry_failed or "MaaFramework failure"
            if detail is not None:
                self.emit("INFO", "异常前节点：" + " → ".join(n.name for n in detail.nodes[-12:]))
            raise RuntimeError(f"Pipeline failed: {entry} ({reason})")
        return detail

    @staticmethod
    def _ocr_number(text: str):
        digits = re.sub(r"\D", "", text.replace(",", "").replace(".", ""))
        return int(digits) if digits else None

    def _ocr_results(self, entry: str, pipeline_override: dict | None = None):
        detail = self._post_checked(entry, pipeline_override)
        if detail is None:
            return []
        for node in reversed(detail.nodes):
            if node.name == entry and node.recognition is not None:
                return [
                    result
                    for result in node.recognition.all_results
                    if hasattr(result, "text") and hasattr(result, "box")
                ]
        return []

    def _read_ladder_attempts(self) -> int:
        results = self._ocr_results("CrossServerLadderAttemptsScan")
        values = [self._ocr_number(result.text) for result in results]
        values = [value for value in values if value is not None and 0 <= value <= 99]
        if not values:
            raise RuntimeError("Unable to read cross-server ladder challenge attempts")
        return values[-1]

    def _read_ladder_rank(self) -> int | None:
        """Read the current ladder rank when it is visible.

        A top-100 account cannot skip the normal challenge animation.  The
        one-click route is both faster and is the only deterministic route for
        that page variant, so rank is treated as a strategy hint rather than a
        hard requirement.
        """
        results = self._ocr_results("CrossServerLadderScan")
        for result in results:
            text = result.text.replace(" ", "").replace(",", "")
            match = re.search(r"排名[:：]?([0-9]{1,6})", text)
            if match is not None:
                return int(match.group(1))
        return None

    def _select_ladder_opponent(self):
        results = []
        for scan_index in range(6):
            results = self._ocr_results("CrossServerLadderScan")
            own_values = []
            for result in results:
                x, y, width, height = self._box_parts(result)
                center_y = y + height // 2
                normalized = result.text.replace(",", "").replace(".", "").strip()
                if (
                    65 <= center_y <= 115
                    and x < 200
                    and width < 220
                    and re.fullmatch(r"[0-9]{6,10}", normalized)
                ):
                    own_values.append(int(normalized))
            if own_values:
                self._ladder_own_power = max(own_values)
                break
            if self._ladder_own_power is not None:
                break
            if scan_index < 5:
                self.emit("INFO", "Own ladder power is covered; retrying OCR")
                time.sleep(0.8)

        def values_in_band(y_min: int, y_max: int):
            values = []
            for result in results:
                _, y, _, height = self._box_parts(result)
                center_y = y + height // 2
                if not (y_min <= center_y <= y_max):
                    continue
                value = self._ocr_number(result.text)
                if value is not None and value >= 1000:
                    values.append(value)
            return values

        if self._ladder_own_power is None:
            # The current-rank badge/chat overlay can temporarily cover the
            # account power.  Treat an unreadable value exactly like "no
            # confirmed weaker opponent": refresh at most three times and
            # then use the deterministic one-click fallback.  Failing the
            # whole queue here is both unnecessary and contrary to the
            # ladder strategy.
            self.emit(
                "WARN",
                "Own ladder power is covered; no weaker opponent can be confirmed",
            )
            return None
        own_power = self._ladder_own_power
        opponent_bands = [
            (510, 565),
            (600, 665),
            (745, 810),
            (910, 980),
            (1020, 1090),
        ]
        challenge_points = [
            (260, 453),
            (647, 540),
            (400, 690),
            (633, 857),
            (360, 967),
        ]
        opponents = []
        for band, point in zip(opponent_bands, challenge_points):
            band_values = values_in_band(*band)
            opponents.append((max(band_values) if band_values else None, point))

        readable = [value for value, _ in opponents if value is not None]
        self.emit("INFO", f"Ladder power: self={own_power}, opponents={readable}")
        for index, (power, point) in enumerate(opponents, start=1):
            if power is not None and power < own_power:
                return index, own_power, power, point
        return None

    def _run_cross_server_ladder(self):
        self._ladder_own_power = None
        self._post_checked("CrossServerLadderDaily")
        attempts = self._read_ladder_attempts()
        if self._parent_deadline is not None:
            # One fixed budget from the initial balance: each unskippable battle
            # may take 300s. Do not renew this deadline on every iteration.
            self._parent_deadline = time.monotonic() + 180 + max(0, attempts) * 320
            self.emit("INFO", f"天梯按剩余{attempts}次设置总时限：{180 + max(0, attempts) * 320}秒")
        if attempts <= 0:
            self.emit("OK", "Cross-server ladder: no free attempts remaining")
            self._post_checked("CrossServerLadderReturnHome")
            return
        rank = self._read_ladder_rank()
        if rank is not None and rank <= 100:
            self.emit(
                "INFO",
                f"Ladder rank={rank}; using one-click challenge because top-100 battles cannot be skipped",
            )
            self._post_checked("CrossServerLadderOneClick")
            if self._read_ladder_attempts() != 0:
                raise RuntimeError("天梯一键挑战后仍有免费次数，未记完成")
            self._post_checked("CrossServerLadderReturnHome")
            self.emit("OK", "Cross-server ladder completed through one-click challenge")
            return

        completed = 0
        refreshes = 0
        while True:
            attempts = self._read_ladder_attempts()
            if attempts <= 0:
                self.emit("OK", "Cross-server ladder: no free attempts remaining")
                break
            choice = self._select_ladder_opponent()
            if choice is None:
                if refreshes < 3:
                    refreshes += 1
                    self.emit("INFO", f"No weaker ladder opponent; refreshing ({refreshes}/3)")
                    self._post_checked("CrossServerLadderRefresh")
                    continue
                self.emit(
                    "WARN",
                    "No weaker opponent after three refreshes; consuming all free attempts with one-click challenge",
                )
                self._post_checked("CrossServerLadderOneClick")
                if self._read_ladder_attempts() != 0:
                    raise RuntimeError("天梯一键挑战后仍有免费次数，未记完成")
                break
            index, own_power, opponent_power, point = choice
            self.emit(
                "TASK",
                f"Ladder challenge {completed + 1}: row {index}, {opponent_power} < {own_power}",
            )
            click = self.controller.post_click(*point)
            click.wait()
            if not click.succeeded:
                raise RuntimeError(f"Failed to click ladder opponent row {index}")
            self._post_checked("CrossServerLadderResolveBattle")
            self._verify_attempts(attempts, self._read_ladder_attempts(), "天梯")
            completed += 1
        self._post_checked("CrossServerLadderReturnHome")
        self.emit("OK", f"Cross-server ladder completed: {completed} battles")

    @staticmethod
    def _box_parts(result):
        box = result.box
        if hasattr(box, "y"):
            return box.x, box.y, box.w, box.h
        return box[0], box[1], box[2], box[3]

    def _read_team_arena_attempts(self) -> int:
        results = self._ocr_results("TeamArenaAttemptsScan")
        values = []
        for result in results:
            value = self._ocr_number(result.text)
            if value is not None and 0 <= value <= 6:
                values.append(value)
        if not values:
            raise RuntimeError("Unable to read team-arena challenge attempts")
        return values[-1]

    def _team_arena_status(self) -> str | None:
        """Return a normal non-combat phase instead of treating it as failure."""
        results = self._ocr_results("TeamArenaStatusScan")
        text = "".join(result.text.replace(" ", "") for result in results)
        grouping = any(
            marker in text
            for marker in (
                "组队期",
                "距离挑战开启",
                "距离开启挑战",
                "虚位以待",
            )
        )
        if grouping:
            self.emit("INFO", f"Team arena is in grouping phase: {text}")
            return "grouping"
        closed = any(marker in text for marker in ("休赛期", "距离组队开放"))
        if closed:
            self.emit("INFO", f"Team arena is outside challenge phase: {text}")
            return "closed"
        return None

    def _team_arena_match_powers(self):
        results = self._ocr_results("TeamArenaMatchScan")
        own_bands = [(165, 235), (295, 365), (425, 495)]
        enemy_bands = [(680, 750), (810, 885), (940, 1015)]

        def powers_in_bands(bands):
            output = []
            for y_min, y_max in bands:
                band_values = []
                for result in results:
                    _, y, _, height = self._box_parts(result)
                    center_y = y + height // 2
                    if y_min <= center_y <= y_max:
                        value = self._ocr_number(result.text)
                        if value is not None and value >= 1_000_000:
                            band_values.append(value)
                output.append(max(band_values) if band_values else None)
            return output

        own = powers_in_bands(own_bands)
        enemy = powers_in_bands(enemy_bands)
        if any(value is None for value in own):
            raise RuntimeError(f"Unable to read all own team powers: {own}")
        return own, enemy

    def _accept_team_arena_match(self):
        from team_arena import best_order

        for _ in range(4):
            options = self._ocr_results("TeamArenaMatchOptionsScan")
            text = "".join(q.text.replace(" ", "") for q in options)
            if "保存调整" in text:
                return  # Reopening a pending challenge goes straight to formation.
            match = re.search(r"剩余刷新次数[:：]?(\d)", text)
            refreshes = int(match[1]) if match else 0
            own, enemy = self._team_arena_match_powers()
            _, wins = best_order(own, enemy)
            self.emit(
                "INFO",
                f"Team match: own={own}, enemy={enemy}, "
                f"estimated_wins={wins}/3, free_refreshes={refreshes}",
            )
            if wins < 2 and refreshes > 0:
                self._post_checked("TeamArenaRematch")
                continue
            self._post_checked("TeamArenaConfirmMatch")
            return
        raise RuntimeError("组队斗技免费重匹配次数未正确减少")

    def _team_arena_order_powers(self):
        results = self._ocr_results("TeamArenaOrderScan")
        from team_arena import enemy_names

        self._team_arena_order_names = enemy_names(results, self._box_parts)
        row_bands = [(390, 465), (700, 780), (1010, 1085)]
        own = []
        enemy = []
        for y_min, y_max in row_bands:
            own_values = []
            enemy_values = []
            for result in results:
                x, y, _, height = self._box_parts(result)
                center_y = y + height // 2
                if not (y_min <= center_y <= y_max):
                    continue
                value = self._ocr_number(result.text)
                if value is None or value < 1_000_000:
                    continue
                if x < 360:
                    own_values.append(value)
                else:
                    enemy_values.append(value)
            own.append(max(own_values) if own_values else None)
            enemy.append(max(enemy_values) if enemy_values else None)
        if any(value is None for value in own):
            raise RuntimeError(f"Unable to read own formation powers: {own}")
        return own, enemy

    def _arrange_team_arena(self):
        from team_arena import best_order, scout_order

        own, enemy = scout_order(self)
        desired, wins = best_order(own, enemy)
        self.emit(
            "INFO",
            f"Tian Ji order: own={own}, enemy={enemy}, target={desired}, estimated_wins={wins}/3",
        )

        current = list(own)
        for target_index, target_power in enumerate(desired):
            source_index = current.index(target_power, target_index)
            while source_index > target_index:
                boundary = source_index - 1
                entry = "TeamArenaSwap12" if boundary == 0 else "TeamArenaSwap23"
                self._post_checked(entry)
                current[boundary], current[boundary + 1] = current[boundary + 1], current[boundary]
                source_index -= 1

        verified_own, verified_enemy = self._team_arena_order_powers()
        if verified_own != desired:
            raise RuntimeError(
                f"Team order verification failed: expected={desired}, actual={verified_own}, enemy={verified_enemy}"
            )
        self.emit("OK", f"Team order verified: {verified_own}")

    def _run_team_arena(self):
        from team_arena import TeamArenaRunner

        TeamArenaRunner(self).run()

    def _read_super_rank_attempts(self) -> int:
        results = self._ocr_results("SuperRankAttemptsScan")
        values = []
        for result in results:
            normalized = result.text.replace(" ", "")
            match = re.search(r"(\d+)\s*/\s*\d+", normalized)
            if match is None:
                continue
            value = int(match.group(1))
            if 0 <= value <= 20:
                values.append(value)
        if not values:
            raise RuntimeError("Unable to read super-rank matching attempts")
        return values[-1]

    def _run_super_rank(self):
        self._post_checked("SuperRankDaily")
        self._post_checked("SuperRankClaimRewardOptional")
        status = self._ocr_results("SuperRankAvailabilityScan")
        closed = next((q.text for q in status if re.search(r"\d+[:：].*后开启", q.text)), None)
        if closed:
            self._post_checked("SuperRankReturnHome")
            self._defer("SuperRankDaily", "超凡段位尚未开放：" + closed)
            return
        attempts = self._read_super_rank_attempts()
        completed = 0
        if attempts > 0:
            self._post_checked("SuperRankEnterMatch")
        while attempts > 0:
            self.emit(
                "TASK",
                f"Super rank challenge {completed + 1}; attempts before matching={attempts}",
            )
            self._post_checked("SuperRankFindAndChallenge")
            self._post_checked("SuperRankBattleResolve")
            completed += 1
            before = attempts
            attempts = self._read_super_rank_attempts()
            self._verify_attempts(before, attempts, "超凡")
        self.emit("OK", "Super rank: no free matching attempts remaining")
        self._post_checked("SuperRankReturnHome")
        self.emit("OK", f"Super rank completed: {completed} battles")

    def _read_world_arena_attempts(self) -> int:
        results = self._ocr_results("WorldArenaAttemptsScan")
        values = []
        for result in results:
            normalized = result.text.replace(" ", "")
            match = re.search(r"(\d+)\s*/\s*\d+", normalized)
            if match is None:
                continue
            value = int(match.group(1))
            if 0 <= value <= 20:
                values.append(value)
        if not values:
            raise RuntimeError("Unable to read world-arena matching attempts")
        return values[-1]

    def _run_world_arena(self):
        # The mode is only open on Monday, Wednesday, Friday and Sunday.
        # Treat a closed day as a normal skip instead of navigating into the
        # neighbouring Season Glory tab and reporting a false task failure.
        if dt.datetime.now().isoweekday() not in (1, 3, 5, 7):
            self._defer("WorldArenaDaily", "世界竞技场今日未开放")
            return
        self._post_checked("WorldArenaDaily")
        self._post_checked("WorldArenaEnsureAutoSkip")
        attempts = self._read_world_arena_attempts()
        completed = 0
        while attempts > 0:
            self.emit(
                "TASK",
                f"World arena challenge {completed + 1}; attempts before matching={attempts}",
            )
            self._post_checked("WorldArenaStartAndChallenge")
            self._post_checked("WorldArenaBattleResolve")
            completed += 1
            before = attempts
            attempts = self._read_world_arena_attempts()
            self._verify_attempts(before, attempts, "世界竞技场")
        self.emit("OK", "World arena: no free matching attempts remaining")
        self._post_checked("WorldArenaReturnHome")
        self.emit("OK", f"World arena completed: {completed} battles")

    def _arena_power_state(self):
        """Read our power and the three visible arena opponents."""
        results = self._ocr_results("ArenaPowerScan")
        own_candidates = []
        opponent_bands = [(360, 445), (480, 570), (605, 695)]
        opponents = []

        for result in results:
            x, y, width, height = self._box_parts(result)
            center_y = y + height // 2
            value = self._ocr_number(result.text)
            if (
                x < 360
                and center_y <= 140
                and width < 320
                and value is not None
                and 100_000 <= value <= 9_999_999_999
            ):
                # Some themes merge the crossed-swords icon with the number
                # (for example "X 20410458").  Extract digits instead of
                # requiring the OCR text to be numeric-only.
                own_candidates.append(value)

        for y_min, y_max in opponent_bands:
            values = []
            for result in results:
                _, y, _, height = self._box_parts(result)
                center_y = y + height // 2
                if not (y_min <= center_y <= y_max):
                    continue
                value = self._ocr_number(result.text)
                if value is not None and value >= 1_000:
                    values.append(value)
            opponents.append(max(values) if values else None)

        if own_candidates:
            self._arena_own_power = max(own_candidates)
        if self._arena_own_power is None:
            raise RuntimeError("Unable to read own arena power")
        own_power = self._arena_own_power
        self.emit("INFO", f"Arena power: self={own_power}, opponents={opponents}")
        return own_power, opponents

    def _read_home_combat_power(self) -> int:
        """Read the account combat power while it is visible on main city.

        The normal arena opponent page does not display our combat power.  The
        old implementation tried to recover it from that page and therefore
        aborted before the first battle.  Capture it before entering arena and
        keep it for all refresh comparisons in this run.
        """
        results = self._ocr_results("ArenaHomePowerScan")
        values = []
        for result in results:
            value = self._ocr_number(result.text)
            if value is not None and 100_000 <= value <= 9_999_999_999:
                values.append(value)
        if not values:
            raise RuntimeError("Unable to read combat power on main city")
        value = max(values)
        self.emit("INFO", f"Arena home combat power={value}")
        return value

    def _select_arena_opponent(self):
        challenge_points = [(575, 387), (575, 513), (575, 640)]
        last_opponents = [None, None, None]
        # Read the initial list, then make at most three refresh attempts.
        # A weaker opponent is preferred, but the daily must not deadlock when
        # matchmaking only offers stronger accounts.
        for refresh_index in range(4):
            own_power, opponents = self._arena_power_state()
            last_opponents = opponents
            for index, (power, point) in enumerate(zip(opponents, challenge_points), start=1):
                if power is not None and power < own_power:
                    return index, own_power, power, point, True
            if refresh_index < 3:
                self.emit(
                    "INFO",
                    f"Arena: no lower-power opponent; refresh {refresh_index + 1}/3",
                )
                self._post_checked("ArenaRefresh")
        for index, (power, point) in enumerate(zip(last_opponents, challenge_points), start=1):
            if power is not None:
                self.emit(
                    "WARN",
                    "Arena: no lower-power opponent after three refreshes; "
                    f"falling back to row {index} ({power})",
                )
                return index, own_power, power, point, False
        self.emit(
            "WARN",
            "Arena: opponent powers remain unreadable after three refreshes; "
            "falling back to the first row",
        )
        return 1, own_power, None, challenge_points[0], False

    def _click_arena_opponent(self, battle_number: int):
        choice = self._select_arena_opponent()
        index, own_power, opponent_power, point, is_lower = choice
        comparison = (
            f"{opponent_power} < {own_power}"
            if is_lower
            else f"fallback opponent={opponent_power}, self={own_power}"
        )
        self.emit(
            "TASK",
            f"Arena battle {battle_number}: row {index}, {comparison}",
        )
        self._progress().put("arena", pending=True, completed=battle_number - 1)
        click = self.controller.post_click(*point)
        click.wait()
        if not click.succeeded:
            raise RuntimeError(f"Failed to click arena opponent row {index}")

    def _arena_game_completed(self):
        from daily_activity import DailyActivityRunner

        daily = DailyActivityRunner(self)
        daily.open()
        for _ in range(7):
            daily.panel()
            task = next((t for t in daily.objectives() if t["title"] == "参与3次竞技场挑战"), None)
            if task:
                match = re.fullmatch(r"([0-3])\s*/\s*3", task["progress"])
                count = (
                    3
                    if task["claimed"] or task["action"] == "提交"
                    else int(match[1])
                    if match
                    else None
                )
                if count is not None:
                    daily.evidence("arena-objective")
                    self._post_checked("ResourceCycleReturnHome")
                    return count
            job = self.controller.post_swipe(460, 940, 460, 700, 550)
            self._wait_job(job, 10)
            self._cancel_event.wait(0.5)
        raise RuntimeError("竞技场游戏任务进度未读取，停止追加挑战")

    def _run_arena(self):
        """Complete exactly three battles with skip enabled."""
        progress = self._progress()
        record = progress.get("arena")
        if record.get("completed", 0) >= 3 and record.get("verified_revision") == 2:
            self.emit("OK", "竞技场今日3次已核验完成")
            return
        completed = self._arena_game_completed()
        if record.get("pending") and completed != record.get("completed", 0) + 1:
            raise RuntimeError("上次竞技场点击结果未确认，保留现场，避免重复消耗挑战券")
        progress.put("arena", completed=completed, pending=False, verified_revision=2)
        if completed >= 3:
            return
        self._arena_own_power = None
        # Do not assume the global Ready gate left us on the selected main-city
        # tab.  This also recovers safely when a previous task ended on Attack.
        self._post_checked("ResourceCycleReturnHome")
        self._arena_own_power = self._read_home_combat_power()
        self._post_checked("ArenaDaily")
        # The checkbox persists between runs. Verify it before the first click;
        # an unchecked box is clicked and re-checked by the pipeline.
        self._post_checked("ArenaEnsureSkip")
        for _ in range(completed, 3):
            self._click_arena_opponent(completed + 1)
            self._post_checked("ArenaResolveSkippedBattle")
            actual = self._arena_game_completed()
            if actual != completed + 1:
                raise RuntimeError(f"竞技场挑战进度未增加：{completed}/3→{actual}/3，停止重复挑战")
            completed = actual
            progress.put("arena", pending=False, completed=completed, verified_revision=2)
            if completed < 3:
                self._post_checked("ArenaDaily")
                self._post_checked("ArenaEnsureSkip")

        self._post_checked("ResourceCycleReturnHome")
        self.emit("OK", f"Arena completed: {completed} battles and returned to Ready")

    def _read_voyage_intel(self) -> int:
        """Read spendable voyage intel below the voyage icon on the idle page."""
        results = self._ocr_results("VoyageIdleValueScan")
        values = []
        for result in results:
            value = self._ocr_number(result.text)
            if value is not None and 0 <= value <= 99_999:
                values.append(value)
        if not values:
            raise RuntimeError("Unable to read spendable voyage intel on idle page")
        value = values[-1]
        self.emit("INFO", f"Voyage spendable intel={value}")
        return value

    def _run_voyage(self):
        """Drain voyage intel with one verified merchant action per iteration.

        Each merchant pass accepts one visible epic/legendary/60-diamond task
        before it is allowed to refresh.  The pass then returns to the idle
        page, where the real spendable value is visible.  This intentionally
        avoids the merchant's ``x/27000`` cumulative-progress display.
        """
        self._post_checked("VoyageEnsureIdlePage")
        stagnant = 0
        previous = None
        for cycle in range(1, 51):
            intel = self._read_voyage_intel()
            if intel < 2000:
                self.emit("OK", f"Voyage complete: spendable intel={intel} < 2000")
                return
            if previous == intel:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 8:
                raise RuntimeError(
                    "Voyage intel did not change after eight bounded passes; "
                    "stopped to prevent an uncontrolled refresh loop"
                )
            previous = intel
            self.emit("TASK", f"Voyage merchant pass {cycle}; intel before={intel}")
            self._post_checked("VoyageDaily")
            self._post_checked("VoyageReturnIdlePage")
        raise RuntimeError("Voyage exceeded 50 bounded merchant passes")

    def _read_basic_summon_tickets(self) -> int:
        results = self._ocr_results("SummonThreeBasicTicketCountScan")
        values = [self._ocr_number(result.text) for result in results]
        values = [value for value in values if value is not None and 0 <= value <= 99999]
        if not values:
            raise RuntimeError("Unable to read basic summon ticket count")
        return values[-1]

    def _run_daily_summon(self):
        """Free advanced, free basic, then one ticket-funded basic summon."""
        progress = self._progress()
        if progress.get("daily_summon").get("done"):
            return
        self._post_checked("ResourceCycleReturnHome")
        self._post_checked("SummonThreeEnter")

        self._post_checked("SummonThreeSelectAdvanced")
        advanced = self._post_checked("SummonThreeAdvancedFreeOptional")
        advanced_free = bool(
            advanced and any(node.name == "SummonThreeAdvancedFreePull" for node in advanced.nodes)
        )
        self.emit(
            "OK" if advanced_free else "INFO",
            "Advanced free summon completed"
            if advanced_free
            else "Advanced free summon unavailable; skipped safely",
        )

        self._post_checked("SummonThreeSelectBasic")
        basic = self._post_checked("SummonThreeBasicFreeOptional")
        basic_free = bool(
            basic and any(node.name == "SummonThreeBasicFreePull" for node in basic.nodes)
        )
        self.emit(
            "OK" if basic_free else "INFO",
            "Basic free summon completed"
            if basic_free
            else "Basic free summon unavailable; continuing with ticket summon",
        )

        tickets = self._read_basic_summon_tickets()
        ticket = progress.get("basic_ticket")
        if ticket.get("pending"):
            if tickets != ticket["before"] - 1:
                raise RuntimeError("上次基础券消耗结果未确认，不重复召唤")
            progress.put("basic_ticket", pending=False, done=True)
            ticket = progress.get("basic_ticket")
        if ticket.get("done"):
            self.emit("OK", "今日基础券召唤已核验，不重复消耗")
        elif tickets <= 0:
            self.emit("WARN", "No basic summon ticket is available; diamond summon is forbidden")
        else:
            self.emit("TASK", f"Basic summon ticket count={tickets}; spending exactly one ticket")
            progress.put("basic_ticket", pending=True, before=tickets)
            self._post_checked("SummonThreeBasicTicketPull")
            # The result resolver must return to the basic summon page.  Verify
            # the on-screen ticket balance instead of treating a successful
            # pipeline callback as proof that the draw actually completed.
            remaining = self._read_basic_summon_tickets()
            if remaining != tickets - 1:
                raise RuntimeError(
                    f"Basic summon ticket verification failed: before={tickets}, after={remaining}"
                )
            progress.put("basic_ticket", pending=False, done=True)
            self.emit(
                "OK",
                f"Basic ticket summon verified: {tickets} -> {remaining}",
            )

        self._post_checked("SummonThreeReturnHome")
        progress.put("daily_summon", done=True)
        self.emit("OK", "Three-summon routine completed and returned to Ready")

    def _read_abyss_number(self, entry: str, label: str, maximum: int = 99) -> int:
        results = self._ocr_results(entry)
        values = [self._ocr_number(result.text) for result in results]
        values = [value for value in values if value is not None and 0 <= value <= maximum]
        if not values:
            raise RuntimeError(f"Unable to read abyss {label}")
        return values[-1]

    def _read_abyss_cost(self) -> tuple[int, int]:
        results = self._ocr_results("AbyssSweepCostScan")
        values = []
        for result in results:
            normalized = result.text.replace(" ", "")
            match = re.search(r"(\d+)\s*/\s*(\d+)", normalized)
            if match is not None:
                values.append((int(match.group(1)), int(match.group(2))))
        if not values:
            raise RuntimeError("Unable to read abyss stamina cost")
        return values[-1]

    def _run_abyss_dungeon(self):
        self._post_checked("AbyssDungeonDaily")
        stamina = self._read_abyss_number("AbyssStaminaScan", "remaining stamina")
        self.emit("INFO", f"Abyss remaining stamina={stamina}")
        if stamina <= 0:
            self.emit("OK", "Abyss dungeon: no stamina remaining")
            self._post_checked("AbyssReturnHome")
            return

        self._post_checked("AbyssOpenSweep")
        self._post_checked("AbyssClickMax")
        count = self._read_abyss_number("AbyssSweepCountScan", "sweep count")
        available, cost = self._read_abyss_cost()
        if not (count == stamina == available == cost):
            raise RuntimeError(
                "Abyss Max verification failed: "
                f"page_stamina={stamina}, sweep_count={count}, cost={available}/{cost}"
            )
        self.emit(
            "OK",
            f"Abyss Max verified: {count} sweeps consume all {stamina} remaining stamina",
        )
        self._post_checked("AbyssExecuteSweep")
        self._post_checked("AbyssConfirmResult")
        self._post_checked("AbyssReturnHome")
        self.emit("OK", f"Abyss dungeon completed: {count} sweeps")

    @staticmethod
    def _detail_has_node(detail, name: str) -> bool:
        return detail is not None and any(node.name == name for node in detail.nodes)

    def _read_element_temple_attempts(self) -> int:
        results = self._ocr_results("ElementTempleAttemptsScan")
        values = []
        for result in results:
            normalized = result.text.replace(" ", "")
            match = re.search(r"(\d+)\s*/\s*10", normalized)
            if match is not None:
                value = int(match.group(1))
                if 0 <= value <= 10:
                    values.append(value)
        if not values:
            raise RuntimeError("Unable to read element-temple challenge attempts")
        return values[-1]

    def _element_temple_floor_state(self) -> dict:
        results = self._ocr_results("ElementTempleFloorScan")
        challenges = []
        sweeps = []
        floors = []
        for result in results:
            x, y, width, height = self._box_parts(result)
            center_y = y + height // 2
            normalized = result.text.replace(" ", "").strip()
            if normalized == "挑战":
                challenges.append((center_y, result))
                continue
            if normalized == "扫荡":
                sweeps.append((center_y, result))
                continue
            if x < 180 and re.fullmatch(r"(?:[1-9]|1[0-2])", normalized):
                floors.append((center_y, int(normalized)))

        def floor_for(button_y: int):
            if not floors:
                return None
            floor_y, floor = min(floors, key=lambda item: abs(item[0] - button_y))
            return floor if abs(floor_y - button_y) <= 95 else None

        challenge_floors = [floor_for(y) for y, _ in challenges]
        sweep_floors = [floor_for(y) for y, _ in sweeps]
        challenge_floors = [value for value in challenge_floors if value is not None]
        sweep_floors = [value for value in sweep_floors if value is not None]
        if sweep_floors:
            highest = max(sweep_floors)
        elif challenge_floors:
            highest = max(0, min(challenge_floors) - 1)
        else:
            highest = 0
        return {
            "challenge": bool(challenges),
            "sweep": bool(sweeps),
            "highest": highest,
            "challenge_floors": challenge_floors,
            "sweep_floors": sweep_floors,
        }

    def _element_temple_battle_once(self) -> bool:
        detail = self._post_checked("ElementTempleChallengeOne")
        if self._detail_has_node(detail, "ElementTempleBattleSuccessStop"):
            return True
        if self._detail_has_node(detail, "ElementTempleBattleFailureStop"):
            return False
        raise RuntimeError("Element-temple battle ended without a success/failure result")

    def _element_temple_consume_sweeps(self, maximum: int | None = None) -> int:
        consumed = 0
        while maximum is None or consumed < maximum:
            before = self._read_element_temple_attempts()
            if before <= 0:
                break
            state = self._element_temple_floor_state()
            if not state["sweep"]:
                self.emit(
                    "WARN", "Element temple: no cleared floor is available for fallback sweep"
                )
                break
            self.emit(
                "TASK",
                f"Element temple fallback sweep {consumed + 1}; attempts before sweep={before}",
            )
            self._post_checked("ElementTempleSweepOne")
            after = self._read_element_temple_attempts()
            if after != before - 1:
                raise RuntimeError(
                    f"Element-temple sweep verification failed: before={before}, after={after}"
                )
            consumed += 1
        return consumed

    def _element_temple_advance(
        self,
        success_limit: int | None = None,
        sweep_when_blocked: bool = True,
    ) -> tuple[int, bool]:
        successes = 0
        blocked = False
        while success_limit is None or successes < success_limit:
            before = self._read_element_temple_attempts()
            if before <= 0:
                break

            state = self._element_temple_floor_state()
            if not state["challenge"]:
                blocked = True
                self.emit(
                    "INFO",
                    "Element temple has no new challenge button; using the highest visible cleared floor",
                )
                if sweep_when_blocked:
                    self._element_temple_consume_sweeps()
                break

            self.emit(
                "TASK",
                (
                    f"Element temple first-clear "
                    f"attempt {successes + 1}; attempts before battle={before}"
                ),
            )
            success = self._element_temple_battle_once()
            if success:
                successes += 1
                expected_after = before - 1

                # Do not click "challenge next" here.  On the live client that
                # button may immediately consume another attempt and run the
                # next battle before control returns to Python.  The old fast
                # path therefore observed 6 -> 4 attempts after one loop and
                # incorrectly treated a pair of successful battles as failure.
                # Returning to the floor list makes every loop atomic: exactly
                # one battle, one attempt, then a fresh screen-state check.
                self._post_checked("ElementTempleResolveSuccessFinal")

                after = self._read_element_temple_attempts()
                if after != expected_after:
                    raise RuntimeError(
                        "Element-temple success did not consume exactly one free attempt: "
                        f"before={before}, after={after}"
                    )
                continue

            self._post_checked("ElementTempleResolveFailure")
            after = self._read_element_temple_attempts()
            if after != before:
                raise RuntimeError(
                    "Element-temple failure unexpectedly changed attempts: "
                    f"before={before}, after={after}"
                )
            blocked = True
            self.emit(
                "WARN",
                "Element temple new floor failed; free attempt was preserved and retries are disabled",
            )
            if sweep_when_blocked:
                self._element_temple_consume_sweeps()
            break
        return successes, blocked

    @staticmethod
    def _element_temple_entry(name: str) -> str:
        entries = {
            "water": "ElementTempleSelectWater",
            "light": "ElementTempleSelectLight",
            "fire": "ElementTempleSelectFire",
            "dark": "ElementTempleSelectDark",
            "wind": "ElementTempleSelectWind",
        }
        return entries[name]

    def _select_element_temple(self, name: str):
        self._post_checked(self._element_temple_entry(name))
        self._post_checked("ElementTempleRelocateChat")

    def _find_best_element_temple(self) -> tuple[str | None, dict[str, int]]:
        progress = {}
        best_name = None
        best_floor = -1
        for name in ("water", "light", "fire", "dark", "wind"):
            self._select_element_temple(name)
            state = self._element_temple_floor_state()
            progress[name] = state["highest"]
            self.emit("INFO", f"Element temple progress: {name}={state['highest']}")
            if state["sweep"] and state["highest"] > best_floor:
                best_name = name
                best_floor = state["highest"]
            self._post_checked("ElementTempleReturnMap")
        return best_name, progress

    def _run_element_temple(self):
        weekday = dt.datetime.now().isoweekday()
        self._post_checked("ElementTempleOpenMap")

        if weekday <= 5:
            day_names = {1: "water", 2: "light", 3: "fire", 4: "dark", 5: "wind"}
            name = day_names[weekday]
            self.emit("INFO", f"Element temple weekday strategy: {name}")
            self._post_checked("ElementTempleSelectOpenAuto")
            self._post_checked("ElementTempleRelocateChat")
            attempts = self._read_element_temple_attempts()
            if attempts > 0:
                self._element_temple_advance(sweep_when_blocked=True)

        elif weekday == 6:
            self.emit("INFO", "Element temple Saturday strategy: advance two floors in each temple")
            for name in ("water", "light", "fire", "dark", "wind"):
                if self._read_element_temple_attempts() <= 0:
                    break
                self._select_element_temple(name)
                successes, blocked = self._element_temple_advance(
                    success_limit=2,
                    sweep_when_blocked=False,
                )
                self.emit(
                    "INFO",
                    f"Saturday temple {name}: first-clears={successes}, blocked={blocked}",
                )
                self._post_checked("ElementTempleReturnMap")

            if self._read_element_temple_attempts() > 0:
                best_name, progress = self._find_best_element_temple()
                if best_name is not None:
                    self.emit(
                        "INFO",
                        f"Saturday fallback: sweep {best_name}; progress={progress}",
                    )
                    self._select_element_temple(best_name)
                    self._element_temple_consume_sweeps()

        else:
            self.emit("INFO", "Element temple Sunday strategy: sweep the highest cleared temple")
            best_name, progress = self._find_best_element_temple()
            if best_name is None:
                self.emit("WARN", f"Sunday strategy found no cleared temple; progress={progress}")
            else:
                self.emit("INFO", f"Sunday selected {best_name}; progress={progress}")
                self._select_element_temple(best_name)
                self._element_temple_consume_sweeps()

        self._post_checked("ElementTempleReturnHome")
        self.emit("OK", "Element temple completed and returned to Ready")

    def _run_idle_income_cycle(self):
        """Drain voyage intel before claiming the current idle-income chest."""
        if self._progress().get("income").get("done"):
            return
        self.emit("STEP", "挂机收益：先清空远航值到 2000 以下")
        self._run_voyage()
        self._post_checked("VoyageReturnIdlePage")
        self.emit("STEP", "挂机收益：确认快捷领取并领取宝箱")
        self._post_checked("IdleIncomeClaimDaily")
        self._progress().put("income", done=True)
        self._post_checked("ResourceCycleReturnHome")

    def _run_fast_battle_voyage_cycle(self):
        """Run the verified voyage/income/fast-battle/voyage resource order."""
        self.emit("STEP", "快速战斗远航：第一次清空远航值")
        self._run_voyage()
        self._post_checked("VoyageReturnIdlePage")
        if not self._progress().get("income").get("done"):
            self.emit("STEP", "快速战斗远航：领取挂机收益")
            self._post_checked("IdleIncomeClaimDaily")
            self._progress().put("income", done=True)
        self.emit("STEP", "快速战斗远航：领取收益后再次清空远航值")
        self._run_voyage()
        self._post_checked("VoyageReturnIdlePage")
        self.emit("STEP", "快速战斗远航：用完快速战斗次数并清空新增远航值")
        self._post_checked("FastBattleVoyageDaily")
        self._run_voyage()
        self._post_checked("ResourceCycleReturnHome")

    def _execute_entry(self, entry: str):
        """Run one concrete Maa entry while preserving Python strategies."""
        checkpointed = entry in {
            "DailySummonDaily",
            "DailyShopRefreshDaily",
            "FastBattleVoyageDaily",
            "IdleIncomeClaimDaily",
        }
        if checkpointed and self._progress().get("entry:" + entry).get("done"):
            self.emit("OK", f"{entry} 今日步骤已完成，跳过重复执行")
            return
        special = {
            "DailyInitialClaim": self._run_activity_checkpoint,
            "TournamentPredictionDaily": self._run_predictions,
            "RankingLikesDaily": self._run_ranking_likes,
            "VoyageDaily": self._run_voyage,
            "DailySummonDaily": self._run_daily_summon,
            "ArenaDaily": self._run_arena,
            "DailyArenaDaily": self._run_arena,
            "CrossServerLadderDaily": self._run_cross_server_ladder,
            "TeamArenaDaily": self._run_team_arena,
            "SuperRankDaily": self._run_super_rank,
            "WorldArenaDaily": self._run_world_arena,
            "AbyssDungeonDaily": self._run_abyss_dungeon,
            "ElementTempleDaily": self._run_element_temple,
            "DomainSpiritDaily": self._run_domain_spirit,
            "IdleIncomeClaimDaily": self._run_idle_income_cycle,
            "FastBattleVoyageDaily": self._run_fast_battle_voyage_cycle,
            "PlaneExpeditionDaily": self._run_plane_expedition,
            "DailyShopRefreshDaily": self._run_spirit_shop,
            "DailyFinalClaim": self._run_final_claim,
        }
        runner = special.get(entry)
        if runner is not None:
            runner()
        else:
            self._post_checked(entry)
        if checkpointed:
            self._progress().put("entry:" + entry, done=True)
        from daily_activity import CHECKPOINT_ENTRIES

        if getattr(self, "_activity_tracking", False) and entry in CHECKPOINT_ENTRIES:
            try:
                self._run_activity_checkpoint()
            except RunCancelled:
                raise
            except Exception as exc:
                self._save_failure_frame("ActivityAfter" + entry)
                self.emit("WARN", f"{entry}业务已完成，活跃核账留待最终检查：{exc}")

    def _run_activity_checkpoint(self):
        from daily_activity import DailyActivityRunner

        DailyActivityRunner(self).run()

    def _run_final_claim(self):
        from daily_activity import DailyActivityRunner

        activity, claimed = DailyActivityRunner(self).run(final=True)
        status = "success" if activity >= 100 and 100 in claimed else "deferred"
        message = f"活跃度{activity}/100；已核验宝箱：{claimed}"
        if status == "deferred":
            message += "；活跃不足，未点击未达标宝箱，等待未完成任务"
        self._final_activity_result = (status, message)
        self.emit("OK" if status == "success" else "INFO", message)

    def _run_predictions(self):
        from home_reminders import check
        from predictions import ENTRY

        account = AccountTaskLedger().current()
        message = check(self, account, force=True)
        if message:
            self._completion_messages[ENTRY] = message
            AccountTaskLedger().record(account, ENTRY, "success", message)
        else:
            self._defer(ENTRY, "主城暂无竞猜提示，下一次运行继续检查")
            AccountTaskLedger().record(account, ENTRY, "deferred", self._completion_messages[ENTRY])

    def _run_spirit_shop(self):
        from spirit_shop import SpiritShopRunner

        SpiritShopRunner(self).run()

    def _run_ranking_likes(self):
        from ranking_likes import ENTRY, RankingLikesRunner

        results = RankingLikesRunner(self).run()
        message = "；".join(f"{kind}：{status}" for kind, status in results.items())
        self._completion_messages[ENTRY] = message
        if any(not status.startswith("核验") for status in results.values()):
            self._defer(ENTRY, message)

    def _run_domain_spirit(self):
        from domain_spirit import DomainSpiritRunner

        DomainSpiritRunner(self).run()

    def _run_plane_expedition(self):
        from plane_expedition import PlaneExpeditionRunner

        ledger = AccountTaskLedger()
        account_key = ledger.current()
        if account_key not in DAILY_ACCOUNT_KEYS:
            raise RuntimeError("位面征战需要已验证的 SDK 账号；请先选择账号")
        marker = ledger.plane_cycle(account_key)
        if marker.get("status") in {"success", "exhausted"}:
            status = marker["status"]
            message = marker.get("message") or "本账号位面征战本周期已结束，等待重置"
        else:
            self._post_checked("PlaneExpeditionDaily")
            status, message = PlaneExpeditionRunner(
                self, account_key, ledger.plane_store.path, allow_revive=True
            ).run()
            if status not in {"success", "exhausted"}:
                raise RuntimeError(message)
        self._completion_statuses["PlaneExpeditionDaily"] = status
        self._completion_messages["PlaneExpeditionDaily"] = message
        self.emit("OK" if status == "success" else "WARN", message)

    def _ensure_ready(self, reason: str = "Ready check", allow_restart: bool = True):
        """Reach a clean main-city screen with soft recovery before one restart.

        Ordinary page-state mismatches are first handled through Maa UI nodes.
        A force-stop is reserved for a repeated Ready failure and is performed
        at most once, so a harmless optional-page miss cannot cause three
        unexplained game restarts.
        """
        bootstrap_error = None
        for attempt in range(1, 3):
            self._check_cancelled()
            self._entry_failed = None
            self._entry_started_at = time.time()
            self.emit("TASK", f"{reason} (soft {attempt}/2)")
            job = self.tasker.post_task("GameBootstrap")
            timed_out = False
            try:
                self._wait_job(job, 60)
            except RunCancelled:
                raise
            except RuntimeError as exc:
                self._check_cancelled()  # Parent deadline must still stop recovery.
                timed_out = True
                self._entry_failed = str(exc)
                self._save_failure_frame("ReadyTimeout")
            # post_stop() makes the in-flight Maa job finish unsuccessfully.
            # Treat that as an intentional cancellation before recovery logic
            # can mistake it for a Ready failure and restart the game.
            self._check_cancelled()
            detail = job.get()
            if detail is not None:
                recovery = next(
                    (node.name for node in detail.nodes if node.name.endswith("StopSafely")),
                    None,
                )
                self._entry_failed = self._entry_failed or recovery
            if job.succeeded and not self._entry_failed and not timed_out:
                self.emit("READY", "Game is on the clean main screen")
                return

            bootstrap_error = self._entry_failed or "MaaFramework returned failure"
            if attempt == 1:
                self.emit(
                    "WARN",
                    f"Ready soft check failed: {bootstrap_error}; trying Maa UI return-home recovery",
                )
                self._entry_failed = None
                recovery = self.tasker.post_task("ResourceCycleReturnHome")
                try:
                    self._wait_job(recovery, 30)
                except RunCancelled:
                    raise
                except RuntimeError as exc:
                    self._check_cancelled()
                    self.emit("WARN", f"Return-home recovery failed: {exc}")

        if allow_restart:
            self.emit(
                "WARN",
                f"Ready still unavailable after soft recovery: {bootstrap_error}; restarting game once",
            )
            result = subprocess.run(
                [str(ADB), "-s", DEVICE, "shell", "am", "force-stop", GAME_PACKAGE],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to restart game: {result.stderr.strip()}")
            self._cancel_event.wait(1.5)
            self._check_cancelled()
            self._entry_failed = None
            self._entry_started_at = time.time()
            self.emit("TASK", f"{reason} (single restart recovery)")
            job = self.tasker.post_task("GameBootstrap")
            try:
                self._wait_job(job, 60)
            except RunCancelled:
                raise
            except RuntimeError as exc:
                self._check_cancelled()
                self._entry_failed = str(exc)
            self._check_cancelled()
            detail = job.get()
            recovery = (
                None
                if detail is None
                else next(
                    (node.name for node in detail.nodes if node.name.endswith("StopSafely")),
                    None,
                )
            )
            if job.succeeded and recovery is None and not self._entry_failed:
                self.emit("READY", "Game is on the clean main screen after one restart")
                return
            bootstrap_error = self._entry_failed or recovery or "MaaFramework returned failure"

        screenshot = self._latest_error_screenshot()
        self._save_failure_frame("ReadyUnavailable")
        suffix = f"; screenshot: {screenshot}" if screenshot else ""
        raise ReadyUnavailable(f"Unable to reach Ready: {bootstrap_error}{suffix}")

    @staticmethod
    def _normalized_account_text(text: str) -> str:
        return re.sub(r"\s+", "", text or "")

    def _current_sdk_account(self) -> str:
        results = self._ocr_results("AccountSwitchSdkCurrentScan")
        known = {}
        for key, item in ACCOUNT_PRESETS.items():
            for label in item.get("sdk_labels", [item["sdk_label"]]):
                known[self._normalized_account_text(label)] = key
        for result in results:
            normalized = self._normalized_account_text(result.text)
            if normalized in known:
                return known[normalized]
        texts = [getattr(result, "text", "") for result in results]
        raise RuntimeError(f"Unable to identify the current saved account: {texts}")

    def _select_sdk_account_from_open_list(self, account_key: str):
        """Click an explicitly whitelisted OCR row in the expanded SDK list.

        Maa's OCR node can return the account text reliably, but target-specific
        `expected` filtering is inconsistent in the packaged runtime.  Match in
        Python and click only the exact alias box; never fall back to a row or
        coordinate because another saved account must not be touched.
        """
        target = ACCOUNT_PRESETS[account_key]
        aliases = {
            self._normalized_account_text(label)
            for label in target.get("sdk_labels", [target["sdk_label"]])
        }
        texts = []
        # Expanded cards retain their previous scroll position. Start at the
        # top, then scan full visible labels; never select a clipped row.
        self.controller.post_swipe(450, 658, 450, 835, 400).wait()
        self._cancel_event.wait(0.5)
        for page in range(5):
            self._check_cancelled()
            results = self._ocr_results("AccountSwitchListScanAll")
            texts.extend(getattr(result, "text", "") for result in results)
            for result in results:
                normalized = self._normalized_account_text(result.text)
                is_target = normalized in aliases
                x, y, w, h = self._box_parts(result)
                if not is_target or y < 640 or y + h > 885:
                    continue
                self._check_cancelled()
                click = self.controller.post_click(int(x + w / 2), int(y + h / 2))
                click.wait()
                if not click.succeeded:
                    raise RuntimeError(f"Failed to click whitelisted SDK account {account_key}")
                self._cancel_event.wait(0.8)
                return
            if page < 4:
                self.controller.post_swipe(450, 824, 450, 658, 500).wait()
                self._cancel_event.wait(0.5)
        raise RuntimeError(
            f"Whitelisted SDK account {account_key!r} was not found in list: {texts}"
        )

    def switch_account(self, account_key: str):
        """Switch to one explicitly allowed saved SDK account.

        No credentials are stored or entered here.  The SDK account card is
        selected visually and the ordinary GameBootstrap state machine handles
        the agreement/start page, popups and final Ready verification.
        """
        if account_key not in ACCOUNT_PRESETS:
            raise ValueError(f"Unknown account preset: {account_key}")
        self._check_cancelled()
        self._load()
        target = ACCOUNT_PRESETS[account_key]
        self.emit("TASK", f"Switch account: {target['label']}")
        try:
            # An interrupted previous switch may already be on the SDK page.
            # Probe it first without clicking through a possible game popup.
            current = self._current_sdk_account()
        except Exception:
            # Account switching starts from a genuinely clean home screen.
            # Background home anchors remain visible through modal popups, so
            # opening the SDK before this cleanup would only click behind them.
            self._ensure_ready("Ready before account switch", allow_restart=True)
            self._post_checked("AccountSwitchOpenSdk")
            current = self._current_sdk_account()
        if current != account_key:
            self._check_cancelled()
            self._post_checked("AccountSwitchExpandAccountList")
            self._select_sdk_account_from_open_list(account_key)
            selected = self._current_sdk_account()
            if selected != account_key:
                raise RuntimeError(f"SDK selected {selected!r}, expected {account_key!r}")
            current = selected
        else:
            self.emit("INFO", "Requested account is already selected in the SDK")

        self._post_checked("AccountSwitchEnterGame")
        try:
            self._ensure_ready("Ready after account switch", allow_restart=False)
            self._post_checked("AccountServerVerify")
            self._post_checked("AccountServerReturnHome")
            self._ensure_ready("Ready after fixed-server verification", allow_restart=False)
        except Exception as ready_error:
            self._save_failure_frame("AccountServerVerify")
            raise RuntimeError(
                f"账号已选中，但主城或区服验证未通过，未更新账号进度：{ready_error}"
            ) from ready_error
        AccountTaskLedger().mark_current(account_key)
        self._progress_store = DailyProgress(account_key)
        self.emit("OK", f"Account switched and Ready: {target['label']}")

    def run_account_daily(self, account_key: str, entries: list[str], switch_first: bool = True):
        """Run only today's unfinished entries for one whitelisted account."""
        if account_key not in ACCOUNT_PRESETS:
            raise ValueError(f"Account is not whitelisted: {account_key}")
        if account_key not in DAILY_ACCOUNT_KEYS:
            raise ValueError(f"Account is excluded from daily automation: {account_key}")
        self._progress_store = DailyProgress(account_key)
        if switch_first:
            self.switch_account(account_key)
        else:
            self._load()
            if set(entries) != {"PlaneExpeditionDaily"}:
                self._ensure_ready(f"Ready for {account_key}", allow_restart=True)
            if "PlaneExpeditionDaily" in entries and AccountTaskLedger().current() != account_key:
                raise RuntimeError("当前已验证 SDK 账号与待运行账号不一致，请先切换账号")

        ledger = AccountTaskLedger()
        ordered = list(dict.fromkeys(entries))
        # A new round can appear while ordinary daily tasks remain complete.
        pending = [entry for entry in ordered if not ledger.completed(account_key, entry)]
        if "TournamentPredictionDaily" in ordered and "TournamentPredictionDaily" not in pending:
            pending.insert(0, "TournamentPredictionDaily")
        skipped = [entry for entry in ordered if entry not in pending]
        if skipped:
            self.emit(
                "INFO",
                f"{ACCOUNT_PRESETS[account_key]['label']} 今日已完成，跳过：{', '.join(skipped)}",
            )
        if not pending:
            self.emit("OK", f"{ACCOUNT_PRESETS[account_key]['label']} 今日任务均已完成")
            return {}

        # When an earlier partial run already marked DailyQuestDaily complete,
        # newly finished activity-producing tasks can still unlock another
        # chest.  Re-run only the final visual chest sweep, not all daily quest
        # children.
        run_entries = list(pending)
        append_final_sweep = (
            "DailyQuestDaily" not in pending
            and "DailyQuestDaily" in ordered
            and any(
                entry not in {"DailyQuestDaily", "TournamentPredictionDaily"} for entry in pending
            )
        )
        if append_final_sweep:
            run_entries.append("DailyFinalClaim")

        def persist_progress(parent: str, status: str, message: str = ""):
            # Persist each parent immediately so an interrupted multi-account
            # batch does not lose the work already completed for this account.
            if parent in pending:
                AccountTaskLedger().record(account_key, parent, status, message)

        failures = self.run(run_entries, completion_callback=persist_progress)
        if append_final_sweep and "DailyQuestDaily" in failures:
            self.emit("WARN", "补充活跃度宝箱领取失败，保留到下一次运行重试")
        return failures

    def run(self, entries: list[str], completion_callback=None):
        self._check_cancelled()
        self._load()
        # Enforce task-assistant priority even for callers that bypass the UI.
        entries = list(dict.fromkeys(entries))
        entries.sort(key=lambda entry: TASK_PRIORITY.get(entry, 2))

        parent_children: dict[str, list[str]] = {}
        standalone_entries = set(entries)
        endless_requested = False
        final_claim_requested = False
        for entry in entries:
            if entry == "DailyQuestDaily":
                children = [
                    "DailyInitialClaim",
                    "DailySummonDaily",
                    "DailyArenaDaily",
                    "DailyShopRefreshDaily",
                    "DailyFinalClaim",
                ]
            else:
                children = [entry]
            filtered: list[str] = []
            for child in children:
                # The spirit shop is also exposed as an independent App task.
                # When both are selected, execute it once under its own status
                # instead of repeating it inside the daily-quest parent.
                if (
                    entry == "DailyQuestDaily"
                    and child == "DailyShopRefreshDaily"
                    and "DailyShopRefreshDaily" in standalone_entries
                ):
                    continue
                # ArenaDaily already completes the three arena challenges and
                # is deliberately scheduled immediately after the assistant.
                # Do not re-enter the daily page looking for a row that the
                # game may hide once those challenges are complete.
                if (
                    entry == "DailyQuestDaily"
                    and child == "DailyArenaDaily"
                    and "ArenaDaily" in standalone_entries
                ):
                    continue
                if child in {"GameBootstrap", "LoginPreparation"}:
                    continue
                if child == "EndlessTrialDaily":
                    endless_requested = True
                    continue
                if child == "DailyFinalClaim":
                    final_claim_requested = True
                    continue
                filtered.append(child)
            if filtered:
                parent_children[entry] = filtered

        # Endless Trial is intentionally removed from the ordinary queue. It
        # runs after ordinary retries and immediately before the final activity
        # claim, so a delayed settlement page cannot interrupt another module.
        if "EndlessTrialDaily" in entries:
            endless_requested = True

        def persist(parent: str, status: str, message: str = ""):
            if completion_callback is not None:
                completion_callback(parent, status, message)

        completed_children = set()
        self._activity_tracking = "DailyQuestDaily" in entries
        self._final_activity_result = None

        def run_pass(plans: list[tuple[str, list[str]]], label: str) -> dict[str, str]:
            pass_failures: dict[str, str] = {}
            for parent, children in plans:
                self._parent_deadline = None
                self._check_cancelled()
                self._completion_messages.pop(parent, None)
                self._completion_statuses.pop(parent, None)
                first_entry = children[0]
                self.state_emit(parent, "running", f"{label}：{first_entry}")

                # Plane navigation also accepts an interrupted in-progress screen.
                # Other parent tasks start from a verified Ready state. If the
                # previous task left an unknown page, this is the point where a
                # necessary single restart may occur. Business failures can be
                # retried; exhausted global recovery stops this shared queue.
                try:
                    if parent != "PlaneExpeditionDaily":
                        self._ensure_ready(f"Ready before {parent}", allow_restart=True)
                except RunCancelled:
                    self.state_emit(parent, "waiting", "用户已停止，本轮未继续执行")
                    raise
                except ReadyUnavailable:
                    self.state_emit(parent, "failed", "主城恢复失败，本轮队列已停止")
                    self.emit("ERROR", "主城恢复已耗尽，本轮队列停止，未继续逐项等待或重试")
                    raise
                except Exception as exc:
                    error = f"Ready unavailable before task: {exc}"
                    pass_failures[parent] = error
                    persist(parent, "failed", error)
                    self.state_emit(parent, "failed", error)
                    self.emit("ERROR", f"{parent}: {error}; skipping to next task")
                    continue

                completed = True
                if parent != "TournamentPredictionDaily" and "TournamentPredictionDaily" in entries:
                    try:
                        from home_reminders import check

                        if parent != "PlaneExpeditionDaily":
                            check(self, AccountTaskLedger().current())
                    except RunCancelled:
                        raise
                    except Exception as exc:
                        self.emit("WARN", f"主城提醒检查延后：{exc}")
                        self._ensure_ready("Ready after reminder", allow_restart=False)
                budgets = {
                    "PlaneExpeditionDaily": 1800,
                    "TeamArenaDaily": 3000,
                    "CrossServerLadderDaily": 900,
                    "SuperRankDaily": 3000,
                    "WorldArenaDaily": 3000,
                    "ElementTempleDaily": 600,
                    "DomainSpiritDaily": 600,
                    "FastBattleVoyageDaily": 600,
                    "DailyQuestDaily": 600,
                }
                self._parent_deadline = time.monotonic() + budgets.get(parent, 300)
                for index, entry in enumerate(children):
                    self._check_cancelled()
                    if index:
                        self.state_emit(parent, "running", f"{label}继续：{entry}")
                    self.emit("TASK", f"{label}开始：{entry}")
                    try:
                        step = (parent, index, entry)
                        if step not in completed_children:
                            self._execute_entry(entry)
                            completed_children.add(step)
                    except RunCancelled:
                        self.state_emit(parent, "waiting", "用户已停止，本轮未继续执行")
                        raise
                    except Exception as exc:
                        self._parent_deadline = None
                        self._save_failure_frame(entry)
                        completed = False
                        error = str(exc)
                        pass_failures[parent] = error
                        persist(parent, "failed", error)
                        self.state_emit(parent, "failed", error)
                        self.emit("ERROR", f"{parent} failed at {entry}: {error}")
                        if getattr(self, "_team_arena_in_flight", False):
                            raise ReadyUnavailable(
                                "组队匹配或战斗尚未核验结束，本轮队列停止，避免结算干扰其他任务"
                            ) from exc
                        if parent == "PlaneExpeditionDaily":
                            self.emit("WARN", "位面失败现场已保留，由位面重试直接恢复")
                            break
                        self.emit(
                            "WARN",
                            "Saving diagnostics and skipping this task; soft Ready recovery will run before continuing",
                        )
                        # Do not immediately restart for an unknown popup. First
                        # try Maa visual cleanup/return-home and let the next
                        # parent perform a restart only if Ready is still absent.
                        try:
                            self._ensure_ready(f"Soft recovery after {parent}", allow_restart=False)
                        except RunCancelled:
                            raise
                        except Exception as recovery_exc:
                            self.emit(
                                "WARN",
                                f"Soft recovery after {parent} did not reach Ready: {recovery_exc}",
                            )
                        break
                    self.emit("OK", f"完成：{entry}")

                self._parent_deadline = None
                if not completed:
                    continue
                if parent == "DailyQuestDaily":
                    try:
                        self._post_checked("ResourceCycleReturnHome")
                    except Exception as exc:
                        pass_failures[parent] = str(exc)
                        persist(parent, "failed", str(exc))
                        self.state_emit(parent, "failed", str(exc))
                        continue
                try:
                    if parent == "PlaneExpeditionDaily" or (parent, children) == plans[-1]:
                        self._ensure_ready(f"Ready after {parent}", allow_restart=False)
                except RunCancelled:
                    raise
                except Exception as exc:
                    # A verified business action must not be relabelled as a
                    # task failure merely because its final navigation was
                    # dirty.  The next parent performs its own Ready recovery.
                    self.emit(
                        "WARN",
                        f"{parent}: work completed; deferred Ready recovery: {exc}",
                    )

                message = self._completion_messages.pop(parent, "本轮完成，等待下次执行")
                completion_status = self._completion_statuses.pop(parent, "success")
                # These two parents are finalized in their dedicated deferred
                # blocks below; ordinary tasks can be committed immediately.
                if parent not in {"DailyQuestDaily", "EndlessTrialDaily"}:
                    persist(parent, completion_status, message)
                self.state_emit(parent, "waiting", message)
            return pass_failures

        initial_plans = list(parent_children.items())
        failures = run_pass(initial_plans, "首轮")

        # Retry each failed parent exactly once, after every selected task has
        # received its first attempt. Successful retries are removed from the
        # final failure report.
        retry_plans = [
            (parent, parent_children[parent]) for parent in parent_children if parent in failures
        ]
        if retry_plans:
            self.emit(
                "TASK", f"首轮结束，重试失败任务：{', '.join(parent for parent, _ in retry_plans)}"
            )
            retry_failures = run_pass(retry_plans, "重试")
            for parent, _ in retry_plans:
                if parent in retry_failures:
                    failures[parent] = retry_failures[parent]
                else:
                    failures.pop(parent, None)

        # Endless Trial is always the last activity-producing task. If it
        # fails, retry it once here before moving to the chest claim.
        if endless_requested:
            endless_plan = [("EndlessTrialDaily", ["EndlessTrialDaily"])]
            endless_failures = run_pass(endless_plan, "无尽试炼")
            if endless_failures:
                self.emit("WARN", "无尽试炼首轮失败，立即进行唯一一次重试")
                endless_failures = run_pass(endless_plan, "无尽试炼重试")
            if endless_failures:
                failures.update(endless_failures)
                persist(
                    "EndlessTrialDaily",
                    "failed",
                    endless_failures.get("EndlessTrialDaily", "Endless Trial failed"),
                )
            else:
                failures.pop("EndlessTrialDaily", None)
                persist("EndlessTrialDaily", "success")

        # Claim activity chests once at the absolute end. A failed claim gets
        # one final retry because all activity-producing work is already done.
        if final_claim_requested:
            final_plan = [("DailyQuestDaily", ["DailyFinalClaim"])]
            final_failures = run_pass(final_plan, "最终活跃度领奖")
            if final_failures:
                self.emit("WARN", "最终活跃度领奖失败，执行一次最终重试")
                final_failures = run_pass(final_plan, "最终活跃度领奖重试")
            if final_failures:
                failures.update(final_failures)
                persist(
                    "DailyQuestDaily",
                    "failed",
                    final_failures.get("DailyQuestDaily", "Final activity claim failed"),
                )
            elif failures.get("DailyQuestDaily"):
                # Preserve a persistent failure from DailyQuest's activity
                # generation children even if chest collection itself passed.
                persist("DailyQuestDaily", "failed", failures["DailyQuestDaily"])
            else:
                failures.pop("DailyQuestDaily", None)
                status, message = self._final_activity_result or ("success", "")
                persist("DailyQuestDaily", status, message)
                self.state_emit("DailyQuestDaily", "waiting", message)

        if "TournamentPredictionDaily" in entries and len(entries) > 1:
            try:
                from home_reminders import check

                self._ensure_ready("Ready for final reminders", allow_restart=False)
                check(self, AccountTaskLedger().current())
            except RunCancelled:
                raise
            except Exception as exc:
                self.emit("WARN", f"日常结束提醒检查延后：{exc}")
        return failures

    def stop(self):
        self._cancel_event.set()
        if self.tasker is not None:
            try:
                self.tasker.post_stop().wait()
            except Exception:
                pass


class OverviewTaskCard(QFrame):
    """Compact ALAS-style scheduler row."""

    def __init__(self, state: TaskRunState, onclick):
        super().__init__()
        self.setObjectName("statusCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 10, 9)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = QLabel(state.title)
        title.setObjectName("taskName")
        texts.addWidget(title)
        if state.status == "running":
            timestamp = state.started_at or state.updated_at
        elif state.status == "queued":
            timestamp = state.queued_at or state.updated_at
        else:
            timestamp = state.next_run or state.updated_at
        line = QLabel(f"{timestamp}  ·  {state.message}" if timestamp else state.message)
        line.setObjectName("taskTime")
        line.setWordWrap(True)
        texts.addWidget(line)
        layout.addLayout(texts, 1)
        button = QPushButton("设置")
        button.setFixedWidth(66)
        button.clicked.connect(onclick)
        layout.addWidget(button)


class AlasMainWindow(QMainWindow):
    """Desktop shell inspired by ALAS while keeping MaaFramework execution."""

    def __init__(self, check_device_on_start=True):
        super().__init__()
        self.root_dir = base_dir()
        self.runtime = None
        self.running = False
        self.bridge = Bridge()
        self.bridge.log.connect(self.append_log)
        self.bridge.finished.connect(self.run_finished)
        self.bridge.device.connect(self.device_checked)
        self.bridge.task_state.connect(self.task_state_changed)
        self.task_defs = {
            entry: (title, description, checked) for title, description, entry, checked in TASKS
        }
        self.task_order = [entry for _, _, entry, _ in TASKS]
        self.task_states = self._load_task_states()
        self.active_run_entries: list[str] = []
        self.nav_buttons: list[QPushButton] = []
        self.task_pages: dict[str, QWidget] = {}
        self.task_enabled_boxes: dict[str, QCheckBox] = {}
        self.page_status: dict[str, QLabel] = {}
        self.page_timestamps: dict[str, QLabel] = {}
        self.status_layouts: dict[str, QVBoxLayout] = {}
        self._overview_signatures = {}
        self._close_when_finished = False
        self.galaxy_running = False
        self.state_save_timer = QTimer(self)
        self.state_save_timer.setSingleShot(True)
        self.state_save_timer.timeout.connect(self._flush_task_states)
        self.setWindowTitle("MaaShanShuo 分享版 · 闪烁之光日常助手")
        self.setWindowIcon(app_icon())
        self.resize(1500, 900)
        self.setMinimumSize(1120, 720)
        self.build_ui()
        self.log_storage_timer = QTimer(self)
        self.log_storage_timer.setInterval(60_000)
        self.log_storage_timer.timeout.connect(self.refresh_log_storage)
        self.bridge.log_storage.connect(self.log_storage_updated)
        if check_device_on_start:
            self.log_storage_timer.start()
            QTimer.singleShot(500, self.initialize_log_storage)
        self.team_arena_timer = QTimer(self)
        self.team_arena_timer.setInterval(30_000)
        self.team_arena_timer.timeout.connect(self._team_arena_tick)
        self.team_arena_timer.start()
        self.refresh_all()
        if check_device_on_start:
            QTimer.singleShot(250, self.check_device)

    @property
    def state_file(self) -> Path:
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return local / APP_NAME / "ui_state.json"

    @staticmethod
    def _now() -> str:
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _next_daily_run() -> str:
        now = dt.datetime.now()
        next_run = (now + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return next_run.strftime("%Y-%m-%d %H:%M:%S")

    def _load_task_states(self) -> dict[str, TaskRunState]:
        saved = {}
        try:
            if self.state_file.exists():
                saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        states = {}
        now = self._now()
        for title, _, entry, enabled in TASKS:
            data = saved.get(entry, {})
            allowed = set(TaskRunState.__dataclass_fields__)
            data = {key: value for key, value in data.items() if key in allowed}
            state = TaskRunState(entry=entry, title=title, enabled=enabled, updated_at=now)
            for key, value in data.items():
                setattr(state, key, value)
            state.title = title
            if state.status in {"running", "queued"}:
                state.status = "waiting"
                state.message = "上次运行被中断，等待重新执行"
                state.updated_at = now
            states[entry] = state
        return states

    def save_task_states(self):
        self.state_save_timer.start(300)

    def _flush_task_states(self):
        try:
            payload = {entry: asdict(state) for entry, state in self.task_states.items()}
            write_json(self.state_file, payload)
        except Exception as exc:
            if hasattr(self, "log_box"):
                self.append_log("WARN", f"任务状态保存失败：{exc}")

    def build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QFrame()
        top.setObjectName("topbar")
        top.setFixedHeight(64)
        top_l = QHBoxLayout(top)
        top_l.setContentsMargins(20, 0, 22, 0)
        logo = QLabel("闪")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(38, 38)
        logo.setStyleSheet("background:#8172da;border-radius:19px;font-size:20px;font-weight:700;")
        top_l.addWidget(logo)
        name = QLabel("MaaShanShuoShare")
        name.setObjectName("title")
        top_l.addWidget(name)
        self.header_run_dot = QLabel("●")
        self.header_run_dot.setObjectName("muted")
        top_l.addWidget(self.header_run_dot)
        self.header_run_state = QLabel("闲置")
        self.header_run_state.setObjectName("muted")
        top_l.addWidget(self.header_run_state)
        top_l.addStretch()
        self.header_title = QLabel("总览")
        self.header_title.setObjectName("sectionTitle")
        top_l.addWidget(self.header_title)
        top_l.addStretch()
        self.top_status = QLabel(f"{DEVICE} · 检查中")
        self.top_status.setObjectName("muted")
        top_l.addWidget(self.top_status)
        outer.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(245)
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(0, 10, 0, 10)
        side_l.setSpacing(0)
        overview = self._make_nav_button("▌  总览", 0)
        side_l.addWidget(overview)
        accounts = self._make_nav_button("   ▸  账号", 1)
        side_l.addWidget(accounts)
        group = QLabel("   已实现功能")
        group.setObjectName("muted")
        group.setContentsMargins(8, 14, 0, 7)
        side_l.addWidget(group)
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_body = QWidget()
        nav_layout = QVBoxLayout(nav_body)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        for index, entry in enumerate(self.task_order, start=2):
            title = self.task_defs[entry][0]
            nav_layout.addWidget(self._make_nav_button(f"   ▸  {title}", index))
        nav_layout.addWidget(self._make_nav_button("   ▸  星河神殿守候", len(self.task_order) + 2))
        nav_layout.addStretch()
        nav_scroll.setWidget(nav_body)
        side_l.addWidget(nav_scroll, 1)
        self.sidebar_device = QLabel(f"设备检测中\n目标 {DEVICE}")
        self.sidebar_device.setObjectName("muted")
        self.sidebar_device.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sidebar_device.setContentsMargins(8, 10, 8, 5)
        side_l.addWidget(self.sidebar_device)
        body.addWidget(side)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_overview_page())
        self.stack.addWidget(self._build_account_page())
        for entry in self.task_order:
            page = self._build_task_page(entry)
            self.task_pages[entry] = page
            self.stack.addWidget(page)
        self.stack.addWidget(self._build_galaxy_page())
        body.addWidget(self.stack, 1)
        self.show_page(0)

        self.append_log("INFO", "界面已启动，任务调度器处于闲置状态")
        self.append_log("INFO", f"目标设备为 {DEVICE}，可在本地设置中修改")

    def _make_nav_button(self, text: str, page_index: int) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("nav")
        button.clicked.connect(lambda checked=False, index=page_index: self.show_page(index))
        self.nav_buttons.append(button)
        return button

    def show_page(self, index: int):
        self.stack.setCurrentIndex(index)
        for seq, button in enumerate(self.nav_buttons):
            button.setObjectName("navActive" if seq == index else "nav")
            button.style().unpolish(button)
            button.style().polish(button)
        if index == 0:
            title = "总览"
        elif index == 1:
            title = "账号"
        elif index == len(self.task_order) + 2:
            title = "星河神殿守候"
        else:
            title = self.task_defs[self.task_order[index - 2]][0]
        self.header_title.setText(title)
        if index == 1:
            self.refresh_accounts()

    def _build_account_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        heading.addWidget(QLabel("账号与今日进度", objectName="pageTitle"))
        heading.addStretch()
        run_both = QPushButton("依次执行所有账号")
        run_both.setObjectName("primary")
        run_both.clicked.connect(self.run_all_accounts_from_ui)
        heading.addWidget(run_both)
        refresh = QPushButton("刷新状态")
        refresh.clicked.connect(self.refresh_accounts)
        heading.addWidget(refresh)
        configure = QPushButton("本地设置")
        configure.clicked.connect(self.configure_local)
        heading.addWidget(configure)
        layout.addLayout(heading)

        server = QFrame()
        server.setObjectName("deviceCard")
        server_l = QHBoxLayout(server)
        server_l.addWidget(QLabel("固定区服"))
        server_l.addWidget(
            QLabel(f"{FIXED_SERVER['id']}  ·  {FIXED_SERVER['region']}  ·  {FIXED_SERVER['name']}")
        )
        server_l.addStretch()
        self.account_current_label = QLabel("当前账号：读取中")
        self.account_current_label.setObjectName("statusGood")
        server_l.addWidget(self.account_current_label)
        layout.addWidget(server)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.account_cards_layout = QVBoxLayout(body)
        self.account_cards_layout.setContentsMargins(0, 0, 5, 0)
        self.account_cards_layout.setSpacing(12)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        return page

    def configure_local(self):
        from share_settings import settings_dialog

        if self.running:
            QMessageBox.information(self, APP_NAME, "请先停止任务，再修改本地设置。")
            return
        if settings_dialog(self):
            notice = QMessageBox(self)
            notice.setWindowTitle(APP_NAME)
            notice.setText("设置已保存。程序即将关闭，请重新打开后使用新设置。")
            notice.setStyleSheet("QWidget { color:#202226; background:#f2f3f5; }")
            notice.exec()
            self.close()

    def refresh_accounts(self):
        if not hasattr(self, "account_cards_layout"):
            return
        self._clear_layout(self.account_cards_layout)
        ledger = AccountTaskLedger()
        current = ledger.current()
        current_text = ACCOUNT_PRESETS[current]["label"] if current else "尚未检测"
        self.account_current_label.setText(f"当前账号：{current_text}")
        enabled_entries = [entry for entry in self.task_order if self.task_states[entry].enabled]
        for account_key, preset in ACCOUNT_PRESETS.items():
            day = ledger.day_status(account_key)
            completed = sum(ledger.completed(account_key, entry) for entry in enabled_entries)
            card = QFrame()
            card.setObjectName("card")
            card_l = QVBoxLayout(card)
            card_l.setContentsMargins(16, 14, 16, 14)
            top = QHBoxLayout()
            name = QLabel(preset["label"] + ("  ·  当前" if account_key == current else ""))
            name.setObjectName("sectionTitle")
            top.addWidget(name)
            top.addStretch()
            switch = QPushButton("切换到此账号")
            switch.clicked.connect(
                lambda checked=False, key=account_key: self.switch_account_from_ui(key)
            )
            top.addWidget(switch)
            run = QPushButton("执行未完成")
            run.setObjectName("primary")
            if account_key not in DAILY_ACCOUNT_KEYS:
                run.setText("不纳入日常")
                run.setEnabled(False)
            run.clicked.connect(
                lambda checked=False, key=account_key: self.run_account_from_ui(key)
            )
            top.addWidget(run)
            card_l.addLayout(top)
            summary = QLabel(f"今日完成 {completed}/{len(enabled_entries)}")
            summary.setObjectName("statusGood" if completed == len(enabled_entries) else "muted")
            card_l.addWidget(summary)
            from predictions import PredictionStore

            prediction_message = PredictionStore().summary(account_key)
            if prediction_message:
                prediction_label = QLabel(prediction_message)
                prediction_label.setWordWrap(True)
                prediction_label.setObjectName("statusGood")
                card_l.addWidget(prediction_label)
            rows = []
            for entry in enabled_entries:
                item = day.get(entry, {})
                status = item.get("status", "waiting")
                if status == "success" and not ledger.completed(account_key, entry):
                    status = "waiting"
                stamp = item.get("finished_at", "-")
                marker = {
                    "success": "✓",
                    "failed": "×",
                    "exhausted": "⊘",
                }.get(status, "·")
                suffix = "  本周期已结束" if status == "exhausted" else ""
                rows.append(f"{marker} {self.task_defs[entry][0]}    {stamp}{suffix}")
            details = QLabel("\n".join(rows) if rows else "当前没有启用任务")
            details.setObjectName("muted")
            details.setWordWrap(True)
            card_l.addWidget(details)
            self.account_cards_layout.addWidget(card)
        self.account_cards_layout.addStretch()

    def _start_account_worker(self, worker, label: str):
        if self.running:
            QMessageBox.information(self, APP_NAME, "调度器正在运行，请先停止当前任务。")
            return
        self.running = True
        self.active_run_entries = []
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.header_run_dot.setText("●")
        self.header_run_dot.setObjectName("statusGood")
        self.header_run_state.setText("运行中")
        self.current_task.setText(label)
        self.append_log("TASK", label)
        self.runtime = MaaRuntime(
            self.root_dir,
            lambda level, msg: self.bridge.log.emit(level, msg),
            lambda entry, state, msg="": self.bridge.task_state.emit(entry, state, msg),
        )

        def wrapped():
            try:
                message = worker(self.runtime)
                self.bridge.finished.emit(True, message)
            except RunCancelled:
                self.bridge.finished.emit(True, "任务已停止")
            except Exception as exc:
                self.runtime.emit("ERROR", traceback.format_exc())
                self.bridge.finished.emit(False, str(exc))

        threading.Thread(target=wrapped, daemon=True).start()

    def _build_galaxy_page(self):
        from galaxy_temple import INTERVAL, checked_interval

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        title = QLabel("星河神殿守候")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel(
            f"仅使用当前登录角色，查找六个神位中的「{GALAXY_TARGET or '尚未设置目标'}」。\n"
            "确认进化1次后挑战，切回主城等待称号结果，再按设定间隔复查。\n"
            "未发现目标或战败均继续守候，直到手动停止。\n"
            "资格不足时找低战力对手补竞技场排名；仅用免费次数及已有券，耗尽即暂停。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.galaxy_interval_seconds = INTERVAL
        try:
            settings = read_json(self.state_file.with_name("galaxy_guard_settings.json"), {})
            self.galaxy_interval_seconds = checked_interval(
                settings.get("interval_seconds", INTERVAL)
            )
        except (RuntimeError, ValueError, AttributeError) as exc:
            self.append_log("WARN", f"星河间隔设置未读取，使用默认30秒：{exc}")
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("检查间隔"))
        self.galaxy_interval = QLineEdit(str(self.galaxy_interval_seconds))
        self.galaxy_interval.setFixedWidth(150)
        self.galaxy_interval.setPlaceholderText("输入秒数，如30")
        self.galaxy_interval.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        self.galaxy_interval.editingFinished.connect(self.apply_galaxy_interval)
        interval_row.addWidget(self.galaxy_interval)
        interval_row.addWidget(QLabel("秒"))
        self.galaxy_interval_applied = QLabel(f"当前生效：{self.galaxy_interval_seconds}秒")
        self.galaxy_interval_applied.setObjectName("muted")
        interval_row.addWidget(self.galaxy_interval_applied)
        interval_row.addStretch()
        layout.addLayout(interval_row)
        self.galaxy_interval_error = QLabel()
        self.galaxy_interval_error.setObjectName("statusBad")
        self.galaxy_interval_error.setWordWrap(True)
        self.galaxy_interval_error.hide()
        layout.addWidget(self.galaxy_interval_error)
        interval_hint = QLabel(
            "直接输入1～86400秒，按回车或离开输入框后生效并自动保存。\n"
            "每轮结束后计时；运行中也可修改，当前等待按新值调整。"
        )
        interval_hint.setWordWrap(True)
        interval_hint.setObjectName("muted")
        layout.addWidget(interval_hint)
        controls = QHBoxLayout()
        self.galaxy_start = QPushButton("开始守候当前角色")
        self.galaxy_start.setObjectName("primary")
        self.galaxy_start.clicked.connect(self.start_galaxy_guard)
        controls.addWidget(self.galaxy_start)
        self.galaxy_stop = QPushButton("停止守候")
        self.galaxy_stop.setEnabled(False)
        self.galaxy_stop.clicked.connect(self.stop_tasks)
        controls.addWidget(self.galaxy_stop)
        controls.addStretch()
        layout.addLayout(controls)
        self.galaxy_status = QLabel("尚未启动；请先登录需要守候的角色。")
        self.galaxy_status.setWordWrap(True)
        layout.addWidget(self.galaxy_status)
        self.bridge.galaxy_status.connect(self.galaxy_status.setText)
        log = QLabel("运行详情与证据路径显示在总览日志中。")
        log.setObjectName("muted")
        layout.addWidget(log)
        layout.addStretch()
        return page

    def apply_galaxy_interval(self):
        from galaxy_temple import checked_interval

        raw = self.galaxy_interval.text().strip()
        try:
            seconds = checked_interval(int(raw) if re.fullmatch(r"[0-9]{1,5}", raw) else None)
        except ValueError:
            self.galaxy_interval_error.setText("请输入1～86400之间的整数秒数；当前生效值保持不变。")
            self.galaxy_interval_error.show()
            return False
        self.galaxy_interval_error.hide()
        self.galaxy_interval.setText(str(seconds))
        if seconds != self.galaxy_interval_seconds:
            self.set_galaxy_interval(seconds)
        return True

    def set_galaxy_interval(self, seconds):
        self.galaxy_interval_seconds = seconds
        self.galaxy_interval_applied.setText(f"当前生效：{seconds}秒")
        try:
            write_json(
                self.state_file.with_name("galaxy_guard_settings.json"),
                {"interval_seconds": seconds},
            )
        except OSError as exc:
            self.append_log("WARN", f"星河间隔已应用，但保存失败：{exc}")
        self.append_log("INFO", f"星河检查间隔已设为{seconds}秒")

    def start_galaxy_guard(self):
        if not re.sub(r"[\s|｜丨]", "", GALAXY_TARGET):
            QMessageBox.information(
                self, APP_NAME, "请在账号页的本地设置中填写星河目标，并重启程序。"
            )
            return
        if self.running:
            QMessageBox.information(self, APP_NAME, "调度器正在运行，请先停止当前任务。")
            return
        if not self.apply_galaxy_interval():
            self.galaxy_interval.setFocus()
            return
        from galaxy_temple import GalaxyTempleRunner

        self.reminder_watch.setChecked(False)
        self.team_arena_watch.setChecked(False)
        self.galaxy_running = True
        self.galaxy_start.setEnabled(False)
        self.galaxy_stop.setEnabled(True)
        self._start_account_worker(
            lambda runtime: GalaxyTempleRunner(
                runtime,
                self.bridge.galaxy_status.emit,
                interval_provider=lambda: self.galaxy_interval_seconds,
            ).run(),
            "星河神殿守候（当前角色）",
        )

    def switch_account_from_ui(self, account_key: str):
        label = ACCOUNT_PRESETS[account_key]["label"]
        self._start_account_worker(
            lambda runtime: runtime.switch_account(account_key) or f"已切换到 {label}",
            f"切换账号：{label}",
        )

    def run_account_from_ui(self, account_key: str):
        label = ACCOUNT_PRESETS[account_key]["label"]
        entries = [entry for entry in self.task_order if self.task_states[entry].enabled]

        def work(runtime):
            failures = runtime.run_account_daily(account_key, entries, switch_first=True)
            return f"{label} 今日未完成任务已执行" + (
                f"；失败：{', '.join(failures)}" if failures else ""
            )

        self._start_account_worker(work, f"执行账号日常：{label}")

    def run_all_accounts_from_ui(self):
        entries = [entry for entry in self.task_order if self.task_states[entry].enabled]
        if not entries:
            QMessageBox.information(self, APP_NAME, "请先启用至少一个任务。")
            return
        account_keys = list(DAILY_ACCOUNT_KEYS)
        dialog = QMessageBox(self)
        dialog.setWindowTitle("依次执行账号日常")
        dialog.setText(
            "将依次执行以下账号今日尚未完成的已启用任务：\n\n"
            + "\n".join(ACCOUNT_PRESETS[key]["label"] for key in account_keys)
            + f"\n\n区服：{FIXED_SERVER['region']} · {FIXED_SERVER['name']}\n已完成任务会跳过。是否开始？"
        )
        dialog.setStyleSheet(
            "QMessageBox { background:#2b2e34; } QLabel { color:#e8eaf0; } QPushButton { color:#e8eaf0; background:#454852; min-width:80px; }"
        )
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.button(QMessageBox.StandardButton.Yes).setText("开始执行")
        dialog.button(QMessageBox.StandardButton.No).setText("取消")
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            return

        def work(runtime):
            failed_accounts = []
            for account_key in account_keys:
                failures = runtime.run_account_daily(account_key, entries, switch_first=True)
                if failures:
                    failed_accounts.append(account_key)
            return f"{len(account_keys)} 个账号今日任务已执行" + (
                f"；存在失败：{', '.join(failed_accounts)}" if failed_accounts else ""
            )

        self._start_account_worker(work, "依次执行所有账号今日未完成任务")

    def _team_arena_tick(self):
        from team_arena import TeamArenaStore

        store = TeamArenaStore()
        try:
            from home_reminders import ReminderStore

            self.reminder_summary.setText(
                "\n".join(
                    f"{ACCOUNT_PRESETS[key]['label']}：{ReminderStore().get(key).get('message', '尚未检查')}"
                    for key in DAILY_ACCOUNT_KEYS
                )
            )
            reminder_times = [
                ReminderStore().get(key).get("next_check", 0) for key in DAILY_ACCOUNT_KEYS
            ]
            if any(reminder_times):
                stamp = dt.datetime.fromtimestamp(min(t for t in reminder_times if t)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                state = self.task_states["TournamentPredictionDaily"]
                if state.next_run != stamp:
                    state.next_run = stamp
                    self.save_task_states()
                    self.refresh_task_pages("TournamentPredictionDaily")
            if not self.running:
                self.stop_btn.setEnabled(
                    self.team_arena_watch.isChecked() or self.reminder_watch.isChecked()
                )
            if (
                not self.running
                and not self._close_when_finished
                and self.reminder_watch.isChecked()
            ):
                if due := ReminderStore().due():
                    self.run_reminder_accounts(due)
            self.team_arena_schedule.setText(
                "\n".join(
                    f"{ACCOUNT_PRESETS[key]['label']}：{store.summary(key)}"
                    for key in DAILY_ACCOUNT_KEYS
                )
            )
            moments = [store.get(key).get("next_check", 0) for key in DAILY_ACCOUNT_KEYS]
            if any(moments):
                stamp = dt.datetime.fromtimestamp(min(t for t in moments if t)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                state = self.task_states["TeamArenaDaily"]
                if state.next_run != stamp:
                    state.next_run = stamp
                    self.save_task_states()
                    self.refresh_task_pages("TeamArenaDaily")
            if self.running or self._close_when_finished or not self.team_arena_watch.isChecked():
                return
            if due := store.due():
                self.run_team_arena_accounts(due, resident=True)
        except Exception as exc:
            self.team_arena_watch.setChecked(False)
            self.reminder_watch.setChecked(False)
            self.append_log("ERROR", f"常驻检查暂停：{exc}")

    def run_team_arena_accounts(self, account_keys=None, resident=False):
        from team_arena import TeamArenaRunner, TeamArenaStore

        keys = list(DAILY_ACCOUNT_KEYS if account_keys is None else account_keys)
        keys.sort(key=lambda key: key != AccountTaskLedger().current())

        def work(runtime):
            runtime.team_arena_resident = resident
            failed = []
            for key in keys:
                runtime._check_cancelled()
                try:
                    runtime._load()
                    title = runtime._ocr_results("TeamArenaTitleScan")
                    if any(q.text.strip() == "组队斗技" for q in title):
                        # Finish the already matched round before changing the SDK account.
                        TeamArenaRunner(runtime).resume_pending()
                    runtime._post_checked("TeamArenaSwitchBoundary")
                    runtime.switch_account(key)
                    failures = runtime.run(
                        ["TeamArenaDaily"],
                        completion_callback=lambda entry, status, message="", account=key: (
                            AccountTaskLedger().record(account, entry, status, message)
                        ),
                    )
                    if failures:
                        raise RuntimeError(failures["TeamArenaDaily"])
                except RunCancelled:
                    raise
                except Exception as exc:
                    failed.append(key)
                    TeamArenaStore().put(
                        key,
                        next_check=time.time() + 1800,
                        message="执行失败，30 分钟后重试，详情见日志",
                    )
                    runtime.emit("ERROR", f"{ACCOUNT_PRESETS[key]['label']} 组队斗技失败：{exc}")
            return f"组队斗技已检查 {len(keys)} 个账号" + (
                f"；失败：{', '.join(failed)}" if failed else ""
            )

        self._start_account_worker(
            work, "组队斗技常驻检查" if resident else "已配置账号组队斗技立即执行"
        )

    def run_reminder_accounts(self, account_keys=None):
        from home_reminders import ReminderStore

        keys = list(DAILY_ACCOUNT_KEYS if account_keys is None else account_keys)
        keys.sort(key=lambda key: key != AccountTaskLedger().current())

        def work(runtime):
            failed = []
            for key in keys:
                runtime._check_cancelled()
                try:
                    runtime.switch_account(key)
                    if runtime.run(["TournamentPredictionDaily"]):
                        failed.append(key)
                except RunCancelled:
                    raise
                except Exception as exc:
                    failed.append(key)
                    runtime.emit("ERROR", f"主城提醒 {key}：{exc}")
                finally:
                    ReminderStore().put(key, next_check=time.time() + 900)
            return "主城提醒检查结束" + (f"；失败：{', '.join(failed)}" if failed else "")

        self._start_account_worker(work, "检查已配置账号主城提醒")

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        device = QFrame()
        device.setObjectName("deviceCard")
        device_l = QHBoxLayout(device)
        device_l.setContentsMargins(16, 11, 14, 11)
        device_l.addWidget(QLabel("设备"))
        addr = QLabel(DEVICE)
        addr.setStyleSheet("font-weight:700;")
        device_l.addWidget(addr)
        device_l.addWidget(QLabel("· MuMu Android 12 · 1080 × 1920 · MuMu IPC + MaaTouch"))
        device_l.addStretch()
        self.device_status = QLabel("检查中")
        self.device_status.setObjectName("muted")
        device_l.addWidget(self.device_status)
        check = QPushButton("重新检测")
        check.clicked.connect(self.check_device)
        device_l.addWidget(check)
        layout.addWidget(device)

        columns = QHBoxLayout()
        columns.setSpacing(14)
        layout.addLayout(columns, 1)

        scheduler_scroll = QScrollArea()
        scheduler_scroll.setWidgetResizable(True)
        scheduler_scroll.setMinimumWidth(380)
        scheduler_scroll.setMaximumWidth(465)
        scheduler_body = QWidget()
        scheduler_l = QVBoxLayout(scheduler_body)
        scheduler_l.setContentsMargins(0, 0, 0, 0)
        scheduler_l.setSpacing(12)

        bar = QFrame()
        bar.setObjectName("card")
        bar_l = QHBoxLayout(bar)
        bar_l.addWidget(QLabel("调度器", objectName="sectionTitle"))
        bar_l.addStretch()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self.select_all)
        bar_l.addWidget(self.select_all_btn)
        self.start_btn = QPushButton("启动")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_tasks)
        bar_l.addWidget(self.start_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_tasks)
        bar_l.addWidget(self.stop_btn)
        scheduler_l.addWidget(bar)

        for key, label in (("running", "运行中"), ("queued", "队列中"), ("waiting", "等待中")):
            card = QFrame()
            card.setObjectName("card")
            card_l = QVBoxLayout(card)
            card_l.setContentsMargins(12, 12, 12, 12)
            card_l.setSpacing(7)
            card_l.addWidget(QLabel(label, objectName="sectionTitle"))
            divider = QFrame()
            divider.setFixedHeight(1)
            divider.setStyleSheet("background:#41464f;")
            card_l.addWidget(divider)
            content_l = QVBoxLayout()
            content_l.setSpacing(6)
            card_l.addLayout(content_l)
            self.status_layouts[key] = content_l
            scheduler_l.addWidget(card)
        scheduler_l.addStretch()
        scheduler_scroll.setWidget(scheduler_body)
        columns.addWidget(scheduler_scroll, 4)

        log_panel = QFrame()
        log_panel.setObjectName("logCard")
        log_l = QVBoxLayout(log_panel)
        log_l.setContentsMargins(14, 12, 14, 14)
        log_head = QHBoxLayout()
        log_head.addWidget(QLabel("日志", objectName="sectionTitle"))
        log_head.addStretch()
        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        log_head.addWidget(self.auto_scroll)
        clear = QPushButton("清空")
        clear.clicked.connect(lambda: self.log_box.clear())
        log_head.addWidget(clear)
        log_l.addLayout(log_head)
        storage_row = QHBoxLayout()
        self.log_storage_label = QLabel("日志占用：等待统计（自动清理目标1 GB）")
        self.log_storage_label.setObjectName("muted")
        storage_row.addWidget(self.log_storage_label, 1)
        self.clean_logs_button = QPushButton("清理历史日志")
        self.clean_logs_button.clicked.connect(lambda: self.refresh_log_storage(manual=True))
        storage_row.addWidget(self.clean_logs_button)
        log_l.addLayout(storage_row)
        self.current_task = QLabel("当前无任务")
        self.current_task.setObjectName("muted")
        log_l.addWidget(self.current_task)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.document().setMaximumBlockCount(2000)
        log_l.addWidget(self.log_box, 1)
        columns.addWidget(log_panel, 7)
        return page

    def initialize_log_storage(self):
        retention = manager(runtime_log_dir(self.root_dir))

        def initialize():
            try:
                retention.protect_running_apps()
                retention.maintain(self.bridge.log_storage.emit, force=True)
            except Exception as exc:
                self.bridge.log_storage.emit({"errors": [str(exc)]})

        threading.Thread(target=initialize, daemon=True, name="log-cleanup-startup").start()

    def refresh_log_storage(self, manual=False):
        manager(runtime_log_dir(self.root_dir)).maintain(self.bridge.log_storage.emit, force=True)
        if manual:
            self.append_log("INFO", "正在清理历史日志；保留当前执行和未确认战果。")

    def log_storage_updated(self, report):
        self.clean_logs_button.setEnabled(True)
        if "after" not in report:
            self.log_storage_label.setText("日志统计/清理失败，可重试")
        else:
            text = f"日志占用：{report['after'] / 1024**2:.1f} MB / 1024 MB"
            if report.get("over_limit"):
                text += "（保护中的记录暂超限）"
            self.log_storage_label.setText(text)
            if report["freed"]:
                self.append_log("INFO", f"历史日志已清理：释放{report['freed'] / 1024**2:.1f} MB")
        if report.get("errors"):
            self.append_log("WARN", f"部分日志保留未清理：{report['errors'][0]}")

    def _build_task_page(self, entry: str) -> QWidget:
        title, description, _ = self.task_defs[entry]
        state = self.task_states[entry]
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)
        outer.addWidget(QLabel(title, objectName="pageTitle"))
        desc = QLabel(description)
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        settings = QFrame()
        settings.setObjectName("card")
        form = QVBoxLayout(settings)
        form.setContentsMargins(20, 18, 20, 18)
        form.addWidget(QLabel("任务设置", objectName="sectionTitle"))
        enabled = QCheckBox("启用并加入总览调度")
        enabled.setChecked(state.enabled)
        enabled.toggled.connect(lambda value, key=entry: self.set_task_enabled(key, value))
        self.task_enabled_boxes[entry] = enabled
        form.addWidget(enabled)
        form.addWidget(QLabel(f"Pipeline 入口：{entry}", objectName="muted"))
        outer.addWidget(settings)

        if entry == "TournamentPredictionDaily":
            self.reminder_watch = QCheckBox("本次启动保持常驻：空闲时每 15 分钟检查已配置账号提醒")
            form.addWidget(self.reminder_watch)
            self.reminder_watch.toggled.connect(
                lambda checked: QTimer.singleShot(0, self._team_arena_tick)
            )
            self.reminder_summary = QLabel("尚未检查主城提醒")
            self.reminder_summary.setWordWrap(True)
            form.addWidget(self.reminder_summary)
            hint = QLabel(
                "已支持两类赛事竞猜；未知黄框保存文字和截图。日常中在任务间检查，停止按钮暂停常驻。"
            )
            hint.setWordWrap(True)
            form.addWidget(hint)
            batch = QPushButton("立即检查已配置账号")
            batch.clicked.connect(lambda: self.run_reminder_accounts())
            form.addWidget(batch)
        if entry == "TeamArenaDaily":
            self.team_arena_watch = QCheckBox("本次启动保持常驻：已配置账号攒到 5 次自动清空")
            self.team_arena_watch.toggled.connect(
                lambda checked: QTimer.singleShot(0, self._team_arena_tick)
            )
            form.addWidget(self.team_arena_watch)
            hint = QLabel(
                "日常执行期间等待；关闭前 90 分钟开始清尾，后续恢复一次打一次。\n"
                "首次参赛及时打；停止按钮同时暂停常驻，关闭助手后不再执行。"
            )
            hint.setWordWrap(True)
            form.addWidget(hint)
            self.team_arena_schedule = QLabel("开启常驻或运行任务后显示各账号下次检查时间")
            self.team_arena_schedule.setWordWrap(True)
            form.addWidget(self.team_arena_schedule)

        status_card = QFrame()
        status_card.setObjectName("card")
        status_l = QVBoxLayout(status_card)
        status_l.setContentsMargins(20, 18, 20, 18)
        status_l.addWidget(QLabel("运行状态", objectName="sectionTitle"))
        status = QLabel()
        status.setObjectName("statusGood")
        status_l.addWidget(status)
        timestamps = QLabel()
        timestamps.setObjectName("muted")
        timestamps.setWordWrap(True)
        status_l.addWidget(timestamps)
        self.page_status[entry] = status
        self.page_timestamps[entry] = timestamps
        outer.addWidget(status_card)

        actions = QHBoxLayout()
        run = QPushButton("立即运行此任务")
        run.setObjectName("primary")
        run.clicked.connect(lambda checked=False, key=entry: self.start_entries([key]))
        actions.addWidget(run)
        if entry == "TeamArenaDaily":
            batch = QPushButton("已配置账号立即清空次数并领取宝箱")
            batch.clicked.connect(lambda: self.run_team_arena_accounts())
            actions.addWidget(batch)
        actions.addStretch()
        outer.addLayout(actions)
        outer.addStretch()
        return page

    @staticmethod
    def _clear_layout(layout: QVBoxLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def refresh_overview(self):
        buckets = {"running": [], "queued": [], "waiting": []}
        for entry in self.task_order:
            state = self.task_states[entry]
            if state.status == "running":
                buckets["running"].append(state)
            elif state.status == "queued":
                buckets["queued"].append(state)
            elif state.enabled:
                buckets["waiting"].append(state)
        for key, layout in self.status_layouts.items():
            signature = tuple(
                (s.entry, s.status, s.message, s.updated_at, s.next_run) for s in buckets[key]
            )
            if self._overview_signatures.get(key) == signature:
                continue
            self._overview_signatures[key] = signature
            self._clear_layout(layout)
            if not buckets[key]:
                empty = QLabel("无任务")
                empty.setObjectName("muted")
                empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(empty)
                continue
            for state in buckets[key]:
                index = self.task_order.index(state.entry) + 2
                layout.addWidget(
                    OverviewTaskCard(state, lambda checked=False, i=index: self.show_page(i))
                )

    def refresh_task_pages(self, only=None):
        labels = {
            "waiting": "等待中",
            "queued": "队列中",
            "running": "运行中",
            "failed": "上次运行失败",
            "disabled": "已停用",
        }
        for entry, state in self.task_states.items():
            if only and entry != only:
                continue
            if entry not in self.page_status:
                continue
            label = self.page_status[entry]
            label.setText(f"{labels.get(state.status, state.status)} · {state.message}")
            label.setObjectName("statusBad" if state.status == "failed" else "statusGood")
            label.style().unpolish(label)
            label.style().polish(label)
            times = [
                f"更新时间：{state.updated_at or '-'}",
                f"入队时间：{state.queued_at or '-'}",
                f"开始时间：{state.started_at or '-'}",
                f"完成时间：{state.finished_at or '-'}",
                f"下次时间：{state.next_run or '-'}",
            ]
            self.page_timestamps[entry].setText("\n".join(times))

    def refresh_all(self):
        self.refresh_overview()
        self.refresh_task_pages()
        if self.stack.currentIndex() == 1:
            self.refresh_accounts()
        if hasattr(self, "select_all_btn"):
            self.select_all_btn.setText(
                "取消全选" if all(state.enabled for state in self.task_states.values()) else "全选"
            )

    def append_log(self, level: str, message: str):
        stamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        colors = {
            "INFO": "#30a8ff",
            "STEP": "#63b3ff",
            "TASK": "#b69cff",
            "OK": "#55d6a6",
            "READY": "#55d6a6",
            "WARN": "#f0b45c",
            "ERROR": "#ef6b73",
        }
        color = colors.get(level, "#cbd0db")
        safe = html.escape(str(message))
        self.log_box.append(
            f'<span style="color:{color};font-weight:600">{html.escape(level):5}</span> '
            f'<span style="color:#35b6df">{stamp}</span> '
            f'<span style="color:#cbd0db">│ {safe}</span>'
        )
        if self.auto_scroll.isChecked():
            self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())
        if level in {"TASK", "STEP"}:
            self.current_task.setText(message)

    def set_task_enabled(self, entry: str, enabled: bool):
        state = self.task_states[entry]
        state.enabled = enabled
        if not enabled and state.status not in {"running", "queued"}:
            state.status = "disabled"
            state.message = "未启用"
        elif enabled and state.status == "disabled":
            state.status = "waiting"
            state.message = "等待调度"
        state.touch()
        self.save_task_states()
        self.refresh_all()

    def select_all(self):
        enabled = [state.enabled for state in self.task_states.values()]
        target = not all(enabled)
        for state in self.task_states.values():
            state.enabled = target
            state.status = "waiting" if target else "disabled"
            state.message = "等待调度" if target else "未启用"
            state.touch()
        for entry, checkbox in self.task_enabled_boxes.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(self.task_states[entry].enabled)
            checkbox.blockSignals(False)
        self.save_task_states()
        self.refresh_all()

    def check_device(self):
        self.device_status.setText("检查中…")

        def worker():
            if not ADB.exists():
                self.bridge.device.emit(False, f"ADB 不存在：{ADB}")
                return
            try:
                subprocess.run(
                    [str(ADB), "connect", DEVICE],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                result = subprocess.run(
                    [str(ADB), "-s", DEVICE, "get-state"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                ok = result.returncode == 0 and result.stdout.strip() == "device"
                self.bridge.device.emit(ok, result.stdout.strip() or result.stderr.strip())
            except Exception as exc:
                self.bridge.device.emit(False, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def device_checked(self, ok: bool, details: str):
        self.device_status.setText("● 已连接" if ok else "● 未连接")
        self.device_status.setObjectName("statusGood" if ok else "statusBad")
        self.device_status.style().unpolish(self.device_status)
        self.device_status.style().polish(self.device_status)
        self.top_status.setText(f"{DEVICE} · {'在线' if ok else '离线'}")
        self.top_status.setObjectName("statusGood" if ok else "statusBad")
        self.top_status.style().unpolish(self.top_status)
        self.top_status.style().polish(self.top_status)
        self.sidebar_device.setText(f"{'● 已连接' if ok else '● 未连接'}\n固定目标 {DEVICE}")
        self.append_log("OK" if ok else "ERROR", f"设备检测：{details}")

    def start_tasks(self):
        entries = [entry for entry in self.task_order if self.task_states[entry].enabled]
        self.start_entries(entries)

    def start_entries(self, entries: list[str]):
        if self.running:
            QMessageBox.information(self, APP_NAME, "调度器正在运行，请先停止当前任务。")
            return
        if not entries:
            QMessageBox.information(self, APP_NAME, "请至少启用一个任务。")
            return
        entries = list(dict.fromkeys(entries))
        entries.sort(key=lambda entry: TASK_PRIORITY.get(entry, 2))
        now = self._now()
        self.active_run_entries = list(entries)
        for entry in entries:
            state = self.task_states[entry]
            state.status = "queued"
            state.queued_at = now
            state.started_at = ""
            state.finished_at = ""
            state.message = "已加入本轮队列"
            state.touch()
        self.running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.header_run_dot.setText("●")
        self.header_run_dot.setObjectName("statusGood")
        self.header_run_state.setText("运行中")
        self.current_task.setText(f"准备执行 {len(entries)} 个任务")
        self.append_log("INFO", "任务队列：" + " → ".join(self.task_defs[e][0] for e in entries))
        self.save_task_states()
        self.refresh_all()

        self.runtime = MaaRuntime(
            self.root_dir,
            lambda level, msg: self.bridge.log.emit(level, msg),
            lambda entry, state, msg="": self.bridge.task_state.emit(entry, state, msg),
        )

        def worker():
            try:
                failures = self.runtime.run(entries)
                if failures:
                    names = "、".join(self.task_defs.get(entry, (entry,))[0] for entry in failures)
                    self.bridge.finished.emit(
                        True, f"本轮已完成；跳过 {len(failures)} 个失败任务：{names}"
                    )
                else:
                    self.bridge.finished.emit(True, "所选任务已全部完成")
            except RunCancelled:
                self.bridge.finished.emit(True, "任务已停止")
            except Exception as exc:
                self.runtime.emit("ERROR", traceback.format_exc())
                self.bridge.finished.emit(False, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def task_state_changed(self, entry: str, status: str, message: str):
        if entry not in self.task_states:
            return
        state = self.task_states[entry]
        now = self._now()
        previous = state.status
        state.status = status
        state.message = message or status
        state.updated_at = now
        if status == "running" and previous != "running":
            state.started_at = now
        if status == "waiting" and previous == "running":
            state.finished_at = now
            state.next_run = self._next_daily_run()
        if status == "failed":
            state.finished_at = now
        self.save_task_states()
        self.refresh_overview()
        self.refresh_task_pages(entry)
        if self.stack.currentIndex() == 1:
            self.refresh_accounts()

    def stop_tasks(self):
        self.reminder_watch.setChecked(False)
        self.team_arena_watch.setChecked(False)
        self.append_log("WARN", "正在请求安全停止…")
        self.stop_btn.setEnabled(False)
        if self.runtime:
            threading.Thread(target=self.runtime.stop, daemon=True).start()

    def run_finished(self, ok: bool, message: str):
        now = self._now()
        stopped = message == "任务已停止"
        if self.galaxy_running:
            self.galaxy_running = False
            self.galaxy_start.setEnabled(True)
            self.galaxy_stop.setEnabled(False)
            self.galaxy_status.setText(message)
        for entry in self.active_run_entries:
            state = self.task_states[entry]
            if state.status == "queued":
                state.status = "waiting"
                state.message = (
                    "用户停止，本轮未执行"
                    if stopped
                    else ("前序任务结束，本轮未执行" if not ok else "本轮完成")
                )
                state.updated_at = now
                state.next_run = self._next_daily_run()
            elif state.status == "running":
                state.status = "waiting" if ok else "failed"
                state.message = "用户已停止" if stopped else ("本轮完成" if ok else message)
                state.finished_at = now
                state.updated_at = now
                if ok:
                    state.next_run = self._next_daily_run()
        self.running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.header_run_dot.setObjectName("statusBad" if not ok else "muted")
        self.header_run_state.setText("异常" if not ok else "闲置")
        self.current_task.setText(
            "已停止" if stopped else ("已完成" if ok else "执行失败，请查看日志")
        )
        self.append_log("OK" if ok else "ERROR", message)
        self.runtime = None
        self.save_task_states()
        self.refresh_all()
        if self._close_when_finished:
            self.close()
        else:
            QTimer.singleShot(0, self._team_arena_tick)

    def closeEvent(self, event):
        if self.running:
            if self._close_when_finished:
                event.ignore()
                return
            answer = QMessageBox.question(self, APP_NAME, "任务仍在运行，确定要关闭并停止吗？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.runtime:
                self._close_when_finished = True
                self.stop_tasks()
                event.ignore()
                return
        self.state_save_timer.stop()
        self._flush_task_states()
        event.accept()


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--screenshot", type=str)
    parser.add_argument("--self-test", type=str)
    parser.add_argument("--run-entry", nargs="+")
    parser.add_argument("--switch-account", choices=sorted(ACCOUNT_PRESETS))
    parser.add_argument("--run-accounts", nargs="+", choices=sorted(DAILY_ACCOUNT_KEYS))
    parser.add_argument("--run-result", type=str)
    parser.add_argument("--galaxy-guard", action="store_true")
    parser.add_argument("--galaxy-cycles", type=int)
    parser.add_argument("--galaxy-interval", type=int, default=30)
    return parser.parse_known_args()[0]


def main():
    args = parse_args()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    cli_mode = bool(
        args.self_test
        or args.switch_account
        or args.run_accounts
        or args.run_entry
        or args.galaxy_guard
    )
    window = AlasMainWindow(check_device_on_start=not cli_mode)
    window.bridge.cli_done.connect(app.exit)
    if not cli_mode:
        window.show()
    if args.galaxy_guard:
        from galaxy_temple import GalaxyTempleRunner

        output = Path(args.run_result).resolve() if args.run_result else None

        def run_galaxy():
            code = 1
            runtime = MaaRuntime(base_dir(), lambda level, msg: window.bridge.log.emit(level, msg))
            try:
                if args.galaxy_cycles is not None and args.galaxy_cycles < 1:
                    raise ValueError("--galaxy-cycles 必须大于0")
                result = GalaxyTempleRunner(runtime, interval_seconds=args.galaxy_interval).run(
                    max_cycles=args.galaxy_cycles
                )
                code = 0
            except Exception:
                result = "FAILED:\n" + traceback.format_exc()
                runtime.emit("ERROR", result)
            finally:
                if output:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(result, encoding="utf-8")
                window.bridge.cli_done.emit(code)

        QTimer.singleShot(0, lambda: threading.Thread(target=run_galaxy, daemon=True).start())
        return app.exec()
    if args.self_test:
        output = Path(args.self_test).resolve()

        def self_test():
            code = 1
            try:
                runtime = MaaRuntime(
                    base_dir(), lambda level, msg: window.bridge.log.emit(level, msg)
                )
                runtime._load()
                result = "OK: resource/controller/tasker initialized for " + DEVICE
                code = 0
            except Exception:
                result = "FAILED:\n" + traceback.format_exc()
            finally:
                try:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(result, encoding="utf-8")
                finally:
                    window.bridge.cli_done.emit(code)

        QTimer.singleShot(0, lambda: threading.Thread(target=self_test, daemon=True).start())
        return app.exec()
    if args.switch_account:
        output = Path(args.run_result).resolve() if args.run_result else None

        def switch_account():
            code = 1
            try:
                runtime = MaaRuntime(
                    base_dir(), lambda level, msg: window.bridge.log.emit(level, msg)
                )
                runtime.switch_account(args.switch_account)
                result = "OK: switched to " + args.switch_account
                code = 0
            except Exception:
                result = "FAILED:\n" + traceback.format_exc()
            finally:
                try:
                    if output:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(result, encoding="utf-8")
                finally:
                    window.bridge.cli_done.emit(code)

        QTimer.singleShot(0, lambda: threading.Thread(target=switch_account, daemon=True).start())
        return app.exec()
    if args.run_accounts:
        output = Path(args.run_result).resolve() if args.run_result else None

        def run_accounts():
            code = 1
            try:
                runtime = MaaRuntime(
                    base_dir(), lambda level, msg: window.bridge.log.emit(level, msg)
                )
                entries = [entry for _, _, entry, enabled in TASKS if enabled]
                all_failures = {}
                for account_key in args.run_accounts:
                    failures = runtime.run_account_daily(account_key, entries, switch_first=True)
                    if failures:
                        all_failures[account_key] = failures
                if all_failures:
                    result = "COMPLETED_WITH_FAILURES: " + repr(all_failures)
                else:
                    result = "OK: " + " -> ".join(args.run_accounts)
                    code = 0
            except Exception:
                result = "FAILED:\n" + traceback.format_exc()
            finally:
                try:
                    if output:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(result, encoding="utf-8")
                finally:
                    window.bridge.cli_done.emit(code)

        QTimer.singleShot(0, lambda: threading.Thread(target=run_accounts, daemon=True).start())
        return app.exec()
    if args.run_entry:
        output = Path(args.run_result).resolve() if args.run_result else None

        def run_entries():
            code = 1
            try:
                runtime = MaaRuntime(
                    base_dir(), lambda level, msg: window.bridge.log.emit(level, msg)
                )
                failures = runtime.run(args.run_entry)
                if failures:
                    result = "COMPLETED_WITH_FAILURES: " + repr(failures)
                    code = 1
                else:
                    result = "OK: " + " -> ".join(args.run_entry)
                    code = 0
            except Exception:
                result = "FAILED:\n" + traceback.format_exc()
            finally:
                try:
                    if output:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(result, encoding="utf-8")
                finally:
                    window.bridge.cli_done.emit(code)

        QTimer.singleShot(0, lambda: threading.Thread(target=run_entries, daemon=True).start())
        return app.exec()
    if args.screenshot:
        output = Path(args.screenshot).resolve()

        def save_and_quit():
            output.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(output))
            app.quit()

        QTimer.singleShot(900, save_and_quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
