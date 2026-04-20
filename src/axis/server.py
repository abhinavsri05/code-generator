"""FastAPI web server for the code generator."""

import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import sessions as sess
from . import skills as sk
from .agent import stream_events, stream_events_phased
from .config import fresh_settings
from .jira import extract_issue_key, post_comment


@asynccontextmanager
async def lifespan(app: FastAPI):
    sess.mark_stale_sessions()
    yield


app = FastAPI(title="AXIS — AI-eXecuted Implementation System", lifespan=lifespan)

# In-memory job store: job_id -> (events_queue, task, feedback_queue)
_jobs: dict[str, tuple[asyncio.Queue, asyncio.Task, asyncio.Queue]] = {}

# In-memory upload store: file_id -> {name, kind, content|path}
_uploads: dict[str, dict] = {}


class GenerateRequest(BaseModel):
    jira_key: str
    repo_path: str
    context: str = ""
    attachment_ids: list[str] = []
    phased: bool = True
    model: str = "claude-sonnet-4-6"
    force: bool = False


class FeedbackRequest(BaseModel):
    approved: bool = True
    features: list[dict] = []
    comment: str = ""


async def _git_head_sha(repo_path: str) -> str | None:
    """Return the current HEAD commit SHA, or None if not a git repo / no commits."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode().strip()
    except Exception:
        pass
    return None


async def _git_diff(repo_path: str, base_sha: str | None) -> str:
    """Return a unified diff covering everything that changed since base_sha.

    Uses a single pass of `git diff <base>` (base commit vs current working tree)
    so committed and uncommitted changes are shown in one coherent diff with no
    duplicate file sections.  Untracked new files are appended separately.

    If base_sha is None (empty / brand-new repo) the Git empty-tree object is used
    as the base so all files in the first commit are shown.
    """
    EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    base = base_sha or EMPTY_TREE
    parts: list[str] = []

    async def _git(*args: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode() if proc.returncode == 0 else ""
        except Exception:
            return ""

    # Single pass: base commit vs current working tree.
    # Covers all tracked files — both committed and uncommitted changes —
    # without duplication.
    tracked = await _git("diff", base)
    if tracked:
        parts.append(tracked)

    # Untracked new files (not yet staged) won't appear in `git diff`.
    untracked_out = await _git("ls-files", "--others", "--exclude-standard")
    for filepath in untracked_out.splitlines():
        filepath = filepath.strip()
        if not filepath:
            continue
        file_diff = await _git("diff", "--no-index", "/dev/null", filepath)
        if file_diff:
            parts.append(file_diff)

    return "".join(parts)


async def _run_init(repo_path: str, emit) -> None:
    """Run `claude /init` in repo_path to ensure CLAUDE.md exists."""
    await emit({"type": "info", "text": "Running claude /init…"})
    proc = await asyncio.create_subprocess_exec(
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        cwd=repo_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate(input=b"/init")
    if stdout:
        await emit({"type": "info", "text": stdout.decode().strip()})


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "static" / "index.html"
    return html_path.read_text()


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Accept a file upload. Images are saved to a temp path; text files are stored as strings."""
    file_id = str(uuid.uuid4())
    content = await file.read()
    media_type = file.content_type or "application/octet-stream"

    if media_type.startswith("image/"):
        suffix = Path(file.filename or "upload").suffix or ".bin"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="codegen_img_")
        os.close(fd)
        with open(tmp_path, "wb") as f:
            f.write(content)
        _uploads[file_id] = {
            "name": file.filename,
            "kind": "image",
            "path": tmp_path,
        }
    else:
        _uploads[file_id] = {
            "name": file.filename,
            "kind": "text",
            "content": content.decode("utf-8", errors="replace"),
        }

    return {"id": file_id, "name": file.filename, "kind": _uploads[file_id]["kind"]}


@app.delete("/upload/{file_id}")
async def delete_upload(file_id: str):
    """Remove an uploaded file from the store (and disk if it was an image)."""
    entry = _uploads.pop(file_id, None)
    if entry and entry["kind"] == "image":
        try:
            os.unlink(entry["path"])
        except OSError:
            pass
    return {"deleted": True}


@app.post("/generate")
async def generate(req: GenerateRequest):
    if not Path(req.repo_path).is_dir():
        raise HTTPException(status_code=400, detail=f"Repository path does not exist: {req.repo_path}")

    # Block if another session is already running for this repo (unless forced)
    if not req.force:
        active = sess.active_session_for_repo(req.repo_path)
        if active:
            return JSONResponse(
                status_code=409,
                content={"detail": "session_running", "session": active},
            )

    # Delete previous sessions for this repo, then start fresh
    sess.delete_sessions_for_repo(req.repo_path)

    # Build prompt, appending any uploaded attachments
    prompt = req.jira_key
    if req.context.strip():
        prompt += f"\n\nAdditional context: {req.context.strip()}"

    for att_id in req.attachment_ids:
        att = _uploads.get(att_id)
        if not att:
            continue
        if att["kind"] == "text":
            prompt += f"\n\n## Attached Document: {att['name']}\n{att['content']}"
        elif att["kind"] == "image":
            prompt += (
                f"\n\n## Attached Wireframe/Image: {att['name']}\n"
                f"The image is saved at: {att['path']}\n"
                f"Use the Read tool to view this image for UI/design context."
            )

    job_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    feedback_queue: asyncio.Queue = asyncio.Queue()

    async def _run():
        cfg = fresh_settings()
        result = ""
        cancelled = False
        base_sha = None
        sess.create_session(job_id, req.jira_key, req.repo_path, req.model)

        async def emit(event: dict) -> None:
            await queue.put(event)
            sess.append_event(job_id, event)

        try:
            await _run_init(req.repo_path, emit)
            base_sha = await _git_head_sha(req.repo_path)
            if req.phased:
                event_stream = stream_events_phased(
                    prompt, cwd=req.repo_path, cfg=cfg,
                    jira_key=req.jira_key, feedback_queue=feedback_queue,
                    model=req.model,
                )
            else:
                event_stream = stream_events(prompt, cwd=req.repo_path, cfg=cfg,
                                             jira_key=req.jira_key, model=req.model)
            async for event in event_stream:
                await emit(event)
                if event["type"] == "result":
                    result = event["text"]
        except asyncio.CancelledError:
            cancelled = True
            await emit({"type": "error", "text": "Job cancelled by user."})
        except Exception as exc:
            await emit({"type": "error", "text": str(exc)})
        finally:
            if not cancelled:
                diff = await _git_diff(req.repo_path, base_sha)
                if diff:
                    await emit({"type": "diff", "text": diff})
                if result and cfg.jira_url and cfg.jira_api_token:
                    issue_key = extract_issue_key(req.jira_key)
                    if issue_key:
                        try:
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(None, post_comment, issue_key, result)
                            await emit({"type": "info", "text": f"Comment posted to {issue_key}."})
                        except Exception as exc:
                            await emit({"type": "error", "text": f"Could not post JIRA comment: {exc}"})
            final_status = "cancelled" if cancelled else "done"
            sess.update_status(job_id, final_status)
            await queue.put({"type": "done"})
            sess.append_event(job_id, {"type": "done"})

    task = asyncio.create_task(_run())
    _jobs[job_id] = (queue, task, feedback_queue)
    return {"job_id": job_id}


@app.post("/jobs/{job_id}/feedback")
async def submit_feedback(job_id: str, body: FeedbackRequest):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Job not found")
    _, _, feedback_queue = entry
    await feedback_queue.put(body.model_dump())
    return {"ok": True}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Job not found")
    queue, _, _ = entry

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] == "done":
                    break
        finally:
            _jobs.pop(job_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class OpenVSCodeRequest(BaseModel):
    repo_path: str


@app.post("/open-vscode")
async def open_vscode(req: OpenVSCodeRequest):
    if not Path(req.repo_path).is_dir():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {req.repo_path}")
    try:
        subprocess.Popen(["code", req.repo_path])
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="vscode_not_found")
    return {"opened": True}


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Job not found")
    _, task, _ = entry
    task.cancel()
    return {"cancelled": True}


@app.get("/sessions")
async def list_sessions():
    return sess.list_sessions()


@app.get("/sessions/{session_id}/events")
async def get_session_events(session_id: str):
    events = sess.get_events(session_id)
    if not events and not (sess.sessions_dir() / session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return events


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    # If the job is still active, cancel it first
    entry = _jobs.get(session_id)
    if entry:
        _, task, _ = entry
        task.cancel()
    sess.delete_session(session_id)
    return {"deleted": True}


@app.delete("/sessions")
async def delete_all_sessions():
    # Cancel any active jobs whose session is being wiped
    for job_id in list(_jobs.keys()):
        _, task, _ = _jobs[job_id]
        task.cancel()
    sess.delete_all_sessions()
    return {"deleted": True}


@app.get("/skills")
async def list_skills():
    return sk.list_skills()


@app.post("/skills")
async def upload_skill(file: UploadFile = File(...)):
    data = await file.read()
    filename = file.filename or "skill.md"
    saved = sk.ingest_upload(filename, data)
    if not saved:
        raise HTTPException(status_code=400, detail="No .md files found in upload.")
    return {"saved": saved}


@app.delete("/skills/{name}")
async def delete_skill(name: str):
    if not sk.delete_skill(name):
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"deleted": True}


@app.delete("/skills")
async def delete_all_skills():
    sk.delete_all_skills()
    return {"deleted": True}


def serve():
    import uvicorn
    uvicorn.run("axis.server:app", host="0.0.0.0", port=8000, reload=False)
