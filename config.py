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

    # --- Claude / model ---
    # api_key is the first/primary key (kept for back-compat and the startup
    # check); api_keys is the full rotation pool used on rate-limit/auth errors.
    api_key: str | None
    api_keys: list[str]
    model: str
    enable_thinking: bool
    max_output_tokens: int

    # --- The task to perform ---
    # When `task` is set, the agent runs in free-form mode: it pursues this
    # natural-language instruction. When it is None, it runs the built-in
    # form-filling demo (name_value / description_value on target_url).
    task: str | None
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


def _collect_api_keys() -> list[str]:
    """Gather every configured Anthropic key into an ordered, de-duplicated list.

    Reads ANTHROPIC_API_KEY, ANTHROPIC_API_KEY_2..5, and a comma-separated
    ANTHROPIC_API_KEYS. The agent tries them in order, rotating to the next one
    when a key is rate-limited (HTTP 429) or rejected (401/403). The unedited
    ``sk-ant-...`` placeholder from .env.example is ignored.
    """
    candidates: list[str] = []
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_2",
        "ANTHROPIC_API_KEY_3",
        "ANTHROPIC_API_KEY_4",
        "ANTHROPIC_API_KEY_5",
    ):
        candidates.append(os.getenv(name) or "")
    candidates.extend((os.getenv("ANTHROPIC_API_KEYS") or "").split(","))

    keys: list[str] = []
    for raw in candidates:
        key = raw.strip()
        if key and key != "sk-ant-..." and key not in keys:
            keys.append(key)
    return keys


def load_config() -> Config:
    """Build a :class:`Config` from environment variables (with defaults)."""
    api_keys = _collect_api_keys()
    return Config(
        # First key is primary; the rest are fallbacks used on rate-limit/auth errors.
        api_key=api_keys[0] if api_keys else None,
        api_keys=api_keys,
        model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8"),
        enable_thinking=_get_bool("ENABLE_THINKING", True),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "16000")),
        task=os.getenv("TASK") or None,
        target_url=os.getenv(
            "TARGET_URL", "https://ui.shadcn.com/docs/forms/react-hook-form"
        ),
        name_value=os.getenv("FORM_NAME_VALUE", "Jane Doe"),
        description_value=os.getenv(
            "FORM_DESCRIPTION_VALUE",
            "This form was filled automatically by a Claude-powered vision "
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
