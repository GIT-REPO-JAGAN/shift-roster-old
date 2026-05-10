"""
__main__.py
-----------
Entry point: python -m roster

Modes
-----
  Interactive (default):
      python -m roster
      python -m roster --interactive

  Non-interactive / scripted (all flags supplied):
      python -m roster --no-interactive \\
                        --input  data/input/Roster-Input.xlsx \\
                        --output data/output/Roster-Out.xlsx  \\
                        --start  2026-06-01 --end 2026-06-30  \\
                        --holidays "05,09,16"

  Console-script alias:
      roster-generate          (after pip install -e .)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="roster",
        description="Interactive Shift Roster Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m roster                          # interactive wizard\n"
            "  python -m roster --no-interactive \\       # fully scripted\n"
            "    --start 2026-07-01 --end 2026-07-31 \\\n"
            "    --holidays '04,14,21' --input data/input/emp.xlsx\n"
        ),
    )
    p.add_argument("--interactive", action="store_true", default=True,
                   help="Run the interactive setup wizard (default)")
    p.add_argument("--no-interactive", dest="interactive", action="store_false",
                   help="Skip wizard — all flags must be provided")
    p.add_argument("--config",    type=Path, default=None,
                   help="Path to config.yaml")
    p.add_argument("--input",     type=Path, default=None,
                   help="Employee input Excel file (.xlsx)")
    p.add_argument("--output",    type=Path, default=None,
                   help="Output Excel file path")
    p.add_argument("--start",     type=str,  default=None,
                   help="Roster start date  YYYY-MM-DD")
    p.add_argument("--end",       type=str,  default=None,
                   help="Roster end date    YYYY-MM-DD")
    p.add_argument("--holidays",  type=str,  default=None,
                   help='Comma-separated holiday day numbers e.g. "05,09,16"')
    p.add_argument("--verbose",   action="store_true",
                   help="Print extra progress information")
    return p.parse_args()


def _scripted_run(args: argparse.Namespace) -> int:
    """Non-interactive run: load config from file/flags and generate."""
    from .config import load as load_config
    from .loader import load_employees
    from .scheduler import ShiftScheduler
    from .writer import write_workbook
    from datetime import date
    import re

    cfg = load_config(args.config)

    if args.input:
        cfg.input_file = args.input.resolve()
    if args.output:
        cfg.output_file = args.output.resolve()
    if args.start:
        cfg.roster_start = datetime.strptime(args.start, "%Y-%m-%d").date()
    if args.end:
        cfg.roster_end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if args.holidays:
        days = [d.strip() for d in re.split(r"[,\s]+", args.holidays) if d.strip()]
        cfg.account_holidays = {
            date(cfg.roster_start.year, cfg.roster_start.month, int(d))
            for d in days
        }

    if args.verbose:
        print(f"[roster] Input    : {cfg.input_file}")
        print(f"[roster] Output   : {cfg.output_file}")
        print(f"[roster] Period   : {cfg.roster_start} -> {cfg.roster_end}")
        print(f"[roster] Holidays : {sorted(cfg.account_holidays)}")

    try:
        employees = load_employees(cfg)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"[roster] Loaded {len(employees)} employees")

    scheduler   = ShiftScheduler(cfg, employees)
    schedules   = scheduler.run()
    output_path = write_workbook(cfg, schedules)

    print(f"Roster generated -> {output_path}")
    print(f"Employees : {len(schedules)}")
    print(f"Period    : {cfg.roster_start} -> {cfg.roster_end}")
    return 0


def _interactive_run(args: argparse.Namespace) -> int:
    """Interactive wizard run."""
    from .prompt import run_wizard
    from .loader import load_employees
    from .scheduler import ShiftScheduler
    from .writer import write_workbook

    result = run_wizard()
    cfg    = result.cfg

    if RICH:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Loading employees ...", total=None)
            try:
                employees = load_employees(cfg)
            except (FileNotFoundError, ValueError) as exc:
                console.print(f"[red]ERROR: {exc}[/red]")
                return 1

            progress.update(task, description=f"Scheduling {len(employees)} employees ...")
            scheduler = ShiftScheduler(cfg, employees)
            schedules = scheduler.run()

            progress.update(task, description="Writing Excel workbook ...")
            output_path = write_workbook(cfg, schedules)

        console.print(
            f"\n[bold green]Roster generated successfully![/bold green]\n"
            f"   [bold]Output    :[/bold] {output_path}\n"
            f"   [bold]Employees :[/bold] {len(schedules)}\n"
            f"   [bold]Period    :[/bold] {cfg.roster_start.strftime('%d %b %Y')} -> "
            f"{cfg.roster_end.strftime('%d %b %Y')}\n"
            f"\n[dim]Tip: Right-click the output file in VS Code Explorer -> Download[/dim]"
        )
    else:
        try:
            employees = load_employees(cfg)
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        print(f"Scheduling {len(employees)} employees ...")
        scheduler   = ShiftScheduler(cfg, employees)
        schedules   = scheduler.run()
        print("Writing Excel workbook ...")
        output_path = write_workbook(cfg, schedules)
        print(f"\nRoster generated -> {output_path}")
        print(f"Employees : {len(schedules)}")
        print(f"Period    : {cfg.roster_start} -> {cfg.roster_end}")

    return 0


def main() -> int:
    args = _parse_args()
    if not args.interactive:
        return _scripted_run(args)
    return _interactive_run(args)


if __name__ == "__main__":
    sys.exit(main())
