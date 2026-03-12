"""
Pydantic schema definitions for the Bot Orchestrator.

This module defines all data models used throughout the orchestrator system.
"""
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class Mode(str, Enum):
    """Execution modes for the orchestrator."""
    INVESTIGATE = "investigate"
    PROPOSE_FIX = "propose_fix"
    VALIDATE_FIX = "validate_fix"
    APPROVED_EXECUTE = "approved_execute"


class SessionStatus(str, Enum):
    """Status states for a session."""
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    PAUSED = "paused"
    STALLED = "stalled"
    AWAITING_APPROVAL = "awaiting_approval"


class ApprovalStatus(str, Enum):
    """Status states for approval workflow."""
    NONE = "none"
    REQUESTED = "requested"
    GRANTED = "granted"
    DENIED = "denied"


class ApprovalRequestType(str, Enum):
    """Types of actions requiring approval."""
    CODE_WRITE = "code_write"
    MIGRATION = "migration"
    RESTART = "restart"
    DB_UPDATE = "db_update"
    SECRET_CHANGE = "secret_change"


class PlannerResponse(BaseModel):
    """
    Strict JSON response from ChatGPT.

    Control fields are for orchestrator only and never sent to Claude.
    Only prompt_for_claude is sent to the tmux target.
    """
    # Control fields (for orchestrator only, never sent to Claude)
    status: Literal["continue", "resolved", "blocked", "needs_human_action", "stalled"] = Field(
        description="Controls orchestrator flow"
    )
    step_number: int = Field(description="Current step counter", ge=1)
    step_title: str = Field(description="Human-readable step description")
    why: str = Field(description="Why this step is necessary")
    success_criteria: str = Field(description="How to validate completion")
    done_signal: bool = Field(description="Is the investigation complete?")
    resolution_summary: Optional[str] = Field(default=None, description="Final summary when done")
    human_action: Optional[str] = Field(default=None, description="What human needs to do")

    # The exact payload to send to Claude (this and only this goes to tmux)
    prompt_for_claude: str = Field(
        description="Exact text to send to Claude CLI"
    )

    # Optional: requested approval with reason
    approval_requested: Optional[str] = Field(
        default=None,
        description="Reason approval is needed for this step"
    )

    @field_validator('prompt_for_claude')
    @classmethod
    def validate_prompt_for_claude(cls, v, info):
        """Validate that prompt_for_claude is non-empty when continuing."""
        if info.data.get('status') == 'continue' and not v.strip():
            raise ValueError("prompt_for_claude cannot be empty when status is 'continue'")
        return v

    @field_validator('resolution_summary', 'human_action')
    @classmethod
    def validate_completion_fields(cls, v, info):
        """Validate completion fields are set when done."""
        if info.data.get('done_signal') and info.data.get('status') in ('resolved', 'blocked'):
            if not v:
                field_name = 'resolution_summary' if 'resolution_summary' in info.field_names else 'human_action'
                raise ValueError(f"{field_name} must be set when done_signal is True and status is {info.data.get('status')}")
        return v


class StepHistory(BaseModel):
    """Record of a single step in the investigation."""
    step_number: int
    planner_status: str
    step_title: str
    prompt_for_claude: str
    claude_output: str
    claude_summary: Optional[str] = Field(default=None, description="Extracted summary for planner context")
    planner_interpretation: str
    timestamp: datetime
    files_touched: List[str] = Field(default_factory=list, description="Track files mentioned for no-progress detection")


class ApprovalState(BaseModel):
    """Human approval workflow state."""
    status: ApprovalStatus = Field(default=ApprovalStatus.NONE)
    request_type: Optional[ApprovalRequestType] = Field(default=None, description="Type of action requiring approval")
    reason: Optional[str] = Field(default=None, description="Reason for approval request")
    approved_commands: List[str] = Field(default_factory=list, description="Commands that were approved")
    denied_reason: Optional[str] = Field(default=None, description="Reason approval was denied")
    requested_at: Optional[datetime] = Field(default=None)
    decided_at: Optional[datetime] = Field(default=None)

    @field_validator('status')
    @classmethod
    def validate_approval_state(cls, v, info):
        """Validate approval state consistency."""
        if v == ApprovalStatus.GRANTED and not info.data.get('decided_at'):
            # Allow validation to pass if not yet decided
            pass
        if v == ApprovalStatus.DENIED and not info.data.get('denied_reason'):
            # Denies should have a reason
            pass
        return v


class Session(BaseModel):
    """Main session state."""
    session_id: str

    # Issue seed fields (full context for ChatGPT)
    title: str = Field(description="Issue title")
    issue_description: str = Field(description="Full problem description")
    issue_seed_prompt: str = Field(description="Original prompt/ask from operator")
    operator_constraints: Optional[str] = Field(default=None, description="Operator-specified constraints")
    project_context: Optional[str] = Field(default=None, description="System/project context")

    # Session state
    status: SessionStatus = Field(default=SessionStatus.RUNNING)
    mode: Mode = Field(default=Mode.INVESTIGATE)
    project: str = Field(description="Project name/identifier")
    tmux_target: str = Field(description="Tmux target in format 'session:window.pane'")

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # Step control
    step_count: int = Field(default=0, ge=0)
    max_steps: int = Field(default=30, ge=1, le=100)

    # History and approval
    history: List[StepHistory] = Field(default_factory=list)
    approval: ApprovalState = Field(default_factory=ApprovalState)

    # Outcomes
    resolution_summary: Optional[str] = Field(default=None)
    stall_reason: Optional[str] = Field(default=None)

    # Recovery state
    last_checkpoint: Optional[datetime] = Field(default=None)
    recovery_attempts: int = Field(default=0, ge=0)

    @field_validator('tmux_target')
    @classmethod
    def validate_tmux_target(cls, v):
        """Validate tmux target format."""
        parts = v.split(':')
        if len(parts) != 2:
            raise ValueError("tmux_target must be in format 'session:window.pane'")
        session_name, window_pane = parts
        if '.' not in window_pane:
            raise ValueError("tmux_target must include pane identifier (e.g., 'session:0.0')")
        return v

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v):
        """Validate session ID format."""
        if not v or not v.startswith('issue_'):
            raise ValueError("session_id must start with 'issue_'")
        return v


class PlannerInput(BaseModel):
    """
    Exact contract for what gets sent to ChatGPT.

    This represents the complete input structure for the planner.
    """
    issue_title: str
    issue_description: str
    project_context: Optional[str] = None
    operator_constraints: Optional[str] = None
    current_mode: Mode
    approved_capabilities: List[str] = Field(default_factory=list)

    # Latest step context
    latest_step_number: int = Field(default=0, ge=0)
    latest_claude_output: Optional[str] = None
    latest_claude_summary: Optional[str] = None

    # Session state
    history_summary: List[Dict[str, Any]] = Field(default_factory=list)
    step_count: int = Field(default=0, ge=0)
    max_steps: int = Field(default=30, ge=1)

    # Current approval state
    current_approval_status: Optional[str] = None


class TmuxCaptureResult(BaseModel):
    """Result of a tmux capture operation."""
    raw_output: str
    cleaned_output: str
    extracted_response: Optional[str] = None
    has_start_marker: bool = False
    has_end_marker: bool = False
    is_complete: bool = False


class RecoveryAction(BaseModel):
    """Recovery action to take."""
    action: Literal["retry", "reattach", "restart_claude", "pause", "block"]
    reason: str
    can_proceed: bool


class PartialCapture(BaseModel):
    """Partial Claude output capture saved for manual review."""
    session_id: str
    step_number: int
    captured_at: datetime
    partial_output: str
    is_complete: bool = False
