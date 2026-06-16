"""Centralised configuration for the web-automation agent.

All settings have sensible defaults and can be overridden via environment
variables (loaded from a ``.env`` file) or command-line flags (see ``main.py``).
Keeping configuration in one typed object makes the rest of the codebase easy to
read and test.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load variables from a local .env file if present. This is a no-op when the
# file does not exist, so it is safe to call unconditionally.
load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    """Read a boolean-ish environment variable (true/1/yes/on)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Runtime configuration for a single agent run."""

    # --- Gemini / model ---
    api_key: str | None
    model: str
    enable_thinking: bool
    max_output_tokens: int

    # --- The task to perform ---
    target_url: str
    name_value: str
    description_value: str

    # --- Browser behaviour ---
    headless: bool
    viewport_width: int
    viewport_height: int
    nav_timeout_ms: int
    action_pause_ms: int

    # --- Agent loop / output ---
    max_steps: int
    screenshots_dir: str
    logs_dir: str
    log_level: str


def load_config() -> Config:
    """Build a :class:`Config` from environment variables (with defaults)."""
    return Config(
        # GEMINI_API_KEY is preferred; GOOGLE_API_KEY is accepted as a fallback
        # because the google-genai SDK also recognises it.
        api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        enable_thinking=_get_bool("ENABLE_THINKING", True),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "8192")),
        target_url=os.getenv(
            "TARGET_URL", "https://ui.shadcn.com/docs/forms/react-hook-form"
        ),
        name_value=os.getenv("FORM_NAME_VALUE", "Jane Doe"),
        description_value=os.getenv(
            "FORM_DESCRIPTION_VALUE",
            "This form was filled automatically by a Gemini-powered vision "
            "agent using Playwright.",
        ),
        headless=_get_bool("HEADLESS", False),
        viewport_width=int(os.getenv("VIEWPORT_WIDTH", "1280")),
        viewport_height=int(os.getenv("VIEWPORT_HEIGHT", "800")),
        nav_timeout_ms=int(os.getenv("NAV_TIMEOUT_MS", "45000")),
        action_pause_ms=int(os.getenv("ACTION_PAUSE_MS", "400")),
        max_steps=int(os.getenv("MAX_STEPS", "25")),
        screenshots_dir=os.getenv("SCREENSHOTS_DIR", "screenshots"),
        logs_dir=os.getenv("LOGS_DIR", "logs"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
