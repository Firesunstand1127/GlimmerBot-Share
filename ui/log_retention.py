"""Bound runtime evidence; never touch settings, ledgers or repository evidence."""

from __future__ import annotations

import ctypes
import functools
import json
import os
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from storage import write_json

LIMIT = 1024**3
MAX_AGE = 3 * 86400
FEATURES = (
    "galaxy_temple",
    "daily_activity",
    "domain_spirit",
    "ranking_likes",
    "predictions",
    "plane",
    "team_arena",
    "failures",
)
_LOCK = threading.RLock()
_MANAGERS = {}


def process_alive(pid):
    if pid == os.getpid():
        return True
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied: preserve it.
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def files_under(path):
    """Do not follow Windows junctions, symlinks or other reparse points."""
    if not path.exists() or linked(path):
        return []
    if path.is_file():
        return [path]
    result = []
    for base, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not linked(Path(base) / name)]
        result.extend(Path(base) / name for name in names if not linked(Path(base) / name))
    return result


@dataclass
class Unit:
    path: Path
    files: list
    feature: str
    account: str
    status: str
    stamp: float
    protected: bool = False

    @property
    def size(self):
        return sum(info.st_size for _, info in self.files)


class LogRetention:
    def __init__(self, root, limit=LIMIT, max_age=MAX_AGE):
        self.root = Path(root).resolve()
        self.limit = limit
        self.max_age = max_age
        self.last_cleanup = 0
        self.running = False
        self.last_report = None

    def protect_running_apps(self):
        if os.name != "nt":
            return
        command = (
            "Get-Process -Name MaaShanShuoShare -ErrorAction SilentlyContinue | "
            "ForEach-Object { @{pid=$_.Id; started=([DateTimeOffset]$_.StartTime)."
            "ToUnixTimeSeconds()} } | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.stdout.strip():
            rows = json.loads(completed.stdout)
            for row in rows if isinstance(rows, list) else [rows]:
                if row["pid"] != os.getpid():
                    self.protect_legacy(row["pid"], row["started"])

    def protect_legacy(self, pid, started):
        """Register an already-running pre-retention EXE before migration cleanup."""
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / ".legacy-processes.json"
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            old[str(pid)] = started
            write_json(path, old)

    def _legacy_cutoff(self):
        path = self.root / ".legacy-processes.json"
        if not path.exists():
            return float("inf")
        data = json.loads(path.read_text(encoding="utf-8"))
        return min(
            (float(started) for pid, started in data.items() if process_alive(int(pid))),
            default=float("inf"),
        )

    def _safe(self, path):
        path = Path(path)
        if not path.is_relative_to(self.root) or path == self.root:
            return False
        for part in (path, *path.parents):
            if part.exists() and linked(part):
                return False
            if part == self.root:
                break
        return path.resolve().is_relative_to(self.root)

    def _unit(self, path, feature, now, cutoff):
        if not self._safe(path):
            return None
        files = [(file, file.stat()) for file in files_under(path)]
        if not files:
            return None
        newest = max(info.st_mtime for _, info in files)
        meta = path / "record.json" if path.is_dir() else None
        try:
            data = json.loads(meta.read_text(encoding="utf-8")) if meta and meta.exists() else {}
        except (ValueError, OSError):
            data = {"pending": True}  # Corrupt metadata is not deletion authority.
        active = data.get("status") == "active" and process_alive(data.get("pid"))
        # Old versions have no manifests: preserve any run touched since they started.
        protected = active or data.get("pending", False) or (not data and newest >= cutoff)
        if not data and newest >= now - 120:
            protected = True
        status = data.get("status", "unknown")
        if status == "active" and not active:
            status = "incomplete"
        return Unit(
            path,
            files,
            feature + (":" + data["feature_entry"] if data.get("feature_entry") else ""),
            str(data.get("account") or "legacy-unknown"),
            status,
            float(data.get("finished", newest)),
            bool(protected),
        )

    def _inventory(self, now, feature_only=None):
        cutoff = self._legacy_cutoff()
        units = []
        for feature in (feature_only,) if feature_only else FEATURES:
            folder = self.root / feature
            if not folder.exists() or linked(folder):
                continue
            for path in folder.iterdir():
                # Old flat evidence is grouped with its recognition sidecar.
                if path.suffix == ".json" and path.with_suffix(".png").exists():
                    continue
                if not path.is_dir() and path.suffix not in {".png", ".json", ".jsonl"}:
                    continue
                unit = self._unit(path, feature, now, cutoff)
                if unit:
                    sidecar = path.with_suffix(".json")
                    if path.is_file() and path.suffix == ".png" and sidecar.exists():
                        if not self._safe(sidecar):
                            continue
                        info = sidecar.stat()
                        unit.files.append((sidecar, info))
                        unit.protected |= info.st_mtime >= min(cutoff, now - 120)
                    units.append(unit)
        if feature_only:
            return units
        for folder in (self.root / "framework", self.root / "debug"):
            for path in files_under(folder):
                if "config" in path.relative_to(folder).parts:
                    continue
                if path.suffix not in {".log", ".png", ".json", ".jsonl"}:
                    continue
                # Native backups are closed files. The current log remains protected.
                backup = path.name.startswith("maafw.bak.")
                unit = self._unit(path, "framework", now, float("inf") if backup else cutoff)
                if unit:
                    units.append(unit)
        for path in self.root.glob("run-*.log*"):
            if path.is_file():
                unit = self._unit(path, "text", now, cutoff)
                if unit:
                    units.append(unit)
        return units

    def _remove(self, unit):
        # Recheck every file before removing any part of a record. In-progress or
        # changed legacy data wins over the quota; never recursively delete a path.
        if unit.path.is_dir() and set(files_under(unit.path)) != {p for p, _ in unit.files}:
            return 0, 0
        for path, old in unit.files:
            if not self._safe(path):
                return 0, 0
            new = path.stat()
            if (new.st_size, new.st_mtime_ns) != (old.st_size, old.st_mtime_ns):
                return 0, 0
        freed = count = 0
        for path, old in sorted(unit.files, key=lambda item: item[0].name == "record.json"):
            path.unlink()
            freed += old.st_size
            count += 1
        if unit.path.is_dir():
            for base, _, _ in os.walk(unit.path, topdown=False, followlinks=False):
                folder = Path(base)
                if self._safe(folder):
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
        return freed, count

    def cleanup(self, *, dry_run=False, feature_only=None):
        with _LOCK:
            now = time.time()
            if feature_only is not None and feature_only not in FEATURES:
                raise ValueError("Unknown feature")
            all_files = files_under(self.root / feature_only if feature_only else self.root)
            total = sum(path.stat().st_size for path in all_files)
            units = self._inventory(now, feature_only)
            selected, groups = {}, {}
            for unit in units:
                if unit.protected:
                    continue
                bucket = unit.status if unit.status in {"success", "noop"} else "diagnostic"
                group = (unit.feature, unit.account, bucket)
                groups.setdefault(group, []).append(unit)
            for (_, _, status), records in groups.items():
                records.sort(key=lambda unit: unit.stamp, reverse=True)
                keep = 1 if status in {"success", "noop"} else 3
                for i, unit in enumerate(records):
                    if i >= keep or (status != "success" and now - unit.stamp > self.max_age):
                        selected[unit.path] = unit
            remaining = total - sum(unit.size for unit in selected.values())
            for unit in sorted(units, key=lambda unit: unit.stamp):
                if remaining <= self.limit:
                    break
                # Keep the latest complete success, current run and unresolved battle.
                if unit.protected or unit.path in selected or unit.status == "success":
                    continue
                selected[unit.path] = unit
                remaining -= unit.size
            freed = count = 0
            errors = []
            for unit in selected.values():
                try:
                    size, number = (unit.size, len(unit.files)) if dry_run else self._remove(unit)
                    freed += size
                    count += number
                except OSError as exc:
                    errors.append(f"{unit.path}: {exc}")
            after = (
                total - freed
                if dry_run
                else sum(
                    path.stat().st_size
                    for path in files_under(self.root / feature_only if feature_only else self.root)
                )
            )
            report = {
                "before": total,
                "freed": freed,
                "after": after,
                "files": count,
                "protected": sum(unit.size for unit in units if unit.protected),
                "over_limit": after > self.limit,
                "errors": errors,
                "dry_run": dry_run,
            }
            if not dry_run and not feature_only:
                self.last_cleanup = time.monotonic()
                self.last_report = report
            return report

    def maintain(self, callback=None, *, force=False):
        # UI requests must never wait behind a disk scan or deletion batch.
        if not _LOCK.acquire(blocking=False):
            return
        try:
            if self.running or (not force and time.monotonic() - self.last_cleanup < 60):
                return
            self.running = True
        finally:
            _LOCK.release()

        def work():
            try:
                result = self.cleanup()
            except (OSError, ValueError) as exc:
                result = {"errors": [str(exc)]}
                self.last_cleanup = time.monotonic()
            finally:
                self.running = False
            if callback:
                callback(result)

        threading.Thread(target=work, daemon=True, name="log-retention").start()


def manager(root):
    root = Path(root).resolve()
    if root in _MANAGERS:
        return _MANAGERS[root]
    with _LOCK:
        if root not in _MANAGERS:
            _MANAGERS[root] = LogRetention(root)
        return _MANAGERS[root]


class EvidenceRecord:
    def __init__(self, root, feature, account=None):
        if feature not in FEATURES:
            raise ValueError(f"Unknown evidence feature: {feature}")
        self.manager = manager(root)
        self.feature = feature
        self.folder = self.manager.root / feature / str(time.time_ns())
        self.outcome = "unknown"
        self.closed = False
        self.data = {
            "feature": feature,
            "account": account,
            "status": "active",
            "pid": os.getpid(),
            "started": time.time(),
            "pending": False,
        }
        self.update()

    def update(self, **fields):
        with _LOCK:
            self.data.update(fields)
            write_json(self.folder / "record.json", self.data)

    def finish(self, outcome=None, *, account=None, pending=False):
        with _LOCK:
            self.update(
                status=outcome or self.outcome,
                finished=time.time(),
                pending=pending,
                account=account or self.data.get("account"),
            )
            self.closed = True
            # Empty no-op rounds do not replace the most recent screenshot evidence.
            if not pending and files_under(self.folder) == [self.folder / "record.json"]:
                (self.folder / "record.json").unlink()
                self.folder.rmdir()
        # Prune this feature promptly. Full-directory scans are at most once/minute.
        try:
            self.manager.cleanup(feature_only=self.feature)
            if time.monotonic() - self.manager.last_cleanup >= 60:
                self.manager.cleanup()
        except (OSError, ValueError):
            # The next scheduled/UI cleanup will report errors; never alter a battle result.
            pass


def start_record(runner, feature):
    runtime = getattr(runner, "runtime", None) or runner.r
    account = getattr(runner, "account", None)
    if account is None and hasattr(runtime, "_progress"):
        account = runtime._progress().account
    # Test doubles and unverified identities must not become persistent account keys.
    account = account if isinstance(account, str) else None
    runner.evidence_record = EvidenceRecord(runtime.log_dir, feature, account)
    runner.folder = runner.evidence_record.folder


def result(runner, outcome):
    record = getattr(runner, "evidence_record", None)
    if record:
        record.outcome = outcome


def append_log(path, message, max_bytes=5 * 1024**2, backups=3):
    with _LOCK:
        if path.exists() and path.stat().st_size + len(message.encode("utf-8")) > max_bytes:
            for number in range(backups, 0, -1):
                source = path if number == 1 else Path(f"{path}.{number - 1}")
                if source.exists():
                    source.replace(Path(f"{path}.{number}"))
        with path.open("a", encoding="utf-8") as stream:
            stream.write(message)


def recorded(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        record = getattr(self, "evidence_record", None)
        if record is None:
            return method(self, *args, **kwargs)
        if record.closed:
            start_record(self, record.feature)
            record = self.evidence_record
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            record.outcome = "incomplete" if type(exc).__name__ == "RunCancelled" else "failed"
            raise
        finally:
            runtime = getattr(self, "runtime", None) or self.r
            pending = (
                getattr(self, "pending", record.data.get("pending", False)) is True
                or getattr(runtime, "_team_arena_in_flight", False) is True
                or getattr(self, "awaiting_battle", False) is True
            )
            record.finish(account=getattr(self, "account", None), pending=pending)

    return wrapped
