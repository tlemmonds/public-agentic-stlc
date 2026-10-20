import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result
from collect_kane_exports import is_native, native_dir_rel

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default="scenarios/scenarios.json")
    parser.add_argument("--manifest", default="reports/test_execution_manifest.json")
    parser.add_argument("--selection", default="reports/pytest_selection.txt")
    return parser.parse_args()


def function_name_for(scenario):
    return scenario.get("function_name") or f"test_{scenario['id'].lower().replace('-', '_')}"


def selection_line_for(scenario):
    """testmu export → its staged directory (ci/run_selected.py cds into it);
    vanilla → the pytest node id in test_regression.py, exactly as before."""
    if is_native(scenario):
        return native_dir_rel(scenario["id"])
    return f"tests/playwright/test_regression.py::{function_name_for(scenario)}"


def _load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] Failed to read {path}: {e}")
        return default


def main():
    args = parse_args()
    print_stage_header("4", "SELECT_TESTS", "Build test manifest and pytest selection file")
    Path("reports").mkdir(exist_ok=True)
    scenarios = _load_json(args.scenarios, [])
    if not scenarios:
        print(f"[ERROR] No scenarios found in {args.scenarios}", file=sys.stderr)
        sys.exit(1)
    full_run = os.environ.get("FULL_RUN", "false").lower() == "true"

    selected = []
    excluded = []
    reasons = {}
    for scenario in scenarios:
        status = scenario.get("status", "active")
        if status == "deprecated":
            excluded.append(scenario["id"])
            reasons[scenario["id"]] = "deprecated"
            continue
        if full_run or status in {"new", "updated"}:
            selected.append(scenario)
        elif status == "active":
            excluded.append(scenario["id"])
            reasons[scenario["id"]] = "not part of incremental run"

    run_type = "full" if full_run else "incremental"
    manifest = {
        "run_type": run_type,
        "selected_scenarios": [scenario["id"] for scenario in selected],
        "selected_test_ids": [scenario["test_case_id"] for scenario in selected],
        "selected_native": [scenario["id"] for scenario in selected if is_native(scenario)],
        "excluded_scenarios": excluded,
        "exclusion_reason": reasons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    selection_lines = [selection_line_for(scenario) for scenario in selected]
    native_count = len(manifest["selected_native"])
    selection_path = Path(args.selection)
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_text("\n".join(selection_lines) + ("\n" if selection_lines else ""), encoding="utf-8")

    print_stage_result("4", "SELECT_TESTS", {
        "Run type":  run_type,
        "Selected":  f"{len(selected)} scenarios ({len(selected) - native_count} vanilla, {native_count} native)",
        "Excluded":  f"{len(excluded)} (deprecated/inactive)",
        "Output":    f"{args.selection}, {args.manifest}",
    })


if __name__ == "__main__":
    main()
