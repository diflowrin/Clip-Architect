"""Entry point for the backend when it runs as a Tauri sidecar.

Differences from upstream ``main.py``:

* Binds to 127.0.0.1 instead of ``config.listen_host`` (which defaults to
  0.0.0.0). A desktop app has no reason to accept connections from the LAN, and
  the API is unauthenticated by default.
* Takes the port from the command line, because the Rust shell picks a free one
  at launch rather than assuming 8080 is available.
* Calls ``multiprocessing.freeze_support()`` so the frozen build does not
  re-execute the whole app when a worker process is spawned on Windows.

``MPT_ROOT_DIR`` and ``MPT_RESOURCE_DIR`` must already be set by the caller; see
``app/paths.py``.
"""

import argparse
import multiprocessing
import os
import sys


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ca-desktop-backend")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MPT_PORT", "0") or 0),
        help="TCP port to listen on; 0 lets the OS choose.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind. Defaults to loopback only.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    # Import uvicorn and the app only after freeze_support(), so spawned worker
    # processes do not pay for (or re-run) application startup.
    import uvicorn
    from loguru import logger

    logger.info(f"starting backend on http://{args.host}:{args.port}")
    logger.info(f"data dir: {os.getenv('MPT_ROOT_DIR', '<source tree>')}")

    # Import the ASGI app rather than passing uvicorn the "app.asgi:app" import
    # string. The string form defers the import to uvicorn, which means
    # PyInstaller's static analysis never sees the `app` package and leaves the
    # entire backend out of the bundle - the frozen build then fails at startup
    # with 'Could not import module "app.asgi"'. Passing the object also skips
    # a redundant import. Safe here because reload is off; uvicorn only requires
    # the string form when it has to re-import in a worker process.
    from app.asgi import app as asgi_app
    from app.utils import utils

    # Which FFmpeg is in use is the single most common thing to need when
    # diagnosing a failed render: the wrong binary silently changes which
    # encoders exist. Logging it once at startup makes that answerable from a
    # user's log alone.
    logger.info(f"ffmpeg: {utils.get_ffmpeg_binary()}")

    uvicorn.run(
        app=asgi_app,
        host=args.host,
        port=args.port,
        reload=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
