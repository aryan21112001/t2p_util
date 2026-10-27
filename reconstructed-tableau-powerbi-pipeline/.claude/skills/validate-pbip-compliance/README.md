# validate-pbip-compliance Skill

Validates generated Power BI projects (.pbip) for compliance with platform-specific rules and best practices.

## What it does

Runs 42 compliance checks against a PBIP project to catch structural issues that Power BI Desktop:
1. Won't report (silent failures with broken projects)
2. Reports with misleading error messages
3. Catches via schema validation

Each check encodes a concretely-observed failure with the symptom it produces.

## Usage

```bash
# Console output (human readable)
python3 validate_pbip_compliance.py <path_to_pbip>

# JSON output (machine readable)
python3 validate_pbip_compliance.py <path_to_pbip> --json output.json

# Also validate against Microsoft JSON schemas
python3 validate_pbip_compliance.py <path_to_pbip> --schemas
```

### Arguments
- `<path>`: Path to PBIP project folder(s) or parent directory containing multiple projects
- `--json <output_file>`: Write findings to JSON file instead of stdout
- `--schemas`: Also validate against Microsoft JSON schemas (requires `jsonschema` package)

### Returns
- `0`: All checks passed (PASSED)
- `1`: Compliance failures found (FAILED)
- `2`: Error (invalid arguments, missing files, etc.)

## Output Format

### Console Output
```
NOTES (2):
  - [Project] check: note message
  - [Project] check: note message

FAILED (3 problems across 1 project(s)):
  - [Project] check: failure message
  - [Project] check: failure message
  - [Project] check: failure message
```

### JSON Output
```json
{
  "projects_checked": 1,
  "failures": ["[Project] check: failure message"],
  "notes": ["[Project] check: note message"],
  "failure_count": 1,
  "note_count": 1,
  "status": "FAILED"
}
```

## Checks Performed

The skill runs the following categories of checks:

### Report Definition Checks
- `version.json` version must be "2.0.0" (not "1.0.0" which causes zero page tabs)
- `definition.pbir` and `definition.pbism` versions must be ≥ 4.0
- `datasetReference.byPath` references must resolve to valid folders

### Semantic Model Checks
- V3 marker (`powerBI_V3`) required in both TMSL and TMDL formats
- No duplicate member names within tables
- Valid data types (not arbitrary strings)
- Partition and DAX formula validation
- Column and measure reference validation

### Report Checks
- Resource packages properly defined
- Image URLs are resolvable
- Page definitions are valid
- Visual data bindings are correct
- Object names follow requirements

### Advanced Checks
- `pbip_rules/checks.py`: 38 checks against parsed model structure
- Optional JSON schema validation against Microsoft schemas

## Dependencies

- Python 3.7+
- `openpyxl` (optional, for Excel validation in validate-dummy-data)
- `jsonschema` (optional, for `--schemas` mode)

## Related Files

- `pbip_lint.py`: Core linting implementation
- `pbip_rules/`: Model parsing and advanced compliance checks
- `/tools/pbip_rules/`: Shared infrastructure for other skills

## Integration

Called by:
- Agent 1 (Converter): Validates converted PBIP projects
- Can be called standalone for batch validation
