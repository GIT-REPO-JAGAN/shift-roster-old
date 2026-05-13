"""
scheduler.py
------------
Coverage-aware shift assignment engine.

Key enhancements:
  - Minimum Coverage Guarantee (MCG): ensures at least one employee per
    configured shift type every working day, even when rolling week-offs
    would otherwise leave a shift uncovered.
  - Intelligent slot rebalancing when employee count ≠ configured slots.
  - ALWAYS override: full roster period reassignment for an employee.
  - E1 shift code support.
  - Multi-month roster support.

Priority order (highest wins):
    PL > CO > ADHOC > H > W(*) > rotation
    (*) W is demoted if MCG would leave a shift type uncovered.

Rotation options:
    Every Week     → rotate every 7 days
    Every 2-Weeks  → rotate every 14 days
    Every 3-Weeks  → rotate every 21 days
    Every Month    → rotate once per calendar month
    NA / Static    → no rotation

Week-off rules:
    weekends        → Sat & Sun
    rolling7th      → every 7th calendar day (staggered by peer_index)
    rolling6th7th   → every 6th AND 7th calendar day
    na / ""         → no week-off

Shift codes:
    M  Morning       A  Afternoon    N  Night
    E  Evening       E1 Evening-1    G  General
    PL Planned Leave CO Comp-Off     ADHOC Adhoc
    H  Holiday       W  Week Off
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

from .config import AppConfig
from .loader import Employee


# ─── Constants ────────────────────────────────────────────────────────────────

SHIFT_LABELS: dict[str, str] = {
    "M":     "Morning (05:30–14:30)",
    "A":     "Afternoon (13:30–22:30)",
    "N":     "Night (21:30–06:30)",
    "E":     "Evening (17:30–02:30)",
    "E1":    "Evening-1 (19:30–04:30)",
    "G":     "General (09:30–18:30)",
    "PL":    "Planned Leave",
    "CO":    "Comp-Off",
    "ADHOC": "Adhoc / On-call Shift",
    "H":     "Holiday",
    "W":     "Week Off",
}

# Codes that count as non-working days
NON_WORKING: frozenset[str] = frozenset({"H", "W", "PL", "CO"})

# Valid shift codes for rotation
WORK_SHIFTS: frozenset[str] = frozenset({"M", "A", "N", "E", "E1", "G"})


# ─── Date helpers ─────────────────────────────────────────────────────────────

def iter_dates(start: date, end: date) -> Iterator[date]:
    """Yield every calendar date from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


# ─── Slot rebalancing ─────────────────────────────────────────────────────────

def _rebalance_slots(slots: list[str], emp_count: int) -> list[str]:
    """
    Redistribute a slot list to fit the actual employee count while
    preserving the relative proportions of each shift type.

    Examples:
        [M,M,A,A,N,N] + 5 emp  → [M,M,A,N,N]
        [M,M,A,A,N,N] + 4 emp  → [M,A,N,N]
        [M,M,A,A]     + 3 emp  → [M,A,A]
        [M,A,N]       + 3 emp  → [M,A,N]  (unchanged)
    """
    if not slots or emp_count <= 0:
        return slots
    if emp_count == len(slots):
        return slots

    # Tally shift types and their weights
    counts: dict[str, int] = {}
    for s in slots:
        counts[s] = counts.get(s, 0) + 1
    types = list(counts.keys())
    total = len(slots)

    if len(types) == 1:
        return [types[0]] * emp_count

    # Proportional allocation with minimum 1 per type
    allocated: dict[str, int] = {
        t: max(1, round(counts[t] / total * emp_count))
        for t in types
    }

    # Correct rounding errors to hit exactly emp_count
    current = sum(allocated.values())
    while current > emp_count:
        t = max((t for t in types if allocated[t] > 1),
                key=lambda t: allocated[t], default=None)
        if t is None:
            break
        allocated[t] -= 1
        current -= 1

    while current < emp_count:
        t = max(types, key=lambda t: counts[t] / total - allocated[t] / emp_count)
        allocated[t] += 1
        current += 1

    result: list[str] = []
    for t in types:
        result.extend([t] * allocated[t])
    return result


# ─── Week-off calculator ──────────────────────────────────────────────────────

def _compute_week_offs(
    start: date,
    end: date,
    week_off_rule: str,
    peer_index: int = 0,
) -> set[date]:
    """
    Compute the set of week-off dates for an employee.

    Rules:
        "weekends"      → all Sat & Sun
        "rolling7th"    → every 7th day (staggered by peer_index)
        "rolling6th7th" → every 6th AND 7th day
        "everyNth"      → every Nth calendar day
        "na" / ""       → no week-off
    """
    all_dates = list(iter_dates(start, end))
    rule = week_off_rule.lower().strip()

    if rule in ("na", "", "none"):
        return set()

    if rule in ("weekends", "weekend") or rule.startswith("custom:"):
        return {d for d in all_dates if is_weekend(d)}

    if "rolling" in rule:
        ns = [int(x) for x in re.findall(r"\d+", rule)] or [6, 7]
        cycle = max(ns)
        offs: set[date] = set()
        count = peer_index
        for d in all_dates:
            count += 1
            if any(count % cycle == n % cycle for n in ns):
                offs.add(d)
        return offs

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


# ─── Rotation engine ──────────────────────────────────────────────────────────

def _build_rotation(
    slots: list[str],
    rotation_type: str,
    start: date,
    end: date,
    slot_index: int,
) -> dict[date, str]:
    """
    Build {date → shift_code} for one employee.

    rotation_type:
        "7"       → Every Week
        "14"      → Every 2-Weeks
        "21"      → Every 3-Weeks
        "monthly" → Every Month
        "0"/"na"  → Static (no rotation)
    """
    all_dates = list(iter_dates(start, end))
    n = len(slots)
    daily: dict[date, str] = {}

    if n == 0:
        for d in all_dates:
            daily[d] = "M"
        return daily

    if rotation_type in ("0", "na"):
        shift = slots[slot_index % n]
        for d in all_dates:
            daily[d] = shift
        return daily

    if rotation_type == "monthly":
        months_seen: dict[tuple, int] = {}
        for d in all_dates:
            key = (d.year, d.month)
            if key not in months_seen:
                months_seen[key] = len(months_seen)
            daily[d] = slots[(months_seen[key] + slot_index) % n]
        return daily

    rotation_days = int(rotation_type)
    for d in all_dates:
        day_num = (d - start).days
        daily[d] = slots[(day_num // rotation_days + slot_index) % n]

    return daily


# ─── Coverage-aware MCG ───────────────────────────────────────────────────────

def _apply_mcg(
    group_schedules: list,
    required_shifts: list[str],
    all_dates: list[date],
    base_daily_per_emp: list[dict[date, str]],
    locked_per_emp: list[set[date]],
    wo_rule: str = "",
) -> list[dict[date, str]]:
    """
    Minimum Coverage Guarantee (MCG).

    For each calendar day:
      1. Find which required shifts are UNCOVERED (no working employee assigned).
      2. For each uncovered shift, find an employee whose shift has SURPLUS
         coverage on that day and reassign them.
      3. Only W (week-off) can be overridden — PL/CO/H/ADHOC are never touched.

    Returns modified daily schedules for each employee.
    """
    n_emp = len(base_daily_per_emp)
    daily = [dict(d) for d in base_daily_per_emp]  # deep copy

    for d in all_dates:
        # Skip MCG on actual weekends when rule is "weekends" (not rolling)
        # — we never force overtime on Sat/Sun for weekends-rule groups
        if is_weekend(d) and "rolling" not in wo_rule.lower() and wo_rule.lower() not in ("na","","none"):
            continue

        # Count current coverage per shift type
        coverage: dict[str, list[int]] = defaultdict(list)
        for i in range(n_emp):
            code = daily[i].get(d, "")
            if code in WORK_SHIFTS and code != "G":
                coverage[code].append(i)

        # Find uncovered required shifts
        for missing_shift in required_shifts:
            if missing_shift == "G":
                continue  # G employees never reassigned
            if coverage.get(missing_shift):
                continue  # already covered

            # Find a donor: only borrow from an employee on W whose
            # original shift type STILL has at least 1 other person covering it.
            # This preserves week-offs whenever possible.
            donor_idx = None

            # Pass 1: employee on W whose shift type remains covered
            for i in range(n_emp):
                code = daily[i].get(d, "")
                if code != "W":
                    continue
                if d in locked_per_emp[i]:
                    continue  # locked (PL/CO/H/ADHOC) — never override
                # Check that their rotation shift (not W) is still covered
                # i.e., their W override isn't the only reason their shift type is covered
                # We find what shift they WOULD have had (from their base rotation)
                # For simplicity: only borrow if there's surplus in any other shift
                has_surplus = any(
                    len(coverage.get(s, [])) > 1
                    for s in required_shifts
                    if s != missing_shift
                )
                if has_surplus or len(coverage.get(missing_shift, [])) == 0:
                    donor_idx = i
                    break

            # Pass 2: employee with a working shift that has surplus coverage
            if donor_idx is None:
                for i in range(n_emp):
                    code = daily[i].get(d, "")
                    if code not in WORK_SHIFTS or code == "G":
                        continue
                    if d in locked_per_emp[i]:
                        continue
                    if len(coverage.get(code, [])) > 1:
                        donor_idx = i
                        break

            if donor_idx is not None:
                old_code = daily[donor_idx].get(d, "")
                daily[donor_idx][d] = missing_shift
                # Update coverage map
                coverage[missing_shift].append(donor_idx)
                if old_code in coverage:
                    coverage[old_code] = [x for x in coverage[old_code] if x != donor_idx]

    return daily


# ─── Result dataclass ────────────────────────────────────────────────────────

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


# ─── Scheduler ───────────────────────────────────────────────────────────────

class ShiftScheduler:
    """
    Coverage-aware shift scheduler.

    Steps per skill group:
      1. Rebalance slot list to match employee count.
      2. Build base rotation schedule for every employee.
      3. Apply locked overrides (PL, CO, H, ADHOC, ALWAYS).
      4. Apply week-offs.
      5. Run MCG to fill any uncovered shifts.
    """

    def __init__(self, cfg: AppConfig, employees: list[Employee]) -> None:
        self.cfg = cfg
        self.employees = employees
        self._all_dates = list(iter_dates(cfg.roster_start, cfg.roster_end))

        # Group employees by resolved skill name
        self._skill_groups: dict[str, list[Employee]] = {}
        for emp in employees:
            self._skill_groups.setdefault(emp.skill, []).append(emp)

    def run(self) -> list[EmployeeSchedule]:
        # Process skill by skill so MCG can work across the whole group
        result: list[EmployeeSchedule] = []
        processed: dict[str, list[EmployeeSchedule]] = {}

        for skill, group in self._skill_groups.items():
            schedules = self._schedule_group(skill, group)
            processed[skill] = schedules

        # Flatten in original employee order
        emp_order = {emp.name: i for i, emp in enumerate(self.employees)}
        all_scheds = [s for scheds in processed.values() for s in scheds]
        all_scheds.sort(key=lambda s: emp_order.get(s.employee.name, 999))
        return all_scheds

    # ── Group-level scheduling ────────────────────────────────────────────────

    def _schedule_group(
        self, skill: str, group: list[Employee]
    ) -> list[EmployeeSchedule]:
        cfg = self.cfg
        rule = cfg.skill_rules.get(skill, {})
        is_general = rule.get("isGeneral", False) or rule.get("shifts", []) == ["G"]

        # 1. Build slot list, rebalanced to group size
        raw_slots = self._expand_slots(rule)
        eff_slots = _rebalance_slots(raw_slots, len(group))
        required_shifts = list(dict.fromkeys(eff_slots))  # unique, ordered
        rotation_type = str(rule.get("rotation_type", "0"))
        wo_rule = rule.get("week_off", "weekends")

        # 2. Per-employee: parse overrides and build base rotation
        base_dailys: list[dict[date, str]] = []
        week_offs_list: list[set[date]] = []
        leaves_list: list[set[date]] = []
        comp_offs_list: list[set[date]] = []
        adhoc_list: list[dict[date, str]] = []
        locked_list: list[set[date]] = []  # dates that cannot be overridden by MCG
        always_list: list[dict | None] = []

        for peer_idx, emp in enumerate(group):
            leaves, comp_offs, adhoc, always_entry = self._parse_overrides(emp)
            week_offs = _compute_week_offs(
                cfg.roster_start, cfg.roster_end, wo_rule, peer_idx
            )

            # ALWAYS override: reassign entire roster period
            if always_entry:
                always_slots = [always_entry["shift"]]
                always_wo_raw = always_entry.get("weekOff", "")
                if always_wo_raw and always_wo_raw.strip().lower() not in ("na", ""):
                    from roster.web.app import _normalise_weekoff  # type: ignore
                    try:
                        week_offs = _compute_week_offs(
                            cfg.roster_start, cfg.roster_end,
                            _normalise_weekoff(always_wo_raw), peer_idx
                        )
                    except Exception:
                        pass
                base = _build_rotation(
                    always_slots, "0", cfg.roster_start, cfg.roster_end, 0
                )
            else:
                base = _build_rotation(
                    eff_slots, rotation_type, cfg.roster_start, cfg.roster_end, peer_idx
                )

            # Build locked set: days where MCG must NOT override
            locked: set[date] = set()
            for d in self._all_dates:
                if (d in leaves or d in comp_offs or d in adhoc
                        or d in cfg.account_holidays):
                    locked.add(d)

            base_dailys.append(base)
            week_offs_list.append(week_offs)
            leaves_list.append(leaves)
            comp_offs_list.append(comp_offs)
            adhoc_list.append(adhoc)
            locked_list.append(locked)
            always_list.append(always_entry)

        # 3. Apply all overrides to get pre-MCG schedule
        pre_mcg: list[dict[date, str]] = []
        for i, emp in enumerate(group):
            daily: dict[date, str] = {}
            base = base_dailys[i]
            leaves = leaves_list[i]
            comp_offs = comp_offs_list[i]
            adhoc = adhoc_list[i]
            week_offs = week_offs_list[i]
            emp_holidays = cfg.account_holidays

            # Location-aware holidays
            loc_hol_map = getattr(cfg, "location_holidays", {})
            if loc_hol_map and emp.location:
                from .scheduler import _merge_holidays
                emp_holidays = _merge_holidays(emp_holidays, loc_hol_map, emp.location)

            for d in self._all_dates:
                if d in leaves:
                    daily[d] = "PL"
                elif d in comp_offs:
                    daily[d] = "CO"
                elif d in adhoc:
                    daily[d] = adhoc[d]
                elif d in emp_holidays:
                    daily[d] = "H"
                elif d in week_offs:
                    daily[d] = "W"
                else:
                    daily[d] = base.get(d, eff_slots[i % len(eff_slots)] if eff_slots else "M")

            pre_mcg.append(daily)

        # 4. MCG: ensure minimum coverage per shift type (skip for G/static groups)
        if not is_general and len(required_shifts) > 0 and "G" not in required_shifts:
            post_mcg = _apply_mcg(
                group_schedules=[],
                required_shifts=required_shifts,
                all_dates=self._all_dates,
                base_daily_per_emp=pre_mcg,
                locked_per_emp=locked_list,
                wo_rule=wo_rule,
            )
        else:
            post_mcg = pre_mcg

        # 5. Build result objects
        results: list[EmployeeSchedule] = []
        for i, emp in enumerate(group):
            results.append(EmployeeSchedule(
                employee=emp,
                daily=post_mcg[i],
                week_offs=week_offs_list[i],
                leaves=leaves_list[i],
                comp_offs=comp_offs_list[i],
                adhoc=adhoc_list[i],
            ))
        return results

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _expand_slots(self, rule: dict) -> list[str]:
        shifts = rule.get("shifts", ["M", "A", "N"])
        allocation = rule.get("allocation", [])

        if shifts in (["G"], ["E"], ["E1"]):
            return shifts

        if not allocation:
            return shifts

        slots: list[str] = []
        for shift, cnt in zip(shifts, allocation):
            slots.extend([shift] * cnt)
        return slots

    def _parse_overrides(
        self, emp: Employee
    ) -> tuple[set[date], set[date], dict[date, str], dict | None]:
        """
        Parse planned_leaves for this employee.
        Matches by exact name first, then case-insensitive fallback.
        Returns (leaves, comp_offs, adhoc_map, always_entry|None).
        """
        leaves: set[date] = set()
        comp_offs: set[date] = set()
        adhoc: dict[date, str] = {}
        always_entry: dict | None = None

        # Case-insensitive name lookup
        pl = self.cfg.planned_leaves
        entries_raw = pl.get(emp.name) or pl.get(emp.name.upper()) or pl.get(emp.name.lower())
        if entries_raw is None:
            emp_norm = emp.name.lower().strip()
            for key, val in pl.items():
                if key.lower().strip() == emp_norm:
                    entries_raw = val
                    break
        if entries_raw is None:
            entries_raw = []

        for entry in entries_raw:
            if not isinstance(entry, dict):
                leaves.add(entry)
                continue
            t = entry.get("type", "PL")
            if t == "ALWAYS":
                always_entry = entry
            elif t == "CO":
                comp_offs.add(entry["date"])
            elif t == "ADHOC":
                adhoc[entry["date"]] = entry.get("shift", "G")
            else:
                leaves.add(entry["date"])

        return leaves, comp_offs, adhoc, always_entry


def _merge_holidays(
    account: set[date],
    loc_map: dict[str, set[date]],
    emp_location: str,
) -> set[date]:
    """Merge account-wide holidays with location-specific ones."""
    merged = set(account)
    el = emp_location.lower().replace(" ", "")
    for loc, dates in loc_map.items():
        if loc.lower().replace(" ", "") == el:
            merged |= dates
    return merged


def parse_strong_conditions(raw: str) -> dict[str, int]:
    """
    Parse strong condition strings into minimum coverage requirements.

    Examples:
        "Maintain Coverage in 2 in M / A / N / E / E1" → {M:2, A:2, N:2, E:2, E1:2}
        "Maintain Coverage in 2 in M / A / N"           → {M:2, A:2, N:2}
        "Maintain minimum coverage"                      → {} (use MCG defaults)

    Returns dict of {shift_code: min_count_required}.
    Empty dict means no strong conditions.
    """
    if not raw or not raw.strip():
        return {}

    raw_l = raw.lower().strip()
    if not any(w in raw_l for w in ("coverage", "maintain", "minimum", "required")):
        return {}

    result: dict[str, int] = {}

    # Pattern: "N in SHIFT[/SHIFT...]"  e.g. "2 in M / A / N"
    explicit = re.findall(r"(\d+)\s+in\s+([MANE][1]?)", raw, re.I)
    if explicit:
        first_count = int(explicit[0][0])
        for cnt, shift in explicit:
            result[shift.upper()] = int(cnt)
        # Capture extra shifts after the last explicit "N in X" token
        # e.g. "2 in M / A / N" → also covers A and N with count=2
        after_match = re.search(r"\d+\s+in\s+(.+)", raw, re.I)
        if after_match:
            for tok in re.split(r"[/,\s]+", after_match.group(1)):
                tok = tok.strip().upper()
                if re.match(r"^[MANE][1]?$", tok) and tok not in result:
                    result[tok] = first_count
        return result

    # No explicit counts — detect shift codes mentioned
    shifts_found = re.findall(r"\b([MANE][1]?)\b", raw, re.I)
    for s in shifts_found:
        result[s.upper()] = 1

    return result


def validate_coverage(
    schedules: list,  # list[EmployeeSchedule]
    skill_rules: dict,
    all_dates: list[date],
    skill_alias: dict | None = None,
) -> dict:
    """
    Validate shift coverage across all skill groups for every roster day.

    Returns:
        {
            "coverage_gaps":      [ {date, skill, shift, required, actual, severity, message} ],
            "understaffed_days":  [ "YYYY-MM-DD", ... ],
            "affected_skills":    [ "SkillName", ... ],
            "summary": {
                "total_warnings": N,
                "total_errors":   N,
                "total_gaps":     N,
                "affected_dates": N,
            }
        }
    """
    from collections import defaultdict

    # Group schedules by skill
    skill_scheds: dict[str, list] = defaultdict(list)
    for sched in schedules:
        sk = sched.employee.skill
        if skill_alias:
            sk = skill_alias.get(sk, sk)
        skill_scheds[sk].append(sched)

    gaps = []
    understaffed_days: set[str] = set()
    affected_skills: set[str] = set()

    for skill, rule in skill_rules.items():
        group = skill_scheds.get(skill, [])
        if not group:
            continue

        is_general = rule.get("isGeneral", False) or rule.get("shifts", []) == ["G"]
        if is_general:
            continue

        # Strong conditions override MCG defaults
        sc = rule.get("strong_conditions", {})

        # Derive minimum expected coverage per shift
        shifts     = rule.get("shifts", [])
        allocation = rule.get("allocation", [])
        min_cov: dict[str, int] = {}

        if sc:
            min_cov = sc
        else:
            # Default: at least 1 per configured shift type
            for s in shifts:
                min_cov[s] = 1

        for d in all_dates:
            dk = d.strftime("%Y-%m-%d")
            # Count working employees per shift
            shift_counts: dict[str, int] = defaultdict(int)
            for sched in group:
                code = sched.daily.get(d, "")
                if code not in NON_WORKING and code != "":
                    shift_counts[code] += 1

            # Check each required shift
            for shift, min_required in min_cov.items():
                actual = shift_counts.get(shift, 0)
                if actual < min_required:
                    severity = "ERROR" if actual == 0 else "WARNING"
                    gaps.append({
                        "date":      dk,
                        "day":       d.strftime("%a"),
                        "skill":     skill,
                        "shift":     shift,
                        "required":  min_required,
                        "actual":    actual,
                        "severity":  severity,
                        "message":   (
                            f"{skill}: {shift} shift has {actual} employee(s) "
                            f"but {min_required} required on {dk} ({d.strftime('%a')})"
                        ),
                    })
                    understaffed_days.add(dk)
                    affected_skills.add(skill)

    errors   = sum(1 for g in gaps if g["severity"] == "ERROR")
    warnings = sum(1 for g in gaps if g["severity"] == "WARNING")

    return {
        "coverage_gaps":     gaps,
        "understaffed_days": sorted(understaffed_days),
        "affected_skills":   sorted(affected_skills),
        "summary": {
            "total_errors":   errors,
            "total_warnings": warnings,
            "total_gaps":     len(gaps),
            "affected_dates": len(understaffed_days),
            "status":         "ERROR" if errors else ("WARNING" if warnings else "OK"),
        },
    }
