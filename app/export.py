"""XLSX export of a comparison result, via openpyxl."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

_HEADERS = [
    ("Asset", 20),
    ("Type", 14),
    ("Service Tag", 14),
    ("FS End", 13),
    ("Dell End", 13),
    ("Service Level", 40),
    ("System", 18),
    ("Status", 14),
    ("Detail", 50),
]

_HEADER_FILL = PatternFill("solid", fgColor="005EB8")
_HEADER_FONT = Font(color="FFFFFF", bold=True)


def default_export_path() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return str(Path.home() / f"dell_warranty_sync_{stamp}.xlsx")


def export_rows(rows: list[dict], path: str | None = None) -> str:
    """Write ``rows`` to an .xlsx file and return its path."""
    path = path or default_export_path()
    wb = Workbook()
    ws = wb.active
    ws.title = "Warranty Sync"

    for col, (title, width) in enumerate(_HEADERS, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = "A2"

    for r in rows:
        ws.append([
            r.get("name", ""),
            r.get("type", ""),
            r.get("tag", ""),
            r.get("fsEnd", ""),
            r.get("dellEnd", ""),
            r.get("level", ""),
            r.get("system", ""),
            r.get("status", ""),
            r.get("detail", ""),
        ])

    wb.save(path)
    return path
