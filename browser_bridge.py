"""
Browser bridge wrapper for orchestrator integration.

Provides a synchronous interface to the async ChatGPT web client.
"""
import asyncio
import logging
import hashlib
from typing import Optional
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

from chatgpt_web import ChatGPTWebClient, get_client, ChatGPTWebError
from config import get_settings
from chatgpt_parser import (
    parse_and_validate,
    extract_orchestrator_block,
    OrchestratorResponse,
    build_chatgpt_prompt,
    detect_error_in_response,
)

logger = logging.getLogger(__name__)


class BrowserBridgeError(Exception):
    """Base exception for browser bridge errors."""
    pass


class BrowserNotStarted(BrowserBridgeError):
    """Raised when browser is not started."""
    pass


class BrowserBridge:
    """
    Synchronous wrapper for async ChatGPT web client.

    Runs the async client in a background thread and provides
    synchronous methods for the orchestrator.
    """

    def __init__(self):
        """Initialize the browser bridge."""
        self._client: Optional[ChatGPTWebClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser_bridge")
        self._running = False
        self._in_flight = False
        self._state = "IDLE"
        self._lock = Lock()

    def start(
        self,
        headless: bool = False,
        existing_url: Optional[str] = None
    ) -> str:
        """
        Start the browser.

        Args:
            headless: Whether to run headless
            existing_url: URL of existing ChatGPT conversation

        Returns:
            The current page URL
        """
        if self._running:
            logger.warning("Browser already started")
            return self._run_async(self._client.get_conversation_url())

        # Start event loop in background thread
        self._thread = Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

        # Wait for loop to be ready
        while self._loop is None:
            import time
            time.sleep(0.01)

        # Start the browser
        future = asyncio.run_coroutine_threadsafe(
            self._start_browser(headless, existing_url),
            self._loop
        )

        settings = get_settings()
        start_timeout_s = max(45, int(settings.browser_timeout_ms / 1000))
        url = future.result(timeout=start_timeout_s)

        self._running = True
        self._state = "IDLE"
        logger.info(f"Browser bridge started: {url}")

        return url

    def _run_event_loop(self):
        """Run the async event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _start_browser(
        self,
        headless: bool,
        existing_url: Optional[str]
    ) -> str:
        """Async helper to start browser."""
        self._client = ChatGPTWebClient()
        page = await self._client.start(headless=headless, existing_page_url=existing_url)
        return page.url

    def send_and_receive(
        self,
        prompt: str,
        timeout_ms: int = 120000
    ) -> OrchestratorResponse:
        """
        Send a prompt to ChatGPT and receive the parsed response.

        Args:
            prompt: Prompt to send
            timeout_ms: Timeout in milliseconds

        Returns:
            Parsed OrchestratorResponse

        Raises:
            BrowserNotStarted: If browser not started
            BrowserBridgeError: If send/receive fails
        """
        if not self._running or not self._client:
            raise BrowserNotStarted("Browser not started. Call start() first.")

        with self._lock:
            if self._in_flight:
                raise BrowserBridgeError(
                    f"Cannot send to ChatGPT while previous request is still in flight (state={self._state})"
                )
            self._in_flight = True
            self._state = "SENDING_TO_CHATGPT"

        logger.info(f"Sending prompt to ChatGPT via browser bridge")

        future = asyncio.run_coroutine_threadsafe(
            self._client.send_message(prompt, wait_for_response=True, timeout=timeout_ms),
            self._loop
        )

        try:
            self._state = "WAITING_FOR_CHATGPT"
            response_text = future.result(timeout=timeout_ms / 1000 + 10)
        except Exception as e:
            logger.error(f"Failed to get ChatGPT response: {e}")
            self._state = "IDLE"
            raise BrowserBridgeError(f"Failed to get response: {e}")
        finally:
            with self._lock:
                self._in_flight = False

        # Check for errors in response
        error = detect_error_in_response(response_text)
        if error:
            self._state = "IDLE"
            raise BrowserBridgeError(f"ChatGPT returned error: {error}")
        logger.info(
            "[TRACE][BROWSER RAW CHATGPT] response_len=%s response_hash=%s",
            len(response_text or ""),
            hashlib.sha256((response_text or "").encode("utf-8", errors="replace")).hexdigest()[:12],
        )
        logger.info("LATEST_ASSISTANT_MESSAGE_RAW=%s", response_text)

        # Parse the response
        try:
            orch_block = extract_orchestrator_block(response_text or "") or ""
            logger.info("EXTRACTED_ORCH_BLOCK=%s", orch_block)
            response = parse_and_validate(response_text)
            logger.info("EXTRACTED_PROMPT_FOR_CLAUDE=%s", response.prompt_for_claude)
            self._state = "CHATGPT_DONE"
            logger.info(f"Received parsed response: status={response.status}, step={response.step_title}")
            return response
        except Exception as e:
            logger.error(f"Failed to parse ChatGPT response: {e}")
            logger.debug(f"Response text: {response_text[:500]}")
            self._save_parse_failure(response_text)
            self._state = "IDLE"
            raise BrowserBridgeError(f"Failed to parse response: {e}")

    def send_claude_output(
        self,
        claude_output: str,
        issue_title: str,
        issue_description: str,
        mode: str,
        current_step: int,
        history_summary: Optional[str] = None
    ) -> OrchestratorResponse:
        """
        Send Claude's output to ChatGPT and get the next step.

        Builds the full prompt including context and sends to ChatGPT.

        Args:
            claude_output: Claude's response from tmux
            issue_title: Issue title
            issue_description: Issue description
            mode: Current mode
            current_step: Step number
            history_summary: Optional history summary

        Returns:
            Parsed OrchestratorResponse
        """
        prompt = build_chatgpt_prompt(
            issue_title=issue_title,
            issue_description=issue_description,
            mode=mode,
            current_step=current_step,
            claude_output=claude_output,
            history_summary=history_summary
        )

        return self.send_and_receive(prompt)

    def get_conversation_url(self) -> str:
        """Get the current conversation URL."""
        if not self._running or not self._client:
            raise BrowserNotStarted("Browser not started")

        return self._run_async(self._client.get_conversation_url())

    def take_screenshot(self, path: Optional[str] = None) -> bytes:
        """Take a screenshot of the current page."""
        if not self._running or not self._client:
            raise BrowserNotStarted("Browser not started")

        return self._run_async(self._client.take_screenshot(path))

    def _run_async(self, coro):
        """Run an async coroutine in the background loop."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)

    def stop(self) -> None:
        """Stop the browser bridge."""
        if not self._running:
            return

        logger.info("Stopping browser bridge")

        if self._client:
            future = asyncio.run_coroutine_threadsafe(
                self._client.stop(),
                self._loop
            )
            future.result(timeout=10)

        self._running = False
        self._state = "STOPPED"

    def close(self) -> None:
        """Fully close the browser bridge."""
        self.stop()

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        self._executor.shutdown(wait=True)

    def is_running(self) -> bool:
        """Check if browser bridge is running."""
        return self._running

    def _save_parse_failure(self, response_text: str) -> None:
        """Persist raw latest assistant message for parser debugging."""
        try:
            settings = get_settings()
            logs_dir = settings.get_logs_path()
            logs_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            debug_path = logs_dir / f"chatgpt_parse_fail_{ts}.txt"
            debug_path.write_text(response_text or "", encoding="utf-8")
            logger.error(f"Saved raw assistant message to {debug_path}")
        except Exception as save_err:
            logger.error(f"Failed to save parse failure debug file: {save_err}")


# Global bridge instance
_bridge: Optional[BrowserBridge] = None


def get_browser_bridge() -> BrowserBridge:
    """Get or create the global browser bridge."""
    global _bridge
    if _bridge is None:
        _bridge = BrowserBridge()
    return _bridge


def start_browser(
    headless: bool = False,
    existing_url: Optional[str] = None
) -> BrowserBridge:
    """
    Start the browser bridge and return it.

    Args:
        headless: Whether to run headless
        existing_url: URL of existing ChatGPT conversation

    Returns:
        Started BrowserBridge instance
    """
    bridge = get_browser_bridge()
    bridge.start(headless=headless, existing_url=existing_url)
    return bridge


def stop_browser() -> None:
    """Stop the browser bridge."""
    global _bridge
    if _bridge:
        _bridge.stop()
