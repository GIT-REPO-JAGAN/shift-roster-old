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
import re
import tempfile
import traceback
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

import sys
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from roster.config import AppConfig
from roster.loader import load_employees
from roster.scheduler import ShiftScheduler
from roster.writer import write_workbook
from roster.prompt import _parse_day_list, build_skill_alias_map

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ── Leave parser ───────────────────────────────────────────────────────────────

def _parse_leave_lines(raw: str, year: int, month: int) -> dict[str, list]:
    """
    Parse planned leave, comp-off, and adhoc shift entries.

    Supported formats (all case-insensitive):
        Name – PL: 05, 08
        Name – Planned Leave: 05, 08
        Name – CO: 05, 08
        Name – COFF: 05, 08
        Name – Comp Off: 05, 08
        Name – Adhoc Shift: 05 | G
        Name – Adhoc Shift: 05 | M
        Name – Adhoc Shift: 06 to 09 | N
        Name – Adhoc Shift: 06-09 | A
    """
    result: dict[str, list] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue

        # Split name from the rest on – or -
        parts = re.split(r"\s*[–\-]\s*", line, maxsplit=1)
        if len(parts) < 2:
            continue

        name      = parts[0].strip()
        remainder = parts[1].strip()
        if not name:
            continue

        # ── Adhoc Shift: 05 | G  or  06 to 09 | M ────────────────────────────
        if re.match(r"adhoc\s*shift\s*[:\-]", remainder, re.I):
            body = re.sub(r"^adhoc\s*shift\s*[:\-]\s*", "", remainder, flags=re.I).strip()
            # Split on | to get dates and shift code
            if "|" in body:
                date_part, shift_part = body.split("|", 1)
                shift_code = shift_part.strip().upper()
                if shift_code not in ("M", "A", "N", "E", "G"):
                    shift_code = "G"
            else:
                date_part  = body
                shift_code = "G"

            date_part = date_part.strip()
            entries   = result.setdefault(name, [])

            # Handle "06 to 09" or "06-09" range
            range_match = re.match(r"(\d{1,2})\s*(?:to|-)\s*(\d{1,2})", date_part)
            if range_match:
                d1, d2 = int(range_match.group(1)), int(range_match.group(2))
                for day in range(d1, d2 + 1):
                    try:
                        entries.append({
                            "type":  "ADHOC",
                            "shift": shift_code,
                            "date":  date(year, month, day),
                        })
                    except ValueError:
                        pass
            else:
                days = [d.strip() for d in re.split(r"[,\s]+", date_part)
                        if re.match(r"^\d{1,2}$", d.strip())]
                for d in days:
                    try:
                        entries.append({
                            "type":  "ADHOC",
                            "shift": shift_code,
                            "date":  date(year, month, int(d)),
                        })
                    except ValueError:
                        pass
            continue

        # ── CO / COFF / Comp Off ───────────────────────────────────────────────
        if re.match(r"(comp\s*off|co(?:ff)?)\s*[:\-]", remainder, re.I):
            leave_type = "CO"
            remainder  = re.sub(r"^(comp\s*off|co(?:ff)?)\s*[:\-]\s*", "", remainder, flags=re.I)

        # ── PL / Planned Leave ────────────────────────────────────────────────
        elif re.match(r"(planned\s*leave|pl)\s*[:\-]", remainder, re.I):
            leave_type = "PL"
            remainder  = re.sub(r"^(planned\s*leave|pl)\s*[:\-]\s*", "", remainder, flags=re.I)

        else:
            leave_type = "PL"  # default

        days = [d.strip() for d in re.split(r"[,\s]+", remainder.strip())
                if re.match(r"^\d{1,2}$", d.strip())]
        if not days:
            continue

        entries = result.setdefault(name, [])
        for d in days:
            try:
                entries.append({"type": leave_type, "date": date(year, month, int(d))})
            except ValueError:
                pass

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
            slot_matches = re.findall(r"(\d+)\s+in\s+([MANE])", alloc_raw, re.I)
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

        skill_rules[skill_raw] = {
            "count":         count,
            "shifts":        shifts,
            "allocation":    allocation,
            "rotation_type": rotation_type,
            "week_off":      week_off,
        }

    return skill_rules


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
            # Try to parse as full dates first, then as day numbers for roster_start month
            for token in re.split(r"[,\s]+", holidays_raw):
                token = token.strip()
                if not token:
                    continue
                if re.match(r"^\d{4}-\d{2}-\d{2}$", token):
                    account_holidays.add(datetime.strptime(token, "%Y-%m-%d").date())
                elif re.match(r"^\d{1,2}$", token):
                    account_holidays.add(date(year, month, int(token)))
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

        out_bytes = io.BytesIO(output_path.read_bytes())
        out_bytes.seek(0)
        s = roster_start.strftime("%d-%b-%Y")
        e = roster_end.strftime("%d-%b-%Y")
        return send_file(
            out_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"Roster-Out_{s}_to_{e}.xlsx",
        )


def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    print(f"\n  ShiftScheduler Web UI  →  http://127.0.0.1:{port}\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run_server(debug=True)
