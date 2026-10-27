---
name: extract-twbx-contents
description: >
  Unpacks Tableau .twbx workbooks so their contents can be read. A .twbx is a ZIP archive
  wrapping the workbook XML plus its Data/ and Image/ folders; every later stage in this
  toolchain reads those unpacked files, not the archive, so this normally runs FIRST. Walks an
  input folder recursively, tolerates the doubled .twbx.twbx suffix that some exports produce,
  and extracts each workbook into its own subfolder named after the workbook, preserving the
  internal ZIP structure. Bad or corrupt archives are reported per file and skipped rather than
  aborting the run. Prompts interactively for the input and output folders -- it takes NO
  command-line arguments, so pass paths when asked, not on the command line. Nothing about any
  particular workbook is built in. Use before inventory-tableau-assets, whose --base expects the
  folder of extracted workbooks this produces. Not needed for a bare .twb, which is already XML.
argument-hint: none -- prompts for input folder then output folder
---

# Extract TWBX Contents

Extract raw contents from Tableau .twbx workbook files for inspection and analysis.

## Purpose

.twbx files are ZIP archives containing workbook definitions (XML), data sources, and metadata. This skill extracts these contents to a local folder so you can inspect the structure, datasources, parameters, and calculated fields without loading the workbooks in Tableau.

> **Corpus-agnostic.** This skill has no built-in knowledge of any particular workbook,
> dashboard or field. Every path is an argument. Measured figures quoted below are
> provenance from the reference corpus, not thresholds or expectations for your input —
> see [`docs/reference-corpus.md`](../../../docs/reference-corpus.md).

## Usage

```bash
python extract_twbx_contents.py
```

The script will interactively prompt you for:

1. **Input folder** – Path to a folder containing .twbx files (will discover all .twbx files recursively)
2. **Output folder** – Path where extracted contents will be saved (one subfolder per workbook)

### Example

```
Input folder (contains .twbx files): /path/to/dashboards
Output folder (where to save extracted contents): /tmp/extracted_twbx
```

This will:
- Find all .twbx files in `/path/to/dashboards`
- Extract each to `/tmp/extracted_twbx/<workbook_name>/`
- Preserve the internal ZIP structure (workbook.xml, Datasources/, Worksheets/, etc.)

## Output Structure

For each workbook, you'll get:

```
<output_folder>/<workbook_name>/
├── workbook.xml           # Main workbook definition
├── Datasources/           # Data source definitions (.tds files)
├── Worksheets/            # Worksheet definitions (.xml files)
├── Dashboards/            # Dashboard definitions (.xml files)
├── Data/
│   ├── Extracts/          # Extract files (.hyper, .tde, etc.)
│   └── Datasources/       # Cached datasource data
└── [Content_Types].xml    # ZIP manifest
```

## Next Steps

After extraction:

1. **Inspect workbook.xml** – See the overall structure and dashboard layout
2. **Check Datasources/** – Identify data sources, connections, and calculated fields
3. **Review Worksheets/** – See field definitions, filters, and calculations
4. **Check parameters** – Look for published parameters in the workbook

For deeper analysis, you can then:
- Run the `detect-tableau-archetypes` skill to auto-detect the workbook archetype
- Run the `generate-tableau-domains` skill to generate ConvBI metadata

## Notes

- Extraction is **read-only** – no files are modified
- Large extracts may take a few seconds
- Binary files (images, fonts) are included but may not be human-readable
- To decode datasource connections, inspect `.tds` files under `Datasources/`

## Troubleshooting

- **Permission denied** – Ensure write access to output folder
- **No .twbx files found** – Check that input folder contains valid .twbx files
- **Extraction fails** – Some .twbx files may be corrupted; check file integrity
