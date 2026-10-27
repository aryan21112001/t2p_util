#!/usr/bin/env python3
"""
Shared step implementations for the Tableau-to-PowerBI agent pipeline.

Split out of the single pipeline_orchestrator.py so basic_pipeline_orchestrator.py
(no data generation) and dummy_data_pipeline_orchestrator.py (with Agent 2/3 data
generation) can each compose their own run order without duplicating agent
plumbing. RUN ORDER within a script is still the caller's responsibility:

    Agent 1 (build) -> Agent 2/3 (data) -> Agent 5 (prompts) -> Agent 4 (validate)

There is no automated "apply fixes" step: every DAX/model fix a human writes
gets applied by hand in Power BI Desktop, guided by Agent 5's
COPILOT_FIX_PROMPTS.md. Agent 4's `effort_report.py` step still excludes
whatever is already live in the model (however it got there) from its to-do
list, so applying a fix by hand and re-running the pipeline still works.
"""

from pathlib import Path
from typing import Dict, Optional

import yaml

from converter_agent import ConverterAgent
from dummy_data_agent import DummyDataGenerator
from realistic_data_agent import RealisticDataGenerator
from copilot_prompts_agent import CopilotPromptsAgent
from validator_agent import ValidatorAgent


class PipelineSteps:
    """One method per pipeline stage. Each returns its own result dict.

    Agent 2 (dummy data) wires data IN PLACE into Agent 1's own output folder -- `pbip_path`
    stays the SAME path across that step, so a workbook run through the dummy-data pipeline ends
    up with exactly one output folder, not two. Agent 3 (opt-in, real-extract sampling) is the
    one exception: it still writes its OWN separate folder (real sampled data is worth keeping
    distinct from the synthetic baseline), so the caller must re-point `pbip_path` at Agent 3's
    `output_pbip_path` if it ran."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.converter_agent = ConverterAgent(verbose=verbose)
        self.dummy_generator = DummyDataGenerator(verbose=verbose)
        self.realistic_generator = RealisticDataGenerator(verbose=verbose)
        self.copilot_prompts_agent = CopilotPromptsAgent(verbose=verbose)
        self.validator_agent = ValidatorAgent(verbose=verbose)

    def log(self, message: str, level: str = "INFO"):
        if self.verbose or level in ("ERROR", "WARN"):
            print(f"[{level}] {message}")

    def load_config(self, config_path: Path) -> Dict:
        if not config_path.exists():
            return {}
        with open(config_path) as f:
            return yaml.safe_load(f) or {}

    def convert(self, input_file: Path, output_dir: Path) -> Dict:
        """AGENT 1: Convert Tableau to Power BI."""
        self.log("\n" + "=" * 60)
        self.log("AGENT 1: Tableau-to-PowerBI Converter")
        self.log("=" * 60)

        pbip_dir = output_dir / input_file.stem
        agent1_result = self.converter_agent.convert_workbook(
            input_file,
            pbip_dir,
            project_name=input_file.stem,
        )

        pbip_path = agent1_result.get('pbip_path')

        # Stop only if NO PROJECT WAS PRODUCED. Agent 1's own `success` flag is
        # `quality_score >= 90`, which conflates "the conversion ran" with "the
        # conversion is good enough" -- and that score is the PRE-fix score,
        # before any hand-authored DAX fix (guided by Agent 5's prompts) has
        # been applied in Desktop. Gating on it would stop the pipeline on
        # exactly the workbooks that need Agent 5's fix prompts most.
        if not pbip_path or not Path(pbip_path).exists():
            self.log("Agent 1 produced no project. Stopping pipeline.", "ERROR")
            return agent1_result

        if agent1_result['success']:
            self.log(f"✓ Agent 1 succeeded: {pbip_path}")
        else:
            self.log(f"Agent 1 built the project but scored "
                     f"{agent1_result.get('quality_score', '?')}/100 "
                     f"({agent1_result.get('status', 'unknown')}) -- this is the "
                     f"PRE-fix score; continuing so the later agents can improve it.",
                     "WARN")
        return agent1_result

    def generate_dummy_data(self, pbip_path: Path, input_file: Path,
                             output_dir: Path, config_dir: Path) -> Dict:
        """AGENT 2: Generate dummy (synthetic, schema-matched) data -- wired IN PLACE into
        `pbip_path` (Agent 1's own output folder), not a separate copy. A workbook run through
        the dummy-data orchestrator therefore ends up with exactly one output folder, the same
        one Agent 1 produced, now with data wired into its stub partitions."""
        self.log("\n" + "=" * 60)
        self.log("AGENT 2: Dummy Data Generator")
        self.log("=" * 60)

        dummy_config = self.load_config(config_dir / 'config_dummy_data.yaml')
        dummy_config_data = dummy_config.get('generation', {})
        result = self.dummy_generator.generate_data(
            Path(pbip_path),
            row_count=dummy_config_data.get('default_row_count', 20),
        )

        if result.get('success'):
            self.log(f"✓ Agent 2 succeeded: {result.get('output_pbip_path')}")
        else:
            self.log("Agent 2 failed. Continuing.", "WARN")
        return result

    def generate_realistic_data(self, pbip_path: Path, input_file: Path,
                                 output_dir: Path, config_dir: Path,
                                 sample_pct: Optional[float] = None,
                                 max_real_rows: Optional[int] = None) -> Dict:
        """AGENT 3: Generate realistic data sampled from a real embedded extract.

        `sample_pct`/`max_real_rows` override the generate-dummy-data skill's own defaults
        (100% / uncapped, i.e. full real data) -- e.g. `sample_pct=10` samples only 10% of
        the source extract, for a faster but less complete build."""
        self.log("\n" + "=" * 60)
        self.log("AGENT 3: Realistic Dummy Data Generator")
        self.log("=" * 60)

        realistic_config = self.load_config(config_dir / 'config_realistic_data.yaml')
        realistic_config_data = realistic_config.get('generation', {})
        data_out = output_dir / f"{input_file.stem}_realistic"
        result = self.realistic_generator.generate_data(
            Path(pbip_path),
            input_file,
            data_out,
            row_count=realistic_config_data.get('default_row_count', 20),
            sample_pct=sample_pct,
            max_real_rows=max_real_rows,
        )

        if result.get('success'):
            self.log(f"✓ Agent 3 succeeded: {result.get('output_pbip_path')}")
        else:
            self.log("Agent 3 failed. Continuing.", "WARN")
        return result

    def copilot_prompts(self, pbip_path: Path) -> Dict:
        """AGENT 5: Copilot prompts for whatever is still declined."""
        self.log("\n" + "=" * 60)
        self.log("AGENT 5: Copilot Fix Prompts")
        self.log("=" * 60)

        code5 = self.copilot_prompts_agent.run(Path(pbip_path), None)
        result = {
            'success': code5 == 0,
            'exit_code': code5,
            'projects': self.copilot_prompts_agent.results,
        }
        if code5 == 0:
            self.log(f"✓ Agent 5 succeeded: "
                     f"{len(self.copilot_prompts_agent.results)} project(s) documented")
        else:
            self.log("Agent 5 failed. Continuing.", "WARN")
        return result

    def validate(self, pbip_path: Path) -> Dict:
        """AGENT 4: Independent QA -- last, so it scores the model AFTER the fixes went in."""
        self.log("\n" + "=" * 60)
        self.log("AGENT 4: PBIP Validator")
        self.log("=" * 60)

        code4 = self.validator_agent.validate(Path(pbip_path))
        result = {
            'success': code4 == 0,
            'exit_code': code4,
            'projects': getattr(self.validator_agent, 'results', []),
        }
        if code4 == 0:
            self.log("✓ Agent 4 succeeded: all project(s) approved")
        else:
            self.log("Agent 4 reported findings -- see its summary.", "WARN")
        return result

    def write_output_pointer(self, output_dir: Path, final_pbip_path: Optional[Path],
                              other_folders_note: str) -> None:
        """Write a top-level START_HERE.md naming the one folder to open in Desktop.

        A single `-o` run can legitimately produce more than one `<Name>.pbip` on disk --
        Agent 1's own pre-data stub build, plus Agent 2/3's data-wired copy, each kept
        deliberately separate (see the orchestrator module docstrings for why). Nothing on
        disk said which one was the real deliverable, so a user browsing the output folder in
        Explorer had equal-looking odds of opening the blank stub instead of the final project
        -- exactly what happened on a real run (stub project opened, every tab showed zero
        data, even though the data-wired sibling folder was sitting right next to it with
        real rows in its Excel source). This file is the fix: cheap, always regenerated, and
        it travels with the output instead of living only in a chat transcript.
        """
        output_dir = Path(output_dir)
        lines = ["# START HERE\n"]
        if final_pbip_path and Path(final_pbip_path).exists():
            pbip_files = list(Path(final_pbip_path).glob("*.pbip"))
            open_target = pbip_files[0] if pbip_files else final_pbip_path
            lines.append(f"Open this file in Power BI Desktop: `{open_target}`\n")
            lines.append(
                "**After it opens, click Refresh (Home ribbon) before judging the data.** "
                "PBIP is a source-controlled, file-based format -- Desktop treats a project "
                "whose TMDL/PBIR files changed on disk since its own last session as needing "
                "an explicit refresh, and shows 'One or more calculated objects need to be "
                "manually refreshed' / 'Some of the tables have incomplete or no data' instead "
                "of auto-loading. Skipping Refresh makes EVERY visual look blank -- confirmed "
                "directly: COUNTROWS on a real table returned blank before Refresh, a real row "
                "count after it. This is standard Desktop behavior for a regenerated PBIP, not "
                "a defect in this build.\n")
        else:
            lines.append("No project was produced by this run -- see the console output / "
                          "logs for the stage that failed.\n")
        lines.append(other_folders_note)
        (output_dir / "START_HERE.md").write_text("\n".join(lines), encoding="utf-8")
