# MeetingNotes — Roadmap

Future enhancements, roughly prioritized. Check items off as they're completed.

---

## Planned

- [x] **Post-transcription corrections dictionary** — `Settings/corrections.md` lets non-technical users add terms and find-and-replace pairs to fix systematic misrecognitions. Parsed by `Engine/app/corrections.py`, applied after transcript filtering in both batch and realtime paths.

- [x] **Apple Speech as second engine** — On-device speech recognition via `SFSpeechRecognizer` available as an alternative to Parakeet. Swift binary at `Engine/SpeechTranscribe/`, Python wrapper at `Engine/app/speech_transcriber.py`. Includes audio normalization for low-level system audio captures. Selectable via menubar → Transcription Engine. Requires macOS 26+, Dictation enabled for on-device model download. Does not support custom vocabulary — corrections dictionary is essential.

- [x] **In-Swift audio mixer + Process Tap rewrite** — Landed as the Phase 4 audio capture rewrite. `AudioCapture.swift` now maintains two Int16 ring buffers (mic + system) and a `MixerDrainer` that reads `min(mic_available, sys_available)` every 100 ms, saturating-adds both streams, and writes one mixed WAV. Realtime transcription picks up both voices from the same file. System audio source changed from ScreenCaptureKit (which silently dropped Voice Processing IO calls like Zoom / FaceTime / Meet native) to a CoreAudio Process Tap wrapped in a private aggregate device. `audio_mixer.py`, `.sys.wav` sidecars, and the pipeline mix block are all gone.

- [ ] **Mic ducking in audio capture** — When system audio and mic overlap, reduce mic gain to prioritize meeting audio. Envelope-based gain control (~10ms attack, ~200ms release) in `AudioCapture.swift`. Inspired by TypeWhisper's implementation.

- [ ] **Investigate WhisperKit** — Swift-native whisper implementation with CoreML acceleration, temperature fallback, and streaming tokens. May offer superior accuracy to Parakeet for some use cases. Worth benchmarking against Parakeet and Apple SpeechAnalyzer on real meetings.

- [ ] **Action item export to task apps** — Push action items from meeting summaries to a personal task manager. Build a `task_exporter.py` module with a common interface and swappable backends (like `summarizer.py` does for Claude/Ollama). Action item data is already structured JSON from the summarizer. Candidate integrations, in rough order of implementation ease:
  - **Apple Reminders** — AppleScript, zero setup, works for any Mac user
  - **Things 3** — URL scheme (`things:///add?title=...`), no API key needed
  - **Google Tasks** — Already have OAuth wired up, just add Tasks API scope
  - **Notion** — REST API, requires integration token setup

- [x] **Standalone setup.command installer (Phase 2)** — Make `setup.command` fully standalone: a single file the user downloads from GitHub. It installs initial dependencies (Xcode CLI tools, Homebrew, git), then clones the repo from GitHub to `~/MeetingNotes`, then continues with the rest of the setup (Python venv, Parakeet, Swift build, API key, calendar). Coworkers never need to think about git or where to put files — just download one file and double-click.

## Investigate

- [x] **Real-time transcription via Parakeet** — Parakeet via MLX is now the sole transcription engine. Runs on Apple Silicon with real-time live transcription during recordings. whisper.cpp batch mode was removed due to accuracy issues (hallucination loops, required extensive workarounds).

- [ ] **Borrow patterns from AudioTee** — Evaluated the [AudioTee](https://github.com/makeusabrew/audiotee) library (open-sourced by Talat's developer). **Not adopting as a dependency** — it only handles system audio capture (likely the open-sourced half of Talat's stack, with mic capture kept proprietary). Our code already does both mic + system audio in ~240 lines, and AudioTee's API is marked unstable. But several patterns are worth borrowing, especially if we build real-time transcription:
  - **Output handler protocol** — AudioTee abstracts output behind an `AudioOutputHandler` protocol (`handleAudioData`, `handleStreamStart/Stop`). If we build real-time mode, we'd need the same pattern: one handler writes to WAV (current batch mode), another streams chunks to the transcription model. This is the most relevant borrowable idea.
  - **Zero-copy ring buffer** — AudioTee uses a raw pointer ring buffer to avoid Swift Array copy-on-write overhead on the audio thread. Irrelevant for batch/file recording, but matters for real-time streaming where latency counts.
  - **Process filtering by PID** — Could capture only Zoom/Meet audio instead of all system audio. Nice-to-have, not urgent.
  - **`deinit` cleanup** — Cleaner than our explicit `teardownSystemAudio()` method. Minor improvement we could adopt anytime.
