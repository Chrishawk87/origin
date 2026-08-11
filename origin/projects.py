"""Projects — open Origin against a named piece of work (e.g. "Everroot").

Each project has its own working directory (where the agent acts on files), its
own chat history, and optional per-project config overrides. Projects live
under ~/.origin/projects/<slug>/ and can be exported/imported as a shareable
`.originproj` bundle.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .paths import DATA_DIR

ROOT = DATA_DIR / "projects"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "project"


# ── history (de)serialization ─────────────────────────────────────────────
def serialize_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in history:
        role = m.get("role")
        if role == "assistant":
            out.append({
                "role": "assistant",
                "content": m.get("content", ""),
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in m.get("tool_calls", [])
                ],
            })
        else:
            out.append({k: v for k, v in m.items()})
    return out


def deserialize_history(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from .llm.base import ToolCall

    out: List[Dict[str, Any]] = []
    for m in data:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content", ""),
                "tool_calls": [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
                    for tc in m["tool_calls"]
                ],
            })
        else:
            out.append(dict(m))
    return out


@dataclass
class Project:
    name: str
    slug: str
    workdir: str
    created: float = 0.0
    notes: str = ""
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    root: Optional[Path] = None  # set by ProjectManager; single source of truth

    @property
    def dir(self) -> Path:
        return (self.root or ROOT) / self.slug

    @property
    def meta_path(self) -> Path:
        return self.dir / "project.json"

    @property
    def history_path(self) -> Path:
        return self.dir / "history.json"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "workdir": self.workdir,
            "created": self.created,
            "notes": self.notes,
            "config_overrides": self.config_overrides,
        }

    # history helpers
    def load_history(self) -> List[Dict[str, Any]]:
        if self.history_path.is_file():
            try:
                return deserialize_history(json.loads(self.history_path.read_text()))
            except Exception:
                return []
        return []

    def save_history(self, history: List[Dict[str, Any]]) -> None:
        # drop the leading system message; it's regenerated from persona
        persisted = [m for m in history if m.get("role") != "system"]
        self.history_path.write_text(json.dumps(serialize_history(persisted), indent=2))

    def display_transcript(self) -> List[Dict[str, str]]:
        """User/assistant text turns for the UI (tool traffic omitted)."""
        turns = []
        for m in self.load_history():
            if m.get("role") == "user":
                turns.append({"role": "user", "text": m.get("content", "")})
            elif m.get("role") == "assistant" and m.get("content"):
                turns.append({"role": "assistant", "text": m.get("content", "")})
        return turns


class ProjectManager:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> List[Project]:
        projects = []
        for d in sorted(self.root.iterdir()):
            meta = d / "project.json"
            if meta.is_file():
                try:
                    projects.append(self._from_dict(json.loads(meta.read_text())))
                except Exception:
                    continue
        return projects

    def _from_dict(self, d: Dict[str, Any]) -> Project:
        return Project(
            name=d["name"], slug=d["slug"], workdir=d.get("workdir", str(Path.home())),
            created=d.get("created", 0.0), notes=d.get("notes", ""),
            config_overrides=d.get("config_overrides", {}), root=self.root,
        )

    def get(self, slug: str) -> Optional[Project]:
        meta = self.root / slug / "project.json"
        if meta.is_file():
            return self._from_dict(json.loads(meta.read_text()))
        return None

    def create(self, name: str, workdir: Optional[str] = None, notes: str = "",
               created_ts: float = 0.0) -> Project:
        slug = slugify(name)
        # avoid collision
        base, i = slug, 2
        while (self.root / slug).exists():
            slug = f"{base}-{i}"; i += 1
        wd = str(Path(workdir).expanduser()) if workdir else str(Path.home() / "Origin" / slug)
        Path(wd).mkdir(parents=True, exist_ok=True)
        proj = Project(name=name, slug=slug, workdir=wd, created=created_ts or time.time(),
                       notes=notes, root=self.root)
        proj.dir.mkdir(parents=True, exist_ok=True)
        proj.meta_path.write_text(json.dumps(proj.to_dict(), indent=2))
        proj.save_history([])
        return proj

    def save(self, proj: Project) -> None:
        proj.meta_path.write_text(json.dumps(proj.to_dict(), indent=2))

    # ── sharing ────────────────────────────────────────────────────────────
    def export_bytes(self, slug: str) -> bytes:
        proj = self.get(slug)
        if not proj:
            raise KeyError(slug)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.json", json.dumps(proj.to_dict(), indent=2))
            if proj.history_path.is_file():
                z.writestr("history.json", proj.history_path.read_text())
        return buf.getvalue()

    def import_bytes(self, data: bytes, new_name: Optional[str] = None) -> Project:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            meta = json.loads(z.read("project.json"))
            history = z.read("history.json").decode() if "history.json" in z.namelist() else "[]"
        name = new_name or meta.get("name", "Imported project")
        proj = self.create(name, workdir=meta.get("workdir"), notes=meta.get("notes", ""))
        proj.config_overrides = meta.get("config_overrides", {})
        self.save(proj)
        proj.history_path.write_text(history)
        return proj
