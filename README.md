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
21. [Open questions](#21-open-questions)
22. [Contributing and license](#22-contributing-and-license)
23. [References](#23-references)

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

### 4.2 Tableau Server / Tableau Cloud

- **REST API**: enumerate sites, projects, workbooks, data sources, views, owners; download workbooks and published data sources.
- **Metadata API (GraphQL)**: lineage, upstream tables and columns, field-level dependencies, and usage, useful for prioritising what to migrate.
- Authentication via personal access token (PAT). Credentials are read from environment variables or a secrets manager, never committed or written to reports.
- Bulk inventory mode produces the portfolio-level assessment before any conversion.

### 4.3 Published data sources

Workbooks that reference published data sources are resolved by downloading the data source definition (`.tds` / `.tdsx`) and treating it as a first-class input. Where a published data source feeds many workbooks, it is migrated once into a shared semantic model and the workbooks are pointed at it.

---

## 5. Outputs

For each migrated workbook the accelerator produces:

```
out/<workbook>/
├── <workbook>.pbip                     # Power BI Project pointer file
├── <workbook>.SemanticModel/           # TMDL semantic model
│   └── definition/                     #   tables/, relationships, expressions, model
├── <workbook>.Report/                  # Report definition (PBIR)
│   └── definition/                     #   pages/, visuals/, bookmarks, report config
├── ossie/
│   └── <datasource>.ossie.yaml         # Apache Ossie semantic model (one per data source)
├── data/                               # Extracted packaged data (Parquet/CSV) where needed
├── review/
│   ├── calculations.csv                # Tableau formula, DAX, status, confidence, notes
│   ├── review-queue.md                 # Items needing human attention
│   └── mapping.json                    # Tableau object → Power BI object trace
└── reports/
    ├── assessment.md|html
    ├── translation-coverage.md|html
    └── validation.md|html
```

Notes:

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
- **Date handling.** The accelerator can generate a shared date table and mark it as a date table, since most time-intelligence patterns in DAX depend on one.
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
- **Data minimisation.** Only formulas and schema metadata are sent, never row-level data. A `--no-llm` flag and a redaction option for table / column names are planned.

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
| **7. Server / Cloud** | REST + Metadata API ingest; portfolio-level assessment |
| **8. Validation** | DAX / model checks, data parity, visual parity, review workflow and sign-off reports |
| **9. Later** | Tableau Prep flows, publishing to Fabric, permissions mapping, additional connectors |

---

## 21. Open questions

These are open and will shape the implementation:

1. Which Tableau versions must be supported (workbook XML differs between versions)?
2. Expected workbook volume and size (affects batching, parallelism and cost of LLM use)?
3. Is a target Power BI / Fabric workspace and a service principal available for validation and publishing?
4. Should migrated reports connect to the **original source systems**, or to a new location (e.g. Fabric lakehouse)?
5. How should Tableau row-level security (user filters, entitlements) be handled in v1: flag only, or translate to Power BI RLS?
6. Are there data-residency or policy limits on sending formulas and metadata to an external LLM?
7. Is there a corporate Power BI theme or naming standard to apply to generated models and reports?
8. Is a shared date table acceptable as a default?

---

## 22. Contributing and license

The project is at the design stage. Issues and proposals are welcome. A license has not yet been chosen; one will be added before the first release.

Contributor guidelines (test requirements for every new mapping rule, fixture policy of synthetic data only, code style) will be added with the first implementation phase.

---

## 23. References

- Apache Ossie (formerly Open Semantic Interchange): <https://ossie.apache.org/> and <https://github.com/apache/ossie> (spec in `core-spec/`)
- Power BI Projects (PBIP), TMDL and the enhanced report format (PBIR): <https://learn.microsoft.com/en-us/power-bi/developer/embedded/projects-enhanced-report-format>
- Tableau REST API, Metadata API and Hyper API documentation (Tableau developer docs)
- Tabular Editor (CLI and Best Practice Analyzer) documentation
