#!/usr/bin/env python3
"""
AGENT (isolated, not part of the numbered 1-5 pipeline): DAX Measure Extension

Thin wrapper over the `extend-dax-measures` skill -- same "agent = wrapper over skills, no LLM
in the loop, fixed commands" convention every other agent in this folder follows (see
tools/agents/README_AGENTS.md). Deliberately its own agent, not folded into Agent 1's converter
step: it acts on an ALREADY-BUILT project and writes a SEPARATE, new copy, so a run of this agent
never changes what Agent 1/2/5/4 already produced for the same workbook.

Usage:
  python3 dax_measures_extension_agent.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>
"""
import argparse
import subprocess
import sys
from pathlib import Path


def call_skill(twbx: str, pbip_dir: str, out_dir: str) -> dict:
    repo_root = Path(__file__).parent.parent.parent.parent.parent
    script = (repo_root / ".claude" / "skills" / "extend-dax-measures"
              / "scripts" / "extend_dax_measures.py")
    cmd = [sys.executable, str(script), twbx, pbip_dir, "--out", out_dir]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"success": False, "error": result.stderr.strip() or result.stdout.strip()}
    report_path = Path(out_dir) / "DAX_MEASURE_EXTENSION_REPORT.md"
    return {"success": True, "stdout": result.stdout.strip(),
            "out_dir": out_dir, "report": str(report_path) if report_path.exists() else None}


def main():
    parser = argparse.ArgumentParser(
        description="Isolated agent: promote resolvable LOOKUP(...,0)/INDEX()/fixed-window "
                    "WINDOW_SUM table calcs into a SEPARATE copy of an existing "
                    "build-tableau-pbip project. Never touches any *.Report file.")
    parser.add_argument("twbx", help="original .twbx/.twb (provenance only)")
    parser.add_argument("pbip_dir", help="existing build-tableau-pbip output folder")
    parser.add_argument("-o", "--out", required=True, help="new output folder (must not exist)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("AGENT: DAX Measure Extension (isolated, experimental)")
    print("=" * 60)

    result = call_skill(args.twbx, args.pbip_dir, args.out)

    if not result["success"]:
        print(f"[ERROR] {result['error']}")
        sys.exit(1)

    print(f"[INFO] {result['stdout']}")
    if result["report"]:
        print(f"[INFO] Report: {result['report']}")
    print()
    print("=" * 60)
    print("Result: SUCCESS")
    print(f"New project: {result['out_dir']}")
    print("=" * 60)
    print()
    print("This is a SEPARATE copy -- the original build was not touched, and no *.Report file")
    print("was read or written. Review the report above, then open the new project in Power BI")
    print("Desktop before trusting any promoted measure (see the report's own 'Before trusting")
    print("this build' checklist).")
    sys.exit(0)


if __name__ == "__main__":
    main()
