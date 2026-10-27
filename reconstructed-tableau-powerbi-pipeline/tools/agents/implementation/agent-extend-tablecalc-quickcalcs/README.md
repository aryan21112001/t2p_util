# Agent: Table-Calc Quick-Calculations Extension (isolated, experimental)

Not part of the numbered Agent 1 -> 2/3 -> 5 -> 4 pipeline. Acts on an ALREADY-BUILT
build-tableau-pbip project and writes a SEPARATE, new copy identifying every Tableau quick
table-calculation button (Percent of Total, Difference, Running Total, Moving Average, ...) and
promoting resolvable Percent-of-Total instances into real DAX measures -- see the
`extend-tablecalc-quickcalcs` skill (`.claude/skills/extend-tablecalc-quickcalcs/SKILL.md`) for
the full scope and safety rules.

## Run

```bash
python3 tablecalc_quickcalcs_extension_agent.py <workbook.twbx> <existing_pbip_dir> -o <new_pbip_dir>
```

## Why this is a separate agent

A quick-calc button is not a named calculated field -- it attaches directly to a worksheet's
shelf placement, so nothing about it exists in `ir.json` until this extension's own capture in
`extract.py`. Running this agent never changes what the regular pipeline already produced for a
workbook; it only ever adds a new, separate output to review.

## Output

- A full copy of `<existing_pbip_dir>` at `<new_pbip_dir>`, with 0+ Percent-of-Total measures
  patched in.
- `TABLECALC_QUICKCALCS_EXTENSION_REPORT.md` inside `<new_pbip_dir>`, listing every promotion,
  every OTHER recognized quick-calc type found (by name and count, not yet templated), and every
  Percent-of-Total instance that was checked and declined, with a specific reason.

Nothing in `<existing_pbip_dir>` is ever modified.
