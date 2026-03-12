"""
Tmux output parsing and cleanup utilities.

Handles the messy reality of tmux capture-pane output including:
- ANSI escape sequences
- Echoed prompt text
- Duplicate response markers
- Incomplete captures
"""
import re
import string
from typing import Optional

from schema import TmuxCaptureResult


# ANSI escape sequence patterns
ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
ANSI_OSC = re.compile(r'\x1B\][^\x07]*\x07?')

# Response markers
START_MARKER = "<<<CLAUDE_RESPONSE_START>>>"
END_MARKER = "<<<CLAUDE_RESPONSE_END>>>"


def strip_ansi_codes(text: str) -> str:
    """
    Remove ANSI escape sequences from text.

    Handles:
    - CSI sequences: ESC[... (colors, cursor movement)
    - OSC sequences: ESC]... (window title, etc.)
    - Simple ESC sequences: ESCX

    Args:
        text: Text potentially containing ANSI codes

    Returns:
        Text with all ANSI sequences removed
    """
    # Remove OSC sequences first (they can contain brackets)
    text = ANSI_OSC.sub('', text)
    # Remove CSI and other escape sequences
    text = ANSI_ESCAPE.sub('', text)
    return text


def isolate_latest_marker_block(text: str) -> Optional[str]:
    """
    Find the most recent complete <<<CLAUDE_RESPONSE_START>>>...<<<CLAUDE_RESPONSE_END>>> block.

    If multiple blocks exist, return only the last complete one.
    If end marker is missing, return None (incomplete response).

    Args:
        text: Text to search for marker blocks

    Returns:
        Content between the last pair of markers, or None if incomplete
    """
    # Find all occurrences of start and end markers
    start_positions = [m.start() for m in re.finditer(re.escape(START_MARKER), text)]
    end_positions = [m.end() for m in re.finditer(re.escape(END_MARKER), text)]

    if not start_positions:
        return None

    if not end_positions:
        return None  # Incomplete - no end marker

    # We need at least one end marker after the last start marker
    last_start = start_positions[-1]
    matching_ends = [pos for pos in end_positions if pos > last_start]

    if not matching_ends:
        return None  # Incomplete - no end marker after last start

    # Extract content from the last complete block
    last_end = matching_ends[-1]
    content_start = last_start + len(START_MARKER)
    content_end = last_end - len(END_MARKER)

    return text[content_start:content_end].strip()


def trim_echoed_prompt(text: str, prompt_sent: str) -> str:
    """
    Remove the echoed prompt text from the beginning of captured output.

    This prevents the orchestrator from reading its own prompt as Claude's response.
    Handles various shell escaping and formatting issues.

    Args:
        text: Captured output that may include echoed prompt
        prompt_sent: The prompt that was sent (to identify echo)

    Returns:
        Text with echoed prompt removed
    """
    if not prompt_sent:
        return text

    lines = text.split('\n')

    # Normalize for comparison
    prompt_normalized = prompt_sent.strip().lower()
    prompt_first_word = prompt_sent.strip().split()[0].lower() if prompt_sent.split() else ""

    start_idx = 0
    for i, line in enumerate(lines):
        line_stripped = line.strip()

        # Skip empty lines at the start
        if not line_stripped:
            continue

        # Check if this line looks like our echoed prompt
        # Match if:
        # 1. Line contains a significant portion of our prompt
        # 2. Line starts with the same first word
        line_lower = line_stripped.lower()

        # Simple containment check (shell may add escaping)
        if prompt_normalized in line_lower and len(prompt_normalized) > 10:
            start_idx = i + 1
            break

        # First word match check (more robust)
        if prompt_first_word and prompt_first_word in line_lower.split()[0:1]:
            # Additional check: length should be reasonable
            if len(line_stripped) >= len(prompt_sent) * 0.5:
                start_idx = i + 1
                break

        # If we've hit a line that doesn't look like a prompt, stop looking
        if line_stripped and not (line_lower.startswith(prompt_first_word)):
            break

    return '\n'.join(lines[start_idx:])


def handle_duplicate_markers(text: str) -> str:
    """
    If Claude accidentally includes marker text in its response,
    ensure we only capture the outermost delimiters.

    For example, if Claude's response contains the marker text as part of
    its output, we need to make sure we don't treat it as a real marker.

    Args:
        text: Text with potentially duplicate/nested markers

    Returns:
        Text with only the outermost markers preserved
    """
    start_count = text.count(START_MARKER)
    end_count = text.count(END_MARKER)

    # No duplicates
    if start_count <= 1 and end_count <= 1:
        return text

    # Find first start and last end
    first_start = text.find(START_MARKER)
    last_end = text.rfind(END_MARKER)

    if first_start != -1 and last_end != -1 and last_end > first_start:
        # Extract from first START to last END (inclusive)
        return text[first_start:last_end + len(END_MARKER)]

    return text


def normalize_whitespace(text: str) -> str:
    """
    Normalize whitespace in text for more reliable parsing.

    - Collapse multiple consecutive spaces to single space
    - Normalize line endings
    - Remove trailing whitespace from each line

    Args:
        text: Text with potentially irregular whitespace

    Returns:
        Text with normalized whitespace
    """
    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Remove trailing whitespace from each line
    lines = text.split('\n')
    lines = [line.rstrip() for line in lines]

    # Rejoin and collapse multiple spaces (but not newlines)
    text = '\n'.join(lines)
    text = re.sub(r' +', ' ', text)

    return text


def parse_tmux_output(raw_output: str, prompt_sent: str) -> TmuxCaptureResult:
    """
    Full parsing pipeline for tmux output.

    Pipeline:
    1. Strip ANSI codes
    2. Normalize whitespace
    3. Trim echoed prompt
    4. Handle duplicate markers
    5. Isolate latest marker block

    Args:
        raw_output: Raw output from tmux capture-pane
        prompt_sent: The prompt that was sent (for echo removal)

    Returns:
        TmuxCaptureResult with parsing details
    """
    # Step 1: Strip ANSI codes
    cleaned = strip_ansi_codes(raw_output)

    # Step 2: Normalize whitespace
    cleaned = normalize_whitespace(cleaned)

    # Step 3: Trim echoed prompt
    cleaned = trim_echoed_prompt(cleaned, prompt_sent)

    # Check for markers
    has_start = START_MARKER in cleaned
    has_end = END_MARKER in cleaned

    # Step 4: Handle duplicate markers
    cleaned = handle_duplicate_markers(cleaned)

    # Step 5: Isolate latest marker block
    extracted = isolate_latest_marker_block(cleaned)
    is_complete = extracted is not None

    return TmuxCaptureResult(
        raw_output=raw_output,
        cleaned_output=cleaned,
        extracted_response=extracted,
        has_start_marker=has_start,
        has_end_marker=has_end,
        is_complete=is_complete
    )


def extract_summary_from_response(response: str) -> Optional[str]:
    """
    Extract the SUMMARY section from a Claude response.

    Args:
        response: Full Claude response text

    Returns:
        Summary text if found, None otherwise
    """
    if not response:
        return None

    # Look for SUMMARY: ... (end at next section or end of text)
    summary_match = re.search(
        r'SUMMARY:\s*\n(.*?)(?=\n[A-Z]+:|\n\n\n|$)',
        response,
        re.DOTALL
    )

    if summary_match:
        summary = summary_match.group(1).strip()
        # Take first 3 lines max
        lines = summary.split('\n')[:3]
        return ' '.join(lines).strip()

    # Fallback: return first 500 chars
    return response[:500].strip()


def extract_files_from_response(response: str) -> list[str]:
    """
    Extract file paths mentioned in a Claude response.

    Looks for patterns like:
    - path: /path/to/file
    - file.py
    - /path/to/file.py

    Args:
        response: Claude response text

    Returns:
        List of file paths found
    """
    if not response:
        return []

    files = []

    # Pattern 1: "path: /some/path"
    path_matches = re.findall(r'path:\s*([/\w\-.]+)', response)
    files.extend(path_matches)

    # Pattern 2: File extensions in context (heuristic)
    # Look for common code file patterns
    file_pattern = r'[/\w\-]+\.(?:py|js|ts|tsx|jsx|go|java|rs|rb|php|cs|sh|yaml|yml|json|xml|html|css|sql|md|txt|conf|cfg|ini)'
    file_matches = re.findall(file_pattern, response)
    files.extend(file_matches)

    # Deduplicate while preserving order
    seen = set()
    unique_files = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    return unique_files


def validate_response_markers(text: str) -> tuple[bool, str]:
    """
    Validate that response markers are present and well-formed.

    Args:
        text: Text to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if START_MARKER not in text:
        return False, "Missing start marker"

    if END_MARKER not in text:
        return False, "Missing end marker"

    # Check for proper ordering (start before end)
    start_pos = text.find(START_MARKER)
    end_pos = text.find(END_MARKER)

    if end_pos <= start_pos:
        return False, "End marker before start marker"

    return True, ""
