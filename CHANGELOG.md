# Changelog

All notable changes to MeetingNotes will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Pre-1.0 releases may contain breaking changes on any minor-version bump.

## [Unreleased]

## [0.1.1] — 2026-04-20

Installer reliability pass, driven by a real coworker handoff. No changes
to the running app — this release is entirely about making a fresh install
on a new Mac work without hands-on help.

### Added
- `install.sh` bootstrap at the repo root. Public install command is now
  a single line:
  ```
  curl -fsSL https://raw.githubusercontent.com/SF-Phenom/MeetingNotes/main/install.sh | zsh
  ```
  Sidesteps the macOS exec-bit / Gatekeeper-quarantine breakage that hits
  `setup.command` when it travels as a file (email, Slack, AirDrop). The
  bootstrap downloads via curl (Terminal-initiated, no quarantine),
  chmods, and re-execs with `/dev/tty` as stdin so the interactive
  setup prompts still work when the bootstrap was itself piped from curl.
- `Engine/defaults/google_oauth_client.json` shipped in the repo so
  coworkers don't need to provision their own Google Cloud project. It's
  an installed (Desktop-app) OAuth client, which Google explicitly
  designs to be embedded in distributed source — per-user refresh tokens
  still live only on each user's machine in the gitignored
  `Engine/.credentials/`.

### Changed
- `setup.command` step 5 (Parakeet model): replaced the silent
  `&>/dev/null` check with a HuggingFace cache-dir sniff plus a
  visible-progress download. Fresh installs now see HuggingFace's
  native tqdm bars during the 2.5 GB download instead of a frozen
  terminal for several minutes. Cached runs skip Python startup entirely.
- `setup.command` step 12: seeds the OAuth client from `Engine/defaults/`
  on fresh installs; the "OAuth client JSON not found" warning is now
  a true corner case (user deleted the defaults file).
- `Engine/SETUP.md` Quick Start leads with the curl one-liner. Double-
  click and `git clone` paths kept as documented alternatives.

## [0.1.0] — 2026-04-20

First tracked release. The app is feature-complete for single-user meeting
capture on macOS Apple Silicon and is in active personal use; pre-1.0
signals that APIs, config, and workflows may still shift before a wider
handoff to coworkers.

### Capture
- CoreAudio Process Tap for system audio (reliable capture of Zoom, Google
  Meet, FaceTime — replaces ScreenCaptureKit, which silently dropped VP-IO
  apps).
- AVAudioEngine for microphone capture, with auto-recovery on device
  hot-swap (AirPods connect mid-recording, HDMI plug/unplug).
- In-Swift `MixerDrainer` combines mic + system audio into a single WAV.
- Ad-hoc codesigned `capture-audio` binary with embedded Info.plist so
  macOS attributes TCC prompts to MeetingNotes, not Terminal.
- 14-day auto-delete of retained recordings; delete-after-transcribe by
  default.

### Transcription
- Parakeet (MLX) as the primary engine — on-device, GPU-accelerated.
- Apple Speech (`SFSpeechRecognizer`) available as an alternative via a
  menubar submenu.
- Real-time transcription during recording: chunks roll in via `.live.txt`
  with pause-based paragraph breaks.
- Post-transcription corrections dictionary at `Settings/corrections.md`
  for systematic misrecognitions.

### Summarization
- Claude API (primary) with automatic fallback to Ollama (`qwen3:4b`)
  when credits run out or the network fails.
- Permanent 4xx errors short-circuit the retry loop so the fallback kicks
  in sooner.
- Action-item export with pluggable backends (currently Apple Reminders;
  Things 3 / Google Tasks / Notion on the roadmap).

### Calendar integration
- Google Calendar enrichment resolves the event title, participants, and
  description from a meeting on the user's primary calendar.
- Transcript title prefers the calendar event name over the LLM-generated
  title; falls back to source-based name when neither is available.
- Strict association rule: a recording is associated with an event iff
  `event.start - 10 min <= recording_start <= event.end`. Handles
  back-to-back meetings by preferring the upcoming event.
- Menubar submenu exposes "Test Connection" (probes auth + scope + event
  fetch) and "Re-authenticate" (resets and re-runs OAuth).
- Part N suffix on repeat recordings of the same meeting (e.g. after a
  crash + restart) — tracked by calendar event ID in the frontmatter.

### Privacy & retention
- Recording retention gated on `MEETINGNOTES_RETAIN_RECORDINGS=1` in
  gitignored `Engine/.env.local` — no in-app toggle, no easy path for a
  non-technical coworker to stumble into the recordings folder.
- Audio and transcripts are local-first; only Claude summarization sends
  transcript text off-device when the Claude API is configured.

### Ops
- Atomic `.md` transcript writes (fsync + rename); atomic `state.json`
  writes with file locking.
- `setup.command` is idempotent and self-updating; fresh clones and
  existing installs converge to the same layout.
- Startup cleanup removes old recordings and reconciles orphans.

### Deferred / out of scope
- Speaker diarization (planned — FluidAudio CoreML CLI). See
  `Engine/ROADMAP.md`.
- Mic ducking during loud system audio.
- WhisperKit benchmark.
- Additional action-item exporter backends (Things 3, Google Tasks,
  Notion).
