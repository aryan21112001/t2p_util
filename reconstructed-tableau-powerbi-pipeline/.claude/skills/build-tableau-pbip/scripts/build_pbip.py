#!/usr/bin/env python3
"""
S5 -- assemble, validate, report.

Runs S0..S4 in the only order that is safe (every name allocated before either builder emits a
reference to it), then runs a pre-flight check over the files on disk.

The pre-flight exists because of what the failures in this format look like
--------------------------------------------------------------------------
Most of them are silent. `version.json` with `"1.0.0"` is schema-valid and opens the report with
zero page tabs and no error at all. A `visual.json` whose `Property` names a measure the model does
not have gives a broken visual with no indication of which file is wrong. A `resourcePackages` item
with no file on disk makes Desktop refuse the whole project.

None of those are caught by reading the code, so each is an assertion here, checked against the
bytes that were actually written rather than against the intent that wrote them.

Coverage is reported as a fraction of what the SOURCE had, not of what was attempted. A build that
declines 30 calcs and emits 100 has 77% coverage, not 100%.
"""
import os
import re
import sys
import json
import shutil
import uuid
import argparse
import collections

import extract
import names as names_mod
import tmdl as tmdl_mod
import pbir as pbir_mod


def _w(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


# TOM's real DataType enum -- what `dataType:` in TMDL is actually allowed to say. Confirmed
# against real Desktop: "date" is NOT a member (docs/schema-reference/pbip-schema.md's own
# "Column Data Types" table claims otherwise -- it is wrong, same as it has been six other times;
# Desktop is the authority, not the doc). Only the values this project's writers actually emit
# are listed; a value outside this set is either the "date" bug recurring or a new one just like
# it, and either way `preflight` should say so before Desktop does.
TOM_DATA_TYPES = {"string", "int64", "double", "decimal", "boolean", "dateTime"}


# ---------------------------------------------------------------- pre-flight
def preflight(root, model_dir, report_dir, na):
    """Return a list of (level, message). `level` 'FAIL' means Desktop will reject or silently
    mis-open the project; 'WARN' means it opens but something is off."""
    out = []
    md = os.path.join(root, model_dir)
    rd = os.path.join(root, report_dir)

    def fail(m):
        out.append(("FAIL", m))

    def warn(m):
        out.append(("WARN", m))

    # -- the silent one --------------------------------------------------
    vpath = os.path.join(rd, "definition", "version.json")
    if not os.path.isfile(vpath):
        fail("definition/version.json missing")
    else:
        v = json.load(open(vpath, encoding="utf-8"))
        if v.get("version") != "2.0.0":
            fail(f'version.json version is {v.get("version")!r}, must be "2.0.0" -- "1.0.0" is '
                 "schema-valid and opens the report with zero page tabs and no error")

    # -- required files ---------------------------------------------------
    for rel, where in (("definition.pbism", md), ("definition/database.tmdl", md),
                       ("definition/model.tmdl", md), ("definition.pbir", rd),
                       ("definition/report.json", rd), ("definition/pages/pages.json", rd)):
        if not os.path.isfile(os.path.join(where, rel)):
            fail(f"{os.path.relpath(os.path.join(where, rel), root)} missing")

    # -- TMDL indentation -------------------------------------------------
    for dirpath, _d, files in os.walk(md):
        for fn in files:
            if not fn.endswith(".tmdl"):
                continue
            p = os.path.join(dirpath, fn)
            for i, ln in enumerate(open(p, encoding="utf-8"), 1):
                # Only the TMDL depth prefix must be tabs. Spaces AFTER it are legal and Desktop
                # writes them: its own partition body is `\t\t\t\t    Source = ...`, tabs to reach the
                # expression level then spaces for M's internal indentation. The earlier rule also
                # rejected "\t " anywhere, which would have failed a Desktop-authored file.
                if ln.startswith(" "):
                    fail(f"{os.path.relpath(p, root)}:{i} indentation starts with a space -- "
                         f"TMDL depth must be TABs")
                    break
            # A TMDL document must OPEN with an object declaration. A leading comment is line type
            # "Other" and aborts the whole project:
            #   Document './relationships' Line 1 - '// All model relationships...'
            # And a document containing nothing but comments has no object at all.
            lines = open(p, encoding="utf-8").read().split("\n")
            body = [ln for ln in lines if ln.strip()]
            if body and body[0].lstrip().startswith("/"):
                fail(f"{os.path.relpath(p, root)}:1 a TMDL document must begin with an object "
                     f"declaration, not a comment: {body[0].strip()[:60]!r}")
            elif not body:
                fail(f"{os.path.relpath(p, root)} is empty -- delete the file instead")
            elif all(ln.lstrip().startswith("/") for ln in body):
                fail(f"{os.path.relpath(p, root)} contains only comments and declares no object "
                     f"-- omit the file entirely")
            # `expression =` as a COLUMN property. Desktop:
            #   Property 'expression' is unknown and is not expected in the situation it appears.
            # A calculated column is `column X = <dax>` on the declaration line. Asserted here
            # because the construct is written from several places and one of them was missed.
            for j, ln in enumerate(lines, 1):
                if re.match(r"^\t\texpression\s*=", ln):
                    fail(f"{os.path.relpath(p, root)}:{j} `expression =` is not a column property "
                         f"-- write `column X = <dax>` on the declaration line")
                    break
            # `dataType:` must be one of TOM's real enum members. A value that IS schema-valid
            # text but not a real enum member (e.g. "date") makes Desktop refuse to load the
            # WHOLE project: "Failed to convert the value '...' to the expected type DataType!"
            for j, ln in enumerate(lines, 1):
                m = re.match(r"^\t\tdataType:\s*(\S+)", ln)
                if m and m.group(1) not in TOM_DATA_TYPES:
                    fail(f"{os.path.relpath(p, root)}:{j} dataType {m.group(1)!r} is not a real "
                         f"TOM DataType (valid: {', '.join(sorted(TOM_DATA_TYPES))}) -- Desktop "
                         f'will refuse to load the model: "Failed to convert the value \'...\' to '
                         f'the expected type DataType!"')
            # A `Table.FromRows` row value must not be a quoted string when the column's own M
            # type says it isn't text (e.g. `{{"2026-05-28"}}` against `type table [Value =
            # datetime]`). Confirmed against real Desktop, on a real parameter table:
            # "Expression.Error: The type of the value does not match the type of the column."
            # This is the sibling check to the dataType one above -- that one is the TOM column's
            # declared type, this is the M partition's actual data -- and it exists because the
            # FIX for the dataType bug (normalizing the column to `datetime`) did not also fix the
            # value still being a plain quoted string. Two data points asserted in two different
            # places with nothing checking they still agree.
            for j, ln in enumerate(lines, 1):
                m = re.search(
                    r"Table\.FromRows\(\{\{(?P<value>.*?)\}\},\s*type table \[\w+\s*=\s*"
                    r"(?P<mtype>[\w.]+)\]\)", ln)
                if m and m.group("mtype") in ("datetime", "date", "logical", "Int64.Type",
                                              "number", "Number.Type") \
                        and m.group("value").strip().startswith('"'):
                    fail(f"{os.path.relpath(p, root)}:{j} Table.FromRows row value "
                         f"{m.group('value').strip()} is a quoted string but the column type is "
                         f"{m.group('mtype')!r} -- Desktop will reject this at refresh: \"The "
                         f"type of the value does not match the type of the column\"")

    # -- pages: name == folder name, 20 lowercase hex ---------------------
    pages_dir = os.path.join(rd, "definition", "pages")
    pj = os.path.join(pages_dir, "pages.json")
    listed = set()
    if os.path.isfile(pj):
        meta = json.load(open(pj, encoding="utf-8"))
        listed = set(meta.get("pageOrder") or [])
        if not listed:
            fail("pages.json pageOrder is empty -- the report would open with no page tabs")
        if meta.get("activePageName") and meta["activePageName"] not in listed:
            fail("pages.json activePageName is not in pageOrder")
    on_disk = {d for d in (os.listdir(pages_dir) if os.path.isdir(pages_dir) else [])
               if os.path.isdir(os.path.join(pages_dir, d))}
    for extra in sorted(on_disk - listed):
        warn(f"page folder {extra} is not in pages.json pageOrder (Desktop appends it by name)")
    for missing in sorted(listed - on_disk):
        fail(f"pages.json lists page {missing} with no folder on disk")

    # -- every visual/page name matches its folder ------------------------
    HEX20 = re.compile(r"^[0-9a-f]{20}$")
    refs = []
    for page in sorted(on_disk):
        p = os.path.join(pages_dir, page, "page.json")
        if not os.path.isfile(p):
            fail(f"page folder {page} has no page.json")
            continue
        d = json.load(open(p, encoding="utf-8"))
        if d.get("name") != page:
            fail(f"page.json name {d.get('name')!r} != folder name {page!r}")
        if not HEX20.match(page):
            warn(f"page name {page} is not 20 lowercase hex characters")
        vdir = os.path.join(pages_dir, page, "visuals")
        for v in sorted(os.listdir(vdir) if os.path.isdir(vdir) else []):
            vp = os.path.join(vdir, v, "visual.json")
            if not os.path.isfile(vp):
                fail(f"visual folder {page}/{v} has no visual.json")
                continue
            vd = json.load(open(vp, encoding="utf-8"))
            if vd.get("name") != v:
                fail(f"visual.json name {vd.get('name')!r} != folder name {v!r}")
            if ("visual" in vd) == ("visualGroup" in vd):
                fail(f"{page}/{v}/visual.json must carry exactly one of visual/visualGroup")
            # -- a data-bound visual with an empty field well renders as a blank rectangle,
            # with none of Desktop's own "add data to this visual" affordance to say why.
            # `pbir.py`'s own `_visual` now declines a visual in exactly this shape instead of
            # emitting it (found on the WorkbookC corpus workbook: 164 such visuals, via
            # `tools/pbip_rules/checks.py`'s independent RPT-ROLE-02 check) -- this is the build's
            # OWN backstop for the same invariant, so a regression in the generator is caught
            # here too, not only by the external lint pass.
            vt = (vd.get("visual") or {}).get("visualType")
            if vt in pbir_mod.WELLS or vt == "slicer":
                qs = ((vd.get("visual") or {}).get("query") or {}).get("queryState") or {}
                n_proj = sum(len(role.get("projections", [])) for role in qs.values()
                             if isinstance(role, dict))
                if n_proj == 0:
                    fail(f"{page}/{v}/visual.json: visualType {vt!r} has no field bound in "
                         f"any role -- renders as a blank rectangle in Desktop with no "
                         f"indication which field is missing (RPT-ROLE-02)")
                # -- a role with more fields than its well physically accepts. Found on a real
                # rebuilt workbook (WorkbookF, "Region Rpt Status" pie chart): two
                # dimensions on the source worksheet's Rows shelf (ordinary, valid Tableau) both
                # landed in a pieChart's `Category` well, which Desktop caps at 1 -- schema-valid
                # PBIR that `tools/pbip_lint.py --schemas`'s independent RPT-ROLE-03-style check
                # caught and this build's own `preflight()` had no assertion for at the time. This
                # is the build's own backstop for the same invariant `pbir_mod.ROLE_CAPS` now
                # enforces at emission time, so a regression in the generator is caught here too.
                for role, cap in (pbir_mod.ROLE_CAPS.get(vt) or {}).items():
                    role_name = pbir_mod.WELLS.get(vt, {}).get(role)
                    if not role_name:
                        continue
                    count = len((qs.get(role_name) or {}).get("projections", []))
                    if count > cap:
                        fail(f"{page}/{v}/visual.json: visualType {vt!r} role {role_name!r} "
                             f"has {count} field(s) but the field well accepts {cap} -- the "
                             f"editor physically refuses the extra drop")
            for proj in _iter_projections(vd):
                refs.append((page, v, proj))

    # -- measure names must be unique across the WHOLE MODEL --------------
    # Columns are scoped per table; MEASURES are not. Desktop:
    #   Could not add Measure with the name 'X' because a Measure with the same name already
    #   exists in the 'Model' Model.
    # The existing duplicate check was per table, so 63 model-wide duplicates passed it.
    # Folded case-insensitively -- see the per-table check below for why: TOM's own collision
    # rule ignores case, so two measures differing only in case are exactly as invalid as two
    # spelled identically, and a case-sensitive dict key would miss that pair.
    all_measures = collections.defaultdict(list)
    for table, members in _tmdl_objects(md).items():
        for name, kind in members.items():
            if kind == "measure":
                all_measures[name.casefold()].append((name, table))
    for folded, where in sorted(all_measures.items()):
        if len(where) > 1:
            names = ", ".join(sorted({n for n, _t in where}))
            tables = [t for _n, t in where]
            fail(f"measure(s) {names} collide case-insensitively across {len(tables)} tables "
                 f"({', '.join(tables[:3])}{'...' if len(tables) > 3 else ''}) -- measure names "
                 f"are unique MODEL-WIDE")

    # -- within a table, column/measure names collide CASE-INSENSITIVELY --
    # TOM's own name collection ignores case (Windows/SQL Server convention), so 'SAMPLE CHECK ALPHA
    # ' and 'Sample Check Alpha' are the SAME name to it even though they are different
    # Python strings. names.py's own collision avoidance is fixed to compare the same way; this
    # is the backstop that catches it if that ever regresses -- a real corpus workbook had
    # exactly this pair (a physical column and a same-named calculated-field copy) and Desktop
    # refused to load the file at all ("Item '...' already exists in the collection"), not just
    # render one visual wrong.
    for _table, _members_list in _tmdl_member_lines(md).items():
        seen = {}
        for _name in _members_list:
            folded = _name.casefold()
            if folded in seen:
                fail(f"table {_table!r}: {seen[folded]!r} and {_name!r} collide -- "
                     f"{'the exact same name appears twice' if seen[folded] == _name else 'TOM treats them as the same name case-insensitively'}, "
                     f"and Desktop will refuse to load the model")
            seen[folded] = _name

    # -- structural TOM rules, read back from the emitted files ------------
    # Each of these is a rule TOM enforces at model load, invisible to TMDL parsing: the file is
    # syntactically perfect and the model is still rejected.
    for table, info in _tmdl_structure(md).items():
        if not info["columns"]:
            fail(f"table {table!r} declares no column -- TOM requires at least one")
        if not info["partition"]:
            fail(f"table {table!r} has no partition")
        for h in info["hierarchies"]:
            if h in info["members"]:
                fail(f"table {table!r}: hierarchy {h!r} shares its name with a column or measure")
        seen_levels = collections.Counter(info["levels"])
        for (h, lv), n in seen_levels.items():
            if n > 1:
                fail(f"table {table!r}: hierarchy {h!r} declares level {lv!r} {n} times")
        for h, lv in info["levels"]:
            if lv not in info["columns"]:
                fail(f"table {table!r}: hierarchy {h!r} level {lv!r} references a column that is "
                     f"not declared in this table")

    # -- every PBIR field reference resolves to a real TMDL object --------
    model_objects = _tmdl_objects(md)

    # -- a calculated column must never reference a measure ----------------
    # DAX forbids it. Desktop reports it as "A circular dependency was detected" between two
    # UNRELATED columns that happen to share the measure reference -- the real cause (a column
    # referencing a measure) never appears in the error. Only discoverable by tracing the model
    # back to source, which is what this checks directly from the emitted files.
    # NOTE: `refs` is also the name of the outer PBIR-projections list built above. Using it here
    # too silently reassigned that list and broke the loop below it, which was caught only by
    # actually running the build -- a second reminder that these checks need to be exercised, not
    # just read.
    for table in model_objects:
        col_refs = _column_dax_refs(md, table)
        for col, col_dax_refs in col_refs.items():
            for ref_table, ref_name in col_dax_refs:
                kind = model_objects.get(ref_table, {}).get(ref_name)
                if kind == "measure":
                    fail(f"table {table!r}: calculated column {col!r} references measure "
                         f"{ref_table}[{ref_name}] -- DAX forbids a column referencing a measure; "
                         f"Desktop reports this as an unrelated circular dependency")

    # Same rule, the BARE `[Name]` shape. Measures are unique model-wide (names.py's own contract),
    # so a bare reference that matches ANY measure anywhere is a violation regardless of which table
    # the calculated column lives in. Missing this let a real workbook (WorkbookB Dashboard) emit
    # 'cl.Is Selected Selector?' as a calculated COLUMN referencing a parameter's SELECTEDVALUE
    # measure via the bare form `resolve_param` always uses -- the qualified-only check above never
    # saw it, the model loaded, and every report filter on that column failed in Desktop with
    # "Broken_Filters" instead of a load-time error. `tmdl.py`'s `_aggregate_map` now promotes any
    # such calc to a measure itself before this can happen, so a FAIL here means that promotion
    # regressed, not that the workbook has a new unsupported pattern.
    all_measures = {name for members in model_objects.values()
                    for name, kind in members.items() if kind == "measure"}
    for table in model_objects:
        for col, bare_refs in _column_bare_dax_refs(md, table).items():
            for ref_name in bare_refs:
                if ref_name in all_measures:
                    fail(f"table {table!r}: calculated column {col!r} references measure "
                         f"[{ref_name}] -- DAX forbids a column referencing a measure; Desktop "
                         f"reports this as an unrelated circular dependency")

    # -- an aggregate function must never wrap a bare reference to a MEASURE ------------------
    # The symmetric case of the two checks just above: instead of a calculated COLUMN referencing
    # a measure, this is SUM()/AVERAGE()/.../DISTINCTCOUNT() -- which DAX requires a COLUMN
    # argument for -- wrapping a bare reference that resolves to a MEASURE. `resolve()` in
    # tmdl.py renders a promoted measure with the exact same 'Table'[Name] string shape as an
    # ordinary column, so `dax.py`'s own `_BARE_COLUMN` syntax check cannot tell them apart from
    # the resolved TEXT alone -- only `resolve_kind`, threaded through at parse time, can, and if
    # that ever regresses this is what would let SUM(measure) reach TMDL again. Desktop does not
    # reject the model at load: it fails per-visual, once opened, with "Fields that need to be
    # fixed ... cannot be found or may not be used in this expression", Underlying Error
    # Missing_References. Confirmed on a real workbook (WorkbookD,
    # `Amount Decline Rate` = `DIVIDE(SUM('tbl_d'[Amount Declined]), ...)`,
    # where `Amount Declined` has no aggregate of its own and was promoted to a measure by
    # `_aggregate_map`'s fixpoint) -- confirmed corpus-wide across 3 of 6 reference workbooks (135
    # instances) before `dax.py`'s `func_call` was fixed to decline this shape instead of
    # emitting it.
    for table in model_objects:
        for member, hits in _member_agg_bare_refs(md, table).items():
            for fn_name, ref_tbl, ref_name in hits:
                if ref_tbl is not None:
                    kind = model_objects.get(ref_tbl, {}).get(ref_name)
                    shown = f"'{ref_tbl}'[{ref_name}]"
                else:
                    kind = "measure" if ref_name in all_measures else None
                    shown = f"[{ref_name}]"
                if kind == "measure":
                    fail(f"table {table!r}: {member!r} calls {fn_name}({shown}) -- {shown} is a "
                         f"MEASURE, not a column, and {fn_name}() requires a column argument; "
                         f"Desktop rejects the affected visual at open with Missing_References, "
                         f"not the model at load")

    # A string literal touching `+` means the original Tableau formula used `+` for concatenation
    # -- valid-looking DAX that fails at QUERY TIME, not load time, on the first visual that
    # evaluates it. See `_literal_plus_hits`'s docstring for the real Desktop error this is.
    for tname, member, snippet in _literal_plus_hits(md):
        fail(f"table {tname!r}: {member!r} uses arithmetic + on a string literal ({snippet!r}) "
             f"-- Tableau's + means concatenation for text, DAX's + is always numeric; Desktop "
             f"fails at query time with \"Cannot convert value '' of type Text to type "
             f"Numeric/Date\", not at load time")

    for page, v, (entity, prop, kind) in refs:
        if entity not in model_objects:
            fail(f"{page}/{v}: references table {entity!r} which is not in the semantic model")
        elif prop not in model_objects[entity]:
            fail(f"{page}/{v}: references {entity}[{prop}] which is not in the semantic model")
        elif kind == "Measure" and model_objects[entity][prop] != "measure":
            warn(f"{page}/{v}: {entity}[{prop}] is bound as a Measure but the model declares a "
                 f"column")

    # -- a filter's literal must carry the SAME type as the column it filters -----------------
    # extract.py reads every Tableau filter value off an XML attribute, so it is always a `str`
    # regardless of the column's real type. pbir.py's literal writer now resolves the column's
    # actual dataType from a field index before choosing string-vs-numeric literal shape -- but
    # it did not always: it used to decide from the VALUE's own Python type, which (being always
    # `str`) meant EVERY numeric/boolean categorical filter silently degraded into a quoted
    # string literal. That is schema-valid PBIR and a type mismatch against the column's real
    # dataType. Confirmed against real Desktop, on a filter on an int64 column emitted as `'1'`
    # instead of `1L`: "This visual contains one or more filters with... type mismatches" --
    # Desktop refuses to render the visual, not just warn. 111 filters in one corpus workbook hit
    # this before it was found, from one visual a user happened to open. This is the backstop,
    # read back from the emitted files, the same way the duplicate-name check above is a backstop
    # for names.py: if pbir.py's own dtype resolution ever regresses, this still catches it.
    column_types = _tmdl_column_types(md)
    for page in sorted(on_disk):
        vdir = os.path.join(pages_dir, page, "visuals")
        for v in sorted(os.listdir(vdir) if os.path.isdir(vdir) else []):
            vp = os.path.join(vdir, v, "visual.json")
            if not os.path.isfile(vp):
                continue
            vd = json.load(open(vp, encoding="utf-8"))
            for entity, prop, lit_val in _iter_filter_literals(vd):
                dtype = (column_types.get(entity) or {}).get(prop)
                if dtype not in ("int64", "double", "boolean"):
                    continue  # string/date/unknown: no shape to enforce without false positives
                bad = (lit_val not in ("true", "false") if dtype == "boolean"
                       else lit_val.startswith("'"))
                if bad:
                    fail(f"{page}/{v}: filter on {entity}[{prop}] (dataType {dtype}) has literal "
                         f"{lit_val!r} -- Desktop rejects the visual: \"filters with deleted "
                         f"columns, type mismatches, or other breaking modeling changes\"")

    # -- resourcePackages <-> disk ---------------------------------------
    rj = os.path.join(rd, "definition", "report.json")
    if os.path.isfile(rj):
        rep = json.load(open(rj, encoding="utf-8"))
        for pkg in rep.get("resourcePackages") or []:
            base = os.path.join(rd, "StaticResources", pkg["name"])
            for item in pkg.get("items") or []:
                if not os.path.isfile(os.path.join(base, item["path"])):
                    fail(f"report.json resourcePackages item {item['name']!r} has no file at "
                         f"StaticResources/{pkg['name']}/{item['path']}")

    # -- pbir points at the model that exists ----------------------------
    pbir_p = os.path.join(rd, "definition.pbir")
    if os.path.isfile(pbir_p):
        d = json.load(open(pbir_p, encoding="utf-8"))
        rel = ((d.get("datasetReference") or {}).get("byPath") or {}).get("path")
        if not rel:
            fail("definition.pbir has no datasetReference.byPath.path")
        elif not os.path.isdir(os.path.normpath(os.path.join(rd, rel))):
            fail(f"definition.pbir points at {rel!r} which does not exist")

    # -- every tables/*.tmdl is a real table declaration --------------------
    if not model_objects:
        fail("no table is declared in definition/tables/*.tmdl -- the model would be empty")

    # -- the theme must be declared AND present ---------------------------
    # Desktop declares its baseTheme in BOTH themeCollection and a SharedResources
    # resourcePackage, and ships the .json. Referencing a theme without shipping it was a real
    # defect: the pre-flight below only validated packages that were declared, and the missing
    # package meant nothing was checked at all.
    if os.path.isfile(rj):
        rep = json.load(open(rj, encoding="utf-8"))
        theme = (((rep.get("themeCollection") or {}).get("baseTheme") or {}).get("name"))
        if theme:
            declared = {i["name"] for p in (rep.get("resourcePackages") or [])
                        if p.get("type") == "SharedResources" for i in (p.get("items") or [])}
            if theme not in declared:
                fail(f"report.json declares baseTheme {theme!r} but no SharedResources "
                     f"resourcePackage item declares it")
            elif not os.path.isfile(os.path.join(rd, "StaticResources", "SharedResources",
                                                 "BaseThemes", f"{theme}.json")):
                fail(f"baseTheme {theme!r} is declared but "
                     f"StaticResources/SharedResources/BaseThemes/{theme}.json is missing")
    return out


def _iter_projections(vd):
    """Yield (entity, property, kind) for every field reference in a visual.json."""
    def walk(node):
        if isinstance(node, dict):
            for kind in ("Column", "Measure"):
                if kind in node and isinstance(node[kind], dict):
                    c = node[kind]
                    ent = (((c.get("Expression") or {}).get("SourceRef") or {}).get("Entity"))
                    if ent and c.get("Property"):
                        yield ent, c["Property"], kind
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)
    yield from walk(vd)


def _tmdl_structure(model_dir):
    """Per table: columns, all members, hierarchies, (hierarchy, level) pairs, has-partition.

    Read back from the emitted TMDL, not from the in-memory authority -- checking the authority
    against itself passes even when the writer never emitted the object.
    """
    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    for fn in sorted(os.listdir(tdir) if os.path.isdir(tdir) else []):
        if not fn.endswith(".tmdl"):
            continue
        table = fn[:-5]
        info = {"columns": set(), "members": set(), "hierarchies": [], "levels": [],
                "partition": False}
        cur_h = None
        for ln in open(os.path.join(tdir, fn), encoding="utf-8"):
            r = ln.rstrip("\n")
            m = re.match(r"^table\s+(.+)$", r)
            if m:
                table = _unquote_tmdl(m.group(1))
                continue
            m = re.match(r"^\tcolumn\s+(.+?)(?:\s*=.*)?$", r)
            if m:
                n = _unquote_tmdl(m.group(1))
                info["columns"].add(n)
                info["members"].add(n)
                cur_h = None
                continue
            m = re.match(r"^\tmeasure\s+(.+?)\s*=", r)
            if m:
                info["members"].add(_unquote_tmdl(m.group(1)))
                cur_h = None
                continue
            m = re.match(r"^\thierarchy\s+(.+)$", r)
            if m:
                cur_h = _unquote_tmdl(m.group(1))
                info["hierarchies"].append(cur_h)
                continue
            m = re.match(r"^\t\tlevel\s+(.+)$", r)
            if m and cur_h:
                info["levels"].append((cur_h, _unquote_tmdl(m.group(1))))
                continue
            if re.match(r"^\tpartition\s", r):
                info["partition"] = True
                cur_h = None
        out[table] = info
    return out


def _unquote_tmdl(s):
    """Strip ONE surrounding quote pair and un-double internal quotes.

    Not `.strip("'")`: a field captioned `''` serialises as ``, and stripping every quote
    character reports it as an empty name -- which is how a false positive gets mistaken for a bug.
    """
    s = s.strip()
    if len(s) >= 2 and s.startswith("'") and s.endswith("'"):
        return s[1:-1].replace("''", "'")
    return s


_DAX_REF = re.compile(r"'((?:[^']|'')*)'\[([^\]]+)\]")

# A bare `[Name]` (no quoted table prefix) is the shape `resolve_param` and every measure-to-measure
# reference in this codebase deliberately use -- NOT a typo of the qualified form. The two must be
# told apart: `'Table'[Col]` is a qualified column/measure ref, `[Name]` alone is either a same-table
# column shorthand (fine) or a measure reference (forbidden from a calculated column). Matching the
# qualified alternative FIRST means finditer consumes `'Table'[Col]` whole and never also reports its
# `[Col]` tail as a bare ref.
_BARE_DAX_REF = re.compile(r"'(?:[^']|'')*'\[[^\]]+\]|\[([^\]]+)\]")


def _column_dax_refs(model_dir, table):
    """{calculated-column name: [(ref_table, ref_name), ...]} for one table.

    Scoped to the calculated-column DECLARATION LINE and its continuation only -- a partition's M
    body can contain literal text that happens to look like 'Table'[Column] and must not be read
    as a DAX reference.
    """
    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    # Table names can contain characters a filename cannot ("Count/Items" is a real corpus table).
    # `tmdl_mod._safe_file` is the ONE function that maps a table name to its filename -- reused
    # here rather than duplicated, so the two can never drift apart.
    fn = os.path.join(tdir, f"{tmdl_mod.TmdlModel._safe_file(table)}.tmdl")
    if not os.path.isfile(fn):
        return out
    cur = None
    for ln in open(fn, encoding="utf-8"):
        r = ln.rstrip("\n")
        m = re.match(r"^\tcolumn\s+(.+?)\s*=\s*(.*)$", r)
        if m:
            cur = _unquote_tmdl(m.group(1))
            out[cur] = _DAX_REF.findall(m.group(2))
            continue
        if cur and r.startswith("\t\t\t"):
            out[cur] += _DAX_REF.findall(r)
            continue
        cur = None
    return out


def _column_bare_dax_refs(model_dir, table):
    """{calculated-column name: [bare name, ...]} -- the `[Name]` shape, same scoping as
    `_column_dax_refs`. See `_BARE_DAX_REF` for why this must be a separate pass, not a tweak to
    that regex."""
    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    fn = os.path.join(tdir, f"{tmdl_mod.TmdlModel._safe_file(table)}.tmdl")
    if not os.path.isfile(fn):
        return out
    cur = None
    for ln in open(fn, encoding="utf-8"):
        r = ln.rstrip("\n")
        m = re.match(r"^\tcolumn\s+(.+?)\s*=\s*(.*)$", r)
        if m:
            cur = _unquote_tmdl(m.group(1))
            out[cur] = [g for g in _BARE_DAX_REF.findall(m.group(2)) if g]
            continue
        if cur and r.startswith("\t\t\t"):
            out[cur] += [g for g in _BARE_DAX_REF.findall(r) if g]
            continue
        cur = None
    return out


# Aggregate functions DAX requires a COLUMN argument for -- Tableau's own aggregates map onto
# these (see dax.py's AGG_ITERATOR/NO_ITERATOR). Kept as its own list rather than importing
# dax.py's, since this one only needs to name the DAX-side functions to scan emitted TEXT for,
# not decide anything about translation.
_AGG_FUNCS_PREFLIGHT = ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT",
                        "MEDIAN", "STDEV\\.S", "VAR\\.S")
_AGG_BARE_REF = re.compile(
    r"\b(?P<fn>" + "|".join(_AGG_FUNCS_PREFLIGHT) + r")\s*\(\s*"
    r"(?:'(?P<qtbl>(?:[^']|'')*)'\[(?P<qname>[^\]]+)\]|\[(?P<bname>[^\]]+)\])\s*\)"
)


def _member_agg_bare_refs(model_dir, table):
    """{member name (column OR measure): [(fn, ref_table_or_None, ref_name), ...]} for one table
    -- every aggregate-function call in that member's own body wrapping a BARE reference
    ('Table'[Name] or unqualified [Name]), scoped to the declaration line and its continuation
    only, same discipline as `_column_dax_refs`.

    Unlike `_column_dax_refs` (calculated columns only), this covers MEASURES too: the shape this
    exists to catch is a MEASURE's own formula aggregating a bare reference that turns out to
    itself be a measure -- SUM('Table'[PromotedMeasure]) -- which is invalid DAX (aggregate
    functions require a column argument) and which Desktop only reports once a user opens the
    affected visual, as Missing_References. See the caller in `preflight()` for the real Desktop
    error this backstops, confirmed on a real workbook (WorkbookD).
    """
    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    fn = os.path.join(tdir, f"{tmdl_mod.TmdlModel._safe_file(table)}.tmdl")
    if not os.path.isfile(fn):
        return out
    cur, buf = None, []

    def flush():
        if cur is None:
            return
        body = "\n".join(buf)
        for m in _AGG_BARE_REF.finditer(body):
            ref_tbl = m.group("qtbl").replace("''", "'") if m.group("qtbl") is not None else None
            ref_name = m.group("qname") if m.group("qname") is not None else m.group("bname")
            out.setdefault(cur, []).append((m.group("fn"), ref_tbl, ref_name))

    for ln in open(fn, encoding="utf-8"):
        r = ln.rstrip("\n")
        m = re.match(r"^\t(?:column|measure)\s+(.+?)\s*=\s*(.*)$", r)
        if m:
            flush()
            cur, buf = _unquote_tmdl(m.group(1)), [m.group(2)]
            continue
        if cur and r.startswith("\t\t\t"):
            buf.append(r)
            continue
        flush()
        cur, buf = None, []
    flush()
    return out


_LITERAL_PLUS = re.compile(r'"(?:[^"]|"")*"\s*\+|\+\s*"(?:[^"]|"")*"')


def _literal_plus_hits(model_dir):
    """[(table, member, snippet), ...] -- a string literal directly touching `+` in a
    column/measure body.

    DAX's `+` is always arithmetic; a literal string on either side means the ORIGINAL Tableau
    formula used `+` for concatenation, which `dax.py`'s `add_expr` now always rewrites to `&`.
    This shape should therefore never appear in emitted DAX -- if it does, that rewrite regressed.
    Confirmed on a real workbook (WorkbookB Dashboard): `COALESCE(a, "") + "|" + COALESCE(b, "")`
    loaded and built fine, then failed at QUERY TIME on the first visual that evaluated it --
    "Cannot convert value '' of type Text to type Numeric/Date" -- invisible to every check that
    only looks at whether the DAX is syntactically valid, since `+` between text and a number IS
    syntactically valid DAX, just wrong at runtime.
    """
    hits = []
    tdir = os.path.join(model_dir, "definition", "tables")
    for fn in sorted(os.listdir(tdir) if os.path.isdir(tdir) else []):
        if not fn.endswith(".tmdl"):
            continue
        table, cur, buf = fn[:-5], None, []
        path = os.path.join(tdir, fn)
        lines = open(path, encoding="utf-8").readlines()
        for ln in lines:
            m = re.match(r"^table\s+(.+)$", ln.rstrip("\n"))
            if m:
                table = _unquote_tmdl(m.group(1))
        for ln in lines:
            r = ln.rstrip("\n")
            m = re.match(r"^\t(?:column|measure)\s+(.+?)\s*=\s*(.*)$", r)
            if m:
                if cur:
                    hits += [(table, cur, s) for s in _LITERAL_PLUS.findall("\n".join(buf))]
                cur, buf = _unquote_tmdl(m.group(1)), [m.group(2)]
                continue
            if cur and r.startswith("\t\t\t"):
                buf.append(r)
                continue
            if cur:
                hits += [(table, cur, s) for s in _LITERAL_PLUS.findall("\n".join(buf))]
            cur, buf = None, []
        if cur:
            hits += [(table, cur, s) for s in _LITERAL_PLUS.findall("\n".join(buf))]
    return hits


def _tmdl_member_lines(model_dir):
    """{table name: [member name, ...]} read back from the emitted TMDL, duplicates preserved.

    `_tmdl_objects()` below collapses same-named members into one dict entry -- exactly the
    detail a same-table collision check needs preserved. Same line-matching rules as that
    function, kept in sync deliberately rather than shared, since diverging here would go
    unnoticed (this function's whole job is to see what that one hides).
    """
    def unquote(s):
        s = s.strip()
        return s[1:-1].replace("''", "'") if s.startswith("'") and s.endswith("'") else s

    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    for fn in sorted(os.listdir(tdir) if os.path.isdir(tdir) else []):
        if not fn.endswith(".tmdl"):
            continue
        table, members = None, []
        for ln in open(os.path.join(tdir, fn), encoding="utf-8"):
            m = re.match(r"^table\s+(.+)$", ln.rstrip("\n"))
            if m:
                table = unquote(m.group(1))
                continue
            m = re.match(r"^\tcolumn\s+(.+?)(?:\s*=.*)?$", ln.rstrip("\n"))
            if m:
                members.append(unquote(m.group(1)))
                continue
            m = re.match(r"^\tmeasure\s+(.+?)\s*=", ln.rstrip("\n"))
            if m:
                members.append(unquote(m.group(1)))
        if table:
            out[table] = members
    return out


def _tmdl_objects(model_dir):
    """{table name: {member name: 'column'|'measure'}} read back from the emitted TMDL.

    Read from the FILES, not from the in-memory Name Authority. Checking the authority against
    itself would pass even if the writer never emitted the object.
    """
    def unquote(s):
        s = s.strip()
        return s[1:-1].replace("''", "'") if s.startswith("'") and s.endswith("'") else s

    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    for fn in sorted(os.listdir(tdir) if os.path.isdir(tdir) else []):
        if not fn.endswith(".tmdl"):
            continue
        table, members = None, {}
        for ln in open(os.path.join(tdir, fn), encoding="utf-8"):
            m = re.match(r"^table\s+(.+)$", ln.rstrip("\n"))
            if m:
                table = unquote(m.group(1))
                continue
            # Both forms must be recognised: a plain `column X` and a CALCULATED column
            # `column X = <dax>`. Matching only the first silently lost every calculated column,
            # so the PBIR reference check then failed on objects that were actually present.
            m = re.match(r"^\tcolumn\s+(.+?)(?:\s*=.*)?$", ln.rstrip("\n"))
            if m:
                members[unquote(m.group(1))] = "column"
                continue
            m = re.match(r"^\tmeasure\s+(.+?)\s*=", ln.rstrip("\n"))
            if m:
                members[unquote(m.group(1))] = "measure"
        if table:
            out[table] = members
    return out


def _tmdl_column_types(model_dir):
    """{table name: {column name: dataType}} read back from the emitted TMDL.

    Measures are excluded -- only a physical/calculated COLUMN carries a `dataType` a filter
    literal must agree with. Kept separate from `_tmdl_objects` rather than folding dataType into
    its 'column'|'measure' value, so that function's existing callers are untouched.
    """
    def unquote(s):
        s = s.strip()
        return s[1:-1].replace("''", "'") if s.startswith("'") and s.endswith("'") else s

    out = {}
    tdir = os.path.join(model_dir, "definition", "tables")
    for fn in sorted(os.listdir(tdir) if os.path.isdir(tdir) else []):
        if not fn.endswith(".tmdl"):
            continue
        table, types, cur = None, {}, None
        for ln in open(os.path.join(tdir, fn), encoding="utf-8"):
            r = ln.rstrip("\n")
            m = re.match(r"^table\s+(.+)$", r)
            if m:
                table = unquote(m.group(1))
                continue
            m = re.match(r"^\tcolumn\s+(.+?)(?:\s*=.*)?$", r)
            if m:
                cur = unquote(m.group(1))
                continue
            if re.match(r"^\t(?:measure|partition|hierarchy)\s", r):
                cur = None
                continue
            if cur is not None:
                m = re.match(r"^\t\tdataType:\s*(\S+)", r)
                if m:
                    types[cur] = m.group(1)
                    cur = None
        if table:
            out[table] = types
    return out


def _iter_filter_literals(vd):
    """Yield (entity, prop, literal_value_string) for every Column-targeted filter literal in one
    visual's filterConfig."""
    def walk(node):
        if isinstance(node, dict):
            lit = node.get("Literal")
            if isinstance(lit, dict) and "Value" in lit:
                yield str(lit["Value"])
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    for flt in (vd.get("filterConfig") or {}).get("filters") or []:
        col = (flt.get("field") or {}).get("Column")
        if not isinstance(col, dict):
            continue
        entity = ((col.get("Expression") or {}).get("SourceRef") or {}).get("Entity")
        prop = col.get("Property")
        if not (entity and prop):
            continue
        for lit_val in walk(flt.get("filter") or {}):
            yield entity, prop, lit_val


# ---------------------------------------------------------------- reports
def residue_md(model, report, ir):
    by_tier = collections.defaultdict(list)
    for i in model.residue.items:
        by_tier[i["tier"]].append(i)
    lines = ["# Residue — everything the deterministic tiers did not translate", "",
             "Each entry is a real object in the Tableau source that has **no** counterpart in "
             "the emitted PBIP. Nothing here was approximated or emitted as a placeholder: a "
             "silently-wrong measure loads cleanly and gives the user an incorrect number, which "
             "is worse than a missing one.", ""]
    TIER_DESC = {
        "D+": "the deterministic formula parser declined it",
        "L1": "level-of-detail expression — needs a `CALCULATE`/`ALLEXCEPT` restructure (LLM tier L1)",
        "L2": "irregular or long-tail calculation (LLM tier L2)",
        "L4": "intent classification needed (LLM tier L4)",
        "H": "human decision, cacheable by signature (tier H)",
        "D": "deterministic rule could not resolve its inputs",
        "GAP": "no Power BI construct exists",
    }
    for tier in sorted(by_tier):
        items = by_tier[tier]
        lines.append(f"## Tier {tier} — {len(items)} item(s)")
        lines.append("")
        lines.append(f"_{TIER_DESC.get(tier, 'unclassified')}_")
        lines.append("")
        lines.append("| Table | Object | Reason | Tableau formula |")
        lines.append("|---|---|---|---|")
        for i in sorted(items, key=lambda x: (x["table"], str(x["subject"]))):
            f = (i["formula"] or "").replace("|", "\\|").replace("\n", " ")
            f = (f[:110] + "…") if len(f) > 110 else f
            reason = i["reason"].replace("|", "\\|")
            lines.append(f"| {i['table']} | {i['subject']} | {reason} | "
                         f"{('`' + f + '`') if f else '—'} |")
        lines.append("")
    if report.review:
        lines += ["## Visual type undecided — %d view(s)" % len(report.review), "",
                  "No rule fired, so `tableEx` was emitted. A table shows every field that was "
                  "on the shelves and is obviously unfinished; a plausible-looking `barChart` "
                  "would hide the fact that nothing decided it.", "",
                  "| Page | Worksheet | Tableau mark | Emitted | Why no rule fired |",
                  "|---|---|---|---|---|"]
        for r in report.review:
            lines.append(f"| {r['page']} | {r['worksheet']} | {r['mark']} | `{r['emitted']}` | "
                         f"{r['reason']} |")
        lines.append("")
    if report.gaps:
        lines += ["## Zones with no Power BI construct — %d" % len(report.gaps), "",
                  "| Page | Zone | Reason |", "|---|---|---|"]
        for g in report.gaps:
            lines.append(f"| {g['page']} | {g['zone']} | {g['reason']} |")
        lines.append("")
    if report.declined_encodings:
        lines += ["## Color/Size encodings not reproduced — %d" %
                  len(report.declined_encodings), "",
                  "A visual still renders with its axis and values intact; only the legend/size "
                  "split from this field is missing.", "",
                  "| Page | Visual | Field | Reason |", "|---|---|---|---|"]
        for d in report.declined_encodings:
            lines.append(f"| {d['page']} | {d['visual']} | {d['field']} | {d['reason']} |")
        lines.append("")
    lines += _not_translated(ir, report)
    if model.notes:
        lines += ["## Structural notes", ""]
        for n in model.notes:
            lines.append(f"- {n}")
        lines.append("")
    if ir["unresolved_proxies"]:
        lines += ["## Unresolved published datasources", "",
                  "A `sqlproxy` connection means the workbook holds no physical source, only the "
                  "address of a datasource published on Tableau Server. Without the sibling "
                  "`.tds`/`.tdsx` there is no schema to extract — pass `--tds <dir>`.", ""]
        for p in ir["unresolved_proxies"]:
            lines.append(f"- `{p}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def _not_translated(ir, report):
    """Every asset class that was EXTRACTED and does not reach an output file.

    This section exists because it was missing, and its absence was a real defect rather than an
    omission of detail. extract.py promises "nothing is dropped silently", and 2423 worksheet
    filters, 64 dashboard actions and 19 alias sets were being dropped with no entry anywhere --
    while COVERAGE.md reported 100% on pages and views. A visual whose filters vanished renders
    unfiltered data, which is a plausible-looking wrong number: exactly what the fail-closed rule
    is for. An audit of the IR against the writers found it; reading the code did not.
    """
    lines = ["## Extracted but not translated", "",
             "Counted here so the shortfall is visible. An empty row means the workbook had none.",
             ""]

    if report.filter_stats:
        emitted = report.filter_stats.get("emitted", 0)
        total = sum(report.filter_stats.values())
        lines += [f"### Worksheet filters — {emitted} of {total} emitted", "",
                  "Tableau writes several different things through one `<filter>` element. Most of "
                  "the shortfall below is not a translation failure: `level-members` restricts "
                  "nothing, and an action-derived filter belongs to `visualInteractions` rather "
                  "than to a visual filter.", "",
                  "Counted **per visual placement**, not per source element: a worksheet shown on "
                  "three dashboards becomes three visuals, each needing its own `filterConfig`, so "
                  "this total is larger than the number of `<filter>` elements in the `.twb`.", "",
                  "| Disposition | Count |", "|---|---|"]
        for k, n in report.filter_stats.most_common():
            label = "**emitted as a PBIR visual filter**" if k == "emitted" else k
            lines.append(f"| {label} | {n} |")
        lines.append("")
        if report.context_filters:
            lines += [f"{len(report.context_filters)} of the emitted filters are Tableau CONTEXT "
                      "filters. Power BI has no filter type that restricts what *other* filters "
                      "see, so they are emitted as ordinary filters — correct for the visual's own "
                      "result, but any dependent percent-of-total is computed over a different "
                      "denominator.", ""]

    rows = [
        ("Dashboard actions", len(ir["actions"]),
         "filter/highlight/URL actions. A filter action maps to `visualInteractions` on `page.json`"
         " (`\"DataFilter\"`); a URL action has no Power BI counterpart"),
        ("Alias sets (member value renames)", sum(len(t["aliases"]) for t in ir["tables"]),
         "Tableau renames a displayed value without changing the data; Power BI needs either a "
         "mapped column or a translation"),
        ("Datasource-level filters", sum(len(t["datasource_filters"]) for t in ir["tables"]),
         "would become a Power Query step or a model-level filter"),
        ("Initial SQL blocks", sum(len(t["initial_sql"]) for t in ir["tables"]),
         "Power Query has no connect-time hook — no target exists"),
        ("Extract specifications", sum(1 for t in ir["tables"] if t["extract"]),
         "refresh schedule and extract filters; a `.hyper` file cannot be read by Power Query"),
        ("Shared (saved) views", len(ir["shared_views"]),
         "closest counterpart is a Power BI bookmark"),
        ("Internal pseudo-columns dropped", sum(len(t["internal_dropped"]) for t in ir["tables"]),
         "`__tableau_internal_object_id__` and `:Measure Names` — plumbing, not data"),
    ]
    lines += ["### Other asset classes", "", "| Asset | Count | Why it is not emitted |",
              "|---|---|---|"]
    for name, n, why in rows:
        lines.append(f"| {name} | {n} | {why} |")
    lines += ["", "Never extracted at all, so not counted anywhere: sorts, number/date format "
              "strings, colours and fonts, conditional formatting, row-level security roles.", ""]
    return lines


def coverage_md(ir, model, report, checks):
    src_calcs = sum(len(t["calcs"]) for t in ir["tables"])
    src_cols = sum(len(t["columns"]) for t in ir["tables"])
    emitted_calcs = model.stats["measures"] + model.stats["calc_columns"]
    ws_in_dash = sum(1 for db in ir["dashboards"] for z in db["zones"] if z["kind"] == "worksheet")
    other_zones = sum(1 for db in ir["dashboards"] for z in db["zones"]
                      if z["kind"] in pbir_mod.ZONE_VISUAL)

    def pct(a, b):
        return "—" if not b else f"{100.0 * a / b:.1f}%"

    fails = [m for lvl, m in checks if lvl == "FAIL"]
    warns = [m for lvl, m in checks if lvl == "WARN"]
    lines = [
        f"# Build coverage — {ir['workbook']}", "",
        "Every fraction is **of what the source had**, not of what was attempted. A build that "
        "declines 30 calcs and emits 100 has 77% coverage, not 100%.", "",
        "## Semantic model", "",
        "| Source object | In source | Emitted | Coverage |", "|---|---|---|---|",
        f"| Physical columns | {src_cols} | {model.stats['columns']} | "
        f"{pct(model.stats['columns'], src_cols)} |",
        f"| Calculated fields | {src_calcs} | {emitted_calcs} | {pct(emitted_calcs, src_calcs)} |",
        f"| — as measures | — | {model.stats['measures']} | — |",
        f"| — as calculated columns | — | {model.stats['calc_columns']} | — |",
        f"| Groups | {sum(len(t['groups']) for t in ir['tables'])} | {model.stats['groups']} | "
        f"{pct(model.stats['groups'], sum(len(t['groups']) for t in ir['tables']))} |",
        f"| Bins | {sum(len(t['bins']) for t in ir['tables'])} | {model.stats['bins']} | "
        f"{pct(model.stats['bins'], sum(len(t['bins']) for t in ir['tables']))} |",
        f"| Hierarchies | {sum(len(t['hierarchies']) for t in ir['tables'])} | "
        f"{model.stats['hierarchies']} | "
        f"{pct(model.stats['hierarchies'], sum(len(t['hierarchies']) for t in ir['tables']))} |",
        f"| Parameters | {len(ir['parameters'])} | {model.stats['parameters']} | "
        f"{pct(model.stats['parameters'], len(ir['parameters']))} |",
        "",
        f"Plus {model.stats['shelf_measures']} explicit measure(s) for shelf aggregates that had "
        "no calculated field behind them — Power BI has implicit measures, but PBIR can only bind "
        "to something with a name.", "",
        f"Partitions: {model.stats['stub_partitions']} stub, "
        f"{model.stats['native_partitions']} native M.", "",
        f"Relationships: {model.stats['blend_relationships']} emitted for a real, "
        "worksheet-observed Tableau data blend with a deterministic shared-column key "
        f"({model.stats['blend_key_columns']} calculated composite-key column(s) added, one per "
        "side of a multi-column key -- NOT counted as a 'calculated field' above, they have no "
        f"Tableau source), {model.stats['blend_pairs_declined']} blend pair(s) declined for "
        "having no shared column/dtype at all (see RESIDUE.md). Every emitted one is "
        "manyToMany/bothDirections: no real data exists to verify a one-sided uniqueness claim "
        "against (see relationships.tmdl and this build's own notes).", "",
        "## Report", "",
        "| Source object | In source | Emitted | Coverage |", "|---|---|---|---|",
        f"| Dashboards → pages | {len(ir['dashboards'])} | {report.stats['pages']} | "
        f"{pct(report.stats['pages'], len(ir['dashboards']))} |",
        f"| Worksheet zones → visuals | {ws_in_dash} | {report.stats['worksheet_visuals']} | "
        f"{pct(report.stats['worksheet_visuals'], ws_in_dash)} |",
        f"| — visual type decided by a rule | — | {report.stats['decided_visuals']} | — |",
        f"| — **undecided, emitted as tableEx** | — | {report.stats['review_visuals']} | — |",
        f"| Filter / parameter / text / image zones → visuals | {other_zones} | "
        f"{report.stats['control_visuals']} | "
        f"{pct(report.stats['control_visuals'], other_zones)} |",
        f"| Visuals with field wells bound | — | {report.stats['visuals_with_wells']} | — |",
        f"| Field-well projections | — | {report.stats['projections']} | — |",
        f"| — Colour/Size/Tooltip shelf projections (Series/Size/Tooltips wells) | — | "
        f"{report.stats['extra_role_projections']} | — |",
        f"| — declined as unverified against Desktop (see RESIDUE.md) | — | "
        f"{report.stats['extra_role_declined_unverified']} | — |",
        "",
        f"Dropped: {report.stats['chrome_zones_dropped']} layout/chrome zone(s), "
        f"{len(report.gaps)} zone(s) with no Power BI construct, "
        f"{report.stats['unresolved_field_refs']} shelf reference(s) that did not resolve to a "
        "model object.", "",
        "## Pre-flight", "",
        f"- **{len(fails)} FAIL** — Desktop would reject or silently mis-open the project",
        f"- {len(warns)} WARN — opens, but something is off", "",
    ]
    for m in fails:
        lines.append(f"- **FAIL** {m}")
    for m in warns:
        lines.append(f"- WARN {m}")
    if not fails and not warns:
        lines.append("All pre-flight assertions passed.")
    lines += ["", "## What this does not establish", "",
              "- **Not opened in Power BI Desktop.** Desktop requires Windows. Every claim above "
              "is structural — files exist, TMDL is tab-indented, every PBIR field reference "
              "resolves to an object the emitted TMDL actually declares. None of it is "
              "\"Desktop opened it\".",
              "- **Syntactic validity is not numerical equivalence.** A translated measure that "
              "parses and resolves may still not return what Tableau returned. Establishing that "
              "needs a data-level QA pass against both systems.",
              "- **Field-well shape is schema-doc only.** No visual in `docs/examples/` carries a "
              "`queryState`, so the projection shape is unverified against a real Desktop file. "
              f"{report.stats['projections']} projection(s) depend on it being right.",
              ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main
def build(src, out_root, tds_dir=None, partitions="stub", clean=False):
    wb = os.path.splitext(os.path.basename(src))[0]
    safe = re.sub(r'[<>:"/\\|?*\[\]]', "_", wb).strip(". ") or "Workbook"
    model_dir, report_dir = f"{safe}.SemanticModel", f"{safe}.Report"
    root = os.path.join(out_root, safe)
    if clean and os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)

    ir = extract.extract(src, os.path.join(root, "_work"), tds_dir)

    na = names_mod.NameAuthority()
    model = tmdl_mod.write_semantic_model(ir, na, os.path.join(root, model_dir), safe, partitions)
    # "4.2" and an empty settings object, matched to a Desktop-authored definition.pbism. The
    # schema reference says 4.0 is the minimum for TMDL; Desktop writes 4.2.
    _w(os.path.join(root, model_dir, "definition.pbism"),
       {"$schema": pbir_mod.SCHEMA["pbism"], "version": "4.2", "settings": {}})
    # .platform is what Fabric git integration reads. logicalId is derived from the name so a
    # rebuild does not present the item as a different artifact.
    for folder, kind in ((model_dir, "SemanticModel"), (report_dir, "Report")):
        _w(os.path.join(root, folder, ".platform"), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
                       "platformProperties/2.0.0/schema.json",
            "metadata": {"type": kind, "displayName": safe},
            "config": {"version": "2.0",
                       "logicalId": str(uuid.uuid5(tmdl_mod.NS, f"{kind}:{safe}"))}})

    report = pbir_mod.write_report(ir, na, os.path.join(root, report_dir), model_dir,
                                    relationship_pairs=model.relationship_pairs)

    _w(os.path.join(root, f"{safe}.pbip"),
       {"$schema": pbir_mod.SCHEMA["pbip"], "version": "1.0",
        "artifacts": [{"report": {"path": report_dir}}],
        "settings": {"enableAutoRecovery": True}})
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("cache.abf\nlocalSettings.json\n.pbi/localSettings.json\n_work/\n")

    # The IR and the Binding Ledger, so a later LLM/human tier resumes without re-parsing.
    _w(os.path.join(root, "_work", "ir.json"), ir)
    _w(os.path.join(root, "_work", "name_authority.json"), na.to_dict())

    checks = preflight(root, model_dir, report_dir, na)
    with open(os.path.join(root, "RESIDUE.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(residue_md(model, report, ir))
    with open(os.path.join(root, "COVERAGE.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(coverage_md(ir, model, report, checks))
    # Same data RESIDUE.md renders as prose, kept structured too. A downstream skill that needs
    # "every declined object, its tier and reason" should read this, not regex-scrape a markdown
    # table -- the table's shape is free to change for readability, this is the stable contract.
    _w(os.path.join(root, "_work", "residue.json"), {
        "calc_residue": model.residue.items,
        "visual_undecided": report.review,
        "zone_gaps": report.gaps,
        "declined_encodings": report.declined_encodings,
    })
    # Keep only the JSON handoff files. `_work` held the entire unpacked .twbx -- including
    # .hyper extracts -- which made the six output projects 2.0 GB, almost none of it output. The
    # unpacked tree is needed only DURING the build (the report writer copies images out of it).
    work = os.path.join(root, "_work")
    keep = {"ir.json", "name_authority.json", "residue.json"}
    for entry in sorted(os.listdir(work)):
        if entry in keep:
            continue
        target = os.path.join(work, entry)
        shutil.rmtree(target, ignore_errors=True) if os.path.isdir(target) else os.remove(target)
    return root, ir, model, report, checks


def main():
    ap = argparse.ArgumentParser(description="Tableau .twbx/.twb -> Power BI PBIP project")
    ap.add_argument("inputs", nargs="+", help=".twbx / .twb files, or a folder of them")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--tds", help="folder of sibling .tds/.tdsx published datasources")
    ap.add_argument("--partitions", choices=["stub", "native"], default="stub",
                    help="stub (default): typed empty table, always loads, no credentials. "
                         "native: real M for mapped connection classes -- unverified against "
                         "Desktop, and unmapped classes still fall back to stub.")
    ap.add_argument("--clean", action="store_true", help="delete each output project first")
    a = ap.parse_args()

    srcs = []
    for i in a.inputs:
        if os.path.isdir(i):
            srcs += [os.path.join(i, f) for f in sorted(os.listdir(i))
                     if f.lower().endswith((".twb", ".twbx"))]
        else:
            srcs.append(i)
    if not srcs:
        raise SystemExit("[fatal] no .twb/.twbx inputs found")

    total_fail = 0
    for s in srcs:
        try:
            root, ir, model, report, checks = build(s, a.out, a.tds, a.partitions, a.clean)
        except Exception as e:                                   # noqa: BLE001
            print(f"[FAIL] {os.path.basename(s)}: {type(e).__name__}: {e}")
            total_fail += 1
            continue
        fails = sum(1 for lvl, _ in checks if lvl == "FAIL")
        warns = sum(1 for lvl, _ in checks if lvl == "WARN")
        total_fail += fails
        src_calcs = sum(len(t["calcs"]) for t in ir["tables"])
        emitted = model.stats["measures"] + model.stats["calc_columns"]
        print(f"{'[FAIL]' if fails else '[ok]  '} {ir['workbook']}")
        print(f"        model   : {len(ir['tables'])} table(s), {model.stats['columns']} column(s),"
              f" {emitted}/{src_calcs} calc(s), {model.stats['shelf_measures']} shelf measure(s),"
              f" {model.stats['parameters']} parameter(s)")
        print(f"        report  : {report.stats['pages']} page(s), "
              f"{report.stats['worksheet_visuals']} view(s) "
              f"({report.stats['decided_visuals']} typed by rule + "
              f"{report.stats['review_visuals']} undecided), "
              f"{report.stats['control_visuals']} control/text/image, "
              f"{report.stats['projections']} projection(s), "
              f"{report.filter_stats.get('emitted', 0)}/"
              f"{sum(report.filter_stats.values())} filter(s)")
        print(f"        residue : {model.stats['residue']} "
              f"({dict(model.residue.by_tier())})")
        print(f"        preflight: {fails} FAIL, {warns} WARN   -> {root}")
        for lvl, m in checks:
            if lvl == "FAIL":
                print(f"          FAIL {m}")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
