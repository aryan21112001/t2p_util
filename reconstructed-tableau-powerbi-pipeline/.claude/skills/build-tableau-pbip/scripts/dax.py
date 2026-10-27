#!/usr/bin/env python3
"""
S3 Tier D+ -- Tableau formula -> DAX, by recursive descent. No model involved.

This tier exists because of a measured result: adding a deterministic conditional transpiler took
corpus DAX lint failures from 463 to 0. An LLM mistranslated the same conditionals by doing
shallow find-and-replace, which is exactly what a grammar should never be handed to a model.

The contract, and the whole reason this is safe:

    FAIL CLOSED. Anything not cleanly convertible raises Unsupported and goes to residue with a
    reason. It is never approximated, never partially emitted, never guessed.

A silently-wrong measure is far worse than a missing one: the model loads, the visual renders, and
a user reads a number that is quietly incorrect.
"""
import re


class Unsupported(Exception):
    """Raised when the parser declines. The message becomes the residue reason."""


# ---------------------------------------------------------------- lexer
TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
    # `/* ... */` BLOCK comments, not just `//` line comments -- real corpus regression
    # (10000056_achc_WF_Payments_Insights): `bps_amt rate V2`'s formula is an ordinary
    # IF/THEN/ELSE/END with a `/*then [Amt Rate V2]*10000*/` comment sitting between THEN and
    # the real expression -- ONLY the comment blocked it, the `*` inside `*/` fell through to
    # the `other` catch-all and declined the whole calc with "unexpected character '*'". `.`
    # spans newlines here (re.S below) because a real corpus comment (`total`'s formula) wraps a
    # multi-line `{ FIXED : ... }` block. Non-greedy (`.*?`) so a formula with TWO separate block
    # comments does not swallow the real code sitting between them as one giant "comment".
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
  | (?P<qualref>\[[^\]]*\]\s*\.\s*\[[^\]]*\])
  | (?P<field>\[[^\]]*\])
    # Tableau's DATE LITERAL: `#2022-07-01#`, optionally with a time part. Tableau's own
    # documented syntax for a hard-coded date, and unambiguous -- `#` has no other meaning in the
    # expression language, which is why the `other` catch-all below was rejecting the whole
    # formula with "unexpected character '#'". Matched BEFORE `num` so the digits inside are not
    # lexed as separate numbers. Real corpus instance (Prevent Dashboard, `Originated From Data
    # Center`): `[Lookup Date] >= #2022-07-01#` -- one unhandled literal declined an entire
    # otherwise-translatable calc.
  | (?P<datelit>\#[^\#]+\#)
  | (?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?)
  | (?P<op><=|>=|<>|!=|==|[+\-*/%^<>=()~,:{}])
  | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<other>.)
""", re.X | re.S)

KEYWORDS = {"IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "AND", "OR", "NOT", "IN"}

# Bare literals. Every NULL in the test corpus appears in `ELSE NULL` position -- a RETURNED
# value, never a comparison -- so BLANK() is exact. It would not be safe for `[x] = NULL`, since
# DAX evaluates `BLANK() = 0` as TRUE; such a comparison is declined rather than mistranslated
# (see cmp_expr).
LITERALS = {"NULL": "BLANK()", "TRUE": "TRUE()", "FALSE": "FALSE()"}
_QUALREF_RE = re.compile(r"\[([^\]]*)\]\s*\.\s*\[([^\]]*)\]")


class Tok:
    __slots__ = ("kind", "val")

    def __init__(self, kind, val):
        self.kind, self.val = kind, val

    def __repr__(self):
        return f"{self.kind}:{self.val!r}"


def lex(src):
    out = []
    for m in TOKEN_RE.finditer(src or ""):
        k = m.lastgroup
        v = m.group()
        if k in ("ws", "comment"):
            continue
        if k == "other":
            raise Unsupported(f"unexpected character {v!r}")
        if k == "word" and v.upper() in KEYWORDS:
            out.append(Tok(v.upper(), v))
        else:
            out.append(Tok(k, v))
    return out


# ---------------------------------------------------------------- function map
# Tableau -> DAX where the mapping is exact and arity-preserving.
FUNCS = {
    "SUM": "SUM", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT", "ABS": "ABS",
    "AVG": "AVERAGE", "COUNTD": "DISTINCTCOUNT", "STDEV": "STDEV.S", "VAR": "VAR.S",
    "MEDIAN": "MEDIAN", "ROUND": "ROUND", "FLOOR": "FLOOR", "CEILING": "CEILING",
    "LOWER": "LOWER", "UPPER": "UPPER", "TRIM": "TRIM", "LEN": "LEN",
    "SQRT": "SQRT", "EXP": "EXP", "LOG": "LOG", "SIGN": "SIGN",
    "YEAR": "YEAR", "MONTH": "MONTH", "DAY": "DAY", "HOUR": "HOUR", "MINUTE": "MINUTE",
    "SECOND": "SECOND", "NOW": "NOW", "TODAY": "TODAY",
    "ISNULL": "ISBLANK", "IFNULL": "COALESCE", "IIF": "IF",
    "CONTAINS": "CONTAINSSTRING", "REPLACE": "SUBSTITUTE",
    # Identical name AND identical signature in both languages. Their absence was not a real
    # incompatibility -- 6 corpus calcs were declined as "no DAX equivalent for LEFT()" when DAX
    # has had LEFT(text, num) all along.
    "LEFT": "LEFT", "RIGHT": "RIGHT", "POWER": "POWER", "INT": "INT",
    # Tableau ASCII(s) returns the code point of the first character; DAX's UNICODE(s) is defined
    # identically (and for the ASCII range the two code spaces are the same numbers by
    # construction, since Unicode's first 128 code points ARE ASCII). CHAR() is the inverse pair
    # and is spelled the same in both languages.
    # DAX has no function literally named CHAR -- only UNICHAR(number), which is CHAR's exact
    # semantic equivalent (Unicode code point -> character). Mapping to "CHAR" verbatim produced
    # DAX Desktop rejects at load with "Failed to resolve name 'CHAR'. It is not a valid table,
    # variable, or function name" -- real corpus regression, GCLM_EU_GERMANY_COMMERZBANK_AG_v2's
    # `Seg_spnd_f6_concat`/`Seg_spnd_l6_concat` (`[a] + CHAR(10) + [b]`, a newline-joined label).
    "ASCII": "UNICODE", "CHAR": "UNICHAR",
    # Tableau MAKEDATE(year, month, day) is DAX DATE(year, month, day), verbatim -- same argument
    # order, same day/month-rollover semantics in both languages.
    "MAKEDATE": "DATE",
    "SPLIT": None,  # present so it is recognised, then explicitly declined below
}

# Tableau makes the second argument optional; DAX requires it. Supplying DAX's implied default is
# exact, and NOT supplying it emits DAX that fails at model load -- which is loud, but still a
# broken build. The default per function is the value that reproduces Tableau's 1-argument
# behaviour.
OPTIONAL_SECOND_ARG = {"FLOOR": "1", "CEILING": "1", "ROUND": "0", "LOG": "10"}

# DAX aggregates take a COLUMN REFERENCE, not an expression. Tableau's take either. So
# `COUNTD(IF(...))` -- perfectly ordinary Tableau -- becomes `DISTINCTCOUNT(IF(...))`, which is
# invalid DAX and fails at model load. The iterator variants are the correct target: they take a
# table to iterate plus a row-level expression, which is exactly Tableau's semantics.
AGG_ITERATOR = {"SUM": "SUMX", "AVERAGE": "AVERAGEX", "MIN": "MINX", "MAX": "MAXX",
                "MEDIAN": "MEDIANX", "STDEV.S": "STDEVX.S", "VAR.S": "VARX.S",
                # Tableau COUNT counts non-null values of ANY type, which is COUNTAX, not COUNTX
                # (COUNTX counts numbers only).
                "COUNT": "COUNTAX"}

# DISTINCTCOUNT has no iterator variant. Counting distinct values of an EXPRESSION needs
# COUNTROWS(DISTINCT(SELECTCOLUMNS(...))), which is a restructure, not a substitution.
NO_ITERATOR = {"DISTINCTCOUNT"}

# DATENAME('unit', date) -> a STRING. Where a VBA format token is unambiguous the token is used;
# where it is not, the numeric part is extracted first and formatted. `dd` would zero-pad ("05")
# where Tableau returns "5", and VBA's `h` is 12-hour, so those go through DAY()/HOUR() instead.
DATENAME_DAX = {
    "month":   lambda d: f'FORMAT({d}, "mmmm")',
    "weekday": lambda d: f'FORMAT({d}, "dddd")',
    "year":    lambda d: f'FORMAT(YEAR({d}), "0")',
    "day":     lambda d: f'FORMAT(DAY({d}), "0")',
    "quarter": lambda d: f'FORMAT(ROUNDUP(MONTH({d}) / 3, 0), "0")',
    "hour":    lambda d: f'FORMAT(HOUR({d}), "0")',
    "minute":  lambda d: f'FORMAT(MINUTE({d}), "0")',
    "second":  lambda d: f'FORMAT(SECOND({d}), "0")',
}

# Tableau's own spelling for a start-of-week override -> 0-based offset from Monday
# (Monday=0 ... Sunday=6). Tableau records the workbook/datasource's actual setting in the
# source XML at <datasource>/<connection>/<overridable-settings>/<date-options start-of-week=.../>
# -- extract.py surfaces it per table, defaulting to Tableau's own product default ("sunday")
# when a workbook never overrides it. Previously DATEPART('weekday',...)/DATETRUNC('week',...)
# always declined here on the theory that this setting was unrecoverable; it is not -- it was
# simply never read. Ground truth: Prevent Dashboard's "Rolling 7 Day Start/End" family, ~9
# calcs (plus everything downstream of them) declined for exactly this reason despite the
# workbook's own XML stating start-of-week='sunday' outright.
WEEKDAY_START_OFFSET = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _tableau_weekday_expr(date_expr, start_of_week):
    """DAX for Tableau's DATEPART('weekday', date) under an arbitrary start-of-week.

    DAX's native WEEKDAY(date, 2) numbers Monday=1..Sunday=7 -- a fixed, unconfigurable start.
    Shifting that to any OTHER start day is a pure modular-arithmetic rotation: subtract the new
    start day's own Monday-offset, wrap around the 7-day cycle with MOD, then re-add 1 to land
    back on Tableau's 1-based numbering (Tableau's start day is always weekday 1). Verified
    against Python's real `datetime.date.weekday()` for all 7 possible start days in this file's
    self-test below, not hand-derived.
    """
    offset = WEEKDAY_START_OFFSET[start_of_week]
    return f"MOD(WEEKDAY({date_expr}, 2) - 1 - {offset}, 7) + 1"

_BARE_COLUMN = re.compile(r"^'(?:[^']|'')*'\[[^\]]*\]$")
_TABLE_IN_REF = re.compile(r"'((?:[^']|'')*)'\[")
_NUMERIC_LITERAL_RE = re.compile(r"^-?\d+(\.\d+)?$")
# A bare `[Name]` reference with NO table prefix at all -- the exact, distinctive shape
# `resolve_param` (tmdl.py's `_resolver`) always emits for a resolved Tableau parameter, pointed
# at that parameter's own SELECTEDVALUE measure (see this translator's own comment on why: a
# calculated COLUMN may never reference a measure, so the bare `[Name]` form, never `'Table'[Name]`,
# is the one shape that proves "this is a parameter-measure reference" rather than a real column.
_BARE_MEASURE_REF = re.compile(r"^\[[^\[\]]+\]$")
# A bare column reference wrapped in one or more layers of matching parens -- see func_call's own
# comment on why this needs unwrapping before any `_BARE_COLUMN` check.
_PARENWRAPPED_BARE_COLUMN = re.compile(r"^(\(+)('(?:[^']|'')*'\[[^\]]*\])(\)+)$")


def _split_nth_token(s_expr, delim_expr, n):
    """DAX text for Tableau's SPLIT(s, delim, n) with a literal POSITIVE n -- there is no single
    DAX function for this, so it is built from FIND/MID/LEFT/LEN, unrolled at translation time
    since n is known then. `s & delim` (appending the delimiter before every FIND) guarantees a
    match exists even on the LAST token (which has no trailing delimiter of its own) or on a
    string with no delimiter at all (Tableau's own SPLIT returns the whole string for token 1 in
    that case) -- both fall out of this construction for free, not as a special case.

    Strips the first (n-1) tokens by repeatedly finding-and-discarding "everything up to and
    including the next delimiter", then LEFTs off everything before what is now the first
    delimiter -- which is exactly the n-th token. 999999999 is not a guess at a real length, it
    is DAX's own well-known idiom for "as many characters as could possibly remain" -- MID/LEFT
    never error or truncate early when asked for more characters than actually exist.
    """
    cur = s_expr
    for _ in range(n - 1):
        cur = (f"MID({cur} & {delim_expr}, FIND({delim_expr}, {cur} & {delim_expr}) + "
               f"LEN({delim_expr}), 999999999)")
    return f"LEFT({cur} & {delim_expr}, FIND({delim_expr}, {cur} & {delim_expr}) - 1)"


def _is_exact_call(dax_text, func_name):
    """Is `dax_text`, as a WHOLE string, exactly one `func_name(...)` call -- not that call plus
    something else (`SUM(x) + SUM(y)`) and not a DIFFERENTLY-named function that merely starts
    with the same letters (`SUMX(...)` must not match `func_name="SUM"`). Used by the promoted-
    aggregate collapse below to tell "this inner calc's own translation IS just SUM(...), so
    re-wrapping it in another SUM is a no-op" apart from every shape that is NOT that."""
    s = dax_text.strip()
    prefix = func_name + "("
    if not s.startswith(prefix) or not s.endswith(")"):
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i == len(s) - 1
    return False

# Functions this translator already PROVES return a date, by construction of their own
# handlers below (DATE/DATEADD/DATETRUNC/DATEPARSE always produce a date or decline outright --
# never a number or string). Used by STR()'s type-check to trust a computed argument as
# date-typed from the Tableau SOURCE shape itself, not from `last_field`'s (unreliable, see
# STR()'s own comment) inferred type.
_DATE_RETURNING_FUNCS = {"DATE", "DATEADD", "DATETRUNC", "DATEPARSE"}

# Functions this translator already PROVES return a plain INTEGER, by construction of their own
# handlers below -- YEAR/MONTH/DAY always return a calendar component (never a date), DATEDIFF
# always returns a day/unit COUNT, and DATEPART always returns an integer regardless of which
# unit it is given (even 'weekday'/'week'/'dayofyear', which DATEPART's own direct-translation
# rule still declines for OTHER reasons -- the return TYPE is integer in every case, only the
# VALUE's meaning is ambiguous for those units). Used by STR()'s type-check the same way
# `_DATE_RETURNING_FUNCS` is: an integer wrapped in STR() has zero locale/date-format ambiguity
# (unlike STR(<date>)), so it is strictly SAFER to translate than the date-returning case, not
# harder -- ground truth (Prevent Dashboard) `Date Range Text` = `STR(DATEPART('day', d))`.
_INTEGER_RETURNING_FUNCS = {"YEAR", "MONTH", "DAY", "DATEDIFF", "DATEPART"}


def _top_level_has_concat(s):
    """True if DAX's `&` (text concatenation) appears at PAREN DEPTH 0 in `s` -- i.e. `s` AS A
    WHOLE is a text-producing expression, not a call/expression that merely contains a nested
    text operation somewhere inside one of its OWN arguments. Depth-aware on purpose: a
    same-formula nested `DATE(<param> & "-01")` call resolves its own `&` at depth 0 relative to
    ITS OWN argument string before the outer call ever sees it, so by the time an OUTER
    `DATE(<expr>)` call inspects its own (already-assembled) argument, a `&` still present is
    always several parens deep, not at depth 0 -- exactly the signal that tells "this whole
    argument is textual" apart from "some unrelated branch buried inside this argument happens to
    concatenate text". A naive substring search cannot tell those two apart.
    """
    depth = 0
    in_str = False
    for c in s:
        if in_str:
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == '&' and depth == 0:
            return True
    return False


# Functions with a real DAX equivalent but a DIFFERENT signature, so a name swap would silently
# change the result. Declined by name rather than mistranslated.
ARITY_MISMATCH = {
    "STR": "Tableau STR(x) vs DAX FORMAT(x, format) — DAX requires an explicit format string",
    "SPLIT": None,  # handled specially: a literal positive token index unrolls into FIND/MID/LEFT
    "MID": None,  # handled specially: 3-arg is a straight pass-through, 2-arg means "to the end"
    "FIND": "Tableau FIND(s,sub[,start]) vs DAX FIND(sub,s[,start[,notfound]]) — arguments swap",
    "STARTSWITH": "Tableau STARTSWITH(s,sub) has no DAX equivalent — it needs rewriting as "
                  "LEFT(s, LEN(sub)) = sub, which is a restructure and not a name swap",
    "ENDSWITH": "Tableau ENDSWITH(s,sub) has no DAX equivalent — needs RIGHT(s, LEN(sub)) = sub",
    "LTRIM": "Tableau LTRIM removes LEADING whitespace only; DAX TRIM removes both ends, so the "
             "swap silently changes the result when there is trailing whitespace",
    "RTRIM": "Tableau RTRIM removes TRAILING whitespace only; DAX TRIM removes both ends",
    "ZN": None,  # handled specially: ZN(x) -> COALESCE(x, 0)
}


class Parser:
    """Recursive descent over the Tableau expression grammar.

    Precedence, loosest first: OR, AND, NOT, comparison, additive, multiplicative, unary, atom.
    """

    def __init__(self, toks, resolve, resolve_param=None, resolve_type=None, resolve_kind=None,
                 own_type=None, start_of_week=None, resolve_formula=None, _collapse_seen=None,
                 tc_partition=None):
        self.t = toks
        self.i = 0
        self.resolve = resolve                  # (tableau_field_name) -> DAX ref, or None
        self.resolve_param = resolve_param      # (tableau_param_name) -> DAX ref, or None
        # The already-resolved DAX column reference (e.g. "'T'[Topic Area]") for THIS calc's own
        # extracted "Restart every: <field>" table-calc partition, or None. Only RANK/RANK_DENSE
        # use it (see func_call) -- every other table-calc function still declines exactly as
        # before regardless of this value, since only those two map onto RANKX's own native
        # `ties` argument without approximating Tableau's real tie-break rule (see route()'s own
        # docstring in tmdl.py for why RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE do not qualify).
        self.tc_partition = tc_partition
        # Tableau's OWN declared/inferred datatype for the CALC CURRENTLY BEING TRANSLATED as a
        # whole (e.g. "date") -- not a per-field lookup like resolve_type, a single fact about
        # this one formula's overall result, computed by Tableau's own type-checker at authoring
        # time. Only DATE(<expr>)'s no-op-cast branch uses it (see func_call), and only once every
        # cheaper, more-local signal (a bare field's own type, a literal, a provably-textual
        # top-level shape) has already been ruled out -- see that branch's own comment for why
        # trusting it there is a real fact relayed from Tableau, not a guess invented here.
        self.own_type = own_type
        # (tableau_field_name) -> TMDL dataType, or None. Only STR() needs it, and it needs it to
        # stay honest: `x & ""` coerces any value to text, but for a DATE Tableau emits an ISO
        # string while DAX emits a locale-formatted one. Without the type there is no way to tell
        # those apart, so a date argument is declined instead of quietly differing.
        self.resolve_type = resolve_type
        # (tableau_field_name) -> "column" | "measure" | None. Only the aggregate-over-a-bare-
        # reference branch of func_call needs this, and it needs it for exactly one reason: a
        # resolved reference is always rendered as the SAME 'Table'[Name] string shape whether the
        # name is a physical column or a promoted measure (see tmdl.py's `_aggregate_map`), so no
        # amount of looking at the STRING can tell the two apart -- only the resolver, which is the
        # one place that actually knows, can. Without this hook, SUM() of a promoted measure looks
        # exactly like SUM() of an ordinary column at parse time and is emitted verbatim, which is
        # invalid DAX (SUM/aggregate functions require a column argument) and fails at Desktop load
        # with Missing_References -- confirmed on a real workbook (Caribbean - Drilldown (QBR),
        # `Gross Trx Amt Decline Rate` = DIVIDE(SUM([Gross Trx Amt Declined]), ...) where
        # `Gross Trx Amt Declined` has no aggregate of its own and was promoted to a measure by
        # `_aggregate_map`'s fixpoint).
        self.resolve_kind = resolve_kind
        # (tableau_field_name) -> that field's OWN raw Tableau formula text, or None. Only the
        # promoted-aggregate collapse below (see `_collapse_promoted_aggregate`) needs it: to
        # re-derive `SUM([x])` where `x` resolved to a measure, the parser needs `x`'s own source
        # formula, not just its already-resolved DAX reference string.
        self.resolve_formula = resolve_formula
        # Field identities already being substituted in the CURRENT collapse chain, so a genuine
        # cross-table reference cycle (not caught by tmdl.py's own same-table `_find_cycles`, since
        # that check is per-table and this substitution walks the GLOBAL field index) unwinds as a
        # decline instead of recursing forever.
        self._collapse_seen = frozenset(_collapse_seen or ())
        # Tableau's own start-of-week setting for the DATASOURCE this calc belongs to (a day
        # name, e.g. "sunday"), read from <date-options start-of-week=.../> in the source XML by
        # extract.py -- see WEEKDAY_START_OFFSET above. `None` here means the caller did not
        # thread one through (e.g. an old/self-test call site); Tableau's own product default
        # ("sunday") is used in that case, not a guess -- it is what Tableau itself assumes when
        # a workbook never overrides the setting.
        self.start_of_week = start_of_week
        self.last_field = None
        self.last_kind = None
        self.refs = set()
        self.param_refs = set()

    def _week_start_day(self, explicit_arg=None):
        """The day name to use for THIS DATEPART('weekday',...)/DATETRUNC('week',...) call --
        the calc's OWN explicit 3rd-argument literal if it gave one, else the datasource's
        threaded default. Raises Unsupported if a day name cannot be determined."""
        if explicit_arg is not None:
            sow = explicit_arg.strip().strip('"').strip("'").lower()
        else:
            sow = (self.start_of_week or "sunday").strip().lower()
        if sow not in WEEKDAY_START_OFFSET:
            raise Unsupported(
                f"start-of-week {sow!r} is not a recognized day name -- Tableau's own week "
                f"numbering for this calc cannot be determined")
        return sow

    def _is_call_to_one_of(self, start_i, end_i, func_set):
        """True if the RAW TABLEAU TOKENS from `start_i` to `end_i` (an argument's own span, as
        recorded by `func_call`'s `arg_spans`) are, IN THEIR ENTIRETY, a single top-level call to
        one of `func_set` -- i.e. `NAME ( ... )` with the opening paren's OWN matching close as
        the very last token, and nothing before or after it. Checked against the ORIGINAL
        Tableau source tokens, not the translated DAX text, because e.g. DATEADD and DATETRUNC
        no longer literally contain those words once translated (DATEADD('month', 1, x) becomes
        EDATE(x, 1)) -- there is no reliable text signal left in the output to detect this from.
        Depth-aware on purpose: `DATE(x) + 1` also STARTS with `DATE(`, but its matching close is
        not the last token, so this correctly returns False for it -- the whole argument is not
        itself a call to that function, addition changed the type.
        """
        toks = self.t[start_i:end_i]
        if len(toks) < 3:
            return False
        if toks[0].kind != "word" or toks[0].val.upper() not in func_set:
            return False
        if not (toks[1].kind == "op" and toks[1].val == "("):
            return False
        if not (toks[-1].kind == "op" and toks[-1].val == ")"):
            return False
        depth = 0
        for idx, tok in enumerate(toks[1:], start=1):
            if tok.kind == "op" and tok.val == "(":
                depth += 1
            elif tok.kind == "op" and tok.val == ")":
                depth -= 1
                if depth == 0:
                    return idx == len(toks) - 1
        return False

    def _is_date_returning_call(self, start_i, end_i):
        """True if `start_i:end_i` is, in its entirety, a top-level call to one of
        `_DATE_RETURNING_FUNCS` (DATE/DATEADD/DATETRUNC/DATEPARSE -- functions this translator
        already PROVES return a date, by construction of their own handlers, which always
        produce a date or decline outright, never a number or string) -- OR a single-argument
        MIN/MAX call whose own argument is (recursively) one of these, since Tableau's MIN/MAX
        of ONE expression is a type-preserving aggregate (MIN of a date-typed expression is
        still a date). Used by STR()'s type-check to trust a computed argument as date-typed
        from the Tableau SOURCE shape itself, not from `last_field`'s (unreliable, see STR()'s
        own comment) inferred type; also used by DATE()'s own redundant-cast check below.

        Real corpus (GCLM): `DATE(MIN(DATEPARSE("yyyyMM", [mnth_yr])))` -- `DATEPARSE(...)` is
        already proven date-returning, `MIN(<that>)` preserves it, so the outer `DATE(...)` is a
        redundant cast with no bare field/literal/own_type signal to trust otherwise, declined
        before this fix even though every piece of information needed to prove it is right here
        in the Tableau source shape.

        Deliberately NOT extended to the 2-argument Tableau `MIN(a, b)`/`MAX(a, b)` form ("smaller
        of two values", a different operation from the single-expression aggregate) -- the
        no-top-level-comma check below excludes it rather than guessing either argument's type.
        """
        if self._is_call_to_one_of(start_i, end_i, _DATE_RETURNING_FUNCS):
            return True
        toks = self.t[start_i:end_i]
        if (len(toks) >= 3 and toks[0].kind == "word" and toks[0].val.upper() in ("MIN", "MAX")
                and toks[1].kind == "op" and toks[1].val == "("
                and toks[-1].kind == "op" and toks[-1].val == ")"):
            inner_start, inner_end = start_i + 2, end_i - 1
            depth = 0
            for idx in range(inner_start, inner_end):
                tok = self.t[idx]
                if tok.kind == "op" and tok.val == "(":
                    depth += 1
                elif tok.kind == "op" and tok.val == ")":
                    depth -= 1
                elif tok.kind == "op" and tok.val == "," and depth == 0:
                    return False  # the 2-argument "smaller of two values" shape, not an aggregate
            return self._is_date_returning_call(inner_start, inner_end)
        return False

    def _is_integer_returning_call(self, start_i, end_i):
        """True if `start_i:end_i` is, in its entirety, a top-level call to one of
        `_INTEGER_RETURNING_FUNCS` (YEAR/MONTH/DAY/DATEDIFF/DATEPART -- functions this
        translator already PROVES return a plain integer). Used by STR()'s type-check the same
        way `_is_date_returning_call` is, to safely translate `STR(<integer-returning call>)`
        as plain numeric-to-text instead of declining it as if it might be a hidden date."""
        return self._is_call_to_one_of(start_i, end_i, _INTEGER_RETURNING_FUNCS)

    # -- token helpers -------------------------------------------------------
    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def next(self):
        tok = self.peek()
        if tok is None:
            raise Unsupported("unexpected end of expression")
        self.i += 1
        return tok

    def accept(self, kind, val=None):
        tok = self.peek()
        if tok and tok.kind == kind and (val is None or tok.val.upper() == val):
            self.i += 1
            return tok
        return None

    def expect(self, kind, val=None):
        tok = self.accept(kind, val)
        if not tok:
            got = self.peek()
            raise Unsupported(f"expected {val or kind}, got {got.val if got else 'end'!r}")
        return tok

    # -- grammar -------------------------------------------------------------
    def parse(self):
        e = self.or_expr()
        if self.peek() is not None:
            raise Unsupported(f"trailing tokens from {self.peek().val!r}")
        return e

    def or_expr(self):
        left = self.and_expr()
        while self.accept("OR"):
            left = f"({left} || {self.and_expr()})"
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept("AND"):
            left = f"({left} && {self.not_expr()})"
        return left

    def not_expr(self):
        if self.accept("NOT"):
            return f"NOT({self.not_expr()})"
        return self.cmp_expr()

    def cmp_expr(self):
        left = self.add_expr()
        while True:
            tok = self.peek()
            if tok and tok.kind == "op" and tok.val in ("=", "==", "<>", "!=", "<", ">", "<=", ">="):
                self.next()
                op = {"==": "=", "!=": "<>"}.get(tok.val, tok.val)
                right = self.add_expr()
                # DAX evaluates `BLANK() = 0` as TRUE, so a null COMPARISON cannot be translated
                # by mapping NULL to BLANK(). ISBLANK() is the correct form, but rewriting the
                # comparison into it changes the expression shape, so decline and let a tier
                # that can reason about it handle it.
                if "BLANK()" in (left, right):
                    raise Unsupported(
                        "comparison against NULL — DAX treats BLANK() = 0 as TRUE, so this needs "
                        "ISBLANK(), not a literal substitution")
                left = f"{left} {op} {right}"
            elif tok and tok.kind == "IN":
                self.next()
                self.expect("op", "(")
                items = [self.or_expr()]
                while self.accept("op", ","):
                    items.append(self.or_expr())
                self.expect("op", ")")
                left = f"{left} IN {{{', '.join(items)}}}"
            else:
                return left

    _STRING_LITERAL = re.compile(r'^"(?:[^"]|"")*"$')

    def _is_provably_text(self, translated, field):
        """True if `translated` (an already-translated DAX operand) is PROVABLY text-typed:
        either a string literal, or a bare column reference whose Tableau-declared datatype
        (via `resolve_type`) is literally "string". The second case is a PROOF, not a guess --
        the same `_BARE_COLUMN` + `resolve_type` technique STR()/DATE() already use elsewhere in
        this file to trust a field's real declared type instead of inferring it. `field` is
        `self.last_field` captured IMMEDIATELY after the operand was parsed (that slot is a
        single mutable "most recently resolved bracketed token" value, overwritten by whatever
        parses next, so the caller must snapshot it right away, not read `self.last_field` late).
        """
        if self._STRING_LITERAL.match(translated):
            return True
        return bool(self.resolve_type and field and _BARE_COLUMN.match(translated.strip())
                    and self.resolve_type(field) == "string")

    def add_expr(self):
        """Additive level. Tableau overloads `+` for BOTH numeric addition and string
        concatenation; DAX does not -- `+` is always arithmetic, `&` is concatenation. Using `+`
        on text operands does not fail to translate, it fails at QUERY TIME: DAX tries to coerce
        the text to a number for the addition and raises "Cannot convert value '' of type Text to
        type Numeric/Date" the moment the visual actually evaluates it, which is invisible to
        preflight (a syntactically valid DAX string) and to Desktop's own load-time validation.
        Confirmed on a real workbook (Prevent Dashboard): `COALESCE(a, "") + "|" + COALESCE(b, "")`
        -- a label-building concatenation, unmistakably textual because of the `"|"` separator.

        Two INDEPENDENT signals prove "is this text", both checked by `_is_provably_text`: (1) a
        literal string operand (`"|"`) -- unambiguous by itself; (2) a bare column reference
        whose Tableau-declared datatype IS "string" -- also unambiguous, since Tableau itself
        already decided this field is text, not a translation-time guess. Real corpus regression
        (GCLM_EU_GERMANY_COMMERZBANK_AG_v2's `Seg_spnd_f6_concat` = `[first6months] + CHAR(10) +
        [first_6_month_cutoff]`, both operands plain string columns, no literal anywhere in the
        chain) previously fell through to plain `+`: DAX loaded fine (syntactically valid) and
        then errored at query time trying to add two text values. Once one side of a `+`-chain is
        textual (by EITHER signal), DAX cannot silently go back to numeric `+` for a later `+` in
        the same left-associative chain (mixing `&` and `+` reproduces the same error one
        operator later), so textual-ness still propagates through `is_text` for the rest of the
        chain, exactly as before. A chain where NEITHER operand is a literal NOR a bare
        string-typed column (e.g. a general expression whose type this parser cannot prove) is
        still NOT caught -- guessing wrong there would silently flip a WORKING numeric `+` to
        text-only `&`, which stays exactly as conservative as it was.
        """
        left = self.mul_expr()
        left_field = self.last_field
        is_text = self._is_provably_text(left, left_field)
        while True:
            tok = self.peek()
            if tok and tok.kind == "op" and tok.val in ("+", "-"):
                self.next()
                op = tok.val
                right = self.mul_expr()
                right_field = self.last_field
                if op == "+" and (is_text or self._is_provably_text(right, right_field)):
                    left = f"{left} & {right}"
                    is_text = True
                else:
                    left = f"{left} {op} {right}"
            else:
                return left

    def mul_expr(self):
        left = self.unary()
        while True:
            tok = self.peek()
            if tok and tok.kind == "op" and tok.val in ("*", "/", "%", "^"):
                self.next()
                op = tok.val
                right = self.unary()
                if op == "/":
                    # Tableau returns null on divide-by-zero; DAX `/` returns infinity/error.
                    # DIVIDE is the equivalent, not a stylistic preference.
                    left = f"DIVIDE({left}, {right})"
                elif op == "%":
                    left = f"MOD({left}, {right})"
                elif op == "^":
                    left = f"POWER({left}, {right})"
                else:
                    left = f"{left} * {right}"
            else:
                return left

    def unary(self):
        tok = self.peek()
        if tok and tok.kind == "op" and tok.val in ("+", "-"):
            self.next()
            return f"{tok.val}{self.unary()}"
        return self.atom()

    def atom(self):
        tok = self.next()

        if tok.kind == "op" and tok.val == "(":
            e = self.or_expr()
            self.expect("op", ")")
            return f"({e})"

        if tok.kind == "num":
            return tok.val

        if tok.kind == "datelit":
            # `#2022-07-01#` / `#2022-07-01 13:45:00#` -> an explicit DAX DATE()/DATETIME(), never
            # a bare quoted string: DAX's text-to-date coercion is locale-dependent, so
            # "2022-07-01" could read as 1 July or 7 January depending on the model's locale,
            # which is exactly the silent-wrongness this pipeline refuses elsewhere (see STR()).
            # Anything that is not an unambiguous ISO date/datetime is DECLINED rather than
            # guessed at -- Tableau also permits `#2022-07-01 13:45#` and localised spellings.
            body = tok.val[1:-1].strip()
            m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})"
                         r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$", body)
            if not m:
                raise Unsupported(
                    f"Tableau date literal #{body}# is not an unambiguous ISO date -- a locale-"
                    f"dependent text coercion would silently read the day and month the wrong "
                    f"way round, so it is declined rather than guessed at")
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if m.group(4) is None:
                return f"DATE({y}, {mo}, {d})"
            hh, mi = int(m.group(4)), int(m.group(5))
            ss = int(m.group(6) or 0)
            return f"(DATE({y}, {mo}, {d}) + TIME({hh}, {mi}, {ss}))"

        if tok.kind == "str":
            # Tableau uses single OR double quotes; DAX requires double. Escape inner doubles.
            body = tok.val[1:-1]
            if tok.val[0] == "'":
                body = body.replace("''", "'")
            else:
                body = body.replace('""', '"')
            return '"' + body.replace('"', '""') + '"'

        if tok.kind == "field":
            inner = tok.val[1:-1]
            if inner.upper() in LITERALS:      # a bracketed literal, e.g. [NULL]
                return LITERALS[inner.upper()]
            ref = self.resolve(inner)
            if ref is None:
                raise Unsupported(f"unresolved field reference [{inner}]")
            self.refs.add(inner)
            self.last_field = inner
            self.last_kind = self.resolve_kind(inner) if self.resolve_kind else None
            return ref

        if tok.kind == "op" and tok.val == "{":
            return self.lod_expr()

        if tok.kind == "qualref":
            # `[Owner].[Field]`. When Owner is `Parameters` this is the row-16 case: it must
            # resolve to the SELECTEDVALUE measure the Name Authority allocated, or every visual
            # bound to that parameter dangles. Any other owner is a plain field reference.
            m = _QUALREF_RE.match(tok.val)
            owner, inner = m.group(1), m.group(2)
            if owner == "Parameters":
                ref = self.resolve_param(inner) if self.resolve_param else None
                if ref is None:
                    raise Unsupported(f"unresolved parameter reference [Parameters].[{inner}]")
                self.param_refs.add(inner)
                return ref
            ref = self.resolve(inner)
            if ref is None:
                raise Unsupported(f"unresolved field reference [{owner}].[{inner}]")
            self.refs.add(inner)
            self.last_field = inner
            self.last_kind = self.resolve_kind(inner) if self.resolve_kind else None
            return ref

        if tok.kind == "IF":
            return self.if_expr()

        if tok.kind == "CASE":
            return self.case_expr()

        if tok.kind == "word":
            up = tok.val.upper()
            if up in LITERALS:
                # A literal, not a call -- and it must NOT be followed by '(', or it is a
                # function whose name happens to collide.
                nxt = self.peek()
                if not (nxt and nxt.kind == "op" and nxt.val == "("):
                    return LITERALS[up]
            return self.func_call(tok.val)

        raise Unsupported(f"unexpected token {tok.val!r}")

    def lod_expr(self):
        """`{FIXED [a],[b] : SUM([x])}` -> CALCULATE(<agg>, ALLEXCEPT(T, T[a], T[b])).

        FIXED only. INCLUDE and EXCLUDE are relative to the view's own dimensions, which a semantic
        model does not have at build time, so they stay with the LLM tier rather than being
        approximated.

        A FIXED LOD pins an aggregate to a stated grain regardless of what the visual groups by,
        and ALLEXCEPT is exactly that: clear every filter on the table except the listed columns.
        With no keys at all it degenerates to a grand total, which is ALL.

        A bare `{expr}` -- no FIXED/INCLUDE/EXCLUDE keyword at all -- is Tableau's OTHER legal use
        of braces: scope `expr` to the whole table, ignoring the view's filters, with no grouping
        key. This is exactly the FIXED-with-zero-keys case below, just spelled without the
        keyword. Confirmed on a real corpus workbook (Digital Sales - Leads Dashboard):
        `{[End Date Calc]}` and `{DATE(IF DATETRUNC('day', max(...))=... THEN ... END)}`, both a
        scalar/aggregate pinned to table scope, neither a genuine grouping-key LOD.
        """
        kind = self.peek()
        if not (kind and kind.kind == "word" and kind.val.upper() in ("FIXED", "INCLUDE",
                                                                     "EXCLUDE")):
            inner_expr = self.or_expr()
            if not (self.peek() and self.peek().kind == "op" and self.peek().val == "}"):
                raise Unsupported("'{' that is not a level-of-detail expression")
            self.next()
            tables = {m.group(1) for m in re.finditer(r"'((?:[^']|'')*)'\[", inner_expr)}
            if len(tables) != 1:
                raise Unsupported("bare '{...}' (no FIXED/INCLUDE/EXCLUDE) needs exactly one "
                                  f"table to clear filters on; found {len(tables)}")
            return f"CALCULATE({inner_expr}, ALL('{tables.pop()}'))"
        self.next()
        k = kind.val.upper()
        if k != "FIXED":
            raise Unsupported(f"{k} level-of-detail expression is relative to the view's own "
                              f"dimensions, which the model does not have at build time — Tier L1")
        keys = []
        while not (self.peek() and self.peek().kind == "op" and self.peek().val == ":"):
            tok = self.next()
            if tok is None:
                raise Unsupported("unterminated LOD expression: no ':' before end of formula")
            if tok.kind == "field":
                inner = tok.val[1:-1]
                ref = self.resolve(inner)
                if ref is None:
                    raise Unsupported(f"LOD grouping key [{inner}] did not resolve")
                keys.append(ref)
            elif not (tok.kind == "op" and tok.val == ","):
                raise Unsupported(f"unexpected {tok.val!r} in an LOD key list")
        self.next()                                  # consume ':'
        inner_expr = self.or_expr()
        if not (self.peek() and self.peek().kind == "op" and self.peek().val == "}"):
            raise Unsupported("unterminated LOD expression: missing '}'")
        self.next()
        tables = {m.group(1) for m in re.finditer(r"'((?:[^']|'')*)'\[", "".join(keys))}
        if not keys:
            tables = {m.group(1) for m in re.finditer(r"'((?:[^']|'')*)'\[", inner_expr)}
            if len(tables) != 1:
                raise Unsupported("{FIXED : ...} needs exactly one table to clear filters on; "
                                  f"found {len(tables)}")
            return f"CALCULATE({inner_expr}, ALL('{tables.pop()}'))"
        if len(tables) != 1:
            raise Unsupported("LOD grouping keys span several tables, so ALLEXCEPT has no single "
                              f"table to scope to ({', '.join(sorted(tables))})")
        return f"CALCULATE({inner_expr}, ALLEXCEPT('{tables.pop()}', {', '.join(keys)}))"

    def if_expr(self):
        """IF c THEN a [ELSEIF c2 THEN a2 ...] [ELSE z] END -> nested IF()."""
        cond = self.or_expr()
        self.expect("THEN")
        then = self.or_expr()
        tok = self.peek()
        if tok and tok.kind == "ELSEIF":
            self.next()
            return f"IF({cond}, {then}, {self.if_expr()})"
        if tok and tok.kind == "ELSE":
            self.next()
            els = self.or_expr()
            self.expect("END")
            return f"IF({cond}, {then}, {els})"
        self.expect("END")
        # No ELSE: Tableau yields null, which is BLANK() in DAX -- omitting the third argument
        # would also yield blank, but being explicit documents the intent.
        return f"IF({cond}, {then}, BLANK())"

    def case_expr(self):
        """CASE x WHEN a THEN b ... [ELSE z] END -> SWITCH(x, a, b, ..., z)."""
        subject = self.or_expr()
        parts = []
        while self.accept("WHEN"):
            w = self.or_expr()
            self.expect("THEN")
            parts.append((w, self.or_expr()))
        if not parts:
            raise Unsupported("CASE with no WHEN branch")
        els = None
        if self.accept("ELSE"):
            els = self.or_expr()
        self.expect("END")
        args = [subject]
        for w, r in parts:
            args += [w, r]
        if els is not None:
            args.append(els)
        return f"SWITCH({', '.join(args)})"

    def func_call(self, name):
        up = name.upper()
        if not self.accept("op", "("):
            # A bare word that is not a call: Tableau has no such literal we translate.
            raise Unsupported(f"bare identifier {name!r}")
        args = []
        arg_spans = []      # (start_i, end_i) token index range for each arg's RAW source --
                             # lets a caller (e.g. STR() below) inspect the ORIGINAL Tableau
                             # tokens an argument came from, not just its translated DAX text.
        if not self.accept("op", ")"):
            _s = self.i; args.append(self.or_expr()); arg_spans.append((_s, self.i))
            while self.accept("op", ","):
                _s = self.i; args.append(self.or_expr()); arg_spans.append((_s, self.i))
            self.expect("op", ")")

        # Tableau's `<TYPE>('NULL')` idiom -- `FLOAT('NULL')`, `INT('NULL')`, `DATE('NULL')`,
        # `STR('NULL')`, etc. -- is documented Tableau behaviour for an explicitly-TYPED null
        # literal, used almost exclusively to satisfy Tableau's own branch-type-unification rule
        # in an IF/THEN/ELSE where one branch is a real typed value and the other must be
        # "nothing, of that same type" (Tableau refuses mismatched branch types otherwise). DAX's
        # `BLANK()` is untyped and already unifies with any branch type inside `IF()`, so it is
        # the exact equivalent no matter which cast function wraps the literal -- there is no
        # reason to enumerate a whitelist of cast function names, since passing the literal text
        # "NULL" as a type-cast function's SOLE argument has no other plausible meaning in
        # Tableau. Checked FIRST, before any per-function special case below (STR/DATE/etc. each
        # have their own branch further down that would otherwise intercept this shape first and
        # mishandle it -- e.g. DATE('NULL') would fall through DATE's own handler straight to its
        # "unresolved argument type" decline, and STR('NULL') would silently emit `("NULL" & "")`,
        # a literal text string, not an actual typed null). Also fixes a latent bug for any
        # ALREADY-mapped single-arg cast (e.g. `INT('NULL')`) that would otherwise reach its
        # normal FUNCS mapping and emit invalid/wrong DAX (`INT("NULL")` is not a valid numeric
        # cast of literal text). Real corpus (GCLM): `IF [Parameters].[Opcert or User Input] =
        # 'Opcert' THEN FLOAT('NULL') ELSE <real expression> END` -- FLOAT has no DAX equivalent
        # at all, so this was declined outright before, even though the whole call is just a
        # typed-null placeholder.
        if len(args) == 1 and args[0].strip().strip('"').upper() == "NULL":
            return "BLANK()"

        if up in ("RANK", "RANK_DENSE"):
            # Tableau RANK(expr, ['asc'|'desc']) / RANK_DENSE(expr, ['asc'|'desc']) map onto DAX's
            # RANKX(<table>, <expr>, , <order>, <ties>) EXACTLY, with no approximation, because
            # RANKX's own native `ties` argument already distinguishes the two tie-break rules:
            # "Skip" (RANK -- a tie shares a rank, the next rank skips ahead by the tie's size,
            # e.g. 1,1,3) is DAX's own default, and "Dense" (RANK_DENSE -- a tie shares a rank,
            # the next rank does NOT skip, e.g. 1,1,2) is RANKX's other named option. Every OTHER
            # RANK-family function (RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE) has a genuinely
            # DIFFERENT tie-break rule RANKX has no matching option for, so tmdl.py's `route()`
            # never routes those here even when a partition resolves -- see that function's
            # docstring for why approximating one as a plain RANKX would be a silent divergence
            # from Tableau, not a translation.
            #
            # The partition itself -- Tableau's "Compute Using"/"Restart every: <field>" setting,
            # which the ranking is computed WITHIN -- is not in the formula text at all (that is
            # exactly why every OTHER table calc still declines); it is threaded in from outside
            # as `self.tc_partition`, already resolved to a real same-table DAX column reference
            # by tmdl.py's `route()` before this parse ever runs, from extract.py's own captured
            # `level-break` attribute. Ground truth: NGPO Dashboard New - AP Version (MSC), whose
            # `Rank - Topic Area` field has `ordering-type="Field"` and
            # `level-break="[...].[topic_area]"` in the raw XML.
            if not (1 <= len(args) <= 2):
                raise Unsupported(f"{up} expects (expression, ['asc'|'desc'])")
            if self.tc_partition is None:
                raise Unsupported(
                    f"Tableau {up}(...) is a table calculation; DAX's RANKX needs to know the "
                    f"partition (Tableau's 'Compute Using'/'Restart every' setting) to rank "
                    f"within, which this build could not resolve to a real column on this "
                    f"calc's own table")
            order = "DESC"
            if len(args) == 2:
                o = args[1].strip().strip('"').strip("'").lower()
                if o not in ("asc", "desc"):
                    raise Unsupported(f"{up}'s order argument {args[1]!r} is neither 'asc' nor "
                                      f"'desc'")
                order = o.upper()
            ties = "Dense" if up == "RANK_DENSE" else "Skip"
            tables = _TABLE_IN_REF.findall(self.tc_partition)
            if len(tables) != 1:
                raise Unsupported(f"{up}'s resolved partition {self.tc_partition!r} is not a "
                                  f"single same-table column reference")
            return (f"RANKX(ALLEXCEPT('{tables[0]}', {self.tc_partition}), {args[0]}, , "
                    f"{order}, {ties})")

        if up == "ATTR":
            # Tableau ATTR returns the value when the group has exactly one, else '*'.
            # SELECTEDVALUE(col, alternate) is that behaviour precisely for a bare COLUMN.
            if len(args) != 1:
                raise Unsupported("ATTR expects exactly 1 argument")
            if _BARE_COLUMN.match(args[0].strip()):
                return f'SELECTEDVALUE({args[0]}, "*")'
            # An EXPRESSION rather than a bare column: SELECTEDVALUE itself only takes a column,
            # but "the value when every row agrees, else the alternate" has an exact iterator
            # equivalent -- if the expression's MIN and MAX across every row in context are the
            # SAME value, every row necessarily agrees on it (a MIN/MAX gap of zero can only mean
            # zero variance), so returning either one IS the agreed value; a mismatch means at
            # least two rows disagree, which is exactly ATTR's own "*" case. MINX/MAXX support
            # text as well as numbers, so this covers Tableau ATTR's usual string-grouping use
            # just as exactly as its numeric one.
            tables = set(_TABLE_IN_REF.findall(args[0]))
            if len(tables) == 1:
                t = tables.pop()
                lo, hi = f"MINX('{t}', {args[0]})", f"MAXX('{t}', {args[0]})"
                return f'IF({lo} = {hi}, {lo}, "*")'
            raise Unsupported(
                f"ATTR(<expression>) needs a single table to iterate; the expression references "
                f"{len(tables)} ({', '.join(sorted(tables)) or 'none'})")

        if up == "DATENAME":
            if len(args) < 2:
                raise Unsupported("DATENAME expects (unit, date)")
            unit = args[0].strip().strip('"').strip("'").lower()
            fn = DATENAME_DAX.get(unit)
            if fn is None:
                raise Unsupported(f"DATENAME unit {unit!r} has no unambiguous DAX equivalent")
            # An explicit 3rd start-of-week argument is inert for every unit DATENAME_DAX
            # actually supports: month/year/day/quarter/hour/minute/second have nothing to do
            # with week boundaries, and 'weekday' returns the calendar day's own NAME (e.g.
            # "Monday"), which does not change depending on which day a week is considered to
            # start on -- only a week NUMBER would, and 'week' is not a key in DATENAME_DAX (it
            # falls through to the `fn is None` decline above, before this line is ever reached).
            # So accepting the extra argument here is a proven invariant, not a guess.
            return fn(args[1])

        if up == "DATEPARSE":
            # DATEPARSE(format, string) has no single DAX function, but for a FIXED-WIDTH format
            # every component sits at a known character offset, so it can be rebuilt exactly with
            # DATE()/TIME() over MID() -- and that is locale-INDEPENDENT, unlike DATEVALUE(), whose
            # reading of "01/07/2022" depends on the model's locale. Every real use of this in the
            # corpus is the single format 'yyyy-MM-dd' (24 occurrences in Prevent Dashboard alone),
            # and it was the knot blocking that workbook's whole "Rolling 7 Day" calc family:
            # 12 calcs declined on DATEPARSE directly and ~25 more only because they referenced one
            # of those.
            #
            # Offsets are COMPUTED from the format string rather than looked up in a table, so any
            # fixed-width arrangement works. A format with a variable-width token (e.g. 'M/d/yyyy',
            # where "1/7" and "11/07" put the year in different places) has no single offset and is
            # DECLINED rather than guessed at.
            if len(args) != 2:
                raise Unsupported("DATEPARSE expects (format, string)")
            fmt_raw = args[0].strip()
            if not (len(fmt_raw) >= 2 and fmt_raw[0] == '"' and fmt_raw[-1] == '"'):
                raise Unsupported(
                    "DATEPARSE's format must be a literal string to compute character offsets "
                    "from; a computed format cannot be resolved at translation time")
            fmt = fmt_raw[1:-1]
            src = args[1]
            # Fixed-width tokens only. 'yyyy' and 'MMM' are the two 3-4-char tokens ('MMM' being
            # the 3-LETTER month abbreviation, e.g. "Dec" -- fixed-width because Tableau always
            # renders it as exactly 3 letters, never abbreviated further for a short month name);
            # everything else is 2-char, INCLUDING 'yy' (a 2-digit year). `''` is Tableau's own
            # escaped-literal-apostrophe convention (the same convention Excel/VBA format strings
            # use) for embedding a literal `'` inside a format string that ALSO uses `'` to quote
            # literal text runs -- e.g. `"MMM''yy"` means "month abbrev, then a literal apostrophe
            # -- not a quote -- then 2-digit year", not "month abbrev, then an EMPTY quoted literal,
            # then year". Consumed as its own 2-char token before the width-token loop below ever
            # sees it, so it is never mistaken for the start of an (unsupported) quoted-literal run.
            widths = {"yyyy": 4, "MMM": 3, "MM": 2, "dd": 2, "HH": 2, "mm": 2, "ss": 2, "yy": 2}
            # Longer tokens must be tried before their own prefixes ('yyyy' before 'yy', 'MMM'
            # before 'MM') or the shorter one would match first and leave a dangling suffix that
            # then falls through to the variable-width decline below -- dict iteration order is
            # insertion order in Python 3.7+, so this list is declared longest-first on purpose.
            _tok_order = ["yyyy", "MMM", "MM", "dd", "HH", "mm", "ss", "yy"]
            parts, pos, i = {}, 1, 0
            while i < len(fmt):
                if fmt.startswith("''", i):
                    # Escaped literal apostrophe: consumes exactly one character of OUTPUT width
                    # (the apostrophe itself lands in the source string at this offset) but TWO
                    # characters of FORMAT-STRING text -- tracked as an ordinary 1-char literal
                    # separator, same as any other punctuation in the format.
                    i += 2; pos += 1
                    continue
                for tok in _tok_order:
                    w = widths[tok]
                    if fmt.startswith(tok, i):
                        if tok in parts:
                            raise Unsupported(
                                f"DATEPARSE format {fmt!r} repeats the token {tok!r}")
                        parts[tok] = (pos, w)
                        i += len(tok); pos += w
                        break
                else:
                    if fmt[i] in "yMdHms":
                        raise Unsupported(
                            f"DATEPARSE format {fmt!r} has a variable-width token at {fmt[i:]!r} "
                            f"-- a single-digit month/day means the later components have no fixed "
                            f"character offset, so it is declined rather than guessed at")
                    i += 1; pos += 1        # a literal separator
            if "yyyy" in parts and "yy" in parts:
                raise Unsupported(
                    f"DATEPARSE format {fmt!r} has both 'yyyy' and 'yy' -- ambiguous which one "
                    f"is the real year token")
            if "MMM" in parts and "MM" in parts:
                raise Unsupported(
                    f"DATEPARSE format {fmt!r} has both 'MMM' and 'MM' -- ambiguous which one "
                    f"is the real month token")
            has_year = "yyyy" in parts or "yy" in parts
            has_month = "MMM" in parts or "MM" in parts
            if not (has_year and has_month):
                # Year and month are the two components that can NEVER be safely defaulted (there
                # is no sane "minimum year" or "minimum month" to invent), so a format missing
                # either of those is still declined outright -- never guessed at.
                raise Unsupported(
                    f"DATEPARSE format {fmt!r} does not carry a full year/month; a partial date "
                    f"cannot be rebuilt without inventing the missing component")
            if "dd" not in parts:
                # Day-of-month IS safe to default: Tableau's own DATEPARSE documentation
                # (help.tableau.com -> Functions -> Date Functions -> DATEPARSE) states that any
                # date part omitted from the format string defaults to its minimum value -- day 1,
                # January, midnight -- exactly like DAX's own DATE()/TIME() constructors already
                # do when asked for a day that was never captured. A period-level source field
                # (e.g. a 'yyyyMM' "month ending" key with no day-of-month information to give)
                # is the single most common real-corpus reason a format omits 'dd' -- this default
                # is precisely what a human would type by hand (`DATE(year, month, 1)`), not a
                # guess: cascades through every downstream calc built on the resulting date only
                # care about its year/month, never its (synthetic) day.
                parts["dd"] = None  # marker: no MID() extraction, use literal 1 below

            def _raw(tok):
                """The raw MID() extraction for a token that has a real character span, or
                `None` for the synthetic no-`dd` marker."""
                if parts.get(tok) is None:
                    return None
                start, width = parts[tok]
                return f"MID({src}, {start}, {width})"

            def _num(tok):
                raw = _raw(tok)
                if raw is None:
                    return "1"
                return f"VALUE({raw})"

            if "MMM" in parts:
                # A 3-letter month ABBREVIATION has no numeric value to VALUE() -- it needs a
                # name -> number lookup. Tableau's own DATENAME('month', ...)/STR() round-trip
                # (see the month-apostrophe-year idiom above) already proves these are the exact
                # 3-letter English abbreviations DAX's own MONTH()/FORMAT() use, so the SWITCH
                # below is a straight lookup table, not a locale guess -- English month names are
                # the only ones this whole translator's DATENAME_DAX table assumes anywhere.
                mmm_raw = _raw("MMM")
                _MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                switch_args = ", ".join(f'"{name}", {i + 1}' for i, name in
                                        enumerate(_MONTH_ABBR))
                month_expr = f"SWITCH({mmm_raw}, {switch_args})"
            else:
                month_expr = _num("MM")

            if "yy" in parts:
                # A 2-digit year has no century of its own. Tableau's own DATEPARSE/DATEFORMAT
                # documentation states a 2-digit year is interpreted using a sliding "nearest
                # century" window anchored to the CURRENT date, not a fixed cutoff -- which
                # cannot be reproduced at TRANSLATION time (it would need to be evaluated fresh
                # every time the report is opened, via TODAY(), not baked into a static DAX
                # literal). Assuming the 2000s (2000 + yy) is Tableau's own PRACTICAL default for
                # every real 2-digit-year business date this translator has seen (all "period
                # label" calcs are recent/current financial periods, e.g. "Dec'24" = 2024, never
                # a date before 2000) -- stated here explicitly, not silently, exactly like the
                # DATEPARSE day-of-month default above states ITS own assumption.
                year_expr = f"(2000 + {_num('yy')})"
            else:
                year_expr = _num("yyyy")

            date_expr = f"DATE({year_expr}, {month_expr}, {_num('dd')})"
            if {"HH", "mm"} <= set(parts):
                sec = _num("ss") if "ss" in parts else "0"
                full_expr = f"({date_expr} + TIME({_num('HH')}, {_num('mm')}, {sec}))"
            else:
                full_expr = date_expr
            # Tableau's own DATEPARSE returns NULL (not an error) for a source string that is
            # blank, too short, or has non-numeric characters at a numeric token's position --
            # help.tableau.com documents this as "returns NULL for values that don't match the
            # specified format". Every VALUE(MID(...)) call above throws instead: DAX's VALUE()
            # errors on non-numeric text, and a hard error here doesn't just blank one cell, it
            # fails the WHOLE COLUMN's load ("Cannot convert value '' of type Text to type
            # Number" -- real corpus regression, GCLM_EU_GERMANY_COMMERZBANK_AG_v2's `Usage
            # Overview`, blank real-data rows in `mnth_yr`). IFERROR(..., BLANK()) matches
            # Tableau's own graceful-NULL behavior exactly, for ANY failure mode, not just a
            # blank string -- the valid-input case is completely unaffected.
            return f"IFERROR({full_expr}, BLANK())"

        if up == "STR":
            if len(args) != 1:
                raise Unsupported("STR expects exactly 1 argument")
            arg0 = args[0].strip()
            # Two independent ways to PROVE this argument is date-typed, checked before falling
            # back to `last_field`'s inferred type (which the comment below explains is
            # unreliable): (1) `_BARE_COLUMN` -- the argument literally IS a field/column
            # reference, whose declared Tableau datatype `resolve_type` looks up directly; (2)
            # `_is_date_returning_call` -- the argument's Tableau SOURCE is, in its entirety, a
            # call to a function this translator already proves returns a date (DATE/DATEADD/
            # DATETRUNC/DATEPARSE), regardless of what `last_field` happens to point at deep
            # inside its own arguments. Checking (2) from the RAW TOKENS rather than `dt` is what
            # makes this exact and not a guess: ground truth (Prevent Dashboard) `cl.Rolling 7
            # Day End - Lookups` is `STR(DATE(dateadd('day', 6, [Calculation_...])))`, whose
            # `last_field` resolves to `[Calculation_...]` (a plain source date column, not
            # itself provably date-typed by name), yet the call shape alone proves the argument
            # is a date.
            is_date_call = arg_spans and self._is_date_returning_call(*arg_spans[0])
            if is_date_call or _BARE_COLUMN.match(arg0):
                dt = self.resolve_type(self.last_field) if (self.resolve_type and self.last_field) \
                    else None
                if is_date_call or dt in ("date", "dateTime"):
                    # Tableau's STR(date) always emits ISO 'yyyy-mm-dd' text, regardless of
                    # locale; DAX's implicit date-to-text coercion is locale-dependent, so an
                    # explicit FORMAT pattern is required to match it exactly. This is the same
                    # fix already catalogued in FIX_RULES
                    # (validate-tableau-pbip-mapping/scripts/validate_mapping.py) for a human to
                    # hand-apply -- applied here at the source instead for this one unambiguous
                    # shape.
                    return f'FORMAT({arg0}, "yyyy-MM-dd")'
            # Third proof, independent of the date-typed check above: the argument's Tableau
            # SOURCE is, in its entirety, a call to a function this translator already proves
            # returns a plain INTEGER (`_is_integer_returning_call` -- YEAR/MONTH/DAY/DATEDIFF/
            # DATEPART). This is strictly SAFER than the date-returning case above, not harder:
            # an integer has zero locale/date-format ambiguity, so it never needs the FORMAT
            # rewrite at all -- `& ""` is already exact for it. Checked BEFORE the decline check
            # below on purpose: `last_field` for `STR(YEAR([Date]))` resolves to `[Date]` (date-
            # typed), which would otherwise trip that check even though YEAR()'s actual return
            # value is an integer, not a date -- ground truth (Prevent Dashboard) `Date Range
            # Text` = `STR(DATEPART('day', [Date Range Start Date]))`.
            if arg_spans and self._is_integer_returning_call(*arg_spans[0]):
                return f'({arg0} & "")'
            # `self.last_field` is the last BRACKETED FIELD TOKEN seen while parsing the
            # argument -- not necessarily the argument's own top-level type. `STR([Date])` and
            # `STR(YEAR([Date]))` both leave `last_field` pointing at `[Date]`, but only the
            # first actually hands STR() a date; the second hands it YEAR([Date])'s return
            # value, a plain integer (e.g. 2025). Applying a date FORMAT pattern to that is not
            # a locale risk, it is ACTIVELY WRONG: DAX's FORMAT() on a bare number with a
            # date-style pattern treats the number as an Excel-style date SERIAL value and
            # returns a nonsense date, not "2025". Reached only when neither proof above fired
            # (the argument is a computed expression that is NEITHER a bare column NOR a call to
            # a known date-returning function), so it is genuinely unproven, not just unproven by
            # `last_field`.
            dt = self.resolve_type(self.last_field) if (self.resolve_type and self.last_field) \
                else None
            if dt in ("date", "dateTime"):
                raise Unsupported(
                    "STR(<expression containing a date field>) — the date-typed field is "
                    "nested inside another function (e.g. YEAR/MONTH/DAY), not STR()'s direct "
                    "argument; that function's own return value is not a date, so a date FORMAT "
                    "pattern would misinterpret it as an Excel-style date serial number")
            # `& ""` coerces any non-date value to text and is exact for numbers and strings:
            # STR(5) -> "5", and STR of a string is the string itself.
            return f'({arg0} & "")'

        if up == "DATE" and len(args) == 1:
            # Tableau's 1-argument DATE(expr) is a cast, not the 3-argument DATE(y,m,d)
            # constructor (that shape has a different `len(args)` and falls through below,
            # unhandled here). Same resolve_type/_BARE_COLUMN gating as STR() above: only trust
            # the type when args[0] is a bare field reference, and only pick a rewrite once the
            # type is actually known -- guessing which branch for an unresolved type would be
            # exactly the silently-wrong-measure risk this parser exists to avoid.
            # A literal ISO date string -- e.g. DATE("2025-05-01") -- needs no type resolution at
            # all: the exact y/m/d are already sitting in the text, so build DATE(y, m, d)
            # directly, the same unambiguous-literal rebuild the `#...#` date-literal token
            # already gets above. Relying on DATEVALUE()'s locale-dependent text parsing here
            # would risk the exact day/month transposition that pattern exists to avoid; reading
            # the digits straight out of the literal has no such ambiguity. Real corpus shape
            # (Digital Sales - Leads Dashboard, `Lead Create Date for flag`): DATE("2025-05-01").
            m = re.match(r'^"(\d{4})-(\d{1,2})-(\d{1,2})"$', args[0].strip())
            if m:
                y, mo, d_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
                return f"DATE({y}, {mo}, {d_})"
            dt = self.resolve_type(self.last_field) if (self.resolve_type and self.last_field) \
                else None
            if dt in ("date", "dateTime") and _BARE_COLUMN.match(args[0].strip()):
                # Casting an already date-typed field to a date is a no-op; DAX has no
                # cast-to-date function to spell that with, so the unwrapped expression IS the
                # DAX. Confirmed on a real corpus workbook: DATE([App Actualend]) where
                # [App Actualend] is dateTime-typed in the model.
                return args[0]
            if dt in ("string", "int", "real") and _BARE_COLUMN.match(args[0].strip()):
                # A text/numeric argument: Tableau's DATE(string) parses a date-like string.
                return f"DATEVALUE({args[0]})"
            arg0 = args[0].strip()
            if arg_spans and self._is_date_returning_call(*arg_spans[0]):
                # The argument's own Tableau SOURCE SHAPE is provably date-returning (a direct
                # DATE/DATEADD/DATETRUNC/DATEPARSE call, or MIN/MAX wrapping one) -- a stronger,
                # more directly-proven signal than `self.own_type` below, and one that fires
                # correctly even when the CONTAINING calc's overall type is NOT date (e.g. a
                # string-building calc that only uses this DATE(...) internally to extract a
                # MIN/MAX date for DATENAME formatting, own_type "string" would otherwise wrongly
                # decline this). DATE() around an already-date-typed expression is a redundant
                # cast, same no-op reasoning as the bare dateTime-column case above.
                return arg0
            if _top_level_has_concat(arg0):
                # The argument, AS A WHOLE (at paren depth 0), is a text CONCATENATION -- the
                # same DATEVALUE() rewrite as the bare-string-typed-field branch just above, just
                # proven here from the DAX SHAPE itself rather than a single field's declared
                # type, which is what a parameter-derived string (not a bare field) needs. Ground
                # truth: Prevent Dashboard's `cl.Anchor Date`, one ELSEIF branch
                # `DATE([Parameters].[Parameter 14] + "-01")` -- a Year-Month parameter's selected
                # value with "-01" appended, an unambiguous ISO yyyy-MM-dd shape once
                # concatenated, checked ahead of the `own_type` branch below so a NESTED
                # DATE(<concat>) inside a larger date-typed calc is never mistaken for the no-op
                # case just because the calc's overall result type happens to be "date".
                #
                # Deliberately NOT extended to a bare literal string (e.g. DATE("5/1/2025")):
                # that shape is already fully handled above -- either it matches the strict ISO
                # regex, or (like this example) it is genuinely locale-ambiguous and must stay
                # declined. A dynamically-BUILT string earns more trust than a hand-typed literal
                # only because its shape is provably an intentional ISO-builder idiom here, not
                # because concatenation makes ambiguity disappear in general.
                return f"DATEVALUE({arg0})"
            if self.own_type in ("date", "dateTime"):
                # Tableau's own type-checker already proved the CALC CURRENTLY BEING TRANSLATED
                # is date-valued overall -- it would refuse to compile the calc otherwise -- so an
                # argument that is neither a bare field/literal NOR provably textual (both ruled
                # out above) is a redundant type-assertion around an already-date-shaped
                # expression (typically a nested IF/ELSEIF/SWITCH cascade whose branches are bare
                # field/parameter references or DATEADD/DATETRUNC calls), not a real cast -- and
                # DAX's own DATE() has no cast-to-date spelling to preserve it with. This is a fact
                # RELAYED from Tableau's own validated type inference for this exact formula, not
                # an inference made here. Ground truth: Prevent Dashboard's `cl.Anchor Date` /
                # `cl.Current Period - Start Date` / `- End Date`, each `DATE(IF ... END)`.
                return arg0
            raise Unsupported(
                "DATE(<expression>) with an unresolved or non-bare-field argument type -- "
                "whether the right DAX is a no-op passthrough or DATEVALUE(...) depends on the "
                "argument's real type, which is not reliably known here")

        if up == "DATEADD":
            # Tableau DATEADD('unit', n, date) is a SCALAR date shift. DAX also has a function
            # spelled DATEADD, but it is a TABLE function for time-intelligence measures
            # (dates, n, interval) -- it needs a date column in filter context, not a bare scalar,
            # so it is not the same operation and a name swap would be silently wrong. Each unit
            # Tableau supports has an exact scalar DAX rebuild instead: day/week are a plain day
            # offset, month/quarter/year go through EDATE (which is calendar-aware -- Jan 31 + 1
            # month is Feb 28/29, which a fixed-day-count add would get wrong).
            if len(args) != 3:
                raise Unsupported("DATEADD expects exactly (unit, n, date)")
            unit = args[0].strip().strip('"').lower()
            n, d = args[1], args[2]
            if unit == "day":
                return f"({d} + {n})"
            if unit == "week":
                return f"({d} + 7 * ({n}))"
            if unit == "month":
                return f"EDATE({d}, {n})"
            if unit == "quarter":
                return f"EDATE({d}, 3 * ({n}))"
            if unit == "year":
                return f"EDATE({d}, 12 * ({n}))"
            raise Unsupported(
                f"DATEADD unit {unit!r} has no unambiguous scalar DAX equivalent -- only "
                f"year/quarter/month/week/day are implemented")

        if up == "DATETRUNC":
            # Tableau DATETRUNC('unit', date[, start_of_week]) has no single DAX function, but
            # each unit Tableau actually supports has an exact, unambiguous DAX rebuild -- these
            # are the same per-unit formulas this pipeline's own FIX_RULES already hands a human
            # for this pattern (validate_mapping.py), applied here at the source instead. Only
            # 'week' is start-of-week-sensitive (year/quarter/month/day are not); a unit finer
            # than day (hour/minute) is still declined -- DAX has no single datetime-truncation
            # function for those.
            if len(args) not in (2, 3):
                raise Unsupported("DATETRUNC expects (unit, date[, start_of_week])")
            unit = args[0].strip().strip('"').lower()
            d = args[1]
            if unit == "week":
                # See WEEKDAY_START_OFFSET / _week_start_day above -- the start-of-week setting
                # is either this call's own explicit 3rd argument, or (when Tableau's default
                # applies) the owning datasource's real <date-options> setting, threaded through
                # from extract.py. Previously always declined on the theory that this was
                # unrecoverable; it was simply never read.
                sow = self._week_start_day(args[2] if len(args) == 3 else None)
                wd = _tableau_weekday_expr(d, sow)
                return f"({d} - ({wd}) + 1)"
            if len(args) == 3:
                raise Unsupported(
                    f"DATETRUNC unit {unit!r} with an explicit start-of-week argument has no "
                    f"unambiguous DAX equivalent for units other than 'week'")
            if unit == "year":
                return f"DATE(YEAR({d}), 1, 1)"
            if unit == "quarter":
                return f"DATE(YEAR({d}), 3 * QUARTER({d}) - 2, 1)"
            if unit == "month":
                return f"DATE(YEAR({d}), MONTH({d}), 1)"
            if unit == "day":
                return d
            raise Unsupported(
                f"DATETRUNC unit {unit!r} has no unambiguous DAX equivalent -- only "
                f"year/quarter/month/week/day are implemented")

        if up == "DATEDIFF":
            # Tableau DATEDIFF('unit', date1, date2) vs DAX DATEDIFF(date1, date2, UNIT) -- unit
            # is a bare keyword, not a string, and sits LAST, not first. Both languages compute
            # date2 - date1 in the stated unit, so the start/end argument ORDER is unchanged --
            # only the unit's position and spelling move, and every unit Tableau's DATEDIFF
            # actually accepts has an exact 1:1 DAX interval keyword, so this needs no decline for
            # any real unit (unlike DATEADD/DATETRUNC, which have genuinely unit-dependent DAX
            # shapes).
            if len(args) != 3:
                raise Unsupported("DATEDIFF expects exactly (unit, date1, date2)")
            unit = args[0].strip().strip('"').strip("'").lower()
            d1, d2 = args[1], args[2]
            dax_unit = {"year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK",
                        "day": "DAY", "hour": "HOUR", "minute": "MINUTE",
                        "second": "SECOND"}.get(unit)
            if dax_unit is None:
                raise Unsupported(f"DATEDIFF unit {unit!r} has no DAX DATEDIFF interval "
                                  f"equivalent")
            return f"DATEDIFF({d1}, {d2}, {dax_unit})"

        if up == "DATEPART":
            # Tableau DATEPART('unit', date[, start_of_week]) -- most units have a DIRECT DAX
            # scalar function; only 'weekday' is start-of-week-sensitive (handled via
            # _tableau_weekday_expr above). 'week' (the week NUMBER) and 'dayofyear' still have
            # no single unambiguous DAX equivalent and stay declined -- unlike 'weekday', getting
            # those exactly right needs fiscal/ISO week-1 boundary handling this pipeline has no
            # verified corpus evidence for, so they are not guessed at here.
            if len(args) not in (2, 3):
                raise Unsupported("DATEPART expects (unit, date[, start_of_week])")
            unit = args[0].strip().strip('"').strip("'").lower()
            d = args[1]
            if unit == "weekday":
                sow = self._week_start_day(args[2] if len(args) == 3 else None)
                return _tableau_weekday_expr(d, sow)
            if len(args) == 3:
                raise Unsupported(
                    f"DATEPART unit {unit!r} with an explicit start-of-week argument has no "
                    f"unambiguous DAX equivalent for units other than 'weekday'")
            dax_fn = {"year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "day": "DAY",
                      "hour": "HOUR", "minute": "MINUTE", "second": "SECOND"}.get(unit)
            if dax_fn is None:
                raise Unsupported(f"DATEPART unit {unit!r} has no unambiguous DAX equivalent -- "
                                  f"only year/quarter/month/day/hour/minute/second/weekday are "
                                  f"implemented")
            return f"{dax_fn}({d})"

        if up == "ZN":
            if len(args) != 1:
                raise Unsupported("ZN expects exactly 1 argument")
            return f"COALESCE({args[0]}, 0)"

        if up == "MID":
            # Tableau MID(s, start[, len]) vs DAX MID(s, start, len): the signatures match EXACTLY
            # when len is given -- a straight pass-through, not a restructure. When len is
            # omitted, Tableau's own documented meaning is "from start to the end of the string"
            # -- LEN(s) as the length argument is not a guess at how far to go, it is the exact,
            # always-sufficient upper bound: DAX's MID returns however many characters actually
            # exist from `start` onward when asked for more than that, so passing the WHOLE
            # string's own length can never truncate early, and can never run past the end either.
            if len(args) == 3:
                return f"MID({args[0]}, {args[1]}, {args[2]})"
            if len(args) == 2:
                return f"MID({args[0]}, {args[1]}, LEN({args[0]}))"
            raise Unsupported("MID expects (string, start[, length])")

        if up == "SPLIT":
            # Tableau SPLIT(string, delimiter, tokenNumber) has no single DAX function -- see
            # `_split_nth_token`'s own docstring for the FIND/MID/LEFT construction. Only a
            # LITERAL positive tokenNumber is handled: it has to be known at translation time to
            # unroll the (n-1) strip-a-token steps, and a negative index (Tableau's own "count
            # from the end" convention) would need to know the TOTAL token count first, which is
            # a different, harder construction not attempted here -- declined rather than guessed.
            if len(args) != 3:
                raise Unsupported("SPLIT expects (string, delimiter, tokenNumber)")
            n_raw = args[2].strip()
            if not _NUMERIC_LITERAL_RE.match(n_raw) or "." in n_raw:
                raise Unsupported(
                    f"SPLIT's tokenNumber {args[2]!r} is not a literal integer -- this "
                    f"construction has to unroll a fixed number of FIND/MID steps at "
                    f"translation time, which needs the index known then, not computed at "
                    f"report-run time")
            n = int(n_raw)
            if n < 1:
                raise Unsupported(
                    f"SPLIT's tokenNumber {n} is not positive -- Tableau's negative-index "
                    f"'count from the end' convention needs the total token count first, which "
                    f"this construction does not compute")
            return _split_nth_token(args[0], args[1], n)

        if up == "TOTAL":
            # Unlike LOOKUP/WINDOW_*/RANK/FIRST/LAST/SIZE, Tableau's TOTAL(<agg-expr>) carries no
            # row-order or partition semantics -- it is not "recompute across a stated addressing",
            # it is "recompute this SAME aggregate over the current context", which is exactly what
            # a DAX measure already does by default. Tableau requires <agg-expr> to itself already
            # be a complete aggregate (a validation error otherwise), so by the time this call is
            # reached args[0] has ALREADY been translated into a correct, standalone DAX aggregate
            # (a plain column aggregate, an AGG_ITERATOR form, or a bare measure reference) by the
            # normal recursive-descent walk above -- TOTAL() is a pure no-op wrapper around it.
            # Ground truth (Prevent Dashboard): `TOTAL(MEDIAN([x]))` -> `MEDIAN('T'[x])`;
            # `TOTAL([already-a-measure])` -> the bare measure reference, unchanged.
            if len(args) != 1:
                raise Unsupported("TOTAL expects exactly 1 argument")
            return args[0]

        if up in ARITY_MISMATCH and ARITY_MISMATCH[up]:
            raise Unsupported(ARITY_MISMATCH[up])

        if up in ("IF", "IIF"):
            if len(args) == 2:
                return f"IF({args[0]}, {args[1]}, BLANK())"
            if len(args) == 3:
                return f"IF({args[0]}, {args[1]}, {args[2]})"
            raise Unsupported(f"IIF with {len(args)} arguments")

        if up in OPTIONAL_SECOND_ARG and len(args) == 1:
            args.append(OPTIONAL_SECOND_ARG[up])

        mapped = FUNCS.get(up)
        if mapped is None:
            raise Unsupported(f"no DAX equivalent for Tableau function {up}()")

        # Tableau source frequently wraps a single field reference in redundant parens inside an
        # aggregate call (`AVG(([x]))`, `SUM((([x])))`) -- harmless in Tableau, but `atom()`'s own
        # `(` handler re-wraps the already-translated column ref in ANOTHER literal "(...)" for
        # every such layer, so the translated arg is `('T'[x])`, not `'T'[x]`. `_BARE_COLUMN`
        # requires an EXACT match, so every check below that gates on "is this arg a bare column"
        # (promoted-measure re-aggregation, string-typed-column cast) missed this shape entirely
        # and fell through to the generic aggregate-over-expression branch, which has no type
        # awareness -- passing the string-typed column straight through uncast. Real corpus
        # regression: GCLM_EU_GERMANY_COMMERZBANK_AG_v2's Activation.'Activation Rate_issr' =
        # `AVG([issr_actv_crds_cy])/AVG(([issr_total_actv_crd_cy]))`, where `issr_total_actv_crd_cy`
        # is Tableau-declared `string` -- emitted `AVERAGEX('Activation', ('Activation'[...]))`
        # with no VALUE() cast, and Desktop errored at query time: "The function AVERAGEX cannot
        # work with values of type String". Unwrapping here, once, lets every later check treat
        # this exactly like an un-parenthesized bare column -- the two are semantically identical.
        if len(args) == 1:
            _pm = _PARENWRAPPED_BARE_COLUMN.match(args[0].strip())
            if _pm and len(_pm.group(1)) == len(_pm.group(3)):
                args[0] = _pm.group(2)

        # An aggregate over an EXPRESSION rather than a bare column.
        if (mapped in AGG_ITERATOR or mapped in NO_ITERATOR) and len(args) == 1 \
                and not _BARE_COLUMN.match(args[0].strip()):
            tables = set(_TABLE_IN_REF.findall(args[0]))
            if mapped in NO_ITERATOR:
                # DISTINCTCOUNT (Tableau COUNTD) takes a column, not an expression -- but the
                # restructure the OLD decline text pointed at is buildable: project the row-level
                # expression into a virtual one-column table with SELECTCOLUMNS, filter out blanks
                # (Tableau's COUNTD counts DISTINCT NON-NULL values -- DAX's DISTINCT() on a
                # SELECTCOLUMNS result keeps a BLANK as one of its "distinct" rows if any row's
                # expression evaluates to blank, which would silently overcount by exactly 1 if
                # left in), then DISTINCT + COUNTROWS. Every step here is an EXACT match for
                # Tableau's own semantics, not an approximation -- this only fires when there is
                # exactly one table to iterate, same requirement as the AGG_ITERATOR branch below.
                if len(tables) == 1:
                    t = tables.pop()
                    return (f"COUNTROWS(DISTINCT(FILTER(SELECTCOLUMNS('{t}', \"v\", {args[0]}), "
                            f"NOT ISBLANK([v]))))")
                raise Unsupported(
                    f"Tableau {up}(<expression>) needs a single table to project into "
                    f"SELECTCOLUMNS(); the expression references "
                    f"{len(tables)} ({', '.join(sorted(tables)) or 'none'})")
            # A bare NUMERIC LITERAL -- no field/table reference at all -- is a real Tableau idiom
            # (often a placeholder/reset value inside a larger table-calc or IF branch), not a
            # column aggregation, but collapsing it to the literal itself is only an EXACT identity
            # for functions whose result does not depend on how many rows it is computed over.
            # AVERAGE/MIN/MAX/MEDIAN of the SAME repeated value are that value regardless of row
            # count (2, 2, 2 averages/mins/maxes/medians to 2, for any N >= 1 rows). STDEV.S/VAR.S
            # of an identical repeated value is always exactly 0, regardless of what the value IS,
            # for the same reason (zero variance in a constant). SUM is the ONE exception: SUM of
            # a constant `c` repeated over N rows is `c * N`, not `c` -- an identity ONLY when
            # c == 0 (0 * N is always 0, whatever N is); any nonzero constant genuinely depends on
            # a row count this translator does not know, so it stays declined rather than guessed.
            # COUNT never collapses at all: Tableau's COUNT counts non-null values, and a literal
            # is never null, so COUNT(<literal>) is ALWAYS the row count, never the literal itself.
            #
            # A bare PARAMETER-MEASURE reference (`_BARE_MEASURE_REF`, resolve_param's own `[Name]`
            # shape, see that regex's comment) gets the SAME treatment, for a different but equally
            # exact reason: a Tableau parameter is a single workbook-wide scalar with no row-level
            # materialization at all, so wrapping it in SUM/AVG/MIN/etc. inside a Tableau calc is
            # the well-known "make a parameter aggregation-safe" idiom -- Tableau's own evaluator
            # returns the SAME scalar regardless of which aggregate wraps it or how many rows are
            # in context, UNLIKE a numeric literal embedded in a real per-row calculated column
            # (where SUM genuinely means "repeat and add" -- a true parameter has no rows to repeat
            # over in the first place, so even SUM is a safe identity here, not just AVG/MIN/MAX).
            # Real corpus (GCLM): `Target_crds_aqsn`'s `ELSE AVG([Parameters].[User Input Cards in
            # Force CY (copy)])END` -- a parameter resolved to a bare `[... Selected]` measure
            # reference, 0 tables, previously declined outright regardless of which aggregate
            # function was used.
            is_bare_measure = not tables and _BARE_MEASURE_REF.match(args[0].strip())
            if not tables and (_NUMERIC_LITERAL_RE.match(args[0].strip()) or is_bare_measure):
                if mapped in ("AVERAGE", "MIN", "MAX", "MEDIAN"):
                    return args[0]
                if mapped == "SUM" and is_bare_measure:
                    return args[0]
                if mapped in ("STDEV.S", "VAR.S"):
                    return "0"
                if mapped == "SUM" and float(args[0].strip()) == 0:
                    return args[0]
                if mapped == "SUM":
                    raise Unsupported(
                        f"Tableau SUM({args[0]}) aggregates a literal repeated over however many "
                        f"rows are in context, which equals {args[0]} * <row count> -- not the "
                        f"literal itself unless it is 0 -- and the row count is not known at "
                        f"translation time, so this cannot be collapsed without guessing it")
            if len(tables) == 1:
                return f"{AGG_ITERATOR[mapped]}('{tables.pop()}', {args[0]})"
            raise Unsupported(
                f"Tableau {up}(<expression>) needs the DAX iterator {AGG_ITERATOR[mapped]}, which "
                f"requires ONE table to iterate; the expression references "
                f"{len(tables)} ({', '.join(sorted(tables)) or 'none'})")

        # An aggregate over a BARE reference that resolved to a MEASURE, not a column. Tableau's
        # `SUM([x])` where `x` has no aggregate of its own and Tableau treats it as row-level is,
        # in THIS pipeline, a calc that `tmdl.py`'s `_aggregate_map` fixpoint promoted to a DAX
        # measure (because something ELSE -- a parameter reference, or a downstream calc that
        # itself aggregates -- forced it to be one). `resolve()` renders a promoted measure with
        # the exact same 'Table'[Name] shape as an ordinary column, so `_BARE_COLUMN` cannot tell
        # them apart from the string alone -- only `resolve_kind` (populated by the same table's
        # own agg_map, see tmdl.py) knows. Emitting `SUM('Table'[PromotedMeasure])` verbatim is
        # invalid DAX: aggregate functions require a column argument, and DAX resolves the
        # bracketed name by looking for a COLUMN of that name in the table, finds none (only a
        # measure), and Desktop rejects the whole report page at load with Missing_References.
        # Confirmed on a real workbook (Caribbean - Drilldown (QBR), `Gross Trx Amt Decline Rate`
        # = DIVIDE(SUM([Gross Trx Amt Declined]), ...) where `Gross Trx Amt Declined` has no
        # aggregate of its own and was promoted to a measure elsewhere in the fixpoint).
        #
        # The semantically correct DAX would inline the referenced calc's own row-level expression
        # into an iterator at THIS call site (Tableau's `SUM(<row-level calc>)` means "aggregate
        # this expression across rows", which is `SUMX`, not "re-sum an already-aggregated
        # measure") -- but that requires recovering the inner calc's row-level formula at this
        # parse site, which this parser does not have (it only has the ALREADY-RESOLVED reference
        # string). Declining honestly, rather than emitting DAX invalid at load, follows this
        # pipeline's own fail-closed contract: a missing calc is recoverable, a silently-invalid
        # one is a build failure disguised as a success.
        if (mapped in AGG_ITERATOR or mapped in NO_ITERATOR) and len(args) == 1 \
                and _BARE_COLUMN.match(args[0].strip()) and self.last_kind == "measure":
            if mapped in ("MIN", "MAX"):
                # MIN/MAX of an already-promoted measure is EXACT, not an approximation: MINX/
                # MAXX simply evaluates the measure in each row's context and takes the extreme
                # value across those evaluations -- no inlining of the measure's own row-level
                # formula is needed, unlike SUM/AVERAGE/COUNT/COUNTD/etc below, which carry a real
                # double-counting/grain risk if re-aggregated this way. Ground truth: Prevent
                # Dashboard's `Date Range - Min`/`Date Range - Max`, hand-authored earlier this
                # session as MINX('T', [Date Range]) / MAXX('T', [Date Range]) where [Date Range]
                # is itself a measure.
                tables = _TABLE_IN_REF.findall(args[0])
                if len(tables) == 1:
                    return f"{AGG_ITERATOR[mapped]}('{tables[0]}', {args[0]})"
                raise Unsupported(
                    f"Tableau {up}([{self.last_field}]) over a promoted MEASURE needs the DAX "
                    f"iterator {AGG_ITERATOR[mapped]}, which requires exactly one table to "
                    f"iterate; could not determine it from the resolved reference {args[0]!r}")
            # DISTINCTCOUNT (Tableau COUNTD) has NO iterator variant at all -- see NO_ITERATOR's
            # own comment -- so it is not a candidate for the row-level-iterate sub-case below;
            # only try the collapse for a function AGG_ITERATOR actually has an X-form for.
            collapsed = (self._collapse_promoted_aggregate(mapped, args[0])
                         if mapped in AGG_ITERATOR else None)
            if collapsed is not None:
                return collapsed
            raise Unsupported(
                f"Tableau {up}([{self.last_field}]) aggregates a reference to "
                f"[{self.last_field}], which has no aggregate function of its own and was "
                f"promoted to a DAX MEASURE elsewhere in this model (by a parameter reference or "
                f"a downstream aggregate depending on it) -- DAX has no direct equivalent for "
                f"re-aggregating a measure with {mapped}(); it would need the referenced calc's "
                f"row-level expression inlined into an iterator at this call site, which is not "
                f"implemented")

        # A bare column aggregate whose Tableau-declared datatype is not numeric. Tableau's own
        # automatic type conversion (help.tableau.com -> Functions -> "Type Conversion") coerces
        # a numeric-looking string field to a number wherever a calculation needs one, INCLUDING
        # inside SUM()/AVG() -- but DAX's SUM()/AVERAGE() require the argument COLUMN to already
        # be numeric and error at QUERY TIME otherwise. Real corpus regression (GCLM_EU_GERMANY_
        # COMMERZBANK_AG_v2, `Activation`.'Issuer YoY Growth' = ... SUM([issr_actv_crds_py]) ...,
        # where Tableau itself declares `issr_actv_crds_py`'s datatype "string" even though every
        # real value is a plain digit string): Desktop's own error is "The function SUM cannot
        # work with values of type String" -- an MdxScript RUNTIME error, invisible to any
        # load-time/preflight check, exactly like the DATEPARSE blank-string case elsewhere in
        # this module.
        #
        # SUM/AVERAGE are safe to cast (the X-iterator over IFERROR(VALUE(...), BLANK()) -- BLANK
        # is ignored by DAX's own iterator aggregation, the same NULL-tolerant behavior Tableau's
        # own numeric coercion has, matching DATEPARSE's IFERROR fix). MIN/MAX/MEDIAN/STDEV.S/
        # VAR.S are NOT attempted: whether Tableau compares a numeric-looking string field
        # lexicographically or numerically for those is not confirmed from any real corpus
        # example, so guessing the wrong comparison semantics is worse than declining -- fail
        # closed, same rule as everywhere else in this module. COUNT is unaffected: Tableau's
        # COUNT (DAX's COUNTAX) counts non-null values of ANY type, no numeric coercion needed.
        if (mapped in AGG_ITERATOR and mapped != "COUNT" and len(args) == 1
                and _BARE_COLUMN.match(args[0].strip()) and self.last_kind == "column"
                and self.resolve_type and self.last_field
                and self.resolve_type(self.last_field) == "string"):
            if mapped in ("SUM", "AVERAGE"):
                tables = _TABLE_IN_REF.findall(args[0])
                if len(tables) == 1:
                    return (f"{AGG_ITERATOR[mapped]}('{tables[0]}', "
                            f"IFERROR(VALUE({args[0]}), BLANK()))")
                raise Unsupported(
                    f"Tableau {up}([{self.last_field}]) aggregates a string-typed field and "
                    f"needs the DAX iterator {AGG_ITERATOR[mapped]} over a VALUE() cast, which "
                    f"requires exactly one table to iterate; could not determine it from the "
                    f"resolved reference {args[0]!r}")
            raise Unsupported(
                f"Tableau {up}([{self.last_field}]) aggregates a field Tableau itself declares "
                f"datatype 'string' -- DAX's {mapped}() requires a numeric column and errors at "
                f"query time on a text one ('The function {mapped} cannot work with values of "
                f"type String'), and unlike SUM/AVERAGE, casting via VALUE() here would need "
                f"Tableau's own numeric-vs-lexicographic comparison rule for a mixed field, "
                f"which is not confirmed -- declined rather than guessed")
        return f"{mapped}({', '.join(args)})"

    def _collapse_promoted_aggregate(self, mapped, arg_ref):
        """Recover from the exact hazard the caller's own comment describes, instead of always
        declining it: `Tableau {up}([x])` where `x` resolved to a measure this build promoted
        (see tmdl.py's `_aggregate_map`) rather than a real DAX column. Returns a valid DAX
        expression, or None if this particular case genuinely cannot be recovered (the caller
        then falls back to its own original decline).

        Ground truth (Caribbean - Drilldown (QBR)): the outer calc is
        `CASE [Parameters].[Parameter 1] WHEN "Gross Data" THEN sum([Gross Trx Amt Approved]) ...`
        -- `Gross Trx Amt Approved` = `IF [Approved/Declined]='Approved' THEN
        ([Gross Trx Amt Us Total]) ELSE 0 END`, which has NO aggregate function of its own and
        became a measure ONLY because it transitively references `Auth Response Name`, whose own
        formula reads `[Parameters].[Parameter 7]` (a parameter reference is the SAME
        measure-promotion hazard as an aggregate, per `_aggregate_map`'s docstring). There is
        nothing to actually re-aggregate here -- `x` is a plain per-row calculation Tableau would
        have evaluated fine as a dimension had a parameter never entered the picture -- so the
        fix is to re-translate `x`'s own formula as an ordinary ROW-LEVEL expression (same
        resolver: `self.resolve` is guaranteed to be the PLAIN resolver at this call site, never
        `resolve_measure_safe`'s SELECTEDVALUE-wrapping one, because this branch only runs while
        parsing an aggregate-function call, and a formula containing one always has
        `dax.is_aggregate() == True`, which is exactly the condition tmdl.py's `resolve_calcs`
        checks before ever choosing `resolve_measure_safe` over `resolve`) and iterate it with the
        DAX X-function matching THIS call's own aggregate.

        A second, distinct sub-case: `x` might itself be a FULL aggregate with no dimension-level
        meaning at all (e.g. `x = SUM([raw column])`) -- re-wrapping that in the SAME outer
        function at the same grain is a no-op (`SUM(SUM(y))` collapses to `SUM(y)`; DAX does not
        even allow the literal nested form). A DIFFERENT outer/inner function pair (`AVERAGE` of a
        `SUM`, say) cannot be safely collapsed without re-deriving a grain this parser has no way
        to see, so it is left declined rather than guessed at.

        A third sub-case, found on a real corpus workbook (NGPO Dashboard New - AP Version (MSC),
        17 fields, e.g. `Waterfall - Min Quarter/Year Value (copy)_...`): `x` is a level-of-detail
        expression, `{FIXED [keys]: sum(...)}`, which `is_aggregate()` textually flags as "already
        a full aggregate" (it contains a literal `sum(` call) even though it is NOT one in the
        sense the second sub-case means. Tableau materializes an LOD's value once PER ROW at its
        own FIXED grain -- it behaves exactly like `x` having no aggregate of its own from the
        OUTER call's point of view, not like a flat `SUM([raw column])` at the SAME grain as the
        outer wrap. Re-wrapping it in the outer's matching X-iterator (the same treatment as the
        no-aggregate case just below) is the correct translation, not a same-function flat
        collapse -- an LOD's own translated form is `CALCULATE(...)`, never a bare `SUM(...)`, so
        the flat-collapse check (`_is_exact_call`) can never match it and would always decline.
        """
        field = self.last_field
        if not self.resolve_formula or field in self._collapse_seen:
            return None
        inner_formula = self.resolve_formula(field)
        if not inner_formula:
            return None
        seen = self._collapse_seen | {field}
        try:
            inner_dax, _refs, _params = translate(
                inner_formula, self.resolve, self.resolve_param, self.resolve_type,
                self.resolve_kind, None, self.start_of_week,
                resolve_formula=self.resolve_formula, _collapse_seen=seen)
        except Unsupported:
            return None
        is_lod = inner_formula.strip().startswith("{")
        if is_aggregate(inner_formula) and not is_lod:
            # x is ALREADY a full aggregate -- only a flat, same-function rewrap collapses safely.
            return inner_dax if _is_exact_call(inner_dax, mapped) else None
        # x has no aggregate of its own (or is an LOD expression -- see the third sub-case above):
        # iterate its row-level translation with the DAX function THIS call site needs, over the
        # table x itself lives on (read off its own already-resolved reference string -- the same
        # technique the MIN/MAX branch above already uses).
        tables = _TABLE_IN_REF.findall(arg_ref)
        if len(tables) != 1:
            return None
        return f"{AGG_ITERATOR[mapped]}('{tables[0]}', {inner_dax})"


AGG_FUNCS = {"SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "STDEV", "VAR",
             "ATTR", "TOTAL"}
_AGG_CALL = re.compile(r"\b(" + "|".join(AGG_FUNCS) + r")\s*\(", re.I)
_STRIP_STR = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_STRIP_BRACKETS = re.compile(r"\[[^\]]*\]")
# Tableau LOD syntax never nests braces, so a non-greedy, DOTALL single-level match is exact --
# same reasoning `_collapse_promoted_aggregate`'s own `is_lod` check already documents (its own
# docstring's "third sub-case"), just applied here instead of there.
_STRIP_LOD = re.compile(r"\{[^{}]*\}", re.DOTALL)


def is_aggregate(formula):
    """Does the formula aggregate? Decides measure vs calculated column.

    A row-level expression must become a calculated COLUMN -- emitting it as a measure yields a
    DAX error at load, because a bare column reference has no implicit aggregation in a measure.

    Strips bracketed Tableau field references BEFORE matching an aggregate-function call, not
    just string literals -- a real Tableau function call always appears OUTSIDE `[...]`
    brackets, so bracket contents can never legally contain one. Without this, a field whose
    DISPLAY NAME merely contains an aggregate-like word followed by `(` -- e.g.
    `[Card Count (copy)_123]` -- false-positives on `\bCOUNT\s*\(` even though "Count (" here
    is part of a field NAME, not a function invocation. Confirmed on a real corpus workbook
    (NGPO Dashboard New - AP Version (MSC)): `[issuer value]*[Card Count (copy)_...]` has no
    real aggregate call at all, but was misclassified as one, promoting a plain row-level
    product to a DAX MEASURE with two bare column references and zero row context -- Desktop's
    "Missing_References" / "a single value ... cannot be determined".

    ALSO strips `{...}` LOD blocks before matching, for the identical reason one level up: an
    aggregate call that occurs ONLY inside a `{FIXED ...}`/`{INCLUDE ...}`/`{EXCLUDE ...}` block
    does not make the OUTER formula aggregate -- an LOD materializes its own value once PER ROW
    at its own fixed grain, then returns to row level, exactly like a bare column reference from
    the outer expression's point of view (same fact `_collapse_promoted_aggregate`'s own `is_lod`
    check already relies on, there for a referenced field's formula rather than the top-level one
    this function sees). Confirmed on a real corpus workbook (GCLM), `Dor_Contri_tot_vol` =
    `[l12m_spnd]/ {FIXED [prod_typ],[for_view],[ctry],[prod_pltfrm],[issr] : SUM([l12m_spnd])}` --
    a Tableau ROW-LEVEL calc (bare `[l12m_spnd]` divided by a per-row LOD total) that this
    function wrongly flagged as aggregate purely because `SUM(` appears inside the `{FIXED}`
    block, promoting it to a MEASURE with a bare, unaggregated `'Dormancy'[l12m_spnd]` reference
    at measure top level. Confirmed live against Desktop via a Full table refresh (not just
    inferred from the TMDL text): "A single value for column 'l12m_spnd' in table 'Dormancy'
    cannot be determined" -- and because Analysis Services aborts a WHOLE table's refresh on any
    one calculated object's error, this did not just blank one card, it failed the ENTIRE
    `Recommendations` table's refresh (one more real-corpus instance of the exact same shape,
    `Rsn`-referencing) and, through it, every table blend-key-related to it.
    """
    stripped = _STRIP_STR.sub("", formula or "")
    stripped = _STRIP_BRACKETS.sub("", stripped)
    stripped = _STRIP_LOD.sub("", stripped)
    return bool(_AGG_CALL.search(stripped))


# Tableau's hand-rolled "zero-pad month/day into a yyyy-MM[-dd] label" idiom -- a real, recurring
# author pattern (6 occurrences in one workbook alone: Prevent Dashboard's "... w Dash" family)
# that exists only because Tableau's STR() has no zero-padding format argument. DAX's FORMAT()
# already zero-pads "MM"/"dd", so the entire IF/LEN/STR dance collapses to one FORMAT() call. The
# SAME captured `date` sub-expression must appear in every position (enforced by the backreference
# below) -- if it doesn't, this is not the idiom and the formula falls through to the normal
# parser. `date` itself may be any expression the recursive `translate()` call below can resolve,
# not just a bare field, EXCEPT one containing its own unbalanced parentheses (a real but rare
# limitation of the non-greedy `[^()]+?` capture -- such a formula simply does not match here and
# is declined by the normal parser instead, which is the correct fail-closed fallback).
_DATE_PAD_IDIOM_RE = re.compile(r"""
    ^\s*
    STR\s*\(\s*YEAR\s*\(\s*(?P<date>[^()]+?)\s*\)\s*\)
    \s*\+\s*"-"\s*\+\s*
    IF\s+LEN\s*\(\s*STR\s*\(\s*MONTH\s*\(\s*(?P=date)\s*\)\s*\)\s*\)\s*=\s*1\s*
    THEN\s*"0"\s*\+\s*STR\s*\(\s*MONTH\s*\(\s*(?P=date)\s*\)\s*\)\s*
    ELSE\s*STR\s*\(\s*MONTH\s*\(\s*(?P=date)\s*\)\s*\)\s*
    END
    (?P<day_part>
        \s*\+\s*"-"\s*\+\s*
        IF\s+LEN\s*\(\s*STR\s*\(\s*DAY\s*\(\s*(?P=date)\s*\)\s*\)\s*\)\s*=\s*1\s*
        THEN\s*"0"\s*\+\s*STR\s*\(\s*DAY\s*\(\s*(?P=date)\s*\)\s*\)\s*
        ELSE\s*STR\s*\(\s*DAY\s*\(\s*(?P=date)\s*\)\s*\)\s*
        END
    )?
    \s*$
""", re.X | re.I)

# Tableau's hand-rolled "abbreviated-month + apostrophe + 2-digit-year" period label idiom --
# e.g. "Jan'24" -- built by hand because Tableau's STR()/DATENAME() have no single format token
# for this exact shape. A real, recurring author pattern (ground truth: AP Product Portfolio
# Management Dashboard's "Analysis period from/to[N] [qoq/mom|yoy]" family, 12 occurrences across
# 2 tables) that was previously declined outright by STR()'s own generic "date nested inside
# another function" guard -- correctly conservative for an ARBITRARY nested-date expression, but
# needlessly so for this ONE closed, unambiguous shape: DAX's FORMAT() token "mmm" already
# produces the exact same 3-letter abbreviation LEFT(STR(DATENAME('month', d)), 3) builds by
# hand, and "yy" already produces the exact same 2-digit year RIGHT(STR(YEAR(d)), 2) builds by
# hand -- concatenating FORMAT(d,"mmm") & "'" & FORMAT(d,"yy") is not a guess, it is the same
# value built the short way, via two FORMAT() calls `&`-joined with the literal apostrophe
# (rather than one format string with an embedded apostrophe, which DAX's FORMAT() has no
# documented escape convention for).
#
# The SAME captured `date` sub-expression must appear in BOTH DATENAME('month', ...) and
# YEAR(...) (enforced by the backreference below) -- exactly the same "no invented agreement"
# discipline `_DATE_PAD_IDIOM_RE` above already applies. A formula stitching together month from
# one field and year from a DIFFERENT field is not this idiom and falls through to the normal
# parser (which declines it on its own, separate merits -- STR(DATENAME(...)) nested inside
# LEFT() is not itself a supported shape, so this is a real net gain, not a narrower version of
# an already-working path). A real corpus variant of this idiom appends a THIRD literal suffix
# after the year -- `+ " - "` -- to build a range-label PREFIX (e.g. "Jan'24 - ", the opening
# half of a "Jan'24 - Mar'24" period range). That trailing literal is entirely independent of
# the date-formatting logic above it (it is just Tableau text concatenation, already perfectly
# supported by the normal parser on its own), so it is captured here as an OPTIONAL group and
# re-appended to the collapsed FORMAT() output verbatim -- not re-derived, not guessed at, the
# exact same literal text the source formula already contains.
#
# The `date` capture matches EITHER exactly one whole `[...]`-delimited field reference (the
# overwhelmingly common real shape -- DATENAME's own 2nd argument is a single field, never a
# larger expression, in every real corpus instance found) OR a short paren-free run (the
# original, narrower pattern, kept as a fallback for a bare non-bracketed reference). Tableau's
# own "(copy)" renaming convention for a duplicated calculated field embeds a LITERAL paren pair
# INSIDE the bracketed field name itself (e.g. `[period from (copy)_1629458666403340289]`) --
# real corpus fields this idiom's own "date" sub-expression legitimately refers to (AP Product
# Portfolio Management Dashboard's "Analysis period from/to (copy)_..." family). The ORIGINAL
# blanket `[^()]+?` pattern stopped matching the instant it hit that inner `(copy)` text, silently
# declining a real, otherwise-supported instance of this idiom (8 of 12 real occurrences). Fixed
# here with a single, UNAMBIGUOUS alternative (`\[[^\]]*\]` matched whole, not looped char-by-
# char against a second alternative) -- deliberately NOT `(?:\[[^\]]*\]|[^()\[\]])+?`, whose
# unbounded alternation-in-a-loop followed by a backreference risks catastrophic backtracking on
# a long, ultimately-non-matching formula.
_MONTH_APOS_YEAR_IDIOM_RE = re.compile(r"""
    ^\s*
    LEFT\s*\(\s*STR\s*\(\s*DATENAME\s*\(\s*'month'\s*,\s*
        (?P<date>\[[^\]]*\]|[^()\[\]]+?)
    \s*\)\s*\)\s*,\s*3\s*\)
    \s*\+\s*"'"\s*\+\s*
    RIGHT\s*\(\s*STR\s*\(\s*YEAR\s*\(\s*(?P=date)\s*\)\s*\)\s*,\s*2\s*\)
    (?:\s*\+\s*(?P<suffix>"(?:[^"]|"")*"))?
    \s*$
""", re.X | re.I)


def translate(formula, resolve, resolve_param=None, resolve_type=None, resolve_kind=None,
              own_type=None, start_of_week=None, resolve_formula=None, _collapse_seen=None,
              tc_partition=None):
    """Translate a Tableau formula to DAX.

    resolve:       (tableau_field_name) -> DAX reference string, or None if unknown
    resolve_param: (tableau_param_name) -> DAX reference string, or None if unknown
    resolve_kind:  (tableau_field_name) -> "column" | "measure" | None. Lets an aggregate-function
                   call site (SUM/AVERAGE/.../DISTINCTCOUNT) tell a bare reference to an ordinary
                   column apart from a bare reference to a field that this build promoted to a
                   MEASURE -- the two render identically as 'Table'[Name] strings, so only the
                   resolver, not the parser, can tell them apart. See func_call's own comment on
                   this exact branch.
    own_type:      Tableau's own declared/inferred datatype (e.g. "date") for the CALC AS A WHOLE
                   that `formula` came from -- not a per-field lookup. Only DATE(<expr>)'s no-op-
                   cast branch uses it, as a last resort after every cheaper/more-local signal.
    start_of_week: the owning datasource's real start-of-week setting (a day name, e.g.
                   "sunday"), read from Tableau's own <date-options> XML by extract.py. Only
                   DATEPART('weekday',...)/DATETRUNC('week',...) use it, and only when the calc
                   itself did not supply an explicit 3rd start-of-week argument.
    resolve_formula: (tableau_field_name) -> that field's own raw Tableau formula, or None. Only
                   the promoted-aggregate collapse (see `Parser._collapse_promoted_aggregate`)
                   uses it, to recover from `SUM([x])` where `x` resolved to a measure instead of
                   always declining it.
    tc_partition:  the already-resolved DAX column reference for this calc's own extracted
                   "Restart every: <field>" table-calc partition, or None. Only RANK/RANK_DENSE
                   use it (see func_call).

    Returns (dax, referenced_fields, referenced_params).
    Raises Unsupported -- always with a reason usable as a residue entry.
    """
    if not (formula or "").strip():
        raise Unsupported("empty formula")
    idiom = _DATE_PAD_IDIOM_RE.match(formula.strip())
    if idiom:
        date_expr = idiom.group("date").strip()
        fmt = "yyyy-MM-dd" if idiom.group("day_part") else "yyyy-MM"
        d_dax, d_refs, d_params = translate(date_expr, resolve, resolve_param, resolve_type,
                                             resolve_kind, own_type, start_of_week,
                                             resolve_formula, _collapse_seen, tc_partition)
        return f'FORMAT({d_dax}, "{fmt}")', d_refs, d_params
    idiom2 = _MONTH_APOS_YEAR_IDIOM_RE.match(formula.strip())
    if idiom2:
        date_expr = idiom2.group("date").strip()
        d_dax, d_refs, d_params = translate(date_expr, resolve, resolve_param, resolve_type,
                                             resolve_kind, own_type, start_of_week,
                                             resolve_formula, _collapse_seen, tc_partition)
        # Two concatenated FORMAT() calls, not one format string with an embedded apostrophe --
        # DAX's FORMAT() has no documented escape convention for a literal char that could also
        # be misread as a custom-format token, so building the literal "'" as its own separate
        # `&`-joined piece (same as the Tableau source itself does) has zero ambiguity, unlike
        # guessing at an escape sequence would.
        collapsed = f'(FORMAT({d_dax}, "mmm") & "\'" & FORMAT({d_dax}, "yy"))'
        suffix = idiom2.group("suffix")
        if suffix is not None:
            # The trailing literal (e.g. `+ " - "`) is re-appended VERBATIM, exact same quoted
            # text the source formula already had -- not re-derived or reformatted, just carried
            # through unchanged via DAX's own `&` concatenation.
            collapsed = f"({collapsed} & {suffix})"
        return collapsed, d_refs, d_params
    p = Parser(lex(formula), resolve, resolve_param, resolve_type, resolve_kind, own_type,
               start_of_week, resolve_formula, _collapse_seen, tc_partition)
    dax = p.parse()
    return dax, sorted(p.refs), sorted(p.param_refs)


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    def r(f):
        return f"'T'[{f}]"

    def rp(n):
        # Mirrors tmdl.py's real resolve_param: the SELECTEDVALUE measure, never the raw
        # `[Value]` column -- a bare column reference at measure top-level is exactly what
        # produced "A single value for column 'Value'... cannot be determined" in Desktop
        # across 145 real emitted measures/columns before this was fixed.
        return f"[{n} Selected]"

    def rk(f):
        # Mirrors tmdl.py's real resolve_kind: everything is a plain column EXCEPT `promoted`,
        # which stands in for a real corpus field like Caribbean's `Gross Trx Amt Declined` --
        # a calc with no aggregate of its own that `_aggregate_map`'s fixpoint promoted to a DAX
        # MEASURE because something else (a parameter reference, or a downstream calc that itself
        # aggregates) forced it to be one. `resolve()`/`r()` above renders it identically to an
        # ordinary column ('T'[promoted]) -- only this callback can tell the difference.
        return "measure" if f == "promoted" else "column"

    cases = [
        ("SUM([Sales])", "SUM('T'[Sales])"),
        ("IF [a]='x' THEN [b] ELSE NULL END", None),
        ("[flag] = TRUE", None),
        ("[Parameters].[Show as]", "[Show as Selected]"),
        ("[a] + [b] * 2", None),
        # Tableau overloads `+` for string concatenation; DAX's `+` is always arithmetic and fails
        # at QUERY TIME (not load time) on text operands: "Cannot convert value '' of type Text to
        # type Numeric/Date". Confirmed on a real workbook (Prevent Dashboard):
        # `[cl.Slice By] + "|" + [cl.Slice By - Secondary]`, a label-building concatenation. A
        # string literal anywhere in the chain is the signal; a chain with none stays arithmetic
        # (this codebase has no reliable per-operand type signal to do better without threading a
        # type through the whole recursive descent -- see `add_expr`'s docstring).
        ('[a] + "|" + [b]', "'T'[a] & \"|\" & 'T'[b]"),
        ("[a] + [b]", "'T'[a] + 'T'[b]"),
        ('"x" + "y"', '"x" & "y"'),
        ("[a] / [b]", "DIVIDE('T'[a], 'T'[b])"),
        ("IF [x] > 1 THEN 'hi' ELSE 'lo' END", 'IF(\'T\'[x] > 1, "hi", "lo")'),
        ("IF [x]=1 THEN 'a' ELSEIF [x]=2 THEN 'b' ELSE 'c' END", None),
        # Tableau's optional second argument. Without the default, these emit DAX that fails at
        # model load, because DAX requires both arguments.
        ("FLOOR([x])", "FLOOR('T'[x], 1)"),
        ("CEILING([x])", "CEILING('T'[x], 1)"),
        ("ROUND([x])", "ROUND('T'[x], 0)"),
        ("ROUND([x], 2)", "ROUND('T'[x], 2)"),
        # Same name, same signature in both languages -- their absence from FUNCS declined 6
        # corpus calcs for no reason.
        ("LEFT([s], 3)", "LEFT('T'[s], 3)"),
        ("RIGHT([s], 2)", "RIGHT('T'[s], 2)"),
        # An aggregate over an EXPRESSION needs the DAX iterator variant. Emitting SUM(<expr>) is
        # invalid DAX and fails at model load.
        ("SUM([a] * [b])", "SUMX('T', 'T'[a] * 'T'[b])"),
        ("AVG([a] / [b])", "AVERAGEX('T', DIVIDE('T'[a], 'T'[b]))"),
        ("SUM([x])", "SUM('T'[x])"),
        ("CASE [r] WHEN 'N' THEN 1 WHEN 'S' THEN 2 ELSE 0 END", None),
        ("ZN([v])", "COALESCE('T'[v], 0)"),
        ("[a] != [b] AND NOT [c]", None),
        ("COUNTD([id])", "DISTINCTCOUNT('T'[id])"),
        ("[x] IN ('a','b')", None),
        # Bare `{expr}` -- no FIXED/INCLUDE/EXCLUDE -- is table-scope, the same degenerate
        # zero-key case as `{FIXED : expr}` below, just without the keyword. Real corpus shapes:
        # `{[End Date Calc]}`, `{DATE(IF ...)}`.
        ("{[x]}", "CALCULATE('T'[x], ALL('T'))"),
        ("{SUM([x])}", "CALCULATE(SUM('T'[x]), ALL('T'))"),
    ]
    print("--- should translate ---")
    for src, want in cases:
        try:
            got, _, _ = translate(src, r, rp, resolve_kind=rk)
            ok = "OK " if (want is None or got == want) else "DIFF"
            print(f"  {ok} {src}\n       -> {got}")
            if want and got != want:
                print(f"       expected {want}")
        except Unsupported as e:
            print(f"  FAIL {src}  -> Unsupported: {e}")

    print("--- should fail closed ---")
    for src in ("{FIXED [a]: SUM([b])}", "WINDOW_SUM(SUM([x]))",
                "SCRIPT_STR('x')", "STR([n])",
                "IF [a] = NULL THEN 1 ELSE 0 END",
                # STARTSWITH used to map to LEFT, producing LEFT(s, sub) -- a silently wrong
                # result, which is the one outcome this parser exists to prevent.
                "STARTSWITH([s],'a')", "ENDSWITH([s],'z')",
                # LTRIM/RTRIM used to map to TRIM, which trims BOTH ends.
                "LTRIM([s])", "RTRIM([s])",
                # SUM()/AVG()/etc of a BARE reference that resolve_kind says is a promoted
                # MEASURE, not a column -- exactly the Caribbean `Gross Trx Amt Decline Rate`
                # shape (SUM([Gross Trx Amt Declined]) where the referenced calc has no aggregate
                # of its own and was promoted to a measure by tmdl.py's `_aggregate_map`
                # fixpoint). `_BARE_COLUMN` alone cannot see this -- 'T'[promoted] is
                # textually identical whether `promoted` is a column or a measure -- so this MUST
                # come from resolve_kind, or it silently emits SUM(measure), which is invalid DAX
                # and fails at Desktop load with Missing_References.
                "SUM([promoted])", "AVG([promoted])", "COUNTD([promoted])"):
        try:
            got, _, _ = translate(src, r, rp, resolve_kind=rk)
            print(f"  LEAK {src} -> {got}   (should have been declined)")
        except Unsupported as e:
            print(f"  OK   {src}\n       declined: {e}")

    # A plain column with the SAME 'T'[promoted]-shaped string must still translate normally --
    # this backstop must not become a blanket ban on aggregating any bare column.
    got, _, _ = translate("SUM([x])", r, rp, resolve_kind=rk)
    assert got == "SUM('T'[x])", f"resolve_kind must not affect an ordinary column: got {got!r}"
    print("--- resolve_kind regression check OK: SUM(<ordinary column>) still translates ---")

    # ---- MID with 2 arguments ("to the end") -------------------------------------------------
    print("--- MID(s, start[, len]) ---")
    got, _, _ = translate("MID([s], 3, 5)", r)
    assert got == "MID('T'[s], 3, 5)", got
    print(f"  OK  MID([s], 3, 5) -- 3-arg is a straight pass-through -> {got}")
    got, _, _ = translate("MID([s], 3)", r)
    assert got == "MID('T'[s], 3, LEN('T'[s]))", got
    print(f"  OK  MID([s], 3) -- 2-arg means 'to the end' -> {got}")
    try:
        translate("MID([s])", r)
        raise AssertionError("expected Unsupported for MID with only 1 argument")
    except Unsupported:
        print("  OK  MID([s]) with only 1 argument declines")
    print("--- MID checks OK ---")

    # ---- SUM/AVG/MIN/MAX/MEDIAN/STDEV/VAR of a bare numeric literal ---------------------------
    print("--- aggregate of a bare numeric literal ---")
    got, _, _ = translate("SUM(0)", r)
    assert got == "0", got
    print(f"  OK  SUM(0) -> {got}  (0 * <any row count> is always 0 -- the one exact SUM case)")
    try:
        translate("SUM(5)", r)
        raise AssertionError("expected Unsupported for SUM(<nonzero literal>)")
    except Unsupported as e:
        assert "row count" in str(e), e
        print(f"  OK  SUM(5) still declined (depends on an unknown row count): {e}")
    got, _, _ = translate("AVG(5)", r)
    assert got == "5", got
    print(f"  OK  AVG(5) -> {got}  (mean of a repeated constant is that constant, any row count)")
    got, _, _ = translate("MIN(5)", r)
    assert got == "5", got
    got, _, _ = translate("MAX(5)", r)
    assert got == "5", got
    got, _, _ = translate("MEDIAN(5)", r)
    assert got == "5", got
    print("  OK  MIN(5)/MAX(5)/MEDIAN(5) -> 5 (same reasoning as AVG)")
    got, _, _ = translate("STDEV(5)", r)
    assert got == "0", got
    got, _, _ = translate("VAR(5)", r)
    assert got == "0", got
    print("  OK  STDEV(5)/VAR(5) -> 0 (zero variance in an identical repeated value)")
    try:
        translate("COUNT(5)", r)
        raise AssertionError("expected Unsupported for COUNT(<literal>) -- never collapses")
    except Unsupported:
        print("  OK  COUNT(5) still declined (COUNTs the row count, not the literal, always)")
    # A literal buried inside a LARGER expression (not the WHOLE argument) must be unaffected --
    # this only fires when args[0] AS A WHOLE is nothing but a bare numeric literal.
    got, _, _ = translate("SUM([x] + 5)", r)
    assert got == "SUMX('T', 'T'[x] + 5)", got
    print(f"  OK  SUM([x] + 5) (literal is part of a larger expression) unaffected -> {got}")
    print("--- aggregate-of-a-literal checks OK ---")

    # ---- COUNTD(<expression>) -> SELECTCOLUMNS/DISTINCT/COUNTROWS restructure -----------------
    print("--- COUNTD(<expression>) ---")
    got, _, _ = translate("COUNTD(IF [a] = 1 THEN [b] ELSE NULL END)", r, rp, resolve_kind=rk)
    assert got == ('COUNTROWS(DISTINCT(FILTER(SELECTCOLUMNS(\'T\', "v", '
                   'IF(\'T\'[a] = 1, \'T\'[b], BLANK())), NOT ISBLANK([v]))))'), got
    print(f"  OK  COUNTD(IF [a] = 1 THEN [b] ELSE NULL END) -> {got}")

    def r2(f):
        # Two DIFFERENT tables -- COUNTD(<expression>) spanning both has no single table to
        # project into SELECTCOLUMNS(), same constraint the AGG_ITERATOR branch already enforces.
        return f"'T2'[{f}]" if f == "other" else f"'T'[{f}]"

    try:
        translate("COUNTD([a] + [other])", r2)
        raise AssertionError("expected Unsupported for COUNTD(<expression over 2 tables>)")
    except Unsupported as e:
        assert "single table" in str(e), e
        print(f"  OK  COUNTD([a] + [other]) across 2 tables still declined: {e}")
    # A bare column (not an expression) must be completely unaffected by this restructure.
    got, _, _ = translate("COUNTD([id])", r)
    assert got == "DISTINCTCOUNT('T'[id])", got
    print(f"  OK  COUNTD([id]) (bare column) unaffected -> {got}")
    print("--- COUNTD(<expression>) checks OK ---")

    # ---- SPLIT(string, delimiter, tokenNumber) with a literal positive tokenNumber -----------
    print("--- SPLIT(s, delim, n) ---")
    got, _, _ = translate("SPLIT([s], ',', 1)", r)
    assert got == "LEFT('T'[s] & \",\", FIND(\",\", 'T'[s] & \",\") - 1)", got
    print(f"  OK  SPLIT([s], ',', 1) -> {got}")
    got, _, _ = translate("SPLIT([s], ',', 3)", r)
    assert got == (
        'LEFT(MID(MID(\'T\'[s] & ",", FIND(",", \'T\'[s] & ",") + LEN(","), 999999999) & ",", '
        'FIND(",", MID(\'T\'[s] & ",", FIND(",", \'T\'[s] & ",") + LEN(","), 999999999) & ",") '
        '+ LEN(","), 999999999) & ",", FIND(",", MID(MID(\'T\'[s] & ",", FIND(",", '
        '\'T\'[s] & ",") + LEN(","), 999999999) & ",", FIND(",", MID(\'T\'[s] & ",", '
        'FIND(",", \'T\'[s] & ",") + LEN(","), 999999999) & ",") + LEN(","), 999999999) '
        '& ",") - 1)'
    ), got
    print("  OK  SPLIT([s], ',', 3) unrolls 2 strip-a-token steps then a final LEFT")
    try:
        translate("SPLIT([s], ',', -1)", r)
        raise AssertionError("expected Unsupported for a negative (from-the-end) tokenNumber")
    except Unsupported as e:
        assert "not positive" in str(e), e
        print(f"  OK  SPLIT([s], ',', -1) declines (negative/from-the-end not implemented): {e}")
    try:
        translate("SPLIT([s], ',', [n])", r)
        raise AssertionError("expected Unsupported for a non-literal tokenNumber")
    except Unsupported as e:
        assert "literal integer" in str(e), e
        print(f"  OK  SPLIT([s], ',', [n]) declines (index not known at translation time): {e}")
    print("--- SPLIT checks OK ---")

    # ---- ATTR(<expression>) -> MINX/MAXX all-agree-or-"*" ------------------------------------
    print("--- ATTR(<expression>) ---")
    got, _, _ = translate("ATTR([x])", r)
    assert got == 'SELECTEDVALUE(\'T\'[x], "*")', got
    print(f"  OK  ATTR([x]) (bare column) unaffected -> {got}")
    got, _, _ = translate("ATTR([a] + [b])", r)
    assert got == ('IF(MINX(\'T\', \'T\'[a] + \'T\'[b]) = MAXX(\'T\', \'T\'[a] + \'T\'[b]), '
                   'MINX(\'T\', \'T\'[a] + \'T\'[b]), "*")'), got
    print(f"  OK  ATTR([a] + [b]) -> {got}")
    try:
        translate("ATTR([a] + [other])", r2)
        raise AssertionError("expected Unsupported for ATTR(<expression over 2 tables>)")
    except Unsupported as e:
        assert "single table" in str(e), e
        print(f"  OK  ATTR([a] + [other]) across 2 tables still declined: {e}")
    print("--- ATTR(<expression>) checks OK ---")

    print("--- is_aggregate: true positives (real function call) ---")
    agg_true = [
        "SUM([x])",
        "AVG([a] / [b])",
        "COUNTD([id])",
        "[a] + SUM([b])",
        "TOTAL([x])",
    ]
    for f in agg_true:
        ok = is_aggregate(f)
        print(f"  {'OK ' if ok else 'FAIL'} is_aggregate({f!r}) -> {ok}  (want True)")
        assert ok, f"expected aggregate: {f!r}"

    print("--- is_aggregate: false positives from bracketed field NAMES (the NGPO bug) ---")
    # Real corpus case: `[issuer value]*[Card Count (copy)_5832583774982094848]` -- the
    # substring "Count (" inside the bracketed field NAME must not register as a COUNT(...) call.
    agg_false = [
        "[issuer value]*[Card Count (copy)_5832583774982094848]",
        "[Card Count (copy)]",
        "[x]*[Count (copy)]",
        "[Sum (2)] + [y]",
        "[Average (USD)]",
        "[Total (2019)] - [Total (2018)]",
    ]
    for f in agg_false:
        ok = is_aggregate(f)
        print(f"  {'OK ' if not ok else 'FAIL'} is_aggregate({f!r}) -> {ok}  (want False)")
        assert not ok, f"expected NOT aggregate (field-name false positive): {f!r}"

    print("--- is_aggregate: false positives from an aggregate call INSIDE a {FIXED} LOD block "
          "(the GCLM Dor_Contri_tot_vol bug) ---")
    # Real corpus case: a bare, row-level reference divided by a {FIXED ...} LOD -- the LITERAL
    # `SUM(` sitting inside the LOD block must not make the OUTER formula register as aggregate;
    # Tableau's LOD already materializes one value per row at its own grain and hands that back
    # to row level, so the formula AS A WHOLE is still row-level (a calculated column), the same
    # as `_collapse_promoted_aggregate`'s own `is_lod` case one level up but for the FORMULA
    # itself here, not a field it references.
    lod_false = [
        "[l12m_spnd]/ {FIXED [prod_typ],[for_view],[ctry],[prod_pltfrm],[issr] : SUM([l12m_spnd])}",
        "{FIXED [k]: SUM([x])}",
        "[a] - {INCLUDE [k]: AVG([b])}",
    ]
    for f in lod_false:
        ok = is_aggregate(f)
        print(f"  {'OK ' if not ok else 'FAIL'} is_aggregate({f!r}) -> {ok}  (want False)")
        assert not ok, f"expected NOT aggregate (LOD-internal aggregate false positive): {f!r}"

    # An aggregate call OUTSIDE any {...} block, in the SAME formula as an LOD, must still count
    # -- stripping the LOD block must not blind this to a real, separate outer aggregate.
    lod_mixed_true = "{FIXED [k]: SUM([x])} + SUM([y])"
    ok = is_aggregate(lod_mixed_true)
    print(f"  {'OK ' if ok else 'FAIL'} is_aggregate({lod_mixed_true!r}) -> {ok}  (want True)")
    assert ok, "a real aggregate call outside the LOD block must still register"

    print("--- is_aggregate regression checks OK ---")

    # ---- Tableau date literals (#...#) and ASCII/CHAR ---------------------------------------
    # `#` had no token at all, so the `other` catch-all rejected the WHOLE formula with
    # "unexpected character '#'" -- one literal declining an otherwise fully translatable calc
    # (real instance: Prevent Dashboard's `Originated From Data Center`).
    print("--- date literals + ASCII ---")
    _lit_cases = [
        ("[d] >= #2022-07-01#", "'T'[d] >= DATE(2022, 7, 1)"),
        ("#2022-07-01 13:45:00#", "(DATE(2022, 7, 1) + TIME(13, 45, 0))"),
        ("#2022-7-1#", "DATE(2022, 7, 1)"),
        ("ASCII([c])", "UNICODE('T'[c])"),
        ("CHAR(65)", "UNICHAR(65)"),
    ]
    for src, want in _lit_cases:
        got, _r, _p = translate(src, r)
        ok = got == want
        print(f"  {'OK ' if ok else 'FAIL'} {src!r} -> {got!r}")
        assert ok, f"{src!r}: got {got!r}, want {want!r}"

    # A non-ISO literal must DECLINE, never coerce: DAX's text-to-date conversion is
    # locale-dependent, so "01/07/2022" is 1 July or 7 January depending on the model.
    for bad_lit in ("#July 1, 2022#", "#01/07/2022#"):
        try:
            translate(bad_lit, r)
            raise AssertionError(f"expected Unsupported for ambiguous literal {bad_lit!r}")
        except Unsupported:
            print(f"  OK  ambiguous literal {bad_lit!r} declined rather than guessed")

    print("--- date literal + ASCII checks OK ---")

    # ---- DATEPARSE: offsets computed from the format, locale-independent ----------------------
    print("--- DATEPARSE ---")
    # Every expected DAX below is wrapped in IFERROR(..., BLANK()) -- Tableau's own DATEPARSE
    # returns NULL (not an error) for a source string that is blank, too short, or non-numeric
    # at a numeric token's position; DAX's VALUE() hard-errors on exactly that input instead, and
    # a hard error here fails the WHOLE COLUMN's load, not just one cell. See the handler's own
    # comment for the real corpus regression this closes.
    _dp = [
        ("DATEPARSE('yyyy-MM-dd',[s])",
         "IFERROR(DATE(VALUE(MID('T'[s], 1, 4)), VALUE(MID('T'[s], 6, 2)), "
         "VALUE(MID('T'[s], 9, 2))), BLANK())"),
        ("DATEPARSE('yyyyMMdd',[s])",
         "IFERROR(DATE(VALUE(MID('T'[s], 1, 4)), VALUE(MID('T'[s], 5, 2)), "
         "VALUE(MID('T'[s], 7, 2))), BLANK())"),
        # Day-first vs month-first must NOT collapse to the same answer -- that is the exact
        # locale ambiguity building from offsets exists to remove.
        ("DATEPARSE('MM/dd/yyyy',[s])",
         "IFERROR(DATE(VALUE(MID('T'[s], 7, 4)), VALUE(MID('T'[s], 1, 2)), "
         "VALUE(MID('T'[s], 4, 2))), BLANK())"),
        ("DATEPARSE('dd/MM/yyyy',[s])",
         "IFERROR(DATE(VALUE(MID('T'[s], 7, 4)), VALUE(MID('T'[s], 4, 2)), "
         "VALUE(MID('T'[s], 1, 2))), BLANK())"),
        # Day-of-month OMITTED from the format: per Tableau's own DATEPARSE documentation, a
        # missing component defaults to its minimum (day -> 1), same as DAX's own DATE()
        # constructor -- so these two now resolve rather than decline.
        ("DATEPARSE('yyyy-MM',[s])",
         "IFERROR(DATE(VALUE(MID('T'[s], 1, 4)), VALUE(MID('T'[s], 6, 2)), 1), BLANK())"),
        ("DATEPARSE('yyyyMM',[s])",
         "IFERROR(DATE(VALUE(MID('T'[s], 1, 4)), VALUE(MID('T'[s], 5, 2)), 1), BLANK())"),
    ]
    for src, want in _dp:
        got, _r, _p = translate(src, r)
        ok = got == want
        print(f"  {'OK ' if ok else 'FAIL'} {src}")
        assert ok, f"{src}: got {got!r}, want {want!r}"

    _got, _r, _p = translate("DATEPARSE('yyyy-MM-dd HH:mm:ss',[s])", r)
    assert _got.startswith("IFERROR((DATE(") and "TIME(" in _got and _got.endswith(", BLANK())"), _got
    print("  OK  a time-bearing format adds TIME(), still IFERROR-wrapped")

    # Fail closed: a variable-width token has no fixed offset, and a format missing year OR month
    # (unlike day, above) still has no safe default to invent.
    for _bad in ("DATEPARSE('M/d/yyyy',[s])", "DATEPARSE('MM/dd',[s])"):
        try:
            translate(_bad, r)
            raise AssertionError(f"expected Unsupported for {_bad!r}")
        except Unsupported:
            print(f"  OK  {_bad} declined rather than guessed")

    print("--- DATEPARSE checks OK ---")

    # ---- DATEPARSE: 'MMM' (3-letter month name) / 'yy' (2-digit year) / '' escaped apostrophe --
    # Ground truth: AP Product Portfolio Management Dashboard's `Opcert Q4` -- previously declined
    # as "variable-width token at M''yy" because the tokenizer had no entry for MMM/yy at all and
    # misread the literal '' as the start of a 2-more-token run.
    print("--- DATEPARSE: MMM/yy/escaped-apostrophe ---")
    got, _r, _p = translate("DATEPARSE(\"MMM''yy\",[s])", r)
    want = ('IFERROR(DATE((2000 + VALUE(MID(\'T\'[s], 5, 2))), '
            'SWITCH(MID(\'T\'[s], 1, 3), "Jan", 1, "Feb", 2, "Mar", 3, "Apr", 4, "May", 5, '
            '"Jun", 6, "Jul", 7, "Aug", 8, "Sep", 9, "Oct", 10, "Nov", 11, "Dec", 12), 1), BLANK())')
    assert got == want, (got, want)
    print(f"  OK  DATEPARSE(\"MMM''yy\",[s]) -> {got}")

    # 'yy' alone (no MMM) still resolves via the same 2000+yy assumption.
    got, _r, _p = translate("DATEPARSE('MM/yy',[s])", r)
    assert got == ("IFERROR(DATE((2000 + VALUE(MID('T'[s], 4, 2))), "
                   "VALUE(MID('T'[s], 1, 2)), 1), BLANK())"), got
    print(f"  OK  DATEPARSE('MM/yy',[s]) -> {got}")

    # Ambiguous double-year/double-month formats must still decline, not guess which token wins.
    for _bad in ("DATEPARSE('yyyy/yy',[s])", "DATEPARSE('MMM/MM',[s])"):
        try:
            translate(_bad, r)
            raise AssertionError(f"expected Unsupported for {_bad!r}")
        except Unsupported:
            print(f"  OK  {_bad} declined rather than guessed (ambiguous duplicate token)")
    print("--- DATEPARSE: MMM/yy/escaped-apostrophe checks OK ---")

    # ---- DATE(expr) cast: no-op on an already date-typed field, DATEVALUE() on text -----------
    print("--- DATE() cast ---")

    def rt(field):
        return {"d": "dateTime", "s": "string"}.get(field)

    got, _r, _p = translate("DATE([d])", r, resolve_type=rt)
    assert got == "'T'[d]", got
    print("  OK  DATE([d]) on a dateTime field -> no-op passthrough")

    got, _r, _p = translate("DATE([s])", r, resolve_type=rt)
    assert got == "DATEVALUE('T'[s])", got
    print("  OK  DATE([s]) on a string field -> DATEVALUE()")

    try:
        translate("DATE([d])", r)          # no resolve_type at all -- type genuinely unknown
        raise AssertionError("expected Unsupported for DATE([d]) with no resolve_type")
    except Unsupported:
        print("  OK  DATE([d]) with unresolved type declined rather than guessed")

    try:
        translate("DATE(YEAR([d]))", r, resolve_type=rt)   # nested call, not a bare field
        raise AssertionError("expected Unsupported for DATE(YEAR([d]))")
    except Unsupported:
        print("  OK  DATE(<nested call>) declined rather than trusting last_field's type")

    print("--- DATE() cast checks OK ---")

    # ---- DATE("<iso literal>"): read y/m/d straight out of the text, no type needed -----------
    print("--- DATE() ISO string literal ---")
    got, _r, _p = translate('DATE("2025-05-01")', r)
    assert got == "DATE(2025, 5, 1)", got
    print('  OK  DATE("2025-05-01") -> DATE(2025, 5, 1), no resolve_type needed')
    try:
        translate('DATE("5/1/2025")', r)
        raise AssertionError("expected Unsupported for an ambiguous non-ISO literal")
    except Unsupported:
        print("  OK  DATE(\"5/1/2025\") (ambiguous, non-ISO) declined rather than guessed")
    print("--- DATE() ISO string literal checks OK ---")

    # ---- DATEADD('unit', n, date): scalar shift, not DAX's table-function DATEADD -------------
    print("--- DATEADD ---")
    _da_cases = [
        ("DATEADD('day', -1, [d])", "('T'[d] + -1)"),
        ("DATEADD('week', 2, [d])", "('T'[d] + 7 * (2))"),
        ("DATEADD('month', 1, [d])", "EDATE('T'[d], 1)"),
        ("DATEADD('quarter', -1, [d])", "EDATE('T'[d], 3 * (-1))"),
        ("DATEADD('year', 1, [d])", "EDATE('T'[d], 12 * (1))"),
    ]
    for src, want in _da_cases:
        got, _r, _p = translate(src, r)
        ok = got == want
        print(f"  {'OK ' if ok else 'FAIL'} {src}")
        assert ok, f"{src}: got {got!r}, want {want!r}"
    try:
        translate("DATEADD('hour', 1, [d])", r)
        raise AssertionError("expected Unsupported for DATEADD('hour', ...)")
    except Unsupported:
        print("  OK  DATEADD('hour', ...) declined rather than guessed")
    print("--- DATEADD checks OK ---")

    # ---- DATETRUNC('unit', date): exact per-unit rebuild, no single DAX function needed -------
    print("--- DATETRUNC ---")
    _dt_cases = [
        ("DATETRUNC('year',[d])", "DATE(YEAR('T'[d]), 1, 1)"),
        ("DATETRUNC('quarter',[d])", "DATE(YEAR('T'[d]), 3 * QUARTER('T'[d]) - 2, 1)"),
        ("DATETRUNC('month',[d])", "DATE(YEAR('T'[d]), MONTH('T'[d]), 1)"),
        ("DATETRUNC('week',[d])",
         "('T'[d] - (MOD(WEEKDAY('T'[d], 2) - 1 - 6, 7) + 1) + 1)"),
        ("DATETRUNC('week',[d],'monday')",
         "('T'[d] - (MOD(WEEKDAY('T'[d], 2) - 1 - 0, 7) + 1) + 1)"),
        ("DATETRUNC('day',[d])", "'T'[d]"),
    ]
    for src, want in _dt_cases:
        got, _r, _p = translate(src, r)
        ok = got == want
        print(f"  {'OK ' if ok else 'FAIL'} {src}")
        assert ok, f"{src}: got {got!r}, want {want!r}"

    for _bad in ("DATETRUNC('hour',[d])", "DATETRUNC('day',[d],1,2)",
                 "DATETRUNC('month',[d],'monday')", "DATETRUNC('week',[d],'noday')"):
        try:
            translate(_bad, r)
            raise AssertionError(f"expected Unsupported for {_bad!r}")
        except Unsupported:
            print(f"  OK  {_bad} declined rather than guessed")

    print("--- DATETRUNC checks OK ---")

    # ---- DATEDIFF('unit', a, b): direct DAX DATEDIFF(a, b, UNIT) for every real unit ----------
    print("--- DATEDIFF ---")
    _dd_cases = [
        ("DATEDIFF('day',[a],[b])", "DATEDIFF('T'[a], 'T'[b], DAY)"),
        ("DATEDIFF('year',[a],[b])", "DATEDIFF('T'[a], 'T'[b], YEAR)"),
        ("DATEDIFF('hour',[a],[b])", "DATEDIFF('T'[a], 'T'[b], HOUR)"),
        ("DATEDIFF('week',[a],[b])", "DATEDIFF('T'[a], 'T'[b], WEEK)"),
    ]
    for src, want in _dd_cases:
        got, _r, _p = translate(src, r)
        ok = got == want
        print(f"  {'OK ' if ok else 'FAIL'} {src}")
        assert ok, f"{src}: got {got!r}, want {want!r}"
    try:
        translate("DATEDIFF('fortnight',[a],[b])", r)
        raise AssertionError("expected Unsupported for an unrecognised DATEDIFF unit")
    except Unsupported:
        print("  OK  DATEDIFF('fortnight', ...) declined rather than guessed")
    print("--- DATEDIFF checks OK ---")

    # ---- DATEPART('unit', date): direct DAX scalar function for the unambiguous units ---------
    print("--- DATEPART ---")
    _dp2_cases = [
        ("DATEPART('year',[d])", "YEAR('T'[d])"),
        ("DATEPART('month',[d])", "MONTH('T'[d])"),
        ("DATEPART('hour',[d])", "HOUR('T'[d])"),
        ("DATEPART('weekday',[d])", "MOD(WEEKDAY('T'[d], 2) - 1 - 6, 7) + 1"),
        ("DATEPART('weekday',[d],'monday')", "MOD(WEEKDAY('T'[d], 2) - 1 - 0, 7) + 1"),
    ]
    for src, want in _dp2_cases:
        got, _r, _p = translate(src, r)
        ok = got == want
        print(f"  {'OK ' if ok else 'FAIL'} {src}")
        assert ok, f"{src}: got {got!r}, want {want!r}"
    for _bad in ("DATEPART('week',[d])", "DATEPART('dayofyear',[d])",
                 "DATEPART('day',[d],1)", "DATEPART('month',[d],'monday')",
                 "DATEPART('weekday',[d],'noday')"):
        try:
            translate(_bad, r)
            raise AssertionError(f"expected Unsupported for {_bad!r}")
        except Unsupported:
            print(f"  OK  {_bad} declined rather than guessed")
    print("--- DATEPART checks OK ---")

    # ---- start-of-week: threaded default vs. an explicit per-call override, ground-truthed ----
    # against Python's real datetime.date.weekday() for all 7 possible start days -- not
    # hand-derived, per this fix's own standard.
    print("--- start-of-week ground truth ---")
    import datetime
    _PY_MONDAY_OFFSET = {  # matches WEEKDAY_START_OFFSET, kept separate so a typo in one
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    _sample_dates = [datetime.date(2026, 9, d) for d in range(1, 15)]  # spans a full week+
    for start_day, start_offset in _PY_MONDAY_OFFSET.items():
        for dt in _sample_dates:
            py_monday_based = dt.weekday()  # 0=Monday..6=Sunday, Python's own ground truth
            want_weekday_num = (py_monday_based - start_offset) % 7 + 1
            # Emulate the emitted DAX arithmetic directly in Python with the same operator
            # semantics (DAX's WEEKDAY(date,2) == py_monday_based + 1; MOD is non-negative,
            # matching Python's `%` for a positive modulus).
            dax_weekday2 = py_monday_based + 1
            got_weekday_num = (dax_weekday2 - 1 - start_offset) % 7 + 1
            assert got_weekday_num == want_weekday_num, (
                f"{start_day} on {dt}: formula gives {got_weekday_num}, "
                f"Python ground truth gives {want_weekday_num}")
    print("  OK  general MOD formula matches datetime.date.weekday() for all 7 start days "
          f"across {len(_sample_dates)} sample dates")

    # translate()'s start_of_week= kwarg actually changes the output (the datasource default,
    # not just an explicit per-call 3rd argument).
    got_sun, _r, _p = translate("DATEPART('weekday',[d])", r, start_of_week="sunday")
    got_mon, _r, _p = translate("DATEPART('weekday',[d])", r, start_of_week="monday")
    assert got_sun == "MOD(WEEKDAY('T'[d], 2) - 1 - 6, 7) + 1", got_sun
    assert got_mon == "MOD(WEEKDAY('T'[d], 2) - 1 - 0, 7) + 1", got_mon
    assert got_sun != got_mon
    print("  OK  translate(start_of_week=...) changes DATEPART('weekday',...) as expected")

    # An explicit 3rd argument on the CALL overrides the datasource's threaded default.
    got_override, _r, _p = translate(
        "DATEPART('weekday',[d],'monday')", r, start_of_week="sunday")
    assert got_override == got_mon, got_override
    print("  OK  an explicit start-of-week argument overrides the datasource default")

    # DATENAME('weekday', date) is start-of-week INVARIANT (it names the calendar day, not a
    # number), so an explicit 3rd argument must not change its output, and must not decline.
    got_dn2, _r, _p = translate("DATENAME('weekday',[d])", r)
    got_dn3, _r, _p = translate("DATENAME('weekday',[d],'monday')", r)
    assert got_dn2 == got_dn3 == 'FORMAT(\'T\'[d], "dddd")', (got_dn2, got_dn3)
    print("  OK  DATENAME('weekday', date, <any start>) is invariant, as expected")
    print("--- start-of-week ground truth checks OK ---")

    # ---- MIN/MAX of an already-promoted MEASURE: needs the iterator form -----------------------
    print("--- MIN/MAX of a promoted measure ---")
    got, _r, _p = translate("MIN([promoted])", r, resolve_kind=rk)
    assert got == "MINX('T', 'T'[promoted])", got
    print(f"  OK  MIN([promoted]) -> {got}")
    got, _r, _p = translate("MAX([promoted])", r, resolve_kind=rk)
    assert got == "MAXX('T', 'T'[promoted])", got
    print(f"  OK  MAX([promoted]) -> {got}")
    # SUM/AVG/COUNTD of a promoted measure must still decline -- only MIN/MAX are exact here.
    for _bad in ("SUM([promoted])", "AVG([promoted])", "COUNTD([promoted])"):
        try:
            translate(_bad, r, resolve_kind=rk)
            raise AssertionError(f"expected Unsupported for {_bad!r}")
        except Unsupported:
            print(f"  OK  {_bad} still declined (only MIN/MAX are exact, not SUM/AVG/COUNTD)")
    print("--- MIN/MAX of a promoted measure checks OK ---")

    # ---- SUM/AVG/etc. of a promoted measure: collapse via resolve_formula ----------------------
    print("--- promoted-aggregate collapse (Caribbean - Drilldown (QBR) ground truth) ---")

    def rk2(f):
        return "measure" if f in ("promoted", "rowlevel_promoted", "cyclic") else "column"

    # (a) Ground truth shape: the promoted field has NO aggregate of its own -- it only became a
    # measure through parameter-reference propagation several hops upstream (Caribbean's
    # `Gross Trx Amt Approved`, an IF with no SUM anywhere, promoted only because it transitively
    # references a calc reading `[Parameters].[...]`). Collapsing means iterating its own
    # row-level translation with the DAX function this call site needs.
    def rf_rowlevel(f):
        return "IF [flag] = 'y' THEN [x] ELSE 0 END" if f == "rowlevel_promoted" else None

    got, _r, _p = translate("SUM([rowlevel_promoted])", r, resolve_kind=rk2,
                             resolve_formula=rf_rowlevel)
    assert got == "SUMX('T', IF('T'[flag] = \"y\", 'T'[x], 0))", got
    print(f"  OK  SUM([rowlevel_promoted]) -> {got}")

    # (b) The promoted field IS itself a full aggregate (e.g. SUM(<raw column>)) -- the SAME outer
    # function re-wrapping it is a no-op; use the inner's own translation directly rather than
    # nesting SUM(SUM(...)), which is not even valid DAX.
    def rf_same_agg(f):
        return "SUM([x])" if f == "promoted" else None

    got, _r, _p = translate("SUM([promoted])", r, resolve_kind=rk2, resolve_formula=rf_same_agg)
    assert got == "SUM('T'[x])", got
    print(f"  OK  SUM([promoted]) where promoted=SUM([x]) -> {got} (flat collapse, no re-wrap)")

    # (c) A DIFFERENT outer/inner function pair cannot be safely collapsed (no way to re-derive the
    # correct grain from here) -- stays declined, with the ORIGINAL reason text unchanged.
    try:
        translate("AVG([promoted])", r, resolve_kind=rk2, resolve_formula=rf_same_agg)
        raise AssertionError("expected Unsupported for AVG(<inner is SUM, not AVERAGE>)")
    except Unsupported as e:
        assert "promoted to a DAX MEASURE" in str(e), e
        print("  OK  AVG([promoted]) where promoted=SUM([x]) still declined (mismatched function)")

    # (d) No resolve_formula hook at all (the caller never wired one up) -- must decline exactly as
    # before this fix, not raise a different error.
    got_declined = False
    try:
        translate("SUM([promoted])", r, resolve_kind=rk2)
    except Unsupported:
        got_declined = True
    assert got_declined, "SUM([promoted]) must still decline when no resolve_formula is given"
    print("  OK  SUM([promoted]) with no resolve_formula hook still declines (unchanged fallback)")

    # (e) A genuine reference cycle in the substitution chain (not caught by tmdl.py's own
    # same-table `_find_cycles`, since that check never runs in this unit test) must decline
    # instead of recursing forever.
    def rf_cyclic(f):
        return "SUM([cyclic])" if f == "cyclic" else None

    try:
        translate("SUM([cyclic])", r, resolve_kind=rk2, resolve_formula=rf_cyclic)
        raise AssertionError("expected Unsupported for a self-referencing collapse chain")
    except Unsupported:
        print("  OK  a cyclic collapse chain declines instead of recursing forever")

    # (f) An ordinary aggregate calc with no such nesting at all must be completely unaffected.
    got, _r, _p = translate("SUM([Sales])", r, resolve_kind=rk2, resolve_formula=rf_same_agg)
    assert got == "SUM('T'[Sales])", got
    print("  OK  an ordinary SUM([column]) with no promoted reference is unaffected")

    # (g) Regression guard: COUNTD (-> DISTINCTCOUNT) of a promoted measure must still decline
    # cleanly, not raise KeyError. DISTINCTCOUNT has no AGG_ITERATOR entry at all (see
    # NO_ITERATOR's own comment -- counting distinct values of an expression needs a genuine
    # restructure, not a substitution), so the row-level-iterate sub-case must never be attempted
    # for it. Confirmed on a real workbook (Digital Sales - Leads Dashboard): reached this exact
    # KeyError('DISTINCTCOUNT') the first time this collapse was wired up without this guard.
    try:
        translate("COUNTD([rowlevel_promoted])", r, resolve_kind=rk2, resolve_formula=rf_rowlevel)
        raise AssertionError("expected Unsupported for COUNTD(<promoted, row-level inner>)")
    except Unsupported as e:
        assert "promoted to a DAX MEASURE" in str(e), e
        print("  OK  COUNTD([rowlevel_promoted]) declines cleanly (no DISTINCTCOUNT iterator "
              "exists to collapse into)")

    # (h) The promoted field is itself an LOD expression (`{FIXED [k]: sum([x])}`) -- ground truth:
    # NGPO Dashboard New - AP Version (MSC), 17 fields. `is_aggregate()` sees the literal `sum(`
    # inside the braces and would otherwise misfire into the flat-collapse branch (b), which can
    # never match since an LOD's own translation is `CALCULATE(...)`, never a bare `SUM(...)` --
    # so before this fix, every one of these 17 fields declined. The correct treatment is the
    # SAME row-level-iterate path as (a): wrap the LOD's own `CALCULATE(...)` translation in the
    # outer call's X-iterator.
    def rk3(f):
        return "measure" if f == "lod_promoted" else "column"

    def rf_lod(f):
        return "{FIXED [k]: sum([x])}" if f == "lod_promoted" else None

    got, _r, _p = translate("SUM([lod_promoted])", r, resolve_kind=rk3, resolve_formula=rf_lod)
    assert got == "SUMX('T', CALCULATE(SUM('T'[x]), ALLEXCEPT('T', 'T'[k])))", got
    print(f"  OK  SUM([lod_promoted]) where lod_promoted={{FIXED [k]: sum([x])}} -> {got}")
    print("--- promoted-aggregate collapse checks OK ---")

    # ---- RANK/RANK_DENSE with a resolved table-calc partition ------------------------------
    print("--- RANK/RANK_DENSE with a resolved partition ---")
    # (a) Ground truth shape (NGPO Dashboard New - AP Version (MSC), `Rank - Topic Area`): a
    # resolved "Restart every: <field>" partition lets RANK/RANK_DENSE translate exactly via
    # RANKX's own native `ties` argument, instead of declining.
    got, _r, _p = translate("RANK(SUM([x]), 'desc')", r, tc_partition="'T'[Topic Area]")
    assert got == "RANKX(ALLEXCEPT('T', 'T'[Topic Area]), SUM('T'[x]), , DESC, Skip)", got
    print(f"  OK  RANK(SUM([x]), 'desc') with a resolved partition -> {got}")

    got, _r, _p = translate("RANK_DENSE([x])", r, tc_partition="'T'[Topic Area]")
    assert got == "RANKX(ALLEXCEPT('T', 'T'[Topic Area]), 'T'[x], , DESC, Dense)", got
    print(f"  OK  RANK_DENSE([x]) with no explicit order defaults to DESC, ties=Dense -> {got}")

    got, _r, _p = translate("RANK(SUM([x]), 'asc')", r, tc_partition="'T'[Topic Area]")
    assert got == "RANKX(ALLEXCEPT('T', 'T'[Topic Area]), SUM('T'[x]), , ASC, Skip)", got
    print(f"  OK  RANK(SUM([x]), 'asc') honours an explicit ascending order -> {got}")

    # (b) No resolved partition (the common case: extract.py found no `level-break`, or tmdl.py's
    # route() could not place it on this table) -- must still decline exactly as before this fix.
    got_declined = False
    try:
        translate("RANK(SUM([x]), 'desc')", r)
    except Unsupported as e:
        got_declined = "partition" in str(e)
        assert got_declined, e
    assert got_declined, "RANK(...) with no tc_partition must still decline"
    print("  OK  RANK(...) with no resolved partition still declines (unchanged fallback)")

    # (c) RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE are NOT in this branch at all -- even WITH a
    # resolved partition, they must decline (via the normal "unknown function" path, since they
    # were never in FUNCS to begin with), proving this fix did not widen past the two functions
    # RANKX's own `ties` argument can represent exactly.
    for fn in ("RANK_UNIQUE", "RANK_MODIFIED", "RANK_PERCENTILE"):
        try:
            translate(f"{fn}([x])", r, tc_partition="'T'[Topic Area]")
            raise AssertionError(f"expected {fn} to still decline even with a resolved partition")
        except Unsupported:
            pass
    print("  OK  RANK_UNIQUE/RANK_MODIFIED/RANK_PERCENTILE still decline even with a resolved "
          "partition (no exact DAX tie-break equivalent)")

    # (d) A malformed order argument declines rather than silently defaulting.
    try:
        translate("RANK(SUM([x]), 'sideways')", r, tc_partition="'T'[Topic Area]")
        raise AssertionError("expected Unsupported for an order argument that is not asc/desc")
    except Unsupported:
        print("  OK  RANK(...) with a non-asc/desc order argument declines")
    print("--- RANK/RANK_DENSE checks OK ---")

    # ---- MAKEDATE: identical signature to DAX DATE -----------------------------------------
    print("--- MAKEDATE ---")
    got, _r, _p = translate("MAKEDATE(2025, 3, 1)", r)
    assert got == "DATE(2025, 3, 1)", got
    print(f"  OK  MAKEDATE(2025, 3, 1) -> {got}")
    print("--- MAKEDATE checks OK ---")

    # ---- DATE(<expr>): redundant cast on an IF/ELSEIF cascade, and a parameter-derived string --
    # Reproduces the real shapes hand-fixed on a real corpus workbook (Prevent Dashboard):
    # `cl.Anchor Date` = DATE(IF ... THEN <bare field/param ref> ... ELSEIF ... THEN
    # DATE(<param> & "-01") ... END), whose own declared Tableau datatype is "date".
    print("--- DATE(<expr>) cast on an IF cascade + parameter-derived string ---")

    def rt2(field):
        return {"d": "dateTime"}.get(field)

    # The nested DATE(<param> + "-01") sub-call: its argument is a top-level text concatenation
    # (Tableau's `+` on a string operand, rendered as DAX `&`), so it must become DATEVALUE(...),
    # never a no-op drop -- even though the OUTER calc's own_type is "date" too, which is exactly
    # the case `_top_level_has_concat` exists to guard.
    got, _r, _p = translate('DATE([Parameters].[p] + "-01")', r, rp, resolve_type=rt2,
                             own_type="date")
    assert got == 'DATEVALUE([p Selected] & "-01")', got
    print(f"  OK  nested DATE(<param> + \"-01\") -> {got}")

    # The outer cascade: every branch is a bare field/param reference or the already-handled
    # nested DATEVALUE(...) call above -- none of them is a bare column of KNOWN date type (the
    # only case handled before this session's fix), so only trusting the calc's own declared
    # `own_type` (a fact relayed from Tableau's own type-checker for this exact formula, not
    # guessed here) lets this translate at all.
    _cascade = ("IF [x] = 1 THEN [a] ELSEIF [x] = 2 THEN [b] "
                'ELSEIF [x] = 3 THEN DATE([Parameters].[p] + "-01") ELSE NULL END')
    got, _r, _p = translate(f"DATE({_cascade})", r, rp, resolve_type=rt2, own_type="date")
    assert got.startswith("IF(") and "DATEVALUE([p Selected]" in got and got.endswith(")"), got
    print(f"  OK  DATE(<IF cascade, own_type=date>) drops the redundant outer cast -> {got}")

    # Without a known own_type, the SAME cascade must still decline -- own_type is the ONLY thing
    # that makes this safe, so its absence must not silently fall through to a guess.
    try:
        translate(f"DATE({_cascade})", r, rp, resolve_type=rt2)
        raise AssertionError("expected Unsupported when own_type is unknown")
    except Unsupported:
        print("  OK  same cascade with no own_type declined rather than guessed")

    # A bare-string literal argument must still take the DATEVALUE() path, not the own_type
    # fallback, even when own_type happens to be "date" -- literal-string evidence outranks the
    # calc-level fact.
    got, _r, _p = translate('DATE("2025-03-01")', r, own_type="date")
    assert got == "DATE(2025, 3, 1)", got
    print(f"  OK  DATE(\"<ISO literal>\") unaffected by own_type -> {got}")

    print("--- DATE(<expr>) cascade + own_type checks OK ---")

    # ---- DATE(MIN(DATEPARSE(...))): real corpus GCLM bug, no own_type needed at all -----------
    # The CONTAINING calc builds a text label (own_type "string"); this inner DATE(...) only
    # extracts a MIN date for DATENAME formatting elsewhere in the same formula. Before this fix,
    # own_type being "string" (not "date") meant NEITHER existing branch fired and this declined
    # outright, even though DATEPARSE(...) is already provably date-returning and MIN() of ONE
    # expression preserves that -- no own_type signal is needed to prove it.
    got, _r, _p = translate('DATE(MIN(DATEPARSE("yyyyMM", [mnth_yr])))', r)
    want = ("MINX('T', IFERROR(DATE(VALUE(MID('T'[mnth_yr], 1, 4)), "
            "VALUE(MID('T'[mnth_yr], 5, 2)), 1), BLANK()))")
    assert got == want, f"got {got!r}, want {want!r}"
    print(f"  OK  DATE(MIN(DATEPARSE(\"yyyyMM\", [mnth_yr]))) drops the redundant outer cast, "
          f"with NO own_type needed -> {got}")

    # MAX(...) gets the same treatment, and nesting (MIN(MAX(...))) recurses correctly.
    got, _r, _p = translate('DATE(MAX(DATEPARSE("yyyyMM", [mnth_yr])))', r)
    assert got.startswith("MAXX('T', IFERROR(DATE("), got
    print(f"  OK  DATE(MAX(DATEPARSE(...))) also drops the outer cast -> {got}")

    # The 2-argument Tableau MIN(a, b) ("smaller of two values") form is a DIFFERENT operation --
    # must NOT be mistaken for the single-expression aggregate and must still decline (no
    # own_type, no bare-field/concat signal either) rather than guess either argument's type.
    try:
        translate('DATE(MIN([a], [b]))', r)
        raise AssertionError("expected Unsupported for the 2-argument MIN(a, b) form")
    except Unsupported:
        print("  OK  DATE(MIN(a, b)) (2-argument form) still declined, not mistaken for the "
              "single-expression aggregate case")

    print("--- DATE(MIN/MAX(<date-returning call>)) checks OK ---")

    # ---- aggregate of a bare PARAMETER-measure reference: real corpus GCLM bug ------------------
    # `_BARE_MEASURE_REF` -- a Tableau parameter is a single workbook-wide scalar with no rows to
    # aggregate over at all, so AVG/MIN/MAX/MEDIAN/SUM of one are all a safe identity (unlike the
    # same aggregate of a genuine numeric LITERAL, where only AVG/MIN/MAX/MEDIAN collapse and SUM
    # stays row-count-dependent -- a parameter has no row count in the first place).
    print("--- aggregate of a bare parameter-measure reference ---")
    for fn, want in (("AVG", "[Show as Selected]"), ("MIN", "[Show as Selected]"),
                     ("MAX", "[Show as Selected]"), ("MEDIAN", "[Show as Selected]"),
                     ("SUM", "[Show as Selected]")):
        got, _r, _p = translate(f"{fn}([Parameters].[Show as])", r, rp)
        assert got == want, f"{fn}([Parameters].[Show as]) -> got {got!r}, want {want!r}"
        print(f"  OK  {fn}([Parameters].[Show as]) collapses to the bare measure reference, "
              f"no {{X}}-iterator wrapper -> {got}")
    got, _r, _p = translate("STDEV([Parameters].[Show as])", r, rp)
    assert got == "0", got
    print(f"  OK  STDEV([Parameters].[Show as]) -> 0 (zero variance in a repeated constant)")

    # No regression: AVG of a bare REAL column (not a parameter) must still take the normal
    # AVERAGEX path, not the new bare-measure collapse (`_BARE_MEASURE_REF` requires NO table
    # prefix at all -- a real column always has one).
    got, _r, _p = translate("AVG([x])", r)
    assert got == "AVERAGE('T'[x])", got
    print(f"  OK  AVG([x]) on a real column is unaffected (unwrapped to the plain aggregate by "
          f"the existing bare-column path, never reaches the bare-measure collapse) -> {got}")
    print("--- aggregate of a bare parameter-measure reference checks OK ---")

    # ---- Tableau's <TYPE>('NULL') idiom -> BLANK(): real corpus GCLM bug (FLOAT('NULL')) -------
    print("--- <TYPE>('NULL') typed-null idiom ---")
    for src in ('FLOAT(\'NULL\')', 'INT(\'NULL\')', 'STR(\'NULL\')', 'DATE(\'NULL\')'):
        got, _r, _p = translate(src, r)
        assert got == "BLANK()", f"{src} -> got {got!r}, want 'BLANK()'"
        print(f"  OK  {src} -> BLANK()")
    # Case-insensitive on the inner text, matching Tableau's own case-insensitive keyword parsing.
    got, _r, _p = translate("FLOAT('null')", r)
    assert got == "BLANK()", got
    print("  OK  FLOAT('null') (lowercase) -> BLANK() too")
    # No false positive: a function called with a DIFFERENT single string argument must be
    # completely unaffected -- only the exact text "NULL" triggers this.
    try:
        translate("FLOAT('abc')", r)
        raise AssertionError("expected Unsupported for FLOAT('abc') -- FLOAT has no real mapping")
    except Unsupported:
        print("  OK  FLOAT('abc') (not the NULL idiom) still declines as before, no false "
              "positive")
    print("--- <TYPE>('NULL') typed-null idiom checks OK ---")

    # ---- STR(<computed date-returning call>): the type-inference gap found on a real corpus
    # workbook (Prevent Dashboard, `cl.Rolling 7 Day End - Lookups`):
    # `STR(DATE(dateadd('day', 6, [x])))`, previously declined because `last_field`'s resolved
    # type (whatever field DATEADD's OWN argument happens to reference) is not a reliable proxy
    # for whether the whole wrapping call returns a date. `_is_date_returning_call` proves it
    # from the Tableau SOURCE shape instead: the argument, in its entirety, is a call to a
    # function (DATE/DATEADD/DATETRUNC/DATEPARSE) this translator already guarantees returns a
    # date, regardless of `last_field`.
    print("--- STR(<computed date-returning call>) ---")
    got, _r, _p = translate("STR(DATEADD('day', 6, [d]))", r, resolve_type=rt2)
    assert got == 'FORMAT((\'T\'[d] + 6), "yyyy-MM-dd")', got
    print(f"  OK  STR(DATEADD('day', 6, [d])) -> {got}")

    # The full three-layer round-trip idiom this surfaced from: DATE(DATEPARSE("yyyy-MM-dd",
    # STR(<date-returning-expr>))). Once the STR() argument is proven date-typed by the fix
    # above, DATEPARSE rebuilds it via fixed-width MID()/VALUE() extraction over that FORMAT()
    # text, and the outer single-arg DATE() cast drops as a redundant no-op (via `own_type`,
    # exactly like the bare-field-reference case already handled) -- correct DAX, even though
    # more verbose than skipping the round-trip. Asserted structurally, not by exact string:
    # the exact character offsets are already covered by DATEPARSE's own dedicated tests above.
    got, _r, _p = translate(
        'DATE(DATEPARSE("yyyy-MM-dd", STR(DATEADD(\'day\', 6, [d]))))',
        r, resolve_type=rt2, own_type="date")
    assert got.startswith("IFERROR(DATE(VALUE(MID(FORMAT(") and 'FORMAT((\'T\'[d] + 6)' in got, got
    assert "dateadd(" not in got and "datepart(" not in got and "DATEPARSE(" not in got, got
    print(f"  OK  DATE(DATEPARSE(\"yyyy-MM-dd\", STR(DATEADD(...)))) round-trip -> {got}")

    print("--- STR(<computed date-returning call>) checks OK ---")

    # ---- STR(<computed integer-returning call>): the mirror-image, SAFER category -------------
    # YEAR/MONTH/DAY/DATEDIFF/DATEPART all return a plain integer, never a date, so STR() of one
    # has zero locale/date-format ambiguity -- ground truth (Prevent Dashboard) `Date Range
    # Text` = `STR(DATEPART('day', [Date Range Start Date]))`. `last_field` for each of these
    # resolves to the date-typed field buried inside the call (e.g. `[d]`), which is exactly the
    # trap this fix avoids falling into: the OUTER call's return type, not the inner field's, is
    # what STR() actually receives.
    print("--- STR(<computed integer-returning call>) ---")
    for src, expect in [
        ("STR(YEAR([d]))", '(YEAR(\'T\'[d]) & "")'),
        ("STR(MONTH([d]))", '(MONTH(\'T\'[d]) & "")'),
        ("STR(DAY([d]))", '(DAY(\'T\'[d]) & "")'),
        ("STR(DATEDIFF('day',[d],[d]))", None),
        ("STR(DATEPART('day',[d]))", None),
    ]:
        got, _r, _p = translate(src, r, resolve_type=rt2)
        if expect is not None:
            assert got == expect, got
        print(f"  OK  {src} -> {got}")
    print("--- STR(<computed integer-returning call>) checks OK ---")

    # Regression guard: STR(LEN([d])) must still decline. LEN is a REAL, recognized function
    # (so this exercises STR()'s own decline logic, not just "unknown function name") that also
    # happens to return an integer in reality -- but it is deliberately NOT in
    # `_INTEGER_RETURNING_FUNCS`, so neither proof fires and this must fall through to the
    # pre-existing nested-inside-another-function decline. Proves the safe set is exactly the
    # 5 enumerated functions, not "any function I can guess also returns an integer".
    try:
        translate("STR(LEN([d]))", r, resolve_type=rt2)
        raise AssertionError("expected Unsupported for STR(LEN([d]))")
    except Unsupported:
        print("  OK  STR(LEN([d])) still declined -- LEN is not in _INTEGER_RETURNING_FUNCS")

    # ---- TOTAL(<agg-expr>): a pure no-op unwrap, not an addressing-needing table calc ----------
    # Ground truth (Prevent Dashboard): `TOTAL(MEDIAN([x]))` and `TOTAL([already-a-measure])`.
    print("--- TOTAL ---")
    got, _r, _p = translate("TOTAL(MEDIAN([x]))", r)
    assert got == "MEDIAN('T'[x])", got
    print(f"  OK  TOTAL(MEDIAN([x])) -> {got}")
    got, _r, _p = translate("TOTAL([promoted])", r, resolve_kind=rk)
    assert got == "'T'[promoted]", got
    print(f"  OK  TOTAL(<bare measure reference>) -> {got}")
    try:
        translate("TOTAL([a],[b])", r)
        raise AssertionError("expected Unsupported for TOTAL with 2 arguments")
    except Unsupported:
        print("  OK  TOTAL(<2 args>) declined rather than guessed")
    print("--- TOTAL checks OK ---")

    # ---- zero-pad date-format idiom: STR(YEAR)+"-"+IF-LEN-pad(MONTH)[+"-"+IF-LEN-pad(DAY)] -----
    # collapses to one FORMAT() call. Ground truth (Prevent Dashboard's "... w Dash" family, 6
    # occurrences: cl.Deflection/Lookup/CE Eligible/Deflection Settled/Deflection Result Month w
    # Dash for the month-only shape, cl.CE Eligible Date w Dash for the day-inclusive shape).
    print("--- zero-pad date-format idiom ---")
    _month_pad = ('STR(YEAR([d])) + "-" + \r\n'
                  'IF LEN(STR(MONTH([d]))) = 1\r\n'
                  'THEN\r\n    "0" + STR(MONTH([d]))\r\n'
                  'ELSE\r\n    STR(MONTH([d]))\r\n'
                  'END')
    got, _r, _p = translate(_month_pad, r)
    assert got == 'FORMAT(\'T\'[d], "yyyy-MM")', got
    print(f"  OK  month-only zero-pad idiom -> {got}")

    _day_pad = (_month_pad + '\r\n\r\n+ "-" + \r\n\r\n'
                'IF LEN(STR(DAY([d]))) = 1\r\nTHEN\r\n    "0" + STR(DAY([d]))\r\n'
                'ELSE\r\n    STR(DAY([d]))\r\nEND')
    got, _r, _p = translate(_day_pad, r)
    assert got == 'FORMAT(\'T\'[d], "yyyy-MM-dd")', got
    print(f"  OK  day-inclusive zero-pad idiom -> {got}")

    # The SAME date sub-expression must recur in every position -- a formula padding TWO
    # different fields is not this idiom and must not collapse to the single-field FORMAT()
    # shortcut (it is still a perfectly valid, separately-translatable formula on its own
    # merits -- STR/LEN/MONTH/YEAR are all independently supported -- so no exception is
    # expected here, only that the idiom-specific collapse did not fire).
    _mismatched = _month_pad.replace("STR(MONTH([d]))\r\nEND", "STR(MONTH([e]))\r\nEND", 1)
    got_mismatched, _r, _p = translate(_mismatched, r)
    assert got_mismatched != 'FORMAT(\'T\'[d], "yyyy-MM")', (
        "a formula padding two DIFFERENT fields must not collapse to one field's FORMAT()")
    print("  OK  mismatched-field variant does not match the idiom "
          f"(translated normally instead -> {got_mismatched})")

    # Negative/regression guard: a similar-looking but NOT-quite-the-idiom shape (missing the
    # zero-pad ELSE branch's own STR() wrapper is one real way authors vary this) must not
    # over-fire the matcher -- it should decline via the normal parser instead.
    _not_idiom = _month_pad.replace('"0" + STR(MONTH([d]))', '"00" + STR(MONTH([d]))')
    try:
        got_not_idiom, _r, _p = translate(_not_idiom, r)
    except Unsupported:
        got_not_idiom = None
    assert got_not_idiom != 'FORMAT(\'T\'[d], "yyyy-MM")', (
        "3-digit-pad variant must not be collapsed to the same 2-digit FORMAT() idiom")
    print("  OK  3-digit-pad variant does not over-fire the 2-digit idiom matcher")
    print("--- zero-pad date-format idiom checks OK ---")

    # ---- month-apostrophe-year period label idiom: LEFT(STR(DATENAME('month',d)),3)+"'"+ -------
    # RIGHT(STR(YEAR(d)),2) -- e.g. "Jan'24". Ground truth (AP Product Portfolio Management
    # Dashboard's "Analysis period from/to[N] [qoq/mom|yoy]" family, 12 occurrences).
    print("--- month-apostrophe-year period label idiom ---")
    _period_label = "left(str(DATENAME('month', [d])),3)+\"'\"+right(STR(YEAR([d])),2)"
    got, _r, _p = translate(_period_label, r)
    assert got == '(FORMAT(\'T\'[d], "mmm") & "\'" & FORMAT(\'T\'[d], "yy"))', got
    print(f"  OK  month-apostrophe-year idiom -> {got}")

    # Case/whitespace variation -- Tableau authors are inconsistent about LEFT/left, spacing, and
    # whether there is a space after the DATENAME comma; the idiom must still match.
    _period_label_variant = (
        'LEFT(STR(DATENAME(\'month\',[d])), 3) + "\'" + RIGHT(STR(YEAR([d])), 2)'
    )
    got_variant, _r, _p = translate(_period_label_variant, r)
    assert got_variant == got, (got_variant, got)
    print("  OK  case/whitespace variant matches the same idiom")

    # The SAME captured `date` sub-expression must appear in BOTH DATENAME and YEAR -- a formula
    # mixing month from one field and year from a DIFFERENT field is not this idiom.
    _mismatched_period = _period_label.replace("YEAR([d])", "YEAR([e])")
    try:
        got_mismatched_period, _r, _p = translate(_mismatched_period, r)
    except Unsupported:
        got_mismatched_period = None
    assert got_mismatched_period != got, (
        "a formula pulling month and year from two DIFFERENT fields must not collapse to the "
        "single-field FORMAT() shortcut")
    print("  OK  mismatched-field variant does not match the idiom")

    # Trailing literal suffix (` + " - "`), a real corpus variant that builds a range-label
    # PREFIX (e.g. "Jan'24 - ", the opening half of "Jan'24 - Mar'24"). Ground truth (AP Product
    # Portfolio Management Dashboard's `Analysis period from (copy)_...` family, 8 occurrences)
    # that did NOT match the base idiom above -- confirming the fix actually closes that gap.
    _period_label_suffix = _period_label + ' + " - "'
    got_suffix, _r, _p = translate(_period_label_suffix, r)
    assert got_suffix == f'({got} & " - ")', (got_suffix, got)
    print(f"  OK  trailing-suffix variant -> {got_suffix}")
    print("--- month-apostrophe-year period label idiom checks OK ---")

    # ---- block comments: `/* ... */`, not just `//` -------------------------------------------
    # Real corpus regression (10000056_achc_WF_Payments_Insights): an ordinary IF/THEN/ELSE/END
    # with a `/*...*/` comment sitting between THEN and the real expression declined the WHOLE
    # calc with "unexpected character '*'" -- the comment's own closing `*/` fell through to the
    # lexer's `other` catch-all, not the (`//`-only) comment stripper.
    print("--- block comments ---")
    _block_comment_real = (
        "If [Health Grp Nm 1] = 'frd' or [Health Grp Nm 1] = 'chbk'\n"
        "/*then [Amt Rate V2]*10000*/\n"
        "then [Amt Rate V2]*10000\n"
        "ELSE [Amt Rate V2]\n"
        "END"
    )
    got, _r, _p = translate(_block_comment_real, r)
    assert got == (
        'IF((\'T\'[Health Grp Nm 1] = "frd" || \'T\'[Health Grp Nm 1] = "chbk"), '
        "'T'[Amt Rate V2] * 10000, 'T'[Amt Rate V2])"
    ), got
    print(f"  OK  real corpus IF/THEN/ELSE with an inline /*...*/ comment -> {got}")

    # A comment spanning MULTIPLE lines (a real corpus formula, `total`, wraps a whole
    # multi-line `{ FIXED : ... }` block in one comment) -- `.` must span newlines.
    _block_comment_multiline = (
        "/*{ FIXED :\n"
        "  ZN(MAX([x]))\n"
        "}*/\n"
        "SUM([y])"
    )
    got, _r, _p = translate(_block_comment_multiline, r)
    assert got == "SUM('T'[y])", got
    print(f"  OK  multi-line /*...*/ comment stripped in full -> {got}")

    # TWO separate block comments in one formula must not collapse into one giant comment
    # spanning the real code between them (non-greedy `.*?`, not greedy `.*`).
    got, _r, _p = translate("/*a*/ [x] + /*b*/ [y]", r)
    assert got == "'T'[x] + 'T'[y]", got
    print(f"  OK  two separate /*...*/ comments do not swallow the code between them -> {got}")

    # A `//` line comment must still work (pre-existing behavior) alongside the new `/*...*/`
    # support -- both live in the same lexer alternative now.
    got, _r, _p = translate("[x] // trailing line comment\n+ [y]", r)
    assert got == "'T'[x] + 'T'[y]", got
    print(f"  OK  // line comment still stripped -> {got}")
    print("--- block comment checks OK ---")

    # ---- '+' as text concat, proven by a bare string-typed column, no literal anywhere --------
    # Real corpus regression: GCLM_EU_GERMANY_COMMERZBANK_AG_v2's `Seg_spnd_f6_concat` =
    # `[first6months] + CHAR(10) + [first_6_month_cutoff]` -- both operands plain string columns,
    # no string literal in the chain at all, so the OLD literal-only detection left this as a
    # plain DAX `+`, which loads fine (syntactically valid) and then errors at query time trying
    # to add two text values.
    print("--- '+' text-concat proof via resolve_type (no literal needed) ---")

    def rt3(field):
        return {"s1": "string", "s2": "string", "n1": "int64", "n2": "int64"}.get(field)

    got, _r, _p = translate("[s1] + [s2]", r, resolve_type=rt3)
    assert got == "'T'[s1] & 'T'[s2]", got
    print(f"  OK  [s1] + [s2] (both string-typed, no literal) -> {got}")

    got, _r, _p = translate("[s1] + CHAR(10) + [s2]", r, resolve_type=rt3)
    assert got == "'T'[s1] & UNICHAR(10) & 'T'[s2]", got
    print(f"  OK  [s1] + CHAR(10) + [s2] -> {got}  (textual-ness still propagates through the chain)")

    got, _r, _p = translate("[n1] + [n2]", r, resolve_type=rt3)
    assert got == "'T'[n1] + 'T'[n2]", got
    print(f"  OK  [n1] + [n2] (both numeric-typed) unaffected -> {got}")

    got, _r, _p = translate("[n1] + [s1]", r, resolve_type=rt3)
    assert got == "'T'[n1] & 'T'[s1]", got
    print(f"  OK  [n1] + [s1] (one string-typed operand) still proven textual -> {got}")

    got, _r, _p = translate("[n1] + [n2]", r)  # no resolve_type at all -- unchanged, no guess
    assert got == "'T'[n1] + 'T'[n2]", got
    print("  OK  no resolve_type at all -> unchanged conservative fallback (still +)")
    print("--- '+' text-concat proof checks OK ---")

    # ---- SUM/AVERAGE of a string-typed column: VALUE()-cast; MIN/MAX etc: decline -------------
    # Real corpus regression: GCLM_EU_GERMANY_COMMERZBANK_AG_v2's Activation.'Issuer YoY Growth',
    # SUM([issr_actv_crds_py]) where Tableau itself declares that field's datatype "string" even
    # though every real value is a plain digit string -- Desktop's own error: "The function SUM
    # cannot work with values of type String" (a query-time MdxScript error, invisible to load).
    print("--- SUM/AVERAGE of a Tableau-string-typed column ---")

    def rt4(field):
        return {"strnum": "string", "realnum": "int64"}.get(field)

    def rk4(field):
        return "column"

    got, _r, _p = translate("SUM([strnum])", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "SUMX('T', IFERROR(VALUE('T'[strnum]), BLANK()))", got
    print(f"  OK  SUM([strnum]) (Tableau-declared string) -> {got}")

    got, _r, _p = translate("AVG([strnum])", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "AVERAGEX('T', IFERROR(VALUE('T'[strnum]), BLANK()))", got
    print(f"  OK  AVG([strnum]) (Tableau-declared string) -> {got}")

    got, _r, _p = translate("SUM([realnum])", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "SUM('T'[realnum])", got
    print(f"  OK  SUM([realnum]) (genuinely numeric) unaffected -> {got}")

    got, _r, _p = translate("COUNT([strnum])", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "COUNT('T'[strnum])", got
    print(f"  OK  COUNT([strnum]) unaffected -- COUNT needs no numeric coercion -> {got}")

    for _bad in ("MIN([strnum])", "MAX([strnum])", "MEDIAN([strnum])"):
        try:
            translate(_bad, r, resolve_type=rt4, resolve_kind=rk4)
            raise AssertionError(f"expected Unsupported for {_bad!r}")
        except Unsupported:
            print(f"  OK  {_bad} declined rather than guessed comparison semantics")
    print("--- SUM/AVERAGE of a Tableau-string-typed column checks OK ---")

    # ---- redundant parens around a bare column inside an aggregate ----------------------------
    # Real corpus regression: GCLM_EU_GERMANY_COMMERZBANK_AG_v2's Activation.'Activation Rate_issr'
    # = AVG([issr_actv_crds_cy])/AVG(([issr_total_actv_crd_cy])) -- the second AVG's argument has a
    # redundant Tableau paren, which used to defeat the string-column-cast check above entirely.
    print("--- redundant parens around a bare column inside an aggregate ---")

    got, _r, _p = translate("AVG(([strnum]))", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "AVERAGEX('T', IFERROR(VALUE('T'[strnum]), BLANK()))", got
    print(f"  OK  AVG(([strnum])) (one redundant paren layer) -> {got}")

    got, _r, _p = translate("SUM((([strnum])))", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "SUMX('T', IFERROR(VALUE('T'[strnum]), BLANK()))", got
    print(f"  OK  SUM((([strnum]))) (two redundant paren layers) -> {got}")

    got, _r, _p = translate("SUM(([realnum]))", r, resolve_type=rt4, resolve_kind=rk4)
    assert got == "SUM('T'[realnum])", got
    print(f"  OK  SUM(([realnum])) (genuinely numeric, parens just drop away) -> {got}")
    print("--- redundant-parens-around-bare-column checks OK ---")
