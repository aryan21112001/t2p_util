# Restoration Report

This file is restoration metadata. It is not part of the archived source roots (`tools/` and `.claude/skills/`).

## Inputs

| File | Size (bytes) | SHA-256 |
|---|---:|---|
| `ALL_AGENTS_AND_PIPELINE.md` | 6444088 | `58be165b92cfa26688fad5ee07df00f4f6a4f9410a63d9aaf08eaa0091197d53` |
| `RECONSTRUCT_AGENTS_AND_PIPELINE_PROMPT.md` | 9924 | `c08a3d06878540696caf79817d20bd45785da23656f394a177f7e4c109f065f0` |

## Totals

- Archive file blocks: **147** (every block had matching BEGIN/END markers, one metadata line, a matching fence, and a unique, safe path under `tools/` or `.claude/skills/`)
- Restored files: **147** (text: **86**, binary: **61**)
- Total restored source bytes: **5,346,067**
- Archive index paths and file-block paths: identical sets (147 each)

| Archive section | Files |
|---|---:|
| 00. Pipeline architecture and runbook | 4 |
| 01. Agent 1 - conversion and build validation | 51 |
| 02. Agents 2 and 3 - synthetic and realistic data | 16 |
| 03. Agent 5 - Copilot fix prompts and effort reports | 14 |
| 04. Agent 4 - final validation and optional data checks | 6 |
| 05. Isolated extensions, retired agent, and gotchas | 16 |
| 06. Shared tools, rule engines, fixtures, and remaining skills | 40 |

## Integrity

- Every payload was checked against its recorded byte length and SHA-256 **before** it was written.
- After writing, every restored file was re-read as bytes and compared again; 147 of 147 matched.
- The set of restored paths equals the set of archive paths: no missing, extra or duplicate files.
- All restored paths are beneath `tools/` or `.claude/skills/`.
- Failed or skipped items: none.

## Framing note (one file)

For 85 of 86 text files, removing exactly one framing newline before the closing fence reproduced the recorded length and SHA-256. For `.claude/skills/build-tableau-pbip/assets/CY26SU02.json` (marked `Source ended with newline: no`), the archive has one additional blank line before the closing fence, so removing one newline gave 8632 bytes and failed the check. Removing two newlines gave exactly the recorded 8631 bytes with a matching SHA-256, and that is what was written. No other file needed this.

## Post-restoration sanitization (deliberate deviation from the archive)

After the byte-for-byte restoration above, the tree was **deliberately modified** to remove references to the environment it was copied from, because this project is to be maintained as an independent, personal long-term project. The integrity results above describe the tree *as restored*; they no longer hold for the files listed below, and the SHA-256 values in the archive no longer match them.

- **Names replaced** (case-sensitive, text files only): the name of the originating organisation, and the internal names of the real dashboards the tooling was developed against, replaced with neutral placeholders. This covers workbook names, page, sheet and table names, field, column and calculated-field names, data source and database table names, auto-generated Tableau ID digits, and payments-domain vocabulary used in examples. Placeholders look like `WorkbookA`, `Sheet 4 - ...`, `Rate_x` and `Item Id`. Each replacement was applied consistently across every file, including self-test fixtures, so fixtures still agree with each other. About 44 text files were modified in total. Line-wrapped occurrences were handled as well. Every changed `.py` file was re-parsed with `ast`.
- **Verification:** every script's built-in self-test (13 of them) was run before and after, on a copy of the original restored tree and on the sanitized tree, and the results were compared (exit code, number of OK lines, number of failures or tracebacks). They are identical. Two scripts (`extract.py` and `validate_dummy_data.py`) exit non-zero both before and after: their inline tests pass but they then stop on a usage message or on a sample file that is not part of the archive. This is not a full functional test of the pipeline.
- **Code fix (not a sanitization change):** `tools/data-prep/hyper_to_csv.py` was rewritten to work with the current `tableauhyperapi`. The original imported a name that package does not provide (`SqlQueryContext`), which the script then reported as "tableauhyperapi not installed"; it also connected without a database path, called a non-existent catalog method, passed a wrong argument to `escape_name`, referenced an undefined variable in its "file not found" message, and wrote CSV by hand without quoting. It now writes the first table of the `Extract` schema with the `csv` module (NULL becomes an empty field) and keeps the same command line. It is covered by `scripts/smoke_test.sh`. The pipeline's agents and skills do not use this helper.
- **Compiled caches removed:** all 61 `__pycache__/*.pyc` files were deleted. They embedded a personal username and an internal repository path, and the replaced names, and cannot be edited safely. They are regenerated automatically when the scripts run, and a repository-root `.gitignore` now excludes them. The tree now holds 86 files besides this report (86 text files).
- **Not changed or not guaranteed:**
  - The `cl.` prefix is kept because `extend_tablecalc_quickcalcs.py` generates measure names with it.
  - Generic Tableau, Power BI and DAX terms, generic test-fixture names (for example `Total Sales`, `Workbook Alpha`) and the personal folder name `agent-extend-aryan` were left alone.
  - The replacement was driven by names found by searching the files, so a name that was not recognised may remain, especially inside long prose lines. There is no automatic way to prove none is left. A list of additional terms to remove can be applied the same way.
- **Git history:** earlier commits on this repository still contain the original, unsanitized files. Editing the current tree does not remove them from history.

## Scope

This restores the archived snapshot under `tools/` and `.claude/skills/`. Files outside those roots, such as root-level dependency manifests or environment configuration, are not part of the archive and have not been restored. The archived documents were restored as archived, including any contradictory or stale guidance. Nothing was executed, no dependencies were installed, and restoration checks prove archive integrity only, not that dependencies are installed or that Power BI Desktop opens a project.
