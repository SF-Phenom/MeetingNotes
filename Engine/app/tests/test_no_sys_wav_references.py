"""Regression test: Phase 4C removed the ``.sys.wav`` sidecar plumbing.

Before 4C, ``audio_mixer.py`` merged a mic ``.wav`` with a system-audio
``.sys.wav`` sibling and the pipeline expected the pair. After 4C, the Swift
capture binary produces a single pre-mixed WAV and the sidecar is gone.
Reintroducing a two-file pattern would silently break pipeline assumptions —
this test fails loudly if anyone adds a live-code reference.
"""
from __future__ import annotations

from pathlib import Path


# Files whose ``.sys.wav`` references are historical documentation or the
# single legacy compound-extension parsing test — not live plumbing.
_ALLOWED = {
    "recording_file.py",                    # docstring + legacy-extension comment
    "pipeline.py",                          # comment explaining the 4C cleanup
    "tests/test_recording_file.py",         # compound-extension parse test
    "tests/test_no_sys_wav_references.py",  # this file
}


def test_no_live_sys_wav_references() -> None:
    app_dir = Path(__file__).parent.parent
    violations: list[str] = []
    for py_file in sorted(app_dir.rglob("*.py")):
        rel = py_file.relative_to(app_dir).as_posix()
        if rel in _ALLOWED:
            continue
        text = py_file.read_text(encoding="utf-8")
        if ".sys.wav" in text or "sys_wav" in text:
            violations.append(rel)
    assert not violations, (
        "New .sys.wav references found outside the allowlist. Phase 4C "
        "removed the split-file audio path — the Swift capture binary "
        "produces a single pre-mixed WAV now, and the pipeline assumes no "
        f"sidecar. Offending files: {violations}"
    )
