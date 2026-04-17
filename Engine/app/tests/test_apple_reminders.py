"""Tests for the Apple Reminders exporter backend.

Mocks subprocess.run so the suite stays hermetic — never invokes
osascript or talks to Reminders.app. Most of the value here is in
verifying AppleScript escaping (the injection vector), since the
osascript call itself is a one-liner.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.exporters import apple_reminders
from app.exporters.apple_reminders import (
    _build_script,
    _escape,
    add_items,
)


class TestEscape:
    def test_plain_text_unchanged(self):
        assert _escape("hello world") == "hello world"

    def test_backslash_doubled(self):
        assert _escape(r"a\b") == r"a\\b"

    def test_double_quote_escaped(self):
        assert _escape('say "hi"') == r'say \"hi\"'

    def test_newline_to_literal_n(self):
        assert _escape("a\nb") == "a\\nb"

    def test_carriage_return_dropped(self):
        assert _escape("a\rb") == "ab"

    def test_backslash_processed_first(self):
        # If quotes were escaped first, the new \" would itself be re-escaped
        # to \\" by the backslash pass, breaking the literal. Backslash MUST
        # come first.
        assert _escape('"') == r'\"'
        assert _escape('\\"') == r'\\\"'


class TestBuildScript:
    def test_includes_list_creation_guard(self):
        script = _build_script(
            [{"item": "do thing"}], list_name="MyList", source_label="meeting",
        )
        assert 'tell application "Reminders"' in script
        assert 'if not (exists list "MyList") then' in script
        assert 'make new list with properties {name:"MyList"}' in script
        assert 'tell list "MyList"' in script

    def test_item_with_owner_and_due(self):
        script = _build_script(
            [{"item": "ship it", "owner": "alice", "due": "Friday"}],
            list_name="L", source_label="planning",
        )
        assert 'name:"ship it"' in script
        assert "Owner: alice" in script
        assert "Due: Friday" in script
        assert "From: planning" in script

    def test_item_with_no_metadata_emits_no_body(self):
        script = _build_script(
            [{"item": "bare"}], list_name="L", source_label="",
        )
        # body property absent — only name.
        assert 'name:"bare"' in script
        assert "body:" not in script

    def test_skips_empty_item_names(self):
        script = _build_script(
            [{"item": "real"}, {"item": "   "}, {"item": ""}],
            list_name="L", source_label="",
        )
        assert script.count("make new reminder") == 1

    def test_quotes_in_item_text_escaped(self):
        script = _build_script(
            [{"item": 'use "fancy" library'}],
            list_name="L", source_label="",
        )
        # The injected string must remain inside its literal — no
        # unescaped quote that would terminate the AppleScript string.
        assert r'name:"use \"fancy\" library"' in script

    def test_list_name_with_quote_escaped(self):
        script = _build_script(
            [{"item": "x"}], list_name='Project "Aurora"', source_label="",
        )
        assert r'list "Project \"Aurora\""' in script

    def test_injection_attempt_stays_inside_literal(self):
        """Adversarial item: try to break out of the AppleScript string and
        inject extra commands. After escaping, the closing quote and
        injected statement are themselves quoted — "do shell script" must
        only appear INSIDE the reminder's name literal, never after it."""
        attack = '"} \nset x to do shell script "rm -rf ~"\n -- '
        script = _build_script(
            [{"item": attack}], list_name="L", source_label="",
        )
        # Pull just the reminder line (not the list-creation line above it).
        reminder_line = next(
            line for line in script.split("\n") if "make new reminder" in line
        )
        between_quotes = reminder_line.split('name:"', 1)[1]
        # Walk character-by-character respecting backslash escapes.
        i = 0
        while i < len(between_quotes):
            ch = between_quotes[i]
            if ch == "\\":
                i += 2  # skip the backslash and whatever it escapes
                continue
            if ch == '"':
                tail = between_quotes[i + 1:]
                assert "do shell script" not in tail, (
                    "attack escaped its containing literal"
                )
                break
            i += 1
        else:
            pytest.fail("no closing quote found — script malformed")


class TestAddItems:
    def test_empty_list_no_subprocess(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: called.append((a, kw))
            or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        count, errors = add_items([])
        assert count == 0
        assert errors == []
        assert called == []

    def test_all_empty_names_no_subprocess(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: called.append((a, kw))
            or SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        count, errors = add_items([{"item": ""}, {"item": "  "}])
        assert count == 0
        assert errors == []
        assert called == []

    def test_success(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            captured["timeout"] = kwargs.get("timeout")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        count, errors = add_items(
            [{"item": "a"}, {"item": "b"}],
            list_name="L", source_label="src",
        )
        assert count == 2
        assert errors == []
        assert captured["cmd"] == ["osascript", "-"]
        assert "tell application \"Reminders\"" in captured["input"]
        assert captured["timeout"] is not None and captured["timeout"] > 0

    def test_osascript_failure_returns_stderr(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: SimpleNamespace(
                returncode=1, stdout="", stderr="execution error: -1728\n",
            ),
        )
        count, errors = add_items([{"item": "x"}])
        assert count == 0
        assert errors == ["execution error: -1728"]

    def test_osascript_missing_handled(self, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("osascript")
        monkeypatch.setattr(subprocess, "run", boom)
        count, errors = add_items([{"item": "x"}])
        assert count == 0
        assert errors and "osascript" in errors[0].lower()

    def test_timeout_handled(self, monkeypatch):
        def slow(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=30.0)
        monkeypatch.setattr(subprocess, "run", slow)
        count, errors = add_items([{"item": "x"}])
        assert count == 0
        assert errors and "timed out" in errors[0].lower()
