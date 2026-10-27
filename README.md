# t2p_util — Tableau → Power BI Migration Accelerator

> **Status: planned / pre-alpha.** This repository currently contains the design and roadmap for the accelerator. Nothing described below is implemented yet. Command-line examples show the *planned* interface and will change as the code lands.

`t2p_util` is an accelerator for migrating Tableau content to Power BI. It reads Tableau workbooks (`.twb` / `.twbx`) and Tableau Server / Cloud, assesses what you have, and generates a **Power BI Project (PBIP)** containing a TMDL semantic model and a report definition. Alongside the PBIP it emits a vendor-neutral **Apache Ossie** semantic model file for each Tableau data source, and a **correct DAX translation of every calculation it can translate**, with the rest flagged for human review.

It is built to be rules-first and auditable. Deterministic translation handles what it can. A Claude-assisted fallback handles the long tail (LODs, table calculations), and every LLM-produced item is forced into human review.

---

## Table of contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Scope](#2-scope)
3. [High-level architecture](#3-high-level-architecture)
4. [Inputs](#4-inputs)
5. [Outputs](#5-outputs)
6. [Pipeline stages](#6-pipeline-stages)
7. [Calculation translation (Tableau → DAX)](#7-calculation-translation-tableau--dax)
8. [Data source and model translation](#8-data-source-and-model-translation)
9. [Visual and dashboard translation](#9-visual-and-dashboard-translation)
10. [Apache Ossie (OSI) export](#10-apache-ossie-osi-export)
11. [LLM assistance](#11-llm-assistance)
12. [Assessment and reporting](#12-assessment-and-reporting)
13. [Validation](#13-validation)
14. [Human review workflow](#14-human-review-workflow)
15. [Planned CLI](#15-planned-cli)
16. [Configuration](#16-configuration)
17. [Planned repository layout](#17-planned-repository-layout)
18. [Prerequisites](#18-prerequisites)
19. [Known limitations and risks](#19-known-limitations-and-risks)
20. [Roadmap](#20-roadmap)
21. [Decisions log and open questions](#21-decisions-log-and-open-questions)
22. [Large packaged workbooks and version support](#22-large-packaged-workbooks-and-version-support)
23. [Theme and branding assistant](#23-theme-and-branding-assistant)
24. [Row-level security handling](#24-row-level-security-handling)
25. [Contributing and license](#25-contributing-and-license)
26. [References](#26-references)

---

## 1. Goals and non-goals

### Goals

- **Cut migration effort** by automating the repetitive, mechanical parts: inventory, data model recreation, calculation translation, and report scaffolding.
- **Produce source-control-friendly output** (PBIP: TMDL + report JSON) that opens in Power BI Desktop and diffs cleanly in Git.
- **Translate calculations correctly, not just plausibly.** Every translation carries a status (auto / needs review / manual) and a confidence score, and is verifiable by the validation suite.
- **Emit a vendor-neutral semantic model** (Apache Ossie YAML) per data source, so the business definitions are portable and not locked into one BI tool.
- **Be honest about gaps.** Anything not translated cleanly is reported with the reason, never silently approximated.

### Non-goals

- Pixel-perfect visual cloning. Power BI and Tableau have different visual grammars; the goal is functionally equivalent, reviewable reports.
- Migrating Tableau Prep flows, Tableau Server administration (permissions, schedules, subscriptions) or Tableau extensions in v1. These are listed in [Roadmap](#20-roadmap) as later phases.
- Guaranteeing zero manual work. The accelerator targets the bulk of the effort; complex dashboards will still need an analyst.

---

## 2. Scope

| Area | In scope | Notes |
|---|---|---|
| Workbooks / dashboards | Yes | Worksheets, dashboards, filters, parameters, actions, layout containers → report pages and visuals |
| Data sources & models | Yes | Live connections, extracts, embedded data, joins, relationships, custom SQL → TMDL tables, relationships and Power Query M |
| Calculated fields | Yes | Row-level, aggregate, LOD, table calculations, parameters → DAX measures / columns / Power Query M |
| Apache Ossie export | Yes | One Ossie YAML semantic model per Tableau data source |
| Assessment & reporting | Yes | Inventory, complexity scoring, readiness report, effort estimate, post-migration validation report |
| Theme and branding | Yes | Client-driven theme suggestions (colour palettes, Power BI theme JSON) — see [section 23](#23-theme-and-branding-assistant) |
| Row-level security | Detect and flag in v1 | See [section 24](#24-row-level-security-handling) |
| Tableau Prep flows | Later | Not in v1 |
| Server administration (permissions, schedules, subscriptions) | Later | Inventory only in v1 |

Source data platforms expected in practice: Snowflake, Databricks, SQL Server / Azure SQL / Synapse, Oracle, PostgreSQL, MySQL, Redshift, BigQuery, Excel / CSV, Tableau extracts (`.hyper`), and data embedded in `.twbx` packages.

---

## 3. High-level architecture

```
        ┌──────────────────────┐        ┌───────────────────────────┐
        │ Local .twb / .twbx   │        │ Tableau Server / Cloud     │
        │ (XML + packaged data)│        │ REST + Metadata API        │
        └──────────┬───────────┘        └─────────────┬─────────────┘
                   └────────────────┬─────────────────┘
                                    ▼
                         ┌─────────────────────┐
                         │  1. Ingest & parse  │
                         └──────────┬──────────┘
                                    ▼
                    ┌───────────────────────────────┐
                    │ 2. Intermediate representation │   (workbook, data sources,
                    │    (typed, serialisable)       │    fields, calcs, sheets, dashboards)
                    └───────┬───────────────┬───────┘
                            ▼               ▼
              ┌──────────────────┐   ┌──────────────────────┐
              │ 3. Assessment    │   │ 4. Translators        │
              │ inventory/score  │   │ calc · model · visual │
              └────────┬─────────┘   └───────┬───────────────┘
                       │                     ├──► Rules engine (deterministic)
                       │                     └──► LLM fallback (Claude), always flagged
                       ▼                     ▼
              ┌──────────────────┐   ┌───────────────────────────────┐
              │ Reports (md/html │   │ 5. Emitters                   │
              │ /xlsx)           │   │ PBIP (TMDL + report), Ossie   │
              └──────────────────┘   │ YAML, extracted data (Parquet)│
                                     └───────────────┬───────────────┘
                                                     ▼
                                     ┌───────────────────────────────┐
                                     │ 6. Validation & review        │
                                     │ DAX checks · data parity ·    │
                                     │ visual parity · sign-off      │
                                     └───────────────────────────────┘
```

Key design decision: **a typed intermediate representation (IR) sits between Tableau and every output.** Parsers produce the IR; translators and emitters consume it. This keeps Tableau-specific parsing separate from Power BI-specific generation, lets the Ossie export reuse the same data model, and makes each stage independently testable.

---

## 4. Inputs

### 4.1 Local `.twb` / `.twbx` files

- `.twb` is XML. `.twbx` is a zip containing the `.twb` plus packaged assets (extracts as `.hyper`, local files, images, custom shapes).
- The parser reads datasources, columns, calculations, worksheets (shelves, marks, filters, encodings), dashboards (zones, containers, actions), parameters and formatting.
- Needs no Tableau Server. Best for pilots, single workbooks and offline analysis.
- **One workbook is processed per run.** Packaged workbooks may be several GB, so the pipeline streams from the zip and never loads a whole package into memory (see [section 22](#22-large-packaged-workbooks-and-version-support)).
- **All Tableau versions are in scope.** The parser is version-aware and degrades gracefully on unknown XML (see section 22).

### 4.2 Tableau Server / Tableau Cloud

- **REST API**: enumerate sites, projects, workbooks, data sources, views, owners; download workbooks and published data sources.
- **Metadata API (GraphQL)**: lineage, upstream tables and columns, field-level dependencies, and usage, useful for prioritising what to migrate.
- Authentication via personal access token (PAT). Credentials are read from environment variables or a secrets manager, never committed or written to reports.
- Bulk inventory mode produces the portfolio-level assessment before any conversion.

### 4.3 Published data sources

Workbooks that reference published data sources are resolved by downloading the data source definition (`.tds` / `.tdsx`) and treating it as a first-class input. Where a published data source feeds many workbooks, it is migrated once into a shared semantic model and the workbooks are pointed at it.

---

## 5. Outputs

Each run migrates **one workbook** and creates a **new, self-contained output folder** with a fixed structure. Nothing is written into the input location. Every PBIP is independent: it owns its own data sources, date table and theme, and shares nothing with other PBIPs.

```
out/<client>/<workbook>/
├── run.yaml                            # Run manifest: inputs (with hashes), tool version, config, timings
├── 01_extracted/                       # Tableau content as definite YAML (the IR; see below)
│   ├── workbook.yaml
│   ├── datasources/<datasource>.yaml
│   ├── calculations/<datasource>.yaml
│   ├── parameters.yaml
│   ├── worksheets/<worksheet>.yaml
│   ├── dashboards/<dashboard>.yaml
│   └── security.yaml                   # Detected RLS / user filters (flag only in v1)
├── 02_assessment/                      # Inventory, complexity, readiness (md/html/yaml)
├── 03_translation/                     # Per-item translation records as YAML + calculations.csv
│   ├── calculations.yaml               # formula, classification, DAX, Ossie SQL, method, confidence, status
│   ├── model.yaml
│   └── visuals.yaml
├── 04_powerbi/                         # The deliverable Power BI Project
│   ├── <workbook>.pbip                 # Project pointer file
│   ├── <workbook>.SemanticModel/       # TMDL semantic model (incl. its own date table)
│   │   └── definition/                 #   tables/, relationships, expressions, model
│   ├── <workbook>.Report/              # Report definition (PBIR)
│   │   └── definition/                 #   pages/, visuals/, bookmarks, report config
│   ├── themes/                         # Selected Power BI theme JSON (see section 23)
│   └── data/                           # Extracted packaged data (Parquet/CSV) where needed
├── 05_ossie/
│   └── <datasource>.ossie.yaml         # Apache Ossie semantic model (one per data source)
├── 06_validation/                      # DAX/model checks, data parity, visual parity results
├── 07_review/
│   ├── review-queue.md                 # Items needing human attention
│   ├── decisions.yaml                  # Reviewer decisions and sign-off trail
│   └── mapping.yaml                    # Tableau object → Power BI object trace
└── logs/
```

Notes:

- **Everything that is extracted is stored as definite, schema-checked YAML.** The YAML files are the single source of truth between stages: they can be inspected, diffed, hand-edited and re-fed to later stages, and they are what the LLM tooling reads and writes.
- **Assumption to confirm:** reports connect to the **original source systems** by default (connections are parameterised in Power Query), while data that only exists inside the `.twbx` package is extracted into `04_powerbi/data/`. Repointing to a new location (e.g. a lakehouse) is a parameter change, not a rebuild.

- PBIP is the target because it is plain text, Git-friendly and can be generated as code. Verify the current status of the PBIR report format against Microsoft's documentation before relying on it; the generator should be able to fall back to the legacy report format if needed.
- `.ossie.yaml` is a **project naming convention**, not something the Ossie spec mandates.

---

## 6. Pipeline stages

1. **Ingest.** Load `.twb` / `.twbx` or pull from Server / Cloud. Unpack packages into an isolated working directory.
2. **Parse to IR.** Build typed objects: `Workbook`, `DataSource`, `Connection`, `Table`, `Field`, `Calculation`, `Parameter`, `Worksheet`, `Dashboard`, `Action`.
3. **Resolve dependencies.** Build the calculation dependency graph (calc → calc → column), detect cycles, resolve parameter references and calculations that reference sheet context.
4. **Assess.** Inventory, complexity scoring, and readiness classification (see [section 12](#12-assessment-and-reporting)).
5. **Translate.** Calculations, data model, and visuals via the rules engine first, then the LLM fallback for unmapped items.
6. **Emit.** Write the PBIP, the Ossie YAML, extracted data and review artefacts.
7. **Validate.** Syntax checks, data parity, visual parity.
8. **Review.** Humans resolve flagged items; sign-off is recorded.

---

## 7. Calculation translation (Tableau → DAX)

This is the highest-risk and highest-value part of the accelerator. Every Tableau calculation must end up as one of: a DAX **measure**, a DAX **calculated column**, a **Power Query M** step, or an explicit **manual** item.

### 7.1 Classify before translating

The first decision is not "what is the DAX for this?" but **"is this calculation row-level or aggregate?"**

| Tableau calc type | Typical Power BI target |
|---|---|
| Row-level (no aggregation), e.g. `[Sales] - [Cost]` | Power Query M column (preferred for source-side shaping) or DAX calculated column |
| Aggregate, e.g. `SUM([Sales]) / SUM([Cost])` | DAX measure |
| Mixed (aggregate wrapped around row-level logic) | DAX measure using an iterator (`SUMX`, `AVERAGEX`, …) |
| LOD (`FIXED`, `INCLUDE`, `EXCLUDE`) | DAX measure using `CALCULATE` and filter modifiers (see below) |
| Table calculation (`WINDOW_*`, `RUNNING_*`, `LOOKUP`, `RANK`, `INDEX`, `TOTAL`, …) | DAX measure using `WINDOW`, `OFFSET`, `INDEX`, `RANKX` or `CALCULATE` patterns, depending on sheet context |
| Parameter | Power BI field parameter or what-if parameter table |
| Set, group, bin | Calculated column / table, or a grouping in the model |

### 7.2 Function mapping

A curated mapping table drives the rules engine. Each entry is versioned, carries unit tests, and states any caveats. A starting sample (to be verified by tests before being marked "auto"):

| Tableau | DAX | Caveat |
|---|---|---|
| `COUNTD(x)` | `DISTINCTCOUNT(x)` | |
| `ZN(x)` / `IFNULL(x, y)` | `COALESCE(x, 0)` / `COALESCE(x, y)` | |
| `CONTAINS(s, sub)` | `CONTAINSSTRING(s, sub)` | Check case sensitivity expectations |
| `ATTR(x)` | `IF(HASONEVALUE(x), VALUES(x))` | Tableau's `*` for multiple values has no direct equivalent |
| `DATETRUNC('month', d)` | `STARTOFMONTH` / date-table or `DATE(YEAR(d), MONTH(d), 1)` | Prefer a proper date table |
| `IF … THEN … ELSEIF … END` | `IF(…)` / `SWITCH(TRUE(), …)` | |
| `CASE … WHEN` | `SWITCH(…)` | |
| `REGEXP_*` | **No DAX equivalent** | Push to Power Query M or flag for manual |

The full table lives in `rules/` once implemented; this README samples it only.

### 7.3 Level of detail (LOD) expressions

LODs are not a syntax swap. Tableau's order of operations and Power BI's filter-context evaluation differ.

| Tableau | Typical DAX shape | Review note |
|---|---|---|
| `{ FIXED [Region] : SUM([Sales]) }` | `CALCULATE(SUM(Sales[Sales]), REMOVEFILTERS(), VALUES(Sales[Region]))` or `ALLEXCEPT` | `FIXED` ignores dimension filters but **respects context filters** and data-source filters; Power BI has no direct "context filter" concept, so this is flagged for review rather than claimed as exact |
| `{ INCLUDE [Customer] : SUM([Sales]) }` | Iterator over `SUMMARIZE` / `VALUES` of the extra dimension | Depends on the viz's level of detail |
| `{ EXCLUDE [Region] : SUM([Sales]) }` | `CALCULATE(SUM(...), REMOVEFILTERS(Sales[Region]))` | Depends on the viz's level of detail |

### 7.4 Table calculations depend on the sheet

`WINDOW_SUM`, `RUNNING_SUM`, `LOOKUP`, `RANK`, `INDEX` and friends are evaluated over the worksheet's **partitioning and addressing** ("compute using"). The same formula can mean different things on different sheets. The translator therefore reads the worksheet (shelves, sort, compute-using setting) as well as the formula, and may emit one DAX measure per distinct context or flag the calc as needing manual design. These map to DAX `WINDOW`, `OFFSET`, `INDEX`, `RANKX` and related patterns.

### 7.5 Every translation is recorded

`review/calculations.csv` has one row per calculation:

| Column | Meaning |
|---|---|
| `workbook`, `datasource`, `name` | Where the calc lives |
| `tableau_formula` | Original, untouched |
| `classification` | row-level / aggregate / LOD-fixed / LOD-include / LOD-exclude / table-calc / parameter |
| `target` | measure / calculated column / M step / manual |
| `dax` | Generated DAX (or empty) |
| `ossie_sql` | Generated Ossie SQL expression (or empty) |
| `method` | rules / llm / manual |
| `confidence` | 0–1 |
| `status` | auto / needs-review / manual |
| `notes` | Why it was flagged; assumptions made |

Calculations are also written into the semantic model with their original Tableau formula preserved as a TMDL description or annotation, so reviewers see both side by side in Power BI Desktop.

---

## 8. Data source and model translation

- **Connections.** Each Tableau connection becomes Power Query M. Connector mapping covers Snowflake, Databricks, SQL Server / Azure SQL / Synapse, Oracle, PostgreSQL, MySQL, Redshift, BigQuery, Excel and CSV. Credentials and server details are parameterised, never embedded.
- **Joins and relationships.** Tableau's logical-layer relationships map to Power BI relationships (cardinality, cross-filter direction). Physical-layer joins are translated to M merges or, where possible, pushed into the source query. Join types that don't map cleanly (e.g. full outer, complex join conditions) are flagged.
- **Custom SQL.** Carried across as native queries, with a warning about query folding and dialect portability.
- **Embedded data in `.twbx`.** Power BI cannot read Tableau `.hyper` files directly. The pipeline extracts `.hyper` content using the Tableau Hyper API, writes **Parquet (or CSV)**, and generates M that points at those files. Loose Excel / CSV files packaged in the `.twbx` are handled the same way. The extracted files are written under `data/` and referenced by a parameterised folder path.
- **Extracts vs. live.** Extract refresh schedules are not migrated; the report lists them so Power BI refresh can be configured.
- **Date handling.** Each PBIP gets **its own date table**, generated inside its semantic model and marked as a date table, since most time-intelligence patterns in DAX depend on one. Nothing is shared across PBIP projects or across Tableau workbooks.
- **Hierarchies, folders, formats, default aggregations, sort-by columns, descriptions** are carried over where Power BI has an equivalent.

---

## 9. Visual and dashboard translation

- **Worksheet → visual.** Mark type, shelves and encodings map to a Power BI visual type and field wells. Examples: bar / line / area / scatter / map / table / matrix / KPI card. Unsupported mark combinations (e.g. highly customised dual-axis charts) are flagged.
- **Dashboard → report page.** Zones and containers map to page layout; floating and tiled layouts are approximated with coordinates.
- **Filters and parameters.** Worksheet / dashboard filters become visual / page / report-level filters or slicers. Parameters become field parameters or what-if parameters.
- **Actions.** Filter actions map to cross-filtering and cross-highlighting; URL actions map to URL buttons; highlight actions map to cross-highlighting. Parameter actions and set actions are flagged.
- **Formatting.** Colours, fonts, titles and number formats are carried over as a theme plus per-visual formatting where practical.
- **Coverage report.** Every worksheet and dashboard gets a visual-mapping coverage line: mapped / approximated / not mapped, with reasons.

---

## 10. Apache Ossie (OSI) export

[Apache Ossie](https://ossie.apache.org/) (incubating; formerly **Open Semantic Interchange, OSI**) is a vendor-agnostic JSON/YAML specification for exchanging semantic models. The accelerator emits one Ossie semantic model per Tableau data source, so the business logic is captured in a portable form and is not only expressed as Power BI DAX.

> **Spec version.** The accelerator targets Ossie spec `0.2.0.dev0`, which is **a draft** and may change before release. The exact spec lives in the `core-spec/` folder of the Ossie repository; the generator pins a version and tracks upstream changes.

### 10.1 What gets exported

| Tableau | Ossie |
|---|---|
| Data source / logical table | `datasets` (`name`, `source`, `primary_key`, `unique_keys`) |
| Dimension / column / row-level calc | `fields` (`name`, `expression`, `datatype`, `dimension.is_time`, `label`, `description`) |
| Aggregate calc / measure | `metrics` (dataset-scoped, or model-scoped for cross-dataset metrics) |
| Relationship | `relationships` (`from`, `to`, `from_columns`, `to_columns`) |
| Field comments, aliases, synonyms | `description` and `ai_context` (`instructions`, `synonyms`, `examples`) |

### 10.2 Expressions need two translation targets

Ossie defines only one dialect with fixed semantics, `OSSIE_SQL_2026`. Each Tableau calculation therefore has up to **two** outputs:

1. **DAX**, for the Power BI semantic model.
2. **Ossie SQL**, for the Ossie file.

Many Tableau calculations (LODs, table calculations, parameter-driven logic) have **no clean SQL-metric equivalent**. For these the exporter:

- keeps the original Tableau formula and the generated DAX in `custom_extensions` (`vendor_name: TABLEAU` and `vendor_name: POWERBI`),
- leaves the portable `sql` expression out if it cannot be expressed faithfully,
- and marks the item for review in `review/calculations.csv`.

### 10.3 Illustrative output (shape only)

```yaml
version: 0.2.0.dev0
name: sales_analytics
datasets:
  - name: orders
    source: sales.public.orders
    primary_key: [order_id]
    fields:
      - name: order_date
        datatype: Date
        dimension:
          is_time: true
        expression:
          dialects:
            - dialect: OSSIE_SQL_2026
              expression: order_date
    metrics:
      - name: total_amount
        datatype: Decimal
        expression:
          dialects:
            - dialect: OSSIE_SQL_2026
              expression: SUM(amount)
        custom_extensions:
          - vendor_name: TABLEAU
            data: '{"formula": "SUM([Amount])"}'
          - vendor_name: POWERBI
            data: '{"dax": "SUM(orders[amount])"}'
relationships:
  - name: orders_to_customers
    from: orders
    to: customers
    from_columns: [customer_id]
    to_columns: [id]
```

Generated files are validated against the Ossie schema using the validation tooling published in the Ossie repository.

---

## 11. LLM assistance

The accelerator is **rules-first**. A deterministic engine translates everything it has a tested rule for. Claude is used only for what remains: unmapped functions, complex LODs, context-dependent table calculations, and expression explanations for reviewers.

Principles:

- **Rules before LLM.** The LLM never overrides a rule-based translation.
- **Everything the LLM produces is flagged `needs-review`**, with a confidence score and the model's stated assumptions. Nothing LLM-generated is marked `auto`.
- **Grounded prompts.** The prompt includes the calc's formula, its dependency chain, the relevant column types and relationships, the worksheet context (for table calcs) and the target dialect (DAX or Ossie SQL).
- **Verified outputs.** Generated DAX must pass the syntax / semantic checks in [section 13](#13-validation) before it is written into the model. Failures are retried with the error message, then escalated to `manual`.
- **No hard-coded model.** The model is selected by configuration (`T2P_LLM_MODEL`); the repository does not pin a model ID.
- **Fully usable without an LLM.** With the LLM disabled, unmapped items become `manual` entries with the original formula and a TODO, so the tool still runs offline.
- **Data policy.** There is no restriction on sending formulas and metadata to the LLM, and the LLM tooling reads and writes the extracted YAML files directly. Row-level data is still never sent: only formulas, schema and other metadata. A `--no-llm` flag and an optional redaction setting for table / column names remain available for stricter clients.

---

## 12. Assessment and reporting

Run before converting anything, individually or across an entire Tableau site.

**Inventory**

- Sites, projects, workbooks, data sources, sheets, dashboards, calculated fields, parameters, filters, actions, extracts vs. live connections, connector types
- Owners, last-modified dates, view counts (Server / Cloud) to identify dead content

**Complexity scoring** (configurable weights) based on, for example:

- Number and type of calculations (LOD and table calcs weigh more)
- Unsupported or approximated visual types
- Number of data sources, custom SQL, blended data
- Dashboard actions and parameter usage
- Dependency depth

**Readiness classification**

| Class | Meaning |
|---|---|
| Green | Expected to convert largely automatically |
| Amber | Convertible with review of flagged items |
| Red | Needs redesign or substantial manual work |

**Reports.** Per-workbook and portfolio-level, in Markdown / HTML / XLSX: readiness, effort estimate, risk list, translation coverage, post-migration validation results and the remaining review queue.

---

## 13. Validation

Validation needs access to **both** sides (Tableau and Power BI) for the parity checks. The syntax checks need only the generated model.

| Check | What it does | Needs |
|---|---|---|
| **DAX / model syntax and semantics** | Loads the generated model and validates DAX, TMDL and best-practice rules | Tabular Editor CLI (Best Practice Analyzer) and/or a Power BI / Fabric workspace via XMLA |
| **Ossie schema validation** | Validates generated `.ossie.yaml` against the Ossie schema | Ossie validation tooling |
| **Data parity** | Runs equivalent queries in Tableau and Power BI for selected measures and dimensions, compares results within a tolerance, and reports differences | Tableau query access (e.g. Tableau Server data or the original source) and Power BI `executeQueries` / XMLA |
| **Visual parity** | Renders the Tableau view and the Power BI page, then produces side-by-side images and a mapping coverage report | Both services reachable for rendering; manual capture is a fallback |
| **Row / aggregate reconciliation** | Compares row counts and key totals between source, Tableau and Power BI | Source and report access |

Results are written to `reports/validation.md|html` and update the status of each item in the review queue.

---

## 14. Human review workflow

Each migrated object (calculation, relationship, visual, parameter) carries one of three statuses:

- **auto**: rule-based translation with a passing check.
- **needs-review**: translated, but with an LLM, an assumption, a semantic difference (e.g. LOD / context-filter behaviour) or a failed parity check.
- **manual**: no safe translation; original definition preserved with a TODO.

The review queue (`review/review-queue.md` and CSV) lists items by priority. Reviewers record a decision (accept / edit / reject), who made it and when, and the final report includes that sign-off trail. Overrides are stored in a project-level rules file and reused on the next run, so a decision made once is not asked again.

---

## 15. Planned CLI

> These commands show the **planned** interface. They are not implemented yet and the names may change.

```bash
# Assess one workbook or a folder of workbooks
t2p assess ./workbooks --out ./out/assessment

# Assess a whole Tableau site (needs credentials via env vars)
t2p assess --server https://tableau.example.com --site my-site --out ./out/assessment

# Convert a workbook to a PBIP project, an Ossie file and review artefacts
t2p convert ./workbooks/sales.twbx --out ./out/sales

# Same, without any LLM calls
t2p convert ./workbooks/sales.twbx --out ./out/sales --no-llm

# Translate a single calculation (handy while developing rules)
t2p calc "{ FIXED [Region] : SUM([Sales]) }" --context ./out/sales/review/mapping.json

# Export only the Ossie semantic model
t2p ossie ./workbooks/sales.twbx --out ./out/sales/ossie

# Validate generated output
t2p validate ./out/sales --checks dax,ossie,parity,visual
```

---

## 16. Configuration

Configuration is a YAML file plus environment variables. Secrets are only ever read from the environment (or a secrets manager).

| Setting | Purpose |
|---|---|
| `TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` | Tableau Server / Cloud access (PAT) |
| `ANTHROPIC_API_KEY` | Claude API access (only if LLM assistance is enabled) |
| `T2P_LLM_MODEL` | Which Claude model to use; deliberately not hard-coded |
| `T2P_LLM_ENABLED` | Enable / disable LLM fallback |
| `T2P_CLIENT` | Client name; used for the output folder and for theme research (section 23) |
| `T2P_WORK_DIR` | Scratch location for unpacking large `.twbx` files (needs room for the extracted data) |
| `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_CLIENT_SECRET` | Power BI / Fabric service principal for validation and optional publish |
| `t2p.yaml` | Connector mappings, complexity weights, naming conventions, override rules, type mappings |

---

## 17. Planned repository layout

```
t2p_util/
├── README.md
├── pyproject.toml
├── src/t2p/
│   ├── ingest/          # twb/twbx parsing, Server/Cloud clients
│   ├── ir/              # typed intermediate representation
│   ├── assess/          # inventory, scoring, readiness
│   ├── translate/
│   │   ├── calc/        # classifier, rules engine, DAX + Ossie SQL generation
│   │   ├── model/       # connections, joins, relationships → M / TMDL
│   │   └── visual/      # worksheets/dashboards → report definition
│   ├── llm/             # Claude fallback, prompts, guardrails
│   ├── theme/           # brand research, palette suggestion, theme JSON, contrast checks
│   ├── emit/            # PBIP, TMDL, report JSON, Ossie YAML, Parquet extraction
│   ├── validate/        # DAX checks, parity, visual diff
│   └── cli/
├── rules/               # versioned function/pattern mapping tables
├── tests/
│   ├── unit/
│   └── fixtures/        # sample workbooks (synthetic only)
└── docs/
```

---

## 18. Prerequisites

Planned requirements (to be confirmed as implementation proceeds):

- Python 3.11+
- Power BI Desktop (to open and inspect generated PBIP projects)
- Tabular Editor CLI (DAX / model validation)
- Tableau Hyper API (`tableauhyperapi`) for extracting packaged `.hyper` data
- Network access to Tableau Server / Cloud (for server mode and parity checks)
- A Power BI / Fabric workspace and service principal (for XMLA-based validation and optional publishing)
- Optional: Anthropic API key (for LLM fallback)

---

## 19. Known limitations and risks

- **Semantic differences are real.** Tableau's order of operations (extract filters → data-source filters → context filters → dimension filters → …) and Power BI's filter context do not line up one-to-one. Equivalent-looking formulas can return different numbers. This is why parity testing and human review are core features, not extras.
- **Table calculations** are tied to a specific sheet layout and may need several DAX measures or manual redesign.
- **Visual fidelity.** Some Tableau visuals have no Power BI equivalent; approximations are reported.
- **Report format maturity.** Power BI's enhanced report format (PBIR) and tooling are evolving; the generator must be checked against current Microsoft documentation.
- **Ossie spec is a draft** (`0.2.0.dev0`) and may change.
- **`.hyper` extraction** requires the Hyper API and enough disk space; very large extracts may be better re-sourced from the original system.
- **LLM output is never trusted blindly.** It is always reviewed and validated.
- **Security and privacy.** Workbooks can contain connection strings and sensitive field names. Secrets must not be committed; reports must redact credentials.

---

## 20. Roadmap

| Phase | Deliverable |
|---|---|
| **0. Foundations** | Repo scaffolding, IR, `.twb` / `.twbx` parser, synthetic fixture workbooks |
| **1. Assessment** | Inventory and complexity scoring for local files; readiness reports |
| **2. Calculations** | Classifier, function mapping rules with tests, row-level and aggregate calcs → DAX; calc report |
| **3. Model** | Connections, joins, relationships → TMDL + Power Query M; `.hyper` → Parquet extraction; first end-to-end PBIP that opens in Power BI Desktop |
| **4. Ossie export** | Per-data-source Ossie YAML with schema validation |
| **5. Advanced calcs** | LOD and table-calculation translation, LLM fallback with review gating |
| **6. Visuals** | Worksheet → visual and dashboard → page mapping, filters, parameters, actions |
| **6b. Theme assistant** | Client-driven palette research and Power BI theme generation (section 23) |
| **7. Server / Cloud** | REST + Metadata API ingest; portfolio-level assessment |
| **8. Validation** | DAX / model checks, data parity, visual parity, review workflow and sign-off reports |
| **9. Later** | Tableau Prep flows, publishing to Fabric, permissions mapping, additional connectors |

---

## 21. Decisions log and open questions

### Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Tableau versions | **All versions** are in scope (see [section 22](#22-large-packaged-workbooks-and-version-support)) |
| 2 | Volume and size | **One workbook per run.** Packaged workbooks can be **several GB**, so processing is streaming and disk-aware |
| 3 | Power BI / Fabric workspace and service principal | Not available locally yet but can be arranged. Until then, only offline checks run (TMDL/DAX syntax, Ossie schema); data and visual parity are enabled once access exists |
| 4 | Output location | Each run creates a **new output folder with a defined structure** ([section 5](#5-outputs)). Reports connect to the original sources by default (assumption to confirm) |
| 5 | Row-level security | Detect and flag in v1; see [section 24](#24-row-level-security-handling). Awaiting confirmation |
| 6 | LLM data policy | No restriction on formulas and metadata. All extracted content is stored as **definite YAML files** that the LLM tooling reads and writes. Row-level data is never sent |
| 7 | Theme / naming standard | None fixed. The **theme is driven by user input** (for example a client name), with researched suggestions ([section 23](#23-theme-and-branding-assistant)) |
| 8 | Date table | **One per PBIP**, inside its own semantic model; nothing shared between PBIPs or workbooks |

### Still open

1. **Row-level security:** does any workbook use user filters or `USERNAME()` / `ISMEMBEROF()`? If so, should v1 translate them to Power BI RLS roles, or only flag them?
2. **Output connections:** confirm that reports should reference the original source systems (with packaged-only data extracted into the output folder), and not a new central store.
3. **Service principal:** who provides the Power BI / Fabric workspace and when? This gates data and visual parity checks.
4. **Hardware limits:** how much disk and memory does the machine running multi-GB workbooks have?
5. **Which sources hold the embedded data** in the large `.twbx` files (extracts vs. loose files)? This decides how the Parquet conversion is sized.
6. **Brand usage:** for client themes, who approves a final palette, and are there client brand guidelines to supply directly?

---

## 22. Large packaged workbooks and version support

### 22.1 Multi-GB `.twbx` files

A `.twbx` is a zip. In multi-GB packages almost all of the size is packaged data (`.hyper` extracts and files), not the workbook definition.

- **Definition first.** Only the `.twb` XML (typically a few MB at most) is read for analysis, assessment, calculation translation and visual translation. This is done by streaming directly from the zip, without unpacking everything.
- **Data on demand.** Packaged data is only unpacked when the data stage runs, into `T2P_WORK_DIR`, one table at a time, and converted to Parquet in chunks, so memory stays bounded.
- **Disk pre-check.** Before unpacking, the tool reports the expected disk need and refuses to start if there is not enough space.
- **Resumable.** The run manifest (`run.yaml`) records completed stages and file hashes, so an interrupted run resumes and does not repeat finished work.
- **Skip or defer data.** `--no-data` produces the model, calculations and report without extracting packaged data, which is useful for quick iterations on big files.
- **Very large extracts** may be better re-sourced from the original system than copied into Parquet; the assessment flags these.

### 22.2 All Tableau versions

Tableau workbook XML has changed across releases (for example `.tds`/`.twb` structure, the introduction of the relationships/logical layer in newer versions, and `.hyper` replacing `.tde` extracts).

- The parser reads the workbook's declared version attributes and selects version-specific handling.
- Older workbooks that use the legacy physical-layer-only data model, `.tde` extracts or deprecated features are supported on a best-effort basis. `.tde` extracts cannot be read directly and will need re-extraction or re-sourcing; they are flagged.
- **Unknown or unsupported XML is never dropped silently.** It is preserved in the extracted YAML as raw nodes and listed in the assessment as "unparsed".
- A **fixture corpus** of synthetic workbooks across versions is part of the test suite. The real version range actually supported will be stated in the release notes, backed by those tests, since "all versions" can only be claimed for what has been tested.

---

## 23. Theme and branding assistant

The theme is not fixed. It comes from the **user's input**, for example the client's name.

### 23.1 Flow

1. The user supplies a client or brand name (and optionally brand guideline documents, logo files or hex colours).
2. The assistant researches the publicly available brand identity of that client: primary and secondary colours, typography and tone.
3. It proposes **several candidate themes** (for example a primary-brand theme, a neutral/light theme and a dark theme), each with a categorical palette, a sequential and a diverging palette, background, foreground and accent colours.
4. Each candidate is checked for **accessibility** (WCAG contrast ratios for text and data labels) and for **colour-blind safety** of the categorical palette. Colours that fail are adjusted, and the adjustment is reported.
5. The user previews and picks one (or edits it). The choice is recorded.
6. The assistant emits a **Power BI theme JSON** (`04_powerbi/themes/`) and applies it to the report definition. The chosen palette and its rationale are stored as YAML in `07_review/decisions.yaml`.

### 23.2 Notes

- The research uses public information, and the output is a **suggestion**. Official brand guidelines supplied by the client always override researched values.
- Brand logos, wordmarks and trademarked assets are **not** generated or embedded unless the client supplies them.
- Without network access or a brand name, a neutral default theme is used.

---

## 24. Row-level security handling

**Row-level security (RLS)** restricts which rows of data each user can see. In Tableau it is usually implemented as a *user filter*, or a filter using functions such as `USERNAME()`, `FULLNAME()`, `USERDOMAIN()` or `ISMEMBEROF("group")`, in a calculation, a data-source filter or an entitlement table. In Power BI the equivalent is an **RLS role** with DAX filter expressions on tables (static or dynamic, using `USERPRINCIPALNAME()`), assigned to users or groups in the Power BI service.

Because a wrong translation can expose data, the default in v1 is:

- **Detect** every use of user functions and user filters during parsing, and record it in `01_extracted/security.yaml`.
- **Flag** it prominently in the assessment and mark the affected items `manual`.
- **Do not auto-publish** anything that has RLS-flagged content.
- Optionally, in a later phase, generate candidate RLS roles as `needs-review` items, validated by comparing visible rows for test users on both platforms.

---

## 25. Contributing and license

The project is at the design stage. Issues and proposals are welcome. A license has not yet been chosen; one will be added before the first release.

Contributor guidelines (test requirements for every new mapping rule, fixture policy of synthetic data only, code style) will be added with the first implementation phase.

---

## 26. References

- Apache Ossie (formerly Open Semantic Interchange): <https://ossie.apache.org/> and <https://github.com/apache/ossie> (spec in `core-spec/`)
- Power BI Projects (PBIP), TMDL and the enhanced report format (PBIR): <https://learn.microsoft.com/en-us/power-bi/developer/embedded/projects-enhanced-report-format>
- Tableau REST API, Metadata API and Hyper API documentation (Tableau developer docs)
- Tabular Editor (CLI and Best Practice Analyzer) documentation
