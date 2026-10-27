# Agent 6: Apply Fixes

**Purpose**: Writes reviewed DAX fixes into a PBIP's real semantic model — the step that turns
what the earlier agents only *staged* into something Power BI Desktop actually loads.

## Pipeline position

```
Agent 1 (convert) → Agent 2/3 (data) → Agent 5 (prompts) → [ AGENT 6: apply ] → Agent 4 (validate)
```

Agent 1 declines anything the deterministic tiers cannot translate (recorded in
`_work/residue.json`) rather than approximating it. Agent 5 turns that residue into review
material. **Agent 6 applies the fixes.** Agent 4 then validates the result.

## Use cases

1. **Apply generated candidates** — the mechanical rewrites the skill authors itself
   (DATETRUNC/DATEPART/DATEADD/DATEDIFF/STR/COUNTD/DATE-cast/MAKEDATE/TOTAL/LOOKUP(x,0))
2. **Apply reviewed hand-authored DAX** — whatever a human or Copilot wrote, via an overrides file
3. **Clear cascades** — fields declined *only* because a dependency was declined
4. **Re-apply after a rebuild** — a rebuild resets the model's name ledger; this puts the reviewed
   fixes back
5. **Dry-run audit** — see exactly what would change, writing nothing

## Usage

```bash
# Apply to one project, looping to a fixpoint (auto-uses dax_overrides/<Workbook>.json if present)
python3 apply_fixes_agent.py output/prevent_rebuild/"Prevent Dashboard"

# See what WOULD be applied, writing nothing
python3 apply_fixes_agent.py output/prevent_rebuild/"Prevent Dashboard" --dry-run

# Every project under a parent directory
python3 apply_fixes_agent.py output/corpus_rebuild

# Show every round
python3 apply_fixes_agent.py output/prevent_rebuild/"Prevent Dashboard" -v

# A specific overrides file instead of the auto-discovered one
python3 apply_fixes_agent.py <path> --overrides dax_overrides/custom.json

# JSON output
python3 apply_fixes_agent.py <path> --json
```

## ⚠️ Ordering matters

Run this on the **raw Agent 1 output, BEFORE Agent 2/3 wire the data.** Agent 2/3 *copy* the whole
project, so promoting afterwards leaves the copy and the original out of step:

```
Agent 1 (build)  →  AGENT 6 (apply)  →  Agent 2/3 (data)  →  Agent 5 (prompts)  →  Agent 4 (validate)
```

## Architecture

Skills-first, exactly like Agent 5: a thin wrapper that shells out to
`.claude/skills/generate-copilot-fix-prompts/scripts/promote_dax_candidates.py`, parses its summary
line, and aggregates. **No LLM is involved** — every decision is mechanical (fixed commands, fixed
order, and a termination condition that is literally `promoted=0`). The judgment lives in the
overrides *file*; this agent only applies it deterministically and reports what happened.

### The fixpoint loop is the whole point

Promoting one calc can unblock others that were declined only because they depended on it. The
skill re-attempts those cascades on each run (its resolvers read the live `emitted` set), so this
agent calls it **repeatedly until a round promotes nothing.**

Measured on the reference corpus (Prevent Dashboard, fresh build):

| Round | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Promoted | 49 | 30 | 11 | 14 | 14 | 9 | 6 | 6 | 1 | 1 | 0 |

**141 objects total.** A single pass would have applied 49 and stopped — the loop is worth ~3× the
recovery, which is why it is the agent's core behaviour rather than a flag.

Result on that run: `pbip_rules/lint.py` clean, Agent 4 **APPROVED (91/100)**.

`MAX_ROUNDS = 40` is a safety cap only; the real exit is `promoted=0`. Hitting the cap is reported
as an error, because it would mean promotion is not idempotent and the project should not be
trusted until that is understood.

### Overrides are auto-discovered

`dax_overrides/<Workbook>.json` at the repo root is passed through automatically, so reviewed fixes
survive every rebuild without anyone remembering a flag. That directory sits **outside** the output
tree deliberately — everything under `output/` is regenerated from source.

### Summary lines are parsed by name, not position

Counters are matched as `name=number` pairs rather than in a fixed order, so a counter added to the
skill later is additive. This is not hypothetical: Agent 5 originally used one positional regex
listing every counter, and when the prompts skill grew `root_causes`/`cascade`/
`already_fixed_excluded` it matched *nothing* and silently reported zero projects. Fixed there, and
avoided here by construction.

## Output

- Applied measures/columns written directly into `<Project>.SemanticModel/definition/tables/*.tmdl`
  using build-tableau-pbip's own emitters, under the name the build had already reserved
- `emitted` updated in `_work/name_authority.json` so every downstream tool sees the change
- `APPLY_FIXES_RUN_SUMMARY.json` — per-project totals, hand-authored count, and per-round detail

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All projects applied cleanly |
| 1 | At least one project failed (e.g. no fixpoint within `MAX_ROUNDS`) |
| 2 | Bad input — path missing, skill script missing, or no `_work/residue.json` found |

## Known limits

- Applies only what the skill can author plus what an overrides file supplies. Gaps needing
  information the `.twbx` does not contain — table calculations whose addressing/partitioning lives
  in Tableau's UI, `EXCLUDE`/`INCLUDE` LOD relative to a view's dimensions — stay declined and are
  reported by Agent 5 for a human.
- Does not rewrite `residue.json`/`RESIDUE.md`/`COVERAGE.md`. Those remain an accurate record of
  what the original deterministic build did not translate; `validate-tableau-pbip-mapping`'s
  Measures/Columns sheets are the source of truth for what is in the model *now*.
- `--dry-run` reports a single pass, not the full fixpoint: nothing is written, so every later round
  would report the identical number forever.
