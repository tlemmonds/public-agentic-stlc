"""
Stage 6 helper — pull HyperExecute's merged `TestReports` artifact back into reports/.

Native (testmu-SDK) tasks write their per-scenario results on the VM
(`reports/kane_result_<SC>_<browser>.json`, `reports/native_<SC>.xml`);
HyperExecute merges every VM's `uploadArtefacts` into one zip whose members are
suffixed with the task id (`kane_result_SC-004_chrome-HYPL-…HUJ.json`). Nothing
downloads that zip by default, so normalize_artifacts saw `data_unavailable`
for every native scenario. This script closes that gap:

  1. resolve the job id (arg, env HE_JOB_ID, reports/api_details.json, or the CLI log)
  2. GET https://api.hyperexecute.cloud/v2.0/artefacts/<job>/download?name=TestReports
     with LT basic auth
  3. unzip into reports/he_artifacts/<job>/ and copy the per-scenario files into
     reports/ with the task suffix stripped (a retried task's later copy wins)

Usage:  python ci/fetch_he_artifacts.py [--job-id ID] [--name TestReports]
Exit 0 even when nothing is found (Stage 6 stays advisory); exit 2 on auth/config errors.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from stage_utils import print_stage_header, print_stage_result  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS = REPO_ROOT / "reports"
API = "https://api.hyperexecute.cloud/v2.0"

_TASK_SUFFIX = re.compile(r"-(HYPL-[A-Za-z0-9-]+)(?=\.[A-Za-z0-9]+$)")
_WANTED = re.compile(r"^(kane_result_SC-\d+_[a-z_]+|native_SC-\d+|junit[A-Za-z0-9_-]*)\.(json|xml)$")


def _resolve_job_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = os.environ.get("HE_JOB_ID", "").strip()
    if env:
        return env
    api_details = REPORTS / "api_details.json"
    if api_details.exists():
        try:
            data = json.loads(api_details.read_text(encoding="utf-8"))
            for key in ("job_id", "jobId"):
                if data.get(key):
                    return str(data[key])
            summary = data.get("he_summary") or {}
            for key in ("job_id", "jobId", "id"):
                if summary.get(key):
                    return str(summary[key])
        except (OSError, json.JSONDecodeError):
            pass
    cli_log = REPORTS / "hyperexecute-cli.log"
    if cli_log.exists():
        m = re.search(r"jobId=([0-9a-f-]{36})", cli_log.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return None


def _download(job_id: str, name: str, username: str, access_key: str) -> bytes:
    """The artefact endpoint 302-redirects to a signed blob URL. Basic auth must
    be sent to the API host only — forwarding it to the blob host yields 403 —
    so use httpx (drops auth on cross-host redirects); fall back to a urllib
    redirect handler that strips the header."""
    url = f"{API}/artefacts/{job_id}/download?name={name}"
    try:
        import httpx  # engine dependency
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            resp = client.get(url, auth=(username, access_key), headers={"Accept": "*/*"})
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(url, resp.status_code, resp.reason_phrase, hdrs=None, fp=None)  # type: ignore[arg-type]
            return resp.content
    except ImportError:
        pass

    class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new_req is not None and "Authorization" in new_req.headers:
                new_req.remove_header("Authorization")
            return new_req

    token = base64.b64encode(f"{username}:{access_key}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "*/*"})
    opener = urllib.request.build_opener(_StripAuthRedirect())
    with opener.open(req, timeout=120) as resp:  # noqa: S310 — fixed https host
        return resp.read()


def _place(zip_bytes: bytes, job_id: str) -> tuple[int, int, list[str]]:
    extract_dir = REPORTS / "he_artifacts" / job_id
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    members = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            members += 1
            base = Path(info.filename).name
            if not _WANTED.match(_TASK_SUFFIX.sub("", base)):
                continue
            with zf.open(info) as src, open(extract_dir / base, "wb") as dst:
                shutil.copyfileobj(src, dst)
    _, placed, names = _place_from_dir(extract_dir)
    return members, placed, names


def _place_from_dir(extract_dir: Path) -> tuple[int, int, list[str]]:
    """Select one genuine file per stripped name from an extracted artefact dir and copy it into reports/."""
    members = 0
    candidates: dict[str, list[Path]] = {}
    for target_tmp in sorted(extract_dir.iterdir()):
        if not target_tmp.is_file():
            continue
        members += 1
        stripped = _TASK_SUFFIX.sub("", target_tmp.name)
        if _WANTED.match(stripped):
            candidates.setdefault(stripped, []).append(target_tmp)
    # Every VM uploads its whole reports/ dir, so a result file left over from an
    # earlier job in the working tree comes back N times (once per VM, with a
    # doubled task suffix) alongside the one genuine result. Zip order is not
    # provenance — pick the JSON whose start_time is newest, and the XML that
    # shares that JSON's task id.
    placed: dict[str, Path] = {}
    chosen_task: dict[str, str] = {}
    for stripped, paths in candidates.items():
        if stripped.endswith(".json"):
            best = max(paths, key=_result_start_time)
            placed[stripped] = best
            m = _TASK_SUFFIX.search(best.name)
            if m:
                chosen_task[stripped[: -len(".json")].replace("kane_result_", "").rsplit("_", 1)[0]] = m.group(1)
    for stripped, paths in candidates.items():
        if stripped.endswith(".xml"):
            sc = stripped[: -len(".xml")].replace("native_", "")
            want = chosen_task.get(sc)
            # Exact name only: an echoed stale copy carries a doubled suffix
            # (…-<oldtask>-<want>.xml) and would also pass an endswith() test.
            match = [p for p in paths if want and p.name == f"{stripped[: -len('.xml')]}-{want}.xml"]
            placed[stripped] = match[0] if match else max(paths, key=lambda p: p.stat().st_mtime)
    for stripped, src in placed.items():
        shutil.copy2(src, REPORTS / stripped)
    return members, len(placed), sorted(placed)


def _result_start_time(path: Path) -> str:
    """ISO start_time of a kane_result json ('' when unreadable) — sortable as text."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("start_time") or data.get("end_time") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", default=None)
    ap.add_argument("--name", default="TestReports", help="artefact name from hyperexecute.yaml uploadArtefacts")
    ap.add_argument("--force", action="store_true", help="re-download even if this job's artefact is already extracted")
    args = ap.parse_args()

    print_stage_header("6b", "FETCH_HE_ARTIFACTS", "Pull merged HyperExecute artefacts into reports/")
    REPORTS.mkdir(exist_ok=True)

    job_id = _resolve_job_id(args.job_id)
    username = os.environ.get("LT_USERNAME", "")
    access_key = os.environ.get("LT_ACCESS_KEY", "")
    if not job_id:
        print_stage_result("6b", "FETCH_HE_ARTIFACTS", {"Status": "skipped — no job id resolved"})
        return 0
    if not username or not access_key:
        print_stage_result("6b", "FETCH_HE_ARTIFACTS", {"Status": "skipped — LT credentials missing"})
        return 2

    # Idempotent re-run (post_pipeline calls this again after Stage 6): if this
    # job's artefact was already extracted, re-place from disk — a second
    # download of the same artefact is refused (403) for a while.
    extract_dir = REPORTS / "he_artifacts" / job_id
    if not args.force and extract_dir.is_dir() and any(extract_dir.iterdir()):
        members, placed, names = _place_from_dir(extract_dir)
        print_stage_result("6b", "FETCH_HE_ARTIFACTS", {
            "Job": job_id,
            "Artefact": f"{args.name} (already extracted, {members} members on disk)",
            "Per-scenario files placed": placed,
            "Examples": ", ".join(names[:4]) + (" …" if len(names) > 4 else ""),
            "Extracted to": str(extract_dir.relative_to(REPO_ROOT)),
        })
        return 0

    # HyperExecute merges the per-VM uploads *after* the CLI reports the job
    # finished; for a minute or two the artefact answers 404 (not merged yet) or
    # 403 (signed URL not issued yet). Poll instead of giving up on first touch.
    wait_s = int(os.environ.get("HE_ARTIFACT_WAIT_SECONDS", "300"))
    poll_s = int(os.environ.get("HE_ARTIFACT_POLL_SECONDS", "20"))
    deadline = time.monotonic() + wait_s
    last_status = ""
    blob = None
    while True:
        try:
            blob = _download(job_id, args.name, username, access_key)
            break
        except urllib.error.HTTPError as exc:
            last_status = f"HTTP {exc.code} — {exc.reason}"
            if exc.code not in (403, 404) or time.monotonic() >= deadline:
                print_stage_result("6b", "FETCH_HE_ARTIFACTS", {"Job": job_id, "Status": last_status,
                                                                 "Waited": f"{wait_s}s" if exc.code in (403, 404) else "—"})
                return 0 if exc.code == 404 else 2
        except (urllib.error.URLError, TimeoutError) as exc:
            last_status = f"download failed — {exc}"
            if time.monotonic() >= deadline:
                print_stage_result("6b", "FETCH_HE_ARTIFACTS", {"Job": job_id, "Status": last_status})
                return 0
        print(f"[6b] artefact not ready ({last_status}); retrying in {poll_s}s")
        time.sleep(poll_s)

    members, placed, names = _place(blob, job_id)
    print_stage_result("6b", "FETCH_HE_ARTIFACTS", {
        "Job": job_id,
        "Artefact": f"{args.name} ({len(blob)} bytes, {members} members)",
        "Per-scenario files placed": placed,
        "Examples": ", ".join(names[:4]) + (" …" if len(names) > 4 else ""),
        "Extracted to": str((REPORTS / "he_artifacts" / job_id).relative_to(REPO_ROOT)),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
