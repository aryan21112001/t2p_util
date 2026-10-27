---
name: generate-copilot-fix-prompts
description: >
  For a real build-tableau-pbip output, turns every declined item in that workbook's own
  `_work/residue.json` into a ready-to-paste Copilot prompt in COPILOT_FIX_PROMPTS.md -- a
  reviewer hands these to Power BI Desktop's Copilot / DAX-writing assistant and writes the DAX
  themselves, in Desktop, with real schema access. Nothing here writes DAX or edits the
  `.SemanticModel` automatically: writing DAX for a table calc, LOD expression, or join from the
  formula text alone would be guessing, and a human (or Copilot, with the real model open) always
  stays in the loop before anything lands in the model. The fix/prompt text is looked up from
  `FIX_RULES`, the same pattern table validate-tableau-pbip-mapping already maintains (imported
  directly, not duplicated) -- a gap whose reason matches a rule gets a prompt with a specific
  starting DAX shape; a gap that matches no rule gets the raw problem and no starting point.
  Use AFTER build-tableau-pbip (or after generate-dummy-data, whose output folder also carries a
  copy of `_work/`) -- every script here only reads `_work/residue.json` (+ `_work/ir.json` and
  `_work/name_authority.json` where noted), nothing else, and never writes into the real project.
  A second script, `suggest_visual_candidates.py`, does the analogous triage for the
  presentation-layer residue (`visual_undecided` + `zone_gaps`), since Copilot has no
  visual-authoring capability. A third, `effort_report.py`, rolls the same FIX_RULES-recommended
  vs. no-recommendation split up into a management-facing number: how much manual work is left,
  split by who can do it, with an estimated-hours total and a QA checklist -- one
  EFFORT_REPORT.md per workbook, one EFFORT_SUMMARY.md across a batch.
argument-hint: <build-tableau-pbip or generate-dummy-data output: project-or-parent-folder> [--out folder]
---

# Generate Copilot Fix Prompts

Turns `RESIDUE.md`'s prose (and its structured twin, `_work/residue.json`) into something a
reviewer can act on directly. Three scripts:

- `generate_copilot_prompts.py` -- one Copilot prompt per *kind* of gap, not per instance, for
  every declined calc, plus a manual-action note for structural gaps (relationships, parameters).
- `suggest_visual_candidates.py` -- triages the PRESENTATION-layer residue (`visual_undecided` +
  `zone_gaps`) into self-resolving / genuinely-needs-a-human / one well-evidenced suggested-visual
  bucket. Copilot has no visual-layout capability, so this is a triage tool, not a Copilot-prompt
  generator -- see "Presentation-layer triage" below.
- `effort_report.py` -- turns everything still outstanding into ONE management-facing number: how
  much manual work is left, split into prompt-with-a-recommendation / prompt-with-no-
  recommendation / needs-a-human-in-Tableau, with an estimated-hours total and a QA checklist. At
  corpus scale it also produces a batch `EFFORT_SUMMARY.md` across every workbook validated in
  the same run. See "Effort/QA reporting" below.

A fourth script, `report_dangling_references.py`, is a thin CLI wrapper over
`generate_copilot_prompts.py`'s own `find_residue_workbooks`/`drop_already_emitted` helpers for a
narrower dangling-reference sweep -- see that script's own docstring.

> **Corpus-agnostic.** No built-in knowledge of any particular workbook, dashboard, or field.
> Every path is an argument.

## Nothing here writes DAX or edits the model

Every fix path in this skill ends the same way: a Copilot prompt (or, for structural gaps, a
manual-action note) that a human applies themselves in Power BI Desktop, with real schema
autocomplete and the ability to ask Copilot clarifying questions when the prompt is uncertain.
There is no staged-candidate/promotion step and no monkeypatched parser dispatch -- writing DAX
straight from `residue.json`'s formula text, without a human ever opening Desktop to check it
against the real model, is exactly the class of silent-wrong-measure risk this whole pipeline's
"fail closed, never guess" principle exists to avoid. `drop_already_emitted` (used by both
`generate_copilot_prompts.py` and `effort_report.py`) still checks `name_authority.json`'s
`emitted` set before listing a gap, so once a human applies a fix by hand in Desktop and the
project's `_work/` is refreshed, that item drops off every to-do list this skill produces --
however the fix got there.

## Why this reuses `FIX_RULES` instead of a new pattern table

`validate-tableau-pbip-mapping`'s `FIX_RULES` (in `validate_mapping.py`) is already an ordered
`(regex on the reason text) -> fix` table, grounded in the real decline-reason strings
`dax.py`/`tmdl.py` raise. This skill imports that module directly (`sys.path` insertion, not a
subprocess call — it only needs the pure lookup table and `suggest_fix()`, not the CLI) so both
skills' idea of "what the fix is" never diverges. Extend `FIX_RULES` in that skill's script when
a new reason pattern recurs; this skill picks up the change automatically.

## Run

```bash
python3 scripts/generate_copilot_prompts.py <build-tableau-pbip or generate-dummy-data output> [--out <folder>]
```

- **Input**: either one workbook's build/output folder (has a `_work/residue.json` directly
  inside it) or a parent directory holding several such folders — the script walks the input
  recursively and processes every `_work/residue.json` it finds.
- **`--out`** (optional): write `COPILOT_FIX_PROMPTS.md` under `<out>/<Workbook>/` for every
  workbook instead of directly into each workbook's own folder. Omit to write in place, matching
  where `RESIDUE.md`/`COVERAGE.md` already live for that project.

## Grouping

Every `calc_residue` entry's `reason` is checked against `FIX_RULES` in order; the first pattern
that matches becomes the group key. Entries whose reason matches no rule share `GENERIC_FIX` but
are still split into sub-groups by a normalized reason (quoted names, bracketed field references,
and bare integers replaced with placeholders) so two genuinely different uncatalogued gaps don't
collapse into one prompt just because neither has a dedicated rule yet.

## Output

```
<project folder>/
  COPILOT_FIX_PROMPTS.md
```

Per workbook:

- **DAX Measures to Fix** — one numbered group per gap pattern: affected field names, each
  field's own Tableau table/tier/formula, the shared decline reason, the shared suggested fix,
  and one ready-to-paste Copilot prompt covering the whole group. Every field's `locate_field()`
  line is prefixed **`[MANDATORY]`** or **`[OPTIONAL -- No Current Report Impact]`** — the exact
  `field_has_report_impact()` verdict the "No Current Report Impact" section (below) uses for the
  same field — so a reader skimming section A/B directly sees the same call, instead of having to
  notice the softer "Semantic model only" wording on its own or cross-check a separate list.
- **Other Metadata Gaps** — `visual_undecided` (mark/shelf combinations no rule recognized, so a
  safe `tableEx` placeholder was emitted) and `zone_gaps` (dashboard zones whose shelf fields all
  failed to resolve, so the visual was dropped rather than emitted blank) — each with the same
  background treatment, but a manual Desktop-action note instead of a Copilot prompt, and an
  explicit "self-resolves once the underlying field is fixed" note for zone gaps that trace back
  to a calc-residue group above. Both are always mandatory: a dashboard zone/visual either is
  live on a report page or is missing from one, so "no current report impact" never applies here.
- **No Current Report Impact — Fix Only If Needed** — the cross-cutting list every `[OPTIONAL]`
  item above is drawn from, gathered in one place: fields with no current report impact are
  either an internal ingredient another calc references, or truly orphaned. Safe to skip on a
  first pass.

## Known limits

- Only reads `_work/residue.json`. It does not re-run `validate-tableau-pbip-mapping`, so it
  will not catch the two static DAX checks that skill runs (the `Param <X>[Value]` misuse and
  dangling-reference checks) — those live in that skill's own `MAPPING_REPORT.xlsx` / `DAX
  Warnings` sheet, not here.
- A group's Copilot prompt lists every affected field's Tableau formula verbatim but does not
  verify those formulas still match the live Tableau source — `residue.json` is a snapshot from
  the build that produced it.
- Grouping by normalized reason text is a heuristic for the GENERIC_FIX bucket only; a reason
  that happens to normalize identically to an unrelated one would merge into the same prompt.
  Add a `FIX_RULES` entry for a recurring pattern to get it its own precise group instead of
  relying on normalization.
- `drop_already_emitted` matches a residue item to its allocated name by the RESOLVED PBI table
  name (`name_authority.json`'s `tables` map), not the raw Tableau caption/connection string --
  those two differ whenever `sanitize()` strips a trailing parenthetical or a name collision
  renames the table. Using the raw caption previously meant this check matched nothing for any
  workbook where that gap applied, silently reporting every already-fixed item as still
  outstanding; fixed at the source.

## Presentation-layer triage

```bash
python3 scripts/suggest_visual_candidates.py <build-tableau-pbip or generate-dummy-data output> [--out <folder>]
```

Same input convention as `generate_copilot_prompts.py`. Writes `PRESENTATION_LAYER_REVIEW.md` per
workbook, split into four sections:

1. **Suggested visual type** -- items where Rows AND Columns are both empty. `pbir.py`'s own
   `_shelf_shape` docstring already establishes this exact shape is Tableau's text-table/BAN-card
   idiom regardless of the recorded mark (with nothing on Rows or Columns, there is no axis for
   any mark to plot against, so Tableau's own Automatic-mark algorithm can only fall back to a
   text display) -- this reuses the IDENTICAL two rules `pbir.py`'s `VISUAL_RULES` already applies
   for a literal `mark == "Text"`, just triggered on the shelf shape instead of the mark label.
   Measured against the corpus: 68 of 122 `visual_undecided` items (56%) are this shape. Still
   needs an SME to build the visual in Desktop and confirm -- nothing is written to the report.
2. **Needs a Tableau lookup** -- everything else. Genuinely depends on what Tableau's Automatic
   mark rendered from the live data (e.g. a single dimension alone with no measure), which isn't
   recoverable from shelf counts. No suggestion offered; same manual-Desktop path as
   `generate_copilot_prompts.py`'s existing note, just no longer mixed into one big list.
3. **Zone gaps, self-resolving cascades** -- the visual's own mark/shape already resolved; it's
   missing only because a shelf field it depends on is a declined calc. No new action: once that
   calc's DAX is written by hand (per its own entry in `COPILOT_FIX_PROMPTS.md`) and the project
   rebuilds, these recover automatically.
4. **Zone gaps, structural** -- the Tableau zone TYPE itself (`dashboard-object`, `empty`, ...)
   has no Power BI construct at all. Not fixable by any rule, LLM, or Copilot, because there is
   nothing to translate TO -- an SME manually recreates the equivalent in Desktop or accepts it's
   dropped.

### Why this is a triage tool, not a Copilot-prompt generator

Power BI Copilot is a DAX/M-writing assistant with no visual-layout capability -- there is no
tool to hand a "build this chart" prompt to. What DOES transfer from `generate_copilot_prompts.py`
is triage discipline: separate what's genuinely undecidable without a human from what already has
a real, evidenced answer, and be honest that bucket 2 and bucket 4 have neither -- rather than
proposing a plausible-looking visual type for an `Automatic` mark this script cannot actually
verify against the real Tableau render.

### Known limits

- Section 1's suggestion is a `visualType` string + rationale for a human to apply, not a PBIR
  JSON visual container this script emits -- there's no equivalent "run this and see the number"
  verification step for a visual; the SME's checklist is "build it in Desktop, does it look like
  the Tableau worksheet."
- Cascade detection (section 3) matches on the exact reason-string shapes `pbir.py` emits today
  (`_CASCADE_RE`) -- a new cascade-reason wording added to `pbir.py` later would need a matching
  update here, the same maintenance contract `FIX_RULES` already has with `dax.py`'s reasons.

## Effort/QA reporting

```bash
python3 scripts/effort_report.py <build-tableau-pbip or generate-dummy-data output> [--out <folder>]
```

Same input convention as the other scripts (one workbook, or a parent directory of several).
Built for one specific question the prompt document doesn't answer directly: **at corpus scale,
how much manual work is left, and who can do it** -- the number a management report actually
needs, not a per-workbook markdown file nobody has time to read individually.

Every item still outstanding lands in exactly one bucket. Every bucket still needs a human (or
Copilot in Desktop) to write the DAX/model change by hand -- the only distinction this report
draws is how much of a running start `COPILOT_FIX_PROMPTS.md` gives that human, exactly the same
Section A ("Recommended Fixes") vs Section B ("No Recommended Fix") split that document itself
uses, reusing `validate_mapping.suggest_fix` directly so the two can never disagree:

1. **Recommended prompt** -- a `FIX_RULES` pattern matched this reason, so the Copilot prompt
   includes a specific starting DAX shape (e.g. "Rewrite as `DATEVALUE(...)`"). Lower effort than
   diagnosing from nothing, but still real work -- the pattern is a starting point, not runnable
   DAX; nothing has been written into the model.
2. **Raw prompt** -- no `FIX_RULES` pattern matches this reason (`GENERIC_FIX`). The Copilot
   prompt hands over the raw problem with no starting shape; a human/SME diagnoses it from
   scratch.
3. **Manual investigation** -- a structural/visual/zone gap that inherently needs a person to
   look at Tableau (table calc addressing, LOD, free-entry parameters, marks, dropped zones,
   relationships) -- there is no DAX to write at all.

Per-item minutes (`MINUTES_PER_ITEM`) are a documented, adjustable ASSUMPTION, not a measurement
-- every hours figure in the output says so explicitly. Replace the constants once real timing
data exists from actually doing the work; re-running then refreshes every number downstream.

### Mandatory vs. Optional effort

Every bucket's count and hours are split into **Mandatory** and **Optional**, using the exact
same `field_has_report_impact()` check `COPILOT_FIX_PROMPTS.md`'s own "No Current Report Impact"
section is built from (imported directly, not a second definition), so the two documents can
never disagree about which fields are load-bearing today:

- **Mandatory** -- the field is placed on a real worksheet visual or filter somewhere in this
  workbook. Something currently visible in the report is wrong or missing until this is fixed.
- **Optional** -- no current report impact: an internal ingredient another calc references, or
  truly orphaned. A real gap, not a false positive -- just safe to defer. Only matters if someone
  later wants to use that field directly on a report page.
- **Never split** -- `visual_undecided`/`zone_gap` are always 100% mandatory: both are already a
  real dashboard zone either rendering something or missing something on a live page today, never
  an internal-model-only ingredient.
- **Missing data defaults to mandatory, never optional** -- if `_work/ir.json` is missing or
  unreadable (no evidence either way), every item is counted mandatory. A field is only ever
  demoted to optional on POSITIVE evidence it has no report placement, matching this pipeline's
  fail-closed rule elsewhere.

Plan a delivery date against the mandatory total; optional hours are a separate, smaller number
for a team that wants to clear everything, not something blocking anything visible today.

### Output

- `EFFORT_REPORT.md` + `effort_report.json` per workbook -- the bucket table with **Mandatory
  hrs** / **Optional hrs** columns, a "manual items by recurring reason" breakdown (fixing the
  pattern once, e.g. in `dax.py` or a new `FIX_RULES` entry, clears every instance of it — this
  is the DATEPARSE-fix precedent generalized into a report), and a QA checklist: static DAX
  warnings that will error in Desktop (`validate_mapping.py`'s own check, reused directly), an
  already-resolved count (already live in the TMDL, however it got there -- still worth a
  spot-check, never machine-verified), and an unaccounted-field count that should always be 0
  (nonzero means a build-consistency bug, not a content gap).
- `EFFORT_SUMMARY.md` + `effort_summary.json`, written automatically when more than one workbook
  is found under the input -- aggregate counts/Mandatory+Optional hours across the whole batch, a
  person-days estimate for each, the top 15 recurring decline reasons **across every workbook**
  (the actual prioritization list for "fix this once, it helps everyone"), and a per-workbook
  table with its own Mandatory/Optional/Total hours columns.

### Known limits

- If pointed at a directory holding BOTH a workbook's raw (pre-data) output and its
  `generate-dummy-data` copy, `find_residue_workbooks()` (shared with
  `generate_copilot_prompts.py`) finds both `_work/residue.json` files and counts the same
  workbook twice in a batch rollup. Point this at one canonical output tree per workbook, not a
  mixed raw+copy directory.
