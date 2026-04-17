"""Model-manager: Ollama discovery, Anthropic API key state, model preference.

Owns the "which LLM do we use for summarization" concerns so menubar.py
doesn't have to. Returns plain data (lists, strings, bools); the menubar
builds the rumps menu items from it. This keeps the manager testable
without a rumps import and without spawning Ollama during unit tests.
"""
from __future__ import annotations

import logging
import os

from app import summarizer

logger = logging.getLogger(__name__)


class ModelManager:
    """Owns discovery + preferences + API key persistence."""

    def __init__(self) -> None:
        self._available_models: list[str] = []

    # -- Ollama discovery -----------------------------------------------------

    def discover(self) -> None:
        """Re-scan Ollama for installed models. Safe to call repeatedly."""
        try:
            self._available_models = summarizer.discover_ollama_models()
        except Exception as e:  # noqa: BLE001 — discovery is best-effort
            logger.warning("Model discovery failed: %s", e)
            self._available_models = []
            return
        if self._available_models:
            logger.info("Ollama models available: %s", self._available_models)
        else:
            logger.info("No Ollama models found (Ollama may not be running)")

    def available_models(self) -> list[str]:
        return list(self._available_models)

    # -- Preference -----------------------------------------------------------

    def current_preference(self) -> str:
        return summarizer.get_model_preference()

    def set_preference(self, preference: str) -> None:
        summarizer.set_model_preference(preference)
        logger.info("Model preference set: %s", preference)

    # -- API key --------------------------------------------------------------

    def api_key_present(self) -> bool:
        """True if ANTHROPIC_API_KEY is set to a non-empty value."""
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def save_api_key(self, raw_key: str) -> tuple[bool, str]:
        """Validate and persist an Anthropic API key.

        Returns ``(ok, message)``. On success, updates the environment and
        writes the key to ``~/.zshrc`` with an atomic rewrite so a mid-write
        crash can't wipe the user's shell config. On validation failure,
        returns ``(False, reason)`` without touching anything.

        Validation is two-stage: a cheap shape check, then a live
        round-trip against the Anthropic API. Catching a typo or revoked
        key here means the user finds out at save time instead of at
        their next meeting transcription.
        """
        key = (raw_key or "").strip()
        if not key:
            return False, "No key provided."
        if not key.startswith("sk-ant-"):
            return False, "The key should start with 'sk-ant-'."

        ok, validation_msg = summarizer.validate_api_key(key)
        if not ok:
            return False, validation_msg

        os.environ["ANTHROPIC_API_KEY"] = key

        zshrc_path = os.path.expanduser("~/.zshrc")
        try:
            lines: list[str] = []
            if os.path.exists(zshrc_path):
                with open(zshrc_path, "r", encoding="utf-8") as f:
                    lines = [
                        line for line in f.readlines()
                        if not line.lstrip().startswith("export ANTHROPIC_API_KEY=")
                    ]
            lines.append('export ANTHROPIC_API_KEY="{}"\n'.format(key))
            tmp_path = zshrc_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.rename(tmp_path, zshrc_path)
        except OSError as e:
            logger.error("Failed to save API key to ~/.zshrc: %s", e)
            return True, (
                "Key active for this session but could not be written to "
                "~/.zshrc: {}".format(e)
            )
        logger.info("API key saved to ~/.zshrc")
        return True, "Claude summarization is now active."
