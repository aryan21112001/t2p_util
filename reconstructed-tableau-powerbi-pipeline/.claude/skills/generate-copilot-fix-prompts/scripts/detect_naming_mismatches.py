#!/usr/bin/env python3
"""
Detects a specific, recurring root cause among "unresolved field reference" residue items: a
formula on table T references [some_prefix_X] where NO such column exists anywhere in the
workbook, but table T (or occasionally a sibling table) already has a real, connected column
named [X] once a conventional prefix/suffix is stripped.

This is NOT a translation gap and not random data corruption -- it is the signature of a real,
mechanical authoring mistake already present in the source Tableau workbook: a calc formula that
was copy-pasted from a worksheet/table using one naming convention onto a table using a
DIFFERENT one, without updating the field reference. Confirmed on a real corpus workbook (AP
Product Portfolio Management Dashboard): 12 calcs on `tvaf_vcpp_market_landscape_apac` reference
`[tokn_pv_cy]`, `[tokn_active_cards_cy]`, etc., none of which exist ANYWHERE in the workbook's
connection schema -- but that same table has real, connected columns named `[pv_cy]`,
`[active_cards_cy]` etc. (the `tokn_` prefix legitimately exists only on a DIFFERENT table,
`tvaf_vcpp_portfolio_view_apac`, as `tokn_count_cy` etc.). Verified directly against the raw
.twb's own <metadata-record> connection schema, not guessed at.

This script is a MECHANICAL, DETERMINISTIC string-comparison over data build-tableau-pbip already
extracted (ir.json's own column lists) -- no LLM call, no fabricated guess. It surfaces every
match as a ranked HYPOTHESIS for a human to verify in Tableau Desktop before touching the source
formula, exactly like report_dangling_references.py's own cascade-closure report is a mechanical
tabulation, not an automated fix. Nothing here writes to TMDL or COPILOT_FIX_PROMPTS.md; it is a
new, standalone report only.

Usage:
  python3 detect_naming_mismatches.py <build output: project-or-parent-folder> [--out folder]
"""
import os
import re
import sys
import json
import argparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
from generate_copilot_prompts import find_residue_workbooks, drop_already_emitted  # noqa: E402

# tmdl.py's own decline message for a field that exists nowhere in the workbook -- see
# report_dangling_references.py's identical constant and comment for why this exact suffix, not
# a looser match, is required (it is the one tmdl.py emits ONLY after searching every declined
# calc in the workbook and finding no cascade root, i.e. genuinely missing, not a pass-through).
_DANGLING_RE = re.compile(
    r"unresolved field reference \[([^\]]+)\] -- "
    r"no field by that name is defined anywhere in the workbook"
)

# Conventional prefixes/suffixes real Tableau workbooks are observed to vary between a formula's
# OWN table and the table the formula's field name actually belongs on (copy-paste-without-
# repointing being the single most common cause). Ordered longest-first so e.g. 'tokn_' is tried
# before a shorter, accidental sub-match. This list is deliberately a small, named, auditable set
# -- not a fuzzy edit-distance search -- so a hit is always explainable as "field name minus this
# exact literal affix", never a coincidental near-match.
_PREFIXES = ["tokn_", "stg_", "raw_", "src_", "tmp_", "old_", "orig_", "bak_"]
_SUFFIXES = ["_tokn", "_stg", "_raw", "_src", "_tmp", "_old", "_orig", "_bak", "_v1", "_v2", "_copy"]


def _variants(name):
    """Every plausible 'strip one conventional affix' rewrite of `name`, paired with which
    affix was stripped (for reporting). Case-insensitive on the affix only, not on the
    remaining field name (Tableau field names are themselves case-sensitive identifiers)."""
    low = name.lower()
    out = []
    for p in _PREFIXES:
        if low.startswith(p):
            out.append((name[len(p):], f"prefix '{p}'"))
    for s in _SUFFIXES:
        if low.endswith(s):
            out.append((name[:len(name) - len(s)], f"suffix '{s}'"))
    return out


def _all_columns_by_table(ir):
    """{table_caption_or_name: set of real column tableau_names, bracket-stripped}."""
    by_table = {}
    for t in ir.get("tables", []):
        key = t.get("caption") or t.get("tableau_name")
        names = set()
        for col in t.get("columns") or []:
            raw = col.get("tableau_name") or col.get("name") or ""
            names.add(raw.strip("[]"))
        by_table[key] = names
    return by_table


def find_naming_mismatches(ir, calc_residue):
    """For every dangling reference, check whether stripping a conventional affix off the
    missing field's name lands on a REAL column -- on the SAME table the calc lives on first
    (the copy-paste-onto-wrong-table case this was built from), then any other table (a rename
    that only partially propagated). Returns a list of hypothesis rows, ranked same-table first.
    """
    by_table = _all_columns_by_table(ir)
    rows = []
    seen = set()
    for item in calc_residue:
        m = _DANGLING_RE.search(item.get("reason") or "")
        if not m:
            continue
        missing = m.group(1)
        table = item.get("table")
        key = (missing, table)
        if key in seen:
            continue
        seen.add(key)

        same_table_hits = []
        other_table_hits = []
        for candidate, affix_desc in _variants(missing):
            if table in by_table and candidate in by_table[table]:
                same_table_hits.append((candidate, affix_desc, table))
            for other_table, cols in by_table.items():
                if other_table == table:
                    continue
                if candidate in cols:
                    other_table_hits.append((candidate, affix_desc, other_table))

        if not (same_table_hits or other_table_hits):
            continue

        rows.append({
            "missing_field": missing,
            "table": table,
            "subject": item.get("subject"),
            "formula": item.get("formula"),
            "same_table_hits": same_table_hits,
            "other_table_hits": other_table_hits,
        })

    # Same-table hits are the strong, high-confidence signal (this is the exact shape the
    # tokn_ ground-truth case was) -- rank those first.
    rows.sort(key=lambda r: (0 if r["same_table_hits"] else 1, r["missing_field"]))
    return rows


def _md_escape(s):
    return (s or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(workbook_name, rows):
    if not rows:
        return (
            f"# Naming-Mismatch Hypotheses — {workbook_name}\n\n"
            f"None found. Every dangling field reference in this workbook's residue has no "
            f"same-workbook column matching it under a stripped conventional prefix/suffix -- "
            f"either genuinely missing data, or a naming convention this script does not yet "
            f"know about (see `_PREFIXES`/`_SUFFIXES` in `detect_naming_mismatches.py`).\n"
        )

    strong = [r for r in rows if r["same_table_hits"]]
    weak = [r for r in rows if not r["same_table_hits"]]

    out = [f"# Naming-Mismatch Hypotheses — {workbook_name}\n"]
    out.append(
        f"{len(rows)} dangling field reference(s) where stripping a conventional prefix/suffix "
        f"(see `_PREFIXES`/`_SUFFIXES`) from the missing field's name lands on a column that "
        f"REALLY EXISTS elsewhere in this same workbook. This is a MECHANICAL string match, not "
        f"a verified fact for this specific workbook's semantics -- confirm each one in Tableau "
        f"Desktop before editing the source formula. **If confirmed, fix it in Tableau, not in "
        f"Power BI** -- this is a pre-existing bug in the source calc, present regardless of "
        f"migration.\n"
    )

    if strong:
        out.append(f"## {len(strong)} high-confidence: matching column on the SAME table\n")
        out.append(
            "The missing field's own table already has a column matching it once the affix is "
            "stripped -- the single strongest signal this is a copy-pasted formula never "
            "repointed to its own table's real column names.\n"
        )
        out.append("| Table | Missing Field | Calc | Real Column (same table) | Affix Stripped |")
        out.append("|---|---|---|---|---|")
        for r in strong:
            for candidate, affix_desc, _t in r["same_table_hits"]:
                out.append(
                    f"| `{r['table']}` | `[{r['missing_field']}]` | `{_md_escape(r['subject'])}` "
                    f"| `[{candidate}]` | {affix_desc} |"
                )
        out.append("")

    if weak:
        out.append(f"## {len(weak)} lower-confidence: matching column on a DIFFERENT table only\n")
        out.append(
            "No match on the field's own table, but a same-named-once-stripped column exists on "
            "another table in this workbook -- could be the same bug with the formula on the "
            "wrong table entirely, or could be unrelated (a different field that happens to "
            "share a stem name). Needs more scrutiny than the same-table case above.\n"
        )
        out.append("| Table | Missing Field | Calc | Real Column (other table) | Found On | Affix Stripped |")
        out.append("|---|---|---|---|---|---|")
        for r in weak:
            for candidate, affix_desc, other_table in r["other_table_hits"]:
                out.append(
                    f"| `{r['table']}` | `[{r['missing_field']}]` | `{_md_escape(r['subject'])}` "
                    f"| `[{candidate}]` | `{other_table}` | {affix_desc} |"
                )
        out.append("")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(
        description="Detect a naming-mismatch signature (conventional prefix/suffix stripped "
                    "matches a real column) among dangling field references.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single workbook build folder (writes NAMING_MISMATCHES.md in place)
  %(prog)s /path/to/output/dummy_pbip/MyDashboard

  # Parent directory of many workbook folders
  %(prog)s /path/to/output/dummy_pbip

  # Redirect output elsewhere
  %(prog)s /path/to/output/dummy_pbip --out /path/to/reports
""",
    )
    parser.add_argument("input", help="Workbook build folder, or parent directory of several")
    parser.add_argument("--out", help="Write NAMING_MISMATCHES.md under <out>/<Workbook>/ instead of in place")
    args = parser.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    workbooks = find_residue_workbooks(root)
    if not workbooks:
        print(f"error: no _work/residue.json found under {root}", file=sys.stderr)
        sys.exit(2)

    any_found = False
    for wb_dir, residue_path in workbooks:
        workbook_name = os.path.basename(wb_dir)
        with open(residue_path, encoding="utf-8") as fh:
            residue = json.load(fh)
        residue, _n_already_fixed = drop_already_emitted(wb_dir, residue)

        ir_path = os.path.join(wb_dir, "_work", "ir.json")
        with open(ir_path, encoding="utf-8") as fh:
            ir = json.load(fh)

        rows = find_naming_mismatches(ir, residue.get("calc_residue", []))
        md = render_markdown(workbook_name, rows)

        if args.out:
            out_dir = os.path.join(os.path.abspath(args.out), workbook_name)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = wb_dir

        with open(os.path.join(out_dir, "NAMING_MISMATCHES.md"), "w", encoding="utf-8") as fh:
            fh.write(md)

        n_strong = sum(1 for r in rows if r["same_table_hits"])
        n_weak = len(rows) - n_strong
        if rows:
            any_found = True
        print(f"[ok] {workbook_name}  hypotheses={len(rows)} (strong={n_strong} weak={n_weak}) "
              f"-> {os.path.join(out_dir, 'NAMING_MISMATCHES.md')}")

    # Informational report only -- never fails the pipeline. `any_found` is tracked for a
    # future caller that might want to know whether anything was written without parsing stdout.
    del any_found
    sys.exit(0)


if __name__ == "__main__":
    main()

