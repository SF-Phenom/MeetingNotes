# MeetingNotes — Roadmap

Future enhancements, roughly prioritized. Check items off as they're completed.

---

## Planned

- [ ] **Transcription progress indicator** — Stream whisper.cpp's stderr progress output (e.g. `42%`) to the menubar icon in real time. Requires switching from `subprocess.run` to `subprocess.Popen` in `transcriber.py` and threading a callback to update the `rumps` menu title.
