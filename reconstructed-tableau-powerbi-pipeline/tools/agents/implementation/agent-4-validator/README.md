# Agent 4: PBIP Validator

**Purpose**: Validates existing Power BI projects (.pbip) for quality, compliance, and data integrity.

## Use Cases

1. **Independent QA Validation** — Validate PBIP without re-converting
2. **Batch Validation** — Run validation on multiple PBIP projects
3. **Compliance Audits** — Check PBIP compliance across portfolio
4. **Post-Generation Validation** — Validate Agent 1/2/3 outputs
5. **Regression Testing** — Ensure PBIP quality over time

## Usage

```bash
# Validate single PBIP project
python3 validator_agent.py /path/to/project.pbip

# Validate directory containing multiple PBIP projects
python3 validator_agent.py /path/to/projects/

# Verbose output
python3 validator_agent.py /path/to/projects/ -v

# JSON output format
python3 validator_agent.py /path/to/projects/ --json
```

## Architecture

```
Input: PBIP project path(s)
  ├─ Step 1: Discover PBIP projects
  ├─ Step 2: Validate mapping quality (call-skill: validate-tableau-pbip-mapping)
  ├─ Step 3: Validate PBIP compliance (call-skill: validate-pbip-compliance)
  ├─ Step 4: Validate data integrity (call-skill: validate-dummy-data, if has data)
  ├─ Step 5: Effort/QA report (call-skill: generate-copilot-fix-prompts → effort_report.py)
  ├─ Step 6: Aggregate results and calculate quality score
  ├─ Step 7: Persist validation reports
  └─ Output: Exit code + JSON validation reports
```

## Output Files

For each validated PBIP project:
- **VALIDATION_REPORT.json** — Detailed validation results
  - Mapping quality metrics
  - Compliance findings (failures & notes)
  - Data validation results
  - Effort/QA numbers (see below)
  - Quality score (0-100)
  - Status (APPROVED/NEEDS_REVIEW/FAILED)

- **VALIDATION_SUMMARY.json** — High-level summary
  - Project name and path
  - Quality score
  - Status and exit code
  - Compliance issue count
  - Mapping quality percentage
  - `effort`: `{already_resolved, recommended_prompt, raw_prompt, manual_structural, visual_undecided, zone_gap, est_hours}`

- **`validation/<Project>/EFFORT_REPORT.md`** + **`effort_report.json`** — the actual
  management-facing document: every outstanding item split into **already resolved** (live in
  the model, however it got there — zero effort, but never machine-verified, spot-check it),
  **recommended prompt** (a `FIX_RULES` pattern matched, so the Copilot prompt has a specific
  starting DAX shape — lower effort), **raw prompt** (no pattern matched — the Copilot prompt
  hands over the raw problem, diagnose from scratch) and **manual investigation** (a structural/
  visual/zone gap — needs a person in Tableau, no DAX to write at all), with an estimated-hours
  total per bucket and a QA checklist (static DAX warnings that will error in Desktop,
  unaccounted-field build bugs). Hours come from a documented, adjustable per-item-minutes
  assumption in
  `generate-copilot-fix-prompts/scripts/effort_report.py`'s `MINUTES_PER_ITEM` — not a
  measurement. **This is what does NOT affect `quality_score`** — effort is a planning number,
  not a correctness signal.

- **`validation/EFFORT_SUMMARY.md`** + **`effort_summary.json`** — written automatically
  whenever more than one project is validated in the same run: aggregate hours across the whole
  batch, a person-days estimate, and the top recurring decline reasons across every workbook
  (fixing one of these in `dax.py`/`FIX_RULES` benefits every workbook that hits it, not just
  one) — this is the report to hand to management for a "how much work is left across N
  dashboards" estimate.

## Quality Scoring

**Base**: 100 points

**Deductions**:
- Mapping validation: Replace base with mapping_percentage (0-100)
- Compliance: -1 per failure, -0.5 per note
- Data validation: -10 if data present but validation failed

**Final Score**: 0-100 (clamped)

## Exit Codes

| Code | Status | Score | Condition |
|------|--------|-------|-----------|
| 0 | APPROVED | ≥90 | Perfect mapping + no compliance failures |
| 1 | NEEDS_REVIEW | 75-89 | Good but has minor issues |
| 2 | FAILED | <75 | Significant issues found |

## Exit Behavior

- Returns **worst exit code** across all projects
- Validates all projects even if one fails
- One or more projects FAILED → exit 2
- All APPROVED → exit 0
- Mixed → exit 1 (worst that's not 2)

## Skills Called

1. **validate-tableau-pbip-mapping**
   - Checks conversion quality
   - Cross-references Tableau→PowerBI mapping
   - Output: MAPPING_VALIDATION.json

2. **validate-pbip-compliance**
   - Checks 42 PBIP compliance rules
   - Validates against Power BI best practices
   - Output: COMPLIANCE.json

3. **validate-dummy-data** (if project has data)
   - Validates data integrity
   - Checks Excel file structure
   - Verifies M expression correctness
   - Output: Data validation results

4. **generate-copilot-fix-prompts** (`effort_report.py`)
   - Splits every outstanding gap into already-resolved / recommended-prompt / raw-prompt /
     manual-investigation
   - Reuses `validate_mapping.suggest_fix` directly, so this can never disagree with what
     `COPILOT_FIX_PROMPTS.md`'s own Section A/B split shows
   - Output: `EFFORT_REPORT.md` per project, `EFFORT_SUMMARY.md` for a batch

## Integration

Called by:
- QA automation pipelines
- Compliance audit workflows
- Standalone validation requests
- Batch processing of multiple PBIP projects

## Examples

### Validate Single Project
```bash
$ python3 validator_agent.py /data/pbip_output/my_project.pbip
[INFO] Found 1 PBIP project(s)

Validating: my_project.pbip
Path: /data/pbip_output/my_project.pbip
Step 1/4: Validating mapping quality...
✓ Mapping: 95%
Step 2/4: Checking PBIP compliance...
✓ No compliance issues
Step 3/4: No data to validate (stub project)
Step 4/4: Computing effort/QA report...
✓ Effort: 12 auto-fixed, 3 Copilot-suggested, 8 manual, ~5.2h remaining
Quality Score: 95/100 → APPROVED (exit code: 0)
✓ Saved validation report: /data/pbip_output/VALIDATION_REPORT.json
✓ Saved validation summary: /data/pbip_output/VALIDATION_SUMMARY.json

============================================================
VALIDATION SUMMARY
============================================================
my_project.pbip                             APPROVED        (95/100)

Total: 1 projects | 1 approved

$ echo $?
0
```

### Batch Validate Directory
```bash
$ python3 validator_agent.py /data/pbip_output/

[INFO] Found 5 PBIP project(s)
Validating: Dashboard1.pbip
...
Validating: Dashboard5.pbip
...

============================================================
VALIDATION SUMMARY
============================================================
Dashboard1.pbip                             APPROVED        (100/100)
Dashboard2.pbip                             APPROVED        (98/100)
Dashboard3.pbip                             NEEDS_REVIEW    (78/100)
Dashboard4.pbip                             FAILED          (42/100)
Dashboard5.pbip                             APPROVED        (92/100)

Total: 5 projects | 3 approved

$ echo $?
2
```

### Output Files Example
```json
{
  "project": "my_project.pbip",
  "path": "/data/pbip_output/my_project.pbip",
  "timestamp": "2026-09-01T13:56:00.000000",
  "has_data": false,
  "validations": {
    "mapping": {
      "mapping_percentage": 95,
      "issues": [],
      "warnings": []
    },
    "compliance": {
      "failures": [],
      "notes": [],
      "failure_count": 0,
      "note_count": 0
    },
    "data": {
      "success": true,
      "note": "No data present"
    }
  },
  "quality_score": 95,
  "status": "APPROVED",
  "exit_code": 0
}
```

## Implementation Details

- **Lines**: ~450
- **Pattern**: Skills-first orchestration (identical to Agents 1-3)
- **Skills**: 3 validation skills (mapping, compliance, data)
- **Result Persistence**: JSON files with full audit trail
- **Project Discovery**: Recursive search for .Report + .SemanticModel
- **Data Detection**: Checks for non-empty Data directory with Excel files
