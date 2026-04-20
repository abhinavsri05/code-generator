"""Skill management — stores .md instruction files at ~/.axis/skills/.

Accepted upload formats:
  - Single .md file
  - .zip archive containing .md files
  - .tar.gz / .tgz archive containing .md files

On conflict (same filename), the newly uploaded file overwrites the existing one.
Skill content is injected at the end of system prompts; skill instructions take
precedence over application defaults on any conflicting points.
"""

import gzip
import io
import platform
import shutil
import tarfile
import zipfile
from pathlib import Path


def skills_dir() -> Path:
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming" / "AXIS"
    else:
        base = Path.home() / ".axis"
    d = base / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_skills() -> list[dict]:
    result = []
    for p in sorted(skills_dir().glob("*.md")):
        result.append({"name": p.name, "size": p.stat().st_size})
    return result


def get_skill_content(name: str) -> str | None:
    p = skills_dir() / name
    if not p.exists() or p.suffix != ".md":
        return None
    return p.read_text(encoding="utf-8")


def load_all_skills() -> str:
    """Return concatenated content of all skill files, or empty string."""
    parts = []
    for p in sorted(skills_dir().glob("*.md")):
        try:
            parts.append(p.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return "\n\n".join(parts)


def save_skill(name: str, content: bytes | str) -> None:
    """Write a single skill file, overwriting if it already exists."""
    p = skills_dir() / name
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)


def ingest_upload(filename: str, data: bytes) -> list[str]:
    """Parse an upload and persist all .md files found. Returns list of saved names."""
    saved: list[str] = []
    fname = filename.lower()

    if fname.endswith(".md"):
        save_skill(Path(filename).name, data)
        saved.append(Path(filename).name)

    elif fname.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.infolist():
                if member.filename.lower().endswith(".md") and not member.is_dir():
                    name = Path(member.filename).name
                    save_skill(name, zf.read(member))
                    saved.append(name)

    elif fname.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name.lower().endswith(".md") and member.isfile():
                    name = Path(member.name).name
                    f = tf.extractfile(member)
                    if f:
                        save_skill(name, f.read())
                        saved.append(name)

    elif fname.endswith(".gz"):
        inner_name = Path(filename[:-3]).name
        if inner_name.lower().endswith(".md"):
            save_skill(inner_name, gzip.decompress(data))
            saved.append(inner_name)

    return saved


def delete_skill(name: str) -> bool:
    p = skills_dir() / name
    if p.exists() and p.suffix == ".md":
        p.unlink()
        return True
    return False


def delete_all_skills() -> None:
    for p in skills_dir().glob("*.md"):
        p.unlink(missing_ok=True)
