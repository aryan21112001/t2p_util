---
name: validate-dummy-data
description: >
  For a real generate-dummy-data output, checks that the project still holds together after the
  rewrite: TMDL structurally parses, tools/pbip_lint.py --schemas is clean, the project matches
  build-tableau-pbip's own Desktop-authored ground truth, and -- the two genuinely new checks --
  every table wired to Excel.Workbook(...) actually points at a real file that exists in the
  project's own folder, and that file's sheet header exactly matches the column list inside the
  same table's Table.TransformColumnTypes(...) call. Mostly a FORMATTER/AGGREGATOR over three
  checks this pipeline already has (tools/pbip_rules/model.py's parser, tools/pbip_lint.py,
  build-tableau-pbip/tests/check_against_ground_truth.py) -- the same relationship
  validate-tableau-pbip-mapping has to build-tableau-pbip. The Excel-vs-M header check and the
  path-sanity check are the one genuinely new thing: they never trust the Desktop-machine path
  string inside the rewritten M (it cannot resolve on this machine), only the real file this
  project's own generate-dummy-data output actually wrote, resolved by that skill's own naming
  convention. The path-sanity check exists specifically to catch a regression of a real bug --
  every wired table across a whole corpus once pointed at an identical, made-up placeholder path
  that matched neither the real file's name nor its location -- even after the generator itself
  is fixed, because this check verifies the claim against the real file on disk rather than
  trusting the generator got it right this run. Writes VALIDATE_DUMMY_DATA_REPORT.md into each
  project folder, matching generate-dummy-data's own DUMMY_DATA_REPORT.md placement.
argument-hint: <generate-dummy-data --out project-or-parent-folder> [--quiet]
---

# Validate Dummy Data

Answers one question none of the four checks it runs answers alone: **after
generate-dummy-data rewrote a project's stub partitions, does the whole thing still hold
together** -- structurally, against Desktop's own ground truth, against `pbip_lint`'s corpus of
observed real failures, and (the new part) does each rewritten table's Excel file actually still
match the model it claims to feed.

> **Corpus-agnostic.** This skill has no built-in knowledge of any particular workbook, table or
> column. Every path is an argument.

## Why this is mostly a formatter, not a new checker

Three of the five checks already exist elsewhere in this repo and are reused directly, not
reimplemented:

| Check | Reused from |
|---|---|
| 1. TMDL structural parse | `tools/pbip_rules/model.py`'s `load_project()` -- `parse_errors` must be empty |
| 2. Lint | `tools/pbip_lint.py`'s `lint_project()`, run with `with_schemas=True` |
| 3. Ground truth | `build-tableau-pbip/tests/check_against_ground_truth.py`'s `rules()`, against the project's own `*.SemanticModel` |

Checks 4 and 5 are the genuinely new work this skill adds:

| Check | What it does |
|---|---|
| 4. Excel-vs-M header match | For every table whose `source =` M was rewired to `Excel.Workbook(...)`, opens the REAL `.xlsx` file this project's own generate-dummy-data output wrote (`<Workbook>_dummy_data.xlsx`, next to the `.pbip` -- generate-dummy-data's own naming convention, never the Desktop-machine path string inside the M itself), reads the sheet named in `Source{[Item="...",Kind="Sheet"]}`, and asserts its header row EXACTLY matches (same names, same order) the column list inside that table's own `Table.TransformColumnTypes(...)` call. |
| 5. Path sanity | Flags any wired table whose `File.Contents(...)` argument's filename component does not correspond to a `.xlsx` file that actually exists in the project's own folder. |

Checks 4 and 5 are deliberately independent -- neither trusts the other, and neither trusts the
generator. Check 4 never reads the path string in the M at all (it cannot resolve on this
machine, by `generate-dummy-data`'s own design -- see that skill's `--desktop-path` section); it
always resolves the real file by naming convention. Check 5 exists purely to catch the failure
mode check 4 cannot see: a path string that names a file which was never written by this
project's own output at all, even if a same-named, wrong file happens to sit somewhere else.

## Run

```bash
python3 scripts/validate_dummy_data.py <generate-dummy-data --out project-or-parent-folder> [--quiet]
```

- First argument: a single `generate-dummy-data` project folder (holds a `.pbip`), or a parent
  folder holding several -- the same discovery convention `tools/pbip_lint.py`'s own
  `find_projects()` uses, reused rather than reinvented, same as `generate-dummy-data` itself
  reused it.
- `--quiet`: only print projects that fail at least one check.

## Output

`<project>/VALIDATE_DUMMY_DATA_REPORT.md` -- written INTO each project folder, matching
`generate-dummy-data`'s own placement of `DUMMY_DATA_REPORT.md` (this is a check on that output,
not a new artifact with its own separate lifecycle, so there is no `--out` tree). Per project:

- Pass/fail for the TMDL parse, lint, and ground-truth checks, with the actual failing lines
  when any exist (lint's pre-existing stray-file and schema-fetch notes are shown but never
  count as a failure).
- One row per Excel-wired table: sheet name, path-sanity verdict, header-match verdict, each
  with the actual expected-vs-found detail needed to act on a failure directly.
- Tables with nothing wired (all Param, or a project generate-dummy-data reported zero wiring
  for) are noted plainly, not silently omitted.

Exit code is non-zero if any project fails any check, matching `tools/pbip_lint.py`'s own
convention -- never hand over a project while this reports FAIL.

## A schema-fetch note is not a failure -- but check what caused it

Check 2's `--schemas` pass needs network access to `raw.githubusercontent.com` to fetch
Microsoft's published JSON schemas. If that network path is unavailable (a corporate proxy, an
offline machine), every schema-fetch attempt degrades to a NOTE (`tools/pbip_lint.py`'s own
`check_against_schemas()` catches the exception itself), not a FAILure -- so a project can report
clean while the schema-shape checks were never actually run. This is `pbip_lint.py`'s own
degradation behaviour, not something this skill overrides; if a report's Notes section is mostly
`could not validate ...: urlopen error`, the `--schemas` pass did not run for that project, and a
"PASS" there is honest but incomplete -- the underlying checks were skipped, not satisfied.

## Known limits

- Check 4 only ever compares the header ROW (names, order) -- not the column TYPES, and not any
  data row beyond the header. A model whose `Table.TransformColumnTypes` names the right columns
  but coerces them to the wrong DAX type would pass this check; that class of mismatch is not
  what this check is for (it exists to catch a header/column-name mismatch specifically, the bug
  class this whole area of work has hit).
- If a project has more than one `*_dummy_data.xlsx`-shaped file in its folder and none matches
  the exact `<project folder name>_dummy_data.xlsx` convention, check 4 declines with a reported
  ambiguity rather than guessing which one is real.
- A table whose M carries `Excel.Workbook(File.Contents(` but does not fully match
  generate-dummy-data's exact rewrite shape (missing sheet-item lookup, or no
  `Table.TransformColumnTypes` call at all) is reported as a parse failure for that table, not
  silently skipped -- the same fail-closed discipline the rest of this pipeline holds to.
- Needs `openpyxl` for check 4 (already a dependency of `generate-dummy-data` itself); checks
  1-3 and 5 need no third-party package.
