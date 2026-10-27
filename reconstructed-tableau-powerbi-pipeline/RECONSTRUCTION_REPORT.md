# Reconstruction Report

This file is a reconstruction audit. It is not a source document and is not part of any extracted Markdown file.

## Status

**DOCUMENTATION-ONLY** — documentation reconstructed; executable pipeline not recoverable from the two Markdown files alone. The output is **not runnable**. No agent, skill or pipeline was run.

## Inputs

| File | Size (bytes) | SHA-256 |
|---|---:|---|
| `ALL_AGENTS_AND_PIPELINE.md` | 363614 | `cac192a25217d18984428a60cbb782044e2c7a8f52c68df6085eb97c53360c22` |
| `RECONSTRUCT_AGENTS_AND_PIPELINE_PROMPT.md` | 14403 | `91884ce4aaae36604a83a163c8b502e1585beba393ec345b6198334071124b19` |

Both were read as supplied (UTF-8, LF line endings, no CR characters) and not modified. Hashes are for validation only.

## Result of the split

- All 29 start signatures were found. Each identifies exactly one document start, and the positions are strictly increasing in table order.
- Nothing precedes section 1 (section 1 starts at byte 0).
- All 29 destination files exist and are non-empty.
- Concatenating the 29 written files in table order reproduces the input bundle **byte-for-byte** (verified on disk; SHA-256 identical to the input).
- Extraction was done on raw bytes, with no Markdown parsing and no added headings, separators or attribution.

Start line of each document in the bundle: 1, 224, 579, 943, 1539, 1645, 1877, 1960, 2196, 2321, 2349, 2380, 2417, 2532, 2754, 2909, 2952, 3126, 3251, 3469, 3562, 3976, 4291, 4407, 4612, 4718, 4986, 5071, 5206.

### Signature note (entry 13)

`# agent-extend-aryan` matches the start of two lines by prefix: line 216 (`# agent-extend-aryan/README.md for the verification checklist before trusting output)`, a comment inside a code block in the document that starts at line 1) and line 2417. Only line 2417 equals the signature as a whole line. Line 2417 was used as the boundary; line 216 is content of document 1 and was left untouched.

## Source checkout

No matching original repository or source archive was supplied. Nothing was copied, and no code, configuration, test or dependency file was created.

Empty directories were created for implementation areas that have no Markdown file of their own:

- `tools/agents/implementation/agent-1-converter/`
- `tools/agents/implementation/agent-2-dummy-data/`
- `tools/agents/implementation/agent-3-realistic-data/`
- `tools/agents/implementation/orchestrator/`

(Git does not track empty directories, so these exist only on disk until a file is added.)

## Described by the documents but not contained in the inputs

Paths as named in the source documents. None of these files exist in the reconstructed tree.

**Agents and orchestrators**

- `tools/agents/implementation/agent-1-converter/converter_agent.py`
- `tools/agents/implementation/agent-2-dummy-data/dummy_data_agent.py`
- `tools/agents/implementation/agent-3-realistic-data/realistic_data_agent.py`
- `tools/agents/implementation/agent-4-validator/validator_agent.py`
- `tools/agents/implementation/agent-5-copilot-prompts/copilot_prompts_agent.py`
- `tools/agents/implementation/_retired_agent-6-apply-fixes/apply_fixes_agent.py`
- `tools/agents/implementation/agent-extend-tablecalc-rank/tablecalc_rank_extension_agent.py`
- `tools/agents/implementation/agent-extend-tablecalc-quickcalcs/tablecalc_quickcalcs_extension_agent.py`
- `tools/agents/implementation/agent-extend-dax-measures/dax_measures_extension_agent.py`
- `tools/agents/implementation/orchestrator/basic_pipeline_orchestrator.py`
- `tools/agents/implementation/orchestrator/dummy_data_pipeline_orchestrator.py`
- `tools/agents/implementation/orchestrator/realistic_data_pipeline_orchestrator.py`
- `tools/agents/implementation/orchestrator/pipeline_steps.py`

**Skill scripts**

- `.claude/skills/extract-twbx-contents/extract_twbx_contents.py`
- `.claude/skills/build-tableau-pbip/scripts/` (`build_pbip.py`, `extract.py`, `names.py`, `dax.py`, `tmdl.py`, `pbir.py`) and `build-tableau-pbip/tests/check_against_ground_truth.py`
- `.claude/skills/generate-dummy-data/scripts/generate_dummy_data.py`
- `.claude/skills/validate-dummy-data/scripts/validate_dummy_data.py`
- `.claude/skills/validate-tableau-pbip-mapping/scripts/validate_mapping.py`
- `.claude/skills/validate-pbip-compliance/` (`validate_pbip_compliance.py`)
- `.claude/skills/generate-copilot-fix-prompts/scripts/` (`generate_copilot_prompts.py`, `effort_report.py`, `suggest_visual_candidates.py`, `promote_dax_candidates.py`)
- `.claude/skills/extend-tablecalc-rank/scripts/extend_tablecalc_rank.py`
- `.claude/skills/extend-tablecalc-quickcalcs/scripts/extend_tablecalc_quickcalcs.py`
- `.claude/skills/extend-dax-measures/scripts/extend_dax_measures.py`

**Shared code and configuration**

- `tools/pbip_rules/` (`checks.py`, `model.py`, `lint.py`), `tools/pbip_lint.py`
- `config_converter.yaml`, `config_dummy_data.yaml`, `config_realistic_data.yaml`
- The repository's dependency manifest (not named by the documents)

## Contradictions, stale guidance and concerns (report only; nothing was edited)

1. **Agent count / scope.** `tools/agents/docs/01-Getting-Started/README.md` (title `# Three-Agent Architecture`) describes three agents (Converter, Dummy Data, Realistic) and its "Next Steps" list "Add Agent 4 - Packaging/PBIX creation" and "Add Agent 5 - Deployment/publishing to Power BI Service" as future work. `tools/agents/README_AGENTS.md` describes a numbered pipeline of five agents, with Agent 4 as a PBIP validator and Agent 5 as the Copilot fix-prompt generator (also `agent-4-validator/README.md` and `agent-5-copilot-prompts/README.md`).
2. **Removed skills still described.** `tools/agents/docs/03-Skills-Architecture/SKILLS_QUICK_REFERENCE.md` says `prepare-dummy-pbip`, `map-tableau-powerbi-translation` and `inventory-tableau-assets` "no longer exist" and any reference to them is stale. They are still described as current in `tools/agents/docs/SKILLS_FIRST_ARCHITECTURE.md` (sections 6 and 7, with script paths `inventory_assets.py` and `plan_translation.py`) and `tools/agents/docs/README.md` (lines 115–116). `.claude/skills/build-tableau-pbip/SKILL.md` (line 45), `.claude/skills/extract-twbx-contents/SKILL.md` (line 12) and `.claude/skills/validate-tableau-pbip-mapping/SKILL.md` (line 183) also still mention `inventory-tableau-assets` or `map-tableau-powerbi-translation`.
3. **`/tools/twb2pbip/`.** `SKILLS_FIRST_ARCHITECTURE.md` (line 302 onward) and `tools/agents/docs/README.md` (lines 127 and 133) say the module is legacy/deprecated and that Agent 1 "currently imports from twb2pbip" but should call the `build-tableau-pbip` skill. Whether that change was made is not stated in the inputs; the module is not in the bundle.
4. **Agent 6 pipeline position.** `README_AGENTS.md` says Agent 6 is retired and unused, and the task's pipeline order has no Agent 6. `_retired_agent-6-apply-fixes/README.md` opens with a "Pipeline position" diagram that places Agent 6 between Agent 5 and Agent 4 (`Agent 1 (convert) → Agent 2/3 (data) → Agent 5 (prompts) → [ AGENT 6: apply ] → Agent 4 (validate)`) and shows usage commands for `apply_fixes_agent.py`, with no retirement notice in its first lines.
5. **Code examples in architecture documents.** The older architecture documents contain code examples. They were preserved as text and were not turned into implementations.
6. **Encoding.** The input is valid UTF-8 with LF endings. It contains non-ASCII characters (em dashes, arrows, emoji status markers). No decoding problems were observed.

Items 1–4 are conflicting or stale statements for a maintainer to resolve. No resolution is proposed here.

## Runnable?

No. Only the Markdown documentation and folder layout were reconstructed. The agents cannot be executed from the two input files alone.
