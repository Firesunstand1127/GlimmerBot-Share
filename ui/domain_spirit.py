"""Domain spirit trials, the daily court reward and resource-limited pillar upgrades."""

from __future__ import annotations

import json
import re
import time

from log_retention import recorded, result, start_record
from storage import DailyProgress
from task_config import ACCOUNT_PRESETS, DAILY_ACCOUNT_KEYS

ENTRY = "DomainSpiritDaily"
FULL = (0, 0, 720, 1280)
TITLE = (0, 0, 240, 75)
TRIAL_BUTTON = (230, 1090, 280, 90)


def compact(text):
    return re.sub(r"[\s,，丶]", "", text)


def fraction(text):
    match = re.search(r"(\d+(?:\.\d+)?)(万?)\s*/\s*(\d+)", compact(text))
    if not match:
        raise RuntimeError(f"域灵数值无法读取：{text}")
    return int(float(match[1]) * (10000 if match[2] else 1)), int(match[3])


def verify_challenge(before, after):
    if after != (before[0] - 1, before[1] - 1):
        raise RuntimeError(f"域灵挑战结果未确认：次数/额外奖励 {before}→{after}，停止重复挑战")


class DomainSpiritRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        start_record(self, "domain_spirit")
        self.rows = []
        self.frame = None
        self.progress = None
        self.trials_verified = False

    def scan(self):
        from maa.pipeline import JOCR, JRecognitionType

        r = self.runtime
        r._check_cancelled()
        job = r.controller.post_screencap()
        r._wait_job(job, 10)
        self.frame = job.get()
        if self.frame is None:
            raise RuntimeError("域灵截图失败")
        job = r.tasker.post_recognition(JRecognitionType.OCR, JOCR(), self.frame)
        r._wait_job(job, 10)
        detail = job.get()
        if detail is None:
            raise RuntimeError("域灵OCR失败")
        self.rows = [q for n in detail.nodes if n.recognition for q in n.recognition.all_results]

    def find(self, pattern, roi=FULL):
        x, y, w, h = roi
        return next(
            (
                row
                for row in self.rows
                if x <= row.box[0] + row.box[2] / 2 < x + w
                and y <= row.box[1] + row.box[3] / 2 < y + h
                and re.search(pattern, compact(row.text))
            ),
            None,
        )

    def wait_for(self, pattern, roi=FULL, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.scan()
            if row := self.find(pattern, roi):
                return row
            self.runtime._cancel_event.wait(0.35)
        self.evidence("unexpected")
        raise RuntimeError(f"域灵页面未出现：{pattern}")

    def click(self, row=None, point=None):
        if point is None:
            x, y, w, h = row.box
            point = (int(x + w / 2), int(y + h / 2))
        r = self.runtime
        r._check_cancelled()
        job = r.controller.post_click(*point)
        r._wait_job(job, 10)
        if not job.succeeded:
            raise RuntimeError("域灵点击失败")
        r._cancel_event.wait(0.65)

    def evidence(self, name):
        from PIL import Image

        path = self.folder / f"{name}-{time.time_ns()}.png"
        Image.fromarray(self.frame[:, :, ::-1]).save(path)
        path.with_suffix(".json").write_text(
            json.dumps(
                [{"text": q.text, "box": list(q.box)} for q in self.rows],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(path)

    def identify(self):
        from maa.pipeline import JOCR, JRecognitionType

        account = None
        for attempt in range(3):
            self.scan()
            # Crop before OCR so VIP digits cannot be merged with the name.
            job = self.runtime.tasker.post_recognition(
                JRecognitionType.OCR, JOCR(roi=(170, 0, 175, 55)), self.frame
            )
            self.runtime._wait_job(job, 10)
            detail = job.get()
            candidates = (
                [q for n in detail.nodes if n.recognition for q in n.recognition.all_results]
                if detail
                else []
            )
            for role in candidates:
                name = re.sub(r"[\W_丶]", "", role.text)
                account = next(
                    (
                        key
                        for key in DAILY_ACCOUNT_KEYS
                        if name == re.sub(r"[\W_丶]", "", ACCOUNT_PRESETS[key]["role_name"])
                    ),
                    None,
                )
                if account:
                    break
            if account:
                break
            if attempt < 2:
                self.runtime._cancel_event.wait(0.4)
        if account is None:
            raise RuntimeError("域灵日常未识别到白名单主城角色")
        # The SDK ledger can be stale after the user changes accounts manually.
        # Scope these consuming steps to the role actually visible on the device.
        self.progress = DailyProgress(account)
        self.account = account
        self.runtime.emit("INFO", f"域灵日常：已核验角色 {role.text}（{account}）")

    def open(self):
        self.identify()
        self.runtime._post_checked(ENTRY)
        self.wait_hub()
        self.evidence("hub-before")
        self.click(self.find("^域灵试炼$", (110, 730, 230, 100)))
        self.wait_for("挑战次数", (260, 1020, 200, 80))

    def wait_hub(self):
        self.wait_for("^域灵荣院$", (350, 570, 260, 100))
        if not self.find("^域灵柱$", (300, 330, 250, 100)):
            raise RuntimeError("域灵主页锚点不完整")

    def trial_counts(self):
        attempts = self.find("挑战次数", (250, 1020, 230, 80))
        extra = self.find(r"\d/3", (455, 1060, 125, 55))
        if not attempts or not self.find("^域灵试炼$", TITLE):
            raise RuntimeError("域灵试炼挑战次数或额外奖励未识别")
        left, total = fraction(attempts.text)
        if extra:
            bonus, bonus_total = fraction(extra.text)
        elif left <= 7 and not self.find("额外奖励", (455, 1020, 135, 90)):
            # The entire extra-reward bubble disappears after the third battle.
            bonus, bonus_total = 0, 3
        else:
            raise RuntimeError("域灵试炼额外奖励未识别，次数尚未证明三场完成")
        if total != 10 or bonus_total != 3 or not 0 <= bonus <= 3 or not 0 <= left <= 10:
            raise RuntimeError("域灵试炼次数超出预期")
        return left, bonus

    def resolve_battle(self):
        deadline = time.monotonic() + 220
        captured = False
        skipped = False
        while time.monotonic() < deadline:
            self.scan()
            # The trial's counters remain visible behind the result overlay.
            # Dismiss and verify the foreground first, before accepting the page.
            if self.find("当前战斗|本次记录伤害|挑战奖励", (155, 435, 410, 310)):
                confirm = self.find("^确定$", (80, 925, 200, 110))
                if not confirm:
                    self.runtime._cancel_event.wait(0.35)
                    continue
                self.evidence("battle-result")
                self.click(confirm)
                continue
            if self.find("挑战次数", (250, 1020, 230, 80)) and not self.find(
                "确定|点击空白处", (70, 875, 550, 160)
            ):
                return
            skip = self.find("^跳过$", (640, 430, 80, 100))
            if not skipped and skip and self.find("回合", (270, 0, 180, 65)):
                self.evidence("battle-skip")
                self.click(skip)
                skipped = True
                continue
            if not captured:
                self.runtime.emit("INFO", "域灵试炼：等待战斗结算")
                self.evidence("battle-start")
                captured = True
            self.runtime._cancel_event.wait(1)
        self.evidence("battle-timeout")
        raise RuntimeError("域灵试炼战斗未在220秒内结算")

    def open_formation(self, before):
        # This button only opens formation; the separate 开战 action consumes
        # the attempt. Retry a missed navigation click only on unchanged counts.
        for attempt in range(3):
            self.scan()
            if self.find("^英雄出战$", (220, 60, 290, 100)):
                return
            if self.trial_counts() != before:
                raise RuntimeError("域灵挑战次数已变化，停止重复进入出战")
            row = self.find("^挑战$", TRIAL_BUTTON)
            if row is None:
                raise RuntimeError("域灵挑战入口未识别")
            self.click(row)
            for _ in range(6):
                self.scan()
                if self.find("^英雄出战$", (220, 60, 290, 100)):
                    return
                self.runtime._cancel_event.wait(0.35)
        self.evidence("formation-unopened")
        raise RuntimeError("域灵出战页面连续3次未打开")

    def challenges(self):
        self.scan()
        before = self.trial_counts()
        pending = self.progress.get("domain_spirit_trial").get("pending")
        if pending:
            verify_challenge(tuple(pending), before)
            self.progress.put("domain_spirit_trial", pending=None)
            self.runtime.emit("OK", f"域灵试炼：恢复并核验上次挑战，当前{before}")
        self.evidence("trial-before")
        # The three daily extra rewards are the game's durable daily counter.
        # Already-used bonuses are never replaced by additional challenges.
        for _ in range(before[1]):
            if before[0] <= 0:
                raise RuntimeError("域灵试炼剩余挑战不足，未购买次数")
            self.open_formation(before)
            start = self.find("^开战$", (240, 1080, 250, 90))
            if not start:
                raise RuntimeError("域灵试炼开战按钮未识别")
            self.progress.put("domain_spirit_trial", pending=list(before))
            self.click(start)
            self.resolve_battle()
            after = self.trial_counts()
            verify_challenge(before, after)
            self.progress.put("domain_spirit_trial", pending=None, counts=list(after))
            self.evidence(f"challenge-{3 - after[1]}-verified")
            self.runtime.emit(
                "OK",
                f"域灵试炼：第{3 - after[1]}/3次已核验，次数{before[0]}→{after[0]}，额外奖励{before[1]}→{after[1]}",
            )
            before = after
        self.trials_verified = before[1] == 0

    def back_to_hub(self):
        self.click(self.wait_for("^返回$", (30, 1180, 170, 100)))
        self.wait_hub()

    def pillar_values(self):
        self.wait_for("^消耗$", (270, 975, 180, 75))
        level = self.find(r"\d+级", (300, 165, 280, 80))
        resources = sorted(
            (r for r in self.rows if "/" in r.text and 1035 <= r.box[1] <= 1080),
            key=lambda r: r.box[0],
        )
        if not level or len(resources) not in (2, 3):
            raise RuntimeError("域灵柱等级或完整消耗资源未识别")
        levels = [int(n) for n in re.findall(r"(\d+)级", level.text)]
        if len(levels) != 2 or levels[1] != levels[0] + 1:
            raise RuntimeError(f"域灵柱升级等级异常：{level.text}")
        # Breakthroughs add a material and spread the columns across the row.
        # An outer column also prevents treating a missing middle OCR as two costs.
        expected = (
            3
            if any(
                r.box[0] + r.box[2] / 2 < 240 or r.box[0] + r.box[2] / 2 > 550 for r in resources
            )
            else 2
        )
        if len(resources) != expected:
            raise RuntimeError(f"域灵柱{levels[1]}级需识别{expected}项资源，实际{len(resources)}项")
        return (levels[0], *(fraction(r.text) for r in resources))

    def upgrade_pillar(self):
        self.wait_hub()
        self.click(self.find("^域灵柱$", (300, 330, 250, 100)))
        before = self.pillar_values()
        initial_level = before[0]
        self.evidence("pillar-before")
        for _ in range(500):
            level, *resources = before
            if any(owned < cost for owned, cost in resources):
                self.evidence("pillar-exhausted")
                self.runtime.emit(
                    "OK",
                    f"域灵柱：{initial_level}→{level}级，资源"
                    + "、".join(f"{owned}/{cost}" for owned, cost in resources)
                    + "不足，停止升级",
                )
                self.progress.put("domain_spirit_pillar", level=level, exhausted=True)
                self.back_to_hub()
                return initial_level, level
            upgrade = self.find("^升级$", (240, 1100, 260, 85))
            if not upgrade:
                raise RuntimeError("域灵柱升级按钮未识别")
            self.click(upgrade)
            after = self.pillar_values()
            if after[0] != level + 1:
                self.evidence("pillar-upgrade-unconfirmed")
                raise RuntimeError(f"域灵柱等级未增加：{level}→{after[0]}")
            self.runtime.emit("OK", f"域灵柱升级已核验：{level}→{after[0]}")
            before = after
        raise RuntimeError("域灵柱达到500次升级上限，资源仍足，未标记完成")

    def matches(self, template, roi, threshold=0.7):
        from maa.pipeline import JRecognitionType, JTemplateMatch

        r = self.runtime
        job = r.tasker.post_recognition(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(template=["domain_spirit/" + template], roi=roi, threshold=[threshold]),
            self.frame,
        )
        r._wait_job(job, 10)
        detail = job.get()
        return bool(detail and any(n.recognition and n.recognition.hit for n in detail.nodes))

    def dismiss_reward(self, name):
        self.wait_for("恭喜获得", (180, 370, 370, 130))
        proof = self.evidence(name)
        close = self.find("点击空白处关闭界面", (120, 800, 460, 180))
        if not close:
            close = self.wait_for("点击空白处关闭界面", (120, 800, 460, 180))
        self.click(close)
        for _ in range(10):
            self.scan()
            if not self.find("恭喜获得", (180, 370, 370, 130)):
                return proof
            self.runtime._cancel_event.wait(0.25)
        raise RuntimeError("域灵奖励弹窗未关闭")

    def stage_cards(self):
        damage = self.find(r"\d", (0, 480, 310, 80))
        value = re.fullmatch(r"(\d+(?:\.\d+)?)(万?)", compact(damage.text)) if damage else None
        if not value:
            raise RuntimeError("域灵试炼最高伤害未识别")
        highest = float(value[1]) * (10000 if value[2] else 1)
        cards = []
        for row in self.rows:
            x, y, w, h = row.box
            if not (20 <= x + w / 2 <= 570 and 960 <= y + h / 2 <= 1010):
                continue
            value = re.fullmatch(r"(\d+(?:\.\d+)?)万", compact(row.text))
            if value:
                cards.append((int(x + w / 2), float(value[1]) * 10000 <= highest))
        if len(cards) < 4:
            raise RuntimeError("域灵试炼阶段奖励条目未完整识别")
        return cards

    def claim_stages(self):
        # The counters may update before the last result overlay animates in.
        self.runtime._cancel_event.wait(0.7)
        self.resolve_battle()
        self.scan()
        self.trial_counts()
        self.evidence("stages-before")
        cards = self.stage_cards()
        outstanding = [
            x
            for x, reached in cards
            if reached and not self.matches("stage_claimed.png", (max(0, x - 55), 875, 110, 90))
        ]
        if outstanding:
            # Live sampling verified that any reached reward claims every
            # available stage at once, including stages outside the viewport.
            claimable = next(
                (
                    x
                    for x in outstanding
                    if self.matches("reward_dot.png", (x + 17, 860, 45, 45), 0.85)
                ),
                None,
            )
            if claimable is None:
                raise RuntimeError("域灵阶段勾选或红点识别不一致，未点击物品")
            self.click(point=(claimable, 920))
            self.dismiss_reward("stages-reward")
            self.wait_for("^阶段奖励$", (250, 805, 225, 70))
        for x, reached in self.stage_cards():
            if reached and not self.matches("stage_claimed.png", (max(0, x - 55), 875, 110, 90)):
                raise RuntimeError("域灵试炼阶段奖励未出现领取勾选")
        self.evidence("stages-claimed")
        self.runtime.emit("OK", "域灵试炼：已达到的阶段奖励领取状态已核验")
        self.back_to_hub()

    def court_row_state(self, row):
        y = row.box[1]
        if not self.find("^1/1$", (520, y - 8, 130, 60)):
            raise RuntimeError("每日挑战1次域灵试炼进度尚未达到1/1")
        if self.matches("court_claimed.png", (510, y + 25, 145, 115), 0.86):
            return "claimed", None
        claim = self.find("^领取$", (510, y + 35, 150, 100))
        if claim:
            return "available", claim
        raise RuntimeError("域灵每日任务已完成但领取状态未识别")

    def claim_daily(self):
        if not self.trials_verified:
            raise RuntimeError("域灵三次挑战尚未核验，不能判断每日奖励已完成")
        self.wait_hub()
        self.click(self.find("^域灵荣院$", (350, 570, 260, 100)))
        self.wait_for("^域灵任务$", (230, 130, 260, 90))
        row = self.find("^每日挑战1次域灵试炼$", (35, 230, 460, 620))
        proof = self.evidence("daily-task-front")
        if row:
            state, claim = self.court_row_state(row)
            if state == "available":
                self.click(claim)
                proof = self.dismiss_reward("daily-task-reward")
                self.wait_for("^域灵任务$", (230, 130, 260, 90))
                self.runtime.emit("OK", "域灵荣院：已领取首屏每日挑战奖励并确认奖励弹窗")
            else:
                self.runtime.emit("OK", "域灵荣院：首屏每日任务已领取")
        else:
            # After the verified trials, an unclaimed daily task is promoted
            # to the front. Claimed tasks are below; do not scroll to them.
            if not self.find("建筑达到|任意域灵|域灵试炼伤害", (35, 230, 460, 150)):
                raise RuntimeError("域灵荣院首屏任务内容未识别")
            self.runtime.emit("OK", "域灵荣院：挑战已完成，首屏无待领每日任务，直接返回")
        self.progress.put("domain_spirit_daily_reward", done=True, evidence=proof)
        self.click(point=(690, 185))
        self.wait_hub()

    @recorded
    def run(self):
        try:
            self.open()
            self.challenges()
            self.claim_stages()
            self.claim_daily()
            start, end = self.upgrade_pillar()
            self.evidence("hub-after")
            self.click(self.find("^返回$", (30, 1180, 170, 100)))
            self.runtime._ensure_ready("域灵日常返回主城Ready", allow_restart=False)
            self.scan()
            proof = self.evidence("home-ready")
            message = f"域灵试炼每日三场、阶段奖励和荣院奖励已核验；域灵柱{start}→{end}级，资源不足停止；主城Ready"
            self.runtime._completion_messages[ENTRY] = message
            self.runtime.emit("OK", f"{message}；证据：{proof}")
            result(self, "success")
        except Exception:
            if self.frame is not None:
                self.evidence("failed")
            raise
