"""
web/app.py
----------
Flask web application for the Shift Roster Generator.

Endpoints:
    GET  /                   → main UI
    POST /api/skills         → read skill names from uploaded xlsx
    POST /api/generate       → multipart: xlsx + form fields → streams xlsx back
    GET  /api/health         → health check
"""

from __future__ import annotations

import io
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
    _parse_leave_block,
    build_skill_alias_map,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR   = Path(__file__).parent / "static"

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR),
)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


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
    Accept a multipart upload of the roster xlsx and return the unique
    skill names found in the 'Skill' column.

    Response:
        { "skills": ["Monitoring", "Azure + Windows", ...] }
    """
    roster_file = request.files.get("roster_file")
    if not roster_file or not roster_file.filename:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        df = pd.read_excel(roster_file, dtype=str)
        df.columns = [c.strip() for c in df.columns]

        if "Skill" not in df.columns:
            return jsonify({"error": "No 'Skill' column found in the file"}), 400

        skills = (
            df["Skill"]
            .dropna()
            .str.strip()
            .unique()
            .tolist()
        )
        # Sort alphabetically for consistent display
        skills = sorted(set(s for s in skills if s))
        return jsonify({"skills": skills})

    except Exception as exc:
        return jsonify({"error": f"Could not read file: {exc}"}), 400


@app.post("/api/generate")
def generate():
    """
    Accept multipart/form-data:
        roster_file    : .xlsx upload  (required)
        start_date     : YYYY-MM-DD
        end_date       : YYYY-MM-DD
        holidays       : "05,09,16"   (comma-separated day numbers)
        shift_rules    : pipe-delimited table text
        planned_leaves : "Name – Planned Leave: dd, dd" (one per line)

    Returns Roster-Out.xlsx as a file download, or JSON error on failure.
    """

    # ── 1. Input file ──────────────────────────────────────────────────────────
    roster_file = request.files.get("roster_file")
    if not roster_file or not roster_file.filename:
        return jsonify({"error": "No roster file uploaded."}), 400
    if not roster_file.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "Uploaded file must be .xlsx or .xls"}), 400

    # ── 2. Dates ───────────────────────────────────────────────────────────────
    start_raw = request.form.get("start_date", "").strip()
    end_raw   = request.form.get("end_date",   "").strip()

    if not start_raw or not end_raw:
        return jsonify({"error": "Start date and end date are required."}), 400

    try:
        roster_start = datetime.strptime(start_raw, "%Y-%m-%d").date()
        roster_end   = datetime.strptime(end_raw,   "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Dates must be in YYYY-MM-DD format."}), 400

    if roster_end < roster_start:
        return jsonify({"error": "End date must be on or after start date."}), 400
    if (roster_end - roster_start).days > 365:
        return jsonify({"error": "Roster period cannot exceed 366 days."}), 400

    year  = roster_start.year
    month = roster_start.month

    # ── 3. Holidays ────────────────────────────────────────────────────────────
    holidays_raw = request.form.get("holidays", "").strip()
    account_holidays: set[date] = set()
    if holidays_raw:
        try:
            account_holidays = set(_parse_day_list(holidays_raw, year, month))
        except Exception as exc:
            return jsonify({"error": f"Invalid holiday dates: {exc}"}), 400

    # ── 4. Shift rules — parsed entirely from user input, no hardcoded fallback ─
    rules_text = request.form.get("shift_rules", "").strip()
    if not rules_text:
        return jsonify({"error": "Shift rules table is required."}), 400

    skill_rules, _ = _parse_shift_rules_table(rules_text)

    if not skill_rules:
        return jsonify({
            "error": (
                "Could not parse any skill rules from the table. "
                "Please check the format: | Skill | Count | Shift | Week Off |"
            )
        }), 400

    # ── 5. Planned leaves ──────────────────────────────────────────────────────
    leaves_raw = request.form.get("planned_leaves", "").strip()
    planned_leaves: dict[str, list[date]] = {}
    if leaves_raw:
        try:
            for line in leaves_raw.splitlines():
                if line.strip():
                    partial = _parse_leave_block(line, year, month)
                    planned_leaves.update(partial)
        except Exception as exc:
            return jsonify({"error": f"Invalid leave data: {exc}"}), 400

    # ── 6. Save upload, read Excel skills, build dynamic alias map ─────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path    = Path(tmpdir)
        input_path  = tmp_path / "Roster - Input.xlsx"
        output_path = tmp_path / "Roster - Out.xlsx"

        roster_file.save(str(input_path))

        # Read skill names directly from the Excel file
        try:
            df = pd.read_excel(input_path, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            excel_skills: list[str] = (
                df["Skill"].dropna().str.strip().unique().tolist()
                if "Skill" in df.columns else []
            )
        except Exception:
            excel_skills = []

        # Build alias map purely from Excel skills ↔ user-typed rule skills
        # No hardcoded defaults — fully dynamic
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

        # ── 7. Generate ────────────────────────────────────────────────────────
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

        # ── 8. Stream the file back ────────────────────────────────────────────
        out_bytes = io.BytesIO(output_path.read_bytes())
        out_bytes.seek(0)

        period_label  = (
            f"{roster_start.strftime('%d-%b-%Y')}"
            f"_to_{roster_end.strftime('%d-%b-%Y')}"
        )
        download_name = f"Roster-Out_{period_label}.xlsx"

        return send_file(
            out_bytes,
            mimetype=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            as_attachment=True,
            download_name=download_name,
        )


# ── Entry point ────────────────────────────────────────────────────────────────

def run_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False) -> None:
    print(f"\n  ShiftScheduler Web UI")
    print(f"  ─────────────────────────────────────")
    print(f"  Local  : http://127.0.0.1:{port}")
    print(f"  Network: http://{host}:{port}")
    print(f"  ─────────────────────────────────────\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    run_server(debug=True)
