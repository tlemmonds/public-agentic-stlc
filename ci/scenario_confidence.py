"""
Scenario Confidence Analysis Engine — Stage 2b of the Agentic STLC pipeline.

Scores each scenario's test coverage sufficiency across multiple dimensions:
  - happy_path: Does the scenario cover the primary success flow?
  - negative: Are failure/rejection cases tested?
  - edge_case: Are boundary/unusual inputs covered?

Produces reports consumed by ConfidenceAnalysisSkill, ChatReporter, AND
write_github_summary.py (which renders the Stage 2b table in the GH summary).

Entrypoints:
  - run_confidence_analysis(...) — library function used by skills/astlc
  - python ci/scenario_confidence.py — Stage 2b in the ci/agent.py post-pipeline
    chain; reads requirements/analyzed_requirements.json + scenarios/scenarios.json
    from disk and writes reports/scenario-confidence-report.json (etc).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result
from project_config import (
    criticality_for,
    classify_feature,
    deprecated_by_requirement,
    feature_taxonomy,
    group_scenarios_by_requirement,
)


# Keywords that indicate negative/edge-case coverage in scenario descriptions
_NEGATIVE_KEYWORDS = re.compile(
    r"invalid|error|fail|reject|wrong|bad|empty|null|missing|exceed|over limit|"
    r"negative|edge|boundary|out of stock|no results|unauthorized|forbidden|expire",
    re.IGNORECASE,
)
_EDGE_KEYWORDS = re.compile(
    r"special character|unicode|long string|max|min|limit|boundary|zero|"
    r"empty cart|no item|already exists|duplicate|concurrent",
    re.IGNORECASE,
)

# Scenario tags that satisfy a coverage dimension outright (ingested assets
# carry these; generated scenarios usually don't, so keyword detection on the
# description/title remains the fallback).
_NEGATIVE_TAGS = {"negative", "error"}
_EDGE_TAGS     = {"edge", "boundary"}


def _criticality_sets() -> tuple[set[str], set[str]]:
    """(HIGH features, MEDIUM features) from the shared taxonomy
    (agentic-stlc.config.yaml `features:` or the built-in fallback)."""
    crit, _, _ = feature_taxonomy()
    high   = {f for f, c in crit.items() if c == "HIGH"}
    medium = {f for f, c in crit.items() if c == "MEDIUM"}
    return high, medium


def _tags(scenario: dict) -> set[str]:
    return {str(t).lower() for t in (scenario.get("tags") or [])}


def _scenario_text(scenario: dict) -> str:
    return " ".join(str(scenario.get(k) or "") for k in ("description", "title"))

# Score → confidence-level bucket map. Exported so the GitHub summary can
# render it as a "Confidence Score Ranges" legend table. Order matters: scan
# top-to-bottom and return the first range that contains the score.
CONFIDENCE_SCORE_RANGES: list[dict] = [
    {"level": "VERY_HIGH",    "min": 90, "max": 100, "label": "🟢 VERY_HIGH",    "meaning": "All coverage dimensions satisfied"},
    {"level": "HIGH",         "min": 75, "max": 89,  "label": "🟡 HIGH",         "meaning": "Core flow validated; one minor coverage gap"},
    {"level": "MEDIUM",       "min": 50, "max": 74,  "label": "🟠 MEDIUM",       "meaning": "Happy path present; two important gaps remain"},
    {"level": "LOW",          "min": 1,  "max": 49,  "label": "🔴 LOW",          "meaning": "Coverage gaps with Kane not yet run, OR Kane functional failure"},
    {"level": "CRITICAL_GAP", "min": 0,  "max": 0,   "label": "🚨 CRITICAL_GAP", "meaning": "No scenario mapped — zero automated coverage"},
]


def _join_ids(record: dict) -> str:
    """'SC-013, SC-014' — falls back to the legacy single scenario_id."""
    ids = record.get("scenario_ids") or ([record["scenario_id"]] if record.get("scenario_id") else [])
    return ", ".join(ids)


def _level_for_score(score: int) -> str:
    """Map a numeric confidence_score (0-100) to its bucket label."""
    for band in CONFIDENCE_SCORE_RANGES:
        if band["min"] <= score <= band["max"]:
            return band["level"]
    return "LOW"


def _score_scenario(
    requirement: dict,
    scenarios: list[dict],
    playwright_bodies: dict[str, str],
) -> dict:
    """Compute the confidence score for one requirement across ALL of its
    (non-deprecated) scenarios. Coverage dimensions are the union: a
    dimension is satisfied if any scenario's tags or description/title
    satisfy it. An empty list is the zero-coverage case (CRITICAL_GAP)."""
    req_text    = requirement.get("description", "")
    kane_status = requirement.get("kane_status", "not_run")   # roll-up from Stage 1
    sc_ids      = [s.get("id", "") for s in scenarios if s.get("id")]
    first_sc    = scenarios[0] if scenarios else {}
    sc_id       = first_sc.get("id", "")

    # Feature: the explicit scenario feature wins. Legacy 1:1 scenarios carry
    # no feature and stay GENERAL — keyword-classifying them here would
    # re-score an existing baseline (e.g. GENERAL/LOW → HIGH).
    # Zero scenarios: nothing to inherit, so keyword-classify the requirement
    # text (drives criticality → risk_level for the CRITICAL_GAP record).
    feature = (
        classify_feature("", explicit=first_sc.get("feature")) if scenarios
        else classify_feature(req_text)
    )

    criticality = criticality_for(
        feature, requirement_id=requirement.get("id"), brd_ref=requirement.get("brd_ref"))

    # Dimension scoring — union across the requirement's scenarios.
    has_happy = bool(scenarios)   # any mapped scenario implicitly covers the happy path
    has_negative = has_edge = has_real_body = False
    for sc in scenarios:
        tags = _tags(sc)
        text = _scenario_text(sc) + " " + req_text
        has_negative |= bool(tags & _NEGATIVE_TAGS) or bool(_NEGATIVE_KEYWORDS.search(text))
        has_edge     |= bool(tags & _EDGE_TAGS)     or bool(_EDGE_KEYWORDS.search(text))
        # Check if the test body is more than the generic fallback
        body = playwright_bodies.get(sc.get("id", ""), "")
        has_real_body |= bool(body and "page.title" not in body)

    if not scenarios:
        # Zero automated coverage — the CRITICAL_GAP band (score 0).
        return {
            "requirement_id": requirement.get("id", ""),
            "brd_ref": requirement.get("brd_ref", ""),
            "scenario_id": "",
            "scenario_ids": [],
            "scenario_count": 0,
            "scenario_status": "missing",
            "acceptance_criterion": req_text,
            "feature": feature,
            "criticality": criticality,
            "kane_status": kane_status,
            "confidence_score": 0,
            "confidence_level": "CRITICAL_GAP",
            "coverage_dimensions": {
                "happy_path": False, "negative": False, "edge_case": False,
                "real_body": False,
            },
            "coverage_gaps": ["No scenario mapped — zero automated coverage"],
            "recommendations": [f"Map or ingest at least one test case for '{req_text[:60]}'"],
            "confidence_reason": "No scenario mapped — zero automated coverage",
            "risk_assessment": {
                "criticality": criticality,
                "risk_level": "HIGH" if criticality == "HIGH" else "MEDIUM",
            },
        }

    # Coverage gaps — order matters: the GitHub summary surfaces the first
    # gap in the "Top Gap" column, so list disqualifying signals first so
    # Kane functional failures aren't hidden behind a generic
    # "Missing negative" line.
    gaps: list[str] = []
    if kane_status == "failed":
        gaps.append("Kane AI functional verification failed")
    if kane_status == "not_run":
        gaps.append("Kane AI verification not yet executed")
    if not has_negative:
        gaps.append("Missing negative/error scenario coverage")
    if not has_edge and criticality == "HIGH":
        gaps.append("Missing edge-case coverage for high-criticality feature")

    # Confidence score (0-100) — per-dimension penalties, criticality-aware.
    # See CONFIDENCE_SCORE_RANGES for the score → level mapping the GitHub
    # summary renders as a legend table.
    score = 100
    if not has_negative:
        score -= 25
    if not has_edge and criticality == "HIGH":
        score -= 25
    if kane_status == "not_run":
        score -= 30
    if kane_status == "failed":
        # Cap into the LOW band — a failed functional check is disqualifying.
        score = min(score, 25)
    score = max(0, score)

    confidence = _level_for_score(score)

    # Build recommendation list
    recommendations: list[str] = []
    if not has_negative:
        recommendations.append(f"Add scenario: '{req_text[:60]}' with invalid/error conditions")
    if not has_edge and criticality == "HIGH":
        recommendations.append("Add boundary condition test cases")

    confidence_reason = "; ".join(gaps) if gaps else "All coverage dimensions satisfied"

    return {
        "requirement_id": requirement.get("id", ""),
        "brd_ref": requirement.get("brd_ref", ""),
        "scenario_id": sc_id,                # legacy key — first scenario
        "scenario_ids": sc_ids,
        "scenario_count": len(sc_ids),
        "scenario_status": first_sc.get("status", "active"),
        "acceptance_criterion": req_text,
        "feature": feature,
        "criticality": criticality,
        "kane_status": kane_status,
        "confidence_score": score,
        "confidence_level": confidence,
        "coverage_dimensions": {
            "happy_path":  has_happy,
            "negative":    has_negative,
            "edge_case":   has_edge,
            "real_body":   has_real_body,
        },
        "coverage_gaps": gaps,
        "recommendations": recommendations,
        "confidence_reason": confidence_reason,
        "risk_assessment": {
            "criticality": criticality,
            "risk_level": "HIGH" if confidence == "LOW" and criticality == "HIGH" else
                          "MEDIUM" if confidence != "HIGH" else "LOW",
        },
    }


def run_confidence_analysis(
    requirements: list[dict],
    scenarios: list[dict],
    playwright_bodies: dict[str, str] | None = None,
    output_dir: str = "reports",
) -> dict:
    """
    Score all active scenarios and produce confidence report files.

    Returns a report dict with "summary" and "records" (per-scenario scores).
    Writes:
      - {output_dir}/scenario-confidence-report.json
      - {output_dir}/requirement-confidence-summary.md
      - {output_dir}/high-risk-requirements.json
      - {output_dir}/coverage-gap-analysis.json
    """
    bodies = playwright_bodies or {}
    out    = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Many-to-one: requirement_id → [scenario, ...] (scenarios.json order)
    sc_by_req  = group_scenarios_by_requirement(scenarios)
    dep_by_req = deprecated_by_requirement(scenarios)

    records: list[dict] = []
    for req in requirements:
        rid = req.get("id", "")
        scs = sc_by_req.get(rid, [])
        if not scs and rid in dep_by_req:
            # Tombstone — the AC line is still in the requirements file but
            # every scenario was removed by a release_diff DELETE op. Don't
            # score it as a coverage gap; surface it as a deprecated record so
            # downstream tables can render the tombstone explicitly.
            deps = dep_by_req[rid]
            dep  = deps[0]
            records.append({
                "requirement_id":    rid,
                "brd_ref":           req.get("brd_ref", ""),
                "scenario_id":       dep.get("id", ""),
                "scenario_ids":      [d.get("id", "") for d in deps],
                "scenario_count":    0,
                "function_name":     dep.get("function_name", ""),
                "feature":           classify_feature("", explicit=dep.get("feature")),
                "kane_status":       "skipped",
                "confidence_level":  "DEPRECATED",
                "coverage_dimensions": {},
                "coverage_gaps":     [],
                "confidence_reason": f"Tombstoned in {dep.get('deprecated_in_release', 'prior release')}",
                "risk_assessment":   {"criticality": "DEPRECATED", "risk_level": "DEPRECATED"},
                "deprecated_in_release": dep.get("deprecated_in_release", ""),
            })
            continue
        # Zero scenarios → _score_scenario returns the CRITICAL_GAP record.
        records.append(_score_scenario(req, scs, bodies))

    # Aggregate summary
    by_level: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for r in records:
        by_level[r["confidence_level"]] = by_level.get(r["confidence_level"], 0) + 1

    high_risk = [r for r in records if r["confidence_level"] in ("LOW", "CRITICAL_GAP")]
    missing_neg = [r for r in records if "Missing negative" in " ".join(r.get("coverage_gaps", []))]

    quality_signals = {
        "confidence_gate_passed": by_level["LOW"] == 0,
        "high_criticality_low_confidence": [
            r.get("scenario_id") or r["requirement_id"] for r in high_risk
            if r.get("risk_assessment", {}).get("criticality") == "HIGH"
        ],
        "missing_negative_coverage_count": len(missing_neg),
    }

    summary = {
        "total_requirements": len(requirements),
        "by_confidence_level": by_level,
        "high_confidence_count": by_level["HIGH"],
        "critical_gap_count": by_level["LOW"],
        "missing_negative_coverage": len(missing_neg),
        "missing_edge_case_coverage": sum(
            1 for r in records
            if not r["coverage_dimensions"].get("edge_case")
        ),
        "kane_failed_requirements": sum(1 for r in records if r["kane_status"] == "failed"),
        "quality_signals": quality_signals,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }

    report = {"summary": summary, "records": records}

    # Write reports
    (out / "scenario-confidence-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    # High-risk requirements
    (out / "high-risk-requirements.json").write_text(
        json.dumps({
            "high_risk_count": len(high_risk),
            "requirements": [r["requirement_id"] for r in high_risk],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n", encoding="utf-8"
    )

    # Coverage gap analysis
    (out / "coverage-gap-analysis.json").write_text(
        json.dumps({
            "missing_negative_coverage": [r["requirement_id"] for r in missing_neg],
            "missing_edge_case_coverage": [
                r["requirement_id"] for r in records
                if not r["coverage_dimensions"].get("edge_case")
            ],
            "kane_failures": [r["requirement_id"] for r in records if r["kane_status"] == "failed"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n", encoding="utf-8"
    )

    # Markdown summary
    md_lines = [
        "# Scenario Confidence Report",
        "",
        f"**Total requirements:** {summary['total_requirements']}  ",
        f"**HIGH confidence:** {by_level['HIGH']}  ",
        f"**MEDIUM confidence:** {by_level['MEDIUM']}  ",
        f"**LOW confidence:** {by_level['LOW']}",
        "",
        "## Low Confidence Scenarios",
        "",
    ]
    for r in high_risk:
        md_lines.append(f"- **{_join_ids(r)}** ({r['requirement_id']}): {r['confidence_reason']}")
    if not high_risk:
        md_lines.append("_None — all scenarios have acceptable confidence_")
    md_lines += [
        "",
        "## Requirement Confidence Detail",
        "",
        "| Requirement | Scenarios | Feature | Criticality | Score | Confidence | Kane | Top Gap |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        gaps = r.get("coverage_gaps", []) or []
        score = r.get("confidence_score", "—")
        md_lines.append(
            f"| `{r['requirement_id']}` | {_join_ids(r) or '—'} | {r.get('feature', '')} | "
            f"{r.get('risk_assessment', {}).get('criticality', '')} | {score} | "
            f"{r['confidence_level']} | {r.get('kane_status', '')} | "
            f"{(gaps[0] if gaps else r.get('confidence_reason', ''))[:60]} |"
        )
    (out / "requirement-confidence-summary.md").write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )

    return report


def main() -> int:
    """Stage 2b CLI entrypoint — invoked from ci/agent.py's post-pipeline chain.

    Reads analyzed requirements and scenarios from disk, runs the analysis,
    prints a stage banner with the level breakdown, and exits 0 on success
    (1 if either input file is missing — the pipeline treats Stage 2b as a
    critical script in the chain)."""
    print_stage_header("2b", "SCENARIO_CONFIDENCE", "Score scenario coverage sufficiency and gate on HIGH-criticality gaps")

    req_path = Path("requirements/analyzed_requirements.json")
    sc_path  = Path("scenarios/scenarios.json")
    if not req_path.exists() or not sc_path.exists():
        print(
            f"[scenario_confidence] missing inputs — req={req_path.exists()} sc={sc_path.exists()}",
            file=sys.stderr,
        )
        return 1

    requirements = json.loads(req_path.read_text(encoding="utf-8"))
    scenarios    = json.loads(sc_path.read_text(encoding="utf-8"))

    # Optional: pass through a Playwright-bodies map so the scoring can detect
    # whether each scenario has a dedicated regression body. agent.py exposes
    # PLAYWRIGHT_BODIES and the test asset chain; reading it lazily keeps this
    # module decoupled from agent.py's import side-effects.
    bodies: dict[str, str] = {}
    try:
        from agent import PLAYWRIGHT_BODIES as _bodies  # type: ignore[attr-defined]
        bodies = dict(_bodies)
    except Exception:
        pass

    report = run_confidence_analysis(
        requirements=requirements,
        scenarios=scenarios,
        playwright_bodies=bodies,
        output_dir="reports",
    )

    summary  = report.get("summary", {})
    by_level = summary.get("by_confidence_level", {})
    high_risk_count = len(report.get("high_risk_requirements", []))
    print_stage_result("2b", "SCENARIO_CONFIDENCE", {
        "Scenarios scored":      summary.get("total_requirements", 0),
        "VERY_HIGH":             by_level.get("VERY_HIGH", 0),
        "HIGH":                  by_level.get("HIGH", 0),
        "MEDIUM":                by_level.get("MEDIUM", 0),
        "LOW":                   by_level.get("LOW", 0),
        "CRITICAL_GAP":          by_level.get("CRITICAL_GAP", 0),
        "High-risk requirements": high_risk_count,
        "Outputs":               "reports/scenario-confidence-report.json, requirement-confidence-summary.md",
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
