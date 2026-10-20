"""Agentic Release Notes — Stage 0.

Given the most recent release lock (`release_notes/<prev>.lock.json`,
a frozen snapshot of the requirement list + scenarios.json from the last
shipped release) and a new release-notes markdown file
(`release_notes/<next>.md`), produce an Add / Edit / Delete operations
list against the current test pool.

Two modes:

    python ci/release_diff.py --propose
        Default. Compute the delta and write reports/release_delta.{json,md}.
        Mutates nothing. Used by CI on every push to release_notes/.

    python ci/release_diff.py --apply
        Apply the operations: mutate scenarios.json, write the new lock
        file, and emit a one-line audit record. Use after a human has
        reviewed the propose output.

Decision algorithm (pure rules, deterministic):

    Section "Added"   → ADD    : always create a new AC. A scenario is
                                 created only when `scenarios.auto_create`
                                 is true (default).
    Section "Changed" → EDIT   : match release item text to an existing
                                 REQUIREMENT by token Jaccard similarity
                                 (default ≥ 0.5). No match → unmatched_items[].
    Section "Removed" → DELETE : same matching; marks EVERY scenario of the
                                 requirement deprecated (never deleted).
    Section "Fixed"   → noted only; no scenario op.

Many-to-one: a requirement may be covered by several
scenarios. Ops carry `requirement_id` + `sc_ids` (all scenarios of that
requirement); `sc_id` is kept as the first id for legacy renderers.

Lock schema (new):
    {"release", "generated_at", "requirements": [{"id","brd_ref","description"}],
     "scenarios": [...]}
Legacy locks (scenarios only) are still read: matching then falls back to
scenario descriptions and the requirement list is derived from them.

Match threshold is conservative on purpose: it's better to surface an
"unmatched" warning to the author than to silently retitle the wrong
requirement.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from release_notes_parser import parse_file, parse_version_from_filename
from stage_utils import print_stage_header, print_stage_result
from project_config import (
    auto_create_scenarios,
    cfg,
    classify_feature,
    group_scenarios_by_requirement,
    is_ingested,
    scenario_title,
    session_start_url,
)

REPO_ROOT      = Path(__file__).resolve().parent.parent
RELEASE_DIR    = REPO_ROOT / "release_notes"
SCENARIOS_PATH = REPO_ROOT / "scenarios" / "scenarios.json"
ANALYZED_REQS  = REPO_ROOT / "requirements" / "analyzed_requirements.json"
DELTA_JSON     = REPO_ROOT / "reports" / "release_delta.json"
DELTA_MD       = REPO_ROOT / "reports" / "release_delta.md"

DEFAULT_MATCH_THRESHOLD = 0.5

# ── Token similarity (Jaccard) ─────────────────────────────────────────────
# Stop words removed before tokenization so "the user can add" doesn't
# perfectly match "the user can remove" by the shared filler tokens.
_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "can", "for", "from", "in",
    "is", "it", "of", "on", "or", "so", "that", "the", "their", "this",
    "to", "user", "users", "want", "wants", "with", "see", "sees",
}
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
# Parenthetical phrases in release-notes prose are author commentary, not
# scenario-defining tokens. "User can delete a task (items now move to Archive
# instead)" should still match SC-005 "User can delete a task" — without this,
# the parenthetical's extra tokens dilute the Jaccard score below threshold.
_PARENS_RE = re.compile(r"\([^)]*\)")


def _tokens(text: str) -> set[str]:
    stripped = _PARENS_RE.sub(" ", text or "")
    return {t.lower() for t in _TOKEN_RE.findall(stripped) if t.lower() not in _STOPWORDS and len(t) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ── Operation model ────────────────────────────────────────────────────────
@dataclass
class Operation:
    op: str               # ADD | EDIT | DELETE
    item_text: str
    item_section: str
    issue: str | None
    sc_id: str | None     # first scenario of the requirement (legacy renderers)
    requirement_id: str | None
    match_score: float    # 0.0 for ADD; jaccard for EDIT/DELETE
    rationale: str
    prev_text: str | None = None  # the previous-release description; populated for EDIT/DELETE
    sc_ids: list[str] = field(default_factory=list)  # every scenario of the requirement

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "item_text": self.item_text,
            "item_section": self.item_section,
            "issue": self.issue,
            "sc_id": self.sc_id,
            "sc_ids": list(self.sc_ids),
            "requirement_id": self.requirement_id,
            "match_score": round(self.match_score, 3),
            "rationale": self.rationale,
            "prev_text": self.prev_text,
        }


@dataclass
class DiffResult:
    from_release: str
    to_release: str
    from_lock_path: str
    to_lock_path: str
    operations: list[Operation] = field(default_factory=list)
    unmatched_items: list[dict] = field(default_factory=list)
    threshold: float = DEFAULT_MATCH_THRESHOLD
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def summary(self) -> dict:
        return {
            label: sum(1 for o in self.operations if o.op == label)
            for label in ("ADD", "EDIT", "DELETE")
        } | {"UNMATCHED": len(self.unmatched_items)}

    def to_dict(self) -> dict:
        return {
            "from_release": self.from_release,
            "to_release": self.to_release,
            "from_lock_path": self.from_lock_path,
            "to_lock_path": self.to_lock_path,
            "threshold": self.threshold,
            "generated_at": self.generated_at,
            "summary": self.summary,
            "operations": [o.to_dict() for o in self.operations],
            "unmatched_items": self.unmatched_items,
        }


# ── Inputs ─────────────────────────────────────────────────────────────────

# Release versions are sorted as tuples of integers so v1.10.0 > v1.2.0.
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?$")


def _version_key(stem: str) -> tuple[int, ...]:
    m = _VERSION_RE.match(stem)
    if not m:
        return (0,)
    return tuple(int(g) if g else 0 for g in m.groups())


def _release_files() -> tuple[list[Path], list[Path]]:
    """Return (notes, locks) sorted ascending by version."""
    if not RELEASE_DIR.exists():
        return [], []
    notes = sorted(
        (p for p in RELEASE_DIR.glob("*.md") if not p.name.lower().startswith("readme")),
        key=lambda p: _version_key(p.stem),
    )
    locks = sorted(RELEASE_DIR.glob("*.lock.json"), key=lambda p: _version_key(p.stem.replace(".lock", "")))
    return notes, locks


def resolve_release_pair(target: Path | None = None) -> tuple[Path, Path | None]:
    """Pick the release-notes file to diff and its predecessor lock file.

    target  — explicit path to the new release notes (overrides auto-pick)
    Returns (notes_path, prev_lock_path or None for the very first release).
    """
    notes, locks = _release_files()
    if target is not None:
        notes_path = target
    elif notes:
        notes_path = notes[-1]
    else:
        raise FileNotFoundError(
            f"No release notes found under {RELEASE_DIR.relative_to(REPO_ROOT)}/. "
            f"Author release_notes/v1.0.0.md to bootstrap the first release."
        )
    target_key = _version_key(notes_path.stem)
    prev_locks = [p for p in locks if _version_key(p.stem.replace(".lock", "")) < target_key]
    return notes_path, (prev_locks[-1] if prev_locks else None)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _description_of(scenario: dict) -> str:
    return scenario.get("description") or scenario.get("source_description") or scenario.get("title") or ""


def _ac_num(rid: str | None) -> int:
    m = re.match(r"AC-(\d+)", str(rid or ""))
    return int(m.group(1)) if m else 0


def _requirements_from_scenarios(scenarios: list[dict]) -> list[dict]:
    """Legacy lock / no lock: derive the requirement list from the scenario
    pool. Description = first non-deprecated scenario's description (falls
    back to the first scenario when all are deprecated)."""
    grouped = group_scenarios_by_requirement(scenarios, include_deprecated=True)
    out: list[dict] = []
    for rid, scs in grouped.items():
        live = [s for s in scs if s.get("status") != "deprecated"]
        rep = (live or scs)[0]
        rec = {"id": rid, "brd_ref": rep.get("brd_ref"), "description": _description_of(rep)}
        if not live:
            rec["status"] = "deprecated"
        out.append(rec)
    return sorted(out, key=lambda r: _ac_num(r["id"]))


def _parse_requirements_txt(paths: list[Path]) -> list[dict]:
    """Minimal parser for the `requirements.paths` .txt files: lines under
    "Acceptance Criteria:"; an optional leading `[REF]` token becomes
    `brd_ref`. Ids are sequential AC-NNN across all files."""
    out: list[dict] = []
    ref_re = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
    for path in paths:
        if not path.exists():
            continue
        in_ac = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not in_ac:
                if line.lower().startswith("acceptance criteria"):
                    in_ac = True
                continue
            if not line:
                continue
            if line.endswith(":") and not line.startswith(("-", "*", "[")):
                break  # next section header
            line = re.sub(r"^[-*•]\s*", "", line)
            brd_ref = None
            m = ref_re.match(line)
            if m:
                brd_ref, line = m.group(1).strip(), m.group(2).strip()
            if not line:
                continue
            out.append({"id": f"AC-{len(out) + 1:03d}", "brd_ref": brd_ref, "description": line})
    return out


def current_requirements() -> list[dict]:
    """The requirement list for the CURRENT release.

    The `requirements.paths` .txt files from agentic-stlc.config.yaml are the
    source of truth (they carry the [REF] labels ADD-binding relies on);
    requirements/analyzed_requirements.json is only a fallback for projects
    with no configured paths — it is a Stage 1 *output* and can be stale
    (e.g. new ACs added since the last Kane run)."""
    paths = cfg("requirements.paths") or []
    if isinstance(paths, str):
        paths = [paths]
    txt_paths = [REPO_ROOT / p for p in paths if (REPO_ROOT / p).exists()]
    if txt_paths:
        parsed = _parse_requirements_txt(txt_paths)
        if parsed:
            return parsed
    if ANALYZED_REQS.exists():
        try:
            data = json.loads(ANALYZED_REQS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        if isinstance(data, list) and data:
            return [
                {"id": r.get("id"), "brd_ref": r.get("brd_ref"), "description": r.get("description", "")}
                for r in data if isinstance(r, dict) and r.get("id")
            ]
    return []


def _load_lock_for_diff(prev_lock: Path | None) -> tuple[list[dict], list[dict], bool, str]:
    """Return (requirements, scenarios, lock_has_requirements, source_label).

    If a prev_lock is supplied it's the canonical R(n-1) snapshot; otherwise
    we fall back to the current scenarios.json (first-release case). Legacy
    locks (scenarios only) get a requirement list derived from the scenarios."""
    if prev_lock is not None and prev_lock.exists():
        data = json.loads(prev_lock.read_text(encoding="utf-8"))
        scenarios = data.get("scenarios", []) or []
        reqs = data.get("requirements")
        if isinstance(reqs, list) and reqs:
            return reqs, scenarios, True, _rel(prev_lock)
        return _requirements_from_scenarios(scenarios), scenarios, False, _rel(prev_lock)
    if SCENARIOS_PATH.exists():
        scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
        return _requirements_from_scenarios(scenarios), scenarios, False, _rel(SCENARIOS_PATH)
    return [], [], False, "<empty>"


# ── Matching + diff ────────────────────────────────────────────────────────
def _match_candidates(requirements: list[dict], scenarios: list[dict], lock_has_requirements: bool) -> list[dict]:
    """One candidate per matchable unit: {"text", "requirement_id", "sc_ids"}.
    New locks → one per non-deprecated requirement (text = requirement
    description). Legacy locks → one per non-deprecated scenario (text =
    scenario description), exactly as before; sc_ids still expands to every
    live scenario of that requirement."""
    grouped = group_scenarios_by_requirement(scenarios, include_deprecated=False)
    live_ids = {rid: [s.get("id") for s in scs] for rid, scs in grouped.items()}
    if lock_has_requirements:
        out = []
        for r in requirements:
            rid = r.get("id")
            if not rid or r.get("status") == "deprecated":
                continue
            all_scs = [s for s in scenarios if s.get("requirement_id") == rid]
            if all_scs and not live_ids.get(rid):
                continue  # every scenario deprecated → requirement retired
            out.append({"text": r.get("description", ""), "requirement_id": rid,
                        "sc_ids": list(live_ids.get(rid, []))})
        return out
    return [
        {"text": _description_of(s), "requirement_id": s.get("requirement_id"),
         "sc_ids": list(live_ids.get(s.get("requirement_id"), [s.get("id")]))}
        for s in scenarios if s.get("status") != "deprecated"
    ]


_REF_TOKEN = re.compile(r"^\[([A-Za-z0-9.,_-]+)\]\s*")


def _split_ref(text: str) -> tuple[str | None, str]:
    """'[BR-1.1] Create a task …' → ('BR-1.1', 'Create a task …'); no token → (None, text)."""
    m = _REF_TOKEN.match(text or "")
    return (m.group(1), text[m.end():].strip()) if m else (None, (text or "").strip())


def _best_match(item_text: str, candidates: list[dict], threshold: float) -> tuple[dict | None, float]:
    best: dict | None = None
    best_score = 0.0
    for c in candidates:
        score = jaccard(item_text, c["text"])
        if score > best_score:
            best, best_score = c, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


def _next_ac_id(requirements: list[dict], scenarios: list[dict]) -> str:
    max_n = max(
        [_ac_num(r.get("id")) for r in requirements] + [_ac_num(s.get("requirement_id")) for s in scenarios],
        default=0,
    )
    return f"AC-{max_n + 1:03d}"


def diff(
    notes_path: Path,
    prev_lock: Path | None,
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> DiffResult:
    items = parse_file(notes_path)
    prev_reqs, prev_scenarios, has_reqs, source_label = _load_lock_for_diff(prev_lock)

    next_ac_index = int(_next_ac_id(prev_reqs, prev_scenarios).split("-")[1])
    targets = _match_candidates(prev_reqs, prev_scenarios, has_reqs)  # matched units removed as we go

    result = DiffResult(
        from_release=Path(source_label).stem.replace(".lock", "") if prev_lock else "<initial>",
        to_release=parse_version_from_filename(notes_path),
        from_lock_path=source_label,
        to_lock_path=str((RELEASE_DIR / f"{parse_version_from_filename(notes_path)}.lock.json").relative_to(REPO_ROOT)),
        threshold=threshold,
    )

    # ADD binding: a bullet that already corresponds to a requirement in the
    # current requirements source (by [REF] → brd_ref, else Jaccard) binds to
    # that requirement instead of allocating a new id. This is the first-release
    # / ingest path: the BRD ACs and the release notes describe the same set.
    # Only requirements recorded in an actual previous lock are un-bindable;
    # on a first release prev_reqs is derived from scenarios.json and every
    # current requirement is fair game.
    prev_ids = {r.get("id") for r in prev_reqs} if prev_lock is not None else set()
    bindable = [r for r in current_requirements() if r.get("id") and r["id"] not in prev_ids]
    bound_ids: set[str] = set()
    try:
        current_scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8")) if SCENARIOS_PATH.exists() else []
    except (OSError, json.JSONDecodeError):
        current_scenarios = []

    def _bind_add(text: str) -> tuple[dict | None, str, float]:
        ref, body = _split_ref(text)
        if ref:
            for r in bindable:
                if r["id"] not in bound_ids and (r.get("brd_ref") or "").lower() == ref.lower():
                    return r, body, 1.0
        best, score = None, 0.0
        for r in bindable:
            if r["id"] in bound_ids:
                continue
            sc = jaccard(body, r.get("description", ""))
            if sc > score:
                best, score = r, sc
        return (best, body, score) if best is not None and score >= threshold else (None, body, score)

    for item in items:
        if item.section == "Added":
            bound, body, score = _bind_add(item.text)
            if bound is not None:
                bound_ids.add(bound["id"])
                sc_ids = [s["id"] for s in current_scenarios
                          if s.get("requirement_id") == bound["id"] and s.get("status") != "deprecated"]
                result.operations.append(Operation(
                    op="ADD", item_text=body, item_section=item.section,
                    issue=item.issue, sc_id=(sc_ids[0] if sc_ids else None), requirement_id=bound["id"],
                    sc_ids=sc_ids, match_score=round(score, 3),
                    rationale=(f"binds existing requirement {bound['id']} ({bound.get('brd_ref') or 'no ref'}) — "
                               + (f"{len(sc_ids)} scenario(s) already mapped" if sc_ids else "no scenario yet (uncovered)")),
                ))
                continue
            ac_id = f"AC-{next_ac_index:03d}"
            next_ac_index += 1
            result.operations.append(Operation(
                op="ADD", item_text=body, item_section=item.section,
                issue=item.issue, sc_id=None, requirement_id=ac_id,
                match_score=0.0,
                rationale=(f"new requirement; will allocate {ac_id} and let Stage 1 record the asset"
                           if auto_create_scenarios() else
                           f"new requirement; will allocate {ac_id} (scenarios.auto_create=false → left uncovered)"),
            ))
        elif item.section in ("Changed", "Removed"):
            best, score = _best_match(item.text, targets, threshold)
            if best is None:
                result.unmatched_items.append({
                    "section": item.section, "text": item.text, "issue": item.issue,
                    "best_score": round(score, 3),
                    "reason": f"no requirement cleared similarity threshold {threshold}",
                })
                continue
            # The whole requirement is consumed: remove every candidate for it.
            targets[:] = [c for c in targets if c["requirement_id"] != best["requirement_id"]]
            sc_ids = [i for i in best["sc_ids"] if i]
            n = len(sc_ids)
            if item.section == "Changed":
                op, rationale = "EDIT", (
                    f"matched requirement on description similarity ({score:.2f}); EDIT will rewrite the "
                    f"requirement text and flag {n} scenario(s) review_required (generated scenarios re-record; "
                    f"ingested assets are never auto-rerecorded)")
            else:
                op, rationale = "DELETE", (
                    f"matched requirement on description similarity ({score:.2f}); will mark "
                    f"{n} scenario(s) deprecated (assets preserved)")
            result.operations.append(Operation(
                op=op, item_text=item.text, item_section=item.section,
                issue=item.issue, sc_id=(sc_ids[0] if sc_ids else None),
                requirement_id=best["requirement_id"], match_score=score,
                rationale=rationale, prev_text=best["text"] or None, sc_ids=sc_ids,
            ))
        # Fixed / Deprecated / Security: noted in markdown but no scenario op.
    return result


# ── Application ────────────────────────────────────────────────────────────
def _requirements_for_lock(prev_lock: Path | None, scenarios: list[dict]) -> list[dict]:
    """Base requirement list for the new lock: previous lock entries (win on
    description/status) merged with the current release source
    (analyzed_requirements.json / .txt), plus any requirement referenced
    only by a scenario."""
    prev_reqs, _, has_reqs, _ = _load_lock_for_diff(prev_lock)
    merged: dict[str, dict] = {}
    for r in (prev_reqs if has_reqs else []):
        if r.get("id"):
            merged[r["id"]] = dict(r)
    for r in current_requirements():
        rid = r.get("id")
        if not rid:
            continue
        if rid in merged:
            if not merged[rid].get("brd_ref") and r.get("brd_ref"):
                merged[rid]["brd_ref"] = r["brd_ref"]
            if not merged[rid].get("description"):
                merged[rid]["description"] = r.get("description", "")
        else:
            merged[rid] = {"id": rid, "brd_ref": r.get("brd_ref"), "description": r.get("description", "")}
    for r in _requirements_from_scenarios(scenarios):
        merged.setdefault(r["id"], r)
    return sorted(merged.values(), key=lambda r: _ac_num(r["id"]))


def _kane_objective(text: str, requirement_id: str | None = None, sc_id: str | None = None) -> str:
    """Same form agent.py::sync_scenarios uses. Kane runs a stored objective
    verbatim, so without the AUT URL it lands on its own playground site."""
    target = (os.environ.get("TARGET_URL") or cfg("target_url", "") or "").strip()
    start = session_start_url(requirement_id, sc_id, target)
    return f"On {start} — {text}" if start else text


def apply(result: DiffResult, prev_lock: Path | None = None) -> dict:
    """Mutate scenarios.json per the operations list and write the new lock."""
    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    today = datetime.now(timezone.utc).date().isoformat()
    summary = {"ADD": 0, "EDIT": 0, "DELETE": 0, "SCENARIOS_CREATED": 0,
               "SCENARIOS_FLAGGED": 0, "SCENARIOS_DEPRECATED": 0, "UNCOVERED": 0}
    requirements = _requirements_for_lock(prev_lock, scenarios)
    req_by_id = {r["id"]: r for r in requirements}
    next_sc_n = max(
        (int(re.match(r"SC-(\d+)", s["id"]).group(1))  # type: ignore[union-attr]
         for s in scenarios if re.match(r"SC-\d+", s["id"])),
        default=0,
    ) + 1

    def _scenarios_of(op: Operation) -> list[dict]:
        wanted = set(op.sc_ids)
        return [s for s in scenarios
                if s.get("requirement_id") == op.requirement_id or s.get("id") in wanted]

    def _ensure_req(op: Operation) -> dict:
        req = req_by_id.get(op.requirement_id)
        if req is None:
            req = {"id": op.requirement_id, "brd_ref": None, "description": op.item_text}
            requirements.append(req)
            req_by_id[op.requirement_id] = req
        return req

    for op in result.operations:
        if op.op == "ADD":
            existed = op.requirement_id in req_by_id
            req = _ensure_req(op)
            if not existed or not req.get("description"):
                req["description"] = op.item_text
            req.pop("status", None)
            req["added_in_release"] = result.to_release
            summary["ADD"] += 1
            if _scenarios_of(op):
                # Bound to a requirement that already has scenarios (ingested baseline) — nothing to create.
                continue
            if not auto_create_scenarios():
                print(f"[release_diff] {op.requirement_id}: scenarios.auto_create=false — requirement left uncovered")
                summary["UNCOVERED"] += 1
                continue
            sc_id = f"SC-{next_sc_n:03d}"
            next_sc_n += 1
            scenarios.append({
                "id": sc_id,
                "test_case_id": sc_id.replace("SC-", "TC-"),
                "requirement_id": op.requirement_id,
                "brd_ref": req.get("brd_ref"),
                "title": scenario_title(op.item_text),
                "description": op.item_text,
                "feature": classify_feature(op.item_text),
                "status": "new",
                # Stage 2 keeps this status for one sync so the next incremental
                # run executes it (then clears the flag).
                "pending_rerun": True,
                "kane_objective": _kane_objective(op.item_text, op.requirement_id, sc_id),
                "added_in_release": result.to_release,
                "added_at": today,
                "issue": op.issue,
            })
            summary["SCENARIOS_CREATED"] += 1
        elif op.op == "EDIT" and op.requirement_id:
            req = _ensure_req(op)
            req["description"] = op.item_text
            req["last_changed_in_release"] = result.to_release
            for sc in _scenarios_of(op):
                if sc.get("status") == "deprecated":
                    continue
                sc["review_required"] = True
                sc["review_reason"] = f"requirement {op.requirement_id} changed in {result.to_release}: {op.item_text}"
                sc["last_changed_in_release"] = result.to_release
                sc["last_changed_at"] = today
                if op.issue:
                    sc.setdefault("issues", []).append(op.issue)
                if not is_ingested(sc):
                    # Generated scenario: rewrite the description so the
                    # replay policy's hash drifts and Stage 1 re-records.
                    # Ingested assets are never auto-rerecorded.
                    sc["description"] = op.item_text
                    sc["source_description"] = op.item_text
                    sc["kane_objective"] = _kane_objective(op.item_text, op.requirement_id, sc.get("id"))
                    sc["status"] = "updated"
                    sc["pending_rerun"] = True
                summary["SCENARIOS_FLAGGED"] += 1
            summary["EDIT"] += 1
        elif op.op == "DELETE" and op.requirement_id:
            req = req_by_id.get(op.requirement_id)
            if req is not None:
                req["status"] = "deprecated"
                req["deprecated_in_release"] = result.to_release
            for sc in _scenarios_of(op):
                if sc.get("status") == "deprecated":
                    continue
                sc["status"] = "deprecated"
                sc["deprecated_in_release"] = result.to_release
                sc["deprecated_at"] = today
                if op.issue:
                    sc.setdefault("issues", []).append(op.issue)
                summary["SCENARIOS_DEPRECATED"] += 1
            summary["DELETE"] += 1

    SCENARIOS_PATH.write_text(json.dumps(scenarios, indent=2) + "\n", encoding="utf-8")

    # Write the lock file for the new release.
    lock_path = RELEASE_DIR / f"{result.to_release}.lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({
            "release": result.to_release,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "released_at": today,
            "from_release": result.from_release,
            "delta_summary": result.summary,
            "requirements": requirements,
            "scenarios": scenarios,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"summary": summary, "lock_path": str(lock_path.relative_to(REPO_ROOT))}


# ── Reporting ──────────────────────────────────────────────────────────────
def write_reports(result: DiffResult, *, applied: bool, applied_summary: dict | None = None) -> None:
    DELTA_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["applied"] = applied
    if applied_summary:
        payload["applied_summary"] = applied_summary
    DELTA_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines: list[str] = [
        f"# Release Delta — {result.from_release} → {result.to_release}",
        "",
        f"_Generated: {result.generated_at}_",
        f"_Match threshold: {result.threshold}_",
        f"_Mode: {'APPLIED' if applied else 'PROPOSE (no mutations)'}_",
        "",
        "## Summary",
        "",
        f"- ADD: **{result.summary['ADD']}**",
        f"- EDIT: **{result.summary['EDIT']}**",
        f"- DELETE: **{result.summary['DELETE']}**",
        f"- UNMATCHED: **{result.summary['UNMATCHED']}**",
        "",
    ]
    if result.operations:
        lines += [
            "## Operations",
            "",
            "| Op | Req | Scenarios | Issue | Score | Item |",
            "|---|---|---|---|---|---|",
        ]
        for op in result.operations:
            scs = ", ".join(f"`{i}`" for i in op.sc_ids) or "—"
            lines.append(
                f"| **{op.op}** | `{op.requirement_id or '—'}` | {scs} | "
                f"{op.issue or '—'} | {op.match_score:.2f} | {op.item_text} |"
            )
        lines.append("")
    if result.unmatched_items:
        lines += [
            "## Unmatched items (require manual requirement assignment)",
            "",
            "| Section | Best score | Reason | Item |",
            "|---|---|---|---|",
        ]
        for u in result.unmatched_items:
            lines.append(
                f"| {u['section']} | {u['best_score']:.2f} | {u['reason']} | {u['text']} |"
            )
        lines.append("")
    DELTA_MD.write_text("\n".join(lines), encoding="utf-8")


# ── CLI ────────────────────────────────────────────────────────────────────
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agentic Release Notes — Stage 0")
    p.add_argument("--notes", type=Path, help="Explicit release notes file (default: latest under release_notes/)")
    p.add_argument("--threshold", type=float, default=DEFAULT_MATCH_THRESHOLD,
                   help=f"Jaccard similarity threshold for matching (default {DEFAULT_MATCH_THRESHOLD})")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--propose", action="store_true",
                     help="Default: write reports/release_delta.{json,md}; mutate nothing")
    grp.add_argument("--apply", action="store_true",
                     help="Apply operations to scenarios.json + write the new lock file")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print_stage_header("0", "RELEASE_DIFF",
                       "Compute Add/Edit/Delete operations from release notes diff")
    try:
        notes, prev_lock = resolve_release_pair(args.notes)
    except FileNotFoundError as exc:
        print(f"[release_diff] {exc}", file=sys.stderr)
        # Nothing to propose is not a failure; nothing to apply is.
        return 1 if args.apply else 0

    print(
        f"[release_diff] notes={_rel(notes)} "
        f"prev_lock={_rel(prev_lock) if prev_lock else '<none — first release>'} "
        f"threshold={args.threshold}"
    )

    result = diff(notes, prev_lock, threshold=args.threshold)
    applied = bool(args.apply)
    applied_summary: dict | None = None
    if applied:
        applied_summary = apply(result, prev_lock)
    write_reports(result, applied=applied, applied_summary=applied_summary)

    print_stage_result("0", "RELEASE_DIFF", {
        "From release":   result.from_release,
        "To release":     result.to_release,
        "ADD":            result.summary["ADD"],
        "EDIT":           result.summary["EDIT"],
        "DELETE":         result.summary["DELETE"],
        "UNMATCHED":      result.summary["UNMATCHED"],
        "Mode":           "APPLIED" if applied else "PROPOSE",
        "Delta JSON":     str(DELTA_JSON.relative_to(REPO_ROOT)),
        "Delta MD":       str(DELTA_MD.relative_to(REPO_ROOT)),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
