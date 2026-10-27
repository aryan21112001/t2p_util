# Skills-First Architecture

## Core Principle

**Skills are the primary execution units. Agents are orchestrators that call skills.**

```
WRONG ❌                           CORRECT ✅
──────────────────────────────────────────────────

Agent                              Agent
 ├─ Extract logic                   ├─ Call extract-twbx-contents skill
 ├─ Parse logic                     ├─ Call validate-tableau-pbip-mapping
 ├─ Build logic                     ├─ Call build-tableau-pbip skill
 ├─ Validation logic                ├─ Call pbip_lint compliance check
 └─ Report logic                    ├─ Persist results
                                    └─ Return status

❌ Agent does everything           ✅ Agent orchestrates
❌ Code duplication                ✅ Single source of truth
❌ Hard to maintain                ✅ Easy to maintain
❌ Inconsistent validation         ✅ Consistent validation
```

---

## Available Skills (Core Infrastructure)

### 1. **extract-twbx-contents** ⭐ EXTRACT
**Purpose:** Unzip and extract .twbx file contents  
**Input:** `.twbx` file path  
**Output:** Extracted folder with XML, data, images  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/extract-twbx-contents/extract_twbx_contents.py <twbx_file>
```

---

### 2. **build-tableau-pbip** ⭐ CONVERT
**Purpose:** Convert extracted Tableau to Power BI .pbip  
**Input:** Extracted Tableau structure  
**Output:** `.pbip` project folder  
**Status:** ✅ Implemented  

Sub-components:
- `scripts/extract.py` - Parse Tableau XML
- `scripts/plan_translation.py` - Plan mapping
- `scripts/build_pbip.py` - Build .pbip structure
- `scripts/tmdl.py` - Generate semantic model
- `scripts/pbir.py` - Generate report definitions
- `scripts/dax.py` - Translate calculations to DAX

```bash
python3 .claude/skills/build-tableau-pbip/scripts/build_pbip.py <input> <output>
```

---

### 3. **validate-tableau-pbip-mapping** ⭐ VALIDATE CONVERSION
**Purpose:** Validate conversion quality  
**Input:** Original `.twbx`, converted `.pbip`  
**Output:** Validation report (mapping %, issues, warnings)  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/validate-tableau-pbip-mapping/scripts/validate_mapping.py <twbx> <pbip>
```

---

### 4. **generate-dummy-data** ⭐ CREATE DATA
**Purpose:** Generate synthetic test data for Power BI  
**Input:** `.pbip` project, schema info, row count  
**Output:** CSV/JSON data files  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/generate-dummy-data/scripts/generate_dummy_data.py <pbip> --rows 1000
```

---

### 5. **validate-dummy-data** ⭐ VALIDATE DATA
**Purpose:** Validate generated dummy data  
**Input:** Schema definition, generated CSV/JSON  
**Output:** Validation report (schema match, type check, NULL ratio)  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/validate-dummy-data/scripts/validate_dummy_data.py <schema> <data>
```

---

### 6. **inventory-tableau-assets** 📊 ANALYZE
**Purpose:** Analyze Tableau structure (what needs converting)  
**Input:** `.twbx` file  
**Output:** Asset inventory (dashboards, worksheets, data sources)  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/inventory-tableau-assets/scripts/inventory_assets.py <twbx>
```

---

### 7. **map-tableau-powerbi-translation** 🗺️ PLAN
**Purpose:** Plan Tableau to Power BI translation strategy  
**Input:** Tableau asset inventory  
**Output:** Translation plan (what maps to what)  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/map-tableau-powerbi-translation/scripts/plan_translation.py <inventory>
```

---

### 8. **prepare-dummy-pbip** 🔧 PREPARE
**Purpose:** Prepare .pbip for opening in Power BI Desktop  
**Input:** Generated `.pbip`  
**Output:** Modified `.pbip` with dummy data  
**Status:** ✅ Implemented  

```bash
python3 .claude/skills/prepare-dummy-pbip/ <pbip>
```

---

### Additional Tools (NOT Skills)

**`/tools/pbip_lint.py`** - PBIP compliance checker  
- 42 compliance rules
- DAX validation
- Project structure validation
- Naming conventions

```python
from tools.pbip_lint import check_project, Findings
findings: Findings = check_project(pbip_path)
```

---

## Redesigned Agent Architecture (Skills-First)

### Agent 1: Converter (Orchestrator)

**Job:** Call skills to convert Tableau → PowerBI

```python
class ConverterAgent:
    def convert_workbook(self, input_file, output_dir):
        
        # Step 1: Extract using skill
        extracted_path = call_skill(
            'extract-twbx-contents',
            input_file
        )
        
        # Step 2: Build PBIP using skill
        pbip_path = call_skill(
            'build-tableau-pbip',
            extracted_path,
            output_dir
        )
        
        # Step 3: Validate conversion using skill
        validation = call_skill(
            'validate-tableau-pbip-mapping',
            input_file,
            pbip_path
        )
        
        # Step 4: Check PBIP compliance (tool)
        compliance = check_project(pbip_path)
        
        # Step 5: Persist results
        persist_results({
            'validation': validation,
            'compliance': compliance
        })
        
        # Step 6: Return status
        return determine_status(validation, compliance)
```

**Skills Used:**
- ✅ `extract-twbx-contents` - Extract input
- ✅ `build-tableau-pbip` - Do conversion
- ✅ `validate-tableau-pbip-mapping` - Validate result
- ✅ `/tools/pbip_lint.py` - Check compliance

**Validation Output:**
- ✅ `CONVERSION_VALIDATION.json` - Mapping quality
- ✅ `COMPLIANCE.json` - PBIP compliance results
- ✅ `REPORT.json` - Summary report
- ✅ Exit codes (0=pass, 1=review, 2=fail)

---

### Agent 2: Dummy Data Generator (Orchestrator)

**Job:** Call skill to generate dummy data

```python
class DummyDataGenerator:
    def generate_for_pbip(self, pbip_path, row_count):
        
        # Step 1: Generate data using skill
        data_files = call_skill(
            'generate-dummy-data',
            pbip_path,
            rows=row_count
        )
        
        # Step 2: Validate data using skill
        validation = call_skill(
            'validate-dummy-data',
            pbip_path,
            data_files
        )
        
        # Step 3: Persist results
        persist_results({
            'validation': validation,
            'data_files': data_files
        })
        
        # Step 4: Return status
        return determine_status(validation)
```

**Skills Used:**
- ✅ `generate-dummy-data` - Create synthetic data
- ✅ `validate-dummy-data` - Validate output

**Validation Output:**
- ✅ `DUMMY_DATA_VALIDATION.json` - Schema match, type check, NULL ratio
- ✅ Exit codes (0=pass, 1=review, 2=fail)

---

### Agent 3: Realistic Data Generator (Orchestrator)

**Job:** Extract profiles from source, generate realistic data

```python
class RealisticDataGenerator:
    def generate_realistic_data(self, twbx_path, pbip_path, scale):
        
        # Step 1: Extract source data using skill
        extracted_twbx = call_skill(
            'extract-twbx-contents',
            twbx_path
        )
        
        # Step 2: Analyze source profiles
        profiles = analyze_source_data(extracted_twbx)
        
        # Step 3: Generate realistic data using skill
        data_files = call_skill(
            'generate-dummy-data',
            pbip_path,
            profiles=profiles,
            scale=scale
        )
        
        # Step 4: Validate realistic data using skill
        validation = call_skill(
            'validate-dummy-data',
            pbip_path,
            data_files,
            profiles=profiles
        )
        
        # Step 5: Persist results
        persist_results({
            'validation': validation,
            'data_files': data_files,
            'profiles': profiles
        })
        
        # Step 6: Return status
        return determine_status(validation)
```

**Skills Used:**
- ✅ `extract-twbx-contents` - Read source
- ✅ `generate-dummy-data` - Create realistic data
- ✅ `validate-dummy-data` - Validate output

**Validation Output:**
- ✅ `REALISTIC_DATA_VALIDATION.json` - Profile match, distribution similarity
- ✅ Exit codes (0=pass, 1=review, 2=fail)

---

## Important: Deprecating /tools/twb2pbip

**Status:** The `/tools/twb2pbip/` module is LEGACY and should be deprecated.

**Reason:** 
- `/.claude/skills/build-tableau-pbip/` provides the same functionality
- Agent 1 currently imports from twb2pbip but should call the skill instead
- This eliminates code duplication

**What to Keep:**
- ✅ `/tools/pbip_rules/` (KEEP - used by pbip_lint.py for compliance checking)
- ✅ `/tools/pbip_lint.py` (KEEP - compliance checker with 42 rules)

**Deprecation Timeline:**
1. Update Agent 1 to call `build-tableau-pbip` skill
2. Test Agent 1 works correctly
3. Delete `/tools/twb2pbip/` directory

---

## Call Skill Helper Function

Each agent needs a utility to call skills:

```python
import subprocess
from pathlib import Path

def call_skill(skill_name: str, *args, **kwargs) -> dict:
    """
    Call a skill and return results.
    
    Args:
        skill_name: Name of skill (e.g., 'extract-twbx-contents')
        *args: Positional arguments
        **kwargs: Keyword arguments (converted to flags)
    
    Returns:
        {
            'success': bool,
            'output': str,
            'exit_code': int,
            'error': str | None
        }
    """
    skill_path = Path(__file__).parent.parent.parent.parent / \
                 f'.claude/skills/{skill_name}'
    
    if not skill_path.exists():
        return {
            'success': False,
            'error': f'Skill not found: {skill_path}'
        }
    
    # Build command
    cmd = ['python3']
    
    # Add skill entry point
    if (skill_path / f'{skill_name}.py').exists():
        cmd.append(str(skill_path / f'{skill_name}.py'))
    elif (skill_path / 'scripts').exists():
        # Find main script
        scripts = list((skill_path / 'scripts').glob('*.py'))
        if scripts:
            cmd.append(str(scripts[0]))
    
    # Add positional arguments
    cmd.extend(str(arg) for arg in args)
    
    # Add keyword arguments
    for key, value in kwargs.items():
        cmd.append(f'--{key}')
        cmd.append(str(value))
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        return {
            'success': result.returncode == 0,
            'output': result.stdout,
            'exit_code': result.returncode,
            'error': result.stderr if result.returncode != 0 else None
        }
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': f'Skill execution timed out: {skill_name}'
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to execute skill: {str(e)}'
        }
```

---

## Pipeline Flow (Skills-First)

```
┌─────────────────────────────────────────────────────────────┐
│                     INPUT: .twbx file                        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│         AGENT 1: CONVERTER (Orchestrator)                    │
├─────────────────────────────────────────────────────────────┤
│  1. [extract-twbx-contents] Extract ZIP                     │
│  2. [build-tableau-pbip] Generate .pbip                     │
│  3. [validate-tableau-pbip-mapping] Validate mapping        │
│  4. [pbip_lint] Check compliance (42 rules)                 │
│  5. Persist: CONVERSION_VALIDATION.json                     │
│  6. Persist: COMPLIANCE.json                                │
│  7. Exit: 0=APPROVED, 1=NEEDS_REVIEW, 2=FAILED            │
└─────────────────────────────────────────────────────────────┘
                              ↓
                      IF EXIT = 0 or 1
                              ↓
┌─────────────────────────────────────────────────────────────┐
│         AGENT 2: DUMMY DATA (Orchestrator)                  │
├─────────────────────────────────────────────────────────────┤
│  1. [generate-dummy-data] Create synthetic data             │
│  2. [validate-dummy-data] Validate schema match             │
│  3. Persist: DUMMY_DATA_VALIDATION.json                     │
│  4. Exit: 0=APPROVED, 1=NEEDS_REVIEW, 2=FAILED            │
└─────────────────────────────────────────────────────────────┘
                              ↓
                      IF EXIT = 0 or 1
                              ↓
┌─────────────────────────────────────────────────────────────┐
│         AGENT 3: REALISTIC DATA (Orchestrator)              │
├─────────────────────────────────────────────────────────────┤
│  1. [extract-twbx-contents] Extract source                  │
│  2. Analyze source data profiles                            │
│  3. [generate-dummy-data] Create realistic data             │
│  4. [validate-dummy-data] Validate distribution match       │
│  5. Persist: REALISTIC_DATA_VALIDATION.json                 │
│  6. Exit: 0=APPROVED, 1=NEEDS_REVIEW, 2=FAILED            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                   FINAL OUTPUT                               │
├─────────────────────────────────────────────────────────────┤
│  ✓ .pbip project (or .pbix if opened in Power BI)          │
│  ✓ CSV/JSON data files                                      │
│  ✓ CONVERSION_VALIDATION.json                              │
│  ✓ COMPLIANCE.json                                          │
│  ✓ DUMMY_DATA_VALIDATION.json (optional)                   │
│  ✓ REALISTIC_DATA_VALIDATION.json (optional)                │
└─────────────────────────────────────────────────────────────┘
```

---

## Validation Strategy (Skills-First)

### Validation is BUILT INTO Skills

Each skill returns validation results:

```python
# Agent 1 calls validate-tableau-pbip-mapping skill
validation = call_skill('validate-tableau-pbip-mapping', twbx, pbip)
# Returns:
# {
#     'valid': bool,
#     'mapping_percentage': float (0-100),
#     'issues': [...],
#     'warnings': [...],
#     'missing_features': [...]
# }

# Agent 1 calls pbip_lint tool
compliance = check_project(pbip_path)
# Returns:
# {
#     'all_pass': bool,
#     'failures': [{'severity': 'CRITICAL', 'rule': 'x', 'message': 'y'}],
#     'notes': [...]
# }

# Agent 2 calls validate-dummy-data skill
validation = call_skill('validate-dummy-data', pbip, data_files)
# Returns:
# {
#     'schema_valid': bool,
#     'type_check': {...},
#     'null_ratio': float,
#     'row_count': int
# }
```

### NO SEPARATE VALIDATORS NEEDED

Validation happens WITHIN agents, results are PERSISTED

```python
# Inside agent
validation_results = call_skill(...)
quality_score = calculate_score(validation_results)
status = determine_status(quality_score)

persist_results({
    'validation': validation_results,
    'quality_score': quality_score,
    'status': status,
    'timestamp': datetime.now()
})

sys.exit(0 if status == 'APPROVED' else 1 if status == 'NEEDS_REVIEW' else 2)
```

---

## Implementation Priority

### Phase 1: Fix Agent 1 (IMMEDIATE) ⭐
- [ ] Make Agent 1 call actual skills
- [ ] Add compliance checking
- [ ] Persist validation results
- [ ] Implement exit codes
- **Time:** 5-7 hours

### Phase 2: Fix Agents 2 & 3 (FOLLOW-UP)
- [ ] Make Agent 2 call generate-dummy-data skill
- [ ] Make Agent 3 call extract + generate skills
- [ ] Add validation for both
- [ ] Persist results
- **Time:** 4-6 hours

### Phase 3: Update Documentation
- [ ] Document skill integration
- [ ] Update architecture diagrams
- [ ] Create skill usage examples

---

## Key Principles (Skills-First)

✅ **Skills do the work**  
✅ **Agents orchestrate skills**  
✅ **Validation is part of skills**  
✅ **Results are persisted**  
✅ **Exit codes indicate status**  
✅ **Single source of truth** (skills, not agents)  
✅ **Easy to maintain** (modify skills, not agents)  
✅ **Easy to test** (test skills independently)  

---

## Benefits of Skills-First Design

| Benefit | Why |
|---------|-----|
| **No Code Duplication** | Skills are written once, used by all agents |
| **Single Source of Truth** | If you fix a skill, all agents benefit |
| **Easy Maintenance** | Update skill logic, no need to update agents |
| **Consistent Validation** | All agents use same validation logic |
| **Easy Testing** | Test skills independently from agents |
| **Flexible Orchestration** | Can reorder, skip, or parallelize skills |
| **Production Ready** | Skills are battle-tested, proven |
| **Clear Responsibility** | Skills = compute, Agents = orchestration |

---

## Next Steps

1. **Update Agent 1** to call skills (start here!)
   - Replace `from twb2pbip` with skill calls
   - Add compliance checking
   - Persist results
   
2. **Update Agents 2 & 3** similarly
   - Use `generate-dummy-data` skill
   - Use `validate-dummy-data` skill
   
3. **Update Documentation**
   - Show skill integration
   - Document skill APIs
   - Provide examples

4. **Test End-to-End**
   - Verify skills called correctly
   - Verify results persisted
   - Verify exit codes work

---

*Last updated: Sept 1, 2024*
*Architecture: Skills-First Design*
