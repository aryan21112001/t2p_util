#!/usr/bin/env python3
"""Lint a generated PBIP project for the failures Power BI Desktop reports badly or not at all.

Why this exists
---------------
Every bug in the list below was schema-valid JSON. Validating our output against
Microsoft's published JSON schemas passed 32/32 files clean while Power BI Desktop
still refused to render the report, because the schemas constrain shape, not meaning:
`version.json`'s `version` is checked only against a regex, `resourcePackages` is
optional, and nothing in any schema knows that `Value.NativeQuery(null, ...)` is an
invalid M expression.

Worse, Desktop's own diagnostics for these are actively misleading. Wrong
`version.json` produces no error at all -- the report just opens with zero page tabs.
A missing V3 marker surfaces as "Value cannot be null. Parameter name: Non-null
assertion failure: query", which points nowhere near the actual cause.

So each check here encodes one concretely-observed failure, with the symptom it
produces recorded alongside it. Run this on every generated project before handing it
to anyone, and these cannot silently regress.

None of that means JSON-Schema validation is useless -- it is the only thing that catches a
value outside a closed enum, and Desktop refuses to open a project at all when it hits one
(`"type": "Filter"` where VisualInteractionFilterType allows only Default / DataFilter /
HighlightFilter / NoFilter). So `--schemas` runs that pass too. The two are complementary:
schemas catch illegal shapes and values, the checks above catch legal-but-wrong ones.

Run `--schemas` over EVERY workbook, not a sample. The Filter/DataFilter bug survived a clean
32/32 validation run because the one project checked happened to have no visualInteractions.

Usage
-----
    python3 tools/pbip_lint.py <path>            # a project folder, or a parent of many
    python3 tools/pbip_lint.py <path> --quiet    # only print failures
    python3 tools/pbip_lint.py <path> --schemas  # also validate against MS JSON schemas

Exits non-zero if any check fails. The default mode needs no third-party packages and no
network. `--schemas` additionally needs `jsonschema` and network access to fetch the schemas
(they are cached under the system temp dir after first use).

Ground truth for the expected values is docs/blankreporttwtabs.pbix, a two-page PBIR report
with an image visual, saved by Power BI Desktop 2.151.377.0 (release 2026.02).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# `pbip_rules` is the sibling package tools/pbip_rules/ -- checks.py's 38 checks operate on a
# properly-parsed model.Project (real tables/measures/columns/relationships/pages/visuals)
# rather than regex-on-text, so they are wired in here rather than duplicated. This import is
# NOT circular: registry.py's own `_lint_sync_check` cross-references this file by AST, not by
# importing it back.
from pbip_rules import model as _prm
from pbip_rules import checks as _prc

# definition/version.json must say exactly this. "1.0.0" is equally schema-valid but makes
# Desktop apply an older required-file set that never loads definition/pages/ -- the report
# opens with the semantic model fully intact and ZERO page tabs, with no error message.
EXPECTED_REPORT_DEFINITION_VERSION = "2.0.0"

# definition.pbir / definition.pbism must say "4.0" or higher. At "1.0" the report definition
# is only allowed to be PBIR-Legacy (a flat report.json), so a definition/ folder is ignored.
EXPECTED_ITEM_DEFINITION_VERSION = "4.0"

# Both the TMSL and TMDL semantic model writers must emit this. Without it Desktop loads the
# model as legacy "PowerBI_V1" metadata and fails with either "A data model with version 3 of
# metadata is required" or a null-reference crash inside its V3-assuming query-load path.
V3_MARKER = "powerBI_V3"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}


class Findings:
    """Collects failures and notes, keyed by the project they were found in."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, project: str, check: str, detail: str) -> None:
        self.failures.append(f"[{project}] {check}: {detail}")

    def note(self, project: str, check: str, detail: str) -> None:
        self.notes.append(f"[{project}] {check}: {detail}")


def load_json(path: Path):
    """Return parsed JSON, or None if the file is absent or unparseable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def check_report_definition_version(f: Findings, project: str, report_dir: Path) -> None:
    """The zero-page-tabs bug. Silent in Desktop; invisible to schema validation."""
    path = report_dir / "definition" / "version.json"
    doc = load_json(path)
    if doc is None:
        f.fail(project, "version.json", f"missing or unparseable: {path}")
        return
    found = doc.get("version")
    if found != EXPECTED_REPORT_DEFINITION_VERSION:
        f.fail(
            project, "version.json",
            f'version is "{found}", must be "{EXPECTED_REPORT_DEFINITION_VERSION}" -- '
            f"Desktop will open the report with ZERO page tabs and no error message",
        )


def check_item_definition_versions(f: Findings, project: str, root: Path) -> None:
    """definition.pbir / definition.pbism must allow the PBIR + TMSL/TMDL formats we emit."""
    for pattern, label in ((".Report/definition.pbir", "definition.pbir"),
                           (".SemanticModel/definition.pbism", "definition.pbism")):
        for path in root.glob(f"*{pattern}"):
            doc = load_json(path)
            if doc is None:
                f.fail(project, label, f"missing or unparseable: {path}")
                continue
            version = str(doc.get("version", ""))
            major = version.split(".")[0]
            if not major.isdigit() or int(major) < int(EXPECTED_ITEM_DEFINITION_VERSION.split(".")[0]):
                f.fail(
                    project, label,
                    f'version is "{version}", must be "{EXPECTED_ITEM_DEFINITION_VERSION}" or higher '
                    f"or the definition/ folder is ignored in favour of a flat report.json",
                )


def check_dataset_reference(f: Findings, project: str, report_dir: Path) -> None:
    """A byPath datasetReference pointing at a folder that isn't there loads no model at all."""
    doc = load_json(report_dir / "definition.pbir")
    if doc is None:
        return
    by_path = (doc.get("datasetReference") or {}).get("byPath") or {}
    rel = by_path.get("path")
    if not rel:
        return  # byConnection is legitimate; nothing local to verify
    if not (report_dir / rel).resolve().is_dir():
        f.fail(project, "datasetReference",
               f'byPath "{rel}" does not resolve to a folder relative to {report_dir.name}')


def check_semantic_model_v3(f: Findings, project: str, root: Path) -> None:
    """The missing-V3-marker bug, in both serialisations."""
    for sm_dir in root.glob("*.SemanticModel"):
        bim = sm_dir / "model.bim"
        tmdl = sm_dir / "definition" / "model.tmdl"
        if bim.exists():
            doc = load_json(bim)
            if doc is None:
                f.fail(project, "model.bim", "unparseable JSON")
            elif (doc.get("model") or {}).get("defaultPowerBIDataSourceVersion") != V3_MARKER:
                f.fail(project, "model.bim",
                       f'model.defaultPowerBIDataSourceVersion must be "{V3_MARKER}" -- '
                       f'without it Desktop reports "A data model with version 3 of metadata is required"')
        elif tmdl.exists():
            if V3_MARKER not in tmdl.read_text(encoding="utf-8"):
                f.fail(project, "model.tmdl",
                       f"missing 'defaultPowerBIDataSourceVersion: {V3_MARKER}' -- "
                       f'without it Desktop crashes with "Value cannot be null ... assertion failure: query"')
        else:
            f.fail(project, "semantic model", f"neither model.bim nor definition/model.tmdl found in {sm_dir.name}")


def model_table_names(sm_dir: Path) -> set[str]:
    """Table names declared by the semantic model, from either serialisation."""
    bim = load_json(sm_dir / "model.bim")
    if bim is not None:
        return {t.get("name", "") for t in (bim.get("model") or {}).get("tables") or []}
    names = set()
    for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
        match = re.search(r"^table\s+(.+)$", tmdl.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            names.add(match.group(1).strip().strip("'").replace("''", "'"))
    return names


_TMDL_MEMBER_DECL = re.compile(r"^\t(?:column|measure)\s+(.*)$")


def _tmdl_declared_name(rest: str) -> str:
    """Pull just the declared name out of a `column '...' [= ...]` / `measure '...' = ...` line,
    ignoring everything from the `=` (the expression) onward.

    An unquoted TMDL identifier may contain any character except whitespace, `.`, `=`, `:` or
    `'` (those force quoting -- see names.py's `_NEEDS_QUOTE` / tmdl_writer.py's `_q`), so `/`,
    `%`, `-`, parens etc. are all legal bare-name characters. A `\\w+`-only match truncates a
    real name like `Token/Source` down to `Token` and reports a false collision against an
    unrelated column actually named `Token`.
    """
    rest = rest.strip()
    if not rest.startswith("'"):
        match = re.match(r"[^\s=]+", rest)
        return match.group(0) if match else rest
    buf: list[str] = []
    i = 1
    while i < len(rest):
        if rest[i] == "'":
            if i + 1 < len(rest) and rest[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            break
        buf.append(rest[i])
        i += 1
    return "".join(buf)


def table_member_names(sm_dir: Path) -> dict[str, list[str]]:
    """table name -> [every column/measure name declared on it], from either serialisation."""
    bim = load_json(sm_dir / "model.bim")
    if bim is not None:
        return {
            t.get("name", "?"): [
                obj.get("name", "") for kind in ("columns", "measures") for obj in t.get(kind) or []
            ]
            for t in (bim.get("model") or {}).get("tables") or []
        }
    result: dict[str, list[str]] = {}
    for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
        names: list[str] = []
        for line in tmdl.read_text(encoding="utf-8").splitlines():
            match = _TMDL_MEMBER_DECL.match(line)
            if match:
                names.append(_tmdl_declared_name(match.group(1)))
        result[tmdl.stem] = names
    return result


_TMDL_COLUMN_DECL = re.compile(r"^\tcolumn\s+(.*)$")
_TMDL_STOP_DECL = re.compile(r"^\t(?:measure|partition|hierarchy|calculationGroup)\s")
_TMDL_DATATYPE_LINE = re.compile(r"^\t\tdataType:\s*(\S+)")


def column_datatypes(sm_dir: Path) -> dict[str, dict[str, str]]:
    """table -> {column_name: dataType}, from either serialisation. Measures are excluded --
    only physical/calculated COLUMNS carry a dataType a filter literal must match."""
    bim = load_json(sm_dir / "model.bim")
    if bim is not None:
        return {
            t.get("name", "?"): {c.get("name", ""): c.get("dataType", "")
                                  for c in t.get("columns") or []}
            for t in (bim.get("model") or {}).get("tables") or []
        }
    result: dict[str, dict[str, str]] = {}
    for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
        text = tmdl.read_text(encoding="utf-8")
        table_match = re.search(r"^table\s+(.+)$", text, re.MULTILINE)
        table_name = (table_match.group(1).strip().strip("'").replace("''", "'")
                      if table_match else tmdl.stem)
        types: dict[str, str] = {}
        current = None
        for line in text.splitlines():
            col_match = _TMDL_COLUMN_DECL.match(line)
            if col_match:
                current = _tmdl_declared_name(col_match.group(1))
                continue
            if _TMDL_STOP_DECL.match(line):
                current = None
                continue
            if current is not None:
                dt_match = _TMDL_DATATYPE_LINE.match(line)
                if dt_match:
                    types[current] = dt_match.group(1)
                    current = None
        result[table_name] = types
    return result


def _iter_literal_values(node):
    """Every `{"Literal": {"Value": "..."}}` string found anywhere under `node`."""
    if isinstance(node, dict):
        lit = node.get("Literal")
        if isinstance(lit, dict) and "Value" in lit:
            yield str(lit["Value"])
        for v in node.values():
            yield from _iter_literal_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_literal_values(v)


# TOM's real DataType enum. "date" is deliberately absent: confirmed against real Desktop, it is
# schema-valid TEXT but not a real enum member -- "Failed to convert the value 'date' to the
# expected type DataType!" -- a hard model-load failure for the WHOLE project, not one column.
# docs/schema-reference/pbip-schema.md's own "Column Data Types" table claims `date` is valid;
# it is not. Only the values either writer actually emits are listed here.
TOM_DATA_TYPES = {"string", "int64", "double", "decimal", "boolean", "dateTime"}


def check_valid_data_types(f: Findings, project: str, root: Path) -> None:
    """Every column's `dataType` must be a real TOM enum member, not just schema-valid text.

    Neither JSON-Schema validation nor any structural check here (indentation, references
    resolving, name collisions) catches this: `dataType: date` parses fine as TMDL and matches
    no closed enum a schema could enforce. Only Desktop's own TOM deserializer knows the real
    enum, and it refuses the whole model rather than warn.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        for table, cols in column_datatypes(sm_dir).items():
            for col, dtype in cols.items():
                if dtype and dtype not in TOM_DATA_TYPES:
                    f.fail(project, "invalid dataType",
                           f"table '{table}', column '{col}': dataType {dtype!r} is not a real "
                           f"TOM DataType (valid: {', '.join(sorted(TOM_DATA_TYPES))}) -- Desktop "
                           f"will refuse to load the model")


# M type names that mean "this column is NOT text" -- a quoted string in a Table.FromRows row for
# one of these is a value/type mismatch, whatever the specific mismatch (date vs text, number vs
# text, ...). Kept as "not text" rather than an exhaustive positive-shape check per type: the
# confirmed failure mode is specifically a stray quoted string, and a narrower check that also
# tried to validate every OTHER type's own literal syntax would risk false positives on shapes
# this project hasn't seen yet.
_M_NON_TEXT_TYPES = {"datetime", "date", "logical", "Int64.Type", "number", "Number.Type"}
_M_FROM_ROWS = re.compile(
    r"Table\.FromRows\(\{\{(?P<value>.*?)\}\},\s*type table \[\w+\s*=\s*(?P<mtype>[\w.]+)\]\)")


def check_m_row_literal_types(f: Findings, project: str, root: Path) -> None:
    """A `Table.FromRows` row value must not be a quoted string when the column's own M type
    ascription says it isn't text -- e.g. `{{"2026-05-28"}}` against `type table [Value =
    datetime]`. Confirmed against real Desktop, on a real generated parameter table: "Expression.
    Error: The type of the value does not match the type of the column." This is the sibling bug
    to `check_valid_data_types` above -- that one is the TOM column's declared type, this one is
    the M partition's actual data -- and it was introduced BY the fix for the other one: making
    the TOM column correctly say `datetime` didn't also fix the value still being emitted as a
    plain quoted string. Two data points asserted in two different places with nothing checking
    they still agree; this is the check that does.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
            text = tmdl.read_text(encoding="utf-8")
            for m in _M_FROM_ROWS.finditer(text):
                mtype, value = m.group("mtype"), m.group("value").strip()
                if mtype in _M_NON_TEXT_TYPES and value.startswith('"'):
                    f.fail(project, "M row literal type mismatch",
                           f"{tmdl.name}: Table.FromRows row value {value} is a quoted string "
                           f"but the column type is {mtype!r} -- Desktop will reject this at "
                           f'refresh: "The type of the value does not match the type of the '
                           f'column"')


def check_filter_literal_types(f: Findings, project: str, root: Path) -> None:
    """A visual-level filter's literal value must carry the SAME type as the column it filters --
    PBIR encodes a string literal single-quoted inside the JSON string and a numeric/boolean one
    bare (with an L/D suffix or true/false), and Desktop enforces that the two agree. A value
    that started life as Tableau XML attribute text is a Python `str` regardless of the column's
    real type, so any writer that trusts the value's own Python type instead of the column's
    declared dataType silently emits a string literal for a numeric/boolean column. Confirmed
    against real Desktop: exactly this produced "This visual contains one or more filters with
    ... type mismatches" and the visual refused to render, on a filter on an int64 column that had
    been emitted as `'1'` instead of `1L`.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        col_types = column_datatypes(sm_dir)
        for report_dir in root.glob("*.Report"):
            pages_dir = report_dir / "definition" / "pages"
            if not pages_dir.is_dir():
                continue
            for vf in sorted(pages_dir.rglob("visual.json")):
                doc = load_json(vf)
                if doc is None:
                    continue
                for flt in (doc.get("filterConfig") or {}).get("filters") or []:
                    col = (flt.get("field") or {}).get("Column")
                    if not isinstance(col, dict):
                        continue
                    entity = ((col.get("Expression") or {}).get("SourceRef") or {}).get("Entity")
                    prop = col.get("Property")
                    dtype = (col_types.get(entity) or {}).get(prop)
                    if dtype not in ("int64", "double", "boolean"):
                        continue  # string/date/unknown: no shape to enforce without false positives
                    for lit in _iter_literal_values(flt.get("filter") or {}):
                        bad = (lit not in ("true", "false") if dtype == "boolean"
                               else lit.startswith("'"))
                        if bad:
                            f.fail(project, "filter literal type mismatch",
                                   f"{vf.relative_to(root)}: filter on '{entity}'[{prop}] "
                                   f"(dataType {dtype}) has literal {lit!r} -- Desktop rejects the "
                                   f'visual: "filters with deleted columns, type mismatches, or '
                                   f'other breaking modeling changes"')


def check_duplicate_member_names(f: Findings, project: str, root: Path) -> None:
    """A column/measure name that collides only by case with another name on the same table is
    schema-valid and reads fine in a diff, but TOM's own name collection ignores case
    (Windows/SQL Server convention) -- so 'SAMPLE CHECK ALPHA' and 'Sample Check Alpha' are
    the same name to it even though they are different strings on disk. Desktop refuses to open
    the file at all: "Failed to add a deserialized Column object into the model ... Item '...'
    already exists in the collection." A real corpus workbook produced exactly this: a physical
    column captioned "Sample Check Alpha" alongside a calculated-field alias of it captioned
    "SAMPLE CHECK ALPHA".
    """
    for sm_dir in root.glob("*.SemanticModel"):
        for table_label, names in table_member_names(sm_dir).items():
            seen: dict[str, str] = {}
            for name in names:
                folded = name.casefold()
                if folded in seen:
                    prior = seen[folded]
                    detail = f"'{prior}' and '{name}'" if prior != name else f"'{name}' (twice)"
                    f.fail(project, "duplicate name",
                           f"table '{table_label}': {detail} collide case-insensitively -- Desktop "
                           f'will refuse to load with "already exists in the collection"')
                else:
                    seen[folded] = name


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.M)


def strip_dax_comments(text: str) -> str:
    """Remove DAX comments before scanning an expression.

    Both converters deliberately keep the original Tableau formula in a comment
    beside an honest `BLANK()` when they cannot translate it -- exactly the
    behaviour we want. Scanning the comment as if it were code turned that
    honesty into four blocking lint failures: `expression = BLANK() /* ... Original
    formula: IF [Parameters].[Parameter 2]='Monthly' ... */` was reported as an
    untranslated parameter reference, when the measure itself contains no such
    reference and there is nothing to fix.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub(" ", text))


def iter_dax_expressions(sm_dir: Path):
    """Yield (owner_description, dax_text) for every measure and calculated column.

    Comments are stripped -- see strip_dax_comments for why that matters."""
    bim = load_json(sm_dir / "model.bim")
    if bim is not None:
        for table in (bim.get("model") or {}).get("tables") or []:
            for kind in ("measures", "columns"):
                for obj in table.get(kind) or []:
                    expr = obj.get("expression")
                    if expr is None:
                        continue
                    text = " ".join(expr) if isinstance(expr, list) else str(expr)
                    yield f"{table.get('name')}[{obj.get('name')}]", strip_dax_comments(text)
        return
    # TMDL: pull out only the measure/column declarations. Scanning whole files would sweep in
    # `partition` blocks, whose M code and prose comments ("...and select table 'x'...") produce
    # false positives for anything looking for quoted table references.
    declaration = re.compile(r"^\t(?P<kind>measure|column|partition|hierarchy|calculationGroup)\s+(?P<name>.*)$")
    for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
        owner, buffer, keep = tmdl.name, [], False
        for line in tmdl.read_text(encoding="utf-8").splitlines():
            match = declaration.match(line)
            if match:
                if keep and buffer:
                    yield owner, strip_dax_comments("\n".join(buffer))
                buffer = []
                keep = match.group("kind") in ("measure", "column")
                owner = f"{tmdl.stem}.{match.group('name').split('=')[0].strip()}"
                if keep:
                    buffer.append(line)
            elif keep:
                buffer.append(line)
        if keep and buffer:
            yield owner, strip_dax_comments("\n".join(buffer))


def check_dax_references(f: Findings, project: str, root: Path) -> None:
    """Catch DAX that references things the model does not contain.

    Desktop surfaces these only once you click the affected visual, as
    "Fields that need to be fixed ... cannot be found or may not be used in this expression"
    with Underlying Error Missing_References -- so a project can open, show all its pages, and
    still be substantively broken. Two distinct problems:

      - `[Parameters].[X]` is Tableau syntax, not DAX, and is never valid. It means a Tableau
        parameter reference was passed through untranslated.
      - `'SomeTable'[Col]` naming a table absent from the model. Usually the flip side of the
        same gap: the DAX was rewritten to reference a what-if parameter table that nothing
        ever generated.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        known = model_table_names(sm_dir)
        tableau_refs: dict[str, int] = {}
        missing_tables: dict[str, int] = {}
        for owner, text in iter_dax_expressions(sm_dir):
            for match in re.finditer(r"\[Parameters\]\s*\.\s*\[([^\]]+)\]", text):
                tableau_refs[match.group(1)] = tableau_refs.get(match.group(1), 0) + 1
            for match in re.finditer(r"'([^']+)'\s*\[", text):
                name = match.group(1)
                if name not in known:
                    missing_tables[name] = missing_tables.get(name, 0) + 1

        for name, count in sorted(tableau_refs.items()):
            f.fail(project, "untranslated Tableau parameter",
                   f"DAX references [Parameters].[{name}] ({count}x) -- Tableau syntax, not "
                   f"valid DAX. Resolve it to a model-native reference (what-if parameter "
                   f"table, SELECTEDVALUE, or a constant); see pbip_interview.py's "
                   f"'parameter' questions")
        for name, count in sorted(missing_tables.items()):
            f.fail(project, "missing table",
                   f"DAX references table '{name}' ({count}x) which the model does not define "
                   f"(model has: {', '.join(sorted(known)) or 'no tables'})")


_CALC_COLUMN_REF = re.compile(r"'(?:[^']|'')*'\[[^\]]+\]|\[([^\]]+)\]")


def check_column_references_measure(f: Findings, project: str, root: Path) -> None:
    """A calculated COLUMN must never reference a MEASURE. DAX forbids it structurally --
    columns are computed at data-refresh time, before any measure (query-time only) exists to
    reference -- but Desktop's error names an unrelated pair of columns as "A circular dependency
    was detected", never the real cause. Two shapes both count: a qualified `'Table'[Name]` and
    the bare `[Name]` form -- the latter is what a parameter reference translates to (resolve_param
    always points at the parameter's SELECTEDVALUE measure), and it is easy to special-case away by
    accident since it looks identical to an ordinary same-table column shorthand.

    Confirmed against a real workbook (WorkbookB Dashboard): a calculated column referenced a
    parameter's measure via the bare form, the model loaded without error, and every report filter
    on that column failed in Desktop with "Broken_Filters" -- silent until a user opened that exact
    page. Measures are unique model-wide, so a bare name matching ANY measure anywhere is a hit
    regardless of which table declares the offending column.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        bim = load_json(sm_dir / "model.bim")
        if bim is not None:
            tables = (bim.get("model") or {}).get("tables") or []
            measure_names = {m.get("name", "") for t in tables for m in t.get("measures") or []}
            for t in tables:
                for c in t.get("columns") or []:
                    expr = c.get("expression")
                    if expr is None:
                        continue
                    text = " ".join(expr) if isinstance(expr, list) else str(expr)
                    for ref in _CALC_COLUMN_REF.findall(text):
                        if ref and ref in measure_names:
                            f.fail(project, "column references measure",
                                   f"table '{t.get('name')}': calculated column "
                                   f"'{c.get('name')}' references measure [{ref}] -- DAX forbids "
                                   f"a column referencing a measure; Desktop reports this as an "
                                   f"unrelated circular dependency")
            continue

        # table_member_names mixes columns and measures together -- fine for the collision check
        # it was built for, wrong here (it would make every same-table column shorthand a false
        # positive). Build a measures-ONLY set straight from the TMDL declarations instead.
        measure_names = set()
        for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
            for line in tmdl.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\tmeasure\s+(.+?)\s*=", line)
                if m:
                    measure_names.add(_tmdl_declared_name(m.group(1)))

        decl = re.compile(r"^\tcolumn\s+(?P<name>.+?)\s*=\s*(?P<body>.*)$")
        stop = re.compile(r"^\t(?:measure|partition|hierarchy|calculationGroup)\s")
        for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
            table_match = re.search(r"^table\s+(.+)$", tmdl.read_text(encoding="utf-8"),
                                     re.MULTILINE)
            table_name = (_tmdl_declared_name(table_match.group(1)) if table_match else tmdl.stem)
            cur, buf = None, []
            for line in tmdl.read_text(encoding="utf-8").splitlines():
                m = decl.match(line)
                if m:
                    if cur:
                        _check_calc_column_refs(f, project, table_name, cur, buf, measure_names)
                    cur, buf = _tmdl_declared_name(m.group("name")), [m.group("body")]
                    continue
                if stop.match(line):
                    if cur:
                        _check_calc_column_refs(f, project, table_name, cur, buf, measure_names)
                    cur, buf = None, []
                    continue
                if cur is not None and line.startswith("\t\t\t"):
                    buf.append(line)
            if cur:
                _check_calc_column_refs(f, project, table_name, cur, buf, measure_names)


def _check_calc_column_refs(f, project, table_name, column_name, body_lines, measure_names):
    text = strip_dax_comments("\n".join(body_lines))
    for ref in _CALC_COLUMN_REF.findall(text):
        if ref and ref in measure_names:
            f.fail(project, "column references measure",
                   f"table '{table_name}': calculated column '{column_name}' references measure "
                   f"[{ref}] -- DAX forbids a column referencing a measure; Desktop reports this "
                   f"as an unrelated circular dependency")


_LITERAL_PLUS = re.compile(r'"(?:[^"]|"")*"\s*\+|\+\s*"(?:[^"]|"")*"')


def _table_measures_and_columns(sm_dir: Path) -> dict[str, tuple[set[str], set[str]]]:
    """table name -> ({measure names}, {column names}), from either serialisation.

    Split out for `check_aggregate_wraps_measure`, which needs to know, PER TABLE, whether a
    given name is a measure or a column -- the same name can legitimately be a column in one
    table and coincidentally collide with a measure name declared on a DIFFERENT table, and only
    the qualifying table in the actual reference tells them apart.
    """
    bim = load_json(sm_dir / "model.bim")
    if bim is not None:
        return {
            t.get("name", "?"): (
                {m.get("name", "") for m in t.get("measures") or []},
                {c.get("name", "") for c in t.get("columns") or []},
            )
            for t in (bim.get("model") or {}).get("tables") or []
        }
    result: dict[str, tuple[set[str], set[str]]] = {}
    for tmdl in (sm_dir / "definition" / "tables").glob("*.tmdl"):
        text = tmdl.read_text(encoding="utf-8")
        table_match = re.search(r"^table\s+(.+)$", text, re.MULTILINE)
        table_name = (_tmdl_declared_name(table_match.group(1)) if table_match else tmdl.stem)
        measures, columns = set(), set()
        for line in text.splitlines():
            m = re.match(r"^\tmeasure\s+(.+?)\s*=", line)
            if m:
                measures.add(_tmdl_declared_name(m.group(1)))
                continue
            c = re.match(r"^\tcolumn\s+(.+?)\s*$", line)
            if c:
                columns.add(_tmdl_declared_name(c.group(1)))
        result[table_name] = (measures, columns)
    return result


_AGG_FUNCS_LINT = ("SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "DISTINCTCOUNT",
                    "MEDIAN", "STDEV\\.S", "VAR\\.S")
_AGG_BARE_REF = re.compile(
    r"\b(?P<fn>" + "|".join(_AGG_FUNCS_LINT) + r")\s*\(\s*"
    r"(?:'(?P<qtbl>(?:[^']|'')*)'\[(?P<qname>[^\]]+)\]|\[(?P<bname>[^\]]+)\])\s*\)"
)


def check_aggregate_wraps_measure(f: Findings, project: str, root: Path) -> None:
    """The symmetric case of `check_column_references_measure`: an aggregate function
    (SUM/AVERAGE/MIN/MAX/COUNT/COUNTA/DISTINCTCOUNT/MEDIAN/STDEV.S/VAR.S) wrapping a BARE
    reference to a MEASURE instead of a column. DAX aggregate functions require a column
    argument; when the bracketed name is a measure rather than a column, the engine looks for a
    column of that name in the qualified table, finds none, and Desktop rejects the affected
    visual only once opened, as "Fields that need to be fixed ... cannot be found or may not be
    used in this expression", Underlying Error Missing_References -- the exact same class of
    late, per-visual surfacing `check_dax_references` exists for, but from the opposite
    direction.

    Confirmed on a real workbook (WorkbookD, `Amount Decline Rate` =
    `DIVIDE(SUM('tbl_d'[Amount Declined]), ...)`, where `Amount Declined` has
    no aggregate of its own and was promoted to a measure elsewhere in the same model by the
    build's own `_aggregate_map` fixpoint) -- confirmed corpus-wide across 3 of 6 reference
    workbooks (135 instances) before the generator (`dax.py`) was fixed to decline this shape
    instead of emitting it. This check is the backstop should the generator regress.

    A bare, UNQUALIFIED `[Name]` inside an aggregate call is flagged unconditionally against
    every measure name in the model: this pipeline's `dax_ref` always table-qualifies a column
    reference, so an unqualified name in emitted DAX is never a column -- it is always a measure
    reference (see `resolve_param`'s contract) -- and no aggregate function ever legitimately
    takes one.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        per_table = _table_measures_and_columns(sm_dir)
        global_measures: set[str] = set()
        for measures, _cols in per_table.values():
            global_measures |= measures

        for owner, text in iter_dax_expressions(sm_dir):
            for m in _AGG_BARE_REF.finditer(text):
                fn = m.group("fn")
                if m.group("qname") is not None:
                    tbl = m.group("qtbl").replace("''", "'")
                    name = m.group("qname")
                    measures, cols = per_table.get(tbl, (set(), set()))
                    if name in measures and name not in cols:
                        f.fail(project, "aggregate wraps measure",
                               f"{owner}: {fn}('{tbl}'[{name}]) -- '{tbl}'[{name}] is a MEASURE, "
                               f"not a column, and {fn}() requires a column argument; Desktop "
                               f"rejects this at load with Missing_References")
                else:
                    name = m.group("bname")
                    if name in global_measures:
                        f.fail(project, "aggregate wraps measure",
                               f"{owner}: {fn}([{name}]) -- [{name}] is a MEASURE (an "
                               f"unqualified reference in this model's own convention is always "
                               f"a measure reference, never a column), and {fn}() requires a "
                               f"column argument; Desktop rejects this at load with "
                               f"Missing_References")


def check_literal_plus_concatenation(f: Findings, project: str, root: Path) -> None:
    """A string literal directly touching `+` in a measure/column body means the original Tableau
    formula used `+` for concatenation. DAX's `+` is always arithmetic -- valid-looking DAX that
    fails at QUERY TIME, not load time, on the first visual that evaluates it: "Cannot convert
    value '' of type Text to type Numeric/Date". Confirmed on a real workbook (WorkbookB Dashboard):
    `COALESCE(a, "") + "|" + COALESCE(b, "")`, a label-building concatenation. `dax.py`'s
    `add_expr` now always rewrites a `+` chain containing a literal to `&`, so this shape should
    never appear in emitted DAX -- a hit here means that rewrite regressed.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        for owner, text in iter_dax_expressions(sm_dir):
            for snippet in _LITERAL_PLUS.findall(text):
                f.fail(project, "arithmetic + on a string literal",
                       f"{owner}: uses + on a string literal ({snippet!r}) -- Tableau's + means "
                       f"concatenation for text, DAX's + is always numeric; Desktop fails at "
                       f"query time with \"Cannot convert value '' of type Text to type "
                       f"Numeric/Date\", not at load time")


def check_partitions_and_dax(f: Findings, project: str, root: Path) -> None:
    """Two placeholder bugs that make Desktop reject a table or the whole model.

    `Value.NativeQuery(null, ...)` is rejected outright ("Native queries aren't supported by
    this value") because null is not a valid connection. A literal 'Table' left in DAX is an
    unsubstituted placeholder from the LLM enrichment tier, which never sees real table names;
    it references a table that does not exist and can fail the entire model load, not just the
    one measure.
    """
    for sm_dir in root.glob("*.SemanticModel"):
        sources = list(sm_dir.glob("model.bim")) + list((sm_dir / "definition").rglob("*.tmdl"))
        for path in sources:
            text = path.read_text(encoding="utf-8")
            if "Value.NativeQuery(null" in text:
                f.fail(project, "M partition",
                       f"{path.name} contains Value.NativeQuery(null, ...) -- Desktop reports "
                       f'"Native queries aren\'t supported by this value"; use a connection-free '
                       f"#table(type table [...], {{}}) placeholder instead")
            if re.search(r"'Table'", text):
                f.fail(project, "DAX placeholder",
                       f"{path.name} contains a literal 'Table' reference -- an unsubstituted "
                       f"placeholder that can fail the whole model load")


def collect_static_resources(report_dir: Path) -> dict[str, set[str]]:
    """package folder name -> set of resource paths relative to that folder."""
    found: dict[str, set[str]] = {}
    static = report_dir / "StaticResources"
    if not static.is_dir():
        return found
    for pkg_dir in sorted(p for p in static.iterdir() if p.is_dir()):
        found[pkg_dir.name] = {
            p.relative_to(pkg_dir).as_posix() for p in pkg_dir.rglob("*") if p.is_file()
        }
    return found


def check_resource_packages(f: Findings, project: str, report_dir: Path) -> None:
    """Undeclared resource files are orphans Desktop will not load.

    The PBIR docs are explicit that "every resource file must have a corresponding entry in
    the report.json file". An image visual pointing at an undeclared file renders as an empty
    "Select Image in the format pane under Style" placeholder, and an undeclared base theme is
    simply ignored.
    """
    report_json = report_dir / "definition" / "report.json"
    doc = load_json(report_json)
    if doc is None:
        f.fail(project, "report.json", f"missing or unparseable: {report_json}")
        return

    on_disk = collect_static_resources(report_dir)
    declared: dict[str, set[str]] = {}
    for pkg in doc.get("resourcePackages") or []:
        declared.setdefault(pkg.get("name", "?"), set()).update(
            item.get("path", "") for item in pkg.get("items") or []
        )

    for pkg_name, paths in on_disk.items():
        undeclared = paths - declared.get(pkg_name, set())
        for rel in sorted(undeclared):
            f.fail(project, "resourcePackages",
                   f"{pkg_name}/{rel} exists on disk but is not declared in report.json")

    for pkg_name, paths in declared.items():
        missing = paths - on_disk.get(pkg_name, set())
        for rel in sorted(missing):
            f.fail(project, "resourcePackages",
                   f"report.json declares {pkg_name}/{rel} but no such file exists on disk")


def check_image_urls(f: Findings, project: str, report_dir: Path) -> None:
    """Every imageUrl literal must match a declared resource, by name or by path.

    Desktop resolves an image visual's imageUrl against the resourcePackages entries. If it
    finds no match the visual renders as an empty placeholder rather than erroring, so this
    is easy to miss.
    """
    doc = load_json(report_dir / "definition" / "report.json") or {}
    resolvable: set[str] = set()
    for pkg in doc.get("resourcePackages") or []:
        for item in pkg.get("items") or []:
            resolvable.add(item.get("name", ""))
            resolvable.add(item.get("path", ""))

    # Aggregate by image rather than by visual: one missing logo reused on every page would
    # otherwise produce dozens of identical lines and bury the other findings.
    unresolved: dict[str, int] = {}
    for visual_json in (report_dir / "definition" / "pages").rglob("visual.json"):
        visual = load_json(visual_json) or {}
        if '"imageUrl"' in visual_json.read_text(encoding="utf-8"):
            f.fail(project, "image visual",
                   f"{visual_json.parent.name} uses an 'imageUrl' property -- Desktop ignores "
                   f"this entirely and renders an empty 'Select Image in the format pane under "
                   f"Style' box. Use objects.image[].properties.sourceFile.image with a "
                   f"ResourcePackageItem expression instead")
        for name in find_image_resource_names(visual):
            if name not in resolvable:
                unresolved[name] = unresolved.get(name, 0) + 1

    static_root = report_dir / "StaticResources" / "RegisteredResources"
    for item_name, count in sorted(unresolved.items()):
        # Distinguish "we failed to declare it" from "the source workbook never shipped it".
        # The latter is not fixable from the workbook alone -- Tableau linked the image
        # externally rather than packaging it -- so it needs a human to supply the file.
        exists = (static_root / item_name).is_file()
        cause = ("present on disk but not declared in report.json"
                 if exists else
                 "no such file was packaged in the source workbook, so it cannot be recovered "
                 "automatically -- supply the image and re-declare it, or delete the visual")
        f.fail(project, "image resource",
               f"'{item_name}' ({count} visual(s)) resolves to no declared resource: {cause}")


def find_image_resource_names(node) -> list[str]:
    """Pull every ResourcePackageItem ItemName out of a visual.json.

    This is how Desktop addresses an image: not by path or URL but by naming the package and
    the item within it, e.g.
        {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources",
                                          "PackageType": 1, "ItemName": "logo.png"}}}
    """
    names: list[str] = []
    if isinstance(node, dict):
        item = node.get("ResourcePackageItem")
        if isinstance(item, dict) and isinstance(item.get("ItemName"), str):
            names.append(item["ItemName"])
        for value in node.values():
            names.extend(find_image_resource_names(value))
    elif isinstance(node, list):
        for entry in node:
            names.extend(find_image_resource_names(entry))
    return names


def check_pages(f: Findings, project: str, report_dir: Path) -> None:
    """Page folders, pages.json and each page.json's own name must all agree.

    A page folder Desktop cannot correlate is treated as a private user file and silently
    dropped from the report -- no tab, no error.
    """
    pages_dir = report_dir / "definition" / "pages"
    if not pages_dir.is_dir():
        f.fail(project, "pages", "no definition/pages folder")
        return

    on_disk = {p.name for p in pages_dir.iterdir() if p.is_dir() and (p / "page.json").exists()}
    if not on_disk:
        f.fail(project, "pages", "no page folders containing a page.json")

    doc = load_json(pages_dir / "pages.json")
    if doc is not None:
        declared = list(doc.get("pageOrder") or [])
        for name in sorted(set(declared) - on_disk):
            f.fail(project, "pages.json", f"declares page '{name}' with no matching folder on disk")
        for name in sorted(on_disk - set(declared)):
            f.fail(project, "pages.json", f"page folder '{name}' is not listed in pageOrder")
        active = doc.get("activePageName")
        if active and active not in declared:
            f.fail(project, "pages.json", f"activePageName '{active}' is not in pageOrder")

    for name in sorted(on_disk):
        page = load_json(pages_dir / name / "page.json") or {}
        if page.get("name") != name:
            f.fail(project, "page.json",
                   f"folder '{name}' contains page.json with name '{page.get('name')}' -- must match")
        for visual_dir in sorted(p for p in (pages_dir / name / "visuals").glob("*") if p.is_dir()):
            visual = load_json(visual_dir / "visual.json") or {}
            if visual.get("name") != visual_dir.name:
                f.fail(project, "visual.json",
                       f"folder '{visual_dir.name}' contains visual.json with name "
                       f"'{visual.get('name')}' -- must match")


_DECORATIVE_VISUAL_TYPES = {"actionButton", "image", "shape", "textbox"}


def check_visual_data_bindings(f: Findings, project: str, report_dir: Path) -> None:
    """Require every data-capable visual to select at least one model field."""
    pages_dir = report_dir / "definition" / "pages"
    if not pages_dir.is_dir():
        return
    for visual_json in sorted(pages_dir.rglob("visual.json")):
        visual = ((load_json(visual_json) or {}).get("visual") or {})
        visual_type = visual.get("visualType")
        if not visual_type or visual_type in _DECORATIVE_VISUAL_TYPES:
            continue
        query_state = ((visual.get("query") or {}).get("queryState") or {})
        bound = any(
            isinstance(role, dict) and bool(role.get("projections"))
            for role in query_state.values()
        )
        if not bound:
            f.fail(
                project,
                "visual data binding",
                f"{visual_json.relative_to(report_dir)}: {visual_type!r} has no field projections; "
                "it will render with no selected data element",
            )


def check_object_names(f: Findings, project: str, report_dir: Path) -> None:
    """PBIR object names must be word characters or hyphens, or Desktop ignores the folder."""
    valid = re.compile(r"^[\w-]+$")
    pages_dir = report_dir / "definition" / "pages"
    for path in sorted(p for p in pages_dir.rglob("*") if p.is_dir()) if pages_dir.is_dir() else []:
        if path.name == "visuals":
            continue
        if not valid.match(path.name):
            f.fail(project, "naming convention",
                   f"'{path.name}' is not made only of word characters or hyphens -- "
                   f"Desktop ignores it and treats it as a private user file")


def check_stray_files(f: Findings, project: str, root: Path) -> None:
    """Editor/OS droppings inside a project folder are noise at best, confusing at worst."""
    for path in root.rglob(".*"):
        if path.name in {".", ".."}:
            continue
        # .pbi/ holds Desktop's own per-user localSettings.json and is legitimate.
        if ".pbi" in path.parts:
            continue
        f.note(project, "stray file", f"{path.relative_to(root)} should not be shipped in the project")


# --- optional JSON-Schema pass (--schemas) -------------------------------------------------
# Every PBIR file declares its own schema by URL. Those URLs are served from
# developer.microsoft.com but the same documents -- including all the $refs they chain to --
# are in the public microsoft/json-schemas repo, which is what we actually fetch.
_MS_PREFIX = "https://developer.microsoft.com/json-schemas/"
_RAW_PREFIX = "https://raw.githubusercontent.com/microsoft/json-schemas/main/"


def _schema_cache_dir() -> Path:
    import tempfile
    path = Path(tempfile.gettempdir()) / "pbir_schema_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_schema(url: str) -> dict:
    import urllib.request
    cached = _schema_cache_dir() / url.replace(_MS_PREFIX, "").replace("/", "__")
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    with urllib.request.urlopen(url.replace(_MS_PREFIX, _RAW_PREFIX), timeout=30) as response:
        body = response.read().decode("utf-8")
    cached.write_text(body, encoding="utf-8")
    return json.loads(body)


def check_against_schemas(f: Findings, project: str, root: Path) -> None:
    """Validate every $schema-declaring JSON file in the project against its own schema."""
    try:
        from jsonschema import Draft7Validator
        from referencing import Registry, Resource
        from referencing.exceptions import Unresolvable
        from referencing.jsonschema import DRAFT7
    except ImportError:
        f.note(project, "schemas", "skipped: pip install jsonschema referencing")
        return

    def retrieve(uri: str):
        if not uri.startswith(_MS_PREFIX):
            raise Unresolvable(uri)
        return Resource.from_contents(_fetch_schema(uri), default_specification=DRAFT7)

    registry = Registry(retrieve=retrieve)

    for path in sorted(root.rglob("*.json")):
        if ".pbi" in path.parts:
            continue
        doc = load_json(path)
        if not isinstance(doc, dict):
            continue
        url = doc.get("$schema")
        if not isinstance(url, str) or not url.startswith(_MS_PREFIX):
            continue  # theme resources and similar carry no PBIR schema
        try:
            validator = Draft7Validator(_fetch_schema(url), registry=registry)
            errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        except Exception as exc:
            f.note(project, "schemas", f"could not validate {path.name}: {exc}")
            continue
        rel = path.relative_to(root)
        for err in errors[:10]:
            where = "/".join(str(p) for p in err.absolute_path) or "(root)"
            f.fail(project, "schema", f"{rel} at {where}: {err.message[:200]}")
        if len(errors) > 10:
            f.fail(project, "schema", f"{rel}: and {len(errors) - 10} further schema errors")


def lint_project(root: Path, f: Findings, with_schemas: bool = False) -> None:
    """Run every check against one project folder (the one containing the .pbip)."""
    project = root.name
    check_item_definition_versions(f, project, root)
    check_semantic_model_v3(f, project, root)
    check_duplicate_member_names(f, project, root)
    check_filter_literal_types(f, project, root)
    check_valid_data_types(f, project, root)
    check_m_row_literal_types(f, project, root)
    check_partitions_and_dax(f, project, root)
    check_dax_references(f, project, root)
    check_column_references_measure(f, project, root)
    check_aggregate_wraps_measure(f, project, root)
    check_literal_plus_concatenation(f, project, root)
    check_stray_files(f, project, root)
    for report_dir in sorted(root.glob("*.Report")):
        check_report_definition_version(f, project, report_dir)
        check_dataset_reference(f, project, report_dir)
        check_resource_packages(f, project, report_dir)
        check_image_urls(f, project, report_dir)
        check_pages(f, project, report_dir)
        check_visual_data_bindings(f, project, report_dir)
        check_object_names(f, project, report_dir)

    # tools/pbip_rules/checks.py: 38 checks against a real parsed model.Project, rather than
    # the regex-on-text approach above. `load_project` never raises -- a file it could not
    # parse is recorded in `parse_errors` and surfaced here as a finding of its own.
    parsed_project = _prm.load_project(root)
    for error in parsed_project.parse_errors:
        f.fail(project, "pbip_rules parse", error)
    for check_fn in _prc.ALL_CHECKS:
        check_fn(f, project, parsed_project)

    if with_schemas:
        check_against_schemas(f, project, root)


def find_projects(target: Path) -> list[Path]:
    """A project folder is one holding a .pbip (or, failing that, a .Report folder)."""
    if list(target.glob("*.pbip")) or list(target.glob("*.Report")):
        return [target]
    found = sorted({p.parent for p in target.glob("*/*.pbip")})
    if found:
        return found
    return sorted({p.parent for p in target.glob("*/*.Report")})


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    quiet = "--quiet" in argv
    if not args:
        print(__doc__)
        return 2

    target = Path(args[0])
    if not target.exists():
        print(f"no such path: {target}")
        return 2

    projects = find_projects(target)
    if not projects:
        print(f"no PBIP projects found under {target}")
        return 2

    f = Findings()
    with_schemas = "--schemas" in argv
    for project in projects:
        lint_project(project, f, with_schemas=with_schemas)

    if f.notes and not quiet:
        print(f"NOTES ({len(f.notes)}):")
        for note in f.notes:
            print(f"  - {note}")
        print()

    if f.failures:
        print(f"FAILED ({len(f.failures)} problems across {len(projects)} project(s)):")
        for failure in f.failures:
            print(f"  - {failure}")
        return 1

    print(f"PASSED: {len(projects)} project(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
