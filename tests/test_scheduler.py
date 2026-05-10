"""
tests/test_scheduler.py
-----------------------
Unit tests for the shift-assignment engine.
Run with: pytest
"""

from __future__ import annotations

from datetime import date

import pytest

from roster.config import AppConfig, DEFAULT_SKILL_RULES, DEFAULT_SKILL_ALIAS
from roster.loader import Employee
from roster.scheduler import ShiftScheduler, iter_dates, is_weekend


# ─── Fixtures ──────────────────────────────────────────────────────────────────

def _make_cfg(**overrides) -> AppConfig:
    defaults = dict(
        input_file=None,
        output_file=None,
        roster_start=date(2026, 6, 1),
        roster_end=date(2026, 6, 30),
        account_holidays={date(2026, 6, 5), date(2026, 6, 9), date(2026, 6, 16)},
        planned_leaves={},
        skill_rules=DEFAULT_SKILL_RULES,
        skill_alias=DEFAULT_SKILL_ALIAS,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _emp(name, skill) -> Employee:
    return Employee(name=name, email=f"{name}@test.com", skill=skill, location="Test")


# ─── Tests ─────────────────────────────────────────────────────────────────────

class TestIterDates:
    def test_count(self):
        dates = list(iter_dates(date(2026, 6, 1), date(2026, 6, 30)))
        assert len(dates) == 30

    def test_start_end(self):
        dates = list(iter_dates(date(2026, 6, 1), date(2026, 6, 5)))
        assert dates[0] == date(2026, 6, 1)
        assert dates[-1] == date(2026, 6, 5)


class TestIsWeekend:
    def test_saturday(self):
        assert is_weekend(date(2026, 6, 6))   # Saturday

    def test_sunday(self):
        assert is_weekend(date(2026, 6, 7))   # Sunday

    def test_monday(self):
        assert not is_weekend(date(2026, 6, 1))  # Monday


class TestHolidayAssignment:
    def test_holiday_days_marked(self):
        cfg = _make_cfg()
        emp = _emp("Alice", "Monitoring")
        [sched] = ShiftScheduler(cfg, [emp]).run()

        for hol in cfg.account_holidays:
            assert sched.daily[hol] == "H", f"Expected H on holiday {hol}"

    def test_no_shift_on_holiday(self):
        cfg = _make_cfg()
        emp = _emp("Bob", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        assert sched.daily[date(2026, 6, 5)] == "H"


class TestLeaveAssignment:
    def test_leave_days_marked(self):
        leave_dates = [date(2026, 6, 10), date(2026, 6, 11)]
        cfg = _make_cfg(planned_leaves={"Guru": leave_dates})
        emp = _emp("Guru", "Monitoring")
        [sched] = ShiftScheduler(cfg, [emp]).run()

        for ld in leave_dates:
            assert sched.daily[ld] == "L"

    def test_leave_priority_over_weekend(self):
        """Leave should take priority over a week-off that falls on a weekend."""
        # June 6 2026 is a Saturday
        cfg = _make_cfg(planned_leaves={"Sam": [date(2026, 6, 6)]})
        emp = _emp("Sam", "Azure + Windows")   # weekend week-off rule
        [sched] = ShiftScheduler(cfg, [emp]).run()
        assert sched.daily[date(2026, 6, 6)] == "L"


class TestWeekOffRules:
    def test_weekends_off(self):
        cfg = _make_cfg()
        emp = _emp("Carol", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        saturdays = [d for d in iter_dates(cfg.roster_start, cfg.roster_end) if d.weekday() == 5]
        for sat in saturdays:
            if sat not in cfg.account_holidays and sat not in cfg.planned_leaves.get("Carol", []):
                assert sched.daily[sat] == "W", f"Expected W on Saturday {sat}"

    def test_every5th_stagger(self):
        """Two employees with every5th should not share the same week-off dates."""
        cfg = _make_cfg()
        emp1 = _emp("E1", "Monitoring")
        emp2 = _emp("E2", "Monitoring")
        s1, s2 = ShiftScheduler(cfg, [emp1, emp2]).run()
        assert s1.week_offs != s2.week_offs


class TestShiftRotation:
    def test_only_allowed_shifts_assigned(self):
        cfg = _make_cfg()
        emp = _emp("Dan", "SRE Azure + Windows")   # E only
        [sched] = ShiftScheduler(cfg, [emp]).run()
        working = {code for d, code in sched.daily.items()
                   if code not in ("H", "L", "W")}
        assert working == {"E"}, f"Unexpected shifts: {working}"

    def test_man_rotation_uses_all_three(self):
        cfg = _make_cfg()
        emp = _emp("Eve", "Monitoring")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        working = {code for d, code in sched.daily.items()
                   if code not in ("H", "L", "W")}
        assert "M" in working
        assert "A" in working
        assert "N" in working


class TestWorkingDays:
    def test_working_days_plus_offs_equals_period(self):
        cfg = _make_cfg()
        emp = _emp("Fred", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        total = len(list(iter_dates(cfg.roster_start, cfg.roster_end)))
        counts = sched.shift_counts()
        day_sum = sum(counts.get(k, 0) for k in ("M", "A", "N", "E", "H", "L", "W"))
        assert day_sum == total
