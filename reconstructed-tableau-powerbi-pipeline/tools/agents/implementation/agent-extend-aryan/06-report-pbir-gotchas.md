# Report / PBIR gotchas (`pbir.py`)

The PBIR report writer — mark/shelf → `visualType` mapping, field-well projections, filters,
fonts. Full detail: `SKILL.md`'s bug table (search for the quoted phrases) and
`.claude/skills/pbir-visual-authoring/` for the confirmed-vs-Microsoft-docs schema notes.

---

## Filter resolution

### A cross-table filter could be bound with no relationship path to the visual's own query table
A visual's `filterConfig` could carry a filter on a table with NO relationship path to what the
visual actually queries — Tableau data BLENDING, where the source worksheet's filter silently
carries fields from a datasource its own `<datasources>` element never declares, matched by name/
role at Tableau query time with no stored key. Desktop: "Can't determine relationships between
the fields... InvalidUnconstrainedJoin." Confirmed corpus-wide: 117 instances across 2 of 6
workbooks. **Fix:** `_query_entities` (the visual's own queried tables) +
`_relationship_reachable` (BFS over the relationship adjacency graph); `_visual_filters` drops a
filter whose target table is neither a query table, a `Param `-prefixed table, nor reachable from
one — reported in `RESIDUE.md`, never silently dropped. **Status:** ✅ Fixed, with a
`RPT-FILTER-04` compliance-rule backstop.

### A FILTER's own field resolved by CAPTION ALONE, globally — the SAME hazard as `names.py`'s
`_resolve_field`'s filter call site never passed a table hint to `by_spelling` at all — the exact
global-first-match hazard `names.py`'s own fix (see `03-name-authority-gotchas.md`) already
named for CALC resolution, recurring independently because that fix never touched this call site.
Real corpus (GCLM): Activation's own filter on `For View = "Trend"` resolved to
`Recommendations[For View]` instead — matched ZERO of 247,425 real rows, blanking every visual
that filtered through it. 281 real filter sites hit this for one caption alone, across 6 pages.
**Fix:** `_resolve_field` gained a `candidate_tables` parameter (`_visual_filters` passes the
visual's own `query_entities`), tried in order as the PREFERRED table before the old global-only
fallback. **Status:** ✅ Fixed (found on GCLM). **Not sufficient alone** — see
`02-extraction-gotchas.md`'s `name_map` backfill fix, found immediately after verifying THIS fix
live and discovering the right table still didn't have a matching NAME for the field. Both are
needed together any time the same caption exists across 2+ datasources with inconsistent
captioning. **Verified live**: `'Activation'[For View]` now carries its own real domain
(`"Trend"` included); the real card value resolves to `0.0875`, not blank.

### Same hazard, one level up: `_query_state` had NO table-preference logic AT ALL — only `_visual_filters` did
The fix two rows above (`candidate_tables`) only ever threaded through `_visual_filters`, for a
FILTER's own field. `_query_state` — the method that resolves a worksheet's OWN Rows/Cols/Colour/
Tooltip/Text-shelf fields, i.e. the visual's primary data binding, not just its filters — called
`_resolve_field` with NO table hint anywhere, at all three of its own call sites. A worksheet's
Category/axis field is just as exposed to the SAME caption-collision hazard as a filter's field;
nothing before this fix protected it. Real corpus (GCLM), confirmed live: fixing
`extract.py`'s own caption gap (see `02-extraction-gotchas.md`) widened the caption "Prod Id" to 5
tables at once; "2.3 Issuer YoY Growth"'s barChart then resolved its Category field to
`Activation.Prod Id` (global first-match) while its Y measure stayed on `Acquisition.Issuer - YoY
Growth` — two different tables for ONE worksheet's own shelves, confirmed via the visual's own
`visual.json`. **Fix:** `_query_state` now pre-scans ALL of a worksheet's own encodings once, up
front, collecting the table(s) any MEASURE resolves to (a measure caption rarely collides the way
a plain dimension caption does, and there's no built query state yet to seed the hint from, unlike
the filter case) — then threads that as `candidate_tables` through every one of its own three
`_resolve_field` call sites (the primary Rows/Cols loop, the Colour/Size/Tooltip EXTRA_WELLS loop,
and the Text-shelf fallback). **Status:** ✅ Fixed and self-tested (both the collision-preference
case and a no-measure-to-anchor-on no-regression case). **Confirmed this fix is correct in
isolation** (the visual's Category and Y fields now both resolve to `Acquisition`, matching) —
**but this did NOT fix the real Desktop symptom.** The barChart STILL renders zero bars (category
labels only, no value marks, no Desktop error) after this fix. See
`01-orchestrator-gotchas.md`'s "fresh rebuild renders several barChart visuals with zero bars, no
error" entry — this fix is necessary (both Category and Y belonging to the same, correct table is
clearly right) but is not, on its own, sufficient to explain or fix the blank-render symptom.

### `_resolve_field`'s `if agg:` branch (Tooltip/Color/Text aggregated fields) never used `candidate_tables` AT ALL
A THIRD occurrence of the same global-first-match hazard the two entries above already fixed for
`_visual_filters` and for `_query_state`'s Rows/Cols/EXTRA_WELLS/Text-fallback loops — but those
fixes only ever threaded `candidate_tables` into `_resolve_field`'s NON-agg branch (the plain
`for cand in candidate_tables: by_spelling(...)` loop). The SEPARATE `if agg:` branch — reached
by ANY truthy aggregation code, not just `SUM`/`AVG` shelf-measure lookups, critically including
Tableau's `ATTR()` wrapper (`agg="attr"`), the common case for a plain dimension dropped on
Tooltip/Color/Text — called `self.na.by_spelling(field, emitted_only=True)` with NO table hint
at all, unconditionally. Real corpus (GCLM): `_query_state`'s own `anchor_tables` correctly held
`("Acquisition",)` for "2.3 Issuer YoY Growth"'s barChart (confirmed via the SAME worksheet's Y
measure resolving correctly) — the chart's Tooltip field (`Issr`, `agg="attr"`) STILL resolved to
`Recommendations.Issr` instead, because the agg branch never consulted `anchor_tables` in the
first place. Combined with a same-session new `Acquisition↔Activation` relationship chaining
through the pre-existing zero-overlap `Activation↔Recommendations` relationship, this forced the
WHOLE VISUAL to zero rows under any Acquisition filter (confirmed via Performance Analyzer's own
captured DAX query showing `RowCount: 0`) — not wrong values, the entire chart empty. **Fix:**
the `if agg:` branch now tries `by_spelling(field, table=cand, emitted_only=True)` for each
`candidate_tables` entry FIRST, falling to the pre-existing global `by_spelling(field,
emitted_only=True)` only when none match — mirroring the non-agg branch exactly, including
re-applying the same scoped-then-global order to the paren-stripping fallback. **Status:** ✅
Fixed and self-tested. **If you ever add a new `_resolve_field` call site or a new early-return
branch inside it, check whether it uses `candidate_tables` — this exact class of bug (a NEW
branch silently skipping the existing scoping mechanism) has now recurred twice.**

### `_query_state`'s `anchor_tables` seed is empty for a worksheet with ZERO measures anywhere
The `anchor_tables` mechanism (see the two `_query_state` entries above) is seeded ENTIRELY from
measures found in a worksheet's own encodings — a worksheet that is a pure dimension/label
display (e.g. "show this view's own `Ctry` as text," no measure on any shelf at all) gives that
scan nothing to find, so `anchor_tables` stays `()` and every field in it falls straight through
to the unscoped global fallback regardless of any of the fixes above. Real corpus (GCLM): 98 of
382 worksheets have this zero-measure shape; confirmed on `Mkt_acti`'s own `Ctry` field (`agg:
None`, the ONLY encoding on the whole worksheet) landing on `Recommendations.Ctry` instead of
`Activation.Ctry`. **Fix:** `extract.py`'s `extract_worksheets()` now captures each worksheet's
own declared Tableau datasource(s) as `own_datasources` (`<table><view><datasources><datasource
name='federated.XXXX'>`, the SAME raw key `na.table()` is allocated under — mirrors the
dashboard-zone `zone["param"]` fix in `_visual`, just at worksheet scope instead of zone scope).
When the measure-seeded `anchor_tables` scan comes back empty, `_query_state` now falls back to
resolving `own_datasources` via `self.na.tables.get(key)` before giving up on scoping entirely.
**Status:** ✅ Fixed and self-tested in both `extract.py` and `pbir.py`. **General signal for a
NEW workbook:** any worksheet whose `encodings` are ALL `agg: None` (grep `_work/ir.json` for
this shape) is a candidate — before this fix, 100% of such worksheets' fields resolved by
unscoped global caption match, correct only by accident when the caption happened to be unique
workbook-wide.

### A discrete date/dateTime categorical filter had no confirmed PBIR shape — now declines
Unlike string/int64/double/boolean (each confirmed against real Desktop or in the bug log), a
date-valued filter's correct PBIR literal shape was unconfirmed. **Fix:** DECLINE rather than
guess — zero real instances exist across the corpus so far, so this costs nothing today and
avoids risking Desktop refusing the whole project on an unconfirmed shape. **Status:** ✅ Declined
on purpose, matching this pipeline's own fail-closed rule (not a translate-fix).

### `_filter_literal` decided string-vs-numeric SHAPE from the VALUE's Python `type`, not the column's `dataType`
Every Tableau filter value reaches `extract.py` as a raw XML attribute — always a Python `str`,
even for an `int64`/`double`/`boolean` column. `isinstance(v, int)` was therefore ALWAYS False;
the correctly-written numeric branch was dead code. A real filter on an int64 column degraded to
the quoted string literal `'1'` instead of `1L` — schema-valid PBIR, type mismatch against the
real column, "This visual contains one or more filters with... type mismatches," visual refused
to render. Measured AFTER finding it: 111 filters in one workbook alone. **Fix:** resolve the
column's real `dataType` (a field index built in `ReportBuilder.__init__`) and let `dtype`
override the value's Python type. **Status:** ✅ Fixed, with a `preflight()` backstop
(`_tmdl_column_types` + the filter-literal-type check). The identical pattern was pre-emptively
found and fixed in `tmdl.py`'s parameter-table writer too (a boolean parameter domain value is
also a `str`, `"true"`/`"false"`, not a Python `bool`) while auditing for the same shape
elsewhere — no workbook has hit that one yet, fixed before it could.

---

## Visual type mapping

### Mark-class rule table checked `m == "Gantt"` — Tableau's XML never writes that
Tableau's own XML writes `<mark class='GanttBar' />`, never `'Gantt'`. All 6 real corpus Gantt
marks silently fell to `tableEx` undecided — invisible unless someone read `RESIDUE.md` and
independently knew the real mark name. Worse: the Synthetic Coverage Probe's OWN test fixture
made the IDENTICAL wrong assumption, so running the project's own defence against exactly this
failure mode CONFIRMED the bug instead of catching it. **Fix:** corrected the rule, the fixture,
AND added a direct case to `pbir.py`'s own `cases` list so the rule is asserted without a full
build. **Status:** ✅ Fixed, found by grepping real `.twb` mark values and diffing against the
rule table — not by reasoning about what Tableau "probably" writes. If you ever add a new mark
rule, grep real corpus XML for the literal `class='...'` value FIRST.

### `GanttBar` + `size` encoding is Tableau's floating-bar WATERFALL trick, not a literal timeline
Both real `GanttBar` worksheets in the whole fleet use the well-known floating-bar idiom (a
`size` encoding controlling bar length, fields literally named `Waterfall Values`/`Waterfall
Step`) to fake a chart type Tableau has no native mark for. Mapping `GanttBar` unconditionally to
`ganttChart` would have been CONFIDENTLY WRONG for 100% of real evidence. **Fix:** `GanttBar` +
`size` encoding → `waterfallChart` (wells confirmed against the real fields); plain `GanttBar`
still falls to `ganttChart` but deliberately wired with NO wells (zero real literal-timeline
examples exist to bind role names against). **Status:** ✅ Fixed — investigated the real data
before wiring wells blindly, since the original plan assumed a literal-timeline reading.

### `_query_state` had NO field-count cap on any well except card's `Values` (a one-off)
A `pieChart`/`waterfallChart`/`treemap` well that real Desktop caps at 1 field had no such cap in
EITHER of `_query_state`'s two code paths — found via `pbip_lint.py --schemas` on a workbook
OUTSIDE the original reference corpus: 5 `pieChart` visuals failed with "role 'Category' has 2
fields but the field well accepts 1." Root cause: a declined Rows measure forced the Text-shelf
fallback path, which bound TWO plain dimension columns into the pie's `Category` well, because
that fallback had never been taught ANY per-visual-type cap, only card's. **Fix:** `ROLE_CAPS`
(`{"card": {"measure": 1}, "pieChart": {"dim": 1, "measure": 1}, ...}`, mirroring
`tools/pbip_rules/checks.py`'s own `_ROLE_LIMITS`), read from BOTH `_query_state` code paths
uniformly, keyed by role KIND not the literal role string — replacing the old card-only special
case entirely. **Status:** ✅ Fixed, with a `preflight()` backstop reading `pbir.ROLE_CAPS`/
`WELLS` against the emitted `visual.json`.

### `_shelf_shape`/`_query_state` only ever read Rows/Cols — missed the Text-shelf BAN idiom
A worksheet using Tableau's Text-shelf-only "BAN"/card idiom, or a bare parameter placed directly
on a shelf as a dynamic header label, read as "0 dimensions, 0 measures" and bound NO field at
all. On one real workbook: 239 Text-channel encodings against only 50 Rows/Cols — the MAJORITY
shape in that workbook, not an edge case. `pbip_lint.py`'s RPT-ROLE-02 found 142 unlabeled blank
visuals from this in that workbook alone. **Fix:** fall back to the worksheet's Text-shelf
encodings in BOTH functions, ONLY when Rows/Cols are BOTH empty (so a real chart with a tooltip/
text label is never touched); `param_index` keyed by both caption and internal name so a bare
`[Parameter]` on a shelf resolves to its `SELECTEDVALUE` measure; card's Values well capped at
exactly 1 field (Desktop's real limit) for Tableau's BAN idiom stacking several text objects into
what becomes one card. **Status:** ✅ Fixed, with a `preflight()` RPT-ROLE-02 backstop
(independent of `pbip_lint.py`'s external check).

### Systematic chart-type audit found `Square` mark with ≥1 measure and 0 dimension → `card`
Found while deliberately auditing for ANY unmapped chart type, not reacting to a report. **One
further finding, left UNFIXED on purpose:** `Square` mark with neither a colour encoding nor a
clean shelf shape (28 instances, 3 distinct degenerate shapes) has NO confident target, because
`_shelf_shape()` doesn't inspect Color/Size/Detail encodings — guessing here would be exactly the
"silently wrong is worse than missing" case this project exists to avoid. **Status:** ⚠️ Partially
fixed — the clean case is mapped; the ambiguous one stays declined deliberately. Don't "fix" the
ambiguous case without a real signal to rule on.

---

## Fonts

### Textbox rich-text runs never got `fontFamily` — paired with `extract.py`'s capture fix
`fontSize`/`bold`/`italic`/`color` all threaded through a text run correctly; `fontFamily` was
emitted exactly 0 times corpus-wide because `extract.py` never captured `fontname` in the first
place (see `02-extraction-gotchas.md`). **Fix:** once captured, `pbir.py`'s text-run `textStyle`
builder maps it to `fontFamily` the same way `fontsize`→`fontSize` already works — a plain
string, NOT the `objects.*.properties` literal-expr shape `cardVisual`'s own font properties use
(different PBIR schema location). **Status:** ✅ Fixed. Covers only `textbox` visuals.

### A `card` visual's base label font was never mapped at all
Paired with `extract.py`'s `_label_font()` capture (see `02-extraction-gotchas.md`). **Fix:** a
`card`'s own `label_font` maps onto `objects.value.properties.fontSize`/`fontFamily` as literal
expressions (`"{size}D"` / `"'{family}'"`), scoped to `vt == "card"` only. **Status:** ✅ Fixed.
**Still genuinely unextracted (scope gap, not a bug):** `tableEx`/chart/slicer fonts — no
Tableau style-rule scope has been mapped for these visual types yet.

---

## Other

### PBIR bound 4 visuals to a declined measure
A name could be ALLOCATED by the Name Authority with no TMDL object actually behind it (the
formula parser declined it after allocation). PBIR binds by name with no indirection, so binding
to such a name is a dangling reference Desktop reports as a broken visual with no clue where it
came from. **Fix:** `_resolve_field` only trusts `emitted_only=True` lookups throughout.
**Status:** ✅ Fixed — this is WHY every `by_spelling`/`lookup` call in this pipeline passes
`emitted_only=True`; don't remove that flag from a new call site.
