"""Screen-verified ranking likes; no combat or season-calendar assumptions."""

from __future__ import annotations

import json
import re
import time

from log_retention import recorded, result, start_record

ENTRY = "RankingLikesDaily"
KINDS = ("竞技场", "在线锦标赛", "巅峰冠军赛", "跨服天梯", "世界竞技场")
FULL = (0, 0, 720, 1280)
WORLD_PODIUM = {1: (285, 420, 155, 75), 2: (85, 535, 155, 75), 3: (490, 540, 155, 75)}


def compact(text):
    return re.sub(r"[\s,，丶]", "", text)


def button_state(frame, box):
    import numpy as np

    x, y, w, h = box
    # Sample the thumb itself. Vote-count digits are black/white and can
    # incorrectly make a gold button look gray or unknown as the count grows.
    patch = frame[y + 5 : y + h - 5, x + 4 : x + w - 4].astype(np.int16)
    if not patch.size:
        return "unknown"
    chroma = np.median(patch.max(axis=2) - patch.min(axis=2))
    return "gold" if chroma > 45 else "gray" if chroma < 18 else "unknown"


def assign_ranks(buttons, anchors, spacing):
    """Use recognized rank badges/digits; infer missing digits only on the same row grid."""
    assigned = {}
    for rank, ay in anchors:
        if not buttons:
            continue
        index = min(range(len(buttons)), key=lambda i: abs(buttons[i][1] + 17 - ay))
        if abs(buttons[index][1] + 17 - ay) > 50:
            continue
        for i, box in enumerate(buttons):
            delta = (box[1] - buttons[index][1]) / spacing
            if abs(delta - round(delta)) > 0.10:
                continue
            candidate = rank + round(delta)
            if candidate < 1:
                continue
            if i in assigned and assigned[i] != candidate:
                raise RuntimeError("排行榜名次识别冲突，停止点赞")
            assigned[i] = candidate
    return [(assigned[i], box) for i, box in enumerate(buttons) if i in assigned]


def digit_anchors(rows):
    # Scrolling can turn a 9 into a low-confidence 6. Do not let that digit
    # override the remaining, consistent rank anchors on the same frame.
    return [
        (int(q.text), q.box[1] + q.box[3] / 2)
        for q in rows
        if re.fullmatch(r"[4-9]|1[0-9]|20", q.text)
        and q.score >= 0.90
        and 75 <= q.box[0] < 135
        and 260 < q.box[1] < 875
    ]


class RankingLikesRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        start_record(self, "ranking_likes")
        self.rows = []
        self.frame = None
        self.results = {}
        self.world_podium = False

    def recognize(self, kind, params):
        job = self.runtime.tasker.post_recognition(kind, params, self.frame)
        self.runtime._wait_job(job, 10)
        detail = job.get()
        return (
            [q for n in detail.nodes if n.recognition for q in n.recognition.all_results]
            if detail
            else []
        )

    def scan(self):
        from maa.pipeline import JOCR, JRecognitionType

        self.runtime._check_cancelled()
        job = self.runtime.controller.post_screencap()
        self.runtime._wait_job(job, 10)
        self.frame = job.get()
        if self.frame is None:
            raise RuntimeError("排行榜截图失败")
        self.rows = self.recognize(JRecognitionType.OCR, JOCR())

    def find(self, pattern, roi=FULL):
        x, y, w, h = roi
        return next(
            (
                q
                for q in self.rows
                if x <= q.box[0] + q.box[2] / 2 < x + w
                and y <= q.box[1] + q.box[3] / 2 < y + h
                and re.search(pattern, compact(q.text))
            ),
            None,
        )

    def wait_for(self, pattern, roi=FULL, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.scan()
            if row := self.find(pattern, roi):
                return row
            self.runtime._cancel_event.wait(0.3)
        self.evidence("unexpected")
        raise RuntimeError(f"排行榜页面未出现：{pattern}")

    def click(self, row=None, point=None):
        if point is None:
            x, y, w, h = row.box
            point = (int(x + w / 2), int(y + h / 2))
        self.runtime._check_cancelled()
        job = self.runtime.controller.post_click(*point)
        self.runtime._wait_job(job, 10)
        if not job.succeeded:
            raise RuntimeError("排行榜点击失败")
        self.runtime._cancel_event.wait(0.8)

    def swipe(self, begin, end):
        self.runtime._check_cancelled()
        job = self.runtime.controller.post_swipe(*begin, *end, 1000)
        self.runtime._wait_job(job, 10)
        if not job.succeeded:
            raise RuntimeError("排行榜滑动失败")
        self.runtime._cancel_event.wait(0.8)

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
        return str(path)

    def matches(self, name, roi, threshold=0.84):
        from maa.pipeline import JRecognitionType, JTemplateMatch

        rows = self.recognize(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(template=[f"ranking/{name}.png"], roi=roi, threshold=[threshold]),
        )
        return [q for q in rows if q.score >= threshold]

    def buttons(self, roi, world=False):
        prefix = "world_" if world else ""
        matches = self.matches(prefix + "thumb_gold", roi) + self.matches(
            prefix + "thumb_gray", roi
        )
        boxes = []
        for q in sorted(matches, key=lambda q: q.score, reverse=True):
            if not any(abs(q.box[0] - b[0]) < 25 and abs(q.box[1] - b[1]) < 25 for b in boxes):
                boxes.append(tuple(q.box))
        return sorted(boxes, key=lambda b: (b[1], b[0]))

    def go_home(self):
        for _ in range(6):
            self.scan()
            if self.find("^跨服战场$", (100, 300, 550, 600)) and self.find(
                "商店|镇妖塔", (50, 300, 550, 600)
            ):
                self.runtime._ensure_ready("排行榜点赞返回主城", allow_restart=False)
                return
            if self.find("^英雄殿$", (250, 150, 220, 80)):
                self.click(point=(680, 195))
            elif row := self.find("^返回$", (0, 1070, 200, 110)):
                self.click(row)
            else:
                self.runtime._ensure_ready("排行榜点赞准备主城", allow_restart=False)
                return
        raise RuntimeError("排行榜返回主城超出次数")

    def open_kind(self, kind):
        self.go_home()
        self.scan()
        if kind == "竞技场":
            for _ in range(3):
                self.scan()
                if row := self.find("^竞技场$", (0, 300, 650, 650)):
                    self.click(row)
                    break
                self.swipe((620, 650), (120, 650))
            else:
                self.evidence("竞技场入口缺失")
                raise RuntimeError("主城滑动后未找到竞技场入口")
            self.scan()
            if row := self.find("^进入挑战$", (220, 1040, 300, 110)):
                self.click(row)
            self.click(self.wait_for("^排名榜$", (180, 170, 200, 80)))
            self.wait_for("我的排名", (0, 880, 200, 160))
            return True
        # Same visible cross-server menu for all four modes.
        for _ in range(3):
            self.scan()
            if row := self.find("^跨服战场$", (100, 300, 550, 600)):
                self.click(row)
                break
            self.swipe((620, 650), (120, 650))
        self.click(self.wait_for("^" + kind + "$", (70, 220, 550, 800)))
        self.wait_for("^天梯争霸$" if kind == "跨服天梯" else "^" + kind + "$", (0, 0, 270, 110))
        if kind == "跨服天梯":
            self.click(self.wait_for("^英雄殿$", (0, 170, 160, 110)))
            self.wait_for("每日点赞", (0, 1010, 500, 80))
            return True
        if kind == "世界竞技场":
            # Open days display recordings and matching; closed days expose ranking.
            # Never infer completion from the weekday alone.
            self.scan()
            self.world_podium = self.world_podium_visible()
            if self.world_podium:
                return True
            row = self.find("^排行榜$|^排名榜$", (0, 100, 720, 1000))
            if row is None and self.find("^开始匹配$", (220, 970, 300, 120)):
                self.evidence("世界竞技场-开放日无排行榜")
                self.results[kind] = "暂不可点赞：当前开放页无排行榜，下次运行重查"
                return False
            if row is None:
                self.evidence("世界竞技场-待适配页面")
                self.results[kind] = "待实机确认：未识别排行榜入口"
                return False
            self.click(row)
        else:
            # Click the visible label; retry only navigation, never a like submission.
            for _ in range(2):
                row = self.wait_for("^排行榜$", (610, 70, 110, 130))
                self.click(row)
                self.scan()
                if self.find(kind + "排行榜", (150, 100, 400, 80)):
                    break
        self.wait_for("排行榜|排名榜", (0, 100, 700, 140))
        return True

    def verify_like(self, kind, rank, box):
        x, y, w, h = box
        state = button_state(self.frame, box)
        if state == "gray":
            self.runtime.emit("INFO", f"{kind} 第{rank}名：灰色按钮，已点赞或不可点赞")
            return "gray"
        if state != "gold":
            self.evidence(f"{kind}-{rank}-unknown-color")
            raise RuntimeError(f"{kind} 第{rank}名按钮状态不明")
        self.evidence(f"{kind}-{rank}-before")
        self.click(point=(x + w + 30, y + h // 2))
        for _ in range(8):
            if kind in {"竞技场", "在线锦标赛", "巅峰冠军赛"}:
                # The list can finish scrolling after the click. Confirm by
                # recognized rank on a fresh frame, not the old pixel position.
                ranked = self.ranked_rows(kind)
                current = next((b for r, b in ranked if r == rank), None)
            else:
                self.scan()
                roi = (max(0, x - 8), max(0, y - 12), 145, 65)
                candidates = (
                    self.buttons(roi, world=True)
                    if kind == "世界竞技场" and self.world_podium
                    else self.buttons(roi)
                )
                current = next(
                    (b for b in candidates if abs(b[0] - x) < 12 and abs(b[1] - y) < 12), None
                )
            if current and button_state(self.frame, current) == "gray":
                self.evidence(f"{kind}-{rank}-confirmed")
                self.runtime.emit("OK", f"{kind} 第{rank}名：点赞后已核验按钮变灰")
                return "liked"
            self.runtime._cancel_event.wait(0.35)
        self.evidence(f"{kind}-{rank}-unconfirmed")
        raise RuntimeError(f"{kind} 第{rank}名点赞结果未确认，停止重复点击")

    def ranked_rows(self, kind):
        for attempt in range(3):
            self.scan()
            if not self.find("排行榜|排名榜", (0, 100, 700, 140)) or not self.find(
                "我的排名", (0, 880, 220, 180)
            ):
                raise RuntimeError(f"{kind} 榜单锚点缺失")
            boxes = self.buttons((500, 260, 155, 620))
            anchors = []
            for rank in (1, 2, 3):
                for q in self.matches(f"rank{rank}", (75, 260, 75, 620), 0.92):
                    anchors.append((rank, q.box[1] + q.box[3] / 2))
            if kind == "竞技场":
                anchors += digit_anchors(self.rows)
            try:
                ranked = assign_ranks(boxes, anchors, 129 if kind == "竞技场" else 125)
                if not ranked:
                    raise RuntimeError(f"{kind} 未识别到可核对的排名和点赞按钮")
                return ranked
            except RuntimeError as exc:
                path = self.evidence(f"{kind}-rank-retry-{attempt + 1}")
                self.runtime.emit(
                    "WARN",
                    f"{exc}；原地核对 {attempt + 1}/3，anchors={anchors}，"
                    f"buttons={boxes}，现场={path}",
                )
                if attempt == 2:
                    raise
                self.runtime._cancel_event.wait(0.6)

    def list_likes(self, kind, limit):
        done = {}
        for page in range(10):
            ranked = self.ranked_rows(kind)
            for rank, box in ranked:
                if rank > limit or rank in done:
                    continue
                # Never reuse coordinates collected before earlier likes/scans.
                fresh = next((b for r, b in self.ranked_rows(kind) if r == rank), None)
                if fresh is not None:
                    done[rank] = self.verify_like(kind, rank, fresh)
            if all(rank in done for rank in range(1, limit + 1)):
                self.evidence(kind + "-complete")
                return done
            if kind != "竞技场":
                break
            # Small, slow movement retains row overlap and avoids flinging past rank 6.
            self.swipe((450, 760), (450, 560))
        raise RuntimeError(f"{kind} 排名核验不完整：{sorted(done)} / 前{limit}名")

    def hall_likes(self):
        done = {}
        for rank, (left, right) in {1: (270, 465), 2: (45, 250), 3: (475, 685)}.items():
            self.scan()
            if not self.find("^英雄殿$", (250, 150, 220, 80)) or not self.find(
                "每日点赞", (0, 1010, 500, 80)
            ):
                raise RuntimeError("天梯英雄殿锚点缺失")
            boxes = self.buttons((left, 810, right - left, 175))
            if len(boxes) != 1:
                raise RuntimeError(f"英雄殿第{rank}名按钮未唯一识别")
            done[rank] = self.verify_like("跨服天梯", rank, boxes[0])
        return done

    def world_podium_visible(self):
        return bool(
            self.find("^世界竞技场$", (0, 0, 270, 100))
            and self.find("^赛季荣耀$", (390, 100, 165, 65))
            and all(len(self.buttons(roi, world=True)) == 1 for roi in WORLD_PODIUM.values())
        )

    def world_likes(self):
        done = {}
        for rank, roi in WORLD_PODIUM.items():
            for attempt in range(4):
                self.scan()
                boxes = self.buttons(roi, world=True)
                if (
                    self.find("^世界竞技场$", (0, 0, 270, 100))
                    and self.find("^赛季荣耀$", (390, 100, 165, 65))
                    and len(boxes) == 1
                ):
                    done[rank] = self.verify_like("世界竞技场", rank, boxes[0])
                    break
                self.runtime._cancel_event.wait(0.4)
            else:
                self.evidence(f"世界竞技场-{rank}-missing")
                raise RuntimeError(f"世界竞技场赛季荣耀第{rank}名按钮未完整识别")
        self.evidence("世界竞技场-complete")
        return done

    @recorded
    def run(self, kinds=KINDS):
        for kind in kinds:
            if not self.open_kind(kind):
                self.runtime.emit("INFO", self.results[kind])
                continue
            if kind == "跨服天梯":
                done = self.hall_likes()
            elif kind == "世界竞技场" and self.world_podium:
                done = self.world_likes()
            else:
                done = self.list_likes(kind, 10 if kind == "竞技场" else 3)
            self.results[kind] = (
                f"核验{len(done)}名，新增{sum(v == 'liked' for v in done.values())}次点赞"
            )
        self.go_home()
        (self.folder / "result.json").write_text(
            json.dumps(self.results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result(
            self,
            "success"
            if self.results and all(value.startswith("核验") for value in self.results.values())
            else "unknown",
        )
        return self.results
