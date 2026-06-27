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
        description="Claude-powered vision agent that fills a web form with Playwright."
    )
    parser.add_argument(
        "--task",
        help="Free-form instruction for the agent, e.g. "
        '"open youtube, search fifa world cup, click the second video". '
        "If omitted, you are prompted for one (press Enter for the form-fill demo).",
    )
    parser.add_argument("--url", help="Starting URL to open before the task.")
    parser.add_argument("--name", help="Value to type into the Name field (demo mode).")
    parser.add_argument("--description", help="Value to type into the Description field (demo mode).")
    parser.add_argument("--model", help="Claude model id (default claude-opus-4-8).")
    parser.add_argument("--max-steps", type=int, help="Safety cap on agent iterations.")
    parser.add_argument("--headless", dest="headless", action="store_true",
                        help="Run the browser without a visible window.")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Force a visible browser window.")
    parser.add_argument("--no-thinking", dest="thinking", action="store_false",
                        help="Disable Claude adaptive thinking (faster, cheaper).")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default INFO).")
    parser.set_defaults(headless=None, thinking=None)
    return parser.parse_args()


def resolve_task(args: argparse.Namespace, config) -> None:
    """Decide whether to run a free-form task or the built-in form-fill demo.

    Priority: an explicit ``--task`` (or ``TASK`` env var) wins; otherwise, when
    running interactively, prompt the user. An empty answer means "run the demo".
    """
    if args.task is not None:
        config.task = args.task.strip() or None
    elif config.task is None and sys.stdin.isatty():
        try:
            answer = input(
                "\nWhat should the agent do?\n"
                "  • Type a task, e.g. 'open youtube, search fifa world cup, click the second video'\n"
                "  • Or press Enter to run the default form-filling demo.\n> "
            ).strip()
        except EOFError:
            answer = ""
        config.task = answer or None

    # In free-form mode, start from a neutral launch page unless the user gave a
    # specific --url; the agent navigates onward itself.
    if config.task and args.url is None:
        config.target_url = "https://www.google.com"


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

    resolve_task(args, config)

    logger = setup_logger(config.logs_dir, config.log_level)
    logger.info("Starting web-automation agent")
    logger.info("Model  : %s (thinking=%s)", config.model, config.enable_thinking)
    logger.info("Keys   : %d loaded (auto-rotate on quota/denied)", len(config.api_keys))
    logger.info("Start  : %s", config.target_url)
    if config.task:
        logger.info("Mode   : free-form task")
        logger.info('Task   : "%s"', config.task)
    else:
        logger.info("Mode   : form-fill demo")
        logger.info('Name   : "%s"', config.name_value)
        logger.info('Desc.  : "%s"', config.description_value)

    if not config.api_key:
        logger.error("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")
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
