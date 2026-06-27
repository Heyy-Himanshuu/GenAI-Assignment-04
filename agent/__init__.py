"""Web-automation agent package.

Exposes the two main building blocks:

* :class:`~agent.browser_tools.BrowserController` — the low-level Playwright
  wrapper that implements the required browser capabilities.
* :class:`~agent.agent.WebAutomationAgent` — the Claude-driven loop that decides
  which capability to use based on screenshots.
"""

from agent.agent import WebAutomationAgent
from agent.browser_tools import BrowserController

__all__ = ["WebAutomationAgent", "BrowserController"]
