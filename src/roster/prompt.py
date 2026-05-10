"""
prompt.py
---------
Interactive wizard that collects all inputs from the user at runtime:
  1. Roster period  (start / end dates)
  2. Account holiday dates
  3. Planned leave details  (optional)
  4. Shift rules  (paste table)
  5. Input Excel file  (path or upload)

Returns a fully-populated AppConfig + resolved input path.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ── rich for pretty output ─────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import print as rprint
    RICH = True
except ImportError:
    RICH = False

# ── questionary for interactive prompts ───────────────────────────────────────
try:
    import questionary
    from questionary import Style
    QUESTIONARY = True
except ImportError:
    QUESTIONARY = False

from .config import AppConfig, DEFAULT_SKILL_ALIAS, _parse_date

console = Console() if RICH else None

WIZARD_STYLE = Style([
    ("qmark",        "fg:#00d7ff bold"),
    ("question",     "fg:#ffffff bold"),
    ("answer",       "fg:#00ff87 bold"),
    ("pointer",      "fg:#00d7ff bold"),
    ("highlighted",  "fg:#00d7ff bold"),
    ("selected",     "fg:#00ff87"),
    ("separator",    "fg:#6c6c6c"),
    ("instruction",  "fg:#6c6c6c"),
]) if QUESTIONARY else None


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _banner() -> None:
    if RICH:
        console.print(Panel.fit(
            "[bold cyan]Shift Roster Generator[/bold cyan]\n"
            "[dim]Interactive Setup Wizard[/dim]",
            border_style="cyan",
            padding=(1, 4),
        ))
    else:
        print("\n" + "=" * 50)
        print("  Shift Roster Generator — Setup Wizard")
        print("=" * 50 + "\n")


def _section(title: str) -> None:
    if RICH:
        console.print(f"\n[bold yellow]▶  {title}[/bold yellow]")
    else:
        print(f"\n── {title} ──")


def _ask(question: str, default: str = "", validate=None) -> str:
    """Single-line text prompt with optional validation."""
    if QUESTIONARY:
        kwargs = dict(style=WIZARD_STYLE, default=default)
        if validate:
            kwargs["validate"] = validate
        return questionary.text(question, **kwargs).ask() or default
    prompt = f"{question}"
    if default:
        prompt += f" [{default}]"
    prompt += ": "
    val = input(prompt).strip()
    return val if val else default


def _ask_multiline(question: str, hint: str = "") -> str:
    """Collect multi-line input until the user enters a blank line."""
    if RICH:
        console.print(f"[bold]{question}[/bold]")
        if hint:
            console.print(f"[dim]{hint}[/dim]")
        console.print("[dim]  (Enter a blank line when done)[/dim]")
    else:
        print(f"\n{question}")
        if hint:
            print(f"  Hint: {hint}")
        print("  (Enter a blank line when done)")

    lines = []
    while True:
        line = input()
        if line.strip() == "":
            if lines:
                break
        else:
            lines.append(line)
    return "\n".join(lines)


def _parse_date_input(raw: str, year: int) -> date:
    """Parse a date from dd, dd/mm, dd/mm/yyyy or yyyy-mm-dd."""
    raw = raw.strip()
    # yyyy-mm-dd
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return datetime.strptime(raw, "%Y-%m-%d").date()
    # dd/mm/yyyy
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", raw):
        return datetime.strptime(raw, "%d/%m/%Y").date()
    # dd/mm (assume given year)
    if re.match(r"^\d{1,2}/\d{1,2}$", raw):
        return datetime.strptime(f"{raw}/{year}", "%d/%m/%Y").date()
    # bare day number (assume given year+month from context — caller must handle)
    if re.match(r"^\d{1,2}$", raw):
        return date(year, 1, int(raw))   # month patched by caller
    raise ValueError(f"Cannot parse date: {raw!r}")


def _parse_day_list(raw: str, year: int, month: int) -> list[date]:
    """
    Parse a comma/space-separated list of day numbers (or full dates)
    for a given year/month.
    Examples: "05, 09, 16"  or  "2026-06-05, 2026-06-09"
    """
    dates: list[date] = []
    for part in re.split(r"[,\s]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        if re.match(r"^\d{1,2}$", part):
            dates.append(date(year, month, int(part)))
        else:
            dates.append(_parse_date_input(part, year))
    return dates


def _parse_shift_rules_table(text: str) -> tuple[dict, dict]:
    """
    Parse a pasted Markdown-style shift-rules table.

    Returns (skill_rules, skill_alias_additions).

    Accepted row formats:
      | Monitoring | 2 | M / A / N | Every 5th day |
      | SRE Azure + Windows | 1 | E only | Saturday & Sunday |
    """
    skill_rules: dict = {}
    alias_additions: dict = {}

    for line in text.splitlines():
        line = line.strip()
        # skip separator and empty rows
        if not line or set(line.replace("|", "").replace("-", "").replace(" ", "")) == set():
            continue
        if line.startswith("|") and "---" in line:
            continue
        # skip header row
        if re.search(r"Skill\s*\|.*Count\s*\|.*Shift\s*\|.*Week", line, re.I):
            continue

        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 4:
            continue

        skill_raw  = parts[0].strip()
        # count     = parts[1].strip()   # informational only
        shift_raw  = parts[2].strip()
        week_raw   = parts[3].strip().lower()

        # Normalise shifts
        shifts: list[str] = []
        shift_upper = shift_raw.upper()
        if "E ONLY" in shift_upper or shift_upper == "E":
            shifts = ["E"]
        else:
            for code in ["M", "A", "N", "E"]:
                if code in shift_upper:
                    shifts.append(code)
        if not shifts:
            shifts = ["M", "A", "N"]

        # Normalise week-off
        if "5th" in week_raw or "5 th" in week_raw or "every" in week_raw:
            week_off = "every5th"
        else:
            week_off = "weekends"

        try:
            count = int(parts[1].strip())
        except ValueError:
            count = 1

        skill_rules[skill_raw] = {
            "count": count,
            "shifts": shifts,
            "week_off": week_off,
        }
        # Self-alias so the canonical name maps to itself
        alias_additions[skill_raw] = skill_raw

    return skill_rules, alias_additions


def _parse_leave_block(text: str, year: int, month: int) -> dict[str, list[date]]:
    """
    Parse planned leave block. Accepted formats:
      Guru Prasad – Planned Leave: 05, 08
      Alice: 10, 15
      Bob – 03
    """
    leaves: dict[str, list[date]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on – or : to get name / dates
        match = re.split(r"[–\-:]+", line, maxsplit=1)
        if len(match) < 2:
            continue
        name_part  = match[0].strip()
        dates_part = match[1].strip()
        # Strip "Planned Leave" prefix if present
        dates_part = re.sub(r"(?i)planned\s*leave\s*[:\-–]?\s*", "", dates_part).strip()
        name = re.sub(r"(?i)\s*(planned\s*leave)?\s*$", "", name_part).strip()
        if not name or not dates_part:
            continue
        try:
            leaves[name] = _parse_day_list(dates_part, year, month)
        except ValueError:
            if RICH:
                console.print(f"[yellow]  ⚠  Could not parse leave dates for '{name}': {dates_part}[/yellow]")
    return leaves


def _preview_rules(skill_rules: dict) -> None:
    if not RICH:
        print("\nParsed skill rules:")
        for sk, rule in skill_rules.items():
            print(f"  {sk}: shifts={rule['shifts']} week_off={rule['week_off']}")
        return

    table = Table(title="Parsed Skill Rules", border_style="dim")
    table.add_column("Skill",     style="cyan",  no_wrap=False)
    table.add_column("Count",     style="white", justify="center")
    table.add_column("Shifts",    style="green", justify="center")
    table.add_column("Week Off",  style="yellow", justify="center")
    for sk, rule in skill_rules.items():
        table.add_row(
            sk,
            str(rule["count"]),
            " / ".join(rule["shifts"]),
            "Every 5th Day" if rule["week_off"] == "every5th" else "Sat & Sun",
        )
    console.print(table)


def _confirm(question: str, default: bool = True) -> bool:
    if QUESTIONARY:
        return questionary.confirm(question, default=default, style=WIZARD_STYLE).ask()
    yn = "Y/n" if default else "y/N"
    val = input(f"{question} [{yn}]: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _resolve_input_file(year: int, month: int) -> Path:
    """Ask user for the Excel input file path (or accept drag-drop path)."""
    _section("Step 5 — Input File")

    default_paths = [
        Path.cwd() / "data" / "input" / "Roster - Input.xlsx",
        Path.cwd() / "Roster - Input.xlsx",
        Path.cwd() / "Roster_-_Input.xlsx",
    ]
    found_default = next((p for p in default_paths if p.exists()), None)

    if RICH:
        console.print("[dim]  Provide the full path to 'Roster - Input.xlsx'.[/dim]")
        if found_default:
            console.print(f"[dim]  Found automatically: {found_default}[/dim]")
    else:
        print("  Provide the full path to 'Roster - Input.xlsx'.")

    default_str = str(found_default) if found_default else ""

    while True:
        raw = _ask("  Excel file path", default=default_str)
        path = Path(raw.strip().strip("'\""))

        if not path.is_absolute():
            path = Path.cwd() / path

        if path.exists() and path.suffix.lower() in (".xlsx", ".xls"):
            # Copy to data/input if not already there
            dest = Path.cwd() / "data" / "input" / "Roster - Input.xlsx"
            if path.resolve() != dest.resolve():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                if RICH:
                    console.print(f"  [green]✔ Copied to {dest}[/green]")
            return dest
        elif not path.exists():
            if RICH:
                console.print(f"  [red]✘ File not found: {path}[/red]")
            else:
                print(f"  File not found: {path}")
        else:
            if RICH:
                console.print("  [red]✘ File must be .xlsx or .xls[/red]")
            else:
                print("  File must be .xlsx or .xls")


# ─── Public API ────────────────────────────────────────────────────────────────

@dataclass
class WizardResult:
    cfg: AppConfig
    input_file: Path
    output_file: Path


def run_wizard() -> WizardResult:
    """Run the interactive setup wizard and return a fully configured AppConfig."""

    _banner()

    # ── Step 1: Roster period ──────────────────────────────────────────────────
    _section("Step 1 — Roster Period")

    def _validate_date(val: str) -> bool | str:
        try:
            datetime.strptime(val.strip(), "%d/%m/%Y")
            return True
        except ValueError:
            try:
                datetime.strptime(val.strip(), "%Y-%m-%d")
                return True
            except ValueError:
                return "Use DD/MM/YYYY or YYYY-MM-DD"

    start_raw = _ask(
        "  Start date (DD/MM/YYYY or YYYY-MM-DD)",
        default="01/06/2026",
        validate=_validate_date if QUESTIONARY else None,
    )
    end_raw = _ask(
        "  End   date (DD/MM/YYYY or YYYY-MM-DD)",
        default="30/06/2026",
        validate=_validate_date if QUESTIONARY else None,
    )

    def _parse_period_date(raw: str) -> date:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(raw.strip(), fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Cannot parse: {raw}")

    roster_start = _parse_period_date(start_raw)
    roster_end   = _parse_period_date(end_raw)

    if RICH:
        console.print(f"  [green]✔  {roster_start.strftime('%d %B %Y')} → {roster_end.strftime('%d %B %Y')}[/green]")

    year  = roster_start.year
    month = roster_start.month

    # ── Step 2: Account holidays ───────────────────────────────────────────────
    _section("Step 2 — Account Holiday Dates")

    if RICH:
        console.print(f"  [dim]Enter day numbers (e.g. 05, 09, 16) for {roster_start.strftime('%B %Y')}[/dim]")

    holidays_raw = _ask(
        f"  Holiday dates in {roster_start.strftime('%B %Y')} (comma-separated day numbers)",
        default="05, 09, 16",
    )
    account_holidays: set[date] = set(_parse_day_list(holidays_raw, year, month))

    if RICH:
        hol_fmt = ", ".join(d.strftime("%d %b") for d in sorted(account_holidays))
        console.print(f"  [green]✔  Holidays: {hol_fmt}[/green]")

    # ── Step 3: Shift rules ────────────────────────────────────────────────────
    _section("Step 3 — Shift Rules")

    default_table = (
        "| Skill                   | Count | Shift     | Week Off          |\n"
        "| Monitoring              | 2     | M / A / N | Every 5th day     |\n"
        "| SRE Azure + Windows     | 1     | E only    | Saturday & Sunday |\n"
        "| Azure + Windows         | 2     | M / A / N | Saturday & Sunday |\n"
        "| Azure + Windows SME     | 1     | M / A / N | Saturday & Sunday |\n"
        "| OCI Azure + Windows     | 1     | M / A / N | Saturday & Sunday |\n"
        "| SRE OCI Azure + Linux   | 1     | M / A / N | Saturday & Sunday |\n"
        "| OCI Azure + Linux       | 2     | M / A / N | Every 5th day     |\n"
        "| OCI Azure + Linux SME   | 1     | M / A / N | Saturday & Sunday |\n"
        "| OCI Azure + Network AKS | 1     | M / A / N | Every 5th day     |"
    )

    use_default_rules = _confirm(
        "  Use the default shift rules table (shown in README)?",
        default=True,
    )

    if use_default_rules:
        rules_text = default_table
    else:
        rules_text = _ask_multiline(
            "  Paste the shift rules table",
            hint="| Skill | Count | Shift | Week Off |  (one rule per line)",
        )

    skill_rules, alias_additions = _parse_shift_rules_table(rules_text)

    if not skill_rules:
        if RICH:
            console.print("  [red]✘ No rules parsed — using defaults.[/red]")
        from .config import DEFAULT_SKILL_RULES
        skill_rules = DEFAULT_SKILL_RULES

    _preview_rules(skill_rules)

    # Merge alias map: keep defaults + add new skill names as self-aliases
    skill_alias = {**DEFAULT_SKILL_ALIAS, **alias_additions}

    # ── Step 4: Planned leaves ─────────────────────────────────────────────────
    _section("Step 4 — Planned Leave Details  (optional)")

    if RICH:
        console.print("  [dim]Format:  Employee Name – Planned Leave: 05, 08[/dim]")
        console.print("  [dim]Leave blank and press Enter to skip.[/dim]")
    else:
        print("  Format:  Employee Name – Planned Leave: 05, 08")
        print("  Leave blank and press Enter to skip.")

    planned_leaves: dict[str, list[date]] = {}
    while True:
        line = input("  > ").strip()
        if not line:
            break
        partial = _parse_leave_block(line, year, month)
        planned_leaves.update(partial)
        if RICH and partial:
            for name, dates in partial.items():
                dates_fmt = ", ".join(d.strftime("%d %b") for d in sorted(dates))
                console.print(f"    [green]✔  {name}: leave on {dates_fmt}[/green]")

    # ── Step 5: Input file ─────────────────────────────────────────────────────
    input_file = _resolve_input_file(year, month)

    # ── Output file ────────────────────────────────────────────────────────────
    out_dir = Path.cwd() / "data" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / "Roster - Out.xlsx"

    # ── Assemble config ────────────────────────────────────────────────────────
    cfg = AppConfig(
        input_file=input_file,
        output_file=output_file,
        roster_start=roster_start,
        roster_end=roster_end,
        account_holidays=account_holidays,
        planned_leaves=planned_leaves,
        skill_rules=skill_rules,
        skill_alias=skill_alias,
    )

    # ── Summary ────────────────────────────────────────────────────────────────
    if RICH:
        console.print(Panel(
            f"[bold]Period:[/bold]   {roster_start.strftime('%d %b %Y')} → {roster_end.strftime('%d %b %Y')}\n"
            f"[bold]Holidays:[/bold] {', '.join(d.strftime('%d %b') for d in sorted(account_holidays))}\n"
            f"[bold]Leaves:[/bold]   {len(planned_leaves)} employee(s)\n"
            f"[bold]Skills:[/bold]   {len(skill_rules)} rules loaded\n"
            f"[bold]Input:[/bold]    {input_file.name}\n"
            f"[bold]Output:[/bold]   {output_file}",
            title="[cyan]Configuration Summary[/cyan]",
            border_style="cyan",
            padding=(0, 2),
        ))
    else:
        print("\n── Configuration Summary ──")
        print(f"  Period   : {roster_start} → {roster_end}")
        print(f"  Holidays : {sorted(account_holidays)}")
        print(f"  Skills   : {len(skill_rules)} rules")
        print(f"  Input    : {input_file}")

    if not _confirm("\n  Generate the roster now?", default=True):
        if RICH:
            console.print("[yellow]Aborted.[/yellow]")
        else:
            print("Aborted.")
        sys.exit(0)

    return WizardResult(cfg=cfg, input_file=input_file, output_file=output_file)
