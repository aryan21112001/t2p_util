#!/usr/bin/env python3
"""Assert generated TMDL matches the DESKTOP-AUTHORED reference in docs/examples/GroundTruth.

Every rule here was a real defect, and every one of them passed a pre-flight that only checked
self-consistency. The lesson the file encodes: the schema reference is a secondary source and
Desktop is the authority. Where they disagreed, the reference was wrong six times.

Run: python3 tests/check_against_ground_truth.py <a-generated-project-folder>
"""
import os
import re
import sys
import json
import glob

GT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "..", "..", "..", "..", "docs", "examples", "GroundTruth.SemanticModel")


def rules(model_dir):
    """(name, ok, detail) per rule."""
    out = []
    d = os.path.join(model_dir, "definition")
    db = open(os.path.join(d, "database.tmdl"), encoding="utf-8").read()
    model = open(os.path.join(d, "model.tmdl"), encoding="utf-8").read()
    tables = sorted(glob.glob(os.path.join(d, "tables", "*.tmdl")))
    # EVERY table file, concatenated. Checking only tables[0] gave false assurance: VRM's first
    # file alphabetically had no groups, so the `expression =` rule passed while a later file in
    # the same project carried four violations that Desktop then rejected. A rule that samples one
    # of N files is a rule that reports "PASS" for work it never looked at.
    tbl = "\n".join(open(f, encoding="utf-8").read() for f in tables)
    pbism = json.load(open(os.path.join(model_dir, "definition.pbism"), encoding="utf-8"))

    # database: bare keyword, no name; 1600 not 1567
    out.append(("database has no name", bool(re.match(r"^database\s*$", db.split("\n")[0])),
                db.split("\n")[0]))
    out.append(("compatibilityLevel 1600", "compatibilityLevel: 1600" in db, ""))

    # THE one that broke Desktop: ref/annotation at column 0, never indented under model
    bad = [ln for ln in model.split("\n") if re.match(r"^\t+(ref|annotation)\s", ln)]
    out.append(("no indented ref/annotation in model.tmdl", not bad, bad[:1]))
    out.append(("model has ref table at column 0",
                any(ln.startswith("ref table ") for ln in model.split("\n")), ""))
    out.append(("model has ref cultureInfo", "ref cultureInfo en-US" in model, ""))
    out.append(("cultures/en-US.tmdl exists",
                os.path.isfile(os.path.join(d, "cultures", "en-US.tmdl")), ""))
    out.append(("model has no discourageImplicitMeasures",
                "discourageImplicitMeasures" not in model, ""))
    out.append(("model has dataAccessOptions", "\tdataAccessOptions" in model, ""))

    out.append(("definition.pbism version 4.2", pbism.get("version") == "4.2",
                pbism.get("version")))
    out.append((".platform present", os.path.isfile(os.path.join(model_dir, ".platform")), ""))

    if tbl:
        # calculated column is `column X = <dax>`, not a separate `expression =` property
        offenders = [os.path.basename(f) for f in tables
                     if re.search(r"^\t\texpression\s*=",
                                  open(f, encoding="utf-8").read(), re.M)]
        out.append((f"no `expression =` property on columns (checked {len(tables)} table file(s))",
                    not offenders, ", ".join(offenders[:3])))
        out.append(("no isDataTypeInferred", "isDataTypeInferred" not in tbl, ""))
        out.append(("columns carry SummarizationSetBy",
                    "annotation SummarizationSetBy = Automatic" in tbl, ""))
        out.append(("table carries PBI_ResultType",
                    "annotation PBI_ResultType = Table" in tbl, ""))
        # M identifiers are #"..." -- NOT TMDL's '...'
        m = re.search(r"type table \[(.*?)\]", tbl, re.S)
        out.append(("M type-table uses #\"..\" quoting, never '..'",
                    not (m and re.search(r"'[^']*'\s*=", m.group(1))),
                    (m.group(1)[:70] if m else "")))
        # partition body two levels below `source =`
        mm = re.search(r"^(\t+)source =\n(\t+)", tbl, re.M)
        out.append(("expression body is 2 levels below its property",
                    bool(mm) and len(mm.group(2)) == len(mm.group(1)) + 2,
                    (f"{len(mm.group(1))} -> {len(mm.group(2))}" if mm else "no partition")))
        # tabs for depth; spaces allowed AFTER the prefix (Desktop does this)
        out.append(("no line starts with a space",
                    not any(ln.startswith(" ") for ln in tbl.split("\n")), ""))
    # File-set rule. Desktop's single-table project has NO relationships.tmdl -- the absence of
    # the file WAS the answer, sitting in the ground truth, and it was missed because only file
    # CONTENTS were compared and not which files exist.
    rel = os.path.join(d, "relationships.tmdl")
    if os.path.isfile(rel):
        txt = [ln for ln in open(rel, encoding="utf-8").read().split("\n") if ln.strip()]
        out.append(("relationships.tmdl exists only if it declares a relationship",
                    any(ln.startswith("relationship ") for ln in txt), "comment-only or empty"))
    else:
        out.append(("relationships.tmdl absent when there are none", True, ""))

    # No TMDL document may open with a comment, and none may be comment-only.
    for f in sorted(glob.glob(os.path.join(d, "**", "*.tmdl"), recursive=True)):
        body = [ln for ln in open(f, encoding="utf-8").read().split("\n") if ln.strip()]
        rel_f = os.path.relpath(f, d)
        if not body:
            out.append((f"{rel_f} is non-empty", False, "empty file"))
        elif body[0].lstrip().startswith("/"):
            out.append((f"{rel_f} opens with an object, not a comment", False, body[0][:40]))
    out.append(("no TMDL document opens with a comment",
                not any(not ok for n, ok, _ in out if "opens with an object" in n), ""))
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = sys.argv[1]
    md = next((os.path.join(root, x) for x in os.listdir(root)
               if x.endswith(".SemanticModel")), None)
    if md is None:
        print(f"[fatal] no *.SemanticModel folder in {root}")
        return 1
    print(f"Checking {os.path.basename(md)} against the Desktop-authored GroundTruth\n")
    bad = 0
    for name, ok, detail in rules(md):
        bad += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if not ok else ""))
    print(f"\n{bad} rule(s) failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
