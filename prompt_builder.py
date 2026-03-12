"""
Prompt builder for constructing prompts for both the planner and Claude.

Handles prompt templates and mode-specific prompt construction.
"""
from pathlib import Path
from typing import Optional

from schema import Mode, Session, PlannerInput
from config import get_settings
from tmux_parser import START_MARKER, END_MARKER


class PromptBuilder:
    """Builds prompts for both planner and Claude interactions."""

    def __init__(self):
        """Initialize the prompt builder."""
        self.settings = get_settings()
        self.prompts_dir = self.settings.get_prompts_path()

    def _load_template(self, name: str) -> str:
        """
        Load a prompt template from file.

        Args:
            name: Template name (without .txt extension)

        Returns:
            Template content, or default if file not found
        """
        template_path = self.prompts_dir / f"{name}.txt"

        if template_path.exists():
            with open(template_path, 'r', encoding='utf-8') as f:
                return f.read()

        # Return default template
        return self._get_default_template(name)

    def _get_default_template(self, name: str) -> str:
        """Get default template when file is not found."""
        defaults = {
            "claude_investigate": self._default_claude_investigate_template(),
            "claude_propose_fix": self._default_claude_propose_fix_template(),
            "claude_validate_fix": self._default_claude_validate_fix_template(),
            "claude_approved_execute": self._default_claude_approved_execute_template(),
        }
        return defaults.get(name, "")

    def build_claude_prompt(
        self,
        step_prompt: str,
        issue_title: str,
        mode: Mode,
        context: Optional[str] = None
    ) -> str:
        """
        Build a prompt for Claude CLI based on the current mode.

        Args:
            step_prompt: The specific step/prompt from the planner
            issue_title: The issue being investigated
            mode: Current execution mode
            context: Optional additional context

        Returns:
            Complete prompt to send to Claude
        """
        template_name = f"claude_{mode.value}"
        template = self._load_template(template_name)

        prompt = template.format(
            issue=issue_title,
            step=step_prompt,
            context=context or "",
            start_marker=START_MARKER,
            end_marker=END_MARKER
        )

        return prompt.strip()

    def _default_claude_investigate_template(self) -> str:
        """Default Claude prompt template for investigate mode."""
        return """You are assisting in a controlled investigation loop.

Current issue:
{issue}

{context}

Current step:
{step}

Instructions:
- Do only this step
- Do not modify code
- Use read-only inspection only
- Search the relevant files, handlers, routes, workers, logs, and mappings
- Be precise and concise
- Return evidence-based findings only

Return in this structure:

{start_marker}
SUMMARY:
[1-2 sentence summary of findings]

FILES:
- path: /path/to/file.ext
- function: function_name

FLOW:
1. Step one
2. Step two

FINDINGS:
- Evidence-based finding 1
- Evidence-based finding 2

LIKELY_BREAKPOINTS:
- Potential issue location 1
- Potential issue location 2

RECOMMENDED_NEXT_CHECK:
- Specific next action to verify
{end_marker}"""

    def _default_claude_propose_fix_template(self) -> str:
        """Default Claude prompt template for propose_fix mode."""
        return """You are assisting in a controlled investigation loop - PROPOSE FIX mode.

Current issue:
{issue}

{context}

Current step:
{step}

Instructions:
- Analyze the issue and propose a fix
- DO NOT write or modify any code
- Suggest changes with specific file paths and line numbers
- Explain why the proposed fix would resolve the issue
- Consider edge cases and potential side effects

Return in this structure:

{start_marker}
SUMMARY:
[Brief summary of the proposed fix]

AFFECTED_FILES:
- path: /path/to/file.ext
- changes: Description of changes needed

PROPOSED_FIX:
1. File: /path/to/file.ext
   Line: XX
   Current: [current code]
   Proposed: [proposed code]
   Reason: [why this change]

2. [Additional changes...]

VALIDATION:
- How to verify the fix works
- What to test
- Expected outcome

RISK_ASSESSMENT:
- Potential side effects
- Breaking changes
- Dependencies affected

RECOMMENDED_NEXT_CHECK:
- Specific next action to verify the fix approach
{end_marker}"""

    def _default_claude_validate_fix_template(self) -> str:
        """Default Claude prompt template for validate_fix mode."""
        return """You are assisting in a controlled investigation loop - VALIDATE FIX mode.

Current issue:
{issue}

{context}

Current step:
{step}

Instructions:
- Verify that a fix is correct and complete
- Check for edge cases and potential issues
- Confirm the fix addresses the root cause
- Use read-only inspection only
- Do not modify code

Return in this structure:

{start_marker}
SUMMARY:
[Assessment of whether the fix is correct]

FIX_VERIFICATION:
- Root cause addressed: Yes/No
- Complete: Yes/No/Partial
- Safe: Yes/No

CHECKS_PERFORMED:
- Check 1: [result]
- Check 2: [result]

EDGE_CASES_CONSIDERED:
- Edge case 1: [status]
- Edge case 2: [status]

POTENTIAL_ISSUES:
- Any concerns found
- Recommended additional testing

FINAL_ASSESSMENT:
- Ready to deploy: Yes/No
- Additional work needed: [description]
{end_marker}"""

    def _default_claude_approved_execute_template(self) -> str:
        """Default Claude prompt template for approved_execute mode."""
        return """You are assisting in a controlled investigation loop - APPROVED EXECUTE mode.

Current issue:
{issue}

{context}

Current step:
{step}

Instructions:
- Execute the approved action only
- Stay within the approved scope
- Report exactly what was done
- If the action would exceed approved scope, stop and report

Return in this structure:

{start_marker}
SUMMARY:
[What was executed]

ACTIONS_TAKEN:
1. Action 1: [description]
   Result: [outcome]

2. Action 2: [description]
   Result: [outcome]

FILES_MODIFIED:
- path: /path/to/file.ext (brief description of change)

VERIFICATION:
- How the changes were verified
- Current status

NEXT_STEPS:
- Recommended next actions
{end_marker}"""

    def build_planner_input(self, session: Session, latest_claude_summary: Optional[str] = None) -> PlannerInput:
        """
        Build the complete input for ChatGPT planner.

        Args:
            session: Current session state
            latest_claude_summary: Optional pre-computed summary

        Returns:
            PlannerInput ready for the OpenAI client
        """
        # Summarize history to avoid token bloat
        history_summary = []
        for step in session.history[-self.settings.history_summary_length:]:
            summary_entry = {
                "step": step.step_number,
                "title": step.step_title,
                "summary": step.claude_summary or (step.claude_output[:200] if step.claude_output else ""),
            }
            if step.files_touched:
                summary_entry["files"] = step.files_touched[:5]  # Limit files
            history_summary.append(summary_entry)

        # Get approved capabilities based on mode
        approved_capabilities = self._get_approved_capabilities(session)

        # Get latest Claude output
        latest_claude_output = None
        if session.history:
            latest_step = session.history[-1]
            latest_claude_output = latest_claude_summary or latest_step.claude_summary or latest_step.claude_output

        return PlannerInput(
            issue_title=session.title,
            issue_description=session.issue_description,
            project_context=session.project_context,
            operator_constraints=session.operator_constraints,
            current_mode=session.mode,
            approved_capabilities=approved_capabilities,
            latest_step_number=session.step_count,
            latest_claude_output=latest_claude_output,
            latest_claude_summary=latest_claude_summary,
            history_summary=history_summary,
            step_count=session.step_count,
            max_steps=session.max_steps,
            current_approval_status=session.approval.status.value if session.approval else None
        )

    def _get_approved_capabilities(self, session: Session) -> list[str]:
        """
        Return capabilities approved for current mode.

        Args:
            session: Current session

        Returns:
            List of approved capability names
        """
        base_capabilities = ["read_files", "grep_code", "inspect_logs"]

        if session.mode == Mode.INVESTIGATE:
            return base_capabilities

        elif session.mode == Mode.PROPOSE_FIX:
            return base_capabilities + ["suggest_patch", "analyze_code"]

        elif session.mode == Mode.VALIDATE_FIX:
            return base_capabilities + ["verify_fix", "check_syntax"]

        elif session.mode == Mode.APPROVED_EXECUTE:
            # Include any commands that were specifically approved
            approved = base_capabilities + session.approval.approved_commands
            return approved

        return base_capabilities

    def build_issue_summary(self, session: Session) -> str:
        """
        Build a human-readable summary of the issue and progress.

        Args:
            session: Current session

        Returns:
            Formatted summary string
        """
        lines = [
            f"# {session.title}",
            "",
            f"**Description:** {session.issue_description}",
            "",
            f"**Status:** {session.status.value}",
            f"**Mode:** {session.mode.value}",
            f"**Progress:** Step {session.step_count} of {session.max_steps}",
            "",
        ]

        if session.project_context:
            lines.extend([
                f"**Project Context:** {session.project_context}",
                "",
            ])

        if session.operator_constraints:
            lines.extend([
                f"**Constraints:** {session.operator_constraints}",
                "",
            ])

        if session.history:
            lines.extend([
                "**Recent Steps:**",
                ""
            ])
            for step in session.history[-5:]:
                lines.append(f"- Step {step.step_number}: {step.step_title}")
                if step.files_touched:
                    lines.append(f"  Files: {', '.join(step.files_touched[:3])}")

        if session.resolution_summary:
            lines.extend([
                "",
                "**Resolution:**",
                session.resolution_summary
            ])

        return "\n".join(lines)


# Global builder instance
_builder: Optional[PromptBuilder] = None


def get_builder() -> PromptBuilder:
    """Get the global prompt builder instance."""
    global _builder
    if _builder is None:
        _builder = PromptBuilder()
    return _builder


def build_planner_input(session: Session, latest_claude_summary: Optional[str] = None) -> PlannerInput:
    """Convenience function to build planner input."""
    return get_builder().build_planner_input(session, latest_claude_summary)


def build_claude_prompt(step_prompt: str, issue_title: str, mode: Mode, context: Optional[str] = None) -> str:
    """Convenience function to build Claude prompt."""
    return get_builder().build_claude_prompt(step_prompt, issue_title, mode, context)
