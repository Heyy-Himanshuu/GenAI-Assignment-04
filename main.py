"""Command-line entry point for the web-automation agent.

Examples
--------
    # Use defaults from .env (fills the shadcn form):
    python main.py

    # Fill custom values, watch the browser:
    python main.py --name "Ada Lovelace" --description "First programmer."

    # Run headless and disable thinking for speed:
    python main.py --headless --no-thinking
"""
from __future__ import annotations

import argparse
import sys

from agent.agent import WebAutomationAgent
from agent.logger import setup_logger
from config import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gemini-powered vision agent that fills a web form with Playwright."
    )
    parser.add_argument("--url", help="Target URL to automate.")
    parser.add_argument("--name", help="Value to type into the Name field.")
    parser.add_argument("--description", help="Value to type into the Description field.")
    parser.add_argument("--model", help="Gemini model id (default gemini-2.5-flash).")
    parser.add_argument("--max-steps", type=int, help="Safety cap on agent iterations.")
    parser.add_argument("--headless", dest="headless", action="store_true",
                        help="Run the browser without a visible window.")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Force a visible browser window.")
    parser.add_argument("--no-thinking", dest="thinking", action="store_false",
                        help="Disable Gemini 2.5 thinking (faster).")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default INFO).")
    parser.set_defaults(headless=None, thinking=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()

    # Apply CLI overrides on top of env/defaults (only when explicitly given).
    if args.url is not None:
        config.target_url = args.url
    if args.name is not None:
        config.name_value = args.name
    if args.description is not None:
        config.description_value = args.description
    if args.model is not None:
        config.model = args.model
    if args.max_steps is not None:
        config.max_steps = args.max_steps
    if args.headless is not None:
        config.headless = args.headless
    if args.thinking is not None:
        config.enable_thinking = args.thinking
    if args.log_level is not None:
        config.log_level = args.log_level

    logger = setup_logger(config.logs_dir, config.log_level)
    logger.info("Starting web-automation agent")
    logger.info("Target : %s", config.target_url)
    logger.info("Model  : %s (thinking=%s)", config.model, config.enable_thinking)
    logger.info('Name   : "%s"', config.name_value)
    logger.info('Desc.  : "%s"', config.description_value)

    if not config.api_key:
        logger.error("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        return 2

    agent = WebAutomationAgent(config, logger)
    result = agent.run()

    logger.info("══════════════════════════════════════════")
    logger.info("RESULT : %s", "SUCCESS" if result.success else "INCOMPLETE")
    logger.info("Steps  : %d", result.steps_taken)
    logger.info("Summary: %s", result.summary)
    logger.info("Screenshots saved under: %s/", config.screenshots_dir)
    logger.info("══════════════════════════════════════════")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
