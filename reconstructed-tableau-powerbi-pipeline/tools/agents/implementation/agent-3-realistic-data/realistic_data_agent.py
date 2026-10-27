#!/usr/bin/env python3
"""
AGENT 3: Realistic Data Generator (Skills-First)
Creates realistic test data for Power BI projects based on source Tableau data profiles.
Uses profiles from original .twbx file to generate realistic synthetic data.

Architecture:
  1. Extract source data profiles from original .twbx using extract-twbx-contents
  2. Generate realistic data using generate-dummy-data skill with profiles
  3. Validate data using validate-dummy-data skill
  4. Persist validation results
  5. Return status and exit code
"""

import argparse
import json
import re
import subprocess
import sys
import traceback
import zipfile
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
    
    if skill_name == "extract-twbx-contents":
        # Extract TWBX file to temporary directory - use direct function call
        twbx_file = Path(args[0])
        output_dir = Path(args[1]) if len(args) > 1 else Path("/tmp") / twbx_file.stem
        
        try:
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
        
    elif skill_name == "generate-dummy-data":
        # Generate data for Power BI project (with or without --realistic)
        pbip_path = Path(args[0])
        output_dir = Path(args[1])
        row_count = kwargs.get('rows', 20)
        realistic_input = kwargs.get('realistic_input')
        sample_pct = kwargs.get('sample_pct')
        max_real_rows = kwargs.get('max_real_rows')

        script = skills_dir / "generate-dummy-data" / "scripts" / "generate_dummy_data.py"
        cmd = [sys.executable, str(script), str(pbip_path), "--out", str(output_dir), "--rows", str(row_count)]

        if realistic_input:
            cmd.extend(["--realistic", str(realistic_input)])
            if sample_pct is not None:
                cmd.extend(["--sample-pct", str(sample_pct)])
            if max_real_rows is not None:
                cmd.extend(["--max-real-rows", str(max_real_rows)])
        
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


def parse_validation_result(output: str) -> Dict[str, Any]:
    """
    Parse validation result from skill output.
    Expected format: JSON or text output with pass/fail status
    """
    try:
        if output.startswith('{'):
            return json.loads(output)
        # If not JSON, return as generic validation result
        return {
            'validation_text': output,
            'status': 'UNKNOWN'
        }
    except json.JSONDecodeError:
        return {
            'validation_text': output,
            'status': 'UNKNOWN'
        }


def calculate_quality_score(validation: Dict) -> int:
    """
    Calculate quality score (0-100) based on validation results.
    
    Scoring:
    - If validation passes: 100
    - If validation fails: 0
    - Otherwise: 50 (unknown state)
    """
    # Check common pass/fail indicators
    output_text = str(validation.get('validation_text', '')).lower()

    # validate-dummy-data's own --quiet summary line is "N/N project(s) clean." with no
    # "pass"/"success" word in it at all -- without this, every clean run fell into the
    # "unknown" 50 bucket below and Agent 3 never scored a real APPROVED.
    clean_match = re.search(r'(\d+)/(\d+) project\(s\) clean', output_text)
    if clean_match:
        clean, total = int(clean_match.group(1)), int(clean_match.group(2))
        return 100 if total > 0 and clean == total else 0

    if 'pass' in output_text or 'success' in output_text:
        return 100
    elif 'fail' in output_text or 'error' in output_text:
        return 0
    else:
        return 50


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


class RealisticDataGenerator:
    """Orchestrates realistic data generation using skills."""
    
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.repo_root = Path(__file__).parent.parent.parent.parent
        
    def log(self, message: str, level: str = "INFO"):
        """Log message with level."""
        if self.verbose or level in ("ERROR", "WARN"):
            print(f"[{level}] {message}")
    
    def persist_results(self, output_pbip_path: Path, validation: Dict, quality_score: int, status: str, source_twbx: str = "unknown"):
        """
        Persist validation results to JSON files.
        Creates:
        - DATA_VALIDATION.json: data quality metrics
        - REPORT.json: summary report
        """
        results_dir = output_pbip_path.parent
        
        # Save validation results
        validation_file = results_dir / "DATA_VALIDATION.json"
        validation_to_save = {
            'timestamp': datetime.now().isoformat(),
            'data_type': 'realistic',
            'source_twbx': source_twbx,
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
            'data_type': 'realistic',
            'source_twbx': source_twbx,
            'validation': validation_to_save
        }
        report_file.write_text(json.dumps(report, indent=2))
        self.log(f"✓ Saved summary report: {report_file}")
    
    def generate_data(
        self,
        pbip_path: Path,
        source_twbx: Path,
        output_dir: Path,
        row_count: int = 20,
        tds_dir: Optional[Path] = None,
        sample_pct: Optional[float] = None,
        max_real_rows: Optional[int] = None,
    ) -> Dict:
        """
        Generate realistic data for a Power BI project based on source Tableau data.
        
        Flow:
        1. Extract source data profiles from original .twbx
        2. Generate realistic data using generate-dummy-data skill with profiles
        3. Validate generated data using validate-dummy-data skill
        4. Persist results
        5. Return status with exit code
        
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
            source_twbx = Path(source_twbx).resolve()
            output_dir = Path(output_dir).resolve()
            
            if not pbip_path.exists():
                result['error'] = f"Input PBIP path not found: {pbip_path}"
                self.log(result['error'], "ERROR")
                return result
            
            if not source_twbx.exists():
                result['error'] = f"Source TWBX not found: {source_twbx}"
                self.log(result['error'], "ERROR")
                return result
            
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # ================================================================
            # STEP 1: Extract source data profiles from original .twbx
            # ================================================================
            self.log(f"Step 1/3: Extracting source data profiles...")
            
            extracted_dir = output_dir.parent / f"{source_twbx.stem}_profiles"
            extract_result = call_skill('extract-twbx-contents', source_twbx, extracted_dir)
            if not extract_result['success']:
                result['error'] = f"Extraction failed: {extract_result['error']}"
                self.log(result['error'], "ERROR")
                return result
            
            self.log(f"✓ Extracted source profiles: {extracted_dir}")
            
            # ================================================================
            # STEP 2: Generate realistic data using skill with profiles
            # ================================================================
            self.log(f"Step 2/3: Generating realistic data based on source profiles...")
            
            gen_result = call_skill(
                'generate-dummy-data',
                pbip_path,
                output_dir,
                rows=row_count,
                # `--realistic` opens the ORIGINAL .twbx as a zip itself to reach the embedded
                # .hyper members -- it needs source_twbx's own directory, not the unzipped
                # `extracted_dir` scratch copy above (which has no .twbx file in it at all, so
                # every table silently fell back to fully-synthetic no matter --sample-pct).
                realistic_input=source_twbx.parent,
                sample_pct=sample_pct,
                max_real_rows=max_real_rows,
            )
            if not gen_result['success']:
                result['error'] = f"Data generation failed: {gen_result['error']}"
                self.log(result['error'], "ERROR")
                return result
            
            # Find the generated project in output directory. `generate-dummy-data` always nests
            # the real project one level down (output_dir/<name>/<name>.pbip), so a direct-child
            # dirname check for '.Report'/'.SemanticModel'/'.pbip' never actually matches -- this
            # always fell through to an "exactly one subdirectory" fallback that is NOT robust to
            # re-running into an output directory that already has other artifacts sitting next
            # to the project folder. Confirmed real: a second run into an already-used -o path
            # (this agent's own `validation/` folder left over from a prior run) counted
            # [<name>, validation] as 2 subdirs and failed with "No output Power BI project
            # found" even though <name>/<name>.pbip was sitting right there, fully generated.
            # Fixed to search for the real `.pbip` file itself -- the one authoritative signal a
            # project exists -- rather than guessing from directory names or counts; picks the
            # most recently modified match if more than one is found (the one just generated).
            pbip_files = sorted(output_dir.glob("*/*.pbip"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            if not pbip_files:
                pbip_files = sorted(output_dir.glob("**/*.pbip"),
                                     key=lambda p: p.stat().st_mtime, reverse=True)
            if not pbip_files:
                result['error'] = "No output Power BI project found in generated directory"
                self.log(result['error'], "ERROR")
                return result
            output_pbip_path = pbip_files[0].parent
            
            self.log(f"✓ Generated realistic data: {output_pbip_path}")
            result['output_pbip_path'] = output_pbip_path
            
            # ================================================================
            # STEP 3: Validate generated data using skill
            # ================================================================
            self.log(f"Step 3/3: Validating generated data...")
            
            validate_result = call_skill('validate-dummy-data', output_pbip_path)
            if not validate_result['success']:
                # Validation warnings don't fail the whole process
                self.log(f"⚠ Validation warnings: {validate_result['error']}", "WARN")
                validation = {
                    'validation_text': validate_result['error'],
                    'status': 'WARNING'
                }
            else:
                validation = parse_validation_result(validate_result['output'])
            
            result['validation'] = validation
            self.log(f"✓ Validation complete")
            
            # ================================================================
            # STEP 4: Calculate quality score and status
            # ================================================================
            quality_score = calculate_quality_score(validation)
            status, exit_code = determine_status(quality_score)
            
            result['success'] = (exit_code == 0)
            result['status'] = status
            result['quality_score'] = quality_score
            result['exit_code'] = exit_code
            
            # ================================================================
            # STEP 5: Persist results
            # ================================================================
            self.persist_results(output_pbip_path, validation, quality_score, status, str(source_twbx))
            
            # ================================================================
            # Summary
            # ================================================================
            status_symbol = {
                'APPROVED': '✓',
                'NEEDS_REVIEW': '⚠',
                'FAILED': '✗'
            }.get(status, '?')
            
            self.log(f"\n{status_symbol} Realistic Data Generation {status}")
            self.log(f"  Quality Score: {quality_score}/100")
            self.log(f"  Status: {status}")
            self.log(f"  Row Count: {row_count}")
            self.log(f"  Source: {source_twbx.name}")
            
            return result
            
        except Exception as e:
            result['error'] = f"{type(e).__name__}: {str(e)}"
            if self.verbose:
                traceback.print_exc()
            self.log(result['error'], "ERROR")
            return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AGENT 3: Realistic Data Generator (Skills-First Architecture)"
    )
    parser.add_argument(
        'pbip_input',
        type=Path,
        help='Input .pbip project folder or parent directory of multiple projects'
    )
    parser.add_argument(
        'twbx_input',
        type=Path,
        help='Source .twbx file or directory containing .twbx files'
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
        help='Number of realistic rows per table (default 20)'
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
        '--json',
        action='store_true',
        help='Output results as JSON'
    )
    
    args = parser.parse_args()
    
    agent = RealisticDataGenerator(verbose=args.verbose)
    
    result = agent.generate_data(
        args.pbip_input,
        args.twbx_input,
        args.output,
        row_count=args.rows,
        tds_dir=args.tds
    )
    
    # Print summary
    print(f"\n{'='*70}")
    print(f"REALISTIC DATA GENERATION RESULT: {result['status']}")
    print(f"{'='*70}")
    print(f"PBIP Input:    {args.pbip_input}")
    print(f"TWBX Input:    {args.twbx_input}")
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
