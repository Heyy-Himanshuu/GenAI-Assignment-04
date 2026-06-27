"""The Claude-driven agent loop (the agent's "brain").

``WebAutomationAgent`` opens the page, then repeatedly:

  1. sends the conversation (instructions + screenshots) to Claude,
  2. reads the tool call Claude decides to make,
  3. executes it against the :class:`BrowserController`,
  4. feeds the result back as a ``tool_result`` block that carries BOTH the
     result text AND a fresh screenshot,

until Claude calls ``report_task_complete`` or a safety step-limit is hit.

This is a *manual* tool-use loop: we keep full control so every decision and
action is logged, and so we can attach a screenshot after each action — the
visual feedback that makes the vision approach work. (We do not use the SDK's
automatic tool runner because it would call our functions without giving the
model a fresh screenshot in between.)
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from logging import Logger

import anthropic

from agent.browser_tools import BrowserController, BrowserError
from agent.tools_schema import (
    FUNCTION_DECLARATIONS,
    build_generic_task_prompt,
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


def _image_block(png_bytes: bytes) -> dict:
    """Wrap raw PNG bytes as an Anthropic base64 image content block."""
    data = base64.standard_b64encode(png_bytes).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


class WebAutomationAgent:
    """Drives a browser to complete a task using Claude's vision + tool use."""

    def __init__(self, config: Config, logger: Logger) -> None:
        if not config.api_keys:
            raise ValueError(
                "No Anthropic API key is set. Add ANTHROPIC_API_KEY to your .env "
                "file or environment before running the agent."
            )
        self.config = config
        self.log = logger
        # Rotation pool: start on the first key, advance to the next on a
        # rate-limit (429) or auth (401/403) error so a single exhausted/limited
        # key doesn't stop a run.
        self.api_keys = config.api_keys
        self._key_index = 0
        self.client = anthropic.Anthropic(api_key=self.api_keys[0])
        # Anthropic takes the tool list verbatim — each entry is already a
        # {name, description, input_schema} dict.
        self.tools = FUNCTION_DECLARATIONS
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

            # Free-form mode (a user task is set) vs the built-in form-fill demo.
            if self.config.task:
                task_text = build_generic_task_prompt(
                    self.config.task, self.config.target_url
                )
            else:
                task_text = build_task_prompt(
                    self.config.target_url,
                    self.config.name_value,
                    self.config.description_value,
                )

            # The conversation history (list of message dicts). It starts with the
            # task description and the first screenshot.
            messages: list[dict] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": task_text},
                        _image_block(screenshot),
                    ],
                }
            ]

            for step in range(1, self.config.max_steps + 1):
                result.steps_taken = step
                self.log.info("──── step %d/%d ────", step, self.config.max_steps)

                response = self._generate(messages)
                self._log_model_output(response)

                # Record the assistant turn verbatim. Passing the response content
                # blocks back unchanged preserves any thinking blocks, which Claude
                # uses to keep its reasoning coherent across tool-use turns.
                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "refusal":
                    self.log.warning("Model declined the request (refusal).")
                    result.summary = "Model refused the request."
                    break

                tool_uses = [b for b in response.content if b.type == "tool_use"]

                if not tool_uses:
                    # Model replied with text instead of a tool call — nothing left
                    # to do (or it is asking a question). End the loop.
                    self.log.info("Model ended turn without a tool call.")
                    result.summary = self._first_text(response.content) or (
                        "Model stopped without a tool call."
                    )
                    break

                # Execute each requested tool and build the reply turn. Every
                # tool_result carries the result text plus a fresh screenshot, so
                # Claude sees the new page state for its next decision.
                tool_results: list[dict] = []
                finished = False
                for tu in tool_uses:
                    text, screenshot, done, outcome = self._execute_tool(tu)
                    content: list[dict] = [{"type": "text", "text": text}]
                    if screenshot is not None:
                        content.append(_image_block(screenshot))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": content,
                        }
                    )
                    if done:
                        finished = True
                        result.success = outcome.success
                        result.summary = outcome.summary

                messages.append({"role": "user", "content": tool_results})

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

        except anthropic.APIError as exc:
            self.log.error("Claude API error: %s", exc)
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
    def _generate(self, messages: list[dict]):
        """Call ``messages.create`` with vision + tool-use config."""
        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_output_tokens,
            "system": self.system_prompt,
            "tools": self.tools,
            # One tool per turn, so each action gets its own screenshot before the
            # model decides the next step (the perceive→act→perceive loop).
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "messages": messages,
        }
        # Adaptive thinking lets Claude decide how much to reason before acting,
        # which improves coordinate accuracy. display="summarized" surfaces a
        # readable summary we log (the raw chain of thought is never returned).
        # --no-thinking omits the parameter entirely (faster, cheaper).
        if self.config.enable_thinking:
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        # Try the current key; on rate-limit (429) / auth (401/403), rotate to the
        # next key and retry the same request. Other errors propagate immediately.
        while True:
            try:
                return self.client.messages.create(**kwargs)
            except anthropic.APIStatusError as exc:
                code = getattr(exc, "status_code", None)
                if code in (429, 401, 403):
                    self.log.warning(
                        "API key #%d/%d failed (HTTP %s): %s",
                        self._key_index + 1, len(self.api_keys), code,
                        getattr(exc, "message", str(exc)),
                    )
                    if self._rotate_key():
                        self.log.info(
                            "Rotated to API key #%d/%d — retrying.",
                            self._key_index + 1, len(self.api_keys),
                        )
                        continue
                    self.log.error("All %d API keys exhausted.", len(self.api_keys))
                raise

    def _rotate_key(self) -> bool:
        """Switch the client to the next API key. Returns False if none remain."""
        if self._key_index + 1 >= len(self.api_keys):
            return False
        self._key_index += 1
        self.client = anthropic.Anthropic(api_key=self.api_keys[self._key_index])
        return True

    # ------------------------------------------------------------------ #
    # Tool execution
    # ------------------------------------------------------------------ #
    def _execute_tool(self, tool_use):
        """Run one tool call.

        Returns ``(result_text, screenshot_bytes_or_None, finished, outcome)``
        where ``finished`` is True only for ``report_task_complete``.
        """
        name = tool_use.name
        args = dict(tool_use.input or {})
        self.log.info("→ tool: %s %s", name, args)

        # report_task_complete ends the run and carries no screenshot — but only
        # if the page really contains the expected values. We re-check the DOM
        # here so the model cannot end on a hallucinated "success".
        if name == "report_task_complete":
            claimed = bool(args.get("success", False))
            # The DOM value check only applies to the built-in form-fill demo,
            # where we know exactly what text should be on the page. In free-form
            # mode there is no fixed expected value, so trust the model's report.
            if claimed and not self.config.task:
                ok, detail, screenshot = self._verify_expected_values()
                if not ok:
                    self.log.warning("report_task_complete REJECTED: %s", detail)
                    return (
                        "NOT complete. A direct read of the page shows: " + detail
                        + " Click the correct field at its visual CENTER, type the "
                        "missing text, call verify_form, and only then report complete.",
                        screenshot,
                        False,
                        None,
                    )
            outcome = RunResult(
                success=claimed,
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
        """Map a tool name to a BrowserController call; return (text, screenshot)."""
        if name == "take_screenshot":
            _, png = self.controller.take_screenshot("requested")
            return "Screenshot captured.", png

        if name == "verify_form":
            fields = self.controller.read_fields()
            _, png = self.controller.take_screenshot("verify_form")
            body = json.dumps(fields, ensure_ascii=False, indent=2)
            return f"Current field values read from the page:\n{body}", png

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

    def _verify_expected_values(self):
        """Confirm the configured Name/Description text is actually on the page.

        Reads the live field values from the DOM and checks that each expected
        value appears in some field. Label-agnostic on purpose: the page may call
        the field "Bug Title" rather than "Name", so we match on the *value* the
        user asked us to type, not on the label.

        Returns ``(ok, detail, screenshot_bytes_or_None)``.
        """
        fields = self.controller.read_fields()
        values = [str(f.get("value", "")) for f in fields]
        missing = []
        for label, expected in (
            ("Name", self.config.name_value),
            ("Description", self.config.description_value),
        ):
            exp = (expected or "").strip()
            if exp and not any(exp in v for v in values):
                shown = exp if len(exp) <= 40 else exp[:40] + "…"
                missing.append(f'the {label} value "{shown}"')

        screenshot = None
        try:
            _, screenshot = self.controller.take_screenshot("verify_complete")
        except Exception:
            pass

        if missing:
            return False, "no field contains " + "; ".join(missing) + ".", screenshot
        return True, "all expected values are present.", screenshot

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #
    def _log_model_output(self, response) -> None:
        """Log thinking summaries / text and token usage from a model response."""
        for block in response.content or []:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                thought = getattr(block, "thinking", "")
                if thought:
                    self.log.info("[thinking] %s", _truncate(thought))
            elif btype == "text":
                if block.text:
                    self.log.info("[assistant] %s", _truncate(block.text))
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.log.debug(
                "tokens input=%s output=%s cache_read=%s",
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
                getattr(usage, "cache_read_input_tokens", "?"),
            )

    @staticmethod
    def _first_text(content) -> str:
        for block in content or []:
            if getattr(block, "type", None) == "text" and block.text:
                return block.text
        return ""


def _truncate(text: str, limit: int = _LOG_SNIPPET) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
