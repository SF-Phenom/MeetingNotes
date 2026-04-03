# Check-in Prompt Template

This is a static reference showing the format of the prompt that
`app/checkin.py` generates automatically. The actual prompt is built
dynamically from your real data and copied to the clipboard via the
menubar app.

To customize the prompt, edit `generate_checkin_prompt()` in
`app/checkin.py`.

---

## Prompt Template (with placeholders)

```
Please help me update my project knowledge base based on recent meetings.

Context about my role and team:
[contents of context.md]

New transcripts since last check-in ({last_checkin_date | "never"}):
- {YYYY-MM-DD} — {Meeting Title} ({relative/path/from/MeetingNotes/})
- {YYYY-MM-DD} — {Meeting Title} ({relative/path/from/MeetingNotes/})
...

Current project files:
- {Project Title} ({relative/path/from/MeetingNotes/})
...
(or "No project files yet." if the projects/ directory is empty)

Please:
1. Read each of the transcript files listed above
2. Identify which existing projects each new meeting relates to
3. Identify any new projects or initiatives that appear to have emerged
4. Note my specific contributions and any accomplishments worth logging
5. Flag anything you're uncertain about and ask me to clarify
6. Summarize proposed updates by project before making any changes

After I confirm, update the project .md files accordingly.
```

---

## Trigger Conditions

A check-in is suggested when either of these is true:

| Condition | Threshold |
|-----------|-----------|
| Transcripts since last check-in | ≥ 6 |
| Days since last check-in (with ≥ 1 new transcript) | ≥ 14 |

On first use (no previous check-in), only the transcript count applies.

## State Fields

Tracked in `state.json`:

- `transcripts_since_checkin` — incremented each time a new transcript is
  saved; reset to 0 when a check-in is marked complete.
- `last_checkin_date` — ISO date string (`YYYY-MM-DD`) of the last completed
  check-in, or `null` if none.
