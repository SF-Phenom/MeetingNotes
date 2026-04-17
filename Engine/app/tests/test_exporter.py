"""Tests for the action-item exporter dispatcher."""
from __future__ import annotations

import pytest

from app import state as state_mod
from app import exporter
from app.exporter import (
    BACKEND_APPLE_REMINDERS,
    BACKEND_DISABLED,
    ExportResult,
    SUPPORTED_BACKENDS,
    export_action_items,
    get_backend_preference,
    is_backend_available,
    set_backend_preference,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Clean state per test — never touches the user's real state.json."""
    store = {**state_mod.DEFAULT_STATE}
    monkeypatch.setattr(state_mod, "load", lambda: dict(store))
    monkeypatch.setattr(
        state_mod, "update",
        lambda **kw: store.update(kw) or dict(store),
    )
    return store


class TestPreference:
    def test_default_is_disabled(self):
        assert get_backend_preference() == BACKEND_DISABLED

    def test_set_persists(self):
        set_backend_preference(BACKEND_APPLE_REMINDERS)
        assert get_backend_preference() == BACKEND_APPLE_REMINDERS

    def test_set_normalizes_case_and_whitespace(self):
        set_backend_preference("  Apple_Reminders  ")
        assert get_backend_preference() == BACKEND_APPLE_REMINDERS

    def test_set_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            set_backend_preference("things3")
        assert "things3" in str(excinfo.value)


class TestAvailability:
    def test_known_backends_available(self):
        for name in SUPPORTED_BACKENDS:
            assert is_backend_available(name) is True

    def test_unknown_unavailable(self):
        assert is_backend_available("notion") is False


class TestDispatchDisabled:
    def test_disabled_returns_noop_result(self, _isolate_state):
        result = export_action_items([{"item": "do thing"}])
        assert isinstance(result, ExportResult)
        assert result.backend == BACKEND_DISABLED
        assert result.exported_count == 0
        assert result.attempted is False
        assert result.ok is True

    def test_disabled_doesnt_invoke_backend(self, monkeypatch, _isolate_state):
        from app.exporters import apple_reminders
        called = []
        monkeypatch.setattr(
            apple_reminders, "add_items",
            lambda *a, **kw: called.append((a, kw)) or (1, []),
        )
        export_action_items([{"item": "x"}])
        assert called == []


class TestDispatchAppleReminders:
    @pytest.fixture(autouse=True)
    def _set_backend(self, _isolate_state, monkeypatch):
        _isolate_state["exporter_backend"] = BACKEND_APPLE_REMINDERS

    def test_empty_items_skips_backend(self, monkeypatch):
        from app.exporters import apple_reminders
        called = []
        monkeypatch.setattr(
            apple_reminders, "add_items",
            lambda *a, **kw: called.append(kw) or (0, []),
        )
        result = export_action_items([])
        assert result.exported_count == 0
        assert called == []
        assert result.attempted is True  # backend is configured, just nothing to send

    def test_passes_list_name_and_source_label(self, monkeypatch):
        from app.exporters import apple_reminders
        captured = {}

        def fake_add(items, list_name, source_label):
            captured["items"] = items
            captured["list_name"] = list_name
            captured["source_label"] = source_label
            return len(items), []

        monkeypatch.setattr(apple_reminders, "add_items", fake_add)
        items = [{"item": "ship it", "owner": "alice", "due": None}]
        result = export_action_items(items, metadata={
            "title": "Q2 Planning", "source": "zoom", "date_str": "2026-04-16",
        })
        assert captured["items"] == items
        # Default list name from state.
        assert captured["list_name"] == "MeetingNotes"
        # Title preferred over source/date in label.
        assert captured["source_label"] == "Q2 Planning"
        assert result.exported_count == 1
        assert result.ok

    def test_label_falls_back_to_source_date(self, monkeypatch):
        from app.exporters import apple_reminders
        captured = {}

        def fake_add(items, list_name, source_label):
            captured["source_label"] = source_label
            return len(items), []

        monkeypatch.setattr(apple_reminders, "add_items", fake_add)
        export_action_items(
            [{"item": "x"}],
            metadata={"source": "google_meet", "date_str": "2026-04-16"},
        )
        assert "google_meet" in captured["source_label"]
        assert "2026-04-16" in captured["source_label"]

    def test_backend_errors_propagate(self, monkeypatch):
        from app.exporters import apple_reminders
        monkeypatch.setattr(
            apple_reminders, "add_items",
            lambda *a, **kw: (0, ["osascript exploded"]),
        )
        result = export_action_items([{"item": "x"}])
        assert result.exported_count == 0
        assert result.errors == ["osascript exploded"]
        assert result.ok is False

    def test_custom_list_name_from_state(self, monkeypatch, _isolate_state):
        _isolate_state["apple_reminders_list"] = "Work"
        from app.exporters import apple_reminders
        captured = {}
        monkeypatch.setattr(
            apple_reminders, "add_items",
            lambda items, list_name, source_label:
                captured.update({"list_name": list_name}) or (len(items), []),
        )
        export_action_items([{"item": "x"}])
        assert captured["list_name"] == "Work"


class TestUnknownBackend:
    def test_unknown_backend_in_state_returns_error(self, _isolate_state):
        # Bypass set_backend_preference (which validates) by writing state directly.
        _isolate_state["exporter_backend"] = "things3"
        result = export_action_items([{"item": "x"}])
        assert result.errors and "things3" in result.errors[0]
        assert result.exported_count == 0
