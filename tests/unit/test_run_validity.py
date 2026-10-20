"""DEMO_MODE honesty, HyperExecute-skipped invalid runs, and release-driven re-runs."""
import json
import sys
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parents[2] / "ci"
sys.path.insert(0, str(CI))

import agent  # noqa: E402
import analyze_requirements  # noqa: E402
import release_diff as rd  # noqa: E402
import release_recommendation  # noqa: E402
import stage_utils  # noqa: E402

REQS = [{"id": "AC-001", "description": "first"}, {"id": "AC-002", "description": "second"}]
GROUPED = {"AC-001": [{"id": "SC-001"}], "AC-002": [{"id": "SC-002"}]}


# ── DEMO_MODE ──────────────────────────────────────────────────────────────

def test_demo_mode_missing_cache_entry_is_not_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci" / "demo_kane_results.json").write_text(json.dumps([{"status": "passed"}]))
    results = analyze_requirements.load_demo_results(REQS, GROUPED)
    by_sc = {r["scenario_id"]: r["status"] for r in results}
    assert by_sc == {"SC-001": "passed", "SC-002": "not_run"}


def test_cached_banner():
    lines = stage_utils.run_banner_lines({"kane_mode": "cached", "hyperexecute": "ran", "valid": True})
    assert any("CACHED" in l for l in lines)
    assert stage_utils.run_banner_lines({"kane_mode": "live", "hyperexecute": "ran", "valid": True}) == []


# ── HyperExecute skipped → invalid run ─────────────────────────────────────

@pytest.mark.parametrize("reason,he,valid", [
    ("HyperExecute CLI not found at ./hyperexecute", "skipped", False),
    ("LT_USERNAME / LT_ACCESS_KEY not set", "skipped", False),
    ("no tests selected", "not_needed", True),
])
def test_write_run_validity(tmp_path, monkeypatch, reason, he, valid):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "HE_SKIP_REASON", reason)
    v = agent.write_run_validity([{"kane_mode": "cached"}], job_id="")
    assert (v["hyperexecute"], v["valid"], v["kane_mode"]) == (he, valid, "cached")
    assert json.loads((tmp_path / "reports" / "run_validity.json").read_text()) == v


def test_verdict_invalid_when_hyperexecute_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    summary = {"requirements_covered": 2, "requirements_total": 2, "executed": 2,
               "passed": 2, "pass_rate": 100.0, "failing_scenarios": [], "untested_requirements": []}
    (reports / "traceability_matrix.json").write_text(json.dumps({"summary": summary}))
    (reports / "run_validity.json").write_text(json.dumps(
        {"kane_mode": "cached", "hyperexecute": "skipped", "hyperexecute_reason": "CLI missing", "valid": False}))
    monkeypatch.setattr(sys, "argv", ["release_recommendation.py"])
    release_recommendation.main()
    data = json.loads((reports / "release_recommendation.json").read_text())
    md = (reports / "release_recommendation.md").read_text(encoding="utf-8")
    assert data["verdict"] == "INVALID" and data["valid"] is False and data["cached"] is True
    assert "not a valid run" in md and "CACHED" in md and "GREEN" not in md


# ── Changed requirement re-runs once (release_diff EDIT → Stage 2) ──────────

def test_changed_requirement_reruns_once(tmp_path, monkeypatch):
    sc_path = tmp_path / "scenarios.json"
    sc_path.write_text(json.dumps([{
        "id": "SC-001", "requirement_id": "AC-001", "status": "active", "title": "old",
        "description": "old text", "source_description": "old text",
        "kane_objective": "On https://example.test/ — old text", "last_verified": "2026-01-01",
    }]))
    monkeypatch.setattr(rd, "SCENARIOS_PATH", sc_path)
    monkeypatch.setattr(rd, "RELEASE_DIR", tmp_path / "release_notes")
    monkeypatch.setattr(rd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rd, "current_requirements", lambda: [])
    result = rd.DiffResult(from_release="v1.0.0", to_release="v1.1.0", from_lock_path="", to_lock_path="")
    result.operations.append(rd.Operation(
        op="EDIT", item_text="new text", item_section="Changed", issue=None, sc_id="SC-001",
        requirement_id="AC-001", match_score=0.9, rationale="", sc_ids=["SC-001"]))
    rd.apply(result, None)

    edited = json.loads(sc_path.read_text())
    assert edited[0]["status"] == "updated" and edited[0]["pending_rerun"] is True
    assert edited[0]["kane_objective"].endswith("— new text")

    reqs = [{"id": "AC-001", "description": "new text", "title": "new"}]
    first = agent.sync_scenarios(reqs, edited)
    assert first[0]["status"] == "updated"          # selected by the incremental run
    assert "pending_rerun" not in first[0]
    assert agent.sync_scenarios(reqs, first)[0]["status"] == "active"   # and only once


def test_added_scenario_is_new_then_active():
    added = [{"id": "SC-002", "requirement_id": "AC-002", "status": "new", "pending_rerun": True,
              "title": "t", "kane_objective": "On https://example.test/ — second"}]
    reqs = [{"id": "AC-002", "description": "second", "title": "t"}]
    first = agent.sync_scenarios(reqs, added)
    assert first[0]["status"] == "new"
    assert agent.sync_scenarios(reqs, first)[0]["status"] == "active"
