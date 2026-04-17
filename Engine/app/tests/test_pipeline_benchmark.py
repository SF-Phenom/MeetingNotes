"""End-to-end pipeline test against the benchmark fixture.

This is the single regression test that catches cross-cutting breakage. It
runs the full transcription + summarization + formatting pipeline against a
known-good recording and asserts:

  - All `expected_phrases` appear in the transcript
  - Each `expected_corrections` rule is applied (heard absent, should_be present)
  - Each `expected_action_items` substring appears in the Action Items section

If the benchmark files do not exist, the entire module is skipped so Phase 0
lands without blocking. Drop `benchmark.wav` + `benchmark.yaml` into
`app/tests/fixtures/` (see that directory's README for the schema) and the
tests auto-activate.

Because this hits the real Parakeet model, Claude API (or Ollama fallback), and
performs disk I/O, it is slow (~30s+). Mark it with `-m benchmark` to
include/exclude in CI.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from .conftest import BENCHMARK_META, BENCHMARK_WAV


pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(
        not BENCHMARK_WAV.exists() or not BENCHMARK_META.exists(),
        reason=(
            "benchmark fixture missing — drop benchmark.wav + benchmark.yaml into "
            "Engine/app/tests/fixtures/ (see fixtures/README.md)."
        ),
    ),
]


def _load_yaml(path: Path) -> dict:
    """Tiny ad-hoc YAML loader to avoid adding PyYAML to requirements.

    Only supports the shapes used in benchmark.yaml:
      - top-level scalars: `key: value`
      - flat string lists: `key:\\n  - item\\n  - item`
      - list of mappings: `key:\\n  - key1: value\\n    key2: value`
    Quotes are stripped from values; everything else is a hand-roll.
    """
    import yaml  # type: ignore[import-not-found]

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_meta() -> dict:
    try:
        return _load_yaml(BENCHMARK_META)
    except ImportError:
        pytest.skip(
            "PyYAML not installed. Run: pip install pyyaml (or add to "
            "requirements.txt if you want benchmark tests in CI)"
        )


@pytest.fixture(scope="module")
def benchmark_meta() -> dict:
    """Parse benchmark.yaml once per test module."""
    return _load_meta()


@pytest.fixture(scope="module")
def pipeline_output(tmp_path_factory, benchmark_meta) -> dict:
    """Run the full pipeline against benchmark.wav and return the output bundle.

    Copies the fixture into an isolated tmp directory first so the test never
    mutates the committed file. Returns:
        {
            "md_path": Path to the produced .md,
            "md_text": str contents,
            "meta": the parsed benchmark.yaml,
        }
    """
    # Late-import so test collection doesn't pull in the whole app stack
    # (parakeet-mlx, anthropic, etc.) if the fixture is absent.
    from app import pipeline

    work_dir = tmp_path_factory.mktemp("benchmark_run")
    mic_copy = work_dir / "zoom_2026-01-01_10-00.wav"
    shutil.copy(BENCHMARK_WAV, mic_copy)

    # Point the pipeline's TRANSCRIPTS_DIR at the tmp area. Kept narrow; we
    # don't want to fight pipeline internals, just isolate output.
    import app.state as state_mod
    import app.pipeline as pipe_mod

    transcripts_dir = work_dir / "transcripts"
    transcripts_dir.mkdir()
    orig_transcripts = pipe_mod.TRANSCRIPTS_DIR
    pipe_mod.TRANSCRIPTS_DIR = str(transcripts_dir)
    try:
        md_path = pipeline.process_recording(str(mic_copy))
    finally:
        pipe_mod.TRANSCRIPTS_DIR = orig_transcripts

    if not md_path or not os.path.exists(md_path):
        pytest.fail(f"Pipeline returned no markdown file (got {md_path!r})")

    return {
        "md_path": Path(md_path),
        "md_text": Path(md_path).read_text(encoding="utf-8"),
        "meta": benchmark_meta,
    }


class TestBenchmarkPipeline:
    def test_expected_phrases_present(self, pipeline_output):
        missing = [
            p
            for p in pipeline_output["meta"].get("expected_phrases", [])
            if p.lower() not in pipeline_output["md_text"].lower()
        ]
        assert not missing, f"Expected phrases missing from transcript: {missing}"

    def test_corrections_applied(self, pipeline_output):
        md_lower = pipeline_output["md_text"].lower()
        for rule in pipeline_output["meta"].get("expected_corrections", []):
            heard = rule["heard"].lower()
            should_be = rule["should_be"].lower()
            assert heard not in md_lower, (
                f"Correction rule not applied — 'heard' still present: {heard!r}"
            )
            assert should_be in md_lower, (
                f"Correction rule not applied — 'should_be' missing: {should_be!r}"
            )

    def test_action_items_extracted(self, pipeline_output):
        md_lower = pipeline_output["md_text"].lower()
        # Find the Action Items section and only search within it to avoid
        # false positives from the full transcript below it.
        marker = "## action items"
        idx = md_lower.find(marker)
        if idx == -1:
            pytest.fail("No '## Action Items' section in transcript")
        next_section = md_lower.find("\n## ", idx + len(marker))
        action_section = (
            md_lower[idx:next_section] if next_section != -1 else md_lower[idx:]
        )
        for item in pipeline_output["meta"].get("expected_action_items", []):
            assert item.lower() in action_section, (
                f"Expected action item missing: {item!r}"
            )
