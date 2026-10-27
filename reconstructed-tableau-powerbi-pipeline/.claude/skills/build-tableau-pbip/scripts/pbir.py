#!/usr/bin/env python3
"""
S4 -- the PBIR report writer.

One Tableau dashboard becomes one PBIR page; one dashboard zone becomes one visual container.
Geometry is the easy half and is exact. The hard half is `visualType`, and this file's design is
mostly about not lying when the mark type does not determine it.

Why a Tableau mark does not determine a Power BI visual type
-----------------------------------------------------------
62% of worksheets in the corpus carry mark class `Automatic`, which is not a value in the file at
all -- Tableau resolves it at render time from the shelf field types. So `Automatic` is genuinely
absent information, not a mark to look up. The rule table below therefore keys on
(mark, shelf shape), and when no rule fires the view goes onto a REVIEW list and the container is
emitted as `tableEx`.

`tableEx` is chosen as the fallback deliberately: a table shows every field that was on the shelves
and is obviously not a finished chart, so a reader sees unfinished work. A plausible-looking
`barChart` would hide the fact that nothing decided it.

Field wells are the least-verified thing here
---------------------------------------------
No visual in docs/examples/ carries a `queryState`, so the projection shape is schema-doc only.
That is why `--wells` defaults to on but every emitted well is counted separately in the report:
if Desktop rejects the projection shape, the count says exactly how much output is affected.
"""
import os
import re
import json
import math
import hashlib
import collections

from names import dax_ref  # noqa: F401  (kept for symmetry of the binding contract)
from tmdl import fkey
from dax import lex as _dax_lex

SCHEMA = {
    "version": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
    "report": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.1.0/schema.json",
    "pages": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
    "page": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json",
    "visual": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.6.0/schema.json",
    "pbir": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
    "pbism": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "pbip": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
}

# The base theme Desktop writes for this release. Shipped as a file AND declared as a
# SharedResources item, because a declared-but-absent theme is what the pre-flight now rejects.
BASE_THEME = "CY26SU02"


# PBIR page/visual names are 20 lowercase hex characters and MUST equal the folder name -- true for
# all six names in the Desktop-authored example. Derived from a hash so a re-run is byte-identical:
# a random id would rewrite every file on every build and make diffs unreadable.
def pbir_name(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:20]


# Tableau zone types that are chrome, not data. Emitting a visual for these produces containers no
# one wants; they are counted as dropped so the count is visible.
#
# 'empty' is a fixed-size spacer/divider Tableau's own layout inspector inserts between real
# content -- confirmed by walking every zone type-v2='empty' across the full 7-workbook reference
# corpus (440 zones total): not one carries a direct child beyond its own <zone-style> formatting
# block. Whatever visible content sits in the same screen area belongs to a SEPARATE, sibling or
# nested zone with its own type-v2 (a `text` title, a `paramctrl`, a real `worksheet`, ...), which
# extract_dashboards()'s `.iter("zone")` walk already visits and records as its own zone record --
# exactly the same relationship 'container'/'layout-flow' already have to their real-content
# children, which is why those are chrome-listed already. Before this, every 'empty' zone fell
# through to the generic "zone type 'empty' has no mapped Power BI visual" gap instead of the
# `chrome_zones_dropped` counter -- correct outcome (nothing was ever emitted for it), dishonest
# accounting (263 of these on WorkbookH alone were counted as "needs a human
# to look at it" for zones that were never anything to look at).
CHROME_ZONES = {"layout-basic", "layout-flow", "container", "title", "layout-tabbed",
                "layout-flipboard", "blank", "empty"}

# Non-worksheet zone kind -> visualType. These are exact: the zone type states the intent.
ZONE_VISUAL = {
    "paramctrl": "slicer",
    "filter": "slicer",
    "color": "slicer",
    "text": "textbox",
    "bitmap": "image",
    "web": "textbox",          # a web zone has no Power BI equivalent; see GAP note
}


def _shelf_shape(ws, calc_lookup=None):
    """Summarise the shelves the way the visual-type rules need to ask about them.

    Rows/Cols both empty is Tableau's own signal for the "text table"/BAN-card idiom: a
    worksheet that puts its ONLY field(s) on the Text/Label shelf and nothing on Rows or Cols
    at all -- extremely common for KPI-style dashboards built from many small single-value
    tiles. Measured on a real corpus workbook (WorkbookG):
    239 `text`-channel encodings against 50 `rows`/`cols` encodings combined -- the MAJORITY
    shape in that workbook, not an edge case, and every one of those worksheets fell to
    "0 dimension(s) and 0 measure(s)" (an honest-looking but wrong reading) before this,
    because n_dims/n_measures only ever looked at rows/cols. Falling back to the Text shelf
    ONLY when rows/cols are both silent means a real chart that legitimately uses rows/cols
    (Bar/Line/Pie/etc.) is never touched by this -- found by reading _query_state's real
    Desktop-facing output (pbip_lint.py's RPT-ROLE-02: 142 visuals binding no field at all on
    this same workbook), not by inspecting this function in isolation.
    """
    rows, cols = ws["shelves"].get("rows", []), ws["shelves"].get("cols", [])
    aggs = [e for e in (rows + cols) if e.get("agg")]
    dims = [e for e in (rows + cols) if not e.get("agg")]
    chans = {e["channel"] for e in ws["encodings"]}
    if not rows and not cols:
        text_fields = [e for e in ws["encodings"] if e["channel"] == "text"]
        aggs = aggs + [e for e in text_fields if e.get("agg")]
        dims = dims + [e for e in text_fields if not e.get("agg")]
        # Also count measure encodings on Color or Shape channels -- Tableau's up/down-triangle
        # KPI idiom (mark class 'Shape') places its single measure exclusively on these non-
        # positional channels with nothing on Rows or Cols at all (confirmed: 8 WorkbookA worksheets
        # "Sheet 8 - Vol YoY Marks", "Sheet 8 - Txn YoY Marks", etc.).  Only MEASURES (agg is truthy) are
        # included here; dims are deliberately excluded so `color_dims` stays undisturbed for
        # the color-as-Category barChart/columnChart rules (which require rows_measure or
        # cols_measure to be True -- impossible when rows/cols are empty, so those rules never
        # fire here regardless, but the separation prevents any accidental double-counting of
        # a color dimension in both n_dims and color_dims).
        aggs = aggs + [e for e in ws["encodings"]
                       if e["channel"] in ("color", "shape") and e.get("agg")]
    elif not aggs:
        # Rows/Cols resolved dimension(s) only, no measure at all -- a second, narrower Tableau
        # idiom the `not rows and not cols` branch above does not cover: a dimension on Rows
        # filtered down to a single value (a label, not a real grouping), with the actual
        # displayed number living on the Text shelf (e.g. COUNTD(RecId), a user calc). Found on
        # this exact shape across 7 real corpus worksheets ("Metric A (2)", "Metric B (2)",
        # "Qualified Leads (2)", "Completed Items", "Sub Category Items", "Sheet C Pipeline
        # (Won)", "Completed Seller Handoff (Overview)"): each read as "N dimension(s), 0
        # measure(s)" here, and downstream in `_query_state` the Values well ended up bound to
        # the bare filter dimension -- Desktop would show the filter's own text ("MQL") in place
        # of the count it labels, never the real number. Deliberately added only to `aggs`, never
        # `dims`: Rows/Cols already own the dimension count for a worksheet that legitimately
        # still has a real grouping dimension too (e.g. Sub Category Items' Meeting Sub
        # Category column), so this must not double-count that.
        text_fields = [e for e in ws["encodings"] if e["channel"] == "text"]
        aggs = aggs + [e for e in text_fields if e.get("agg")]
    # A Measure Names/Values pivot shelf field (e.g. "Sheet 1 - Table") has nothing real to resolve
    # by itself, so it counts as neither a dimension nor a measure above -- but extract.py's
    # `measure_names_pivot` already resolved the real, ordered measure list from the workbook's
    # own <groupfilter> spec (see its docstring). Non-empty here means _query_state below WILL
    # expand it into real projections, so this is a deliberate, decided outcome, not "no rule
    # fired" -- surfaced as its own VISUAL_RULES entry rather than falling to the generic
    # tableEx fallback with a misleading "0 measure(s)" reason.
    pivot = bool(ws.get("measure_names_pivot")) and any(
        ":Measure Names" in e["field"] or ":Measure Values" in e["field"] for e in rows + cols)
    # The COLOR shelf, split the same way rows/cols are (an `agg` means it is a measure). A
    # DISCRETE (dimension) field on Color is what makes a Tableau bar chart stacked -- that is
    # Tableau's documented default for the Bar mark, not an inference about this corpus -- so the
    # split, not merely "color is present", is what the stacked rules below need. A CONTINUOUS
    # (measure) field on Color is a colour GRADIENT and does not stack, which is exactly why
    # `channels` ("is there a colour encoding at all") could not answer this on its own.
    color_encs = [e for e in ws["encodings"] if e["channel"] == "color"]
    # `dim0_label`: set when there is exactly ONE dimension, NO measures, and the caller
    # supplied a `calc_lookup` dict (caption -> raw Tableau formula) that maps that
    # dimension's field caption to a bare string-literal formula.  None otherwise.
    # Used by the VISUAL_RULES string-literal entry to classify Tableau's nav-button
    # worksheet idiom (a tab-label whose entire content is a hardcoded string like
    # "Highlights Page", not a real data column) as a `textbox` rather than `tableEx`.
    # Deliberately absent from the shape dict when `calc_lookup is None` -- module-level
    # callers that do not pass formula access (e.g. the self-test's plain `choose_visual(ws)`)
    # get `s.get("dim0_label") == None`, which correctly prevents the rule from firing.
    dim0_label = None
    if calc_lookup is not None and len(dims) == 1 and not aggs:
        formula = calc_lookup.get(dims[0]["field"])
        dim0_label = _extract_string_literal(formula) if formula else None
    return {"n_rows": len(rows), "n_cols": len(cols), "n_measures": len(aggs),
            "n_dims": len(dims), "channels": chans,
            "rows_measure": bool(rows and rows[-1].get("agg")),
            "cols_measure": bool(cols and cols[-1].get("agg")),
            "color_dims": len([e for e in color_encs if not e.get("agg")]),
            "color_measures": len([e for e in color_encs if e.get("agg")]),
            "measure_names_pivot": pivot, "dual_axis_combo": _dual_axis_combo(ws),
            "dim0_label": dim0_label}


def _dual_axis_combo(ws):
    """Tableau's dual-axis bar+line combo: two measures dropped on the SAME shelf (Rows or
    Cols), the second promoted to its own synced axis via right-click "Dual Axis". The
    worksheet-level `ws['mark']` every other rule in this table reads is always 'Automatic' for
    this idiom -- it is the outer/default pane's own mark, never either measure's -- which is
    exactly why no rule above ever fired for it (confirmed on WorkbookB Dashboard's "Sheet 1 - Trend":
    RESIDUE.md logged "no rule fired for mark 'Automatic' with 1 dimension(s) and 2 measure(s)"
    before this). The REAL per-measure marks live one level down, on extract.py's own `panes`
    capture (`worksheet_panes` inside `extract_worksheets`): pane id='1' mark='Bar' with its own
    y-axis field, pane id='2' mark='Line' with its own. Returns None unless EXACTLY 2 identified
    sub-panes exist with EXACTLY the marks {'Bar', 'Line'} and each names a resolvable y-axis
    field -- any other pane count or mark combination declines, never guessed at.
    """
    panes = ws.get("panes") or []
    if len(panes) != 2:
        return None
    by_mark = {p.get("mark"): p for p in panes}
    if set(by_mark) != {"Bar", "Line"}:
        return None
    bar, line = by_mark["Bar"], by_mark["Line"]
    if not bar.get("y_axis_field") or not line.get("y_axis_field"):
        return None
    return {"Y": bar["y_axis_field"], "Y2": line["y_axis_field"]}


def _combo_role_for(ws, field_caption):
    """Which axis role (Y=bar, Y2=line) a dual-axis combo's own measure belongs to, matched by
    field identity against `_dual_axis_combo`'s per-pane mapping -- not by shelf position, which
    would be a guess (Tableau's own Rows-shelf text joins both measures with '+' in document
    order, not necessarily bar-then-line order). None means this field is not one of the two
    the combo shape confirmed -- the caller declines it rather than binding it to the wrong axis.
    """
    combo = _dual_axis_combo(ws) or {}
    for role, cap in combo.items():
        if cap == field_caption:
            return role
    return None


# (predicate, visualType, why). Ordered: the first match wins, so the specific marks come before
# the shape-derived ones.
VISUAL_RULES = [
    # Checked first, regardless of mark: a Measure Names/Values pivot is a deliberate crosstab
    # (each pivoted measure becomes its own table column) with a real, already-resolved measure
    # list behind it -- see _shelf_shape's own comment. tableEx is also this pipeline's plain
    # multi-column table type, so it is the correct (not merely fallback) choice here.
    (lambda m, s: s["measure_names_pivot"], "tableEx",
     "Measure Names/Values pivot -- each pivoted measure becomes its own column"),
    (lambda m, s: m == "Pie", "pieChart", "mark class Pie"),
    (lambda m, s: m == "Line", "lineChart", "mark class Line"),
    (lambda m, s: m == "Area", "areaChart", "mark class Area"),
    # Guarded on shelf shape, not just the mark: Power BI's scatterChart needs two CONTINUOUS
    # axes -- one measure on rows AND one on cols, nothing else on either shelf. Checked against
    # every real corpus Circle/Shape-mark worksheet (23 across "WorkbookD" and
    # "WorkbookC"): NONE has this shape. All 7 WorkbookD ones put TWO
    # measures on rows and a DIMENSION on cols (Tableau's "circle view"/dot-plot idiom, comparing
    # two measures across a category -- not an X/Y scatter at all), and all 16 WorkbookC ones have NO
    # measure on either shelf (a decorative delta/heatmap glyph technique). The old unconditional
    # rule mapped every one of these to scatterChart anyway, which -- with no real X role ever
    # populated (see the `scatterChart` WELLS entry's own comment) -- Power BI Desktop refused to
    # render at all: "Can't display this visual... Remove Values to display x- and y-axis
    # pairs." A guess that fails to open is worse than an honest "no rule fired": unguarded, this
    # falls through to the tableEx default below, exactly like any other shape this deterministic
    # tier has no confident mapping for.
    (lambda m, s: m == "Circle" and s["rows_measure"] and s["cols_measure"] and s["n_dims"] == 0,
     "scatterChart", "mark class Circle with one continuous measure on each shelf -- a true X/Y "
     "scatter"),
    (lambda m, s: m == "Shape" and s["rows_measure"] and s["cols_measure"] and s["n_dims"] == 0,
     "scatterChart", "mark class Shape with one continuous measure on each shelf -- a true X/Y "
     "scatter"),
    # mark='Shape' with measures on Color/Shape channel only (nothing on Rows or Cols) --
    # Tableau's up/down-triangle KPI trend-indicator convention: an icon whose shape and colour
    # are conditional on the measure's sign (positive = green up-triangle, negative = red
    # down-triangle).  No standard Power BI visual reproduces the conditional triangle-icon
    # styling; emitting a plain `card` bound to the same measure shows the number itself (e.g.
    # "+5.2%") rather than dropping the visual entirely.  Confirmed on 8 WorkbookA worksheets:
    # "Sheet 8 - Vol YoY Marks", "Sheet 8 - Txn YoY Marks", "Sheet 8 - Marks decline vol", "Sheet 8 - Marks decline txn
    # ", "Sheet 9 - Marks Return YoY growth Vol", "Sheet 9 - Marks Return Vol", "Sheet 9 - Marks Return Txn
    # ", "Sheet 9 - Marks Return Txn YoY".  Icon/triangle conditional styling is a
    # deliberately deferred cosmetic gap -- see SKILL.md bug table entry "Shape-mark
    # color/shape-only KPI: card, icon styling deferred".
    # Placed AFTER the scatterChart rule: if BOTH rows_measure AND cols_measure are True, the
    # scatterChart rule fires first.  This card rule catches everything else (0 dims, >=1 measure
    # from the color/shape channel fallback _shelf_shape now counts).
    (lambda m, s: m == "Shape" and s["n_dims"] == 0 and s["n_measures"] >= 1,
     "card",
     "mark class Shape, measures only (Color/Shape channel), no dimension -- Tableau KPI "
     "trend-indicator icon; emitting as card (icon/triangle conditional styling deferred, "
     "see SKILL.md)"),
    # No dimension at all means there is nothing for a treemap to partition into rectangles --
    # checked BEFORE the colour rule below, since a 0-dim Square can still carry a colour
    # encoding and treemap would be the wrong choice for it. Same shape as the Text -> card rule
    # two lines down; found by auditing all 28 real corpus 'Square' instances that fell to
    # tableEx undecided (12 of the 28 are exactly this shape).
    (lambda m, s: m == "Square" and s["n_dims"] == 0 and s["n_measures"] >= 1,
     "card", "mark class Square with measures and no dimension"),
    # A Square mark with BOTH a colour encoding AND a size encoding is the true treemap idiom:
    # size fills Power BI's required Values well (area), colour fills the Group well (category).
    # Checked BEFORE the no-size rule below so the size-present case is never silently downgraded.
    (lambda m, s: m == "Square" and "color" in s["channels"] and "size" in s["channels"],
     "treemap", "mark class Square with colour AND size encoding -- true treemap"),
    # A Square mark with colour but NO size is Tableau's colour-matrix / delta-heatmap idiom
    # (mark-sizing-setting='marks-scaling-off'): each cell is uniformly sized and only the colour
    # encodes the category value.  Power BI's treemap REQUIRES a size measure in its Values well;
    # without one the visual renders completely blank.  The safe, non-blank fallback is a plain
    # table (tableEx) whose rows/columns carry the dimensions -- cell background conditional-
    # formatting by the colour field is a cosmetic gap deferred to manual post-processing (see
    # SKILL.md bug table: "Square-colour-no-size: treemap→tableEx, CF styling deferred").
    (lambda m, s: m == "Square" and "color" in s["channels"] and "size" not in s["channels"],
     "tableEx", "mark class Square with colour but no size -- colour-matrix heatmap, "
     "treemap would be blank without a size measure; emitting tableEx instead"),
    (lambda m, s: m == "Text" and s["n_dims"] == 0 and s["n_measures"] >= 1,
     "card", "Text marks with measures and no dimension is a Tableau BAN"),
    (lambda m, s: m == "Text", "tableEx", "mark class Text with dimensions is a text table"),
    # GanttBar is not always a literal Gantt/timeline chart. Tableau has no native waterfall
    # mark, so the well-known workaround is to fake one with GanttBar: a discrete "step" category
    # on one shelf, a running/aggregated value on the other, and a `size` encoding controlling
    # each bar's length -- the bar is drawn floating, not from a real start date. Checked against
    # the ONLY two real GanttBar worksheets in the whole 8-workbook fleet ("Waterfall - Group",
    # "Waterfall - Alt"): both have this exact shape, and their own field names are literally
    # "Waterfall Values" / "Waterfall Step" / "Waterfall Color" / "Waterfall Label/Size" -- not
    # named coincidentally, this is unambiguously the waterfall technique. Mapping it to Power
    # BI's `ganttChart` (which expects a Task/StartDate/Duration timeline) would have been a
    # confidently-labelled WRONG chart, not just an empty one. `size` presence is the check
    # because it is the one structural signal available (no date-typed field distinguishes this
    # from a literal Gantt either way in this data) -- a literal date-driven Gantt would not need
    # to control bar length via an explicit size encoding.
    (lambda m, s: m == "GanttBar" and "size" in s["channels"],
     "waterfallChart", "mark class GanttBar with a size encoding is Tableau's floating-bar "
     "technique for a chart type it has no native mark for -- a waterfall"),
    # No real corpus workbook has a GanttBar mark WITHOUT a size encoding, so this is the
    # textbook literal-timeline case by elimination, not by direct evidence -- kept as the
    # fallback rather than declined, but its field wells are deliberately NOT wired (see WELLS
    # below): Task/StartDate/Duration would be guessed role names with zero real data to check
    # against, unlike waterfallChart's Category/Y which reuses the same well-worn roles already
    # trusted for every other chart type here.
    (lambda m, s: m == "GanttBar", "ganttChart", "mark class GanttBar"),
    (lambda m, s: m in ("Map", "Filled Map"), "filledMap", "mark class Map"),
    # A DISCRETE field on the Color shelf of a Bar mark stacks, by Tableau's own documented
    # default for that mark -- so a bar/column chart with no series split at all would be the
    # WRONG chart: the segments Tableau draws are missing entirely. Measured across the corpus:
    # 462 colour encodings sit on worksheets whose visual type was already decided, i.e.
    # information the file states explicitly that the report layer was discarding. Checked
    # BEFORE the plain Bar rules, which stay as the correct answer for a bar with no colour
    # dimension (or with a continuous colour gradient, which does not stack -- see
    # `_shelf_shape`'s own note on the dim/measure split).
    #
    # `barChart`/`columnChart` ARE the stacked variants in Power BI's own visualType naming --
    # there is no separate "stackedBarChart"/"stackedColumnChart" registered visual at all;
    # `clusteredBarChart`/`clusteredColumnChart` is the side-by-side one that needs the
    # qualifier. Emitting the never-registered "stacked*Chart" name is exactly what a real
    # Desktop file rejects with "Can't display this visual... CustomVisualNotFound" -- it reads
    # as a request for a THIRD-PARTY custom visual by that name, not a color-split ask, because
    # Desktop has no built-in visual registered under that string. Confirmed by opening a real
    # rebuilt project (WorkbookI) in Desktop: every color-split bar/
    # column chart failed to render with that exact error. The field-well SHAPE (Category/Y,
    # plus a Series/Tooltips color well below) needs no change -- stacking is the visual type's
    # own default behaviour, not a different well.
    (lambda m, s: m == "Bar" and s["color_dims"] >= 1 and s["cols_measure"]
     and not s["rows_measure"], "barChart",
     "mark class Bar, measure on columns, with a dimension on Colour -- Tableau stacks a "
     "discrete colour field on a bar mark by default"),
    (lambda m, s: m == "Bar" and s["color_dims"] >= 1, "columnChart",
     "mark class Bar, measure on rows, with a dimension on Colour -- Tableau stacks a discrete "
     "colour field on a bar mark by default"),
    (lambda m, s: m == "Bar" and s["cols_measure"] and not s["rows_measure"],
     "barChart", "mark class Bar with the measure on columns is a horizontal bar"),
    (lambda m, s: m == "Bar", "columnChart", "mark class Bar with the measure on rows"),
    # Checked before the generic 'Automatic' rules below: a dual-axis combo's OWN worksheet-level
    # mark is always 'Automatic' (see `_dual_axis_combo`'s own docstring for why), so without this
    # rule it would fall through every mark-specific rule above and land on the generic
    # 2-measure/1-dimension 'Automatic' shape, which has no rule at all -- exactly the review item
    # this closes (WorkbookB Dashboard's "Sheet 1 - Trend").
    (lambda m, s: s["dual_axis_combo"] is not None and s["n_measures"] == 2 and s["n_dims"] == 1,
     "lineClusteredColumnComboChart", "two panes on the same shelf with Bar + Line marks -- "
     "Tableau's dual-axis combo idiom (right-click 'Dual Axis' on a second Rows/Cols measure)"),
    # Automatic: 0 positional dimensions but >= 1 dimension ONLY on the Color shelf, with
    # exactly 1 measure. In Tableau the Color shelf doubles as a category/grouping axis for the
    # Automatic mark: dropping a DISCRETE (non-aggregated) field on Color produces a bar-per-
    # value breakdown, identical to putting that dimension on Rows/Cols. Without this pair of
    # rules, a worksheet that puts its only grouping dimension on Color (and its measure on
    # Rows/Cols) lands on the generic "measures only, no dimension -> card" rule below and loses
    # the entire by-category breakdown -- emitting a grand-total-only card where a stacked
    # bar/column chart should be. Confirmed on 4 real WorkbookA worksheets ("Sheet 6 - Segmentation(A)",
    # "Sheet 6 - Segmentation(B)", "Sheet 7 - Item ID A", "Sheet 7 - Item ID B") -- each appeared on
    # the Detail Page page as a card with extreme bar-chart aspect-ratio dimensions.
    # Checked BEFORE the plain "measures only" card rule so those worksheets don't fall through
    # to it. A worksheet with no dimension on Color AND no positional dimension still reaches the
    # card rule unchanged -- the `color_dims >= 1` guard is explicit.
    (lambda m, s: m == "Automatic" and s["n_measures"] == 1 and s["n_dims"] == 0
     and s["color_dims"] >= 1 and s["rows_measure"],
     "columnChart",
     "Automatic marks, one measure on rows, dimension only on Color -- Color is the sole "
     "grouping axis, treated as a column chart with the Color field as category"),
    (lambda m, s: m == "Automatic" and s["n_measures"] == 1 and s["n_dims"] == 0
     and s["color_dims"] >= 1 and s["cols_measure"],
     "barChart",
     "Automatic marks, one measure on columns, dimension only on Color -- Color is the sole "
     "grouping axis, treated as a bar chart with the Color field as category"),
    # Automatic: the mark carries no information, so only the shelf shape can decide, and only
    # where the shape is unambiguous.
    (lambda m, s: m == "Automatic" and s["n_measures"] >= 1 and s["n_dims"] == 0,
     "card", "Automatic marks, measures only, no dimension"),
    (lambda m, s: m == "Automatic" and s["n_measures"] == 1 and s["n_dims"] == 1
     and s["cols_measure"], "barChart",
     "Automatic marks, one dimension on rows and one measure on columns"),
    (lambda m, s: m == "Automatic" and s["n_measures"] == 1 and s["n_dims"] == 1
     and s["rows_measure"], "columnChart",
     "Automatic marks, one dimension on columns and one measure on rows"),
    # Rows AND Columns both empty, with dimensions present. This is the same conclusion the
    # `mark == "Text"` rule above already reaches, arrived at from the shelf shape instead of the
    # mark label: with nothing on either positional shelf there is no axis for any mark to plot
    # against, so Tableau's Automatic mark can only fall back to the text display an explicit
    # Text mark would give. The 0-dimension variant of this shape is already decided as a `card`
    # by the "measures only, no dimension" rule above -- this is deliberately only the
    # has-dimensions half, which was the LAST shape still falling through to the undecided
    # fallback for this reason.
    #
    # Emitted as `tableEx` either way, so this changes no visual's TYPE. What it changes is the
    # honesty of the report: 77 real corpus worksheets (12 of them in WorkbookB Dashboard) were
    # being counted as "no rule fired -- needs a human to look at it" when the shape does in fact
    # determine the answer, which buried the genuinely-undecided ones in false positives.
    # Promoted here from generate-copilot-fix-prompts/scripts/suggest_visual_candidates.py, which
    # had been reporting exactly this as a suggestion for a human to apply by hand.
    # DELIBERATELY excludes the (exactly 1 dimension, 0 measures) shape, which stays undecided:
    # that is the header/label tile idiom, and a one-column `tableEx` of repeated values is very
    # possibly NOT what its author wanted (a textbox often is). The pre-existing self-test for
    # that case guards it explicitly -- "the fallback must not manufacture false confidence" --
    # and it is right, so this rule claims only the shapes that are genuinely unambiguous: two or
    # more dimensions (a real crosstab), or dimensions alongside at least one measure.
    # ...and it is a CARD family, not a table. A Tableau text tile draws its value(s) as a
    # caption with no header; `tableEx` draws a COLUMN HEADER (the field's name) above a small
    # value, so the field name visually dominates the tile and the value looks like an
    # afterthought. Seen in real Desktop output for this exact shape: a row of tiles reading
    # "cl.Chosen Metric - Count - Label" / "pl.Date Type Selected" in large text -- which reads
    # as "the data is missing" even though the value ("Lookup") was rendering correctly directly
    # beneath it. `card` shows one value big; `multiRowCard` shows several values each with a
    # small caption, which is much closer to Tableau's label block than a header-and-rows table.
    # A SINGLE Text-shelf dimension with no measure stays UNDECIDED, deliberately. It could be a
    # one-value caption (a card) or a list of many values (a table shows them all; a card would
    # show one and silently lose the rest), and the shelf shape cannot tell those apart. The
    # pre-existing self-test guards exactly this -- "the fallback must not manufacture false
    # confidence" -- and it is right. The multi-field rule below is a different case: several
    # DISTINCT fields side by side is a label block, where a table would emit one header per
    # one-value column, which is unambiguously wrong.
    # Mark-AGNOSTIC on purpose, not just for 'Automatic': with Rows AND Columns both empty there
    # is no axis for ANY mark to plot against, so whatever the mark label says, Tableau's render
    # can only be the same text/label display an explicit Text mark would give -- the exact
    # reasoning `mark == "Text"`'s own rules above are built on, just reached from the shelf shape
    # instead of the mark name. Confirmed this is a real, not hypothetical, gap: this repo's own
    # `suggest_visual_candidates.py` (built to triage exactly the visuals THIS rule set left
    # undecided) already computes this identical shape check mark-agnostically and reports 68 of
    # 122 real corpus `visual_undecided` items (56%) match it -- every one of those was a mark
    # OTHER than 'Automatic' that fell through every rule above to the tableEx/undecided default,
    # since this rule was the only one written for this shape and it was gated on 'Automatic'
    # alone. This is the LAST rule in VISUAL_RULES, so generalizing it can only recover currently
    # UNDECIDED cases -- it can never preempt an earlier (more specific-mark) rule's decision, and
    # there is no later rule for it to shadow.
    # Automatic marks (or any mark), exactly 1 dimension, 0 measures, where that
    # dimension's own Tableau formula is a bare string literal constant -- e.g.
    # [Color_high] = "Highlights Page", [Dor] = "Delta".  These are Tableau's
    # "nav button" worksheet idiom: a coloured zone whose only displayed content is the
    # page/tab name baked directly into a calculated field's formula as a constant string.
    # Real-corpus examples confirmed in WorkbookA: "0.Color_light", "0.Color_dark",
    # "0.Acquisition_white", "0.Op_white", "0.Reco_white", "Dashboard_footer" and others.
    #
    # Why tableEx is wrong: a tableEx with a single column renders as an empty-looking table
    # with a column header equal to the field name -- the exact opposite of the author's
    # intent, which is just to show the label text.
    # Why textbox is right: ZONE_VISUAL["text"] already uses "textbox" for Tableau's own
    # static text/caption zones; the same type is appropriate here.  The caller (`_visual`)
    # populates the textbox's `objects.general.paragraphs` content from the literal value
    # so the label actually renders in Desktop rather than appearing as a blank white box.
    #
    # NARROW: fires ONLY when `dim0_label` is not None (i.e., `calc_lookup` was supplied AND
    # the formula is provably a bare string literal).  A worksheet with a real single-column
    # lookup table (genuine 1-dim / 0-measure with a field-reference formula) has no
    # `dim0_label` and continues to fall through to the undecided tableEx default unchanged.
    # Mark-AGNOSTIC: a string-literal dimension is a string-literal dimension regardless of
    # the mark label; the rule is therefore not gated on `m == "Automatic"`, just as the
    # multiRowCard rule below is not gated on it either.
    (lambda m, s: s["n_dims"] == 1 and s["n_measures"] == 0
     and s.get("dim0_label") is not None,
     "textbox",
     "1 dimension whose entire Tableau formula is a bare string literal constant -- "
     "a nav-button tab-label, not a real data column; rendered as a textbox"),
    (lambda m, s: s["n_rows"] == 0 and s["n_cols"] == 0
     and s["n_dims"] >= 1 and (s["n_measures"] >= 1 or s["n_dims"] >= 2), "multiRowCard",
     "Rows and Columns both empty -- no axis exists for any mark to plot against, so the Text "
     "shelf is the whole view: a Tableau label block, whose several values map to a multi-row "
     "card rather than a table with column headers"),
]


def choose_visual(ws, calc_lookup=None):
    """(visualType, why, decided). `decided=False` means NO rule fired -- a review item.

    `calc_lookup` is an optional {caption: formula} dict used by the string-literal rule
    to detect nav-button worksheets.  Callers that do not have IR access (e.g. the
    self-test's stand-alone fixtures) may omit it; the string-literal rule is then simply
    unreachable, which is the correct behaviour for a rule that requires formula access.
    """
    mark, shape = ws["mark"], _shelf_shape(ws, calc_lookup=calc_lookup)
    for pred, vt, why in VISUAL_RULES:
        if pred(mark, shape):
            return vt, why, True
    return "tableEx", (f"no rule fired for mark {mark!r} with "
                       f"{shape['n_dims']} dimension(s) and {shape['n_measures']} measure(s)"), False


# A Tableau quick TABLE CALCULATION pill's own outer shelf code -- Percent of Total, %
# Difference/Difference, Running Total (Cumulative), a moving-Window calc, and shelf-level Rank --
# never a real Tableau aggregation (never in tmdl.py's `AGG_DAX`). See `_resolve_field`'s own
# comment for why this must decline rather than bind the field it now (post the extract.py FIELDREF
# fix) correctly resolves.
_QUICK_CALC_SHELF_CODES = {"pcto", "pcdf", "diff", "cum", "win", "rank"}


def _strip_wrapping_parens(caption):
    """Strip one outer parenthesis pair from a Tableau-auto-named caption like
    "([Diff Metric A])", and any resulting outer brackets that are Tableau's own
    field-reference syntax "[Field]" -> "Field".

    Returns the original string unchanged when:
    - it does not start with "(" and end with ")" (e.g. "Build Up By (copy)" is safe), OR
    - stripping the parens yields an inner string that contains embedded brackets (i.e. the
      inner part is an expression like "[A] + [B]", not a simple field reference).

    Only the ENTIRE-caption paren-wrap pattern is stripped, never a mid-caption pair.
    """
    if not (caption.startswith("(") and caption.endswith(")")):
        return caption
    inner = caption[1:-1]
    # If the result is a bracket-quoted field reference "[Name]" with no embedded brackets,
    # strip those brackets too -- Tableau's own formula syntax writes field references as
    # "[FieldName]", so "([Diff Metric A])" becomes "[Diff Metric A]" then "Diff Metric A".
    if (inner.startswith("[") and inner.endswith("]")
            and "[" not in inner[1:-1] and "]" not in inner[1:-1]):
        inner = inner[1:-1]
    return inner


# visualType -> the field-well role names its queryState uses. Only visual types whose roles are
# unambiguous get wells; the rest emit a container with no query, which renders as an empty visual
# a user can bind by hand -- honest, and it still opens.
WELLS = {
    "barChart":    {"dim": "Category", "measure": "Y"},
    "columnChart": {"dim": "Category", "measure": "Y"},
    "lineChart":   {"dim": "Category", "measure": "Y"},
    "areaChart":   {"dim": "Category", "measure": "Y"},
    "pieChart":    {"dim": "Category", "measure": "Y"},
    # "measure": "Y" here is the role a lone/uncapped measure falls back to (used by the
    # Text-shelf fallback below, which never sees a scatter chart's second axis). The PRIMARY
    # Rows/Cols loop overrides this per-channel for scatterChart -- see SCATTER_AXIS_BY_CHANNEL
    # -- because a scatter plot needs TWO independent measure roles (X, Y), not one "Y" holding
    # both. Real-world confirmation this was wrong: every scatterChart this tool ever built (7 in
    # "WorkbookD", 16 in "WorkbookC") bound BOTH its measures into Y
    # and left X empty, and Power BI Desktop refused to render every one of them ("Can't display
    # this visual... Remove Values to display x- and y-axis pairs").
    "scatterChart": {"dim": "Category", "measure": "Y"},
    "treemap":     {"dim": "Group", "measure": "Values"},
    "card":        {"dim": None, "measure": "Values"},
    # A multi-row card puts every field in one `Values` well and captions each -- the whole reason
    # it is the right target for a Tableau label BLOCK (several values, no headers). Uncapped on
    # purpose: holding all of them is the point, unlike `card`.
    "multiRowCard": {"dim": "Values", "measure": "Values"},
    "tableEx":     {"dim": "Values", "measure": "Values"},
    # Category/Y, same as bar/line/area/etc. -- not Gantt-specific role names, so this carries
    # the SAME (already-accepted) confidence level as every other entry here, unlike a bespoke
    # Task/StartDate/Duration set for `ganttChart` would. See the waterfallChart rule above.
    "waterfallChart": {"dim": "Category", "measure": "Y"},
    # "measure": "Y" is the dim-role fallback only (role_kind == "dim" reads WELLS[...]["dim"]
    # regardless of visual type) -- the measure side is never read from here for this visual
    # type; `_query_state`'s own scatterChart-style special case routes each of the two measures
    # to "Y" (the Bar pane) or "Y2" (the Line pane) via `_combo_role_for`, matched by field
    # identity against `_dual_axis_combo`'s pane mapping, not by this table.
    "lineClusteredColumnComboChart": {"dim": "Category", "measure": "Y"},
}

# Visual types whose declared role has a hard field-count cap in real Desktop -- mirrors
# tools/pbip_rules/checks.py's own `_ROLE_LIMITS`, the independent lint gate that caught the
# gap this closes: `_query_state`'s primary Rows/Cols path had no cap at all, so a Tableau
# worksheet with two dimensions on Rows/Cols (ordinary, valid Tableau -- a real corpus pie
# chart, "Region Rpt Status", carries `Region Report ID` AND `Rpt Status` on its Rows
# shelf) filled a pieChart's `Category` well with 2 projections. Schema-valid PBIR, but
# `tools/pbip_lint.py --schemas` on the rebuilt project failed it: "pieChart role 'Category'
# has 2 fields but the field well accepts 1 -- the editor physically refuses the extra drop".
# Keyed by the WELLS role KIND ("dim"/"measure"), not the literal role name, so it applies
# uniformly regardless of which string a visual type happens to use for that kind of well.
ROLE_CAPS = {
    "card":           {"measure": 1},
    "pieChart":       {"dim": 1, "measure": 1},
    "waterfallChart": {"dim": 1, "measure": 1},
    "treemap":        {"measure": 1},
    # X and Y are each their own bucket (see SCATTER_AXIS_BY_CHANNEL) but share the "measure"
    # role KIND, so this one cap of 1 applies to each independently -- exactly like `pieChart`'s
    # single shared cap already does for its one measure bucket.
    "scatterChart":   {"dim": 1, "measure": 1},
    # Not the measure side -- Y and Y2 are two distinct role NAMES sharing the "measure" role
    # KIND, so this cap of 1 applies to each independently, same as scatterChart's X/Y above.
    # Category itself still needs the cap: `_dual_axis_combo`'s own shape check already requires
    # exactly 1 dimension, but that guards which VISUAL TYPE gets chosen, not how many
    # projections land in the well once chosen.
    "lineClusteredColumnComboChart": {"dim": 1, "measure": 1},
}

# Tableau puts a scatter plot's two axis measures on two DIFFERENT shelves -- one on Columns
# (horizontal, X), one on Rows (vertical, Y) -- the same convention `choose_visual`'s own
# Bar-vs-Column rule already relies on ("measure on columns is a horizontal bar" / "measure on
# rows" for the vertical one). `_query_state`'s primary loop consults this by CHANNEL, in place
# of WELLS["scatterChart"]["measure"], only for scatterChart -- every other visual type keeps
# using the single "Y"/"Values" role from WELLS untouched.
SCATTER_AXIS_BY_CHANNEL = {"cols": "X", "rows": "Y"}

# Non-positional shelves -> the Power BI role that means the same thing. `_query_state` used to
# read ONLY `rows`/`cols` (plus a `text` fallback), so a field Tableau explicitly placed on
# Colour, Size or Tooltip was extracted by extract.py and then silently discarded: 462 colour,
# 337 tooltip and 10 size encodings across the corpus, on 310 worksheets whose visual type was
# ALREADY decided correctly. That is why a converted chart could have the right type and the
# right axis and still not resemble the Tableau original -- the series split was simply absent.
#
# CONFIDENCE, stated plainly: these role NAMES carry the same caveat the whole WELLS table
# already does (see this module's own header -- no visual in docs/examples/ has a `queryState`,
# so nothing here is verified against a Desktop-authored file). Emitted projections are counted
# under their own `extra_role_projections` stat, separate from `projections`, so if Desktop
# rejects the shape the report says exactly how much output is affected -- the same discipline
# the original wells shipped under. Set EXTRA_ROLES = False to turn the whole behaviour off
# without touching anything else.
EXTRA_ROLES = True

EXTRA_WELLS = {
    "barChart":           {"color": "Series", "tooltip": "Tooltips"},
    "columnChart":        {"color": "Series", "tooltip": "Tooltips"},
    "lineChart":          {"color": "Series", "tooltip": "Tooltips"},
    "areaChart":          {"color": "Series", "tooltip": "Tooltips"},
    "scatterChart":       {"color": "Series", "tooltip": "Tooltips", "size": "Size"},
    # Deliberately ABSENT, each for a stated reason rather than an oversight:
    #   pieChart/treemap -- Colour is already the same field as Category/Group on these, so
    #                       binding it twice adds a redundant well, not information.
    #   card/tableEx     -- no legend or size concept; every field is already a Value.
    #   waterfallChart   -- its Category/Y pair is the floating-bar technique (see VISUAL_RULES);
    #                       a second series split on top of that is not a shape this has evidence
    #                       for.
    #   ganttChart       -- its wells are deliberately not wired at all (see WELLS).
}

# By role NAME, not role kind -- these wells are channel-derived, so the existing dim/measure
# keyed ROLE_CAPS cannot express them. A cartesian legend and a size well each hold ONE field in
# real Desktop; Tooltips is a genuine list role and is left uncapped.
EXTRA_ROLE_CAPS = {"Series": 1, "Size": 1}

# Real-world confirmation that the "no visual in docs/examples/ has a queryState" caveat above
# was not just theoretical, and that the failure is NOT scoped to one visual type or one field
# kind. Series/Legend bindings built exactly per EXTRA_WELLS, with the single-field
# EXTRA_ROLE_CAPS cap already applied, were opened in real Power BI Desktop across TWO separate
# dummy-data-populated workbooks and rejected with "There's too many columns in the Legend
# bucket. Please remove some." every single time:
#   - areaChart, Legend = 'Token vs Source', a plain calculated COLUMN
#     ("WorkbookD")
#   - barChart/columnChart, Legend = 'Raw vs Unique Selected', a parameter's SELECTEDVALUE
#     MEASURE, across 8+ visuals at once ("WorkbookI")
# Two different visual types, two different field kinds (column vs. measure), same error both
# times, and zero visuals of any kind with this well populated have opened cleanly yet. The
# parsimonious read of that evidence is that the Series/Legend JSON shape itself -- not the
# field or the chart type -- is what Desktop rejects, so lineChart/scatterChart are not assumed
# safe just because nobody has hit them yet; the SAME caveat in EXTRA_ROLES's own docstring
# covers them, and now has two failures backing it instead of zero. Declining rather than
# guessing at a different shape: fits this skill's own fail-closed rule (see build_pbip.py's
# residue_md docstring) better than shipping a file Desktop already proved it won't open.
# Revisit (narrow this back down) only once a real Desktop-authored queryState with a working
# Series/Legend well exists to diff against -- see this module's header for why nothing here has
# had that yet.
UNVERIFIED_COLOR_REJECTED = {"areaChart", "barChart", "columnChart", "lineChart", "scatterChart"}

# `ROLE_CAPS` is read by BOTH `_query_state` code paths -- the primary Rows/Cols loop AND the
# Text-shelf fallback below it. Originally only the fallback enforced a cap, and only for
# "card" (`_SINGLE_VALUE_VISUALS`, now folded in here) -- discovered incomplete by the SAME real
# corpus bug that motivated `ROLE_CAPS` in the first place: WorkbookF's "Region Rpt Status
# " pie chart has its aggregated measure on Rows, so the PRIMARY loop should have bound
# it -- but that measure calc was independently declined by the formula parser, so `_resolve_field`
# returned `None` for it, `state` came back empty, and control fell through to the FALLBACK
# path, which bound TWO plain dimension columns (`Region Report ID`, `Rpt Status`, both on the
# Text shelf) into `pieChart`'s Category well with no cap at all -- the fallback path had never
# been taught about `ROLE_CAPS`, only about card's old one-off `_SINGLE_VALUE_VISUALS` check.
# Fixed by having the fallback loop consult the SAME `role_caps` dict the primary loop already
# does, rather than adding a second, pieChart-specific special case next to the first one.


_ORG_PREFIX_RE = re.compile(r"^(?:cl|pl)\.")


def _strip_org_prefix(name):
    """Some Tableau authors namespace every calc/parameter under a folder-style prefix baked
    straight into the field's own caption ('cl.' for a calculated field, 'pl.' for a parameter
    label -- confirmed on WorkbookB Dashboard: 236 of 747 fields/parameters use one of these two,
    consistently, with zero real-word false positives -- 'Account Name'/'Account Id' etc. never
    match, since the pattern requires the literal dot right after the 2-letter code). The model
    keeps this real name verbatim everywhere (TMDL, DAX, name_authority.json) -- it IS the
    field's true name, not a guess -- but showing it to a report VIEWER leaks the workbook
    author's own internal organizational scheme, the same class of leak `categoryLabels=False`
    and the paramctrl slicer header fix already exist to stop. Returns None (no override) when
    the prefix is absent, so an unrelated field's `displayName` key is never added at all.
    """
    if _ORG_PREFIX_RE.match(name):
        return _ORG_PREFIX_RE.sub("", name)
    return None


def _projection(entity, prop, is_measure):
    kind = "Measure" if is_measure else "Column"
    out = {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}},
        "queryRef": f"{entity}.{prop}",
        "nativeQueryRef": prop,
    }
    display = _strip_org_prefix(prop)
    if display:
        out["displayName"] = display
    return out


def _expand_measure_names(encodings, pivot):
    """A `:Measure Names`/`:Measure Values` shelf encoding has no column of its own to resolve --
    expand it in place into its already-known pivot member list (each carrying the SAME channel
    the pseudo-field encoding had), so the ordinary per-field resolution loops in `_query_state`
    handle every pivoted measure exactly like any other shelf field, including role caps and
    dedup. `pivot` is extract.py's `measure_names_pivot` -- see that function's docstring for
    where it comes from and why it is never guessed. A worksheet with no pivot is returned
    unchanged.
    """
    if not pivot:
        return encodings
    out = []
    for e in encodings:
        field = e.get("field") or ""
        if ":Measure Names" in field or ":Measure Values" in field:
            out += [{"channel": e["channel"], "field": p["field"], "agg": p["agg"]} for p in pivot]
        else:
            out.append(e)
    return out


def _filter_literal(v, dtype=None):
    """A PBIR literal inside a filter Values list.

    The format's own convention, visible in the example's image sourceFile and report.json:
    a string literal is single-quoted INSIDE the JSON string. Numbers carry a type suffix --
    `L` for integer, `D` for double -- which is why they cannot simply be str()'d.

    `dtype` is the FIELD's actual Power BI datatype (from the Name Authority-adjacent field
    index built in ReportBuilder), not the Python type of `v`. It has to be: every filter value
    reaching this function is a `str` straight out of a Tableau XML attribute (see
    extract.py's `_member_value`), so `isinstance(v, int)` is always False even for a filter on
    an int64 column -- without `dtype`, EVERY numeric/boolean categorical filter silently
    degrades into a quoted string literal. That is schema-valid PBIR but a type mismatch against
    the column's real dataType, and confirmed against real Desktop: "This visual contains one or
    more filters with... type mismatches" -- Desktop refuses to render the visual, not just warn.
    """
    if dtype == "boolean" or isinstance(v, bool):
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "1", "yes", "-1")
        return {"Literal": {"Value": "true" if v else "false"}}
    if dtype == "int64" or (dtype is None and isinstance(v, int)):
        try:
            return {"Literal": {"Value": f"{int(v)}L"}}
        except (TypeError, ValueError):
            pass
    if dtype == "double" or (dtype is None and isinstance(v, float)):
        try:
            return {"Literal": {"Value": f"{float(v)}D"}}
        except (TypeError, ValueError):
            pass
    return {"Literal": {"Value": "'" + str(v).replace("'", "''") + "'"}}


def _query_entities(query_state):
    """Every distinct `Entity` a visual's OWN `query.queryState` projects, recursively.

    Same walk `_visual_filters` below needs to decide reachability with, and the same shape a
    real Desktop `filterConfig`/`query` pair uses: an `Entity` string can be nested arbitrarily
    deep under `field`/`Column`/`Measure`/`Expression`/`SourceRef`. Written generically (walk any
    dict/list, collect every `"Entity"` string value) rather than hard-coding the wells' exact
    nesting, so a future well shape is covered for free.
    """
    out = set()

    def _walk(obj):
        if isinstance(obj, dict):
            e = obj.get("Entity")
            if isinstance(e, str):
                out.add(e)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(query_state)
    return out


def _relationship_reachable(entity, targets, adj):
    """True if `entity` IS one of `targets`, or reachable from one via `adj` -- an UNDIRECTED
    table-relationship graph (entity -> set of directly-related entities).

    Power BI cross-filtering direction is a real, separate concept, but for "can Desktop resolve
    how this filter's table relates to the visual's own queried table(s) at all" -- the question
    an `InvalidUnconstrainedJoin` failure is asking -- only the UNDIRECTED path matters: Desktop
    needs SOME relationship chain to walk, in either direction, not a specific one.
    """
    if entity in targets:
        return True
    seen = {entity}
    frontier = [entity]
    while frontier:
        nxt = []
        for e in frontier:
            for n in adj.get(e, ()):
                if n in targets:
                    return True
                if n not in seen:
                    seen.add(n)
                    nxt.append(n)
        frontier = nxt
    return False


def _visual_filters(builder, page_key, vname, ws, query_entities=frozenset()):
    """Tableau worksheet filters -> a PBIR visual-level filterConfig.

    ONLY the categorical value lists are emitted. A quantitative range needs a `Comparison`
    condition, whose `ComparisonKind` numbering appears nowhere in docs/examples/ or the schema
    reference -- and a wrong condition shape makes Desktop refuse the ENTIRE project, not just that
    visual. 24 range filters are not worth that risk, so they are declined with the reason.

    A filter whose field did not make it into the model is also declined: PBIR binds by name, so a
    filter on a missing column is a dangling reference.

    `query_entities` is the visual's OWN `query.queryState` table set (empty if the visual has no
    query at all, e.g. a pure filter/slicer control -- see the caller). A filter whose resolved
    table is neither one of those, nor a `Param ` table (a slicer's own legitimate direct binding,
    no relationship needed), nor relationship-reachable from one of them via `builder.rel_adj` is
    Tableau data-BLENDING carried straight through with no Power BI relationship underneath it --
    confirmed on a real Desktop-opened build (WorkbookC, "Page L4
    " page): "Can't determine relationships between the fields... Underlying Error:
    InvalidUnconstrainedJoin". Emitting that filter is declined rather than guessed at, the same
    fail-closed rule the `range`/date declines just above already follow -- Tableau's blend
    semantics (aggregate-then-join, often many-to-many on a non-unique attribute, no stored key)
    do not map onto a Power BI relationship in general, and this pipeline's `relationships.tmdl`
    writer (tmdl.py's `_relationships`) never emits one anyway (see its own docstring) -- so
    "decline" is not a fallback for a harder case, it is the only translatable outcome today.
    """
    out = []
    for i, flt in enumerate(ws.get("filters") or []):
        kind = flt.get("kind")
        if kind in ("none",):
            # Two distinct real shapes land here now: `level-members` (every member enumerated,
            # literally nothing to restrict) and a single-member filter whose value is Tableau's
            # own "(All)" show-everything sentinel (extract.py's `classify_filter`) -- different
            # reasons, so report them separately rather than bucketing both under one label that
            # named only the first.
            builder.filter_stats[flt.get("reason") or "no restriction"] += 1
            continue
        if kind == "action":
            builder.filter_stats["dashboard action state -> visualInteractions, not a filter"] += 1
            continue
        if kind == "range":
            builder.filter_stats[
                "quantitative range — needs a PBIR `Comparison` condition, whose ComparisonKind "
                "numbering is in neither docs/examples/ nor the schema reference; a wrong condition "
                "shape makes Desktop refuse the WHOLE project, so this is declined rather than "
                "guessed"] += 1
            continue
        if kind not in ("in", "not_in"):
            builder.filter_stats[flt.get("reason") or f"unsupported filter kind {kind!r}"] += 1
            continue
        hit = builder._resolve_field(flt["field"], None, candidate_tables=query_entities)
        if hit is None:
            builder.filter_stats["field is not in the emitted model"] += 1
            continue
        entity, prop, is_measure = hit
        if is_measure:
            builder.filter_stats["filter on a measure needs a HAVING-style condition"] += 1
            continue
        if (query_entities and entity not in query_entities
                and not entity.startswith("Param ")
                and not _relationship_reachable(entity, query_entities, builder.rel_adj)):
            builder.filter_stats[
                f"filter on {entity!r} has no relationship path to the visual's own queried "
                "table(s) -- Tableau data blend/cross-datasource filter with no stored Power BI "
                "relationship; emitting it produces Desktop's InvalidUnconstrainedJoin "
                "('Can't determine relationships between the fields'), so it is declined"] += 1
            continue
        dtype = builder.field_datatype.get(flt["field"])
        if dtype in ("date", "dateTime"):
            # Same reasoning as the `range` decline above: `_filter_literal` knows the correct
            # literal shape for string/int64/double/boolean (each confirmed against real Desktop
            # output), but a discrete date/dateTime literal's correct PBIR shape is NOT confirmed
            # anywhere in this project -- guessing one risks the exact failure this project has
            # hit twice already this way, a value/type mismatch Desktop rejects outright. No
            # workbook has needed this yet (0 date-typed categorical filters across 8 real
            # workbooks), so declining costs nothing today and avoids shipping an unverified guess.
            builder.filter_stats[
                "date/dateTime categorical filter — correct PBIR literal shape for a discrete "
                "date value is unconfirmed against real Desktop output, declined rather than "
                "guessed"] += 1
            continue
        vals = flt["values"] if kind == "in" else flt["exclude"]
        src = {"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": prop}}
        cond = {"In": {"Expressions": [src],
                       "Values": [[_filter_literal(v, dtype)] for v in vals]}}
        if kind == "not_in":
            cond = {"Not": {"Expression": cond}}
        out.append({
            "name": pbir_name("filter", page_key, vname, flt["field"], i),
            "field": {"Column": {"Expression": {"SourceRef": {"Entity": entity}},
                                 "Property": prop}},
            "type": "Categorical",
            "filter": {"Version": 2,
                       "From": [{"Name": "f", "Entity": entity, "Type": 0}],
                       "Where": [{"Condition": cond}]},
            "howCreated": "User",
        })
        builder.filter_stats["emitted"] += 1
        if flt.get("context"):
            builder.context_filters.append({"page": page_key, "field": flt["field"]})
    return out


def _lit(value):
    """A PBIR literal expression. Strings are single-quoted INSIDE the JSON string value, which is
    the format's own convention and not a JSON quoting artefact -- see the example's image
    sourceFile and the report.json verticalAlignment."""
    return {"expr": {"Literal": {"Value": f"'{value}'"}}}


def _lit_str(value):
    """Same as `_lit`, but for a string that can contain an apostrophe (a button caption typed
    by a Tableau author, not an internal constant like `_lit`'s own callers use) -- escaped the
    same way `_filter_literal` already escapes a quoted filter value ('->'')."""
    return {"expr": {"Literal": {"Value": "'" + str(value).replace("'", "''") + "'"}}}


def _heatmap_gradient_object(entity, raw_column, field_query_ref,
                              min_color="#FFFFFF", max_color="#118DFF"):
    """Confirmed PBIR schema for a tableEx column's background-colour gradient (Format pane's
    own "Background color -> Field value" rule, the Power BI equivalent of Tableau's colour-
    matrix heatmap encoding) -- the SKILL.md gap this closes: "no Desktop-safe PBIR schema for
    conditionalFormatting / background-colour-by-category has been confirmed for tableEx, and
    this project's fail-closed convention requires confirmed schema before emitting, not a
    guess." Confirmed by round-tripping `pbi-cli`'s own `format background-gradient` against a
    real tableEx visual and inspecting the written `visual.json` (`pbi_cli.core.format_backend
    .format_background_gradient`, same shape, independently reproduced here with no dependency
    on that package at runtime). The gradient always keys off a RAW column wrapped in its own
    fresh `Aggregation{Function: 0}` (SUM) -- never the already-built measure object -- which is
    why callers must resolve the colour field's UNAGGREGATED form (same Tableau field, agg=None)
    separately from the aggregated measure projection already bound to the Values well."""
    return {
        "properties": {
            "backColor": {
                "solid": {
                    "color": {
                        "expr": {
                            "FillRule": {
                                "Input": {
                                    "Aggregation": {
                                        "Expression": {
                                            "Column": {
                                                "Expression": {"SourceRef": {"Entity": entity}},
                                                "Property": raw_column,
                                            }
                                        },
                                        "Function": 0,
                                    }
                                },
                                "FillRule": {
                                    "linearGradient2": {
                                        "min": {
                                            "color": {"Literal": {"Value": f"'{min_color}'"}},
                                            "nullColoringStrategy": {
                                                "strategy": {"Literal": {"Value": "'asZero'"}}
                                            },
                                        },
                                        "max": {"color": {"Literal": {"Value": f"'{max_color}'"}}},
                                    }
                                },
                            }
                        }
                    }
                }
            }
        },
        "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}],
            "metadata": field_query_ref,
        },
    }


# A Tableau formula that is EXACTLY a double-quoted string literal with nothing else:
# no field references, no operators, no function calls.  Tableau uses `""` to escape a
# literal double-quote inside the string.  Used by `_shelf_shape` to recognise a
# nav-button worksheet (see the VISUAL_RULES string-literal entry's own comment).
_BARE_STR_LIT_RE = re.compile(r'^\s*"(?:[^"]|"")*"\s*$')


def _extract_string_literal(formula):
    """If `formula` is ONLY a Tableau double-quoted string literal, return its content.

    Returns None for everything else (field references, operators, function calls, empty
    formula).  Used to distinguish a nav-button "Highlights Page" calc from a real
    single-column lookup table whose formula references actual data.

    Tableau uses double-quote delimiters and two consecutive double-quotes to escape an
    embedded double-quote.  Example: a formula stored as the five-char sequence
    DQUOTE Say DQUOTE DQUOTE hello DQUOTE DQUOTE DQUOTE  yields content  Say "hello".
    """
    if not formula:
        return None
    stripped = formula.strip()
    if len(stripped) < 2 or stripped[0] != '"' or stripped[-1] != '"':
        return None
    if not _BARE_STR_LIT_RE.match(stripped):
        return None
    return stripped[1:-1].replace('""', '"')


# ---------------------------------------------------------------- dim-shaped-measure rescue
#
# See `_query_state`'s own comment on the primary Rows/Cols loop for the hazard this rescues from:
# a Tableau calc used as a plain (non-aggregated) Rows/Cols dimension that resolves to a DAX
# measure cannot be bound as a Power BI grouping key. Declining it (as the guard does by default)
# is always SAFE, but for one common, mechanically-recoverable shape it is needlessly lossy: a
# calc whose ENTIRE formula is an IF/ELSEIF/ELSE cascade gated purely on `[Parameter] = "literal"`
# comparisons -- Tableau's own "dynamic date grain"/"selected metric" idiom (real corpus example:
# WorkbookB Dashboard's `Chosen Date - Grain - w Dash`, gated on `Period Sel`/`Grain Sel`
# parameters). Every Tableau workbook SAVES a current value for each parameter
# (`extract_parameters()`'s own `value` field, read straight from the source XML's
# `<column value=...>`, never a guess) -- so which branch is "active" for THIS workbook is a fact
# on disk, not a judgement call. Folding the cascade against those saved values and landing on a
# single bare field reference recovers the REAL grouping column Tableau itself would have shown
# per-row; anything less mechanical (a real per-row field in the condition, an expression instead
# of a bare field at the leaf, an unterminated/malformed cascade) returns None and the caller falls
# back to the existing, always-safe decline -- this never approximates, it only sometimes resolves.
_PARAM_QUALREF_RE = re.compile(r"\[[^\]]*\]\s*\.\s*\[([^\]]*)\]")


def _unquote_tableau_str(raw):
    q = raw[0]
    return raw[1:-1].replace(q + q, q)


def _bool3_or(vals):
    if True in vals:
        return True
    if None in vals:
        return None
    return False


def _bool3_and(vals):
    if False in vals:
        return False
    if None in vals:
        return None
    return True


def _find_matching_end(toks, i, n):
    """toks[i] is IF. The index of the END that closes it, or `n` if unterminated."""
    depth = 1
    j = i + 1
    while j < n:
        if toks[j].kind == "IF":
            depth += 1
        elif toks[j].kind == "END":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


def _split_if_branches(toks, i, end):
    """toks[i] is IF, toks[end] its matching END (from `_find_matching_end`). Returns
    (branches, else_body): `branches` is [(cond_tokens, body_tokens), ...] for the IF and every
    ELSEIF in order; `else_body` is the ELSE body's tokens, or None if there is no ELSE. Only
    keyword tokens at THIS IF's own nesting depth are treated as boundaries -- a nested IF's own
    THEN/ELSEIF/ELSE/END never gets mistaken for one of this IF's branch edges."""
    depth, k = 0, i + 1
    cond_start, body_start = k, None
    cond_toks = None
    branches, else_body, state = [], None, "cond"
    while k < end:
        tk = toks[k]
        if tk.kind == "IF":
            depth += 1
        elif tk.kind == "END":
            depth -= 1
        if depth == 0:
            if state == "cond" and tk.kind == "THEN":
                cond_toks = toks[cond_start:k]
                state, body_start = "body", k + 1
            elif state == "body" and tk.kind == "ELSEIF":
                branches.append((cond_toks, toks[body_start:k]))
                cond_start, state = k + 1, "cond"
            elif state == "body" and tk.kind == "ELSE":
                branches.append((cond_toks, toks[body_start:k]))
                state, body_start = "else", k + 1
        k += 1
    if state == "body":
        branches.append((cond_toks, toks[body_start:end]))
    elif state == "else":
        else_body = toks[body_start:end]
    return branches, else_body


def _split_top_level(toks, kind):
    """Split on top-level tokens of `kind` ("OR"/"AND"), respecting ( ) nesting."""
    parts, depth, start = [], 0, 0
    for idx, tk in enumerate(toks):
        if tk.kind == "op" and tk.val == "(":
            depth += 1
        elif tk.kind == "op" and tk.val == ")":
            depth -= 1
        elif depth == 0 and tk.kind == kind:
            parts.append(toks[start:idx])
            start = idx + 1
    parts.append(toks[start:])
    return parts


def _eval_atom(toks, current_value_fn):
    """A single `<ref> = <literal>` comparison, 3-valued (True/False/None=undecidable). `ref`
    must be a bare field/qualref token -- anything else (a function call, a second operator) is
    not a shape this can evaluate without running the expression, so it declines rather than
    guess."""
    toks = list(toks)
    while (len(toks) >= 2 and toks[0].kind == "op" and toks[0].val == "("
           and toks[-1].kind == "op" and toks[-1].val == ")"):
        toks = toks[1:-1]
    if len(toks) != 3:
        return None
    ref, eq, lit = toks
    if not (eq.kind == "op" and eq.val == "="):
        return None
    if ref.kind not in ("field", "qualref"):
        return None
    current = current_value_fn(ref)
    if current is None:
        return None
    if lit.kind == "str":
        return str(current) == _unquote_tableau_str(lit.val)
    if lit.kind == "word" and lit.val.upper() in ("TRUE", "FALSE"):
        cv = str(current).strip().lower()
        if cv not in ("true", "false", "1", "0"):
            return None
        cur_bool = cv in ("true", "1")
        return cur_bool == (lit.val.upper() == "TRUE")
    return None


def _eval_cond(toks, current_value_fn):
    """OR of ANDs of atoms, 3-valued -- the only shape every real corpus condition of this kind
    uses (a flat `[Param]="A" OR [Param2]=FALSE` chain, no NOT, no cross-field comparisons)."""
    or_vals = []
    for or_part in _split_top_level(toks, "OR"):
        and_vals = [_eval_atom(p, current_value_fn) for p in _split_top_level(or_part, "AND")]
        or_vals.append(_bool3_and(and_vals))
    return _bool3_or(or_vals)


def _fold_if(toks, i, n, current_value_fn, leaf_fn):
    end = _find_matching_end(toks, i, n)
    if end >= n:
        return None
    branches, else_body = _split_if_branches(toks, i, end)
    for cond_toks, body_toks in branches:
        v = _eval_cond(cond_toks, current_value_fn)
        if v is None:
            # Cannot rule out this branch being the active one -- falling through to a later
            # branch here could silently pick the WRONG column. Bail, never guess.
            return None
        if v:
            return _fold_expr(body_toks, current_value_fn, leaf_fn)
    if else_body is not None:
        return _fold_expr(else_body, current_value_fn, leaf_fn)
    return None  # no branch matched, no ELSE -- Tableau's own result here is NULL


def _fold_expr(toks, current_value_fn, leaf_fn):
    toks = list(toks)
    while (len(toks) >= 2 and toks[0].kind == "op" and toks[0].val == "("
           and toks[-1].kind == "op" and toks[-1].val == ")"):
        toks = toks[1:-1]
    if not toks:
        return None
    if toks[0].kind == "IF":
        return _fold_if(toks, 0, len(toks), current_value_fn, leaf_fn)
    if len(toks) == 1 and toks[0].kind == "field":
        return leaf_fn(toks[0])
    # Anything else (a function call, an operator expression, a bare parameter leaf) is not a
    # single recoverable field identity -- decline rather than partially evaluate it.
    return None


def _fold_default_branch(formula_text, current_value_fn, leaf_fn):
    try:
        toks = _dax_lex(formula_text)
    except Exception:
        return None
    return _fold_expr(toks, current_value_fn, leaf_fn)


class ReportBuilder:
    def __init__(self, ir, authority, semantic_model_dir_name, agg_dax=None,
                 relationship_pairs=None):
        self.ir = ir
        self.na = authority
        self.model_dir = semantic_model_dir_name
        self.agg_dax = agg_dax or {}
        self.files = {}
        self.review = []          # views whose visualType nothing decided
        self.gaps = []            # zones with no Power BI construct at all
        self.declined_encodings = []  # Color/Size/Tooltip fields declined as unverified, plus
                                       # dim-shaped Rows/Cols fields that resolved to a measure
        self.stats = collections.Counter()
        self.filter_stats = collections.Counter()
        self.context_filters = []
        self.images = {}          # resource item name -> source path on disk
        self.ws_by_name = {w["name"]: w for w in self.ir["worksheets"]}
        # Keyed by (dashboard, worksheet) -- see extract.py's `extract_url_actions` docstring for
        # exactly which real corpus shapes this does and does not cover (its own dedupe already
        # guarantees at most one entry per key, so a plain dict build is enough here).
        self.url_actions = {(a["dashboard"], a["worksheet"]): a
                            for a in (self.ir.get("url_actions") or [])}
        # (dashboard, worksheet) pairs that are the SOURCE of some on-select Tableau dashboard
        # action -- see extract.py's `extract_action_source_worksheets` docstring for the real
        # WorkbookA case this closes. `_visual` only leaves `drillFilterOtherVisuals` True for a
        # worksheet in this set; every other worksheet gets it set False, matching Tableau's own
        # opt-in cross-filtering default instead of Power BI's opt-out one.
        self._action_source_worksheets = set(
            tuple(p) for p in (self.ir.get("action_source_worksheets") or ()))
        # Every dashboard this project actually builds a page for -- a nav button's resolved
        # window-id target (see extract.py's _nav_button_target) is only wireable if it names
        # ONE of these; a target GUID that resolves to a worksheet-class window, or to nothing
        # at all, has no page in this project to point at.
        self._dashboard_names = {db["name"] for db in self.ir["dashboards"]}
        # Undirected table-relationship graph, from the SemanticModel's own
        # `TmdlModel.relationship_pairs` (real corpus input: always empty today -- see that
        # attribute's own comment). `_visual_filters` walks this to decide whether a filter on a
        # table other than the visual's own queried table(s) is reachable via ANY relationship
        # chain, rather than assuming "different table" always means "unrelated" -- correct now
        # only because there is nothing in the graph yet, but the right shape for when there is.
        self.rel_adj = collections.defaultdict(set)
        for a, b in (relationship_pairs or []):
            self.rel_adj[a].add(b)
            self.rel_adj[b].add(a)
        # Same purpose as `filter_stats`, one level up: a whole VISUAL, not just one of its
        # filters, whose own field wells span two tables with no relationship path is the other
        # half of the real Desktop bug this corpus surfaced (WorkbookC
        # , "Page L2"/"Page L4" pages) -- see `_visual`'s own comment for
        # where this is consulted.
        self.blend_stats = collections.Counter()
        # Datatype per Tableau spelling (caption OR internal name) -- mirrors tmdl.py's own
        # `types` index exactly, needed here for the same reason: a filter value is a plain str
        # regardless of the field's real type, and _filter_literal must know that real type to
        # avoid emitting a type-mismatched literal. See _filter_literal's docstring.
        self.field_datatype = {}
        for t in self.ir["tables"]:
            for c in t["columns"] + t["calcs"]:
                for spelling in (c["name"], fkey(c)):
                    if spelling:
                        self.field_datatype.setdefault(spelling, c.get("datatype"))
        # Parameters have the SAME caption-vs-internal-name split as fields (see tmdl.py's
        # `_resolver` for the fuller explanation): a dashboard's <paramctrl> zone names its
        # parameter by INTERNAL name (`Parameter 1`), while `self.na.params` is keyed by the
        # CAPTION a user sees ("Show as:") -- names.py's `parameter()` is only ever called with
        # `p["name"]` (the caption). A paramctrl zone's field therefore never matched, and the
        # slicer it should bind to the parameter's SELECTEDVALUE measure got wired with NO query
        # at all -- schema-valid, but Desktop renders an empty/broken slicer, confirmed by
        # `pbip_lint.py`'s RPT-ROLE-02 on every real corpus workbook with a parameter control (0
        # of them wired). Same fix tmdl.py's own `_resolver` already applies for DAX formula
        # references to `[Parameters].[X]`: index by BOTH spellings, case-insensitively.
        self.param_index = {}
        for p in self.ir["parameters"]:
            rec = self.na.params.get(p["name"])
            if rec is None:
                continue
            # `extract_parameters()`'s own recovered aliases -- an orphaned internal parameter
            # ID present only in a stale, worksheet-embedded copy of this same parameter (see
            # that function's own comment). tmdl.py's `pindex` already includes these; this
            # index did not, so any reference to a parameter ONLY by such an alias (a real
            # corpus shape -- WorkbookB Dashboard's `Parameter 9 1` for `pl.Grain Sel`) missed
            # here even though the DAX side resolved it correctly.
            spellings = [p["name"], (p.get("tableau_name") or "").strip("[]")]
            spellings += [a.strip("[]") for a in (p.get("aliases") or [])]
            for spelling in spellings:
                if spelling:
                    self.param_index.setdefault(spelling, rec)
                    self.param_index.setdefault(spelling.lower(), rec)
        # This workbook's own SAVED current value for each parameter -- see the dim-shaped-
        # measure rescue's own header comment above for why that value is a fact on disk, not a
        # guess. Keyed by the parameter's resolved DAX measure name (`rec["measure"]`), the same
        # identity `_resolve_field`'s Parameter fallback returns, so a fold can look a comparison
        # up by exactly what it already resolved the reference to.
        self._param_current_value_by_measure = {}
        for p in self.ir["parameters"]:
            rec = self.na.params.get(p["name"])
            if rec is not None:
                self._param_current_value_by_measure[rec["measure"]] = p.get("value")
        # Every calc's OWN raw Tableau formula, keyed by ITS OWN canonical identity (whatever
        # `self.na.by_spelling` resolves ITS name to) -- NOT by the raw spelling other formulas
        # reference it by (that is tmdl.py's `resolve_formula`'s job, for a different caller).
        # Needed because a dim-shaped measure being rescued may itself branch into ANOTHER
        # measure (real corpus shape: `Chosen Date - Grain - w Dash` branches into
        # `Month w Dash - Selected Date Type`, a second parameter-gated cascade of its own) --
        # the rescue must be able to fold that SECOND calc's formula too, by canonical name.
        self._formulas_by_canonical = {}
        for t in self.ir["tables"]:
            for c in t["calcs"]:
                for spelling in (c["name"], fkey(c)):
                    if not spelling:
                        continue
                    _tbl, nm, _kind = self.na.by_spelling(spelling, emitted_only=True)
                    if nm is None:
                        continue
                    if nm not in self._formulas_by_canonical:
                        self._formulas_by_canonical[nm] = c["formula"]
                    break
        # Formula index by Tableau CAPTION -- used by choose_visual to detect nav-button
        # worksheets (see VISUAL_RULES's string-literal rule and _extract_string_literal).
        # Unlike _formulas_by_canonical (keyed by the emitted PBI name, requires a Name
        # Authority round-trip), this is a direct caption->formula lookup: a shelf encoding's
        # `field` key IS the caption, so no resolution step is needed here.
        # NOT limited to emitted_only calcs: the formula check happens before deciding whether
        # the field is in the emitted model, and a nav-button calc whose formula is a bare
        # string literal would have been DECLINED by dax.py's translator anyway (no DAX
        # for a bare string constant that is not part of a real expression) -- emitted_only
        # would systematically exclude exactly the calcs this check needs to find.
        self._calc_lookup_by_caption = {}
        for t in self.ir["tables"]:
            for c in t["calcs"]:
                if c.get("name") and c.get("formula") is not None:
                    self._calc_lookup_by_caption.setdefault(c["name"], c["formula"])

    def _all_relationship_reachable(self, entities):
        """True if every entity in `entities` lies in ONE connected component of the
        relationship graph -- the general form of `_relationship_reachable` above, needed when a
        visual's OWN query (not just one of its filters) spans more than one table. A `Param `
        table is never passed in here (see `_visual`'s caller) -- its own SELECTEDVALUE binding
        needs no relationship, by the same convention `_visual_filters` already applies to it."""
        ents = list(entities)
        if len(ents) <= 1:
            return True
        seen, frontier = {ents[0]}, [ents[0]]
        while frontier:
            nxt = []
            for e in frontier:
                for n in self.rel_adj.get(e, ()):
                    if n not in seen:
                        seen.add(n)
                        nxt.append(n)
            frontier = nxt
        return all(e in seen for e in ents)

    # ------------------------------------------------------------------ wells
    def _resolve_field(self, field, agg, candidate_tables=None):
        """(entity, property, is_measure) for a shelf reference, via the Name Authority only.

        `emitted_only=True` throughout: a name may be ALLOCATED and still have no TMDL object
        behind it, because the formula parser declined it after allocation. PBIR binds by name with
        no indirection, so binding to such a name is a dangling reference Desktop reports as a
        broken visual with no clue where it came from. The pre-flight caught exactly this on the
        first real run -- four visuals bound to a declined measure.

        `candidate_tables` (currently only passed by `_visual_filters`, for a FILTER's own field):
        tried, in order, as the PREFERRED table for `by_spelling`'s already-existing local-then-
        global lookup, before falling through to the global-only match below. Real corpus bug
        (WorkbookA): a filter authored against one Tableau datasource's `view_flag`/`Rgn` field (real
        values like "Trend", or "DOM"/"XB") resolved, by caption alone, to a DIFFERENT table's
        same-named column from a DIFFERENT datasource (whose real domain never contains that
        value at all) -- global-first-match is "genuinely ambiguous when several... datasources
        define the same field name", exactly the risk `by_spelling`'s own docstring already
        names, but this call site passed no table hint at all, so it always took the ambiguous
        global path. The visual's OWN query tables (`query_entities`) are the right local hint: a
        filter overwhelmingly belongs to the same table(s) the worksheet's own shelf fields
        already resolved to. Confirmed live against Desktop (`pbi-cli`): the exact failing filter
        (`'Gamma'[View Flag] = "Trend"`) matched zero of 247,425 real rows before this
        fix, blanking every visual that filtered through it -- including, via blend-key
        cross-filtering, visuals on OTHER tables entirely.
        """
        if agg in _QUICK_CALC_SHELF_CODES:
            # These 6 codes name a Tableau quick TABLE CALCULATION pill (Percent of Total,
            # % Difference, Difference, Running Total, moving-Window, Rank), never a real Tableau
            # aggregation -- they are not in tmdl.py's own `AGG_DAX` map and never will be, because
            # this pipeline does not compute the calc itself. Before this session's own FIELDREF
            # fix in extract.py, `field` here was always a garbage underlying-agg placeholder
            # ("sum"/"usr"/"ctd"/"cnt") that no real column was ever named, so this fell through to
            # the plain-column branch below, failed to resolve, and was safely dropped (counted in
            # `unresolved_field_refs`). That fix now makes `field` the REAL column name -- which
            # WOULD resolve there, silently binding the raw, unaggregated column as if it were the
            # calc's own value (e.g. every row of "Selector" instead of a % of total). Decline here
            # explicitly instead, so the fix to extract.py changes nothing about what THIS pipeline
            # ships; only `extract-tablecalc-quickcalcs`-style extensions, reading `_work/ir.json`
            # directly, see the now-correct field name.
            return None
        if agg:
            # tmdl.py's `_plan_shelf_measures` allocates a shelf measure keyed by
            # `__shelf__<agg>__<field>` (its own synthetic Name Authority key, NOT its display
            # name "<AGG> of <column>") -- looking it up by the display name instead, as this
            # line used to, is a different string and never matches, so this ALWAYS fell through
            # to the bare column below. Never caught before because no prior corpus worksheet's
            # rows/cols/tooltip usage combined an aggregation with a plain calculated COLUMN
            # field through this exact path; the Measure Names/Values pivot ("Sheet 1 - Table") is the
            # first thing that ever exercised it. Prefer the real measure: an implicit
            # aggregation cannot be referenced by name from PBIR.
            _lookup_field = field
            # `candidate_tables`-scoped lookup FIRST, same reasoning as the non-agg branch
            # below: this `if agg:` path is reached by ANY truthy aggregation code, not just
            # SUM/AVG shelf-measure lookups -- Tableau's own ATTR() wrapper ("attr") is the
            # common case for a plain dimension dropped on Tooltip/Color/Text, and that was
            # falling straight to the GLOBAL `by_spelling` below with no table hint at all,
            # bypassing the exact protection `candidate_tables` exists to provide. Real corpus
            # bug (WorkbookA): "Sheet 5 - Growth Rate A"'s own Tooltip field (`Grp`, agg="attr")
            # resolved to `Gamma.Grp` instead of `Alpha.Grp` even though
            # `anchor_tables` correctly held `("Alpha",)` -- this branch simply never
            # consulted it. Combined with a same-session Alpha<->Beta relationship
            # that chains through a zero-overlap Beta<->Gamma relationship, the
            # mis-bound Tooltip field forced the WHOLE visual to zero rows under any Alpha
            # filter -- confirmed live via Performance Analyzer (`RowCount: 0`).
            tbl = col = kind = None
            for cand in (candidate_tables or ()):
                tbl, col, kind = self.na.by_spelling(field, table=cand, emitted_only=True)
                if tbl is not None:
                    break
            if tbl is None:
                tbl, col, kind = self.na.by_spelling(field, emitted_only=True)
            if tbl is None:
                # Paren-stripping fallback: Tableau auto-names wrapper calcs as "([Field])"
                # when the formula body is just a parenthesised field reference, e.g.
                # "([Diff Metric A])".  The wrapper calc may have been DECLINED by the
                # formula parser (its alias exists but has no emitted PBI object), so the
                # direct lookup above returned None.  Strip one outer paren pair and any
                # resulting outer brackets to find the underlying emitted field instead.
                # Applied ONLY on failure so a genuine "(X)" caption that IS emitted wins.
                _norm = _strip_wrapping_parens(field)
                if _norm != field:
                    for cand in (candidate_tables or ()):
                        tbl, col, kind = self.na.by_spelling(_norm, table=cand, emitted_only=True)
                        if tbl is not None:
                            _lookup_field = _norm
                            break
                    if tbl is None:
                        tbl, col, kind = self.na.by_spelling(_norm, emitted_only=True)
                        if tbl is not None:
                            _lookup_field = _norm
            if tbl is None:
                return None
            nm, _k = self.na.lookup(tbl, f"__shelf__{agg}__{_lookup_field}", emitted_only=True)
            if nm:
                return tbl, nm, True
            return tbl, col, kind == "measure"
        for cand in (candidate_tables or ()):
            tbl, nm, kind = self.na.by_spelling(field, table=cand, emitted_only=True)
            if tbl is not None:
                return tbl, nm, kind == "measure"
        tbl, nm, kind = self.na.by_spelling(field, emitted_only=True)
        if tbl is not None:
            return tbl, nm, kind == "measure"
        # Fallback: not a table column/calc at all -- it may be a PARAMETER placed directly on
        # a shelf (Rows/Cols/Text/Filters), a legitimate and common Tableau technique for a
        # dynamic header label ("Showing data for: <param>") that is NOT a dedicated `paramctrl`
        # zone (that case is already handled separately, a few lines below in `_visual`, via
        # this same `self.param_index`). `self.na.by_spelling` only ever searches TABLE
        # columns/measures, so a bare parameter reference here always missed -- found on a real
        # workbook (WorkbookG): every "*_header_zone"
        # worksheet across all 5 dashboard pages shows the current 'Group Zone' parameter
        # value as a text label this way, and every one of them rendered as a blank rectangle
        # (pbip_lint.py's RPT-ROLE-02) before this. Bind to the parameter's SELECTEDVALUE
        # MEASURE, not its raw Value column -- a text/card display wants the scalar current
        # value, exactly the reasoning `paramctrl`'s own slicer binding already documents for
        # why IT deliberately uses the raw column instead (a slicer needs the domain, not a
        # single value).
        rec = self.param_index.get(field) or self.param_index.get(field.lower())
        if rec:
            return rec["table"], rec["measure"], True
        # Paren-stripping fallback for the non-agg path: same logic as the agg branch above,
        # applied here for dimension/plain-column shelf references wrapped in parens.
        _norm = _strip_wrapping_parens(field)
        if _norm != field:
            for cand in (candidate_tables or ()):
                tbl, nm, kind = self.na.by_spelling(_norm, table=cand, emitted_only=True)
                if tbl is not None:
                    return tbl, nm, kind == "measure"
            tbl, nm, kind = self.na.by_spelling(_norm, emitted_only=True)
            if tbl is not None:
                return tbl, nm, kind == "measure"
        return None

    def _param_current_value(self, ref_tok):
        """This workbook's saved current value for the Parameter a condition's `ref_tok`
        names, or None if `ref_tok` does not name a known parameter (a real per-row field in
        the condition, most commonly -- the fold must decline there, never guess)."""
        if ref_tok.kind == "qualref":
            m = _PARAM_QUALREF_RE.match(ref_tok.val)
            name = m.group(1) if m else None
        else:
            name = ref_tok.val[1:-1]
        if not name:
            return None
        rec = self.param_index.get(name) or self.param_index.get(name.lower())
        if rec is None:
            return None
        return self._param_current_value_by_measure.get(rec["measure"])

    def _fold_leaf(self, ref_tok, visited):
        """A fold that bottomed out at a single bare `[Field]` -- resolve it exactly as any
        other shelf reference (`_resolve_field`). A COLUMN is the answer. A MEASURE means this
        leaf is itself another parameter-gated selector (real corpus shape -- see this module's
        dim-shaped-measure rescue header comment); recurse into ITS OWN formula, guarded against
        a reference cycle by `visited` and against runaway depth by the same set's size."""
        hit = self._resolve_field(ref_tok.val[1:-1], None)
        if hit is None:
            return None
        entity, prop, is_measure = hit
        if not is_measure:
            return entity, prop, False
        if prop in visited or len(visited) > 8:
            return None
        formula = self._formulas_by_canonical.get(prop)
        if formula is None:
            return None
        return self._try_rescue_dim_shaped_measure(formula, visited | {prop})

    def _try_rescue_dim_shaped_measure(self, formula_text, visited):
        return _fold_default_branch(formula_text, self._param_current_value,
                                     lambda tok: self._fold_leaf(tok, visited))

    def _query_state(self, ws, visual_type, page_key=None):
        roles = WELLS.get(visual_type)
        if not roles:
            return None
        state = {}
        role_caps = ROLE_CAPS.get(visual_type, {})
        encodings = _expand_measure_names(ws["encodings"], ws.get("measure_names_pivot"))
        # Same caption-collision hazard `_resolve_field`'s own `candidate_tables` docstring names
        # for a FILTER's field, one level up: EVERY field well in this method (Rows/Cols, Colour/
        # Size/Tooltip, the Text-shelf fallback) resolved with NO table hint at all, right up
        # until this fix -- only `_visual_filters` ever passed one. A worksheet's own MEASURE
        # field is close to unambiguous (a calc caption rarely collides table-to-table the way a
        # plain dimension caption does), so scan every encoding here ONCE, up front, for whichever
        # table(s) a measure resolves to, and prefer those same table(s) for every OTHER field in
        # this same worksheet -- mirroring the filter fix's own "a field overwhelmingly belongs to
        # the same table the worksheet's shelf fields already resolved to" reasoning, just seeded
        # from the measure side instead of from an already-built query state (there isn't one yet
        # at this point in the method -- that's the whole reason this scan runs first).
        # Real corpus (WorkbookA), confirmed live: giving `Alpha`'s own `[item_id]` column its
        # missing caption (see `extract.py`'s "explicit <column> tag with no caption" gotcha)
        # fixed ITS collision but widened "Item Id" to 5 tables workbook-wide at once (Highlights Page
        # , Beta, Alpha, Overview Page, Detail Page all caption a
        # `item_id`-shaped column "Item Id") -- "Sheet 5 - Growth Rate A"'s barChart then resolved
        # its OWN Category field to `Beta.Item Id` while its Y measure stayed on
        # `Alpha.Group - YoY Growth`, two tables now relationship-reachable (so the
        # existing cross-table blend-check at `_visual` didn't fire) but not the SAME table the
        # worksheet's own filters (`Item_id_all_logic`, `rank_per_flg`, both `Alpha`-scoped)
        # were authored against -- every bar rendered with zero height, categories listed with no
        # value, exactly the "all the graphs went blank" regression this fix closes.
        anchor_tables = []
        # The seed scan itself used to call `_resolve_field` with NO `candidate_tables` at all --
        # so a measure caption that collides across tables (e.g. `sheet_l6_m` exists in BOTH
        # `Gamma` and `Delta`, same real column name, different tables) could seed
        # `anchor_tables` with the WRONG table before any scoping existed to catch it, and every
        # OTHER field in the worksheet (including its own filters) then inherited that wrong
        # scope. Confirmed live on WorkbookA's `Sheet_f6_m_` worksheet (Delta's "Migration View"
        # cards): its own declared datasource is `Delta`, but the seed scan's unscoped lookup
        # resolved `sheet_l6_m` to `Gamma` instead, so the `View Flag` filter ended up
        # checking `Gamma[View Flag]` ("migration_view", lowercase) instead of
        # `Delta[View Flag]` ("Migration View", title case) -- a value that is genuinely absent
        # from `Gamma`, making every one of those cards blank. Seed from the worksheet's
        # OWN declared datasource(s) first; `_resolve_field`'s own candidate_tables fallback chain
        # still reaches the prior unscoped behavior for any worksheet where the measure isn't on
        # its own primary datasource (e.g. an intentional cross-table/blend reference).
        _own_candidates = []
        for _ds_key in ws.get("own_datasources") or ():
            _pbi_tbl = self.na.tables.get(_ds_key)
            if _pbi_tbl and _pbi_tbl not in _own_candidates:
                _own_candidates.append(_pbi_tbl)
        _own_candidates = tuple(_own_candidates)
        for e in encodings:
            hit = self._resolve_field(e["field"], e.get("agg"), candidate_tables=_own_candidates)
            if hit and hit[2] and hit[0] not in anchor_tables:
                anchor_tables.append(hit[0])
        if not anchor_tables:
            # A worksheet with NO measure anywhere in its encodings (a plain dimension/label
            # display -- e.g. "show this view's own Ctr as text") gives the measure scan above
            # nothing to seed from at all, so every field in it fell through to the unscoped
            # global `_resolve_field` fallback -- confirmed on WorkbookA's own "Sheet_x" worksheet,
            # whose sole `Ctr` field landed on `Gamma` (first-registered) instead of
            # `Beta`. 98 of 382 real corpus worksheets share this zero-measure shape.
            # Fall back to the worksheet's OWN declared datasource(s) (extract.py's
            # `own_datasources`, the exact same raw key `na.table()` was allocated under --
            # mirrors the dashboard-zone `zone["param"]` fix in `_visual` one level up).
            for _ds_key in ws.get("own_datasources") or ():
                _pbi_tbl = self.na.tables.get(_ds_key)
                if _pbi_tbl and _pbi_tbl not in anchor_tables:
                    anchor_tables.append(_pbi_tbl)
        anchor_tables = tuple(anchor_tables)
        resolved_measure = False
        for e in encodings:
            if e["channel"] not in ("rows", "cols"):
                continue
            hit = self._resolve_field(e["field"], e.get("agg"), candidate_tables=anchor_tables)
            if hit is None:
                self.stats["unresolved_field_refs"] += 1
                continue
            entity, prop, is_measure = hit
            if is_measure and not e.get("agg"):
                # Tableau used this field as a plain (non-aggregated) Rows/Cols dimension --
                # a per-row/per-category grouping key -- but `_resolve_field` says it is a DAX
                # MEASURE. That happens whenever the calc is gated on a Parameter (tmdl.py's
                # `resolve_param` always points at the parameter's own SELECTEDVALUE measure,
                # never a raw column -- see this same file's own comment a few lines up in
                # `_resolve_field`). A measure cannot serve as a Power BI grouping key: Desktop
                # evaluates it once over the visual's whole filter context, so every row/category
                # collapses into one. Confirmed on WorkbookB Dashboard's "Sheet 2 - Summary" page:
                # `cl.Chosen Date - Grain - w Dash` on Rows collapsed a 12-month table
                # into a single "2024-01" row, and `cl.Selector` used the same way on a
                # barChart's Category channel left Category empty with the field stranded in Y.
                # Before declining: if this calc's WHOLE formula is a Parameter-gated
                # IF-cascade, the workbook's own SAVED parameter values may make which branch
                # is active a fact, not a guess -- try to fold it down to the real underlying
                # column (see this module's dim-shaped-measure rescue header comment). Falls
                # through to the decline below untouched whenever the fold can't determine an
                # answer -- this only ever narrows how often the decline fires, never weakens it.
                formula = self._formulas_by_canonical.get(prop)
                rescued = (self._try_rescue_dim_shaped_measure(formula, frozenset({prop}))
                           if formula else None)
                if rescued is not None:
                    entity, prop, is_measure = rescued
                    self.stats["dim_shaped_field_rescued_to_column"] += 1
                else:
                    # Decline rather than ship a table/chart that silently misrenders -- the
                    # same discipline this pipeline already applies to a declined measure or an
                    # unresolved field a few lines above.
                    self.declined_encodings.append({
                        "page": page_key, "visual": ws["name"], "field": e["field"],
                        "reason": "resolves to a DAX measure (parameter-gated calculated "
                                  "field) but was used as a plain Rows/Cols dimension in "
                                  "Tableau -- a measure cannot serve as a Power BI grouping "
                                  "key and would collapse every row/category into one",
                    })
                    self.stats["dim_shaped_field_resolved_to_measure_declined"] += 1
                    continue
            role_kind = "measure" if is_measure else "dim"
            if not is_measure:
                _sf = self._formulas_by_canonical.get(prop)
                if _sf is not None and (
                        _extract_string_literal(_sf) == "" or _sf.strip() == "INT(NULL)"):
                    # Pure layout spacer (empty-string or always-null formula) -- Tableau uses
                    # these to add visual space between row/column groups in a crosstab.
                    # A Power BI table visual has no concept of a spacer column, so projecting
                    # one would just add a null/(Blank) column a viewer was never meant to see.
                    # Real corpus: WorkbookA Delta heatmaps use 'Blank (1)' formula='' and
                    # 'Blank' formula='INT(NULL)' as separators between segment groups.
                    self.stats["spacer_dims_skipped"] += 1
                    continue
            if is_measure and visual_type == "scatterChart":
                # See SCATTER_AXIS_BY_CHANNEL's own comment -- a scatter plot's two measures
                # come from two different shelves and need two different roles, not one "Y".
                role = SCATTER_AXIS_BY_CHANNEL.get(e["channel"])
            elif is_measure and visual_type == "lineClusteredColumnComboChart":
                # See `_combo_role_for`'s own docstring -- a dual-axis combo's two measures come
                # from the SAME shelf and are told apart by field identity, not by channel.
                role = _combo_role_for(ws, e["field"])
                if role is None:
                    self.declined_encodings.append({
                        "page": page_key, "visual": ws["name"], "field": e["field"],
                        "reason": "dual-axis combo chart: this measure could not be matched to "
                                  "either sub-pane's own Bar/Line mark by field identity -- "
                                  "dropped rather than guessed onto the wrong axis",
                    })
                    self.stats["combo_axis_unmatched"] += 1
                    continue
            else:
                role = roles[role_kind]
            if role is None:
                continue
            if is_measure:
                resolved_measure = True
            bucket = state.setdefault(role, {"projections": []})
            proj = _projection(entity, prop, is_measure)
            if proj in bucket["projections"]:
                continue
            cap = role_caps.get(role_kind)
            # Never guess which of several fields is "the" one for a capped well -- bind the
            # first that resolves, count the rest as dropped (not silently discarded), same
            # discipline the Text-shelf fallback below already applies to a capped card.
            if cap is not None and len(bucket["projections"]) >= cap:
                self.stats["field_role_capacity_dropped"] += 1
                continue
            bucket["projections"].append(proj)
            self.stats["projections"] += 1

        # Color-as-Category fallback: when a dimension on Color is the SOLE grouping axis (no
        # dimension came from Rows/Cols), populate the visual's own Category well from the Color
        # shelf. This is the `_query_state` counterpart of the two new VISUAL_RULES above that
        # classify such a worksheet as barChart/columnChart instead of card. Without this step
        # the classification fix would merely produce a correctly-typed chart with an empty
        # Category well -- every bar would collapse to a single un-grouped bar -- because the
        # primary Rows/Cols loop above only ever sees the measure, and the EXTRA_WELLS pass below
        # routes the color dimension to `Series` (which is then declined via
        # UNVERIFIED_COLOR_REJECTED). The dedup check in the EXTRA_WELLS loop will skip the same
        # field once it is already in Category, so no double-binding occurs.
        # Guard: only fires when the dim role exists (visual type has a Category-style well) AND
        # that well is still empty after the Rows/Cols pass -- i.e. exactly the worksheets whose
        # only grouping dimension is on Color, never ordinary charts that already have a
        # positional dimension.
        _cat_role = roles.get("dim")
        if _cat_role and not state.get(_cat_role):
            for _e in encodings:
                if _e["channel"] != "color" or _e.get("agg"):
                    continue
                _hit = self._resolve_field(_e["field"], None,
                                           candidate_tables=anchor_tables)
                if _hit is None:
                    self.stats["unresolved_field_refs"] += 1
                    continue
                _entity, _prop, _is_measure = _hit
                if _is_measure:
                    continue
                _proj = _projection(_entity, _prop, _is_measure)
                _bucket = state.setdefault(_cat_role, {"projections": []})
                if _proj in _bucket["projections"]:
                    continue
                _cap = role_caps.get("dim")
                if _cap is not None and len(_bucket["projections"]) >= _cap:
                    self.stats["field_role_capacity_dropped"] += 1
                    continue
                _bucket["projections"].append(_proj)
                self.stats["projections"] += 1

        # Colour / Size / Tooltip shelves -> their Power BI equivalents (see EXTRA_WELLS).
        # Gated on `state` being non-empty: these are ADDITIONS to a chart that already has a
        # real positional binding, never the thing that makes a visual data-bound on its own. A
        # legend on a visual with no axis would be meaningless, and the BAN/card case below owns
        # the "nothing on rows/cols" path.
        if EXTRA_ROLES and state:
            extra = EXTRA_WELLS.get(visual_type) or {}
            for e in encodings:
                role = extra.get(e["channel"])
                if role is None:
                    continue
                hit = self._resolve_field(e["field"], e.get("agg"), candidate_tables=anchor_tables)
                if hit is None:
                    self.stats["unresolved_field_refs"] += 1
                    continue
                entity, prop, is_measure = hit
                if role == "Series" and visual_type in UNVERIFIED_COLOR_REJECTED:
                    # See UNVERIFIED_COLOR_REJECTED's own comment: this exact well shape has been
                    # opened in real Desktop on two different visual types with two different
                    # field kinds and rejected both times. Report the loss instead of shipping a
                    # file already proven broken.
                    self.declined_encodings.append({
                        "page": page_key, "visual": ws["name"], "field": prop,
                        "reason": f"Legend/Series role on {visual_type} is rejected by Power BI "
                                  "Desktop (\"too many columns in the Legend bucket\") -- "
                                  "color split not reproduced",
                    })
                    self.stats["extra_role_declined_unverified"] += 1
                    continue
                proj = _projection(entity, prop, is_measure)
                # Skip a field already bound ANYWHERE in this visual, not just in this role's own
                # bucket. Tableau routinely puts the same dimension on both Rows and Colour; that
                # is one field doing two jobs in Tableau, but re-binding it to Series in Power BI
                # adds a redundant well rather than the series split the colour shelf implies.
                if any(proj in b.get("projections", []) for b in state.values()):
                    continue
                bucket = state.setdefault(role, {"projections": []})
                cap = EXTRA_ROLE_CAPS.get(role)
                if cap is not None and len(bucket["projections"]) >= cap:
                    self.stats["extra_role_capacity_dropped"] += 1
                    continue
                bucket["projections"].append(proj)
                self.stats["extra_role_projections"] += 1

        # Text/Color/Shape-shelf fallback -- see _shelf_shape's own comment for the measured
        # split behind this.  Consulted whenever Rows/Cols resolved NO measure -- state may still
        # be non-empty (a dimension resolved from Rows/Cols, e.g. a Tableau filter-dimension-as-
        # label idiom), but with no measure bound yet, the same fallback that finds a BAN card's
        # lone value must also find the value hiding on Text (or, when rows/cols are both empty,
        # on the Color/Shape channels) here.  A real Bar/Line chart that already bound a measure
        # from Rows/Cols is untouched -- `resolved_measure` is only True in that case.
        # The Color/Shape extension fires ONLY when ws["shelves"]["rows"] and ["cols"] are both
        # literally empty (not just when no measure resolved from them), because for any worksheet
        # that HAS positional shelf fields, the color encoding is a series/legend channel already
        # handled by EXTRA_WELLS -- NOT the measure source for the chart's main axis.
        if not resolved_measure:
            _shelves = ws.get("shelves") or {}
            _empty_shelves = (not _shelves.get("rows") and
                              not _shelves.get("cols"))
            for e in encodings:
                if e["channel"] == "text":
                    pass  # text-shelf: always include in this fallback
                elif (_empty_shelves and e["channel"] in ("color", "shape")
                      and e.get("agg")):
                    pass  # Color/Shape measure when rows AND cols both empty: Shape-KPI idiom
                elif (visual_type in ("tableEx", "multiRowCard")
                      and e["channel"] in ("color", "shape") and e.get("agg")):
                    # tableEx/multiRowCard have no legend/series well at all (see EXTRA_WELLS'
                    # own comment -- "no legend or size concept; every field is already a
                    # Value"), unlike a real chart where Color is a legitimate Series/legend
                    # that must NOT double as a value. So the `_empty_shelves` guard above,
                    # which exists only to protect that chart case, does not apply here: an
                    # aggregated Color field is a genuine Value even when Rows/Cols already
                    # hold real dimensions. Without this, Tableau's "colour-matrix heatmap"
                    # idiom (Square mark, two real dimensions on Rows/Cols, the sole measure on
                    # Colour, confirmed live on WorkbookA's `recency` worksheet -- VISUAL_RULES'
                    # Square+colour+no-size rule) lost its ONLY measure: the tableEx rendered
                    # with just the two dimension columns and nothing else, not even a plain
                    # table of numbers, let alone a heatmap.
                    pass
                else:
                    continue
                hit = self._resolve_field(e["field"], e.get("agg"), candidate_tables=anchor_tables)
                if hit is None:
                    self.stats["unresolved_field_refs"] += 1
                    continue
                entity, prop, is_measure = hit
                role_kind = "measure" if is_measure else "dim"
                if not is_measure:
                    _sf = self._formulas_by_canonical.get(prop)
                    if _sf is not None and (
                            _extract_string_literal(_sf) == "" or _sf.strip() == "INT(NULL)"):
                        self.stats["spacer_dims_skipped"] += 1
                        continue
                role = roles[role_kind]
                if role is None:
                    continue
                bucket = state.setdefault(role, {"projections": []})
                proj = _projection(entity, prop, is_measure)
                if proj in bucket["projections"]:
                    continue
                # A capped well (card's Values, or a pieChart/waterfallChart/treemap well that
                # fell through to this path because its Rows/Cols field was declined -- see
                # `ROLE_CAPS`'s own comment) holds only so many fields in real Desktop. Tableau's
                # floating-text BAN idiom often stacks several text objects (the value itself, an
                # arrow glyph, a positive/negative token) into what becomes one Power BI card
                # here. Only the FIRST resolving field per role is bound -- never guess which of
                # several is "the" one, and never overflow a well Desktop would reject
                # (tools/pbip_rules/checks.py's RPT-ROLE-03). The rest are counted, not dropped
                # silently.
                cap = role_caps.get(role_kind)
                if cap is not None and len(bucket["projections"]) >= cap:
                    self.stats["text_field_role_capacity_dropped"] += 1
                    continue
                bucket["projections"].append(proj)
                self.stats["projections"] += 1
        return {"queryState": state} if state else None

    # ------------------------------------------------------------------ visuals
    def _visual(self, page_key, zone, index, z_order, dashboard_name=None):
        if zone.get("hidden"):
            # The workbook author hid this zone in Tableau -- not a decline, not a gap, just
            # honoring the same "hidden-by-user" flag Tableau's own Desktop/Server viewer honors.
            # Emitting it would put content on the page Tableau itself never showed.
            self.stats["hidden_zones_skipped"] += 1
            return None
        g = zone["geom"]
        if any(g.get(k) is None for k in ("x", "y", "w", "h")):
            self.gaps.append({"page": page_key, "zone": zone["name"],
                              "reason": "zone has no resolvable geometry"})
            return None
        kind, vfilters = zone["kind"], []
        if kind in CHROME_ZONES:
            self.stats["chrome_zones_dropped"] += 1
            return None

        name = pbir_name(page_key, kind, zone["name"], index)
        body = {"drillFilterOtherVisuals": True}

        if kind == "worksheet":
            ws = self.ws_by_name.get(zone["name"])
            if ws is None:
                self.gaps.append({"page": page_key, "zone": zone["name"],
                                  "reason": "dashboard references a worksheet that is not in "
                                            "<worksheets> -- nothing to render"})
                return None
            # Tableau's own default is the OPPOSITE of Power BI's: a worksheet affects no other
            # sheet on its dashboard unless the author explicitly wired an Action FROM it. Leave
            # the Power BI default (True) only when this exact (dashboard, worksheet) pair is a
            # confirmed Action source -- see extract.py's `extract_action_source_worksheets`.
            if (dashboard_name, ws["name"]) not in self._action_source_worksheets:
                body["drillFilterOtherVisuals"] = False
            vt, why, decided = choose_visual(ws, calc_lookup=self._calc_lookup_by_caption)
            q = self._query_state(ws, vt, page_key=page_key)
            if not q and vt in WELLS:
                # WELLS defines real field-well roles for this visual type, so a query was
                # expected. Getting none back means every shelf field this worksheet
                # references failed to resolve -- declined by the formula parser, or a field
                # Tableau references on a shelf but defines nowhere in the workbook (the same
                # "dangling in the source" shape RESIDUE.md already reports for calc-to-calc
                # references, just at the shelf level). The container would still be
                # schema-valid and pass every structural check, but Desktop renders it as an
                # unlabeled blank rectangle -- exactly what
                # tools/pbip_rules/checks.py's RPT-ROLE-02 exists to catch. Decline instead of
                # emitting a visual with nothing behind it.
                self.gaps.append({"page": page_key, "zone": zone["name"],
                                  "reason": f"worksheet {ws['name']!r} resolved to visual type "
                                            f"{vt!r} ({why}) but every shelf field it "
                                            "references failed to resolve to a model object "
                                            "-- nothing to bind, so the visual is dropped "
                                            "rather than emitted blank"})
                return None
            if q:
                qents = _query_entities(q)
                non_param = {e for e in qents if not e.startswith("Param ")}
                if len(non_param) > 1 and not self._all_relationship_reachable(non_param):
                    # The real Desktop bug this whole check exists for: a worksheet's OWN field
                    # wells (not just a filter riding along) project fields from two or more
                    # tables with NO relationship path between them -- Tableau data blending
                    # carried straight into one visual with no stored Power BI relationship
                    # underneath it. Confirmed on WorkbookC, "Page L2
                    # "/"Page L4" pages: e.g. a lineChart's Category came from
                    # 'Level 3 - Mapping Data' while its Y came from 'Page L2 - Data
                    # ', with `relationships.tmdl` empty. Desktop: "Can't determine
                    # relationships between the fields... Underlying Error:
                    # InvalidUnconstrainedJoin." Same fail-closed rule `_visual_filters` already
                    # applies to a lone unreachable filter, one level up: the whole visual is
                    # dropped rather than shipped broken, and reported here rather than silently.
                    self.blend_stats[
                        f"worksheet {ws['name']!r}'s own field wells query "
                        f"{sorted(non_param)} -- tables with no relationship path between them "
                        "(Tableau data blend/cross-datasource fields carried into one visual "
                        "with no stored Power BI relationship); emitting it produces Desktop's "
                        "InvalidUnconstrainedJoin ('Can't determine relationships between the "
                        "fields'), so the visual is dropped rather than shipped broken"] += 1
                    self.gaps.append({"page": page_key, "zone": zone["name"],
                                      "reason": f"worksheet {ws['name']!r} resolved to visual "
                                                f"type {vt!r} but its own field wells span "
                                                f"tables {sorted(non_param)} with no "
                                                "relationship path between them -- Tableau data "
                                                "blend with no deterministic key found for this "
                                                "table pair, so the visual is dropped rather "
                                                "than left to fail in Desktop"})
                    return None
            body["visualType"] = vt
            self.stats["worksheet_visuals"] += 1
            if not decided:
                self.review.append({"page": page_key, "worksheet": ws["name"],
                                    "mark": ws["mark"], "emitted": vt, "reason": why})
                self.stats["review_visuals"] += 1
            else:
                self.stats["decided_visuals"] += 1
            if q:
                body["query"] = q
                self.stats["visuals_with_wells"] += 1
            if ws.get("title") and zone.get("show_title", True):
                body["objects"] = {"title": [{"properties": {"text": _lit(ws["title"])}}]}
            elif vt not in ("textbox", "image", "actionButton", "shape", "slicer"):
                # No explicit Tableau title (or zone hides it) -- suppress Power BI's own
                # auto-generated "SUM of X, SUM of Y by Z" header that would otherwise appear
                # for any chart/table visual with fields in its query. Tableau's own dashboard
                # viewer shows nothing when a worksheet has no <title> element; PBI must match.
                body.setdefault("objects", {})["title"] = [
                    {"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
            if (q and vt == "tableEx" and ws["mark"] == "Square"
                    and any(e["channel"] == "color" and e.get("agg") for e in ws["encodings"])
                    and not any(e["channel"] == "size" for e in ws["encodings"])):
                # Square+colour+no-size colour-matrix heatmap (see VISUAL_RULES and
                # `_query_state`'s own comment on this shape) -- the deferred half of that fix:
                # colour the measure's cells instead of shipping a plain, unstyled table.
                _color_proj = next((p for p in q["queryState"]["Values"]["projections"]
                                     if "Measure" in p["field"]), None)
                _color_e = next((e for e in ws["encodings"]
                                  if e["channel"] == "color" and e.get("agg")), None)
                if _color_proj and _color_e:
                    _cf_entity = _color_proj["field"]["Measure"]["Expression"]["SourceRef"]["Entity"]
                    _raw_hit = self._resolve_field(_color_e["field"], None,
                                                    candidate_tables=(_cf_entity,))
                    if _raw_hit and _raw_hit[0] == _cf_entity and not _raw_hit[2]:
                        body.setdefault("objects", {})["values"] = [_heatmap_gradient_object(
                            _cf_entity, _raw_hit[1], _color_proj["queryRef"])]
            url_action = (self.url_actions.get((dashboard_name, ws["name"]))
                         if dashboard_name else None)
            if url_action:
                # Tableau's URL dashboard action (click a mark on this worksheet -> open a fixed
                # address) -- see extract.py's `extract_url_actions` for exactly which shapes
                # reach here. Power BI's own visual-level click action, same `visualLink` object
                # the "Go to Sheet" nav-button case below already uses for `PageNavigation`, just
                # with `webUrl` instead of `navigationSection` -- confirmed against Microsoft's
                # own PBIR visualConfiguration schema (`RoleProjection`'s sibling `VisualLink`
                # definition lists `webUrl` as a real property, camelCase, alongside
                # `navigationSection`).
                body.setdefault("visualContainerObjects", {})["visualLink"] = [
                    {"properties": {
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                        "type": {"expr": {"Literal": {"Value": "'WebUrl'"}}},
                        "webUrl": _lit_str(url_action["url"]),
                    }}]
                self.stats["url_actions_wired"] += 1
            if vt in ("card", "multiRowCard"):
                # Desktop's own default for these two visual types prints the bound field's raw
                # name as a caption under every value ("Category label"), on by default with no
                # opt-out needed -- unlike Tableau's BAN/floating-text idiom this pair exists to
                # reproduce (see `choose_visual`'s own comment on `card`/`multiRowCard`), which
                # shows only the formatted value, nothing underneath. Left on, every one of these
                # visuals leaks the workbook author's internal calc name (WorkbookB Dashboard: a
                # "Lookup" value captioned "cl.Chosen Metric - Count - Label" underneath it) --
                # a name a report viewer was never meant to see, on every single-value tile this
                # project builds. Off by default here for the same reason a declined field is
                # reported rather than guessed: never show the pipeline's own internals to the
                # person reading the report.
                body.setdefault("objects", {})["categoryLabels"] = [
                    {"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
                # A plain `card`'s displayed value IS the worksheet's own label/mark-label text,
                # so Tableau's base-level label font (extract.py's `_label_font`, no `field=`
                # scope -- a per-value conditional-format override, a different and harder shape)
                # carries straight onto it. `objects.value.properties.fontSize`/`fontFamily` with
                # the Literal-expr shape below is the one Desktop-confirmed card font property
                # path this skill has (see `pbir-visual-authoring`'s own cardVisual reference) --
                # deliberately NOT extended to `multiRowCard` here, which has no confirmed shape
                # of its own yet. `fontSize` is a Double literal (`"<pt>D"`), not the plain
                # `"<pt>pt"` string a textbox's per-run `textStyle.fontSize` uses two schema
                # locations away -- the two are NOT interchangeable despite both being "a font
                # size on a visual".
                if vt == "card" and ws.get("label_font"):
                    lf = ws["label_font"]
                    value_props = {}
                    if lf.get("size"):
                        value_props["fontSize"] = {
                            "expr": {"Literal": {"Value": f"{lf['size']}D"}}}
                    if lf.get("family"):
                        value_props["fontFamily"] = {
                            "expr": {"Literal": {"Value": f"'{lf['family']}'"}}}
                    if value_props:
                        body["objects"]["value"] = [{"properties": value_props}]
            # A worksheet classified as `textbox` by the string-literal rule: the
            # dimension's entire formula is a hardcoded label like "Highlights Page" --
            # populate the textbox's paragraph content with that literal so Desktop renders
            # the label rather than showing an empty white box.  Uses the same
            # `objects.general.paragraphs` structure the ZONE_VISUAL["text"] path uses for
            # Tableau's own static text/caption zones (see the `kind == "text"` block in the
            # `elif kind in ZONE_VISUAL` branch below).  `setdefault` merges with an
            # existing `"title"` key if the worksheet also has a title set above.
            if vt == "textbox" and decided:
                all_dims = [e for e in ws["encodings"] if not e.get("agg")]
                if len(all_dims) == 1:
                    _nav_formula = self._calc_lookup_by_caption.get(all_dims[0]["field"])
                    _nav_label = _extract_string_literal(_nav_formula) if _nav_formula else None
                    if _nav_label is not None:
                        body.setdefault("objects", {})["general"] = [
                            {"properties": {"paragraphs": [
                                {"textRuns": [{"value": _nav_label}]}]}}]
                        self.stats["literal_label_textboxes"] += 1
            # Empty (not None) when the visual has no query at all -- `_visual_filters` only
            # enforces the relationship-reachability check when this is non-empty, matching the
            # scan that found this bug: a filter/slicer-only visual with nothing of its own to
            # relate a filter's table TO is a different, unscoped question.
            vfilters = _visual_filters(self, page_key, name, ws,
                                       _query_entities(q) if q else frozenset())
        elif kind == "web":
            self.gaps.append({"page": page_key, "zone": zone["name"],
                              "reason": "Tableau web-page zone: no Power BI visual embeds an "
                                        "arbitrary URL in a report page"})
            return None
        elif kind in ZONE_VISUAL:
            body["visualType"] = ZONE_VISUAL[kind]
            if kind == "text" and zone.get("text_runs"):
                # `paragraphs` is rich text -- an array of paragraph/textRun objects, not a
                # scalar property -- so it does NOT take `_lit()`'s literal-expression wrapper
                # the way `title.text` does. Wrapping it that way parses as valid JSON but is
                # not a shape Desktop's textbox visual renders: the page LOOKS empty even though
                # the real Tableau text is sitting right there in the file. Caught by
                # cross-checking against `tools/twb2pbip`'s independently-written PBIR writer,
                # which uses this exact shape for the same property.
                #
                # Each run keeps its OWN bold/italic/color/fontSize -- previously flattened to
                # one plain string, losing all of it (WorkbookJ alone has 128 multi-run text zones
                # mixing bold+plain in one caption). `horizontalTextAlignment` is paragraph-level
                # in this schema (Tableau does not vary alignment mid-paragraph in any real
                # corpus example checked), taken from the zone's own declared alignment.
                text_runs = []
                for run in zone["text_runs"]:
                    tr = {"value": run["text"]}
                    style = {}
                    if run.get("bold"):
                        style["fontWeight"] = "bold"
                    if run.get("italic"):
                        style["fontStyle"] = "italic"
                    if run.get("color"):
                        style["color"] = run["color"]
                    if run.get("fontsize"):
                        style["fontSize"] = f"{run['fontsize']}pt"
                    if run.get("fontname"):
                        style["fontFamily"] = run["fontname"]
                    if style:
                        tr["textStyle"] = style
                    text_runs.append(tr)
                paragraph = {"textRuns": text_runs}
                if zone.get("text_align"):
                    paragraph["horizontalTextAlignment"] = zone["text_align"]
                body["objects"] = {"general": [{"properties": {"paragraphs": [paragraph]}}]}
            if kind == "text" and zone.get("zone_style"):
                # Tableau's own zone-style background-color/border -- never read before this
                # (see extract.py's parse_zone_style docstring: 136 of WorkbookJ's 1064 text zones
                # have a real background, used as colored banner bars/dividers, rendering as a
                # plain "blank" white box without this). Applies even when the zone has no text
                # of its own (a purely decorative colored divider), independent of the
                # paragraphs block above.
                zs = zone["zone_style"]
                vco = {}
                if zs.get("background_color"):
                    vco["background"] = [{"properties": {
                        "color": {"solid": {"color": _lit(zs["background_color"])}}}}]
                if zs.get("border_color"):
                    vco["border"] = [{"properties": {
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                        "color": {"solid": {"color": _lit(zs["border_color"])}}}}]
                if vco:
                    body["visualContainerObjects"] = vco
            if kind == "bitmap":
                item = self._register_image(zone.get("image"))
                if item is None:
                    self.gaps.append({"page": page_key, "zone": zone["name"],
                                      "reason": "image zone references a file not present in "
                                                "the .twbx package"})
                    return None
                body["objects"] = {"image": [{"properties": {"sourceFile": {"image": {
                    "name": _lit(item),
                    "url": {"expr": {"ResourcePackageItem": {
                        "PackageName": "RegisteredResources", "PackageType": 1,
                        "ItemName": item}}},
                    "scaling": _lit("Normal"),
                }}}}]}
            if kind in ("filter", "color"):
                # Scope the lookup to the zone's own originating datasource, mirroring
                # _visual_filters (candidate_tables=query_entities) and _query_state
                # (candidate_tables=anchor_tables) for worksheet field-well encodings.
                # The zone's `param` attribute is the raw Tableau field reference
                # `[DS_INTERNAL_NAME].[none:field:nk]`; the first bracket-group is the
                # exact key `na.table()` was called with for that datasource, so
                # `na.tables.get(that_key)` returns the correct PBI table without any
                # caption-guessing.  Without this, _resolve_field's global fallback returns
                # whichever table was allocated first -- confirmed to be Gamma on
                # WorkbookA, landing Alpha's ctr/grp/item_typ/item_id filters on the wrong
                # table and blanking every visual that filtered through them.  Falls through to
                # candidate_tables=None (existing global search) when param is absent or the
                # key isn't in na.tables (e.g. a split federated datasource whose sub-tables
                # are keyed by object caption, not parent name) -- no regression in those cases.
                _cand = None
                _raw_param = zone.get("param") or ""
                _ds_m = re.match(r"\[([^\]]+)\]", _raw_param)
                if _ds_m:
                    _pbi_tbl = self.na.tables.get(_ds_m.group(1))
                    if _pbi_tbl:
                        _cand = (_pbi_tbl,)
                hit = self._resolve_field(zone["field"], None, candidate_tables=_cand) if zone.get("field") else None
                if hit:
                    entity, prop, is_measure = hit
                    body["query"] = {"queryState": {"Values": {"projections": [
                        _projection(entity, prop, is_measure)]}}}
                    # Tableau's own declared quick-filter style ('dropdown'/'checkdropdown')
                    # is a compact dropdown control with search and select-all -- Power BI's
                    # slicer default (no style set at all) is a plain vertical list with
                    # neither, which is exactly the mismatch real SME feedback flagged on this
                    # corpus. Only touched when Tableau's own zone attribute says dropdown --
                    # a 'list'-style (or unset) quick filter is left at Power BI's own default,
                    # never guessed into a style Tableau never declared.
                    if kind == "filter" and zone.get("filter_mode") in ("dropdown",
                                                                        "checkdropdown"):
                        self.stats["dropdown_filters_styled"] += 1
                        body["objects"] = {
                            "data": [{"properties": {"mode": _lit("Dropdown")}}],
                            "general": [{"properties": {
                                "selfFilterEnabled": {"expr": {"Literal": {"Value": "true"}}}}}],
                            "selection": [{"properties": {
                                "selectAllCheckboxEnabled": {
                                    "expr": {"Literal": {"Value": "true"}}}}}],
                        }
                else:
                    # A slicer/legend with no field, or one whose field never resolves to a
                    # model object, has nothing to slice on -- schema-valid, but Desktop
                    # renders it as an empty control with no indication why (RPT-ROLE-02, same
                    # failure mode as the worksheet case above). Decline rather than emit it.
                    self.stats["unresolved_field_refs"] += 1
                    self.gaps.append({"page": page_key, "zone": zone["name"],
                                      "reason": f"{kind} zone's field {zone.get('field')!r} "
                                                "did not resolve to a model object -- a "
                                                "slicer with nothing to slice would render as "
                                                "an empty control"})
                    return None
            if kind == "paramctrl":
                field = str(zone.get("field") or "").strip("[]")
                rec = (self.param_index.get(zone.get("field"))
                       or self.param_index.get(field)
                       or self.param_index.get(field.lower())) if zone.get("field") else None
                if rec:
                    body["query"] = {"queryState": {"Values": {"projections": [
                        _projection(rec["table"], rec["column"], False)]}}}
                    # Every parameter's own domain column is named the same generic "Value"
                    # (see `NameAuthority.parameter`'s convention), so Desktop's own slicer-
                    # header default (the bound field's raw name, on by default) prints "Value"
                    # on EVERY paramctrl slicer in the report -- indistinguishable from each
                    # other with no indication which parameter each one controls (confirmed on
                    # WorkbookB Dashboard's "Sheet 2 - Summary" page: 3 slicers, all captioned
                    # "Value"). Prefer the ZONE's own "Edit Title..." override when Tableau set
                    # one (`extract.py`'s `custom_title` capture) -- a purpose-built caption for
                    # THIS control specifically ("Chart Metric Type" for the `pl.Chosen Metric
                    # ` parameter, "Color Bar Chart By" for `pl.Color By`) that reads
                    # nothing like the parameter's own internal name; falling back to the
                    # parameter's own real caption otherwise, stripped of the same 'cl.'/'pl.'
                    # organizational prefix `_projection`'s own `displayName` override strips
                    # everywhere else, so this header does not reintroduce that exact leak
                    # (e.g. "pl.Selector") when there is no custom title to prefer.
                    header_text = (zone.get("custom_title")
                                  or _strip_org_prefix(rec["tableau"]) or rec["tableau"])
                    body.setdefault("objects", {})["header"] = [
                        {"properties": {"text": _lit_str(header_text)}}]
                else:
                    self.stats["unresolved_field_refs"] += 1
                    self.gaps.append({"page": page_key, "zone": zone["name"],
                                      "reason": f"paramctrl zone's field "
                                                f"{zone.get('field')!r} did not resolve to a "
                                                "parameter -- a slicer with nothing to slice "
                                                "would render as an empty control"})
                    return None
            if "query" in body:
                self.stats["visuals_with_wells"] += 1
            # Counted separately from worksheet visuals: mixing them into one numerator against a
            # worksheet-zone denominator produced a 290% "coverage" figure. Counted here, after
            # the decline checks above, so a dropped filter/color/paramctrl zone is not also
            # counted as an emitted control visual.
            self.stats["control_visuals"] += 1
        elif kind == "dashboard-object" and zone.get("nav_button_hint"):
            # A Tableau "Go to Sheet" navigation button. Power BI's equivalent is a `button`
            # visual (visualType `actionButton`) with a Page Navigation action.
            #
            # The target IS resolvable: Tableau stores the button's `window-id` GUID as a
            # `<simple-id uuid='{GUID}' />` child of the matching `<windows>/<window>` element
            # (NOT an `id` attribute on `<window>` itself -- that was checked and correctly
            # found absent; the `<simple-id>` child was missed the first time this was
            # investigated). extract.py's `_nav_button_target` resolves this GUID against every
            # window in the workbook and returns the target's dashboard name -- confirmed
            # against real corpus workbooks: WorkbookJ resolves 21/21
            # button targets this way, WorkbookH resolves 8/9 (the 1 miss
            # has no `<simple-id>` anywhere in the file at all -- a genuinely dangling
            # reference, correctly left unresolvable, not a bug in the lookup).
            #
            # Only wired when the resolved name is one of THIS project's own built dashboards
            # (`self._dashboard_names`) -- a target that resolves to a bare worksheet (no page
            # of its own here) or to nothing at all still declines exactly as before.
            target = zone.get("nav_button_target")
            if target and target in self._dashboard_names:
                target_key = pbir_name("page", target)
                body["visualType"] = "actionButton"
                body["visualContainerObjects"] = {"visualLink": [{"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "type": _lit_str("PageNavigation"),
                    "navigationSection": _lit_str(target_key),
                }}]}
                caption = zone.get("nav_button_caption")
                if caption:
                    body["objects"] = {"text": [{"properties": {
                        "text": _lit_str(caption)}}]}
                self.stats["nav_buttons_wired"] += 1
            else:
                self.gaps.append({
                    "page": page_key, "zone": zone["name"],
                    "reason": f"Tableau 'Go to Sheet' navigation button captioned "
                              f"{zone['nav_button_hint']!r} -- Power BI's equivalent is a "
                              f"Button visual with a Page Navigation action, but the target "
                              f"page could not be resolved to one of this project's own "
                              f"dashboards; add the button by hand in Desktop, targeting "
                              f"whichever page matches this caption"})
                return None
        else:
            self.gaps.append({"page": page_key, "zone": zone["name"],
                              "reason": f"zone type {kind!r} has no mapped Power BI visual"})
            return None

        # Tableau permits a small deliberate off-canvas bleed on DECORATIVE zones in its own
        # primary <zones> tree -- e.g. y='-333' on a logo bitmap (WorkbookE), y='-141'/
        # y='-845' on a section-break image and a title-text zone (WorkbookD).
        # After scaling to this page's canvas that comes out as a small negative x/y (-1.0 to
        # -3.0 on a ~1280x720-scale page) -- PBIR's schema requires non-negative visual
        # positions, and Desktop's own report editor would never let a human author one, so a
        # generator emitting it anyway is exactly the state-no-human-could-author RPT-POS-01
        # exists to catch. `w`/`h` are independent zone attributes in Tableau's source XML --
        # NEVER derived from `x`/`y` extended out to a fixed right/bottom edge (see extract.py's
        # geom(), which reads all four straight off the zone element with no cross-dependency) --
        # so clamping a negative position to 0 needs no matching width/height adjustment: this is
        # a floor, not an edge-preserving shrink. A 1-3 unit bleed on this canvas scale is
        # visually negligible. Position-only: a legitimately zero or positive x/y is untouched,
        # and width/height are never touched either way.
        pos_x = max(0.0, round(g["x"] * self.page_w, 2))
        pos_y = max(0.0, round(g["y"] * self.page_h, 2))

        out = {
            "$schema": SCHEMA["visual"],
            "name": name,
            "position": {"x": pos_x, "y": pos_y,
                         "z": z_order, "width": round(g["w"] * self.page_w, 2),
                         "height": round(g["h"] * self.page_h, 2), "tabOrder": z_order},
            "visual": body,
        }
        if kind == "worksheet" and vfilters:
            out["filterConfig"] = {"filters": vfilters}
        return name, out

    def _register_image(self, param):
        if not param:
            return None
        base = os.path.basename(str(param).replace("\\", "/"))
        for p in self.ir["package"]["images"]:
            if os.path.basename(p) == base:
                self.images[base] = p
                return base
        return None

    # A chart-type visual (as opposed to card/tableEx/multiRowCard/slicer/textbox/image, which
    # render fine at any size) needs real plotting area -- axis, bars/line/points -- to show
    # anything at all. Confirmed live on WorkbookA's Detail Page page: Tableau's own zone geometry
    # for `5.2 - Seg group`/`Sheet 10 - Seg B` etc. is genuinely only ~57-68px tall once scaled
    # to this page's canvas (Tableau itself compresses a chart down to fit almost any space, no
    # matter how cramped), but Power BI Desktop has its own hard rendering floor below which it
    # gives up and shows a generic placeholder icon instead of real bars -- confirmed by contrast
    # against this SAME page's `Sheet 6 - Segmentation(Group/Alt)` charts, which render correctly
    # at ~300px tall. 120 is a conservative pick comfortably above the confirmed-broken 57-68px
    # band without being needlessly large.
    _CHART_VISUAL_TYPES = frozenset({
        "barChart", "columnChart", "lineChart", "areaChart", "scatterChart", "waterfallChart",
        "lineClusteredColumnComboChart", "pieChart", "treemap", "ganttChart",
    })
    _MIN_CHART_HEIGHT = 120.0

    def _grow_undersized_charts(self, visuals):
        """Grow a too-short chart's height toward `_MIN_CHART_HEIGHT`, but never past whatever
        sibling visual sits closest below it in the SAME horizontal range -- growing blind would
        just trade "renders as a placeholder icon" for "overlaps the visual underneath it",
        which is not an improvement. `visuals` is this page's own `(name, out)` list, already in
        final page-pixel position units (scaled once per page, same units for every entry) --
        safe to compare directly across entries without any rescaling.
        """
        boxes = [{"name": n, "x": o["position"]["x"], "y": o["position"]["y"],
                  "w": o["position"]["width"], "h": o["position"]["height"], "out": o}
                 for n, o in visuals]
        for b in boxes:
            if b["out"]["visual"].get("visualType") not in self._CHART_VISUAL_TYPES:
                continue
            if b["h"] >= self._MIN_CHART_HEIGHT:
                continue
            x0, x1, bottom = b["x"], b["x"] + b["w"], b["y"] + b["h"]
            limit = None
            for other in boxes:
                if other is b:
                    continue
                ox0, ox1 = other["x"], other["x"] + other["w"]
                if ox1 <= x0 or ox0 >= x1:
                    continue  # no horizontal overlap -- a different column, not in the way
                if other["y"] >= bottom and (limit is None or other["y"] < limit):
                    limit = other["y"]
            available = (limit - b["y"]) if limit is not None else None
            new_h = self._MIN_CHART_HEIGHT if available is None else min(self._MIN_CHART_HEIGHT,
                                                                          available)
            if new_h > b["h"]:
                b["out"]["position"]["height"] = round(new_h, 2)
                self.stats["undersized_chart_grown"] += 1
                if new_h < self._MIN_CHART_HEIGHT:
                    self.stats["undersized_chart_grown_capped"] += 1

    _TIDY_TARGET_GAP = 20.0
    _TIDY_BOTTOM_MARGIN = 32.0
    _TIDY_BACKDROP_HEIGHT_FRACTION = 0.6

    def _tidy_vertical_gaps(self, visuals):
        """Shrink an oversized vertical gap between two of Tableau's own section "bands" (a
        band = a maximal run of visuals whose y-ranges transitively overlap -- e.g. a section's
        header bar plus everything stacked under it) down to a comfortable fixed gap, shifting
        that band and every band below it UP by the difference as one rigid-body translation.
        Every visual's position relative to its own band-mates is untouched; only the empty
        space BETWEEN bands shrinks -- addresses "too much empty space around visuals" without
        restructuring anything Tableau's own author laid out within a section, and without
        disturbing horizontal (column) alignment since x is never touched.

        A single visual whose OWN height exceeds `_TIDY_BACKDROP_HEIGHT_FRACTION` of the page's
        declared height is treated as a full-page decorative backdrop, not a content section --
        excluded from band clustering AND left untouched by the shift (a backdrop is meant to
        span a fixed area, not hug compacted content), though its own bottom edge still counts
        toward the page's final recommended height so compacted content never ends up
        underneath where the backdrop stops.

        Returns the new recommended page height (tight to the compacted content plus a fixed
        bottom margin, so the page's trailing dead space shrinks too), or `None` if there were
        no visuals to measure at all.
        """
        if not visuals:
            return None
        backdrop_cutoff = self.page_h * self._TIDY_BACKDROP_HEIGHT_FRACTION
        boxes = [{"y": o["position"]["y"], "bottom": o["position"]["y"] + o["position"]["height"],
                  "out": o}
                 for _, o in visuals if o["position"]["height"] <= backdrop_cutoff]
        if boxes:
            boxes.sort(key=lambda b: b["y"])
            bands, band, running_bottom = [], [boxes[0]], boxes[0]["bottom"]
            for b in boxes[1:]:
                if b["y"] <= running_bottom:
                    band.append(b)
                    running_bottom = max(running_bottom, b["bottom"])
                else:
                    bands.append((band, running_bottom))
                    band, running_bottom = [b], b["bottom"]
            bands.append((band, running_bottom))

            shift, prev_bottom_shifted = 0.0, None
            for band, band_bottom in bands:
                band_top = min(b["y"] for b in band)
                if prev_bottom_shifted is not None:
                    gap = (band_top - shift) - prev_bottom_shifted
                    if gap > self._TIDY_TARGET_GAP:
                        shift += gap - self._TIDY_TARGET_GAP
                        self.stats["layout_gaps_compacted"] += 1
                for b in band:
                    b["out"]["position"]["y"] = round(b["y"] - shift, 2)
                prev_bottom_shifted = band_bottom - shift

        # PBIR's page height, unlike a VISUAL's own position fields (float throughout this file,
        # confirmed fine everywhere else), must be a whole integer -- confirmed by a real
        # Desktop crash report surfaced during this fix's own live testing: Export-to-PDF's
        # print-metadata deserializer reads `Sections[0].Height` as an Int32 and threw
        # `Newtonsoft.Json.JsonReaderException: Input string '1406.01' is not a valid integer`
        # the moment this method handed back a 2-decimal float. `db["height"]` (the pre-tidy
        # fallback in `build()`) was always a plain int already; round UP so the bottom margin
        # this method promises never gets silently shaved off by truncation.
        return math.ceil(max(o["position"]["y"] + o["position"]["height"] for _, o in visuals)
                          + self._TIDY_BOTTOM_MARGIN)

    _SLIVER_MAX = 20.0
    _SLIVER_AREA_RATIO = 0.3
    _SLIVER_MAX_PASSES = 20

    def _fix_sliver_overlaps(self, visuals):
        """Tableau's own zone geometry occasionally has two sibling zones overlapping by a few
        pixels at the shared canvas scale -- confirmed real example: WorkbookA Alpha's zone 26
        ('Sheet 5 - Growth Rate A') and zone 81 ('2. Item ID') overlap by ~8px in x (full height
        in y), inherited verbatim from the .twb's own x/w values -- not something this pipeline's
        own scaling introduced. Tableau's own renderer tolerates this (internal padding/borders
        absorb a few px of nominal overlap); Power BI's visual containers do not -- the result is
        visible clipping ("Group" rendering as "ssuer" because the neighbour's edge sits on top
        of its first letter) or a stray hairline where two borders cross. A real-corpus survey of
        this workbook found 251 such pairs across all 10 pages -- systematic, not a one-off.

        This is a thin SLIVER overlap in exactly one axis (full-length in the other), not the
        large-area containment a background/decorative zone legitimately has with everything
        drawn on top of it (e.g. a full-page backdrop textbox) -- the two are told apart by the
        overlap-area-to-smaller-box-area RATIO, not by overlap size alone: a sliver's ratio is
        tiny (one box's edge barely clips the other), a real containment's ratio is close to 1
        (the smaller box sits almost entirely inside the bigger one).

        Resolved by nudging the geometrically LATER box's start coordinate forward by exactly
        the overlap amount, along whichever axis has the SMALLER overlap (the sliver axis) -- a
        pure translation, never a resize, so nothing ever ends up smaller than Tableau's own
        author sized it. Re-scans after each nudge since fixing one pair can occasionally expose
        another (two slivers chained along the same edge); capped at a few passes so a
        pathological cluster is left alone (not cascaded into an unbounded drift) rather than
        risk compounding small nudges into a visible misalignment.
        """
        boxes = [{"x": o["position"]["x"], "y": o["position"]["y"],
                  "w": o["position"]["width"], "h": o["position"]["height"], "out": o}
                 for _, o in visuals]
        for _pass in range(self._SLIVER_MAX_PASSES):
            fixed_this_pass = 0
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    a, b = boxes[i], boxes[j]
                    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
                    bx0, by0, bx1, by1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
                    ox = min(ax1, bx1) - max(ax0, bx0)
                    oy = min(ay1, by1) - max(ay0, by0)
                    if ox <= 0 or oy <= 0:
                        continue
                    smaller = min(a["w"] * a["h"], b["w"] * b["h"])
                    if smaller <= 0 or (ox * oy) / smaller >= self._SLIVER_AREA_RATIO:
                        continue
                    if min(ox, oy) >= self._SLIVER_MAX:
                        continue
                    if ox <= oy:
                        later = b if bx0 >= ax0 else a
                        later["x"] = round(later["x"] + ox, 2)
                        later["out"]["position"]["x"] = later["x"]
                    else:
                        later = b if by0 >= ay0 else a
                        later["y"] = round(later["y"] + oy, 2)
                        later["out"]["position"]["y"] = later["y"]
                    self.stats["sliver_overlaps_fixed"] += 1
                    fixed_this_pass += 1
            if not fixed_this_pass:
                break

    # ------------------------------------------------------------------ pages
    def build(self):
        order, active = [], None
        for db in self.ir["dashboards"]:
            key = pbir_name("page", db["name"])
            self.page_w, self.page_h = db["width"], db["height"]
            visuals, z = [], 0
            for i, zone in enumerate(db["zones"]):
                made = self._visual(key, zone, i, z, dashboard_name=db["name"])
                if made is None:
                    continue
                visuals.append(made)
                z += 1
            self._grow_undersized_charts(visuals)
            self._fix_sliver_overlaps(visuals)
            tidied_height = self._tidy_vertical_gaps(visuals)
            for vname, vjson in visuals:
                self.files[f"definition/pages/{key}/visuals/{vname}/visual.json"] = vjson
            self.files[f"definition/pages/{key}/page.json"] = {
                "$schema": SCHEMA["page"], "name": key, "displayName": db["name"],
                "displayOption": "FitToPage",
                "height": tidied_height or db["height"], "width": db["width"],
            }
            order.append(key)
            active = active or key
            self.stats["pages"] += 1

        # A workbook with worksheets but no dashboard would otherwise produce a report with zero
        # pages, which Desktop opens with no tabs and no error -- indistinguishable from success.
        if not order:
            key = pbir_name("page", "orphan-worksheets")
            self.page_w, self.page_h = 1280, 720
            visuals = []
            for i, ws in enumerate(self.ir["worksheets"]):
                zone = {"kind": "worksheet", "name": ws["name"],
                        "geom": {"x": 0.02 + 0.32 * (i % 3), "y": 0.02 + 0.33 * (i // 3 % 3),
                                 "w": 0.30, "h": 0.30}}
                made = self._visual(key, zone, i, i)
                if made:
                    visuals.append(made)
            for vname, vjson in visuals:
                self.files[f"definition/pages/{key}/visuals/{vname}/visual.json"] = vjson
            self.files[f"definition/pages/{key}/page.json"] = {
                "$schema": SCHEMA["page"], "name": key,
                "displayName": "Worksheets (no dashboard in source)",
                "displayOption": "FitToPage", "height": 720, "width": 1280}
            order, active = [key], key
            self.stats["pages"] += 1
            self.stats["synthetic_page"] += 1

        self.files["definition/pages/pages.json"] = {
            "$schema": SCHEMA["pages"], "pageOrder": order, "activePageName": active}
        self.files["definition/version.json"] = {"$schema": SCHEMA["version"],
                                                 "version": "2.0.0"}
        self.files["definition/report.json"] = self._report()
        self.files["definition.pbir"] = {
            "$schema": SCHEMA["pbir"], "version": "4.0",
            "datasetReference": {"byPath": {"path": f"../{self.model_dir}"}}}
        return self.files

    def _report(self):
        packages = []
        if self.images:
            packages.append({"name": "RegisteredResources", "type": "RegisteredResources",
                             "items": [{"name": n, "path": n, "type": "Image"}
                                       for n in sorted(self.images)]})
        # A declared baseTheme must ALSO appear as a SharedResources item and exist on disk --
        # confirmed against a Desktop-authored report.json, which carries all three. Declaring the
        # theme in themeCollection alone was a real defect.
        packages.append({"name": "SharedResources", "type": "SharedResources",
                         "items": [{"name": BASE_THEME, "path": f"BaseThemes/{BASE_THEME}.json",
                                    "type": "BaseTheme"}]})
        rep = {
            "$schema": SCHEMA["report"],
            "themeCollection": {"baseTheme": {
                "name": BASE_THEME,
                "reportVersionAtImport": {"visual": "2.6.0", "report": "3.1.0", "page": "2.3.0"},
                "type": "SharedResources"}},
            # Desktop emits this; harmless and keeps the file closer to a round-tripped one.
            "objects": {"section": [{"properties": {"verticalAlignment": _lit("Top")}}]},
            "settings": {"useStylableVisualContainerHeader": True,
                         "exportDataMode": "AllowSummarized",
                         "defaultDrillFilterOtherVisuals": True,
                         "allowChangeFilterTypes": True,
                         "useEnhancedTooltips": True,
                         "useDefaultAggregateDisplayName": True},
        }
        if packages:
            rep["resourcePackages"] = packages
        return rep


DEFAULT_THEME_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets", f"{BASE_THEME}.json")


def write_report(ir, authority, out_dir, semantic_model_dir_name, theme_src=None,
                  relationship_pairs=None):
    """Emit <out>/**. Returns the builder for its stats/review/gaps."""
    import shutil
    b = ReportBuilder(ir, authority, semantic_model_dir_name, relationship_pairs=relationship_pairs)
    for rel, payload in b.build().items():
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
    # resourcePackages and disk must agree: a package item with no file on disk makes Desktop
    # refuse the project, and a file with no item is dead weight. build_pbip asserts this.
    if b.images:
        dst = os.path.join(out_dir, "StaticResources", "RegisteredResources")
        os.makedirs(dst, exist_ok=True)
        for item, src in b.images.items():
            shutil.copy2(src, os.path.join(dst, item))
    theme_src = theme_src or DEFAULT_THEME_SRC
    if os.path.isfile(theme_src):
        dst = os.path.join(out_dir, "StaticResources", "SharedResources", "BaseThemes")
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(theme_src, os.path.join(dst, os.path.basename(theme_src)))
    return b


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Every filter value out of extract.py is a `str` regardless of the column's real type (a
    # Tableau XML attribute has no type of its own). A real corpus workbook had a categorical
    # filter on an int64 column emitted as the string literal `'1'` instead of the numeric literal
    # `1L`; Desktop refused to render the visual: "This visual contains one or more filters with
    # ... type mismatches." `dtype` must win over the value's own (always-str) Python type.
    assert _filter_literal("1", "int64") == {"Literal": {"Value": "1L"}}, (
        "an int64 filter value must NOT stay a quoted string literal")
    assert _filter_literal("1.5", "double") == {"Literal": {"Value": "1.5D"}}
    assert _filter_literal("true", "boolean") == {"Literal": {"Value": "true"}}
    assert _filter_literal("0", "boolean") == {"Literal": {"Value": "false"}}, (
        "a boolean filter value must resolve from its declared dtype, not from str(v) truthiness "
        "(str('0') is truthy in Python)")
    assert _projection("T", "cl.Chosen Metric - Count", False)["displayName"] == (
        "Chosen Metric - Count"), (
        "a 'cl.'-namespaced field must get a stripped-prefix displayName override in every "
        "projection, without changing its real Property/queryRef")
    assert _projection("T", "cl.Chosen Metric - Count", False)["field"]["Column"][
        "Property"] == "cl.Chosen Metric - Count", (
        "the displayName override must never touch the real bound field")
    assert _projection("T", "pl.Selector", True)["displayName"] == "Selector", (
        "the 'pl.' parameter-label prefix strips the same way as 'cl.'")
    assert "displayName" not in _projection("T", "Account Name", False), (
        "'Account Name' must NOT be mistaken for the 'cl.' prefix -- no literal dot right after "
        "the 2-letter code, so no override key is added at all")
    assert "displayName" not in _projection("T", "Real Field", False)

    assert _filter_literal("Signature", "string") == {"Literal": {"Value": "'Signature'"}}
    assert _filter_literal("Signature", None) == {"Literal": {"Value": "'Signature'"}}, (
        "no dtype known -- must still fall back to a safe quoted string, never guess numeric")

    # A discrete date/dateTime filter must be DECLINED, not emitted with a guessed literal shape
    # -- the correct PBIR shape for one is unconfirmed against real Desktop, unlike
    # string/int64/double/boolean (each confirmed above or in the bug log). Same reasoning as the
    # `range`-kind decline right above this in _visual_filters.
    class _FakeBuilder:
        filter_stats = collections.Counter()
        context_filters = []
        field_datatype = {"OrderDate": "date"}

        def _resolve_field(self, field, agg, candidate_tables=None):
            return "T", "OrderDate", False

    _fb = _FakeBuilder()
    _decided = _visual_filters(_fb, "p", "v", {
        "filters": [{"kind": "in", "field": "OrderDate", "values": ["2026-05-28"],
                     "exclude": [], "context": False}]})
    assert _decided == [], "a date/dateTime categorical filter must be declined, not emitted"
    assert any("date/dateTime categorical filter" in k for k in _fb.filter_stats), (
        "the decline must be visible in filter_stats, not silently dropped")

    # The real Desktop bug: WorkbookC, "Page L4" page,
    # visual ffffffffffffffffffff. Its OWN query projects only 'Level 3 - Mapping A
    # '[Stage Name], but its filterConfig also carried a filter on 'Level 4 - Mapping B
    # '[Group Area (copy)] -- a table with NO relationship path to the
    # visual's own query table, because relationships.tmdl is empty for this corpus (Tableau
    # data blending, not a stored key). Desktop refused the page: "Can't determine
    # relationships between the fields... InvalidUnconstrainedJoin". A filter on the query's OWN
    # table, and a filter on a `Param ` table, must both still be emitted -- this is a targeted
    # decline, not a blanket one.
    class _FakeBlendBuilder:
        filter_stats = collections.Counter()
        context_filters = []
        field_datatype = {}
        rel_adj = collections.defaultdict(set)  # empty graph -- exactly this corpus's reality

        _FIELDS = {
            "Stage Name": ("Level 3 - Mapping A", "Stage Name", False),
            "Group Area (copy)": ("Level 4 - Mapping B", "Group Area (copy)",
                                   False),
            "Show as": ("Param Show as_", "Value", False),
        }

        def _resolve_field(self, field, agg, candidate_tables=None):
            return self._FIELDS[field]

    _bb = _FakeBlendBuilder()
    _query_ents = {"Level 3 - Mapping A"}
    _flt_ws = {"filters": [
        {"kind": "in", "field": "Group Area (copy)", "values": ["X"], "exclude": [],
         "context": False},
        {"kind": "in", "field": "Stage Name", "values": ["Retention"], "exclude": [],
         "context": False},
        {"kind": "in", "field": "Show as", "values": ["Y"], "exclude": [], "context": False},
    ]}
    _emitted = _visual_filters(_bb, "p", "v", _flt_ws, _query_ents)
    _emitted_entities = {f["field"]["Column"]["Expression"]["SourceRef"]["Entity"]
                          for f in _emitted}
    ok = (_emitted_entities == {"Level 3 - Mapping A", "Param Show as_"}
          and any("no relationship path" in k for k in _bb.filter_stats))
    bad0 = 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} cross-table blend filter declined, same-table + Param "
          f"filters kept: emitted entities={_emitted_entities}")

    # A visual with NO query at all (a pure filter/slicer control, `query_entities` empty) must
    # NOT trigger the reachability check -- there is no "own table" to be unrelated to, and this
    # shape is out of the bug's own scope (confirmed by the scan that found it: it only counted
    # visuals where `query_ents` was non-empty).
    _bb2 = _FakeBlendBuilder()
    _emitted2 = _visual_filters(_bb2, "p", "v", _flt_ws, frozenset())
    ok = len(_emitted2) == 3
    bad0 += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a query-less visual's filters are left untouched: "
          f"{len(_emitted2)} emitted")

    # `_resolve_field`'s `candidate_tables`: a caption genuinely owned by TWO tables (real corpus
    # shape -- "View Flag" is a real column on both 'Beta' and 'Gamma', from two
    # DIFFERENT Tableau datasources that happen to share a caption). Global-first-match alone
    # cannot tell these apart; the visual's own query table must win.
    class _FakeNA:
        def __init__(self, owners):
            self._owners = owners  # spelling -> [(table, name, kind), ...] in global-match order

        def by_spelling(self, spelling, table=None, emitted_only=False):
            rows = self._owners.get(spelling, [])
            if table is not None:
                for t, n, k in rows:
                    if t == table:
                        return t, n, k
                return None, None, None
            if rows:
                return rows[0]
            return None, None, None

        def lookup(self, table, key, emitted_only=False):
            return None, None

    class _FakeResolveBuilder:
        def __init__(self, na):
            self.na = na

        _resolve_field = ReportBuilder._resolve_field

    _na = _FakeNA({"View Flag": [("Gamma", "View Flag", "column"),
                                 ("Beta", "View Flag", "column")]})
    _rb = _FakeResolveBuilder(_na)
    _hit_pref = _rb._resolve_field("View Flag", None, candidate_tables=("Beta",))
    ok = _hit_pref == ("Beta", "View Flag", False)
    bad0 += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} candidate_tables prefers the visual's own table over "
          f"global first-match on a cross-table caption collision: {_hit_pref}")

    _hit_noglobal = _rb._resolve_field("View Flag", None, candidate_tables=())
    ok = _hit_noglobal == ("Gamma", "View Flag", False)
    bad0 += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} no candidate_tables falls back to the old global-first-"
          f"match behavior unchanged: {_hit_noglobal}")

    _hit_miss = _rb._resolve_field("View Flag", None, candidate_tables=("Delta",))
    ok = _hit_miss == ("Gamma", "View Flag", False)
    bad0 += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a candidate table that doesn't own the caption falls "
          f"through to the next candidate, then the global match: {_hit_miss}")

    # `_query_state` itself must thread the SAME candidate_tables preference through its own
    # Rows/Cols resolution, not just `_visual_filters`. Real corpus (WorkbookA), confirmed live:
    # captioning Alpha's own `item_id` column "Item Id" (extract.py's own gotcha) widened
    # the caption to 5 tables workbook-wide; "Sheet 5 - Growth Rate A"'s barChart then resolved its
    # Category field to `Beta.Item Id` (global first-match) while its Y measure stayed on
    # `Alpha.Group - YoY Growth` -- two DIFFERENT tables for ONE worksheet's own shelves,
    # every bar rendering with zero height. `_query_state` must instead prefer whichever table a
    # measure on the SAME worksheet already resolved to.
    class _FakeCollisionReportBuilder:
        """Simulates a real caption collision: 'Item Id' resolves to 'Beta' with no table
        hint (global first-match, picking the WRONG table), but to 'Alpha' once that table
        is preferred -- exactly the shape `by_spelling` produces for a caption two tables share.
        'Group - YoY Growth' is a measure, unambiguous, always 'Alpha' regardless of hint.
        """
        def __init__(self):
            self.stats = collections.Counter()
            self.declined_encodings = []
            self._formulas_by_canonical = {}

        def _resolve_field(self, field, agg, candidate_tables=None):
            if field == "Group - YoY Growth":
                return ("Alpha", "Group - YoY Growth", True)
            if field == "Item Id":
                for cand in (candidate_tables or ()):
                    if cand == "Alpha":
                        return ("Alpha", "Item Id", False)
                return ("Beta", "Item Id", False)  # global first-match, no hint
            return None

        _try_rescue_dim_shaped_measure = ReportBuilder._try_rescue_dim_shaped_measure
        _fold_leaf = ReportBuilder._fold_leaf
        _param_current_value = ReportBuilder._param_current_value

    _coll_ws = {"name": "Sheet 5 - Growth Rate A", "encodings": [
        {"channel": "rows", "field": "Item Id", "agg": None},
        {"channel": "cols", "field": "Group - YoY Growth", "agg": "usr"},
    ]}
    _fb_coll = _FakeCollisionReportBuilder()
    _q_coll = ReportBuilder._query_state(_fb_coll, _coll_ws, "barChart")
    _cat_entity = _q_coll["queryState"]["Category"]["projections"][0]["field"]["Column"][
        "Expression"]["SourceRef"]["Entity"]
    ok = _cat_entity == "Alpha"
    bad0 += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state prefers the SAME table as this worksheet's "
          f"own measure for a caption-colliding Category field, instead of global first-match: "
          f"Category entity={_cat_entity!r}")

    # No regression: a worksheet with NO measure to anchor on (nothing in Rows/Cols resolves
    # `is_measure=True`) must fall back to the OLD global-first-match behavior unchanged --
    # `anchor_tables` is empty, so `candidate_tables=()` reproduces today's existing resolution.
    _coll_ws_dimonly = {"name": "w", "encodings": [
        {"channel": "rows", "field": "Item Id", "agg": None},
    ]}
    _q_coll2 = ReportBuilder._query_state(_fb_coll, _coll_ws_dimonly, "tableEx")
    _cat_entity2 = _q_coll2["queryState"]["Values"]["projections"][0]["field"]["Column"][
        "Expression"]["SourceRef"]["Entity"]
    ok = _cat_entity2 == "Beta"
    bad0 += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} with no measure to anchor on, _query_state's global-"
          f"first-match resolution is unchanged (no regression): Category entity={_cat_entity2!r}")

    assert re.fullmatch(r"[0-9a-f]{20}", pbir_name("page", "X")), "name is not 20 lowercase hex"
    assert pbir_name("page", "X") == pbir_name("page", "X"), "name is not stable"
    assert pbir_name("page", "X") != pbir_name("page", "Y")

    def ws(mark, rows, cols, chans=()):
        return {"name": "w", "mark": mark, "title": None,
                "shelves": {"rows": rows, "cols": cols, "pages": []},
                "encodings": [{"channel": c, "field": "f", "agg": None} for c in chans]}

    def ws_text(mark, text_encodings):
        """A worksheet with NOTHING on Rows/Cols, only Text-shelf encodings -- the BAN/text-table
        idiom `_shelf_shape`'s and `_query_state`'s text-channel fallback exist for. Each entry
        is (field, agg)."""
        return {"name": "w", "mark": mark, "title": None,
                "shelves": {"rows": [], "cols": [], "pages": []},
                "encodings": [{"channel": "text", "field": f, "agg": a}
                              for f, a in text_encodings]}

    M = [{"field": "Sales", "agg": "sum"}]
    D = [{"field": "Region", "agg": None}]
    cases = [
        (ws("Pie", M, D), "pieChart", True),
        # Tableau's OWN XML never writes `<mark class='Gantt' />` -- it writes 'GanttBar'.
        # `m == "Gantt"` never matched anything, all 6 real corpus Gantt marks fell to `tableEx`
        # undecided, and this went uncaught because the Synthetic Coverage Probe fixture ALSO
        # assumed 'Gantt' (fixed alongside this) and no case here tested the rule directly either
        # -- three layers all sharing one unverified assumption about what Tableau actually emits.
        (ws("GanttBar", D, M), "ganttChart", True),
        # A `size` encoding on a GanttBar mark is the real-corpus signature of the waterfall
        # technique, not a literal timeline -- see the rule's own comment for the two real
        # "Waterfall - Group"/"Waterfall - Alt" worksheets this was found from.
        (ws("GanttBar", D, M, chans=("size",)), "waterfallChart", True),
        (ws("Square", [], M), "card", True),
        # Square + colour + size is a true treemap (size encodes area, colour the group).
        (ws("Square", D, [], chans=("color", "size")), "treemap", True),
        # Square + colour + NO size is a colour-matrix heatmap: treemap would be blank because
        # its Values (size) well is empty.  Must emit tableEx instead (the non-blank fallback).
        (ws("Square", D, [], chans=("color",)), "tableEx", True),
        (ws("Bar", D, M), "barChart", True),
        (ws("Bar", M, D), "columnChart", True),
        (ws("Text", [], M), "card", True),
        (ws("Automatic", [], M), "card", True),
        # Dimension ONLY on Color (no positional dim), 1 measure on cols -> barChart.
        # Real WorkbookA shape: "Sheet 7 - Item ID A" / "Sheet 7 - Item ID B" (cols: SUM measure,
        # color: item_id dimension, rows empty). Was misclassified as card before this fix.
        (ws("Automatic", [], M, chans=("color",)), "barChart", True),
        # Dimension ONLY on Color, 1 measure on rows -> columnChart.
        # Real WorkbookA shape: "Sheet 6 - Segmentation(A)" / "Sheet 6 - Segmentation(B)".
        (ws("Automatic", M, [], chans=("color",)), "columnChart", True),
        # No color dimension at all -> still card (no regression).
        # Verified: the new rules gate on color_dims >= 1, so this shape is unchanged.
        (ws("Automatic", D, M), "barChart", True),
        (ws("Automatic", M, D), "columnChart", True),
        # The important case: Automatic with an ambiguous shape must NOT be quietly decided.
        (ws("Automatic", D + D, M + M), "tableEx", False),
        (ws("Polygon", D, M), "tableEx", False),
        # A true X/Y scatter: one continuous measure on EACH shelf, no dimension on either.
        (ws("Circle", M, [{"field": "Profit", "agg": "sum"}]), "scatterChart", True),
        # The real corpus shape (see the Circle/Shape rules' own comment): TWO measures on one
        # shelf plus a dimension on the other is a dot-plot comparing measures across a
        # category, not a scatter -- must fall through to undecided, not the old unconditional
        # scatterChart guess that Power BI Desktop refused to render.
        (ws("Circle", M + [{"field": "Profit", "agg": "sum"}], D), "tableEx", False),
        # No encodings at all: still undecided (regression guard -- the new rule must not fire
        # unless there is at least one measure, even for Shape mark).
        (ws("Shape", [], []), "tableEx", False),
    ]
    bad = bad0
    for w, want_vt, want_decided in cases:
        vt, why, decided = choose_visual(w)
        ok = vt == want_vt and decided == want_decided
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {w['mark']:<10} -> {vt:<16} "
              f"decided={decided}  {why}")

    # ---- Shape-mark KPI triangle idiom (8 WorkbookA worksheets: "Sheet 8 - Vol YoY Marks" etc.) ---------
    # mark='Shape', measure on BOTH Color AND Shape channels (Tableau's own dual-channel
    # convention for this idiom), nothing on Rows or Cols -- must decide `card`, not be dropped.
    _shape_kpi_ws = {"name": "w", "mark": "Shape", "title": None,
                     "shelves": {"rows": [], "cols": [], "pages": []},
                     "encodings": [
                         {"channel": "color", "field": "Marks Decline Vol - Rate", "agg": "usr"},
                         {"channel": "shape", "field": "Marks Decline Vol - Rate", "agg": "usr"},
                     ]}
    vt_kpi, why_kpi, dec_kpi = choose_visual(_shape_kpi_ws)
    ok = (vt_kpi == "card" and dec_kpi)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Shape-KPI (measure on color+shape, empty rows/cols) -> "
          f"{vt_kpi!r} decided={dec_kpi}  {why_kpi}")

    # Same measure on the Color channel only (shape channel omitted) -- should still fire.
    _shape_kpi_color_only = {"name": "w", "mark": "Shape", "title": None,
                             "shelves": {"rows": [], "cols": [], "pages": []},
                             "encodings": [
                                 {"channel": "color", "field": "Marks Decline Vol - Rate",
                                  "agg": "usr"},
                             ]}
    vt_kco, _why_kco, dec_kco = choose_visual(_shape_kpi_color_only)
    ok = (vt_kco == "card" and dec_kco)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Shape-KPI (measure on color channel only) -> "
          f"{vt_kco!r} decided={dec_kco}")

    # Regression: the scatterChart rule still fires when Shape mark has BOTH rows AND cols
    # measures (the pre-existing scatterChart path), even though the new card rule exists.
    _shape_scatter = {"name": "w", "mark": "Shape", "title": None,
                      "shelves": {"rows": [{"field": "Profit", "agg": "sum"}],
                                  "cols": [{"field": "Revenue", "agg": "sum"}], "pages": []},
                      "encodings": [{"channel": "rows", "field": "Profit", "agg": "sum"},
                                    {"channel": "cols", "field": "Revenue", "agg": "sum"}]}
    vt_ss, _why_ss, dec_ss = choose_visual(_shape_scatter)
    ok = (vt_ss == "scatterChart" and dec_ss)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Shape + rows+cols both have measure -> {vt_ss!r} "
          f"decided={dec_ss} (scatterChart rule must still win)")

    # Dual-axis bar+line combo -- WorkbookB Dashboard's own "Sheet 1 - Trend" shape: mark 'Automatic'
    # at the worksheet level (the outer/default pane's own mark), 2 measures on Rows, 1
    # dimension on Cols, and 2 identified sub-panes (Bar + Line) each naming its own measure.
    combo_ws = ws("Automatic",
                  [{"field": "Count", "agg": "sum"}, {"field": "Rate", "agg": "avg"}], D)
    combo_ws["panes"] = [{"id": "1", "mark": "Bar", "y_axis_field": "Count"},
                         {"id": "2", "mark": "Line", "y_axis_field": "Rate"}]
    vt, why, decided = choose_visual(combo_ws)
    ok = vt == "lineClusteredColumnComboChart" and decided
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} dual-axis Bar+Line panes -> {vt:<16} "
          f"decided={decided}  {why}")

    assert _dual_axis_combo(combo_ws) == {"Y": "Count", "Y2": "Rate"}
    assert _combo_role_for(combo_ws, "Count") == "Y"
    assert _combo_role_for(combo_ws, "Rate") == "Y2"
    assert _combo_role_for(combo_ws, "SomeOtherField") is None, (
        "a field that matches neither pane's own y-axis field must not be guessed onto an axis")

    # Every other pane shape must decline -- never guessed at.
    assert _dual_axis_combo({"panes": []}) is None, "no identified sub-panes at all"
    assert _dual_axis_combo({"panes": [{"id": "1", "mark": "Bar", "y_axis_field": "Count"}]}
                            ) is None, "only ONE identified sub-pane -- not a combo"
    assert _dual_axis_combo({"panes": [
        {"id": "1", "mark": "Bar", "y_axis_field": "Count"},
        {"id": "2", "mark": "Bar", "y_axis_field": "Count2"}]}) is None, (
        "two Bar panes (a trellis, not a dual-axis combo) must not be mistaken for one")
    assert _dual_axis_combo({"panes": [
        {"id": "1", "mark": "Bar", "y_axis_field": None},
        {"id": "2", "mark": "Line", "y_axis_field": "Rate"}]}) is None, (
        "a pane with no resolvable y-axis field must decline rather than bind the wrong measure")

    # Not enough measures/dimensions for the combo TYPE itself, even with valid panes -- the
    # VISUAL_RULES predicate's own n_measures==2/n_dims==1 guard, not just `_dual_axis_combo`.
    combo_ws_1measure = ws("Automatic", [{"field": "Count", "agg": "sum"}], D)
    combo_ws_1measure["panes"] = combo_ws["panes"]
    vt, why, decided = choose_visual(combo_ws_1measure)
    ok = vt != "lineClusteredColumnComboChart"
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} valid panes but only 1 real measure on the shelf -> "
          f"{vt} (must not fire the combo rule): decided={decided}")

    # Text-shelf fallback -- the bug this session found on a real workbook (WorkbookG
    # ): a worksheet with its only field(s) on Text and NOTHING
    # on Rows/Cols read as "0 dimension(s), 0 measure(s)" before this fix, so no rule ever
    # fired for it even though Tableau's own file plainly shows a field. mark 'Automatic' with
    # one aggregated Text-shelf field, no Rows/Cols at all, must now read as a measures-only,
    # no-dimension shape and decide "card" -- exactly like a Square/Text mark with a measure on
    # Rows/Cols already did before this fix.
    text_card = ws_text("Automatic", [("Sales Growth", "usr")])
    vt, why, decided = choose_visual(text_card)
    ok = vt == "card" and decided
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} text-shelf-only Automatic -> {vt:<16} "
          f"decided={decided}  {why}")

    # A header/label worksheet -- one Text-shelf field, no aggregation, no Rows/Cols -- has a
    # real dimension (1) but no measure, which no Automatic rule claims. It must stay UNDECIDED
    # (tableEx, decided=False), the same "don't guess" behaviour as the pre-existing ambiguous
    # Automatic case a few lines up -- the fallback must not manufacture false confidence.
    text_label = ws_text("Automatic", [("Region", None)])
    vt, why, decided = choose_visual(text_label)
    ok = vt == "tableEx" and not decided
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} text-shelf-only label      -> {vt:<16} "
          f"decided={decided}  {why}")

    # _query_state's own text-shelf fallback: a card visual whose ONLY field is on Text must
    # bind it into the Values well (the actual Desktop-facing bug -- pbip_lint.py's RPT-ROLE-02
    # found 142 visuals on the real workbook binding no field at all, almost all of this shape).
    class _FakeReportBuilder:
        def __init__(self, hits, formulas=None):
            self.stats = collections.Counter()
            self.declined_encodings = []
            self._hits = hits
            # No formula on file for any prop by default -> the dim-shaped-measure rescue's
            # `formula = self._formulas_by_canonical.get(prop)` always misses and it falls
            # straight through to the existing decline, exactly like before that rescue existed.
            self._formulas_by_canonical = formulas or {}

        def _resolve_field(self, field, agg, candidate_tables=None):
            return self._hits.get(field)

        _try_rescue_dim_shaped_measure = ReportBuilder._try_rescue_dim_shaped_measure
        _fold_leaf = ReportBuilder._fold_leaf
        _param_current_value = ReportBuilder._param_current_value

    fb = _FakeReportBuilder({"Sales Growth": ("T", "Sales Growth", True)})
    q = ReportBuilder._query_state(fb, text_card, "card")
    ok = (q is not None
          and q["queryState"]["Values"]["projections"][0]["queryRef"] == "T.Sales Growth")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state binds a lone Text-shelf card measure: {q}")

    # A card's Values well is a ONE-field limit in real Desktop. Tableau's floating-text BAN
    # idiom stacks several text objects (value + arrow glyph + pos/neg token) onto one
    # worksheet; only the FIRST resolving field must be bound, and the rest must be COUNTED,
    # not silently dropped -- the same "never guess, never drop silently" rule this whole
    # project holds DAX translation to, now held for the report layer's own text-shelf fallback.
    multi_text = ws_text("Automatic", [("Value", "usr"), ("Arrow Glyph", "usr")])
    fb2 = _FakeReportBuilder({"Value": ("T", "Value", True),
                              "Arrow Glyph": ("T", "Arrow Glyph", True)})
    q2 = ReportBuilder._query_state(fb2, multi_text, "card")
    ok = (q2 is not None and len(q2["queryState"]["Values"]["projections"]) == 1
          and fb2.stats["text_field_role_capacity_dropped"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state caps a card's Values well at 1 and counts "
          f"the overflow: {q2}  dropped={fb2.stats['text_field_role_capacity_dropped']}")

    # The real bug this session found on 7 corpus worksheets ("Metric A (2)" etc.): a dimension
    # on Rows filtered down to a single value (a label, not a real grouping), with the actual
    # displayed number on the Text shelf. Before this fix, `_shelf_shape` never looked at the
    # Text shelf once Rows/Cols resolved ANYTHING (even a bare dimension), so this shape read as
    # "1 dimension, 0 measures" -- and `_query_state`'s Values well ended up bound to only the
    # filter dimension itself (Desktop would show the filter's own text, e.g. "MQL", never the
    # count it labels). Both halves of that bug are asserted here: the shape must now count the
    # Text-shelf measure, and the binding must include it.
    filtered_label_ws = {"name": "w", "mark": "Automatic", "title": None,
                          "shelves": {"rows": [{"field": "Stage List", "agg": None}],
                                      "cols": [], "pages": []},
                          "encodings": [{"channel": "rows", "field": "Stage List", "agg": None},
                                        {"channel": "text", "field": "RecId", "agg": "ctd"}]}
    vt, why, decided = choose_visual(filtered_label_ws)
    ok = "1 measure(s)" in why
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} filtered-dimension-as-label + Text measure counts as "
          f"1 measure, not 0: {vt} decided={decided}  {why}")

    fb6 = _FakeReportBuilder({"Stage List": ("T", "Stage List", False),
                              "RecId": ("T", "RecId", True)})
    q6 = ReportBuilder._query_state(fb6, filtered_label_ws, "tableEx")
    refs = {p["queryRef"] for p in q6["queryState"]["Values"]["projections"]} if q6 else set()
    ok = refs == {"T.Stage List", "T.RecId"}
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state's Values well binds BOTH the filter "
          f"dimension and the real Text measure, not the dimension alone: {refs}")

    # Shape-KPI idiom: _query_state binds the Color/Shape-channel measure into the card's Values
    # well (the binding counterpart of the `choose_visual` and `_shelf_shape` fix above).
    # `_shape_kpi_ws` was built earlier (in the choose_visual section); `_FakeReportBuilder` is
    # now defined, so the binding tests live here.
    _fb_kpi = _FakeReportBuilder(
        {"Marks Decline Vol - Rate": ("OpsX", "Marks Decline Vol - Rate", True)})
    q_kpi = ReportBuilder._query_state(_fb_kpi, _shape_kpi_ws, "card")
    ok = (q_kpi is not None
          and len(q_kpi["queryState"]["Values"]["projections"]) == 1
          and q_kpi["queryState"]["Values"]["projections"][0]["queryRef"]
          == "OpsX.Marks Decline Vol - Rate")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state binds a Color/Shape-channel KPI measure "
          f"into the card's Values well: {q_kpi}")

    # The same field appears on BOTH color AND shape channels -- the dedup check must prevent
    # a double-binding (one Values projection, not two).
    ok2 = (q_kpi is not None
           and len(q_kpi["queryState"]["Values"]["projections"]) == 1)
    bad += 0 if ok2 else 1
    print(f"  {'OK  ' if ok2 else 'FAIL'} _query_state does NOT double-bind the same field from "
          f"color+shape channels (dedup): {q_kpi['queryState']['Values']['projections'] if q_kpi else None}")

    # Colour-matrix heatmap idiom: a Square mark with REAL dimensions on BOTH Rows and Cols
    # (unlike the Shape-KPI case above, where rows/cols are both empty) plus the sole measure
    # on Colour. `tableEx` has no legend/series well at all, so unlike a real chart's Color
    # shelf, this is unambiguously a Value, not a series split -- confirmed live on WorkbookA's
    # `recency` worksheet (rows=f6_seg_recency, cols=l6_seg_recency, colour=SUM(
    # no_of_items_recency)), whose tableEx previously carried only the two dimension columns
    # with no measure at all.
    _heatmap_ws = {"name": "w", "mark": "Square", "title": None,
                   "shelves": {"rows": [{"field": "RowDim"}], "cols": [{"field": "ColDim"}],
                               "pages": []},
                   "encodings": [
                       {"channel": "rows", "field": "RowDim", "agg": None},
                       {"channel": "cols", "field": "ColDim", "agg": None},
                       {"channel": "color", "field": "Cell Count", "agg": "sum"},
                   ]}
    _fb_heatmap = _FakeReportBuilder({
        "RowDim": ("Delta", "RowDim", False),
        "ColDim": ("Delta", "ColDim", False),
        "Cell Count": ("Delta", "Cell Count", True),
    })
    q_heatmap = ReportBuilder._query_state(_fb_heatmap, _heatmap_ws, "tableEx")
    refs = {p["queryRef"] for p in q_heatmap["queryState"]["Values"]["projections"]} if q_heatmap else None
    ok = refs == {"Delta.RowDim", "Delta.ColDim", "Delta.Cell Count"}
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state binds a Square-mark colour-matrix "
          f"heatmap's Colour measure into tableEx's Values well ALONGSIDE both real Rows/Cols "
          f"dimensions (no legend well to protect, unlike a real chart): {refs}")

    # `_heatmap_gradient_object`'s exact shape, confirmed against a real `pbi-cli
    # format background-gradient`-written visual.json (see this function's own docstring):
    # the Input is a RAW column (never the measure itself) wrapped in a fresh SUM aggregation,
    # and the selector keys off the MEASURE's own queryRef so Desktop applies the rule to the
    # right Values-well column.
    _grad = _heatmap_gradient_object("Delta", "no_of_items_recency",
                                      "Delta.SUM of no_of_items_recency")
    _fill = _grad["properties"]["backColor"]["solid"]["color"]["expr"]["FillRule"]
    ok = (_fill["Input"]["Aggregation"]["Expression"]["Column"]["Property"]
          == "no_of_items_recency"
          and _fill["Input"]["Aggregation"]["Expression"]["Column"]["Expression"]["SourceRef"]
          ["Entity"] == "Delta"
          and _fill["Input"]["Aggregation"]["Function"] == 0
          and _fill["FillRule"]["linearGradient2"]["min"]["color"]["Literal"]["Value"]
          == "'#FFFFFF'"
          and _fill["FillRule"]["linearGradient2"]["max"]["color"]["Literal"]["Value"]
          == "'#118DFF'"
          and _grad["selector"]["metadata"] == "Delta.SUM of no_of_items_recency")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _heatmap_gradient_object keys the gradient's Input off "
          f"the RAW column (not the measure) and its selector off the measure's own queryRef: "
          f"{_grad}")

    # Regression guard: when the rows shelf is non-empty (even if its field declined),
    # `_query_state` must NOT fall back to the color channel -- that fallback is only for the
    # strictly-empty-shelves case (otherwise a color encoding on a chart is a legend field,
    # not the chart's main measure).
    _shape_rows_declined = {"name": "w", "mark": "Automatic", "title": None,
                            "shelves": {"rows": [{"field": "Declined Calc", "agg": "sum"}],
                                        "cols": [], "pages": []},
                            "encodings": [
                                {"channel": "rows", "field": "Declined Calc", "agg": "sum"},
                                {"channel": "color", "field": "Margin", "agg": "sum"},
                            ]}
    _fb_rd = _FakeReportBuilder({"Declined Calc": None,
                                  "Margin": ("T", "Margin", True)})
    q_rd = ReportBuilder._query_state(_fb_rd, _shape_rows_declined, "card")
    ok = (q_rd is None)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state does NOT fall back to color channel when "
          f"rows shelf is non-empty but its field declined (regression guard): q={q_rd}")

    # A pieChart's Category well is a ONE-field limit in real Desktop (ROLE_CAPS, mirroring
    # tools/pbip_rules/checks.py's _ROLE_LIMITS). Two dimensions on a real Tableau worksheet's
    # Rows shelf (ordinary, valid Tableau -- the real corpus case that surfaced this: WorkbookF
    # 's "Region Rpt Status" pie chart, `Region Report ID` + `Rpt Status`
    # both on Rows) must bind only the FIRST resolving dimension into Category, and count the
    # rest as dropped -- not silently discard them, and not emit a 2-field well Desktop refuses.
    pie_two_dims = {"name": "w", "mark": "Pie", "title": None,
                    "shelves": {"rows": [], "cols": [], "pages": []},
                    "encodings": [{"channel": "rows", "field": "Region Report ID", "agg": None},
                                  {"channel": "rows", "field": "Rpt Status", "agg": None},
                                  {"channel": "rows", "field": "Txn Count", "agg": "sum"}]}
    fb3 = _FakeReportBuilder({"Region Report ID": ("T", "Region Report ID", False),
                              "Rpt Status": ("T", "Rpt Status", False),
                              "Txn Count": ("T", "Txn Count", True)})
    q3 = ReportBuilder._query_state(fb3, pie_two_dims, "pieChart")
    ok = (q3 is not None and len(q3["queryState"]["Category"]["projections"]) == 1
          and q3["queryState"]["Category"]["projections"][0]["queryRef"]
          == "T.Region Report ID"
          and len(q3["queryState"]["Y"]["projections"]) == 1
          and fb3.stats["field_role_capacity_dropped"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state caps a pieChart's Category well at 1 and "
          f"counts the overflow: {q3}  dropped={fb3.stats['field_role_capacity_dropped']}")

    # Rows/Cols both empty + dimensions present is a DECIDED text table now, not a fall-through.
    # Same emitted type as the undecided fallback (tableEx), so this asserts the `decided` flag,
    # which is the part that was wrong -- 77 real corpus worksheets were being reported as
    # needing a human when the shelf shape already determines the answer.
    _txt_tbl = {"name": "w", "mark": "Automatic", "title": None,
                "shelves": {"rows": [], "cols": [], "pages": []},
                "encodings": [{"channel": "text", "field": "Region", "agg": None},
                              {"channel": "text", "field": "Segment", "agg": None}]}
    vt_t, _why_t, dec_t = choose_visual(_txt_tbl)
    ok = (vt_t == "multiRowCard" and dec_t)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Automatic + empty Rows/Cols + 2 dims -> {vt_t} "
          f"decided={dec_t} (want multiRowCard/True -- a label BLOCK, not a header+rows table)")

    # The SAME shape with a DIFFERENT mark label (not 'Automatic') must ALSO decide multiRowCard
    # -- real corpus regression (WorkbookJ): this rule was gated on
    # mark == 'Automatic' alone, so any OTHER mark with Rows/Columns both empty fell through
    # every rule to the tableEx/undecided fallback -- confirmed by this repo's own
    # suggest_visual_candidates.py, which computes this identical shape check mark-agnostically
    # and found 68 of 122 real corpus visual_undecided items (56%) match it. 'Polygon' specifically
    # (not 'Bar'/'Pie'/etc): those marks each have their OWN unconditional shape-blind fallback
    # rule earlier in VISUAL_RULES that would claim them first regardless of shelf shape -- this
    # generalization only ever reaches a mark with no earlier rule of its own, exactly like the
    # existing 'Polygon -> tableEx, undecided' case elsewhere in this self-test already uses to
    # represent "no rule recognizes this mark at all".
    _txt_tbl_polygon = {**_txt_tbl, "mark": "Polygon"}
    vt_b, _why_b, dec_b = choose_visual(_txt_tbl_polygon)
    ok = (vt_b == "multiRowCard" and dec_b)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Polygon (not Automatic) + empty Rows/Cols + 2 dims -> "
          f"{vt_b} decided={dec_b} (want multiRowCard/True -- the rule must not be mark-specific)")

    # ONE Text-shelf dimension must stay UNDECIDED -- caption vs list is not decidable from the
    # shape, so neither `card` (loses every value but one) nor `tableEx` can be asserted.
    _one_label = {**_txt_tbl,
                  "encodings": [{"channel": "text", "field": "Region", "agg": None}]}
    vt_1, _why_1, dec_1 = choose_visual(_one_label)
    ok = (not dec_1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Automatic + empty Rows/Cols + ONE dim -> {vt_1} "
          f"decided={dec_1} (want undecided -- caption vs list is ambiguous)")

    # ---- Nav-button string-literal rule ------------------------------------------------
    # The same 1-dim/0-measure shape without calc_lookup MUST remain undecided -- the rule
    # requires formula access and must never fire from shelf shape alone.
    vt_nb0, _why_nb0, dec_nb0 = choose_visual(_one_label)
    ok = (not dec_nb0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} nav-button shape WITHOUT calc_lookup stays undecided -> "
          f"{vt_nb0} decided={dec_nb0} (want undecided -- no formula access)")

    # With calc_lookup showing the single dimension's formula is a bare string literal, the
    # rule MUST fire and route to textbox (decided=True).  Real corpus shape: a WorkbookA nav-tab
    # worksheet like '0.Color_light' whose field 'Color_high' has formula '"Highlights Page"'.
    _nav_calc_lookup = {"Region": '"Highlights Page"'}
    vt_nb1, _why_nb1, dec_nb1 = choose_visual(_one_label, calc_lookup=_nav_calc_lookup)
    ok = (vt_nb1 == "textbox" and dec_nb1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} nav-button: 1 dim with bare string-literal formula -> "
          f"{vt_nb1} decided={dec_nb1} (want textbox/True)")

    # A real column reference (field-reference formula) must NOT fire the rule -- a genuine
    # single-column lookup table stays undecided as before.
    _real_col_lookup = {"Region": "[SomeRealColumn]"}
    vt_nb2, _why_nb2, dec_nb2 = choose_visual(_one_label, calc_lookup=_real_col_lookup)
    ok = (not dec_nb2)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} 1-dim with real field-reference formula stays undecided"
          f" -> {vt_nb2} decided={dec_nb2} (want undecided -- narrow rule, not for real calcs)")

    # Strings with escaped double-quotes inside must also fire (e.g. 'Say ""hello""').
    _escaped_lookup = {"Region": '"Say ""hello"""'}
    vt_nb3, _why_nb3, dec_nb3 = choose_visual(_one_label, calc_lookup=_escaped_lookup)
    ok = (vt_nb3 == "textbox" and dec_nb3)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} nav-button with escaped double-quotes in literal -> "
          f"{vt_nb3} decided={dec_nb3} (want textbox/True)")

    # 2 dims + string-literal formula for one of them: must NOT fire (rule requires exactly 1 dim).
    _two_dims_one_literal = {**_txt_tbl}  # has 2 text dims already
    _two_dim_lookup = {"Region": '"Region"', "Segment": '"Segment"'}
    vt_nb4, _why_nb4, dec_nb4 = choose_visual(_two_dims_one_literal, calc_lookup=_two_dim_lookup)
    ok = (vt_nb4 != "textbox")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} 2 dims with string-literal formulas must NOT fire -> "
          f"{vt_nb4} (want multiRowCard, not textbox -- rule requires exactly 1 dim)")

    # _extract_string_literal: unit-level checks for the bare-literal helper itself.
    _sl_cases = [
        ('"Highlights Page"', "Highlights Page"),
        ('"Delta"', "Delta"),
        ('  "Delta"  ', "Delta"),   # leading/trailing whitespace
        ('""', ""),                        # empty string is a valid literal
        ('"Say ""hello"""', 'Say "hello"'),  # escaped double-quote
        ("[SomeColumn]", None),            # field reference -> None
        ("SUM([col])", None),              # function call -> None
        ('"a" + "b"', None),              # operator expression -> None
        ("", None),                        # empty formula -> None
        ('"no closing', None),             # unterminated -> None
    ]
    _sl_ok = all(_extract_string_literal(f) == exp for f, exp in _sl_cases)
    bad += 0 if _sl_ok else 1
    print(f"  {'OK  ' if _sl_ok else 'FAIL'} _extract_string_literal: bare literals extracted, "
          f"field refs / operators / empty formula -> None")

    # The 0-dimension sibling must still be a card, not swept into the text-table rule above.
    _ban = {**_txt_tbl,
            "encodings": [{"channel": "text", "field": "Sales", "agg": "sum"}]}
    vt_b, _why_b, dec_b = choose_visual(_ban)
    ok = (vt_b == "card" and dec_b)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} Automatic + empty Rows/Cols + measures only -> {vt_b} "
          f"(want card -- the BAN rule still wins)")

    # A real chart with fields on Rows/Cols must NOT be captured by the empty-shelf rule.
    _real_chart = {"name": "w", "mark": "Automatic", "title": None,
                   "shelves": {"rows": [{"field": "Sales", "agg": "sum", "channel": "rows"}],
                               "cols": [{"field": "Region", "agg": None, "channel": "cols"}],
                               "pages": []},
                   "encodings": [{"channel": "cols", "field": "Region", "agg": None},
                                 {"channel": "rows", "field": "Sales", "agg": "sum"}]}
    vt_r, _why_r, _dec_r = choose_visual(_real_chart)
    ok = (vt_r == "columnChart")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a real Rows/Cols chart is untouched by the empty-shelf "
          f"rule -> {vt_r} (want columnChart)")

    # ---- Colour shelf: stacked visual type + the Series/Tooltips wells it implies ----------
    # A DISCRETE field on Colour + a Bar mark is a STACKED bar in Tableau; emitting a flat
    # columnChart drops the segmentation entirely. 45 real corpus worksheets were typed flat
    # before this. A CONTINUOUS (aggregated) colour field is a gradient and must NOT stack.
    _bar_color_dim = {"name": "w", "mark": "Bar", "title": None,
                      "shelves": {"rows": [{"field": "Sales", "agg": "sum", "channel": "rows"}],
                                  "cols": [{"field": "Region", "agg": None, "channel": "cols"}],
                                  "pages": []},
                      "encodings": [{"channel": "cols", "field": "Region", "agg": None},
                                    {"channel": "rows", "field": "Sales", "agg": "sum"},
                                    {"channel": "color", "field": "Segment", "agg": None},
                                    {"channel": "tooltip", "field": "Margin", "agg": "sum"}]}
    vt_s, _why_s, dec_s = choose_visual(_bar_color_dim)
    ok = (vt_s == "columnChart" and dec_s)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} discrete Colour on a Bar mark -> {vt_s} (want "
          f"columnChart)")

    _bar_color_measure = {**_bar_color_dim,
                          "encodings": [e for e in _bar_color_dim["encodings"]
                                        if e["channel"] != "color"]
                          + [{"channel": "color", "field": "Margin", "agg": "sum"}]}
    vt_g, _why_g, _dec_g = choose_visual(_bar_color_measure)
    ok = (vt_g == "columnChart")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} CONTINUOUS Colour on a Bar mark stays flat -> {vt_g} "
          f"(want columnChart -- a gradient does not stack)")

    # Real Power BI Desktop rejected this exact Colour/Series shape on TWO different visual
    # types with two different field kinds (see UNVERIFIED_COLOR_REJECTED's comment: a
    # calculated COLUMN on areaChart, a parameter MEASURE on columnChart/barChart across 8+
    # visuals at once) -- so every EXTRA_WELLS visual type is declined here, not just the one
    # that happened to get opened first. The Colour field must be declined and logged, not
    # bound -- while the Tooltip field (a different well, never implicated by either failure)
    # keeps working, and the visual itself is untouched.
    for _vt in sorted(UNVERIFIED_COLOR_REJECTED):
        fb_dec = _FakeReportBuilder({"Region": ("T", "Region", False),
                                     "Sales": ("T", "Sales", True),
                                     "Segment": ("T", "Segment", False),
                                     "Margin": ("T", "Margin", True)})
        q_dec = ReportBuilder._query_state(fb_dec, _bar_color_dim, _vt, page_key="p1")
        ok = (q_dec is not None
              and "Series" not in q_dec["queryState"]
              and q_dec["queryState"]["Tooltips"]["projections"][0]["queryRef"] == "T.Margin"
              and fb_dec.stats["extra_role_declined_unverified"] == 1
              and fb_dec.stats["extra_role_projections"] == 1
              and len(fb_dec.declined_encodings) == 1
              and fb_dec.declined_encodings[0]["page"] == "p1"
              and fb_dec.declined_encodings[0]["field"] == "Segment")
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {_vt}'s Colour shelf is declined (Desktop-rejected "
              f"shape), Tooltip shelf still binds: declined={fb_dec.declined_encodings}")

    # Color-as-Category fallback: a barChart/columnChart classified because its only grouping
    # dimension lives on Color (not Rows/Cols) must have the color dim routed to the Category
    # well in _query_state, so the chart actually shows per-category bars instead of one
    # grand-total bar. Confirms the REAL shape: measure on rows, color dim on Color, no
    # Rows/Cols dimension (the shape of WorkbookA's "Sheet 6 - Segmentation(A)" etc.).
    _color_cat_ws = {"name": "w", "mark": "Automatic", "title": None,
                     "shelves": {"rows": [{"field": "GroupCount", "agg": "sum"}],
                                 "cols": [], "pages": []},
                     "encodings": [{"channel": "rows", "field": "GroupCount", "agg": "sum"},
                                   {"channel": "color", "field": "Segment", "agg": None}]}
    fb_cc = _FakeReportBuilder({"GroupCount": ("UD", "GroupCount", True),
                                "Segment":     ("UD", "Segment",     False)})
    q_cc = ReportBuilder._query_state(fb_cc, _color_cat_ws, "columnChart")
    ok = (q_cc is not None
          and "Category" in q_cc["queryState"]
          and q_cc["queryState"]["Category"]["projections"][0]["queryRef"] == "UD.Segment"
          and q_cc["queryState"]["Y"]["projections"][0]["queryRef"] == "UD.GroupCount")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} color-as-Category fallback: Color dim goes to "
          f"Category well when Rows/Cols has no dimension: "
          f"{q_cc['queryState'] if q_cc else None}")

    # No regression: a normal columnChart WITH a dimension on Rows/Cols must NOT be affected --
    # the fallback only fires when Category is empty after the Rows/Cols pass.
    _normal_col_ws = {"name": "w", "mark": "Automatic", "title": None,
                      "shelves": {"rows": [{"field": "Sales", "agg": "sum"}],
                                  "cols": [{"field": "Region", "agg": None}], "pages": []},
                      "encodings": [{"channel": "cols", "field": "Region", "agg": None},
                                    {"channel": "rows", "field": "Sales", "agg": "sum"},
                                    {"channel": "color", "field": "Segment", "agg": None}]}
    fb_nc = _FakeReportBuilder({"Sales": ("T", "Sales", True),
                                "Region": ("T", "Region", False),
                                "Segment": ("T", "Segment", False)})
    q_nc = ReportBuilder._query_state(fb_nc, _normal_col_ws, "columnChart")
    ok = (q_nc is not None
          and len(q_nc["queryState"]["Category"]["projections"]) == 1
          and q_nc["queryState"]["Category"]["projections"][0]["queryRef"] == "T.Region")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} color-as-Category fallback does NOT fire when "
          f"Rows/Cols already has a dimension (no regression): "
          f"Category={q_nc['queryState']['Category'] if q_nc else None}")

    # A true scatter's two measures (one on cols, one on rows) must land in DIFFERENT roles --
    # see SCATTER_AXIS_BY_CHANNEL's own comment for why this, not the old shared "Y" well, is
    # what real Power BI Desktop needs.
    _scatter_ws = {"name": "w", "mark": "Circle", "title": None,
                   "shelves": {"rows": [{"field": "Sales", "agg": "sum"}],
                               "cols": [{"field": "Margin", "agg": "sum"}], "pages": []},
                   "encodings": [{"channel": "rows", "field": "Sales", "agg": "sum"},
                                 {"channel": "cols", "field": "Margin", "agg": "sum"}]}
    fb_sc = _FakeReportBuilder({"Sales": ("T", "Sales", True), "Margin": ("T", "Margin", True)})
    q_sc = ReportBuilder._query_state(fb_sc, _scatter_ws, "scatterChart")
    ok = (q_sc is not None
          and q_sc["queryState"]["Y"]["projections"][0]["queryRef"] == "T.Sales"
          and q_sc["queryState"]["X"]["projections"][0]["queryRef"] == "T.Margin")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} scatterChart splits its two measures into X (cols) and "
          f"Y (rows), not one shared well: {q_sc['queryState'] if q_sc else None}")

    # A dual-axis combo's two measures (both on the SAME shelf, Rows) must land in DIFFERENT
    # roles -- Y for the Bar pane's own measure, Y2 for the Line pane's -- matched by field
    # identity via `_combo_role_for`, not by shelf position.
    _combo_ws = {"name": "w", "mark": "Automatic", "title": None,
                 "shelves": {"rows": [{"field": "Count", "agg": "sum"},
                                       {"field": "Rate", "agg": "avg"}],
                             "cols": [{"field": "Month", "agg": None}], "pages": []},
                 "encodings": [{"channel": "rows", "field": "Count", "agg": "sum"},
                               {"channel": "rows", "field": "Rate", "agg": "avg"},
                               {"channel": "cols", "field": "Month", "agg": None}],
                 "panes": [{"id": "1", "mark": "Bar", "y_axis_field": "Count"},
                          {"id": "2", "mark": "Line", "y_axis_field": "Rate"}]}
    fb_combo = _FakeReportBuilder({"Count": ("T", "Count", True), "Rate": ("T", "Rate", True),
                                   "Month": ("T", "Month", False)})
    q_combo = ReportBuilder._query_state(fb_combo, _combo_ws, "lineClusteredColumnComboChart")
    ok = (q_combo is not None
          and q_combo["queryState"]["Y"]["projections"][0]["queryRef"] == "T.Count"
          and q_combo["queryState"]["Y2"]["projections"][0]["queryRef"] == "T.Rate"
          and q_combo["queryState"]["Category"]["projections"][0]["queryRef"] == "T.Month"
          and fb_combo.stats.get("combo_axis_unmatched", 0) == 0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} lineClusteredColumnComboChart splits its two "
          f"same-shelf measures into Y (Bar) and Y2 (Line) by field identity: "
          f"{q_combo['queryState'] if q_combo else None}")

    # A measure that resolves but matches NEITHER pane's own y-axis field (extract.py captured
    # panes for a DIFFERENT pair of fields than what ended up on the shelf -- should not happen
    # given how both are read from the same worksheet, but never guessed at if it ever does)
    # must be declined and counted, not silently dropped.
    _combo_ws_mismatch = {**_combo_ws, "shelves": {**_combo_ws["shelves"],
                          "rows": [{"field": "Count", "agg": "sum"},
                                   {"field": "Unrelated", "agg": "sum"}]},
                          "encodings": [{"channel": "rows", "field": "Count", "agg": "sum"},
                                        {"channel": "rows", "field": "Unrelated", "agg": "sum"},
                                        {"channel": "cols", "field": "Month", "agg": None}]}
    fb_combo2 = _FakeReportBuilder({"Count": ("T", "Count", True),
                                    "Unrelated": ("T", "Unrelated", True),
                                    "Month": ("T", "Month", False)})
    q_combo2 = ReportBuilder._query_state(fb_combo2, _combo_ws_mismatch,
                                          "lineClusteredColumnComboChart")
    ok = (q_combo2 is not None
          and "Y2" not in q_combo2["queryState"]
          and fb_combo2.stats.get("combo_axis_unmatched", 0) == 1
          and len(fb_combo2.declined_encodings) == 1
          and fb_combo2.declined_encodings[0]["field"] == "Unrelated")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a combo measure matching neither pane's y-axis field "
          f"is declined and counted, not silently dropped: "
          f"declined={fb_combo2.declined_encodings} stats={fb_combo2.stats.get('combo_axis_unmatched')}")

    # A parameter-gated calculated field used as a plain (non-aggregated) Rows dimension in
    # Tableau, but that resolves to a DAX MEASURE (real corpus shape: WorkbookB Dashboard's
    # `cl.Chosen Date - Grain - w Dash`, IF-gated on a Parameter, on the Rows shelf of a
    # tableEx) must be DECLINED, not bound -- a measure evaluates once over the whole filter
    # context, so binding it as the grouping key silently collapses a 12-month table to 1 row.
    _dimshaped_measure_ws = {"name": "w", "mark": "Automatic", "title": None,
                              "shelves": {"rows": [{"field": "Selected Grain Sel", "agg": None}],
                                          "cols": [{"field": "Lookup", "agg": "sum"}],
                                          "pages": []},
                              "encodings": [{"channel": "rows", "field": "Selected Grain Sel",
                                             "agg": None},
                                            {"channel": "cols", "field": "Lookup", "agg": "sum"}]}
    fb_dm = _FakeReportBuilder({"Selected Grain Sel": ("T", "cl.Selected Grain Sel", True),
                                "Lookup": ("T", "cl.Lookup", True)})
    q_dm = ReportBuilder._query_state(fb_dm, _dimshaped_measure_ws, "tableEx", page_key="p1")
    ok = (q_dm is not None
          and len(q_dm["queryState"]["Values"]["projections"]) == 1
          and q_dm["queryState"]["Values"]["projections"][0]["queryRef"] == "T.cl.Lookup"
          and fb_dm.stats["dim_shaped_field_resolved_to_measure_declined"] == 1
          and len(fb_dm.declined_encodings) == 1
          and fb_dm.declined_encodings[0]["field"] == "Selected Grain Sel")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a dim-shaped Rows field that resolves to a measure is "
          f"declined, not bound as a collapsing grouping key: {q_dm['queryState'] if q_dm else None}"
          f"  declined={fb_dm.declined_encodings}")

    # Same hazard on a barChart, where dim (Category) and measure (Y) are DIFFERENT wells --
    # real corpus shape: WorkbookB Dashboard's `cl.Selector` on a barChart's Category channel,
    # which the OLD code routed into Y (via is_measure alone) leaving Category empty and the
    # bars unlabeled. Must decline instead of landing in either well.
    _dimshaped_bar_ws = {"name": "w", "mark": "Automatic", "title": None,
                         "shelves": {"cols": [{"field": "Selector", "agg": None}],
                                     "rows": [{"field": "Count", "agg": "sum"}], "pages": []},
                         "encodings": [{"channel": "cols", "field": "Selector", "agg": None},
                                       {"channel": "rows", "field": "Count", "agg": "sum"}]}
    fb_db = _FakeReportBuilder({"Selector": ("T", "cl.Selector", True),
                                "Count": ("T", "cl.Count", True)})
    q_db = ReportBuilder._query_state(fb_db, _dimshaped_bar_ws, "barChart", page_key="p1")
    ok = (q_db is not None
          and "Category" not in q_db["queryState"]
          and q_db["queryState"]["Y"]["projections"][0]["queryRef"] == "T.cl.Count"
          and fb_db.stats["dim_shaped_field_resolved_to_measure_declined"] == 1
          and fb_db.declined_encodings[0]["field"] == "Selector")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a dim-shaped Cols field on a barChart that resolves to a "
          f"measure is declined rather than stranded in Y with an empty Category: "
          f"{q_db['queryState'] if q_db else None}  declined={fb_db.declined_encodings}")

    # `_resolve_field`'s own quick-table-calc-shelf-code guard: even when "Real Field" is a real,
    # resolvable column, an agg of "pcto"/"pcdf"/"diff"/"cum"/"win"/"rank" must still return None --
    # the exact case that would silently start binding the raw column once extract.py's own
    # FIELDREF fix makes the field name resolvable for the first time. A REAL `self.na`-shaped
    # fake (not `_FakeReportBuilder`, which overrides `_resolve_field` entirely and so cannot
    # exercise this guard) is needed here.
    class _FakeNA:
        def by_spelling(self, spelling, table=None, emitted_only=False):
            if spelling == "Real Field":
                return "T", "Real Field", "column"
            return None, None, None

        def lookup(self, table, field, emitted_only=False):
            return None, None

    class _FakeRBForGuard:
        na = _FakeNA()
        param_index = {}

    for _code in sorted(_QUICK_CALC_SHELF_CODES):
        _hit = ReportBuilder._resolve_field(_FakeRBForGuard(), "Real Field", _code)
        ok = _hit is None
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} _resolve_field declines a quick-table-calc shelf "
              f"code ({_code!r}) even though the field it now correctly names resolves to a "
              f"real column: {_hit!r}")
    # A real, unrelated agg value ("usr" -- Tableau's own "the field's formula is already an
    # aggregate" code, nothing to do with a quick-calc pill) must NOT be swept into this guard.
    _usr_hit = ReportBuilder._resolve_field(_FakeRBForGuard(), "Real Field", "usr")
    ok = _usr_hit == ("T", "Real Field", False)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _resolve_field's quick-calc guard leaves an unrelated "
          f"agg value ('usr') alone: {_usr_hit!r}")

    # _strip_wrapping_parens: unit tests for the helper.
    assert _strip_wrapping_parens("([Diff Metric A])") == "Diff Metric A", (
        "outer paren+bracket pair must both be stripped: '([Diff Metric A])' -> "
        "'Diff Metric A'")
    assert _strip_wrapping_parens("([Diff Metric B])") == "Diff Metric B"
    assert _strip_wrapping_parens("(Revenue)") == "Revenue", (
        "paren-only wrap (no inner brackets) still strips the parens")
    assert _strip_wrapping_parens("Build Up By (copy)") == "Build Up By (copy)", (
        "a caption that does not START with '(' must never be altered")
    assert _strip_wrapping_parens("(A] + [B)") == "A] + [B", (
        "outer parens ARE stripped; the inner 'A] + [B' has no leading '[' so no bracket "
        "stripping applies -- the result is a garbage name that simply won't resolve")
    assert _strip_wrapping_parens("([A] + [B])") == "[A] + [B]", (
        "outer parens ARE stripped; the inner '[A] + [B]' has embedded brackets so bracket "
        "stripping is skipped -- the result is a garbage name that won't resolve")
    assert _strip_wrapping_parens("Diff Metric A") == "Diff Metric A", (
        "a plain name with no outer parens is returned unchanged")
    print("  OK   _strip_wrapping_parens: bare literals extracted, mid-caption parens / "
          "complex expressions / plain names -> unchanged")

    # _resolve_field paren-stripping fallback (agg branch): a Tableau-auto-named wrapper
    # caption like "([Diff Metric A])" must fall through to the underlying "Diff Metric A"
    # measure when the wrapper calc was declined (alias exists but not emitted).
    class _FakeNAParen:
        """Simulates: wrapper calc "([Diff Metric A])" was allocated but DECLINED (not emitted);
        underlying "Diff Metric A" measure IS emitted."""
        def by_spelling(self, spelling, table=None, emitted_only=False):
            # Wrapper has an alias but the lookup returns nothing (declined, emitted_only=True)
            if spelling == "([Diff Metric A])":
                if emitted_only:
                    return None, None, None   # declined -- not emitted
                return "T", "([Diff Metric A])", "measure"
            # Underlying measure IS emitted
            if spelling == "Diff Metric A":
                return "T", "Diff Metric A", "measure"
            return None, None, None

        def lookup(self, table, field, emitted_only=False):
            return None, None   # no shelf-aggregate measure exists for usr/Diff Metric A

    class _FakeRBForParen:
        na = _FakeNAParen()
        param_index = {}

    _paren_hit = ReportBuilder._resolve_field(
        _FakeRBForParen(), "([Diff Metric A])", "usr")
    ok = _paren_hit == ("T", "Diff Metric A", True)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _resolve_field strips '([Diff Metric A])' -> "
          f"'Diff Metric A' when the wrapper calc was declined: {_paren_hit!r}")

    # The emitted wrapper caption must still win when the wrapper IS emitted (no regression).
    class _FakeNAParenEmitted:
        """Wrapper "([Revenue])" IS emitted as its own measure; must NOT be stripped away."""
        def by_spelling(self, spelling, table=None, emitted_only=False):
            if spelling == "([Revenue])":
                return "T", "((Revenue))", "measure"   # emitted under sanitized name
            if spelling == "Revenue":
                return "T", "Revenue", "measure"
            return None, None, None

        def lookup(self, table, field, emitted_only=False):
            return None, None

    class _FakeRBParenEmitted:
        na = _FakeNAParenEmitted()
        param_index = {}

    _emitted_paren_hit = ReportBuilder._resolve_field(
        _FakeRBParenEmitted(), "([Revenue])", "usr")
    ok = _emitted_paren_hit == ("T", "((Revenue))", True)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _resolve_field: when wrapper '([Revenue])' IS emitted, "
          f"it wins over the stripped 'Revenue' (no regression): {_emitted_paren_hit!r}")

    # A field on BOTH Rows and Colour (very common in Tableau) must not be double-bound -- one
    # field doing two jobs there is still one field here, and a redundant Series well is noise.
    _dup = {**_bar_color_dim,
            "encodings": [{"channel": "cols", "field": "Region", "agg": None},
                          {"channel": "rows", "field": "Sales", "agg": "sum"},
                          {"channel": "color", "field": "Region", "agg": None}]}
    fb_d = _FakeReportBuilder({"Region": ("T", "Region", False), "Sales": ("T", "Sales", True)})
    qd = ReportBuilder._query_state(fb_d, _dup, "columnChart")
    ok = (qd is not None and "Series" not in qd["queryState"]
          and fb_d.stats["extra_role_projections"] == 0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a field already on Rows/Cols is NOT re-bound to Series: "
          f"roles={sorted(qd['queryState'])}")

    # A visual type with no EXTRA_WELLS entry (card) must be completely untouched by this.
    fb_c = _FakeReportBuilder({"Region": ("T", "Region", False), "Sales": ("T", "Sales", True),
                               "Segment": ("T", "Segment", False), "Margin": ("T", "Margin", True)})
    qc = ReportBuilder._query_state(fb_c, _bar_color_dim, "card")
    ok = (fb_c.stats["extra_role_projections"] == 0
          and (qc is None or "Series" not in qc["queryState"]))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a visual type absent from EXTRA_WELLS gets no extra "
          f"roles: extra_role_projections={fb_c.stats['extra_role_projections']}")

    # The SAME two-dimension shape on a barChart (no cap in ROLE_CAPS) must keep BOTH -- Power
    # BI's Category well for a bar/column/line/area chart is a real multi-level axis, so this
    # fix must not over-generalise the pieChart-specific cap onto visual types that never had it.
    bar_two_dims = {**pie_two_dims, "mark": "Bar"}
    fb4 = _FakeReportBuilder({"Region Report ID": ("T", "Region Report ID", False),
                              "Rpt Status": ("T", "Rpt Status", False),
                              "Txn Count": ("T", "Txn Count", True)})
    q4 = ReportBuilder._query_state(fb4, bar_two_dims, "columnChart")
    ok = (q4 is not None and len(q4["queryState"]["Category"]["projections"]) == 2
          and fb4.stats["field_role_capacity_dropped"] == 0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state leaves an UNCAPPED visual type's multi-"
          f"dimension Category well untouched: {q4}")

    # The EXACT real corpus shape (WorkbookF, "Region Rpt Status" pie chart): the Rows
    # measure is a DECLINED calc (`_resolve_field` returns None for it), so the primary loop
    # binds nothing and falls through to the Text-shelf path -- which must ALSO respect
    # pieChart's Category cap, not just card's. Two plain dimension columns on the Text shelf
    # (`Region Report ID`, `Rpt Status`) must bind only the first into Category.
    pie_declined_measure = {"name": "w", "mark": "Pie", "title": None,
                            "shelves": {"rows": [], "cols": [], "pages": []},
                            "encodings": [{"channel": "rows", "field": "Declined Calc",
                                           "agg": "usr"},
                                          {"channel": "text", "field": "Region Report ID",
                                           "agg": "ctd"},
                                          {"channel": "text", "field": "Rpt Status",
                                           "agg": None}]}
    fb5 = _FakeReportBuilder({"Declined Calc": None,
                              "Region Report ID": ("T", "Region Report ID", False),
                              "Rpt Status": ("T", "Rpt Status", False)})
    q5 = ReportBuilder._query_state(fb5, pie_declined_measure, "pieChart")
    ok = (q5 is not None and len(q5["queryState"]["Category"]["projections"]) == 1
          and q5["queryState"]["Category"]["projections"][0]["queryRef"]
          == "T.Region Report ID"
          and fb5.stats["text_field_role_capacity_dropped"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state's Text-shelf FALLBACK also caps a "
          f"pieChart's Category well (the real WorkbookF bug): {q5}  "
          f"dropped={fb5.stats['text_field_role_capacity_dropped']}")

    # `_visual`'s own RPT-ROLE-02 backstop: a worksheet whose chosen visual type carries real
    # WELLS roles, but whose _query_state comes back empty because every shelf field it
    # references failed to resolve (declined by the formula parser, or -- the real shape found
    # on the WorkbookC corpus workbook -- referenced on a shelf but defined nowhere in the
    # workbook at all), must be DECLINED, not emitted as a typed container with no query. That
    # container would pass every existing structural check yet still render as an unlabeled
    # blank rectangle in Desktop -- the exact symptom tools/pbip_rules/checks.py's RPT-ROLE-02
    # exists to catch, and did catch: 164 such visuals across 5 pages of that real workbook.
    class _FakeVisualBuilder:
        # Real method, not a fake -- it only depends on `self.rel_adj`, which this fixture sets,
        # so re-testing a hand-copied reimplementation here would prove nothing about the real
        # one `_visual` actually calls.
        _all_relationship_reachable = ReportBuilder._all_relationship_reachable
        _CHART_VISUAL_TYPES = ReportBuilder._CHART_VISUAL_TYPES
        _MIN_CHART_HEIGHT = ReportBuilder._MIN_CHART_HEIGHT
        _TIDY_TARGET_GAP = ReportBuilder._TIDY_TARGET_GAP
        _TIDY_BOTTOM_MARGIN = ReportBuilder._TIDY_BOTTOM_MARGIN
        _TIDY_BACKDROP_HEIGHT_FRACTION = ReportBuilder._TIDY_BACKDROP_HEIGHT_FRACTION
        _SLIVER_MAX = ReportBuilder._SLIVER_MAX
        _SLIVER_AREA_RATIO = ReportBuilder._SLIVER_AREA_RATIO
        _SLIVER_MAX_PASSES = ReportBuilder._SLIVER_MAX_PASSES

        def __init__(self, ws_map=None, query_state_result=None, resolve_field_result=None,
                     param_index=None, rel_adj=None, dashboard_names=None, url_actions=None,
                     na_tables=None, calc_lookup=None, action_source_worksheets=None):
            self.gaps = []
            self.stats = collections.Counter()
            self.blend_stats = collections.Counter()
            self.review = []
            self.ws_by_name = ws_map or {}
            self.param_index = param_index or {}
            self._resolve_field_result = resolve_field_result
            self._query_state_result = query_state_result
            self.page_w, self.page_h = 1280, 720
            self.rel_adj = rel_adj if rel_adj is not None else collections.defaultdict(set)
            self._dashboard_names = dashboard_names or set()
            self.url_actions = url_actions or {}
            self._action_source_worksheets = action_source_worksheets or set()
            # `_visual` now reads `self.na.tables` to scope filter/color zone field
            # resolution to the zone's own datasource (see that method's own comment).
            self.na = type("_FakeNA", (), {"tables": na_tables or {}})()
            # nav-button string-literal rule needs this in _visual -> choose_visual
            self._calc_lookup_by_caption = calc_lookup or {}

        def _resolve_field(self, field, agg, candidate_tables=None):
            return self._resolve_field_result

        def _query_state(self, ws, vt, page_key=None):
            return self._query_state_result

        def _register_image(self, param):
            return None

    dangling_ws = ws("Automatic", [], M)  # measures-only, no dimension -> "card" (in WELLS)
    zone_ws = {"kind": "worksheet", "name": "w",
               "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb3 = _FakeVisualBuilder(ws_map={"w": dangling_ws}, query_state_result=None)
    made = ReportBuilder._visual(fb3, "p", zone_ws, 0, 0)
    ok = (made is None and len(fb3.gaps) == 1
          and "nothing to bind" in fb3.gaps[0]["reason"]
          and fb3.stats["worksheet_visuals"] == 0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines a worksheet whose sole shelf field "
          f"never resolves, instead of emitting a blank card: gaps={fb3.gaps}")

    fb4 = _FakeVisualBuilder(ws_map={"w": dangling_ws},
                              query_state_result={"queryState": {"Values": {"projections": [
                                  {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}})
    made2 = ReportBuilder._visual(fb4, "p", zone_ws, 0, 0)
    ok = (made2 is not None and not fb4.gaps and fb4.stats["worksheet_visuals"] == 1
          and fb4.stats["visuals_with_wells"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual keeps a worksheet whose field DOES resolve "
          f"(no regression from the decline check above)")

    # Tableau's own default is the OPPOSITE of Power BI's: a worksheet affects no sibling on its
    # dashboard unless the author wired an explicit Action FROM it (extract.py's
    # `extract_action_source_worksheets`). A worksheet absent from that set must get
    # `drillFilterOtherVisuals: False` -- real WorkbookA bug: the `recency` heatmap had no outgoing
    # Action in Tableau at all, yet Power BI's unconditional default True let selecting one of
    # its cells cross-filter every other Delta card to an empty row set.
    fb_nosrc = _FakeVisualBuilder(
        ws_map={"w": dangling_ws},
        query_state_result={"queryState": {"Values": {"projections": [
            {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}})
    made_nosrc = ReportBuilder._visual(fb_nosrc, "p", zone_ws, 0, 0, dashboard_name="Dash1")
    ok = (made_nosrc is not None
          and made_nosrc[1]["visual"]["drillFilterOtherVisuals"] is False)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual sets drillFilterOtherVisuals=False for a "
          f"worksheet with NO Tableau Action sourced from it: {made_nosrc[1]['visual'].get('drillFilterOtherVisuals')}")

    fb_src = _FakeVisualBuilder(
        ws_map={"w": dangling_ws},
        query_state_result={"queryState": {"Values": {"projections": [
            {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}},
        action_source_worksheets={("Dash1", "w")})
    made_src = ReportBuilder._visual(fb_src, "p", zone_ws, 0, 0, dashboard_name="Dash1")
    ok = (made_src is not None
          and made_src[1]["visual"]["drillFilterOtherVisuals"] is True)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual leaves drillFilterOtherVisuals=True for a "
          f"worksheet that IS a confirmed Tableau Action source (no regression): "
          f"{made_src[1]['visual'].get('drillFilterOtherVisuals')}")

    # A card's own base label font (extract.py's `_label_font`) lands on `objects.value` as a
    # Double-literal fontSize ("8D", not "8pt" -- a different schema location than a textbox
    # run's plain-string textStyle.fontSize) and a quoted-string fontFamily literal.
    font_ws = {**dangling_ws, "label_font": {"size": "8", "family": "Segoe UI Semibold"}}
    fb4e = _FakeVisualBuilder(ws_map={"w": font_ws},
                              query_state_result={"queryState": {"Values": {"projections": [
                                  {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}})
    made4e = ReportBuilder._visual(fb4e, "p", zone_ws, 0, 0)
    ok = (made4e is not None
          and made4e[1]["visual"]["objects"]["value"][0]["properties"]["fontSize"]["expr"]
              ["Literal"]["Value"] == "8D"
          and made4e[1]["visual"]["objects"]["value"][0]["properties"]["fontFamily"]["expr"]
              ["Literal"]["Value"] == "'Segoe UI Semibold'")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a card's base label font lands on objects.value as "
          f"fontSize/fontFamily literals: "
          f"{made4e[1]['visual'].get('objects', {}).get('value') if made4e else None}")

    # No label_font at all (the overwhelming majority of worksheets) must not add an empty/
    # malformed `objects.value` the model-default theme would otherwise apply cleanly.
    made4f = ReportBuilder._visual(fb4, "p", zone_ws, 0, 0)
    ok = made4f is not None and "value" not in made4f[1]["visual"].get("objects", {})
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a card with no label_font gets no objects.value "
          f"(no regression, no fabricated font)")

    # URL dashboard action -- wired onto the SAME worksheet visual the action's own <source>
    # names, not a dedicated button zone (that is the separate "Go to Sheet" case below).
    fb4c = _FakeVisualBuilder(
        ws_map={"w": dangling_ws},
        query_state_result={"queryState": {"Values": {"projections": [
            {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}},
        url_actions={("Dash1", "w"): {"dashboard": "Dash1", "worksheet": "w",
                                      "url": "https://example.com/landing", "caption": "Go"}})
    made4c = ReportBuilder._visual(fb4c, "p", zone_ws, 0, 0, dashboard_name="Dash1")
    ok = (made4c is not None
          and made4c[1]["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
              ["type"]["expr"]["Literal"]["Value"] == "'WebUrl'"
          and made4c[1]["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
              ["webUrl"]["expr"]["Literal"]["Value"] == "'https://example.com/landing'"
          and fb4c.stats["url_actions_wired"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual wires a resolved URL action onto its own "
          f"worksheet's visual as a WebUrl visualLink: "
          f"{made4c[1]['visual'].get('visualContainerObjects') if made4c else None}")

    # No dashboard_name passed at all (every OTHER call site/test in this file) -- must not
    # blow up even though `fb4c`-style fixtures now carry a `url_actions` dict; must not wire
    # anything either, since there is no dashboard context to look one up by.
    made4d = ReportBuilder._visual(fb4c, "p", zone_ws, 0, 0)
    ok = (made4d is not None
          and "visualLink" not in made4d[1]["visual"].get("visualContainerObjects", {}))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual with no dashboard_name never wires a URL "
          f"action (every pre-existing call site in this file omits it) -- no regression")

    # A worksheet with NO matching URL action at all (the overwhelming majority) must not gain
    # a visualLink object it never had.
    fb4e = _FakeVisualBuilder(
        ws_map={"w": dangling_ws},
        query_state_result={"queryState": {"Values": {"projections": [
            {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}},
        url_actions={("Dash1", "some other worksheet"): {"url": "https://example.com/x"}})
    made4e = ReportBuilder._visual(fb4e, "p", zone_ws, 0, 0, dashboard_name="Dash1")
    ok = (made4e is not None
          and "visualLink" not in made4e[1]["visual"].get("visualContainerObjects", {}))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a worksheet with no matching URL action gets no "
          f"visualLink object (no false positive)")

    # show_title: a dashboard zone's own show-title='false' must suppress the title even
    # though the worksheet itself has one -- real corpus shape (WorkbookJ): 994 zones set this.
    titled_ws = {**dangling_ws, "title": "My Custom Title"}
    zone_ws_hidden_title = {**zone_ws, "show_title": False}
    fb4b = _FakeVisualBuilder(ws_map={"w": titled_ws},
                              query_state_result={"queryState": {"Values": {"projections": [
                                  {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}})
    made2b = ReportBuilder._visual(fb4b, "p", zone_ws_hidden_title, 0, 0)
    _t2b = made2b[1]["visual"].get("objects", {}).get("title", [{}])[0] if made2b else {}
    ok = (made2b is not None
          # zone hides the title -> emit title.show=false (not the text) so PBI also hides it
          and _t2b.get("properties", {}).get("show", {}) == {"expr": {"Literal": {"Value": "false"}}}
          and "text" not in _t2b.get("properties", {})
          # A card visual gets its own categoryLabels=False regardless of show_title -- a
          # DIFFERENT object key, not the title this test is actually about (see the
          # categoryLabels-specific test below for that one's own coverage).
          and "categoryLabels" in made2b[1]["visual"].get("objects", {}))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual suppresses a worksheet's title when the "
          f"dashboard zone sets show_title=False (emits title.show=false): "
          f"title={_t2b}")

    zone_ws_shown_title = {**zone_ws, "show_title": True}
    fb4c = _FakeVisualBuilder(ws_map={"w": titled_ws},
                              query_state_result={"queryState": {"Values": {"projections": [
                                  {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}})
    made2c = ReportBuilder._visual(fb4c, "p", zone_ws_shown_title, 0, 0)
    ok = (made2c is not None
          and made2c[1]["visual"]["objects"]["title"][0]["properties"]["text"]["expr"]
              ["Literal"]["Value"] == "'My Custom Title'")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual shows a worksheet's title when the dashboard "
          f"zone leaves show_title True (no regression)")

    # card/multiRowCard's own default caption -- the field's raw internal name printed under
    # every value -- must be turned off, but a chart type that never shows one (columnChart
    # here) must be left alone (no key added where Desktop has nothing to suppress).
    ok = (made2c[1]["visual"]["visualType"] == "card"
          and made2c[1]["visual"]["objects"]["categoryLabels"][0]["properties"]["show"]["expr"]
              ["Literal"]["Value"] == "false")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual turns off a card's default category-label "
          f"caption (the raw internal field name) rather than leaving it to leak through: "
          f"objects={made2c[1]['visual']['objects']}")

    non_card_ws = {**dangling_ws, "mark": "Bar",
                   "shelves": {"rows": [{"field": "Dim", "agg": None}],
                               "cols": [{"field": "Meas", "agg": "sum"}], "pages": []},
                   "encodings": [{"channel": "rows", "field": "Dim", "agg": None},
                                 {"channel": "cols", "field": "Meas", "agg": "sum"}]}
    fb4d = _FakeVisualBuilder(ws_map={"w": non_card_ws},
                              query_state_result={"queryState": {"Category": {"projections": [
                                  {"field": {}, "queryRef": "x", "nativeQueryRef": "x"}]}}})
    made2d = ReportBuilder._visual(fb4d, "p", zone_ws, 0, 0)
    ok = (made2d is not None and made2d[1]["visual"]["visualType"] == "barChart"
          and "categoryLabels" not in made2d[1]["visual"].get("objects", {}))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a non-card chart type gets no categoryLabels object "
          f"(nothing to suppress there): visualType={made2d[1]['visual']['visualType']!r} "
          f"objects={made2d[1]['visual'].get('objects')}")

    # Auto-title suppression: a chart with NO Tableau <title> element must get title.show=false
    # so PBI does not auto-generate "SUM of X, SUM of Y by Z" as a visible visual header.
    # Real corpus (WorkbookA): 300+ visuals without any title were showing raw field-name
    # concatenations in Desktop because no objects.title key was emitted at all.
    # `made2d` (barChart, title=None, show_title absent -> True) is exactly that shape.
    _t2d = made2d[1]["visual"].get("objects", {}).get("title", [{}])[0] if made2d else {}
    ok = (made2d is not None
          and _t2d.get("properties", {}).get("show", {}) == {"expr": {"Literal": {"Value": "false"}}}
          and "text" not in _t2d.get("properties", {}))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} chart with no Tableau title gets title.show=false "
          f"(suppresses PBI auto-generated field-name header): title={_t2d}")

    # textbox zones must NOT get title.show=false -- they use their own content chrome.
    textbox_zone = {**zone_ws, "kind": "text", "name": ""}
    fb4f = _FakeVisualBuilder()
    made2f = ReportBuilder._visual(fb4f, "p", textbox_zone, 0, 0)
    ok = (made2f is not None and made2f[1]["visual"]["visualType"] == "textbox"
          and "title" not in made2f[1]["visual"].get("objects", {}))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} textbox zone does NOT get title.show=false "
          f"(has its own chrome, no data title): objects={made2f[1]['visual'].get('objects') if made2f else None}")

    # Spacer dim skip: _query_state must NOT add Tableau layout spacer dims to PBI field wells.
    # Real corpus (WorkbookA Delta): 'Blank (1)' (formula='""') and 'Blank' (formula='INT(NULL)')
    # are used as visual separators in heatmap crosstabs. Projecting them to Power BI produces
    # null/(Blank) columns the viewer was never meant to see.
    # Build manually: ws() only puts `chans` into encodings; rows/cols go to shelves only.
    # Real worksheets have encodings that mirror their shelf placements -- replicate that.
    spacer_ws = {"name": "w", "mark": "Square", "title": None,
                 "shelves": {"rows": [{"field": "Blank 1", "agg": None},
                                      {"field": "Blank 2", "agg": None},
                                      {"field": "Region", "agg": None}],
                             "cols": [], "pages": []},
                 "encodings": [{"channel": "rows", "field": "Blank 1", "agg": None},
                                {"channel": "rows", "field": "Blank 2", "agg": None},
                                {"channel": "rows", "field": "Region", "agg": None}]}
    # All three fields must resolve via _resolve_field (entity/prop/is_measure),
    # then the spacer check fires on prop against _formulas_by_canonical.
    fb_spacer = _FakeReportBuilder(
        {"Blank 1": ("T", "Blank 1", False),
         "Blank 2": ("T", "Blank 2", False),
         "Region": ("T", "Region", False)},
        formulas={"Blank 1": '""', "Blank 2": "INT(NULL)"})
    q_spacer = ReportBuilder._query_state(fb_spacer, spacer_ws, "tableEx")
    spacer_refs = [p["queryRef"] for p in
                   q_spacer["queryState"].get("Values", {}).get("projections", [])] if q_spacer else []
    ok = (q_spacer is not None
          and "T.Region" in spacer_refs
          and not any("Blank" in r for r in spacer_refs)
          and fb_spacer.stats["spacer_dims_skipped"] == 2)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _query_state skips spacer dims (formula='' or INT(NULL)) "
          f"from PBI field wells: refs={spacer_refs} skipped={fb_spacer.stats['spacer_dims_skipped']}")

    # The other half of the real Desktop bug WorkbookC surfaced:
    # `_visual_filters` above already declines a lone unreachable FILTER, but a worksheet's own
    # field WELLS can equally span two tables with no relationship path (e.g. a lineChart's
    # Category from 'Level 3 - Mapping Data', its Y from 'Page L2 - Data'),
    # and nothing checked THAT before this fix -- the visual was emitted, and Desktop refused the
    # whole page with InvalidUnconstrainedJoin. Two tables, no edge -> the visual must be dropped.
    two_table_q = {"queryState": {
        "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {
            "Entity": "Level 3 - Mapping Data"}}, "Property": "Quarter"}},
            "queryRef": "x", "nativeQueryRef": "x"}]},
        "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {
            "Entity": "Page L2 - Data"}}, "Property": "Sales"}},
            "queryRef": "y", "nativeQueryRef": "y"}]}}}
    fb4b = _FakeVisualBuilder(ws_map={"w": dangling_ws}, query_state_result=two_table_q)
    made2b = ReportBuilder._visual(fb4b, "p", zone_ws, 0, 0)
    ok = (made2b is None and len(fb4b.gaps) == 1
          and "no relationship path" in fb4b.gaps[0]["reason"]
          and fb4b.stats["worksheet_visuals"] == 0
          and any("no relationship path" in k for k in fb4b.blend_stats))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines its OWN two-table field wells with no "
          f"relationship path: gaps={fb4b.gaps}")

    # Same two tables, but NOW with a relationship edge between them (as `tmdl.py`'s blend-key
    # detection would emit for a real shared column) -- the visual must be KEPT, not declined,
    # or every blend relationship this fix adds would be pointless.
    fb4c = _FakeVisualBuilder(
        ws_map={"w": dangling_ws}, query_state_result=two_table_q,
        rel_adj={"Page L2 - Data": {"Level 3 - Mapping Data"},
                 "Level 3 - Mapping Data": {"Page L2 - Data"}})
    made2c = ReportBuilder._visual(fb4c, "p", zone_ws, 0, 0)
    ok = made2c is not None and not fb4c.gaps and fb4c.stats["worksheet_visuals"] == 1
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual keeps its own two-table field wells once a "
          f"relationship connects them (no regression once the blend key exists)")

    # A visual whose OWN wells touch a 'Param '-prefixed table plus one real table must NOT be
    # flagged -- a parameter's own SELECTEDVALUE binding needs no relationship, the same
    # exemption `_visual_filters` already applies at the filter level.
    param_q = {"queryState": {
        "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {
            "Entity": "Page L2 - Data"}}, "Property": "Product"}},
            "queryRef": "x", "nativeQueryRef": "x"}]},
        "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {
            "Entity": "Param Quarter Selection"}}, "Property": "Quarter Selection Selected"}},
            "queryRef": "y", "nativeQueryRef": "y"}]}}}
    fb4d = _FakeVisualBuilder(ws_map={"w": dangling_ws}, query_state_result=param_q)
    made2d = ReportBuilder._visual(fb4d, "p", zone_ws, 0, 0)
    ok = made2d is not None and not fb4d.gaps and fb4d.stats["worksheet_visuals"] == 1
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual leaves a table + Param-table well pair alone "
          f"(no relationship needed for a parameter's own binding)")

    # Same backstop for a filter/color/paramctrl zone (-> slicer): no field at all, or a field
    # that never resolves, must decline rather than emit an empty slicer control.
    zone_filter_no_field = {"kind": "filter", "name": "f1", "field": None,
                            "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb5 = _FakeVisualBuilder(resolve_field_result=None)
    made3 = ReportBuilder._visual(fb5, "p", zone_filter_no_field, 0, 0)
    ok = made3 is None and len(fb5.gaps) == 1 and "nothing to slice" in fb5.gaps[0]["reason"]
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines a filter zone with no field: "
          f"gaps={fb5.gaps}")

    zone_filter_bad_field = {"kind": "filter", "name": "f2", "field": "Ghost Field",
                             "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb6 = _FakeVisualBuilder(resolve_field_result=None)
    made4 = ReportBuilder._visual(fb6, "p", zone_filter_bad_field, 0, 0)
    ok = made4 is None and len(fb6.gaps) == 1
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines a filter zone whose field never "
          f"resolves: gaps={fb6.gaps}")

    zone_filter_ok = {"kind": "filter", "name": "f3", "field": "Real Field",
                      "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb7 = _FakeVisualBuilder(resolve_field_result=("T", "Real Field", False))
    made5 = ReportBuilder._visual(fb7, "p", zone_filter_ok, 0, 0)
    ok = made5 is not None and not fb7.gaps and fb7.stats["control_visuals"] == 1
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual keeps a filter zone whose field resolves "
          f"(no regression)")

    # Dropdown quick-filter styling: only when Tableau's own zone attribute says
    # dropdown/checkdropdown -- a 'list'-style (or unset) quick filter must stay at Power BI's
    # own default, never guessed into a style Tableau never declared.
    zone_filter_dropdown = {"kind": "filter", "name": "f4", "field": "Real Field",
                            "filter_mode": "dropdown",
                            "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb7b = _FakeVisualBuilder(resolve_field_result=("T", "Real Field", False))
    made5b = ReportBuilder._visual(fb7b, "p", zone_filter_dropdown, 0, 0)
    body5b = made5b[1]["visual"] if made5b else None
    ok = (made5b is not None and not fb7b.gaps
          and body5b["objects"]["data"][0]["properties"]["mode"]["expr"]["Literal"]["Value"]
              == "'Dropdown'"
          and body5b["objects"]["general"][0]["properties"]["selfFilterEnabled"]["expr"]
              ["Literal"]["Value"] == "true"
          and body5b["objects"]["selection"][0]["properties"]["selectAllCheckboxEnabled"]
              ["expr"]["Literal"]["Value"] == "true"
          and fb7b.stats["dropdown_filters_styled"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual styles a Tableau dropdown quick filter as a "
          f"Power BI Dropdown slicer with search + select-all: body={body5b}")

    zone_filter_checkdropdown = {**zone_filter_dropdown, "name": "f4b",
                                 "filter_mode": "checkdropdown"}
    fb7c = _FakeVisualBuilder(resolve_field_result=("T", "Real Field", False))
    made5c = ReportBuilder._visual(fb7c, "p", zone_filter_checkdropdown, 0, 0)
    ok = made5c is not None and fb7c.stats["dropdown_filters_styled"] == 1
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual treats 'checkdropdown' the same as 'dropdown'")

    zone_filter_list = {**zone_filter_dropdown, "name": "f4c", "filter_mode": "list"}
    fb7d = _FakeVisualBuilder(resolve_field_result=("T", "Real Field", False))
    made5d = ReportBuilder._visual(fb7d, "p", zone_filter_list, 0, 0)
    ok = (made5d is not None and "objects" not in made5d[1]["visual"]
          and fb7d.stats["dropdown_filters_styled"] == 0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual leaves a non-dropdown quick filter at Power "
          f"BI's own default (no regression, no guessed style)")

    # Core bug fix (WorkbookA): filter/color zone _resolve_field must be scoped to the zone's
    # own datasource via its `param` attribute, not left to the global first-match that
    # previously resolved Alpha's ctr/grp/item_typ filters to Gamma
    # (allocated earlier, same-named columns across 8 datasources).  This verifies that the
    # correct candidate_tables tuple is passed, not just that the field ultimately resolves.
    class _RecordingFakeVB(_FakeVisualBuilder):
        """Extends _FakeVisualBuilder to capture candidate_tables passed to _resolve_field."""
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._last_candidate_tables = "NOT_SET"
        def _resolve_field(self, field, agg, candidate_tables=None):
            self._last_candidate_tables = candidate_tables
            return self._resolve_field_result

    zone_filter_scoped = {
        "kind": "filter", "name": "fscoped", "field": "Ctr",
        "param": "[federated.1f31cgn15bs7al169fjs61tjd77x].[none:ctr:nk]",
        "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05},
    }
    fb_scoped = _RecordingFakeVB(
        na_tables={"federated.1f31cgn15bs7al169fjs61tjd77x": "Alpha"},
        resolve_field_result=("Alpha", "Ctr", False),
    )
    made_scoped = ReportBuilder._visual(fb_scoped, "p", zone_filter_scoped, 0, 0)
    ok = (made_scoped is not None
          and not fb_scoped.gaps
          and fb_scoped._last_candidate_tables == ("Alpha",))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} filter zone _resolve_field scoped to own datasource "
          f"(not first global match): candidate_tables={fb_scoped._last_candidate_tables!r}")

    # Color zone uses the same scoping path (covers Highlights Page' Flag Volume color-kind zones)
    zone_color_scoped = {
        "kind": "color", "name": "cscoped", "field": "Item Id",
        "param": "[federated.05audxa00naz061cabu8o083ux42].[none:item_id:nk]",
        "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05},
    }
    fb_color_scoped = _RecordingFakeVB(
        na_tables={"federated.05audxa00naz061cabu8o083ux42": "Highlights Page"},
        resolve_field_result=("Highlights Page", "Item Id", False),
    )
    made_color_scoped = ReportBuilder._visual(fb_color_scoped, "p", zone_color_scoped, 0, 0)
    ok = (made_color_scoped is not None
          and not fb_color_scoped.gaps
          and fb_color_scoped._last_candidate_tables == ("Highlights Page",))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} color zone _resolve_field also scoped to own "
          f"datasource: candidate_tables={fb_color_scoped._last_candidate_tables!r}")

    # Fallback: no `param` on zone -> candidate_tables=None (global search, existing behaviour)
    zone_filter_no_param = {"kind": "filter", "name": "fnoparam", "field": "Real Field",
                            "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb_no_param = _RecordingFakeVB(na_tables={}, resolve_field_result=("T", "Real Field", False))
    made_noparam = ReportBuilder._visual(fb_no_param, "p", zone_filter_no_param, 0, 0)
    ok = (made_noparam is not None
          and not fb_no_param.gaps
          and fb_no_param._last_candidate_tables is None)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} filter with no `param` falls back to global search "
          f"(candidate_tables=None): candidate_tables={fb_no_param._last_candidate_tables!r}")

    # Rich text runs: bold/color/fontSize/fontFamily survive per-run, alignment is
    # paragraph-level -- real corpus shape (WorkbookJ): a multi-run zone mixing bold and plain text.
    zone_text_rich = {"kind": "text", "name": "t1", "text_align": "center",
                      "text_runs": [
                          {"text": "Bold part ", "bold": True, "italic": False,
                           "color": "#ffffff", "fontsize": "18",
                           "fontname": "Segoe UI Semibold"},
                          {"text": "plain part", "bold": False, "italic": False,
                           "color": None, "fontsize": None, "fontname": None},
                      ],
                      "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb9 = _FakeVisualBuilder()
    made7 = ReportBuilder._visual(fb9, "p", zone_text_rich, 0, 0)
    para = made7[1]["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]
    ok = (made7 is not None
          and para["horizontalTextAlignment"] == "center"
          and para["textRuns"][0]["value"] == "Bold part "
          and para["textRuns"][0]["textStyle"] == {"fontWeight": "bold", "color": "#ffffff",
                                                    "fontSize": "18pt",
                                                    "fontFamily": "Segoe UI Semibold"}
          and para["textRuns"][1]["value"] == "plain part"
          and "textStyle" not in para["textRuns"][1])
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual keeps per-run bold/color/fontSize/fontFamily "
          f"and paragraph-level alignment for a multi-run text zone: paragraph={para}")

    # zone-style background/border applies even with NO text of its own (a purely decorative
    # colored divider) -- real corpus shape (WorkbookJ zone id=16: zone-style only, no
    # <formatted-text> at all).
    zone_text_bg_only = {"kind": "text", "name": "divider", "text_runs": [],
                         "zone_style": {"background_color": "#d71e28", "border_color": None,
                                        "border_width": None},
                         "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb10 = _FakeVisualBuilder()
    made8 = ReportBuilder._visual(fb10, "p", zone_text_bg_only, 0, 0)
    ok = (made8 is not None and "objects" not in made8[1]["visual"]
          and made8[1]["visual"]["visualContainerObjects"]["background"][0]["properties"]
              ["color"]["solid"]["color"]["expr"]["Literal"]["Value"] == "'#d71e28'")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual paints a decorative text zone's background "
          f"even with no text runs of its own")

    zone_paramctrl = {"kind": "paramctrl", "name": "p1", "field": "[Ghost Param]",
                      "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb8 = _FakeVisualBuilder(param_index={})
    made6 = ReportBuilder._visual(fb8, "p", zone_paramctrl, 0, 0)
    ok = made6 is None and len(fb8.gaps) == 1
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines a paramctrl zone whose parameter "
          f"never resolves: gaps={fb8.gaps}")

    zone_paramctrl2 = {"kind": "paramctrl", "name": "p2", "field": "[Parameter 10]",
                       "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb8b = _FakeVisualBuilder(param_index={
        "[Parameter 10]": {"table": "Param pl.Selector", "column": "Value",
                           "measure": "Selector Selected", "tableau": "pl.Selector"}})
    made6b = ReportBuilder._visual(fb8b, "p", zone_paramctrl2, 0, 0)
    ok = (made6b is not None
          and made6b[1]["visual"]["objects"]["header"][0]["properties"]["text"]["expr"]
              ["Literal"]["Value"] == "'Selector'")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual overrides a paramctrl slicer's default "
          f"\"Value\" header with the parameter's own real caption, 'pl.' prefix stripped same "
          f"as every other displayName override: "
          f"{made6b[1]['visual'].get('objects') if made6b else None}")

    zone_paramctrl3 = {"kind": "paramctrl", "name": "p3", "field": "[Parameter 99]",
                       "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb8c = _FakeVisualBuilder(param_index={
        "[Parameter 99]": {"table": "Param Show As", "column": "Value",
                           "measure": "Show As Selected", "tableau": "Show As"}})
    made6c = ReportBuilder._visual(fb8c, "p", zone_paramctrl3, 0, 0)
    ok = (made6c is not None
          and made6c[1]["visual"]["objects"]["header"][0]["properties"]["text"]["expr"]
              ["Literal"]["Value"] == "'Show As'")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a paramctrl slicer header falls back to the "
          f"parameter's own unprefixed caption verbatim when it carries no 'cl.'/'pl.' prefix "
          f"at all (no regression for a normally-named parameter)")

    # A zone's own "Edit Title..." override (extract.py's `custom_title`) must WIN over the
    # parameter's own name entirely -- real corpus shape (WorkbookB Dashboard "Sheet 2 - Summary"):
    # the `pl.Chosen Metric` parameter's own zone is captioned "Chart Metric Type", which
    # reads nothing like "Chosen Metric" (what the parameter-name fallback would show).
    zone_paramctrl4 = {"kind": "paramctrl", "name": "p4", "field": "[Parameter 7]",
                       "custom_title": "Chart Metric Type",
                       "geom": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.05}}
    fb8d = _FakeVisualBuilder(param_index={
        "[Parameter 7]": {"table": "Param pl.Chosen Metric", "column": "Value",
                          "measure": "Chosen Metric Selected", "tableau": "pl.Chosen Metric"}})
    made6d = ReportBuilder._visual(fb8d, "p", zone_paramctrl4, 0, 0)
    ok = (made6d is not None
          and made6d[1]["visual"]["objects"]["header"][0]["properties"]["text"]["expr"]
              ["Literal"]["Value"] == "'Chart Metric Type'")
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} a zone's own custom_title override wins over the "
          f"parameter's own (even prefix-stripped) name entirely: "
          f"{made6d[1]['visual'].get('objects') if made6d else None}")

    # RPT-POS-01, genuinely-authored bleed (NOT the devicelayouts-duplicate-zone bug, which is
    # fixed separately in extract.py's extract_dashboards): a zone in the PRIMARY <zones> tree
    # with a small negative x/y (WorkbookE's logo zone, WorkbookD's section-break image
    # and title-text zone) must clamp its emitted position to 0 without touching width/height,
    # since w/h are independent attributes never derived from a negative starting edge.
    zone_neg_y = {"kind": "bitmap", "name": "logo", "image": "logo.png",
                  "geom": {"x": 0.887, "y": -0.00333, "w": 0.115, "h": 0.079}}
    fb9 = _FakeVisualBuilder()
    fb9._register_image = lambda param: "logo.png"
    made7 = ReportBuilder._visual(fb9, "p", zone_neg_y, 0, 0)
    ok = (made7 is not None and made7[1]["position"]["y"] == 0.0
          and made7[1]["position"]["x"] == round(0.887 * 1280, 2)
          and made7[1]["position"]["width"] == round(0.115 * 1280, 2)
          and made7[1]["position"]["height"] == round(0.079 * 720, 2))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual clamps a negative-y decorative zone's position "
          f"to 0 without touching width/height: {made7[1]['position'] if made7 else None}")

    zone_neg_x = {"kind": "text", "name": "title", "text": "Title",
                  "geom": {"x": -0.00395, "y": 0.1, "w": 0.9, "h": 0.13}}
    fb10 = _FakeVisualBuilder()
    made8 = ReportBuilder._visual(fb10, "p", zone_neg_x, 0, 0)
    ok = (made8 is not None and made8[1]["position"]["x"] == 0.0
          and made8[1]["position"]["y"] == round(0.1 * 720, 2)
          and made8[1]["position"]["width"] == round(0.9 * 1280, 2)
          and made8[1]["position"]["height"] == round(0.13 * 720, 2))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual clamps a negative-x decorative zone's position "
          f"to 0 without touching width/height: {made8[1]['position'] if made8 else None}")

    zone_pos_untouched = {"kind": "text", "name": "label", "text": "Label",
                          "geom": {"x": 0.05, "y": 0.05, "w": 0.2, "h": 0.05}}
    fb11 = _FakeVisualBuilder()
    made9 = ReportBuilder._visual(fb11, "p", zone_pos_untouched, 0, 0)
    ok = (made9 is not None and made9[1]["position"]["x"] == round(0.05 * 1280, 2)
          and made9[1]["position"]["y"] == round(0.05 * 720, 2))
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual leaves a legitimately positive x/y untouched "
          f"(no regression from the clamp): {made9[1]['position'] if made9 else None}")

    # 'empty' is a pure layout spacer (see CHROME_ZONES's own comment: 440 real-corpus zones of
    # this type, zero with any direct child beyond formatting) -- it must drop silently via
    # `chrome_zones_dropped`, the same as 'container'/'layout-flow', NOT surface as a
    # "no mapped Power BI visual" gap needing a human to look at a zone that was never anything
    # to look at.
    zone_empty = {"kind": "empty", "name": "Divider",
                  "geom": {"x": 0.0, "y": 0.1, "w": 0.02, "h": 0.05}}
    fb12 = _FakeVisualBuilder()
    made10 = ReportBuilder._visual(fb12, "p", zone_empty, 0, 0)
    ok = made10 is None and fb12.stats["chrome_zones_dropped"] == 1 and not fb12.gaps
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual drops an 'empty' spacer zone as chrome, not a "
          f"gap: made={made10} chrome_zones_dropped={fb12.stats['chrome_zones_dropped']} "
          f"gaps={fb12.gaps}")

    # Nav-button wiring: a resolved target naming one of this project's own built dashboards
    # gets a real actionButton + PageNavigation visual; an unresolved/foreign target still
    # falls back to the honest decline gap exactly as before.
    zone_nav_resolved = {"kind": "dashboard-object", "name": "NavBtn",
                         "geom": {"x": 0.0, "y": 0.0, "w": 0.05, "h": 0.05},
                         "nav_button_hint": "Page Perf",
                         "nav_button_caption": "Page Perf",
                         "nav_button_target": "Summary Page X"}
    fb13 = _FakeVisualBuilder(dashboard_names={"Summary Page X"})
    made11 = ReportBuilder._visual(fb13, "p", zone_nav_resolved, 0, 0)
    body11 = made11[1]["visual"] if made11 else None
    ok = (made11 is not None and not fb13.gaps
          and body11["visualType"] == "actionButton"
          and body11["visualContainerObjects"]["visualLink"][0]["properties"]["type"]["expr"]
              ["Literal"]["Value"] == "'PageNavigation'"
          and body11["visualContainerObjects"]["visualLink"][0]["properties"]
              ["navigationSection"]["expr"]["Literal"]["Value"]
              == f"'{pbir_name('page', 'Summary Page X')}'"
          and body11["objects"]["text"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
              == "'Page Perf'"
          and fb13.stats["nav_buttons_wired"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual wires a resolved nav-button target to a real "
          f"actionButton + PageNavigation visual: gaps={fb13.gaps} body={body11}")

    zone_nav_unresolved = {**zone_nav_resolved, "nav_button_target": None}
    fb14 = _FakeVisualBuilder(dashboard_names={"Summary Page X"})
    made12 = ReportBuilder._visual(fb14, "p", zone_nav_unresolved, 0, 0)
    ok = (made12 is None and len(fb14.gaps) == 1
          and "could not be resolved" in fb14.gaps[0]["reason"]
          and fb14.stats["nav_buttons_wired"] == 0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines an unresolved nav-button target "
          f"exactly as before: gaps={fb14.gaps}")

    zone_nav_foreign = {**zone_nav_resolved, "nav_button_target": "Some Other Workbook's Page"}
    fb15 = _FakeVisualBuilder(dashboard_names={"Summary Page X"})
    made13 = ReportBuilder._visual(fb15, "p", zone_nav_foreign, 0, 0)
    ok = made13 is None and len(fb15.gaps) == 1 and fb15.stats["nav_buttons_wired"] == 0
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _visual declines a target that resolved to a "
          f"dashboard name NOT built in this project: gaps={fb15.gaps}")

    # `_grow_undersized_charts`: a too-short chart visual grows toward `_MIN_CHART_HEIGHT`, but
    # never past the nearest sibling sitting below it in the SAME horizontal range; a non-chart
    # visual type (no plotting-area need) is left untouched regardless of height; a chart with
    # NO sibling in its way grows all the way to the target.
    def _vis(vtype, x, y, w, h, name):
        return (name, {"position": {"x": x, "y": y, "width": w, "height": h},
                        "visual": {"visualType": vtype}})
    _grow_fixture = [
        _vis("barChart", 0, 100, 50, 20, "a"),    # chart, "b" sits right below -> capped at 30
        _vis("barChart", 0, 130, 50, 20, "b"),    # chart, nearest blocker ("c") is far below -> full 120
        _vis("card", 0, 500, 50, 20, "c"),        # not a chart type -> untouched regardless of height
        _vis("barChart", 200, 100, 50, 20, "d"),  # different x range, no blocker at all -> full 120
    ]
    fb_grow = _FakeVisualBuilder()
    ReportBuilder._grow_undersized_charts(fb_grow, _grow_fixture)
    heights = {n: o["position"]["height"] for n, o in _grow_fixture}
    ok = (heights["a"] == 30.0 and heights["b"] == 120.0 and heights["c"] == 20.0
          and heights["d"] == 120.0 and fb_grow.stats["undersized_chart_grown"] == 3
          and fb_grow.stats["undersized_chart_grown_capped"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _grow_undersized_charts grows a blocked chart only as "
          f"far as its nearest sibling allows, grows an unblocked chart to the full target, and "
          f"never touches a non-chart visual type: {heights}")

    # `_tidy_vertical_gaps`: three bands -- A (y=0-100), a big 200px gap, B (y=300-400), a
    # SMALL already-fine 15px gap, C (y=415-450) -- plus one full-page decorative backdrop
    # (height=500, over `_FakeVisualBuilder`'s page_h=720 * 0.6 = 432 cutoff). Expect: A
    # untouched (first band, nothing above it to compact against); B shifted up by 180 (the
    # 200px gap minus the 20px target); C shifted up by the SAME 180 (its own small 15px gap to
    # B was already fine, so no ADDITIONAL shift -- relative spacing within the B/C pair is
    # preserved); the backdrop untouched entirely; final height = backdrop's own unshifted
    # bottom (500, taller than the compacted content) + the 32px bottom margin.
    def _box(y, h, name):
        return (name, {"position": {"x": 0, "y": y, "width": 100, "height": h}, "visual": {}})
    _tidy_fixture = [
        _box(0, 500, "backdrop"), _box(0, 100, "a"), _box(300, 100, "b"), _box(415, 35, "c"),
    ]
    fb_tidy = _FakeVisualBuilder()
    new_h = ReportBuilder._tidy_vertical_gaps(fb_tidy, _tidy_fixture)
    ys = {n: o["position"]["y"] for n, o in _tidy_fixture}
    ok = (ys["backdrop"] == 0 and ys["a"] == 0 and ys["b"] == 120.0 and ys["c"] == 235.0
          and new_h == 532.0)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _tidy_vertical_gaps shrinks an oversized gap between "
          f"bands, preserves an already-small gap between the next pair, leaves a full-page "
          f"backdrop untouched, and returns a tight recommended page height: ys={ys} "
          f"new_h={new_h}")

    # `_fix_sliver_overlaps`: the exact real-corpus shape confirmed on WorkbookA's Alpha page
    # (zone 26 'Sheet 5 - Growth Rate A' x=[828.77,1258.74] vs zone 81 '2. Item ID'
    # x=[696.26,836.54], both y=[804,1126]) -- a 7.77px sliver overlap in x, full height in y.
    # Must nudge the later box (zone 26, the larger x) forward by exactly the overlap so the
    # two end up edge-to-edge with zero overlap. A big background box fully containing a small
    # one (high overlap-to-smaller-area ratio) must be left untouched -- that is legitimate
    # containment, not an accidental sliver.
    def _box2(x, y, w, h, name):
        return (name, {"position": {"x": x, "y": y, "width": w, "height": h}, "visual": {}})
    _sliver_fixture = [
        _box2(696.26, 804, 140.28, 322, "item_id_list"),
        _box2(828.77, 804, 430.0, 322, "group_yoy_chart"),
        _box2(0, 0, 2220, 1200, "backdrop"),
        _box2(100, 100, 50, 50, "contained_card"),
    ]
    fb_sliver = _FakeVisualBuilder()
    ReportBuilder._fix_sliver_overlaps(fb_sliver, _sliver_fixture)
    xs = {n: o["position"]["x"] for n, o in _sliver_fixture}
    ok = (xs["item_id_list"] == 696.26 and xs["group_yoy_chart"] == 836.54
          and xs["backdrop"] == 0 and xs["contained_card"] == 100
          and fb_sliver.stats["sliver_overlaps_fixed"] == 1)
    bad += 0 if ok else 1
    print(f"  {'OK  ' if ok else 'FAIL'} _fix_sliver_overlaps nudges a confirmed real-corpus "
          f"7.77px sliver overlap apart to zero overlap, and leaves a legitimate large-area "
          f"background containment untouched: xs={xs}")

    raise SystemExit(bad and f"{bad} visual-type case(s) wrong" or
                     print("pbir.py self-test OK") or 0)
