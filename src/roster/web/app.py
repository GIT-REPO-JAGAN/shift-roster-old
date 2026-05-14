"""
web/app.py
----------
Flask web application for the Shift Roster Generator.

Endpoints:
    GET  /                → main UI
    POST /api/skills      → skill names + counts from uploaded xlsx
    POST /api/generate    → generate roster Excel
    GET  /api/health      → health check
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import traceback
import threading
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
import json as _json_module

import sys
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from roster.config import AppConfig
from roster.loader import load_employees
from roster.scheduler import ShiftScheduler, iter_dates, parse_strong_conditions, validate_coverage
from roster.writer import write_workbook
from roster.prompt import _parse_day_list, build_skill_alias_map

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ── Leave parser ───────────────────────────────────────────────────────────────

def _parse_date_token(token: str, default_year: int, default_month: int):
    """
    Parse a single date token into a date object.

    Accepted formats:
        DD                  → uses default_year, default_month
        DD-MM-YYYY          → explicit full date
        DD/MM/YYYY          → explicit full date (slash variant)
        YYYY-MM-DD          → ISO format
    Returns None if the token cannot be parsed.
    """
    token = token.strip()
    if not token:
        return None

    # DD-MM-YYYY or DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", token)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None

    # YYYY-MM-DD (ISO)
    m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$", token)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # Bare day number DD
    if re.match(r"^\d{1,2}$", token):
        try:
            return date(default_year, default_month, int(token))
        except ValueError:
            return None

    return None


def _parse_date_list(raw: str, default_year: int, default_month: int) -> list:
    """
    Parse a comma-separated list of dates.
    Each token may be DD, DD-MM-YYYY, DD/MM/YYYY, or YYYY-MM-DD.
    Returns a list of date objects (invalid tokens are silently skipped).
    """
    dates = []
    for token in re.split(r",\s*", raw.strip()):
        token = token.strip()
        if not token:
            continue
        d = _parse_date_token(token, default_year, default_month)
        if d:
            dates.append(d)
    return dates


def _parse_date_range(raw: str, default_year: int, default_month: int) -> list:
    """
    Parse a date range "START to END" or "START-END".

    START / END may each be DD, DD-MM-YYYY, DD/MM/YYYY, or YYYY-MM-DD.

    Examples:
        06 to 09
        06-09
        20-06-2026 to 22-07-2026
        20/06/2026 to 22/07/2026

    Returns a list of all dates in the range (inclusive).
    """
    raw = raw.strip()

    # "START to END" (space-separated "to")
    m = re.match(r"^(.+?)\s+to\s+(.+)$", raw, re.I)
    if m:
        start = _parse_date_token(m.group(1).strip(), default_year, default_month)
        end   = _parse_date_token(m.group(2).strip(), default_year, default_month)
        if start and end and end >= start:
            result = []
            current = start
            while current <= end:
                result.append(current)
                current = date.fromordinal(current.toordinal() + 1)
            return result
        return []

    # "DD-DD" bare day range (no year info — same month) e.g. "06-09"
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", raw)
    if m:
        d1, d2 = int(m.group(1)), int(m.group(2))
        try:
            result = []
            for day in range(d1, d2 + 1):
                result.append(date(default_year, default_month, day))
            return result
        except ValueError:
            return []

    # "DD-MM-YYYY to DD-MM-YYYY" already handled above via "to" branch
    # Try single date fallback
    d = _parse_date_token(raw, default_year, default_month)
    return [d] if d else []


def _parse_leave_lines(raw: str, year: int, month: int) -> dict[str, list]:
    """
    Parse planned leave, comp-off, and adhoc shift entries.

    Supported formats (all case-insensitive):

        Single/multi date — bare day numbers (same month as roster start):
            Name – PL: 05, 08
            Name – COFF: 05, 08

        Single/multi date — DD-MM-YYYY (multi-month support):
            Name – PL: 05-06-2026, 08-07-2026
            Name – COFF: 12-06-2026, 13-07-2026

        Adhoc shift — single day:
            Per Day – Name – Adhoc Shift: 05 | G
            Per Day – Name – Adhoc Shift: 15-06-2026 | M

        Adhoc shift — date range (bare days or full dates):
            Period Time – Name – Adhoc Shift: 06 to 09 | N
            Period Time – Name – Adhoc Shift: 20-06-2026 to 22-07-2026 | M

    The optional "Per Day –" or "Period Time –" prefix is stripped before
    parsing — it exists only as a visual label for the user.
    The shift code after | is one of: G / M / A / N / E
    """
    result: dict[str, list] = {}

    # Label prefixes users can optionally add — strip them before parsing
    LABEL_PREFIXES = re.compile(
        r"^(per\s+day|period\s+time|adhoc\s+day|adhoc\s+period)\s*[–\-]\s*",
        re.I,
    )

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Strip optional label prefix ("Per Day –", "Period Time –")
        line = LABEL_PREFIXES.sub("", line).strip()

        # Split name from the rest — use maxsplit=1
        parts = re.split(r"\s*[–\-]\s*", line, maxsplit=1)
        if len(parts) < 2:
            continue

        name      = parts[0].strip()
        remainder = parts[1].strip()
        if not name:
            continue

        # ── ALWAYS override ──────────────────────────────────────────────────
        if re.match(r"throughout\s*month", remainder, re.I):
            pipe_parts = remainder.split("|")
            shift_raw = pipe_parts[1].strip().split()[0].upper() if len(pipe_parts) > 1 else "G"
            shift_code = shift_raw if shift_raw in ("M","A","N","E","E1","G") else "G"
            rotation_raw = pipe_parts[2].strip() if len(pipe_parts) > 2 else ""
            weekoff_raw  = pipe_parts[3].strip() if len(pipe_parts) > 3 else ""
            entries = result.setdefault(name, [])
            entries.append({
                "type": "ALWAYS", "shift": shift_code,
                "rotation": rotation_raw, "weekOff": weekoff_raw,
            })
            continue

        # ── Adhoc Shift ───────────────────────────────────────────────────────
        if re.match(r"adhoc\s*shift\s*[:\-]", remainder, re.I):
            body = re.sub(r"^adhoc\s*shift\s*[:\-]\s*", "", remainder, flags=re.I).strip()

            # Split on | for shift code — shift part may be "G" or "G / M / A / N"
            if "|" in body:
                date_part, shift_part = body.split("|", 1)
                shift_code = re.split(r"[/\s]+", shift_part.strip())[0].upper()
                if shift_code not in ("M", "A", "N", "E", "E1", "G"):
                    shift_code = "G"
            else:
                date_part  = body
                shift_code = "G"

            date_part = date_part.strip()
            entries   = result.setdefault(name, [])

            # Check if it is a range ("X to Y" or bare "DD-DD")
            is_range = bool(
                re.search(r"\bto\b", date_part, re.I) or
                re.match(r"^\d{1,2}-\d{1,2}$", date_part)
            )

            if is_range:
                for d in _parse_date_range(date_part, year, month):
                    entries.append({"type": "ADHOC", "shift": shift_code, "date": d})
            else:
                # Could be a single date (bare day or DD-MM-YYYY)
                d = _parse_date_token(date_part, year, month)
                if d:
                    entries.append({"type": "ADHOC", "shift": shift_code, "date": d})
            continue

        # ── CO / COFF / Comp Off ──────────────────────────────────────────────
        if re.match(r"(comp\s*off|co(?:ff)?)\s*[:\-]", remainder, re.I):
            leave_type = "CO"
            remainder  = re.sub(r"^(comp\s*off|co(?:ff)?)\s*[:\-]\s*", "", remainder, flags=re.I)

        # ── PL / Planned Leave ────────────────────────────────────────────────
        elif re.match(r"(planned\s*leave|pl)\s*[:\-]", remainder, re.I):
            leave_type = "PL"
            remainder  = re.sub(r"^(planned\s*leave|pl)\s*[:\-]\s*", "", remainder, flags=re.I)

        else:
            leave_type = "PL"  # default

        # Parse list of dates (bare DD or DD-MM-YYYY mixed)
        dates = _parse_date_list(remainder.strip(), year, month)
        if not dates:
            continue

        entries = result.setdefault(name, [])
        for d in dates:
            entries.append({"type": leave_type, "date": d})

    return result


# ── Rule parser ────────────────────────────────────────────────────────────────

def _parse_rules_extended(rules_text: str) -> dict:
    """
    Parse 5-column shift rules table:
        | Skill | Count | Shift Allocation | Rotation | Week Off |

    Rotation accepted values:
        Every Week / 1-Week / 7        → rotation_type = "7"
        Every 2-Weeks / 2-Week / 14    → rotation_type = "14"
        Every 3-Weeks / 3-Week / 21    → rotation_type = "21"
        Every Month / Monthly          → rotation_type = "monthly"
        NA / Static / 0                → rotation_type = "0"

    Week Off accepted values:
        Sat & Sun / Saturday & Sunday  → "weekends"
        Rolling (7th Day)              → "rolling7th"
        Rolling (6th & 7th)            → "rolling6th7th"
        Every 5th day                  → "every5th"
        NA / None / Static             → "na"  (for General)
    """
    skill_rules: dict = {}

    for line in rules_text.splitlines():
        line = line.strip()
        if not line or not line.startswith("|"):
            continue
        if re.match(r"^\|[\s\-\|]+\|?$", line):
            continue
        if re.search(r"Skill\s*\|.*Count", line, re.I):
            continue
        if re.match(r"^\|\s*Skill\s*(Category)?\s*\|", line, re.I):
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue

        skill_raw    = cells[0].strip()
        count_raw    = cells[1].strip()
        alloc_raw    = cells[2].strip()
        rotation_raw = cells[3].strip()
        weekoff_raw  = cells[4].strip()

        if not skill_raw:
            continue

        # Skip header-word skill names
        if re.match(r"^(skill|count|allocation|rotation|week\s*off)$", skill_raw, re.I):
            continue

        count_m = re.search(r"\d+", count_raw)
        count = int(count_m.group()) if count_m else 1
        if count == 0:
            continue

        # ── Shift allocation ───────────────────────────────────────────────────
        shifts:     list[str] = []
        allocation: list[int] = []
        is_general  = False

        alloc_upper = alloc_raw.upper().strip()

        if re.match(r"^G(\s+ONLY)?$", alloc_upper):
            shifts = ["G"]; is_general = True

        elif re.match(r"^E(\s+ONLY)?$", alloc_upper):
            shifts = ["E"]

        else:
            # "2 in M, 2 in A, 2 in N"
            slot_matches = re.findall(r"(\d+)\s+in\s+([MANE][1]?)", alloc_raw, re.I)
            if slot_matches:
                for cnt, shift in slot_matches:
                    allocation.append(int(cnt))
                    shifts.append(shift.upper())
            else:
                for tok in re.split(r"[\s/,]+", alloc_upper):
                    tok = tok.strip()
                    if tok in ("M", "A", "N", "E") and tok not in shifts:
                        shifts.append(tok)

        if not shifts:
            shifts = ["M", "A", "N"]

        # ── Rotation type ─────────────────────────────────────────────────────
        rotation_type = "0"
        r_lower = rotation_raw.lower().strip()

        if is_general or r_lower in ("na", "static", "none", ""):
            rotation_type = "0"
        elif "month" in r_lower:
            rotation_type = "monthly"
        elif "3" in r_lower and "week" in r_lower:
            rotation_type = "21"
        elif "2" in r_lower and "week" in r_lower:
            rotation_type = "14"
        elif "1" in r_lower and "week" in r_lower:
            rotation_type = "7"
        elif "every week" in r_lower:
            rotation_type = "7"
        else:
            # Try bare numbers
            nm = re.search(r"\d+", rotation_raw)
            if nm:
                rotation_type = nm.group()
            else:
                rotation_type = "0"

        # ── Week off ──────────────────────────────────────────────────────────
        wo_lower = weekoff_raw.lower().strip()

        if re.search(r"sat|sun|weekend", wo_lower):
            week_off = "weekends"
        elif re.search(r"na|none|static", wo_lower):
            week_off = "na"
        else:
            nums = re.findall(r"\d+", wo_lower)
            if nums:
                if "rolling" in wo_lower:
                    week_off = "rolling" + "_".join(nums) + ("th" if len(nums) == 1 else "")
                    # Normalise to canonical form
                    if set(nums) == {"6", "7"} or set(nums) == {"7", "6"}:
                        week_off = "rolling6th7th"
                    elif nums == ["7"]:
                        week_off = "rolling7th"
                else:
                    week_off = "every" + "_".join(f"{n}th" for n in nums)
            else:
                week_off = "weekends"

        # ── Strong Conditions (6th column, optional) ────────────────────
        strong_conditions: dict = {}
        if len(cells) >= 6 and cells[5].strip():
            strong_conditions = parse_strong_conditions(cells[5].strip())

        skill_rules[skill_raw] = {
            "count":             count,
            "shifts":            shifts,
            "allocation":        allocation,
            "rotation_type":     rotation_type,
            "week_off":          week_off,
            "strong_conditions": strong_conditions,
        }

    return skill_rules


def _build_roster_json(
    schedules: list,
    cfg,
    skill_rules: dict,
    validation: dict,
) -> dict:
    """
    Build the complete roster JSON output.
    Contains all roster details, shift allocations, leave/CO, rotation,
    week-off info, holiday mappings, and validation messages.
    """
    from datetime import date as _date

    all_dates = list(iter_dates(cfg.roster_start, cfg.roster_end))

    employees_json = []
    for sched in schedules:
        emp = sched.employee
        daily_list = []
        for d in all_dates:
            code = sched.daily.get(d, "")
            daily_list.append({
                "date":    d.strftime("%Y-%m-%d"),
                "day":     d.strftime("%a"),
                "shift":   code,
                "is_working": code not in ("H", "W", "PL", "CO") and code != "",
                "category": (
                    "leave"    if code == "PL" else
                    "comp_off" if code == "CO" else
                    "holiday"  if code == "H"  else
                    "week_off" if code == "W"  else
                    "adhoc"    if code == "ADHOC" else
                    "work"
                ),
            })

        # Leave / CO / adhoc details
        leaves   = [d.strftime("%Y-%m-%d") for d in sorted(sched.leaves)]
        comp_offs= [d.strftime("%Y-%m-%d") for d in sorted(sched.comp_offs)]
        adhoc_map= {d.strftime("%Y-%m-%d"): s for d, s in sorted(sched.adhoc.items())}
        wo_dates = [d.strftime("%Y-%m-%d") for d in sorted(sched.week_offs)]

        counts = sched.shift_counts()
        employees_json.append({
            "name":         emp.name,
            "email":        getattr(emp, "email", ""),
            "skill":        emp.skill,
            "location":     getattr(emp, "location", ""),
            "primary_shift": next(
                (c for c in sched.daily.values() if c not in ("H","W","PL","CO","ADHOC","") ), "—"
            ),
            "working_days": sched.working_days(),
            "total_days":   len(all_dates),
            "leave_days":   len(leaves),
            "comp_off_days":len(comp_offs),
            "holiday_days": counts.get("H", 0),
            "week_off_days":counts.get("W", 0),
            "shift_counts": counts,
            "planned_leaves": leaves,
            "comp_offs":      comp_offs,
            "adhoc_shifts":   adhoc_map,
            "week_off_dates": wo_dates,
            "daily": daily_list,
        })

    # Skill rules summary
    rules_summary = {}
    for skill, rule in skill_rules.items():
        rules_summary[skill] = {
            "count":             rule.get("count", 0),
            "shifts":            rule.get("shifts", []),
            "allocation":        rule.get("allocation", []),
            "rotation_type":     rule.get("rotation_type", "0"),
            "week_off":          rule.get("week_off", "weekends"),
            "strong_conditions": rule.get("strong_conditions", {}),
        }

    # Account holidays
    hol_list = sorted(d.strftime("%Y-%m-%d") for d in cfg.account_holidays)

    # Location holidays
    loc_hols: dict = {}
    for loc, dates in getattr(cfg, "location_holidays", {}).items():
        loc_hols[loc] = sorted(d if isinstance(d, str) else d.strftime("%Y-%m-%d") for d in dates)

    # Coverage summary per skill per day
    coverage_summary = {}
    skill_groups: dict[str, list] = {}
    for sched in schedules:
        sk = sched.employee.skill
        skill_groups.setdefault(sk, []).append(sched)

    for skill, group in skill_groups.items():
        daily_cov = []
        for d in all_dates:
            shift_counts: dict[str, int] = {}
            for sched in group:
                code = sched.daily.get(d, "")
                if code not in ("H","W","PL","CO",""):
                    shift_counts[code] = shift_counts.get(code, 0) + 1
            daily_cov.append({
                "date":   d.strftime("%Y-%m-%d"),
                "day":    d.strftime("%a"),
                "shifts": shift_counts,
                "total_working": sum(shift_counts.values()),
            })
        coverage_summary[skill] = daily_cov

    return {
        "metadata": {
            "generated_at":  __import__("datetime").datetime.now().isoformat(),
            "roster_start":  cfg.roster_start.strftime("%Y-%m-%d"),
            "roster_end":    cfg.roster_end.strftime("%Y-%m-%d"),
            "total_days":    len(all_dates),
            "total_employees": len(schedules),
            "account_holidays": hol_list,
            "location_holidays": loc_hols,
        },
        "skill_rules":      rules_summary,
        "employees":        employees_json,
        "coverage_summary": coverage_summary,
        "validation":       validation,
    }


def _normalise_weekoff(raw: str) -> str:
    """Convert human-readable week-off text to the internal scheduler rule string."""
    l = raw.lower().strip()
    if not l or l in ("na", "none", "static"):
        return "na"
    if re.search(r"sat|sun|weekend", l):
        return "weekends"
    nums = re.findall(r"\d+", l)
    if "rolling" in l:
        if set(nums) >= {"6", "7"}:
            return "rolling6th7th"
        if nums and nums[0] == "7":
            return "rolling7th"
        return "rolling" + "_".join(nums)
    if nums:
        return "every" + "_".join(f"{n}th" for n in nums)
    return "weekends"


def _parse_location_holidays(raw: str, default_year: int, default_month: int) -> dict:
    """
    Parse location-based holiday lines.

    Format (one per line):
        Location – DD-MM-YYYY, DD-MM-YYYY, ...
        Location – DD-MM-YYYY
        Location - DD-MM-YYYY

    Returns {location_name: set(date, ...)}
    """
    result: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on en-dash or plain hyphen that separates location from dates
        # Use the FIRST en-dash or the first ' - ' to split location from dates
        m = re.match(r"^(.+?)\s*[–\-]\s*(.+)$", line)
        if not m:
            continue
        location = m.group(1).strip()
        dates_raw = m.group(2).strip()
        if not location or not dates_raw:
            continue
        dates = _parse_date_list(dates_raw, default_year, default_month)
        if dates:
            result.setdefault(location, set()).update(dates)
    return result


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/skills")
def get_skills():
    roster_file = request.files.get("roster_file")
    if not roster_file or not roster_file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        df = pd.read_excel(roster_file, dtype=str)
        df.columns = [c.strip() for c in df.columns]
        if "Skill" not in df.columns:
            return jsonify({"error": "No 'Skill' column found"}), 400

        counts = df["Skill"].dropna().str.strip().value_counts().reset_index()
        counts.columns = ["name", "count"]
        skills = [
            {"name": row["name"], "count": int(row["count"])}
            for _, row in counts.iterrows()
            if row["name"]
        ]
        return jsonify({"skills": skills})

    except Exception as exc:
        return jsonify({"error": f"Could not read file: {exc}"}), 400


@app.post("/api/generate")
def generate():
    roster_file = request.files.get("roster_file")
    if not roster_file or not roster_file.filename:
        return jsonify({"error": "No roster file uploaded."}), 400
    if not roster_file.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "File must be .xlsx or .xls"}), 400

    start_raw = request.form.get("start_date", "").strip()
    end_raw   = request.form.get("end_date",   "").strip()
    if not start_raw or not end_raw:
        return jsonify({"error": "Start and end dates are required."}), 400

    try:
        roster_start = datetime.strptime(start_raw, "%Y-%m-%d").date()
        roster_end   = datetime.strptime(end_raw,   "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Dates must be YYYY-MM-DD"}), 400

    if roster_end < roster_start:
        return jsonify({"error": "End date must be on or after start date."}), 400

    year  = roster_start.year
    month = roster_start.month

    # Holidays — support multiple months
    holidays_raw     = request.form.get("holidays", "").strip()
    account_holidays: set[date] = set()
    if holidays_raw:
        try:
            for token in re.split(r"[,\s]+", holidays_raw):
                token = token.strip()
                if not token:
                    continue
                d = _parse_date_token(token, year, month)
                if d:
                    account_holidays.add(d)
        except Exception as exc:
            return jsonify({"error": f"Invalid holiday dates: {exc}"}), 400

    # Shift rules
    rules_text = request.form.get("shift_rules", "").strip()
    if not rules_text:
        return jsonify({"error": "Shift rules table is required."}), 400

    skill_rules = _parse_rules_extended(rules_text)
    if not skill_rules:
        return jsonify({"error": "Could not parse skill rules. Check format."}), 400

    # Planned leaves / comp-offs / adhoc
    leaves_raw = request.form.get("planned_leaves", "").strip()
    planned_leaves: dict[str, list] = {}
    if leaves_raw:
        try:
            planned_leaves = _parse_leave_lines(leaves_raw, year, month)
        except Exception as exc:
            return jsonify({"error": f"Invalid leave data: {exc}"}), 400

    # Location-based holidays
    loc_hol_raw = request.form.get("location_holidays", "").strip()
    location_holidays: dict = {}
    if loc_hol_raw:
        try:
            location_holidays = _parse_location_holidays(loc_hol_raw, year, month)
        except Exception as exc:
            return jsonify({"error": f"Invalid location holiday data: {exc}"}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path    = Path(tmpdir)
        input_path  = tmp_path / "Roster - Input.xlsx"
        output_path = tmp_path / "Roster - Out.xlsx"

        roster_file.save(str(input_path))

        try:
            df = pd.read_excel(input_path, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            excel_skills = (
                df["Skill"].dropna().str.strip().unique().tolist()
                if "Skill" in df.columns else []
            )
        except Exception:
            excel_skills = []

        skill_alias = build_skill_alias_map(
            excel_skills=excel_skills,
            rule_skills=list(skill_rules.keys()),
        )

        cfg = AppConfig(
            input_file=input_path,
            output_file=output_path,
            roster_start=roster_start,
            roster_end=roster_end,
            account_holidays=account_holidays,
            planned_leaves=planned_leaves,
            skill_rules=skill_rules,
            skill_alias=skill_alias,
        )
        cfg.location_holidays = {
            loc: set(dates) for loc, dates in location_holidays.items()
        }

        try:
            employees   = load_employees(cfg)
            scheduler   = ShiftScheduler(cfg, employees)
            schedules   = scheduler.run()
            write_workbook(cfg, schedules)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": f"Generation failed: {exc}"}), 500

        # ── Run coverage validation ──────────────────────────────────────
        all_roster_dates = list(iter_dates(roster_start, roster_end))
        validation_result = validate_coverage(
            schedules=schedules,
            skill_rules=skill_rules,
            all_dates=all_roster_dates,
            skill_alias=skill_alias,
        )

        # ── Build JSON output ────────────────────────────────────────────
        json_data = _build_roster_json(
            schedules=schedules,
            cfg=cfg,
            skill_rules=skill_rules,
            validation=validation_result,
        )
        json_path = tmp_path / "Roster-Out.json"
        import json as _json
        json_path.write_text(_json.dumps(json_data, indent=2, default=str), encoding="utf-8")

        # ── Package both files as zip ────────────────────────────────────
        import zipfile
        s = roster_start.strftime("%d-%b-%Y")
        e = roster_end.strftime("%d-%b-%Y")
        zip_path = tmp_path / f"Roster-Out_{s}_to_{e}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(output_path, f"Roster-Out_{s}_to_{e}.xlsx")
            zf.write(json_path,   f"Roster-Out_{s}_to_{e}.json")

        out_bytes = io.BytesIO(zip_path.read_bytes())
        out_bytes.seek(0)
        return send_file(
            out_bytes,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"Roster-Out_{s}_to_{e}.zip",
        )


def _start_keep_alive(url: str, interval: int = 840) -> None:
    """
    Ping the app's /api/health endpoint every 14 minutes (840 seconds).
    This prevents Render's free tier from spinning the dyno down after
    15 minutes of inactivity — keeping the app always active.
    """
    def _ping():
        while True:
            time.sleep(interval)
            try:
                urllib.request.urlopen(url, timeout=10)
                print(f"[keep-alive] pinged {url} — app is active")
            except Exception as exc:
                print(f"[keep-alive] ping failed: {exc}")

    t = threading.Thread(target=_ping, daemon=True)
    t.start()
    print(f"[keep-alive] started — pinging {url} every {interval}s")


def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    print(f"\n  ShiftScheduler Web UI  →  http://127.0.0.1:{port}\n")

    # Start keep-alive pinger when running on Render (RENDER env var is set)
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        _start_keep_alive(f"{render_url}/api/health")

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run_server(debug=True)
