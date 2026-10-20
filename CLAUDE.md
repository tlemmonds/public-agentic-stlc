# Agentic STLC — Agent Context and Setup Playbook

This file is for coding agents (Claude Code, Copilot, Codex, Cursor, …). If the
user asks you to "set this up" for their application, follow the
[Setup playbook](#setup-playbook-new-aut) below, step by step.

## Project overview

**Agentic STLC** is an automated Software Testing Lifecycle pipeline. It turns
plain-English acceptance criteria into executed, traced test results, with no
one writing a test by hand. It chains two checks:

- **Kane CLI** verifies each acceptance criterion in a real browser. The first
  run records a replayable `_test.md` asset; later runs replay it.
- **HyperExecute** runs the Playwright regression suite in parallel on the
  LambdaTest grid.

Every run produces a traceability matrix and a GREEN / YELLOW / RED release verdict.

The repo ships **empty**: no application under test (AUT), no scenarios, no Kane
assets. `target_url` is `https://example.com/` and there is one example
criterion (AC-001) with one example regression template (SC-001). The user
brings their own AUT.

No LLM generates tests. Kane verifies live behaviour; the Playwright regression
bodies are fixed templates that you (the agent) write once per scenario from the
AUT's real DOM.

---

## Setup playbook (new AUT)

Do these in order. Report progress after each step.

### 1. Collect what the user must provide

Ask for anything you don't already have. Don't guess.

| Item | Needed for |
|---|---|
| AUT URL, reachable from the LambdaTest cloud (public, or via LambdaTest Tunnel) | `target_url` |
| The features / user stories to cover, or existing acceptance criteria | `requirements/acceptance_criteria.txt` |
| Whether the AUT needs a login, and test-account credentials (as env vars, never in files) | criteria + templates |
| A LambdaTest account with Kane AI + HyperExecute (`LT_USERNAME`, `LT_ACCESS_KEY`) | Stages 1 and 5 |
| Optional: Test Manager project / folder IDs | `KANE_PROJECT_ID`, `KANE_FOLDER_ID` |
| Optional: their GitHub repo (fork) if they want CI | secrets / variables |

### 2. Prepare the environment

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
node --version                                  # must be v22+
npm install -g @testmuai/kane-cli@0.9.0
```

- **Ask the user before running `kane-cli login`** (interactive, uses their account).
- Credentials go in `.env` (gitignored): `LT_USERNAME=…` and `LT_ACCESS_KEY=…`.
  Ask the user to create it or to paste the values. Never echo, log or commit them.
  Load with `set -a; source ./.env; set +a`.
- Download the HyperExecute CLI into the repo root (gitignored):
  `curl -fsSL -o hyperexecute https://downloads.lambdatest.com/hyperexecute/<darwin|linux>/hyperexecute && chmod +x hyperexecute`
  (Windows: `…/windows/hyperexecute.exe`).

### 3. Point the config at the AUT

In `agentic-stlc.config.yaml`:

- Set `target_url`.
- Replace the example `features:` block with the AUT's feature areas (keywords
  that appear in each criterion's *defining* clause, plus a criticality). Or
  delete the block: every requirement is then GENERAL.
- Leave `fresh_session_per_run: false` unless the AUT scopes data by a
  `/s/<32-hex>/` URL prefix.

Warm the AUT once: `curl -s -o /dev/null -w "%{http_code}\n" <target_url>` (expect 2xx/3xx).

### 4. Write the acceptance criteria

Replace the example criterion in `requirements/acceptance_criteria.txt` (keep the
header format; only lines under `Acceptance Criteria:` count). Explore the AUT
first (open it, read its pages/forms) so the criteria use the AUT's real labels.

Each line is **steps + a check** that Kane can verify in a browser, using the
exact visible labels:

```
[REF-1] Enter "Pay invoice" in the "Title" field and click "Add", then verify the list shows a "Pay invoice" row
```

- One behaviour per line; lines become AC-001, AC-002, … in file order.
- Make each criterion **independent of leftover data** (create what it checks;
  check "a row X exists", not "the list has exactly N rows") — Kane replays reuse
  the recorded start URL (docs/kane-cli-0.9.0-issues.md, issue 3).
- Include login steps if the AUT needs them; reference credentials by env var.
- Show the criteria to the user and get approval before recording. Once
  recorded, **ask before changing any criterion's text** (it forces a re-record).

### 5. Create the scenarios

```bash
python ci/analyze_requirements.py --skip-kane   # parse criteria, no Kane calls
python ci/manage_scenarios.py                   # one SC-xxx per new criterion
```

### 6. Record the Kane assets (Stage 1)

```bash
python ci/analyze_requirements.py
```

This records `tests/kane/<feature>/sc_0xx_<title>_test.md` per scenario. Check
`reports/kane_results.json`. For a failed criterion, read the Kane output: fix
the criterion's wording (with the user's approval) if Kane misunderstood it; if
the AUT really misbehaves, report it — never "fix" the test to hide an AUT bug.

### 7. Write the regression templates

For each SC-xxx in `scenarios/scenarios.json`, add a body to `BODIES` in
`ci/playwright_bodies.py` (delete the example SC-001 body if AC-001 changed):

- Open the AUT and use its real selectors (roles, labels, stable classes/ids).
- Use Playwright `expect(...)` for every check. Wrap clicks that submit a form or
  navigate in `with page.expect_navigation():`.
- Shared steps (login, "add item", row lookup) go in `HELPERS`.
- Bodies are `str.format`-ed with `{url}` only, so use no other braces, and
  indent four spaces.
- Each test must create its own data and not depend on another test (they run in
  parallel on 5 VMs, in Chrome and Firefox).

Optionally sanity-run the generated file locally before using the grid.

### 8. Run Stages 2–7

```bash
FULL_RUN=true python ci/agent.py
```

Read `reports/traceability_matrix.md` and `reports/release_recommendation.md`.
Fix template selector/timing failures and rerun. A run without `./hyperexecute`
or credentials is marked **INVALID** (exit code 3).

### 9. Commit

Commit on the user's branch (ask which), with `[skip ci]` unless a CI run is
intended:

- `agentic-stlc.config.yaml`, `requirements/acceptance_criteria.txt`,
  `scenarios/scenarios.json`, `ci/playwright_bodies.py`;
- `tests/kane/**` assets **including** each `output-<stem>/.internal/meta.json`
  (it links the asset to its Test Manager case, so replays reuse the case).

Never commit `.env`, `reports/`, `./hyperexecute` or
`tests/playwright/test_regression.py` changes made by hand. Before every commit,
scan the staged diff for the `$LT_ACCESS_KEY` value (raw and URL-encoded).

### 10. CI (optional)

On the user's GitHub repo: enable Actions (forks start disabled), add secrets
`LT_USERNAME` / `LT_ACCESS_KEY` (`gh secret set`), optionally variables
`KANE_PROJECT_ID` / `KANE_FOLDER_ID` (`gh variable set`), then
`gh workflow run agentic-stlc.yml -f full_run=true`. Ask before setting secrets.

---

## Tech stack

| Layer | Technology |
|---|---|
| Functional testing | Kane CLI `@testmuai/kane-cli@0.9.0` (pinned) on the LambdaTest Playwright grid |
| Regression testing | Playwright (Python) + pytest |
| Cloud execution | HyperExecute (5 Linux VMs, Chrome + Firefox) |
| Test management | LambdaTest Test Manager (optional project / folder) |
| Orchestration | Python 3.11+ |
| CI/CD | GitHub Actions |
| Reporting | JUnit XML, pytest-html, JSON/Markdown traceability |

## Directory structure

```
├── agentic-stlc.config.yaml     # target_url, requirements paths, kaneai.*, features, regression.source
├── hyperexecute.yaml            # HE config: concurrency 5, Linux, maxRetries 1
├── .github/
│   ├── workflows/agentic-stlc.yml   # analyze → orchestrate → Pipeline Summary
│   └── actions/{analyze,orchestrate}/action.yml
├── ci/
│   ├── release_diff.py          # Stage 0: release notes → ADD/EDIT/DELETE vs the last lock
│   ├── analyze_requirements.py  # Stage 1 entry point
│   ├── kane_dispatch.py         # Stage 1: replay-first dispatch, retry, parallel workers
│   ├── replay_policy.py         # replay / record / rerecord decision; asset paths
│   ├── kane_record.py           # kane-cli run (records a _test.md asset)
│   ├── kane_replay.py           # kane-cli testmd run (replays an asset)
│   ├── manage_scenarios.py      # Stage 2 (standalone)
│   ├── agent.py                 # Stages 2–7 orchestrator
│   ├── playwright_bodies.py     # Stage 3: HELPERS + BODIES (one template per SC-xxx) — YOU WRITE THESE
│   ├── ingest_kane_pack.py      # import an existing Kane test pack instead of recording
│   ├── project_config.py        # config access
│   ├── run_selected.py          # per-test runner on each HE VM
│   ├── build_traceability.py / release_recommendation.py / write_github_summary.py
│   └── …                        # metrics, RCA, failure intelligence, quality gates
├── requirements/
│   ├── acceptance_criteria.txt  # INPUT: acceptance criteria — YOU WRITE THESE
│   └── analyzed_requirements.json   # Stage 1 output (generated)
├── scenarios/scenarios.json     # Scenario pool (SC-xxx); never delete entries
├── release_notes/               # optional: vX.Y.Z.md, vX.Y.Z.lock.json, vX.Y.Z/reports/ (baselines)
├── tests/
│   ├── kane/<feature>/          # Kane assets (recorded): sc_0xx_<title>_test.md, .meta.json,
│   │                            #   output-<stem>/.internal/meta.json (Test Manager link — keep)
│   ├── playwright/
│   │   ├── conftest.py          # grid fixture, browsers, build name
│   │   ├── test_regression.py   # GENERATED — never edit
│   │   └── native/              # runner + compat shims for Kane code exports
│   └── unit/                    # pytest unit tests for the pipeline
├── docs/kane-cli-0.9.0-issues.md    # Kane bugs found, with workarounds
└── reports/                     # Run output (gitignored)
```

## Pipeline stages

### Stage 0: Release diff (optional)
`ci/release_diff.py --propose` (read-only) or `--apply` compares
`release_notes/vX.Y.Z.md` with the last lock and marks scenarios new, updated or
deprecated. Start with `release_notes/v1.0.0.md` (see `release_notes/README.md`).

### Stage 1: Kane verification
- `ci/analyze_requirements.py` → `kane_dispatch.dispatch_all`. The work list is
  the scenarios: a criterion with no SC-xxx does no Kane work (create scenarios first).
- **Replay-first:** each scenario's asset is replayed. It is recorded only when
  it's new, its criterion changed, or `FORCE_RE_AUTHOR=true`.
- **Parallel:** `KANE_MAX_WORKERS` replays at once (default 5). The first wave
  starts `KANE_LAUNCH_STAGGER` seconds apart (default 4): launching 5 kane-cli
  runners in the same second can crash them with SIGBUS.
- **Retry:** a replay with status `failed` or `error` runs once more
  (`kaneai.replay_retries`), after `KANE_RETRY_DELAY` seconds.
- Output: `requirements/analyzed_requirements.json`, `reports/kane_results.json`,
  `reports/replay_decisions.json`.

### Stage 2: Scenario sync
New criterion → new SC-xxx; changed → `updated`; removed → `deprecated` (never
deleted). Scenario titles are stable; asset filenames derive from titles.

### Stage 3: Test generation
With `regression.source: templates`, writes `tests/playwright/test_regression.py`
from `ci/playwright_bodies.py`. A scenario without a body generates
`pytest.fail("No regression template…")`. (`regression.source: kane_exports`
runs Kane's code exports instead; those have SDK bugs, see the issues doc.)

### Stage 4: Selection
`FULL_RUN=true` → every non-deprecated scenario. Otherwise only `new` and `updated`.

### Stage 5: HyperExecute
Submits `hyperexecute.yaml`: 5 Linux VMs, autosplit, `maxRetries: 1`,
`BROWSERS=chrome,firefox`.

### Stage 6: Results
HE REST API (`/v2.0/job/{id}/sessions`), then artifacts.

### Stage 7: Traceability + verdict
A requirement passes only if **both** Kane and Playwright pass. **GREEN** ≥ 90%
with full coverage, **YELLOW** ≥ 75%, **RED** < 75%, **INVALID** when the run
can't be trusted. Cached results are labelled `[CACHED]`. In incremental runs, a
scenario that wasn't selected carries its latest result forward from an archived
release (`release_notes/vX.Y.Z/reports/`).

## Key architectural rules

1. **Scenario IDs are immutable.** Never renumber or delete. Deprecate instead.
2. **`tests/playwright/test_regression.py` is generated.** Never edit it. Change `ci/playwright_bodies.py`.
3. **No LLM for test generation at runtime.** Regression bodies are fixed templates; Kane verifies, it doesn't generate.
4. **Incremental by default.** `FULL_RUN=true` runs everything.
5. **Both verifications required** for a requirement to pass.
6. **History is never lost.** Deprecated scenarios stay in `scenarios.json`.
7. **Kane assets are replay-first and committed**, with `output-<stem>/.internal/meta.json`.
8. **No new architecture** (orchestrators, gates, approval layers) without asking.

## Working rules

- **Never commit secrets.** Credentials come from `LT_USERNAME` / `LT_ACCESS_KEY`
  in the gitignored `.env` locally and GitHub Actions secrets in CI. Never print them.
- **Ask before** a credential or login step, before setting GitHub secrets, and
  before changing recorded requirement text.
- **Never modify the AUT's source code.** Report AUT bugs; don't work around them in tests.
- Don't commit `reports/` output; archive a reviewed baseline under `release_notes/vX.Y.Z/reports/`.
- **DEMO_MODE caches must come from real runs.** Never fabricate cached results.
- On Windows, prefix Python with `PYTHONIOENCODING=utf-8 PYTHONUTF8=1`. On macOS,
  if Python reports `CERTIFICATE_VERIFY_FAILED`, set `SSL_CERT_FILE` to certifi's bundle.

## Environment variables

| Variable | Where set | Purpose |
|---|---|---|
| `LT_USERNAME`, `LT_ACCESS_KEY` | `.env` / GitHub secrets | LambdaTest credentials |
| `KANE_PROJECT_ID`, `KANE_FOLDER_ID` | env / GitHub **variables** | Test Manager project / folder (optional) |
| `TARGET_URL` | env / dispatch input | AUT URL (default `target_url` in config) |
| `FULL_RUN` | dispatch input | `true` runs all non-deprecated scenarios |
| `DEMO_MODE` | dispatch input | use cached Kane results from a real run |
| `FORCE_RE_AUTHOR` | dispatch input `regenerate_tests` | re-record every Kane asset |
| `KANE_MAX_WORKERS` | env | parallel Kane replays (default 5) |
| `KANE_LAUNCH_STAGGER`, `KANE_RETRY_DELAY` | env | seconds between first-wave launches (4) / before a retry (10) |
| `KANE_REPLAY_TIMEOUT`, `KANE_RECORD_TIMEOUT` | env | per-run timeouts (180 / 300 s) |
| `BROWSERS` | `hyperexecute.yaml` | `chrome,firefox` |

## Adding a requirement later

1. Add the criterion to `requirements/acceptance_criteria.txt` (and the release notes, if used).
2. `python ci/analyze_requirements.py --skip-kane && python ci/manage_scenarios.py`
3. `python ci/analyze_requirements.py` (records the new asset).
4. Add a body for the new SC-xxx to `BODIES` in `ci/playwright_bodies.py`.
5. `python ci/agent.py`, then commit the asset including `output-<stem>/.internal/meta.json`.

## GitHub Actions

`.github/workflows/agentic-stlc.yml` — jobs `Stage 1 · KaneAI Verification` →
`Stages 2–7 · Orchestrator` → `Pipeline Summary`. Triggers: pushes to
`requirements/`, `scenarios/`, `tests/`, `ci/`, `hyperexecute.yaml`, the config
or the workflow (unless the commit says `[skip ci]`); pull requests; and
`workflow_dispatch` (`full_run`, `demo_mode`, `target_url`, `regenerate_tests`,
`apply_release_delta`). Kane results are cached on `requirements/*.txt`,
`tests/kane/**/*_test.md`, `ci/*.py` and the config; rerunning unchanged code
reuses the last result, labelled `[CACHED]` (`gh cache delete` forces a live run).

## Known Kane CLI issues

See `docs/kane-cli-0.9.0-issues.md`. The ones that matter most when setting up:

- Code exports compare yes/no vision answers to `true`/`false`, export
  `select_option()` without a value, and don't scope row locators — hence
  `regression.source: templates`.
- Replays overwrite the Test Manager case title: identify cases by their tags (`SC-xxx`, `AC-xxx`).
- `{{global.start_url}}` is one shared Test Manager global; `--variables` on a
  replay doesn't change navigation but overwrites the global.
