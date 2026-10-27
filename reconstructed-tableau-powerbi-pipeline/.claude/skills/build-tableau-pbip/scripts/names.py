#!/usr/bin/env python3
"""
S2 -- Name Authority and Binding Ledger.

The piece whose absence caused the most damage in the prior architecture. PBIR binds to model
objects purely by NAME (`Entity` = table, `Property` = column/measure) with no ID indirection, so
every identifier must be allocated ONCE, before either builder runs, and both must resolve
through this.

The failure it exists to prevent, verbatim from the prior hybrid build notes: the semantic model
emitted a parameter table called `Param Show as` while the report referenced
`Parameters.Show as`. Every parameter-bound visual dangled. Here the convention lives in one
place and is read by both sides, so the mismatch is structurally impossible.
"""
import re
import hashlib

# TMDL quoting: enclose a name in single quotes if it contains . = : ' or whitespace.
# Escape an embedded single quote by doubling it.
_NEEDS_QUOTE = re.compile(r"[.=:'\s]")

# TOM/TMDL will not accept these as object names.
RESERVED = {"table", "column", "measure", "model", "database", "partition", "relationship",
            "hierarchy", "level", "role", "perspective", "culture", "expression", "annotation",
            "ref", "datasource"}

MAX_NAME = 100  # TOM allows more, but long names + deep nesting breach the 260-char path limit


def tmdl_ident(name):
    """Quote a name for use as a TMDL object identifier."""
    s = str(name)
    if _NEEDS_QUOTE.search(s) or s.lower() in RESERVED or not s:
        return "'" + s.replace("'", "''") + "'"
    return s


def dax_ref(table, column):
    """A DAX column/measure reference: 'Table'[Column].

    The table part always quotes -- unconditionally, not conditionally. A conditional quote is a
    latent bug: it works until the first table name with a space in it, and Tableau captions are
    full of spaces.
    """
    return "'" + str(table).replace("'", "''") + "'[" + str(column).replace("]", "]]") + "]"


def sanitize(name, fallback="Field"):
    """Make a Tableau caption safe as a Power BI identifier, without losing readability.

    Control characters and the TMDL-hostile bracket forms go; ordinary punctuation and spaces
    stay, because they are legal once quoted and stripping them makes every name uglier for no
    gain.
    """
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "")).strip()
    s = s.replace("[", "(").replace("]", ")")   # brackets terminate a DAX column ref
    s = re.sub(r"\s+", " ", s)
    # A name made only of quotes/whitespace is not a name. One corpus field is captioned `''`,
    # which produced `column ` -- syntactically a quoted two-apostrophe identifier, and
    # meaningless to anyone reading the model.
    if not s or not re.sub(r"[\'\"\s]", "", s):
        s = fallback
    if len(s) > MAX_NAME:
        # Keep a readable prefix plus a stable hash so two long names cannot collide.
        h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
        s = s[:MAX_NAME - 7].rstrip() + "~" + h
    return s


class NameAuthority:
    """Allocates every Power BI identifier. Deterministic and stable across runs.

    Stability matters: a re-run that renumbers a collision suffix would invalidate every
    visual.json that referenced the old name, silently breaking a report that previously worked.
    """

    def __init__(self):
        self.tables = {}        # tableau table key -> pbi table name
        self.columns = {}       # (pbi table, tableau field) -> pbi column name
        self.measures = {}      # (pbi table, tableau field) -> pbi measure name
        self._used_tables = set()
        self._used_in_table = {}    # pbi table -> set of member names (cols + measures share it)
        # MEASURE names are unique across the WHOLE MODEL, not per table -- columns are per table.
        # Desktop:
        #   Could not add Measure with the name 'CTD of Case Number' because a Measure with the
        #   same name already exists in the 'Model' Model.
        # Note "in the 'Model' Model": the scope is the model. Uniquifying measures per table let
        # 63 duplicates through across the corpus.
        self._used_measures = set()
        self.params = {}        # tableau param name -> {table, column, measure}
        self.bindings = []      # the Binding Ledger
        self.log = []
        # Allocation is not emission. A calculated field is allocated a name up front, then the
        # formula parser may decline it -- leaving an allocated name with no TMDL object behind it.
        # PBIR binds by name with no indirection, so binding to such a name is a dangling
        # reference. The semantic writer records what it ACTUALLY wrote here, and the report
        # resolver consults only this. Found by the pre-flight assertion on the first real run:
        # four visuals bound to a measure whose formula had been declined.
        self.emitted = set()    # (pbi table, pbi member name)
        # Tableau names a field two ways and BOTH appear in real files: an internal name
        # (`[Calculation_1750...]`) that formulas use, and a caption that shelves, drill paths and
        # group sources use. Allocation keys on the internal name because it is unique. So the
        # spelling->identity map has to live here, in the one object both builders consult -- when
        # each builder kept its own, rekeying dropped every shelf measure and 96% of the report's
        # field-well projections, in two separate places, for the same reason.
        self.aliases = {}       # (pbi table, spelling) -> field identity
        self._alias_order = []  # spellings in allocation order, for deterministic global fallback

    # -- internal ------------------------------------------------------------
    def _unique(self, base, used):
        """Deterministic collision resolution: Name, Name 2, Name 3, ...

        Checked CASE-INSENSITIVELY: TOM's own name collection ignores case (Windows/SQL Server
        convention), so 'BIC Compliance Check' and 'Bic Compliance Check' are the same name to
        it even though they are different Python strings. A real corpus workbook had exactly
        this pair -- a physical column and a calculated-field copy of it, differing only in
        case -- and `base not in used` let both through; Desktop refused to load the file at
        all: "Item '...' already exists in the collection."
        """
        folded = base.casefold()
        if not any(u.casefold() == folded for u in used):
            used.add(base)
            return base
        i = 2
        while any(u.casefold() == f"{base} {i}".casefold() for u in used):
            i += 1
        out = f"{base} {i}"
        used.add(out)
        self.log.append(f"collision: {base!r} -> {out!r}")
        return out

    def _members(self, table):
        return self._used_in_table.setdefault(table, set())

    # -- allocation ----------------------------------------------------------
    def table(self, key, caption):
        """Allocate a table name. `key` identifies the Tableau datasource uniquely."""
        if key in self.tables:
            return self.tables[key]
        # A Tableau caption is often "name (project | something)"; the leading clean segment is
        # the part a modeller recognises.
        base = sanitize(str(caption).split(" (")[0].strip() or caption, "Data")
        name = self._unique(base, self._used_tables)
        self.tables[key] = name
        return name

    def column(self, table, field, caption=None):
        """Allocate a column name. Columns and measures share one namespace per table, because
        TOM forbids a column and a measure with the same name in the same table.

        `field` is the IDENTITY and must be unique within the table -- Tableau's internal name.
        `caption` is only the display label the Power BI name is derived from. Keying on the
        caption instead collapses two distinct Tableau fields that happen to share a label into one
        entry, and both then get emitted: SMB IRF Program produced six duplicated members that way,
        which TOM rejects, so the model would not load.
        """
        k = (table, field)
        if k in self.columns:
            return self.columns[k]
        name = self._unique(sanitize(caption or field), self._members(table))
        self.columns[k] = name
        return name

    def measure(self, table, field, caption=None):
        """Allocate a measure name, unique MODEL-WIDE and also against this table's columns.

        Case-insensitive collision check for the same reason `_unique()` is -- see its
        docstring. This has its own inline check rather than calling `_unique()` because its
        `taken` set is a UNION of two others (model-wide measures + this table's columns) that
        `_unique()`'s single-`used`-set signature doesn't take.
        """
        k = (table, field)
        if k in self.measures:
            return self.measures[k]
        base = sanitize(caption or field)
        taken = self._used_measures | self._members(table)
        folded_taken = {t.casefold() for t in taken}
        name = base
        if name.casefold() in folded_taken:
            i = 2
            while f"{base} {i}".casefold() in folded_taken:
                i += 1
            name = f"{base} {i}"
            self.log.append(f"measure collision (model-wide): {base!r} -> {name!r}")
        self._used_measures.add(name)
        self._members(table).add(name)
        self.measures[k] = name
        return name

    def mark_emitted(self, table, name):
        """Called by the semantic writer for every object it actually writes to TMDL."""
        self.emitted.add((table, name))

    def alias(self, table, identity, *spellings):
        """Register every Tableau spelling that refers to one field identity."""
        for sp in spellings:
            if sp and (table, sp) not in self.aliases:
                self.aliases[(table, sp)] = identity
                self._alias_order.append((table, sp))

    def all_by_spelling(self, spelling, emitted_only=False):
        """All (pbi_table, pbi_name, kind) for every table that carries this spelling.

        `by_spelling` returns only the first match -- correct when the caller already has a
        specific table in mind, but wrong for a global allocation pass that must allocate for
        EVERY table that defines a column under the same caption, not just the one that won
        the allocation-order race.
        """
        seen = set()
        results = []
        for t, sp in self._alias_order:
            if sp != spelling or t in seen:
                continue
            name, kind = self.lookup(t, self.aliases[(t, sp)], emitted_only)
            if name:
                seen.add(t)
                results.append((t, name, kind))
        return results

    def by_spelling(self, spelling, table=None, emitted_only=False):
        """(pbi table, pbi name, kind) for a field named by caption OR internal name.

        Prefers the given table. The global fallback is for a shelf reference that omits its
        datasource, and it walks allocation order so the result is stable across runs -- but it is
        genuinely ambiguous when several near-duplicate datasources define the same field name,
        which is why the table-scoped path is tried first.
        """
        if table is not None:
            ident = self.aliases.get((table, spelling))
            if ident:
                name, kind = self.lookup(table, ident, emitted_only)
                if name:
                    return table, name, kind
        for t, sp in self._alias_order:
            if sp != spelling:
                continue
            name, kind = self.lookup(t, self.aliases[(t, sp)], emitted_only)
            if name:
                return t, name, kind
        return None, None, None

    def lookup(self, table, field, emitted_only=False):
        """Resolve a Tableau field name to (pbi_name, kind) within a table, or (None, None).

        This is the function every reference -- DAX and PBIR alike -- must go through. A field
        the authority does not know is an error to surface, never a name to pass through and hope.

        `emitted_only=True` additionally requires that the object was actually written to TMDL.
        The report layer must always pass it: an allocated-but-not-emitted name is exactly the
        dangling reference this class exists to prevent.
        """
        k = (table, field)
        for store, kind in ((self.measures, "measure"), (self.columns, "column")):
            if k in store:
                if emitted_only and (table, store[k]) not in self.emitted:
                    return None, None
                return store[k], kind
        return None, None

    def find_anywhere(self, field, emitted_only=False):
        """Resolve a field when the owning table is not known (Tableau shelf refs often omit it).

        Returns (table, name, kind) for the first match in allocation order. Deterministic, but
        genuinely ambiguous when several tables share a field name -- callers should prefer the
        table-scoped `lookup` and treat this as the fallback it is.
        """
        for store, kind in ((self.columns, "column"), (self.measures, "measure")):
            for (t, f), n in store.items():
                if f != field:
                    continue
                if emitted_only and (t, n) not in self.emitted:
                    continue
                return t, n, kind
        return None, None, None

    # -- the parameter convention -------------------------------------------
    def parameter(self, tableau_name):
        """ONE convention, recorded once, read by both builders.

        A Tableau parameter becomes:
            table    'Param <Caption>'
            column   Value
            measure  '<Caption> Selected'  = SELECTEDVALUE('Param <Caption>'[Value])

        Every `[Parameters].[X]` reference in every translated formula rewrites to that measure.
        """
        if tableau_name in self.params:
            return self.params[tableau_name]
        clean = sanitize(tableau_name, "Parameter")
        tname = self._unique(sanitize(f"Param {clean}"), self._used_tables)
        col = self.column(tname, "Value")
        meas = self.measure(tname, f"{clean} Selected")
        rec = {"table": tname, "column": col, "measure": meas, "tableau": tableau_name}
        self.params[tableau_name] = rec
        self.bind("parameter", tableau_name,
                  semantic=f"{tname}[{col}] + measure {meas}",
                  presentation=f"slicer bound to {tname}[{col}]",
                  invariant=f"every [Parameters].[{tableau_name}] reference resolves to {meas}")
        return rec

    # -- binding ledger ------------------------------------------------------
    def bind(self, kind, subject, semantic, presentation, invariant):
        """Record a Both-row pair whose two halves must agree, so S5 can VERIFY rather than hope."""
        self.bindings.append({"kind": kind, "subject": subject, "semantic": semantic,
                              "presentation": presentation, "invariant": invariant})

    def to_dict(self):
        return {
            "tables": {f"{k}": v for k, v in self.tables.items()},
            "columns": {f"{t} | {f}": v for (t, f), v in self.columns.items()},
            "measures": {f"{t} | {f}": v for (t, f), v in self.measures.items()},
            "parameters": self.params,
            "bindings": self.bindings,
            "log": self.log,
            # `columns`/`measures` above are ALLOCATION -- every calc gets a name before the
            # formula parser gets a chance to decline it. `emitted` is the actual write: what
            # reached TMDL. A downstream consumer asking "did this Tableau field make it into
            # Power BI" needs THIS, not the allocation maps -- see `lookup(emitted_only=True)`.
            "emitted": sorted(f"{t} | {n}" for t, n in self.emitted),
        }


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    na = NameAuthority()
    c1 = na.column("T", "[bic compliance check]", "Bic Compliance Check")
    c2 = na.column("T", "[Bic Compliance Check (copy)_1]", "BIC Compliance Check")
    assert c1 == "Bic Compliance Check", c1
    assert c2 == "BIC Compliance Check 2", (
        "case-insensitive collision not caught -- 'BIC Compliance Check' must be disambiguated "
        f"against 'Bic Compliance Check', got {c2!r}")

    na2 = NameAuthority()
    na2.measure("A", "[x]", "Total Sales")
    m = na2.measure("B", "[y]", "total sales")
    assert m == "total sales 2", (
        f"measure() must collide case-insensitively against another measure MODEL-WIDE, got {m!r}")
    c3 = na2.column("A", "[z]", "total sales")
    assert c3 == "total sales 2", (
        f"measure() must also collide case-insensitively against ITS OWN table's columns, got {c3!r}")

    t1 = na2.table("ds1", "Region Data")
    t2 = na2.table("ds2", "region data")
    assert t1 == "Region Data" and t2 == "region data 2", (t1, t2)

    print("names.py self-test OK (case-insensitive collision resolution for tables, "
          "columns, and measures)")
