"""
OpenAI client for communicating with ChatGPT as the planner.

Handles JSON schema enforcement, retry logic, and response validation.
"""
import json
import logging
from typing import Optional
from datetime import datetime

try:
    from openai import OpenAI, APIError, APIConnectionError, RateLimitError
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None
    APIError = None
    APIConnectionError = None
    RateLimitError = None

from pydantic import ValidationError

from config import get_settings
from schema import PlannerResponse, PlannerInput

logger = logging.getLogger(__name__)


class InvalidPlannerResponse(Exception):
    """Raised when ChatGPT returns invalid JSON or doesn't match schema."""
    pass


class PlannerAPIClient:
    """
    Client for interacting with ChatGPT as the planner.

    Enforces JSON output format with strict schema validation.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        """
        Initialize the OpenAI client.

        Args:
            api_key: OpenAI API key (uses config if None)
            model: Model to use (uses config if None)
        """
        settings = get_settings()
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.planner_model

        if not self.api_key:
            logger.warning("OpenAI API key not set - client will fail when used")

        if OPENAI_AVAILABLE and self.api_key:
            self.client = OpenAI(api_key=self.api_key)
        else:
            self.client = None

    def _load_system_prompt(self) -> str:
        """Load the system prompt from file."""
        settings = get_settings()
        prompt_path = settings.get_prompts_path() / "planner_system.txt"

        if prompt_path.exists():
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read().strip()

        # Default system prompt if file not found
        return self._default_system_prompt()

    def _default_system_prompt(self) -> str:
        """Default system prompt for the planner."""
        return """You are the planning brain of a strict issue orchestrator.

Your role:
- Analyze the issue and latest Claude output
- Decide exactly one next investigation or validation step
- Keep the process one-step-at-a-time
- Prefer read-only investigation unless explicitly in propose-fix or validate-fix mode
- Stop only when the issue is clearly resolved, blocked, or needs human action

Rules:
- Return valid JSON only matching the provided schema
- Never return more than one step
- prompt_for_claude must contain ONLY the exact content to send to Claude CLI
- Do not ask Claude to do multiple unrelated things
- Do not ask Claude to modify code unless mode explicitly allows it
- Do not use markdown fences around the JSON
- If the issue is solved, return status=resolved
- If human approval is needed for risky actions, return status=needs_human_action and fill approval_requested
- If no progress is being made (same checks, repetitive findings), return status=stalled
- You have access to full issue description, project context, and operator constraints

Available capabilities by mode:
- investigate: read_files, grep_code, inspect_logs only
- propose_fix: above + suggest_patch (no execution)
- validate_fix: read_files + verify_fix (confirm fix correctness)
- approved_execute: execute only pre-approved commands

When responding:
1. Set status to continue, resolved, blocked, needs_human_action, or stalled
2. Provide a clear step_title describing the single next action
3. Explain why this step is necessary in the why field
4. Put the EXACT prompt for Claude in prompt_for_claude (this is what gets sent)
5. Describe success criteria for this step
6. If done, set done_signal=true and provide resolution_summary"""

    def _build_json_schema(self) -> dict:
        """Build the JSON schema for ChatGPT's response."""
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["continue", "resolved", "blocked", "needs_human_action", "stalled"]
                },
                "step_number": {"type": "integer", "minimum": 1},
                "step_title": {"type": "string"},
                "why": {"type": "string"},
                "success_criteria": {"type": "string"},
                "done_signal": {"type": "boolean"},
                "resolution_summary": {"type": "string"},
                "human_action": {"type": "string"},
                "prompt_for_claude": {"type": "string"},
                "approval_requested": {"type": "string"}
            },
            "required": ["status", "step_number", "step_title", "why", "success_criteria", "done_signal", "prompt_for_claude"],
            "additionalProperties": False
        }

    def _build_user_message(self, input_data: PlannerInput) -> str:
        """Build the user message from the input data."""
        parts = [
            f"# Issue: {input_data.issue_title}\n",
            f"## Description\n{input_data.issue_description}\n"
        ]

        if input_data.project_context:
            parts.append(f"## Project Context\n{input_data.project_context}\n")

        if input_data.operator_constraints:
            parts.append(f"## Operator Constraints\n{input_data.operator_constraints}\n")

        parts.append(f"## Current Mode\n{input_data.current_mode}\n")
        parts.append(f"## Approved Capabilities\n{', '.join(input_data.approved_capabilities) or 'None'}\n")

        if input_data.latest_claude_output:
            parts.append("## Latest Claude Output\n")
            # Include summary if available, otherwise truncate
            if input_data.latest_claude_summary:
                parts.append(input_data.latest_claude_summary)
            else:
                output = input_data.latest_claude_output
                if len(output) > 2000:
                    output = output[:2000] + "\n...(truncated)"
                parts.append(output)
            parts.append("\n")

        if input_data.history_summary:
            parts.append("## Recent History\n")
            for item in input_data.history_summary[-5:]:  # Last 5 entries
                parts.append(f"- Step {item.get('step', '?')}: {item.get('title', 'Unknown')}")
                if item.get('summary'):
                    summary = item['summary']
                    if len(summary) > 100:
                        summary = summary[:100] + "..."
                    parts.append(f"  ({summary})")
                if item.get('files'):
                    parts.append(f"  Files: {', '.join(item['files'])}")
            parts.append("\n")

        parts.append(f"## Progress\nStep {input_data.step_count} of {input_data.max_steps}\n")

        if input_data.current_approval_status:
            parts.append(f"## Approval Status\n{input_data.current_approval_status}\n")

        parts.append("\nPlease determine the next step and respond with JSON only.")

        return "\n".join(parts)

    def get_planner_response(
        self,
        input_data: PlannerInput,
        max_retries: int = 3,
        use_strong_schema: bool = False
    ) -> PlannerResponse:
        """
        Get the next step plan from ChatGPT.

        Args:
            input_data: The planner input containing issue context
            max_retries: Maximum retry attempts on failure
            use_strong_schema: If True, use stronger schema enforcement

        Returns:
            PlannerResponse with the next step

        Raises:
            InvalidPlannerResponse: If response doesn't match schema after retries
            APIError: If the API call fails
        """
        if not OPENAI_AVAILABLE:
            raise InvalidPlannerResponse("OpenAI package not installed. Use web mode instead or: pip install openai")

        if not self.client:
            raise InvalidPlannerResponse("OpenAI client not initialized - check API key")

        system_prompt = self._load_system_prompt()
        user_message = self._build_user_message(input_data)

        # Add schema instructions for stronger enforcement
        if use_strong_schema:
            schema_instructions = "\n\nCRITICAL: You must respond with valid JSON only. No markdown fences, no explanations outside the JSON. The JSON must match this exact schema:\n"
            schema_instructions += json.dumps(self._build_json_schema(), indent=2)
            user_message += schema_instructions

        last_error = None

        for attempt in range(max_retries):
            try:
                logger.debug(f"Calling OpenAI API (attempt {attempt + 1}/{max_retries})")

                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.7,
                    max_tokens=2000
                )

                content = response.choices[0].message.content
                logger.debug(f"Received response: {content[:200]}...")

                # Parse JSON response
                try:
                    response_data = json.loads(content)
                except json.JSONDecodeError as e:
                    # Try to extract JSON from markdown fences
                    if "```json" in content:
                        start = content.find("```json") + 7
                        end = content.find("```", start)
                        if end != -1:
                            try:
                                response_data = json.loads(content[start:end].strip())
                            except json.JSONDecodeError:
                                raise InvalidPlannerResponse(f"Failed to parse JSON: {e}")
                    elif "```" in content:
                        start = content.find("```") + 3
                        end = content.find("```", start)
                        if end != -1:
                            try:
                                response_data = json.loads(content[start:end].strip())
                            except json.JSONDecodeError:
                                raise InvalidPlannerResponse(f"Failed to parse JSON: {e}")
                    else:
                        raise InvalidPlannerResponse(f"Failed to parse JSON: {e}")

                # Validate against schema
                try:
                    planner_response = PlannerResponse(**response_data)
                    logger.info(f"Valid planner response: step {planner_response.step_number}, status={planner_response.status}")
                    return planner_response

                except ValidationError as e:
                    logger.warning(f"Schema validation failed: {e}")
                    raise InvalidPlannerResponse(f"Response doesn't match schema: {e}")

            except RateLimitError as e:
                wait_time = 2 ** attempt
                logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                import time
                time.sleep(wait_time)
                last_error = e

            except APIConnectionError as e:
                logger.warning(f"API connection error: {e}")
                last_error = e

            except APIError as e:
                logger.error(f"OpenAI API error: {e}")
                if attempt == max_retries - 1:
                    raise
                last_error = e

            except InvalidPlannerResponse as e:
                logger.warning(f"Invalid response (attempt {attempt + 1}): {e}")
                if use_strong_schema and attempt == max_retries - 1:
                    # Already using strong schema and still failing
                    raise
                # Retry with stronger schema enforcement
                use_strong_schema = True
                last_error = e

        # All retries exhausted
        raise InvalidPlannerResponse(f"Failed after {max_retries} attempts. Last error: {last_error}")


def get_planner_response(input_data: PlannerInput) -> PlannerResponse:
    """
    Convenience function to get a planner response.

    Args:
        input_data: The planner input

    Returns:
        PlannerResponse with the next step
    """
    settings = get_settings()
    client = PlannerAPIClient(api_key=settings.openai_api_key, model=settings.planner_model)
    return client.get_planner_response(input_data)
