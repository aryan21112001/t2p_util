#!/usr/bin/env python3
"""
S3 -- the TMDL semantic-model writer.

Consumes the IR from extract.py, resolves every identifier through names.NameAuthority, translates
formulas through dax.translate, and emits a TMDL `definition/` folder.

Three rules govern this file.

1.  TAB indentation, one tab per level. The format spec calls this "strictly enforced", and a
    space-indented file is rejected wholesale rather than per-line, so indentation is produced by
    one function (`_line`) and never by hand-written literals.

2.  A multi-line expression is indented ONE LEVEL DEEPER than its parent object's properties. This
    is the rule most easily got wrong, because the natural instinct is to align the continuation
    with the property that introduced it. `_expr` is the only place expressions are laid out.

3.  Fail closed. A formula the D+ tier declines produces a residue entry and NO TMDL object.
    A declined formula must never be emitted as a comment-with-placeholder or a zero-returning
    stub: both load cleanly and both silently give the user a wrong number.

The partition question, and why the default is what it is
--------------------------------------------------------
A Tableau connection is not a Power Query expression. For a handful of connection classes the M
function is a mechanical mapping; for the rest -- `presto`, `sqlproxy`, `hyper` -- there is no
supported M connector to map to at all, and a `.hyper` extract simply cannot be read by Power
Query. So the default partition mode is `stub`: a typed, empty `Table.FromRows` whose column list
and data types are exactly the model's. That produces a project that opens in Desktop, validates
every DAX expression against a real schema, and asks for no credentials -- and is honest, because
it claims to carry no data rather than claiming a connection that was never verified.

`--partitions native` emits real M for the mapped classes. It is opt-in because those mappings are
from the M connector library rather than from anything in docs/examples, and are therefore
unverified here in exactly the sense §4 of the schema reference means.
"""
import os
import re
import uuid
import collections

import dax
from names import tmdl_ident, dax_ref, NameAuthority

# A fixed namespace so lineageTags are stable across runs. A regenerated model whose lineageTags
# moved is treated by downstream tooling as a different model, discarding field-level lineage.
NS = uuid.UUID("6f1c1de6-1d61-5a2f-93c3-0f0b2a4d7e11")

# Confirmed against a Desktop-authored database.tmdl (Feb 2026, 2.151.1182.0): 1600, not the 1567
# the schema reference's example shows.
COMPATIBILITY_LEVEL = 1600

# A multi-line expression body sits TWO levels below the property that introduces it, not one.
# Desktop writes `source =` at level 2 and `let` at level 4. The reference says "one extra level",
# which is what produced level 3 here; matching the real file is worth more than matching the prose.
EXPR_EXTRA_INDENT = 2

# role -> summarizeBy. Tableau's role IS the aggregation intent, so this is a real mapping and not
# a default: a measure gets implicit SUM, a dimension gets none.
SUMMARIZE = {"measure": "sum", "dimension": "none"}

# ...but ONLY on a numeric column. TOM rejects `summarizeBy: sum` on a string or date column, and
# Tableau's role is not always consistent with the column's type: 46 of 1113 corpus columns arrived
# as role=measure with a non-numeric datatype, every one of which would have failed at model load.
NUMERIC = {"int64", "double", "decimal"}


def summarize_by(role, dtype):
    return SUMMARIZE.get(role, "none") if dtype in NUMERIC else "none"

# Tableau aggregation -> DAX function, for a shelf-level aggregate that has no calculated field.
AGG_DAX = {"sum": "SUM", "avg": "AVERAGE", "min": "MIN", "max": "MAX",
           "cnt": "COUNT", "count": "COUNT", "ctd": "DISTINCTCOUNT", "countd": "DISTINCTCOUNT",
           "median": "MEDIAN", "stdev": "STDEV.S", "var": "VAR.S"}

# Connection class -> M. Only classes whose connector name is unambiguous. Anything absent falls
# back to a stub partition and is reported -- never guessed at.
NATIVE_M = {
    "sqlserver":   lambda c: f'Sql.Database("{c.get("server") or ""}", "{c.get("dbname") or ""}")',
    "postgres":    lambda c: f'PostgreSQL.Database("{c.get("server") or ""}", "{c.get("dbname") or ""}")',
    "mysql":       lambda c: f'MySQL.Database("{c.get("server") or ""}", "{c.get("dbname") or ""}")',
    "oracle":      lambda c: f'Oracle.Database("{c.get("server") or ""}")',
    "snowflake":   lambda c: f'Snowflake.Databases("{c.get("server") or ""}", "")',
    "excel-direct": lambda c: f'Excel.Workbook(File.Contents("{(c.get("filename") or "").replace(chr(92), "/")}"), true)',
    "textscan":    lambda c: f'Csv.Document(File.Contents("{(c.get("filename") or "").replace(chr(92), "/")}"), [Delimiter=",", Encoding=65001])',
}

# M type literal per TMDL dataType, for the stub partition's `type table [...]` ascription.
# No "date" entry: see _tom_dtype -- nothing should reach this dict still carrying that value.
M_TYPE = {"string": "text", "int64": "Int64.Type", "double": "number", "decimal": "number",
          "boolean": "logical", "dateTime": "datetime"}


def _tom_dtype(dtype):
    """Map a Tableau/extract.py datatype string to a value TOM's own DataType enum accepts.

    extract.py's `DTYPE` normalizes a Tableau date-only field to `"date"`, kept distinct from
    `"dateTime"` because some consumers care (dax.py's STR()-on-a-date decline checks for either
    value, so it does not actually need the distinction, but the docs/schema-reference table this
    project has otherwise trusted -- "Column Data Types": `date` = "Date only" -- claims `date` is
    a valid TOM DataType, and it is not). Confirmed against real Desktop, on a real corpus
    workbook's date column: "TMDL Format Error ... Failed to convert the value 'date' to the
    expected type DataType! ... Requested value 'date' was not found." -- TOM has no `date` type,
    only `dateTime`, and this is a hard model-load failure for the WHOLE project, not one column.
    This is the one place `date` must become `dateTime`: at the boundary where a dataType string
    is about to be serialised into TMDL or an M type ascription. Every other consumer of the raw
    `datatype` value (dax.py, this file's own NUMERIC/SUMMARIZE lookups) is unaffected.
    """
    return "dateTime" if dtype == "date" else dtype


_M_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# decode_param_value() strips Tableau's `#...#` date fencing but returns the inner text as a
# plain str, e.g. "2026-05-28" or "2026-05-28 14:30:00" -- always ISO, per Tableau's own writer.
_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?$")


def m_ident(name):
    """Quote a name as a Power Query (M) identifier: `#"Billable BID"`, not `'Billable BID'`.

    M and TMDL use DIFFERENT quoting. Reusing tmdl_ident here produced `'Billable BID' = text`
    inside a `type table [...]` ascription, which is not valid M -- and since most Tableau captions
    contain spaces, nearly every stub partition was malformed. Nothing caught it because the
    partition body is opaque text to a TMDL-level check.
    """
    s = str(name)
    if _M_PLAIN.match(s):
        return s
    return '#"' + s.replace('"', '""') + '"'


def _param_literal(v, dtype):
    """An M row-literal for one parameter domain value, inside `Table.FromRows({..}, type table
    [Value = <mtype>])`.

    `dtype` (the parameter's OWN declared Tableau datatype) is authoritative; `v`'s Python type is
    not. Same bug class as pbir.py's `_filter_literal`: `decode_param_value()` only casts to
    int/float when the raw text parses as one, so a Tableau BOOLEAN parameter whose domain values
    are the literal strings "true"/"false" would otherwise fall through to the quoted-string
    branch below -- `"true"` inside a `type table [Value = logical]` ascription, a value/type
    mismatch in the M partition. No boolean-parameter workbook has hit this yet, but the
    mechanism is identical to the filter-literal bug that DID ship, so it is closed here
    pre-emptively rather than waiting for a 3000-workbook run to find the one that hits it.

    date/dateTime got this same treatment only AFTER shipping, not pre-emptively: `_tom_dtype`
    (this file) correctly normalizes the COLUMN's ascription to `datetime`, but a date/dateTime
    parameter's domain value is still Tableau text like "2026-05-28" -- quoting it, as the
    fallback branch below does for anything else, produces `Table.FromRows({{"2026-05-28"}},
    type table [Value = datetime])`. Confirmed against real Desktop: "Expression.Error: The type
    of the value does not match the type of the column." A quoted STRING is not an M `datetime`
    value no matter how date-shaped the text looks. Needs a real `#datetime(...)` literal.
    """
    if dtype == "boolean" or isinstance(v, bool):
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "1", "yes", "-1")
        return "true" if v else "false"
    if dtype in ("date", "dateTime"):
        m = _DATETIME_RE.match(str(v).strip()) if v is not None else None
        if m:
            y, mo, d, h, mi, s = m.groups()
            return (f"#datetime({int(y)}, {int(mo)}, {int(d)}, "
                    f"{int(h or 0)}, {int(mi or 0)}, {int(s or 0)})")
        # Unparseable date text: Tableau's own parameter values are always ISO, so this should not
        # happen. Falls through to the quoted-string branch below -- which still mismatches a
        # `datetime` column, same as before this fix, but a bad ISO string is not something to
        # guess a date out of.
    if dtype in ("int64", "double") or isinstance(v, (int, float)):
        try:
            return str(int(v)) if dtype == "int64" else str(float(v))
        except (TypeError, ValueError):
            pass
    return '"' + str(v).replace('"', '""') + '"'


def _param_dax_literal(v, dtype):
    """A DAX literal for one parameter's saved Tableau current value -- used as
    SELECTEDVALUE's second (alternateResult) argument, so the measure resolves to Tableau's own
    default the moment the report opens, before anything is picked in a slicer.

    Deliberately NOT `_param_literal` above: that one emits M syntax (`true`/`false`,
    `#datetime(...)`) for the table's row literals, which is invalid DAX. Same datatype dispatch,
    different target language -- DAX's own literal forms (`TRUE()`/`FALSE()`, `DATE(y, m, d)`)
    are pure DAX syntax already used elsewhere in this file, not a new unconfirmed construct.
    """
    if dtype == "boolean" or isinstance(v, bool):
        if isinstance(v, str):
            v = v.strip().lower() in ("true", "1", "yes", "-1")
        return "TRUE()" if v else "FALSE()"
    if dtype in ("date", "dateTime"):
        m = _DATETIME_RE.match(str(v).strip()) if v is not None else None
        if m:
            y, mo, d, h, mi, s = m.groups()
            date_part = f"DATE({int(y)}, {int(mo)}, {int(d)})"
            if any(int(x or 0) for x in (h, mi, s)):
                return f"{date_part} + TIME({int(h or 0)}, {int(mi or 0)}, {int(s or 0)})"
            return date_part
        # Unparseable date text -- same rationale as _param_literal's identical fallthrough:
        # Tableau's own parameter values are always ISO, so this should not happen in practice.
    if dtype in ("int64", "double") or isinstance(v, (int, float)):
        try:
            return str(int(v)) if dtype == "int64" else str(float(v))
        except (TypeError, ValueError):
            pass
    return '"' + str(v).replace('"', '""') + '"'


def lineage(*parts):
    return str(uuid.uuid5(NS, "".join(str(p) for p in parts)))


def _line(level, text):
    return "\t" * level + text


def _expr(level, prop, body):
    """Emit `prop =` followed by the expression body indented ONE level deeper.

    A single-line body still goes on its own line rather than being appended after the `=`. Both
    forms are legal, but one code path means the multi-line indentation rule cannot be applied in
    some places and forgotten in others.
    """
    raw = str(body).rstrip().split("\n")
    pad = min((len(ln) - len(ln.lstrip()) for ln in raw if ln.strip()), default=0)
    out = [_line(level, f"{prop} =")]
    for ln in raw:
        if not ln.strip():
            out.append("")
            continue
        # Keep the body's own relative structure, but express it in TABS. Preserving it as spaces
        # is what a naive re-indent does and it puts space indentation inside a TMDL file, which
        # the pre-flight rejects -- TMDL uses tabs for depth, and depth is how it decides where a
        # multi-line expression ends. Four spaces of source indentation is one tab.
        depth = (len(ln) - len(ln.lstrip()) - pad) // 4
        out.append(_line(level + EXPR_EXTRA_INDENT + max(0, depth), ln.strip()))
    return out


def _display_folder_line(level, folder):
    """`displayFolder: <path>` from a Tableau <folders-common> path, or nothing.

    SM-NAME-07: a backslash-separated path with no leading/trailing separator and no empty
    segment -- so each `\\`-joined piece from extract.parse_folders is stripped and empty
    pieces dropped here, once, rather than trusting the source XML's whitespace.

    Quoting: `docs/schema-reference/pbip-schema.md` claims double quotes for a value with
    "special chars", but that reference has been wrong about TMDL syntax before (see tmdl.py's
    module docstring) and tools/pbip_rules/model.py's own parser -- the thing that actually
    reads this file back -- only understands SINGLE-quote unescaping (`_strip_value` calls
    `unquote_tmdl`, which looks for a LEADING `'`). A value that unquoted would itself start
    with `'` is the one case genuinely ambiguous to that parser (it would try to consume it as
    a quoted string); everything else, including `\\`, `:` and `"` mid-value, parses fine
    because `displayFolder: ` already split key from value on the first space. So: quote with
    single quotes, doubling any embedded `'`, matching `tmdl_ident`'s own convention -- and
    only do it when the value actually starts with `'`.
    """
    if not folder:
        return []
    path = "\\".join(seg for seg in (s.strip() for s in str(folder).split("\\")) if seg)
    if not path:
        return []
    if path.startswith("'"):
        path = "'" + path.replace("'", "''") + "'"
    return [_line(level, f"displayFolder: {path}")]


def _calc_column(pbi, name, expr, summarize, dtype=None, folder=None, format_string=None):
    """`column 'X' = <dax>` -- the ONLY place a calculated column is written.

    There were three: calculated fields, groups and bins. When the form changed from an
    `expression =` property to the inline declaration, only the first was updated, and Desktop
    rejected the other two:

        Property 'expression' is unknown and is not expected in the situation it appears.
        at TmdlObjectReader.ReadTableColumnsProperty

    That is a duplication defect, not a knowledge one -- the correct syntax was already known and
    already in the file. One construct, one emitter.

    `dtype`, when given, emits an explicit `dataType:`. Desktop itself omits this for a calculated
    column (confirmed against docs/examples/GroundTruth) -- but that example is never the target
    of a report filter. A real workbook (WorkbookB Dashboard) that DOES filter a calculated boolean
    column ('cl.Is Selected Slice By?') failed to load with "Broken_Filters": every OTHER filtered
    column in that same report is a plain source column (always has an explicit dataType) and
    loaded fine, so a calculated column's dataType being left for the engine to infer is the one
    variable that differs. Declaring it explicitly, using the type Tableau itself already recorded
    for the field, removes the ambiguity regardless of the exact TOM mechanics behind it.
    """
    out = []
    if "\n" in str(expr):
        out.append(_line(1, f"column {tmdl_ident(name)} ="))
        for ln in str(expr).split("\n"):
            out.append(_line(1 + EXPR_EXTRA_INDENT, ln.strip()))
    else:
        out.append(_line(1, f"column {tmdl_ident(name)} = {expr}"))
    if dtype is not None:
        out.append(_line(2, f"dataType: {_tom_dtype(dtype)}"))
    if format_string:
        out.append(_line(2, f"formatString: {format_string}"))
    out.append(_line(2, f"lineageTag: {lineage('column', pbi, name)}"))
    out.append(_line(2, f"summarizeBy: {summarize}"))
    out += _display_folder_line(2, folder)
    out.append("")
    out.append(_line(2, "annotation SummarizationSetBy = Automatic"))
    out.append("")
    return out


def _measure(pbi, name, expr, tag="measure", folder=None, format_string=None):
    """`measure 'X' = <dax>` -- inline when the DAX is one line, which is what Desktop writes.

    No formatString is emitted UNLESS `format_string` is given. Desktop always writes one
    (`formatString: 0` for an integer SUM), but it is optional, and guessing one would silently
    change how every number displays: a missing formatString falls back to a sensible default, a
    wrong one is invisible and wrong. `format_string` is only ever passed when Tableau's own
    `default-format` attribute on the source calc translated with confidence (see tmdl.py's
    caller and extract.py's `translate_number_format`) -- reading Tableau's own declaration, not
    guessing one from nothing. Line ORDER (formatString before lineageTag) confirmed against
    `docs/examples/GroundTruth.SemanticModel/definition/tables/Sales Data.tmdl`'s own
    Desktop-authored `measure 'Total Amount'`.
    """
    out = []
    if "\n" in str(expr):
        out.append(_line(1, f"measure {tmdl_ident(name)} ="))
        for ln in str(expr).split("\n"):
            out.append(_line(1 + EXPR_EXTRA_INDENT, ln.strip()))
    else:
        out.append(_line(1, f"measure {tmdl_ident(name)} = {expr}"))
    if format_string:
        out.append(_line(2, f"formatString: {format_string}"))
    out.append(_line(2, f"lineageTag: {lineage(tag, pbi, name)}"))
    out += _display_folder_line(2, folder)
    out.append("")
    return out


def _desc(level, text):
    """Deliberately emits NOTHING.

    A `///` description is documented in the schema reference, but Desktop's own file carries none
    and the reference has now been wrong about TMDL seven times -- including `//` comments in
    relationships.tmdl, which abort the entire project. A description is cosmetic; a project that
    will not open is not. Tableau field descriptions are reported in COVERAGE.md instead.
    """
    return []


class ResidueLog:
    """Everything not translated, with the reason. The contract the LLM/human tiers plug into.

    Its job is to make under-delivery visible. A build that quietly emits fewer measures than the
    source had is indistinguishable from a successful one unless the shortfall is counted here.
    """

    def __init__(self):
        self.items = []

    def add(self, tier, table, subject, reason, formula=None):
        self.items.append({"tier": tier, "table": table, "subject": subject,
                           "reason": reason, "formula": formula})

    def by_tier(self):
        return collections.Counter(i["tier"] for i in self.items)


def fkey(c):
    """The IDENTITY of a Tableau field: its internal name, falling back to the caption.

    Distinct from its label. Two calcs in one datasource can share a caption, and keying the Name
    Authority on the caption merges them -- both then emit under one name, which TOM rejects.
    """
    return (c.get("tableau_name") or "").strip("[]") or c["name"]


_RANK_PARTITION_FUNC_RE = re.compile(r"^\s*(RANK|RANK_DENSE)\s*\(", re.I)


def route(calc, table_columns=None):
    """Which tier owns this calc, before the parser is even tried.

    `kind` comes from extract.classify_formula. A table calc is routed to H without attempting
    translation: WINDOW_/RUNNING_/LOOKUP semantics depend on Tableau's addressing-and-partitioning
    which is not in the formula text, so no amount of parsing recovers it.

    ONE table-calc shape is the exception: `RANK`/`RANK_DENSE` when extract.py's `tc_spec`
    resolved a concrete "Restart every: <field>" partition (`ordering_type == "Field"` with a
    real `level_break_field`) AND that field is a real column on THIS calc's own table --
    ALLEXCEPT needs a same-table column, so a level_break that names a field extraction could not
    place here (a different physical Tableau object a relationship now separates into a sibling
    table, say) must still decline rather than guess which table it meant.

    `RANK_UNIQUE`/`RANK_MODIFIED`/`RANK_PERCENTILE` deliberately stay on tier H even with a
    resolved partition: each has a real, DIFFERENT tie-break rule (unique-by-sort-order,
    modified-competition-ranking, percentile) with no exact, unapproximated DAX equivalent the way
    `RANK`("Skip" ties)/`RANK_DENSE`("Dense" ties) map onto RANKX's own native `ties` argument --
    approximating one as a plain RANKX would only silently diverge from Tableau exactly when two
    rows tie, which is the one case fail-closed exists to catch, not paper over.

    `table_columns`: the set of column captions defined on this calc's own table (pass None from
    any caller that does not have this, e.g. a caller only checking whether a calc counts as D+
    for stats -- that caller gets the OLD, conservative "always H" answer for this one narrow
    shape, never a wrong D+).
    """
    if calc["kind"] == "tablecalc":
        if table_columns is not None and _RANK_PARTITION_FUNC_RE.match(calc["formula"] or ""):
            spec = calc.get("tc_spec") or {}
            lb = spec.get("level_break_field")
            if spec.get("ordering_type") == "Field" and lb and lb in table_columns:
                return "D+"
        return "H"
    if calc["kind"] == "lod":
        # FIXED is deterministic -- ALLEXCEPT pins an aggregate to a stated grain, which is what
        # FIXED means. INCLUDE and EXCLUDE are relative to the view's own dimensions and stay with
        # the LLM tier. The parser still fails closed on anything it cannot resolve.
        if re.search(r"\{\s*FIXED", calc["formula"] or "", re.I):
            return "D+"
        return "L1"
    return "D+"


def _formula_shape(formula):
    """Coarse computation-shape fingerprint of a Tableau formula, for blend-key structural
    comparison (`TmdlModel._shared_columns`). Two columns sharing a name/dataType across two
    blended tables are Tableau's ONLY signal for a blend key (see `_relationships`'s docstring
    -- there is no stored linking-field), but a shared name says nothing about whether the two
    sides compute the value the SAME way. Real corpus regression (WorkbookA_
    v2): `Acquisition.'Quarter Year'` = `[quarter]+" "+STR([year_nm])` (e.g. "Q3 2025"),
    `Activation.'Quarter Year'` = bare `[qtr]` (e.g. "Apr25-Jun25") -- same name, same dataType
    (string), but the two strings never equal each other for any real row, so the many-to-many
    relationship built on them can never produce a matching row. Neither side's formula is
    wrong; they simply are not the same key.

    `None` (a plain physical column, not a calc -- ir.json carries no `formula` key for those)
    is treated as `("bare", 1)`: a physical column carries no synthetic computation at all,
    the same shape as a calc that is nothing but one bare field alias. Deliberately coarse --
    same rough class of computation (conditional / concatenation / other-expression / bare) and
    the same count of underlying field references, NOT full semantic equivalence. A false
    "different shape" on two formulas that happen to be equivalent is cheap (the pair is
    declined and routed to a human via COPILOT_FIX_PROMPTS, same as any other GAP); a false
    "same shape" is the bug this closes, so ties are broken toward declining.

    Returns `None` -- NOT comparable, fails closed -- when the formula does not cleanly lex
    (`dax.Unsupported`), matching this module's own fail-closed convention elsewhere (`route`,
    the D+ parser passes): never guess a shape for text this pipeline could not even tokenize.
    """
    if not formula:
        return ("bare", 1)
    try:
        toks = dax.lex(formula)
    except dax.Unsupported:
        return None
    if not toks:
        return None
    if len(toks) == 1 and toks[0].kind in ("field", "qualref"):
        return ("bare", 1)
    field_count = sum(1 for t in toks if t.kind in ("field", "qualref"))
    if any(t.kind in ("IF", "CASE") for t in toks):
        category = "conditional"
    elif any(t.kind == "op" and t.val in ("+", "&") for t in toks):
        category = "concat"
    else:
        category = "other_expr"
    return (category, field_count)


def _describe_formula(formula):
    """Short human-facing rendering of a formula for a residue reason string -- `formula=None`
    (plain physical column) reads as "a plain column", never as the Python literal `None`."""
    if not formula:
        return "a plain column"
    return f"formula {formula.strip()!r}" if len(formula) <= 60 else f"formula {formula[:57].strip()!r}..."


class TmdlModel:
    def __init__(self, ir, authority, model_name, partitions="stub"):
        self.ir = ir
        self.na = authority
        self.model_name = model_name
        self.partitions = partitions
        self.residue = ResidueLog()
        self.files = {}          # relative path -> text
        self.table_order = []
        self.stats = collections.Counter()
        self.notes = []
        # Every table pair actually emitted as a `relationship` object in relationships.tmdl, as
        # (a, b) edges -- undirected for reachability purposes (Power BI cross-filtering has a
        # real direction, but "is there ANY path Desktop can resolve at all", the question an
        # InvalidUnconstrainedJoin failure is asking, is not direction-sensitive). Read by
        # pbir.py's `_visual_filters` (a filter's own reachability) and by its general
        # query-well reachability check (a whole visual's OWN queried tables) to decide whether a
        # table other than a visual's own has ANY relationship path to ride on. Populated by
        # `_relationships`' blend-key detection below -- see that method's docstring for what
        # does and does not qualify for a real relationship.
        self.relationship_pairs = []

    def _aggregate_map(self, t):
        """{field identity: is_aggregate}, propagated to a FIXPOINT.

        `dax.is_aggregate` only inspects a formula's OWN text for an aggregate function call. But
        Tableau propagates aggregation status through references: if calc A is
        `IF [B] < 0 THEN "v" END` and calc B is `SUM([x]) / SUM([y])`, then A is *also* an
        aggregate calculation in Tableau, even though A's own text has no SUM in it.

        Missing that produced exactly the failure Desktop reported: A emitted as a calculated
        COLUMN referencing B, which was emitted as a MEASURE. A calculated column cannot reference
        a measure -- the measure's implicit whole-table aggregation depends on the very column
        being computed, which the engine reports as "a circular dependency" between the columns
        that share the reference, not as the real error (a column-referencing-a-measure). One pass
        is not enough for a chain A -> B -> C, so this iterates like `resolve_calcs` does for
        declines.

        A parameter reference is the SAME hazard by a different route. `resolve_param` (see
        `_resolver`) always points at the parameter's SELECTEDVALUE MEASURE, never its raw column
        -- so `[Parameters].[X]` in a calc's formula makes that calc a measure-referencer exactly
        like `SUM(...)` does, even though the formula has no aggregate function and Tableau itself
        would call the calc a plain dimension. Missing this is what let a real workbook (WorkbookB
        Dashboard) emit `cl.Is Selected Slice By?` as a calculated COLUMN whose DAX referenced a
        parameter's measure: the model loaded (no preflight check saw it, because the existing
        column-references-measure check only matches the QUALIFIED 'Table'[Column] shape, and a
        parameter reference is the unqualified [Name] shape `resolve_param` deliberately uses), but
        every report filter on that column failed in Desktop with "Broken_Filters" -- confirmed by
        elimination against every other filter in the same report, all of which target plain
        source columns and all of which loaded fine.

        Groups and bins share this hazard exactly the same way a calc-referencing-a-calc does: a
        group's `SWITCH(TRUE(), ..., src)` or a bin's `FLOOR(src / size) * size` references its
        `source` field, and if that source is (transitively) a parameter-dependent calc -- now
        promoted to a measure by this same fixpoint -- the group/bin would emit the identical
        forbidden shape one level up, UNLESS it is promoted too. Unlike a calc, a group/bin has no
        formula of its own to seed from (its only reference is the single `source` field), so it
        is seeded False and can only inherit aggregate status by propagation.

        CROSS-table references get the identical hazard through a different door:
        `split_federated_datasource` homes a calc on whichever ONE physical object its own column
        references belong to, but an "unconstrained" calc (no physical column ref at all -- e.g.
        one that only reads a Parameter) has no object to home on and defaults to the PRIMARY
        object's table. A calc on a DIFFERENT (non-primary) table can reference that same
        unconstrained calc without ever being flagged `cross_object_note` -- the note only fires
        when a calc's OWN references resolve to more than one *concrete* object, and the
        unconstrained one contributes no concrete object of its own to that check. So the
        referencing calc lands on its own table, the referenced one lands on the primary table,
        and if the referenced one gets promoted to a measure by THIS fixpoint (e.g. because its
        formula reads `[Parameters].[...]`), the referencing calc must be promoted too or it
        emits a calculated COLUMN referencing a MEASURE on a *sibling* table -- Desktop rejects
        this at load. Confirmed on a real workbook (WorkbookB Dashboard): `cl.Rolling 7 Day Start -
        Deflection Settled` (table `Insights`) references `cl.Current Period - End Date` (table
        `Currencies`, promoted for exactly the parameter reason above); the same-table-only
        version of this fixpoint could not see the cross-table dependency and 25 calcs reached
        preflight as invalid DAX. Fixed by falling back to `_global_field_index()` -- first-match
        across every OTHER table, the same tolerated-collision shape `_resolver`'s own global
        index already accepts -- for any reference this table's own `refs_of` does not know,
        recursing into that other table's OWN `_aggregate_map` (cached exactly like this one, with
        a `_agg_computing` guard against a genuine cross-table reference cycle, which is treated
        as "not aggregate" rather than looped forever -- a real one would be a Tableau-level
        circular reference this pipeline declines elsewhere, not a case this fixpoint owns).
        """
        cached = getattr(self, "_agg_cache", {})
        key = id(t)
        if key in cached:
            return cached[key]
        computing = getattr(self, "_agg_computing", None)
        if computing is None:
            computing = set()
            self._agg_computing = computing
        if key in computing:
            return {}
        computing.add(key)
        FIELDREF = re.compile(r"\[([^\]]+)\]")
        agg = {fkey(c): (dax.is_aggregate(c["formula"]) or "[Parameters]." in (c["formula"] or ""))
               for c in t["calcs"]}
        agg.update({fkey(g): False for g in t.get("groups", [])})
        agg.update({fkey(b): False for b in t.get("bins", [])})
        refs_of = {fkey(c): FIELDREF.findall(c["formula"] or "") for c in t["calcs"]}
        refs_of.update({fkey(g): [g["source"]] for g in t.get("groups", [])})
        refs_of.update({fkey(b): [b["source"]] for b in t.get("bins", [])})
        other_idx = self._global_field_index()
        for _ in range(len(refs_of) + 1):
            grew = False
            for ident, ident_refs in refs_of.items():
                if agg[ident]:
                    continue
                for ref in ident_refs:
                    if agg.get(ref):
                        agg[ident] = True
                        grew = True
                        break
                    if ref not in agg:
                        other_t = other_idx.get(ref)
                        if other_t is not None and other_t is not t \
                                and self._aggregate_map(other_t).get(ref):
                            agg[ident] = True
                            grew = True
                            break
            if not grew:
                break
        computing.discard(key)
        cached[key] = agg
        self._agg_cache = cached
        return agg

    def _shelf_agg_only(self, t):
        """{calc identity (`fkey`): DAX iterator function name} for every calc on table `t`
        used EXCLUSIVELY, across every worksheet encoding referencing it by caption, via ONE
        consistent REAL Tableau shelf aggregation (`AGG_DAX`'s own keys -- sum/avg/min/max/
        median/stdev/var/count) -- never as a bare dimension (`agg` is `None`), never with a
        DIFFERENT aggregation on another worksheet, never as a Tableau quick-table-calc pill
        (a non-`AGG_DAX` agg code, e.g. 'pcto'/'rank'/'cum' -- a different translation question
        `pbir.py`'s `_QUICK_CALC_SHELF_CODES` already owns).

        Used by `resolve_calcs` to decide, for a calc promoted to a MEASURE ONLY by a parameter
        reference (`becomes_measure`, no aggregate function in its OWN formula text), whether to
        prefer SUMX-style inlining (wrap the WHOLE translated formula in the matching X-iterator,
        with every bare column reference left bare -- safe, because the iterator itself supplies
        the row context) over `resolve_measure_safe`'s SELECTEDVALUE-wrap. SELECTEDVALUE is
        correct for a genuinely dimension-like reference (`resolve_measure_safe`'s own original
        "cl.Slice By" bug: a branch-selection formula picking between whole COLUMNS by name) -- it
        is WRONG for a value Tableau's own shelf usage already wraps in SUM/AVERAGE, because a
        bare row-level column has many distinct real values across the table, and SELECTEDVALUE
        returns BLANK whenever more than one exists. Real corpus (WorkbookA): `Penetration` --
        formula `(IF [Parameters].[Opcert or User Input]="Opcert" THEN [issr_cif_cy] ... END) /
        [Parameters].[Retail Customers]`, promoted to a measure purely by the parameter division,
        shelf-used as `SUM(Penetration)` on its own worksheet -- emitted as
        `DIVIDE(SELECTEDVALUE('Acquisition'[issr_cif_cy]), ...)`, BLANK the instant more than one
        row exists (confirmed live against real Desktop: every real row), where Tableau's own
        intent (the shelf's SUM) is `SUMX('Acquisition', DIVIDE([issr_cif_cy], ...))` -- a real,
        non-blank number once correctly inlined. The Power BI card bound to this measure showed
        "(Blank)" against a real Tableau value of 7.4%.

        `ctd`/`countd` (DISTINCTCOUNT) excluded on purpose: DISTINCTCOUNT has no X-iterator form
        at all (`dax.py`'s own `NO_ITERATOR` set) -- it needs the SELECTCOLUMNS/DISTINCT/
        COUNTROWS restructure `dax.py`'s `func_call` already builds for an EXPLICIT
        `COUNTD(<expr>)` call in a calc's own formula text, not a plain wrap this method could
        emit safely. A calc shelf-used only via `ctd`/`countd` falls through to the existing
        `resolve_measure_safe` behavior unchanged -- no regression, just not fixed here.

        Caption-matched, not table-verified: a worksheet encoding's `field` is compared by
        STRING to this table's own calc captions, without re-resolving which table that
        encoding's field really belongs to (the same tolerated-collision risk `_resolver`'s own
        global fallback already accepts elsewhere in this file). Worst case on a caption
        collision is a SUBOPTIMAL choice between two already-valid DAX shapes, never invalid DAX
        -- this method only ever picks which safe translation to use, it never invents a new one.
        """
        cached = getattr(self, "_shelf_agg_cache", {})
        key = id(t)
        if key in cached:
            return cached[key]
        name_to_fkey = {c["name"]: fkey(c) for c in t["calcs"]}
        uses = {}
        dimension_use = set()
        for ws in self.ir.get("worksheets", []):
            for e in ws.get("encodings", []):
                field = e.get("field")
                if field not in name_to_fkey:
                    continue
                agg = e.get("agg")
                fn = AGG_DAX.get(agg) if agg else None
                if fn is None or fn == "DISTINCTCOUNT":
                    dimension_use.add(field)
                    continue
                uses.setdefault(field, set()).add(fn)
        result = {}
        for name, fns in uses.items():
            if name in dimension_use or len(fns) != 1:
                continue
            fn = next(iter(fns))
            if fn in dax.AGG_ITERATOR:
                result[name_to_fkey[name]] = dax.AGG_ITERATOR[fn]
        cached[key] = result
        self._shelf_agg_cache = cached
        return result

    def _global_field_index(self):
        """(field identity) -> owning raw IR table, first-match across the WHOLE workbook.

        Calcs/groups/bins only -- a plain source column can never be promoted to a measure, so it
        is not a hazard this index needs to answer for. Built once and cached; `_aggregate_map`
        consults it only for a reference its OWN table's `refs_of` does not already resolve, which
        keeps the existing same-table-first behavior exactly as it was.
        """
        idx = getattr(self, "_global_field_idx", None)
        if idx is not None:
            return idx
        idx = {}
        for t in self.ir["tables"]:
            for c in t["calcs"] + t.get("groups", []) + t.get("bins", []):
                idx.setdefault(fkey(c), t)
        self._global_field_idx = idx
        return idx

    def _by_tableau_name(self):
        """Tableau table caption -> allocated PBI table id, first-match. Used to resolve a
        `cross_table_refs` entry's target table (a plain caption, recorded by
        `split_federated_datasource`) back to the concrete PBI table `_resolver` needs."""
        cached = getattr(self, "_by_tableau_name_cache", None)
        if cached is not None:
            return cached
        m = {t["tableau_name"]: pbi for pbi, t in self.table_order}
        self._by_tableau_name_cache = m
        return m

    # ---------------------------------------------------------------- allocation
    def allocate(self):
        """S2: allocate EVERY name before emitting anything.

        Order matters. Columns are allocated before measures because a column and a measure cannot
        share a name within a table, and a column is the object a report can also group by --
        losing a measure name to a suffix is recoverable, losing a column's is not.
        """
        for t in self.ir["tables"]:
            key = t["tableau_name"] or t["caption"]
            pbi = self.na.table(key, t["caption"])
            self.table_order.append((pbi, t))
            for c in t["columns"] + t["calcs"] + t["groups"] + t["bins"]:
                self.na.alias(pbi, fkey(c), c["name"], fkey(c))
            for c in t["columns"]:
                self.na.column(pbi, fkey(c), c["name"])
            # Groups/bins allocate AFTER agg_map, same as calcs -- a group/bin whose source is
            # (transitively) parameter-dependent must become a measure too, or it would emit the
            # same forbidden column-references-measure shape one level up. See `_aggregate_map`.
            agg_map = self._aggregate_map(t)
            for g in t["groups"]:
                if agg_map.get(fkey(g)):
                    self.na.measure(pbi, fkey(g), g["name"])
                else:
                    self.na.column(pbi, fkey(g), g["name"])
            for b in t["bins"]:
                if agg_map.get(fkey(b)):
                    self.na.measure(pbi, fkey(b), b["name"])
                else:
                    self.na.column(pbi, fkey(b), b["name"])
            table_columns = {col["name"] for col in t["columns"]}
            for c in t["calcs"]:
                if route(c, table_columns) != "D+":
                    continue
                if agg_map[fkey(c)]:
                    self.na.measure(pbi, fkey(c), c["name"])
                else:
                    self.na.column(pbi, fkey(c), c["name"])
        self._assert_no_table_collision()
        for p in self.ir["parameters"]:
            self.na.parameter(p["name"])
        self._plan_shelf_measures()

    def _assert_no_table_collision(self):
        """Backstop for a silent allocation collision between two DIFFERENT source tables.

        `self.table_order` gets exactly one entry per `self.ir["tables"]` entry (the loop above
        appends unconditionally), so its length is always the source table count. If two of those
        entries resolve to the SAME pbi name, `NameAuthority.table()` treated the second
        allocation request as a re-lookup of an object it had already named -- correct when the
        same source object is legitimately referenced twice, silently wrong when it is actually
        two DIFFERENT source tables whose `(tableau_name or caption)` key happened to collide (the
        VRM Compliance Dashboard bug: two distinct tds-resolved tables, two different .hyper
        extracts, both had tableau_name=None and caption="Data"). Emission then writes both under
        one file path and the second write clobbers the first -- an entire table disappears with
        no residue entry. Caught HERE, pre-emit, before any TMDL reaches disk.
        """
        counts = collections.Counter(pbi for pbi, _t in self.table_order)
        dupes = {pbi: n for pbi, n in counts.items() if n > 1}
        if not dupes:
            return
        detail = "; ".join(
            f"{pbi!r} claimed by {n} source tables (captions: "
            f"{[t['caption'] for p, t in self.table_order if p == pbi]})"
            for pbi, n in dupes.items())
        raise AssertionError(
            f"table allocation collision: {len(self.table_order)} source table(s) produced only "
            f"{len(counts)} distinct pbi table name(s) -- {detail}. Two different Tableau tables "
            f"were about to be silently merged/overwritten into one TMDL file.")

    def _plan_shelf_measures(self):
        """Plan shelf aggregates ONCE for the model, not once per table.

        The per-table version asked "does THIS table have a column of that name?" and emitted a
        measure wherever the answer was yes. With several near-duplicate datasources that produced
        the same aggregate in eight tables -- 63 duplicate measure names across the corpus, and
        eight copies of something that should exist once. A shelf reference identifies ONE field, so
        it resolves to one table and the measure belongs there.
        """
        self.shelf_plan = collections.defaultdict(list)
        wanted = set()
        for ws in self.ir["worksheets"]:
            for e in ws["encodings"]:
                if e.get("agg"):
                    wanted.add((e["field"], e["agg"].lower()))
            # A Measure Names/Values pivot ("1W - Table") carries its own aggregated field list
            # OUTSIDE `encodings` (see extract.py's `_measure_names_pivot`) -- missing this left
            # every pivoted field with an agg (e.g. "sum:Order Detail Matches") silently bound to
            # its bare, unaggregated column instead of a real SUM measure: a Power BI table with
            # no group-by dimension and a mix of one real measure + several raw per-row columns
            # can't collapse to one summary row, so it fans out to one row per underlying record
            # instead. Same `wanted` set, same promotion path -- not a second mechanism.
            for p in ws.get("measure_names_pivot") or []:
                if p.get("agg"):
                    wanted.add((p["field"], p["agg"].lower()))

        # Shelf field -> Tableau datatype, same local-then-global lookup `_resolver()` builds for
        # calc type resolution (see its `local_types`/`glob_types` below) -- a field dragged
        # straight onto a shelf with an aggregation is just as exposed to a Tableau-string column
        # as any user-written calc is, and this path had NO type awareness at all: it always
        # emitted a bare `SUM(col)`/`AVERAGE(col)` regardless of the column's own TOM dataType.
        # dax.py's Parser already declines/casts this exact shape for an explicit calc formula
        # (see its SUM/AVERAGE-of-string-field branch) -- real corpus regression there was this
        # SAME column, `issr_total_actv_crd_cy` (string in Activation, double in Key Highlights):
        # WorkbookA's own `Activation Rate_issr` hit "AVERAGEX cannot work with values of type String"
        # before that fix. This implicit-shelf-measure path was never covered by it: a WorkbookA
        # realistic-data run emitted `SUM('Activation'[issr_total_actv_crd_cy])` straight to TMDL,
        # which loads cleanly (no load-time error) but hard-errors every card/visual that uses it
        # at QUERY time ("The function SUM cannot work with values of type String") -- confirmed
        # live against Desktop via pbi-cli, not just inferred from the TMDL text.
        local_types, glob_types = {}, {}
        for t in self.ir["tables"]:
            key = self.na.tables.get(t["tableau_name"] or t["caption"])
            if key is None:
                continue
            for c in t["columns"]:
                for spelling in (c["name"], fkey(c)):
                    if spelling:
                        local_types.setdefault((key, spelling), c.get("datatype"))
                        glob_types.setdefault(spelling, c.get("datatype"))

        # FIXED LOD calcs translate to CALCULATE(<agg>, ALLEXCEPT(T, T[key])) in DAX, which
        # means every row within a group already carries the same group-aggregate value --
        # materialized once per grain, stored on every row that shares that grain.  A flat SUM()
        # or AVERAGE() shelf measure over such a column over-counts by (rows-per-group): the
        # group total is repeated N times and summed N times instead of once.  MAX() is the
        # correct substitute: since all rows in a group carry the identical value, MAX equals
        # that one group value.  This index identifies those columns so the loop below can apply
        # the substitution.  Scope is narrow: only SUM/AVERAGE on confirmed FIXED LOD columns;
        # COUNT/DISTINCTCOUNT/MIN/MAX/MEDIAN are left as-is (either safe or out of scope).
        local_lod_fixed = set()   # (tbl_key, field_spelling) pairs for FIXED LOD calc columns
        for t in self.ir["tables"]:
            key = self.na.tables.get(t["tableau_name"] or t["caption"])
            if key is None:
                continue
            for c in t["calcs"]:
                if c.get("kind") == "lod" and re.search(
                        r"\{\s*FIXED", c.get("formula") or "", re.I):
                    for spelling in (c["name"], fkey(c)):
                        if spelling:
                            local_lod_fixed.add((key, spelling))

        for field, agg in sorted(wanted):
            fn = AGG_DAX.get(agg)
            if fn is None:
                continue
            # Allocate a shelf measure for EVERY table that carries this field caption as a
            # plain column -- not just the first match. When two tables share the same column
            # caption (e.g. `Issr Actv Crd Cy` in both `Key Highlights` and `Usage Overview`),
            # the old single-match path allocated the shelf measure only under the first table;
            # the second table's worksheet then found no promoted measure for its own column
            # and fell back to routing the field to the Category well instead of the Y axis.
            for tbl, col, kind in self.na.all_by_spelling(field):
                if kind != "column":
                    continue
                ref = dax_ref(tbl, col)
                dtype = local_types.get((tbl, field), glob_types.get(field))
                is_fixed_lod = (tbl, field) in local_lod_fixed or (tbl, col) in local_lod_fixed
                if is_fixed_lod and fn in ("SUM", "AVERAGE"):
                    # Per-group CALCULATE/ALLEXCEPT column: SUM/AVERAGE multiplies the group
                    # value by (rows per group); MAX gives the correct group value since every
                    # row in the group carries the identical pre-computed aggregate.  Only SUM
                    # and AVERAGE are affected -- other aggregations are left for the path below.
                    expr = f"MAX({ref})"
                elif dtype == "string":
                    if fn in ("SUM", "AVERAGE"):
                        # Same cast dax.py uses for the identical shape in a calc formula: an
                        # X-iterator over IFERROR(VALUE(...), BLANK()) -- BLANK is ignored by
                        # DAX's own iterator aggregation, matching Tableau's NULL-tolerant coerce.
                        expr = f"{dax.AGG_ITERATOR[fn]}('{tbl}', IFERROR(VALUE({ref}), BLANK()))"
                    elif fn in ("COUNT", "DISTINCTCOUNT"):
                        # Legal DAX on text as-is -- no numeric cast needed.
                        expr = f"{fn}({ref})"
                    else:
                        # MIN/MAX/MEDIAN/STDEV.S/VAR.S on a string field: same decline as
                        # dax.py's identical shape -- skipped rather than guessed.
                        continue
                else:
                    expr = f"{fn}({ref})"
                label = f"{agg.upper()} of {col}"
                allocated = self.na.measure(tbl, f"__shelf__{agg}__{field}", label)
                self.shelf_plan[tbl].append((allocated, expr, field))

    def lookup_by_spelling(self, pbi, spelling):
        """(pbi name, kind) for a field named by caption OR internal name, else (None, None).

        Delegates to the Name Authority: the spelling map belongs to the one object both builders
        read, not to a private copy in each.
        """
        _t, name, kind = self.na.by_spelling(spelling, table=pbi)
        return name, kind

    # ---------------------------------------------------------------- resolution
    def _resolver(self, pbi_table):
        """Resolve a Tableau field reference to a DAX reference.

        Tableau formulas reference fields by INTERNAL name (`[year_ind]`,
        `[Calculation_1750...]`), not by caption, while the Name Authority is keyed by the caption
        that becomes the Power BI name. Both spellings must resolve or nearly every formula that
        references a calculated field fails -- measured: resolving by caption alone translated 0%
        of real corpus formulas.
        """
        # TWO indexes, and the order they are consulted is a correctness matter, not a preference.
        #
        # A workbook routinely holds several near-duplicate datasources -- this corpus has four
        # that each define `[Calculation_475692734733418497]` captioned `month`. A single global
        # first-match index binds a calc in the FOURTH datasource to the FIRST one's column. That
        # is not a missing translation, it is a wrong one: valid DAX reading the wrong table's
        # data. So the owning table wins, and the global index is only a fallback for a shelf
        # reference that genuinely omits its datasource.
        local, glob = {}, {}
        for t in self.ir["tables"]:
            key = self.na.tables.get(t["tableau_name"] or t["caption"])
            if key is None:
                continue
            for c in t["columns"] + t["calcs"] + t["groups"] + t["bins"]:
                ident = fkey(c)
                for spelling in (c["name"], ident):
                    if spelling:
                        local.setdefault((key, spelling), (key, ident))
                        glob.setdefault(spelling, (key, ident))

        def _resolve_with_kind(field):
            hit = local.get((pbi_table, field)) or glob.get(field)
            if hit is None:
                return None, None
            tbl, tab_field = hit
            if (tbl, tab_field) in self.declined:
                # Known to the source but NOT going into the model -- its own formula was
                # declined. Returning None makes this formula decline too, rather than emitting a
                # reference to an object that will not exist. This is what makes `declined`
                # cascade, and why resolution runs to a fixpoint.
                return None, None
            name, kind = self.na.lookup(tbl, tab_field)
            return (None, None) if name is None else (dax_ref(tbl, name), kind)

        def resolve(field):
            ref, _kind = _resolve_with_kind(field)
            return ref

        def resolve_kind(field):
            """(tableau_field_name) -> "column" | "measure" | None. Feeds dax.py's Parser so its
            aggregate-over-a-bare-reference check (SUM/AVERAGE/.../DISTINCTCOUNT of a bare
            'Table'[Name]) can tell a promoted MEASURE apart from an ordinary column -- `resolve`
            above renders both identically, so the parser has no other way to know. Missing this
            is exactly what let `SUM('caribbean'[Gross Trx Amt Declined])` -- a reference to a
            calc that `_aggregate_map`'s fixpoint promoted to a measure -- reach TMDL as invalid
            DAX, which Desktop rejected at load with Missing_References.
            """
            _ref, kind = _resolve_with_kind(field)
            return kind

        def resolve_measure_safe(field):
            """Same as `resolve`, but a plain COLUMN reference is wrapped in `SELECTEDVALUE`.

            Only for formulas that will be emitted as a MEASURE with no aggregate function of
            their own (see the call site in `resolve_calcs`/`_group_body`/`_bin_body`) -- a bare
            `'Table'[Column]` at measure top level has no row context to reduce it to one value,
            and Desktop rejects it at evaluation time: "A single value for column '...' cannot be
            determined... This can happen when a measure or function formula refers to a column
            that contains many values without specifying an aggregation." Confirmed on a real
            workbook (WorkbookB Dashboard, `cl.Slice By`): promoting it to a measure (to fix the
            EARLIER column-references-a-measure bug) was necessary but not sufficient -- its
            formula is a pure branch-selection over plain columns with no aggregate function
            anywhere, exactly the shape this wraps. A MEASURE reference is already a scalar and is
            returned bare, same as `resolve` -- wrapping it in SELECTEDVALUE would be a no-op at
            best and a second measure-referencing-measure oddity at worst.
            """
            ref, kind = _resolve_with_kind(field)
            if ref is not None and kind == "column":
                return f"SELECTEDVALUE({ref})"
            return ref

        # Parameters have the SAME caption-vs-internal-name split as fields, and it bites harder:
        # a formula writes `[Parameters].[Parameter 1]` -- the internal name -- while the Name
        # Authority is keyed by the caption a user sees. Indexing by caption alone left 18 of
        # Caribbean's calcs declined as "unresolved parameter reference" for parameters that were
        # sitting in the model the whole time.
        pindex = {}
        for p in self.ir["parameters"]:
            rec = self.na.params.get(p["name"])
            if rec is None:
                continue
            spellings = [p["name"], (p.get("tableau_name") or "").strip("[]")]
            # extract.py's own aliases: an orphaned internal ID that unanimously points at this
            # same parameter's caption wherever it's declared (see extract_parameters()) -- not a
            # second guess layered on top of this one, a second SPELLING of it.
            spellings += [a.strip("[]") for a in (p.get("aliases") or [])]
            for spelling in spellings:
                if spelling:
                    pindex.setdefault(spelling, rec)
                    pindex.setdefault(spelling.lower(), rec)

        # datatype per spelling, for STR()'s date-vs-text-coercion check and dax.py's string-typed-
        # aggregate proof (SUM/AVERAGE of a Tableau-string field, `+` concat-vs-add). Same LOCAL-
        # then-GLOBAL lookup as `local`/`glob` above, for the identical reason: a field name is not
        # unique across tables (e.g. `issr_total_actv_crd_cy` is `string` in `Activation` but
        # `double` in `Key Highlights`), so a single flat dict keyed by spelling alone lets whichever
        # table happens to iterate FIRST win globally -- Activation's own calc got Key Highlights'
        # dtype, silently defeating the string-cast proof and letting an uncast AVERAGE(<string
        # column>) reach TMDL. Real corpus regression: WorkbookA_v2's
        # Activation.'Activation Rate_issr', Desktop error "The function AVERAGEX cannot work with
        # values of type String".
        local_types, glob_types = {}, {}
        for t in self.ir["tables"]:
            key = self.na.tables.get(t["tableau_name"] or t["caption"])
            if key is None:
                continue
            for c in t["columns"] + t["calcs"]:
                for spelling in (c["name"], fkey(c)):
                    if spelling:
                        local_types.setdefault((key, spelling), c.get("datatype"))
                        glob_types.setdefault(spelling, c.get("datatype"))

        def resolve_type(field):
            hit = local_types.get((pbi_table, field))
            return hit if hit is not None else glob_types.get(field)

        # A calc's own raw Tableau formula per spelling, for `dax.py`'s promoted-aggregate
        # collapse (see its `_collapse_promoted_aggregate`) -- recovering `SUM([x])` where `x`
        # resolved to a measure needs `x`'s OWN source formula, not just its resolved DAX
        # reference string. Declined identically to `_resolve_with_kind` (a field whose own
        # formula was itself declined -- tier-routed away before ever reaching `dax.translate`,
        # or failed there on an earlier fixpoint pass -- must not be silently inlined as if it
        # were a working formula; the recursive `dax.translate` call would not know to reproduce
        # a structural decline this resolver already knows about).
        formulas = {}
        for t in self.ir["tables"]:
            key = self.na.tables.get(t["tableau_name"] or t["caption"])
            if key is None:
                continue
            for c in t["calcs"]:
                for spelling in (c["name"], fkey(c)):
                    if spelling:
                        formulas.setdefault(spelling, (key, fkey(c), c.get("formula")))

        def resolve_formula(field):
            hit = formulas.get(field)
            if hit is None:
                return None
            tbl, tab_field, formula = hit
            if (tbl, tab_field) in self.declined:
                return None
            return formula

        def resolve_param(pname):
            key = str(pname).strip("[]")
            rec = pindex.get(key) or pindex.get(key.lower())
            if rec is None:
                return None
            # The SELECTEDVALUE measure, NOT the raw `[Value]` column -- names.py's own
            # documented contract ("every [Parameters].[X] reference... rewrites to that
            # measure") and dax.py's own comment on this exact branch both say so. Returning
            # the column instead put a bare `'Param X'[Value] = "..."` comparison at measure
            # top-level in 145 emitted measures/columns across the corpus, which Desktop
            # rejects at evaluation time with "A single value for column 'Value'... cannot be
            # determined" -- DAX cannot implicitly reduce a multi-row column to a scalar
            # outside a row context. Measures are unique model-wide (also names.py's
            # contract), so an unqualified `[Name]` always resolves.
            return f"[{rec['measure']}]"

        return resolve, resolve_param, resolve_type, resolve_measure_safe, resolve_kind, \
            resolve_formula

    _UNRESOLVED = re.compile(r"unresolved field reference \[(?:[^\]]*\]\.\[)?([^\]]+)\]")

    def _explain(self, pbi, message):
        """Separate a CASCADE from a genuinely unknown field.

        Both surface from the parser as "unresolved field reference", and they need opposite
        responses: a cascade is fixed by translating the ONE calc upstream of it, while an unknown
        field means extraction missed a field the formula depends on. Reporting them with the same
        wording makes a 10-dependent cascade look like ten separate problems.
        """
        m = self._UNRESOLVED.search(message)
        if not m:
            return message
        field = m.group(1)
        # Search declines across EVERY table, not just this one. Scoping the search to the current
        # table reported a cross-table cascade as "no field by that name was extracted" -- which
        # sends a reader looking for an extraction bug when the fix is to translate one calc in a
        # different table.
        for (tbl, cap), (tier, _r) in self.declined.items():
            if cap == field or field in self._alias_of(tbl, cap):
                where = "" if tbl == pbi else f" in table {tbl!r}"
                return (f"depends on {cap!r}{where}, which was itself declined (tier {tier}) -- "
                        f"translating that one calc would also recover this")
        return message + " -- no field by that name is defined anywhere in the workbook"

    def _alias_of(self, pbi, caption):
        for _p, t in self.table_order:
            if _p != pbi:
                continue
            for c in t["calcs"] + t["columns"]:
                if fkey(c) == caption:
                    return {c["name"]}
        return set()

    @staticmethod
    def _find_cycles(t):
        """{identity in a genuine reference cycle: the cycle path, for the residue message}.

        Distinct from `_aggregate_map`'s fixpoint, which propagates a ONE-DIRECTIONAL property
        (does this calc aggregate) and terminates safely either way. A cycle is different: A's
        formula depends on B's value and B's depends on A's. There is no order to evaluate them
        in, in Tableau or in DAX -- it is not a translation gap, it is an undefined calculation in
        the source. Detected with an iterative Tarjan SCC (iterative, not recursive: a table can
        hold hundreds of calcs, and recursing one stack frame per calc risks the interpreter's
        recursion limit on a long reference chain that ISN'T even a cycle).
        """
        FIELDREF = re.compile(r"\[([^\]]+)\]")
        idents = {fkey(c) for c in t["calcs"]}
        formula_of = {fkey(c): (c["formula"] or "") for c in t["calcs"]}
        # Self-references ARE kept in the graph (not filtered out): Tableau itself refuses to
        # save a calc that references itself, so a genuine `D = D + 1` should not exist in a real
        # .twb -- but nothing downstream should assume that. A size-1 SCC with a self-edge is a
        # cycle exactly as much as a size-2 one is.
        graph = {a: [r for r in FIELDREF.findall(formula_of[a]) if r in idents]
                 for a in idents}

        index = {}
        low = {}
        on_stack = {}
        stack = []
        counter = [0]
        out = {}

        def strongconnect(root):
            work = [(root, iter(graph[root]))]
            index[root] = low[root] = counter[0]
            counter[0] += 1
            stack.append(root)
            on_stack[root] = True
            while work:
                v, it = work[-1]
                advanced = False
                for w in it:
                    if w not in index:
                        index[w] = low[w] = counter[0]
                        counter[0] += 1
                        stack.append(w)
                        on_stack[w] = True
                        work.append((w, iter(graph[w])))
                        advanced = True
                        break
                    elif on_stack.get(w):
                        low[v] = min(low[v], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    scc = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        scc.append(w)
                        if w == v:
                            break
                    if len(scc) > 1 or (len(scc) == 1 and scc[0] in graph[scc[0]]):
                        for n in scc:
                            out[n] = " -> ".join(scc)

        for n in idents:
            if n not in index:
                strongconnect(n)
        return out

    # ---------------------------------------------------------------- resolution to a fixpoint
    def resolve_calcs(self):
        """Translate every D+ calc, iterating until the declined set stops growing.

        One pass is not enough. Translation and dependency are circular: whether calc A can be
        translated depends on whether calc B (which A references) survived, and B is translated
        later in the same pass. A single pass therefore emits A with a reference to a B that was
        subsequently declined -- a dangling DAX reference that fails at model load.

        Each iteration only ever ADDS to `declined`, and the set is bounded by the number of
        calcs, so this terminates. The iteration count is recorded: more than 2 means the corpus
        really does chain declined calcs, which is worth knowing.
        """
        self.declined = {}       # (pbi table, tableau field caption) -> (tier, reason)
        self.calc_dax = {}       # (pbi table, tableau field caption) -> dax

        for pbi, t in self.table_order:
            cycles = self._find_cycles(t)
            for c in t["calcs"]:
                ident = fkey(c)
                if ident in cycles:
                    self.declined[(pbi, ident)] = (
                        "GAP",
                        f"circular reference among Tableau calculated fields ({cycles[ident]}) -- "
                        f"there is no order to evaluate a cycle in, in Tableau or in DAX; this is "
                        f"an undefined calculation in the source, not a translation gap")
                    continue
                if c.get("cross_object_note"):
                    # split_federated_datasource already proved this calc spans physical objects
                    # a real relationship now separates into different tables. Decline it HERE,
                    # before the resolver below ever sees it -- letting it through to the normal
                    # resolve() walk risks it falling through to `na.find_anywhere()` and finding
                    # the SAME-NAMED column on a sibling table, emitting a same-table-looking DAX
                    # reference that is silently wrong once the tables are no longer one (exactly
                    # the class of bug the rest of this pipeline exists to make impossible).
                    self.declined[(pbi, fkey(c))] = ("L2", c["cross_object_note"])
                    continue
                if c.get("cross_table_refs"):
                    # split_federated_datasource already proved a RELATED()-legal hub exists for
                    # every cross-table reference this calc makes (this table IS that hub) -- but
                    # it could not check, at extraction time, whether the referenced field is
                    # itself a plain column or something that resolved to a MEASURE (a bare
                    # measure resolved by RELATED() has no meaning; RELATED() only reduces a
                    # column). Validate that here, once, before the translation fixpoint below
                    # ever tries to use it -- same reasoning as the cross_object_note check above:
                    # fail closed with a reason rather than let a bad wrap reach dax.translate.
                    by_tn = self._by_tableau_name()
                    related = {}
                    bad_ref = None
                    for ref, other_caption in c["cross_table_refs"].items():
                        other_pbi = by_tn.get(other_caption)
                        if other_pbi is None:
                            bad_ref = (ref, other_caption, "not found in the model")
                            break
                        other_resolve, _op, _ot, _oms, other_kind, _of = self._resolver(other_pbi)
                        other_ref = other_resolve(ref)
                        kind = other_kind(ref)
                        if other_ref is None:
                            bad_ref = (ref, other_caption, "could not be resolved on that table")
                            break
                        if kind != "column":
                            bad_ref = (ref, other_caption,
                                       "resolved to a measure, not a plain column -- a "
                                       "cross-table reference to a measure is a different hazard "
                                       "(see _aggregate_map) this path does not cover")
                            break
                        # Bare 'OtherTable'[Column] -- wrapped RELATED() or SELECTEDVALUE() at the
                        # actual use site below, depending on whether THIS calc ends up emitted as
                        # a calculated column (row context -> RELATED() is legal and correct) or a
                        # measure (no row context -> RELATED() would raise at evaluation; the
                        # relationship's own filter propagation makes SELECTEDVALUE() correct
                        # instead, the exact idiom `resolve_measure_safe` already uses for a
                        # same-table bare column at measure top level).
                        related[ref] = other_ref
                    if bad_ref is not None:
                        ref, other_caption, why = bad_ref
                        self.declined[(pbi, fkey(c))] = (
                            "L2",
                            f"calculated field references '{ref}' on physical Tableau object "
                            f"'{other_caption}' through a verified many-to-one relationship, but "
                            f"that reference {why} -- hand-author this one with the relationship "
                            f"in mind")
                        continue
                    c["_related_refs"] = related
                table_columns = {col["name"] for col in t["columns"]}
                tier = route(c, table_columns)
                if tier == "D+" and c["kind"] == "tablecalc":
                    # route() only returns D+ for a tablecalc when it just proved level_break_field
                    # is a real column on THIS table -- stash the resolved DAX-ready column name so
                    # the dax.translate() call below can pass it through as `tc_partition` without
                    # re-deriving this same lookup a second time.
                    c["_tc_partition_field"] = c["tc_spec"]["level_break_field"]
                if tier != "D+":
                    self.declined[(pbi, fkey(c))] = (tier, {
                        "H": "table calculation -- addressing/partitioning is not in the "
                             "formula text",
                        "L1": "level-of-detail expression -- needs a CALCULATE/ALLEXCEPT "
                              "restructure",
                    }[tier])

        for iteration in range(1, len(self.ir["tables"]) * 200 + 3):
            grew = False
            for pbi, t in self.table_order:
                resolve, resolve_param, resolve_type, resolve_measure_safe, resolve_kind, \
                    resolve_formula = self._resolver(pbi)
                agg_map = self._aggregate_map(t)
                shelf_agg_map = self._shelf_agg_only(t)
                for c in t["calcs"]:
                    key = (pbi, fkey(c))
                    if key in self.declined:
                        continue
                    # A calc promoted to a MEASURE with no aggregate function of its own (its
                    # "aggregate" status came ONLY from a parameter/measure reference elsewhere in
                    # the fixpoint) needs every plain column reference SELECTEDVALUE-wrapped -- a
                    # bare column at measure top level has no row context. See
                    # `resolve_measure_safe`'s docstring for the real Desktop error this fixes.
                    becomes_measure = agg_map.get(fkey(c)) and not dax.is_aggregate(c["formula"])
                    # EXCEPT when the calc is used EXCLUSIVELY via one real shelf aggregation
                    # (`_shelf_agg_only`) -- then SELECTEDVALUE is the wrong choice (it returns
                    # BLANK across many real rows); inline the whole formula into an X-iterator
                    # instead, with plain bare-column resolution (the iterator supplies the row
                    # context SELECTEDVALUE was there to work around). See `_shelf_agg_only`'s own
                    # docstring for the real Desktop symptom this fixes (WorkbookA's "Card Penetration").
                    shelf_iter = shelf_agg_map.get(fkey(c)) if becomes_measure else None
                    active_resolve = resolve if shelf_iter else (
                        resolve_measure_safe if becomes_measure else resolve)
                    call_resolve, call_kind = active_resolve, resolve_kind
                    related = c.get("_related_refs")
                    if related:
                        # Pre-validated in the pre-pass above (every entry is a real column reached
                        # by a verified many-to-one relationship) -- substitute a relationship-aware
                        # reference in place of the normal same-table lookup, which would otherwise
                        # miss this field entirely (it is not allocated on THIS table) or, worse,
                        # silently bind to a same-named column on an unrelated sibling table via the
                        # global fallback index. RELATED() needs row context, so it is only correct
                        # when this calc itself becomes a calculated COLUMN, OR is about to be
                        # wrapped in an X-iterator below (`shelf_iter`, which supplies the same real
                        # row context a calculated column would have); a MEASURE with neither gets
                        # the bare cross-table reference through the SAME SELECTEDVALUE wrap
                        # `resolve_measure_safe` already applies to an own-table bare column,
                        # relying on the real relationship's own filter propagation.
                        def call_resolve(field, _base=active_resolve, _rel=related,
                                          _measure=(becomes_measure and not shelf_iter)):
                            if field in _rel:
                                return (f"SELECTEDVALUE({_rel[field]})" if _measure
                                        else f"RELATED({_rel[field]})")
                            return _base(field)

                        def call_kind(field, _base=resolve_kind, _rel=related):
                            return "column" if field in _rel else _base(field)
                    tc_partition_field = c.get("_tc_partition_field")
                    try:
                        expr, _r, _p = dax.translate(
                            c["formula"], call_resolve, resolve_param,
                            resolve_type, call_kind, own_type=c.get("datatype"),
                            start_of_week=(t.get("date_options") or {}).get("start_of_week"),
                            resolve_formula=resolve_formula,
                            tc_partition=(call_resolve(tc_partition_field)
                                          if tc_partition_field else None))
                        if shelf_iter:
                            expr = f"{shelf_iter}('{pbi}', {expr})"
                    except dax.Unsupported as e:
                        self.declined[key] = ("L2", self._explain(pbi, str(e)))
                        self.calc_dax.pop(key, None)
                        grew = True
                        continue
                    self.calc_dax[key] = expr
                # Groups and bins can depend on ANOTHER group/bin (a group-of-a-group is
                # ordinary Tableau usage), not only on calcs -- so they share this fixpoint
                # rather than resolving in one independent pass. A single pass emitted a group
                # whose source was itself a declined group with a plausible-looking DAX
                # reference to a column that was never written to TMDL: valid-looking DAX that
                # fails to load in Desktop ("Column '...' cannot be found"), the exact class of
                # dangling reference this fixpoint exists to prevent for calcs.
                for g in t["groups"]:
                    key = (pbi, fkey(g))
                    if key in self.declined:
                        continue
                    if not g["mapping"]:
                        self.declined[key] = ("D", "group has no member mapping")
                        grew = True
                    elif resolve(g["source"]) is None:
                        self.declined[key] = ("D",
                            f"group source field {g['source']!r} did not resolve")
                        grew = True
                for b in t["bins"]:
                    key = (pbi, fkey(b))
                    if key in self.declined:
                        continue
                    try:
                        size_ok = bool(float(b["size"]))
                    except (TypeError, ValueError):
                        size_ok = False
                    if resolve(b["source"]) is None or not size_ok:
                        self.declined[key] = ("D",
                            f"bin needs a resolvable numeric source and size; got "
                            f"source={b['source']!r} size={b['size']!r}")
                        grew = True
            if not grew:
                self.stats["resolve_iterations"] = iteration
                break

        for (pbi, field), (tier, reason) in sorted(self.declined.items()):
            hit = next((c for _p, t in self.table_order for c in t["calcs"]
                        if _p == pbi and fkey(c) == field), None)
            if hit is None:
                hit = next((g for _p, t in self.table_order
                            for g in t["groups"] + t["bins"] if _p == pbi and fkey(g) == field),
                           None)
            self.residue.add(tier, pbi, (hit or {}).get("name", field), reason,
                             (hit or {}).get("formula"))
            self.stats["residue"] += 1

    # ---------------------------------------------------------------- emit
    def build(self):
        self.allocate()
        self.resolve_calcs()
        # `database` carries NO name in a Desktop-authored file -- just the bare keyword.
        self.files["definition/database.tmdl"] = "\n".join([
            _line(0, "database"),
            _line(1, f"compatibilityLevel: {COMPATIBILITY_LEVEL}"),
        ]) + "\n"
        self.files["definition/cultures/en-US.tmdl"] = "cultureInfo en-US\n"

        for pbi, t in self.table_order:
            self.files[f"definition/tables/{self._safe_file(pbi)}.tmdl"] = self._table(pbi, t)
        for p in self.ir["parameters"]:
            rec = self.na.params[p["name"]]
            path = f"definition/tables/{self._safe_file(rec['table'])}.tmdl"
            self.files[path] = self._param_table(p, rec)

        # relationships.tmdl is written ONLY when there is a relationship to put in it. Desktop's
        # own single-table project has NO such file, and a comment-only document is rejected:
        #   Document './relationships' Line 1 - '// All model relationships...'
        #   Parsing error type - InvalidLineType / Unexpected line type: Other!
        rels = self._relationships()
        if rels:
            self.files["definition/relationships.tmdl"] = rels
        self.files["definition/model.tmdl"] = self._model()
        return self.files

    @staticmethod
    def _safe_file(name):
        """A table name is not a filename. `/` and `:` would create directories or break on
        Windows; the 260-char path limit is why names.sanitize caps length upstream."""
        return re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(". ")

    def _model(self):
        """model.tmdl, matched to a Desktop-authored file.

        The shape that matters: `annotation` and `ref` lines sit at COLUMN 0 -- they are top-level
        documents in the TMDL project, NOT properties of `model`. Emitting `\tref table X` indented
        under `model` is what made Desktop refuse the whole project:

            TMDL Format Error: Unexpected line type: ReferenceObject!
            Document './model'  Line 6 - '    ref table MSD_Case_Details_Compliance'

        `discourageImplicitMeasures` is dropped: legal, but Desktop does not write it, and it
        suppresses the implicit aggregations a reader may expect to find.
        """
        tables = [pbi for pbi, _t in self.table_order]
        tables += [self.na.params[p["name"]]["table"] for p in self.ir["parameters"]]
        order = ", ".join('"' + t.replace('"', '\\"') + '"' for t in tables)
        out = [_line(0, "model Model"),
               _line(1, "culture: en-US"),
               _line(1, "defaultPowerBIDataSourceVersion: powerBI_V3"),
               _line(1, "sourceQueryCulture: en-US"),
               _line(1, "dataAccessOptions"),
               _line(2, "legacyRedirects"),
               _line(2, "returnErrorValuesAsNull"),
               "",
               _line(0, f"annotation PBI_QueryOrder = [{order}]"),
               ""]
        for t in tables:
            out.append(_line(0, f"ref table {tmdl_ident(t)}"))
            out.append("")
        out.append(_line(0, "ref cultureInfo en-US"))
        return "\n".join(out) + "\n"

    def _table(self, pbi, t):
        resolve, resolve_param, _rt, resolve_measure_safe, _rk, _rf = self._resolver(pbi)
        out = []
        out += _desc(0, f"From Tableau datasource {t['caption']} ({t['provenance']})")
        out.append(_line(0, f"table {tmdl_ident(pbi)}"))
        out.append(_line(1, f"lineageTag: {lineage('table', pbi)}"))
        out.append("")

        physical = []      # (pbi name, dataType) for the partition schema
        measures, calc_cols = [], []

        for c in t["columns"]:
            name, _ = self.na.lookup(pbi, fkey(c))
            # sourceColumn must name a column the PARTITION'S OWN M actually produces (SM-TYPE-03),
            # not the raw Tableau/database column name captured in c["source_column"]. In stub mode
            # (the default -- see _partition) the M body is a synthetic Table.FromRows typed with
            # m_ident(name) for each entry in `physical` below, i.e. the ALLOCATED display name, so
            # sourceColumn must be that same `name` or Desktop opens the table and immediately fails
            # it with "column not found" the moment the spelling/case differs even slightly -- which
            # it almost always does, since c["source_column"] is the pre-allocation source spelling
            # (e.g. a lowercase/snake_case db column) while `name` has already been through
            # names.py's caption-based allocation. Native mode's own NATIVE_M connectors return the
            # bare connection object with no per-column projection either, so no current partition
            # shape actually emits c["source_column"] anywhere -- using `name` is correct for both.
            out += self._column(pbi, name, c["datatype"], c["role"],
                                source=name, hidden=c["hidden"], folder=c.get("folder"))
            physical.append((name, c["datatype"]))
            self.stats["columns"] += 1

        agg_map = self._aggregate_map(t)

        for g in t["groups"]:
            # A group's `source` reference has no aggregate function of its own by construction
            # (see `_aggregate_map`'s docstring) -- if the group is promoted to a measure, its
            # source reference needs the SAME SELECTEDVALUE-wrapping a promoted calc gets.
            promoted = agg_map.get(fkey(g))
            body = self._group_body(pbi, g, resolve_measure_safe if promoted else resolve)
            if body is None:
                continue        # declined; already recorded as residue by resolve_calcs
            name, expr, _g = body
            # A group's own summarizeBy was always the literal "none" -- it has no "role" key the
            # way a calc does, so it cannot go through `summarize_by(c["role"], ...)`.
            (measures if promoted else calc_cols).append(
                (name, expr, g.get("datatype"), "none", None, g.get("folder"), None))
            self.stats["groups"] += 1

        for b in t["bins"]:
            promoted = agg_map.get(fkey(b))
            body = self._bin_body(pbi, b, resolve_measure_safe if promoted else resolve)
            if body is None:
                continue        # declined; already recorded as residue by resolve_calcs
            name, expr, _b = body
            (measures if promoted else calc_cols).append(
                (name, expr, b.get("datatype"), "none", None, b.get("folder"), None))
            self.stats["bins"] += 1

        for c in t["calcs"]:
            expr = self.calc_dax.get((pbi, fkey(c)))
            if expr is None:
                continue        # declined; already recorded as residue by resolve_calcs
            name, _ = self.na.lookup(pbi, fkey(c))
            entry = (name, expr, c["datatype"], summarize_by(c["role"], c["datatype"]),
                     c.get("description"), c.get("folder"), c.get("format_string"))
            if agg_map[fkey(c)]:
                measures.append(entry)
            else:
                calc_cols.append(entry)

        # Desktop writes MEASURES FIRST, then columns, then the partition. Matched so a
        # regenerated file diffs cleanly against a Desktop round-trip.
        for name, expr, _dtype, _summarize, description, folder, format_string in measures:
            out += _desc(1, description)
            out += _measure(pbi, name, expr, folder=folder, format_string=format_string)
            self.na.mark_emitted(pbi, name)
            self.stats["measures"] += 1

        for name, expr, dtype, summarize, _description, folder, format_string in calc_cols:
            out += _calc_column(pbi, name, expr, summarize, dtype=dtype, folder=folder,
                                format_string=format_string)
            self.na.mark_emitted(pbi, name)
            self.stats["calc_columns"] += 1

        # Shelf-level aggregates with no calculated field behind them (row 25). Tableau writes
        # `SUM([Sales])` on a shelf with no <column> for it; Power BI has implicit measures, but
        # the report layer must name something, so an explicit measure is emitted per (field, agg)
        # actually used. Explicit beats implicit: an implicit measure cannot be referenced by name
        # from PBIR.
        for name, expr, src in self._shelf_measures(pbi, t):
            out += _measure(pbi, name, expr, tag="shelfmeasure")
            self.na.mark_emitted(pbi, name)
            self.stats["shelf_measures"] += 1

        out += self._hierarchies(pbi, t)
        out += self._partition(pbi, t, physical)
        out.append(_line(1, "annotation PBI_ResultType = Table"))
        out.append("")
        return "\n".join(out) + "\n"

    def _column(self, pbi, name, dtype, role, source=None, hidden=False, key=False, folder=None):
        self.na.mark_emitted(pbi, name)
        dtype = _tom_dtype(dtype)
        out = [_line(1, f"column {tmdl_ident(name)}"),
               _line(2, f"dataType: {dtype}")]
        if key:
            out.append(_line(2, "isKey"))
        if hidden:
            out.append(_line(2, "isHidden"))
        out.append(_line(2, f"lineageTag: {lineage('column', pbi, name)}"))
        out.append(_line(2, f"summarizeBy: {summarize_by(role, dtype)}"))
        if source:
            out.append(_line(2, f"sourceColumn: {tmdl_ident(source)}"))
        out += _display_folder_line(2, folder)
        # Desktop writes this on every column, preceded by a blank line.
        out.append("")
        out.append(_line(2, "annotation SummarizationSetBy = Automatic"))
        out.append("")
        return out

    def _group_body(self, pbi, g, resolve):
        """A Tableau group -> `(name, SWITCH(TRUE(), ...) DAX, g)`, or None if declined.

        Deterministic and exact: a group is a literal member->bucket mapping, so the translation
        needs no inference. Members not listed keep their own value, which is Tableau's behaviour
        for an incomplete group.

        Resolution (does the source resolve, is the mapping non-empty) already ran to a fixpoint
        in `resolve_calcs`, alongside calcs and bins -- a group's source can be ANOTHER group,
        so a group-only pass here could emit a reference to a group declined later in the same
        pass. `self.declined` is the single source of truth for what got skipped and why; this
        function only builds the body -- `_write_table` decides column vs measure and emits,
        the same as it already does for calcs (see `_aggregate_map`'s docstring on why a group's
        source can make it a measure too).
        """
        if (pbi, fkey(g)) in self.declined:
            return None
        name, _ = self.na.lookup(pbi, fkey(g))
        src = resolve(g["source"])
        branches = []
        for bucket, members in sorted(g["mapping"].items()):
            for m in members:
                branches.append(f'{src} = "{m}", "{bucket}"')
        body = "SWITCH(TRUE(),\n" + ",\n".join("    " + b for b in branches) + f",\n    {src})"
        return name, body, g

    def _bin_body(self, pbi, b, resolve):
        """A Tableau bin -> `(name, FLOOR(x / size) * size DAX, b)`, or None if declined.

        Exact for a numeric bin of fixed width. Resolution already ran to a fixpoint in
        `resolve_calcs` (see `_group_body`'s note); this function only builds the body.
        """
        if (pbi, fkey(b)) in self.declined:
            return None
        name, _ = self.na.lookup(pbi, fkey(b))
        src = resolve(b["source"])
        size = float(b["size"])
        sz = int(size) if size.is_integer() else size
        return name, f"FLOOR({src} / {sz}, 1) * {sz}", b

    def _shelf_measures(self, pbi, t):
        return self.shelf_plan.get(pbi, [])

    def _hierarchies(self, pbi, t):
        out = []
        for h in t["hierarchies"]:
            levels = []
            for lv in h["levels"]:
                # emitted_only: a hierarchy level must point at a column that EXISTS. `Issr SRegn`
                # is a Tableau group whose own source did not resolve, so it was declined and no
                # column was written -- yet the level was still emitted, referencing nothing. Every
                # other by-name lookup already guarded for this; this was the one that did not.
                _t, name, kind = self.na.by_spelling(lv, table=pbi, emitted_only=True)
                if name and kind == "column":
                    levels.append(name)
            if len(levels) != len(h["levels"]):
                missing = len(h["levels"]) - len(levels)
                self.residue.add("D", pbi, h["name"],
                                 f"{missing} of {len(h['levels'])} drill-path level(s) resolved to "
                                 f"no emitted column, so the hierarchy is incomplete"
                                 if levels else
                                 "drill path resolved to no columns in this table")
                self.stats["residue"] += 1
            if not levels:
                continue
            hname = f"{h['name']} Hierarchy"
            out.append(_line(1, f"hierarchy {tmdl_ident(hname)}"))
            out.append(_line(2, f"lineageTag: {lineage('hierarchy', pbi, hname)}"))
            out.append("")
            for lv in levels:
                out.append(_line(2, f"level {tmdl_ident(lv)}"))
                out.append(_line(3, f"column: {tmdl_ident(lv)}"))
                out.append("")
            self.stats["hierarchies"] += 1
        return out

    def _partition(self, pbi, t, physical):
        conns = [c for c in t["connections"] if not c["is_proxy"] and not c["is_engine"]]
        native = None
        if self.partitions == "native":
            for c in conns:
                fn = NATIVE_M.get(c["class"])
                if fn:
                    native = fn(c)
                    break
        out = [_line(1, f"partition {tmdl_ident(pbi)} = m"), _line(2, "mode: import")]
        if native:
            body = f"let\n    Source = {native}\nin\n    Source"
            self.stats["native_partitions"] += 1
        else:
            if self.partitions == "native":
                self.notes.append(
                    f"{pbi}: no M connector mapping for connection classes "
                    f"{sorted({c['class'] for c in t['connections']})} -- stub partition emitted")
            cols = ", ".join(f"{m_ident(n)} = {M_TYPE.get(_tom_dtype(d), 'text')}"
                             for n, d in physical) or "Placeholder = text"
            body = ("let\n"
                    f"    Source = Table.FromRows({{}}, type table [{cols}])\n"
                    "in\n"
                    "    Source")
            self.stats["stub_partitions"] += 1
        out += _expr(2, "source", body)
        out.append("")
        return out

    def _param_table(self, p, rec):
        """A Tableau parameter -> a one-row table + a SELECTEDVALUE measure.

        The list/range domain becomes the table's rows so a slicer has something to bind to; an
        `any` (free-entry) domain has no enumerable domain at all, so only the current value is
        emitted and the widening is recorded as residue rather than invented.
        """
        vals = list(p["members"])
        if not vals and p["range"] and p["range"].get("min") is not None:
            lo, hi = p["range"]["min"], p["range"]["max"]
            step = p["range"].get("step") or 1
            try:
                cur, guard = lo, 0
                while cur <= hi and guard < 1000:
                    vals.append(cur)
                    cur += step
                    guard += 1
            except TypeError:
                # decode_param_value() only casts range bounds to int/float when the text parses
                # as one -- a date-range parameter's min/max stay plain ISO strings, and
                # `cur += step` (str + int) raises here rather than silently walking off into
                # string concatenation. Falls through to the single-value case below like a free-
                # entry domain does, but MUST say so: that branch already records residue for
                # narrowing a domain down to one value, and doing it here silently (`vals = []`
                # with nothing said) would be exactly the kind of silent scope-narrowing this
                # project's own RESIDUE.md exists to prevent.
                vals = []
                self.residue.add("D", rec["table"], p["name"],
                                 "range-domain parameter whose min/max/step could not be "
                                 "enumerated arithmetically (likely a date/dateTime range): only "
                                 "the saved value is emitted, so the slicer offers one choice")
                self.stats["residue"] += 1
        if not vals:
            vals = [p["value"]]
            if p["domain"] == "any":
                self.residue.add("L4", rec["table"], p["name"],
                                 "free-entry parameter domain: only the saved value is "
                                 "enumerable, so the slicer offers one choice")
                self.stats["residue"] += 1

        rows = ", ".join("{" + _param_literal(v, p["datatype"]) + "}" for v in vals)
        mtype = M_TYPE.get(_tom_dtype(p["datatype"]), "text")
        body = ("let\n"
                f"    Source = Table.FromRows({{{rows}}}, "
                f"type table [{m_ident(rec['column'])} = {mtype}])\n"
                "in\n"
                "    Source")
        out = _desc(0, f"Tableau parameter {p['name']} (domain: {p['domain']})")
        out.append(_line(0, f"table {tmdl_ident(rec['table'])}"))
        out.append(_line(1, f"lineageTag: {lineage('table', rec['table'])}"))
        out.append("")
        out += self._column(rec["table"], rec["column"], p["datatype"], "dimension",
                            source=rec["column"])
        # SELECTEDVALUE's optional 2nd arg (alternateResult) -- standard DAX, not a new construct
        # -- makes this measure resolve to Tableau's own saved current value the moment the
        # report opens, instead of BLANK() until a user clicks a slicer. Without this, EVERY
        # measure built on a parameter (a large fraction of most workbooks' calcs) reads blank on
        # first open, because a multi-row Param table starts with nothing selected and Power BI
        # emits no default-selection state for it. Real corpus example: WorkbookB Dashboard's own
        # "1D - OI Summary" tab renders nothing until pl.Slice By/pl.Selected Metric/etc. are all
        # manually picked -- this is the fix, not a workaround at the report layer, because it
        # is one already-proven DAX function, not an unconfirmed PBIR page/report filterConfig
        # shape (this project only has visual-level filterConfig confirmed against real Desktop
        # output; report/page-level would be an unverified guess, so it was deliberately not
        # used here).
        selected_value = f"SELECTEDVALUE({dax_ref(rec['table'], rec['column'])}"
        if p["value"] is not None:
            selected_value += f", {_param_dax_literal(p['value'], p['datatype'])}"
        selected_value += ")"
        out.append(_line(1, f"measure {tmdl_ident(rec['measure'])} = {selected_value}"))
        out.append(_line(2, f"lineageTag: {lineage('measure', rec['table'], rec['measure'])}"))
        self.na.mark_emitted(rec["table"], rec["measure"])
        out.append("")
        out.append(_line(1, f"partition {tmdl_ident(rec['table'])} = m"))
        out.append(_line(2, "mode: import"))
        out += _expr(2, "source", body)
        out.append("")
        # Same table-level annotation Desktop writes. The parameter table went down a separate code
        # path and silently missed it -- caught by the ground-truth check, not by the pre-flight.
        out.append(_line(1, "annotation PBI_ResultType = Table"))
        out.append("")
        self.stats["parameters"] += 1
        return "\n".join(out) + "\n"

    def _blend_pairs(self):
        """Pairs of PBI tables whose fields ACTUALLY co-occur in at least one Tableau worksheet's
        own field wells or categorical filters -- i.e. real Tableau data BLENDING, not a
        coincidental column-name match between two tables no worksheet ever combines. Tableau
        stores no linking-field/blend-key element anywhere in the .twb for this (confirmed by
        grep: no `linking-field` tag exists in this corpus) -- a blend is matched at query time by
        common field name, so "two different tables named on the same worksheet's own shelves/
        filters" is the only observable fact available to detect one from the file alone.

        Mirrors the exact scope pbir.py's `_shelf_shape`/`_query_state`/`_visual_filters` use to
        decide what a visual's OWN query touches: Rows/Cols, the Text-shelf fallback when both are
        silent (the BAN/text-table idiom -- see `_shelf_shape`'s own comment), and every
        categorical filter field. A `Param `-prefixed table is excluded -- a parameter's own
        SELECTEDVALUE binding needs no relationship, the same convention `_visual_filters`
        already applies to it.

        Returns {frozenset({tableA, tableB}): [worksheet name, ...]}, corpus-agnostic: nothing
        about any particular table/workbook name is hard-coded, only the worksheet's own field
        references.
        """
        pairs = collections.defaultdict(list)
        for ws in self.ir["worksheets"]:
            rows = ws["shelves"].get("rows", [])
            cols = ws["shelves"].get("cols", [])
            fields = [e["field"] for e in (rows + cols)]
            if not rows and not cols:
                fields += [e["field"] for e in ws["encodings"] if e["channel"] == "text"]
            for flt in ws.get("filters") or []:
                if flt.get("field"):
                    fields.append(flt["field"])
            ents = set()
            for f in fields:
                tbl, _name, _kind = self.na.by_spelling(f, emitted_only=True)
                if tbl and not tbl.startswith("Param "):
                    ents.add(tbl)
            ordered = sorted(ents)
            for i in range(len(ordered)):
                for j in range(i + 1, len(ordered)):
                    pairs[frozenset((ordered[i], ordered[j]))].append(ws["name"])
        return pairs

    def _column_dtype_index(self):
        """{(pbi table, pbi column name): {"datatype": ..., "formula": ...}}, EMITTED columns
        only -- measures excluded, a blend key is a dimension, never an aggregate. Built from the
        real emitted state (`emitted_only=True`), not the allocation-time one, which is why this
        is only ever called from `_relationships`, after both table-writing loops in `build()`
        have already run and every calc has either been emitted or declined.

        `formula` is `None` for a plain physical column (ir.json's `columns` entries carry no
        `formula` key at all) and the raw Tableau calc text for a `calcs` entry -- fed to
        `_formula_shape` by `_shared_columns` to catch a same-name/same-dtype column computed
        two structurally different ways on either side (see `_formula_shape`'s docstring)."""
        idx = {}
        for pbi, t in self.table_order:
            for c in t["columns"] + t["calcs"]:
                name, kind = self.na.lookup(pbi, fkey(c), emitted_only=True)
                if name and kind == "column":
                    idx[(pbi, name)] = {"datatype": c.get("datatype"), "formula": c.get("formula")}
        return idx

    @staticmethod
    def _shared_columns(a, b, dtype_index):
        """Column names common to tables `a` and `b` by exact display name -- the SAME signal
        Tableau's own blend engine uses to line up a secondary field with the primary (it matches
        by caption; there is no stored key to match by instead). A name present on both sides but
        with a DIFFERENT Power BI dataType is excluded: `docs/schema-reference/pbip-constraint-
        rules.md`'s SM-STRUCT-06 -- Power BI refuses a relationship whose two columns do not share
        a dataType, where Tableau's blend would have silently coerced.

        A name/dtype match is additionally required to have the SAME `_formula_shape` on both
        sides -- same rough class of computation, same field-reference count. A match whose two
        sides compute the value differently (real corpus regression: `Quarter Year` = concat of
        two fields on one side, a bare single-field alias on the other) is excluded and returned
        separately as `shape_mismatches`, not silently treated as a safe key: Tableau's blend
        matches by caption alone and would have joined these same mismatched values, which is
        exactly the defect this closes. Returns `(shared, shape_mismatches)`.

        NOT caught here, by design: two columns that are the SAME shape (e.g. both a bare
        physical column) but whose real-world VALUE DOMAINS never overlap (real corpus
        regression: `for_view` in {opcert, opcert_dist} on one side, {Channel, MCC, ...} on the
        other) -- nothing in the IR distinguishes that from a legitimately shared dimension; it
        is only detectable once real data exists, which is Agent 4's `validate-blend-key-domains`
        check's job, not this static, build-time one.
        """
        cols_a = {n: v for (t, n), v in dtype_index.items() if t == a}
        cols_b = {n: v for (t, n), v in dtype_index.items() if t == b}
        candidates = sorted(n for n in (set(cols_a) & set(cols_b))
                             if cols_a[n]["datatype"] == cols_b[n]["datatype"])
        shared, shape_mismatches = [], []
        for n in candidates:
            shape_a = _formula_shape(cols_a[n]["formula"])
            shape_b = _formula_shape(cols_b[n]["formula"])
            if shape_a is not None and shape_a == shape_b:
                shared.append(n)
            else:
                shape_mismatches.append((n, cols_a[n]["formula"], cols_b[n]["formula"]))
        return shared, shape_mismatches

    def _append_calc_column(self, pbi_table, lines):
        """Splice extra calculated-column TMDL lines into a table file `_table()` already wrote,
        just before its closing `annotation PBI_ResultType = Table`. The composite blend key is
        only known once EVERY table has been emitted (see `_column_dtype_index`'s docstring),
        which is after `_table()` has already returned that table's complete text -- so the key
        column cannot go through the normal calc-column path inside `_table()` itself."""
        path = f"definition/tables/{self._safe_file(pbi_table)}.tmdl"
        text = self.files[path]
        marker = _line(1, "annotation PBI_ResultType = Table")
        idx = text.rfind(marker)
        assert idx != -1, f"{pbi_table!r} TMDL is missing its trailing PBI_ResultType annotation"
        self.files[path] = text[:idx] + "\n".join(lines) + "\n" + text[idx:]

    def _reachable(self, start, edges):
        """Every table reachable from `start` by walking an undirected edge LIST (a, b) tuples --
        used to detect a blend relationship that would close a cycle/create a second path between
        two tables that are already connected, which `docs/schema-reference/pbip-constraint-
        rules.md`'s SM-STRUCT-08 forbids ("At most one ACTIVE relationship may exist on any path
        between two tables, and the active set must contain no ambiguous or circular path")."""
        adj = collections.defaultdict(set)
        for x, y in edges:
            adj[x].add(y)
            adj[y].add(x)
        seen, frontier = {start}, [start]
        while frontier:
            nxt = []
            for n in frontier:
                for nb in adj.get(n, ()):
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
        return seen

    def _relationships(self):
        """All relationships in one file. Emits key-based ones Tableau actually declares, PLUS a
        deterministic blend-key relationship for a genuine, worksheet-observed data BLEND between
        two top-level datasources (`_blend_pairs`) that share at least one same-name, same-dtype
        column (`_shared_columns`) -- corpus-agnostic: driven entirely by what a worksheet's own
        field references and the tables' own columns say, nothing about any specific workbook.

        Real Desktop bug this closes: NGPO Dashboard New - AP Version (MSC), "Level 2 Analysis"/
        "Level 4 Analysis" pages -- multiple worksheets project fields from two federated
        datasources at once (Tableau data blending) with NO relationship anywhere in the model
        (this method emitted nothing for real corpus input before this). Desktop: "Can't determine
        relationships between the fields... Underlying Error: InvalidUnconstrainedJoin." Tableau
        itself stores no blend key -- it matches a secondary datasource's fields to the primary's
        by common CAPTION at query time -- so the shared column name(s) `_shared_columns` finds
        are the only deterministic signal available for a Power BI relationship at all.

        A single shared column becomes a direct relationship. More than one becomes a calculated
        composite key (`&`-concatenated, same formula on both sides) -- Tableau blends on the
        FULL set of common field names, not just the strongest one, and keying on a subset would
        match rows the real blend kept apart (e.g. the same Product in a different Quarter),
        silently fanning many-to-many rows out into a WRONG total rather than just resolving DS0.

        Every emitted blend relationship is `manyToMany`/`bothDirections`, deliberately, never
        `oneToMany`: there is no data (default `--partitions stub` is zero rows) and no stored key
        to verify uniqueness on either side against, and asserting one-sided uniqueness anyway is
        exactly what SM-STRUCT-07 warns against ("A Tableau join that is genuinely many-to-many
        must not be emitted as many-to-one just because the cardinality is unknown"). manyToMany
        asserts no uniqueness on either side, so it cannot fail Desktop's own relationship-load
        check regardless of what the real data turns out to be.

        Two things are reported as GAP residue rather than guessed at, matching the fail-closed
        rule the rest of this pipeline already follows: a blend pair with no shared column/dtype
        at all (no deterministic key exists), and a blend pair whose relationship would create a
        second path between two already-connected tables (SM-STRUCT-08 -- Desktop refuses to load
        an ambiguous/circular relationship graph, a whole-project failure far worse than one
        undecided visual). Both leave `self.relationship_pairs` unchanged for that pair, so
        pbir.py's reachability checks correctly keep declining any visual that needs it.
        """
        out = []
        # Real key-based joins extract.py's split_federated_datasource already proved safe to
        # split on -- a real key present as a real column on both sides, plus Tableau's own
        # unique-key declaration on at least one side. `entry["one_field"]`/`entry["many_field"]`
        # are already stripped Tableau internal names (fkey identity), so a lookup against a
        # PHYSICAL column allocated in `allocate()` should never miss; if it somehow does (an
        # extraction-layer bug, not a translation gap), decline with a GAP rather than emit a
        # relationship pointing at a column that was never written.
        #
        # CARDINALITY, on every one of these, always: unlike a blend relationship (no stored
        # uniqueness at all, hence manyToMany as the only honest default), split_federated_data-
        # source() only ever creates a `resolved_joins` entry when Tableau's OWN metadata declares
        # one side unique (`from_unique`/`to_unique` -- see that function's own docstring, point
        # 3: "the real cardinality is stated, never inferred from row counts we do not have").
        # Emitting it explicitly removes any dependence on Desktop's own data-sampled inference,
        # which a small dummy/stub row count makes genuinely unreliable either way.
        #
        # DIRECTION: MULTI-SPOKE HUBS need bothDirections; single-spoke ones do not. Real Desktop
        # bug this closes (dummy-data-populated "Digital Sales - Leads Dashboard"): its ONE-side
        # table (`bdr_lead_global_tableau`) has TWO different MANY-side tables joined to it (a
        # lead's meetings and its opportunities) -- a plain flat table combining raw columns from
        # both, filtered by a field on either, needs to filter fact -> dimension -> fact, which
        # the default single cross-filter direction (dimension -> fact only) cannot do. Desktop:
        # "There is a missing relationship between these fields. Use Autodetect...". Contrast
        # with a single-spoke hub (real corpus example: "WorkbookB Dashboard"'s `Insights` fact
        # joined to 13 DIFFERENT one-side dimensions, each with exactly one spoke) -- an ordinary
        # star schema that Desktop's default single direction already handles fine, so it is left
        # untouched: turning on bothDirections where it is not needed only adds the well-documented
        # risk of bidirectional relationships changing filter context in unrelated measures.
        # Topologically safe either way: `split_federated_datasource()` only ever splits a REAL
        # spanning TREE (its own docstring, point 4), so turning on bothDirections on a tree edge
        # can never create the cycle `tools/pbip_rules/checks.py`'s `check_relationship_ambiguity`
        # (SM-STRUCT-08) exists to catch -- that check is about relationship COUNT/topology, not
        # direction.
        hub_many_tables = collections.defaultdict(set)
        for rj in self.ir.get("resolved_joins") or []:
            hub_many_tables[rj["one_table"]].add(rj["many_table"])
        multi_spoke_hubs = {one for one, many in hub_many_tables.items() if len(many) >= 2}

        by_tableau_name = {t["tableau_name"]: pbi for pbi, t in self.table_order}
        for rj in self.ir.get("resolved_joins") or []:
            one_pbi = by_tableau_name.get(rj["one_table"])
            many_pbi = by_tableau_name.get(rj["many_table"])
            one_col, _k1 = self.na.lookup(one_pbi, rj["one_field"]) if one_pbi else (None, None)
            many_col, _k2 = self.na.lookup(many_pbi, rj["many_field"]) if many_pbi else (None, None)
            if not (one_pbi and many_pbi and one_col and many_col):
                self.residue.add(
                    "GAP", many_pbi or rj["many_table"],
                    f"{rj['many_table']} ~ {rj['one_table']}",
                    "a real key-based join was split into separate tables, but the join key "
                    "column could not be resolved back to an allocated name on one or both "
                    "sides -- relationship not emitted rather than pointing at a column that "
                    "was never written")
                self.stats["residue"] += 1
                continue
            relid = lineage("relationship", many_pbi, one_pbi, many_col, one_col)
            out.append(_line(0, f"relationship {relid}"))
            out.append(_line(1, f"fromColumn: {tmdl_ident(many_pbi)}.{tmdl_ident(many_col)}"))
            out.append(_line(1, f"toColumn: {tmdl_ident(one_pbi)}.{tmdl_ident(one_col)}"))
            out.append(_line(1, "fromCardinality: many"))
            out.append(_line(1, "toCardinality: one"))
            if rj["one_table"] in multi_spoke_hubs:
                out.append(_line(1, "crossFilteringBehavior: bothDirections"))
                self.stats["bidirectional_hub_relationships"] = self.stats.get(
                    "bidirectional_hub_relationships", 0) + 1
            out.append("")
            self.relationship_pairs.append(tuple(sorted((many_pbi, one_pbi))))
            self.stats["key_based_relationships"] = self.stats.get("key_based_relationships", 0) + 1
        for pbi, t in self.table_order:
            # PHYSICAL joins first. These are a different element from a logical-layer
            # <object-graph> relationship, and checking only the latter dropped a real key-based
            # join with no relationship and no residue entry at all.
            for rel in t.get("relations") or []:
                if rel.get("type") != "join":
                    continue
                keys = rel.get("key_pairs") or []
                pairs = ", ".join(f"{k['from']} = {k['to']}" for k in keys) or "no key expression"
                self.residue.add(
                    "GAP", pbi,
                    f"{rel.get('join') or 'join'} join: {' ~ '.join(rel.get('operands') or [])}",
                    f"physical-layer join ({pairs}) inside a datasource that is extracted as a "
                    f"SINGLE table -- there is no second model table to relate to, so the join "
                    f"belongs in the partition query rather than in relationships.tmdl")
                self.stats["residue"] += 1
            og = t.get("object_graph")
            if not og:
                continue
            if len(og["objects"]) > 1:
                self.notes.append(
                    f"{pbi}: Tableau datasource federates {len(og['objects'])} physical objects "
                    f"({', '.join(o['caption'] for o in og['objects'][:4])}"
                    f"{'...' if len(og['objects']) > 4 else ''}) into one table -- the physical "
                    f"layer is collapsed, so its internal joins are not modelled as "
                    f"relationships")
            for rel in og["relationships"]:
                if rel["cross_join"]:
                    self.residue.add("GAP", pbi,
                                     f"{rel['from_object']} ~ {rel['to_object']}",
                                     "Tableau '1 = 1' relationship (relate with no matching "
                                     "fields): a Power BI relationship cannot join on a constant")
                    self.stats["residue"] += 1
                    continue
                # Surface the real join key and Tableau's own unique-key declaration -- same
                # level of detail as the physical-layer join message above. Without this, the
                # reason read "no second table to relate to" even though a real, named second
                # table and a real join key ARE present; only the ability to query that table
                # independently is missing (one opaque connection, not per-table connections).
                keys = rel.get("key_pairs") or []
                pairs = ", ".join(f"{k['from']} = {k['to']}" for k in keys) or "no key expression"
                if rel.get("from_unique") and not rel.get("to_unique"):
                    unique_note = f"; '{rel['from_object']}' side is the unique/'one' side"
                elif rel.get("to_unique") and not rel.get("from_unique"):
                    unique_note = f"; '{rel['to_object']}' side is the unique/'one' side"
                elif rel.get("from_unique") and rel.get("to_unique"):
                    unique_note = "; both sides declared unique (one-to-one)"
                else:
                    unique_note = "; neither side declared unique -- cardinality unknown"
                self.residue.add("GAP", pbi,
                                 f"{rel['from_object']} ~ {rel['to_object']}",
                                 f"key-based join ({pairs}{unique_note}) inside a federated "
                                 f"datasource that is extracted as a single table -- the second "
                                 f"table is real and named, but not independently queryable "
                                 f"through this one shared connection")
                self.stats["residue"] += 1
        dtype_index = self._column_dtype_index()
        # Priority order: most-worksheets-evidenced pair first, ties broken alphabetically for a
        # stable rebuild. This is a MAXIMUM weighted spanning forest over the blend graph (plain
        # greedy/Kruskal, using `_reachable` instead of a formal union-find since the corpus-scale
        # table count makes that unnecessary) -- the pair with the strongest real usage wins a
        # DIRECT edge; a pair with less evidence that would otherwise create a second path between
        # two already-connected tables (SM-STRUCT-08) is left to ride the stronger path instead of
        # competing with it. Processing in name order instead (tried first) let an incidental,
        # rarely-used pair claim a table's only direct edge, silently pushing a heavily-used real
        # pair like the NGPO bug's own Level 2/Level 4 blend onto a longer, less faithful path.
        for pair, worksheets in sorted(self._blend_pairs().items(),
                                        key=lambda kv: (-len(set(kv[1])), tuple(sorted(kv[0])))):
            a, b = sorted(pair)
            ws_seen = sorted(set(worksheets))
            ws_list = ", ".join(ws_seen[:4]) + (f" (+{len(ws_seen) - 4} more)"
                                                 if len(ws_seen) > 4 else "")
            if b in self._reachable(a, self.relationship_pairs):
                # NOT a gap: the join path Desktop needs already exists, through a
                # higher-priority blend edge. Emitting this one TOO would add a second path
                # between the same two tables, which Power BI refuses to load at all (an
                # ambiguous/circular relationship graph -- SM-STRUCT-08), so it is silently
                # skipped rather than reported as a shortfall.
                self.notes.append(
                    f"{a} <-> {b}: also blended in worksheet(s) {ws_list} -- already connected "
                    "via another, more-used blend relationship, so no separate direct edge is "
                    "emitted (a second path would be an ambiguous/circular relationship graph, "
                    "SM-STRUCT-08); the existing path already resolves any InvalidUnconstrainedJoin")
                continue
            shared, shape_mismatches = self._shared_columns(a, b, dtype_index)
            for name, formula_a, formula_b in shape_mismatches:
                self.residue.add(
                    "GAP", a, f"blend key candidate {name!r} shared with {b!r}",
                    f"worksheet(s) {ws_list} pull fields from both {a!r} and {b!r} in the same "
                    f"view, and both tables have a column named {name!r} with the same Power BI "
                    f"dataType, which would normally key this blend pair -- but the two sides "
                    f"compute {name!r} differently ({_describe_formula(formula_a)} on {a!r} "
                    f"vs {_describe_formula(formula_b)} on {b!r}). Tableau's own blend matches "
                    f"by caption alone and would have joined these mismatched values too; a "
                    f"same-caption column computed two structurally different ways is not a "
                    f"safe deterministic key, so {name!r} is excluded from this pair's key "
                    f"rather than joined on a column that renders a different format/"
                    f"granularity on each side")
                self.stats["residue"] += 1
                self.stats["blend_key_shape_declined"] += 1
            if not shared:
                self.residue.add(
                    "GAP", a, f"data blend with {b!r}",
                    f"worksheet(s) {ws_list} pull fields from both {a!r} and {b!r} in the same "
                    "view -- Tableau data blending (no stored linking-field anywhere in the "
                    ".twb; matched at query time by common field name) -- but the two tables "
                    "share no column with the same name AND the same Power BI dataType, so "
                    "there is no deterministic key to build a relationship on. Emitting a "
                    "visual that queries both would hit Desktop's InvalidUnconstrainedJoin "
                    "(\"Can't determine relationships between the fields\"); no relationship is "
                    "emitted, and the affected visual(s) are declined at the report layer "
                    "instead")
                self.stats["residue"] += 1
                self.stats["blend_pairs_declined"] += 1
                continue
            if len(shared) == 1:
                from_col, to_col = shared[0], shared[0]
                key_desc = f"column {shared[0]!r}"
            else:
                key_caption = f"Blend Key ({', '.join(shared)})"
                key_field = "__blend_key__:" + "|".join(shared)
                for t in (a, b):
                    name = self.na.column(t, key_field, key_caption)
                    # `self.na.column()` is idempotent (same (table, key_field) -> same cached
                    # name), but `_append_calc_column` is NOT -- it unconditionally appends fresh
                    # TMDL lines. A table with 2+ blend partners that all share this exact
                    # `shared` column set (e.g. X blends with both Y and Z on the same
                    # Country+Issuer pair) reaches this loop once per partner, so without this
                    # guard `t`'s composite key gets written into its own TMDL TWICE under the
                    # identical name -- Desktop's own "collide -- the exact same name appears
                    # twice" load failure. One real corpus workbook (AP Product Portfolio
                    # Management Dashboard) hits exactly this shape.
                    if (t, name) in self.na.emitted:
                        continue
                    expr = " & \"|\" & ".join(dax_ref(t, c) for c in shared)
                    self._append_calc_column(
                        t, _calc_column(t, name, expr, "none", dtype="string"))
                    self.na.mark_emitted(t, name)
                    # Deliberately NOT counted in `self.stats["calc_columns"]` -- that stat feeds
                    # COVERAGE.md's "Calculated fields" ratio against `len(ir['tables'][*]['calcs'])`,
                    # a count of real TABLEAU source calcs. This column has no Tableau source at
                    # all; counting it there would inflate the coverage percentage with something
                    # the source never asked for -- exactly the "round a shortfall up" the
                    # project's own honesty rules forbid.
                    self.stats["blend_key_columns"] += 1
                from_col = self.na.lookup(b, key_field)[0]
                to_col = self.na.lookup(a, key_field)[0]
                key_desc = f"a calculated composite key over {', '.join(shared)}"
            relid = lineage("relationship", a, b, from_col, to_col)
            out.append(_line(0, f"relationship {relid}"))
            out.append(_line(1, f"fromColumn: {tmdl_ident(b)}.{tmdl_ident(from_col)}"))
            out.append(_line(1, f"toColumn: {tmdl_ident(a)}.{tmdl_ident(to_col)}"))
            out.append(_line(1, "fromCardinality: many"))
            out.append(_line(1, "toCardinality: many"))
            out.append(_line(1, "crossFilteringBehavior: bothDirections"))
            out.append("")
            self.relationship_pairs.append((a, b))
            self.stats["blend_relationships"] += 1
            self.notes.append(
                f"{a} <-> {b}: Tableau data blend across {len(ws_seen)} worksheet(s) "
                f"({ws_list}) -- related on {key_desc}, manyToMany/bothDirections because the "
                "real cardinality could not be verified against any real data (see "
                "relationships.tmdl)")

        # No comment header. A TMDL document must begin with an object declaration -- a leading
        # `//` line is line type "Other" and aborts the whole project. The reference's example for
        # this file shows `//` comments; it is wrong.
        return ("\n".join(out) + "\n") if out else ""


def write_semantic_model(ir, authority, out_dir, model_name, partitions="stub"):
    """Emit <out>/definition/**. Returns the model object for its stats/residue/notes."""
    m = TmdlModel(ir, authority, model_name, partitions)
    for rel, text in m.build().items():
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return m


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Asserts the two rules that a reader cannot verify by eye: tabs not spaces, and an expression
    # body one level deeper than the property that introduced it.
    got = _expr(2, "expression", "IF(\n1,\n2\n)")
    assert got[0] == "\t\texpression =", repr(got[0])
    assert got[1] == "\t\t\t\tIF(", repr(got[1])   # level 2 property -> level 4 body
    assert all(not ln.startswith(" ") for ln in got), "space indentation leaked in"
    # Relative structure must survive AS TABS. Preserving it as spaces is the bug the pre-flight
    # caught: TMDL uses tab depth to decide where a multi-line expression ends.
    nested = _expr(2, "source", "let\n    Source = 1\nin\n    Source")
    assert nested == ["\t\tsource =", "\t\t\t\tlet", "\t\t\t\t\tSource = 1", "\t\t\t\tin",
                      "\t\t\t\t\tSource"], nested
    assert not any(" " in ln[:len(ln) - len(ln.lstrip())] for ln in nested), "spaces in indent"
    assert _line(3, "x") == "\t\t\tx"
    assert m_ident("Region") == "Region"
    assert m_ident("Billable BID") == '#"Billable BID"', m_ident("Billable BID")
    assert m_ident('od"d') == '#"od""d"'
    assert lineage("table", "A") == lineage("table", "A"), "lineageTag is not stable"
    assert lineage("table", "A") != lineage("table", "B")

    # route(): RANK/RANK_DENSE with a resolved "Restart every" partition go to D+, but ONLY when
    # that field is a real column on the calc's OWN table -- ground truth: NGPO Dashboard New -
    # AP Version (MSC)'s `Rank - Topic Area`. Every other table calc, and RANK_UNIQUE/
    # RANK_MODIFIED/RANK_PERCENTILE even with the SAME resolved partition, still route to H.
    _rank_calc = {"kind": "tablecalc", "formula": "RANK(SUM([x]), 'desc')",
                  "tc_spec": {"ordering_type": "Field", "level_break_field": "Topic Area"}}
    assert route(_rank_calc, {"Topic Area", "Other Col"}) == "D+", (
        "RANK with a resolved same-table partition must route to D+")
    assert route(_rank_calc, {"Other Col"}) == "H", (
        "RANK's own level_break_field must be a real column on THIS table, not just resolved to "
        "some caption -- a field extraction placed on a different (sibling, post-split) table "
        "must still decline rather than guess which table's ALLEXCEPT to emit")
    assert route(_rank_calc) == "H", (
        "the OLD, conservative answer when no caller passes table_columns at all -- never a "
        "wrong D+ just because a caller (e.g. a stats-only check) did not wire this through")
    _generic_tc = {"kind": "tablecalc", "formula": "INDEX()", "tc_spec": None}
    assert route(_generic_tc, {"Topic Area"}) == "H", (
        "a table calc with no tc_spec at all is unaffected by this fix")
    _rank_no_field_addressing = {
        "kind": "tablecalc", "formula": "RANK(SUM([x]), 'desc')",
        "tc_spec": {"ordering_type": "Rows", "level_break_field": None}}
    assert route(_rank_no_field_addressing, {"Topic Area"}) == "H", (
        "the generic 'Rows' default (Tableau's own Table-Down addressing, genuinely no "
        "partition) must still decline -- this is the common case across the real corpus")
    for fn in ("RANK_UNIQUE", "RANK_MODIFIED", "RANK_PERCENTILE"):
        _other_rank = {"kind": "tablecalc", "formula": f"{fn}([x])",
                        "tc_spec": {"ordering_type": "Field", "level_break_field": "Topic Area"}}
        assert route(_other_rank, {"Topic Area"}) == "H", (
            f"{fn} must stay on tier H even with a resolved partition -- no exact DAX tie-break "
            f"equivalent (see route()'s own docstring)")
    print("tmdl.py self-test OK (route(): RANK/RANK_DENSE with a resolved same-table partition "
          "-> D+; RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE and unresolved addressing stay H)")

    # See _param_literal's docstring: a boolean parameter domain value is a `str` ("true"/"false")
    # straight out of decode_param_value(), and dtype must win over that string's own truthiness.
    assert _param_literal("1", "int64") == "1"
    assert _param_literal("true", "boolean") == "true"
    assert _param_literal("0", "boolean") == "false", (
        "a boolean domain value must resolve from dtype, not str('0') truthiness (always True)")
    assert _param_literal("Actual", "string") == '"Actual"'

    # A quoted string does NOT satisfy a `type table [Value = datetime]` ascription -- confirmed
    # against real Desktop: "Expression.Error: The type of the value does not match the type of
    # the column." A date/dateTime parameter value must become a real M #datetime(...) literal.
    assert _param_literal("2026-05-28", "date") == "#datetime(2026, 5, 28, 0, 0, 0)"
    assert _param_literal("2026-05-28 14:30:00", "dateTime") == "#datetime(2026, 5, 28, 14, 30, 0)"
    assert _param_literal("not-a-date", "date") == '"not-a-date"', (
        "unparseable date text must fall back to the old (still-broken) quoted string, not crash "
        "or guess a date out of it")

    # TOM's DataType enum has no "date" member, only "dateTime" -- confirmed against real
    # Desktop: "Failed to convert the value 'date' to the expected type DataType!", a hard
    # model-load failure for the whole project. docs/schema-reference/pbip-schema.md's own
    # "Column Data Types" table claims `date` is valid; it is not -- Desktop is the authority.
    assert _tom_dtype("date") == "dateTime"
    assert _tom_dtype("dateTime") == "dateTime"
    assert _tom_dtype("int64") == "int64", "must not touch non-date types"
    assert "date" not in M_TYPE, "M_TYPE must never be consulted with the raw 'date' string"

    # Aggregation propagates through a reference chain. Tableau treats a calc that references an
    # aggregate as aggregate too. Missing this emitted a calculated COLUMN referencing a MEASURE --
    # DAX forbids it, and Desktop reported it as "a circular dependency" between two calcs that
    # never referenced each other. 100 real calcs across the corpus had this shape.
    agg_t = {"calcs": [
        {"name": "base", "tableau_name": "[base]", "formula": "SUM([x])/SUM([y])"},
        {"name": "flag", "tableau_name": "[flag]", "formula": "IF [base] > 0 THEN 1 END"},
        {"name": "wrap", "tableau_name": "[wrap]", "formula": "IF [flag] = 1 THEN 'y' END"},
        {"name": "plain", "tableau_name": "[plain]", "formula": "[x] + [y]"},
    ]}
    m = TmdlModel({"tables": []}, None, "X")
    agg = m._aggregate_map(agg_t)
    assert agg["base"] and agg["flag"] and agg["wrap"], "aggregate status did not propagate"
    assert not agg["plain"], "a non-aggregate calc was wrongly classified as aggregate"

    # A parameter reference is the SAME hazard: resolve_param always points at the parameter's
    # SELECTEDVALUE measure, so a calc referencing [Parameters].[X] must become a measure too, even
    # with no aggregate function in sight. Missing this shipped a real workbook (WorkbookB Dashboard)
    # with 'cl.Is Selected Slice By?' as a calculated COLUMN whose DAX referenced a parameter's
    # measure -- the model loaded, but every report filter on that column failed in Desktop with
    # "Broken_Filters" (confirmed by elimination: every other filter in that report targets a plain
    # source column and loaded fine).
    param_t = {"calcs": [
        {"name": "flag", "tableau_name": "[flag]",
         "formula": 'IF [Parameters].[Parameter 1] = "x" THEN TRUE ELSE FALSE END'},
        {"name": "wrap", "tableau_name": "[wrap]", "formula": "IF [flag] = TRUE THEN 1 END"},
        {"name": "plain", "tableau_name": "[plain]", "formula": "[x] + [y]"},
    ]}
    agg2 = m._aggregate_map(param_t)
    assert agg2["flag"], "a calc referencing [Parameters].[X] must be treated as an aggregate"
    assert agg2["wrap"], "parameter-aggregate status did not propagate to a dependent calc"
    assert not agg2["plain"], "a calc with no parameter/aggregate reference was wrongly flagged"

    # Groups and bins share the hazard: a group/bin has no formula of its own, only a `source`
    # field, so it can ONLY inherit aggregate status by propagation -- never seed it. A group
    # built on a parameter-dependent calc, or a bin built on a plain field, must be told apart.
    gb_t = {
        "calcs": [{"name": "flag", "tableau_name": "[flag]",
                   "formula": 'IF [Parameters].[Parameter 1] = "x" THEN TRUE ELSE FALSE END'}],
        "groups": [{"name": "grp", "tableau_name": "[grp]", "source": "flag",
                    "mapping": {"y": ["1"]}, "datatype": "string"},
                   {"name": "grp2", "tableau_name": "[grp2]", "source": "plain_field",
                    "mapping": {"y": ["1"]}, "datatype": "string"}],
        "bins": [{"name": "bin1", "tableau_name": "[bin1]", "source": "flag", "size": "5",
                  "datatype": "integer"}],
    }
    agg3 = m._aggregate_map(gb_t)
    assert agg3["grp"], "a group built on a parameter-dependent calc must become a measure too"
    assert agg3["bin1"], "a bin built on a parameter-dependent calc must become a measure too"
    assert not agg3["grp2"], "a group on an ordinary field was wrongly classified as aggregate"

    # CROSS-table reference: after split_federated_datasource homes an unconstrained (e.g.
    # parameter-only) calc on the primary table and a DIFFERENT calc that references it on a
    # sibling table, the referencing calc must be promoted too -- exactly the same hazard as
    # `agg_t`/`param_t` above, one table apart. Missing this shipped 10 real fields in WorkbookB
    # Dashboard as calculated columns referencing a measure on a sibling table; Desktop rejected
    # all of them at load ("references measure ... DAX forbids a column referencing a measure").
    cross_a = {"calcs": [
        {"name": "referencer", "tableau_name": "[referencer]",
         "formula": "IF [param_calc] = TRUE THEN [x] END"},
        {"name": "unrelated", "tableau_name": "[unrelated]", "formula": "[x] + [y]"},
    ]}
    cross_b = {"calcs": [
        {"name": "param_calc", "tableau_name": "[param_calc]",
         "formula": 'IF [Parameters].[Parameter 1] = "x" THEN TRUE ELSE FALSE END'},
    ]}
    m_cross = TmdlModel({"tables": [cross_a, cross_b]}, None, "X")
    agg_cross_a = m_cross._aggregate_map(cross_a)
    agg_cross_b = m_cross._aggregate_map(cross_b)
    assert agg_cross_b["param_calc"], "sibling table's own parameter-dependent calc must be aggregate"
    assert agg_cross_a["referencer"], \
        "a calc referencing a parameter-dependent calc on a DIFFERENT table must be promoted too"
    assert not agg_cross_a["unrelated"], \
        "a calc with no cross-table reference was wrongly promoted"

    # Promoting a calc to a measure fixes the column-references-measure hazard above, but a pure
    # branch-selection formula (no aggregate function of its own) has bare column references that
    # relied on a calculated COLUMN's implicit row context -- a measure has none. Confirmed on a
    # real workbook (WorkbookB Dashboard, 'cl.Slice By'): promoting it was necessary but surfaced a
    # SECOND real Desktop error, "A single value for column '...' cannot be determined... This can
    # happen when a measure or function formula refers to a column that contains many values
    # without specifying an aggregation" (Underlying Error: Missing_References). `resolve_measure_safe`
    # wraps a plain COLUMN resolution in SELECTEDVALUE and leaves a MEASURE resolution bare --
    # wrapping a measure would itself be invalid DAX (SELECTEDVALUE's argument must be a column).
    rt = {"tables": [
        {"tableau_name": "T", "caption": "T", "provenance": "test",
         "columns": [{"name": "Region", "tableau_name": "[Region]", "datatype": "string",
                      "role": "dimension", "source_column": "Region", "hidden": False}],
         "calcs": [{"name": "picked", "tableau_name": "[picked]", "datatype": "string",
                    "kind": "formula",
                    "formula": 'IF [Parameters].[P] = "x" THEN [Region] ELSE [Region] END'}],
         "groups": [], "bins": [], "aliases": {}}],
        "worksheets": [], "parameters": []}
    na2 = NameAuthority()
    m2 = TmdlModel(rt, na2, "X")
    m2.declined = {}
    m2.allocate()
    pbi_t = m2.table_order[0][0]
    resolve, resolve_param, _rt2, resolve_measure_safe, _rk2, _rf2 = m2._resolver(pbi_t)
    assert resolve_measure_safe("Region") == f"SELECTEDVALUE('{pbi_t}'[Region])", (
        "a plain column reference must be SELECTEDVALUE-wrapped for a measure-bound formula")
    assert resolve("Region") == f"'{pbi_t}'[Region]", (
        "the ordinary resolver must stay unwrapped -- only the measure-safe one wraps"
    )

    # `_shelf_agg_only`: a calc promoted to a measure PURELY by a parameter reference (no
    # aggregate of its own) but used EXCLUSIVELY via `SUM(<calc>)` on its own worksheet shelf
    # must be SUMX-inlined, not SELECTEDVALUE-wrapped -- SELECTEDVALUE returns BLANK the instant
    # more than one real row exists, exactly the WorkbookA "Card Penetration" bug (see that method's
    # own docstring for the full real-corpus story).
    shelf_t = {
        "tableau_name": "ShelfT", "caption": "ShelfT", "provenance": "test",
        "columns": [{"name": "val", "tableau_name": "[val]", "datatype": "real",
                     "role": "dimension", "source_column": "val", "hidden": False}],
        "calcs": [{"name": "Penetration", "tableau_name": "[Penetration]", "datatype": "real",
                   "kind": "formula", "formula": "[val] / [Parameters].[Denom]"}],
        "groups": [], "bins": [], "aliases": {},
    }
    shelf_ir = {"tables": [shelf_t],
                "worksheets": [{"name": "w", "encodings": [
                    {"channel": "text", "field": "Penetration", "agg": "sum",
                     "table_calc": None}]}],
                "parameters": [{"name": "Denom", "tableau_name": "[Denom]", "aliases": []}]}
    na3 = NameAuthority()
    m3 = TmdlModel(shelf_ir, na3, "X")
    m3.declined = {}
    m3.allocate()
    m3.resolve_calcs()
    pbi_shelf = m3.table_order[0][0]
    got_shelf = m3.calc_dax[(pbi_shelf, "Penetration")]
    want_shelf = f"SUMX('{pbi_shelf}', DIVIDE('{pbi_shelf}'[val], [Denom Selected]))"
    assert got_shelf == want_shelf, f"got {got_shelf!r}, want {want_shelf!r}"
    print(f"  OK  a parameter-promoted measure used only via a SUM() shelf encoding is "
          f"SUMX-inlined, not SELECTEDVALUE-wrapped -> {got_shelf}")

    # No regression: the SAME calc with no worksheet usage at all (or used as a plain dimension
    # reference, `agg=None`) must still take the old SELECTEDVALUE path -- `_shelf_agg_only`
    # must not fire just because a calc happens to be promoted to a measure.
    shelf_ir_dim = {"tables": [shelf_t],
                    "worksheets": [{"name": "w", "encodings": [
                        {"channel": "rows", "field": "Penetration", "agg": None,
                         "table_calc": None}]}],
                    "parameters": [{"name": "Denom", "tableau_name": "[Denom]", "aliases": []}]}
    na3b = NameAuthority()
    m3b = TmdlModel(shelf_ir_dim, na3b, "X")
    m3b.declined = {}
    m3b.allocate()
    m3b.resolve_calcs()
    pbi_shelf_b = m3b.table_order[0][0]
    got_dim = m3b.calc_dax[(pbi_shelf_b, "Penetration")]
    assert "SELECTEDVALUE" in got_dim, (
        f"a calc used as a plain dimension reference must keep the SELECTEDVALUE-wrap, no "
        f"regression -- got {got_dim!r}")
    print(f"  OK  the same calc used as a plain dimension reference (agg=None) is unaffected, "
          f"still SELECTEDVALUE-wrapped -> {got_dim}")

    # `cross_table_refs` (set by `split_federated_datasource` when it found a RELATED()-legal hub
    # for a calc that mixes columns from more than one physical Tableau object -- see extract.py's
    # own self-tests) must actually produce RELATED()/SELECTEDVALUE() DAX here, not just be
    # recorded and ignored. RELATED() needs row context (calculated COLUMN); SELECTEDVALUE() is
    # the measure-safe equivalent, matching `resolve_measure_safe`'s own convention above.
    related_many = {
        "tableau_name": "Many", "caption": "Many", "provenance": "test",
        "columns": [{"name": "Key", "tableau_name": "[key]", "datatype": "string",
                     "role": "dimension", "source_column": "key", "hidden": False}],
        "calcs": [
            {"name": "ColRef", "tableau_name": "[ColRef]", "datatype": "string",
             "kind": "formula", "formula": "[dim_col]",
             "cross_table_refs": {"dim_col": "One"}},
            {"name": "MeasureRef", "tableau_name": "[MeasureRef]", "datatype": "string",
             "kind": "formula",
             "formula": 'IF [Parameters].[Parameter 1] = "x" THEN [dim_col] ELSE [dim_col] END',
             "cross_table_refs": {"dim_col": "One"}},
            {"name": "SameTable", "tableau_name": "[SameTable]", "datatype": "string",
             "kind": "formula", "formula": "[key]"},
            {"name": "BadCross", "tableau_name": "[BadCross]", "datatype": "string",
             "kind": "formula", "formula": "[measure_col]",
             "cross_table_refs": {"measure_col": "One"}},
        ],
        "groups": [], "bins": [], "aliases": {},
    }
    related_one = {
        "tableau_name": "One", "caption": "One", "provenance": "test",
        "columns": [{"name": "DimCol", "tableau_name": "[dim_col]", "datatype": "string",
                     "role": "dimension", "source_column": "dim_col", "hidden": False}],
        "calcs": [{"name": "MeasureCol", "tableau_name": "[measure_col]", "datatype": "string",
                   "kind": "formula", "formula": "SUM([dim_col])",
                   "cross_table_refs": {"measure_col": "one_self_ref_never_used"}}],
        "groups": [], "bins": [], "aliases": {},
    }
    related_ir = {"tables": [related_many, related_one], "worksheets": [],
                  "parameters": [{"name": "P", "tableau_name": "[Parameter 1]", "aliases": []}]}
    na_rel = NameAuthority()
    na_rel.parameter("P")
    m_rel = TmdlModel(related_ir, na_rel, "X")
    m_rel.allocate()
    m_rel.resolve_calcs()
    many_pbi = next(pbi for pbi, t in m_rel.table_order if t["tableau_name"] == "Many")
    one_pbi = next(pbi for pbi, t in m_rel.table_order if t["tableau_name"] == "One")
    assert m_rel.calc_dax.get((many_pbi, "ColRef")) == f"RELATED('{one_pbi}'[DimCol])", (
        f"a calculated COLUMN's cross-table reference must be RELATED()-wrapped: "
        f"{m_rel.calc_dax.get((many_pbi, 'ColRef'))!r}")
    assert (many_pbi, "ColRef") not in m_rel.declined, m_rel.declined.get((many_pbi, "ColRef"))
    measure_dax = m_rel.calc_dax.get((many_pbi, "MeasureRef"))
    assert measure_dax is not None and "SELECTEDVALUE(" in measure_dax \
        and "RELATED(" not in measure_dax, (
        f"a calc promoted to a MEASURE has no row context -- its cross-table reference must be "
        f"SELECTEDVALUE()-wrapped, never RELATED(): {measure_dax!r}")
    assert m_rel.calc_dax.get((many_pbi, "SameTable")) == f"'{many_pbi}'[Key]", (
        "a same-table reference must be completely unaffected by the cross_table_refs machinery: "
        f"{m_rel.calc_dax.get((many_pbi, 'SameTable'))!r}")
    assert (many_pbi, "BadCross") in m_rel.declined, (
        "a cross_table_refs entry that resolves to a MEASURE on the other table (not a plain "
        "column) must decline rather than emit RELATED()/SELECTEDVALUE() of a measure")
    print("tmdl.py self-test OK (cross_table_refs resolves via RELATED() for a calculated column, "
          "SELECTEDVALUE() for a promoted measure, same-table refs unaffected, a measure-shaped "
          "cross-reference still declines)")

    # A genuine A<->B cycle is a different failure: an undefined calculation in the SOURCE, not a
    # translation gap. It must decline, never reach TMDL.
    cyc = TmdlModel._find_cycles({"calcs": [
        {"name": "A", "tableau_name": "[A]", "formula": "[B] + 1"},
        {"name": "B", "tableau_name": "[B]", "formula": "[A] * 2"},
        {"name": "C", "tableau_name": "[C]", "formula": "SUM([x])"},
        {"name": "D", "tableau_name": "[D]", "formula": "[D] + 1"},
    ]})
    assert "A" in cyc and "B" in cyc, "mutual A<->B reference not detected as a cycle"
    assert "C" not in cyc, "an acyclic calc was wrongly flagged as circular"
    assert "D" in cyc, "a self-referencing calc was not detected as a cycle"

    # Regression for the VRM Compliance Dashboard bug: two DIFFERENT source tables (different
    # provenance, different connections -- here two different .hyper extracts) that both carry
    # tableau_name=None and the SAME generic caption "Data" must never be silently collapsed onto
    # one pbi table. Before the fix, `NameAuthority.table()`'s `if key in self.tables: return
    # self.tables[key]` treated the second allocation request as a re-lookup of the first table,
    # and `build()`'s `self.files[f"definition/tables/{name}.tmdl"] = ...` then let the second
    # table's emission silently overwrite the first's file -- an entire table (and every column,
    # measure and residue entry in it) vanished with no error and no residue entry at all.
    # `_assert_no_table_collision()` must catch this pre-emit, before either table reaches disk.
    collide_ir = {"tables": [
        {"tableau_name": None, "caption": "Data", "provenance": "tds:MSD_Billing_Details.tdsx",
         "connections": [{"class": "hyper", "dbname": "Data/Extracts/MSD_Billing_Details.hyper",
                          "is_proxy": False, "is_engine": True}],
         "columns": [], "calcs": [], "groups": [], "bins": [], "aliases": {}},
        {"tableau_name": None, "caption": "Data",
         "provenance": "tds:MSD_Case_Details_Compliance.tdsx",
         "connections": [{"class": "hyper",
                          "dbname": "Data/Extracts/MSD_Case_Details_Compliance.hyper",
                          "is_proxy": False, "is_engine": True}],
         "columns": [], "calcs": [], "groups": [], "bins": [], "aliases": {}},
    ], "worksheets": [], "parameters": []}
    na3 = NameAuthority()
    m3 = TmdlModel(collide_ir, na3, "X")
    try:
        m3.allocate()
    except AssertionError as e:
        assert "collision" in str(e), f"wrong exception raised for a real collision: {e}"
    else:
        raise AssertionError(
            "two DIFFERENT source tables sharing tableau_name=None/caption='Data' must raise a "
            "table allocation collision -- allocate() silently let them merge")

    # Regression for the NGPO Dashboard New - AP Version (MSC) InvalidUnconstrainedJoin bug --
    # `_relationships`' blend-key detection, corpus-agnostic (nothing below names any real
    # workbook table). Four tables, no <object-graph>/physical join at all (a real Tableau data
    # blend has none):
    #   Fact <-> Dim1: share column 'Key' -- worksheets WS1 (x3) put Dim1's own 'Label' and Fact's
    #     own 'Amount' on the SAME view -- a real, observed blend with a real key.
    #   Fact <-> Dim2: share NO column at all -- worksheet WS2 blends them anyway (WS2 puts
    #     Dim2's 'Other' next to Fact's 'Amount') -- must be declined, not guessed at.
    #   Dim1 <-> Dim3, Fact <-> Dim3: BOTH share column 'Key' too, and BOTH are genuinely blended
    #     (WS3 x2 for Dim1<->Dim3, WS4 x1 for Fact<->Dim3) -- but Fact-Dim1-Dim3 would already be
    #     one connected component once Fact<->Dim1 and Dim1<->Dim3 are both emitted, so the THIRD
    #     edge (Fact<->Dim3) must be declined as an ambiguous/circular path (SM-STRUCT-08), not
    #     emitted as a second parallel path Desktop would refuse to load. Priority is worksheet
    #     count, so Dim1<->Dim3 (2 worksheets) must beat Fact<->Dim3 (1) for the shared edge.
    def _tbl(name, cols):
        return {"tableau_name": name, "caption": name, "provenance": "test",
                "columns": [{"name": c, "tableau_name": f"[{c}]", "datatype": "string",
                             "role": "dimension", "source_column": c, "hidden": False}
                            for c in cols],
                "calcs": [], "groups": [], "bins": [], "aliases": {}, "hierarchies": [],
                "connections": []}

    def _ws(name, a_field, b_field):
        return {"name": name, "mark": "Automatic",
                "shelves": {"rows": [{"field": a_field, "agg": None}],
                           "cols": [{"field": b_field, "agg": None}], "pages": []},
                "encodings": [], "filters": []}

    blend_ir = {
        "tables": [
            _tbl("Fact", ["Key", "Amount"]),
            _tbl("Dim1", ["Key", "Label"]),
            _tbl("Dim2", ["Other"]),
            _tbl("Dim3", ["Key", "Extra"]),
        ],
        "worksheets": (
            [_ws("WS1", "Label", "Amount")] * 3
            + [_ws("WS2", "Other", "Amount")]
            + [_ws("WS3", "Extra", "Label")] * 2
            + [_ws("WS4", "Extra", "Amount")]
        ),
        "parameters": [],
    }
    na_blend = NameAuthority()
    m_blend = TmdlModel(blend_ir, na_blend, "Blend")
    m_blend.build()
    edges = {frozenset(e) for e in m_blend.relationship_pairs}
    ok = (frozenset(("Fact", "Dim1")) in edges
          and frozenset(("Dim1", "Dim3")) in edges
          and frozenset(("Fact", "Dim3")) not in edges
          and frozenset(("Fact", "Dim2")) not in edges
          and len(edges) == 2)
    if not ok:
        raise AssertionError(f"blend relationship detection wrong: edges={edges}")
    rels_text = m_blend.files.get("definition/relationships.tmdl", "")
    assert "fromCardinality: many" in rels_text and "toCardinality: many" in rels_text, (
        "a blend relationship must be manyToMany -- real cardinality is unverifiable against "
        "any real data, and asserting oneToMany anyway is exactly what SM-STRUCT-07 forbids")
    assert any(i["tier"] == "GAP" and "Dim2" in (i["table"] + str(i["subject"]))
               for i in m_blend.residue.items), (
        "a blend pair with no shared column must be reported as GAP residue, not silently dropped"
    )
    assert any("Fact" in n and "Dim3" in n and "ambiguous" in n for n in m_blend.notes), (
        "the declined THIRD edge that would close a cycle must be explained in a note, not "
        "silently skipped -- it is not a real gap (the path already exists), just not a direct one"
    )
    print(f"  OK   blend relationship detection: 2 edges emitted ({sorted(edges)}), Fact<->Dim2 "
          f"declined (no shared column), Fact<->Dim3 declined (would close a cycle)")

    # Regression for the "Digital Sales - Leads Dashboard" missing-relationship Desktop bug --
    # real KEY-BASED joins (resolved_joins), not a blend. "Global" is a MULTI-SPOKE hub (two
    # different many-side tables, "Meetings" and "Opportunity", both join to it) and must get
    # `crossFilteringBehavior: bothDirections` on both its relationships -- a flat table combining
    # raw columns from both spokes, filtered by a field on either, needs to filter
    # fact -> dimension -> fact, which Desktop's default single direction cannot do. "Dim2" is a
    # single-spoke hub (only "Fact2" joins to it, the ordinary star-schema shape "WorkbookB
    # Dashboard"'s real `Insights` fact/13-dimension model already has) and must be LEFT at
    # Desktop's default single direction -- turning on bothDirections where it is not needed only
    # adds the well-documented risk of bidirectional relationships changing filter context in
    # unrelated measures. Cardinality is explicit and real (`many`/`one`) on every one of these,
    # never `manyToMany` like the blend case above -- resolved_joins only exists because Tableau's
    # OWN metadata already declared one side unique (see split_federated_datasource()'s own
    # docstring, point 3), unlike a blend, which has no such evidence.
    hub_ir = {
        "tables": [
            _tbl("Global", ["Key"]),
            _tbl("Meetings", ["MeetingKey"]),
            _tbl("Opportunity", ["OppKey"]),
            _tbl("Dim2", ["D2Key"]),
            _tbl("Fact2", ["F2Key"]),
        ],
        "worksheets": [],
        "parameters": [],
        "resolved_joins": [
            {"one_table": "Global", "many_table": "Meetings",
             "one_field": "Key", "many_field": "MeetingKey"},
            {"one_table": "Global", "many_table": "Opportunity",
             "one_field": "Key", "many_field": "OppKey"},
            {"one_table": "Dim2", "many_table": "Fact2",
             "one_field": "D2Key", "many_field": "F2Key"},
        ],
    }
    na_hub = NameAuthority()
    m_hub = TmdlModel(hub_ir, na_hub, "Hub")
    m_hub.build()
    hub_edges = {frozenset(e) for e in m_hub.relationship_pairs}
    assert hub_edges == {frozenset(("Global", "Meetings")), frozenset(("Global", "Opportunity")),
                          frozenset(("Dim2", "Fact2"))}, hub_edges
    hub_rels_text = m_hub.files.get("definition/relationships.tmdl", "")
    hub_blocks = [b for b in hub_rels_text.split("\n\n") if b.strip()]
    both_blocks = [b for b in hub_blocks if "bothDirections" in b]
    single_blocks = [b for b in hub_blocks if "bothDirections" not in b]
    assert len(both_blocks) == 2, f"expected 2 bothDirections relationships, got: {hub_blocks}"
    assert all("Global" in b and ("Meetings" in b or "Opportunity" in b) for b in both_blocks), (
        f"bothDirections must land on the multi-spoke hub's OWN relationships only: {both_blocks}"
    )
    assert len(single_blocks) == 1 and "Dim2" in single_blocks[0] and "Fact2" in single_blocks[0], (
        f"single-spoke Dim2<->Fact2 must be left at Desktop's default direction: {single_blocks}"
    )
    assert "fromCardinality: many" in hub_rels_text and "toCardinality: one" in hub_rels_text, (
        "every key-based join has a REAL verified one/many side -- cardinality must be explicit, "
        "not left to Desktop's own (dummy-row-count-unreliable) inference"
    )
    assert "manyToMany" not in hub_rels_text and "fromCardinality: many\n\ttoCardinality: many" \
        not in hub_rels_text, "a key-based join must never be manyToMany -- that is the blend " \
        "fallback for when cardinality is genuinely unverifiable, not this case"
    assert m_hub.stats.get("bidirectional_hub_relationships") == 2, m_hub.stats
    print(f"  OK   multi-spoke hub gets bothDirections (2), single-spoke hub does not (1): "
          f"{len(hub_blocks)} key-based relationship(s) emitted")

    # _measure/_calc_column: format_string, when given, sits BEFORE lineageTag (measure) or
    # right after dataType (column) -- exact order confirmed against
    # docs/examples/GroundTruth.SemanticModel/definition/tables/Sales Data.tmdl's own
    # Desktop-authored measure/column. Omitted (None) must change nothing (no regression).
    m_fmt = _measure("T", "Pct", "SUM('T'[x])", format_string="0.00%")
    assert m_fmt[0] == "\tmeasure Pct = SUM('T'[x])"
    assert m_fmt[1].strip() == "formatString: 0.00%"
    assert m_fmt[2].strip().startswith("lineageTag:")
    m_nofmt = _measure("T", "Plain", "SUM('T'[x])")
    assert not any("formatString" in ln for ln in m_nofmt), (
        "omitting format_string must emit no formatString line at all -- no regression from "
        "this pipeline's own established 'never guess a format' rule"
    )

    c_fmt = _calc_column("T", "PctCol", "'T'[x] / 'T'[y]", "sum", dtype="double",
                         format_string="0.00%")
    assert c_fmt[1].strip() == "dataType: double"
    assert c_fmt[2].strip() == "formatString: 0.00%"
    assert c_fmt[3].strip().startswith("lineageTag:")
    c_nofmt = _calc_column("T", "PlainCol", "'T'[x] / 'T'[y]", "sum", dtype="double")
    assert not any("formatString" in ln for ln in c_nofmt)
    print("  OK   _measure/_calc_column: format_string line ordering matches Desktop's own "
          "GroundTruth example, omitted -> no regression")

    # _plan_shelf_measures: FIXED LOD column -- SUM()/AVERAGE() must become MAX(), not
    # multiply the per-group value by (rows per group).  Regression: Usage Deepdive's
    # `issr-spend-l6 (copy)` column was CALCULATE(SUM(...), ALLEXCEPT(...)), so the
    # `SUM of issr-spend-l6 (copy)` shelf measure re-summed a group total across every row
    # that shared the same key, inflating the result by the number of rows per group.
    lod_tbl = {
        "tableau_name": "Deepdive", "caption": "Deepdive", "provenance": "test",
        "columns": [
            {"name": "key", "tableau_name": "[key]", "datatype": "string",
             "role": "dimension", "source_column": "key", "hidden": False},
            {"name": "spend", "tableau_name": "[spend]", "datatype": "double",
             "role": "measure", "source_column": "spend", "hidden": False},
        ],
        "calcs": [
            {"name": "spend-l6 (copy)", "tableau_name": "[spend_l6_copy_123]",
             "datatype": "double", "kind": "lod",
             "formula": "{FIXED [key]: SUM([spend])}"},
        ],
        "groups": [], "bins": [], "aliases": {},
    }
    lod_ir = {
        "tables": [lod_tbl],
        "worksheets": [{"name": "ws1", "encodings": [
            # SUM of the FIXED LOD column -- must become MAX(...)
            {"channel": "text", "field": "spend-l6 (copy)", "agg": "sum",
             "table_calc": None},
            # SUM of a plain physical column -- must stay SUM(...)
            {"channel": "size", "field": "spend", "agg": "sum", "table_calc": None},
        ], "measure_names_pivot": None}],
        "parameters": [],
    }
    na_lod = NameAuthority()
    m_lod = TmdlModel(lod_ir, na_lod, "LodTest")
    m_lod.declined = {}
    m_lod.allocate()
    pbi_dd = m_lod.table_order[0][0]
    plan_entries = m_lod.shelf_plan.get(pbi_dd, [])
    lod_entry = next((e for e in plan_entries if e[2] == "spend-l6 (copy)"), None)
    assert lod_entry is not None, (
        "shelf measure for the FIXED LOD column must be planned -- entry missing")
    assert lod_entry[1] == f"MAX('{pbi_dd}'[spend-l6 (copy)])", (
        f"SUM of a FIXED LOD column must become MAX(...), not SUM -- got {lod_entry[1]!r}")
    plain_entry = next((e for e in plan_entries if e[2] == "spend"), None)
    assert plain_entry is not None, (
        "shelf measure for the plain physical column must still be planned -- entry missing")
    assert plain_entry[1] == f"SUM('{pbi_dd}'[spend])", (
        f"SUM of a normal column must stay SUM, no regression -- got {plain_entry[1]!r}")
    print("  OK   FIXED LOD column shelf measure: SUM -> MAX (not re-aggregating a "
          "per-group column); plain column SUM unaffected")

    # _plan_shelf_measures: same column caption in MULTIPLE tables must each get their own
    # shelf measure -- not just the first-match winner. Real failure: `Issr Actv Crd Cy` exists
    # as a plain column in both `Key Highlights` and `Usage Overview`; the old single-match
    # `by_spelling` call allocated `SUM of Issr Actv Crd Cy` only under Key Highlights, leaving
    # Usage Overview with no shelf measure; `_resolve_field` found only the raw column there and
    # routed it to Category instead of Y, breaking the lineChart's second data series.
    def _shared_col_tbl(name, col_name):
        return {"tableau_name": name, "caption": name, "provenance": "test",
                "columns": [{"name": col_name, "tableau_name": f"[{col_name}]",
                             "datatype": "double", "role": "measure",
                             "source_column": col_name, "hidden": False}],
                "calcs": [], "groups": [], "bins": [], "aliases": {}}
    shared_ir = {
        "tables": [
            _shared_col_tbl("Table A", "shared_metric"),
            _shared_col_tbl("Table B", "shared_metric"),
        ],
        "worksheets": [
            {"name": "ws_a", "encodings": [
                {"channel": "text", "field": "shared_metric", "agg": "sum",
                 "table_calc": None}],
             "measure_names_pivot": None},
            {"name": "ws_b", "encodings": [
                {"channel": "text", "field": "shared_metric", "agg": "sum",
                 "table_calc": None}],
             "measure_names_pivot": None},
        ],
        "parameters": [],
    }
    na_sh = NameAuthority()
    m_sh = TmdlModel(shared_ir, na_sh, "SharedTest")
    m_sh.declined = {}
    m_sh.allocate()
    pbi_a = next(pbi for pbi, t in m_sh.table_order if t["tableau_name"] == "Table A")
    pbi_b = next(pbi for pbi, t in m_sh.table_order if t["tableau_name"] == "Table B")
    entries_a = m_sh.shelf_plan.get(pbi_a, [])
    entries_b = m_sh.shelf_plan.get(pbi_b, [])
    assert any(e[2] == "shared_metric" for e in entries_a), (
        "Table A must have a shelf measure for shared_metric -- first-match table must still "
        f"be covered; shelf_plan[{pbi_a!r}] = {entries_a}")
    assert any(e[2] == "shared_metric" for e in entries_b), (
        "Table B must ALSO have its own shelf measure for shared_metric -- the second table "
        "sharing the same column caption was silently dropped by the old first-match path; "
        f"shelf_plan[{pbi_b!r}] = {entries_b}")
    assert na_sh.lookup(pbi_a, f"__shelf__sum__shared_metric") != (None, None), (
        f"na.lookup for Table A's shelf measure must succeed after allocate()")
    assert na_sh.lookup(pbi_b, f"__shelf__sum__shared_metric") != (None, None), (
        f"na.lookup for Table B's shelf measure must succeed after allocate()")
    print("  OK   _plan_shelf_measures: same caption in two tables -> each gets its own "
          "independent shelf measure (multi-table allocation fix)")

    print("tmdl.py self-test OK "
          f"({len(got)} expression lines, tabs verified, lineageTag stable, "
          f"aggregation propagation OK, cycle detection OK, table-collision backstop OK, "
          f"blend-relationship detection OK, formatString threading OK, "
          f"FIXED LOD shelf MAX substitution OK, "
          f"multi-table shared-caption shelf allocation OK)")
