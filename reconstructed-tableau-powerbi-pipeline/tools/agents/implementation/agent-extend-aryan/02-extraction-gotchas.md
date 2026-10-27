# Extraction gotchas (`extract.py`)

Covers reading a Tableau `.twb`/`.twbx` (plus sibling `.tds`/`.tdsx`) into this pipeline's own IR
(`_work/ir.json`). Full detail for every entry: `.claude/skills/build-tableau-pbip/SKILL.md`,
section "Bugs the pre-flight and the reference corpus caught" — search for the quoted phrase.

---

### `unpack()` decided ZIP vs. raw XML from the FILE EXTENSION, not the actual bytes
A real `.twbx` was on disk as plain `<workbook>` XML with no zip wrapper at all — the build
aborted outright (`BadZipFile`) before extraction even began, for a perfectly legitimate input.
**Fix:** switched to `zipfile.is_zipfile(src)` (a real content sniff) instead of
`.endswith(".twb")`. **Status:** ✅ Fixed.

### Metadata-record backfill never derived a human caption for a column with no `<column>` tag
A column visible ONLY via `<metadata-record>` (no explicit `<column>` tag in ITS OWN datasource)
got stored under its raw internal name (`"for_view"`, lowercase) — but every filter/shelf
reference to that same raw name, anywhere in the workbook, resolves its DISPLAYED caption through
a workbook-GLOBAL `name_map`, which could return `"For View"` (borrowed from a totally different
datasource that happens to give the same raw field an explicit caption). Real corpus (GCLM): this
affected `Activation`/`Dormancy`/`Key Highlights`/`Operations Management`/`Usage Deepdive`/`Usage
Overview` — 281 real filter sites hit it for one field alone. **Fix:** `extract_datasource` now
takes an optional `name_map` parameter and consults it in the metadata-record backfill loop,
falling back to the bare raw name only when `name_map` has no entry at all. **Status:** ✅ Fixed.
Paired with a `pbir.py` fix — see that file's "cross-table filter resolved by caption alone" entry.

### Same hazard, one level up: an EXPLICIT `<column>` tag with no caption of its own skipped `name_map` too
The row above fixed the metadata-record-ONLY backfill path, but a field that DOES have a real
`<column>` tag in its own datasource — just with no `caption=` attribute on that tag — hit the
identical bug one code path over: `caption = c.get("caption") or (name or "").strip("[]")` fell
straight to the bare raw name, never consulting `name_map`, even though the metadata-record path
right below it in the same function already did. Real corpus (GCLM): confirmed live against a
real rebuilt Desktop model. `Acquisition`'s own `[prod_id]` column has an explicit `<column
name='[prod_id]' ... />` tag with NO caption; `Key Highlights`' sibling datasource captions its
OWN `[prod_id]` as `"Prod Id"`. Left bare, Acquisition's column stayed named `prod_id` while every
worksheet's Category/axis field well captioned "Prod Id" resolved globally (`by_spelling`) to Key
Highlights' column instead — an unrelated table with NO working relationship to Acquisition at
that grain (confirmed via `INFO.RELATIONSHIPS()`/`pbi relationship list`: zero relationship rows
exist between those two tables in the loaded model at all). Net effect: "2.3 Issuer YoY Growth"
and "2.3 Market YoY Growth" rendered the IDENTICAL value for every one of 13 real `Prod Id`
categories — confirmed via `SUMMARIZECOLUMNS('Key Highlights'[Prod Id], "YoY", [Issuer - YoY
Growth])` returning `0.06002596424510865` for all 13 rows, and confirmed the measure's own DAX
(`DIVIDE(SUM(...), SUM(...))`) was completely ordinary — the bug was never in the DAX, only in
which TABLE's column the visual's field well resolved to. **Fix:** `caption = c.get("caption") or
(name_map or {}).get(name) or (name or "").strip("[]")` — the explicit-`<column>` loop now
consults the SAME workbook-global `name_map` the metadata-record backfill loop already does, one
line up. **Status:** ✅ Fixed and self-tested, ✅ live-verified the DAX-level symptom is actually
gone (`SUMMARIZECOLUMNS(Acquisition[Prod Id], "YoY", [Issuer - YoY Growth])` now returns 6 real
distinct values instead of one repeated across every row). If you hit this gotcha again, check
`pbi relationship list` for the two tables first — a zero-relationship-rows result with everything
else looking correct is the fastest tell.

**But fixing this surfaced a SECOND, still-OPEN problem — do not ship this fix alone.** Giving
`Acquisition`'s own `prod_id` its missing caption widened "Prod Id" to a caption now shared by 5
tables workbook-wide (Key Highlights, Activation, Acquisition, Usage Overview, Usage Deepdive all
happen to have a `prod_id`-shaped column). That immediately broke a DIFFERENT field-well
resolution — `pbir.py`'s `_query_state`, which had NO table-preference logic at all before this
(see `06-report-pbir-gotchas.md`'s own entry for that half of the fix) — and even after fixing
THAT too, the real Desktop symptom (several barChart visuals on the Acquisition page render zero
bars, no error, category labels only) is STILL NOT RESOLVED as of this entry. Confirmed via a
controlled A/B test: reverting ONLY the Category field's table back to the pre-fix binding
(`Key Highlights` instead of `Acquisition`), with every other fix left in place, did NOT bring
the bars back — ruling out the caption-collision theory as the (sole) cause of the blank-render
symptom. See `01-orchestrator-gotchas.md`'s "fresh rebuild renders several barChart visuals with
zero bars, no error" entry for the full open-investigation writeup. **Net effect: this fix is
correct and self-tested, but is NOT currently safe to ship into `out2` on its own** — the canonical
deliverable stays on its pre-fix state (same-value bug, but every chart renders) until a rebuild
exists that has this fix AND working chart rendering together.

### Metadata-record backfill skipped `class='measure'` records, only handled `class='column'`
A real physical column with NO `<column>` override, visible only via a `class='measure'`
metadata-record, never reached `rec["columns"]` — starving object-graph relationships of a real
key attribution and collapsing a federated datasource's split into one flattened table instead of
the correct multiple. **Fix:** backfill now covers both `class='column'` and `class='measure'`.
**Status:** ✅ Fixed.

### `classify_filter`: Tableau's `"(All)"` sentinel emitted as a literal filter value
A lone `member='"(All)"'` member filter is Tableau's own "show every value" sentinel, not a real
domain value — but it was being emitted as a literal `kind: "in"` filter matching ZERO real rows,
silently zeroing out every visual that filtered through it (plus anything blend-key cross-filtered
from it). Real corpus (GCLM): ~74 of 156 cards affected. **Fix:** a single-member value of exactly
`"(All)"` now classifies as `kind: "none"` (restricts nothing), the same treatment `level-members`
already got. **Status:** ✅ Fixed.

### `classify_formula()`/`is_aggregate()` matched a function call inside a bracketed FIELD NAME
A raw regex for an aggregate/table-calc function call ran over the formula text after stripping
only STRING literals, never bracketed `[...]` field references — so a field literally named
`[Card Count (copy)_...]` (containing the substring `"Count ("`) false-positived as a `COUNT(`
call, even though a bracketed field name can never legally contain a real function call. Real
corpus: 3+ workbooks, several fields silently mis-promoted to measures or mis-tiered to table-calc.
**Fix:** strip `\[[^\]]*\]` (bracketed field refs) from the formula text before running the
aggregate/table-calc regex, same place the string-literal strip already happens. **Status:** ✅
Fixed, in both `extract.py`'s `classify_formula` and `dax.py`'s `is_aggregate` independently (same
bug, two call sites — see `04-dax-translation-gotchas.md` too).

### `is_aggregate()`-adjacent: a `{FIXED...}` LOD's internal `SUM(` promoted the WHOLE formula
A Tableau LOD expression (`{FIXED [k]: SUM([x])}`) closes its own aggregation and returns
row-level — but the aggregate-call regex matched the `SUM(` INSIDE the `{...}` block and wrongly
treated the entire containing formula as aggregate, which (via `tmdl.py`'s promotion fixpoint)
turned a row-level calc into a measure with a bare unaggregated column reference. Severe because a
Full table refresh recalculates ALL calculated objects — one broken calc aborts the WHOLE TABLE's
refresh, cascading through every blend-key-related table. **Fix:** strip `{...}` LOD blocks before
the aggregate-call regex, same reasoning as the bracket-strip above. **Status:** ✅ Fixed (lives in
`dax.py`'s `is_aggregate`, triggered by `extract.py`'s own formula classification feeding into it).

### `extract_dashboards` walked the WHOLE `<dashboard>` subtree, including auto-generated device layouts
`db.iter("zone")` pulled in every zone under `<devicelayouts>` too — Tableau's auto-generated
Phone/Tablet layouts, a COMPLETE second copy of the same zone tree authored against a different
canvas size. Every worksheet on an affected dashboard got emitted TWICE, the duplicate positioned
using the PRIMARY canvas's dimensions against Phone-canvas-authored coordinates — landing
off-canvas or negative. **Fix:** scoped to `db.find("./zones")` only, never
`<devicelayouts>`. **Status:** ✅ Fixed. **Caveat still open:** a few genuinely-authored small
negative `x`/`y` values on PRIMARY-tree decorative zones (Tableau permits a deliberate off-canvas
bleed; PBIR requires non-negative coordinates) are a separate, still-unfixed root cause — not this
bug, don't conflate the two if a negative-position finding shows up again.

### A physical-join operand with no `name`/`table` attribute crashed the WHOLE build
Tableau nests a join inside a join for a 3+-way physical join; the nested-join child has neither
attribute. `c.get("name") or c.get("table")` returned `None`, and the residue-message
`' ~ '.join(rel['operands'])` call in `tmdl.py` crashed with `TypeError` — not a bad translation,
a build that produces NO output at all. **Fix:** unnamed nested-join operand gets a descriptive
fallback label (`"(nested <kind> join)"`) instead of `None` — pure residue text, not a translation
decision. **Status:** ✅ Fixed.

### `parse_formatted_text()` never captured a text run's `fontname` attribute
Tableau's `<run fontname='...'>` was read nowhere, so textbox/caption zones' `fontFamily` was
never available for `pbir.py` to map — `fontSize`/`bold`/`italic`/`color` all threaded through
correctly, `fontFamily` was emitted exactly 0 times across a full rebuild. **Fix:**
`parse_formatted_text()` now also captures `r.get("fontname")` per run. **Status:** ✅ Fixed.
Covers only `textbox` visuals' rich text — `tableEx`/chart/slicer fonts are still genuinely
unextracted (not a bug, a scope gap — see `SKILL.md`'s "Fonts" scope note).

### No per-worksheet `label_font` capture for a `card` visual's base font
A worksheet's own `fontSize`/`fontFamily` (from Tableau's `datalabel`/`worksheet`-scope style
rules — **not** `label` scope, that was a wrong first guess, corrected same session) was never
read at all, so a `card` visual's value text always fell back to Desktop's generic default
regardless of what Tableau actually specified. **Fix:** `_label_font(ws)` reads `datalabel` scope
primarily, `worksheet` scope as fallback for whichever attribute `datalabel` doesn't specify,
excluding `field=`-scoped conditional-format overrides. **Status:** ✅ Fixed (paired with a
`pbir.py` change — see that file).

### Parameters indexed by caption; formulas reference the parameter's internal name
A parameter reference inside a calc formula uses Tableau's internal name, but the extraction's
own parameter lookup was keyed by caption — the same caption-vs-internal-name split this pipeline
already documents for calculated fields. Real corpus: 18 of one workbook's 79 calcs declined for
parameters that were in the model all along. **Fix:** index parameters by internal name (with
caption as a secondary lookup), matching how calc resolution already works. **Status:** ✅ Fixed.

### Group/bin internal Tableau name was never captured, only its caption
A group auto-named `<source field caption> (group)` at creation keeps that as its STABLE internal
identity even after the user renames its caption — exactly the caption-vs-internal-name split
calcs already have. A group-of-a-group whose `source` references the OLD internal name could
never match a group indexed only by the NEW caption. **Fix:** capture `tableau_name` for groups
and bins too, the same field calcs already carry. **Status:** ✅ Fixed; recovered 3 previously
declined calcs across 2 workbooks with no other code change.
