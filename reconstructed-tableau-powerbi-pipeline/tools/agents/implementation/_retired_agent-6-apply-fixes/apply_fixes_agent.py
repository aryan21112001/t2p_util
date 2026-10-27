#!/usr/bin/env python3
"""
AGENT 6: Apply Fixes (Skills-First Architecture)

Writes reviewed DAX fixes into a PBIP's real semantic model. This is the step that turns
everything the earlier agents only STAGED into something Power BI Desktop actually loads.

Pipeline position
-----------------
  Agent 1 (convert)  ->  Agent 2/3 (data)  ->  Agent 5 (prompts)  ->  [ AGENT 6: apply ]  ->  Agent 4 (validate)

  1. Agent 1 builds the PBIP. Anything the deterministic tiers cannot translate is DECLINED, not
     approximated, and recorded in `_work/residue.json`.
  2. Agent 5 turns that residue into review material.
  3. THIS agent applies the fixes: the ones the skill can author itself, plus any reviewed
     hand-authored/Copilot DAX handed to it in an overrides file.
  4. Agent 4 validates the result.

Why this is a wrapper and not an LLM
------------------------------------
Every decision here is mechanical: fixed commands, fixed order, and a termination condition that
is literally "the skill reported promoted=0". There is no judgment to exercise, so this follows the
same shape as Agent 5 -- shell out to the skill, parse its summary line, aggregate -- rather than
reasoning about anything. The judgment lives in the overrides FILE (a human or Copilot decides what
the DAX should be); this agent only applies it deterministically and reports what happened.

The fixpoint loop is the whole point
------------------------------------
Promoting one calc can unblock others that were declined only because they depended on it, so a
single pass leaves most of the available recovery on the table. The skill re-attempts those
cascades on each run (its resolvers read the live `emitted` set), so this agent calls it REPEATEDLY
until a round promotes nothing. Measured on the reference corpus (WorkbookB Dashboard): a single pass
promoted 49 objects, while looping to the fixpoint promoted 141 across 11 rounds -- 49, 30, 11, 14,
14, 9, 6, 6, 1, 1, 0.

Overrides are auto-discovered
-----------------------------
If `dax_overrides/<Workbook>.json` exists at the repo root it is passed through automatically, so
the reviewed hand-authored fixes are re-applied on every rebuild without anyone having to remember
a flag. That file deliberately lives OUTSIDE the output tree, which every rebuild regenerates.

Usage:
  python3 apply_fixes_agent.py <pbip project folder, or parent of several>
  python3 apply_fixes_agent.py <path> --dry-run
  python3 apply_fixes_agent.py <path> --overrides /path/to/overrides.json
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# The skill does the work; this agent only sequences and reports it.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"
_PROMOTE_SCRIPT = (_SKILLS_DIR / "generate-copilot-fix-prompts" / "scripts"
                   / "promote_dax_candidates.py")

# Safety cap. The loop's real exit is `promoted=0`; this only stops a pathological case (e.g. a
# skill change that made promotion non-idempotent) from spinning forever. Set well above the 11
# rounds the largest real corpus workbook needed.
MAX_ROUNDS = 40

# `[ok] <name>  promoted=N (hand-authored=M) skipped=K`, parsed by NAME rather than position so a
# counter added to the skill later is additive instead of breaking the parse -- the exact bug that
# silently zeroed Agent 5's output when the prompts skill grew new counters.
_OK_LINE_RE = re.compile(r"^\[(?:ok|dry-run)\]\s+(?P<name>.+?)\s{2,}(?P<rest>\S.*)$")
_COUNTER_RE = re.compile(r"([\w-]+)=\s*(\d+)")


def find_projects(input_path: Path) -> List[Path]:
    """Project folders under `input_path` that have a `_work/residue.json`.

    Walks RECURSIVELY, matching how the skills themselves discover work (`os.walk` looking for
    `_work/residue.json`). A single-level glob looked right against a project's parent
    (`output/dummy_pbip`) but found nothing when pointed one level higher (`output`), where the
    real corpora live -- the same path a caller would naturally try first.
    """
    if (input_path / "_work" / "residue.json").is_file():
        return [input_path]
    return sorted({residue.parent.parent
                   for residue in input_path.rglob("_work/residue.json")})


def discover_overrides(project: Path) -> Optional[Path]:
    """`dax_overrides/<Workbook>.json` at the repo root, if it exists."""
    candidate = _REPO_ROOT / "dax_overrides" / f"{project.name}.json"
    return candidate if candidate.is_file() else None


def call_skill(project: Path, overrides: Optional[Path], dry_run: bool) -> Dict[str, Any]:
    """One invocation of promote_dax_candidates.py over a single project folder."""
    cmd = [sys.executable, str(_PROMOTE_SCRIPT), str(project)]
    if overrides:
        cmd += ["--overrides", str(overrides)]
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                 cwd=str(_SKILLS_DIR))
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "", "error": "promote_dax_candidates timed out (600s)"}
    except Exception as exc:                                    # noqa: BLE001
        return {"success": False, "output": "", "error": str(exc)}
    if result.returncode != 0:
        return {"success": False, "output": result.stdout,
                "error": result.stderr.strip() or f"exit code {result.returncode}"}
    return {"success": True, "output": result.stdout.strip(), "error": None}


def parse_round(output: str) -> Dict[str, Any]:
    """Counters from the skill's own summary line, plus any `skipped:`/`overrides:` detail lines."""
    counters: Dict[str, int] = {}
    notes: List[str] = []
    for line in (output or "").splitlines():
        m = _OK_LINE_RE.match(line.rstrip())
        if m:
            counters.update({k: int(v) for k, v in _COUNTER_RE.findall(m.group("rest"))})
            continue
        stripped = line.strip()
        if stripped.startswith(("skipped:", "overrides:")):
            notes.append(stripped)
    return {"counters": counters, "notes": notes}


class ApplyFixesAgent:
    """Agent 6: applies staged + reviewed DAX fixes to the real semantic model."""

    def __init__(self, verbose: bool = False, json_output: bool = False):
        self.verbose = verbose
        self.json_output = json_output
        self.results: List[Dict[str, Any]] = []

    def log(self, message: str, level: str = "INFO"):
        if not self.json_output:
            prefix = f"[{level}]" if level != "INFO" else ""
            print(f"{prefix} {message}" if prefix else message)

    def apply_to_project(self, project: Path, overrides: Optional[Path],
                         dry_run: bool) -> Dict[str, Any]:
        """Loop the skill over ONE project until a round promotes nothing."""
        rounds: List[Dict[str, Any]] = []
        total = hand = 0
        last_skipped = 0
        error = None

        for round_no in range(1, MAX_ROUNDS + 1):
            res = call_skill(project, overrides, dry_run)
            if not res["success"]:
                error = res["error"]
                break
            parsed = parse_round(res["output"])
            promoted = parsed["counters"].get("promoted", 0)
            hand += parsed["counters"].get("hand-authored", 0)
            last_skipped = parsed["counters"].get("skipped", last_skipped)
            total += promoted
            rounds.append({"round": round_no, **parsed["counters"],
                            "notes": parsed["notes"]})
            if self.verbose:
                self.log(f"    round {round_no}: promoted={promoted} "
                         f"skipped={parsed['counters'].get('skipped', 0)}")
                for note in parsed["notes"]:
                    self.log(f"      {note}")
            if promoted == 0:
                break
            # A dry run never actually writes, so the next round would report the identical
            # number forever -- report the single pass and stop rather than spin.
            if dry_run:
                break
        else:
            error = (f"did not reach a fixpoint within {MAX_ROUNDS} rounds -- promotion may not be "
                     f"idempotent; investigate before trusting this project")

        return {
            "project": project.name,
            "path": str(project),
            "overrides": str(overrides) if overrides else None,
            "dry_run": dry_run,
            "promoted_total": total,
            "hand_authored": hand,
            "skipped_last_round": last_skipped,
            "rounds": len(rounds),
            "round_detail": rounds,
            "error": error,
        }

    def run(self, input_path: Path, overrides_arg: Optional[Path], dry_run: bool) -> int:
        input_path = input_path.resolve()
        if not input_path.exists():
            self.log(f"Error: path does not exist: {input_path}", "ERROR")
            return 2
        if not _PROMOTE_SCRIPT.is_file():
            self.log(f"Error: skill script not found: {_PROMOTE_SCRIPT}", "ERROR")
            return 2

        projects = find_projects(input_path)
        if not projects:
            self.log(f"Error: no project with _work/residue.json found under {input_path}",
                     "ERROR")
            return 2

        mode = "DRY RUN (nothing will be written)" if dry_run else "applying to the real model"
        self.log(f"Agent 6: {len(projects)} project(s) -- {mode}")

        failed = 0
        for project in projects:
            overrides = overrides_arg or discover_overrides(project)
            if overrides and not self.json_output:
                self.log(f"  {project.name}: using overrides {overrides.name}")
            result = self.apply_to_project(project, overrides, dry_run)
            self.results.append(result)

            if result["error"]:
                failed += 1
                self.log(f"✗ {project.name}: {result['error']}", "ERROR")
                continue
            self.log(f"✓ {project.name}: promoted {result['promoted_total']} object(s) over "
                     f"{result['rounds']} round(s)"
                     + (f", {result['hand_authored']} hand-authored" if result["hand_authored"]
                        else "")
                     + (f", {result['skipped_last_round']} already present"
                        if result["skipped_last_round"] else ""))

        if failed:
            return 1
        self.log("\nNext: run Agent 4 (validator) and tools/pbip_rules/lint.py --schemas to "
                 "confirm the model still loads cleanly.")
        return 0

    def persist_summary(self, summary_path: Path):
        summary = {
            "timestamp": datetime.now().isoformat(),
            "agent": "6-apply-fixes",
            "total_projects": len(self.results),
            "total_promoted": sum(r["promoted_total"] for r in self.results),
            "total_hand_authored": sum(r["hand_authored"] for r in self.results),
            "projects": self.results,
        }
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2))
        self.log(f"✓ Saved run summary: {summary_path}")


def main():
    parser = argparse.ArgumentParser(
        description="AGENT 6: Apply reviewed DAX fixes to a PBIP's semantic model "
                    "(Skills-First Architecture)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Apply to one project, looping to a fixpoint (auto-uses dax_overrides/<Workbook>.json if present)
  %(prog)s output/workbookb_rebuild/"WorkbookB Dashboard"

  # See what WOULD be applied, writing nothing
  %(prog)s output/workbookb_rebuild/"WorkbookB Dashboard" --dry-run

  # Every project under a parent directory
  %(prog)s output/corpus_rebuild

  # Point at a specific overrides file instead of the auto-discovered one
  %(prog)s output/workbookb_rebuild/"WorkbookB Dashboard" --overrides dax_overrides/custom.json

IMPORTANT -- run this on the RAW Agent 1 output, BEFORE Agent 2/3 wire the data. Agent 2/3 COPY the
whole project, so promoting afterwards leaves the copy and the original out of step. A rebuild also
resets the model's name ledger, which is why reviewed fixes live in dax_overrides/ (outside the
output tree) and are simply re-applied after each rebuild.
""",
    )
    parser.add_argument("input", type=Path,
                        help="PBIP project folder, or a parent directory of several")
    parser.add_argument("--overrides", type=Path,
                        help="Reviewed hand-authored/Copilot DAX to apply as well "
                             "(default: auto-discover dax_overrides/<Workbook>.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be applied without writing any file")
    parser.add_argument("--summary", type=Path,
                        help="Where to write the JSON run summary "
                             "(default: <input>/APPLY_FIXES_RUN_SUMMARY.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show every round")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="JSON output format")
    args = parser.parse_args()

    agent = ApplyFixesAgent(verbose=args.verbose, json_output=args.json_output)
    exit_code = agent.run(args.input, args.overrides, args.dry_run)

    if exit_code == 0 and not args.dry_run:
        summary_path = args.summary or (args.input / "APPLY_FIXES_RUN_SUMMARY.json")
        agent.persist_summary(summary_path)

    if args.json_output:
        print(json.dumps({"exit_code": exit_code, "projects": agent.results}, indent=2))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
