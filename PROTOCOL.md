# Bot Orchestrator Protocol Specification

## Overview

This protocol defines the strict investigation loop between:
- **ChatGPT** (Planner/Reviewer)
- **Claude CLI** (Executor/Codebase Investigator)
- **Human Operator** (Approval/Override)

The orchestrator enforces one-step-at-a-time execution with reliable response capture and explicit stop conditions.

---

## 1. ChatGPT JSON Response Schema

### PlannerResponse

```json
{
  "status": "continue | resolved | blocked | needs_human_action | stalled",
  "step_number": 1,
  "step_title": "Single clear action title",
  "why": "Why this step is necessary",
  "success_criteria": "How to know this step succeeded",
  "done_signal": false,
  "resolution_summary": null,
  "human_action": null,
  "prompt_for_claude": "Exact text to send to Claude CLI",
  "approval_requested": null
}
```

**Field Descriptions:**

| Field | Type | Required | Status |
|-------|------|----------|--------|
| `status` | string | Yes | Controls orchestrator flow |
| `step_number` | int | Yes | Current step counter |
| `step_title` | string | Yes | Human-readable step description |
| `why` | string | Yes | Rationale for this step |
| `success_criteria` | string | Yes | How to validate completion |
| `done_signal` | bool | Yes | Is the investigation complete? |
| `resolution_summary` | string\|null | No | Final summary when done |
| `human_action` | string\|null | No | What human needs to do |
| `prompt_for_claude` | string | Yes | **ONLY this goes to Claude** |
| `approval_requested` | string\|null | No | Reason approval needed |

**Status Values:**

| Status | Meaning |
|--------|---------|
| `continue` | Execute step and continue loop |
| `resolved` | Issue solved, stop and summarize |
| `blocked` | Cannot proceed, missing prerequisites |
| `needs_human_action` | Requires human intervention |
| `stalled` | No progress detected, abort |

**Critical Rule:** Only `prompt_for_claude` content is sent to Claude CLI. All other fields are control-only.

---

## 2. Claude Response Markers

All Claude CLI responses must be wrapped in markers:

```
<<<CLAUDE_RESPONSE_START>>>
[Actual Claude response content here]
<<<CLAUDE_RESPONSE_END>>>
```

**Marker Rules:**

1. Markers must be on their own lines
2. Start marker opens the response block
3. End marker closes the response block
4. If multiple blocks exist, only the **last complete block** is captured
5. If end marker is missing, capture fails (incomplete response)

**Response Structure (Recommended):**

```
<<<CLAUDE_RESPONSE_START>>>
SUMMARY:
[Brief 1-2 sentence summary]

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
<<<CLAUDE_RESPONSE_END>>>
```

---

## 3. Session JSON Structure

```json
{
  "session_id": "issue_2026_03_11_001",
  "title": "Issue title",
  "issue_description": "Full problem description",
  "issue_seed_prompt": "Original prompt from operator",
  "operator_constraints": "Any constraints specified by operator",
  "project_context": "System/project context information",
  "status": "running | completed | blocked | paused | stalled | awaiting_approval",
  "mode": "investigate | propose_fix | validate_fix | approved_execute",
  "project": "project-name",
  "tmux_target": "session:window.pane",
  "created_at": "2026-03-11T10:00:00",
  "updated_at": "2026-03-11T10:00:00",
  "step_count": 0,
  "max_steps": 30,
  "history": [
    {
      "step_number": 1,
      "planner_status": "continue",
      "step_title": "Step title",
      "prompt_for_claude": "Prompt sent to Claude",
      "claude_output": "Full captured response",
      "claude_summary": "Extracted summary for context",
      "planner_interpretation": "Why this step was taken",
      "timestamp": "2026-03-11T10:01:00",
      "files_touched": ["file1.py", "file2.py"]
    }
  ],
  "approval": {
    "status": "none | requested | granted | denied",
    "request_type": "code_write | migration | restart | db_update | secret_change",
    "reason": "Reason for approval request",
    "approved_commands": ["command1", "command2"],
    "denied_reason": null,
    "requested_at": null,
    "decided_at": null
  },
  "resolution_summary": null,
  "stall_reason": null,
  "last_checkpoint": "2026-03-11T10:00:00",
  "recovery_attempts": 0
}
```

---

## 4. Stop Conditions

The orchestrator stops when ANY of these conditions are met:

### Explicit Stop Signals

| Condition | Description |
|-----------|-------------|
| `status == "resolved"` | Planner confirms issue solved |
| `status == "blocked"` | Cannot proceed (missing access/logs) |
| `status == "needs_human_action"` | Human intervention required |
| `status == "stalled"` | No progress detected |

### Automatic Stop Conditions

| Condition | Trigger |
|-----------|---------|
| Max steps | `step_count >= max_steps` |
| No progress | Repeated patterns detected (see below) |

### No-Progress Detection Heuristics

The system detects no progress when ANY of these occur:

1. **Same file repeated**: Same file touched 3+ consecutive times
2. **Same step title**: Identical normalized step titles 3 times
3. **Similar rationale**: Planner `why` field >85% similar for 2+ steps
4. **Empty responses**: Claude returns "not enough info" 2+ times

---

## 5. Allowed Modes

| Mode | Capabilities | Description |
|------|--------------|-------------|
| `investigate` | read_files, grep_code, inspect_logs | Read-only inspection |
| `propose_fix` | + suggest_patch | Suggest without applying |
| `validate_fix` | + verify_fix | Confirm fix correctness |
| `approved_execute` | + pre-approved commands | Execute with approval |

### Mode Transition Rules

- Default starts in `investigate` mode
- `investigate` → `propose_fix`: Planner proposes fix
- `propose_fix` → `validate_fix`: Fix approved, needs validation
- `validate_fix` → `investigate`: Validation failed, continue digging
- Any mode → `approved_execute`: Requires explicit human approval

---

## 6. Planner Input Contract

**Exact fields sent to ChatGPT:**

```json
{
  "issue_title": "Issue title",
  "issue_description": "Full problem description",
  "project_context": "System/project context",
  "operator_constraints": "Operator constraints",
  "current_mode": "investigate",
  "approved_capabilities": ["read_files", "grep_code", "inspect_logs"],
  "latest_step_number": 5,
  "latest_claude_output": "Last captured Claude response",
  "latest_claude_summary": "Condensed summary for context",
  "history_summary": [
    {
      "step": 3,
      "title": "Step 3 title",
      "summary": "Summary of findings",
      "files": ["file1.py"]
    }
  ],
  "step_count": 5,
  "max_steps": 30,
  "current_approval_status": "granted"
}
```

**History Limit:** Last 5 steps sent to avoid token bloat.

---

## 7. Approval Workflow Contract

### Approval State Machine

```
none → requested → granted → none
                   ↘ denied → blocked
```

### Request Types

| Type | Description | Requires |
|------|-------------|----------|
| `code_write` | Writing or editing files | List of files |
| `migration` | Database schema changes | Migration plan |
| `restart` | Service/container restart | Service name |
| `db_update` | Direct database updates | SQL statements |
| `secret_change` | Modifying secrets/API keys | Secret location |

### Approval Process

1. Planner sets `approval_requested` field
2. Orchestrator pauses session
3. Human runs `approve` or `deny` command
4. Session resumes or blocks accordingly

---

## 8. Tmux Parsing Rules

### Cleanup Pipeline

1. **Strip ANSI codes**: Remove all escape sequences
2. **Trim echoed prompt**: Remove orchestrator's own prompt from capture
3. **Handle duplicate markers**: Extract outermost block if nested
4. **Isolate latest block**: Return only last complete marker-delimited block

### ANSI Escape Patterns

```
\x1B[...[m...   - CSI sequences
\x1B]...        - OSC sequences
\x1B[@-Z\\-_]   - Escape sequences
```

### Echo Removal

Skip lines containing the sent prompt text (with tolerance for shell escaping).

### Marker Extraction

- Find ALL `<<<CLAUDE_RESPONSE_START>>>...<<<CLAUDE_RESPONSE_END>>>` blocks
- Return only the LAST complete block
- If end marker missing: return `None` (incomplete)
- If no markers found: return `None` (no response)

---

## 9. Recovery Behavior

### Failure Type → Action

| Failure | Recovery | Fallback |
|---------|----------|----------|
| Tmux pane not found | Reattach to default | Block |
| Claude not responding | Restart Claude in pane | Block |
| Partial capture (timeout) | Save partial, pause | Manual review |
| Invalid JSON | Retry with stronger schema | Block after 3 |
| Network failure | Exponential backoff | Block |
| Mid-step crash | Save checkpoint | Resume from checkpoint |

### Recovery Limits

- Max attempts: 3
- Backoff multiplier: 2^n attempts
- Checkpoint interval: Every step
- Partial capture handling: Pause and notify

---

## 10. Polling Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `poll_interval` | 3 seconds | Time between pane captures |
| `stable_poll_count` | 4 | Consecutive unchanged polls = stable |
| `capture_timeout` | 300 seconds | Max wait for Claude response |

### Stability Detection

Output considered stable when:
- 4 consecutive captures produce identical output, OR
- End marker detected, OR
- Timeout reached

---

## 11. Safety Rules

1. **Default mode**: `investigate` (read-only)
2. **Code modification**: Never unless explicitly allowed
3. **Audit trail**: Log all actions
4. **Approval required**: code_write, migration, restart, db_update, secret_change
5. **Max steps**: Prevent infinite loops
6. **No-progress detection**: Catch repetitive patterns
7. **Checkpoint saving**: Enable crash recovery

---

## 12. File Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Session | `issue_YYYY_MM_DD_XXX.json` | `issue_2026_03_11_001.json` |
| Log | `run_YYYY_MM_DD.log` | `run_2026_03_11.log` |
| Partial capture | `partial_SESSION_ID.txt` | `partial_issue_2026_03_11_001.txt` |

---

This protocol is the single source of truth for all orchestrator behavior.
