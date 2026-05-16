"""
scheduler.py — Enterprise Shift Scheduling Engine
==================================================

Logic pipeline per skill group:
  1.  Slot expansion       — parse "2 in M, 1 in A, 2 in N" → [M,M,A,N,N]
  2.  Slot rebalancing     — fit slots to actual employee count proportionally
  3.  Rotation engine      — build {date → shift} per employee
  4.  Week-off engine      — staggered rolling or Sat/Sun offs
  5.  Holiday merge        — AH (account) + LH (location-specific) per employee
  6.  Override engine      — apply ADHOC > AH > LH > PL > CO > WO > rotation
  7.  MCG engine           — Minimum Coverage Guarantee post-override
  8.  Shift stability      — eliminate short isolated shift intrusions
  9.  Coverage validation  — error/warning report per skill per day

Override priority order (highest → lowest):
    ADHOC > AH > LH > PL > CO > WO > rotation

  • ADHOC  — always wins; locked from MCG override
  • AH     — account holiday; locked from MCG override; cell RED
  • LH     — location holiday; locked from MCG override; cell RED
  • PL     — planned leave; locked from MCG override; cell RED
  • CO     — comp-off; locked from MCG override; cell RED
  • WO     — week-off; MCG may override for rolling rules only
  • rotation — base schedule, applies when nothing else wins

MCG rules:
  • Never override ADHOC, AH, LH, PL, CO  (all are locked)
  • Never override Sat/Sun WO for weekends-rule groups
  • For rolling week-off: may override WO as last resort
  • Protect consecutive WO pairs (6th+7th) from being split
  • Critical shifts N, E, E1 are filled before M and A
  • If strong_conditions minimum cannot be met → reduce to min 1;
    if still impossible → surface as WARNING (not silent failure)
  • MAX_CONSEC_WORK enforced:
      Rolling 7th Day     → max 6 consecutive working days
      Rolling 6th & 7th   → max 5 consecutive working days

Rotation semantics:
    "7"       Every Week     → rotate slot every 7 calendar days
    "14"      Every 2-Weeks  → rotate every 14 days
    "21"      Every 3-Weeks  → rotate every 21 days
    "monthly" Every Month    → one fixed shift per full calendar month;
                               advances at each month boundary
    "0"/"na"  Static / NA    → same shift for entire roster period

Shift codes:
    M  Morning      A  Afternoon    N  Night
    E  Evening      E1 Evening-1    G  General
    PL Planned Leave    CO Comp-Off
    ADHOC Adhoc Shift   H Holiday   W Week Off
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

from .config import AppConfig
from .loader import Employee


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

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

# All non-working day codes
NON_WORKING: frozenset[str] = frozenset({"H", "W", "PL", "CO"})

# All actual working shift codes
WORK_SHIFTS: frozenset[str] = frozenset({"M", "A", "N", "E", "E1", "G"})

# Rotatable shift codes (G is excluded — General never rotates)
VALID_SHIFTS: frozenset[str] = frozenset({"M", "A", "N", "E", "E1"})

# Codes that are permanently locked — MCG must never override these
LOCKED_CODES: frozenset[str] = frozenset({"ADHOC", "H", "PL", "CO"})

# Maximum consecutive working days per week-off rule
# rolling7th  → employees work max 6 days (1 off in 7)
# rolling6th7th → employees work max 5 days (2 off in 7)
MAX_CONSEC_WORK: dict[str, int] = {
    "rolling7th":    6,
    "rolling6th7th": 5,
}


# ═══════════════════════════════════════════════════════════════════════════════
#  DATE UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def iter_dates(start: date, end: date) -> Iterator[date]:
    """Yield every calendar date from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


# ═══════════════════════════════════════════════════════════════════════════════
#  SLOT REBALANCING
# ═══════════════════════════════════════════════════════════════════════════════

def _rebalance_slots(slots: list[str], emp_count: int) -> list[str]:
    """
    Redistribute a configured slot list to match the actual employee count
    while preserving relative proportions of each shift type.

    Minimum 1 slot per configured shift type is always preserved.

    Examples
    --------
    [M,M,A,A,N,N] + 5 → [M,M,A,N,N]
    [M,M,A,A,N,N] + 4 → [M,M,A,N]
    [M,M,A,A]     + 3 → [M,A,A]
    [M,A,N]       + 3 → [M,A,N]   (unchanged)
    """
    if not slots or emp_count <= 0:
        return list(slots)
    if emp_count == len(slots):
        return list(slots)

    counts: dict[str, int] = {}
    for s in slots:
        counts[s] = counts.get(s, 0) + 1
    types = list(counts.keys())  # preserve insertion order
    total = len(slots)

    if len(types) == 1:
        return [types[0]] * emp_count

    # Proportional allocation — minimum 1 per type
    allocated: dict[str, int] = {
        t: max(1, round(counts[t] / total * emp_count)) for t in types
    }

    # Correct rounding drift to hit exactly emp_count
    cur = sum(allocated.values())
    while cur > emp_count:
        t = max(
            (t for t in types if allocated[t] > 1),
            key=lambda t: allocated[t],
            default=None,
        )
        if t is None:
            break
        allocated[t] -= 1
        cur -= 1
    while cur < emp_count:
        t = max(types, key=lambda t: counts[t] / total - allocated[t] / emp_count)
        allocated[t] += 1
        cur += 1

    result: list[str] = []
    for t in types:
        result.extend([t] * allocated[t])
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  HOLIDAY MERGE  (AH + LH)
# ═══════════════════════════════════════════════════════════════════════════════

def _merge_holidays(
    account: set[date],
    loc_map: dict[str, set[date]],
    emp_location: str,
) -> set[date]:
    """
    Build the complete holiday set for one employee:
        employee_holidays = AH (account-wide) ∪ LH (location-specific)

    Location matching is case-insensitive and space-insensitive so that
    "Chennai", "CHENNAI", and "chennai" all resolve to the same set.
    """
    merged = set(account)
    if not emp_location or not loc_map:
        return merged
    el = emp_location.lower().replace(" ", "")
    for loc, dates in loc_map.items():
        if loc.lower().replace(" ", "") == el:
            merged |= dates
    return merged


# ═══════════════════════════════════════════════════════════════════════════════
#  WEEK-OFF CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_week_offs(
    start: date,
    end: date,
    week_off_rule: str,
    peer_index: int = 0,
) -> set[date]:
    """
    Compute the complete set of week-off (WO) dates for one employee.

    Staggering: peer_index shifts the cycle start so that employees in the
    same group never share the same rest day — preventing simultaneous absences.

    Rules
    -----
    "weekends"      → every Saturday and Sunday
    "rolling7th"    → every 7th calendar day, staggered by peer_index
    "rolling6th7th" → every 6th AND 7th day as a consecutive pair
    "na" / ""       → no week-offs (used for General shift)
    """
    all_dates = list(iter_dates(start, end))
    rule = week_off_rule.lower().strip()

    if rule in ("na", "", "none", "static"):
        return set()

    if "weekend" in rule or rule in ("weekends", "sat & sun"):
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


# ═══════════════════════════════════════════════════════════════════════════════
#  ROTATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _build_rotation(
    slots: list[str],
    rotation_type: str,
    start: date,
    end: date,
    slot_index: int,
) -> dict[date, str]:
    """
    Build {date → shift_code} for one employee for the full roster period.

    Monthly rotation
    ----------------
    Employee keeps ONE fixed shift for the entire calendar month.
    At each month boundary the slot advances by 1.
    This guarantees:
      • Zero mid-month shift changes (stable, predictable scheduling)
      • Clean rotation continuity across multi-month rosters
      • Employee knows their shift a full month in advance

    Staggering: slot_index (= peer_index within the skill group) gives each
    employee a different starting slot so all shifts are always covered.
    """
    all_dates = list(iter_dates(start, end))
    n = len(slots)
    daily: dict[date, str] = {}

    if n == 0:
        for d in all_dates:
            daily[d] = "M"
        return daily

    rtype = rotation_type.lower().strip()

    # ── Static / NA ───────────────────────────────────────────────────────────
    if rtype in ("0", "na", "none", "static"):
        shift = slots[slot_index % n]
        for d in all_dates:
            daily[d] = shift
        return daily

    # ── Every Month ───────────────────────────────────────────────────────────
    if rtype == "monthly":
        seen_months: list[tuple[int, int]] = []
        for d in all_dates:
            key = (d.year, d.month)
            if not seen_months or seen_months[-1] != key:
                seen_months.append(key)
        month_offset: dict[tuple[int, int], int] = {
            k: i for i, k in enumerate(seen_months)
        }
        for d in all_dates:
            key = (d.year, d.month)
            offset = month_offset[key]
            daily[d] = slots[(offset + slot_index) % n]
        return daily

    # ── Every Week / 2-Weeks / 3-Weeks  (day-count rotation) ─────────────────
    rotation_days = int(rtype) if rtype.isdigit() else 7
    for d in all_dates:
        day_num = (d - start).days
        daily[d] = slots[(day_num // rotation_days + slot_index) % n]

    return daily


# ═══════════════════════════════════════════════════════════════════════════════
#  MCG — MINIMUM COVERAGE GUARANTEE
# ═══════════════════════════════════════════════════════════════════════════════

def _is_paired_week_off(
    emp_idx: int,
    d: date,
    daily: list[dict[date, str]],
) -> bool:
    """
    Return True if day d is part of a consecutive WO-pair for this employee.
    Used to protect Rolling (6th & 7th Day) paired rest blocks from being split
    by the MCG engine.
    """
    prev_d = d - timedelta(days=1)
    next_d = d + timedelta(days=1)
    return (
        daily[emp_idx].get(prev_d) == "W"
        or daily[emp_idx].get(next_d) == "W"
    )


def _apply_mcg(
    required_shifts: list[str],
    min_coverage: dict[str, int],
    all_dates: list[date],
    base_daily_per_emp: list[dict[date, str]],
    locked_per_emp: list[set[date]],
    wo_rule: str = "",
) -> list[dict[date, str]]:
    """
    Minimum Coverage Guarantee engine.

    For every calendar day:
      1. Count working employees per required shift type.
      2. For each shift below min_coverage, find a donor and reassign.
      3. Donor selection order (strict):
           Pass A — unpaired WO employee when surplus exists elsewhere
           Pass B — paired WO employee when surplus exists elsewhere
           Pass C — employee on surplus working shift (>min)
           Pass D — any unlocked WO (rolling rule only, last resort)
      4. ADHOC / AH / LH / PL / CO dates are LOCKED — never touched.
      5. Weekends-rule Sat/Sun WO never overridden.
      6. If strong_conditions minimum cannot be met → try with min=1;
         if still impossible → leave gap (surfaces as validation WARNING).

    Critical shift fill order: N → E → E1 → M → A
    (night/evening staffing is prioritised over day shifts)
    """
    n_emp = len(base_daily_per_emp)
    if n_emp == 0 or not required_shifts:
        return base_daily_per_emp

    daily = [dict(d) for d in base_daily_per_emp]  # deep copy
    is_rolling = "rolling" in wo_rule.lower()

    # Fill critical shifts first: N, E, E1 before M and A
    critical     = [s for s in ("N", "E", "E1") if s in required_shifts]
    non_critical = [s for s in required_shifts if s not in critical]
    ordered_shifts = critical + non_critical

    for d in all_dates:
        # Never force coverage on weekends for weekends-rule groups
        if is_weekend(d) and not is_rolling:
            continue

        # Current coverage snapshot
        coverage: dict[str, list[int]] = {s: [] for s in required_shifts}
        for i in range(n_emp):
            code = daily[i].get(d, "")
            if code in coverage:
                coverage[code].append(i)

        for target in ordered_shifts:
            # Try with configured min first; fall back to 1 if impossible
            for effective_min in (min_coverage.get(target, 1), 1):
                while len(coverage.get(target, [])) < effective_min:

                    donor_idx: int | None = None

                    # ── Pass A: unpaired WO with surplus elsewhere ─────────
                    for i in range(n_emp):
                        if daily[i].get(d) != "W":
                            continue
                        if d in locked_per_emp[i]:
                            continue
                        if _is_paired_week_off(i, d, daily):
                            continue  # skip paired — try unpaired first
                        if any(
                            len(coverage.get(s, [])) > min_coverage.get(s, 1)
                            for s in required_shifts if s != target
                        ):
                            donor_idx = i
                            break

                    # ── Pass B: paired WO with surplus elsewhere ───────────
                    if donor_idx is None:
                        for i in range(n_emp):
                            if daily[i].get(d) != "W":
                                continue
                            if d in locked_per_emp[i]:
                                continue
                            if not _is_paired_week_off(i, d, daily):
                                continue
                            if any(
                                len(coverage.get(s, [])) > min_coverage.get(s, 1)
                                for s in required_shifts if s != target
                            ):
                                donor_idx = i
                                break

                    # ── Pass C: surplus working shift employee ─────────────
                    if donor_idx is None:
                        for i in range(n_emp):
                            code = daily[i].get(d, "")
                            if code not in VALID_SHIFTS or code == "G":
                                continue
                            if d in locked_per_emp[i]:
                                continue
                            if (code in coverage
                                    and len(coverage[code]) > min_coverage.get(code, 1)):
                                donor_idx = i
                                break

                    # ── Pass D: last resort — any WO (rolling only) ────────
                    if donor_idx is None and is_rolling:
                        for prefer_unpaired in (True, False):
                            for i in range(n_emp):
                                if daily[i].get(d) != "W":
                                    continue
                                if d in locked_per_emp[i]:
                                    continue
                                if prefer_unpaired and _is_paired_week_off(i, d, daily):
                                    continue
                                donor_idx = i
                                break
                            if donor_idx is not None:
                                break

                    if donor_idx is None:
                        break  # gap cannot be filled — surfaces as validation warning

                    old_code = daily[donor_idx].get(d, "")
                    daily[donor_idx][d] = target
                    coverage[target].append(donor_idx)
                    if old_code in coverage:
                        coverage[old_code] = [
                            x for x in coverage[old_code] if x != donor_idx
                        ]

                # If target is now covered, break out of effective_min loop
                if len(coverage.get(target, [])) >= effective_min:
                    break

    return daily


# ═══════════════════════════════════════════════════════════════════════════════
#  SHIFT STABILITY SMOOTHER
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_shift_stability(
    daily_list: list[dict[date, str]],
    all_dates: list[date],
    locked_per_emp: list[set[date]],
    required_shifts: list[str],
    min_coverage: dict[str, int],
    min_window: int = 7,
) -> list[dict[date, str]]:
    """
    Post-MCG shift stability smoother.

    Prevents employee fatigue from irregular shift sequences like M-A-N or
    isolated 1–2 day shift intrusions caused by MCG borrowing.

    Target: employees should stay on one shift for at least min_window
    consecutive working days wherever coverage constraints allow.

    Algorithm (per employee):
      1. Build contiguous shift segments.
      2. For each segment < min_window days on a working shift (M/A/N/E/E1):
         a. Find the nearest working segments before AND after.
         b. If they are the same shift (surrounding shift ≠ this segment),
            this is a short "intrusion" of a different shift.
         c. Check every date in the segment: can it be changed to the
            surrounding shift without dropping coverage below min?
            (Other employees must still cover the intrusion's shift.)
         d. If yes → smooth the intrusion out.
         e. If no  → leave it (coverage always wins over stability).
      3. Never touch LOCKED dates (ADHOC / H / PL / CO / W).
    """
    n_emp = len(daily_list)
    if n_emp == 0:
        return daily_list

    result = [dict(d) for d in daily_list]  # deep copy

    for emp_idx in range(n_emp):
        daily  = result[emp_idx]
        locked = locked_per_emp[emp_idx]

        # Build contiguous segment list: [(shift_code, [date, ...]), ...]
        segments: list[tuple[str, list[date]]] = []
        for d in all_dates:
            shift = daily.get(d, "")
            if segments and segments[-1][0] == shift:
                segments[-1][1].append(d)
            else:
                segments.append((shift, [d]))

        for seg_idx, (seg_shift, seg_dates) in enumerate(segments):
            # Only process actual working shifts
            if seg_shift not in VALID_SHIFTS:
                continue
            # Already long enough — nothing to smooth
            if len(seg_dates) >= min_window:
                continue

            # Find nearest working segments before and after this one
            prev_work: str | None = None
            next_work: str | None = None

            for pi in range(seg_idx - 1, -1, -1):
                if segments[pi][0] in VALID_SHIFTS:
                    prev_work = segments[pi][0]
                    break
            for ni in range(seg_idx + 1, len(segments)):
                if segments[ni][0] in VALID_SHIFTS:
                    next_work = segments[ni][0]
                    break

            # Smooth only if both neighbours are the same shift and it differs
            # from the current short segment (true "intrusion" pattern)
            if (prev_work is None
                    or next_work is None
                    or prev_work != next_work
                    or prev_work == seg_shift):
                continue

            target_shift = prev_work

            # Verify every date can be changed safely
            can_smooth = True
            for d in seg_dates:
                if d in locked:
                    can_smooth = False
                    break
                # Other employees must still cover seg_shift at min level
                other_count = sum(
                    1 for eidx in range(n_emp)
                    if eidx != emp_idx and result[eidx].get(d) == seg_shift
                )
                if other_count < min_coverage.get(seg_shift, 1):
                    can_smooth = False
                    break

            if can_smooth:
                for d in seg_dates:
                    result[emp_idx][d] = target_shift

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  RESULT DATACLASS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EmployeeSchedule:
    employee:           Employee
    daily:              dict[date, str]
    week_offs:          set[date]
    leaves:             set[date]
    comp_offs:          set[date]
    adhoc:              dict[date, str]  = field(default_factory=dict)
    location_holidays:  set[date]        = field(default_factory=set)
    account_holidays:   set[date]        = field(default_factory=set)

    def shift_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for code in self.daily.values():
            counts[code] = counts.get(code, 0) + 1
        return counts

    def working_days(self) -> int:
        return sum(1 for c in self.daily.values() if c not in NON_WORKING)


# ═══════════════════════════════════════════════════════════════════════════════
#  STRONG CONDITIONS PARSER
# ═══════════════════════════════════════════════════════════════════════════════

def parse_strong_conditions(raw: str) -> dict[str, int]:
    """
    Parse the optional 6th column "Strong Conditions" into minimum coverage
    thresholds per shift type.

    Supported formats
    -----------------
    "Maintain 2 in M / A / N / E / E1"         → {M:2, A:2, N:2, E:2, E1:2}
    "Maintain Coverage in 2 in M / A / N"       → {M:2, A:2, N:2}
    "Maintain Coverage in 1 in M, 1 in A"       → {M:1, A:1}
    "Maintain minimum coverage in M / A / N"    → {M:1, A:1, N:1}
    "" or G-only                                → {}  (no conditions)

    Fallback: if not enough employees are available to meet the configured
    minimum, the MCG engine falls back to minimum = 1 automatically.
    """
    if not raw or not raw.strip():
        return {}

    raw_l = raw.lower().strip()
    if not any(w in raw_l for w in ("coverage", "maintain", "minimum", "required")):
        return {}

    result: dict[str, int] = {}

    # Pattern: "N in SHIFT" e.g. "2 in M", "1 in A"
    explicit = re.findall(r"(\d+)\s+in\s+([MANE][1]?)", raw, re.I)
    if explicit:
        first_count = int(explicit[0][0])
        for cnt, shift in explicit:
            result[shift.upper()] = int(cnt)
        # Also capture extra shifts listed after the count group
        # e.g. "2 in M / A / N / E / E1" → A, N, E, E1 inherit count 2
        after_match = re.search(r"\d+\s+in\s+(.+)", raw, re.I)
        if after_match:
            for tok in re.split(r"[/,\s]+", after_match.group(1)):
                tok = tok.strip().upper()
                if re.match(r"^(M|A|N|E|E1)$", tok) and tok not in result:
                    result[tok] = first_count
        return result

    # No explicit counts — detect shift codes, default min = 1
    for m in re.finditer(r"\b(M|A|N|E|E1)\b", raw, re.I):
        result[m.group(1).upper()] = 1

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  COVERAGE VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def validate_coverage(
    schedules: list,
    skill_rules: dict,
    all_dates: list[date],
    skill_alias: dict | None = None,
) -> dict:
    """
    Post-generation coverage validation report.

    For every skill group × every roster day × every required shift:
      • ERROR   — actual = 0  (shift completely uncovered)
      • WARNING — actual < strong_conditions minimum

    General (G) shift groups are excluded.
    Results are structured for both UI display and JSON export.
    """
    skill_scheds: dict[str, list] = defaultdict(list)
    for sched in schedules:
        sk = sched.employee.skill
        if skill_alias:
            sk = skill_alias.get(sk, sk)
        skill_scheds[sk].append(sched)

    gaps:               list[dict]  = []
    understaffed_days:  set[str]    = set()
    affected_skills:    set[str]    = set()

    for skill, rule in skill_rules.items():
        group = skill_scheds.get(skill, [])
        if not group:
            continue

        is_general = rule.get("isGeneral", False) or rule.get("shifts", []) == ["G"]
        if is_general:
            continue

        sc     = rule.get("strong_conditions", {})
        shifts = rule.get("shifts", [])
        min_cov: dict[str, int] = sc if sc else {s: 1 for s in shifts if s != "G"}

        for d in all_dates:
            dk = d.strftime("%Y-%m-%d")
            shift_counts: dict[str, int] = defaultdict(int)
            for sched in group:
                code = sched.daily.get(d, "")
                if code not in NON_WORKING and code:
                    shift_counts[code] += 1

            for shift, min_req in min_cov.items():
                actual = shift_counts.get(shift, 0)
                if actual < min_req:
                    severity = "ERROR" if actual == 0 else "WARNING"
                    gaps.append({
                        "date":     dk,
                        "day":      d.strftime("%a"),
                        "skill":    skill,
                        "shift":    shift,
                        "required": min_req,
                        "actual":   actual,
                        "severity": severity,
                        "message":  (
                            f"{skill}: {shift} shift has {actual} employee(s) "
                            f"but {min_req} required on {dk} ({d.strftime('%a')})"
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


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

class ShiftScheduler:
    """
    Enterprise shift scheduler — orchestrates the full pipeline per skill group.

    Pipeline per group:
      1. Expand allocation rule  → ordered slot list  [M,M,A,N,N]
      2. Rebalance slots         → fit to actual employee count
      3. Build base rotation     → {date → shift} per employee
      4. Compute week-offs       → staggered per peer_index
      5. Apply overrides         → ADHOC > AH > LH > PL > CO > WO > rotation
      6. Run MCG                 → enforce minimum coverage
      7. Shift stability smoother → eliminate short isolated intrusions
      8. Return EmployeeSchedule objects
    """

    def __init__(self, cfg: AppConfig, employees: list[Employee]) -> None:
        self.cfg           = cfg
        self.employees     = employees
        self._all_dates    = list(iter_dates(cfg.roster_start, cfg.roster_end))
        self._loc_hol_map: dict[str, set[date]] = getattr(cfg, "location_holidays", {}) or {}

        self._skill_groups: dict[str, list[Employee]] = {}
        for emp in employees:
            self._skill_groups.setdefault(emp.skill, []).append(emp)

    def run(self) -> list[EmployeeSchedule]:
        processed: dict[str, list[EmployeeSchedule]] = {}
        for skill, group in self._skill_groups.items():
            processed[skill] = self._schedule_group(skill, group)

        # Return in original input-file employee order
        emp_order = {emp.name: i for i, emp in enumerate(self.employees)}
        all_scheds = [s for scheds in processed.values() for s in scheds]
        all_scheds.sort(key=lambda s: emp_order.get(s.employee.name, 999))
        return all_scheds

    # ─── Group-level pipeline ─────────────────────────────────────────────────

    def _schedule_group(
        self, skill: str, group: list[Employee]
    ) -> list[EmployeeSchedule]:
        cfg        = self.cfg
        rule       = cfg.skill_rules.get(skill, {})
        is_general = rule.get("isGeneral", False) or rule.get("shifts", []) == ["G"]

        # ── Step 1 & 2: Slot expansion + rebalancing ──────────────────────────
        raw_slots = self._expand_slots(rule)
        eff_slots = _rebalance_slots(raw_slots, len(group))

        rotation_type = str(rule.get("rotation_type", "0"))
        wo_rule       = rule.get("week_off", "weekends")

        # Unique required shifts in original order (no G)
        required_shifts = list(dict.fromkeys(
            s for s in eff_slots if s not in ("G",)
        ))

        # Min coverage: strong_conditions if present, else default 1
        sc = rule.get("strong_conditions", {})
        min_coverage: dict[str, int] = (
            {s: sc[s] for s in sc}
            if sc
            else {s: 1 for s in required_shifts}
        )

        # ── Step 3 & 4: Build base rotation + week-offs per employee ──────────
        base_dailys:    list[dict[date, str]] = []
        week_offs_list: list[set[date]]       = []
        leaves_list:    list[set[date]]       = []
        comp_offs_list: list[set[date]]       = []
        adhoc_list:     list[dict[date, str]] = []
        locked_list:    list[set[date]]       = []
        loc_hols_list:  list[set[date]]       = []

        for peer_idx, emp in enumerate(group):
            leaves, comp_offs, adhoc, always_entry = self._parse_overrides(emp)

            # AH + LH merged into one holiday set per employee
            emp_holidays = _merge_holidays(
                cfg.account_holidays, self._loc_hol_map, emp.location
            )

            week_offs = _compute_week_offs(
                cfg.roster_start, cfg.roster_end, wo_rule, peer_idx
            )

            # ALWAYS override: reassign the employee to a different shift
            # for the entire roster period, with optional new week-off rule
            if always_entry:
                awo_raw = always_entry.get("weekOff", "")
                if awo_raw and awo_raw.strip().lower() not in ("na", ""):
                    from roster.web.app import _normalise_weekoff  # type: ignore
                    try:
                        awo_rule  = _normalise_weekoff(awo_raw)
                        week_offs = _compute_week_offs(
                            cfg.roster_start, cfg.roster_end, awo_rule, peer_idx
                        )
                    except Exception:
                        pass
                base = _build_rotation(
                    [always_entry["shift"]], "0",
                    cfg.roster_start, cfg.roster_end, 0
                )
            else:
                base = _build_rotation(
                    eff_slots, rotation_type,
                    cfg.roster_start, cfg.roster_end, peer_idx
                )

            # Locked dates: MCG must never override ADHOC, AH, LH, PL, CO
            locked: set[date] = set()
            for d in self._all_dates:
                if (d in leaves
                        or d in comp_offs
                        or d in adhoc
                        or d in emp_holidays):
                    locked.add(d)

            base_dailys.append(base)
            week_offs_list.append(week_offs)
            leaves_list.append(leaves)
            comp_offs_list.append(comp_offs)
            adhoc_list.append(adhoc)
            locked_list.append(locked)
            loc_hols_list.append(emp_holidays)

        # ── Step 5: Apply override priority per day ───────────────────────────
        # Priority: ADHOC > AH > LH > PL > CO > WO > rotation
        pre_mcg: list[dict[date, str]] = []
        for i in range(len(group)):
            daily:     dict[date, str] = {}
            base       = base_dailys[i]
            leaves     = leaves_list[i]
            comp_offs  = comp_offs_list[i]
            adhoc      = adhoc_list[i]
            week_offs  = week_offs_list[i]
            emp_hols   = loc_hols_list[i]  # AH + LH combined

            for d in self._all_dates:
                if d in adhoc:
                    # ADHOC is the highest priority
                    daily[d] = adhoc[d]
                elif d in emp_hols:
                    # AH and LH both map to "H" — cell RED in output
                    daily[d] = "H"
                elif d in leaves:
                    daily[d] = "PL"
                elif d in comp_offs:
                    daily[d] = "CO"
                elif d in week_offs:
                    daily[d] = "W"
                else:
                    fallback = eff_slots[i % len(eff_slots)] if eff_slots else "M"
                    daily[d] = base.get(d, fallback)

            pre_mcg.append(daily)

        # ── Step 6: MCG ───────────────────────────────────────────────────────
        if not is_general and required_shifts:
            post_mcg = _apply_mcg(
                required_shifts=required_shifts,
                min_coverage=min_coverage,
                all_dates=self._all_dates,
                base_daily_per_emp=pre_mcg,
                locked_per_emp=locked_list,
                wo_rule=wo_rule,
            )
        else:
            post_mcg = pre_mcg

        # ── Step 7: Shift stability smoother ─────────────────────────────────
        if not is_general and required_shifts:
            post_mcg = _apply_shift_stability(
                daily_list=post_mcg,
                all_dates=self._all_dates,
                locked_per_emp=locked_list,
                required_shifts=required_shifts,
                min_coverage=min_coverage,
                min_window=7,
            )

        # ── Step 8: Build result objects ─────────────────────────────────────
        acct_hols = cfg.account_holidays
        return [
            EmployeeSchedule(
                employee=group[i],
                daily=post_mcg[i],
                week_offs=week_offs_list[i],
                leaves=leaves_list[i],
                comp_offs=comp_offs_list[i],
                adhoc=adhoc_list[i],
                location_holidays=loc_hols_list[i] - acct_hols,
                account_holidays=acct_hols,
            )
            for i in range(len(group))
        ]

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _expand_slots(self, rule: dict) -> list[str]:
        """
        Expand the allocation rule into a flat ordered slot list.
        "2 in M, 1 in A, 2 in N" → [M, M, A, N, N]
        """
        shifts     = rule.get("shifts", ["M", "A", "N"])
        allocation = rule.get("allocation", [])

        if shifts in (["G"], ["E"], ["E1"]):
            return list(shifts)
        if not allocation:
            return list(shifts)

        slots: list[str] = []
        for shift, cnt in zip(shifts, allocation):
            slots.extend([shift] * cnt)
        return slots

    def _parse_overrides(
        self, emp: Employee
    ) -> tuple[set[date], set[date], dict[date, str], dict | None]:
        """
        Extract PL / CO / ADHOC / ALWAYS overrides for this employee.
        Uses case-insensitive name lookup so 'SOWMIYA A' matches 'Sowmiya A'.
        """
        leaves:       set[date]       = set()
        comp_offs:    set[date]       = set()
        adhoc:        dict[date, str] = {}
        always_entry: dict | None     = None

        pl  = self.cfg.planned_leaves
        raw = (
            pl.get(emp.name)
            or pl.get(emp.name.upper())
            or pl.get(emp.name.lower())
        )
        if raw is None:
            norm = emp.name.lower().strip()
            for key, val in pl.items():
                if key.lower().strip() == norm:
                    raw = val
                    break
        if raw is None:
            return leaves, comp_offs, adhoc, always_entry

        for entry in raw:
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
