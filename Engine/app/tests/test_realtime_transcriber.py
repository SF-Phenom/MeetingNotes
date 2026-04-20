"""Tests for RealtimeTranscriber — non-GPU-path behavior only."""
from __future__ import annotations

import os

from app.realtime_transcriber import RealtimeTranscriber
from app.transcript_formatter import Sentence


class TestWriteLiveTranscriptRaceGuard:
    """`_write_live_transcript` must no-op after stop() has fired.

    A long-running Parakeet cycle can finish after stop() deleted the
    `.live.txt` sidecar; if the post-cycle write ran anyway, the file
    would reappear and orphan in `recordings/active/`.
    """

    def test_writes_when_stop_event_clear(self, tmp_path):
        rt = RealtimeTranscriber()
        rt._live_txt_path = str(tmp_path / "foo.live.txt")
        rt._write_live_transcript("hello")
        assert os.path.exists(rt._live_txt_path)
        with open(rt._live_txt_path) as f:
            assert f.read() == "hello\n"

    def test_noop_when_stop_event_set(self, tmp_path):
        rt = RealtimeTranscriber()
        rt._live_txt_path = str(tmp_path / "foo.live.txt")
        rt._stop_event.set()
        rt._write_live_transcript("hello")
        assert not os.path.exists(rt._live_txt_path)

    def test_noop_when_path_is_none(self):
        # Pre-start() calls should also not explode.
        rt = RealtimeTranscriber()
        assert rt._live_txt_path is None
        rt._write_live_transcript("hello")  # no-op, no crash


class TestAccumulatedSentences:
    """The pipeline-level diarization step pulls sentences off the realtime
    engine after stop() via this property. Completeness and correction-
    application matter — wrong sentences means wrong speaker alignment.
    """

    def test_empty_when_nothing_transcribed(self):
        rt = RealtimeTranscriber()
        assert rt.accumulated_sentences == []

    def test_combines_finalized_chunks_and_current(self):
        rt = RealtimeTranscriber()
        rt._chunk_sentences = [
            [Sentence(start=0.0, end=1.0, text="alpha")],
            [Sentence(start=1.5, end=2.0, text="beta")],
        ]
        rt._current_chunk_sentences = [
            Sentence(start=2.5, end=3.0, text="gamma"),
        ]
        out = rt.accumulated_sentences
        assert [s.text for s in out] == ["alpha", "beta", "gamma"]

    def test_applies_corrections(self, monkeypatch):
        # Corrections are a user-configured dict; we patch apply_corrections
        # to avoid loading the real file and confirm we go through it.
        from app import corrections as corr_mod

        seen: list[str] = []

        def _fake_apply(text: str) -> str:
            seen.append(text)
            return text.upper()

        monkeypatch.setattr(corr_mod, "apply_corrections", _fake_apply)

        rt = RealtimeTranscriber()
        rt._current_chunk_sentences = [
            Sentence(start=0.0, end=1.0, text="hello world"),
        ]
        out = rt.accumulated_sentences
        assert seen == ["hello world"]
        assert out[0].text == "HELLO WORLD"

    def test_preserves_timings_and_speech_end(self):
        rt = RealtimeTranscriber()
        rt._current_chunk_sentences = [
            Sentence(start=5.0, end=6.0, text="hi", speech_end=5.8),
        ]
        out = rt.accumulated_sentences
        assert out[0].start == 5.0
        assert out[0].end == 6.0
        assert out[0].speech_end == 5.8

    def test_returns_new_sentences_not_input_references(self):
        # The property must copy rather than return live references — callers
        # (pipeline diarization) replace .speaker on each Sentence, which
        # would silently mutate the realtime state otherwise.
        original = Sentence(start=0.0, end=1.0, text="hi")
        rt = RealtimeTranscriber()
        rt._current_chunk_sentences = [original]
        out = rt.accumulated_sentences
        assert out[0] is not original
