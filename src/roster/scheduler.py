"""
scheduler.py
------------
Core shift-assignment engine supporting:
  - Positional shift allocation: 2M / 2A / 2N within a skill group
  - N-week rotation schedules (1/2/3/4-week)
  - Dynamic week-off rules: weekends | rolling(6th&7th) | every-Nth
  - Planned Leave (PL) and Comp-Off (CO) as separate day codes
  - Account holidays (H)
  - G (General) shift stays as G — no M/A/N expansion
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from .config import AppConfig
from .loader import Employee


# ─── Shift / day labels ────────────────────────────────────────────────────────
SHIFT_LABELS = {
    "M":  "Morning (06:00–14:00)",
    "A":  "Afternoon (14:00–22:00)",
    "N":  "Night (22:00–06:00)",
    "E":  "Evening (14:00–22:00)",
    "G":  "General (M/A/N rotation)",
    "PL": "Planned Leave",
    "CO": "Comp-Off",
    "H":  "Holiday",
    "W":  "Week Off",
}

NON_WORKING = {"H", "W", "PL", "CO"}


# ─── Date helpers ──────────────────────────────────────────────────────────────

def iter_dates(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


# ─── Week-off calculator ───────────────────────────────────────────────────────

def _compute_week_offs(
    start: date,
    end: date,
    week_off_rule: str,
    peer_index: int = 0,
) -> set[date]:
    """
    Generate week-off dates.

    Rules:
        "weekends"          -> Sat & Sun
        "rolling6th_7th"    -> every 6th and 7th day (rolling, staggered)
        "every5th_6th"      -> every 5th AND 6th calendar day
        "every<N>th"        -> every Nth calendar day
        "custom:<raw>"      -> weekends fallback
    """
    all_dates = list(iter_dates(start, end))

    if week_off_rule == "weekends" or week_off_rule.startswith("custom:"):
        return {d for d in all_dates if is_weekend(d)}

    if "rolling" in week_off_rule:
        ns = [int(x) for x in re.findall(r"\d+", week_off_rule)]
        if not ns:
            ns = [6, 7]
        cycle = max(ns)
        offs: set[date] = set()
        count = peer_index
        for d in all_dates:
            count += 1
            if count % cycle in {n % cycle for n in ns}:
                offs.add(d)
        return offs

    ns = [int(x) for x in re.findall(r"\d+", week_off_rule)]
    if not ns:
        return {d for d in all_dates if is_weekend(d)}

    offs = set()
    for n in ns:
        count = peer_index
        for d in all_dates:
            count += 1
            if count % n == 0:
                offs.add(d)
    return offs


# ─── Rotation engine ───────────────────────────────────────────────────────────

def _build_rotation_schedule(
    shifts_per_slot: list[str],
    rotation_weeks: int,
    start: date,
    end: date,
    slot_index: int,
) -> dict[date, str]:
    """
    Build a {date: shift} map using a rolling rotation.

    For a 2-week rotation with 6 employees [M,M,A,A,N,N]:
      - Employee 0 starts on M for 2 weeks, then rotates to A, then N
      - Employee 1 starts on M but offset by 2 weeks from employee 0

    rotation_weeks = 0 means static (no rotation).
    G and E shifts always use rotation_weeks = 0.
    """
    n_slots = len(shifts_per_slot)
    all_dates = list(iter_dates(start, end))
    daily: dict[date, str] = {}

    if rotation_weeks == 0 or n_slots == 0:
        shift = shifts_per_slot[slot_index % n_slots] if shifts_per_slot else "M"
        for d in all_dates:
            daily[d] = shift
        return daily

    rotation_days = rotation_weeks * 7

    for d in all_dates:
        day_num   = (d - start).days
        cycle_pos = (day_num // rotation_days + slot_index) % n_slots
        daily[d]  = shifts_per_slot[cycle_pos]

    return daily


# ─── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EmployeeSchedule:
    employee:  Employee
    daily:     dict[date, str]
    week_offs: set[date]
    leaves:    set[date]
    comp_offs: set[date]

    def shift_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for code in self.daily.values():
            counts[code] = counts.get(code, 0) + 1
        return counts

    def working_days(self) -> int:
        return sum(1 for c in self.daily.values() if c not in NON_WORKING)


# ─── Scheduler ─────────────────────────────────────────────────────────────────

class ShiftScheduler:
    """
    Assigns shifts to all employees.

    Skill rule dict format:
    {
        "count": 6,
        "shifts": ["M", "A", "N"],      # shift codes
        "allocation": [2, 2, 2],        # employees per shift slot
        "rotation_weeks": 2,            # 0 = static, 1/2/3/4 = N-week rotation
        "week_off": "rolling6th_7th",   # or "weekends", "every5th_6th", etc.
    }

    Special shifts:
        shifts = ["G"]  -> General shift, always static (rotation_weeks forced to 0)
        shifts = ["E"]  -> Evening shift, always static
    """

    def __init__(self, cfg: AppConfig, employees: list[Employee]) -> None:
        self.cfg = cfg
        self.employees = employees
        self._all_dates = list(iter_dates(cfg.roster_start, cfg.roster_end))

        # Pre-group employees by skill for positional slot assignment
        self._skill_groups: dict[str, list[Employee]] = {}
        for emp in employees:
            self._skill_groups.setdefault(emp.skill, []).append(emp)

    def run(self) -> list[EmployeeSchedule]:
        return [self._schedule_employee(emp) for emp in self.employees]

    def _slot_index(self, emp: Employee) -> int:
        """0-based position of this employee within their skill group."""
        group = self._skill_groups.get(emp.skill, [])
        for i, e in enumerate(group):
            if e.name == emp.name:
                return i
        return 0

    def _expand_slots(self, rule: dict) -> list[str]:
        """
        Build a flat list of shift codes from allocation.

        Special cases:
          shifts=["G"]            -> ["G"]   (General — static, never expand)
          shifts=["E"]            -> ["E"]   (Evening — static)
          shifts=["M","A","N"],
          allocation=[2,2,2]      -> ["M","M","A","A","N","N"]
        """
        shifts     = rule.get("shifts", ["M", "A", "N"])
        allocation = rule.get("allocation", [])

        # G and E are always single-code static — never expand into M/A/N
        if shifts == ["G"] or shifts == ["E"]:
            return shifts

        if not allocation:
            return shifts

        slots: list[str] = []
        for shift, count in zip(shifts, allocation):
            slots.extend([shift] * count)
        return slots

    def _week_offs_for(self, emp: Employee, peer_index: int) -> set[date]:
        rule    = self.cfg.skill_rules.get(emp.skill, {})
        wo_rule = rule.get("week_off", "weekends")
        return _compute_week_offs(
            self.cfg.roster_start,
            self.cfg.roster_end,
            wo_rule,
            peer_index=peer_index,
        )

    def _schedule_employee(self, emp: Employee) -> EmployeeSchedule:
        cfg      = self.cfg
        rule     = cfg.skill_rules.get(emp.skill, {})
        slot_idx = self._slot_index(emp)

        slots          = self._expand_slots(rule)
        rotation_weeks = rule.get("rotation_weeks", 0)

        rotation_daily = _build_rotation_schedule(
            shifts_per_slot=slots,
            rotation_weeks=rotation_weeks,
            start=cfg.roster_start,
            end=cfg.roster_end,
            slot_index=slot_idx,
        )

        # Parse leaves and comp-offs
        leaves:    set[date] = set()
        comp_offs: set[date] = set()

        for leave_entry in cfg.planned_leaves.get(emp.name, []):
            if isinstance(leave_entry, dict):
                if leave_entry.get("type") == "CO":
                    comp_offs.add(leave_entry["date"])
                else:
                    leaves.add(leave_entry["date"])
            else:
                # Legacy plain date → treat as PL
                leaves.add(leave_entry)

        week_offs = self._week_offs_for(emp, slot_idx)

        # Priority: PL > CO > H > W > rotation shift
        daily: dict[date, str] = {}
        for d in self._all_dates:
            if d in leaves:
                daily[d] = "PL"
            elif d in comp_offs:
                daily[d] = "CO"
            elif d in cfg.account_holidays:
                daily[d] = "H"
            elif d in week_offs:
                daily[d] = "W"
            else:
                daily[d] = rotation_daily.get(d, "M")

        return EmployeeSchedule(
            employee=emp,
            daily=daily,
            week_offs=week_offs,
            leaves=leaves,
            comp_offs=comp_offs,
        )
