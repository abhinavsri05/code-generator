"""Claude agent via CLI with JIRA and Git MCP integration for code generation."""

import anyio
import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator

from .config import Settings, settings


def build_mcp_config(cfg: Settings | None = None, repo_path: str | None = None) -> dict:
    """Build MCP server configuration dict for the claude CLI --mcp-config flag."""
    cfg = cfg or settings
    servers = {}

    # Git MCP server — reads local repo context (branches, commits, diffs)
    git_repo = repo_path or cfg.git_repo_path
    servers["git"] = {
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", git_repo],
    }

    # Atlassian MCP server — reads JIRA tickets and Confluence pages
    if cfg.jira_url and cfg.jira_api_token:
        env: dict[str, str] = {
            "JIRA_URL": cfg.jira_url,
            "JIRA_USERNAME": cfg.jira_username,
            "JIRA_API_TOKEN": cfg.jira_api_token,
        }
        conf_url = cfg.effective_confluence_url
        if conf_url and cfg.effective_confluence_api_token:
            env.update({
                "CONFLUENCE_URL": conf_url,
                "CONFLUENCE_USERNAME": cfg.effective_confluence_username,
                "CONFLUENCE_API_TOKEN": cfg.effective_confluence_api_token,
            })
        servers["atlassian"] = {
            "command": "uvx",
            "args": ["mcp-atlassian"],
            "env": env,
        }

    return {"mcpServers": servers}


def _build_system_prompt(jira_key: str = "") -> str:
    ticket_ref = jira_key.strip() if jira_key.strip() else "<ticket-key>"
    return f"""You are an expert software engineer. Your job is to implement code
based on JIRA tickets and the existing Git repository context.

The JIRA ticket for this task is: {ticket_ref}

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
9. If git repo then create or work on a branch named feature/{ticket_ref}_codegen and commit all changes with the message "{ticket_ref}: <ticket summary>". Do NOT push the branch.
10. For each method, function, class or any other code you write, include a docstring that explains what it does, its inputs and outputs, and any important implementation details. Always reference the exact JIRA ticket key {ticket_ref} (not any other placeholder) as the source of the requirements. This is critical for maintainability and readability of the code.
11. Always write README.md. Include images/wireframes if relevant. The README should explain the purpose of the code, how to use it, and any other relevant details. This is important for anyone who will read or maintain the code in the future.
12. Run /init at the start to ensure CLAUDE.md is present in the repo, which is required for the Git MCP to work properly. Run at the end as well to update CLAUDE.md with the new code changes and test results, which helps CLAUDE.md provide better context for future runs.
"""

async def stream_events(prompt: str, cwd: str = ".", cfg: Settings | None = None, jira_key: str = "") -> AsyncIterator[dict]:
    """Async generator that yields agent events as dicts.

    Event shapes:
      {"type": "session", "text": "session-id"}
      {"type": "tool",    "text": "ToolName(args)"}
      {"type": "text",    "text": "Claude message"}
      {"type": "result",  "text": "final result markdown"}
      {"type": "done"}
    """
    cfg = cfg or settings
    mcp_config = build_mcp_config(cfg, repo_path=cwd)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(mcp_config, f)
        mcp_config_path = f.name

    try:
        cmd = [
            "claude",
            "--print",
            "--verbose",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--max-turns", "50",
            "--system-prompt", _build_system_prompt(jira_key),
            "--mcp-config", mcp_config_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Send prompt via stdin to avoid positional-arg / --mcp-config parsing conflicts
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # Drain stderr concurrently to avoid pipe-buffer deadlock
        stderr_task = asyncio.create_task(proc.stderr.read())

        # Read stdout in raw chunks to avoid the default 64 KB per-line limit
        # (large MCP responses can exceed it, causing LimitOverrunError)
        events_received = 0
        leftover = b""
        tool_call_map: dict[str, str] = {}  # tool_use_id -> tool_name
        while True:
            chunk = await proc.stdout.read(131072)  # 128 KB
            if not chunk:
                break
            leftover += chunk
            while b"\n" in leftover:
                raw_line, leftover = leftover.split(b"\n", 1)
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                events_received += 1
                event_type = event.get("type")

                if event_type == "system" and event.get("subtype") == "init":
                    yield {"type": "session", "text": event.get("session_id", "")}

                elif event_type == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text", "").strip():
                            yield {"type": "text", "text": block["text"].strip()}
                        elif block.get("type") == "tool_use":
                            tool_id = block.get("id", "")
                            tool_name = block.get("name", "")
                            tool_call_map[tool_id] = tool_name
                            def _fmt(v: object) -> str:
                                if isinstance(v, str):
                                    if "\n" in v or len(v) > 80:
                                        lines = v.count("\n") + 1
                                        return f"<{lines} lines>"
                                    return repr(v)
                                return repr(v)
                            input_summary = ", ".join(
                                f"{k}={_fmt(v)}" for k, v in (block.get("input") or {}).items()
                            )
                            yield {"type": "tool", "text": f"{tool_name}({input_summary})"}

                elif event_type == "user":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") != "tool_result":
                            continue
                        tool_id = block.get("tool_use_id", "")
                        tool_name = tool_call_map.get(tool_id, "")
                        if not tool_name.startswith("jira_"):
                            continue
                        raw_content = block.get("content", "")
                        if isinstance(raw_content, list):
                            text_parts = [
                                c.get("text", "") for c in raw_content if c.get("type") == "text"
                            ]
                            raw_content = "\n".join(text_parts)
                        if raw_content:
                            yield {"type": "jira", "text": raw_content, "tool": tool_name}

                elif event_type == "result":
                    usage = event.get("usage", {})
                    total_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    yield {
                        "type": "result",
                        "text": event.get("result", ""),
                        "tokens": total_tokens or None,
                        "cost_usd": event.get("cost_usd"),
                    }

        await proc.wait()
        stderr_text = (await stderr_task).decode().strip()
        if stderr_text:
            yield {"type": "error", "text": f"claude stderr (exit {proc.returncode}):\n{stderr_text}"}
        elif proc.returncode != 0:
            yield {"type": "error", "text": f"claude exited with code {proc.returncode} (no output)"}
        elif events_received == 0:
            yield {"type": "error", "text": "claude produced no output (exit 0) — check MCP config or prompt"}
    finally:
        os.unlink(mcp_config_path)


async def generate_code(prompt: str, cwd: str = ".") -> str:
    """Run the code-generation agent, print progress, and return the final result."""
    from .jira import extract_issue_key
    jira_key = extract_issue_key(prompt) or ""
    result = ""
    async for event in stream_events(prompt, cwd, jira_key=jira_key):
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
