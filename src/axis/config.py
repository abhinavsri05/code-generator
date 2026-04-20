"""Configuration for MCP servers and agent settings."""

from dotenv import load_dotenv
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # JIRA MCP settings
    jira_url: str = ""
    jira_username: str = ""
    jira_api_token: str = ""

    # Confluence settings — defaults to JIRA credentials if not set explicitly.
    # confluence_url defaults to <jira_url>/wiki (standard Atlassian Cloud layout).
    confluence_url: str = ""
    confluence_username: str = ""
    confluence_api_token: str = ""

    # Git MCP settings
    git_repo_path: str = "."

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def effective_confluence_url(self) -> str:
        if self.confluence_url:
            return self.confluence_url
        if self.jira_url:
            return self.jira_url.rstrip("/") + "/wiki"
        return ""

    @property
    def effective_confluence_username(self) -> str:
        return self.confluence_username or self.jira_username

    @property
    def effective_confluence_api_token(self) -> str:
        return self.confluence_api_token or self.jira_api_token


def fresh_settings() -> Settings:
    """Reload .env into os.environ then return a new Settings instance."""
    load_dotenv(override=True)
    return Settings()


# Module-level singleton for CLI usage
settings = Settings()
