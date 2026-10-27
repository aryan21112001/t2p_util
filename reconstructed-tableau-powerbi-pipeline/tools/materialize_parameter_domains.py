#!/usr/bin/env python3
"""
materialize_parameter_domains.py -- post-build step, run AFTER build-tableau-pbip.

Purpose
-------
build-tableau-pbip deliberately declines to invent a fixed-list domain for any Tableau
parameter whose domain is "any" (free-entry) or an unenumerable "range" -- it emits a
one-row Param table (the saved value only) and logs the gap to residue.json / L4, because
guessing a domain would be worse than admitting it doesn't know one.

But some of those "free-entry" parameters are not actually free-entry at all: they are a
categorical switch that Tableau happens to model as a string parameter, and the ONLY reason
its real domain isn't visible to the builder is that the domain lives implicitly in an
IF/ELSEIF or CASE/WHEN comparison inside some OTHER calculated field's formula --
e.g. `IF [Parameters].[pl.Lookup Type] = "Lookup Date" THEN ... ELSEIF ... = "Eligible
Date" THEN ...`. Tableau's own parameter definition never lists "Lookup Date" as a member;
Tableau just trusts the workbook author typed a value that some formula happens to test for.

This script is that missing bridge, run as an explicit, reviewable, opt-in step -- never
folded into build-tableau-pbip itself, so the builder's "never invent" invariant stays
intact and this step's output is always a diff you can inspect before it touches anything.

What it does
------------
1. Reads this project's own `_work/ir.json` (must not have been deleted after build; run
   build-tableau-pbip without --clean-work, or keep a copy, if you want to use this step).
2. For every parameter whose domain is "any" or an unenumerated "range" (the exact set
   build-tableau-pbip already flagged as L4/D residue for this reason):
   a. Scans every calc's Tableau formula, across every table, for that parameter's raw
      Tableau identity (`[Parameters].[<tableau_name>]`) appearing in either:
        - `[Parameters].[X] = "literal"` / `= 'literal'` (the IF/ELSEIF idiom), or
        - `CASE [Parameters].[X] ... WHEN 'literal' THEN ...` (the CASE idiom)
   b. If it finds one or more literal string comparisons for that parameter, treats that as
      the parameter's real (previously invisible) domain.
   c. If it finds none, leaves the parameter alone and reports it as CONFIRMED free-entry
      -- genuinely no enumerable domain exists in this workbook, so nothing is invented.
3. For every parameter with a newly-discovered domain, rewrites its already-built
   `Param <name>.tmdl` file:
   - the `Table.FromRows({...}, type table [...])` row list becomes the discovered values
     (the column type annotation is left untouched -- this script never re-derives it)
   - the `SELECTEDVALUE(..., <default>)` fallback becomes whichever discovered value occurs
     most often across the scanned formulas (ties broken by first occurrence), so the
     model has a sane default the moment the report opens, same rationale tmdl.py's own
     comment gives for why that fallback exists at all.
4. Writes a Markdown report next to the project (PARAMETER_DOMAIN_MATERIALIZATION.md)
   listing what was converted, what was confirmed free-entry, and what needed a human
   (non-string datatype with comparison hits -- deliberately not auto-formatted, since this
   script's literal-discovery logic only has confirmed corpus evidence for string domains).

Scope note: this only ever touches parameters domain == "any" or unenumerated "range" --
a parameter build-tableau-pbip already gave a real list/range domain to is left completely
alone, matching its own decision, not second-guessed.

Usage
-----
    python3 materialize_parameter_domains.py <project_dir> [<project_dir> ...] [--apply]

<project_dir> is a built project folder, e.g.
    output/corpus_run/WorkbookB Dashboard_dummy
i.e. the folder containing `_work/ir.json` and a `*.SemanticModel` subfolder.

Without --apply this is a dry run: it prints exactly what it would change and writes the
report, but touches no .tmdl file. Pass --apply to actually rewrite the Param tables.
"""
import sys
import re
import json
import glob
import os
import collections

PARAM_EQ = re.compile(
    r"\[Parameters\]\.\[([^\]]+)\]\s*(?:<>|!=|=)\s*(?:\"([^\"]*)\"|'([^']*)')")
CASE_HEAD = re.compile(r"CASE\s*\[Parameters\]\.\[([^\]]+)\]", re.IGNORECASE)
WHEN_LIT = re.compile(r"WHEN\s+(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
END_KW = re.compile(r"\bEND\b", re.IGNORECASE)
CASE_KW = re.compile(r"\bCASE\b", re.IGNORECASE)

FROMROWS_RE = re.compile(
    r"(Source\s*=\s*Table\.FromRows\()(\{.*\})(,\s*type table \[[^\]]+\]\))", re.S)
SELECTEDVALUE_RE = re.compile(
    r"(SELECTEDVALUE\('Param [^']+'\[Value\],\s*)([^)]+?)(\))")


def load_ir(project_dir):
    ir_path = os.path.join(project_dir, "_work", "ir.json")
    if not os.path.isfile(ir_path):
        return None, ir_path
    with open(ir_path, encoding="utf-8") as fh:
        return json.load(fh), ir_path


def all_formulas(ir):
    """Yield every Tableau formula string in the workbook: table calcs first, then any
    parameter that is itself defined as a formula-bearing "(copy)" alias (rare, but the same
    [Parameters].[...] idiom can appear inside one parameter's own definition referencing
    another)."""
    for t in ir.get("tables", []):
        for c in t.get("calcs", []) or []:
            f = c.get("formula")
            if f:
                yield f


def find_literal_domain(tableau_name, formulas):
    """Search every formula for comparisons against [Parameters].[tableau_name]; return an
    ordered Counter of the literal strings found, or an empty Counter if none."""
    hits = collections.Counter()
    needle = tableau_name
    for f in formulas:
        if needle not in f:
            continue
        # IF/ELSEIF idiom: direct "[Parameters].[X] = 'literal'" pairs, anywhere in the text.
        for m in PARAM_EQ.finditer(f):
            if m.group(1) != needle:
                continue
            lit = m.group(2) if m.group(2) is not None else m.group(3)
            hits[lit] += 1
        # CASE/WHEN idiom: CASE [Parameters].[X] ... WHEN 'A' THEN ... WHEN 'B' THEN ... END
        for cm in CASE_HEAD.finditer(f):
            if cm.group(1) != needle:
                continue
            # values between this CASE and its closing END (or next CASE, or end of string)
            start = cm.end()
            end_m = END_KW.search(f, start)
            next_case_m = CASE_KW.search(f, start)
            stop = len(f)
            if end_m:
                stop = end_m.start()
            if next_case_m and next_case_m.start() < stop:
                stop = next_case_m.start()
            block = f[start:stop]
            for wm in WHEN_LIT.finditer(block):
                lit = wm.group(1) if wm.group(1) is not None else wm.group(2)
                hits[lit] += 1
    return hits


def m_string_literal(v):
    return '"' + v.replace('"', '""') + '"'


def dax_string_literal(v):
    return '"' + v.replace('"', '""') + '"'


def find_param_tmdl(project_dir, param_name):
    pattern = os.path.join(project_dir, "*.SemanticModel", "definition", "tables",
                            "Param " + param_name + ".tmdl")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def rewrite_param_tmdl(path, values_in_order, default_value, apply):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    new_rows = "{" + ", ".join("{" + m_string_literal(v) + "}" for v in values_in_order) + "}"
    fm = FROMROWS_RE.search(text)
    if not fm:
        return None, "no Table.FromRows(...) found in file -- left untouched"
    new_text = text[:fm.start()] + fm.group(1) + new_rows + fm.group(3) + text[fm.end():]

    sm = SELECTEDVALUE_RE.search(new_text)
    if sm:
        new_text = (new_text[:sm.start()] + sm.group(1)
                    + dax_string_literal(default_value) + sm.group(3)
                    + new_text[sm.end():])
    else:
        # Some param tables may have no alternateResult arg at all; leave as-is rather than
        # guessing where to insert one -- report the gap instead of silently changing shape.
        pass

    diff_preview = (
        f"  rows: {new_rows}\n"
        f"  default: {dax_string_literal(default_value)}"
        + ("" if sm else "  (SELECTEDVALUE default arg not found -- left unchanged)"))

    if apply:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    return diff_preview, None


def process_project(project_dir, apply):
    ir, ir_path = load_ir(project_dir)
    report_lines = [f"# Parameter Domain Materialization -- {os.path.basename(project_dir)}",
                     ""]
    if ir is None:
        report_lines.append(
            f"**Could not run: `{ir_path}` not found.** This step needs the project's own "
            f"`_work/ir.json`, which build-tableau-pbip deletes at the end of a normal build "
            f"unless run with a flag that keeps `_work/`. Re-run the build keeping `_work/`, "
            f"or restore it from source control, before using this step.")
        return "\n".join(report_lines), 0

    formulas = list(all_formulas(ir))
    params = ir.get("parameters", []) or []

    converted, confirmed_free, needs_review, missing_tmdl = [], [], [], []

    for p in params:
        domain = p.get("domain")
        members = p.get("members") or []
        if domain not in ("any", "range") or members:
            continue  # build-tableau-pbip already gave this one a real domain -- not ours to touch
        hits = find_literal_domain(p["tableau_name"], formulas)
        if not hits:
            confirmed_free.append(p)
            continue
        if p.get("datatype") != "string":
            needs_review.append((p, hits))
            continue
        values_in_order = [v for v, _ in hits.most_common()]
        # Guarantee the parameter's own currently-saved value is representable, even if no
        # formula happened to compare against it (e.g. it's the default branch of an
        # ELSEIF/CASE with no explicit equality of its own).
        saved = p.get("value")
        if isinstance(saved, str) and saved and saved not in values_in_order:
            values_in_order.append(saved)
        default_value = hits.most_common(1)[0][0]

        tmdl_path = find_param_tmdl(project_dir, p["name"])
        if not tmdl_path:
            missing_tmdl.append(p)
            continue
        diff_preview, err = rewrite_param_tmdl(tmdl_path, values_in_order, default_value, apply)
        if err:
            needs_review.append((p, hits))
            continue
        converted.append((p, values_in_order, default_value, tmdl_path, diff_preview))

    report_lines.append(
        f"Scanned {len(params)} parameter(s), {len(formulas)} calc formula(s). "
        f"{'APPLIED' if apply else 'DRY RUN -- no files changed'}.")
    report_lines.append("")

    report_lines.append(f"## Converted -- {len(converted)} parameter(s) given a real fixed list")
    report_lines.append("")
    for p, values, default_value, tmdl_path, diff_preview in converted:
        report_lines.append(f"### `{p['name']}` (table `Param {p['name']}`)")
        report_lines.append(f"- Tableau identity: `[Parameters].[{p['tableau_name']}]`")
        report_lines.append(f"- Discovered values: {', '.join(repr(v) for v in values)}")
        report_lines.append(f"- New default: {default_value!r}")
        report_lines.append(f"- File: `{os.path.relpath(tmdl_path, project_dir)}`")
        report_lines.append(f"```\n{diff_preview}\n```")
        report_lines.append("")

    report_lines.append(
        f"## Confirmed free-entry -- {len(confirmed_free)} parameter(s), left untouched")
    report_lines.append("")
    report_lines.append(
        "No calc formula in this workbook compares these against a literal value -- they are "
        "genuinely free-entry (a date, a numeric offset, an arbitrary user-typed value), not a "
        "disguised categorical switch. Nothing was changed.")
    report_lines.append("")
    for p in confirmed_free:
        report_lines.append(f"- `{p['name']}` (`{p['tableau_name']}`, datatype {p['datatype']})")
    report_lines.append("")

    if needs_review:
        report_lines.append(f"## Needs manual review -- {len(needs_review)} parameter(s)")
        report_lines.append("")
        report_lines.append(
            "Literal comparisons were found, but this script only auto-formats string-typed "
            "domains (the only shape confirmed in this corpus). Review these by hand.")
        report_lines.append("")
        for p, hits in needs_review:
            vals = ", ".join(f"{v!r}x{n}" for v, n in hits.most_common())
            report_lines.append(f"- `{p['name']}` (datatype {p['datatype']}): {vals}")
        report_lines.append("")

    if missing_tmdl:
        report_lines.append(f"## Param .tmdl file not found -- {len(missing_tmdl)} parameter(s)")
        report_lines.append("")
        for p in missing_tmdl:
            report_lines.append(f"- `{p['name']}` -- expected `Param {p['name']}.tmdl` under "
                                f"`*.SemanticModel/definition/tables/`, not found")
        report_lines.append("")

    return "\n".join(report_lines), len(converted)


def main(argv):
    apply = "--apply" in argv
    projects = [a for a in argv if a != "--apply"]
    if not projects:
        print(__doc__)
        return 1
    total_converted = 0
    for project_dir in projects:
        project_dir = project_dir.rstrip("/")
        report, n = process_project(project_dir, apply)
        total_converted += n
        out_path = os.path.join(project_dir, "PARAMETER_DOMAIN_MATERIALIZATION.md")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"[{project_dir}] {n} parameter(s) converted "
             f"({'applied' if apply else 'dry run'}) -- report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

