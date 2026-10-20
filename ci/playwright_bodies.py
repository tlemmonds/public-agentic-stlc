"""Deterministic Playwright bodies for the regression layer.

Used when `regression.source: templates` (agentic-stlc.config.yaml). Each body
implements one acceptance criterion with real selectors and Playwright `expect`
checks, so the HyperExecute run has no vision AI in its pass/fail decision. Kane
still verifies every criterion in Stage 1 and records it in Test Manager.

Bring your own AUT: replace the example below with one body per scenario
(SC-xxx in scenarios/scenarios.json), using selectors from your application.
A scenario without a body generates a test that fails with "No regression
template", so coverage gaps are visible instead of silently passing.

Bodies are `str.format`-ed with `url` only (the scenario's AUT URL), so they
contain no other braces. Indent each body by four spaces: it becomes the body
of `def test_...(page):` in tests/playwright/test_regression.py.
"""

# Prepended to tests/playwright/test_regression.py after the generated header.
# Put shared helpers for your AUT here (login, add_item, row lookup, ...).
HELPERS = '''
import re
'''

BODIES: dict[str, str] = {
    # Example for the sample criterion in requirements/acceptance_criteria.txt
    # (AC-001 → SC-001). Replace it with bodies for your own scenarios.
    "SC-001": '''\
    page.goto("{url}")
    expect(page.get_by_role("heading", level=1)).to_have_text("Example Domain")''',
}
