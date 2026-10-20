"""Stage 1 dispatcher — replaces the inline `_KANE_TASK_OVERRIDES` lookup
in analyze_requirements.py with a replay-first policy.

Many-to-one: the unit of work is the *scenario*, not
the requirement. For each non-deprecated scenario the pipeline asks: replay
an existing _test.md, record a new one, or skip? See ci/replay_policy.py
for the decision algorithm. Results carry `scenario_id` + `requirement_id`
so Stage 1 can roll them up per requirement.
"""
from __future__ import annotations

import json
import os
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from replay_policy import (
    REPO_ROOT,
    ReplayDecision,
    asset_path_for,
    decide,
    hash_description,
)
import kane_record
import kane_replay
import project_config

CODE_EXPORT_CACHE = REPO_ROOT / "tests" / "playwright" / "exported"
DECISIONS_LOG = REPO_ROOT / "reports" / "replay_decisions.json"

# Sites/phrases Kane's headless planner falls back to when it loses the AUT
# URL anchor. Both literal hostnames and generic phrasing — Kane sometimes
# summarizes a drift as "first landing on a LambdaTest playground page"
# without naming the exact host. If a session's evidence (summary +
# one_liner) mentions any of these but not the configured TARGET_URL, we
# treat the recording as drifted and retry.
_KNOWN_DRIFT_HOSTS = (
    "kaneai-playground.lambdatest.io",
    "ecommerce-playground.lambdatest.io",
    "lambdatest playground",
    "lambdatest demo",
)

# Maximum retries per AC when drift is detected. Cap is low because each retry
# costs a full Kane session (~3-5 min); two re-rolls is usually enough to escape
# a transient planner glitch, and persistent drift is a deeper problem to flag.
_MAX_DRIFT_RETRIES = 2


def _detect_drift(result: dict, target_url: str) -> str | None:
    """Return a drift reason if the recording landed off the AUT, else None.

    Only signal: explicit mention of a known fallback site in the session
    evidence. The previous "target host absent" check was too aggressive —
    successful replays often have terse summaries that don't repeat the host
    name, leading to false-positive drifts that purged proven assets.
    Persisted drift to a brand-new fallback site requires adding that site
    to _KNOWN_DRIFT_HOSTS rather than relying on inverse-matching."""
    if result.get("status") not in ("passed", "failed"):
        return None
    text = (
        (result.get("summary") or "") + " " + (result.get("one_liner") or "")
    ).lower()
    for site in _KNOWN_DRIFT_HOSTS:
        if site in text:
            return f"session evidence mentions {site}"
    return None


def _purge_drifted_asset(asset_path: Path | None) -> None:
    """Delete the asset + its sidecar so the next attempt records fresh.
    Contaminated assets are worse than no asset at all — every subsequent
    replay would walk the same broken navigation sequence."""
    if not asset_path:
        return
    if asset_path.exists():
        asset_path.unlink(missing_ok=True)
    sidecar = asset_path.with_suffix(".meta.json")
    if sidecar.exists():
        sidecar.unlink(missing_ok=True)
    cached = _code_export_cache_path(asset_path)
    if cached and cached.exists():
        cached.unlink(missing_ok=True)


def _code_export_cache_path(asset_path: Path | None) -> Path | None:
    """Where the Playwright code-export is cached alongside the test.md asset.
    Kane only emits --code-export on fresh records, not replays, so the
    cache lets Stage 3a still find a real test body on replay-heavy runs
    instead of falling through to pytest.skip stubs."""
    if not asset_path:
        return None
    stem = asset_path.stem
    if stem.endswith("_test"):
        stem = stem[:-len("_test")]
    return asset_path.with_name(f"{stem}.playwright.py")


def _cache_code_export(asset_path: Path | None, code_export_dir: Path | None) -> bool:
    """After a successful record, persist Kane's Playwright code-export so
    future replays can restore it. Returns True if a file was cached."""
    cache = _code_export_cache_path(asset_path)
    if not cache or not code_export_dir:
        return False
    live = code_export_dir / "test.py"
    if not live.exists() or live.stat().st_size == 0:
        return False
    try:
        cache.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except OSError:
        return False


def _restore_code_export(asset_path: Path | None, code_export_dir: Path | None) -> bool:
    """Before a replay, hydrate code_export_dir from the asset's cached
    Playwright export so Stage 3a finds something even if Kane doesn't
    re-emit on replay. No-op if the live dir already has content (don't
    clobber a fresh export) or if there's no cache yet."""
    cache = _code_export_cache_path(asset_path)
    if not cache or not cache.exists() or not code_export_dir:
        return False
    live = code_export_dir / "test.py"
    if live.exists() and live.stat().st_size > 0:
        return False
    try:
        code_export_dir.mkdir(parents=True, exist_ok=True)
        live.write_text(cache.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except OSError:
        return False


def build_name() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    return f"Agentic STLC #{run_number} | {today}" if run_number else f"Agentic STLC | {today}"


def _description_for(sc: dict) -> str:
    """Tolerate both the current `description` key and the legacy
    `source_description` key emitted by older Stage 2 runs."""
    return sc.get("description") or sc.get("source_description") or ""


def _objective_for(sc: dict, description: str) -> str:
    """Reuse the canonical objective baked into scenarios.json
    (kane_objective) if present; fall back to the AC description prefixed
    with TARGET_URL so Kane lands on the right site.

    The URL prefix is critical: without it, Kane defaults to its own demo
    site (kaneai-playground.lambdatest.io) instead of the AUT. We only
    inject TARGET_URL when the objective doesn't already mention it, to
    avoid double-prefixing custom objectives."""
    if sc.get("kane_objective"):
        return sc["kane_objective"]
    target_url = os.environ.get("TARGET_URL", "").strip()
    if target_url and target_url.rstrip("/") not in description:
        return f"On {target_url} — {description}"
    return description


def _export_subdir_for(sc_id: str) -> Path:
    return CODE_EXPORT_CACHE / sc_id.lower().replace("-", "_")


def _scenarios_from(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _skipped_result(decision: ReplayDecision) -> dict[str, Any]:
    return {
        "status": "skipped",
        "summary": f"skipped: {decision.reason}",
        "one_liner": "", "steps": [], "final_state": {},
        "duration": None, "test_url": "",
        "session_id": "", "code_export_dir": "",
        "asset_path": str(decision.asset_path) if decision.asset_path else "",
        "replay_decision": decision.decision,
    }


def dispatch_one(
    *,
    requirement: dict,
    scenario: dict,
    username: str,
    access_key: str,
    force_re_author: bool,
    allow_record: bool = True,
) -> tuple[ReplayDecision, dict[str, Any]]:
    """Decide + execute for a single scenario. Returns (decision, kane_result).

    requirement: {"id": "AC-001", "description": ..., "brd_ref": ...}
    scenario:    one scenarios.json record (non-deprecated)

    The kane_result shape matches analyze_requirements.run_kane() plus
    `scenario_id` / `requirement_id`, so Stage 1 can roll results up to the
    requirement.

    Legacy (generated) scenarios behave exactly as before: asset located via
    existing_asset_path / asset_path_for, hash of the requirement description.
    Ingested scenarios (`kane_asset` / source=ingested): the asset path is
    authoritative, the hash is of the asset body, and the pipeline never
    records or purges the customer's asset — a would-be rerecord becomes a
    replay of the edited test, and drift retries are disabled."""
    requirement_id = requirement["id"]
    description = requirement.get("description") or _description_for(scenario)
    sc_id = scenario["id"]
    feature = scenario.get("feature", "general")
    title = scenario.get("title", description[:60])
    ingested = project_config.is_ingested(scenario)
    asset_override = project_config.scenario_asset_path(scenario)

    decision = decide(
        sc_id=sc_id,
        requirement_id=requirement_id,
        description=description,
        feature=feature,
        title=title,
        force_re_author=force_re_author,
        deprecated=(scenario.get("status") == "deprecated"),
        asset_override=asset_override,
        hash_source="asset" if ingested else "description",
        allow_record=allow_record,
    )

    if ingested and decision.decision in ("record", "rerecord") and decision.asset_path and decision.asset_path.exists():
        # The customer's test is the source of truth. If its body drifted,
        # the human edited it — replay the edited version, never overwrite it.
        decision = ReplayDecision(
            sc_id=sc_id, requirement_id=requirement_id, decision="replay",
            asset_path=decision.asset_path,
            reason=f"ingested asset is authoritative — replaying instead of {decision.decision} ({decision.reason})",
            description_hash=decision.description_hash, hash_source=decision.hash_source,
        )

    if decision.decision == "skip":
        result = _skipped_result(decision)
        result["scenario_id"] = sc_id
        result["requirement_id"] = requirement_id
        return decision, result

    objective = _objective_for(scenario, description)
    export_dir = _export_subdir_for(sc_id)
    bn = build_name()

    # First-record on a new AUT (serverless cold start, large JS bundle, etc.)
    # often needs more headroom than replays of an already-mapped flow.
    record_timeout = int(os.environ.get("KANE_RECORD_TIMEOUT", "300"))
    replay_timeout = int(os.environ.get("KANE_REPLAY_TIMEOUT", "180"))

    target_url = os.environ.get("TARGET_URL", "").strip()
    target_asset = decision.asset_path or asset_path_for(sc_id, feature, title)
    session_label = scenario.get("kane_session_name") or f"{sc_id} | {title[:60]}"
    current_decision = decision
    drift_attempts = 0
    drift_history: list[str] = []
    # Drift recovery re-records from scratch. That is only legitimate for
    # pipeline-owned assets when recording is allowed.
    drift_retry_budget = 0 if (ingested or not allow_record) else _MAX_DRIFT_RETRIES

    while True:
        if current_decision.decision == "replay":
            # Replays don't trigger Kane's --code-export, so pre-hydrate the
            # live export dir from the cached copy alongside the asset.
            # Stage 3a then finds a real test body instead of a skip stub.
            _restore_code_export(current_decision.asset_path, export_dir)
            # A failed replay is retried (kaneai.replay_retries, default 1). This
            # covers kane-cli runner crashes at startup (SIGBUS, no browser) and
            # agent flakes. The retry is logged and kept in the result.
            replay_retries = int(project_config.cfg("kaneai.replay_retries", 1) or 0)
            for replay_attempt in range(replay_retries + 1):
                result = kane_replay.replay(
                    current_decision.asset_path,
                    session_name=f"Replay {session_label}",
                    build_name=bn,
                    username=username,
                    access_key=access_key,
                    timeout_seconds=replay_timeout,
                    code_export_dir=export_dir,
                )
                # "error" is a kane-cli crash (e.g. two runners hitting SIGBUS in
                # the same second at startup); "failed" is a run that ended.
                if result.get("status") not in ("failed", "error") or replay_attempt == replay_retries:
                    break
                print(f"    [retry] {sc_id}: replay {result.get('status')} "
                      f"({(result.get('summary') or '')[-120:]!r}); retrying "
                      f"{replay_attempt + 1}/{replay_retries}", flush=True)
                time.sleep(float(os.environ.get("KANE_RETRY_DELAY", "10")))
            if replay_attempt:
                result["replay_retries"] = replay_attempt
        else:
            # record (new asset) or rerecord (asset exists but stale).
            result = kane_record.record(
                objective,
                sc_id=sc_id,
                requirement_id=requirement_id,
                description=description,
                description_hash=current_decision.description_hash,
                feature=feature,
                asset_path=target_asset,
                build_name=bn,
                username=username,
                access_key=access_key,
                timeout_seconds=record_timeout,
                code_export_dir=export_dir,
            )
            # On successful record, persist the export next to the asset
            # so future replays can hydrate from cache.
            if result.get("status") == "passed":
                _cache_code_export(target_asset, export_dir)

        drift_reason = _detect_drift(result, target_url)
        if drift_reason is None or drift_attempts >= drift_retry_budget:
            if drift_reason is not None:
                drift_history.append(f"{drift_reason} (no retry budget)")
            break

        drift_attempts += 1
        drift_history.append(drift_reason)
        print(
            f"[drift] {sc_id} attempt {drift_attempts}/{drift_retry_budget}: "
            f"{drift_reason} — purging contaminated asset and re-recording fresh",
            flush=True,
        )
        # Whether the drift happened during replay (asset contaminated) or
        # record (Kane's first plan landed off-site), the only way out is to
        # wipe the artifact and try the record path again — replaying a
        # contaminated asset would walk the same broken sequence.
        _purge_drifted_asset(current_decision.asset_path)
        current_decision = ReplayDecision(
            sc_id=sc_id,
            requirement_id=requirement_id,
            decision="record",
            asset_path=target_asset,
            reason=f"drift retry {drift_attempts}: {drift_reason}",
            description_hash=current_decision.description_hash,
            hash_source=current_decision.hash_source,
        )

    result["scenario_id"] = sc_id
    result["requirement_id"] = requirement_id
    result["asset_path"] = str(decision.asset_path or "")
    result["replay_decision"] = current_decision.decision
    if drift_attempts:
        result["drift_retries"] = drift_attempts
        result["drift_history"] = drift_history
    return current_decision, result


def dispatch_all(
    requirements: list[dict],
    scenarios: list[dict],
    *,
    username: str,
    access_key: str,
    max_workers: int = 5,
) -> list[dict[str, Any]]:
    """Dispatch every non-deprecated scenario in parallel.

    requirements: [{"id": "AC-001", "description": ..., "brd_ref": ...}, ...]
    scenarios:    scenarios.json records (many may share one requirement_id)

    The work list is the scenarios, not the requirements: a requirement with
    zero scenarios produces no Kane work (Stage 1 reports it as not_run).
    Returns one kane-result dict per scenario, in scenarios.json order, each
    carrying `scenario_id` + `requirement_id`. Writes replay_decisions.json
    with one entry per scenario."""
    from concurrent.futures import ThreadPoolExecutor

    force = os.environ.get("FORCE_RE_AUTHOR", "false").lower() == "true"
    allow_record = project_config.auto_record()
    req_by_id = {r["id"]: r for r in requirements if isinstance(r, dict) and r.get("id")}

    work: list[tuple[dict, dict]] = []
    for sc in scenarios:
        if not isinstance(sc, dict) or not sc.get("id"):
            continue
        if sc.get("status") == "deprecated":
            continue
        req = req_by_id.get(sc.get("requirement_id"))
        if req is None:
            print(
                f"[Stage 1] {sc['id']} references unknown requirement "
                f"{sc.get('requirement_id')!r} — skipping",
                flush=True,
            )
            continue
        work.append((req, sc))

    decisions: list[ReplayDecision] = []
    results: list[dict[str, Any]] = [None] * len(work)  # type: ignore[list-item]

    # Stagger the first wave of kane-cli launches. CI run 5: two runners
    # started in the same second both crashed (SIGBUS, "concurrent session").
    stagger = float(os.environ.get("KANE_LAUNCH_STAGGER", "4"))

    def _work(indexed: tuple[int, tuple[dict, dict]]) -> tuple[ReplayDecision, dict[str, Any]]:
        index, (req, sc) = indexed
        if index < max_workers and stagger > 0:
            time.sleep(index * stagger)
        return dispatch_one(
            requirement=req,
            scenario=sc,
            username=username,
            access_key=access_key,
            force_re_author=force,
            allow_record=allow_record,
        )

    if work:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for i, (decision, result) in enumerate(pool.map(_work, enumerate(work))):
                decisions.append(decision)
                results[i] = result

    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "force_re_author": force,
        "auto_record": allow_record,
        "decisions": [d.to_dict() for d in decisions],
        "summary": {
            label: sum(1 for d in decisions if d.decision == label)
            for label in ("replay", "record", "rerecord", "skip")
        },
    }
    DECISIONS_LOG.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return results
