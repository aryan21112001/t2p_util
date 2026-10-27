# Skills & Agents Quick Reference

**FIVE AGENTS over SEVEN SKILLS.** Agents hold no logic of their own — each shells out to skill
scripts under `.claude/skills/`, parses their summary output, and aggregates. "Agent" here means
*wrapper*, not *model*: none of them contain an LLM.

For what each agent does and how they chain, see
[`../../README_AGENTS.md`](../../README_AGENTS.md). This file is the skill↔agent map.

---

## Skills that exist

| Skill | Scripts |
|---|---|
| `build-tableau-pbip` | `build_pbip.py`, `extract.py`, `dax.py`, `tmdl.py`, `pbir.py`, `names.py` |
| `extract-twbx-contents` | *(interactive; prompts for paths, no CLI args)* |
| `generate-dummy-data` | `generate_dummy_data.py` |
| `validate-dummy-data` | `validate_dummy_data.py` |
| `validate-tableau-pbip-mapping` | `validate_mapping.py` |
| `validate-pbip-compliance` | `validate_pbip_compliance.py`, `pbip_lint.py` |
| `generate-copilot-fix-prompts` | `generate_copilot_prompts.py`, `suggest_visual_candidates.py`, `effort_report.py`, `report_dangling_references.py` |

---

## Which agent uses which skill

| Skill | A1 build | A2 dummy | A3 realistic | A5 prompts | A4 validate |
|---|---|---|---|---|---|
| `extract-twbx-contents` | ✓ | | ✓ | | |
| `build-tableau-pbip` | ✓ | | | | |
| `generate-dummy-data` | | ✓ | ✓ *(realistic mode)* | | |
| `validate-dummy-data` | | ✓ | ✓ | | ✓ |
| `validate-tableau-pbip-mapping` | ✓ | | | | ✓ |
| `validate-pbip-compliance` | ✓ | | | | ✓ |
| `generate-copilot-fix-prompts` | | | | ✓ | ✓ *(`effort_report.py`)* |

**Run order** (not a preference — see `README_AGENTS.md`):

```
A1 build  →  A2/A3 data  →  A5 prompts  →  A4 validate
```

There is no "apply" agent -- every DAX fix a human writes gets applied by hand in Power BI
Desktop, guided by A5's `COPILOT_FIX_PROMPTS.md`. Nothing in this pipeline writes into the real
`.SemanticModel` on its own.

---

## Reuse, and why nothing is split

| Skill | Shared by | Split needed? | Why |
|---|---|---|---|
| `generate-dummy-data` | A2, A3 | ❌ | One implementation, two modes (`--realistic` flips it). Splitting would duplicate the stub-partition parser. |
| `validate-dummy-data` | A2, A3, A4 | ❌ | Generic; takes a project path and reports. |
| `validate-tableau-pbip-mapping` | A1, A4 | ❌ | A1 uses it as a build self-check, A4 as independent QA. Same question, same answer. |
| `validate-pbip-compliance` | A1, A4 | ❌ | Same. |
| `generate-copilot-fix-prompts` | A5, A4 | ❌ — **but note** | The two agents call **different scripts in the same skill**: A5 runs `generate_copilot_prompts.py` + `suggest_visual_candidates.py` (produce the Copilot prompts + presentation triage), A4 runs `effort_report.py` (how much of what's left needs a human, and how long). Both share `validate_mapping.suggest_fix`'s recommended-vs-raw split, so "what the prompt shows" and "what the effort report counts" can never drift apart. |
| `build-tableau-pbip` | A1 | ❌ | The deterministic core, single owner. |
| `extract-twbx-contents` | A1, A3 | ❌ | Unpacks the archive; both need the same bytes. |

---

## The one cross-skill dependency worth knowing

`generate-copilot-fix-prompts` **imports from `build-tableau-pbip`** rather than reimplementing:

- `dax.py` — `is_aggregate`, so cascade/root-cause detection in `COPILOT_FIX_PROMPTS.md` agrees
  with the build's own aggregate-ness check, not a second, disagreeing one
- `pbir.py` — `_shelf_shape`, so visual triage asks about shelves exactly as the build does

Consequence: **changing `dax.py`'s aggregate detection changes cascade grouping in
`COPILOT_FIX_PROMPTS.md`.** Run both skills' self-tests (`python3 <script>.py` with no arguments)
after touching either.

`validate-tableau-pbip-mapping` owns `FIX_RULES`, which `generate-copilot-fix-prompts` imports —
so the two skills' idea of "what the fix is" cannot diverge.

---

## Contract every skill honours

- **Fail closed.** Anything not cleanly translatable is declined *with a reason*, never
  approximated. A silently-wrong measure is worse than a missing one.
- **Deterministic.** No random draws, no `Date.now()`. Lineage tags are content-hashed; synthetic
  data is seeded from column names via `hashlib` (not Python's per-process-salted `hash()`), so a
  re-run is byte-identical.
- **Corpus-agnostic.** No workbook, dashboard, or field name is built in. Every path is an argument.
- **Self-testing.** Run any script with no arguments to execute its self-test.
- **Summary lines parsed by name, not position.** `promoted=141` is matched as a `name=number`
  pair. Agent 5 originally used one positional regex listing every counter in order; when the
  prompts skill grew `root_causes`/`cascade`/`already_fixed_excluded`, it matched *nothing* and
  silently reported zero projects. Keep new counters additive.

---

## Removed skills

Three skills this document previously described no longer exist. If you find references to them,
they are stale:

| Removed | Replaced by |
|---|---|
| `prepare-dummy-pbip` | folded into `generate-dummy-data` (it copies the project itself) |
| `map-tableau-powerbi-translation` | `validate-tableau-pbip-mapping` |
| `inventory-tableau-assets` | `COVERAGE.md` / `RESIDUE.md`, emitted directly by `build-tableau-pbip` |
