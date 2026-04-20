# MeetingNotes — Setup Guide

**Requirements:** macOS on Apple Silicon (M1/M2/M3/M4).

## Quick Start (Recommended)

Open **Terminal** (Applications → Utilities → Terminal) and paste this one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/SF-Phenom/MeetingNotes/main/install.sh | zsh
```

It downloads the setup script, runs it, and takes 5–15 minutes on a fresh machine
(mostly the ~2.5 GB Parakeet model download). The install is idempotent — safe
to re-run if anything goes sideways.

You'll be prompted for a few things along the way: your macOS password (for Homebrew),
and your Anthropic API key if you have one (optional — the app falls back to a local
model when Claude isn't available).

### Already have the repo cloned?

If you got the source via `git clone` rather than a file transfer, you can skip the
bootstrap and run the setup script directly:

```bash
cd ~/MeetingNotes
./Engine/setup.command
```

Or double-click `Engine/setup.command` in Finder.

### Why a curl installer?

When `setup.command` travels as a file (email, Slack, AirDrop, direct download),
macOS strips its executable bit and flags it with a Gatekeeper quarantine —
double-clicking fails, "Run Anyway" doesn't fully rescue it. The `curl | zsh`
route sidesteps both issues by pulling the script fresh from GitHub and running
it without ever touching Finder. **This is the path most users should take.**

If you prefer to install manually, or if the script hits an issue, follow the
step-by-step guide below.

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
cd ~/MeetingNotes
python3.12 -m venv Engine/.venv
source Engine/.venv/bin/activate
```

Your terminal prompt should now show `(.venv)`. Install the dependencies:

```bash
pip install -r Engine/requirements.txt
```

**Every time you open a new terminal** to work with MeetingNotes, activate the environment first:

```bash
source ~/MeetingNotes/Engine/.venv/bin/activate
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

Auto-populates the transcript title and participants from your Google Calendar. If you skip this, the pipeline still works — you just won't get calendar metadata.

**The app ships with a shared OAuth client** (a Desktop-app credential from the MeetingNotes Google Cloud project). `setup.command` copies it to `Engine/.credentials/google_oauth_client.json` automatically on first install. You sign in with your own Google account; your personal refresh token lives only in `Engine/.credentials/google_token.json` and never leaves your Mac.

**Signing in:** `setup.command` asks once during install. You can also trigger it any time from the menubar → **Calendar ✓ → Re-authenticate** (or **Sign in to Google Calendar** if you haven't signed in yet).

**Advanced: using your own OAuth client.** If you'd rather point at your own Google Cloud project:

1. Create an OAuth 2.0 "Desktop app" credential in Google Cloud Console with the Calendar API enabled.
2. Replace the file:
   ```bash
   cp /path/to/your/client_secret_XXXXX.json ~/MeetingNotes/Engine/.credentials/google_oauth_client.json
   ```
3. From the menubar → **Calendar ✓ → Re-authenticate** to pick up the new client.

`Engine/.credentials/` is gitignored, so your replacement stays local.

---

## 10. Build the Swift Audio Capture Binary

```bash
cd ~/MeetingNotes/Engine/CaptureAudio
swift build -c release
```

Install the binary:

```bash
mkdir -p ~/MeetingNotes/Engine/.bin
cp .build/release/CaptureAudio ~/MeetingNotes/Engine/.bin/capture-audio
codesign -s - --force ~/MeetingNotes/Engine/.bin/capture-audio
```

The `codesign` step replaces the linker-provided signature with a proper ad-hoc
one so the embedded `Info.plist` (including the Audio Capture and Microphone
usage descriptions) is hashed into the CodeDirectory. Without it, macOS may
attribute TCC prompts to Terminal / python3.12 instead of MeetingNotes.

---

## 11. Apple Speech Transcription (Optional)

Build the SpeechTranscribe binary for on-device Apple Speech recognition.
Requires macOS 26+ and Xcode 26+ (Swift 6.2).

```bash
cd ~/MeetingNotes/Engine/SpeechTranscribe
swift build -c release
```

Install into the app bundle:

```bash
cp .build/release/SpeechTranscribe ~/MeetingNotes/Engine/.bin/SpeechTranscribe.app/Contents/MacOS/speech-transcribe
codesign --sign - --force --deep ~/MeetingNotes/Engine/.bin/SpeechTranscribe.app
```

**Required macOS settings:**
- System Settings → General → Keyboard → Dictation → **Enable Dictation** (downloads the on-device speech model)
- On first run, grant Speech Recognition permission when prompted

Select "Apple Speech" in the menubar → Transcription Engine submenu.

---

## 12. Obsidian (Transcript Viewer)

Install [Obsidian](https://obsidian.md/) to browse and search your meeting transcripts:

```bash
brew install --cask obsidian
```

Create the vault:

```bash
mkdir -p ~/MeetingNotes/transcripts/.obsidian
```

Then open Obsidian → **Open folder as vault** → select `~/MeetingNotes/transcripts`.

---

## 13. macOS Permissions

The app needs several permissions. macOS will prompt you the first time each is needed:

| Permission | Why | When prompted |
|---|---|---|
| **Microphone** | Record your side of calls | First recording start |
| **Audio Capture** | Capture meeting audio from Zoom, Meet, FaceTime, etc. via the CoreAudio Process Tap | First recording start |
| **Accessibility** | Read window titles for call detection | First app launch |
| **Notifications** | Show recording/transcription alerts | First app launch |

**To manage later:** System Settings → Privacy & Security → [category]

> **Note on Audio Capture:** this is a separate permission from "Screen Recording", introduced in macOS 14.2 alongside the CoreAudio Process Tap API. MeetingNotes no longer uses ScreenCaptureKit for system audio. The prompt is attributed to MeetingNotes directly (via the embedded `Info.plist` in `capture-audio`) — grant it there, not to Terminal. Without this permission, only your microphone will be captured — you won't hear the other side of calls in the transcript.
>
> From the menubar, "⚠ System audio unavailable — grant Audio Capture" jumps straight to the right pane (`x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture`).

---

## 14. Run the App

```bash
cd ~/MeetingNotes
source Engine/.venv/bin/activate
python Engine/app/menubar.py
```

You should see a microphone icon (🎙) in your menu bar.

Or just double-click `LaunchMeetingNotes.command` in Finder.

---

## 15. First-Run Checklist

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
| `MEETINGNOTES_HOME` | `~/MeetingNotes` | Base directory for the project |
| `ANTHROPIC_API_KEY` | (none) | Claude API key for summarization |

---

## Troubleshooting

**"Permission denied" errors:** Check System Settings → Privacy & Security.

**Menu bar icon doesn't appear:** Make sure your virtual environment is activated (`source Engine/.venv/bin/activate`) and rumps is installed (`pip list | grep rumps`).

**No audio in recording:** Check System Settings → Sound → Input. System audio capture requires the "Audio Capture" permission (System Settings → Privacy & Security → Audio Capture) — ensure MeetingNotes is enabled there. If you only see Terminal / python3.12 in the list, the binary is missing its ad-hoc signature: re-run `Engine/setup.command` or `codesign -s - --force ~/MeetingNotes/Engine/.bin/capture-audio`.

**Swift binary won't compile:** Ensure Xcode Command Line Tools are installed. Requires macOS 14+.

**Transcription not working:** Ensure `parakeet-mlx` is installed (`pip list | grep parakeet`). The model downloads on first use (~2.5 GB).

**Claude summarization fails:** Check `ANTHROPIC_API_KEY` is set. If using Ollama fallback, ensure `ollama serve` is running.

**Summary says "unavailable":** Transcription succeeded but summarization failed. Check `Engine/logs/app.log`. The transcript is still saved with raw text.

**setup.command failed partway through:** Re-run — it skips completed steps and resumes.

**Apple Silicon required:** MeetingNotes requires an M-series Mac for Metal GPU acceleration.
