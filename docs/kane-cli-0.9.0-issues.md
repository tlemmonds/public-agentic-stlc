# Kane CLI 0.9.0 known issues

Versions: `@testmuai/kane-cli` 0.9.0, `testmuai-playwright-bindings` 0.1.48, Playwright 1.57.
Grid: LambdaTest Playwright (chrome, pw-firefox).

Each issue lists what happens, how to reproduce it, and the workaround this repo uses (if any).
Issues 1 and 2 are in the code export. Issues 3–6 are in `testmd run` / `--name`. Issues 7–9
are in Test Manager's "Execute and verify".

## 1. Boolean vision checks fail on a correct page ("Yes" is not "true")

**What happens.** When Kane records a yes/no check, it stores `expected_value: "true"` (or
`"false"`) with operator `equals` and no transforms. When the exported test runs, the SDK's
`vision_query` answers the same question in words: `"Yes"`, `"No"`, and sometimes
`"__not_visible__"`. The exact string compare then fails (`"Yes" != "true"`), so the test
fails even though the page is correct.

A related case: text checks compare with exact case, but CSS can change how text renders.
If the page shows the value `high` as `HIGH` (e.g. `text-transform: uppercase`), the vision
answer is `"HIGH"` and the check fails.

**Reproduce.** Record any objective that ends in "verify … shows …", export the Playwright
Python code, and run it with `TESTMU_SMART=1`. Every boolean sub-check fails.

**Expected.** Either `vision_query` returns `"true"`/`"false"` when the recorded expected value
is a boolean, or the boolean compare accepts yes/no.

**Workaround.** [tests/playwright/native/testmu_compat.py](../tests/playwright/native/testmu_compat.py)
wraps `assertion._resolve_sub_checks` and does two things:

- maps yes/no words to `"true"`/`"false"` when the expected value is a boolean;
- matches text checks case-insensitively.

It logs every value it changes. Toggle with `kaneai.yes_no_compat`.

## 2. Dropdown steps export as `select_option()` with no value

**What happens.** A recorded step `Select "red" in "Label"` exports as:

```python
async with testmu.step('Select "red" in "Label"', instruction_id='...'):
    element_2 = page.locator("role=combobox[name='Label']")
    await element_2.select_option()
```

The call has no value, so the dropdown keeps its default and the test checks the wrong data.

**Reproduce.** Record any objective that picks an option from a `<select>` and look at the
exported `.playwright.py`.

**Expected.** `await element_2.select_option(label="red")`, or the value the step recorded.

**Workaround.** `_repair_select_options` in
[run_with_junit.py](../tests/playwright/native/run_with_junit.py) reads the value back from the
step label and adds `label='<value>'` in a per-run copy. The export itself stays unchanged.
Toggle with `kaneai.select_option_repair`.

## 3. `--variables` does not override `{{global.*}}` when a replay is fetched from Test Manager

**What happens.** An objective that uses `{{start_url}}` is saved as `{{global.start_url}}`, which
is a Test Manager global variable. Replaying with `testmd run … --variables '{"start_url":{"value":"<new url>"}}'`
still opens the URL that was recorded. This means:

- every replay goes back to the same start URL, so if your AUT keeps data per URL or per user,
  leftover data from earlier replays piles up there;
- recordings that share the one global all point at the same value.

**Expected.** Values passed on the command line win over the recorded or global value.

**Workaround.** Make criteria independent of leftover data (e.g. check "a row titled X exists"
rather than "the list has exactly one row"), or reset your AUT's test data before Stage 1.

## 4. `testmd run` reuses an incomplete step cache

**What happens.** `testmd run` writes the steps it ran into `<asset dir>/output-<stem>/.internal/steps`.
The next replay reads that cache. When the earlier run stopped early, the cache is incomplete,
and the replay's vision queries return `None`.

**Workaround.** Before each replay, `_clear_replay_cache` in [ci/kane_replay.py](../ci/kane_replay.py)
empties `output-<stem>/` except `.internal/meta.json`, which links the asset to its Test Manager case.

## 5. A repeated `--name` saves `<name>-2` instead of overwriting

**What happens.** When you record again with the same `--name`, the test is saved as
`<name>-2`, `<name>-3`, and so on, along with its code export. Tools that look for `<name>`
pick up the old recording. Each recording also creates a new Test Manager case, so duplicate
cases pile up quickly.

**Expected.** An option to overwrite, or to record a new version of the existing case.

**Workaround.** `_clear_previous_outputs` in [ci/kane_record.py](../ci/kane_record.py) deletes
earlier outputs before recording. Duplicate cases have to be deleted in Test Manager by hand.

## 6. A replay overwrites the Test Manager case title

**What happens.** A title set through the Test Manager API (e.g. `SC-001 · <criterion>`) is
reset by the next `testmd run` replay to Kane's own generated wording. Tags, priority, type
and preconditions survive.

**Expected.** A replay keeps a title that someone has edited, or there is an option to turn
the rewrite off.

**Workaround.** None. Identify cases by their tags (`SC-xxx`, `AC-xxx`) instead of by title.

## 7. Execute and verify fails when the exported locator matches more than one row

**What happens.** Exported locators for row actions are not scoped to the row the step acted
on, e.g. `page.locator("role=button[name='☐']")` for a button that every row has. Once the page
has more than one such row, Playwright stops with "strict mode violation: … resolved to N
elements". Each failed attempt can leave more data behind, so the next attempt sees more matches.

The failure is also labelled "Environment Issue → Platform Failure", although the cause is the
locator, not the platform.

**Expected.** The exported locator is scoped to the row the step acted on (for example
`page.locator("tr", has_text="<row title>").get_by_role("button", name="☐")`).

**Workaround.** None for Execute and verify. The regression run here uses hand-written, scoped
selectors (`regression.source: templates`, `ci/playwright_bodies.py`).

## 8. A vision check right after a click sees the old page

**What happens.** A step clicks a button that submits a form and reloads the page; the next
step asks the vision model whether the page changed. In Execute and verify the model can
answer from the page before the reload and fail the check, even though the AUT did the right
thing. The export doesn't wait for the navigation before taking the screenshot for the vision
query.

**Expected.** After an action that navigates, the export waits for the new page before running
the next vision query.

**Workaround.** None for Execute and verify. The same criterion passes as a Kane replay. In
regression templates, wrap every click that submits a form in `page.expect_navigation()`.

## 9. `--variables` on a replay overwrites the shared Test Manager global

**What happens.** Passing `testmd run … --variables '{"start_url":{"value":"<url>"}}'` on a replay
doesn't change where the replay navigates (issue 3), but kane-cli writes the passed value back
to the Test Manager global `start_url`, which every case using `{{start_url}}` shares. Execute
and verify then runs those cases against the last value passed, while the Test Manager screen
still shows the old value.

**Expected.** Values passed on the command line apply to that run only, and are not saved to
the global. The screen shows the value a run will actually use.

**Workaround.** [ci/kane_replay.py](../ci/kane_replay.py) passes only the value the asset was
recorded with (`kaneai.start_url_variable`), never a new one.
