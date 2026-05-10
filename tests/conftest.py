"""Shared pytest fixtures."""
import sys
from pathlib import Path

# Ensure src/ is on the path when running tests directly
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
