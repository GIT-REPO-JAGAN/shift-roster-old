"""
config.py
---------
Centralised configuration loader.
Values come from (in priority order):
  1. CLI arguments  (handled in __main__.py)
  2. Environment variables
  3. config.yaml   (project root)
  4. Hard-coded defaults below
"""

from __future__ import annotations

import os
import yaml
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path.cwd()   # repo root — always run from the project directory


# ─── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_SKILL_RULES: dict = {
    "Monitoring":              {"count": 2, "shifts": ["M", "A", "N"], "week_off": "every5th"},
    "SRE Azure + Windows":     {"count": 1, "shifts": ["E"],           "week_off": "weekends"},
    "Azure + Windows":         {"count": 2, "shifts": ["M", "A", "N"], "week_off": "weekends"},
    "Azure + Windows SME":     {"count": 1, "shifts": ["M", "A", "N"], "week_off": "weekends"},
    "OCI Azure + Windows":     {"count": 1, "shifts": ["M", "A", "N"], "week_off": "weekends"},
    "SRE OCI Azure + Linux":   {"count": 1, "shifts": ["M", "A", "N"], "week_off": "weekends"},
    "OCI Azure + Linux":       {"count": 2, "shifts": ["M", "A", "N"], "week_off": "every5th"},
    "OCI Azure + Linux SME":   {"count": 1, "shifts": ["M", "A", "N"], "week_off": "weekends"},
    "OCI Azure + Network AKS": {"count": 1, "shifts": ["M", "A", "N"], "week_off": "every5th"},
}

DEFAULT_SKILL_ALIAS: dict = {
    "Monitoring":              "Monitoring",
    "AZURE SRE +Windows":      "SRE Azure + Windows",
    "Azure + Windows":         "Azure + Windows",
    "SME: Azure + Windows":    "Azure + Windows SME",
    "SME:Windows + AZURE":     "Azure + Windows SME",
    "OCI + AZURE Windows":     "OCI Azure + Windows",
    "SRE: AZURE/OCI + Linux":  "SRE OCI Azure + Linux",
    "OCI+Azure+inux Admin L2": "OCI Azure + Linux",
    "SME: Linux +OCI + Azure": "OCI Azure + Linux SME",
    "Azure+OCI Network, AKS":  "OCI Azure + Network AKS",
}


# ─── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class AppConfig:
    input_file: Path
    output_file: Path
    roster_start: date
    roster_end: date
    account_holidays: set[date]
    planned_leaves: dict[str, list[date]]
    skill_rules: dict
    skill_alias: dict

    @property
    def month_label(self) -> str:
        return self.roster_start.strftime("%B %Y")


# ─── Loader ────────────────────────────────────────────────────────────────────

def _parse_date(val) -> date:
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    return datetime.strptime(str(val), "%Y-%m-%d").date()


def load(config_path: Path | None = None) -> AppConfig:
    """
    Load config from YAML file, then overlay with environment variables.
    config_path defaults to <repo-root>/config.yaml
    """
    cfg_file = config_path or (ROOT / "config.yaml")
    raw: dict = {}
    if cfg_file.exists():
        with cfg_file.open() as f:
            raw = yaml.safe_load(f) or {}

    # ── Paths ──────────────────────────────────────────────────────────────────
    input_file = Path(
        os.environ.get("ROSTER_INPUT", raw.get("input_file", "data/input/Roster - Input.xlsx"))
    )
    output_file = Path(
        os.environ.get("ROSTER_OUTPUT", raw.get("output_file", "data/output/Roster - Out.xlsx"))
    )
    if not input_file.is_absolute():
        input_file = ROOT / input_file
    if not output_file.is_absolute():
        output_file = ROOT / output_file

    # ── Roster period ──────────────────────────────────────────────────────────
    roster_start = _parse_date(
        os.environ.get("ROSTER_START", raw.get("roster_start", "2026-06-01"))
    )
    roster_end = _parse_date(
        os.environ.get("ROSTER_END", raw.get("roster_end", "2026-06-30"))
    )

    # ── Holidays ───────────────────────────────────────────────────────────────
    raw_holidays = raw.get("account_holidays", [])
    if isinstance(raw_holidays, str):
        raw_holidays = [d.strip() for d in raw_holidays.split(",")]
    account_holidays: set[date] = {_parse_date(d) for d in raw_holidays}

    # ── Planned leaves ────────────────────────────────────────────────────────
    raw_leaves: dict = raw.get("planned_leaves", {})
    planned_leaves: dict[str, list[date]] = {
        name: [_parse_date(d) for d in dates]
        for name, dates in raw_leaves.items()
    }

    # ── Skill rules / alias ───────────────────────────────────────────────────
    skill_rules = raw.get("skill_rules", DEFAULT_SKILL_RULES)
    skill_alias = raw.get("skill_alias", DEFAULT_SKILL_ALIAS)

    return AppConfig(
        input_file=input_file,
        output_file=output_file,
        roster_start=roster_start,
        roster_end=roster_end,
        account_holidays=account_holidays,
        planned_leaves=planned_leaves,
        skill_rules=skill_rules,
        skill_alias=skill_alias,
    )
