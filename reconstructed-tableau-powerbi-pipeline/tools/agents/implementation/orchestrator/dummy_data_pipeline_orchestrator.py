#!/usr/bin/env python3
"""
Dummy Data Pipeline Orchestrator -- convert, wire synthetic (or realistic)
data, document, validate.

Use this when the output needs to open in Power BI Desktop against data --
Agent 2 generates deterministic dummy data by default, wired IN PLACE into
Agent 1's own output folder (no separate copy -- a workbook run through this
orchestrator ends up with exactly ONE output folder, not two). Agent 3 is
opt-in and samples a real embedded Tableau extract instead, into its own
separate folder (a real extract's data is worth keeping distinct from the
synthetic baseline). For a run with no data generation, see
basic_pipeline_orchestrator.py instead.

There is no automated "apply fixes" stage: DAX/model fixes are written by hand
in Power BI Desktop, guided by Agent 5's COPILOT_FIX_PROMPTS.md.

Usage:
  python3 dummy_data_pipeline_orchestrator.py <input.twbx> \
    --output /path/to/output \
    [--agent3] [--agent5] [--agent4] [--all-agents] [--verbose]
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


class DummyDataPipelineOrchestrator:
    """Orchestrates the data-generation run: Agent 1 -> 2/3 -> 5 -> 4."""

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
        run_agent3: bool = False,
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
                'agent2_result': dict,
                'agent3_result': dict,
                'agent5_result': dict,
                'agent4_result': dict,
                'summary': str
            }
        """
        result = {
            'success': False,
            'agent1_result': None,
            'agent2_result': None,
            'agent3_result': None,
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

            # AGENT 2: generate dummy (synthetic, schema-matched) data. This is the whole point
            # of this orchestrator, so it always runs.
            result['agent2_result'] = self.steps.generate_dummy_data(
                pbip_path, input_file, output_dir, config_dir)
            if result['agent2_result'].get('success'):
                # Agent 2 wires data IN PLACE now (no copy) -- output_pbip_path is the SAME
                # folder as pbip_path already. This reassignment is a defensive no-op, kept in
                # case a future change to Agent 2 ever needs to redirect downstream steps again.
                new_path = result['agent2_result'].get('output_pbip_path')
                if new_path and Path(new_path).exists():
                    pbip_path = new_path

            # AGENT 3: realistic data instead of/in addition to synthetic. Opt-in: it samples
            # a real embedded .hyper extract, and a workbook whose only connection is a
            # published-datasource proxy has none to sample.
            if run_agent3:
                result['agent3_result'] = self.steps.generate_realistic_data(
                    pbip_path, input_file, output_dir, config_dir)
                if result['agent3_result'].get('success'):
                    new_path = result['agent3_result'].get('output_pbip_path')
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

            realistic_note = (
                f"- `{input_file.stem}_realistic/` -- Agent 3 ran (`--agent3`) and wrote its "
                "own separate realistic-data folder, which takes priority over Agent 2's "
                "in-place synthetic data if both ran.\n" if run_agent3 else "")
            self.steps.write_output_pointer(
                output_dir, pbip_path if produced else None,
                "## Why this folder, not the others\n\n"
                f"- `{input_file.stem}/` -- the ONE project folder this pipeline produces. "
                "Agent 2 wires synthetic data into it IN PLACE (same path as Agent 1's own "
                "build), so there is no separate data-free copy to confuse this with.\n"
                f"- `{input_file.stem}_profiles/` -- intermediate source-data profiling "
                "output, not a Power BI project, nothing to open here.\n"
                + realistic_note
            )

            # Summary, listed in RUN order.
            summary_parts = [
                f"Agent 1 (Converter): "
                f"{'✓' if result['agent1_result'] and result['agent1_result']['success'] else '✗'}",
            ]
            summary_parts.append(
                f"Agent 2 (Dummy Data): "
                f"{'✓' if result['agent2_result'] and result['agent2_result']['success'] else '✗'}")
            if run_agent3:
                summary_parts.append(
                    f"Agent 3 (Realistic): "
                    f"{'✓' if result['agent3_result'] and result['agent3_result']['success'] else '✗'}")
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
        description="Dummy-data agent pipeline: Tableau -> Power BI, wire synthetic data, "
                    "document gaps, validate. Run order is 1 -> 2/3 -> 5 -> 4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert + dummy data only
  %(prog)s input.twbx -o out/

  # Full pipeline: convert, dummy data, prompts, validate
  %(prog)s input.twbx -o out/ --all-agents

  # Realistic data instead of/alongside synthetic (needs a real embedded .hyper extract)
  %(prog)s input.twbx -o out/ --agent3 --agent5 --agent4
""",
    )
    parser.add_argument('input', type=Path, help='Input Tableau workbook (.twbx or .twb)')
    parser.add_argument('-o', '--output', type=Path, required=True, help='Output directory')
    parser.add_argument(
        '--agent3', action='store_true',
        help='Run Agent 3 (Realistic Data Generator), needs a real embedded extract')
    parser.add_argument(
        '--agent5', action='store_true',
        help='Run Agent 5 (Copilot Fix Prompts) for whatever is still declined')
    parser.add_argument(
        '--agent4', action='store_true',
        help='Run Agent 4 (Validator) last, to QA the result')
    parser.add_argument(
        '--all-agents', action='store_true',
        help='Run the full pipeline: 1 -> 2 -> 5 -> 4 (Agent 3 is opt-in, it needs a source '
             'workbook with a real embedded extract)')
    parser.add_argument(
        '--config', type=Path, help='Config directory (defaults to this script\'s directory)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging')

    args = parser.parse_args()

    # Agent 3 is NOT part of --all-agents: see the help text above.
    run_agent3 = args.agent3
    run_agent5 = args.agent5 or args.all_agents
    run_agent4 = args.agent4 or args.all_agents

    orchestrator = DummyDataPipelineOrchestrator(verbose=args.verbose)
    result = orchestrator.run(
        args.input,
        args.output,
        run_agent3=run_agent3,
        run_agent5=run_agent5,
        run_agent4=run_agent4,
        config_dir=args.config,
    )

    print(f"\n{'='*60}")
    print(f"Pipeline Result")
    print(f"{'='*60}")
    print(result['summary'])

    # The FINAL project -- Agent 1's own output folder, with data wired in place by Agent 2
    # (no separate copy anymore; see this module's docstring).
    final_path = result.get('final_pbip_path') or (
        (result['agent1_result'] or {}).get('pbip_path'))
    if final_path:
        print(f"\nOutput: {final_path}")

    sys.exit(0 if result['success'] else 1)


if __name__ == '__main__':
    main()
