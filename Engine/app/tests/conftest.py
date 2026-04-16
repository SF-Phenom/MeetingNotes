"""Pytest configuration for MeetingNotes tests.

Ensures the `app` package is importable regardless of where pytest is invoked
from, and isolates tests from the user's real ~/MeetingNotes_RT installation
by pointing MEETINGNOTES_HOME at a tmp directory before any app module is
imported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# Engine/ is the parent of app/, so tests resolve `app.*` imports correctly.
_ENGINE_DIR = Path(__file__).resolve().parents[2]
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))


# Point MEETINGNOTES_HOME at a throwaway directory so state.py / corrections.py
# can't read or write the real user install during tests. Individual tests that
# need specific filesystem layout should use tmp_path and monkeypatch further.
os.environ.setdefault(
    "MEETINGNOTES_HOME",
    str(_ENGINE_DIR / "app" / "tests" / "_tmp_home"),
)


FIXTURES_DIR = _ENGINE_DIR / "app" / "tests" / "fixtures"
BENCHMARK_WAV = FIXTURES_DIR / "benchmark.wav"
BENCHMARK_SYS_WAV = FIXTURES_DIR / "benchmark.sys.wav"
BENCHMARK_META = FIXTURES_DIR / "benchmark.yaml"
