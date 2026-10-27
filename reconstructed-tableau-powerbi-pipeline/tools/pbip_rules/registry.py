#!/usr/bin/env python3
"""The Power BI constraint register — one row per rule Desktop's editor enforces.

Why this exists
---------------
`docs/schema-reference/tableau-metadata-inventory.md` is the register for the SOURCE
side: 59 rows, every asset a Tableau workbook can carry. There has been no equivalent
register for the TARGET side, and that asymmetry is exactly where the migration loses
time. Checks in `tools/pbip_lint.py` were added one at a time, each in response to a
workbook that failed to open, so every new dashboard rediscovers a rule empirically at
a cost of two or three Desktop round-trips.

The rules being rediscovered are not in any file format. Power BI enforces correctness
in three places and only one of them is inspectable:

    JSON schemas   shape only, and `additionalProperties: false` is the whole of it
    TMDL grammar   indentation and keywords, nothing about model consistency
    EDITOR LOGIC   the actual semantics -- and it is not published

Desktop's editor never lets a human reach an invalid state: it greys out the illegal
option, renames on collision, refuses the OK button. A generator has no such guard rail,
so it writes states no human could author, and the first thing that notices is Desktop
at open time -- with a diagnostic that routinely names the wrong object (see
`check_column_references_measure`, where a calculated column referencing a measure is
reported as "a circular dependency" between two unrelated columns).

This module is that missing register, as data rather than prose. It is the tiebreaker if
the generated document and the code ever disagree, on the same principle the Tableau
inventory uses.

Honesty rules, carried over from the Tableau census
---------------------------------------------------
1. `confidence` qualifies the RULE, never a violation. `documented` means Microsoft says
   so (published schema, TOM/TMSL reference, DAX reference). `empirical` means we watched
   Desktop do it and recorded the message. `inferred` means it follows from the object
   model but nobody has confirmed the exact boundary -- an `inferred` rule must never be
   quoted as fact.
2. `status: gap` rows are as important as implemented ones. A rule nobody checks is a
   known blind spot; a rule nobody has WRITTEN DOWN is an invisible one. Every domain we
   believe exists gets a row even when no detector is planned yet.
3. `severity` is what sequences the work, not `confidence`. An `open-fail` rule stops
   fourteen dashboards dead; a `cosmetic` one never will.

Regenerate the companion document with:

    python3 tools/pbip_rules/registry.py > docs/schema-reference/pbip-constraint-rules.md
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

REGISTER_VERSION = "1.0"

# ---------------------------------------------------------------- vocabularies

# Where the rule is enforced. This decides whether a violation is loud or silent.
ENGINE = "engine"   # the AS/TOM engine rejects it -- loud, at load time
EDITOR = "editor"   # only the editor prevents it -- a script can emit it freely
SCHEMA = "schema"   # a published JSON schema rejects it -- caught by --schemas

# What a violation costs. This is the sequencing key.
OPEN_FAIL = "open-fail"        # the project does not open at all
RENDER_FAIL = "render-fail"    # it opens; a page or visual is broken
SILENT_WRONG = "silent-wrong"  # it opens and renders, showing the wrong thing
COSMETIC = "cosmetic"          # noise, not correctness

# How much to trust the rule statement itself.
DOCUMENTED = "documented"
EMPIRICAL = "empirical"
INFERRED = "inferred"

# Where the rule can be caught. Left is better: a constructive rule cannot be violated.
CONSTRUCTIVE = "constructive"  # the IR makes the invalid state unrepresentable
PRE_EMIT = "pre-emit"          # checked against the IR before anything is written
POST_EMIT = "post-emit"        # checked against files already on disk

IMPLEMENTED = "implemented"
PARTIAL = "partial"
GAP = "gap"
# Settled only against real data at refresh, never against the files. Distinct from `gap`,
# which implies a check could be written; calling these unimplemented would be misleading.
RUNTIME = "runtime"


@dataclass(frozen=True)
class Rule:
    """One constraint. `check` names the pbip_lint function that enforces it, if any."""

    id: str
    domain: str
    layer: str
    statement: str
    enforced_by: str
    severity: str
    symptom: str
    detection: str
    confidence: str
    status: str
    check: str | None = None

    @property
    def is_enforced(self) -> bool:
        return self.status in (IMPLEMENTED, PARTIAL)


SM = "Semantic Model"
RPT = "Report"
PRJ = "Project"


# ================================================================ THE REGISTER
#
# Rows with status=implemented are BANKED EVIDENCE: each one cost a real Desktop
# round-trip on a real corpus workbook, and the symptom text is what Desktop actually
# said. Those symptoms are the most valuable column in the table, because they are what
# turns an opaque Desktop message back into a rule ID.

RULES: tuple[Rule, ...] = (

    # ------------------------------------------------------ project shell
    Rule(
        id="PRJ-01",
        domain="Project shell",
        layer=PRJ,
        statement="definition.pbir and definition.pbism must declare version 4.0 or higher.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="At 1.0 the report definition may only be PBIR-Legacy (a flat report.json), "
                "so the definition/ folder we emit is ignored entirely.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_item_definition_versions",
    ),
    Rule(
        id="PRJ-02",
        domain="Project shell",
        layer=PRJ,
        statement='definition/version.json must be exactly "2.0.0".',
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom='"1.0.0" is equally schema-valid but makes Desktop apply an older required-file '
                "set that never loads definition/pages/. The report opens with the semantic model "
                "fully intact and ZERO page tabs, and no error message at all.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_report_definition_version",
    ),
    Rule(
        id="PRJ-03",
        domain="Project shell",
        layer=PRJ,
        statement="A byPath datasetReference must resolve to a sibling .SemanticModel folder "
                 "that exists.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="No model loads at all.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_dataset_reference",
    ),
    Rule(
        id="PRJ-04",
        domain="Project shell",
        layer=PRJ,
        statement="The semantic model must carry defaultPowerBIDataSourceVersion = powerBI_V3, "
                 "in whichever of TMSL/TMDL is emitted.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='Desktop loads the model as legacy PowerBI_V1 and fails with either "A data model '
                'with version 3 of metadata is required" or a null-reference crash inside its '
                'V3-assuming query-load path, surfacing as "Value cannot be null. Parameter name: '
                'Non-null assertion failure: query" -- which points nowhere near the cause.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_semantic_model_v3",
    ),
    Rule(
        id="PRJ-05",
        domain="Project shell",
        layer=PRJ,
        statement="No editor or OS droppings (.DS_Store, ~$*, *.swp) inside an item folder.",
        enforced_by=EDITOR,
        severity=COSMETIC,
        symptom="Noise at best; an unexpected file inside a definition/ folder can confuse "
                "Desktop's file correlation.",
        detection=POST_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_stray_files",
    ),
    Rule(
        id="PRJ-06",
        domain="Project shell",
        layer=PRJ,
        statement="Every $schema-declaring JSON file must validate against its own published "
                 "Microsoft schema. Those schemas are additionalProperties:false, so the emitter "
                 "must whitelist properties rather than blacklist them.",
        enforced_by=SCHEMA,
        severity=OPEN_FAIL,
        symptom='Desktop refuses to open the project when a value falls outside a closed enum -- '
                'e.g. "type": "Filter" where VisualInteractionFilterType allows only Default / '
                'DataFilter / HighlightFilter / NoFilter. Run this over EVERY workbook, not a '
                'sample: that bug survived a clean 32/32 run because the one project checked had '
                'no visualInteractions.',
        detection=POST_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_against_schemas",
    ),

    # ------------------------------------------------------ SM: naming and identity
    Rule(
        id="SM-NAME-01",
        domain="Naming and identity",
        layer=SM,
        statement="Column and measure names on a table must be unique CASE-INSENSITIVELY.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='TOM\'s name collection ignores case, so "BIC Compliance Check" and "Bic Compliance '
                'Check" are one name to it though they differ on disk. Desktop refuses to open the '
                'file: "Failed to add a deserialized Column object into the model ... Item \'...\' '
                'already exists in the collection." Seen on a real corpus workbook where a physical '
                'column collided with a calculated-field alias of itself.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_duplicate_member_names",
    ),
    Rule(
        id="SM-NAME-02",
        domain="Naming and identity",
        layer=SM,
        statement="Measure names must be unique across the ENTIRE model, not merely within "
                 "their table.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Duplicate measure name across two tables; the model refuses to deserialize. "
                "Tableau has no such constraint -- two datasources may each carry a field of the "
                "same caption -- so this fires whenever a workbook has more than one datasource.",
        detection=CONSTRUCTIVE,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_measure_names_unique_model_wide",
    ),
    Rule(
        id="SM-NAME-03",
        domain="Naming and identity",
        layer=SM,
        statement="Table names must be unique model-wide, case-insensitively.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Same collection-collision failure as SM-NAME-01, one level up.",
        detection=CONSTRUCTIVE,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_table_names_unique",
    ),
    Rule(
        id="SM-NAME-04",
        domain="Naming and identity",
        layer=SM,
        statement="Within a table, a column name must not collide with a measure name or a "
                 "hierarchy name on that same table.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Columns, measures and hierarchies share one namespace per table; the collision "
                "is rejected at deserialize.",
        detection=CONSTRUCTIVE,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_table_namespace_collisions",
    ),
    Rule(
        id="SM-NAME-05",
        domain="Naming and identity",
        layer=SM,
        statement="Names must not contain characters that break a DAX reference -- `[` and "
                 "`]` in particular -- nor any control character.",
        enforced_by=EDITOR,
        severity=OPEN_FAIL,
        symptom="`[` and `]` delimit a DAX column reference, so a name containing one can "
                "never be referenced. Checked. The editor additionally rejects a WIDER set for "
                "table names (. , ; ' ` : / \\ * | ? \" & % $ ! + = ( ) [ ] { } < >), but that "
                "is NOT checked and is not asserted here: the corpus contains `.` in both table "
                "and column names in projects that demonstrably load, so the documented list "
                "over-states what Desktop actually refuses. Pinning the real boundary needs "
                "fault injection.",
        detection=CONSTRUCTIVE,
        confidence=INFERRED,
        status=PARTIAL,
        check="check_name_characters",
    ),
    Rule(
        id="SM-NAME-06",
        domain="Naming and identity",
        layer=SM,
        statement="Names must be non-empty, must not exceed the length cap, and must carry no "
                 "leading or trailing whitespace.",
        enforced_by=EDITOR,
        severity=OPEN_FAIL,
        symptom="The editor silently trims whitespace on entry, so no human-authored model ever "
                "carries it and the failure mode is untested territory. Tableau captions derived "
                "from XML attribute text keep theirs. The 100-character cap is NOT a TOM limit -- "
                "TOM accepts longer. It is names.MAX_NAME in the builder, chosen so a long name in "
                "a deep output path stays inside the Windows 260-character path limit, so a breach "
                "is a portability defect rather than a model Desktop would reject.",
        detection=CONSTRUCTIVE,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_name_wellformed",
    ),
    Rule(
        id="SM-NAME-07",
        domain="Naming and identity",
        layer=SM,
        statement="A displayFolder is a backslash-separated path with no leading or trailing "
                 "separator and no empty segment.",
        enforced_by=EDITOR,
        severity=COSMETIC,
        symptom="A malformed path produces an unnamed or unreachable folder in the field list.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_display_folder_syntax",
    ),
    Rule(
        id="SM-NAME-08",
        domain="Naming and identity",
        layer=SM,
        statement="A name that collides with a DAX reserved word must be bracketed at every "
                 "reference site.",
        enforced_by=ENGINE,
        severity=SILENT_WRONG,
        symptom="Unbracketed, the reference parses as the keyword and the expression means "
                "something else or fails at query time.",
        detection=CONSTRUCTIVE,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_reserved_word_names",
    ),

    # ------------------------------------------------------ SM: types and formatting
    Rule(
        id="SM-TYPE-01",
        domain="Types and formatting",
        layer=SM,
        statement="Every column's dataType must be a real TOM enum member.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Nothing else catches this: `dataType: date` parses fine as TMDL and matches no "
                "closed enum any published schema could enforce. Only Desktop's own TOM "
                "deserializer knows the real enum, and it refuses the whole model rather than warn.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_valid_data_types",
    ),
    Rule(
        id="SM-TYPE-02",
        domain="Types and formatting",
        layer=SM,
        statement="A Table.FromRows literal must carry the type its own M type-ascription "
                 "declares -- no quoted string against `type table [Value = datetime]`.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='"Expression.Error: The type of the value does not match the type of the column." '
                "This is the sibling of SM-TYPE-01 and was INTRODUCED BY the fix for it: correcting "
                "the TOM column to say datetime did not also fix the value still being emitted as a "
                "quoted string. Two facts asserted in two places with nothing checking they agree.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_m_row_literal_types",
    ),
    Rule(
        id="SM-TYPE-03",
        domain="Types and formatting",
        layer=SM,
        statement="Every column's sourceColumn must exactly match a column the partition's M "
                 "query actually produces.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="The classic 'column not found' load failure. Any renaming applied for SM-NAME-05 "
                "or SM-NAME-06 must NOT propagate to sourceColumn, which names the physical source "
                "-- so the naming fixes and this rule pull in opposite directions and must be "
                "implemented together.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_source_column_in_partition",
    ),
    Rule(
        id="SM-TYPE-04",
        domain="Types and formatting",
        layer=SM,
        statement="A formatString must be legal for the column's declared dataType.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="A date format string on a string column, typically inherited from a Tableau "
                "field format (inventory row 20) without re-checking the mapped type.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=PARTIAL,
        check="check_format_string_matches_type",
    ),
    Rule(
        id="SM-TYPE-05",
        domain="Types and formatting",
        layer=SM,
        statement="summarizeBy must be legal for the dataType -- no sum or average on a string "
                 "or boolean column.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Tableau's default aggregation (inventory row 9) does not map 1:1, and a "
                "role=measure field whose type mapped to string produces exactly this.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_summarize_by_matches_type",
    ),
    Rule(
        id="SM-TYPE-06",
        domain="Types and formatting",
        layer=SM,
        statement="A column marked isKey must be unique and non-nullable in the data.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Fails at refresh, not at open -- so it survives a lint pass and dies on real data.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=RUNTIME,
    ),

    # ------------------------------------------------------ SM: structural integrity
    Rule(
        id="SM-STRUCT-01",
        domain="Structural integrity",
        layer=SM,
        statement="A sortByColumn must live on the SAME table as the column it sorts.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="The editor only ever offers same-table columns in that dropdown, so no authored "
                "model can violate it and the diagnostic is correspondingly untested.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_sort_by_column_same_table",
    ),
    Rule(
        id="SM-STRUCT-02",
        domain="Structural integrity",
        layer=SM,
        statement="A sortByColumn must be functionally 1:1 with the column it sorts -- each "
                 "sorted value maps to exactly one sort value.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='"There can\'t be more than one value in SortBy column for the same value in '
                'Column" -- raised at refresh, against real data, not at open. Tableau custom '
                'member order (inventory row 21) translates into exactly this construct.',
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=RUNTIME,
    ),
    Rule(
        id="SM-STRUCT-03",
        domain="Structural integrity",
        layer=SM,
        statement="sortByColumn must not form a cycle (A sorts by B, B sorts by A).",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Reported as a circular dependency, naming the columns but not the sort "
                "relationship that caused it.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_sort_by_cycles",
    ),
    Rule(
        id="SM-STRUCT-04",
        domain="Structural integrity",
        layer=SM,
        statement="Hierarchy levels must reference columns of their own table, with contiguous "
                 "ordinals starting at 0.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Tableau drill paths (inventory row 14) can span a blended datasource, which has "
                "no single-table equivalent -- the translation must refuse rather than emit.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_hierarchy_levels",
    ),
    Rule(
        id="SM-STRUCT-05",
        domain="Structural integrity",
        layer=SM,
        statement="Both endpoints of a relationship must name a table and column that exist.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Dangling endpoint; the model refuses to load.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_relationship_endpoints_exist",
    ),
    Rule(
        id="SM-STRUCT-06",
        domain="Structural integrity",
        layer=SM,
        statement="The two columns joined by a relationship must have the SAME dataType.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Tableau joins on columns of differing type without complaint (it coerces); Power "
                "BI does not. A Tableau join key of int64 against string is a silent source-side "
                "success and a hard target-side failure.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_relationship_type_match",
    ),
    Rule(
        id="SM-STRUCT-07",
        domain="Structural integrity",
        layer=SM,
        statement="The 'one' side of a many-to-one relationship must hold unique, non-blank "
                 "values.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='"The relationship cannot be created because each column contains duplicate '
                'values" -- at refresh, on real data. A Tableau join that is genuinely many-to-many '
                'must not be emitted as many-to-one just because the cardinality is unknown.',
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=RUNTIME,
    ),
    Rule(
        id="SM-STRUCT-08",
        domain="Structural integrity",
        layer=SM,
        statement="At most one ACTIVE relationship may exist on any path between two tables, and "
                 "the active set must contain no ambiguous or circular path.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Multiple Tableau joins between the same pair of tables map to multiple "
                "relationships; all but one must be emitted inactive.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_relationship_ambiguity",
    ),
    Rule(
        id="SM-STRUCT-09",
        domain="Structural integrity",
        layer=SM,
        statement="The calculated-column / measure dependency graph must be acyclic.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='"A circular dependency was detected" -- and Desktop routinely names an unrelated '
                'pair of objects rather than the real cycle. SM-DAX-02 covers one specific cause; '
                'the general graph check does not exist yet.',
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_calc_dependency_cycles",
    ),
    Rule(
        id="SM-STRUCT-10",
        domain="Structural integrity",
        layer=SM,
        statement="Every table must carry at least one partition unless it is a calculated table.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="A table emitted from a Tableau relation that resolved to nothing.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_tables_have_partitions",
    ),
    Rule(
        id="SM-STRUCT-11",
        domain="Structural integrity",
        layer=SM,
        statement="An RLS role's table filter expression must evaluate to boolean, and role "
                 "names must be unique.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Relevant once entitlement-table RLS (BRD in-scope) is generated rather than "
                "hand-built.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_role_names_unique",
    ),
    Rule(
        id="SM-STRUCT-12",
        domain="Structural integrity",
        layer=SM,
        statement="A calculation-group table may hold exactly one calculationGroup column; items "
                 "need ordinals, and precedence is required once more than one group exists.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Only reachable if the generator starts emitting calculation groups; no corpus "
                "workbook drives one today.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_calculation_groups",
    ),
    Rule(
        id="SM-STRUCT-13",
        domain="Structural integrity",
        layer=SM,
        statement="Perspectives and translations may only reference objects that exist in the "
                 "model.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="A dangling reference left behind after a rename pass.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_perspective_references",
    ),

    # ------------------------------------------------------ SM: DAX semantics
    Rule(
        id="SM-DAX-01",
        domain="DAX semantics",
        layer=SM,
        statement="Every table, column and measure a DAX body references must exist in the model.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom='Surfaces only once the affected visual is clicked: "Fields that need to be fixed '
                '... cannot be found or may not be used in this expression", Underlying Error '
                'Missing_References. The project opens, shows all its pages, and is still '
                'substantively broken. Two shapes seen: `[Parameters].[X]`, which is Tableau syntax '
                'passed through untranslated and is never valid DAX; and a qualified reference to a '
                'what-if parameter table that nothing ever generated.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_dax_references",
    ),
    Rule(
        id="SM-DAX-02",
        domain="DAX semantics",
        layer=SM,
        statement="A calculated COLUMN must never reference a MEASURE, in either the qualified "
                 "'Table'[Name] or the bare [Name] form.",
        enforced_by=ENGINE,
        severity=SILENT_WRONG,
        symptom='DAX forbids it structurally -- columns compute at refresh time, before any '
                'query-time measure exists. Desktop reports "A circular dependency was detected" '
                'naming an unrelated pair of columns, never the real cause. Confirmed on Prevent '
                'Dashboard: a calculated column referenced a parameter\'s SELECTEDVALUE measure via '
                'the bare form, the model loaded clean, and every report filter on that column then '
                'failed with Broken_Filters -- silent until a user opened that exact page. The bare '
                'form is the dangerous one: it is what a parameter reference translates to, and it '
                'looks identical to an ordinary same-table column shorthand. Measures are unique '
                'model-wide, so a bare name matching ANY measure anywhere is a hit.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_column_references_measure",
    ),
    Rule(
        id="SM-DAX-03",
        domain="DAX semantics",
        layer=SM,
        statement="`+` must never be used for string concatenation; DAX requires `&`.",
        enforced_by=ENGINE,
        severity=SILENT_WRONG,
        symptom='Valid-looking DAX that fails at QUERY time, not load time, on the first visual '
                'that evaluates it: "Cannot convert value \'\' of type Text to type Numeric/Date". '
                'Tableau uses `+` for both. Confirmed on Prevent Dashboard: '
                '`COALESCE(a,"") + "|" + COALESCE(b,"")`. The translator now rewrites any `+` chain '
                'containing a literal to `&`, so a hit here means that rewrite regressed.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_literal_plus_concatenation",
    ),
    Rule(
        id="SM-DAX-04",
        domain="DAX semantics",
        layer=SM,
        statement="Every emitted DAX body must parse.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="The translator's output is currently trusted. A parse step before emit turns an "
                "opaque Desktop load failure into a named measure and character offset.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_dax_balanced",
    ),
    Rule(
        id="SM-DAX-05",
        domain="DAX semantics",
        layer=SM,
        statement="A measure must not reference a bare column without an aggregation.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom='"A single value for column X cannot be determined." Tableau permits the '
                'unaggregated form in many contexts, so this is a systematic translation hazard '
                'rather than an occasional slip.',
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_measure_bare_column",
    ),
    Rule(
        id="SM-DAX-06",
        domain="DAX semantics",
        layer=SM,
        statement="DAX references must match the model object's name with exact case.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="Interacts with SM-NAME-01: TOM's collection is case-insensitive for UNIQUENESS "
                "but references still resolve against the stored spelling, so a rename pass that "
                "changes case must rewrite every reference.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_reference_case",
    ),
    Rule(
        id="SM-DAX-07",
        domain="DAX semantics",
        layer=SM,
        statement="An aggregate function's (SUM/AVERAGE/MIN/MAX/COUNT/COUNTA/DISTINCTCOUNT/"
                 "MEDIAN/STDEV.S/VAR.S) single bare-reference argument must resolve to a COLUMN, "
                 "never a MEASURE.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom='Confirmed on Caribbean - Drilldown (QBR): `Gross Trx Amt Decline Rate` = '
                '`DIVIDE(SUM(\'caribbean\'[Gross Trx Amt Declined]), ...)`, where '
                '`Gross Trx Amt Declined` carries no aggregate of its own in its Tableau source '
                'and was correctly promoted to a MEASURE by `tmdl.py`\'s `_aggregate_map` '
                'fixpoint -- but a promoted measure renders with the IDENTICAL `\'Table\'[Name]` '
                'string shape as an ordinary column, so no text-only check can tell them apart '
                'without knowing which names were promoted. Desktop opened the model clean and '
                'rejected only the dependent visual once a real user clicked it, on the CNP(PPT) '
                'page\'s "(caribbean) Approval Rate (#/$)" visual: "This expression refers to a '
                'Measure object ... which has an error", Underlying Error Missing_References. '
                'Confirmed corpus-wide, not a one-off: a regex scan for AGGFUNC(\'Table\'[Name]) '
                'where Name is a measure-only identifier found 135 instances across 3 of 6 '
                'reference workbooks (Caribbean 18, NGPO Dashboard New - AP Version (MSC) 110, '
                'LAC Pedidosya Transaction Profile 7) before the fix; 0 after. The primary fix is '
                'a PRE_EMIT decline: a new `resolve_kind` callback threaded through '
                '`dax.translate`/`Parser` the same way `resolve_type` already was for the STR() '
                'date check (SM-DAX-01\'s sibling), so `func_call`\'s aggregate-over-bare-'
                'reference branch declines with a clear reason instead of emitting invalid DAX '
                'whenever the resolved kind is measure rather than column -- every dependent calc '
                'then declines too via the same cascade `resolve_calcs`\'s fixpoint already uses '
                'elsewhere. `check_aggregate_wraps_measure` is the POST_EMIT backstop this rule '
                'names, should the generator regress.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_aggregate_wraps_measure",
    ),

    # ------------------------------------------------------ SM: partitions / M
    Rule(
        id="SM-PART-01",
        domain="Partitions and M",
        layer=SM,
        statement="A partition must contain no placeholder: no Value.NativeQuery(null, ...) and "
                 "no literal 'Table' left in DAX.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom='"Native queries aren\'t supported by this value" -- null is not a valid '
                "connection. A literal 'Table' is an unsubstituted placeholder from the LLM "
                "enrichment tier, which never sees real table names; it references a table that "
                "does not exist and can fail the entire model load, not just the one measure.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_partitions_and_dax",
    ),
    Rule(
        id="SM-PART-02",
        domain="Partitions and M",
        layer=SM,
        statement="Every partition's M expression must parse and must define a Source step.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="Same argument as SM-DAX-04: parse before emit rather than trusting the writer.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_m_structure",
    ),
    Rule(
        id="SM-PART-03",
        domain="Partitions and M",
        layer=SM,
        statement="Every M parameter or shared expression referenced by a partition must be "
                 "declared in expressions.tmdl.",
        enforced_by=ENGINE,
        severity=OPEN_FAIL,
        symptom="The M-side analogue of SM-DAX-01.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_m_parameter_refs",
    ),

    # ------------------------------------------------------ RPT: field references
    Rule(
        id="RPT-REF-01",
        domain="Field references",
        layer=RPT,
        statement="Every field a visual's queryState references must resolve to a live model "
                 "table, column or measure.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="The visual renders broken or empty. Partially covered: "
                "twb2pbip/validator.py resolves report field references against the TMDL model, "
                "but only for visuals, and it reports rather than blocks.",
        detection=PRE_EMIT,
        confidence=EMPIRICAL,
        status=PARTIAL,
    ),
    Rule(
        id="RPT-REF-02",
        domain="Field references",
        layer=RPT,
        statement="Report-side field references must match model object names with exact case.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="The report half of SM-DAX-06. A model-side rename for SM-NAME-05 that does not "
                "rewrite the report leaves every visual dangling.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_report_reference_case",
    ),

    # ------------------------------------------------------ RPT: visual data-role contracts
    Rule(
        id="RPT-ROLE-01",
        domain="Visual data-role contracts",
        layer=RPT,
        statement="visualType must be a value Desktop recognises.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="An unknown type renders as an empty container. This is where an unmapped "
                "Tableau mark type lands if the translation approximates instead of refusing.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=PARTIAL,
        check="check_visual_types_known",
    ),
    Rule(
        id="RPT-ROLE-02",
        domain="Visual data-role contracts",
        layer=RPT,
        statement="Every data role a visual type marks as required must be populated.",
        enforced_by=EDITOR,
        severity=RENDER_FAIL,
        symptom="The editor shows an explicit 'add data to this visual' well; a generated visual "
                "just renders blank with no indication which role is missing.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_visual_has_required_data",
    ),
    Rule(
        id="RPT-ROLE-03",
        domain="Visual data-role contracts",
        layer=RPT,
        statement="A data role's field count must not exceed that visual type's maximum "
                 "cardinality.",
        enforced_by=EDITOR,
        severity=RENDER_FAIL,
        symptom="Purely editor-enforced -- the field well physically refuses a second drop (a pie "
                "chart accepts one Legend field, a card one field, a slicer one). Nothing in the "
                "file format records the limit, so this is the largest silent-divergence class in "
                "the report layer, and the one that needs an empirically-built contract table.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=PARTIAL,
        check="check_visual_role_cardinality",
    ),
    Rule(
        id="RPT-ROLE-04",
        domain="Visual data-role contracts",
        layer=RPT,
        statement="A projection's aggregation must be legal for the field: no aggregation over a "
                 "measure, no numeric aggregation over a string column.",
        enforced_by=EDITOR,
        severity=RENDER_FAIL,
        symptom="Tableau pills carry their aggregation inline (inventory row 23, column-instance), "
                "so the mapping must decide per field whether the aggregation belongs on the "
                "projection or was already baked into a measure -- doing both double-aggregates.",
        detection=PRE_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_visual_aggregation_legal",
    ),

    # ------------------------------------------------------ RPT: filters
    Rule(
        id="RPT-FILTER-01",
        domain="Filters",
        layer=RPT,
        statement="A filter's literal value must carry the same type as the column it filters -- "
                 "string literals single-quoted inside the JSON string, numerics bare with an "
                 "L/D suffix, booleans bare.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom='"This visual contains one or more filters with ... type mismatches" and the '
                'visual refuses to render. Confirmed on a filter over an int64 column emitted as '
                "`'1'` instead of `1L`. The root cause is structural: a value that came from "
                "Tableau XML attribute text is a Python str no matter what the column really is, so "
                "any writer trusting the value's own Python type gets this wrong every time.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_filter_literal_types",
    ),
    Rule(
        id="RPT-FILTER-02",
        domain="Filters",
        layer=RPT,
        statement="Every filter target must resolve to a live model object, at report, page and "
                 "visual scope alike.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="Broken_Filters, page-local and silent until that page is opened -- the same "
                "surfacing behaviour that made SM-DAX-02 expensive to find.",
        detection=PRE_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_visual_filter_targets",
    ),
    Rule(
        id="RPT-FILTER-03",
        domain="Filters",
        layer=RPT,
        statement="The filter kind must suit the target column's type -- no range filter on a "
                 "string, no categorical In-list on a continuous measure.",
        enforced_by=EDITOR,
        severity=RENDER_FAIL,
        symptom="The editor picks the filter card type from the field automatically, so a human "
                "cannot mismatch them; the translation picks it from the Tableau filter class "
                "instead, which does not always agree with the mapped Power BI type.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=PARTIAL,
        check="check_filter_kind_matches_type",
    ),
    Rule(
        id="RPT-FILTER-04",
        domain="Filters",
        layer=RPT,
        statement="A visual filter's target table must be relationship-reachable (undirected) "
                 "from at least one of the visual's OWN queried table(s) -- RPT-FILTER-02's "
                 "check that the target exists is not the same claim as this one, that Desktop "
                 "can relate it to what the visual is actually asking for.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom='Confirmed on NGPO Dashboard New - AP Version (MSC), "Level 4 Analysis" page, visual 0cac7a67f24e6629c51c: query projects only \'Level 3 - Intervention Mapping\'[Card Lifestage], but filterConfig also carried a filter on \'Level 4 - Lifestage X Interventions\'[Topic Area (copy)] -- a different table with NO relationship to the first anywhere in the model (relationships.tmdl is empty for every rebuilt workbook in this corpus -- see build-tableau-pbip/SKILL.md\'s "the join path is exercised only by the synthetic probe"). Desktop: "Can\'t determine relationships between the fields... Underlying Error: InvalidUnconstrainedJoin... Not showing data for DataShape \'DS0\' because it\'s not clear how these fields are related." Root cause is Tableau data BLENDING: the worksheet\'s filter set silently carries fields from a datasource the worksheet\'s own <datasources> list never declares, matched by Tableau at query time with no stored key -- there is no Power BI relationship to emit for it. Scan of all 1,251 visuals across the 6-workbook corpus for "a filterConfig entity absent from the visual\'s own query entities, and not a Param table" found 117 instances across 2 of 6 workbooks (NGPO 80, AP Product Portfolio Management Dashboard 37); 0 in the other 4. Fixed at the generator (pbir.py\'s `_visual_filters`/`_relationship_reachable`): the unreachable filter is DROPPED and reported in RESIDUE.md\'s filter-disposition table, never emitted broken -- a `Param `-prefixed table is exempted (a slicer\'s own direct binding, no relationship needed by design). This rule is the POST_EMIT backstop should the generator regress; it is a graph-reachability question (`Project.relationship_reachable`), not a text match, because a real key-based relationship (never yet seen in this corpus, but structurally possible) would make the SAME cross-table filter legal.',
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_visual_filter_relationship_reachable",
    ),
    Rule(
        id="RPT-ID-01",
        domain="Identifiers and layout",
        layer=RPT,
        statement="Page folder names, pages.json entries and each page.json's own name must all "
                 "agree.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="A page folder Desktop cannot correlate is treated as a private user file and "
                "silently dropped -- no tab, no error.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_pages",
    ),
    Rule(
        id="RPT-ID-02",
        domain="Identifiers and layout",
        layer=RPT,
        statement="PBIR object names must consist of word characters or hyphens.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="Desktop ignores the folder outright. Tableau dashboard captions become folder "
                "names, and they contain spaces, brackets and slashes -- so this fires constantly "
                "without a sanitising allocator.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_object_names",
    ),
    Rule(
        id="RPT-ID-03",
        domain="Identifiers and layout",
        layer=RPT,
        statement="Visual names must be unique within their page, and every visual folder name "
                 "must equal the name inside its visual.json.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="The visual-level analogue of RPT-ID-01, which currently checks pages only.",
        detection=POST_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_visual_identity",
    ),
    Rule(
        id="RPT-POS-01",
        domain="Identifiers and layout",
        layer=RPT,
        statement="A visual's x, y, width, height and z must be numbers, non-negative, and lie "
                 "within the page canvas.",
        enforced_by=EDITOR,
        severity=SILENT_WRONG,
        symptom="The canvas physically constrains a dragged visual. Tableau zone geometry is in "
                "different units against a different canvas size, so an unscaled translation puts "
                "visuals off-page where they exist but cannot be seen.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_visual_position",
    ),
    Rule(
        id="RPT-SORT-01",
        domain="Identifiers and layout",
        layer=RPT,
        statement="A visual's sort field must be one of that visual's own projections.",
        enforced_by=EDITOR,
        severity=RENDER_FAIL,
        symptom="The sort menu only lists fields already in the visual.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_visual_sort_fields",
    ),

    # ------------------------------------------------------ RPT: resources and wiring
    Rule(
        id="RPT-RES-01",
        domain="Resources and wiring",
        layer=RPT,
        statement="Every resource file must have a corresponding resourcePackages entry in "
                 "report.json.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="An undeclared file is an orphan Desktop will not load: an image visual pointing "
                'at one renders as an empty "Select Image in the format pane under Style" '
                "placeholder, and an undeclared base theme is simply ignored.",
        detection=POST_EMIT,
        confidence=DOCUMENTED,
        status=IMPLEMENTED,
        check="check_resource_packages",
    ),
    Rule(
        id="RPT-RES-02",
        domain="Resources and wiring",
        layer=RPT,
        statement="Every imageUrl literal must match a declared resource, by name or by path.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="No match means an empty placeholder rather than an error, so it is easy to miss.",
        detection=POST_EMIT,
        confidence=EMPIRICAL,
        status=IMPLEMENTED,
        check="check_image_urls",
    ),
    Rule(
        id="RPT-NAV-01",
        domain="Resources and wiring",
        layer=RPT,
        statement="Bookmarks, sync-slicer groups, drillthrough targets and page-navigation "
                 "actions must reference pages and visuals that exist.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="Relevant as soon as Tableau dashboard actions (inventory row 37) and the "
                "dashboard-button cluster from the unclaimed-element backlog get translated.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=PARTIAL,
        check="check_navigation_targets",
    ),
    Rule(
        id="RPT-CF-01",
        domain="Resources and wiring",
        layer=RPT,
        statement="Conditional-formatting rules must reference measures or columns that exist "
                 "and are of a comparable type.",
        enforced_by=ENGINE,
        severity=RENDER_FAIL,
        symptom="The destination for Tableau colour-encoding detail; dangling references here "
                "break the visual rather than the model.",
        detection=PRE_EMIT,
        confidence=INFERRED,
        status=IMPLEMENTED,
        check="check_conditional_formatting_refs",
    ),
)


# ---------------------------------------------------------------- accessors

_BY_ID = {r.id: r for r in RULES}
_BY_CHECK = {r.check: r for r in RULES if r.check}


def by_id(rule_id: str) -> Rule | None:
    return _BY_ID.get(rule_id)


def for_check(check_name: str) -> Rule | None:
    """The rule a pbip_lint check function enforces, or None if it is unregistered."""
    return _BY_CHECK.get(check_name)


def implemented() -> tuple[Rule, ...]:
    return tuple(r for r in RULES if r.status == IMPLEMENTED)


def gaps() -> tuple[Rule, ...]:
    return tuple(r for r in RULES if r.status == GAP)


def unenforced() -> tuple[Rule, ...]:
    """Rules nothing checks. `runtime` rules are excluded: no check could exist."""
    return tuple(r for r in RULES if r.status == GAP)


def coverage() -> dict:
    """Counts by status and by severity -- the numbers that sequence the work."""
    out: dict = {"total": len(RULES), "by_status": {}, "by_severity": {}, "gap_by_severity": {}}
    for r in RULES:
        out["by_status"][r.status] = out["by_status"].get(r.status, 0) + 1
        out["by_severity"][r.severity] = out["by_severity"].get(r.severity, 0) + 1
        if r.status == GAP:
            out["gap_by_severity"][r.severity] = out["gap_by_severity"].get(r.severity, 0) + 1
    return out


def _lint_sync_check() -> list[str]:
    """The register and pbip_lint must agree on which checks exist.

    Read by AST rather than import: pbip_lint imports this module, so importing it back
    would be circular. Parsing the source has no such problem and needs nothing installed.

    A check with no rule is the failure this register exists to prevent -- a constraint
    enforced that nobody wrote down. A rule naming a check that no longer exists is the
    mirror image: a rule claimed as implemented that in fact is not.
    """
    import ast
    from pathlib import Path

    here = Path(__file__).resolve().parent
    sources = [here.parent / "pbip_lint.py", here / "checks.py"]

    functions: dict[str, str] = {}
    problems: list[str] = []
    for path in sources:
        if not path.exists():
            problems.append(f"cannot find {path} to cross-check")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("check_"):
                functions[node.name] = path.name

    claimed = {r.check for r in RULES if r.check}
    problems += [f"{functions[name]}.{name} enforces no registered rule"
                 for name in sorted(set(functions) - claimed)]
    problems += [f"a rule names check {name}, which exists in neither source"
                 for name in sorted(claimed - set(functions))]
    return problems


def _integrity_check() -> list[str]:
    """The register must not contradict itself."""
    problems = []
    seen_ids = set()
    seen_checks = set()
    for r in RULES:
        if r.id in seen_ids:
            problems.append(f"duplicate rule id: {r.id}")
        seen_ids.add(r.id)
        if r.check:
            if r.check in seen_checks:
                problems.append(f"check {r.check} claimed by more than one rule")
            seen_checks.add(r.check)
            if r.status == GAP:
                problems.append(f"{r.id} names a check but is marked gap")
        elif r.status == IMPLEMENTED:
            problems.append(f"{r.id} is marked implemented but names no check")
        for field, allowed in (
            (r.enforced_by, {ENGINE, EDITOR, SCHEMA}),
            (r.severity, {OPEN_FAIL, RENDER_FAIL, SILENT_WRONG, COSMETIC}),
            (r.confidence, {DOCUMENTED, EMPIRICAL, INFERRED}),
            (r.detection, {CONSTRUCTIVE, PRE_EMIT, POST_EMIT}),
            (r.status, {IMPLEMENTED, PARTIAL, GAP, RUNTIME}),
        ):
            if field not in allowed:
                problems.append(f"{r.id}: '{field}' not in {sorted(allowed)}")
    return problems + _lint_sync_check()


# ---------------------------------------------------------------- doc generation

_PREAMBLE = """# Power BI Constraint Rules — What the Editor Enforces

> Generated from `tools/pbip_rules/registry.py`. **Do not edit by hand** — edit the
> registry and regenerate:
> `python3 tools/pbip_rules/registry.py > docs/schema-reference/pbip-constraint-rules.md`

Companion documents in this directory:

- `tableau-metadata-inventory.md` — the register for the **source** side (59 asset rows)
- `twb-schema.md` — the Tableau workbook XML format
- `pbip-schema.md` — the target PBIP format

## Why this register exists

Power BI enforces correctness in three places, and only one of them is inspectable:

| | Enforces | Inspectable |
|---|---|---|
| Published JSON schemas | shape, closed enums, `additionalProperties: false` | yes |
| TMDL grammar | indentation and keywords | yes |
| **Desktop editor logic** | **model and report semantics** | **no** |

The editor never lets a human reach an invalid state — it greys out the illegal option,
renames on collision, refuses the OK button. A generator has no such guard rail, so it
writes states no human could author. The first thing that notices is Desktop at open
time, and its diagnostic routinely names the wrong object: a calculated column that
references a measure is reported as *"a circular dependency"* between two unrelated
columns (`SM-DAX-02`).

That mismatch — invalid state produced silently, reported misleadingly — is the whole
cost. It is why each new workbook takes two or three Desktop round-trips to migrate.

## How to read a row

| Column | Meaning |
|---|---|
| **Enforced by** | `engine` = the model refuses to load, loudly. `editor` = **only** the editor prevents it, so a script can emit it freely and nothing complains until a user looks. `schema` = a published schema catches it. |
| **Severity** | `open-fail` the project does not open · `render-fail` it opens, something is broken · `silent-wrong` it opens and renders the wrong thing · `cosmetic`. This is the sequencing key. |
| **Detection** | Where the rule can be caught. `constructive` = the IR makes the state unrepresentable and no check is needed · `pre-emit` = checked against the IR before writing · `post-emit` = checked against files on disk. Left is better. |
| **Confidence** | `documented` Microsoft says so · `empirical` we watched Desktop do it · `inferred` it follows from the object model but the exact boundary is unconfirmed. **An `inferred` rule must never be quoted as fact.** |
| **Status** | `implemented` a check enforces it · `partial` a check enforces the defensible core, with the remainder stated in the symptom · `runtime` it can only be settled against real data at refresh, so no static check can exist · `gap` nothing checks it. |

A `partial` row is a deliberate choice, not an unfinished one. One false positive costs
more than one missed defect: a check that cries wolf gets suppressed and then catches
nothing at all. Where a rule is broader than what can be verified from the files, the check
implements the core it can defend and the **Symptom** column records exactly what is left
unverified.

A `runtime` row is not a gap. `SM-TYPE-06`, `SM-STRUCT-02` and `SM-STRUCT-07` are
statements about *data*, not metadata — nothing in a PBIP file can settle them, and they
fail at refresh against the real source. Calling them unimplemented would imply a check
could be written.

## Where the implemented rules came from

Every `implemented` row is banked evidence: each one cost a real Desktop round-trip on a
real corpus workbook, and its **Symptom** column records what Desktop actually said. That
column is the most valuable thing here, because it is what maps an opaque Desktop message
back to a rule ID — which is precisely the lookup that currently costs two or three
iterations per dashboard.
"""

_SEVERITY_ORDER = {OPEN_FAIL: 0, RENDER_FAIL: 1, SILENT_WRONG: 2, COSMETIC: 3}


def _emit_markdown() -> str:
    cov = coverage()
    lines = [_PREAMBLE, "", "## Coverage", "",
             f"Register version {REGISTER_VERSION} — **{cov['total']} rules**.", ""]

    lines.append("| Status | Rules |")
    lines.append("|---|---|")
    for status in (IMPLEMENTED, PARTIAL, GAP):
        lines.append(f"| {status} | {cov['by_status'].get(status, 0)} |")
    lines.append("")

    lines.append("Unenforced rules by what a violation costs — this is the work queue, "
                 "highest cost first:")
    lines.append("")
    lines.append("| Severity | Unenforced |")
    lines.append("|---|---|")
    for sev in (OPEN_FAIL, RENDER_FAIL, SILENT_WRONG, COSMETIC):
        lines.append(f"| {sev} | {cov['gap_by_severity'].get(sev, 0)} |")
    lines.append("")

    by_layer: dict[str, list[Rule]] = {}
    for r in RULES:
        by_layer.setdefault(r.layer, []).append(r)

    for layer in (PRJ, SM, RPT):
        rules = by_layer.get(layer, [])
        if not rules:
            continue
        lines.append(f"## {layer}")
        lines.append("")
        by_domain: dict[str, list[Rule]] = {}
        for r in rules:
            by_domain.setdefault(r.domain, []).append(r)
        for domain, drules in by_domain.items():
            lines.append(f"### {domain}")
            lines.append("")
            lines.append("| ID | Rule | Enforced by | Severity | Detection | Confidence | Status | Symptom |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for r in sorted(drules, key=lambda x: (_SEVERITY_ORDER[x.severity], x.id)):
                statement = r.statement.replace("|", "\\|").replace("\n", " ")
                symptom = r.symptom.replace("|", "\\|").replace("\n", " ")
                status = f"**{r.status}**" if r.status in (GAP, RUNTIME) else r.status
                lines.append(
                    f"| `{r.id}` | {statement} | {r.enforced_by} | {r.severity} | "
                    f"{r.detection} | {r.confidence} | {status} | {symptom} |"
                )
            lines.append("")

    lines.append("## Known blind spots")
    lines.append("")
    lines.append(
        "This register cannot be complete, for the same reason `tableau-metadata-inventory.md` "
        "cannot: Microsoft publishes no specification of editor behaviour. Two backstops keep the "
        "incompleteness visible rather than invisible:"
    )
    lines.append("")
    lines.append(
        "1. **Unclaimed-property backstop.** Count every property the emitter writes that no rule "
        "governs. A high count is expected and is the point — it names the blind spots instead of "
        "defining them away."
    )
    lines.append(
        "2. **Fault injection.** Take a known-good PBIP, mutate one property to an illegal value, "
        "open it, record the exact message. That builds the violation-to-symptom map, which is "
        "what removes the iteration loop: a Desktop error resolves to a rule ID instead of a "
        "debugging session."
    )
    lines.append("")
    lines.append(
        "Neither is built yet. Both are worth more than any attempt to enumerate a \"complete\" "
        "rule list."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    problems = _integrity_check()
    if problems:
        for p in problems:
            print(f"REGISTER INTEGRITY: {p}", file=sys.stderr)
        return 1
    if "--check" in argv:
        cov = coverage()
        print(f"register ok: {cov['total']} rules, "
              f"{cov['by_status'].get(IMPLEMENTED, 0)} implemented, "
              f"{cov['by_status'].get(PARTIAL, 0)} partial, "
              f"{cov['by_status'].get(RUNTIME, 0)} runtime, "
              f"{cov['by_status'].get(GAP, 0)} gap")
        return 0
    print(_emit_markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
