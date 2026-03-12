"""
Configuration management for the Bot Orchestrator.

Loads settings from environment variables and config.yaml files.
"""
import os
from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator

from schema import Mode


class Settings(BaseSettings):
    """
    Configuration settings for the orchestrator.

    Settings are loaded from:
    1. Environment variables (highest priority)
    2. .env file
    3. config.yaml file
    4. Default values (lowest priority)
    """

    # API settings
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key for ChatGPT access"
    )
    planner_model: str = Field(
        default="gpt-4o",
        description="Model to use for planning (ChatGPT)"
    )

    # Tmux settings
    default_tmux_target: str = Field(
        default="claude_bot:0.0",
        description="Default tmux target (format: session:window.pane)"
    )
    ssh_host: str = Field(
        default="",
        description="SSH host for remote tmux (e.g., 'root@algonney-server')"
    )
    ssh_key_path: str = Field(
        default="",
        description="Path to SSH private key for authentication"
    )

    # Paths
    sessions_dir: str = Field(
        default="sessions",
        description="Directory for session JSON files"
    )
    logs_dir: str = Field(
        default="logs",
        description="Directory for execution logs"
    )
    prompts_dir: str = Field(
        default="prompts",
        description="Directory for prompt templates"
    )
    partials_dir: str = Field(
        default="partials",
        description="Directory for partial captures"
    )

    # Loop control
    max_steps: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum steps before automatic stop"
    )
    stable_poll_count: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Consecutive unchanged polls to consider output stable"
    )
    poll_interval: int = Field(
        default=3,
        ge=1,
        le=60,
        description="Seconds between tmux pane polls"
    )
    capture_timeout: int = Field(
        default=3600,
        ge=30,
        le=86400,
        description="Soft max seconds to wait for Claude response before stall-only mode"
    )
    capture_stall_timeout: int = Field(
        default=300,
        ge=30,
        le=86400,
        description="Seconds with no tmux pane change before capture is considered stalled"
    )

    # Safety settings
    default_mode: Mode = Field(
        default=Mode.INVESTIGATE,
        description="Default execution mode"
    )
    require_approval_for: List[str] = Field(
        default=[
            "code_write",
            "migration",
            "restart",
            "db_update",
            "secret_change"
        ],
        description="Actions requiring explicit human approval"
    )

    # Recovery settings
    max_recovery_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum recovery attempts before blocking"
    )
    recovery_backoff_base: int = Field(
        default=2,
        ge=1,
        description="Base for exponential backoff (seconds)"
    )

    # History settings
    history_summary_length: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of recent steps to include in planner input"
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR)"
    )
    log_to_file: bool = Field(
        default=True,
        description="Write logs to file"
    )

    # Browser automation settings
    browser_headless: bool = Field(
        default=False,
        description="Run browser in headless mode"
    )
    browser_user_data_dir: str = Field(
        default="browser_data",
        description="Directory for persistent browser profile"
    )
    browser_timeout_ms: int = Field(
        default=120000,
        ge=30000,
        le=300000,
        description="ChatGPT response timeout in milliseconds"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ORCHESTRATOR_",
        extra="ignore"
    )

    @field_validator('openai_api_key')
    @classmethod
    def validate_api_key(cls, v):
        """Validate that API key is set when needed."""
        if not v:
            # Allow empty for testing, but warn in real usage
            pass
        return v

    @field_validator('default_tmux_target')
    @classmethod
    def validate_tmux_target_format(cls, v):
        """Validate tmux target format."""
        if isinstance(v, str) and v:
            parts = v.split(':')
            if len(parts) != 2:
                raise ValueError(f"Invalid tmux target format: {v}. Expected 'session:window.pane'")
        return v

    def get_sessions_path(self) -> Path:
        """Get Path object for sessions directory."""
        return Path(self.sessions_dir)

    def get_logs_path(self) -> Path:
        """Get Path object for logs directory."""
        return Path(self.logs_dir)

    def get_prompts_path(self) -> Path:
        """Get Path object for prompts directory."""
        return Path(self.prompts_dir)

    def get_partials_path(self) -> Path:
        """Get Path object for partials directory."""
        return Path(self.partials_dir)

    def get_browser_data_path(self) -> Path:
        """Get Path object for browser user data directory."""
        return Path(self.browser_user_data_dir)

    def ensure_directories(self) -> None:
        """Create all required directories if they don't exist."""
        for path in [
            self.get_sessions_path(),
            self.get_logs_path(),
            self.get_prompts_path(),
            self.get_partials_path()
        ]:
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_yaml(cls, config_path: str = "config.yaml") -> "Settings":
        """
        Load settings from a YAML configuration file.

        Args:
            config_path: Path to the YAML configuration file

        Returns:
            Settings instance with values from the file
        """
        import yaml

        config_file = Path(config_path)
        if not config_file.exists():
            return cls()

        with open(config_file, 'r') as f:
            config_data = yaml.safe_load(f) or {}

        # Flatten nested keys for environment variable compatibility
        # e.g., {api: {openai_key: "xxx"}} -> {openai_api_key: "xxx"}
        flattened = {}
        for key, value in config_data.items():
            if isinstance(value, dict):
                for subkey, subvalue in value.items():
                    flattened[f"{key}_{subkey}"] = subvalue
            else:
                flattened[key] = value

        return cls(**flattened)

    def to_dict(self) -> dict:
        """Convert settings to dictionary (excluding sensitive values)."""
        data = self.model_dump()
        # Don't log the API key
        if 'openai_api_key' in data:
            data['openai_api_key'] = "***REDACTED***"
        return data


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Get the global settings instance.

    Returns:
        Settings instance (singleton)
    """
    global _settings
    if _settings is None:
        # Try to load from config.yaml first, then environment
        _settings = Settings.from_yaml()
        _settings.ensure_directories()
    return _settings


def reload_settings() -> Settings:
    """
    Reload settings from configuration sources.

    Returns:
        Fresh Settings instance
    """
    global _settings
    _settings = Settings.from_yaml()
    _settings.ensure_directories()
    return _settings
