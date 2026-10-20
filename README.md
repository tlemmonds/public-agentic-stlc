# Agentic STLC

An automated Software Testing Lifecycle pipeline. It turns plain-English
acceptance criteria into executed, traced test results against **your own web
application**, with no one writing a test by hand.

The pipeline runs two kinds of checks:

- **Kane CLI** checks each acceptance criterion in a real browser. It records
  a replayable `_test.md` asset on the first run and replays it afterwards.
- **HyperExecute** runs a Playwright regression suite in parallel on
  LambdaTest (5 VMs, Chrome and Firefox). Its test bodies are fixed templates
  in `ci/playwright_bodies.py` that check the page with real selectors, so no
  vision AI decides pass or fail in the regression layer.

It combines both results into a traceability matrix and a GREEN / YELLOW / RED
release verdict. Test generation is deterministic: no LLM writes tests.

The repo ships **empty**: no application under test, no scenarios, no recorded
Kane assets. A single example criterion against `https://example.com/` shows
the format. You bring your own AUT.

## Set it up with an agent

This repo is written so a coding agent can set it up for you. Open it in
Claude Code (or Copilot / Codex / Cursor; see [AGENTS.md](AGENTS.md)) and say:

> Set up this pipeline for my application at `<your URL>`. Follow the setup
> playbook in CLAUDE.md.

The agent follows [CLAUDE.md](CLAUDE.md#setup-playbook-new-aut). It asks you
for the items below, writes your acceptance criteria and regression templates,
records the Kane assets and runs the pipeline. It asks before any credential
or login step and never commits secrets.

## What you must provide

| # | Item | Where it goes |
|---|---|---|
| 1 | **Your application under test**: a URL reachable from the LambdaTest cloud (public, or via LambdaTest Tunnel) | `target_url` in `agentic-stlc.config.yaml` (or the `TARGET_URL` env var / workflow input) |
| 2 | **Acceptance criteria**: one line per criterion, written as steps plus a check | `requirements/acceptance_criteria.txt` (replace the example) |
| 3 | **A regression template per scenario**: Playwright body with your AUT's selectors | `BODIES` in `ci/playwright_bodies.py` (replace the example) |
| 4 | **A LambdaTest account** with Kane AI and HyperExecute access | `LT_USERNAME`, `LT_ACCESS_KEY` in a local `.env` (gitignored) and as GitHub secrets |
| 5 | **Kane CLI login** on your machine | `kane-cli login` (interactive) |
| 6 | **The HyperExecute CLI binary** in the repo root | `./hyperexecute` (gitignored; download below) |
| 7 | *Optional:* Test Manager project and folder for Kane cases | `KANE_PROJECT_ID`, `KANE_FOLDER_ID` env vars / GitHub repo **variables** |
| 8 | *Optional:* a feature taxonomy for coverage and criticality | `features:` in `agentic-stlc.config.yaml` (replace the example) |
| 9 | *Optional:* release notes, to drive ADD / EDIT / DELETE between releases | `release_notes/vX.Y.Z.md` (see [release_notes/README.md](release_notes/README.md)) |

If your AUT needs a login, put the sign-in steps in each criterion (or a
shared helper in `ci/playwright_bodies.py`) and keep test credentials in env
vars, never in committed files.

## How it flows

```
release_notes/vX.Y.Z.md ──► Stage 0  release_diff.py      ADD / EDIT / DELETE vs the last lock
requirements/*.txt ───────► Stage 1  analyze_requirements  Kane CLI per criterion (replay-first)
                            Stage 2  scenario sync         scenarios/scenarios.json (SC-xxx, immutable)
                            Stage 3  test generation       tests/playwright/test_regression.py (from ci/playwright_bodies.py)
                            Stage 4  selection             incremental by default, FULL_RUN=true for all
                            Stage 5  HyperExecute          parallel Playwright on LambdaTest
                            Stage 6+ traceability, verdict reports/*.md, GitHub Step Summary
```

A requirement passes only when **both** Kane and Playwright pass.

## Rules

- Scenario IDs are immutable. A removed requirement marks its scenarios
  `deprecated`; entries are never deleted.
- `tests/playwright/test_regression.py` is generated. Never edit it by hand.
- Kane assets are replay-first. An asset is re-recorded only when its
  requirement changes or `FORCE_RE_AUTHOR=true` is set.
- Runs are incremental by default: only `new` and `updated` scenarios run
  unless `FULL_RUN=true`.

## Manual setup

Prerequisites: Python 3.11+, Node.js 22+, and a LambdaTest account.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install -g @testmuai/kane-cli@0.9.0 && kane-cli login

# HyperExecute CLI (gitignored)
curl -fsSL -o hyperexecute https://downloads.lambdatest.com/hyperexecute/darwin/hyperexecute && chmod +x hyperexecute   # macOS
curl -fsSL -o hyperexecute https://downloads.lambdatest.com/hyperexecute/linux/hyperexecute  && chmod +x hyperexecute   # Linux
# Windows: https://downloads.lambdatest.com/hyperexecute/windows/hyperexecute.exe

# Credentials: keep them in .env (gitignored), never in committed files
printf 'LT_USERNAME=%s\nLT_ACCESS_KEY=%s\n' '<username>' '<access key>' > .env
set -a; source ./.env; set +a
```

Then:

1. Set `target_url` in `agentic-stlc.config.yaml` to your AUT.
2. Replace the example in `requirements/acceptance_criteria.txt` with your criteria.
3. Create the scenarios (one `SC-xxx` per criterion, no Kane calls yet):
   ```bash
   python ci/analyze_requirements.py --skip-kane
   python ci/manage_scenarios.py
   ```
4. Run Stage 1 for real. It records a Kane asset per scenario:
   `python ci/analyze_requirements.py`
5. Add a body to `BODIES` in `ci/playwright_bodies.py` for each `SC-xxx`
   in `scenarios/scenarios.json`.
6. Run the rest: `FULL_RUN=true python ci/agent.py`.
7. Commit `requirements/`, `scenarios/`, `ci/playwright_bodies.py` and the new
   assets under `tests/kane/`, **including** each
   `output-<stem>/.internal/meta.json` (it links the asset to its Test Manager case).

On Windows, prefix every Python command with `PYTHONIOENCODING=utf-8 PYTHONUTF8=1`.
On macOS, if Python reports `CERTIFICATE_VERIFY_FAILED`, set `SSL_CERT_FILE` to
certifi's bundle.

Other commands:

```bash
python ci/release_diff.py --propose        # Stage 0 preview (changes nothing)
python ci/release_diff.py --apply          # Stage 0 apply (scenarios.json + vX.Y.Z.lock.json)
python ci/agent.py                         # incremental (new + updated scenarios)
python -m pytest tests/unit -q             # pipeline unit tests
```

Main outputs: `reports/traceability_matrix.md`, `reports/release_recommendation.md`
and `reports/release_delta.md`. If `./hyperexecute` or the credentials are
missing, the run still writes reports but is marked **INVALID** (exit code 3).

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `LT_USERNAME` | yes | LambdaTest username (GitHub secret in CI) |
| `LT_ACCESS_KEY` | yes | LambdaTest access key (GitHub secret in CI) |
| `TARGET_URL` | no | AUT URL. The default is `target_url` in `agentic-stlc.config.yaml` |
| `FULL_RUN` | no | `true` runs every non-deprecated scenario |
| `DEMO_MODE` | no | `true` uses cached Kane results (`ci/demo_kane_results.json`, from a real run) |
| `FORCE_RE_AUTHOR` | no | `true` re-records every Kane asset |
| `KANE_PROJECT_ID` / `KANE_FOLDER_ID` | no | Test Manager project and folder (GitHub repo variables in CI) |
| `KANE_MAX_WORKERS` | no | Parallel Kane replays (default 5) |
| `KANE_REPLAY_TIMEOUT` / `KANE_RECORD_TIMEOUT` | no | Per-run timeouts in seconds (180 / 300) |
| `BROWSERS` | no | Comma-separated browser list (`hyperexecute.yaml` sets `chrome,firefox`) |

## CI (GitHub Actions)

`.github/workflows/agentic-stlc.yml` runs on pushes to `requirements/`,
`scenarios/`, `tests/` or `ci/`, on pull requests, and on **workflow_dispatch**
(inputs `full_run`, `demo_mode`, `target_url`, `regenerate_tests`,
`apply_release_delta`). Job 1 runs the Kane verification, job 2 the
orchestrator, which writes the verdict to the Step Summary.

On your fork:

1. Enable Actions (forks start with them disabled).
2. Add secrets `LT_USERNAME` and `LT_ACCESS_KEY`.
3. Optionally add variables `KANE_PROJECT_ID` and `KANE_FOLDER_ID`.
4. Start a run: `gh workflow run agentic-stlc.yml -f full_run=true`.

## Known Kane CLI issues

See [docs/kane-cli-0.9.0-issues.md](docs/kane-cli-0.9.0-issues.md) for the
bugs found in Kane CLI 0.9.0 and the workarounds this repo uses.

## License

MIT. See [LICENSE](LICENSE).
