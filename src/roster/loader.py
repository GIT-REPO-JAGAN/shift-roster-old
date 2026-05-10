"""
loader.py
---------
Read and validate the employee input spreadsheet.
"""

from __future__ import annotations

import pandas as pd
from pathlib import Path
from dataclasses import dataclass

from .config import AppConfig


REQUIRED_COLUMNS = {"Name", "Email", "Skill"}


@dataclass
class Employee:
    name: str
    email: str
    skill: str
    location: str


def load_employees(cfg: AppConfig) -> list[Employee]:
    """Read Roster - Input.xlsx and return a list of Employee objects."""
    path: Path = cfg.input_file
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    df = pd.read_excel(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.dropna(subset=["Name"])
    df = df.fillna("")

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Input file is missing columns: {missing}")

    # Normalise skill names via alias map
    df["Skill"] = (
        df["Skill"]
        .str.strip()
        .map(cfg.skill_alias)
        .fillna(df["Skill"].str.strip())
    )

    employees: list[Employee] = []
    for _, row in df.iterrows():
        employees.append(
            Employee(
                name=row["Name"].strip(),
                email=row["Email"].strip(),
                skill=row["Skill"].strip(),
                location=row.get("Location", "").strip(),
            )
        )

    return employees
