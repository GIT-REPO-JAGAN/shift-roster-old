Shift Roster Generator
A Python tool that generates a formatted monthly shift roster Excel workbook
from an employee input spreadsheet.
---
Quick Start
1 — Clone & open in Codespaces / VS Code Dev Container
```bash
git clone https://gitlab.com/<your-group>/shift-roster.git
cd shift-roster
# Open in VS Code → "Reopen in Container"
# Or launch a GitLab / GitHub Codespace from the repository page
```
The dev container automatically installs all dependencies on first start.
2 — Add your input file
Place your employee spreadsheet at:
```
data/input/Roster - Input.xlsx
```
Expected columns: `Name`, `Email`, `Skill`, `Location`
3 — Configure the roster
Edit `config.yaml` in the project root:
```yaml
roster_start: "2026-06-01"
roster_end:   "2026-06-30"

account_holidays:
  - "2026-06-05"
  - "2026-06-09"
  - "2026-06-16"

planned_leaves:
  "Guru Prasad L":
    - "2026-06-05"
    - "2026-06-08"
```
4 — Generate the roster
```bash
# Default (uses config.yaml)
python -m roster

# With verbose output
python -m roster --verbose

# Override paths on the command line
python -m roster --input path/to/input.xlsx --output path/to/output.xlsx

# Override the roster period
python -m roster --start 2026-07-01 --end 2026-07-31
```
Output is written to `data/output/Roster - Out.xlsx`.
---
Project Structure
```
shift-roster/
├── .devcontainer/
│   └── devcontainer.json      # VS Code / Codespaces container config
├── .gitlab/
│   └── merge_request_templates/
│       └── Default.md
├── .gitlab-ci.yml             # CI/CD pipeline (lint → test → generate)
├── src/
│   └── roster/
│       ├── __init__.py        # Public API
│       ├── __main__.py        # CLI entry point
│       ├── config.py          # Configuration loader
│       ├── loader.py          # Input Excel reader
│       ├── scheduler.py       # Shift-assignment engine (pure logic)
│       └── writer.py          # Excel output formatter
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   └── test_scheduler.py
├── data/
│   ├── input/                 # Place Roster - Input.xlsx here
│   └── output/                # Generated Roster - Out.xlsx appears here
├── config.yaml                # ← Edit this for each roster period
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```
---
Shift Codes
Code	Shift	Hours
M	Morning	06:00 – 14:00
A	Afternoon	14:00 – 22:00
N	Night	22:00 – 06:00
E	Evening (SRE only)	14:00 – 22:00
H	Account Holiday	—
L	Planned Leave	—
W	Week Off	—
Week-Off Rules
Type	Applies to
Weekends	SRE Azure+Windows, Azure+Windows, Azure+Windows SME, OCI Azure+Windows, SRE OCI Azure+Linux, OCI Azure+Linux SME
Every 5th Day (staggered)	Monitoring, OCI Azure+Linux, OCI Azure+Network AKS
---
Running Tests
```bash
pytest                          # all tests
pytest -v                       # verbose
pytest --cov=roster             # with coverage
```
Linting
```bash
ruff check src/ tests/          # style + import checks
ruff check --fix src/ tests/    # auto-fix where possible
```
---
Environment Variables
All settings in `config.yaml` can be overridden with environment variables:
Variable	Description	Example
`ROSTER_INPUT`	Path to input Excel file	`data/input/Roster - Input.xlsx`
`ROSTER_OUTPUT`	Path to output Excel file	`data/output/Roster - Out.xlsx`
`ROSTER_START`	Roster start date	`2026-07-01`
`ROSTER_END`	Roster end date	`2026-07-31`
Copy `.env.example` to `.env` for local development.
---
GitLab CI/CD
The pipeline runs automatically on every push:
Stage	Job	What it does
lint	`lint:ruff`	Checks code style with ruff
test	`test:pytest`	Runs unit tests + publishes coverage report
generate	`generate:roster`	Produces `Roster - Out.xlsx` as a CI artifact
The generated Excel file is available as a downloadable artifact in the
GitLab pipeline → generate:roster → Download artifacts.
The `generate` job runs automatically on `main`/`master` and can also be
triggered manually from any branch via GitLab CI/CD → Pipelines → Run pipeline.
