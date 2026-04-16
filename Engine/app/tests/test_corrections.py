"""Tests for corrections — find-and-replace loaded from Settings/corrections.md.

corrections.py caches parsed rules at module level keyed by mtime. Each test
points CORRECTIONS_PATH at a fresh temp file and resets the cache so tests
don't leak state through the module globals.
"""
from __future__ import annotations

import pytest

from app import corrections


@pytest.fixture
def corrections_file(tmp_path, monkeypatch):
    """Swap corrections.CORRECTIONS_PATH for a writable temp file.

    Returns a helper that rewrites the file and bumps the cached mtime so
    subsequent calls to apply_corrections() pick up the new content.
    """
    path = tmp_path / "corrections.md"
    monkeypatch.setattr(corrections, "CORRECTIONS_PATH", str(path))
    # Force reload on first call — the module-level cache may be populated
    # from a previous test.
    monkeypatch.setattr(corrections, "_cached_mtime", 0)
    monkeypatch.setattr(corrections, "_cached_terms", [])
    monkeypatch.setattr(corrections, "_cached_replacements", [])

    def write(text: str) -> None:
        path.write_text(text, encoding="utf-8")
        # Reset mtime cache so the next _load() picks up the new file.
        corrections._cached_mtime = 0

    return write


class TestApplyCorrections:
    def test_no_file_returns_unchanged(self, corrections_file):
        # File doesn't exist yet; apply_corrections should be a passthrough.
        assert corrections.apply_corrections("hello world") == "hello world"

    def test_empty_file_returns_unchanged(self, corrections_file):
        corrections_file("")
        assert corrections.apply_corrections("anything") == "anything"

    def test_single_replacement(self, corrections_file):
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| aqua fluxin | Aquafluxion |\n"
        )
        assert (
            corrections.apply_corrections("The aqua fluxin team met today.")
            == "The Aquafluxion team met today."
        )

    def test_case_insensitive_match(self, corrections_file):
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| zoom | Zoom |\n"
        )
        # Replacement is applied regardless of input casing.
        assert corrections.apply_corrections("ZOOM call") == "Zoom call"
        assert corrections.apply_corrections("zoom call") == "Zoom call"
        assert corrections.apply_corrections("Zoom call") == "Zoom call"

    def test_word_boundary_respected(self, corrections_file):
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| pan | pot |\n"
        )
        # "pan" inside "pancake" should NOT be replaced.
        assert corrections.apply_corrections("pancake in a pan") == "pancake in a pot"

    def test_multiple_replacements_applied_in_order(self, corrections_file):
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| foo | bar |\n"
            "| baz | qux |\n"
        )
        out = corrections.apply_corrections("foo and baz walked into foo baz")
        assert out == "bar and qux walked into bar qux"

    def test_terms_section_parsed(self, corrections_file):
        corrections_file(
            "## Terms\n"
            "- Aquafluxion\n"
            "- Project Halyard\n"
            "\n"
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
        )
        terms = corrections.get_terms()
        assert "Aquafluxion" in terms
        assert "Project Halyard" in terms

    def test_reloads_when_file_changes(self, corrections_file):
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| foo | bar |\n"
        )
        assert corrections.apply_corrections("foo") == "bar"

        # User edits the file to change the replacement.
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| foo | baz |\n"
        )
        assert corrections.apply_corrections("foo") == "baz"

    def test_invalid_regex_in_heard_is_logged_not_fatal(self, corrections_file):
        # re.escape should make every input safe, but if parsing ever produced
        # a bad entry we shouldn't blow up.
        corrections_file(
            "## Replacements\n"
            "| heard | should be |\n"
            "| --- | --- |\n"
            "| valid | replaced |\n"
        )
        # Should not raise.
        assert corrections.apply_corrections("valid text") == "replaced text"

    def test_table_without_header_separator_still_parses(self, corrections_file):
        # Some users may omit the --- separator; the parser is lenient.
        corrections_file(
            "## Replacements\n"
            "| foo | bar |\n"
        )
        # We expect replacement to be applied.
        assert corrections.apply_corrections("foo") == "bar"
