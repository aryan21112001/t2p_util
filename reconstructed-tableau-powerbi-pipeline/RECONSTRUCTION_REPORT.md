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

## Scope

This restores the archived snapshot under `tools/` and `.claude/skills/`. Files outside those roots, such as root-level dependency manifests or environment configuration, are not part of the archive and have not been restored. The archived documents were restored as archived, including any contradictory or stale guidance. Nothing was executed, no dependencies were installed, and restoration checks prove archive integrity only, not that dependencies are installed or that Power BI Desktop opens a project.
