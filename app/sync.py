"""The sync engine.

Compares Freshservice warranty data against Dell TechDirect and, when asked,
writes the differences back. It drives the two clients but has no UI
dependency — it reports everything through a single ``emit`` callback so it can
be reused from the Admin Suite with a different front end.

When ``settings.demo_mode`` is on (the default) the engine makes no network
calls at all: it replays the bundled sample dataset and a representative run
sequence so the whole UI is explorable without credentials.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable

from . import dell as dell_mod
from .config import Settings, get_secret
from .dell import DellClient
from .freshservice import FreshserviceClient

log = logging.getLogger(__name__)

Emit = Callable[[dict], None]

# Status vocabulary shared with the front end.
STATUS_UPDATE = "update"
STATUS_MATCH = "match"
STATUS_SYNCED = "synced"
STATUS_NO_DATA = "no-data"
STATUS_NO_TAG = "no-tag"
STATUS_BAD_TAG = "bad-tag"
STATUS_ERROR = "error"

#: Fixed reference date for the demo dataset so the DAYS column is stable.
DEMO_TODAY = "2026-07-02"
DEMO_FIELD = "warranty_expiry_date_47"


@dataclass
class Row:
    """One asset's comparison result, in the shape the front end renders."""

    id: int
    name: str
    type: str
    tag: str
    fsEnd: str
    dellEnd: str
    level: str
    system: str
    status: str
    detail: str = ""
    display_id: int = 0  # Freshservice display_id — the write-back target

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Demo data — mirrors the design mock so the UI looks identical out of the box.
# ---------------------------------------------------------------------------
def _demo_rows() -> list[Row]:
    data = [
        (1, "CORP-CLN-04821", "Laptop", "7KJ2QM3", "2024-11-02", "2026-11-02", "ProSupport Plus: Next Business Day", "Latitude 5540", "update", ""),
        (2, "CORP-CLN-04822", "Laptop", "9XR4TB1", "2026-03-15", "2026-03-15", "ProSupport: Next Business Day", "Latitude 5450", "match", ""),
        (3, "CORP-WRD-01193", "Desktop", "3H7NLP2", "", "2027-06-30", "Basic Onsite Service", "OptiPlex 7010", "update", ""),
        (4, "CORP-LAB-00874", "Workstation", "QJ8ZK5T", "2025-09-01", "2025-09-01", "ProSupport Plus: 4-Hour Mission Critical", "Precision 3591", "match", ""),
        (5, "CORP-CLN-04980", "Laptop", "5MW3RD9", "2023-01-20", "2025-01-20", "ProSupport: Next Business Day", "Latitude 5420", "update", ""),
        (6, "CORP-THE-00233", "Desktop", "8BFK2N6", "", "", "", "", "no-data", "Dell returned no entitlement data for this tag"),
        (7, "CORP-CLN-05001", "Laptop", "ABC12", "", "", "", "", "bad-tag", "'ABC12' is 5 characters; Dell tags are exactly 7 — skipped"),
        (8, "CORP-ADM-00612", "Desktop", "", "2024-05-05", "", "", "", "no-tag", "No serial number on asset"),
        (9, "CORP-CLN-04710", "Laptop", "2QT9WX4", "2025-12-11", "2027-12-11", "ProSupport Plus: Next Business Day", "Latitude 7450", "update", ""),
        (10, "CORP-RAD-00088", "Workstation", "6LP1VH8", "2026-08-19", "2026-08-19", "ProSupport: 4-Hour Mission Critical", "Precision 5860", "match", ""),
        (11, "CORP-CLN-05130", "Laptop", "4NK7DM2", "", "2028-02-28", "ProSupport Plus: Next Business Day", "Latitude 5550", "update", ""),
        (12, "CORP-PHR-00341", "Desktop", "7GW2XR5", "2022-10-03", "2024-10-03", "Basic Hardware Service", "OptiPlex 5090", "update", ""),
        (13, "CORP-CLN-04655", "Laptop", "9DZ3PQ7", "2027-04-14", "2027-04-14", "ProSupport: Next Business Day", "Latitude 5340", "match", ""),
        (14, "CORP-ITS-00019", "Server", "3XV8KB1", "2023-07-22", "2026-07-22", "ProSupport Plus: 4-Hour Mission Critical", "PowerEdge R650", "update", ""),
        (15, "CORP-CLN-05201", "Laptop", "8HR4NT6", "", "2027-01-30", "ProSupport: Next Business Day", "Latitude 5531", "error", "HTTP 400: field 'warranty_expiry_date_47' rejected by Freshservice"),
        (16, "CORP-CLN-04533", "Laptop", "5TW9LK3", "2026-01-30", "2026-01-30", "ProSupport: Next Business Day", "Latitude 5531", "match", ""),
    ]
    return [Row(*d) for d in data]


# (delay_ms, progress, label, log_line)
_DEMO_FETCH_SEQUENCE_LAPTOP = [
    (0, 8, "Connecting to Freshservice…", "Fetching 'Laptop' assets from Freshservice (id=47)..."),
    (550, 22, "Probing filter strategy…", "Probing filter strategy: filter=asset_type_id (id)"),
    (400, 33, "Paginating assets…", "  + filter=asset_type_id confirmed: page 1 has 100 assets, all matching"),
    (500, 46, "Paginating assets…", "Page 1: 100 returned, 100 kept"),
    (400, 56, "Paginating assets…", "Page 2: 62 returned, 62 kept"),
    (350, 63, "Assets fetched", "Fetched 16 assets (14 with serial numbers, 2 without)."),
    (400, 71, "Querying Dell TechDirect…", "Querying Dell for 13 service tags (7-char Dell format)"),
    (300, 75, "Querying Dell TechDirect…", "  Skipping 1 asset with a serial that isn't 7 characters."),
    (550, 88, "Querying Dell batch 1/1…", "Querying Dell batch 1/1 (13 tags)"),
    (550, 96, "Detecting warranty field…", "Warranty field detected: 'warranty_expiry_date_47'"),
    (350, 100, "Comparison complete", "Comparison complete: 7 update(s), 5 already in sync."),
]


class SyncEngine:
    """Compare and apply warranty data. One instance per app session."""

    def __init__(self, settings: Settings, emit: Emit) -> None:
        self.settings = settings
        self.emit = emit
        self.rows: list[Row] = []
        self.today: str = DEMO_TODAY if settings.demo_mode else date.today().isoformat()
        self.warranty_field: str = DEMO_FIELD

    # -- helpers ------------------------------------------------------------
    def _phase(self, phase: str) -> None:
        self.emit({"type": "phase", "phase": phase})

    def _progress(self, value: int, label: str) -> None:
        self.emit({"type": "progress", "value": value, "label": label})

    def _log(self, text: str) -> None:
        self.emit({"type": "log", "text": text})

    def _toast(self, msg: str, dot: str) -> None:
        self.emit({"type": "toast", "msg": msg, "dot": dot})

    def _publish_rows(self) -> None:
        self.emit(
            {
                "type": "rows",
                "rows": [r.to_dict() for r in self.rows],
                "today": self.today,
                "field": self.warranty_field,
            }
        )

    def _warranty_field_for(self, type_id: int) -> str:
        template = self.settings.field_map.get("warranty_expiry_date", "")
        return template.replace("{type_id}", str(type_id)) if template else ""

    # -- fetch + compare ----------------------------------------------------
    def fetch_and_compare(self, asset_label: str, type_id: int, sync_all: bool) -> None:
        if not self.settings.demo_mode:
            missing = []
            if not get_secret("dell_client_secret"):
                missing.append("Dell client secret")
            if not get_secret("fs_api_key"):
                missing.append("Freshservice API key")
            if missing:
                self._log("! Live mode needs credentials: " + ", ".join(missing)
                          + ". Save them on the Configuration tab.")
                self._phase("idle")
                self._toast("Add credentials on Configuration to use live mode.", "#e0a000")
                return

        self._phase("fetching")
        self._progress(0, "Connecting to Freshservice…")
        try:
            if self.settings.demo_mode:
                self._demo_fetch(sync_all)
            else:
                self._real_fetch(asset_label, type_id, sync_all)
        except Exception as exc:  # pragma: no cover - network errors
            log.exception("Fetch failed")
            self._log(f"! Fetch failed: {exc}")
            self._phase("idle")
            self._toast("Fetch failed — see the activity log.", "#d5281b")
            return

        self._phase("results")
        pending = sum(1 for r in self.rows if r.status == STATUS_UPDATE)
        self._toast(f"Comparison ready — {pending} assets need updating.", "#0072CE")

    def _demo_fetch(self, sync_all: bool) -> None:
        self.rows = []
        self.today = DEMO_TODAY
        self.warranty_field = DEMO_FIELD
        label = "ALL asset types" if sync_all else "'Laptop' assets"
        for delay_ms, pct, step_label, line in _DEMO_FETCH_SEQUENCE_LAPTOP:
            time.sleep(delay_ms / 1000.0)
            self._progress(pct, step_label)
            # The very first line reflects the chosen scope.
            self._log(line.replace("'Laptop' assets", label) if "Fetching" in line else line)
        time.sleep(0.25)
        self.rows = _demo_rows()
        self._publish_rows()

    def _real_fetch(self, asset_label: str, type_id: int, sync_all: bool) -> None:
        s = self.settings
        self.today = date.today().isoformat()
        fs = FreshserviceClient(s.fs_domain, get_secret("fs_api_key"), log=self._log)
        dell = DellClient(
            s.dell_client_id,
            get_secret("dell_client_secret"),
            auth_url=s.dell_auth_url,
            warranty_url=s.dell_warranty_url,
            log=self._log,
        )

        self.warranty_field = self._warranty_field_for(type_id)
        scope = None if sync_all else type_id
        scope_label = "ALL asset types" if sync_all else f"'{asset_label}' assets"
        self._log(f"Fetching {scope_label} from Freshservice (id={type_id})...")

        def on_page(page: int, returned: int, kept: int) -> None:
            self._progress(min(60, 20 + page * 10), "Paginating assets…")
            self._log(f"Page {page}: {returned} returned, {kept} kept")

        assets = fs.list_assets(scope, warranty_field=self.warranty_field, progress=on_page)
        with_tags = [a for a in assets if a.service_tag]
        self._log(f"Fetched {len(assets)} assets ({len(with_tags)} with serial numbers, "
                  f"{len(assets) - len(with_tags)} without).")

        valid_tags = [a.service_tag for a in with_tags if dell_mod.is_valid_service_tag(a.service_tag)]
        self._progress(71, "Querying Dell TechDirect…")
        self._log(f"Querying Dell for {len(valid_tags)} service tags (7-char Dell format)")
        entitlements = dell.get_entitlements(valid_tags)
        self._log(f"Warranty field detected: '{self.warranty_field}'")

        self.rows = [self._compare(i + 1, a, entitlements) for i, a in enumerate(assets)]
        updates = sum(1 for r in self.rows if r.status == STATUS_UPDATE)
        matches = sum(1 for r in self.rows if r.status == STATUS_MATCH)
        self._progress(100, "Comparison complete")
        self._log(f"Comparison complete: {updates} update(s), {matches} already in sync.")
        self._publish_rows()

    def _compare(self, row_id: int, asset, entitlements: dict) -> Row:
        tag = (asset.service_tag or "").strip()
        base = dict(
            id=row_id, name=asset.name, type=_type_label(asset.asset_type_id, self.settings),
            tag=tag, fsEnd=asset.warranty_expiry, dellEnd="", level="", system="",
            display_id=asset.display_id,
        )
        if not tag:
            return Row(**base, status=STATUS_NO_TAG, detail="No serial number on asset")
        if not dell_mod.is_valid_service_tag(tag):
            return Row(**base, status=STATUS_BAD_TAG,
                       detail=f"'{tag}' is {len(tag)} characters; Dell tags are exactly 7 — skipped")
        ent = entitlements.get(tag)
        if not ent or not ent.warranty_end:
            return Row(**base, status=STATUS_NO_DATA,
                       detail="Dell returned no entitlement data for this tag")
        base.update(dellEnd=ent.warranty_end, level=ent.service_level, system=ent.system_description)
        status = STATUS_MATCH if asset.warranty_expiry == ent.warranty_end else STATUS_UPDATE
        return Row(**base, status=status)

    # -- apply --------------------------------------------------------------
    def apply_updates(self, dry_run: bool) -> None:
        pending = [r for r in self.rows if r.status == STATUS_UPDATE]
        if not pending:
            self._toast("Nothing to apply — everything is already in sync.", "#8a99a1")
            return

        if dry_run:
            self._log(f"DRY RUN: would update {len(pending)} assets:")
            for r in pending:
                self._log(f"  ~ {r.name} ({r.tag}): {r.fsEnd or '(empty)'} -> {r.dellEnd}")
            self._toast(
                f"Dry run: {len(pending)} assets would be updated. Untick Dry run to write.",
                "#e0a000",
            )
            return

        if self.settings.demo_mode:
            self._demo_apply(pending)
        else:
            self._real_apply(pending)

    def _demo_apply(self, pending: list[Row]) -> None:
        self._phase("applying")
        self._progress(0, f"Writing {len(pending)} assets to Freshservice…")
        self._log(f"Applying {len(pending)} updates with 6 field mapping(s)")
        total = len(pending)
        for i, r in enumerate(pending, start=1):
            time.sleep(0.38)
            r.status = STATUS_SYNCED
            r.detail = "Updated 6 field(s)"
            self.emit({"type": "row_update", "id": r.id, "status": r.status, "detail": r.detail})
            self._progress(round(i / total * 100), f"Writing to Freshservice… ({i}/{total})")
            self._log(f"  + {r.name} ({r.tag}): 6 field(s) updated")
        time.sleep(0.3)
        self._phase("done")
        self._log(f"Sync complete: {total} updated, 0 failed.")
        self._toast(f"Sync complete — {total} assets updated.", "#45d17f")

    def _real_apply(self, pending: list[Row]) -> None:
        s = self.settings
        fs = FreshserviceClient(s.fs_domain, get_secret("fs_api_key"), log=self._log)
        mappings = {k: v for k, v in s.field_map.items() if v}
        self._phase("applying")
        self._progress(0, f"Writing {len(pending)} assets to Freshservice…")
        self._log(f"Applying {len(pending)} updates with {len(mappings)} field mapping(s)")
        total = len(pending)
        failed = 0
        for i, r in enumerate(pending, start=1):
            type_id = _type_id(r.type, s)
            fields = self._build_fields(r, mappings, type_id)
            try:
                fs.update_asset_fields(r.display_id or r.id, fields)
                r.status = STATUS_SYNCED
                r.detail = f"Updated {len(fields)} field(s)"
                self._log(f"  + {r.name} ({r.tag}): {len(fields)} field(s) updated")
            except Exception as exc:
                failed += 1
                r.status = STATUS_ERROR
                r.detail = str(exc)
                self._log(f"! {r.name} ({r.tag}): {exc}")
            self.emit({"type": "row_update", "id": r.id, "status": r.status, "detail": r.detail})
            self._progress(round(i / total * 100), f"Writing to Freshservice… ({i}/{total})")
        self._phase("done")
        self._log(f"Sync complete: {total - failed} updated, {failed} failed.")
        dot = "#45d17f" if failed == 0 else "#e0a000"
        self._toast(f"Sync complete — {total - failed} assets updated.", dot)

    def _build_fields(self, row: Row, mappings: dict, type_id: int) -> dict:
        subst = lambda tmpl: tmpl.replace("{type_id}", str(type_id))
        values = {
            "warranty_expiry_date": row.dellEnd,
            "service_level": row.level,
            "system_description": row.system,
        }
        fields: dict = {}
        for key, tmpl in mappings.items():
            if key in values and values[key]:
                fields[subst(tmpl)] = values[key]
        return fields


def _type_label(type_id: int, settings: Settings) -> str:
    for at in settings.asset_types:
        if at.type_id == type_id:
            return at.label
    return "Asset"


def _type_id(label: str, settings: Settings) -> int:
    for at in settings.asset_types:
        if at.label == label:
            return at.type_id
    return 0
