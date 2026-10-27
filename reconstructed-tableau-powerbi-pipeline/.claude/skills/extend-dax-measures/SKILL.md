---
name: extend-dax-measures
description: >
  EXPERIMENTAL, ISOLATED extension of build-tableau-pbip's own table-calculation rule, covering
  four calc shapes beyond the RANK/RANK_DENSE case extend-tablecalc-rank already handles:
  LOOKUP(<expr>, 0) same-row passthroughs (need no addressing at all), INDEX() row-position calcs,
  fixed-window WINDOW_SUM(<expr>, -N, 0) moving sums/averages (including the ratio-of-two "moving
  average rate" shape), and DYNAMIC-window WINDOW_SUM whose start/end bound is a Tableau parameter
  instead of a literal (resolved to that parameter's own SELECTEDVALUE measure, built via a
  rank-difference template). The genuinely new capability: when worksheets DISAGREE on the
  addressing field, this skill does not decline the whole calc the way extend-tablecalc-rank does
  -- it generates ONE DAX measure PER DISTINCT addressing variant found, so a calc used on several
  pages with different "Compute Using" settings gets one measure per setting instead of a blanket
  refusal. Deliberately MODEL-ONLY: never reads or writes anything under a *.Report folder (no
  page.json, no visual.json) -- deciding which page's visual should bind to which variant is a
  separate, later, human-reviewed step, out of scope here on purpose. Deliberately a separate
  skill from extend-tablecalc-rank/extend-tablecalc-quickcalcs, same isolation reasoning: checked
  against a real corpus workbook, in a build nobody else depends on, before (if ever) folding any
  of it into the main pipeline. Takes an existing build-tableau-pbip output (its `_work/*.json`
  already has everything needed) plus the source `.twbx` for provenance, and writes a NEW,
  separate PBIP copy with the promoted measure(s) patched in -- the original build is never
  touched, so it stays the untouched baseline to diff against. On the real WorkbookB Dashboard
  corpus, the dynamic-window template is real and self-tested but promotes nothing new there --
  its 4 real residue candidates ALSO have no named addressing field recorded at all (Tableau's
  default row/pane addressing), the same dead end as the fixed-window siblings that never even
  reach the literal-vs-parameter question; it exists for a workbook/calc where addressing IS
  resolvable but the window size itself is parameter-driven. Future templates (LOOKUP with a
  non-zero offset / "% Change" calcs needing true row-lag DAX or a bespoke text template) get
  added to this SAME skill, not spun into new ones.
argument-hint: <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
---

# Extend DAX Measures

Promotes three more classes of Tableau table calculation into real DAX -- in an isolated copy of
an existing build, not the main pipeline -- and, where a calc's addressing genuinely differs by
worksheet, promotes it into MULTIPLE measures (one per variant) instead of declining outright.

## Why this is separate from build-tableau-pbip (and from the other extend-tablecalc-* skills)

`tmdl.py`'s `route()` sends every table calc to Tier H residue by default ("addressing/
partitioning is not in the formula text"), with one carve-out already implemented elsewhere:
RANK/RANK_DENSE (`extend-tablecalc-rank`). That skill's own resolver requires every worksheet
using a calc to agree on the same addressing field, else it declines the whole calc -- correct for
RANK, but needlessly conservative for the three shapes here, where the honest answer to
disagreement is "build both measures," not "build neither." That extra fan-out logic
(`resolve_addressing_variants()`) is new and unproven against the whole corpus, which is exactly
why it lives in its own skill: run, checked, and diffed against a real build without touching
`build-tableau-pbip` or `extend-tablecalc-rank` at all.

## Run

```bash
python3 scripts/extend_dax_measures.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
```

| Argument | Meaning |
|---|---|
| `<workbook.twbx>` | the original source file. Every promotion DECISION acts only on `<existing_pbip_dir>/_work/{ir,name_authority,residue}.json`, written by the SAME extraction the regular build already ran -- this file is NOT re-parsed for that. It IS read once, best-effort, purely for the report's own usage annotation (see below); a read failure here (wrong path, an actual `.twb`/`.twbx` mismatch) never aborts the run, it just leaves that annotation off. |
| `<existing_pbip_dir>` | a build-tableau-pbip output folder (must contain `_work/ir.json`, `_work/name_authority.json`, `_work/residue.json`, and a `*.SemanticModel/` folder). |
| `--out` | required. A NEW folder (must not already exist) -- the existing build is copied here in full, then patched. The original input is never modified, and no file under any `*.Report` folder is ever touched. |

Writes `DAX_MEASURE_EXTENSION_REPORT.md` into `--out`, listing every promoted measure (its new
DAX, its addressing field, and which worksheet(s) that variant belongs to) and every candidate
that was checked and left alone, with a specific reason -- matching this whole project's "never
silently drop, always report" convention. Every declined candidate is further split into
**Mandatory** (actually referenced on a worksheet, or read by another calc's formula -- checked
directly against the workbook's own XML, `classify_usage()`) vs **Optional** (referenced nowhere
at all -- a dead calculated field sitting in the model, safe to ignore; this is also usually WHY
no addressing was ever recorded for it, since Tableau only saves a per-usage "Compute Using"
setting for a calc that is actually placed somewhere).

## Scope, on purpose -- fails closed on everything outside it

- Only 4 shapes: `LOOKUP(<expr>, 0)`, `INDEX()`, `WINDOW_SUM(<expr>, -N, 0)` (bare or as a ratio of
  two such calls, `N` a **literal** integer, end literally `0`), and the same `WINDOW_SUM` shape
  with a **dynamic** window (one or both bounds a Tableau parameter, optionally negated, resolved
  to that parameter's own `SELECTEDVALUE` measure). Everything else recognizably close is
  *deferred*, never guessed at:
  - `LOOKUP(..., <non-zero offset>)`, or any `LOOKUP`/`WINDOW_SUM` nested inside a larger
    expression (e.g. a "% Change" formula that builds a whole sentence, not a plain number) --
    needs true row-lag DAX (`OFFSET()`) or a bespoke text template, a different template family.
  - Any other `WINDOW_*`/`RUNNING_*` function.
- Only promotes to a **measure**. A shape whose relevant expression is not itself an aggregate
  (`dax.is_aggregate()`) would resolve to a calculated **column**, which is skipped and reported,
  not guessed at -- not implemented here yet. `INDEX()` is the one deliberate exception: it has no
  inner expression to check, and its `RANKX(...)` shape is unambiguously a measure regardless.
- An addressing field must be a real, ALREADY-EMITTED column on the calc's own table (checked
  against `name_authority.json`'s own `emitted` list, same ground truth `extend-tablecalc-rank`
  uses).
- When addressing is worksheet-saved and worksheets disagree, EVERY distinct field gets its own
  measure (named `<calc> — by <field>`) instead of the whole calc being declined -- the one
  genuinely new behavior in this skill.
- **Model-only, always.** No `*.Report`/PBIR file is ever read or written. Binding a page's visual
  to the right addressing variant is a separate, human (or later, separate skill's) step.
- The rolling-window template ASSUMES the addressed column's own ascending/descending sort
  matches Tableau's row order for that field (true for real dates and for consistently-formatted
  month strings), and that the model's grain at the resolved scope is one row per distinct
  addressing value. Both are stated per-promoted-measure in the report, never silently assumed.

## Before trusting a patched build

This script's own name allocation and TMDL insertion point are NOT the same code path
`NameAuthority`/`TmdlModel.build()` use in the main pipeline -- treat every promoted measure as
provisional until:
1. The new build opens in Power BI Desktop with no load errors.
2. Each promoted measure's value is spot-checked against Tableau's own rendered number.
3. For a `WINDOW_SUM`/`INDEX` measure, the model's grain at the resolved addressing field is
   confirmed to be one row per distinct value -- the template sums/ranks ROWS, not periods.
4. For a DYNAMIC-window measure specifically: re-check the value after changing the underlying
   parameter in Desktop -- the `ADDCOLUMNS`+`RANKX`+`FILTER` template is materially newer and
   less proven than the fixed-window `TOPN` one.
5. `tools/pbip_rules/lint.py <new_pbip_dir> --schemas` passes clean.

## Self-test

```bash
python3 scripts/extend_dax_measures.py --selftest
```
