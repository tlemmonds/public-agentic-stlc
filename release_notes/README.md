# Release notes

One Keep-a-Changelog file per release, named `vX.Y.Z.md`. `ci/release_diff.py`
diffs the newest file against the previous `vX.Y.Z.lock.json`.

```markdown
# v1.2.0 — 2026-10-17

### Added
- [BR-1.1] Creating a task with a title adds it to the list as active (#101)

### Changed
- ...

### Removed
- ...
```

- `python ci/release_diff.py --propose` writes `reports/release_delta.{json,md}` and changes nothing.
- `python ci/release_diff.py --apply` updates `scenarios/scenarios.json` and writes `vX.Y.Z.lock.json`.
- After a run, `ci/agent.py` archives that release's reports to `vX.Y.Z/reports/`.

This README is ignored by the diff engine.
