#!/usr/bin/env python3
"""
Render effort_report.py's own structured JSON output (`effort_summary.json` for a batch,
`effort_report.json` for a single workbook) as a Word document with real tables -- the same
numbers `render_summary_md`/`render_report_md` already compute, just laid out for a reader who
wants a .docx to circulate rather than a markdown file. Reads the JSON directly rather than
re-parsing the markdown, so this can never drift from what EFFORT_SUMMARY.md/EFFORT_REPORT.md
already say -- it is a second RENDERING of the same numbers, not a second computation of them.

Usage:
  python3 render_effort_docx.py <effort_summary.json or effort_report.json> [--out <path.docx>]

Input is auto-detected: a JSON array is a batch (effort_summary.json), a JSON object is a single
workbook (effort_report.json). `--out` defaults to the input's own path with .json replaced by
.docx.
"""
import os
import sys
import json
import argparse

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH


MANDATORY_OPTIONAL_NOTE = (
    "Mandatory = placed on a real worksheet visual or filter somewhere in that workbook -- "
    "something visible in the report is wrong or missing until fixed. Optional = no current "
    "report impact (an internal ingredient another calc references, or truly orphaned) -- the "
    "same field_has_report_impact() check each workbook's own COPILOT_FIX_PROMPTS.md \"No "
    "Current Report Impact\" section uses, so this document and that one never disagree about "
    "which fields are load-bearing today. Plan delivery dates against the mandatory total; "
    "optional items don't block anything currently visible."
)

_BUCKET_LABELS = [
    ("recommended_prompt", "Copilot prompt with recommendation"),
    ("raw_prompt", "Copilot prompt, no recommendation"),
    ("manual_structural", "Structural/model-design"),
    ("visual_undecided", "Visual type undecided"),
    ("zone_gap", "Dropped zones"),
]

# Short column labels shared by the Corpus Rollup and Per-Workbook Detail tables -- same 5
# buckets as _BUCKET_LABELS, just the compact header text used in a wide table.
_ROLLUP_BUCKET_LABELS = [
    ("recommended_prompt", "Copilot\nCandidate"),
    ("raw_prompt", "Manual\nDAX"),
    ("manual_structural", "Manual\nStructural"),
    ("visual_undecided", "Visual\nUndecided"),
    ("zone_gap", "Zone\nGap"),
]


def _mapping_pct(e):
    m = e.get("mapping") or {}
    mapped, gaps = m.get("mapped", 0), m.get("gaps", 0)
    denom = mapped + gaps
    return round(100 * mapped / denom) if denom else 0


def _rollup_row(counts_key, hours_key, items):
    """One row's worth of the 5 shared buckets, from a list of per-workbook effort dicts
    (`items`) -- used for both a single workbook (items=[e]) and a whole group (items=many)."""
    vals = {b: sum(e[counts_key][b] for e in items) for b, _ in _ROLLUP_BUCKET_LABELS}
    total = sum(vals.values())
    hours = round(sum(e[hours_key]["total"] for e in items), 2)
    return vals, total, hours


# ==================================================================== styling helpers
def _add_heading(doc, text, level=1):
    h = doc.add_heading(text, level=level)
    return h


def _add_note(doc, text):
    p = doc.add_paragraph(text)
    for run in p.runs:
        run.italic = True
        run.font.size = Pt(9)
    return p


def _style_table(table):
    table.style = "Light Grid Accent 1"
    for cell in table.rows[0].cells:
        for p in cell.paragraphs:
            for run in p.runs:
                run.bold = True


def _add_table(doc, headers, rows, bold_last_row=False):
    table = doc.add_table(rows=1, cols=len(headers))
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = str(h)
    for r_i, row in enumerate(rows):
        cells = table.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = "" if v is None else str(v)
        if bold_last_row and r_i == len(rows) - 1:
            for cell in cells:
                for p in cell.paragraphs:
                    for run in p.runs:
                        run.bold = True
    _style_table(table)
    return table


def _truncate(text, n):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "..."


# ==================================================================== batch (EFFORT_SUMMARY)
def render_batch_docx(reports, out_path):
    doc = Document()
    doc.add_heading(f"Effort Summary -- {len(reports)} workbook(s)", level=0)
    _add_note(doc, MANDATORY_OPTIONAL_NOTE)

    total_counts = {b: 0 for b, _ in _BUCKET_LABELS}
    total_hm = {b: 0.0 for b, _ in _BUCKET_LABELS}
    total_ho = {b: 0.0 for b, _ in _BUCKET_LABELS}
    for e in reports:
        for b, _ in _BUCKET_LABELS:
            total_counts[b] += e["counts"][b]
            total_hm[b] += e["hours_mandatory"][b]
            total_ho[b] += e["hours_optional"][b]
    already_resolved = sum(e["qa"]["already_resolved_count"] for e in reports)

    grand_m = round(sum(total_hm.values()), 1)
    grand_o = round(sum(total_ho.values()), 1)
    grand_total = round(grand_m + grand_o, 1)

    _add_heading(doc, "Mandatory Effort", level=1)
    doc.add_paragraph(
        "Placed on a real worksheet visual or filter somewhere in that workbook -- something "
        "visible in the report is wrong or missing until fixed. Plan delivery dates against "
        "this total.")
    rows_m = [["Already resolved", already_resolved, 0]]
    for b, label in _BUCKET_LABELS:
        rows_m.append([label, total_counts[b], round(total_hm[b], 1)])
    rows_m.append(["Total mandatory", "", grand_m])
    _add_table(doc, ["Bucket", "Count (all workbooks)", "Hours"], rows_m, bold_last_row=True)
    p = doc.add_paragraph()
    r = p.add_run(f"Mandatory total: {grand_m} hours")
    r.bold = True
    p.add_run(f" -- at 6 productive hours/day, that is ~{round(grand_m / 6, 1)} person-days "
              f"across this batch, at the assumed per-item rate.")

    _add_heading(doc, "Optional Effort", level=1)
    doc.add_paragraph(
        "No current report impact -- an internal ingredient another calc references, or truly "
        "orphaned. A real gap, not a false positive, but safe to defer: fixing one only matters "
        "if someone later wants to use that field directly on a report page.")
    rows_o = [["Already resolved", already_resolved, 0]]
    for b, label in _BUCKET_LABELS:
        rows_o.append([label, total_counts[b], round(total_ho[b], 1)])
    rows_o.append(["Total optional", "", grand_o])
    _add_table(doc, ["Bucket", "Count (all workbooks)", "Hours"], rows_o, bold_last_row=True)
    p = doc.add_paragraph()
    r = p.add_run(f"Optional total: {grand_o} hours")
    r.bold = True
    p.add_run(f" -- ~{round(grand_o / 6, 1)} person-days on top of the mandatory total, only if "
              f"the team chooses to clear it too.")

    p = doc.add_paragraph()
    p.add_run(f"Grand total (mandatory + optional): {grand_total} hours.").bold = True

    total_dax_warnings = sum(len(e["qa"]["dax_warnings"]) for e in reports)
    total_unaccounted = sum(e["qa"]["unaccounted_count"] for e in reports)
    _add_heading(doc, "QA Rollup", level=1)
    doc.add_paragraph(
        f"{total_dax_warnings} static DAX warning(s) across the batch -- these will likely "
        f"error in Desktop; see each workbook's own EFFORT_REPORT.md.", style="List Bullet")
    if total_unaccounted:
        doc.add_paragraph(
            f"{total_unaccounted} unaccounted field(s) -- a build-consistency bug, not a "
            f"content gap; investigate before trusting any score from this batch.",
            style="List Bullet")

    reason_totals = {}
    for e in reports:
        for r in e["reason_breakdown"]:
            key = r["label"]
            if key not in reason_totals:
                reason_totals[key] = {"label": r["label"], "suggested_fix": r["suggested_fix"],
                                       "count": 0, "workbooks": set()}
            reason_totals[key]["count"] += r["count"]
            reason_totals[key]["workbooks"].add(e["workbook"])
    if reason_totals:
        _add_heading(doc, "Top Recurring Reasons Across the Batch", level=1)
        doc.add_paragraph(
            "Fix once, benefit every workbook -- sorted by count, so the top of this list is "
            "where a systemic fix (in dax.py/tmdl.py, or a new FIX_RULES entry) pays off most.")
        top = sorted(reason_totals.values(), key=lambda r: -r["count"])[:15]
        rows = [[r["count"], len(r["workbooks"]), r["label"], _truncate(r["suggested_fix"], 120)]
                for r in top]
        _add_table(doc, ["Count", "Workbooks affected", "Reason", "Suggested fix"], rows)

    groups = {}
    for e in reports:
        groups.setdefault(e.get("group", e["workbook"]), []).append(e)

    rollup_headers = (["Group", "Wkbks", "Mapping %"]
                       + [lbl.replace("\n", " ") for _, lbl in _ROLLUP_BUCKET_LABELS]
                       + ["Total", "Hours", "Estimated Avg Hrs / Workbook"])

    def _rollup_table(kind_suffix):
        counts_key, hours_key = f"counts_{kind_suffix}", f"hours_{kind_suffix}"
        rows = []
        for group_name, items in groups.items():
            mapped = sum(e["mapping"]["mapped"] for e in items if e.get("mapping"))
            gaps = sum(e["mapping"]["gaps"] for e in items if e.get("mapping"))
            pct = round(100 * mapped / (mapped + gaps)) if (mapped + gaps) else 0
            vals, total, hours = _rollup_row(counts_key, hours_key, items)
            avg = round(hours / len(items), 2) if items else 0
            rows.append([group_name, len(items), f"{pct}%"]
                        + [vals[b] for b, _ in _ROLLUP_BUCKET_LABELS] + [total, hours, avg])
        rows.sort(key=lambda r: r[0])
        mapped_all = sum(e["mapping"]["mapped"] for e in reports if e.get("mapping"))
        gaps_all = sum(e["mapping"]["gaps"] for e in reports if e.get("mapping"))
        pct_all = round(100 * mapped_all / (mapped_all + gaps_all)) if (mapped_all + gaps_all) \
            else 0
        vals_all, total_all, hours_all = _rollup_row(counts_key, hours_key, reports)
        avg_all = round(hours_all / len(reports), 2) if reports else 0
        rows.append(["GRAND TOTAL", len(reports), f"{pct_all}%"]
                    + [vals_all[b] for b, _ in _ROLLUP_BUCKET_LABELS]
                    + [total_all, hours_all, avg_all])
        _add_table(doc, rollup_headers, rows, bold_last_row=True)

    _add_heading(doc, "Corpus Rollup by Group", level=1)
    doc.add_paragraph(
        "Mapping % is a whole-model build metric (share of that group's calculated fields that "
        "emitted into the model) -- it is not split mandatory/optional, since it measures "
        "translation success, not remaining work.")
    _add_heading(doc, "Mandatory", level=2)
    _rollup_table("mandatory")
    _add_heading(doc, "Optional", level=2)
    _rollup_table("optional")

    worksheet_headers = (["Workbook", "Mapping %"]
                          + [lbl.replace("\n", " ") for _, lbl in _ROLLUP_BUCKET_LABELS]
                          + ["Total", "Hours"])

    def _worksheet_table(kind_suffix):
        counts_key, hours_key = f"counts_{kind_suffix}", f"hours_{kind_suffix}"
        rows = []
        for e in reports:
            vals, total, hours = _rollup_row(counts_key, hours_key, [e])
            rows.append([e["workbook"], f"{_mapping_pct(e)}%"]
                        + [vals[b] for b, _ in _ROLLUP_BUCKET_LABELS] + [total, hours])
        rows.sort(key=lambda r: -r[-1])
        _add_table(doc, worksheet_headers, rows)

    _add_heading(doc, "Worksheet-Level Detail (Per-Workbook)", level=1)
    _add_heading(doc, "Mandatory", level=2)
    _worksheet_table("mandatory")
    _add_heading(doc, "Optional", level=2)
    _worksheet_table("optional")

    doc.save(out_path)
    return out_path


# ==================================================================== single workbook (EFFORT_REPORT)
def render_single_docx(e, out_path):
    doc = Document()
    doc.add_heading(f"Effort Report -- {e['workbook']}", level=0)
    _add_note(doc, MANDATORY_OPTIONAL_NOTE)

    c, hm, ho = e["counts"], e["hours_mandatory"], e["hours_optional"]
    who = {
        "recommended_prompt": "Analyst + Copilot in Desktop",
        "raw_prompt": "Analyst, in Tableau + Desktop",
        "manual_structural": "Analyst, in Desktop",
        "visual_undecided": "Analyst, screenshot from Tableau",
        "zone_gap": "Recheck after rebuild",
    }
    total_hm = round(e["hours_mandatory"]["total"], 2)
    total_ho = round(e["hours_optional"]["total"], 2)

    _add_heading(doc, "Mandatory Effort", level=1)
    rows_m = [[label, c[b], round(hm[b], 2), who[b]] for b, label in _BUCKET_LABELS]
    rows_m.append(["Total mandatory", "", total_hm, ""])
    _add_table(doc, ["Bucket", "Count", "Hours", "Who"], rows_m, bold_last_row=True)

    _add_heading(doc, "Optional Effort", level=1)
    rows_o = [[label, c[b], round(ho[b], 2), who[b]] for b, label in _BUCKET_LABELS]
    rows_o.append(["Total optional", "", total_ho, ""])
    _add_table(doc, ["Bucket", "Count", "Hours", "Who"], rows_o, bold_last_row=True)

    grand_total = round(e["hours"]["total"], 2)
    p = doc.add_paragraph()
    r = p.add_run(f"Mandatory total: {total_hm} hours")
    r.bold = True
    p.add_run(" -- the number to plan a delivery date against. ")
    p.add_run(f"Optional total: {total_ho} hours -- only if the team chooses to clear it too. ")
    p2 = doc.add_paragraph()
    p2.add_run(f"Grand total (mandatory + optional): {grand_total} hours.").bold = True

    doc.add_paragraph(
        f"Already resolved and live in the model: {e['qa']['already_resolved_count']} "
        f"(zero remaining effort, but see QA checklist below).")

    if e["reason_breakdown"]:
        _add_heading(doc, "Manual-Investigation Items, by Recurring Reason", level=1)
        rows = [[r["count"], r["label"], _truncate(r["suggested_fix"], 140)]
                for r in e["reason_breakdown"]]
        _add_table(doc, ["Count", "Reason", "Suggested fix"], rows)

    _add_heading(doc, "QA Checklist -- Verify Before Trusting This Model", level=1)
    doc.add_paragraph(
        f"{e['qa']['already_resolved_count']} already-resolved field(s): "
        f"{e['qa']['already_resolved_note']}", style="List Bullet")
    if e["qa"]["dax_warnings"]:
        doc.add_paragraph(
            f"{len(e['qa']['dax_warnings'])} static DAX warning(s) -- these will likely error "
            f"in Desktop, fix before shipping:", style="List Bullet")
        for w in e["qa"]["dax_warnings"]:
            doc.add_paragraph(f"{w['table']}[{w['field']}]: {w['warning']}",
                               style="List Bullet 2")
    if e["qa"]["unaccounted_count"]:
        doc.add_paragraph(
            f"{e['qa']['unaccounted_count']} unaccounted field(s): "
            f"{e['qa']['unaccounted_note']}", style="List Bullet")
    doc.add_paragraph(
        "Run tools/pbip_rules/lint.py <project> --schemas -- structural checks Desktop reports "
        "badly or not at all.", style="List Bullet")
    doc.add_paragraph(
        "Open the model in Power BI Desktop and spot-check the pages that use any "
        "already-resolved field above against the original Tableau workbook.",
        style="List Bullet")

    doc.save(out_path)
    return out_path


# ==================================================================== CLI
def main():
    parser = argparse.ArgumentParser(
        description="Render effort_report.py's JSON output as a Word document")
    parser.add_argument("input", help="effort_summary.json (batch) or effort_report.json "
                                       "(single workbook)")
    parser.add_argument("--out", help="Output .docx path (default: input path with .docx)")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as fh:
        data = json.load(fh)

    out_path = args.out or os.path.splitext(args.input)[0] + ".docx"

    if isinstance(data, list):
        render_batch_docx(data, out_path)
    else:
        render_single_docx(data, out_path)

    print(f"[ok] wrote {out_path}")


if __name__ == "__main__":
    main()
