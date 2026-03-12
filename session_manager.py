"""
Session manager for CRUD operations on orchestration sessions.

Handles creating, saving, loading, and updating sessions.
"""
import json
import logging
from pathlib import Path
from typing import Optional, List
from datetime import datetime
from uuid import uuid4

from schema import Session, StepHistory, SessionStatus, Mode
from config import get_settings

logger = logging.getLogger(__name__)


def _generate_session_id() -> str:
    """Generate a unique session ID."""
    now = datetime.now()
    # Use a counter for uniqueness within the day
    return f"issue_{now.strftime('%Y_%m_%d')}_{uuid4().hex[:6]}"


def create_session(
    title: str,
    issue_description: str,
    project: str,
    tmux_target: str,
    mode: Mode = Mode.INVESTIGATE,
    issue_seed_prompt: Optional[str] = None,
    operator_constraints: Optional[str] = None,
    project_context: Optional[str] = None,
    max_steps: int = 30
) -> Session:
    """
    Create a new orchestration session.

    Args:
        title: Issue title
        issue_description: Full problem description
        project: Project name/identifier
        tmux_target: Tmux target (format: "session:window.pane")
        mode: Execution mode
        issue_seed_prompt: Original prompt from operator
        operator_constraints: Operator-specified constraints
        project_context: System/project context
        max_steps: Maximum steps before auto-stop

    Returns:
        Created Session object
    """
    settings = get_settings()

    session = Session(
        session_id=_generate_session_id(),
        title=title,
        issue_description=issue_description,
        issue_seed_prompt=issue_seed_prompt or title,
        operator_constraints=operator_constraints,
        project_context=project_context,
        status=SessionStatus.RUNNING,
        mode=mode,
        project=project,
        tmux_target=tmux_target,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        step_count=0,
        max_steps=max_steps,
        last_checkpoint=datetime.now()
    )

    save_session(session)
    logger.info(f"Created session {session.session_id}: {title}")

    return session


def save_session(session: Session) -> None:
    """
    Save a session to disk.

    Args:
        session: Session to save
    """
    settings = get_settings()
    sessions_dir = settings.get_sessions_path()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    filepath = sessions_dir / f"{session.session_id}.json"

    # Update the updated_at timestamp
    session.updated_at = datetime.now()

    # Convert to dict for JSON serialization
    session_dict = session.model_dump(mode='json')

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(session_dict, f, indent=2, default=str)

    logger.debug(f"Saved session to {filepath}")


def load_session(session_id: str) -> Optional[Session]:
    """
    Load a session from disk.

    Args:
        session_id: Session ID to load

    Returns:
        Session object, or None if not found
    """
    settings = get_settings()
    sessions_dir = settings.get_sessions_path()
    filepath = sessions_dir / f"{session_id}.json"

    if not filepath.exists():
        logger.warning(f"Session file not found: {filepath}")
        return None

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            session_dict = json.load(f)

        session = Session(**session_dict)
        logger.debug(f"Loaded session {session_id}")
        return session

    except Exception as e:
        logger.error(f"Failed to load session {session_id}: {e}")
        return None


def list_sessions(status: Optional[SessionStatus] = None) -> List[Session]:
    """
    List all sessions, optionally filtered by status.

    Args:
        status: Optional status filter

    Returns:
        List of Session objects
    """
    settings = get_settings()
    sessions_dir = settings.get_sessions_path()

    if not sessions_dir.exists():
        return []

    sessions = []

    for filepath in sessions_dir.glob("issue_*.json"):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                session_dict = json.load(f)

            session = Session(**session_dict)

            # Filter by status if specified
            if status is None or session.status == status:
                sessions.append(session)

        except Exception as e:
            logger.warning(f"Failed to load session from {filepath}: {e}")

    # Sort by created_at, newest first
    sessions.sort(key=lambda s: s.created_at, reverse=True)

    return sessions


def delete_session(session_id: str) -> bool:
    """
    Delete a session from disk.

    Args:
        session_id: Session ID to delete

    Returns:
        True if deleted, False if not found
    """
    settings = get_settings()
    sessions_dir = settings.get_sessions_path()
    filepath = sessions_dir / f"{session_id}.json"

    if filepath.exists():
        filepath.unlink()
        logger.info(f"Deleted session {session_id}")
        return True

    return False


def append_step(session: Session, step: StepHistory) -> Session:
    """
    Append a step to the session history.

    Args:
        session: Session to update
        step: Step to append

    Returns:
        Updated session
    """
    session.history.append(step)
    session.step_count = len(session.history)
    session.last_checkpoint = datetime.now()

    save_session(session)
    return session


def update_status(session: Session, status: SessionStatus, reason: Optional[str] = None) -> Session:
    """
    Update the status of a session.

    Args:
        session: Session to update
        status: New status
        reason: Optional reason for the status change

    Returns:
        Updated session
    """
    old_status = session.status
    session.status = status

    if status == SessionStatus.COMPLETED and session.resolution_summary:
        pass  # Already has resolution
    elif status == SessionStatus.BLOCKED:
        session.stall_reason = reason or "Session blocked"
    elif status == SessionStatus.STALLED:
        session.stall_reason = reason or "Session stalled"

    save_session(session)
    logger.info(f"Session {session.session_id} status: {old_status} -> {status}")

    return session


def set_resolution(session: Session, resolution_summary: str) -> Session:
    """
    Set the resolution summary for a completed session.

    Args:
        session: Session to update
        resolution_summary: Resolution description

    Returns:
        Updated session
    """
    session.resolution_summary = resolution_summary
    session.status = SessionStatus.COMPLETED

    save_session(session)
    logger.info(f"Session {session.session_id} resolved: {resolution_summary[:100]}...")

    return session


def increment_step_count(session: Session) -> Session:
    """
    Increment the step counter for a session.

    Args:
        session: Session to update

    Returns:
        Updated session
    """
    session.step_count += 1
    session.updated_at = datetime.now()

    save_session(session)
    return session


def get_session_summary(session: Session) -> str:
    """
    Get a human-readable summary of a session.

    Args:
        session: Session to summarize

    Returns:
        Formatted summary string
    """
    lines = [
        f"Session: {session.session_id}",
        f"Title: {session.title}",
        f"Status: {session.status.value}",
        f"Mode: {session.mode.value}",
        f"Steps Completed: {session.step_count}",
        f"Target: {session.tmux_target}",
        f"Created: {session.created_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Updated: {session.updated_at.strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    if session.resolution_summary:
        lines.append(f"Resolution: {session.resolution_summary}")

    if session.stall_reason:
        lines.append(f"Stall Reason: {session.stall_reason}")

    if session.approval and session.approval.status != "none":
        lines.append(f"Approval: {session.approval.status}")
        if session.approval.reason:
            lines.append(f"  Reason: {session.approval.reason}")

    return "\n".join(lines)


def find_sessions_by_project(project: str) -> List[Session]:
    """
    Find all sessions for a given project.

    Args:
        project: Project name

    Returns:
        List of matching sessions
    """
    all_sessions = list_sessions()
    return [s for s in all_sessions if s.project == project]


def get_recent_sessions(limit: int = 10) -> List[Session]:
    """
    Get the most recent sessions.

    Args:
        limit: Maximum number of sessions to return

    Returns:
        List of recent sessions
    """
    sessions = list_sessions()
    return sessions[:limit]


def cleanup_old_sessions(days: int = 30) -> int:
    """
    Delete sessions older than the specified number of days.

    Args:
        days: Age threshold in days

    Returns:
        Number of sessions deleted
    """
    from datetime import timedelta

    settings = get_settings()
    sessions_dir = settings.get_sessions_path()

    if not sessions_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=days)
    deleted = 0

    for filepath in sessions_dir.glob("issue_*.json"):
        try:
            # Check file modification time
            mtime = datetime.fromtimestamp(filepath.stat().st_mtime)

            if mtime < cutoff:
                # Check if session is completed before deleting
                with open(filepath, 'r') as f:
                    session_dict = json.load(f)

                status = session_dict.get('status')
                if status == SessionStatus.COMPLETED.value:
                    filepath.unlink()
                    deleted += 1
                    logger.info(f"Deleted old session: {filepath.stem}")

        except Exception as e:
            logger.warning(f"Failed to process {filepath}: {e}")

    return deleted


def export_session(session_id: str, output_path: str) -> bool:
    """
    Export a session to a readable text format.

    Args:
        session_id: Session to export
        output_path: Output file path

    Returns:
        True if export successful
    """
    session = load_session(session_id)
    if not session:
        return False

    lines = [
        "=" * 70,
        f"SESSION EXPORT: {session.session_id}",
        "=" * 70,
        "",
        f"Title: {session.title}",
        f"Status: {session.status.value}",
        f"Mode: {session.mode.value}",
        f"Project: {session.project}",
        f"Created: {session.created_at.isoformat()}",
        f"Updated: {session.updated_at.isoformat()}",
        "",
        "-" * 70,
        "ISSUE DESCRIPTION",
        "-" * 70,
        session.issue_description,
        "",
    ]

    if session.project_context:
        lines.extend([
            "-" * 70,
            "PROJECT CONTEXT",
            "-" * 70,
            session.project_context,
            "",
        ])

    if session.operator_constraints:
        lines.extend([
            "-" * 70,
            "OPERATOR CONSTRAINTS",
            "-" * 70,
            session.operator_constraints,
            "",
        ])

    lines.extend([
        "-" * 70,
        "EXECUTION HISTORY",
        "-" * 70,
        "",
    ])

    for step in session.history:
        lines.extend([
            f"Step {step.step_number}: {step.step_title}",
            f"  Status: {step.planner_status}",
            f"  Time: {step.timestamp.isoformat()}",
            "",
        ])

        if step.files_touched:
            lines.append(f"  Files: {', '.join(step.files_touched)}")
            lines.append("")

        if step.planner_interpretation:
            lines.extend([
                "  Planner Rationale:",
                f"    {step.planner_interpretation}",
                "",
            ])

        if step.claude_output:
            lines.extend([
                "  Claude Response:",
                "  " + "-" * 66,
            ])
            for output_line in step.claude_output.split('\n')[:50]:  # Limit output
                lines.append(f"  {output_line}")
            lines.extend(["  " + "-" * 66, ""])

    if session.resolution_summary:
        lines.extend([
            "-" * 70,
            "RESOLUTION",
            "-" * 70,
            session.resolution_summary,
            "",
        ])

    if session.stall_reason:
        lines.extend([
            "-" * 70,
            "STALL REASON",
            "-" * 70,
            session.stall_reason,
            "",
        ])

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        logger.info(f"Exported session {session_id} to {output_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to export session: {e}")
        return False
