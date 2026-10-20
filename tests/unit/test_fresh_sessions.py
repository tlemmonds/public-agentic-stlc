"""Fresh /s/<id>/ session per run, and exports with no assertion never pass."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
sys.path.insert(0, str(ROOT / "tests" / "playwright" / "native"))

import project_config  # noqa: E402
import run_with_junit  # noqa: E402

SESSION = re.compile(r"^https://aut\.test/s/[0-9a-f]{32}(/.*)$")


def test_fresh_session_url(monkeypatch):
    monkeypatch.setattr(project_config, "fresh_sessions", lambda: True)
    assert SESSION.match(project_config.fresh_session_url("https://aut.test/")).group(1) == "/tasks"
    assert SESSION.match(project_config.fresh_session_url("https://aut.test/archive")).group(1) == "/archive"
    old = "https://aut.test/s/" + "a" * 32 + "/tasks"
    new = project_config.fresh_session_url(old)
    assert new != old and SESSION.match(new).group(1) == "/tasks"
    assert project_config.fresh_session_url("https://aut.test/") != project_config.fresh_session_url("https://aut.test/")


def test_fresh_session_off_keeps_url(monkeypatch):
    monkeypatch.setattr(project_config, "fresh_sessions", lambda: False)
    assert project_config.fresh_session_url("https://aut.test/") == "https://aut.test/"


def _config(monkeypatch, fresh: str):
    values = {"target_url": "https://aut.test/", "fresh_session_per_run": fresh, "yes_no_compat": "false", "select_option_repair": "false"}
    monkeypatch.setattr(run_with_junit, "_config_value", lambda key: values.get(key, ""))
    monkeypatch.delenv("TARGET_URL", raising=False)


def test_session_copy_repoints_only_target_host(tmp_path, monkeypatch):
    _config(monkeypatch, "true")
    (tmp_path / "test.py").write_text(
        'await page.goto("https://aut.test/")\n'
        'await page.goto("https://aut.test/archive")\n'
        'await page.goto("https://other.test/")\n'
        'global_variables=[{"name": "start_url", "value": "https://aut.test/s/' + 'b' * 32 + '/tasks"}]\n')
    script = run_with_junit._session_copy(tmp_path)
    lines = (tmp_path / script).read_text().splitlines()
    ids = {re.search(r"/s/([0-9a-f]{32})/", l).group(1) for l in lines[:2]}
    assert script == ".session_test.py" and len(ids) == 1          # one session per test
    assert lines[0].endswith('/tasks")') and lines[1].endswith('/archive")')
    assert lines[2] == 'await page.goto("https://other.test/")'
    assert re.search(r"/s/([0-9a-f]{32})/tasks", lines[3]).group(1) in ids    # variable value, same session
    assert "aut.test/\")" in (tmp_path / "test.py").read_text()   # the export is untouched


def test_session_copy_off(tmp_path, monkeypatch):
    _config(monkeypatch, "false")
    (tmp_path / "test.py").write_text('await page.goto("https://aut.test/")\n')
    assert run_with_junit._session_copy(tmp_path) == "test.py"


def test_export_without_assertion_fails(tmp_path, monkeypatch):
    folder = tmp_path / "sc_004"
    folder.mkdir()
    (folder / "test.py").write_text('await page.goto("https://aut.test/")\n')
    monkeypatch.chdir(folder)
    monkeypatch.setattr(run_with_junit, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(run_with_junit, "REPO_ROOT", tmp_path)
    assert run_with_junit.main() == 1
    assert 'failures="1"' in (tmp_path / "reports" / "native_SC-004.xml").read_text()


def test_record_clears_previous_attempt_outputs(tmp_path, monkeypatch):
    import kane_record
    tui = tmp_path / ".testmuai" / "tests"
    tui.mkdir(parents=True)
    for name in ("SC-004_x_test.md", "SC-004_x-2_test.md", "output-SC-004_x", "output-SC-004_x-3",
                 "SC-040_x_test.md"):
        (tui / name).mkdir() if name.startswith("output-") else (tui / name).write_text("old")
    export = tmp_path / "exported" / "sc_004"
    export.mkdir(parents=True)
    (export / "test.py").write_text("stale")
    monkeypatch.setattr(kane_record, "KANE_TUI_TESTS_DIR", tui)
    kane_record._clear_previous_outputs("SC-004_x", export)
    assert sorted(p.name for p in tui.iterdir()) == ["SC-040_x_test.md"]
    assert not export.exists()


def test_record_keeps_test_manager_link(tmp_path, monkeypatch):
    import json
    import kane_record
    tui = tmp_path / ".testmuai" / "tests"
    link = tui / "output-SC-007_x" / ".internal" / "meta.json"
    link.parent.mkdir(parents=True)
    link.write_text(json.dumps({"project_id": "P", "testcase_id": "C"}))
    monkeypatch.setattr(kane_record, "KANE_TUI_TESTS_DIR", tui)
    asset = tmp_path / "tests" / "kane" / "archive" / "sc_007_x_test.md"
    asset.parent.mkdir(parents=True)
    assert kane_record._copy_tms_link("SC-007_x", asset)
    copied = asset.parent / "output-sc_007_x" / ".internal" / "meta.json"
    assert json.loads(copied.read_text())["testcase_id"] == "C"


def test_yes_no_compat_only_touches_boolean_checks():
    import testmu_compat as c
    assert c.normalize("true", "Yes") == "true" and c.normalize("false", "No.") == "false"
    assert c.normalize("true", "No") == "false"            # a real "no" still fails a "true" check
    assert c.normalize("active", "Yes") == "Yes"           # non-boolean checks stay exact...
    assert c.normalize("high", "HIGH") == "high"           # ...apart from letter case
    assert c.normalize("high", "HIGHEST") == "HIGHEST"
    assert c.normalize("1", "Seed row") == "Seed row"
    assert c.normalize("true", "maybe") == "maybe"


def test_session_copy_installs_compat(tmp_path, monkeypatch):
    values = {"target_url": "https://aut.test/", "fresh_session_per_run": "false", "yes_no_compat": "true"}
    monkeypatch.setattr(run_with_junit, "_config_value", lambda key: values.get(key, ""))
    (tmp_path / "test.py").write_text('import testmu\n')
    script = run_with_junit._session_copy(tmp_path)
    assert script == ".session_test.py"
    assert (tmp_path / script).read_text().splitlines()[0].endswith("import testmu_compat  # noqa: E402,F401  (run_with_junit.py)")


def test_select_option_gets_value_from_step_label():
    src = (
        "    async with testmu.step('Select \"high\" in \"Priority\"', instruction_id='x'):\n"
        "        element_2 = page.locator(\"role=combobox[name='Priority']\")\n"
        "        \n"
        "        await element_2.select_option()\n"
        "    async with testmu.step('Click \"Add task\"', instruction_id='y'):\n"
        "        await page.locator('b').select_option()\n"
    )
    out, n = run_with_junit._repair_select_options(src)
    assert n == 1
    assert "await element_2.select_option(label='high')" in out
    assert out.rstrip().endswith("select_option()")      # a non-Select step is untouched


def test_replay_cache_cleared_except_test_manager_link(tmp_path):
    import kane_replay
    asset = tmp_path / "sc_005_x_test.md"
    asset.write_text("x")
    out = tmp_path / "output-sc_005_x"
    (out / ".internal" / "steps" / "1-abc").mkdir(parents=True)
    (out / ".internal" / "meta.json").write_text("{}")
    (out / ".internal" / "execution.json").write_text("{}")
    (out / "Result.md").write_text("old")
    kane_replay._clear_replay_cache(asset)
    assert [p.name for p in out.iterdir()] == [".internal"]
    assert [p.name for p in (out / ".internal").iterdir()] == ["meta.json"]


def test_stage2_keeps_title_and_names_new_from_criterion():
    import agent
    req = {"id": "AC-900", "description": "[BR-1.1] Add a task \"X\", then verify it shows",
           "kane_one_liner": "The check failed because the browser closed"}
    kept = agent.sync_scenarios([req], [{"id": "SC-900", "requirement_id": "AC-900", "title": "Stable name",
                                         "source_description": req["description"]}])
    assert kept[0]["title"] == "Stable name"
    new = agent.sync_scenarios([req], [])
    assert new[0]["title"] == 'Add a task "X"'


def test_stage2_keeps_release_provenance():
    import agent
    req = {"id": "AC-900", "description": "Add a task \"X\""}
    out = agent.sync_scenarios([req], [{"id": "SC-900", "requirement_id": "AC-900", "title": "X",
                                        "status": "new", "pending_rerun": True,
                                        "source_description": req["description"],
                                        "description": req["description"],
                                        "added_in_release": "v9.0.0", "added_at": "2026-01-01"}])
    assert out[0]["status"] == "new"
    assert (out[0]["added_in_release"], out[0]["added_at"]) == ("v9.0.0", "2026-01-01")
    assert out[0]["description"] == req["description"]


def test_stage2_linked_scenario_stays_new_for_one_sync():
    import agent
    req = {"id": "AC-900", "description": "Add a task \"X\""}
    linked = {"id": "SC-901", "requirement_id": "AC-900", "title": "Empty title is rejected",
              "kane_asset": "tests/kane/none/sc_901_test.md", "tags": ["negative"],
              "status": "new", "pending_rerun": True}
    first = agent.sync_scenarios([req], [linked])
    assert [s["id"] for s in first] == ["SC-901"]
    assert first[0]["status"] == "new" and "pending_rerun" not in first[0]
    assert agent.sync_scenarios([req], first)[0]["status"] == "active"


def test_build_name_ignores_unexpanded_placeholder(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "playwright"))
    import conftest as pw_conftest
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "${GITHUB_RUN_NUMBER}")
    assert "#" not in pw_conftest._build_name()
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "42")
    assert pw_conftest._build_name().startswith("Agentic STLC #42 |")


def _replay_command(monkeypatch, asset):
    import kane_replay
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        raise RuntimeError("stop")
    monkeypatch.setattr(kane_replay, "_build_caps", lambda *a, **k: "wss://grid.test")
    monkeypatch.setattr(kane_replay, "_kane_exe", lambda: "kane-cli")
    monkeypatch.setattr(kane_replay.subprocess, "run", fake_run)
    try:
        kane_replay.replay(asset, session_name="s", build_name="b", username="u", access_key="k")
    except Exception:  # noqa: BLE001
        pass
    return seen["cmd"]


def test_replay_passes_recorded_start_url(monkeypatch, tmp_path):
    """Without --variables the browser stays on about:blank; the value is the
    recorded session, never a fresh one."""
    import json
    import project_config
    monkeypatch.setattr(project_config, "start_url_variable", lambda: True)
    sid = "c" * 32
    asset = tmp_path / "sc_900_x_test.md"
    asset.write_text("---\nvariables:\n  global.start_url:\n    value: >-\n"
                     f"      https://aut.test/s/{sid}/tasks\n---\n## Step 1\nOn {{{{start_url}}}} — do it\n")
    cmd = _replay_command(monkeypatch, asset)
    value = json.loads(cmd[cmd.index("--variables") + 1])["start_url"]["value"]
    assert value == f"https://aut.test/s/{sid}/tasks"


def test_replay_without_start_url_passes_no_variables(monkeypatch, tmp_path):
    asset = tmp_path / "sc_901_x_test.md"
    asset.write_text("---\nmax_steps: 5\n---\n## Step 1\nOn https://aut.test/s/" + "d" * 32 + "/tasks — do it\n")
    assert "--variables" not in _replay_command(monkeypatch, asset)


def test_failed_replay_is_retried_once(monkeypatch, tmp_path):
    import kane_dispatch
    from replay_policy import ReplayDecision
    asset = tmp_path / "sc_900_x_test.md"
    asset.write_text("## Step 1\nOn https://aut.test/s/" + "e" * 32 + "/tasks — do it\n")
    monkeypatch.setattr(kane_dispatch, "decide", lambda **k: ReplayDecision(
        sc_id="SC-900", requirement_id="AC-900", decision="replay", asset_path=asset,
        reason="test", description_hash="h", hash_source="description"))
    monkeypatch.setattr(kane_dispatch, "_restore_code_export", lambda *a, **k: None)
    monkeypatch.setattr(kane_dispatch, "_detect_drift", lambda *a, **k: None)
    monkeypatch.setenv("KANE_RETRY_DELAY", "0")
    outcomes = iter([{"status": "error", "summary": "v16-runner exited before ready (SIGBUS)"},
                     {"status": "passed"}])
    calls = []
    monkeypatch.setattr(kane_dispatch.kane_replay, "replay",
                        lambda *a, **k: calls.append(1) or dict(next(outcomes)))
    _, result = kane_dispatch.dispatch_one(
        requirement={"id": "AC-900", "description": "do it"},
        scenario={"id": "SC-900", "requirement_id": "AC-900"},
        username="u", access_key="k", force_re_author=False)
    assert len(calls) == 2
    assert result["status"] == "passed" and result["replay_retries"] == 1


def test_incremental_carries_forward_latest_archived_result(tmp_path):
    import json
    import build_traceability as bt
    for rel, status in (("v1.2.0", "passed"), ("v1.10.0", "failed")):
        d = tmp_path / rel / "reports"
        d.mkdir(parents=True)
        (d / "normalized_results.json").write_text(json.dumps({"results": [
            {"scenario_id": "SC-001", "browser": "chrome", "status": status, "source": "conftest"},
            {"scenario_id": "SC-002", "browser": "chrome", "status": "passed", "source": "carried_forward"},
            {"scenario_id": "SC-003", "browser": "chrome", "status": "data_unavailable", "source": "none"},
        ]}))
    archives = bt._archived_results(tmp_path)
    assert [r for r, _ in archives] == ["v1.10.0", "v1.2.0"]          # numeric, newest first
    release, recs = bt._carried_forward("SC-001", archives)
    assert release == "v1.10.0" and recs[0]["status"] == "failed"
    assert recs[0]["source"] == "carried_forward" and recs[0]["carried_from"] == "v1.10.0"
    assert bt._carried_forward("SC-002", archives) == ("", [])        # never re-carry a carried result
    assert bt._carried_forward("SC-003", archives) == ("", [])        # placeholders are not results


def test_current_release_and_archive(tmp_path, monkeypatch):
    import agent
    monkeypatch.chdir(tmp_path)
    (tmp_path / "release_notes").mkdir()
    for v in ("v1.2.0", "v1.10.0", "v2.0.0"):
        (tmp_path / "release_notes" / f"{v}.lock.json").write_text("{}")
    assert agent._current_release() == "v2.0.0"
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "kane_result_SC-001_chrome.json").write_text("{}")
    (tmp_path / "reports" / "traceability_matrix.md").write_text("# m")
    dest = agent._archive_reports("v2.0.0")
    assert (dest / "traceability_matrix.md").exists()
    assert agent._clear_previous_results() == 1
    assert not (tmp_path / "reports" / "kane_result_SC-001_chrome.json").exists()
