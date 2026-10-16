"""Screen-driven plane expedition; Maa owns capture, OCR and touch input."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import Counter
from pathlib import Path

from interruptions import overlay_action
from log_retention import recorded, result, start_record
from maa.pipeline import JOCR, JRecognitionType, JTemplateMatch
from PIL import Image
from plane_state import PlaneCycleStore, reset_window
from task_config import ACCOUNT_PRESETS


class PlaneExpeditionRunner:
    def __init__(self, runtime, account: str, store_path: Path, allow_revive=False):
        self.runtime = runtime
        self.account = account
        self.store = PlaneCycleStore(store_path)
        self.allow_revive = allow_revive
        self.floor = 0
        self.started = time.monotonic()
        self.changed = self.started
        self.actions = Counter()
        self.pending_action = None
        self.awaiting_battle = False
        self.result_seen = False
        self.cycle_checked = False
        self.cycle = self.store.get(account) or {}
        self.identity_checked = False
        self.last_timer = None
        self.reward_started = None
        self.reward_clicked = None
        start_record(self, "plane")
        self.timings = Counter()

    def progress(self):
        self.changed = time.monotonic()
        self.actions.clear()

    def checkpoint(self, **fields):
        self.cycle = self.store.checkpoint(self.account, **fields)

    def scan(self):
        self.runtime._check_cancelled()
        started = time.monotonic()
        self.captured_at = dt.datetime.now()
        self.frame = self.runtime.controller.post_screencap().get(wait=True)
        captured = time.monotonic()
        if self.frame is None:
            raise RuntimeError("位面截图失败")
        detail = self.runtime.tasker.post_recognition(JRecognitionType.OCR, JOCR(), self.frame).get(
            wait=True
        )
        self.rows = [v for n in detail.nodes if n.recognition for v in n.recognition.all_results]
        self.timings["capture_seconds"] += captured - started
        self.timings["ocr_seconds"] += time.monotonic() - captured
        self.timings["frames"] += 1

    def find(self, pattern, roi=None):
        for row in self.rows:
            x, y, w, h = row.box
            if roi and not (
                roi[0] <= x + w / 2 <= roi[0] + roi[2] and roi[1] <= y + h / 2 <= roi[1] + roi[3]
            ):
                continue
            if re.search(pattern, row.text):
                return row
        return None

    def evidence(self, state):
        started = time.monotonic()
        stamp = str(time.time_ns())
        Image.fromarray(self.frame[:, :, ::-1]).save(self.folder / (stamp + ".png"))
        with (self.folder / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "time": stamp,
                        "state": state,
                        "floor": self.floor,
                        "rows": [{"text": r.text, "box": list(r.box)} for r in self.rows],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.timings["evidence_seconds"] += time.monotonic() - started

    def click(self, state, row=None, point=None):
        if point is None:
            x, y, w, h = row.box
            point = (int(x + w / 2), int(y + h / 2))
        key = (self.floor, state, point[0] // 30, point[1] // 30)
        self.actions[key] += 1
        if self.actions[key] > 3:
            raise RuntimeError(f"位面动作无进展：{state}")
        self.evidence(state)
        self.runtime.emit("STEP", f"位面第{self.floor}层：{state}")
        self.runtime._check_cancelled()
        job = self.runtime.controller.post_click(*point)
        job.wait()
        if not job.succeeded:
            raise RuntimeError(f"位面点击失败：{state}")
        self.runtime._cancel_event.wait(1.2 if state.startswith("进入") else 0.8)

    def template(self, name, threshold, roi):
        detail = self.runtime.tasker.post_recognition(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(template=["plane/" + name], threshold=[threshold], roi=roi),
            self.frame,
        ).get(wait=True)
        for node in detail.nodes:
            if node.recognition and node.recognition.hit:
                return node.recognition.best_result
        return None

    def map_enemy(self):
        candidates = []
        # Use the arrow only to confirm reachability. Click the power plate,
        # not an offset from the animated arrow or a character's head.
        for power in self.rows:
            x, y, w, h = power.box
            if not re.fullmatch(r"\d{5,10}", power.text) or not (
                90 <= x <= 610 and 250 <= y <= 1030
            ):
                continue
            cx = int(x + w / 2)
            roi = (max(0, cx - 70), max(200, y - 240), 140, 190)
            arrow = self.template("available_arrow.png", 0.80, roi)
            if arrow:
                candidates.append((int(power.text), (cx, int(y + h / 2 - 18))))
        return max(candidates) if candidates else None

    def observe_cycle(self):
        """Read the timer crop only when establishing a cycle, not on every animation."""
        last_error = None
        for attempt in range(3):
            if attempt:
                self.runtime._cancel_event.wait(0.4)
                self.scan()
            text = self.countdown_text()
            try:
                reset_window(text, self.captured_at)
                cycle = self.store.observe(self.account, text, self.captured_at)
            except ValueError as exc:
                last_error = exc
                continue
            self.cycle, self.last_timer, self.cycle_checked = cycle, text, True
            return
        raise RuntimeError(f"位面倒计时三次局部识别未核验：{last_error}")

    def countdown_text(self):
        detail = self.runtime.tasker.post_recognition(
            JRecognitionType.OCR, JOCR(roi=(240, 155, 270, 50)), self.frame
        ).get(wait=True)
        rows = [r for n in detail.nodes if n.recognition for r in n.recognition.all_results]
        return "".join(r.text for r in sorted(rows, key=lambda r: r.box[0]))

    @recorded
    def run(self):
        try:
            return self._run()
        except Exception:
            if hasattr(self, "frame"):
                self.evidence("failed")
            raise
        finally:
            (self.folder / "timings.json").write_text(
                json.dumps(dict(self.timings), indent=2), encoding="utf-8"
            )

    def reward_overlay(self):
        if not self.find("恭喜获得", (150, 300, 420, 230)):
            self.reward_started = self.reward_clicked = None
            return False
        now = time.monotonic()
        if self.reward_started is None:
            self.reward_started = now
        if now - self.reward_started > 20:
            raise RuntimeError("位面奖励弹窗20秒未关闭，保留现场")
        # The '接好运' animation intercepts clicks although the close text behind
        # it is already visible. Waiting is not a failed action or proof of progress.
        if (
            self.find("接好运", (150, 540, 450, 200))
            or not self.find("点击空白处关闭", (100, 800, 550, 300))
            or (self.reward_clicked is not None and now - self.reward_clicked < 3)
        ):
            self.runtime._cancel_event.wait(0.8)
            return True
        self.pending_action = ("reward", None)
        self.click("关闭奖励", point=(360, 920))
        self.reward_clicked = time.monotonic()
        return True

    def _run(self):
        while time.monotonic() - self.started < 1800:
            self.scan()
            overlay = overlay_action(self.find)
            if overlay:
                self.click(overlay[0], point=overlay[1])
                continue
            if self.reward_overlay():
                continue
            title = self.find(r"第([123])层位面探险", (160, 70, 400, 100))
            if title:
                floor = int(re.search(r"第([123])层", title.text)[1])
                if floor != self.floor:
                    self.floor = floor
                    self.progress()
            if not self.identity_checked:
                role = ACCOUNT_PRESETS.get(self.account, {}).get("role_name", "")
                if not role or not self.find("^" + re.escape(role) + "$", (150, 0, 220, 60)):
                    raise RuntimeError("位面角色与当前账号不一致，未执行操作")
                self.identity_checked = True
            reset_at = self.cycle.get("reset_earliest")
            timer_visible = self.find("后重置", (160, 150, 410, 60))
            if timer_visible and (
                not self.cycle_checked
                or (reset_at and self.captured_at >= dt.datetime.fromisoformat(reset_at))
            ):
                self.observe_cycle()
                if self.cycle.get("status") in {"success", "exhausted"}:
                    self.runtime._post_checked("PlaneReturnHome")
                    return self.cycle["status"], self.cycle.get("message", "本周期已处理")
            # Progress is a verified postcondition, never animated OCR numbers.
            if self.pending_action:
                kind, old = self.pending_action
                current = self.find("请选择需要的遗物", (180, 290, 380, 130))
                names = tuple(r.text for r in self.rows if 440 < r.box[1] < 520 and r.box[2] > 45)
                if (kind == "relic" and (not current or names != old)) or (
                    kind == "reward" and not self.find("恭喜获得", (150, 300, 420, 230))
                ):
                    self.progress()
                    self.pending_action = None
            if time.monotonic() - self.changed > 120:
                raise RuntimeError("位面120秒无可验证进展")

            if self.find("开启自动探索", (100, 450, 520, 250)):
                row = self.find("^否$", (100, 680, 220, 140))
                if row:
                    self.click("取消内置自动探索", row)
                    continue
                raise RuntimeError("自动探索确认框缺少可识别的否按钮")
            if self.find("位面自动探索已结束", (100, 450, 520, 250)):
                row = self.find("^确定$", (400, 680, 250, 140))
                if row:
                    self.click("关闭自动探索结束提示", row)
                    continue
            if self.find("挑战失败", (100, 250, 520, 280)):
                close = self.find("点击空白处关闭") or self.find("^确定$", (50, 900, 180, 120))
                if self.cycle.get("revive_verified"):
                    self.store.finish(
                        self.account, "exhausted", "本周期使用一次晶石后再次战败，等待重置"
                    )
                    if close:
                        self.click("关闭复活后的失败战果", close)
                    return self.exhausted("本周期使用一次晶石后再次战败，等待重置")
                if self.cycle.get("revive_reserved"):
                    raise RuntimeError("晶石操作结果未确认，不重复使用")
                self.checkpoint(need_revive=True)
                if not close:
                    raise RuntimeError("已记录战败，未识别退出操作，保留复活待办")
                self.click("关闭失败战果准备复活", close)
                self.awaiting_battle = False
                continue
            if self.find("挑战成功", (100, 250, 520, 280)):
                row = self.find("点击空白处关闭")
                if row:
                    if not self.result_seen:
                        self.progress()
                        self.result_seen = True
                    self.awaiting_battle = False
                    self.click("关闭战果", row)
                    continue
            if self.find("不足5名|坚持出战|上阵人数", (100, 450, 520, 350)):
                self.awaiting_battle = False
                if self.cycle.get("revive_verified"):
                    confirm = self.find("^(确定|确认|坚持出战)$", (390, 680, 260, 160))
                    if not confirm:
                        raise RuntimeError("人数不足提示缺少出战确认")
                    self.click("人数不足仍继续本次挑战", confirm)
                    self.awaiting_battle = True
                else:
                    cancel = self.find("^取消$", (100, 680, 220, 160))
                    if not cancel:
                        raise RuntimeError("人数不足提示缺少取消按钮")
                    self.checkpoint(need_revive=True)
                    self.click("取消出战并准备全体复活", cancel)
                continue
            if self.awaiting_battle:
                self.runtime._cancel_event.wait(0.5)
                continue
            if self.find("^回复泉水$", (220, 100, 300, 110)):
                soak = self.find("^浸泡$", (200, 990, 320, 120))
                close = self.find("点击空白处关闭", (120, 1100, 500, 80))
                if soak:
                    self.click("浸泡泉水", soak)
                elif close:
                    self.click("关闭泉水面板", close)
                else:
                    raise RuntimeError("泉水面板没有已知操作")
                continue
            if self.find("^复活十字架$", (220, 100, 300, 110)):
                revive = self.find("^复活$", (200, 990, 320, 120))
                close = self.find("点击空白处关闭", (120, 1100, 500, 80))
                if revive:
                    self.click("使用地图十字架", revive)
                elif close:
                    self.click("关闭十字架面板", close)
                else:
                    raise RuntimeError("十字架面板没有已知操作")
                continue
            if self.find("当前所有非阵亡英雄均满血", (60, 480, 600, 180)):
                confirm = self.find("^确认$", (400, 680, 240, 150))
                if confirm:
                    self.click("满血使用泉水以通过节点", confirm)
                    continue
            if self.find("当前无阵亡英雄且存活的英雄均满血.*使用复活十字", (60, 480, 600, 180)):
                confirm = self.find("^确认$", (400, 680, 240, 150))
                if not confirm:
                    raise RuntimeError("满血十字架提示缺少确认按钮")
                self.click("满血使用地图十字架以通过节点", confirm)
                continue
            combat = self.find("守卫|首领|藏宝洞穴", (220, 130, 300, 65))
            if combat:
                if self.cycle.get("need_revive"):
                    self.click("关闭战斗面板准备复活", point=(682, 155))
                    continue
                start = self.find("^开战$", (430, 970, 220, 140))
                if start:
                    self.click("挑战守卫", start)
                    self.awaiting_battle = True
                    self.result_seen = False
                else:
                    self.click("关闭已完成战斗面板", point=(682, 155))
                continue
            if self.find("请选择需要的遗物", (180, 290, 380, 130)):
                row = self.find("^确认选择$", (20, 820, 660, 140))
                if row:
                    self.pending_action = (
                        "relic",
                        tuple(r.text for r in self.rows if 440 < r.box[1] < 520 and r.box[2] > 45),
                    )
                self.click(
                    "确认遗物" if row else "选择第一项遗物", row, None if row else (140, 590)
                )
                continue
            if self.find("^英雄雇佣$", (200, 170, 330, 140)):
                selected = self.template("hire_selected.png", 0.96, (75, 385, 70, 75))
                confirm = self.find("^确认选择$", (190, 920, 350, 120))
                if not selected:
                    self.click("选择第一位雇佣英雄", point=(106, 420))
                elif confirm:
                    self.click("确认雇佣", confirm)
                else:
                    self.click("关闭已雇佣英雄面板", point=(684, 244))
                continue
            if self.find("^旅行商人$", (220, 100, 300, 450)) and (
                self.find("^(放弃|前往)$", (180, 760, 370, 250))
                or self.find("点击空白处关闭", (120, 1000, 500, 180))
            ):
                abandon = self.find("^放弃$", (180, 760, 370, 250))
                enter = self.find("^前往$", (180, 760, 370, 250))
                close = self.find("点击空白处关闭", (120, 1000, 500, 180))
                if abandon:
                    self.click("放弃商人购物", abandon)
                elif enter:
                    self.click("打开商人事件", enter)
                elif close:
                    self.click("关闭商人", close)
                else:
                    raise RuntimeError("商人面板无已知退出操作")
                continue
            if self.find("是否花费一个", (60, 500, 600, 170)) and self.find(
                "^永恒晶石$", (200, 320, 320, 100)
            ):
                confirm = self.find("^确定$", (400, 680, 250, 160))
                if not confirm or not self.cycle.get("revive_reserved"):
                    raise RuntimeError("晶石消费确认缺少预留记录或按钮")
                if self.cycle.get("revive_commit_sent"):
                    raise RuntimeError("晶石消费确认已发送，结果待核实，不重复点击")
                self.checkpoint(revive_commit_sent=True)
                self.click("确认消耗本轮唯一一次晶石", confirm)
                self.runtime._cancel_event.wait(1)
                continue
            if self.find("^永恒晶石$", (200, 320, 320, 100)):
                count = self.find(r"^\d+$", (355, 850, 125, 70))
                cancel = self.find("^取消$", (100, 900, 230, 100))
                confirm = self.find("^确定$", (420, 900, 240, 100))
                if not count or not cancel:
                    raise RuntimeError("晶石库存或取消按钮不可读，不执行消耗")
                amount = int(count.text)
                if self.cycle.get("revive_reserved"):
                    before = self.cycle.get("crystal_before")
                    if not self.cycle.get("revive_verified"):
                        if not self.cycle.get("revive_commit_sent") and amount == before:
                            if not confirm:
                                raise RuntimeError("晶石消费确认按钮不可读")
                            self.click("打开晶石消费确认", confirm)
                            continue
                        if before is None or amount != before - 1:
                            raise RuntimeError("晶石库存未确认减少1，不重复确认")
                        self.evidence("晶石库存减少1，确认全体复活")
                        self.checkpoint(revive_verified=True, need_revive=False)
                        self.progress()
                    self.click("关闭晶石复活核验面板", cancel)
                elif self.cycle.get("need_revive"):
                    if amount == 0:
                        self.click("取消零库存晶石", cancel)
                        return self.exhausted("队伍无法继续且晶石库存为0，等待重置")
                    if not self.allow_revive:
                        raise RuntimeError("晶石复活未授权")
                    if not confirm:
                        raise RuntimeError("晶石消费确认按钮不可读")
                    self.checkpoint(crystal_before=amount)
                    if not self.store.reserve_revive(self.account):
                        raise RuntimeError("本周期晶石额度已预留，不重复使用")
                    self.cycle = self.store.get(self.account)
                    self.click("打开晶石消费确认", confirm)
                else:
                    self.click("关闭无需使用的晶石面板", cancel)
                continue
            if self.find("^难度选择$", (200, 250, 340, 140)):
                hard = self.find("^困难模式$", (400, 600, 260, 180))
                row = self.find("^确认选择$", (400, 830, 260, 120))
                if not hard or not row:
                    raise RuntimeError("无法确认困难模式及对应确认按钮")
                self.click("选择最高难度（困难模式）", row)
                continue
            finish = self.find("^结束探索$", (200, 600, 320, 260))
            if finish and self.floor == 3:
                self.evidence("通关结束探索")
                self.click("结束探索", finish)
                return self.finish("success", "已确认第三层结束探索")

            if title:
                if self.find("^提示$", (200, 350, 320, 200)):
                    raise RuntimeError("位面出现未处理提示，保留现场")
                if self.cycle.get("need_revive"):
                    crystal = self.find("永恒晶石", (50, 1160, 220, 120))
                    if crystal:
                        self.click("打开永恒晶石", crystal)
                        continue
                    raise RuntimeError("等待复活但无法识别晶石入口，不继续挑战")
                relic = self.find("未选遗物", (570, 680, 150, 220))
                if relic:
                    self.click("打开待选遗物", relic)
                    continue
                stored_hero = self.find("英雄", (590, 850, 125, 130))
                if stored_hero:
                    self.click("领取待雇佣英雄", stored_hero)
                    continue
                map_relic = self.find(
                    r"^(初级|中级|高级|稀有|史诗|传说)?遗物$", (90, 250, 540, 850)
                )
                if map_relic:
                    self.click("领取地图遗物", map_relic)
                    continue
                enemy = self.map_enemy()
                if enemy:
                    power, point = enemy
                    self.checkpoint(target=list(point))
                    self.click(f"进入可达敌人（战力{power}）", point=point)
                    continue
                for label in ("回复泉水", "复活十字架", "英雄雇佣", "旅行商人"):
                    event = self.find("^" + label + "$", (90, 250, 540, 800))
                    if event:
                        self.click("进入可达" + label, event)
                        break
                else:
                    event = None
                if event:
                    continue
                chest = self.template("reward_chest.png", 0.75, (200, 180, 320, 420))
                if chest:
                    self.click("领取层顶宝箱", chest)
                    continue
                for name in ("portal_blue.png", "portal_orange.png"):
                    portal = self.template(name, 0.78, (250, 225, 235, 325))
                    if portal:
                        self.click("进入下一层", portal)
                        break
                else:
                    self.runtime._cancel_event.wait(1)
                continue
            self.runtime._cancel_event.wait(1)
        raise RuntimeError("位面超过30分钟总时限")

    def exhausted(self, message):
        return self.finish("exhausted", message)

    def finish(self, status, message):
        if self.account:
            self.store.finish(self.account, status, message)
        self.runtime._post_checked("PlaneReturnHome")
        result(self, "success" if status == "success" else "exhausted")
        return status, message
