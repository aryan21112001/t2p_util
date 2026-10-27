# Orchestrator & data-sampling gotchas

Covers `tools/agents/implementation/orchestrator/*.py` and
`.claude/skills/generate-dummy-data/scripts/generate_dummy_data.py`. These are pipeline-wiring
bugs, not Tableau-translation bugs — they affect every workbook run through
`realistic_data_pipeline_orchestrator.py`, regardless of what's inside the `.twbx`.

---

### 1. Two output folders appear per run, unclear which is the real deliverable

**Symptom:** a run produces both `<out>/<Name>/` (Agent 1's data-free stub) and
`<out>/<Name>_realistic/` (the real deliverable with sampled data) — nothing in either folder
says which one to open.

**Fix:** `pipeline_steps.py`'s `write_output_pointer()` now writes a `START_HERE.md` into every
run's output root, naming the exact `.pbip` to open and explaining what each sibling folder is.
Wired into `basic_pipeline_orchestrator.py`, `dummy_data_pipeline_orchestrator.py`, and
`realistic_data_pipeline_orchestrator.py`, each with its own tailored explanation of its sibling
folders.

**Status:** ✅ Fixed — every future run gets a `START_HERE.md` automatically.

---

### 2. Opening a regenerated PBIP in Desktop shows blank visuals / "incomplete data" banners

**Symptom:** looks like the whole conversion is broken — every visual renders blank the first
time a freshly (re)generated `.pbip` is opened in Desktop.

**Root cause:** not a bug — PBIP is a file-based, source-control-friendly format. Desktop does
NOT auto-refresh data when files change on disk since its last session; it shows "One or more
calculated objects need to be manually refreshed" / "Some of the tables have incomplete or no
data" instead, until **Refresh** is clicked (Home ribbon, or `pbi-cli table refresh <name> --type
Full` per table).

**Fix:** `START_HERE.md` (see #1 above) explicitly tells the user to Refresh after opening.

**Status:** ✅ Documented — not a code fix, a documentation one, but it's in the auto-generated
output now, not something a human has to remember to say every time.

---

### 3. Agent 3's "did the realistic-data generation succeed" check was fragile

**Symptom:** re-running the pipeline into an already-used output directory could make Agent 3
falsely report failure (or succeed against the wrong folder), because its own success heuristic
was "exactly one subdirectory exists" — which breaks the moment that directory already has
history in it.

**Fix:** `realistic_data_agent.py` now searches directly for the real `.pbip` file
(`output_dir.glob("*/*.pbip")`, falling back to `output_dir.glob("**/*.pbip")`), sorted by
modification time, instead of counting/naming subdirectories.

**Status:** ✅ Fixed — safe to re-run into a reused output directory any number of times.

---

### 4. `--sample-pct 100` still silently truncated large tables to 10,000 rows

**Symptom:** asked for 100% sampling, got exactly 10,000 rows on the largest tables anyway, with
no error or warning.

**Root cause:** `generate_dummy_data.py` had a hardcoded `max_rows` default with no way to fully
disable it — any positive number, however large, was still a cap.

**Fix:** `--max-real-rows 0` (or any value `<= 0`) now means **no cap at all**. Previously there
was no way to express "truly unlimited."

**Status:** ✅ Fixed — **always pass `--max-real-rows 0`** when the goal is genuinely full/real
data, not a representative sample. This is a flag the caller must still remember to pass; the
pipeline doesn't assume it by default (so an intentionally-capped sample run still works the old
way if someone wants one).

---

### A hyper-sourced column author-overridden to `datatype='string'` on a DOUBLE got the wrong string form
**Symptom:** a worksheet filter on a bare member value (e.g. `per_rnk_flg = "1"`) matches ZERO
rows even though rows with the real underlying value genuinely exist — every card/table gated by
that filter goes blank, with no error anywhere.

**Root cause:** the real `.hyper` extract stores the column as a native `DOUBLE` (confirmed via
`tableauhyperapi` directly: `1.0`, `3.87096774`, ...), but Tableau's own `.twb` XML declares it
`datatype='string' datatype-customized='true'` — the WORKBOOK AUTHOR deliberately overrode
Tableau's type inference. When Tableau casts a double to a string under this override, it trims
a trailing `.0` for a whole number (`1.0` → `"1"`); a worksheet's saved filter member (`member=
"1"`) is captured in that trimmed form. `generate_dummy_data.py`'s `coerce_hyper_value()`,
sampling the same raw double for its `mtype == "text"` branch, did a naive `str(value)` instead
— `"1.0"`, never equal to the filter's `"1"`. Real corpus (WorkbookA): Beta's
`per_rnk_flg`, 0 of 9 real rows with the underlying value `1.0` ever matched `per_rnk_flg="1"`
until fixed.

**Fix:** `coerce_hyper_value()` now special-cases a whole-number float for `mtype == "text"`:
`str(int(value))` instead of `str(value)`. A genuine fractional value (`3.87096774`) is
untouched — Tableau's own cast only ever drops a trailing `.0`, never rounds.

**Status:** ✅ Fixed, in `generate_dummy_data.py`. **General signal for a NEW workbook:** grep the
`.twb` for `datatype-customized='true'` columns whose `datatype='string'` — any one of them is a
candidate for this exact mismatch if its real extract column is numeric. No `--selftest` harness
exists for this script yet; verify via a scratch rebuild + direct Excel-cell inspection
(`openpyxl`, `data_only=True`) comparing the sampled string form against the worksheet's own
saved filter `member=` value, not just eyeballing the output.

---

### First `Full` refresh of a freshly-opened/never-processed model can transiently fail
**Symptom:** `pbi-cli table refresh <name> --type Full` (or Desktop's own Refresh button, same
first time) fails with `'<SomeColumn>' column does not exist in the rowset`, naming a column
that DOES exist and IS independently queryable via plain DAX (`EVALUATE
SUMMARIZECOLUMNS('T'[SomeColumn])` works fine). A different column name fails on different
tables, no fixed pattern. Reproducible on a cold Desktop restart against the SAME file, so it is
NOT the generic "genuinely intermittent" `Seg Status` AS-engine quirk documented elsewhere.

**Root cause:** an Analysis Services ordering issue specific to the FIRST `Full` process on a
model that has never been processed at all — `DataOnly` and `Calculate` each succeed
independently when run as SEPARATE operations; it's specifically the combined/cascaded `Full`
pass (which also recalculates every dependent table across any relationship) that trips on this
on a cold model. More relationships (more cross-table dependency edges) makes it more likely to
surface, not less.

**Fix / workaround (no code fix exists, this is an AS engine behavior):** on a `Full` refresh
failure with this exact error shape on a freshly-opened file, run `table refresh <name> --type
DataOnly` then `--type Calculate` as two SEPARATE calls, THEN retry `--type Full` — this
sequence has resolved it 100% of the times it's been hit so far. If `DataOnly` alone also fails,
just retry `DataOnly` once more before moving to `Calculate`.

**Status:** ✅ Workaround confirmed reliable, not root-caused further (it's inside Analysis
Services itself, outside this pipeline's own code). Don't mistake this for a real schema/data
bug — the tell is that the SAME column name is independently queryable via plain DAX while the
`Full` refresh claims it doesn't exist.

---

### 5. OPEN, UNRESOLVED (2026-10-06): fresh rebuilds render several barChart visuals with zero bars, no error

**Symptom:** on WorkbookA's Alpha page, 4 chart visuals ("Sheet 4 - Quarterly Trend - Items Active",
"Sheet 5 - Group YoY growth", "Sheet 5 - Alt YoY growth", "Gap to alt") show their Category axis labels
(a plain list of `Item Id` values) but draw NO bars/lines/value marks at all — not an error, not a
"(Blank)" card, just an empty plot area. Confirmed present in **every** fresh rebuild attempted
this session (`vf9`, `vf10`, `vf11`, `vf12`, `vf_control_a`) and **absent** in `out2` (the
pre-existing deliverable, where the same charts render real bars, just with the OLD "same value
for every category" data bug `02-extraction-gotchas.md`/`05-semantic-model-gotchas.md` above
fixed). A plain Refresh (Home ribbon) does not fix it.

**Ruled out, with direct evidence — do not re-walk these:**
- **Not a DAX/data bug.** `EVALUATE SUMMARIZECOLUMNS(Alpha[Item Id], "YoY", [Group - YoY Growth
  ])` and the full `CALCULATETABLE(...)` reproduction of the visual's own filters both return
  real, correctly-varying values via `pbi-cli` against the exact same live model the blank chart
  sits in.
- **Not a query execution error.** Power BI Desktop's own Performance Analyzer (Optimize ribbon →
  Performance Analyzer → Start recording → Refresh visuals) shows a normal duration breakdown for
  the blank chart, no error of any kind.
- **Not the field-well caption-collision fix** (`02-extraction-gotchas.md`'s `item_id` caption fix
  / `06-report-pbir-gotchas.md`'s `_query_state` `candidate_tables` fix above). Confirmed via a
  controlled A/B test on two cold copies of the SAME rebuild (`vf12` vs `vf_control_a`): reverting
  ONLY the Category field's table back to the pre-fix binding (`Highlights Page` instead of
  `Alpha`), with every other fix left in place, did NOT bring the bars back. The
  caption-collision fix is independently confirmed correct (both fields now resolve to the same,
  right table) — it just isn't the cause of THIS symptom.
- **Not a `pbi relationship list` gap** for the specific table these 4 charts query — Category and
  Y both resolve to `Alpha` directly, no cross-table relationship is even needed for the
  core query.

**Not yet checked (the real next steps, in order of promise):**
- Whether the 4 top-of-page slicers (`Gamma[Ctr]`/`[Grp]`/`[Item Plat]`/`[Item Typ
  ]` — confirmed via their own `visual.json`, all bound to `Gamma`, NOT `Alpha`)
  carry a saved default selection that propagates a zero-row filter into `Alpha` through the
  `Gamma → Beta → Alpha` relationship chain — a chain that did not exist at
  all before this session's fixes (`out2` has zero working relationships touching `Alpha`).
  A manual DAX repro that ALSO applies these 4 slicers' actual bookmark/default state (not just the
  visual's own local `filterConfig`) would confirm or rule this out directly.
- Whether the NEW `Beta.'Blend Key (Ctr, Item Id, Item Plat, Item Typ, blank)'` relationship
  (which also did not exist in `out2`) has a validation issue — the vf10 build log's own validator
  flagged "⚠ 1 of 2 Blend Key relationship(s) have zero overlapping real values" for a DIFFERENT
  pair at the time, worth re-checking whether this one has the same problem under fresh dummy data.
- Whether `realistic_data_pipeline_orchestrator.py`'s dummy-data generation is non-deterministic
  run-to-run (new random data each time `--all-agents` runs) in a way that could make a Blend Key
  relationship's real-value overlap flaky across rebuilds, rather than a fixed property of the
  schema.

**RESOLVED (2026-10-08): root cause is Desktop's own path-keyed cache, not a pipeline bug at all.**
Recurred on a LATER WorkbookA session in the exact same shape — Alpha rendering blank, then
Beta's "Sheet 11 - Preferred G" section rendering blank, both AFTER a rebuild that was already
confirmed correct at the model layer (`pbi-cli dax execute` reproducing the exact visual's own
filter context returned real, correct rows both times — same signature as the "ruled out" list
above). The decisive test, done both times: copy the SAME content to a BRAND-NEW folder path
Desktop has never opened before (`sed`-fix the TMDL partitions' `File.Contents(...)` path to
match, then open that `.pbip` instead) — **it rendered correctly immediately, no other change.**
`out2`'s own path had been opened/closed/overwritten many times across the session (every
promote-a-fix cycle reopens the identical path with different content underneath); a fresh,
never-before-seen path does not carry whatever stale rendering state Desktop pins to a path.

**Detection — how to tell THIS from a real bug, in under 2 minutes, before re-opening any
investigation:**
1. Reproduce the exact blank visual's own query via `pbi-cli dax execute`, using its real
   `filterConfig` conditions verbatim (`CALCULATETABLE`/`SUMMARIZECOLUMNS` wrapping the visual's
   own measure/columns). Real, non-blank rows back ⇒ model/data is fine, skip straight to step 3.
2. Run `pbip_lint.py --schemas` against the project. Clean (only the usual 3 stray-file notes)
   ⇒ the PBIR/TMDL structure is fine too.
3. If both pass: copy the project to a brand-new folder name never opened in Desktop before,
   fix the TMDL partitions' hardcoded `File.Contents(...)` path to match (grep/`sed` the one
   path string across every table's `.tmdl`), and open THAT copy. If it renders correctly, this
   IS the path-cache issue — stop here, there is no code bug to find.

**How to proceed once confirmed:** no code fix exists or is needed. Practical mitigations:
- Prefer a genuinely new output folder per rebuild during active back-and-forth debugging
  (matches this project's own `builds/NN_<name>` convention — see the README) rather than
  repeatedly overwriting one reused path; only the FINAL promote step should land on the stable
  path, and even then expect to need one fresh-path render check before trusting what's seen in
  the reused path.
- If the user is going back to the SAME stable path (e.g. `out2`) across many sessions, a full
  Desktop EXIT (not just closing the report tab) before reopening is worth trying first — cheaper
  than a fresh-folder copy, not confirmed as reliably as the fresh-folder test but worth the one
  try.
- Do not spend time re-deriving DAX/relationship/filter theories once step 1 and 2 above both
  pass clean — that was exactly the trap this entry sat in for 2 days.

**Status:** ✅ Explained and has a fast detection procedure. Not a code bug, nothing to fix in
`extract.py`/`dax.py`/`tmdl.py`/`pbir.py`. The two fixes originally held back by this entry
(`02-extraction-gotchas.md`'s `item_id` caption fix, `05-semantic-model-gotchas.md`'s
`_shelf_agg_only` Share fix) were both promoted into `out2` without incident once this was
understood.
