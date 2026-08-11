"""Where Origin stores its data (projects, memory, knowledge).

Defaults to ~/.origin. On a cloud host with ephemeral disk, set the env var
ORIGIN_DATA_DIR to a path backed by a persistent volume (e.g. /data) so
projects and conversations survive restarts and redeploys.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))
