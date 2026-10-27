#!/usr/bin/env python3
"""
extend_dax_measures.py -- EXPERIMENTAL, ISOLATED extension of build-tableau-pbip's own table-calc
rule (tmdl.py's `route()`), covering three more calc shapes whose addressing IS recoverable from
the workbook, in addition to the RANK/RANK_DENSE shape `extend-tablecalc-rank` already covers:

  1. LOOKUP(<expr>, 0)               -- a same-row passthrough. Offset 0 needs no addressing at
                                         all (there is no "previous row" involved), so this is the
                                         zero-risk case: it always resolves to exactly one measure.
  2. INDEX()                          -- row position along the resolved addressing field.
  3. WINDOW_SUM(<expr>, -N, 0)         -- a fixed-size (literal N) trailing moving sum/average,
     WINDOW_SUM(a,-N,0)/WINDOW_SUM(b,-N,0)  including the "moving average RATE" ratio-of-two shape.
  4. WINDOW_SUM(<expr>, <start>, <end>) -- the SAME shape as (3), but with a DYNAMIC window: one
     or both bounds reference a Tableau PARAMETER (optionally negated) instead of a literal
     integer, e.g. `WINDOW_SUM(expr, -[Parameters].[Window Size], [Parameters].[Window End])`.
     Resolves the parameter(s) to their own already-promoted `SELECTEDVALUE` measure
     (`build_param_resolver()`) and builds the window via a rank-difference template
     (`ADDCOLUMNS`+`RANKX`+`FILTER`) instead of (3)'s simpler `TOPN` -- a materially newer, less
     proven pattern, flagged extra-clearly per-measure in the report.

     NOTE on the real corpus this was built against: shape (4) is real and self-tested (see
     `_selftest()`), but on `WorkbookB Dashboard` it promotes NOTHING new -- its 4 real
     parameter-driven `WINDOW_SUM` residue items (`Rolling Lookups`,
     `User Specified Moving Average/Rolling ...`) all ALSO carry `tc_spec.ordering_type=="Rows"`
     with `has_address=False`, i.e. Tableau recorded no named addressing field for them at all --
     the same dead end shape (3)'s own siblings hit. The literal-vs-parameter distinction was
     never the actual blocker for THIS workbook; shape (4) exists for a workbook/calc where a
     named addressing field IS resolvable but the window size itself is still parameter-driven.

The genuinely NEW capability this skill adds (vs. `extend-tablecalc-rank`'s own resolver): when
worksheets DISAGREE on the addressing field, this skill does not decline the whole calc -- it
generates ONE DAX measure PER DISTINCT addressing variant found (`resolve_addressing_variants()`
below), so a calc used on 4 pages with 2 different "Compute Using" settings gets 2 measures, one
per setting, each reported with the worksheets that should use it.

Deliberately MODEL-ONLY. This skill never reads or writes anything under a `*.Report` folder --
no `page.json`, no `visual.json`. Deciding which page's visual should bind to which addressing
variant is a separate, later, human-reviewed step, out of scope here on purpose.

Deliberately a SEPARATE skill from `extend-tablecalc-rank`/`extend-tablecalc-quickcalcs`, same
isolation reasoning those two already give: this extension's own correctness gets checked against
a real corpus workbook, in a build nobody else depends on, before (if ever) folding any of it into
the main pipeline. NEVER modifies the input .pbip: it always copies to a new output directory
first, so the original build stays the untouched baseline to diff against.

Reuses build-tableau-pbip's own dax.translate()/names.py/tmdl.py helpers as a LIBRARY (imported,
never re-implemented) for the pieces that already exist there (aggregate-expression translation,
TMDL measure emission); the window/rank DAX *templates* themselves are new and kept local to this
script, not added to dax.py -- same convention the two sibling skills already follow.

Scope, on purpose -- fail closed on everything outside it:
  - Only the 4 shapes above. Anything recognizably close but outside them is DEFERRED with a
    specific reason (a future template on this SAME skill, never spun into a new one):
      * LOOKUP(..., <non-zero offset>) or any LOOKUP/WINDOW_SUM nested inside a larger expression
        (e.g. a "% Change" IF/IFNULL formula that builds a whole sentence, not a plain number) --
        needs true row-lag DAX (OFFSET) or a bespoke text template, a different template family.
        Checked against the real corpus: the two "% Change" calcs there ALSO have no resolvable
        addressing at all (Tableau's default Table/Pane addressing, no named field) -- so even
        this template would not help them; not attempted for that reason too.
      * Any other WINDOW_*/RUNNING_* function.
  - Only promotes to a MEASURE. A shape whose relevant expression is not itself an aggregate
    (`dax.is_aggregate`) would resolve to a calculated COLUMN, which is skipped and reported, not
    guessed at -- the column-emission path is not implemented here (same guard
    `extend-tablecalc-rank` already uses). `INDEX()` is the one deliberate exception: it has no
    inner expression to check at all, and its DAX shape (`RANKX(...)`) is unambiguously a MEASURE
    regardless.
  - An addressing field must be a real, ALREADY-EMITTED column on the calc's OWN table (checked
    against `name_authority.json`'s own `emitted` list, same ground truth `extend-tablecalc-rank`
    uses).
  - The rolling-window template ASSUMES the addressed column's own ascending/descending sort
    matches Tableau's row order for that field (true for real dates and for consistently-
    formatted month strings), and assumes the model's grain at the resolved scope is one row per
    distinct addressing value. Both are stated per-promoted-measure in the report, not silently
    assumed away.

Usage:
  python3 extend_dax_measures.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>

`<workbook.twbx>` is not re-parsed for any promotion DECISION -- every fact those act on already
lives in `<existing_pbip_dir>/_work/{ir,name_authority,residue}.json`, written by the SAME
extraction the regular build already ran. It IS read once, best-effort (`read_workbook_xml()`),
purely so the report can annotate each DECLINED calc as MANDATORY (actually used on a worksheet,
or read by another calc's formula) vs OPTIONAL (referenced nowhere -- a dead field, safe to
ignore, and usually the very reason no addressing was ever recorded for it) -- `classify_usage()`.
A read failure here never aborts the run, it only omits that annotation.
"""
import argparse
import json
import os
import re
import shutil
import sys
import zipfile

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "build-tableau-pbip", "scripts"))
sys.path.insert(0, _BUILD_SCRIPTS)

import dax                                                     # noqa: E402
from names import dax_ref                                     # noqa: E402
from tmdl import _measure, fkey                                # noqa: E402

_TOP_CALL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", re.S)
_INDEX_RE = re.compile(r"^\s*INDEX\s*\(\s*\)\s*$", re.I)
_WORKSHEET_BLOCK_RE = re.compile(r"<worksheet name=(['\"])(.*?)\1>(.*?)</worksheet>", re.S)
_INT_RE = re.compile(r"^\s*-?\d+\s*$")
_LOOKUP_ANYWHERE_RE = re.compile(r"\bLOOKUP\s*\(", re.I)
_WINDOW_SUM_ANYWHERE_RE = re.compile(r"\bWINDOW_SUM\s*\(", re.I)
_OTHER_WINDOW_FUNC_RE = re.compile(
    r"\b(RUNNING_SUM|RUNNING_AVG|WINDOW_AVG|WINDOW_MAX|WINDOW_MIN|WINDOW_MEDIAN|WINDOW_COUNT|"
    r"WINDOW_STDEV|WINDOW_VAR|WINDOW_PERCENTILE)\s*\(", re.I)
_TOP_LEVEL_COLUMN_OR_PARTITION_RE = re.compile(r"^\t(column|partition)\s", re.MULTILINE)


# ==================================================================== load existing build state
def load_workbook_state(pbip_dir):
    work = os.path.join(pbip_dir, "_work")
    with open(os.path.join(work, "ir.json"), encoding="utf-8") as fh:
        ir = json.load(fh)
    with open(os.path.join(work, "name_authority.json"), encoding="utf-8") as fh:
        na = json.load(fh)
    with open(os.path.join(work, "residue.json"), encoding="utf-8") as fh:
        residue = json.load(fh)
    return ir, na, residue


def find_semantic_model_dir(pbip_dir):
    for entry in os.listdir(pbip_dir):
        if entry.endswith(".SemanticModel"):
            return entry
    raise FileNotFoundError(f"no *.SemanticModel folder under {pbip_dir}")


# ==================================================================== usage classification
def read_workbook_xml(workbook_path):
    """The raw workbook XML text, whether given a `.twbx` (a ZIP wrapping the `.twb`) or a bare
    `.twb`. Used ONLY for REPORTING which declined calcs are live vs. dead -- never for
    addressing resolution itself, so a read failure here must never abort a run, only skip the
    usage annotation (see `render_report`'s own try/except around this).

    Falls back to a plain-text read on `BadZipFile` even for a `.twbx`-named path -- confirmed
    real-corpus case: some workbooks in this reference corpus store the unpacked `.twb` XML
    directly under a `.twbx` name (byte-identical to the sibling `.twb`), not an actual ZIP."""
    if workbook_path.lower().endswith(".twbx"):
        try:
            with zipfile.ZipFile(workbook_path) as zf:
                twb_name = next(n for n in zf.namelist() if n.lower().endswith(".twb"))
                return zf.read(twb_name).decode("utf-8", errors="replace")
        except zipfile.BadZipFile:
            pass
    with open(workbook_path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def find_worksheet_usage(workbook_xml, tableau_name):
    """Every worksheet whose own XML block contains this calc's internal Tableau identifier --
    the same substring check a human would do reading the XML directly. This is deliberately NOT
    how addressing is resolved anywhere else in this script (that only trusts extract.py's own
    structured `tc_spec` capture) -- it exists purely to tell a REPORT READER whether a declined
    calc is actually rendered anywhere, or is dead weight sitting in the model unused."""
    ident = (tableau_name or "").strip("[]")
    if not ident or not workbook_xml:
        return []
    return sorted({m.group(2) for m in _WORKSHEET_BLOCK_RE.finditer(workbook_xml)
                   if ident in m.group(3)})


def referenced_by_other_calc(ir, tableau_name):
    """Every OTHER calc (by name) whose own formula text contains this calc's internal Tableau
    identifier -- a calc can be "used" as a building block for another calc without ever sitting
    on a worksheet itself."""
    ident = (tableau_name or "").strip("[]")
    if not ident:
        return []
    hits = []
    for t in ir["tables"]:
        for c in t.get("calcs") or []:
            if (c.get("tableau_name") or "").strip("[]") == ident:
                continue
            if ident in (c.get("formula") or ""):
                hits.append(c["name"])
    return sorted(hits)


def classify_usage(workbook_xml, ir, calc):
    """(is_used: bool, description: str) for one calc -- MANDATORY (used on >=1 worksheet, or
    referenced by another calc's formula) vs OPTIONAL (neither -- a dead field sitting in the
    model, safe to leave declined; also explains why no addressing was ever recorded for it:
    Tableau only saves a per-usage "Compute Using" setting for a calc that is actually placed
    somewhere)."""
    worksheets = find_worksheet_usage(workbook_xml, calc.get("tableau_name"))
    other_calcs = referenced_by_other_calc(ir, calc.get("tableau_name"))
    if not worksheets and not other_calcs:
        return False, ("OPTIONAL -- not referenced on any worksheet and not used by any other "
                        "calc; a dead field, safe to leave declined")
    bits = []
    if worksheets:
        bits.append(f"used on {len(worksheets)} worksheet(s): {', '.join(worksheets)}")
    if other_calcs:
        bits.append(f"referenced by calc(s): {', '.join(other_calcs)}")
    return True, "MANDATORY -- " + "; ".join(bits)


def alloc_table_name(na, t):
    """The REAL, allocated PBI table name (and hence .tmdl filename) for this ir.json table --
    same lookup `extend_tablecalc_rank.py`'s own `alloc_table_name()` uses."""
    return na["tables"].get(t.get("tableau_name") or t.get("caption"))


def resolve_via_na(na, alloc_table, field_dict):
    """(alloc PBI table name, {tableau_name,...} dict) -> the real, ALREADY-EMITTED PBI name for
    this field, or None. Same two checks `TmdlModel._resolver()`/`extend_tablecalc_rank.py`'s own
    `resolve_via_na()` already use -- an allocated name that was never actually written into the
    model (declined) must resolve to None here too."""
    tid = fkey(field_dict)
    key = f"{alloc_table} | {tid}"
    name = na["columns"].get(key) or na["measures"].get(key)
    if not name:
        return None
    if f"{alloc_table} | {name}" not in na.get("emitted", []):
        return None
    return name


def make_resolver(t, na, alloc_table):
    """A field-reference resolver scoped to THIS ONE TABLE -- same narrow, same-table-only shape
    `extend_tablecalc_rank.py`'s own `make_resolver()` uses. Indexed by both a field's caption and
    its bare internal identifier (`fkey()`), since a real Tableau formula can use either
    spelling."""
    by_spelling = {}
    for c in t["columns"] + t["calcs"]:
        for spelling in (c["name"], fkey(c)):
            if spelling:
                by_spelling.setdefault(spelling, c)

    def resolve(field):
        c = by_spelling.get(field)
        if c is None:
            return None
        name = resolve_via_na(na, alloc_table, c)
        return dax_ref(alloc_table, name) if name else None

    return resolve


# ==================================================================== formula classification
def _split_top_level(s):
    """Split a comma-separated argument list at commas that are not nested inside ()/[] -- the
    exact narrow amount of parsing the 3 v1 calc shapes need (their arguments are always an
    aggregate expression and/or a plain integer, never a string literal containing a comma)."""
    args, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur))
    return [a.strip() for a in args]


def _top_level_call(formula):
    """(FUNC_NAME, [arg, ...]) if `formula` (stripped) is EXACTLY ONE function call spanning the
    whole string, else (None, None). Balance-checks the argument body so a formula that merely
    starts with `NAME(` but has trailing content after its matching `)` is correctly rejected."""
    m = _TOP_CALL_RE.match(formula or "")
    if not m:
        return None, None
    name, argstr = m.group(1), m.group(2)
    depth = 0
    for ch in argstr:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth < 0:
                return None, None
    if depth != 0:
        return None, None
    return name.upper(), _split_top_level(argstr)


def _split_top_level_division(formula):
    """(left, right) if `formula` is exactly `<left> / <right>` at the TOP level (a '/' outside
    every paren/bracket), else None. Only ever used to detect the ratio-of-two-WINDOW_SUM "moving
    average rate" shape -- not a general expression splitter."""
    depth, slash_at = 0, None
    for i, ch in enumerate(formula):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "/" and depth == 0:
            if slash_at is not None:
                return None      # more than one top-level '/' -- not this shape
            slash_at = i
    if slash_at is None:
        return None
    return formula[:slash_at].strip(), formula[slash_at + 1:].strip()


def _classify_window_sum_call(args):
    """WINDOW_SUM(<expr>, <start>, <end>) -> a shape dict, or (None, reason).

    Two DIFFERENT templates share this one Tableau function, split by whether `start`/`end` are
    both LITERAL integers:
      - Both literal, `end == 0`, `start < 0`: the well-understood FIXED trailing window --
        `{"kind": "fixed", "inner": expr, "n": window_row_count}` -- built later with the
        simpler, already-verified `TOPN`/`SUMX` template.
      - Anything else that is still a well-formed 3-argument call (real corpus example:
        `WINDOW_SUM(expr, -[Parameters].[Parameter 4], [Parameters].[Window Rolling Average -
        Start (copy)_...])` -- BOTH bounds are Tableau parameters, not just `start`): a DYNAMIC
        window -- `{"kind": "dynamic", "inner": expr, "start_text": start, "end_text": end}`.
        Whether `start_text`/`end_text` actually resolve to real DAX (typically a parameter
        reference, optionally negated) is checked later, in `generate_dax()`, via
        `dax.Unsupported` -- never guessed here.
    """
    if args is None:
        return None, "not a recognizable function call"
    if len(args) != 3:
        return None, (f"{len(args)} argument(s) -- only the explicit 3-argument "
                       f"(expression, start, end) form is supported")
    inner, start, end = args
    if _INT_RE.match(start) and _INT_RE.match(end) and int(end) == 0 and int(start) < 0:
        return {"kind": "fixed", "inner": inner, "n": -int(start) + 1}, None
    return {"kind": "dynamic", "inner": inner, "start_text": start, "end_text": end}, None


def classify_formula(formula):
    """Classifies a table-calc formula into one of this skill's 3 v1 templates, a SPECIFIC
    deferred reason (a recognized-but-out-of-scope shape -- future template, see SKILL.md), or
    (None, None) meaning this formula is not this skill's concern at all -- silently invisible,
    same convention `extend_tablecalc_rank.py`'s own RANK-only regex filter already uses for every
    non-RANK table calc.

    Returns (shape_dict, None) | (None, reason) | (None, None).
    """
    f = (formula or "").strip()

    if _INDEX_RE.match(f):
        return {"kind": "index"}, None

    name, args = _top_level_call(f)
    if name == "LOOKUP":
        if len(args) != 2:
            return None, (f"LOOKUP with {len(args)} argument(s) -- only the 2-argument form is "
                           f"recognized")
        if not _INT_RE.match(args[1]):
            return None, "LOOKUP's offset is not a literal integer -- not this skill's concern"
        offset = int(args[1])
        if offset != 0:
            return None, (f"LOOKUP(..., {offset}) is a non-zero row offset -- needs true row-lag "
                           f"DAX (e.g. OFFSET()), a different template family than this skill's "
                           f"v1 covers; deferred, see SKILL.md 'future templates'")
        return {"kind": "lookup_passthrough", "inner": args[0]}, None

    if name == "WINDOW_SUM":
        shape, reason = _classify_window_sum_call(args)
        if shape is None:
            return None, reason
        kind = "window_fixed" if shape["kind"] == "fixed" else "window_dynamic"
        return {"kind": kind, **{k: v for k, v in shape.items() if k != "kind"}}, None

    div = _split_top_level_division(f)
    if div:
        left, right = div
        ln, largs = _top_level_call(left)
        rn, rargs = _top_level_call(right)
        if ln == "WINDOW_SUM" and rn == "WINDOW_SUM":
            lshape, lreason = _classify_window_sum_call(largs)
            rshape, rreason = _classify_window_sum_call(rargs)
            if lshape is None:
                return None, f"numerator WINDOW_SUM: {lreason}"
            if rshape is None:
                return None, f"denominator WINDOW_SUM: {rreason}"
            if lshape["kind"] != rshape["kind"]:
                return None, ("ratio's two WINDOW_SUM calls use different window shapes (one "
                               "fixed-literal, one dynamic) -- not a single template")
            if lshape["kind"] == "fixed":
                if lshape["n"] != rshape["n"]:
                    return None, (f"ratio's two WINDOW_SUM calls have different window sizes "
                                   f"({lshape['n']} vs {rshape['n']}) -- not a single "
                                   f"moving-average template")
                return {"kind": "window_ratio_fixed", "inner_num": lshape["inner"],
                        "inner_den": rshape["inner"], "n": lshape["n"]}, None
            if (lshape["start_text"], lshape["end_text"]) != (
                    rshape["start_text"], rshape["end_text"]):
                return None, ("ratio's two WINDOW_SUM calls have different start/end "
                               "expressions -- not a single moving-average template")
            return {"kind": "window_ratio_dynamic", "inner_num": lshape["inner"],
                    "inner_den": rshape["inner"], "start_text": lshape["start_text"],
                    "end_text": lshape["end_text"]}, None

    if _LOOKUP_ANYWHERE_RE.search(f):
        return None, ("LOOKUP(...) appears nested inside a larger expression (e.g. a % Change "
                       "IF/IFNULL formula) -- only a bare LOOKUP(expr, 0) passthrough is "
                       "recognized; deferred, see SKILL.md 'future templates'")
    if _WINDOW_SUM_ANYWHERE_RE.search(f):
        return None, ("WINDOW_SUM(...) appears nested inside a larger expression, or as part of "
                       "a ratio this skill could not fully classify -- only a bare or "
                       "ratio-of-two top-level WINDOW_SUM call is recognized; deferred")
    m = _OTHER_WINDOW_FUNC_RE.search(f)
    if m:
        return None, (f"{m.group(1).upper()}(...) is not one of this skill's v1 templates "
                       f"(LOOKUP(...,0), INDEX(), fixed-window WINDOW_SUM) -- future template, "
                       f"see SKILL.md")
    return None, None


# ==================================================================== addressing resolution
def resolve_addressing_variants(calc):
    """Generalizes `extend_tablecalc_rank.py`'s own `resolved_partition_field()` -- instead of
    requiring every worksheet to agree on one addressing field (or declining the whole calc), this
    GROUPS worksheet usages by their resolved field into distinct variants. Each variant becomes
    its own DAX measure later; an unresolvable worksheet is reported and skipped WITHOUT blocking
    a different worksheet's resolvable addressing.

    Returns (variants, unresolved):
      variants:   [{"addr_field": str, "worksheets": [str, ...]}, ...], sorted by addr_field.
      unresolved: [{"reason": str}, ...] -- reasons that block ALL addressing for this calc (an
                  ambiguous own-definition, or none found at all). A per-worksheet DISAGREEMENT is
                  not an unresolved entry here -- it is exactly what produces multiple variants
                  instead of one.
    """
    spec = calc.get("tc_spec") or {}
    if spec.get("level_break_field"):
        return [], [{"reason": "already covered by the main pipeline's own Restart-every-field "
                                "rule -- not this extension's concern"}]
    own = sorted(set(spec.get("order_fields") or []))
    if len(own) == 1:
        return [{"addr_field": own[0], "worksheets": ["(own definition)"]}], []
    if len(own) > 1:
        return [], [{"reason": f"the calc's OWN addressing names {len(own)} different fields "
                                f"({', '.join(own)}) -- ambiguous, not a single field"}]
    ws = spec.get("ws_order_fields") or []
    if not ws:
        return [], [{"reason": "no Specific-Dimension(s) addressing found on the calc or any "
                                "worksheet"}]
    groups = {}
    for d in ws:
        groups.setdefault(d["field"], set()).add(d["worksheet"])
    variants = [{"addr_field": f, "worksheets": sorted(wss)} for f, wss in sorted(groups.items())]
    return variants, []


# ==================================================================== candidate discovery
def find_candidates(ir, na, residue):
    """Every table-calc this extension COULD act on, classified as either one-or-more promotable
    candidates or a skip (with a specific reason) -- never silently dropped, matching this whole
    pipeline's own "always report" convention. A calc whose worksheets disagree on addressing
    fans out into MULTIPLE candidate entries (one per distinct addressing variant), not one
    accept-or-decline outcome -- the one genuinely new behavior vs. `extend_tablecalc_rank.py`.

    `residue.json`'s own `"table"` field is the ALLOCATED PBI table name, same gating
    `extend_tablecalc_rank.py`'s own `find_candidates()` already uses.
    """
    declined_keys = {(item.get("table"), item.get("subject"))
                      for item in residue.get("calc_residue", [])}
    out = []
    for t in ir["tables"]:
        table_caption = t.get("caption") or t.get("tableau_name")
        alloc_table = alloc_table_name(na, t)
        for c in t["calcs"]:
            if c.get("kind") != "tablecalc":
                continue
            if (alloc_table, c["name"]) not in declined_keys:
                continue    # not currently outstanding -- nothing for this extension to do
            shape, reason = classify_formula(c.get("formula") or "")
            if shape is None and reason is None:
                continue    # not this skill's concern at all -- silently invisible

            base = {"table_caption": table_caption, "alloc_table": alloc_table,
                    "calc": c, "name": c["name"], "formula": c["formula"]}
            if alloc_table is None:
                out.append({**base, "status": "skipped",
                            "reason": "this table's own PBI name could not be resolved from "
                                      "name_authority.json"})
                continue
            if shape is None:
                out.append({**base, "status": "skipped", "reason": reason})
                continue

            if shape["kind"] == "lookup_passthrough":
                if not dax.is_aggregate(shape["inner"]):
                    out.append({**base, "status": "skipped",
                                "reason": "this calc would resolve to a calculated COLUMN, not a "
                                          "MEASURE -- the column-emission path is not "
                                          "implemented in this extension yet"})
                    continue
                out.append({**base, "status": "candidate", "shape": shape})
                continue

            if shape["kind"] != "index":
                inners = ([shape["inner"]] if shape["kind"] in ("window_fixed", "window_dynamic")
                          else [shape["inner_num"], shape["inner_den"]])
                bad = next((e for e in inners if not dax.is_aggregate(e)), None)
                if bad is not None:
                    out.append({**base, "status": "skipped",
                                "reason": f"{bad!r} is not an aggregate expression -- this calc "
                                          f"would resolve to a calculated COLUMN, not a MEASURE "
                                          f"-- not implemented in this extension yet"})
                    continue

            # index / window_fixed / window_dynamic / *_ratio_* -- all need resolved addressing,
            # and all
            # can fan out into MULTIPLE candidates instead of one-or-decline.
            variants, unresolved = resolve_addressing_variants(c)
            for u in unresolved:
                out.append({**base, "status": "skipped", "reason": u["reason"]})
            multi = len(variants) > 1
            for v in variants:
                suffix = f" — by {v['addr_field']}" if multi else ""
                out.append({**base, "status": "candidate", "shape": shape,
                            "name": base["name"] + suffix, "addr_field": v["addr_field"],
                            "variant_worksheets": v["worksheets"]})
    return out


# ==================================================================== DAX generation + patch
def build_param_resolver(ir, na):
    """Whole-workbook parameter resolver: (tableau_param_name) -> a bare, model-wide-unique
    `[Measure Name]` DAX reference, or None. Mirrors `tmdl.py`'s own `resolve_param()` closure
    exactly (same dual-spelling index: friendly name, raw internal identifier, and any recorded
    alias) -- parameters are model-wide, unlike columns/measures, so this is the one resolver in
    this script that is NOT scoped to a single table."""
    pindex = {}
    for p in ir.get("parameters", []) or []:
        rec = na.get("parameters", {}).get(p["name"])
        if rec is None:
            continue
        spellings = [p["name"], (p.get("tableau_name") or "").strip("[]")]
        spellings += [a.strip("[]") for a in (p.get("aliases") or [])]
        for spelling in spellings:
            if spelling:
                pindex.setdefault(spelling, rec)
                pindex.setdefault(spelling.lower(), rec)

    def resolve_param(pname):
        key = str(pname).strip("[]")
        rec = pindex.get(key) or pindex.get(key.lower())
        return f"[{rec['measure']}]" if rec else None

    return resolve_param


def generate_dax(t, na, alloc_table, candidate, ir):
    """Dispatches to one of this skill's DAX templates. Raises `dax.Unsupported` for anything the
    narrow same-table resolver (or, for a DYNAMIC window, the whole-workbook parameter resolver)
    cannot handle -- reported as a skip by the caller, never a guess.

    `window_dynamic`/`window_ratio_dynamic` use a RANK-DIFFERENCE template instead of the simpler,
    already-verified `TOPN`/`SUMX` template `window_fixed`/`window_ratio_fixed` use, because their
    start/end bounds are arbitrary DAX expressions (typically a Tableau parameter's own
    `SELECTEDVALUE` measure), not literal integers `TOPN`'s row-count argument could take
    directly: each row's own rank (via `RANKX`) is compared against `<current row's rank> +
    <bound>`, computed once per row through `ADDCOLUMNS` so both the start and end comparison
    reuse the same rank rather than recomputing it. This is a materially newer, less-proven
    pattern than the fixed-window template -- flagged as such in the report.
    """
    resolve = make_resolver(t, na, alloc_table)
    shape = candidate["shape"]

    if shape["kind"] == "lookup_passthrough":
        expr, refs, _params = dax.translate(shape["inner"], resolve)
        return expr, refs

    addr_col = resolve(candidate["addr_field"])
    if addr_col is None:
        raise dax.Unsupported(
            f"addressing field {candidate['addr_field']!r} did not resolve to a real, "
            f"already-emitted column on this same table")
    qtable = "'" + alloc_table.replace("'", "''") + "'"

    if shape["kind"] == "index":
        expr = f"RANKX(ALLEXCEPT({qtable}, {addr_col}), {addr_col}, , ASC, Dense)"
        return expr, []

    if shape["kind"] in ("window_fixed", "window_ratio_fixed"):
        window = (f"TOPN({shape['n']}, "
                  f"FILTER(ALLEXCEPT({qtable}, {addr_col}), "
                  f"{addr_col} <= SELECTEDVALUE({addr_col})), {addr_col}, DESC)")
    else:
        resolve_param = build_param_resolver(ir, na)
        try:
            start_dax, _r1, _p1 = dax.translate(shape["start_text"], resolve,
                                                 resolve_param=resolve_param)
            end_dax, _r2, _p2 = dax.translate(shape["end_text"], resolve,
                                               resolve_param=resolve_param)
        except dax.Unsupported as e:
            raise dax.Unsupported(f"window bound could not be resolved: {e}")
        rank_expr = f"RANKX(ALLEXCEPT({qtable}, {addr_col}), {addr_col}, , ASC, Dense)"
        ranked = f'ADDCOLUMNS(ALLEXCEPT({qtable}, {addr_col}), "__r", {rank_expr})'
        window = (f"FILTER({ranked}, [__r] >= {rank_expr} + ({start_dax}) && "
                  f"[__r] <= {rank_expr} + ({end_dax}))")

    if shape["kind"] in ("window_fixed", "window_dynamic"):
        inner_expr, refs, _params = dax.translate(shape["inner"], resolve)
        expr = f"VAR __window = {window}\nRETURN\n    SUMX(__window, {inner_expr})"
        return expr, refs

    num_expr, num_refs, _p1 = dax.translate(shape["inner_num"], resolve)
    den_expr, den_refs, _p2 = dax.translate(shape["inner_den"], resolve)
    expr = (f"VAR __window = {window}\nRETURN\n    "
            f"DIVIDE(SUMX(__window, {num_expr}), SUMX(__window, {den_expr}))")
    return expr, sorted(set(num_refs) | set(den_refs))


def promote(pbip_dir, out_dir, candidates, ir, na):
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
        t = next(tt for tt in ir["tables"]
                  if (tt.get("caption") or tt.get("tableau_name")) == cand["table_caption"])
        alloc_table = cand["alloc_table"]
        try:
            expr, refs = generate_dax(t, na, alloc_table, cand, ir)
        except dax.Unsupported as e:
            cand = {**cand, "status": "skipped", "reason": f"DAX generation declined: {e}"}
            results.append(cand)
            continue
        tmdl_path = os.path.join(out_dir, model_dir, "definition", "tables",
                                  f"{alloc_table}.tmdl")
        measure_block = "\n".join(_measure(alloc_table, cand["name"], expr))
        with open(tmdl_path, encoding="utf-8") as fh:
            text = fh.read()
        m = _TOP_LEVEL_COLUMN_OR_PARTITION_RE.search(text)
        insert_at = m.start() if m else len(text)
        new_text = text[:insert_at] + measure_block + "\n" + text[insert_at:]
        with open(tmdl_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        cand = {**cand, "status": "promoted", "dax": expr, "tmdl_file": tmdl_path,
                "referenced_fields": refs}
        results.append(cand)
    return results


# ==================================================================== report
def render_report(workbook_ref, pbip_dir, out_dir, results, ir, workbook_xml):
    promoted = [r for r in results if r["status"] == "promoted"]
    skipped = [r for r in results if r["status"] == "skipped"]
    lines = [
        "# DAX Measure Extension Report (EXPERIMENTAL, isolated from the main pipeline)", "",
        f"Source workbook: `{workbook_ref}`", f"Existing build: `{pbip_dir}`",
        f"New, patched build: `{out_dir}`", "",
        f"{len(promoted)} table-calc measure(s) promoted from declined to a real DAX measure "
        f"(LOOKUP(...,0) passthrough / INDEX() row position / fixed-window or dynamic "
        f"(parameter-driven) WINDOW_SUM moving average or rate). {len(skipped)} candidate(s) "
        f"checked and left alone, each for a specific, reported reason -- never silently "
        f"dropped.", "",
        "**Model-only.** This skill never reads or writes anything under a `*.Report` folder -- "
        "no page, visual, or filter is touched. When a calc's addressing disagreed across "
        "worksheets, EACH distinct addressing got its OWN measure below; wiring the right "
        "worksheet's visual to the right measure variant is a separate, human step.", "",
    ]
    if promoted:
        lines.append("## Promoted")
        lines.append("")
        for r in promoted:
            lines.append(f"### `{r['name']}` (table `{r['table_caption']}`)")
            lines.append("")
            lines.append(f"**Original Tableau formula:** `{r['formula']}`")
            lines.append("")
            if "addr_field" in r:
                lines.append(f"**Addressing field:** `{r['addr_field']}`")
                lines.append("")
                lines.append(f"**Worksheet(s) using this variant:** "
                              f"{', '.join(r['variant_worksheets'])}")
                lines.append("")
                lines.append(
                    "> Assumes this field's natural ascending/descending sort matches Tableau's "
                    "own row order for it (true for real dates and for consistently-formatted "
                    "month strings), and that the model's grain at this scope is one row per "
                    "distinct value of it -- verify before trusting the number.")
                lines.append("")
            if r["shape"]["kind"] in ("window_dynamic", "window_ratio_dynamic"):
                lines.append(
                    "> **DYNAMIC window.** This calc's own start/end bounds are not fixed "
                    "literals (typically a Tableau parameter the report-viewer can change) -- "
                    "this measure uses a rank-difference template (`ADDCOLUMNS`+`RANKX`+"
                    "`FILTER`), which is materially newer and less proven than the fixed-window "
                    "`TOPN` template. Verify extra carefully in Desktop, including after "
                    "changing the underlying parameter's value.")
                lines.append("")
            lines.append(f"**New DAX measure:**\n```\n{r['dax']}\n```")
            lines.append("")
            lines.append(f"**Written to:** `{r['tmdl_file']}`")
            lines.append("")
    if skipped:
        lines.append("## Checked, not promoted")
        lines.append("")
        lines.append(
            "Split by whether the calc is actually LIVE anywhere in the workbook -- checked "
            "directly against the workbook's own XML (which worksheet, if any, references this "
            "calc's internal identifier) and against every other calc's formula text, never "
            "guessed. A calc with no addressing recorded and no usage anywhere is usually the "
            "SAME root cause: Tableau only saves a per-usage \"Compute Using\" setting for a "
            "calc that is actually placed somewhere.")
        lines.append("")
        mandatory, optional, unclassified = [], [], []
        for r in skipped:
            if workbook_xml is None:
                unclassified.append(r)
                continue
            is_used, desc = classify_usage(workbook_xml, ir, r["calc"])
            (mandatory if is_used else optional).append((r, desc))
        if mandatory:
            lines.append("### Mandatory -- used on a live worksheet, worth fixing")
            lines.append("")
            for r, desc in mandatory:
                lines.append(f"- `{r['name']}` (table `{r['table_caption']}`): {r['reason']}")
                lines.append(f"  - {desc}")
            lines.append("")
        if optional:
            lines.append("### Optional -- not used anywhere, safe to leave declined")
            lines.append("")
            for r, desc in optional:
                lines.append(f"- `{r['name']}` (table `{r['table_caption']}`): {r['reason']}")
                lines.append(f"  - {desc}")
            lines.append("")
        if unclassified:
            lines.append("### Usage unknown (source workbook could not be read for cross-check)")
            lines.append("")
            for r in unclassified:
                lines.append(f"- `{r['name']}` (table `{r['table_caption']}`): {r['reason']}")
            lines.append("")
    lines.append("## Before trusting this build")
    lines.append("")
    lines.append("- [ ] Open the new build in Power BI Desktop; confirm it loads with no errors.")
    lines.append(
        "- [ ] For each promoted measure, spot-check its value against Tableau's own rendered "
        "number on a worksheet that uses it -- this extension's own name allocation and "
        "insertion point are NOT the same code path the main pipeline's `NameAuthority` and "
        "`TmdlModel.build()` use, so treat this build as provisional until verified.")
    lines.append(
        "- [ ] For a promoted `WINDOW_SUM`/`INDEX` measure, also confirm the model's grain at "
        "the resolved addressing field is one row per distinct value -- the rolling-window "
        "template sums/ranks ROWS of the table, not periods, so a coarser or finer grain than "
        "expected will silently span more or fewer periods than Tableau did.")
    lines.append(
        "- [ ] No visual was touched by this step -- if a page should show one of several "
        "variants of the same calc, bind it by hand (or via a later, separate step).")
    lines.append(
        "- [ ] Run `tools/pbip_rules/lint.py <new_pbip_dir> --schemas` for a structural check.")
    lines.append("")
    return "\n".join(lines)


# ==================================================================== CLI
def main():
    parser = argparse.ArgumentParser(
        description="EXPERIMENTAL: promote LOOKUP(...,0)/INDEX()/fixed-window WINDOW_SUM table "
                    "calcs into real DAX measures -- one per distinct addressing variant found "
                    "across worksheets -- in a separate copy of an existing build-tableau-pbip "
                    "project. Never touches any *.Report file.")
    parser.add_argument("twbx", help="original .twbx/.twb (provenance only, not re-parsed)")
    parser.add_argument("pbip_dir", help="existing build-tableau-pbip output folder "
                                          "(must contain _work/ir.json etc.)")
    parser.add_argument("--out", required=True, help="new output folder (must not exist)")
    args = parser.parse_args()

    ir, na, residue = load_workbook_state(args.pbip_dir)
    candidates = find_candidates(ir, na, residue)
    results = promote(args.pbip_dir, args.out, candidates, ir, na)

    try:
        workbook_xml = read_workbook_xml(args.twbx)
    except (OSError, zipfile.BadZipFile, StopIteration):
        workbook_xml = None    # usage classification is best-effort reporting, never fatal

    report = render_report(args.twbx, args.pbip_dir, args.out, results, ir, workbook_xml)
    report_path = os.path.join(args.out, "DAX_MEASURE_EXTENSION_REPORT.md")
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)

    n_promoted = sum(1 for r in results if r["status"] == "promoted")
    n_skipped = sum(1 for r in results if r["status"] == "skipped")
    print(f"[ok] promoted={n_promoted} skipped={n_skipped} -> {args.out}")
    print(f"     report: {report_path}")
    sys.exit(0)


# ==================================================================== self-test
def _selftest():
    # ---- classify_formula: all 3 v1 shapes, and every deferred/not-our-concern case ------
    shape, reason = classify_formula("LOOKUP(MAX([x]), 0)")
    assert shape == {"kind": "lookup_passthrough", "inner": "MAX([x])"}, (shape, reason)

    shape, reason = classify_formula("LOOKUP(MAX([x]), -1)")
    assert shape is None and "non-zero row offset" in reason, (shape, reason)

    shape, reason = classify_formula("INDEX()")
    assert shape == {"kind": "index"}, (shape, reason)

    shape, reason = classify_formula("WINDOW_SUM(SUM([x]),-6,0)")
    assert shape == {"kind": "window_fixed", "inner": "SUM([x])", "n": 7}, (shape, reason)

    shape, reason = classify_formula("WINDOW_SUM(SUM([x]),-[Parameters].[P],0)")
    assert shape == {"kind": "window_dynamic", "inner": "SUM([x])",
                      "start_text": "-[Parameters].[P]", "end_text": "0"}, (shape, reason)

    # real corpus shape: BOTH bounds are parameters, not just start.
    shape, reason = classify_formula(
        "WINDOW_SUM(SUM([x]),-[Parameters].[P],[Parameters].[Q])")
    assert shape == {"kind": "window_dynamic", "inner": "SUM([x])",
                      "start_text": "-[Parameters].[P]",
                      "end_text": "[Parameters].[Q]"}, (shape, reason)

    shape, reason = classify_formula("WINDOW_SUM(SUM([a]),-6,0) / WINDOW_SUM(SUM([b]),-6,0)")
    assert shape == {"kind": "window_ratio_fixed", "inner_num": "SUM([a])",
                      "inner_den": "SUM([b])", "n": 7}, (shape, reason)

    shape, reason = classify_formula(
        "WINDOW_SUM(SUM([a]),-6,0) / WINDOW_SUM(SUM([b]),-29,0)")
    assert shape is None and "different window sizes" in reason, (shape, reason)

    shape, reason = classify_formula(
        "WINDOW_SUM(SUM([a]),-[Parameters].[P],0) / WINDOW_SUM(SUM([b]),-6,0)")
    assert shape is None and "different window shapes" in reason, (shape, reason)

    shape, reason = classify_formula(
        "WINDOW_SUM(SUM([a]),-[Parameters].[P],0) / WINDOW_SUM(SUM([b]),-[Parameters].[Q],0)")
    assert shape is None and "different start/end expressions" in reason, (shape, reason)

    shape, reason = classify_formula(
        "WINDOW_SUM(SUM([a]),-[Parameters].[P],0) / WINDOW_SUM(SUM([b]),-[Parameters].[P],0)")
    assert shape == {"kind": "window_ratio_dynamic", "inner_num": "SUM([a])",
                      "inner_den": "SUM([b])", "start_text": "-[Parameters].[P]",
                      "end_text": "0"}, (shape, reason)

    shape, reason = classify_formula("IF ISNULL(LOOKUP(MAX([x]),-1)) THEN 0 ELSE 1 END")
    assert shape is None and "nested inside a larger expression" in reason, (shape, reason)

    shape, reason = classify_formula("RUNNING_SUM(SUM([x]))")
    assert shape is None and "future template" in reason, (shape, reason)

    shape, reason = classify_formula("[x] + 1")
    assert shape is None and reason is None, (shape, reason)   # not this skill's concern at all
    print("OK  classify_formula: all 3 v1 shapes, and every deferred/not-our-concern case")

    # ---- resolve_addressing_variants -------------------------------------------------------
    variants, unresolved = resolve_addressing_variants({"tc_spec": {"order_fields": ["Month"]}})
    assert variants == [{"addr_field": "Month", "worksheets": ["(own definition)"]}], variants
    assert unresolved == []

    variants, unresolved = resolve_addressing_variants({"tc_spec": {"ws_order_fields": [
        {"field": "Month", "worksheet": "W1"}, {"field": "Partner", "worksheet": "W2"},
        {"field": "Month", "worksheet": "W3"}]}})
    by_field = {v["addr_field"]: v["worksheets"] for v in variants}
    assert by_field == {"Month": ["W1", "W3"], "Partner": ["W2"]}, by_field
    assert unresolved == []
    print("OK  resolve_addressing_variants: own-definition, and worksheet disagreement fans out "
          "into 2 variants instead of declining")

    variants, unresolved = resolve_addressing_variants({"tc_spec": {}})
    assert variants == [] and "no Specific-Dimension" in unresolved[0]["reason"], unresolved
    print("OK  resolve_addressing_variants: no addressing anywhere -> unresolved, no variants")

    # ---- find_candidates + generate_dax end-to-end, no real build on disk -----------------
    ir = {"tables": [
        {"tableau_name": "ds1", "caption": "T1", "columns": [
            {"name": "Deflections", "tableau_name": "[deflections]"},
            {"name": "Month", "tableau_name": "[month]"},
            {"name": "Partner", "tableau_name": "[partner]"},
        ], "calcs": [
            {"name": "Rolling Deflections", "tableau_name": "[Calculation_1]",
             "kind": "tablecalc", "formula": "WINDOW_SUM(SUM([deflections]),-6,0)",
             "tc_spec": {"ws_order_fields": [{"field": "Month", "worksheet": "W1"},
                                              {"field": "Partner", "worksheet": "W2"}]}},
            {"name": "Lookup Passthrough", "tableau_name": "[Calculation_2]",
             "kind": "tablecalc", "formula": "LOOKUP(MAX([deflections]),0)", "tc_spec": {}},
            {"name": "Row Number", "tableau_name": "[Calculation_3]",
             "kind": "tablecalc", "formula": "INDEX()",
             "tc_spec": {"order_fields": ["Month"]}},
            {"name": "Dynamic Rolling", "tableau_name": "[Calculation_4]",
             "kind": "tablecalc",
             "formula": "WINDOW_SUM(SUM([deflections]),-[Parameters].[Parameter 4],0)",
             "tc_spec": {"order_fields": ["Month"]}},
        ]},
    ]}
    ir["parameters"] = [{"name": "Window Size", "tableau_name": "[Parameter 4]", "aliases": []}]
    na = {"tables": {"ds1": "T1"},
          "columns": {"T1 | deflections": "Deflections", "T1 | month": "Month",
                      "T1 | partner": "Partner"},
          "measures": {}, "parameters": {"Window Size": {"measure": "Window Size Selected"}},
          "emitted": ["T1 | Deflections", "T1 | Month", "T1 | Partner"]}
    residue = {"calc_residue": [
        {"table": "T1", "subject": "Rolling Deflections",
         "reason": "table calculation -- addressing/partitioning is not in the formula text"},
        {"table": "T1", "subject": "Lookup Passthrough",
         "reason": "table calculation -- addressing/partitioning is not in the formula text"},
        {"table": "T1", "subject": "Row Number",
         "reason": "table calculation -- addressing/partitioning is not in the formula text"},
        {"table": "T1", "subject": "Dynamic Rolling",
         "reason": "table calculation -- addressing/partitioning is not in the formula text"},
    ]}
    cands = find_candidates(ir, na, residue)
    promoted_names = sorted(c["name"] for c in cands if c["status"] == "candidate")
    assert promoted_names == [
        "Dynamic Rolling", "Lookup Passthrough", "Rolling Deflections — by Month",
        "Rolling Deflections — by Partner", "Row Number",
    ], promoted_names
    print("OK  find_candidates: worksheet disagreement on Rolling Deflections fans out into 2 "
          "named candidates instead of one decline; LOOKUP passthrough and INDEX() need no "
          "worksheet-agreement at all")

    t = ir["tables"][0]
    by_name = {c["name"]: c for c in cands if c["status"] == "candidate"}

    expr, refs = generate_dax(t, na, "T1", by_name["Lookup Passthrough"], ir)
    assert expr == "MAX('T1'[Deflections])", expr
    print(f"OK  generate_dax lookup_passthrough: {expr}")

    expr, refs = generate_dax(t, na, "T1", by_name["Row Number"], ir)
    assert expr == "RANKX(ALLEXCEPT('T1', 'T1'[Month]), 'T1'[Month], , ASC, Dense)", expr
    print(f"OK  generate_dax index: {expr}")

    expr, refs = generate_dax(t, na, "T1", by_name["Rolling Deflections — by Month"], ir)
    assert "TOPN(7," in expr and "SUMX(__window, SUM('T1'[Deflections]))" in expr, expr
    print(f"OK  generate_dax window_fixed: {expr}")

    ratio_shape = {"kind": "window_ratio_fixed", "inner_num": "SUM([deflections])",
                   "inner_den": "SUM([deflections])", "n": 7}
    ratio_cand = {**by_name["Rolling Deflections — by Month"], "shape": ratio_shape}
    expr, refs = generate_dax(t, na, "T1", ratio_cand, ir)
    assert expr.startswith("VAR __window") and "DIVIDE(SUMX(__window" in expr, expr
    print(f"OK  generate_dax window_ratio_fixed: {expr}")

    expr, refs = generate_dax(t, na, "T1", by_name["Dynamic Rolling"], ir)
    assert "ADDCOLUMNS(ALLEXCEPT('T1', 'T1'[Month])" in expr, expr
    assert "+ (-[Window Size Selected])" in expr, expr
    assert "SUMX(__window, SUM('T1'[Deflections]))" in expr, expr
    print(f"OK  generate_dax window_dynamic (parameter-driven window): {expr}")

    bad_cand = {**by_name["Rolling Deflections — by Month"], "addr_field": "Nonexistent"}
    try:
        generate_dax(t, na, "T1", bad_cand, ir)
        raise AssertionError("expected dax.Unsupported for an unresolvable addressing field")
    except dax.Unsupported:
        pass
    print("OK  generate_dax declines (does not guess) when the addressing field cannot be "
          "resolved to a real column")

    unresolvable_dynamic = {**by_name["Dynamic Rolling"],
                             "shape": {**by_name["Dynamic Rolling"]["shape"],
                                       "start_text": "-[Parameters].[Nonexistent Param]"}}
    try:
        generate_dax(t, na, "T1", unresolvable_dynamic, ir)
        raise AssertionError("expected dax.Unsupported for an unresolvable parameter reference")
    except dax.Unsupported:
        pass
    print("OK  generate_dax declines (does not guess) when a dynamic window bound's parameter "
          "cannot be resolved")

    # ---- usage classification: mandatory (live somewhere) vs optional (dead field) --------
    fake_xml = (
        "<workbook><worksheets>"
        "<worksheet name='Dashboard Sheet'><table><view>[ds].[Calculation_3]</view></table>"
        "</worksheet>"
        "<worksheet name='Other Sheet'><table><view>[ds].[unrelated_field]</view></table>"
        "</worksheet>"
        "</worksheets></workbook>")
    assert find_worksheet_usage(fake_xml, "[Calculation_3]") == ["Dashboard Sheet"]
    assert find_worksheet_usage(fake_xml, "[Calculation_99]") == []
    print("OK  find_worksheet_usage: finds the one worksheet referencing a calc, none for an "
          "unreferenced one")

    ir_usage = {"tables": [{"calcs": [
        {"name": "Base Calc", "tableau_name": "[Calculation_3]", "formula": "SUM([x])"},
        {"name": "Wraps Base Calc", "tableau_name": "[Calculation_5]",
         "formula": "[Calculation_3] * 2"},
        {"name": "Truly Dead", "tableau_name": "[Calculation_99]", "formula": "SUM([y])"},
    ]}]}
    assert referenced_by_other_calc(ir_usage, "[Calculation_3]") == ["Wraps Base Calc"]
    assert referenced_by_other_calc(ir_usage, "[Calculation_99]") == []
    print("OK  referenced_by_other_calc: finds the calc that reads another calc as a building "
          "block, none for a truly unreferenced one")

    is_used, desc = classify_usage(fake_xml, ir_usage,
                                    {"tableau_name": "[Calculation_3]"})
    assert is_used and "MANDATORY" in desc and "Dashboard Sheet" in desc, desc
    is_used, desc = classify_usage(fake_xml, ir_usage,
                                    {"tableau_name": "[Calculation_99]"})
    assert not is_used and "OPTIONAL" in desc, desc
    print("OK  classify_usage: MANDATORY when used on a worksheet, OPTIONAL when it is neither "
          "placed anywhere nor read by another calc")

    print("extend_dax_measures.py self-test OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        sys.exit(0)
    if len(sys.argv) == 1:
        print("usage: extend_dax_measures.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>")
        sys.exit(2)
    main()
