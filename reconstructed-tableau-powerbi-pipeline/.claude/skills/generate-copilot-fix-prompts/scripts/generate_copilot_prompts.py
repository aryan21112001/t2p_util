#!/usr/bin/env python3
"""
Turn a build-tableau-pbip workbook's `_work/residue.json` into ready-to-paste Copilot prompts.

RESIDUE.md already says what did not translate and why. This script groups those declined items
by the underlying gap PATTERN (reusing validate-tableau-pbip-mapping's own FIX_RULES table, not a
new one) and writes one Copilot-ready prompt per pattern -- covering every affected field in one
shot, rather than one near-duplicate prompt per instance. Each prompt is deliberately DAX-free: it
states what's affected and why, and asks Copilot to write the DAX, because this script has not
verified real column names against the live TMDL.

Usage:
  python3 generate_copilot_prompts.py <build-tableau-pbip or generate-dummy-data output> [--out <folder>]
"""
import os
import re
import sys
import glob
import json
import argparse
from collections import OrderedDict

# Reuse the FIX_RULES pattern table from validate-tableau-pbip-mapping instead of duplicating it --
# see that skill's SKILL.md ("Suggested fixes: a pattern table, not fabricated per instance").
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MAPPING_SKILL_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "validate-tableau-pbip-mapping", "scripts")
)
sys.path.insert(0, _MAPPING_SKILL_SCRIPTS)
from validate_mapping import FIX_RULES, suggest_fix, GENERIC_FIX  # noqa: E402

# Reuse build-tableau-pbip's OWN aggregate-detector instead of a second, disagreeing
# implementation -- this is the exact seed rule its tmdl.py `_aggregate_map()` fixpoint uses to
# decide Measure vs calculated Column, so this skill's guidance matches what a real rebuild emits.
_BUILD_SKILL_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "build-tableau-pbip", "scripts")
)
sys.path.insert(0, _BUILD_SKILL_SCRIPTS)
from dax import is_aggregate  # noqa: E402


# ============================================================== grouping
def rule_index(reason):
    """Which FIX_RULES entry (by position) matches this reason text, or None for GENERIC_FIX."""
    for i, (pat, _fn) in enumerate(FIX_RULES):
        if pat.search(reason or ""):
            return i
    return None


def normalize_reason(reason):
    """Collapse instance-specific identifiers out of a reason string so unrelated instances of
    the same underlying (uncatalogued) gap still group together, without merging genuinely
    different GENERIC_FIX reasons that happen to share only their fixed wording."""
    r = reason or ""
    r = re.sub(r"'[^']*'", "'...'", r)
    r = re.sub(r"\[[^\]]*\]", "[...]", r)
    r = re.sub(r"\b\d+\b", "N", r)
    return r.strip()


def dedupe_items(items):
    """residue.json sometimes logs the exact same declined object more than once (e.g. a
    physical-layer join reported from both sides). Collapse byte-identical rows so a group's
    "affected fields" list doesn't repeat the same entry -- this never changes what's reported,
    only how many times an identical row is printed."""
    seen = set()
    out = []
    for it in items:
        key = (it.get("tier"), it.get("table"), it.get("subject"), it.get("reason"), it.get("formula"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


_DEP_RE = re.compile(r"^depends on '([^']*)'(?: in table '[^']*')?, which was itself declined")


def load_ir_calc_index(wb_dir):
    """Map a bare Tableau field id (e.g. 'Calculation_123...') to its own {name, formula},
    read from this workbook's own `_work/ir.json`. Cascade root causes are frequently internal
    calcs that never appear on any shelf, so they never get their own residue.json entry -- this
    is the only place their real name/formula can be recovered for the SME."""
    path = os.path.join(wb_dir, "_work", "ir.json")
    index = {}
    if not os.path.isfile(path):
        return index
    try:
        with open(path, encoding="utf-8") as fh:
            ir = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return index
    for table in ir.get("tables", []):
        for calc in table.get("calcs", []):
            tid = (calc.get("tableau_name") or "").strip("[]")
            if tid:
                index[tid] = {"name": calc.get("name") or tid, "formula": calc.get("formula")}
    return index


_FIELDREF_RE = re.compile(r"\[([^\]]+)\]")
_PARAM_REF_RE = re.compile(r"\[Parameters\]\.\[([^\]]+)\]")


def _extract_refs(formula):
    """(param_ids, field_ids) referenced by a Tableau formula's own text. `[Parameters].[X]` is
    matched and stripped FIRST so scanning what's left for plain field references never
    double-counts the literal `Parameters` namespace token, or misreads a parameter id `X` as an
    ordinary calc/column id -- the two id spaces are unrelated and can coincidentally collide."""
    formula = formula or ""
    param_ids = _PARAM_REF_RE.findall(formula)
    remainder = _PARAM_REF_RE.sub("", formula)
    field_ids = [f for f in _FIELDREF_RE.findall(remainder) if not f.startswith(":")]
    return param_ids, field_ids


def load_ir_full_index(wb_dir):
    """Everything this skill needs from a workbook's own `_work/ir.json` beyond
    `load_ir_calc_index()`, in one pass:
      calc_by_id:      {bare tableau id -> {"name", "formula", "tc_spec"}} (same shape as
                       load_ir_calc_index, plus `tc_spec` -- extract.py's `table_calc_spec()`
                       output, i.e. the calc's own `<table-calc>` XML (ordering type, rank/diff
                       options, resolved `order_fields`) when this calc is a table calculation,
                       else None. This is real workbook data that already exists in `ir.json` --
                       `describe_table_calc()` below is what turns it into a human-readable line
                       instead of leaving every table-calc decline's reason as a bare "addressing
                       is not in the formula text" with nothing to act on.
      calc_id_by_name: {PBI display name -> bare tableau id}     (reverse lookup)
      real_col_ids:    {bare tableau id -> PBI display name} for REAL (never-calculated) columns
                       -- a terminal leaf in any dependency chain. A real column's Tableau
                       identity (e.g. `approved_auth_transaction_count`) is ALSO not the model
                       name (`Approved Auth Transaction Count`) -- same caption-vs-internal-name
                       split as a calc, just less visually obvious because it's usually only a
                       case/spacing difference. Kept as `{id: name}` rather than a bare set so
                       `resolve_formula_display` can substitute it too; every existing `in`
                       membership check on this still works unchanged since it's still keyed the
                       same way.
      param_by_id:     {bare tableau id -> {"name","value"} dict, or None if that EXACT id string
                       was recorded with two different names/values -- a known Tableau internal-
                       ID-collision hazard (the same raw id can mean different things in different
                       nested contexts in one file) -- never silently pick one candidate.
    All-empty on a missing/unreadable ir.json, same fail-open-to-nothing contract as
    load_ir_calc_index -- every caller degrades to "could not verify" rather than crashing.
    """
    path = os.path.join(wb_dir, "_work", "ir.json")
    calc_by_id, real_col_ids, param_candidates = {}, {}, {}
    if not os.path.isfile(path):
        return calc_by_id, {}, real_col_ids, {}
    try:
        with open(path, encoding="utf-8") as fh:
            ir = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return calc_by_id, {}, real_col_ids, {}
    for table in ir.get("tables", []):
        for calc in table.get("calcs", []):
            tid = (calc.get("tableau_name") or "").strip("[]")
            if tid:
                calc_by_id[tid] = {"name": calc.get("name") or tid, "formula": calc.get("formula"),
                                    "tc_spec": calc.get("tc_spec")}
        for col in table.get("columns", []):
            tid = (col.get("tableau_name") or "").strip("[]")
            if tid:
                real_col_ids[tid] = col.get("name") or tid
    for p in ir.get("parameters", []):
        tid = (p.get("tableau_name") or "").strip("[]")
        if tid:
            param_candidates.setdefault(tid, []).append(p)
    param_by_id = {}
    for tid, cands in param_candidates.items():
        uniq = {(c.get("name"), c.get("value")) for c in cands}
        param_by_id[tid] = cands[0] if len(uniq) == 1 else None
    calc_id_by_name = {c["name"]: tid for tid, c in calc_by_id.items()}
    return calc_by_id, calc_id_by_name, real_col_ids, param_by_id


_PARAM_ALIAS_SUFFIX_RE = re.compile(r"^(.*\S)\s+\d+$")


def lookup_param(pid, param_by_id):
    """(param dict or None, status) for a formula's raw `[Parameters].[pid]` reference.

    `param_by_id.get(pid)` alone conflates two genuinely different situations under the same
    `None`: an id this workbook recorded MULTIPLE conflicting values for (real ambiguity -- never
    guess which one a formula meant), and an id that was never recorded as its own entry AT ALL.
    Found on a real corpus workbook (WorkbookB Dashboard): `[Parameters].[Parameter 9 1]` was
    reported "AMBIGUOUS, multiple saved values recorded" when `ir.json` in fact has ZERO entries
    under that exact id -- the message was simply wrong about WHY it could not be resolved.

    The real reason: Tableau appends a bare numeric suffix (`Parameter 9 1`, `Parameter 9 2`, ...)
    to a parameter's OWN internal id when the same logical parameter gets a second/third copy
    minted in another datasource's context -- the same "(copy)_<digits>" aliasing this pipeline
    already resolves for calculated fields, just a different suffix shape for parameters. When the
    base id (`Parameter 9`, suffix stripped) IS present and itself unambiguous, the suffixed id is
    resolved to that SAME canonical parameter rather than reported as an unrelated ambiguity.

    status is one of:
      "ok"       -- pid resolved directly, no aliasing involved.
      "alias"    -- pid resolved via the base-id fallback; the returned dict is the BASE id's.
      "ambiguous" -- pid (or, if aliasing was tried, the base id) has multiple conflicting
                     recorded values -- a real, earned ambiguity, never guessed past.
      "unresolved" -- pid was never recorded under any id this function could find; genuinely
                     absent from this workbook's extracted parameters, not merely ambiguous.
    """
    if pid in param_by_id:
        p = param_by_id[pid]
        return (p, "ok") if p is not None else (None, "ambiguous")
    m = _PARAM_ALIAS_SUFFIX_RE.match(pid)
    if m:
        base = m.group(1)
        if base in param_by_id:
            p = param_by_id[base]
            return (p, "alias") if p is not None else (None, "ambiguous")
    return None, "unresolved"


def compute_agg_map_by_name(calc_by_id):
    """{PBI display name -> True (build-tableau-pbip's tmdl.py promotes this to a MEASURE) |
    False (stays a calculated COLUMN)} -- reproduces tmdl.py's own `_aggregate_map()` fixpoint
    EXACTLY (same seed rule, same bracket-reference propagation) from this workbook's own
    `_work/ir.json`, so this skill's Measure-vs-Column guidance matches what a real rebuild would
    actually emit.

    A calc's own declared `role` field decides NOTHING here -- confirmed the hard way on this
    exact corpus workbook (WorkbookB Dashboard): tmdl.py's own docstring records a real failure
    where a field got emitted as a calculated COLUMN whose DAX referenced a parameter's MEASURE;
    the model loaded fine (no preflight check catches the unqualified `[Name]` shape a parameter
    reference uses), but every report filter built on that column then failed in Desktop with
    "Broken_Filters". The fixpoint below -- seed True for `is_aggregate(formula)` OR any
    `[Parameters].` reference, then propagate True to anything that references an already-True
    field -- is the actual fix tmdl.py ships, not a simplification of it.
    """
    agg = {tid: (is_aggregate(c["formula"] or "") or "[Parameters]." in (c["formula"] or ""))
           for tid, c in calc_by_id.items()}
    refs_of = {tid: _FIELDREF_RE.findall(c["formula"] or "") for tid, c in calc_by_id.items()}
    for _ in range(len(refs_of) + 1):
        grew = False
        for tid, refs in refs_of.items():
            if agg[tid]:
                continue
            for ref in refs:
                if agg.get(ref):
                    agg[tid] = True
                    grew = True
                    break
        if not grew:
            break
    return {calc_by_id[tid]["name"]: is_agg for tid, is_agg in agg.items()}


# The bare (unquoted) alternative is `\S+`, not `[A-Za-z_][A-Za-z0-9_]*` -- build-tableau-pbip's
# own quoting rule (names.py's `_NEEDS_QUOTE = re.compile(r"[.=:'\s]")`) only quotes a name
# containing `. = : '` or whitespace; a bare measure/column name is free to contain `%`, `-`, `#`,
# etc. Real corpus regression: `Dormancy_%_of_cards`/`issr-spend-l6`/`mkt-spend-l6` are genuinely
# live TMDL measures, but the old `[A-Za-z0-9_]*` alternative matched neither them nor their
# absent quotes, so they never landed in `existing_names` -- `resolve_prereq_chain` and
# `drop_already_emitted` both then treated a measure that already exists as still needing to be
# built, inflating "still outstanding" counts and telling a reader to recreate something already
# correct in the model.
_TMDL_COLUMN_HEADER_RE = re.compile(
    r"^\tcolumn\s+(?:'((?:[^']|'')*)'|(\S+))", re.MULTILINE)
_TMDL_MEASURE_HEADER_RE = re.compile(
    r"^\tmeasure\s+(?:'((?:[^']|'')*)'|(\S+))", re.MULTILINE)


def load_tmdl_existing_names(wb_dir):
    """{every measure and column name actually present in this workbook's own BUILT TMDL} --
    read directly off disk (every `*.SemanticModel/definition/tables/*.tmdl`), not inferred, so
    a dependency chain's "already exists" call reflects this specific build's real current
    state, not what should theoretically be there."""
    names = set()
    for sm_dir in glob.glob(os.path.join(wb_dir, "*.SemanticModel")):
        for tmdl_path in glob.glob(os.path.join(sm_dir, "definition", "tables", "*.tmdl")):
            try:
                text = open(tmdl_path, encoding="utf-8").read()
            except OSError:
                continue
            for rx in (_TMDL_COLUMN_HEADER_RE, _TMDL_MEASURE_HEADER_RE):
                for quoted, bare in rx.findall(text):
                    names.add(quoted.replace("''", "'") if quoted else bare)
    return names


def drop_already_emitted(wb_dir, residue):
    """Remove calc_residue entries whose object is NOW REALLY IN THE MODEL.

    `residue.json` is a frozen record of what the ORIGINAL build declined; it is never rewritten.
    That is correct as history and actively misleading as a to-do list once anything has been
    fixed by hand in Desktop -- on WorkbookB Dashboard, this document kept telling a reviewer to
    write DAX for 17 DATETRUNC and 10 STR fields that were already live in the TMDL. This check
    keeps every to-do list this skill produces (COPILOT_FIX_PROMPTS.md, EFFORT_REPORT.md) honest
    about what is actually still outstanding, whoever applied the earlier fix and however.

    Authority is `name_authority.json`'s `emitted` set (what was actually WRITTEN to TMDL), not
    the allocation maps -- see names.py's own "allocation is not emission". Returns a NEW residue
    dict; the file on disk is untouched.
    """
    work = os.path.join(wb_dir, "_work")
    try:
        with open(os.path.join(work, "ir.json"), encoding="utf-8") as fh:
            ir = json.load(fh)
        with open(os.path.join(work, "name_authority.json"), encoding="utf-8") as fh:
            na = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return residue, 0

    # (pbi table, calc caption) -> the calc's Tableau identity, which is how the allocation maps
    # are keyed (names.py's `fkey`). Must key by the RESOLVED PBI table name (`tables_map`), not
    # the raw Tableau caption/connection string ir.json's own `t["caption"]` carries -- those two
    # differ whenever `sanitize()` strips a trailing parenthetical or a name collision renames the
    # table (the exact same caption-vs-pbi_table gap validate_mapping.py's own build_mapping()
    # already documents). Using the raw caption here meant this function matched NOTHING for any
    # workbook where that gap applies -- silently reporting 0 already-fixed even after every fix
    # in this exact workbook had been applied and verified live in the TMDL.
    tables_map = na.get("tables", {})
    ident_of = {}
    for t in ir.get("tables", []):
        caption = t.get("caption") or t.get("tableau_name")
        pbi_table = tables_map.get(t.get("tableau_name"), caption)
        for c in t.get("calcs", []):
            ident_of[(pbi_table, c["name"])] = (c.get("tableau_name") or "").strip("[]") or c["name"]

    emitted = set(na.get("emitted", []))
    measures, columns = na.get("measures", {}), na.get("columns", {})

    kept, dropped = [], 0
    for item in residue.get("calc_residue", []):
        ident = ident_of.get((item["table"], item["subject"]))
        name = None
        if ident is not None:
            key = f"{item['table']} | {ident}"
            name = measures.get(key) or columns.get(key)
        if name is not None and f"{item['table']} | {name}" in emitted:
            dropped += 1
            continue
        kept.append(item)

    return {**residue, "calc_residue": kept}, dropped


def resolve_prereq_chain(subject, calc_by_id, calc_id_by_name, real_col_ids, param_by_id,
                          agg_by_name, existing_names, visible_subjects, max_depth=15):
    """Every field `subject`'s own Tableau formula needs -- directly or transitively -- before
    it will actually work, whether or not each one already has its own entry elsewhere in this
    document. This is the exact manual trace a 'root' cascade field's formula often forces by
    hand: on WorkbookB Dashboard's real `cl.Is Current Period - Lookups?`, its own residue.json
    entry names it as an independent "root" clearing 4 downstream fields, but ITS OWN formula
    transitively needs 4 MORE fields first (`cl.Current Period - Start/End Date`, `cl.Current
    Month Start/End Date`) that each already have their OWN separate entry elsewhere in this
    same document (as cascade roots in their own right) -- nothing on `cl.Is Current Period -
    Lookups?`'s own entry said so, because an earlier version of this function only flagged
    prerequisites with NO entry anywhere in the document, silently treating "has a doc entry" as
    "already fixed." A doc entry only means declined-and-explained, not built -- this walk
    always expands a prerequisite's own references too, regardless of whether it is visible or
    hidden elsewhere, and only stops at something ACTUALLY present in the built TMDL.

    Returns:
      needed_names: [name, ...] in dependency-first (buildable top-to-bottom) order -- EVERY
                    prerequisite, whether it has its own section elsewhere in this document or
                    not -- for the "Also needs" line on `subject`'s own entry.
      hidden:       [{"name","formula","kind"}, ...] -- only the NOT-YET-BUILT prerequisites
                    with no entry anywhere else in this document, for the global "Hidden
                    Prerequisite Fields" registry (no need to duplicate a formula the reader can
                    already find under its own heading, and no need to list something that's
                    already built and needs no action).
      param_notes, hit_cap: as before.
    Cycle-safe (visited set); depth-capped, and a capped chain says so rather than silently
    stopping short.
    """
    needed_names, hidden, param_notes, visited, hit_cap = [], [], [], set(), [False]
    seen_params = set()

    def walk(tid, depth):
        if tid in visited or depth > max_depth:
            if depth > max_depth:
                hit_cap[0] = True
            return
        visited.add(tid)
        calc = calc_by_id.get(tid)
        if calc is None:
            return
        param_ids, field_ids = _extract_refs(calc["formula"])
        for pid in param_ids:
            if pid in seen_params:
                continue
            seen_params.add(pid)
            p, param_status = lookup_param(pid, param_by_id)
            if param_status == "ambiguous":
                param_notes.append(
                    f"AMBIGUOUS parameter id '{pid}' -- multiple different saved values are "
                    f"recorded for this exact id in this workbook; verify by hand in the .twb "
                    f"before relying on it.")
            elif param_status == "unresolved":
                param_notes.append(
                    f"UNRESOLVED parameter id '{pid}' -- not recorded under this id (or its "
                    f"base id) anywhere in this workbook's extracted parameters; verify by hand "
                    f"in the .twb before relying on it.")
            else:
                alias_note = f" (via alias of base id, both share the same recorded value)" \
                    if param_status == "alias" else ""
                sel = f"{p['name']} Selected"
                status = "OK, already in the model" if sel in existing_names else (
                    "MISSING from the built model -- rebuild, or this parameter's own domain "
                    "resolution failed")
                param_notes.append(
                    f"parameter '{p['name']}' -> measure `[{sel}]`{alias_note} ({status})")
        for fid in field_ids:
            if fid in real_col_ids or fid not in calc_by_id:
                continue
            dep_name = calc_by_id[fid]["name"]
            if dep_name in existing_names:
                continue    # already built -- nothing to do, nothing further to trace
            walk(fid, depth + 1)      # a doc entry existing for it does not mean it's built
            if dep_name not in needed_names:
                needed_names.append(dep_name)
            if dep_name not in visible_subjects and not any(h["name"] == dep_name for h in hidden):
                hidden.append({
                    "name": dep_name,
                    "formula": calc_by_id[fid]["formula"],
                    "kind": "measure" if agg_by_name.get(dep_name) else "column",
                })

    tid0 = calc_id_by_name.get(subject)
    if tid0 is not None:
        walk(tid0, 0)
    return needed_names, hidden, param_notes, hit_cap[0]


def build_location_index(wb_dir):
    """Map a field caption to where it's actually placed in the Power BI report -- which
    worksheet(s)/page(s) reference it on a shelf or in a filter -- read from this workbook's own
    `_work/ir.json`. A field never found this way is not on any visual at all: it exists only as
    an intermediate step inside another calc's formula, so the fix belongs in the semantic model
    only (Power BI Desktop Model view / TMDL) and there is no page to open for it.

    Also returns `field_to_shelf`: {caption -> [(worksheet name, {"rows": [...], "cols": [...]})]}
    for every worksheet where this field is placed directly on the Rows/Cols/Pages shelf (from
    `extract_worksheets()`'s own `shelves` capture, a DIFFERENT list than `encodings` above --
    color/label/tooltip/detail encodings do not carry Tableau's row/column addressing, only the
    Rows/Cols/Pages shelves do). This is what `describe_table_calc()` uses to resolve a table
    calc's generic "Compute Using: Table (Down)/(Across)" addressing to the REAL field(s) on that
    shelf, for the one worksheet that actually shows this exact calc -- instead of sending a human
    to Tableau Desktop to look up something already sitting in this workbook's own ir.json.
    """
    path = os.path.join(wb_dir, "_work", "ir.json")
    field_to_ws, ws_to_pages, field_to_shelf = {}, {}, {}
    if not os.path.isfile(path):
        return field_to_ws, ws_to_pages, field_to_shelf
    try:
        with open(path, encoding="utf-8") as fh:
            ir = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return field_to_ws, ws_to_pages, field_to_shelf

    for db in ir.get("dashboards", []):
        for z in db.get("zones", []):
            if z.get("kind") == "worksheet" and z.get("name"):
                ws_to_pages.setdefault(z["name"], set()).add(db["name"])

    for ws in ir.get("worksheets", []):
        name = ws.get("name")
        for enc in ws.get("encodings") or []:
            field = enc.get("field")
            if field:
                field_to_ws.setdefault(field, set()).add((name, "visual"))
        for filt in ws.get("filters") or []:
            field = filt.get("field")
            if field:
                field_to_ws.setdefault(field, set()).add((name, "filter"))

        shelves = ws.get("shelves") or {}
        row_cols = {
            "rows": [r.get("field") for r in shelves.get("rows") or [] if r.get("field")],
            "cols": [r.get("field") for r in shelves.get("cols") or [] if r.get("field")],
        }
        placed_here = set(row_cols["rows"]) | set(row_cols["cols"])
        placed_here |= {r.get("field") for r in shelves.get("pages") or [] if r.get("field")}
        for field in placed_here:
            field_to_shelf.setdefault(field, []).append((name, row_cols))

    return field_to_ws, ws_to_pages, field_to_shelf


def field_kind_label(subject, agg_by_name):
    """'MEASURE' or 'COLUMN', per `compute_agg_map_by_name()` -- or a hedged fallback if this
    subject has no entry there (e.g. ir.json missing), since guessing wrong here is exactly the
    "Broken_Filters" hazard that fixpoint exists to prevent -- never silently default to one."""
    is_agg = agg_by_name.get(subject)
    if is_agg is None:
        return "MEASURE or COLUMN (could not verify -- _work/ir.json missing/unreadable; check which one this field's own references need)"
    return "MEASURE" if is_agg else "COLUMN"


def locate_field(subject, field_to_ws, ws_to_pages, kind_label, forced_mandatory_reason=None):
    """Return a one-line, beginner-friendly statement of where to make this fix, PREFIXED with
    an explicit `[MANDATORY]`/`[OPTIONAL]` tag -- the identical `field_has_report_impact()` verdict
    the "No Current Report Impact" cross-cutting section (further down the document) uses for the
    same field, so a reader skimming section A/B directly sees the same call that section makes,
    instead of having to notice the softer "Semantic model only" wording on its own or cross-check
    a separate list to tell a load-bearing fix apart from one that is safe to defer.

    `forced_mandatory_reason`: for an item `structural_item_is_mandatory()` promotes to mandatory
    despite having no shelf placement of its own (a relationship/join is never a shelf field to
    begin with) -- keeps this per-item line in agreement with that classification instead of
    contradicting it with the softer "Semantic model only... safe to defer" wording below.
    """
    hits = field_to_ws.get(subject)
    if not hits:
        if forced_mandatory_reason:
            return (
                f"**[MANDATORY]** **Create as a new {kind_label} in Model view first**. Not "
                f"placed on any worksheet shelf itself -- a relationship/join is never a shelf "
                f"field -- but treated as mandatory anyway because {forced_mandatory_reason}."
            )
        return (
            f"**[OPTIONAL -- No Current Report Impact]** **Semantic model only** -- not placed "
            f"on any visual or filter; it's an internal building block for another calc. Create "
            f"it as a new **{kind_label}** in Power BI Desktop's **Model view**. There is no "
            f"report page to open for this one; safe to defer unless something else needs it "
            f"directly."
        )
    pages = sorted({p for ws_name, _role in hits for p in ws_to_pages.get(ws_name, set())})
    ws_roles = sorted({f"{ws_name} ({role})" for ws_name, role in hits})
    if pages:
        return (
            f"**[MANDATORY]** **Create as a new {kind_label} in Model view first**, **then "
            f"check it on page(s):** {', '.join(f'`{p}`' for p in pages)} -- worksheet(s): "
            f"{', '.join(ws_roles)}."
        )
    return (
        f"**[MANDATORY]** **Create as a new {kind_label} in Model view first**. Used on "
        f"worksheet(s) {', '.join(ws_roles)}, but no page/dashboard mapping was found for "
        f"those -- check Power BI Desktop's page list directly."
    )


def field_has_report_impact(subject, field_to_ws):
    """True if `subject` is placed on any real worksheet visual/filter anywhere in this
    workbook, per `build_location_index()`'s own `field_to_ws` map -- the exact same check
    `locate_field()` uses internally to decide between "Semantic model only" and a real page
    pointer. Kept as its own named function (rather than inlining `bool(field_to_ws.get(...))`
    at each call site) so the "No Current Report Impact" cross-cutting section is built from
    the identical rule every per-field entry already states, not a second, potentially
    diverging definition of "used"."""
    return bool(field_to_ws.get(subject))


def structural_item_is_mandatory(item, field_to_ws):
    """True if a structural/model-design item's own placement says so (`field_has_report_impact`
    on its `subject`), OR its decline reason describes a correctness risk regardless of
    placement -- an unmodeled join collapsed into one flattened table (tmdl.py emits two
    wordings for this: "physical-layer join (...)" for a physical `<relation type='join'>`, and
    "key-based join (...) inside a federated datasource that is extracted as a single table" for
    an object-graph `<relationship>` -- checked against the real Bookshop Data Analysis
    workbook, which has both) risks silently inflating every SUM/COUNT built on that table the
    moment the join turns out to be one-to-many, EVEN if Tableau declared a unique side (a known
    one-to-many join flattened alongside another one-to-many join off the same hub still
    cross-multiplies into a combinatorial row explosion -- see this reason's own "What's needed"
    text). This risk exists whether or not the JOIN ITSELF sits on any shelf -- a join is never a
    field a Tableau author could place on a shelf in the first place, so
    `field_has_report_impact` on its synthetic `subject` name almost always says "optional" no
    matter how central the flattened table actually is to the report -- exactly the mismatch this
    override exists to correct. Real corpus workbooks with this shape (Bookshop Data Analysis, 9
    object-graph joins; PVT Monthly Report) had every one of these join items landing in the
    OPTIONAL file before this override, despite the fan-out risk being real and unconditional."""
    reason = (item.get("reason") or "")
    if reason.startswith("physical-layer join") or reason.startswith("key-based join"):
        return True
    return field_has_report_impact(item.get("subject", ""), field_to_ws)


def split_cascade(items):
    """Separate pass-through 'depends on X' entries (declined ONLY because X was already
    declined) from genuine root-cause failures. Without this split, every cascade entry --
    regardless of which of many different roots it actually depends on -- collapsed into one
    FIX_RULES group that showed just the FIRST item's root/reason as if it applied to all of
    them, which misrepresents the other roots entirely."""
    roots, cascade = [], []
    for it in items:
        m = _DEP_RE.match(it.get("reason") or "")
        if m:
            cascade.append((m.group(1), it))
        else:
            roots.append(it)
    return roots, cascade


def group_cascade_by_root(cascade, ir_index):
    """Group cascade (root_id, item) pairs by their actual root, preserving first-seen order."""
    groups = OrderedDict()
    for root_id, it in cascade:
        if root_id not in groups:
            info = ir_index.get(root_id)
            groups[root_id] = {
                "friendly_name": info["name"] if info else root_id,
                "formula": info["formula"] if info else None,
                "downstream": [],
            }
        groups[root_id]["downstream"].append(it)
    return groups


def split_dax_vs_structural(calc_residue):
    """Only an item with a captured Tableau formula is a DAX-writing task. Items with no formula
    (physical-layer joins, free-entry parameter domains, etc.) are relationship/model-design gaps
    that Copilot's DAX assistant cannot fix by writing a measure -- verified against every
    residue.json in this repo's output/: no GAP-tier item ever carries a formula, and no item
    carrying a formula is ever GAP-tier, so this split never misclassifies in practice."""
    dax_items = [it for it in calc_residue if it.get("formula")]
    structural_items = [it for it in calc_residue if not it.get("formula")]
    return dedupe_items(dax_items), dedupe_items(structural_items)


def group_items(items):
    """Group items by FIX_RULES rule index, or normalized reason if no rule fired.
    Returns an OrderedDict preserving first-seen order: key -> {"fix_text": str, "items": [...]}."""
    groups = OrderedDict()
    for item in items:
        reason = item.get("reason", "")
        idx = rule_index(reason)
        key = ("rule", idx) if idx is not None else ("generic", normalize_reason(reason))
        if key not in groups:
            groups[key] = {"fix_text": suggest_fix(reason), "items": []}
        groups[key]["items"].append(item)
    return groups


def split_by_recommendation(groups):
    """Split a `group_items()` result into (recommended, no_recommendation), preserving order
    in each. "Recommended" means FIX_RULES actually matched this reason and produced real
    guidance -- still needs human review before pasting into Desktop, it is a starting point,
    not a verified-correct answer for this exact workbook's real names. "No recommendation"
    means the reason fell through to GENERIC_FIX -- nobody has taught this tool a fix pattern
    for it yet, so the SME gets the raw problem plus enough context to investigate by hand,
    rather than a made-up suggestion."""
    recommended, no_rec = OrderedDict(), OrderedDict()
    for key, g in groups.items():
        (no_rec if g["fix_text"] == GENERIC_FIX else recommended)[key] = g
    return recommended, no_rec


SME_HINT_BLOCK = """\
**No automated recommendation exists for this pattern yet.** Diagnose and fix it yourself:
1. Start from the "Tableau metadata background" above -- the exact reason and (if captured) the
   original Tableau formula are your starting point, not a guess.
2. Open the original `.twb`/`.twbx` in Tableau Desktop and find this exact field/join/item by
   name, to see how it actually behaves with real data before deciding on a Power BI equivalent.
3. Check this workbook's own built model (`*.SemanticModel/definition/tables/*.tmdl`) for the
   real table and column names already in the model -- reference those, don't guess a name that
   might not exist; Desktop's autocomplete in the formula bar will confirm what's real.
4. If you find a fix that would generalize to other workbooks with the same reason text,
   consider adding a rule to `FIX_RULES` in
   `validate-tableau-pbip-mapping/scripts/validate_mapping.py` -- the next build of any workbook
   hitting this same pattern gets your fix automatically instead of landing back in this
   no-recommendation section.\
"""


def group_label(items, resolved_reason=None):
    """Short human title for a group, derived from the first item's own reason text.

    Pass `resolved_reason` (from `resolve_formula_display`) when the caller already resolved
    Tableau internal ids to real names in that reason string, so the group heading doesn't
    show a raw `Calculation_...` id even though the body text below it does not either.
    """
    reason = resolved_reason if resolved_reason is not None else items[0].get("reason", "")
    label = re.split(r" -- | — | \(| ; | : ", reason, maxsplit=1)[0]
    if len(label) > 90:
        cut = label[:90].rsplit(" ", 1)[0]
        label = (cut or label[:90]) + "..."
    return label.strip() or "Uncategorized gap"


# ============================================================== prompt synthesis
COPILOT_PROMPT_TEMPLATE = """\
I'm rebuilding a Tableau dashboard as a Power BI semantic model. The Tableau calculated field(s)
below could not be automatically translated to DAX and were intentionally left out rather than
guessed at. Please write the DAX for them.

Tableau field(s) affected in this workbook:
{fields_block}

Why automated translation declined this pattern:
{reason}

What the DAX needs to do:
{fix_text}

Ask me for the real table and column names in this semantic model if you are not sure of them --
do not guess at a name that might not exist.\
"""


def build_dax_prompt(fix_text, reason, items, calc_by_id=None, real_col_ids=None, param_by_id=None):
    fields_block = "\n".join(
        "- \"{subject}\" (table: {table}, tier: {tier})\n  Tableau formula: {formula}".format(
            subject=it.get("subject", "?"),
            table=it.get("table", "?"),
            tier=it.get("tier", "?"),
            formula=resolve_formula_display(
                it.get("formula"), calc_by_id or {}, real_col_ids or {}, param_by_id or {}
            ) or "(no formula captured)",
        )
        for it in items
    )
    return COPILOT_PROMPT_TEMPLATE.format(fields_block=fields_block, reason=reason, fix_text=fix_text)


STRUCTURAL_PROMPT_TEMPLATE = """\
I'm rebuilding a Tableau dashboard as a Power BI semantic model. The item(s) below are NOT a
calculated field/DAX gap -- they are a Tableau relationship, parameter, or model-design construct
that automated translation could not carry over.

Item(s) affected in this workbook:
{fields_block}

Why automated translation declined this:
{reason}

What I need help with:
{fix_text}

If this needs a Power Query M step, write that; if it needs a modeling change (relationships,
parameters, slicers), tell me exactly what to change in Power BI Desktop's Model view. Ask me for
the real table and column names in this semantic model if you are not sure of them.\
"""


def build_structural_prompt(fix_text, reason, items):
    fields_block = "\n".join(
        "- \"{subject}\" (table: {table})".format(subject=it.get("subject", "?"), table=it.get("table", "?"))
        for it in items
    )
    return STRUCTURAL_PROMPT_TEMPLATE.format(fields_block=fields_block, reason=reason, fix_text=fix_text)


DESKTOP_WALKTHROUGH = """\
## How to Apply a Fix in Power BI Desktop (Step-by-Step)

This workbook is a **PBIP project** (a folder with a `.Report` and `.SemanticModel` subfolder,
not a single `.pbix` file), so opening and editing it needs two one-time settings turned on.

**0. One-time setup (skip if already done):**
1. Open Power BI Desktop -> **File > Options and settings > Options > Preview features**.
2. Tick **"Power BI Project (.pbip) save option"** and **"Store semantic model using TMDL
   format"**. Click OK and restart Desktop if it asks you to.

**1. Open the project:** File > Open report > browse to this workbook's folder > select the
`.pbip` file (not a `.pbix`). Desktop opens the Report and Model together.

**2. Switch to Model view:** click the diagram/relationships icon on the far-left vertical rail
(it's below the Report and Data icons). This is where every DAX fix below happens -- not on a
report page.

**3. Create the field (it does not exist in the model yet -- you're creating it, not editing
something broken). Every field's own entry below tells you which of the two to pick -- read that
line before you start, do not assume:**
1. In the **Data** pane on the right, find the **table** named in the field's entry below (e.g.
   `DS_WorkbookB Extract | Project : Data Sources`) and click it once to select it.
2. On the ribbon, go to the **Table tools** tab (or right-click the table name) and choose
   **New measure** or **New column** -- exactly as the field's own entry says.
3. A formula bar opens at the top of the canvas. Type:
   `FieldName = <the DAX Copilot gave you>`
   -- using the exact field name from "Affected fields" below as `FieldName` (wrap it in single
   quotes if it has special characters). Press **Enter** to commit, or **Esc** to discard.
4. Autocomplete will suggest real column/measure names as you type `'Table Name'[` -- use those
   suggestions rather than typing names by hand, so you don't introduce a typo.

**Why this document tells you Measure vs. Column per field, instead of "usually a measure":** on
this exact workbook, picking wrong is not cosmetic. A field that should be a calculated COLUMN
but references a Tableau `[Parameters].[...]` value (which always resolves to a MEASURE in this
model) loads fine in Desktop -- no error at open -- but every report filter built on that column
then silently fails with **"Broken_Filters"**. The determination below reproduces the exact rule
build-tableau-pbip's own tmdl.py uses to decide Measure vs. Column, so trust the field's own
entry over any general rule of thumb, including the general guidance in this paragraph.

**4. Check for prerequisites BEFORE you create a field.** Some fields reference other fields that
were never declined on their own (they never sit on a shelf, so this tool never saw a reason to
flag them) -- if the field's entry lists an **"Also needs"** or points you at **'Hidden
Prerequisite Fields'**, create every one of those first, in the order given, or your paste will
reference a name that doesn't exist yet.

**5. Verify it worked:** switch to **Report view** (top icon on the left rail), open the page(s)
named in the field's entry below, and confirm the visual that uses this field now shows values
instead of being blank. If a field's entry says "Semantic model only", there is no page to check
-- it's an internal building block for another field; you'll see the *effect* of fixing it on
whatever else depends on it instead.

**6. Save:** Ctrl+S. Because this is a PBIP project, saving writes directly back to the `.tmdl`
files on disk in this workbook's `.SemanticModel/definition/tables/` folder -- there is no
separate "publish" or rebuild step needed for your fix to take effect in Desktop.

**For "Structural / Model-Design Gaps" items (relationships, parameters) instead of DAX:**
those are also fixed in Model view, but via different dialogs -- **Manage relationships** (on
the ribbon) for joins, and the **Fields** pane's parameter/what-if setup for slicers. Copilot's
answer for these will tell you which dialog and what to set; there is no formula bar involved.

"""


def build_visual_lines(items):
    return [
        "- Worksheet \"{ws}\" (page {page}): mark='{mark}', emitted as '{emitted}' -- {reason}".format(
            ws=it.get("worksheet", "?"),
            page=it.get("page", "?"),
            mark=it.get("mark", "?"),
            emitted=it.get("emitted", "?"),
            reason=it.get("reason", ""),
        )
        for it in items
    ]


def build_zone_lines(items):
    return [
        "- Zone \"{zone}\" (page {page}): {reason}".format(
            zone=it.get("zone", "?"), page=it.get("page", "?"), reason=it.get("reason", "")
        )
        for it in items
    ]


def build_encoding_lines(items):
    """`declined_encodings` items: a Legend/Series/Size role that Power BI's own visual schema
    rejects outright, or a measure used where Tableau's own semantics need a dimension. Each one
    is a LIVE, currently-rendering visual with a stated defect (its axis/values still work; only
    the legend/size split is missing) -- not a translation gap in the DAX sense, so there is no
    "recommended DAX" for it the way a calc gap gets; the fix is a Power BI Desktop authoring
    decision (a different visual type, a different field role, or accepting the limitation)."""
    return [
        "- Visual \"{visual}\" (page {page}), field `{field}`: {reason}".format(
            visual=it.get("visual", "?"), page=it.get("page", "?"),
            field=it.get("field", "?"), reason=it.get("reason", "")
        )
        for it in items
    ]


def build_blend_key_lines(items):
    """`blend_key_gaps` items (see effort_report.py's own `load_blend_key_gaps`): a Blend Key
    many-to-many relationship whose two tables' real data has ZERO overlapping key values,
    discovered post-data by validate-blend-key-domains. Not a DAX gap -- there is no formula to
    write; the fix is a Desktop model-design decision (re-key, bridge table, or accept the
    limitation)."""
    return [
        "- Table \"{table}\": {subject} -- {reason}".format(
            table=it.get("table", "?"), subject=it.get("subject", "?"),
            reason=it.get("reason", "")
        )
        for it in items
    ]


def describe_refs(formula, calc_by_id, real_col_ids, param_by_id, existing_names,
                   visible_subjects, hidden_names):
    """Short, SELF-CONTAINED description of every other field a formula references, categorized
    so a reader can tell what's safe to use as-is vs. what still needs building, without having
    to cross-reference anything else in the document."""
    param_ids, field_ids = _extract_refs(formula)
    out = []
    seen = set()
    for fid in field_ids:
        if fid in real_col_ids:
            col_name = real_col_ids[fid]
            if col_name not in seen:
                seen.add(col_name)
                out.append(f"`{col_name}` -- a real column, always available")
            continue
        calc = calc_by_id.get(fid)
        if calc is None:
            continue
        name = calc["name"]
        if name in seen:
            continue
        seen.add(name)
        if name in existing_names:
            out.append(f"`{name}` -- already built in this model")
        elif name in hidden_names:
            out.append(f"`{name}` -- see 'Hidden Prerequisite Fields' below")
        elif name in visible_subjects:
            out.append(f"`{name}` -- see its own entry elsewhere in this document")
        else:
            out.append(f"`{name}` -- not resolved anywhere in this document; verify by hand")
    seen_params = set()
    for pid in param_ids:
        if pid in seen_params:
            continue
        seen_params.add(pid)
        p, param_status = lookup_param(pid, param_by_id)
        if param_status == "ambiguous":
            out.append(f"parameter id '{pid}' -- AMBIGUOUS, multiple saved values recorded "
                       f"for this id; verify by hand")
        elif param_status == "unresolved":
            out.append(f"parameter id '{pid}' -- UNRESOLVED, not recorded under this id (or "
                       f"its base id) anywhere in this workbook; verify by hand")
        else:
            sel = f"{p['name']} Selected"
            status = "already in the model" if sel in existing_names else "MISSING from the built model"
            out.append(f"parameter `[{sel}]` ({status})")
    return out


_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


def resolve_formula_display(formula, calc_by_id, real_col_ids, param_by_id):
    """A human-readable copy of a Tableau formula, with every bracketed internal identity
    swapped for the real Power BI name a reader would actually see in Desktop's autocomplete.

    Every formula in residue.json is Tableau's OWN raw text, unedited -- and Tableau formulas
    reference other fields by whichever spelling was current when the calc was AUTHORED: often
    an internal id (`[Calculation_891431257683640320]`), sometimes a lowercase/snake_case source
    column (`[approved_auth_transaction_count]`), because a calc's/column's DISPLAY name in
    Tableau's UI and its own internal identity are two different strings from the moment it's
    created (see extract.py's own `tableau_name` vs `name` split). This pipeline already resolves
    that split when it EMITS a field's own DAX -- but every prior version of this document showed
    formulas VERBATIM, so a reader who typed `SUM([Calculation_891431257683640320])` literally,
    as directed, hit Desktop's "Column ... cannot be found" the moment they tried it: that string
    was never a real column OR measure name in the model, just an internal id that happened to
    sit inside a formula the reader was told to trust as-is. Confirmed against a real corpus
    workbook (LAC Pedidosya Transaction Profile) reproducing exactly that error.

    `[Parameters].[X]` is resolved first and separately (X is a parameter id, not a field id, and
    lives in a different namespace) so a param id that coincidentally collides with a field id
    elsewhere in the same workbook is never misresolved. Anything that doesn't resolve --  a
    literal like `[NULL]`, a dangling reference to a field that was never extracted at all, an
    ambiguous parameter id -- is left exactly as written; this never invents a name, it only
    substitutes ones already confirmed to exist in this workbook's own `_work/ir.json`.
    """
    if not formula:
        return formula

    def _param(m):
        pid = m.group(1)
        p, _status = lookup_param(pid, param_by_id)
        return f"[Parameters].[{p['name']}]" if p else m.group(0)

    resolved = _PARAM_REF_RE.sub(_param, formula)

    def _field(m):
        fid = m.group(1)
        if fid in real_col_ids:
            return f"[{real_col_ids[fid]}]"
        calc = calc_by_id.get(fid)
        if calc:
            return f"[{calc['name']}]"
        return m.group(0)

    return _BRACKET_RE.sub(_field, resolved)


_ORDERING_TYPE_ENGLISH = {
    "Table": "Table (the whole visual)",
    "Pane": "Pane",
    "Cell": "Cell",
    "Rows": "Table (Down)",
    "Columns": "Table (Across)",
}


def describe_table_calc(tc_spec, subject, field_to_shelf):
    """A human-readable rendering of a table calculation's OWN `<table-calc>` XML -- its real
    Tableau "Compute Using" addressing -- read from this workbook's own `_work/ir.json`
    (`extract.py`'s `table_calc_spec()`), instead of the bare boilerplate reason
    ("addressing/partitioning is not in the formula text") that reason is true about the FORMULA
    STRING but not about the WORKBOOK: Tableau stores this on the calc's `<table-calc>` element
    (and, for "Specific Dimensions" addressing, on `<order field=...>` children), not inside the
    formula. This never invents a value -- when this workbook's own worksheets never place the
    field on a Rows/Cols/Pages shelf either, it says so plainly instead of guessing.

    Three out of four cases here are unambiguous, concrete fixes:
      - `order_fields` non-empty (Compute Using: Specific Dimension(s)) -- the real field names,
        already resolved by extract.py against this workbook's own name map, from the calc's OWN
        `<table-calc><order field=...>` children.
      - `ordering_type == "Field"` with a raw `ordering_field` extract.py could not resolve --
        still a concrete field, just given as the raw workbook reference.
      - `ws_order_fields` non-empty -- the calc's own definition carries no addressing at all, but
        a WORKSHEET's own saved table-calc setting for this exact calc does (Tableau keeps this as
        a second, separate copy under `<worksheet>...<column-instance><table-calc
        ordering-field=...>`, populated whenever a calc is used inside a worksheet without sitting
        directly on a shelf -- see `extract.py`'s `worksheet_tc_addressing()`). Real corpus example
        (Digital Sales - Leads Dashboard's `Rank - Text`): the calc's own definition is a bare
        `ordering-type='Rows'`, but worksheet `Leads Team View` saves `ordering-field='lead_owner'`
        for it -- a real, resolved field extract.py used to have no code path to see at all.
    The fourth case (`Rows`/`Columns`/`Table`/`Pane`/`Cell`, Tableau's GENERIC table-relative
    addressing, with no field recorded anywhere above either) is only resolvable by checking which
    worksheet places this exact field on a Rows/Cols/Pages SHELF (via `field_to_shelf`, from
    `build_location_index()`) -- when that ALSO comes up empty, this really is undetermined in the
    SOURCE WORKBOOK, not merely unextracted: opening Tableau Desktop would show the identical
    generic "Compute Using" setting, not a resolved field, because Tableau itself never pinned one
    down for this calc either.
    """
    if not tc_spec:
        return None
    kind_bits = []
    if tc_spec.get("type"):
        kind_bits.append(f"type={tc_spec['type']}")
    if tc_spec.get("rank_options"):
        kind_bits.append(f"rank_options={tc_spec['rank_options']}")
    if tc_spec.get("diff_options"):
        kind_bits.append(f"diff_options={tc_spec['diff_options']}")
    header = ("Tableau's own `<table-calc>` config for this field: " + ", ".join(kind_bits)) \
        if kind_bits else \
        "Tableau's own `<table-calc>` config for this field carries no explicit `type` (a plain running-total-style calc by default)."

    ot = tc_spec.get("ordering_type")
    ofs = tc_spec.get("order_fields") or []
    of = tc_spec.get("ordering_field")
    wsf = tc_spec.get("ws_order_fields") or []
    addr = None
    if ofs:
        plural = "this field" if len(ofs) == 1 else "these fields"
        addr = (f"**Compute Using: Specific Dimension(s)** -- {', '.join(f'`{n}`' for n in ofs)}. "
                f"This IS a concrete addressing already in the workbook; hand-author with an "
                f"`ALLEXCEPT`/`RANKX`/window-style DAX pattern partitioned/ordered by {plural}.")
    elif ot == "Field" and of:
        addr = (f"**Compute Using: a specific field** -- raw workbook reference `{of}` (this "
                f"pipeline could not resolve it to a display name against this workbook's own "
                f"model; the id itself confirms this IS field-based addressing, not generic).")
    elif wsf:
        names = sorted({d["field"] for d in wsf})
        ws_list = sorted({d["worksheet"] for d in wsf})
        plural = "this field" if len(names) == 1 else "these fields"
        addr = (f"**Compute Using: Specific Dimension(s)** -- {', '.join(f'`{n}`' for n in names)}. "
                f"This calc's OWN `<table-calc>` definition carries no addressing, but worksheet(s) "
                f"{', '.join(f'`{w}`' for w in ws_list)} save a separate, more specific table-calc "
                f"setting for this exact calc (Tableau does this whenever a calc is used inside a "
                f"worksheet without sitting directly on a Rows/Cols/Pages shelf). This IS a concrete "
                f"addressing already in the workbook; hand-author with an "
                f"`ALLEXCEPT`/`RANKX`/window-style DAX pattern partitioned/ordered by {plural}.")
    elif ot in _ORDERING_TYPE_ENGLISH:
        english = _ORDERING_TYPE_ENGLISH[ot]
        hits = field_to_shelf.get(subject) or []
        if hits:
            listing = "; ".join(
                f"worksheet `{ws}` -- Rows: {', '.join(rc['rows']) or '(none)'}, "
                f"Cols: {', '.join(rc['cols']) or '(none)'}"
                for ws, rc in hits)
            addr = (f"**Compute Using: {english}** -- resolved from the worksheet(s) that place "
                    f"this exact field on a shelf: {listing}. Hand-author with an "
                    f"`ALLEXCEPT`/window-style DAX pattern using those field(s).")
        else:
            addr = (f"**Compute Using: {english}** -- Tableau's generic, worksheet-relative "
                    f"addressing (not a fixed field). Checked every worksheet in this workbook: "
                    f"this field is never placed directly on a Rows/Cols/Pages shelf anywhere -- "
                    f"it's only ever referenced from inside another calc's formula, so there is no "
                    f"worksheet to resolve a concrete field from. Opening Tableau Desktop's own "
                    f"'Edit Table Calculation' dialog for this field would show this identical "
                    f"generic setting, not a resolved field, because Tableau itself never pinned "
                    f"one down for this calc -- this is genuinely undetermined in the source "
                    f"workbook, not merely unextracted by this pipeline.")
    if tc_spec.get("level_break"):
        addr = (addr or "") + f"\n  Partition break field: `{tc_spec['level_break']}`"
    return header + ("\n  " + addr if addr else "")


# ============================================================== rendering
def render_markdown(workbook_name, residue, ir_index=None, field_to_ws=None, ws_to_pages=None,
                     agg_by_name=None, calc_by_id=None, calc_id_by_name=None, real_col_ids=None,
                     param_by_id=None, existing_names=None, field_to_shelf=None,
                     impact_filter=None):
    """impact_filter: None renders everything together (original combined document, kept for
    the self-tests). "mandatory" or "optional" renders only the subset matching
    field_has_report_impact() -- used to produce the two split COPILOT_FIX_PROMPTS files, so a
    reader working the mandatory file never has to eyeball each entry's own impact note."""
    field_to_ws = field_to_ws or {}
    ws_to_pages = ws_to_pages or {}
    agg_by_name = agg_by_name or {}
    calc_by_id = calc_by_id or {}
    calc_id_by_name = calc_id_by_name or {}
    real_col_ids = real_col_ids or {}
    param_by_id = param_by_id or {}
    existing_names = existing_names or set()
    field_to_shelf = field_to_shelf or {}

    calc_residue_raw = residue.get("calc_residue", [])
    visual_undecided = residue.get("visual_undecided", [])
    zone_gaps = residue.get("zone_gaps", [])
    declined_encodings = residue.get("declined_encodings", [])
    # Discovered POST-data by validate-blend-key-domains (Agent 4), unlike the three lists
    # above, which _work/residue.json freezes at build time -- see main()'s own sibling-file
    # merge for where this key gets populated before render_markdown ever sees it.
    blend_key_gaps = residue.get("blend_key_gaps", [])
    dax_items, structural_items = split_dax_vs_structural(calc_residue_raw)
    dax_root_items, dax_cascade = split_cascade(dax_items)
    cascade_groups = group_cascade_by_root(dax_cascade, ir_index or {})

    if impact_filter == "mandatory":
        keep = lambda subj: field_has_report_impact(subj, field_to_ws)
        keep_structural = lambda it: structural_item_is_mandatory(it, field_to_ws)
    elif impact_filter == "optional":
        keep = lambda subj: not field_has_report_impact(subj, field_to_ws)
        keep_structural = lambda it: not structural_item_is_mandatory(it, field_to_ws)
    else:
        keep = keep_structural = None
    if keep is not None:
        dax_root_items = [it for it in dax_root_items if keep(it.get("subject", ""))]
        structural_items = [it for it in structural_items if keep_structural(it)]
        cascade_groups = OrderedDict(
            (rid, g) for rid, g in cascade_groups.items() if keep(g["friendly_name"]))
        dax_cascade = [it for g in cascade_groups.values() for it in g["downstream"]]
        dax_items = dax_root_items + dax_cascade
        # Only report-facing gaps -- always shown in the mandatory file, never split by impact,
        # and dropped from the optional file so it isn't implied any of these are safe to skip.
        if impact_filter == "optional":
            visual_undecided, zone_gaps, declined_encodings, blend_key_gaps = [], [], [], []

    n_roots = len(dax_root_items) + len(structural_items)

    # ---- prerequisite chains: everything a field's OWN formula transitively needs, whether or
    # not each prerequisite already has its own entry elsewhere in this document -- see
    # resolve_prereq_chain()'s docstring for why "has a doc entry" != "already built". ----
    visible_subjects = {it.get("subject", "") for it in dax_items}
    hidden_registry = OrderedDict()
    field_all_needs, field_param_notes = {}, {}
    any_hit_cap = False
    start_names = [it.get("subject", "") for it in dax_items]
    start_names += [g["friendly_name"] for g in cascade_groups.values()]
    for name in start_names:
        needed_names, hidden, param_notes, hit_cap = resolve_prereq_chain(
            name, calc_by_id, calc_id_by_name, real_col_ids, param_by_id, agg_by_name,
            existing_names, visible_subjects)
        if needed_names:
            field_all_needs[name] = needed_names
        for h in hidden:
            hidden_registry.setdefault(h["name"], h)
        if param_notes:
            field_param_notes[name] = param_notes
        any_hit_cap = any_hit_cap or hit_cap
    hidden_names = set(hidden_registry.keys())

    # Cross-cutting "no current report impact" list -- populated as each section below is
    # built, from the SAME per-field placement fact each entry already states individually
    # (field_has_report_impact), just gathered in one place. See the "No Current Report
    # Impact" section render below for why this exists.
    no_impact_entries = []

    title_suffix = {"mandatory": " -- Mandatory (Has Report Impact)",
                     "optional": " -- Optional (No Current Report Impact)"}.get(impact_filter, "")
    lines = [f"# Copilot Fix Prompts -- {workbook_name}{title_suffix}", ""]
    if impact_filter == "mandatory":
        lines.append(
            "This file covers only fields/items placed on a live worksheet visual or filter "
            "somewhere in this workbook -- fix these first. Everything with no current report "
            "impact is in the companion `COPILOT_FIX_PROMPTS_OPTIONAL.md` instead."
        )
        lines.append("")
    elif impact_filter == "optional":
        lines.append(
            "This file covers only fields/items with **no current report impact** -- internal "
            "building blocks another calc references, or truly orphaned fields. Safe to defer; "
            "fix the companion `COPILOT_FIX_PROMPTS_MANDATORY.md` first."
        )
        lines.append("")
    dedup_note = ""
    if impact_filter is None and len(dax_items) + len(structural_items) != len(calc_residue_raw):
        dedup_note = (
            f" ({len(calc_residue_raw)} record(s) in residue.json before de-duplication)"
        )
    lines.append(
        f"{len(dax_items)} declined calculated field(s) needing DAX{dedup_note}, "
        f"{len(structural_items)} structural/model-design gap(s) (not DAX), "
        f"{len(visual_undecided)} undecided visual(s), {len(zone_gaps)} dropped zone(s)/visual(s), "
        f"{len(declined_encodings)} color/size encoding(s) not reproduced, "
        f"{len(blend_key_gaps)} Blend Key relationship(s) with zero real-data overlap "
        f"-- generated by generate-copilot-fix-prompts from this workbook's own "
        f"`_work/residue.json`."
    )
    if dax_cascade:
        lines.append(
            f"\n**Fix-effort note:** of the {len(dax_items)} DAX gaps above, only "
            f"{len(dax_root_items)} are root causes with their own problem; the other "
            f"{len(dax_cascade)} are declined *only* because they reference one of "
            f"{len(cascade_groups)} already-declined root field(s) -- fixing each root clears "
            f"every field that depends on it, for free. Effective root-cause count across this "
            f"whole workbook: {n_roots} (see 'Cascading Dependencies' below)."
        )
    lines.append("")

    lines.extend(DESKTOP_WALKTHROUGH.splitlines())

    lines.append("## Suggested Order to Work Through This Document")
    lines.append("")
    lines.append(
        "If you're new to Power BI: every fix below except 'Structural / Model-Design Gaps' "
        "is a **DAX measure or calculated column**, which you write in Power BI Desktop's "
        "**Model view** (or the `.tmdl` file directly, if you're comfortable with that) -- "
        "never on a report page directly. A field only has a report *page* to check once its "
        "DAX is correct and you want to see it render. Work in this order:"
    )
    lines.append("")
    lines.append(
        "1. **Cascading Dependencies** (below) -- fix these roots first. Each one you fix "
        "clears multiple downstream fields for free on the next rebuild, so this section has "
        "the best return per fix and should always come first."
    )
    lines.append(
        "2. **DAX Measures to Fix** -- the remaining fields with their own, independent "
        "problem. Each field's entry says where (if anywhere) it appears on a report page, so "
        "you can batch fixes by page if you'd rather work page-by-page than pattern-by-pattern."
    )
    lines.append(
        "3. **Structural / Model-Design Gaps** -- relationships, parameters, and other "
        "non-DAX model changes. These don't block the DAX fixes above, so they can be done "
        "any time, but doing them last keeps you from context-switching out of DAX mode."
    )
    lines.append(
        "4. **Other Metadata Gaps** (visual types, dropped zones) -- check these *last*, after "
        "rebuilding. Most self-resolve automatically once the fields above are fixed."
    )
    lines.append(
        "5. **No Current Report Impact** (further below, after the Structural section) -- a "
        "cross-cutting list gathered from every section above, of fields with no live report "
        "impact today. Safe to skip on a first pass; nothing currently visible in the report "
        "depends on these."
    )
    lines.append("")

    if cascade_groups:
        lines.append("## Cascading Dependencies (fix the root, not each downstream field)")
        lines.append("")
        lines.append(
            f"{len(dax_cascade)} DAX gap(s) below are declined only because they reference one "
            f"of the {len(cascade_groups)} root field(s) here, which was itself already "
            f"declined. These roots are usually internal calcs that never sit directly on a "
            f"worksheet shelf, so they never earn their own residue.json entry -- their name and "
            f"formula here were recovered from this workbook's own `_work/ir.json`. Fix the root "
            f"and every field listed under it should resolve on the next rebuild without "
            f"separate Copilot work."
        )
        lines.append("")
        for i, (root_id, g) in enumerate(cascade_groups.items(), 1):
            downstream = g["downstream"]
            n = len(downstream)
            lines.append(
                f"### {i}. Root: `{g['friendly_name']}` ({root_id}) -- blocks "
                f"{n} downstream field{'s' if n != 1 else ''}"
            )
            lines.append("")
            if g["formula"]:
                resolved_formula = resolve_formula_display(
                    g["formula"], calc_by_id, real_col_ids, param_by_id)
                lines.append(f"**Root's own Tableau formula:** `{resolved_formula}`")
            else:
                lines.append(
                    "**Root's own Tableau formula:** not found in `_work/ir.json` -- may live "
                    "in a sibling datasource not captured there; check the .twb directly."
                )
            lines.append("")
            kind_label = field_kind_label(g["friendly_name"], agg_by_name)
            lines.append(locate_field(g["friendly_name"], field_to_ws, ws_to_pages, kind_label))
            lines.append("")
            if not field_has_report_impact(g["friendly_name"], field_to_ws):
                no_impact_entries.append({
                    "name": g["friendly_name"], "table": None, "tier": None,
                    "ref": f"Cascading Dependencies root #{i}",
                })
            needs = field_all_needs.get(g["friendly_name"])
            if needs:
                lines.append(
                    "**This root's OWN formula also needs (create these FIRST, in order -- "
                    "each has its own entry either elsewhere in this document or under "
                    "'Hidden Prerequisite Fields' below):** "
                    + ", ".join(f"`{n}`" for n in needs)
                )
                lines.append("")
            for note in field_param_notes.get(g["friendly_name"], []):
                lines.append(f"**Parameter reference:** {note}")
            if field_param_notes.get(g["friendly_name"]):
                lines.append("")
            lines.append(
                "**Downstream field(s) that will clear once this root is fixed:** "
                + ", ".join(f"`{it.get('subject', '?')}`" for it in downstream)
            )
            lines.append("")

    if hidden_registry:
        lines.append("## Hidden Prerequisite Fields (never on a shelf, no residue.json entry)")
        lines.append("")
        lines.append(
            f"{len(hidden_registry)} field(s) that at least one item above -- a DAX gap or a "
            f"Cascading Dependencies root -- references in its OWN Tableau formula, but that "
            f"never sits on any worksheet shelf itself, so build-tableau-pbip's own residue "
            f"tracking never logged a decline for it and it never appears anywhere else in this "
            f"document. Their name/formula were recovered directly from this workbook's own "
            f"`_work/ir.json`. Create these FIRST, in the order listed -- each one may itself "
            f"need an earlier entry in this same list."
        )
        lines.append("")
        if any_hit_cap:
            lines.append(
                "**Note:** at least one dependency chain hit this tool's own depth cap (15 "
                "levels) without fully resolving -- that chain is reported as far as it goes, "
                "not truncated silently; verify the last entry's own references by hand."
            )
            lines.append("")
        for i, (name, h) in enumerate(hidden_registry.items(), 1):
            kind_label = field_kind_label(name, agg_by_name)
            lines.append(f"### {i}. `{name}` -- create as a new **{kind_label}**")
            lines.append("")
            resolved_h_formula = resolve_formula_display(
                h["formula"], calc_by_id, real_col_ids, param_by_id) if h["formula"] else None
            lines.append(f"**Tableau formula:** `{resolved_h_formula or '(no formula captured)'}`")
            lines.append("")
            refs = describe_refs(h["formula"], calc_by_id, real_col_ids, param_by_id,
                                  existing_names, visible_subjects, hidden_names)
            if refs:
                lines.append("**References:** " + "; ".join(refs))
            else:
                lines.append("**References:** none -- self-contained.")
            lines.append("")

    lines.append("## DAX Measures to Fix")
    lines.append("")
    if dax_root_items:
        groups = group_items(dax_root_items)
        recommended, no_rec = split_by_recommendation(groups)

        def dax_group_lines(label, g, is_no_rec):
            items = g["items"]
            resolved_reason = resolve_formula_display(
                items[0].get("reason", ""), calc_by_id, real_col_ids, param_by_id)
            glabel = group_label(items, resolved_reason)
            n = len(items)
            out = [f"### {label}. {glabel} ({n} field{'s' if n != 1 else ''})", ""]
            out.append(
                "**Affected fields:** "
                + ", ".join(f"`{it.get('subject', '?')}`" for it in items)
            )
            out.append("")
            out.append("**Tableau metadata background:**")
            for it in items:
                subject = it.get("subject", "")
                resolved_it_formula = resolve_formula_display(
                    it.get("formula"), calc_by_id, real_col_ids, param_by_id)
                out.append(
                    f"- `{subject}` (table `{it.get('table', '?')}`, "
                    f"tier {it.get('tier', '?')}): `{resolved_it_formula or '(no formula captured)'}`"
                )
                kind_label = field_kind_label(subject, agg_by_name)
                out.append(f"  {locate_field(subject, field_to_ws, ws_to_pages, kind_label)}")
                if not field_has_report_impact(subject, field_to_ws):
                    no_impact_entries.append({
                        "name": subject, "table": it.get("table"), "tier": it.get("tier"),
                        "ref": f"{label} above",
                    })
                needs = field_all_needs.get(subject)
                if needs:
                    out.append(
                        "  **Also needs (create these FIRST, in order -- each has its own "
                        "entry either elsewhere in this document or under 'Hidden "
                        "Prerequisite Fields' above):** " + ", ".join(f"`{n}`" for n in needs))
                for note in field_param_notes.get(subject, []):
                    out.append(f"  **Parameter reference:** {note}")
                if "table calculation" in resolved_reason.lower():
                    tid = calc_id_by_name.get(subject)
                    tc_spec = calc_by_id.get(tid, {}).get("tc_spec") if tid else None
                    tc_desc = describe_table_calc(tc_spec, subject, field_to_shelf)
                    if tc_desc:
                        out.append(f"  {tc_desc}")
            out.append("")
            out.append(f"**Why it was declined:** {resolved_reason}")
            out.append("")
            if is_no_rec:
                out.append(SME_HINT_BLOCK)
            else:
                out.append(f"**What's needed in DAX (review before using):** {g['fix_text']}")
                out.append("")
                out.append("**Copilot prompt:**")
                out.append("```")
                out.append(build_dax_prompt(g["fix_text"], resolved_reason, items,
                                             calc_by_id, real_col_ids, param_by_id))
                out.append("```")
            out.append("")
            return out

        lines.append(
            f"{len(recommended)} pattern-group(s) below have a canned recommendation "
            f"(section A) -- still review the DAX before using it, since it is a starting "
            f"point that has not been checked against this exact workbook's real names. "
            f"{len(no_rec)} pattern-group(s) have no canned recommendation at all (section B) "
            f"-- those get the raw problem plus enough context for the SME to diagnose by hand."
        )
        lines.append("")
        if recommended:
            lines.append("### A. Recommended Fixes (Needs Review)")
            lines.append("")
            for i, (_key, g) in enumerate(recommended.items(), 1):
                lines.extend(dax_group_lines(f"A.{i}", g, False))
        if no_rec:
            lines.append("### B. No Automated Recommendation -- SME Investigation Needed")
            lines.append("")
            for i, (_key, g) in enumerate(no_rec.items(), 1):
                lines.extend(dax_group_lines(f"B.{i}", g, True))
    elif not dax_cascade:
        lines.append("None -- no declined calculated fields in this workbook.")
        lines.append("")
    else:
        lines.append(
            "None with their own root cause -- every DAX gap in this workbook is a cascade "
            "entry blocked purely by an already-declined dependency; see 'Cascading "
            "Dependencies' above."
        )
        lines.append("")

    lines.append("## Structural / Model-Design Gaps (not DAX)")
    lines.append("")
    if structural_items:
        lines.append(
            "These declined items carry no Tableau formula -- they are relationship, "
            "parameter, or model-design constructs, not calculated fields, so there is no DAX "
            "for Copilot to write. Each still gets a prompt, phrased as a design question rather "
            "than a DAX request."
        )
        lines.append("")
        groups = group_items(structural_items)
        recommended, no_rec = split_by_recommendation(groups)

        def structural_group_lines(label, g, is_no_rec):
            items = g["items"]
            resolved_reason = resolve_formula_display(
                items[0].get("reason", ""), calc_by_id, real_col_ids, param_by_id)
            glabel = group_label(items, resolved_reason)
            n = len(items)
            out = [f"### {label}. {glabel} ({n} item{'s' if n != 1 else ''})", ""]
            out.append(
                "**Affected item(s):** "
                + ", ".join(f"`{it.get('subject', '?')}`" for it in items)
            )
            out.append("")
            out.append("**Tableau metadata background:**")
            for it in items:
                subject = it.get("subject", "")
                out.append(f"- `{subject}` (table `{it.get('table', '?')}`)")
                forced = ("an unmodeled join collapsed into one flattened table can silently "
                          "inflate every SUM/COUNT built on it the moment either side turns out "
                          "to be one-to-many, whether or not the join itself sits on any shelf"
                          if structural_item_is_mandatory(it, field_to_ws)
                          and not field_has_report_impact(subject, field_to_ws) else None)
                # Not a calc -- a relationship/parameter/model-design item, never a Measure or
                # Column, so field_kind_label() (which only knows about calcs) does not apply.
                out.append(f"  {locate_field(subject, field_to_ws, ws_to_pages, 'model change (not a Measure or Column)', forced_mandatory_reason=forced)}")
                if not structural_item_is_mandatory(it, field_to_ws):
                    no_impact_entries.append({
                        "name": subject, "table": it.get("table"), "tier": None,
                        "ref": f"Structural {label} above",
                    })
            out.append("")
            out.append(f"**Why it was declined:** {resolved_reason}")
            out.append("")
            if is_no_rec:
                out.append(SME_HINT_BLOCK)
            else:
                out.append(f"**What's needed (review before using):** {g['fix_text']}")
                out.append("")
                out.append("**Copilot / Desktop prompt:**")
                out.append("```")
                out.append(build_structural_prompt(g["fix_text"], resolved_reason, items))
                out.append("```")
            out.append("")
            return out

        lines.append(
            f"{len(recommended)} pattern-group(s) below have a canned recommendation "
            f"(section A) -- still review it before using, since it has not been checked "
            f"against this exact workbook's real names. {len(no_rec)} pattern-group(s) have no "
            f"canned recommendation at all (section B) -- those get the raw problem plus enough "
            f"context for the SME to diagnose by hand."
        )
        lines.append("")
        if recommended:
            lines.append("### A. Recommended Fixes (Needs Review)")
            lines.append("")
            for i, (_key, g) in enumerate(recommended.items(), 1):
                lines.extend(structural_group_lines(f"A.{i}", g, False))
        if no_rec:
            lines.append("### B. No Automated Recommendation -- SME Investigation Needed")
            lines.append("")
            for i, (_key, g) in enumerate(no_rec.items(), 1):
                lines.extend(structural_group_lines(f"B.{i}", g, True))
    else:
        lines.append("None -- no structural/model-design gaps in this workbook.")
        lines.append("")

    if no_impact_entries and impact_filter is None:
        lines.append("## No Current Report Impact -- Fix Only If Needed")
        lines.append("")
        lines.append(
            "The field(s) below don't affect anything currently visible in this report -- "
            "they're either internal ingredients other calculated fields reference, or truly "
            "orphaned. Fixing one only matters if someone later wants to use it directly on a "
            "report page; until then, it's safe to leave it declined. This list cuts across "
            "every section above by REPORT IMPACT rather than by decline reason -- it doesn't "
            "replace those sections, it just re-surfaces the same per-field \"Semantic model "
            "only\" fact each entry above already states, gathered in one place so you don't "
            "have to read every entry closely to tell which ones matter today."
        )
        lines.append("")
        for e in no_impact_entries:
            loc = (f"table `{e['table']}`, tier {e['tier']}" if e.get("table") and e.get("tier")
                   else f"table `{e['table']}`" if e.get("table")
                   else "internal calc, no residue.json entry of its own")
            lines.append(f"- `{e['name']}` ({loc}) -- see {e['ref']} for the full Tableau formula and decline reason.")
        lines.append("")

    if visual_undecided or zone_gaps or declined_encodings or blend_key_gaps:
        lines.append("## Other Metadata Gaps")
        lines.append("")
        if visual_undecided:
            n = len(visual_undecided)
            lines.append(f"### Visual type undecided ({n} worksheet{'s' if n != 1 else ''})")
            lines.append("")
            lines.append(
                "**Tableau metadata background:** each worksheet below has a mark/shelf "
                "combination the deterministic visual-mapping rules don't recognize, so a safe "
                "`tableEx` placeholder was emitted instead of guessing a chart type."
            )
            lines.append("")
            lines.extend(build_visual_lines(visual_undecided))
            lines.append("")
            lines.append(
                "**Manual fix:** open each worksheet in Tableau to see what it actually renders, "
                "then pick the matching Power BI visual by hand in Desktop."
            )
            lines.append("")
        if zone_gaps:
            n = len(zone_gaps)
            lines.append(f"### Dropped zones/visuals ({n})")
            lines.append("")
            lines.append(
                "**Tableau metadata background:** each dashboard zone below had every shelf "
                "field fail to resolve to a model object (usually because the field itself is in "
                "the declined-calc list above), so the visual was dropped rather than emitted "
                "blank."
            )
            lines.append("")
            lines.extend(build_zone_lines(zone_gaps))
            lines.append("")
            lines.append(
                "**Manual fix:** these self-resolve once the underlying declined field(s) above "
                "are fixed and the workbook is rebuilt -- no separate action needed."
            )
            lines.append("")
        if declined_encodings:
            n = len(declined_encodings)
            lines.append(f"### Color/Size encodings not reproduced ({n})")
            lines.append("")
            lines.append(
                "**Tableau metadata background:** each visual below still renders with its axis "
                "and values intact -- only the legend/size split from the named field is "
                "missing, because Power BI Desktop's own visual schema rejects that role in this "
                "shape (e.g. too many distinct values for a Legend bucket) or the field resolves "
                "to a measure where Tableau used it as a grouping dimension."
            )
            lines.append("")
            lines.extend(build_encoding_lines(declined_encodings))
            lines.append("")
            lines.append(
                "**Manual fix:** in Power BI Desktop, either pick a different visual type/field "
                "role that tolerates this field's cardinality (e.g. a slicer instead of a Legend "
                "bucket), or accept the visual without this split -- there is no DAX fix, this is "
                "a visual-authoring decision."
            )
            lines.append("")
        if blend_key_gaps:
            n = len(blend_key_gaps)
            lines.append(f"### Blend Key relationships with zero real-data overlap ({n})")
            lines.append("")
            lines.append(
                "**Tableau metadata background:** each relationship below is build-tableau-"
                "pbip's own translation of a Tableau data blend into a Power BI many-to-many "
                "relationship, keyed on a synthetic composite \"Blend Key\" column. Once real "
                "(or realistic-sampled) data was wired in, validate-blend-key-domains found ZERO "
                "overlapping key values between the two tables -- this relationship can never "
                "produce a matching row, so any visual spanning both tables renders empty in "
                "Power BI Desktop regardless of how much data is loaded."
            )
            lines.append("")
            lines.extend(build_blend_key_lines(blend_key_gaps))
            lines.append("")
            lines.append(
                "**Manual fix:** in Power BI Desktop, either re-key the relationship on fields "
                "that genuinely share the same real-world values, build a bridge/mapping table "
                "between the two tables' mismatched dimensions, or accept that a visual spanning "
                "both tables cannot be built this way -- there is no DAX fix, this is a "
                "relationship/model-design decision."
            )
            lines.append("")

    return "\n".join(lines)


# ============================================================== discovery / CLI
def load_blend_key_gaps(wb_dir):
    """Blend Key zero-overlap findings from validate-blend-key-domains' own
    BLEND_KEY_DOMAIN_REPORT.json, mapped into the same {"tier", "table", "subject", "reason"}
    shape _work/residue.json's calc_residue items use -- deliberately no `formula` key, there is
    no DAX to write for this kind of gap (see `split_dax_vs_structural`'s own invariant: no
    GAP-tier item ever carries a formula). validate-blend-key-domains only ever runs once
    real/dummy data exists, so this finding is discovered POST-data and lives in a sibling
    `validation/` artifact, never inside the build-time-frozen `_work/residue.json` itself --
    every caller merges this in memory; that frozen file is never opened for writing. Returns
    `[]` if the report doesn't exist (a stub-only build, or Agent 4 has not run yet) or doesn't
    parse. Shared by `main()` (below) and effort_report.py's `compute_effort()`, which imports
    this function rather than keeping its own copy."""
    report_path = os.path.join(os.path.dirname(os.path.normpath(wb_dir)), "validation",
                                "BLEND_KEY_DOMAIN_REPORT.json")
    if not os.path.isfile(report_path):
        return []
    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []
    return [
        {"tier": "GAP", "table": r["table_a"],
         "subject": f"blend key relationship with {r['table_b']!r}",
         "reason": f"Blend Key relationship on ({', '.join(r['constituent_columns'])}) has ZERO "
                   f"overlapping real values between {r['table_a']!r} "
                   f"({r['value_count_a']} distinct value(s)) and {r['table_b']!r} "
                   f"({r['value_count_b']} distinct value(s)) -- this many-to-many relationship "
                   f"can never produce a matching row; any visual spanning both tables will "
                   f"render empty in Power BI Desktop regardless of how much data is loaded"}
        for r in report.get("relationships", []) if r.get("zero_overlap")
    ]


def find_residue_workbooks(root):
    """Return a sorted list of (workbook_dir, residue_json_path) under root."""
    found = []
    residue_path = os.path.join(root, "_work", "residue.json")
    if os.path.isfile(residue_path):
        found.append((root, residue_path))
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == "_work" and "residue.json" in filenames:
            found.append((os.path.dirname(dirpath), os.path.join(dirpath, "residue.json")))
    return sorted(found)


def _selftest():
    """Regression coverage for `_DEP_RE`: a cascade root declined in a DIFFERENT table than the
    field referencing it gets an ` in table '...'` qualifier appended by tmdl.py's `_explain()`
    (see tmdl.py's `where` variable) BEFORE the trailing ", which was itself declined" -- the
    original regex's non-greedy `(.+?)` had no way to know the id's own closing quote from the
    table name's, so it captured everything up to the LAST quote (id + " in table '" + table
    name) as one garbled key, which then never matched anything in `ir_index` (keyed by bare id
    alone) -- silently downgrading a fixable cascade root to "not found ... check the .twb
    directly." Found on AP Product Portfolio Management Dashboard's Roots #15/#16, both
    cross-table references into a Tableau-duplicated sibling datasource."""
    same_table = "depends on 'Calculation_123', which was itself declined (tier D) -- x"
    cross_table = (
        "depends on 'Calculation_475692734733418497' in table "
        "'tvaf_vcpp_market_landscape_apac', which was itself declined (tier D) -- x"
    )
    cross_table_slash = (
        "depends on 'Issuer - PV/Txns cy (copy)_1152640081415864320' in table "
        "'tvaf_vcpp_portfolio_view_apac_issuer', which was itself declined (tier D) -- x"
    )
    not_a_cascade = "table calculation -- addressing/partitioning is not in the formula text"

    m = _DEP_RE.match(same_table)
    assert m and m.group(1) == "Calculation_123", f"same-table case broke: {m}"

    m = _DEP_RE.match(cross_table)
    assert m, "cross-table case must still match"
    assert m.group(1) == "Calculation_475692734733418497", (
        f"cross-table id must not swallow the table qualifier, got {m.group(1)!r}"
    )

    m = _DEP_RE.match(cross_table_slash)
    assert m and m.group(1) == "Issuer - PV/Txns cy (copy)_1152640081415864320", (
        f"cross-table id with slashes/parens must not swallow the table qualifier, got "
        f"{m and m.group(1)!r}"
    )

    assert _DEP_RE.match(not_a_cascade) is None, "non-cascade reason must not match at all"

    print("OK  generate_copilot_prompts.py self-test (_DEP_RE cross-table cascade parsing)")


def _selftest_report_impact():
    """`field_has_report_impact()` must agree exactly with `locate_field()`'s own "Semantic
    model only" vs. real-page-pointer split -- a field with a real placement is never listed in
    the "No Current Report Impact" section, a field with none always is."""
    field_to_ws = {"Used Field": {("Worksheet A", "visual")}}

    assert field_has_report_impact("Used Field", field_to_ws) is True, (
        "a field WITH a real worksheet placement must report report-impact=True"
    )
    assert field_has_report_impact("Unused Field", field_to_ws) is False, (
        "a field never placed on any worksheet must report report-impact=False"
    )
    assert "Semantic model only" in locate_field("Unused Field", field_to_ws, {}, "MEASURE"), (
        "field_has_report_impact's False case must agree with locate_field's own wording"
    )
    assert "Semantic model only" not in locate_field("Used Field", field_to_ws, {}, "MEASURE"), (
        "field_has_report_impact's True case must agree with locate_field's own wording"
    )
    # The inline tag COPILOT_FIX_PROMPTS.md's own section A/B entries now carry, so a reader
    # skimming those sections directly sees the same mandatory/optional call the cross-cutting
    # "No Current Report Impact" section makes -- never has to cross-check a separate list.
    assert "[OPTIONAL" in locate_field("Unused Field", field_to_ws, {}, "MEASURE")
    assert "[MANDATORY]" in locate_field("Used Field", field_to_ws, {}, "MEASURE")

    # structural_item_is_mandatory: a physical-layer join is mandatory unconditionally, even
    # with zero shelf placement -- real corpus shape (Bookshop Data Analysis, PVT Monthly
    # Report) had every one of these landing in the OPTIONAL file before this override.
    join_item = {"subject": "Book ~ Author", "reason": "physical-layer join (BookID=AuthID) "
                 "inside a datasource that is extracted as a SINGLE table"}
    assert structural_item_is_mandatory(join_item, {}) is True, (
        "a physical-layer join item must be mandatory even with no shelf placement at all"
    )
    og_join_item = {"subject": "Book ~ Author", "reason": "key-based join (AuthID = AuthID "
                    "(Author); neither side declared unique -- cardinality unknown) inside a "
                    "federated datasource that is extracted as a single table"}
    assert structural_item_is_mandatory(og_join_item, {}) is True, (
        "an object-graph 'key-based join' item must ALSO be mandatory even with no shelf "
        "placement -- real corpus shape (Bookshop Data Analysis): 9 of these were missed by "
        "an earlier, narrower version of this check that only matched 'physical-layer join'"
    )
    other_item = {"subject": "Format to Title", "reason": "free-entry parameter domain"}
    assert structural_item_is_mandatory(other_item, {}) is False, (
        "a non-join structural item with no placement must still be optional (no regression)"
    )
    assert structural_item_is_mandatory(other_item, {"Format to Title": {("W", "filter")}}) is True, (
        "a non-join structural item WITH a real placement must still be mandatory (no regression)"
    )
    assert "[MANDATORY]" in locate_field(
        "Book ~ Author", {}, {}, "model change (not a Measure or Column)",
        forced_mandatory_reason="it risks silently inflating aggregates"), (
        "locate_field's forced_mandatory_reason must produce a [MANDATORY] line even with no "
        "shelf placement, agreeing with structural_item_is_mandatory's own verdict"
    )
    print("OK  generate_copilot_prompts.py self-test (field_has_report_impact, "
          "structural_item_is_mandatory)")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        _selftest_report_impact()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description="Generate ready-to-paste Copilot fix prompts from a workbook's residue.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single workbook build folder (writes COPILOT_FIX_PROMPTS.md in place)
  %(prog)s /path/to/output/dummy_pbip/MyDashboard

  # Parent directory of many workbook folders
  %(prog)s /path/to/output/dummy_pbip

  # Redirect output elsewhere
  %(prog)s /path/to/output/dummy_pbip --out /path/to/reports
""",
    )
    parser.add_argument("input", help="Workbook build folder, or parent directory of several")
    parser.add_argument("--out", help="Write COPILOT_FIX_PROMPTS.md under <out>/<Workbook>/ instead of in place")
    args = parser.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        sys.exit(2)

    workbooks = find_residue_workbooks(root)
    if not workbooks:
        print(f"error: no _work/residue.json found under {root}", file=sys.stderr)
        sys.exit(2)

    for wb_dir, residue_path in workbooks:
        workbook_name = os.path.basename(wb_dir)
        with open(residue_path, encoding="utf-8") as fh:
            residue = json.load(fh)
        residue, n_already_fixed = drop_already_emitted(wb_dir, residue)
        residue["blend_key_gaps"] = load_blend_key_gaps(wb_dir)
        ir_index = load_ir_calc_index(wb_dir)
        field_to_ws, ws_to_pages, field_to_shelf = build_location_index(wb_dir)
        calc_by_id, calc_id_by_name, real_col_ids, param_by_id = load_ir_full_index(wb_dir)
        agg_by_name = compute_agg_map_by_name(calc_by_id)
        existing_names = load_tmdl_existing_names(wb_dir)

        if args.out:
            out_dir = os.path.join(os.path.abspath(args.out), workbook_name)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = wb_dir

        written = []
        for impact_filter, fname in (("mandatory", "COPILOT_FIX_PROMPTS_MANDATORY.md"),
                                      ("optional", "COPILOT_FIX_PROMPTS_OPTIONAL.md")):
            md = render_markdown(workbook_name, residue, ir_index, field_to_ws, ws_to_pages,
                                  agg_by_name, calc_by_id, calc_id_by_name, real_col_ids,
                                  param_by_id, existing_names, field_to_shelf,
                                  impact_filter=impact_filter)
            out_path = os.path.join(out_dir, fname)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(md)
            written.append(out_path)

        old_combined = os.path.join(out_dir, "COPILOT_FIX_PROMPTS.md")
        if os.path.exists(old_combined):
            os.remove(old_combined)

        dax_items, structural_items = split_dax_vs_structural(residue.get("calc_residue", []))
        dax_root_items, dax_cascade = split_cascade(dax_items)
        n_dax, n_struct = len(dax_items), len(structural_items)
        n_roots = len(dax_root_items) + n_struct
        n_vis = len(residue.get("visual_undecided", []))
        n_zone = len(residue.get("zone_gaps", []))
        n_enc = len(residue.get("declined_encodings", []))
        n_blend = len(residue.get("blend_key_gaps", []))
        print(f"[ok] {workbook_name}  dax_gaps={n_dax} structural_gaps={n_struct} "
              f"root_causes={n_roots} cascade={len(dax_cascade)} "
              f"visual_undecided={n_vis} zone_gaps={n_zone} declined_encodings={n_enc} "
              f"blend_key_gaps={n_blend} "
              f"already_fixed_excluded={n_already_fixed}  -> {', '.join(written)}")

    sys.exit(0)


if __name__ == "__main__":
    main()
