"""
GitHub Actions step summary renderer.

Pure data renderer — reads only from real artifacts:
  - requirements/analyzed_requirements.json
  - scenarios/scenarios.json
  - reports/normalized_results.json
  - reports/traceability_matrix.json
  - reports/api_details.json
  - reports/release_recommendation.md
  - reports/validation_report.json
  - reports/test_execution_manifest.json
  - kane/objectives.json

Produces factual tables only. No first-person narration. No fabricated values.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import load_run_validity, print_stage_header, print_stage_result, run_banner_lines
from project_config import (
    feature_taxonomy,
    group_scenarios_by_requirement,
    rollup_kane_status,
    rollup_playwright_status,
)


def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return default
    try:
        return json.loads(content)
    except Exception:
        return default


def emit(text):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    # Force UTF-8 on Windows consoles that default to cp1252
    import sys
    out = sys.stdout
    try:
        if hasattr(out, "reconfigure"):
            out.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def verdict_emoji(verdict):
    return {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "INVALID": "⛔"}.get(verdict, "⚪")


def status_icon(status):
    mapping = {
        "passed": "✅",
        "failed": "❌",
        "skipped": "⏭️",
        "data_unavailable": "⚠️",
        "new": "🆕",
        "updated": "🔄",
        "active": "✅",
        "deprecated": "🚫",
    }
    return mapping.get(status, "❓")


def _derive_normalized_status(raw_status: str, he_tasks: list) -> str:
    """Mirror of agent.py _normalize_he_status — used for display when api_details lacks normalized_status."""
    _map = {
        "completed": "PASSED", "passed": "PASSED",
        "failed": "FAILED", "error": "INFRA_FAILURE",
        "aborted": "CANCELLED", "cancelled": "CANCELLED",
        "running": "RUNNING", "initiated": "RUNNING", "queued": "RUNNING",
    }
    n = _map.get((raw_status or "").lower())
    if n:
        return n
    if he_tasks:
        passed = sum(1 for t in he_tasks if t.get("status") == "passed")
        failed = sum(1 for t in he_tasks if t.get("status") == "failed")
        total = passed + failed
        if total == 0:
            return "NOT_EXECUTED"
        if failed == 0:
            return "PASSED"
        if passed == 0:
            return "FAILED"
        return "PARTIAL"
    return "NOT_EXECUTED" if not raw_status else "INFRA_FAILURE"


def _scenario_ids_cell(record: dict) -> str:
    """'SC-013, SC-014' from `scenario_ids`; legacy single `scenario_id` fallback."""
    ids = record.get("scenario_ids") or ([record["scenario_id"]] if record.get("scenario_id") else [])
    return ", ".join(f"`{i}`" for i in ids) if ids else "—"


def _requirement_rollup(trace_json: dict, trace_rows: list, requirements: list) -> list:
    """The traceability `requirements` roll-up (build_traceability Stage 7).
    Derived from `rows` grouped by requirement_id when the key is absent
    (older traceability_matrix.json or a build_traceability that predates it)."""
    rollup = trace_json.get("requirements")
    if isinstance(rollup, list) and rollup:
        return rollup
    brd_by_req = {r.get("id"): r.get("brd_ref", "") for r in requirements if r.get("id")}
    grouped: dict = {}
    for row in trace_rows:
        rid = row.get("requirement_id", "")
        if rid:
            grouped.setdefault(rid, []).append(row)
    derived = []
    for rid, rows in grouped.items():
        sc_ids = [r.get("scenario_id") for r in rows if r.get("scenario_id") and r.get("scenario_id") != "n/a"]
        kane = rollup_kane_status(r.get("kane_ai_result", "") for r in rows)
        pw_statuses = [r.get("playwright_status", "data_unavailable") for r in rows]
        pw = rollup_playwright_status(pw_statuses)
        executed = [s for s in pw_statuses if s in ("passed", "failed")]
        # overall = passed iff every executed scenario passed, ≥1 executed, Kane roll-up passed
        if executed and all(s == "passed" for s in executed) and kane == "passed":
            overall = "passed"
        elif "failed" in executed or kane == "failed":
            overall = "failed"
        else:
            overall = "data_unavailable"
        derived.append({
            "requirement_id": rid,
            "brd_ref": (rows[0].get("brd_ref") or brd_by_req.get(rid, "")),
            "scenario_ids": sc_ids,
            "kane_status": kane,
            "playwright_status": pw,
            "overall": overall,
        })
    return derived


def _he_job_id_from_log():
    for path in ("reports/hyperexecute-cli.log", "reports/hyperexecute_failure_analysis.md"):
        p = Path(path)
        if not p.exists():
            continue
        m = re.search(r"jobId=([\w-]+)", p.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return ""


def main():
    run_url = os.environ.get("RUN_URL", "")

    requirements = load_json("requirements/analyzed_requirements.json", [])
    scenarios = load_json("scenarios/scenarios.json", [])
    manifest = load_json("reports/test_execution_manifest.json", {})
    trace_json = load_json("reports/traceability_matrix.json", {})
    api_details = load_json("reports/api_details.json", {})
    normalized_raw = load_json("reports/normalized_results.json", {})
    normalized = normalized_raw.get("results", [])
    validation = load_json("reports/validation_report.json", {})

    he_api = api_details.get("he_summary", {})
    _seen_he: dict = {}
    for t in api_details.get("he_tasks", []):
        key = t.get("name") or t.get("task_id", "")
        _seen_he.setdefault(key, t)
    he_tasks_api = list(_seen_he.values())

    trace_summary = trace_json.get("summary", {})
    trace_rows = trace_json.get("rows", [])
    result_analysis = trace_json.get("result_analysis", {})

    executed = trace_summary.get("executed", 0)
    passed = trace_summary.get("passed", 0)
    pass_rate = trace_summary.get("pass_rate", 0.0)
    all_browsers = trace_summary.get("browsers_tested", [])
    failing_scenarios = trace_summary.get("failing_scenarios", [])
    untested = trace_summary.get("untested_requirements", [])

    he_job_id = he_api.get("job_id", "") or _he_job_id_from_log()
    he_job_link = he_api.get("job_link", "") or (
        f"https://hyperexecute.lambdatest.com/hyperexecute/task?jobId={he_job_id}"
        if he_job_id else ""
    )
    he_status_raw = he_api.get("status", "") or ""
    # Use pre-computed normalized status when available; fall back to inline derivation
    he_normalized = he_api.get("normalized_status", "") or _derive_normalized_status(he_status_raw, he_tasks_api)
    he_parser_status = he_api.get("parser_status", "unknown")
    he_status = he_normalized or he_status_raw or "NOT_EXECUTED"

    release_md = Path("reports/release_recommendation.md")
    verdict_line = "UNKNOWN"
    recommendation_line = ""
    if release_md.exists():
        for line in release_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("**Verdict:**"):
                verdict_line = line.replace("**Verdict:**", "").replace("(CACHED)", "").strip()
            if line.startswith("## Recommendation") is False and (
                line.startswith("Approve") or line.startswith("Block") or line.startswith("Conditional")
            ):
                recommendation_line = line.strip()

    # ── Header ────────────────────────────────────────────────────────────────
    print_stage_header("9", "GITHUB_SUMMARY", "Writing GitHub Actions step summary")
    from stage_utils import current_release
    emit(f"# Agentic STLC — Pipeline Report · {current_release() or 'unreleased'}")
    emit("")
    for banner in run_banner_lines():
        emit(banner)
    if run_url:
        emit(f"> **Run:** {run_url}")
    emit("")

    # ── Stage Status Table ────────────────────────────────────────────────────
    kane_passed = sum(1 for r in requirements if r.get("kane_status") == "passed")
    kane_total = len(requirements)
    kane_status_icon = "✅" if kane_passed == kane_total else ("🟡" if kane_passed > 0 else "❌")
    _he_icon_map = {
        "PASSED": "✅", "FAILED": "❌", "PARTIAL": "🟡",
        "RUNNING": "⏳", "CANCELLED": "🚫", "INFRA_FAILURE": "🔥",
        "NOT_EXECUTED": "⏭️", "TIMED_OUT": "⏰",
    }
    he_task_pass = he_api.get("task_pass_count", sum(1 for t in he_tasks_api if t.get("status") == "passed"))
    he_task_total = he_api.get("total_tasks", len(he_tasks_api))
    # Reconcile stage-5 badge with task-level reality: HyperExecute's job-level
    # `status` field is independently set and can read "failed" even when every
    # individual task passes (artifact upload warnings, task retries, etc.).
    # When task pass rate is 100% and the parser fetched the status cleanly,
    # trust the task results for the stage badge — the raw job status still
    # appears in the Stage 5 detail table for full transparency.
    if he_task_total > 0 and he_task_pass == he_task_total and he_parser_status == "api_ok":
        he_stage_status = "PASSED"
    else:
        he_stage_status = he_status
    he_icon = _he_icon_map.get(he_stage_status, "⚠️")
    verdict_icon = verdict_emoji(verdict_line)

    emit("## Pipeline Stage Status")
    emit("")
    emit("| Stage | Name | Status | Normalized Status | Details |")
    emit("|-------|------|--------|-------------------|---------|")
    emit(f"| 1 | KaneAI Verification | {kane_status_icon} | {'PASSED' if kane_passed == kane_total else 'PARTIAL' if kane_passed > 0 else 'FAILED'} | {kane_passed}/{kane_total} criteria passed |")
    emit(f"| 2–4 | Scenarios + Test Gen + Selection | ✅ | PASSED | {len([s for s in scenarios if s.get('status') != 'deprecated'])} active tests generated |")
    emit(f"| 5 | HyperExecute Regression | {he_icon} | {he_stage_status} | {he_task_pass}/{he_task_total} tasks · parser: {he_parser_status} |")
    emit(f"| 6 | Result Aggregation | ✅ | PASSED | {len(normalized)} results normalized |")
    emit(f"| 7–8 | Traceability + Verdict | {verdict_icon} | {verdict_line} | see release recommendation below |")
    emit("")

    # ── Execution Links ────────────────────────────────────────────────────────
    emit("## Execution Links")
    emit("")
    if he_job_link:
        emit(f"- [HyperExecute Dashboard]({he_job_link})")
    if run_url:
        emit(f"- [GitHub Actions Run]({run_url})")
    emit("")

    # ── Stage 0: Agentic Release Notes Diff ───────────────────────────────────
    delta = load_json("reports/release_delta.json", None)
    if delta:
        applied = bool(delta.get("applied"))
        summary_0 = delta.get("summary", {}) or {}
        ops = delta.get("operations", []) or []
        unmatched = delta.get("unmatched_items", []) or []
        op_icon = {"ADD": "🟢 ADD", "EDIT": "🟡 EDIT", "DELETE": "🔴 DELETE"}
        emit("## Stage 0 · Agentic Release Notes")
        emit("")
        emit(
            f"**{delta.get('from_release', '?')} → {delta.get('to_release', '?')}**  ·  "
            f"Mode: **{'APPLIED' if applied else 'PROPOSE (no mutations)'}**  ·  "
            f"Match threshold: `{delta.get('threshold', 0)}`"
        )
        emit("")
        emit("| Operation | Count |")
        emit("|---|---|")
        emit(f"| 🟢 ADD       | {summary_0.get('ADD', 0)} |")
        emit(f"| 🟡 EDIT      | {summary_0.get('EDIT', 0)} |")
        emit(f"| 🔴 DELETE    | {summary_0.get('DELETE', 0)} |")
        emit(f"| ⚠️ Unmatched | {summary_0.get('UNMATCHED', 0)} |")
        emit("")
        if ops:
            emit("| Op | Scenario | Requirement | Issue | Score | Change |")
            emit("|---|---|---|---|---|---|")
            for op in ops:
                op_type = op.get("op", "")
                item_text = (op.get("item_text") or "").replace("|", "\\|")
                prev_text = (op.get("prev_text") or "").replace("|", "\\|")
                # Build the "Change" cell with before → after when applicable.
                # Use <br> for in-cell line breaks (GitHub-flavored markdown).
                if op_type == "EDIT" and prev_text:
                    change_cell = f"**was:** {prev_text}<br>**now:** {item_text}"
                elif op_type == "DELETE" and prev_text:
                    change_cell = f"**removed:** {prev_text}"
                elif op_type == "ADD":
                    change_cell = f"**new:** {item_text}"
                else:
                    change_cell = item_text
                # Many-to-one: show every scenario bound to the requirement;
                # say "uncovered" outright when there are none.
                sc_ids = op.get("sc_ids") or ([op["sc_id"]] if op.get("sc_id") else [])
                sc_cell = ", ".join(f"`{i}`" for i in sc_ids) if sc_ids else "⚠️ uncovered"
                emit(
                    f"| {op_icon.get(op_type, op_type)} | {sc_cell} | "
                    f"`{op.get('requirement_id') or '—'}` | {op.get('issue') or '—'} | "
                    f"{op.get('match_score', 0):.2f} | {change_cell} |"
                )
            emit("")
        if unmatched:
            emit("**Unmatched items** (manual scenario assignment required):")
            emit("")
            emit("| Section | Best score | Reason | Item |")
            emit("|---|---|---|---|")
            for u in unmatched:
                emit(
                    f"| {u.get('section', '')} | {u.get('best_score', 0):.2f} | "
                    f"{u.get('reason', '')} | {u.get('text', '')} |"
                )
            emit("")
        if not applied:
            emit("> ℹ️ Preview only. Run with `apply_release_delta=true` to commit "
                 "operations to `scenarios.json` and freeze the new release lock.")
            emit("")

    # ── Stage 1: Kane AI Requirement Analysis ─────────────────────────────────
    emit("## Stage 1 · Kane AI Functional Verification")
    emit("")
    if not requirements:
        emit("_No requirements data found in analyzed_requirements.json._")
    else:
        # Scenario id → title so a roll-up reads as *what* was tested, not just which id.
        sc_title = {
            s.get("id"): (s.get("title") or s.get("kane_objective") or "")
            for s in (scenarios or []) if s.get("id")
        }
        emit(f"| Req ID | Acceptance Criterion | Scenario | Kane Status | What Kane Observed |")
        emit("|---|---|---|---|---|")
        for r in requirements:
            kane_status = r.get("kane_status", "unknown")
            icon = status_icon(kane_status)
            criterion = r.get("description", "")[:60]
            per_scenario = r.get("scenarios") or []
            if per_scenario:
                # Many-to-one: requirement roll-up row, then one row per
                # scenario Kane actually ran (Stage 1 writes `scenarios`).
                emit(f"| `{r['id']}` | {criterion} | {len(per_scenario)} scenario(s) | {icon} {kane_status} *(roll-up)* | — |")
                for s in per_scenario:
                    s_status = s.get("kane_status", "unknown")
                    s_links = s.get("kane_links", []) or []
                    s_link = f" [session]({s_links[0]})" if s_links else ""
                    if s.get("kane_case_link"):
                        s_link += f" · [case]({s['kane_case_link']})"
                    sid = s.get("scenario_id", "")
                    title = (s.get("title") or sc_title.get(sid) or "").replace("|", "\\|")
                    sc_cell = f"`{sid}` {title}".strip()
                    emit(
                        f"| ↳ | | {sc_cell} | {status_icon(s_status)} {s_status}{s_link} | "
                        f"{s.get('kane_one_liner', '') or '—'} |"
                    )
            else:
                # Legacy 1:1 record — single line
                one_liner = r.get("kane_one_liner", "") or "—"
                emit(f"| `{r['id']}` | {criterion} | — | {icon} {kane_status} | {one_liner} |")
        emit("")

        kane_failed = [r for r in requirements if r.get("kane_status") == "failed"]
        if kane_failed:
            emit(f"**{len(kane_failed)} criterion/criteria failed Kane AI verification:**")
            for r in kane_failed:
                emit(f"- ❌ `{r['id']}` {r.get('title', '')} — {r.get('kane_one_liner', '')}")
            emit("")

    # ── Stage 2: Scenario Management ──────────────────────────────────────────
    emit("## Stage 2 · Scenario Catalog")
    emit("")
    if scenarios:
        new_sc = [s for s in scenarios if s.get("status") == "new"]
        updated_sc = [s for s in scenarios if s.get("status") == "updated"]
        active_sc = [s for s in scenarios if s.get("status") == "active"]
        deprecated_sc = [s for s in scenarios if s.get("status") == "deprecated"]
        emit(
            f"Total: **{len(scenarios)}** — "
            f"{len(active_sc)} active, {len(new_sc)} new, "
            f"{len(updated_sc)} updated, {len(deprecated_sc)} deprecated"
        )
        if new_sc or updated_sc:
            emit("")
            emit("| Scenario | Status | Requirement |")
            emit("|---|---|---|")
            for s in new_sc + updated_sc:
                emit(f"| `{s['id']}` {s.get('title', '')} | {status_icon(s['status'])} {s['status']} | `{s.get('requirement_id', '')}` |")
    emit("")

    # ── Stage 2b: Scenario Confidence ──────────────────────────────────────────
    confidence = load_json("reports/scenario-confidence-report.json", None)
    if confidence:
        summary_2b = confidence.get("summary", {}) or {}
        by_level = summary_2b.get("by_confidence_level", {}) or {}
        records  = confidence.get("records", []) or []
        high_risk = [r for r in records if r.get("risk_assessment", {}).get("risk_level") == "HIGH"]
        emit("## Stage 2b · Scenario Confidence Analysis")
        emit("")
        if high_risk:
            emit(f"**Confidence gate:** ❌ FAILED — HIGH criticality requirements with LOW/CRITICAL_GAP confidence: {len(high_risk)}")
        else:
            emit("**Confidence gate:** ✅ PASSED")
        emit("")
        emit("**Confidence Score Ranges** _(score → level)_:")
        emit("")
        emit("| Score Range | Level | Meaning |")
        emit("|---|---|---|")
        emit("| 90 – 100 | 🟢 VERY_HIGH    | All coverage dimensions satisfied |")
        emit("| 75 – 89  | 🟡 HIGH         | Core flow validated; one minor coverage gap |")
        emit("| 50 – 74  | 🟠 MEDIUM       | Happy path present; two important gaps remain |")
        emit("| 1 – 49   | 🔴 LOW          | Three or more gaps OR Kane functional failure |")
        emit("| 0        | 🚨 CRITICAL_GAP | No scenario mapped — zero automated coverage |")
        emit("")
        emit("**Distribution across this release:**")
        emit("")
        emit("| Level | Count |")
        emit("|---|---|")
        emit(f"| 🟢 VERY_HIGH    | {by_level.get('VERY_HIGH', 0)} |")
        emit(f"| 🟡 HIGH         | {by_level.get('HIGH', 0)} |")
        emit(f"| 🟠 MEDIUM       | {by_level.get('MEDIUM', 0)} |")
        emit(f"| 🔴 LOW          | {by_level.get('LOW', 0)} |")
        emit(f"| 🚨 CRITICAL_GAP | {by_level.get('CRITICAL_GAP', 0)} |")
        emit("")
        if records:
            emit("### Requirement Confidence Detail")
            emit("")
            emit("| Requirement | Scenario | Feature | Criticality | Score | Confidence | Kane | Top Gap |")
            emit("|---|---|---|---|---|---|---|---|")
            level_icon = {
                "VERY_HIGH": "🟢 VERY_HIGH",
                "HIGH":      "🟡 HIGH",
                "MEDIUM":    "🟠 MEDIUM",
                "LOW":       "🔴 LOW",
                "CRITICAL_GAP": "🚨 CRITICAL_GAP",
                "DEPRECATED": "⚰️ DEPRECATED",
            }
            kane_icon = {"passed": "✅ passed", "failed": "❌ failed"}
            for r in records:
                lvl   = r.get("confidence_level", "")
                kane  = r.get("kane_status", "")
                if lvl == "DEPRECATED":
                    dep_in = r.get("deprecated_in_release", "prior release")
                    emit(
                        f"| `{r.get('requirement_id', '')}` | {_scenario_ids_cell(r)} | "
                        f"— | ⚰️ DEPRECATED | — | ⚰️ DEPRECATED | ⏭️ skipped | "
                        f"removed in {dep_in} |"
                    )
                    continue
                gaps  = r.get("coverage_gaps", []) or []
                top_gap = (gaps[0] if gaps else "")[:60]
                score = r.get("confidence_score", "")
                emit(
                    f"| `{r.get('requirement_id', '')}` | {_scenario_ids_cell(r)} | "
                    f"{r.get('feature', '')} | {r.get('criticality', '')} | "
                    f"**{score}** | {level_icon.get(lvl, lvl)} | {kane_icon.get(kane, kane)} | "
                    f"{top_gap}{'…' if len(top_gap) == 60 else ''} |"
                )
            emit("")
            _crit, _, _ = feature_taxonomy()
            _high_feats = ", ".join(f for f, c in _crit.items() if c == "HIGH") or "none configured"
            emit(f"> **How to close these gaps:** A *Missing negative/error scenario coverage* gap deducts **25 points** from the confidence score, and for HIGH-criticality features ({_high_feats}) it compounds with the edge-case penalty — which is why happy-path-only HIGH-crit ACs land at score **50 / MEDIUM** while MEDIUM-crit ACs with the same gap sit at **75 / HIGH**. Resolve it by listing the negative or error case as its own acceptance criterion in the BRD and naming it in the release notes (e.g. *\"User cannot submit a task with an empty title\"*). The pipeline picks the new AC up on the next release-notes diff, generates a scenario for it, and Kane verifies it against the AUT — closing the gap and lifting the score on the next run.")
            emit("")

    # ── Stage 3: Test Generation ───────────────────────────────────────────────
    emit("## Stage 3 · Generated Playwright Tests")
    emit("")
    # Source from scenarios.json directly — kane/objectives.json is no longer
    # written by the pipeline (test generation happens via collect_kane_exports
    # in agent.py Stage 3a, which writes tests/playwright/test_regression.py).
    active_scenarios = [s for s in scenarios if s.get("status") != "deprecated"]
    if active_scenarios:
        emit(f"**{len(active_scenarios)}** test function(s) in `tests/playwright/test_regression.py`:")
        emit("")
        emit("| Scenario | Test Case | Function |")
        emit("|---|---|---|")
        for sc in active_scenarios:
            fn = sc.get("function_name", "")
            emit(f"| `{sc['id']}` | `{sc.get('test_case_id', '')}` | `{fn}` |")
    else:
        emit("_No test generation data available._")
    emit("")

    # ── Stage 4: Test Selection ────────────────────────────────────────────────
    emit("## Stage 4 · Test Selection")
    emit("")
    selected = manifest.get("selected_scenarios", [])
    run_type = manifest.get("run_type", "unknown")
    emit(f"Run type: **{run_type}** · **{len(selected)}** scenario(s) submitted to HyperExecute")
    emit("")

    # ── Stage 5: HyperExecute Execution (multi-browser) ───────────────────────
    emit("## Stage 5 · HyperExecute Regression (Multi-Browser)")
    emit("")

    he_passed_count = he_api.get("task_pass_count", sum(1 for t in he_tasks_api if t.get("status") == "passed"))
    he_failed_count = he_api.get("task_fail_count", sum(1 for t in he_tasks_api if t.get("status") == "failed"))
    he_total_count  = he_api.get("total_tasks") or len(he_tasks_api) or len(selected)

    emit("| Metric | Raw Value | Normalized | Evidence |")
    emit("|--------|-----------|------------|----------|")
    if he_job_link:
        emit(f"| HyperExecute Job | `{he_job_id}` | — | [Open in LambdaTest ↗]({he_job_link}) |")
    else:
        emit(f"| HyperExecute Job ID | `{he_job_id or 'n/a'}` | — | job not submitted or ID extraction failed |")
    # Mirror the stage-5 badge reconciliation here: when task pass rate is
    # 100% and the parser fetched the status cleanly, the badge promotes the
    # job to PASSED even though HE's job-level field still says "failed".
    # The detail row should agree with the badge — otherwise readers see
    # "Stage 5 ✅ PASSED" up top and "Job Status FAILED" below and assume
    # one of them is wrong.
    reconciled_note = ""
    if he_stage_status != he_status and he_stage_status == "PASSED":
        reconciled_note = " *(reconciled: all tasks passed)*"
    emit(f"| Job Status | `{he_status_raw or 'not returned'}` | **{he_stage_status}**{reconciled_note} | source: {he_parser_status} |")
    emit(f"| Parser Status | `{he_parser_status}` | — | how status was resolved |")
    emit(f"| Browsers | — | — | {', '.join(all_browsers) if all_browsers else 'chrome (default)'} |")
    emit(f"| Total tasks | {he_total_count} | — | submitted to HyperExecute |")
    emit(f"| ✅ Passed | {he_passed_count} | — | task-level results |")
    emit(f"| ❌ Failed | {he_failed_count} | — | task-level results |")
    emit(f"| Pass rate | {pass_rate}% | — | across all browsers |")
    emit("")

    _parser_notes = {
        "api_ok":             "✅ Job status fetched directly from HyperExecute API",
        "derived_from_tasks": "ℹ️ Job API unreachable — status derived from individual task results",
        "mcp_unavailable":    "⚠️ MCP connection failed and HE REST API returned no status — check LT credentials",
        "not_executed":       "⏭️ HyperExecute stage was not executed (no job submitted)",
    }
    note = _parser_notes.get(he_parser_status, f"ℹ️ Parser status: `{he_parser_status}`")
    emit(f"> **Status resolution:** {note}")
    emit("")

    if he_tasks_api:
        emit("### Per-Test Results")
        emit("")
        emit("| Test | Status | Session |")
        emit("|---|---|---|")
        for task in he_tasks_api:
            icon = "✅" if task.get("status") == "passed" else "❌"
            session = f"[View session]({task['session_link']})" if task.get("session_link") else "—"
            emit(f"| `{task.get('name') or task.get('task_id', 'unknown')}` | {icon} {task.get('status', 'unknown')} | {session} |")
        emit("")

    # ── Multi-browser breakdown from normalized results ────────────────────────
    # Per-browser status comes from the traceability rows, which include results
    # carried forward in an incremental run (normalized_results.json only holds
    # this run's, plus data_unavailable placeholders for everything else).
    breakdown_rows = [r for r in trace_rows
                      if r.get("scenario_id", "n/a") != "n/a" and r.get("overall") != "deprecated"]
    if breakdown_rows and all_browsers:
        emit("### Browser Breakdown")
        emit("")
        emit("| Scenario | " + " | ".join(b.capitalize() for b in all_browsers) + " | Result from |")
        emit("|---| " + " | ".join("---" for _ in all_browsers) + " |---|")
        for row in sorted(breakdown_rows, key=lambda r: r["scenario_id"]):
            per_browser = row.get("playwright_per_browser", {})
            cells = " | ".join(
                f"{status_icon(per_browser.get(b, 'data_unavailable'))} {per_browser.get(b, '—')}"
                for b in all_browsers
            )
            source = (f"carried from {row['playwright_carried_from']}"
                      if row.get("playwright_carried_from") else "this run")
            emit(f"| `{row['scenario_id']}` | {cells} | {source} |")
        emit("")

    # ── Stage 6: Traceability Matrix ───────────────────────────────────────────
    emit("## Stage 6 · Traceability Matrix")
    emit("")
    emit(
        f"**{passed}/{executed}** regression tests passed across "
        f"**{len(all_browsers) or 1}** browser(s) — {pass_rate}% pass rate"
    )
    if trace_summary.get("carried_forward"):
        emit("")
        emit(f"Incremental run: **{trace_summary.get('executed_this_run')}** executed on HyperExecute "
             f"this run; **{trace_summary['carried_forward']}** unchanged scenario(s) carried forward "
             "from the last archived release (marked in the matrix).")
    emit("")

    # Requirement roll-up (many-to-one): one line per AC, scenarios comma-joined.
    req_rollup = _requirement_rollup(trace_json, trace_rows, requirements)
    if req_rollup:
        emit("### Requirement Roll-up")
        emit("")
        emit("| Req | BRD ref | Scenarios | Kane | Playwright | Overall |")
        emit("|---|---|---|---|---|---|")
        for rr in req_rollup:
            kane = rr.get("kane_status", "not_run")
            pw = rr.get("playwright_status", "data_unavailable")
            overall = rr.get("overall", "unknown")
            emit(
                f"| `{rr.get('requirement_id', '')}` | {rr.get('brd_ref') or '—'} | "
                f"{_scenario_ids_cell(rr)} | {status_icon(kane)} {kane} | "
                f"{status_icon(pw)} {pw} | {status_icon(overall)} {overall} |"
            )
        emit("")

    # Coverage gaps — requirements with no (non-deprecated) scenario at all.
    # Tombstoned requirements (only deprecated scenarios) are intentional
    # removals and are rendered as DEPRECATED elsewhere, not as gaps.
    _live_by_req = group_scenarios_by_requirement(scenarios)
    _dep_by_req = group_scenarios_by_requirement(scenarios, include_deprecated=True)
    uncovered_reqs = [
        r for r in requirements
        if r.get("id") and not _live_by_req.get(r["id"]) and not _dep_by_req.get(r["id"])
    ]
    if uncovered_reqs:
        emit(f"> 🚨 **Coverage gaps — {len(uncovered_reqs)} requirement(s) with no scenario:** "
             + ", ".join(
                 f"`{r['id']}`" + (f" ({r['brd_ref']})" if r.get("brd_ref") else "")
                 for r in uncovered_reqs
             ))
        emit("")

    if trace_rows:
        browser_header = " | ".join(b.capitalize() for b in all_browsers) if all_browsers else "Browser"
        browser_sep = " | ".join("---" for _ in (all_browsers or ["chrome"]))
        emit(
            f"| Req | Acceptance Criterion | Scenario | Test Case | Kane AI | Kane Session | What Kane Saw | "
            f"{browser_header} | Playwright | Session | Overall |"
        )
        emit(f"|---|---|---|---|---|---|---|{browser_sep}|---|---|---|")

        for row in trace_rows:
            req_id = row.get("requirement_id", "")
            criterion = (row.get("acceptance_criterion", ""))[:55]
            if len(row.get("acceptance_criterion", "")) > 55:
                criterion += "…"
            kane = row.get("kane_ai_result", "unknown")
            kane_link = row.get("kane_session_link", "")
            kane_cell = f"[session]({kane_link})" if kane_link else "—"
            if row.get("kane_case_link"):
                kane_cell = ((f"[session]({kane_link}) · " if kane_link else "")
                             + f"[case]({row['kane_case_link']})")
            one_liner = row.get("kane_one_liner", "") or "—"
            per_browser = row.get("playwright_per_browser", {})
            browser_cells = " | ".join(
                f"{status_icon(per_browser.get(b, 'data_unavailable'))} {per_browser.get(b, '—')}"
                for b in all_browsers
            ) if all_browsers else "—"
            pw = row.get("playwright_status", "data_unavailable")
            sel_link = row.get("session_link", "")
            sel_cell = f"[session]({sel_link})" if sel_link else "—"
            overall = row.get("overall", "unknown")
            overall_icon = status_icon(overall)
            emit(
                f"| `{req_id}` | {criterion} | `{row.get('scenario_id', 'n/a')}` "
                f"| `{row.get('test_case_id', 'n/a')}` | {kane} | {kane_cell} | {one_liner} "
                f"| {browser_cells} | {pw} | {sel_cell} | {overall_icon} {overall} |"
            )
        emit("")

        # Kane AI detail (collapsible)
        if any(row.get("kane_steps") or row.get("kane_summary") for row in trace_rows):
            emit("<details>")
            emit("<summary>Kane AI verification steps (expand)</summary>")
            emit("")
            for row in trace_rows:
                if not row.get("kane_steps") and not row.get("kane_summary"):
                    continue
                emit(f"**`{row['requirement_id']}` — {row.get('acceptance_criterion', '')}**")
                emit("")
                for step in row.get("kane_steps", []):
                    emit(f"- {step}")
                if row.get("kane_summary"):
                    emit("")
                    emit(f"_{row['kane_summary']}_")
                emit("")
            emit("</details>")
            emit("")

    if result_analysis:
        emit("### Result Analysis")
        emit("")
        emit(f"- **Overall health:** {result_analysis.get('overall_health', 'unknown')}")
        emit(f"- **Risk level:** {result_analysis.get('risk_level', 'unknown')}")
        emit(f"- **Kane AI pass rate:** {result_analysis.get('kane_pass_rate', 0)}%")
        emit(f"- **Playwright pass rate:** {result_analysis.get('playwright_pass_rate', result_analysis.get('selenium_pass_rate', 0))}%")
        emit("")
        for finding in result_analysis.get("key_findings", []):
            emit(f"- {finding}")
        hint = result_analysis.get("recommendation_hint", "")
        if hint:
            emit("")
            emit(f"> {hint}")
        emit("")

    if failing_scenarios:
        emit("**Failing scenarios:**")
        for sc in failing_scenarios:
            emit(f"- ❌ `{sc}`")
        emit("")

    if untested and executed > 0:
        emit("**No execution data for:**")
        for req in untested:
            emit(f"- ⚠️ `{req}` (data_unavailable)")
        emit("")

    # ── Validation Report ──────────────────────────────────────────────────────
    if validation:
        valid = validation.get("valid", True)
        v_errors = validation.get("errors", [])
        v_warnings = validation.get("warnings", [])
        emit("## Data Validation")
        emit("")
        label = "✅ VALID" if valid else "❌ INVALID"
        emit(f"Traceability integrity: **{label}**")
        if v_errors:
            emit("")
            for e in v_errors:
                emit(f"- ❌ {e}")
        if v_warnings:
            emit("")
            for w in v_warnings:
                emit(f"- ⚠️ {w}")
        emit("")

    # ── Coverage Analysis ──────────────────────────────────────────────────────
    coverage_data = load_json("reports/coverage_report.json", {})
    if coverage_data:
        cov_sum  = coverage_data.get("summary", {})
        cov_reqs = coverage_data.get("requirements", [])
        feat_rollup = coverage_data.get("feature_rollup", [])

        emit("## Requirement Coverage Analysis")
        emit("")
        emit("| Metric | Value |")
        emit("|--------|-------|")
        emit(f"| Total Requirements | {cov_sum.get('total_requirements', '?')} |")
        emit(f"| Fully Covered | {cov_sum.get('covered_full', 0)} "
             f"({cov_sum.get('coverage_pct', 0)}%) |")
        emit(f"| Partially Covered | {cov_sum.get('covered_partial', 0)} |")
        emit(f"| Uncovered | {cov_sum.get('uncovered', 0)} |")
        if cov_sum.get('deprecated', 0):
            emit(f"| ⚰️ Deprecated (tombstone) | {cov_sum.get('deprecated', 0)} |")
        emit(f"| Negative Test Coverage | {cov_sum.get('negative_coverage_pct', 0)}% |")
        emit(f"| HyperExecute Coverage | {cov_sum.get('he_coverage_pct', 0)}% |")
        emit(f"| Flaky Requirements | {cov_sum.get('flaky_count', 0)} |")
        emit(f"| High-Risk Requirements | {cov_sum.get('high_risk_count', 0)} |")
        emit(f"| Missing Scenario Types | {cov_sum.get('missing_scenario_types', 0)} |")
        emit("")

        # Per-requirement coverage table
        emit("### Requirement Coverage Detail")
        emit("")
        emit("| Requirement | Coverage | Tests | Pass | Fail | Missing | Risk |")
        emit("|-------------|----------|-------|------|------|---------|------|")
        for r in cov_reqs:
            es = r.get("execution_status", {})
            cov_status = r.get("coverage_status", "NONE")
            risk_level = r.get("risk_level", "")
            # DEPRECATED tombstones are intentional, not gaps — render them
            # as a distinct row so readers don't read "no coverage / HIGH
            # risk" and assume a real coverage hole.
            if cov_status == "DEPRECATED":
                dep_in = r.get("deprecated_in_release", "prior release")
                emit(
                    f"| `{r.get('requirement_id', '?')}` | ⚰️ DEPRECATED "
                    f"| — | — | — | — | — *(removed in {dep_in})* |"
                )
                continue
            cov_icon = {"FULL": "✅", "PARTIAL": "🟡", "NONE": "❌"}.get(cov_status, "❓")
            risk_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(risk_level, "⚪")
            emit(
                f"| `{r.get('requirement_id', '?')}` | {cov_icon} {cov_status} "
                f"| {es.get('total', 0)} | {es.get('passed', 0)} | {es.get('failed', 0)} "
                f"| {len(r.get('missing_scenarios', []))} | {risk_icon} {risk_level or '?'} |"
            )
        emit("")

        # Feature heatmap
        if feat_rollup:
            emit("### Feature Coverage Heatmap")
            emit("")
            emit("| Feature | Criticality | Total | Covered | Partial | Uncovered |")
            emit("|---------|-------------|-------|---------|---------|-----------|")
            for f in feat_rollup:
                crit_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(f.get("criticality", ""), "⚪")
                emit(
                    f"| {f['feature']} | {crit_icon} {f['criticality']} "
                    f"| {f['total']} | {f['covered']} | {f['partial']} | {f['none']} |"
                )
            emit("")

        # Missing scenarios
        missing_data = load_json("reports/missing_scenarios.json", {})
        missing_list = missing_data.get("missing", [])
        if missing_list:
            emit("### Missing Scenario Types (Coverage Gaps)")
            emit("")
            for m in missing_list:
                req_id = m.get("requirement_id", "?")
                feat   = m.get("feature", "?")
                crit   = m.get("criticality", "?")
                emit(f"**`{req_id}`** — {feat} (criticality: {crit})")
                for ms in m.get("missing", []):
                    type_badge = {"negative": "🔴 NEGATIVE", "edge_case": "🟡 EDGE",
                                  "happy_path": "🟢 HAPPY"}.get(ms["type"], ms["type"].upper())
                    emit(f"- `[{type_badge}]` {ms['description']}")
                emit("")
            emit("> **Why these aren't auto-generated:** This is a coverage-gap report, not a pipeline failure. The pipeline only generates tests from explicit acceptance criteria in the BRD — auto-authoring negative/edge scenarios would require either an LLM-driven test generator (explicitly disallowed by the deterministic-pipeline design) or expanding the BRD's contract. To close a gap, add an explicit AC for it to the BRD and the pipeline will pick it up on the next release-notes diff.")
            emit("")

        # Flaky requirements
        flaky_data = load_json("reports/flaky_requirements.json", {})
        flaky_list = flaky_data.get("flaky", [])
        if flaky_list:
            emit("### Flaky Requirements")
            emit("")
            for f in flaky_list:
                emit(f"- ⚠️ `{f['requirement_id']}` ({f['feature']}) — "
                     f"{f['retry_count']} retries  scenarios: {', '.join(f['scenarios'])}")
            emit("")

    # ── Quality Gates ──────────────────────────────────────────────────────────
    qg_data = load_json("reports/quality_gates.json", {})
    if qg_data:
        gates_passed = qg_data.get("gates_passed", True)
        qg_icon = "✅" if gates_passed else "❌"
        emit("## Quality Gates")
        emit("")
        emit(f"**Overall: {qg_icon} {'PASSED' if gates_passed else 'FAILED'}**  "
             f"({qg_data.get('critical_failures', 0)} critical failures, "
             f"{qg_data.get('warnings', 0)} warnings)")
        emit("")
        emit("| Gate | Severity | Status | Actual | Threshold |")
        emit("|------|----------|--------|--------|-----------|")
        for g in qg_data.get("gates", []):
            g_icon = "✅" if g["passed"] else ("❌" if g["severity"] == "CRITICAL" else "⚠️")
            sev_icon = "🔴" if g["severity"] == "CRITICAL" else "🟡"
            emit(
                f"| {g['gate']} | {sev_icon} {g['severity']} | {g_icon} "
                f"| {g['actual']} {g.get('unit', '')} | {g['threshold']} {g.get('unit', '')} |"
            )
        emit("")

    # ── Impact Analysis ────────────────────────────────────────────────────────
    impact_data = load_json("reports/impacted_requirements.json", {})
    if impact_data and impact_data.get("changed_file_count", 0) > 0:
        max_impact = impact_data.get("max_impact", "NONE")
        impact_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(max_impact, "⚪")
        emit("## Change Impact Analysis")
        emit("")
        emit(f"**{len(impact_data.get('changed_files', []))} file(s) changed — "
             f"max impact: {impact_icon} {max_impact}**")
        emit("")
        emit(f"> {impact_data.get('recommendation', '')}")
        emit("")
        impacted_reqs = impact_data.get("impacted_requirements", [])
        if impacted_reqs:
            emit(f"**{len(impacted_reqs)} requirement(s) impacted:** "
                 + ", ".join(f"`{r}`" for r in impacted_reqs[:10])
                 + ("..." if len(impacted_reqs) > 10 else ""))
            emit("")
        impacted_feats = impact_data.get("impacted_features", [])
        if impacted_feats:
            emit(f"**Features affected:** {', '.join(impacted_feats)}")
            emit("")

    # ── RCA — Root Cause Analysis ──────────────────────────────────────────────
    rca_data = load_json("reports/rca_report.json", {})
    if rca_data and rca_data.get("total_failed", 0) > 0:
        emit("## Root Cause Analysis — Failed Tests")
        emit("")
        if rca_data.get("skipped_no_creds"):
            emit("> ⚠️ RCA skipped — `LT_USERNAME` / `LT_ACCESS_KEY` not set.")
            emit("> Configure these secrets to enable LambdaTest AI root cause analysis.")
            emit("")
        else:
            emit(f"**{rca_data['total_failed']} failed test(s) — "
                 f"{rca_data.get('rca_fetched', 0)} RCA analyses retrieved**")
            emit("")
            for a in rca_data.get("analyses", []):
                sc_id   = a.get("scenario_id", "?")
                req_id  = a.get("requirement_id", "?")
                browser = a.get("browser", "?")
                link    = a.get("session_link", "")
                rca     = a.get("root_cause", "N/A")
                ctx     = a.get("kane_context", {})
                session_md = f"[session]({link})" if link else "—"
                emit(f"**❌ `{sc_id}` / `{req_id}` — {browser}** — {session_md}")
                if ctx.get("acceptance_criterion"):
                    emit(f"> _{ctx['acceptance_criterion']}_")
                emit(f"> **Root Cause:** {rca[:300]}")
                emit("")

    # ── Release Recommendation ─────────────────────────────────────────────────
    emit("## Release Recommendation")
    emit("")
    icon = verdict_emoji(verdict_line)
    emit(f"### {icon} {verdict_line}")
    emit("")
    if recommendation_line:
        emit(recommendation_line)
    emit("")
    emit(f"- Requirements covered: **{trace_summary.get('requirements_covered', '?')}/{trace_summary.get('requirements_total', '?')}**")
    emit(f"- Browsers tested: **{', '.join(all_browsers) or 'none'}**")
    emit(f"- Playwright pass rate: **{pass_rate}%** ({passed} passed, {executed - passed} failed)")
    if run_url:
        emit(f"- Full run details: {run_url}")
    emit("")


    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    summary_len = len(Path(summary_path).read_text(encoding="utf-8")) if summary_path and Path(summary_path).exists() else 0
    print_stage_result("9", "GITHUB_SUMMARY", {
        "Sections":       "stage-status, kane-results, playwright-results, traceability, verdict",
        "Verdict":        verdict_line,
        "HE dashboard":   he_job_link or "N/A",
        "Summary size":   f"{summary_len:,} chars" if summary_len else "stdout only",
        "Output":         summary_path or "stdout",
    })


if __name__ == "__main__":
    main()
