"""Team-arena matchup planning and renewable challenge scheduling."""

import datetime as dt
import os
import re
import time
from itertools import permutations
from pathlib import Path

from log_retention import recorded, result
from storage import read_json, write_json
from task_config import APP_NAME, DAILY_ACCOUNT_KEYS

ENTRY = "TeamArenaDaily"
RESTORE_SECONDS = 3 * 3600
FINISH_MARGIN = 90 * 60
THRESHOLD = 5


def countdown(text):
    match = re.search(r"(?:(\d+)天)?(\d{1,2})[:：](\d{2})[:：](\d{2})", text.replace(" ", ""))
    if not match:
        return None
    days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
    if hours >= 24 or minutes >= 60 or seconds >= 60:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def next_check(now, attempts, restore_at, closes_at, total):
    """Use estimated recovery only to wake up; spending always rereads the device."""
    if closes_at <= now:
        return now + 1800
    if attempts == 0 and total >= 17:
        return closes_at + 30
    if attempts and (attempts >= THRESHOLD or total == 0 or closes_at - now <= FINISH_MARGIN):
        return now
    if restore_at is None:
        # No displayed recovery can mean all 15 recoveries were used up.
        return min(now + 3600, closes_at + 30)
    target = restore_at + max(0, THRESHOLD - attempts - 1) * RESTORE_SECONDS + 10
    if closes_at - now > FINISH_MARGIN:
        target = min(target, closes_at - FINISH_MARGIN)
    else:
        target = restore_at + 10
    return max(now + 30, min(target, closes_at + 30))


class TeamArenaStore:
    def __init__(self):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        self.path = local / APP_NAME / "team_arena.json"

    def get(self, account):
        return read_json(self.path, {}).get(account, {})

    def put(self, account, **fields):
        if account not in DAILY_ACCOUNT_KEYS:
            return
        data = read_json(self.path, {})
        data.setdefault(account, {}).update(fields)
        write_json(self.path, data, durable=True)

    def due(self, now=None):
        now = time.time() if now is None else now
        return [key for key in DAILY_ACCOUNT_KEYS if self.get(key).get("next_check", 0) <= now]

    def summary(self, account):
        data = self.get(account)
        if not data:
            return "尚未读取次数"
        stamp = dt.datetime.fromtimestamp(data["next_check"]).strftime("%m-%d %H:%M")
        return f"{data.get('message', '')}；下次检查 {stamp}"


class TeamArenaRunner:
    def __init__(self, runtime):
        self.r = runtime
        self.store = TeamArenaStore()
        self.account = runtime._progress().account
        from log_retention import start_record

        start_record(self, "team_arena")

    def defer(self, message, seconds=1800):
        self.store.put(
            self.account, next_check=time.time() + seconds, message=message, phase="waiting_open"
        )
        self.r._defer(ENTRY, message)

    def timing(self, attempts, total):
        rows = self.r._ocr_results("TeamArenaClockScan")
        now = time.time()
        closing = next((countdown(q.text) for q in rows if "挑战关闭" in q.text), None)
        restore = next((countdown(q.text) for q in rows if "次数恢复" in q.text), None)
        if closing is None:
            raise RuntimeError("组队斗技未读到挑战关闭倒计时")
        closes_at = now + closing
        restore_at = now + restore if restore is not None else None
        scheduled = next_check(now, attempts, restore_at, closes_at, total)
        self.store.put(
            self.account,
            phase="challenge",
            observed_at=now,
            attempts=attempts,
            total=total,
            restore_at=restore_at,
            closes_at=closes_at,
            next_check=scheduled,
            message=f"剩余 {attempts} 次，本期已挑战 {total} 次",
        )
        self.r.emit(
            "INFO",
            f"Team arena: attempts={attempts}, total={total}, close_in={closing}s, "
            f"restore_in={restore}s, next_check={dt.datetime.fromtimestamp(scheduled)}",
        )
        return scheduled <= now

    def chests(self):
        r = self.r
        r._post_checked("TeamArenaOpenChests")
        total = None
        claimed = 0
        for _ in range(5):
            rows = r._ocr_results("TeamArenaChestScan")
            fractions = []
            buttons = []
            for row in rows:
                x, y, w, h = r._box_parts(row)
                if x < 500:
                    continue
                match = re.search(r"(\d+)\s*/\s*(\d+)", row.text)
                if match:
                    fractions.append((int(match[1]), int(match[2]), y + h / 2))
                if row.text.strip() == "领取":
                    buttons.append((int(x + w / 2), int(y + h / 2)))
            if not fractions:
                raise RuntimeError("组队挑战宝箱未读到累计挑战次数")
            total = max(done for done, _, _ in fractions)
            target = next(
                (
                    point
                    for done, need, y in fractions
                    if done >= need
                    for point in buttons
                    if 35 <= point[1] - y <= 110
                ),
                None,
            )
            if target is None:
                break
            r._post_checked(
                "TeamArenaChestClaim",
                {
                    "TeamArenaChestClaim": {
                        "target": [*target, 1, 1],
                        "roi": [500, target[1] - 35, 150, 75],
                    }
                },
            )
            # Receipt popup confirms the claim, then return to this same list.
            self.evidence(f"chest-{claimed + 1}")
            r._post_checked("TeamArenaChestRewardClose")
            claimed += 1
        else:
            raise RuntimeError("组队挑战宝箱领取后仍重复可领，停止")
        r._post_checked("TeamArenaCloseChests")
        r.emit("OK", f"Team arena chests: claimed={claimed}, total_challenges={total}")
        return total

    def evidence(self, name):
        from PIL import Image

        job = self.r.controller.post_screencap()
        self.r._wait_job(job, 10)
        frame = job.get()
        folder = self.folder
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.account}-{name}-{time.time_ns()}.png"
        Image.fromarray(frame[:, :, ::-1]).save(path)
        self.r.emit("INFO", f"Team arena evidence: {path}")

    @recorded
    def run(self):
        r = self.r
        now = dt.datetime.now()
        weekday = now.isoweekday()
        if weekday not in (5, 6) and not (weekday == 7 and now.hour < 23):
            friday = (now + dt.timedelta(days=(4 - now.weekday()) % 7)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            self.defer("未到入口开放时间，等待开放中", (friday - now).total_seconds())
            return
        r._post_checked(ENTRY)
        self.resume_pending()
        status = r._team_arena_status()
        if status is not None:
            rows = r._ocr_results("TeamArenaClockScan")
            opening = next(
                (
                    countdown(q.text)
                    for q in rows
                    if any(label in q.text for label in ("挑战开启", "开启挑战", "组队开放"))
                ),
                None,
            )
            r._post_checked("TeamArenaReturnHome")
            self.defer(
                "处于组队期，等待开放中" if status == "grouping" else "当前不在挑战期，等待开放中",
                max(60, opening + 10) if opening is not None else 1800,
            )
            return
        total = self.chests()
        attempts = r._read_team_arena_attempts()
        due = self.timing(attempts, total)
        if getattr(r, "team_arena_resident", False) and not due:
            r._post_checked("TeamArenaReturnHome")
            r._defer(ENTRY, self.store.summary(self.account))
            return
        completed = 0
        while attempts > 0:
            r.emit(
                "TASK", f"Team arena challenge {completed + 1}; attempts before battle={attempts}"
            )
            r._team_arena_in_flight = True
            r._post_checked("TeamArenaOpenMatch")
            r._accept_team_arena_match()
            r._arrange_team_arena()
            r._post_checked("TeamArenaStartBattle")
            r._post_checked("TeamArenaBattleResolve")
            after = r._read_team_arena_attempts()
            r._verify_attempts(attempts, after, "组队")
            r._team_arena_in_flight = False
            attempts = after
            completed += 1
            self.evidence(f"battle-{completed}")
        total = self.chests()
        self.timing(attempts, total)
        r._post_checked("TeamArenaReturnHome")
        r.emit(
            "OK", f"Team arena completed: {completed} battles; {self.store.summary(self.account)}"
        )
        result(self, "success")

    def resume_pending(self):
        r = self.r
        options = r._ocr_results("TeamArenaMatchOptionsScan")
        if not any(q.text.strip() in ("保存调整", "确定") for q in options):
            return False
        before = r._read_team_arena_attempts()
        r._team_arena_in_flight = True
        r._accept_team_arena_match()
        r._arrange_team_arena()
        r._post_checked("TeamArenaStartBattle")
        r._post_checked("TeamArenaBattleResolve")
        r._verify_attempts(before, r._read_team_arena_attempts(), "组队")
        r._team_arena_in_flight = False
        self.evidence("resumed-battle")
        return True


def merge_enemy_powers(previous, names, powers):
    """Only combine observations of the same three distinctly named opponents."""
    if len(names) != 3 or any(not name for name in names) or len(set(names)) != 3:
        return {}, list(powers)
    known = dict(previous) if set(previous) == set(names) else dict.fromkeys(names)
    for name, power in zip(names, powers):
        if power is not None:
            known[name] = power
    return known, [known[name] for name in names]


def scout_order(runtime):
    known = {}
    for attempt in range(3):
        own, enemy = runtime._team_arena_order_powers()
        names = getattr(runtime, "_team_arena_order_names", [])
        known, enemy = merge_enemy_powers(known, names, enemy)
        runtime.emit("INFO", f"Team scout {attempt}: names={names}, enemy={enemy}")
        if all(power is not None for power in enemy) or attempt == 2 or not known:
            return own, enemy
        runtime._post_checked("TeamArenaCloseOrder")
        runtime._post_checked("TeamArenaReopenOrder")


def enemy_names(results, box_parts):
    names = []
    for low, high in ((145, 190), (455, 505), (770, 815)):
        names.append(
            next(
                (
                    re.sub(r"[\s,，丶、·….]", "", row.text)
                    for row in results
                    if box_parts(row)[0] >= 400
                    and low <= box_parts(row)[1] + box_parts(row)[3] / 2 <= high
                ),
                None,
            )
        )
    return names


def best_order(own, enemy):
    """Prefer known wins, then the weakest winning margin; hidden power is unknown."""

    def score(order):
        ratios = [a / b for a, b in zip(order, enemy) if b is not None and a > b]
        return len(ratios), min(ratios, default=0), -sum(a != b for a, b in zip(order, own))

    order = max(permutations(own), key=score)
    return list(order), score(order)[0]
