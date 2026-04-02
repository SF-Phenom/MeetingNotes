#!/bin/zsh
# Double-click this file in Finder to launch MeetingNotes.
# The menubar app runs in the background — Terminal will close automatically.

cd "${MEETINGNOTES_HOME:-$HOME/MeetingNotes}"
source .venv/bin/activate
nohup python app/menubar.py &>/dev/null &

# Close this Terminal window
osascript -e 'tell application "Terminal" to close front window' &>/dev/null &
