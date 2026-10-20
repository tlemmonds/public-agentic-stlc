"""Unit tests for ci/ingest_kane_pack.py against a synthetic Kane pack."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ci"))

import ingest_kane_pack as ikp  # noqa: E402

yaml = pytest.importorskip("yaml")

ASSET = """---
mode: testing
max_steps: 40
timeout: 300
variables: {}
tags: [tasks, task-create, positive]
---

# {title}

First paragraph line one
continues on line two.

## Step 1
Do the thing.
"""


def _mk_case(root: Path, folder: str, group: str, stem: str, title: str, *, tc_id: str,
             export: bool = False, meta_nested: bool = False, meta: bool = True) -> None:
    d = root / folder / group
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_test.md").write_text(ASSET.replace("{title}", title), encoding="utf-8")
    out = d / f"output-{stem}"
    (out / ".internal").mkdir(parents=True)
    (out / "Result.md").write_text("ok", encoding="utf-8")
    if meta:
        payload = {"testcase_id": tc_id, "folder_id": "F1", "project_id": "P1", "session_name": stem,
                   "executions": [{"status": "passed", "at": "2026-07-01T10:00:00.000Z"},
                                  {"status": "passed", "at": "2026-07-23T22:34:33.881Z"}]}
        target = (out / ".internal" / "meta.json") if meta_nested else (out / "meta.json")
        target.write_text(json.dumps(payload), encoding="utf-8")
    if export:
        (out / "playwright-python-code").mkdir()
        (out / "playwright-python-code" / "test.py").write_text("print('hi')\n", encoding="utf-8")


@pytest.fixture
def pack(tmp_path: Path) -> dict:
    src = tmp_path / "src"
    _mk_case(src, "Tasks", "Tasks-Create-Validate", "create-task", "Create a task", tc_id="TC-A", export=True)
    _mk_case(src, "Tasks", "Tasks-List", "list-tasks", "List tasks", tc_id="TC-B", meta_nested=True)
    _mk_case(src, "Auth", "Auth-Ok", "sign-in", "Sign in", tc_id="TC-C")
    _mk_case(src, "Auth", "Auth-Nometa", "orphan", "Orphan", tc_id="TC-D", meta=False)
    _mk_case(src, "Archive", "Orig-Pairwise", "pairwise-row-01", "Pairwise", tc_id="TC-E")
    _mk_case(src, "Archive", "Orig-Other", "unmapped-case", "Unmapped", tc_id="TC-F")
    mapping = {
        "target_url": "https://example.test/",
        "folders": {"Tasks": "TASKS", "Auth": "AUTH", "Archive": "ARCHIVE"},
        "cases": {
            "Tasks/**": {"requirement": "AC-002", "brd_ref": "AC-02"},
            "Tasks/Tasks-Create-Validate/**": {"requirement": "AC-003", "brd_ref": "AC-03"},
            "Auth/**": {"requirement": "AC-005", "brd_ref": "AC-05"},
        },
        "exclude": ["Archive/Orig-Pairwise/**"],
    }
    map_path = tmp_path / "map.yaml"
    map_path.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    reqs = tmp_path / "requirements"
    reqs.mkdir()
    (reqs / "brd.txt").write_text(
        "Title: X\n\nAcceptance Criteria:\n[AC-01] one\n[AC-02] two\n[AC-03] three\n[AC-04] four\n[AC-05] five\n",
        encoding="utf-8")
    return {"src": src, "map": map_path, "scen": tmp_path / "scenarios.json", "dest": tmp_path / "tests" / "kane",
            "reports": tmp_path / "reports", "reqs": reqs}


def _run(p: dict, *extra: str) -> int:
    return ikp.main(["--source", str(p["src"]), "--map", str(p["map"]), "--scenarios", str(p["scen"]),
                     "--dest", str(p["dest"]), "--reports-dir", str(p["reports"]),
                     "--requirements-dir", str(p["reqs"]), *extra])


def test_parse_asset_title_paragraph_tags():
    parsed = ikp.parse_asset(ASSET.replace("{title}", "My Title"))
    assert parsed["title"] == "My Title"
    assert parsed["first_paragraph"] == "First paragraph line one continues on line two."
    assert parsed["tags"] == ["tasks", "task-create", "positive"]
    assert ikp.build_description("T", "p") == "T — p"


def test_longest_glob_wins():
    cases = {"Tasks/**": {"requirement": "AC-002"}, "Tasks/Svc-X/**": {"requirement": "AC-003"}}
    assert ikp.resolve_case_mapping("Tasks/Svc-X/a_test.md", cases)[1]["requirement"] == "AC-003"
    assert ikp.resolve_case_mapping("Tasks/Svc-Y/a_test.md", cases)[1]["requirement"] == "AC-002"
    assert ikp.resolve_case_mapping("Auth/a_test.md", cases) == (None, None)


def test_requirements_txt_parsing(tmp_path: Path):
    (tmp_path / "r.txt").write_text("Acceptance Criteria:\n[AC-01] one\ntwo\n\nOther:\n", encoding="utf-8")
    reqs = ikp.load_requirement_universe(tmp_path)
    assert [(r["id"], r["brd_ref"], r["description"]) for r in reqs] == [
        ("AC-001", "AC-01", "one"), ("AC-002", None, "two")]


def test_propose_writes_reports_only(pack: dict, capsys):
    assert _run(pack) == 0
    out = capsys.readouterr().out
    assert "no TMS sidecar" in out and "Auth/Auth-Nometa/orphan_test.md" in out
    assert "UNMAPPED" in out and "Archive/Orig-Other/unmapped-case_test.md" in out
    assert "Archive/Orig-Pairwise/pairwise-row-01_test.md" in out  # excluded
    assert "**UNCOVERED REQUIREMENTS** (2)" in out and "AC-001" in out and "AC-004" in out
    assert not pack["scen"].exists() and not pack["dest"].exists()
    report = json.loads((pack["reports"] / "ingest_report.json").read_text(encoding="utf-8"))
    assert [s["id"] for s in report["scenarios"]] == ["SC-001", "SC-002", "SC-003"]
    assert (pack["reports"] / "ingest_report.md").exists()


def test_apply_is_idempotent_and_verbatim(pack: dict):
    assert _run(pack, "--apply") == 0
    recs = json.loads(pack["scen"].read_text(encoding="utf-8"))
    assert [r["id"] for r in recs] == ["SC-001", "SC-002", "SC-003"]
    by_stem = {Path(r["kane_asset"]).name: r for r in recs}
    accept = by_stem["create-task_test.md"]
    assert accept["requirement_id"] == "AC-003" and accept["brd_ref"] == "AC-03"
    assert accept["export_kind"] == "testmu" and accept["playwright_export"].endswith("output-create-task/playwright-python-code")
    assert accept["feature"] == "TASKS" and accept["status"] == "new" and accept["source"] == "ingested"
    assert accept["kane_testcase_id"] == "TC-A" and accept["last_verified"] == "2026-07-23"
    assert accept["kane_url"] == "https://example.test/" and accept["function_name"] is None
    assert accept["description"] == "Create a task — First paragraph line one continues on line two."
    dash = by_stem["list-tasks_test.md"]
    assert dash["export_kind"] == "vanilla" and dash["playwright_export"] is None and dash["kane_testcase_id"] == "TC-B"

    feat = pack["dest"] / "tasks"
    asset = feat / "create-task_test.md"
    assert asset.read_bytes() == (pack["src"] / "Tasks/Tasks-Create-Validate/create-task_test.md").read_bytes()
    assert (feat / "output-create-task" / "Result.md").exists()
    assert (feat / "output-create-task" / ".internal").is_dir()
    assert (feat / "output-create-task" / "meta.json").exists()
    side = json.loads((feat / "create-task_test.meta.json").read_text(encoding="utf-8"))
    assert side["sc_id"] == accept["id"] and side["hash_source"] == "asset"
    assert side["description_hash"] == ikp.hash_asset_body(asset.read_text(encoding="utf-8"))
    assert side["ingested_from"] == "Tasks/Tasks-Create-Validate/create-task_test.md"
    assert side["kane_testcase_id"] == "TC-A"

    # second apply: nothing added, existing untouched
    assert _run(pack, "--apply") == 0
    recs2 = json.loads(pack["scen"].read_text(encoding="utf-8"))
    assert recs2 == recs


def test_unmapped_folder_errors(pack: dict):
    mapping = yaml.safe_load(pack["map"].read_text(encoding="utf-8"))
    del mapping["folders"]["Auth"]
    pack["map"].write_text(yaml.safe_dump(mapping), encoding="utf-8")
    with pytest.raises(SystemExit):
        _run(pack)
