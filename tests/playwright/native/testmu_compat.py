"""Yes/No ↔ true/false equivalence for testmu assertion checks. Framework-owned.

Kane records a yes/no check with `expected_value` "true" or "false", but at run
time the SDK's vision query answers the same question in words ("Yes", "No"). The
SDK's deterministic check is an exact string `equals` with no transforms, so a
correct page fails ("Yes" != "true"). Seen on every boolean check in HyperExecute
job #28160 (testmuai-playwright-bindings 0.1.48).

This wraps the SDK's sub-check resolver and rewrites only two cases: expected is
exactly "true"/"false" and the answer is a yes/no word; or the answer equals the
expected text apart from letter case (e.g. CSS that renders a chip in capitals,
"HIGH", for the value "high"). Everything else stays an exact compare.
Each rewrite is logged:

    [testmu_compat] normalized 'Yes' -> 'true' (pay_invoice_row_visible)

run_with_junit.py imports this from the per-run copy of test.py; the export
itself is never edited. Off with kaneai.yes_no_compat: false.
"""
from __future__ import annotations

import functools

_YES = {"yes", "y", "true", "present", "visible"}
_NO = {"no", "n", "false", "absent", "not visible"}


def normalize(expected: str, extracted: str) -> str:
    """The extracted value to compare: a yes/no word becomes "true"/"false" when
    the expected value is a boolean literal; anything else is returned as is."""
    exp = (expected or "").strip().lower()
    got = (extracted or "").strip().strip(".").lower()
    if exp not in ("true", "false"):
        # Text checks compare case-insensitively: the vision answer reads the
        # rendered text, and CSS can change its case ("HIGH" for the value "high").
        return expected if exp and got == exp else extracted
    if got in _YES:
        return "true"
    if got in _NO:
        return "false"
    return extracted


def _wrap(module) -> bool:
    original = getattr(module, "_resolve_sub_checks", None)
    if original is None or getattr(original, "_yes_no_compat", False):
        return False

    @functools.wraps(original)
    async def resolve(sub_checks):
        resolved = await original(sub_checks)
        for check in resolved:
            before = check.get("extracted_value", "")
            after = normalize(check.get("expected_value", ""), before)
            if after != before:
                print(f"[testmu_compat] normalized {before!r} -> {after!r} "
                      f"({check.get('description', '')[:60]})", flush=True)
                check["extracted_value"] = after
        return resolved

    resolve._yes_no_compat = True
    module._resolve_sub_checks = resolve
    return True


def install() -> int:
    """Patch every SDK assertion module present. Returns how many were patched."""
    patched = 0
    for name in ("testmu._v4._helpers.assertion", "testmu._v3._helpers.assertion"):
        try:
            module = __import__(name, fromlist=["_resolve_sub_checks"])
        except ImportError:
            continue
        patched += _wrap(module)
    print(f"[testmu_compat] yes/no equivalence on ({patched} assertion module(s))", flush=True)
    return patched


install()
