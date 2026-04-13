#!/bin/zsh
set -euo pipefail

# ============================================================
# MeetingNotes — Automated Setup
# Double-click this file in Finder, or run: ./setup.command
# Safe to re-run — skips anything already installed.
# ============================================================

# -- Constants ------------------------------------------------
BASE_DIR="${MEETINGNOTES_HOME:-$HOME/MeetingNotes_RT}"
ENGINE_DIR="$BASE_DIR/Engine"
CAPTURE_BINARY="$ENGINE_DIR/.bin/capture-audio"
VENV_DIR="$ENGINE_DIR/.venv"
REPO_URL="https://github.com/SF-Phenom/MeetingNotes.git"
SCRIPT_VERSION="2"
CURRENT_STEP=0
TOTAL_STEPS=13

# -- Colors ---------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

# -- Helpers --------------------------------------------------
info()    { echo "${BLUE}==>${RESET} $1" }
success() { echo "${GREEN}  ✓${RESET} $1" }
already() { echo "${GREEN}  ✓${RESET} $1 ${YELLOW}(already installed)${RESET}" }
warn()    { echo "${YELLOW}  !${RESET} $1" }
error()   { echo "${RED}  ✗${RESET} $1" >&2 }

step() {
    CURRENT_STEP=$1
    echo ""
    echo "${BOLD}[$1/$TOTAL_STEPS] $2${RESET}"
}

fail() {
    error "$1"
    exit 1
}

confirm() {
    local reply
    echo -n "${BLUE}==>${RESET} $1 [y/N] "
    read -r reply
    [[ "$reply" =~ ^[Yy] ]]
}

# -- Trap -----------------------------------------------------
cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        echo ""
        error "Setup failed at step $CURRENT_STEP."
        info "Fix the issue above, then re-run setup.command to resume."
    fi
}
trap cleanup EXIT

# ============================================================
# WELCOME
# ============================================================
echo ""
echo "${BOLD}════════════════════════════════════════${RESET}"
echo "${BOLD}  MeetingNotes Setup${RESET}"
echo "${BOLD}════════════════════════════════════════${RESET}"
echo ""
echo "  This script installs everything needed to run MeetingNotes,"
echo "  a local meeting capture and transcription system."
echo ""
echo "  ${BOLD}What gets installed:${RESET}"
echo ""
echo "  ${BLUE}Dependencies${RESET}"
echo "    Homebrew (macOS package manager)"
echo "    Python 3.12, ffmpeg"
echo ""
echo "  ${BLUE}Transcription engine${RESET}"
echo "    Parakeet — runs speech-to-text locally on your Mac"
echo "    using GPU acceleration via MLX. Audio never leaves your"
echo "    machine. Includes a ~2.5GB model download."
echo ""
echo "  ${BLUE}Summarization${RESET}"
echo "    Claude API — sends transcript text (never audio) to"
echo "    Claude for meeting summaries and action items."
echo "    Requires an API key from Anthropic."
echo ""
echo "  ${BLUE}Apps${RESET}"
echo "    Obsidian — for browsing and searching transcripts"
echo ""
echo "  Takes 10-20 minutes on a fresh machine."
echo ""

if ! confirm "Ready to start?"; then
    echo "  No problem. Run this script again when you're ready."
    exit 0
fi

# ============================================================
# PRE-FLIGHT CHECKS
# ============================================================
echo ""
echo "${BOLD}Pre-flight checks${RESET}"
echo "─────────────────────────────────"

# macOS only
if [[ "$(uname)" != "Darwin" ]]; then
    fail "MeetingNotes requires macOS. Detected: $(uname)"
fi

# Apple Silicon
if [[ "$(uname -m)" != "arm64" ]]; then
    fail "MeetingNotes requires Apple Silicon (M1/M2/M3/M4) for Metal GPU acceleration. Detected: $(uname -m)"
fi

# Repo presence (will be cloned in step 1 if missing)
repo_present=false
if [[ -f "$ENGINE_DIR/app/menubar.py" ]]; then
    repo_present=true
fi

# Internet check
if ! curl -s --max-time 5 -o /dev/null https://brew.sh 2>/dev/null; then
    fail "No internet connection detected. Setup requires internet for downloads."
fi

# Disk space warning
available_gb=$(df -g "$HOME" | awk 'NR==2 {print $4}')
if [[ "$available_gb" -lt 5 ]]; then
    warn "Low disk space: ${available_gb}GB available. Setup needs ~3GB (Parakeet model + builds)."
    if ! confirm "Continue anyway?"; then
        exit 0
    fi
fi

success "Pre-flight checks passed"

# ============================================================
# STEP 1: Clone MeetingNotes Repository
# ============================================================
step 1 "Clone MeetingNotes repository"

if $repo_present; then
    already "MeetingNotes repo at $BASE_DIR"
else
    # git requires Xcode CLI tools — install inline if needed
    if ! xcode-select -p &>/dev/null; then
        info "Installing Xcode Command Line Tools (needed for git)..."
        info "A dialog will appear — click 'Install' and wait for it to finish."
        xcode-select --install 2>/dev/null || true

        elapsed=0
        while ! xcode-select -p &>/dev/null; do
            if [[ $elapsed -ge 600 ]]; then
                fail "Timed out waiting for Xcode CLI tools. Please install manually and re-run."
            fi
            sleep 5
            elapsed=$((elapsed + 5))
        done
        success "Xcode CLI tools installed"
    fi

    info "Cloning MeetingNotes from GitHub..."
    if ! git clone "$REPO_URL" "$BASE_DIR"; then
        fail "Failed to clone repository. Check your internet connection and try again."
    fi
    success "MeetingNotes cloned to $BASE_DIR"
    repo_present=true

    # Self-update: if the repo's setup.command is newer, re-exec it
    repo_script="$ENGINE_DIR/setup.command"
    if [[ -f "$repo_script" ]]; then
        repo_version=$(grep -m1 '^SCRIPT_VERSION=' "$repo_script" 2>/dev/null | cut -d'"' -f2)
        if [[ -n "$repo_version" && "$repo_version" != "$SCRIPT_VERSION" ]]; then
            info "Repo contains a newer setup.command (v$repo_version). Switching to it..."
            exec "$repo_script" "$@"
        fi
    fi
fi

# ============================================================
# STEP 2: Xcode Command Line Tools
# ============================================================
step 2 "Xcode Command Line Tools"

if xcode-select -p &>/dev/null; then
    already "Xcode CLI tools"
else
    info "Installing Xcode Command Line Tools..."
    info "A dialog will appear — click 'Install' and wait for it to finish."
    xcode-select --install 2>/dev/null || true

    # Poll until installed (up to 10 minutes)
    elapsed=0
    while ! xcode-select -p &>/dev/null; do
        if [[ $elapsed -ge 600 ]]; then
            fail "Timed out waiting for Xcode CLI tools. Please install manually and re-run."
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    success "Xcode CLI tools installed"
fi

# ============================================================
# STEP 3: Homebrew
# ============================================================
step 3 "Homebrew"

if command -v brew &>/dev/null; then
    already "Homebrew"
else
    info "Installing Homebrew..."
    echo ""
    warn "macOS will ask for your login password (the one you use to unlock your Mac)."
    warn "When you type, nothing will appear on screen — that's normal."
    warn "Type your password and press Enter."
    echo ""
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    # Ensure brew is on PATH for this session
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi

    # Add to .zprofile if not already there
    if ! grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null; then
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
    fi

    if ! command -v brew &>/dev/null; then
        fail "Homebrew installed but not on PATH. Restart your terminal and re-run."
    fi
    success "Homebrew installed"
fi

# ============================================================
# STEP 4: Brew Packages (Python 3.12, ffmpeg)
# ============================================================
step 4 "Brew packages (Python 3.12, ffmpeg)"

missing_pkgs=()
if ! command -v python3.12 &>/dev/null; then missing_pkgs+=(python@3.12); fi
if ! command -v ffmpeg &>/dev/null;     then missing_pkgs+=(ffmpeg); fi

if [[ ${#missing_pkgs[@]} -eq 0 ]]; then
    already "python3.12, ffmpeg"
else
    info "Installing: ${missing_pkgs[*]}..."
    brew install "${missing_pkgs[@]}"
    success "Brew packages installed"
fi

# ============================================================
# STEP 5: Python Virtual Environment + pip
# ============================================================
step 5 "Python virtual environment + packages"

need_venv=false
need_packages=false

if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    need_venv=true
    need_packages=true
elif ! "$VENV_DIR/bin/python" --version 2>/dev/null | grep -q "3.12"; then
    warn "Existing venv uses wrong Python version."
    if confirm "Recreate venv with Python 3.12?"; then
        rm -rf "$VENV_DIR"
        need_venv=true
        need_packages=true
    fi
fi

if $need_venv; then
    info "Creating virtual environment..."
    python3.12 -m venv "$VENV_DIR"
    success "Virtual environment created"
fi

# Check if packages are installed
if ! $need_packages && ! "$VENV_DIR/bin/python" -c "import rumps, psutil, anthropic" &>/dev/null; then
    need_packages=true
fi

if $need_packages; then
    info "Installing Python packages..."
    "$VENV_DIR/bin/pip" install --quiet -r "$ENGINE_DIR/requirements.txt"
    success "Python packages installed"
else
    already "Python venv + packages"
fi

# Pre-download the Parakeet model (~2.5 GB, cached in ~/.cache/huggingface/)
if "$VENV_DIR/bin/python" -c "from parakeet_mlx import from_pretrained; from_pretrained('mlx-community/parakeet-tdt-0.6b-v3')" &>/dev/null; then
    already "Parakeet model"
else
    info "Downloading Parakeet model (~2.5 GB, this may take a few minutes)..."
    "$VENV_DIR/bin/python" -c "from parakeet_mlx import from_pretrained; from_pretrained('mlx-community/parakeet-tdt-0.6b-v3')"
    success "Parakeet model downloaded"
fi

# ============================================================
# STEP 6: Swift Audio Capture Binary
# ============================================================
step 6 "Swift audio capture binary"

if [[ -x "$CAPTURE_BINARY" ]]; then
    already "capture-audio binary"
else
    info "Building CaptureAudio (Swift)..."
    # Build in /tmp to avoid cloud sync (Google Drive/iCloud) locking build files
    build_tmp=$(mktemp -d)
    cd "$ENGINE_DIR/CaptureAudio"
    swift build -c release --scratch-path "$build_tmp" 2>&1 | tail -5 || true

    if [[ ! -f "$build_tmp/release/CaptureAudio" ]]; then
        fail "Swift build failed. Ensure macOS 14+ and Xcode CLI tools are installed."
    fi

    mkdir -p "$ENGINE_DIR/.bin"
    cp "$build_tmp/release/CaptureAudio" "$CAPTURE_BINARY"
    rm -rf "$build_tmp"
    cd "$BASE_DIR"
    success "capture-audio built and installed"
fi

# ============================================================
# STEP 7: Directory Structure
# ============================================================
step 7 "Directory structure"

mkdir -p "$ENGINE_DIR/recordings/active"
mkdir -p "$ENGINE_DIR/recordings/queue"
mkdir -p "$BASE_DIR/transcripts"
mkdir -p "$BASE_DIR/projects"
mkdir -p "$BASE_DIR/Settings"
mkdir -p "$ENGINE_DIR/logs"
mkdir -p "$ENGINE_DIR/.bin"
mkdir -p "$ENGINE_DIR/.credentials"
success "Directories ready"

# ============================================================
# STEP 8: state.json
# ============================================================
step 8 "state.json"

if [[ -f "$ENGINE_DIR/state.json" ]]; then
    already "state.json"
else
    cat <<'EOF' > "$ENGINE_DIR/state.json"
{
  "transcripts_since_checkin": 0,
  "last_checkin_date": null,
  "suppressed_sources": [],
  "pending_deletion": [],
  "recording_active": false,
  "active_recording_path": null,
  "active_call_url": null,
  "active_call_source": null,
  "retain_recordings": false
}
EOF
    success "state.json created"
fi

# ============================================================
# STEP 9: Anthropic API Key
# ============================================================
step 9 "Anthropic API key"

api_key_set=false

# Check environment
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    api_key_set=true
# Check .zshrc
elif grep -q 'ANTHROPIC_API_KEY' "$HOME/.zshrc" 2>/dev/null; then
    source "$HOME/.zshrc" 2>/dev/null || true
    if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        api_key_set=true
    fi
fi

if $api_key_set; then
    already "ANTHROPIC_API_KEY (${ANTHROPIC_API_KEY:0:12}...)"
else
    info "An API key from https://console.anthropic.com/ enables"
    info "higher-quality Claude summaries. Without one, summaries"
    info "use the local Qwen model (which still works well)."
    echo ""

    if confirm "Do you have an Anthropic API key?"; then
        attempts=0
        while [[ $attempts -lt 3 ]]; do
            echo -n "  Paste your ANTHROPIC_API_KEY: "
            read -r api_key
            if [[ "$api_key" == sk-ant-* ]]; then
                echo "export ANTHROPIC_API_KEY=\"$api_key\"" >> "$HOME/.zshrc"
                export ANTHROPIC_API_KEY="$api_key"
                success "API key saved to ~/.zshrc"
                break
            else
                warn "Key should start with 'sk-ant-'. Try again (or press Ctrl+C to skip)."
                attempts=$((attempts + 1))
            fi
        done

        if [[ $attempts -ge 3 ]]; then
            warn "Skipping API key. You can add one later from the MeetingNotes menu bar."
        fi
    else
        info "No problem. Summaries will use the local Qwen model."
        info "You can add an API key later from the MeetingNotes menu bar."
    fi
fi

# ============================================================
# STEP 11: Obsidian
# ============================================================
step 10 "Obsidian (transcript viewer)"

if [[ -d "/Applications/Obsidian.app" ]] || brew list --cask obsidian &>/dev/null 2>&1; then
    already "Obsidian"
else
    info "Installing Obsidian..."
    brew install --cask obsidian
    success "Obsidian installed"
fi

# Create vault at transcripts/
if [[ -d "$BASE_DIR/transcripts/.obsidian" ]]; then
    already "Obsidian vault at transcripts/"
else
    mkdir -p "$BASE_DIR/transcripts/.obsidian"
    success "Obsidian vault created at $BASE_DIR/transcripts/"
    info "Open Obsidian → 'Open folder as vault' → select $BASE_DIR/transcripts"
fi

# ============================================================
# STEP 12: Ollama (local AI for summaries)
# ============================================================
step 11 "Ollama (local AI for summaries)"

if command -v ollama &>/dev/null || [[ -d "/Applications/Ollama.app" ]]; then
    already "Ollama"
else
    info "Installing Ollama (runs AI models locally on your Mac)..."
    brew install --cask ollama
    success "Ollama installed"
fi

# Ensure Ollama is running (needed to pull models)
if ! curl -s --max-time 3 -o /dev/null http://localhost:11434/api/tags 2>/dev/null; then
    info "Starting Ollama..."
    open -a Ollama 2>/dev/null || ollama serve &>/dev/null &
    # Wait for it to be ready
    elapsed=0
    while ! curl -s --max-time 2 -o /dev/null http://localhost:11434/api/tags 2>/dev/null; do
        if [[ $elapsed -ge 30 ]]; then
            warn "Ollama did not start in time. You may need to open Ollama manually and re-run."
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
fi

# Pull Qwen model
if ollama list 2>/dev/null | grep -q "qwen3:4b"; then
    already "Qwen model (qwen3:4b)"
else
    info "Downloading Qwen model (qwen3:4b, ~2.5GB)..."
    info "This powers local meeting summaries when Claude is not available."
    ollama pull qwen3:4b
    success "Qwen model downloaded"
fi

# ============================================================
# STEP 13: Google Calendar (Optional)
# ============================================================
step 12 "Google Calendar integration (optional)"

if [[ -f "$ENGINE_DIR/.credentials/google_token.json" ]]; then
    already "Google Calendar authenticated"
elif [[ ! -f "$ENGINE_DIR/.credentials/google_oauth_client.json" ]]; then
    warn "OAuth client JSON not found at Engine/.credentials/google_oauth_client.json"
    info "Skipped. See Engine/SETUP.md section 7b for details."
else
    if confirm "Set up Google Calendar integration? (auto-populates meeting names)"; then
        info "Authenticating — a browser window will open. Sign in with your Google account."
        cd "$ENGINE_DIR"
        "$VENV_DIR/bin/python" -c "
import sys; sys.path.insert(0, '.')
from app.calendar_lookup import _get_credentials
creds = _get_credentials()
print('SUCCESS' if creds else 'FAILED')
" && success "Google Calendar authenticated" || warn "Authentication failed. You can retry later by re-running setup.command."
    else
        info "Skipped. Re-run setup.command anytime to set this up."
    fi
fi

# ============================================================
# FINAL VERIFICATION
# ============================================================
echo ""
echo "${BOLD}Verification${RESET}"
echo "─────────────────────────────────"

pass=0
total=0

check() {
    total=$((total + 1))
    if eval "$2" &>/dev/null; then
        success "$1"
        pass=$((pass + 1))
    else
        error "$1"
    fi
}

check "MeetingNotes repo"       "test -f $ENGINE_DIR/app/menubar.py"
check "Xcode CLI tools"        "xcode-select -p"
check "Homebrew"                "command -v brew"
check "Python 3.12"             "command -v python3.12"
check "ffmpeg"                  "command -v ffmpeg"
check "Python venv"             "test -f $VENV_DIR/bin/python"
check "Python packages"         "$VENV_DIR/bin/python -c 'import rumps, psutil, anthropic'"
check "capture-audio"           "test -x $CAPTURE_BINARY"
check "Ollama"                  "command -v ollama"
check "Qwen model"             "ollama list 2>/dev/null | grep -q qwen3:4b"
check "Obsidian"                "test -d /Applications/Obsidian.app"
check "Obsidian vault"          "test -d $BASE_DIR/transcripts/.obsidian"
check "Directory structure"     "test -d $ENGINE_DIR/recordings/queue"
check "state.json"              "test -f $ENGINE_DIR/state.json"

echo ""
if [[ $pass -eq $total ]]; then
    echo "${GREEN}${BOLD}All $total checks passed!${RESET}"
else
    echo "${YELLOW}${BOLD}$pass/$total checks passed.${RESET} Review any failures above."
fi

# ============================================================
# DONE
# ============================================================
echo ""
echo "${BOLD}════════════════════════════════════════${RESET}"
echo "${BOLD}  MeetingNotes setup complete!${RESET}"
echo "${BOLD}════════════════════════════════════════${RESET}"
echo ""
echo "  ${BOLD}To launch:${RESET}"
echo "    Double-click ${BLUE}LaunchMeetingNotes.command${RESET} in Finder"
echo "    (in $BASE_DIR/)"
echo ""
echo "  ${BOLD}To view transcripts:${RESET}"
echo "    Open Obsidian → 'Open folder as vault' → $BASE_DIR/transcripts"
echo ""
echo "  ${BOLD}First time?${RESET}"
echo "    Edit ${BLUE}$BASE_DIR/Settings/context.md${RESET} with your role, team, and meeting info."
echo "    macOS will prompt for Microphone, Screen Recording (system audio),"
echo "    Accessibility, and Notification permissions on first use."
echo ""
