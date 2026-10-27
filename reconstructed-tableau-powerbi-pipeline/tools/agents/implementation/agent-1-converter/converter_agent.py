#!/usr/bin/env python3
"""
AGENT 1: Tableau-to-PowerBI Converter (Skills-First)
Orchestrates skills to convert Tableau workbooks (.twbx/.twb) to Power BI projects (.pbip)

Architecture:
  1. Extract TWBX using extract-twbx-contents skill
  2. Build PBIP using build-tableau-pbip skill
  3. Validate conversion using validate-tableau-pbip-mapping skill
  4. Check compliance using pbip_lint
  5. Persist validation/compliance results
  6. Return status and exit code
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


def call_skill(skill_name: str, *args, **kwargs) -> Dict[str, Any]:
    """
    Execute a skill and return the result.
    
    Skills are Python scripts in /.claude/skills/ directory.
    Handles both CLI-based and direct function calls.
    Returns: {'success': bool, 'output': Path | str, 'error': str | None}
    """
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    skills_dir = repo_root / ".claude" / "skills"
    
    if skill_name == "extract-twbx-contents":
        # Extract TWBX file to temporary directory - use direct function call
        twbx_file = Path(args[0])
        output_dir = Path(args[1]) if len(args) > 1 else Path(tempfile.gettempdir()) / twbx_file.stem
        
        try:
            import zipfile
            twbx_file = Path(twbx_file).resolve()
            output_dir = Path(output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Extract the TWBX (which is a ZIP file)
            workbook_name = twbx_file.stem
            if workbook_name.endswith('.twbx'):
                workbook_name = workbook_name[:-5]
            
            extract_dir = output_dir / workbook_name
            extract_dir.mkdir(parents=True, exist_ok=True)
            
            with zipfile.ZipFile(twbx_file, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            
            return {
                'success': True,
                'output': str(extract_dir),
                'error': None
            }
        except Exception as e:
            return {'success': False, 'output': None, 'error': str(e)}
        
    elif skill_name == "build-tableau-pbip":
        # Build .pbip from extracted Tableau structure - use CLI
        input_path = Path(args[0])
        output_dir = Path(args[1])
        tds_dir = kwargs.get('tds_dir')
        
        script = skills_dir / "build-tableau-pbip" / "scripts" / "build_pbip.py"
        cmd = [sys.executable, str(script), str(input_path), "--out", str(output_dir)]
        
        if tds_dir:
            cmd.extend(["--tds", str(tds_dir)])
        
    elif skill_name == "validate-tableau-pbip-mapping":
        # Validate conversion quality - use CLI
        # validate_mapping.py expects the *parent* of a build-tableau-pbip workbook folder --
        # it lists that folder's children and looks for one with a _work/ir.json inside. Passing
        # pbip_path itself (the workbook folder) makes it iterate pbip_path's own contents
        # (.Report/, .SemanticModel/, _work/, ...) and find no _work one level further down,
        # silently producing an empty rollup.
        pbip_path = Path(args[0])
        build_out = pbip_path.parent
        validation_dir = pbip_path.parent / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        output_report = validation_dir / "MAPPING_VALIDATION"

        script = skills_dir / "validate-tableau-pbip-mapping" / "scripts" / "validate_mapping.py"
        cmd = [sys.executable, str(script), str(build_out), "--out", str(output_report)]

    elif skill_name == "validate-pbip-compliance":
        # Validate PBIP compliance using pbip_lint - use CLI
        pbip_path = Path(args[0])
        validation_dir = pbip_path.parent / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        output_report = validation_dir / "COMPLIANCE.json"

        script = skills_dir / "validate-pbip-compliance" / "scripts" / "validate_pbip_compliance.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--json", str(output_report)]
        
    else:
        return {'success': False, 'output': None, 'error': f'Unknown skill: {skill_name}'}
    
    # Execute CLI-based skills
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(skills_dir)  # Run from skills directory so relative imports work
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


_MAPPING_STATS_RE = re.compile(
    r"mapped=\s*(\d+)\s+gaps=\s*(\d+)\s+unaccounted=(\d+)\s+collisions=(\d+)"
)


def parse_validation_result(output: str) -> Dict[str, Any]:
    """
    Parse validation result from validate_mapping.py's stdout, e.g.:
      "[ok] <name>  mapped=  55 gaps=  10 unaccounted=0 collisions=0  -> <path>"

    The skill has no JSON output mode -- it writes an .xlsx report per workbook and a
    Markdown rollup, and prints this one summary line per workbook to stdout. mapped/gaps
    reflect the same D/D+ tier coverage the build's own COVERAGE.md reports, so
    mapped / (mapped + gaps) is a faithful mapping_percentage.
    """
    match = _MAPPING_STATS_RE.search(output or "")
    if not match:
        return {
            'mapping_percentage': 0,
            'issues': ["Could not parse mapping stats from validator output"],
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


def calculate_quality_score(validation: Dict, compliance: Dict) -> int:
    """
    Calculate quality score (0-100) based on validation and compliance.
    
    Scoring:
    - Base: mapping_percentage (0-100)
    - Penalties: -1 per compliance failure, -0.5 per compliance note
    """
    score = validation.get('mapping_percentage', 0)
    
    # Apply compliance penalties
    score -= compliance.get('failure_count', 0)
    score -= compliance.get('note_count', 0) * 0.5
    
    # Clamp to 0-100
    return max(0, min(100, int(score)))


def determine_status(quality_score: int, compliance_failures: int) -> tuple[str, int]:
    """
    Determine conversion status and exit code.
    
    Returns: (status: str, exit_code: int)
    - APPROVED (0): quality >= 90 and no compliance failures
    - NEEDS_REVIEW (1): quality >= 75 or some compliance failures
    - FAILED (2): quality < 75 or critical compliance failures
    """
    if compliance_failures > 0:
        if quality_score >= 75:
            return ("NEEDS_REVIEW", 1)
        else:
            return ("FAILED", 2)
    
    if quality_score >= 90:
        return ("APPROVED", 0)
    elif quality_score >= 75:
        return ("NEEDS_REVIEW", 1)
    else:
        return ("FAILED", 2)


class ConverterAgent:
    """Orchestrates Tableau to Power BI conversion using skills."""
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.repo_root = Path(__file__).parent.parent.parent.parent
        self.results_dir = None
        
    def log(self, message: str, level: str = "INFO"):
        """Log message with level."""
        if self.verbose or level in ("ERROR", "WARN"):
            print(f"[{level}] {message}")
    
    def persist_results(self, pbip_path: Path, validation: Dict, compliance: Dict, quality_score: int, status: str):
        """
        Persist validation and compliance results to JSON files.
        Creates:
        - VALIDATION.json: conversion quality metrics
        - COMPLIANCE.json: PBIP compliance check results
        - REPORT.json: summary report
        """
        results_dir = pbip_path.parent / "validation"
        results_dir.mkdir(parents=True, exist_ok=True)

        # Save validation results
        validation_file = results_dir / "VALIDATION.json"
        validation_to_save = {
            'timestamp': datetime.now().isoformat(),
            'mapping_percentage': validation.get('mapping_percentage', 0),
            'issues': validation.get('issues', []),
            'warnings': validation.get('warnings', [])
        }
        validation_file.write_text(json.dumps(validation_to_save, indent=2))
        self.log(f"✓ Saved validation results: {validation_file}")
        
        # Save compliance results
        compliance_file = results_dir / "COMPLIANCE.json"
        compliance_to_save = {
            'timestamp': datetime.now().isoformat(),
            'failures': compliance.get('failures', []),
            'notes': compliance.get('notes', []),
            'failure_count': compliance.get('failure_count', 0),
            'note_count': compliance.get('note_count', 0)
        }
        compliance_file.write_text(json.dumps(compliance_to_save, indent=2))
        self.log(f"✓ Saved compliance results: {compliance_file}")
        
        # Save summary report
        report_file = results_dir / "REPORT.json"
        report = {
            'timestamp': datetime.now().isoformat(),
            'pbip_path': str(pbip_path),
            'status': status,
            'quality_score': quality_score,
            'validation': validation_to_save,
            'compliance': {
                'failures': len(compliance.get('failures', [])),
                'notes': len(compliance.get('notes', [])),
                'details': compliance_to_save
            }
        }
        report_file.write_text(json.dumps(report, indent=2))
        self.log(f"✓ Saved summary report: {report_file}")
    
    def convert_workbook(
        self,
        input_file: Path,
        output_dir: Path,
        project_name: Optional[str] = None,
        tds_dir: Optional[Path] = None,
    ) -> Dict:
        """
        Convert a single Tableau workbook to Power BI project using skills.
        
        Flow:
        1. Extract TWBX using extract-twbx-contents skill
        2. Build PBIP using build-tableau-pbip skill
        3. Validate conversion using validate-tableau-pbip-mapping skill
        4. Check compliance using pbip_lint
        5. Persist results
        6. Return status with exit code
        
        Returns:
            {
                'success': bool,
                'pbip_path': Path | None,
                'status': str,  # APPROVED, NEEDS_REVIEW, FAILED
                'quality_score': int,  # 0-100
                'exit_code': int,  # 0, 1, 2
                'error': str | None,
                'validation': dict,
                'compliance': dict
            }
        """
        result = {
            'success': False,
            'pbip_path': None,
            'status': 'FAILED',
            'quality_score': 0,
            'exit_code': 2,
            'error': None,
            'validation': {},
            'compliance': {}
        }
        
        try:
            input_file = Path(input_file).resolve()
            output_dir = Path(output_dir).resolve()
            
            if not input_file.exists():
                result['error'] = f"Input file not found: {input_file}"
                self.log(result['error'], "ERROR")
                return result
            
            if input_file.suffix.lower() not in ('.twb', '.twbx'):
                result['error'] = f"Invalid file type: {input_file.suffix}. Expected .twb or .twbx"
                self.log(result['error'], "ERROR")
                return result
            
            output_dir.mkdir(parents=True, exist_ok=True)
            project_name = project_name or input_file.stem
            
            # ================================================================
            # STEP 1: Build PBIP using skill (includes extraction & conversion)
            # ================================================================
            self.log(f"Step 1/3: Converting Tableau to Power BI (.pbip)...")
            pbip_output_dir = output_dir / project_name
            pbip_output_dir.mkdir(parents=True, exist_ok=True)
            
            # Call build-tableau-pbip skill - pass the .twbx file directly
            # (it does its own extraction and creates a nested structure)
            build_result = call_skill('build-tableau-pbip', input_file, pbip_output_dir, tds_dir=tds_dir)
            if not build_result['success']:
                result['error'] = f"Build failed: {build_result['error']}"
                self.log(result['error'], "ERROR")
                return result
            
            # build_pbip.py creates a nested structure: output_dir/project_name/project_name/
            # Find the actual .pbip directory
            pbip_path = None
            
            # First check if .Report and .SemanticModel are in the direct output dir
            pbip_dirs = list(pbip_output_dir.glob("*.Report"))
            if pbip_dirs:
                pbip_path = pbip_output_dir
            else:
                # Check for the nested project dir by content, not position: re-running this
                # workbook into an output dir that already has a `validation/` folder from a
                # prior run gives pbip_output_dir a SECOND sibling entry, and the old
                # `len(subdirs) == 1` check then always found nothing -- a rebuild indistinguishable
                # from a real build failure.
                for candidate in sorted(pbip_output_dir.iterdir()):
                    if candidate.is_dir() and (
                            list(candidate.glob("*.Report")) or list(candidate.glob("*.SemanticModel"))):
                        pbip_path = candidate
                        break
            
            if not pbip_path:
                result['error'] = "No Power BI project generated (.Report or .SemanticModel not found)"
                self.log(result['error'], "ERROR")
                return result
            
            self.log(f"✓ Built Power BI project: {pbip_path}")
            result['pbip_path'] = pbip_path
            
            # ================================================================
            # STEP 2: Validate conversion using skill
            # ================================================================
            self.log(f"Step 2/3: Validating conversion quality...")
            
            validate_result = call_skill('validate-tableau-pbip-mapping', pbip_path)
            if not validate_result['success']:
                # Validation warnings don't fail the whole process
                self.log(f"⚠ Validation warnings: {validate_result['error']}", "WARN")
                validation = {'mapping_percentage': 0, 'issues': [validate_result['error']], 'warnings': []}
            else:
                validation = parse_validation_result(validate_result['output'])
            
            result['validation'] = validation
            self.log(f"✓ Mapping quality: {validation.get('mapping_percentage', 0)}%")
            
            # ================================================================
            # STEP 3: Check PBIP compliance using validate-pbip-compliance skill
            # ================================================================
            self.log(f"Step 3/3: Checking PBIP compliance...")
            
            compliance_result = call_skill("validate-pbip-compliance", pbip_path)
            if not compliance_result['success']:
                self.log(f"⚠ Compliance check failed: {compliance_result['error']}", "WARN")
                compliance_dict = {
                    'failures': [compliance_result['error']],
                    'notes': [],
                    'failure_count': 1,
                    'note_count': 0
                }
            else:
                # Read the JSON output file
                compliance_json_path = pbip_path.parent / "validation" / "COMPLIANCE.json"
                if compliance_json_path.exists():
                    compliance_json = json.loads(compliance_json_path.read_text())
                    compliance_dict = {
                        'failures': compliance_json.get('failures', []),
                        'notes': compliance_json.get('notes', []),
                        'failure_count': compliance_json.get('failure_count', 0),
                        'note_count': compliance_json.get('note_count', 0)
                    }
                else:
                    self.log(f"⚠ Compliance report file not found: {compliance_json_path}", "WARN")
                    compliance_dict = {
                        'failures': [],
                        'notes': [],
                        'failure_count': 0,
                        'note_count': 0
                    }
            
            result['compliance'] = compliance_dict
            
            if compliance_dict['failure_count'] > 0:
                self.log(f"⚠ Found {compliance_dict['failure_count']} compliance issue(s)", "WARN")
                for failure in compliance_dict['failures'][:3]:  # Show first 3
                    self.log(f"  - {failure}", "WARN")
                if compliance_dict['failure_count'] > 3:
                    self.log(f"  ... and {compliance_dict['failure_count'] - 3} more", "WARN")
            else:
                self.log(f"✓ No compliance issues found")
            
            # ================================================================
            # STEP 4: Calculate quality score and status
            # ================================================================
            quality_score = calculate_quality_score(validation, compliance_dict)
            status, exit_code = determine_status(quality_score, compliance_dict['failure_count'])
            
            result['success'] = (exit_code == 0)
            result['status'] = status
            result['quality_score'] = quality_score
            result['exit_code'] = exit_code
            
            # ================================================================
            # STEP 5: Persist results
            # ================================================================
            self.persist_results(pbip_path, validation, compliance_dict, quality_score, status)

            # ================================================================
            # Summary
            # ================================================================
            status_symbol = {
                'APPROVED': '✓',
                'NEEDS_REVIEW': '⚠',
                'FAILED': '✗'
            }.get(status, '?')

            self.log(f"\n{status_symbol} Conversion {status}")
            self.log(f"  Quality Score: {quality_score}/100")
            self.log(f"  Status: {status}")
            self.log(f"  Compliance: {compliance_dict['failure_count']} failures, {compliance_dict['note_count']} notes")
            
            return result
            
        except Exception as e:
            result['error'] = f"{type(e).__name__}: {str(e)}"
            if self.verbose:
                traceback.print_exc()
            self.log(result['error'], "ERROR")
            return result
    
    
    def batch_convert(
        self,
        input_dir: Path,
        output_dir: Path,
        tds_dir: Optional[Path] = None,
    ) -> Dict:
        """
        Batch convert all .twbx files in a directory.
        
        Returns:
            {
                'total': int,
                'approved': int,
                'needs_review': int,
                'failed': int,
                'results': {
                    'filename': {...result...}
                }
            }
        """
        input_dir = Path(input_dir).resolve()
        
        if not input_dir.exists():
            self.log(f"Input directory not found: {input_dir}", "ERROR")
            return {'total': 0, 'approved': 0, 'needs_review': 0, 'failed': 0, 'results': {}}
        
        workbooks = sorted(input_dir.rglob("*.twbx"))
        self.log(f"Found {len(workbooks)} workbook(s) to convert")
        
        results = {}
        approved = 0
        needs_review = 0
        failed = 0
        
        for i, workbook in enumerate(workbooks, 1):
            self.log(f"\n[{i}/{len(workbooks)}] Converting: {workbook.name}")
            
            # convert_workbook already nests its output under output_dir/project_name --
            # passing output_dir/workbook.stem here would duplicate that folder name twice.
            result = self.convert_workbook(
                workbook,
                output_dir,
                project_name=workbook.stem,
                tds_dir=tds_dir
            )
            
            results[workbook.name] = result
            
            status = result.get('status', 'FAILED')
            if status == 'APPROVED':
                approved += 1
            elif status == 'NEEDS_REVIEW':
                needs_review += 1
            else:
                failed += 1
        
        return {
            'total': len(workbooks),
            'approved': approved,
            'needs_review': needs_review,
            'failed': failed,
            'results': results
        }



def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AGENT 1: Tableau-to-PowerBI Converter (Skills-First Architecture)"
    )
    parser.add_argument(
        'input',
        type=Path,
        help='Input .twbx/.twb file or directory of files'
    )
    parser.add_argument(
        '-o', '--output',
        type=Path,
        required=True,
        help='Output directory for .pbip projects'
    )
    parser.add_argument(
        '-n', '--name',
        help='Project name (auto-derived if not specified)'
    )
    parser.add_argument(
        '--tds',
        type=Path,
        help='Path to .tds (Tableau Data Source) files'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--batch',
        action='store_true',
        help='Batch mode: convert all .twbx files in directory'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON'
    )
    
    args = parser.parse_args()
    
    agent = ConverterAgent(verbose=args.verbose)
    
    if args.batch or args.input.is_dir():
        result = agent.batch_convert(args.input, args.output, args.tds)
        
        # Print summary
        print(f"\n{'='*70}")
        print(f"BATCH CONVERSION SUMMARY")
        print(f"{'='*70}")
        print(f"Total:        {result['total']}")
        print(f"Approved:     {result['approved']}")
        print(f"Needs Review: {result['needs_review']}")
        print(f"Failed:       {result['failed']}")
        print(f"{'='*70}")
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        
        # Determine exit code based on any failures
        if result['failed'] > 0:
            sys.exit(1)
        elif result['needs_review'] > 0:
            sys.exit(1)
        else:
            sys.exit(0)
    else:
        result = agent.convert_workbook(
            args.input,
            args.output,
            project_name=args.name,
            tds_dir=args.tds
        )
        
        # Print summary
        print(f"\n{'='*70}")
        print(f"CONVERSION RESULT: {result['status']}")
        print(f"{'='*70}")
        print(f"Input:           {args.input}")
        print(f"Output:          {result['pbip_path']}")
        print(f"Quality Score:   {result['quality_score']}/100")
        print(f"Status:          {result['status']}")
        print(f"Compliance:      {result['compliance'].get('failure_count', 0)} failures")
        print(f"{'='*70}")
        
        if args.json:
            # Make result JSON-serializable
            result_json = {
                'status': result['status'],
                'pbip_path': str(result['pbip_path']) if result['pbip_path'] else None,
                'quality_score': result['quality_score'],
                'exit_code': result['exit_code'],
                'error': result['error'],
                'validation': result['validation'],
                'compliance': result['compliance']
            }
            print(json.dumps(result_json, indent=2))
        
        if result['error']:
            print(f"Error: {result['error']}")
        
        sys.exit(result['exit_code'])


if __name__ == '__main__':
    main()
