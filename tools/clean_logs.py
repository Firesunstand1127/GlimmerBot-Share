"""Preview or apply the desktop runtime's retention rules, without any device access."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
from log_retention import LogRetention


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Exact runtime logs directory")
    parser.add_argument("--apply", action="store_true", help="Delete eligible historical files")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if root.name.lower() != "logs":
        parser.error("The target must be the runtime logs directory")
    retention = LogRetention(root)
    retention.protect_running_apps()
    report = retention.cleanup(dry_run=not args.apply)
    report["root"] = str(root)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return bool(report["errors"])


if __name__ == "__main__":
    sys.exit(main())
