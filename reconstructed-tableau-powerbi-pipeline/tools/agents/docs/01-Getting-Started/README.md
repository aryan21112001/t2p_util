# Three-Agent Architecture: Tableau → Power BI Pipeline

## Overview

The three-agent system decomposes the complex Tableau-to-Power BI conversion into specialized, reusable agents:

| Agent | Purpose | Input | Output | Use Case |
|-------|---------|-------|--------|----------|
| **Agent 1: Converter** | Core Tableau → Power BI conversion | `.twbx`/`.twb` | `.pbip` project | Always required |
| **Agent 2: Dummy Data** | Quick synthetic data generation | `.pbip` | CSV/JSON data files | Smoke testing, dev |
| **Agent 3: Realistic** | Realistic data from source profiles | `.twbx` + `.pbip` | CSV/JSON data files | UAT, staging |

---

## Quick Start

### Single-File Conversion (Agent 1 only)
```bash
python3 tools/agents/implementation/agent-1-converter/converter_agent.py input.twbx -o output/
```

### Full Pipeline with Dummy Data (Agents 1 + 2)
```bash
python3 tools/agents/implementation/orchestrator/dummy_data_pipeline_orchestrator.py input.twbx -o output/
```

### Full Pipeline with Realistic Data (Agents 1 + 3)
```bash
python3 tools/agents/implementation/orchestrator/dummy_data_pipeline_orchestrator.py input.twbx -o output/ --agent3
```

### All Agents, No Data (basic run)
```bash
python3 tools/agents/implementation/orchestrator/basic_pipeline_orchestrator.py input.twbx -o output/ --all-agents
```

### All Agents, With Dummy Data
```bash
python3 tools/agents/implementation/orchestrator/dummy_data_pipeline_orchestrator.py input.twbx -o output/ --all-agents
```

---

## Detailed Usage

### AGENT 1: Converter

Converts Tableau workbooks to Power BI projects.

**Basic:**
```bash
python3 tools/agents/implementation/agent-1-converter/converter_agent.py input.twbx -o output/
```

**With project name:**
```bash
python3 tools/agents/implementation/agent-1-converter/converter_agent.py input.twbx -o output/ -n "MyProject"
```

**Batch convert directory:**
```bash
python3 tools/agents/implementation/agent-1-converter/converter_agent.py input_folder/ -o output/ --batch
```

**With TDS files:**
```bash
python3 tools/agents/implementation/agent-1-converter/converter_agent.py input.twbx -o output/ --tds /path/to/tds/
```

**Python API:**
```python
from tools.agents.implementation.agent_1_converter.converter_agent import ConverterAgent

agent = ConverterAgent(verbose=True)
result = agent.convert_workbook(
    input_file='input.twbx',
    output_dir='output/',
    project_name='MyProject'
)
```

---

### AGENT 2: Dummy Data Generator

Creates synthetic test data without reading source files.

**Default (generic data):**
```bash
python3 tools/agents/implementation/agent-2-dummy-data/dummy_data_agent.py output/MyProject.pbip -r 100 -f csv
```

**With configuration:**
```bash
python3 tools/agents/implementation/agent-2-dummy-data/dummy_data_agent.py output/MyProject.pbip \
  -c config_dummy_data.yaml \
  -r 500 \
  -s 42 \
  -v
```

**Python API:**
```python
from tools.agents.implementation.agent_2_dummy_data.dummy_data_agent import DummyDataGenerator

generator = DummyDataGenerator(seed_val=42, verbose=True)
result = generator.generate_for_pbip(
    pbip_path='output/MyProject.pbip',
    data_config={
        'row_count': 100,
        'format': 'csv',
        'tables': {
            'Customers': {
                'columns': {
                    'CustomerID': 'int',
                    'Name': 'string',
                    'Email': 'string',
                    'JoinDate': 'date'
                },
                'rows': 500
            }
        }
    }
)
```

**Config file example** (config_dummy_data.yaml):
```yaml
generation:
  default_row_count: 100
  file_format: csv
  seed: 42

tables:
  Customers:
    rows: 500
    columns:
      CustomerID: int
      Name: string
      Email: string
      Country: string
      JoinDate: date
```

---

### AGENT 3: Realistic Data Generator

Analyzes actual data from Tableau and generates realistic synthetic data.

**Basic:**
```bash
python3 tools/agents/implementation/agent-3-realistic-data/realistic_data_agent.py input.twbx output/MyProject.pbip \
  --scale 1.0 \
  -f csv
```

**With custom seed:**
```bash
python3 tools/agents/implementation/agent-3-realistic-data/realistic_data_agent.py input.twbx output/MyProject.pbip \
  --scale 0.5 \
  --seed 42 \
  -v
```

**Python API:**
```python
from tools.agents.implementation.agent_3_realistic_data.realistic_data_agent import RealisticDummyGenerator

generator = RealisticDummyGenerator(seed_val=42, verbose=True)
result = generator.generate_realistic_data(
    twbx_path='input.twbx',
    pbip_path='output/MyProject.pbip',
    scale_factor=0.5,
    file_format='csv'
)
```

**Profile extraction:**
```python
from tools.agents.implementation.agent_3_realistic_data.realistic_data_agent import RealisticDummyGenerator

generator = RealisticDummyGenerator()

# Extract data from TWBX
tables = generator.extract_twbx_data('input.twbx')

# Analyze distributions
profiles = generator.profile_tables(tables)

# Generate from profiles
generated = generator.generate_data_from_profile(profiles, scale_factor=0.5)
```

---

## Configuration Files

### config_converter.yaml
Controls how Tableau is converted to Power BI:
- Validation rules
- Custom measure mappings
- Data source handling
- Output packaging

### config_dummy_data.yaml
Defines synthetic data generation:
- Row counts
- File format (CSV/JSON)
- Table and column definitions
- Data type templates

### config_realistic_data.yaml
Controls realistic data generation:
- Scale factors
- Data profiling settings
- Anonymization rules
- Per-table overrides

---

## Data Type Reference

### Agent 2 (Dummy Data) - Supported Types

| Type | Example | Notes |
|------|---------|-------|
| `string` | "Generic text" | |
| `name` | "John Smith" | First + last name |
| `first_name` | "John" | First names only |
| `last_name` | "Smith" | Last names only |
| `email` | "john@example.com" | |
| `int` | 42 | |
| `decimal` | 3.14 | |
| `price` / `amount` / `cost` | 99.99 | 10-10000 range |
| `quantity` / `count` | 500 | 1-1000 range |
| `percent` / `percentage` | 75.5 | 0-100 range |
| `date` | "2024-01-15" | Past year |
| `datetime` | "2024-01-15 14:30:00" | Past year + time |
| `category` | "Premium" | Low cardinality |
| `status` | "Active" | Status values |
| `bool` | true/false | |
| `city` | "New York" | |
| `state` | "CA" | US states |
| `country` | "USA" | |
| `product` | "Product A" | |

---

## Examples

### Example 1: Quick Smoke Test

```bash
# Agent 1: Convert
python3 tools/agents/implementation/agent-1-converter/converter_agent.py input.twbx -o /tmp/pbip_out

# Agent 2: Add quick dummy data
python3 tools/agents/implementation/agent-2-dummy-data/dummy_data_agent.py /tmp/pbip_out/input -r 100 -s 42
```

### Example 2: UAT Environment

```bash
# Agent 1: Convert
# Agent 3: Add realistic data
python3 tools/agents/implementation/orchestrator/dummy_data_pipeline_orchestrator.py input.twbx \
  -o /uat/pbip_out \
  --agent3
```

### Example 3: Custom Dummy Data

**Create config file** (my_config.json):
```json
{
  "row_count": 1000,
  "format": "csv",
  "tables": {
    "Orders": {
      "rows": 5000,
      "columns": {
        "OrderID": "int",
        "CustomerName": "name",
        "OrderDate": "date",
        "Amount": "price",
        "Status": "status"
      }
    },
    "Customers": {
      "rows": 500,
      "columns": {
        "CustomerID": "int",
        "Name": "name",
        "Email": "email",
        "City": "city",
        "Country": "country",
        "JoinDate": "date"
      }
    }
  }
}
```

**Generate data:**
```bash
python3 tools/agents/implementation/agent-2-dummy-data/dummy_data_agent.py /tmp/pbip_out/input \
  -c my_config.json \
  -s 42
```

---

## Architecture Benefits

✅ **Separation of Concerns** - Each agent has one clear responsibility  
✅ **Modularity** - Use agents independently or in pipeline  
✅ **Testability** - Each agent has clear input/output contracts  
✅ **Maintainability** - Updates to one agent don't affect others  
✅ **Flexibility** - Users choose which agents to run  
✅ **Reusability** - Agents can be imported into other tools  
✅ **Extensibility** - Easy to add new agents to the pipeline  

---

## Troubleshooting

### Agent 1 Conversion Fails
- Ensure `.twbx` is not corrupted: `unzip -t input.twbx`
- Check for TDS files: Provide `--tds` flag if needed
- Enable verbose mode: `-v` flag
- Check generated report for specific issues

### Agent 2 Data Not Generated
- Check PBIP project structure exists
- Ensure write permissions to output directory
- Try with `--seed` for reproducibility
- Use `-v` for detailed logs

### Agent 3 Can't Find Data in TWBX
- Agent 3 looks for `Data/` folder inside `.twbx`
- Tableau may store data differently; check TWBX contents
- If no data found, fall back to Agent 2
- Scale factor should be 0.1 - 2.0 range

---

## Performance Tips

- **Agent 1** typically takes 5-30 seconds per workbook
- **Agent 2** is very fast (~1 second for 1000 rows)
- **Agent 3** depends on TWBX size (data extraction + profiling)
- Use `--scale 0.1` for Agent 3 to quickly test with small data
- Batch convert with `--batch` flag for multiple workbooks

---

## Next Steps

1. **Extend Agent 2** - Add more sophisticated column templates
2. **Enhance Agent 3** - Add referential integrity generation
3. **Add Agent 4** - Packaging/PBIX creation
4. **Add Agent 5** - Deployment/publishing to Power BI Service
5. **Create GUI** - Web interface for three-agent pipeline
