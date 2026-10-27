#!/usr/bin/env python3
"""
S0 + S1 -- ingest a Tableau deliverable and extract it to a typed IR.

Implements stages S0 (Ingest & Census) and S1 (Extract) of
docs/translation-architecture-plan.md, deterministic tier only.

The governing rule of S1:

    Completeness is the parser's job and the parser must be unable to lie about it.

Concretely: the IR has an explicit slot per asset class. Anything extracted lands in a slot or in
`unsupported` with a reason. Nothing is dropped silently.
"""
import os
import re
import json
import zipfile
import collections
import xml.etree.ElementTree as ET

# Tableau's six datatypes -> TOM dataType enum (pbip-schema.md "Column Data Types")
DTYPE = {"string": "string", "integer": "int64", "real": "double", "date": "date",
         "datetime": "dateTime", "boolean": "boolean", "spatial": "string"}

# A connection class that means "no physical source here, only a server address".
PROXY_CLASSES = {"sqlproxy", "datasource-proxy"}
# Local-engine classes: these indicate an extract, never a live source.
ENGINE_CLASSES = {"hyper", "dataengine", "tde"}


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


# Tableau plumbing that appears in <metadata-record> alongside real columns. None of it is data:
#   __tableau_internal_object_id__  the logical-layer object key, one per federated object
#   :Measure Names / :Measure Values  the pseudo-fields behind Tableau's measure-pivot shelf
# Emitting them produces columns with no meaning, and the object-id one carries a `].[` in its
# name that mangles every downstream reference. Dropped and counted, never silently discarded.
INTERNAL_MARKERS = ("__tableau_internal_object_id__", ":measure names", ":measure values",
                    "__tableau_internal_")

# A <metadata-record> describes a REAL physical column under BOTH class='column' (Tableau's
# default-dimension schema guess) and class='measure' (Tableau's default-measure guess) -- the
# class is Tableau's own aggregation-role heuristic from SQL introspection, not a signal of
# whether the record is a real column. Only other classes (e.g. 'capability') are non-column
# metadata. Filtering to class='column' alone silently drops every real numeric/summable column
# that never got an explicit <column> override -- confirmed on WorkbookB Dashboard's vwOI_Insights
# object, where 2 real columns (both class='measure', no override) were missing from every
# relationship's join-key attribution, which starved split_federated_datasource()'s all-or-nothing
# eligibility check and collapsed 14 real tables into 1.
METADATA_RECORD_COLUMN_CLASSES = {"column", "measure"}


def is_internal_field(*names):
    for n in names:
        low = str(n or "").lower()
        if any(mark in low for mark in INTERNAL_MARKERS):
            return True
    return False


def load_xml(path):
    """Parse a .twb/.tds tolerating a stray leading character (some exports start with '/')."""
    txt = open(path, encoding="utf-8", errors="replace").read().lstrip("/").lstrip()
    return ET.fromstring(txt)


def find_twb(folder):
    """Locate a .twb inside a folder.

    NOT glob: the folder name derives from the workbook filename and is data, not a pattern.
    `[VRM] Compliance Dashboard` is a real workbook in this corpus and `[VRM]` is a valid glob
    character class, which would silently match nothing and drop the workbook with no error.
    """
    for n in sorted(os.listdir(folder)):
        if n.lower().endswith(".twb") and os.path.isfile(os.path.join(folder, n)):
            return os.path.join(folder, n)
    return None


def unpack(src, workdir):
    """Unpack a .twbx (or accept a bare .twb) and return (twb_path, package_dir).

    Zip-ness is decided by SNIFFING THE BYTES (`zipfile.is_zipfile`), never by the file
    extension. A real corpus workbook shipped as `WorkbookB Dashboard.twbx` is, on disk, plain
    `<workbook>` XML with no ZIP wrapper at all -- confirmed by parsing it directly with
    `ET.fromstring` and finding a real `<workbook version=...>` root, not a truncated/corrupt
    archive. Trusting `.lower().endswith(".twb")` alone made that indistinguishable from a
    genuinely broken archive: `zipfile.ZipFile()` raised `BadZipFile` and aborted the whole
    build with no path to the legitimate XML one line away. Sniffing first means a `.twb` that
    happens to actually BE a zip (unusual, but nothing here rules it out) and a `.twbx` that
    ISN'T one both resolve correctly, and a genuinely corrupt archive still fails loudly --
    `is_zipfile` returning False on truncated garbage falls through to `load_xml`, whose own
    `ET.ParseError` on non-XML content is the honest failure, not a silent misread.
    """
    os.makedirs(workdir, exist_ok=True)
    if not zipfile.is_zipfile(src):
        return src, os.path.dirname(src)
    with zipfile.ZipFile(src) as z:
        z.extractall(workdir)
    twb = find_twb(workdir)
    if not twb:
        raise SystemExit(f"[fatal] no .twb inside {src}")
    return twb, workdir


def ancestors(root):
    """{element: [ancestor tags, nearest first]}.

    Needed because the same tag means different assets depending on where it sits: `<filter>`
    serves datasource filters, worksheet filters and saved-view state; `<sort>` is one of seven
    sort spellings split across datasource and worksheet scope. Discriminating by tag name alone
    under-reports by hundreds of instances.
    """
    parent = {c: p for p in root.iter() for c in p}
    out = {}
    for e in root.iter():
        chain, cur = [], e
        while cur in parent:
            cur = parent[cur]
            chain.append(cur.tag)
        out[e] = chain
    return out


# ---------------------------------------------------------------- formula classification
LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)", re.I)
_STRIP_STR = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_STRIP_BRACKETS = re.compile(r"\[[^\]]*\]")
TABLECALC_RE = re.compile(
    r"\b(WINDOW_\w+|RUNNING_\w+|LOOKUP|PREVIOUS_VALUE|INDEX|RANK\w*|FIRST|LAST|SIZE)\s*\(",
    re.I)
# TOTAL(<agg-expr>) is deliberately EXCLUDED from the set above: unlike WINDOW_*/RUNNING_*/
# LOOKUP/RANK/FIRST/LAST/SIZE, it carries no row-order or partition semantics -- Tableau requires
# its argument to already be a complete aggregate, and DAX's own measures already recompute over
# "the current context" the same way, so dax.py's translator handles it directly (a pure no-op
# unwrap) instead of it being blanket-routed to the addressing-needed H tier. If a formula mixes
# TOTAL with a genuinely addressing-needing function (e.g. `TOTAL(LOOKUP(...))`), that OTHER
# function's own name still matches TABLECALC_RE above, so it still correctly routes to H.


def classify_formula(f):
    """Route a formula to a tier. Deterministic; the parser still fails closed afterwards.

    Table-calc detection requires a function CALL (name + '('), matched outside string literals
    AND outside bracketed field references -- a real Tableau function call never appears inside
    `[...]`, which is only ever a field name. Without stripping brackets too, a field whose
    DISPLAY NAME happens to contain a table-calc word followed by `(` -- e.g. `[Rank (Overall)]`,
    `[Total (2019)]`, `[First (copy)]` -- would false-positive route a plain scalar formula
    referencing that field into the `tablecalc`/H tier (or `lod` for a FIXED-like bracket
    collision) instead of `scalar`/D+, exactly the same false-positive class fixed in
    `dax.is_aggregate` for the identical raw-regex-over-unstripped-brackets pattern (see that
    function's docstring for the real corpus workbook that surfaced it).
    """
    stripped = _STRIP_STR.sub("", f or "")
    stripped = _STRIP_BRACKETS.sub("", stripped)
    if TABLECALC_RE.search(stripped):
        return "tablecalc"          # -> H (signature registry)
    if LOD_RE.search(stripped):
        return "lod"                # -> D+ for simple FIXED, else L1
    return "scalar"                 # -> D+


_PERCENT_FORMAT_RE = re.compile(r"^p(\d+)(?:\.(0+))?%$")
# Matches a worksheet-local <column> formula that is EXACTLY a bare pass-through alias,
# e.g. `([Calculation_868913308838137858])` -- nothing else, no arithmetic, no IF.
_WRAPPER_ALIAS_RE = re.compile(r"^\(\[([^\]]+)\]\)$")


def translate_number_format(default_format):
    """Tableau's own `default-format` attribute on a calc's `<column>` (e.g. `p0.00%`) -> a
    Power BI `formatString`, for the ONE pattern class actually verified against this corpus:
    percentage with N decimal places. 58 real fields across achc alone carry `default-format`,
    all `p0.00%` -- confirmed this always sits on a real calculated field (`<calculation>`
    present), never a plain pass-through column.

    Deliberately narrow: Tableau's number-format mini-language also covers currency, custom
    units (K/M/B), thousands separators, negative-number parens, and dates, none of which are
    documented or verified here. `tmdl.py`'s own `_measure` docstring already explains why a
    wrong formatString is worse than none -- this function extends that same rule to translation
    rather than relaxing it: anything outside the one verified pattern returns None, exactly the
    same "no confident data, no answer" behavior as today, for every OTHER pattern.
    """
    m = _PERCENT_FORMAT_RE.match(default_format or "")
    if not m:
        return None
    decimals = len(m.group(2)) if m.group(2) else 0
    return "0." + "0" * decimals + "%" if decimals else "0%"


def parse_folders(ds):
    """<folders-common>/<folder>/<folder-item> -> {tableau_name: folder_path}.

    Tableau's Data-pane folders have a real Power BI equivalent (TMDL `displayFolder`), so this
    is captured rather than dropped. Recurses into nested <folder> for sub-folders, joining with
    `\\` to match TMDL's nested-displayFolder syntax. A field listed under more than one folder
    (Tableau allows it, decorative only) keeps whichever folder is encountered FIRST -- TMDL's
    displayFolder is a single path, not a set.
    """
    folder_of = {}

    def walk(folder_el, path):
        name = folder_el.get("name") or ""
        full = f"{path}\\{name}" if path else name
        for item in folder_el.findall("folder-item"):
            if (item.get("type") or "field") != "field":
                continue
            ident = item.get("name")
            if ident and ident not in folder_of:
                folder_of[ident] = full
        for child in folder_el.findall("folder"):
            walk(child, full)

    for fc in ds.findall(".//folders-common"):
        for f in fc.findall("folder"):
            walk(f, "")
    return folder_of


def extract_datasource(ds, anc, provenance, name_map=None):
    """One <datasource> -> an IR table record. Works identically on a .twb or a .tds root.

    Identity: `name` is the internal identifier Tableau assigns a datasource EMBEDDED in a
    workbook (`federated.0xyz`, `sqlproxy.0xyz`) and it is always present there. A STANDALONE
    `.tds`/`.tdsx` root -- what `resolve_siblings` loads for a `sqlproxy` proxy -- carries no
    `name` and no `caption` at all; the only identifying attribute on that root is
    `formatted-name` (the published datasource's display name, e.g. "MSD_Billing_Details").
    Falling back to the literal string "Data" for both `tableau_name` and `caption` when `name`
    is absent silently collapses every resolved-tds table with a generic caption onto the SAME
    allocation key -- confirmed on a real corpus workbook where two DIFFERENT tds files (two
    different .hyper extracts, two disjoint schemas) both produced tableau_name=None,
    caption="Data", so the second's table allocation silently returned the first's already
    -allocated pbi name and the first's TMDL was overwritten out of existence at emission, with
    no residue entry -- the failure this whole IR is designed to make impossible. `formatted-name`
    is the real distinguishing identifier this case was discarding; use it before falling back to
    the generic label.

    `name_map` (optional, workbook-global raw-name -> caption, built once by the caller from every
    datasource's explicit `<column caption=...>` tags): consulted ONLY by the metadata-record
    backfill below, to resolve a field THIS datasource never gives its own caption to. Real corpus
    bug (WorkbookA): `for_view` is a genuine column on 7 of 8 datasources, but only 2 of them (Key
    Highlights, Recommendations) carry an explicit `<column caption='For View' name='[for_view]'>`
    -- the other 5 (e.g. Activation) reference it ONLY via `<metadata-record>`, with no `<column>`
    tag at all. Every FILTER/shelf reference to it, anywhere in the workbook, resolves its caption
    through this same global `name_map` and gets "For View" back (borrowed from Key Highlights'
    tag) regardless of which datasource it is really on. Before this fix, the backfill below used
    the bare metadata-record name verbatim with no caption lookup, so Activation's own column
    landed in the IR named "for_view" (lowercase) -- a different string than "For View" -- so
    `by_spelling("For View", table="Activation")` could never find it locally, no matter how good
    the caller's own table hint was: the names themselves disagreed, not just which table owned
    them. This is the fix underneath `pbir.py`'s `candidate_tables` parameter, not a replacement
    for it -- that fix supplies the right table to look in; this one makes sure the right table
    actually has a matching name once it gets there.
    """
    fname = ds.get("formatted-name")
    label = ds.get("caption") or ds.get("name") or fname or "Data"
    rec = {
        "tableau_name": ds.get("name") or fname, "caption": label, "provenance": provenance,
        "connections": [], "relations": [], "columns": [], "calcs": [], "groups": [],
        "bins": [], "hierarchies": [], "aliases": {}, "datasource_filters": [],
        "extract": None, "object_graph": None, "initial_sql": [],
        "internal_dropped": [],
        # Tableau's own start-of-week/fiscal-year-start setting for THIS datasource, read from
        # <datasource>/<overridable-settings>/<date-options .../> below -- a DIRECT child of the
        # datasource itself, a SIBLING of <connection>, not nested inside it (confirmed via a
        # real parent-chain walk, not assumed from indentation). Defaults to Tableau's own
        # documented product defaults ("sunday"/"january") when a workbook never overrides them
        # -- that is a real fact about Tableau's behaviour, not a guess. Previously never read at
        # all, which is why DATEPART('weekday',...)/DATETRUNC('week',...) always declined in
        # dax.py on the theory that this setting was unrecoverable from the source file --
        # confirmed wrong: e.g. WorkbookB Dashboard's own XML states start-of-week='sunday'
        # outright, and 3 other real corpus workbooks explicitly say 'monday'.
        "date_options": {"start_of_week": "sunday", "fiscal_year_start": "january"},
    }

    _do = ds.find("overridable-settings/date-options")
    if _do is not None:
        _sow = (_do.get("start-of-week") or "").strip().lower()
        _fys = (_do.get("fiscal-year-start") or "").strip().lower()
        if _sow:
            rec["date_options"]["start_of_week"] = _sow
        if _fys:
            rec["date_options"]["fiscal_year_start"] = _fys

    for c in ds.iter("connection"):
        cls = c.get("class") or ""
        rec["connections"].append({
            "class": cls, "server": c.get("server"), "dbname": c.get("dbname"),
            "port": c.get("port"), "schema": c.get("schema"),
            "authentication": c.get("authentication"), "filename": c.get("filename"),
            "is_proxy": cls in PROXY_CLASSES, "is_engine": cls in ENGINE_CLASSES,
        })
        if (c.get("one-time-sql") or "").strip():
            # Row 7 GAP leg: Power Query has no connect-time hook. Captured so it can be
            # reported, never emitted.
            rec["initial_sql"].append(c.get("one-time-sql").strip())

    for r in ds.iter("relation"):
        t = r.get("type")
        rel = {"name": r.get("name"), "type": t, "table": r.get("table")}
        if t == "text" and (r.text or "").strip():
            rel["sql"] = r.text.strip()
        if t == "join":
            # PHYSICAL-layer join. Distinct from a logical <object-graph> relationship and it must
            # be captured separately: the corpus contains zero physical joins, so relying on
            # object-graph alone dropped a real join with no relationship AND no residue entry --
            # a silent loss, found only by running a synthetic workbook that had one.
            rel["join"] = r.get("join")
            # A join operand is usually a named/table relation, but Tableau also nests a join
            # INSIDE a join for a 3+-way physical join (a real corpus workbook's first: the prior
            # "corpus has zero physical joins" comment above no longer holds). A nested join child
            # has no `name`/`table` of its own -- `ds.iter("relation")` already visits it
            # separately and gives it its own residue entry, so this is purely the OUTER join's
            # human-readable label; `None` here crashed `' ~ '.join()` in tmdl.py's residue
            # message, not a translation decision.
            rel["operands"] = [c.get("name") or c.get("table")
                               or (f"(nested {c.get('join')} join)" if c.get("type") == "join"
                                   else "(unnamed relation)")
                               for c in r.findall("./relation")]
            rel["key_pairs"] = []
            for eq in r.iter("expression"):
                if (eq.get("op") or "") != "=":
                    continue
                kids = list(eq)
                if len(kids) != 2:
                    continue
                a, b = _join_operand(kids[0]), _join_operand(kids[1])
                if a and b:
                    rel["key_pairs"].append({"from": a, "to": b})
        rec["relations"].append(rel)

    ex = ds.find(".//extract")
    if ex is not None:
        rec["extract"] = {"enabled": (ex.get("enabled") or "").lower() == "true",
                          "count": ex.get("count"), "units": ex.get("units")}

    og = ds.find(".//object-graph")
    if og is not None:
        rec["object_graph"] = {
            "objects": [{"id": o.get("id"), "caption": o.get("caption") or o.get("id"),
                        "physical_table": _og_object_table(o)}
                        for o in og.iter("object")],
            "relationships": [parse_og_relationship(r) for r in og.iter("relationship")],
        }

    # Per-column physical-object attribution, for a federated datasource only (object_id stays
    # None on every column when there is no <object-graph> -- a single-object datasource has
    # nothing to attribute against). `<metadata-record>`'s own `<object-id>` is the authoritative
    # source: confirmed on a real corpus workbook (Digital Sales - Leads Dashboard) that EVERY
    # metadata-record column carries it (214/214), while a <column> override element carries no
    # per-object information at all -- only local-name-keyed lookup into the metadata-records
    # recovers which physical object a column (override or not) actually belongs to.
    object_id_of = {}
    if og is not None:
        for mr in ds.findall(".//metadata-record"):
            if mr.get("class") not in METADATA_RECORD_COLUMN_CLASSES:
                continue
            ln = mr.find("local-name")
            oid = mr.find("object-id")
            if ln is not None and ln.text and oid is not None and oid.text:
                object_id_of[ln.text] = oid.text.strip("[]")

    # Datasource-scope filters (row 6). Scoped to DIRECT children: a <shared-view> carries its
    # own nested <datasources>/<datasource> whose filters are saved-view state, not these.
    for f in ds.findall("./filter"):
        rec["datasource_filters"].append({"class": f.get("class"), "column": f.get("column"),
                                          "context": (f.get("context") or "").lower() == "true"})

    # --- columns, calcs, groups, bins ---
    folder_of = parse_folders(ds)
    override_names = set()
    for c in ds.findall("./column"):
        name = c.get("name")
        override_names.add(name)
        if is_internal_field(name, c.get("caption")):
            rec["internal_dropped"].append(name)
            continue
        # An explicit <column> tag with NO caption of its own still borrows the SAME workbook-
        # global `name_map` the metadata-record backfill path already consults below -- otherwise
        # this exact raw field, captioned on ANOTHER datasource (e.g. a Tableau data-blend
        # partner table), keeps its bare internal name here while every OTHER reference to it
        # workbook-wide resolves to the shared caption, so a worksheet's own field well silently
        # binds to the WRONG table's same-captioned column instead (WorkbookA: Acquisition's own
        # `[prod_id]` has no caption on its own <column> tag, Key Highlights' DOES ('Prod Id') --
        # left bare, `_resolve_field`'s global by_spelling fallback bound every Acquisition
        # worksheet's "Prod Id" field well to Key Highlights' column instead of Acquisition's own,
        # across an unrelated/non-propagating relationship, so "2.3 Issuer YoY Growth"/"2.3 Market
        # YoY Growth" rendered the identical grand-total value for every Prod Id category).
        caption = c.get("caption") or (name_map or {}).get(name) or (name or "").strip("[]")
        calc = c.find("calculation")
        al = {a.get("key", "").strip('"'): a.get("value")
              for a in c.findall(".//alias") if a.get("key") is not None}
        if al:
            rec["aliases"][caption] = al

        if calc is not None and calc.get("class") == "categorical-bin":
            mapping = {}
            for b in calc.findall("bin"):
                mapping[(b.get("value") or "").strip('"')] = [
                    v.text.strip('"') for v in b.findall("value") if v.text]
            rec["groups"].append({"name": caption, "tableau_name": name,
                                  "source": calc.get("column", "").strip("[]"),
                                  "mapping": mapping,
                                  "datatype": DTYPE.get(c.get("datatype"), "string"),
                                  "folder": folder_of.get(name),
                                  "object_id": object_id_of.get(name)})
        elif calc is not None and calc.get("class") == "bin":
            rec["bins"].append({"name": caption, "tableau_name": name,
                                "source": calc.get("column", "").strip("[]"),
                                "size": calc.get("decimal") or calc.get("size"),
                                "datatype": DTYPE.get(c.get("datatype"), "string"),
                                "folder": folder_of.get(name),
                                "object_id": object_id_of.get(name)})
        elif calc is not None and calc.get("formula") is not None:
            f = calc.get("formula")
            tc = calc.find("table-calc")
            rec["calcs"].append({
                "name": caption, "tableau_name": name, "formula": f,
                "datatype": DTYPE.get(c.get("datatype"), "string"),
                "role": c.get("role") or "dimension",
                "kind": classify_formula(f),
                "tc_addressing": tc.get("ordering-type") if tc is not None else None,
                "tc_spec": table_calc_spec(tc),
                "description": desc_of(c),
                "folder": folder_of.get(name),
                "object_id": object_id_of.get(name),
                "format_string": translate_number_format(c.get("default-format")),
            })
        else:
            rec["columns"].append({
                "name": caption, "tableau_name": name,
                "datatype": DTYPE.get(c.get("datatype"), "string"),
                "role": c.get("role") or "dimension",
                "hidden": (c.get("hidden") or "").lower() == "true",
                "semantic_role": semantic_role_of(c),
                "source_column": (name or "").strip("[]"),
                "from_metadata": False,
                "folder": folder_of.get(name),
                "object_id": object_id_of.get(name),
            })

    # Backfill from <metadata-record>: a pass-through column that never needed a caption or role
    # override has NO <column> element at all, so scanning <column> alone silently loses real
    # columns. This is the authoritative physical schema.
    seen = {c["name"] for c in rec["columns"]} | {c["name"] for c in rec["calcs"]}
    for mr in ds.findall(".//metadata-record"):
        if mr.get("class") not in METADATA_RECORD_COLUMN_CLASSES:
            continue
        ln = mr.find("local-name")
        if ln is None or not ln.text:
            continue
        if ln.text in override_names:
            continue
        if is_internal_field(ln.text):
            rec["internal_dropped"].append(ln.text)
            continue
        clean = ln.text.strip("[]")
        if clean in seen:
            continue
        seen.add(clean)
        lt = mr.find("local-type")
        cap = (name_map or {}).get(ln.text) or clean
        rec["columns"].append({
            "name": cap, "tableau_name": ln.text,
            "datatype": DTYPE.get(lt.text if lt is not None else None, "string"),
            # refined later from worksheet aggregation usage; Tableau's own metadata-record class
            # is real evidence though -- a 'measure' record is never a dimension by default.
            "role": "measure" if mr.get("class") == "measure" else "dimension",
            "hidden": False, "semantic_role": None,
            "source_column": clean, "from_metadata": True,
            "folder": folder_of.get(ln.text),
            "object_id": object_id_of.get(ln.text),
        })

    for dp in ds.findall(".//drill-path"):
        levels = [f.text.strip("[]") for f in dp.findall("field") if f.text]
        if levels:
            rec["hierarchies"].append({"name": dp.get("name"), "levels": levels})

    return rec


_FIELD_REF = re.compile(r"\[([^\[\]]+)\]")
_CROSS = "__cross_object__"


def split_federated_datasource(rec):
    """One federated `rec` (multiple `object_graph["objects"]`) -> a list of per-object recs with
    a REAL relationship between them, when every relationship in the graph gives enough to build
    one without guessing; otherwise `[rec]` unchanged (today's flatten-and-decline behaviour).

    Splitting is corpus-agnostic and fails closed exactly like every other stage here: it never
    partially splits. Every one of these must hold, or nothing splits and `tmdl.py`'s existing
    per-object-graph decline in `_relationships()` fires exactly as before:
      1. No relationship is a `cross_join` (a Tableau '1 = 1' relate has no real key at all).
      2. Every relationship's key field is a REAL column, attributed (via `<metadata-record>`'s
         own `<object-id>`, captured onto each column as `object_id` above) to the exact two
         objects the relationship names -- not merely present somewhere in the datasource.
      3. At least one side of every relationship carries Tableau's own `unique-key` declaration --
         the real cardinality is stated, never inferred from row counts we do not have.
      4. Every object is reachable from every other object through these relationships (a real
         spanning tree) -- a split with an orphan object would leave that object's columns in a
         table nothing else can join to.

    A calculated field (no single physical object of its own) is homed on whichever ONE object
    every physical column and every OTHER calc it transitively references belongs to. A field with
    no resolvable reference at all (a bare constant, or one that only references a Parameter) is
    unconstrained and defaults to the PRIMARY object -- the one that is never on the "many" side of
    any relationship, matching how it would have anchored the flattened table today.

    A field whose references resolve to MORE than one distinct object is not automatically a gap.
    `_find_hub()` looks for a single object O such that every one of those distinct objects is
    either O itself or reachable from O by ONE many-to-one hop (O is the "many" side, the
    referenced object is the "one" side) -- i.e. a row on O can RELATED() to each of them directly.
    In a real star (one fact-shaped "many" object joined to several "one"-side dimension objects,
    WorkbookB Dashboard's `Insights` being the corpus example), that hub always exists whenever the
    referenced objects are dimensions the fact object touches, even though NONE of them is
    `primary` (primary is a one-side leaf itself, never the fact object -- RELATED() from a
    one-side leaf to another one-side leaf is never valid, only from the many side inward). When a
    hub is found, the calc is rehomed onto it (not `primary`) and `cross_table_refs` records which
    named references need a `RELATED('OtherTable'[Column])` wrap at translation time, in
    `tmdl.py.resolve_calcs()`, instead of declining.

    When no such hub exists (the referenced objects have no common many-to-one anchor -- e.g. two
    "one"-side leaves that only share a distant many-side neighbor NEITHER of them can safely
    RELATED() into), this is a real, un-guessable gap: the calc survives on the primary object's
    table, but flagged `cross_object_note` so `tmdl.py.resolve_calcs()` can decline it immediately,
    with a formula and a reason, rather than let its normal resolver walk fall through to a
    same-name column that has moved to a sibling table and silently emit a same-table-looking DAX
    reference that is actually wrong once the tables are no longer one.

    Confirmed against a real corpus workbook (Digital Sales - Leads Dashboard): 3 objects, 2
    qualifying relationships (a real star, hub object never on the "many" side), 214 physical
    columns cleanly attributed 1:1, and exactly 4 of 111 calcs are genuinely cross-object (`AANR`,
    `Pipeline`, `Pipeline (1)`, `Regions for Funnel` -- each mixes a global-object field with a
    meetings- or opportunity-object field in one formula) and decline correctly rather than
    silently mis-resolving.
    """
    og = rec.get("object_graph")
    if not og or len(og["objects"]) < 2:
        return [rec]

    col_object = {(c.get("tableau_name") or "").strip("[]"): c.get("object_id")
                  for c in rec["columns"] if c.get("object_id")}
    obj_ids = {o["id"] for o in og["objects"]}
    id_to_tableau_name = {o["id"]: o["caption"] for o in og["objects"]}

    edges, many_side = [], set()
    for rel in og["relationships"]:
        if rel.get("cross_join"):
            return [rec]
        keys = rel.get("key_pairs") or []
        if not keys:
            return [rec]
        from_obj, to_obj = rel.get("from_object"), rel.get("to_object")
        if from_obj not in obj_ids or to_obj not in obj_ids:
            return [rec]
        if not (rel.get("from_unique") or rel.get("to_unique")):
            return [rec]          # cardinality genuinely unknown -- do not guess it
        # Every key field named on EITHER side must attribute to THAT side's own object -- not
        # merely exist somewhere in the datasource (a same-named field on the wrong object would
        # silently relate the wrong pair of columns).
        ok = True
        for k in keys:
            if col_object.get(k["from"]) != from_obj or col_object.get(k["to"]) != to_obj:
                ok = False
                break
        if not ok:
            return [rec]
        one_obj = from_obj if rel.get("from_unique") else to_obj
        many_obj = to_obj if rel.get("from_unique") else from_obj
        many_key = next(k["to"] if rel.get("from_unique") else k["from"] for k in keys)
        one_key = next(k["from"] if rel.get("from_unique") else k["to"] for k in keys)
        edges.append({"one_obj": one_obj, "many_obj": many_obj,
                     "one_key": one_key, "many_key": many_key})
        many_side.add(many_obj)

    # Reachability: every object must be connected to the graph, or an orphan object's columns
    # would land in a table with no relationship to anything.
    adj = collections.defaultdict(set)
    for e in edges:
        adj[e["one_obj"]].add(e["many_obj"])
        adj[e["many_obj"]].add(e["one_obj"])
    start = next(iter(obj_ids))
    seen, frontier = {start}, [start]
    while frontier:
        cur = frontier.pop()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    if seen != obj_ids:
        return [rec]

    primary = next((o["id"] for o in og["objects"] if o["id"] not in many_side), None)
    if primary is None:
        return [rec]              # every object is a "many" side somewhere -- no clear hub

    # -- home resolution for calcs/groups/bins: fixed point over a reference graph -----------
    calc_by_ref = {(c.get("tableau_name") or "").strip("[]"): c for c in rec["calcs"]}
    group_bin_source = {(g.get("tableau_name") or "").strip("[]"): g["source"]
                        for g in rec["groups"] + rec["bins"]}
    memo = {}

    def home_of(ref, visiting):
        if ref in memo:
            return memo[ref]
        if ref in visiting:
            return None            # circular reference -- unresolvable, not this function's job
        if ref in col_object:
            return col_object[ref]
        if ref in calc_by_ref and ref not in group_bin_source:
            visiting.add(ref)
            homes = set()
            for m in _FIELD_REF.finditer(calc_by_ref[ref]["formula"] or ""):
                h = home_of(m.group(1), visiting)
                if h == _CROSS:
                    homes.add(_CROSS)
                elif h is not None:
                    homes.add(h)
            visiting.discard(ref)
            concrete = homes - {_CROSS}
            # A referenced calc that itself mixes more than one concrete object is not
            # automatically `_CROSS` here either -- `_find_hub` gets the SAME chance to find a
            # RELATED()-legal anchor for it that the outer per-calc loop gives a calc with direct
            # multi-object references (this is exactly how `cl.Slice By`, in a real workbook,
            # reaches its own resolvable hub even though one of ITS branches is `[Partner/Client]`
            # -- itself a 2-object calc that only resolves once THIS check exists). `_CROSS in
            # homes` still vetoes unconditionally: a genuinely unresolvable sub-reference cannot be
            # papered over by finding a hub for the concrete remainder.
            if _CROSS in homes:
                result = _CROSS
            elif len(concrete) > 1:
                hub = _find_hub(concrete)
                result = hub if hub is not None else _CROSS
            elif concrete:
                result = next(iter(concrete))
            else:
                result = None
            memo[ref] = result
            return result
        if ref in group_bin_source:
            return home_of(group_bin_source[ref], visiting)
        return None

    def cross_note(objs):
        if objs:
            capt = {o["id"]: o["caption"] for o in og["objects"]}
            names = sorted(capt.get(o, o) for o in objs)
            where = f"columns homed on {len(names)} different physical Tableau objects " \
                    f"({', '.join(names)})"
        else:
            # Ambiguity came only through a nested calc-to-calc reference, not a direct physical
            # column on this calc itself -- there is no concrete object list to name here.
            where = "a chain of calculated fields that together span more than one physical " \
                    "Tableau object"
        return (f"calculated field references {where} that a real relationship now separates "
                f"into different Power BI tables, and no single object among them can RELATED() "
                f"to every other one directly -- automatic cross-table DAX is not implemented "
                f"for this shape; hand-author this one with the relationship in mind")

    # (many_obj, one_obj) pairs a RELATED() call can legally traverse in that direction -- a row
    # on `many_obj` maps to at most one row on `one_obj`. Built once for `_find_hub` below.
    many_to_one = {(e["many_obj"], e["one_obj"]) for e in edges}

    def _find_hub(concrete_ids):
        """An object O (not necessarily `primary`) such that every id in `concrete_ids` is O
        itself or one RELATED()-legal hop away from O. In a real star -- one fact-shaped "many"
        object joined to several "one"-side dimensions -- O is that fact object, never `primary`
        (primary is a one-side leaf; RELATED() from one leaf to another leaf is never legal, only
        from the many side inward). Sorted iteration keeps the pick deterministic if more than one
        object happens to qualify."""
        for o in sorted(obj_ids):
            if all(h == o or (o, h) in many_to_one for h in concrete_ids):
                return o
        return None

    for c in rec["calcs"]:
        homes = set()
        ref_home = {}
        for m in _FIELD_REF.finditer(c["formula"] or ""):
            h = home_of(m.group(1), set())
            if h is not None:
                homes.add(h)
                if h != _CROSS:
                    ref_home[m.group(1)] = h
        concrete = homes - {_CROSS}
        # `_CROSS in homes` on its own (e.g. homes == {_CROSS}, a calc whose only reference is to
        # an already-cross-object calc) must decline too -- checking `len(concrete) > 1` alone
        # would miss it (len(concrete) is 0 or 1 here) and fall through to `next(iter(homes))`,
        # which would set `_home` to the `_CROSS` sentinel string itself: not a real object id, so
        # the calc would match NO sub-table's `c["_home"] == o["id"]` filter and vanish from every
        # split table with no residue entry -- a silent loss, not a decline. A calc with any
        # `_CROSS` reference is vetoed from hub resolution too, regardless of `concrete` --
        # `ref_home` cannot be trusted as complete when part of the reference graph is itself
        # unresolvable, so this must not silently pick a hub for the part that IS resolvable.
        if _CROSS in homes:
            c["_home"] = primary
            c["cross_object_note"] = cross_note(concrete if len(concrete) > 1 else None)
        elif len(concrete) > 1:
            hub = _find_hub(concrete)
            if hub is not None:
                c["_home"] = hub
                cross_refs = {ref: id_to_tableau_name[h]
                              for ref, h in ref_home.items() if h != hub}
                if cross_refs:
                    c["cross_table_refs"] = cross_refs
            else:
                c["_home"] = primary
                c["cross_object_note"] = cross_note(concrete)
        elif len(concrete) == 1:
            c["_home"] = next(iter(concrete))
        else:
            c["_home"] = primary

    for g in rec["groups"] + rec["bins"]:
        h = home_of((g.get("tableau_name") or "").strip("[]"), set())
        g["_home"] = h if (h and h != _CROSS) else primary

    # -- materialize one sub-table per object --------------------------------------------------
    subs = []
    for o in og["objects"]:
        sub = dict(rec)
        sub["tableau_name"] = o["caption"]
        sub["caption"] = o["caption"]
        sub["object_graph"] = None
        sub["columns"] = [c for c in rec["columns"] if col_object.get(
            (c.get("tableau_name") or "").strip("[]"), primary) == o["id"]]
        sub["calcs"] = [c for c in rec["calcs"] if c["_home"] == o["id"]]
        sub["groups"] = [g for g in rec["groups"] if g["_home"] == o["id"]]
        sub["bins"] = [b for b in rec["bins"] if b["_home"] == o["id"]]
        sub["relations"] = rec["relations"] if o["id"] == primary else []
        sub["hierarchies"] = rec["hierarchies"] if o["id"] == primary else []
        sub["aliases"] = rec["aliases"] if o["id"] == primary else {}
        sub["datasource_filters"] = rec["datasource_filters"] if o["id"] == primary else []
        sub["internal_dropped"] = rec["internal_dropped"] if o["id"] == primary else []
        sub["initial_sql"] = rec["initial_sql"] if o["id"] == primary else []
        sub["physical_table"] = o.get("physical_table")
        subs.append(sub)

    # Cleanup in ONE pass over the ORIGINAL lists, after every sub's filtering above has already
    # read `_home` for every object -- popping it per-sub instead, inside the loop above, deletes
    # the key a LATER object's own `c["_home"] == o["id"]` filter still needs to read, which
    # raises KeyError the moment a calc homed to the first object is popped before the second
    # object's filter runs over the same shared `rec["calcs"]` list.
    for c in rec["calcs"] + rec["groups"] + rec["bins"]:
        c.pop("_home", None)

    rec["_resolved_joins"] = [{
        "one_table": id_to_tableau_name[e["one_obj"]], "many_table": id_to_tableau_name[e["many_obj"]],
        "one_field": e["one_key"], "many_field": e["many_key"],
    } for e in edges]
    # Carried on the primary sub-table so a later stage that only sees the split list (not the
    # original `rec`) can still find it -- `_relationships()` looks here.
    for sub in subs:
        if sub["tableau_name"] == id_to_tableau_name[primary]:
            sub["_resolved_joins"] = rec["_resolved_joins"]
    return subs


def _og_object_table(obj_el):
    """An object-graph `<object>`'s real, queryable physical table name (e.g.
    '[global_digital_sales].[bdr_lead_meetings_tableau]'), from its own `<properties context=''>`
    relation -- the SAME name a native connector would need to query that one physical object
    independently, as opposed to the federated `[Extract].[...]` alias the `context='extract'`
    properties block carries (that alias names the one flattened extract, not this one object)."""
    for props in obj_el.findall("properties"):
        if (props.get("context") or "") != "":
            continue
        rel = props.find("relation")
        if rel is not None and rel.get("table"):
            return rel.get("table")
    return None


_QUAL = re.compile(r"^\[([^\]]+)\]\s*\.\s*\[([^\]]+)\]$")


def _join_operand(e):
    """`[Orders].[cust_id]` -> `Orders.cust_id`. A bare `.strip("[]")` leaves `Orders].[cust_id`,
    because the brackets are in the MIDDLE of a qualified reference, not only at its ends."""
    op = (e.get("op") or "").strip()
    m = _QUAL.match(op)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return op.strip("[]") if op.startswith("[") and op.endswith("]") else None


def parse_og_relationship(rel):
    """Parse one <relationship> from a logical-layer <object-graph> into key pairs.

    Tableau encodes the join predicate as a nested <expression op="..."> tree where a leaf's `op`
    is either a field reference `[Field]` or a literal. A predicate of `1 = 1` -- two literal
    leaves -- is Tableau's "relate these tables with no matching fields" cross join. It looks like
    a relationship in the XML and is NOT one: Power BI has no relationship that joins on a
    constant, so emitting a TMDL relationship for it would invent a key that does not exist.
    Recorded with `cross_join: True` so it is reported rather than translated.

    Each end-point can carry `unique-key='true'` -- Tableau's own declaration that this object's
    side of the join key is unique, i.e. the "one" side of a one-to-many. Real corpus example
    (WorkbookB Dashboard): every `<second-end-point>` on this datasource's 13 relationships carries
    it, none of the `<first-end-point>`s do -- captured per end-point rather than assumed to be on
    one fixed side, since nothing in the schema guarantees which end-point carries it.
    """
    def leaf_field(e):
        op = (e.get("op") or "").strip()
        return op.strip("[]") if op.startswith("[") and op.endswith("]") else None

    def is_unique(ep):
        return ep is not None and (ep.get("unique-key") or "").lower() == "true"

    pairs, literal_only = [], True
    for eq in rel.iter("expression"):
        if (eq.get("op") or "") != "=":
            continue
        kids = list(eq)
        if len(kids) != 2:
            continue
        a, b = leaf_field(kids[0]), leaf_field(kids[1])
        if a and b:
            pairs.append({"from": a, "to": b})
            literal_only = False
    ends = [rel.find("first-end-point"), rel.find("second-end-point")]
    return {
        "from_object": ends[0].get("object-id") if ends[0] is not None else None,
        "to_object": ends[1].get("object-id") if ends[1] is not None else None,
        "from_unique": is_unique(ends[0]),
        "to_unique": is_unique(ends[1]),
        "key_pairs": pairs,
        "cross_join": literal_only,
    }


def desc_of(col):
    d = col.find("desc")
    if d is None:
        return (col.get("description") or "").strip() or None
    txt = "".join(r.text or "" for r in d.iter("run"))
    return re.sub(r"\s+", " ", txt).strip() or None


def semantic_role_of(col):
    sr = col.find("semantic-role")
    if sr is not None and (sr.text or "").strip():
        return sr.text.strip()
    for k, v in col.attrib.items():
        if k.split("}")[-1] == "semantic-role":
            return v
    return None


FIELDREF = re.compile(r"\[[^\]]*\]\.\[(?:([a-z]+):)?([^:\]]+(?::[^\]]+)?)\]")


def _fieldref_parts(m):
    """(field, agg) from one FIELDREF match. `m.group(2)` is everything after the optional
    leading `agg:` up to the closing `]` -- every remaining colon-segment, not just the first.

    Tableau's own convention (confirmed against this corpus): the LAST segment, when 2+ remain, is
    always a kind suffix ('qk'/'nk'/...), and the field itself is always the one immediately
    before it. True for the ordinary 3-segment shape (`agg:FIELD:suffix` -> group(2)=
    'FIELD:suffix', field=parts[-2]='FIELD') AND for the 4-segment shape a Tableau quick table
    calculation PILL writes directly onto a shelf (`agg:underlying-agg:FIELD:suffix` ->
    group(2)='underlying-agg:FIELD:suffix', field=parts[-2]='FIELD') -- taking `parts[0]`
    unconditionally, as this used to, is only ever correct for the 3-segment shape; for the
    4-segment one it silently returns the underlying-agg placeholder ("sum"/"usr"/"ctd"/"cnt")
    instead of the real field, and throws the real field away in the discarded tail. Real corpus
    example: WorkbookB Dashboard's "1W - Slice By" worksheet's own `[pcto:usr:REALFIELD:qk]` cols
    ref -- REALFIELD was never recovered anywhere downstream before this.

    A 5-segment shape (`agg:underlying-agg:FIELD:kind:N`, e.g. WorkbookA's
    `[pcto:sum:Calculation_993606723128832005:qk:4]`) adds a trailing numeric partition-scope
    index -- stripping it before taking parts[-2] recovers FIELD here too.
    """
    agg_raw, rest = m.group(1), m.group(2)
    parts = rest.split(":")
    while len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    field = parts[-2] if len(parts) > 1 else parts[0]
    return field, (agg_raw if agg_raw and agg_raw != "none" else None)


def _fieldref_underlying_agg(m):
    """The 4-segment quick-table-calc shape's OWN middle segment (`agg:UNDERLYING:field:suffix`
    -- e.g. "sum" in `[pcto:sum:Calculation_X:qk]`), or None for the ordinary 3-segment/1-segment
    shape, which has no such segment at all. This is the calc PILL's own base-value aggregation
    when Tableau never wrote an explicit `aggregation=` attribute on the `<table-calc>` itself
    (confirmed real corpus shape: WorkbookB Dashboard's "1W - Slice By" PctTotal pill carries no
    `aggregation=` at all -- this segment is the ONLY place its base SUM/AVG/etc. is recorded)."""
    rest = m.group(2)
    parts = rest.split(":")
    return parts[0] if len(parts) > 2 else None


def table_calc_spec(tc):
    """The WHOLE `<table-calc>` element, not just its `ordering-type`.

    This file used to keep one attribute (`ordering-type`) and drop the rest, which made every
    table calculation look like it carried NO addressing/partitioning information at all -- so
    `tmdl.route()` sent all of them to the human tier with the reason "addressing/partitioning is
    not in the formula text". That reason is true about the FORMULA and false about the WORKBOOK:
    measured across the reference corpus, the element carries `ordering-type` (245 occurrences),
    `type` (64 -- e.g. PctTotal/Difference/PctDiff, the table-calc KIND), `ordering-field` (26),
    `level-break` (21 -- this is the PARTITIONING), `field` (9), `rank-options` (8),
    `diff-options` (4), plus `<order field=...>` children (93) naming the addressing fields
    explicitly and `<address>` children (4).

    Captured RAW here on purpose. `<order>`'s `field` is a fully-qualified Tableau reference
    (`[datasource].[none:col:nk]`) and `parse_fieldref` -- the function that normalises those --
    needs a `name_map` built from every datasource, which does not exist yet at this point in the
    parse. `extract()` resolves `order_fields_raw` into `order_fields` in a post-pass once it
    does. Nothing is interpreted here; this is transport only.

    `ws_order_fields` is populated in that SAME post-pass, from a source this function does not
    see at all: `worksheet_tc_addressing()`'s scan of every worksheet's own saved table-calc
    setting, which is a separate, often richer copy Tableau keeps per worksheet USAGE of a calc
    (see that function's docstring). Only filled in when this calc's own definition -- everything
    above -- carries no addressing of its own.
    """
    if tc is None:
        return None
    return {
        "ordering_type": tc.get("ordering-type"),
        "type": tc.get("type"),
        "field": tc.get("field"),
        "ordering_field": tc.get("ordering-field"),
        "level_break": tc.get("level-break"),
        "rank_options": tc.get("rank-options"),
        "diff_options": tc.get("diff-options"),
        "order_fields_raw": [o.get("field") for o in tc.findall("order") if o.get("field")],
        "order_fields": [],
        "ws_order_fields": [],
        "has_address": tc.find("address") is not None,
    }


def parse_fieldref(ref, name_map):
    """`[federated.HASH].[agg:FIELD:kk]` -> (caption, agg). Also correctly handles the 4+-segment
    shelf shape a quick table-calc pill writes (`[agg:underlying-agg:FIELD:kk]`, e.g.
    `[pcto:usr:SomeField:qk]`) -- see `_fieldref_parts`'s own docstring for why FIELD is always
    the second-to-last colon-segment, never the first one after the leading `agg:`."""
    if not ref:
        return None, None
    m = FIELDREF.search(ref)
    if not m:
        return ref.strip("[]"), None
    field, agg = _fieldref_parts(m)
    return name_map.get(f"[{field}]", field), agg


def _measure_names_pivot(ws, name_map):
    """The ordered list of real measures Tableau pivots into their own columns when a worksheet
    drags Measure Names onto Rows/Cols (a "text table"/crosstab of several measures side by
    side). Tableau does NOT record that list on the rows/cols shelf text itself -- that just
    says "[:Measure Names]", a pseudo-field with no column to resolve. It's recorded on a
    <filter class="categorical" column="...:Measure Names"> whose <groupfilter function="member">
    children each hold one real, fully-qualified measure reference, in pivot order. Real corpus
    example: WorkbookB Dashboard's "1W - Table" -- 10 <groupfilter function="member"> children,
    each a `"[federated...].[agg:Calculation_X:qk]"` string.

    Returns [] when this worksheet has no such filter -- never guessed at, same as every other
    decline in this pipeline. Kept separate from `classify_filter()`'s own handling of this same
    <filter> element (which still declines it as an unsupported categorical filter -- correctly:
    it restricts nothing, it SPECIFIES the pivot).
    """
    for f in ws.findall(".//filter"):
        col = f.get("column") or ""
        if ":Measure Names" not in col and ":Measure Values" not in col:
            continue
        out = []
        for gf in f.iter("groupfilter"):
            if gf.get("function") != "member":
                continue
            raw = _member_value(gf.get("member"))
            if not raw:
                continue
            cap, agg = parse_fieldref(raw, name_map)
            if cap:
                out.append({"field": cap, "agg": agg})
        return out  # one Measure Names filter is the whole pivot spec
    return []


def worksheet_tc_addressing(root, name_map):
    """{calc's bare internal name -> [{"field": caption, "worksheet": ws_name}, ...]}.

    Tableau records a SECOND, richer table-calc setting per worksheet usage, separate from the
    calc's own `<calculation><table-calc>` definition that `table_calc_spec()` above reads (which
    is often just the bare generic default, e.g. `ordering-type='Rows'`, no field at all). This
    second copy lives under `<worksheet><table><view><datasource-dependencies>
    <column-instance column='[calc-id]'><table-calc ordering-field='[ds].[real_col]'
    ordering-type='Field' /></column-instance>` -- exactly where Tableau saves "Compute Using:
    Specific Dimension(s)" when a calc is used inside a worksheet WITHOUT sitting directly on a
    Rows/Cols/Pages shelf (the case `extract_worksheets()`'s shelf scan cannot see at all). Only
    `ordering-type='Field'` entries carry a real field to resolve; every other ordering-type here
    is exactly as generic as the calc's own definition already reports, so it is skipped rather
    than repeating the same non-information under a different label.

    ONE `<column-instance>` can carry MULTIPLE `<table-calc>` SIBLING children -- Tableau's shape
    when several tightly-coupled calcs share one shelf slot (real corpus example: `Y Axis
    Position`'s column-instance holds its own addressing PLUS one entry each for the two other
    calcs it references, `AANR / Owner Axis Test` and `Normalized AANR`, neither of which is ever
    placed on a shelf itself). A `<table-calc>` WITHOUT its own `field=` attribute addresses the
    column-instance's own `column`; WITH one, `field=` -- a fully-qualified reference like
    `[ds].[Calculation_X]` -- names which OTHER calc this particular entry is for, overriding the
    column-instance's `column` as the target.
    """
    out = {}
    for ws in root.findall(".//worksheets/worksheet"):
        ws_name = ws.get("name")
        for ci in ws.findall(".//datasource-dependencies/column-instance"):
            own_cid = (ci.get("column") or "").strip("[]")
            for tc in ci.findall("table-calc"):
                if (tc.get("ordering-type") or "") != "Field":
                    continue
                of_raw = tc.get("ordering-field")
                if not of_raw:
                    continue
                target_raw = tc.get("field")
                if target_raw:
                    m = FIELDREF.search(target_raw)
                    # `_fieldref_parts` (not a raw `m.group(2)`), same reason as every other
                    # FIELDREF caller: group 2 now carries the WHOLE rest of the ref, not just
                    # its own first colon-segment, since the regex change that lets a 4-segment
                    # quick-table-calc shelf ref keep its real trailing field also widens what
                    # this group matches for a `field=` override here -- reading it raw would
                    # regress this identifier for any override ref with 2+ colon-segments.
                    cid = _fieldref_parts(m)[0] if m else None
                else:
                    cid = own_cid
                if not cid:
                    continue
                cap, _agg = parse_fieldref(of_raw, name_map)
                if not cap:
                    continue
                out.setdefault(cid, []).append({"field": cap, "worksheet": ws_name})
    return out


def _fieldref_inner(m):
    """The exact text inside a FIELDREF match's OWN second bracket pair, reconstructed from its
    two groups (`(agg:)?rest` -> `agg:rest` or bare `rest`) -- i.e. `m.group(0)` with the leading
    `[ds].[` and trailing `]` stripped off, nothing else changed. Confirmed against real corpus
    data (WorkbookB Dashboard's "3B - Days to CE Eligible") that this is the SAME string Tableau
    writes, unbracketed, as a `<column-instance name=...>` attribute -- e.g. a shelf ref
    `[ds].[pcto:sum:Calculation_X:qk]` and a column-instance `name='[pcto:sum:Calculation_X:qk]'`
    share this exact inner text.

    This -- NOT `<column-instance column=...>`, which only names which underlying field an
    instance ultimately points at -- is the real per-USE discriminator. Real corpus proof this
    distinction matters: that same worksheet carries TWO separate column-instances with the
    IDENTICAL `column='[Calculation_X]'` but different `name=` -- one plain
    (`name='[sum:Calculation_X:qk]'`, no table-calc, the one actually placed on the Rows shelf)
    and one Percent-of-Total-tagged (`name='[pcto:sum:Calculation_X:qk]'`, used elsewhere in the
    SAME worksheet, e.g. a tooltip). Keying on `column=` alone (an earlier version of this fix
    did) collapses these two into one, and wrongly tags the plain, unrelated shelf placement with
    the OTHER instance's quick-calc metadata.
    """
    agg_raw, rest = m.group(1), m.group(2)
    return (f"{agg_raw}:" if agg_raw else "") + rest


def worksheet_pcto_meta(root, name_map):
    """{worksheet name -> {column-instance `name=` inner text -> {"type":, "ordering_type":,
    "aggregation":, "ordering_field":}}} for every `<column-instance>`'s OWN `<table-calc>` child
    (no `field=`
    override -- an override means "for a DIFFERENT calc", `worksheet_tc_addressing()`'s concern,
    not this one; the two conditions -- `field=` absent here, `ordering-type='Field'` AND
    `ordering-field=` present there -- read the same elements for disjoint purposes and can
    coexist on sibling `<table-calc>` children of the same column-instance without conflict).

    This is Tableau's quick TABLE CALCULATION button applied directly to a shelf pill (Percent of
    Total, Running Total, Difference, ...) -- it never creates a named calc/formula at all, so
    NOTHING about it exists anywhere else in `ir.json` today. Captured for EVERY table-calc
    `type`, not just Percent of Total, so every other quick-calc button (Difference, Running
    Total, Moving Average, ...) inherits the same groundwork at zero extra extraction cost --
    only a consuming skill's own template selection is scoped narrower.

    Measured real `ordering-type` values co-occurring with `type='PctTotal'` across this corpus
    (`grep -roE "ordering-type='[^']*' type='PctTotal'|type='PctTotal' ordering-type='[^']*'"`
    over every `.twb`/unzipped-`.twbx` under `input/`): `Rows` (301), `Columns` (329), `Field`
    (156), `Table` (17), `RowInPane` (14), `ColumnInPane` (6), `CellInPane` (2). The first four
    name a real, addressable partition (or, for `Table`, none at all -- see the consuming skill's
    own decision rule); the `*InPane` values are relative to the VISUAL's own trellis/pane layout,
    which has no stable model-level anchor, and are not treated as safe by anything downstream --
    recorded here regardless, since this function's own job is capture, not judgment.

    Keyed by the column-instance's OWN `name=` attribute, bracket-stripped -- NOT `column=`,
    which only names which underlying field an instance ultimately points at, not which SPECIFIC
    use of it this is. Real corpus proof this distinction is load-bearing, not academic: WorkbookB
    Dashboard's "3B - Days to CE Eligible" carries two column-instances with the IDENTICAL
    `column='[Calculation_X]'` but different `name=` -- one plain (no table-calc, actually placed
    on the Rows shelf) and one Percent-of-Total-tagged (used elsewhere in the same worksheet).
    Keying on `column=` collapses the two and wrongly tags the plain, unrelated shelf placement
    with the quick-calc's own metadata. `name=`'s bracket-stripped text is exactly the same
    string `_fieldref_inner()` reconstructs from a shelf/`<encodings>` FIELDREF match, so the two
    sides compare equal only when they really are the same use.
    """
    out = {}
    for ws in root.findall(".//worksheets/worksheet"):
        ws_name = ws.get("name")
        for ci in ws.findall(".//datasource-dependencies/column-instance"):
            own_use = (ci.get("name") or "").strip("[]")
            if not own_use:
                continue
            for tc in ci.findall("table-calc"):
                if tc.get("field"):
                    continue  # targets a DIFFERENT calc -- worksheet_tc_addressing()'s concern
                if not tc.get("type"):
                    continue  # nothing to promote without a quick-calc type at all
                of_cap, _of_agg = parse_fieldref(tc.get("ordering-field"), name_map)
                out.setdefault(ws_name, {})[own_use] = {
                    "type": tc.get("type"),
                    "ordering_type": tc.get("ordering-type"),
                    "aggregation": tc.get("aggregation"),
                    "ordering_field": of_cap,
                }
    return out


def _label_font(ws):
    """A worksheet's own data-label font: `<style-rule element='datalabel'>`'s plain
    `<format attr='font-size'|'font-family' value='...'/>` children (NO `field=` attribute),
    falling back to `<style-rule element='worksheet'>` for whichever attribute `datalabel` does
    not itself specify. This is the font a Power BI `card` visual's displayed value renders in: a
    card's value IS the worksheet's own mark data-label text (Tableau's "Label" button -> "Font
    ..." on the Marks card), which is `element='datalabel'`, NOT `element='label'` -- confirmed by
    counting every field-less font-size/font-family `<format>` by its enclosing scope across a
    real corpus workbook (WorkbookA): `datalabel` and `worksheet` together account for the bulk of
    real overrides (62 of 114), while `label` has only 3, all font-size, zero font-family. A first
    implementation targeted `element='label'` on the (wrong) assumption that it meant "the mark's
    value label" -- it does not; the real scope is `datalabel`, and the worksheet-wide font (set
    once via Tableau's whole-worksheet "Format > Font" dialog, not re-stated per element unless
    overridden) is what `datalabel` itself falls back to when it specifies only one of the two
    attributes, same as Tableau's own general-to-specific cascade.

    A `field=`-scoped `<format>` on the SAME style-rule is a per-VALUE conditional-format override
    (e.g. a different font for one specific member), a different and harder shape than a
    worksheet's base font -- deliberately excluded here rather than risked as a false base-font
    read.

    A worksheet can carry more than one style-rule per scope (one per pane); the first rule to
    declare a given attribute wins, same as the DATALABEL-before-WORKSHEET cascade above (both are
    "first write wins", just over two different search passes instead of one). Returns None when
    neither attribute is found anywhere, so a worksheet with no font override at all gets no font
    (model-default rather than a fabricated one).
    """
    def _scan(element):
        size = family = None
        for sr in ws.findall(f".//style-rule[@element='{element}']"):
            for fmt in sr.findall("format"):
                if fmt.get("field"):
                    continue
                attr, val = fmt.get("attr"), fmt.get("value")
                if not val:
                    continue
                if attr == "font-size" and size is None:
                    size = val
                elif attr == "font-family" and family is None:
                    family = val
        return size, family

    size, family = _scan("datalabel")
    ws_size, ws_family = _scan("worksheet")
    size = size or ws_size
    family = family or ws_family
    return {"size": size, "family": family} if (size or family) else None


def extract_worksheets(root, name_map, pcto_meta=None):
    out = []
    for ws in root.findall(".//worksheets/worksheet"):
        ws_pcto = (pcto_meta or {}).get(ws.get("name"), {})
        mark = "Automatic"
        mk = ws.find(".//mark")
        if mk is not None and mk.get("class"):
            mark = mk.get("class")
        # Tableau's dual-axis combo idiom (two measures on the SAME Rows/Cols shelf, the second
        # promoted to its own synced axis via right-click "Dual Axis"): the worksheet-level
        # `<mark>` above is always the OUTER/default pane's own mark ('Automatic' on every real
        # corpus instance found) -- the two measures' REAL marks (one 'Bar', one 'Line') live one
        # level down, on the two IDENTIFIED sub-panes (`<pane id=...>`), each carrying its own
        # `y-axis-name` attribute naming which measure it belongs to. Confirmed on WorkbookB
        # Dashboard's "1W - Trend": `<pane><mark class='Automatic'/></pane>` (the default,
        # id-less outer pane) alongside `<pane id='1' y-axis-name='...'><mark class='Bar'/></pane>`
        # and `<pane id='2' y-axis-name='...'><mark class='Line'/></pane>`. Only id-bearing panes
        # are captured -- the id-less default pane carries no per-measure information at all.
        panes = []
        for p in ws.findall(".//pane"):
            if not p.get("id"):
                continue
            pm = p.find("mark")
            y_field, _ = parse_fieldref(p.get("y-axis-name"), name_map)
            panes.append({"id": p.get("id"), "mark": pm.get("class") if pm is not None else None,
                          "y_axis_field": y_field})
        encodings, shelves = [], {}
        for shelf in ("rows", "cols", "pages"):
            e = ws.find(f".//{shelf}")
            refs = []
            if e is not None and e.text:
                # Tableau writes 2+ fields stacked on one shelf as an arbitrarily-deep nested
                # `(A / (B / (C / D)))` expression, not a flat delimiter list -- confirmed against
                # this corpus's own `<rows onTop='true' total='true'>` shelves (real example:
                # Opportunity List nests 10+ fields this way). FIELDREF.finditer() finds every
                # `[ds].[field]` pair by pattern regardless of the surrounding parens/slashes, in
                # left-to-right = shelf-hierarchy order, so no paren-depth parsing is needed.
                matches = list(FIELDREF.finditer(e.text))
                if matches:
                    for m in matches:
                        field, agg = _fieldref_parts(m)
                        cap = name_map.get(f"[{field}]", field)
                        # A Tableau quick TABLE CALCULATION button (Percent of Total, Running
                        # Total, ...) applied directly to THIS SPECIFIC shelf placement -- see
                        # `worksheet_pcto_meta()`'s own docstring on why `_fieldref_inner()` (this
                        # match's own inner-bracket text, e.g. "pcto:sum:Calculation_X:qk") is the
                        # correct join key, not the bare field identifier alone (`field`) -- the
                        # SAME underlying field can have a second, unrelated plain column-instance
                        # in this same worksheet, and matching on the field alone would wrongly
                        # tag that one too. `None` (the overwhelming majority case) for a plain
                        # field with no quick calc attached.
                        tc = ws_pcto.get(_fieldref_inner(m))
                        if tc is not None:
                            # The pill's own base-value aggregation when Tableau wrote no
                            # explicit `aggregation=` attribute -- see `_fieldref_underlying_agg`'s
                            # own docstring. Merged in here (not stored on `tc` itself inside
                            # `worksheet_pcto_meta()`) because it comes from THIS match's own
                            # ref text, not from the `<table-calc>` element that function reads.
                            tc = {**tc, "underlying_agg": _fieldref_underlying_agg(m)}
                        if cap:
                            refs.append({"field": cap, "agg": agg, "table_calc": tc})
                            encodings.append({"channel": shelf, "field": cap, "agg": agg,
                                              "table_calc": tc})
                else:
                    # Pseudo-field shelf text (e.g. bare "[ds].[:Measure Names]") has no
                    # `[ds].[field]`-shaped match at all -- fall back to the single-ref parse so
                    # this case is unchanged from before. Can never carry a quick-calc's own
                    # `table_calc` (it never matched a real field ref to look one up by).
                    cap, agg = parse_fieldref(e.text, name_map)
                    if cap:
                        refs.append({"field": cap, "agg": agg, "table_calc": None})
                        encodings.append({"channel": shelf, "field": cap, "agg": agg,
                                          "table_calc": None})
            shelves[shelf] = refs
            shelves[f"{shelf}_total"] = (e.get("total") == "true") if e is not None else False
        for enc in ws.findall(".//encodings/*"):
            raw_col = enc.get("column")
            cap, agg = parse_fieldref(raw_col, name_map)
            if cap:
                _m = FIELDREF.search(raw_col or "")
                _tc = ws_pcto.get(_fieldref_inner(_m)) if _m else None
                if _tc is not None:
                    _tc = {**_tc, "underlying_agg": _fieldref_underlying_agg(_m)}
                encodings.append({"channel": enc.tag, "field": cap, "agg": agg,
                                  "table_calc": _tc})
        filters = []
        for f in ws.findall(".//filter"):
            cap, _ = parse_fieldref(f.get("column"), name_map)
            if cap:
                filters.append(classify_filter(f, cap, name_map))
        title = ws.find(".//title")
        # The worksheet's OWN declared datasource(s) -- `<table><view><datasources><datasource
        # name='federated.XXXX' .../></datasources>`, distinct from the workbook-level datasource
        # DEFINITIONS this module already parses elsewhere. This is the exact same per-datasource
        # key `pbir.py`'s own `na.tables` dict is keyed by (see the dashboard-zone `zone["param"]`
        # fix) -- a worksheet with NO measure anywhere in its encodings (a plain dimension/label
        # display, e.g. "show this row's own Ctry/Issr as text") gives `_query_state`'s own
        # `anchor_tables` scan nothing to seed from, since that scan only ever looks at resolved
        # MEASURES. 98 of 382 real corpus (WorkbookA) worksheets have this zero-measure shape, and
        # every single-field reference in one of them fell through to the GLOBAL (unscoped,
        # first-table-wins) resolution path for lack of any other hint -- confirmed on
        # "Mkt_acti"'s own `Ctry` field landing on `Recommendations` instead of `Activation`.
        # Captured here, unconditionally, so a consumer can fall back to it when its own
        # measure-seeded anchor is empty; this worksheet's OTHER fields already resolve correctly
        # today whenever a measure IS present, so this is purely an additional fallback source,
        # never a replacement for the existing measure-based anchor.
        own_datasources = [d.get("name") for d in ws.findall(".//view/datasources/datasource")
                           if d.get("name")]
        out.append({
            "name": ws.get("name"), "mark": mark, "shelves": shelves,
            "encodings": encodings, "filters": filters,
            "n_panes": len(ws.findall(".//pane")), "panes": panes,
            "title": resolve_title(title, ws.get("name")),
            "measure_names_pivot": _measure_names_pivot(ws, name_map),
            "label_font": _label_font(ws),
            "own_datasources": own_datasources,
        })
    return out


UI_NS = "{http://www.tableausoftware.com/xml/user}"


def _member_value(raw):
    """`&quot;ALL&quot;` -> `ALL`. Tableau quotes a categorical member value inside the attribute."""
    if raw is None:
        return None
    t = str(raw).strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].replace('""', '"')
    return t


def classify_filter(f, caption, name_map):
    """Classify one <filter> into something a report writer can act on.

    Tableau writes several very different things through the SAME <filter> element, and three of
    them are not restrictions at all:

    - `level-members` with ui-enumeration="all" means "show every member" -- there is nothing to
      filter. Emitting an `In` list of every value would be wrong as well as pointless.
    - a `ui-action-filter` marker means the element records DASHBOARD ACTION state, not a filter the
      author set. Its Power BI counterpart is `visualInteractions` on the page, not a visual filter.
    - a `filter` function carries an `expression` (often referencing a parameter), which is a
      computed predicate rather than a value list.

    Measured on the corpus: of 2423 <filter> elements only 1103 are authored static filters.
    Treating the tag as one thing would have emitted ~1300 filters that restrict nothing or
    duplicate action behaviour.
    """
    rec = {"field": caption, "class": f.get("class"),
           "context": (f.get("context") or "").lower() == "true",
           "kind": "unsupported", "values": [], "exclude": [],
           "min": None, "max": None, "reason": None}
    col = f.get("column") or ""
    if ":Measure Names" in col or ":Measure Values" in col:
        rec["reason"] = ("filters the Measure Names/Values pseudo-field, which is Tableau's "
                         "measure-pivot shelf and has no column in the model")
        return rec

    if f.get("class") == "quantitative":
        lo, hi = f.find("min"), f.find("max")
        if lo is None and hi is None:
            rec["reason"] = "quantitative filter with neither a min nor a max bound"
            return rec
        rec["kind"] = "range"
        rec["min"] = decode_param_value(lo.text) if lo is not None else None
        rec["max"] = decode_param_value(hi.text) if hi is not None else None
        return rec

    top = [g for g in f if g.tag == "groupfilter"]
    if not top:
        rec["reason"] = "filter element with no groupfilter child"
        return rec
    g = top[0]
    if g.get(UI_NS + "ui-action-filter") or any(
            x.get(UI_NS + "ui-action-filter") for x in g.iter("groupfilter")):
        rec["kind"] = "action"
        rec["reason"] = ("records dashboard ACTION state, not an authored filter -- its Power BI "
                         "counterpart is visualInteractions on the page")
        return rec

    fn = g.get("function")
    if fn == "member":
        val = _member_value(g.get("member"))
        if val == "(All)":
            # Tableau's own reserved "show every value" caption -- always offered in a
            # categorical quick-filter's dropdown regardless of whether "(All)" is a real domain
            # value, and distinct from the ui-action-filter marker already excluded above (that
            # one records a DIFFERENT reset-state shape, action-driven rather than a standalone
            # filter card). Confirmed empirically on a real corpus workbook (WorkbookA): queried the
            # field's own embedded .hyper extract directly (247,425 rows for one such field,
            # `rgn`) and found zero rows valued literally "(All)" -- this is a sentinel, not
            # data. Previously emitted as a literal `In` match on the string "(All)", which
            # matches NO real row and -- via this table's own blend-key relationship --
            # zero-filtered every OTHER table cross-filtered through it too: confirmed on a real
            # rebuild that 74 of 156 cards carried a filter shaped exactly like this, and the
            # ones that did rendered blank in Desktop even though the same measure returned a
            # real value with no filter context at all. Same "restricts nothing" reasoning
            # `level-members` already gets below, just reached through a single-member
            # `function='member'` shape instead of an enumerated-all one.
            rec["kind"] = "none"
            rec["reason"] = ('member "(All)" is Tableau\'s own show-every-value sentinel, not a '
                              'real domain value -- there is no restriction to apply')
        else:
            rec["kind"] = "in"
            rec["values"] = [val]
    elif fn == "union" and all(c.get("function") == "member"
                               for c in g if c.tag == "groupfilter"):
        rec["kind"] = "in"
        rec["values"] = [_member_value(c.get("member")) for c in g if c.tag == "groupfilter"]
    elif fn == "except":
        members = [_member_value(x.get("member")) for x in g.iter("groupfilter")
                   if x.get("function") == "member"]
        if members:
            rec["kind"] = "not_in"
            rec["exclude"] = members
        else:
            rec["reason"] = "except filter with no enumerated members to exclude"
    elif fn == "level-members":
        rec["kind"] = "none"
        rec["reason"] = "level-members: every member is included, so there is no restriction"
    elif fn == "filter":
        rec["kind"] = "expression"
        rec["reason"] = (f"computed filter predicate {g.get('expression')!r} -- a condition, not a "
                         f"value list")
    elif fn == "crossjoin":
        rec["kind"] = "action"
        rec["reason"] = "crossjoin of action-filter levels, not an authored filter"
    else:
        rec["reason"] = f"groupfilter function {fn!r} is not a value list or a range"
    rec["values"] = [v for v in rec["values"] if v is not None]
    rec["exclude"] = [v for v in rec["exclude"] if v is not None]
    if rec["kind"] in ("in", "not_in") and not (rec["values"] or rec["exclude"]):
        rec["kind"] = "unsupported"
        rec["reason"] = "value list resolved to nothing"
    return rec


def flatten_runs(elem):
    if elem is None:
        return None
    txt = "".join(r.text or "" for r in elem.iter("run"))
    # U+00C6 is a formatted-run separator glyph Tableau emits inside title/text zones -- layout
    # noise, not authored content.
    txt = re.sub(r"[Æ]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip() or None


# Tableau's own numeric alignment code on a <run> -- 0/1/2 observed across achc, Digital Sales,
# Bookshop (816+ real instances), matching the community-documented convention for this
# attribute. Not verified against an official Tableau schema doc (none available here); low risk
# if wrong -- worst case is a text box's horizontal alignment being off, not a data/crash issue.
_ALIGN_MAP = {"0": "left", "1": "center", "2": "right"}
_RUN_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def parse_formatted_text(elem):
    """Tableau's `<formatted-text><run .../></formatted-text>` -> (runs, align).

    `runs` is a list of `{"text", "bold", "italic", "color", "fontsize", "fontname"}` dicts, one
    per `<run>` -- never flattened into one plain string, so per-run bold/color/size/family
    survives (previously lost entirely: `flatten_runs` only ever kept `.text`). `align` is the
    zone's overall horizontal alignment, taken from the first run that declares one (Tableau does
    not vary alignment mid-paragraph in any real corpus example checked).

    Real corpus zones with >1 run are not rare -- achc alone has 128 multi-run text zones
    (vs. 894 single-run) -- so this cannot be a single flattened string and still be correct.

    `fontname` (e.g. 'Segoe UI Semibold', 'Agency FB') was captured nowhere until now -- every
    `<run>` in a real corpus workbook carries it, but it was dropped at extraction, so pbir.py
    had nothing to emit even for text zones and every textbox fell back to Desktop's default
    typeface regardless of what the source actually used.
    """
    if elem is None:
        return [], None
    runs, align = [], None
    for r in elem.iter("run"):
        txt = re.sub(r"[Æ]+", " ", r.text or "")
        if not txt.strip():
            continue
        if align is None and r.get("fontalignment") in _ALIGN_MAP:
            align = _ALIGN_MAP[r.get("fontalignment")]
        color = r.get("fontcolor")
        runs.append({
            "text": txt,
            "bold": (r.get("bold") or "").lower() == "true",
            "italic": (r.get("italic") or "").lower() == "true",
            "color": color if color and _RUN_COLOR_RE.match(color) else None,
            "fontsize": r.get("fontsize"),
            "fontname": r.get("fontname"),
        })
    return runs, align


def parse_zone_style(z):
    """Tableau's `<zone-style>` background-color/border for a zone -- only meaningful values
    (not none/transparent/default-white) are returned, so a zone Tableau never actually painted
    doesn't grow an unwanted box in Power BI. Real corpus evidence (achc): 136 of 1064 `text`
    zones have a real, non-default background-color -- used as colored banner bars/dividers,
    completely dropped before this (`zone-style` was never read anywhere in this pipeline)."""
    zs = z.find("zone-style")
    if zs is None:
        return None
    fmt = {f.get("attr"): f.get("value") for f in zs.findall("format")}
    bg = fmt.get("background-color")
    if bg in (None, "none", "#ffffff", "transparent"):
        bg = None
    border_style = fmt.get("border-style")
    border_color = fmt.get("border-color") if border_style not in (None, "none") else None
    if not bg and not border_color:
        return None
    return {"background_color": bg, "border_color": border_color,
            "border_width": fmt.get("border-width")}


_TITLE_TOKEN_RE = re.compile(r"<[^<>]+>")


def resolve_title(elem, ws_name):
    """A worksheet's `<title>` text, with Tableau's `<Sheet Name>` dynamic-title token
    substituted for the worksheet's own name -- the only title token that is a static,
    always-known-at-build-time value.

    Other Tableau dynamic-title tokens (`<[Parameters].[Parameter 8]>`, a field-value
    reference like `<[federated.xxx].[some_field]>`) depend on live filter/parameter state at
    render time, which a static string cannot reproduce -- emitting the literal bracket syntax
    as if it were real title text would be worse than no title (a real corpus workbook,
    10000056_achc_WF_Payments_Insights, has 22 titles with one of these; showing
    "Sample Card <[Parameters].[Parameter 1]>" verbatim is confusing, not merely incomplete), so
    a title carrying any OTHER token after the Sheet Name substitution is declined (returns
    None) rather than guessed at -- same fail-closed rule as everywhere else in this pipeline.
    """
    txt = flatten_runs(elem)
    if txt is None:
        return None
    txt = txt.replace("<Sheet Name>", ws_name or "")
    if _TITLE_TOKEN_RE.search(txt):
        return None
    return re.sub(r"\s+", " ", txt).strip() or None


def geom(z):
    """Zone geometry: Tableau's 0..100000 canvas -> fraction of the canvas."""
    def f(v):
        try:
            return int(v) / 100000.0
        except (TypeError, ValueError):
            return None
    return {k: f(z.get(k)) for k in ("x", "y", "w", "h")}


_GOTO_SHEET_RE = re.compile(r"tabdoc:goto-sheet\s+window-id=")
_WINDOW_ID_RE = re.compile(r'window-id="(\{[0-9A-Fa-f-]+\})"')


def _build_window_guid_map(root):
    """GUID -> (window class, window name) for every `<windows>/<window>` in this workbook,
    keyed by the same `{UUID}` string a goto-sheet button's own `window-id="{GUID}"` carries.

    Tableau stores this id as a `<simple-id uuid='{GUID}' />` CHILD of `<window>` -- NOT an
    `id` attribute on `<window>` itself. An earlier pass over this exact problem checked for
    an `id` attribute, correctly found none, and concluded the GUID was unresolvable; that
    conclusion was wrong -- it never looked for the nested `<simple-id>` element. Confirmed
    against two real corpus workbooks: 10000056_achc_WF_Payments_Insights resolves 21/21 of its
    goto-sheet button targets this way, Digital Sales - Leads Dashboard resolves 8/9 (the 1
    miss has no `<simple-id>` anywhere in the file at all -- a genuinely dangling reference,
    correctly left unresolved rather than guessed at).
    """
    m = {}
    for w in root.findall(".//windows/window"):
        sid = w.find("simple-id")
        if sid is not None and sid.get("uuid"):
            m[sid.get("uuid")] = (w.get("class"), w.get("name"))
    return m


def _nav_button_caption(btn):
    """The button's own `<caption>` text, if it has one -- None for an icon-only or bare
    button. Split out from `_nav_button_hint` so pbir.py can use JUST the real caption (never
    the icon-filename fallback) as a wired button's own display text."""
    for vs in btn.findall("button-visual-state"):
        cap = vs.find("caption")
        if cap is not None and (cap.text or "").strip():
            return cap.text.strip()
    return None


def _nav_button_hint(z):
    """For a `dashboard-object` zone: if its `<button>` child is a "Go to Sheet" navigation
    action, a short human-readable HINT of the intended target -- used only as the message
    text on the rare decline this pipeline still has to fall back to when
    `_nav_button_target` cannot resolve a real target (see that function). Falls back to the
    button's icon FILENAME when there is no caption -- still better than nothing, though a
    filename like "WF_icon_house_rgb_F1_WF Red.png" is a much weaker hint than a real caption.
    """
    btn = z.find("button")
    if btn is None or not _GOTO_SHEET_RE.search(btn.get("action") or ""):
        return None
    cap = _nav_button_caption(btn)
    if cap:
        return cap
    for vs in btn.findall("button-visual-state"):
        img = vs.find("image-path")
        if img is not None and (img.text or "").strip():
            return f"icon {img.text.strip()!r}"
    return "(no caption or icon on the button)"


def _nav_button_target(z, guid_map):
    """The RESOLVED dashboard name a "Go to Sheet" button targets, via its `window-id` GUID
    against `guid_map` (see `_build_window_guid_map`) -- None if the button is not a
    goto-sheet action, the GUID is missing from the map (a dangling reference), or the
    resolved window is a worksheet rather than a dashboard (a goto-sheet button that targets a
    bare worksheet has no page of its own in this pipeline, which builds one page per
    dashboard, never per worksheet)."""
    btn = z.find("button")
    if btn is None:
        return None
    m = _WINDOW_ID_RE.search(btn.get("action") or "")
    if not m:
        return None
    info = guid_map.get(m.group(1))
    if not info or info[0] != "dashboard":
        return None
    return info[1]


_FIELDREF_IN_TEXT = re.compile(r"<\[[^\]]+\]>")


def extract_url_actions(root):
    """Tableau's URL dashboard action (Actions dialog, a `<link>`-bearing `<action>`): clicking a
    mark on worksheet X opens a browser to a fixed address. A DIFFERENT mechanism from a
    dedicated "Go to Sheet" button zone (`_nav_button_*` above) -- this one lives in the
    workbook-level `<actions>` block and names its trigger worksheet via `<source>`, not a
    `<button>` child on a `dashboard-object` zone.

    Real corpus measurement (117 workbooks): 899 `<action>` elements total. Scoped narrowly to
    the one safely-reproducible shape: `<activation type='on-select'>` (847 of 899 -- Power BI's
    own click-action model; the 21 'on-hover' actions have no clean equivalent and are skipped,
    not guessed at), `<source type='sheet'>` (878 of 899 -- a SINGLE worksheet's own click;
    'datasource'/'all' fire from every sheet sharing a datasource or every sheet on the
    dashboard, a broader semantic this does not attempt), and a `<link>` child, not a `<command>`
    (571 of 878 -- a URL action, not a filter/highlight/parameter action, out of scope here).

    Of those 571, only a FIXED address is captured: 353 are a plain URL string; 218 embed a
    Tableau field reference (`<[Field]>`) to build the address dynamically per mark -- skipped,
    since reproducing that would mean folding the referenced calc into a bound DAX expression, a
    materially bigger feature than a fixed-address click action, not this function's job.

    Returns a list of `{dashboard, worksheet, url, caption}` -- one entry per (dashboard,
    worksheet) pair, first action found wins on a duplicate (same "never guess, first that
    resolves" discipline as a capped field-well elsewhere in this pipeline; two actions never
    intentionally overlap in practice on the same worksheet).
    """
    seen, out = set(), []
    for a in root.findall(".//actions/action"):
        act = a.find("activation")
        if act is None or act.get("type") != "on-select":
            continue
        src = a.find("source")
        if src is None or src.get("type") != "sheet":
            continue
        dashboard, worksheet = src.get("dashboard"), src.get("worksheet")
        if not dashboard or not worksheet:
            continue
        link = a.find("link")
        if link is None or not link.get("expression"):
            continue
        url = link.get("expression")
        if _FIELDREF_IN_TEXT.search(url):
            continue
        key = (dashboard, worksheet)
        if key in seen:
            continue
        seen.add(key)
        out.append({"dashboard": dashboard, "worksheet": worksheet, "url": url,
                    "caption": link.get("caption")})
    return out


def extract_action_source_worksheets(root):
    """Every (dashboard, worksheet) pair that is the SOURCE of an on-select, single-sheet
    dashboard action in Tableau -- filter (`tsc:tsl-filter`) or highlight (`tsc:brush`) alike,
    whichever command. Tableau's own default is the OPPOSITE of Power BI's: a worksheet never
    affects any other sheet on its dashboard unless the author explicitly wired an Action for it
    (Dashboard > Actions...) -- confirmed on WorkbookA's own 'Dormancy Management' dashboard, whose
    `Spend_migration_viz`/`Dormancy_pattern_viz` filter actions carry careful per-action
    `exclude` lists (deliberately keeping other worksheets, e.g. `recency`, OUT of their own
    filter's reach), and whose `recency` worksheet is never the `source` of ANY action at all.
    Power BI's `drillFilterOtherVisuals` defaults to True on every visual regardless, so a
    worksheet with zero authored outgoing actions still cross-filters its whole page's siblings
    once converted -- selecting a cell on WorkbookA's `recency` heatmap, whose rows occupy a disjoint
    `For View='Recency'` slice of the single wide `Dormancy` table, filtered every OTHER
    Dormancy-bound card (each pinned to its OWN `For View` slice via its own baked-in filter) to
    an empty row set, blanking the entire page on any heatmap click.
    `pbir.py`'s `_visual` uses this to decide `drillFilterOtherVisuals` per worksheet: True only
    when `(dashboard, worksheet)` is a member of this set, matching Tableau's own opt-in default
    instead of Power BI's opt-out one. Same `on-select`/`source type='sheet'` scoping as
    `extract_url_actions` right above (that function's own docstring has the real-corpus
    action-shape breakdown) -- this one does not care about the action's own command or target,
    only that *some* on-select action exists FROM this worksheet.
    """
    out = set()
    for a in root.findall(".//actions/action"):
        act = a.find("activation")
        if act is None or act.get("type") != "on-select":
            continue
        src = a.find("source")
        if src is None or src.get("type") != "sheet":
            continue
        dashboard, worksheet = src.get("dashboard"), src.get("worksheet")
        if dashboard and worksheet:
            out.add((dashboard, worksheet))
    return sorted(out)


def extract_dashboards(root, name_map, ws_names):
    out = []
    guid_map = _build_window_guid_map(root)
    for db in root.findall(".//dashboards/dashboard"):
        size = db.find("./size")
        zones = []
        # Scope to the primary <zones> tree only. `<dashboard>` can also carry
        # `<devicelayouts>` -- Tableau's auto-generated Phone/Tablet layouts, each a COMPLETE
        # second copy of the same zone tree (same ids, same names) against a DIFFERENT canvas
        # (a device layout's own <size> is unrelated to the primary dashboard's). A real corpus
        # workbook (SMB IRF Program) has one on 3 of its 4 dashboards; `db.iter("zone")` walked
        # the whole `<dashboard>` subtree and silently pulled in every device-layout zone
        # alongside the real ones -- every worksheet on those dashboards was emitted TWICE (one
        # visual positioned correctly against the primary canvas, one duplicate whose geometry
        # was computed against the primary canvas but authored for the Phone canvas, landing at
        # an unrelated position -- sometimes off-canvas). Found by `pbip_lint.py`'s RPT-POS-01/
        # RPT-ROLE-02 checks and confirmed directly against the raw XML: every affected
        # worksheet name appeared in RESIDUE.md's undecided-view list exactly twice.
        primary_zones = db.find("./zones")
        zone_iter = primary_zones.iter("zone") if primary_zones is not None else ()
        for z in zone_iter:
            t = z.get("type-v2") or z.get("type")
            nm = z.get("name")
            kind = ("worksheet" if (not t and nm in ws_names) else t or "container")
            rec = {"kind": kind, "name": nm, "geom": geom(z),
                   "param": z.get("param"), "text": None,
                   # Tableau's own "hide this container from the published view" flag on a zone
                   # (and every zone nested inside it -- WorkbookB Dashboard's "Filter Container"
                   # panel and its whole nested parameter-control tree are 257 zones deep, ALL
                   # carrying this). Never read before this: the whole hidden panel was emitted
                   # as visible Power BI visuals, landing wherever Tableau had left its
                   # never-meant-to-be-seen layout (in WorkbookB Dashboard's case, floating on top
                   # of the "1W - Slice By" bar chart) -- not a translation gap, a translation of
                   # something the workbook author explicitly hid.
                   "hidden": (z.get("hidden-by-user") or "").lower() == "true"}
            if kind == "text":
                rec["text"] = flatten_runs(z)
                rec["text_runs"], rec["text_align"] = parse_formatted_text(
                    z.find("formatted-text"))
                rec["zone_style"] = parse_zone_style(z)
            if kind == "worksheet":
                # A dashboard zone can hide a worksheet's own title just for THIS placement,
                # independent of the worksheet's own <title> definition (Tableau's per-zone
                # "Edit Title" toggle) -- absent means Tableau's own default, which is shown.
                # Real corpus workbook (10000056_achc_WF_Payments_Insights): 994 zones set this
                # to 'false', zero set 'true'; never read before this, so every one of those
                # zones got a title anyway whenever the worksheet itself had one.
                rec["show_title"] = (z.get("show-title") or "true").lower() != "false"
            if kind in ("filter", "color", "size", "shape"):
                cap, _ = parse_fieldref(z.get("param"), name_map)
                rec["field"] = cap
            if kind == "filter":
                # Tableau's own declared quick-filter style for this zone ('dropdown',
                # 'checkdropdown' for multi-select, 'list', etc.) -- never read before this, so
                # every filter rendered as Power BI's default List style regardless of what
                # Tableau actually showed. Real corpus: achc's 10 filter zones are all
                # 'dropdown'; Digital Sales/NGPO/Caribbean/SMB IRF also have 'checkdropdown'.
                rec["filter_mode"] = z.get("mode")
            if kind == "paramctrl":
                rec["field"] = (z.get("param") or "").replace("[Parameters].", "").strip("[]")
                # Tableau's own "Edit Title..." override for a parameter control -- a purpose-
                # built caption for THIS zone specifically, independent of the parameter's own
                # name (which is all a slicer's default header can show otherwise). Real corpus
                # confirmation (WorkbookB Dashboard "1D - OI Summary"): every one of its 10
                # paramctrl zones sets `custom-title='true'`, and the actual captions
                # ("Chart Metric Type" for `pl.Selected Metric`, "Color Bar Chart By" for
                # `pl.Color By`, "Slice By", "Date Type", "Report Period", ...) read nothing
                # like the parameter's own internal name -- carrying only the parameter's own
                # (prefix-stripped) name, as this pipeline did before, is a real, silent
                # mislabel, not a cosmetic nuance.
                if (z.get("custom-title") or "").lower() == "true":
                    rec["custom_title"] = resolve_title(z.find("formatted-text"), None)
            if kind == "bitmap":
                rec["image"] = z.get("param")
            if kind == "dashboard-object":
                rec["nav_button_hint"] = _nav_button_hint(z)
                rec["nav_button_target"] = _nav_button_target(z, guid_map)
                btn = z.find("button")
                rec["nav_button_caption"] = _nav_button_caption(btn) if btn is not None else None
            zones.append(rec)
        out.append({
            "name": db.get("name"),
            "width": int(size.get("maxwidth") or size.get("width") or 1280) if size is not None else 1280,
            "height": int(size.get("maxheight") or size.get("height") or 720) if size is not None else 720,
            "zones": zones,
        })
    return out


def decode_param_value(raw):
    """Tableau writes these three ways; every consumer wants the bare value.
        "Actual Growth"   quoted string
        #2025-03-01#      date fenced with hashes
        7 / 310.5         bare number
    """
    if raw is None:
        return None
    t = str(raw).strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].replace('""', '"')
    if len(t) >= 2 and t[0] == "#" and t[-1] == "#":
        return t[1:-1]
    for cast in (int, float):
        try:
            return cast(t)
        except ValueError:
            pass
    return t


def extract_parameters(root):
    out = []
    for pd in root.findall("./datasources/datasource"):
        if pd.get("name") != "Parameters":
            continue
        for c in pd.findall("./column"):
            rng = c.find("./range")
            out.append({
                "name": c.get("caption") or (c.get("name") or "").strip("[]"),
                "tableau_name": c.get("name"),
                "datatype": DTYPE.get(c.get("datatype"), "string"),
                "value": decode_param_value(c.get("value")),
                "domain": c.get("param-domain-type") or "any",
                "members": [decode_param_value(m.get("value"))
                            for m in c.findall("./members/member")],
                "range": None if rng is None else {
                    "min": decode_param_value(rng.get("min")),
                    "max": decode_param_value(rng.get("max")),
                    "step": decode_param_value(rng.get("granularity"))},
                "aliases": [],
            })

    # Recover an orphaned internal parameter ID: a calc can reference [Parameters].[X] where X
    # was never declared in the canonical top-level Parameters datasource above -- only in a
    # stale, worksheet-embedded copy of it (Tableau doesn't always re-point every calc when a
    # parameter is effectively replaced/recreated; real corpus example: WorkbookB Dashboard's
    # calc-level `[Parameters].[Parameter 7 1]`, absent from the canonical block, present only in
    # per-worksheet copies). Safe to fold in, NOT a guess: only when EVERY <column name="[X]"
    # param-domain-type=...> anywhere in the whole document unanimously agrees on one caption,
    # and that caption matches an existing canonical parameter's caption exactly. A name that is
    # ambiguous anywhere it appears, or whose caption matches no canonical parameter, is left
    # alone -- still unresolved, still reported as residue, same as before this fix.
    canonical_names = {p["tableau_name"] for p in out}
    canonical_by_caption = {}
    for p in out:
        canonical_by_caption.setdefault(p["name"], p)

    captions_by_name = collections.defaultdict(set)
    for c in root.iter("column"):
        nm = c.get("name")
        if not nm or nm in canonical_names or c.get("param-domain-type") is None:
            continue
        captions_by_name[nm].add(c.get("caption") or nm.strip("[]"))

    for nm, caps in captions_by_name.items():
        if len(caps) != 1:
            continue  # ambiguous everywhere it's declared -- do not guess
        target = canonical_by_caption.get(next(iter(caps)))
        if target is not None:
            target["aliases"].append(nm)

    return out


def infer_measure_roles(root, tables, name_map):
    """A backfilled column has no role of its own. Whether any worksheet AGGREGATES it is the
    only real signal for measure-vs-dimension -- it mirrors Tableau's own behaviour of treating a
    numeric field dropped on a shelf as an aggregated measure.

    Scoped per (datasource, field): a workbook's near-duplicate datasources routinely reuse a
    plain field name, and a global by-name check would let usage in one wrongly flip another's.
    """
    used = collections.defaultdict(set)
    ref_re = re.compile(r"\[([^\]]+)\]\.\[(?:([a-z]+):)?([^:\]]+)(?::[^\]]+)?\]")
    for ws in root.findall(".//worksheets/worksheet"):
        refs = []
        for shelf in ("rows", "cols"):
            e = ws.find(f".//{shelf}")
            if e is not None and e.text:
                refs += [r if r.startswith("[") else "[" + r for r in e.text.split("]/[")]
        refs += [e.get("column") for e in ws.findall(".//encodings/*")]
        refs += [f.get("column") for f in ws.findall(".//filter")]
        for ref in refs:
            if not ref:
                continue
            m = ref_re.search(ref)
            if not m:
                continue
            ds_name, agg, field = m.group(1), m.group(2), m.group(3)
            if agg and agg != "none":
                used[(ds_name, field.lower())].add(agg)
    for t in tables:
        for col in t["columns"]:
            if not col["from_metadata"]:
                continue
            if used.get((t["tableau_name"], col["name"].lower())):
                col["role"] = "measure"
                col["role_inferred"] = True
    return sum(1 for t in tables for c in t["columns"] if c.get("role_inferred"))


def resolve_siblings(root, tds_dir):
    """Load .tds roots for every published datasource this workbook proxies to.

    A `class="sqlproxy"` connection means the workbook holds NO physical source -- only the
    address of a datasource published on Tableau Server. That is a dead end reading the .twb
    alone. The sibling .tds root element is <datasource>, the same vocabulary, so it goes through
    the identical extractor.
    """
    if not tds_dir or not os.path.isdir(tds_dir):
        return [], []
    want = {norm(c.get("dbname")) for c in root.iter("connection")
            if (c.get("class") or "") in PROXY_CLASSES and c.get("dbname")}
    if not want:
        return [], []
    found, unresolved = [], set(want)
    for dirpath, _d, files in os.walk(tds_dir):
        for fn in sorted(files):
            stem = norm(os.path.splitext(fn)[0])
            if stem not in want:
                continue
            path = os.path.join(dirpath, fn)
            try:
                if fn.lower().endswith(".tds"):
                    found.append((fn, load_xml(path)))
                elif fn.lower().endswith(".tdsx"):
                    with zipfile.ZipFile(path) as z:
                        for n in z.namelist():
                            if n.lower().endswith(".tds"):
                                txt = z.read(n).decode("utf-8", "replace").lstrip("/").lstrip()
                                found.append((fn, ET.fromstring(txt)))
                                break
                unresolved.discard(stem)
            except (ET.ParseError, zipfile.BadZipFile, OSError):
                continue
    return found, sorted(unresolved)


def extract(src, workdir, tds_dir=None):
    """S0 + S1. Returns the IR."""
    twb, pkg = unpack(src, workdir)
    root = load_xml(twb)
    wb_name = os.path.splitext(os.path.basename(twb))[0]

    all_ds = root.findall("./datasources/datasource")
    name_map = {c.get("name"): c.get("caption")
                for d in all_ds for c in d.iter("column")
                if c.get("name") and c.get("caption")}

    tables = [extract_datasource(d, None, "twb", name_map=name_map)
              for d in all_ds if d.get("name") != "Parameters"]

    # Union in sibling published datasources -- this is what recovers a real connection and a
    # real relation for a workbook whose .twb offers only a proxy address.
    sibs, unresolved = resolve_siblings(root, tds_dir)
    for fn, sroot in sibs:
        for sd in ([sroot] if sroot.tag == "datasource"
                   else sroot.findall("./datasources/datasource")):
            if sd.get("name") == "Parameters":
                continue
            tables.append(extract_datasource(sd, None, f"tds:{fn}", name_map=name_map))

    # RC-B: extend name_map with worksheet-local thin-wrapper aliases. A worksheet may define a
    # <column> override inside <datasource-dependencies> whose formula is exactly ([real_field_id])
    # -- a bare pass-through to a real top-level datasource field. These never appear in any
    # datasource's own schema, so _resolve_field returns None for the wrapper id. Registering the
    # wrapper as a synonym for the real field's caption here (before extract_worksheets runs)
    # means shelf references that use the wrapper id resolve to the real caption, and from there
    # to the real semantic-model measure, same as a direct reference would.
    for _ws_el in root.findall(".//worksheets/worksheet"):
        for _col in _ws_el.findall(".//datasource-dependencies/column"):
            _cname = _col.get("name")
            if not _cname or _cname in name_map:
                continue
            _calc = _col.find("calculation")
            if _calc is None:
                continue
            _wm = _WRAPPER_ALIAS_RE.match((_calc.get("formula") or "").strip())
            if _wm:
                _real_cap = name_map.get(f"[{_wm.group(1)}]")
                if _real_cap:
                    name_map[_cname] = _real_cap

    ws = extract_worksheets(root, name_map, pcto_meta=worksheet_pcto_meta(root, name_map))
    ws_names = {w["name"] for w in ws}
    inferred = infer_measure_roles(root, tables, name_map)

    # Split a federated datasource into one real table per physical Tableau object, when its own
    # object-graph gives enough to do that without guessing (see split_federated_datasource's own
    # docstring for the exact bar). Run AFTER infer_measure_roles, which is keyed by column name
    # regardless of which table dict currently holds it, so splitting first or after makes no
    # difference to it -- kept after simply to change as little as possible about the flattened
    # shape while that function runs. Every OTHER datasource (no object-graph, or one that does not
    # clear the bar) returns unchanged from its own single-element list.
    resolved_joins = []
    split_tables = []
    for t in tables:
        subs = split_federated_datasource(t)
        split_tables.extend(subs)
        for sub in subs:
            rj = sub.pop("_resolved_joins", None)
            if rj:
                resolved_joins.extend(rj)
    tables = split_tables

    # Resolve each table calc's `<order field=...>` references into plain captions, now that
    # `name_map` exists (see table_calc_spec's own note on why this cannot happen in-place during
    # the datasource parse). These name the calc's ADDRESSING fields explicitly -- the thing a
    # reviewer would otherwise have to open Tableau's "Edit Table Calculation" dialog to read.
    ws_tc = worksheet_tc_addressing(root, name_map)
    for t in tables:
        for c in t["calcs"]:
            spec = c.get("tc_spec")
            if not spec:
                continue
            resolved = []
            for raw in spec.get("order_fields_raw") or []:
                cap, _agg = parse_fieldref(raw, name_map)
                if cap:
                    resolved.append(cap)
            spec["order_fields"] = resolved
            # `level-break` ("Restart every: <field>" in Tableau's own "Edit Table Calculation"
            # dialog) is a SEPARATE setting from the addressing/`<order>` fields above: it is the
            # partition boundary a RANK-family calc resets at, which is exactly the ALLEXCEPT
            # grouping tmdl.py needs to translate one -- resolved here for the same reason
            # order_fields is (no name_map at parse time). Left as None when absent, same
            # fail-closed default as every other field this dict carries.
            lb_raw = spec.get("level_break")
            spec["level_break_field"] = parse_fieldref(lb_raw, name_map)[0] if lb_raw else None
            # A worksheet-saved addressing (see `worksheet_tc_addressing()`) is only informative
            # when the calc's OWN definition carries none -- when it does, that is the more
            # authoritative copy (it is what Tableau's "Edit Table Calculation" dialog shows by
            # default) and repeating the worksheet copy alongside it would just be noise.
            spec["ws_order_fields"] = []
            if not resolved and not (spec.get("ordering_type") == "Field" and spec.get("ordering_field")):
                cid = (c.get("tableau_name") or "").strip("[]")
                hits = ws_tc.get(cid)
                if hits:
                    spec["ws_order_fields"] = hits

    # Package assets (rows 54-56): what actually travels in the .twbx.
    images, extracts, filesrc = [], [], []
    for dirpath, _d, files in os.walk(pkg):
        for fn in files:
            low, full = fn.lower(), os.path.join(dirpath, fn)
            if low.endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".bmp")):
                images.append(full)
            elif low.endswith((".hyper", ".tde")):
                extracts.append(full)
            elif low.endswith((".xlsx", ".xls", ".csv", ".txt", ".json")):
                filesrc.append(full)

    return {
        "workbook": wb_name, "source": src, "package_dir": pkg,
        "source_build": root.get("source-build"),
        "tables": tables, "worksheets": ws,
        "dashboards": extract_dashboards(root, name_map, ws_names),
        "parameters": extract_parameters(root),
        "shared_views": [v.get("name") for v in root.findall(".//shared-views/shared-view")],
        "actions": [{"name": a.get("caption") or a.get("name"),
                     "command": (a.find("command").get("command")
                                 if a.find("command") is not None else None)}
                    for a in root.iter("action")],
        "url_actions": extract_url_actions(root),
        "action_source_worksheets": extract_action_source_worksheets(root),
        "package": {"images": images, "extracts": extracts, "file_sources": filesrc},
        "sibling_tds": [fn for fn, _ in sibs],
        "unresolved_proxies": unresolved,
        "resolved_joins": resolved_joins,
        "stats": {"role_inferred_columns": inferred,
                  "internal_columns_dropped": sum(len(t["internal_dropped"]) for t in tables)},
    }


if __name__ == "__main__":
    import sys

    # A physical join's operand can itself be a nested join -- Tableau's shape for a 3+-way
    # physical join -- and a nested-join child has no `name`/`table` attribute of its own. A real
    # corpus workbook was the first to have one: the "corpus has zero physical joins" comment
    # this file used to carry no longer holds. `extract_datasource` must never produce a `None`
    # operand label: tmdl.py's residue message does `' ~ '.join(rel['operands'])`, which raised
    # `TypeError: sequence item 0: expected str instance, NoneType found` on exactly this shape.
    _nested_join_ds = ET.fromstring(
        '<datasource name="ds" caption="DS">'
        '<connection class="mongodb" />'
        '<relation type="join" join="left">'
        '<relation type="join" join="inner">'
        '<relation type="table" name="A" table="[A]" connection="c" />'
        '<relation type="table" name="B" table="[B]" connection="c" />'
        '</relation>'
        '<relation type="table" name="C" table="[C]" connection="c" />'
        '</relation>'
        '</datasource>')
    _rec = extract_datasource(_nested_join_ds, None, "test")
    _outer = next(r for r in _rec["relations"] if r["type"] == "join" and r["join"] == "left")
    assert all(op is not None for op in _outer["operands"]), (
        f"a nested-join operand must get a string label, never None: {_outer['operands']!r}")
    assert _outer["operands"] == ["(nested inner join)", "C"], _outer["operands"]
    print("extract.py self-test OK (nested-join operand labelling)")

    # <datasource>/<overridable-settings>/<date-options start-of-week=.../> -- a DIRECT child of
    # <datasource>, a SIBLING of <connection> (confirmed via a real parent-chain walk over
    # WorkbookB Dashboard's actual XML, not assumed from indentation) -- Tableau's real week-start/
    # fiscal-year-start setting for a datasource, previously never read at all. A datasource with
    # NO <date-options> element must fall back to Tableau's own documented defaults
    # ("sunday"/"january"), not surface None -- every downstream reader (dax.py) expects a real
    # day name, never a missing one.
    _explicit_ds = ET.fromstring(
        '<datasource name="ds1" caption="DS1">'
        '<connection class="hyper" />'
        '<overridable-settings>'
        '<date-options fiscal-year-start="july" start-of-week="monday" />'
        '</overridable-settings>'
        '</datasource>')
    _rec1 = extract_datasource(_explicit_ds, None, "test")
    assert _rec1["date_options"] == {"start_of_week": "monday", "fiscal_year_start": "july"}, \
        _rec1["date_options"]
    _default_ds = ET.fromstring(
        '<datasource name="ds2" caption="DS2"><connection class="hyper" /></datasource>')
    _rec2 = extract_datasource(_default_ds, None, "test")
    assert _rec2["date_options"] == {"start_of_week": "sunday", "fiscal_year_start": "january"}, \
        _rec2["date_options"]
    print("extract.py self-test OK (date-options: explicit override read, absent -> Tableau's "
          "own default)")

    # A dashboard with an auto-generated Phone devicelayout duplicates its whole zone tree
    # under <devicelayouts>, reusing the SAME zone ids/names against a DIFFERENT canvas.
    # extract_dashboards() must only walk the primary <zones> tree, never the devicelayouts one.
    _db_xml = ET.fromstring(
        "<dashboard name='D'>"
        "<size maxwidth='1300' maxheight='900' />"
        "<zones><zone type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
        "<zone type-v2='worksheet' name='WS1' x='0' y='0' w='50000' h='50000' /></zone></zones>"
        "<devicelayouts><devicelayout name='Phone'>"
        "<size minheight='2400' maxheight='2400' sizing-mode='vscroll' />"
        "<zones><zone type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
        "<zone type-v2='worksheet' name='WS1' x='0' y='0' w='100000' h='20000' /></zone></zones>"
        "</devicelayout></devicelayouts>"
        "</dashboard>")
    _dbs = extract_dashboards(ET.fromstring("<workbook/>"), {}, {"WS1"})
    # extract_dashboards() only finds dashboards via root.findall(".//dashboards/dashboard"), so
    # wrap the fixture the same way the real file is shaped.
    _root = ET.fromstring("<workbook><dashboards></dashboards></workbook>")
    _root.find("dashboards").append(_db_xml)
    _dbs = extract_dashboards(_root, {}, {"WS1"})
    assert len(_dbs) == 1
    _ws_zones = [z for z in _dbs[0]["zones"] if z["kind"] == "worksheet"]
    assert len(_ws_zones) == 1, (
        f"devicelayouts zones must not be walked -- got {len(_ws_zones)} worksheet zone(s), "
        f"expected 1 (the primary-layout one only): {_ws_zones!r}")
    print("extract.py self-test OK (devicelayouts zones excluded)")

    # A paramctrl zone's own "Edit Title..." override -- real corpus shape (WorkbookB Dashboard
    # "1D - OI Summary"): `custom-title='true'` with a <formatted-text><run> giving the zone a
    # purpose-built caption ("Chart Metric Type") that reads nothing like the parameter's own
    # internal name ("Parameter 7"/"pl.Selected Metric"). Must be captured; a paramctrl with NO
    # custom-title set (or set to 'false') must get no `custom_title` key at all -- additive.
    _db_paramctrl_xml = ET.fromstring(
        "<dashboard name='D'><size maxwidth='1300' maxheight='900' /><zones>"
        "<zone type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
        "<zone type-v2='paramctrl' custom-title='true' param='[Parameters].[Parameter 7]' "
        "x='0' y='0' w='10000' h='5000'>"
        "<formatted-text><run>Chart Metric Type</run></formatted-text>"
        "</zone>"
        "<zone type-v2='paramctrl' param='[Parameters].[Parameter 9]' "
        "x='0' y='10000' w='10000' h='5000' />"
        "</zone></zones></dashboard>")
    _root_pc = ET.fromstring("<workbook><dashboards></dashboards></workbook>")
    _root_pc.find("dashboards").append(_db_paramctrl_xml)
    _dbs_pc = extract_dashboards(_root_pc, {}, set())
    _pc_zones = {z["field"]: z for z in _dbs_pc[0]["zones"] if z["kind"] == "paramctrl"}
    assert _pc_zones["Parameter 7"].get("custom_title") == "Chart Metric Type", _pc_zones
    assert "custom_title" not in _pc_zones["Parameter 9"], _pc_zones["Parameter 9"]
    print("extract.py self-test OK (paramctrl custom-title override captured when set, "
          "absent -- not even the key -- when Tableau never set one)")

    # `_nav_button_hint`: real corpus shapes (10000056_achc_WF_Payments_Insights) -- a captioned
    # goto-sheet button, an icon-only goto-sheet button (no caption), and a goto-sheet button
    # with neither. A non-navigation button (e.g. show/hide, or a blank action) must return None
    # so the caller falls back to the honest generic "no mapped Power BI visual" gap instead of
    # claiming a navigation hint that was never there.
    _btn_captioned = ET.fromstring(
        "<zone type-v2='dashboard-object'>"
        "<button action='tabdoc:goto-sheet window-id=\"{DC4DC9F1-B29B-444E-8D13-A35E219A28F4}\"'>"
        "<button-visual-state><caption>Operational Performance</caption></button-visual-state>"
        "</button></zone>")
    assert _nav_button_hint(_btn_captioned) == "Operational Performance"

    _btn_icon_only = ET.fromstring(
        "<zone type-v2='dashboard-object'>"
        "<button action='tabdoc:goto-sheet window-id=\"{4BAABADC-BCDA-4384-8011-83BB92713579}\"'>"
        "<button-visual-state><image-path>Image/WF_icon_house_rgb_F1_WF Red.png</image-path>"
        "</button-visual-state></button></zone>")
    assert _nav_button_hint(_btn_icon_only) == "icon 'Image/WF_icon_house_rgb_F1_WF Red.png'"

    _btn_bare = ET.fromstring(
        "<zone type-v2='dashboard-object'>"
        "<button action='tabdoc:goto-sheet window-id=\"{CB665BD6-EB8A-43D7-91D6-E8E4FDE6CF7C}\"'>"
        "</button></zone>")
    assert _nav_button_hint(_btn_bare) == "(no caption or icon on the button)"

    _btn_not_nav = ET.fromstring(
        "<zone type-v2='dashboard-object'><button action=''>"
        "<button-visual-state><image-path>Image/info.png</image-path></button-visual-state>"
        "</button></zone>")
    assert _nav_button_hint(_btn_not_nav) is None, (
        "a non-goto-sheet button (e.g. show a dialog) must not be mistaken for a navigation "
        "button just because it has an icon")

    _zone_no_button = ET.fromstring("<zone type-v2='dashboard-object'/>")
    assert _nav_button_hint(_zone_no_button) is None
    print("extract.py self-test OK (_nav_button_hint: caption, icon-only, no-hint, and "
          "non-navigation-button cases)")

    # `_build_window_guid_map` / `_nav_button_target`: the GUID lives in a nested <simple-id>
    # child, not a <window> attribute -- real shape from 10000056_achc_WF_Payments_Insights.
    _wb_root = ET.fromstring(
        "<workbook><windows>"
        "<window class='dashboard' name='Operational Performance Summary'>"
        "<simple-id uuid='{DC4DC9F1-B29B-444E-8D13-A35E219A28F4}' /></window>"
        "<window class='worksheet' name='Some Raw Sheet'>"
        "<simple-id uuid='{AAAAAAAA-0000-0000-0000-000000000000}' /></window>"
        "</windows></workbook>")
    _guid_map = _build_window_guid_map(_wb_root)
    assert _guid_map["{DC4DC9F1-B29B-444E-8D13-A35E219A28F4}"] == (
        "dashboard", "Operational Performance Summary")
    assert _nav_button_target(_btn_captioned, _guid_map) == "Operational Performance Summary"
    # A goto-sheet button whose GUID resolves to a WORKSHEET-class window (not dashboard) has
    # no page of its own to point at in this pipeline -- must stay unresolved, not misfire.
    _btn_to_worksheet = ET.fromstring(
        "<zone type-v2='dashboard-object'>"
        "<button action='tabdoc:goto-sheet window-id=\"{AAAAAAAA-0000-0000-0000-000000000000}\"'>"
        "</button></zone>")
    assert _nav_button_target(_btn_to_worksheet, _guid_map) is None
    # A GUID with no <simple-id> anywhere in the file at all (real shape: Digital Sales - Leads
    # Dashboard has exactly one of these) must resolve to None, not raise.
    assert _nav_button_target(_btn_bare, _guid_map) is None
    assert _nav_button_target(_zone_no_button, _guid_map) is None
    assert _nav_button_caption(_btn_captioned.find("button")) == "Operational Performance"
    assert _nav_button_caption(_btn_icon_only.find("button")) is None
    print("extract.py self-test OK (_build_window_guid_map / _nav_button_target: nested "
          "simple-id resolution, worksheet-class rejection, dangling-GUID case)")

    # extract_url_actions -- real corpus shapes (Client Module - US: 'VAS_Home Icon' fixed URL,
    # 'VAS_Email Icon' a mailto: with an embedded field reference).
    _url_root = ET.fromstring(
        "<workbook><actions>"
        "<action caption='VAS_Home Icon'>"
        "<activation type='on-select' />"
        "<source dashboard='VAS Revenue' type='sheet' worksheet='Home Icon' />"
        "<link caption='Module Landing Page 1' expression='https://example.com/landing' />"
        "</action>"
        "<action caption='VAS_Email Icon'>"
        "<activation type='on-select' />"
        "<source dashboard='VAS Revenue' type='sheet' worksheet='Email Icon' />"
        "<link caption='Email 1' expression='mailto:&lt;[Calculation_1]&gt;' />"
        "</action>"
        "<action caption='Hover Tooltip Action'>"
        "<activation type='on-hover' />"
        "<source dashboard='VAS Revenue' type='sheet' worksheet='Hover Icon' />"
        "<link expression='https://example.com/hover' />"
        "</action>"
        "<action caption='Datasource-wide Action'>"
        "<activation type='on-select' />"
        "<source type='datasource' />"
        "<link expression='https://example.com/ds' />"
        "</action>"
        "<action caption='Filter Action (no link)'>"
        "<activation type='on-select' />"
        "<source dashboard='VAS Revenue' type='sheet' worksheet='Filter Trigger' />"
        "<command command='tsc:tsl-filter' />"
        "</action>"
        "<action caption='Duplicate target'>"
        "<activation type='on-select' />"
        "<source dashboard='VAS Revenue' type='sheet' worksheet='Home Icon' />"
        "<link expression='https://example.com/should-not-win' />"
        "</action>"
        "</actions></workbook>")
    _url_actions = extract_url_actions(_url_root)
    assert _url_actions == [{"dashboard": "VAS Revenue", "worksheet": "Home Icon",
                             "url": "https://example.com/landing",
                             "caption": "Module Landing Page 1"}], (
        "extract_url_actions must keep only the fixed-address, on-select, single-sheet URL "
        "action, first-match-wins on a duplicate target, and skip on-hover / datasource-wide "
        f"/ command(non-link) / field-ref-dynamic-URL actions -- got {_url_actions}")
    print("extract.py self-test OK (extract_url_actions: fixed-address on-select single-sheet "
          "URL action kept; on-hover, datasource-wide, command-only, dynamic-field-ref, and "
          "duplicate-target actions all correctly excluded)")

    # extract_action_source_worksheets -- reuses `_url_root` above. Unlike extract_url_actions,
    # a COMMAND-only action with no <link> at all (the 'Filter Trigger' tsc:tsl-filter case) DOES
    # count here -- this function tracks "does ANY on-select action originate here", not "is
    # there a fixed-address URL to reproduce". on-hover and datasource-wide sources are still
    # excluded (no single-sheet on-select trigger to translate either way), and a duplicate
    # target worksheet collapses to one entry (it's a set).
    _src_ws = extract_action_source_worksheets(_url_root)
    assert _src_ws == [("VAS Revenue", "Email Icon"), ("VAS Revenue", "Filter Trigger"),
                        ("VAS Revenue", "Home Icon")], (
        "extract_action_source_worksheets must include every on-select single-sheet action's "
        "source worksheet regardless of command/link shape, exclude on-hover and "
        f"datasource-wide sources, and dedup -- got {_src_ws}")
    print("extract.py self-test OK (extract_action_source_worksheets: command-only filter "
          "action source kept same as a URL action's; on-hover / datasource-wide excluded; "
          "duplicate source worksheet deduped)")

    # resolve_title: real corpus shapes from 10000056_achc_WF_Payments_Insights.
    _title_clean = ET.fromstring(
        "<title><formatted-text><run>Recent 3-Months</run></formatted-text></title>")
    assert resolve_title(_title_clean, "Total Payment Volume") == "Recent 3-Months"
    _title_sheetname = ET.fromstring(
        "<title><formatted-text><run>&lt;Sheet Name&gt; - Recent 12 Months</run>"
        "</formatted-text></title>")
    assert resolve_title(_title_sheetname, "Total Payment Volume - Deb/Cred") == (
        "Total Payment Volume - Deb/Cred - Recent 12 Months"
    ), "the <Sheet Name> token must be substituted with the worksheet's own name"
    _title_param = ET.fromstring(
        "<title><formatted-text><run>Sample Card &lt;[Parameters].[Parameter 1]&gt;</run>"
        "</formatted-text></title>")
    assert resolve_title(_title_param, "Some Sheet") is None, (
        "a title with an unresolvable dynamic token (parameter/field-value reference) must "
        "decline rather than emit the literal bracket syntax as if it were real text"
    )
    assert resolve_title(None, "Some Sheet") is None
    print("extract.py self-test OK (resolve_title: <Sheet Name> substitution, unresolvable "
          "dynamic-token decline)")

    # show_title / filter_mode: zone-level attributes extract_dashboards must now read.
    _wb_root_zones = ET.fromstring(
        "<workbook><worksheets>"
        "<worksheet name='W1'/><worksheet name='W2'/>"
        "</worksheets>"
        "<dashboards><dashboard name='D1'><zones>"
        "<zone name='W1' show-title='false' x='0' y='0' w='10000' h='10000'/>"
        "<zone name='W2' x='0' y='0' w='10000' h='10000'/>"
        "<zone type-v2='filter' mode='dropdown' param='[ds].[F]' x='0' y='0' w='1' h='1'/>"
        "</zones></dashboard></dashboards></workbook>")
    _dbs = extract_dashboards(_wb_root_zones, {}, {"W1", "W2"})
    _zones_by_name = {z["name"]: z for z in _dbs[0]["zones"] if z.get("kind") == "worksheet"}
    assert _zones_by_name["W1"]["show_title"] is False
    assert _zones_by_name["W2"]["show_title"] is True, (
        "show-title absent must default to shown (Tableau's own default), not hidden"
    )
    _filter_zone = next(z for z in _dbs[0]["zones"] if z.get("kind") == "filter")
    assert _filter_zone["filter_mode"] == "dropdown"
    print("extract.py self-test OK (extract_dashboards: show_title default/override, "
          "filter_mode captured)")

    # parse_formatted_text: real corpus shapes (achc) -- single-run bold caption, multi-run
    # zone (bold + plain), and alignment taken from the first run that declares one.
    _ft_single = ET.fromstring(
        "<formatted-text><run bold='true' fontalignment='1' fontcolor='#ffffff' "
        "fontsize='18' fontname='Segoe UI Semibold'>Card Sales Overview</run></formatted-text>")
    _runs, _align = parse_formatted_text(_ft_single)
    assert _runs == [{"text": "Card Sales Overview", "bold": True, "italic": False,
                       "color": "#ffffff", "fontsize": "18", "fontname": "Segoe UI Semibold"}]
    assert _align == "center"

    _ft_multi = ET.fromstring(
        "<formatted-text>"
        "<run bold='true' fontalignment='2'>Bold part </run>"
        "<run>plain part</run>"
        "</formatted-text>")
    _runs2, _align2 = parse_formatted_text(_ft_multi)
    assert len(_runs2) == 2 and _runs2[0]["bold"] is True and _runs2[1]["bold"] is False, _runs2
    assert _align2 == "right"

    assert parse_formatted_text(None) == ([], None)
    _ft_empty_run = ET.fromstring("<formatted-text><run>Æ</run></formatted-text>")
    assert parse_formatted_text(_ft_empty_run)[0] == [], (
        "a run that is pure layout-noise glyph (no real content after Æ->space) must not "
        "become a visible empty text run"
    )
    print("extract.py self-test OK (parse_formatted_text: multi-run bold/color/fontsize, "
          "alignment from first declaring run)")

    # _label_font: base-level datalabel font-size/font-family kept, a field-scoped conditional-
    # format override on the SAME style-rule excluded, first-rule-wins within a scope, None when
    # neither attribute is present anywhere -- real corpus shape (WorkbookA).
    _ws_label_font = ET.fromstring(
        "<worksheet><table><panes><pane><style>"
        "<style-rule element='datalabel'>"
        "<format attr='font-size' value='8' />"
        "<format attr='color' field='[ds].[x]' value='#fcb215' />"
        "</style-rule>"
        "</style></pane></panes></table></worksheet>")
    lf = _label_font(_ws_label_font)
    assert lf == {"size": "8", "family": None}, lf
    print(f"  OK  base datalabel font-size kept, field-scoped format excluded -> {lf}")

    _ws_no_font = ET.fromstring(
        "<worksheet><table><panes><pane><style>"
        "<style-rule element='datalabel'><format attr='text-align' value='center' /></style-rule>"
        "</style></pane></panes></table></worksheet>")
    assert _label_font(_ws_no_font) is None, "no font-size/font-family present must return None"
    print("  OK  a datalabel style-rule with no font-size/font-family -> None")

    _ws_multi_rule = ET.fromstring(
        "<worksheet><table><panes>"
        "<pane><style><style-rule element='datalabel'>"
        "<format attr='font-family' value='Segoe UI Semibold' /></style-rule></style></pane>"
        "<pane><style><style-rule element='datalabel'>"
        "<format attr='font-family' value='Agency FB' />"
        "<format attr='font-size' value='18' /></style-rule></style></pane>"
        "</panes></table></worksheet>")
    lf2 = _label_font(_ws_multi_rule)
    assert lf2 == {"size": "18", "family": "Segoe UI Semibold"}, lf2
    print(f"  OK  first rule to declare each attribute wins across multiple datalabel "
          f"style-rules -> {lf2}")

    # The cascade this scope choice exists for: `datalabel` only declares font-SIZE (the common
    # real-corpus shape -- a user widened just the value's text without touching its typeface), so
    # font-family falls back to the worksheet-wide default set once via Tableau's "Format > Font".
    _ws_cascade = ET.fromstring(
        "<worksheet><table><panes><pane><style>"
        "<style-rule element='datalabel'><format attr='font-size' value='20' /></style-rule>"
        "<style-rule element='worksheet'>"
        "<format attr='font-size' value='10' /><format attr='font-family' value='Segoe UI' />"
        "</style-rule>"
        "</style></pane></panes></table></worksheet>")
    lf3 = _label_font(_ws_cascade)
    assert lf3 == {"size": "20", "family": "Segoe UI"}, lf3
    print(f"  OK  datalabel's own font-size wins, worksheet-wide font-family fills the gap "
          f"datalabel left -> {lf3}")
    print("extract.py self-test OK (_label_font: datalabel base font kept, field-scoped "
          "excluded, worksheet-level fallback, None when absent)")

    # classify_filter: a single-member filter whose value is Tableau's own "(All)"
    # show-everything sentinel restricts nothing -- real corpus shape (WorkbookA), confirmed against
    # the field's own embedded extract to have zero rows literally valued "(All)". A real member
    # value that happens to spell something else entirely is untouched (no regression), and an
    # ACTUAL "(All)"-valued member reached via a `ui-action-filter` marker is still caught by the
    # EARLIER action check, not this one (both exclude it, for different, already-tested reasons).
    _f_all_sentinel = ET.fromstring(
        "<filter class='categorical' column='[ds].[rgn]'>"
        "<groupfilter function='member' level='[none:rgn:nk]' member='&quot;(All)&quot;' "
        "user:ui-domain='database' user:ui-enumeration='inclusive' "
        "xmlns:user='http://www.tableausoftware.com/xml/user' />"
        "</filter>")
    cf = classify_filter(_f_all_sentinel, "Rgn", {})
    assert cf["kind"] == "none", cf
    assert "(All)" in cf["reason"] and "sentinel" in cf["reason"], cf
    print(f"  OK  a single-member '(All)' filter restricts nothing -> kind={cf['kind']!r}")

    _f_real_member = ET.fromstring(
        "<filter class='categorical' column='[ds].[rgn]'>"
        "<groupfilter function='member' level='[none:rgn:nk]' member='&quot;DOM&quot;' />"
        "</filter>")
    cf2 = classify_filter(_f_real_member, "Rgn", {})
    assert cf2["kind"] == "in" and cf2["values"] == ["DOM"], cf2
    print(f"  OK  a real single-member value is unaffected (no regression) -> {cf2}")
    print("extract.py self-test OK (classify_filter: '(All)' sentinel restricts nothing, "
          "real member value unaffected)")

    # parse_zone_style: real corpus shape (achc) -- meaningful background-color kept, a
    # default/transparent one dropped so a zone Tableau never painted stays unpainted.
    _zone_bg = ET.fromstring(
        "<zone><zone-style>"
        "<format attr='background-color' value='#d71e28' />"
        "<format attr='border-style' value='none' />"
        "</zone-style></zone>")
    assert parse_zone_style(_zone_bg) == {
        "background_color": "#d71e28", "border_color": None, "border_width": None}

    _zone_no_bg = ET.fromstring(
        "<zone><zone-style>"
        "<format attr='background-color' value='none' />"
        "<format attr='border-style' value='none' />"
        "</zone-style></zone>")
    assert parse_zone_style(_zone_no_bg) is None, (
        "a zone with no real background AND no real border must return None, not an empty "
        "'paint nothing' object"
    )
    assert parse_zone_style(ET.fromstring("<zone/>")) is None
    print("extract.py self-test OK (parse_zone_style: real background kept, "
          "default/transparent dropped)")

    # translate_number_format: real corpus value (achc, 58 fields) is the only pattern
    # confidently translated; anything else declines rather than guesses.
    assert translate_number_format("p0.00%") == "0.00%"
    assert translate_number_format("p0%") == "0%"
    assert translate_number_format("p0.0000%") == "0.0000%"
    assert translate_number_format(None) is None
    assert translate_number_format("") is None
    assert translate_number_format("c0.00") is None, (
        "a currency format (or any other Tableau format-string class) must decline, not be "
        "guessed at from an unverified pattern"
    )
    assert translate_number_format("n0") is None
    print("extract.py self-test OK (translate_number_format: percentage pattern translated, "
          "every other Tableau format class declined)")

    # Two DIFFERENT standalone .tds roots (real shape: no name, no caption, only
    # formatted-name) must never collapse to the same tableau_name/caption. Regression for
    # the VRM Compliance Dashboard bug: both tds-resolved "Data" tables got tableau_name=None,
    # caption="Data" -- an identical allocation key -- so tmdl.py's allocate() silently
    # returned the FIRST table's already-allocated pbi name for the second, and the second's
    # emission overwrote the first's TMDL file outright with no residue entry.
    _tds_a = ET.fromstring(
        '<datasource formatted-name="MSD_Billing_Details" inline="true" version="18.1">'
        '<connection class="hyper" dbname="Data/Extracts/MSD_Billing_Details.hyper" />'
        '</datasource>')
    _tds_b = ET.fromstring(
        '<datasource formatted-name="MSD_Case_Details_Compliance" inline="true" version="18.1">'
        '<connection class="hyper" dbname="Data/Extracts/MSD_Case_Details_Compliance.hyper" />'
        '</datasource>')
    _rec_a = extract_datasource(_tds_a, None, "tds:MSD_Billing_Details.tdsx")
    _rec_b = extract_datasource(_tds_b, None, "tds:MSD_Case_Details_Compliance.tdsx")
    assert _rec_a["tableau_name"] is not None, (
        "a standalone .tds root with no name/caption must fall back to formatted-name, "
        f"got tableau_name={_rec_a['tableau_name']!r}")
    assert _rec_a["tableau_name"] != _rec_b["tableau_name"], (
        "two DIFFERENT tds-resolved tables must never share one tableau_name -- got "
        f"{_rec_a['tableau_name']!r} for both")
    assert _rec_a["caption"] == "MSD_Billing_Details" and _rec_b["caption"] == (
        "MSD_Case_Details_Compliance"), (_rec_a["caption"], _rec_b["caption"])
    print("extract.py self-test OK (formatted-name fallback disambiguates tds-resolved tables)")

    # classify_formula must match a table-calc/LOD FUNCTION CALL, never a bracketed field NAME
    # that merely contains a table-calc word followed by '(' -- the identical false-positive
    # class fixed in dax.is_aggregate. e.g. `[Rank (Overall)]` is a field NAME, not a RANK() call.
    _classify_true = [
        ("RANK(SUM([Sales]))", "tablecalc"),
        ("WINDOW_SUM([x])", "tablecalc"),
        ("{FIXED [Region]: SUM([Sales])}", "lod"),
        ("[a] + [b]", "scalar"),
        # TOTAL() carries no row-order/partition semantics -- unlike its table-calc siblings, it
        # does not need worksheet-level addressing, so dax.py's translator handles it directly
        # instead of it being blanket-routed to the addressing-needed H tier.
        ("TOTAL(SUM([x]))", "scalar"),
        ("TOTAL([already-a-measure])", "scalar"),
        # ...but TOTAL() wrapping a GENUINELY addressing-needing function must still route to H --
        # removing TOTAL from TABLECALC_RE must not blind it to the OTHER function inside.
        ("TOTAL(LOOKUP([x], -1))", "tablecalc"),
    ]
    for _f, _want in _classify_true:
        _got = classify_formula(_f)
        assert _got == _want, f"classify_formula({_f!r}) -> {_got!r}, want {_want!r}"
    _classify_false = [
        # Real corpus shape (NGPO Dashboard): a field NAME containing "Count (" collided with
        # \bCOUNT\s*\( in dax.is_aggregate; the same bracket-unstripped pattern exists here for
        # TABLECALC_RE's RANK/FIRST/LAST/TOTAL words.
        ("[Rank (Overall)] + [y]", "scalar"),
        ("[Total (2019)] - [Total (2018)]", "scalar"),
        ("[First (copy)]", "scalar"),
        ("[Running Total (est)]", "scalar"),
    ]
    for _f, _want in _classify_false:
        _got = classify_formula(_f)
        assert _got == _want, (
            f"classify_formula({_f!r}) -> {_got!r}, want {_want!r} -- bracketed field NAME "
            f"false-positived as a table-calc/LOD function call")
    print("extract.py self-test OK (classify_formula ignores bracketed field-name collisions)")

    # A <metadata-record> with class='measure' (Tableau's own default-aggregation-role guess for
    # this physical column, from SQL introspection) describes a REAL column just as much as
    # class='column' does, and one with NO <column> override at all is only ever visible through
    # its metadata-record. Regression for WorkbookB Dashboard's vwOI_Insights: 2 real columns were
    # class='measure' with no override, so they never reached rec["columns"] or object_id_of,
    # starving 2 of 13 object-graph relationships of a real key attribution and collapsing all
    # 14 objects into 1 flattened table via split_federated_datasource's all-or-nothing check.
    _measure_backfill_ds = ET.fromstring(
        '<datasource name="federated.0x1" caption="DS">'
        '<object-graph>'
        '<objects>'
        '<object id="A" caption="A"><properties context=""><relation table="[s].[a]" />'
        '</properties></object>'
        '<object id="B" caption="B"><properties context=""><relation table="[s].[b]" />'
        '</properties></object>'
        '</objects>'
        '<relationships>'
        '<relationship>'
        '<expression op="="><expression op="[id]" /><expression op="[a_id]" /></expression>'
        '<first-end-point object-id="A" unique-key="true" />'
        '<second-end-point object-id="B" />'
        '</relationship>'
        '</relationships>'
        '</object-graph>'
        '<metadata-records>'
        '<metadata-record class="column"><remote-name>id</remote-name>'
        '<local-name>[id]</local-name><object-id>[A]</object-id></metadata-record>'
        '<metadata-record class="measure"><remote-name>a_id</remote-name>'
        '<local-name>[a_id]</local-name><object-id>[B]</object-id></metadata-record>'
        '</metadata-records>'
        '</datasource>')
    _mb_rec = extract_datasource(_measure_backfill_ds, None, "test")
    _mb_cols = {c["name"]: c for c in _mb_rec["columns"]}
    assert "a_id" in _mb_cols, (
        "a class='measure' metadata-record with no <column> override must still reach "
        f"rec['columns'] -- got {sorted(_mb_cols)}")
    assert _mb_cols["a_id"]["object_id"] == "B", (
        "a class='measure' column's object-id attribution must resolve the same way a "
        f"class='column' one does -- got {_mb_cols['a_id']['object_id']!r}")
    assert _mb_cols["a_id"]["role"] == "measure", (
        "a class='measure' backfilled column's default role must reflect Tableau's own "
        f"aggregation-role guess, not the hardcoded 'dimension' default -- got "
        f"{_mb_cols['a_id']['role']!r}")
    _mb_subs = split_federated_datasource(_mb_rec)
    assert len(_mb_subs) == 2, (
        "the relationship's key column on the 'many' side is only attributed via its "
        f"class='measure' metadata-record; losing it must not collapse the split -- got "
        f"{len(_mb_subs)} table(s)")
    print("extract.py self-test OK (metadata-record backfill includes class='measure', "
          "not just class='column')")

    # Real corpus bug (WorkbookA, see extract_datasource's own docstring on `name_map`): a field with
    # NO <column> tag in THIS datasource (metadata-record only) must still get the SAME caption
    # every filter/shelf reference to it resolves to workbook-wide, via name_map -- not the bare
    # internal name verbatim. Two datasources sharing a raw field name, only one of which gives it
    # an explicit caption, is exactly the shape that broke Activation's "For View" filters.
    _nm_ds = ET.fromstring(
        '<datasource name="federated.0x2" caption="DS2">'
        '<connection class="hyper"><relation table="[s].[t]" type="table" />'
        '<metadata-records>'
        '<metadata-record class="column"><remote-name>for_view</remote-name>'
        '<local-name>[for_view]</local-name></metadata-record>'
        '<metadata-record class="column"><remote-name>truly_unnamed</remote-name>'
        '<local-name>[truly_unnamed]</local-name></metadata-record>'
        '</metadata-records>'
        '</connection>'
        '</datasource>')
    _nm_map = {"[for_view]": "For View"}  # as if some OTHER datasource gave it this caption
    _nm_rec = extract_datasource(_nm_ds, None, "test", name_map=_nm_map)
    _nm_cols = {c["source_column"]: c for c in _nm_rec["columns"]}
    assert _nm_cols["for_view"]["name"] == "For View", (
        "a metadata-record-only column must borrow name_map's workbook-wide caption for its own "
        f"raw name, so it matches what filters/shelves resolve to -- got "
        f"{_nm_cols['for_view']['name']!r}")
    assert _nm_cols["truly_unnamed"]["name"] == "truly_unnamed", (
        "a raw name with NO entry anywhere in name_map must still fall back to itself verbatim, "
        f"not raise or disappear -- got {_nm_cols['truly_unnamed']['name']!r}")
    _nm_rec_nomap = extract_datasource(_nm_ds, None, "test")
    _nm_cols_nomap = {c["source_column"]: c for c in _nm_rec_nomap["columns"]}
    assert _nm_cols_nomap["for_view"]["name"] == "for_view", (
        "no name_map at all (every existing self-test call site that omits it) must be unchanged "
        f"-- verbatim raw name, not a crash -- got {_nm_cols_nomap['for_view']['name']!r}")
    print("extract.py self-test OK (metadata-record-only backfill borrows name_map's "
          "workbook-wide caption instead of the bare internal name)")

    # Companion real-corpus bug (WorkbookA): the SAME hazard one level up, for an EXPLICIT <column> tag
    # that simply has no caption of ITS OWN -- not just the metadata-record-only backfill path
    # above. Acquisition's own `[prod_id]` column has a real <column> tag with no `caption`
    # attribute at all; Key Highlights' sibling datasource captions its OWN `[prod_id]` as
    # "Prod Id". Left bare, Acquisition's column stayed named "prod_id" (raw, lowercase) while
    # every worksheet field well captioned "Prod Id" resolved globally to Key Highlights' column
    # instead -- an unrelated table with no working relationship to Acquisition at that grain, so
    # "2.3 Issuer YoY Growth"/"2.3 Market YoY Growth" rendered the identical value for every
    # category (confirmed live: SUMMARIZECOLUMNS by the wrongly-bound column returned the exact
    # same number for all 13 real Prod Id values).
    _ec_ds = ET.fromstring(
        '<datasource name="federated.0x3" caption="DS3">'
        '<connection class="hyper"><relation table="[s].[t]" type="table" /></connection>'
        '<column name="[prod_id]" datatype="string" role="dimension" />'
        '<column name="[other]" caption="Other" datatype="string" role="dimension" />'
        '</datasource>')
    _ec_map = {"[prod_id]": "Prod Id"}  # as if Key Highlights' own captioned column set this
    _ec_rec = extract_datasource(_ec_ds, None, "test", name_map=_ec_map)
    _ec_cols = {c["source_column"]: c for c in _ec_rec["columns"]}
    assert _ec_cols["prod_id"]["name"] == "Prod Id", (
        "an explicit <column> tag with NO caption of its own must still borrow name_map's "
        f"workbook-wide caption, same as the metadata-record-only path above -- got "
        f"{_ec_cols['prod_id']['name']!r}")
    assert _ec_cols["other"]["name"] == "Other", (
        "a <column> tag that already HAS its own caption must keep it unchanged, never "
        f"overridden by name_map -- got {_ec_cols['other']['name']!r}")
    _ec_rec_nomap = extract_datasource(_ec_ds, None, "test")
    _ec_cols_nomap = {c["source_column"]: c for c in _ec_rec_nomap["columns"]}
    assert _ec_cols_nomap["prod_id"]["name"] == "prod_id", (
        "no name_map at all (every existing self-test call site that omits it) must be "
        f"unchanged -- verbatim raw name -- got {_ec_cols_nomap['prod_id']['name']!r}")
    print("extract.py self-test OK (an explicit <column> tag with no caption of its own also "
          "borrows name_map's workbook-wide caption, not just the metadata-record-only backfill)")

    # split_federated_datasource: a real star (A unique/"one", B many) with a same-object calc on
    # each side and one genuinely cross-object calc must split into 2 real tables, home the
    # single-object calcs correctly, and decline (not silently mis-home) the cross-object one.
    def _fixture_rec(a_unique=True, b_unique=False, cross_join=False, key_ok=True):
        og = {
            "objects": [{"id": "A", "caption": "A", "physical_table": "[s].[a]"},
                       {"id": "B", "caption": "B", "physical_table": "[s].[b]"}],
            "relationships": [{
                "from_object": "A", "to_object": "B",
                "from_unique": a_unique, "to_unique": b_unique,
                "key_pairs": [{"from": "id", "to": "a_id" if key_ok else "nonexistent"}],
                "cross_join": cross_join,
            }],
        }
        return {
            "tableau_name": "DS", "caption": "DS", "provenance": "test",
            "connections": [], "relations": [], "object_graph": og,
            "columns": [
                {"name": "Id", "tableau_name": "[id]", "object_id": "A", "datatype": "string",
                 "role": "dimension", "hidden": False, "semantic_role": None,
                 "source_column": "id", "from_metadata": False, "folder": None},
                {"name": "Name", "tableau_name": "[name]", "object_id": "A", "datatype": "string",
                 "role": "dimension", "hidden": False, "semantic_role": None,
                 "source_column": "name", "from_metadata": False, "folder": None},
                {"name": "A Id", "tableau_name": "[a_id]", "object_id": "B", "datatype": "string",
                 "role": "dimension", "hidden": False, "semantic_role": None,
                 "source_column": "a_id", "from_metadata": False, "folder": None},
                {"name": "B Col", "tableau_name": "[b_col]", "object_id": "B",
                 "datatype": "string", "role": "dimension", "hidden": False,
                 "semantic_role": None, "source_column": "b_col", "from_metadata": False,
                 "folder": None},
            ],
            "calcs": [
                {"name": "OnlyA", "tableau_name": "[OnlyA]", "formula": "[name]",
                 "datatype": "string", "role": "dimension", "kind": "scalar",
                 "tc_addressing": None, "tc_spec": None, "description": None, "folder": None,
                 "object_id": None},
                {"name": "OnlyB", "tableau_name": "[OnlyB]", "formula": "[b_col]",
                 "datatype": "string", "role": "dimension", "kind": "scalar",
                 "tc_addressing": None, "tc_spec": None, "description": None, "folder": None,
                 "object_id": None},
                {"name": "CrossAB", "tableau_name": "[CrossAB]", "formula": "[name] + [b_col]",
                 "datatype": "string", "role": "dimension", "kind": "scalar",
                 "tc_addressing": None, "tc_spec": None, "description": None, "folder": None,
                 "object_id": None},
            ],
            "groups": [], "bins": [], "hierarchies": [], "aliases": {},
            "datasource_filters": [], "extract": None, "initial_sql": [],
            "internal_dropped": [],
        }

    _subs = split_federated_datasource(_fixture_rec())
    assert len(_subs) == 2, f"expected a real 2-table split, got {len(_subs)}"
    _a = next(s for s in _subs if s["tableau_name"] == "A")
    _b = next(s for s in _subs if s["tableau_name"] == "B")
    assert {c["name"] for c in _a["columns"]} == {"Id", "Name"}, _a["columns"]
    assert {c["name"] for c in _b["columns"]} == {"A Id", "B Col"}, _b["columns"]
    assert {c["name"] for c in _a["calcs"]} == {"OnlyA"}, (
        "OnlyA must home on A (its only reference); CrossAB now finds B (the many side) as a "
        f"valid RELATED() hub and must move there, not stay on A: got {[c['name'] for c in _a['calcs']]}")
    assert {c["name"] for c in _b["calcs"]} == {"OnlyB", "CrossAB"}, (
        "CrossAB mixes A's column with B's own -- B is the many side of the A~B relationship, so "
        f"a row on B can RELATED() to its one A row directly; it must land on B: {_b['calcs']}")
    _cross = next(c for c in _b["calcs"] if c["name"] == "CrossAB")
    assert not _cross.get("cross_object_note"), (
        "a resolvable hub exists (B) -- this must NOT decline: " f"{_cross.get('cross_object_note')}")
    assert _cross.get("cross_table_refs") == {"name": "A"}, (
        "the reference to A's own column must be flagged for a RELATED('A'[...]) wrap in "
        f"tmdl.py, got {_cross.get('cross_table_refs')!r}")
    assert "_home" not in _cross, "internal _home bookkeeping must not leak into the final IR"
    assert _a.get("_resolved_joins") == [
        {"one_table": "A", "many_table": "B", "one_field": "id", "many_field": "a_id"}], (
        f"real relationship must be recorded on the primary (one-side) table: "
        f"{_a.get('_resolved_joins')!r}")
    print("extract.py self-test OK (federated datasource splits into real tables + relationship, "
          "cross-object calc rehomes onto a RELATED()-legal hub instead of always defaulting to "
          "primary)")

    def _hub_fixture_rec():
        """A~H~B star (H many-relative-to-A, one-relative-to-B) so a calc that mixes a LEAF (A)
        with the HUB itself (H) has a resolvable RELATED() hub (H), while a calc that mixes the
        two LEAVES (A, B) -- which only meet through H, and never in a RELATED()-legal direction
        for either -- has none and must still decline."""
        og = {
            "objects": [{"id": "A", "caption": "A", "physical_table": "[s].[a]"},
                       {"id": "B", "caption": "B", "physical_table": "[s].[b]"},
                       {"id": "H", "caption": "H", "physical_table": "[s].[h]"}],
            "relationships": [
                {"from_object": "H", "to_object": "A", "from_unique": False, "to_unique": True,
                 "key_pairs": [{"from": "hk1", "to": "ak1"}], "cross_join": False},
                {"from_object": "B", "to_object": "H", "from_unique": False, "to_unique": True,
                 "key_pairs": [{"from": "bk1", "to": "hk2"}], "cross_join": False},
            ],
        }

        def _col(name, tname, obj):
            return {"name": name, "tableau_name": f"[{tname}]", "object_id": obj,
                    "datatype": "string", "role": "dimension", "hidden": False,
                    "semantic_role": None, "source_column": tname, "from_metadata": False,
                    "folder": None}

        def _calc(name, formula):
            return {"name": name, "tableau_name": f"[{name}]", "formula": formula,
                    "datatype": "string", "role": "dimension", "kind": "scalar",
                    "tc_addressing": None, "tc_spec": None, "description": None, "folder": None,
                    "object_id": None}

        return {
            "tableau_name": "DS", "caption": "DS", "provenance": "test",
            "connections": [], "relations": [], "object_graph": og,
            "columns": [_col("Ak1", "ak1", "A"), _col("ACol", "a_col", "A"),
                       _col("Bk1", "bk1", "B"), _col("BCol", "b_col2", "B"),
                       _col("Hk1", "hk1", "H"), _col("Hk2", "hk2", "H"),
                       _col("HCol", "h_col", "H")],
            "calcs": [_calc("HubOK", "[h_col] + [a_col]"),
                     _calc("NoHub", "[a_col] + [b_col2]"),
                     _calc("Nested", "[HubOK] + [b_col2]")],
            "groups": [], "bins": [], "hierarchies": [], "aliases": {},
            "datasource_filters": [], "extract": None, "initial_sql": [],
            "internal_dropped": [],
        }

    _hsubs = split_federated_datasource(_hub_fixture_rec())
    assert len(_hsubs) == 3, f"expected a real 3-table split, got {len(_hsubs)}"
    _h = next(s for s in _hsubs if s["tableau_name"] == "H")
    _hubok = next(c for c in _h["calcs"] if c["name"] == "HubOK")
    assert not _hubok.get("cross_object_note"), (
        f"H can RELATED() to A directly -- HubOK must resolve, not decline: "
        f"{_hubok.get('cross_object_note')}")
    assert _hubok.get("cross_table_refs") == {"a_col": "A"}, _hubok.get("cross_table_refs")
    _nohub = next((c for s in _hsubs for c in s["calcs"] if c["name"] == "NoHub"), None)
    assert _nohub is not None, "NoHub must still land somewhere (the primary), not vanish"
    assert _nohub.get("cross_object_note"), (
        "A and B only meet through H, and neither direction is RELATED()-legal from a leaf "
        f"to the other leaf -- NoHub must still decline: {_nohub}")
    assert not _nohub.get("cross_table_refs"), _nohub.get("cross_table_refs")
    _b = next(s for s in _hsubs if s["tableau_name"] == "B")
    _nested = next((c for c in _b["calcs"] if c["name"] == "Nested"), None)
    assert _nested is not None, (
        "Nested references HubOK (itself a 2-object calc) plus B's own column -- B is a valid "
        f"hub for {{home_of(HubOK)=H, B}}, so this must resolve onto B: {[c['name'] for s in _hsubs for c in s['calcs']]}")
    assert not _nested.get("cross_object_note"), (
        f"home_of() must resolve HubOK's OWN hub (H) instead of collapsing it to the _CROSS "
        f"sentinel just because HubOK itself mixes 2 objects: {_nested.get('cross_object_note')}")
    assert _nested.get("cross_table_refs") == {"HubOK": "H"}, (
        "only the reference to HubOK (homed on H, not B) needs a cross-table wrap -- b_col2 is "
        f"already B's own column: {_nested.get('cross_table_refs')!r}")
    print("extract.py self-test OK (hub resolution finds a real RELATED()-legal anchor when one "
          "exists, still declines a genuine no-hub chain, and propagates through a nested "
          "calc-to-calc reference instead of collapsing it to the cross sentinel)")

    # Every one of these must decline the split entirely (fail closed) -- no partial split.
    for _kwargs, _why in [
        ({"a_unique": False, "b_unique": False}, "no side declared unique -- cardinality unknown"),
        ({"cross_join": True}, "a Tableau '1 = 1' relate has no real key"),
        ({"key_ok": False}, "a key field that is not actually attributed to the object it names"),
    ]:
        _r = _fixture_rec(**_kwargs)
        _out = split_federated_datasource(_r)
        assert _out == [_r], f"must decline to split when {_why}, got {len(_out)} table(s)"
    print("extract.py self-test OK (split declines rather than guesses on an ineligible graph)")

    # A calc's own <calculation><table-calc> is often just the bare generic default
    # (ordering-type='Rows', no field), but Tableau saves a SECOND, richer copy per worksheet
    # usage under <worksheet><table><view><datasource-dependencies><column-instance
    # column='[id]'><table-calc ordering-field=... ordering-type='Field' />. Real corpus example
    # (Digital Sales - Leads Dashboard's `Rank - Text`, addressed by `lead_owner` in worksheet
    # `Leads Team View`) -- worksheet_tc_addressing() must resolve that field via name_map, and
    # a bare ordering-type (no 'Field'/no ordering-field) must be skipped, not misread as a hit.
    _ws_tc_root = ET.fromstring(
        "<workbook><worksheets>"
        "<worksheet name='Leads Team View'><table><view><datasource-dependencies "
        "datasource='federated.abc'>"
        "<column-instance column='[Calculation_1]' name='n1'>"
        "<table-calc ordering-field='[federated.abc].[lead_owner]' ordering-type='Field' />"
        "</column-instance>"
        "<column-instance column='[Calculation_2]' name='n2'>"
        "<table-calc ordering-type='Rows' />"
        "</column-instance>"
        "</datasource-dependencies></view></table></worksheet>"
        "</worksheets></workbook>")
    _ws_tc = worksheet_tc_addressing(_ws_tc_root, {"[lead_owner]": "Lead Owner"})
    assert _ws_tc == {"Calculation_1": [{"field": "Lead Owner", "worksheet": "Leads Team View"}]}, _ws_tc
    print("extract.py self-test OK (worksheet-saved table-calc addressing resolved, generic "
          "ordering-type skipped)")

    # A single <column-instance> can carry MULTIPLE sibling <table-calc> children -- Tableau's
    # shape when a shelf-placed calc (here 'Y Axis Position', column-instance id Calculation_3)
    # references OTHER calcs that are never placed on a shelf themselves (AANR test / Normalized
    # AANR, real corpus example). The column-instance's OWN addressing has no `field=`; each
    # sibling calc's entry names its target via `field='[ds].[Calculation_X]'`, distinct from the
    # column-instance's own `column` id.
    _ws_tc_root2 = ET.fromstring(
        "<workbook><worksheets>"
        "<worksheet name='Leads Team View'><table><view><datasource-dependencies "
        "datasource='federated.abc'>"
        "<column-instance column='[Calculation_3]' name='n3'>"
        "<table-calc ordering-field='[federated.abc].[Lead Stage]' ordering-type='Field' />"
        "<table-calc field='[federated.abc].[Calculation_4]' "
        "ordering-field='[federated.abc].[Lead Stage]' ordering-type='Field' />"
        "<table-calc field='[federated.abc].[Calculation_5]' "
        "ordering-field='[federated.abc].[Lead Stage]' ordering-type='Field' />"
        "</column-instance>"
        "</datasource-dependencies></view></table></worksheet>"
        "</worksheets></workbook>")
    _ws_tc2 = worksheet_tc_addressing(_ws_tc_root2, {"[Lead Stage]": "Lead Stage"})
    assert set(_ws_tc2) == {"Calculation_3", "Calculation_4", "Calculation_5"}, _ws_tc2
    assert all(hits == [{"field": "Lead Stage", "worksheet": "Leads Team View"}]
               for hits in _ws_tc2.values()), _ws_tc2
    print("extract.py self-test OK (sibling table-calc entries with a 'field=' target resolved "
          "to the RIGHT calc, not the column-instance's own id)")

    # A shelf with 2+ stacked fields is written as an arbitrarily-deep nested `(A / (B / C))`
    # expression -- real corpus example: Opportunity List nests 10+ fields this way. The OLD
    # code split on the literal substring "]/[" (no spaces, no parens), which never matched this
    # real shape and silently kept only whatever `parse_fieldref` happened to match first,
    # dropping every field after it -- e.g. a real "Lead Business Unit" / "Product Portfolio
    # Rollup" two-level row hierarchy collapsed to just "Lead Business Unit".
    _shelf_name_map = {"[lead_business_unit]": "Lead Business Unit",
                        "[product_portfolio_rollup]": "Product Portfolio Rollup"}
    _shelf_ws_root = ET.fromstring(
        "<workbook><worksheets><worksheet name='Lead Business Unit'>"
        "<table><view>"
        "<rows onTop='true' total='true'>(["
        "federated.x].[none:lead_business_unit:nk] / [federated.x]"
        ".[none:product_portfolio_rollup:nk])</rows>"
        "<cols>[federated.x].[:Measure Names]</cols>"
        "</view></table>"
        "</worksheet></worksheets></workbook>")
    _shelf_ws = extract_worksheets(_shelf_ws_root, _shelf_name_map)[0]
    assert [r["field"] for r in _shelf_ws["shelves"]["rows"]] == [
        "Lead Business Unit", "Product Portfolio Rollup"], _shelf_ws["shelves"]["rows"]
    assert _shelf_ws["shelves"]["rows_total"] is True, _shelf_ws["shelves"]
    # No `[ds].[field]`-shaped match exists in "[ds].[:Measure Names]" -- falls back to the
    # pre-existing single-ref parse unchanged (same raw, partially-stripped string it always
    # produced for this pseudo-field; not a regression target of this fix).
    assert _shelf_ws["shelves"]["cols"] == [
        {"field": "federated.x].[:Measure Names", "agg": None, "table_calc": None}], (
        _shelf_ws["shelves"]["cols"])
    print("extract.py self-test OK (multi-field nested shelf parsed as a real hierarchy, not "
          "collapsed to its first field)")

    # 3-level nesting (real corpus example: Completed Meeting Breakout View) and no total='true'
    # attribute at all (also that same worksheet) must both work -- deepest nesting and the
    # boolean default (False, not a crash) are the two edges the two-level case above can't catch.
    _shelf_ws_root2 = ET.fromstring(
        "<workbook><worksheets><worksheet name='Nested3'>"
        "<table><view>"
        "<rows onTop='true'>([federated.x].[none:a:nk] / "
        "([federated.x].[none:b:nk] / [federated.x].[none:c:nk]))</rows>"
        "</view></table>"
        "</worksheet></worksheets></workbook>")
    _shelf_ws2 = extract_worksheets(_shelf_ws_root2, {})[0]
    assert [r["field"] for r in _shelf_ws2["shelves"]["rows"]] == ["a", "b", "c"], (
        _shelf_ws2["shelves"]["rows"])
    assert _shelf_ws2["shelves"]["rows_total"] is False, _shelf_ws2["shelves"]
    print("extract.py self-test OK (3-level nested shelf, and rows_total defaults to False)")

    # <table-calc>'s `level-break` attribute ("Restart every: <field>" in Tableau's own "Edit
    # Table Calculation" dialog) is the partition boundary a RANK-family calc resets at -- tmdl.py
    # needs it, resolved to a plain caption, to translate one via RANKX/ALLEXCEPT. Ground truth:
    # NGPO Dashboard New - AP Version (MSC)'s `Rank - Topic Area` =
    # `RANK_UNIQUE(zn([Calculation_...]),'desc')`, `level-break="[federated.HASH].[topic_area]"`.
    _tc = ET.fromstring(
        '<calculation class="tablecalc" ordering-type="Field" '
        'level-break="[federated.HASH].[topic_area]">'
        '<order field="[federated.HASH].[issuer_name]" />'
        '</calculation>')
    _spec = table_calc_spec(_tc)
    assert _spec["level_break"] == "[federated.HASH].[topic_area]", _spec
    assert _spec.get("level_break_field") is None, (
        "level_break_field must not be resolved before name_map exists -- that happens in the "
        f"same extract()-time post-pass as order_fields, not inside table_calc_spec: {_spec!r}")
    _name_map = {"[topic_area]": "Topic Area"}
    _resolved_cap, _agg = parse_fieldref(_spec["level_break"], _name_map)
    assert _resolved_cap == "Topic Area", _resolved_cap
    _spec_no_break = table_calc_spec(ET.fromstring('<calculation class="tablecalc" />'))
    assert _spec_no_break["level_break"] is None and _spec_no_break.get("level_break_field") is \
        None, "a table-calc with no level-break attribute must resolve to None, never guessed"
    print("extract.py self-test OK (table-calc level-break resolves to a plain caption)")

    # The 4-segment shape a quick-table-calc PILL writes directly onto a shelf
    # (`[agg:underlying-agg:FIELD:suffix]`, e.g. WorkbookB Dashboard's real "1W - Slice By" cols
    # ref) must resolve FIELD as the real field, not the underlying-agg placeholder -- the exact
    # bug `_fieldref_parts` fixes. No existing fixture above exercises this shape, which is why it
    # was never caught.
    _pcto_cap, _pcto_agg = parse_fieldref(
        "[federated.x].[pcto:usr:real_field:qk]", {"[real_field]": "Real Field"})
    assert (_pcto_cap, _pcto_agg) == ("Real Field", "pcto"), (_pcto_cap, _pcto_agg)
    _pcdf_cap, _pcdf_agg = parse_fieldref(
        "[federated.x].[pcdf:sum:other_field:qk]", {"[other_field]": "Other Field"})
    assert (_pcdf_cap, _pcdf_agg) == ("Other Field", "pcdf"), (_pcdf_cap, _pcdf_agg)
    # Every existing 3-segment/1-segment shape stays unaffected -- re-check a representative one
    # of each here rather than trusting the regex change by inspection alone.
    _cap3, _agg3 = parse_fieldref("[federated.x].[none:lead_business_unit:nk]",
                                   {"[lead_business_unit]": "Lead Business Unit"})
    assert (_cap3, _agg3) == ("Lead Business Unit", None), (_cap3, _agg3)
    _cap1, _agg1 = parse_fieldref("[federated.abc].[lead_owner]", {})
    assert (_cap1, _agg1) == ("lead_owner", None), (_cap1, _agg1)
    print("extract.py self-test OK (4-segment quick-table-calc shelf ref resolves the REAL "
          "field, not the underlying-agg placeholder; 3-segment/1-segment refs unchanged)")

    # `extract_worksheets()`'s own inline shelf-text scan is a SECOND, independently-broken call
    # site (it read `m.group(1)`/`m.group(2)` directly instead of going through `parse_fieldref`)
    # -- must get the identical fix, not just the shared helper's other caller.
    _pcto_shelf_root = ET.fromstring(
        "<workbook><worksheets><worksheet name='W'><table><view>"
        "<cols>[federated.x].[pcto:usr:real_field:qk]</cols>"
        "</view></table></worksheet></worksheets></workbook>")
    _pcto_shelf_ws = extract_worksheets(_pcto_shelf_root, {"[real_field]": "Real Field"})[0]
    assert _pcto_shelf_ws["shelves"]["cols"] == [
        {"field": "Real Field", "agg": "pcto", "table_calc": None}], _pcto_shelf_ws["shelves"]["cols"]
    assert _pcto_shelf_ws["encodings"] == [
        {"channel": "cols", "field": "Real Field", "agg": "pcto", "table_calc": None}], (
        _pcto_shelf_ws["encodings"])
    print("extract.py self-test OK (shelf-text scan's own inline FIELDREF use gets the same fix)")

    # RC-A: 5-segment quick-table-calc shelf token (`pcto:sum:FIELD:qk:N`) -- the trailing :N is
    # a partition-scope index Tableau appends when a pill is scoped (e.g. :4 means "Table across").
    # Old code: parts[-2] on ["sum","FIELD","qk","4"] picks "qk", not FIELD.
    # Fixed code: strip trailing digit-only segments first, then parts[-2].
    # Real corpus case: WorkbookA Usage_split cols ref `[pcto:sum:Calculation_993606723128832005:qk:4]`
    # was yielding field="qk", so the pipeline couldn't identify the underlying measure at all.
    _pcto5_cap, _pcto5_agg = parse_fieldref(
        "[federated.x].[pcto:sum:Calculation_993606723128832005:qk:4]",
        {"[Calculation_993606723128832005]": "Issr Vol Txn Cnt Cy"})
    assert (_pcto5_cap, _pcto5_agg) == ("Issr Vol Txn Cnt Cy", "pcto"), (
        f"5-segment pcto:sum:FIELD:qk:N must extract FIELD, not 'qk' -- "
        f"got ({_pcto5_cap!r}, {_pcto5_agg!r})")
    # Existing 4-segment shape must remain unaffected by the strip-digits loop.
    _pcto4_cap2, _pcto4_agg2 = parse_fieldref(
        "[federated.x].[pcto:usr:real_field:qk]", {"[real_field]": "Real Field"})
    assert (_pcto4_cap2, _pcto4_agg2) == ("Real Field", "pcto"), (
        f"4-segment shape must be unaffected -- got ({_pcto4_cap2!r}, {_pcto4_agg2!r})")
    print("extract.py self-test OK (RC-A: 5-segment pcto:sum:FIELD:qk:N correctly extracts "
          "FIELD instead of 'qk'; 4-segment shape unchanged)")

    # RC-B: worksheet-local <column> thin-wrapper alias. WorkbookA's Usage_Metrics_SPAC_header (and 5
    # sibling worksheets) defines [Calculation_1136595959878615049] with formula
    # ([Calculation_868913308838137858]) -- a bare pass-through to the real "SPAC Difference"
    # measure. Before this fix, name_map had no entry for the wrapper id, so any shelf reference
    # to it resolved to a bare raw id string and _resolve_field returned None.
    # This test verifies that extract()'s RC-B extension adds wrapper -> real caption to name_map.
    _w_root = ET.fromstring(
        "<workbook>"
        "<datasources>"
        "<datasource name='federated.0x1' caption='DS'>"
        "<column name='[Calculation_868913308838137858]' caption='SPAC Difference' />"
        "</datasource>"
        "</datasources>"
        "<worksheets><worksheet name='Usage_Metrics_SPAC_header'><table><view>"
        "<datasource-dependencies datasource='federated.0x1'>"
        "<column name='[Calculation_1136595959878615049]' caption='([SPAC Difference])'>"
        "<calculation formula='([Calculation_868913308838137858])' />"
        "</column>"
        "<column name='[Complex_Calc]' caption='Complex'>"
        "<calculation formula='[Calculation_868913308838137858] * 2' />"
        "</column>"
        "</datasource-dependencies>"
        "</view></table></worksheet></worksheets>"
        "</workbook>")
    _w_all_ds = _w_root.findall("./datasources/datasource")
    _w_nm = {c.get("name"): c.get("caption")
             for d in _w_all_ds for c in d.iter("column")
             if c.get("name") and c.get("caption")}
    for _ws_el in _w_root.findall(".//worksheets/worksheet"):
        for _col in _ws_el.findall(".//datasource-dependencies/column"):
            _cname = _col.get("name")
            if not _cname or _cname in _w_nm:
                continue
            _calc = _col.find("calculation")
            if _calc is None:
                continue
            _wm2 = _WRAPPER_ALIAS_RE.match((_calc.get("formula") or "").strip())
            if _wm2:
                _real_cap2 = _w_nm.get(f"[{_wm2.group(1)}]")
                if _real_cap2:
                    _w_nm[_cname] = _real_cap2
    assert _w_nm.get("[Calculation_1136595959878615049]") == "SPAC Difference", (
        "worksheet-local thin-wrapper alias must resolve to its real field's caption -- "
        f"got {_w_nm.get('[Calculation_1136595959878615049]')!r}")
    assert _w_nm.get("[Calculation_868913308838137858]") == "SPAC Difference", (
        f"real field caption must remain unchanged -- got "
        f"{_w_nm.get('[Calculation_868913308838137858]')!r}")
    assert "[Complex_Calc]" not in _w_nm, (
        "a non-wrapper formula (not bare ([id])) must NOT be aliased -- "
        f"got {_w_nm.get('[Complex_Calc]')!r}")
    print("extract.py self-test OK (RC-B: worksheet-local thin-wrapper alias extends name_map; "
          "real field unchanged; non-wrapper formula not aliased)")

    # `worksheet_pcto_meta()` + its wiring into `extract_worksheets()`: a Percent-of-Total quick
    # calc attached directly to a shelf pill's OWN column-instance must surface as THAT shelf
    # ref's `table_calc` metadata -- and a sibling `field=`-override `<table-calc>` (RANK's own
    # addressing shape) on the SAME column-instance must be correctly skipped by this function,
    # not double-counted. Real corpus shape (WorkbookB Dashboard's "3B - Days to CE Eligible"):
    # TWO column-instances share the IDENTICAL `column='[Calculation_X]'` but differ in `name=`
    # -- one plain (`name='[sum:Calculation_X:qk]'`, actually on the Rows shelf, no table-calc)
    # and one Percent-of-Total-tagged (`name='[pcto:sum:Calculation_X:qk]'`, used elsewhere in
    # the SAME worksheet). Both fixtures below share `column='[Calculation_X]'` on purpose, to
    # prove the plain one is NOT wrongly tagged with the other's quick-calc metadata.
    _pcto_meta_root = ET.fromstring(
        "<workbook><worksheets><worksheet name='W'><table><view>"
        "<rows>[federated.x].[sum:Calculation_X:qk]</rows>"
        "<cols>[federated.x].[pcto:sum:Calculation_X:qk]</cols>"
        "<datasource-dependencies>"
        "<column-instance column='[Calculation_X]' name='[sum:Calculation_X:qk]' />"
        "<column-instance column='[Calculation_X]' name='[pcto:sum:Calculation_X:qk]'>"
        "<table-calc type='PctTotal' ordering-type='Rows' />"
        "<table-calc field='[federated.x].[Calculation_9]' ordering-type='Field' "
        "ordering-field='[federated.x].[none:other:nk]' />"
        "</column-instance>"
        "</datasource-dependencies>"
        "</view></table></worksheet></worksheets></workbook>")
    _pcto_meta = worksheet_pcto_meta(_pcto_meta_root, {})
    assert _pcto_meta == {"W": {"pcto:sum:Calculation_X:qk":
                          {"type": "PctTotal", "ordering_type": "Rows", "aggregation": None,
                           "ordering_field": None}}}, _pcto_meta
    _pcto_meta_ws = extract_worksheets(
        _pcto_meta_root, {"[Calculation_X]": "Real Field"}, pcto_meta=_pcto_meta)[0]
    # The Rows shelf ref is the PLAIN instance (`sum:...`, no pcto) -- it must NOT pick up the
    # sibling pcto-tagged instance's metadata just because they share the same `column=`.
    assert _pcto_meta_ws["shelves"]["rows"] == [
        {"field": "Real Field", "agg": "sum", "table_calc": None}], (
        _pcto_meta_ws["shelves"]["rows"])
    # The Cols shelf ref IS the pcto-tagged instance -- its `table_calc` must carry the merged
    # `underlying_agg` ("sum", this match's own 4-segment middle-segment), since this real
    # `<table-calc>` has no explicit `aggregation=` attribute of its own.
    assert _pcto_meta_ws["shelves"]["cols"] == [{"field": "Real Field", "agg": "pcto",
        "table_calc": {"type": "PctTotal", "ordering_type": "Rows", "aggregation": None,
                       "ordering_field": None, "underlying_agg": "sum"}}], (
        _pcto_meta_ws["shelves"]["cols"])
    print("extract.py self-test OK (worksheet_pcto_meta captures a shelf pill's own quick-calc "
          "type/ordering-type/aggregation, skips a sibling field=-override table-calc, and "
          "extract_worksheets does NOT cross-tag a sibling PLAIN column-instance that shares "
          "the same underlying column= but a different name=)")

    # "Compute Using: Specific Dimension(s)" on a quick-calc pill itself (`ordering-type='Field'`
    # + a real `ordering-field=`) must resolve to a plain caption, exactly like RANK's own
    # addressing already does via `worksheet_tc_addressing()` -- same source data, same
    # resolution, just captured by this sibling function instead.
    _pcto_field_root = ET.fromstring(
        "<workbook><worksheets><worksheet name='W'><table><view>"
        "<datasource-dependencies>"
        "<column-instance column='[Calculation_X]' name='[pcto:sum:Calculation_X:qk]'>"
        "<table-calc type='PctTotal' ordering-type='Field' "
        "ordering-field='[federated.x].[none:region:nk]' />"
        "</column-instance>"
        "</datasource-dependencies>"
        "</view></table></worksheet></worksheets></workbook>")
    _pcto_field_meta = worksheet_pcto_meta(_pcto_field_root, {"[region]": "Region"})
    assert _pcto_field_meta["W"]["pcto:sum:Calculation_X:qk"]["ordering_field"] == "Region", (
        _pcto_field_meta)
    print("extract.py self-test OK (worksheet_pcto_meta resolves a quick-calc's own "
          "Specific-Dimension(s) ordering-field to a plain caption)")

    if len(sys.argv) < 3:
        print("Usage: extract.py <src.twbx|.twb> <workdir> [tds_dir]")
        raise SystemExit(1)
    ir = extract(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    t = ir["tables"][0] if ir["tables"] else {}
    print(json.dumps({
        "workbook": ir["workbook"], "tables": len(ir["tables"]),
        "columns": sum(len(x["columns"]) for x in ir["tables"]),
        "calcs": sum(len(x["calcs"]) for x in ir["tables"]),
        "calc_kinds": dict(collections.Counter(
            c["kind"] for x in ir["tables"] for c in x["calcs"])),
        "groups": sum(len(x["groups"]) for x in ir["tables"]),
        "bins": sum(len(x["bins"]) for x in ir["tables"]),
        "hierarchies": sum(len(x["hierarchies"]) for x in ir["tables"]),
        "worksheets": len(ir["worksheets"]), "dashboards": len(ir["dashboards"]),
        "parameters": len(ir["parameters"]),
        "marks": dict(collections.Counter(w["mark"] for w in ir["worksheets"])),
        "images": len(ir["package"]["images"]),
        "sibling_tds": ir["sibling_tds"], "unresolved_proxies": ir["unresolved_proxies"],
        "role_inferred": ir["stats"]["role_inferred_columns"],
    }, indent=1))
