"""Replay policy — decides per acceptance criterion whether Kane should
replay a saved _test.md asset, re-author it, or skip.

This is the heart of the Test.md migration: the pipeline becomes
"replay-first", paying Kane authoring cost only when a requirement actually
changes (description hash differs) or an operator explicitly forces it.

Invoked from Stage 1 (analyze_requirements) and Stage 3 (test generation).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_KANE_DIR = REPO_ROOT / "tests" / "kane"

Decision = Literal["replay", "record", "rerecord", "skip"]


@dataclass
class ReplayDecision:
    sc_id: str
    requirement_id: str
    decision: Decision
    asset_path: Path | None
    reason: str
    description_hash: str
    hash_source: str = "description"   # "description" | "asset"

    def to_dict(self) -> dict:
        return {
            "sc_id": self.sc_id,
            "requirement_id": self.requirement_id,
            "decision": self.decision,
            "asset_path": _rel(self.asset_path),
            "reason": self.reason,
            "description_hash": self.description_hash,
            "hash_source": self.hash_source,
        }


def _rel(path: Path | None) -> str | None:
    """Repo-relative string when inside the repo, else as-is. An ingested
    `kane_asset` override may legitimately resolve outside REPO_ROOT."""
    if path is None:
        return None
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def hash_description(description: str) -> str:
    """Stable short hash of an AC description. 12 hex chars is plenty for
    collision safety across a single project's requirement set."""
    normalized = re.sub(r"\s+", " ", description.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def hash_asset_body(text: str) -> str:
    """Stable short hash of a Kane _test.md body, for ingested scenarios
    (hash_source == "asset"): drift means *the test was edited*, not the AC.
    Line endings and trailing whitespace are normalized so a CRLF checkout
    or an editor's EOF newline does not register as drift."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    normalized = "\n".join(lines).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def asset_path_for(sc_id: str, feature: str, title: str) -> Path:
    """Convention: tests/kane/<feature_slug>/<sc_id_lower>_<title_slug>_test.md
    Feature slugs come from scenarios.json (CART, AUTH, …)."""
    feature_slug = (feature or "general").lower()
    title_slug = re.sub(r"[^a-z0-9]+", "_", (title or sc_id).lower()).strip("_")[:60]
    sc_slug = sc_id.lower().replace("-", "_")
    return TESTS_KANE_DIR / feature_slug / f"{sc_slug}_{title_slug}_test.md"


def existing_asset_path(sc_id: str) -> Path | None:
    """Find an existing asset for this SC by searching all feature subdirs.
    Allows the asset to be moved between feature folders without breaking
    the policy."""
    sc_prefix = f"{sc_id.lower().replace('-', '_')}_"
    if not TESTS_KANE_DIR.exists():
        return None
    for path in TESTS_KANE_DIR.rglob("*_test.md"):
        if path.parent.name == "helpers":
            continue
        if path.name.startswith(sc_prefix):
            return path
    return None


def sidecar_path_for(asset_path: Path) -> Path:
    """The pipeline's metadata sidecar lives alongside the Kane asset.
    Kane CLI 0.3.1's `testmd run` rejects unknown frontmatter keys, so the
    pipeline-specific fields (sc_id, description_hash, …) cannot live in
    the _test.md itself."""
    return asset_path.with_suffix(".meta.json")


def read_asset_hash(asset_path: Path) -> str | None:
    """Read the recorded description_hash from the asset's sidecar.
    Returns None if the sidecar is missing or malformed."""
    sidecar = sidecar_path_for(asset_path)
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("description_hash")
    return value if isinstance(value, str) and value else None


def decide(
    sc_id: str,
    requirement_id: str,
    description: str,
    feature: str,
    title: str,
    *,
    force_re_author: bool = False,
    deprecated: bool = False,
    asset_override: Path | None = None,
    hash_source: str = "description",
    asset_body: str | None = None,
    allow_record: bool = True,
) -> ReplayDecision:
    """Compute the replay policy decision for one scenario.

    Order:
      deprecated       → skip
      override missing → skip  (reason "asset_missing"; ingested assets are never recorded)
      missing          → record   (skip when allow_record is False)
      force flag       → rerecord (skip when allow_record is False)
      hash drift       → rerecord (skip when allow_record is False)
      otherwise        → replay

    asset_override: explicit `kane_asset` from scenarios.json. When set it IS
      the asset path — `asset_path_for()` naming is not applied.
    hash_source: "description" (legacy — hash of the AC text) or "asset"
      (hash of the asset file body). Stored in the sidecar under the same
      `description_hash` key, alongside `hash_source`.
    asset_body: optional pre-read asset text for hash_source == "asset";
      otherwise the file at the resolved asset path is read.
    allow_record: `kaneai.auto_record`. False turns every would-be record /
      rerecord into a skip so gaps are reported rather than filled.
    """
    hash_source = "asset" if hash_source == "asset" else "description"

    def _mk(decision: Decision, asset: Path | None, reason: str, h: str) -> ReplayDecision:
        return ReplayDecision(
            sc_id=sc_id, requirement_id=requirement_id, decision=decision,
            asset_path=asset, reason=reason, description_hash=h, hash_source=hash_source,
        )

    def _gate(decision: Decision, asset: Path | None, reason: str, h: str) -> ReplayDecision:
        """record / rerecord pass through only when recording is allowed."""
        if decision in ("record", "rerecord") and not allow_record:
            return _mk("skip", asset, f"auto_record disabled (would {decision}: {reason})", h)
        return _mk(decision, asset, reason, h)

    desc_hash = hash_description(description)

    if deprecated:
        return _mk("skip", None, "scenario marked deprecated", desc_hash)

    if asset_override is not None:
        asset: Path | None = asset_override
        if not asset_override.exists():
            return _mk("skip", asset_override, "asset_missing", desc_hash)
    else:
        asset = existing_asset_path(sc_id)
        if asset is None:
            target = asset_path_for(sc_id, feature, title)
            return _gate("record", target, "no asset on disk yet", desc_hash)

    # Comparison hash: AC text (legacy) or the asset body (ingested).
    if hash_source == "asset":
        body = asset_body
        if body is None:
            try:
                body = asset.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
        current_hash = hash_asset_body(body)
    else:
        current_hash = desc_hash

    if force_re_author:
        return _gate("rerecord", asset, "FORCE_RE_AUTHOR set", current_hash)

    recorded_hash = read_asset_hash(asset)
    if recorded_hash and recorded_hash != current_hash:
        label = "asset body hash" if hash_source == "asset" else "description hash"
        return _gate("rerecord", asset, f"{label} drifted ({recorded_hash} → {current_hash})", current_hash)

    return _mk("replay", asset, "hash matches recorded asset", current_hash)


def write_decisions_log(decisions: list[ReplayDecision], out_path: Path) -> None:
    """Persist decisions for auditability + downstream stages."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "force_re_author": os.environ.get("FORCE_RE_AUTHOR", "false").lower() == "true",
        "decisions": [d.to_dict() for d in decisions],
        "summary": {
            label: sum(1 for d in decisions if d.decision == label)
            for label in ("replay", "record", "rerecord", "skip")
        },
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """CLI entry: prints decisions for the current scenarios.json. Useful for
    pipeline operators to preview what the next run will do."""
    scenarios_path = REPO_ROOT / "scenarios" / "scenarios.json"
    if not scenarios_path.exists():
        print("scenarios.json not found", file=sys.stderr)
        return 1
    scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
    force = os.environ.get("FORCE_RE_AUTHOR", "false").lower() == "true"
    import project_config
    allow_record = project_config.auto_record()
    decisions = []
    for sc in scenarios:
        # scenarios.json has had two key names for the AC text over time:
        # `description` (current) and `source_description` (legacy from older
        # Stage 2 runs). Treat them interchangeably so the hash is stable.
        description = sc.get("description") or sc.get("source_description") or ""
        d = decide(
            sc_id=sc["id"],
            requirement_id=sc.get("requirement_id", ""),
            description=description,
            feature=sc.get("feature", "general"),
            title=sc.get("title", sc["id"]),
            force_re_author=force,
            deprecated=(sc.get("status") == "deprecated"),
            asset_override=project_config.scenario_asset_path(sc),
            hash_source="asset" if project_config.is_ingested(sc) else "description",
            allow_record=allow_record,
        )
        decisions.append(d)
        print(f"{d.sc_id}  {d.decision:9}  hash={d.description_hash}({d.hash_source})  {d.reason}")
    out = REPO_ROOT / "reports" / "replay_decisions.json"
    write_decisions_log(decisions, out)
    print(f"\nWrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
