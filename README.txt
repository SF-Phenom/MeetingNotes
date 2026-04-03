MeetingNotes
============

MeetingNotes automatically captures, transcribes, and summarizes your
meetings. Audio is transcribed locally on your Mac using GPU acceleration
-- recordings never leave your machine.

Summarization works in two modes:

  - If you have an Anthropic API key, transcript text (never audio) is
    sent to Claude for high-quality summaries.
  - If no API key is set, Claude is unreachable, or you're out of
    tokens, summaries fall back to Qwen, a local model that runs
    entirely on your Mac.


HOW TO USE
----------

Launch:
  Double-click "LaunchMeetingNotes.command" in this folder.
  A microphone icon will appear in your menu bar.

The app watches for Zoom, Google Meet, and other calls. When it
detects an active call, it asks if you'd like to record. When the
call ends, it automatically transcribes and summarizes the meeting.

You can also start and stop recording manually from the menu bar.

View transcripts:
  Open Obsidian, then open the "transcripts" folder in this directory
  as a vault. Transcripts are organized by year and month.

Check for updates:
  Click the menu bar icon and select "Check for Updates" to pull the
  latest version from GitHub.


SETTINGS FOLDER
---------------

The "Settings" folder contains files you can customize:

  context.md
    Your professional context -- your name, role, team members, and
    regular meetings. The AI uses this to write better summaries and
    correctly spell names. Fill this out before your first meeting.

  summarize.md
    Instructions for how Claude Code summarizes transcripts when you
    run meeting summaries manually. You can adjust the format,
    what details to include, or how action items are structured.

  checkin-prompt-template.md
    Reference for the periodic check-in prompt that helps you update
    your project knowledge base. The actual prompt is generated
    automatically -- this file shows the template and explains when
    check-ins are triggered.


PROJECTS FOLDER
---------------

The "projects" folder stores project knowledge files that are built
up over time through check-in sessions. When the app detects you have
enough new transcripts, it will suggest a check-in to update your
project files with insights from recent meetings.


OTHER FILES
-----------

  Engine/              All application internals. You should not need
                       to modify anything in this folder.

  uninstall.command    Removes MeetingNotes and its dependencies.
                       Your transcripts and projects are preserved.


PERMISSIONS
-----------

On first launch, macOS will ask for three permissions:

  Microphone       Required to capture meeting audio.
  Accessibility    Required to detect active calls (Zoom, Meet, etc).
  Notifications    Optional, for transcription-complete alerts.

Grant these when prompted. If you accidentally deny one, go to
System Settings > Privacy & Security to re-enable it.


TROUBLESHOOTING
---------------

App won't launch:
  Make sure you've run setup first (Engine/setup.command).

No transcription after a meeting:
  Check that recordings appear in Engine/recordings/queue/.
  If empty, the audio capture may not have permissions.

Summaries say "unavailable":
  If you have an API key, verify it's set. Open Terminal and run:
    echo $ANTHROPIC_API_KEY
  It should start with "sk-ant-". Without an API key, summaries
  use the local Qwen model -- if those also fail, the transcript
  is still saved and can be summarized later.

For more help, see Engine/SETUP.md or contact the person who
shared this app with you.
