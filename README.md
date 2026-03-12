# Bot Orchestrator

A text-only bridge orchestrator that manages a strict investigation loop between ChatGPT (as planner/reviewer), Claude CLI (as executor/codebase investigator), and a human operator.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│   You       │────▶│ Orchestrator │────▶│   ChatGPT       │
│             │     │  (Python)    │◀────│   (Planner)     │
└─────────────┘     └──────┬───────┘     └─────────────────┘
                           │
                           │ tmux send-keys
                           ▼
                    ┌──────────────┐
                    │ Claude CLI   │
                    │ (in tmux)    │
                    └──────────────┘
```

## Features

- **Strict Investigation Loop**: One-step-at-a-time execution enforced
- **Reliable Response Capture**: Marker-based extraction from tmux
- **Stop Condition Detection**: Automatic detection of stalled loops
- **Approval Workflow**: Human-in-the-loop for risky actions
- **Crash Recovery**: Checkpoint-based recovery from failures
- **Multiple Modes**: investigate, propose_fix, validate_fix, approved_execute

## Installation

1. Clone the repository
2. Install dependencies:
```bash
pip install -r requirements.txt
playwright install chromium  # For web mode
```

3. Configure environment:
```bash
cp .env.example .env
# Edit .env with settings
```

4. Ensure tmux is installed and Claude CLI is running in a tmux session:
```bash
tmux new-session -d -s dev
tmux send-keys -t dev:0.0 "claude" C-m
```

## Usage

### Web Mode (Recommended - No API Key Required)

**Step 1:** Start Chrome with remote debugging enabled, then open ChatGPT and log in.

Windows PowerShell example:
```powershell
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
Start-Process -FilePath $chrome -ArgumentList "--remote-debugging-port=9222","https://chatgpt.com"
```

If you use a different CDP endpoint, set:
```powershell
$env:ORCHESTRATOR_CDP_URL="http://127.0.0.1:9222"
```

**Step 2:** Run the orchestrator with web mode:

```bash
python main.py run-web \
  --title "Investigate admin deposit page" \
  --description "The admin deposit page is missing records and showing wrong status..." \
  --project algonney \
  --mode investigate
```

The orchestrator will:
1. Attach to your already open Chrome browser via CDP
2. Send your issue to the open ChatGPT conversation
3. Get the next investigation step
4. Send it to Claude in tmux
5. Capture Claude's response
6. Send it back to ChatGPT
7. Repeat until resolved/blocked

**Optional:** Attach to an existing ChatGPT conversation:
```bash
python main.py run-web \
  --session issue_2026_03_11_abc123 \
  --chatgpt-url "https://chatgpt.com/c/..."
```

### API Mode (Requires OpenAI API Key)

### Create a New Session

```bash
python main.py new \
  --title "Issue: User login fails" \
  --description "Users report login form returns 500 error" \
  --project myapp \
  --tmux dev:0.0 \
  --mode investigate
```

### Run Orchestration

```bash
python main.py run --session issue_2026_03_11_abc123
```

### Resume a Paused Session

```bash
python main.py resume --session issue_2026_03_11_abc123
```

### Check Session Status

```bash
python main.py status --session issue_2026_03_11_abc123
```

### Approve an Action

```bash
python main.py approve --session issue_2026_03_11_abc123
```

### List All Sessions

```bash
python main.py list --status running --project myapp
```

## Modes

| Mode | Description | Capabilities |
|------|-------------|--------------|
| `investigate` | Read-only investigation | read_files, grep_code, inspect_logs |
| `propose_fix` | Suggest patches | + suggest_patch (no execution) |
| `validate_fix` | Verify fix correctness | + verify_fix |
| `approved_execute` | Execute with approval | Pre-approved commands only |

## Configuration

Configuration is loaded from:
1. Environment variables (prefixed with `ORCHESTRATOR_`)
2. `.env` file
3. `config.yaml` file
4. Default values

Key settings:
- `ORCHESTRATOR_OPENAI_API_KEY`: Your OpenAI API key
- `ORCHESTRATOR_DEFAULT_TMUX_TARGET`: Default tmux pane (e.g., `dev:0.0`)
- `ORCHESTRATOR_MAX_STEPS`: Maximum steps before auto-stop (default: 30)

## Protocol

See [PROTOCOL.md](PROTOCOL.md) for complete specification of:
- Response schemas
- Claude response markers
- Stop conditions
- Approval workflow
- Recovery behavior

## File Structure

```
bot_orchestrator/
├── main.py                  # CLI entry point
├── config.py                # Configuration management
├── schema.py                # Pydantic data models
├── orchestrator.py          # Main loop controller
├── openai_client.py         # ChatGPT API client
├── tmux_bridge.py           # Tmux operations
├── claude_capture.py        # Response capture
├── tmux_parser.py           # Output parsing
├── prompt_builder.py        # Prompt construction
├── stop_detector.py         # Stop condition logic
├── recovery.py              # Crash/recovery handling
├── approval.py              # Approval workflow
├── session_manager.py       # Session CRUD operations
├── utils.py                 # Helper utilities
├── PROTOCOL.md              # Protocol specification
├── sessions/                # Session JSON files
├── logs/                    # Execution logs
└── prompts/                 # Prompt templates
    ├── planner_system.txt
    └── claude_*.txt
```

## Safety

- Default mode is read-only (`investigate`)
- Approval required for: code writes, migrations, restarts, DB updates, secret changes
- Max steps limit prevents infinite loops
- No-progress detection catches repetitive patterns
- All actions logged for audit

## License

MIT
# orchestrator
