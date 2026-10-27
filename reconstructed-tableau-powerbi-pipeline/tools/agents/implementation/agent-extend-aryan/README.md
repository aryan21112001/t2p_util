# agent-extend-aryan

Gotcha files for the Tableau → Power BI pipeline (`.claude/skills/build-tableau-pbip/`,
`.claude/skills/generate-dummy-data/`, `tools/agents/implementation/orchestrator/`).

**Different in kind from its siblings in this folder.** `agent-extend-tablecalc-rank`,
`agent-extend-tablecalc-quickcalcs`, and `agent-extend-dax-measures` (also under
`implementation/`) are real, runnable extension AGENTS — each is a `.py` script + README wrapping
its own skill, acting on an already-built project and writing a new, separate copy with extra
measures promoted. This folder has no `.py` script and runs nothing. It's a **documentation-only**
reference: gotcha files for bugs already fixed directly IN the main pipeline's own code
(`extract.py`/`names.py`/`dax.py`/`tmdl.py`/`pbir.py`), not a new capability layered on top of an
existing build. See `tools/agents/README_AGENTS.md` for the authoritative map of every real agent
(numbered 1–5 plus the three experimental extension agents) — that doc, not this one, is where a
NEW extension agent should be registered.

**Every fix listed here is already in the code.** Nothing in this folder needs to be re-applied
by hand — it exists so that the NEXT time this pipeline runs on a NEW workbook (not just WorkbookA,
any `.twbx`/`.twb`), whoever is debugging a weird result can grep this folder for the symptom
first, confirm "yes, that class of bug is already handled," and go look for a genuinely NEW
cause instead of re-discovering an old one. If a bug described here ever recurs, that means the
fix regressed — check the self-test for that fix first (`python <script>.py --selftest`).

**Note on scope — read this before telling anyone a table calculation is unfixable.** This
folder's own `04-dax-translation-gotchas.md` does NOT cover Tableau table calculations
(`INDEX()`, `RANK`, `WINDOW_SUM`, `LOOKUP`) at all — those are declined by the MAIN pipeline on
purpose (`tmdl.py`'s `route()`, "addressing/partitioning is not in the formula text"), but three
of this folder's sibling extension agents exist SPECIFICALLY to promote a subset of them when
their addressing happens to be resolvable from worksheet-saved settings: `RANK`/`RANK_DENSE`
(`agent-extend-tablecalc-rank`), Percent-of-Total and other quick-calc buttons
(`agent-extend-tablecalc-quickcalcs`), and `LOOKUP(<expr>,0)`/`INDEX()`/`WINDOW_SUM`
(`agent-extend-dax-measures`). Before telling a user "this `INDEX()`/`RANK` field needs a human
and Tableau's Edit Table Calculation dialog," run the relevant extension agent against the
existing build first — it costs nothing (writes a separate copy, never touches the original).

**Run against WorkbookA specifically (2026-10-06), all three, verified live via `pbi-cli`, not just
trusted from the generated report:**

| Agent | Promoted | Live-verified result |
|---|---|---|
| `agent-extend-tablecalc-rank` | 0 of 0 candidates | WorkbookA has no `RANK`/`RANK_DENSE` table calcs at all — nothing to check. |
| `agent-extend-dax-measures` | 1 of 9 candidates (`Usage Overview`'s `index`) | ❌ **Loads fine, computes WRONG.** `RANKX(ALLEXCEPT('Usage Overview', [coridr_mcg]), [coridr_mcg], ...)` as a MEASURE requires one row per distinct `coridr_mcg` value — confirmed via direct query that it does NOT (`"0"` alone has 1,794 rows, `"SUPERMARKETS"` has 47, etc.), so evaluating it throws "a single value for column 'coridr_mcg' cannot be determined." This is EXACTLY the risk the extension's own "Before trusting this build" checklist names ("confirm the model's grain... is one row per distinct value") — here it's confirmed false. The other 8 of WorkbookA's 9 real `INDEX()`/`WINDOW_*`/`RUNNING_SUM` candidates stayed correctly declined (4 "Mandatory" ones have no Specific-Dimension(s) addressing recorded anywhere — their real Tableau "Compute Using" mode is "Table (Down)", which this skill doesn't implement; 4 "Optional" ones are genuinely dead fields or use an unimplemented function). **Do not ship this one promotion as-is.**
| `agent-extend-tablecalc-quickcalcs` | 8 of 45 candidates (Percent-of-Total, `Recommendations`/`Dormancy`) | ✅ **Correct, verified two ways.** A grand-total variant (`cl.tot_crds_cnt - % of Total`) returns exactly `1.0` with no filter (= 100% of itself) and correctly re-fractions to `0.0595`/`0`/`0.9405`/`0` across `Dormancy[For View]`'s four real values (summing back to 1.0). A partitioned variant (`Issr_mcg_30_days`) returns sane 0–1 fractions summing correctly across the partition. Both tables (`Recommendations`, `Dormancy`) Full-refresh cleanly with the new measures in place. Markedly better than this skill's only other prior real-corpus result (2 promotions on WorkbookB Dashboard) — **worth promoting into the real build.**

Both builds pass `tools/pbip_rules/lint.py --schemas` clean (only pre-existing stray-file notes,
unrelated). Outputs: `C:\pbi\ext_dax_measures`, `C:\pbi\ext_tablecalc_rank`,
`C:\pbi\ext_tablecalc_quickcalcs` — all separate copies of `C:\pbi\out2\WorkbookA_realistic\WorkbookA` as
it stood at the time, original untouched.

**Merged into the canonical `out2` deliverable (2026-10-06, same day):** the 8 good
`Recommendations`/`Dormancy` quick-calc measures were hand-copied (via a small Python script
copying the exact TMDL lines, not retyped) into a FRESH rebuild of `out2` — fresh because `out2`
was itself stale relative to the DATE/parameter-aggregate/`NULL`-idiom `dax.py` fixes above at
the time this merge happened. The 1 bad `Usage Overview` `index` measure (wrong at this
workbook's real data grain, see table above) was deliberately left OUT. Re-verified live
afterward via a fresh isolated Desktop copy: all 8 tables Full-refresh cleanly, the merged
`Dormancy` grand-total measure still returns `1.0`, and a previously-fixed value
(`Activation Rate_issr` filtered `For View="Trend"` → `0.0875`) is unchanged — confirming the
merge didn't disturb anything already working. `C:\pbi\out2\WorkbookA_realistic\WorkbookA\WorkbookA.pbip` is
now the ONE current deliverable; see its own `START_HERE.md`.

## How this folder relates to `SKILL.md`

`.claude/skills/build-tableau-pbip/SKILL.md` is the authoritative, detailed record — every entry
here links back to it. This folder is the **fast-scan index**: one short entry per bug (symptom →
root cause → fix location → status), organized by WHICH FILE you'd be looking at if you hit the
symptom, instead of SKILL.md's single long chronological table. Read SKILL.md's own entry (search
for a distinctive phrase quoted here) when you need the full reasoning, the exact regression test,
or the real-corpus numbers behind a fix.

## Files

| File | Covers |
|---|---|
| [01-orchestrator-gotchas.md](01-orchestrator-gotchas.md) | `tools/agents/implementation/orchestrator/*`, `generate-dummy-data` skill — pipeline wiring, output folders, data sampling |
| [02-extraction-gotchas.md](02-extraction-gotchas.md) | `extract.py` — reading Tableau's `.twb` XML into the IR |
| [03-name-authority-gotchas.md](03-name-authority-gotchas.md) | `names.py` — allocating/resolving every PBI identifier |
| [04-dax-translation-gotchas.md](04-dax-translation-gotchas.md) | `dax.py` — Tableau formula → DAX recursive-descent parser |
| [05-semantic-model-gotchas.md](05-semantic-model-gotchas.md) | `tmdl.py` — TMDL semantic model writer, resolution fixpoint |
| [06-report-pbir-gotchas.md](06-report-pbir-gotchas.md) | `pbir.py` — PBIR report writer, visual type/field-well mapping |

## Status at a glance

All fixes below were found by running the pipeline against real workbooks (NGPO, Caribbean -
Drilldown, WorkbookB Dashboard, SMB IRF Program, PVT Monthly Report, AP Product Portfolio
Management, Digital Sales - Leads, WorkbookA — 8 workbooks across the reference corpus), not by
reading the code and guessing. Each one is:

- **Fixed** in the script named — not a workaround, not a manual patch to generated output.
- **Self-tested** — every fix has a regression test in that script's own `--selftest` block,
  built from the real failing case (verbatim formula/XML shape where possible), not just the
  happy path.
- **Fail-closed where a fix wasn't safe** — several entries below are "this now DECLINES
  cleanly instead of silently guessing wrong," not "this now translates." A missing field is
  recoverable from `RESIDUE.md`; a silently-wrong one is the failure this whole pipeline exists
  to prevent. Don't mistake a decline-fix for a translate-fix when reading the counts.

Running total as of 2026-10-08: **48 distinct fixes** across the four scripts below, plus the
orchestrator/data-sampling fixes in file 01. 12 of them were found specifically while migrating
WorkbookA (this engagement); the rest predate WorkbookA and are already baked in for every workbook that's
been run since they were found.

**2026-10-08 addition — a long-OPEN mystery got explained, not fixed in code.**
`01-orchestrator-gotchas.md` entry 5 ("fresh rebuilds render several barChart visuals with zero
bars, no error") sat unresolved for 2 days. Root cause turned out to be Desktop's own path-keyed
rendering cache, not a pipeline bug — confirmed by reproducing the exact blank visual's query via
`pbi-cli` (always correct), then opening the SAME content from a brand-new, never-before-seen
folder path (always renders correctly). That entry now has a 3-step detection procedure (DAX
repro → `pbip_lint.py --schemas` → fresh-path test) so this doesn't cost another 2 days on the
next workbook. Three genuine code fixes landed alongside it: `generate_dummy_data.py`'s
whole-number-float-to-string coercion (a `datatype-customized='true'` string-cast-of-double
column), and two more `pbir.py` resolution-scoping gaps in `_resolve_field`'s `if agg:` branch
and `_query_state`'s zero-measure-worksheet fallback — both the SAME global-first-match hazard
as the already-documented `_visual_filters`/`_query_state` fixes, recurring in code paths those
fixes never touched.
