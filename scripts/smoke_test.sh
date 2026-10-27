#!/usr/bin/env bash
# End-to-end smoke test for the pipeline under reconstructed-tableau-powerbi-pipeline/.
#
#   1. checks Python and the third-party packages,
#   2. runs every script's own built-in self-test,
#   3. runs the full pipeline (Agent 1 -> [2] -> 5 -> 4) on the bundled test workbook and checks that
#      every stage produced its artifacts.
#
# It proves the pipeline runs and writes its outputs. It does NOT judge conversion quality: the bundled
# "Synthetic Coverage Probe.twb" deliberately uses features Power BI has no equivalent for, so Agent 1
# and Agent 4 report it as FAILED (about 63/100) by design.
#
# Usage:  scripts/smoke_test.sh          (from the repository root, or anywhere)
# Needs:  pip install -r requirements.txt   (tableauhyperapi is optional here)
# Network: not needed for this script.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPE="$ROOT/reconstructed-tableau-powerbi-pipeline"
PROBE="$PIPE/.claude/skills/build-tableau-pbip/tests/Synthetic Coverage Probe.twb"
ORCH="$PIPE/tools/agents/implementation/orchestrator"
export PYTHONDONTWRITEBYTECODE=1
PY="${PYTHON:-python3}"
export PIPE
WORK="$(mktemp -d "${TMPDIR:-/tmp}/t2p_smoke.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
FAILS=0
ok()   { echo "  PASS  $*"; }
bad()  { echo "  FAIL  $*"; FAILS=$((FAILS+1)); }
skip() { echo "  SKIP  $*"; }

echo "== 1. Environment =="
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' && ok "Python $("$PY" -c 'import platform; print(platform.python_version())')" || bad "Python 3.9+ required"
for mod in openpyxl docx jsonschema referencing yaml; do
  "$PY" -c "import $mod" 2>/dev/null && ok "import $mod" || bad "missing package for '$mod' (pip install -r requirements.txt)"
done
"$PY" -c "import tableauhyperapi" 2>/dev/null && ok "import tableauhyperapi" || skip "tableauhyperapi not installed (only Agent 3 / --realistic need it)"
[ -f "$PROBE" ] && ok "test workbook present" || bad "test workbook missing: $PROBE"

echo "== 2. Built-in self-tests =="
selftest() {  # dir script [args...]
  local d="$1"; shift
  local out; out="$(cd "$PIPE/$d" && timeout 600 "$PY" "$@" 2>&1)"; local rc=$?
  if echo "$out" | grep -qE 'Traceback|AssertionError|FAIL'; then
    # known gap: needs docs/examples/GroundTruth.pbip, which is not part of this repository
    if echo "$out" | grep -q 'GroundTruth'; then skip "$d/$1 (needs docs/examples/GroundTruth.pbip, not in repo)"; return; fi
    bad "$d/$*"; echo "$out" | tail -5 | sed 's/^/        /'
  elif [ $rc -ne 0 ] && ! echo "$out" | grep -q 'self-test OK'; then
    bad "$d/$* (exit $rc)"; echo "$out" | tail -3 | sed 's/^/        /'
  else
    ok "$d/$*"
  fi
}
S=.claude/skills
for f in extract dax tmdl pbir names; do selftest $S/build-tableau-pbip/scripts $f.py; done
selftest $S/generate-dummy-data/scripts generate_dummy_data.py
selftest $S/validate-tableau-pbip-mapping/scripts validate_mapping.py
selftest $S/generate-copilot-fix-prompts/scripts effort_report.py
selftest $S/generate-copilot-fix-prompts/scripts generate_copilot_prompts.py --selftest
selftest $S/validate-dummy-data/scripts validate_dummy_data.py
selftest $S/extend-dax-measures/scripts extend_dax_measures.py --selftest
selftest $S/extend-tablecalc-rank/scripts extend_tablecalc_rank.py --selftest
selftest $S/extend-tablecalc-quickcalcs/scripts extend_tablecalc_quickcalcs.py --selftest

echo "== 3. Full pipeline on the bundled workbook =="
need() {  # description, pattern under dir
  local desc="$1" dir="$2" pat="$3"
  if [ -n "$(find "$dir" -name "$pat" -type f 2>/dev/null | head -1)" ]; then ok "$desc"; else bad "$desc (no '$pat' under $dir)"; fi
}
mkdir -p "$WORK/basic" "$WORK/dummy"
"$PY" "$ORCH/basic_pipeline_orchestrator.py" "$PROBE" -o "$WORK/basic" --all-agents > "$WORK/basic.log" 2>&1
grep -q 'Pipeline Result' "$WORK/basic.log" && ok "basic orchestrator ran to completion" || { bad "basic orchestrator did not finish"; tail -8 "$WORK/basic.log" | sed 's/^/        /'; }
need "Agent 1: Power BI project (.pbip)"        "$WORK/basic" '*.pbip'
need "Agent 1: COVERAGE.md"                      "$WORK/basic" 'COVERAGE.md'
need "Agent 1: RESIDUE.md"                       "$WORK/basic" 'RESIDUE.md'
need "Agent 5: mandatory fix prompts"            "$WORK/basic" 'COPILOT_FIX_PROMPTS_MANDATORY.md'
need "Agent 5: optional fix prompts"             "$WORK/basic" 'COPILOT_FIX_PROMPTS_OPTIONAL.md'
need "Agent 4: VALIDATION_SUMMARY.json"          "$WORK/basic" 'VALIDATION_SUMMARY.json'
"$PY" "$ORCH/dummy_data_pipeline_orchestrator.py" "$PROBE" -o "$WORK/dummy" --agent5 --agent4 > "$WORK/dummy.log" 2>&1
grep -q 'Pipeline Result' "$WORK/dummy.log" && ok "dummy-data orchestrator ran to completion" || { bad "dummy-data orchestrator did not finish"; tail -8 "$WORK/dummy.log" | sed 's/^/        /'; }
need "Agent 2: generated data workbook (.xlsx)"  "$WORK/dummy" '*_dummy_data.xlsx'
need "Agent 4: VALIDATION_SUMMARY.json (with data)" "$WORK/dummy" 'VALIDATION_SUMMARY.json'

echo "== 4. Hyper-to-CSV helper (needs tableauhyperapi) =="
if "$PY" -c "import tableauhyperapi" 2>/dev/null; then
  # run from the temp folder: the Hyper engine writes a hyperd.log into the current directory
  ( cd "$WORK" && "$PY" - "$WORK" <<'PYEOF'
import sys, csv, subprocess, os, datetime
from tableauhyperapi import HyperProcess, Telemetry, Connection, CreateMode, TableDefinition, SqlType, TableName, Inserter, NULLABLE
work = sys.argv[1]; hy = os.path.join(work, "t.hyper"); out = os.path.join(work, "t.csv")
with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as h:
    with Connection(h.endpoint, hy, CreateMode.CREATE_AND_REPLACE) as c:
        c.catalog.create_schema("Extract")
        t = TableDefinition(TableName("Extract", "Extract"), [
            TableDefinition.Column("Name, x", SqlType.text(), NULLABLE), TableDefinition.Column("N", SqlType.big_int(), NULLABLE)])
        c.catalog.create_table(t)
        with Inserter(c, t) as i:
            i.add_rows([('a "q", b', 1), (None, None)]); i.execute()
tool = os.path.join(os.environ["PIPE"], "tools", "data-prep", "hyper_to_csv.py")
r = subprocess.run([sys.executable, tool, hy, "-o", out], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
rows = list(csv.reader(open(out, newline="", encoding="utf-8")))
assert rows == [["Name, x", "N"], ['a "q", b', "1"], ["", ""]], rows
PYEOF
  )
  [ $? -eq 0 ] && ok "hyper_to_csv.py round-trips quotes, commas and NULLs" || bad "hyper_to_csv.py"
else
  skip "tableauhyperapi not installed"
fi

echo
if [ $FAILS -eq 0 ]; then echo "SMOKE TEST PASSED"; exit 0; else echo "SMOKE TEST FAILED ($FAILS problem(s))"; exit 1; fi
