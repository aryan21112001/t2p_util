#!/usr/bin/env python3
"""
AGENT 5: Copilot Fix Prompt Generator (Skills-First)
Turns a build-tableau-pbip/generate-dummy-data output's residue into ready-to-paste Copilot
prompts, so a reviewer can hand each declined gap straight to Power BI Desktop's Copilot instead
of re-deriving the fix by hand.

Architecture:
  1. Discover project folder(s) with a `_work/residue.json` under the input path
  2. Generate Copilot prompts per project (call-skill: generate-copilot-fix-prompts)
  3. Parse the skill's per-project summary line
  4. Aggregate results and persist a run summary
  5. Return status and exit code
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List


def call_skill(pbip_path: Path, out_dir: Path = None) -> Dict[str, Any]:
    """Run generate-copilot-fix-prompts over a single project folder (or a parent directory of
    several -- the skill itself walks the tree for `_work/residue.json`)."""
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    skills_dir = repo_root / ".claude" / "skills"
    script = skills_dir / "generate-copilot-fix-prompts" / "scripts" / "generate_copilot_prompts.py"

    cmd = [sys.executable, str(script), str(pbip_path)]
    if out_dir:
        cmd.extend(["--out", str(out_dir)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, cwd=str(skills_dir)
        )
        if result.returncode != 0:
            return {"success": False, "output": result.stdout, "error": result.stderr or
                    f"generate-copilot-fix-prompts failed with exit code {result.returncode}"}
        return {"success": True, "output": result.stdout.strip(), "error": None}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": None, "error": "generate-copilot-fix-prompts timed out after 300s"}
    except Exception as e:
        return {"success": False, "output": None, "error": str(e)}


# Parsed FIELD-BY-FIELD rather than as one positional pattern. The previous single regex listed
# every counter in a fixed order, so adding a counter to the skill's summary line silently broke
# this agent: it stopped matching entirely and reported zero projects rather than failing loudly.
# The skill has since grown `root_causes`, `cascade` and `already_fixed_excluded`, and will grow
# more. Keying on names makes new counters additive, and missing ones an explicit None instead of
# a total parse failure.
_OK_LINE_RE = re.compile(r"^\[ok\]\s+(?P<name>.+?)\s{2,}(?P<counters>\S.*?)\s+->\s+(?P<path>.+)$")
_COUNTER_RE = re.compile(r"(\w+)=(\d+)")


def parse_skill_output(output: str) -> List[Dict[str, Any]]:
    """The skill prints one `[ok] <name>  <k>=<n> <k>=<n> ...  -> <path>` line per project.

    Every `name=number` pair on the line is captured, so a counter added to the skill later shows
    up here automatically instead of breaking the parse. The four the report has always used are
    still surfaced explicitly (defaulting to None when absent) so downstream formatting can tell
    "not reported" apart from "zero".
    """
    projects = []
    for line in (output or "").splitlines():
        m = _OK_LINE_RE.match(line.rstrip())
        if not m:
            continue
        counters = {k: int(v) for k, v in _COUNTER_RE.findall(m.group("counters"))}
        projects.append({
            "project": m.group("name").strip(),
            "dax_gaps": counters.get("dax_gaps"),
            "structural_gaps": counters.get("structural_gaps"),
            "visual_undecided": counters.get("visual_undecided"),
            "zone_gaps": counters.get("zone_gaps"),
            "counters": counters,          # everything, including any newly added key
            "output_file": m.group("path").strip(),
        })
    return projects


class CopilotPromptsAgent:
    """Copilot Fix Prompt Generator Agent."""

    def __init__(self, verbose: bool = False, json_output: bool = False):
        self.verbose = verbose
        self.json_output = json_output
        self.results: List[Dict[str, Any]] = []

    def log(self, message: str, level: str = "INFO"):
        if not self.json_output:
            prefix = f"[{level}]" if level != "INFO" else ""
            print(f"{prefix} {message}" if prefix else message)

    def run(self, input_path: Path, out_dir: Path = None) -> int:
        input_path = input_path.resolve()
        if not input_path.exists():
            self.log(f"Error: path does not exist: {input_path}", "ERROR")
            return 2

        self.log(f"Scanning for residue.json under: {input_path}")
        skill_result = call_skill(input_path, out_dir)

        if not skill_result["success"]:
            self.log(f"Error: {skill_result['error']}", "ERROR")
            return 2

        projects = parse_skill_output(skill_result["output"])
        if not projects:
            self.log("Error: no projects with _work/residue.json found", "ERROR")
            return 2

        self.results = projects
        for p in projects:
            self.log(
                f"✓ {p['project']}: {p['dax_gaps']} DAX gap(s), {p['structural_gaps']} "
                f"structural gap(s), {p['visual_undecided']} undecided visual(s), "
                f"{p['zone_gaps']} dropped zone(s) -> {p['output_file']}"
            )

        return 0

    def persist_summary(self, summary_path: Path):
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_projects": len(self.results),
            "total_dax_gaps": sum(p["dax_gaps"] for p in self.results),
            "total_structural_gaps": sum(p["structural_gaps"] for p in self.results),
            "projects": self.results,
        }
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2))
        self.log(f"✓ Saved run summary: {summary_path}")


def main():
    parser = argparse.ArgumentParser(
        description="AGENT 5: Copilot Fix Prompt Generator (Skills-First Architecture)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single project (build-tableau-pbip or generate-dummy-data output)
  %(prog)s /path/to/output/dummy_pbip/MyDashboard

  # Every project under a parent directory
  %(prog)s /path/to/output/dummy_pbip

  # Redirect COPILOT_FIX_PROMPTS.md elsewhere instead of writing in place
  %(prog)s /path/to/output/dummy_pbip --out /path/to/reports
""",
    )
    parser.add_argument("input", type=Path, help="Project folder or parent directory of several")
    parser.add_argument("-o", "--out", type=Path, help="Write COPILOT_FIX_PROMPTS.md under <out>/<Project>/ instead of in place")
    parser.add_argument("--summary", type=Path, help="Path to write a JSON run summary (default: <input>/COPILOT_PROMPTS_RUN_SUMMARY.json when input is a directory)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", dest="json_output", help="JSON output format")

    args = parser.parse_args()

    agent = CopilotPromptsAgent(verbose=args.verbose, json_output=args.json_output)
    exit_code = agent.run(args.input, args.out)

    if exit_code == 0:
        summary_path = args.summary or (args.input / "COPILOT_PROMPTS_RUN_SUMMARY.json")
        agent.persist_summary(summary_path)

    if args.json_output:
        print(json.dumps({"exit_code": exit_code, "projects": agent.results}, indent=2))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
