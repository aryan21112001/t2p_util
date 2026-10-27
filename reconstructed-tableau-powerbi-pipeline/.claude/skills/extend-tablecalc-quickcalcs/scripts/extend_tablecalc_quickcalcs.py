#!/usr/bin/env python3
"""
extend_tablecalc_quickcalcs.py -- EXPERIMENTAL, ISOLATED extension of build-tableau-pbip covering
Tableau's built-in "quick table calculation" BUTTONS (Percent of Total, Difference, Running
Total, Moving Average, ...) applied directly to a shelf pill via Tableau's own "Add Table
Calculation" menu -- a structurally DIFFERENT thing from a NAMED calc table-calc
(RANK/RANK_DENSE, already handled by the sibling `extend-tablecalc-rank` skill): a quick calc
button never creates a calculated field or formula at all. It attaches as a
`<column-instance><table-calc type="PctTotal" .../></column-instance>` directly inside a
worksheet's own `<datasource-dependencies>`. Nothing about it existed anywhere in `ir.json`
before this session's own `extract.py` capture (`worksheet_pcto_meta()`), because the main
pipeline's own formula-based table-calc detection (`TABLECALC_RE`) only ever looks at CALC
formula text, and a quick-calc pill has none.

Two jobs in ONE skill, on purpose -- not two separate skills:
  1. IDENTIFY every quick-calc instance in a build, of EVERY type (Percent of Total, Difference,
     Running Total, Moving Average, Rank, ...), with counts -- useful on its own even for a type
     this extension cannot promote yet, so the next template built here starts from real,
     already-captured data instead of a fresh extraction pass.
  2. PROMOTE to a real DAX measure whichever types have a verified-safe template. Today: Percent
     of Total (`type == "PctTotal"`) ONLY. Every other recognized type is reported by name and
     count under "Recognized, not yet templated" -- never silently dropped, never guessed at.
     Future templates (Difference, Running Total, Moving Average, ...) get added to THIS SAME
     skill's `find_candidates()`/`generate_dax()`, not spun off into new sibling skills.

Deliberately a SEPARATE skill from build-tableau-pbip itself -- this extension's own correctness
gets checked against a real corpus workbook, in a build nobody else depends on, before (if ever)
folding any of its rules into the main pipeline. NEVER modifies the input .pbip: it always copies
to a new output directory first, so the original build stays the untouched baseline to diff
against. Mirrors `extend-tablecalc-rank`'s own architecture throughout, including its "never
silently drop, always report a specific reason" convention.

Scope, on purpose -- fail closed on everything outside it, same as the main pipeline:
  - Only `type == "PctTotal"`. Every other recognized quick-calc type is reported separately as
    "not yet templated", never conflated with an actual decline.
  - "Compute Using" (`ordering-type`) safety, derived (not copied from RANK's own rule):
      * `Field` (Specific Dimension(s)) with a resolvable per-usage `ordering-field` -- always
        safe, regardless of what else is on the shelves.
      * `Table` -- a partition-less grand total (`ALLSELECTED()` with no column argument) --
        always safe, no dimension-counting judgement call needed at all.
      * `Rows`/`Columns` (Table Down/Across) -- safe ONLY when the worksheet's Rows+Cols shelves
        together carry exactly ONE real, non-aggregated dimension; then "down" and "across" name
        the same single axis and the direction question is moot. 0 or 2+ dimensions decline.
      * Anything else (`*InPane`, missing) -- always declines. Relative to the visual's own
        pane/cell layout; no stable model-level column to anchor a DAX filter-removal to.
  - Base-value (numerator) aggregation must map to a real DAX aggregation function
    (`tmdl.AGG_DAX`), sourced from the pill's own explicit `aggregation=` attribute first, else
    its shelf ref's own underlying-agg segment. A base value that is itself a computed field
    (Tableau's "User" aggregation, `usr`) is NOT implemented -- declined by name, not guessed at.
  - Partition and base field must each resolve to a real, ALREADY-EMITTED COLUMN (never a
    measure) on the SAME table -- cross-table Percent of Total is not implemented.
  - `DIVIDE(<AGG>(base), CALCULATE(<AGG>(base), ALLSELECTED(partition)))` -- `ALLSELECTED`, not
    RANK's `ALLEXCEPT`: Percent of Total must keep every OTHER active filter intact and lift only
    the ONE addressed dimension's own filter; `ALLEXCEPT` would clear every filter except the
    partition, computing against the wrong (too-large) population whenever another filter is
    active. See SKILL.md for the full derivation.

Usage:
  python3 extend_tablecalc_quickcalcs.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>

`<workbook.twbx>` is accepted for provenance/logging and is not re-parsed -- every fact this
script acts on already lives in `<existing_pbip_dir>/_work/{ir,name_authority}.json`, written by
the SAME extraction the regular build already ran.
"""
import argparse
import collections
import json
import os
import re
import shutil
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "build-tableau-pbip", "scripts"))
sys.path.insert(0, _BUILD_SCRIPTS)

from names import dax_ref                                     # noqa: E402
from tmdl import _measure, fkey, AGG_DAX                       # noqa: E402
from pbir import _fold_default_branch, _PARAM_QUALREF_RE       # noqa: E402

_TOP_LEVEL_COLUMN_OR_PARTITION_RE = re.compile(r"^\t(column|partition)\s", re.MULTILINE)

# `ordering-type` values relative to the visual's own trellis/pane layout -- no stable
# model-level column to anchor a DAX filter-removal to, the same reasoning `tmdl.route()` uses to
# exclude RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE. Measured real values co-occurring with
# `type='PctTotal'` in this corpus (see `extract.py`'s own `worksheet_pcto_meta()` docstring):
# `RowInPane` (14), `ColumnInPane` (6), `CellInPane` (2).
_PANE_RELATIVE_NOTE = ("is relative to the visual's own pane/cell layout -- no stable "
                       "model-level column to anchor a DAX filter-removal to")


# ==================================================================== load existing build state
def load_workbook_state(pbip_dir):
    work = os.path.join(pbip_dir, "_work")
    with open(os.path.join(work, "ir.json"), encoding="utf-8") as fh:
        ir = json.load(fh)
    with open(os.path.join(work, "name_authority.json"), encoding="utf-8") as fh:
        na = json.load(fh)
    return ir, na


def find_semantic_model_dir(pbip_dir):
    for entry in os.listdir(pbip_dir):
        if entry.endswith(".SemanticModel"):
            return entry
    raise FileNotFoundError(f"no *.SemanticModel folder under {pbip_dir}")


def alloc_table_name(na, t):
    """The REAL, allocated PBI table name (and hence .tmdl filename) for this ir.json table --
    same lookup `extend_tablecalc_rank.py`'s own `alloc_table_name` already uses."""
    return na["tables"].get(t.get("tableau_name") or t.get("caption"))


def resolve_via_na_kind(na, alloc_table, field_dict):
    """(real emitted PBI name, "column"|"measure") for this field on `alloc_table`, or
    (None, None). Same two checks as `extend_tablecalc_rank.py`'s own `resolve_via_na` -- an
    allocated-but-never-emitted name resolves to nothing here either -- but ALSO reports which
    kind it is: a Percent-of-Total PARTITION must always be a real COLUMN (a measure cannot be
    `ALLSELECTED`'s own column argument), while the BASE value may be either -- see
    `resolve_pcto_base_agg`'s own docstring for the MEASURE case."""
    tid = fkey(field_dict)
    key = f"{alloc_table} | {tid}"
    if key in na["columns"]:
        name, kind = na["columns"][key], "column"
    elif key in na["measures"]:
        name, kind = na["measures"][key], "measure"
    else:
        return None, None
    if f"{alloc_table} | {name}" not in na.get("emitted", []):
        return None, None
    return name, kind


def _spelling_matches(c, caption):
    """A field is referenced by ITS OWN CAPTION on a shelf (extract.py's own name_map already
    resolves shelf refs that way), but from INSIDE another calc's formula text Tableau just as
    often uses the field's INTERNAL name instead -- and that internal name is not always the
    same string, case included: real corpus example (Prevent Dashboard), the 'Partner Name -
    ID' column's own `tableau_name` is literally `[Partner Name - Id]` (lowercase 'Id'), and
    `cl.Slice By`'s formula references it that exact way. `tmdl.py`'s own `_resolver` already
    indexes a field under BOTH spellings (`c["name"]` and `fkey(c)`) for exactly this reason --
    same fix here, not a new invention."""
    return c["name"] == caption or fkey(c) == caption


def resolve_field(ir, na, caption, required_kind=None):
    """(alloc_table, resolved_name, kind, None) for a field named either by its CAPTION (a
    shelf-encoding, already resolved by extract.py's own name_map) or by its INTERNAL Tableau
    name (a formula's own literal `[Field]` reference -- see `_spelling_matches`), or
    (None, None, None, reason). Same-table only -- 0 or 2+ matches both decline with a specific
    reason, never guessed at."""
    hits = [t for t in ir["tables"]
           if any(_spelling_matches(c, caption) for c in t["columns"] + t["calcs"])]
    if not hits:
        return None, None, None, f"field {caption!r} was not found in any table's own columns/calcs"
    if len(hits) > 1:
        capts = sorted({(t.get("caption") or t.get("tableau_name")) for t in hits})
        return None, None, None, (f"field {caption!r} exists on {len(hits)} different tables "
                                  f"({', '.join(capts)}) -- cross-table resolution is not "
                                  f"implemented")
    t = hits[0]
    alloc_table = alloc_table_name(na, t)
    if alloc_table is None:
        return None, None, None, ("this table's own PBI name could not be resolved from "
                                  "name_authority.json")
    c = next(cc for cc in t["columns"] + t["calcs"] if _spelling_matches(cc, caption))
    name, kind = resolve_via_na_kind(na, alloc_table, c)
    if name is None:
        return None, None, None, f"field {caption!r} did not resolve to a real, already-emitted object"
    if required_kind and kind != required_kind:
        return None, None, None, (f"field {caption!r} resolved to a {kind}, not a "
                                  f"{required_kind} -- not implemented for this shape")
    return alloc_table, name, kind, None


# ==================================================================== addressing / safety rules
def resolve_pcto_partition(tc, dims):
    """(True, partition_field_caption_or_None, None) when this Percent-of-Total's "Compute
    Using" is one of the safe shapes, else (False, None, reason). `dims` is the worksheet's own
    Rows+Cols shelf entries with no aggregation (real dimensions) -- see this module's own
    docstring for the derivation of each branch below."""
    ot = tc.get("ordering_type")
    if ot == "Field":
        of = tc.get("ordering_field")
        if of:
            return True, of, None
        return False, None, ("ordering-type is 'Field' but no resolvable ordering-field for "
                             "this usage")
    if ot == "Table":
        return True, None, None
    if ot in ("Rows", "Columns"):
        if len(dims) == 1:
            return True, dims[0]["field"], None
        if not dims:
            return False, None, ("0 real dimensions on Rows/Cols -- nothing to compute a "
                                 "percentage across")
        names = ", ".join(sorted({d["field"] for d in dims}))
        return False, None, (f"{len(dims)} real dimensions on Rows/Cols ({names}) with "
                             f"ordering-type {ot!r} -- which one is addressed is ambiguous "
                             f"without a confirmed Down/Across-to-shelf mapping")
    if not ot:
        return False, None, "no ordering-type saved for this quick calc"
    return False, None, f"ordering-type {ot!r} {_PANE_RELATIVE_NOTE}"


def resolve_pcto_base_agg(tc):
    """(DAX aggregation function name, None) when the base value's own aggregation maps to a
    real `tmdl.AGG_DAX` entry -- meaning the base resolves to a plain COLUMN this template must
    wrap in that aggregation itself. (`"MEASURE"`, None) when the base value's own aggregation is
    Tableau's 'User' aggregation (`derivation='User'`, shelf ref segment `usr`) -- meaning the
    base field's OWN calculation already produces the fully-aggregated value, so it resolves to a
    real Power BI MEASURE already emitted by the main build, not a column, and needs no
    aggregation wrapper at all: `CALCULATE(<measure>, ALLSELECTED(<partition>))` is valid DAX
    against ANY measure regardless of how complex its own formula is. Real corpus confirmation
    (Prevent Dashboard's "1W - Slice By"): the base pill is `cl.Selected Metric - Count`, a
    Parameter-switched `IF/ELSEIF` cascade over several different SUMs -- already emitted as its
    own DAX measure (`'Insights'[cl.Selected Metric - Count]`, referenced the exact same way by a
    SIBLING measure already in this model, `cl.Selected Metric - Choose Value Type`) -- there is
    nothing harder about this case once the base is recognized as already-a-measure; the earlier
    version of this function declined it outright, assuming 'User' aggregation always meant an
    unimplementable pattern, which real inspection shows is not true. `find_candidates` is what
    actually confirms the base resolves to a measure (via `resolve_field(..., required_kind=...)`)
    -- this function only classifies the AGGREGATION shape, never resolves the field itself.
    (None, reason) for anything else -- checked in order: the pill's own explicit `aggregation=`
    attribute (Tableau's own explicit statement, authoritative when present) -- real corpus
    example: a Running Total pill with `aggregation='Sum'`, no `type='PctTotal'` equivalent yet
    confirmed in this corpus but captured the same way for when one is -- else the shelf ref's own
    underlying-agg segment (real corpus example: Prevent Dashboard's "3B - Days to CE Eligible"
    PctTotal pill has no `aggregation=` attribute at all; `sum` is the ONLY place its base
    aggregation is recorded)."""
    explicit = tc.get("aggregation")
    if explicit:
        fn = AGG_DAX.get(explicit.lower())
        if fn:
            return fn, None
        return None, f"aggregation={explicit!r} does not map to a known DAX aggregation function"
    under = (tc.get("underlying_agg") or "").lower()
    fn = AGG_DAX.get(under)
    if fn:
        return fn, None
    if under == "usr":
        return "MEASURE", None
    return None, (f"base value's own aggregation ({tc.get('underlying_agg')!r}) is not a plain "
                 f"SUM/AVG/COUNT/etc. aggregation and not Tableau's 'User' aggregation either -- "
                 f"not implemented")


def _param_current_value_index(ir):
    """Every parameter's own spelling (caption, internal Tableau name, recovered aliases) -> its
    SAVED current value -- a fact this workbook's own file already states, not a guess. Same
    index pbir.py's `ReportBuilder.__init__` builds for its own dim-shaped-measure rescue."""
    out = {}
    for p in ir.get("parameters", []):
        val = p.get("value")
        spellings = [p["name"], (p.get("tableau_name") or "").strip("[]")]
        spellings += [a.strip("[]") for a in (p.get("aliases") or [])]
        for s in spellings:
            if s:
                out.setdefault(s, val)
                out.setdefault(s.lower(), val)
    return out


def _calc_formula_index(ir):
    """Every calc's own caption -> its Tableau formula text, flat across every table -- the same
    role pbir.py's own `_formulas_by_canonical` plays there, needed here to recurse into a
    partition field that itself resolves to ANOTHER parameter-gated measure."""
    return {c["name"]: c.get("formula") for t in ir["tables"] for c in t.get("calcs", [])}


def rescue_partition_to_column(ir, na, partition_field, param_values=None, calc_formulas=None,
                               visited=None):
    """When a Percent-of-Total's own PARTITION field resolves to a Power BI MEASURE, not a
    column, `ALLSELECTED` has nothing real to take as its argument -- but if that measure's
    ENTIRE formula is a Parameter-gated `IF`/`ELSEIF`/`ELSE` cascade (Tableau's "dynamic
    dimension selector" idiom -- the exact shape pbir.py's own dim-shaped-measure rescue already
    recovers for a Rows/Cols dimension in the main pipeline, reused here via the SAME
    `_fold_default_branch` engine rather than re-implemented), the workbook's own SAVED
    parameter value may make which branch is active a FACT, not a guess.

    Real corpus confirmation (Prevent Dashboard's "1W - Slice By"): the partition field is
    `cl.Slice By`, a Parameter-gated cascade over `pl.Slice By` (saved value `"Partner"`), whose
    active branch is a real column (`Partner Name - ID`).

    Returns (alloc_table, resolved_column_name) on success, else None -- never guesses; a
    cascade this cannot fully evaluate (an undecidable branch, a non-IF-cascade formula, a
    reference cycle) declines exactly like pbir.py's own rescue does.
    """
    param_values = param_values if param_values is not None else _param_current_value_index(ir)
    calc_formulas = calc_formulas if calc_formulas is not None else _calc_formula_index(ir)
    visited = visited or frozenset()

    def current_value_fn(ref_tok):
        if ref_tok.kind == "qualref":
            m = _PARAM_QUALREF_RE.match(ref_tok.val)
            name = m.group(1) if m else None
        else:
            name = ref_tok.val[1:-1]
        if not name:
            return None
        return param_values.get(name) or param_values.get(name.lower())

    def leaf_fn(ref_tok):
        caption = ref_tok.val[1:-1]
        alloc, name, kind, _reason = resolve_field(ir, na, caption)
        if alloc is None:
            return None
        if kind == "column":
            return alloc, name
        if name in visited or len(visited) > 8:
            return None
        return rescue_partition_to_column(ir, na, caption, param_values, calc_formulas,
                                          visited | {name})

    formula = calc_formulas.get(partition_field)
    if formula is None:
        return None
    return _fold_default_branch(formula, current_value_fn, leaf_fn)


# ==================================================================== candidate discovery
def find_candidates(ir, na):
    """Every quick-table-calc instance across the whole build, classified as a PctTotal
    candidate, a PctTotal skip (with a specific reason), or another recognized type not yet
    templated -- never silently dropped, matching `extend_tablecalc_rank.py`'s own convention.

    One instance's `table_calc` metadata can appear on SEVERAL of a worksheet's own encoding
    entries (the shelf placement itself, plus a duplicated Tooltip/Text/Color use of the exact
    same column-instance) -- deduplicated by `(field, agg, table_calc)` so it is reported and
    (if eligible) promoted exactly once per real instance, not once per encoding row.
    """
    out = []
    used_names = set()  # (alloc_table, measure_name) already claimed by an EARLIER candidate in
                        # THIS SAME run -- the same base field with a DIFFERENT partition on a
                        # different worksheet is a real, DISTINCT measure, and naming both from
                        # the base field alone collides (real corpus proof: Prevent Dashboard's
                        # "3B - Days to CE Eligible" and "3C - Reversals - Time from Defl" both
                        # promote a Percent-of-Total over `Lookups`, with different partitions --
                        # `tools/pbip_rules/lint.py` caught this as a load-breaking duplicate
                        # name before this check existed).
    for ws in ir["worksheets"]:
        dims = [e for e in (ws["shelves"].get("rows", []) + ws["shelves"].get("cols", []))
               if not e.get("agg")]
        seen = set()
        for e in ws["encodings"]:
            tc = e.get("table_calc")
            if tc is None:
                continue
            sig = (e["field"], e["agg"], tuple(sorted(tc.items())))
            if sig in seen:
                continue
            seen.add(sig)
            entry = {"worksheet": ws["name"], "field": e["field"], "type": tc.get("type"),
                     "ordering_type": tc.get("ordering_type")}
            if tc.get("type") != "PctTotal":
                entry.update(status="not_yet_templated",
                             reason=f"recognized quick-calc type {tc.get('type')!r} -- no DAX "
                                    f"template implemented in this extension yet")
                out.append(entry)
                continue
            ok, partition_field, reason = resolve_pcto_partition(tc, dims)
            if not ok:
                entry.update(status="skipped", reason=reason)
                out.append(entry)
                continue
            base_fn, reason = resolve_pcto_base_agg(tc)
            if base_fn is None:
                entry.update(status="skipped", reason=reason)
                out.append(entry)
                continue
            # 'MEASURE' (see resolve_pcto_base_agg's own docstring): the base is Tableau's 'User'
            # aggregation, already fully aggregated by its own formula, so it must resolve to a
            # real Power BI MEASURE, not a column -- the ordinary AGG_DAX case still requires a
            # plain column, since THIS code wraps it in the aggregation function itself.
            base_alloc, base_name, _kind, reason = resolve_field(
                ir, na, e["field"], required_kind=("measure" if base_fn == "MEASURE" else "column"))
            if base_alloc is None:
                entry.update(status="skipped", reason=reason)
                out.append(entry)
                continue
            part_name = None
            if partition_field is not None:
                part_alloc, part_name, part_kind, reason = resolve_field(ir, na, partition_field)
                if part_alloc is None:
                    entry.update(status="skipped", reason=reason)
                    out.append(entry)
                    continue
                if part_kind == "measure":
                    # See rescue_partition_to_column's own docstring -- a partition field that
                    # is ITSELF a parameter-gated dynamic-dimension measure (real corpus shape:
                    # Prevent Dashboard's "1W - Slice By", partitioned by `cl.Slice By`) may still
                    # fold down to a real column via the workbook's own saved parameter value.
                    rescued = rescue_partition_to_column(ir, na, partition_field)
                    if rescued is None:
                        entry.update(status="skipped", reason=(
                            f"partition field {partition_field!r} resolves to a MEASURE "
                            f"(parameter-gated dynamic dimension), and its own formula could "
                            f"not be folded down to a real column using the workbook's saved "
                            f"parameter value(s) -- ALLSELECTED needs a real column, not a "
                            f"measure, to filter on"))
                        out.append(entry)
                        continue
                    part_alloc, part_name = rescued
                if part_alloc != base_alloc:
                    entry.update(status="skipped",
                                 reason=(f"partition field {partition_field!r} lives on a "
                                        f"different table than base field {e['field']!r} -- "
                                        f"cross-table Percent of Total is not implemented"))
                    out.append(entry)
                    continue
            # The partition (or "grand total") is part of what makes this measure semantically
            # DISTINCT from another Percent-of-Total over the same base field -- it must be part
            # of the name, not just the base field alone (see `used_names`'s own comment above).
            measure_name = (f"cl.{e['field']} - % of Total by {part_name}" if part_name
                            else f"cl.{e['field']} - % of Total")
            name_key = (base_alloc, measure_name)
            if f"{base_alloc} | {measure_name}" in na.get("emitted", []) or name_key in used_names:
                entry.update(status="skipped",
                             reason=f"a measure named {measure_name!r} already exists on this "
                                    f"table (either in the existing model, or claimed by another "
                                    f"candidate earlier in this same run)")
                out.append(entry)
                continue
            used_names.add(name_key)
            entry.update(status="candidate", alloc_table=base_alloc, base_field=base_name,
                        base_agg=base_fn, partition_field=part_name, measure_name=measure_name)
            out.append(entry)
    return out


# ==================================================================== DAX generation + patch
def generate_dax(cand):
    """`DIVIDE(<AGG>(base), CALCULATE(<AGG>(base), ALLSELECTED(partition)))` -- or, when the
    addressing is `Table` (a partition-less grand total), `ALLSELECTED()` with no column
    argument at all. See this module's own docstring for why `ALLSELECTED`, never `ALL` or
    RANK's own `ALLEXCEPT`.

    When `base_agg == "MEASURE"` (see `resolve_pcto_base_agg`'s own docstring), `base_field` is
    already a fully-aggregated Power BI MEASURE, not a column -- referenced bare, with NO
    aggregation-function wrapper, since wrapping an already-scalar measure in `SUM()`/`AVG()`
    would be a type error (and meaningless even where DAX tolerated it): `dax_ref` produces the
    exact same `'Table'[Name]` syntax this model's own build already uses to reference this same
    measure elsewhere (confirmed: `cl.Selected Metric - Choose Value Type`'s own formula
    references `'Insights'[cl.Selected Metric - Count]` the identical way)."""
    base_ref = dax_ref(cand["alloc_table"], cand["base_field"])
    agg = cand["base_agg"]
    base_expr = base_ref if agg == "MEASURE" else f"{agg}({base_ref})"
    if cand["partition_field"] is None:
        denom = f"CALCULATE({base_expr}, ALLSELECTED())"
    else:
        part_ref = dax_ref(cand["alloc_table"], cand["partition_field"])
        denom = f"CALCULATE({base_expr}, ALLSELECTED({part_ref}))"
    return f"DIVIDE({base_expr}, {denom})"


def promote(pbip_dir, out_dir, candidates):
    if os.path.exists(out_dir):
        raise FileExistsError(f"{out_dir} already exists -- refusing to overwrite; remove it "
                              f"or pick a different --out")
    shutil.copytree(pbip_dir, out_dir)
    model_dir = find_semantic_model_dir(out_dir)
    results = []
    for cand in candidates:
        if cand["status"] != "candidate":
            results.append(cand)
            continue
        expr = generate_dax(cand)
        tmdl_path = os.path.join(out_dir, model_dir, "definition", "tables",
                                 f"{cand['alloc_table']}.tmdl")
        measure_block = "\n".join(
            _measure(cand["alloc_table"], cand["measure_name"], expr, format_string="0.0%"))
        with open(tmdl_path, encoding="utf-8") as fh:
            text = fh.read()
        m = _TOP_LEVEL_COLUMN_OR_PARTITION_RE.search(text)
        insert_at = m.start() if m else len(text)
        new_text = text[:insert_at] + measure_block + "\n" + text[insert_at:]
        with open(tmdl_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        cand = {**cand, "status": "promoted", "dax": expr, "tmdl_file": tmdl_path}
        results.append(cand)
    return results


# ==================================================================== report
def render_report(workbook_ref, pbip_dir, out_dir, results):
    promoted = [r for r in results if r["status"] == "promoted"]
    skipped = [r for r in results if r["status"] == "skipped"]
    not_yet = [r for r in results if r["status"] == "not_yet_templated"]
    lines = [
        "# Table-Calc Quick-Calculations Extension Report "
        "(EXPERIMENTAL, isolated from the main pipeline)", "",
        f"Source workbook: `{workbook_ref}`", f"Existing build: `{pbip_dir}`",
        f"New, patched build: `{out_dir}`", "",
        f"{len(promoted)} Percent-of-Total quick calc(s) promoted from unbound to a real DAX "
        f"measure. {len(skipped)} Percent-of-Total instance(s) checked and left alone, each for "
        f"a specific, reported reason. {len(not_yet)} recognized quick-calc instance(s) of OTHER "
        f"types found -- not yet templated by this extension, never silently dropped.", "",
    ]
    if promoted:
        lines.append("## Promoted")
        lines.append("")
        for r in promoted:
            lines.append(f"### `{r['measure_name']}` (worksheet `{r['worksheet']}`, "
                         f"table `{r['alloc_table']}`)")
            lines.append("")
            lines.append(f"**Base field:** `{r['base_field']}` ({r['base_agg']})")
            lines.append("")
            lines.append(f"**Partition field:** "
                         f"`{r['partition_field'] or '(none -- grand total over the table)'}`")
            lines.append("")
            lines.append(f"**New DAX measure:**\n```\n{r['dax']}\n```")
            lines.append("")
            lines.append(f"**Written to:** `{r['tmdl_file']}`")
            lines.append("")
    if not_yet:
        lines.append("## Recognized, not yet templated (by type)")
        lines.append("")
        counts = collections.Counter(r["type"] for r in not_yet)
        for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{t}`: {n} instance(s) -- no DAX template implemented in this "
                        f"extension yet")
        lines.append("")
    if skipped:
        lines.append("## Checked, not promoted")
        lines.append("")
        for r in skipped:
            lines.append(f"- `{r['field']}` (worksheet `{r['worksheet']}`): {r['reason']}")
        lines.append("")
    lines.append("## Before trusting this build")
    lines.append("")
    lines.append(
        "- [ ] Open the new build in Power BI Desktop; confirm it loads with no errors.")
    lines.append(
        "- [ ] For each promoted measure, drag it onto a table/matrix visual alongside the SAME "
        "dimension(s) the source Tableau worksheet had on Rows/Cols, matching the same filter "
        "context as closely as possible, and spot-check its value row-by-row against Tableau's "
        "own rendered percentage.")
    lines.append(
        "- [ ] Re-check with a page/report filter active, specifically: `ALLSELECTED` is "
        "genuinely context-dependent and does not reproduce Tableau's before/after-table-calc "
        "filter distinction byte-for-byte in every edge case -- confirm the percentage still "
        "matches Tableau's FILTERED view, not the unfiltered total.")
    lines.append(
        "- [ ] Run `tools/pbip_rules/lint.py <new_pbip_dir> --schemas` for a structural check.")
    lines.append("")
    return "\n".join(lines)


# ==================================================================== CLI
def main():
    parser = argparse.ArgumentParser(
        description="EXPERIMENTAL: identify every Tableau quick table-calculation button in a "
                    "build, and promote Percent-of-Total instances with a resolved, safe "
                    "'Compute Using' addressing into a real DAX measure, in a separate copy of "
                    "an existing build-tableau-pbip project.")
    parser.add_argument("twbx", help="original .twbx/.twb (provenance only, not re-parsed)")
    parser.add_argument("pbip_dir", help="existing build-tableau-pbip output folder "
                                         "(must contain _work/ir.json etc.)")
    parser.add_argument("--out", required=True, help="new output folder (must not exist)")
    args = parser.parse_args()

    ir, na = load_workbook_state(args.pbip_dir)
    candidates = find_candidates(ir, na)
    results = promote(args.pbip_dir, args.out, candidates)

    report = render_report(args.twbx, args.pbip_dir, args.out, results)
    report_path = os.path.join(args.out, "TABLECALC_QUICKCALCS_EXTENSION_REPORT.md")
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)

    n_promoted = sum(1 for r in results if r["status"] == "promoted")
    n_skipped = sum(1 for r in results if r["status"] == "skipped")
    n_not_yet = sum(1 for r in results if r["status"] == "not_yet_templated")
    print(f"[ok] promoted={n_promoted} skipped={n_skipped} not_yet_templated={n_not_yet} "
         f"-> {args.out}")
    print(f"     report: {report_path}")
    sys.exit(0)


# ==================================================================== self-test
def _selftest():
    # resolve_pcto_partition: every branch.
    ok, field, reason = resolve_pcto_partition({"ordering_type": "Field", "ordering_field": "Region"}, [])
    assert (ok, field, reason) == (True, "Region", None), (ok, field, reason)

    ok, field, reason = resolve_pcto_partition({"ordering_type": "Field", "ordering_field": None}, [])
    assert ok is False and "no resolvable ordering-field" in reason, (ok, field, reason)

    ok, field, reason = resolve_pcto_partition({"ordering_type": "Table"}, [])
    assert (ok, field, reason) == (True, None, None), (ok, field, reason)

    one_dim = [{"field": "Region", "agg": None}]
    ok, field, reason = resolve_pcto_partition({"ordering_type": "Rows"}, one_dim)
    assert (ok, field, reason) == (True, "Region", None), (ok, field, reason)
    ok, field, reason = resolve_pcto_partition({"ordering_type": "Columns"}, one_dim)
    assert (ok, field, reason) == (True, "Region", None), (ok, field, reason)

    ok, field, reason = resolve_pcto_partition({"ordering_type": "Rows"}, [])
    assert ok is False and "0 real dimensions" in reason, (ok, field, reason)

    two_dims = [{"field": "Region", "agg": None}, {"field": "Product", "agg": None}]
    ok, field, reason = resolve_pcto_partition({"ordering_type": "Rows"}, two_dims)
    assert ok is False and "2 real dimensions" in reason, (ok, field, reason)

    ok, field, reason = resolve_pcto_partition({"ordering_type": "RowInPane"}, one_dim)
    assert ok is False and "pane/cell layout" in reason, (ok, field, reason)

    ok, field, reason = resolve_pcto_partition({"ordering_type": None}, one_dim)
    assert ok is False and "no ordering-type saved" in reason, (ok, field, reason)
    print("OK  resolve_pcto_partition: Field, Table, Rows/Columns-1-dim, 0-dim decline, "
         "2-dim decline, Pane-relative decline, missing-ordering-type decline")

    # resolve_pcto_base_agg: explicit aggregation= wins, underlying_agg fallback works, a
    # computed-field ('usr') base value classifies as 'MEASURE' (find_candidates is what then
    # decides promotable-vs-declined, based on whether that field actually resolves to a real
    # measure), and an unmapped/unrecognized aggregation still declines outright.
    fn, reason = resolve_pcto_base_agg({"aggregation": "Sum", "underlying_agg": "usr"})
    assert (fn, reason) == ("SUM", None), (fn, reason)
    fn, reason = resolve_pcto_base_agg({"aggregation": None, "underlying_agg": "sum"})
    assert (fn, reason) == ("SUM", None), (fn, reason)
    fn, reason = resolve_pcto_base_agg({"aggregation": None, "underlying_agg": "usr"})
    assert (fn, reason) == ("MEASURE", None), (fn, reason)
    fn, reason = resolve_pcto_base_agg({"aggregation": None, "underlying_agg": "nk"})
    assert fn is None and "not implemented" in reason, (fn, reason)
    print("OK  resolve_pcto_base_agg: explicit aggregation= wins, underlying_agg fallback, "
         "'usr' classifies as MEASURE (not an outright decline), unmapped aggregation declines")

    # find_candidates: end-to-end against a small synthetic ir/na fixture covering a promotable
    # PctTotal-over-Field instance, a PctTotal-over-Rows-with-2-dims decline, a PctTotal-over-
    # 'usr' decline, and a non-PctTotal quick-calc reported separately.
    ir = {"tables": [
        {"tableau_name": "ds1", "caption": "T1", "columns": [
            {"name": "Sales", "tableau_name": "[sales]"},
            {"name": "Region", "tableau_name": "[region]"},
            {"name": "Product", "tableau_name": "[product]"},
        ], "calcs": []},
    ], "worksheets": [
        {"name": "W1", "shelves": {"rows": [{"field": "Sales", "agg": "pcto"}], "cols": []},
        "encodings": [{"field": "Sales", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Field", "ordering_field": "Region",
            "aggregation": None, "underlying_agg": "sum"}}]},
        {"name": "W2", "shelves": {
            "rows": [{"field": "Region", "agg": None}, {"field": "Product", "agg": None}],
            "cols": [{"field": "Sales", "agg": "pcto"}]},
        "encodings": [{"field": "Sales", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Columns", "ordering_field": None,
            "aggregation": None, "underlying_agg": "sum"}}]},
        {"name": "W3", "shelves": {"rows": [{"field": "Sales", "agg": "pcto"}], "cols": []},
        "encodings": [{"field": "Sales", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Field", "ordering_field": "Region",
            "aggregation": None, "underlying_agg": "usr"}}]},
        {"name": "W4", "shelves": {"rows": [{"field": "Sales", "agg": "pcdf"}], "cols": []},
        "encodings": [{"field": "Sales", "agg": "pcdf", "table_calc": {
            "type": "PctDiff", "ordering_type": "Rows", "ordering_field": None,
            "aggregation": None, "underlying_agg": "sum"}}]},
        # Real corpus shape (Prevent Dashboard "1W - Slice By"): base value is 'usr' aggregation
        # AND resolves to a real, already-emitted MEASURE (not just declined by name -- promoted).
        {"name": "W5", "shelves": {"rows": [{"field": "Count", "agg": "pcto"}], "cols": []},
        "encodings": [{"field": "Count", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Field", "ordering_field": "Region",
            "aggregation": None, "underlying_agg": "usr"}}]},
    ]}
    na = {"tables": {"ds1": "T1"},
         "columns": {"T1 | sales": "Sales", "T1 | region": "Region", "T1 | product": "Product"},
         "measures": {"T1 | count": "Count"},
         "emitted": ["T1 | Sales", "T1 | Region", "T1 | Product", "T1 | Count"]}
    ir["tables"][0]["calcs"] = [{"name": "Count", "tableau_name": "[count]"}]
    cands = find_candidates(ir, na)
    by_ws = {c["worksheet"]: c for c in cands}
    assert by_ws["W1"]["status"] == "candidate", by_ws["W1"]
    assert by_ws["W1"]["partition_field"] == "Region", by_ws["W1"]
    assert by_ws["W1"]["base_agg"] == "SUM", by_ws["W1"]
    assert by_ws["W2"]["status"] == "skipped" and "2 real dimensions" in by_ws["W2"]["reason"], by_ws["W2"]
    assert (by_ws["W3"]["status"] == "skipped"
           and "resolved to a column, not a measure" in by_ws["W3"]["reason"]), by_ws["W3"]
    assert by_ws["W4"]["status"] == "not_yet_templated" and by_ws["W4"]["type"] == "PctDiff", by_ws["W4"]
    assert by_ws["W5"]["status"] == "candidate", by_ws["W5"]
    assert by_ws["W5"]["base_agg"] == "MEASURE", by_ws["W5"]
    assert by_ws["W5"]["base_field"] == "Count", by_ws["W5"]
    print("OK  find_candidates: one promotable Field-addressed column instance, one "
         "2-dimension Rows/Columns decline, one 'usr'-base-resolves-to-a-column decline, one "
         "non-PctTotal quick-calc reported separately as not-yet-templated, one promotable "
         "'usr'-base-resolves-to-a-MEASURE instance")

    # Real corpus bug this fixes: the SAME base field, Percent-of-Total'd with a DIFFERENT
    # partition on two different worksheets, must get two DISTINCT measure names -- naming from
    # the base field alone collides (caught by `tools/pbip_rules/lint.py` as a load-breaking
    # duplicate before this check existed).
    ir_collide = {"tables": ir["tables"], "worksheets": [
        {"name": "WA", "shelves": {"rows": [{"field": "Sales", "agg": "pcto"}], "cols": []},
        "encodings": [{"field": "Sales", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Field", "ordering_field": "Region",
            "aggregation": None, "underlying_agg": "sum"}}]},
        {"name": "WB", "shelves": {"rows": [{"field": "Sales", "agg": "pcto"}], "cols": []},
        "encodings": [{"field": "Sales", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Field", "ordering_field": "Product",
            "aggregation": None, "underlying_agg": "sum"}}]},
    ]}
    cands_collide = find_candidates(ir_collide, na)
    names = {c["worksheet"]: c["measure_name"] for c in cands_collide}
    assert names["WA"] != names["WB"], names
    assert all(c["status"] == "candidate" for c in cands_collide), cands_collide
    print(f"OK  find_candidates gives two DIFFERENT measure names ({names['WA']!r} vs "
         f"{names['WB']!r}) to the same base field Percent-of-Total'd with different "
         f"partitions, instead of colliding")

    # generate_dax: exact DAX string, both the Field-addressed case and the Table (partition-
    # less grand total) case.
    field_cand = {**by_ws["W1"], "alloc_table": "T1", "base_field": "Sales",
                 "base_agg": "SUM", "partition_field": "Region"}
    expr = generate_dax(field_cand)
    assert expr == "DIVIDE(SUM('T1'[Sales]), CALCULATE(SUM('T1'[Sales]), ALLSELECTED('T1'[Region])))", expr
    print(f"OK  generate_dax (Field-addressed): {expr}")

    table_cand = {**field_cand, "partition_field": None}
    expr2 = generate_dax(table_cand)
    assert expr2 == "DIVIDE(SUM('T1'[Sales]), CALCULATE(SUM('T1'[Sales]), ALLSELECTED()))", expr2
    print(f"OK  generate_dax (Table -- partition-less grand total): {expr2}")

    # generate_dax: the MEASURE case -- base_field is already a fully-aggregated measure, so it
    # must be referenced BARE, never wrapped in an aggregation function.
    measure_cand = {**field_cand, "base_field": "Count", "base_agg": "MEASURE"}
    expr3 = generate_dax(measure_cand)
    assert expr3 == ("DIVIDE('T1'[Count], CALCULATE('T1'[Count], "
                     "ALLSELECTED('T1'[Region])))"), expr3
    print(f"OK  generate_dax (MEASURE base -- no aggregation wrapper): {expr3}")

    # rescue_partition_to_column -- real corpus shape (Prevent Dashboard "1W - Slice By"): the
    # partition field itself is a parameter-gated dynamic-dimension MEASURE, whose active branch
    # folds down to a real column via the workbook's own SAVED parameter value.
    ir_rescue = {"tables": [
        {"tableau_name": "ds1", "caption": "T1",
         "columns": [{"name": "Partner Name - ID", "tableau_name": "[partner]"},
                    {"name": "Client Name", "tableau_name": "[client]"}],
         "calcs": [{"name": "cl.Slice By", "tableau_name": "[calc_sliceby]",
                   "formula": 'IF [Parameters].[pl.Slice By] = "Partner" THEN '
                              '[Partner Name - ID] ELSEIF [Parameters].[pl.Slice By] = '
                              '"Client" THEN [Client Name] END'}]},
    ], "parameters": [{"name": "pl.Slice By", "tableau_name": "[pl.Slice By]",
                       "value": "Partner", "aliases": []}]}
    na_rescue = {"tables": {"ds1": "T1"},
                "columns": {"T1 | partner": "Partner Name - ID", "T1 | client": "Client Name"},
                "measures": {"T1 | calc_sliceby": "cl.Slice By"},
                "emitted": ["T1 | Partner Name - ID", "T1 | Client Name", "T1 | cl.Slice By"]}
    rescued = rescue_partition_to_column(ir_rescue, na_rescue, "cl.Slice By")
    assert rescued == ("T1", "Partner Name - ID"), rescued
    print(f"OK  rescue_partition_to_column: a parameter-gated dynamic-dimension partition "
         f"folds down to the workbook's own SAVED-parameter-value active branch (a real "
         f"column), matching pbir.py's own dim-shaped-measure rescue: {rescued}")

    # A branch this fold cannot decide (the SAVED value matches neither ELSEIF, and there is no
    # ELSE) must decline, never guess a branch.
    ir_rescue2 = {**ir_rescue, "parameters": [{"name": "pl.Slice By",
                                              "tableau_name": "[pl.Slice By]",
                                              "value": "Something Else", "aliases": []}]}
    rescued2 = rescue_partition_to_column(ir_rescue2, na_rescue, "cl.Slice By")
    assert rescued2 is None, rescued2
    print("OK  rescue_partition_to_column declines when the saved parameter value matches no "
         "branch and there is no ELSE (Tableau's own NULL result, never guessed at)")

    # find_candidates: end-to-end -- the partition-is-a-rescuable-measure shape must promote,
    # not just resolve_partition/base in isolation.
    ir_w6 = {**ir_rescue, "tables": [{**ir_rescue["tables"][0],
             "calcs": ir_rescue["tables"][0]["calcs"] + [
                 {"name": "Count", "tableau_name": "[count]"}]}]}
    ir_w6["worksheets"] = [
        {"name": "W6", "shelves": {"rows": [{"field": "cl.Slice By", "agg": None}], "cols": []},
        "encodings": [{"field": "Count", "agg": "pcto", "table_calc": {
            "type": "PctTotal", "ordering_type": "Columns", "ordering_field": None,
            "aggregation": None, "underlying_agg": "usr"}}]}]
    na_w6 = {**na_rescue, "measures": {**na_rescue["measures"], "T1 | count": "Count"},
            "emitted": na_rescue["emitted"] + ["T1 | Count"]}
    cands_w6 = find_candidates(ir_w6, na_w6)
    assert len(cands_w6) == 1 and cands_w6[0]["status"] == "candidate", cands_w6
    assert cands_w6[0]["partition_field"] == "Partner Name - ID", cands_w6[0]
    print(f"OK  find_candidates promotes a PctTotal whose partition is a rescuable "
         f"parameter-gated measure, resolved to its real active-branch column: {cands_w6[0]}")

    # A field that resolves ambiguously across 2 tables, or not at all, must decline with a
    # specific reason -- never guess which table "wins".
    ir_ambig = {"tables": [
        {"tableau_name": "dsA", "caption": "TA", "columns": [{"name": "Sales", "tableau_name": "[s]"}], "calcs": []},
        {"tableau_name": "dsB", "caption": "TB", "columns": [{"name": "Sales", "tableau_name": "[s]"}], "calcs": []},
    ]}
    alloc, name, kind, reason = resolve_field(ir_ambig, {"tables": {}}, "Sales")
    assert alloc is None and "2 different tables" in reason, (alloc, name, kind, reason)
    alloc, name, kind, reason = resolve_field({"tables": []}, {"tables": {}}, "Nonexistent")
    assert alloc is None and "was not found" in reason, (alloc, name, kind, reason)
    print("OK  resolve_field declines (does not guess) a field ambiguous across 2 tables, or "
         "absent entirely")

    # Real corpus bug this fixes (Prevent Dashboard, 'cl.Slice By' -> 'Partner Name - ID'): a
    # formula's own literal `[Field]` reference can match a column's INTERNAL `tableau_name`
    # spelling, not its resolved CAPTION -- resolve_field must try both, exactly like tmdl.py's
    # own `_resolver` already does, never declining a field that genuinely does resolve just
    # because the caller asked for it by the "wrong" (but real) spelling.
    ir_intname = {"tables": [
        {"tableau_name": "ds1", "caption": "T1", "columns": [
            {"name": "Partner Name - ID", "tableau_name": "[Partner Name - Id]"}], "calcs": []},
    ]}
    na_intname = {"tables": {"ds1": "T1"},
                 "columns": {"T1 | Partner Name - Id": "Partner Name - ID"}, "measures": {},
                 "emitted": ["T1 | Partner Name - ID"]}
    alloc, name, kind, reason = resolve_field(ir_intname, na_intname, "Partner Name - Id")
    assert (alloc, name, kind) == ("T1", "Partner Name - ID", "column"), (alloc, name, kind, reason)
    alloc2, name2, kind2, reason2 = resolve_field(ir_intname, na_intname, "Partner Name - ID")
    assert (alloc2, name2, kind2) == ("T1", "Partner Name - ID", "column"), (
        alloc2, name2, kind2, reason2)
    print("OK  resolve_field matches a field by its INTERNAL tableau_name spelling as well as "
         "its resolved caption -- a formula-literal case mismatch ('Partner Name - Id' vs the "
         "real caption 'Partner Name - ID') resolves correctly instead of declining")

    print("extend_tablecalc_quickcalcs.py self-test OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        sys.exit(0)
    if len(sys.argv) == 1:
        print("usage: extend_tablecalc_quickcalcs.py <workbook.twbx> <existing_pbip_dir> "
             "--out <new_pbip_dir>")
        sys.exit(2)
    main()
