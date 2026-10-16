"""Discover home reminder buttons; dispatch only supported tournament handlers."""

import os
import time
from pathlib import Path

from predictions import KINDS, PredictionRunner, normalized
from storage import read_json, write_json
from task_config import DAILY_ACCOUNT_KEYS

INTERVAL = 900


class ReminderStore:
    def __init__(self, path=None):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        self.path = path or local / "MaaShanShuoShare" / "home_reminders.json"

    def get(self, account):
        return read_json(self.path, {}).get(account, {})

    def put(self, account, **fields):
        data = read_json(self.path, {})
        data.setdefault(account, {}).update(fields)
        write_json(self.path, data, durable=True)

    def due(self):
        return [k for k in DAILY_ACCOUNT_KEYS if self.get(k).get("next_check", 0) <= time.time()]


def reminder_rows(rows, frame):
    """Restrict candidates to the reminder area and a yellow button background."""
    found = []
    for row in rows:
        x, y, w, h = map(int, row.box)
        if w < 60 or len(normalized(row.text)) < 2:
            continue
        if not (60 <= x + w / 2 < 360 and 850 <= y + h / 2 < 1110):
            continue
        patch = frame[max(0, y - 4) : y + h + 4, max(0, x - 4) : x + w + 4]
        if not patch.size:
            continue
        b, g, r = (patch[:, :, i].astype("int16") for i in range(3))
        yellow = (r > 180) & (g > 120) & (r > b + 45) & (g > b + 20)
        if float(yellow.mean()) >= 0.35 and normalized(row.text):
            found.append(row)
    return found


def check(runtime, account, *, force=False):
    store = ReminderStore()
    old = store.get(account)
    if not force and old.get("next_check", 0) > time.time():
        return old.get("message", "等待下次检查")
    scanner = PredictionRunner(runtime, account)
    scanner.scan()
    scanner.verify_role()
    rows = reminder_rows(scanner.rows, scanner.frame)
    now = time.time()
    catalog = old.get("items", {})
    visible = []
    for row in rows:
        text = normalized(row.text)
        visible.append(text)
        item = catalog.setdefault(text, {"first_seen": now})
        known = any(text == normalized(k + "竞猜") for k in KINDS)
        if "evidence" not in item:
            item["evidence"] = scanner.evidence("home-reminder")
        item.update(text=row.text, last_seen=now, supported=known)
    # Back off even if a supported handler fails, avoiding idle retry storms.
    store.put(
        account,
        items=catalog,
        visible=visible,
        last_check=now,
        next_check=now + INTERVAL,
        message="已识别，正在检查竞猜",
    )
    result = scanner.run()
    unknown = [catalog[t]["text"] for t in visible if not catalog[t]["supported"]]
    message = result or "暂无可处理竞猜"
    if unknown:
        message += "；仅记录：" + "、".join(unknown)
    store.put(account, message=message)
    runtime.emit("INFO", "主城提醒：" + message)
    return message
