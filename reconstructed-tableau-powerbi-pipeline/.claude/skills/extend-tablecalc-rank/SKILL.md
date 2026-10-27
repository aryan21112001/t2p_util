---
name: extend-tablecalc-rank
description: >
  EXPERIMENTAL, ISOLATED extension of build-tableau-pbip's own RANK/RANK_DENSE table-calculation
  rule. The main pipeline already auto-translates a Tableau RANK/RANK_DENSE table calc into a
  real DAX measure (RANKX/ALLEXCEPT) in exactly ONE safe case: "Restart every: <field>" saved on
  the calc's OWN definition. This skill covers a SECOND, equally safe case the main pipeline does
  not yet act on: "Compute Using: Specific Dimension(s)" -- a fixed field name, saved either on
  the calc's own `<table-calc>` definition or, when the calc never sits directly on a shelf, on
  every WORKSHEET that references it. Deliberately built as a separate skill rather than a
  change to `tmdl.py`'s `route()`, so this extension's own correctness can be checked against a
  real corpus workbook -- in a build nobody else depends on -- before it is ever folded into the
  main pipeline. Takes an existing build-tableau-pbip output (its `_work/*.json` already has
  everything needed) plus the source `.twbx` for provenance, and writes a NEW, separate PBIP
  copy with the promoted measure(s) patched in -- the original build is never touched, so it
  stays the untouched baseline to diff against.
argument-hint: <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
---

# Extend Table-Calc RANK

Promotes a second, currently-untouched class of Tableau RANK/RANK_DENSE table calculation into
real DAX -- in an isolated copy of an existing build, not the main pipeline.

## Why this is separate from build-tableau-pbip

`tmdl.py`'s `route()` already promotes RANK/RANK_DENSE when the calc's own `<table-calc>`
definition carries a resolved "Restart every: `<field>`" partition. That is safe because the
setting is universal -- it applies everywhere the calc is used, not just on one worksheet.

"Compute Using: Specific Dimension(s)" is the same kind of fact (a fixed, named field, not a
view-relative one) -- but it is sometimes saved per-WORKSHEET rather than on the calc itself
(Tableau does this whenever a calc is used inside a worksheet without sitting directly on a
Rows/Cols/Pages shelf). Before promoting a per-worksheet setting into ONE shared model measure,
every worksheet that uses the calc has to agree on the same field -- if they don't, the correct
answer is "no", not "guess the first one found". That extra agreement check is new, not yet
proven against the whole corpus, which is exactly why this lives in its own skill: it can be
run, checked, and diffed against a real build without touching `build-tableau-pbip` at all.

## Run

```bash
python3 scripts/extend_tablecalc_rank.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
```

| Argument | Meaning |
|---|---|
| `<workbook.twbx>` | the original source file, for provenance/logging only -- NOT re-parsed. Every fact this script acts on already lives in `<existing_pbip_dir>/_work/{ir,name_authority,residue}.json`, written by the SAME extraction the regular build already ran. |
| `<existing_pbip_dir>` | a build-tableau-pbip output folder (must contain `_work/ir.json`, `_work/name_authority.json`, `_work/residue.json`, and a `*.SemanticModel/` folder). |
| `--out` | required. A NEW folder (must not already exist) -- the existing build is copied here in full, then patched. The original input is never modified. |

Writes `TABLECALC_RANK_EXTENSION_REPORT.md` into `--out`, listing every promoted measure (with
its new DAX) and every candidate that was checked and left alone, with a specific reason --
matching this whole project's "never silently drop, always report" convention.

## Scope, on purpose -- fails closed on everything outside it

- Only `RANK`/`RANK_DENSE`. `RANK_UNIQUE`/`RANK_MODIFIED`/`RANK_PERCENTILE` never qualify, for
  the same reason `tmdl.py`'s own `route()` excludes them: no exact DAX tie-break equivalent.
- Only promotes to a **measure**. A calc that would resolve to a calculated **column** instead
  (checked via `dax.is_aggregate()`, the same seed rule `compute_agg_map_by_name()` uses) is
  skipped and reported -- the column-emission path is not implemented here yet.
- The partition field must be a real, ALREADY-EMITTED column on the calc's own table (checked
  against `name_authority.json`'s own `emitted` list -- the same "is this really in the model"
  ground truth `TmdlModel._resolver()` uses via its `declined` cascade guard).
- When the addressing is worksheet-saved, every worksheet referencing the calc must agree on the
  same field -- checked by set-equality on the resolved names, never "the first one found".
- The formula must reference nothing outside a narrow, same-table-only resolver -- a parameter,
  a cross-table field, another calc. `dax.translate()` raises `Unsupported` for anything wider,
  reported as a skip, never a guess.

## Before trusting a patched build

This script's own name allocation and TMDL insertion point are NOT the same code path
`NameAuthority`/`TmdlModel.build()` use in the main pipeline -- treat every promoted measure as
provisional until:
1. The new build opens in Power BI Desktop with no load errors.
2. Each promoted measure's value is spot-checked against Tableau's own rendered number.
3. `tools/pbip_rules/lint.py <new_pbip_dir> --schemas` passes clean.

## Self-test

```bash
python3 scripts/extend_tablecalc_rank.py --selftest
```
