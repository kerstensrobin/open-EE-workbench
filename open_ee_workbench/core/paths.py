"""
core/paths.py — shared output-folder resolution.

Every run (automation tests, screenshots, sandbox exports, standalone
scripts) writes into one dated folder per day, results/<YYYY-MM-DD>/, so a
single session's files stay together instead of scattering across separate
results/ and screenshots/ trees. No Flask/PyVISA dependency, so both the
route modules and the standalone scripts/ tools can import it directly.
"""
import os
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def today_output_dir(base: str | None = None) -> Path:
    """Return today's dated output folder, creating it if needed.

    `base` overrides the results/ root (e.g. a user-configured output path);
    the date subfolder is always appended under it.
    """
    root = Path(os.path.expanduser(base)).resolve() if base else PROJECT_ROOT / "results"
    out = root / date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    return out
