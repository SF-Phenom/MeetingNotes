# Test fixtures

## Benchmark recording (pending)

Tests look for a known-good reference recording in this directory so we stop
picking random (often empty) files from `recordings/queue/` when validating
the pipeline. Until the file exists, integration tests are auto-skipped.

### Expected files

- `benchmark.wav` — mic-only (or mixed) 16 kHz mono Int16 PCM, ~2 minutes
- `benchmark.sys.wav` — the system-audio counterpart (needed for Phase 4
  full-audio mixer verification). Same format as above. Optional until Phase 4.
- `benchmark.yaml` — metadata describing the recording (see schema below)

### benchmark.yaml schema

```yaml
duration_seconds: 120

# Distinctive strings that MUST appear in the transcript. Tests assert
# each of these is found (case-insensitive, substring match). Avoid generic
# words that could appear by chance — use made-up product names, specific
# jargon, or uncommon phrases.
expected_phrases:
  - "Aquafluxion"
  - "Project Halyard retrospective"

# Phrases that SHOULD be rewritten by Settings/corrections.md. Each entry is
# a pair: what Parakeet produces (heard) vs. what the final transcript should
# contain (should_be). The test asserts `heard` does NOT appear AND `should_be`
# DOES appear in the transcript.
expected_corrections:
  - heard: "aqua fluxin"
    should_be: "Aquafluxion"

# Clearly-phrased action items that the summarizer should extract. Tests
# assert each substring appears in the final .md's Action Items section.
expected_action_items:
  - "send the Q2 slide deck"

# Free-form notes for humans — not used by tests.
notes: |
  Recorded 2026-04-DD. Mock meeting between me and a colleague over Zoom.
  Includes ~10s of silence at 0:45 to exercise VAD / chunk boundaries.
```

### Recording guidance

1. Record a real 2-minute mock meeting with a Zoom/Meet call so BOTH mic and
   system audio are captured (proves dual-WAV + eventual in-Swift mixer).
2. Include 2–3 distinctive made-up terms that you list in `expected_phrases`.
3. Include at least one clearly phrased action item.
4. Include ~10 seconds of silence somewhere to exercise VAD / chunk boundary
   logic.
5. If possible, include a proper noun that Parakeet will mistranscribe, so
   `corrections.md` has something to fix — list it in `expected_corrections`.

Once you drop the files in, `pytest` will automatically unskip the benchmark
tests in `test_pipeline_benchmark.py` on the next run. No code changes needed.
