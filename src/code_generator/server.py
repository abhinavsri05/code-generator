"""FastAPI web server for the code generator."""

import asyncio
import json
import uuid
from pathlib import Path

import claude_agent_sdk
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .agent import stream_events
from .config import fresh_settings
from .jira import extract_issue_key, post_comment

app = FastAPI(title="Code Generator")

_CLAUDE_BIN = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"

# In-memory job store: job_id -> (queue, task)
_jobs: dict[str, tuple[asyncio.Queue, asyncio.Task]] = {}


class GenerateRequest(BaseModel):
    jira_key: str
    repo_path: str
    context: str = ""


async def _run_init(repo_path: str, queue: asyncio.Queue) -> None:
    """Run `claude /init` in repo_path to ensure CLAUDE.md exists."""
    await queue.put({"type": "info", "text": "Running claude /init…"})
    proc = await asyncio.create_subprocess_exec(
        str(_CLAUDE_BIN),
        "--print",
        "--dangerously-skip-permissions",
        "/init",
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if stdout:
        await queue.put({"type": "info", "text": stdout.decode().strip()})


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "static" / "index.html"
    return html_path.read_text()


@app.post("/generate")
async def generate(req: GenerateRequest):
    prompt = req.jira_key
    if req.context.strip():
        prompt = f"{req.jira_key}\n\nAdditional context: {req.context.strip()}"

    job_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        cfg = fresh_settings()
        result = ""
        cancelled = False
        try:
            await _run_init(req.repo_path, queue)
            async for event in stream_events(prompt, cwd=req.repo_path, cfg=cfg):
                await queue.put(event)
                if event["type"] == "result":
                    result = event["text"]
        except asyncio.CancelledError:
            cancelled = True
            await queue.put({"type": "error", "text": "Job cancelled by user."})
        except Exception as exc:
            await queue.put({"type": "error", "text": str(exc)})
        finally:
            # Post JIRA comment if credentials are configured (skip on cancellation)
            if not cancelled and result and cfg.jira_url and cfg.jira_api_token:
                issue_key = extract_issue_key(req.jira_key)
                if issue_key:
                    try:
                        # Run in executor so the blocking urllib call never stalls the event loop
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, post_comment, issue_key, result)
                        await queue.put({"type": "info", "text": f"Comment posted to {issue_key}."})
                    except Exception as exc:
                        await queue.put({"type": "error", "text": f"Could not post JIRA comment: {exc}"})
            await queue.put({"type": "done"})
            _jobs.pop(job_id, None)

    task = asyncio.create_task(_run())
    _jobs[job_id] = (queue, task)
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Job not found")
    queue, _ = entry

    async def event_generator():
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] == "done":
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Job not found")
    _, task = entry
    task.cancel()
    return {"cancelled": True}


def serve():
    import uvicorn
    uvicorn.run("code_generator.server:app", host="0.0.0.0", port=8000, reload=False)
