#!/usr/bin/env python3
"""
AGENT 4: PBIP Validator (Skills-First)
Validates existing Power BI projects (.pbip) for quality, compliance, and data integrity.

Architecture:
  1. Discover PBIP project(s) from input path
  2. Validate conversion quality (validate-tableau-pbip-mapping)
  3. Validate PBIP compliance (validate-pbip-compliance)
  4. Validate data integrity (validate-dummy-data, if has data)
  5. Blend Key domain-overlap check (validate-blend-key-domains, if has data) -- does every
     Blend Key many-to-many relationship (build-tableau-pbip's own translation of Tableau data
     blending) actually have any overlapping real key values between its two tables. Report-
     only, detect-and-flag, never mutates relationships.tmdl.
  6. Effort/QA report (generate-copilot-fix-prompts: effort_report.py) -- how much manual work
     is left, split by whether a Copilot candidate already exists, plus what to QA-verify
  7. Aggregate results and scoring
  8. Persist validation reports
  9. Return status and exit code
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any, List


def call_skill(skill_name: str, *args, **kwargs) -> Dict[str, Any]:
    """Execute a skill and return the result."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    skills_dir = repo_root / ".claude" / "skills"
    
    if skill_name == "validate-tableau-pbip-mapping":
        # validate_mapping.py expects the *parent* of a build-tableau-pbip workbook folder --
        # it lists that folder's children looking for a _work/ir.json one level down. Passing
        # pbip_path itself makes it iterate pbip_path's own contents and find nothing.
        pbip_path = Path(args[0])
        build_out = pbip_path.parent
        validation_dir = pbip_path.parent / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        output_report = validation_dir / "MAPPING_VALIDATION"
        script = skills_dir / "validate-tableau-pbip-mapping" / "scripts" / "validate_mapping.py"
        cmd = [sys.executable, str(script), str(build_out), "--out", str(output_report)]

    elif skill_name == "validate-pbip-compliance":
        pbip_path = Path(args[0])
        validation_dir = pbip_path.parent / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        output_report = validation_dir / "COMPLIANCE.json"
        script = skills_dir / "validate-pbip-compliance" / "scripts" / "validate_pbip_compliance.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--json", str(output_report)]
        
    elif skill_name == "validate-dummy-data":
        pbip_path = Path(args[0])
        script = skills_dir / "validate-dummy-data" / "scripts" / "validate_dummy_data.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--quiet"]

    elif skill_name == "generate-effort-report":
        pbip_path = Path(args[0])
        validation_dir = pbip_path.parent / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        script = skills_dir / "generate-copilot-fix-prompts" / "scripts" / "effort_report.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--out", str(validation_dir)]

    elif skill_name == "validate-blend-key-domains":
        pbip_path = Path(args[0])
        validation_dir = pbip_path.parent / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        script = (skills_dir / "validate-blend-key-domains" / "scripts"
                  / "validate_blend_key_domains.py")
        cmd = [sys.executable, str(script), str(pbip_path)]

    else:
        return {'success': False, 'output': None, 'error': f'Unknown skill: {skill_name}'}
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(skills_dir)
        )
        
        if result.returncode != 0:
            return {
                'success': False,
                'output': None,
                'error': result.stderr or f'Skill {skill_name} failed with exit code {result.returncode}'
            }
        
        return {
            'success': True,
            'output': result.stdout.strip(),
            'error': None
        }
        
    except subprocess.TimeoutExpired:
        return {'success': False, 'output': None, 'error': f'Skill {skill_name} timed out after 300s'}
    except Exception as e:
        return {'success': False, 'output': None, 'error': str(e)}


def _mapping_stats_re(project_name: str) -> re.Pattern:
    return re.compile(
        r"\[ok\]\s+" + re.escape(project_name) +
        r"\s+mapped=\s*(\d+)\s+gaps=\s*(\d+)\s+unaccounted=(\d+)\s+collisions=(\d+)"
    )


def parse_mapping_result(output: str, project_name: str) -> Dict[str, Any]:
    """
    Parse validate_mapping.py's stdout summary line for THIS project, e.g.:
      "[ok] <name>  mapped=  55 gaps=  10 unaccounted=0 collisions=0  -> <path>"

    The skill has no JSON output mode -- it writes an .xlsx report per workbook and a
    Markdown rollup, and prints ONE such line PER WORKBOOK IT FINDS UNDER build_out (which is
    pbip_path.parent, i.e. every sibling project already sitting in the same output directory --
    not just this one). Anchoring the match to `project_name` is required: a plain "first match"
    search silently returns whichever sibling happens to sort first alphabetically, which is wrong
    for every OTHER project once more than one has been built into the same output directory.
    """
    match = _mapping_stats_re(project_name).search(output or "")
    if not match:
        return {
            'mapping_percentage': 0,
            'issues': [f"Could not find a mapping stats line for '{project_name}' in validator output"],
            'warnings': [],
        }

    mapped, gaps, unaccounted, collisions = (int(g) for g in match.groups())
    total = mapped + gaps
    mapping_percentage = round(100 * mapped / total) if total else 100

    issues = []
    if unaccounted:
        issues.append(f"{unaccounted} field(s) unaccounted for -- neither mapped nor in residue")
    if collisions:
        issues.append(
            f"{collisions} case-insensitive name collision(s) -- Power BI Desktop will refuse "
            f"to load this project"
        )

    warnings = []
    if gaps:
        warnings.append(f"{gaps} field(s) declined translation -- see RESIDUE.md / Gaps sheet")

    return {
        'mapping_percentage': mapping_percentage,
        'mapped': mapped,
        'gaps': gaps,
        'unaccounted': unaccounted,
        'collisions': collisions,
        'issues': issues,
        'warnings': warnings,
    }


def _blend_key_stats_re(project_name: str) -> re.Pattern:
    return re.compile(
        r"\[ok\]\s+" + re.escape(project_name) +
        r"\s+checked=\s*(\d+)\s+zero_overlap=\s*(\d+)\s+unresolved=\s*(\d+)"
    )


def parse_blend_key_result(output: str, project_name: str) -> Dict[str, Any]:
    """
    Parse validate_blend_key_domains.py's stdout summary line for THIS project, e.g.:
      "[ok] <name>  checked=3 zero_overlap=1 unresolved=4  -> <path>"

    Anchored to `project_name` for the same reason `parse_mapping_result` is: the skill can be
    pointed at a parent directory of several projects and prints one line per project it finds.
    A `zero_overlap` Blend Key relationship means the many-to-many relationship built on it can
    never produce a matching row -- see build-tableau-pbip/scripts/tmdl.py's `_relationships()`
    docstring and validate-blend-key-domains/SKILL.md for the real corpus regression this catches.
    """
    match = _blend_key_stats_re(project_name).search(output or "")
    if not match:
        return {
            'checked': 0, 'zero_overlap': 0, 'unresolved': 0,
            'issues': [f"Could not find a Blend Key stats line for '{project_name}' in "
                       f"validator output"],
        }
    checked, zero_overlap, unresolved = (int(g) for g in match.groups())
    issues = []
    if zero_overlap:
        issues.append(
            f"{zero_overlap} of {checked} Blend Key relationship(s) have ZERO overlapping real "
            f"values -- any visual spanning those two tables will render empty in Power BI "
            f"Desktop regardless of how much data is loaded (see BLEND_KEY_DOMAIN_REPORT.md)")
    return {
        'checked': checked, 'zero_overlap': zero_overlap, 'unresolved': unresolved,
        'issues': issues,
    }


def parse_effort_result(output: str) -> Dict[str, Any]:
    """
    Parse effort_report.py's stdout summary line, e.g.:
      "[ok] <name>  already_resolved=20 recommended_prompt=37 raw_prompt=7
       manual_structural=4 visual_undecided=32 zone_gap=436 declined_encoding=6 est_hours=58.41
       -> <path>"

    Counters matched by NAME, not position -- the same convention this repo standardized on
    after Agent 5's own regression (see README_AGENTS.md: a positional regex silently matched
    nothing when the wrapped script grew new counters). New counters here are additive-safe.
    """
    line = next((l for l in (output or "").splitlines() if l.startswith("[ok]")), "")
    counters = dict(re.findall(r"(\w+)=([\d.]+)", line))
    if not counters:
        return {
            'already_resolved': 0, 'recommended_prompt': 0, 'raw_prompt': 0,
            'manual_structural': 0, 'visual_undecided': 0, 'zone_gap': 0,
            'declined_encoding': 0, 'blend_key_gap': 0, 'est_hours': 0.0,
            'issues': ["Could not parse effort-report stats from output"],
        }
    return {
        'already_resolved': int(counters.get('already_resolved', 0)),
        'recommended_prompt': int(counters.get('recommended_prompt', 0)),
        'raw_prompt': int(counters.get('raw_prompt', 0)),
        'manual_structural': int(counters.get('manual_structural', 0)),
        'visual_undecided': int(counters.get('visual_undecided', 0)),
        'zone_gap': int(counters.get('zone_gap', 0)),
        'declined_encoding': int(counters.get('declined_encoding', 0)),
        'blend_key_gap': int(counters.get('blend_key_gap', 0)),
        'est_hours': float(counters.get('est_hours', 0.0)),
        'issues': [],
    }


def find_pbip_projects(target: Path) -> List[Path]:
    """Discover PBIP projects (folders containing .Report and .SemanticModel)."""
    projects = []

    # Check if target itself is a PBIP -- ".Report"/".SemanticModel" folders are named after the
    # project (e.g. "Foo.Report"), never that literal name, so this needs a glob, not exists().
    if list(target.glob("*.Report")) and list(target.glob("*.SemanticModel")):
        return [target]

    # Search recursively for PBIP projects
    for item in target.rglob("*.Report"):
        pbip_dir = item.parent
        if list(pbip_dir.glob("*.SemanticModel")):
            if pbip_dir not in projects:
                projects.append(pbip_dir)

    return sorted(projects)


def has_data(pbip_path: Path) -> bool:
    """Check if PBIP has real/dummy data wired in.

    generate-dummy-data's own naming convention (`<project>_dummy_data.xlsx`) writes that file
    DIRECTLY inside the project folder itself, never into a `Data/` subfolder -- checking only
    `pbip_path / "Data"` (the original shape of this function) meant this always returned False
    for every project this pipeline has ever built, silently skipping Step 3 (data validation)
    and Step 4 (Blend Key domain check) even when real/dummy data was actually present. Falls
    back to a `Data/*.xlsx` check in case a native (non-stub) partition convention ever produces
    one there instead."""
    if list(pbip_path.glob("*_dummy_data.xlsx")):
        return True
    data_dir = pbip_path / "Data"
    if data_dir.exists():
        return bool(list(data_dir.glob("*.xlsx")))
    return False


def calculate_quality_score(validations: Dict[str, Any]) -> int:
    """
    Calculate quality score (0-100) based on all validations.
    
    Scoring:
    - Mapping: 0-100 (from validate-tableau-pbip-mapping)
    - Compliance: -1 per failure, -0.5 per note
    - Data: 0 if has data and validation failed, 0 if no data and expected
    """
    score = 100  # Start optimistic
    
    # Mapping validation penalty
    if validations.get('mapping'):
        mapping_pct = validations['mapping'].get('mapping_percentage', 0)
        score = mapping_pct  # Replace base score with mapping percentage
    
    # Compliance validation penalty
    if validations.get('compliance'):
        score -= validations['compliance'].get('failure_count', 0)
        score -= validations['compliance'].get('note_count', 0) * 0.5
    
    # Data validation penalty
    if validations.get('data'):
        if not validations['data'].get('success', False):
            score -= 10

    # Blend Key domain-overlap penalty -- same weight class as a compliance failure: a
    # zero-overlap relationship is a structural defect (an unusable relationship), not a data
    # quality nit. Only meaningful once real/dummy data exists (see validate_pbip's Step 4/5,
    # gated on `has_data`), so this is a no-op for a stub-only project.
    if validations.get('blend_key_domains'):
        score -= validations['blend_key_domains'].get('zero_overlap', 0) * 2

    return max(0, min(100, int(score)))


def determine_status(quality_score: int, compliance_failures: int) -> tuple[str, int]:
    """Determine validation status and exit code."""
    if quality_score >= 90 and compliance_failures == 0:
        return "APPROVED", 0
    elif quality_score >= 75:
        return "NEEDS_REVIEW", 1
    else:
        return "FAILED", 2


class ValidatorAgent:
    """PBIP Validator Agent - validates existing PBIP projects."""
    
    def __init__(self, verbose: bool = False, json_output: bool = False):
        self.verbose = verbose
        self.json_output = json_output
        self.results = []
    
    def log(self, message: str, level: str = "INFO"):
        """Log a message."""
        if not self.json_output:
            prefix = f"[{level}]" if level != "INFO" else ""
            print(f"{prefix} {message}" if prefix else message)
    
    def validate_pbip(self, pbip_path: Path) -> Dict[str, Any]:
        """Validate a single PBIP project."""
        pbip_path = pbip_path.resolve()
        project_name = pbip_path.name
        
        result = {
            'project': project_name,
            'path': str(pbip_path),
            'timestamp': datetime.now().isoformat(),
            'has_data': has_data(pbip_path),
            'validations': {}
        }
        
        self.log(f"\nValidating: {project_name}")
        self.log(f"Path: {pbip_path}")
        
        # STEP 1: Validate mapping (conversion quality)
        self.log("Step 1/5: Validating mapping quality...")
        validate_result = call_skill('validate-tableau-pbip-mapping', pbip_path)
        
        if not validate_result['success']:
            self.log(f"⚠ Mapping validation failed: {validate_result['error']}", "WARN")
            mapping = {'mapping_percentage': 0, 'issues': [validate_result['error']], 'warnings': []}
        else:
            mapping = parse_mapping_result(validate_result['output'], project_name)
        
        result['validations']['mapping'] = mapping
        self.log(f"✓ Mapping: {mapping.get('mapping_percentage', 0)}%")
        
        # STEP 2: Validate compliance
        self.log("Step 2/5: Checking PBIP compliance...")
        compliance_result = call_skill("validate-pbip-compliance", pbip_path)
        
        if not compliance_result['success']:
            self.log(f"⚠ Compliance check failed: {compliance_result['error']}", "WARN")
            compliance = {
                'failures': [compliance_result['error']],
                'notes': [],
                'failure_count': 1,
                'note_count': 0
            }
        else:
            try:
                compliance_json_path = pbip_path.parent / "validation" / "COMPLIANCE.json"
                if compliance_json_path.exists():
                    compliance = json.loads(compliance_json_path.read_text())
                else:
                    compliance = {'failures': [], 'notes': [], 'failure_count': 0, 'note_count': 0}
            except Exception as e:
                self.log(f"⚠ Could not parse compliance results: {str(e)}", "WARN")
                compliance = {'failures': [], 'notes': [], 'failure_count': 0, 'note_count': 0}
        
        result['validations']['compliance'] = compliance
        
        if compliance['failure_count'] > 0:
            self.log(f"⚠ Found {compliance['failure_count']} compliance issue(s)", "WARN")
        elif compliance['note_count'] > 0:
            self.log(f"✓ No compliance failures ({compliance['note_count']} note(s) -- see COMPLIANCE.json)")
        else:
            self.log(f"✓ No compliance issues")
        
        # STEP 3: Validate data (if present)
        if result['has_data']:
            self.log("Step 3/5: Validating data integrity...")
            data_result = call_skill('validate-dummy-data', pbip_path)

            data_validation = {
                'success': data_result['success'],
                'output': data_result['output'],
                'error': data_result['error']
            }
            result['validations']['data'] = data_validation

            if data_result['success']:
                self.log(f"✓ Data validation passed")
            else:
                self.log(f"⚠ Data validation failed: {data_result['error']}", "WARN")
        else:
            self.log("Step 3/5: No data to validate (stub project)")
            result['validations']['data'] = {'success': True, 'note': 'No data present'}

        # STEP 4: Blend Key domain-overlap check -- ONLY meaningful once real/dummy data exists
        # (same gate as Step 3): a many-to-many relationship keyed on a synthetic "Blend Key"
        # composite column (build-tableau-pbip's own translation of Tableau data blending, see
        # tmdl.py's `_relationships()`) can share a same-name/same-dtype/same-formula-shape
        # column on both sides (tmdl.py's own static check already covers that) and STILL have
        # zero real overlapping values -- only detectable from actual data. Detect-and-report
        # only: never rewrites relationships.tmdl.
        if result['has_data']:
            self.log("Step 4/5: Checking Blend Key relationship domains...")
            blend_key_result = call_skill('validate-blend-key-domains', pbip_path)
            if not blend_key_result['success']:
                self.log(f"⚠ Blend Key domain check failed: {blend_key_result['error']}", "WARN")
                blend_key_domains = {
                    'checked': 0, 'zero_overlap': 0, 'unresolved': 0,
                    'issues': [blend_key_result['error']],
                }
            else:
                blend_key_domains = parse_blend_key_result(blend_key_result['output'], project_name)
            result['validations']['blend_key_domains'] = blend_key_domains
            if blend_key_domains['zero_overlap']:
                self.log(f"⚠ {blend_key_domains['zero_overlap']} of "
                         f"{blend_key_domains['checked']} Blend Key relationship(s) have zero "
                         f"overlapping real values", "WARN")
            else:
                self.log(f"✓ Blend Key domains: {blend_key_domains['checked']} checked, "
                         f"0 zero-overlap")
        else:
            self.log("Step 4/5: No data to check Blend Key domains against (stub project)")
            result['validations']['blend_key_domains'] = {
                'checked': 0, 'zero_overlap': 0, 'unresolved': 0, 'issues': [],
            }

        # STEP 5: Effort/QA report -- how much manual work is left, split by how much of a
        # running start the Copilot prompt gives, plus what to spot-check before trusting this
        # model. Does not affect quality_score/status: this is a planning number, not a
        # correctness check.
        self.log("Step 5/5: Computing effort/QA report...")
        effort_result = call_skill('generate-effort-report', pbip_path)
        if not effort_result['success']:
            self.log(f"⚠ Effort report failed: {effort_result['error']}", "WARN")
            effort = {
                'already_resolved': 0, 'recommended_prompt': 0, 'raw_prompt': 0,
                'manual_structural': 0, 'visual_undecided': 0, 'zone_gap': 0,
                'declined_encoding': 0, 'blend_key_gap': 0, 'est_hours': 0.0,
                'issues': [effort_result['error']],
            }
        else:
            effort = parse_effort_result(effort_result['output'])
        result['validations']['effort'] = effort
        if effort['issues']:
            self.log(f"⚠ {effort['issues'][0]}", "WARN")
        else:
            self.log(f"✓ Effort: {effort['already_resolved']} already resolved, "
                     f"{effort['recommended_prompt']} recommended prompt, "
                     f"{effort['raw_prompt'] + effort['manual_structural']} manual, "
                     f"~{effort['est_hours']}h remaining")

        # STEP 5: Calculate quality score and status
        quality_score = calculate_quality_score(result['validations'])
        status, exit_code = determine_status(quality_score, compliance['failure_count'])
        
        result['quality_score'] = quality_score
        result['status'] = status
        result['exit_code'] = exit_code
        
        self.log(f"Quality Score: {quality_score}/100 → {status} (exit code: {exit_code})")
        
        return result
    
    def write_effort_rollup(self, root: Path):
        """Combine every project's own effort_report.json (written by Step 4 above) into ONE
        EFFORT_SUMMARY.md at the batch root -- the actual management deliverable when validating
        many workbooks together: total remaining hours, and the top recurring reasons worth
        fixing once rather than per-workbook. Skipped when only one project was validated (its
        own EFFORT_REPORT.md already says everything there is to say)."""
        repo_root = Path(__file__).parent.parent.parent.parent.parent
        effort_report_scripts = (repo_root / ".claude" / "skills" / "generate-copilot-fix-prompts"
                                  / "scripts")
        sys.path.insert(0, str(effort_report_scripts))
        import effort_report  # noqa: E402

        reports = []
        for result in self.results:
            project_path = Path(result['path'])
            json_path = project_path.parent / "validation" / project_path.name / "effort_report.json"
            if not json_path.exists():
                continue
            try:
                reports.append(json.loads(json_path.read_text()))
            except json.JSONDecodeError:
                continue
        if not reports:
            return

        out_dir = root / "validation"
        out_dir.mkdir(parents=True, exist_ok=True)
        summary_path = out_dir / "EFFORT_SUMMARY.md"
        summary_path.write_text(effort_report.render_summary_md(reports))
        (out_dir / "effort_summary.json").write_text(json.dumps(reports, indent=2, default=list))
        self.log(f"✓ Saved batch effort summary: {summary_path}")

    def persist_results(self, pbip_path: Path, result: Dict[str, Any]):
        """Persist validation results to JSON files."""
        results_dir = pbip_path.parent / "validation"
        results_dir.mkdir(parents=True, exist_ok=True)
        
        # Save detailed validation results
        validation_file = results_dir / "VALIDATION_REPORT.json"
        validation_file.write_text(json.dumps(result, indent=2))
        self.log(f"✓ Saved validation report: {validation_file}")
        
        # Save summary
        summary_file = results_dir / "VALIDATION_SUMMARY.json"
        summary = {
            'project': result['project'],
            'timestamp': result['timestamp'],
            'quality_score': result['quality_score'],
            'status': result['status'],
            'exit_code': result['exit_code'],
            'has_data': result['has_data'],
            'compliance_issues': result['validations']['compliance']['failure_count'],
            'mapping_quality': result['validations']['mapping'].get('mapping_percentage', 0),
            'blend_key_zero_overlap': result['validations'].get(
                'blend_key_domains', {}).get('zero_overlap', 0),
            'effort': result['validations'].get('effort', {}),
        }
        summary_file.write_text(json.dumps(summary, indent=2))
        self.log(f"✓ Saved validation summary: {summary_file}")
    
    def validate(self, pbip_path: Path) -> int:
        """Main validation entry point."""
        pbip_path = pbip_path.resolve()
        
        if not pbip_path.exists():
            self.log(f"Error: Path does not exist: {pbip_path}", "ERROR")
            return 2
        
        # Find all PBIP projects
        projects = find_pbip_projects(pbip_path)
        if not projects:
            self.log(f"Error: No PBIP projects found under {pbip_path}", "ERROR")
            return 2
        
        self.log(f"Found {len(projects)} PBIP project(s)")
        
        # Validate each project
        exit_code = 0
        for project in projects:
            result = self.validate_pbip(project)
            self.results.append(result)
            self.persist_results(project, result)
            
            # Track worst exit code
            if result['exit_code'] > exit_code:
                exit_code = result['exit_code']
        
        # Print summary
        if self.results:
            self.log("\n" + "=" * 60)
            self.log("VALIDATION SUMMARY")
            self.log("=" * 60)
            
            for result in self.results:
                self.log(f"{result['project']:40} {result['status']:15} ({result['quality_score']}/100)")
            
            approved = sum(1 for r in self.results if r['exit_code'] == 0)
            self.log(f"\nTotal: {len(self.results)} projects | {approved} approved")

            if len(self.results) > 1:
                self.write_effort_rollup(pbip_path)

        return exit_code


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description='Validate existing Power BI projects (.pbip)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Validate single PBIP project
  %(prog)s /path/to/project.pbip
  
  # Validate directory containing multiple PBIP projects
  %(prog)s /path/to/projects/
  
  # Verbose output with JSON
  %(prog)s /path/to/projects/ -v --json
        '''
    )
    
    parser.add_argument('input', type=Path, help='Path to PBIP project or directory of projects')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('--json', action='store_true', dest='json_output', help='JSON output format')
    
    args = parser.parse_args()
    
    agent = ValidatorAgent(verbose=args.verbose, json_output=args.json_output)
    exit_code = agent.validate(args.input)
    
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
