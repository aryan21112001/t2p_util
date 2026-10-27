# Master Prompts — Tableau → Power BI Pipeline

Checkpoint-gated master prompts for running the pipeline (`tools/agents/README_AGENTS.md`) end
to end: one phase per section, each divided into sub-steps with a verification gate before
advancing. Every warning here traces back to a documented bug or a confirmed workaround in
`tools/agents/implementation/agent-extend-aryan/` (the gotcha files) or a live-verified session —
nothing here is speculative.

**Working directory:** every command below is written relative to the **repo root** (`C:\t2p`)
and is self-contained — none of them depends on a `cd` from a previous command, so blocks are
safe to copy/paste in any order or into a fresh terminal. The one exception (`pbip_rules/lint.py`,
which needs `tools/` on its import path) is wrapped in its own subshell for exactly this reason.

**Environment note:** if `python3` on your shell redirects to the Microsoft Store instead of
running Python (seen on this Windows box), use `python` instead — same scripts, same flags.

**Folder convention used throughout:** Agent 1 produces `<out>/<Name>/<Name>.pbip`. Phase 2
below wires data **in place** into that same folder for both synthetic and realistic modes — so
`<out>/<Name>` is the one project folder every later phase (3 and 4) operates on. (The
orchestrator's own Agent-3 wrapper creates a separate `<Name>_realistic/` folder instead; this
runbook deliberately avoids that wrapper — see Phase 2 for why — so that branch never applies
here.)

---

## Pre-flight — read before touching anything

```
Open RESIDUE.md and COVERAGE.md's "What this does not establish" section from any prior
build of this workbook, if one exists. Also check tools/agents/implementation/agent-extend-aryan/
for any OPEN (not-yet-fixed) item specific to this workbook's known issues before treating a
fresh build as a clean slate.
```

If this is the WorkbookA workbook specifically: four items are **known-open, not fixed** as of the
last session — blend-key relationship zero-overlap (`Activation<->Recommendations`),
`Dormancy`'s 2 bare-column measures (manual fix required), 6 `INDEX()` table-calc fields (manual
authoring required), and nav-tab fields misclassified as data tables on `Acquisition`. **Decide
now whether any of these block tonight's ship** — don't discover them during Phase 4.

---

## Phase 1 — Conversion (Agent 1)

**1a. Confirm the source has real data to work with:**
```
Check <file.twbx> for an embedded .hyper extract (not just a sqlproxy published-datasource
reference) — this decides whether Phase 2 can sample real values at all.
```

**1b. Convert:**
```bash
python3 tools/agents/implementation/agent-1-converter/converter_agent.py \
  "<file.twbx>" -o "<out>" --verbose
```

**1c. Verification gate — do not advance to Phase 2 until all four pass:**
```bash
(cd tools && python3 pbip_rules/lint.py "<out>/<Name>" --schemas)
```
- `RESIDUE.md` count is lower than or equal to any prior build of this same workbook — a higher
  count means something regressed, not that this is a harder workbook.
- `COVERAGE.md`'s own "what this does not establish" section is read, not skipped — it names
  exactly what no agent downstream will catch (visual formatting, table-calc addressing, LOD).
- `START_HERE.md` exists and names the exact folder to open next (`<out>/<Name>`).
- The lint command above reports clean (only pre-existing stray-file notes are acceptable).

---

## Phase 2 — Data Creation

Deliberately calls the agents **directly on the Phase-1 output**, not the orchestrator — the
orchestrator's own entry point always takes the raw `.twbx` and re-runs Agent 1 from scratch as
part of its flow, which would silently discard the Phase 1c verification gate above and could
produce a build that was never checked. Running Agent 2/3 directly avoids that.

**2a. Pick mode** (don't default to synthetic for a workbook that has real data — production
needs real value distributions, not uniform fakes):
```
If <file.twbx> has an embedded .hyper extract, use realistic mode (2B) with
--sample-pct 100 --max-real-rows 0 (explicit, never left to default). Otherwise use
synthetic mode (2A) with --rows sized to the longest trend window any measure needs
(365 for a year, 3650 for a decade).
```

**2A. Synthetic (always works, wires in place into the Phase-1 folder):**
```bash
python3 tools/agents/implementation/agent-2-dummy-data/dummy_data_agent.py \
  "<out>/<Name>" -o "<out>" --rows 365 --verbose
```

**2B. Realistic (needs the embedded `.hyper` extract confirmed in 1a):**
```bash
python3 .claude/skills/generate-dummy-data/scripts/generate_dummy_data.py \
  "<out>/<Name>" --in-place --realistic "<folder containing file.twbx>" \
  --sample-pct 100 --max-real-rows 0 --rows 365
```
Called directly against the skill script rather than the `agent-3-realistic-data` wrapper on
purpose: the wrapper's own CLI has **no `--sample-pct`/`--max-real-rows` flags at all** and
silently defaults to 20 rows even in "realistic" mode — it cannot deliver a full, uncapped sample.
`--in-place` wires directly into the SAME `<out>/<Name>` folder Agent 1 produced (no separate
`_realistic` copy), so Phase 3/4 below always point at one consistent path regardless of which
mode you picked. Always pass `--sample-pct 100 --max-real-rows 0` explicitly, even though these
are already the skill's own defaults — a stale copy of an older command elsewhere could still
carry a 10%/10,000-row cap from before that default was fixed.

**`--desktop-path`** (either mode): required if Power BI Desktop will open this project on a
*different* machine than the one running this build — otherwise the Excel path baked into the M
expression is this machine's own path, silently wrong elsewhere, with every column showing
missing and no error. Omit only when build and Desktop are the same machine.

**2c. Verification gate — this is where most real bugs surface, do not skip:**
1. Copy `<out>/<Name>` to an **isolated scratch folder** (e.g. `<out>_diag/<Name>`) and open a
   **separate** Desktop instance against the copy. Never connect `pbi-cli` to your own
   live/interactive Desktop session.
2. Full-refresh every real table: `pbi-cli table refresh <name> --type Full` per table, one
   direct Bash call each — never batch multiple `pbi-cli` calls through a nested Python wrapper,
   it fails with a DLL load error.
3. If the *first-ever* refresh on a table fails with `"'X' column does not exist in the
   rowset"`: run `--type DataOnly`, then `--type Calculate` (separate calls), then retry
   `--type Full`. This is a known cold-model engine-ordering issue, resolved 100% of the times
   it's been hit — not a schema bug, don't chase it as one.
4. Spot-check 2–3 real card measures per table via `pbi-cli dax execute` against a value you can
   reason about (grand-total `% of Total` should be `1.0`; a filtered card should be non-blank
   and plausible) — "it loaded" is not the bar, "the number is right" is.

---

## Phase 3 — Gotcha Fixes (Mandatory → Optional)

**3a. Generate** (pass the **project folder**, not the `.pbip` file — `copilot_prompts_agent.py`'s
own usage examples always show a folder):
```bash
python3 tools/agents/implementation/agent-5-copilot-prompts/copilot_prompts_agent.py \
  "<out>/<Name>" --verbose
```

**3b. Apply MANDATORY only, one field at a time** — not a blind batch script. Each one is a real
visual on a real page; a wrong batch edit is worse than a slower manual one tonight.

**3c. Live-verify EACH fix individually before moving to the next one:**
```
pbi-cli dax execute, pasting the measure's own raw formula text directly rather than
referencing it by name — referencing a freshly-added measure by name intermittently
fails with "no current row for this column" even when the formula is correct; pasting
the raw formula text avoids this entirely.
```
If a query repeats the same bracketed table name several times in one call, wrap it in
`DEFINE MEASURE 'T'[_t] = <expr>` first — `pbi-cli dax execute` can silently corrupt a query
that references the same table bracket many times.

**3d. Decide Optional now, explicitly** — don't silently ship with them untouched and
undocumented. For a tonight deadline: confirm with whoever owns the dashboard whether any
`[OPTIONAL -- No Current Report Impact]` item is actually about to become mandatory (e.g., a
field someone's about to add to a page tonight).

---

## Phase 4 — Validator (final gate before ship)

**4a. Score** (project folder, same as Phase 3):
```bash
python3 tools/agents/implementation/agent-4-validator/validator_agent.py \
  "<out>/<Name>" --verbose
(cd tools && python3 pbip_rules/lint.py "<out>/<Name>" --schemas)
```

**4b. Regression pass — re-verify, don't re-trust:** in a **fresh** isolated scratch copy (a
brand-new folder path — Desktop has its own path-keyed rendering cache; reopening the SAME path
can show stale blank visuals even when the data is correct), full-refresh all tables again and
re-check the SAME 2–3 measures from Phase 2c plus every measure touched in Phase 3 — confirming
the Mandatory fixes landed AND nothing already-working regressed.

**4c. Consolidate to the one shipping folder:** `<out>/<Name>` is already the single folder every
phase has been writing into — rebuild `START_HERE.md` there explicitly stating it's the
production deliverable, and list every remaining known-and-deliberately-out-of-scope item (from
Pre-flight) in it, so whoever opens this at 2am knows what's genuinely unfinished versus what's
just unfamiliar.

**4d. Go/no-go:** quality score + lint clean + all Mandatory fixes live-verified + Pre-flight's
open items explicitly accepted or resolved. If any of the four known-open WorkbookA items (blend-key
overlap, `Dormancy` bare columns, `INDEX()` fields, nav-tab misclassification) land on a page
someone will actually look at tonight, that's a stop-ship, not a footnote.

---

## Shortcut — all-in-one, if you don't need the phase gates

Only use this for a *fresh* workbook with no Phase-1 build to protect yet — it re-runs Agent 1
itself, so it cannot be used to resume after Phase 1 like Phase 2 above does:
```bash
python3 tools/agents/implementation/orchestrator/realistic_data_pipeline_orchestrator.py \
  "<file.twbx>" -o "<out>" --sample-pct 100 --max-real-rows 0 --agent5 --agent4 --verbose
```
Human-applied Mandatory fixes (Phase 3b/3c above) still have to happen by hand in Desktop
regardless of which path produced the build, before a final validator re-run means anything.

---

## Optional — Extension Agents (outside the numbered pipeline)

Run these on an already-built Agent 1 output when you want to auto-promote table calculations
the main pipeline declines. Each writes a **separate new copy** and never touches the original.

```bash
# Percent-of-Total and other quick-calc buttons (verified on real corpus: 8 promotions on WorkbookA)
python3 tools/agents/implementation/agent-extend-tablecalc-quickcalcs/tablecalc_quickcalcs_extension_agent.py \
  "<file.twbx>" "<out>/<Name>" -o "<new_pbip_dir>"

# RANK/RANK_DENSE with Specific Dimension addressing (no confirmed real-corpus promotions yet)
python3 tools/agents/implementation/agent-extend-tablecalc-rank/tablecalc_rank_extension_agent.py \
  "<file.twbx>" "<out>/<Name>" -o "<new_pbip_dir>"

# LOOKUP/INDEX/WINDOW_SUM — verify model grain before shipping any promotion
# (WorkbookA's Usage Overview index was promoted but computed wrong at real data grain — see
# agent-extend-aryan/README.md for the verification checklist before trusting output)
python3 tools/agents/implementation/agent-extend-dax-measures/dax_measures_extension_agent.py \
  "<file.twbx>" "<out>/<Name>" -o "<new_pbip_dir>"
```

Each writes `*_EXTENSION_REPORT.md` listing every promotion (with its DAX) and every declined
candidate with a specific reason. Read that report and verify against the model's real data
grain before merging any promotion into the canonical build.
