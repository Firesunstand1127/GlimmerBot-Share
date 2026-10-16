"""Account identity and durable daily completion records."""

import datetime as dt
import os
from pathlib import Path

from plane_state import PlaneCycleStore
from predictions import ENTRY as PREDICTION_ENTRY
from predictions import PredictionStore
from storage import read_json, write_json
from task_config import ACCOUNT_PRESETS, APP_NAME, FIXED_SERVER, TASK_COMPLETION_REVISIONS


class AccountTaskLedger:
    """Per-account, per-server daily completion ledger.

    A task is written only after MaaRuntime reports that parent entry as
    successful.  The account key is an internal whitelist key, never a role
    name or a mutable avatar/theme feature.
    """

    SCHEMA = 1

    def __init__(self):
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        self.path = local / APP_NAME / "account_task_ledger.json"
        self.plane_store = PlaneCycleStore(local / APP_NAME / "plane_cycles.json")
        self.data = self._load()

    @staticmethod
    def today() -> str:
        return dt.datetime.now().strftime("%Y-%m-%d")

    def _empty(self) -> dict:
        return {
            "schema": self.SCHEMA,
            "server": FIXED_SERVER.copy(),
            "accounts": {},
        }

    def _load(self) -> dict:
        data = read_json(self.path, self._empty())
        if not isinstance(data, dict) or data.get("schema") != self.SCHEMA:
            raise RuntimeError(f"账号账本格式不受支持：{self.path}")
        data["server"] = FIXED_SERVER.copy()
        return data

    def _day(self, account_key: str) -> dict:
        account = self._account(account_key)
        return account.setdefault("days", {}).setdefault(self.today(), {})

    def _account(self, account_key: str) -> dict:
        account = self.data.setdefault("accounts", {}).setdefault(
            account_key,
            {"label": ACCOUNT_PRESETS[account_key]["label"], "days": {}},
        )
        account["label"] = ACCOUNT_PRESETS[account_key]["label"]
        return account

    def plane_cycle(self, account_key: str) -> dict:
        return self.plane_store.get(account_key) or {}

    def plane_exhausted(self, account_key: str) -> bool:
        return self.plane_cycle(account_key).get("status") == "exhausted"

    def completed(self, account_key: str, entry: str) -> bool:
        if entry == "TeamArenaDaily":
            import time

            from team_arena import TeamArenaStore

            state = TeamArenaStore().get(account_key)
            return state.get("phase") == "challenge" and state.get("next_check", 0) > time.time()
        if entry == PREDICTION_ENTRY:
            store = PredictionStore()
            return bool(store.active(account_key)) and not store.unresolved(account_key)
        if entry == "PlaneExpeditionDaily":
            return self.plane_cycle(account_key).get("status") in {"success", "exhausted"}
        item = self._day(account_key).get(entry, {})
        if item.get("status") != "success":
            return False
        expected_revision = TASK_COMPLETION_REVISIONS.get(entry)
        return expected_revision is None or item.get("completion_revision") == expected_revision

    def mark_current(self, account_key: str):
        if account_key not in ACCOUNT_PRESETS:
            raise ValueError(f"Account is not whitelisted: {account_key}")
        self.data["current_account"] = account_key
        self.save()

    def current(self) -> str:
        value = self.data.get("current_account", "")
        return value if value in ACCOUNT_PRESETS else ""

    def day_status(self, account_key: str) -> dict:
        result = dict(self._day(account_key))
        # Calendar snapshots remain history; only an active observed cycle is current.
        result.pop("PlaneExpeditionDaily", None)
        marker = self.plane_cycle(account_key)
        if marker:
            result["PlaneExpeditionDaily"] = {
                "status": marker.get("status", "running"),
                "finished_at": (marker.get("finished_at") or "-").replace("T", " "),
                "message": marker.get("message", ""),
            }
        return result

    def record(self, account_key: str, entry: str, status: str, message: str = ""):
        if entry == "PlaneExpeditionDaily":
            marker = self.plane_cycle(account_key)
            if marker.get("status") in {"success", "exhausted"}:
                status = marker["status"]
                message = marker.get("message", message)
        self._day(account_key)[entry] = {
            "status": status,
            "finished_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": message,
            "completion_revision": TASK_COMPLETION_REVISIONS.get(entry),
        }
        self.save()

    def save(self):
        # Keep 30 calendar snapshots per account.
        for account in self.data.get("accounts", {}).values():
            days = account.get("days", {})
            for old_day in sorted(days)[:-30]:
                days.pop(old_day, None)
        write_json(self.path, self.data, durable=True)
