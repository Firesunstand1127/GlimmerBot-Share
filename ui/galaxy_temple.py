"""Current-role Galaxy Temple guard, independent of daily completion ledgers."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from types import SimpleNamespace

from log_retention import recorded, result, start_record
from task_config import GALAXY_TARGET as TARGET

INTERVAL = 30
MAX_INTERVAL = 86400
FULL = (0, 0, 720, 1280)


def checked_interval(seconds):
    if type(seconds) is not int or not 1 <= seconds <= MAX_INTERVAL:
        raise ValueError("检查间隔必须为1～86400秒的整数")
    return seconds


def compact(text):
    return re.sub(r"\s+", "", text)


def target_name(text):
    # Separator variants are OCR-equivalent; other name characters stay exact.
    expected = re.sub(r"[\s|｜丨]", "", TARGET)
    return bool(expected) and re.sub(r"[\s|｜丨]", "", text) == expected


@dataclass(frozen=True)
class Seat:
    name: str
    roi: tuple
    rank: int


SEATS = (
    Seat("上中", (275, 559, 180, 49), 10),
    Seat("左中", (70, 658, 185, 48), 20),
    Seat("右中", (485, 658, 195, 48), 20),
    Seat("左下", (25, 969, 185, 45), 50),
    Seat("中下", (275, 1001, 185, 45), 50),
    Seat("右下", (510, 972, 185, 45), 50),
)


def evolution_selected(frame):
    """Green check must be present only in the 1x checkbox (BGR screenshot)."""
    import numpy as np

    selected = []
    for x in (57, 270, 471):
        patch = frame[949:994, x : x + 46].astype(np.int16)
        if not patch.size:
            selected.append(False)
            continue
        b, g, r = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
        selected.append(int(((g > 110) & (g > r + 30) & (g > b + 35)).sum()) >= 65)
    return tuple(selected)


def qualification(text):
    match = re.search(r"前(10|20|50)名.*我的排名[:：]?(\d+)", compact(text))
    return tuple(map(int, match.groups())) if match else None


class GalaxyTempleRunner:
    def __init__(self, runtime, status=None, interval_seconds=INTERVAL, interval_provider=None):
        if not re.sub(r"[\s|｜丨]", "", TARGET):
            raise ValueError("请先在本地设置中填写星河目标")
        self.runtime = runtime
        self.status = status or (lambda message: None)
        self.interval_seconds = checked_interval(interval_seconds)
        self.interval_provider = interval_provider
        start_record(self, "galaxy_temple")
        self.frame = None
        self.rows = []
        self.account = None
        self.rounds = 0
        self.wins = 0
        self.losses = 0
        self.pending = False
        self.last_outcome = None

    def say(self, message):
        self.runtime.emit("TASK", "星河神殿：" + message)
        self.status(message)

    def interval(self):
        return checked_interval(
            self.interval_provider() if self.interval_provider else self.interval_seconds
        )

    def wait_interval(self):
        started = time.monotonic()
        previous = None
        while True:
            self.runtime._check_cancelled()
            seconds = self.interval()
            remaining = started + seconds - time.monotonic()
            if seconds != previous:
                self.say(
                    f"检查间隔{seconds}秒；约{max(0, remaining):.0f}秒后再次检查（可随时停止）"
                )
                previous = seconds
            if remaining <= 0:
                return
            self.runtime._cancel_event.wait(min(1, remaining))

    def recognize(self, kind, params, frame=None):
        job = self.runtime.tasker.post_recognition(
            kind, params, self.frame if frame is None else frame
        )
        self.runtime._wait_job(job, 10)
        detail = job.get()
        return (
            [q for n in detail.nodes if n.recognition for q in n.recognition.all_results]
            if detail
            else []
        )

    def scan(self):
        from interruptions import overlay_action
        from maa.pipeline import JOCR, JRecognitionType

        for _ in range(6):
            self.runtime._check_cancelled()
            job = self.runtime.controller.post_screencap()
            self.runtime._wait_job(job, 10)
            self.frame = job.get()
            if self.frame is None:
                raise RuntimeError("星河神殿截图失败")
            self.rows = self.recognize(JRecognitionType.OCR, JOCR())
            action = overlay_action(self.find)
            if not action:
                return
            self.evidence("known-overlay")
            self.say(action[0])
            self.click(point=action[1])
        raise RuntimeError("连续活动弹窗未清理完，已暂停星河守候")

    def crop_rows(self, roi):
        from maa.pipeline import JOCR, JRecognitionType

        x, y, w, h = roi
        return [
            SimpleNamespace(
                text=q.text, score=q.score, box=(q.box[0] + x, q.box[1] + y, *q.box[2:])
            )
            for q in self.recognize(
                JRecognitionType.OCR, JOCR(), self.frame[y : y + h, x : x + w].copy()
            )
        ]

    def find(self, pattern, roi=FULL):
        x, y, w, h = roi
        return next(
            (
                q
                for q in self.rows
                if q.score >= 0.80
                and x <= q.box[0] + q.box[2] / 2 < x + w
                and y <= q.box[1] + q.box[3] / 2 < y + h
                and re.search(pattern, compact(q.text))
            ),
            None,
        )

    def click(self, row=None, point=None, delay=0.5):
        if point is None:
            x, y, w, h = row.box
            point = (int(x + w / 2), int(y + h / 2))
        self.runtime._check_cancelled()
        job = self.runtime.controller.post_click(*point)
        self.runtime._wait_job(job, 10)
        if not job.succeeded:
            raise RuntimeError("星河神殿点击失败")
        self.runtime._cancel_event.wait(delay)

    def evidence(self, name):
        from PIL import Image

        path = self.folder / f"{name}-{time.time_ns()}.png"
        Image.fromarray(self.frame[:, :, ::-1]).save(path)
        path.with_suffix(".json").write_text(
            json.dumps(
                [{"text": q.text, "box": list(q.box), "score": q.score} for q in self.rows],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.runtime.emit("INFO", f"星河证据：{path}")
        return path

    def wait_for(self, predicate, label, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.scan()
            if value := predicate():
                return value
            self.runtime._cancel_event.wait(0.35)
        self.evidence("timeout")
        raise RuntimeError(f"星河神殿等待超时：{label}；已暂停")

    def dialog_visible(self):
        return self.find("^神殿挑战$", (240, 125, 250, 65)) and self.find(
            "当前占有者", (40, 190, 180, 65)
        )

    def temple_visible(self):
        return (
            self.find("^星河神殿$", (0, 0, 250, 85))
            and self.find("^退出$", (580, 100, 140, 100))
            and not self.dialog_visible()
        )

    def title_reward(self):
        return self.find("恭喜获得") and self.find("前往装备称号")

    def seat_change_prompt(self):
        return (
            self.dialog_visible()
            and self.find("^提示$", (260, 430, 200, 90))
            and self.find("当前已占有一个神位.*挑战其他神位成功", (60, 540, 600, 120))
            and self.find("原有神位.*是否继续挑战", (60, 540, 600, 120))
        )

    def home(self):
        if self.pending:
            raise RuntimeError("星河战果尚未确认，禁止启动下一轮")
        self.scan()
        if self.dialog_visible():
            self.click(point=(680, 175))
            self.wait_for(self.temple_visible, "关闭神殿挑战")
        if (
            self.temple_visible()
            or self.find("^竞技场$", (250, 90, 220, 90))
            or self.find("^试炼之门$", (200, 90, 350, 160))
        ):
            self.click(point=(52, 1248))
        self.runtime._ensure_ready("星河神殿返回主城", allow_restart=False)
        from home_identity import identify_home

        self.account = identify_home(self.runtime, expected=self.account)
        self.evidence_record.update(account=self.account)

    def open_temple(self):
        self.home()
        self.scan()
        entry = self.find("^历练$", (420, 1190, 110, 90))
        if not entry:
            raise RuntimeError("主城历练入口未识别")
        self.click(entry)
        entry = self.wait_for(lambda: self.find("^星河神殿$", (35, 430, 260, 180)), "历练星河入口")
        self.click(entry)
        self.wait_for(self.temple_visible, "星河神殿六神位")

    def occupants(self):
        if not self.temple_visible():
            raise RuntimeError("未确认星河神殿页面，不能扫描名牌")
        names = []
        for seat in SEATS:
            rows = self.crop_rows(seat.roi)
            text = "".join(q.text for q in sorted(rows, key=lambda q: q.box[0]) if q.score >= 0.85)
            if not re.search(r"[\u4e00-\u9fffA-Za-z]", text):
                raise RuntimeError(f"{seat.name}神位名牌未读清，不能判定目标缺席")
            names.append((seat, text))
        return names

    def target_seat(self):
        for attempt in range(3):
            try:
                names = self.occupants()
                self.runtime.emit(
                    "INFO", "星河名牌：" + "；".join(f"{s.name}={n}" for s, n in names)
                )
                return next((seat for seat, name in names if target_name(name)), None)
            except RuntimeError:
                if attempt == 2:
                    self.evidence("unreadable-names")
                    raise
                self.runtime._cancel_event.wait(0.5)
                self.scan()

    def dialog_target(self):
        if not self.dialog_visible():
            return False
        rows = self.crop_rows((50, 350, 180, 45))
        text = "".join(q.text for q in sorted(rows, key=lambda q: q.box[0]) if q.score >= 0.85)
        return target_name(text)

    def ensure_evolution_one(self):
        for _ in range(3):
            self.scan()
            if not self.dialog_target():
                return False
            if evolution_selected(self.frame) == (True, False, False):
                return True
            self.click(point=(80, 972))
        self.evidence("evolution-unconfirmed")
        raise RuntimeError("未能确认只勾选进化1次，已暂停")

    def battle_home_button(self):
        from maa.pipeline import JRecognitionType, JTemplateMatch

        if self.dialog_visible() or self.temple_visible() or self.find("^布阵$", (0, 0, 720, 200)):
            return None
        matches = self.recognize(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(
                template=["trial/home_from_battle.png"],
                roi=(0, 1133, 133, 147),
                threshold=[0.78],
            ),
        )
        return next((q for q in matches if q.score >= 0.78), None)

    def resolve_battle(self):
        """One submitted challenge, one result; never launch a second battle while waiting."""
        deadline = time.monotonic() + 900
        sent_home = False
        formation_sent = False
        seat_change_confirmed = False
        while time.monotonic() < deadline:
            self.scan()
            if not seat_change_confirmed and self.seat_change_prompt():
                confirm = self.find("^确定$", (440, 715, 180, 95))
                if not confirm:
                    self.evidence("seat-change-confirm-unreadable")
                    raise RuntimeError("已有神位提示的确定按钮未读清，暂停守候")
                self.evidence("seat-change-confirm")
                self.say("已有神位，确认继续挑战目标；战斗开始后切主城等待结算")
                self.click(confirm)
                seat_change_confirmed = True
                self.wait_for(lambda: not self.seat_change_prompt(), "已有神位确认生效")
                continue
            if self.title_reward() and (confirm := self.find("^确定$")):
                self.evidence("title-reward")
                self.click(confirm)
                self.wait_for(lambda: not self.title_reward(), "称号确定生效")
                self.pending = False
                self.wins += 1
                return "won"
            if self.find("挑战失败|战斗失败|战败") and (confirm := self.find("^确定$")):
                self.evidence("defeat")
                self.click(confirm)
                self.wait_for(lambda: not self.find("挑战失败|战斗失败|战败"), "战败确认")
                self.pending = False
                self.losses += 1
                return "lost"
            if self.find("^布阵$", (150, 20, 400, 190)) and (
                battle := self.find("^战斗$", (200, 1030, 320, 180))
            ):
                if not formation_sent:
                    self.evidence("formation")
                    self.click(battle)
                    formation_sent = True
            elif not sent_home and (button := self.battle_home_button()):
                self.evidence("battle-return-home")
                self.click(button)
                sent_home = True
                self.say("已切主城，等待本场称号或战败结算")
            self.runtime._cancel_event.wait(0.6)
        self.evidence("unresolved-battle")
        raise RuntimeError("本场900秒内未确认战果，暂停守候；请先处理当前战斗再重新启动")

    def arena_state(self):
        if not self.find("^竞技场$", (250, 90, 220, 90)):
            raise RuntimeError("未确认普通竞技场页面")
        rank = self.find(r"^排名[:：]\d+名$", (40, 250, 180, 60))
        points = self.find(r"^积分[:：]\d+$", (200, 250, 200, 60))
        tickets = self.find(r"^\d[\d,]*$", (540, 250, 100, 60))
        if not rank or not points or not tickets:
            self.evidence("arena-state-unreadable")
            raise RuntimeError("竞技场排名、积分或现有券未读清，暂停补排名")
        numbers = [int(re.sub(r"\D", "", row.text)) for row in (rank, points, tickets)]
        return tuple(numbers)

    def recover_rank(self, required):
        self.home()
        self.runtime._arena_own_power = self.runtime._read_home_combat_power()
        self.runtime._post_checked("ArenaDaily")
        while True:
            self.runtime._check_cancelled()
            self.scan()
            rank, points, tickets = self.arena_state()
            if rank <= required:
                self.say(f"竞技场排名{rank}，已达到前{required}名资格")
                return True
            free = self.find("^免费挑战$", (500, 330, 180, 380))
            if not free and tickets <= 0:
                self.evidence("arena-exhausted")
                raise RuntimeError("竞技场免费次数及已有挑战券已耗尽，守候暂停")
            selected = None
            for refresh in range(4):
                own, powers = self.runtime._arena_power_state()
                candidates = [(power, i) for i, power in enumerate(powers) if power and power < own]
                if candidates:
                    _, selected = min(candidates)
                    break
                if refresh < 3:
                    self.runtime._post_checked("ArenaRefresh")
            if selected is None:
                self.say(f"刷新3次仍无低战力对手，回主城等{self.interval()}秒再检查")
                return False
            self.runtime._post_checked("ArenaEnsureSkip")
            self.scan()
            before = self.arena_state()
            y = (387, 513, 640)[selected]
            button = self.find(r"^(免费挑战|1挑战)$", (500, y - 35, 170, 70))
            if button is None or (compact(button.text) != "免费挑战" and before[2] <= 0):
                raise RuntimeError("未确认免费/已有1张挑战券按钮，暂停补排名")
            free = compact(button.text) == "免费挑战"
            self.evidence("arena-before")
            self.say(f"竞技场第{before[0]}名，挑战较低战力对手补至前{required}名")
            self.click(button)
            # Existing resolver handles the verified skip checkbox and optional formation.
            self.runtime._post_checked("ArenaResolveSkippedBattle")
            self.scan()
            after = self.arena_state()
            self.evidence("arena-after")
            if not free and after[2] != before[2] - 1:
                raise RuntimeError("竞技场挑战券变化未确认，暂停重复挑战")
            if after[0] <= required:
                return True
            if after[1] <= points:
                self.say(f"本次竞技场积分未提升，{self.interval()}秒后继续检查")
                return False

    def cycle(self, recovered_rank=False):
        self.open_temple()
        seat = self.target_seat()
        if seat is None:
            if self.last_outcome != "absent":
                self.evidence("target-absent")
            self.home()
            return "absent"
        self.evidence("target-found")
        x, y, w, h = seat.roi
        self.click(point=(x + w // 2, y + h // 2))
        self.wait_for(self.dialog_visible, "神殿挑战对话框")
        if not self.dialog_target():
            self.evidence("occupant-changed")
            self.home()
            return "changed"
        text = self.find("挑战条件.*我的排名", (70, 1070, 560, 80))
        required_rank = qualification(text.text) if text else None
        if not required_rank or required_rank[0] != seat.rank:
            self.evidence("qualification-unreadable")
            raise RuntimeError("神位资格和当前排名未核实，已暂停")
        if required_rank[1] > required_rank[0]:
            if recovered_rank:
                raise RuntimeError("竞技场达标后神殿仍提示资格不足，已暂停")
            self.say(f"资格不足，前往竞技场补至前{seat.rank}名")
            recovered = self.recover_rank(seat.rank)
            # Reopen and rescan all six seats: the enemy may have moved during arena battles.
            if recovered:
                return self.cycle(recovered_rank=True)
            self.home()
            return "qualification"
        if not self.ensure_evolution_one():
            self.home()
            return "changed"
        challenge = self.find("^挑战$", (250, 1020, 230, 65))
        if not challenge:
            raise RuntimeError("神殿挑战按钮未识别，已暂停")
        self.evidence("evolution-one-verified")
        self.evidence_record.update(pending=True)
        self.pending = True
        self.click(challenge)
        outcome = self.resolve_battle()
        self.home()
        return outcome

    @recorded
    def run_round(self):
        outcome = self.cycle()
        result(self, {"won": "success", "lost": "failed"}.get(outcome, "noop"))
        return outcome

    def run(self, max_cycles=None):
        self.runtime._load()
        self.say(f"当前角色守候 {TARGET}；每轮结束等待{self.interval()}秒")
        try:
            while True:
                self.runtime._check_cancelled()
                outcome = self.run_round()
                self.rounds += 1
                self.last_outcome = outcome
                labels = {
                    "absent": "六神位未发现目标",
                    "changed": "占有者已变化，本轮未挑战",
                    "qualification": "等待继续补资格",
                    "won": "已确认获得神位称号",
                    "lost": "本场战败，下轮继续",
                }
                self.say(f"第{self.rounds}轮：{labels[outcome]}；胜{self.wins}/败{self.losses}")
                if max_cycles is not None and self.rounds >= max_cycles:
                    return f"星河检查{self.rounds}轮：胜{self.wins}/败{self.losses}"
                self.wait_interval()
        finally:
            if self.pending:
                self.runtime.emit("WARN", "星河本场战果尚未确认；请处理完当前战斗后再启动守候")
