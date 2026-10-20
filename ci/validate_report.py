"""
Validate traceability integrity before report generation.

Rules enforced:
  1. Every active scenario maps to a known requirement.
  2. Every normalized result maps to a real scenario and requirement.
  3. Every traceability row references a real requirement (many-to-one: a
     requirement may legitimately own several rows, one per scenario).
  4. Missing data is marked "data_unavailable" — never fabricated.
  5. Comparison results cite their data sources.
  7. Every row scenario_id (other than "n/a") exists in scenarios.json.
  8. No requirement has both an "n/a" row and real scenario rows; the
     `requirements` roll-up list agrees with the rows.

Set REPORT_DEBUG=true for verbose derivation output.

Writes: reports/validation_report.json
Exits 0 (warnings only) or prints errors; does not hard-fail the pipeline.
"""
import json
import os
from pathlib import Path

DEBUG = os.environ.get("REPORT_DEBUG", "false").lower() == "true"


def _debug(msg):
    if DEBUG:
        print(f"[REPORT_DEBUG] {msg}")


def _load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def validate():
    errors = []
    warnings = []

    requirements = _load_json("requirements/analyzed_requirements.json", [])
    scenarios = _load_json("scenarios/scenarios.json", [])
    normalized_raw = _load_json("reports/normalized_results.json", {})
    normalized = normalized_raw.get("results", [])
    traceability = _load_json("reports/traceability_matrix.json", {})

    req_ids = {r["id"] for r in requirements}
    all_sc_ids = {s["id"] for s in scenarios}
    active_sc_ids = {s["id"] for s in scenarios if s.get("status") != "deprecated"}

    _debug(f"Requirements: {sorted(req_ids)}")
    _debug(f"Active scenarios: {sorted(active_sc_ids)}")
    _debug(f"Normalized results: {len(normalized)}")
    _debug(f"Traceability rows: {len(traceability.get('rows', []))}")

    # Rule 1: every active scenario maps to a known requirement
    for sc in scenarios:
        if sc.get("status") == "deprecated":
            continue
        req_id = sc.get("requirement_id", "")
        if req_id not in req_ids:
            errors.append(
                f"Scenario {sc['id']} references requirement {req_id!r} "
                f"which does not exist in analyzed_requirements.json"
            )
        _debug(f"Scenario {sc['id']} → {req_id} ({'OK' if req_id in req_ids else 'MISSING'})")

    # Rule 2: every normalized result maps to real scenario and requirement
    for r in normalized:
        sc_id = r.get("scenario_id", "")
        req_id = r.get("requirement_id", "")
        browser = r.get("browser", "?")
        if sc_id not in all_sc_ids:
            errors.append(
                f"Normalized result has scenario_id={sc_id!r} not found in scenarios.json"
            )
        if req_id not in req_ids:
            errors.append(
                f"Normalized result for {sc_id}/{browser} references requirement "
                f"{req_id!r} which does not exist in analyzed_requirements.json"
            )
        _debug(f"Result {sc_id}/{browser} → req={req_id}, status={r.get('status')}")

    # Rule 3: traceability rows reference real requirements.
    # Many-to-one: multiple rows per requirement are expected (one per scenario).
    trace_rows = traceability.get("rows", [])
    rows_by_req: dict = {}
    for row in trace_rows:
        req_id = row.get("requirement_id", "")
        rows_by_req.setdefault(req_id, []).append(row)
        if req_id and req_id not in req_ids:
            errors.append(
                f"Traceability row {row.get('scenario_id', '?')} references requirement "
                f"{req_id!r} not found in analyzed_requirements.json"
            )
    _debug("Rows per requirement: " + ", ".join(f"{k}={len(v)}" for k, v in rows_by_req.items()))

    # Rule 7: every real row scenario_id exists in scenarios.json
    for row in trace_rows:
        sc_id = row.get("scenario_id", "")
        if sc_id and sc_id != "n/a" and sc_id not in all_sc_ids:
            errors.append(
                f"Traceability row for {row.get('requirement_id')} references scenario "
                f"{sc_id!r} not found in scenarios.json"
            )

    # Rule 8: a requirement is either uncovered (one "n/a" row) or covered
    # (real scenario rows) — never both; and the roll-up list must agree.
    for req_id, req_rows in rows_by_req.items():
        na_rows = [r for r in req_rows if r.get("scenario_id") == "n/a"]
        real_rows = [r for r in req_rows if r.get("scenario_id") not in ("", "n/a", None)]
        if na_rows and real_rows:
            errors.append(
                f"Requirement {req_id} has an 'n/a' row alongside scenario rows "
                f"{[r.get('scenario_id') for r in real_rows]}"
            )
        if len(na_rows) > 1:
            errors.append(f"Requirement {req_id} has {len(na_rows)} 'n/a' rows (expected at most one)")
    rollup = traceability.get("requirements", []) or []
    for q in rollup:
        if not isinstance(q, dict):
            continue
        rid = q.get("requirement_id", "")
        if rid not in req_ids:
            errors.append(f"Roll-up entry references requirement {rid!r} not found in analyzed_requirements.json")
        active_in_rows = {
            r.get("scenario_id") for r in rows_by_req.get(rid, [])
            if r.get("scenario_id") not in ("", "n/a", None) and r.get("overall") != "deprecated"
        }
        if set(q.get("scenario_ids", [])) != active_in_rows:
            errors.append(
                f"Roll-up for {rid} lists scenarios {sorted(q.get('scenario_ids', []))} "
                f"but rows contain {sorted(active_in_rows)}"
            )

    # Rule 4: data_unavailable entries are flagged as warnings (correct behaviour, not an error)
    # An incremental run carries unchanged scenarios' results forward (see
    # build_traceability); their placeholders here are expected, not gaps.
    carried = {r.get("scenario_id") for r in traceability.get("rows", []) if r.get("playwright_carried_from")}
    unavailable = [r for r in normalized
                   if r.get("status") == "data_unavailable" and r.get("scenario_id") not in carried]
    for r in unavailable:
        warnings.append(
            f"{r['scenario_id']}/{r.get('browser', '?')}: "
            f"no execution data — status correctly set to data_unavailable"
        )
        _debug(f"WARNING data_unavailable: {r['scenario_id']}/{r.get('browser')}")

    # Rule 5: traceability rows with a verdict should not have fabricated session links
    for row in traceability.get("rows", []):
        status = row.get("playwright_status", "")
        if status in ("passed", "failed") and not row.get("session_link"):
            warnings.append(
                f"{row.get('requirement_id')}: playwright_status={status} "
                f"but no session link — source is junit only (acceptable)"
            )

    # Rule 6: comparison results must cite normalized_results.json
    comparison = _load_json("reports/comparison_results.json", {})
    if comparison:
        sources = str(comparison.get("data_sources", ""))
        if "normalized_results" not in sources:
            warnings.append(
                "comparison_results.json does not cite normalized_results.json as a data source"
            )

    valid = len(errors) == 0

    if DEBUG:
        print(f"\n[REPORT_DEBUG] Validation complete:")
        print(f"  Valid: {valid}")
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    ERROR: {e}")
        print(f"  Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    WARNING: {w}")

    report = {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "requirements_count": len(requirements),
        "active_scenarios_count": len(active_sc_ids),
        "traceability_rows_count": len(trace_rows),
        "traceability_requirements_count": len(rollup),
        "results_count": len(normalized),
        "data_unavailable_count": len(unavailable),
    }

    Path("reports").mkdir(parents=True, exist_ok=True)
    Path("reports/validation_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    label = "VALID" if valid else "INVALID"
    print(f"[validate] {label} — {len(errors)} error(s), {len(warnings)} warning(s)")

    if not valid:
        for e in errors:
            print(f"[validate] ERROR: {e}")

    return report


if __name__ == "__main__":
    validate()
