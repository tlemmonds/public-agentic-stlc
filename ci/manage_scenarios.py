import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result
from project_config import (
    auto_create_scenarios,
    group_scenarios_by_requirement,
    is_ingested,
    scenario_asset_path,
)

# Optional per-AC objective overrides. Empty by default — every AC
# falls through to its description verbatim. Kept in sync with
# _KANE_TASK_OVERRIDES in analyze_requirements.py.
_OBJECTIVE_OVERRIDES: dict[str, str] = {}


def _get_kane_objective(description: str) -> str:
    dl = description.lower()
    for keyword, objective in _OBJECTIVE_OVERRIDES.items():
        if keyword in dl:
            return objective
    return description


# ── Ingested-asset drift (many-to-one) ───────────────────────────────────────
# Same rule in agent.py::sync_scenarios and skills/scenario_generation.py:
# an ingested scenario is "updated" when the asset file's body hash differs
# from the sidecar `<asset>.meta.json` `description_hash`. Prefer
# replay_policy's helpers; fall back to a local equivalent.

def _hash_asset_body(text: str) -> str:
    try:
        from replay_policy import hash_asset_body  # type: ignore
        return hash_asset_body(text)
    except (ImportError, AttributeError):
        lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        normalized = "\n".join(lines).strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _sidecar_hash(asset_path: Path) -> str | None:
    try:
        from replay_policy import read_asset_hash  # type: ignore
        return read_asset_hash(asset_path)
    except (ImportError, AttributeError):
        pass
    sidecar = asset_path.with_suffix(".meta.json")
    if not sidecar.exists():
        return None
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8")).get("description_hash")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def asset_drifted(scenario: dict) -> bool:
    """True when the ingested asset body no longer matches its sidecar hash.
    Missing asset / sidecar → not drifted (Stage 1 reports asset_missing)."""
    path = scenario_asset_path(scenario)
    if path is None or not path.exists():
        return False
    recorded = _sidecar_hash(path)
    if not recorded:
        return False
    try:
        return _hash_asset_body(path.read_text(encoding="utf-8")) != recorded
    except OSError:
        return False


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", default="requirements/analyzed_requirements.json")
    parser.add_argument("--scenarios", default="scenarios/scenarios.json")
    return parser.parse_args()


def load_json(path, default):
    file_path = Path(path)
    if not file_path.exists():
        return default
    content = file_path.read_text(encoding="utf-8").strip()
    if not content:
        return default
    return json.loads(content)


def title_and_steps(requirement):
    """
    Build scenario title, steps, and expected result.

    Primary source: Kane AI's NDJSON run_end / step_end output stored on the
    analyzed requirement (kane_one_liner, kane_steps, kane_summary).
    Falls back to keyword-based defaults when Kane run was skipped or failed.
    """
    one_liner = requirement.get("kane_one_liner", "").strip()
    kane_steps = [s for s in requirement.get("kane_steps", []) if s.strip()]
    summary = requirement.get("kane_summary", "").strip()
    description = requirement["description"]

    # Title is a stable name from the criterion, never Kane's per-run one-liner
    # (it changes every run and drives the asset filename).
    title = _fallback_title(description)
    steps = kane_steps if kane_steps else _fallback_steps(description)
    expected = summary if summary else _fallback_expected(description)

    return title, steps, expected


def _fallback_title(description):
    from project_config import scenario_title
    return scenario_title(description)


def _fallback_steps(description):
    """Generic 3-step fallback when Kane didn't emit step summaries.
    Used by ChatReporter and traceability matrix renderers — the actual
    test execution comes from Kane's _test.md asset, not from this list."""
    target = os.environ.get("TARGET_URL") or __import__("project_config").cfg("target_url", "")
    return [
        f"Navigate to {target}",
        "Perform the action described in the acceptance criterion",
        "Verify the expected outcome is achieved",
    ]


def _fallback_expected(description):
    return description.capitalize()


def _next_sc_number(scenarios) -> int:
    max_n = 0
    for sc in scenarios:
        m = re.match(r"SC-(\d+)", str(sc.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def main():
    args = parse_args()
    print_stage_header("2", "MANAGE_SCENARIOS", "Sync scenarios.json with analyzed requirements")
    requirements = load_json(args.requirements, [])
    scenarios = load_json(args.scenarios, [])
    # Many-to-one: requirement_id → [scenario, ...] (deprecated included so
    # tombstones survive; they are never dropped and never renumbered).
    grouped = group_scenarios_by_requirement(scenarios, include_deprecated=True)
    today = datetime.now(timezone.utc).date().isoformat()
    next_sc = _next_sc_number(scenarios)
    auto_create = auto_create_scenarios()

    updated = []
    counts = {"active": 0, "updated": 0, "new": 0, "deprecated": 0, "uncovered": 0}
    active_requirement_ids = set()

    for requirement in requirements:
        rid = requirement["id"]
        active_requirement_ids.add(rid)
        brd_ref = requirement.get("brd_ref")
        title, steps, expected = title_and_steps(requirement)
        existing = grouped.get(rid, [])

        if not existing:
            if not auto_create:
                print(f"[manage_scenarios] {rid}: no scenarios and scenarios.auto_create=false — left uncovered")
                counts["uncovered"] += 1
                continue
            existing = [{}]  # one generated scenario, exactly as before

        for scenario in existing:
            if not scenario:
                status = "new"
                sc_id = f"SC-{next_sc:03d}"
                tc_id = f"TC-{next_sc:03d}"
                next_sc += 1
            elif scenario.get("status") == "deprecated":
                status = "deprecated"
                sc_id = scenario["id"]
                tc_id = scenario.get("test_case_id", sc_id.replace("SC-", "TC-"))
            elif is_ingested(scenario):
                status = "updated" if asset_drifted(scenario) else "active"
                sc_id = scenario["id"]
                tc_id = scenario.get("test_case_id", sc_id.replace("SC-", "TC-"))
            elif scenario.get("pending_rerun"):
                # release_diff ADD/EDIT flag — same rule as agent.py::sync_scenarios
                status = scenario.get("status") if scenario.get("status") in ("new", "updated") else "updated"
            else:
                status = "active" if scenario.get("source_description") == requirement["description"] else "updated"
                sc_id = scenario["id"]
                tc_id = scenario.get("test_case_id", sc_id.replace("SC-", "TC-"))

            if scenario and is_ingested(scenario):
                # Ingested: the asset is authoritative — keep the record verbatim.
                record = dict(scenario)
                record["status"] = status
                record["kane_last_status"] = requirement.get("kane_status", record.get("kane_last_status", "pending"))
                record["last_verified"] = today
            else:
                record = {
                    "id": sc_id,
                    "requirement_id": rid,
                    "title": title,
                    "steps": steps,
                    "expected_result": expected,
                    "status": status,
                    "kane_objective": scenario.get("kane_objective") or _get_kane_objective(requirement["description"]),
                    # Preserve the existing kane_url when updating an existing scenario so
                    # scenario-specific starting URLs (e.g. category pages) are not reset
                    # to the homepage on every requirements change.
                    "kane_url": scenario.get("kane_url", requirement.get("url", "")) if scenario else requirement.get("url", ""),
                    "kane_last_status": requirement.get("kane_status", "pending"),
                    "test_case_id": tc_id,
                    "last_verified": today,
                    "source_description": requirement["description"],
                }
                for k in ("feature", "function_name", "deprecated_in_release", "deprecated_at", "deprecated_by",
                          "review_required", "review_reason"):
                    if scenario.get(k) is not None:
                        record[k] = scenario[k]
                if requirement.get("kane_status") == "failed":
                    record["kane_failure_reason"] = requirement.get("kane_summary", "")

            if brd_ref and not record.get("brd_ref"):
                record["brd_ref"] = brd_ref
            elif scenario.get("brd_ref") and "brd_ref" not in record:
                record["brd_ref"] = scenario["brd_ref"]

            updated.append(record)
            counts[status] += 1

    for scenario in scenarios:
        if scenario["requirement_id"] in active_requirement_ids:
            continue
        deprecated = dict(scenario)
        deprecated["status"] = "deprecated"
        updated.append(deprecated)
        counts["deprecated"] += 1

    output = Path(args.scenarios)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")

    print_stage_result("2", "MANAGE_SCENARIOS", {
        "Active":     counts["active"],
        "Updated":    counts["updated"],
        "New":        counts["new"],
        "Deprecated": counts["deprecated"],
        "Uncovered":  counts["uncovered"],
        "Total":      len(updated),
        "Output":     args.scenarios,
    })


if __name__ == "__main__":
    main()
