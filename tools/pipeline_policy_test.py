"""Regression checks for spending and termination rules.

These checks intentionally fail the build if a future pipeline edit bypasses
the Voyage intelligence threshold or enables Voyage task acceleration.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = ROOT / "assets" / "resource" / "pipeline"


def load(name: str) -> dict:
    return json.loads((PIPELINE_DIR / name).read_text(encoding="utf-8"))


def main() -> None:
    fast = load("fast_battle_voyage.json")
    voyage = load("voyage.json")

    app_source = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert "screencap_methods=MaaAdbScreencapMethodEnum.EmulatorExtras" in app_source
    assert "RawWithGzip" not in app_source, "ADB screencap fallback is forbidden"
    assert "input_methods=MaaAdbInputMethodEnum.Maatouch" in app_source

    assert "FastBattleEnterVoyage" not in fast
    assert voyage["VoyageRefreshLoop"]["next"] == [
        "VoyagePriorityEpicLegendary",
        "VoyagePriorityRainRebuild",
        "VoyageRefreshOnce",
    ]
    assert voyage["VoyageCycleFinished"]["action"] == "StopTask"
    for name in (
        "VoyageCancelAcceleration",
        "VoyageUnacceptedHighWarningCancel",
    ):
        assert voyage[name]["next"] == ["VoyageCycleFinished"], name
    for name in ("VoyagePriorityEpicLegendary", "VoyagePriorityRainRebuild"):
        assert voyage[name]["next"] == ["VoyageDetailAssign", "VoyageCycleFinished"]
    assert voyage["VoyageDetailAssign"]["max_hit"] == 1
    assert voyage["VoyageDetailDispatch"]["max_hit"] == 1
    assert voyage["VoyageDetailDispatch"]["next"] == ["VoyageCycleFinished"]
    assert voyage["VoyageRefreshOnce"]["max_hit"] == 1
    assert "VoyageCycleFinished" in voyage["VoyageRefreshOnce"]["next"]
    assert "VoyageValueAtLeast2000" not in voyage

    cancel = voyage["VoyageCancelAcceleration"]
    assert cancel["action"] == "Click" and cancel["box_index"] == 1
    allowed_acceleration_guards = {
        "VoyageCancelAcceleration",
        "VoyageAccelerationPromptFeature",
        "VoyageAccelerationCancelFeature",
    }
    forbidden = [
        name
        for name in voyage
        if "Acceleration" in name and name not in allowed_acceleration_guards
    ]
    assert not forbidden, f"Voyage acceleration nodes are forbidden: {forbidden}"

    print("PIPELINE POLICY PASSED: single merchant step returns; acceleration forbidden")


if __name__ == "__main__":
    main()
