"""
Cross-platform Chromium-family browser detection.

Used to launch the app in a standalone window via `--app=` mode (its own
taskbar entry, no address bar / tabs) instead of a regular browser tab.
"""

import os
import shutil
import sys
from pathlib import Path

# PATH-resolvable binary names (Linux / Mac)
_CHROME_CANDIDATES = [
    "google-chrome-stable", "google-chrome",
    "chromium-browser", "chromium",
    "brave-browser", "brave",
    "microsoft-edge-stable", "microsoft-edge",
]

# Windows installs these under Program Files rather than on PATH.
_WINDOWS_CHROME_PATHS = [
    r"Google\Chrome\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
    r"Chromium\Application\chrome.exe",
]

_WINDOWS_CHROME_ROOTS = [
    "PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA",
]


def find_chrome() -> str | None:
    """Return the first available Chromium-family browser binary, or None."""
    for name in _CHROME_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p

    if sys.platform == "win32":
        for env_var in _WINDOWS_CHROME_ROOTS:
            root = os.environ.get(env_var)
            if not root:
                continue
            for rel in _WINDOWS_CHROME_PATHS:
                candidate = Path(root) / rel
                if candidate.exists():
                    return str(candidate)

    return None
