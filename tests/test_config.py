"""
tests/test_config.py
--------------------
Tests for the configuration loader.
"""

from __future__ import annotations

import os
import textwrap
from datetime import date
from pathlib import Path

import pytest

from roster.config import load, DEFAULT_SKILL_RULES


class TestConfigDefaults:
    def test_default_period(self, tmp_path):
        cfg = load(tmp_path / "nonexistent.yaml")
        assert cfg.roster_start == date(2026, 6, 1)
        assert cfg.roster_end   == date(2026, 6, 30)

    def test_default_skill_rules_loaded(self, tmp_path):
        cfg = load(tmp_path / "nonexistent.yaml")
        assert "Monitoring" in cfg.skill_rules


class TestConfigFromYaml:
    def test_yaml_holidays(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            roster_start: "2026-07-01"
            roster_end:   "2026-07-31"
            account_holidays:
              - "2026-07-04"
              - "2026-07-14"
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load(cfg_file)
        assert date(2026, 7, 4) in cfg.account_holidays
        assert date(2026, 7, 14) in cfg.account_holidays

    def test_yaml_planned_leaves(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            planned_leaves:
              Alice:
                - "2026-06-10"
                - "2026-06-11"
        """)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml_content)
        cfg = load(cfg_file)
        assert date(2026, 6, 10) in cfg.planned_leaves["Alice"]


class TestConfigEnvOverride:
    def test_env_input_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROSTER_INPUT", str(tmp_path / "custom_input.xlsx"))
        cfg = load(tmp_path / "nonexistent.yaml")
        assert cfg.input_file == tmp_path / "custom_input.xlsx"

    def test_env_output_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROSTER_OUTPUT", str(tmp_path / "custom_output.xlsx"))
        cfg = load(tmp_path / "nonexistent.yaml")
        assert cfg.output_file == tmp_path / "custom_output.xlsx"
