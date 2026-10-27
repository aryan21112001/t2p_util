# The Agents — what each one is, and how they fit together

Five agents form the numbered pipeline that converts a Tableau workbook into a Power BI project
and progressively closes the gaps the conversion could not close on its own. Three more
**experimental, isolated extension agents** exist outside that numbered pipeline (see
[Experimental extension agents](#experimental-extension-agents-outside-the-numbered-pipeline)),
and one retired agent (`_retired_agent-6-apply-fixes`) is kept in the repo, unused, for reference.

Every agent is a **thin wrapper over skills**. It shells out to one or more scripts under
`.claude/skills/`, parses their summary output, and aggregates. **No agent here contains an LLM** —
"agent" in this repo means *wrapper*, not *model*. Every decision they make is mechanical: fixed
commands, fixed order, and termination conditions that are literally a number in the skill's output.

That matters because this pipeline's core guarantee is determinism — content-hashed lineage tags,
byte-identical rebuilds, and declining anything it cannot translate rather than approximating it. An
LLM in the middle of that would break the guarantee. Where genuine judgment *is* required, it
happens outside the agents and is handed back in through a file (see
[Where the LLM actually belongs](#where-the-llm-actually-belongs)).

---

## The pipeline

```
                    ┌──────────────────────────────────────────────┐
  .twbx / .twb  →   │  AGENT 1  Converter                          │  →  PBIP + COVERAGE.md
                    │           extract-twbx-contents              │      + RESIDUE.md
                    │           build-tableau-pbip                 │      + _work/*.json
                    └──────────────────────────────────────────────┘
                                       ↓
                    ┌──────────────────────────────────────────────┐
                    │  AGENT 2 (synthetic, wires IN PLACE)         │  →  SAME folder, now with
                    │    or  AGENT 3 (realistic, own new folder)   │      data wired
                    │           generate-dummy-data                │      + <Workbook>_dummy_data.xlsx
                    │           (Agent 3 also: validate-dummy-data)│
                    └──────────────────────────────────────────────┘
                                       ↓
                    ┌──────────────────────────────────────────────┐
                    │  AGENT 5  Copilot Prompts                    │  →  COPILOT_FIX_PROMPTS_
                    │           generate-copilot-fix-prompts       │      MANDATORY.md + _OPTIONAL.md
                    └──────────────────────────────────────────────┘
                                       ↓
                    ┌──────────────────────────────────────────────┐
                    │  AGENT 4  Validator                          │  →  VALIDATION_SUMMARY.json
                    │           validate-tableau-pbip-mapping      │      + validation/MAPPING_VALIDATION/
                    │           validate-pbip-compliance           │      + a 0-100 score
                    │           validate-dummy-data (if has data)  │      + EFFORT_REPORT.md
                    │           generate-copilot-fix-prompts       │      + EFFORT_SUMMARY.md (batch)
                    │             (effort_report.py)               │
                    └──────────────────────────────────────────────┘
```

```
Agent 1  →  Agent 2/3  →  Agent 5  →  Agent 4
 build      data          prompts     validate
```

There is no automated "apply" stage. Every DAX/model fix a human writes gets applied by hand in
Power BI Desktop, guided by Agent 5's `COPILOT_FIX_PROMPTS_MANDATORY.md`/`_OPTIONAL.md` -- Agent 5
hands over a ready-to-paste Copilot prompt, not runnable DAX, and nothing in this pipeline writes
into the real `.SemanticModel` on its own. Agent 4's `effort_report.py` step still excludes
whatever is already live in the model (however it got there) from its outstanding-work count, so
applying a fix by
hand and re-running the pipeline still reports accurately.

---

## Agent 1 — Converter

**Does:** Tableau `.twbx`/`.twb` → Power BI `.pbip`. The deterministic core of the whole system.

**Skills:** `extract-twbx-contents`, `build-tableau-pbip`, `validate-tableau-pbip-mapping`,
`validate-pbip-compliance`

```bash
python3 agent-1-converter/converter_agent.py <input.twbx> -o <out> [-n NAME] [--tds DIR] [--batch] [-v] [--json]
```

**Produces:** the PBIP project, plus three things every later agent depends on:

| Artifact | What it is |
|---|---|
| `COVERAGE.md` | what fraction of the source made it, **and what the build does not establish** |
| `RESIDUE.md` | every untranslated object and why |
| `_work/{ir,name_authority,residue}.json` | the structured record the other agents read |

**Fails closed.** Anything not cleanly convertible is *declined with a reason*, never approximated —
a silently-wrong measure is worse than a missing one. That is why the later agents exist.

---

## Agent 2 — Dummy Data (synthetic)

**Does:** fills the empty stub partitions with deterministic synthetic data so the project opens
against something.

**Skill:** `generate-dummy-data`

```bash
python3 agent-2-dummy-data/dummy_data_agent.py <pbip> -o <out> [--rows N] [--batch] [-v] [--json]
```

**Wires data IN PLACE** — into the SAME folder Agent 1 already produced, via
`generate-dummy-data`'s own `--in-place` mode. No second, separately-named copy is made. (Before
this, `generate-dummy-data`'s own copy-then-wire convention left every workbook with TWO output
folders — `<name>/` from Agent 1 and `<name>_dummy/` from Agent 2 — which was the actual source
of folder-duplication confusion this in-place mode exists to remove. The orchestrator's own
`-o/--output` CLI flag on this agent is accepted for call-site compatibility but plays no role in
where data ends up; it is always wired into Agent 1's own folder.)

**Does not run `validate-dummy-data` internally anymore.** That check (TMDL parse, lint, ground
truth, Excel-header-vs-M cross-check) substantially overlaps with what Agent 4 already runs on the
same final folder; folding a second, largely-redundant validation pass into "generate data" added
confusion (two different scores for two overlapping checks) without adding real coverage. Run
`validate-dummy-data` directly, as its own separate step, if that specific cross-check is needed.

Values are derived from each column's **own name and a stable content hash**, so two different flag
columns get different densities and two decimal columns get different series. (Before that, every
flag alternated `1,0` — so every `SUM(flag)` was exactly 10 and every rate exactly `1.00`, which
made a whole dashboard read as broken when it was merely uniform.)

Writes one `<Workbook>_dummy_data.xlsx` per project, and rewires only the empty-stub partitions
to read from it — a Param table or a native-connector partition is never overwritten.

> **`--desktop-path` matters.** The Excel path is baked into the M expression. If Power BI Desktop
> runs on a different machine than the build (e.g. building on macOS, opening on Windows), pass the
> path *Desktop* will resolve — otherwise the table fails to load and **every one of its columns
> reads as missing.**

> **Row count controls date/period coverage, not just volume.** Datetime columns are
> `_EPOCH (2024-01-01) + row-index days`, and any Int64 column named like a period code
> (`\bmonth\b`) cycles `year*100+month` across the same row range — so the default 20 rows covers
> under a month of dates, while `--rows 10000` covers roughly 27 years. Increase `--rows` whenever
> a workbook's measures need to show trend/period-over-period behavior credibly.

---

## Agent 3 — Realistic Data

**Does:** same job as Agent 2, but samples **real values** from the source workbook's embedded
`.hyper` extract.

**Skills:** `generate-dummy-data` (realistic mode), `validate-dummy-data`

```bash
python3 agent-3-realistic-data/realistic_data_agent.py <pbip_input> <twbx_input> -o <out> [--rows N] [--tds DIR] [-v] [--json]
```

**Needs a workbook that actually contains data.** A `.twbx` whose only connection is a published
datasource proxy (`class="sqlproxy"`) has no extract to sample — Agent 3 cannot help there and
Agent 2 is the only option. Falls back to synthetic per-table rather than failing the run.

---

## Agent 5 — Copilot Prompts

**Does:** turns whatever is still declined into review material for a human + Power BI Copilot.

**Skill:** `generate-copilot-fix-prompts`

```bash
python3 agent-5-copilot-prompts/copilot_prompts_agent.py <project|parent> [-o OUT] [--summary F] [-v] [--json]
```

**Produces two documents, split by REPORT IMPACT, not by gap type:**

| Document | For |
|---|---|
| `COPILOT_FIX_PROMPTS_MANDATORY.md` | every gap placed on a live worksheet visual/filter somewhere in the workbook -- fix these first |
| `COPILOT_FIX_PROMPTS_OPTIONAL.md` | gaps with no current report impact (an internal ingredient another calc references, or truly orphaned) -- safe to defer |

Within each document, gaps are further split into Section A (a `FIX_RULES` pattern matched the
decline reason -- prompt includes a specific starting DAX shape) and Section B (no pattern
matched -- the raw problem, no starting shape). Every one of `_work/residue.json`'s four
categories is covered: declined calculated fields/structural gaps (`calc_residue`, itself split
mandatory/optional via `field_has_report_impact()`), undecided visual types (`visual_undecided`),
dropped dashboard zones (`zone_gaps`), and Legend/Series/Size roles Power BI's own visual schema
rejected (`declined_encodings`) -- the last three are always mandatory (each is already a real,
currently-rendering dashboard element with a stated defect today).

Already-applied fixes are excluded from both — however a fix got into the model, this never asks
you to fix something that's already live.

> **A related, separate script exists but is NOT wrapped by this agent:**
> `.claude/skills/generate-copilot-fix-prompts/scripts/suggest_visual_candidates.py` triages
> `visual_undecided`/`zone_gaps` into `PRESENTATION_LAYER_REVIEW.md` (suggested visual type,
> needs-a-Tableau-lookup, self-resolving cascade, or structural-no-equivalent). It lives in the
> same skill folder as `generate_copilot_prompts.py` but Agent 5's own wrapper
> (`copilot_prompts_agent.py`) never calls it — run it directly if that specific triage document
> is wanted:
> ```bash
> python3 .claude/skills/generate-copilot-fix-prompts/scripts/suggest_visual_candidates.py <project|parent> [--out <folder>]
> ```

---

## Agent 4 — Validator

**Does:** independent QA on a finished PBIP. Does not convert anything.

**Skills:** `validate-tableau-pbip-mapping`, `validate-pbip-compliance`, `validate-dummy-data`,
`generate-copilot-fix-prompts` (`effort_report.py`)

```bash
python3 agent-4-validator/validator_agent.py <project|parent> [-v] [--json]
```

Runs 4 steps per project, in this order, and produces a 0–100 score and an APPROVED / needs-work
verdict, plus `validation/VALIDATION_SUMMARY.json`. Run it last — it is the check that any
hand-applied fixes did not break the model.

| Step | Skill | Writes |
|---|---|---|
| 1/4 Mapping | `validate-tableau-pbip-mapping` | `validation/MAPPING_VALIDATION/` -- a `<name>_mapping.xlsx` (Summary/Measures/Columns/Parameters/Gaps sheets) and `VALIDATE.dax` (one EVALUATE query per mapped measure/column). `mapping_percentage = 100 * mapped / (mapped + gaps)` **is** the quality score below. |
| 2/4 Compliance | `validate-pbip-compliance` | `validation/COMPLIANCE.json` -- schema/structural lint, independent of residue |
| 3/4 Data | `validate-dummy-data` (only if the project has data) | nothing extra by default now that Agent 2 no longer calls this internally -- Agent 4 is where this check actually runs |
| 4/4 Effort | `generate-copilot-fix-prompts` (`effort_report.py`) | `EFFORT_REPORT.md` + `effort_report.json` per project -- **does not affect quality_score/status**, it is a planning number, not a correctness check |

**Also produces the management effort report.** Step 4/4, `effort_report.py`, splits every
outstanding gap into SIX buckets. Every bucket still needs a human (or Copilot in Desktop) to
write the fix by hand -- the only distinction is how much of a running start the Copilot prompt
gives that human, the same split `COPILOT_FIX_PROMPTS_MANDATORY.md`/`_OPTIONAL.md` itself uses:

| Bucket | Meaning | Who does it |
|---|---|---|
| Already resolved | Live in the model, however it got there | Nobody — but never machine-verified, spot-check it |
| Recommended prompt | A `FIX_RULES` pattern matched — prompt has a specific starting DAX shape | Analyst + Copilot in Desktop |
| Raw prompt | No pattern matched — prompt hands over the raw problem, no starting shape | Analyst + Copilot in Desktop |
| Manual structural | A relationship/parameter/model-design decision, no DAX to author at all | Analyst, in Desktop |
| Visual undecided / Dropped zone | A visual whose type the deterministic rules couldn't pick, or a zone dropped for lack of a resolvable field | Analyst, checks Tableau |
| Color/Size encoding not reproduced | A Legend/Series/Size role Power BI's own visual schema rejects outright | Analyst, in Desktop (visual authoring, no DAX) |

It also writes a QA checklist (static DAX warnings that will error in Desktop, an unaccounted-
field count that should always be 0) and, when more than one project is validated together,
a batch `validation/EFFORT_SUMMARY.md` — total hours, a person-days estimate, and the top
recurring decline reasons **across the whole batch**, so fixing one systemic pattern (like the
DATEPARSE fix on WorkbookB Dashboard) shows up as the thing worth doing first, not buried per
workbook. When more than one distinct corpus group is present, it also writes
`validation/EFFORT_CORPUS_ROLLUP.md`, the same buckets grouped by corpus instead of by workbook.
This is the actual answer to "how much manual work is left across N dashboards, and
what will it take" — see [`agent-4-validator/README.md`](implementation/agent-4-validator/README.md).
Per-item effort minutes are a documented, adjustable assumption, not a measurement — see
`effort_report.py`'s `MINUTES_PER_ITEM`.

Pair it with the rule linter, which catches the failures Desktop reports badly or not at all:

```bash
cd tools && python3 pbip_rules/lint.py <project> --schemas
```

---

## Orchestrator

Split into two entry points by whether the output needs data wired in. Both share the same step
implementations (`orchestrator/pipeline_steps.py`) and run order — 1 → [2/3] → 5 → 4.

```bash
# No data generation -- output opens against its own real Power BI connection
python3 orchestrator/basic_pipeline_orchestrator.py <input.twbx> -o <dir> [--agent5] [--agent4] [--all-agents] [--config F] [-v]

# With data generation -- Agent 2 (dummy, wired IN PLACE) always runs; Agent 3 (realistic, own separate folder) is opt-in
python3 orchestrator/dummy_data_pipeline_orchestrator.py <input.twbx> -o <dir> [--agent3] [--agent5] [--agent4] [--all-agents] [--config F] [-v]
```

---

## Experimental extension agents (outside the numbered pipeline)

Three more agents exist under `implementation/`, each acting on an ALREADY-BUILT Agent 1 output
and writing a SEPARATE, new copy of the project with additional table calculations promoted from
declined to real DAX measures. None of these run as part of `basic_pipeline_orchestrator.py` or
`dummy_data_pipeline_orchestrator.py` — they are invoked directly, one workbook at a time, and
never modify the project they read from.

| Agent | Skill | Promotes |
|---|---|---|
| `agent-extend-tablecalc-rank` | `extend-tablecalc-rank` | RANK/RANK_DENSE with "Compute Using: Specific Dimension(s)" addressing (a second, safe case beyond the one Agent 1 already handles) |
| `agent-extend-tablecalc-quickcalcs` | `extend-tablecalc-quickcalcs` | Tableau's built-in "quick table calculation" buttons (Percent of Total templated today; Difference/Running Total/Moving Average identified but not yet templated) |
| `agent-extend-dax-measures` | `extend-dax-measures` | `LOOKUP(<expr>, 0)` same-row passthroughs, `INDEX()` row-position calcs, and fixed/dynamic-window `WINDOW_SUM` moving sums/averages |

```bash
python3 agent-extend-tablecalc-rank/tablecalc_rank_extension_agent.py <workbook.twbx> <existing_pbip_dir> -o <new_pbip_dir>
python3 agent-extend-tablecalc-quickcalcs/tablecalc_quickcalcs_extension_agent.py <workbook.twbx> <existing_pbip_dir> -o <new_pbip_dir>
python3 agent-extend-dax-measures/dax_measures_extension_agent.py <workbook.twbx> <existing_pbip_dir> -o <new_pbip_dir>
```

Each writes its own `*_EXTENSION_REPORT.md` into `<new_pbip_dir>` listing every promotion (with
its DAX) and every candidate checked and declined, with a specific reason. **Measured so far:**
`extend-tablecalc-quickcalcs` is the only one with a verified positive result on a real corpus
workbook (2 Percent-of-Total measures promoted on WorkbookB Dashboard, out of 5 candidates found);
`extend-tablecalc-rank` and `extend-dax-measures` have each been run against one real workbook and
promoted 0 measures there (the candidates present had no resolvable addressing) — architecturally
sound but not yet demonstrated to fire successfully on a real workbook. None of the three has been
folded into Agent 1's own `build-tableau-pbip` skill.

**Retired:** `_retired_agent-6-apply-fixes` (an "Apply Fixes" agent that would have written
reviewed DAX directly into a project's real `.tmdl`) is kept in the repo, unused, for reference.
The pipeline's actual position is that no agent writes into the real `.SemanticModel`
automatically — see [Where the LLM actually belongs](#where-the-llm-actually-belongs) below.

**Not an agent, despite the name:** `agent-extend-aryan` (also under `implementation/`) is a
documentation-only gotcha-file folder for bugs already fixed directly in the main pipeline's own
code — no `.py` script, nothing runnable, not one of the three extension agents above. See its
own `README.md` before assuming otherwise.

---

## Where the LLM actually belongs

The agents are deterministic on purpose. Judgment happens in exactly three places, none of them
inside an agent:

1. **Reading a Tableau screenshot to decide a visual type.** 62% of corpus worksheets use the
   `Automatic` mark, which Tableau resolves at *render* time — the chart type is genuinely not in
   the file. A screenshot is new information, not better reasoning.
2. **Hand-authoring DAX for a gap the parser declines.** Example: `cl.Base Date` uses Tableau's
   `DATE()` in both senses at once — an outer no-op cast and an inner string parse — so it needs a
   human to drop one and convert the other.
3. **Deciding what a free-entry parameter should become** (fixed list? what-if parameter? accept the
   single value?).

The handoff back is Agent 5's `COPILOT_FIX_PROMPTS_MANDATORY.md`/`_OPTIONAL.md`: a human (or
Copilot, with the real model open in Desktop) reads the prompt, writes the DAX, and applies it
themselves directly in the `.tmdl` -- there is no automated write-back step. That's the boundary
between judgment and machinery: the pipeline hands over exactly enough context to make the
judgment call, and never tries to make the call itself.

---

## What none of the agents can fix

Honest limits, so time is not spent hunting them:

- **Table calculations** whose addressing/partitioning lives in Tableau's *Edit Table Calculation*
  dialog. `WINDOW_SUM`, `INDEX`, `LOOKUP` with a non-zero offset. Not in the formula text.
- **`EXCLUDE`/`INCLUDE` LOD**, which is relative to a view's own dimension set.
- **All visual formatting** — colours, fonts, number formats, axis and legend config, sort order.
  Not extracted at all. This is the largest remaining source of "it does not look like Tableau".
- **Tableau zone types with no Power BI construct** — `dashboard-object`, `empty`, embedded web
  objects.
- **Federated-datasource joins** where the second table is not independently queryable.

`COVERAGE.md` states these per build, in its own "What this does not establish" section. Trust that
over any summary, including this one.

---

## Convention worth keeping

Agents parse the skills' summary lines **by name, not position** — `already_fixed_excluded=20`
matched as a `name=number` pair rather than as the third field. Agent 5 originally used one positional regex
listing every counter in order; when the prompts skill grew `root_causes`, `cascade` and
`already_fixed_excluded`, it matched *nothing* and silently reported zero projects instead of
failing loudly. Keep new counters additive.
