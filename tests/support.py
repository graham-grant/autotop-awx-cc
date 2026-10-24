"""Shared test setup: puts scripts/ on sys.path and points at fixtures."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FACTS = FIXTURES / "facts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
