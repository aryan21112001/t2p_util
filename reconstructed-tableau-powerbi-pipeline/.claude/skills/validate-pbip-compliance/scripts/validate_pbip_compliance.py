#!/usr/bin/env python3
"""Wrapper script for PBIP compliance validation skill.

Exposes pbip_lint functionality as a CLI with JSON output option.

Usage:
    python3 validate_pbip_compliance.py <path> [--json <output_file>] [--schemas]
    
    <path>: Path to PBIP project folder(s)
    --json <output_file>: Write findings to JSON file instead of stdout
    --schemas: Also validate against Microsoft JSON schemas
    
Returns:
    0: All checks passed
    1: Failures found
    2: Error (invalid arguments, missing files, etc.)
"""

import json
import sys
from pathlib import Path

# Import from local pbip_lint and pbip_rules
sys.path.insert(0, str(Path(__file__).parent))
from pbip_lint import Findings, lint_project


def main(argv: list[str]) -> int:
    """Main entry point matching the skill CLI interface."""
    args = [a for a in argv[1:] if not a.startswith("-")]
    json_output = None
    with_schemas = False
    
    # Parse flags
    for i, arg in enumerate(argv[1:]):
        if arg == "--json" and i + 1 < len(argv) - 1:
            json_output = argv[i + 2]
        elif arg == "--schemas":
            with_schemas = True
    
    if not args:
        print(__doc__)
        return 2
    
    target = Path(args[0])
    if not target.exists():
        print(f"error: no such path: {target}", file=sys.stderr)
        return 2
    
    # Find projects
    if list(target.glob("*.pbip")) or list(target.glob("*.Report")):
        projects = [target]
    else:
        found = sorted({p.parent for p in target.glob("*/*.pbip")})
        if found:
            projects = found
        else:
            projects = sorted({p.parent for p in target.glob("*/*.Report")})
    
    if not projects:
        print(f"error: no PBIP projects found under {target}", file=sys.stderr)
        return 2
    
    # Run linting
    findings = Findings()
    for project in projects:
        lint_project(project, findings, with_schemas=with_schemas)
    
    # Output results
    if json_output:
        output_data = {
            "projects_checked": len(projects),
            "failures": findings.failures,
            "notes": findings.notes,
            "failure_count": len(findings.failures),
            "note_count": len(findings.notes),
            "status": "FAILED" if findings.failures else "PASSED"
        }
        Path(json_output).write_text(json.dumps(output_data, indent=2))
        print(f"✓ Compliance report written to {json_output}")
    else:
        # Console output
        if findings.notes:
            print(f"NOTES ({len(findings.notes)}):")
            for note in findings.notes:
                print(f"  - {note}")
            print()
        
        if findings.failures:
            print(f"FAILED ({len(findings.failures)} problems across {len(projects)} project(s)):")
            for failure in findings.failures:
                print(f"  - {failure}")
            return 1
        
        print(f"PASSED: {len(projects)} project(s) clean.")
    
    return 0 if not findings.failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
