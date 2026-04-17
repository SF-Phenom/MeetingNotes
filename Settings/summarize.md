# Run Meeting Summaries

First, run this to see which transcripts need work:

```
python -m app.needs_summary
```

Then read `~/MeetingNotes/Settings/context.md` for my professional context.

For each transcript that needs summarization or re-summarization:

1. Read the full transcript file
2. Look at the "## Full Transcript" section for the raw meeting content
3. Generate a JSON object with these keys:
   - `title`: 4-6 word descriptive meeting title
   - `summary`: 3-5 sentence narrative summary of what was discussed and decided
   - `action_items`: list of `{"item": "description", "owner": "name or null", "due": "date or null"}`
   - `projects_mentioned`: list of project/initiative names explicitly mentioned
   - `key_decisions`: list of decisions made
4. Update the transcript .md file in place:
   - Replace the `# Title` with the generated title
   - Replace the `## Summary` content
   - Replace `## Action Items` with checkbox-formatted items (`- [ ] item — owner`)
   - Replace `## Key Decisions` with bullet list
   - Add or update `## Projects Mentioned`
   - Update the YAML frontmatter `model:` field to `claude:manual`
   - If the transcript was from a local model, compare your summary against the existing one — keep anything the local model got right and improve/add what it missed

After updating each file, tell me what you changed.
