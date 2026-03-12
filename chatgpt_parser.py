"""
Parser for ChatGPT web responses.

Extracts structured data from ChatGPT responses using marker-based parsing.
"""
import re
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Response markers
ORCH_START_MARKER = "<<<ORCH_RESPONSE_START>>>"
ORCH_END_MARKER = "<<<ORCH_RESPONSE_END>>>"


@dataclass
class OrchestratorResponse:
    """Parsed response from ChatGPT with orchestrator markers."""
    # Control fields
    status: str  # continue, resolved, blocked, needs_human_action, stalled
    step_title: str
    why: str
    success_criteria: str
    done_signal: bool
    resolution_summary: Optional[str] = None
    human_action: Optional[str] = None

    # The prompt for Claude
    prompt_for_claude: str = ""

    # Optional approval request
    approval_requested: Optional[str] = None

    # Step tracking
    step_number: int = 1


class ParseError(Exception):
    """Raised when parsing fails."""
    pass


class MissingMarkersError(ParseError):
    """Raised when required markers are missing."""
    pass


class InvalidFormatError(ParseError):
    """Raised when response format is invalid."""
    pass


def extract_orchestrator_block(text: str) -> Optional[str]:
    """
    Extract the content between orchestrator markers.

    Args:
        text: Full ChatGPT response text

    Returns:
        Content between markers, or None if not found
    """
    # Find the most recent complete marker block
    pattern = re.escape(ORCH_START_MARKER) + r'(.*?)' + re.escape(ORCH_END_MARKER)
    matches = re.findall(pattern, text, re.DOTALL)

    if not matches:
        return None

    # Return the last complete block
    return matches[-1].strip()


def parse_key_value_block(block: str) -> Dict[str, str]:
    """
    Parse a block of KEY: VALUE pairs.

    Args:
        block: Text block with key-value pairs

    Returns:
        Dictionary of parsed key-value pairs
    """
    result = {}

    # Pattern: KEY: VALUE (multiline values supported)
    # Value continues until next KEY: or end of block
    pattern = r'^([A-Z_]+):\s*(.*?)(?=\n[A-Z_]+:|$)'

    matches = re.findall(pattern, block, re.MULTILINE | re.DOTALL)

    for key, value in matches:
        result[key.strip()] = value.strip()

    return result


def parse_chatgpt_response(text: str) -> OrchestratorResponse:
    """
    Parse ChatGPT response into an OrchestratorResponse.

    Expected format:
    <<<ORCH_RESPONSE_START>>>
    STATUS: CONTINUE
    STEP_TITLE: Verify deposit admin query path
    WHY: We must confirm where the admin page gets deposit data from.
    SUCCESS_CRITERIA: We will know the exact query and data source.
    PROMPT_FOR_CLAUDE_START
    Investigate only this one step...
    Can be multiple lines...
    PROMPT_FOR_CLAUDE_END
    DONE_SIGNAL: false
    <<<ORCH_RESPONSE_END>>>

    Args:
        text: Raw ChatGPT response text

    Returns:
        Parsed OrchestratorResponse

    Raises:
        MissingMarkersError: If markers are not found
        InvalidFormatError: If format is invalid
    """
    logger.info("Parsing ChatGPT response...")

    # Extract the marker block
    block = extract_orchestrator_block(text)

    if not block:
        raise MissingMarkersError(
            f"Could not find {ORCH_START_MARKER}...{ORCH_END_MARKER} block in response"
        )

    logger.debug(f"Extracted block: {block[:200]}...")

    # First, extract PROMPT_FOR_CLAUDE content (special multiline handling)
    prompt_for_claude = None
    block_without_prompt = block

    prompt_start_marker = "PROMPT_FOR_CLAUDE_START"
    prompt_end_marker = "PROMPT_FOR_CLAUDE_END"

    # Check for new block format first
    if prompt_start_marker in block and prompt_end_marker in block:
        start_idx = block.find(prompt_start_marker)
        end_idx = block.find(prompt_end_marker)

        # Extract content between markers
        prompt_content_start = start_idx + len(prompt_start_marker)
        prompt_content_end = end_idx
        prompt_for_claude = block[prompt_content_start:prompt_content_end].strip()

        # Remove the prompt block from the text for key-value parsing
        block_without_prompt = (
            block[:start_idx].strip() + "\n" + block[end_idx + len(prompt_end_marker):].strip()
        )
        logger.debug(f"Extracted PROMPT_FOR_CLAUDE block: {len(prompt_for_claude)} chars")
    else:
        # Fallback: try legacy KEY: VALUE format for PROMPT_FOR_CLAUDE
        logger.debug("No PROMPT_FOR_CLAUDE_START/END markers found, trying legacy format")

    # Parse key-value pairs from the remaining block
    data = parse_key_value_block(block_without_prompt)

    # Add prompt_for_claude if we extracted it via the block method
    if prompt_for_claude is not None:
        data['PROMPT_FOR_CLAUDE'] = prompt_for_claude

    # Validate required fields
    required_fields = ['STATUS', 'STEP_TITLE', 'WHY', 'SUCCESS_CRITERIA', 'DONE_SIGNAL']
    missing_fields = [f for f in required_fields if f not in data]

    if missing_fields:
        raise InvalidFormatError(f"Missing required fields: {', '.join(missing_fields)}")

    # PROMPT_FOR_CLAUDE is required for continue status
    if 'PROMPT_FOR_CLAUDE' not in data:
        raise InvalidFormatError("Missing required field: PROMPT_FOR_CLAUDE (use PROMPT_FOR_CLAUDE_START/END block)")

    # Parse fields
    try:
        status = data['STATUS'].lower().strip()
        step_title = data['STEP_TITLE'].strip()
        why = data['WHY'].strip()
        success_criteria = data['SUCCESS_CRITERIA'].strip()

        # Parse done_signal
        done_signal_str = data['DONE_SIGNAL'].lower().strip()
        done_signal = done_signal_str in ['true', 'yes', '1', 'on']

        # Optional fields
        resolution_summary = data.get('RESOLUTION_SUMMARY', '').strip() or None
        human_action = data.get('HUMAN_ACTION', '').strip() or None
        approval_requested = data.get('APPROVAL_REQUESTED', '').strip() or None

        # The prompt for Claude (extracted from block or key-value)
        prompt_for_claude = data.get('PROMPT_FOR_CLAUDE', '').strip()

        # Step number (optional, defaults to 1)
        step_number = int(data.get('STEP_NUMBER', '1'))

        response = OrchestratorResponse(
            status=status,
            step_title=step_title,
            why=why,
            success_criteria=success_criteria,
            done_signal=done_signal,
            resolution_summary=resolution_summary,
            human_action=human_action,
            prompt_for_claude=prompt_for_claude,
            approval_requested=approval_requested,
            step_number=step_number
        )

        logger.info(f"Parsed response: status={status}, step={step_number}, title={step_title}")
        return response

    except (ValueError, KeyError) as e:
        raise InvalidFormatError(f"Failed to parse response: {e}")


def clean_response(text: str) -> str:
    """
    Clean the response text by removing common artifacts.

    Removes:
    - Code blocks (markdown fences)
    - Excessive whitespace
    - Common ChatGPT prefixes/suffixes

    Args:
        text: Raw response text

    Returns:
        Cleaned text
    """
    # Remove markdown code blocks if they wrap our markers
    # e.g., ```json <<<ORCH_RESPONSE_START>>>...```
    text = re.sub(r'```(?:json|)?\s*(' + re.escape(ORCH_START_MARKER) + '.+?' + re.escape(ORCH_END_MARKER) + r')\s*```', r'\1', text, flags=re.DOTALL)

    # Normalize whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return text


def validate_response(response: OrchestratorResponse) -> tuple[bool, Optional[str]]:
    """
    Validate a parsed response for completeness and correctness.

    Args:
        response: Parsed response to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Check status values
    valid_statuses = ['continue', 'resolved', 'blocked', 'needs_human_action', 'stalled']
    if response.status not in valid_statuses:
        return False, f"Invalid status: {response.status}. Must be one of {valid_statuses}"

    # If continue, must have prompt_for_claude
    if response.status == 'continue' and not response.prompt_for_claude:
        return False, "STATUS is continue but PROMPT_FOR_CLAUDE is empty"

    # If resolved, should have resolution_summary
    if response.status == 'resolved' and not response.resolution_summary:
        return False, "STATUS is resolved but RESOLUTION_SUMMARY is missing"

    # If blocked, should have human_action
    if response.status == 'blocked' and not response.human_action:
        return False, "STATUS is blocked but HUMAN_ACTION is missing"

    # If needs_human_action, should have human_action
    if response.status == 'needs_human_action' and not response.human_action:
        return False, "STATUS is needs_human_action but HUMAN_ACTION is missing"

    return True, None


def parse_and_validate(text: str) -> OrchestratorResponse:
    """
    Parse and validate ChatGPT response in one step.

    Args:
        text: Raw ChatGPT response text

    Returns:
        Validated OrchestratorResponse

    Raises:
        ParseError: If parsing or validation fails
    """
    # Clean the text first
    cleaned = clean_response(text)

    # Parse the response
    response = parse_chatgpt_response(cleaned)

    # Validate
    is_valid, error = validate_response(response)
    if not is_valid:
        raise InvalidFormatError(f"Validation failed: {error}")

    return response


def extract_claude_summary_from_chatgpt(text: str) -> Optional[str]:
    """
    Extract a summary from ChatGPT's non-markered response text.

    This is for when ChatGPT doesn't use markers but still provides useful info.

    Args:
        text: ChatGPT response text

    Returns:
        Summary string or None
    """
    # First try to get the orch_response block
    try:
        response = parse_chatgpt_response(text)
        # Return the why field as a summary
        return f"{response.step_title}: {response.why}"
    except:
        pass

    # Fallback: Get first substantial paragraph
    text = clean_response(text)

    # Skip the marker block if present
    if ORCH_START_MARKER in text:
        # Get text before markers
        text = text.split(ORCH_START_MARKER)[0].strip()

    # Get first meaningful paragraph
    paragraphs = text.split('\n\n')
    for para in paragraphs:
        para = para.strip()
        if len(para) > 20 and not para.startswith('#'):
            return para[:200]

    return None


def detect_error_in_response(text: str) -> Optional[str]:
    """
    Detect if ChatGPT returned an error instead of a proper response.

    Args:
        text: ChatGPT response text

    Returns:
        Error message if detected, None otherwise
    """
    text_lower = text.lower()

    # Common error patterns
    error_patterns = [
        'network error',
        'something went wrong',
        'unable to process',
        'an error occurred',
        'please try again',
        'timeout',
        'rate limit',
        'too many requests',
    ]

    for pattern in error_patterns:
        if pattern in text_lower:
            return f"Detected error pattern: {pattern}"

    # Check for missing markers but substantial text (likely error or wrong format)
    if ORCH_START_MARKER not in text and len(text) > 100:
        return "Response missing orchestrator markers - ChatGPT may not be following the expected format"

    return None


def build_chatgpt_prompt(
    issue_title: str,
    issue_description: str,
    mode: str,
    current_step: int,
    claude_output: Optional[str] = None,
    history_summary: Optional[str] = None
) -> str:
    """
    Build the prompt to send to ChatGPT.

    Includes instructions about the expected response format.

    Args:
        issue_title: Issue being investigated
        issue_description: Full issue description
        mode: Current investigation mode
        current_step: Current step number
        claude_output: Latest Claude output (if any)
        history_summary: Summary of previous steps

    Returns:
        Complete prompt for ChatGPT
    """
    is_initial_turn = (current_step <= 0)

    parts = [f"# Investigation Request", f""]

    if is_initial_turn:
        parts.extend([
            f"## Issue: {issue_title}",
            f"",
            f"{issue_description}",
            f"",
        ])
    else:
        # Keep follow-up prompts incremental to avoid resending the full issue text.
        parts.extend([
            f"## Issue (ongoing): {issue_title}",
            f"",
            f"Use the existing conversation context from the initial issue description.",
            f"Do not restart from scratch; continue from latest Claude findings only.",
            f"",
        ])

    parts.extend([
        f"## Current Mode: {mode}",
        f"## Current Step: {current_step}",
        f"",
    ])

    if claude_output:
        parts.extend([
            f"## Latest Claude Output:",
            f"",
            f"{claude_output}",
            f"",
        ])

    if history_summary:
        parts.extend([
            f"## Recent History:",
            f"",
            f"{history_summary}",
            f"",
        ])

    parts.extend([
        f"## Instructions:",
        f"",
        f"Analyze the issue and latest Claude output. Determine the ONE next investigation step.",
        f"Respond ONLY with the following format (no additional text):",
        f"",
        f"<<<ORCH_RESPONSE_START>>>",
        f"STATUS: [continue|resolved|blocked|needs_human_action|stalled]",
        f"STEP_TITLE: [Brief title of the next step]",
        f"WHY: [Why this step is necessary]",
        f"SUCCESS_CRITERIA: [How to know this step succeeded]",
        f"PROMPT_FOR_CLAUDE_START",
        f"[The exact prompt to send to Claude CLI - can be multiple lines]",
        f"PROMPT_FOR_CLAUDE_END",
        f"DONE_SIGNAL: [true|false]",
        f"RESOLUTION_SUMMARY: [If done, the resolution summary]",
        f"HUMAN_ACTION: [If blocked, what human needs to do]",
        f"APPROVAL_REQUESTED: [If approval needed, reason]",
        f"<<<ORCH_RESPONSE_END>>>",
        f"",
        f"IMPORTANT:",
        f"- Keep PROMPT_FOR_CLAUDE focused on ONE specific action",
        f"- Prefer read-only investigation (read files, grep, inspect)",
        f"- Stop only when issue is proven, blocked, or ready for fix",
        f"- If no progress is being made, set STATUS=stalled",
    ])

    return "\n".join(parts)
