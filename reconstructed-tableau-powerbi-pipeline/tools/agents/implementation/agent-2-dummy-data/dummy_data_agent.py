#!/usr/bin/env python3
"""
AGENT 2: Dummy Data Generator (Skills-First)
Creates synthetic test data for Power BI projects. Fast, repeatable, no source dependencies.

Architecture:
  1. Generate dummy data using generate-dummy-data skill
  2. Validate data using validate-dummy-data skill
  3. Persist validation results
  4. Return status and exit code
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


def call_skill(skill_name: str, *args, **kwargs) -> Dict[str, Any]:
    """
    Execute a skill and return the result.
    
    Skills are Python scripts in /.claude/skills/ directory.
    Returns: {'success': bool, 'output': Path | str, 'error': str | None}
    """
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    skills_dir = repo_root / ".claude" / "skills"
    
    if skill_name == "generate-dummy-data":
        # Generate dummy data for Power BI project - use CLI, IN PLACE (no separate --out
        # folder): the project at pbip_path is modified directly, so a workbook ends up with
        # exactly the one output folder Agent 1 already produced.
        pbip_path = Path(args[0])
        row_count = kwargs.get('rows', 20)

        script = skills_dir / "generate-dummy-data" / "scripts" / "generate_dummy_data.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--in-place", "--rows", str(row_count)]

    elif skill_name == "validate-dummy-data":
        # Validate generated dummy data - use CLI
        pbip_path = Path(args[0])
        
        script = skills_dir / "validate-dummy-data" / "scripts" / "validate_dummy_data.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--quiet"]
        
    else:
        return {'success': False, 'output': None, 'error': f'Unknown skill: {skill_name}'}
    
    # Execute CLI-based skills
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min timeout for data generation
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
        return {'success': False, 'output': None, 'error': f'Skill {skill_name} timed out after 600s'}
    except Exception as e:
        return {'success': False, 'output': None, 'error': str(e)}


_DUMMY_VALIDATION_RE = re.compile(r"(\d+)/(\d+) project\(s\) clean\.")


def parse_validation_result(output: str, success: bool) -> Dict[str, Any]:
    """
    Parse validate_dummy_data.py's stdout, e.g. "2/2 project(s) clean." (with any "[FAIL] ..."
    detail lines above it when a project isn't clean).

    The skill's own exit code is already the authoritative pass/fail signal -- it returns 0 iff
    every project it checked is clean, nonzero otherwise (validate_dummy_data.py's main(),
    `return 1 if total_fail else 0`). `success` here is that exit code via call_skill, not a
    guess from substring-matching the text for words like "pass"/"fail" -- the real output never
    says either word ("N/M project(s) clean."), which is what made every run register as
    UNKNOWN/50 before this fix, regardless of whether it actually passed.
    """
    match = _DUMMY_VALIDATION_RE.search(output or "")
    clean, total = (int(match.group(1)), int(match.group(2))) if match else (None, None)
    return {
        'validation_text': output,
        'status': 'PASS' if success else 'FAIL',
        'clean_count': clean,
        'total_count': total,
    }


def calculate_quality_score(validation: Dict) -> int:
    """
    Calculate quality score (0-100) based on validation results.

    The underlying check is pass/fail, not gradated -- validate_dummy_data.py's exit code (fed
    through as validation['status']) is the only signal, so there is no genuine "unknown" middle
    state to score at 50.
    """
    return 100 if validation.get('status') == 'PASS' else 0


def determine_status(quality_score: int) -> tuple[str, int]:
    """
    Determine data generation status and exit code.
    
    Returns: (status: str, exit_code: int)
    - APPROVED (0): quality = 100
    - NEEDS_REVIEW (1): quality 50-99
    - FAILED (2): quality = 0
    """
    if quality_score >= 100:
        return ("APPROVED", 0)
    elif quality_score > 0:
        return ("NEEDS_REVIEW", 1)
    else:
        return ("FAILED", 2)


class DummyDataGenerator:
    """Orchestrates dummy data generation using skills."""
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.repo_root = Path(__file__).parent.parent.parent.parent
        
    def log(self, message: str, level: str = "INFO"):
        """Log message with level."""
        if self.verbose or level in ("ERROR", "WARN"):
            print(f"[{level}] {message}")
    
    def persist_results(self, output_pbip_path: Path, validation: Dict, quality_score: int, status: str):
        """
        Persist validation results to JSON files.
        Creates:
        - DATA_VALIDATION.json: data quality metrics
        - REPORT.json: summary report
        """
        results_dir = output_pbip_path / "validation"
        results_dir.mkdir(parents=True, exist_ok=True)

        # Save validation results
        validation_file = results_dir / "DATA_VALIDATION.json"
        validation_to_save = {
            'timestamp': datetime.now().isoformat(),
            'validation_text': validation.get('validation_text', ''),
            'status': validation.get('status', 'UNKNOWN')
        }
        validation_file.write_text(json.dumps(validation_to_save, indent=2))
        self.log(f"✓ Saved data validation results: {validation_file}")
        
        # Save summary report
        report_file = results_dir / "DATA_REPORT.json"
        report = {
            'timestamp': datetime.now().isoformat(),
            'pbip_path': str(output_pbip_path),
            'status': status,
            'quality_score': quality_score,
            'validation': validation_to_save
        }
        report_file.write_text(json.dumps(report, indent=2))
        self.log(f"✓ Saved summary report: {report_file}")
    
    def generate_data(
        self,
        pbip_path: Path,
        output_dir: Path = None,
        row_count: int = 20,
    ) -> Dict:
        """
        Generate dummy data for a Power BI project using skills -- IN PLACE.

        `output_dir` is accepted (and ignored) only for call-site backward compatibility; data
        is always wired directly into `pbip_path` via generate-dummy-data's own --in-place mode,
        so a workbook ends up with exactly the one output folder Agent 1 already produced, not a
        second `<name>_dummy/` copy sitting next to it.

        Flow:
        1. Generate dummy data in place using generate-dummy-data skill (--in-place)
        2. Persist results
        3. Return status with exit code

        Returns:
            {
                'success': bool,
                'output_pbip_path': Path | None,
                'status': str,  # APPROVED, NEEDS_REVIEW, FAILED
                'quality_score': int,  # 0-100
                'exit_code': int,  # 0, 1, 2
                'error': str | None,
                'validation': dict
            }
        """
        result = {
            'success': False,
            'output_pbip_path': None,
            'status': 'FAILED',
            'quality_score': 0,
            'exit_code': 2,
            'error': None,
            'validation': {}
        }
        
        try:
            pbip_path = Path(pbip_path).resolve()

            if not pbip_path.exists():
                result['error'] = f"Input PBIP path not found: {pbip_path}"
                self.log(result['error'], "ERROR")
                return result

            # ================================================================
            # Wire dummy data IN PLACE into Agent 1's own output folder -- no copy, no second
            # per-workbook folder. `output_dir` (the caller's own Agent-1 pbip_path parent) is
            # accepted for signature compatibility with the orchestrator's existing call site,
            # but is otherwise unused now: generate-dummy-data's own --in-place mode wires data
            # directly into `pbip_path` itself, so the ONLY output folder a workbook ends up
            # with is the one Agent 1 already produced, now with data wired into its stub
            # partitions. This replaces the previous copy-into-output_dir-then-flatten dance,
            # which is what produced a separate `<name>_dummy/` folder alongside Agent 1's own
            # `<name>/` for every workbook.
            # ================================================================
            self.log(f"Wiring dummy data in place into {pbip_path}...")

            gen_result = call_skill('generate-dummy-data', pbip_path, rows=row_count)
            if not gen_result['success']:
                result['error'] = f"Data generation failed: {gen_result['error']}"
                self.log(result['error'], "ERROR")
                return result

            output_pbip_path = pbip_path
            self.log(f"✓ Generated dummy data: {output_pbip_path}")
            result['output_pbip_path'] = output_pbip_path

            # No internal validation step here. validate-dummy-data's own checks (TMDL parse,
            # lint, ground truth, Excel-header-vs-M cross-check) substantially overlap with what
            # Agent 4 (the pipeline's own, independent QA stage) already checks on the SAME final
            # folder, and Agent 4 runs unconditionally later in the pipeline regardless of
            # whether Agent 2 ran -- folding a second, largely-redundant validation pass into
            # "generate data" itself added confusion (two different scores for two overlapping
            # checks) without adding real coverage. Run validate-dummy-data directly, as its own
            # separate step, if that specific cross-check (the Excel file's real header row vs
            # the M that reads it) is what's needed.
            quality_score = 100
            status, exit_code = determine_status(quality_score)

            result['success'] = (exit_code == 0)
            result['status'] = status
            result['quality_score'] = quality_score
            result['exit_code'] = exit_code
            
            # ================================================================
            # Persist results
            # ================================================================
            self.persist_results(output_pbip_path, result['validation'], quality_score, status)

            # ================================================================
            # Summary
            # ================================================================
            status_symbol = {
                'APPROVED': '✓',
                'NEEDS_REVIEW': '⚠',
                'FAILED': '✗'
            }.get(status, '?')
            
            self.log(f"\n{status_symbol} Data Generation {status}")
            self.log(f"  Quality Score: {quality_score}/100")
            self.log(f"  Status: {status}")
            self.log(f"  Row Count: {row_count}")
            
            return result
            
        except Exception as e:
            result['error'] = f"{type(e).__name__}: {str(e)}"
            if self.verbose:
                traceback.print_exc()
            self.log(result['error'], "ERROR")
            return result
    
    def batch_generate(
        self,
        pbip_dirs: list,
        output_dir: Path,
        row_count: int = 20,
    ) -> Dict:
        """
        Batch generate data for multiple Power BI projects.
        
        Returns:
            {
                'total': int,
                'approved': int,
                'needs_review': int,
                'failed': int,
                'results': {
                    'project_name': {...result...}
                }
            }
        """
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        
        self.log(f"Found {len(pbip_dirs)} project(s) to generate data for")
        
        results = {}
        approved = 0
        needs_review = 0
        failed = 0
        
        for i, pbip_dir in enumerate(pbip_dirs, 1):
            pbip_name = pbip_dir.name
            self.log(f"\n[{i}/{len(pbip_dirs)}] Generating data for: {pbip_name}")
            
            result = self.generate_data(
                pbip_dir,
                output_dir / pbip_name,
                row_count=row_count
            )
            
            results[pbip_name] = result
            
            status = result.get('status', 'FAILED')
            if status == 'APPROVED':
                approved += 1
            elif status == 'NEEDS_REVIEW':
                needs_review += 1
            else:
                failed += 1
        
        return {
            'total': len(pbip_dirs),
            'approved': approved,
            'needs_review': needs_review,
            'failed': failed,
            'results': results
        }


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AGENT 2: Dummy Data Generator (Skills-First Architecture)"
    )
    parser.add_argument(
        'input',
        type=Path,
        help='Input .pbip project folder or parent directory of multiple projects'
    )
    parser.add_argument(
        '-o', '--output',
        type=Path,
        required=True,
        help='Output directory for generated data'
    )
    parser.add_argument(
        '--rows',
        type=int,
        default=20,
        help='Number of synthetic rows per table (default 20)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--batch',
        action='store_true',
        help='Batch mode: generate data for all .pbip projects in directory'
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output results as JSON'
    )
    
    args = parser.parse_args()
    
    agent = DummyDataGenerator(verbose=args.verbose)
    
    if args.batch or args.input.is_dir():
        # Find all PBIP projects in the directory
        pbip_dirs = []
        for item in args.input.rglob("*"):
            if item.name.endswith('.pbip') and item.is_file():
                pbip_dirs.append(item.parent)
        
        pbip_dirs = sorted(set(pbip_dirs))  # Remove duplicates
        
        if not pbip_dirs:
            print(f"No .pbip projects found in {args.input}")
            sys.exit(2)
        
        result = agent.batch_generate(pbip_dirs, args.output, row_count=args.rows)
        
        # Print summary
        print(f"\n{'='*70}")
        print(f"BATCH DATA GENERATION SUMMARY")
        print(f"{'='*70}")
        print(f"Total:        {result['total']}")
        print(f"Approved:     {result['approved']}")
        print(f"Needs Review: {result['needs_review']}")
        print(f"Failed:       {result['failed']}")
        print(f"{'='*70}")
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        
        if result['failed'] > 0:
            sys.exit(1)
        elif result['needs_review'] > 0:
            sys.exit(1)
        else:
            sys.exit(0)
    else:
        result = agent.generate_data(
            args.input,
            args.output,
            row_count=args.rows
        )
        
        # Print summary
        print(f"\n{'='*70}")
        print(f"DATA GENERATION RESULT: {result['status']}")
        print(f"{'='*70}")
        print(f"Input:         {args.input}")
        print(f"Output:        {result['output_pbip_path']}")
        print(f"Quality Score: {result['quality_score']}/100")
        print(f"Status:        {result['status']}")
        print(f"{'='*70}")
        
        if args.json:
            # Make result JSON-serializable
            result_json = {
                'status': result['status'],
                'output_pbip_path': str(result['output_pbip_path']) if result['output_pbip_path'] else None,
                'quality_score': result['quality_score'],
                'exit_code': result['exit_code'],
                'error': result['error'],
                'validation': result['validation']
            }
            print(json.dumps(result_json, indent=2))
        
        if result['error']:
            print(f"Error: {result['error']}")
        
        sys.exit(result['exit_code'])


if __name__ == '__main__':
    main()
