"""Tests for state.py — typed State accessor + dict/typed parity."""
from __future__ import annotations

import json
from dataclasses import fields

import pytest

from app import state as state_mod
from app.state import DEFAULT_STATE, State


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Redirect state.py's STATE_PATH at a throwaway file."""
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", str(path))
    return path


class TestStateFromRaw:
    def test_empty_raw_yields_defaults(self):
        s = State.from_raw({})
        for f in fields(State):
            want = f.default_factory() if callable(f.default_factory) else f.default  # type: ignore[misc]
            assert getattr(s, f.name) == want

    def test_known_fields_applied(self):
        s = State.from_raw({
            "recording_active": True,
            "active_recording_path": "/tmp/foo.wav",
            "transcripts_since_checkin": 7,
        })
        assert s.recording_active is True
        assert s.active_recording_path == "/tmp/foo.wav"
        assert s.transcripts_since_checkin == 7
        # Unset fields fall back to defaults:
        assert s.suppressed_sources == []

    def test_unknown_fields_silently_ignored(self):
        """Forward-compat: a state.json written by a newer version with extra
        keys must still load cleanly in the older version."""
        s = State.from_raw({
            "recording_active": True,
            "future_feature_flag": "abc",
            "some_new_list": [1, 2, 3],
        })
        assert s.recording_active is True
        assert not hasattr(s, "future_feature_flag")

    def test_is_frozen(self):
        s = State.from_raw({})
        with pytest.raises(Exception):
            s.recording_active = True  # type: ignore[misc]


class TestStateLoad:
    def test_missing_file_returns_defaults(self, state_file):
        # state_file fixture points STATE_PATH at a path that doesn't exist.
        assert not state_file.exists()
        s = State.load()
        assert s.recording_active is False
        assert s.transcripts_since_checkin == 0

    def test_loads_from_disk(self, state_file):
        state_file.write_text(json.dumps({
            "recording_active": True,
            "active_call_source": "zoom",
            "suppressed_sources": ["teams"],
        }))
        s = State.load()
        assert s.recording_active is True
        assert s.active_call_source == "zoom"
        assert s.suppressed_sources == ["teams"]

    def test_corrupt_file_yields_defaults(self, state_file):
        state_file.write_text("{not valid json")
        s = State.load()
        assert s.recording_active is False


class TestDefaultStateParity:
    """DEFAULT_STATE (dict) must match State (dataclass) field defaults.

    Both exist for back-compat reasons; keeping them in sync is a
    hand-maintained invariant, so an explicit test catches drift.
    """

    def test_keys_match(self):
        dc_names = {f.name for f in fields(State)}
        assert dc_names == set(DEFAULT_STATE)

    def test_values_match(self):
        for f in fields(State):
            if f.default_factory is not None and callable(f.default_factory):
                expected = f.default_factory()
            else:
                expected = f.default
            assert DEFAULT_STATE[f.name] == expected, (
                f"DEFAULT_STATE[{f.name!r}] drifted from State dataclass default"
            )


class TestDictAndTypedWriteParity:
    """update(**kwargs) and State.load() must see the same data."""

    def test_update_then_typed_load(self, state_file):
        state_mod.update(recording_active=True, active_call_source="zoom")
        s = State.load()
        assert s.recording_active is True
        assert s.active_call_source == "zoom"

    def test_typed_load_matches_dict_load(self, state_file):
        state_mod.update(
            recording_active=True,
            transcripts_since_checkin=4,
            suppressed_sources=["foo"],
        )
        raw = state_mod.load()
        s = State.load()
        for f in fields(State):
            assert raw[f.name] == getattr(s, f.name)
