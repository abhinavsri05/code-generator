"""Claude agent with JIRA and Git MCP integration for code generation."""

import anyio
from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
)

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
5. Run the existing test suite and any new tests you wrote using Bash
6. End your response with a summary in this exact format:

## Summary
<what was implemented>

## Test Results
<paste the test output or "No tests found" if none exist>"""


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
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id", "")
            print(f"Session: {session_id}\n")

        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    print(f"Claude: {block.text.strip()}\n")
                elif isinstance(block, ToolUseBlock):
                    input_summary = ", ".join(
                        f"{k}={v!r}" for k, v in (block.input or {}).items()
                    )
                    print(f"  > {block.name}({input_summary})")

        elif isinstance(message, ResultMessage):
            result = message.result

    return result


def run(prompt: str, cwd: str = ".") -> str:
    """Synchronous entry point for code generation."""
    return anyio.run(generate_code, prompt, cwd)
