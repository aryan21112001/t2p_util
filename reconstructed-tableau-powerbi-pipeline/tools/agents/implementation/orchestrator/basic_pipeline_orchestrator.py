#!/usr/bin/env python3
"""
Basic Pipeline Orchestrator -- convert, document, validate.

No data generation: this is the run for a project that will be opened against
its OWN source data (a real Power BI connection), not a synthetic stand-in.
For dummy/synthetic data, see dummy_data_pipeline_orchestrator.py instead.

There is no automated "apply fixes" stage: DAX/model fixes are written by hand
in Power BI Desktop, guided by Agent 5's COPILOT_FIX_PROMPTS.md.

Usage:
  python3 basic_pipeline_orchestrator.py <input.twbx> \
    --output /path/to/output \
    [--agent5] [--agent4] [--all-agents] [--verbose]
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional

# Each agent lives in its OWN `agent-*/` folder, so adding `implementation/` alone put none of the
# modules on the path and every import below raised ModuleNotFoundError -- this file could not be
# run at all, `--help` included. Add each agent directory explicitly.
_IMPL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_IMPL))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for _agent_dir in sorted(_IMPL.glob("agent-*")):
    sys.path.insert(0, str(_agent_dir))

from pipeline_steps import PipelineSteps


class BasicPipelineOrchestrator:
    """Orchestrates the no-data-generation run: Agent 1 -> 5 -> 4."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.steps = PipelineSteps(verbose=verbose)

    def log(self, message: str, level: str = "INFO"):
        if self.verbose or level in ("ERROR", "WARN"):
            print(f"[{level}] {message}")

    def run(
        self,
        input_file: Path,
        output_dir: Path,
        run_agent5: bool = False,
        run_agent4: bool = False,
        config_dir: Optional[Path] = None,
    ) -> Dict:
        """
        Run the pipeline.

        Returns:
            {
                'success': bool,
                'agent1_result': dict,
                'agent5_result': dict,
                'agent4_result': dict,
                'summary': str
            }
        """
        result = {
            'success': False,
            'agent1_result': None,
            'agent5_result': None,
            'agent4_result': None,
            'summary': '',
        }

        try:
            output_dir = Path(output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            config_dir = config_dir or Path(__file__).parent

            # AGENT 1: always runs -- there is nothing to document or validate otherwise.
            result['agent1_result'] = self.steps.convert(input_file, output_dir)
            pbip_path = result['agent1_result'].get('pbip_path')
            if not pbip_path or not Path(pbip_path).exists():
                return result

            # AGENT 5: Copilot prompts for whatever is still declined.
            if run_agent5:
                result['agent5_result'] = self.steps.copilot_prompts(pbip_path)

            # AGENT 4: independent QA -- last, so it scores the model AFTER any hand-applied fixes.
            if run_agent4:
                result['agent4_result'] = self.steps.validate(pbip_path)

            # The pipeline SUCCEEDED if it produced a project and no stage hard-failed.
            # Deliberately NOT `agent1_result['success']`, which is a >=90 quality gate on
            # the PRE-fix build. When Agent 4 ran, its verdict is the one that counts.
            result['final_pbip_path'] = str(pbip_path)
            produced = bool(pbip_path) and Path(pbip_path).exists()
            if run_agent4 and result['agent4_result'] is not None:
                result['success'] = produced and result['agent4_result']['success']
            else:
                result['success'] = produced

            self.steps.write_output_pointer(
                output_dir, pbip_path if produced else None,
                "## Why this folder, not the others\n\n"
                f"- `{input_file.stem}/` -- the ONE project folder this pipeline produces. "
                "No data-generation agent ran, so its partitions are stub/empty by design -- "
                "point it at a real Power BI data source before expecting any tab to show "
                "values.\n"
            )

            # Summary, listed in RUN order.
            summary_parts = [
                f"Agent 1 (Converter): "
                f"{'✓' if result['agent1_result'] and result['agent1_result']['success'] else '✗'}",
            ]
            if run_agent5:
                summary_parts.append(
                    f"Agent 5 (Prompts): "
                    f"{'✓' if result['agent5_result'] and result['agent5_result']['success'] else '✗'}")
            if run_agent4:
                summary_parts.append(
                    f"Agent 4 (Validate): "
                    f"{'✓' if result['agent4_result'] and result['agent4_result']['success'] else '✗'}")

            result['summary'] = " | ".join(summary_parts)
            return result

        except Exception as e:
            self.log(f"Pipeline error: {e}", "ERROR")
            return result


def main():
    parser = argparse.ArgumentParser(
        description="Basic agent pipeline: Tableau -> Power BI, document gaps, validate. No data "
                    "generation -- use dummy_data_pipeline_orchestrator.py for that. Run order is "
                    "1 -> 5 -> 4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert only
  %(prog)s input.twbx -o out/

  # Full basic pipeline: convert, prompts, validate
  %(prog)s input.twbx -o out/ --all-agents

  # Convert + Copilot fix prompts, nothing else
  %(prog)s input.twbx -o out/ --agent5
""",
    )
    parser.add_argument('input', type=Path, help='Input Tableau workbook (.twbx or .twb)')
    parser.add_argument('-o', '--output', type=Path, required=True, help='Output directory')
    parser.add_argument(
        '--agent5', action='store_true',
        help='Run Agent 5 (Copilot Fix Prompts) for whatever is still declined')
    parser.add_argument(
        '--agent4', action='store_true',
        help='Run Agent 4 (Validator) last, to QA the result')
    parser.add_argument(
        '--all-agents', action='store_true',
        help='Run the full basic pipeline: 1 -> 5 -> 4')
    parser.add_argument(
        '--config', type=Path, help='Config directory (defaults to this script\'s directory)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging')

    args = parser.parse_args()

    run_agent5 = args.agent5 or args.all_agents
    run_agent4 = args.agent4 or args.all_agents

    orchestrator = BasicPipelineOrchestrator(verbose=args.verbose)
    result = orchestrator.run(
        args.input,
        args.output,
        run_agent5=run_agent5,
        run_agent4=run_agent4,
        config_dir=args.config,
    )

    print(f"\n{'='*60}")
    print(f"Pipeline Result")
    print(f"{'='*60}")
    print(result['summary'])

    final_path = result.get('final_pbip_path') or (
        (result['agent1_result'] or {}).get('pbip_path'))
    if final_path:
        print(f"\nOutput: {final_path}")

    sys.exit(0 if result['success'] else 1)


if __name__ == '__main__':
    main()
