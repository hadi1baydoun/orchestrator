"""
Stop condition detection for the orchestrator.

Implements concrete heuristics for detecting when the investigation
should stop (completed, blocked, stalled, or needs human action).
"""
import logging
from typing import Tuple, Optional
from collections import Counter

from schema import PlannerResponse, Session, SessionStatus

logger = logging.getLogger(__name__)


class StopDetector:
    """
    Detects stop conditions during orchestration.

    Evaluates both explicit stop signals from the planner and
    automatic detection of no-progress situations.
    """

    def should_stop(
        self,
        planner_response: PlannerResponse,
        session: Session
    ) -> Tuple[bool, str, Optional[SessionStatus]]:
        """
        Determine if the orchestration should stop.

        Args:
            planner_response: The latest response from ChatGPT
            session: Current session state

        Returns:
            Tuple of (should_stop, reason, new_status)
            - should_stop: True if orchestration should stop
            - reason: Human-readable reason for stopping
            - new_status: The session status to set (or None to keep current)
        """
        # DONE_SIGNAL is authoritative. Never stop automatically while False.
        if not planner_response.done_signal:
            if planner_response.status in {"resolved", "blocked", "needs_human_action", "stalled"}:
                logger.warning(
                    "Planner returned status=%s with done_signal=false; continuing per DONE_SIGNAL policy",
                    planner_response.status,
                )
            return False, "", None

        # Planner explicit stop signals (only when done_signal is true)
        if planner_response.status == "resolved":
            return True, "Planner signaled issue is resolved", SessionStatus.COMPLETED

        if planner_response.status == "blocked":
            return True, f"Planner signaled blocked: {planner_response.human_action or 'No forward path'}", SessionStatus.BLOCKED

        if planner_response.status == "needs_human_action":
            return True, f"Human action required: {planner_response.human_action or 'Manual intervention needed'}", SessionStatus.BLOCKED

        if planner_response.status == "stalled":
            return True, f"Planner stalled: {planner_response.resolution_summary or 'No progress being made'}", SessionStatus.STALLED

        # Check if planner is asking for something impossible
        if planner_response.status == "continue" and not planner_response.prompt_for_claude.strip():
            return True, "Planner returned continue status but no prompt for Claude", SessionStatus.BLOCKED

        return False, "", None

    def _detect_no_progress(self, session: Session) -> bool:
        """
        Concrete heuristics for detecting no progress.

        Returns True if any of these conditions are met:
        1. Same file touched 3+ consecutive times
        2. Same step title repeated 3 times
        3. Planner why field >85% similar for 2+ consecutive steps
        4. Claude returns "not enough info" 2+ times

        Args:
            session: Current session state

        Returns:
            True if no progress is detected
        """
        if len(session.history) < 3:
            return False

        recent = session.history[-3:]

        # Check 1: Same file repeated 3 times
        files_touched = []
        for step in recent:
            files_touched.extend(step.files_touched)

        if files_touched:
            file_counts = Counter(files_touched)
            if any(count >= 3 for count in file_counts.values()):
                file = next(f for f, c in file_counts.items() if c >= 3)
                logger.info(f"No progress: same file {file} touched 3+ times")
                return True

        # Check 2: Same step intent repeated
        titles = [self._normalize_title(s.step_title) for s in recent]
        if len(set(titles)) == 1:  # All 3 are the same
            logger.info(f"No progress: same step title '{titles[0]}' repeated 3 times")
            return True

        # Check for similar titles (edit distance or simple containment)
        if self._titles_too_similar(titles):
            logger.info(f"No progress: step titles too similar: {titles}")
            return True

        # Check 3: Planner produces near-duplicate why/step_title
        if len(session.history) >= 2:
            for i in range(len(session.history) - 1):
                step1 = session.history[i]
                step2 = session.history[i + 1]
                similarity = self._similarity_score(
                    step1.planner_interpretation,
                    step2.planner_interpretation
                )
                if similarity > 0.85:
                    logger.info(f"No progress: planner why fields {similarity:.1%} similar")
                    return True

        # Check 4: Claude returns "not enough info" repeatedly
        not_enough_count = sum(
            1 for s in recent
            if self._is_not_enough_info_response(s.claude_output)
        )
        if not_enough_count >= 2:
            logger.info(f"No progress: 'not enough info' responses {not_enough_count} times")
            return True

        # Check 5: Empty or uninformative responses
        empty_response_count = sum(
            1 for s in recent
            if len(s.claude_output.strip()) < 50 or self._is_empty_response(s.claude_output)
        )
        if empty_response_count >= 2:
            logger.info(f"No progress: empty/uninformative responses {empty_response_count} times")
            return True

        # Check 6: Same error or finding repeated
        if self._same_finding_repeated(session.history[-5:]):
            logger.info("No progress: same finding/error repeated")
            return True

        return False

    def _normalize_title(self, title: str) -> str:
        """Normalize step title for comparison."""
        return title.lower().strip()

    def _similarity_score(self, text1: str, text2: str) -> float:
        """
        Calculate simple similarity score between two texts.

        Uses Jaccard similarity on word sets.

        Args:
            text1: First text
            text2: Second text

        Returns:
            Similarity score from 0.0 to 1.0
        """
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2

        return len(intersection) / len(union)

    def _titles_too_similar(self, titles: list[str]) -> bool:
        """
        Check if step titles are too similar to each other.

        Args:
            titles: List of normalized titles

        Returns:
            True if titles are too similar
        """
        if len(titles) < 2:
            return False

        # Check if all titles contain a common core phrase
        words_in_all = None

        for title in titles:
            words = set(title.split())
            if words_in_all is None:
                words_in_all = words
            else:
                words_in_all &= words

        # If there are 3+ words in common, titles are too similar
        if words_in_all and len(words_in_all) >= 3:
            return True

        # Check pairwise similarity
        for i in range(len(titles) - 1):
            if self._similarity_score(titles[i], titles[i + 1]) > 0.75:
                return True

        return False

    def _is_not_enough_info_response(self, text: str) -> bool:
        """
        Check if Claude's response indicates insufficient information.

        Args:
            text: Claude's response text

        Returns:
            True if response indicates not enough info
        """
        if not text:
            return True

        text_lower = text.lower()

        not_enough_phrases = [
            "not enough information",
            "insufficient information",
            "don't have enough context",
            "need more information",
            "cannot determine",
            "unable to determine",
            "insufficient data",
            "more context needed"
        ]

        return any(phrase in text_lower for phrase in not_enough_phrases)

    def _is_empty_response(self, text: str) -> bool:
        """
        Check if Claude's response is essentially empty.

        Args:
            text: Claude's response text

        Returns:
            True if response is empty or uninformative
        """
        if not text or len(text.strip()) < 30:
            return True

        text_lower = text.strip().lower()

        # Check for generic "I understand" or empty acknowledgments
        empty_phrases = [
            "i understand",
            "please provide",
            "let me know",
            "i'll wait for",
            "acknowledged",
            "understood",
            "proceeding"
        ]

        text_stripped = text_lower.replace(" ", "").replace("\n", "")
        if len(text_stripped) < 50:
            return True

        return False

    def _same_finding_repeated(self, steps: list) -> bool:
        """
        Check if the same finding or error is being reported repeatedly.

        Args:
            steps: List of recent steps

        Returns:
            True if same finding is repeated
        """
        if len(steps) < 2:
            return False

        # Extract the "findings" or main message from each step
        findings = []
        for step in steps:
            # Look for patterns like "FINDING:", "ERROR:", "FOUND:"
            output = step.claude_output.lower()

            # Extract the first substantial line after markers
            lines = output.split('\n')
            for line in lines:
                line = line.strip()
                if any(keyword in line for keyword in ['finding:', 'error:', 'issue:', 'problem:']):
                    findings.append(line)
                    break
            else:
                # Use first non-empty line as fallback
                for line in lines:
                    if line.strip() and len(line.strip()) > 20:
                        findings.append(line.strip()[:100])
                        break

        if len(findings) < 2:
            return False

        # Check for similarity between consecutive findings
        for i in range(len(findings) - 1):
            if findings[i] and findings[i + 1]:
                if findings[i] == findings[i + 1]:
                    return True
                if self._similarity_score(findings[i], findings[i + 1]) > 0.9:
                    return True

        return False

    def check_step_limit_approaching(self, session: Session, warning_threshold: float = 0.8) -> bool:
        """
        Check if we're approaching the step limit.

        Args:
            session: Current session
            warning_threshold: Fraction of max_steps to warn at (default 0.8 = 80%)

        Returns:
            Always False because step-limit stopping is disabled.
        """
        return False

    def get_steps_remaining(self, session: Session) -> int:
        """Get the number of steps remaining (0 when no hard limit is enforced)."""
        return 0


# Global detector instance
_detector: Optional[StopDetector] = None


def get_detector() -> StopDetector:
    """Get the global stop detector instance."""
    global _detector
    if _detector is None:
        _detector = StopDetector()
    return _detector
