# DAX translation gotchas (`dax.py`)

The Tableau formula → DAX recursive-descent parser. **Fails closed on purpose**: anything not
cleanly convertible is DECLINED with a reason, never approximated — a silently-wrong measure is
worse than a missing one. Several entries below are "now declines correctly" rather than "now
translates" — read the Status line carefully. Full detail: `SKILL.md`'s bug table.

---

## Function-mapping bugs (wrong/missing DAX equivalent)

### `STARTSWITH` mapped to `LEFT` — silently wrong result
`LEFT(s, sub)` is not what Tableau's `STARTSWITH` means. **Status:** ✅ Fixed with the correct
DAX pattern.

### `FLOOR`/`CEILING`/`ROUND` emitted with only 1 argument
Tableau makes the second argument optional; DAX requires it — fails at model load. **Fix:**
`OPTIONAL_SECOND_ARG` supplies DAX's implied default (the value that reproduces Tableau's
1-argument behavior) when Tableau's own call omits it. **Status:** ✅ Fixed.

### `LEFT`/`RIGHT` missing from the function map entirely
6 real calcs declined as "no DAX equivalent" when DAX has had `LEFT`/`RIGHT` all along, identical
name and signature in both languages. **Status:** ✅ Fixed — simple map entries added.

### `DISTINCTCOUNT(IF(...))` emitted verbatim — invalid DAX
DAX aggregates take a COLUMN reference, not an expression; Tableau's `COUNTD` takes either.
**Fix:** rewritten to the iterator form (`COUNTROWS(DISTINCT(SELECTCOLUMNS(...)))`) where exactly
one table is unambiguous, declined otherwise. **Status:** ✅ Fixed.

### Tableau's `+` mapped unconditionally to DAX `+` — wrong for text
Tableau overloads `+` for text concatenation; DAX's `+` is ALWAYS arithmetic. A label-building
formula (`COALESCE(a,"") + "|" + COALESCE(b,"")`) is syntactically valid DAX that fails at QUERY
time, not model load — invisible to every structural check. **Fix:** a string literal directly
touching either side of `+` signals textual intent, rewriting to `&`; once ANY `+` in a
left-associative chain is textual, every later `+` in that SAME chain becomes `&` too (DAX can't
drop back to numeric mid-chain). Deliberately narrow: a chain with NO literal anywhere (two text
COLUMNS added directly) isn't caught — no reliable per-operand type signal without threading a
type through the whole parse, and guessing wrong would flip a working `+` into a broken `&`.
**Status:** ✅ Fixed for the literal-adjacent case, with a `preflight()`/`pbip_lint.py` backstop
(`check_literal_plus_concatenation`) for the shape this fix guarantees never to produce.

### `TYPE('NULL')` idiom (`FLOAT('NULL')`, `INT('NULL')`, `STR('NULL')`, ...) had no mapping at all
Tableau's documented construct for an explicitly-TYPED null, used to satisfy branch-type-
unification in an `IF/THEN/ELSE`. A function with no DAX equivalent (`FLOAT`) declined outright;
an already-mapped one (`INT`, `STR`) would have silently mishandled it. **Fix:** any single-arg
call whose argument strips down to exactly the text `NULL` (case-insensitive) → `BLANK()`,
checked at the very top of `func_call`'s dispatch — before ANY per-function special case, since
several of those (STR, DATE) have their own earlier branch that would otherwise intercept and
mishandle this shape first. **Status:** ✅ Fixed (found on WorkbookA). **Verified live**, not just
unit-tested: the real downstream formula correctly evaluates to `BLANK()` under the real
parameter branch that selects it.

---

## Aggregate-of-expression bugs

### Aggregate of a BARE reference that resolved to a promoted MEASURE
`SUM([x])` where `x` has no aggregate of its own and Tableau treats it as row-level — but
`tmdl.py`'s promotion fixpoint turned `x` into a DAX MEASURE elsewhere (forced by a parameter
reference or a downstream aggregate depending on it). DAX forbids an aggregate function from
taking a measure argument; the old code didn't know the resolved kind at parse time and emitted
invalid DAX. Confirmed corpus-wide: 135 instances across 3 of 6 reference workbooks before the
fix. **Fix:** a `resolve_kind` callback threaded through `translate`/`Parser` lets `func_call`
DECLINE this shape explicitly with a clear reason, instead of guessing an inlining that risks
being wrong across chained/nested cases. **Status:** ✅ Fixed as a DECLINE, not a translate — by
design (see this pipeline's fail-closed rule). A `preflight()`/`pbip_lint.py` backstop
(`check_aggregate_wraps_measure`) exists for the shape this fix guarantees never to produce.

### Aggregate of a bare PARAMETER-measure reference wrongly hit the same decline
A well-known Tableau idiom — wrap a parameter in `AVG`/`MIN`/`MAX` (deliberately never `SUM`) to
make a reference to a workbook-wide scalar "aggregation-safe" inside a calc that must itself be
an aggregate. This is a DIFFERENT shape from the row above (a genuine parameter, not a promoted
column-turned-measure) but hit the SAME "0 tables to iterate" decline the pre-existing
bare-numeric-literal collapse already handled, just never extended to this second 0-table shape.
A true parameter has NO row-level materialization at all — unlike a numeric literal, which
Tableau's own `SUM` genuinely multiplies by whatever row count is in context, a parameter has no
row count to multiply by, so even `SUM` of one is a safe identity (one case wider than the
literal rule). **Fix:** `_BARE_MEASURE_REF` (`^\[[^\[\]]+\]$`, `resolve_param`'s own exact
no-table-prefix shape — a real column reference always carries a `'Table'[...]` prefix once
resolved, so this can't false-positive on one) extends the existing 0-tables collapse branch.
**Status:** ✅ Fixed (found on WorkbookA). **Verified live**: the exact stored formula evaluates to a
real nonzero number (`240248.8`), not blank/error.

### `SUM`/`AVERAGE` of a Tableau-STRING-typed column, inside an explicit calc formula
`AVERAGEX('T', ('T'[strnum]))` with no cast reached TMDL as syntactically valid DAX that fails
only at QUERY time ("cannot work with values of type String"). **Fix:** a redundant-paren-wrapped
bare column is first unwrapped, then a string-typed column aggregated by `SUM`/`AVERAGE` casts
via `IFERROR(VALUE(...), BLANK())`; `MIN`/`MAX`/`MEDIAN` on a string field decline rather than
guess comparison semantics. **Status:** ✅ Fixed — this is the EXPLICIT-calc-formula half of the
fix; the IMPLICIT shelf-measure half (`SUM of <field>` with no calc behind it) is a SEPARATE fix
in `tmdl.py`'s `_plan_shelf_measures` — see `05-semantic-model-gotchas.md`. Both are needed; one
does not imply the other, because they're two different code paths resolving the field
differently.

### `is_aggregate()` false-positived on a bracketed field NAME containing a function-like substring
Same shape as `extract.py`'s `classify_formula` bug (see `02-extraction-gotchas.md`) — a field
literally named `[Card Count (copy)_...]` matched `_AGG_CALL`'s `\bCOUNT\s*\(` regex even though
"Count (" here is part of a NAME, never an invocation; a real function call is always OUTSIDE
`[...]` brackets. **Fix:** strip bracketed field references before matching, same as the
string-literal strip. **Status:** ✅ Fixed independently in both files (same bug, two call
sites — fix both if you ever touch either regex again).

### `is_aggregate()` treated a `{FIXED...}` LOD's internal `SUM(` as making the WHOLE formula aggregate
An LOD expression closes its own aggregation and returns row-level — but the regex matched the
`SUM(` INSIDE the `{...}` block, wrongly promoting a row-level calc to a measure with a bare
unaggregated column reference. Severe: one broken calc aborts a WHOLE TABLE's Full refresh,
cascading through every blend-key-related table. **Fix:** strip `{...}` LOD blocks before the
aggregate-call regex (`_STRIP_LOD`), same reasoning as the bracket-strip above — a genuinely
SEPARATE outer aggregate (`{FIXED[k]:SUM([x])} + SUM([y])`) still correctly registers as
aggregate. **Status:** ✅ Fixed (found on WorkbookA).

### `DATE(MIN(DATEPARSE(...)))` wrongly declined for a string-returning containing calc
`DATE()`'s redundant-cast removal only trusted a bare field's resolved type or the CONTAINING
calc's own `own_type` — never the argument's OWN call shape. A calc that builds a STRING label
but only uses `DATE(MIN(DATEPARSE(...)))` internally to extract a date for `DATENAME` formatting
(`own_type` = `"string"`, not `"date"`) always declined, even though every piece of information
needed to prove the ARGUMENT (not the whole calc) is date-typed was in its own source shape
(`DATEPARSE` is already proven date-returning; `MIN`/`MAX` of ONE expression is type-preserving).
**Fix:** `_is_date_returning_call` now also recognizes a single-argument `MIN`/`MAX` wrapping a
(recursively) date-returning call — deliberately excluding Tableau's 2-argument `MIN(a,b)`
"smaller of two values" shape, a different operation this must not guess at. **Status:** ✅ Fixed
(found on WorkbookA). **Verified live**: the exact stored formula evaluates to a real date-formatted
string (`"Mar'22 - Jan'23"` embedded in the full label).

---

## Date/time bugs

### `DATEPARSE` had no graceful-NULL handling — hard VALUE() errors on bad input
Tableau's own `DATEPARSE` returns NULL (not an error) for a source string that's blank, too
short, or non-numeric at a numeric token's position; DAX's `VALUE()` hard-errors on exactly that
input, which fails the WHOLE COLUMN's load, not just one cell. **Fix:** every `DATEPARSE`
rewrite wraps in `IFERROR(..., BLANK())`. Offsets are computed FROM THE FORMAT STRING
(locale-independent), not guessed — `MM/dd/yyyy` and `dd/MM/yyyy` correctly produce DIFFERENT
day/month positions. A day-of-month OMITTED from the format defaults to 1, per Tableau's own
documented behavior and matching DAX's own `DATE()` constructor convention. **Status:** ✅ Fixed.

### `STR(<computed date/integer-returning call>)` trusted an unreliable `last_field`
`STR(YEAR([Date]))` and `STR([Date])` both leave `last_field` pointing at `[Date]`, but only the
second actually hands STR a date — the first hands it YEAR's return value, a plain integer.
Applying a date FORMAT pattern to that is actively wrong (DAX treats a bare number with a
date-style FORMAT as an Excel-style date serial). **Fix:** `_is_date_returning_call`/
`_is_integer_returning_call` prove the argument's type from its OWN call shape (DATE/DATEADD/
DATETRUNC/DATEPARSE vs. YEAR/MONTH/DAY/DATEDIFF/DATEPART), not from `last_field`. **Status:** ✅
Fixed.

### `_param_literal` had no date/dateTime branch — a date parameter fell through to quoted-string
Normalizing the parameter COLUMN's type to `datetime` was correct, but the domain VALUE literal
(Tableau text like `"2026-05-28"`) still went through the generic quoted-string fallback — "the
type of the value does not match the type of the column" in Desktop. **Fix:** parse the ISO
date/datetime text and emit a real `#datetime(y,m,d,h,mi,s)` M literal. **Status:** ✅ Fixed, with
a backstop in both `preflight()` and `pbip_lint.py` for the exact broken shape.

### `DTYPE` mapped a Tableau date-only field to TMDL `dataType: date`
`date` is schema-valid TEXT but NOT a member of TOM's real `DataType` enum (only `dateTime`
exists) — "Failed to convert the value 'date' to the expected type DataType!", a hard model-load
failure for the WHOLE project. **Fix:** `tmdl.py`'s `_tom_dtype()` normalizes at every
serialization point, deliberately NOT at the source (`extract.py`'s `DTYPE` stays `date`
internally, since `dax.py`'s date-vs-datetime handling still needs that distinction).
**Status:** ✅ Fixed, with a `preflight()` backstop reading the emitted TMDL's `dataType:` enum.

---

## Measure-promotion / column-vs-measure bugs

### `cl.Slice By`-style promoted measure needed `SELECTEDVALUE`, not a bare column reference
A pure branch-selection `IF(cond, 'T'[ColA], IF(cond, 'T'[ColB], ...))` is legal as a calculated
COLUMN (row context makes each branch unambiguous) but illegal as a MEASURE (no row context at
the top level) — "a single value for column... cannot be determined". **Fix:** `_resolver`
gained `resolve_measure_safe`, wrapping a plain COLUMN resolution in `SELECTEDVALUE(...)` and
leaving a MEASURE resolution bare, used specifically for a calc being promoted to a measure that
has no aggregate function of its own. **Status:** ✅ Fixed, verified against ~450 false positives
from a first naive output-scan attempt (discarded) before landing on a direct unit test of
`resolve_measure_safe` itself.
