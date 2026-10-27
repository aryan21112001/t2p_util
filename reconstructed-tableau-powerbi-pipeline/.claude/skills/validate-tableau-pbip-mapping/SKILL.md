---
name: validate-tableau-pbip-mapping
description: >
  For a real build-tableau-pbip output, reports exactly what each Tableau field became in Power
  BI, and for everything that did not make it, why -- plus a suggested fix. RESIDUE.md already
  says what did not translate; this is mostly a FORMATTER over data the build already produced
  (name_authority.json's Binding Ledger + ir.json's source formulas + the emitted TMDL's real DAX
  text), not a new extractor. Per workbook it writes a MAPPING_REPORT.xlsx (Summary / Measures /
  Columns / Parameters / DAX Warnings / Gaps / Unaccounted / Name Collisions / Identifier Map
  sheets) and a VALIDATE.dax script -- one EVALUATE query per mapped measure/column, for Power BI
  Desktop's DAX query view, plus a commented gaps section (reason + suggested fix) for everything
  declined. Every Tableau formula and DAX expression shown anywhere in the report is dereferenced
  first: an opaque internal identity -- `[Parameters].[Parameter 15]`, or an auto-suffixed
  duplicate field like `[Visa Countryregion C (copy)_739153350486216704]` -- is rewritten to its
  real caption (`[Region - Parameter]`, `[Region]`) straight from ir.json, never guessed; every
  identifier actually substituted is recorded in the Identifier Map sheet, and every parameter's
  own raw identity + real domain values live in the Parameters sheet. "Mapped" is not the
  same claim as "will not error in Desktop" -- two static checks (no DAX engine needed) catch
  this without executing anything: one flags a mapped row whose DAX references a parameter
  table's raw column outside SELECTEDVALUE() (145 real instances found, fixed at the source in
  tmdl.py); the other flags ANY dangling `'Table'[Column]`/`[Measure]` reference against the real
  names read from the emitted TMDL (caught a group-of-a-declined-group cascade bug, also fixed at
  the source). Suggested fixes come from FIX_RULES, a pattern table keyed on the build's own
  decline-reason text, not fabricated per instance. Use AFTER build-tableau-pbip, to check what a
  build actually did
  against what it claims, and to hand a reviewer one file per dashboard to work through in
  Desktop.
argument-hint: <build-tableau-pbip --out folder> --out <output-folder>
---

# Validate Tableau → Power BI Mapping

Answers two questions `RESIDUE.md` alone does not: **exactly what did a Tableau field become**,
and **for everything that didn't make it, what should a reviewer actually do about it**.

> **Corpus-agnostic.** This skill has no built-in knowledge of any particular workbook,
> dashboard or field. Every path is an argument. Measured figures quoted below are
> provenance from the reference corpus, not thresholds or expectations for your input —
> see [`docs/reference-corpus.md`](../../../docs/reference-corpus.md).

## Why this is mostly a formatter, not a new extractor

`build-tableau-pbip` already produces everything this needs, per workbook:

| File | What it holds |
|---|---|
| `_work/name_authority.json` | the Binding Ledger — every ALLOCATED table/column/measure/parameter name, keyed by Tableau source identity, plus `emitted`: which of those were actually written to TMDL |
| `_work/ir.json` | every source calc, its Tableau formula, table, and kind |
| `_work/residue.json` | the same data `RESIDUE.md` renders as prose, structured: `calc_residue` (tier/table/subject/reason/formula), `visual_undecided`, `zone_gaps` |
| `<name>.SemanticModel/definition/tables/*.tmdl` | the real, emitted DAX text |

This skill cross-references those four. `residue.json` and the `emitted` field did not exist
before this skill needed them — both were added to `build-tableau-pbip` (`build_pbip.py`,
`names.py`) as part of building this, because scraping `RESIDUE.md`'s markdown or trusting
allocation-without-emission both turned out to be real bugs (see below).

## Run

```bash
python3 scripts/validate_mapping.py <build-tableau-pbip --out folder> --out <output-folder>
```

- First argument: the folder `build-tableau-pbip` wrote to — one subfolder per workbook, each
  with a `_work/` directory. Workbooks missing `_work/ir.json` are skipped.
- `--out` (required): where the mapping report is written. Convention: a sibling of that
  corpus/run's `inventory_out` and `pbip_out`, e.g. `docs/output/<corpus>/<run>/validate_out`.

## Output

```
<out>/
  MAPPING_INVENTORY.md
  <Workbook>/
    <Workbook>_mapping.xlsx
    VALIDATE.dax
  ...
```

**`<Workbook>_mapping.xlsx`** — Summary (counts + gap tiers + DAX-warning count + identifiers
dereferenced) / Measures / Columns / Parameters (raw Tableau identity + domain values alongside
the PBI mapping) / **DAX Warnings** / Gaps (tier, reason, suggested fix) / Unaccounted (should
always be empty — see below) / Name Collisions / **Identifier Map** (every opaque identity this
report rewrote to a real caption, and how many formulas/DAX expressions it appeared in) sheets.
Every `Formula`/`DAX` column shown is the dereferenced form — the raw identity is not lost, it
lives in the Identifier Map / Parameters sheet instead of repeating inline in every occurrence.

**`VALIDATE.dax`** — one `EVALUATE` block per mapped measure/column, each preceded by the
Tableau source field and its original (dereferenced) formula as a comment, so a reviewer opens
Desktop's DAX query view, runs a block, and eyeballs the result against what the Tableau formula
implies.
Measure and parameter names are unique model-wide (the Binding Ledger's own guarantee, enforced
in `names.py`), so an unqualified `[Name]` always resolves without a table prefix. A query whose
DAX trips a static check (see below) gets a `-- STATIC CHECK WARNING:` line right above it,
inline with the query a reviewer is about to run. Below the mapped section, every gap gets a
comment block: tier, the original formula, the build's reason, and a suggested fix — no query
to run, since there is nothing built to query.

## `Mapped` is not the same claim as `will not error in Desktop`

This script has no DAX engine — it cannot execute a query, only generate one for a human to run.
But one bug class is still catchable without an engine: a real corpus workbook once had a
measure whose DAX read `IF('Param 1.2 View By'[Value] = "Volume", ...)`, comparing a parameter
table's raw, multi-row `Value` column against a literal outside any row context. It emitted
cleanly, showed up as `mapped`, and errored the moment it was opened in Desktop — "a single
value for column 'Value'... cannot be determined". The build's own convention (`names.py`:
*"every `[Parameters].[X]` reference... rewrites to that [SELECTEDVALUE] measure"*) means any
`'Param <X>'[Value]` reference outside `SELECTEDVALUE(...)` is **always** wrong, by construction
— not a heuristic, since `Param <X>` tables are this pipeline's own synthetic naming convention,
not something a real Tableau source could produce by coincidence.

`static_dax_warning()` checks every mapped row's DAX text for exactly this pattern and attaches
`dax_warning` when it fires — surfaced in the `DAX Warnings` sheet, the Summary count, and inline
in `VALIDATE.dax`. It caught 145 real instances across 5 of the 6 reference-corpus workbooks
before the underlying bug (`tmdl.py`'s `resolve_param()`) was fixed; that corpus now reports 0.
The check runs regardless of input — the numbers are provenance, not a threshold.

A second, more general check lives alongside it: `_find_dangling_refs()` parses every
`'Table'[Column]` and bare `[Measure]` token out of a mapped row's DAX and checks it against the
real names read from the emitted TMDL (`names_by_table`, `all_measure_names` from
`load_dax_by_table()`). Found the same way as the Param bug, on a different corpus workbook: a
Tableau group built on top of ANOTHER group whose own source never resolved got emitted anyway,
referencing a column that was never written — same Desktop failure mode, different root cause
(a cascade gap in `tmdl.py`'s group/bin resolution, since fixed). Unlike the Param check, this one
needs `names_by_table`/`all_measure_names` passed in — omit them (as the self-test's narrow unit
assertions do) and only the Param check runs.

**Two real traps building this check, worth knowing before touching these regexes again:**

- TMDL only quotes an identifier that needs it (`names.py`'s own `tmdl_ident()` rule) — a simple
  name like `Value` or `Channel` is written bare, `column Value`, not `column 'Value'`. The
  line-parsing regexes in `load_dax_by_table()` (`_MEASURE_LINE`, `_COLUMN_LINE`,
  `_COLUMN_BARE_LINE`) must accept both the quoted and bare forms, matching `_TABLE_HEADER`'s
  existing pattern — missing the bare form produced 124 false "dangling reference" warnings
  during development, every one of them a real, correctly-emitted unquoted name the regex simply
  never saw.
- A genuinely multi-line expression's header line — `column 'Name' =` with the real expression
  starting on the NEXT line, no trailing space after `=` — must still be MATCHED even though it
  captures no inline expression, or the name never reaches `names_by_table` at all. Requiring a
  literal `"= "` (equals then a mandatory space) instead of `"=\s*"` made the regex fail to match
  that header line outright, producing a false dangling-reference warning against a column that
  was, in fact, correctly emitted — caught on a real corpus workbook, a group-of-a-group whose
  `SWITCH()` had grown long enough to wrap.

Extend either check the same way — a new rule should be similarly unambiguous, grounded in
something this pipeline's own naming/emission convention guarantees or the real TMDL on disk, not
a general DAX linter with false positives.

**`MAPPING_INVENTORY.md`** — corpus rollup, same shape as `ASSET_INVENTORY.md`.

## The `Unaccounted` sheet is the report's own consistency gate

`mapped + gaps + unaccounted` should equal every calc/schema-column in `ir.json`. A nonzero
`Unaccounted` count means a Tableau field is in neither the Binding Ledger's `emitted` set nor
`residue.json` — an inconsistency in the *build*, not a gap in this report, and it should be
investigated there rather than suppressed here. Two real bugs were caught this way while
building this skill, both against the real 6-workbook corpus:

- **Allocation was checked instead of emission.** `name_authority.json`'s `measures`/`columns`
  maps hold every name *allocated* — `names.py` allocates a name before the formula parser gets a
  chance to decline it, by design (`NameAuthority`'s own docstring: "Allocation is not
  emission"). Checking allocation alone misclassified 80 of 98 real gaps in one workbook as
  "mapped". Fixed by adding `emitted` to `NameAuthority.to_dict()` (it was tracked all along via
  `mark_emitted`, just never serialized) and checking `(pbi_table, name) in emitted` before
  calling anything mapped.
- **The lookup key used the wrong table name.** Allocation is keyed by the *resolved* PBI table
  name, not the raw Tableau caption — `sanitize()` strips a trailing parenthetical (`"Foo (DB)"`
  → `"Foo"`) and collisions rename tables, so caption ≠ PBI table name whenever either happens.
  Keying the lookup on caption silently misclassified 40 real, translated calcs as
  `Unaccounted` in one workbook (`Level 1 Analysis - Data (DB)` → PBI table `Level 1 Analysis -
  Data`). Fixed by keying on the already-resolved `pbi_table`, matching what `tmdl.py` itself
  passes to `na.column()`/`na.measure()`.

Both were caught by requiring `Unaccounted` to reconcile to 0 against the real corpus, not by
reasoning about the code — the same method the project's other detector fixes have used.

## Suggested fixes: a pattern table, not fabricated per instance

`FIX_RULES` in `validate_mapping.py` is an ordered list of `(regex on the reason text) -> fix`.
It is grounded in the decline-reason strings `dax.py`/`tmdl.py` actually raise (catalogued by
running the real corpus and collecting every distinct reason), not hypothetical Tableau/DAX gaps.
A reason matching no rule gets `GENERIC_FIX` — "no canned fix yet, treat as a one-off human
decision" — rather than a guessed answer, the same "never guess" convention
`inventory-tableau-assets` uses for `absent?` and `map-tableau-powerbi-translation` uses for
`UNAPPORTIONED`. Extend `FIX_RULES` when a new reason pattern recurs.

Scope note: fixes for calc-tier gaps (measures/columns) are grounded in real DAX rewrite
patterns. Fixes for the "leftover" residue items — groups/bins/hierarchies, tagged tier `D` when
a deterministic rule can't resolve its source, and presentation-layer items (`visual_undecided`,
`zone_gaps`, currently not read by this script) — are the same rule table applied to the same
reason text, but less exhaustively catalogued; treat those fixes as a starting point, not final.

## Known limits

- Only calculated fields, schema columns, and parameters are cross-referenced (rows 9, 16, 17,
  18 of the asset inventory). Groups/bins/hierarchies appear in `Gaps` when declined (via the
  residue leftover pass) but are not walked for successful mappings — there is no `Groups`/`Bins`
  sheet yet.
- `residue.json`'s `visual_undecided` and `zone_gaps` (presentation-layer residue: undecided mark
  types, unmapped zones) are not read by this script at all. `VALIDATE.dax` only ever makes sense
  for the semantic layer; a presentation-layer gap report, if wanted, is a separate concern.
- The DAX query for a calculated column previews 10 rows (`TOPN(10, ...)`) rather than the full
  column — enough to eyeball, not a substitute for a real data-level QA pass.
- Assumes every real corpus expression stays on one physical TMDL line (true for all 6 corpus
  workbooks today). A wrapped multi-line expression — legal TMDL, not yet produced by this
  pipeline — degrades to a placeholder string in the DAX column rather than a wrong parse.
