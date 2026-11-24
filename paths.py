"""
Shared resolution for where persistent runtime data (the SQLite DB, MSAL
token caches, saved config files) actually lives.

Why this exists: every one of these previously used `Path(__file__).parent`,
which is correct when running from source (dev mode -- everything sits next
to the .py files, as it always has). But PyInstaller's --onefile mode
extracts the whole app to a FRESH temporary directory on every single
launch (sys._MEIPASS) -- `Path(__file__).parent` inside a frozen exe points
there, not to a stable location. Writing the database/token caches/config
there would mean "every run_store.db from the previous launch invisibly
disappears" -- i.e. your saved presets, run history, and signed-in accounts
would all be silently lost every time you closed and reopened the app.

When frozen (sys.frozen is set by PyInstaller), resolve to a stable
per-user folder instead (%LOCALAPPDATA%\\CopilotStudioLoadTester on
Windows); when running from source, behave exactly as before (next to the
script files) so the normal dev workflow is unaffected.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        d = base / "CopilotStudioLoadTester"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).parent


DATA_DIR = _resolve_data_dir()
