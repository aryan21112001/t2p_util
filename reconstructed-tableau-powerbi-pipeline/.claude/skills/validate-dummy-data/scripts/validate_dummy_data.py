#!/usr/bin/env python3
"""
Validate a real generate-dummy-data output: does the project actually parse, lint, and match
Desktop's own ground truth, and does the Excel file each wired table was pointed at actually
match that table's own model shape?

Why this exists
----------------
generate-dummy-data wires synthetic data into a build-tableau-pbip project's empty stub
partitions. Nothing downstream of that step re-checks its own work: does the Excel file it wrote
still match the TMDL it rewrote, does the rewritten M still parse, does the project as a whole
still pass the same structural/ground-truth/lint bars build-tableau-pbip itself is held to. This
script is that check -- mostly a FORMATTER/AGGREGATOR over four already-existing checks, three of
them this pipeline already has (`tools/pbip_rules/model.py`'s parser, `tools/pbip_lint.py
--schemas`, `build-tableau-pbip/tests/check_against_ground_truth.py`), the same relationship
`validate-tableau-pbip-mapping` has to `build-tableau-pbip`. Exactly one check here is genuinely
new: cross-referencing the real written `.xlsx` file's header row against the M that claims to
read it, plus a path-sanity check aimed squarely at catching a regression of a real bug this
project hit once already -- every wired table across a whole corpus pointed at an identical,
made-up placeholder path (`C:/Users/analyst/Desktop/PBIP/DummyData.xlsx`) that matched neither
the real file's name nor its location.

What it checks, per project
----------------------------
1. TMDL structural parse -- `tools/pbip_rules/model.py`'s `load_project()`. Must report zero
   `parse_errors`.
2. `tools/pbip_lint.py --schemas` -- the same lint every build-tableau-pbip output is held to.
   Pre-existing stray-file NOTES (e.g. `.platform`) are informational, not failures; only new
   FAILures count against this check.
3. Ground truth -- `build-tableau-pbip/tests/check_against_ground_truth.py`'s `rules()` against
   the project's own `*.SemanticModel`. Must report 0 rules failed.
4. Excel-vs-M cross-check, per table whose `source =` M was rewired to `Excel.Workbook(...)`:
   the REAL `.xlsx` file this project's own generate-dummy-data output actually wrote (resolved
   by that skill's own naming convention, `<Workbook>_dummy_data.xlsx` next to the `.pbip` --
   never by trusting the Desktop-machine path string inside the M, which will not resolve on
   this machine) is opened, its sheet (named in `Source{[Item="...",Kind="Sheet"]}`) is read, and
   its header row must EXACTLY match (same names, same order) the column list inside that same
   table's own `Table.TransformColumnTypes(...)` call.
5. Path sanity -- flags any wired table whose `File.Contents(...)` argument's filename component
   does not correspond to a `.xlsx` file that actually exists in the project's own folder. This
   is the exact shape of the bug this skill exists to catch a regression of (see above); a
   hardcoded placeholder path is caught here even after the generator itself is fixed, because
   this check never trusts the generator -- it verifies the claim against the real file on disk.

Run
---
    python3 validate_dummy_data.py <generate-dummy-data output: project-or-parent-folder>

Writes `VALIDATE_DUMMY_DATA_REPORT.md` into EACH project folder found (mirrors
`generate-dummy-data`'s own `DUMMY_DATA_REPORT.md` placement -- in the project itself, not a
separate `--out` tree, since this is a check ON that output, not a new artifact with its own
lifecycle).

Exit code is non-zero if any project fails any check (matching `tools/pbip_lint.py`'s own
convention). Checks 1-3 need no third-party package; check 4 needs `openpyxl` (already a
dependency of `generate-dummy-data` itself).
"""
import re
import sys
import shutil
import argparse
from pathlib import Path

try:
    import openpyxl
except ImportError:                                                        # pragma: no cover
    openpyxl = None

# ---------------------------------------------------------------- wire up this repo's own tools
#
# This script lives at <repo>/.claude/skills/validate-dummy-data/scripts/validate_dummy_data.py.
# Four levels up is the repo root; from there the three things this script reuses rather than
# reimplements are reachable by path, exactly the way check_against_ground_truth.py's own `GT`
# constant is computed relative to ITS file, not assumed to be on sys.path already.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[4]
_TOOLS_DIR = _REPO_ROOT / "tools"
_GDD_DIR = _REPO_ROOT / ".claude" / "skills" / "generate-dummy-data" / "scripts"
_CAGT_DIR = _REPO_ROOT / ".claude" / "skills" / "build-tableau-pbip" / "tests"

for _p in (_TOOLS_DIR, _GDD_DIR, _CAGT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pbip_rules import model as pbip_model          # noqa: E402  (path set up above)
from pbip_rules import lint as pbip_lint            # noqa: E402
import check_against_ground_truth as cagt            # noqa: E402


# ---------------------------------------------------------------- project discovery

def find_projects(target):
    """A project folder is one holding a .pbip (or, failing that, a .Report folder).

    Mirrors tools/pbip_lint.py's own find_projects() (and generate-dummy-data's copy of the same
    convention) exactly, so `<input>` behaves identically across every script in this pipeline.
    """
    target = Path(target)
    if list(target.glob("*.pbip")) or list(target.glob("*.Report")):
        return [target]
    found = sorted({p.parent for p in target.glob("*/*.pbip")})
    if found:
        return found
    return sorted({p.parent for p in target.glob("*/*.Report")})


# ---------------------------------------------------------------- check 4/5 helpers

# Matches generate_dummy_data.py's build_excel_m() output exactly -- see that module's own
# regression test for the shape being parsed here. Applied against `Partition.source`, which
# tools/pbip_rules/model.py already normalises to one un-indented line per statement, so no tab
# handling is needed here (unlike generate_dummy_data.py's own regexes, which work on raw TMDL
# text before that normalisation exists).
_FILE_CONTENTS_RE = re.compile(r'File\.Contents\(\s*"((?:[^"]|"")*)"\s*\)')
_SHEET_ITEM_RE = re.compile(r'\[Item\s*=\s*"((?:[^"]|"")*)"\s*,\s*Kind\s*=\s*"Sheet"\]')
_TRANSFORM_BLOCK_RE = re.compile(r'Table\.TransformColumnTypes\(\s*Sheet\s*,\s*\{(.*)\}\s*\)',
                                  re.DOTALL)
_COL_PAIR_RE = re.compile(r'\{\s*"((?:[^"]|"")*)"\s*,\s*[^{}]+?\}')


def parse_excel_wired(source_text):
    """If `source_text` is generate-dummy-data's Excel.Workbook(...) rewrite shape, return
    {"desktop_path", "sheet", "columns"} (columns in the exact order TransformColumnTypes lists
    them). Returns None if the shape doesn't parse -- declined rather than guessed at, the same
    fail-closed discipline the rest of this pipeline holds to.
    """
    if "Excel.Workbook(File.Contents(" not in source_text:
        return None
    m_path = _FILE_CONTENTS_RE.search(source_text)
    m_sheet = _SHEET_ITEM_RE.search(source_text)
    m_cols = _TRANSFORM_BLOCK_RE.search(source_text)
    if not (m_path and m_sheet and m_cols):
        return None
    columns = [name.replace('""', '"') for name in _COL_PAIR_RE.findall(m_cols.group(1))]
    if not columns:
        return None
    return {
        "desktop_path": m_path.group(1).replace('""', '"'),
        "sheet": m_sheet.group(1).replace('""', '"'),
        "columns": columns,
    }


def resolve_real_excel(project_root):
    """The real .xlsx file THIS project's own generate-dummy-data output actually wrote --
    `<project folder name>_dummy_data.xlsx`, generate_dummy_data.py's own naming convention
    (`process_project()`'s `excel_path = out_dir / f"{name}_dummy_data.xlsx"`). Never the path
    string inside the M, which is a Desktop-machine path and will not resolve here. Falls back to
    a lone `*_dummy_data.xlsx` if the exact expected name isn't present, but never guesses among
    several -- ambiguity is reported, not resolved silently.
    """
    expected = project_root / f"{project_root.name}_dummy_data.xlsx"
    if expected.is_file():
        return expected
    candidates = list(project_root.glob("*_dummy_data.xlsx"))
    return candidates[0] if len(candidates) == 1 else None


def read_excel_header(xlsx_path, sheet_name):
    """The header row (row 1) of `sheet_name` in `xlsx_path`, trailing blanks trimmed. Returns
    (header, None) or (None, error-detail)."""
    if openpyxl is None:
        return None, "openpyxl is not installed -- cannot open the Excel file"
    try:
        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    except Exception as exc:                                            # noqa: BLE001
        return None, f"could not open {xlsx_path.name}: {exc}"
    if sheet_name not in wb.sheetnames:
        return None, (f"sheet {sheet_name!r} not found in {xlsx_path.name} "
                       f"(sheets present: {wb.sheetnames})")
    ws = wb[sheet_name]
    row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    header = list(row)
    while header and header[-1] is None:
        header.pop()
    return header, None


def check_excel_wired_tables(project_root, proj):
    """Checks 4 (Excel-vs-M header match) and 5 (path sanity) for every Excel-wired table.

    Returns a list of per-table dicts: {table, sheet, m_columns, desktop_path, path_check,
    header_check} where each `*_check` is (ok: bool, detail: str). A table whose M carries
    `Excel.Workbook(File.Contents(` but doesn't parse into the exact rewrite shape gets an
    `"error"` entry instead -- declined, not guessed at.
    """
    real_xlsx = resolve_real_excel(project_root)
    results = []
    for table in proj.tables.values():
        for partition in table.partitions:
            source = partition.source or ""
            if "Excel.Workbook(File.Contents(" not in source:
                continue
            parsed = parse_excel_wired(source)
            if parsed is None:
                results.append({
                    "table": table.name,
                    "error": "M carries Excel.Workbook(File.Contents(...)) but the rewrite "
                             "shape (path / sheet-item / Table.TransformColumnTypes) did not "
                             "fully parse -- declined rather than guessed at.",
                })
                continue

            entry = {"table": table.name, "sheet": parsed["sheet"],
                      "m_columns": parsed["columns"], "desktop_path": parsed["desktop_path"]}

            # ---- check 5: path sanity -- the exact Task-1 placeholder-path bug class
            filename = re.split(r"[\\/]", parsed["desktop_path"])[-1]
            candidate = project_root / filename
            if candidate.is_file():
                entry["path_check"] = (True, f"'{filename}' exists in this project's own folder")
            else:
                entry["path_check"] = (False,
                    f"File.Contents(...) names '{filename}', which does not exist anywhere in "
                    f"this project's own folder ('{project_root.name}') -- this is the shape of "
                    f"a hardcoded/example placeholder path, not a real file this project's own "
                    f"generate-dummy-data output produced")

            # ---- check 4: Excel-vs-M cross-check -- always against the REAL file (never the
            # Desktop-machine path string in the M, which cannot resolve on this machine)
            if real_xlsx is None:
                entry["header_check"] = (False,
                    "no '<Workbook>_dummy_data.xlsx' file found in this project's own folder to "
                    "check the header against")
            else:
                header, err = read_excel_header(real_xlsx, parsed["sheet"])
                if err:
                    entry["header_check"] = (False, err)
                elif header == parsed["columns"]:
                    entry["header_check"] = (True,
                        f"header row of sheet '{parsed['sheet']}' in '{real_xlsx.name}' matches "
                        f"the {len(header)} column(s) in Table.TransformColumnTypes exactly, "
                        f"same order")
                else:
                    entry["header_check"] = (False,
                        f"header row of sheet '{parsed['sheet']}' in '{real_xlsx.name}' is "
                        f"{header!r} but Table.TransformColumnTypes lists {parsed['columns']!r} "
                        f"-- mismatch")
            results.append(entry)
    return results


# ---------------------------------------------------------------- checks 1-3 (existing tools)

def check_tmdl_parse(project_root):
    proj = pbip_model.load_project(project_root)
    return proj, (not proj.parse_errors), list(proj.parse_errors)


def check_lint(project_root):
    """tools/pbip_lint.py's own lint_project(), scoped to this one project."""
    f = pbip_lint.Findings()
    pbip_lint.lint_project(project_root, f, with_schemas=True)
    return (not f.failures), list(f.failures), list(f.notes)


def check_ground_truth(project_root):
    sm_dirs = sorted(project_root.glob("*.SemanticModel"))
    if not sm_dirs:
        return False, ["no *.SemanticModel folder found"]
    results = cagt.rules(str(sm_dirs[0]))
    failed = [(name, detail) for name, ok, detail in results if not ok]
    return (not failed), failed


# ---------------------------------------------------------------- per-project orchestration

def validate_project(project_root):
    """Run all five checks against one project. Returns a report dict; never raises on a
    check's own findings -- those are data, not exceptions."""
    name = project_root.name
    proj, parse_ok, parse_errors = check_tmdl_parse(project_root)
    lint_ok, lint_failures, lint_notes = check_lint(project_root)
    gt_ok, gt_failed = check_ground_truth(project_root)
    excel_tables = check_excel_wired_tables(project_root, proj)

    excel_ok = all(
        "error" not in e and e["path_check"][0] and e["header_check"][0]
        for e in excel_tables
    )

    return {
        "project": name,
        "tmdl_parse": {"ok": parse_ok, "errors": parse_errors},
        "lint": {"ok": lint_ok, "failures": lint_failures, "notes": lint_notes},
        "ground_truth": {"ok": gt_ok, "failed": gt_failed},
        "excel_tables": excel_tables,
        "excel_ok": excel_ok,
        "ok": parse_ok and lint_ok and gt_ok and excel_ok,
    }


# ---------------------------------------------------------------- report

def write_report(project_root, report):
    lines = [
        f"# Validate Dummy Data Report -- {report['project']}",
        "",
        f"**Overall: {'PASS' if report['ok'] else 'FAIL'}**",
        "",
        "Five checks: TMDL structural parse, `tools/pbip_lint.py --schemas`, ground truth "
        "(`check_against_ground_truth.py`), and -- the two genuinely new checks -- an "
        "Excel-vs-M header cross-check and a path-sanity check per Excel-wired table.",
        "",
        "## 1. TMDL structural parse (`tools/pbip_rules/model.py` `load_project()`)",
        "",
    ]
    tp = report["tmdl_parse"]
    lines.append(f"{'PASS' if tp['ok'] else 'FAIL'} -- {len(tp['errors'])} parse_error(s)")
    if tp["errors"]:
        lines += ["", "| Error |", "|---|"]
        lines += [f"| {e} |" for e in tp["errors"]]

    lint = report["lint"]
    lines += ["", "## 2. `tools/pbip_lint.py --schemas`", ""]
    lines.append(
        f"{'PASS' if lint['ok'] else 'FAIL'} -- {len(lint['failures'])} failure(s), "
        f"{len(lint['notes'])} note(s) (pre-existing stray-file/schema-fetch notes are "
        f"informational, not failures)"
    )
    if lint["failures"]:
        lines += ["", "| Failure |", "|---|"]
        lines += [f"| {f} |" for f in lint["failures"]]
    if lint["notes"]:
        lines += ["", "<details><summary>Notes</summary>", ""]
        lines += [f"- {n}" for n in lint["notes"]]
        lines += ["", "</details>"]

    gt = report["ground_truth"]
    lines += ["", "## 3. Ground truth (`check_against_ground_truth.py`)", ""]
    lines.append(f"{'PASS' if gt['ok'] else 'FAIL'} -- {len(gt['failed'])} rule(s) failed")
    if gt["failed"]:
        lines += ["", "| Rule | Detail |", "|---|---|"]
        lines += [f"| {n} | {d} |" for n, d in gt["failed"]]

    lines += ["", "## 4/5. Excel-vs-M header match, and path sanity, per Excel-wired table", ""]
    tables = report["excel_tables"]
    if not tables:
        lines.append(
            "(no table in this project was wired to `Excel.Workbook(...)` -- nothing to check "
            "here; every table is either an unwired stub, a Param table, or on a native "
            "connector)"
        )
    else:
        lines.append("| Table | Sheet | Path sanity | Header match |")
        lines.append("|---|---|---|---|")
        for e in tables:
            if "error" in e:
                lines.append(f"| {e['table']} | -- | FAIL | FAIL -- {e['error']} |")
                continue
            p_ok, p_detail = e["path_check"]
            h_ok, h_detail = e["header_check"]
            lines.append(
                f"| {e['table']} | {e['sheet']} | "
                f"{'PASS' if p_ok else 'FAIL'} -- {p_detail} | "
                f"{'PASS' if h_ok else 'FAIL'} -- {h_detail} |"
            )
    lines.append("")

    report_path = project_root / "VALIDATE_DUMMY_DATA_REPORT.md"
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return report_path


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Validate a generate-dummy-data output: structural parse, lint, ground "
                     "truth, and an Excel-vs-M header/path cross-check per wired table.")
    ap.add_argument("input", help="a generate-dummy-data project folder, or a parent folder "
                                   "holding several such projects")
    ap.add_argument("--quiet", action="store_true", help="only print failing projects")
    a = ap.parse_args(argv)

    target = Path(a.input)
    if not target.exists():
        print(f"[fatal] no such path: {target}", file=sys.stderr)
        return 2
    projects = find_projects(target)
    if not projects:
        print(f"[fatal] no PBIP projects found under {target}", file=sys.stderr)
        return 2

    total_fail = 0
    for project_root in projects:
        report = validate_project(project_root)
        path = write_report(project_root, report)
        if report["ok"]:
            if not a.quiet:
                print(f"[ok]   {report['project']} -- clean -> {path}")
        else:
            total_fail += 1
            print(f"[FAIL] {report['project']} -> {path}")
            if not report["tmdl_parse"]["ok"]:
                print(f"         tmdl_parse: {len(report['tmdl_parse']['errors'])} parse_error(s)")
            if not report["lint"]["ok"]:
                print(f"         lint: {len(report['lint']['failures'])} failure(s)")
            if not report["ground_truth"]["ok"]:
                print(f"         ground_truth: {len(report['ground_truth']['failed'])} rule(s) failed")
            if not report["excel_ok"]:
                for e in report["excel_tables"]:
                    if "error" in e or not e["path_check"][0] or not e["header_check"][0]:
                        print(f"         excel_table '{e['table']}': "
                              f"{e.get('error') or ''}"
                              f"{'' if 'error' in e else ('' if e['path_check'][0] else e['path_check'][1])}"
                              f"{'' if 'error' in e else ('' if e['header_check'][0] else ' | ' + e['header_check'][1])}")

    print(f"\n{len(projects) - total_fail}/{len(projects)} project(s) clean.")
    return 1 if total_fail else 0


# ---------------------------------------------------------------- self-test

def _build_base_fixture(root):
    """A real, Desktop-authored project (docs/examples/GroundTruth) plus one added table wired
    to Excel.Workbook(...) via generate_dummy_data.py's OWN build_excel_m()/write_excel() -- so
    the self-test exercises this script's checks against the same M/xlsx shapes the real
    generator produces, not a hand-rolled approximation of them. Returns the project folder.
    """
    import generate_dummy_data as gdd

    proj = root / "GroundTruth"
    proj.mkdir(parents=True)
    for item in ("GroundTruth.pbip", "GroundTruth.SemanticModel", "GroundTruth.Report"):
        src = _REPO_ROOT / "docs" / "examples" / item
        dst = proj / item
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    columns = [("Foo", "text"), ("Bar", "Int64.Type")]
    excel_path = proj / "GroundTruth_dummy_data.xlsx"
    body = gdd.build_excel_m(str(excel_path.resolve()), "DummyWired", columns)
    table_text = (
        "table DummyWired\n"
        "\tlineageTag: 11111111-1111-1111-1111-111111111111\n"
        "\n"
        "\tcolumn Foo\n"
        "\t\tdataType: string\n"
        "\t\tlineageTag: 22222222-2222-2222-2222-222222222222\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: Foo\n"
        "\n"
        "\t\tannotation SummarizationSetBy = Automatic\n"
        "\n"
        "\tcolumn Bar\n"
        "\t\tdataType: int64\n"
        "\t\tlineageTag: 33333333-3333-3333-3333-333333333333\n"
        "\t\tsummarizeBy: sum\n"
        "\t\tsourceColumn: Bar\n"
        "\n"
        "\t\tannotation SummarizationSetBy = Automatic\n"
        "\n"
        "\tpartition DummyWired = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n" + body +
        "\n\tannotation PBI_ResultType = Table\n"
    )
    tables_dir = proj / "GroundTruth.SemanticModel" / "definition" / "tables"
    (tables_dir / "DummyWired.tmdl").write_text(table_text, encoding="utf-8")

    model_path = proj / "GroundTruth.SemanticModel" / "definition" / "model.tmdl"
    model_text = model_path.read_text(encoding="utf-8")
    model_text = model_text.replace(
        'annotation PBI_QueryOrder = ["Sales Data"]',
        'annotation PBI_QueryOrder = ["Sales Data", "DummyWired"]',
    ).replace(
        "ref table 'Sales Data'\n",
        "ref table 'Sales Data'\n\nref table DummyWired\n",
    )
    model_path.write_text(model_text, encoding="utf-8")

    gdd.write_excel(excel_path, [("DummyWired", columns, gdd.synthetic_rows(columns, 3))])
    return proj


def _self_test():
    import tempfile

    # ---- parse_excel_wired: the exact shape build_excel_m() produces, normalised the way
    # tools/pbip_rules/model.py's own Partition.source normalises it (one statement per line,
    # no leading tabs -- see parse_tmdl()'s `block.append(nxt.strip())`).
    normalised = (
        'let\n'
        'Source = Excel.Workbook(File.Contents("/tmp/x/Foo_dummy_data.xlsx"), true),\n'
        'Sheet = Source{[Item="Sheet1",Kind="Sheet"]}[Data],\n'
        'Typed = Table.TransformColumnTypes(Sheet, {{"Acct Prod Id", type text}, '
        '{"Cnt", Int64.Type}})\n'
        'in\n'
        'Typed'
    )
    parsed = parse_excel_wired(normalised)
    assert parsed is not None
    assert parsed["desktop_path"] == "/tmp/x/Foo_dummy_data.xlsx", parsed
    assert parsed["sheet"] == "Sheet1", parsed
    assert parsed["columns"] == ["Acct Prod Id", "Cnt"], parsed

    # ---- parse_excel_wired: a stub/param/native shape (no Excel.Workbook at all) is None ----
    assert parse_excel_wired("let\nSource = Table.FromRows({}, type table [Foo = text])\nin\n"
                              "Source") is None
    assert parse_excel_wired("let\nSource = Table.FromRows({{\"A\"}}, type table [Value = text])"
                              "\nin\nSource") is None

    # ---- parse_excel_wired: Excel.Workbook present but the rewrite shape doesn't fully parse
    # (no Table.TransformColumnTypes at all) -- declined, not guessed at ----
    assert parse_excel_wired('let\nSource = Excel.Workbook(File.Contents("x.xlsx"), true)\nin\n'
                              'Source') is None

    if openpyxl is None:
        print("validate_dummy_data.py: self-test OK (openpyxl absent -- skipped the xlsx/"
              "fixture-based scenarios)")
        return 0

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # ============================================================ SCENARIO 1: clean project
        clean_root = root / "clean"
        clean_root.mkdir()
        clean_proj = _build_base_fixture(clean_root)
        report = validate_project(clean_proj)
        assert report["tmdl_parse"]["ok"], report["tmdl_parse"]
        assert report["ground_truth"]["ok"], report["ground_truth"]
        assert report["lint"]["ok"], report["lint"]["failures"]
        assert len(report["excel_tables"]) == 1, report["excel_tables"]
        assert report["excel_tables"][0]["path_check"][0], report["excel_tables"][0]
        assert report["excel_tables"][0]["header_check"][0], report["excel_tables"][0]
        assert report["excel_ok"], report["excel_tables"]
        assert report["ok"], report
        report_path = write_report(clean_proj, report)
        assert report_path.is_file()
        assert "**Overall: PASS**" in report_path.read_text(encoding="utf-8")

        # =================================================== SCENARIO 2: mismatched Excel header
        mismatch_root = root / "mismatch"
        mismatch_root.mkdir()
        mismatch_proj = _build_base_fixture(mismatch_root)
        # Rewrite the real .xlsx's header row -- the TMDL/M is untouched, so this is EXACTLY a
        # "the Excel file no longer matches the model" scenario, not a parse-shape difference.
        xlsx_path = mismatch_proj / "GroundTruth_dummy_data.xlsx"
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb["DummyWired"]
        ws["A1"] = "Foo Renamed"          # header no longer matches Table.TransformColumnTypes
        wb.save(xlsx_path)

        report2 = validate_project(mismatch_proj)
        assert not report2["excel_ok"], report2["excel_tables"]
        assert not report2["ok"], report2
        entry = report2["excel_tables"][0]
        assert not entry["header_check"][0], entry
        assert "mismatch" in entry["header_check"][1], entry
        assert "Foo Renamed" in entry["header_check"][1], entry
        # Path sanity is UNAFFECTED by an xlsx-content-only edit -- still passes; the two checks
        # are independent by design.
        assert entry["path_check"][0], entry
        # Everything else (parse/lint/ground-truth) is untouched by this edit.
        assert report2["tmdl_parse"]["ok"] and report2["ground_truth"]["ok"]

        # ================================================ SCENARIO 3: Task-1 placeholder path
        placeholder_root = root / "placeholder"
        placeholder_root.mkdir()
        placeholder_proj = _build_base_fixture(placeholder_root)
        table_path = (placeholder_proj / "GroundTruth.SemanticModel" / "definition" / "tables"
                      / "DummyWired.tmdl")
        text = table_path.read_text(encoding="utf-8")
        # The exact real bug shape: a generic placeholder unrelated to any file this project
        # actually wrote, identical to what a different-workbook run would also produce.
        placeholder = "C:/Users/analyst/Desktop/PBIP/DummyData.xlsx"
        assert "GroundTruth_dummy_data.xlsx" in text, "fixture must start from the real path"
        new_text = re.sub(r'File\.Contents\("[^"]*"\)', f'File.Contents("{placeholder}")', text)
        assert new_text != text
        table_path.write_text(new_text, encoding="utf-8")

        report3 = validate_project(placeholder_proj)
        assert not report3["excel_ok"], report3["excel_tables"]
        assert not report3["ok"], report3
        entry3 = report3["excel_tables"][0]
        assert not entry3["path_check"][0], entry3
        assert "DummyData.xlsx" in entry3["path_check"][1], entry3
        assert "placeholder" in entry3["path_check"][1], entry3
        # The header check is UNAFFECTED -- it never trusts the M's path string in the first
        # place, only the real file resolved by this project's own naming convention. This is
        # the whole point: check 4 and check 5 catch different failure modes independently, and
        # neither masks the other.
        assert entry3["header_check"][0], entry3
        assert report3["tmdl_parse"]["ok"] and report3["ground_truth"]["ok"]

    print("validate_dummy_data.py: self-test OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.exit(_self_test())
    sys.exit(main())
