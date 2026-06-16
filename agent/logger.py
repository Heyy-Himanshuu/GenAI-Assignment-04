"""Logging setup.

A single logger writes to both the console (so you can watch the agent live)
and a timestamped file under ``logs/`` (so every run is auditable afterwards).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime


def setup_logger(logs_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    """Create and return the application logger.

    Args:
        logs_dir: Directory where the per-run log file is written.
        level: Logging level name (``DEBUG``/``INFO``/``WARNING``/``ERROR``).
    """
    os.makedirs(logs_dir, exist_ok=True)

    logger = logging.getLogger("webagent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()  # avoid duplicate handlers if called twice
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.FileHandler(
        os.path.join(logs_dir, f"agent_{stamp}.log"), encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
