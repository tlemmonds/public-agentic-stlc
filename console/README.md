# Release Console

A local, chat-style page for the demo. It starts pipeline runs, streams their
progress and shows the results. It only displays data. Every verdict, score and
status on the page is read from the pipeline's own report files; the console
computes none of them.

```bash
python console/server.py              # http://127.0.0.1:8765, runs enabled
python console/server.py --snapshot   # demo-fallback/reports only, runs disabled, works offline
python console/server.py --port 9000
```

- Standard library only: no new dependencies and no build step. The frontend is
  `console/static/index.html` (plain HTML, CSS and JS).
- The server binds to 127.0.0.1 only.
- It loads `.env` itself if present, never prints it, and redacts every
  `*KEY*`, `*SECRET*`, `*TOKEN*`, `*PASSWORD*` and `LT_USERNAME` value from the
  streamed output, including URL-encoded forms.
- It runs only the fixed steps in `RUN_PLANS` (`console/server.py`) and never
  executes typed text:

  | Button / command | Steps (in order, stop at first non-zero exit) |
  |---|---|
  | Run (DEMO_MODE cached) · `run demo` | `ci/release_diff.py --propose` → `ci/analyze_requirements.py --demo-mode` → `ci/agent.py` → `ci/notify_agent.py` |
  | Run (live) · `run live` | `ci/release_diff.py --propose` → `ci/analyze_requirements.py` → `ci/agent.py` → `ci/notify_agent.py` |

  The **Full run** toggle sets `FULL_RUN=true`. Otherwise the run is incremental.
  DEMO_MODE sets `DEMO_MODE=true`.
- The composer understands fixed commands only (type `help`). No LLM is
  involved. Anything else gets the list of commands back.
- If a run fails, the page shows the failing stage and the last 40 log lines.
  The previously loaded results stay on screen. New results load only after a
  run finishes successfully.

## Sources

| Sidebar entry | Folder |
|---|---|
| Latest run | `reports/` |
| Release vX.Y.Z | `release_notes/vX.Y.Z/reports/` (archived per release) |
| Snapshot | `demo-fallback/reports/` |

When `reports/` has no `release_recommendation.json`, the snapshot is the
default view. Runs stay enabled unless the server was started with `--snapshot`.

## Field mappings

Every number and status on the page, and where it comes from. `rec` =
`release_recommendation.json`, `tm` = `traceability_matrix.json`, `cov` =
`coverage_report.json`, `conf` = `scenario-confidence-report.json`.

### Verdict header and verdict card

| On screen | Field |
|---|---|
| Verdict (GREEN / YELLOW / RED / INVALID, always shown as text) | `rec.verdict` |
| Reason text | `rec.recommendation` |
| CACHED chip | `rec.cached` |
| "HyperExecute skipped" chip and banner | `run_validity.json` → `hyperexecute == "skipped"`, `hyperexecute_reason` |
| CACHED banner | `run_validity.json` → `kane_mode == "cached"` |
| Pass rate | `rec.pass_rate` |
| Passed / executed | `rec.passed` / `rec.executed` |
| Requirements covered | `rec.requirements_covered` / `rec.requirements_total` |
| Uncovered requirements | `rec.requirements_uncovered` |
| Full coverage | `cov.summary.coverage_pct` |
| Kane AI pass rate | `tm.result_analysis.kane_pass_rate` |
| Risk level | `tm.result_analysis.risk_level` |
| Failing scenarios / untested requirements | `rec.failing_scenarios` / `rec.untested_requirements` |
| Key findings | `tm.result_analysis.key_findings` |

### Release delta card

| On screen | Field |
|---|---|
| from → to | `release_delta.json` → `from_release`, `to_release` |
| APPLIED / PROPOSE | `release_delta.json` → `applied` |
| Added / Updated / Deprecated / Unmatched | `release_delta.json` → `summary.ADD` / `summary.EDIT` / `summary.DELETE` / `summary.UNMATCHED` |
| Operations table | `release_delta.json` → `operations[]`: `op`, `requirement_id`, `sc_ids`, `match_score`, `item_text` |
| Selected (run type) | `test_execution_manifest.json` → number of entries in `selected_scenarios`, `run_type` |
| HyperExecute job link | `api_details.json` → `he_summary.job_link` (falls back to `execution_payload.json` → `hyperexecute.dashboard`) |

### Traceability table

| Column | Field |
|---|---|
| Requirement, criterion | `tm.requirements[].requirement_id`, `.acceptance_criterion` |
| BRD ref | `tm.requirements[].brd_ref` |
| Scenarios | `tm.requirements[].scenario_ids` |
| Kane / Playwright / Result | `tm.requirements[].kane_status` / `.playwright_status` / `.overall` |
| Healed | the scenario id appears in `self_healing_report.json` → `patched_scenarios` |
| Confidence | `conf.records[]` matched on `requirement_id` → `confidence_level`, `confidence_score` |
| Filter buttons | the distinct values of `tm.requirements[].overall` |
| "N of M requirements shown" | rows after the filter / all rows in `tm.requirements` |

### Quality gates panel (blue)

| On screen | Field |
|---|---|
| Overall PASS / FAIL | `quality_gates.json` → `gates_passed` |
| Critical failures / warnings | `quality_gates.json` → `critical_failures`, `warnings` |
| Gate, severity, threshold, actual, result, detail | `quality_gates.json` → `gates[]`: `gate`, `severity`, `threshold` + `unit`, `actual` + `unit`, `passed` (shown as PASS / FAIL), `detail` |

### Coverage and confidence card

| On screen | Field |
|---|---|
| Fully covered (%) | `cov.summary.covered_full` (`cov.summary.coverage_pct`) |
| Partially covered / uncovered | `cov.summary.covered_partial` / `cov.summary.uncovered` |
| Negative coverage / HyperExecute coverage | `cov.summary.negative_coverage_pct` / `cov.summary.he_coverage_pct` |
| High-risk requirements | `cov.summary.high_risk_count` |
| Confidence levels | `conf.summary.by_confidence_level` |
| Confidence gate | `conf.summary.quality_signals.confidence_gate_passed` |

### Run card

These come from the run's own console output, not from report files.

| On screen | Source |
|---|---|
| Steps and exit codes | the exit code of each `RUN_PLANS` step |
| Stage timeline | the pipeline's banners: `▶ [Stage N] NAME` (running), `✅ … — complete` (done), `❌ … — FAILED` (failed) |
| Stage durations | the console's wall clock between a stage's start and end banners. Post-pipeline scripts print their banners all at once, so those show about 0s |
| Failed at | the last stage with a ❌ banner, otherwise the step that exited non-zero |
