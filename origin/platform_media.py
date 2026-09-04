"""Logo upload + serving for the white-label platform.

GCs and subcontractors can carry a real logo. To avoid adding any new server
dependency (python-multipart, an object store, etc.), the browser sends the
image as a base64 data URL in a normal JSON body; we decode it, write the bytes
to the persistent data volume, and store a short served path in the row's
logo_url column. A public GET route streams the file back.

Isolated like every other platform module: its own file, registered under a
try/except, importing only platform_db + platform_auth. A bug here cannot touch
the AI app, the portal, or the rest of the platform.

Permissions (enforced server-side on every upload):
  * gc logo  → the owner (any GC) or that GC's own admin.
  * sub logo → the owner, that sub's GC admin, or the subcontractor themselves.
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Optional, Tuple

from . import platform_db as db
from . import platform_auth as auth
from .platform_db import (
    Tenant, Subcontractor, ROLE_OWNER, ROLE_GC_ADMIN, ROLE_SUB,
)

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore

# accepted image types → file extension
_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/gif": "gif", "image/webp": "webp", "image/svg+xml": "svg",
}
_MAX_BYTES = 1_500_000  # ~1.5 MB decoded cap
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_DATAURL = re.compile(r"^data:([\w.+/-]+);base64,(.+)$", re.DOTALL)


def _logo_dir() -> Path:
    from .paths import DATA_DIR
    d = DATA_DIR / "platform_logos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decode_data_url(data_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Return (bytes, ext, error). Validates mime + size."""
    m = _DATAURL.match((data_url or "").strip())
    if not m:
        return None, None, "not a base64 image data URL"
    mime = m.group(1).lower()
    ext = _MIME_EXT.get(mime)
    if not ext:
        return None, None, f"unsupported image type: {mime}"
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except Exception:
        return None, None, "could not decode image"
    if not raw:
        return None, None, "empty image"
    if len(raw) > _MAX_BYTES:
        return None, None, "image too large (max ~1.5 MB)"
    return raw, ext, None


def _save_logo(raw: bytes, ext: str) -> str:
    """Write bytes to the volume, return the served path."""
    fname = f"{uuid.uuid4().hex}.{ext}"
    (_logo_dir() / fname).write_bytes(raw)
    return f"/platform/media/logo/{fname}"


def register_media(app) -> None:
    from fastapi import Body
    from fastapi.responses import JSONResponse, FileResponse

    def _deny():
        return JSONResponse({"error": "not allowed"}, status_code=403)

    @app.post("/platform/media/logo")
    def upload_logo(request: Request, body: dict = Body(...)):
        claims = auth.read_session(request)
        if not claims:
            return _deny()
        role = claims.get("role")
        scope = (body.get("scope") or "").strip().lower()  # "gc" | "sub"
        target_id = (body.get("id") or "").strip()
        image = body.get("image") or ""
        if scope not in ("gc", "sub") or not target_id:
            return JSONResponse({"error": "scope ('gc'|'sub') and id are required"},
                                status_code=400)
        raw, ext, err = _decode_data_url(image)
        if err:
            return JSONResponse({"error": err}, status_code=400)

        with db.session() as s:
            if scope == "gc":
                t = s.get(Tenant, target_id)
                if not t:
                    return JSONResponse({"error": "GC not found"}, status_code=404)
                # owner may set any GC; a gc_admin only their own
                if role == ROLE_OWNER or (
                        role == ROLE_GC_ADMIN and claims.get("gc_id") == t.id):
                    t.logo_url = _save_logo(raw, ext)
                    s.commit()
                    return {"ok": True, "logo_url": t.logo_url}
                return _deny()
            else:  # sub
                sub = s.get(Subcontractor, target_id)
                if not sub:
                    return JSONResponse({"error": "subcontractor not found"},
                                        status_code=404)
                allowed = (
                    role == ROLE_OWNER
                    or (role == ROLE_GC_ADMIN and claims.get("gc_id") == sub.gc_id)
                    or (role == ROLE_SUB and claims.get("sub_id") == sub.id)
                )
                if not allowed:
                    return _deny()
                sub.logo_url = _save_logo(raw, ext)
                s.commit()
                return {"ok": True, "logo_url": sub.logo_url}

    @app.get("/platform/media/logo/{filename}")
    def serve_logo(filename: str):
        # logos are non-sensitive brand marks; served to anyone, but the name is
        # validated to block path traversal.
        if not _SAFE_NAME.match(filename or "") or "/" in filename or ".." in filename:
            return JSONResponse({"error": "bad name"}, status_code=400)
        path = _logo_dir() / filename
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(path))
