---
name: validate-blend-key-domains
description: >
  For a build-tableau-pbip project with real (or realistic-sampled) data wired into its stub
  partitions, checks whether every composite "Blend Key (...)" many-to-many relationship --
  build-tableau-pbip's own translation of Tableau data blending -- actually has ANY overlapping
  real key values between its two tables. build-tableau-pbip's own static check (tmdl.py's
  `_shared_columns`/`_formula_shape`) catches a shared blend-key column computed two DIFFERENT
  ways on either side, but it cannot catch two columns that are the SAME shape (e.g. both an
  ordinary physical column) whose real-world value domains never overlap at all -- that is only
  detectable once real data exists, which is what this skill checks. DETECT-AND-REPORT ONLY: it
  never rewrites relationships.tmdl or any other TMDL file. Real corpus regression this catches:
  GCLM_EU_GERMANY_COMMERZBANK_AG_v2's Acquisition<->Activation Blend Key includes `for_view`, a
  plain column with the same name and dataType on both sides, whose real values are
  {opcert, opcert_dist} on one side and {Channel, MCC, Spend Build Up, ...} on the other -- zero
  overlap, so every visual spanning both tables renders empty in Power BI Desktop no matter how
  much data is loaded. Writes BLEND_KEY_DOMAIN_REPORT.json/.md into the project's validation/
  folder, the same directory validate-pbip-compliance/validate-dummy-data already write into.
argument-hint: <build output: project-or-parent-folder> [--out DIR]
---

# Validate Blend Key Domains

Answers a question neither `build-tableau-pbip` nor `validate-dummy-data` answers: **now that
real data is wired in, does every Blend Key relationship actually have any row it could match
on?** A many-to-many relationship keyed on a same-name/same-dtype column across two blended
tables is Tableau's own blend signal (see `build-tableau-pbip/scripts/tmdl.py`'s
`_relationships()` docstring) -- but a shared name says nothing about whether the two sides'
real-world value domains ever intersect.

> **Corpus-agnostic.** No built-in knowledge of any particular workbook, table, or column.
> Every path is an argument.

## Two-tier division of labor

This is deliberately Tier 2 of a two-tier fix. Tier 1 (`build-tableau-pbip/scripts/tmdl.py`'s
`_formula_shape`, run at build time, before any data exists) declines a shared column that is
computed two structurally different ways on either side (e.g. one side a concatenation of two
fields, the other a bare field alias) -- a property of the model definition, checkable from
`ir.json` alone. This skill is the other half: a shared column that Tier 1 correctly let through
(same shape, e.g. both a plain physical column) can still have completely disjoint real value
domains, which nothing in the IR can tell you -- only the real data can. Tier 1 never re-runs
here, and this check never re-derives Tier 1's shape decision; each looks at a different half of
the same defect.

## What it does

1. Loads the project via `tools/pbip_rules/model.py`'s `load_project()` -- never hand-parses
   `relationships.tmdl` text.
2. Finds every relationship whose join column matches `Blend Key (...)` on both sides
   (`find_blend_key_relationships`). A single-column blend key has no composite wrapper and is
   out of scope for this pass (see that function's own docstring).
3. Parses each Blend Key column's own TMDL expression back into its ordered constituent column
   names (`parse_blend_key_constituents`) -- a bracket-aware scan matching
   `build-tableau-pbip/scripts/names.py`'s own `]`-escaping convention (`dax_ref`), not a naive
   regex.
4. Resolves and reads each side's REAL data, reusing `validate-dummy-data`'s own
   `resolve_real_excel`/`parse_excel_wired` (never trusting the Desktop-machine path string
   inside the M).
5. Recomputes each side's actual Blend Key value SET by replaying the identical `"|"`-join the
   TMDL's own DAX performs over the constituent columns' real row values, and reports every
   relationship pair with **zero intersection**.

## Run

```
python3 validate_blend_key_domains.py <build output: project-or-parent-folder> [--out DIR]
```

Writes `BLEND_KEY_DOMAIN_REPORT.json` + `.md` into `<out>/<project name>/` when `--out` is given,
or `<project's parent>/validation/` by default. Always exits 0 -- a stats-carrying report, not a
pass/fail gate; severity (`zero_overlap`) is read from the printed `[ok]` line or the JSON.

## Known, deliberately accepted limitation

Value comparison uses `str(value).strip()`, which is not DAX-exact for a numeric constituent
column (Excel `5.0` vs a DAX-rendered `"5"`). This can only ever produce a FALSE "zero overlap"
on a numeric column -- which routes to human review anyway, the same outcome as any other
finding here -- never a false "safe", the one direction this check exists to prevent.

## Dependencies

`openpyxl` (already a dependency of `generate-dummy-data` itself). No other third-party package.
