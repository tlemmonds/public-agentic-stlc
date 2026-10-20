"""
Stage 5 dispatcher — HyperExecute `testRunnerCommand: python ci/run_selected.py "$test"`.

Each line of reports/pytest_selection.txt is one of:
  tests/playwright/test_regression.py::<fn>   vanilla ->the pytest invocation that used to live
                                             inline in hyperexecute.yaml (unchanged flags).
  tests/playwright/native/<sc_id_lower>      testmu export ->`cd` into it and run the
                                             framework-owned run_with_junit.py from its parent.

The child's exit code is propagated verbatim so HyperExecute still gates on it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PYTEST_ARGS = ["-v", "--tb=short", "-s", "--html=reports/report.html", "--junitxml=reports/junit.xml"]


def _run_native(test_dir: Path) -> int:
    runner = test_dir.parent / "run_with_junit.py"
    if not runner.is_file():
        print(f"[run_selected] ERROR: {runner} missing — cannot execute native export {test_dir}",
              file=sys.stderr)
        return 2
    print(f"[run_selected] native -> cd {test_dir} && python {runner}")
    return subprocess.call([sys.executable, str(runner)], cwd=str(test_dir))


def _run_pytest(node: str) -> int:
    env = dict(os.environ)
    # PYTHONPATH=. (repo root) — same as the original inline testRunnerCommand.
    env["PYTHONPATH"] = os.pathsep.join(p for p in (".", env.get("PYTHONPATH", "")) if p)
    (REPO_ROOT / "reports").mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pytest", node, *PYTEST_ARGS]
    print(f"[run_selected] pytest -> {' '.join(cmd[2:])}")
    return subprocess.call(cmd, env=env)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].strip():
        print('usage: python ci/run_selected.py "<selection line>"', file=sys.stderr)
        return 2
    selection = argv[0].strip().strip('"').strip("'")
    candidate = Path(selection)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    if candidate.is_dir():
        return _run_native(candidate.resolve())
    return _run_pytest(selection)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
