"""Daily objective accounting and bounded, activity-gated reward collection."""

import datetime as dt
import re

from home_identity import identify_home
from interruptions import overlay_action
from log_retention import recorded, result, start_record
from storage import DailyProgress

# These are game objectives, not arbitrary successful automation modules.
# Points are read from the purple activity reward on the same row.
SOURCES = (
    (r"登录", "登录"),
    (r"友情点", "任务助手·好友赠送"),
    (r"召唤", "每日召唤"),
    (r"竞技场", "普通竞技场"),
    (r"精灵商店", "精灵商店"),
    (r"试练塔|镇妖塔", "任务助手·塔挑战"),
    (r"快速.*战", "快速战斗"),
    (r"远航", "远航"),
    (r"无尽|试炼", "无尽试炼"),
    (r"点金", "任务助手·点金"),
    (r"公会", "任务助手·公会"),
    (r"日常副本", "任务助手·日常副本"),
    (r"挂机|收益", "挂机收益"),
    (r"探宝", "任务助手·探宝"),
    (r"装备|锻造", "任务助手·锻造"),
)
CHECKPOINT_ENTRIES = {
    "TaskAssistantDaily",
    "DailySummonDaily",
    "DailyArenaDaily",
    "ArenaDaily",
    "DailyShopRefreshDaily",
    "FastBattleVoyageDaily",
    "IdleIncomeClaimDaily",
    "EndlessTrialDaily",
}
VERIFIED_POINTS = {
    "快速作战5次": 15,
    "日常登录1次": 5,
    "每天赠送好友友情点5次": 5,
    "进行3次召唤": 10,
    "参与3次竞技场挑战": 15,
    "进行1次公会捐献": 10,
    "参与1次无尽试炼": 10,
    "接取3次远航订单": 10,
    "参与2次日常副本": 10,
    "在锻造屋合成1次装备": 15,
    "精灵商店刷新2次": 5,
    "挑战或扫荡试练塔/镇妖塔3次": 10,
    "挑战任意公会Boss3次": 15,
}


def key(text):
    return re.sub(r"[\s,，。]", "", text)


def source(title):
    return next((name for pattern, name in SOURCES if re.search(pattern, title)), "其他日常任务")


def reconcile(progress, tasks, actual, evidence):
    """Confirm the whole game's batch, then credit each newly claimed objective once."""
    old = progress.get("daily_activity").get("completed", {})
    claimed = {t["title"]: t for t in tasks.values() if t.get("claimed")}
    # A previously proved daily objective stays completed. A clipped row or
    # one missed OCR frame cannot revoke its evidence or make us claim it again.
    combined = dict(old)
    combined.update(claimed)
    total = sum(t["points"] for t in combined.values())
    if total != actual:
        raise RuntimeError(f"日常勾选积分{total}与游戏活跃{actual}不一致，未虚增或重复提交")
    completed = dict(old)
    completed.update(
        {
            title: dict(title=title, points=t["points"], source=t["source"], evidence=evidence)
            for title, t in claimed.items()
        }
    )
    progress.put("daily_activity", completed=completed, actual=actual, credited=total, pending=None)
    return [t for name, t in completed.items() if name not in old]


class DailyActivityRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.date = dt.date.today()
        start_record(self, "daily_activity")

    def scan(self):
        from maa.pipeline import JOCR, JRecognitionType

        self.runtime._check_cancelled()
        if dt.date.today() != self.date:
            raise RuntimeError("日常领奖跨过零点，停止本次操作，下轮读取新一天任务")
        job = self.runtime.controller.post_screencap()
        self.runtime._wait_job(job, 10)
        self.frame = job.get()
        if self.frame is None:
            raise RuntimeError("日常领奖截图失败")
        job = self.runtime.tasker.post_recognition(JRecognitionType.OCR, JOCR(), self.frame)
        self.runtime._wait_job(job, 10)
        detail = job.get()
        self.rows = [q for n in detail.nodes if n.recognition for q in n.recognition.all_results]

    def find(self, pattern, roi):
        x, y, w, h = roi
        return next(
            (
                q
                for q in self.rows
                if x <= q.box[0] + q.box[2] / 2 < x + w
                and y <= q.box[1] + q.box[3] / 2 < y + h
                and re.search(pattern, q.text)
            ),
            None,
        )

    def click(self, point):
        self.runtime._check_cancelled()
        job = self.runtime.controller.post_click(*map(int, point))
        self.runtime._wait_job(job, 10)
        if not job.succeeded:
            raise RuntimeError("日常领奖点击失败")
        self.runtime._cancel_event.wait(0.6)

    def evidence(self, name):
        from PIL import Image

        path = self.folder / (name + ".png")
        Image.fromarray(self.frame[:, :, ::-1]).save(path)
        return str(path)

    def activity(self):
        row = self.find(r"\d+\s*/\s*100", (25, 325, 150, 65))
        if not self.find("^日常$", (230, 90, 255, 85)) or not row:
            raise RuntimeError("日常页或真实活跃度未识别，未点击宝箱")
        value = int(re.search(r"(\d+)\s*/\s*100", row.text)[1])
        if not 0 <= value <= 500:
            raise RuntimeError("日常活跃度读数超出预期")
        return value

    def open(self):
        self.runtime._ensure_ready("Ready before activity accounting", allow_restart=False)
        self.runtime._post_checked("ResourceCycleReturnHome")
        self.account = identify_home(self.runtime)
        self.progress = DailyProgress(self.account)
        # One explicit navigation action per attempt. Never bounce between the
        # covered home page and the daily panel through an unbounded pipeline.
        for _ in range(3):
            self.scan()
            if self.find("^日常$", (230, 90, 255, 85)):
                self.panel()
                return
            self.click((393, 1110))
        self.scan()
        self.activity()

    def panel(self):
        for _ in range(8):
            self.scan()
            overlay = overlay_action(self.find)
            if overlay:
                self.click(overlay[1])
                continue
            if self.find("^当前活跃度奖励$", (200, 500, 330, 100)) and self.find(
                "^奖励$", (250, 420, 220, 90)
            ):
                confirm = self.find("^确定$", (240, 700, 260, 110))
                if confirm:
                    self.click(
                        (confirm.box[0] + confirm.box[2] / 2, confirm.box[1] + confirm.box[3] / 2)
                    )
                continue
            if self.find("恭喜获得", (150, 320, 430, 300)):
                close = self.find("点击空白处", (100, 730, 560, 370))
                if close:
                    self.click((close.box[0] + close.box[2] / 2, close.box[1] + close.box[3] / 2))
                    continue
            elif self.find("^日常$", (230, 90, 255, 85)):
                return self.activity()
            self.runtime._cancel_event.wait(0.3)
        raise RuntimeError("领奖后未返回日常页，保留现场")

    def objectives(self):
        """Bind title, activity reward and action on one row, never across rows."""
        result = []
        titles = [
            q
            for q in self.rows
            if 245 <= q.box[0] < 520
            and 390 <= q.box[1] <= 960
            and len(key(q.text)) >= 4
            and not re.fullmatch(r"[\d/]+", key(q.text))
        ]
        for row in titles:
            y = row.box[1]
            points = VERIFIED_POINTS.get(key(row.text)) or self.reward_points(y)
            button = self.find("^(提交|前往)$", (525, y + 5, 150, 95))
            count = self.find(r"\d+\s*/\s*\d+", (250, y + 20, 255, 65))
            if points:
                result.append(
                    dict(
                        title=key(row.text),
                        points=points,
                        source=source(row.text),
                        progress=count.text if count else "",
                        action=button.text if button else "",
                        button=button,
                        claimed=self.template("task_claimed.png", (545, int(y) - 5, 120, 110)),
                    )
                )
        return result

    def reward_points(self, y):
        import numpy as np
        from maa.pipeline import JOCR, JRecognitionType
        from PIL import Image

        # The tiny number overlaps the reward icon; whole-screen OCR misses it.
        crop = self.frame[int(y) + 50 : int(y) + 82, 205:244]
        crop = np.asarray(Image.fromarray(crop).resize((117, 96), Image.Resampling.BICUBIC)).copy()
        job = self.runtime.tasker.post_recognition(JRecognitionType.OCR, JOCR(), crop)
        self.runtime._wait_job(job, 10)
        values = {
            int(q.text)
            for n in job.get().nodes
            if n.recognition
            for q in n.recognition.all_results
            if re.fullmatch(r"\d{1,2}", q.text)
        }
        return next(iter(values)) if len(values) == 1 and 0 < next(iter(values)) <= 30 else None

    def remember(self, objectives):
        state = self.progress.get("daily_activity")
        catalog = dict(state.get("catalog", {}))
        for objective in objectives:
            catalog[objective["title"]] = {k: v for k, v in objective.items() if k != "button"}
        self.progress.put("daily_activity", catalog=catalog, actual=self.activity())

    def catalog(self):
        previous = None
        catalog = {}
        for index in range(7):
            self.panel()
            tasks = self.objectives()
            self.remember(tasks)
            self.evidence(f"catalog-{index}")
            for task in tasks:
                old = catalog.get(task["title"], {})
                task["claimed"] = task["claimed"] or old.get("claimed", False)
                catalog[task["title"]] = task
            names = tuple(t["title"] for t in tasks)
            if names == previous:
                break
            previous = names
            job = self.runtime.controller.post_swipe(460, 940, 460, 700, 550)
            self.runtime._wait_job(job, 10)
            if not job.succeeded:
                raise RuntimeError("日常任务列表滚动失败")
            self.runtime._cancel_event.wait(0.5)
        self.evidence("task-catalog-bottom")
        return catalog

    def submit(self):
        for index in range(14):
            before = self.panel()
            tasks = self.catalog()
            added = reconcile(self.progress, tasks, before, self.evidence(f"verified-{index}"))
            for task in added:
                self.runtime.emit(
                    "OK",
                    f"活跃记账：{task['title']}（{task['source']}）+{task['points']}；游戏累计{before}",
                )
            if not any(t["action"] == "提交" for t in tasks.values()):
                return before
            self.open()
            selected = None
            for _ in range(7):
                self.panel()
                selected = next((t for t in self.objectives() if t["action"] == "提交"), None)
                if selected:
                    break
                job = self.runtime.controller.post_swipe(460, 940, 460, 700, 550)
                self.runtime._wait_job(job, 10)
                self.runtime._cancel_event.wait(0.5)
            if selected is None:
                raise RuntimeError("待提交任务按钮未重新定位，未点击")
            self.progress.put(
                "daily_activity",
                pending=dict(
                    before=before,
                    titles=[t["title"] for t in tasks.values() if t["action"] == "提交"],
                ),
            )
            box = selected["button"].box
            self.click((box[0] + box[2] / 2, box[1] + box[3] / 2))
            after = self.panel()
            if after <= before:
                raise RuntimeError("提交后活跃度未增加，停止重复提交")
            self.evidence(f"submitted-{index}")
            # A single submit can claim every completed objective. Re-read the
            # entire list and verify its claimed stamps against the actual total.
            self.open()
        raise RuntimeError("日常提交达到14批上限，停止重复操作")

    def template(self, filename, roi):
        from maa.pipeline import JRecognitionType, JTemplateMatch

        job = self.runtime.tasker.post_recognition(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(template=["daily_task/" + filename], roi=roi, threshold=[0.85]),
            self.frame,
        )
        self.runtime._wait_job(job, 10)
        return any(n.recognition and n.recognition.hit for n in job.get().nodes)

    def chests(self, value):
        claimed = []
        for threshold, x in ((25, 247), (50, 377), (75, 507), (100, 643)):
            if value < threshold:
                continue
            self.panel()
            roi = (x - 55, 270, 110, 85)
            opened = f"chest_{threshold}_opened.png"
            if not self.template(opened, roi):
                # Eligibility comes from the actual total. The old claimable
                # template includes animated text and also resembles locked boxes.
                self.runtime.emit("INFO", f"宝箱{threshold}已达标（活跃{value}），仅提交一次")
                self.click((x, 313))  # At most one submission per chest per call.
                self.panel()
                if not self.template(opened, roi):
                    raise RuntimeError(f"{threshold}活跃宝箱未核验打开，停止重复点击")
            claimed.append(threshold)
        return claimed

    @recorded
    def run(self, final=False):
        self.open()
        value = self.submit()
        claimed = self.chests(value) if final else []
        proof = self.evidence("final" if final else "checkpoint")
        self.progress.put("daily_activity", actual=value, evidence=proof)
        if final:
            self.progress.put(
                "daily_100",
                done=value >= 100 and 100 in claimed,
                activity=value,
                chests=claimed,
                evidence=proof,
            )
            if value < 100:
                missing = [
                    f"{task['title']}（{task['progress']}，{task['points']}活跃）"
                    for task in self.progress.get("daily_activity").get("catalog", {}).values()
                    if task["action"] == "前往"
                ]
                self.runtime.emit("INFO", "未完成日常：" + "、".join(missing))
        self.runtime._post_checked("ResourceCycleReturnHome")
        result(self, "success" if final and value >= 100 and 100 in claimed else "noop")
        return value, claimed
