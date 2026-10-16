"""Atomic JSON storage and per-account daily action checkpoints."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import time
from pathlib import Path

_JSON_LOCK = threading.RLock()


def read_json(path: Path, default):
    # Windows readers can briefly prevent replacing the destination file.
    # Keep UI reads and worker writes from overlapping in this process.
    with _JSON_LOCK:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"状态文件无法读取，保留原文件：{path}") from exc


def write_json(path: Path, data, *, durable=False):
    with _JSON_LOCK:
        _write_json(path, data, durable=durable)


def _write_json(path: Path, data, *, durable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(0.05 * 2**attempt)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class DailyProgress:
    def __init__(self, account: str | None):
        self.account = account
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        self.path = local / "MaaShanShuoShare" / "task_progress.json"
        self.memory = {}

    def _load(self):
        data = read_json(self.path, {}) if self.account else self.memory
        days = data.setdefault(self.account or "session", {})
        today = dt.date.today().isoformat()
        for day in sorted(days)[:-30]:
            days.pop(day)
        return data, days.setdefault(today, {})

    def get(self, key):
        return self._load()[1].get(key, {})

    def put(self, key, **fields):
        data, day = self._load()
        day.setdefault(key, {}).update(fields)
        if self.account:
            write_json(self.path, data, durable=True)
