---
name: extend-tablecalc-quickcalcs
description: >
  EXPERIMENTAL, ISOLATED extension of build-tableau-pbip covering Tableau's built-in "quick table
  calculation" BUTTONS (Percent of Total, Difference, Running Total, Moving Average, ...) applied
  directly to a shelf pill via Tableau's "Add Table Calculation" menu -- structurally different
  from a NAMED calc table-calc (RANK/RANK_DENSE, handled by the sibling extend-tablecalc-rank
  skill): a quick-calc button never creates a calculated field or formula at all, so nothing
  about it exists anywhere in a build's own ir.json until this extension's own extract.py capture
  (worksheet_pcto_meta()). Two jobs in one skill, on purpose: (1) IDENTIFY every quick-calc
  instance in a build, of every type, with counts -- useful on its own even for a type with no
  template yet; (2) PROMOTE to a real DAX measure whichever types have a verified-safe template.
  Today: Percent of Total only (DIVIDE + CALCULATE + ALLSELECTED, NOT RANK's ALLEXCEPT -- the
  semantics genuinely differ, see this skill's own derivation). Future templates (Difference,
  Running Total, Moving Average, ...) get added to this SAME skill, not spun into new ones.
  Takes an existing build-tableau-pbip output (its `_work/{ir,name_authority}.json` already has
  everything needed, once the paired extract.py fix/capture has run) plus the source `.twbx` for
  provenance, and writes a NEW, separate PBIP copy with the promoted measure(s) patched in -- the
  original build is never touched, so it stays the untouched baseline to diff against.
argument-hint: <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
---

# Extend Table-Calc Quick Calculations

Identifies every Tableau "quick table calculation" button in a build (Percent of Total,
Difference, Running Total, Moving Average, Rank, ...) and promotes Percent-of-Total instances
with a resolved, safe "Compute Using" addressing into a real DAX measure -- in an isolated copy of
an existing build, not the main pipeline.

## Why this is separate from build-tableau-pbip, and why one skill covers many calc types

A quick-calc button is structurally unlike everything else this pipeline declines: it is not a
named calculated field, so it has no formula text for `dax.py`'s translator to even look at. It
attaches directly to a worksheet's own shelf placement (a `<column-instance><table-calc
type="PctTotal" .../>`). Before this extension, nothing about it existed anywhere in `ir.json` --
`extract.py`'s own `worksheet_pcto_meta()` (added alongside this skill) is what captures it, for
EVERY quick-calc type at once, since the capture cost is the same regardless of which types this
skill can currently template. That is also why "identify" and "promote" live in one skill instead
of two: the identification half is already complete and useful (it reports every type by name and
count, even ones with no template yet) the moment the extraction fix lands, and each new template
this skill grows over time reads from that same, already-correct data -- no new extraction work
per template.

Percent of Total is the first template because it is the single most common quick-calc pattern in
the reference corpus (557 instances across 46 of 115 workbooks) -- but its own DAX pattern had to
be DERIVED, not copied from RANK's `ALLEXCEPT`: Percent of Total must keep every OTHER active
filter intact and lift only the ONE addressed dimension's own filter (`ALLSELECTED`), where RANK
needs the full comparison set regardless of any other active filter (`ALLEXCEPT`). Copying the
wrong pattern would have computed a plausible-looking but WRONG percentage the moment any other
filter was active -- exactly the class of mistake this whole isolated-first architecture exists to
catch before it ever reaches the main pipeline.

## Prerequisite: the paired `extract.py` fix

This skill depends on two small, additive changes already made to `build-tableau-pbip/scripts/extract.py`:

1. A `FIELDREF` regex fix (`parse_fieldref`, the shelf-text scan in `extract_worksheets()`, and
   `worksheet_tc_addressing()`) so a quick-calc pill's own 4-segment shelf reference
   (`[agg:underlying-agg:FIELD:suffix]`) resolves its REAL field, not the underlying-agg
   placeholder ("sum"/"usr"/"ctd"/"cnt") the old regex mistook for it. Paired with a companion
   guard in `pbir.py`'s `_resolve_field()` so this fix changes NOTHING about what the main
   pipeline itself binds -- a quick-calc shelf code is still dropped exactly as before; only this
   extension, reading `_work/ir.json` directly, sees the now-correct field name.
2. `worksheet_pcto_meta()` -- a new capture function, wired into `extract_worksheets()` as one
   new, additive `"table_calc"` key on each affected shelf/encoding entry (`None` for the
   overwhelming majority of entries that carry no quick calc at all).

Both are already part of the regular `build-tableau-pbip` build -- no separate step needed before
running this skill, as long as the build being extended was produced after these changes landed.

## Run

```bash
python3 scripts/extend_tablecalc_quickcalcs.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
```

| Argument | Meaning |
|---|---|
| `<workbook.twbx>` | the original source file, for provenance/logging only -- NOT re-parsed. Every fact this script acts on already lives in `<existing_pbip_dir>/_work/{ir,name_authority}.json`. |
| `<existing_pbip_dir>` | a build-tableau-pbip output folder (must contain `_work/ir.json`, `_work/name_authority.json`, and a `*.SemanticModel/` folder). |
| `--out` | required. A NEW folder (must not already exist) -- the existing build is copied here in full, then patched. The original input is never modified. |

Writes `TABLECALC_QUICKCALCS_EXTENSION_REPORT.md` into `--out`, with three sections: **Promoted**
(each new measure's DAX and where it was written), **Recognized, not yet templated** (every OTHER
quick-calc type found, grouped by type with counts -- never silently dropped just because no
template exists yet), and **Checked, not promoted** (every Percent-of-Total instance that was
inspected and declined, with a specific reason) -- matching this whole project's "never silently
drop, always report" convention.

## Scope, on purpose -- fails closed on everything outside it

- Only `type == "PctTotal"` gets a DAX measure. Every other recognized quick-calc type
  (Difference, Running Total, Moving Average, shelf-level Rank, ...) is reported by name and
  count, never conflated with an actual decline, and never guessed at.
- "Compute Using" (`ordering-type`) safety:
  - `Field` (Specific Dimension(s)) with a resolvable per-usage `ordering-field` -- always safe.
  - `Table` -- a partition-less grand total (`ALLSELECTED()` with no column argument) -- always
    safe, no dimension-counting judgement call needed.
  - `Rows`/`Columns` (Table Down/Across) -- safe ONLY when the worksheet's Rows+Cols shelves
    together carry exactly ONE real, non-aggregated dimension (the Down/Across distinction
    becomes moot when there is only one axis to name). 0 or 2+ dimensions decline.
  - Anything else (`RowInPane`/`ColumnInPane`/`CellInPane`, missing) -- always declines. Relative
    to the visual's own pane/cell layout; no stable model-level column to anchor a filter-removal
    to.
- The base value's own aggregation must map to a real DAX aggregation function
  (`tmdl.AGG_DAX`) -- sourced from the pill's own explicit `aggregation=` attribute first, else
  its shelf ref's own underlying-agg segment. A base value that is itself a computed field
  (Tableau's "User" aggregation, `usr`) is declined by name, not guessed at.
- Partition and base field must each resolve to a real, ALREADY-EMITTED **column** (never a
  measure) on the SAME table -- cross-table Percent of Total is not implemented.
- A measure name collides with either the existing model OR another candidate promoted earlier in
  the SAME run (the same base field, Percent-of-Total'd with a different partition on a different
  worksheet, needs a distinct name -- the partition is part of what makes it a different measure)
  -- declined, never silently overwritten or auto-suffixed.

## Before trusting a patched build

This script's own name allocation and TMDL insertion point are NOT the same code path
`NameAuthority`/`TmdlModel.build()` use in the main pipeline -- treat every promoted measure as
provisional until:
1. The new build opens in Power BI Desktop with no load errors.
2. Each promoted measure is dragged onto a table/matrix visual alongside the SAME dimension(s) the
   source Tableau worksheet had on Rows/Cols, and its value is spot-checked row-by-row against
   Tableau's own rendered percentage.
3. The same check is repeated with a page/report filter active: `ALLSELECTED` is genuinely
   context-dependent and does not reproduce Tableau's before/after-table-calc filter distinction
   byte-for-byte in every edge case -- confirm the percentage still matches Tableau's FILTERED
   view, not the unfiltered total.
4. `tools/pbip_rules/lint.py <new_pbip_dir> --schemas` passes clean.

## Self-test

```bash
python3 scripts/extend_tablecalc_quickcalcs.py --selftest
```
