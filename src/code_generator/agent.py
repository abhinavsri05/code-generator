"""Claude agent with JIRA and Git MCP integration for code generation."""

import anyio
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

from .config import settings


def build_mcp_servers() -> dict:
    """Build MCP server configurations for JIRA and Git."""
    servers = {}

    # Git MCP server — reads local repo context (branches, commits, diffs)
    servers["git"] = {
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", settings.git_repo_path],
    }

    # JIRA MCP server — reads tickets, acceptance criteria, and project context
    if settings.jira_url and settings.jira_api_token:
        servers["jira"] = {
            "command": "uvx",
            "args": ["mcp-atlassian"],
            "env": {
                "JIRA_URL": settings.jira_url,
                "JIRA_USERNAME": settings.jira_username,
                "JIRA_API_TOKEN": settings.jira_api_token,
            },
        }

    return servers


SYSTEM_PROMPT = """You are an expert software engineer. Your job is to implement code
based on JIRA tickets and the existing Git repository context.

When given a JIRA ticket or task description:
1. Use the JIRA MCP tools to read the ticket details, acceptance criteria, and any linked issues
2. Use the Git MCP tools to understand the current codebase: branches, recent commits, file structure
3. Write clean, well-tested code that satisfies the acceptance criteria and description in the JIRA ticket
4. Follow the coding conventions visible in the existing codebase
5. Create a summary of what you implemented and any assumptions made

Always check the existing code before writing new code to avoid duplication."""


async def generate_code(prompt: str, cwd: str = ".") -> str:
    """Run the code-generation agent and return the final result."""
    mcp_servers = build_mcp_servers()

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        mcp_servers=mcp_servers,
        system_prompt=SYSTEM_PROMPT,
        permission_mode="bypassPermissions",
        max_turns=50,
    )

    result = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message.result
        elif isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id", "")
            print(f"Session started: {session_id}")

    return result


def run(prompt: str, cwd: str = ".") -> str:
    """Synchronous entry point for code generation."""
    return anyio.run(generate_code, prompt, cwd)
