# Agent: Table-Calc RANK Extension (isolated, experimental)

Not part of the numbered Agent 1 -> 2/3 -> 5 -> 4 pipeline. Acts on an ALREADY-BUILT
build-tableau-pbip project and writes a SEPARATE, new copy with additional RANK/RANK_DENSE table
calculations promoted from declined to real DAX measures -- see the
`extend-tablecalc-rank` skill (`.claude/skills/extend-tablecalc-rank/SKILL.md`) for the full
scope and safety rules.

## Run

```bash
python3 tablecalc_rank_extension_agent.py <workbook.twbx> <existing_pbip_dir> -o <new_pbip_dir>
```

## Why this is a separate agent

The regular pipeline (Agent 1: Converter) already promotes RANK/RANK_DENSE table calcs in one
safe, narrow case. This agent covers a second, also-safe case that has not yet been folded into
the main pipeline -- keeping it as its own agent means running it never changes what the regular
pipeline already produced for a workbook; it only ever adds a new, separate output to review.

## Output

- A full copy of `<existing_pbip_dir>` at `<new_pbip_dir>`, with 0+ measures patched in.
- `TABLECALC_RANK_EXTENSION_REPORT.md` inside `<new_pbip_dir>`, listing every promotion and
  every skip with its specific reason.

Nothing in `<existing_pbip_dir>` is ever modified.
