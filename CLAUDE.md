# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A POC AI code generator that accepts a JIRA ticket key (or free-text prompt), reads the ticket via JIRA MCP, reads the target Git repo via Git MCP, generates code using Claude, and posts the result back as a JIRA comment. Offered as both a CLI and a FastAPI web UI with SSE streaming.

## Setup

```bash
poetry install
```

Copy `.env.example` to `.env` (if it exists) or create `.env` with:
```
JIRA_URL=https://your-org.atlassian.net
JIRA_USERNAME=you@example.com
JIRA_API_TOKEN=your-token
GIT_REPO_PATH=/path/to/target/repo   # defaults to "."
```

The `claude` CLI and `uvx` must be available on PATH. `claude` is used to run the agent; `uvx` is used at runtime to launch `mcp-server-git` and `mcp-atlassian`.

## Running

**CLI:**
```bash
code-gen "PROJ-123" --cwd /path/to/target/repo
# or with a natural-language prompt containing a ticket key:
code-gen "Implement login endpoint per PROJ-42" --cwd .
```

**Web UI** (port 8000):
```bash
code-gen-ui
# or for development with reload:
uvicorn code_generator.server:app --reload
```

## Architecture

```
src/code_generator/
  config.py   — Pydantic settings loaded from .env (JIRA creds, git repo path)
  agent.py    — Core: builds MCP config, runs `claude` CLI as a subprocess with --output-format stream-json, parses NDJSON events
  jira.py     — Direct JIRA REST API helpers (post ADF-formatted comments)
  cli.py      — Click CLI: runs agent synchronously, posts JIRA comment if key detected
  server.py   — FastAPI: POST /generate -> job_id, GET /stream/{job_id} (SSE), DELETE /jobs/{job_id}
  static/     — Single-page frontend (vanilla HTML/JS, consumes SSE)
```

### Data flow

1. User submits a JIRA key + repo path (web) or a prompt (CLI).
2. `server.py` / `cli.py` calls `agent.stream_events()`.
3. `agent.py` constructs two MCP servers via `uvx`:
   - `mcp-server-git` — provides Git context (branches, commits, diffs) for the target repo.
   - `mcp-atlassian` — provides JIRA ticket details, acceptance criteria.
4. `agent.py` runs `claude --print --dangerously-skip-permissions --output-format stream-json` as a subprocess, writing the MCP config to a temp JSON file. NDJSON lines are parsed into the same event dict shapes.
5. Events are forwarded to the client as SSE (web) or printed (CLI).
6. On completion, `jira.post_comment()` posts the result to the ticket using JIRA API v3 ADF format.

### Agent behavior

- Runs with `--dangerously-skip-permissions` and `--max-turns 50`.
- Creates/checks out a branch `feature/<ticket-key>_codegen` in the target repo.
- Commits all generated files but does **not** push.
- Runs the existing test suite before finishing.
- Web server runs `claude /init` on the target repo before the main agent call.

### Web server job model

Jobs are in-memory (`_jobs` dict): each job holds an `asyncio.Queue` and a `Task`. The SSE endpoint drains the queue until a `{"type": "done"}` event. Cancellation via `DELETE /jobs/{job_id}` cancels the task and sends an error event.
