"""Function declarations and prompts shared with the Gemini model.

The declarations below are what the model "sees". Each maps 1:1 to a method on
:class:`~agent.browser_tools.BrowserController`. They use Gemini's function-
calling schema (OpenAPI subset with UPPERCASE type names), and descriptions are
written to be *prescriptive about when to call the tool*, which improves tool
selection.
"""
from __future__ import annotations

# Gemini function declarations (passed to ``types.Tool(function_declarations=...)``).
# A declaration with no "parameters" key is a no-argument function.
FUNCTION_DECLARATIONS = [
    {
        "name": "take_screenshot",
        "description": (
            "Capture a fresh screenshot of the current browser viewport. You "
            "normally do not need this, because every other tool already returns "
            "a screenshot of its result."
        ),
    },
    {
        "name": "navigate_to_url",
        "description": (
            "Load a different web page. Use only if you need to leave the current "
            "page; the target page is already open when the task begins."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url": {"type": "STRING", "description": "Absolute URL to open."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "click_on_screen",
        "description": (
            "Left-click once at pixel coordinates (x, y) measured from the "
            "top-left of the latest screenshot. Use this to focus a text field "
            "before typing, or to press a button. Click the CENTER of the target."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "x": {"type": "INTEGER", "description": "Horizontal pixel (0 = left edge)."},
                "y": {"type": "INTEGER", "description": "Vertical pixel (0 = top edge)."},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "double_click",
        "description": (
            "Double-click at pixel coordinates (x, y). Useful to select a word, "
            "or to activate controls that require a double-click."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "x": {"type": "INTEGER", "description": "Horizontal pixel."},
                "y": {"type": "INTEGER", "description": "Vertical pixel."},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "send_keys",
        "description": (
            "Type text into (or press a key in) the element that currently has "
            "focus. First click the field you want, then call send_keys with the "
            "text. Set clear_first=true to replace any text already in the field. "
            "Use 'press' for a single key such as 'Enter' or 'Tab'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "Literal text to type."},
                "press": {
                    "type": "STRING",
                    "description": "A single key to press instead of typing, e.g. 'Enter', 'Tab'.",
                },
                "clear_first": {
                    "type": "BOOLEAN",
                    "description": "Select-all and delete before typing (default false).",
                },
            },
        },
    },
    {
        "name": "scroll",
        "description": (
            "Scroll the page vertically to bring off-screen content into view. "
            "Use this when the field or button you need is not visible in the "
            "current screenshot."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "direction": {
                    "type": "STRING",
                    "enum": ["up", "down"],
                    "description": "Scroll direction (default 'down').",
                },
                "amount": {
                    "type": "INTEGER",
                    "description": "Pixels to scroll (default 400).",
                },
            },
        },
    },
    {
        "name": "verify_form",
        "description": (
            "Read the ACTUAL current values of the form fields straight from the "
            "page (ground truth — not a guess from the screenshot). Returns each "
            "field's label and the text it currently contains. ALWAYS call this "
            "right before report_task_complete: if any field you were asked to "
            "fill is empty or wrong, click its CENTER and re-type, then verify "
            "again. Only report complete once verify_form shows the correct text."
        ),
    },
    {
        "name": "report_task_complete",
        "description": (
            "Call this once the task is fully finished and you have visually "
            "confirmed it in a screenshot. This ends the run."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "success": {
                    "type": "BOOLEAN",
                    "description": "True if the task was completed successfully.",
                },
                "summary": {
                    "type": "STRING",
                    "description": "One or two sentences describing what you did.",
                },
            },
            "required": ["success", "summary"],
        },
    },
]


def build_system_prompt(width: int, height: int) -> str:
    """Return the system prompt, parameterised by the viewport size."""
    return (
        "You are an autonomous web-automation agent. You control a real Chromium "
        "browser through a small set of tools, and you decide what to do by "
        "LOOKING at screenshots of the page, exactly like a person would.\n\n"
        f"The browser viewport is {width}x{height} pixels. Every screenshot you "
        f"receive is a PNG of the current viewport at that exact size. When you "
        "click, you give coordinates in pixels from the TOP-LEFT corner "
        f"(x: 0..{width} left-to-right, y: 0..{height} top-to-bottom). The "
        "coordinate you choose maps 1:1 to the pixel in the screenshot, so aim "
        "for the visual CENTER of your target.\n\n"
        "You can only interact with what is currently visible. If the element you "
        "need is not on screen, use the `scroll` tool and then look again.\n\n"
        "To fill a text field:\n"
        "  1. Locate the field in the latest screenshot.\n"
        "  2. `click_on_screen` at the VISUAL CENTER of the input box itself — for a "
        "multi-line text area click the middle of the box, NOT on its label or the "
        "gap just below the label, or the click will miss and focus nothing.\n"
        "  3. `send_keys` with the text (use clear_first=true if it already has text). "
        "If send_keys reports that no field is focused, your click missed — click the "
        "box center again, then retype.\n"
        "  4. Check the next screenshot to confirm the text landed in the RIGHT field.\n\n"
        "Work one step at a time, calling exactly one tool per turn. After every "
        "action you receive a new screenshot showing the result — use it to verify "
        "before moving on, and never repeat an action that already succeeded.\n\n"
        "Do NOT trust the screenshot alone to declare success: before finishing, call "
        "`verify_form` to read the fields' real values from the page. Only when "
        "verify_form shows every required field holds the correct text may you call "
        "`report_task_complete`."
    )


def build_task_prompt(url: str, name_value: str, description_value: str) -> str:
    """Return the first user message describing the concrete task."""
    return (
        f"TASK: The page {url} is already open in the browser. Find the form on "
        "the page and fill it in:\n\n"
        f'  - Name field         -> "{name_value}"\n'
        f'  - Description field   -> "{description_value}"\n\n'
        "A screenshot of the current page is attached. The form may be below the "
        "visible area, so scroll down if you do not see the Name/Description "
        "fields. Some pages render the form inside a preview frame; clicking by "
        "coordinates still works there.\n\n"
        "Note: the field labels on the page may not be literally 'Name'/'Description' "
        "(for example a 'Bug Title' field plays the role of the Name field) — map the "
        "requested values to the most appropriate single-line text input and the "
        "multi-line text area.\n\n"
        "Once both fields look filled, call `verify_form` to confirm their real values "
        "from the page; fix any that are empty or wrong, then call "
        "report_task_complete with a short summary."
    )
