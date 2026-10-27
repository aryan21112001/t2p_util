---
name: generate-dummy-data
description: >
  Wires deterministic, schema-matched synthetic data into a build-tableau-pbip output's empty
  stub partitions, so a project opens in Power BI Desktop against data that actually matches the
  model -- no hand-built spreadsheet, no manual column-name reconciliation, no placeholder-null
  columns. Per project: parses each table's `Table.FromRows({}, type table [...])` empty-stub M
  body (the `{}` -- zero literal rows -- is the signal, not any heuristic), generates N rows
  (default 20) per column typed and DERIVED FROM THE COLUMN'S OWN NAME so a value is traceable
  back to its column in Desktop, writes ONE Excel workbook per project (one sheet per stub table,
  with Excel's real 31-character/illegal-character sheet-name limits handled and deduplicated),
  copies the WHOLE input project to a new output folder (the input is never mutated), and
  rewrites only the COPY's stub partitions to read from that Excel file via
  `Excel.Workbook(path, true)` + `Table.TransformColumnTypes` -- deliberately never an extra
  `Table.PromoteHeaders` call after an already-header-promoting `Excel.Workbook`, the exact
  double-promotion bug a real manual reconciliation session hit and that motivated this skill.
  Never touches a Param table (its `Table.FromRows` already carries real literal domain values)
  or a table already on a native connector (`Excel.Workbook`/`Sql.Database`/etc. from
  `--partitions native`) -- fail closed: skip and report why, never overwrite real data with
  synthetic data. Optional `--realistic <input-corpus-dir>` mode samples REAL values straight
  out of the source workbook's own embedded Tableau `.hyper` extracts (via `_work/ir.json`'s own
  connection records) wherever resolvable, falling back to the same synthetic generator per
  column or per table wherever it isn't -- see the dedicated section below. Emits
  `DUMMY_DATA_REPORT.md` per project: every table wired (row count, sheet name, exact column
  list, and -- under `--realistic` -- a per-column real/synthetic breakdown with why) and every
  table skipped and why. Fully deterministic in the default (non-`--realistic`) mode -- no
  `Date.now()`/random draw anywhere, including the written `.xlsx` file's own embedded
  timestamps -- so re-running against the same input produces byte-identical output.
argument-hint: >
  <build-tableau-pbip --out project-or-parent-folder> --out <output-folder> [--rows N]
  [--desktop-path <path>] [--realistic <input-corpus-dir> [--tds <dir>] [--sample-pct N]
  [--max-real-rows N]]
---

# Generate Dummy Data

Closes the gap between "the model loads" and "the model loads against data that matches it."

> **Corpus-agnostic.** This skill has no built-in knowledge of any particular workbook, table or
> column. Every path is an argument. Counts quoted below are provenance from real runs against
> the 6-workbook reference corpus (see
> [`docs/reference-corpus.md`](../../../docs/reference-corpus.md)), not thresholds for your input.

## Why this exists

A user manually validated a real generated PBIP project (WorkbookI,
table `schema_x.table_monthly`, 72 columns) by pointing its stub M partition at a hand-built Excel
file. That file didn't match the model's real column names/types, so it took extensive manual
reconciliation -- renames plus 59 placeholder-null columns -- and hit one real Desktop bug along
the way: `Excel.Workbook(File.Contents(path), true)` already promotes headers, so an extra
`Table.PromoteHeaders` call after it corrupts every column name.

This skill exists so that reconciliation step, and that bug, never have to happen again: it
generates the Excel file FROM the model's own TMDL, so the columns match exactly by construction,
and it never emits the double-promotion shape.

## Run

```bash
python3 scripts/generate_dummy_data.py <build-tableau-pbip --out project-or-parent-folder> \
    --out <output-folder> [--rows N] [--desktop-path <path>] \
    [--realistic <input-corpus-dir> [--tds <dir>] [--sample-pct N] [--max-real-rows N]]
```

| Option | Meaning |
|---|---|
| first argument | a single `build-tableau-pbip` project folder (holds a `.pbip`), or a parent folder holding several -- same discovery convention as `tools/pbip_lint.py`'s own `find_projects()`, reused rather than reinvented |
| `--out` | required. Each project is **copied** here in full; the original input is never touched |
| `--rows` | synthetic rows per wired table (default 20); also the row count for any table that falls back to fully synthetic under `--realistic` |
| `--desktop-path` | the path string Power BI Desktop -- on the machine that will actually open this project -- resolves to the generated Excel file. See below: omitting this is almost always wrong |
| `--realistic` | the ORIGINAL Tableau input folder (`.twbx`/`.tdsx`, e.g. `input/test_corpus` -- the same folder handed to `build-tableau-pbip`). Enables real-data sampling; **omitted (the default), behavior is exactly the fully-synthetic mode below, unchanged** |
| `--tds` | folder of sibling `.tdsx` published datasources -- same convention as `build-tableau-pbip`'s own `--tds`. Only meaningful with `--realistic` |
| `--sample-pct` | `TABLESAMPLE BERNOULLI(N)` percentage for `--realistic` sampling (default 100, i.e. full data) |
| `--max-real-rows` | hard cap on real rows written per table under `--realistic`, regardless of source table size or `--sample-pct` (default 0, i.e. no cap) |

**Output location convention:** a sibling of that run's `pbip_out`, e.g.
`docs/output/<corpus>/<run>/pbip_dummy` (or `pbip_dummy_realistic` for a `--realistic` run, kept
separate from the plain synthetic output so both remain inspectable) -- never write into
`pbip_out` itself, which stays the untouched baseline.

## `--desktop-path`: why the default is a loud warning, not a quiet guess

If omitted, the default is this script's own resolved absolute POSIX path to the Excel file it
just wrote -- correct only if Power BI Desktop happens to open the project from *this exact
machine*. On any other machine (the normal case: this pipeline runs on macOS, Desktop requires
Windows) that path is wrong, and Desktop will report it can't find the source file. This is
exactly the class of guess -- a path assumed to resolve correctly on a different machine -- that
caused rework in the manual session this script replaces. The script prints an explicit warning
whenever it falls back to this default; pass `--desktop-path` with whatever path string your
actual Desktop instance will resolve, once you know it.

## The one rule this inherits from `build-tableau-pbip`: fail closed

A partition is eligible for synthetic wiring **only** when its `source =` M body is the exact
empty-stub shape `Table.FromRows({}, type table [...])` -- the `{}` (zero literal rows) is the
signal, not a heuristic. Two shapes are never touched, and are reported as skipped with a reason,
never silently:

| Shape | Detection | Why it's skipped |
|---|---|---|
| **Param table** | `Table.FromRows({{...}, ...})` -- non-empty literal rows | Already carries the parameter's real, meaningful domain values (e.g. `Table.FromRows({{"Raw Data"}, {"Unique Data"}}, type table [Value = text])`). Overwriting it would replace real data with fake data. |
| **Native connector** | any other `partition ... = m` body (`Excel.Workbook(...)`, `Sql.Database(...)`, ... from `--partitions native`) | Already wired to a real source. Never overwritten. |

A stub whose `{}` signal is present but whose `type table [...]` ascription doesn't parse is also
declined rather than guessed at, and reported as `unparseable` -- the same "never approximate"
discipline every other skill in this pipeline holds to.

## Synthetic values: derived from the column's own name, never random

| M type (read from the ascription) | Value for row *i* (1-indexed) |
|---|---|
| `text` | `"<ColumnName> <i>"` |
| `Int64.Type` | `1000 + i` |
| `number` | `round(100.0 + i * 1.5, 2)` |
| `datetime` | `2024-01-01 + i days` |
| `logical` | `True` on odd *i*, `False` on even *i* |

No `Date.now()`, no unseeded randomness, anywhere -- every synthetic value is a plain formula of
the row index and the column's own name, so (a) re-running against the same input produces
**byte-identical** output in the default mode, including the `.xlsx` file itself (openpyxl
stamps wall-clock timestamps into every workbook it writes; this script freezes them after the
fact, the same way `tmdl.py`'s lineage tags are content-hashed rather than freshly generated each
run), and (b) a synthetic value is traceable back to its own column by eye in Desktop.

## `--realistic` mode: real values, not synthetic placeholders, wherever resolvable

Passing `--realistic <input-corpus-dir>` switches a stub table's values from purely-synthetic to
REAL, sampled straight out of that workbook's own embedded Tableau `.hyper` extract, wherever
this project's own `_work/ir.json` records a resolvable one. **Omit `--realistic` and behavior is
byte-for-byte the fully-synthetic mode above -- this is an addition, not a replacement.**

**How a table's real source is found -- never guessed, never name-matched by heuristic:**

1. `_work/ir.json` (already emitted by every `build-tableau-pbip` run, in every project this
   skill already reads) is THE authority. Each IR table record's `connections` list may hold an
   entry with `"class": "hyper"` and a `"dbname"` that is the exact zip-relative path to the real
   `.hyper` file inside whichever archive holds it -- the workbook's own `.twbx`
   (`"provenance": "twb"`), or a sibling `.tdsx` resolved via `build-tableau-pbip`'s own `--tds`
   at build time (`"provenance": "tds:<filename>"`, naming the exact sibling file). No filename
   heuristic, no scanning by convention -- the provenance field says exactly which archive.
2. That archive is opened as a zip under `--realistic` (matched by the IR's own recorded
   `workbook` name -- **never** the PBIP project's sanitized folder name, which can legitimately
   differ, e.g. `[WB] Sample Dashboard.twbx` vs the project folder
   `_VRM_ Compliance Dashboard`) or `--tds`. If the expected archive doesn't contain the exact
   member, every `.twbx`/`.tdsx` under both directories is scanned as a fallback before giving up.
3. The matched `.hyper` member is extracted to a scratch directory from `tempfile.mkdtemp()`
   (never bare `/tmp`) and opened via `tableauhyperapi`. The real schema/table name is read from
   `conn.catalog` every time -- Tableau extracts are conventionally `"Extract"."Extract"`, but
   this is VERIFIED per file, never assumed.
4. A sample is drawn with `TABLESAMPLE BERNOULLI(--sample-pct)`, capped at `--max-real-rows` --
   sampling controls value diversity, the cap controls output file size (a 10% sample of a
   50-million-row extract is still enormous). `TABLESAMPLE` can legitimately return zero rows
   from a small table by chance; a plain `LIMIT` (no sampling) is tried once as a fallback so a
   real, if unsampled, row set is still preferred over giving up.
5. Column mapping is read from this SAME `ir.json`: each table's `columns` list already records
   each physical column's real Hyper `source_column` name alongside its allocated PBI/TMDL
   `name`. Selection from Hyper uses `source_column`; the Excel sheet is written with the FINAL
   display name as its header, so no separate rename step is needed in the M -- the generated
   `let ... in ...` body is byte-for-byte the same shape as the synthetic path (see below); only
   the Excel file's cell *values* differ.

**A `.hyper` file for a UNION/federated datasource can hold SEVERAL physical tables at once**
under the `Extract` schema -- found on the real reference corpus: one extract held a real
20-column data table alongside two single-column leftover "sheet" tables from the union. Picking
whichever table a catalog listing happens to return first silently picked the near-empty table
over the real one on that real workbook, until fixed: the resolver disambiguates by
NAME-OVERLAP against the `source_column` names the table's own `ir.json` record already declares
it needs -- never a guess at which table "looks right".

**The one hard boundary, stated plainly: calculated fields are never evaluated.** A column with
no resolvable `source_column` in `ir.json` (a calculated field, group, or bin -- anything with no
raw physical source) ALWAYS falls back to the ordinary synthetic generator for that one column,
even inside an otherwise-"real" table. This script does not implement, and will never implement,
a Tableau-formula or DAX evaluator to produce "realistic" derived values -- `build-tableau-pbip`
has already judged that scope explicitly out of bounds (see its own SKILL.md's "Future
recommendation" section): an incorrectly-computed value that LOOKS plausible is worse than an
honestly-synthetic one, per this whole pipeline's fail-closed philosophy. The same rule applies
one level up: if NO `.hyper` file can be resolved for a table at all (no `hyper` connection
recorded, connection recorded but the archive doesn't contain it, or a genuinely live-only
source), the WHOLE table falls back to synthetic, and why is reported -- never silently.

**Data sensitivity.** `--realistic` mode pulls real data values out of the source workbook's own
embedded extracts -- in this pipeline's own reference corpus, that includes real
transactional fields (response codes, flags, identifiers). **The Excel workbook and rewritten project this mode produces should
be handled with the same data-sensitivity care as the original source Tableau workbook** -- this
is a factual statement about what the mode does, not a hedge. `DUMMY_DATA_REPORT.md` says so
explicitly whenever at least one column in a project was wired with real values.

**Not reproducible run-to-run, by design.** Unlike the synthetic path's byte-identical output,
a `TABLESAMPLE` draw is a fresh sample each run -- only the file-level mechanics (timestamps) are
frozen either way. If you need a fixed sample for repeated comparison, keep one run's output
rather than re-running.

## The exact rewrite shape, and the bug it exists to never reproduce

```
let
    Source = Excel.Workbook(File.Contents("<desktop-path>"), true),
    Sheet = Source{[Item="<sheet-name>",Kind="Sheet"]}[Data],
    Typed = Table.TransformColumnTypes(Sheet, {{"Col1", type text}, {"Col2", Int64.Type}, ...})
in
    Typed
```

`Excel.Workbook(..., true)` already promotes the first row to headers -- the `true` argument.
Calling `Table.PromoteHeaders` again treats the (already-correct) header row as a second
promotion target and corrupts every column name. That is the exact bug the manual WorkbookI
session hit; the generated M here never contains `Table.PromoteHeaders`, and the self-test
asserts it. Because the Excel file's columns already match the model's names/types exactly by
construction, no rename step and no placeholder-null column are needed here, unlike the manual
case that motivated this skill -- true in BOTH the synthetic and `--realistic` paths, since only
the Excel file's cell values differ between them, never the M shape.

## Output per project

```
<out>/<Workbook>/                      <- the WHOLE input project, copied
├── <Workbook>.pbip
├── <Workbook>.SemanticModel/definition/tables/*.tmdl   <- stub tables' `source =` rewritten
├── <Workbook>.Report/                                   <- untouched
├── COVERAGE.md, RESIDUE.md, _work/                      <- untouched, carried over as-is
├── <Workbook>_dummy_data.xlsx        <- one sheet per wired table
└── DUMMY_DATA_REPORT.md              <- every table wired (rows, sheet, columns) and skipped (why);
                                          under --realistic, also a per-table real/synthetic column
                                          count and a full per-column detail section (source +
                                          why, for every wired column in every table)
```

## Measured on the reference corpus

**Fully synthetic**, `--partitions stub` (the default), across all 6 reference-corpus workbooks:

| Workbook | Tables | Wired | Skipped |
|---|---|---|---|
| WorkbookG | 25 | 6 | 19 (all Param) |
| WorkbookD | 6 | 1 | 5 (all Param) |
| WorkbookI | 6 | 1 | 5 (all Param) |
| WorkbookC | 24 | 8 | 16 (all Param) |
| WorkbookE | 3 | 2 | 1 (Param) |
| _VRM_ Compliance Dashboard | 3 | 3 | 0 |
| **Total** | **67** | **21** | **46** |

Every skip in this corpus is a Param table -- this corpus has no `--partitions native` output, so
the "native connector" skip path is unexercised by real input here (it is exercised by the
self-test). Every rewritten project: `tools/pbip_rules/model.py`'s `load_project()` reports **0
`parse_errors`**, and `tools/pbip_lint.py --schemas` reports **0 new FAIL** (only the
pre-existing, unrelated stray-file `NOTES` this corpus already had -- see
`build-tableau-pbip/SKILL.md`'s "Known limits").

**`--realistic input/test_corpus --tds input/test_corpus`**, same 21 wired tables, per-table
real-vs-synthetic column counts (built with `build-tableau-pbip --tds input/test_corpus` so
every sqlproxy table's sibling `.tdsx` is resolved first):

| Workbook | Table | Real cols | Synthetic cols | Why any synthetic |
|---|---|---|---|---|
| WorkbookG | all 6 tables | all columns | 0 | -- |
| WorkbookD | tbl_d | 47 | 13 | calculated fields (groups/bins/derived, no `source_column`) |
| WorkbookI | schema_x.table_monthly | 72 | 0 | -- |
| WorkbookC (8 tables) | Level 1/3/4 tables | 9-109 each | 0-16 each | mostly calculated fields; a handful of physical columns present in Tableau's metadata but genuinely absent from the extract's own materialized schema |
| WorkbookE | Data | 31 | 0 | -- |
| WorkbookE | schema.table_dump | 0 | 33 | whole-table fallback -- no `hyper` connection on THIS table's own ir.json record (see caveat below) |
| _VRM_ Compliance Dashboard | Data | 88 | 0 | -- |
| _VRM_ Compliance Dashboard | SRC_Details_A | 0 | 73 | whole-table fallback (see caveat below) |
| _VRM_ Compliance Dashboard | SRC_Details_B | 0 | 88 | whole-table fallback (see caveat below) |

**Caveat found while building this mode (out of scope for this skill, reported honestly rather
than hidden):** `build-tableau-pbip`'s own table-name allocation keys a table by
`tableau_name or caption`. Both `WorkbookE`'s and `_VRM_ Compliance Dashboard`'s
sqlproxy-referenced sibling `.tdsx` files have a `<datasource>` root element with **neither** a
`name` nor a `caption` attribute, so both fall back to the same hardcoded default label `"Data"`.
For WorkbookE (one sqlproxy table, one sibling) this collides with nothing and the sibling
resolves correctly under the display name `Data`. For _VRM_ Compliance Dashboard (TWO
sqlproxy tables, each with its own sibling `.tdsx`, both similarly unnamed) both siblings collide
on the SAME key, so only the SECOND one's columns survive under a single `Data` table -- the
first sibling's real extract (`SRC_Details_A.hyper`) is never allocated a table at all in
the build. This is a genuine gap in `build-tableau-pbip` itself (not this skill), discovered
because `--realistic` mode's own honest "no hyper connection recorded for this table" reporting
surfaced it; `generate-dummy-data` correctly falls back to synthetic for both affected tables --
it is not silently wrong, just working from an upstream ir.json that has already lost that one
sibling's real connection record.

## Known limits

- **This validates that the model loads against schema-matched data, not that any specific DAX
  result is meaningful.** A measure that divides two synthetic (or even real-sampled) columns
  will compute *something* -- the point is that Desktop opens the file, resolves every
  reference, and a reviewer can see real numbers move, not that those numbers mean anything
  aggregated together as a business result.
- **Not opened in Power BI Desktop by this skill itself**, for the same reason
  `build-tableau-pbip` isn't: Desktop requires Windows, this runs on macOS. Structural validation
  here is `tools/pbip_rules/model.py`'s parser and `tools/pbip_lint.py --schemas`, not a live
  Desktop open.
- **One Excel workbook holds every stub table in a project.** For a very wide corpus this could
  produce a large `.xlsx`; nothing here caps `--rows` (synthetic mode) or splits the workbook.
  Under `--realistic`, `--max-real-rows` now defaults to 0 (no cap) so a build gets full,
  uncapped real data without the caller having to ask for it -- the standing convention this
  skill exists to serve. The write path itself (`write_excel()`) has no Excel row-limit guard
  of its own (openpyxl does not enforce the ~1,048,576-row/sheet ceiling), so a single Hyper
  table beyond that size would produce a workbook Desktop/Excel refuse to open. Accepted as a
  bounded risk for this pipeline's target corpus (BI dashboards over aggregated extracts, not
  raw multi-million-row transactional tables) -- pass `--max-real-rows` explicitly to cap a
  workbook known to carry an unusually large table.
- **Column order in the Excel sheet matches the TMDL ascription's order**, which is not
  necessarily the order Tableau's original source had -- irrelevant to `Table.TransformColumnTypes`
  (which matches by name, not position), but worth knowing if eyeballing the raw sheet.
- **`--realistic` requires `tableauhyperapi`** (already installed in this repo's environment; if
  missing elsewhere, `pip install tableauhyperapi`). Without `--realistic`, this dependency is
  never imported/required at all.
- **`--realistic` sampling is not reproducible run-to-run** (a fresh `TABLESAMPLE` draw each
  time) -- unlike the synthetic path's byte-identical guarantee. Keep a run's output if you need
  a fixed sample to compare against later, rather than re-running.
- **Never evaluates a Tableau calculated field, group, or bin** to produce a "realistic" derived
  value under `--realistic` -- see the hard-boundary paragraph above. That column always falls
  back to the ordinary synthetic generator.
