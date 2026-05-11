"""
prompt.py
---------
Shared parsing utilities used by both the CLI wizard and the Flask web app.

Public functions:
    _parse_shift_rules_table(text)  → (skill_rules, alias_additions)
    _parse_day_list(raw, year, month) → list[date]
    _parse_leave_block(text, year, month) → dict[str, list[date]]

CLI wizard:
    run_wizard() → WizardResult
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import print as rprint
    RICH = True
except ImportError:
    RICH = False

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


# ─── Valid shift codes ─────────────────────────────────────────────────────────
# All single-letter codes that are accepted as shift identifiers.
# G = General (any shift, treated as M/A/N rotation)
VALID_SHIFT_CODES = {"M", "A", "N", "E", "G", "CO"}

SHIFT_EXPANSION = {
    "G":  ["M", "A", "N"],   # General      = full M/A/N rotation
    "CO": ["CO"],              # Comp-Off     = dedicated shift code
    "M":  ["M"],
    "A":  ["A"],
    "N":  ["N"],
    "E":  ["E"],
}


# ─── Core parsers (used by both CLI and web) ───────────────────────────────────

def _parse_date_input(raw: str, year: int) -> date:
    raw = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return datetime.strptime(raw, "%Y-%m-%d").date()
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", raw):
        return datetime.strptime(raw, "%d/%m/%Y").date()
    if re.match(r"^\d{1,2}/\d{1,2}$", raw):
        return datetime.strptime(f"{raw}/{year}", "%d/%m/%Y").date()
    if re.match(r"^\d{1,2}$", raw):
        return date(year, 1, int(raw))
    raise ValueError(f"Cannot parse date: {raw!r}")


def _parse_day_list(raw: str, year: int, month: int) -> list[date]:
    """
    Parse comma/space-separated day numbers (or full dates) for a given year/month.
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

    Accepted shift values (case-insensitive):
        M / A / N          → Morning, Afternoon, Night rotation
        M / A / N / E      → any combination
        E only  or  E      → Evening only
        G                  → General = M/A/N rotation (alias for full rotation)
        <any single code>  → treated as that code only

    Returns:
        skill_rules      : {skill_name: {"count": int, "shifts": [...], "week_off": str}}
        alias_additions  : {skill_name: skill_name}  (self-aliases for dynamic skills)
    """
    skill_rules: dict = {}
    alias_additions: dict = {}

    for line in text.splitlines():
        line = line.strip()
        # Skip empty / separator rows
        if not line:
            continue
        if line.startswith("|") and re.match(r"^[\|\-\s:]+$", line):
            continue
        # Skip header row
        if re.search(r"Skill\s*\|.*Count\s*\|.*Shift\s*\|.*Week", line, re.I):
            continue

        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 4:
            continue

        skill_raw = parts[0].strip()
        shift_raw = parts[2].strip()
        week_raw  = parts[3].strip().lower()

        if not skill_raw:
            continue

        # ── Parse shift codes ──────────────────────────────────────────────────
        shifts = _parse_shifts(shift_raw)

        # ── Parse week-off — fully dynamic, no hardcoded patterns ────────────
        week_off = _parse_week_off(week_raw)

        # ── Parse count ───────────────────────────────────────────────────────
        try:
            count = int(parts[1].strip())
        except ValueError:
            count = 1

        skill_rules[skill_raw] = {
            "count":    count,
            "shifts":   shifts,
            "week_off": week_off,
        }
        # Self-alias: the skill name typed in the table maps to itself.
        # This allows the loader to match employee skill names that
        # exactly match what the user typed.
        alias_additions[skill_raw] = skill_raw

    return skill_rules, alias_additions


def _parse_shifts(raw: str) -> list[str]:
    """
    Convert a raw shift string into a list of shift codes.

    Examples:
        "M / A / N"   → ["M", "A", "N"]
        "E only"      → ["E"]
        "E"           → ["E"]
        "G"           → ["M", "A", "N"]   (General)
        "M / A"       → ["M", "A"]
        ""            → ["M", "A", "N"]   (default)
    """
    raw = raw.strip()
    upper = raw.upper()

    # "E only" or standalone "E"
    if re.match(r"^E\s*(ONLY)?$", upper):
        return ["E"]

    # "CO only" or standalone "CO"
    if re.match(r"^CO\s*(ONLY)?$", upper):
        return ["CO"]

    # "G" or "G only" → General = M/A/N
    if re.match(r"^G\s*(ONLY)?$", upper):
        return ["M", "A", "N"]

    found: list[str] = []
    # Try slash/space/comma-separated tokens first: M / A / N, CO, etc.
    tokens = re.split(r"[\s/,]+", upper)
    for tok in tokens:
        tok = tok.strip()
        if tok in VALID_SHIFT_CODES:
            if tok == "G":
                for c in ["M", "A", "N"]:
                    if c not in found:
                        found.append(c)
            elif tok not in found:
                found.append(tok)

    if found:
        return found

    # Fallback: scan character by character (single-char codes only)
    for ch in upper:
        if ch in VALID_SHIFT_CODES and ch not in ("G", "C", "O") and ch not in found:
            found.append(ch)

    return found if found else ["M", "A", "N"]


def _parse_week_off(raw: str) -> str:
    """
    Parse any week-off description into a structured dict.

    Returns a string key in the form:
        "weekends"           → Saturday & Sunday
        "every_Nth_day"      → e.g. every5th, every4th
        "every_Nth_Mth_day"  → e.g. every5th_6th (multiple)
        "custom:<raw>"       → anything else, preserved verbatim

    Examples:
        "Saturday & Sunday"        → "weekends"
        "Every 5th day"            → "every5th"
        "Every 5th & 6th days"     → "every5th_6th"
        "Every 4th & 6th day"      → "every4th_6th"
        "Every 3rd day"            → "every3rd"
        "Wed & Thu"                → "custom:wed & thu"
    """
    import re as _re
    raw_lower = raw.lower().strip()

    # Weekends pattern
    if _re.search(r"sat|sun|weekend", raw_lower):
        return "weekends"

    # Extract all ordinal numbers: 5th, 4th, 3rd, 2nd, 1st, or bare digits
    ordinals = _re.findall(r"(\d+)(?:st|nd|rd|th)?", raw_lower)

    if ordinals:
        # Remove duplicates while preserving order
        seen = []
        for o in ordinals:
            if o not in seen:
                seen.append(o)
        if len(seen) == 1:
            return f"every{seen[0]}th"
        else:
            return "every" + "_".join(f"{o}th" for o in seen)

    # Fallback: preserve the raw string as a custom key
    return f"custom:{raw_lower}"


def _week_off_dates(start, end, week_off_rule: str):
    """
    Generate the set of dates that are week-offs given a rule string
    returned by _parse_week_off().

    Parameters
    ----------
    start, end : datetime.date
    week_off_rule : str   one of the keys produced by _parse_week_off()

    Returns
    -------
    set[datetime.date]
    """
    from datetime import timedelta as _td
    import re as _re

    all_dates = []
    d = start
    while d <= end:
        all_dates.append(d)
        d += _td(days=1)

    if week_off_rule == "weekends":
        return {d for d in all_dates if d.weekday() >= 5}

    # every_Nth or every_Nth_Mth — cycle-based offs
    # Parse all N values from the rule string
    offsets = [int(x) for x in _re.findall(r"(\d+)", week_off_rule)]
    if not offsets:
        return {d for d in all_dates if d.weekday() >= 5}

    # Build a set of dates where any counter mod N == 0
    offs = set()
    for offset_n in offsets:
        count = 0
        for d in all_dates:
            count += 1
            if count % offset_n == 0:
                offs.add(d)
    return offs


def build_skill_alias_map(
    excel_skills: list[str],
    rule_skills: list[str],
) -> dict[str, str]:
    """
    Build a comprehensive alias map by intelligently matching:
      - Excel skill names  (raw values from the input file)
      - Rule skill names   (what the user typed in the Shift Assignments table)

    Strategy (in order):
      1. Exact match (case-insensitive)
      2. Normalised match (strip punctuation/spaces)
      3. Keep both as-is (self-alias) so unmatched names still work

    Returns: {raw_excel_skill: canonical_rule_skill}
    Also includes {rule_skill: rule_skill} self-aliases.
    """
    alias: dict[str, str] = {}

    def _normalise(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    rule_norm = {_normalise(r): r for r in rule_skills}

    for excel_skill in excel_skills:
        # 1. Exact match
        if excel_skill in rule_skills:
            alias[excel_skill] = excel_skill
            continue
        # 2. Case-insensitive exact
        lower_map = {r.lower(): r for r in rule_skills}
        if excel_skill.lower() in lower_map:
            alias[excel_skill] = lower_map[excel_skill.lower()]
            continue
        # 3. Normalised match
        norm = _normalise(excel_skill)
        if norm in rule_norm:
            alias[excel_skill] = rule_norm[norm]
            continue
        # 4. Keep as-is (no match found — employee will get default rule)
        alias[excel_skill] = excel_skill

    # Always add self-aliases for rule skills
    for rs in rule_skills:
        alias[rs] = rs

    # Merge with legacy hardcoded aliases as final fallback
    merged = {**DEFAULT_SKILL_ALIAS, **alias}
    return merged


def _parse_leave_block(text: str, year: int, month: int) -> dict[str, list[date]]:
    """
    Parse planned leave block.
    Accepted formats:
      Guru Prasad – Planned Leave: 05, 08
      Alice: 10, 15
      Bob – 03
    """
    leaves: dict[str, list[date]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.split(r"[–\-:]+", line, maxsplit=1)
        if len(match) < 2:
            continue
        name_part  = match[0].strip()
        dates_part = match[1].strip()
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


# ─── CLI-only helpers ──────────────────────────────────────────────────────────

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


def _preview_rules(skill_rules: dict) -> None:
    if not RICH:
        print("\nParsed skill rules:")
        for sk, rule in skill_rules.items():
            print(f"  {sk}: shifts={rule['shifts']} week_off={rule['week_off']}")
        return
    table = Table(title="Parsed Skill Rules", border_style="dim")
    table.add_column("Skill",    style="cyan",  no_wrap=False)
    table.add_column("Count",    style="white", justify="center")
    table.add_column("Shifts",   style="green", justify="center")
    table.add_column("Week Off", style="yellow", justify="center")
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
    default_str = str(found_default) if found_default else ""
    while True:
        raw  = _ask("  Excel file path", default=default_str)
        path = Path(raw.strip().strip("'\""))
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.exists() and path.suffix.lower() in (".xlsx", ".xls"):
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


# ─── Public CLI wizard ─────────────────────────────────────────────────────────

@dataclass
class WizardResult:
    cfg: AppConfig
    input_file: Path
    output_file: Path


def run_wizard() -> WizardResult:
    _banner()

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

    start_raw = _ask("  Start date (DD/MM/YYYY or YYYY-MM-DD)", default="01/06/2026",
                     validate=_validate_date if QUESTIONARY else None)
    end_raw   = _ask("  End   date (DD/MM/YYYY or YYYY-MM-DD)", default="30/06/2026",
                     validate=_validate_date if QUESTIONARY else None)

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

    _section("Step 2 — Account Holiday Dates")
    if RICH:
        console.print(f"  [dim]Enter day numbers for {roster_start.strftime('%B %Y')}[/dim]")
    holidays_raw = _ask(
        f"  Holiday dates in {roster_start.strftime('%B %Y')} (comma-separated day numbers)",
        default="05, 09, 16",
    )
    account_holidays: set[date] = set(_parse_day_list(holidays_raw, year, month))
    if RICH:
        hol_fmt = ", ".join(d.strftime("%d %b") for d in sorted(account_holidays))
        console.print(f"  [green]✔  Holidays: {hol_fmt}[/green]")

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
    use_default_rules = _confirm("  Use the default shift rules table?", default=True)
    rules_text = default_table if use_default_rules else _ask_multiline(
        "  Paste the shift rules table",
        hint="| Skill | Count | Shift | Week Off |  (one rule per line)",
    )
    skill_rules, alias_additions = _parse_shift_rules_table(rules_text)
    if not skill_rules:
        from .config import DEFAULT_SKILL_RULES
        skill_rules = DEFAULT_SKILL_RULES
    _preview_rules(skill_rules)

    _section("Step 4 — Planned Leave Details  (optional)")
    if RICH:
        console.print("  [dim]Format:  Employee Name – Planned Leave: 05, 08[/dim]")
        console.print("  [dim]Leave blank and press Enter to skip.[/dim]")
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

    input_file = _resolve_input_file(year, month)

    # Build dynamic alias map from the actual Excel file
    import pandas as pd
    try:
        df = pd.read_excel(input_file, dtype=str)
        df.columns = [c.strip() for c in df.columns]
        excel_skills = df["Skill"].dropna().str.strip().unique().tolist()
    except Exception:
        excel_skills = []

    skill_alias = build_skill_alias_map(excel_skills, list(skill_rules.keys()))

    out_dir = Path.cwd() / "data" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / "Roster - Out.xlsx"

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
    if not _confirm("\n  Generate the roster now?", default=True):
        if RICH:
            console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)

    return WizardResult(cfg=cfg, input_file=input_file, output_file=output_file)
