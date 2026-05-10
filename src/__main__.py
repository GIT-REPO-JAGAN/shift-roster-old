"""
__main__.py
-----------
Entry point: python -m roster  OR  roster-generate (console_script)

Usage examples:
  python -m roster
  python -m roster --input data/input/Roster-Input.xlsx --output data/output/Roster-Out.xlsx
  python -m roster --config custom-config.yaml
  python -m roster --start 2026-07-01 --end 2026-07-31
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import load as load_config
from .loader import load_employees
from .scheduler import ShiftScheduler
from .writer import write_workbook


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="roster",
        description="Generate a monthly shift roster Excel workbook.",
    )
    p.add_argument("--config",  type=Path, default=None,
                   help="Path to config.yaml (default: <repo-root>/config.yaml)")
    p.add_argument("--input",   type=Path, default=None,
                   help="Override input Excel file path")
    p.add_argument("--output",  type=Path, default=None,
                   help="Override output Excel file path")
    p.add_argument("--start",   type=str,  default=None,
                   help="Roster start date YYYY-MM-DD")
    p.add_argument("--end",     type=str,  default=None,
                   help="Roster end date YYYY-MM-DD")
    p.add_argument("--verbose", action="store_true",
                   help="Print detailed progress")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # Load base config
    cfg = load_config(args.config)

    # CLI overrides (highest priority)
    if args.input:
        cfg.input_file = args.input.resolve()
    if args.output:
        cfg.output_file = args.output.resolve()
    if args.start:
        cfg.roster_start = datetime.strptime(args.start, "%Y-%m-%d").date()
    if args.end:
        cfg.roster_end = datetime.strptime(args.end, "%Y-%m-%d").date()

    if args.verbose:
        print(f"[roster] Input  : {cfg.input_file}")
        print(f"[roster] Output : {cfg.output_file}")
        print(f"[roster] Period : {cfg.roster_start} → {cfg.roster_end}")
        print(f"[roster] Holidays: {sorted(cfg.account_holidays)}")

    try:
        employees = load_employees(cfg)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"[roster] Loaded {len(employees)} employees")

    scheduler  = ShiftScheduler(cfg, employees)
    schedules  = scheduler.run()
    output_path = write_workbook(cfg, schedules)

    print(f"✅  Roster generated → {output_path}")
    print(f"    Employees : {len(schedules)}")
    print(f"    Period    : {cfg.roster_start} → {cfg.roster_end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
