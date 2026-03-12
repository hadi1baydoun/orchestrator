"""
Approval workflow manager for human-in-the-loop decision making.

Handles requesting, granting, and denying approvals for risky actions.
"""
import logging
import re
from typing import Optional, List
from datetime import datetime

from schema import (
    Session,
    SessionStatus,
    ApprovalState,
    ApprovalStatus,
    ApprovalRequestType,
)
from session_manager import save_session, load_session
from config import get_settings

logger = logging.getLogger(__name__)


# Risky patterns that require approval
RISKY_PATTERNS = {
    # Keep these specific to avoid false positives from words like "where"/"otherwise".
    "code_write": ["write file", "edit", "modify", "create file", "save file", "update file", "apply patch"],
    "migration": ["migration", "migrate", "schema change", "database migration"],
    "restart": ["restart", "reload", "bounce", "restart service"],
    "db_update": ["update db", "database update", "execute sql", "run migration", "alter table"],
    "secret_change": ["secret", "api key", "password", "credential", "token"],
}


class ApprovalManager:
    """
    Manages the human approval workflow.

    Handles:
    - Requesting approval for risky actions
    - Granting approval
    - Denying approval
    - Checking if approval is required
    """

    def __init__(self):
        """Initialize the approval manager."""
        self.settings = get_settings()

    def request_approval(
        self,
        session: Session,
        request_type: str,
        reason: str,
        commands: Optional[List[str]] = None
    ) -> None:
        """
        Request human approval for an action.

        Args:
            session: Session to request approval for
            request_type: Type of approval needed (e.g., "code_write", "migration")
            reason: Why approval is needed
            commands: Optional list of commands that would be executed
        """
        # Validate request type
        try:
            approval_type = ApprovalRequestType(request_type)
        except ValueError:
            approval_type = None

        session.approval = ApprovalState(
            status=ApprovalStatus.REQUESTED,
            request_type=approval_type,
            reason=reason,
            approved_commands=commands or [],
            requested_at=datetime.now()
        )
        session.status = SessionStatus.AWAITING_APPROVAL

        save_session(session)
        logger.info(f"Approval requested for {session.session_id}: {request_type} - {reason}")

    def grant_approval(
        self,
        session_id: str,
        commands: Optional[List[str]] = None,
        switch_mode: bool = True
    ) -> bool:
        """
        Grant approval for a pending action.

        Args:
            session_id: Session ID
            commands: Optional list of approved commands (can narrow scope)
            switch_mode: Whether to switch to approved_execute mode

        Returns:
            True if approval granted successfully
        """
        session = load_session(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return False

        if session.approval.status != ApprovalStatus.REQUESTED:
            logger.warning(f"Session {session_id} does not have a pending approval request")
            return False

        session.approval.status = ApprovalStatus.GRANTED
        session.approval.decided_at = datetime.now()

        if commands:
            session.approval.approved_commands = commands

        # Switch to approved_execute mode if requested
        if switch_mode:
            from schema import Mode
            session.mode = Mode.APPROVED_EXECUTE

        session.status = SessionStatus.RUNNING

        save_session(session)
        logger.info(f"Approval granted for {session_id}")

        return True

    def deny_approval(self, session_id: str, reason: Optional[str] = None) -> bool:
        """
        Deny approval for a pending action.

        Args:
            session_id: Session ID
            reason: Why approval was denied

        Returns:
            True if denial recorded successfully
        """
        session = load_session(session_id)
        if not session:
            logger.error(f"Session {session_id} not found")
            return False

        if session.approval.status != ApprovalStatus.REQUESTED:
            logger.warning(f"Session {session_id} does not have a pending approval request")
            return False

        session.approval.status = ApprovalStatus.DENIED
        session.approval.denied_reason = reason or "Approval denied by operator"
        session.approval.decided_at = datetime.now()
        session.status = SessionStatus.BLOCKED
        session.stall_reason = f"Approval denied: {reason or 'No reason provided'}"

        save_session(session)
        logger.info(f"Approval denied for {session_id}: {reason}")

        return True

    def check_requires_approval(
        self,
        planner_prompt: str,
        session: Session
    ) -> tuple[bool, Optional[str]]:
        """
        Check if the planned action requires approval.

        Checks both:
        1. Explicit approval_requested from planner
        2. Risky patterns in the prompt based on current mode

        Args:
            planner_prompt: The prompt planned for Claude
            session: Current session

        Returns:
            Tuple of (requires_approval, reason_or_type)
        """
        # Check if mode itself requires approval for certain actions
        from schema import Mode

        # Investigate mode is strictly read-only by design; don't block the round-trip
        # with approval popups triggered by wording matches.
        if session.mode == Mode.INVESTIGATE:
            return False, None

        prompt_lower = planner_prompt.lower()

        # Check each risky pattern category
        for action_type, patterns in RISKY_PATTERNS.items():
            # Skip if this action type doesn't require approval
            if action_type not in self.settings.require_approval_for:
                continue

            # For investigate mode, be extra strict
            if session.mode == Mode.INVESTIGATE and action_type in ["code_write", "migration", "db_update"]:
                for pattern in patterns:
                    if self._pattern_matches(prompt_lower, pattern) and not self._is_negated(prompt_lower, pattern):
                        return True, f"{action_type}: Investigate mode is read-only"

            # For propose_fix mode, allow suggestions but not execution
            if session.mode == Mode.PROPOSE_FIX and action_type == "code_write":
                for pattern in ["execute", "apply", "run"]:
                    if pattern in prompt_lower:
                        return True, f"{action_type}: Propose fix mode should suggest, not execute"

            # For any mode, check for secret changes
            if action_type == "secret_change":
                for pattern in patterns:
                    if self._pattern_matches(prompt_lower, pattern) and not self._is_negated(prompt_lower, pattern):
                        return True, f"{action_type}: Secret changes always require approval"

            # General check for other risky patterns
            if session.mode in [Mode.VALIDATE_FIX, Mode.APPROVED_EXECUTE]:
                for pattern in patterns:
                    if self._pattern_matches(prompt_lower, pattern) and not self._is_negated(prompt_lower, pattern):
                        # In approved_execute, check if it's in the approved commands
                        if session.mode == Mode.APPROVED_EXECUTE:
                            if action_type in [c.split()[0] for c in session.approval.approved_commands]:
                                continue
                        return True, action_type

        return False, None

    @staticmethod
    def _pattern_matches(prompt_lower: str, pattern: str) -> bool:
        """
        Match risky patterns safely.

        - Multi-word patterns use substring match.
        - Single-word patterns use word boundaries to avoid false positives
          (e.g., "edit" matching inside "credited").
        """
        pattern = pattern.lower().strip()
        if " " in pattern:
            return pattern in prompt_lower
        return re.search(rf"\b{re.escape(pattern)}\b", prompt_lower) is not None

    @staticmethod
    def _is_negated(prompt_lower: str, pattern: str) -> bool:
        """
        Detect common negated forms, e.g. "do not modify", "don't edit", "without write file".
        """
        pattern = pattern.lower().strip()
        negation_prefixes = ["do not ", "don't ", "dont ", "never ", "without ", "no "]
        for prefix in negation_prefixes:
            if f"{prefix}{pattern}" in prompt_lower:
                return True
        # Common global read-only intent should suppress code_write flags.
        if "read-only" in prompt_lower and pattern in {"write file", "edit", "modify", "update file", "apply patch"}:
            return True
        return False

    def get_approval_request_info(self, session: Session) -> Optional[dict]:
        """
        Get information about a pending approval request.

        Args:
            session: Session with potential approval request

        Returns:
            Dictionary with approval info, or None if no pending request
        """
        if not session.approval or session.approval.status != ApprovalStatus.REQUESTED:
            return None

        return {
            "request_type": session.approval.request_type.value if session.approval.request_type else "general",
            "reason": session.approval.reason,
            "requested_at": session.approval.requested_at.isoformat() if session.approval.requested_at else None,
            "approved_commands": session.approval.approved_commands,
        }

    def display_approval_request(self, session: Session) -> None:
        """
        Display an approval request to the user.

        Args:
            session: Session with approval request
        """
        info = self.get_approval_request_info(session)
        if not info:
            return

        print("\n" + "=" * 60)
        print("APPROVAL REQUIRED")
        print("=" * 60)
        print(f"Session: {session.session_id}")
        print(f"Type: {info['request_type']}")
        print(f"Reason: {info['reason']}")

        if info['approved_commands']:
            print("Commands to execute:")
            for cmd in info['approved_commands']:
                print(f"  - {cmd}")

        print(f"\nRequested at: {info['requested_at']}")
        print("=" * 60)
        print("\nTo approve: python main.py approve")
        print("To deny:    python main.py deny")
        print("")

    def reset_approval(self, session: Session) -> None:
        """
        Reset approval state (after completion or when no longer needed).

        Args:
            session: Session to reset
        """
        session.approval = ApprovalState()
        save_session(session)

    def is_approval_needed(self, session: Session) -> bool:
        """
        Check if the session is currently waiting for approval.

        Args:
            session: Session to check

        Returns:
            True if awaiting approval
        """
        return (
            session.status == SessionStatus.AWAITING_APPROVAL and
            session.approval.status == ApprovalStatus.REQUESTED
        )

    def get_approved_commands(self, session: Session) -> List[str]:
        """
        Get the list of approved commands for a session.

        Args:
            session: Session to check

        Returns:
            List of approved command strings
        """
        if session.approval and session.approval.status == ApprovalStatus.GRANTED:
            return session.approval.approved_commands
        return []


# Global approval manager instance
_approval_manager: Optional[ApprovalManager] = None


def get_approval_manager() -> ApprovalManager:
    """Get the global approval manager instance."""
    global _approval_manager
    if _approval_manager is None:
        _approval_manager = ApprovalManager()
    return _approval_manager


def request_approval(session: Session, request_type: str, reason: str, commands: Optional[List[str]] = None) -> None:
    """Convenience function to request approval."""
    get_approval_manager().request_approval(session, request_type, reason, commands)


def grant_approval(session_id: str, commands: Optional[List[str]] = None) -> bool:
    """Convenience function to grant approval."""
    return get_approval_manager().grant_approval(session_id, commands)


def deny_approval(session_id: str, reason: Optional[str] = None) -> bool:
    """Convenience function to deny approval."""
    return get_approval_manager().deny_approval(session_id, reason)
