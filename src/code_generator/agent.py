"""Claude agent via CLI with JIRA and Git MCP integration for code generation."""

import anyio
import asyncio
import json
import os
import re
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

_EXPLORE_SYSTEM_PROMPT = """\
You are an expert software engineer. Your task is to deeply explore the context before any code is written.

Steps:
1. Use the JIRA MCP tools to read the ticket: summary, description, acceptance criteria, linked issues
2. Use the Git MCP tools to understand the codebase:
   - Current branch and recent commits
   - File/directory structure of key source directories
   - Existing code relevant to this ticket
   - Tech stack, test framework, build system
   - Coding conventions (naming, structure, docstrings)
3. Identify ambiguities, risks, or missing information in the ticket

Output a comprehensive exploration report with these sections:
## Ticket Summary
<exactly what needs to be built>

## Codebase Overview
<tech stack, key directories, relevant existing code>

## Implementation Notes
<patterns to follow, potential challenges, files likely to change>

## Open Questions
<anything unclear that the user should clarify before planning>
"""

_PLAN_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert software architect. Based on the exploration below, create a detailed implementation plan.

## Exploration Context
{exploration_context}

Using this context, break the work into 2-6 logical features ordered by dependency.

Output your plan as a JSON object inside a ```json code block:
{{
  "ticket_summary": "One-line summary of the ticket",
  "features": [
    {{
      "id": "f1",
      "name": "Short feature name",
      "description": "What this feature implements",
      "files_affected": ["src/path/to/file.py"],
      "depends_on": [],
      "acceptance_criteria": ["Criterion 1", "Criterion 2"]
    }}
  ]
}}

Rules:
- Order features so dependencies come first; "depends_on" lists prerequisite feature ids
- Each feature must be independently testable
- If the ticket is small enough for one feature, output just one
- After the JSON block, briefly explain the rationale
"""

_IMPLEMENT_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert software engineer implementing ONE specific feature of a JIRA ticket.

The JIRA ticket: {jira_key}

## Your feature to implement
Name: {feature_name}
Description: {feature_description}
Expected files: {files_affected}
Acceptance criteria:
{acceptance_criteria}

## All features in this ticket (implement ONLY yours)
{all_features_summary}

## Already-completed features
{completed_summary}

## Instructions
1. Use JIRA MCP tools to read the full ticket if you need more context
2. Use Git MCP tools to understand the codebase and what has already been committed
3. Implement ONLY the feature described above
4. Follow existing coding conventions
5. Write and run tests for your implementation
6. Keep .env and IDE files out of git (add to .gitignore if missing)
7. Write all files with relative paths directly into the cwd — no wrapper directories
8. Commit with message: "{jira_key}: Implement {feature_name}"
9. Include docstrings referencing {jira_key}
10. End your response with:

## Summary
<what was implemented>

## Test Results
<test output or "No tests found">
"""

_COMMIT_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert software engineer. All features have been implemented. Run tests, fix any failures, and finalize everything.

JIRA ticket: {jira_key}

## Features implemented
{features_summary}

## Instructions
1. Use Git MCP tools to review all commits made for this ticket
2. Run the FULL test suite — fix any failures before proceeding
3. Update or create README.md to cover all changes
4. Run /init to update CLAUDE.md
5. Ensure all changes are committed
6. End your response with:

## Commit Summary
<what was verified or fixed>

## Final Test Results
<full test output>
"""


def _extract_json_from_result(text: str) -> dict | None:
    """Extract the first JSON object containing a 'features' key from agent output."""
    for m in re.finditer(r'```(?:json)?\s*([\s\S]*?)\s*```', text):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict) and "features" in data:
                return data
        except json.JSONDecodeError:
            continue
    m = re.search(r'\{[\s\S]*?"features"\s*:\s*\[[\s\S]*?\][\s\S]*?\}', text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


async def stream_events(
    prompt: str,
    cwd: str = ".",
    cfg: Settings | None = None,
    jira_key: str = "",
    system_prompt_override: str | None = None,
) -> AsyncIterator[dict]:
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
            "--system-prompt", system_prompt_override if system_prompt_override is not None else _build_system_prompt(jira_key),
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


async def stream_events_phased(
    prompt: str,
    cwd: str,
    cfg: Settings,
    jira_key: str,
    feedback_queue: asyncio.Queue,
) -> AsyncIterator[dict]:
    """Four-phase code generation with human-in-the-loop checkpoints.

    Phases: Explore → [review] → Plan → [review] → Code (per feature) → [inter-feature review] → Commit.
    feedback_queue receives dicts: {"approved": bool, "features": [...], "comment": "..."}.
    """

    async def _wait_feedback(timeout: float = 600.0) -> dict:
        try:
            return await asyncio.wait_for(feedback_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return {}

    # ── Phase 1: Explore ──
    yield {"type": "phase_start", "phase": "explore", "text": "Exploring codebase and ticket context…"}

    explore_result = ""
    async for event in stream_events(prompt, cwd=cwd, cfg=cfg, jira_key=jira_key,
                                     system_prompt_override=_EXPLORE_SYSTEM_PROMPT):
        yield event
        if event["type"] == "result":
            explore_result = event["text"]

    yield {"type": "phase_complete", "phase": "explore", "summary": explore_result}

    yield {
        "type": "feedback_required",
        "checkpoint": "explore",
        "summary": explore_result,
        "message": "Review the exploration findings. Add any context or corrections before planning begins.",
    }
    fb = await _wait_feedback()
    if not fb:
        yield {"type": "info", "text": "No feedback received — proceeding to planning."}
    explore_extra = fb.get("comment", "")

    # ── Phase 2: Plan ──
    yield {"type": "phase_start", "phase": "plan", "text": "Creating implementation plan…"}

    plan_prompt = f"Create an implementation plan for {jira_key or 'this task'} based on the exploration context."
    if explore_extra:
        plan_prompt += f"\n\nUser corrections/additions: {explore_extra}"

    plan_system = _PLAN_SYSTEM_PROMPT_TEMPLATE.format(
        exploration_context=explore_result or "(no exploration context)",
    )

    plan_result = ""
    async for event in stream_events(plan_prompt, cwd=cwd, cfg=cfg, jira_key=jira_key,
                                     system_prompt_override=plan_system):
        yield event
        if event["type"] == "result":
            plan_result = event["text"]

    decomposed = _extract_json_from_result(plan_result) if plan_result else None
    features: list[dict] = decomposed.get("features", []) if decomposed else []

    if not features:
        yield {"type": "error", "text": "Could not parse feature plan — falling back to single-phase generation."}
        async for event in stream_events(prompt, cwd=cwd, cfg=cfg, jira_key=jira_key):
            yield event
        return

    yield {"type": "phase_complete", "phase": "plan", "features": features}

    yield {
        "type": "feedback_required",
        "checkpoint": "plan",
        "features": features,
        "message": "Review the feature plan. Remove unwanted features or add guidance before coding begins.",
    }
    fb = await _wait_feedback()
    if not fb:
        yield {"type": "info", "text": "No feedback received — proceeding with original plan."}
    if fb.get("features"):
        features = fb["features"]
    code_extra = fb.get("comment", "")

    # ── Phase 3: Code (one subprocess per feature) ──
    completed: list[dict] = []
    for i, feature in enumerate(features):
        yield {
            "type": "phase_start", "phase": "implement",
            "index": i, "total": len(features), "feature": feature,
            "text": f"Coding feature {i + 1}/{len(features)}: {feature.get('name', '')}",
        }

        all_summary = "\n".join(
            f"  [{f.get('id', 'f' + str(j + 1))}] {f.get('name', '')}: {f.get('description', '')}"
            for j, f in enumerate(features)
        )
        done_summary = (
            "\n".join(f"  [{f.get('id', '')}] {f.get('name', '')}: COMPLETE" for f in completed)
            if completed else "  (none yet)"
        )
        criteria = "\n".join(f"  - {c}" for c in feature.get("acceptance_criteria", [])) or "  - See ticket"
        files = ", ".join(feature.get("files_affected", [])) or "TBD"

        impl_prompt = f"Implement the feature '{feature.get('name', '')}' for {jira_key or 'this task'}."
        if code_extra:
            impl_prompt += f"\n\nAdditional user guidance: {code_extra}"

        impl_system = _IMPLEMENT_SYSTEM_PROMPT_TEMPLATE.format(
            jira_key=jira_key or "<ticket>",
            feature_name=feature.get("name", ""),
            feature_description=feature.get("description", ""),
            files_affected=files,
            acceptance_criteria=criteria,
            all_features_summary=all_summary,
            completed_summary=done_summary,
        )

        async for event in stream_events(impl_prompt, cwd=cwd, cfg=cfg, jira_key=jira_key,
                                         system_prompt_override=impl_system):
            yield event

        completed.append(feature)
        yield {
            "type": "phase_complete", "phase": "implement",
            "index": i, "total": len(features), "feature": feature,
        }

        if i < len(features) - 1:
            yield {
                "type": "feedback_required",
                "checkpoint": "feature_complete",
                "feature": feature, "index": i,
                "remaining_features": features[i + 1:],
                "message": (
                    f"'{feature.get('name', '')}' is complete. "
                    f"Review before continuing to '{features[i + 1].get('name', '')}'."
                ),
            }
            code_extra = ""
            fb = await _wait_feedback()
            code_extra = fb.get("comment", "")

    # ── Phase 4: Commit ──
    yield {"type": "phase_start", "phase": "commit", "text": "Running tests and finalizing commits…"}

    features_summary = "\n".join(f"  - {f.get('name', '')}: {f.get('description', '')}" for f in features)
    commit_system = _COMMIT_SYSTEM_PROMPT_TEMPLATE.format(
        jira_key=jira_key or "<ticket>",
        features_summary=features_summary,
    )

    async for event in stream_events(
        f"Run tests and finalize all commits for {jira_key or 'this task'}.",
        cwd=cwd, cfg=cfg, jira_key=jira_key,
        system_prompt_override=commit_system,
    ):
        yield event

    yield {"type": "phase_complete", "phase": "commit", "text": "All phases complete."}
