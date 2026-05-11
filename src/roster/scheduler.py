"""
scheduler.py
------------
Core shift-assignment engine.
Pure logic — no file I/O, no openpyxl, easy to unit-test.

Week-off rules are fully dynamic — driven by the string produced by
prompt._parse_week_off(), e.g.:
    "weekends"        → Sat + Sun off
    "every5th"        → every 5th day (staggered per peer)
    "every5th_6th"    → every 5th AND 6th day
    "every4th_6th"    → every 4th AND 6th day
    "custom:<raw>"    → weekends fallback
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from .config import AppConfig
from .loader import Employee


# ─── Shift labels (extended with CO) ──────────────────────────────────────────
SHIFT_LABELS = {
    "M":  "Morning (06:00–14:00)",
    "A":  "Afternoon (14:00–22:00)",
    "N":  "Night (22:00–06:00)",
    "E":  "Evening (14:00–22:00)",
    "G":  "General (M/A/N rotation)",
    "CO": "Comp-Off / Cut-Off",
    "H":  "Holiday",
    "L":  "Leave",
    "W":  "Week Off",
}


# ─── Date helpers ──────────────────────────────────────────────────────────────

def iter_dates(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5   # Sat=5, Sun=6


# ─── Dynamic week-off calculator ───────────────────────────────────────────────

def _compute_week_offs(
    start: date,
    end:   date,
    week_off_rule: str,
    peer_index: int = 0,
) -> set[date]:
    """
    Compute the set of week-off dates for a given rule string.

    peer_index is used to stagger cycle-based offs so that employees in
    the same skill group don't all take the same days off.

    Supported rule formats (produced by prompt._parse_week_off):
        "weekends"          → all Saturdays & Sundays
        "every5th"          → every 5th day  (staggered)
        "every5th_6th"      → every 5th AND every 6th day
        "every4th_6th"      → every 4th AND every 6th day
        "every3rd"          → every 3rd day
        "custom:<anything>" → falls back to weekends
    """
    all_dates = list(iter_dates(start, end))

    if week_off_rule == "weekends" or week_off_rule.startswith("custom:"):
        return {d for d in all_dates if is_weekend(d)}

    # Extract all N values from the rule, e.g. "every5th_6th" → [5, 6]
    ns = [int(x) for x in re.findall(r"(\d+)", week_off_rule)]
    if not ns:
        return {d for d in all_dates if is_weekend(d)}

    offs: set[date] = set()
    for n in ns:
        count = peer_index   # stagger start so peers don't all coincide
        for d in all_dates:
            count += 1
            if count % n == 0:
                offs.add(d)
    return offs


# ─── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EmployeeSchedule:
    employee:  Employee
    daily:     dict[date, str]   # date → shift code
    week_offs: set[date]
    leaves:    set[date]

    def shift_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for code in self.daily.values():
            counts[code] = counts.get(code, 0) + 1
        return counts

    def working_days(self) -> int:
        return sum(1 for c in self.daily.values() if c not in ("H", "L", "W"))


# ─── Scheduler ─────────────────────────────────────────────────────────────────

class ShiftScheduler:
    """
    Assigns shifts to all employees for the configured roster period.

    Usage:
        scheduler = ShiftScheduler(cfg, employees)
        schedules = scheduler.run()   # list[EmployeeSchedule]
    """

    def __init__(self, cfg: AppConfig, employees: list[Employee]) -> None:
        self.cfg = cfg
        self.employees = employees
        self._all_dates: list[date] = list(iter_dates(cfg.roster_start, cfg.roster_end))

    # ── Public ─────────────────────────────────────────────────────────────────

    def run(self) -> list[EmployeeSchedule]:
        return [self._schedule_employee(emp) for emp in self.employees]

    # ── Private ────────────────────────────────────────────────────────────────

    def _peer_index(self, emp: Employee) -> int:
        """
        Count how many employees with the same skill appear before this one.
        Used to stagger cycle-based week-offs so coverage is maintained.
        """
        idx = 0
        for other in self.employees:
            if other.name == emp.name:
                break
            if other.skill == emp.skill:
                idx += 1
        return idx

    def _week_offs_for(self, emp: Employee) -> set[date]:
        """Return the set of week-off dates for this employee."""
        rule     = self.cfg.skill_rules.get(emp.skill, {})
        wo_rule  = rule.get("week_off", "weekends")
        peer_idx = self._peer_index(emp)
        return _compute_week_offs(
            self.cfg.roster_start,
            self.cfg.roster_end,
            wo_rule,
            peer_index=peer_idx,
        )

    def _schedule_employee(self, emp: Employee) -> EmployeeSchedule:
        cfg            = self.cfg
        rule           = cfg.skill_rules.get(emp.skill, {})
        allowed_shifts = rule.get("shifts", ["M", "A", "N"])

        leaves:    set[date] = set(cfg.planned_leaves.get(emp.name, []))
        week_offs: set[date] = self._week_offs_for(emp)

        shift_cycle = itertools.cycle(allowed_shifts)
        daily: dict[date, str] = {}

        for d in self._all_dates:
            if d in leaves:
                daily[d] = "L"
            elif d in cfg.account_holidays:
                daily[d] = "H"
            elif d in week_offs:
                daily[d] = "W"
            else:
                daily[d] = next(shift_cycle)

        return EmployeeSchedule(
            employee=emp,
            daily=daily,
            week_offs=week_offs,
            leaves=leaves,
        )
