"""
scheduler.py
------------
Core shift-assignment engine.
Pure logic — no file I/O, no openpyxl, easy to unit-test.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

from .config import AppConfig
from .loader import Employee


# ─── Shift codes ───────────────────────────────────────────────────────────────
# M = Morning, A = Afternoon, N = Night, E = Evening
# H = Holiday,  L = Leave,  W = Week Off

SHIFT_LABELS = {
    "M": "Morning (06:00–14:00)",
    "A": "Afternoon (14:00–22:00)",
    "N": "Night (22:00–06:00)",
    "E": "Evening (14:00–22:00)",
    "H": "Holiday",
    "L": "Leave",
    "W": "Week Off",
}


# ─── Helpers ───────────────────────────────────────────────────────────────────

def iter_dates(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def _weekend_offs(start: date, end: date) -> set[date]:
    return {d for d in iter_dates(start, end) if is_weekend(d)}


def _every5th_offs(start: date, end: date, offset: int = 0) -> set[date]:
    """
    Return dates that fall on every 5th working slot starting with `offset`
    so different employees in the same skill group get staggered days off.
    """
    offs: set[date] = set()
    count = offset
    for d in iter_dates(start, end):
        count += 1
        if count % 5 == 0:
            offs.add(d)
    return offs


# ─── Result types ──────────────────────────────────────────────────────────────

@dataclass
class EmployeeSchedule:
    employee: Employee
    daily: dict[date, str]          # date → shift code
    week_offs: set[date]
    leaves: set[date]

    def shift_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {k: 0 for k in "MANEWHLX"}
        for code in self.daily.values():
            counts[code] = counts.get(code, 0) + 1
        return counts

    def working_days(self) -> int:
        return sum(1 for c in self.daily.values() if c in ("M", "A", "N", "E"))


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
        return [self._schedule_employee(emp, idx) for idx, emp in enumerate(self.employees)]

    # ── Private ────────────────────────────────────────────────────────────────

    def _week_offs_for(self, emp: Employee, peer_index: int) -> set[date]:
        """Return the set of week-off dates for this employee."""
        rule = self.cfg.skill_rules.get(emp.skill, {})
        wo_type = rule.get("week_off", "weekends")

        if wo_type == "weekends":
            return _weekend_offs(self.cfg.roster_start, self.cfg.roster_end)

        # every5th — stagger by peer_index so coverage isn't lost
        return _every5th_offs(
            self.cfg.roster_start,
            self.cfg.roster_end,
            offset=peer_index,
        )

    def _peer_index(self, emp: Employee) -> int:
        """
        Return how many employees with the same skill appear before this one
        in the employee list (used to stagger every-5th week-offs).
        """
        idx = 0
        for other in self.employees:
            if other.name == emp.name:
                break
            if other.skill == emp.skill:
                idx += 1
        return idx

    def _schedule_employee(self, emp: Employee, _: int) -> EmployeeSchedule:
        cfg = self.cfg
        rule = cfg.skill_rules.get(emp.skill, {})
        allowed_shifts: list[str] = rule.get("shifts", ["M", "A", "N"])

        leaves: set[date] = set(cfg.planned_leaves.get(emp.name, []))
        week_offs: set[date] = self._week_offs_for(emp, self._peer_index(emp))

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
