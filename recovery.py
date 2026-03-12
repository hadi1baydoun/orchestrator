"""
Recovery manager for handling crashes, retries, and error recovery.

Implements explicit crash/retry/recovery behavior as defined in the protocol.
"""
import time
import logging
from typing import Optional, Type
from datetime import datetime

from schema import (
    Session,
    SessionStatus,
    RecoveryAction,
    PartialCapture,
)
from tmux_bridge import (
    TmuxPaneNotFound,
    TmuxCommandFailed,
    ClaudeNotResponding,
    check_pane_exists,
    list_sessions,
    verify_claude_running,
    send_prompt,
)
from tmux_parser import START_MARKER, END_MARKER
from claude_capture import (
    CaptureTimeout,
    IncompleteCapture,
    has_partial_capture,
    load_partial_capture,
    save_partial_capture,
)
from openai_client import InvalidPlannerResponse
from session_manager import save_session
from config import get_settings

logger = logging.getLogger(__name__)


class RecoveryManager:
    """
    Handles recovery from various failure scenarios.

    Implements the recovery behaviors specified in the protocol:
    - Tmux pane not found → Reattach or block
    - Claude not responding → Restart Claude or block
    - Partial capture → Save and pause
    - Invalid JSON → Retry with stronger schema or block
    - Network failure → Exponential backoff
    """

    def __init__(self):
        """Initialize the recovery manager."""
        self.settings = get_settings()

    def handle_crash(self, session: Session, error: Exception) -> bool:
        """
        Attempt to recover from a crash.

        Args:
            session: Current session
            error: The exception that occurred

        Returns:
            True if recovery succeeded (can retry), False if blocked
        """
        session.recovery_attempts += 1
        logger.warning(f"Recovery attempt {session.recovery_attempts} for error: {error}")

        if session.recovery_attempts > self.settings.max_recovery_attempts:
            session.status = SessionStatus.BLOCKED
            session.stall_reason = f"Max recovery attempts ({self.settings.max_recovery_attempts}) exceeded: {error}"
            save_session(session)
            logger.error(session.stall_reason)
            return False

        # Route to specific recovery handler
        if isinstance(error, TmuxPaneNotFound):
            return self._handle_pane_not_found(session)
        elif isinstance(error, ClaudeNotResponding):
            return self._handle_claude_not_responding(session)
        elif isinstance(error, (CaptureTimeout, IncompleteCapture)):
            return self._handle_capture_error(session, error)
        elif isinstance(error, InvalidPlannerResponse):
            return self._handle_invalid_planner_response(session)
        elif isinstance(error, (TmuxCommandFailed, ConnectionError, TimeoutError)):
            return self._handle_network_error(session, error)

        # Unknown error type
        session.status = SessionStatus.BLOCKED
        session.stall_reason = f"Unknown error type: {type(error).__name__}: {error}"
        save_session(session)
        return False

    def _handle_pane_not_found(self, session: Session) -> bool:
        """Recover from missing tmux pane."""
        # Verify tmux is running at all
        sessions = list_sessions()
        if not sessions:
            session.status = SessionStatus.BLOCKED
            session.stall_reason = "Tmux not running - start tmux first"
            save_session(session)
            logger.error(session.stall_reason)
            return False

        # Try to use default pane
        default_target = self.settings.default_tmux_target
        if check_pane_exists(default_target):
            logger.info(f"Reattaching to default pane: {default_target}")
            session.tmux_target = default_target
            save_session(session)
            return True

        # Try to find any pane with Claude running
        for sess_name in sessions:
            try:
                if check_pane_exists(f"{sess_name}:0.0"):
                    logger.info(f"Found alternative pane: {sess_name}:0.0")
                    session.tmux_target = f"{sess_name}:0.0"
                    save_session(session)
                    return True
            except:
                continue

        session.status = SessionStatus.BLOCKED
        session.stall_reason = f"No accessible tmux pane found. Tried: {session.tmux_target}, {default_target}"
        save_session(session)
        logger.error(session.stall_reason)
        return False

    def _handle_claude_not_responding(self, session: Session) -> bool:
        """Recover from Claude not responding."""
        if not verify_claude_running(session.tmux_target):
            logger.warning("Claude does not appear to be running in target pane")

            # Try to restart Claude
            if self._restart_claude_in_pane(session.tmux_target):
                logger.info("Claude restarted successfully")
                # Small delay for Claude to initialize
                time.sleep(2)
                return True

        session.status = SessionStatus.BLOCKED
        session.stall_reason = "Claude not responding and could not be restarted"
        save_session(session)
        logger.error(session.stall_reason)
        return False

    def _restart_claude_in_pane(self, target: str) -> bool:
        """
        Attempt to restart Claude CLI in the given pane.

        This is a simple attempt - complex restarts may need manual intervention.
        """
        try:
            # Send Ctrl+C to interrupt any current process
            send_prompt(target, "C-c")
            time.sleep(0.5)

            # Try starting Claude
            send_prompt(target, "claude")
            time.sleep(1)

            return verify_claude_running(target)
        except Exception as e:
            logger.warning(f"Failed to restart Claude: {e}")
            return False

    def _handle_capture_error(self, session: Session, error: Exception) -> bool:
        """Recover from capture timeout or incomplete capture."""
        if isinstance(error, IncompleteCapture):
            # Partial capture - save it and pause
            session.status = SessionStatus.PAUSED
            save_session(session)
            logger.warning(f"Incomplete capture, session paused. Manual review needed.")
            return False

        if isinstance(error, CaptureTimeout):
            # Timeout - may have partial output
            session.status = SessionStatus.PAUSED
            save_session(session)
            logger.warning(f"Capture timeout. Session paused for manual review.")
            return False

        return False

    def _handle_invalid_planner_response(self, session: Session) -> bool:
        """Recover from invalid JSON response from ChatGPT."""
        if session.recovery_attempts <= 2:
            # Retry with stronger schema (handled in openai_client)
            logger.info(f"Retrying with stronger schema (attempt {session.recovery_attempts})")
            time.sleep(self.settings.recovery_backoff_base ** session.recovery_attempts)
            return True

        session.status = SessionStatus.BLOCKED
        session.stall_reason = "ChatGPT returning invalid JSON repeatedly after multiple retries"
        save_session(session)
        logger.error(session.stall_reason)
        return False

    def _handle_network_error(self, session: Session, error: Exception) -> bool:
        """Recover from network/API errors with exponential backoff."""
        wait_time = self.settings.recovery_backoff_base ** session.recovery_attempts
        wait_time = min(wait_time, 60)  # Cap at 60 seconds

        logger.info(f"Network error, waiting {wait_time}s before retry: {error}")
        time.sleep(wait_time)
        return True

    def resume_after_crash(self, session_id: str) -> bool:
        """
        Resume a session that was interrupted.

        Args:
            session_id: Session ID to resume

        Returns:
            True if resume successful
        """
        from session_manager import load_session

        session = load_session(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return False

        # Verify tmux pane still exists
        if not check_pane_exists(session.tmux_target):
            logger.error(f"Target tmux pane {session.tmux_target} not found")
            print(f"Target tmux pane {session.tmux_target} not found. Please update session.")
            return False

        # Check for partial capture
        if has_partial_capture(session_id):
            print("Partial Claude output found. Please review and decide:")
            partial = load_partial_capture(session_id)
            if partial:
                print("\n" + "=" * 60)
                print("PARTIAL OUTPUT:")
                print("=" * 60)
                print(partial.partial_output[:1000])
                if len(partial.partial_output) > 1000:
                    print("...(truncated)")
                print("=" * 60)

                response = input("\nUse this partial output? (y/n/q to quit): ").lower().strip()
                if response == 'y':
                    # Mark the partial as accepted and continue
                    session.recovery_attempts = 0
                elif response == 'q':
                    return False
                else:
                    # Retry the step
                    session.recovery_attempts = 0

        # Restore session state
        session.status = SessionStatus.RUNNING
        session.updated_at = datetime.now()
        save_session(session)

        logger.info(f"Resumed session {session_id}")
        return True

    def create_checkpoint(self, session: Session) -> None:
        """
        Create a checkpoint for crash recovery.

        Checkpoints are created after each successful step.

        Args:
            session: Session to checkpoint
        """
        session.last_checkpoint = datetime.now()
        save_session(session)

    def get_recovery_status(self, session: Session) -> dict:
        """
        Get recovery status information for a session.

        Args:
            session: Session to check

        Returns:
            Dictionary with recovery status
        """
        return {
            "recovery_attempts": session.recovery_attempts,
            "max_recovery_attempts": self.settings.max_recovery_attempts,
            "last_checkpoint": session.last_checkpoint.isoformat() if session.last_checkpoint else None,
            "can_retry": session.recovery_attempts < self.settings.max_recovery_attempts,
        }

    def reset_recovery_attempts(self, session: Session) -> None:
        """
        Reset recovery attempt counter after successful step.

        Args:
            session: Session to update
        """
        session.recovery_attempts = 0
        save_session(session)


# Global recovery manager instance
_recovery_manager: Optional[RecoveryManager] = None


def get_recovery_manager() -> RecoveryManager:
    """Get the global recovery manager instance."""
    global _recovery_manager
    if _recovery_manager is None:
        _recovery_manager = RecoveryManager()
    return _recovery_manager


def handle_crash(session: Session, error: Exception) -> bool:
    """Convenience function to handle a crash."""
    return get_recovery_manager().handle_crash(session, error)


def resume_after_crash(session_id: str) -> bool:
    """Convenience function to resume after crash."""
    return get_recovery_manager().resume_after_crash(session_id)
