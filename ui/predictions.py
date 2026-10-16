"""Visible tournament prompts, majority support and confirmed per-round records."""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from pathlib import Path

from home_identity import identify_home
from log_retention import recorded, result, start_record
from storage import read_json, write_json
from task_config import ACCOUNT_PRESETS, DAILY_ACCOUNT_KEYS

KINDS = ("在线锦标赛", "巅峰冠军赛")
ENTRY = "TournamentPredictionDaily"


def normalized(text):
    return re.sub(r"[\W_丶]", "", text)


def majority_side(frame):
    """Measure the blue/red support bar, independent of power and odds."""
    import numpy as np

    # Maa frames are BGR. Exclude the golden border and dice buttons.
    bar = frame[908:922, 132:587].astype(np.int16)
    blue = (bar[:, :, 0] > bar[:, :, 2] + 30) & (bar[:, :, 1] > bar[:, :, 2] + 20)
    red = (bar[:, :, 2] > bar[:, :, 0] + 45) & (bar[:, :, 2] > bar[:, :, 1] + 30)
    left, right = int(blue.sum()), int(red.sum())
    if left + right < bar.shape[0] * bar.shape[1] * 0.85:
        raise RuntimeError("竞猜支持条识别不完整，未选择选手")
    # Equal support has no majority; use the left side as a deterministic tie break.
    return ("left" if left >= right else "right"), left / (left + right)


class PredictionStore:
    def __init__(self, path=None):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        self.path = path or local / "MaaShanShuoShare" / "predictions.json"

    def get(self, account, kind):
        return read_json(self.path, {}).get(account, {}).get(kind, {})

    def put(self, account, kind, **fields):
        data = read_json(self.path, {})
        data.setdefault(account, {})[kind] = fields
        write_json(self.path, data, durable=True)

    def active(self, account):
        now = dt.datetime.now().isoformat(timespec="seconds")
        return {
            kind: item
            for kind in KINDS
            if (item := self.get(account, kind)).get("expires_at", "") > now
            and item.get("status") == "confirmed"
        }

    def summary(self, account):
        return "；".join(
            f"{kind}：已竞猜（{item['phase']}）" for kind, item in self.active(account).items()
        )

    def unresolved(self, account):
        now = dt.datetime.now().isoformat(timespec="seconds")
        return [
            kind
            for kind in KINDS
            if (item := self.get(account, kind)).get("expires_at", "") > now
            and item.get("status") in {"observed", "pending"}
        ]


class PredictionRunner:
    def __init__(self, runtime, account):
        if account not in DAILY_ACCOUNT_KEYS:
            raise RuntimeError("竞猜需要已验证的日常账号，请先选择账号")
        self.runtime = runtime
        self.account = account
        self.store = PredictionStore()
        start_record(self, "predictions")

    def scan(self):
        from maa.pipeline import JOCR, JRecognitionType

        self.runtime._check_cancelled()
        job = self.runtime.controller.post_screencap()
        self.runtime._wait_job(job, 10)
        self.frame = job.get()
        if self.frame is None:
            raise RuntimeError("竞猜截图失败")
        job = self.runtime.tasker.post_recognition(JRecognitionType.OCR, JOCR(), self.frame)
        self.runtime._wait_job(job, 10)
        detail = job.get()
        if detail is None:
            raise RuntimeError("竞猜 OCR 失败")
        self.rows = [r for n in detail.nodes if n.recognition for r in n.recognition.all_results]

    def verify_role(self):
        role = self.find(".+", (170, 0, 175, 55))
        if role and normalized(role.text) == normalized(ACCOUNT_PRESETS[self.account]["role_name"]):
            return
        try:
            identify_home(self.runtime, expected=self.account)
        except RuntimeError as exc:
            raise RuntimeError("主城角色与已验证账号不一致，未操作竞猜") from exc

    def find(self, pattern, roi):
        x0, y0, width, height = roi
        return next(
            (
                row
                for row in self.rows
                if x0 <= row.box[0] + row.box[2] / 2 < x0 + width
                and y0 <= row.box[1] + row.box[3] / 2 < y0 + height
                and re.search(pattern, row.text)
            ),
            None,
        )

    def wait_for(self, pattern, roi):
        for _ in range(8):
            self.scan()
            if row := self.find(pattern, roi):
                return row
            self.runtime._cancel_event.wait(0.4)
        raise RuntimeError(f"竞猜页面未出现预期内容：{pattern}")

    def click(self, row=None, point=None):
        if point is None:
            x, y, w, h = row.box
            point = (int(x + w / 2), int(y + h / 2))
        self.runtime._check_cancelled()
        job = self.runtime.controller.post_click(*point)
        self.runtime._wait_job(job, 10)
        if not job.succeeded:
            raise RuntimeError("竞猜点击失败")
        self.runtime._cancel_event.wait(0.7)

    def evidence(self, name):
        from PIL import Image

        path = self.folder / f"{name}-{time.time_ns()}.png"
        Image.fromarray(self.frame[:, :, ::-1]).save(path)
        return str(path)

    def open_cross_menu(self):
        for attempt in range(3):
            if city := self.find("^跨服战场$", (100, 300, 540, 600)):
                self.click(city)
                return
            if attempt < 2:
                job = self.runtime.controller.post_swipe(620, 650, 120, 650, 300)
                self.runtime._wait_job(job, 10)
                if not job.succeeded:
                    raise RuntimeError("竞猜恢复时主城滑动失败")
                self.runtime._cancel_event.wait(0.5)
                self.scan()
        raise RuntimeError("待确认竞猜的跨服战场入口未识别")

    def round_info(self, kind):
        phase = self.find(r"竞猜阶段", (160, 340, 400, 60))
        timer = self.find(r"^\d{2}:\d{2}:\d{2}$", (20, 740, 200, 60))
        players = [
            self.find(r"\[S\d+\].+", roi) for roi in ((110, 850, 235, 55), (370, 850, 235, 55))
        ]
        if not phase or not timer or not all(players):
            raise RuntimeError("竞猜轮次、选手或剩余时间未识别，未下注")
        hours, minutes, seconds = map(int, timer.text.split(":"))
        now = dt.datetime.now()
        names = [normalized(p.text) for p in players]
        phase_text = normalized(phase.text).replace("竞猜阶段", "")
        return {
            "round": "|".join([now.date().isoformat(), kind, phase_text, *names]),
            "phase": phase_text,
            "players": names,
            "expires_at": (
                now + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
            ).isoformat(timespec="seconds"),
        }

    def predict(self, kind):
        self.wait_for("竞猜阶段", (160, 340, 400, 60))
        info = self.round_info(kind)
        old = self.store.get(self.account, kind)
        marked = self.find("已下注|已竞猜", (20, 875, 690, 70))
        if not marked:
            if old.get("round") == info["round"] and old.get("status") in {"pending", "confirmed"}:
                raise RuntimeError("本轮已有提交记录但未看到已下注，保留记录并停止重复提交")
            self.store.put(self.account, kind, **info, status="observed")
            side, ratio = majority_side(self.frame)
            balance = self.find(r"我的代币.*\d", (490, 170, 220, 65))
            amount = re.search(r"(\d+)\s*$", balance.text) if balance else None
            if not amount or int(amount[1]) <= 0:
                raise RuntimeError("竞猜币余额不足或未识别，未下注")
            amount = int(amount[1])
            self.evidence(kind + "-before")
            self.runtime.emit(
                "STEP", f"{kind}：蓝方支持约{ratio:.1%}，选择{'左' if side == 'left' else '右'}方"
            )
            self.click(point=(70 if side == "left" else 654, 920))
            self.wait_for("支持后竞猜选择将不可更改", (130, 545, 450, 65))
            offer = self.find(r"\d+\s*支持\s*玩家", (100, 495, 510, 60))
            chosen = info["players"][0 if side == "left" else 1]
            chosen_name = re.sub(r"^S\d+", "", chosen)
            if (
                not offer
                or not re.search(rf"(?<!\d){amount}支持玩家", normalized(offer.text))
                or chosen_name not in normalized(offer.text)
            ):
                raise RuntimeError("竞猜确认框金额或选手不一致，未确认")
            confirm = self.find("^确定$", (450, 735, 160, 80))
            if not confirm:
                raise RuntimeError("竞猜确认按钮未识别")
            info.update(side=side, amount=amount, blue_share=ratio)
            self.store.put(self.account, kind, **info, status="pending")
            self.click(confirm)
            self.wait_for("已下注|已竞猜", (20, 875, 690, 70))
        else:
            if old.get("round") == info["round"]:
                info.update({key: old[key] for key in ("amount", "blue_share") if key in old})
            info["side"] = "left" if marked.box[0] < 360 else "right"
        proof = self.evidence(kind + "-confirmed")
        self.store.put(self.account, kind, **info, status="confirmed", evidence=proof)
        self.runtime.emit("OK", f"{kind}已竞猜：{proof}")

    @recorded
    def run(self):
        visited = set()
        try:
            for _ in range(3):
                self.scan()
                prompts = [(kind, self.find(kind + "竞猜", (60, 850, 300, 260))) for kind in KINDS]
                prompts = [(kind, row) for kind, row in prompts if row]
                if not prompts:
                    prompts = [(kind, None) for kind in self.store.unresolved(self.account)]
                    if not prompts:
                        result(self, "success" if visited else "noop")
                        return self.store.summary(self.account)
                self.verify_role()
                # Entering an event can dismiss its reminder before a vote is
                # submitted. Remember both visible events to resume navigation.
                for kind, row in prompts:
                    old = self.store.get(self.account, kind)
                    if row and old.get("expires_at", "") <= dt.datetime.now().isoformat(
                        timespec="seconds"
                    ):
                        self.store.put(
                            self.account,
                            kind,
                            status="observed",
                            expires_at=(dt.datetime.now() + dt.timedelta(hours=1)).isoformat(
                                timespec="seconds"
                            ),
                        )
                kind, row = prompts[0]
                if kind in visited:
                    raise RuntimeError(f"{kind}已处理但主城提示仍存在")
                visited.add(kind)
                if row:
                    self.click(row)
                else:
                    self.open_cross_menu()
                    self.click(self.wait_for("^" + kind + "$", (350, 600, 280, 400)))
                enter = self.wait_for("^进入大赛$", (260, 1050, 220, 90))
                if not self.find(kind, (0, 0, 230, 75)):
                    raise RuntimeError("竞猜入口跳转的赛事不匹配")
                self.click(enter)
                self.predict(kind)
                self.click(point=(48, 1220))
                self.wait_for("跨服战场|竞技场|镇妖塔", (0, 300, 650, 550))
            raise RuntimeError("竞猜提示清理超过两类赛事的上限")
        except Exception:
            if getattr(self, "frame", None) is not None:
                self.evidence("failed")
            raise
