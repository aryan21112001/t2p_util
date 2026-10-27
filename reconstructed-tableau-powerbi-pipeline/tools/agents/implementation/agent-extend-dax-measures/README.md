# Agent: DAX Measure Extension (isolated, experimental)

Not part of the numbered Agent 1 -> 2/3 -> 5 -> 4 pipeline. Acts on an ALREADY-BUILT
build-tableau-pbip project and writes a SEPARATE, new copy with additional table calculations
promoted from declined to real DAX measures -- see the `extend-dax-measures` skill
(`.claude/skills/extend-dax-measures/SKILL.md`) for the full scope and safety rules.

Covers three calc shapes: `LOOKUP(<expr>, 0)` same-row passthroughs, `INDEX()` row-position
calcs, and fixed-window `WINDOW_SUM(<expr>, -N, 0)` moving sums/averages (bare or as a ratio of
two, e.g. a "moving average rate"). When worksheets disagree on the addressing field, this agent's
skill promotes ONE measure PER DISTINCT addressing variant instead of declining the whole calc.

## Run

```bash
python3 dax_measures_extension_agent.py <workbook.twbx> <existing_pbip_dir> -o <new_pbip_dir>
```

## Why this is a separate agent

The regular pipeline (Agent 1: Converter) declines every table calc except one narrow
RANK/RANK_DENSE case (handled by the sibling `agent-extend-tablecalc-rank`). This agent covers
three more, also-safe shapes that have not yet been folded into the main pipeline -- keeping it as
its own agent means running it never changes what the regular pipeline already produced for a
workbook; it only ever adds a new, separate output to review. It is also strictly **model-only**:
it never reads or writes anything under a `*.Report` folder, so it cannot affect any page or
visual, on purpose -- wiring a page's visual to a specific addressing variant is left as a
separate, later, human step.

## Output

- A full copy of `<existing_pbip_dir>` at `<new_pbip_dir>`, with 0+ measures patched in.
- `DAX_MEASURE_EXTENSION_REPORT.md` inside `<new_pbip_dir>`, listing every promotion (with its
  addressing field and which worksheet(s) that variant belongs to) and every skip with its
  specific reason.

Nothing in `<existing_pbip_dir>` is ever modified, and no `*.Report` file is ever touched.
