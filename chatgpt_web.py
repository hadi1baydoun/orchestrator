"""
ChatGPT web automation using Playwright.

Handles browser automation for interacting with ChatGPT through the web interface.
Attaches to an already running CDP-enabled Chrome session.
"""
import asyncio
import logging
import os
import hashlib
import urllib.request
import urllib.error
from typing import Optional, Literal
from pathlib import Path
from datetime import datetime

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from config import get_settings

logger = logging.getLogger(__name__)


# Response markers for parsing
ORCH_START_MARKER = "<<<ORCH_RESPONSE_START>>>"
ORCH_END_MARKER = "<<<ORCH_RESPONSE_END>>>"


class ChatGPTWebError(Exception):
    """Base exception for ChatGPT web errors."""
    pass


class BrowserNotRunning(ChatGPTWebError):
    """Raised when browser is not available."""
    pass


class PageNotLoaded(ChatGPTWebError):
    """Raised when ChatGPT page is not loaded."""
    pass


class ResponseTimeout(ChatGPTWebError):
    """Raised when ChatGPT response times out."""
    pass


class ChatGPTWebClient:
    """
    Async client for interacting with ChatGPT via Playwright.

    Reuses the user's already-open browser tab/session via CDP.
    """

    def __init__(self):
        """Initialize the ChatGPT web client."""
        self.settings = get_settings()
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._is_running = False
        self._attached_cdp = False
        self._last_processed_chatgpt_hash: Optional[str] = None

    @staticmethod
    def _urls_match(expected: str, actual: str) -> bool:
        """Return True when URLs point to the same page, ignoring query/hash differences."""
        expected_norm = (expected or "").rstrip("/")
        actual_norm = (actual or "").rstrip("/")
        if not expected_norm or not actual_norm:
            return False
        return (
            actual_norm == expected_norm
            or actual_norm.startswith(expected_norm + "?")
            or actual_norm.startswith(expected_norm + "#")
        )

    def _pick_existing_page(self, existing_page_url: Optional[str]) -> Page:
        """
        Select the best existing browser page without creating a new one.

        Preference:
        1) Exact existing_page_url match (if provided)
        2) Any open chatgpt.com tab
        3) First available tab
        """
        pages = []
        for ctx in self.browser.contexts:
            pages.extend(ctx.pages)

        if not pages:
            raise BrowserNotRunning(
                "Connected to Chrome via CDP but found no open tabs. "
                "Open a tab first, then rerun."
            )

        if existing_page_url:
            for p in pages:
                if self._urls_match(existing_page_url, p.url):
                    return p

        for p in pages:
            if "chatgpt.com" in (p.url or ""):
                return p

        return pages[0]

    @staticmethod
    def _discover_cdp_url() -> Optional[str]:
        """
        Detect an already-running Chrome/Chromium remote debugging endpoint.

        Returns:
            CDP base URL (e.g., http://127.0.0.1:9222) when reachable, else None.
        """
        candidates = [
            os.getenv("ORCHESTRATOR_CDP_URL", "").strip(),
            "http://127.0.0.1:9222",
            "http://localhost:9222",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            probe = candidate.rstrip("/") + "/json/version"
            try:
                with urllib.request.urlopen(probe, timeout=1.5) as resp:
                    if resp.status == 200:
                        return candidate
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
        return None

    async def start(self, headless: bool = False, existing_page_url: Optional[str] = None) -> Page:
        """
        Attach to an already running CDP-enabled browser and reuse an existing tab.

        Args:
            headless: Ignored when attaching to existing browser
            existing_page_url: URL of existing ChatGPT conversation to attach to

        Returns:
            The Playwright Page object
        """
        if self._is_running:
            logger.warning("Browser already running")
            return self.page

        logger.info("Starting browser attachment...")

        self.playwright = await async_playwright().start()

        cdp_url = self._discover_cdp_url()
        if not cdp_url:
            raise BrowserNotRunning(
                "No CDP browser found at http://127.0.0.1:9222 (or ORCHESTRATOR_CDP_URL). "
                "Start Chrome with --remote-debugging-port=9222, open ChatGPT, then rerun."
            )

        if headless:
            logger.warning("headless=True ignored when attaching to an existing browser")

        logger.info(f"Attaching to existing browser via CDP: {cdp_url}")
        self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
        self._attached_cdp = True
        self.page = self._pick_existing_page(existing_page_url)
        self.context = self.page.context

        self._is_running = True

        # Navigate to existing conversation or ChatGPT home
        if existing_page_url:
            if self._urls_match(existing_page_url, self.page.url):
                logger.info(f"Using existing conversation tab: {self.page.url}")
            else:
                logger.info(f"Reusing existing tab and navigating to conversation: {existing_page_url}")
                await self.page.goto(existing_page_url, wait_until="domcontentloaded", timeout=45000)
        else:
            if "chatgpt.com" in (self.page.url or ""):
                logger.info(f"Using existing ChatGPT tab: {self.page.url}")
            else:
                logger.info("Reusing existing tab and navigating to ChatGPT home")
                await self.page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=45000)

        # Wait for page to be ready, allowing time for Cloudflare/login transitions.
        await self._wait_for_page_ready(timeout=max(30000, self.settings.browser_timeout_ms))

        logger.info("Browser started and ready")
        return self.page

    async def attach_to_existing_page(self, url: str) -> Page:
        """
        Attach to an already open ChatGPT page.

        Useful when user manually opened the conversation.

        Args:
            url: URL of the existing ChatGPT conversation

        Returns:
            The Playwright Page object
        """
        if not self._is_running:
            await self.start(headless=False, existing_page_url=url)
        else:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await self._wait_for_page_ready()

        return self.page

    async def _wait_for_page_ready(self, timeout: int = 10000) -> None:
        """
        Wait for ChatGPT page to be ready for interaction.

        Args:
            timeout: Maximum time to wait in milliseconds
        """
        # ChatGPT may stay behind Cloudflare challenge for a short period.
        deadline = asyncio.get_event_loop().time() + (timeout / 1000.0)

        while asyncio.get_event_loop().time() < deadline:
            try:
                await self._find_input_element(timeout=1500)
                logger.debug("ChatGPT page is ready")
                return
            except PlaywrightTimeoutError:
                title = ""
                try:
                    title = await self.page.title()
                except Exception:
                    pass

                # Keep waiting while Cloudflare challenge page is shown.
                if "just a moment" in title.lower():
                    logger.info("Waiting for Cloudflare challenge to clear...")
                    await asyncio.sleep(1)
                    continue

                # If login page is shown, continue waiting briefly for redirects.
                if "log in" in title.lower() or "sign in" in title.lower():
                    logger.info("Waiting for ChatGPT login/session page transition...")
                    await asyncio.sleep(1)
                    continue

                await asyncio.sleep(0.5)

        current_title = ""
        current_url = ""
        try:
            current_title = await self.page.title()
            current_url = self.page.url
        except Exception:
            pass
        raise PageNotLoaded(
            f"ChatGPT page did not load properly (title='{current_title}', url='{current_url}')"
        )

    async def _find_input_element(self, timeout: int = 10000):
        """
        Find a visible composer input element.

        Args:
            timeout: Maximum time to wait in milliseconds

        Returns:
            A visible Playwright Locator for the input element
        """
        selectors = [
            "#prompt-textarea",
            "textarea[name='prompt-textarea']",
            "textarea.wcDTda_fallbackTextarea",
            "[contenteditable='true'][data-virtualkeyboard='true']",
            "[contenteditable='true'][role='textbox']",
        ]

        deadline = asyncio.get_event_loop().time() + (timeout / 1000.0)
        while asyncio.get_event_loop().time() < deadline:
            for selector in selectors:
                locator = self.page.locator(selector)
                count = await locator.count()
                for i in range(count):
                    candidate = locator.nth(i)
                    try:
                        if await candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
            await asyncio.sleep(0.2)

        raise PlaywrightTimeoutError("Could not find a visible ChatGPT composer input")

    async def send_message(
        self,
        message: str,
        wait_for_response: bool = True,
        timeout: int = 120000
    ) -> str:
        """
        Send a message to ChatGPT and optionally wait for response.

        Args:
            message: Message to send
            wait_for_response: Whether to wait for ChatGPT's response
            timeout: Maximum time to wait for response in milliseconds

        Returns:
            ChatGPT's response text if wait_for_response is True
        """
        if not self._is_running or not self.page:
            raise BrowserNotRunning("Browser not started. Call start() first.")

        logger.info(f"Sending message to ChatGPT ({len(message)} chars)")
        baseline_text = await self._get_latest_assistant_message()
        baseline_hash = self._hash_text(baseline_text) if baseline_text else None
        logger.info(
            "[TRACE][CHATGPT SEND] prompt_len=%s baseline_hash=%s",
            len(message or ""),
            (baseline_hash[:12] if baseline_hash else "none"),
        )

        # Find a visible input/composer element.
        try:
            textarea = await self._find_input_element(timeout=10000)
        except PlaywrightTimeoutError:
            raise PageNotLoaded("Could not find ChatGPT input textarea")

        # Clear and insert the full message in one shot (no line-by-line typing).
        await textarea.click()
        await self.page.keyboard.press("Control+A")
        try:
            tag_name = await textarea.evaluate("el => (el.tagName || '').toLowerCase()")
        except Exception:
            tag_name = ""

        if tag_name == "textarea":
            await textarea.fill(message)
        else:
            await self.page.keyboard.insert_text(message)

        # Send strategy:
        # 1) Click send button if visible/enabled
        # 2) Enter key
        # 3) Ctrl+Enter (handles custom "Enter to send" setting)
        if not await self._trigger_send():
            raise PageNotLoaded("Could not trigger ChatGPT send action")

        if wait_for_response:
            response = await self.wait_for_response(timeout, baseline_hash=baseline_hash)
            return response

        return ""

    async def _trigger_send(self) -> bool:
        """
        Trigger ChatGPT send and confirm it started.

        Returns:
            True when UI indicates message was submitted.
        """
        send_selectors = [
            "button[data-testid='send-button']",
            "button[data-testid='composer-send-button']",
            "button[aria-label='Send prompt']",
            "button[aria-label='Send message']",
            "button[aria-label='Send']",
            "button[aria-label*='Send']",
        ]

        for selector in send_selectors:
            try:
                button = self.page.locator(selector).first
                if await button.count() == 0 or not await button.is_visible():
                    continue

                disabled = await button.get_attribute("disabled")
                aria_disabled = await button.get_attribute("aria-disabled")
                if disabled is not None or aria_disabled == "true":
                    continue

                box = await button.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2
                    await self.page.mouse.move(x, y, steps=8)
                    await self.page.mouse.click(x, y)
                else:
                    await button.click()

                if await self._wait_for_send_ack():
                    logger.debug(f"Message sent via mouse click ({selector})")
                    return True
            except Exception:
                continue

        # Keyboard fallbacks
        for key_combo in ("Enter", "Control+Enter"):
            try:
                await self.page.keyboard.press(key_combo)
                if await self._wait_for_send_ack():
                    logger.debug(f"Message sent via keyboard fallback ({key_combo})")
                    return True
            except Exception:
                continue

        return False

    async def _wait_for_send_ack(self, timeout_ms: int = 4000) -> bool:
        """
        Wait until UI reflects that a message was submitted.

        Success signals:
        - Stop-generation button appears (generation started), or
        - Composer input is cleared.
        """
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
        while asyncio.get_event_loop().time() < deadline:
            try:
                if await self._has_visible_stop_button():
                    return True
                if not await self._composer_has_text():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def _composer_has_text(self) -> bool:
        """Return True if the composer currently contains non-whitespace text."""
        try:
            composer = await self._find_input_element(timeout=1200)
        except Exception:
            return False

        try:
            tag_name = await composer.evaluate("el => (el.tagName || '').toLowerCase()")
        except Exception:
            tag_name = ""

        try:
            if tag_name == "textarea":
                text = (await composer.input_value()).strip()
            else:
                text = (await composer.inner_text()).strip()
            return len(text) > 0
        except Exception:
            return False

    async def _has_visible_stop_button(self) -> bool:
        """Return True when ChatGPT generation controls are visible."""
        stop_selectors = [
            "button[data-testid='stop-button']",
            "button[aria-label*='Stop']",
            "button:has-text('Stop generating')",
        ]
        for selector in stop_selectors:
            try:
                stop_btn = self.page.locator(selector).first
                if await stop_btn.count() > 0 and await stop_btn.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def wait_for_response(
        self,
        timeout: int = 120000,
        check_interval: float = 0.75,
        baseline_hash: Optional[str] = None
    ) -> str:
        """
        Wait for ChatGPT to complete its response.

        Uses 3-layer detection:
        1. Response has stopped growing
        2. Input is usable again
        3. Response markers are present

        Args:
            timeout: Maximum time to wait in milliseconds
            check_interval: Time between checks in seconds

        Returns:
            The complete response text
        """
        logger.info(f"Waiting for ChatGPT response (timeout={timeout}ms)")

        start_time = datetime.now()
        last_response_text = ""
        stable_count = 0
        required_stable_count = 4

        while (datetime.now() - start_time).total_seconds() * 1000 < timeout:
            try:
                # Get current response
                response = await self._get_latest_assistant_message()

                if not response:
                    await asyncio.sleep(check_interval)
                    continue

                response_hash = self._hash_text(response)

                # Ignore old message from before this send.
                if baseline_hash and response_hash == baseline_hash:
                    await asyncio.sleep(check_interval)
                    continue

                # Ignore already processed message to prevent reprocessing loops.
                if self._last_processed_chatgpt_hash and response_hash == self._last_processed_chatgpt_hash:
                    await asyncio.sleep(check_interval)
                    continue

                # Check 1: Latest assistant message has stopped changing.
                if response == last_response_text:
                    stable_count += 1
                else:
                    stable_count = 0
                    last_response_text = response

                # Check 2: UI is idle again.
                ui_idle = await self._is_chatgpt_idle()

                # Check 3: Has a full marker block (not partial stream).
                has_markers = (
                    ORCH_START_MARKER in response and
                    ORCH_END_MARKER in response
                )

                # If all conditions met, we're done
                if stable_count >= required_stable_count and ui_idle and has_markers:
                    self._last_processed_chatgpt_hash = response_hash
                    await self._click_latest_copy_button()
                    logger.info(
                        "[TRACE][CHATGPT CAPTURE DONE] message_hash=%s message_len=%s stable_count=%s",
                        response_hash[:12],
                        len(response),
                        stable_count,
                    )
                    logger.info("ChatGPT response complete (all checks passed)")
                    return response

                # If stable but missing markers, wait a bit more
                if stable_count >= required_stable_count and not has_markers:
                    logger.debug("Response stable but missing markers, waiting...")

                await asyncio.sleep(check_interval)

            except Exception as e:
                logger.debug(f"Error while waiting for response: {e}")
                await asyncio.sleep(check_interval)

        # Timeout reached: do not return partial output.
        raise ResponseTimeout(f"ChatGPT response timed out after {timeout}ms")

    async def _click_latest_copy_button(self) -> None:
        """
        Click the latest assistant message Copy button with mouse movement.

        Best-effort only; failures should not block orchestration.
        """
        selectors = [
            "button[data-testid='copy-turn-action-button']",
            "button[aria-label='Copy']",
            "button:has-text('Copy')",
        ]
        for selector in selectors:
            try:
                buttons = self.page.locator(selector)
                count = await buttons.count()
                if count == 0:
                    continue

                button = buttons.nth(count - 1)
                if not await button.is_visible():
                    continue

                box = await button.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2
                    await self.page.mouse.move(x, y, steps=10)
                    await self.page.mouse.click(x, y)
                else:
                    await button.click()
                logger.debug(f"Clicked latest copy button via {selector}")
                return
            except Exception:
                continue

    async def _get_latest_assistant_message(self) -> str:
        """
        Get the latest assistant message from the conversation.

        Returns:
            The text of the latest assistant response
        """
        try:
            selectors = [
                "[data-message-author-role='assistant'] [data-message-content]",
                "[data-message-author-role='assistant']",
            ]

            for selector in selectors:
                try:
                    locator = self.page.locator(selector)
                    count = await locator.count()
                    if count == 0:
                        continue

                    # Walk backwards to find the latest non-empty assistant message.
                    for i in range(count - 1, -1, -1):
                        text = (await locator.nth(i).inner_text()).strip()
                        if text:
                            return text
                except Exception:
                    continue

            return ""

        except Exception as e:
            logger.debug(f"Error getting assistant message: {e}")
            return ""

    async def _is_input_ready(self) -> bool:
        """
        Check if the input is ready for new message (not disabled).

        Returns:
            True if input is ready
        """
        try:
            textarea = await self._find_input_element(timeout=2000)
            disabled = await textarea.get_attribute("disabled")
            aria_disabled = await textarea.get_attribute("aria-disabled")
            return disabled is None and aria_disabled != "true"
        except Exception:
            pass
        return False

    async def _is_chatgpt_idle(self) -> bool:
        """
        Check whether ChatGPT UI is idle after generation.

        Idle = input is ready and stop-generation controls are not visible.
        """
        input_ready = await self._is_input_ready()
        if not input_ready:
            return False

        return not await self._has_visible_stop_button()

    @staticmethod
    def _hash_text(text: str) -> str:
        """Generate a stable hash for message identity tracking."""
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    async def get_conversation_url(self) -> str:
        """
        Get the URL of the current conversation.

        Returns:
            Current page URL
        """
        if not self.page:
            raise BrowserNotRunning("Browser not started")
        return self.page.url

    async def take_screenshot(self, path: Optional[str] = None) -> bytes:
        """
        Take a screenshot of the current page.

        Args:
            path: Optional path to save screenshot

        Returns:
            Screenshot bytes
        """
        if not self.page:
            raise BrowserNotRunning("Browser not started")

        screenshot = await self.page.screenshot(full_page=False)

        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'wb') as f:
                f.write(screenshot)
            logger.info(f"Screenshot saved to {path}")

        return screenshot

    async def stop(self) -> None:
        """
        Stop the browser and cleanup resources.

        Note: Keeps context for persistent login across runs.
        """
        logger.info("Stopping browser...")

        if self.page and not self._attached_cdp:
            try:
                await self.page.close()
            except:
                pass
            self.page = None

        # Don't close context to preserve login
        # if self.context:
        #     await self.context.close()

        self._is_running = False
        logger.info("Browser stopped")

    async def close(self) -> None:
        """
        Fully close browser and context (logs out).
        """
        await self.stop()

        if self.context and not self._attached_cdp:
            try:
                await self.context.close()
            except:
                pass
            self.context = None

        if self.browser and self._attached_cdp:
            try:
                await self.browser.close()
            except:
                pass
            self.browser = None

        if self.playwright:
            try:
                await self.playwright.stop()
            except:
                pass
            self.playwright = None

    def is_running(self) -> bool:
        """Check if browser is currently running."""
        return self._is_running


# Global client instance
_client: Optional[ChatGPTWebClient] = None


async def get_client() -> ChatGPTWebClient:
    """Get or create the global ChatGPT web client."""
    global _client
    if _client is None:
        _client = ChatGPTWebClient()
    return _client
