#!/usr/bin/env python3
"""Checks for the register rules that `pbip_lint.py` did not already cover.

These operate on a parsed `model.Project` rather than re-scanning text, because every rule
here is about a RELATIONSHIP between objects rather than a local property: does this
sortByColumn live on the same table, is this dependency graph acyclic, does this
sourceColumn exist in its own partition's M output.

Discipline for every check in this file
---------------------------------------
A linter earns its place by being trusted, and one false positive costs more than one
missed defect: a check that cries wolf gets suppressed, and then it catches nothing at
all. So each check below is deliberately narrowed to the cases it can defend, and where a
rule is broader than what can be verified statically, the check implements the defensible
core and the register records the remainder as still-open rather than claiming the rule is
covered.

Three rules are absent from this file on purpose. `SM-TYPE-06` (isKey unique and
non-nullable), `SM-STRUCT-02` (sortByColumn functionally 1:1) and `SM-STRUCT-07` (the one
side of a relationship holds unique values) are all statements about DATA, not metadata.
Nothing in a PBIP file can settle them; they fail at refresh, against the real source.
They are marked `runtime` in the register rather than `gap`, because calling them
unimplemented would imply a check could exist.
"""
from __future__ import annotations

import re

from . import model as M

# ---------------------------------------------------------------- shared helpers

# `'Quoted Table'[Column]`, `Table[Column]`, or a bare `[Name]`.
_QUALIFIED_REF = re.compile(r"(?:'((?:[^']|'')+)'|(\w+))\s*\[([^\]]+)\]")
_BARE_REF = re.compile(r"(?<![\w')\]])\[([^\]]+)\]")

# DAX string literals and comments must be removed before any reference scan, or a
# `"[not a reference]"` inside a label string is read as one.
_DAX_STRING = re.compile(r'"(?:[^"\\]|\\.|"")*"')
_DAX_LINE_COMMENT = re.compile(r"(?://|--)[^\n]*")
_DAX_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def scrub_dax(text: str) -> str:
    """Blank out comments and string literals, preserving length-independent structure."""
    text = _DAX_BLOCK_COMMENT.sub(" ", text)
    text = _DAX_LINE_COMMENT.sub(" ", text)
    return _DAX_STRING.sub('""', text)


def qualified_refs(expression: str) -> set[tuple[str, str]]:
    """(table, member) pairs a DAX body references with an explicit table qualifier."""
    out = set()
    for match in _QUALIFIED_REF.finditer(scrub_dax(expression)):
        table = (match.group(1) or match.group(2) or "").replace("''", "'")
        out.add((table, match.group(3).strip()))
    return out


def bare_refs(expression: str) -> set[str]:
    """Unqualified `[Name]` references, which resolve to a measure or a same-table column."""
    scrubbed = scrub_dax(expression)
    qualified_spans = {m.span(3) for m in _QUALIFIED_REF.finditer(scrubbed)}
    out = set()
    for match in _BARE_REF.finditer(scrubbed):
        if match.span(1) not in qualified_spans:
            out.add(match.group(1).strip())
    return out


def _fmt(member: M.Member) -> str:
    return f"{member.kind} '{member.table}'[{member.name}]"


# ================================================================ naming and identity


def check_measure_names_unique_model_wide(f, project: str, proj: M.Project) -> None:
    """SM-NAME-02. Measure names are a MODEL-wide namespace, unlike column names.

    Tableau has no equivalent constraint -- two datasources in one workbook may each carry
    a field of the same caption -- so this fires exactly when a workbook has more than one
    datasource, which is the common case rather than the exotic one.
    """
    for name, members in sorted(proj.measure_index().items()):
        if len(members) > 1:
            where = ", ".join(sorted(f"'{m.table}'" for m in members))
            f.fail(project, "measure name not unique",
                   f"measure [{name}] is declared on {len(members)} tables ({where}) -- "
                   f"measure names must be unique across the whole model, not per table; "
                   f"Desktop refuses to deserialize the second one")


def check_table_names_unique(f, project: str, proj: M.Project) -> None:
    """SM-NAME-03. TOM's collections are case-insensitive, so differing case is not enough."""
    seen: dict[str, list[str]] = {}
    for name in proj.tables:
        seen.setdefault(name.casefold(), []).append(name)
    for names in sorted(seen.values()):
        if len(names) > 1:
            f.fail(project, "table name collision",
                   f"tables {sorted(names)} differ only by case -- TOM's name collection "
                   f"ignores case, so these are one name to it")


def check_table_namespace_collisions(f, project: str, proj: M.Project) -> None:
    """SM-NAME-04. Columns, measures and hierarchies share ONE namespace per table.

    `check_duplicate_member_names` (SM-NAME-01) already covers the column/measure pair.
    What it does not know about is hierarchies, which occupy the same namespace and are
    generated from Tableau drill paths whose caption routinely matches a column's.
    """
    for table in proj.tables.values():
        buckets: dict[str, list[str]] = {}
        for name in table.columns:
            buckets.setdefault(name.casefold(), []).append(f"column '{name}'")
        for name in table.measures:
            buckets.setdefault(name.casefold(), []).append(f"measure '{name}'")
        for hierarchy in table.hierarchies:
            buckets.setdefault(hierarchy.name.casefold(), []).append(
                f"hierarchy '{hierarchy.name}'")
        for kinds in sorted(buckets.values()):
            # Only report collisions that involve a hierarchy; SM-NAME-01 owns the rest,
            # and reporting both would double-count the same defect under two rule IDs.
            if len(kinds) > 1 and any(k.startswith("hierarchy") for k in kinds):
                f.fail(project, "table namespace collision",
                       f"table '{table.name}': {' and '.join(sorted(kinds))} share a name -- "
                       f"columns, measures and hierarchies are one namespace per table")


# `[` and `]` terminate a DAX column reference, so a name containing either cannot be
# referenced at all. This is the defensible core of the character rule; the wider set the
# editor rejects for TABLE names specifically is recorded in the register as unverified,
# because the corpus demonstrably contains `.` in both table and column names in projects
# that do load, and inventing a restriction Desktop does not enforce would be worse than
# leaving it unchecked.
_NAME_BREAKS_DAX = re.compile(r"[\[\]]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def check_name_characters(f, project: str, proj: M.Project) -> None:
    """SM-NAME-05, narrowed to characters that provably break a DAX reference."""
    def inspect(kind: str, owner: str, name: str) -> None:
        if _NAME_BREAKS_DAX.search(name):
            f.fail(project, "name breaks DAX reference syntax",
                   f"{kind} '{name}'{owner} contains a square bracket -- `[` and `]` "
                   f"delimit a DAX column reference, so this name can never be referenced")
        if _CONTROL_CHARS.search(name):
            f.fail(project, "control character in name",
                   f"{kind} '{name!r}'{owner} contains a control character")

    for table in proj.tables.values():
        inspect("table", "", table.name)
        for column in table.columns:
            inspect("column", f" on '{table.name}'", column)
        for measure in table.measures:
            inspect("measure", f" on '{table.name}'", measure)
        for hierarchy in table.hierarchies:
            inspect("hierarchy", f" on '{table.name}'", hierarchy.name)


# Not a TOM limit. TOM itself accepts longer names; this mirrors names.MAX_NAME in the
# builder, which caps at 100 so that a long name nested in a deep output path stays inside
# the Windows 260-character path limit. Exceeding it is therefore a portability defect
# rather than a model that Desktop would refuse, which is why the finding says so.
MAX_NAME_LENGTH = 100


def check_name_wellformed(f, project: str, proj: M.Project) -> None:
    """SM-NAME-06. Empty, over-long, or whitespace-padded names.

    The editor trims whitespace the instant a human types it, so no authored model carries
    a padded name and the failure mode is untested territory. A caption taken from Tableau
    XML attribute text keeps whatever padding the author left, and -- worse -- two captions
    differing only by a trailing space become a case-of-one collision under SM-NAME-01
    while looking distinct in a diff.
    """
    def inspect(kind: str, owner: str, name: str) -> None:
        if not name.strip():
            f.fail(project, "empty name", f"{kind}{owner} has an empty or whitespace-only name")
            return
        if name != name.strip():
            f.fail(project, "padded name",
                   f"{kind} {name!r}{owner} has leading or trailing whitespace -- the editor "
                   f"trims this on entry, so it cannot occur in an authored model")
        if len(name) > MAX_NAME_LENGTH:
            f.fail(project, "name too long",
                   f"{kind} '{name[:40]}...'{owner} is {len(name)} characters, over the "
                   f"{MAX_NAME_LENGTH}-character cap the builder applies so that deep output "
                   f"paths stay within the Windows 260-character limit -- TOM itself would "
                   f"accept the name")

    for table in proj.tables.values():
        inspect("table", "", table.name)
        for column in table.columns:
            inspect("column", f" on '{table.name}'", column)
        for measure in table.measures:
            inspect("measure", f" on '{table.name}'", measure)


def check_display_folder_syntax(f, project: str, proj: M.Project) -> None:
    """SM-NAME-07. A displayFolder is backslash-separated with no empty segment."""
    for member in proj.all_members():
        folder = member.props.get("displayFolder", "")
        if not folder:
            continue
        if folder != folder.strip() or folder.startswith("\\") or folder.endswith("\\") \
                or "\\\\" in folder:
            f.fail(project, "malformed displayFolder",
                   f"{_fmt(member)} has displayFolder {folder!r} -- a leading, trailing or "
                   f"doubled separator produces an unnamed folder in the field list")


# Words that begin a DAX expression construct. A table named for one of these must be
# quoted at every reference site or the parser reads the keyword instead.
_DAX_KEYWORDS = {"var", "return", "not", "in", "order", "by", "start", "at", "define",
                 "measure", "column", "table", "evaluate", "true", "false", "blank"}


def check_reserved_word_names(f, project: str, proj: M.Project) -> None:
    """SM-NAME-08. Reported as a note: legal, but only while every reference stays quoted."""
    for name in sorted(proj.tables):
        if name.casefold() in _DAX_KEYWORDS:
            f.note(project, "table named for a DAX keyword",
                   f"table '{name}' collides with a DAX keyword -- every reference to it "
                   f"must stay quoted or the parser reads the keyword instead")


# ================================================================ types and formatting

# `type table [ #"Quoted Name" = Int64.Type, Bare = text ]` -- the ascription the generator
# emits on every Table.FromRows partition, which is what makes SM-TYPE-03 checkable at all.
_TYPE_TABLE = re.compile(r"type\s+table\s*\[(.*?)\]", re.DOTALL)
_M_FIELD = re.compile(r'(?:#"((?:[^"]|"")*)"|([A-Za-z_]\w*))\s*=')


def _partition_columns(source: str) -> set[str] | None:
    """Column names the partition's M type-ascription declares, or None if it declares none."""
    match = _TYPE_TABLE.search(source)
    if not match:
        return None
    names = set()
    for field_match in _M_FIELD.finditer(match.group(1)):
        raw = field_match.group(1) or field_match.group(2) or ""
        names.add(raw.replace('""', '"'))
    return names or None


def check_source_column_in_partition(f, project: str, proj: M.Project) -> None:
    """SM-TYPE-03. Every sourceColumn must be a column the partition's M actually produces.

    Deliberately conservative: it runs only when the table has exactly one partition AND
    that partition declares an explicit `type table [...]` ascription. Any other shape
    (multiple partitions, a native query, a folded source) means the produced columns
    cannot be known from the file, and guessing would produce noise.

    This rule is also the counterweight to the naming rules. SM-NAME-05 and SM-NAME-06 want
    to rename objects; `sourceColumn` names the PHYSICAL source and must not be renamed
    with them. The two pull in opposite directions, and this check is what notices when a
    rename pass forgets the distinction.
    """
    for table in proj.tables.values():
        if len(table.partitions) != 1:
            continue
        declared = _partition_columns(table.partitions[0].source)
        if declared is None:
            continue
        folded = {name.casefold(): name for name in declared}
        normalised = {re.sub(r"[^a-z0-9]", "", name.casefold()): name for name in declared}

        missing: list[tuple[str, str, str]] = []
        for column in table.columns.values():
            source_column = column.props.get("sourceColumn", "")
            if not source_column or column.is_calculated or source_column in declared:
                continue
            if source_column.casefold() in folded:
                cause, actual = "case", folded[source_column.casefold()]
            elif re.sub(r"[^a-z0-9]", "", source_column.casefold()) in normalised:
                cause, actual = "spelling", normalised[
                    re.sub(r"[^a-z0-9]", "", source_column.casefold())]
            else:
                cause, actual = "absent", ""
            missing.append((column.name, source_column, f"{cause}:{actual}"))

        if not missing:
            continue

        # Reported per table, not per column. A mismatch of this kind is systematic -- one
        # writer disagreeing with another -- so one line per column would bury every other
        # finding while saying the same thing 31 times.
        causes = {entry[2].split(":")[0] for entry in missing}
        sample = "; ".join(f"'{col}' wants '{src}'" + (f" but M has '{act.split(':', 1)[1]}'"
                                                       if ":" in act and act.split(":", 1)[1] else "")
                           for col, src, act in missing[:3])
        if causes <= {"case", "spelling"}:
            diagnosis = ("the M partition writer and the column writer disagree on how to "
                         "spell the same column -- M is case-sensitive, so neither wins")
        else:
            diagnosis = "the partition's M output does not contain these columns at all"
        f.fail(project, "sourceColumn not produced by partition",
               f"table '{table.name}': {len(missing)} of {len(table.columns)} columns name a "
               f"sourceColumn the partition does not produce ({diagnosis}). "
               f"Desktop fails the table with 'column not found'. e.g. {sample}")


_DATE_TOKENS = re.compile(r"(yy|mm|dd|hh|ss|AM/PM)", re.IGNORECASE)
_NUMERIC_TYPES = {"int64", "double", "decimal"}


def check_format_string_matches_type(f, project: str, proj: M.Project) -> None:
    """SM-TYPE-04, narrowed to the two mismatches that are unambiguous."""
    for member in proj.all_members():
        fmt = member.props.get("formatString", "")
        data_type = member.data_type.casefold()
        if not fmt or not data_type:
            continue
        if _DATE_TOKENS.search(fmt) and data_type in _NUMERIC_TYPES | {"string", "boolean"}:
            f.fail(project, "date format on a non-date column",
                   f"{_fmt(member)} is dataType {data_type} but carries the date/time "
                   f"formatString {fmt!r}")
        if data_type == "string" and ("%" in fmt or "#" in fmt or "0" in fmt):
            f.fail(project, "numeric format on a string column",
                   f"{_fmt(member)} is dataType string but carries the numeric "
                   f"formatString {fmt!r}")


_NON_SUMMABLE = {"string", "boolean", "binary"}
_NUMERIC_AGGS = {"sum", "average"}


def check_summarize_by_matches_type(f, project: str, proj: M.Project) -> None:
    """SM-TYPE-05. Restricted to sum/average, which are illegal on non-numeric types.

    min and max are excluded on purpose: they ARE valid over text and dates, so flagging
    them would be the false positive that gets this whole check switched off.
    """
    for table in proj.tables.values():
        for column in table.columns.values():
            summarize = column.props.get("summarizeBy", "").casefold()
            data_type = column.data_type.casefold()
            if summarize in _NUMERIC_AGGS and data_type in _NON_SUMMABLE:
                f.fail(project, "illegal summarizeBy for type",
                       f"{_fmt(column)} is dataType {data_type} with summarizeBy "
                       f"{summarize} -- only numeric columns can be summed or averaged")


# ================================================================ structural integrity


def check_sort_by_column_same_table(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-01. The editor's sort dropdown only ever offers same-table columns."""
    for table in proj.tables.values():
        for column in table.columns.values():
            target = column.props.get("sortByColumn", "")
            if not target:
                continue
            if target not in table.columns:
                f.fail(project, "sortByColumn not on this table",
                       f"{_fmt(column)} sorts by '{target}', which is not a column of "
                       f"'{table.name}' -- a sortByColumn must live on the same table")


def check_sort_by_cycles(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-03. A sort cycle is reported by Desktop as an unexplained circular dependency."""
    edges: dict[tuple[str, str], tuple[str, str]] = {}
    for table in proj.tables.values():
        for column in table.columns.values():
            target = column.props.get("sortByColumn", "")
            if target:
                edges[(table.name, column.name)] = (table.name, target)

    for start in sorted(edges):
        seen = [start]
        node = start
        while node in edges:
            node = edges[node]
            if node in seen:
                cycle = " -> ".join(f"'{t}'[{c}]" for t, c in seen[seen.index(node):] + [node])
                if seen.index(node) == 0:  # report each cycle once, from its lowest member
                    f.fail(project, "sortByColumn cycle", f"sort cycle: {cycle}")
                break
            seen.append(node)


def check_hierarchy_levels(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-04. Levels must name real columns of their own table.

    Tableau drill paths can span a blended datasource; Power BI hierarchies cannot leave
    their table, so a translation that flattens the blend leaves levels pointing nowhere.
    """
    for table in proj.tables.values():
        for hierarchy in table.hierarchies:
            if not hierarchy.levels:
                f.fail(project, "empty hierarchy",
                       f"table '{table.name}': hierarchy '{hierarchy.name}' declares no levels")
            seen: set[str] = set()
            for level_name, column_name in hierarchy.levels:
                if not column_name:
                    f.fail(project, "hierarchy level without a column",
                           f"table '{table.name}': hierarchy '{hierarchy.name}' level "
                           f"'{level_name}' names no column")
                elif column_name not in table.columns:
                    f.fail(project, "hierarchy level column missing",
                           f"table '{table.name}': hierarchy '{hierarchy.name}' level "
                           f"'{level_name}' references column '{column_name}', which is not "
                           f"on this table")
                if level_name.casefold() in seen:
                    f.fail(project, "duplicate hierarchy level",
                           f"table '{table.name}': hierarchy '{hierarchy.name}' declares "
                           f"level '{level_name}' more than once")
                seen.add(level_name.casefold())


def check_relationship_endpoints_exist(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-05. A dangling endpoint stops the model loading."""
    for rel in proj.relationships:
        for side, table_name, column_name in (
            ("from", rel.from_table, rel.from_column),
            ("to", rel.to_table, rel.to_column),
        ):
            table = proj.tables.get(table_name)
            if table is None:
                f.fail(project, "relationship endpoint table missing",
                       f"relationship {rel.name}: {side} table '{table_name}' is not in the model")
            elif column_name not in table.columns:
                f.fail(project, "relationship endpoint column missing",
                       f"relationship {rel.name}: {side} column '{table_name}'[{column_name}] "
                       f"is not in the model")


def check_relationship_type_match(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-06. Tableau coerces join-key types; Power BI refuses to."""
    for rel in proj.relationships:
        from_table = proj.tables.get(rel.from_table)
        to_table = proj.tables.get(rel.to_table)
        if not from_table or not to_table:
            continue  # SM-STRUCT-05 owns that failure; do not report it twice
        from_col = from_table.columns.get(rel.from_column)
        to_col = to_table.columns.get(rel.to_column)
        if not from_col or not to_col:
            continue
        if from_col.data_type and to_col.data_type and from_col.data_type != to_col.data_type:
            f.fail(project, "relationship type mismatch",
                   f"relationship {rel.name}: '{rel.from_table}'[{rel.from_column}] is "
                   f"{from_col.data_type} but '{rel.to_table}'[{rel.to_column}] is "
                   f"{to_col.data_type} -- both ends must have the same dataType")


def check_one_to_one_cross_filter_direction(f, project: str, proj: M.Project) -> None:
    """PFE_TM_ONE2ONE_REL_CF_DIR_ONEWAY. A relationship with BOTH sides declared cardinality
    "one" must set crossFilteringBehavior: bothDirections -- the Tabular engine hard-rejects a
    one-to-one relationship left on the (single-direction) default at model load/save, with no
    warning at authoring time: Desktop's own preflight/lint never runs against hand-authored or
    manually-restructured TMDL, so this is caught only when Desktop itself tries to load the
    project. Real failure this closes: splitting a Tableau datasource's flattened single table
    back into a star schema (see build-tableau-pbip's own "extracted as a single table" residue
    note) naturally produces genuine one-to-one relationships between the split-out tables, and
    it is easy to declare `fromCardinality: one` for documentation/intent without also setting
    the cross-filtering property the engine actually requires alongside it.
    """
    for rel in proj.relationships:
        if rel.is_one_to_one and rel.cross_filtering_behavior != "bothDirections":
            observed = rel.cross_filtering_behavior or "(default: single/OneDirection)"
            f.fail(project, "one-to-one relationship needs bothDirections",
                   f"relationship {rel.name} ('{rel.from_table}'[{rel.from_column}] <-> "
                   f"'{rel.to_table}'[{rel.to_column}]) declares both sides cardinality 'one' "
                   f"but crossFilteringBehavior is {observed} -- Power BI requires "
                   f"crossFilteringBehavior: bothDirections on every one-to-one relationship, "
                   f"or Desktop refuses to load the model")


def check_relationship_ambiguity(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-08. More than one active relationship per table pair, or an active cycle.

    Multiple Tableau joins between the same two tables map to multiple relationships; all
    but one have to be emitted inactive.
    """
    active = [r for r in proj.relationships if r.is_active]

    pairs: dict[frozenset, list[str]] = {}
    for rel in active:
        pairs.setdefault(frozenset((rel.from_table, rel.to_table)), []).append(rel.name)
    for pair, names in sorted(pairs.items(), key=lambda kv: sorted(kv[0])):
        if len(names) > 1:
            f.fail(project, "multiple active relationships",
                   f"tables {sorted(pair)} are joined by {len(names)} ACTIVE relationships "
                   f"({sorted(names)}) -- only one may be active")

    adjacency: dict[str, set[str]] = {}
    for rel in active:
        adjacency.setdefault(rel.from_table, set()).add(rel.to_table)
        adjacency.setdefault(rel.to_table, set()).add(rel.from_table)

    # An active relationship graph must be a forest: |edges| >= |nodes| inside any connected
    # component means a cycle, which is an ambiguous filter path.
    seen: set[str] = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        component, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency.get(node, ()))
        seen |= component
        edges = sum(1 for r in active if r.from_table in component)
        if edges >= len(component):
            f.fail(project, "ambiguous relationship path",
                   f"active relationships form a cycle across {sorted(component)} -- "
                   f"{edges} relationships between {len(component)} tables leaves the filter "
                   f"path ambiguous")


def check_calc_dependency_cycles(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-09. The general cycle check Desktop reports so misleadingly.

    SM-DAX-02 catches one specific cause (a calculated column referencing a measure). This
    is the whole graph: any cycle among calculated columns and measures, by whatever route.
    """
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    measures = proj.measure_index()

    for member in proj.all_members():
        if not member.is_calculated:
            continue
        node = (member.table, member.name)
        deps: set[tuple[str, str]] = set()
        for table_name, target in qualified_refs(member.expression):
            table = proj.tables.get(table_name)
            if table and (target in table.columns or target in table.measures):
                deps.add((table_name, target))
        for target in bare_refs(member.expression):
            if target in measures:
                for candidate in measures[target]:
                    deps.add((candidate.table, candidate.name))
            elif target in proj.tables[member.table].columns:
                deps.add((member.table, target))
        graph[node] = deps

    # Iterative depth-first search with an explicit colour map; recursion would overflow on
    # the deeper corpus models (WorkbookB Dashboard carries 262 calculated objects).
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[tuple[str, str], int] = {}
    reported: set[frozenset] = set()

    for root in sorted(graph):
        if colour.get(root, WHITE) != WHITE:
            continue
        stack = [(root, iter(sorted(graph.get(root, ()))))]
        path = [root]
        colour[root] = GREY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child not in graph:
                    continue  # a plain (non-calculated) column terminates the walk
                state = colour.get(child, WHITE)
                if state == GREY:
                    cycle = path[path.index(child):] + [child]
                    key = frozenset(cycle)
                    if key not in reported:
                        reported.add(key)
                        f.fail(project, "circular dependency",
                               "cycle: " + " -> ".join(f"'{t}'[{n}]" for t, n in cycle) +
                               " -- Desktop reports this as a circular dependency between "
                               "unrelated objects")
                elif state == WHITE:
                    colour[child] = GREY
                    path.append(child)
                    stack.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
            if not advanced:
                colour[node] = BLACK
                stack.pop()
                if path:
                    path.pop()


def check_tables_have_partitions(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-10. A table with no partition and no calculated definition loads nothing."""
    for table in proj.tables.values():
        if not table.partitions:
            f.fail(project, "table without a partition",
                   f"table '{table.name}' declares no partition -- a table needs one unless "
                   f"it is a calculated table")


def check_role_names_unique(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-11, name half. Whether a filter returns boolean needs a DAX evaluator."""
    seen: dict[str, int] = {}
    for name in proj.roles:
        seen[name.casefold()] = seen.get(name.casefold(), 0) + 1
    for name, count in sorted(seen.items()):
        if count > 1:
            f.fail(project, "duplicate RLS role",
                   f"role '{name}' is declared {count} times -- role names must be unique")


def check_calculation_groups(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-12. At most one calculationGroup column per table."""
    for table in proj.tables.values():
        groups = [c for c in table.columns.values()
                  if c.props.get("dataCategory", "").casefold() == "calculationgroup"
                  or "calculationGroup" in c.props]
        if len(groups) > 1:
            f.fail(project, "multiple calculation-group columns",
                   f"table '{table.name}' declares {len(groups)} calculationGroup columns -- "
                   f"a calculation-group table may hold exactly one")


def check_perspective_references(f, project: str, proj: M.Project) -> None:
    """SM-STRUCT-13. A perspective naming a table that no longer exists."""
    for perspective, tables in sorted(proj.perspectives.items()):
        for table_name in tables:
            if table_name not in proj.tables:
                f.fail(project, "perspective references a missing table",
                       f"perspective '{perspective}' includes table '{table_name}', which is "
                       f"not in the model")


# ================================================================ DAX and M


def _unbalanced(text: str) -> str | None:
    """The first structural imbalance in a scrubbed expression, or None."""
    depth = {"(": 0, "[": 0}
    closers = {")": "(", "]": "["}
    for char in text:
        if char in depth:
            depth[char] += 1
        elif char in closers:
            opener = closers[char]
            depth[opener] -= 1
            if depth[opener] < 0:
                return f"unmatched '{char}'"
    for opener, count in depth.items():
        if count > 0:
            return f"{count} unclosed '{opener}'"
    return None


def check_dax_balanced(f, project: str, proj: M.Project) -> None:
    """SM-DAX-04, structural half.

    A full DAX parser is a large dependency and this is not one. What it does catch is the
    class a template-driven or LLM-assisted generator actually produces: truncated output
    and mismatched delimiters. An expression that passes here is not thereby valid DAX --
    the register records the remainder as still open rather than claiming SM-DAX-04 is met.
    """
    for member in proj.all_members():
        if not member.is_calculated:
            continue
        scrubbed = scrub_dax(member.expression)
        # Counted on the raw body, not the scrubbed one: scrub_dax elides string *contents*
        # and leaves the delimiters behind, so its quote count is an artefact of scrubbing
        # rather than a fact about the expression. Doubled "" escapes cancel in pairs and so
        # do not disturb the parity test.
        if member.expression.count('"') % 2:
            f.fail(project, "unterminated DAX string",
                   f"{_fmt(member)} has an odd number of quote characters -- a string "
                   f"literal is unterminated")
            continue
        problem = _unbalanced(scrubbed)
        if problem:
            f.fail(project, "unbalanced DAX", f"{_fmt(member)}: {problem}")
        if re.search(r"[+\-*/&=<>,]\s*$", scrubbed.strip()):
            f.fail(project, "DAX ends on an operator",
                   f"{_fmt(member)} ends with a trailing operator -- the expression is truncated")


def check_measure_bare_column(f, project: str, proj: M.Project) -> None:
    """SM-DAX-05, narrowed to the unambiguous case: the whole body IS a column reference.

    The general rule -- no unaggregated column anywhere in a measure -- cannot be decided
    without knowing row context, since a bare column is perfectly legal inside SUMX or
    CALCULATE. Flagging those would swamp the output. A measure whose entire body is one
    column reference needs no context analysis and is always wrong.

    Considered widening this, after the NGPO `Issuer - PV` bug (see SKILL.md's bug table --
    `col * col` with zero aggregation anywhere is the same illegal shape, just not textually
    ONE bare column) to: flag a measure with ZERO aggregate/iterator calls anywhere in its body
    AND at least one bare qualified column reference. Tested empirically against the real
    6-workbook reference corpus before deciding, rather than reasoning about it in the
    abstract: a prototype of exactly this rule flagged 75 measures in ONE workbook alone (AP
    Product Portfolio Management Dashboard) -- almost entirely legitimate Tableau
    "growth-arrow"/gap-style measures like `IF('T'[x growth] > 0, "up", BLANK())`, which are
    NOT unconditionally broken the way a pure `'T'[Col]` body is: whether they resolve in
    Desktop depends on the report visual's own grouping granularity (a table/matrix grouped
    tightly enough to filter the table to one row makes the bare reference resolve; a coarser
    grouping errors the same way `Issuer - PV` did) -- exactly the row-context ambiguity this
    function's own docstring already says can't be decided statically. That count is the same
    order of magnitude as the ~450-false-positive prototype `tmdl.py`'s SKILL.md bug table
    already records rejecting for the same reason. Decision: NOT adding this as a POST_EMIT
    backstop -- it would be noisy by the same standard that sank the earlier attempt, and this
    project's own convention is a targeted unit test at the translation-time mechanism (already
    covered by `dax.is_aggregate`'s and `extract.classify_formula`'s self-tests) over a general
    checker with a confirmed high false-positive rate. Revisit only if a REAL Desktop failure of
    this broader shape (not just the narrow one) is ever reported.
    """
    for table in proj.tables.values():
        for measure in table.measures.values():
            body = scrub_dax(measure.expression).strip()
            match = _QUALIFIED_REF.fullmatch(body)
            if not match:
                continue
            target_table = (match.group(1) or match.group(2) or "").replace("''", "'")
            target_name = match.group(3).strip()
            owner = proj.tables.get(target_table)
            if owner and target_name in owner.columns:
                f.fail(project, "measure is an unaggregated column",
                       f"{_fmt(measure)} is exactly '{target_table}'[{target_name}] with no "
                       f"aggregation -- Desktop fails it with \"a single value for column "
                       f"'{target_name}' cannot be determined\"")


def check_reference_case(f, project: str, proj: M.Project) -> None:
    """SM-DAX-06. A reference matching an object case-insensitively but not exactly.

    This is the residue a rename pass leaves behind, and it is invisible in review because
    the two spellings look equivalent.
    """
    tables_folded = {name.casefold(): name for name in proj.tables}
    for member in proj.all_members():
        if not member.is_calculated:
            continue
        for table_name, target in sorted(qualified_refs(member.expression)):
            real_table = proj.tables.get(table_name)
            if real_table is None:
                folded = tables_folded.get(table_name.casefold())
                if folded:
                    f.fail(project, "reference differs by case",
                           f"{_fmt(member)} references table '{table_name}' but the model "
                           f"declares '{folded}'")
                continue
            if target in real_table.columns or target in real_table.measures:
                continue
            for actual in list(real_table.columns) + list(real_table.measures):
                if actual.casefold() == target.casefold():
                    f.fail(project, "reference differs by case",
                           f"{_fmt(member)} references '{table_name}'[{target}] but the "
                           f"model declares [{actual}]")
                    break


def check_m_structure(f, project: str, proj: M.Project) -> None:
    """SM-PART-02. An M partition must be a balanced `let ... in` with a result step."""
    for table in proj.tables.values():
        for partition in table.partitions:
            if partition.kind not in ("m", "'m'", ""):
                continue
            source = partition.source
            if not source.strip():
                f.fail(project, "empty partition source",
                       f"table '{table.name}': partition '{partition.name}' has an empty "
                       f"M expression")
                continue
            if "let" in source and not re.search(r"\bin\b", source):
                f.fail(project, "M let without in",
                       f"table '{table.name}': partition '{partition.name}' opens `let` but "
                       f"has no `in` clause")
            problem = _unbalanced(re.sub(r'"(?:[^"\\]|\\.)*"', '""', source))
            if problem:
                f.fail(project, "unbalanced M",
                       f"table '{table.name}': partition '{partition.name}': {problem}")


_M_PARAM_REF = re.compile(r'#"((?:[^"]|"")*)"|(?<![\w.#"])([A-Za-z_]\w*)(?!\s*=)')


def check_m_parameter_refs(f, project: str, proj: M.Project) -> None:
    """SM-PART-03. A shared expression a partition uses must be declared.

    Only names that look like declared shared expressions are considered -- the check asks
    "is every declared expression name that is USED also DECLARED", which is vacuous unless
    the model declares some. It therefore stays silent on models with no expressions.tmdl
    rather than trying to distinguish an M library function from a parameter reference.
    """
    if not proj.expressions:
        return
    declared = set(proj.expressions)
    for table in proj.tables.values():
        for partition in table.partitions:
            used = set()
            for match in _M_PARAM_REF.finditer(partition.source):
                used.add((match.group(1) or match.group(2) or "").replace('""', '"'))
            for name in sorted(used):
                looks_declared = any(name.casefold() == d.casefold() for d in declared)
                if looks_declared and name not in declared:
                    f.fail(project, "M expression name differs by case",
                           f"table '{table.name}': partition '{partition.name}' references "
                           f"'{name}', but expressions.tmdl declares a different spelling")


# ================================================================ report layer


def _field_refs(node, out: list[tuple[str, str, str]]) -> None:
    """Collect (kind, entity, property) from any nested PBIR field expression."""
    if isinstance(node, dict):
        for kind in ("Column", "Measure", "HierarchyLevel"):
            body = node.get(kind)
            if isinstance(body, dict) and "Property" in body:
                entity = (((body.get("Expression") or {}).get("SourceRef") or {}).get("Entity"))
                if entity:
                    out.append((kind, entity, body["Property"]))
        for value in node.values():
            _field_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _field_refs(item, out)


# Visual types that legitimately carry no data. Everything else is bound to the model, and
# an empty one renders as a blank rectangle the user cannot diagnose.
_DATALESS_VISUALS = {"textbox", "image", "shape", "basicShape", "actionButton", "bookmarkNavigator",
                     "pageNavigator", "video", "webBrowser"}

# Maximum fields per data role, for the visual types where the field well is a hard limit.
# Built from the roles the corpus actually emits plus the well-known single-field visuals;
# a type absent from this table is simply not checked rather than guessed at.
_ROLE_LIMITS: dict[str, dict[str, int]] = {
    "card": {"Values": 1},
    "kpi": {"Values": 1, "Indicator": 1},
    "slicer": {"Values": 1, "Fields": 1},
    "pieChart": {"Category": 1, "Y": 1},
    "donutChart": {"Category": 1, "Y": 1},
    "gauge": {"Y": 1},
    "treemap": {"Values": 1},
    "waterfallChart": {"Category": 1, "Y": 1},
    "funnel": {"Category": 1},
}

_KNOWN_VISUAL_TYPES = set(_ROLE_LIMITS) | _DATALESS_VISUALS | {
    # "stackedColumnChart"/"stackedBarChart" deliberately absent: Power BI has no visual
    # registered under either name -- the plain "columnChart"/"barChart" ARE the stacked
    # variants, "clustered*" is the side-by-side one. A real Desktop file confirmed this the
    # hard way (LAC Pedidosya Transaction Profile, CustomVisualNotFound on every color-split
    # bar/column chart) after pbir.py emitted the never-registered name -- keeping it out of
    # this whitelist is what would have caught that before it ever reached Desktop.
    "tableEx", "pivotTable", "matrix", "columnChart", "barChart", "clusteredColumnChart",
    "clusteredBarChart", "hundredPercentStackedColumnChart",
    "hundredPercentStackedBarChart", "lineChart", "areaChart", "stackedAreaChart",
    "lineStackedColumnComboChart", "lineClusteredColumnComboChart", "scatterChart", "map",
    "filledMap", "shapeMap", "azureMap", "multiRowCard", "ribbonChart", "keyDriverInfluencers",
    "decompositionTreeVisual", "qnaVisual", "smartNarrative", "advancedSlicerVisual",
    "esriVisual", "rVisual", "pythonVisual", "scriptVisual",
}


def check_visual_types_known(f, project: str, proj: M.Project) -> None:
    """RPT-ROLE-01. An unrecognised visualType renders as an empty container.

    Reported as a note, not a failure: the list is empirical and custom visuals are
    legitimate, so an unknown name is a prompt to verify rather than proof of a defect.
    """
    unknown: dict[str, int] = {}
    for page in proj.pages:
        for visual in page.visuals:
            if visual.visual_type and visual.visual_type not in _KNOWN_VISUAL_TYPES:
                unknown[visual.visual_type] = unknown.get(visual.visual_type, 0) + 1
    for name, count in sorted(unknown.items()):
        f.note(project, "unrecognised visualType",
               f"'{name}' used by {count} visual(s) -- not in the known set; verify Desktop "
               f"renders it rather than showing an empty container")


def check_visual_has_required_data(f, project: str, proj: M.Project) -> None:
    """RPT-ROLE-02. A data-bound visual with no fields at all.

    The editor shows an explicit 'add data to this visual' well for this state; a generated
    one just renders blank, with nothing to say which role is missing. Counted per page
    rather than per visual, because a translation gap of this kind is systematic and
    listing every instance would bury the rest of the output.
    """
    for page in proj.pages:
        empty: dict[str, int] = {}
        for visual in page.visuals:
            if not visual.visual_type or visual.visual_type in _DATALESS_VISUALS:
                continue
            projections = sum(len(role.get("projections", []))
                              for role in visual.query_state.values()
                              if isinstance(role, dict))
            if not projections:
                empty[visual.visual_type] = empty.get(visual.visual_type, 0) + 1
        for visual_type, count in sorted(empty.items()):
            f.fail(project, "visual has no data",
                   f"page '{page.name}': {count} {visual_type} visual(s) bind no field at "
                   f"all -- a data-bound visual with an empty field well renders as a blank "
                   f"rectangle")


def check_visual_role_cardinality(f, project: str, proj: M.Project) -> None:
    """RPT-ROLE-03. More fields in a role than that visual's field well accepts."""
    for page in proj.pages:
        for visual in page.visuals:
            limits = _ROLE_LIMITS.get(visual.visual_type)
            if not limits:
                continue
            for role, body in sorted(visual.query_state.items()):
                limit = limits.get(role)
                if limit is None or not isinstance(body, dict):
                    continue
                count = len(body.get("projections", []))
                if count > limit:
                    f.fail(project, "visual role over capacity",
                           f"page '{page.name}' visual {visual.name}: {visual.visual_type} "
                           f"role '{role}' has {count} fields but the field well accepts "
                           f"{limit} -- the editor physically refuses the extra drop")


# PBIR aggregation function codes.
_AGG_NAMES = {0: "Sum", 1: "Avg", 2: "Min", 3: "Max", 4: "CountNonNull", 5: "Count",
              6: "Median", 7: "StdDev", 8: "Var"}
_NUMERIC_ONLY_AGGS = {0, 1, 6, 7, 8}


def check_visual_aggregation_legal(f, project: str, proj: M.Project) -> None:
    """RPT-ROLE-04. Aggregating a measure, or summing a string column."""
    def walk(node, page_name: str, visual: M.Visual) -> None:
        if isinstance(node, dict):
            agg = node.get("Aggregation")
            if isinstance(agg, dict):
                function = agg.get("Function")
                inner: list[tuple[str, str, str]] = []
                _field_refs(agg.get("Expression") or {}, inner)
                for kind, entity, prop in inner:
                    if kind == "Measure":
                        f.fail(project, "aggregation applied to a measure",
                               f"page '{page_name}' visual {visual.name}: "
                               f"{_AGG_NAMES.get(function, function)} applied to measure "
                               f"'{entity}'[{prop}] -- a measure is already aggregated")
                    elif function in _NUMERIC_ONLY_AGGS:
                        table = proj.tables.get(entity)
                        column = table.columns.get(prop) if table else None
                        if column and column.data_type.casefold() in _NON_SUMMABLE:
                            f.fail(project, "numeric aggregation on a non-numeric column",
                                   f"page '{page_name}' visual {visual.name}: "
                                   f"{_AGG_NAMES.get(function, function)} applied to "
                                   f"'{entity}'[{prop}], which is {column.data_type}")
            for value in node.values():
                walk(value, page_name, visual)
        elif isinstance(node, list):
            for item in node:
                walk(item, page_name, visual)

    for page in proj.pages:
        for visual in page.visuals:
            walk(visual.doc, page.name, visual)


def check_visual_filter_targets(f, project: str, proj: M.Project) -> None:
    """RPT-FILTER-02. A filter on a field the model does not have.

    Broken_Filters is page-local and silent until a user opens that exact page -- the same
    surfacing behaviour that made SM-DAX-02 expensive to find by hand.
    """
    if not proj.tables:
        return
    for page in proj.pages:
        for visual in page.visuals:
            for filt in (visual.doc.get("filterConfig") or {}).get("filters", []) or []:
                refs: list[tuple[str, str, str]] = []
                _field_refs(filt.get("field") or {}, refs)
                for kind, entity, prop in refs:
                    table = proj.tables.get(entity)
                    if table is None:
                        f.fail(project, "filter targets a missing table",
                               f"page '{page.name}' visual {visual.name}: filter on "
                               f"'{entity}'[{prop}] -- table '{entity}' is not in the model")
                    elif prop not in table.columns and prop not in table.measures:
                        f.fail(project, "filter targets a missing field",
                               f"page '{page.name}' visual {visual.name}: filter on "
                               f"'{entity}'[{prop}] -- that field is not in the model")


# Filter card kinds that require an ordered or temporal column.
_ORDERED_FILTER_TYPES = {"Range", "RelativeDate", "RelativeTime"}
_ORDERED_TYPES = {"int64", "double", "decimal", "dateTime"}


def check_filter_kind_matches_type(f, project: str, proj: M.Project) -> None:
    """RPT-FILTER-03. A range or relative-date card over a column that has no ordering."""
    for page in proj.pages:
        for visual in page.visuals:
            for filt in (visual.doc.get("filterConfig") or {}).get("filters", []) or []:
                kind = filt.get("type", "")
                if kind not in _ORDERED_FILTER_TYPES:
                    continue
                refs: list[tuple[str, str, str]] = []
                _field_refs(filt.get("field") or {}, refs)
                for _, entity, prop in refs:
                    table = proj.tables.get(entity)
                    column = table.columns.get(prop) if table else None
                    if not column or not column.data_type:
                        continue
                    if column.data_type not in _ORDERED_TYPES:
                        f.fail(project, "filter kind does not suit the column type",
                               f"page '{page.name}' visual {visual.name}: {kind} filter on "
                               f"'{entity}'[{prop}], which is {column.data_type}")
                    elif kind.startswith("Relative") and column.data_type != "dateTime":
                        f.fail(project, "relative-date filter on a non-date column",
                               f"page '{page.name}' visual {visual.name}: {kind} filter on "
                               f"'{entity}'[{prop}], which is {column.data_type}")


def check_visual_filter_relationship_reachable(f, project: str, proj: M.Project) -> None:
    """RPT-FILTER-04. A filter's target table must be relationship-reachable from the visual's
    own queried table(s).

    RPT-FILTER-02 (`check_visual_filter_targets`) only checks that the filter's field EXISTS in
    the model. It says nothing about whether Desktop can relate that table to what the visual is
    actually querying -- confirmed as a real, separate failure on NGPO Dashboard New - AP Version
    (MSC): "Can't determine relationships between the fields... InvalidUnconstrainedJoin", on a
    filter whose own field existed fine (RPT-FILTER-02 clean) but sat on a table with no
    relationship path to the visual's query table (Tableau data blending, no stored key).

    A `Param `-prefixed table is exempt: a slicer's own direct binding to its parameter table is
    this pipeline's deliberate, correct pattern (see `pbir.py`'s `_visual_filters` docstring) and
    needs no relationship -- a parameter table is a domain list, not a fact/dimension pairing.

    Only checked when the visual HAS a query of its own (`query_state` non-empty): a pure
    filter/slicer-only visual has no "own table" for this question to be about, matching exactly
    the scan that found the 117 real instances of this bug (`if extra and query_ents`).
    """
    for page in proj.pages:
        for visual in page.visuals:
            query_entities: set[str] = set()
            _entities_in(visual.query_state, query_entities)
            if not query_entities:
                continue
            adj = proj.relationship_adjacency()
            for filt in (visual.doc.get("filterConfig") or {}).get("filters", []) or []:
                refs: list[tuple[str, str, str]] = []
                _field_refs(filt.get("field") or {}, refs)
                for _, entity, prop in refs:
                    if entity in query_entities or entity.startswith("Param "):
                        continue
                    if proj.relationship_reachable(entity, query_entities, adj):
                        continue
                    f.fail(project, "filter targets a relationship-unreachable table",
                           f"page '{page.name}' visual {visual.name}: filter on "
                           f"'{entity}'[{prop}] has no relationship path to the visual's own "
                           f"queried table(s) {sorted(query_entities)} -- Desktop raises "
                           "InvalidUnconstrainedJoin for exactly this shape")


def check_visual_query_relationship_reachable(f, project: str, proj: M.Project) -> None:
    """RPT-FILTER-05. A visual's OWN field wells (not a filter riding along -- RPT-FILTER-04's
    own concern) must not span two or more tables with no relationship path between them.

    A Tableau data blend can put fields from two federated datasources directly on the SAME
    worksheet's Rows/Cols shelves, with no filter involved at all -- confirmed as a real, DISTINCT
    Desktop failure from RPT-FILTER-04's (which only ever looked at `filterConfig`) on NGPO
    Dashboard New - AP Version (MSC), "Level 2 Analysis"/"Level 4 Analysis" pages: a lineChart
    whose Category came from one federated table and whose Y came from another, `relationships.
    tmdl` empty, Desktop: "Can't determine relationships between the fields... Underlying Error:
    InvalidUnconstrainedJoin". `build-tableau-pbip` now declines this shape at the generator
    (`pbir.py`'s `_visual` / `_all_relationship_reachable`) rather than emitting it, so this is the
    POST_EMIT backstop should that regress, the same relationship RPT-FILTER-04 already has to its
    own generator-side fix.

    A `Param `-prefixed table is exempt, same reasoning as RPT-FILTER-04: a slicer/measure's own
    direct SELECTEDVALUE binding needs no relationship.
    """
    for page in proj.pages:
        for visual in page.visuals:
            query_entities: set[str] = set()
            _entities_in(visual.query_state, query_entities)
            non_param = {e for e in query_entities if not e.startswith("Param ")}
            if len(non_param) <= 1:
                continue
            adj = proj.relationship_adjacency()
            anchor = sorted(non_param)[0]
            unreached = [e for e in non_param if e != anchor
                        and not proj.relationship_reachable(e, {anchor}, adj)]
            if unreached:
                f.fail(project, "visual's own field wells span relationship-unreachable tables",
                       f"page '{page.name}' visual {visual.name}: field wells query "
                       f"{sorted(non_param)}, and {sorted(unreached)} has/have no relationship "
                       f"path to {anchor!r} -- Desktop raises InvalidUnconstrainedJoin for "
                       "exactly this shape")


def _entities_in(node, out: set[str]) -> None:
    """Every distinct `Entity` string nested anywhere under `node`. Same generic walk
    build-tableau-pbip's `pbir.py._query_entities` uses for the same reason: a visual's
    `query.queryState` nests `Entity` at varying depth depending on the well/role."""
    if isinstance(node, dict):
        entity = node.get("Entity")
        if isinstance(entity, str):
            out.add(entity)
        for value in node.values():
            _entities_in(value, out)
    elif isinstance(node, list):
        for item in node:
            _entities_in(item, out)


def check_visual_identity(f, project: str, proj: M.Project) -> None:
    """RPT-ID-03. Visual folder name must equal the name inside, and be unique on the page.

    RPT-ID-01 checks exactly this correlation for PAGES. Visuals are correlated the same
    way and were never checked.
    """
    for page in proj.pages:
        seen: dict[str, int] = {}
        for visual in page.visuals:
            folder = visual.path.parent.name
            if visual.name != folder:
                f.fail(project, "visual name does not match its folder",
                       f"page '{page.name}': folder '{folder}' holds a visual named "
                       f"'{visual.name}' -- Desktop correlates the two and drops the visual "
                       f"when they disagree")
            seen[visual.name] = seen.get(visual.name, 0) + 1
        for name, count in sorted(seen.items()):
            if count > 1:
                f.fail(project, "duplicate visual name",
                       f"page '{page.name}': {count} visuals share the name '{name}'")


def check_visual_position(f, project: str, proj: M.Project) -> None:
    """RPT-POS-01. A visual off the canvas exists but cannot be seen.

    Tableau zone geometry uses different units against a different canvas, so an unscaled
    translation puts visuals outside the page. The canvas size comes from the page itself
    when it declares one, so a legitimately custom page size is not reported.
    """
    for page in proj.pages:
        width = page.doc.get("width")
        height = page.doc.get("height")
        for visual in page.visuals:
            position = visual.doc.get("position") or {}
            for axis in ("x", "y", "width", "height"):
                value = position.get(axis)
                if not isinstance(value, (int, float)):
                    f.fail(project, "visual position is not numeric",
                           f"page '{page.name}' visual {visual.name}: position.{axis} is "
                           f"{value!r}")
                elif value < 0:
                    f.fail(project, "negative visual position",
                           f"page '{page.name}' visual {visual.name}: position.{axis} is "
                           f"{value}")
            if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
                continue
            x, y = position.get("x"), position.get("y")
            vw, vh = position.get("width"), position.get("height")
            if not all(isinstance(v, (int, float)) for v in (x, y, vw, vh)):
                continue
            if x >= width or y >= height:
                f.fail(project, "visual starts off-canvas",
                       f"page '{page.name}' visual {visual.name}: origin ({x}, {y}) is "
                       f"outside the {width}x{height} canvas -- the visual exists but is "
                       f"not visible")


def check_visual_sort_fields(f, project: str, proj: M.Project) -> None:
    """RPT-SORT-01. The sort menu only offers fields the visual already carries."""
    for page in proj.pages:
        for visual in page.visuals:
            sort_def = ((visual.doc.get("visual") or {}).get("query") or {}).get("sortDefinition")
            if not isinstance(sort_def, dict):
                continue
            projected: set[tuple[str, str]] = set()
            for role in visual.query_state.values():
                if isinstance(role, dict):
                    refs: list[tuple[str, str, str]] = []
                    _field_refs(role.get("projections") or [], refs)
                    projected |= {(entity, prop) for _, entity, prop in refs}
            refs = []
            _field_refs(sort_def.get("sort") or [], refs)
            for _, entity, prop in refs:
                if (entity, prop) not in projected:
                    f.fail(project, "sort field not in the visual",
                           f"page '{page.name}' visual {visual.name}: sorts by "
                           f"'{entity}'[{prop}], which is not one of its projections")


def check_navigation_targets(f, project: str, proj: M.Project) -> None:
    """RPT-NAV-01. Page-navigation and bookmark actions pointing at objects that are gone."""
    page_names = {page.name for page in proj.pages}
    visual_names = {visual.name for page in proj.pages for visual in page.visuals}

    def walk(node, page_name: str, visual_name: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("navigationSection", "sectionName") and isinstance(value, str):
                    if value and value not in page_names:
                        f.fail(project, "navigation targets a missing page",
                               f"page '{page_name}' visual {visual_name}: navigates to "
                               f"page '{value}', which is not in the report")
                elif key in ("visualName", "targetVisual") and isinstance(value, str):
                    if value and value not in visual_names:
                        f.fail(project, "action targets a missing visual",
                               f"page '{page_name}' visual {visual_name}: targets visual "
                               f"'{value}', which is not in the report")
                else:
                    walk(value, page_name, visual_name)
        elif isinstance(node, list):
            for item in node:
                walk(item, page_name, visual_name)

    for page in proj.pages:
        for visual in page.visuals:
            walk(visual.doc, page.name, visual.name)


def check_conditional_formatting_refs(f, project: str, proj: M.Project) -> None:
    """RPT-CF-01. Conditional formatting bound to a field the model does not have.

    Scoped to the `objects` block, where formatting lives; queryState references are
    RPT-REF-01's territory and reporting them here would double-count.
    """
    if not proj.tables:
        return
    for page in proj.pages:
        for visual in page.visuals:
            objects = (visual.doc.get("visual") or {}).get("objects")
            if not objects:
                continue
            refs: list[tuple[str, str, str]] = []
            _field_refs(objects, refs)
            for kind, entity, prop in sorted(set(refs)):
                table = proj.tables.get(entity)
                if table is None:
                    f.fail(project, "formatting references a missing table",
                           f"page '{page.name}' visual {visual.name}: formatting binds to "
                           f"'{entity}'[{prop}] -- table '{entity}' is not in the model")
                elif prop not in table.columns and prop not in table.measures:
                    f.fail(project, "formatting references a missing field",
                           f"page '{page.name}' visual {visual.name}: formatting binds to "
                           f"'{entity}'[{prop}] -- that field is not in the model")


def check_report_reference_case(f, project: str, proj: M.Project) -> None:
    """RPT-REF-02. A report reference matching a model object only case-insensitively.

    The report half of SM-DAX-06: a model-side rename that does not rewrite the report
    leaves every visual bound to the old spelling.
    """
    if not proj.tables:
        return
    tables_folded = {name.casefold(): name for name in proj.tables}
    reported: set[tuple[str, str]] = set()
    for page in proj.pages:
        for visual in page.visuals:
            refs: list[tuple[str, str, str]] = []
            _field_refs((visual.doc.get("visual") or {}).get("query") or {}, refs)
            for kind, entity, prop in refs:
                table = proj.tables.get(entity)
                if table is None:
                    actual = tables_folded.get(entity.casefold())
                    if actual and (entity, "") not in reported:
                        reported.add((entity, ""))
                        f.fail(project, "report reference differs by case",
                               f"visuals reference table '{entity}' but the model declares "
                               f"'{actual}'")
                    continue
                if prop in table.columns or prop in table.measures:
                    continue
                for candidate in list(table.columns) + list(table.measures):
                    if candidate.casefold() == prop.casefold() and (entity, prop) not in reported:
                        reported.add((entity, prop))
                        f.fail(project, "report reference differs by case",
                               f"visuals reference '{entity}'[{prop}] but the model declares "
                               f"[{candidate}]")
                        break


# The order checks run in. Grouped by layer so related findings appear together.
ALL_CHECKS = (
    check_measure_names_unique_model_wide,
    check_table_names_unique,
    check_table_namespace_collisions,
    check_name_characters,
    check_name_wellformed,
    check_display_folder_syntax,
    check_reserved_word_names,
    check_source_column_in_partition,
    check_format_string_matches_type,
    check_summarize_by_matches_type,
    check_sort_by_column_same_table,
    check_sort_by_cycles,
    check_hierarchy_levels,
    check_relationship_endpoints_exist,
    check_relationship_type_match,
    check_one_to_one_cross_filter_direction,
    check_relationship_ambiguity,
    check_calc_dependency_cycles,
    check_tables_have_partitions,
    check_role_names_unique,
    check_calculation_groups,
    check_perspective_references,
    check_dax_balanced,
    check_measure_bare_column,
    check_reference_case,
    check_m_structure,
    check_m_parameter_refs,
    check_visual_types_known,
    check_visual_has_required_data,
    check_visual_role_cardinality,
    check_visual_aggregation_legal,
    check_visual_filter_targets,
    check_filter_kind_matches_type,
    check_visual_filter_relationship_reachable,
    check_visual_query_relationship_reachable,
    check_visual_identity,
    check_visual_position,
    check_visual_sort_fields,
    check_navigation_targets,
    check_conditional_formatting_refs,
    check_report_reference_case,
)
