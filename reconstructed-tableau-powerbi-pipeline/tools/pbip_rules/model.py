#!/usr/bin/env python3
"""Parse a PBIP project once into a structured model the rule checks can interrogate.

Why a parser rather than more regexes
-------------------------------------
The checks already in `pbip_lint.py` each re-scan the TMDL with their own regex. That is
fine for a check that asks one local question ("is this dataType in the enum?"), and it
does not scale to the rules this module exists to serve, which are about RELATIONSHIPS
between objects: does this sortByColumn live on the same table, is this dependency graph
acyclic, does this sourceColumn exist in the partition's own M output. Those need the
whole model in memory at once.

TMDL shape, as actually emitted
-------------------------------
Verified against the generated corpus rather than assumed:

    table 'Europe Report'                       <- indent 0
        lineageTag: ...                         <- indent 1, a property
        column 'Created By User Name'           <- indent 1, a child object
            dataType: string                    <- indent 2, its properties
            isHidden                            <-   valueless property (a flag)
            sourceColumn: 'Created By User Name'
        column 'Potential Violated Merchant' = IF(...)   <- single-line expression
        column Clusters =                       <- expression is the indented block below
            <dax>
        measure 'PV Growth' = DIVIDE(...)
        hierarchy 'Issr Region Hierarchy'
            level 'Issr Region'
                column: 'Issr Region'           <- note the colon: a property, not a child
        partition 'smekb.mvv_dashbord_dump' = m
            mode: import
            source =
                let Source = Table.FromRows(...) in Source

Two syntactic traps this parser handles and a naive one does not:

1. `column 'X'` (a child object) versus `column: 'X'` (a property of a hierarchy level).
   The colon is the entire difference, and confusing them invents phantom columns on every
   hierarchy.
2. A trailing `=` means the expression is the indented block BELOW, not empty. The corpus
   uses this form for 10+ calculated columns and every single partition. Treating those as
   empty expressions silently drops them from every dependency check.

Names are unquoted with the same rules `pbip_lint._tmdl_declared_name` uses, because an
unquoted TMDL identifier may legally contain `/`, `%`, `-` and parens -- a `\\w+` match
truncates `Token/PAN` to `Token` and reports a false collision.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- name handling


def unquote_tmdl(rest: str) -> str:
    """The declared name from a `column 'x' [= expr]` remainder, ignoring the expression."""
    rest = rest.strip()
    if not rest.startswith("'"):
        match = re.match(r"[^\s=:]+", rest)
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


def _strip_value(text: str) -> str:
    """A property's value, with TMDL quoting removed if present."""
    text = text.strip()
    if text.startswith("'"):
        return unquote_tmdl(text)
    return text


# ---------------------------------------------------------------- generic block tree


@dataclass
class Node:
    """One TMDL line plus everything indented beneath it."""

    keyword: str
    rest: str
    indent: int
    line: int
    children: list["Node"] = field(default_factory=list)
    block: str = ""          # raw text of the indented region, for expressions and M
    is_property: bool = False  # `name: value` or a bare flag, rather than a child object


_DECL = re.compile(r"^(?P<indent>\t*)(?P<body>.*)$")


def parse_tmdl(text: str) -> list[Node]:
    """Indentation-structured parse. Blank lines belong to whatever block encloses them."""
    raw = text.replace("\r\n", "\n").split("\n")
    nodes: list[Node] = []
    stack: list[Node] = []

    i = 0
    while i < len(raw):
        line = raw[i]
        if not line.strip():
            i += 1
            continue
        match = _DECL.match(line)
        indent = len(match.group("indent"))
        body = match.group("body").rstrip()

        while stack and stack[-1].indent >= indent:
            stack.pop()

        keyword, _, rest = body.partition(" ")
        is_property = False
        if keyword.endswith(":"):
            keyword, is_property = keyword[:-1], True
        elif ":" in keyword and not rest:
            keyword, _, rest = body.partition(":")
            is_property = True
        elif " " not in body and "=" not in body:
            is_property = True  # a valueless flag such as `isHidden`

        node = Node(keyword=keyword, rest=rest.strip(), indent=indent, line=i + 1,
                    is_property=is_property)

        # A trailing `=` hands the expression to the indented block below.
        #
        # The block ends where the expression's own indentation ends -- NOT merely where the
        # declaration's indentation resumes. A calculated column is written as
        #
        #     column 'X' =            <- indent 1
        #             SWITCH(...)     <- indent 3, the expression
        #         lineageTag: ...     <- indent 2, a sibling PROPERTY, not expression text
        #
        # so anchoring the end on the declaration's indent silently swallows every property
        # that follows the expression, corrupting the DAX with trailing metadata.
        if body.rstrip().endswith("="):
            block: list[str] = []
            pending_blanks = 0
            body_indent: int | None = None
            j = i + 1
            while j < len(raw):
                nxt = raw[j]
                if not nxt.strip():
                    pending_blanks += 1
                    j += 1
                    continue
                nxt_indent = len(_DECL.match(nxt).group("indent"))
                if body_indent is None:
                    if nxt_indent <= indent:
                        break
                    body_indent = nxt_indent
                elif nxt_indent < body_indent:
                    break
                block.extend([""] * pending_blanks)
                pending_blanks = 0
                block.append(nxt.strip())
                j += 1
            node.block = "\n".join(block).strip()

        if stack:
            stack[-1].children.append(node)
        else:
            nodes.append(node)
        stack.append(node)
        i += 1

    return nodes


def _props(node: Node) -> dict[str, str]:
    """Property children of a node, as name -> value ('' for a valueless flag)."""
    return {c.keyword: _strip_value(c.rest) for c in node.children if c.is_property}


# ---------------------------------------------------------------- model objects


@dataclass
class Member:
    kind: str                     # "column" or "measure"
    name: str
    table: str
    expression: str               # "" when the object is not calculated
    props: dict[str, str]
    line: int

    @property
    def is_calculated(self) -> bool:
        return bool(self.expression)

    @property
    def data_type(self) -> str:
        return self.props.get("dataType", "")


@dataclass
class Hierarchy:
    name: str
    table: str
    levels: list[tuple[str, str]]  # (level name, column it references)
    line: int


@dataclass
class Partition:
    name: str
    table: str
    kind: str                      # "m", "calculated", "entity", ...
    mode: str
    source: str
    line: int


@dataclass
class Table:
    name: str
    path: Path
    props: dict[str, str] = field(default_factory=dict)
    columns: dict[str, Member] = field(default_factory=dict)
    measures: dict[str, Member] = field(default_factory=dict)
    hierarchies: list[Hierarchy] = field(default_factory=list)
    partitions: list[Partition] = field(default_factory=list)

    @property
    def is_calculated(self) -> bool:
        return any(p.kind == "calculated" for p in self.partitions)


@dataclass
class Relationship:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    is_active: bool
    from_cardinality: str          # raw TMDL value, "" if omitted (TOM default: "many")
    to_cardinality: str            # raw TMDL value, "" if omitted (TOM default: "one")
    cross_filtering_behavior: str  # raw TMDL value, "" if omitted (TOM default: "single")
    line: int

    @property
    def is_one_to_one(self) -> bool:
        """True when both sides resolve to cardinality "one", using the Tabular Object Model's
        OWN defaults for whichever side is omitted -- fromCardinality defaults to "many",
        toCardinality defaults to "one". A relationship that sets ONLY `fromCardinality: one`
        and leaves toCardinality unstated (exactly the shape a hand-authored star-schema split
        naturally produces) is STILL a real one-to-one relationship at the engine level, so an
        omitted toCardinality must resolve to its own default "one" here, not to "not one"."""
        effective_from = self.from_cardinality or "many"
        effective_to = self.to_cardinality or "one"
        return effective_from == "one" and effective_to == "one"


@dataclass
class Visual:
    name: str
    path: Path
    page: str
    visual_type: str
    doc: dict

    @property
    def query_state(self) -> dict:
        return (self.doc.get("visual") or {}).get("query", {}).get("queryState", {}) or {}


@dataclass
class Page:
    name: str
    path: Path
    doc: dict
    visuals: list[Visual] = field(default_factory=list)


@dataclass
class Project:
    root: Path
    name: str
    sm_dir: Path | None
    report_dir: Path | None
    tables: dict[str, Table] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    expressions: dict[str, str] = field(default_factory=dict)
    perspectives: dict[str, list[str]] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    # ---- lookups the checks need constantly

    def measure_index(self) -> dict[str, list[Member]]:
        """measure name -> every measure with that name. Length > 1 is SM-NAME-02."""
        out: dict[str, list[Member]] = {}
        for table in self.tables.values():
            for measure in table.measures.values():
                out.setdefault(measure.name, []).append(measure)
        return out

    def all_members(self):
        for table in self.tables.values():
            yield from table.columns.values()
            yield from table.measures.values()

    def has_object(self, table: str, name: str) -> bool:
        tbl = self.tables.get(table)
        return bool(tbl and (name in tbl.columns or name in tbl.measures))

    def relationship_adjacency(self) -> dict[str, set[str]]:
        """Every table's directly-related tables, UNDIRECTED -- built once and reused, mirroring
        build-tableau-pbip's own `pbir.py` ReportBuilder.rel_adj exactly, since a rule here is
        checking the same real-Desktop failure (`InvalidUnconstrainedJoin`) the generator's own
        `_relationship_reachable` was written to avoid producing. Cross-filtering direction is a
        real, separate concept `Relationship.is_active` etc. already carries; this graph only
        answers "is there ANY relationship chain to walk", which is undirected."""
        adj: dict[str, set[str]] = {}
        for rel in self.relationships:
            adj.setdefault(rel.from_table, set()).add(rel.to_table)
            adj.setdefault(rel.to_table, set()).add(rel.from_table)
        return adj

    def relationship_reachable(self, entity: str, targets: set[str],
                                adj: dict[str, set[str]] | None = None) -> bool:
        """True if `entity` IS one of `targets`, or connected to one by a relationship chain."""
        if entity in targets:
            return True
        adj = self.relationship_adjacency() if adj is None else adj
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


# ---------------------------------------------------------------- loading


_OBJECT_KEYWORDS = {"column", "measure", "hierarchy", "partition", "level"}


def _load_table(path: Path, errors: list[str]) -> Table | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # unreadable file is a finding, not a crash
        errors.append(f"{path.name}: unreadable ({exc})")
        return None

    roots = [n for n in parse_tmdl(text) if n.keyword == "table"]
    if not roots:
        errors.append(f"{path.name}: no `table` declaration")
        return None

    root = roots[0]
    table = Table(name=unquote_tmdl(root.rest), path=path, props=_props(root))

    for child in root.children:
        # `column: 'X'` inside a hierarchy level is a PROPERTY, not a column declaration.
        if child.is_property or child.keyword not in _OBJECT_KEYWORDS:
            continue
        name = unquote_tmdl(child.rest)

        if child.keyword in ("column", "measure"):
            expression = child.block
            if not expression and "=" in child.rest:
                # single-line form: everything after the first `=` outside the quoted name
                expression = _expression_after_name(child.rest)
            member = Member(kind=child.keyword, name=name, table=table.name,
                            expression=expression.strip(), props=_props(child), line=child.line)
            (table.columns if child.keyword == "column" else table.measures)[name] = member

        elif child.keyword == "hierarchy":
            levels = []
            for lvl in child.children:
                if lvl.keyword == "level" and not lvl.is_property:
                    col = ""
                    for prop in lvl.children:
                        if prop.keyword == "column" and prop.is_property:
                            col = _strip_value(prop.rest)
                    levels.append((unquote_tmdl(lvl.rest), col))
            table.hierarchies.append(
                Hierarchy(name=name, table=table.name, levels=levels, line=child.line))

        elif child.keyword == "partition":
            kind = ""
            if "=" in child.rest:
                kind = child.rest.split("=", 1)[1].strip()
            props = _props(child)
            source = ""
            for sub in child.children:
                if sub.keyword == "source":
                    source = sub.block or sub.rest.lstrip("= ").strip()
            table.partitions.append(
                Partition(name=name, table=table.name, kind=kind,
                          mode=props.get("mode", ""), source=source, line=child.line))

    return table


def _expression_after_name(rest: str) -> str:
    """Split `'A ''quoted'' name' = EXPR` at the `=` that follows the name, not inside it."""
    rest = rest.strip()
    if not rest.startswith("'"):
        _, sep, expr = rest.partition("=")
        return expr if sep else ""
    i = 1
    while i < len(rest):
        if rest[i] == "'":
            if i + 1 < len(rest) and rest[i + 1] == "'":
                i += 2
                continue
            break
        i += 1
    tail = rest[i + 1:]
    _, sep, expr = tail.partition("=")
    return expr if sep else ""


def _load_relationships(sm_dir: Path) -> list[Relationship]:
    path = sm_dir / "definition" / "relationships.tmdl"
    if not path.exists():
        return []
    out = []
    for node in parse_tmdl(path.read_text(encoding="utf-8")):
        if node.keyword != "relationship":
            continue
        props = _props(node)
        # fromColumn/toColumn are qualified `'Table'.Column` refs, not `name = expr`
        # declarations, so they must bypass _props()'s unquote_tmdl-based stripping,
        # which discards everything after the closing quote (the ".Column" part).
        raw = {c.keyword: c.rest for c in node.children if c.is_property}
        from_col = raw.get("fromColumn", "")
        to_col = raw.get("toColumn", "")
        out.append(Relationship(
            name=unquote_tmdl(node.rest),
            from_table=_qualifier(from_col), from_column=_unqualified(from_col),
            to_table=_qualifier(to_col), to_column=_unqualified(to_col),
            is_active=props.get("isActive", "true").lower() != "false",
            from_cardinality=props.get("fromCardinality", ""),
            to_cardinality=props.get("toCardinality", ""),
            cross_filtering_behavior=props.get("crossFilteringBehavior", ""),
            line=node.line,
        ))
    return out


def _split_qualified(ref: str) -> tuple[str, str]:
    """`Table.Column` or `'Table Name'.Column` -> (table, column)."""
    ref = ref.strip()
    if ref.startswith("'"):
        end = 1
        while end < len(ref):
            if ref[end] == "'":
                if end + 1 < len(ref) and ref[end + 1] == "'":
                    end += 2
                    continue
                break
            end += 1
        return unquote_tmdl(ref[:end + 1]), _strip_value(ref[end + 2:])
    table, _, column = ref.partition(".")
    return table, _strip_value(column)


def _qualifier(ref: str) -> str:
    return _split_qualified(ref)[0]


def _unqualified(ref: str) -> str:
    return _split_qualified(ref)[1]


def _load_report(report_dir: Path) -> list[Page]:
    pages_dir = report_dir / "definition" / "pages"
    if not pages_dir.is_dir():
        return []
    pages = []
    for page_dir in sorted(p for p in pages_dir.iterdir() if p.is_dir()):
        page_json = page_dir / "page.json"
        try:
            doc = json.loads(page_json.read_text(encoding="utf-8"))
        except Exception:
            doc = {}
        page = Page(name=doc.get("name", page_dir.name), path=page_dir, doc=doc)
        visuals_dir = page_dir / "visuals"
        if visuals_dir.is_dir():
            for vdir in sorted(v for v in visuals_dir.iterdir() if v.is_dir()):
                vpath = vdir / "visual.json"
                try:
                    vdoc = json.loads(vpath.read_text(encoding="utf-8"))
                except Exception:
                    continue
                page.visuals.append(Visual(
                    name=vdoc.get("name", vdir.name), path=vpath, page=page.name,
                    visual_type=(vdoc.get("visual") or {}).get("visualType", ""), doc=vdoc,
                ))
        pages.append(page)
    return pages


def load_project(root: Path) -> Project:
    """Parse one project folder. Missing pieces are recorded, never raised."""
    sm_dirs = sorted(root.glob("*.SemanticModel"))
    report_dirs = sorted(root.glob("*.Report"))
    proj = Project(root=root, name=root.name,
                   sm_dir=sm_dirs[0] if sm_dirs else None,
                   report_dir=report_dirs[0] if report_dirs else None)

    if proj.sm_dir:
        tables_dir = proj.sm_dir / "definition" / "tables"
        if tables_dir.is_dir():
            for path in sorted(tables_dir.glob("*.tmdl")):
                table = _load_table(path, proj.parse_errors)
                if table:
                    proj.tables[table.name] = table
        proj.relationships = _load_relationships(proj.sm_dir)

        roles_dir = proj.sm_dir / "definition" / "roles"
        if roles_dir.is_dir():
            for path in sorted(roles_dir.glob("*.tmdl")):
                for node in parse_tmdl(path.read_text(encoding="utf-8")):
                    if node.keyword == "role":
                        proj.roles.append(unquote_tmdl(node.rest))

        expr_path = proj.sm_dir / "definition" / "expressions.tmdl"
        if expr_path.exists():
            for node in parse_tmdl(expr_path.read_text(encoding="utf-8")):
                if node.keyword == "expression":
                    name = unquote_tmdl(node.rest)
                    proj.expressions[name] = node.block or node.rest

        persp_dir = proj.sm_dir / "definition" / "perspectives"
        if persp_dir.is_dir():
            for path in sorted(persp_dir.glob("*.tmdl")):
                for node in parse_tmdl(path.read_text(encoding="utf-8")):
                    if node.keyword == "perspective":
                        proj.perspectives[unquote_tmdl(node.rest)] = [
                            unquote_tmdl(c.rest) for c in node.children
                            if c.keyword == "perspectiveTable"
                        ]

    if proj.report_dir:
        proj.pages = _load_report(proj.report_dir)

    return proj
