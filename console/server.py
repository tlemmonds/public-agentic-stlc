"""Release Console: a local page that starts pipeline runs and shows their results.

    python console/server.py               # http://127.0.0.1:8765
    python console/server.py --snapshot    # read demo-fallback/ only (works offline)
    python console/server.py --port 9000

Display only. Every value on the page comes from the pipeline's own files:
reports/*.json, release_notes/<version>/reports/*.json or demo-fallback/reports/*.json.
The server never computes a verdict, score or status. It runs only the
whitelisted pipeline steps in RUN_PLANS, never shell input. It binds to 127.0.0.1
and redacts secret values from everything it streams.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
REPORTS = REPO / "reports"
RELEASES = REPO / "release_notes"
SNAPSHOT = REPO / "demo-fallback" / "reports"

# Files the page reads, by key. Each is passed through verbatim (never recomputed).
ARTIFACTS = {
    "recommendation": "release_recommendation.json",
    "traceability":   "traceability_matrix.json",
    "confidence":     "scenario-confidence-report.json",
    "coverage":       "coverage_report.json",
    "delta":          "release_delta.json",
    "payload":        "execution_payload.json",
    "gates":          "quality_gates.json",
    "validity":       "run_validity.json",
    "manifest":       "test_execution_manifest.json",
    "api":            "api_details.json",
    "healing":        "self_healing_report.json",
}

# The only commands the console can start. Each step is a fixed argv.
PY = sys.executable
RUN_PLANS = {
    "demo": [
        ("Stage 0 · release delta (preview)", [PY, "ci/release_diff.py", "--propose"]),
        ("Stage 1 · Kane (CACHED)",           [PY, "ci/analyze_requirements.py", "--demo-mode"]),
        ("Stages 2–9 · orchestrator",         [PY, "ci/agent.py"]),
        ("Execution payload",                 [PY, "ci/notify_agent.py"]),
    ],
    "live": [
        ("Stage 0 · release delta (preview)", [PY, "ci/release_diff.py", "--propose"]),
        ("Stage 1 · Kane (live)",             [PY, "ci/analyze_requirements.py"]),
        ("Stages 2–9 · orchestrator",         [PY, "ci/agent.py"]),
        ("Execution payload",                 [PY, "ci/notify_agent.py"]),
    ],
}

STAGE_START = re.compile(r"▶\s+\[Stage ([^\]]+)\]\s+(.+?)\s*$")
STAGE_END = re.compile(r"(✅|❌)\s+\[Stage ([^\]]+)\]\s+(.+?)\s+—\s+(complete|FAILED)")
SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.I)


# ── Secrets ──────────────────────────────────────────────────────────────────

def load_dotenv() -> None:
    """Load REPO/.env into this process (never printed). Existing env wins."""
    path = REPO / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.replace("export ", "").strip()
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def _secret_values() -> list[str]:
    vals = []
    for k, v in os.environ.items():
        if v and len(v) >= 6 and (SECRET_NAME.search(k) or k == "LT_USERNAME"):
            vals += [v, urllib.parse.quote(v, safe="")]
    return sorted(set(vals), key=len, reverse=True)


def redact(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "•••")
    return re.sub(r"(accessKey%22%3A%20%22|accessKey\"\s*:\s*\")[^%\"]+", r"\1•••", text)


# ── Artifacts ────────────────────────────────────────────────────────────────

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _has_reports(d: Path) -> bool:
    return d.is_dir() and (d / ARTIFACTS["recommendation"]).is_file()


def sources() -> list[dict]:
    out = []
    if _has_reports(REPORTS):
        out.append({"id": "current", "label": "Latest run (reports/)"})
    for d in sorted(RELEASES.glob("v*/reports"), key=lambda p: _vkey(p.parent.name), reverse=True):
        if _has_reports(d):
            out.append({"id": d.parent.name, "label": f"Release {d.parent.name}"})
    if _has_reports(SNAPSHOT):
        out.append({"id": "snapshot", "label": "Snapshot (demo-fallback/)"})
    return out


def _vkey(name: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)


def source_dir(source: str) -> Path | None:
    if source == "current":
        return REPORTS
    if source == "snapshot":
        return SNAPSHOT
    if re.fullmatch(r"v\d+(\.\d+){0,3}", source or ""):
        return RELEASES / source / "reports"
    return None


def state(source: str) -> dict:
    d = source_dir(source)
    if d is None or not d.is_dir():
        return {"source": source, "error": "unknown or empty source"}
    data, files = {}, {}
    for key, name in ARTIFACTS.items():
        p = d / name
        data[key] = _read_json(p)
        files[key] = {"path": str(p.relative_to(REPO)), "present": p.is_file(),
                      "mtime": p.stat().st_mtime if p.is_file() else None}
    return {"source": source, "dir": str(d.relative_to(REPO)), "files": files, "data": data}


# ── Runs ─────────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, mode: str, full: bool):
        self.mode, self.full = mode, full
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.lines: list[str] = []
        self.stages: list[dict] = []
        self.steps = [{"label": label, "status": "pending", "exit": None} for label, _ in RUN_PLANS[mode]]
        self.status = "running"
        self.failed_stage: str | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.cond = threading.Condition()

    def _stage(self, num: str, name: str) -> dict:
        for s in self.stages:
            if s["num"] == num and s["name"] == name:
                return s
        s = {"num": num, "name": name, "status": "pending", "started": None, "ended": None}
        self.stages.append(s)
        return s

    def feed(self, line: str) -> None:
        line = redact(line.rstrip("\n"))
        with self.cond:
            self.lines.append(line)
            m = STAGE_START.search(line)
            if m:
                s = self._stage(m.group(1), m.group(2))
                s.update(status="running", started=time.time())
            m = STAGE_END.search(line)
            if m:
                s = self._stage(m.group(2), m.group(3))
                s.update(status="done" if m.group(4) == "complete" else "failed", ended=time.time())
                if s["started"] is None:
                    s["started"] = s["ended"]
            self.cond.notify_all()

    def summary(self) -> dict:
        return {"id": self.id, "mode": self.mode, "full": self.full, "status": self.status,
                "failed_stage": self.failed_stage, "steps": self.steps, "stages": self.stages,
                "started": self.started, "ended": self.ended, "line_count": len(self.lines),
                "tail": self.lines[-40:]}


RUN_LOCK = threading.Lock()
CURRENT: dict[str, Run | None] = {"run": None}


def _env(run: Run) -> dict:
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               FULL_RUN="true" if run.full else "false",
               DEMO_MODE="true" if run.mode == "demo" else "false")
    return env


def execute(run: Run) -> None:
    try:
        for i, (label, argv) in enumerate(RUN_PLANS[run.mode]):
            run.steps[i]["status"] = "running"
            run.feed(f"$ {' '.join(a if a != PY else 'python' for a in argv)}")
            proc = subprocess.Popen(argv, cwd=REPO, env=_env(run), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            assert proc.stdout is not None
            for line in proc.stdout:
                run.feed(line)
            code = proc.wait()
            run.steps[i].update(exit=code, status="done" if code == 0 else "failed")
            if code != 0:
                running = [s for s in run.stages if s["status"] == "running"]
                for s in running:
                    s.update(status="failed", ended=time.time())
                failed = [s for s in run.stages if s["status"] == "failed"]
                run.failed_stage = (f"Stage {failed[-1]['num']} {failed[-1]['name']}" if failed else label)
                run.feed(f"[console] step '{label}' exited with code {code} — run stopped")
                run.status = "failed"
                for later in run.steps[i + 1:]:
                    later["status"] = "skipped"
                return
        run.status = "done"
    except Exception as exc:  # surfaced on the page, never swallowed
        run.status = "failed"
        run.failed_stage = run.failed_stage or "console"
        run.feed(f"[console] {type(exc).__name__}: {exc}")
    finally:
        run.ended = time.time()
        with run.cond:
            run.cond.notify_all()
        RUN_LOCK.release()


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ReleaseConsole/1"
    snapshot_only = False

    def log_message(self, *_args) -> None:  # quiet; never logs request bodies or env
        return

    def _json(self, obj, status=HTTPStatus.OK) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/sources":
            srcs = sources()
            if self.snapshot_only:
                srcs = [s for s in srcs if s["id"] == "snapshot"]
            default = "snapshot" if self.snapshot_only or not any(s["id"] == "current" for s in srcs) else "current"
            if not any(s["id"] == default for s in srcs):
                default = srcs[0]["id"] if srcs else None
            run = CURRENT["run"]
            self._json({"sources": srcs, "default": default, "snapshot_only": self.snapshot_only,
                        "run": run.summary() if run else None})
        elif url.path == "/api/state":
            src = (q.get("source") or ["current"])[0]
            if self.snapshot_only and src != "snapshot":
                return self._json({"error": "snapshot mode: only demo-fallback/ is served"}, HTTPStatus.FORBIDDEN)
            self._json(state(src))
        elif url.path == "/api/run/stream":
            self._stream(int((q.get("from") or ["0"])[0]))
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path != "/api/run":
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        if self.headers.get("X-Console") != "1":
            return self._json({"error": "missing X-Console header"}, HTTPStatus.FORBIDDEN)
        if self.snapshot_only:
            return self._json({"error": "snapshot mode: runs are disabled"}, HTTPStatus.FORBIDDEN)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)
        mode = body.get("mode")
        if mode not in RUN_PLANS:
            return self._json({"error": "mode must be 'demo' or 'live'"}, HTTPStatus.BAD_REQUEST)
        if not RUN_LOCK.acquire(blocking=False):
            return self._json({"error": "a run is already in progress"}, HTTPStatus.CONFLICT)
        run = Run(mode, bool(body.get("full")))
        CURRENT["run"] = run
        threading.Thread(target=execute, args=(run,), daemon=True).start()
        self._json(run.summary(), HTTPStatus.ACCEPTED)

    def _stream(self, start: int) -> None:
        run = CURRENT["run"]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if run is None:
            self.wfile.write(b"event: idle\ndata: {}\n\n")
            return
        sent = start
        try:
            while True:
                with run.cond:
                    if sent >= len(run.lines) and run.ended is None:
                        run.cond.wait(timeout=15)
                    new = run.lines[sent:]
                    finished = run.ended is not None and sent + len(new) >= len(run.lines)
                for line in new:
                    self.wfile.write(f"event: line\ndata: {json.dumps(line)}\n\n".encode("utf-8"))
                    sent += 1
                self.wfile.write(f"event: status\ndata: {json.dumps(run.summary())}\n\n".encode("utf-8"))
                self.wfile.flush()
                if finished:
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    ap = argparse.ArgumentParser(description="Release Console (local, display only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--snapshot", action="store_true", help="serve demo-fallback/ only; runs disabled")
    args = ap.parse_args()
    load_dotenv()
    # --snapshot: serve demo-fallback/ only, runs disabled. An empty reports/ just
    # makes the snapshot the default view (see /api/sources); runs stay enabled.
    Handler.snapshot_only = args.snapshot
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "SNAPSHOT (demo-fallback/, runs disabled)" if Handler.snapshot_only else "live + history"
    print(f"Release Console on http://127.0.0.1:{args.port}  ·  {mode}  ·  Ctrl+C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
