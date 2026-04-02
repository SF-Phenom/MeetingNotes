# MeetingNotes — Setup Guide

**Requirements:** macOS on Apple Silicon (M1/M2/M3/M4).

## Quick Start (Recommended)

Double-click `setup.command` in Finder, or run it from Terminal:

```bash
cd ~/Documents/MeetingNotes
./setup.command
```

The script is idempotent (safe to re-run) and will skip anything already installed.
It takes 10–20 minutes on a fresh machine, mostly due to the ~1.5GB whisper model download.

If you prefer to install manually, or if the script hits an issue, follow the step-by-step guide below.

---

## Manual Setup

Follow these steps in order. Each section builds on the previous one.

---

## 1. Xcode Command Line Tools

You likely already have these (Swift is working), but confirm:

```bash
xcode-select --install
```

If it says "already installed," you're good.

---

## 2. Homebrew

Install the macOS package manager:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After installation, follow the instructions it prints to add Homebrew to your PATH. Then verify:

```bash
brew --version
```

---

## 3. Python 3.12

Your Mac has Python 3.9.6 (the old system version). We need a modern Python:

```bash
brew install python@3.12
```

Verify:

```bash
python3.12 --version
```

Should print `Python 3.12.x`.

---

## 4. Python Virtual Environment

Create an isolated environment so packages don't conflict with your system:

```bash
cd ~/Documents/MeetingNotes
python3.12 -m venv .venv
source .venv/bin/activate
```

Your terminal prompt should now show `(.venv)`. Install the dependencies:

```bash
pip install -r requirements.txt
```

**Every time you open a new terminal** to work with MeetingNotes, activate the environment first:

```bash
source ~/Documents/MeetingNotes/.venv/bin/activate
```

---

## 5. ffmpeg

Needed for audio format conversion (used later in transcription pipeline):

```bash
brew install ffmpeg
```

---

## 6. whisper.cpp with Metal Acceleration

whisper.cpp runs speech-to-text locally on your Mac using the GPU (Metal). This keeps all audio on your machine.

```bash
# Install cmake (build tool)
brew install cmake

# Clone and build whisper.cpp with Metal support
cd ~
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
cmake -B build -DWHISPER_METAL=ON
cmake --build build --config Release
```

Download the model (large-v3-turbo — best quality/speed balance on M4, ~1.5GB RAM):

```bash
cd ~/whisper.cpp
./models/download-ggml-model.sh large-v3-turbo
```

Verify it works:

```bash
~/whisper.cpp/build/bin/whisper-cli --help
```

You should see the whisper.cpp help output. If so, you're good.

**Alternative model:** If `large-v3-turbo` is too slow or uses too much memory, you can switch to `medium.en` (faster, English-only, ~750MB RAM):
```bash
./models/download-ggml-model.sh medium.en
```
Then edit `WHISPER_MODEL` in `app/transcriber.py` to point to the new model file.

---

## 7. Anthropic API Key

The transcription pipeline sends transcript text (never audio) to Claude for summarization. You need an API key from [console.anthropic.com](https://console.anthropic.com/).

```bash
# Add to your shell profile (persists across terminal sessions)
echo 'export ANTHROPIC_API_KEY=sk-ant-your-key-here' >> ~/.zshrc
source ~/.zshrc
```

Verify:

```bash
echo $ANTHROPIC_API_KEY
```

Should print your key (starts with `sk-ant-`).

---

## 7b. Google Calendar Integration (Optional)

This auto-populates meeting name and participants from your Google Calendar. If you skip this step, the pipeline still works — you just won't get calendar metadata.

**Prerequisites:** A Google Cloud project with the Calendar API enabled and an OAuth 2.0 "Desktop app" credential. If you've already set this up, you should have a JSON credential file.

**Install the Google libraries** (if not already installed):

```bash
cd ~/Documents/MeetingNotes
source .venv/bin/activate
pip install -r requirements.txt
```

**Place the credential file:**

```bash
mkdir -p ~/Documents/MeetingNotes/.credentials
cp /path/to/your/client_secret_XXXXX.json ~/Documents/MeetingNotes/.credentials/google_oauth_client.json
```

**Authenticate** (one-time — opens a browser window):

```bash
cd ~/Documents/MeetingNotes
source .venv/bin/activate
python3 -c "
from app.calendar_lookup import _get_credentials
creds = _get_credentials()
print('SUCCESS' if creds else 'FAILED')
"
```

If it prints "SUCCESS", you're set. The token is saved to `.credentials/google_token.json` and will auto-refresh. You won't need to re-authenticate unless you revoke access.

**How it works:** When a recording is processed, the pipeline checks your calendar for events that overlap with the recording's timestamp. If it finds a match, it pulls in the meeting title and attendee list automatically.

---

## 8. Obsidian (Transcript Viewer)

Install [Obsidian](https://obsidian.md/) to browse and search your meeting transcripts:

```bash
brew install --cask obsidian
```

Create the vault (a `.obsidian` folder marks the directory as a vault):

```bash
mkdir -p ~/Documents/MeetingNotes/transcripts/.obsidian
```

Then open Obsidian → **Open folder as vault** → select `~/Documents/MeetingNotes/transcripts`.

All your `.md` transcripts will appear in the sidebar, searchable and linked.

---

## 9. Build the Swift Audio Capture Binary

```bash
cd ~/Documents/MeetingNotes/CaptureAudio
swift build -c release
```

The binary will be at `.build/release/CaptureAudio`. To install it:

```bash
cp .build/release/CaptureAudio ~/Documents/MeetingNotes/.bin/capture-audio
```

---

## 10. macOS Permissions

The app needs several permissions. macOS will prompt you the first time each is needed. Here's what to expect:

| Permission | Why | When prompted |
|---|---|---|
| **Microphone** | Record your side of calls | First time you start a recording |
| **Accessibility** | Read window titles via AppleScript for call detection | First time the app checks for active calls |
| **Notifications** | Show recording status and transcription alerts | First app launch |

**To manage permissions later:** System Settings → Privacy & Security → [category]

The audio capture binary requests microphone access. The Python menubar app requests Accessibility (for AppleScript). If a permission is denied, the relevant feature won't work — the app will log a warning.

---

## 11. Run the App

```bash
cd ~/Documents/MeetingNotes
source .venv/bin/activate
python app/menubar.py
```

You should see a microphone icon (🎙) in your menu bar.

---

## 12. First-Run Checklist

- [ ] Menu bar icon appears
- [ ] Edit `~/Documents/MeetingNotes/context.md` with your role, team, and meeting info
- [ ] Open a Google Meet in Chrome to test call detection
- [ ] Try a manual recording start/stop from the menu bar
- [ ] Check `~/Documents/MeetingNotes/recordings/queue/` for the recorded `.wav` file
- [ ] After recording stops, transcription should auto-start (⏳ icon in menubar)
- [ ] Check `~/Documents/MeetingNotes/transcripts/` for the generated `.md` file
- [ ] Or transcribe manually: `python -m app.pipeline ~/Documents/MeetingNotes/recordings/queue/<filename>.wav`

---

## Troubleshooting

**"Permission denied" errors:** Check System Settings → Privacy & Security. The app may need to be re-added after updates.

**Menu bar icon doesn't appear:** Make sure your virtual environment is activated (`source .venv/bin/activate`) and rumps is installed (`pip list | grep rumps`).

**No audio in recording:** Check that the correct microphone is selected in System Settings → Sound → Input. The system audio capture requires no configuration — it captures all app audio automatically.

**Swift binary won't compile:** Ensure Xcode Command Line Tools are installed (`xcode-select --install`). The binary requires macOS 14.2+ for the Core Audio tap API.

**Transcription fails with "whisper-cli not found":** Make sure you built whisper.cpp per step 6. The binary should be at `~/whisper.cpp/build/bin/whisper-cli`.

**Transcription fails with "model not found":** Download the model: `cd ~/whisper.cpp && ./models/download-ggml-model.sh large-v3-turbo`

**Claude summarization fails:** Check that `ANTHROPIC_API_KEY` is set in your environment. The key must be exported before launching the app. If you added it to `~/.zshrc`, restart your terminal or run `source ~/.zshrc`.

**Transcription runs but summary says "unavailable":** This means whisper succeeded but Claude API failed. Check `~/Documents/MeetingNotes/logs/app.log` for the specific error. The transcript is still saved with the raw text.

**setup.command failed partway through:** Re-run `./setup.command` — it skips completed steps and resumes where it left off.

**setup.command says "Apple Silicon required":** MeetingNotes requires an M-series Mac (M1/M2/M3/M4) for whisper.cpp Metal GPU acceleration. Intel Macs are not supported.
