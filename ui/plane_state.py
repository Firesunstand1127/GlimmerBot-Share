"""Persistent expedition cycles inferred from the game's countdown.

Countdowns are assumed to truncate the smallest displayed unit. A display of
``1天12小时`` locates reset in [now + 36 hours, now + 37 hours). No wall-clock
date parity or fixed 48-hour delay is used.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from storage import read_json, write_json


def reset_window(text: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    clock = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})[:：](\d{2})\s*(?:后重置)?\s*", text)
    if clock:
        hours, minutes, seconds = map(int, clock.groups())
        if hours > 47 or minutes > 59 or seconds > 59:
            raise ValueError(f"Invalid plane countdown: {text!r}")
        lower = now + dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
        return lower, lower + dt.timedelta(seconds=1)
    units = {"天": 86400, "小时": 3600, "分钟": 60, "分": 60, "秒": 1}
    matches = re.findall(r"(\d+)\s*(天|小时|分钟|分|秒)", text)
    if not matches:
        raise ValueError(f"Unrecognized plane countdown: {text!r}")
    seen = set()
    seconds = 0
    for number, unit in matches:
        scale = units[unit]
        if scale in seen:
            raise ValueError(f"Repeated countdown unit: {text!r}")
        seen.add(scale)
        seconds += int(number) * scale
    lower = now + dt.timedelta(seconds=seconds)
    return lower, lower + dt.timedelta(seconds=min(seen))


class PlaneCycleStore:
    """One account record per observed cycle; revive reservation is irreversible.

    ``get`` releases terminal results at the earliest possible reset so callers
    can inspect the countdown again. Reservations stay bound to the old record
    until ``observe`` confirms a new cycle. Callers must serialize operations;
    atomic replacement protects file integrity, not simultaneous reservations
    from multiple processes.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema": 1, "accounts": {}}
        data = read_json(self.path, {"schema": 1, "accounts": {}})
        if not isinstance(data, dict) or not isinstance(data.get("accounts", {}), dict):
            raise ValueError("Invalid plane cycle store")
        data.setdefault("accounts", {})
        return data

    def _save(self, data: dict) -> None:
        write_json(self.path, data, durable=True)

    @staticmethod
    def _now(now: dt.datetime | None) -> dt.datetime:
        return now if now is not None else dt.datetime.now()

    @staticmethod
    def _active(record: dict, now: dt.datetime) -> bool:
        return bool(record.get("reset_latest")) and now < dt.datetime.fromisoformat(
            record["reset_latest"]
        )

    def get(self, account: str, now: dt.datetime | None = None) -> dict | None:
        record = self._load()["accounts"].get(account, {})
        now = self._now(now)
        if not self._active(record, now):
            return None
        if record.get("status") in {"success", "exhausted"} and now >= dt.datetime.fromisoformat(
            record["reset_earliest"]
        ):
            return None
        return record

    def observe(self, account: str, text: str, now: dt.datetime | None = None) -> dict:
        now = self._now(now)
        lower, upper = reset_window(text, now)
        data = self._load()
        record = data["accounts"].setdefault(account, {})
        new_cycle = not self._active(record, now)
        if not new_cycle:
            old_lower = dt.datetime.fromisoformat(record["reset_earliest"])
            old_upper = dt.datetime.fromisoformat(record["reset_latest"])
            overlap_lower, overlap_upper = max(lower, old_lower), min(upper, old_upper)
            if overlap_lower < overlap_upper:
                lower, upper = overlap_lower, overlap_upper
            elif overlap_lower - overlap_upper <= dt.timedelta(seconds=1):
                # A frame/OCR boundary can shift an observation by one second.
                # Keep the established interval, rather than widen it each scan.
                lower, upper = old_lower, old_upper
            elif now >= old_lower and lower >= old_upper:
                new_cycle = True
            else:
                raise ValueError("Countdown conflicts with the current unexpired plane cycle")
        if new_cycle:
            # Keep extension fields while replacing only fields owned here.
            record.update(
                status="running",
                message="",
                revive_reserved=False,
                revive_reserved_at=None,
                finished_at=None,
                observed_at=now.isoformat(),
                need_revive=False,
                revive_verified=False,
                revive_commit_sent=False,
                target=None,
                crystal_before=None,
            )
        record.update(
            reset_earliest=lower.isoformat(),
            reset_latest=upper.isoformat(),
            countdown=text,
            last_observed_at=now.isoformat(),
        )
        self._save(data)
        return record

    def checkpoint(
        self,
        account: str,
        *,
        need_revive: bool | None = None,
        revive_verified: bool | None = None,
        target=None,
        revive_commit_sent: bool | None = None,
        crystal_before: int | None = None,
        now: dt.datetime | None = None,
    ) -> dict:
        """Save recovery state; target=None leaves an existing target unchanged.

        Use target=[] to clear a target. The remaining fields cannot alter cycle
        identity, terminal results or the irreversible revival reservation.
        """
        data = self._load()
        record = data["accounts"].get(account, {})
        if not self._active(record, self._now(now)):
            raise ValueError("Observe an active plane cycle before checkpointing")
        for name, value in (
            ("need_revive", need_revive),
            ("revive_verified", revive_verified),
            ("revive_commit_sent", revive_commit_sent),
        ):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be boolean")
                record[name] = value
        if target is not None:
            record["target"] = target
        if crystal_before is not None:
            if type(crystal_before) is not int or crystal_before < 0:
                raise ValueError("crystal_before must be a nonnegative integer")
            record["crystal_before"] = crystal_before
        self._save(data)
        return record

    def reserve_revive(self, account: str, now: dt.datetime | None = None) -> bool:
        now = self._now(now)
        data = self._load()
        record = data["accounts"].get(account, {})
        if (
            not self._active(record, now)
            or record.get("revive_reserved")
            or record.get("status") in {"success", "exhausted"}
        ):
            return False
        record.update(revive_reserved=True, revive_reserved_at=now.isoformat())
        self._save(data)
        return True

    def finish(
        self, account: str, status: str, message: str, now: dt.datetime | None = None
    ) -> None:
        if status not in {"success", "exhausted", "failed"}:
            raise ValueError(f"Unsupported plane result: {status}")
        now = self._now(now)
        data = self._load()
        record = data["accounts"].get(account, {})
        if not self._active(record, now):
            raise ValueError("Observe an active plane cycle before recording its result")
        record.update(status=status, message=message, finished_at=now.isoformat())
        self._save(data)
