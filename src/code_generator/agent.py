"""Claude agent with JIRA and Git MCP integration for code generation."""

import anyio
from collections.abc import AsyncIterator
from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
)

from .config import Settings, settings


def build_mcp_servers(cfg: Settings | None = None) -> dict:
    """Build MCP server configurations for JIRA and Git."""
    cfg = cfg or settings
    servers = {}

    # Git MCP server — reads local repo context (branches, commits, diffs)
    servers["git"] = {
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", cfg.git_repo_path],
    }

    # JIRA MCP server — reads tickets, acceptance criteria, and project context
    if cfg.jira_url and cfg.jira_api_token:
        servers["jira"] = {
            "command": "uvx",
            "args": ["mcp-atlassian"],
            "env": {
                "JIRA_URL": cfg.jira_url,
                "JIRA_USERNAME": cfg.jira_username,
                "JIRA_API_TOKEN": cfg.jira_api_token,
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
5. Make sure all IDE files and files with sensitive data (e.g. .env) are added to .gitignore and not included in the generated code
6. IMPORTANT: The current working directory IS the root of the target repository. Write all files directly into it using relative paths (e.g. `src/main/Foo.java`, `pom.xml`). Do NOT create a wrapper subdirectory (e.g. do not do `my-library/src/...` — just `src/...`). Never use absolute paths or create files outside the current working directory.
7. Run the existing test suite and any new tests you wrote using Bash
8. End your response with a summary in this exact format:

## Summary
<what was implemented>

## Test Results
<paste the test output or "No tests found" if none exist>
9. If git repo then create or work on a branch named feature/<ticket-key>_codegen (e.g. feature/PROJ-123_codegen) and commit all changes with the message "<ticket-key>: <ticket summary>". Do NOT push the branch."
"""

async def stream_events(prompt: str, cwd: str = ".", cfg: Settings | None = None) -> AsyncIterator[dict]:
    """Async generator that yields agent events as dicts.

    Event shapes:
      {"type": "session", "text": "session-id"}
      {"type": "tool",    "text": "ToolName(args)"}
      {"type": "text",    "text": "Claude message"}
      {"type": "result",  "text": "final result markdown"}
      {"type": "done"}
    """
    mcp_servers = build_mcp_servers(cfg)

    options = ClaudeAgentOptions(
        cwd=cwd,
        mcp_servers=mcp_servers,
        system_prompt=SYSTEM_PROMPT,
        permission_mode="bypassPermissions",
        max_turns=50,
    )

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id", "")
            yield {"type": "session", "text": session_id}

        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    yield {"type": "text", "text": block.text.strip()}
                elif isinstance(block, ToolUseBlock):
                    input_summary = ", ".join(
                        f"{k}={v!r}" for k, v in (block.input or {}).items()
                    )
                    yield {"type": "tool", "text": f"{block.name}({input_summary})"}

        elif isinstance(message, ResultMessage):
            yield {"type": "result", "text": message.result}

    yield {"type": "done"}


async def generate_code(prompt: str, cwd: str = ".") -> str:
    """Run the code-generation agent, print progress, and return the final result."""
    result = ""
    async for event in stream_events(prompt, cwd):
        if event["type"] == "session":
            print(f"Session: {event['text']}\n")
        elif event["type"] == "text":
            print(f"Claude: {event['text']}\n")
        elif event["type"] == "tool":
            print(f"  > {event['text']}")
        elif event["type"] == "result":
            result = event["text"]
    return result


def run(prompt: str, cwd: str = ".") -> str:
    """Synchronous entry point for code generation."""
    return anyio.run(generate_code, prompt, cwd)
