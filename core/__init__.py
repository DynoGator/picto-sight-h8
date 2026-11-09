"""
Picto-Sight H8 Core RF, Hailo-8 NPU, Demodulation & Hardware Arbitration Package.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "3.2.0"
__release_codename__ = "Wasteland-SIGINT"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_runtime_data_dir() -> Path:
    """
    Returns the writable base directory for `profiles/`, `export/`, and `logs/`.
    - Uses `PICTOSIGHT_DATA_DIR` if explicitly configured.
    - Uses `PROJECT_ROOT` when running from a user-writable git checkout.
    - Automatically falls back to `~/.local/share/picto-sight-h8` when installed
      system-wide via the Debian `.deb` package under `/opt/picto-sight-h8`.
    """
    env_dir = os.environ.get("PICTOSIGHT_DATA_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    if os.access(PROJECT_ROOT, os.W_OK):
        return PROJECT_ROOT

    user_data = Path.home() / ".local" / "share" / "picto-sight-h8"
    user_data.mkdir(parents=True, exist_ok=True)
    return user_data
