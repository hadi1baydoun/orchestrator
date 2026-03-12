"""
Tmux bridge for interacting with Claude CLI running in tmux.

Provides functions to send prompts to and capture output from tmux panes.
Supports both local tmux and remote tmux via SSH.
"""
import subprocess
import time
import logging
import hashlib
from typing import Optional

from config import get_settings
from schema import SessionStatus

logger = logging.getLogger(__name__)


class TmuxPaneNotFound(Exception):
    """Raised when the specified tmux pane cannot be found."""
    pass


class TmuxCommandFailed(Exception):
    """Raised when a tmux command fails."""
    pass


class ClaudeNotResponding(Exception):
    """Raised when Claude CLI does not respond within timeout."""
    pass


def _build_ssh_command(base_cmd: list[str]) -> list[str]:
    """
    Wrap a command in SSH if SSH host is configured.

    Args:
        base_cmd: Base command to run (e.g., ['tmux', 'send-keys', ...])

    Returns:
        Command wrapped in SSH if configured, otherwise original command
    """
    settings = get_settings()

    if settings.ssh_host:
        ssh_cmd = ['ssh']

        # Add SSH key if specified
        if settings.ssh_key_path:
            ssh_cmd.extend(['-i', settings.ssh_key_path])

        # Add host and command
        ssh_cmd.append(settings.ssh_host)
        ssh_cmd.extend(base_cmd)

        logger.debug(f"Using SSH: {' '.join(ssh_cmd)}")
        return ssh_cmd

    return base_cmd


def _run_tmux_command(args: list[str]) -> str:
    """
    Run a tmux command and return its output.

    Args:
        args: List of command arguments (e.g., ['-t', 'dev:0.0', 'capture-pane', '-p'])

    Returns:
        Command output as string

    Raises:
        TmuxCommandFailed: If the tmux command fails
    """
    # Build the full command (with SSH if configured)
    full_cmd = _build_ssh_command(['tmux'] + args)

    try:
        # Use bytes for SSH to avoid Windows encoding issues
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            timeout=30  # Longer timeout for SSH
        )

        if result.returncode != 0:
            error_msg = result.stderr.decode('utf-8', errors='replace').strip()
            if "no such session" in error_msg.lower() or "can't find session" in error_msg.lower():
                raise TmuxPaneNotFound(f"Tmux pane not found: {error_msg}")
            raise TmuxCommandFailed(f"Tmux command failed: {error_msg}")

        return result.stdout.decode('utf-8', errors='replace')

    except subprocess.TimeoutExpired:
        raise TmuxCommandFailed("Tmux command timed out")
    except FileNotFoundError:
        # Check if it's SSH that's missing
        if get_settings().ssh_host:
            raise TmuxCommandFailed("SSH is not installed or not in PATH")
        raise TmuxCommandFailed("Tmux is not installed or not in PATH")


def send_prompt(target: str, prompt: str) -> None:
    """
    Send a prompt to the specified tmux pane.

    The prompt is sent as keystrokes followed by Enter (C-m).

    Args:
        target: Tmux target in format "session:window.pane"
        prompt: Text to send to the pane

    Raises:
        TmuxPaneNotFound: If the target pane doesn't exist
        TmuxCommandFailed: If the send-keys command fails
    """
    # Verify pane exists first
    try:
        _run_tmux_command(['display-message', '-p', '-t', target])
    except TmuxCommandFailed as e:
        if "no such session" in str(e).lower():
            raise TmuxPaneNotFound(f"Target pane '{target}' not found")
        raise

    logger.info(
        "[TRACE][TMUX SEND] target=%s prompt_len=%s prompt_hash=%s",
        target,
        len(prompt or ""),
        hashlib.sha256((prompt or "").encode("utf-8", errors="replace")).hexdigest()[:12],
    )

    # Send the prompt using SSH with proper escaping
    settings = get_settings()

    if settings.ssh_host:
        # For SSH, use load-buffer + paste-buffer to safely handle multiline prompts
        # This avoids shell escaping issues with send-keys -l

        ssh_base = ['ssh']
        if settings.ssh_key_path:
            ssh_base.extend(['-i', settings.ssh_key_path])
        ssh_base.append(settings.ssh_host)

        # Step 1: load-buffer reads from stdin (safe for multiline content)
        cmd1 = 'tmux load-buffer -'
        logger.info("TMUX_SEND_COMMAND_1=%s", " ".join(ssh_base + [cmd1]))
        result1 = subprocess.run(
            ssh_base + [cmd1],
            input=prompt.encode('utf-8'),
            capture_output=True,
            timeout=30  # Longer timeout for large prompts
        )

        # Step 2: paste-buffer -t <target> (pastes the buffer content)
        cmd2 = f'tmux paste-buffer -t {target}'
        logger.info("TMUX_SEND_COMMAND_2=%s", " ".join(ssh_base + [cmd2]))
        result2 = subprocess.run(
            ssh_base + [cmd2],
            capture_output=True,
            timeout=10
        )

        # Step 3: send-keys -t <target> C-m (press Enter)
        cmd3 = f'tmux send-keys -t {target} C-m'
        logger.info("TMUX_SEND_COMMAND_3=%s", " ".join(ssh_base + [cmd3]))
        result3 = subprocess.run(
            ssh_base + [cmd3],
            capture_output=True,
            timeout=10
        )

        if result1.returncode != 0 or result2.returncode != 0 or result3.returncode != 0:
            stderr_combined = (result1.stderr + result2.stderr + result3.stderr).decode('utf-8', errors='replace')
            err_lower = stderr_combined.lower()
            if "no such session" in err_lower or "can't find session" in err_lower:
                raise TmuxPaneNotFound(f"Tmux pane not found: {stderr_combined}")
            raise TmuxCommandFailed(f"SSH buffer/paste failed: {stderr_combined}")
    else:
        # Local tmux - use load-buffer + paste-buffer for consistency and safety
        logger.info("TMUX_SEND_LOCAL_BUFFER=true")

        # Step 1: load-buffer (pass prompt via stdin)
        proc1 = subprocess.run(
            ['tmux', 'load-buffer', '-'],
            input=prompt.encode('utf-8'),
            capture_output=True,
            timeout=30
        )
        if proc1.returncode != 0:
            error = proc1.stderr.decode('utf-8', errors='replace')
            raise TmuxCommandFailed(f"Local load-buffer failed: {error}")

        # Step 2: paste-buffer -t <target>
        proc2 = subprocess.run(
            ['tmux', 'paste-buffer', '-t', target],
            capture_output=True,
            timeout=10
        )
        if proc2.returncode != 0:
            error = proc2.stderr.decode('utf-8', errors='replace')
            raise TmuxCommandFailed(f"Local paste-buffer failed: {error}")

        # Step 3: send-keys -t <target> C-m
        _run_tmux_command(['send-keys', '-t', target, 'C-m'])


def capture_pane(target: str, escape: bool = False) -> str:
    """
    Capture the current content of the specified tmux pane.

    Args:
        target: Tmux target in format "session:window.pane"
        escape: Whether to escape ANSI codes (False preserves them)

    Returns:
        Captured pane content as string

    Raises:
        TmuxPaneNotFound: If the target pane doesn't exist
        TmuxCommandFailed: If the capture-pane command fails
    """
    # Include deeper scrollback so START/END markers from long Claude outputs are both available.
    args = ['-p', '-t', target, '-S', '-4000', '-e']  # -e keeps escapes, -S captures history

    output = _run_tmux_command(['capture-pane'] + args)
    logger.debug(
        "[TRACE][TMUX CAPTURE] target=%s bytes=%s",
        target,
        len(output.encode("utf-8", errors="replace")),
    )

    return output


def send_and_capture(target: str, prompt: str) -> str:
    """
    Send a prompt and immediately capture the result.

    Useful for simple commands where you want the output right away.
    For Claude interactions, use send_prompt then wait_for_completion.

    Args:
        target: Tmux target in format "session:window.pane"
        prompt: Text to send to the pane

    Returns:
        Captured output after sending the prompt
    """
    send_prompt(target, prompt)
    time.sleep(0.5)  # Brief pause for command to execute
    return capture_pane(target)


def wait_for_stable_output(
    target: str,
    stable_count: int = 4,
    interval: int = 3,
    timeout: int = 300
) -> str:
    """
    Wait for tmux output to become stable (unchanged across multiple polls).

    Args:
        target: Tmux target in format "session:window.pane"
        stable_count: Number of consecutive unchanged polls to consider stable
        interval: Seconds between polls
        timeout: Maximum seconds to wait

    Returns:
        The stable output when achieved

    Raises:
        ClaudeNotResponding: If timeout is reached
        TmuxPaneNotFound: If the target pane disappears during polling
    """
    settings = get_settings()
    stable_count = stable_count or settings.stable_poll_count
    interval = interval or settings.poll_interval
    timeout = timeout or settings.capture_timeout

    last_output = ""
    stable_counter = 0
    start_time = time.time()

    while (time.time() - start_time) < timeout:
        try:
            current_output = capture_pane(target)

            if current_output == last_output:
                stable_counter += 1
                if stable_counter >= stable_count:
                    return current_output
            else:
                stable_counter = 0
                last_output = current_output

            time.sleep(interval)

        except TmuxPaneNotFound:
            raise
        except TmuxCommandFailed as e:
            # Retry once on transient failures
            time.sleep(1)
            try:
                current_output = capture_pane(target)
                if current_output == last_output:
                    stable_counter += 1
                    if stable_counter >= stable_count:
                        return current_output
                else:
                    stable_counter = 0
                    last_output = current_output
            except Exception:
                raise

    raise ClaudeNotResponding(
        f"Output did not stabilize within {timeout} seconds"
    )


def check_pane_exists(target: str) -> bool:
    """
    Check if the specified tmux pane exists.

    Args:
        target: Tmux target in format "session:window.pane"

    Returns:
        True if pane exists, False otherwise
    """
    try:
        _run_tmux_command(['display-message', '-p', '-t', target])
        return True
    except (TmuxPaneNotFound, TmuxCommandFailed):
        return False


def get_pane_info(target: str) -> dict:
    """
    Get detailed information about a tmux pane.

    Args:
        target: Tmux target in format "session:window.pane"

    Returns:
        Dictionary with pane information including:
        - session_name: Name of the session
        - window_index: Window index
        - pane_index: Pane index
        - pane_current_command: Command running in the pane
        - pane_current_path: Current working directory
    """
    # Get display-message format
    formats = {
        'session_name': '#{session_name}',
        'window_index': '#{window_index}',
        'pane_index': '#{pane_index}',
        'pane_current_command': '#{pane_current_command}',
        'pane_current_path': '#{pane_current_path}',
        'cursor_x': '#{cursor_x}',
        'cursor_y': '#{cursor_y}',
    }

    info = {}
    for key, fmt in formats.items():
        try:
            value = _run_tmux_command(['display-message', '-p', '-t', target, fmt])
            info[key] = value.strip()
        except TmuxCommandFailed:
            info[key] = None

    return info


def list_sessions() -> list[str]:
    """
    List all active tmux sessions.

    Returns:
        List of session names
    """
    try:
        output = _run_tmux_command(['list-sessions', '-F', '#{session_name}'])
        return output.strip().split('\n') if output.strip() else []
    except TmuxCommandFailed:
        return []


def list_windows(session: str) -> list[dict]:
    """
    List all windows in a tmux session.

    Args:
        session: Session name

    Returns:
        List of dictionaries with window info
    """
    try:
        format_str = '#{window_index}:#{window_name}:#{window_width}x#{window_height}'
        output = _run_tmux_command(['list-windows', '-t', session, '-F', format_str])

        windows = []
        for line in output.strip().split('\n'):
            if line:
                parts = line.split(':')
                if len(parts) >= 3:
                    windows.append({
                        'index': parts[0],
                        'name': parts[1],
                        'size': parts[2]
                    })

        return windows

    except TmuxCommandFailed:
        return []


def verify_claude_running(target: str) -> bool:
    """
    Verify that Claude CLI is running in the target pane.

    Args:
        target: Tmux target in format "session:window.pane"

    Returns:
        True if Claude appears to be running, False otherwise
    """
    try:
        info = get_pane_info(target)
        command = info.get('pane_current_command', '').lower()

        # Check for common Claude CLI indicators
        claude_indicators = ['claude', 'python', 'python3']

        return any(indicator in command for indicator in claude_indicators)

    except (TmuxPaneNotFound, TmuxCommandFailed):
        return False


def scroll_to_bottom(target: str) -> None:
    """
    Scroll the tmux pane to the bottom to capture recent output.

    Args:
        target: Tmux target in format "session:window.pane"
    """
    try:
        _run_tmux_command(['send-keys', '-t', target, 'C-d'])
        # Or use select-pane with -m
    except TmuxCommandFailed:
        pass  # Not critical


def clear_pane_history(target: str) -> None:
    """
    Clear the scrollback history of the specified pane.

    Useful for starting fresh captures.

    Args:
        target: Tmux target in format "session:window.pane"
    """
    try:
        _run_tmux_command(['clear-history', '-t', target])
    except TmuxCommandFailed:
        pass  # Not critical
