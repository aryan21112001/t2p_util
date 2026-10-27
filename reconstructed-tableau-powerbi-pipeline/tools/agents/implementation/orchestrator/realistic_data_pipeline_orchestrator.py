#!/usr/bin/env python3
"""
Realistic Data Pipeline Orchestrator -- convert, wire data sampled from the
source workbook's own real embedded extract, document, validate.

Use this when the output needs to open in Power BI Desktop against data that
looks like the real thing -- Agent 3 samples the source .twbx's own embedded
.hyper extract and writes a NEW, separate output folder (real sampled data is
worth keeping distinct from Agent 1's plain stub build, unlike Agent 2's
synthetic data which is wired in place). This is the mirror image of
dummy_data_pipeline_orchestrator.py: here Agent 3 always runs and Agent 2
(synthetic dummy data) is the opt-in one, instead of the other way round.

Agent 3 needs a workbook with a real embedded .hyper extract -- a workbook
whose only connection is a published-datasource proxy has none to sample, and
Agent 3 will report failure rather than fabricate one. When that happens, the
pipeline continues with Agent 1's own stub-partition build (or, if --agent2
was also passed, with Agent 2's synthetic data) rather than stopping.

There is no automated "apply fixes" stage: DAX/model fixes are written by hand
in Power BI Desktop, guided by Agent 5's COPILOT_FIX_PROMPTS.md.

Usage:
  python3 realistic_data_pipeline_orchestrator.py <input.twbx> \
    --output /path/to/output \
    [--agent2] [--agent5] [--agent4] [--all-agents] [--verbose]
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


class RealisticDataPipelineOrchestrator:
    """Orchestrates the realistic-data run: Agent 1 -> 3/2 -> 5 -> 4."""

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
        run_agent2: bool = False,
        run_agent5: bool = False,
        run_agent4: bool = False,
        config_dir: Optional[Path] = None,
        sample_pct: Optional[float] = None,
        max_real_rows: Optional[int] = None,
    ) -> Dict:
        """
        Run the pipeline.

        Returns:
            {
                'success': bool,
                'agent1_result': dict,
                'agent3_result': dict,
                'agent2_result': dict,
                'agent5_result': dict,
                'agent4_result': dict,
                'summary': str
            }
        """
        result = {
            'success': False,
            'agent1_result': None,
            'agent3_result': None,
            'agent2_result': None,
            'agent5_result': None,
            'agent4_result': None,
            'summary': '',
        }

        try:
            output_dir = Path(output_dir).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            config_dir = config_dir or Path(__file__).parent

            # AGENT 1: always runs -- there is nothing to wire data into otherwise.
            result['agent1_result'] = self.steps.convert(input_file, output_dir)
            pbip_path = result['agent1_result'].get('pbip_path')
            if not pbip_path or not Path(pbip_path).exists():
                return result

            # AGENT 3: realistic data sampled from the source workbook's own embedded extract.
            # This is the whole point of this orchestrator, so it always runs. It writes its OWN
            # separate output folder, so re-point pbip_path only on success -- on failure (e.g.
            # no real embedded extract to sample) the pipeline keeps Agent 1's stub build.
            result['agent3_result'] = self.steps.generate_realistic_data(
                pbip_path, input_file, output_dir, config_dir,
                sample_pct=sample_pct, max_real_rows=max_real_rows)
            if result['agent3_result'].get('success'):
                new_path = result['agent3_result'].get('output_pbip_path')
                if new_path and Path(new_path).exists():
                    pbip_path = new_path

            # AGENT 2: synthetic dummy data instead of/in addition to the realistic sample.
            # Opt-in: it wires data IN PLACE into whatever `pbip_path` currently points at
            # (Agent 1's stub build, or Agent 3's realistic-data folder if that ran first).
            if run_agent2:
                result['agent2_result'] = self.steps.generate_dummy_data(
                    pbip_path, input_file, output_dir, config_dir)
                if result['agent2_result'].get('success'):
                    new_path = result['agent2_result'].get('output_pbip_path')
                    if new_path and Path(new_path).exists():
                        pbip_path = new_path

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

            used_realistic = result['agent3_result'] and result['agent3_result'].get('success')
            self.steps.write_output_pointer(
                output_dir, pbip_path if produced else None,
                "## Why this folder, not the others\n\n"
                f"- `{input_file.stem}/` -- Agent 1's own build, with NO data wired in "
                "(stub partitions only). Opening it shows every tab blank. It only matters "
                "if Agent 3 failed and you need the pre-data structure.\n"
                f"- `{input_file.stem}_realistic/` -- Agent 3's output, with real sampled rows "
                "wired in from the source workbook's own embedded extract. "
                + ("This is the one above -- Agent 3 succeeded.\n" if used_realistic else
                   "Agent 3 FAILED this run, so this folder may be missing or still "
                   "data-less -- check the console log for Agent 3's error.\n")
                + f"- `{input_file.stem}_profiles/` -- intermediate source-data profiling "
                "output Agent 3 reads from. Not a Power BI project, nothing to open here.\n"
            )

            # Summary, listed in RUN order.
            summary_parts = [
                f"Agent 1 (Converter): "
                f"{'✓' if result['agent1_result'] and result['agent1_result']['success'] else '✗'}",
                f"Agent 3 (Realistic Data): "
                f"{'✓' if result['agent3_result'] and result['agent3_result']['success'] else '✗'}",
            ]
            if run_agent2:
                summary_parts.append(
                    f"Agent 2 (Dummy Data): "
                    f"{'✓' if result['agent2_result'] and result['agent2_result']['success'] else '✗'}")
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
        description="Realistic-data agent pipeline: Tableau -> Power BI, wire data sampled from "
                    "the source workbook's own real embedded extract, document gaps, validate. "
                    "Run order is 1 -> 3/2 -> 5 -> 4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert + realistic data only
  %(prog)s input.twbx -o out/

  # Full pipeline: convert, realistic data, prompts, validate
  %(prog)s input.twbx -o out/ --all-agents

  # Synthetic dummy data instead of/alongside the realistic sample
  %(prog)s input.twbx -o out/ --agent2 --agent5 --agent4
""",
    )
    parser.add_argument('input', type=Path, help='Input Tableau workbook (.twbx or .twb)')
    parser.add_argument('-o', '--output', type=Path, required=True, help='Output directory')
    parser.add_argument(
        '--agent2', action='store_true',
        help='Run Agent 2 (Dummy Data Generator) instead of/alongside Agent 3')
    parser.add_argument(
        '--agent5', action='store_true',
        help='Run Agent 5 (Copilot Fix Prompts) for whatever is still declined')
    parser.add_argument(
        '--agent4', action='store_true',
        help='Run Agent 4 (Validator) last, to QA the result')
    parser.add_argument(
        '--all-agents', action='store_true',
        help='Run the full pipeline: 1 -> 3 -> 5 -> 4 (Agent 2 is opt-in, it wires synthetic '
             'data in place instead of/alongside Agent 3\'s realistic sample)')
    parser.add_argument(
        '--sample-pct', type=float, default=None,
        help='Agent 3: %% of the source extract\'s rows to sample (skill default: 100, i.e. '
             'every row). Pass a smaller value for a faster but less complete build.')
    parser.add_argument(
        '--max-real-rows', type=int, default=None,
        help='Agent 3: hard cap on sampled real rows per table regardless of --sample-pct '
             '(skill default: 0, i.e. uncapped). Only set this to a positive number if you '
             'deliberately want a capped/faster build.')
    parser.add_argument(
        '--config', type=Path, help='Config directory (defaults to this script\'s directory)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging')

    args = parser.parse_args()

    # Agent 2 is NOT part of --all-agents: see the help text above.
    run_agent2 = args.agent2
    run_agent5 = args.agent5 or args.all_agents
    run_agent4 = args.agent4 or args.all_agents

    orchestrator = RealisticDataPipelineOrchestrator(verbose=args.verbose)
    result = orchestrator.run(
        args.input,
        args.output,
        run_agent2=run_agent2,
        run_agent5=run_agent5,
        run_agent4=run_agent4,
        config_dir=args.config,
        sample_pct=args.sample_pct,
        max_real_rows=args.max_real_rows,
    )

    print(f"\n{'='*60}")
    print(f"Pipeline Result")
    print(f"{'='*60}")
    print(result['summary'])

    # The FINAL project -- Agent 3's realistic-data folder if it succeeded, otherwise Agent 1's
    # own output folder (see this module's docstring).
    final_path = result.get('final_pbip_path') or (
        (result['agent1_result'] or {}).get('pbip_path'))
    if final_path:
        print(f"\nOutput: {final_path}")

    sys.exit(0 if result['success'] else 1)


if __name__ == '__main__':
    main()
