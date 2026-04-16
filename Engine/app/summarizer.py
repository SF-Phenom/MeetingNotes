"""
Summarizer — LLM integration for MeetingNotes.

Supports multiple backends:
  - Claude API (Anthropic) — highest quality, requires API key
  - Ollama (local) — runs on-device, no API key needed

Model selection modes:
  - "automatic" — try Claude first, fall back to Ollama
  - "claude"    — Claude API only
  - "<model>"   — specific Ollama model (e.g. "qwen3:4b")
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS = 4096
CLAUDE_TEMPERATURE = 0
MAX_RETRIES = 2

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen3:4b"

# Active model selection — set via set_model_preference()
_model_preference: str = "automatic"


# --- Data types --------------------------------------------------------------

@dataclass
class SummaryResult:
    title: str
    summary: str
    action_items: list[dict]
    projects_mentioned: list[str]
    key_decisions: list[str]
    raw_json: dict = field(default_factory=dict)
    model_used: str = ""  # which model actually produced this result


# --- Model preference --------------------------------------------------------

def set_model_preference(preference: str) -> None:
    """Set the active model preference.

    Args:
        preference: One of "automatic", "claude", or an Ollama model name.
    """
    global _model_preference
    _model_preference = preference
    logger.info("Model preference set to: %s", preference)


def get_model_preference() -> str:
    """Return the current model preference."""
    return _model_preference


# --- Ollama discovery --------------------------------------------------------

def discover_ollama_models() -> list[str]:
    """Query the local Ollama server for available models.

    Returns a list of model names (e.g. ["qwen3:4b", "llama3.1:8b"]),
    or an empty list if Ollama is not running or not installed.
    """
    import urllib.request
    import urllib.error

    url = OLLAMA_BASE_URL + "/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        logger.info("Ollama models discovered: %s", models)
        return sorted(models)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as e:
        logger.debug("Ollama not available: %s", e)
        return []


# --- Prompt building ---------------------------------------------------------

def _build_system_prompt(context_md: str) -> str:
    return (
        "You are a meeting notes assistant. "
        "The user's role and team context is below.\n\n"
        + context_md.strip()
    )


def _build_user_prompt(
    transcript_text: str,
    metadata: dict | None,
) -> str:
    meta = metadata or {}
    source = meta.get("source", "unknown")
    participants = meta.get("participants", "unknown")
    notes = meta.get("notes", "none")

    return (
        f"Meeting metadata:\n"
        f"- Source: {source}\n"
        f"- Participants: {participants}\n"
        f"- User notes: {notes}\n\n"
        f"<transcript>\n{transcript_text}\n</transcript>\n\n"
        'Return a JSON object with exactly these keys:\n'
        '{\n'
        '  "title": "4-6 word descriptive meeting title",\n'
        '  "summary": "3-5 sentence narrative summary of what was discussed and decided",\n'
        '  "action_items": [\n'
        '    {"item": "action item description", "owner": "name if mentioned, else null", "due": "date if mentioned, else null"}\n'
        '  ],\n'
        '  "projects_mentioned": ["project or initiative names explicitly mentioned"],\n'
        '  "key_decisions": ["decisions made, if any"]\n'
        '}\n\n'
        "Return only valid JSON. No preamble. No commentary. No thinking."
    )


def _extract_json(text: str) -> dict:
    """Parse JSON from an LLM response, handling markdown code fences and thinking tags."""
    # Strip <think>...</think> tags (Qwen3 uses these)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    # Strip markdown code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        # Try to find a bare JSON object
        obj_match = re.search(r"\{.*\}", text, re.DOTALL)
        if obj_match:
            text = obj_match.group(0)

    return json.loads(text)


# --- Claude backend ----------------------------------------------------------

def _summarize_claude(
    transcript_text: str,
    context_md: str,
    metadata: dict | None,
) -> SummaryResult:
    """Summarize using the Claude API."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set."
        )

    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "The 'anthropic' package is not installed. Run: pip install anthropic"
        ) from e

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt(context_md)
    user_prompt = _build_user_prompt(transcript_text, metadata)

    transcript_words = len(transcript_text.split())
    logger.info(
        "Calling Claude API (model=%s, transcript_words=%d)",
        CLAUDE_MODEL,
        transcript_words,
    )

    last_error: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        if attempt > 0:
            backoff = 2 ** attempt
            logger.warning(
                "Claude API attempt %d/%d failed, retrying in %ds: %s",
                attempt, 1 + MAX_RETRIES, backoff, last_error,
            )
            time.sleep(backoff)

        try:
            t0 = time.time()
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=CLAUDE_MAX_TOKENS,
                temperature=CLAUDE_TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            elapsed = time.time() - t0

            raw_text = response.content[0].text
            logger.info(
                "Claude responded in %.1fs (input_tokens=%s, output_tokens=%s)",
                elapsed,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            parsed = _extract_json(raw_text)
            return SummaryResult(
                title=parsed.get("title", "Untitled Meeting"),
                summary=parsed.get("summary", ""),
                action_items=parsed.get("action_items", []),
                projects_mentioned=parsed.get("projects_mentioned", []),
                key_decisions=parsed.get("key_decisions", []),
                raw_json=parsed,
                model_used="claude:" + CLAUDE_MODEL,
            )

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error("Failed to parse Claude response as JSON: %s", e)
            last_error = e
        except anthropic.APIError as e:
            # Covers APIConnectionError, APIStatusError, RateLimitError,
            # InternalServerError, etc. — the anthropic SDK's top-level base
            # for everything that could reasonably be retried. Programming
            # errors (TypeError, AttributeError) are intentionally NOT caught
            # so they surface loudly in logs instead of being silently retried
            # three times and then wrapped in a generic "Claude failed"
            # RuntimeError.
            last_error = e
        except (TimeoutError, ConnectionError, OSError) as e:
            # Network layer below the SDK.
            last_error = e

    raise RuntimeError(
        f"Claude API failed after {1 + MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


# --- Ollama backend ----------------------------------------------------------

def _summarize_ollama(
    transcript_text: str,
    context_md: str,
    metadata: dict | None,
    model: str | None = None,
) -> SummaryResult:
    """Summarize using a local Ollama model."""
    import urllib.request
    import urllib.error

    model = model or OLLAMA_DEFAULT_MODEL
    system_prompt = _build_system_prompt(context_md)
    user_prompt = _build_user_prompt(transcript_text, metadata)

    transcript_words = len(transcript_text.split())
    logger.info(
        "Calling Ollama (model=%s, transcript_words=%d)",
        model, transcript_words,
    )

    payload = json.dumps({
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }).encode()

    url = OLLAMA_BASE_URL + "/api/chat"

    raw_text = ""
    last_error: Exception | None = None
    for attempt in range(1 + MAX_RETRIES):
        if attempt > 0:
            backoff = 2 ** attempt
            logger.warning(
                "Ollama attempt %d/%d failed, retrying in %ds: %s",
                attempt, 1 + MAX_RETRIES, backoff, last_error,
            )
            time.sleep(backoff)

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
            elapsed = time.time() - t0

            raw_text = data.get("message", {}).get("content", "")
            logger.info("Ollama responded in %.1fs", elapsed)

            parsed = _extract_json(raw_text)
            return SummaryResult(
                title=parsed.get("title", "Untitled Meeting"),
                summary=parsed.get("summary", ""),
                action_items=parsed.get("action_items", []),
                projects_mentioned=parsed.get("projects_mentioned", []),
                key_decisions=parsed.get("key_decisions", []),
                raw_json=parsed,
                model_used="ollama:" + model,
            )

        except (json.JSONDecodeError, KeyError) as e:
            logger.error("Failed to parse Ollama response as JSON: %s", e)
            logger.debug("Raw Ollama response: %s", raw_text[:1000] if raw_text else "(empty)")
            last_error = e
        except (urllib.error.URLError, OSError, TimeoutError, ConnectionError) as e:
            # Transport-level failures: network down, Ollama not running,
            # request timeout. Transient — worth retrying.
            # Programming errors (TypeError, AttributeError) are not caught
            # so they surface as crashes rather than silent retries.
            logger.error("Ollama request failed: %s", e)
            last_error = e

    raise RuntimeError(
        f"Ollama ({model}) failed after {1 + MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


# --- Public API --------------------------------------------------------------

def summarize(
    transcript_text: str,
    context_md: str,
    metadata: dict | None = None,
) -> SummaryResult:
    """
    Summarize a transcript using the configured model preference.

    Model preference:
      - "automatic": try Claude, fall back to Ollama
      - "claude": Claude API only
      - "<model_name>": specific Ollama model

    Returns:
        SummaryResult with model_used indicating which backend was used.

    Raises:
        RuntimeError: If all backends fail.
    """
    pref = _model_preference

    if pref == "claude":
        return _summarize_claude(transcript_text, context_md, metadata)

    if pref == "automatic":
        # Automatic mode's whole point is to fall back to Ollama on ANY Claude
        # failure — missing API key, expired key, out of credits, network
        # issue, even a schema-mismatch RuntimeError from _summarize_claude's
        # retry loop. A broad `except Exception` here is intentional. Same
        # for the outer catch: if Ollama ALSO fails, we raise a composite
        # error rather than picking one.
        try:
            return _summarize_claude(transcript_text, context_md, metadata)
        except Exception as e:  # noqa: BLE001 — intentional fallback trigger
            logger.warning(
                "Claude failed in automatic mode, falling back to Ollama: %s", e
            )
            try:
                return _summarize_ollama(transcript_text, context_md, metadata)
            except Exception as ollama_err:  # noqa: BLE001 — composite report
                raise RuntimeError(
                    f"Both Claude and Ollama failed. "
                    f"Claude: {e} | Ollama: {ollama_err}"
                ) from ollama_err

    # Specific Ollama model name
    return _summarize_ollama(transcript_text, context_md, metadata, model=pref)
