"""Configuration for MCP servers and agent settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # JIRA MCP settings
    jira_url: str = ""
    jira_username: str = ""
    jira_api_token: str = ""

    # Git MCP settings
    git_repo_path: str = "."

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
