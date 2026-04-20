"""Tests for transcription_engine — protocol, factory, env-var dispatch.

These tests use Protocol runtime checks plus a fake backend to exercise the
factory behavior without touching Parakeet. Importing ParakeetBatchEngine
itself is cheap because transcribe_with_parakeet is lazily imported.
"""
from __future__ import annotations

import pytest

from app import state as state_mod
from app import transcription_engine as te
from app.transcription_engine import (
    AppleSpeechBatchEngine,
    BatchEngine,
    ENGINE_ENV_VAR,
    ParakeetBatchEngine,
    RealtimeEngine,
    get_batch_engine,
    get_realtime_engine,
    is_engine_available,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Default the on-disk state to a clean dict so engine resolution
    falls through to env var (or DEFAULT_ENGINE) per test.

    Without this, tests inherit whatever transcription_engine the user
    last selected in their real state.json — flaky.
    """
    monkeypatch.setattr(state_mod, "load", lambda: dict(state_mod.DEFAULT_STATE))


class _FakeBatch:
    """Shape-compatible with BatchEngine."""
    def transcribe(self, wav_path: str):
        return ("fake-result", wav_path)


class _FakeRealtime:
    """Shape-compatible with RealtimeEngine."""
    def start(self, wav_path: str) -> None: pass
    def stop(self) -> str: return ""
    @property
    def is_busy(self) -> bool: return False
    @property
    def live_transcript_path(self) -> str | None: return None


class TestProtocols:
    def test_batch_protocol_structural(self):
        assert isinstance(_FakeBatch(), BatchEngine)
        assert isinstance(ParakeetBatchEngine(), BatchEngine)

    def test_realtime_protocol_structural(self):
        assert isinstance(_FakeRealtime(), RealtimeEngine)

    def test_missing_method_is_not_batch(self):
        class Incomplete:
            pass
        assert not isinstance(Incomplete(), BatchEngine)

    def test_realtime_missing_property_is_not_realtime(self):
        class Incomplete:
            def start(self, p): pass
            def stop(self): return ""
            @property
            def is_busy(self) -> bool: return False
            # no live_transcript_path
        # Python's runtime_checkable Protocol only checks attribute
        # presence, not property-vs-method — the point here is that a
        # genuinely incomplete class fails the check.
        assert not isinstance(Incomplete(), RealtimeEngine)


class TestFactory:
    def test_default_env_uses_parakeet(self, monkeypatch):
        monkeypatch.delenv(ENGINE_ENV_VAR, raising=False)
        engine = get_batch_engine()
        assert isinstance(engine, ParakeetBatchEngine)

    def test_explicit_parakeet_env(self, monkeypatch):
        monkeypatch.setenv(ENGINE_ENV_VAR, "parakeet")
        assert isinstance(get_batch_engine(), ParakeetBatchEngine)

    def test_env_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(ENGINE_ENV_VAR, "PARAKEET")
        assert isinstance(get_batch_engine(), ParakeetBatchEngine)
        monkeypatch.setenv(ENGINE_ENV_VAR, "  Parakeet  ")
        assert isinstance(get_batch_engine(), ParakeetBatchEngine)

    def test_unknown_engine_raises(self, monkeypatch):
        monkeypatch.setenv(ENGINE_ENV_VAR, "whisperkit")
        with pytest.raises(ValueError) as excinfo:
            get_batch_engine()
        assert "whisperkit" in str(excinfo.value)
        assert ENGINE_ENV_VAR in str(excinfo.value)

    def test_state_preference_used_when_env_unset(self, monkeypatch):
        """With no env var, the engine comes from state.json."""
        monkeypatch.delenv(ENGINE_ENV_VAR, raising=False)
        monkeypatch.setattr(
            state_mod, "load",
            lambda: {**state_mod.DEFAULT_STATE, "transcription_engine": "apple_speech"},
        )
        assert isinstance(get_batch_engine(), AppleSpeechBatchEngine)

    def test_env_overrides_state_preference(self, monkeypatch):
        """Env var wins over state — for debugging / A/B."""
        monkeypatch.setenv(ENGINE_ENV_VAR, "parakeet")
        monkeypatch.setattr(
            state_mod, "load",
            lambda: {**state_mod.DEFAULT_STATE, "transcription_engine": "apple_speech"},
        )
        assert isinstance(get_batch_engine(), ParakeetBatchEngine)

    def test_apple_speech_batch_via_env(self, monkeypatch):
        monkeypatch.setenv(ENGINE_ENV_VAR, "apple_speech")
        assert isinstance(get_batch_engine(), AppleSpeechBatchEngine)


class TestIsEngineAvailable:
    def test_parakeet_always_available(self):
        assert is_engine_available("parakeet") is True

    def test_apple_speech_checks_module(self, monkeypatch):
        from app import speech_transcriber
        monkeypatch.setattr(speech_transcriber, "is_available", lambda: True)
        assert is_engine_available("apple_speech") is True
        monkeypatch.setattr(speech_transcriber, "is_available", lambda: False)
        assert is_engine_available("apple_speech") is False

    def test_unknown_engine_unavailable(self):
        assert is_engine_available("whisperkit") is False


class TestAppleSpeechBatchAdapter:
    def test_transcribe_delegates_to_module(self, monkeypatch):
        from app import speech_transcriber

        seen: list[str] = []

        def fake_transcribe(path):
            seen.append(path)
            return "hello world"

        monkeypatch.setattr(speech_transcriber, "transcribe_file", fake_transcribe)

        result = AppleSpeechBatchEngine().transcribe("/tmp/foo.wav")
        assert seen == ["/tmp/foo.wav"]
        assert result.plain_text == "hello world"
        # Apple Speech doesn't provide timestamps — mirror plain text.
        assert result.timestamped_text == "hello world"
        assert result.srt_path == ""

    def test_empty_result_raises(self, monkeypatch):
        from app import speech_transcriber
        monkeypatch.setattr(speech_transcriber, "transcribe_file", lambda _p: None)
        with pytest.raises(RuntimeError) as excinfo:
            AppleSpeechBatchEngine().transcribe("/tmp/foo.wav")
        assert "Dictation" in str(excinfo.value)

    def test_unknown_realtime_engine_raises(self, monkeypatch):
        monkeypatch.setenv(ENGINE_ENV_VAR, "parakeet-xl")
        with pytest.raises(ValueError) as excinfo:
            get_realtime_engine()
        assert "parakeet-xl" in str(excinfo.value)

    def test_get_realtime_returns_new_instance(self, monkeypatch):
        # Each call should produce a fresh realtime engine (a recording's
        # lifetime is bounded by one instance).
        monkeypatch.delenv(ENGINE_ENV_VAR, raising=False)
        a = get_realtime_engine()
        b = get_realtime_engine()
        assert a is not b


class TestParakeetBatchAdapter:
    def test_transcribe_delegates_to_module(self, monkeypatch):
        """ParakeetBatchEngine.transcribe should call the module-level
        transcribe_with_parakeet; we verify via a monkeypatched stub so the
        test doesn't actually spin up MLX."""
        from app import transcriber as transcriber_mod

        seen: list[tuple[str, dict | None]] = []

        def fake_transcribe(path, *, hints=None):
            seen.append((path, hints))
            return transcriber_mod.TranscriptionResult(
                plain_text="hello", timestamped_text="[00:00] hello",
                duration_minutes=1, srt_path="",
            )

        monkeypatch.setattr(
            transcriber_mod, "transcribe_with_parakeet", fake_transcribe,
        )

        result = ParakeetBatchEngine().transcribe("/tmp/foo.wav")
        assert seen == [("/tmp/foo.wav", None)]
        assert result.plain_text == "hello"

    def test_transcribe_forwards_hints(self, monkeypatch):
        from app import transcriber as transcriber_mod

        seen: list[dict | None] = []

        def fake_transcribe(path, *, hints=None):
            seen.append(hints)
            return transcriber_mod.TranscriptionResult(
                plain_text="x", timestamped_text="x", duration_minutes=0, srt_path="",
            )

        monkeypatch.setattr(
            transcriber_mod, "transcribe_with_parakeet", fake_transcribe,
        )

        ParakeetBatchEngine().transcribe(
            "/tmp/foo.wav", hints={"participant_count": 3},
        )
        assert seen == [{"participant_count": 3}]
