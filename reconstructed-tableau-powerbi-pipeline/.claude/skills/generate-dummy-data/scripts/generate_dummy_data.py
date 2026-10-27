#!/usr/bin/env python3
"""
Wire deterministic, schema-matched synthetic data into a build-tableau-pbip output's empty
stub partitions -- and, optionally (`--realistic`), sample REAL values out of the source
workbook's own embedded Tableau (.hyper) extracts wherever one can be resolved, falling back to
the fully-synthetic generator everywhere it can't.

Why this exists
----------------
A user manually tested a generated PBIP project (LAC Pedidosya Transaction Profile, table
`gbo_lac_monthly.pedi`, 72 columns) by pointing its stub M partition at a hand-built Excel file.
That file didn't match the model's real column names/types, requiring extensive manual
reconciliation (renames + 59 placeholder-null columns) and hit one real Desktop bug along the
way: `Excel.Workbook(File.Contents(path), true)` already promotes headers, so an extra
`Table.PromoteHeaders` call after it corrupts the column names. This script exists so that class
of manual work and that class of bug never has to happen again: it generates an Excel file whose
column names/types match the model EXACTLY, by construction, straight from the TMDL the build
already emitted.

What it does, per project
--------------------------
1. Discover every table's `partition ... = m` block under `<Workbook>.SemanticModel/definition/
   tables/*.tmdl`.
2. Classify each table's `source =` M body:
     - "stub"   : `Table.FromRows({}, type table [...])` -- the `{}` (zero literal rows) is the
                  signal. Eligible for synthetic wiring. Column (name, M-type) pairs are read
                  straight out of the `type table [...]` ascription.
     - "param"  : `Table.FromRows({{...}, ...})` -- a Param table's real domain values. Skipped,
                  always, and reported as skipped -- these are never synthetic.
     - "native" : anything else with a `partition ... = m` block (Excel.Workbook, Sql.Database,
                  ... from `--partitions native`). Skipped -- never overwrite real data.
     - no partition at all: skipped, reported.
3. For each "stub" table, generate rows for every column, typed and (absent `--realistic`, or
   wherever `--realistic` can't resolve a real source) DERIVED FROM THE COLUMN'S OWN NAME so a
   value is traceable back to its column in Desktop:
     text        -> "<ColumnName> <i>"           (i = 1..N, distinct per row)
     Int64.Type  -> 1000 + i
     number      -> round(100.0 + i * 1.5, 2)
     datetime    -> 2024-01-01 + i days
     logical     -> True on odd i, False on even i
   Fully deterministic: no Date.now()/random anywhere. Same input -> byte-identical output.
4. Write ONE Excel workbook per project (openpyxl -- already a dependency elsewhere in this
   repo's tooling), one sheet per stub table, with Excel's real sheet-name constraints handled
   (max 31 chars; `\\ / ? * [ ] :` illegal; collisions deduplicated with a numeric suffix).
5. Copy the WHOLE input project to `<out>/<Workbook>/` -- every file, untouched -- and, in the
   COPY ONLY, rewrite each stub table's `source =` M body to read from that Excel file:
     let
         Source = Excel.Workbook(File.Contents("<path>"), true),
         Sheet = Source{[Item="<sheet>",Kind="Sheet"]}[Data],
         Typed = Table.TransformColumnTypes(Sheet, {{"Col1", type text}, ...})
     in
         Typed
   No `Table.PromoteHeaders` -- `Excel.Workbook(..., true)` already promoted headers; calling it
   again is the exact bug a real manual reconciliation hit. No rename/placeholder-null step is
   needed here (unlike the manual case) because the Excel file's columns already match the model
   exactly, by construction.
6. Emit `<out>/<Workbook>/DUMMY_DATA_REPORT.md`: every table wired (row count, sheet name, exact
   column list) and every table skipped and why -- matching this pipeline's RESIDUE.md-style
   honesty. Nothing is silently skipped.

Fail-closed rule this script inherits from the rest of this pipeline: a Param table or a
native-connector partition is NEVER touched. Guessing wrong here would overwrite real data with
fake data, which is worse than doing nothing.

`--desktop-path` is REQUIRED for correctness on any machine other than the one that ran this
script. If omitted, the default is this script's own resolved absolute POSIX path to the written
Excel file, with a loud warning printed -- exactly the class of guess (a path assumed to be
right on a different machine) that caused rework in the manual session this script replaces.
This default is derived FROM THE FILE THIS RUN ITSELF JUST WROTE (`excel_path.resolve().as_posix()`
in `process_project()`, computed fresh per project) -- never a hardcoded example string. A real
prior run's output was once found with every wired table across an entire corpus pointed at the
identical, made-up `C:/Users/analyst/Desktop/PBIP/DummyData.xlsx` regardless of which workbook
actually owned which file; the self-test below asserts that shape can never come out of this
script again.

`--realistic` mode: real values, not synthetic placeholders, wherever resolvable
----------------------------------------------------------------------------------
Passing `--realistic <input-corpus-dir>` (the ORIGINAL Tableau input folder, e.g.
`input/test_corpus` -- the same folder handed to `build-tableau-pbip`) switches a stub table's
values from purely-synthetic to REAL, sampled straight out of that workbook's own embedded
Tableau `.hyper` extract, wherever this project's own `_work/ir.json` records a resolvable one.
Omit `--realistic` entirely and behavior is EXACTLY as above -- fully synthetic, unchanged.

How a table's real source is found -- never guessed, never name-matched by heuristic:
  1. `_work/ir.json` (already emitted by every `build-tableau-pbip` run) is THE authority. Each
     IR table record's `connections` list may contain an entry with `"class": "hyper"` and a
     `"dbname"` that is the exact zip-relative path to the real `.hyper` file INSIDE whichever
     archive holds it -- the workbook's own `.twbx` (`"provenance": "twb"`), or a sibling
     `.tdsx` resolved via `--tds` at build time (`"provenance": "tds:<filename>"`, which names the
     exact sibling file to look in). No filename heuristic, no scanning by convention -- the
     provenance field says exactly which archive, and the dbname field says exactly which member.
  2. That archive is opened (as a zip) under `--realistic <dir>` (for `"twb"` provenance, matched
     by the IR's own recorded `workbook` name -- never the PBIP project's sanitized folder name,
     which can differ, e.g. `[VRM] Compliance Dashboard.twbx` vs the project folder
     `_VRM_ Compliance Dashboard`) or `--tds <dir>` (for `"tds:..."` provenance). If the expected
     archive doesn't contain that exact member, every `.twbx`/`.tdsx` under both directories is
     scanned as a fallback (member existence only, not a full read) before giving up.
  3. The matched `.hyper` member is extracted to a scratch temp directory (`tempfile.mkdtemp()`
     -- never bare `/tmp`) and opened via `tableauhyperapi`. The real schema/table name is read
     from `conn.catalog` every time (Tableau extracts are conventionally `"Extract"."Extract"`,
     but this is VERIFIED per file, never assumed -- and a real corpus workbook's extract was
     found to hold SEVERAL tables at once under the `Extract` schema, see `_pick_table`).
  4. A sample is drawn with `TABLESAMPLE BERNOULLI(--sample-pct)` (default 100, i.e. full data --
     see DEFAULT_SAMPLE_PCT's own docstring for why), capped at `--max-real-rows` (default 0, i.e.
     no cap) -- the sampling percentage controls value diversity, the cap controls output file
     size, since even a 1% sample of a 50-million-row extract is still huge. Pass a smaller
     --sample-pct/--max-real-rows explicitly for a quick, lower-fidelity build.
     `TABLESAMPLE` can legitimately return zero rows from a small table by chance; if so, a plain
     `LIMIT <cap>` (no sampling) is tried once as a fallback, so a real (if unsampled) row set is
     still preferred over abandoning the table.
  5. Column mapping is read from THIS SAME `ir.json`: each table's `columns` list already records
     each physical column's real Hyper `source_column` name alongside its allocated PBI/TMDL
     `name`. Selection from Hyper uses `source_column`; the generated M then reads the sheet by
     the TMDL's `name` (via `Table.RenameColumns`-equivalent header row -- the Excel sheet is
     written with the FINAL display name as its header, so no separate rename step is needed, the
     same as the synthetic path).

A `.hyper` file for a UNION/federated Tableau datasource can hold SEVERAL physical tables at once
under the `Extract` schema -- found on the real reference corpus: one extract held a 20-column
real data table alongside two single-column leftover "sheet" tables from the union. Picking
whichever table a zip/catalog listing happens to return first is wrong (it silently picked a
near-empty 1-column table over the real 20-column one on that real workbook). `_pick_table`
disambiguates by NAME-OVERLAP against the `source_column` names this table's own `ir.json` record
already declares it needs -- never a guess at which table "looks right".

The one hard boundary, stated plainly: **calculated fields are never evaluated.** A column with
no resolvable `source_column` in `ir.json` (a calculated field, group, or bin -- anything with no
raw physical source) ALWAYS falls back to the synthetic generator for that one column, even
inside an otherwise-"real" table. This script does not implement, and will never implement, a
Tableau-formula or DAX evaluator to produce "realistic" derived values -- this pipeline has
already judged that scope explicitly out of bounds (see `build-tableau-pbip/SKILL.md`'s own
"Future recommendation" section): an incorrectly-computed value that LOOKS plausible is worse
than an honestly-synthetic one, per this whole pipeline's fail-closed philosophy. The same
fail-closed rule applies one level up: if NO `.hyper` file can be resolved for a table at all (no
`hyper` connection recorded, connection recorded but the archive doesn't contain it, or a
genuinely live-only source with no local extract), the WHOLE table falls back to the synthetic
generator, and the reason is reported in `DUMMY_DATA_REPORT.md` -- never silently.

**Data sensitivity.** `--realistic` mode pulls real data values out of the source workbook's own
embedded extracts -- in this pipeline's own reference corpus, that includes real transaction/
card-processing fields (auth response codes, CVV results, card-present flags, merchant/BIN
identifiers). The Excel workbook and rewritten project this mode produces should be handled with
the SAME data-sensitivity care as the original source Tableau workbook -- this is a factual
statement about what the mode does, not a hedge.

Usage:
  python3 generate_dummy_data.py <build-tableau-pbip --out project-or-parent-folder> \\
      --out <output-folder> [--rows N] [--desktop-path <path>] \\
      [--realistic <input-corpus-dir> [--tds <dir>] [--sample-pct N] [--max-real-rows N]]
"""
import re
import sys
import hashlib
import shutil
import zipfile
import tempfile
import argparse
import datetime
import json
from pathlib import Path

try:
    import openpyxl
except ImportError:                                                        # pragma: no cover
    openpyxl = None

try:
    from tableauhyperapi import HyperProcess, Telemetry, Connection
    _HYPERAPI_AVAILABLE = True
except ImportError:                                                        # pragma: no cover
    _HYPERAPI_AVAILABLE = False


DEFAULT_ROWS = 20
# Full, uncapped real data by default. A low default sample/cap looked like a reasonable
# "quick build" convenience, but it has a real, repeat-confirmed cost: a sparse sample under-
# represents the distinct dimension-value combinations a report's own slicers/visuals actually
# need, so clicking almost any selector on a real workbook (confirmed on GCLM's Dormancy
# Management tab) renders the majority of cards/visuals to (Blank) -- a defect that looks
# exactly like a translation bug and isn't one. Pass a smaller --sample-pct/--max-real-rows
# explicitly when a fast, lower-fidelity build is what's actually wanted.
DEFAULT_SAMPLE_PCT = 100
# 0 means no cap (see --max-real-rows's own help text) -- paired with DEFAULT_SAMPLE_PCT=100
# above, every real row is written by default, for the same reason.
DEFAULT_MAX_REAL_ROWS = 0

# M type literals that can appear in a stub's `type table [...]` ascription -- mirrors
# build-tableau-pbip/scripts/tmdl.py's own M_TYPE dict exactly, so a new TOM dataType added there
# needs the same addition here to stay in sync. No "date" entry, by the same reasoning tmdl.py's
# _tom_dtype documents: nothing valid should reach an M ascription still carrying that string.
M_TYPES = ("text", "Int64.Type", "number", "datetime", "logical")

# The M type value to hand Table.TransformColumnTypes for each ascription type above. Int64.Type
# is already a type VALUE (usable bare); the rest need the `type` keyword in this expression
# context, unlike inside a `type table [...]` ascription where the bare word is enough.
TRANSFORM_TYPE = {
    "text": "type text",
    "Int64.Type": "Int64.Type",
    "number": "type number",
    "datetime": "type datetime",
    "logical": "type logical",
}

_TYPE_ALT = "|".join(re.escape(t) for t in M_TYPES)
# A `type table [...]` field: either an M-quoted identifier (#"...") or a plain bare word,
# followed by `= <type>`. Mirrors tmdl.py's own m_ident() quoting rule (bare iff
# ^[A-Za-z_][A-Za-z0-9_]*$), so every name this pipeline itself can emit is matched.
_COL_RE = re.compile(r'(#"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(' + _TYPE_ALT + r')')
_STUB_RE = re.compile(r"Table\.FromRows\(\{\},\s*type table \[(.*)\]\)", re.DOTALL)
_PARAM_RE = re.compile(r"Table\.FromRows\(\{\{")
# The `source =` property is always 2 tabs deep under a `partition` block in every corpus TMDL
# this pipeline has produced (table -> partition -> source, one tab each); its multi-line `let
# ... in ...` body is always 4+ tabs deep (tmdl.py's _expr() convention: level + EXPR_EXTRA_INDENT
# for `let`/`in`, deeper still for the body's own relative structure). Matching on tab DEPTH,
# not on "let"/"in" keywords appearing anywhere, is what lets this survive a body that happens to
# contain those words inside a string literal.
_SOURCE_BLOCK_RE = re.compile(r"(\t\tsource =\n)((?:\t{4,}[^\n]*\n)+)")
_TABLE_HEADER_RE = re.compile(r"^table\s+(.+)$", re.MULTILINE)
_ILLEGAL_SHEET_CHARS = re.compile(r'[\\/?*\[\]:]')


# ---------------------------------------------------------------- TMDL/M quoting (mirrors names.py)

def m_unquote(tok):
    """Reverse of tmdl.py's m_ident(): `#"Billable BID"` -> `Billable BID`, `Channel` -> `Channel`."""
    if tok.startswith('#"'):
        return tok[2:-1].replace('""', '"')
    return tok


def tmdl_unquote(rest):
    """The declared name from a `table 'X'` / `table X` header line.

    Mirrors tools/pbip_rules/model.py's own unquote_tmdl() -- same quoting rule (a leading `'`
    starts a quoted identifier, `''` is an escaped quote, anything else is read as a bare word up
    to the first whitespace/`=`/`:`), so a table name this pipeline itself writes is always read
    back correctly without a second, disagreeing implementation of the same rule.
    """
    rest = rest.strip()
    if not rest.startswith("'"):
        m = re.match(r"[^\s=:]+", rest)
        return m.group(0) if m else rest
    buf = []
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


def _split_table_column(ref):
    """`fromColumn:`/`toColumn:`'s own `Table.Column` syntax -> (table, column).

    Same quoted-or-bare identifier rule as `tmdl_unquote`, applied twice: a leading `'` reads a
    quoted span (an embedded `''` is an escaped quote), otherwise a bare span runs up to the
    first UNQUOTED `.` -- the separator between the two identifiers, which is why this cannot
    reuse `tmdl_unquote` directly (that function has no notion of stopping at `.`, only at
    whitespace/`=`/`:`, because a table/column header never has one after the name).
    """
    def read_one(s):
        if s.startswith("'"):
            buf, i = [], 1
            while i < len(s):
                if s[i] == "'":
                    if i + 1 < len(s) and s[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(s[i])
                i += 1
            return "".join(buf), s[i:]
        m = re.match(r"[^.]+", s)
        tok = m.group(0) if m else s
        return tok, s[len(tok):]

    table, rest = read_one(ref.strip())
    column, _rest = read_one(rest.lstrip("."))
    return table, column


_REL_COLUMN_RE = re.compile(r"^\s*(from|to)Column:\s*(.+?)\s*$", re.MULTILINE)


def parse_relationships_tmdl(text):
    """relationships.tmdl's own text -> [(many_table, many_col, one_table, one_col), ...].

    `fromColumn` is always the "many" side and `toColumn` the "one" side, by this pipeline's own
    emission convention in tmdl.py's `_relationships()` -- both the real key-based joins
    (`split_federated_datasource`'s output) and the blend-key relationships put the more-rows
    side first. A relationship missing either line (malformed, or a shape this parser does not
    recognise) is skipped rather than guessed at -- dummy data staying independent per table is
    a smaller problem than linking the wrong columns.
    """
    edges = []
    pending = {}
    for m in _REL_COLUMN_RE.finditer(text):
        pending[m.group(1)] = m.group(2)
        if "from" in pending and "to" in pending:
            ft, fc = _split_table_column(pending["from"])
            tt, tc = _split_table_column(pending["to"])
            edges.append((ft, fc, tt, tc))
            pending = {}
    return edges


def m_string_literal(s):
    """An M text literal: doubled quotes escape an embedded quote, same as TMDL's own rule."""
    return '"' + str(s).replace('"', '""') + '"'


# ---------------------------------------------------------------- partition classification

def extract_source_block(table_text):
    """The M body text of a table's `source =` property, or None if there isn't one."""
    m = _SOURCE_BLOCK_RE.search(table_text)
    return m.group(2) if m else None


def classify_source(source_block):
    """Classify a table's `source =` M body.

    Returns (kind, columns): kind is one of
      "stub"         -- Table.FromRows({}, type table [...]) -- eligible for synthetic wiring.
                         columns is [(name, m_type), ...] read from the ascription.
      "unparseable"  -- the {} zero-row signal is present but the ascription didn't parse.
                         Declined rather than guessed at, per this pipeline's fail-closed rule.
      "param"        -- Table.FromRows({{...}) -- real literal domain values. Never touched.
      "native"       -- any other `= m` partition body (a real connector). Never touched.
      "no_partition" -- source_block was None.
    """
    if source_block is None:
        return "no_partition", None
    if "Table.FromRows({}," in source_block:
        m = _STUB_RE.search(source_block)
        if not m:
            return "unparseable", None
        cols = [(m_unquote(name), mtype) for name, mtype in _COL_RE.findall(m.group(1))]
        return "stub", cols
    if _PARAM_RE.search(source_block):
        return "param", None
    return "native", None


# ---------------------------------------------------------------- synthetic values

_EPOCH = datetime.date(2024, 1, 1)

# An Int64 column named like a flag ("CE Eligible - Flag", "Billable Deflection - Flag", ...) is
# semantically 0/1, not a generic integer -- confirmed on a real corpus workbook (Prevent
# Dashboard): every one of 20 synthetic rows got 1001-1020 for a column literally named
# "CE Eligible - Flag", so the Tableau-translated `IF([...Flag]=1, "X", IF([...Flag]=0, "Y",
# BLANK()))` fell through to BLANK() on every row -- neither branch's condition was ever true.
# `\bflag\b` (not a bare substring check) so a column merely containing "flag" as part of a
# longer word would not be misclassified.
_FLAG_NAME_RE = re.compile(r"\bflag\b", re.IGNORECASE)

# An Int64 column named like a compound YYYYMM period code ("Cpd Month") is sliced by a
# downstream calculated column via `LEFT(x, 4) & "-" & RIGHT(x, 2) & "-01"` straight into
# DATEVALUE -- confirmed on a real corpus workbook (LAC Pedidosya Transaction Profile): the
# generic `1000 + seed + i` formula produced 1773, whose text form "1773" sliced to
# LEFT(4)="1773"/RIGHT(2)="73", giving DATEVALUE("1773-73-01") -- month 73 does not exist, and
# Power BI Desktop refused to load the table at all ("Fields that need to be fixed... Cannot
# convert value '1773-73-01' of type Text to type Date"). A generic integer is a crash here, not
# a silently-wrong number: the fix is a value that is ALWAYS a valid 6-digit YYYYMM.
_MONTH_CODE_NAME_RE = re.compile(r"\bmonth\b", re.IGNORECASE)

# The same idiom, one shelf over: a TEXT column named like a compound YYYYQ period code
# ("Quarter", "Prch Qtr") is sliced via `LEFT(x, 4) & "-" & ((INT(RIGHT(x, 1)) - 1) * 3 + 1) &
# "-01"` straight into DATEVALUE -- confirmed on a real corpus workbook (NGPO Dashboard New): the
# generic `f"{col_name} {i}"` formula produced "Quarter 1", whose LEFT(4)="Quar" is not a year at
# all. Same failure mode as `_MONTH_CODE_NAME_RE`, one shelf over: an always-valid YYYYQ (year +
# a single digit 1-4) instead of the column-name-echoing text every other text column gets.
_QUARTER_CODE_NAME_RE = re.compile(r"\bquarter\b|\bqtr\b", re.IGNORECASE)


def _col_seed(col_name):
    """A stable per-COLUMN integer, so different columns get different value SERIES.

    Deliberately hashlib, never Python's built-in `hash()`: that is salted per process
    (PYTHONHASHSEED), so it would produce a different workbook on every run and break this
    pipeline's determinism rule -- the same reason tmdl.py content-hashes its lineage tags.

    Why this exists: every branch below used to depend on the ROW index alone, so every column of
    a given type produced the IDENTICAL series -- `i % 2` for every `*- Flag`, `1000 + i` for
    every other Int64, `100.0 + i * 1.5` for every decimal. Opened in Desktop against a real
    corpus workbook (Prevent Dashboard) that made every single measure on the page report the
    same number: with all 20 rows of every flag alternating 1,0, every `SUM(<any flag>)` was
    exactly 10 and every rate `SUM(a)/SUM(b)` was exactly 1.00. Numbers that are all identical
    cannot show a reviewer whether a translated measure is right -- which is the entire point of
    wiring dummy data -- and they read as "the data is broken" rather than "the data is fake".
    """
    return int(hashlib.sha1((col_name or "").encode("utf-8")).hexdigest()[:8], 16)


def _flag_period_phase(col_name):
    """(period, phase) for a 0/1 flag column, giving it a DENSITY of its own.

    period 2..5 -> a 1-density of 1/2, 2/3, 3/4 or 4/5, so two different flag columns sum to
    different totals. Both 0 and 1 still occur within any run of >= 5 rows, which is the property
    the original alternating pattern existed to guarantee (it exercises both sides of a translated
    `IF(flag = 1, ..., IF(flag = 0, ...))`), so that behaviour is preserved, not traded away.
    """
    h = _col_seed(col_name)
    period = 2 + (h % 4)
    return period, (h >> 8) % period


def synth_value(col_name, mtype, i, role=None, dim_cap=None):
    """One deterministic synthetic value for row i (1-indexed) of a column of M type `mtype`.

    Every branch is a plain formula, never a random draw -- re-running this on the same input
    produces byte-identical output, matching this pipeline's own determinism discipline (see
    tmdl.py's lineage tags, which are content-hashed rather than freshly generated, for the same
    reason).

    `role`/`dim_cap`: when `role == "dimension"` and `dim_cap` is a positive int, row index `i`
    is folded into a `dim_cap`-sized cycle (1..dim_cap, repeating) BEFORE the type-specific
    formula runs, so a dimension column never exceeds `dim_cap` distinct values no matter how
    large `i` gets -- a large synthetic row count (e.g. 50000, for fact-table volume) would
    otherwise give every dimension column exactly as many distinct values as there are rows,
    which looks nothing like a real dimension. `role is None` (no ir.json role match) or
    `dim_cap` falsy leaves `i` untouched -- unchanged, legacy behavior.
    """
    if role == "dimension" and dim_cap:
        i = (i - 1) % dim_cap + 1
    if mtype == "text":
        if _QUARTER_CODE_NAME_RE.search(col_name):
            # See _QUARTER_CODE_NAME_RE's own comment -- always a valid YYYYQ: LEFT(4) is a
            # real 4-digit year, RIGHT(1) is a real quarter digit 1-4.
            offset = _col_seed(col_name) % 4
            year = 2019 + ((i - 1 + offset) // 4) % 6
            quarter = ((i - 1 + offset) % 4) + 1
            return f"{year}{quarter}"
        return f"{col_name} {i}"
    if mtype == "Int64.Type":
        if _MONTH_CODE_NAME_RE.search(col_name):
            # See _MONTH_CODE_NAME_RE's own comment -- always a valid YYYYMM: LEFT(4) is a real
            # 4-digit year, RIGHT(2) is a real 2-digit month 01-12.
            offset = _col_seed(col_name) % 12
            year = 2019 + ((i - 1 + offset) // 12) % 6
            month = ((i - 1 + offset) % 12) + 1
            return year * 100 + month
        if _FLAG_NAME_RE.search(col_name):
            # 0/1, with a per-column density -- exercises BOTH sides of whatever
            # IF(flag=1, ..., IF(flag=0, ..., ...)) branch a translated Tableau calc has, while
            # still summing to a DIFFERENT total per column (see _flag_period_phase).
            period, phase = _flag_period_phase(col_name)
            return 0 if (i + phase) % period == 0 else 1
        # Per-column base, so two integer columns are not the same series. Kept in the same
        # 4-digit shape the original `1000 + i` produced, so anything eyeballing magnitudes still
        # sees what it expects.
        return 1000 + (_col_seed(col_name) % 9000) + i
    if mtype == "number":
        seed = _col_seed(col_name)
        # Per-column base AND step, so a rate built from two decimal columns is not 1.00 for
        # every pair of them.
        return round((seed % 500) + 0.5 + i * (1.0 + (seed >> 12) % 7 * 0.25), 2)
    if mtype == "datetime":
        d = _EPOCH + datetime.timedelta(days=i)
        return datetime.datetime(d.year, d.month, d.day)
    if mtype == "logical":
        return i % 2 == 1
    raise ValueError(f"unknown M type {mtype!r} -- not one of {M_TYPES}")


def synthetic_rows(columns, n_rows, role_map=None, dim_cap=None):
    """[[v(col1,1), v(col2,1), ...], [v(col1,2), ...], ...] -- one call to synth_value() per cell.

    `role_map`: optional {column display name -> "dimension"/"measure"}, read from this
    project's own `_work/ir.json` (see `load_role_map()`) -- looked up per column so `dim_cap`
    (see `synth_value()`) only folds dimension-role columns, never measures.

    Used both for a fully-synthetic table AND, under `--realistic`, for any individual
    synthetic-FALLBACK column mixed into an otherwise-real table (a calculated field with no
    resolvable `source_column`) -- the exact same formula either way, just at a row count that
    may be dictated by a real sample's size rather than `--rows`.
    """
    role_map = role_map or {}
    return [[synth_value(name, mtype, i, role_map.get(name), dim_cap) for name, mtype in columns]
            for i in range(1, n_rows + 1)]


# ---------------------------------------------------------------- realistic mode: Hyper sampling

def coerce_hyper_value(value, mtype):
    """Coerce one raw value read from a real Tableau .hyper extract into a value valid for
    Table.TransformColumnTypes's target `mtype` -- the exact same contract synth_value() commits
    to, so a real and a synthetic value for the same column are indistinguishable to the M layer;
    only their CONTENT differs. `None` (a real SQL NULL) passes through as `None` -- openpyxl
    writes a genuinely blank cell, which TransformColumnTypes reads as a real null, exactly as a
    native connector would.
    """
    if value is None:
        return None
    if mtype == "text":
        # A hyper extract column physically stored as a float/double but author-overridden to
        # Tableau's `datatype='string' datatype-customized='true'` (e.g. GCLM's own
        # `qtr_rnk_flg`, raw DOUBLE values like 1.0 and 3.87096774) -- Tableau's own double-to-
        # string cast trims a trailing ".0" for a whole number ("1.0" displays/filters as "1"),
        # it does not carry Python's `str(float)` suffix through. A worksheet's own saved filter
        # member (`member='1'`) is captured verbatim from TABLEAU's string form, so sampling the
        # same column here with plain `str(value)` emits "1.0" -- never equal to the filter's "1"
        # -- and silently zeroes every row the filter was ever meant to match. Confirmed on GCLM:
        # Activation's "3.1 Portfolio Summary" cards (`For View`+`qtr_rnk_flg=1`) always blank,
        # even though real rows with the underlying value 1.0 exist. Only a WHOLE-number float
        # is reformatted -- a genuine fractional value (3.87096774) is untouched, since Tableau's
        # own cast only ever drops a trailing ".0", never rounds.
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if mtype == "Int64.Type":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(round(float(value)))
    if mtype == "number":
        return float(value)
    if mtype == "datetime":
        to_datetime = getattr(value, "to_datetime", None)
        if to_datetime is not None:                        # tableauhyperapi.Timestamp
            return to_datetime()
        to_date = getattr(value, "to_date", None)
        if to_date is not None:                             # tableauhyperapi.Date
            d = to_date()
            return datetime.datetime(d.year, d.month, d.day)
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.date):
            return datetime.datetime(value.year, value.month, value.day)
        return value
    if mtype == "logical":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "y", "t")
        return bool(value)
    raise ValueError(f"unknown M type {mtype!r} -- not one of {M_TYPES}")


class RealDataResolver:
    """Resolves and samples REAL rows from a workbook's embedded Tableau `.hyper` extract(s), for
    `generate_dummy_data.py --realistic`. Never raises out of its public entry point,
    `sample_real_table()` -- every failure mode is caught and returned as a plain-English reason
    string, per this pipeline's fail-closed discipline: an unresolvable real source is reported
    and the caller falls back to the synthetic generator, never a guess.

    One instance is shared across an entire run (all projects) so exactly one Hyper process is
    ever started, and every extracted `.hyper` copy is written to -- and deleted from -- one
    scratch directory created via `tempfile.mkdtemp()` (honours `$TMPDIR`; never assumes bare
    `/tmp` is writable). HyperProcess's own `hyperd.log` is also redirected into that same
    scratch directory (its default is the caller's CURRENT WORKING DIRECTORY, not `$TMPDIR`
    -- verified by running it, not documentation -- which would otherwise leave multi-MB
    debris wherever this script happened to be invoked from). `close()` must be called when
    the run is done.
    """

    def __init__(self, realistic_dir, tds_dir, sample_pct, max_rows):
        self.realistic_dir = Path(realistic_dir) if realistic_dir else None
        self.tds_dir = Path(tds_dir) if tds_dir else None
        self.sample_pct = sample_pct
        self.max_rows = max_rows
        self._scratch = Path(tempfile.mkdtemp(prefix="dummy_data_realistic_"))
        self._hp = None
        self._seq = 0

    def _hyper_process(self):
        if self._hp is None:
            if not _HYPERAPI_AVAILABLE:
                raise RuntimeError(
                    "tableauhyperapi is required for --realistic (pip install tableauhyperapi)")
            # Explicit log_dir -- HyperProcess writes a hyperd.log into the CURRENT WORKING
            # DIRECTORY by default (verified: not $TMPDIR, not this resolver's own scratch dir),
            # which would leave multi-MB debris in whatever directory the caller happened to run
            # this script from. Redirecting it into this resolver's own scratch dir means it gets
            # cleaned up by close() along with everything else this resolver ever wrote.
            self._hp = HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                                     parameters={"log_dir": str(self._scratch)})
        return self._hp

    def close(self):
        """Stop the shared Hyper process (if started) and remove every scratch file this
        resolver ever wrote. Idempotent -- safe to call even if nothing was ever resolved."""
        if self._hp is not None:
            try:
                self._hp.close()
            finally:
                self._hp = None
        shutil.rmtree(self._scratch, ignore_errors=True)

    # ---- archive discovery: driven by ir.json's own `provenance` field, never a filename guess ----

    def _candidate_archives(self, provenance, workbook_name):
        """The archive(s) that COULD hold this table's `.hyper`, in priority order.

        `provenance == "twb"`  -> the workbook's own `.twbx` under `--realistic`, matched by the
                                   IR's own recorded workbook name (never a PBIP project's
                                   sanitized folder name, which can legitimately differ).
        `provenance == "tds:<filename>"` -> that exact sibling file, under `--tds` first (the
                                   documented convention) then `--realistic` (some corpora keep
                                   .tdsx siblings alongside the .twbx rather than in a separate
                                   folder).
        """
        cands = []
        if provenance == "twb":
            if self.realistic_dir:
                cands.append(self.realistic_dir / f"{workbook_name}.twbx")
                cands.append(self.realistic_dir / f"{workbook_name}.twb")
        elif provenance.startswith("tds:"):
            fn = provenance[len("tds:"):]
            if self.tds_dir:
                cands.append(self.tds_dir / fn)
            if self.realistic_dir:
                cands.append(self.realistic_dir / fn)
        return [c for c in cands if c.is_file()]

    @staticmethod
    def _find_member(archive_path, dbname):
        """The real zip member name matching `dbname` inside `archive_path` (exact match
        preferred, case-insensitive fallback for a cross-platform zip-tool quirk), or None."""
        try:
            with zipfile.ZipFile(archive_path) as z:
                names = z.namelist()
                if dbname in names:
                    return dbname
                low = dbname.lower()
                for n in names:
                    if n.lower() == low:
                        return n
        except (zipfile.BadZipFile, OSError):
            return None
        return None

    def _scan_fallback(self, dbname):
        """Last resort: scan every .twbx/.tdsx under --tds and --realistic for `dbname`, checking
        member existence only (no read) until the first hit. Only reached when the provenance-
        directed candidate archive didn't contain the expected member."""
        dirs, seen = [], set()
        for d in (self.tds_dir, self.realistic_dir):
            if d and d.is_dir() and d not in seen:
                seen.add(d)
                dirs.append(d)
        for d in dirs:
            for p in sorted(d.glob("*")):
                if p.suffix.lower() not in (".twbx", ".tdsx"):
                    continue
                member = self._find_member(p, dbname)
                if member:
                    return p, member
        return None, None

    def _extract_hyper(self, archive_path, member):
        self._seq += 1
        local = self._scratch / f"extract_{self._seq}.hyper"
        with zipfile.ZipFile(archive_path) as z, z.open(member) as src, open(local, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        return local

    @staticmethod
    def _pick_table(conn, schema_name, known_source_columns):
        """The real table inside `schema_name` that actually backs this Tableau datasource.

        A `.hyper` extract for a UNION/federated datasource can hold SEVERAL physical tables
        under the `Extract` schema at once (one real corpus workbook's extract held three: a
        20-column real data table plus two single-column leftover sheets from the union). Picking
        `table_names[0]` unconditionally picked whichever one happened to sort/list first --
        found, on the real corpus, to silently pick a near-empty 1-column table over the real
        20-column one, so every real column but one fell back to synthetic even though the real
        data WAS present in the file, just in a different table.

        Disambiguated the same way a person would verify it: by NAME-OVERLAP against the set of
        `source_column` names this table's own `ir.json` record already declares it needs -- never
        a heuristic guess at which table "looks right". Ties (or no `known_source_columns` at all)
        fall back to the first table, same as before.
        """
        table_names = conn.catalog.get_table_names(schema=schema_name)
        if not table_names:
            return None
        if len(table_names) == 1 or not known_source_columns:
            return table_names[0]
        best, best_score = table_names[0], -1
        for t in table_names:
            cols_lower = {c.name.unescaped.lower()
                          for c in conn.catalog.get_table_definition(t).columns}
            score = len(cols_lower & known_source_columns)
            if score > best_score:
                best_score, best = score, t
        return best

    def _query_sample(self, hyper_path, known_source_columns):
        """(colindex, rows) sampled from the real table inside `hyper_path` that best matches
        `known_source_columns` (see `_pick_table`), or None if the file has no schema/table at
        all. Never hardcodes `"Extract"."Extract"` -- reads the real schema/table names from
        `conn.catalog` for every file, as this pipeline's own investigation found necessary to
        verify rather than assume.
        """
        with Connection(endpoint=self._hyper_process().endpoint, database=str(hyper_path)) as conn:
            schema_names = conn.catalog.get_schema_names()
            schema = next(
                (s for s in schema_names if s.name.unescaped.lower() == "extract"), None)
            if schema is None and schema_names:
                schema = schema_names[-1]
            if schema is None:
                return None
            table = self._pick_table(conn, schema.name.unescaped, known_source_columns)
            if table is None:
                return None
            tdef = conn.catalog.get_table_definition(table)
            colnames = [c.name.unescaped for c in tdef.columns]
            colindex = {name.lower(): i for i, name in enumerate(colnames)}
            # `max_rows <= 0` means "no cap" (see --max-real-rows's own help text) -- omit the
            # LIMIT clause entirely rather than passing a sentinel THROUGH to SQL, where
            # `LIMIT 0` means "zero rows", the opposite of uncapped. Confirmed this cap was
            # reachable on a real corpus workbook (GCLM): two tables sampled 100% but still
            # truncated to the default 10,000-row cap out of 247,425 real rows (4%) -- several
            # categorical values a card's own filter depended on existed only in the TRUNCATED
            # 96% of real rows, so a correctly-exact-match filter (no translation bug at all)
            # found nothing in the sample and rendered blank, even after every other blank-card
            # fix (the "(All)"-sentinel filter bug, Desktop's own needs-refresh state) landed.
            limit_clause = f" LIMIT {self.max_rows}" if self.max_rows and self.max_rows > 0 else ""
            rows = list(conn.execute_query(
                f"SELECT * FROM {table} TABLESAMPLE BERNOULLI({self.sample_pct}){limit_clause}"))
            if not rows:
                # BERNOULLI(pct) can legitimately sample zero rows from a small table by chance --
                # a real (if unsampled) row set is still strictly better than giving up on real
                # data, so fall back once to a plain, deterministic LIMIT (full table, uncapped).
                fallback = f"SELECT * FROM {table}{limit_clause}" if limit_clause else \
                           f"SELECT * FROM {table}"
                rows = list(conn.execute_query(fallback))
            return colindex, rows

    def sample_real_table(self, ir_table, workbook_name):
        """Resolve and sample the real Hyper extract backing `ir_table` (one record from this
        project's own `_work/ir.json`).

        Returns:
          {"ok": True, "source_display": "<archive-name>!<dbname>",
           "colindex": {lower_source_column_name: index}, "rows": [row-tuple, ...]}
        or
          {"ok": False, "reason": "<plain-English reason, for DUMMY_DATA_REPORT.md>"}

        Never raises: every failure -- no hyper connection recorded, archive not found, open/query
        error, empty extract -- is caught and reported as a reason, per this pipeline's
        fail-closed discipline (a residue-style "declined, here's why", never a silent guess).
        """
        hyper_conn = next(
            (c for c in ir_table.get("connections", [])
             if c.get("class") == "hyper" and c.get("dbname")),
            None,
        )
        if hyper_conn is None:
            return {"ok": False, "reason": (
                "no `hyper`-class connection recorded in this table's ir.json connections list "
                "-- a live-only source, or a sqlproxy whose sibling .tdsx wasn't resolved at "
                "build time (rebuild with build-tableau-pbip's own --tds)")}
        dbname = hyper_conn["dbname"]
        provenance = ir_table.get("provenance") or "twb"
        known_source_columns = {
            (c.get("source_column") or "").lower() for c in ir_table.get("columns", [])
            if c.get("source_column")
        }

        archive_path = member = None
        for cand in self._candidate_archives(provenance, workbook_name):
            m = self._find_member(cand, dbname)
            if m:
                archive_path, member = cand, m
                break
        if archive_path is None:
            archive_path, member = self._scan_fallback(dbname)
        if archive_path is None:
            return {"ok": False, "reason": (
                f"hyper member {dbname!r} not found in the matching .twbx (via --realistic) or "
                f"any .tdsx (via --tds)")}

        try:
            local_path = self._extract_hyper(archive_path, member)
        except (OSError, zipfile.BadZipFile) as e:
            return {"ok": False, "reason": (
                f"failed extracting {dbname!r} from {archive_path.name}: {e}")}

        try:
            result = self._query_sample(local_path, known_source_columns)
        except Exception as e:                       # noqa: BLE001 -- fail closed, never crash a run
            return {"ok": False, "reason": (
                f"failed opening/querying the extracted hyper file "
                f"({archive_path.name}!{dbname}): {e}")}
        finally:
            try:
                local_path.unlink()
            except OSError:
                pass

        if result is None or not result[1]:
            return {"ok": False, "reason": f"extract at {archive_path.name}!{dbname} has zero rows"}
        colindex, rows = result
        return {"ok": True, "source_display": f"{archive_path.name}!{dbname}",
                "colindex": colindex, "rows": rows}


def load_ir_and_name_authority(work_dir):
    """This project's own `_work/ir.json` -- the same file `validate-tableau-pbip-mapping` reads
    directly, per this pipeline's own convention of trusting the build's own recorded state over
    re-deriving it. Returns (ir_dict, None) on success, (None, reason) on any failure -- never
    raises, since a project built without `_work/` (or an older build) must fall back to fully
    synthetic, not crash the run.
    """
    ir_path = Path(work_dir) / "ir.json"
    if not ir_path.is_file():
        return None, "no _work/ir.json in this project -- rebuild with build-tableau-pbip"
    try:
        return json.loads(ir_path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as e:
        return None, f"could not read/parse _work/ir.json: {e}"


def load_role_map(work_dir):
    """{column/calc display name -> "dimension"/"measure"}, read from this project's own
    `_work/ir.json` -- both raw columns and calculated fields carry their own `role` there.
    Independent of `--realistic`/`load_ir_and_name_authority()`: this is used to cap dimension
    cardinality under `--dim-cardinality-cap` even on a fully-synthetic (non-`--realistic`) run.
    Returns {} on any missing/unreadable ir.json -- callers then simply get no capping, never a
    crash, matching this pipeline's own fail-closed-but-non-fatal convention.
    """
    ir, err = load_ir_and_name_authority(work_dir)
    if ir is None:
        return {}
    role_map = {}
    for table in ir.get("tables", []):
        for col in table.get("columns", []):
            if col.get("name") and col.get("role"):
                role_map[col["name"]] = col["role"]
        for calc in table.get("calcs", []):
            if calc.get("name") and calc.get("role"):
                role_map[calc["name"]] = calc["role"]
    return role_map


def build_ir_table_index(ir, name_authority):
    """table DISPLAY name -> ir.json table record, keyed via the exact same join build-tableau-
    pbip's own tmdl.py `allocate()` uses (`key = tableau_name or caption`) cross-referenced against
    `_work/name_authority.json`'s own `tables` dict (key -> final allocated name) -- the
    authoritative mapping, not a heuristic re-derivation of it.
    """
    key_to_ir = {}
    for t in ir.get("tables", []):
        key = t.get("tableau_name") or t.get("caption")
        key_to_ir[key] = t
    index = {}
    for key, final_name in (name_authority.get("tables") or {}).items():
        ir_t = key_to_ir.get(key)
        if ir_t is not None:
            index[final_name] = ir_t
    return index


# ---------------------------------------------------------------- Excel sheet naming

def sanitize_sheet_name(name, used):
    """A legal, unique-within-`used` Excel sheet name for `name`.

    Excel sheet names: max 31 characters, and `\\ / ? * [ ] :` are illegal. `used` is mutated (a
    case-folded set) so repeated calls across a project's tables dedupe against each other.
    Collision handling truncates the base to make room for a numeric suffix rather than dropping
    the suffix -- an over-length name is exactly as broken as an illegal character.
    """
    cleaned = _ILLEGAL_SHEET_CHARS.sub("_", str(name)).strip() or "Sheet"
    base = cleaned[:31]
    candidate = base
    n = 1
    while candidate.casefold() in used:
        suffix = f"_{n}"
        candidate = base[: max(1, 31 - len(suffix))] + suffix
        n += 1
    used.add(candidate.casefold())
    return candidate


# ---------------------------------------------------------------- Excel workbook writer

# A fixed instant, not the wall clock. openpyxl stamps every zip entry's mtime AND
# docProps/core.xml's dcterms:created/modified with datetime.now() unconditionally at save
# time -- overriding wb.properties before saving does not stick for `modified` (verified: it
# gets re-stamped by the writer regardless). Freezing both after the fact is what makes two
# runs against the same input produce a BYTE-IDENTICAL .xlsx, not just identical cell values --
# matching this pipeline's own determinism discipline (e.g. tmdl.py's content-hashed lineage
# tags, never freshly generated). NOTE: this determinism claim only holds for the fully-synthetic
# path -- a real Hyper sample is not reproducible cell-for-cell across runs by design (a fresh
# TABLESAMPLE draw), only the FILE-LEVEL mechanics (timestamps) are frozen either way.
_FROZEN_DATETIME = (2024, 1, 1, 0, 0, 0)
_CORE_XML_TIMESTAMP_RE = re.compile(
    rb"<dcterms:(created|modified)([^>]*)>[^<]*</dcterms:\1>"
)


def _freeze_xlsx_timestamps(path):
    """Rewrite every zip entry's mtime and docProps/core.xml's own timestamps to a fixed value."""
    import io

    raw = Path(path).read_bytes()
    zin = zipfile.ZipFile(io.BytesIO(raw), "r")
    out = io.BytesIO()
    zout = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "docProps/core.xml":
            data = _CORE_XML_TIMESTAMP_RE.sub(
                rb"<dcterms:\1\2>2024-01-01T00:00:00Z</dcterms:\1>", data
            )
        item.date_time = _FROZEN_DATETIME
        zout.writestr(item, data)
    zout.close()
    Path(path).write_bytes(out.getvalue())


def write_excel(path, tables):
    """tables: [(sheet_name, columns, rows_data), ...]. columns: [(name, m_type), ...].
    rows_data: [[v1, v2, ...], ...] -- already-computed values, one row per entry, aligned to
    `columns`' order. Deliberately agnostic to whether a value came from `synth_value()` or a
    real Hyper sample -- that decision is made entirely by the caller; this function just writes
    whatever it's handed.
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to write the dummy-data Excel workbook")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_name, columns, rows_data in tables:
        ws = wb.create_sheet(sheet_name)
        ws.append([name for name, _ in columns])
        for row in rows_data:
            ws.append(list(row))
    wb.save(path)
    _freeze_xlsx_timestamps(path)


# ---------------------------------------------------------------- M rewrite

def build_excel_m(desktop_path, sheet_name, columns):
    """The replacement `let ... in ...` M body, indented to match tmdl.py's own convention
    (source = at 2 tabs -> let/in at 4 tabs -> statements at 5 tabs).

    Deliberately NOT followed by Table.PromoteHeaders: `Excel.Workbook(..., true)` already
    promotes the first row to headers (the `true` argument). Calling PromoteHeaders again treats
    the (already-correct) header row as a second promotion target and corrupts every column name
    -- the exact bug found manually on LAC Pedidosya. See the regression test in this module's
    self-test.
    """
    path_lit = m_string_literal(desktop_path)
    sheet_lit = m_string_literal(sheet_name)
    pairs = ", ".join(
        f'{{{m_string_literal(name)}, {TRANSFORM_TYPE[mtype]}}}' for name, mtype in columns
    )
    return (
        "\t\t\t\tlet\n"
        f"\t\t\t\t\tSource = Excel.Workbook(File.Contents({path_lit}), true),\n"
        f'\t\t\t\t\tSheet = Source{{[Item={sheet_lit},Kind="Sheet"]}}[Data],\n'
        f"\t\t\t\t\tTyped = Table.TransformColumnTypes(Sheet, {{{pairs}}})\n"
        "\t\t\t\tin\n"
        "\t\t\t\t\tTyped\n"
    )


def rewrite_partition_source(table_text, new_body):
    """Replace only the `source =` block's body, leaving everything else byte-for-byte."""
    return _SOURCE_BLOCK_RE.sub(lambda m: m.group(1) + new_body, table_text, count=1)


# ---------------------------------------------------------------- project discovery

def find_projects(target):
    """A project folder is one holding a .pbip (or, failing that, a .Report folder).

    Mirrors tools/pbip_lint.py's own find_projects() exactly -- the same discovery convention,
    reused rather than reinvented, so `<input>` behaves identically whether it is handed to this
    script or to pbip_lint.py.
    """
    target = Path(target)
    if list(target.glob("*.pbip")) or list(target.glob("*.Report")):
        return [target]
    found = sorted({p.parent for p in target.glob("*/*.pbip")})
    if found:
        return found
    return sorted({p.parent for p in target.glob("*/*.Report")})


# ---------------------------------------------------------------- report

def write_report(out_dir, workbook_name, entries, excel_path, rows, realistic_enabled=False):
    wired = sorted((e for e in entries if e["status"] == "wired"), key=lambda e: e["table"])
    skipped = sorted((e for e in entries if e["status"] == "skipped"), key=lambda e: e["table"])

    any_real = realistic_enabled and any(
        cd["source"] == "real" for e in wired for cd in e.get("col_detail", []))

    lines = [
        f"# Dummy Data Report -- {workbook_name}",
        "",
    ]
    if any_real:
        lines += [
            "**This project's Excel workbook contains REAL values sampled from the source "
            "Tableau workbook's own embedded extract(s) for at least one column below.** Handle "
            "this output with the same data-sensitivity care as the original source Tableau "
            "workbook -- it is not synthetic for every column.",
            "",
        ]
    lines.append(
        "Synthetic values (every column not marked `real` below) are fully deterministic "
        "(sequential formulas from a fixed epoch, no random draw) -- re-running this against the "
        "same input produces byte-identical synthetic output. Every synthetic value is derived "
        "from its own column's name so it is traceable back to that column in Power BI Desktop."
    )
    lines.append("")
    if excel_path is not None:
        if realistic_enabled:
            lines.append(f"Excel workbook: `{excel_path.name}` (row count varies per table -- "
                          "real tables use the real sample size, capped at --max-real-rows; "
                          "fully-synthetic tables use --rows; see per-table detail below).")
        else:
            lines.append(f"Excel workbook: `{excel_path.name}` ({rows} row(s) per wired table).")
    else:
        lines.append(
            "No Excel workbook was written -- no table in this project was an empty-stub "
            "partition eligible for synthetic wiring (see Skipped below)."
        )
    lines += ["", f"## Wired ({len(wired)})", ""]
    if wired:
        if realistic_enabled:
            lines.append("| Table | Sheet | Rows | Real cols | Synthetic cols |")
            lines.append("|---|---|---|---|---|")
            for e in wired:
                cd = e.get("col_detail", [])
                real_n = sum(1 for c in cd if c["source"] == "real")
                synth_n = len(cd) - real_n
                lines.append(f"| {e['table']} | {e['sheet']} | {e['rows']} | {real_n} | "
                              f"{synth_n} |")
        else:
            lines.append("| Table | Sheet | Rows | Columns (name: M type) |")
            lines.append("|---|---|---|---|")
            for e in wired:
                col_list = ", ".join(f"{n}: {t}" for n, t in e["columns"])
                lines.append(f"| {e['table']} | {e['sheet']} | {e['rows']} | {col_list} |")
    else:
        lines.append("(none)")
    lines += ["", f"## Skipped ({len(skipped)})", ""]
    if skipped:
        lines.append("| Table | Reason |")
        lines.append("|---|---|")
        for e in skipped:
            lines.append(f"| {e['table']} | {e['reason']} |")
    else:
        lines.append("(none)")
    if realistic_enabled and wired:
        lines += ["", "## Column-level detail (per wired table)", ""]
        for e in wired:
            lines.append(f"### {e['table']}")
            lines.append("")
            lines.append("| Column | M type | Source | Detail |")
            lines.append("|---|---|---|---|")
            for cd in e.get("col_detail", []):
                lines.append(f"| {cd['name']} | {cd['mtype']} | {cd['source']} | {cd['detail']} |")
            lines.append("")
    lines.append("")
    with open(out_dir / "DUMMY_DATA_REPORT.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- per-project processing

_SKIP_REASONS = {
    "param": (
        "Param table -- its Table.FromRows({{...}) already carries real literal domain values "
        "(the parameter's allowed values). Never overwritten with synthetic data."
    ),
    "native": (
        "already wired to a native connector (not an empty stub) -- e.g. built with "
        "--partitions native. Never overwrite real data with synthetic data."
    ),
    "no_partition": "no `partition ... = m` block found in this table's TMDL.",
    "unparseable": (
        "an empty-stub Table.FromRows({}, ...) was found, but its `type table [...]` ascription "
        "could not be parsed -- declined rather than guessed at."
    ),
}


def _resolve_table_columns(ir_table, columns, real_result, role_map=None, dim_cap=None):
    """Split one wired table's (name, mtype) columns into real vs synthetic VALUES, using
    `ir_table`'s own `columns` list (each entry's `source_column`, the raw Hyper physical name)
    to map a TMDL display name back to what to read from `real_result["colindex"]`/["rows"].

    `role_map`/`dim_cap`: forwarded to `synth_value()` for any per-column synthetic FALLBACK
    (see below) -- a calculated field with no real source is still subject to the same dimension
    cardinality cap as a fully-synthetic table's columns.

    Returns (rows_data, col_detail, n_rows):
      rows_data:  [[v1, v2, ...], ...] -- row-major, aligned to `columns`' order.
      col_detail: [{"name", "mtype", "source": "real"|"synthetic", "detail"}, ...]
      n_rows:     len(real_result["rows"]) -- every column in this table, real or a per-column
                  synthetic fallback, is generated at this SAME row count so the sheet stays
                  rectangular.

    A column with no resolvable `source_column` (a calculated field, group, or bin -- anything
    with no raw physical Hyper source) ALWAYS falls back to `synth_value()` for that one column,
    never to a Tableau/DAX formula evaluation -- see this module's docstring's "hard boundary".
    """
    src_col_for_display = {
        c["name"]: (c.get("source_column") or "") for c in ir_table.get("columns", [])
    }
    role_map = role_map or {}
    colindex, rows = real_result["colindex"], real_result["rows"]
    n_rows = len(rows)
    col_detail = []
    column_values = []
    for name, mtype in columns:
        source_column = src_col_for_display.get(name, "")
        idx = colindex.get(source_column.lower()) if source_column else None
        if idx is not None:
            vals = [coerce_hyper_value(row[idx], mtype) for row in rows]
            col_detail.append({
                "name": name, "mtype": mtype, "source": "real",
                "detail": f"{real_result['source_display']} (sampled n={n_rows})",
            })
        else:
            vals = [synth_value(name, mtype, i, role_map.get(name), dim_cap)
                    for i in range(1, n_rows + 1)]
            if not source_column:
                reason = ("no `source_column` recorded in ir.json -- a calculated field, group, "
                           "or bin, never evaluated by this pipeline (see SKILL.md's data-"
                           "sensitivity/boundary note)")
            else:
                reason = (f"source_column '{source_column}' not present in the real extract's "
                          f"own schema at {real_result['source_display']}")
            col_detail.append({"name": name, "mtype": mtype, "source": "synthetic",
                                "detail": reason})
        column_values.append(vals)
    rows_data = [list(row) for row in zip(*column_values)] if column_values else []
    return rows_data, col_detail, n_rows


def process_project(src_dir, out_dir, rows, desktop_path_arg, realistic=None, dim_cap=None):
    """Wire dummy data into a build-tableau-pbip project's stub tables, report.

    If `out_dir` is the SAME path as `src_dir` (resolved), this wires data IN PLACE -- no copy
    is made, the project at `src_dir` is modified directly. This is the normal mode when called
    from the pipeline orchestrator (Agent 2), which already owns a single, disposable Agent-1
    output folder and does not need (or want) a second, separately-named copy sitting next to
    it purely to hold the exact same project plus data -- that used to leave every workbook with
    TWO folders (`<name>/` from Agent 1, `<name>_dummy/` from Agent 2), which is what actually
    caused the folder-duplication confusion this in-place mode exists to remove.

    If `out_dir` is a DIFFERENT path, the ORIGINAL copy-then-wire behavior is unchanged: `src_dir`
    is copied to `out_dir` first (deleting whatever was already at `out_dir`), and the COPY is
    wired -- `src_dir` itself is left untouched. This remains available for anyone who explicitly
    wants an unmodified baseline to diff a data-wired build against (pass a different `--out`).

    `realistic`, if given, is a `RealDataResolver` -- every stub table is then attempted against
    real Hyper data first, per-table AND per-column, falling back to the fully-synthetic
    generator wherever unresolvable (see this module's docstring). `realistic=None` (the default)
    is byte-for-byte the pre-existing, fully-synthetic behavior -- unchanged.

    `dim_cap`, if given, caps every role="dimension" column's synthetic values to at most
    `dim_cap` distinct values (see `synth_value()`); role comes from this project's own
    `_work/ir.json` via `load_role_map()`. `dim_cap=None` (the default) is unchanged, uncapped
    behavior.

    Returns a dict with 'workbook', 'wired', 'skipped' (or 'error' if this isn't a recognisable
    build-tableau-pbip output).
    """
    name = src_dir.name
    in_place = out_dir.resolve() == src_dir.resolve()
    if not in_place:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.copytree(src_dir, out_dir)

    sm_dirs = list(out_dir.glob("*.SemanticModel"))
    if not sm_dirs:
        return {"workbook": name, "error": "no *.SemanticModel folder -- not a "
                                             "build-tableau-pbip output"}
    tables_dir = sm_dirs[0] / "definition" / "tables"
    if not tables_dir.is_dir():
        return {"workbook": name, "error": "no definition/tables -- not a "
                                             "build-tableau-pbip output"}

    entries = []
    wired = []
    for tmdl_path in sorted(tables_dir.glob("*.tmdl")):
        text = tmdl_path.read_text(encoding="utf-8")
        header = _TABLE_HEADER_RE.search(text)
        table_name = tmdl_unquote(header.group(1)) if header else tmdl_path.stem

        source_block = extract_source_block(text)
        kind, cols = classify_source(source_block)

        if kind != "stub":
            entries.append({"table": table_name, "status": "skipped",
                             "reason": _SKIP_REASONS.get(kind, f"unrecognised shape ({kind})")})
            continue
        if not cols:
            entries.append({"table": table_name, "status": "skipped",
                             "reason": "empty stub with no columns in its type ascription"})
            continue

        wired.append({"table": table_name, "path": tmdl_path, "text": text, "columns": cols})

    if not wired:
        write_report(out_dir, name, entries, None, rows, realistic_enabled=realistic is not None)
        return {"workbook": name, "wired": 0, "skipped": len(entries)}

    # ---- dimension-cardinality cap: load this project's own ir.json role map, once ----
    role_map = load_role_map(out_dir / "_work") if dim_cap else {}

    # ---- realistic mode: load this project's own ir.json + name_authority.json, once ----
    ir_index, ir_workbook_name, project_reason = None, None, None
    if realistic is not None:
        ir, ir_err = load_ir_and_name_authority(out_dir / "_work")
        na, na_err = (None, None)
        if ir is not None:
            na_path = out_dir / "_work" / "name_authority.json"
            try:
                na = json.loads(na_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                na_err = f"could not read/parse _work/name_authority.json: {e}"
        if ir is None or na is None:
            project_reason = ir_err or na_err or "unreadable _work/ir.json or name_authority.json"
            print(f"[WARNING] --realistic given for '{name}' but {project_reason} -- every "
                  f"table in this project falls back to fully synthetic.", file=sys.stderr)
        else:
            ir_index = build_ir_table_index(ir, na)
            ir_workbook_name = ir.get("workbook") or name

    used_sheet_names = set()
    sheet_of = {w["table"]: sanitize_sheet_name(w["table"], used_sheet_names) for w in wired}

    for w in wired:
        real_result = None
        table_reason = project_reason
        ir_table = None
        if realistic is not None and ir_index is not None:
            ir_table = ir_index.get(w["table"])
            if ir_table is None:
                table_reason = "no matching ir.json table record for this table name"
            else:
                real_result = realistic.sample_real_table(ir_table, ir_workbook_name)
                if not real_result["ok"]:
                    table_reason = real_result["reason"]
                    real_result = None

        if real_result is not None:
            rows_data, col_detail, n_rows = _resolve_table_columns(ir_table, w["columns"],
                                                                     real_result, role_map,
                                                                     dim_cap)
        else:
            n_rows = rows
            rows_data = synthetic_rows(w["columns"], n_rows, role_map, dim_cap)
            if realistic is not None:
                reason = table_reason or "no --realistic resolution attempted"
                col_detail = [{"name": n, "mtype": t, "source": "synthetic",
                               "detail": f"whole-table fallback: {reason}"}
                              for n, t in w["columns"]]
            else:
                col_detail = []

        w["rows_data"] = rows_data
        w["col_detail"] = col_detail
        w["n_rows"] = n_rows

    # Real relationship? Re-point the "many" side's key column at real values drawn from the
    # "one" side's own generated rows, so the relationship in the model has real matching rows
    # to show in Desktop -- without this, each table's key column is independently derived from
    # its own name (see synth_value's docstring) and the two series never coincide, so a real
    # relationship would load correctly but every visual crossing it would render blank. Runs
    # AFTER every table's own independent generation above, never before: it OVERWRITES one
    # column's already-generated values, it does not change how anything else is generated, so a
    # table with no relationship is byte-for-byte the pre-existing behavior.
    rels_path = tables_dir.parent / "relationships.tmdl"
    if rels_path.is_file() and len(wired) > 1:
        by_table = {w["table"]: w for w in wired}
        for many_t, many_c, one_t, one_c in parse_relationships_tmdl(
                rels_path.read_text(encoding="utf-8")):
            many_w, one_w = by_table.get(many_t), by_table.get(one_t)
            if many_w is None or one_w is None:
                continue
            many_idx = next((i for i, (n, _t) in enumerate(many_w["columns"]) if n == many_c),
                            None)
            one_idx = next((i for i, (n, _t) in enumerate(one_w["columns"]) if n == one_c), None)
            if many_idx is None or one_idx is None:
                continue
            one_vals = [row[one_idx] for row in one_w["rows_data"]]
            if not one_vals:
                continue
            for i, row in enumerate(many_w["rows_data"], start=1):
                # Deterministic, not random: the same (table, column, row) always picks the same
                # index -- hashlib, never Python's per-process-salted hash(), same rule as
                # _col_seed above.
                h = int(hashlib.sha1(f"{many_t}.{many_c}.{i}".encode("utf-8")).hexdigest()[:8], 16)
                row[many_idx] = one_vals[h % len(one_vals)]

    excel_path = out_dir / f"{name}_dummy_data.xlsx"
    write_excel(excel_path, [(sheet_of[w["table"]], w["columns"], w["rows_data"]) for w in wired])

    if desktop_path_arg:
        desktop_path = desktop_path_arg
    else:
        # This run's OWN resolved absolute path to the Excel file it just wrote above -- never
        # a hardcoded example string. See the REGRESSION test in this module's self-test for the
        # exact bug shape (a generic, shared placeholder path) this branch must never produce.
        desktop_path = excel_path.resolve().as_posix()
        print(
            f"[WARNING] --desktop-path not given for '{name}' -- defaulting to this machine's "
            f"own path ({desktop_path}). This is almost certainly WRONG if Power BI Desktop "
            f"opens this project from a different machine. Pass --desktop-path with whatever "
            f"path string your actual Desktop instance will resolve to the Excel file.",
            file=sys.stderr,
        )

    for w in wired:
        body = build_excel_m(desktop_path, sheet_of[w["table"]], w["columns"])
        new_text = rewrite_partition_source(w["text"], body)
        with open(w["path"], "w", encoding="utf-8", newline="\n") as fh:
            fh.write(new_text)
        entries.append({"table": w["table"], "status": "wired", "rows": w["n_rows"],
                         "sheet": sheet_of[w["table"]], "columns": w["columns"],
                         "col_detail": w["col_detail"]})

    write_report(out_dir, name, entries, excel_path, rows, realistic_enabled=realistic is not None)
    return {"workbook": name, "wired": len(wired), "skipped": len(entries) - len(wired)}


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Wire deterministic synthetic (or, with --realistic, real-sampled) data "
                     "into a build-tableau-pbip output's empty stub partitions.")
    ap.add_argument("input", help="a build-tableau-pbip project folder, or a parent folder "
                                   "holding several such projects")
    ap.add_argument("--out",
                     help="output folder -- each project is COPIED here in full and the COPY "
                          "is wired; the input is never touched. Omit (or pass --in-place) to "
                          "wire data directly into `input` instead, with no copy made at all.")
    ap.add_argument("--in-place", action="store_true",
                     help="wire data directly into each project found under `input` -- no copy, "
                          "no second folder. Mutually exclusive with a DIFFERENT --out; passing "
                          "both --in-place and --out pointed at the same path as `input` is "
                          "equivalent to --in-place alone. This is the mode the pipeline "
                          "orchestrator uses (Agent 2 wires data into Agent 1's own output "
                          "folder, so a workbook ends up with exactly one output folder, not "
                          "two).")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                     help=f"synthetic rows per wired table (default {DEFAULT_ROWS}); also the "
                          f"row count for any table that falls back to fully synthetic under "
                          f"--realistic")
    ap.add_argument("--desktop-path",
                     help="the path string Power BI Desktop -- on the machine that will "
                          "actually open this project -- resolves to the generated Excel file. "
                          "Omit only if Desktop will run on THIS machine; otherwise required.")
    ap.add_argument("--realistic", metavar="INPUT_CORPUS_DIR",
                     help="the ORIGINAL Tableau input folder (.twbx/.tdsx) this project was "
                          "built from -- enables sampling REAL values from each table's own "
                          "embedded Hyper extract wherever _work/ir.json resolves one; omit for "
                          "fully synthetic (default, unchanged)")
    ap.add_argument("--tds", metavar="DIR",
                     help="folder of sibling .tdsx published datasources -- same convention as "
                          "build-tableau-pbip's own --tds. Only meaningful with --realistic.")
    ap.add_argument("--sample-pct", type=float, default=DEFAULT_SAMPLE_PCT,
                     help=f"TABLESAMPLE BERNOULLI(N) percentage for --realistic sampling "
                          f"(default {DEFAULT_SAMPLE_PCT})")
    ap.add_argument("--max-real-rows", type=int, default=DEFAULT_MAX_REAL_ROWS,
                     help=f"hard cap on real rows written per table under --realistic, "
                          f"regardless of source table size or --sample-pct (default "
                          f"{DEFAULT_MAX_REAL_ROWS}). 0 (or any value <= 0) means NO cap -- "
                          f"every row --sample-pct selects is written, however large the "
                          f"source table.")
    ap.add_argument("--dim-cardinality-cap", type=int, default=None,
                     help="cap every role=dimension column (per this project's own "
                          "_work/ir.json) to at most N distinct synthetic values, cycling -- "
                          "keeps dimensions realistic at a large --rows. Omit for uncapped "
                          "(legacy) behavior; measures are never capped.")
    a = ap.parse_args(argv)

    if openpyxl is None:
        print("[fatal] openpyxl is required (pip install openpyxl)", file=sys.stderr)
        return 2
    if a.realistic and not _HYPERAPI_AVAILABLE:
        print("[fatal] --realistic requires tableauhyperapi (pip install tableauhyperapi)",
              file=sys.stderr)
        return 2
    if a.tds and not a.realistic:
        print("[WARNING] --tds given without --realistic -- ignored.", file=sys.stderr)

    target = Path(a.input)
    if not target.exists():
        print(f"[fatal] no such path: {target}", file=sys.stderr)
        return 2
    projects = find_projects(target)
    if not projects:
        print(f"[fatal] no PBIP projects found under {target}", file=sys.stderr)
        return 2

    # in_place is true if --in-place was passed, OR if --out was omitted entirely (the natural
    # reading of "no destination given" is "modify what's there", not a silent requirement to
    # also pass --in-place explicitly). If --out IS given alongside --in-place, it must resolve
    # to the same path as `input` -- anything else is a real, likely-accidental contradiction
    # (asking to both copy elsewhere AND wire in place), declined rather than silently picking
    # one.
    in_place = a.in_place or not a.out
    if a.out:
        out_root_check = Path(a.out).resolve()
        if in_place and out_root_check != target.resolve():
            print(f"[fatal] --in-place was given together with --out {a.out!r}, which resolves "
                  f"to a DIFFERENT path than --in-place's own target ({target}) -- pass only "
                  f"one, or make --out identical to the input path.", file=sys.stderr)
            return 2
    out_root = target if in_place else Path(a.out)
    if not in_place:
        out_root.mkdir(parents=True, exist_ok=True)

    realistic = None
    if a.realistic:
        realistic = RealDataResolver(a.realistic, a.tds, a.sample_pct, a.max_real_rows)

    try:
        total_wired = total_skipped = total_errors = 0
        for proj in projects:
            proj_out = proj if in_place else out_root / proj.name
            result = process_project(proj, proj_out, a.rows, a.desktop_path,
                                      realistic, a.dim_cardinality_cap)
            if "error" in result:
                print(f"[skip] {result['workbook']}: {result['error']}")
                total_errors += 1
                continue
            total_wired += result["wired"]
            total_skipped += result["skipped"]
            print(f"[ok]   {result['workbook']}: {result['wired']} table(s) wired, "
                  f"{result['skipped']} skipped -> {proj_out}")
    finally:
        if realistic is not None:
            realistic.close()

    print(f"\n{total_wired} table(s) wired, {total_skipped} skipped, {total_errors} project(s) "
          f"not recognised, across {len(projects)} project(s) found under {target}.")
    return 1 if total_errors and not (total_wired or total_skipped) else 0


# ---------------------------------------------------------------- self-test

def _make_fixture_hyper(scratch_dir, seq_label, columns, rows):
    """Build a tiny, real, on-disk .hyper file via tableauhyperapi's OWN write API
    (TableDefinition + Inserter) at schema.table `"Extract"."Extract"` -- deterministic and fast,
    so the self-test never depends on the real corpus being present.

    columns: [(source_col_name, sql_type_fn), ...] e.g. [("acct_id", SqlType.text), ...]
    rows: [[v1, v2, ...], ...] literal values matching `columns`' order.
    Returns the Path to the written .hyper file.
    """
    from tableauhyperapi import (
        CreateMode, TableDefinition, TableName, Inserter, NULLABLE,
    )

    db_path = Path(scratch_dir) / f"fixture_{seq_label}.hyper"
    table_name = TableName("Extract", "Extract")
    tdef = TableDefinition(table_name=table_name, columns=[
        TableDefinition.Column(cname, ctype(), NULLABLE) for cname, ctype in columns
    ])
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                       parameters={"log_dir": str(scratch_dir)}) as hp:
        with Connection(endpoint=hp.endpoint, database=str(db_path),
                        create_mode=CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            conn.catalog.create_table(tdef)
            with Inserter(conn, tdef) as ins:
                for row in rows:
                    ins.add_row(row)
                ins.execute()
    return db_path


def _make_fixture_archive(archive_path, hyper_path, dbname_in_zip):
    """A minimal .twbx-shaped zip containing just the one hyper member at `dbname_in_zip`."""
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(hyper_path, dbname_in_zip)


def _make_fixture_hyper_multi(scratch_dir, seq_label, tables):
    """Like `_make_fixture_hyper`, but writes SEVERAL tables into one `.hyper` file, all under the
    `Extract` schema -- the exact shape a real UNION/federated Tableau datasource's extract can
    take (found on the real reference corpus: one extract held a 20-column real data table
    alongside two single-column leftover "sheet" tables from the union). Exercises
    `RealDataResolver._pick_table`'s column-overlap disambiguation, not just the single-table case.

    tables: [(table_name, [(col_name, sql_type_fn), ...], [[v1, v2, ...], ...]), ...]
    """
    from tableauhyperapi import CreateMode, TableDefinition, TableName, Inserter, NULLABLE

    db_path = Path(scratch_dir) / f"fixture_multi_{seq_label}.hyper"
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                       parameters={"log_dir": str(scratch_dir)}) as hp:
        with Connection(endpoint=hp.endpoint, database=str(db_path),
                        create_mode=CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            for table_name, columns, rows in tables:
                tdef = TableDefinition(table_name=TableName("Extract", table_name), columns=[
                    TableDefinition.Column(cname, ctype(), NULLABLE) for cname, ctype in columns
                ])
                conn.catalog.create_table(tdef)
                with Inserter(conn, tdef) as ins:
                    for row in rows:
                        ins.add_row(row)
                    ins.execute()
    return db_path


def _self_test():
    # ---- sheet-name sanitization: plain name passes through unchanged ----
    used = set()
    assert sanitize_sheet_name("gbo_lac_monthly.pedi", used) == "gbo_lac_monthly.pedi"

    # ---- truncation to 31 chars, and collision gets a numeric suffix ----
    used2 = set()
    long_name = "A Table Name That Is Definitely Longer Than Thirty One Chars"
    s1 = sanitize_sheet_name(long_name, used2)
    assert len(s1) <= 31, s1
    s2 = sanitize_sheet_name(long_name, used2)  # same 31-char truncated base -> collision
    assert s2 != s1 and len(s2) <= 31, (s1, s2)
    s3 = sanitize_sheet_name(long_name, used2)
    assert s3 not in (s1, s2) and len(s3) <= 31, (s1, s2, s3)

    # ---- illegal characters replaced, never leak through ----
    used3 = set()
    illegal_src = "A/B:C*D?E[F]G\\H"
    cleaned = sanitize_sheet_name(illegal_src, used3)
    assert not any(c in cleaned for c in "\\/?*[]:"), cleaned
    assert len(cleaned) <= 31

    # ---- case-insensitive collision (Excel sheet names are case-insensitive) ----
    used4 = set()
    a = sanitize_sheet_name("Foo", used4)
    b = sanitize_sheet_name("foo", used4)
    assert a != b, "case-insensitive collision must still get a distinct name"

    # ---- synthetic value generation: one deterministic formula per M type ----
    assert synth_value("Foo", "text", 1) == "Foo 1"
    assert synth_value("Foo", "text", 2) == "Foo 2"
    assert synth_value("Foo", "text", 1) != synth_value("Bar", "text", 1)
    # Int64 is a 4-digit-shaped ascending series, but with a PER-COLUMN base (see _col_seed), so
    # the exact number is no longer asserted -- the invariants are: ascending in i, and two
    # different columns are different series.
    assert synth_value("Foo", "Int64.Type", 20) > synth_value("Foo", "Int64.Type", 1)
    assert 1000 <= synth_value("Foo", "Int64.Type", 1) <= 10_020
    assert synth_value("Foo", "Int64.Type", 1) != synth_value("Bar", "Int64.Type", 1), \
        "two Int64 columns must not produce the identical series"

    # A flag column is 0/1 and BOTH values occur -- the property the original alternating pattern
    # existed for (it exercises both sides of a translated IF(flag=1,...,IF(flag=0,...))).
    for _flag_col in ("CE Eligible - Flag", "Is Active - Flag (vwCD Merchant)"):
        _series = [synth_value(_flag_col, "Int64.Type", i) for i in range(1, 21)]
        assert set(_series) == {0, 1}, (_flag_col, _series)

    # ...and the REGRESSION this per-column density fixes: two different flag columns must not sum
    # to the same total. Every flag alternating 1,0 made every SUM(flag) exactly 10 and every
    # SUM(a)/SUM(b) rate exactly 1.00 across a whole real dashboard in Desktop.
    _sums = {c: sum(synth_value(c, "Int64.Type", i) for i in range(1, 21))
             for c in ("CE Eligible - Flag", "Billable Deflection - Flag", "Lookup - Flag",
                       "Adjusted Lookup - Flag", "User Active - Flag")}
    assert len(set(_sums.values())) > 1, (
        f"flag columns must not all sum to the same total -- got {_sums}")

    assert 1000 <= synth_value("Flagship Store Count", "Int64.Type", 1), (
        "\\bflag\\b must not misfire on a column that merely CONTAINS the substring 'flag'")
    assert synth_value("Flagship Store Count", "Int64.Type", 1) not in (0, 1), (
        "\\bflag\\b must not misfire on a column that merely CONTAINS the substring 'flag'")

    # Real corpus regression (LAC Pedidosya Transaction Profile): a downstream calculated column
    # slices this Int64 raw column via LEFT(x,4)&"-"&RIGHT(x,2)&"-01" straight into DATEVALUE --
    # the generic 4-digit formula produced 1773, giving DATEVALUE("1773-73-01") and a hard
    # Desktop load failure, not just a wrong number. Every synthesized value must parse as a
    # real YYYYMM: a valid year in LEFT(4), a valid 01-12 month in RIGHT(2).
    for i in range(1, 25):
        v = synth_value("Cpd Month", "Int64.Type", i)
        s = str(v)
        assert len(s) == 6, (i, v)
        year, month = int(s[:4]), int(s[4:])
        assert 2000 <= year <= 2100 and 1 <= month <= 12, (i, v, year, month)
    assert synth_value("Cpd Month", "Int64.Type", 1) != synth_value("Some Other Month", "Int64.Type", 1), \
        "two month-code columns must not produce the identical series"

    # Same idiom, one shelf over (real corpus regression, NGPO Dashboard New): a TEXT raw column
    # sliced via LEFT(x,4)&"-"&((INT(RIGHT(x,1))-1)*3+1)&"-01" -- the generic "{name} {i}" text
    # formula produced "Quarter 1", whose LEFT(4)="Quar" is not a year. Every synthesized value
    # must parse as a real YYYYQ: a valid year in the first 4 chars, a real quarter digit 1-4 in
    # the last.
    for col in ("Quarter", "Prch Qtr"):
        for i in range(1, 17):
            v = synth_value(col, "text", i)
            assert len(v) == 5, (col, i, v)
            year, q = int(v[:4]), int(v[4:])
            assert 2000 <= year <= 2100 and 1 <= q <= 4, (col, i, v, year, q)
    assert synth_value("Quarter", "text", 1) != synth_value("Region", "text", 1), (
        "a quarter-code column must not be mistaken for a plain text column's "
        "'{name} {i}' echo")

    assert isinstance(synth_value("Foo", "number", 1), float)
    assert synth_value("Foo", "number", 2) > synth_value("Foo", "number", 1)
    assert synth_value("Foo", "number", 1) != synth_value("Bar", "number", 1), \
        "two decimal columns must not produce the identical series (a rate of them would be 1.00)"
    v1 = synth_value("Foo", "datetime", 1)
    v2 = synth_value("Foo", "datetime", 2)
    assert isinstance(v1, datetime.datetime) and v2 > v1
    assert synth_value("Foo", "logical", 1) is True
    assert synth_value("Foo", "logical", 2) is False
    # determinism: re-running produces the identical value, never a fresh draw
    assert synth_value("Foo", "datetime", 5) == synth_value("Foo", "datetime", 5)

    # ---- dim_cap: only role="dimension" columns fold, and only to <= dim_cap distinct values ----
    capped = {synth_value("Foo", "text", i, "dimension", 10) for i in range(1, 51)}
    assert capped == {f"Foo {n}" for n in range(1, 11)}, capped
    uncapped_measure = {synth_value("Foo", "text", i, "measure", 10) for i in range(1, 51)}
    assert len(uncapped_measure) == 50, "measures must never be capped"
    unmatched_role = {synth_value("Foo", "text", i, None, 10) for i in range(1, 51)}
    assert len(unmatched_role) == 50, "no ir.json role match must leave legacy behavior unchanged"
    assert synth_value("Foo", "text", 5) == synth_value("Foo", "text", 5, "dimension", None), (
        "dim_cap=None (the default) must be a no-op even for a dimension-role column")

    # ---- classify_source: the real 72-column LAC Pedidosya shape, condensed ----
    stub_source = (
        "\t\t\t\tlet\n"
        "\t\t\t\t\tSource = Table.FromRows({}, type table "
        '[#"Acct Prod Id" = text, Channel = text, Amt = number, '
        "Cnt = Int64.Type, Flag = logical, Dt = datetime])\n"
        "\t\t\t\tin\n"
        "\t\t\t\t\tSource\n"
    )
    kind, cols = classify_source(stub_source)
    assert kind == "stub", kind
    assert cols == [
        ("Acct Prod Id", "text"), ("Channel", "text"), ("Amt", "number"),
        ("Cnt", "Int64.Type"), ("Flag", "logical"), ("Dt", "datetime"),
    ], cols

    # ---- classify_source: a Param table's real literal rows must NEVER read as an empty stub ----
    param_source = (
        "\t\t\t\tlet\n"
        '\t\t\t\t\tSource = Table.FromRows({{"Gross Data"}, {"Dedup Data"}}, '
        "type table [Value = text])\n"
        "\t\t\t\tin\n"
        "\t\t\t\t\tSource\n"
    )
    kind2, cols2 = classify_source(param_source)
    assert kind2 == "param" and cols2 is None, (kind2, cols2)

    # ---- classify_source: an already-native partition must never be treated as a stub ----
    native_source = (
        "\t\t\t\tlet\n"
        '\t\t\t\t\tSource = Excel.Workbook(File.Contents("C:\\data.xlsx"), true)\n'
        "\t\t\t\tin\n"
        "\t\t\t\t\tSource\n"
    )
    kind3, cols3 = classify_source(native_source)
    assert kind3 == "native" and cols3 is None, (kind3, cols3)

    # ---- classify_source: no partition at all ----
    assert classify_source(None) == ("no_partition", None)

    # ---- extract_source_block: pulls the same block out of a full table's TMDL text ----
    full_table_text = (
        "table T\n"
        "\tlineageTag: 00000000-0000-0000-0000-000000000000\n"
        "\n"
        "\tcolumn Channel\n"
        "\t\tdataType: string\n"
        "\n"
        "\tpartition T = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n" + stub_source +
        "\n\tannotation PBI_ResultType = Table\n"
    )
    block = extract_source_block(full_table_text)
    assert block is not None and "Table.FromRows({}," in block

    # ---- REGRESSION: the double-header-promotion bug ----
    # Excel.Workbook(..., true) already promotes headers. An extra Table.PromoteHeaders call
    # after it is exactly the bug found manually on LAC Pedidosya, corrupting every column name.
    body = build_excel_m("/tmp/x.xlsx", "Sheet1", [("Acct Prod Id", "text"), ("Cnt", "Int64.Type")])
    assert "Table.PromoteHeaders" not in body, (
        "generated M must never call Table.PromoteHeaders after an Excel.Workbook(..., true) "
        "source -- that source already promoted headers; a second promotion corrupts every "
        "column name"
    )
    assert "Excel.Workbook(File.Contents(" in body and ", true)" in body
    assert 'Kind="Sheet"' in body
    assert '{"Acct Prod Id", type text}' in body
    assert '{"Cnt", Int64.Type}' in body

    # ---- rewrite_partition_source: replaces ONLY the source block, nothing else ----
    rewritten = rewrite_partition_source(full_table_text, body)
    assert "Table.FromRows" not in rewritten
    assert "Excel.Workbook" in rewritten
    assert rewritten.startswith("table T\n")
    assert "\tcolumn Channel\n\t\tdataType: string\n" in rewritten
    assert "annotation PBI_ResultType = Table" in rewritten

    # ---- m_unquote / tmdl_unquote round-trip the two quoting conventions this pipeline uses ----
    assert m_unquote('#"Billable BID"') == "Billable BID"
    assert m_unquote("Channel") == "Channel"
    assert tmdl_unquote("'Param Gross vs. Dedup Data'") == "Param Gross vs. Dedup Data"
    assert tmdl_unquote("'It''s Fine'") == "It's Fine"
    assert tmdl_unquote("gbo_lac_monthly.pedi") == "gbo_lac_monthly.pedi"

    # ---- coerce_hyper_value: real Python values coerced to each M type, None stays None ----
    assert coerce_hyper_value(None, "text") is None
    assert coerce_hyper_value(42, "text") == "42"
    assert coerce_hyper_value(3, "Int64.Type") == 3
    assert coerce_hyper_value(3.0, "Int64.Type") == 3
    assert coerce_hyper_value(3.7, "number") == 3.7
    assert coerce_hyper_value(True, "logical") is True
    assert coerce_hyper_value(0, "logical") is False
    assert coerce_hyper_value("true", "logical") is True
    assert coerce_hyper_value("false", "logical") is False
    dt = coerce_hyper_value(datetime.date(2020, 1, 2), "datetime")
    assert dt == datetime.datetime(2020, 1, 2)

    # ---- write_excel: byte-identical output across two runs against the same (synthetic) input ----
    # openpyxl stamps wall-clock timestamps into every .xlsx it writes; without freezing them
    # this determinism claim would be false for the workbook file itself, even though every
    # CELL VALUE is already deterministic.
    if openpyxl is not None:
        import time as _time

        cols = [("Foo", "text"), ("Bar", "Int64.Type")]
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "a.xlsx"
            write_excel(p1, [("Sheet1", cols, synthetic_rows(cols, 3))])
            bytes1 = p1.read_bytes()
            _time.sleep(1.1)  # past the zip format's 2-second mtime resolution
            p2 = Path(td) / "b.xlsx"
            write_excel(p2, [("Sheet1", cols, synthetic_rows(cols, 3))])
            bytes2 = p2.read_bytes()
            assert bytes1 == bytes2, (
                "write_excel must produce byte-identical output for identical input -- "
                "openpyxl's own wall-clock timestamps must be frozen, not left as-is"
            )

    # ---- REGRESSION: omitted --desktop-path must default to THIS RUN'S OWN real written Excel
    # path, never a hardcoded example string. A real prior run's output was found with EVERY
    # wired table across an entire 6-workbook corpus pointed at the identical, made-up
    # "C:/Users/analyst/Desktop/PBIP/DummyData.xlsx" -- a path matching neither the real
    # generated filename nor its real location, and identical regardless of which workbook
    # actually owned which file. Exercise the full process_project() path (not just
    # build_excel_m() in isolation) for two DIFFERENTLY-NAMED synthetic projects, so a shared
    # hardcoded constant would be caught even if it happened to coincide with one of them.
    if openpyxl is not None:
        _PLACEHOLDER_SHAPES = (
            "C:/Users/analyst/Desktop/PBIP/DummyData.xlsx",
            "analyst/Desktop",
            "DummyData.xlsx",
        )

        def _make_stub_project(root, workbook_name, table_name="T"):
            sm = root / workbook_name / f"{workbook_name}.SemanticModel" / "definition" / "tables"
            sm.mkdir(parents=True)
            (sm / f"{table_name}.tmdl").write_text(
                "table " + table_name + "\n"
                "\tlineageTag: 00000000-0000-0000-0000-000000000000\n"
                "\n"
                "\tcolumn Foo\n"
                "\t\tdataType: string\n"
                "\n"
                "\tpartition " + table_name + " = m\n"
                "\t\tmode: import\n"
                "\t\tsource =\n"
                "\t\t\t\tlet\n"
                "\t\t\t\t\tSource = Table.FromRows({}, type table [Foo = text])\n"
                "\t\t\t\tin\n"
                "\t\t\t\t\tSource\n"
                "\n\tannotation PBI_ResultType = Table\n",
                encoding="utf-8",
            )
            return root / workbook_name

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_a = _make_stub_project(root / "src", "Workbook Alpha")
            src_b = _make_stub_project(root / "src", "Workbook Beta")
            out_a = root / "out" / "Workbook Alpha"
            out_b = root / "out" / "Workbook Beta"

            process_project(src_a, out_a, 2, None)   # desktop_path_arg=None -- the omitted case
            process_project(src_b, out_b, 2, None)

            tmdl_a = (out_a / "Workbook Alpha.SemanticModel" / "definition" / "tables" /
                      "T.tmdl").read_text(encoding="utf-8")
            tmdl_b = (out_b / "Workbook Beta.SemanticModel" / "definition" / "tables" /
                      "T.tmdl").read_text(encoding="utf-8")

            for shape in _PLACEHOLDER_SHAPES:
                assert shape not in tmdl_a, (
                    f"omitted --desktop-path must never fall back to a hardcoded placeholder "
                    f"path -- found {shape!r} in Workbook Alpha's rewritten source"
                )
                assert shape not in tmdl_b, (
                    f"omitted --desktop-path must never fall back to a hardcoded placeholder "
                    f"path -- found {shape!r} in Workbook Beta's rewritten source"
                )

            # Each project's own default must be traceable to ITS OWN real written Excel file --
            # this run's actual resolved path -- not a shared/generic constant.
            expected_a = (out_a / "Workbook Alpha_dummy_data.xlsx").resolve().as_posix()
            expected_b = (out_b / "Workbook Beta_dummy_data.xlsx").resolve().as_posix()
            assert expected_a in tmdl_a, (
                "Workbook Alpha's default desktop-path must be this run's own resolved path to "
                "the Excel file it actually wrote"
            )
            assert expected_b in tmdl_b, (
                "Workbook Beta's default desktop-path must be this run's own resolved path to "
                "the Excel file it actually wrote"
            )
            # And the two projects' defaults must differ -- proof neither is a shared constant.
            assert expected_a != expected_b

    # ---- REALISTIC MODE self-test: real sampling, per-column fallback, differently-named mapping.
    # Uses a tiny FIXTURE .hyper (built via tableauhyperapi's own write API) so this never depends
    # on the real corpus being present, and stays fast/deterministic.
    if _HYPERAPI_AVAILABLE and openpyxl is not None:
        from tableauhyperapi import SqlType

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scratch = td / "scratch"
            scratch.mkdir()

            # A fixture extract with 3 real rows: source columns "acct_id" (text) and
            # "cnt_raw" (int) -- "cnt_raw" is deliberately mapped to a DIFFERENT display name
            # ("Cnt Nice Name") below, to exercise the display-name != source-name case.
            known_rows = [["ACCT-REAL-1", 111], ["ACCT-REAL-2", 222], ["ACCT-REAL-3", 333]]
            hyper_path = _make_fixture_hyper(
                scratch, "t1",
                [("acct_id", SqlType.text), ("cnt_raw", SqlType.big_int)],
                known_rows,
            )
            archive_dir = td / "corpus"
            archive_dir.mkdir()
            archive_path = archive_dir / "Fixture Workbook.twbx"
            dbname = "Data/Extracts/fixture.hyper"
            _make_fixture_archive(archive_path, hyper_path, dbname)

            resolver = RealDataResolver(str(archive_dir), None, sample_pct=100, max_rows=200)
            try:
                # ---- ir_table WITH a resolvable hyper connection ----
                ir_table_real = {
                    "provenance": "twb",
                    "connections": [{"class": "hyper", "dbname": dbname}],
                    "columns": [
                        {"name": "Acct Id", "source_column": "acct_id"},
                        {"name": "Cnt Nice Name", "source_column": "cnt_raw"},
                        {"name": "Calc Field", "source_column": ""},   # no physical source
                    ],
                }
                result = resolver.sample_real_table(ir_table_real, "Fixture Workbook")
                assert result["ok"], result
                assert result["source_display"] == f"Fixture Workbook.twbx!{dbname}"
                assert len(result["rows"]) == 3
                # Real rows must be a SUBSET of the known fixture values, never synthetic-shaped
                # ("<ColumnName> <i>" text / "1000+i" int) -- prove it by checking the actual
                # sampled cell values against the literal fixture rows.
                acct_idx = result["colindex"]["acct_id"]
                sampled_accts = {row[acct_idx] for row in result["rows"]}
                assert sampled_accts == {"ACCT-REAL-1", "ACCT-REAL-2", "ACCT-REAL-3"}, sampled_accts
                assert not any(v.startswith("Acct Id ") for v in sampled_accts if
                               isinstance(v, str))

                # ---- full column-resolution: display name != source name works correctly ----
                columns = [("Acct Id", "text"), ("Cnt Nice Name", "Int64.Type"),
                           ("Calc Field", "text")]
                rows_data, col_detail, n_rows = _resolve_table_columns(
                    ir_table_real, columns, result)
                assert n_rows == 3
                by_name = {cd["name"]: cd for cd in col_detail}
                assert by_name["Acct Id"]["source"] == "real"
                assert by_name["Cnt Nice Name"]["source"] == "real"
                assert by_name["Calc Field"]["source"] == "synthetic", (
                    "a column with no source_column must fall back to synthetic, never a "
                    "formula evaluation"
                )
                # The "Cnt Nice Name" column's VALUES must be the real cnt_raw values (111/222/333)
                # read via the DIFFERENT source column name, not the display name.
                cnt_col_idx = [i for i, (n, _) in enumerate(columns) if n == "Cnt Nice Name"][0]
                cnt_values = {row[cnt_col_idx] for row in rows_data}
                assert cnt_values == {111, 222, 333}, cnt_values
                # The synthetic-fallback column must use the ORDINARY synth_value() shape.
                calc_col_idx = [i for i, (n, _) in enumerate(columns) if n == "Calc Field"][0]
                calc_values = [row[calc_col_idx] for row in rows_data]
                assert calc_values == [synth_value("Calc Field", "text", i) for i in (1, 2, 3)]

                # ---- fallback activation: a table with NO hyper connection at all ----
                ir_table_none = {"provenance": "twb", "connections": [], "columns": []}
                result_none = resolver.sample_real_table(ir_table_none, "Fixture Workbook")
                assert not result_none["ok"]
                assert "no `hyper`-class connection" in result_none["reason"]

                # ---- fallback activation: a hyper connection whose dbname isn't in any archive ----
                ir_table_missing = {
                    "provenance": "twb",
                    "connections": [{"class": "hyper", "dbname": "Data/Extracts/nope.hyper"}],
                    "columns": [],
                }
                result_missing = resolver.sample_real_table(ir_table_missing, "Fixture Workbook")
                assert not result_missing["ok"]
                assert "not found" in result_missing["reason"]

                # ---- REGRESSION: a multi-table extract (a real UNION/federated datasource's
                # shape) must pick the table matching the KNOWN source_columns, never
                # table_names[0] unconditionally -- the exact bug a real corpus workbook surfaced
                # (a near-empty 1-column "leftover" table picked over the real 20-column one).
                multi_hyper = _make_fixture_hyper_multi(
                    scratch, "m1",
                    [
                        ("ZZZ_leftover_sheet", [("orphan_col", SqlType.text)],
                         [["should never be sampled"]]),
                        ("real_data_table",
                         [("region", SqlType.text), ("amount", SqlType.big_int)],
                         [["APAC", 111], ["LATAM", 222], ["NA", 333]]),
                    ],
                )
                multi_archive = archive_dir / "Multi Workbook.twbx"
                multi_dbname = "Data/Extracts/multi.hyper"
                _make_fixture_archive(multi_archive, multi_hyper, multi_dbname)

                ir_table_multi = {
                    "provenance": "twb",
                    "connections": [{"class": "hyper", "dbname": multi_dbname}],
                    "columns": [
                        {"name": "Region", "source_column": "region"},
                        {"name": "Amount", "source_column": "amount"},
                    ],
                }
                result_multi = resolver.sample_real_table(ir_table_multi, "Multi Workbook")
                assert result_multi["ok"], result_multi
                assert "real_data_table" in result_multi["source_display"] or True  # table name
                # not in source_display by design (only archive!dbname is) -- assert via VALUES:
                region_idx = result_multi["colindex"]["region"]
                sampled_regions = {row[region_idx] for row in result_multi["rows"]}
                assert sampled_regions == {"APAC", "LATAM", "NA"}, (
                    f"must sample the real_data_table (region overlap with known columns), "
                    f"not the leftover single-column table -- got {sampled_regions}"
                )
                assert "orphan_col" not in result_multi["colindex"], (
                    "picked the wrong table -- colindex must be the REAL data table's columns, "
                    "not the leftover sheet's"
                )
            finally:
                resolver.close()
            assert not resolver._scratch.exists(), "close() must remove its own scratch directory"

            # ---- end-to-end: process_project() with --realistic wires REAL cell values into the
            # actual written Excel file, and the rewritten TMDL's Excel M references that same
            # sheet/column set -- not just the lower-level resolver in isolation.
            proj_root = td / "proj_src" / "Fixture Workbook"
            sm = proj_root / "Fixture Workbook.SemanticModel" / "definition" / "tables"
            sm.mkdir(parents=True)
            (sm / "T.tmdl").write_text(
                "table T\n"
                "\tlineageTag: 00000000-0000-0000-0000-000000000000\n"
                "\n"
                "\tcolumn 'Acct Id'\n\t\tdataType: string\n\n"
                "\tcolumn 'Cnt Nice Name'\n\t\tdataType: int64\n\n"
                "\tcolumn 'Calc Field'\n\t\tdataType: string\n\n"
                "\tpartition T = m\n"
                "\t\tmode: import\n"
                "\t\tsource =\n"
                "\t\t\t\tlet\n"
                '\t\t\t\t\tSource = Table.FromRows({}, type table [#"Acct Id" = text, '
                '#"Cnt Nice Name" = Int64.Type, #"Calc Field" = text])\n'
                "\t\t\t\tin\n"
                "\t\t\t\t\tSource\n"
                "\n\tannotation PBI_ResultType = Table\n",
                encoding="utf-8",
            )
            work = proj_root / "_work"
            work.mkdir()
            (work / "ir.json").write_text(json.dumps({
                "workbook": "Fixture Workbook",
                "tables": [{
                    "tableau_name": "ds1", "caption": "T", "provenance": "twb",
                    "connections": [{"class": "hyper", "dbname": dbname}],
                    "columns": [
                        {"name": "Acct Id", "source_column": "acct_id"},
                        {"name": "Cnt Nice Name", "source_column": "cnt_raw"},
                        {"name": "Calc Field", "source_column": ""},
                    ],
                }],
            }), encoding="utf-8")
            (work / "name_authority.json").write_text(
                json.dumps({"tables": {"ds1": "T"}}), encoding="utf-8")

            resolver2 = RealDataResolver(str(archive_dir), None, sample_pct=100, max_rows=200)
            try:
                out_proj = td / "proj_out" / "Fixture Workbook"
                result2 = process_project(proj_root, out_proj, 20, "C:/wherever/data.xlsx",
                                           resolver2)
                assert result2["wired"] == 1
                excel_path2 = out_proj / "Fixture Workbook_dummy_data.xlsx"
                assert excel_path2.is_file()

                wb2 = openpyxl.load_workbook(excel_path2)
                ws2 = wb2[wb2.sheetnames[0]]
                header = [c.value for c in ws2[1]]
                assert header == ["Acct Id", "Cnt Nice Name", "Calc Field"], header
                acct_col = header.index("Acct Id")
                acct_cells = {ws2.cell(row=r, column=acct_col + 1).value for r in range(2, 5)}
                assert acct_cells == {"ACCT-REAL-1", "ACCT-REAL-2", "ACCT-REAL-3"}, acct_cells

                report_text = (out_proj / "DUMMY_DATA_REPORT.md").read_text(encoding="utf-8")
                assert "REAL values sampled" in report_text
                assert "Calc Field" in report_text and "synthetic" in report_text
            finally:
                resolver2.close()

            # ---- REGRESSION: HyperProcess must never leave a stray hyperd.log in the CURRENT
            # WORKING DIRECTORY -- found manually running this module's own realistic-mode logic:
            # HyperProcess()'s default log_dir is the caller's cwd, not $TMPDIR and not this
            # resolver's own scratch dir, which would otherwise leave multi-MB debris behind in
            # whatever directory a user happened to invoke this script from.
            assert not (Path.cwd() / "hyperd.log").exists(), (
                "RealDataResolver must redirect HyperProcess's own log_dir away from the "
                "caller's cwd (pass parameters={'log_dir': ...}) -- a stray hyperd.log turned "
                "up in the current working directory"
            )

    # ---- relationships.tmdl parsing: real key-based-join shape (bare table, quoted column) ----
    _rel_text = (
        "relationship 21c1df50-2e3f-4567-8901-2a3b4c5d6e7f\n"
        "\tfromColumn: bdr_lead_meetings_tableau.'Leadid (Bdr Lead Meetings Tableau)'\n"
        "\ttoColumn: bdr_lead_global_tableau.Leadid\n"
        "\n"
        "relationship 159fb8d8-c48a-4f8f-a2dd-2c429acb4b8b\n"
        "\tfromColumn: 'Table With Space'.'Col A'\n"
        "\ttoColumn: Other.Col\n"
    )
    _edges = parse_relationships_tmdl(_rel_text)
    assert _edges == [
        ("bdr_lead_meetings_tableau", "Leadid (Bdr Lead Meetings Tableau)",
         "bdr_lead_global_tableau", "Leadid"),
        ("Table With Space", "Col A", "Other", "Col"),
    ], _edges

    # ---- FK linkage: a "many" table's key column must be REAL values drawn from the "one"
    # table's own generated key column, not its own independent name-derived series ----
    with tempfile.TemporaryDirectory() as _td:
        _src = Path(_td) / "src"
        _sm = _src / "T.SemanticModel" / "definition" / "tables"
        _sm.mkdir(parents=True)
        _one_tmdl = ("table One\n\tlineageTag: x\n\n\tcolumn Id\n\t\tdataType: string\n\n"
                    "\tpartition One = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
                    "\t\t\t\t\tSource = Table.FromRows({}, type table [Id = text])\n"
                    "\t\t\t\tin\n\t\t\t\t\tSource\n")
        _many_tmdl = ("table Many\n\tlineageTag: y\n\n\tcolumn 'One Id'\n\t\tdataType: string\n\n"
                     "\tpartition Many = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
                     '\t\t\t\t\tSource = Table.FromRows({}, type table [#"One Id" = text])\n'
                     "\t\t\t\tin\n\t\t\t\t\tSource\n")
        (_sm / "One.tmdl").write_text(_one_tmdl, encoding="utf-8")
        (_sm / "Many.tmdl").write_text(_many_tmdl, encoding="utf-8")
        (_sm.parent / "relationships.tmdl").write_text(
            "relationship r1\n\tfromColumn: Many.'One Id'\n\ttoColumn: One.Id\n", encoding="utf-8")
        (_sm.parent / "database.tmdl").write_text("database\n", encoding="utf-8")
        (_src / "T.SemanticModel" / "definition.pbism").write_text('{"version":"4.2"}',
                                                                   encoding="utf-8")
        _out = Path(_td) / "out"
        _result = process_project(_src, _out, rows=5, desktop_path_arg="X:\\fake.xlsx")
        assert _result.get("wired") == 2, _result
        _wb = openpyxl.load_workbook(_out / "src_dummy_data.xlsx", read_only=True, data_only=True)
        _one_vals = {row[0].value for row in _wb["One"].iter_rows(min_row=2)}
        _many_vals = {row[0].value for row in _wb["Many"].iter_rows(min_row=2)}
        assert _many_vals, "Many sheet must have generated values"
        assert _many_vals <= _one_vals, (
            f"every 'Many' key value must be drawn from 'One''s real generated values -- "
            f"got Many={_many_vals!r} not contained in One={_one_vals!r}")

    print("generate_dummy_data.py: self-test OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.exit(_self_test())
    sys.exit(main())
