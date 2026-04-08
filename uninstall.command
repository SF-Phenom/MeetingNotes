#!/bin/zsh
set -uo pipefail

# ============================================================
# MeetingNotes — Uninstall
# Double-click this file in Finder, or run: ./uninstall.command
# ============================================================

BASE_DIR="${MEETINGNOTES_HOME:-$HOME/MeetingNotes_RT}"

# -- Colors ---------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo "${BLUE}==>${RESET} $1" }
success() { echo "${GREEN}  ✓${RESET} $1" }
warn()    { echo "${YELLOW}  !${RESET} $1" }
error()   { echo "${RED}  ✗${RESET} $1" >&2 }

confirm() {
    local reply
    echo -n "${BLUE}==>${RESET} $1 [y/N] "
    read -r reply
    [[ "$reply" =~ ^[Yy] ]]
}

# ============================================================
echo ""
echo "${BOLD}════════════════════════════════════════${RESET}"
echo "${BOLD}  MeetingNotes Uninstall${RESET}"
echo "${BOLD}════════════════════════════════════════${RESET}"
echo ""
echo "  This will remove MeetingNotes and its dependencies."
echo ""
echo "  ${GREEN}Kept:${RESET}  transcripts/, projects/, Settings/ (your data)"
echo "  ${GREEN}Kept:${RESET}  Obsidian vault config (.obsidian/)"
echo "  ${GREEN}Kept:${RESET}  Homebrew, Python, ffmpeg, cmake, Obsidian"
echo ""
echo "  ${RED}Removed:${RESET}  Engine/ (app code, whisper.cpp ~2.5GB, Python venv,"
echo "            Swift binary, logs, recordings, credentials)"
echo "  ${RED}Removed:${RESET}  ANTHROPIC_API_KEY from ~/.zshrc"
echo ""

if ! confirm "Continue with uninstall?"; then
    echo "  Cancelled."
    exit 0
fi

echo ""

# ============================================================
# Move user data out before deleting
# ============================================================
KEPT_DATA=false

if [[ -d "$BASE_DIR/transcripts" ]] && [[ -n "$(ls -A "$BASE_DIR/transcripts" 2>/dev/null)" ]]; then
    info "Moving transcripts to ~/MeetingNotes-data/transcripts..."
    mkdir -p "$HOME/MeetingNotes-data"
    if ! mv "$BASE_DIR/transcripts" "$HOME/MeetingNotes-data/transcripts"; then
        warn "Could not move transcripts — they remain at $BASE_DIR/transcripts"
    else
        KEPT_DATA=true
        success "Transcripts preserved"
    fi
fi

if [[ -d "$BASE_DIR/projects" ]] && [[ -n "$(ls -A "$BASE_DIR/projects" 2>/dev/null)" ]]; then
    info "Moving projects to ~/MeetingNotes-data/projects..."
    mkdir -p "$HOME/MeetingNotes-data"
    if ! mv "$BASE_DIR/projects" "$HOME/MeetingNotes-data/projects"; then
        warn "Could not move projects — they remain at $BASE_DIR/projects"
    else
        KEPT_DATA=true
        success "Projects preserved"
    fi
fi

if [[ -d "$BASE_DIR/Settings" ]]; then
    info "Moving Settings to ~/MeetingNotes-data/Settings..."
    mkdir -p "$HOME/MeetingNotes-data"
    if ! mv "$BASE_DIR/Settings" "$HOME/MeetingNotes-data/Settings"; then
        warn "Could not move Settings — they remain at $BASE_DIR/Settings"
    else
        KEPT_DATA=true
        success "Settings preserved"
    fi
fi

# ============================================================
# Remove app directory
# ============================================================
if [[ -d "$BASE_DIR" ]]; then
    info "Removing $BASE_DIR..."
    rm -rf "$BASE_DIR"
    success "App directory removed"
else
    warn "App directory not found at $BASE_DIR"
fi

# ============================================================
# Remove whisper.cpp
# ============================================================
if [[ -d "$HOME/whisper.cpp" ]]; then
    info "Removing ~/whisper.cpp (~2.5GB)..."
    rm -rf "$HOME/whisper.cpp"
    success "whisper.cpp removed"
fi

# ============================================================
# Remove ANTHROPIC_API_KEY from ~/.zshrc
# ============================================================
if grep -q '^export ANTHROPIC_API_KEY=' "$HOME/.zshrc" 2>/dev/null; then
    info "Removing ANTHROPIC_API_KEY from ~/.zshrc..."
    cp "$HOME/.zshrc" "$HOME/.zshrc.bak"
    grep -v '^export ANTHROPIC_API_KEY=' "$HOME/.zshrc.bak" > "$HOME/.zshrc"
    if [[ $? -eq 0 ]]; then
        rm "$HOME/.zshrc.bak"
    else
        warn "Failed to update ~/.zshrc — backup at ~/.zshrc.bak"
    fi
    unset ANTHROPIC_API_KEY 2>/dev/null
    success "API key removed from shell config"
fi

# ============================================================
# Remove MEETINGNOTES_HOME from ~/.zshrc (if set)
# ============================================================
if grep -q '^export MEETINGNOTES_HOME=' "$HOME/.zshrc" 2>/dev/null; then
    info "Removing MEETINGNOTES_HOME from ~/.zshrc..."
    cp "$HOME/.zshrc" "$HOME/.zshrc.bak"
    grep -v '^export MEETINGNOTES_HOME=' "$HOME/.zshrc.bak" > "$HOME/.zshrc"
    if [[ $? -eq 0 ]]; then
        rm "$HOME/.zshrc.bak"
    else
        warn "Failed to update ~/.zshrc — backup at ~/.zshrc.bak"
    fi
    success "MEETINGNOTES_HOME removed from shell config"
fi

# ============================================================
# Done
# ============================================================
echo ""
echo "${BOLD}════════════════════════════════════════${RESET}"
echo "${BOLD}  Uninstall complete${RESET}"
echo "${BOLD}════════════════════════════════════════${RESET}"
echo ""
if $KEPT_DATA; then
    echo "  Your meeting data was saved to:"
    echo "    ${BLUE}~/MeetingNotes-data/${RESET}"
    echo ""
    echo "  You can open it in Obsidian or delete it when ready."
fi
echo ""
echo "  Homebrew, Python, ffmpeg, cmake, and Obsidian were"
echo "  left installed in case you use them for other things."
echo ""
