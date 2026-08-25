"""Root/resource path resolution for both source runs and frozen desktop builds.

Upstream derives every path from ``__file__``. That works when the project runs
from a checked-out source tree, but a PyInstaller bundle unpacks its code into a
temporary ``_MEIPASS`` directory that is wiped on exit, so ``config.toml`` and
``storage/`` would silently vanish between runs.

The desktop shell therefore points the two locations at different places:

``MPT_ROOT_DIR``
    Writable per-user data directory holding ``config.toml`` and ``storage/``.
``MPT_RESOURCE_DIR``
    Read-only ``resource/`` tree (fonts, songs, public). Kept separate so the
    ~190 MB of bundled assets are not duplicated into the data directory.

Both fall back to the source-tree layout, so running from a checkout or the
existing Docker images behaves exactly as before.
"""

import os

# app/paths.py -> app/ -> project root
_SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _from_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        return ""
    # A relative override would be resolved against the sidecar's working
    # directory, which the desktop shell does not control. Requiring an
    # absolute path keeps the failure obvious instead of scattering task
    # output into whichever directory the process happened to start in.
    return os.path.abspath(os.path.expanduser(value))


def resolve_root_dir() -> str:
    """Directory holding ``config.toml`` and ``storage/``."""
    return _from_env("MPT_ROOT_DIR") or _SOURCE_ROOT


def resolve_resource_dir() -> str:
    """Directory holding the bundled ``fonts/``, ``songs/`` and ``public/``."""
    return _from_env("MPT_RESOURCE_DIR") or os.path.join(resolve_root_dir(), "resource")
