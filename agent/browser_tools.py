"""Low-level browser capabilities (the agent's "hands").

``BrowserController`` wraps Playwright's synchronous Chromium API and exposes
exactly the capabilities the assignment requires:

    open_browser      - launch a Chromium instance
    navigate_to_url   - go to a URL
    take_screenshot   - capture the current viewport
    click_on_screen   - click at pixel coordinates (x, y)
    double_click      - double-click at pixel coordinates (x, y)
    send_keys         - type text / press a key into the focused element
    scroll            - scroll the page up or down
    close             - shut everything down

Two design decisions make the coordinate-driven (vision) approach reliable:

1. ``device_scale_factor=1`` plus a fixed ``viewport`` mean screenshot pixels map
   1:1 to the CSS pixels used by ``page.mouse`` — so a coordinate the model reads
   off a screenshot is exactly where the click lands.
2. Clicks are issued through ``page.mouse`` at absolute viewport coordinates and
   typing goes to whatever element currently has focus. This works even when the
   target is inside an ``<iframe>`` (e.g. a component preview), where CSS/XPath
   selectors would otherwise fail.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from logging import Logger

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from config import Config


class BrowserError(Exception):
    """Raised when a browser action cannot be completed."""


class BrowserController:
    """A thin, well-logged wrapper around Playwright's Chromium browser."""

    def __init__(self, config: Config, logger: Logger) -> None:
        self.config = config
        self.log = logger
        self._playwright = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self._shot_index = 0
        os.makedirs(config.screenshots_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def open_browser(self) -> None:
        """Launch Chromium and open a single page with a fixed viewport."""
        self.log.info(
            "open_browser (headless=%s, viewport=%dx%d)",
            self.config.headless,
            self.config.viewport_width,
            self.config.viewport_height,
        )
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.config.headless)
        # device_scale_factor=1 keeps screenshot pixels == click coordinates.
        self._context = self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            device_scale_factor=1,
        )
        self._context.set_default_timeout(self.config.nav_timeout_ms)
        self.page = self._context.new_page()

    def close(self) -> None:
        """Tear down the page, context, browser and Playwright driver."""
        self.log.info("close browser")
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # best-effort cleanup
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def navigate_to_url(self, url: str) -> str:
        """Navigate to ``url`` and wait for the DOM to be ready."""
        self._require_page()
        self.log.info("navigate_to_url -> %s", url)
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            # Give client-side rendering (React) a moment to paint.
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            # networkidle can legitimately never fire on chatty pages; the DOM
            # is already loaded at this point, so this is non-fatal.
            self.log.warning("networkidle timeout (continuing) for %s", url)
        self._settle()
        return f"Navigated to {self.page.url}"

    # ------------------------------------------------------------------ #
    # Observation
    # ------------------------------------------------------------------ #
    def take_screenshot(self, label: str = "") -> tuple[str, bytes]:
        """Capture the current viewport.

        Returns a ``(file_path, png_bytes)`` tuple. The PNG is saved to disk
        (for the audit trail) and the raw bytes are returned so the caller can
        hand them to the model (Gemini accepts image bytes directly).
        """
        self._require_page()
        self._shot_index += 1
        stamp = datetime.now().strftime("%H%M%S")
        safe_label = (label or "view").replace(" ", "_")
        path = os.path.join(
            self.config.screenshots_dir, f"{self._shot_index:02d}_{stamp}_{safe_label}.png"
        )
        png_bytes = self.page.screenshot(path=path)
        self.log.info("take_screenshot -> %s", path)
        return path, png_bytes

    # ------------------------------------------------------------------ #
    # Interaction
    # ------------------------------------------------------------------ #
    def click_on_screen(self, x: int, y: int) -> str:
        """Single left-click at viewport pixel coordinates ``(x, y)``."""
        self._require_page()
        self._check_bounds(x, y)
        self.log.info("click_on_screen (%d, %d)", x, y)
        self.page.mouse.click(x, y)
        self._settle()
        return f"Clicked at ({x}, {y})"

    def double_click(self, x: int, y: int) -> str:
        """Double left-click at viewport pixel coordinates ``(x, y)``."""
        self._require_page()
        self._check_bounds(x, y)
        self.log.info("double_click (%d, %d)", x, y)
        self.page.mouse.dblclick(x, y)
        self._settle()
        return f"Double-clicked at ({x}, {y})"

    def send_keys(
        self,
        text: str | None = None,
        press: str | None = None,
        clear_first: bool = False,
    ) -> str:
        """Type ``text`` or press a named key into the focused element.

        Args:
            text: Literal text to type (character by character).
            press: A key name to press instead of typing, e.g. ``"Enter"`` or
                ``"Tab"`` (see Playwright key names).
            clear_first: If true, select-all + delete before typing so existing
                content in the field is replaced rather than appended.
        """
        self._require_page()
        # Guard: typing (or a select-all clear) only makes sense when a text field
        # is actually focused. If the previous click missed its target, the focus
        # is on the page body — typing would be lost and ``clear_first`` would
        # Cmd+A the WHOLE document. Refuse and tell the model to click first, so it
        # re-plans instead of silently filling nothing.
        if (text or clear_first) and not self.focused_editable():
            raise BrowserError(
                "No editable field is focused — your last click probably missed. "
                "Click the input/textarea (its visual CENTER) first, then send_keys."
            )
        if clear_first:
            select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
            self.page.keyboard.press(select_all)
            self.page.keyboard.press("Delete")
            self.log.info("send_keys cleared focused field")

        if text:
            self.log.info("send_keys type %r", text)
            self.page.keyboard.type(text, delay=20)
        if press:
            self.log.info("send_keys press %s", press)
            self.page.keyboard.press(press)
        self._settle()

        parts = []
        if clear_first:
            parts.append("cleared field")
        if text:
            parts.append(f"typed {text!r}")
        if press:
            parts.append(f"pressed {press}")
        return "send_keys: " + ", ".join(parts) if parts else "send_keys: no-op"

    def scroll(self, direction: str = "down", amount: int = 400) -> str:
        """Scroll the page vertically by ``amount`` pixels."""
        self._require_page()
        delta = amount if direction.lower() == "down" else -amount
        self.log.info("scroll %s %dpx", direction, amount)
        self.page.mouse.wheel(0, delta)
        self._settle()
        return f"Scrolled {direction} by {amount}px"

    # ------------------------------------------------------------------ #
    # Verification (DOM ground truth)
    # ------------------------------------------------------------------ #
    def focused_editable(self) -> str:
        """Return the focused element's tag if it is editable, else ``""``.

        Walks into same-origin iframes so it also works when the form is rendered
        inside a component-preview frame. Used to verify a click actually landed
        in a text field before we type.
        """
        self._require_page()
        js = """() => {
            function deepActive(doc) {
                let el = doc.activeElement;
                while (el && el.tagName === 'IFRAME') {
                    try {
                        const inner = el.contentDocument;
                        if (!inner) break;
                        doc = inner; el = doc.activeElement;
                    } catch (e) { return null; }   // cross-origin: give up
                }
                return el;
            }
            const el = deepActive(document);
            if (!el) return '';
            const tag = (el.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea') return tag;
            if (el.isContentEditable) return 'contenteditable';
            return '';
        }"""
        try:
            return self.page.evaluate(js) or ""
        except Exception:
            return ""

    def read_fields(self) -> list[dict]:
        """Read every visible input/textarea's label + current value from the DOM.

        This is *ground truth* — what the page actually contains — as opposed to
        the model's guess from a screenshot. It is how the agent confirms it really
        filled the form before declaring success.
        """
        self._require_page()
        js = """() => {
            const out = [];
            const docs = [document];
            document.querySelectorAll('iframe').forEach(f => {
                try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}
            });
            for (const doc of docs) {
                doc.querySelectorAll('input, textarea').forEach(el => {
                    if (el.type === 'hidden' || el.offsetParent === null) return;
                    let label = '';
                    if (el.id) {
                        const l = doc.querySelector('label[for="' + el.id + '"]');
                        if (l) label = l.innerText;
                    }
                    if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
                    if (!label) { const w = el.closest('label'); if (w) label = w.innerText; }
                    if (!label && el.placeholder) label = '(placeholder) ' + el.placeholder;
                    out.push({
                        label: (label || '').trim().slice(0, 60),
                        value: el.value || '',
                        tag: (el.tagName || '').toLowerCase(),
                    });
                });
            }
            return out;
        }"""
        try:
            return self.page.evaluate(js) or []
        except Exception as exc:
            self.log.warning("read_fields failed: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _require_page(self) -> None:
        if self.page is None:
            raise BrowserError("Browser is not open. Call open_browser() first.")

    def _check_bounds(self, x: int, y: int) -> None:
        w, h = self.config.viewport_width, self.config.viewport_height
        if not (0 <= x <= w and 0 <= y <= h):
            raise BrowserError(
                f"Coordinate ({x}, {y}) is outside the {w}x{h} viewport."
            )

    def _settle(self) -> None:
        """Brief pause so the page can react to the last action."""
        if self.page is not None:
            self.page.wait_for_timeout(self.config.action_pause_ms)
