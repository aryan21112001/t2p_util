#!/usr/bin/env python3
"""
Turn a workbook's residue.json into ONE management-facing number: how much manual work is left,
split by who can do it, plus what must be QA-checked before the model is trusted. Built because
at corpus scale (the stated target is ~1000 dashboards) nobody can read a COPILOT_FIX_PROMPTS.md
per workbook -- there needs to be one report that rolls up.

Every outstanding item lands in exactly one bucket. Every "manual" bucket still needs a human (or
Copilot in Power BI Desktop, with real schema access) to write the DAX/model change by hand --
nothing here is machine-applied. The only distinction this report draws is how much of a running
start COPILOT_FIX_PROMPTS.md gives that human, which is exactly the same Section A ("Recommended
Fixes") vs Section B ("No Recommended Fix") split that document itself uses:

  RECOMMENDED_PROMPT    -- a FIX_RULES pattern matched this reason, so the Copilot prompt includes
                           a specific starting DAX shape (e.g. "Rewrite as DATEVALUE(...)"). Lower
                           effort than diagnosing from nothing, but still real work: the pattern is
                           a starting point, not runnable DAX -- nothing here has been written into
                           the model.
  RAW_PROMPT            -- no FIX_RULES pattern matches this reason (GENERIC_FIX). The Copilot
                           prompt hands over the raw problem with no starting shape; a human/SME
                           diagnoses it from scratch.
  MANUAL_INVESTIGATION  -- a structural/visual/zone gap that inherently needs someone to look at
                           Tableau (table calc addressing, LOD, free-entry parameters, marks,
                           dropped zones, relationships) -- there is no DAX to write at all.

This script does not invent a new classification -- it calls validate_mapping.suggest_fix (the
exact function that decides what COPILOT_FIX_PROMPTS.md's Section A/B split shows) and
generate_copilot_prompts.drop_already_emitted (the exact function that decides what is already
live in the model, however it got there), so this report's buckets can never disagree with what
those two documents say.

Per-item minutes (MINUTES_PER_ITEM below) are a documented ASSUMPTION, not a measurement from
real timing data. Treat the hours in every report as "at this assumed rate", and replace the
constants once real numbers exist from actually doing the work.

Usage:
  python3 effort_report.py <workbook build/output folder, or parent of many> [--out <folder>]
"""
import os
import sys
import json
import argparse
import collections

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
from generate_copilot_prompts import (  # noqa: E402
    drop_already_emitted, split_dax_vs_structural, find_residue_workbooks,
    rule_index, group_label, build_location_index, field_has_report_impact,
    structural_item_is_mandatory, load_blend_key_gaps,
)

_MAPPING_SKILL_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "validate-tableau-pbip-mapping", "scripts")
)
sys.path.insert(0, _MAPPING_SKILL_SCRIPTS)
from validate_mapping import (  # noqa: E402
    load_workbook_artifacts, load_dax_by_table, build_mapping, suggest_fix, GENERIC_FIX,
)

# Minutes per item, by bucket -- an assumption, tune freely; see module docstring.
MINUTES_PER_ITEM = {
    "recommended_prompt": 15,      # Copilot prompt already has a specific starting DAX shape
    "raw_prompt": 30,              # trace Tableau UI/logic, hand-write DAX from nothing
    "manual_structural": 20,       # a relationship/parameter/model-design decision in Desktop
    "visual_undecided": 15,        # screenshot the worksheet in Tableau, pick a PBI visual
    "zone_gap": 5,                 # usually self-resolves once its underlying field(s) are fixed;
                                    # a quick recheck after rebuild, not fresh investigation
    "declined_encoding": 10,       # a visual-authoring decision (different visual/role, or
                                    # accept the limitation) -- no DAX, but a real Desktop choice
    "blend_key_gap": 20,           # a relationship/model-design decision in Desktop -- same
                                    # weight class as manual_structural, no DAX to author at all
}


# ==================================================================== pure classification
def classify_items(residue, field_to_ws=None):
    """Bucket every NOT-YET-FIXED residue item. Pure function over already-loaded/derived data
    (no file I/O) so it is directly unit-testable -- see the self-test below.

    `residue` here is the post-drop_already_emitted residue (what's actually still outstanding).
    The recommended/raw split is exactly `suggest_fix(...) != GENERIC_FIX` -- the same test
    COPILOT_FIX_PROMPTS.md uses to sort a gap into its own Section A vs Section B -- so "has a
    recommendation" here can never disagree with what that document shows.

    `field_to_ws` (from `build_location_index()`) is the SAME map COPILOT_FIX_PROMPTS.md's own
    "No Current Report Impact" section is built from -- every DAX/structural item here is also
    tagged `mandatory` via the identical `field_has_report_impact()` check, so a field this
    report calls optional is never one the prompt document calls load-bearing, or vice versa.
    `field_to_ws=None` (e.g. a caller with no `_work/ir.json` to read) makes every item mandatory
    by default -- a field is only ever demoted to optional on POSITIVE evidence it has no report
    placement, never on the absence of evidence, matching this pipeline's fail-closed rule.
    `visual_undecided`/`zone_gaps`/`declined_encodings` are never split: all three are already a
    real dashboard zone/visual with something (or nothing) rendering on a live page today, not an
    internal semantic-model-only ingredient, so by definition they always carry report impact.
    """
    calc_residue = residue.get("calc_residue", [])
    dax_items, structural_items = split_dax_vs_structural(calc_residue)

    def _impact(item):
        return True if field_to_ws is None else field_has_report_impact(
            item.get("subject", ""), field_to_ws)

    recommended_prompt, raw_prompt = [], []
    for item in dax_items:
        fix = suggest_fix(item.get("reason"), item.get("formula") or "")
        row = {**item, "suggested_fix": fix, "bucket": "manual_investigation",
               "mandatory": _impact(item)}
        if fix != GENERIC_FIX:
            row["why"] = "a FIX_RULES pattern matched -- prompt includes a specific starting shape"
            recommended_prompt.append(row)
        else:
            row["why"] = "no FIX_RULES pattern matches this reason -- raw prompt only"
            raw_prompt.append(row)

    def _structural_impact(item):
        # A physical-layer join is mandatory unconditionally, regardless of _impact(item) --
        # see structural_item_is_mandatory's own docstring for why field_has_report_impact
        # alone systematically mis-scores this specific reason as optional.
        return True if field_to_ws is None else structural_item_is_mandatory(item, field_to_ws)

    manual_structural = [
        {**item, "bucket": "manual_investigation",
         "suggested_fix": suggest_fix(item.get("reason"), item.get("formula") or ""),
         "why": "structural/model-design gap -- no DAX to author at all",
         "mandatory": _structural_impact(item)}
        for item in structural_items
    ]

    visual_undecided = residue.get("visual_undecided", [])
    zone_gaps = residue.get("zone_gaps", [])
    declined_encodings = residue.get("declined_encodings", [])
    # Unlike the three buckets above (frozen into _work/residue.json at Agent-1 build time),
    # blend_key_gaps is populated post-data by validate-blend-key-domains -- see
    # `compute_effort`'s own sibling-file merge, which is the ONLY place this bucket's items
    # ever get added to `residue` before this function runs. Never split into recommended/raw
    # (no DAX to write at all, same as the structural items above) and always mandatory (a
    # zero-overlap relationship is a real, live model defect, not an internal-only ingredient).
    blend_key_gaps = residue.get("blend_key_gaps", [])

    manual_all = recommended_prompt + raw_prompt + manual_structural
    reason_groups = collections.OrderedDict()
    for item in manual_all:
        reason = item.get("reason", "")
        idx = rule_index(reason)
        key = ("rule", idx) if idx is not None else ("generic", None)
        if key not in reason_groups:
            reason_groups[key] = {"label": group_label([item]),
                                   "suggested_fix": item["suggested_fix"], "count": 0}
        reason_groups[key]["count"] += 1

    buckets = {
        "recommended_prompt": recommended_prompt,
        "raw_prompt": raw_prompt,
        "manual_structural": manual_structural,
        "visual_undecided": visual_undecided,
        "zone_gap": zone_gaps,
        "declined_encoding": declined_encodings,
        "blend_key_gap": blend_key_gaps,
    }
    # visual_undecided/zone_gap/declined_encoding/blend_key_gap carry no "mandatory" key (always
    # mandatory --
    # see docstring); `.get("mandatory", True)` treats an absent key the same as an explicit True.
    counts = {b: len(items) for b, items in buckets.items()}
    counts_mandatory = {b: sum(1 for it in items if it.get("mandatory", True))
                        for b, items in buckets.items()}
    counts_optional = {b: counts[b] - counts_mandatory[b] for b in buckets}
    hours = {b: round(counts[b] * MINUTES_PER_ITEM[b] / 60.0, 2) for b in buckets}
    hours_mandatory = {b: round(counts_mandatory[b] * MINUTES_PER_ITEM[b] / 60.0, 2)
                        for b in buckets}
    hours_optional = {b: round(hours[b] - hours_mandatory[b], 2) for b in buckets}
    hours["total"] = round(sum(hours.values()), 2)
    hours_mandatory["total"] = round(sum(hours_mandatory.values()), 2)
    hours_optional["total"] = round(sum(hours_optional.values()), 2)

    return {
        "counts": counts,
        "counts_mandatory": counts_mandatory,
        "counts_optional": counts_optional,
        "hours": hours,
        "hours_mandatory": hours_mandatory,
        "hours_optional": hours_optional,
        "recommended_prompt": recommended_prompt,
        "raw_prompt": raw_prompt,
        "manual_structural": manual_structural,
        "visual_undecided": visual_undecided,
        "zone_gaps": zone_gaps,
        "declined_encodings": declined_encodings,
        "blend_key_gaps": blend_key_gaps,
        "reason_breakdown": sorted(
            ({"label": g["label"], "suggested_fix": g["suggested_fix"], "count": g["count"]}
             for g in reason_groups.values()),
            key=lambda r: -r["count"]),
    }


# ==================================================================== I/O wrapper
def compute_effort(wb_dir):
    """The full per-workbook report: real file I/O (residue.json, ir.json, name_authority.json,
    the built TMDL), on top of the pure classify_items() above. Not itself unit-tested --
    exercised against the real corpus, same convention every other skill in this repo uses for its
    end-to-end path (see SKILL.md)."""
    workbook_name = os.path.basename(os.path.normpath(wb_dir))
    ir, na, residue = load_workbook_artifacts(wb_dir)

    residue_remaining, n_already_resolved = drop_already_emitted(wb_dir, residue)
    residue_remaining["blend_key_gaps"] = load_blend_key_gaps(wb_dir)
    field_to_ws, _ws_to_pages, _field_to_shelf = build_location_index(wb_dir)
    classified = classify_items(residue_remaining, field_to_ws=field_to_ws)

    model_dirname = next((d for d in os.listdir(wb_dir) if d.endswith(".SemanticModel")), None)
    if model_dirname:
        dax_by_table, names_by_table, all_measure_names = load_dax_by_table(wb_dir, model_dirname)
    else:
        dax_by_table, names_by_table, all_measure_names = {}, {}, set()
    mapped, gaps, unaccounted, _id_rows_tableau, _id_rows_pbi = build_mapping(
        ir, na, residue, dax_by_table, names_by_table, all_measure_names)
    dax_warnings = [{"table": r["pbi_table"], "field": r["pbi_name"], "warning": r["dax_warning"]}
                    for r in mapped if r.get("dax_warning")]

    mapping = {"mapped": len(mapped), "gaps": len(gaps)}

    qa = {
        "already_resolved_count": n_already_resolved,
        "already_resolved_note": (
            "Already live in the TMDL, however it got there (Desktop edit, Copilot paste-in, "
            "whatever) -- not machine-verified, spot-check a sample against Tableau's own "
            "rendered values before trusting the rest."),
        "dax_warnings": dax_warnings,
        "unaccounted_count": len(unaccounted),
        "unaccounted_note": (
            "Should always be 0. A nonzero count means a build-consistency bug, not a content "
            "gap -- investigate the build, this is not something to fix by hand in Desktop."),
    }

    return {"workbook": workbook_name, **classified, "qa": qa, "mapping": mapping}


# ==================================================================== rendering
def render_report_md(e):
    c, h = e["counts"], e["hours"]
    hm, ho = e["hours_mandatory"], e["hours_optional"]
    cm, co = e["counts_mandatory"], e["counts_optional"]
    lines = [f"# Effort Report -- {e['workbook']}", ""]
    lines.append(
        "Every item below is still outstanding (already-applied fixes are excluded). Hours are "
        "computed from the assumed per-item minutes in `effort_report.py`'s `MINUTES_PER_ITEM` "
        "-- adjust those constants once real timing data exists; treat these numbers as an "
        "estimate, not a commitment.")
    lines.append("")
    lines.append(
        "**Mandatory** = placed on a real worksheet visual or filter somewhere in this workbook "
        "-- something visible in the report is wrong or missing until this is fixed. "
        "**Optional** = no current report impact (an internal ingredient another calc "
        "references, or truly orphaned) -- per `field_has_report_impact()`, the SAME check "
        "COPILOT_FIX_PROMPTS.md's own \"No Current Report Impact\" section uses, so the two "
        "documents never disagree about which fields are load-bearing today. Optional items are "
        "real gaps, not false positives -- just safe to defer; fix them only if someone wants to "
        "use that field directly on a page later.")
    lines.append("")
    lines.append("| Bucket | Count | Mandatory hrs | Optional hrs | Who |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| Copilot prompt with recommendation | {c['recommended_prompt']} "
                  f"({cm['recommended_prompt']} mand. / {co['recommended_prompt']} opt.) "
                  f"| {hm['recommended_prompt']} | {ho['recommended_prompt']} "
                  f"| Analyst + Copilot in Desktop |")
    lines.append(f"| Copilot prompt, no recommendation (raw problem) | {c['raw_prompt']} "
                  f"({cm['raw_prompt']} mand. / {co['raw_prompt']} opt.) "
                  f"| {hm['raw_prompt']} | {ho['raw_prompt']} | Analyst, in Tableau + Desktop |")
    lines.append(f"| Structural/model-design (relationships, parameters) | {c['manual_structural']} "
                  f"({cm['manual_structural']} mand. / {co['manual_structural']} opt.) "
                  f"| {hm['manual_structural']} | {ho['manual_structural']} | Analyst, in Desktop |")
    lines.append(f"| Visual type undecided | {c['visual_undecided']} "
                  f"| {hm['visual_undecided']} | {ho['visual_undecided']} "
                  f"| Analyst, screenshot from Tableau |")
    lines.append(f"| Dropped zone (usually self-resolves) | {c['zone_gap']} "
                  f"| {hm['zone_gap']} | {ho['zone_gap']} | Recheck after rebuild |")
    lines.append(f"| Color/Size encoding not reproduced | {c['declined_encoding']} "
                  f"| {hm['declined_encoding']} | {ho['declined_encoding']} "
                  f"| Analyst, in Desktop (visual authoring) |")
    lines.append(f"| **Total remaining** | | **{hm['total']}** | **{ho['total']}** | |")
    lines.append("")
    lines.append(f"**Grand total (mandatory + optional): {h['total']} hours.** "
                 f"Mandatory alone: **{hm['total']} hours** -- the number to plan a delivery "
                 f"date against; optional items don't block anything currently visible.")
    lines.append("")
    lines.append(f"Already resolved and live in the model: **{e['qa']['already_resolved_count']}** "
                  "(zero remaining effort, but see QA checklist below).")
    lines.append("")

    if e["reason_breakdown"]:
        lines.append("## Manual-investigation items, by recurring reason")
        lines.append("")
        lines.append(
            "Fixing the pattern once (in `dax.py`/`tmdl.py`, or as a new `FIX_RULES`/mechanical "
            "rule) clears every instance of it in one shot -- on the reference corpus, one "
            "DATEPARSE fix cleared 24 direct uses and ~37 fields once cascades resolved. Sorted "
            "by count, so the top of this list is where a systemic fix pays off most.")
        lines.append("")
        lines.append("| Count | Reason | Suggested fix |")
        lines.append("|---|---|---|")
        for r in e["reason_breakdown"]:
            fix = r["suggested_fix"].replace("\n", " ")
            if len(fix) > 140:
                fix = fix[:140].rsplit(" ", 1)[0] + "..."
            lines.append(f"| {r['count']} | {r['label']} | {fix} |")
        lines.append("")

    lines.append("## QA checklist -- verify before trusting this model")
    lines.append("")
    lines.append(f"- [ ] **{e['qa']['already_resolved_count']} already-resolved field(s)**: "
                 f"{e['qa']['already_resolved_note']}")
    if e["qa"]["dax_warnings"]:
        lines.append(f"- [ ] **{len(e['qa']['dax_warnings'])} static DAX warning(s)** -- these "
                     f"will likely error in Desktop, fix before shipping:")
        for w in e["qa"]["dax_warnings"]:
            lines.append(f"      - `{w['table']}[{w['field']}]`: {w['warning']}")
    if e["qa"]["unaccounted_count"]:
        lines.append(f"- [ ] **{e['qa']['unaccounted_count']} unaccounted field(s)**: "
                     f"{e['qa']['unaccounted_note']}")
    lines.append("- [ ] Run `tools/pbip_rules/lint.py <project> --schemas` -- structural checks "
                 "Desktop reports badly or not at all.")
    lines.append("- [ ] Open the model in Power BI Desktop and spot-check the pages that use any "
                 "already-resolved field above against the original Tableau workbook.")
    lines.append("")
    return "\n".join(lines)


def render_summary_md(reports):
    n = len(reports)
    total_counts = collections.Counter()
    total_hours = collections.Counter()
    total_hours_mandatory = collections.Counter()
    total_hours_optional = collections.Counter()
    reason_totals = collections.OrderedDict()
    per_wb_rows = []
    for e in reports:
        for k, v in e["counts"].items():
            total_counts[k] += v
        for k, v in e["hours"].items():
            total_hours[k] += v
        for k, v in e["hours_mandatory"].items():
            total_hours_mandatory[k] += v
        for k, v in e["hours_optional"].items():
            total_hours_optional[k] += v
        for r in e["reason_breakdown"]:
            key = r["label"]
            if key not in reason_totals:
                reason_totals[key] = {"label": r["label"], "suggested_fix": r["suggested_fix"],
                                       "count": 0, "workbooks": set()}
            reason_totals[key]["count"] += r["count"]
            reason_totals[key]["workbooks"].add(e["workbook"])
        per_wb_rows.append((e["workbook"], e["qa"]["already_resolved_count"],
                             e["counts"]["recommended_prompt"],
                             e["counts"]["raw_prompt"] + e["counts"]["manual_structural"],
                             e["counts"]["visual_undecided"] + e["counts"]["zone_gap"]
                             + e["counts"]["declined_encoding"],
                             e["hours_mandatory"]["total"], e["hours_optional"]["total"],
                             e["hours"]["total"]))

    lines = [f"# Effort Summary -- {n} workbook(s)", ""]
    lines.append(
        "One row per workbook below; totals first. Hours use the same assumed per-item minutes "
        "as every EFFORT_REPORT.md (`effort_report.py`'s `MINUTES_PER_ITEM`) -- replace those "
        "constants with real timing data as it becomes available, then re-run to refresh this "
        "number.")
    lines.append("")
    lines.append(
        "**Mandatory** = placed on a real worksheet visual or filter somewhere in that workbook "
        "-- something visible in the report is wrong or missing until fixed. **Optional** = no "
        "current report impact (an internal ingredient another calc references, or truly "
        "orphaned) -- the same `field_has_report_impact()` check each workbook's own "
        "COPILOT_FIX_PROMPTS.md \"No Current Report Impact\" section uses. Plan delivery dates "
        "against the mandatory total; optional items don't block anything currently visible.")
    lines.append("")
    lines.append("## Totals")
    lines.append("")
    lines.append("| Bucket | Count (all workbooks) | Mandatory hrs | Optional hrs |")
    lines.append("|---|---|---|---|")
    lines.append(f"| Already resolved | {sum(e['qa']['already_resolved_count'] for e in reports)} "
                 f"| 0 | 0 |")
    lines.append(f"| Copilot prompt with recommendation | {total_counts['recommended_prompt']} "
                 f"| {round(total_hours_mandatory['recommended_prompt'], 1)} "
                 f"| {round(total_hours_optional['recommended_prompt'], 1)} |")
    lines.append(f"| Copilot prompt, no recommendation | {total_counts['raw_prompt']} "
                 f"| {round(total_hours_mandatory['raw_prompt'], 1)} "
                 f"| {round(total_hours_optional['raw_prompt'], 1)} |")
    lines.append(f"| Structural/model-design | {total_counts['manual_structural']} "
                 f"| {round(total_hours_mandatory['manual_structural'], 1)} "
                 f"| {round(total_hours_optional['manual_structural'], 1)} |")
    lines.append(f"| Visual type undecided | {total_counts['visual_undecided']} "
                 f"| {round(total_hours_mandatory['visual_undecided'], 1)} "
                 f"| {round(total_hours_optional['visual_undecided'], 1)} |")
    lines.append(f"| Dropped zones | {total_counts['zone_gap']} "
                 f"| {round(total_hours_mandatory['zone_gap'], 1)} "
                 f"| {round(total_hours_optional['zone_gap'], 1)} |")
    lines.append(f"| Color/Size encoding not reproduced | {total_counts['declined_encoding']} "
                 f"| {round(total_hours_mandatory['declined_encoding'], 1)} "
                 f"| {round(total_hours_optional['declined_encoding'], 1)} |")
    buckets_6 = ("recommended_prompt", "raw_prompt", "manual_structural",
                 "visual_undecided", "zone_gap", "declined_encoding")
    grand_total_mandatory = round(sum(total_hours_mandatory[k] for k in buckets_6), 1)
    grand_total_optional = round(sum(total_hours_optional[k] for k in buckets_6), 1)
    grand_total = round(sum(total_hours[k] for k in buckets_6), 1)
    lines.append(f"| **Total remaining** | | **{grand_total_mandatory}** "
                 f"| **{grand_total_optional}** |")
    lines.append("")
    lines.append(f"**Grand total (mandatory + optional): {grand_total} hours.** Mandatory alone: "
                 f"**{grand_total_mandatory} hours** -- at 6 productive hours/day, that is "
                 f"**~{round(grand_total_mandatory / 6, 1)} person-days** across this batch, at "
                 f"the assumed per-item rate. Optional adds **{grand_total_optional} hours** "
                 f"(**~{round(grand_total_optional / 6, 1)} person-days**) on top if the team "
                 f"chooses to clear it too.")
    lines.append("")

    total_dax_warnings = sum(len(e["qa"]["dax_warnings"]) for e in reports)
    total_unaccounted = sum(e["qa"]["unaccounted_count"] for e in reports)
    lines.append("## QA rollup")
    lines.append("")
    lines.append(f"- **{total_dax_warnings}** static DAX warning(s) across the batch -- these "
                 "will likely error in Desktop; see each workbook's own EFFORT_REPORT.md.")
    if total_unaccounted:
        lines.append(f"- **{total_unaccounted}** unaccounted field(s) -- a build-consistency bug, "
                     "not a content gap; investigate before trusting any score from this batch.")
    lines.append("")

    if reason_totals:
        top = sorted(reason_totals.values(), key=lambda r: -r["count"])[:15]
        lines.append("## Top recurring reasons across the batch (fix once, benefit every workbook)")
        lines.append("")
        lines.append("| Count | Workbooks affected | Reason | Suggested fix |")
        lines.append("|---|---|---|---|")
        for r in top:
            fix = r["suggested_fix"].replace("\n", " ")
            if len(fix) > 120:
                fix = fix[:120].rsplit(" ", 1)[0] + "..."
            lines.append(f"| {r['count']} | {len(r['workbooks'])} | {r['label']} | {fix} |")
        lines.append("")

    lines.append("## Per-workbook")
    lines.append("")
    lines.append("| Workbook | Already resolved | Recommended prompt | Manual | Presentation | "
                 "Mandatory hrs | Optional hrs | Total hrs |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, resolved, rec, man, pres, hrs_m, hrs_o, hrs in sorted(per_wb_rows,
                                                                      key=lambda r: -r[7]):
        lines.append(f"| {name} | {resolved} | {rec} | {man} | {pres} | {hrs_m} | {hrs_o} "
                     f"| {hrs} |")
    lines.append("")
    return "\n".join(lines)


def render_corpus_rollup_md(grouped):
    """`grouped`: OrderedDict group_name -> list of per-workbook effort dicts (from
    compute_effort()). One row per group -- same counts_mandatory/counts_optional and
    hours_mandatory/hours_optional every per-workbook EFFORT_REPORT.md already carries, just
    rolled up by corpus group instead of by workbook, and rendered as two separate tables
    (mandatory, optional) instead of interleaved columns.

    Mapping % is a whole-model build metric (share of this group's calculated fields that
    actually emitted into the model, from validate_mapping.build_mapping()'s own mapped/gaps
    counts) -- it is NOT split mandatory/optional, since it measures translation success, not
    remaining work."""
    def _table(kind, label):
        counts_key, hours_key = f"counts_{kind}", f"hours_{kind}"
        lines = [f"### {label}", "",
                 "| Group | Wkbks | Mapping % | Copilot Candidate | Manual DAX | "
                 "Manual Structural | Visual Undecided | Zone Gap | Declined Encoding | Total | "
                 "Hours | Estimated Avg Hrs / Workbook |",
                 "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for group_name, reports in grouped.items():
            n_wb = len(reports)
            mapped_total = sum(e["mapping"]["mapped"] for e in reports)
            gaps_total = sum(e["mapping"]["gaps"] for e in reports)
            denom = mapped_total + gaps_total
            mapping_pct = round(100 * mapped_total / denom) if denom else 0
            copilot_candidate = sum(e[counts_key]["recommended_prompt"] for e in reports)
            manual_dax = sum(e[counts_key]["raw_prompt"] for e in reports)
            manual_structural = sum(e[counts_key]["manual_structural"] for e in reports)
            visual_undecided = sum(e[counts_key]["visual_undecided"] for e in reports)
            zone_gap = sum(e[counts_key]["zone_gap"] for e in reports)
            declined_encoding = sum(e[counts_key]["declined_encoding"] for e in reports)
            total = (copilot_candidate + manual_dax + manual_structural + visual_undecided
                     + zone_gap + declined_encoding)
            hours = round(sum(e[hours_key]["total"] for e in reports), 2)
            avg_hrs = round(hours / n_wb, 2) if n_wb else 0
            lines.append(f"| {group_name} | {n_wb} | {mapping_pct}% | {copilot_candidate} "
                         f"| {manual_dax} | {manual_structural} | {visual_undecided} "
                         f"| {zone_gap} | {declined_encoding} | {total} | {hours} | {avg_hrs} |")
        lines.append("")
        return lines

    lines = ["# Corpus Rollup by Group", "",
             "Same buckets as EFFORT_SUMMARY.md's Totals table, grouped by corpus instead of by "
             "workbook, and split into a separate Mandatory and Optional table instead of "
             "side-by-side columns -- **Mandatory** = placed on a real worksheet visual/filter "
             "somewhere in that workbook; **Optional** = no current report impact. See any "
             "workbook's own EFFORT_REPORT.md for what each bucket means.", ""]
    lines.extend(_table("mandatory", "Mandatory Effort by Group"))
    lines.extend(_table("optional", "Optional Effort by Group"))
    return "\n".join(lines)


# ==================================================================== CLI
def main():
    parser = argparse.ArgumentParser(
        description="Effort/QA report: how much manual work is left, and who can do it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single workbook build folder (writes EFFORT_REPORT.md in place)
  %(prog)s /path/to/output/dummy_pbip/MyDashboard

  # Parent directory of many workbook folders (also writes EFFORT_SUMMARY.md at the root)
  %(prog)s /path/to/output/dummy_pbip

  # Redirect output elsewhere
  %(prog)s /path/to/output/dummy_pbip --out /path/to/reports
""",
    )
    parser.add_argument("input", help="Workbook build folder, or parent directory of several")
    parser.add_argument("--out", help="Write reports under <out>/ instead of in place")
    args = parser.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    workbooks = find_residue_workbooks(root)
    if not workbooks:
        print(f"error: no _work/residue.json found under {root}", file=sys.stderr)
        sys.exit(2)

    reports = []
    groups = collections.OrderedDict()
    for wb_dir, _residue_path in workbooks:
        workbook_name = os.path.basename(wb_dir)
        e = compute_effort(wb_dir)
        reports.append(e)

        rel = os.path.relpath(wb_dir, root)
        first_segment = rel.split(os.sep)[0] if rel != "." else workbook_name
        group_name = first_segment[:-4] if first_segment.endswith("_run") else first_segment
        e["group"] = group_name
        groups.setdefault(group_name, []).append(e)

        out_dir = os.path.join(os.path.abspath(args.out), workbook_name) if args.out else wb_dir
        os.makedirs(out_dir, exist_ok=True)
        md_path = os.path.join(out_dir, "EFFORT_REPORT.md")
        with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_report_md(e))
        with open(os.path.join(out_dir, "effort_report.json"), "w", encoding="utf-8") as fh:
            json.dump(e, fh, indent=2, default=list)

        h, hm, ho = e["hours"], e["hours_mandatory"], e["hours_optional"]
        print(f"[ok] {workbook_name}  already_resolved={e['qa']['already_resolved_count']} "
              f"recommended_prompt={e['counts']['recommended_prompt']} "
              f"raw_prompt={e['counts']['raw_prompt']} "
              f"manual_structural={e['counts']['manual_structural']} "
              f"visual_undecided={e['counts']['visual_undecided']} "
              f"zone_gap={e['counts']['zone_gap']} "
              f"declined_encoding={e['counts']['declined_encoding']} "
              f"blend_key_gap={e['counts']['blend_key_gap']} "
              f"est_hours={h['total']} (mandatory={hm['total']}, optional={ho['total']})  "
              f"-> {md_path}")

    if len(reports) > 1:
        out_root = os.path.abspath(args.out) if args.out else root
        os.makedirs(out_root, exist_ok=True)
        summary_md_path = os.path.join(out_root, "EFFORT_SUMMARY.md")
        with open(summary_md_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_summary_md(reports))
        with open(os.path.join(out_root, "effort_summary.json"), "w", encoding="utf-8") as fh:
            json.dump(reports, fh, indent=2, default=list)
        print(f"\nWritten batch rollup to {summary_md_path}")

        if len(groups) > 1:
            rollup_md_path = os.path.join(out_root, "EFFORT_CORPUS_ROLLUP.md")
            with open(rollup_md_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_corpus_rollup_md(groups))
            print(f"Written corpus rollup ({len(groups)} group(s)) to {rollup_md_path}")

    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # self-test: classify_items() as a pure function, plus the discovery/rendering helpers.
        residue = {
            "calc_residue": [
                {"table": "T", "subject": "A", "tier": "L1",
                 "reason": "Tableau STARTSWITH(s,sub) has no DAX equivalent",
                 "formula": "STARTSWITH([x], \"y\")"},
                {"table": "T", "subject": "B", "tier": "L2",
                 "reason": "Tableau STARTSWITH(s,sub) has no DAX equivalent",
                 "formula": "STARTSWITH([z], \"y\")"},
                {"table": "T", "subject": "C", "tier": "H",
                 "reason": "table calculation -- addressing/partitioning is not in the formula text",
                 "formula": "WINDOW_SUM([x])"},
                {"table": "T", "subject": "D", "tier": "L3",
                 "reason": "something never seen before in FIX_RULES",
                 "formula": "SOME_UNKNOWN_FN([x])"},
                {"table": "T", "subject": "E", "tier": "D",
                 "reason": "free-entry parameter domain has no fixed set of values"},
            ],
            "visual_undecided": [{"worksheet": "W1", "page": "P1", "mark": "Automatic",
                                   "emitted": "tableEx", "reason": "no rule fired for mark"}],
            "zone_gaps": [{"zone": "Z1", "page": "P1", "reason": "every shelf field unresolved"}],
            "declined_encodings": [{"page": "P1", "visual": "V1", "field": "F1",
                                     "reason": "Legend/Series role rejected"}],
        }
        result = classify_items(residue)
        # A, B: same STARTSWITH reason, a FIX_RULES pattern matches -> recommended_prompt.
        # C: "table calculation" also matches a FIX_RULES pattern -> recommended_prompt.
        # D: no rule matches this reason at all (GENERIC_FIX) -> raw_prompt.
        assert result["counts"]["recommended_prompt"] == 3, result["counts"]  # A, B, C
        assert result["counts"]["raw_prompt"] == 1, result["counts"]  # D
        assert result["counts"]["manual_structural"] == 1, result["counts"]  # E, no formula
        assert result["counts"]["visual_undecided"] == 1
        assert result["counts"]["zone_gap"] == 1
        assert result["counts"]["declined_encoding"] == 1
        assert result["hours"]["recommended_prompt"] == round(3 * 15 / 60.0, 2)
        assert result["hours"]["raw_prompt"] == round(1 * 30 / 60.0, 2)
        assert result["hours"]["total"] > 0

        # No field_to_ws given -> every item defaults to mandatory (absence of evidence is never
        # grounds to demote something to optional -- see classify_items' own docstring).
        assert result["counts_mandatory"] == result["counts"], result["counts_mandatory"]
        assert all(v == 0 for v in result["counts_optional"].values()), result["counts_optional"]
        assert result["hours_mandatory"]["total"] == result["hours"]["total"]
        assert result["hours_optional"]["total"] == 0

        # With field_to_ws given: A is placed on a real worksheet shelf (mandatory), B/C/D/E are
        # not (optional) -- the exact same field_has_report_impact() check
        # COPILOT_FIX_PROMPTS.md's own "No Current Report Impact" section is built from, so the
        # two documents can never disagree about which of these is load-bearing today.
        field_to_ws = {"A": {("Worksheet1", "visual")}}
        result2 = classify_items(residue, field_to_ws=field_to_ws)
        assert result2["counts_mandatory"]["recommended_prompt"] == 1, result2["counts_mandatory"]
        assert result2["counts_optional"]["recommended_prompt"] == 2, result2["counts_optional"]
        assert result2["counts_mandatory"]["raw_prompt"] == 0
        assert result2["counts_optional"]["raw_prompt"] == 1
        assert result2["counts_mandatory"]["manual_structural"] == 0
        assert result2["counts_optional"]["manual_structural"] == 1
        # visual_undecided/zone_gap/declined_encoding are never split -- always fully mandatory.
        assert result2["counts_mandatory"]["visual_undecided"] == 1
        assert result2["counts_optional"]["visual_undecided"] == 0
        assert result2["counts_mandatory"]["zone_gap"] == 1
        assert result2["counts_optional"]["zone_gap"] == 0
        assert result2["counts_mandatory"]["declined_encoding"] == 1
        assert result2["counts_optional"]["declined_encoding"] == 0
        # Total count is unchanged either way -- the split only re-labels, never drops, an item.
        assert result2["counts"] == result["counts"]
        assert result2["hours"]["total"] == result["hours"]["total"]
        assert result2["hours_mandatory"]["total"] < result2["hours"]["total"]
        assert result2["hours_optional"]["total"] > 0
        assert round(result2["hours_mandatory"]["total"] + result2["hours_optional"]["total"], 2) \
            == result2["hours"]["total"]

        # A/B/C in recommended_prompt, D alone in raw_prompt, each with a distinguishing "why".
        recommended_subjects = {it["subject"] for it in result["recommended_prompt"]}
        assert recommended_subjects == {"A", "B", "C"}, recommended_subjects
        raw_subjects = {it["subject"] for it in result["raw_prompt"]}
        assert raw_subjects == {"D"}, raw_subjects
        assert "no FIX_RULES pattern" in result["raw_prompt"][0]["why"]

        # reason_breakdown groups B and A's shared STARTSWITH reason among manual items.
        labels = [r["label"] for r in result["reason_breakdown"]]
        assert any("STARTSWITH" in l or "startswith" in l.lower() for l in labels)

        md = render_report_md({"workbook": "T", **result,
                                "qa": {"already_resolved_count": 2,
                                       "already_resolved_note": "n",
                                       "dax_warnings": [], "unaccounted_count": 0,
                                       "unaccounted_note": "n"}})
        assert "Effort Report -- T" in md
        assert "Total remaining" in md

        summary = render_summary_md([{"workbook": "T", **result,
                                       "qa": {"already_resolved_count": 2, "dax_warnings": [],
                                              "unaccounted_count": 0}}])
        assert "Effort Summary -- 1 workbook" in summary
        assert "Grand total (mandatory + optional)" in summary
        assert "Mandatory hrs" in summary and "Optional hrs" in summary

        e_with_mapping = {"workbook": "T", **result, "mapping": {"mapped": 18, "gaps": 2},
                           "qa": {"already_resolved_count": 0, "dax_warnings": [],
                                  "unaccounted_count": 0}}
        rollup = render_corpus_rollup_md(collections.OrderedDict(
            [("test_corpus", [e_with_mapping]), ("cit_corpus", [e_with_mapping, e_with_mapping])]))
        assert "Mandatory Effort by Group" in rollup and "Optional Effort by Group" in rollup
        assert "| test_corpus | 1 | 90% |" in rollup
        assert "| cit_corpus | 2 | 90% |" in rollup

        print("effort_report.py self-test OK (classification, hours math, rendering)")
    else:
        main()
