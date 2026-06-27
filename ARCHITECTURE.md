# Architecture & Design

This document explains the design decisions behind the website-automation agent
and walks through its end-to-end workflow.

---

## 1. Goal

Build an agent that, given a URL and a small task ("fill the Name and Description
fields"), completes it autonomously — deciding *what* to do by perceiving the
page, not by following a fixed script. This mirrors how tools like *Browser Use*
work and demonstrates AI-driven browser control.

---

## 2. High-level design: brain + hands

The system separates **decision-making** from **actuation**:

| Layer | Component | Responsibility |
| --- | --- | --- |
| Brain | `WebAutomationAgent` (`agent/agent.py`) | Runs the agentic loop, calls Claude, interprets tool calls, manages the conversation. |
| Hands | `BrowserController` (`agent/browser_tools.py`) | Performs concrete browser actions via Playwright. |
| Contract | `tools_schema.py` | The tool definitions + prompts that connect the two. |
| Config / IO | `config.py`, `logger.py`, `main.py` | Settings, logging, CLI. |

This separation keeps each piece small and independently testable, and means the
"intelligence" (prompting, tool selection) is isolated from the mechanics
(clicking, typing, screenshots). It also made swapping the model provider a
localized change: only the brain layer (`agent.py`, plus the schema format in
`tools_schema.py`) is provider-specific — the browser layer is untouched.

### Why a *vision* agent?

The design drives the browser by **screenshots + pixel coordinates** rather than
DOM selectors. Reasons:

1. **Robustness to iframes.** The shadcn docs embed live component demos inside
   `<iframe>`s. A coordinate click hits whatever pixel is on screen and keystrokes
   go to the focused element — both work across iframe boundaries, where
   `page.query_selector` against the main document would not.
2. **No brittle selectors.** Class names and DOM structure change; what a field
   *looks like* is far more stable.
3. **Generality.** The same agent adapts to a different form with no code change.

The trade-off — coordinate clicks must be accurate — is mitigated by (a) locking
`device_scale_factor=1` and a fixed `1280×800` viewport so screenshot pixels map
1:1 to click coordinates, and (b) using Claude's **adaptive thinking** so the
model reasons about where the target's center is before clicking.

### Why Claude?

`claude-opus-4-8` combines **vision**, **tool use**, and **adaptive thinking** in
a single model, which is exactly what this agent needs. The brain is built on the
official `anthropic` SDK (`anthropic.Anthropic(...).messages.create`). The model
is swappable within Anthropic (`--model claude-sonnet-4-6` for lower cost), but
the provider is pinned: only `agent.py` and the schema format in
`tools_schema.py` would change to move to a different vision LLM.

---

## 3. The tool surface

Each tool maps 1:1 to a `BrowserController` method and is declared to Claude in
`tools_schema.py` as a `{name, description, input_schema}` object — `input_schema`
is plain JSON Schema. The descriptions are deliberately **prescriptive about
*when* to call** each tool, which improves tool-selection accuracy (recent Claude
models reach for tools conservatively, so a clear "call this when…" trigger
helps).

```
take_screenshot   navigate_to_url   click_on_screen   double_click
send_keys         scroll            verify_form       report_task_complete
```

`verify_form` reads the form fields' real values from the DOM (ground truth), so
the agent confirms what it actually typed instead of trusting the screenshot.

`open_browser` is a controller capability invoked by the harness at startup (you
cannot screenshot a page before a browser exists), so it is not exposed as a
model-callable tool. `report_task_complete` is the agent's way to declare the
job done and end the loop.

### Visual feedback after every action

The key mechanism: **after each action, the agent sends the result back as a
`tool_result` block that contains BOTH the result text AND a fresh screenshot
(an image content block) in the same user turn.** Anthropic's `tool_result`
blocks accept a list of content blocks — including images — so the screenshot
travels *inside* the tool result rather than as a separate part. This gives the
model an immediate look at the new page state so it can verify its last action
before deciding the next one. This tight perceive→act→perceive loop is what makes
the vision approach work.

To enforce one screenshot per action, the request sets
`tool_choice={"type": "auto", "disable_parallel_tool_use": True}`, so Claude
calls at most one tool per turn.

---

## 4. The agent loop

Implemented in `WebAutomationAgent.run()` as a *manual tool-use loop* (we own the
loop so we can log everything and attach screenshots; we do **not** use the SDK's
automatic tool runner, which would call our functions without giving the model a
fresh screenshot in between):

```
open_browser()                      # harness
navigate_to_url(target)             # harness
screenshot ──┐
             ▼
   ┌──> messages = [ user: task text + first screenshot (image block) ]
   │
   │   loop (bounded by max_steps):
   │     response = client.messages.create(model, system, tools, messages, thinking, ...)
   │     log thinking summaries / text / token usage
   │     messages.append({role: "assistant", content: response.content})  # preserves thinking blocks
   │     if response.stop_reason == "refusal":  break
   │     tool_uses = [blocks where .type == "tool_use"]
   │     if none:  break                         # model replied with text only
   │     for each tool_use:
   │        result_text, screenshot = execute via BrowserController
   │        tool_results.append( tool_result(tool_use_id, [ text, image ]) )
   │        if report_task_complete: mark finished
   │     messages.append({role: "user", content: tool_results})
   └─    if finished: break
close()                             # always, in finally
```

Design points:

- **Full transcript is replayed each turn** (the API is stateless). The assistant
  `content` — including any **thinking** blocks — is appended verbatim, which
  Claude uses to keep its reasoning coherent across tool-use turns. (Modifying a
  thinking block would be rejected; we pass them back unchanged.)
- **Thinking** is configured via `thinking={"type": "adaptive", "display":
  "summarized"}`. Adaptive lets the model decide how much to think;
  `"summarized"` surfaces a readable summary we log (the raw chain of thought is
  never returned). `--no-thinking` omits the parameter entirely.
- **Every `tool_use` is answered.** Each assistant turn that contains a `tool_use`
  is followed by a single user turn carrying a matching `tool_result` (text +
  screenshot) for every call — the API requires this pairing.
- **Bounded loop.** `max_steps` (default 25) guarantees termination even if the
  model never calls `report_task_complete`.

---

## 5. Element detection & "intelligence"

Element identification is **visual recognition by the model**: Claude locates the
Name/Description fields in the screenshot and emits the pixel coordinates to
click. The agent demonstrates decision-making by:

- scrolling when the target is not in the current viewport,
- focusing a field before typing,
- mapping the requested fields onto the page's real labels (e.g. the demo form's
  "Bug Title" input plays the role of the "Name" field),
- verifying via the post-action screenshot that text landed in the right field,
- confirming the result against DOM ground truth with `verify_form` before
  finishing — and re-typing any field that did not take,
- recovering from errors (a failed tool returns an error message + screenshot so
  the model can re-plan instead of crashing).

### Guarding against silent failure and false success

Two safeguards make the run trustworthy:

1. **Focus guard.** `send_keys` refuses to type (or select-all-clear) unless a
   real input/textarea is focused. If a click misses its target, focus is on the
   page body — without this guard, `clear_first` would `Cmd/Ctrl+A` the *whole
   document* and the text would be lost. Instead the tool returns an error and the
   model re-clicks. (This is exactly the failure mode an early test hit: a click
   landed just above the textarea and the description silently went nowhere.)
2. **Completion check.** `report_task_complete` is not taken at face value — the
   agent re-reads the live field values and confirms the configured Name and
   Description text is actually present. If not, completion is rejected and the
   model is told to fix the missing field. The model cannot end on a hallucinated
   success.

---

## 6. Error handling

Handled at several layers:

- **Browser layer.** `BrowserController` raises a typed `BrowserError` for
  out-of-bounds coordinates or actions before `open_browser`. Navigation tolerates
  a `networkidle` timeout (common on chatty SPAs) and continues once the DOM is
  ready.
- **Tool layer.** A failed tool is caught, logged, and returned to the model as a
  `tool_result` whose text is the error message, plus a current screenshot — so
  the model can adapt instead of the run aborting.
- **Loop layer.** The whole run is wrapped in try/except for `anthropic.APIError`,
  `BrowserError`, and any unexpected exception; the browser is always closed in a
  `finally`. A `max_steps` cap prevents infinite loops. Refusals
  (`stop_reason == "refusal"`) and text-only replies are detected and end the run
  cleanly.
- **API-key rotation.** On a rate-limit (429) or auth (401/403) error, the agent
  rotates to the next configured key (`ANTHROPIC_API_KEY_2`, …) and retries the
  same request; if all keys are exhausted, the error propagates. (The SDK already
  retries transient 429/5xx with backoff before we ever rotate.)

---

## 7. Logging & observability

`logger.py` configures one logger that writes to the console (watch it live) and
to `logs/agent_<timestamp>.log` (audit later). It records the model's thinking
summaries and text, every tool call with arguments, every screenshot path, token
usage (at `DEBUG`: input/output/cache-read token counts), and a final result
block. Every screenshot is also saved under `screenshots/`, giving a
frame-by-frame record of what the agent saw and did.

---

## 8. Configuration

`config.py` provides a single typed `Config` dataclass populated from environment
variables (via `.env`) with defaults, then overridable by CLI flags in `main.py`.
The API key is read from `ANTHROPIC_API_KEY` and never hard-coded. This keeps
secrets and tunables (model, thinking, viewport, timeouts, values to type, step
limit) out of the code.

---

## 9. Possible extensions

- **Grid/element overlays** on screenshots to further improve click precision.
- **Prompt caching** of the system prompt and tool definitions to cut cost on
  long runs (the stable prefix is identical every turn).
- **Context editing** to prune old screenshots/tool results for very long tasks
  (to limit tokens and cost).
- **DOM-assisted hybrid mode**: feed the accessibility tree alongside the image
  for tasks where text labels are more reliable than pixels.
