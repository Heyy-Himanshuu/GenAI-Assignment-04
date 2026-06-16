"""The Gemini-driven agent loop (the agent's "brain").

``WebAutomationAgent`` opens the page, then repeatedly:

  1. sends the conversation (instructions + screenshots) to Gemini,
  2. reads the function call Gemini decides to make,
  3. executes it against the :class:`BrowserController`,
  4. feeds the result back as a ``functionResponse`` part PLUS a fresh screenshot,

until Gemini calls ``report_task_complete`` or a safety step-limit is hit.

This is a *manual* function-calling loop: we keep full control so every decision
and action is logged, and so we can attach a screenshot after each action — the
visual feedback that makes the vision approach work. (Automatic function calling
is disabled because it would call our functions without giving the model a
fresh screenshot in between.)
"""
from __future__ import annotations

from dataclasses import dataclass
from logging import Logger

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agent.browser_tools import BrowserController, BrowserError
from agent.tools_schema import (
    FUNCTION_DECLARATIONS,
    build_system_prompt,
    build_task_prompt,
)
from config import Config

# Max characters of model text/thinking we echo into the log per block.
_LOG_SNIPPET = 500


@dataclass
class RunResult:
    """Outcome of an agent run."""

    success: bool
    summary: str
    steps_taken: int


class WebAutomationAgent:
    """Drives a browser to complete a task using Gemini's vision + function calling."""

    def __init__(self, config: Config, logger: Logger) -> None:
        if not config.api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to your .env file or "
                "environment before running the agent."
            )
        self.config = config
        self.log = logger
        self.client = genai.Client(api_key=config.api_key)
        self.tool = types.Tool(function_declarations=FUNCTION_DECLARATIONS)
        self.controller = BrowserController(config, logger)
        self.system_prompt = build_system_prompt(
            config.viewport_width, config.viewport_height
        )

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self) -> RunResult:
        """Execute the full task and return the result."""
        result = RunResult(success=False, summary="Agent did not finish.", steps_taken=0)
        try:
            self.controller.open_browser()
            self.controller.navigate_to_url(self.config.target_url)
            _, screenshot = self.controller.take_screenshot("initial")

            # The conversation history (list of types.Content). It starts with the
            # task description and the first screenshot.
            contents: list[types.Content] = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=build_task_prompt(
                                self.config.target_url,
                                self.config.name_value,
                                self.config.description_value,
                            )
                        ),
                        types.Part.from_bytes(data=screenshot, mime_type="image/png"),
                    ],
                )
            ]

            for step in range(1, self.config.max_steps + 1):
                result.steps_taken = step
                self.log.info("──── step %d/%d ────", step, self.config.max_steps)

                response = self._generate(contents)
                self._log_model_output(response)

                model_content = self._model_content(response)
                if model_content is None:
                    self.log.warning("Model returned no content (possibly blocked).")
                    result.summary = "Model returned no content."
                    break

                # Record the model turn verbatim (preserves thought signatures).
                contents.append(model_content)

                function_calls = [
                    p.function_call
                    for p in (model_content.parts or [])
                    if getattr(p, "function_call", None)
                ]

                if not function_calls:
                    # Model replied with text instead of a function call — nothing
                    # left to do (or it is asking a question). End the loop.
                    self.log.info("Model ended turn without a function call.")
                    result.summary = self._first_text(model_content) or (
                        "Model stopped without a function call."
                    )
                    break

                # Execute each requested function and build the reply turn.
                fr_parts: list[types.Part] = []   # functionResponse parts (first)
                img_parts: list[types.Part] = []   # screenshot parts (after)
                finished = False
                for fc in function_calls:
                    text, screenshot, done, outcome = self._execute_tool(fc)
                    fr_parts.append(
                        types.Part.from_function_response(
                            name=fc.name, response={"output": text}
                        )
                    )
                    if screenshot is not None:
                        img_parts.append(
                            types.Part.from_bytes(data=screenshot, mime_type="image/png")
                        )
                    if done:
                        finished = True
                        result.success = outcome.success
                        result.summary = outcome.summary

                contents.append(types.Content(role="user", parts=fr_parts + img_parts))

                if finished:
                    self.log.info(
                        "Task reported complete (success=%s).", result.success
                    )
                    break
            else:
                self.log.warning(
                    "Reached the %d-step limit without completion.",
                    self.config.max_steps,
                )
                result.summary = "Step limit reached before the task was reported complete."

        except genai_errors.APIError as exc:
            self.log.error("Gemini API error: %s", exc)
            result.summary = f"API error: {exc}"
        except BrowserError as exc:
            self.log.error("Browser error: %s", exc)
            result.summary = f"Browser error: {exc}"
        except Exception as exc:  # noqa: BLE001 - top-level safety net
            self.log.exception("Unexpected error during run.")
            result.summary = f"Unexpected error: {exc}"
        finally:
            self.controller.close()

        return result

    # ------------------------------------------------------------------ #
    # Model call
    # ------------------------------------------------------------------ #
    def _generate(self, contents: list[types.Content]):
        """Call ``generate_content`` with vision + function-calling config."""
        cfg_kwargs: dict = {
            "system_instruction": self.system_prompt,
            "tools": [self.tool],
            "max_output_tokens": self.config.max_output_tokens,
            # Manual loop: we resolve function calls ourselves so we can attach a
            # screenshot to each result.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        }
        # "Thinking" is only available on Gemini 2.5 models. include_thoughts
        # surfaces a summary we can log; thinking_budget=-1 lets the model decide
        # how much to think (0 would disable it on Flash).
        if "2.5" in self.config.model:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                include_thoughts=self.config.enable_thinking,
                thinking_budget=(-1 if self.config.enable_thinking else 0),
            )

        return self.client.models.generate_content(
            model=self.config.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    # ------------------------------------------------------------------ #
    # Tool execution
    # ------------------------------------------------------------------ #
    def _execute_tool(self, function_call):
        """Run one function call.

        Returns ``(result_text, screenshot_bytes_or_None, finished, outcome)``
        where ``finished`` is True only for ``report_task_complete``.
        """
        name = function_call.name
        args = dict(function_call.args or {})
        self.log.info("→ tool: %s %s", name, args)

        # report_task_complete ends the run and carries no screenshot.
        if name == "report_task_complete":
            outcome = RunResult(
                success=bool(args.get("success", False)),
                summary=str(args.get("summary", "")),
                steps_taken=0,
            )
            return "Acknowledged. Ending run.", None, True, outcome

        try:
            text, screenshot = self._dispatch_browser_tool(name, args)
            return text, screenshot, False, None
        except BrowserError as exc:
            self.log.warning("tool %s failed: %s", name, exc)
            # Report the error back to the model with a current screenshot so it
            # can re-plan rather than crashing the run.
            screenshot = None
            try:
                _, screenshot = self.controller.take_screenshot("after_error")
            except Exception:
                pass
            return f"Error: {exc}", screenshot, False, None

    def _dispatch_browser_tool(self, name: str, args: dict) -> tuple[str, bytes]:
        """Map a function name to a BrowserController call; return (text, screenshot)."""
        if name == "take_screenshot":
            _, png = self.controller.take_screenshot("requested")
            return "Screenshot captured.", png

        if name == "navigate_to_url":
            text = self.controller.navigate_to_url(args["url"])
        elif name == "click_on_screen":
            text = self.controller.click_on_screen(int(args["x"]), int(args["y"]))
        elif name == "double_click":
            text = self.controller.double_click(int(args["x"]), int(args["y"]))
        elif name == "send_keys":
            text = self.controller.send_keys(
                text=args.get("text"),
                press=args.get("press"),
                clear_first=bool(args.get("clear_first", False)),
            )
        elif name == "scroll":
            text = self.controller.scroll(
                direction=args.get("direction", "down"),
                amount=int(args.get("amount", 400)),
            )
        else:
            raise BrowserError(f"Unknown tool '{name}'.")

        # Every interaction returns a fresh screenshot so the model sees the result.
        _, png = self.controller.take_screenshot(name)
        return text, png

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _model_content(response):
        """Return the model's ``Content`` from a response, or None if absent."""
        candidates = getattr(response, "candidates", None)
        if not candidates:
            return None
        return candidates[0].content

    def _log_model_output(self, response) -> None:
        """Log thought summaries / text and token usage from a model response."""
        content = self._model_content(response)
        if content is not None:
            for part in content.parts or []:
                text = getattr(part, "text", None)
                if not text:
                    continue
                if getattr(part, "thought", False):
                    self.log.info("[thinking] %s", _truncate(text))
                else:
                    self.log.info("[assistant] %s", _truncate(text))
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            self.log.debug(
                "tokens prompt=%s output=%s thoughts=%s",
                getattr(usage, "prompt_token_count", "?"),
                getattr(usage, "candidates_token_count", "?"),
                getattr(usage, "thoughts_token_count", "?"),
            )

    @staticmethod
    def _first_text(content) -> str:
        for part in content.parts or []:
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):
                return text
        return ""


def _truncate(text: str, limit: int = _LOG_SNIPPET) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
