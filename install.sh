#!/bin/zsh
#
# Bootstrap installer for MeetingNotes.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/SF-Phenom/MeetingNotes/main/install.sh | zsh
#
# Downloads Engine/setup.command to a temp file, marks it executable,
# and re-execs it with /dev/tty as stdin so setup.command's interactive
# prompts still work when this bootstrap was itself piped from curl.
#
# Why this exists: if a user receives setup.command as a raw file
# (email, Slack, AirDrop, direct download), macOS strips the executable
# bit on the transfer and attaches a Gatekeeper quarantine attribute.
# Double-clicking fails "permission denied"; `sudo /path/to/setup.command`
# fails "command not found" (not a path problem — there's no exec bit
# to honor). This bootstrap sidesteps both issues by pulling a fresh
# copy via curl (no quarantine because Terminal-initiated) and chmoding
# in code.
#
# Users who already have the repo cloned can bypass this entirely and
# run Engine/setup.command directly.
#
set -euo pipefail

URL="https://raw.githubusercontent.com/SF-Phenom/MeetingNotes/main/Engine/setup.command"
DEST="/tmp/mn-setup.command"

echo "==> Fetching MeetingNotes setup script..."
if ! curl -fsSL -o "$DEST" "$URL"; then
    echo "Download failed. Check your internet connection and try again." >&2
    exit 1
fi
chmod +x "$DEST"

# Re-exec setup.command with /dev/tty as stdin so its confirm() prompts
# read from the terminal rather than this bootstrap's stdin (which is
# the curl pipe when invoked via `curl ... | zsh`).
exec "$DEST" < /dev/tty
