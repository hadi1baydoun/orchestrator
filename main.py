"""
CLI interface for the Bot Orchestrator.

Commands:
- new: Create a new session
- run: Run orchestration
- resume: Resume a paused/crashed session
- status: Show session status
- stop: Stop a running session
- list: List all sessions
- approve: Grant approval for pending action
- deny: Deny approval for pending action
- export: Export session to readable format
"""
import sys
import asyncio
import logging
from pathlib import Path
from typing import Optional

import click

from schema import Mode, SessionStatus
from config import get_settings, reload_settings
from session_manager import (
    create_session,
    load_session,
    list_sessions,
    save_session,
    delete_session,
    get_session_summary,
    export_session,
)
from orchestrator import get_orchestrator
from approval import grant_approval, deny_approval, get_approval_manager
from recovery import resume_after_crash
from tmux_bridge import check_pane_exists, list_sessions as list_tmux_sessions

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@click.group()
@click.option('--config', '-c', type=click.Path(), help='Path to config file')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose output')
def cli(config: Optional[str], verbose: bool):
    """Bot Orchestrator - Bridge between ChatGPT planner and Claude CLI executor."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if config:
        # Reload settings with custom config
        settings = reload_settings()
    else:
        settings = get_settings()

    # Ensure directories exist
    settings.ensure_directories()


@cli.command()
@click.option('--title', '-t', required=True, help='Issue title')
@click.option('--description', '-d', required=True, help='Issue description')
@click.option('--project', '-p', default='default', help='Project name')
@click.option('--tmux', '-x', 'tmux_target', help='Tmux target (format: session:window.pane)')
@click.option('--mode', '-m', 'mode',
              type=click.Choice(['investigate', 'propose_fix', 'validate_fix', 'approved_execute']),
              default='investigate', help='Execution mode')
@click.option('--context', '-c', 'project_context', help='Project context')
@click.option('--constraints', '-C', 'operator_constraints', help='Operator constraints')
@click.option('--max-steps', type=int, help='Maximum steps')
def new(title: str, description: str, project: str, tmux_target: Optional[str],
        mode: str, project_context: Optional[str], operator_constraints: Optional[str],
        max_steps: Optional[int]):
    """Create a new investigation session."""
    settings = get_settings()

    # Use default tmux target if not specified
    if not tmux_target:
        tmux_target = settings.default_tmux_target

    # Verify tmux target exists
    if not check_pane_exists(tmux_target):
        click.echo(f"Error: Tmux target '{tmux_target}' not found", err=True)
        click.echo("\nAvailable tmux sessions:")
        for session in list_tmux_sessions():
            click.echo(f"  - {session}")
        sys.exit(1)

    # Create session
    session = create_session(
        title=title,
        issue_description=description,
        issue_seed_prompt=description,
        project_context=project_context,
        operator_constraints=operator_constraints,
        project=project,
        tmux_target=tmux_target,
        mode=Mode(mode),
        max_steps=max_steps or settings.max_steps
    )

    click.echo(f"Created session: {session.session_id}")
    click.echo(f"Title: {title}")
    click.echo(f"Mode: {mode}")
    click.echo(f"Target: {tmux_target}")
    click.echo(f"\nTo run: python main.py run --session {session.session_id}")


@cli.command()
@click.option('--session', '-s', 'session_id', help='Session ID to run')
def run(session_id: Optional[str]):
    """Run orchestration for a session."""
    if not session_id:
        # List running sessions and prompt
        sessions = list_sessions()
        running = [s for s in sessions if s.status == SessionStatus.RUNNING]

        if not sessions:
            click.echo("No sessions found. Create one with 'new' command.")
            return

        if running:
            click.echo("Resume running session?")
            for i, s in enumerate(running, 1):
                click.echo(f"  {i}. {s.session_id}: {s.title}")
            # For now, just use the first running session
            session_id = running[0].session_id
        else:
            click.echo("Available sessions:")
            for i, s in enumerate(sessions[:5], 1):
                click.echo(f"  {i}. {s.session_id}: {s.title} ({s.status.value})")
            click.echo("\nUse --session to specify which session to run.")
            return

    # Run orchestration
    async def do_run():
        orchestrator = get_orchestrator()
        session = await orchestrator.run_orchestration(session_id=session_id)
        return session

    session = asyncio.run(do_run())

    click.echo(f"\nOrchestration completed for session {session.session_id}")
    click.echo(f"Final status: {session.status.value}")


@cli.command('run-web')
@click.option('--session', '-s', 'session_id', help='Session ID to run')
@click.option('--title', '-t', help='Issue title (for new sessions)')
@click.option('--description', '-d', 'issue_description', help='Issue description (for new sessions)')
@click.option('--project', '-p', default='default', help='Project name')
@click.option('--tmux', '-x', 'tmux_target', help='Tmux target (format: session:window.pane)')
@click.option('--mode', '-m', 'mode',
              type=click.Choice(['investigate', 'propose_fix', 'validate_fix', 'approved_execute']),
              default='investigate', help='Execution mode')
@click.option('--chatgpt-url', '-u', help='URL of existing ChatGPT conversation to attach to')
@click.option('--headless', is_flag=True, help='Run browser in headless mode')
def run_web(session_id: Optional[str], title: Optional[str], issue_description: Optional[str],
            project: str, tmux_target: Optional[str], mode: str, chatgpt_url: Optional[str],
            headless: bool):
    """Run orchestration using ChatGPT web interface (browser automation)."""
    settings = get_settings()

    # If creating new session, need title and description
    if not session_id:
        if not title or not issue_description:
            click.echo("Error: --title and --description required for new sessions", err=True)
            click.echo("Or use --session to resume an existing session")
            sys.exit(1)

    # Verify tmux target exists
    tmux_target = tmux_target or settings.default_tmux_target
    if not check_pane_exists(tmux_target):
        click.echo(f"Error: Tmux target '{tmux_target}' not found", err=True)
        click.echo("\nStart tmux with Claude CLI:")
        click.echo("  tmux new-session -d -s dev")
        click.echo("  tmux send-keys -t dev:0.0 'claude' C-m")
        sys.exit(1)

    # Check if Playwright is installed
    try:
        import playwright
    except ImportError:
        click.echo("Error: Playwright not installed", err=True)
        click.echo("\nInstall with: pip install playwright")
        click.echo("Then run: playwright install chromium")
        sys.exit(1)

    # Run web orchestration
    async def do_run_web():
        orchestrator = get_orchestrator()
        session = await orchestrator.run_web_orchestration(
            session_id=session_id,
            title=title,
            issue_description=issue_description,
            project=project,
            tmux_target=tmux_target,
            mode=mode,
            chatgpt_url=chatgpt_url,
            headless=headless
        )
        return session

    session = asyncio.run(do_run_web())

    click.echo(f"\nOrchestration completed for session {session.session_id}")
    click.echo(f"Final status: {session.status.value}")


@cli.command()
@click.option('--session', '-s', 'session_id', required=True, help='Session ID to resume')
def resume(session_id: str):
    """Resume a paused or crashed session."""
    if not resume_after_crash(session_id):
        click.echo(f"Failed to resume session {session_id}", err=True)
        sys.exit(1)

    click.echo(f"Session {session_id} resumed")
    click.echo(f"\nTo continue: python main.py run --session {session_id}")


@cli.command()
@click.option('--session', '-s', 'session_id', help='Session ID (shows latest if not specified)')
def status(session_id: Optional[str]):
    """Show session status."""
    if session_id:
        session = load_session(session_id)
        if not session:
            click.echo(f"Session {session_id} not found", err=True)
            sys.exit(1)
        sessions = [session]
    else:
        sessions = list_sessions()[:5]  # Show latest 5

    for session in sessions:
        click.echo(get_session_summary(session))
        click.echo()


@cli.command()
@click.option('--session', '-s', 'session_id', required=True, help='Session ID to stop')
def stop(session_id: str):
    """Stop a running session."""
    session = load_session(session_id)
    if not session:
        click.echo(f"Session {session_id} not found", err=True)
        sys.exit(1)

    orchestrator = get_orchestrator()
    if orchestrator.is_running():
        orchestrator.stop()
        click.echo(f"Stopped orchestration for session {session_id}")
    else:
        click.echo(f"No orchestration currently running")

    # Update session status
    from session_manager import update_status
    update_status(session, SessionStatus.PAUSED, "Stopped by user")
    click.echo(f"Session {session_id} marked as paused")


@cli.command('list')
@click.option('--status', '-s', 'status_filter', type=click.Choice([
    'running', 'completed', 'blocked', 'paused', 'stalled', 'awaiting_approval'
]), help='Filter by status')
@click.option('--project', '-p', help='Filter by project')
@click.option('--limit', '-l', type=int, default=20, help='Maximum sessions to show')
def list_sessions(status_filter: Optional[str], project: Optional[str], limit: int):
    """List all sessions."""
    sessions = list_sessions(
        SessionStatus(status_filter) if status_filter else None
    )

    if project:
        sessions = [s for s in sessions if s.project == project]

    sessions = sessions[:limit]

    if not sessions:
        click.echo("No sessions found")
        return

    click.echo(f"\n{'Session ID':<35} {'Title':<30} {'Status':<15} {'Steps'}")
    click.echo("-" * 90)

    for session in sessions:
        steps = f"{session.step_count}"
        click.echo(f"{session.session_id:<35} {session.title[:30]:<30} {session.status.value:<15} {steps}")


@cli.command()
@click.option('--session', '-s', 'session_id', help='Session ID (uses latest awaiting approval if not specified)')
@click.option('--commands', '-c', multiple=True, help='Specific commands to approve')
def approve(session_id: Optional[str], commands: tuple):
    """Grant approval for a pending action."""
    approval_mgr = get_approval_manager()

    # Find session awaiting approval
    if not session_id:
        sessions = list_sessions()
        awaiting = [s for s in sessions if s.status == SessionStatus.AWAITING_APPROVAL]
        if not awaiting:
            click.echo("No sessions awaiting approval")
            return
        if len(awaiting) > 1:
            click.echo("Multiple sessions awaiting approval. Please specify --session")
            for s in awaiting:
                click.echo(f"  - {s.session_id}: {s.title}")
            return
        session_id = awaiting[0].session_id

    success = grant_approval(
        session_id,
        list(commands) if commands else None
    )

    if success:
        click.echo(f"Approval granted for session {session_id}")
        click.echo(f"\nTo continue: python main.py run --session {session_id}")
    else:
        click.echo(f"Failed to grant approval for session {session_id}", err=True)
        sys.exit(1)


@cli.command()
@click.option('--session', '-s', 'session_id', help='Session ID')
@click.option('--reason', '-r', help='Reason for denial')
def deny(session_id: Optional[str], reason: Optional[str]):
    """Deny approval for a pending action."""
    approval_mgr = get_approval_manager()

    # Find session awaiting approval
    if not session_id:
        sessions = list_sessions()
        awaiting = [s for s in sessions if s.status == SessionStatus.AWAITING_APPROVAL]
        if not awaiting:
            click.echo("No sessions awaiting approval")
            return
        if len(awaiting) > 1:
            click.echo("Multiple sessions awaiting approval. Please specify --session")
            for s in awaiting:
                click.echo(f"  - {s.session_id}: {s.title}")
            return
        session_id = awaiting[0].session_id

    success = deny_approval(session_id, reason)

    if success:
        click.echo(f"Approval denied for session {session_id}")
        if reason:
            click.echo(f"Reason: {reason}")
    else:
        click.echo(f"Failed to deny approval for session {session_id}", err=True)
        sys.exit(1)


@cli.command()
@click.option('--session', '-s', 'session_id', required=True, help='Session ID to export')
@click.option('--output', '-o', type=click.Path(), help='Output file path')
def export(session_id: str, output: Optional[str]):
    """Export session to readable text format."""
    if not output:
        output = f"{session_id}_export.txt"

    success = export_session(session_id, output)

    if success:
        click.echo(f"Exported session {session_id} to {output}")
    else:
        click.echo(f"Failed to export session {session_id}", err=True)
        sys.exit(1)


@cli.command()
@click.option('--session', '-s', 'session_id', required=True, help='Session ID to delete')
@click.option('--force', '-f', is_flag=True, help='Force deletion without confirmation')
def delete(session_id: str, force: bool):
    """Delete a session."""
    session = load_session(session_id)
    if not session:
        click.echo(f"Session {session_id} not found", err=True)
        sys.exit(1)

    if not force:
        click.echo(f"Session: {session.title}")
        click.echo(f"Status: {session.status.value}")
        click.echo(f"Steps: {session.step_count}")
        click.echo()
        if not click.confirm(f"Delete session {session_id}?"):
            click.echo("Cancelled")
            return

    success = delete_session(session_id)

    if success:
        click.echo(f"Deleted session {session_id}")
    else:
        click.echo(f"Failed to delete session {session_id}", err=True)
        sys.exit(1)


@cli.command()
def version():
    """Show version information."""
    click.echo("Bot Orchestrator v1.0.0")
    click.echo("\nConfiguration:")
    settings = get_settings()
    click.echo(f"  Planner Model: {settings.planner_model}")
    click.echo(f"  Default Tmux: {settings.default_tmux_target}")
    click.echo("  Max Steps: disabled")
    click.echo(f"  Sessions Dir: {settings.sessions_dir}")


if __name__ == '__main__':
    cli()
