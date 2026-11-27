"""Headless sync — the entry point a scheduled task invokes.

Runs a fetch + compare (and, when asked, an apply) using the *saved* settings:
the last asset-type scope used interactively, the field mapping, and the
credentials from the OS credential store. No window is opened; everything is
written to a dated log file and stdout.

    python main.py --sync            # report only (dry run), no writes
    python main.py --sync --apply    # write warranty updates back
    python main.py --sync --export   # also write an XLSX of the comparison
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .config import load_settings
from .export import export_rows
from .sync import SyncEngine

LOG_DIR = Path.home() / "DellWarrantySync-logs"


def _log_path() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"warranty_sync_{dt.date.today():%Y%m%d}.log"


def run_sync(apply: bool = False, export: bool = False) -> int:
    """Run one unattended sync. Returns a process exit code."""
    settings = load_settings()
    logfile = _log_path()
    fh = logfile.open("a", encoding="utf-8")
    state = {"error": False}

    def write(line: str) -> None:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} {line}"
        print(text, flush=True)
        fh.write(text + "\n")
        fh.flush()

    def emit(ev: dict) -> None:
        kind = ev.get("type")
        if kind == "log":
            txt = ev["text"]
            if txt.lstrip().startswith("!") or "FAILED" in txt or "Fetch failed" in txt:
                state["error"] = True
            write(txt)
        elif kind == "toast":
            write(f"[{ev['msg']}]")

    mode = "APPLY (writes enabled)" if apply else "DRY RUN (report only)"
    write(f"=== Scheduled Dell Warranty Sync — {mode} ===")

    if settings.demo_mode:
        write("! Demo mode is on. A scheduled sync needs live mode and saved "
              "credentials. Turn off demo mode in the app, save settings, and retry.")
        fh.close()
        return 2

    type_id = settings.last_asset_type_id
    sync_all = settings.last_sync_all
    label = next((a.label for a in settings.asset_types if a.type_id == type_id), str(type_id))
    scope = "ALL asset types" if sync_all else f"'{label}' (id={type_id})"
    write(f"Scope (from your last run): {scope}")

    engine = SyncEngine(settings, emit)
    try:
        engine.fetch_and_compare(label, type_id, sync_all)
        engine.apply_updates(dry_run=not apply)
        if export and engine.rows:
            path = export_rows(engine.rows)
            write(f"Exported {len(engine.rows)} rows to {path}")
    except Exception as exc:  # pragma: no cover - defensive
        write(f"! Scheduled sync crashed: {exc}")
        fh.close()
        return 1

    write("=== Scheduled sync finished ===")
    fh.close()
    return 1 if state["error"] else 0
