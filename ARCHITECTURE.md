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
| Brain | `WebAutomationAgent` (`agent/agent.py`) | Runs the agentic loop, calls Gemini, interprets function calls, manages the conversation. |
| Hands | `BrowserController` (`agent/browser_tools.py`) | Performs concrete browser actions via Playwright. |
| Contract | `tools_schema.py` | The function declarations + prompts that connect the two. |
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
1:1 to click coordinates, and (b) using Gemini 2.5's "thinking" so the model
reasons about where the target's center is before clicking.

### Why Gemini?

`gemini-2.5-flash` combines **vision**, **function calling**, and **thinking** in
a single model on a **free tier**, which is exactly what this agent needs. The
brain is built on the `google-genai` SDK (`genai.Client(...).models.generate_content`).

---

## 3. The tool surface

Each function maps 1:1 to a `BrowserController` method and is declared to Gemini
in `tools_schema.py` using its function-calling schema (OpenAPI subset). The
declarations are deliberately **prescriptive about *when* to call** each tool,
which improves tool-selection accuracy.

```
take_screenshot   navigate_to_url   click_on_screen   double_click
send_keys         scroll            report_task_complete
```

`open_browser` is a controller capability invoked by the harness at startup (you
cannot screenshot a page before a browser exists), so it is not exposed as a
model-callable tool. `report_task_complete` is the agent's way to declare the
job done and end the loop.

### Visual feedback after every action

The key mechanism: **after each action, the agent sends the result back as a
`functionResponse` part *plus a fresh screenshot* (an inline image part) in the
same user turn.** Gemini's function responses carry structured JSON (not images),
so the screenshot is attached as a separate image part alongside the response.
This gives the model an immediate look at the new page state so it can verify its
last action before deciding the next one. This tight perceive→act→perceive loop
is what makes the vision approach work.

---

## 4. The agent loop

Implemented in `WebAutomationAgent.run()` as a *manual function-calling loop* (we
own the loop so we can log everything and attach screenshots; **automatic function
calling is disabled** because it would invoke our functions without giving the
model a fresh screenshot in between):

```
open_browser()                      # harness
navigate_to_url(target)             # harness
screenshot ──┐
             ▼
   ┌──> contents = [ user: task text + first screenshot ]
   │
   │   loop (bounded by max_steps):
   │     response = client.models.generate_content(model, contents, config)
   │     log thought summaries / text / token usage
   │     model_content = response.candidates[0].content
   │     contents.append(model_content)          # preserves thought signatures
   │     function_calls = [parts with .function_call]
   │     if none:  break                          # model replied with text only
   │     for each function_call:
   │        result_text, screenshot = execute via BrowserController
   │        add functionResponse(name, {"output": result_text})
   │        add image part (screenshot)
   │        if report_task_complete: mark finished
   │     contents.append( user: [functionResponses...] + [screenshots...] )
   └─    if finished: break
close()                             # always, in finally
```

Design points:

- **Full transcript is replayed each turn** (the API is stateless). The model
  `Content` — including any **thought** parts/signatures — is appended verbatim,
  which Gemini uses to keep its reasoning coherent across function-calling turns.
- **Thinking** is configured via `types.ThinkingConfig(include_thoughts=True,
  thinking_budget=-1)` on the `2.5` family (`-1` = let the model decide;
  `--no-thinking` sets it to `0`). `include_thoughts` surfaces a summary we log.
- **Function responses first, screenshots after.** All `functionResponse` parts
  are grouped ahead of the image parts in the reply turn, which keeps Gemini's
  call↔response matching unambiguous when more than one call is returned.
- **Bounded loop.** `max_steps` (default 25) guarantees termination even if the
  model never calls `report_task_complete`.

---

## 5. Element detection & "intelligence"

Element identification is **visual recognition by the model**: Gemini locates the
Name/Description fields in the screenshot and emits the pixel coordinates to
click. The agent demonstrates decision-making by:

- scrolling when the target is not in the current viewport,
- focusing a field before typing,
- verifying via the post-action screenshot that text landed in the right field,
- recovering from errors (a failed tool returns an error message + screenshot so
  the model can re-plan instead of crashing).

---

## 6. Error handling

Handled at several layers:

- **Browser layer.** `BrowserController` raises a typed `BrowserError` for
  out-of-bounds coordinates or actions before `open_browser`. Navigation tolerates
  a `networkidle` timeout (common on chatty SPAs) and continues once the DOM is
  ready.
- **Tool layer.** A failed tool is caught, logged, and returned to the model as a
  `functionResponse` whose output is the error message, plus a current screenshot
  — so the model can adapt instead of the run aborting.
- **Loop layer.** The whole run is wrapped in try/except for
  `google.genai.errors.APIError`, `BrowserError`, and any unexpected exception;
  the browser is always closed in a `finally`. A `max_steps` cap prevents
  infinite loops. Empty/blocked responses (no candidates) are detected and end the
  run cleanly.

---

## 7. Logging & observability

`logger.py` configures one logger that writes to the console (watch it live) and
to `logs/agent_<timestamp>.log` (audit later). It records the model's thought
summaries and text, every function call with arguments, every screenshot path,
token usage (at `DEBUG`: prompt/output/thoughts token counts), and a final result
block. Every screenshot is also saved under `screenshots/`, giving a
frame-by-frame record of what the agent saw and did.

---

## 8. Configuration

`config.py` provides a single typed `Config` dataclass populated from environment
variables (via `.env`) with defaults, then overridable by CLI flags in `main.py`.
The API key is read from `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) and never
hard-coded. This keeps secrets and tunables (model, thinking, viewport, timeouts,
values to type, step limit) out of the code.

---

## 9. Possible extensions

- **Grid/element overlays** on screenshots to further improve click precision.
- **Context pruning** of old screenshots for very long tasks (to limit tokens and
  stay within free-tier limits).
- **DOM-assisted hybrid mode**: feed the accessibility tree alongside the image
  for tasks where text labels are more reliable than pixels.
- **Self-verification step**: read the field values back via the DOM and confirm
  they match the requested text before reporting success.
