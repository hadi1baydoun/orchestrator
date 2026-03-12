"""
Main orchestrator loop that controls the investigation workflow.

Implements the 11-step orchestration cycle as defined in the protocol.
"""
import asyncio
import logging
import hashlib
import time
from pathlib import Path
from typing import Optional
from datetime import datetime
from enum import Enum

from schema import Session, SessionStatus, Mode, StepHistory
from config import get_settings
from session_manager import load_session, save_session, create_session
from openai_client import get_planner_response, InvalidPlannerResponse
from tmux_bridge import send_prompt, capture_pane, check_pane_exists, TmuxPaneNotFound, TmuxCommandFailed
from claude_capture import wait_and_capture, CaptureTimeout, IncompleteCapture
from prompt_builder import build_claude_prompt, build_planner_input
from stop_detector import get_detector
from recovery import get_recovery_manager
from approval import get_approval_manager
from tmux_parser import extract_files_from_response, extract_summary_from_response, parse_tmux_output

logger = logging.getLogger(__name__)


class OrchestrationState(str, Enum):
    """Strict state machine for web orchestration flow."""
    IDLE = "IDLE"
    SENDING_TO_CHATGPT = "SENDING_TO_CHATGPT"
    WAITING_FOR_CHATGPT = "WAITING_FOR_CHATGPT"
    CHATGPT_DONE = "CHATGPT_DONE"
    SENDING_TO_CLAUDE = "SENDING_TO_CLAUDE"
    WAITING_FOR_CLAUDE = "WAITING_FOR_CLAUDE"
    CLAUDE_DONE = "CLAUDE_DONE"
    STOPPED = "STOPPED"


class Orchestrator:
    """
    Main orchestration loop controller.

    Manages the investigation loop between ChatGPT (planner) and Claude CLI (executor).
    """

    def __init__(self):
        """Initialize the orchestrator."""
        self.settings = get_settings()
        self.detector = get_detector()
        self.recovery = get_recovery_manager()
        self.approval = get_approval_manager()
        self._running = False
        self._paused = False
        self._is_processing = False
        self._state = OrchestrationState.IDLE

    def _set_state(self, state: OrchestrationState) -> None:
        """Transition orchestrator state with debug logging."""
        self._state = state
        logger.debug(f"State -> {state.value}")

    def _require_state(self, allowed_states: tuple[OrchestrationState, ...], action: str) -> None:
        """Ensure actions only run from valid states."""
        if self._state not in allowed_states:
            allowed = ", ".join(s.value for s in allowed_states)
            raise RuntimeError(
                f"Cannot {action} while state={self._state.value}. Allowed states: {allowed}"
            )

    def _write_pane_snapshot(
        self,
        debug_dir: Path,
        filename: str,
        pane_text: str,
        prompt_sent: str,
    ) -> None:
        """Save raw pane snapshot and extracted marker block for one capture point."""
        pane_path = debug_dir / filename
        marker_path = debug_dir / f"{pane_path.stem}_marker_block.txt"

        with open(pane_path, "w", encoding="utf-8") as f:
            f.write(pane_text or "")

        parsed = parse_tmux_output(pane_text or "", prompt_sent or "")
        extracted = parsed.extracted_response or ""
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(f"# has_start_marker={parsed.has_start_marker}\n")
            f.write(f"# has_end_marker={parsed.has_end_marker}\n")
            f.write(f"# is_complete={parsed.is_complete}\n")
            f.write(f"# extracted_len={len(extracted)}\n")
            f.write("#" + "=" * 70 + "\n")
            f.write(extracted)

    def _dump_send_cycle_snapshots(
        self,
        session: Session,
        step_number: int,
        prompt_sent: str,
        pane_before_send: str,
    ) -> None:
        """
        Save pane snapshots around send/capture to debug tmux submit behavior.

        Files created per step attempt:
        - pane_before_send.txt
        - pane_after_send.txt
        - pane_after_10s.txt
        - *_marker_block.txt for each snapshot
        """
        debug_dir = (
            self.settings.get_logs_path()
            / "tmux_send_debug"
            / session.session_id
            / f"step_{step_number:03d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        debug_dir.mkdir(parents=True, exist_ok=True)

        self._write_pane_snapshot(
            debug_dir=debug_dir,
            filename="pane_before_send.txt",
            pane_text=pane_before_send or "",
            prompt_sent="",
        )

        try:
            pane_after_send = capture_pane(session.tmux_target)
        except Exception as e:
            pane_after_send = f"[snapshot_capture_error] {type(e).__name__}: {e}"
        self._write_pane_snapshot(
            debug_dir=debug_dir,
            filename="pane_after_send.txt",
            pane_text=pane_after_send,
            prompt_sent=prompt_sent,
        )

        time.sleep(10)
        try:
            pane_after_10s = capture_pane(session.tmux_target)
        except Exception as e:
            pane_after_10s = f"[snapshot_capture_error] {type(e).__name__}: {e}"
        self._write_pane_snapshot(
            debug_dir=debug_dir,
            filename="pane_after_10s.txt",
            pane_text=pane_after_10s,
            prompt_sent=prompt_sent,
        )

        logger.info(
            "[TMUX SNAPSHOT DEBUG] session=%s step=%s dir=%s",
            session.session_id,
            step_number,
            str(debug_dir),
        )

    async def run_orchestration(
        self,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
        issue_description: Optional[str] = None,
        project: str = "default",
        tmux_target: Optional[str] = None,
        mode: str = "investigate"
    ) -> Session:
        """
        Run the orchestration loop.

        Either loads an existing session or creates a new one.

        Args:
            session_id: Existing session ID to resume (optional)
            title: Issue title (for new sessions)
            issue_description: Issue description (for new sessions)
            project: Project name
            tmux_target: Tmux target (uses default if None)
            mode: Execution mode

        Returns:
            Session object when orchestration stops
        """
        if self._is_processing:
            raise RuntimeError("Orchestrator is already processing another run")

        self._is_processing = True
        self._running = True
        self._paused = False
        self._set_state(OrchestrationState.IDLE)
        session = None

        try:
            # Load or create session
            if session_id:
                session = load_session(session_id)
                if not session:
                    raise ValueError(f"Session {session_id} not found")
            else:
                if not title or not issue_description:
                    raise ValueError("title and issue_description required for new sessions")

                from schema import Mode
                tmux_target = tmux_target or self.settings.default_tmux_target
                session = create_session(
                    title=title,
                    issue_description=issue_description,
                    project=project,
                    tmux_target=tmux_target,
                    mode=Mode(mode)
                )

            # Verify tmux target exists
            if not check_pane_exists(session.tmux_target):
                raise TmuxPaneNotFound(f"Tmux target '{session.tmux_target}' not found")

            logger.info(f"Starting orchestration for session {session.session_id}")
            await self._orchestration_loop(session)
            return session
        except Exception as e:
            logger.error(f"Orchestration error: {e}")
            if session is not None:
                session.status = SessionStatus.BLOCKED
                session.stall_reason = f"Unexpected error: {e}"
                save_session(session)
            raise
        finally:
            self._running = False
            self._is_processing = False
            self._set_state(OrchestrationState.STOPPED)

    async def _orchestration_loop(self, session: Session) -> None:
        """
        Main orchestration loop with 11-step workflow.

        The loop continues until a stop condition is met.
        """
        session.last_checkpoint = datetime.now()

        while self._running and not self._paused:
            if self._state == OrchestrationState.WAITING_FOR_CLAUDE:
                logger.debug("Guard: still waiting for Claude; skipping new cycle tick")
                await asyncio.sleep(1)
                continue

            # Step 1: Build complete planner input
            try:
                planner_input = build_planner_input(session)
                logger.info(f"Step {session.step_count + 1}: Getting planner response")
            except Exception as e:
                logger.error(f"Failed to build planner input: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            # Step 2: Get next step from ChatGPT
            try:
                planner_response = get_planner_response(planner_input)
            except InvalidPlannerResponse as e:
                logger.error(f"Invalid planner response: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue
            except Exception as e:
                logger.error(f"Failed to get planner response: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            # Reset recovery on successful planner response
            self.recovery.reset_recovery_attempts(session)

            # Step 3: Check approval requirements
            requires_approval, approval_type = self.approval.check_requires_approval(
                planner_response.prompt_for_claude,
                session
            )

            if requires_approval or planner_response.approval_requested:
                request_type = approval_type or planner_response.approval_requested or "general"
                self.approval.request_approval(
                    session,
                    request_type,
                    planner_response.why
                )
                self._display_approval_request(session, planner_response)
                break  # Pause for manual approval

            # Step 4: Check stop conditions
            should_stop, reason, new_status = self.detector.should_stop(
                planner_response,
                session
            )

            if should_stop:
                session.status = new_status or SessionStatus(planner_response.status)
                session.resolution_summary = planner_response.resolution_summary or reason
                save_session(session)
                self._display_stop_summary(session, reason)
                break

            # Step 5: Send prompt to Claude via tmux
            full_prompt = build_claude_prompt(
                planner_response.prompt_for_claude,
                session.title,
                session.mode
            )

            try:
                if self._state == OrchestrationState.WAITING_FOR_CLAUDE:
                    logger.debug("Guard: send to Claude blocked while WAITING_FOR_CLAUDE")
                    await asyncio.sleep(1)
                    continue
                self._set_state(OrchestrationState.SENDING_TO_CLAUDE)
                try:
                    pane_before_send = capture_pane(session.tmux_target)
                except Exception:
                    pane_before_send = ""
                send_prompt(session.tmux_target, full_prompt)
                try:
                    self._dump_send_cycle_snapshots(
                        session=session,
                        step_number=planner_response.step_number,
                        prompt_sent=full_prompt,
                        pane_before_send=pane_before_send,
                    )
                except Exception as e:
                    logger.warning(f"Failed to save tmux snapshot debug files: {e}")
                self._set_state(OrchestrationState.WAITING_FOR_CLAUDE)
            except (TmuxPaneNotFound, TmuxCommandFailed) as e:
                self._set_state(OrchestrationState.IDLE)
                logger.error(f"Tmux send failed: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            # Step 6: Wait for completion and capture response
            try:
                claude_output = wait_and_capture(
                    session.tmux_target,
                    timeout=self.settings.capture_timeout,
                    prompt_sent=full_prompt,
                    baseline_text=pane_before_send,
                )

                if claude_output is None:
                    raise CaptureTimeout("Claude response capture returned None")
                self._set_state(OrchestrationState.CLAUDE_DONE)

            except (CaptureTimeout, IncompleteCapture) as e:
                self._set_state(OrchestrationState.IDLE)
                logger.warning(f"Capture error: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            # Step 7: Extract files mentioned for no-progress tracking
            from tmux_parser import extract_files_from_response
            files_touched = extract_files_from_response(claude_output)

            # Step 8: Generate summary for next planner input
            from tmux_parser import extract_summary_from_response
            claude_summary = extract_summary_from_response(claude_output)

            # Step 9: Record step in history
            from schema import StepHistory
            step = StepHistory(
                step_number=planner_response.step_number,
                planner_status=planner_response.status,
                step_title=planner_response.step_title,
                prompt_for_claude=planner_response.prompt_for_claude,
                claude_output=claude_output,
                claude_summary=claude_summary,
                planner_interpretation=planner_response.why,
                timestamp=datetime.now(),
                files_touched=files_touched
            )

            session.history.append(step)
            session.step_count = len(session.history)
            session.last_checkpoint = datetime.now()
            save_session(session)

            # Step 10: Display progress
            self._display_step_summary(step, planner_response, session)

            # Step 11: Small delay before next iteration
            await asyncio.sleep(1)

    def _display_step_summary(
        self,
        step,
        planner_response,
        session: Session
    ) -> None:
        """Display a summary of the completed step."""
        print(f"\n{'=' * 60}")
        print(f"Step {step.step_number}: {step.step_title}")
        print(f"{'=' * 60}")

        if step.claude_summary:
            print(f"\nClaude Summary: {step.claude_summary}")
        elif step.claude_output:
            # Show first line or so
            first_line = step.claude_output.split('\n')[0] if step.claude_output else ""
            print(f"\nClaude Output: {first_line[:100]}...")

        if step.files_touched:
            print(f"Files: {', '.join(step.files_touched[:5])}")

        print(f"\nSteps Completed: {session.step_count}")
        print(f"Status: {session.status.value}")

    def _display_stop_summary(self, session: Session, reason: str) -> None:
        """Display the stop reason and summary."""
        print(f"\n{'=' * 60}")
        print("ORCHESTRATION STOPPED")
        print(f"{'=' * 60}")
        print(f"Reason: {reason}")
        print(f"Final Status: {session.status.value}")
        print(f"Steps Taken: {session.step_count}")

        if session.resolution_summary:
            print(f"\nResolution Summary:\n{session.resolution_summary}")

        if session.stall_reason:
            print(f"\nStall Reason: {session.stall_reason}")

        print(f"{'=' * 60}\n")

    def _display_approval_request(self, session: Session, planner_response) -> None:
        """Display approval request details."""
        self.approval.display_approval_request(session)

        print(f"\nPlanner Reason: {planner_response.why}")
        print(f"\nPlanned Action: {planner_response.step_title}")
        print(f"Run: python main.py approve --session {session.session_id}")
        print(f"Or:   python main.py deny --session {session.session_id} --reason 'your reason'")

    def pause(self) -> None:
        """Pause the orchestration loop."""
        self._paused = True
        logger.info("Orchestration paused")

    def resume(self) -> None:
        """Resume the orchestration loop."""
        self._paused = False
        if self._state == OrchestrationState.STOPPED:
            self._set_state(OrchestrationState.IDLE)
        logger.info("Orchestration resumed")

    def stop(self) -> None:
        """Stop the orchestration loop."""
        self._running = False
        self._paused = False
        self._set_state(OrchestrationState.STOPPED)
        logger.info("Orchestration stopped")

    def is_running(self) -> bool:
        """Check if orchestration is running."""
        return self._running

    def is_paused(self) -> bool:
        """Check if orchestration is paused."""
        return self._paused

    async def run_web_orchestration(
        self,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
        issue_description: Optional[str] = None,
        project: str = "default",
        tmux_target: Optional[str] = None,
        mode: str = "investigate",
        chatgpt_url: Optional[str] = None,
        headless: bool = False
    ) -> Session:
        """
        Run orchestration using ChatGPT web interface (browser automation).

        Args:
            session_id: Existing session ID to resume (optional)
            title: Issue title (for new sessions)
            issue_description: Issue description (for new sessions)
            project: Project name
            tmux_target: Tmux target (uses default if None)
            mode: Execution mode
            chatgpt_url: URL of existing ChatGPT conversation (to attach to)
            headless: Whether to run browser headless

        Returns:
            Session object when orchestration stops
        """
        from browser_bridge import get_browser_bridge, BrowserBridgeError

        if self._is_processing:
            raise RuntimeError("Orchestrator is already processing another run")

        self._is_processing = True
        self._running = True
        self._paused = False
        self._set_state(OrchestrationState.IDLE)
        session = None

        try:
            # Load or create session
            if session_id:
                session = load_session(session_id)
                if not session:
                    raise ValueError(f"Session {session_id} not found")
            else:
                if not title or not issue_description:
                    raise ValueError("title and issue_description required for new sessions")

                tmux_target = tmux_target or self.settings.default_tmux_target
                session = create_session(
                    title=title,
                    issue_description=issue_description,
                    project=project,
                    tmux_target=tmux_target,
                    mode=Mode(mode)
                )

            # Verify tmux target exists
            if not check_pane_exists(session.tmux_target):
                raise TmuxPaneNotFound(f"Tmux target '{session.tmux_target}' not found")

            # Start browser bridge
            logger.info("Starting browser bridge...")
            browser = get_browser_bridge()
            try:
                browser_url = browser.start(headless=headless, existing_url=chatgpt_url)
                logger.info(f"Browser started: {browser_url}")
            except Exception as e:
                logger.error(f"Failed to start browser: {e}")
                session.status = SessionStatus.BLOCKED
                session.stall_reason = f"Browser failed to start: {e}"
                save_session(session)
                raise

            logger.info(f"Starting WEB orchestration for session {session.session_id}")
            try:
                await self._web_orchestration_loop(session, browser)
            finally:
                browser.stop()

            return session
        except Exception as e:
            logger.error(f"Web orchestration error: {e}")
            if session is not None:
                session.status = SessionStatus.BLOCKED
                session.stall_reason = f"Unexpected error: {e}"
                save_session(session)
            raise
        finally:
            self._running = False
            self._is_processing = False
            self._set_state(OrchestrationState.STOPPED)

    async def _web_orchestration_loop(self, session: Session, browser) -> None:
        """
        Web mode orchestration loop.

        Uses browser automation to interact with ChatGPT web interface.
        """
        from chatgpt_parser import OrchestratorResponse, build_chatgpt_prompt
        from browser_bridge import BrowserBridgeError

        session.last_checkpoint = datetime.now()
        self._set_state(OrchestrationState.IDLE)

        # First message: send the initial issue description
        initial_prompt = build_chatgpt_prompt(
            issue_title=session.title,
            issue_description=session.issue_description,
            mode=session.mode.value,
            current_step=0,
            claude_output=None,
            history_summary=None
        )

        print(f"\n{'=' * 60}")
        print("Sending initial issue to ChatGPT...")
        print(f"{'=' * 60}\n")

        try:
            self._require_state((OrchestrationState.IDLE, OrchestrationState.CLAUDE_DONE), "send to ChatGPT")
            self._set_state(OrchestrationState.SENDING_TO_CHATGPT)
            self._set_state(OrchestrationState.WAITING_FOR_CHATGPT)
            planner_response = browser.send_and_receive(
                initial_prompt,
                timeout_ms=self.settings.browser_timeout_ms
            )
            self._set_state(OrchestrationState.CHATGPT_DONE)
            logger.info(
                "[TRACE][CHATGPT->ORCH][INITIAL] status=%s step=%s title=%s prompt_len=%s prompt_hash=%s",
                planner_response.status,
                planner_response.step_number,
                planner_response.step_title,
                len(planner_response.prompt_for_claude or ""),
                self._hash_text(planner_response.prompt_for_claude or ""),
            )
        except BrowserBridgeError as e:
            logger.error(f"Failed to get initial ChatGPT response: {e}")
            session.status = SessionStatus.BLOCKED
            session.stall_reason = f"ChatGPT web error: {e}"
            save_session(session)
            return

        # Convert OrchestratorResponse to a dict similar to PlannerResponse
        # for compatibility with existing methods
        class WebPlannerResponse:
            def __init__(self, resp: OrchestratorResponse):
                self.status = resp.status
                self.step_number = resp.step_number
                self.step_title = resp.step_title
                self.why = resp.why
                self.success_criteria = resp.success_criteria
                self.done_signal = resp.done_signal
                self.resolution_summary = resp.resolution_summary
                self.human_action = resp.human_action
                self.prompt_for_claude = resp.prompt_for_claude
                self.approval_requested = resp.approval_requested

        planner_response = WebPlannerResponse(planner_response)

        while self._running and not self._paused:
            if self._state == OrchestrationState.WAITING_FOR_CLAUDE:
                logger.debug("Guard: still waiting for Claude; skipping new cycle tick")
                await asyncio.sleep(1)
                continue

            # Check approval requirements
            requires_approval, approval_type = self.approval.check_requires_approval(
                planner_response.prompt_for_claude,
                session
            )

            if requires_approval or planner_response.approval_requested:
                request_type = approval_type or planner_response.approval_requested or "general"
                self.approval.request_approval(
                    session,
                    request_type,
                    planner_response.why
                )
                self._display_approval_request(session, planner_response)
                break  # Pause for manual approval

            # Check stop conditions
            should_stop, reason, new_status = self.detector.should_stop(
                planner_response,
                session
            )

            if should_stop:
                session.status = new_status or SessionStatus(planner_response.status)
                session.resolution_summary = planner_response.resolution_summary or reason
                save_session(session)
                self._display_stop_summary(session, reason)
                break

            # Build prompt for Claude
            full_prompt = build_claude_prompt(
                planner_response.prompt_for_claude,
                session.title,
                session.mode
            )

            # Send to Claude via tmux
            try:
                if self._state == OrchestrationState.WAITING_FOR_CLAUDE:
                    logger.debug("Guard: send to Claude blocked while WAITING_FOR_CLAUDE")
                    await asyncio.sleep(1)
                    continue
                self._require_state((OrchestrationState.CHATGPT_DONE,), "send to Claude")
                self._set_state(OrchestrationState.SENDING_TO_CLAUDE)
                logger.info(
                    "[TRACE][ORCH->CLAUDE] step=%s title=%s prompt_len=%s prompt_hash=%s",
                    planner_response.step_number,
                    planner_response.step_title,
                    len(full_prompt),
                    self._hash_text(full_prompt),
                )
                try:
                    pane_before_send = capture_pane(session.tmux_target)
                except Exception:
                    pane_before_send = ""
                send_prompt(session.tmux_target, full_prompt)
                try:
                    self._dump_send_cycle_snapshots(
                        session=session,
                        step_number=planner_response.step_number,
                        prompt_sent=full_prompt,
                        pane_before_send=pane_before_send,
                    )
                except Exception as e:
                    logger.warning(f"Failed to save tmux snapshot debug files: {e}")
                logger.info("TMUX_SEND_TRIGGERED=true")
                self._set_state(OrchestrationState.WAITING_FOR_CLAUDE)
            except (TmuxPaneNotFound, TmuxCommandFailed) as e:
                logger.info("TMUX_SEND_TRIGGERED=false")
                self._set_state(OrchestrationState.CHATGPT_DONE)
                logger.error(f"Tmux send failed: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            # Wait for Claude response
            try:
                claude_output = wait_and_capture(
                    session.tmux_target,
                    timeout=self.settings.capture_timeout,
                    prompt_sent=full_prompt,
                    baseline_text=pane_before_send,
                )

                if claude_output is None:
                    raise CaptureTimeout("Claude response capture returned None")
                self._set_state(OrchestrationState.CLAUDE_DONE)
                logger.info(
                    "[TRACE][CLAUDE->ORCH] output_len=%s output_hash=%s preview=%s",
                    len(claude_output or ""),
                    self._hash_text(claude_output or ""),
                    (claude_output or "").replace("\n", " ")[:160],
                )

            except (CaptureTimeout, IncompleteCapture) as e:
                self._set_state(OrchestrationState.CHATGPT_DONE)
                logger.warning(f"Capture error: {e}")
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            # Extract files and summary
            files_touched = extract_files_from_response(claude_output)
            claude_summary = extract_summary_from_response(claude_output)

            # Record step
            step = StepHistory(
                step_number=planner_response.step_number,
                planner_status=planner_response.status,
                step_title=planner_response.step_title,
                prompt_for_claude=planner_response.prompt_for_claude,
                claude_output=claude_output,
                claude_summary=claude_summary,
                planner_interpretation=planner_response.why,
                timestamp=datetime.now(),
                files_touched=files_touched
            )

            session.history.append(step)
            session.step_count = len(session.history)
            session.last_checkpoint = datetime.now()
            save_session(session)

            # Display progress
            self._display_step_summary(step, planner_response, session)

            # Build history summary for next ChatGPT prompt
            history_summary = self._build_history_summary(session)

            # Send Claude output to ChatGPT
            print(f"\n{'=' * 60}")
            print("Sending Claude output to ChatGPT...")
            print(f"{'=' * 60}\n")

            try:
                if self._state == OrchestrationState.WAITING_FOR_CLAUDE:
                    logger.debug("Guard: send to ChatGPT blocked while WAITING_FOR_CLAUDE")
                    await asyncio.sleep(1)
                    continue
                self._require_state((OrchestrationState.CLAUDE_DONE,), "send Claude output to ChatGPT")
                self._set_state(OrchestrationState.SENDING_TO_CHATGPT)
                self._set_state(OrchestrationState.WAITING_FOR_CHATGPT)
                logger.info(
                    "[TRACE][ORCH->CHATGPT] claude_output_len=%s claude_output_hash=%s",
                    len(claude_output or ""),
                    self._hash_text(claude_output or ""),
                )
                next_response = browser.send_claude_output(
                    claude_output=claude_output,
                    issue_title=session.title,
                    issue_description=session.issue_description,
                    mode=session.mode.value,
                    current_step=session.step_count + 1,
                    history_summary=history_summary
                )
                planner_response = WebPlannerResponse(next_response)
                self._set_state(OrchestrationState.CHATGPT_DONE)
                logger.info(
                    "[TRACE][CHATGPT->ORCH][FOLLOWUP] status=%s step=%s title=%s prompt_len=%s prompt_hash=%s",
                    planner_response.status,
                    planner_response.step_number,
                    planner_response.step_title,
                    len(planner_response.prompt_for_claude or ""),
                    self._hash_text(planner_response.prompt_for_claude or ""),
                )
            except BrowserBridgeError as e:
                logger.error(f"Failed to get ChatGPT response: {e}")
                if "Failed to parse response" in str(e):
                    session.status = SessionStatus.BLOCKED
                    session.stall_reason = str(e)
                    save_session(session)
                    break
                if not self.recovery.handle_crash(session, e):
                    break
                continue

            await asyncio.sleep(1)

    @staticmethod
    def _hash_text(text: str) -> str:
        """Stable short hash helper for trace logs."""
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]

    def _build_history_summary(self, session: Session) -> str:
        """Build a summary of session history for ChatGPT context."""
        if not session.history:
            return "No previous steps."

        lines = [f"## Previous Steps ({len(session.history)} steps):", ""]

        for step in session.history[-5:]:  # Last 5 steps
            lines.append(f"### Step {step.step_number}: {step.step_title}")
            if step.claude_summary:
                lines.append(f"**Finding**: {step.claude_summary}")
            if step.files_touched:
                lines.append(f"**Files**: {', '.join(step.files_touched[:3])}")
            lines.append("")

        return "\n".join(lines)


# Global orchestrator instance
_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    """Get the global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


async def run_orchestration(
    session_id: Optional[str] = None,
    title: Optional[str] = None,
    issue_description: Optional[str] = None,
    project: str = "default",
    tmux_target: Optional[str] = None,
    mode: str = "investigate"
) -> Session:
    """Convenience function to run orchestration."""
    orchestrator = get_orchestrator()
    return await orchestrator.run_orchestration(
        session_id=session_id,
        title=title,
        issue_description=issue_description,
        project=project,
        tmux_target=tmux_target,
        mode=mode
    )
