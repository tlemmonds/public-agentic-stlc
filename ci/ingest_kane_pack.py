"""
Stage 0i — INGEST_KANE_PACK

Walks an existing Kane CLI pack (`<Folder>/<Group>/<stem>_test.md` plus the
sibling `output-<stem>/` TMS sidecar directory), copies each pair VERBATIM
into `tests/kane/<feature>/`, writes the pipeline sidecar
`<stem>_test.meta.json`, and appends one scenario record per case to
`scenarios/scenarios.json` — many scenarios per requirement.

    python ci/ingest_kane_pack.py --source <dir> --map <yaml> [--apply]
                                  [--scenarios scenarios/scenarios.json]
                                  [--dest tests/kane] [--reports-dir reports]

Mapping yaml:

    target_url: https://example.test/
    folders:                       # source top-level folder → FEATURE
      Checkout: CHECKOUT
    cases:                         # glob on the POSIX source-relative path → requirement
      "Checkout/**":                        {requirement: AC-001, brd_ref: AC-01}
      "Account/Acct-Password-Reset/**":     {requirement: AC-003, brd_ref: AC-03}
    exclude:
      - "Checkout/Checkout-Experimental/**"

The most specific (longest) matching `cases` glob wins. Without --apply the
script proposes only: prints the scenario table + coverage report, writes
reports/ingest_report.{json,md}, and mutates nothing else. It never writes a
_test.md of its own — assets are copied byte-for-byte and keep their stem
(renaming the stem would orphan the TMS test case).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from stage_utils import print_stage_header, print_stage_result  # noqa: E402

try:  # the sidecar convention + hash live in replay_policy; keep a local fallback
    from replay_policy import sidecar_path_for  # noqa: E402
except ImportError:  # pragma: no cover
    def sidecar_path_for(asset_path: Path) -> Path:
        return asset_path.with_suffix(".meta.json")

try:
    from replay_policy import hash_asset_body  # noqa: E402
except ImportError:  # pragma: no cover
    def hash_asset_body(text: str) -> str:
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE_ID = "0i"
STAGE_NAME = "INGEST_KANE_PACK"
DESCRIPTION_MAX = 400
TEST_SUFFIX = "_test.md"
EXPORT_DIRNAME = "playwright-python-code"


# ── small helpers ────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit(f"ERROR: PyYAML is required to read the mapping file {path} (pip install pyyaml)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        sys.exit(f"ERROR: mapping file {path} must be a YAML mapping")
    return data


def _repo_rel(path: Path, fallback_base: Path | None = None) -> str:
    """Repo-relative POSIX path; when the path lives outside the repo (e.g. a
    --dest in a scratch dir) fall back to the path relative to the dest's
    grandparent (so `<scratch>/tests/kane/...` still reads `tests/kane/...`),
    else the absolute POSIX path."""
    path = path.resolve()
    try:
        return path.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        if fallback_base is not None:
            try:
                return path.relative_to(fallback_base.resolve().parent.parent).as_posix()
            except ValueError:
                pass
        return path.as_posix()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _match(pattern: str, rel_posix: str) -> bool:
    """fnmatch on the POSIX relative path. `*` in fnmatch crosses `/`, so
    `Folder/**` matches any depth; also accept a bare directory prefix."""
    if fnmatch.fnmatchcase(rel_posix, pattern):
        return True
    if pattern.endswith("/**"):
        return rel_posix.startswith(pattern[:-2])
    return False


def _next_id_number(scenarios: list[dict]) -> int:
    top = 0
    for sc in scenarios:
        for key in ("id", "test_case_id"):
            m = re.fullmatch(r"(?:SC|TC)-(\d+)", str((sc or {}).get(key, "")))
            if m:
                top = max(top, int(m.group(1)))
    return top + 1


# ── asset parsing ────────────────────────────────────────────────────────────

def parse_asset(text: str) -> dict:
    """Frontmatter (YAML between the leading `---` lines), H1 title and the
    first paragraph after it."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    frontmatter: dict = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                raw = "\n".join(lines[1:i])
                try:
                    import yaml  # type: ignore
                    fm = yaml.safe_load(raw) or {}
                    frontmatter = fm if isinstance(fm, dict) else {}
                except Exception:
                    frontmatter = {}
                body_start = i + 1
                break
    title = ""
    paragraph: list[str] = []
    i = body_start
    while i < len(lines):
        if lines[i].startswith("# "):
            title = lines[i][2:].strip()
            i += 1
            break
        i += 1
    if title:
        while i < len(lines) and not lines[i].strip():
            i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith("#"):
            paragraph.append(lines[i].strip())
            i += 1
    tags_raw = frontmatter.get("tags") or []
    if isinstance(tags_raw, str):
        tags_raw = [t.strip() for t in tags_raw.split(",")]
    tags = [str(t) for t in tags_raw if str(t).strip()]
    return {
        "frontmatter": frontmatter,
        "title": title,
        "first_paragraph": _collapse(" ".join(paragraph)),
        "tags": tags,
    }


def build_description(title: str, paragraph: str) -> str:
    desc = _collapse(f"{title} — {paragraph}" if paragraph else title)
    if len(desc) > DESCRIPTION_MAX:
        desc = desc[:DESCRIPTION_MAX - 1].rstrip() + "…"
    return desc


def find_meta_json(output_dir: Path) -> Path | None:
    """Kane CLI writes `output-<stem>/meta.json` (contract) — some CLI versions
    nest it under `.internal/`. Accept either."""
    for cand in (output_dir / "meta.json", output_dir / ".internal" / "meta.json"):
        if cand.is_file():
            return cand
    return None


def last_execution_date(meta: dict) -> str | None:
    execs = meta.get("executions") or []
    stamps = [str(e.get("at")) for e in execs if isinstance(e, dict) and e.get("at")]
    if not stamps:
        return None
    return max(stamps)[:10]


# ── requirement universe (for the coverage report) ───────────────────────────

_AC_LINE = re.compile(r"^\s*(?:\[(?P<ref>[^\]]+)\]\s*)?(?P<text>.+?)\s*$")


def load_requirement_universe(requirements_dir: Path) -> list[dict]:
    """[{id, brd_ref, description}] from analyzed_requirements.json when present,
    else from the `Acceptance Criteria:` block(s) of requirements/*.txt."""
    analyzed = requirements_dir / "analyzed_requirements.json"
    if analyzed.is_file():
        try:
            data = json.loads(analyzed.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        if isinstance(data, dict):
            data = data.get("requirements") or []
        out = []
        for r in data:
            if isinstance(r, dict) and r.get("id"):
                out.append({"id": r["id"], "brd_ref": r.get("brd_ref"),
                            "description": _collapse(str(r.get("description") or r.get("title") or ""))})
        if out:
            return out
    out = []
    n = 0
    for txt in sorted(requirements_dir.glob("*.txt")):
        in_block = False
        for line in txt.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^\s*acceptance criteria\s*:\s*$", line, re.I):
                in_block = True
                continue
            if not in_block:
                continue
            if not line.strip():
                if out and out[-1].get("_file") == txt.name:
                    in_block = False  # blank line ends the block once it has content
                continue
            if re.match(r"^\s*[A-Za-z][\w ]*:\s*$", line):
                in_block = False
                continue
            m = _AC_LINE.match(line)
            if not m or not m.group("text"):
                continue
            n += 1
            text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", m.group("text"))
            out.append({"id": f"AC-{n:03d}", "brd_ref": m.group("ref"),
                        "description": _collapse(text), "_file": txt.name})
    for r in out:
        r.pop("_file", None)
    return out


# ── discovery + mapping ──────────────────────────────────────────────────────

def resolve_case_mapping(rel_posix: str, cases: dict) -> tuple[str | None, dict | None]:
    best: tuple[str, dict] | None = None
    for pattern, spec in cases.items():
        if _match(str(pattern), rel_posix):
            if best is None or len(str(pattern)) > len(best[0]):
                best = (str(pattern), spec or {})
    return (best[0], best[1]) if best else (None, None)


def discover(source: Path, mapping: dict, exports_dir: Path | None = None) -> dict:
    folders: dict = mapping.get("folders") or {}
    cases: dict = mapping.get("cases") or {}
    excludes: list = [str(x) for x in (mapping.get("exclude") or [])]

    cases_out: list[dict] = []
    skipped_no_sidecar: list[str] = []
    excluded: list[str] = []
    unmapped: list[str] = []
    unmapped_folders: list[str] = []

    for asset in sorted(source.rglob(f"*{TEST_SUFFIX}"), key=lambda p: p.relative_to(source).as_posix()):
        rel = asset.relative_to(source).as_posix()
        if any(_match(x, rel) for x in excludes):
            excluded.append(rel)
            continue
        stem = asset.name[: -len(TEST_SUFFIX)]
        output_dir = asset.parent / f"output-{stem}"
        meta_path = find_meta_json(output_dir)
        if meta_path is None:
            skipped_no_sidecar.append(rel)
            continue
        top = rel.split("/", 1)[0]
        feature = folders.get(top)
        if not feature:
            unmapped_folders.append(rel)
            continue
        pattern, spec = resolve_case_mapping(rel, cases)
        if spec is None or not spec.get("requirement"):
            unmapped.append(rel)
            continue

        text = asset.read_text(encoding="utf-8", errors="replace")
        parsed = parse_asset(text)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        export_dir = output_dir / EXPORT_DIRNAME
        has_export = (export_dir / "test.py").is_file()
        export_external = False
        # Fallback: packs exported with `kane-cli testmd export` keep the Playwright
        # code in a separate tree (<exports_dir>/<stem>/test.py), not in output-<stem>/.
        if not has_export and exports_dir is not None:
            alt = exports_dir / stem
            if (alt / "test.py").is_file():
                export_dir, has_export, export_external = alt, True, True
        title = parsed["title"] or stem
        cases_out.append({
            "rel": rel,
            "asset_path": asset,
            "output_dir": output_dir,
            "stem": stem,
            "feature": str(feature).upper(),
            "requirement_id": str(spec["requirement"]),
            "brd_ref": spec.get("brd_ref"),
            "matched_pattern": pattern,
            "title": title,
            "description": build_description(title, parsed["first_paragraph"]),
            "tags": parsed["tags"],
            "kane_testcase_id": meta.get("testcase_id"),
            "kane_folder_id": meta.get("folder_id"),
            "kane_session_name": meta.get("session_name") or stem,
            "last_verified": last_execution_date(meta),
            "export_dir": export_dir if has_export else None,
            "export_external": export_external,
            "asset_hash": hash_asset_body(text),
        })

    if unmapped_folders:
        tops = sorted({r.split("/", 1)[0] for r in unmapped_folders})
        sys.exit(f"ERROR: source folder(s) not mapped under `folders:` in the map file: {', '.join(tops)}")

    return {
        "cases": cases_out,
        "skipped_no_sidecar": skipped_no_sidecar,
        "excluded": excluded,
        "unmapped": unmapped,
    }


# ── record building ──────────────────────────────────────────────────────────

def build_records(cases: list[dict], existing: list[dict], dest: Path, target_url: str) -> tuple[list[dict], list[dict]]:
    """(new_records, already_ingested). Ids continue from max SC-NNN; a case
    whose kane_testcase_id is already present in scenarios.json is skipped."""
    known_tc = {str(sc.get("kane_testcase_id")) for sc in existing if sc.get("kane_testcase_id")}
    n = _next_id_number(existing)
    new: list[dict] = []
    already: list[dict] = []
    for c in cases:
        if c["kane_testcase_id"] and str(c["kane_testcase_id"]) in known_tc:
            already.append(c)
            continue
        feat_dir = dest / c["feature"].lower()
        asset_dest = feat_dir / f"{c['stem']}{TEST_SUFFIX}"
        export_dest = feat_dir / f"output-{c['stem']}" / EXPORT_DIRNAME if c["export_dir"] else None
        sc_id = f"SC-{n:03d}"
        rec = {
            "id": sc_id,
            "test_case_id": f"TC-{n:03d}",
            "requirement_id": c["requirement_id"],
            "brd_ref": c["brd_ref"],
            "feature": c["feature"],
            "title": c["title"],
            "description": c["description"],
            "status": "new",
            "source": "ingested",
            "kane_asset": _repo_rel(asset_dest, dest),
            "kane_testcase_id": c["kane_testcase_id"],
            "kane_folder_id": c["kane_folder_id"],
            "kane_session_name": c["kane_session_name"],
            "tags": list(c["tags"]),
            "export_kind": "testmu" if export_dest else "vanilla",
            "playwright_export": _repo_rel(export_dest, dest) if export_dest else None,
            "function_name": None,
            "kane_url": target_url,
            "kane_objective": c["title"],
            "last_verified": c["last_verified"],
        }
        c["record"] = rec
        c["asset_dest"] = asset_dest
        c["output_dest"] = feat_dir / f"output-{c['stem']}"
        new.append(rec)
        n += 1
    return new, already


def sidecar_payload(c: dict, source: Path) -> dict:
    rec = c["record"]
    return {
        "sc_id": rec["id"],
        "requirement_id": rec["requirement_id"],
        "feature": rec["feature"],
        "title": rec["title"],
        "description_hash": c["asset_hash"],
        "hash_source": "asset",
        "ingested_from": c["rel"],
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kane_testcase_id": rec["kane_testcase_id"],
    }


# ── coverage report ──────────────────────────────────────────────────────────

def coverage(mapping: dict, universe: list[dict], existing: list[dict], new: list[dict]) -> list[dict]:
    """One row per requirement: the universe (requirements files) ∪ ids
    referenced by the map's `cases`, with scenario ids (existing + proposed)."""
    rows: dict[str, dict] = {}
    for r in universe:
        rows[r["id"]] = {"requirement_id": r["id"], "brd_ref": r.get("brd_ref"),
                         "description": r.get("description", ""), "scenario_ids": []}
    for spec in (mapping.get("cases") or {}).values():
        rid = str((spec or {}).get("requirement") or "")
        if rid and rid not in rows:
            rows[rid] = {"requirement_id": rid, "brd_ref": (spec or {}).get("brd_ref"),
                         "description": "", "scenario_ids": []}
        elif rid and not rows[rid]["brd_ref"]:
            rows[rid]["brd_ref"] = (spec or {}).get("brd_ref")
    for sc in list(existing) + list(new):
        rid = sc.get("requirement_id")
        if not rid or sc.get("status") == "deprecated":
            continue
        rows.setdefault(rid, {"requirement_id": rid, "brd_ref": sc.get("brd_ref"),
                              "description": "", "scenario_ids": []})
        rows[rid]["scenario_ids"].append(sc["id"])

    def _key(rid: str):
        m = re.fullmatch(r"([A-Za-z]+)-(\d+)", rid)
        return (m.group(1), int(m.group(2))) if m else (rid, 0)
    return [rows[k] for k in sorted(rows, key=_key)]


# ── output ───────────────────────────────────────────────────────────────────

def _print_report(disc: dict, new: list[dict], already: list[dict], cov: list[dict], apply: bool) -> None:
    mode = "APPLY" if apply else "PROPOSE"
    print(f"\n[{mode}] Proposed scenarios ({len(new)}):")
    print(f"  {'SC':7} {'Requirement':12} {'brd_ref':8} {'Feature':12} {'Export':8} Asset")
    for rec in new:
        print(f"  {rec['id']:7} {rec['requirement_id']:12} {str(rec['brd_ref'] or '-'):8} "
              f"{rec['feature']:12} {rec['export_kind']:8} {rec['kane_asset']}")
    if already:
        print(f"\nAlready ingested — not re-added ({len(already)}):")
        for c in already:
            print(f"  {c['rel']}  (kane_testcase_id={c['kane_testcase_id']})")
    if disc["unmapped"]:
        print(f"\nUNMAPPED — not ingested ({len(disc['unmapped'])}): no `cases:` glob matched")
        for rel in disc["unmapped"]:
            print(f"  {rel}")
    if disc["skipped_no_sidecar"]:
        print(f"\nno TMS sidecar — skipped ({len(disc['skipped_no_sidecar'])}):")
        for rel in disc["skipped_no_sidecar"]:
            print(f"  {rel}")
    if disc["excluded"]:
        print(f"\nExcluded by map ({len(disc['excluded'])}):")
        for rel in disc["excluded"]:
            print(f"  {rel}")

    print("\nRequirement coverage:")
    print(f"  {'Requirement':12} {'brd_ref':8} {'#':>3}  scenarios")
    for row in cov:
        ids = ", ".join(row["scenario_ids"]) or "-"
        print(f"  {row['requirement_id']:12} {str(row['brd_ref'] or '-'):8} {len(row['scenario_ids']):>3}  {ids}")
    uncovered = [r for r in cov if not r["scenario_ids"]]
    print(f"\n**UNCOVERED REQUIREMENTS** ({len(uncovered)}):")
    if not uncovered:
        print("  (none)")
    for r in uncovered:
        desc = f" — {r['description']}" if r.get("description") else ""
        print(f"  {r['requirement_id']} ({r['brd_ref'] or 'no brd_ref'}){desc}")


def _write_reports(reports_dir: Path, payload: dict) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    jp = reports_dir / "ingest_report.json"
    mp = reports_dir / "ingest_report.md"
    jp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    md = [f"# Kane pack ingest report — {'APPLY' if payload['apply'] else 'PROPOSE'}", "",
          f"- Source: `{payload['source']}`", f"- Map: `{payload['map']}`",
          f"- Generated: {payload['generated_at']}",
          f"- Proposed/added: **{len(payload['scenarios'])}**, already ingested: {len(payload['already_ingested'])}, "
          f"unmapped: {len(payload['unmapped'])}, no sidecar: {len(payload['skipped_no_sidecar'])}, "
          f"excluded: {len(payload['excluded'])}", "",
          "## Scenarios", "", "| SC | Requirement | brd_ref | Feature | Export | Title | Asset |", "|---|---|---|---|---|---|---|"]
    for rec in payload["scenarios"]:
        title = rec["title"].replace("|", "\\|")
        md.append(f"| {rec['id']} | {rec['requirement_id']} | {rec['brd_ref'] or '-'} | {rec['feature']} | "
                  f"{rec['export_kind']} | {title} | `{rec['kane_asset']}` |")
    md += ["", "## Requirement coverage", "", "| Requirement | brd_ref | scenarios | ids |", "|---|---|---|---|"]
    for row in payload["coverage"]:
        md.append(f"| {row['requirement_id']} | {row['brd_ref'] or '-'} | {len(row['scenario_ids'])} | "
                  f"{', '.join(row['scenario_ids']) or '-'} |")
    md += ["", "## UNCOVERED REQUIREMENTS", ""]
    md += [f"- **{r['requirement_id']}** ({r['brd_ref'] or 'no brd_ref'}) {r.get('description', '')}".rstrip()
           for r in payload["uncovered_requirements"]] or ["- (none)"]
    for label, key in (("Already ingested", "already_ingested"), ("UNMAPPED — not ingested", "unmapped"),
                       ("No TMS sidecar — skipped", "skipped_no_sidecar"), ("Excluded", "excluded")):
        items = payload[key]
        if items:
            md += ["", f"## {label}", ""] + [f"- `{x if isinstance(x, str) else x['rel']}`" for x in items]
    mp.write_text("\n".join(md) + "\n", encoding="utf-8")
    return jp, mp


# ── apply ────────────────────────────────────────────────────────────────────

def apply_changes(cases: list[dict], new: list[dict], existing: list[dict], scenarios_path: Path, source: Path) -> int:
    copied = 0
    for c in cases:
        if "record" not in c:
            continue
        c["asset_dest"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c["asset_path"], c["asset_dest"])
        if c["output_dest"].exists():
            shutil.rmtree(c["output_dest"])
        shutil.copytree(c["output_dir"], c["output_dest"])
        if c.get("export_external") and c.get("export_dir"):
            # External export → place it where the contract expects it (output-<stem>/playwright-python-code)
            shutil.copytree(c["export_dir"], c["output_dest"] / EXPORT_DIRNAME, dirs_exist_ok=True)
        sidecar = sidecar_path_for(c["asset_dest"])
        sidecar.write_text(json.dumps(sidecar_payload(c, source), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        copied += 1
    scenarios_path.parent.mkdir(parents=True, exist_ok=True)
    scenarios_path.write_text(json.dumps(existing + new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return copied


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest an existing Kane CLI pack into tests/kane + scenarios.json")
    ap.add_argument("--source", required=True, help="Kane pack root (contains <Folder>/<Group>/<stem>_test.md)")
    ap.add_argument("--map", required=True, help="mapping yaml (target_url, folders, cases, exclude)")
    ap.add_argument("--apply", action="store_true", help="perform copies, sidecars and the scenarios.json append")
    ap.add_argument("--scenarios", default=str(REPO_ROOT / "scenarios" / "scenarios.json"))
    ap.add_argument("--dest", default=str(REPO_ROOT / "tests" / "kane"))
    ap.add_argument("--exports-dir", default=None,
                    help="fallback tree of `kane-cli testmd export` outputs (<dir>/<stem>/test.py) for cases whose output-<stem>/ has no playwright-python-code")
    ap.add_argument("--reports-dir", default=str(REPO_ROOT / "reports"))
    ap.add_argument("--requirements-dir", default=str(REPO_ROOT / "requirements"))
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):  # UTF-8 everywhere, even on a cp1252 console
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    source = Path(args.source).resolve()
    map_path = Path(args.map).resolve()
    scenarios_path = Path(args.scenarios)
    dest = Path(args.dest)
    reports_dir = Path(args.reports_dir)

    print_stage_header(STAGE_ID, STAGE_NAME,
                       f"{'apply' if args.apply else 'propose'}: {source} → {dest} via {map_path.name}")
    if not source.is_dir():
        sys.exit(f"ERROR: --source {source} is not a directory")
    if not map_path.is_file():
        sys.exit(f"ERROR: --map {map_path} not found")
    mapping = _load_yaml(map_path)
    target_url = str(mapping.get("target_url") or "")
    if not target_url:
        print("WARNING: map has no target_url; kane_url will be empty")

    existing: list[dict] = []
    if scenarios_path.is_file():
        loaded = json.loads(scenarios_path.read_text(encoding="utf-8"))
        existing = loaded if isinstance(loaded, list) else []

    exports_dir = Path(args.exports_dir).resolve() if args.exports_dir else None
    disc = discover(source, mapping, exports_dir)
    new, already = build_records(disc["cases"], existing, dest, target_url)
    universe = load_requirement_universe(Path(args.requirements_dir))
    cov = coverage(mapping, universe, existing, new)
    uncovered = [r for r in cov if not r["scenario_ids"]]

    _print_report(disc, new, already, cov, args.apply)

    copied = 0
    if args.apply:
        copied = apply_changes(disc["cases"], new, existing, scenarios_path, source)
        print(f"\nCopied {copied} asset pair(s) into {dest}; scenarios.json now has {len(existing) + len(new)} records")

    payload = {
        "stage": STAGE_ID, "apply": args.apply,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": source.as_posix(), "map": map_path.as_posix(),
        "scenarios_file": scenarios_path.as_posix(), "dest": dest.as_posix(),
        "scenarios": new,
        "already_ingested": [{"rel": c["rel"], "kane_testcase_id": c["kane_testcase_id"]} for c in already],
        "unmapped": disc["unmapped"], "skipped_no_sidecar": disc["skipped_no_sidecar"], "excluded": disc["excluded"],
        "coverage": cov, "uncovered_requirements": uncovered,
        "copied": copied,
    }
    jp, mp = _write_reports(reports_dir, payload)

    by_feature: dict[str, int] = {}
    for rec in new:
        by_feature[rec["feature"]] = by_feature.get(rec["feature"], 0) + 1
    print_stage_result(STAGE_ID, STAGE_NAME, {
        "Mode": "apply" if args.apply else "propose (nothing copied)",
        "Scenarios proposed/added": f"{len(new)} {dict(sorted(by_feature.items()))}",
        "Already ingested": len(already),
        "Unmapped": len(disc["unmapped"]),
        "No TMS sidecar": len(disc["skipped_no_sidecar"]),
        "Excluded": len(disc["excluded"]),
        "Requirements uncovered": f"{len(uncovered)} / {len(cov)}",
        "Report": f"{jp}, {mp}",
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
