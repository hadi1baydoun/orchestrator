"""
Utility functions for the Bot Orchestrator.
"""
import re
import logging
from typing import List, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def extract_file_paths(text: str) -> List[str]:
    """
    Extract file paths from text using various patterns.

    Args:
        text: Text to search for file paths

    Returns:
        List of unique file paths found
    """
    if not text:
        return []

    files = []

    # Pattern 1: path: /some/path
    path_matches = re.findall(r'path:\s*([/\w\-.\\]+)', text)
    files.extend(path_matches)

    # Pattern 2: File paths with extensions
    # Common code file extensions
    extensions = [
        'py', 'js', 'ts', 'tsx', 'jsx', 'go', 'java', 'rs', 'rb', 'php',
        'cs', 'sh', 'bash', 'yaml', 'yml', 'json', 'xml', 'html', 'css',
        'sql', 'md', 'txt', 'conf', 'cfg', 'ini', 'toml', 'env', 'dockerfile'
    ]

    ext_pattern = r'[/\w\-.\\\]+\.(?:' + '|'.join(extensions) + r')'
    file_matches = re.findall(ext_pattern, text, re.IGNORECASE)
    files.extend(file_matches)

    # Pattern 3: Paths in quotes (common in error messages)
    quote_matches = re.findall(r'["\']([/\w\-.\\\]+)["\']', text)
    for match in quote_matches:
        # Only include if it looks like a file path
        if any(char in match for char in ['/', '\\', '.']) and len(match) > 3:
            files.append(match)

    # Pattern 4: Common path patterns
    # - src/, lib/, app/, etc.
    path_pattern = r'(?:src|lib|app|test|tests|config|scripts|utils|handlers|models|views|controllers)[/\w\-.\\\]*'
    path_matches = re.findall(path_pattern, text)
    files.extend(path_matches)

    # Deduplicate while preserving order
    seen = set()
    unique_files = []
    for f in files:
        # Normalize for comparison
        normalized = f.replace('\\', '/').lower()
        if normalized and normalized not in seen and len(normalized) > 3:
            seen.add(normalized)
            unique_files.append(f)

    return unique_files


def extract_summary(text: str, max_length: int = 500) -> str:
    """
    Extract a concise summary from text.

    Looks for structured sections like SUMMARY:, FINDINGS:, etc.

    Args:
        text: Text to summarize
        max_length: Maximum length of summary

    Returns:
        Extracted summary text
    """
    if not text:
        return ""

    # Look for SUMMARY section
    summary_match = re.search(
        r'SUMMARY:\s*\n(.*?)(?=\n[A-Z]+:|\n\n\n|$)',
        text,
        re.DOTALL
    )

    if summary_match:
        summary = summary_match.group(1).strip()
        # Take first 3 sentences or max_length
        sentences = re.split(r'[.!?]', summary)
        result = '. '.join(sentences[:3])
        return result[:max_length].strip()

    # Look for FINDINGS section
    findings_match = re.search(
        r'FINDINGS:\s*\n(.*?)(?=\n[A-Z]+:|\n\n\n|$)',
        text,
        re.DOTALL
    )

    if findings_match:
        findings = findings_match.group(1).strip()
        # Take first bullet point
        bullet_match = re.search(r'^[-*]\s*(.+)$', findings, re.MULTILINE)
        if bullet_match:
            return bullet_match.group(1).strip()[:max_length]

    # Fallback: Return first portion of text
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if line and not line.startswith('-') and not line.startswith('#') and len(line) > 10:
            return line[:max_length]

    # Last resort: first N chars
    return text[:max_length].strip()


def truncate_output(text: str, max_lines: int = 50, max_chars: int = 5000) -> str:
    """
    Truncate output to a manageable size.

    Args:
        text: Text to truncate
        max_lines: Maximum number of lines
        max_chars: Maximum number of characters

    Returns:
        Truncated text with indicator if truncated
    """
    lines = text.split('\n')

    if len(lines) > max_lines:
        truncated = '\n'.join(lines[:max_lines])
        if len(truncated) > max_chars:
            truncated = truncated[:max_chars]
        return truncated + f"\n\n... ({len(lines) - max_lines} more lines, {len(text) - len(truncated)} more chars)"

    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n... ({len(text) - max_chars} more chars)"

    return text


def format_timestamp(dt: datetime) -> str:
    """
    Format a datetime for display.

    Args:
        dt: DateTime to format

    Returns:
        Formatted timestamp string
    """
    if not dt:
        return "N/A"
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def format_duration(start: datetime, end: Optional[datetime] = None) -> str:
    """
    Format a duration between two datetimes.

    Args:
        start: Start time
        end: End time (uses now if None)

    Returns:
        Formatted duration string
    """
    if not start:
        return "N/A"

    end = end or datetime.now()
    duration = end - start

    total_seconds = int(duration.total_seconds())

    if total_seconds < 60:
        return f"{total_seconds}s"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}m {seconds}s"
    else:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for use as a filename.

    Args:
        name: String to sanitize

    Returns:
        Sanitized filename
    """
    # Remove invalid characters
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Remove leading/trailing spaces and dots
    sanitized = sanitized.strip('. ')
    # Limit length
    if len(sanitized) > 100:
        sanitized = sanitized[:100]
    return sanitized or "unnamed"


def ensure_unique_filepath(filepath: Path) -> Path:
    """
    Ensure a filepath is unique by adding suffix if needed.

    Args:
        filepath: Desired filepath

    Returns:
        Unique filepath (may have numeric suffix added)
    """
    if not filepath.exists():
        return filepath

    stem = filepath.stem
    suffix = filepath.suffix
    parent = filepath.parent

    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Setup logging configuration.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional log file path
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers = [logging.StreamHandler()]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )


def display_progress(current: int, total: int, prefix: str = "Progress") -> str:
    """
    Create a progress bar string.

    Args:
        current: Current progress
        total: Total value
        prefix: Prefix text

    Returns:
        Progress bar string
    """
    if total <= 0:
        return f"{prefix}: N/A"

    percentage = min(100, int((current / total) * 100))
    bar_length = 30
    filled = int((bar_length * current) // total)
    bar = '█' * filled + '░' * (bar_length - filled)

    return f"{prefix}: [{bar}] {percentage}% ({current}/{total})"


def parse_tmux_target(target: str) -> Optional[tuple[str, int, int]]:
    """
    Parse a tmux target string into components.

    Args:
        target: Target string in format "session:window.pane" or "session:window"

    Returns:
        Tuple of (session, window, pane) or None if invalid
    """
    try:
        parts = target.split(':')
        if len(parts) != 2:
            return None

        session = parts[0]
        window_pane = parts[1]

        # Parse window.pane
        if '.' in window_pane:
            window_parts = window_pane.split('.')
            window = int(window_parts[0])
            pane = int(window_parts[1]) if len(window_parts) > 1 else 0
        else:
            window = int(window_pane)
            pane = 0

        return (session, window, pane)

    except (ValueError, IndexError):
        return None


def validate_tmux_target(target: str) -> bool:
    """
    Validate a tmux target string format.

    Args:
        target: Target string to validate

    Returns:
        True if valid format
    """
    return parse_tmux_target(target) is not None
