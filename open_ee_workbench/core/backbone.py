"""
core/backbone.py — eewBackbone and workbench helper imports with graceful fallbacks.
"""
import sys
from pathlib import Path

# Ensure core/ is on sys.path so the bare module names resolve.
_core_dir = str(Path(__file__).parent)
if _core_dir not in sys.path:
    sys.path.insert(0, _core_dir)

try:
    from workbench import active_name, load_workbench
    from eewBackbone import get_command, _resolve_family, _family_index, classify as _classify
    HELPERS_OK = True
except ImportError:
    HELPERS_OK = False

    def active_name():
        return None

    def load_workbench(n=None):
        raise RuntimeError("workbench helpers unavailable")

    def get_command(*a, **k):
        raise KeyError

    def _family_index():
        return {}

    def _resolve_family(f):
        return f

    def _classify(idn):
        return None
