#!/usr/bin/env python3
"""
Tabulates ONE specific root cause separately from everything else in residue.json: a formula
referencing a bracketed field that is defined NOWHERE in the source Tableau workbook -- not
declined for a translation gap, but because the workbook itself carries a dangling reference
(most commonly: a field got renamed/deleted in Tableau and the formula text of every calc that
referenced it by internal id was never updated).

Why this needs its own report and not just another RESIDUE.md row: `tmdl.py`'s own decline
message already distinguishes this exact shape ("unresolved field reference [X] -- no field by
that name is defined anywhere in the workbook") from a CASCADE ("depends on 'Y', which was itself
declined") -- but `effort_report.py` buckets both, and every other decline reason, into the same
flat manual_dax/manual_structural rate. That silently overstates the real fix cost: one missing
field referenced by 145 calcs is ONE fix in Tableau (re-create the field, or repoint the 145
formulas), not 145 independent 30-minute hand-authoring tasks -- and every calc that cascades off
of one of those 145 (because it in turn depends on one of them) clears automatically on rebuild,
also for free. This script surfaces exactly that: one row per missing field, with the direct
reference count and the transitive cascade closure, so a reviewer fixes the SOURCE data problem
instead of hand-authoring around it.

This is read-only over residue.json (+ ir.json/name_authority.json, the same as effort_report.py) --
it writes its own new report file, never touches COPILOT_FIX_PROMPTS.md or the real
.SemanticModel.

Usage:
  python3 report_dangling_references.py <build output: project-or-parent-folder> [--out folder]
"""
import os
import sys
import json
import re
import argparse
from collections import defaultdict

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
from generate_copilot_prompts import find_residue_workbooks, drop_already_emitted  # noqa: E402

# The exact suffix tmdl.py's _explain() appends ONLY when it searched every declined calc in the
# workbook for one captioned by this name and found none -- i.e. genuinely missing, not a cascade.
_DANGLING_RE = re.compile(
    r"unresolved field reference \[(?:[^\]]*\]\.\[)?([^\]]+)\] -- "
    r"no field by that name is defined anywhere in the workbook"
)

# tmdl.py's cascade message, either same-table ("depends on 'X', which...") or cross-table
# ("depends on 'X' in table 'Y', which..."). Captures the caption of the calc THIS item depends
# on, so a chain of cascades can be walked one hop at a time.
_DEPENDS_ON_RE = re.compile(r"depends on '(.+?)'(?: in table '.+?')?, which was itself declined")


def find_dangling_roots(calc_residue):
    """{missing field name -> [items DIRECTLY declined for referencing it]}, first-seen order."""
    roots = {}
    for item in calc_residue:
        m = _DANGLING_RE.search(item.get("reason") or "")
        if not m:
            continue
        roots.setdefault(m.group(1), []).append(item)
    return roots


def build_cascade_index(calc_residue):
    """{caption -> [items that said "depends on '<caption>'..."]} -- every calc's OWN caption is
    a potential cascade root for something else, regardless of why THAT calc was declined."""
    by_caption = defaultdict(list)
    for item in calc_residue:
        m = _DEPENDS_ON_RE.search(item.get("reason") or "")
        if m:
            by_caption[m.group(1)].append(item)
    return by_caption


def cascade_closure(direct_items, by_caption):
    """Every item transitively declined because one of `direct_items` was declined -- a calc
    depending on a calc that depends on a directly-declined one, arbitrarily deep. Returns a
    list, direct_items excluded (those are reported separately as the direct count)."""
    seen = {(it.get("table"), it.get("subject")) for it in direct_items}
    frontier = list(direct_items)
    closure = []
    while frontier:
        item = frontier.pop()
        for dep in by_caption.get(item.get("subject") or "", []):
            key = (dep.get("table"), dep.get("subject"))
            if key in seen:
                continue
            seen.add(key)
            closure.append(dep)
            frontier.append(dep)
    return closure


def analyze_workbook(wb_dir, residue):
    calc_residue = residue.get("calc_residue", [])
    roots = find_dangling_roots(calc_residue)
    by_caption = build_cascade_index(calc_residue)

    rows = []
    for field, direct in roots.items():
        recovered = cascade_closure(direct, by_caption)
        tables = sorted({it.get("table") for it in direct + recovered if it.get("table")})
        rows.append({
            "missing_field": field,
            "direct_count": len(direct),
            "cascade_recovered_count": len(recovered),
            "total_count": len(direct) + len(recovered),
            "tables": tables,
            "direct_items": direct,
            "cascade_items": recovered,
            "sample_formula": next((it.get("formula") for it in direct if it.get("formula")), None),
        })
    rows.sort(key=lambda r: -r["total_count"])
    return rows


def _md_escape(s):
    return (s or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(workbook_name, rows, n_dax_total):
    if not rows:
        return (f"# Dangling Field References — {workbook_name}\n\n"
                f"None found. Every declined calc's reason is a real translation gap or a "
                f"cascade off one -- not a reference to a field absent from the workbook "
                f"entirely.\n")

    total_direct = sum(r["direct_count"] for r in rows)
    total_recovered = sum(r["cascade_recovered_count"] for r in rows)
    total_all = total_direct + total_recovered
    pct_of_dax = round(100 * total_all / n_dax_total) if n_dax_total else 0

    out = [f"# Dangling Field References — {workbook_name}\n"]
    out.append(
        f"{len(rows)} distinct field(s) referenced in a formula but defined NOWHERE in this "
        f"workbook — not a translation gap, a broken reference already present in the source "
        f"Tableau file (most commonly: the field was renamed/deleted in Tableau and every "
        f"formula that referenced its internal id was never updated).\n"
    )
    out.append(
        f"**{total_direct} calc(s) directly reference one of these fields, and {total_recovered} "
        f"more are only declined because they depend on one of THOSE** ({total_all} total, "
        f"{pct_of_dax}% of this workbook's {n_dax_total} outstanding DAX-shaped decline(s)). "
        f"Fixing the field in Tableau and rebuilding is expected to clear most or all of this "
        f"total in one pass — it is not {total_all} independent hand-authoring tasks.\n"
    )
    out.append("| Missing Field | Direct References | Cascade-Recovered | Total | Tables Affected | Sample Formula |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| `{_md_escape(r['missing_field'])}` | {r['direct_count']} | "
            f"{r['cascade_recovered_count']} | {r['total_count']} | "
            f"{', '.join(r['tables'])} | `{_md_escape(r['sample_formula'])[:150]}` |"
        )
    out.append("")
    out.append("## Detail\n")
    for r in rows:
        out.append(f"### `{r['missing_field']}` — {r['total_count']} calc(s) total\n")
        out.append("**Directly references the missing field:**\n")
        for it in r["direct_items"]:
            out.append(f"- {it['table']} / {it['subject']}: `{_md_escape(it.get('formula'))[:200]}`")
        if r["cascade_items"]:
            out.append("\n**Cascades off one of the above (would clear once it's fixed):**\n")
            for it in r["cascade_items"]:
                out.append(f"- {it['table']} / {it['subject']}")
        out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(
        description="Tabulate dangling (undefined-anywhere) field references separately from "
                    "genuine translation gaps and their cascades.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single workbook build folder (writes DANGLING_REFERENCES.md in place)
  %(prog)s /path/to/output/dummy_pbip/MyDashboard

  # Parent directory of many workbook folders (also writes a batch summary)
  %(prog)s /path/to/output/dummy_pbip

  # Redirect output elsewhere
  %(prog)s /path/to/output/dummy_pbip --out /path/to/reports
""",
    )
    parser.add_argument("input", help="Workbook build folder, or parent directory of several")
    parser.add_argument("--out", help="Write DANGLING_REFERENCES.md under <out>/<Workbook>/ instead of in place")
    args = parser.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    workbooks = find_residue_workbooks(root)
    if not workbooks:
        print(f"error: no _work/residue.json found under {root}", file=sys.stderr)
        sys.exit(2)

    summary_rows = []
    for wb_dir, residue_path in workbooks:
        workbook_name = os.path.basename(wb_dir)
        with open(residue_path, encoding="utf-8") as fh:
            residue = json.load(fh)
        residue, _n_already_fixed = drop_already_emitted(wb_dir, residue)
        n_dax_total = len(residue.get("calc_residue", []))

        rows = analyze_workbook(wb_dir, residue)
        md = render_markdown(workbook_name, rows, n_dax_total)

        if args.out:
            out_dir = os.path.join(os.path.abspath(args.out), workbook_name)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = wb_dir

        with open(os.path.join(out_dir, "DANGLING_REFERENCES.md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        with open(os.path.join(out_dir, "dangling_references.json"), "w", encoding="utf-8") as fh:
            json.dump([{k: v for k, v in r.items() if k not in ("direct_items", "cascade_items")}
                       for r in rows], fh, indent=2)

        total = sum(r["total_count"] for r in rows)
        print(f"[ok] {workbook_name}  fields={len(rows)} direct={sum(r['direct_count'] for r in rows)} "
              f"cascade_recovered={sum(r['cascade_recovered_count'] for r in rows)} total={total} "
              f"of {n_dax_total} outstanding DAX decline(s) -> {os.path.join(out_dir, 'DANGLING_REFERENCES.md')}")
        summary_rows.append((workbook_name, rows, n_dax_total))

    if args.out and len(workbooks) > 1:
        out_root = os.path.abspath(args.out)
        lines = ["# Dangling Field References — batch summary\n",
                 "| Workbook | Distinct Missing Fields | Direct | Cascade-Recovered | Total | % of Outstanding DAX |",
                 "|---|---|---|---|---|---|"]
        for name, rows, n_dax_total in summary_rows:
            direct = sum(r["direct_count"] for r in rows)
            recovered = sum(r["cascade_recovered_count"] for r in rows)
            total = direct + recovered
            pct = round(100 * total / n_dax_total) if n_dax_total else 0
            lines.append(f"| {name} | {len(rows)} | {direct} | {recovered} | {total} | {pct}% |")
        with open(os.path.join(out_root, "DANGLING_REFERENCES_SUMMARY.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print(f"\n[ok] batch summary -> {os.path.join(out_root, 'DANGLING_REFERENCES_SUMMARY.md')}")


if __name__ == "__main__":
    main()
