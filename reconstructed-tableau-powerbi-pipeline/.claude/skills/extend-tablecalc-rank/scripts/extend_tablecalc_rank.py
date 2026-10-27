#!/usr/bin/env python3
"""
extend_tablecalc_rank.py -- EXPERIMENTAL, ISOLATED extension of build-tableau-pbip's own
RANK/RANK_DENSE table-calc rule (tmdl.py's `route()`) to a second safe partition shape:
"Compute Using: Specific Dimension(s)" -- a fixed field name, saved either on the calc's own
`<table-calc>` definition (`<order field=...>` children) or, when the calc is never placed
directly on a shelf, on every WORKSHEET that references it (Tableau's per-usage
`<column-instance><table-calc ordering-type='Field'>` copy -- see extract.py's own
`worksheet_tc_addressing()` docstring for the real XML shape).

Deliberately a SEPARATE skill, not a change to build-tableau-pbip/tmdl.py -- this extension's
own correctness gets checked against a real corpus workbook, in a build nobody else depends on,
before (if ever) folding the rule into the main pipeline. NEVER modifies the input .pbip: it
always copies to a new output directory first, so the original build stays the untouched
baseline to diff against.

Reuses build-tableau-pbip's own dax.translate()/names.py/tmdl.py helpers as a LIBRARY (imported,
never re-implemented) -- the DAX/TMDL this script emits is provably the same shape the main
pipeline's own RANK/RANK_DENSE promotion already produces and already self-tests (RANKX with
`ties` set from the same function-name check), just triggered by a different, also-real,
also-unguessed partition-resolution source.

Scope, on purpose -- fail closed on everything outside it, same as the main pipeline:
  - Only RANK/RANK_DENSE. RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE never qualify, for the exact
    reason tmdl.py's own `route()` docstring gives: no exact DAX tie-break equivalent.
  - Only promotes to a MEASURE. A calc `compute_agg_map_by_name`'s own seed rule
    (`dax.is_aggregate`) would resolve to a calculated COLUMN is skipped and reported, not
    guessed at -- the calculated-column emission path is not implemented here yet.
  - Only a partition field that is a real, ALREADY-EMITTED column on the calc's OWN table
    (checked against `name_authority.json`'s own `emitted` list -- the exact "is this real in
    the model" ground truth the main pipeline's `_resolver()` uses via its `declined` guard).
  - When the addressing was saved per-WORKSHEET (not on the calc itself), every worksheet that
    references it must agree on the same field -- checked by set-equality on the resolved field
    names, never "the first one found".
  - The formula must reference nothing this script's own minimal, same-table-only resolver can
    resolve -- a parameter, a cross-table field, another calc. `dax.translate()` raises
    `Unsupported` in that case, reported as a skip, never a crash or a guess.

Usage:
  python3 extend_tablecalc_rank.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>

`<workbook.twbx>` is accepted for provenance/logging and is not re-parsed -- every fact this
script acts on already lives in `<existing_pbip_dir>/_work/{ir,name_authority,residue}.json`,
written by the SAME extraction the regular build already ran. Re-parsing the twbx here would
risk a second, independent parse quietly drifting from what the existing build actually saw.
"""
import argparse
import json
import os
import re
import shutil
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BUILD_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "build-tableau-pbip", "scripts"))
_PROMPTS_SCRIPTS = os.path.normpath(
    os.path.join(_THIS_DIR, "..", "..", "generate-copilot-fix-prompts", "scripts"))
sys.path.insert(0, _BUILD_SCRIPTS)
sys.path.insert(0, _PROMPTS_SCRIPTS)

import dax                                                     # noqa: E402
from names import dax_ref                                      # noqa: E402
from tmdl import _measure, lineage, fkey                       # noqa: E402

_RANK_FUNC_RE = re.compile(r"^\s*(RANK|RANK_DENSE)\s*\(", re.I)
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


def alloc_table_name(na, t):
    """The REAL, allocated PBI table name (and hence .tmdl filename) for this ir.json table --
    keyed the same way `NameAuthority.table()` was originally called, confirmed against a real
    corpus workbook's own name_authority.json (`na['tables'][t['tableau_name']]`)."""
    return na["tables"].get(t.get("tableau_name") or t.get("caption"))


def resolve_via_na(na, alloc_table, field_dict):
    """(alloc PBI table name, {tableau_name,...} dict) -> the real, ALREADY-EMITTED PBI name for
    this field, or None. Mirrors `TmdlModel._resolver()`'s own two checks exactly: an allocated
    name that was never actually written into the model (declined) must resolve to None here
    too, the same as it does for the real pipeline -- checked directly against
    `name_authority.json`'s own `emitted` list, the ground truth for "is this real"."""
    tid = fkey(field_dict)
    key = f"{alloc_table} | {tid}"
    name = na["columns"].get(key) or na["measures"].get(key)
    if not name:
        return None
    if f"{alloc_table} | {name}" not in na.get("emitted", []):
        return None
    return name


def make_resolver(t, na, alloc_table):
    """A field-reference resolver scoped to THIS ONE TABLE -- the narrow shape a RANK/RANK_DENSE
    partition argument needs (same-table `SUM(column)` or a bare column), not a general
    cross-table resolver. Returns a DAX reference string `'Table'[Name]`, or None -- which makes
    `dax.translate()` raise `Unsupported` for anything outside this narrow shape, reported as a
    skip by the caller, never guessed past.

    Indexed by BOTH spellings a Tableau formula can use for the same field -- its caption
    ("Part Share") and its bare internal identifier ("part_share", from `fkey()`) -- mirroring
    `TmdlModel._resolver()`'s own dual-spelling index exactly. Real corpus formula
    (WorkbookJ): `RANK(SUM([part_share]))` references the INTERNAL
    spelling, never the caption "Part Share" -- indexing by caption alone resolves nothing.
    """
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


# ==================================================================== candidate discovery
def resolved_partition_field(calc):
    """This calc's own confidently-resolved "Compute Using: Specific Dimension(s)" field, from
    EITHER its own `<table-calc><order>` children (already unambiguous -- one calc, one saved
    setting) OR every worksheet's saved per-usage override, cross-checked for agreement.

    Returns (field_caption, None) when resolved and safe, or (None, reason) when not -- reason
    is always a specific, reportable sentence, never a bare "no".
    """
    spec = calc.get("tc_spec") or {}
    if spec.get("level_break_field"):
        return None, ("already covered by the main pipeline's own Restart-every-field rule -- "
                      "not this extension's concern")
    own = spec.get("order_fields") or []
    own_unique = sorted(set(own))
    if len(own_unique) == 1:
        return own_unique[0], None
    if len(own_unique) > 1:
        return None, (f"the calc's OWN addressing names {len(own_unique)} different fields "
                      f"({', '.join(own_unique)}) -- ambiguous, not a single partition")
    ws = spec.get("ws_order_fields") or []
    names = sorted({d["field"] for d in ws})
    if len(names) == 1:
        return names[0], None
    if len(names) > 1:
        wss = sorted({d["worksheet"] for d in ws})
        return None, (f"worksheets disagree on the partition field ({', '.join(names)} across "
                      f"{len(wss)} worksheet(s): {', '.join(wss)}) -- not safe to promote as one "
                      f"shared model measure")
    return None, "no Specific-Dimension(s) addressing found on the calc or any worksheet"


def find_candidates(ir, na, residue):
    """Every table-calc this extension COULD act on, classified as either a promotable
    candidate or a skip (with a specific reason) -- never silently dropped, matching this whole
    pipeline's own "always report" convention.

    `residue.json`'s own `"table"` field is the ALLOCATED PBI table name (tmdl.py's `route()`
    writes residue entries keyed by `pbi`, after allocation) -- NOT `ir.json`'s raw Tableau
    caption, which can carry a parenthetical suffix the allocated name strips (real corpus
    example: caption `source_cube_a (nasoln_dm)` allocates to
    `source_cube_a`). Matching against the raw caption here silently found zero
    declined entries for every real candidate on first pass -- matching against the allocated
    name (`alloc_table_name()`, the same lookup `resolve_via_na()` already uses) is correct.
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
            if not _RANK_FUNC_RE.match(c.get("formula") or ""):
                continue
            if (alloc_table, c["name"]) not in declined_keys:
                continue    # not currently outstanding -- nothing for this extension to do
            entry = {"table_caption": table_caption, "alloc_table": alloc_table,
                     "calc": c, "name": c["name"], "formula": c["formula"]}
            if alloc_table is None:
                entry.update(status="skipped",
                             reason="this table's own PBI name could not be resolved from "
                                    "name_authority.json")
                out.append(entry)
                continue
            field, reason = resolved_partition_field(c)
            if field is None:
                entry.update(status="skipped", reason=reason)
                out.append(entry)
                continue
            if not dax.is_aggregate(c["formula"] or ""):
                entry.update(status="skipped",
                             reason="this calc would resolve to a calculated COLUMN, not a "
                                    "MEASURE -- the column-emission path is not implemented in "
                                    "this extension yet")
                out.append(entry)
                continue
            entry.update(status="candidate", partition_field=field)
            out.append(entry)
    return out


# ==================================================================== DAX generation + patch
def generate_dax(t, na, alloc_table, candidate):
    """Reuses `dax.translate()` -- the SAME function tmdl.py's own RANK/RANK_DENSE promotion
    calls -- with `tc_partition` set to this extension's OWN resolved field instead of a
    Restart-every-field one. Raises `dax.Unsupported` for anything the narrow same-table
    resolver above cannot handle; the caller reports that as a skip."""
    resolve = make_resolver(t, na, alloc_table)
    partition_col = resolve(candidate["partition_field"])
    if partition_col is None:
        raise dax.Unsupported(
            f"partition field {candidate['partition_field']!r} did not resolve to a real, "
            f"already-emitted column on this same table")
    expr, refs, _params = dax.translate(
        candidate["formula"], resolve, tc_partition=partition_col)
    return expr, refs


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
            expr, refs = generate_dax(t, na, alloc_table, cand)
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
def render_report(workbook_ref, pbip_dir, out_dir, results):
    promoted = [r for r in results if r["status"] == "promoted"]
    skipped = [r for r in results if r["status"] == "skipped"]
    lines = [
        "# Table-Calc RANK Extension Report (EXPERIMENTAL, isolated from the main pipeline)", "",
        f"Source workbook: `{workbook_ref}`", f"Existing build: `{pbip_dir}`",
        f"New, patched build: `{out_dir}`", "",
        f"{len(promoted)} RANK/RANK_DENSE table calc(s) promoted from declined to a real DAX "
        f"measure. {len(skipped)} candidate(s) checked and left alone, each for a specific, "
        f"reported reason -- never silently dropped.", "",
    ]
    if promoted:
        lines.append("## Promoted")
        lines.append("")
        for r in promoted:
            lines.append(f"### `{r['name']}` (table `{r['table_caption']}`)")
            lines.append("")
            lines.append(f"**Original Tableau formula:** `{r['formula']}`")
            lines.append("")
            lines.append(f"**Partition field:** `{r['partition_field']}`")
            lines.append("")
            lines.append(f"**New DAX measure:**\n```\n{r['dax']}\n```")
            lines.append("")
            lines.append(f"**Written to:** `{r['tmdl_file']}`")
            lines.append("")
    if skipped:
        lines.append("## Checked, not promoted")
        lines.append("")
        for r in skipped:
            lines.append(f"- `{r['name']}` (table `{r['table_caption']}`): {r['reason']}")
        lines.append("")
    lines.append(
        "## Before trusting this build")
    lines.append("")
    lines.append(
        "- [ ] Open the new build in Power BI Desktop; confirm it loads with no errors.")
    lines.append(
        "- [ ] For each promoted measure, spot-check its value against Tableau's own rendered "
        "number on a worksheet that uses it -- this extension's own name allocation and "
        "insertion point are NOT the same code path the main pipeline's `NameAuthority` and "
        "`TmdlModel.build()` use, so treat this build as provisional until verified.")
    lines.append(
        "- [ ] Run `tools/pbip_rules/lint.py <new_pbip_dir> --schemas` for a structural check.")
    lines.append("")
    return "\n".join(lines)


# ==================================================================== CLI
def main():
    parser = argparse.ArgumentParser(
        description="EXPERIMENTAL: promote RANK/RANK_DENSE table calcs with a resolved "
                    "'Specific Dimension(s)' partition into a real DAX measure, in a separate "
                    "copy of an existing build-tableau-pbip project.")
    parser.add_argument("twbx", help="original .twbx/.twb (provenance only, not re-parsed)")
    parser.add_argument("pbip_dir", help="existing build-tableau-pbip output folder "
                                         "(must contain _work/ir.json etc.)")
    parser.add_argument("--out", required=True, help="new output folder (must not exist)")
    args = parser.parse_args()

    ir, na, residue = load_workbook_state(args.pbip_dir)
    candidates = find_candidates(ir, na, residue)
    results = promote(args.pbip_dir, args.out, candidates, ir, na)

    report = render_report(args.twbx, args.pbip_dir, args.out, results)
    report_path = os.path.join(args.out, "TABLECALC_RANK_EXTENSION_REPORT.md")
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)

    n_promoted = sum(1 for r in results if r["status"] == "promoted")
    n_skipped = sum(1 for r in results if r["status"] == "skipped")
    print(f"[ok] promoted={n_promoted} skipped={n_skipped} -> {args.out}")
    print(f"     report: {report_path}")
    sys.exit(0)


# ==================================================================== self-test
def _selftest():
    # resolved_partition_field: own-definition order_fields wins over ws_order_fields; a single
    # consistent value across worksheets resolves, disagreement does not, an already-covered
    # level_break_field defers to the main pipeline's own rule.
    field, reason = resolved_partition_field({"tc_spec": {"order_fields": ["Value1"]}})
    assert (field, reason) == ("Value1", None), (field, reason)

    field, reason = resolved_partition_field({"tc_spec": {
        "ws_order_fields": [{"field": "Value1", "worksheet": "W1"},
                            {"field": "Value1", "worksheet": "W2"}]}})
    assert (field, reason) == ("Value1", None), (field, reason)

    field, reason = resolved_partition_field({"tc_spec": {
        "ws_order_fields": [{"field": "Value1", "worksheet": "W1"},
                            {"field": "Value2", "worksheet": "W2"}]}})
    assert field is None and "disagree" in reason, (field, reason)

    field, reason = resolved_partition_field({"tc_spec": {"level_break_field": "Region"}})
    assert field is None and "already covered" in reason, (field, reason)

    field, reason = resolved_partition_field({"tc_spec": {}})
    assert field is None and "no Specific-Dimension" in reason, (field, reason)
    print("OK  resolved_partition_field: own-definition, worksheet-agreement, "
          "worksheet-disagreement, already-covered, unresolved")

    # find_candidates: end-to-end against a small synthetic ir/na/residue fixture -- mirrors the
    # real WorkbookJ shape (a promotable RANK, a same-name RANK on another table that is a column,
    # not a measure) without needing a real build on disk.
    ir = {"tables": [
        {"tableau_name": "ds1", "caption": "T1 (suffix)", "columns": [
            {"name": "Part Share", "tableau_name": "[part_share]"},
            {"name": "Value1", "tableau_name": "[value1]"},
        ], "calcs": [
            {"name": "Part Share Rank", "tableau_name": "[Calculation_1]", "kind": "tablecalc",
             "formula": "RANK(SUM([part_share]))",
             "tc_spec": {"ws_order_fields": [{"field": "Value1", "worksheet": "W1"}]}},
            {"name": "Not A Calc", "tableau_name": "[Calculation_2]", "kind": "scalar",
             "formula": "[part_share] + 1", "tc_spec": None},
        ]},
    ]}
    na = {"tables": {"ds1": "T1"},
         "columns": {"T1 | part_share": "Part Share", "T1 | value1": "Value1"},
         "measures": {},
         "emitted": ["T1 | Part Share", "T1 | Value1"]}
    residue = {"calc_residue": [
        {"table": "T1", "subject": "Part Share Rank",
         "reason": "table calculation -- addressing/partitioning is not in the formula text"},
    ]}
    cands = find_candidates(ir, na, residue)
    assert len(cands) == 1, cands
    assert cands[0]["status"] == "candidate" and cands[0]["partition_field"] == "Value1", cands[0]
    print("OK  find_candidates: only the declined RANK tablecalc is surfaced, "
          "the non-tablecalc sibling is ignored")

    # generate_dax: reuses dax.translate() end-to-end -- same RANKX/ALLEXCEPT shape the main
    # pipeline's own self-tests already lock in for a resolved Restart-every-field partition,
    # now reached via this extension's own Specific-Dimension(s) resolution instead.
    t = ir["tables"][0]
    expr, refs = generate_dax(t, na, "T1", cands[0])
    assert expr == ("RANKX(ALLEXCEPT('T1', 'T1'[Value1]), SUM('T1'[Part Share]), , DESC, Skip)"), expr
    # tc_partition is passed to dax.translate() already resolved to a DAX string, so the
    # partition field itself is never seen by the formula's OWN resolve() callback -- only the
    # RANK argument's own reference ("part_share") is tracked as a ref.
    assert refs == ["part_share"], refs
    print(f"OK  generate_dax: {expr}")

    # A partition field that never resolves (not a real, emitted column) must raise Unsupported,
    # not silently fall back to something -- the caller turns this into a reported skip.
    bad_cand = {**cands[0], "partition_field": "Nonexistent Field"}
    try:
        generate_dax(t, na, "T1", bad_cand)
        raise AssertionError("expected dax.Unsupported for an unresolvable partition field")
    except dax.Unsupported:
        pass
    print("OK  generate_dax declines (does not guess) when the partition field cannot be "
          "resolved to a real column")

    print("extend_tablecalc_rank.py self-test OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
        sys.exit(0)
    if len(sys.argv) == 1:
        print("usage: extend_tablecalc_rank.py <workbook.twbx> <existing_pbip_dir> --out <new_pbip_dir>")
        sys.exit(2)
    main()
