"""The JavaScript ↔ Python bridge.

Instances of :class:`Api` are handed to pywebview as its ``js_api``. Every
method here is callable from the front end as ``window.pywebview.api.<name>``.
Long-running work (fetch, apply, connection tests) is run on a background
thread so the UI never blocks; results stream back to the page through
``evaluate_js`` by calling ``window.DWS.dispatch(event)``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from .config import (
    Settings,
    get_secret,
    has_secret,
    load_settings,
    save_settings,
    set_secret,
)
from .export import export_rows as export_rows_xlsx
from .sync import SyncEngine

log = logging.getLogger(__name__)

_MASK = "•" * 12  # bullet string shown when a secret is already stored


class Api:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self._window = None
        self._maximized = False
        self.log_buffer: list[str] = []
        self.engine = SyncEngine(self.settings, self._emit)

    def set_window(self, window) -> None:
        self._window = window

    # -- event push ---------------------------------------------------------
    def _emit(self, event: dict) -> None:
        if event.get("type") == "log":
            self.log_buffer.append(event["text"])
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.DWS && window.DWS.dispatch({json.dumps(event)})"
            )
        except Exception:  # pragma: no cover - window gone
            log.debug("evaluate_js failed", exc_info=True)

    def _run(self, target, *args) -> bool:
        threading.Thread(target=target, args=args, daemon=True).start()
        return True

    # -- window controls ----------------------------------------------------
    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        w = self._window
        if not w:
            return
        if self._maximized:
            if hasattr(w, "restore"):
                w.restore()
            self._maximized = False
        else:
            if hasattr(w, "maximize"):
                w.maximize()
            else:  # pragma: no cover - older pywebview
                w.toggle_fullscreen()
            self._maximized = True

    def close_window(self) -> None:
        if self._window:
            self._window.destroy()

    # -- sync ---------------------------------------------------------------
    def start_fetch(self, asset_label: str, type_id: int, sync_all: bool) -> bool:
        # Remember the scope so a scheduled headless run reuses it.
        self.settings.last_asset_type_id = int(type_id)
        self.settings.last_sync_all = bool(sync_all)
        try:
            save_settings(self.settings)
        except Exception:
            log.debug("Could not persist last selection", exc_info=True)
        return self._run(
            self.engine.fetch_and_compare, asset_label, int(type_id), bool(sync_all)
        )

    def start_apply(self, dry_run: bool) -> bool:
        return self._run(self.engine.apply_updates, bool(dry_run))

    def export_rows(self):
        rows = [r.to_dict() for r in self.engine.rows]
        if not rows:
            self._emit(
                {"type": "toast", "msg": "Nothing to export — run a comparison first.",
                 "dot": "#8a99a1"}
            )
            return None
        path = export_rows_xlsx(rows)
        self.engine._log(f"Exported {len(rows)} rows to {os.path.basename(path)}")
        self._emit(
            {"type": "toast", "msg": f"Exported {len(rows)} rows to XLSX.", "dot": "#45d17f"}
        )
        return path

    # -- connection tests ---------------------------------------------------
    def test_dell(self) -> bool:
        return self._run(self._test_dell)

    def _test_dell(self) -> None:
        self._emit({"type": "conn", "which": "dell", "status": "testing"})
        if self.settings.demo_mode:
            time.sleep(0.85)
            self._emit({"type": "conn", "which": "dell", "status": "ok"})
            self.engine._log("OK: Dell authentication successful.")
            self._emit({"type": "toast", "msg": "Dell TechDirect authenticated.", "dot": "#45d17f"})
            return
        try:
            from .dell import DellAPIClient

            s = self.settings
            ok, msg = DellAPIClient(
                s.dell_client_id, get_secret("dell_client_secret"),
                auth_url=s.dell_auth_url, warranty_url=s.dell_warranty_url,
            ).test_connection()
        except Exception as exc:
            ok, msg = False, str(exc)
        if ok:
            self._emit({"type": "conn", "which": "dell", "status": "ok"})
            self.engine._log(f"OK: {msg}")
            self._emit({"type": "toast", "msg": "Dell TechDirect authenticated.", "dot": "#45d17f"})
        else:
            self._emit({"type": "conn", "which": "dell", "status": "error"})
            self.engine._log(f"! Dell authentication failed: {msg}")
            self._emit({"type": "toast", "msg": "Dell authentication failed.", "dot": "#d5281b"})

    def test_fs(self) -> bool:
        return self._run(self._test_fs)

    def _test_fs(self) -> None:
        self._emit({"type": "conn", "which": "fs", "status": "testing"})
        if self.settings.demo_mode:
            time.sleep(0.85)
            self._emit({"type": "conn", "which": "fs", "status": "ok"})
            self.engine._log("OK: Freshservice authentication successful.")
            self._emit({"type": "toast", "msg": "Freshservice authenticated.", "dot": "#45d17f"})
            return
        try:
            from .freshservice import FreshserviceClient

            ok, msg = FreshserviceClient(
                self.settings.fs_domain, get_secret("fs_api_key")
            ).test_connection()
        except Exception as exc:
            ok, msg = False, str(exc)
        if ok:
            self._emit({"type": "conn", "which": "fs", "status": "ok"})
            self.engine._log(f"OK: {msg}")
            self._emit({"type": "toast", "msg": "Freshservice authenticated.", "dot": "#45d17f"})
        else:
            self._emit({"type": "conn", "which": "fs", "status": "error"})
            self.engine._log(f"! Freshservice authentication failed: {msg}")
            self._emit({"type": "toast", "msg": "Freshservice authentication failed.", "dot": "#d5281b"})

    def autodetect(self, asset_label: str, type_id: int) -> bool:
        return self._run(self._autodetect, asset_label, int(type_id))

    def _autodetect(self, asset_label: str, type_id: int) -> None:
        if self.settings.demo_mode:
            field = self.settings.field_map.get("warranty_expiry_date", "").replace(
                "{type_id}", str(type_id)
            )
            self.engine._log(f"Auto-detecting warranty field for '{asset_label}' (id={type_id})...")
            self.engine._log(f"  Detected warranty field from type definition: '{field}'")
            self._emit({"type": "toast", "msg": f"Detected: {field}", "dot": "#0072CE"})
            return
        try:
            from .freshservice import FreshserviceClient

            fs = FreshserviceClient(self.settings.fs_domain, get_secret("fs_api_key"))
            self.engine._log(f"Auto-detecting warranty field for '{asset_label}' (id={type_id})...")
            field = fs.discover_warranty_field(type_id, log_cb=self.engine._log)
        except Exception as exc:
            self.engine._log(f"! Auto-detect failed: {exc}")
            self._emit({"type": "toast", "msg": "Auto-detect failed — see the log.", "dot": "#d5281b"})
            return
        if field:
            self._emit({"type": "toast", "msg": f"Detected: {field}", "dot": "#0072CE"})
        else:
            self._emit({"type": "toast", "msg": "Could not auto-detect — see the log.", "dot": "#e0a000"})

    # -- configuration ------------------------------------------------------
    def load_config(self) -> dict:
        s = self.settings
        return {
            "dell_client_id": s.dell_client_id,
            "dell_auth_url": s.dell_auth_url,
            "dell_warranty_url": s.dell_warranty_url,
            "dell_secret_set": has_secret("dell_client_secret"),
            "fs_domain": s.fs_domain,
            "fs_api_key_set": has_secret("fs_api_key"),
            "field_map": s.field_map,
            "asset_types": [{"label": a.label, "type_id": a.type_id} for a in s.asset_types],
            "demo_mode": s.demo_mode,
            "mask": _MASK,
        }

    def save_config(self, data: dict) -> bool:
        s = self.settings
        data = data or {}
        s.dell_client_id = data.get("dell_client_id", s.dell_client_id)
        s.dell_auth_url = data.get("dell_auth_url", s.dell_auth_url)
        s.dell_warranty_url = data.get("dell_warranty_url", s.dell_warranty_url)
        s.fs_domain = data.get("fs_domain", s.fs_domain)
        if isinstance(data.get("field_map"), dict):
            s.field_map.update(data["field_map"])
        if "demo_mode" in data:
            s.demo_mode = bool(data["demo_mode"])

        # Secrets: only overwrite when the user actually typed something new
        # (an unchanged, masked value is left as-is).
        secret = data.get("dell_client_secret")
        if secret and secret != _MASK:
            set_secret("dell_client_secret", secret)
        key = data.get("fs_api_key")
        if key and key != _MASK:
            set_secret("fs_api_key", key)

        save_settings(s)
        self.engine._log("Settings saved to Credential Manager.")
        self._emit(
            {"type": "toast", "msg": "Settings saved to Windows Credential Manager.", "dot": "#45d17f"}
        )
        return True

    def reload_config(self) -> dict:
        self.settings = load_settings()
        self.engine.settings = self.settings
        self.engine._log("Settings reloaded from Credential Manager.")
        self._emit({"type": "toast", "msg": "Settings reloaded from store.", "dot": "#0072CE"})
        return self.load_config()

    # -- scheduling ---------------------------------------------------------
    def get_schedule(self) -> dict:
        from . import scheduler

        desc = dict(self.settings.schedule or {})
        desc["supported"] = scheduler.is_supported()
        info = scheduler.query()
        desc["task_exists"] = bool(info.get("exists"))
        if info.get("next_run"):
            desc["next_run"] = info["next_run"]
        if info.get("status"):
            desc["status"] = info["status"]
        return desc

    def create_schedule(self, opts: dict) -> dict:
        from . import scheduler

        opts = opts or {}
        freq = (opts.get("frequency") or "WEEKLY").upper()
        time_hhmm = opts.get("time") or "07:30"
        day = (opts.get("day") or "MON").upper()
        apply = bool(opts.get("apply"))

        if not scheduler.is_supported():
            self._emit({"type": "toast", "msg": "Scheduling is only available on Windows.", "dot": "#e0a000"})
            return {"ok": False, "supported": False}
        try:
            ok, msg = scheduler.create(freq, time_hhmm, apply, day)
        except ValueError as exc:
            self._emit({"type": "toast", "msg": str(exc), "dot": "#d5281b"})
            return {"ok": False, "message": str(exc)}

        if ok:
            self.settings.schedule = {
                "enabled": True, "frequency": freq, "time": time_hhmm, "apply": apply, "day": day,
            }
            save_settings(self.settings)
            when = f"{freq.title()} at {time_hhmm}" + (f" ({day})" if freq == "WEEKLY" else "")
            self.engine._log(f"Scheduled task created: {when} — "
                             + ("writes enabled" if apply else "report only"))
            self._emit({"type": "toast", "msg": "Schedule created.", "dot": "#45d17f"})
        else:
            self.engine._log(f"! Could not create schedule: {msg}")
            self._emit({"type": "toast", "msg": "Could not create schedule — see the log.", "dot": "#d5281b"})
        return {"ok": ok, "message": msg}

    def remove_schedule(self) -> dict:
        from . import scheduler

        ok, msg = scheduler.remove()
        self.settings.schedule = {}
        save_settings(self.settings)
        if ok:
            self.engine._log("Scheduled task removed.")
            self._emit({"type": "toast", "msg": "Schedule removed.", "dot": "#0072CE"})
        else:
            self.engine._log(f"! Could not remove schedule: {msg}")
            self._emit({"type": "toast", "msg": "Could not remove schedule — see the log.", "dot": "#d5281b"})
        return {"ok": ok, "message": msg}

    # -- activity log -------------------------------------------------------
    def clear_log(self) -> bool:
        self.log_buffer.clear()
        return True

    def save_log(self) -> str:
        path = Path.home() / "warranty_sync.log"
        try:
            path.write_text("\n".join(self.log_buffer) + "\n", "utf-8")
        except Exception as exc:  # pragma: no cover
            self._emit({"type": "toast", "msg": f"Could not save log: {exc}", "dot": "#d5281b"})
            return ""
        self._emit({"type": "toast", "msg": f"Log saved to {path.name}", "dot": "#45d17f"})
        return str(path)
