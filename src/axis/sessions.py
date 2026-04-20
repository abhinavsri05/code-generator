"""Session persistence — stores job metadata and event logs to disk.

Location:
  macOS / Linux : ~/.axis/sessions/<session_id>/
  Windows       : %APPDATA%/AXIS/sessions/<session_id>/

Each session directory contains:
  meta.json    — id, jira_key, repo_path, model, status, timestamps
  events.jsonl — one JSON event per line (NDJSON)
"""

import json
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path


def sessions_dir() -> Path:
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "AXIS"
    else:
        base = Path.home() / ".axis"
    d = base / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_dir(session_id: str) -> Path:
    return sessions_dir() / session_id


# ── write ────────────────────────────────────────────────────────────────────

def create_session(session_id: str, jira_key: str, repo_path: str, model: str) -> None:
    d = _session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": session_id,
        "jira_key": jira_key,
        "repo_path": str(Path(repo_path).resolve()),
        "model": model,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")


def append_event(session_id: str, event: dict) -> None:
    p = _session_dir(session_id) / "events.jsonl"
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def update_status(session_id: str, status: str) -> None:
    p = _session_dir(session_id) / "meta.json"
    if not p.exists():
        return
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta["status"] = status
        if status in ("done", "cancelled", "error", "interrupted"):
            meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        p.write_text(json.dumps(meta), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


# ── read ─────────────────────────────────────────────────────────────────────

def list_sessions() -> list[dict]:
    d = sessions_dir()
    result: list[dict] = []
    for sd in d.iterdir():
        if not sd.is_dir():
            continue
        meta_path = sd / "meta.json"
        if not meta_path.exists():
            continue
        try:
            result.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    result.sort(key=lambda m: m.get("started_at", ""), reverse=True)
    return result


def get_events(session_id: str) -> list[dict]:
    p = _session_dir(session_id) / "events.jsonl"
    if not p.exists():
        return []
    events: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return events


# ── delete ───────────────────────────────────────────────────────────────────

def delete_session(session_id: str) -> None:
    d = _session_dir(session_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def delete_all_sessions() -> None:
    for sd in sessions_dir().iterdir():
        if sd.is_dir():
            shutil.rmtree(sd, ignore_errors=True)


def delete_sessions_for_repo(repo_path: str) -> None:
    """Delete all stored sessions for a given repo path."""
    resolved = str(Path(repo_path).resolve())
    for meta in list_sessions():
        if meta.get("repo_path") == resolved:
            delete_session(meta["id"])


# ── queries ──────────────────────────────────────────────────────────────────

def active_session_for_repo(repo_path: str) -> dict | None:
    """Return the running session for repo_path, or None."""
    resolved = str(Path(repo_path).resolve())
    for meta in list_sessions():
        if meta.get("status") == "running" and meta.get("repo_path") == resolved:
            return meta
    return None


def mark_stale_sessions() -> None:
    """On server startup, mark any 'running' sessions as 'interrupted'."""
    for meta in list_sessions():
        if meta.get("status") == "running":
            update_status(meta["id"], "interrupted")
