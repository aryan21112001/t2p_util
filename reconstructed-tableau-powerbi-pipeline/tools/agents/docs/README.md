# Agent → Skills → Python Scripts Architecture

## Quick Start

**Three agents orchestrate skills which call Python scripts.**

```
Agent 1 (Converter)
  └→ Skills (CLI wrappers)
      └→ Python scripts (implementation)

Agent 2 (Dummy Data Gen)
  └→ Skills (CLI wrappers)
      └→ Python scripts (implementation)

Agent 3 (Realistic Data Gen)
  └→ Skills (CLI wrappers)
      └→ Python scripts (implementation)
```

---

## Documentation

### 1. **SKILLS_FIRST_ARCHITECTURE.md** ⭐ START HERE
**Purpose:** Understand the design philosophy  
**Read Time:** 15-20 minutes  
**Content:**
- Why skills-first design matters
- All 8 available skills and what they do
- How agents orchestrate skills
- How to call skills from agents
- Implementation plan

### 2. **SKILLS_QUICK_REFERENCE.md**
**Purpose:** Quick lookup of skills and how to call them  
**Read Time:** 5-10 minutes  
**Content:**
- Each skill with purpose, inputs, outputs
- CLI commands to call skills
- Expected results
- Error handling

### 3. **01-Getting-Started/README.md**
**Purpose:** How to run the agents  
**Read Time:** 5 minutes  
**Content:**
- CLI commands for each agent
- How to run single conversions
- How to run full pipeline
- Configuration options

---

## Architecture Overview

### Agent → Skills → Python Scripts

**Agent (Orchestrator)**
- Decides what to do
- Calls skills in sequence
- Persists results
- Returns status

**Skills (CLI Wrappers)**
- Provide Python script with CLI interface
- Located in `/.claude/skills/`
- Handle argument parsing
- Return structured results

**Python Scripts (Implementation)**
- Do the actual work
- Extract, parse, build, validate, generate
- Located in skill folders

### Example: Agent 1 Flow

```python
class ConverterAgent:
    def convert_workbook(self, input_file, output_dir):
        
        # Step 1: Call skill (which calls Python script)
        extracted = call_skill('extract-twbx-contents', input_file)
        
        # Step 2: Call skill (which calls Python script)
        pbip = call_skill('build-tableau-pbip', extracted, output_dir)
        
        # Step 3: Call skill (which calls Python script)
        validation = call_skill('validate-tableau-pbip-mapping', input_file, pbip)
        
        # Step 4: Call tool directly (not a skill)
        compliance = check_project(pbip)
        
        # Step 5: Persist results
        persist_results({
            'validation': validation,
            'compliance': compliance
        })
        
        # Step 6: Return status
        return determine_status(validation, compliance)
```

---

## Skills Reference

| Skill | Purpose | Input | Output |
|-------|---------|-------|--------|
| **extract-twbx-contents** | Unzip Tableau file | `.twbx` file | Extracted folder |
| **build-tableau-pbip** | Convert Tableau to PowerBI | Extracted folder | `.pbip` project |
| **validate-tableau-pbip-mapping** | Validate conversion quality | `.twbx` + `.pbip` | Validation report |
| **generate-dummy-data** | Create synthetic test data | `.pbip` + schema | CSV/JSON files |
| **validate-dummy-data** | Validate generated data | Schema + data | Validation report |
| **inventory-tableau-assets** | Analyze Tableau structure | `.twbx` | Asset list |
| **map-tableau-powerbi-translation** | Plan conversion strategy | Asset list | Translation plan |
| **prepare-dummy-pbip** | Prepare `.pbip` for Power BI | `.pbip` | Modified `.pbip` |

Plus: `/tools/pbip_lint.py` - PBIP compliance checker (42 rules)

---

## Implementation Status

| Component | Status | Location | Notes |
|-----------|--------|----------|-------|
| Agent 1 (Converter) | 🟡 Needs update | `/implementation/agent-1-converter/` | Remove twb2pbip import, use skill |
| Agent 2 (Dummy Data) | 🟡 Needs update | `/implementation/agent-2-dummy-data/` | Add skill calls |
| Agent 3 (Realistic Data) | 🟡 Needs update | `/implementation/agent-3-realistic-data/` | Add skill calls |
| Skills | ✅ Ready | `/.claude/skills/` | All skills ready to use |
| Python Scripts | ✅ Ready | `/.claude/skills/*/scripts/` | All implementations ready |
| pbip_rules | ✅ Keep | `/tools/pbip_rules/` | CORE INFRASTRUCTURE (used by pbip_lint) |
| twb2pbip | 🗑️ Deprecated | `/tools/twb2pbip/` | REMOVE AFTER Agent 1 updated (redundant with build-tableau-pbip skill) |

---

## Next Steps

### Phase 1: Update Agent 1 (5-7 hours)
- [ ] Replace agent code to call skills
- [ ] Add compliance checking
- [ ] Persist validation results
- [ ] Implement quality scoring
- [ ] Implement exit codes (0/1/2)
- [ ] Test with real `.twbx`

### Phase 2: Update Agents 2 & 3 (4-6 hours)
- [ ] Agent 2: Call generate-dummy-data + validate-dummy-data
- [ ] Agent 3: Call extract-twbx + generate + validate
- [ ] Add result persistence for both
- [ ] Implement exit codes

### Phase 3: Test & Verify (2-3 hours)
- [ ] Test each agent independently
- [ ] Test full pipeline
- [ ] Verify JSON output files created
- [ ] Verify exit codes work correctly

**Total Time: 11-16 hours**

---

## Key Files

**Agents (to be updated):**
- `../implementation/agent-1-converter/converter_agent.py`
- `../implementation/agent-2-dummy-data/dummy_data_agent.py`
- `../implementation/agent-3-realistic-data/realistic_data_agent.py`

**Documentation (this folder):**
- `README.md` ← You are here
- `SKILLS_FIRST_ARCHITECTURE.md` ← Read this to understand design
- `01-Getting-Started/README.md` ← How to run
- `03-Skills-Architecture/SKILLS_QUICK_REFERENCE.md` ← Skill lookup

**Skills (don't modify):**
- `/.claude/skills/extract-twbx-contents/`
- `/.claude/skills/build-tableau-pbip/`
- `/.claude/skills/validate-tableau-pbip-mapping/`
- `/.claude/skills/generate-dummy-data/`
- `/.claude/skills/validate-dummy-data/`

**Tools (don't modify):**
- `/tools/pbip_lint.py` - Compliance checker

---

## Pattern to Follow

```
Agent → Skills → Python Scripts
```

**ALWAYS:**
- Agents call skills, don't re-implement
- Skills wrap Python scripts, don't duplicate
- Python scripts do the actual work

**NEVER:**
- Agent re-implementing extraction logic
- Agent re-implementing validation logic
- Skill with its own logic (should delegate to Python scripts)

---

## Questions?

1. **How do I call a skill from an agent?**
   → See `SKILLS_FIRST_ARCHITECTURE.md` for `call_skill()` function

2. **What's the difference between skills and Python scripts?**
   → Skills = CLI interface, Python scripts = implementation
   → Agents call skills, skills call Python scripts

3. **Where do results get stored?**
   → In JSON files (VALIDATION.json, COMPLIANCE.json, REPORT.json)
   → In the output directory alongside generated files

4. **What exit codes should agents use?**
   → 0 = APPROVED
   → 1 = NEEDS_REVIEW
   → 2 = FAILED

5. **How do I know if a skill call succeeded?**
   → Check the return dict: `result['success']`
   → Or check exit code of skill process

---

**Start by reading:** `SKILLS_FIRST_ARCHITECTURE.md`

Then implement Phase 1 to update Agent 1! 🚀
