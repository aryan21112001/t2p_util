# Name Authority gotchas (`names.py`)

`names.py` allocates every PBI identifier (table/column/measure names) BEFORE either `tmdl.py` or
`pbir.py` runs, and resolves references back to those allocations by spelling (caption) or
internal Tableau name. Full detail: `SKILL.md`'s bug table, same phrases quoted below.

---

### Two literal NUL bytes in the file caused an `ImportError`
Never caught because nothing had imported `names.py` yet when it happened — a build-breaking typo
with zero diagnostic value in the error itself. **Status:** ✅ Fixed (removed). Mentioned here
only because it's the simplest possible example of "found by running it, not by reading it."

### Two calcs sharing the same CAPTION collapsed onto one authority key
The authority was keyed by caption; two different Tableau calcs that happen to display the same
name landed on the same key, producing **6 duplicate members in one table** — TOM forbids this,
the model would not load. **Fix:** the authority now keys on Tableau's internal name, never the
caption. **Status:** ✅ Fixed.

### `by_spelling`'s resolver index was GLOBAL first-match, with no per-table scoping
When several near-duplicate datasources define the same field name, the resolver had no way to
prefer the "right" one — a calc in the 4th of 4 near-duplicate datasources bound to the 1ST one's
column: valid DAX, silently reading the WRONG table's data. **Fix:** `by_spelling` now accepts an
optional `table=` preferred-lookup hint, tried before the global fallback (which still exists and
is still the right behavior when no caller has a better hint — a hint that doesn't resolve
locally falls through cleanly, see `04/06`'s notes on `candidate_tables` for how a caller supplies
one). **Status:** ✅ Fixed for calc resolution. **Note:** this same global-first-match hazard
recurred INDEPENDENTLY in `pbir.py`'s filter-resolution call site years later (that fix never
touched `_resolve_field`'s own call) — see `06-report-pbir-gotchas.md`'s `candidate_tables` entry.
If you ever add a NEW caller of `by_spelling`, pass a table hint from day one; don't assume the
global fallback is "probably fine" just because it compiles.

### Collision check (`_unique()`, `measure()`) compared names CASE-SENSITIVELY
TOM's own name collection ignores case (Windows/SQL Server convention) — a real corpus workbook
had a physical column `Sample Check Alpha` and a calculated-field copy captioned
`SAMPLE CHECK ALPHA ` (differing only in case + a trailing space `sanitize()` already strips).
Two different Python strings slipped past the collision check, and Desktop refused to **open the
file at all**: "Item 'SAMPLE CHECK ALPHA' already exists in the collection." Worse than a wrong
visual — a hard load failure, and it passed preflight with 0 FAIL because preflight's own
duplicate check was ALSO case-sensitive. **Fix:** collision resolution in `names.py` now folds
case before comparing (table names, columns-per-table, measures model-wide); `preflight()` gained
a matching case-insensitive backstop. **Status:** ✅ Fixed, with a structural backstop (not just
a generator fix) so a future regression here fails the BUILD, not just produces a bad one.
