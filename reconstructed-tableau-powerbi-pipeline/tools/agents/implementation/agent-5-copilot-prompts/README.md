# Agent 5: Copilot Fix Prompt Generator

**Purpose**: Turns a build-tableau-pbip (or generate-dummy-data) output's declined-translation
residue into ready-to-paste Copilot prompts, so a reviewer can hand each gap straight to Power BI
Desktop's Copilot instead of re-deriving the fix by hand for every instance.

## Use Cases

1. **Parity Punch List** — After Agent 1/2 convert a workbook, generate the exact set of prompts
   a reviewer needs to paste into Copilot to close the remaining gap to the original Tableau
   dashboard.
2. **Batch Prompt Generation** — Run across every dashboard in a corpus at once.
3. **Reviewer Handoff** — One file per project a non-pipeline-author can work through in Desktop.

## Usage

```bash
# Single project (build-tableau-pbip or generate-dummy-data output)
python3 copilot_prompts_agent.py /path/to/output/dummy_pbip/MyDashboard

# Every project under a parent directory
python3 copilot_prompts_agent.py /path/to/output/dummy_pbip

# Redirect output elsewhere instead of writing in place
python3 copilot_prompts_agent.py /path/to/output/dummy_pbip --out /path/to/reports

# Verbose + JSON
python3 copilot_prompts_agent.py /path/to/output/dummy_pbip -v --json
```

## Architecture

```
Input: project path or parent directory
  ├─ Step 1: Discover project(s) with a _work/residue.json
  ├─ Step 2: Generate prompts (call-skill: generate-copilot-fix-prompts)
  ├─ Step 3: Parse the skill's per-project summary line
  ├─ Step 4: Aggregate and persist a run summary
  └─ Output: COPILOT_FIX_PROMPTS.md per project + COPILOT_PROMPTS_RUN_SUMMARY.json
```

## Output Files

Per project (written in place next to `RESIDUE.md`/`COVERAGE.md`, unless `--out` redirects it):

- **COPILOT_FIX_PROMPTS.md**
  - **DAX Measures to Fix** — declined calculated fields grouped by gap pattern (reusing
    `validate-tableau-pbip-mapping`'s own `FIX_RULES` table), each with the affected field names,
    their real Tableau formulas, why translation declined, what the DAX needs to do, and one
    ready-to-paste Copilot prompt covering the whole group.
  - **Structural / Model-Design Gaps (not DAX)** — declined items with no captured Tableau
    formula (physical-layer joins, free-entry parameters, etc.) — not calculated fields, so no
    DAX prompt; instead a design-question prompt aimed at Power Query/Model view.
  - **Other Metadata Gaps** — undecided visual types and dropped zones, with background and a
    manual Desktop-action note.

At the run level:

- **COPILOT_PROMPTS_RUN_SUMMARY.json** — per-project gap counts (`dax_gaps`, `structural_gaps`,
  `visual_undecided`, `zone_gaps`) and each project's output file path.

## Skills Called

1. **generate-copilot-fix-prompts**
   - Reads each project's own `_work/residue.json`
   - Groups declined items by pattern (via `FIX_RULES`, imported from
     `validate-tableau-pbip-mapping`'s script, not duplicated)
   - Output: `COPILOT_FIX_PROMPTS.md`

## Exit Codes

| Code | Condition |
|------|-----------|
| 0 | At least one project processed successfully |
| 2 | Input path missing, or no `_work/residue.json` found anywhere under it |

## Known Limits

- Only reads `_work/residue.json` — does not re-run `validate-tableau-pbip-mapping`'s two static
  DAX checks (see that skill's own `DAX Warnings` sheet for those).
- A DAX-writing prompt is only generated for items that carry a captured Tableau formula; items
  without one (relationships, parameters) get a structural/design prompt instead, never a fake
  DAX request.
