"""Static integrity audit for every MaaShanShuo pipeline bundle.

This intentionally avoids executing the game.  It catches broken node links,
missing templates, out-of-bounds 720x1280 coordinates, and public entries that
would otherwise appear to finish without doing any useful work.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINES = ROOT / "assets" / "resource" / "pipeline"
IMAGES = ROOT / "assets" / "resource" / "image"
INTERFACE = ROOT / "assets" / "interface.json"


def iter_inline_recognitions(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_inline_recognitions(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_inline_recognitions(child)


def check_rect(errors: list[str], owner: str, field: str, rect) -> None:
    # MaaFramework also accepts a named sub-recognition result as a dynamic ROI.
    if field == "roi" and isinstance(rect, str):
        return
    # For Click actions, MaaFramework accepts true to click the recognition result.
    if field == "target" and rect is True:
        return
    if not isinstance(rect, list) or len(rect) != 4:
        errors.append(f"{owner}.{field}: expected [x, y, width, height], got {rect!r}")
        return
    x, y, width, height = rect
    if not all(isinstance(item, int) for item in rect):
        errors.append(f"{owner}.{field}: coordinates must be integers")
    elif x < 0 or y < 0 or width < 1 or height < 1 or x + width > 720 or y + height > 1280:
        errors.append(f"{owner}.{field}: outside 720x1280: {rect}")


def main() -> None:
    errors: list[str] = []
    documents: list[tuple[Path, dict]] = []
    nodes: dict[str, tuple[Path, dict]] = {}

    for path in sorted(PIPELINES.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path.name}: invalid JSON: {exc}")
            continue
        documents.append((path, document))
        for name, node in document.items():
            if name == "$schema":
                continue
            if name in nodes:
                errors.append(f"duplicate node {name}: {nodes[name][0].name}, {path.name}")
            elif not isinstance(node, dict):
                errors.append(f"{path.name}:{name}: node must be an object")
            else:
                nodes[name] = (path, node)

    for name, (path, node) in nodes.items():
        owner = f"{path.name}:{name}"
        for field in ("next", "on_error"):
            refs = node.get(field, [])
            if isinstance(refs, str):
                refs = [refs]
            if not isinstance(refs, list):
                errors.append(f"{owner}.{field}: must be a list")
                continue
            for ref in refs:
                if not isinstance(ref, str) or ref not in nodes:
                    errors.append(f"{owner}.{field}: missing node {ref!r}")

        for recognition in iter_inline_recognitions(node):
            for field in ("all_of", "any_of"):
                for ref in recognition.get(field, []):
                    if isinstance(ref, str) and ref not in nodes:
                        errors.append(f"{owner}.{field}: missing node {ref!r}")
            templates = recognition.get("template", [])
            if isinstance(templates, str):
                templates = [templates]
            for template in templates:
                if not (IMAGES / template).is_file():
                    errors.append(f"{owner}.template: missing image/{template}")
            if "roi" in recognition:
                check_rect(errors, owner, "roi", recognition["roi"])

        for field in ("target", "begin", "end"):
            if field in node:
                check_rect(errors, owner, field, node[field])

        if name.endswith("StopSafely") and node.get("action") != "StopTask":
            errors.append(f"{owner}: recovery node must StopTask")

    interface = json.loads(INTERFACE.read_text(encoding="utf-8"))
    public_entries = {task["entry"] for task in interface.get("task", [])}
    for entry in sorted(public_entries):
        if entry not in nodes:
            errors.append(f"interface.json: public entry is missing: {entry}")
            continue
        node = nodes[entry][1]
        if node.get("action") != "DoNothing" or not node.get("next"):
            errors.append(f"{entry}: public entry must dispatch to recognized states")
        if not node.get("timeout") or not node.get("on_error"):
            errors.append(f"{entry}: public entry needs timeout and safe error exit")

    if errors:
        raise SystemExit("PIPELINE AUDIT FAILED:\n- " + "\n- ".join(errors))
    print(
        f"PIPELINE AUDIT PASSED: {len(documents)} files, {len(nodes)} nodes, "
        f"{len(public_entries)} public entries, all templates/links/coordinates valid"
    )


if __name__ == "__main__":
    main()
