#!/usr/bin/env python3
"""
Generate a Power BI SME remediation script from a real build-tableau-pbip + validate-tableau-
pbip-mapping output.

Why this exists
----------------
validate-tableau-pbip-mapping's Gaps sheet already says, per declined field: what it was, why it
was declined, and (from FIX_RULES) a suggested fix. Nothing turns that into something an SME can
actually work through: an ordered list of what to fix first (root causes before their cascading
dependents), and for the fields a natural-language description can plausibly solve, a ready-to-
paste prompt for Power BI Desktop's Copilot chat. This script is that -- a FORMATTER over data
this pipeline already computed (the Gaps sheet, COMPLIANCE.json, VALIDATION_SUMMARY.json, and the
emitted TMDL), not a new translator. It never drafts DAX itself and never guesses at a fix beyond
what FIX_RULES already says; where no rule fired, the SME is told plainly that this is a human
decision, not a Copilot task.

Per project, classifies each Gaps-sheet row into:
  CASCADE         -- "depends on 'X', which was itself declined" -- fix X, this recovers for free.
  INLINE_MEASURE  -- a SUM/MIN/MAX/AVG re-aggregates another calc that's already a DAX measure in
                     this model (DAX can't SUM() a measure) -- the fix is a row-level iterator
                     that inlines that measure's real DAX, which this script looks up from the
                     emitted TMDL (via validate_mapping.py's own TMDL reader) rather than asking
                     the SME to go find it.
  REWRITE         -- FIX_RULES gave a concrete "Rewrite as ..." transformation -- mechanical,
                     Copilot-assistable.
  MANUAL          -- everything else FIX_RULES says (reconnect a source, ask the workbook author,
                     recreate a group, pick a visual by hand, ...) -- not a DAX-drafting task, so
                     no Copilot prompt is generated; the suggested fix is carried verbatim.

Run
---
    python3 sme_fix_script.py <build-tableau-pbip --out folder>

Writes <project>/validation/SME_FIX_SCRIPT.md for every project folder that has both a
validation/MAPPING_VALIDATION/<safe>/<safe>_mapping.xlsx (from validate-tableau-pbip-mapping) and
a *.SemanticModel folder.
"""
import os
import re
import sys
import json
import argparse
from pathlib import Path

try:
    import openpyxl
except ImportError:                                                        # pragma: no cover
    openpyxl = None

# ---------------------------------------------------------------- reuse the real TMDL reader
# validate_mapping.py already parses the emitted TMDL into {table: {name: dax_text}}. Re-reading
# that here (rather than re-parsing TMDL a second time) is the same "formatter, not a new
# extractor" convention this whole pipeline uses.
_THIS = Path(__file__).resolve()
_VM_DIR = _THIS.parents[1] / ".claude" / "skills" / "validate-tableau-pbip-mapping" / "scripts"
if str(_VM_DIR) not in sys.path:
    sys.path.insert(0, str(_VM_DIR))
from validate_mapping import load_dax_by_table          # noqa: E402  (path set up above)

GENERIC_FIX = ("No canned fix yet for this reason -- treat as a one-off human decision and "
               "consider adding a rule to FIX_RULES in validate_mapping.py if this pattern "
               "recurs.")

_CASCADE_RE = re.compile(r"^depends on '(.+?)', which was itself declined")
_INLINE_MEASURE_RE = re.compile(
    r"Tableau (SUM|MIN|MAX|AVG)\(\[(.+?)\]\) aggregates a reference to"
)

# Reasons FIX_RULES (validate_mapping.py) gives a concrete, mechanical DAX rewrite for -- a good
# Copilot candidate. Matched against `reason`, not `suggested_fix` text: several of these fixes
# don't literally start with the word "Rewrite" (e.g. DATEPARSE's "DATEPARSE(format, string) has
# no single DAX function. Rewrite using ..."), so sniffing the fix text for that word missed
# them. The reason string is what FIX_RULES itself keys on, so matching it directly is the same
# signal FIX_RULES used to pick a fix, not a re-guess of it. Excludes rules whose own fix text
# says to do something manual (LTRIM/RTRIM, generic no-DAX-equivalent, Power Query-only fixes) --
# those stay MANUAL below.
_ASSISTABLE_REASON_PATTERNS = [
    re.compile(r"Tableau COUNTD\(<expression>\) has no DAX equivalent"),
    re.compile(r"Tableau (SUM|MIN|MAX|AVG)\(<expression>\) needs the DAX iterator"),
    re.compile(r"no DAX equivalent for Tableau function DATEPARSE\(\)"),
    re.compile(r"no DAX equivalent for Tableau function REGEXP_\w+\(\)"),
    re.compile(r"no DAX equivalent for Tableau function DATE\(\)"),
    re.compile(r"Tableau STR\(<date>\)"),
    re.compile(r"Tableau MID\(s,start\[,len\]\) vs DAX MID"),
    re.compile(r"Tableau DATEADD\('unit',n,date\) vs DAX DATEADD"),
    re.compile(r"Tableau DATEDIFF\('unit',a,b\) vs DAX DATEDIFF"),
    re.compile(r"Tableau (STARTSWITH|ENDSWITH)\(s,sub\) has no DAX equivalent"),
    re.compile(r"Tableau SPLIT\(s,delim,n\) has no DAX equivalent"),
]


# ==================================================================== classification
def classify(reason, suggested_fix, dax_by_table, all_measure_names):
    """Return (category, extra) for one Gaps-sheet row. See module docstring for categories."""
    reason = reason or ""
    m = _CASCADE_RE.match(reason)
    if m:
        return "CASCADE", {"root": m.group(1)}

    m = _INLINE_MEASURE_RE.search(reason)
    if m:
        agg_fn, ref_name = m.group(1), m.group(2)
        ref_dax = None
        if ref_name in all_measure_names:
            for members in dax_by_table.values():
                if ref_name in members:
                    ref_dax = members[ref_name]
                    break
        return "INLINE_MEASURE", {"agg_fn": agg_fn, "ref_name": ref_name, "ref_dax": ref_dax}

    if "references 0" not in reason:
        for pat in _ASSISTABLE_REASON_PATTERNS:
            if pat.search(reason):
                return "REWRITE", {}

    return "MANUAL", {}


# ==================================================================== Copilot prompt building
def copilot_prompt_rewrite(field, table, formula, suggested_fix):
    return (
        f'Add a DAX measure or calculated column (whichever fits -- a per-row result is a '
        f'column, an aggregated result is a measure) named "{field}" on table \'{table}\' that '
        f"reproduces this Tableau calculated field.\n\n"
        f"Tableau formula:\n{formula}\n\n"
        f"Apply this exact rewrite rule (already worked out for this case, don't invent a "
        f"different approach):\n{suggested_fix}\n\n"
        f"Use real column and measure names from this semantic model. Return only the final DAX "
        f"expression."
    )


def copilot_prompt_inline_measure(field, table, formula, agg_fn, ref_name, ref_dax):
    ref_block = (
        f"'{ref_name}' =\n{ref_dax}"
        if ref_dax else
        f"(look up the '{ref_name}' measure in this model -- Model view or the TMDL definition -- "
        f"and paste its DAX here before sending this prompt)"
    )
    return (
        f'Add a Power BI DAX measure named "{field}" on table \'{table}\' that reproduces this '
        f"Tableau calculated field.\n\n"
        f"Tableau formula:\n{formula}\n\n"
        f"This originally aggregated another calculation with {agg_fn}(), but DAX cannot "
        f"re-aggregate a measure that way -- {agg_fn}() needs a row-level expression to iterate, "
        f"not another measure's already-aggregated result. That other calculation is this "
        f"existing measure in the model:\n\n{ref_block}\n\n"
        f"Rewrite the measure above as a {agg_fn}X(<table>, <row-level expression>) iterator that "
        f"inlines the referenced measure's logic per row, instead of calling the measure "
        f"directly. Return only the final DAX expression."
    )


# ==================================================================== data loading
def load_gaps(mapping_xlsx):
    wb = openpyxl.load_workbook(mapping_xlsx)
    ws = wb["Gaps"]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:]]


def find_mapping_xlsx(project_dir):
    hits = sorted(project_dir.glob("validation/MAPPING_VALIDATION/*/*_mapping.xlsx"))
    return hits[0] if hits else None


def load_compliance_notes(project_dir):
    path = project_dir / "validation" / "COMPLIANCE.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("notes", [])
    except (json.JSONDecodeError, OSError):
        return []


def load_summary(project_dir):
    path = project_dir / "validation" / "VALIDATION_SUMMARY.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ==================================================================== markdown rendering
_INTRO = """\
This is a worklist for closing the gap between this Power BI project and the Tableau workbook it
was converted from. Every item below is a real Tableau calculated field that did **not** make it
into the semantic model -- nothing was approximated or emitted as a guess, so nothing here is a
false positive.

**How to use the Copilot prompts below**: open this project in Power BI Desktop, open Copilot
chat, and paste the prompt for one field at a time. Copilot drafts the DAX; you still need to
add it as a new measure/column on the table named in the prompt, and verify the result against
the Tableau workbook's own numbers before trusting it -- Copilot's draft is a starting point, not
a verified answer. Items marked **manual** are not DAX-drafting tasks at all (a data-source
reconnect, a modeling decision, a Tableau-side lookup); Copilot won't help with those, so none is
given.
"""


def render(project_name, summary, notes, gap_rows, dax_by_table, all_measure_names):
    classified = []
    for row in gap_rows:
        field = row.get("Tableau field") or ""
        table = row.get("Tableau table") or ""
        formula = (row.get("Formula") or "").strip()
        reason = row.get("Reason") or ""
        fix = row.get("Suggested fix") or ""
        cat, extra = classify(reason, fix, dax_by_table, all_measure_names)
        classified.append({
            "field": field, "table": table, "formula": formula, "reason": reason,
            "fix": fix, "cat": cat, **extra,
        })

    by_field = {c["field"]: c for c in classified}
    cascade_unlocks = {}
    for c in classified:
        if c["cat"] == "CASCADE":
            cascade_unlocks.setdefault(c["root"], []).append(c["field"])

    lines = [f"# SME Fix Script — {project_name}", ""]
    if summary:
        lines += [
            f"- Status: **{summary.get('status', 'UNKNOWN')}** "
            f"(quality score {summary.get('quality_score', '?')}/100)",
            f"- Mapping coverage: {summary.get('mapping_quality', '?')}%",
            f"- Compliance issues: {summary.get('compliance_issues', 0)}",
            "",
        ]
    lines += [_INTRO, ""]

    # ---- Step 0: compliance cleanup
    lines.append("## Step 0 — Compliance cleanup")
    lines.append("")
    if notes:
        lines.append("Informational only (these do not block opening the project), but clean "
                      "up before final delivery:")
        lines.append("")
        for n in notes:
            lines.append(f"- [ ] {n}")
    else:
        lines.append("None -- no compliance notes on this build.")
    lines.append("")

    # ---- Step 1: root-cause fields (unlock cascades)
    lines.append("## Step 1 — Fix these first (each one unlocks other fields for free)")
    lines.append("")
    if cascade_unlocks:
        lines.append("Fixing a root field below automatically recovers every field listed under "
                      "it -- those dependents need no separate action.")
        lines.append("")
        for root, dependents in sorted(cascade_unlocks.items()):
            root_c = by_field.get(root)
            lines.append(f"### `{root}`  (unlocks: {', '.join(sorted(dependents))})")
            lines.append("")
            if root_c is None:
                lines.append("_This root field is not itself in the Gaps sheet (it may be on a "
                              "different worksheet/workbook, or already mapped) -- translate it "
                              "wherever it actually lives before revisiting the dependents "
                              "above._")
            else:
                _render_item(lines, root_c)
            lines.append("")
    else:
        lines.append("None -- no field in this workbook is blocked purely on another field.")
        lines.append("")

    already_shown = {root for root in cascade_unlocks} | set().union(*cascade_unlocks.values()) \
        if cascade_unlocks else set()

    # ---- Step 2: remaining Copilot-assistable fields
    remaining = [c for c in classified
                 if c["cat"] in ("INLINE_MEASURE", "REWRITE") and c["field"] not in already_shown]
    lines.append("## Step 2 — Remaining fields Copilot can help draft")
    lines.append("")
    if remaining:
        for c in remaining:
            lines.append(f"### `{c['field']}` ({c['table']})")
            lines.append("")
            _render_item(lines, c)
            lines.append("")
    else:
        lines.append("None.")
        lines.append("")

    # ---- Step 3: manual-judgment fields
    manual = [c for c in classified if c["cat"] == "MANUAL" and c["field"] not in already_shown]
    lines.append("## Step 3 — Fields that need your judgment (not a Copilot task)")
    lines.append("")
    if manual:
        lines.append("| Field | Table | Why it was declined | What to do |")
        lines.append("|---|---|---|---|")
        for c in manual:
            fix = c["fix"] if c["fix"] and c["fix"] != GENERIC_FIX else \
                "No canned guidance -- use your own judgment on how to reproduce this in DAX/Power Query."
            lines.append(f"| {c['field']} | {c['table']} | {c['reason']} | {fix} |")
    else:
        lines.append("None.")
    lines.append("")

    lines.append("## Step 4 — After the fields above are fixed")
    lines.append("")
    lines.append("Re-run the validator, then check RESIDUE.md's \"Visual type undecided\" and "
                 "\"Zones with no Power BI construct\" sections -- most of those failed to "
                 "resolve only because they referenced one of the fields above; once the fields "
                 "exist, revisit those visuals in Desktop and pick the right chart type by hand.")
    lines.append("")

    return "\n".join(lines)


def _render_item(lines, c):
    lines.append(f"- Table: `{c['table']}`")
    lines.append(f"- Tableau formula: `{c['formula']}`")
    lines.append(f"- Reason declined: {c['reason']}")
    lines.append("")
    if c["cat"] == "INLINE_MEASURE":
        prompt = copilot_prompt_inline_measure(
            c["field"], c["table"], c["formula"], c["agg_fn"], c["ref_name"], c["ref_dax"])
        lines.append("**Copilot prompt:**")
        lines.append("```")
        lines.append(prompt)
        lines.append("```")
    elif c["cat"] == "REWRITE":
        prompt = copilot_prompt_rewrite(c["field"], c["table"], c["formula"], c["fix"])
        lines.append("**Copilot prompt:**")
        lines.append("```")
        lines.append(prompt)
        lines.append("```")
    else:
        fix = c["fix"] if c["fix"] and c["fix"] != GENERIC_FIX else \
            "No canned guidance -- use your own judgment on how to reproduce this in DAX/Power Query."
        lines.append(f"**Manual action (not a Copilot task):** {fix}")


# ==================================================================== main
def find_projects(build_out):
    projects = []
    for name in sorted(os.listdir(build_out)):
        project_dir = build_out / name
        if not project_dir.is_dir():
            continue
        if find_mapping_xlsx(project_dir) is not None:
            projects.append(project_dir)
    return projects


def process_project(project_dir):
    mapping_xlsx = find_mapping_xlsx(project_dir)
    if mapping_xlsx is None:
        return None
    if openpyxl is None:
        raise RuntimeError("openpyxl is required (pip install openpyxl)")

    model_dirname = next((d for d in os.listdir(project_dir) if d.endswith(".SemanticModel")),
                          None)
    if model_dirname:
        dax_by_table, _names_by_table, all_measure_names = load_dax_by_table(
            str(project_dir), model_dirname)
    else:
        dax_by_table, all_measure_names = {}, set()

    gap_rows = load_gaps(mapping_xlsx)
    notes = load_compliance_notes(project_dir)
    summary = load_summary(project_dir)

    md = render(project_dir.name, summary, notes, gap_rows, dax_by_table, all_measure_names)
    out_path = project_dir / "validation" / "SME_FIX_SCRIPT.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    return out_path, len(gap_rows)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a Power BI SME remediation script (with Copilot prompts) from a "
                     "validate-tableau-pbip-mapping output.")
    ap.add_argument("build_out", type=Path,
                     help="folder holding one or more build-tableau-pbip project folders")
    args = ap.parse_args(argv)

    projects = find_projects(args.build_out)
    if not projects:
        print(f"[fatal] no projects with a mapping report found under {args.build_out}",
              file=sys.stderr)
        return 2

    for project_dir in projects:
        out_path, n_gaps = process_project(project_dir)
        print(f"[ok] {project_dir.name:50s} {n_gaps:4d} gap(s) -> {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
