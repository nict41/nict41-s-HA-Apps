"""Shared test helpers: skip Pillow-dependent tests when PIL cannot load.

The add-on image always has Pillow; a bare dev machine may not. Tests that
need PIL import `pil` from here and are skipped if it is None.
"""

import importlib.util

import pytest


def _pil_loadable() -> bool:
    """True only if Pillow's native module actually loads (not just the package)."""
    try:
        import PIL  # noqa: F401
        from PIL import Image  # noqa: F401
        from PIL import _imaging  # noqa: F401
        return True
    except Exception:
        return False


pil = _pil_loadable()

# Modules that need Pillow at import time must not be imported at all when it
# is missing; each such test module guards its own imports with this marker.
requires_pil = pytest.mark.skipif(not pil, reason="Pillow not importable in this environment")


def import_or_skip(name: str):
    """Import a module, or skip the calling test if Pillow is unavailable."""
    if not pil:
        pytest.skip("Pillow not importable in this environment")
    return importlib.import_module(name)
