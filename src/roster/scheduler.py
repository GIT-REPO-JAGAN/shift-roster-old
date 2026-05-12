"""
scheduler.py
------------
Core shift-assignment engine.

Rotation options:
    Every Week     → rotate every 7 days
    Every 2-Weeks  → rotate every 14 days
    Every 3-Weeks  → rotate every 21 days
    Every Month    → rotate once per calendar month
    NA / Static    → no rotation

Week-off rules:
    weekends           → Sat & Sun
    rolling7th         → every 7th day rolling
    rolling6th7th      → every 6th and 7th day rolling
    NA (G/static)      → no week-off from rotation (General uses Sat & Sun by default)

Day codes:
    M  Morning   A  Afternoon   N  Night   E  Evening   G  General
    PL Planned Leave   CO Comp-Off   ADHOC Adhoc Shift
    H  Holiday   W  Week Off
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

from .config import AppConfig
from .loader import Employee


# ─── Shift labels ──────────────────────────────────────────────────────────────

SHIFT_LABELS = {
    "M":     "Morning (06:00–14:00)",
    "A":     "Afternoon (14:00–22:00)",
    "N":     "Night (22:00–06:00)",
    "E":     "Evening (14:00–22:00)",
    "G":     "General (static shift)",
    "PL":    "Planned Leave",
    "CO":    "Comp-Off",
    "ADHOC": "Adhoc / On-call Shift",
    "H":     "Holiday",
    "W":     "Week Off",
}

NON_WORKING = {"H", "W", "PL", "CO"}


# ─── Date helpers ──────────────────────────────────────────────────────────────

def iter_dates(start: date, end: date) -> Iterator[date]:
    """Yield every date from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5   # Sat=5, Sun=6


# ─── Week-off calculator ───────────────────────────────────────────────────────

def _compute_week_offs(
    start: date,
    end: date,
    week_off_rule: str,
    peer_index: int = 0,
) -> set[date]:
    """
    Returns the set of week-off dates for an employee.

    Rule strings:
        "weekends"          → all Saturdays & Sundays
        "rolling7th"        → every 7th calendar day (staggered by peer_index)
        "rolling6th7th"     → every 6th AND 7th calendar day
        "every5th_6th"      → every 5th AND 6th calendar day
        "every<N>th"        → every Nth calendar day
        "custom:<raw>"      → weekends fallback
        "na" / ""           → no week-off (used for General/static skills)
    """
    all_dates = list(iter_dates(start, end))
    rule = week_off_rule.lower().strip()

    if rule in ("na", "", "none"):
        return set()

    if rule == "weekends" or rule.startswith("custom:"):
        return {d for d in all_dates if is_weekend(d)}

    if "rolling" in rule:
        ns = [int(x) for x in re.findall(r"\d+", rule)]
        if not ns:
            ns = [6, 7]
        cycle = max(ns)
        offs: set[date] = set()
        count = peer_index
        for d in all_dates:
            count += 1
            if any(count % cycle == n % cycle for n in ns):
                offs.add(d)
        return offs

    # Numeric cycle: every5th, every5th_6th, every4th_6th …
    ns = [int(x) for x in re.findall(r"\d+", rule)]
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
    slots: list[str],
    rotation_type: str,
    start: date,
    end: date,
    slot_index: int,
) -> dict[date, str]:
    """
    Build {date: shift_code} using the specified rotation type.

    rotation_type values:
        "7"   → Every Week   (7-day cycle)
        "14"  → Every 2-Weeks (14-day cycle)
        "21"  → Every 3-Weeks (21-day cycle)
        "monthly" → Every Month (full month stays on same slot, rotates on 1st)
        "0"   → Static / NA (no rotation — slot_index picks the permanent shift)
    """
    all_dates = list(iter_dates(start, end))
    daily: dict[date, str] = {}
    n = len(slots)

    if n == 0:
        for d in all_dates:
            daily[d] = "M"
        return daily

    if rotation_type == "0" or rotation_type == "na":
        shift = slots[slot_index % n]
        for d in all_dates:
            daily[d] = shift
        return daily

    if rotation_type == "monthly":
        # Track which calendar month we're in — rotate slot on month boundary
        # Employee 0 starts at slot 0, employee 1 at slot 1, etc.
        months_seen: dict[tuple, int] = {}
        for d in all_dates:
            key = (d.year, d.month)
            if key not in months_seen:
                months_seen[key] = len(months_seen)
            cycle_pos = (months_seen[key] + slot_index) % n
            daily[d] = slots[cycle_pos]
        return daily

    # Day-based rotation (7, 14, 21)
    rotation_days = int(rotation_type)
    for d in all_dates:
        day_num   = (d - start).days
        cycle_pos = (day_num // rotation_days + slot_index) % n
        daily[d]  = slots[cycle_pos]

    return daily


# ─── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EmployeeSchedule:
    employee:  Employee
    daily:     dict[date, str]
    week_offs: set[date]
    leaves:    set[date]
    comp_offs: set[date]
    adhoc:     dict[date, str] = field(default_factory=dict)

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
    Assigns shifts to all employees for the full roster period.

    Skill rule dict format:
    {
        "count":         6,
        "shifts":        ["M", "A", "N"],
        "allocation":    [2, 2, 2],
        "rotation_type": "14",          # "7"|"14"|"21"|"monthly"|"0"
        "week_off":      "rolling6th7th",
    }
    """

    def __init__(self, cfg: AppConfig, employees: list[Employee]) -> None:
        self.cfg        = cfg
        self.employees  = employees
        self._all_dates = list(iter_dates(cfg.roster_start, cfg.roster_end))

        # Pre-group employees by skill for positional slot assignment
        self._skill_groups: dict[str, list[Employee]] = {}
        for emp in employees:
            self._skill_groups.setdefault(emp.skill, []).append(emp)

    def run(self) -> list[EmployeeSchedule]:
        return [self._schedule_employee(emp) for emp in self.employees]

    def _slot_index(self, emp: Employee) -> int:
        group = self._skill_groups.get(emp.skill, [])
        for i, e in enumerate(group):
            if e.name == emp.name:
                return i
        return 0

    def _expand_slots(self, rule: dict) -> list[str]:
        """
        Build a flat slot list from shifts + allocation.
        G and E are always static single-code slots.
        """
        shifts     = rule.get("shifts", ["M", "A", "N"])
        allocation = rule.get("allocation", [])

        if shifts == ["G"] or shifts == ["E"]:
            return shifts

        if not allocation:
            return shifts

        slots: list[str] = []
        for shift, cnt in zip(shifts, allocation):
            slots.extend([shift] * cnt)
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

        slots         = self._expand_slots(rule)
        rotation_type = str(rule.get("rotation_type", "0"))

        rotation_daily = _build_rotation_schedule(
            slots=slots,
            rotation_type=rotation_type,
            start=cfg.roster_start,
            end=cfg.roster_end,
            slot_index=slot_idx,
        )

        # Parse leaves, comp-offs, adhoc overrides
        leaves:    set[date]       = set()
        comp_offs: set[date]       = set()
        adhoc:     dict[date, str] = {}

        for entry in cfg.planned_leaves.get(emp.name, []):
            if isinstance(entry, dict):
                t = entry.get("type", "PL")
                d = entry.get("date")
                if t == "CO":
                    comp_offs.add(d)
                elif t == "ADHOC":
                    adhoc[d] = entry.get("shift", "G")
                else:
                    leaves.add(d)
            else:
                leaves.add(entry)

        week_offs = self._week_offs_for(emp, slot_idx)

        # Priority: PL > CO > ADHOC > H > W > rotation
        daily: dict[date, str] = {}
        for d in self._all_dates:
            if d in leaves:
                daily[d] = "PL"
            elif d in comp_offs:
                daily[d] = "CO"
            elif d in adhoc:
                daily[d] = adhoc[d]      # e.g. "G", "M", "A", "N"
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
            adhoc=adhoc,
        )
