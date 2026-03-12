"""
Claude response capture from tmux with multi-layer completion detection.

Handles the complexity of waiting for Claude CLI to complete its response
and reliably extracting the output using response markers.
"""
import time
import logging
import hashlib
from typing import Optional
from datetime import datetime

from config import get_settings
from schema import TmuxCaptureResult, PartialCapture
from tmux_bridge import (
    capture_pane,
    wait_for_stable_output,
    check_pane_exists,
    ClaudeNotResponding,
    TmuxPaneNotFound,
)
from tmux_parser import (
    parse_tmux_output,
    START_MARKER,
    END_MARKER,
    strip_ansi_codes,
    normalize_whitespace,
)

logger = logging.getLogger(__name__)


class CaptureTimeout(Exception):
    """Raised when Claude response capture times out."""
    pass


class IncompleteCapture(Exception):
    """Raised when capture is incomplete (missing end marker)."""
    pass


# Global tracking for the last prompt sent (for echo removal)
_last_prompt_sent: Optional[str] = None


def set_last_prompt(prompt: str) -> None:
    """Track the last prompt sent for echo removal."""
    global _last_prompt_sent
    _last_prompt_sent = prompt


def get_last_prompt() -> Optional[str]:
    """Get the last prompt that was sent."""
    global _last_prompt_sent
    return _last_prompt_sent


def extract_claude_response(pane_content: str, prompt_sent: Optional[str] = None) -> Optional[str]:
    """
    Extract Claude's response from tmux pane content.

    Args:
        pane_content: Raw content from tmux capture-pane
        prompt_sent: The prompt that was sent (for echo removal)

    Returns:
        Extracted Claude response, or None if incomplete/not found
    """
    prompt_sent = prompt_sent or get_last_prompt()

    result = parse_tmux_output(pane_content, prompt_sent or "")

    if not result.is_complete:
        if result.has_start_marker and not result.has_end_marker:
            logger.warning("Capture has start marker but missing end marker")
        return None

    return result.extracted_response


def wait_and_capture(
    target: str,
    timeout: Optional[int] = None,
    prompt_sent: Optional[str] = None,
    baseline_text: Optional[str] = None
) -> Optional[str]:
    """
    Poll tmux pane until Claude finishes responding and capture the response.

    Uses 3-layer completion detection with stall-aware waiting:
    1. Primary: End marker found (<<<CLAUDE_RESPONSE_END>>>)
    2. Secondary: Output unchanged for N consecutive polls
    3. Stall fallback: Pane unchanged for too long with no valid response

    Args:
        target: Tmux target in format "session:window.pane"
        timeout: Soft max seconds to wait before entering stall-only mode
        prompt_sent: The prompt that was sent (for echo removal)

    Returns:
        Extracted Claude response, or None on timeout

    Raises:
        CaptureTimeout: If timeout is reached with no response
        TmuxPaneNotFound: If the target pane doesn't exist
    """
    settings = get_settings()
    timeout = timeout or settings.capture_timeout
    stall_timeout = settings.capture_stall_timeout
    prompt_sent = prompt_sent or get_last_prompt()
    poll_interval = settings.poll_interval
    stable_needed = settings.stable_poll_count

    logger.info(
        "Starting capture for %s, max_wait=%ss, stall_timeout=%ss, poll_interval=%ss, stable_needed=%s",
        target,
        timeout,
        stall_timeout,
        poll_interval,
        stable_needed,
    )
    baseline_hash = _text_hash(baseline_text) if baseline_text else None
    saw_new_output = False
    logger.info(
        "[TRACE][CLAUDE CAPTURE START] target=%s baseline_hash=%s",
        target,
        (baseline_hash[:12] if baseline_hash else "none"),
    )

    last_text = ""
    last_hash = ""
    stable_count = 0
    start_time = time.time()
    last_change_time = start_time
    warned_soft_limit = False
    capture_start_time = datetime.now()

    while True:
        try:
            # Verify pane still exists
            if not check_pane_exists(target):
                raise TmuxPaneNotFound(f"Target pane '{target}' no longer exists")

            # Capture current pane content
            text = capture_pane(target)
            current_hash = _text_hash(text)
            if current_hash != last_hash:
                last_change_time = time.time()
                last_hash = current_hash
            has_new_output = baseline_hash is None or current_hash != baseline_hash
            if has_new_output:
                saw_new_output = True

            # Layer 1: Check for end marker (primary detection)
            if END_MARKER in text and has_new_output:
                logger.info("End marker detected, attempting extraction")

                # Give a brief moment for any trailing output
                time.sleep(0.5)
                final_text = capture_pane(target)

                response = extract_claude_response(final_text, prompt_sent)
                if response:
                    if _looks_like_template_echo(response) or not _is_plausible_claude_response(response):
                        structured_fallback = _extract_structured_response_fallback(final_text)
                        if structured_fallback:
                            logger.info(
                                "Echo marker block detected; using structured tail fallback (%s chars)",
                                len(structured_fallback),
                            )
                            logger.info(
                                "[TRACE][CLAUDE CAPTURE DONE] target=%s output_hash=%s output_len=%s",
                                target,
                                _text_hash(structured_fallback)[:12],
                                len(structured_fallback),
                            )
                            return structured_fallback
                        logger.info("Detected non-final/echo marker block, waiting for real Claude output")
                        stable_count = 0
                        time.sleep(poll_interval)
                        continue
                    logger.info(f"Successfully captured response ({len(response)} chars)")
                    logger.info(
                        "[TRACE][CLAUDE CAPTURE DONE] target=%s output_hash=%s output_len=%s",
                        target,
                        _text_hash(response)[:12],
                        len(response),
                    )
                    return response
                else:
                    logger.warning("End marker found but extraction failed")
                    # Fall through to check if we can still extract something

            # Layer 2: Check for stability (secondary detection)
            if text == last_text:
                stable_count += 1
                if stable_count >= stable_needed and saw_new_output:
                    logger.info(f"Output stable for {stable_count} polls, attempting extraction")

                    # Additional check: verify we have both markers
                    if START_MARKER in text and END_MARKER in text:
                        response = extract_claude_response(text, prompt_sent)
                        if response:
                            if _looks_like_template_echo(response) or not _is_plausible_claude_response(response):
                                structured_fallback = _extract_structured_response_fallback(text)
                                if structured_fallback:
                                    logger.info(
                                        "Stable echo marker block detected; using structured tail fallback (%s chars)",
                                        len(structured_fallback),
                                    )
                                    logger.info(
                                        "[TRACE][CLAUDE CAPTURE DONE] target=%s output_hash=%s output_len=%s",
                                        target,
                                        _text_hash(structured_fallback)[:12],
                                        len(structured_fallback),
                                    )
                                    return structured_fallback
                                logger.info("Stable capture is non-final/echo block, continuing to wait")
                                stable_count = 0
                                continue
                            logger.info(f"Successfully captured stable response ({len(response)} chars)")
                            logger.info(
                                "[TRACE][CLAUDE CAPTURE DONE] target=%s output_hash=%s output_len=%s",
                                target,
                                _text_hash(response)[:12],
                                len(response),
                            )
                            return response

                    # If we only have start marker, wait a bit more
                    elif START_MARKER in text:
                        logger.info("Stable but missing end marker, continuing to wait")
                        stable_count = 0  # Reset to keep waiting
                    else:
                        logger.warning("Output stable but no markers found")
            else:
                stable_count = 0
                last_text = text

            # Log progress periodically
            elapsed = int(time.time() - start_time)
            stalled_for = int(time.time() - last_change_time)
            if elapsed % 30 == 0 and elapsed > 0:
                has_start = START_MARKER in text
                has_end = END_MARKER in text
                logger.debug(
                    "Still waiting... elapsed=%ss stalled_for=%ss start_marker=%s end_marker=%s",
                    elapsed,
                    stalled_for,
                    has_start,
                    has_end,
                )

            # Soft total timeout: warn, but keep waiting while pane is changing.
            if elapsed >= timeout and not warned_soft_limit:
                warned_soft_limit = True
                logger.warning(
                    "Capture exceeded soft max wait (%ss) for %s; continuing to wait while output changes",
                    timeout,
                    target,
                )

            # Hard stop on inactivity stall.
            if stalled_for >= stall_timeout:
                logger.warning(
                    "Capture stalled for %ss without pane changes (stall_timeout=%ss)",
                    stalled_for,
                    stall_timeout,
                )
                break

            time.sleep(poll_interval)

        except TmuxPaneNotFound:
            raise
        except Exception as e:
            logger.warning(f"Unexpected error during capture: {e}")
            stalled_for = int(time.time() - last_change_time)
            if stalled_for >= stall_timeout:
                logger.warning(
                    "Capture stalled for %ss while handling errors (stall_timeout=%ss)",
                    stalled_for,
                    stall_timeout,
                )
                break
            time.sleep(1)

    # Layer 3: Fallback - stalled with no confirmed complete response
    logger.warning("Capture ended without confirmed completion, performing final extraction attempt")

    # Try one final extraction attempt
    try:
        final_text = capture_pane(target)
        final_hash = _text_hash(final_text)
        final_has_new_output = baseline_hash is None or final_hash != baseline_hash
        response = extract_claude_response(final_text, prompt_sent)

        if response and final_has_new_output:
            if _looks_like_template_echo(response) or not _is_plausible_claude_response(response):
                structured_fallback = _extract_structured_response_fallback(final_text)
                if structured_fallback:
                    logger.info("Timeout capture used structured tail fallback extraction")
                    return structured_fallback
                logger.info("Timeout capture matched non-final/echo block; treating as incomplete")
            else:
                logger.info("Extracted response at timeout despite missing stable detection")
                return response

        # Save partial capture if we have at least the start marker
        if START_MARKER in final_text:
            save_partial_capture(target, final_text, capture_start_time, timeout)
            raise IncompleteCapture("Capture incomplete - missing end marker")

    except TmuxPaneNotFound:
        pass

    stalled_for = int(time.time() - last_change_time)
    elapsed = int(time.time() - start_time)
    raise CaptureTimeout(
        f"Claude response capture stalled after {elapsed} seconds "
        f"(no pane change for {stalled_for} seconds, stall_timeout={stall_timeout})"
    )


def _text_hash(text: str) -> str:
    """Hash pane text for change detection."""
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _looks_like_template_echo(response: str) -> bool:
    """
    Detect whether extracted text is the static template echoed from the sent prompt.

    This prevents false positives where prompt instructions are mistaken for Claude output.
    """
    lower = (response or "").lower()
    template_markers = [
        "[1-2 sentence summary of findings]",
        "/path/to/file.ext",
        "step one",
        "step two",
        "potential issue location 1",
        "specific next action to verify",
    ]
    hits = sum(1 for marker in template_markers if marker in lower)
    return hits >= 2


def _is_plausible_claude_response(response: str) -> bool:
    """
    Validate that a captured marker block looks like a real Claude final response.

    Rejects UI/control artifacts and requires at least a minimal sectioned structure.
    """
    text = (response or "").strip()
    if not text:
        return False

    lower = text.lower()
    noise_markers = [
        "interrupted",
        "what should claude do instead",
        "[pasted text",
        "ctrl+o to expand",
        "esc to interrupt",
        "for shortcuts",
    ]
    if any(marker in lower for marker in noise_markers):
        return False

    section_markers = [
        "summary:",
        "files:",
        "flow:",
        "findings:",
        "likely_breakpoints:",
        "recommended_next_check:",
        "affected_files:",
        "proposed_fix:",
        "validation:",
        "risk_assessment:",
        "fix_verification:",
        "checks_performed:",
        "edge_cases_considered:",
        "potential_issues:",
        "final_assessment:",
        "actions_taken:",
        "files_modified:",
        "next_steps:",
    ]
    hit_count = sum(1 for marker in section_markers if marker in lower)
    if "summary:" not in lower or hit_count < 2:
        return False

    return True


def _extract_structured_response_fallback(pane_content: str) -> Optional[str]:
    """
    Extract a structured Claude response from pane tail when marker block is echoed.

    This handles cases where Claude prints the real answer outside the requested
    START/END markers but still follows the required section format.
    """
    cleaned = normalize_whitespace(strip_ansi_codes(pane_content or ""))
    tail = cleaned
    if END_MARKER in cleaned:
        tail = cleaned[cleaned.rfind(END_MARKER) + len(END_MARKER):]

    lines = tail.split('\n')
    if not lines:
        return None

    headings = {
        "summary:",
        "files:",
        "flow:",
        "findings:",
        "likely_breakpoints:",
        "recommended_next_check:",
        "affected_files:",
        "proposed_fix:",
        "validation:",
        "risk_assessment:",
        "fix_verification:",
        "checks_performed:",
        "edge_cases_considered:",
        "potential_issues:",
        "final_assessment:",
        "actions_taken:",
        "files_modified:",
        "next_steps:",
    }

    start_idx = None
    for i, raw in enumerate(lines):
        line = raw.strip().lower()
        if line in headings and line == "summary:":
            start_idx = i
            break

    if start_idx is None:
        return None

    stop_prefixes = [
        "✻ brewed for",
        "❯",
        "⏵⏵",
        "bypass permissions on",
    ]

    keep = []
    for raw in lines[start_idx:]:
        line = raw.rstrip()
        line_stripped = line.strip()
        lower = line_stripped.lower()

        if not line_stripped:
            keep.append("")
            continue

        if line_stripped and all(ch == "─" for ch in line_stripped):
            break
        if any(lower.startswith(prefix) for prefix in stop_prefixes):
            break

        keep.append(line_stripped)

    candidate = "\n".join(keep).strip()
    if not candidate:
        return None

    if _looks_like_template_echo(candidate):
        return None
    if not _is_plausible_claude_response(candidate):
        return None

    return candidate


def save_partial_capture(
    target: str,
    content: str,
    capture_time: datetime,
    timeout: int
) -> PartialCapture:
    """
    Save a partial capture for manual review.

    Args:
        target: Tmux target
        content: Partially captured content
        capture_time: When the capture started
        timeout: Timeout that was reached

    Returns:
        PartialCapture object with the saved data
    """
    settings = get_settings()
    partials_dir = settings.get_partials_path()
    partials_dir.mkdir(parents=True, exist_ok=True)

    # Create partial capture record
    partial = PartialCapture(
        session_id=target.replace(':', '_').replace('.', '_'),
        step_number=0,  # Will be updated by caller
        captured_at=capture_time,
        partial_output=content,
        is_complete=False
    )

    # Save to file
    filename = f"partial_{partial.session_id}_{int(capture_time.timestamp())}.txt"
    filepath = partials_dir / filename

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(f"# Partial Capture - {capture_time.isoformat()}\n")
        f.write(f"# Target: {target}\n")
        f.write(f"# Timeout: {timeout}s\n")
        f.write(f"# Start marker present: {START_MARKER in content}\n")
        f.write(f"# End marker present: {END_MARKER in content}\n")
        f.write("#" + "=" * 70 + "\n\n")
        f.write(content)

    logger.info(f"Saved partial capture to {filepath}")

    return partial


def load_partial_capture(session_id: str) -> Optional[PartialCapture]:
    """
    Load the most recent partial capture for a session.

    Args:
        session_id: Session identifier

    Returns:
        PartialCapture if found, None otherwise
    """
    settings = get_settings()
    partials_dir = settings.get_partials_path()

    # Find the most recent partial capture for this session
    pattern = f"partial_{session_id}_*.txt"
    matching_files = list(partials_dir.glob(pattern))

    if not matching_files:
        return None

    # Sort by modification time, get the most recent
    latest_file = max(matching_files, key=lambda p: p.stat().st_mtime)

    with open(latest_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Parse the header
    lines = content.split('\n')
    capture_time = None
    timeout = 0
    has_start = False
    has_end = False

    for line in lines[:10]:  # Check header lines
        if line.startswith("# Capture Time:") or "# Partial Capture -" in line:
            try:
                capture_time = datetime.fromisoformat(line.split("-", 1)[1].strip())
            except:
                pass
        elif line.startswith("# Timeout:"):
            timeout = int(line.split(":")[1].strip().replace("s", ""))
        elif line.startswith("# Start marker present:"):
            has_start = "True" in line
        elif line.startswith("# End marker present:"):
            has_end = "True" in line

    # Extract the actual content (after header)
    content_start = content.find("#" + "=" * 70)
    if content_start != -1:
        actual_content = content[content_start + len("#" + "=" * 70):].strip()
    else:
        actual_content = content

    return PartialCapture(
        session_id=session_id,
        step_number=0,
        captured_at=capture_time or datetime.now(),
        partial_output=actual_content,
        is_complete=has_start and has_end
    )


def has_partial_capture(session_id: str) -> bool:
    """
    Check if there's a partial capture for the given session.

    Args:
        session_id: Session identifier

    Returns:
        True if a partial capture exists
    """
    settings = get_settings()
    partials_dir = settings.get_partials_path()

    pattern = f"partial_{session_id}_*.txt"
    return len(list(partials_dir.glob(pattern))) > 0


def capture_single_step(
    target: str,
    prompt: str,
    timeout: Optional[int] = None
) -> str:
    """
    Complete workflow: send prompt, wait for response, capture and return.

    Args:
        target: Tmux target
        prompt: Prompt to send
        timeout: Optional timeout override

    Returns:
        Captured Claude response

    Raises:
        CaptureTimeout: If timeout reached
        IncompleteCapture: If response is incomplete
        TmuxPaneNotFound: If pane doesn't exist
    """
    from tmux_bridge import send_prompt

    # Track the prompt for echo removal
    set_last_prompt(prompt)

    # Send the prompt
    send_prompt(target, prompt)

    # Wait for and capture the response
    response = wait_and_capture(target, timeout)

    if response is None:
        raise CaptureTimeout("Failed to capture Claude response")

    return response
