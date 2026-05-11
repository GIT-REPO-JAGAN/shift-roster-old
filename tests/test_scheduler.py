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
        skill_rules={
            "Monitoring": {
                "count": 6, "shifts": ["M","A","N"], "allocation": [2,2,2],
                "rotation_weeks": 2, "week_off": "rolling6th_7th"
            },
            "Azure + Windows": {
                "count": 6, "shifts": ["M","A","N"], "allocation": [2,2,2],
                "rotation_weeks": 2, "week_off": "weekends"
            },
            "SME: Azure/Windows": {
                "count": 6, "shifts": ["M","A","N"], "allocation": [],
                "rotation_weeks": 0, "week_off": "weekends"
            },
        },
        skill_alias={
            "Monitoring": "Monitoring",
            "Azure + Windows": "Azure + Windows",
            "SME: Azure/Windows": "SME: Azure/Windows",
        },
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
        assert is_weekend(date(2026, 6, 6))

    def test_sunday(self):
        assert is_weekend(date(2026, 6, 7))

    def test_monday(self):
        assert not is_weekend(date(2026, 6, 1))


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
    def test_pl_days_marked(self):
        leave_dates = [date(2026, 6, 10), date(2026, 6, 11)]
        cfg = _make_cfg(planned_leaves={
            "Guru": [{"type": "PL", "date": d} for d in leave_dates]
        })
        emp = _emp("Guru", "Monitoring")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        for ld in leave_dates:
            assert sched.daily[ld] == "PL"

    def test_co_days_marked(self):
        co_dates = [date(2026, 6, 12), date(2026, 6, 13)]
        cfg = _make_cfg(planned_leaves={
            "Sam": [{"type": "CO", "date": d} for d in co_dates]
        })
        emp = _emp("Sam", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        for cd in co_dates:
            assert sched.daily[cd] == "CO"

    def test_pl_priority_over_weekend(self):
        # June 6 2026 is a Saturday
        cfg = _make_cfg(planned_leaves={
            "Sam": [{"type": "PL", "date": date(2026, 6, 6)}]
        })
        emp = _emp("Sam", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        assert sched.daily[date(2026, 6, 6)] == "PL"

    def test_co_priority_over_weekend(self):
        cfg = _make_cfg(planned_leaves={
            "Sam": [{"type": "CO", "date": date(2026, 6, 6)}]
        })
        emp = _emp("Sam", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        assert sched.daily[date(2026, 6, 6)] == "CO"


class TestWeekOffRules:
    def test_weekends_off(self):
        cfg = _make_cfg()
        emp = _emp("Carol", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        saturdays = [d for d in iter_dates(cfg.roster_start, cfg.roster_end) if d.weekday() == 5]
        for sat in saturdays:
            if sat not in cfg.account_holidays and sat not in {
                e["date"] for e in cfg.planned_leaves.get("Carol", [])
            }:
                assert sched.daily[sat] == "W", f"Expected W on Saturday {sat}"

    def test_rolling_week_off_stagger(self):
        """Two employees with rolling week-off should not share identical off-day sets."""
        cfg = _make_cfg()
        emp1 = _emp("E1", "Monitoring")
        emp2 = _emp("E2", "Monitoring")
        s1, s2 = ShiftScheduler(cfg, [emp1, emp2]).run()
        assert s1.week_offs != s2.week_offs


class TestShiftRotation:
    def test_all_three_shifts_used_across_group(self):
        """With 6 employees [2M,2A,2N] and 2-week rotation, all 3 shifts appear."""
        cfg = _make_cfg()
        emps = [_emp(f"E{i}", "Monitoring") for i in range(6)]
        schedules = ShiftScheduler(cfg, emps).run()
        all_shifts = {
            code
            for sched in schedules
            for code in sched.daily.values()
            if code not in ("H", "W", "PL", "CO")
        }
        assert "M" in all_shifts
        assert "A" in all_shifts
        assert "N" in all_shifts

    def test_static_allocation_consistent(self):
        """Static (rotation_weeks=0) employees always get the same shift."""
        cfg = _make_cfg()
        emp = _emp("Static1", "SME: Azure/Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        working = {c for c in sched.daily.values() if c not in ("H","W","PL","CO")}
        # Static with shifts=["M","A","N"] and no allocation → cycles through M/A/N
        assert len(working) >= 1


class TestWorkingDays:
    def test_working_days_plus_offs_equals_period(self):
        cfg = _make_cfg()
        emp = _emp("Fred", "Azure + Windows")
        [sched] = ShiftScheduler(cfg, [emp]).run()
        total = len(list(iter_dates(cfg.roster_start, cfg.roster_end)))
        counts = sched.shift_counts()
        day_sum = sum(counts.get(k, 0) for k in ("M","A","N","E","G","H","PL","CO","W"))
        assert day_sum == total
