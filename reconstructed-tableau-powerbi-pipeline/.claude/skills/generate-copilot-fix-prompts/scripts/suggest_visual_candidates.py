#!/usr/bin/env python3
"""
Triage a workbook's presentation-layer residue (`visual_undecided` + `zone_gaps`) into what
actually needs a human, and -- for exactly one well-evidenced shape -- a real suggested fix.

Why this is a DIFFERENT shape of problem than the DAX gaps
-----------------------------------------------------------
Power BI Copilot is a DAX/M-writing assistant; it has no visual-layout capability, so
`generate_copilot_prompts.py`'s "hand the SME a ready-to-paste Copilot prompt" pattern for
measures does not transfer here by swapping in a different tool. What DOES transfer is the shape
of the answer: separate what is genuinely undecidable without a human from what is not, and only
claim the second bucket.

Splitting `visual_undecided` (56 characters: pbir.py's own words, "no rule fired for mark") into
three real buckets, all computed from data pbir.py already has (`_shelf_shape`/`choose_visual`,
imported directly, not reimplemented):

  1. SUGGESTED -- rows AND cols are BOTH empty. pbir.py's own `_shelf_shape` docstring already
     establishes this exact shape is Tableau's text-table/BAN-card idiom (documented against a
     real corpus workbook: 239 Text-channel encodings vs 50 rows/cols encodings combined). The
     mark being 'Automatic' rather than literal 'Text' does not change what CAN render: with
     nothing on Rows or Columns, there is no axis for a bar/line/pie/etc mark to plot against --
     Automatic can only fall back to the same text display an explicit Text mark would. This is
     the identical rule pbir.py's own VISUAL_RULES already applies for mark == "Text" (0
     dimensions + measures -> card, otherwise -> tableEx); this script applies the SAME two rules
     when mark == "Automatic" AND rows/cols are both empty, because the underlying reason those
     rules are correct (nothing to plot) is about the shelf shape, not the mark label. Measured
     against the 6-workbook corpus: 68 of 122 visual_undecided items (56%) are exactly this shape.

  2. NEEDS TABLEAU LOOKUP -- everything else. A single dimension alone on Rows or Columns with
     no measure, multiple measures with no clear axis assignment, and so on -- these genuinely
     depend on what Tableau's Automatic-mark algorithm rendered from the live data, which is not
     recoverable from shelf counts alone. No canned rule is proposed; the SME opens the real
     worksheet in Tableau and picks the matching Power BI visual by hand -- same as
     `generate_copilot_prompts.py`'s existing manual-fix note, just now separated from bucket 1
     instead of mixed into one undifferentiated list.

  3. `zone_gaps` split into CASCADE (the visual's mark/shape DID resolve; it disappeared only
     because a shelf field it depends on is a declined calc -- these recover for free, with NO
     new action, once that calc's DAX is fixed by hand per COPILOT_FIX_PROMPTS.md and the
     workbook rebuilds) vs STRUCTURAL (the Tableau zone TYPE itself -- 'dashboard-object',
     'empty', and similar -- has no Power BI construct at all, chrome/embedded-object zones that
     no rule, no LLM, and no Copilot can produce; the SME manually recreates the equivalent in
     Desktop or accepts it is dropped).

Writes NOTHING into the real report -- this is staged, review-first output. Bucket 1's suggestion
is a `visualType` string + rationale for a human to apply themselves in Desktop, not a PBIR JSON
container this script emits.

Usage:
  python3 suggest_visual_candidates.py <build-tableau-pbip or generate-dummy-data output> [--out <folder>]
"""
import os
import re
import sys
import json
import argparse

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_SKILL_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "build-tableau-pbip", "scripts")
)
sys.path.insert(0, _BUILD_SKILL_SCRIPTS)
from pbir import _shelf_shape  # noqa: E402


# ============================================================== bucket 1: suggested
def suggest_for_empty_rows_cols(shape):
    """Mirrors pbir.py's own Text-mark rules exactly, for the shape (rows and cols both empty)
    those rules were themselves written for -- see module docstring. Returns (visualType, why)
    or None if this shape does not qualify (rows or cols is non-empty)."""
    if shape["n_rows"] != 0 or shape["n_cols"] != 0:
        return None
    if shape["n_dims"] == 0 and shape["n_measures"] >= 1:
        return ("card", "Rows and Columns are both empty, so there is no axis for any mark to "
                        "plot against -- Tableau's Automatic mark falls back to the Text shelf, "
                        "exactly the shape pbir.py's own mark=='Text' rule already covers "
                        "(0 dimensions, measures present -> card/BAN tile).")
    if shape["n_dims"] == 1 and shape["n_measures"] == 0:
        # The one shape pbir.py's own empty-shelf rule deliberately REFUSES to decide (see its
        # comment there): a single dimension alone on Text is the header/label-tile idiom, and a
        # one-column table of repeated values is very possibly not what the author wanted. So the
        # suggestion here names the real choice a reviewer has to make rather than pretending
        # there is one obvious answer.
        return ("textbox OR tableEx",
                "A single dimension on Text with nothing on Rows/Columns is Tableau's "
                "header/label-tile idiom. Decide which this is: if it displays ONE fixed value "
                "as a caption, rebuild it as a textbox (or a card) -- a `tableEx` would show a "
                "column of repeated values; if it really is a one-column list, `tableEx` (what "
                "was emitted) is already correct. Left deliberately undecided by the build for "
                "exactly this reason.")
    return ("tableEx", "Rows and Columns are both empty, so there is no axis for any mark to "
                       "plot against -- Tableau's Automatic mark falls back to the Text shelf, "
                       "exactly the shape pbir.py's own mark=='Text' rule already covers "
                       "(dimensions present -> text table).")


# ============================================================== bucket 3: zone_gaps split
_CASCADE_RE = re.compile(
    r"resolved to visual type '.*?' \(.*?\) but every shelf field it references failed to "
    r"resolve to a model object|"
    r"^(?:color|paramctrl|filter) zone's field '.*?' did not resolve to a model object"
)


def split_zone_gaps(zone_gaps):
    cascade, structural = [], []
    for z in zone_gaps:
        (cascade if _CASCADE_RE.search(z.get("reason", "")) else structural).append(z)
    return cascade, structural


_CASCADE_FIELD_RE = re.compile(r"field '([^']+)' did not resolve|references failed to resolve")


def load_page_titles(wb_dir):
    """page id (the 20-hex-char folder/file name) -> its real Tableau dashboard name.

    PBIR writes this itself: every `<Workbook>.Report/definition/pages/<id>/page.json` carries a
    `displayName` -- the id is only a stable hash (`pbir.py`'s own `pbir_name()`), never meant to
    be read by a human. Globs for the `*.Report` folder rather than assuming it's named after
    `wb_dir`'s own basename, since a workbook folder and its `.Report` name can legitimately
    differ (e.g. a copy/rename of the build output)."""
    import glob
    titles = {}
    for report_dir in glob.glob(os.path.join(wb_dir, "*.Report")):
        pages_dir = os.path.join(report_dir, "definition", "pages")
        for page_json in glob.glob(os.path.join(pages_dir, "*", "page.json")):
            try:
                with open(page_json, encoding="utf-8") as fh:
                    page = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if page.get("name") and page.get("displayName"):
                titles[page["name"]] = page["displayName"]
    return titles


# ============================================================== per-workbook run
def analyze_workbook(wb_dir):
    work = os.path.join(wb_dir, "_work")
    with open(os.path.join(work, "ir.json"), encoding="utf-8") as fh:
        ir = json.load(fh)
    residue_path = os.path.join(work, "residue.json")
    if not os.path.isfile(residue_path):
        return None
    with open(residue_path, encoding="utf-8") as fh:
        residue = json.load(fh)

    by_name = {}
    for w in ir.get("worksheets", []):
        by_name.setdefault(w["name"], w)

    suggested, needs_lookup = [], []
    for item in residue.get("visual_undecided", []):
        ws = by_name.get(item["worksheet"])
        if ws is None:
            needs_lookup.append({**item, "why_no_suggestion": "worksheet not found in ir.json"})
            continue
        shape = _shelf_shape(ws)
        hit = suggest_for_empty_rows_cols(shape)
        if hit:
            vt, why = hit
            suggested.append({**item, "suggested_visual": vt, "why": why, "shape": shape})
        else:
            needs_lookup.append({**item, "shape": shape})

    cascade, structural = split_zone_gaps(residue.get("zone_gaps", []))

    return {"suggested": suggested, "needs_lookup": needs_lookup,
            "cascade": cascade, "structural": structural,
            "page_titles": load_page_titles(wb_dir)}


def build_review_md(workbook_name, result):
    s, nl, cascade, structural = (result["suggested"], result["needs_lookup"],
                                   result["cascade"], result["structural"])
    page_titles = result.get("page_titles", {})

    def page_label(page_id):
        return page_titles.get(page_id, page_id)
    lines = [
        f"# Presentation-layer review — {workbook_name}",
        "",
        "Power BI Copilot has no visual-layout capability, so nothing here was 'authored by "
        "Copilot' the way the DAX candidates were -- this is a TRIAGE: what's genuinely "
        "undecidable without a human, what will self-resolve for free, and the one shape with a "
        "real, evidenced suggestion.",
        "",
        f"## 1. Suggested visual type ({len(s)})",
        "",
        "Rows and Columns are both empty for every item below, which is Tableau's own "
        "text-table/BAN-card idiom regardless of the recorded mark -- see this script's own "
        "module docstring for why that's a rule, not a guess. Still needs an SME to build the "
        "actual visual in Desktop and confirm it matches; nothing has been written to the "
        "report.",
        "",
        "| Page | Worksheet | Suggested visual | Why |",
        "|---|---|---|---|",
    ]
    for it in sorted(s, key=lambda i: (page_label(i["page"]), i["worksheet"])):
        lines.append(f"| {page_label(it['page'])} | {it['worksheet']} | "
                     f"{it['suggested_visual']} | {it['why']} |")

    lines += [
        "",
        f"## 2. Needs a Tableau lookup ({len(nl)})",
        "",
        "No canned rule applies -- open each worksheet in the real Tableau workbook to see what "
        "it actually renders, then pick the matching Power BI visual by hand in Desktop.",
        "",
        "| Page | Worksheet | Mark | Shape | Reason |",
        "|---|---|---|---|---|",
    ]
    for it in sorted(nl, key=lambda i: (page_label(i["page"]), i["worksheet"])):
        shape = it.get("shape")
        shape_txt = (f"{shape['n_dims']}dim/{shape['n_measures']}meas "
                     f"rows={shape['n_rows']} cols={shape['n_cols']}") if shape else "n/a"
        lines.append(f"| {page_label(it['page'])} | {it['worksheet']} | {it.get('mark', 'n/a')} "
                     f"| {shape_txt} | {it['reason']} |")

    lines += [
        "",
        f"## 3. Zone gaps — self-resolving cascades ({len(cascade)})",
        "",
        "The visual's own mark/shape already resolved; it's missing only because a shelf field "
        "it depends on is a declined calc. No new action needed here -- once that calc's DAX is "
        "written by hand (see COPILOT_FIX_PROMPTS.md for the fix prompt) and the project "
        "rebuilds, these come back automatically.",
        "",
        "| Page | Zone | Reason |",
        "|---|---|---|",
    ]
    for z in sorted(cascade, key=lambda z: page_label(z["page"])):
        lines.append(f"| {page_label(z['page'])} | {z.get('zone') or '(unnamed)'} | "
                     f"{z['reason']} |")

    lines += [
        "",
        f"## 4. Zone gaps — structural, no Power BI construct ({len(structural)})",
        "",
        "The Tableau zone TYPE itself has no Power BI equivalent (embedded web objects, "
        "dashboard-object containers, spacer zones, ...) -- no rule, no LLM, and no Copilot "
        "produces one, because there is nothing to translate TO. The SME manually recreates the "
        "equivalent in Desktop, or accepts it's dropped.",
        "",
        "| Page | Zone | Reason |",
        "|---|---|---|",
    ]
    for z in sorted(structural, key=lambda z: page_label(z["page"])):
        lines.append(f"| {page_label(z['page'])} | {z.get('zone') or '(unnamed)'} | "
                     f"{z['reason']} |")
    lines.append("")

    return "\n".join(lines)


# ============================================================== discovery / CLI
def find_residue_workbooks(root):
    if os.path.isfile(os.path.join(root, "_work", "residue.json")):
        return [root]
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == "_work" and "residue.json" in filenames:
            found.append(os.path.dirname(dirpath))
    return sorted(found)


def _self_test():
    shape_empty_no_dims = {"n_rows": 0, "n_cols": 0, "n_dims": 0, "n_measures": 2}
    vt, _why = suggest_for_empty_rows_cols(shape_empty_no_dims)
    assert vt == "card", vt

    shape_empty_with_dims = {"n_rows": 0, "n_cols": 0, "n_dims": 2, "n_measures": 0}
    vt, _why = suggest_for_empty_rows_cols(shape_empty_with_dims)
    assert vt == "tableEx", vt

    shape_not_empty = {"n_rows": 1, "n_cols": 0, "n_dims": 1, "n_measures": 0}
    assert suggest_for_empty_rows_cols(shape_not_empty) is None

    cascade, structural = split_zone_gaps([
        {"page": "p", "zone": "z1",
         "reason": "worksheet 'W' resolved to visual type 'tableEx' (some rule) but every shelf "
                   "field it references failed to resolve to a model object -- nothing to bind, "
                   "so the visual is dropped rather than emitted blank"},
        {"page": "p", "zone": "z2", "reason": "zone type 'dashboard-object' has no mapped Power "
                                              "BI visual"},
        {"page": "p", "zone": "z3", "reason": "color zone's field 'X' did not resolve to a "
                                              "model object -- a slicer with nothing to slice "
                                              "would render as an empty control"},
    ])
    assert len(cascade) == 2 and len(structural) == 1, (cascade, structural)

    print("suggest_visual_candidates.py self-test OK")


def main():
    if len(sys.argv) == 1:
        _self_test()
        return

    parser = argparse.ArgumentParser(
        description="Triage a workbook's presentation-layer residue",
    )
    parser.add_argument("input", help="Workbook build folder, or parent directory of several")
    parser.add_argument("--out", help="Write outputs under <out>/<Workbook>/ instead of in place")
    args = parser.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    workbooks = find_residue_workbooks(root)
    if not workbooks:
        print(f"error: no _work/residue.json found under {root}", file=sys.stderr)
        sys.exit(2)

    for wb_dir in workbooks:
        workbook_name = os.path.basename(wb_dir)
        result = analyze_workbook(wb_dir)
        if result is None:
            continue

        if args.out:
            out_dir = os.path.join(os.path.abspath(args.out), workbook_name)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = wb_dir

        with open(os.path.join(out_dir, "PRESENTATION_LAYER_REVIEW.md"), "w", encoding="utf-8",
                   newline="\n") as fh:
            fh.write(build_review_md(workbook_name, result))

        print(f"[ok] {workbook_name:50s} suggested={len(result['suggested']):4d} "
              f"needs_lookup={len(result['needs_lookup']):4d} "
              f"cascade={len(result['cascade']):4d} "
              f"structural={len(result['structural']):4d}  -> {out_dir}")

    sys.exit(0)


if __name__ == "__main__":
    main()
