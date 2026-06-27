# Website Automation Agent (Vision-based, Claude + Playwright)

An intelligent browser-automation agent — a mini "Browser Use". It opens a real
Chromium browser, **looks at screenshots** of the page, and decides where to
click and what to type to fill in a form, with no hard-coded selectors.

The target task: navigate to
[ui.shadcn.com/docs/forms/react-hook-form](https://ui.shadcn.com/docs/forms/react-hook-form),
find the **Name** and **Description** fields, and fill them in automatically.

The "brain" is **Anthropic Claude** (`claude-opus-4-8`) driving the browser
through tool use; the "hands" are **Playwright**. Claude receives a screenshot
after every action and issues the next action as a tool call
(`click_on_screen`, `send_keys`, `scroll`, …) until the form is filled.

---

## How it works (in one picture)

```
            ┌──────────────────────────────────────────────┐
            │                 WebAutomationAgent             │
            │            (the agentic decision loop)         │
            └──────────────────────────────────────────────┘
                 │  screenshot (image) + history          ▲
                 ▼                                         │ tool call
        ┌─────────────────┐                       ┌────────────────────┐
        │   Claude API    │ ───────────────────►  │  BrowserController  │
        │ (vision + tools)│   click/type/scroll   │     (Playwright)     │
        └─────────────────┘                       └────────────────────┘
                                                            │
                                                            ▼
                                                     Chromium browser
```

Each loop iteration: Claude sees the latest screenshot → picks one tool → the
controller performs it → a new screenshot is sent back inside the `tool_result`
→ repeat. Claude ends the run by calling `report_task_complete`.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design rationale.

---

## Required capabilities (assignment checklist)

All implemented in [`agent/browser_tools.py`](agent/browser_tools.py):

| Capability         | Method                              | Exposed to the LLM as a tool |
| ------------------ | ----------------------------------- | ---------------------------- |
| `open_browser`     | `BrowserController.open_browser`    | (harness — runs at startup)  |
| `navigate_to_url`  | `BrowserController.navigate_to_url` | ✅ `navigate_to_url`         |
| `take_screenshot`  | `BrowserController.take_screenshot` | ✅ `take_screenshot`         |
| `click_on_screen`  | `BrowserController.click_on_screen` | ✅ `click_on_screen`         |
| `double_click`     | `BrowserController.double_click`    | ✅ `double_click`            |
| `send_keys`        | `BrowserController.send_keys`       | ✅ `send_keys`               |
| `scroll`           | `BrowserController.scroll`          | ✅ `scroll`                  |

Plus two agent-control tools: `verify_form` (reads the fields' real values from
the DOM so the agent confirms — not guesses — what it typed) and
`report_task_complete` (signals the run is done, and is only accepted if the live
page actually contains the requested values).

---

## Setup

### 1. Prerequisites
- **Python 3.10+**
- An **Anthropic API key** — get one at <https://console.anthropic.com/settings/keys>

### 2. Install dependencies

```bash
# (recommended) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# install Python packages
pip install -r requirements.txt

# install the Chromium browser Playwright drives
playwright install chromium
```

### 3. Configure your API key

```bash
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

Everything else in `.env` is optional (it has defaults).

---

## Running

```bash
python main.py
```

On launch it **asks what you want it to do**. You can either:

- **Type a free-form task** and it will carry it out autonomously, e.g.
  `open youtube, search fifa world cup, click the second video`
- **Press Enter** to run the built-in **form-filling demo** (the assignment's
  target task on the shadcn page).

You can also give the task up front instead of being prompted:

```bash
# Free-form task (the agent navigates wherever it needs)
python main.py --task "open youtube, search fifa world cup, click the second video"

# Start the task on a specific page
python main.py --task "find the pricing page and screenshot it" --url https://playwright.dev

# The form-fill demo with custom values
python main.py --name "Ada Lovelace" --description "Wrote the first algorithm."
```

Useful flags (all optional; they override `.env`):

```bash
python main.py --headless                  # no visible window
python main.py --no-thinking               # disable Claude adaptive thinking (faster, cheaper)
python main.py --model claude-sonnet-4-6   # use a cheaper/faster model
python main.py --max-steps 40              # raise the safety step limit
python main.py --log-level DEBUG           # verbose logging (incl. token usage)
```

> In free-form mode the agent decides every step from what it sees, so results
> vary with the site (cookie popups, layout changes). Complex tasks may need a
> higher `--max-steps`. The DOM value-check only guards the form demo; free-form
> runs end when the model judges the task visually complete.

While it runs you will see, both on screen and in `logs/agent_<timestamp>.log`:
- the model's summarized **thinking** and any text,
- each **tool call** with its arguments,
- the path of every **screenshot** saved to `screenshots/`,
- a final **RESULT / Summary** block.

Exit code is `0` on success, `1` if the task was not completed, `2` if no API
key is configured.

---

## Project structure

```
GenAI-Assignment-04/
├── main.py                 # CLI entry point (arg parsing + run)
├── config.py               # typed configuration (env + CLI defaults)
├── requirements.txt
├── .env.example            # copy to .env and add your key
├── README.md
├── ARCHITECTURE.md         # design decisions & agent workflow
├── agent/
│   ├── __init__.py
│   ├── agent.py            # WebAutomationAgent — the Claude decision loop
│   ├── browser_tools.py    # BrowserController — Playwright capabilities
│   ├── tools_schema.py     # tool definitions + system/task prompts
│   └── logger.py           # console + file logging
├── screenshots/            # screenshots from each run (gitignored)
└── logs/                   # per-run log files (gitignored)
```

---

## Why vision instead of selectors?

Coordinate-based clicking driven by what Claude *sees* is robust to things that
break CSS/XPath selectors:

- The shadcn docs render component demos **inside an `<iframe>`** — selector
  queries against the top document miss them, but a mouse click at screen
  coordinates and keystrokes to the focused element work regardless.
- No dependence on class names / DOM structure that can change.
- It generalises: point the agent at a different form and it adapts.

To keep coordinates reliable, the browser is opened with `device_scale_factor=1`
and a fixed `1280x800` viewport, so a pixel in the screenshot maps **1:1** to the
coordinate Playwright clicks.

---

## Why Claude (and which model)?

- **Vision + tool use + thinking** in one model, which is exactly the
  combination this agent needs: it looks at a screenshot, reasons about where the
  target's center is, and emits a structured tool call.
- **`claude-opus-4-8`** is the default — Anthropic's most capable model, with
  strong vision and long-horizon agentic behaviour. Coordinate accuracy benefits
  from its **adaptive thinking**.
- Swap models with `--model` (e.g. `claude-sonnet-4-6` for lower cost/latency, or
  `claude-haiku-4-5` for the cheapest runs). The agent is provider-pinned to
  Anthropic but model-agnostic within it.

> Unlike a free tier, Anthropic API usage is billed per token, and each run
> attaches a screenshot to every turn (vision tokens). `--no-thinking` and a
> smaller `--model` both reduce cost; see Troubleshooting below.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ANTHROPIC_API_KEY is not set` | Create `.env` from `.env.example` and add your key. |
| `playwright ... Executable doesn't exist` | Run `playwright install chromium`. |
| Browser opens but nothing happens / blank | The site may be slow; re-run, or raise `NAV_TIMEOUT_MS` in `.env`. |
| Output truncated / `stop_reason: max_tokens` | Raise `MAX_OUTPUT_TOKENS` in `.env`. |
| Agent clicks slightly off | Keep `ENABLE_THINKING=true`, or stay on `claude-opus-4-8`. |
| Hits the step limit | Raise `--max-steps`; check `logs/` to see where it got stuck. |
| `429 RateLimitError` | You hit your account's rate limit — wait and re-run, add a fallback key (`ANTHROPIC_API_KEY_2`), or slow the loop. |
| Want to cut cost | Use `--no-thinking` and/or `--model claude-sonnet-4-6`. |

---

## Notes

- The agent rotates across multiple keys (`ANTHROPIC_API_KEY`, `..._2`, …) when
  one is rate-limited or rejected, so a single limited key doesn't stop a run.
- A single run uses a handful of model turns with screenshots attached.
  `--no-thinking` reduces token use and latency.
- All actions are logged and every screenshot is saved, so each run is fully
  auditable after the fact.
