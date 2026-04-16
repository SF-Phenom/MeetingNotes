"""UpdateChecker — background git fetch + prompt + pull + restart.

Owns both git-side (subprocess fetch/compare/pull) and the thread lifecycle
for those operations. rumps-free: menubar injects five callbacks that run
on the main thread via the UIBridge, so this class can be unit-tested
without rumps or a terminal.

Flow:
    check()  → spawns worker → git fetch + rev-parse
                 |
                 +-- up to date          → notify("Up to date")
                 +-- check failed        → notify("Update check failed")
                 +-- update available    → dispatch prompt_install()
                                               ↓
                                    menubar's prompt handler, if user
                                    clicks Install, calls apply()
                                               ↓
              apply() → spawns worker → git pull
                          |
                          +-- success → dispatch restart()
                          +-- failure → dispatch show_alert("Update Failed", ...)
"""
from __future__ import annotations

import logging
import subprocess
import threading
from typing import Callable

from app.ui_bridge import UIBridge

logger = logging.getLogger(__name__)


NotifyFn = Callable[[str, str], None]
PromptFn = Callable[[], None]
AlertFn = Callable[[str, str], None]
RestartFn = Callable[[], None]


class UpdateChecker:
    """Self-contained check → prompt → pull → restart flow."""

    def __init__(
        self,
        *,
        base_dir: str,
        ui_bridge: UIBridge,
        notify: NotifyFn,
        prompt_install: PromptFn,
        show_alert: AlertFn,
        restart: RestartFn,
    ) -> None:
        self._base_dir = base_dir
        self._ui_bridge = ui_bridge
        self._notify = notify
        self._prompt_install = prompt_install
        self._show_alert = show_alert
        self._restart = restart

    # -- Public API (main thread) ---------------------------------------------

    def check(self) -> None:
        """Spawn a bg thread that fetches origin and compares with HEAD."""
        threading.Thread(target=self._run_check, daemon=True).start()

    def apply(self) -> None:
        """Spawn a bg thread that pulls latest and triggers restart on success."""
        threading.Thread(target=self._run_apply, daemon=True).start()

    # -- Worker threads -------------------------------------------------------

    def _run_check(self) -> None:
        try:
            subprocess.run(
                ["git", "-C", self._base_dir, "fetch"],
                capture_output=True, timeout=30,
            )
            local = subprocess.run(
                ["git", "-C", self._base_dir, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "-C", self._base_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "main"
            remote = subprocess.run(
                ["git", "-C", self._base_dir, "rev-parse", f"origin/{branch}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception as e:  # noqa: BLE001
            logger.error("Update check failed: %s", e)
            self._ui_bridge.dispatch(lambda: self._notify(
                "Update check failed",
                "Could not reach GitHub. Check your internet connection.",
            ))
            return

        if local == remote:
            self._ui_bridge.dispatch(lambda: self._notify(
                "Up to date",
                "You're running the latest version.",
            ))
            return

        self._ui_bridge.dispatch(self._prompt_install)

    def _run_apply(self) -> None:
        try:
            result = subprocess.run(
                ["git", "-C", self._base_dir, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                logger.error("git pull failed: %s", result.stderr)
                self._ui_bridge.dispatch(lambda: self._show_alert(
                    "Update Failed",
                    "Could not pull updates. Check Engine/logs/app.log for details.",
                ))
                return
        except Exception as e:  # noqa: BLE001
            logger.error("Update pull failed: %s", e)
            err_msg = str(e)
            self._ui_bridge.dispatch(
                lambda: self._show_alert("Update Failed", err_msg)
            )
            return

        logger.info("Update applied, restarting app")
        self._ui_bridge.dispatch(self._restart)
