"""Windows Task Scheduler integration (via ``schtasks``).

Registers a scheduled task that runs ``main.py --sync`` (or the packaged exe)
unattended. Only the command-building is cross-platform and unit-testable;
``create`` / ``remove`` / ``query`` actually shell out and work on Windows only.

Because credentials live in the *per-user* Windows Credential Manager, the task
must run as the same user whose store holds them. The task is created to run
under the current user; to have it run while that user is logged off, tick
"Run whether user is logged on or not" in Task Scheduler (it will ask for the
account password) — we don't store passwords.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "DellWarrantySync"

VALID_FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY")
VALID_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def is_supported() -> bool:
    return os.name == "nt"


def task_run_string(apply: bool) -> str:
    """The command Task Scheduler should run (/TR value)."""
    action = "--apply" if apply else "--dry-run"
    if getattr(sys, "frozen", False):
        # Packaged exe (PyInstaller --onefile): invoke it directly.
        return f'"{sys.executable}" --sync {action}'
    # Running from source: prefer pythonw.exe (no console window).
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return f'"{runner}" "{main_py}" --sync {action}'


def build_command(frequency: str, time_hhmm: str, apply: bool, day: str | None = None) -> list[str]:
    """Build the ``schtasks /Create`` argument list (no side effects)."""
    frequency = (frequency or "").upper()
    if frequency not in VALID_FREQUENCIES:
        raise ValueError(f"frequency must be one of {VALID_FREQUENCIES}")
    if not _valid_time(time_hhmm):
        raise ValueError("time must be HH:MM (24-hour)")

    cmd = [
        "schtasks", "/Create", "/TN", TASK_NAME,
        "/TR", task_run_string(apply),
        "/SC", frequency, "/ST", time_hhmm, "/F",
    ]
    if frequency == "WEEKLY":
        d = (day or "MON").upper()
        if d not in VALID_DAYS:
            raise ValueError(f"day must be one of {VALID_DAYS}")
        cmd += ["/D", d]
    elif frequency == "MONTHLY":
        cmd += ["/D", "1"]  # 1st of the month
    return cmd


def _valid_time(value: str) -> bool:
    try:
        h, m = value.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, AttributeError):
        return False


def _run(cmd: list[str]) -> tuple[bool, str]:
    if not is_supported():
        return False, "Scheduling is only available on Windows (Task Scheduler)."
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # pragma: no cover - platform specific
        return False, f"Could not run schtasks: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def create(frequency: str, time_hhmm: str, apply: bool, day: str | None = None) -> tuple[bool, str]:
    return _run(build_command(frequency, time_hhmm, apply, day))


def remove() -> tuple[bool, str]:
    return _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])


def query() -> dict:
    """Return {'exists': bool, 'next_run': str, 'status': str, 'raw': str}."""
    if not is_supported():
        return {"exists": False, "supported": False}
    ok, out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    if not ok:
        return {"exists": False, "supported": True}
    info: dict = {"exists": True, "supported": True, "raw": out}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "next run time":
            info["next_run"] = val
        elif key == "status":
            info["status"] = val
        elif key == "task to run":
            info["task_to_run"] = val
    return info
