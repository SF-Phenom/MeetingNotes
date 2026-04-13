# MeetingNotes — Setup Guide

**Requirements:** macOS on Apple Silicon (M1/M2/M3/M4).

## Quick Start (Recommended)

Double-click `Engine/setup.command` in Finder, or run it from Terminal:

```bash
cd ~/MeetingNotes_RT
./Engine/setup.command
```

The script is idempotent (safe to re-run) and will skip anything already installed.
It takes 5–15 minutes on a fresh machine, mostly due to the Parakeet model download (~2.5 GB).

If you prefer to install manually, or if the script hits an issue, follow the step-by-step guide below.

---

## Manual Setup

Follow these steps in order. Each section builds on the previous one.

---

## 1. Xcode Command Line Tools

You likely already have these, but confirm:

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
cd ~/MeetingNotes_RT
python3.12 -m venv Engine/.venv
source Engine/.venv/bin/activate
```

Your terminal prompt should now show `(.venv)`. Install the dependencies:

```bash
pip install -r Engine/requirements.txt
```

**Every time you open a new terminal** to work with MeetingNotes, activate the environment first:

```bash
source ~/MeetingNotes_RT/Engine/.venv/bin/activate
```

---

## 5. ffmpeg

Needed for audio format conversion:

```bash
brew install ffmpeg
```

---

## 6. Parakeet (Transcription Engine)

Parakeet is the transcription engine — it runs on Apple Silicon via MLX and provides real-time live transcription during recordings. It was installed with `pip install -r Engine/requirements.txt` in step 4.

The model (~2.5 GB) downloads automatically the first time you start a recording. To pre-download:

```bash
source Engine/.venv/bin/activate
python3 -c "from parakeet_mlx import from_pretrained; from_pretrained('mlx-community/parakeet-tdt-0.6b-v3')"
```

---

## 7. Anthropic API Key (Optional)

The pipeline sends transcript text (never audio) to Claude for summarization. If you have an API key from [console.anthropic.com](https://console.anthropic.com/):

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-your-key-here' >> ~/.zshrc
source ~/.zshrc
```

Verify:

```bash
echo $ANTHROPIC_API_KEY
```

**If you don't have a key:** The app will fall back to Ollama/Qwen for local summarization (see step 9). Transcription works regardless — only summarization uses the API.

---

## 8. Ollama + Qwen (Local Summarization Fallback)

If you don't have an Anthropic API key, or if the API is unreachable, the app falls back to a local model via [Ollama](https://ollama.ai):

```bash
brew install ollama
ollama pull qwen3:4b
```

Start the Ollama service:

```bash
ollama serve
```

Ollama runs in the background. The app auto-detects it when Claude is unavailable.

---

## 9. Google Calendar Integration (Optional)

Auto-populates meeting name and participants from your Google Calendar. If you skip this, the pipeline still works — you just won't get calendar metadata.

**Prerequisites:** A Google Cloud project with the Calendar API enabled and an OAuth 2.0 "Desktop app" credential.

**Place the credential file:**

```bash
mkdir -p ~/MeetingNotes_RT/Engine/.credentials
cp /path/to/your/client_secret_XXXXX.json ~/MeetingNotes_RT/Engine/.credentials/google_oauth_client.json
```

**Authenticate** (one-time — opens a browser window):

```bash
cd ~/MeetingNotes_RT/Engine
source .venv/bin/activate
python3 -c "
from app.calendar_lookup import _get_credentials
creds = _get_credentials()
print('SUCCESS' if creds else 'FAILED')
"
```

The token is saved to `Engine/.credentials/google_token.json` and auto-refreshes.

---

## 10. Build the Swift Audio Capture Binary

```bash
cd ~/MeetingNotes_RT/Engine/CaptureAudio
swift build -c release
```

Install the binary:

```bash
mkdir -p ~/MeetingNotes_RT/Engine/.bin
cp .build/release/CaptureAudio ~/MeetingNotes_RT/Engine/.bin/capture-audio
```

---

## 11. Obsidian (Transcript Viewer)

Install [Obsidian](https://obsidian.md/) to browse and search your meeting transcripts:

```bash
brew install --cask obsidian
```

Create the vault:

```bash
mkdir -p ~/MeetingNotes_RT/transcripts/.obsidian
```

Then open Obsidian → **Open folder as vault** → select `~/MeetingNotes_RT/transcripts`.

---

## 12. macOS Permissions

The app needs several permissions. macOS will prompt you the first time each is needed:

| Permission | Why | When prompted |
|---|---|---|
| **Microphone** | Record your side of calls | First recording start |
| **Screen Recording** or **System Audio Recording** | Capture meeting audio from Zoom, Meet, etc. via ScreenCaptureKit | First recording start |
| **Accessibility** | Read window titles for call detection | First app launch |
| **Notifications** | Show recording/transcription alerts | First app launch |

**To manage later:** System Settings → Privacy & Security → [category]

> **Note:** The "Screen Recording" permission is required for system audio capture. On macOS 15+, this may appear as "System Audio Recording Only" under Privacy & Security. Grant it to Terminal (or whichever app launches MeetingNotes). Without this permission, only your microphone will be captured — you won't hear the other side of calls in the transcript.

---

## 13. Run the App

```bash
cd ~/MeetingNotes_RT
source Engine/.venv/bin/activate
python Engine/app/menubar.py
```

You should see a microphone icon (🎙) in your menu bar.

Or just double-click `LaunchMeetingNotes.command` in Finder.

---

## 14. First-Run Checklist

- [ ] Menu bar icon appears (🎙)
- [ ] Edit `Settings/context.md` with your role, team, and meeting info
- [ ] Open a Google Meet in Chrome to test call detection
- [ ] Try a manual recording start/stop from the menu bar
- [ ] Check `Engine/recordings/queue/` for the recorded `.wav` file
- [ ] After recording stops, transcription should auto-start (⏳ icon)
- [ ] Check `transcripts/` for the generated `.md` file
- [ ] Or transcribe manually: `python -m app.pipeline Engine/recordings/queue/<filename>.wav`

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `MEETINGNOTES_HOME` | `~/MeetingNotes_RT` | Base directory for the project |
| `ANTHROPIC_API_KEY` | (none) | Claude API key for summarization |

---

## Troubleshooting

**"Permission denied" errors:** Check System Settings → Privacy & Security.

**Menu bar icon doesn't appear:** Make sure your virtual environment is activated (`source Engine/.venv/bin/activate`) and rumps is installed (`pip list | grep rumps`).

**No audio in recording:** Check System Settings → Sound → Input. System audio capture requires the "Screen Recording" (or "System Audio Recording") permission — go to System Settings → Privacy & Security and ensure Terminal is enabled.

**Swift binary won't compile:** Ensure Xcode Command Line Tools are installed. Requires macOS 14+.

**Transcription not working:** Ensure `parakeet-mlx` is installed (`pip list | grep parakeet`). The model downloads on first use (~2.5 GB).

**Claude summarization fails:** Check `ANTHROPIC_API_KEY` is set. If using Ollama fallback, ensure `ollama serve` is running.

**Summary says "unavailable":** Transcription succeeded but summarization failed. Check `Engine/logs/app.log`. The transcript is still saved with raw text.

**setup.command failed partway through:** Re-run — it skips completed steps and resumes.

**Apple Silicon required:** MeetingNotes requires an M-series Mac for Metal GPU acceleration.
