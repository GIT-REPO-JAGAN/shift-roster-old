"""
web/app.py
----------
Flask web application for the Shift Roster Generator.

Endpoints:
    GET  /                → main UI
    POST /api/skills      → read skill names + counts from uploaded xlsx
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
from roster.prompt import (
    _parse_shift_rules_table,
    _parse_day_list,
    build_skill_alias_map,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_leave_lines(raw: str, year: int, month: int) -> dict[str, list]:
    """
    Parse planned leave and comp-off entries.

    Formats:
        Name – Planned Leave: 05, 08
        Name – PL: 05, 08
        Name – Comp Off: 05, 08
        Name – CO: 05, 08
    """
    result: dict[str, list] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on – or : to separate name from the rest
        parts = re.split(r"\s*[–\-]\s*", line, maxsplit=1)
        if len(parts) < 2:
            continue
        name      = parts[0].strip()
        remainder = parts[1].strip()

        # Detect type
        leave_type = "PL"
        if re.match(r"(comp\s*off|comp-off|co)\s*[:\-]", remainder, re.I):
            leave_type = "CO"
            remainder  = re.sub(r"^(comp\s*off|comp-off|co)\s*[:\-]\s*", "", remainder, flags=re.I)
        elif re.match(r"(planned\s*leave|pl)\s*[:\-]", remainder, re.I):
            leave_type = "PL"
            remainder  = re.sub(r"^(planned\s*leave|pl)\s*[:\-]\s*", "", remainder, flags=re.I)

        # Parse day numbers
        days = [d.strip() for d in re.split(r"[,\s]+", remainder) if re.match(r"^\d{1,2}$", d.strip())]
        if not days:
            continue

        entries = result.setdefault(name, [])
        for day in days:
            try:
                entries.append({"type": leave_type, "date": date(year, month, int(day))})
            except ValueError:
                pass

    return result


def _parse_rules_extended(rules_text: str) -> dict:
    """
    Parse the extended shift rules table:
    | Skill | Count | Shift Allocation | Rotation | Week Off |

    Returns skill_rules dict compatible with ShiftScheduler.
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

        # Skip header rows that slipped through (e.g. "Skill Category | Count | ...")
        if re.search(r"skill.{0,5}cat|count|allocation|rotation|week.{0,5}off", skill_raw, re.I):
            continue

        count = int(re.search(r"\d+", count_raw).group()) if re.search(r"\d+", count_raw) else 1

        # Skip zero-count rows (header artefacts, separators)
        if count == 0:
            continue

        # Parse shift allocation: "2 in M, 2 in A, 2 in N" or "G" or "E"
        # G = General shift code — stays as G, no M/A/N expansion
        shifts:     list[str] = []
        allocation: list[int] = []
        is_general = False   # True when the entire skill uses G code

        alloc_upper = alloc_raw.upper().strip()

        # Standalone G (or "G only") → General shift, static, no rotation
        if re.match(r"^G(\s+ONLY)?$", alloc_upper):
            shifts     = ["G"]
            allocation = []
            is_general = True

        # Standalone E (or "E only") → Evening only
        elif re.match(r"^E(\s+ONLY)?$", alloc_upper):
            shifts     = ["E"]
            allocation = []

        # Pattern: "2 in M, 2 in A, 2 in N" or "1 in M, 1 in N"
        else:
            slot_matches = re.findall(r"(\d+)\s+in\s+([MANEG])", alloc_raw, re.I)
            if slot_matches:
                for cnt, shift in slot_matches:
                    s = shift.strip().upper()
                    # G inside a slot list means General for that slot
                    allocation.append(int(cnt))
                    shifts.append(s)
            else:
                # Free-form tokens: M / A / N
                for tok in re.split(r"[\s/,]+", alloc_upper):
                    tok = tok.strip()
                    if tok in ("M", "A", "N", "E") and tok not in shifts:
                        shifts.append(tok)
                allocation = []

        if not shifts:
            shifts = ["M", "A", "N"]

        # Force rotation_weeks=0 for General / Evening / static skills
        # (will be overridden below if user specified a rotation)
        _force_static = is_general or shifts == ["E"]

        # Parse rotation weeks
        # G and E shifts are always static (no rotation)
        rotation_weeks = 0
        if not _force_static:
            rot_match = re.search(r"(\d+)\s*-?\s*week", rotation_raw, re.I)
            if rot_match:
                rotation_weeks = int(rot_match.group(1))

        # Parse week-off
        wo_lower = weekoff_raw.lower()
        if re.search(r"sat|sun|weekend", wo_lower):
            week_off = "weekends"
        else:
            # Extract numbers from "Rolling (6th & 7th)" or "Every 5th & 6th"
            nums = re.findall(r"\d+", wo_lower)
            if nums:
                if "rolling" in wo_lower:
                    week_off = "rolling" + "_".join(f"{n}th" for n in nums)
                else:
                    week_off = "every" + "_".join(f"{n}th" for n in nums)
            else:
                week_off = "weekends"

        skill_rules[skill_raw] = {
            "count":          count,
            "shifts":         shifts,
            "allocation":     allocation,
            "rotation_weeks": rotation_weeks,
            "week_off":       week_off,
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
    """
    Returns skill names and counts from the uploaded xlsx.
    Response: { "skills": [{"name": "Monitoring", "count": 5}, ...] }
    """
    roster_file = request.files.get("roster_file")
    if not roster_file or not roster_file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        df = pd.read_excel(roster_file, dtype=str)
        df.columns = [c.strip() for c in df.columns]
        if "Skill" not in df.columns:
            return jsonify({"error": "No 'Skill' column found"}), 400

        counts = (
            df["Skill"].dropna().str.strip()
            .value_counts()
            .reset_index()
        )
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

    # Holidays
    holidays_raw     = request.form.get("holidays", "").strip()
    account_holidays: set[date] = set()
    if holidays_raw:
        try:
            account_holidays = set(_parse_day_list(holidays_raw, year, month))
        except Exception as exc:
            return jsonify({"error": f"Invalid holiday dates: {exc}"}), 400

    # Shift rules (extended 5-column format)
    rules_text = request.form.get("shift_rules", "").strip()
    if not rules_text:
        return jsonify({"error": "Shift rules table is required."}), 400

    skill_rules = _parse_rules_extended(rules_text)
    if not skill_rules:
        return jsonify({"error": "Could not parse skill rules. Check format."}), 400

    # Planned leaves & comp-offs
    leaves_raw     = request.form.get("planned_leaves", "").strip()
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
        period_label  = f"{roster_start.strftime('%d-%b-%Y')}_to_{roster_end.strftime('%d-%b-%Y')}"
        download_name = f"Roster-Out_{period_label}.xlsx"

        return send_file(
            out_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=download_name,
        )


def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    print(f"\n  ShiftScheduler Web UI  →  http://127.0.0.1:{port}\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run_server(debug=True)
