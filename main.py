"""Dell Warranty Sync for Freshservice — desktop entry point.

Launches a frameless pywebview window hosting the web UI and wires it to the
Python backend through :class:`app.bridge.Api`.

    python main.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

WEB_INDEX = Path(__file__).resolve().parent / "web" / "index.html"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Headless scheduled run — no window.
    if "--sync" in sys.argv:
        from app.headless import run_sync
        return run_sync(apply="--apply" in sys.argv, export="--export" in sys.argv)

    try:
        import webview
    except ImportError:
        sys.stderr.write(
            "pywebview is not installed. Run:\n\n    pip install -r requirements.txt\n\n"
        )
        return 1

    from app import __build__, __version__
    from app.bridge import Api

    api = Api()
    window = webview.create_window(
        f"Dell Warranty Sync v{__version__} (build {__build__})",
        url=WEB_INDEX.as_uri(),
        js_api=api,
        width=1180,
        height=820,
        min_size=(980, 640),
        frameless=True,
        easy_drag=False,
        background_color="#003087",
    )
    api.set_window(window)
    webview.start(debug="--debug" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
