# MeetingNotes — Roadmap

Future enhancements, roughly prioritized. Check items off as they're completed.

---

## Planned

- [ ] **Transcription progress indicator** — Stream whisper.cpp's stderr progress output (e.g. `42%`) to the menubar icon in real time. Requires switching from `subprocess.run` to `subprocess.Popen` in `transcriber.py` and threading a callback to update the `rumps` menu title.

- [ ] **Action item export to task apps** — Push action items from meeting summaries to a personal task manager. Build a `task_exporter.py` module with a common interface and swappable backends (like `summarizer.py` does for Claude/Ollama). Action item data is already structured JSON from the summarizer. Candidate integrations, in rough order of implementation ease:
  - **Apple Reminders** — AppleScript, zero setup, works for any Mac user
  - **Things 3** — URL scheme (`things:///add?title=...`), no API key needed
  - **Google Tasks** — Already have OAuth wired up, just add Tasks API scope
  - **Notion** — REST API, requires integration token setup

- [ ] **Standalone setup.command installer (Phase 2)** — Make `setup.command` fully standalone: a single file the user downloads from GitHub. It installs initial dependencies (Xcode CLI tools, Homebrew, git), then clones the repo from GitHub to `~/MeetingNotes`, then continues with the rest of the setup (whisper.cpp, Python venv, Swift build, API key, calendar). Coworkers never need to think about git or where to put files — just download one file and double-click.

## Investigate

- [ ] **Real-time transcription via Parakeet** — Investigate whether Nvidia's Parakeet models enable viable real-time (live, in-meeting) transcription on Apple Silicon. These two research areas are linked:
  - **Why Parakeet matters for real-time:** whisper.cpp has a `stream` mode, but quality degrades significantly on short audio chunks. Parakeet claims 4x faster inference — if that holds on CoreML/ONNX via the Neural Engine, it may process chunks fast enough to keep up with live audio without sacrificing accuracy. Parakeet's speed is marginal for batch mode (whisper already finishes in minutes), but for real-time it could be the difference between viable and not.
  - **Research steps:** (1) Check Parakeet CoreML/ONNX conversion maturity for Apple Silicon. (2) Benchmark Parakeet vs. whisper.cpp on a real meeting recording in batch mode — compare speed and quality, especially proper noun handling. (3) Test Parakeet on streaming audio chunks — what chunk size maintains acceptable quality? (4) Evaluate whether Parakeet supports initial prompt biasing (we rely on this for name/term spelling).
  - **Architecture if viable:** Stream audio chunks to Parakeet in memory, accumulate partial transcripts, summarize at meeting end. Audio never hits disk. Optional mode alongside current batch pipeline, not a replacement.
  - **If Parakeet doesn't pan out:** Real-time with whisper.cpp `stream` is a fallback, but expect lower quality. Batch remains the default.

- [ ] **Borrow patterns from AudioTee** — Evaluated the [AudioTee](https://github.com/makeusabrew/audiotee) library (open-sourced by Talat's developer). **Not adopting as a dependency** — it only handles system audio capture (likely the open-sourced half of Talat's stack, with mic capture kept proprietary). Our code already does both mic + system audio in ~240 lines, and AudioTee's API is marked unstable. But several patterns are worth borrowing, especially if we build real-time transcription:
  - **Output handler protocol** — AudioTee abstracts output behind an `AudioOutputHandler` protocol (`handleAudioData`, `handleStreamStart/Stop`). If we build real-time mode, we'd need the same pattern: one handler writes to WAV (current batch mode), another streams chunks to the transcription model. This is the most relevant borrowable idea.
  - **Zero-copy ring buffer** — AudioTee uses a raw pointer ring buffer to avoid Swift Array copy-on-write overhead on the audio thread. Irrelevant for batch/file recording, but matters for real-time streaming where latency counts.
  - **Process filtering by PID** — Could capture only Zoom/Meet audio instead of all system audio. Nice-to-have, not urgent.
  - **`deinit` cleanup** — Cleaner than our explicit `teardownSystemAudio()` method. Minor improvement we could adopt anytime.
