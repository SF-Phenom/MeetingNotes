#!/bin/zsh
# Double-click this file in Finder to launch MeetingNotes.
# The menubar app runs in the background — Terminal will close automatically.

BASE_DIR="${MEETINGNOTES_HOME:-$HOME/MeetingNotes_RT}"
cd "$BASE_DIR" || {
    osascript -e 'display dialog "MeetingNotes folder not found at '"$BASE_DIR"'" buttons {"OK"} with icon stop' &>/dev/null
    exit 1
}

if [[ ! -f Engine/.venv/bin/activate ]]; then
    osascript -e 'display dialog "MeetingNotes is not set up yet.\n\nDouble-click Engine/setup.command first." buttons {"OK"} with icon stop' &>/dev/null
    exit 1
fi

if [[ ! -f Engine/app/menubar.py ]]; then
    osascript -e 'display dialog "menubar.py not found. The installation may be corrupted.\n\nTry running Engine/setup.command again." buttons {"OK"} with icon stop' &>/dev/null
    exit 1
fi

source Engine/.venv/bin/activate

# Launch in background, logging to Engine/logs/launch.log
mkdir -p Engine/logs
nohup python Engine/app/menubar.py >> Engine/logs/launch.log 2>&1 &
APP_PID=$!

# Brief pause to verify the process started
sleep 1
if kill -0 "$APP_PID" 2>/dev/null; then
    # Close this Terminal window
    osascript -e 'tell application "Terminal" to close front window' &>/dev/null &
else
    osascript -e 'display dialog "MeetingNotes failed to start.\n\nCheck Engine/logs/launch.log for details." buttons {"OK"} with icon stop' &>/dev/null
    exit 1
fi
