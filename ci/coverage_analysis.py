"""
Enterprise Requirement Coverage Analysis Engine.

Reads real pipeline artifacts to compute:
  - Per-requirement coverage status: FULL / PARTIAL / NONE
  - Coverage categories: happy_path, negative, edge_case, he_executed, regression
  - Flakiness: retries > 0 or mixed status across browsers
  - Missing scenario types vs. a feature-level risk model
  - Risk level per requirement: HIGH / MEDIUM / LOW
  - Feature-level rollup and coverage heatmap

Sources:
  - requirements/analyzed_requirements.json  (Kane AI results)
  - scenarios/scenarios.json                 (scenario catalog)
  - reports/normalized_results.json          (execution results per scenario/browser)

Produces:
  - reports/coverage_report.json
  - reports/missing_scenarios.json
  - reports/flaky_requirements.json
  - reports/coverage_report.md
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result
from project_config import (
    classify_feature,
    deprecated_by_requirement,
    feature_taxonomy,
    group_scenarios_by_requirement,
)

# ── Feature taxonomy ─────────────────────────────────────────────────────────
# Single source: agentic-stlc.config.yaml `features:` (built-in fallback when
# absent) via project_config.feature_taxonomy(). The module-level names are
# kept as aliases so existing importers keep working.
#   FEATURE_CRITICALITY  feature → HIGH | MEDIUM | LOW
#   FEATURE_KEYWORDS     feature → keywords detected in requirement text
#   EXPECTED_SCENARIOS   feature → expected scenario types (static risk model)
FEATURE_CRITICALITY, FEATURE_KEYWORDS, EXPECTED_SCENARIOS = feature_taxonomy()

# ── Coverage category detection keywords ─────────────────────────────────────
_NEGATIVE_KEYWORDS = frozenset([
    "invalid", "error", "fail", "reject", "empty", "negative", "incorrect",
    "wrong", "missing", "cannot", "unable", "remove", "delete", "out of stock",
    "unauthorized", "no results", "not found", "forbidden", "limit",
])
_EDGE_CASE_KEYWORDS = frozenset([
    "empty cart", "zero quantity", "minimum", "maximum", "boundary",
    "already in cart", "duplicate", "special character", "persistence",
    "session expired",
])
_SECURITY_KEYWORDS = frozenset([
    "xss", "sql inject", "brute force", "unauthorized access", "csrf",
    "privilege escalation", "bypass", "tamper",
])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _classify_feature(text: str, explicit: str | None = None) -> str:
    """Feature for a requirement: the explicit scenario feature wins; otherwise
    keyword classification of the requirement text (project_config)."""
    return classify_feature(text, explicit=explicit)


def _compute_risk_level(
    feature: str,
    coverage_status: str,
    has_failures: bool,
    has_flaky: bool,
    has_negative: bool,
) -> str:
    criticality = FEATURE_CRITICALITY.get(feature, "MEDIUM")
    if has_failures and criticality == "HIGH":
        return "HIGH"
    if coverage_status == "NONE" and criticality != "LOW":
        return "HIGH"
    if coverage_status == "NONE":
        return "MEDIUM"
    if has_failures:
        return "HIGH" if criticality == "HIGH" else "MEDIUM"
    if criticality == "HIGH" and not has_negative:
        return "MEDIUM"
    if has_flaky:
        return "MEDIUM"
    if coverage_status == "PARTIAL":
        return "MEDIUM"
    return "LOW"


def _is_flaky(results: list[dict]) -> tuple[bool, int]:
    """Flaky if retries > 0 OR mixed statuses across browsers for same scenario."""
    total_retries = sum(r.get("retries", 0) for r in results)
    live = [r.get("status") for r in results
            if r.get("status") not in ("data_unavailable", None)]
    mixed = len(set(live)) > 1 if len(live) >= 2 else False
    return (total_retries > 0 or mixed), total_retries


def _missing_scenarios(
    feature: str,
    covered_descriptions: list[str],
    has_negative: bool,
    has_edge_case: bool,
    ac_description: str = "",
) -> list[dict]:
    """Compare feature's expected scenario model against what's actually covered.

    Cross-AC noise suppression: EXPECTED_SCENARIOS is a feature-wide list, but
    this function is called per-AC. Without filtering, every AC in a feature
    gets flagged for happy paths that legitimately belong to sibling ACs (e.g.
    AC-001 "create a task" gets falsely flagged for AC-003's "Mark complete",
    AC-005's "Delete", etc., because all live under the TASK_CRUD feature
    pool). We only flag a happy path when its keywords overlap with THIS AC's
    own description — i.e. when this AC is the natural home for that scenario.
    Negative and edge-case gaps remain per-AC since they're feature-level by
    design (one feature can have a single empty-state edge case that applies
    across all its happy paths).
    """
    expected = EXPECTED_SCENARIOS.get(feature, [])
    missing = []
    covered_text = " ".join(d.lower() for d in covered_descriptions)
    ac_keywords = {w for w in ac_description.lower().split() if len(w) > 4}
    for exp in expected:
        stype = exp["type"]
        if stype == "negative" and not has_negative:
            missing.append(exp)
        elif stype == "edge_case" and not has_edge_case:
            missing.append(exp)
        elif stype == "happy_path":
            words = [w for w in exp["description"].lower().split() if len(w) > 4]
            if not words or any(w in covered_text for w in words):
                continue
            if ac_keywords and not (set(words) & ac_keywords):
                continue
            missing.append(exp)
    return missing


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze(
    requirements_path: str = "requirements/analyzed_requirements.json",
    scenarios_path:    str = "scenarios/scenarios.json",
    normalized_path:   str = "reports/normalized_results.json",
) -> dict:
    print_stage_header("7b", "COVERAGE_ANALYSIS",
                        "Enterprise requirement coverage and gap analysis")
    Path("reports").mkdir(exist_ok=True)

    requirements     = _load_json(requirements_path, [])
    scenarios        = _load_json(scenarios_path, [])
    normalized_raw   = _load_json(normalized_path, {})
    normalized       = normalized_raw.get("results", [])

    # Build lookup tables — requirement_id → [scenario, ...] (many-to-one)
    scenarios_by_req  = group_scenarios_by_requirement(scenarios)
    deprecated_by_req = deprecated_by_requirement(scenarios)

    results_by_sc: dict[str, list[dict]] = {}
    for r in normalized:
        results_by_sc.setdefault(r.get("scenario_id", ""), []).append(r)

    # Incremental run: use the results build_traceability carried forward for
    # unchanged scenarios (same archive lookup), instead of their placeholders.
    trace_rows = _load_json("reports/traceability_matrix.json", {}).get("rows", [])
    carried_ids = {r["scenario_id"] for r in trace_rows if r.get("playwright_carried_from")}
    if carried_ids:
        from build_traceability import _archived_results, _carried_forward
        archives = _archived_results()
        for sc_id in carried_ids:
            _, records = _carried_forward(sc_id, archives)
            if records:
                results_by_sc[sc_id] = records

    coverage_records   = []
    missing_all        = []
    flaky_requirements = []

    # Aggregate counters
    cnt_full = cnt_partial = cnt_none = 0
    cnt_negative = cnt_he = 0

    for req in requirements:
        req_id      = req.get("id", "")
        description = req.get("description", "")
        kane_status = req.get("kane_status", "unknown")

        mapped_scenarios = scenarios_by_req.get(req_id, [])
        sc_ids = [s["id"] for s in mapped_scenarios]

        # Explicit feature from the first mapped scenario (ingested assets
        # carry one); generated 1:1 scenarios don't, so fall back to keyword
        # classification of the requirement text.
        first_sc = mapped_scenarios[0] if mapped_scenarios else (deprecated_by_req.get(req_id) or [{}])[0]
        feature  = _classify_feature(description, explicit=first_sc.get("feature"))

        # Deprecated tombstone: the AC line is still in the requirements file but its
        # only scenario was tombstoned by a release_diff DELETE op (v1.1.0
        # removed "User can delete a task"). Render as DEPRECATED, not NONE
        # — otherwise it shows up as "🔴 HIGH risk / no coverage", which is
        # misleading: the absence is intentional, not a gap to fix.
        if not mapped_scenarios and deprecated_by_req.get(req_id):
            dep = deprecated_by_req[req_id][0]
            coverage_records.append({
                "requirement_id":       req_id,
                "description":          description,
                "feature":              feature,
                "criticality":          "DEPRECATED",
                "kane_status":          kane_status,
                "coverage_status":      "DEPRECATED",
                "covered_scenarios":    [dep["id"]],
                "missing_scenarios":    [],
                "execution_status": {
                    "total": 0, "passed": 0, "failed": 0, "skipped": 0,
                    "data_unavailable": 0, "flaky": 0,
                },
                "coverage_categories":  {},
                "browsers_tested":      [],
                "functional_coverage_pct": 0.0,
                "negative_coverage_pct":   0.0,
                "risk_level":           "DEPRECATED",
                "flaky":                False,
                "retry_count":          0,
                "deprecated_in_release": dep.get("deprecated_in_release", "prior release"),
            })
            continue

        # All execution results for this requirement's scenarios
        all_results: list[dict] = []
        for sc_id in sc_ids:
            all_results.extend(results_by_sc.get(sc_id, []))

        # Execution stats (exclude data_unavailable from counts)
        live_results  = [r for r in all_results if r.get("status") != "data_unavailable"]
        exec_passed   = sum(1 for r in live_results if r.get("status") == "passed")
        exec_failed   = sum(1 for r in live_results if r.get("status") == "failed")
        exec_skipped  = sum(1 for r in live_results if r.get("status") == "skipped")
        exec_unavail  = sum(1 for r in all_results  if r.get("status") == "data_unavailable")
        exec_total    = len(live_results)

        # Browser coverage
        browsers_run = {r.get("browser", "") for r in live_results}
        he_executed  = any(r.get("session_link") for r in all_results)

        # Flakiness
        is_flaky, retry_count = _is_flaky(all_results)

        # Coverage category detection from all text
        all_texts = [description] + [s.get("source_description", "") for s in mapped_scenarios]
        combined_lower = " ".join(all_texts).lower()
        has_negative = any(kw in combined_lower for kw in _NEGATIVE_KEYWORDS)
        has_edge     = any(kw in combined_lower for kw in _EDGE_CASE_KEYWORDS)
        has_security = any(kw in combined_lower for kw in _SECURITY_KEYWORDS)

        coverage_categories = {
            "happy_path":      bool(mapped_scenarios),
            "negative":        has_negative,
            "edge_case":       has_edge,
            "security":        has_security,
            "he_executed":     he_executed,
            "regression":      exec_total > 0,
            "api_integration": False,
            "accessibility":   False,
        }

        # Coverage status
        if not mapped_scenarios:
            coverage_status = "NONE"
            cnt_none += 1
        elif exec_total == 0:
            coverage_status = "PARTIAL"   # scenario exists but did not run
            cnt_partial += 1
        elif exec_failed > 0:
            coverage_status = "PARTIAL"   # ran but not all passed
            cnt_partial += 1
        else:
            coverage_status = "FULL"
            cnt_full += 1

        if has_negative:  cnt_negative += 1
        if he_executed:   cnt_he       += 1

        # Risk level
        has_failures = exec_failed > 0 or kane_status == "failed"
        risk_level = _compute_risk_level(
            feature=feature,
            coverage_status=coverage_status,
            has_failures=has_failures,
            has_flaky=is_flaky,
            has_negative=has_negative,
        )

        # Expected scenario types vs actual
        missing = _missing_scenarios(
            feature=feature,
            covered_descriptions=all_texts,
            has_negative=has_negative,
            has_edge_case=has_edge,
            ac_description=description,
        )
        if missing:
            missing_all.append({
                "requirement_id": req_id,
                "feature":        feature,
                "criticality":    FEATURE_CRITICALITY.get(feature, "MEDIUM"),
                "missing":        missing,
            })

        # Functional coverage %: active scenarios vs expected happy-path count
        n_happy_exp = max(1, len([e for e in EXPECTED_SCENARIOS.get(feature, [])
                                  if e["type"] == "happy_path"]))
        n_happy_cov = len(sc_ids)
        functional_pct = min(100.0, round(n_happy_cov / n_happy_exp * 100, 1))

        # Negative coverage %
        n_neg_exp  = len([e for e in EXPECTED_SCENARIOS.get(feature, []) if e["type"] == "negative"])
        negative_pct = 100.0 if (n_neg_exp == 0) else (100.0 if has_negative else 0.0)

        record = {
            "requirement_id":       req_id,
            "description":          description,
            "feature":              feature,
            "criticality":          FEATURE_CRITICALITY.get(feature, "MEDIUM"),
            "kane_status":          kane_status,
            "coverage_status":      coverage_status,
            "covered_scenarios":    sc_ids,
            "missing_scenarios":    missing,
            "execution_status": {
                "total":            exec_total,
                "passed":           exec_passed,
                "failed":           exec_failed,
                "skipped":          exec_skipped,
                "data_unavailable": exec_unavail,
                "flaky":            1 if is_flaky else 0,
            },
            "coverage_categories":  coverage_categories,
            "browsers_tested":      sorted(browsers_run),
            "functional_coverage_pct": functional_pct,
            "negative_coverage_pct":   negative_pct,
            "risk_level":           risk_level,
            "flaky":                is_flaky,
            "retry_count":          retry_count,
        }
        coverage_records.append(record)

        if is_flaky:
            flaky_requirements.append({
                "requirement_id": req_id,
                "feature":        feature,
                "retry_count":    retry_count,
                "scenarios":      sc_ids,
                "browsers_with_retries": sorted({
                    r.get("browser", "") for r in all_results if r.get("retries", 0) > 0
                }),
            })

    total = len(requirements)
    deprecated_count = sum(1 for r in coverage_records if r["coverage_status"] == "DEPRECATED")
    live_total = total - deprecated_count

    # Percentages are computed against the LIVE requirement population
    # (deprecated tombstones are intentional, not coverage gaps — including
    # them in the denominator makes a clean release look incomplete).
    def _pct(n: int) -> float:
        return round(n / live_total * 100, 1) if live_total else 0.0
    summary = {
        "total_requirements":   total,
        "deprecated":           deprecated_count,
        "covered_full":         cnt_full,
        "covered_partial":      cnt_partial,
        "uncovered":            cnt_none,
        "coverage_pct":         _pct(cnt_full),
        "any_coverage_pct":     _pct(cnt_full + cnt_partial),
        "negative_coverage_pct": _pct(cnt_negative),
        "he_coverage_pct":      _pct(cnt_he),
        "flaky_count":          len(flaky_requirements),
        "high_risk_count":      sum(1 for r in coverage_records if r["risk_level"] == "HIGH"),
        "medium_risk_count":    sum(1 for r in coverage_records if r["risk_level"] == "MEDIUM"),
        "missing_scenario_types": sum(len(m["missing"]) for m in missing_all),
    }

    # Feature rollup
    feature_rollup: dict[str, dict] = {}
    for r in coverage_records:
        # Deprecated tombstones are not a feature gap — exclude from rollup
        # so the "Uncovered" column doesn't inflate from intentional removals.
        if r["coverage_status"] == "DEPRECATED":
            continue
        feat = r["feature"]
        if feat not in feature_rollup:
            feature_rollup[feat] = {
                "feature":     feat,
                "criticality": FEATURE_CRITICALITY.get(feat, "MEDIUM"),
                "total":       0, "covered": 0, "partial": 0, "none": 0,
                "failed": 0, "flaky": 0,
            }
        fr = feature_rollup[feat]
        fr["total"] += 1
        if r["coverage_status"] == "FULL":    fr["covered"] += 1
        elif r["coverage_status"] == "PARTIAL": fr["partial"] += 1
        else:                                   fr["none"]    += 1
        if r["execution_status"]["failed"] > 0: fr["failed"]  += 1
        if r["flaky"]:                          fr["flaky"]   += 1

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary":       summary,
        "requirements":  coverage_records,
        "feature_rollup": sorted(feature_rollup.values(), key=lambda x: x["total"], reverse=True),
    }

    Path("reports/coverage_report.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )
    Path("reports/missing_scenarios.json").write_text(
        json.dumps({"generated_at": output["generated_at"], "missing": missing_all}, indent=2) + "\n",
        encoding="utf-8",
    )
    Path("reports/flaky_requirements.json").write_text(
        json.dumps({"generated_at": output["generated_at"], "flaky": flaky_requirements}, indent=2) + "\n",
        encoding="utf-8",
    )

    _write_markdown(coverage_records, summary, feature_rollup)

    print_stage_result("7b", "COVERAGE_ANALYSIS", {
        "Total requirements":     total,
        "Fully covered":          f"{cnt_full} ({summary['coverage_pct']}%)",
        "Partially covered":      cnt_partial,
        "Uncovered":              cnt_none,
        "Negative coverage":      f"{summary['negative_coverage_pct']}%",
        "HyperExecute coverage":  f"{summary['he_coverage_pct']}%",
        "Flaky requirements":     len(flaky_requirements),
        "High-risk requirements": summary["high_risk_count"],
        "Missing scenario types": summary["missing_scenario_types"],
        "Output":                 "reports/coverage_report.json",
    })
    return output


def _write_markdown(records: list, summary: dict, feature_rollup: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Requirement Coverage Analysis Report",
        "",
        f"_Generated: {ts}_",
        "",
        "## Coverage Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total Requirements | {summary['total_requirements']} |",
        f"| Deprecated (excluded from %) | {summary['deprecated']} |",
        f"| Fully Covered | {summary['covered_full']} ({summary['coverage_pct']}%) |",
        f"| Partially Covered | {summary['covered_partial']} |",
        f"| Uncovered | {summary['uncovered']} |",
        f"| Negative Test Coverage | {summary['negative_coverage_pct']}% |",
        f"| HyperExecute Coverage | {summary['he_coverage_pct']}% |",
        f"| Flaky Requirements | {summary['flaky_count']} |",
        f"| High-Risk Requirements | {summary['high_risk_count']} |",
        f"| Missing Scenario Types | {summary['missing_scenario_types']} |",
        "",
        "## Feature Coverage Heatmap",
        "",
        "| Feature | Criticality | Total | Covered | Partial | Uncovered | Failed | Flaky |",
        "|---------|-------------|-------|---------|---------|-----------|--------|-------|",
    ]
    for f in feature_rollup.values():
        lines.append(
            f"| {f['feature']} | {f['criticality']} | {f['total']} | "
            f"{f['covered']} | {f['partial']} | {f['none']} | {f['failed']} | {f['flaky']} |"
        )

    lines += [
        "",
        "## Requirement Coverage Detail",
        "",
        "| Requirement | Coverage | Tests | Pass | Fail | Missing | Risk |",
        "|-------------|----------|-------|------|------|---------|------|",
    ]
    for r in records:
        es = r["execution_status"]
        lines.append(
            f"| `{r['requirement_id']}` | {r['coverage_status']} | "
            f"{es['total']} | {es['passed']} | {es['failed']} | "
            f"{len(r['missing_scenarios'])} | {r['risk_level']} |"
        )

    lines += ["", "## Per-Requirement Detail", ""]
    for r in records:
        # Deprecated tombstone: render a short notice instead of the full
        # category/execution tables (which would all be N/A and confusing).
        if r["coverage_status"] == "DEPRECATED":
            dep_in = r.get("deprecated_in_release", "prior release")
            lines += [
                f"### {r['requirement_id']} — DEPRECATED",
                "",
                f"> {r['description']}",
                "",
                f"_Tombstoned in **{dep_in}**. Scenario kept in catalog for history; intentionally not executed._",
                "",
            ]
            continue
        cats = r["coverage_categories"]
        es   = r["execution_status"]
        yes  = lambda v: "✅" if v else "❌"
        lines += [
            f"### {r['requirement_id']} — {r['feature']}",
            "",
            f"> {r['description']}",
            "",
            f"- **Coverage Status:** {r['coverage_status']}  |  **Risk:** {r['risk_level']}  "
            f"|  **Criticality:** {r['criticality']}  |  **Kane:** {r['kane_status']}",
            f"- **Functional Coverage:** {r['functional_coverage_pct']}%  "
            f"|  **Negative Coverage:** {r['negative_coverage_pct']}%",
            f"- **Browsers Tested:** {', '.join(r['browsers_tested']) or 'none'}",
            f"- **Flaky:** {'YES — ' + str(r['retry_count']) + ' retries' if r['flaky'] else 'no'}",
            "",
            "**Coverage Categories:**",
            f"| Happy Path | Negative | Edge Case | HyperExecute | Regression |",
            f"|------------|----------|-----------|--------------|------------|",
            f"| {yes(cats['happy_path'])} | {yes(cats['negative'])} | {yes(cats['edge_case'])} "
            f"| {yes(cats['he_executed'])} "
            f"| {yes(cats['regression'])} |",
            "",
            f"**Execution:** {es['total']} total | {es['passed']} passed | "
            f"{es['failed']} failed | {es['flaky']} flaky",
            "",
        ]
        if r["missing_scenarios"]:
            lines.append("**Missing Scenario Types:**")
            for ms in r["missing_scenarios"]:
                lines.append(f"- `[{ms['type'].upper()}]` {ms['description']}")
            lines.append("")
        if r["covered_scenarios"]:
            lines.append(f"**Covered by:** {', '.join(r['covered_scenarios'])}")
            lines.append("")

    lines += ["---", "_Coverage analysis generated by Agentic STLC pipeline_", ""]
    Path("reports/coverage_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    analyze()
