# Semantic model gotchas (`tmdl.py`)

The TMDL writer and the decline-cascade resolution fixpoint (why `tmdl.py` iterates: a calc
referencing another calc that itself got declined must ALSO decline, transitively, not just
fail to resolve one level deep). Full detail: `SKILL.md`'s bug table.

---

### Single-pass calc resolution left dangling DAX references
A calc resolved before its OWN dependency had been resolved could reference an object not yet
(or never) in the model. **Fix:** resolution runs to a fixpoint, re-visiting until nothing
changes. **Status:** ✅ Fixed — this is the architectural basis every later "promote to a
fixpoint" fix below builds on.

### `summarizeBy: sum` emitted on string/date columns
TOM rejects a numeric default-aggregation on a non-numeric column outright — 46 of 1113 columns
in one real workbook, model would not load. **Status:** ✅ Fixed — `summarizeBy` now respects the
column's real declared type.

### Expression indentation preserved as literal spaces
TMDL uses TAB DEPTH, not space count, to decide where a multi-line expression ends — indenting
with spaces (even visually identical) silently breaks parsing. **Status:** ✅ Fixed.

### Coverage numerator mixed real views with control zones (filters/legends/images)
Reported 290% coverage — a materially wrong number, not just an eyesore. **Status:** ✅ Fixed —
the numerator now counts only real worksheet views.

### `resolve_param()` returned the parameter's raw `[Value]` column, not its `SELECTEDVALUE` measure
A calculated column/measure referencing a parameter via the raw column failed in Desktop:
"a single value for column 'Value'... cannot be determined." **145 measures/columns across 5 of
6 workbooks.** Also inflated residue counts elsewhere by miscounting the phantom param table as a
second table in an unrelated SUMX-iterator ambiguity check. **Fix:** always resolve a parameter
reference to its `SELECTEDVALUE` MEASURE (bare `[Name]` form, never a qualified column).
**Status:** ✅ Fixed. **Note:** this is the SAME `[Name]`-no-table-prefix shape
`04-dax-translation-gotchas.md`'s "bare parameter-measure reference" fix later had to recognize
and collapse under an aggregate — if you ever change how `resolve_param` shapes its output,
check that fix too, it depends on this exact shape.

### Groups/bins resolved their source in ONE PASS, not the same fixpoint as calcs
A group built on top of ANOTHER group whose own source failed to resolve got emitted anyway — a
plausible-looking `'T'[Column]` reference to a column never actually written. Desktop: "Column
'...' cannot be found." Only calc-to-calc dependencies were tracked in `self.declined`; groups/
bins declining never fed back into it. **Fix:** groups and bins now resolve to the SAME fixpoint
as calcs, sharing `self.declined`. **Status:** ✅ Fixed.

### A calculated COLUMN referencing a Tableau PARAMETER compiled, but failed only when a report filter touched it
DAX forbids a calculated COLUMN from referencing ANY measure — but `resolve_param` (correctly,
per the row above) always points a parameter at its `SELECTEDVALUE` MEASURE. A calc whose ONLY
aggregate-like dependency is a bare parameter reference has no aggregate function in its own text
and references no other calc, so it was invisible to the promotion fixpoint's seed condition and
stayed a calculated column — loaded fine, 0 preflight FAILs, broke ONLY when a user opened the
2 specific pages built around it: "This visual contains one or more filters with... type
mismatches." **Fix:** seed `_aggregate_map` with `"[Parameters]." in formula` in addition to
`dax.is_aggregate()` — the existing fixpoint then promotes the calc (and everything depending on
it) automatically. **Status:** ✅ Fixed, with a `preflight()`/`pbip_lint.py` backstop for the
BARE-reference shape specifically (the pre-existing qualified-reference check had this gap).

### Same hazard, one layer up: groups/bins could NEVER be promoted to a measure at all
`_group_column`/`_bin_column` always emitted via `_calc_column` directly and never consulted
`_aggregate_map` — a group/bin built on a (transitively) parameter-dependent source would
reproduce the exact forbidden shape from the row above, one level removed. No real workbook has
hit this yet (checked all 8 — none needed the promotion) — a preventive fix for a structurally
real gap, not a confirmed failure. **Fix:** generalized `_aggregate_map`'s fixpoint to cover
groups/bins uniformly with calcs; renamed `_group_column`/`_bin_column` to `_group_body`/
`_bin_body`, returning `(name, dax, obj)` for `_write_table` to route the same way calcs already
route. **Status:** ✅ Fixed preventively.

### `_plan_shelf_measures` (implicit `SUM of <field>` with no calc behind it) had ZERO type awareness
Unlike `dax.py`'s Parser, which already casts/declines a string-typed column aggregate for an
EXPLICIT calc formula (see `04-dax-translation-gotchas.md`), the IMPLICIT shelf-measure path
(a field dragged straight onto Rows/Cols with no calculated field) never got that treatment —
`SUM('Activation'[issr_total_actv_crd_cy])` on a Tableau-STRING column reached TMDL as valid DAX
that failed only at QUERY time. Confirmed live against real Desktop: `MdxScript(Model)...
Calculation error ... The function SUM cannot work with values of type String`. **Fix:**
`_plan_shelf_measures` now does the same local-then-global field-dtype lookup `_resolver()`
already builds for calcs: `SUM`/`AVERAGE` on a string field cast via `SUMX`/`AVERAGEX` +
`IFERROR(VALUE(...), BLANK())`; `COUNT`/`DISTINCTCOUNT` pass through; `MIN`/`MAX`/`MEDIAN`/
`STDEV.S`/`VAR.S` on a string field decline rather than guess. **Status:** ✅ Fixed (found on
GCLM). No post-emit backstop yet — there's no existing per-table-per-column dtype feed into
either checker for this specific shape; worth adding if it recurs.

### `resolve_measure_safe`'s SELECTEDVALUE-wrap fired even when the calc's real usage is `SUM(calc)` on a shelf
`resolve_measure_safe` (two rows above) correctly wraps a bare column reference in `SELECTEDVALUE`
for a parameter-promoted measure used as a genuine dimension-like "branch selection between whole
columns" (its original `cl.Slice By` bug). But that fix applied BLANKET to every parameter-promoted
measure with no aggregate of its own — including one whose REAL Tableau usage, on its own
worksheet shelf, is `SUM(calc)`. `SELECTEDVALUE` returns BLANK the instant more than one real row
exists in context, which is always true for a bare row-level column aggregated by a real shelf
SUM. Real corpus (GCLM), confirmed live against real Desktop: `Penetration` = `(IF
[Parameters].[Opcert or User Input]="Opcert" THEN [issr_cif_cy] ... END) / [Parameters].[Retail
Customers]`, shelf-used as `SUM(Penetration)` on its own worksheet, emitted as
`DIVIDE(SELECTEDVALUE('Acquisition'[issr_cif_cy]), ...)` — BLANK against every real row, where
Tableau's own shelf intent is `SUMX('Acquisition', DIVIDE([issr_cif_cy], ...))`, a real non-blank
number (confirmed `1.1668...` live post-fix). The Power BI card bound to this measure showed
"(Blank)" against a real Tableau value. **Fix:** new `_shelf_agg_only(t)` method — `{calc fkey:
DAX iterator function}` for every calc used EXCLUSIVELY, across every worksheet encoding
referencing it by caption, via ONE consistent REAL shelf aggregation (`AGG_DAX`'s own keys —
sum/avg/min/max/median/stdev/var/count; `DISTINCTCOUNT` excluded on purpose, no X-iterator form
exists for it per `dax.py`'s own `NO_ITERATOR`). `resolve_calcs`'s per-table loop now checks this
map before falling back to `resolve_measure_safe`: if the calc has a shelf iterator, the WHOLE
translated formula is wrapped in that iterator (`SUMX('Table', <expr>)`) with every bare column
left bare — the iterator itself supplies the row context `SELECTEDVALUE` was there to work
around. A calc used as a plain dimension reference (`agg=None`) anywhere, or with inconsistent
aggregations across worksheets, is untouched — still takes the old `SELECTEDVALUE` path.
**Status:** ✅ Fixed, self-tested (both the SUMX-inlining case and the no-regression dimension-
reference case), confirmed live against a fresh rebuild (`EVALUATE ROW("x", [Penetration])` →
`1.1668177500000003`, non-blank). Affects 28 calcs across 4 tables in GCLM alone
(Recommendations 21, Operations Management 1, Acquisition 4, Usage Overview 1) — likely resolves
other "blank card" symptoms beyond the one that surfaced it. If you hit a blank card bound to a
parameter-promoted measure, check whether its real Tableau usage is a shelf aggregate before
assuming `resolve_measure_safe`'s path is still correct for it.

**Shipping status (2026-10-06): fix confirmed correct, NOT YET merged into the canonical `out2`
deliverable.** Live-verified in isolation (`[Penetration]` → `1.1668...`, non-blank) across two
separate fresh rebuilds (`vf9`, `vf11`) — the DAX fix itself is good. Held back from `out2`
because the SAME rebuilds that carry this fix also carry an unrelated, still-OPEN regression —
see `01-orchestrator-gotchas.md`'s "fresh rebuild renders several barChart visuals with zero bars,
no error" entry. Do not merge this fix into `out2` on its own until that's resolved; `out2` stays
on its pre-fix state (Penetration blank, confirmed data bug, but every chart renders) until a
rebuild exists that has BOTH this fix AND working chart rendering.

### `_aggregate_map`'s own seed missed a bracketed-field-name false positive — see `is_aggregate()`
Covered fully in `04-dax-translation-gotchas.md` and `02-extraction-gotchas.md` (same regex bug,
listed under `dax.py` since that's where the fix landed) — included here as a pointer because the
SYMPTOM (a calc wrongly promoted to a measure) shows up in `tmdl.py`'s own output first.
