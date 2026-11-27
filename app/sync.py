"""The sync engine.

Two layers:
  * :class:`CoreSyncEngine` — the proven, UI-free comparison/apply logic ported
    from the standalone tool. It drives :class:`~app.dell.DellAPIClient` and
    :class:`~app.freshservice.FreshserviceClient` and talks only through
    ``log_cb`` / ``progress_cb`` callbacks, so it can be reused from the Admin
    Suite.
  * :class:`SyncEngine` — the app orchestrator the UI bridge talks to. It owns
    demo mode (bundled sample data, no network) and, in live mode, runs the
    core engine and streams everything back to the page as events.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable

from .config import Settings, get_secret
from .dell import DELL_TAG_PATTERN, DellAPIClient
from .freshservice import FreshserviceClient, FSAsset

log = logging.getLogger(__name__)

Emit = Callable[[dict], None]

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


# ===========================================================================
# Core engine (ported) — comparison + apply, callback-driven, UI-free.
# ===========================================================================
@dataclass
class SyncRow:
    display_id: int
    name: str
    asset_type_id: int
    asset_type_name: str
    service_tag: str
    fs_warranty_end: dt.date | None
    dell_warranty_end: dt.date | None
    dell_service_level: str
    dell_system_description: str
    status: str
    detail: str = ""
    dell_ship_date: dt.date | None = None
    dell_product_lob: str = ""
    dell_entitlement_type: str = ""
    dell_warranty_start: dt.date | None = None
    type_field_keys: tuple[str, ...] = ()

    @property
    def needs_update(self) -> bool:
        return self.status == STATUS_UPDATE


class CoreSyncEngine:
    """Glues the Dell and Freshservice clients together (ported logic)."""

    def __init__(self, dell: DellAPIClient, fs: FreshserviceClient) -> None:
        self.dell = dell
        self.fs = fs
        self.discovered_warranty_field: str | None = None

    def build_comparison(
        self,
        assets: list[FSAsset],
        log_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[SyncRow]:
        if assets:
            self.discovered_warranty_field = self.fs.discover_warranty_field(
                assets[0].asset_type_id, sample_assets=assets, log_cb=log_cb,
            )

        tags = [
            a.serial_number.strip() for a in assets
            if a.serial_number and DELL_TAG_PATTERN.match(a.serial_number.strip())
        ]
        if log_cb:
            skipped = sum(
                1 for a in assets
                if a.serial_number and not DELL_TAG_PATTERN.match(a.serial_number.strip())
            )
            log_cb(f"Querying Dell for {len(tags)} service tags (7-char Dell format)")
            if skipped:
                log_cb(f"  Skipping {skipped} asset(s) with a serial number that isn't "
                       f"exactly 7 characters (not a valid Dell service tag format).")
        warranty_map = self.dell.get_warranties(
            tags, progress_cb=progress_cb, log_cb=log_cb, cancel_event=cancel_event,
        )

        rows: list[SyncRow] = []
        for a in assets:
            raw_tag = a.serial_number.strip() if a.serial_number else ""
            if not raw_tag:
                rows.append(self._blank_row(a, "", STATUS_NO_TAG, "No serial number on asset"))
                continue
            if not DELL_TAG_PATTERN.match(raw_tag):
                rows.append(self._blank_row(
                    a, raw_tag, STATUS_BAD_TAG,
                    f"'{raw_tag}' is {len(raw_tag)} character(s); Dell service tags are exactly 7 - skipped",
                ))
                continue
            tag = raw_tag.upper()
            dell_data = warranty_map.get(tag)
            if not dell_data or not dell_data.latest_end_date:
                failed = tag in getattr(self.dell, "last_failed_tags", set())
                rows.append(self._blank_row(
                    a, tag,
                    STATUS_ERROR if failed else STATUS_NO_DATA,
                    "Dell query failed for this batch (network/API error) - re-run to retry"
                    if failed else "Dell returned no entitlement data",
                ))
                continue
            new_end = dell_data.latest_end_date
            if a.current_warranty_end == new_end:
                status, detail = STATUS_MATCH, "Already in sync"
            else:
                status = STATUS_UPDATE
                detail = f"FS: {a.current_warranty_end or '(empty)'} -> Dell: {new_end}"
            rows.append(SyncRow(
                display_id=a.display_id, name=a.name, asset_type_id=a.asset_type_id,
                asset_type_name=a.asset_type_name, service_tag=tag,
                fs_warranty_end=a.current_warranty_end, dell_warranty_end=new_end,
                dell_service_level=dell_data.latest_service_level,
                dell_system_description=dell_data.system_description,
                status=status, detail=detail,
                dell_ship_date=dell_data.ship_date,
                dell_product_lob=dell_data.product_lob_description,
                dell_entitlement_type=(dell_data.entitlements[0].entitlement_type if dell_data.entitlements else ""),
                dell_warranty_start=(
                    min((e.start_date for e in dell_data.entitlements if e.start_date), default=None)
                    if dell_data.entitlements else None
                ),
                type_field_keys=tuple((a.raw.get("type_fields") or {}).keys()),
            ))
        return rows

    @staticmethod
    def _blank_row(a: FSAsset, tag: str, status: str, detail: str) -> SyncRow:
        return SyncRow(
            display_id=a.display_id, name=a.name, asset_type_id=a.asset_type_id,
            asset_type_name=a.asset_type_name, service_tag=tag,
            fs_warranty_end=a.current_warranty_end, dell_warranty_end=None,
            dell_service_level="", dell_system_description="", status=status, detail=detail,
        )

    def apply_updates(
        self,
        rows: list[SyncRow],
        field_mappings: dict[str, str],
        log_cb: Callable[[str], None] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
        include_matched: bool = False,
    ) -> tuple[int, int]:
        pending = [r for r in rows if r.needs_update or (include_matched and r.status == STATUS_MATCH)]
        ok = fail = 0
        if log_cb:
            log_cb(f"Field mappings: {list(field_mappings.keys())}")

        run_bad_bases: set[str] = set()
        run_resolved: dict[str, tuple[str, str]] = {"end_of_life": ("bare", "root")}
        skip_logged: set[tuple[str, int]] = set()

        def _base(key: str) -> str:
            return re.sub(r"_\d+$", "", key)

        for i, row in enumerate(pending, start=1):
            if cancel_event and cancel_event.is_set():
                if log_cb:
                    log_cb("Sync cancelled by user.")
                break

            raw_key_by_base = {_base(k): k for k in row.type_field_keys}

            def resolve_key(template: str) -> str | None:
                candidate = template.replace("{type_id}", str(row.asset_type_id))
                b = _base(candidate)
                if b in raw_key_by_base:
                    return raw_key_by_base[b]
                if re.search(r"_\d+$", candidate) and row.type_field_keys:
                    return None
                return candidate

            updates: dict[str, Any] = {}
            for dell_field, fs_template in field_mappings.items():
                fs_field = resolve_key(fs_template)
                if fs_field is None:
                    b = _base(fs_template.replace("{type_id}", str(row.asset_type_id)))
                    if log_cb and (b, row.asset_type_id) not in skip_logged:
                        skip_logged.add((b, row.asset_type_id))
                        type_label = row.asset_type_name or f"type {row.asset_type_id}"
                        log_cb(f"  - '{b}' not present on '{type_label}' assets - skipping that field for this type")
                    continue
                value: Any = None
                if dell_field == "warranty_expiry_date" and row.dell_warranty_end:
                    value = row.dell_warranty_end.isoformat()
                elif dell_field == "service_level" and row.dell_service_level:
                    value = row.dell_service_level
                elif dell_field == "system_description" and row.dell_system_description:
                    value = row.dell_system_description
                elif dell_field == "ship_date" and row.dell_ship_date:
                    value = row.dell_ship_date.isoformat()
                elif dell_field == "warranty_start_date" and row.dell_warranty_start:
                    value = row.dell_warranty_start.isoformat()
                elif dell_field == "end_of_life" and row.dell_warranty_end:
                    value = row.dell_warranty_end.isoformat()
                elif dell_field == "entitlement_type" and row.dell_entitlement_type:
                    value = row.dell_entitlement_type
                elif dell_field == "product_lob" and row.dell_product_lob:
                    value = row.dell_product_lob
                if value is not None:
                    updates[fs_field] = value

            if not updates:
                if log_cb:
                    log_cb(f"  - {row.name} ({row.service_tag}): no mapped fields have values")
                continue

            this_asset_key = {_base(k): k for k in updates}
            concrete_forms: dict[str, tuple[str, str]] = {}
            for b, (kind, loc) in run_resolved.items():
                if b in this_asset_key:
                    name = this_asset_key[b] if kind == "as_configured" else b
                    concrete_forms[b] = (name, loc)

            success, msg, newly_bad, newly_resolved = self.fs.update_warranty(
                row.display_id, updates, known_bad=run_bad_bases, resolved_forms=concrete_forms,
            )
            for base, (name, loc) in newly_resolved.items():
                kind = "as_configured" if name == this_asset_key.get(base) else "bare"
                prev = run_resolved.get(base)
                if prev is not None:
                    prev_kind, prev_loc = prev
                    prev_name = this_asset_key.get(base, base) if prev_kind == "as_configured" else base
                    if prev_loc == loc and prev_name == name:
                        continue
                run_resolved[base] = (kind, loc)
                if log_cb and (kind == "bare" or loc == "root"):
                    log_cb(f"  * Resolved '{base}' -> writes as "
                           f"'{name if kind == 'as_configured' else base}' "
                           f"in {'root' if loc == 'root' else 'type_fields'}")
            if newly_bad:
                run_bad_bases |= newly_bad
                if log_cb:
                    log_cb(f"  * Field(s) not writable in any known form, skipping for the "
                           f"rest of this run: {sorted(newly_bad)}")
            if success:
                ok += 1
                row.status = STATUS_SYNCED
                row.detail = msg
                if log_cb:
                    log_cb(f"  + {row.name} ({row.service_tag}): {msg}")
            else:
                fail += 1
                row.status = STATUS_ERROR
                row.detail = msg
                if log_cb:
                    log_cb(f"  ! {row.name} ({row.service_tag}) FAILED: {msg}")
            if progress_cb:
                progress_cb(i, len(pending))
            time.sleep(0.4)
        return ok, fail


# ===========================================================================
# Demo data — mirrors the design mock so the UI looks identical out of the box.
# ===========================================================================
@dataclass
class _DemoRow:
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
    display_id: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _demo_rows() -> list[dict]:
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
    return [_DemoRow(*d).to_dict() for d in data]


_DEMO_FETCH_SEQUENCE = [
    (0, 8, "Connecting to Freshservice…", "Fetching %SCOPE% from Freshservice (id=47)..."),
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


# ===========================================================================
# Orchestrator — demo vs live, streaming events to the UI.
# ===========================================================================
class SyncEngine:
    def __init__(self, settings: Settings, emit: Emit) -> None:
        self.settings = settings
        self.emit = emit
        self.rows: list[dict] = []          # what the UI renders and export uses
        self._live_rows: list[SyncRow] = []  # live mode only, for apply
        self.today = DEMO_TODAY if settings.demo_mode else date.today().isoformat()
        self.warranty_field = DEMO_FIELD
        self._cancel: threading.Event | None = None

    # -- events -------------------------------------------------------------
    def _phase(self, phase: str) -> None:
        self.emit({"type": "phase", "phase": phase})

    def _progress(self, value: int, label: str) -> None:
        self.emit({"type": "progress", "value": value, "label": label})

    def _log(self, text: str) -> None:
        self.emit({"type": "log", "text": text})

    def _toast(self, msg: str, dot: str) -> None:
        self.emit({"type": "toast", "msg": msg, "dot": dot})

    def _publish_rows(self) -> None:
        self.emit({"type": "rows", "rows": self.rows, "today": self.today, "field": self.warranty_field})

    def _type_label(self, type_id: int) -> str:
        for at in self.settings.asset_types:
            if at.type_id == type_id:
                return at.label
        return str(type_id)

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
            elif self._live_fetch(asset_label, type_id, sync_all):
                self._phase("idle")
                self._toast("No assets returned for that selection.", "#8a99a1")
                return
        except Exception as exc:  # pragma: no cover - network/tenant errors
            log.exception("Fetch failed")
            self._log(f"! Fetch failed: {exc}")
            self._phase("idle")
            self._toast("Fetch failed — see the activity log.", "#d5281b")
            return

        self._phase("results")
        pending = sum(1 for r in self.rows if r["status"] == STATUS_UPDATE)
        self._toast(f"Comparison ready — {pending} assets need updating.", "#0072CE")

    def _demo_fetch(self, sync_all: bool) -> None:
        self.rows = []
        self.today = DEMO_TODAY
        self.warranty_field = DEMO_FIELD
        label = "ALL asset types" if sync_all else "'Laptop' assets"
        for delay_ms, pct, step_label, line in _DEMO_FETCH_SEQUENCE:
            time.sleep(delay_ms / 1000.0)
            self._progress(pct, step_label)
            self._log(line.replace("%SCOPE%", label) if "Fetching" in line else line)
        time.sleep(0.25)
        self.rows = _demo_rows()
        self._publish_rows()

    def _live_fetch(self, asset_label: str, type_id: int, sync_all: bool) -> bool:
        """Run the real fetch/compare. Returns True when no assets were found."""
        s = self.settings
        self.today = date.today().isoformat()
        fs = FreshserviceClient(s.fs_domain, get_secret("fs_api_key"))
        dell = DellAPIClient(
            s.dell_client_id, get_secret("dell_client_secret"),
            auth_url=s.dell_auth_url, warranty_url=s.dell_warranty_url,
        )
        core = CoreSyncEngine(dell, fs)

        if sync_all:
            self._log("Fetching ALL asset types from Freshservice...")
            assets = fs.list_assets(None, None, log_cb=self._log, cancel_event=self._cancel)
            name_by_id = {at.type_id: at.label for at in s.asset_types}
            for a in assets:
                if not a.asset_type_name:
                    a.asset_type_name = name_by_id.get(a.asset_type_id, "")
        else:
            self._log(f"Fetching '{asset_label}' assets from Freshservice (id={type_id})...")
            assets = fs.list_assets(type_id, asset_label, log_cb=self._log, cancel_event=self._cancel)
            for a in assets:
                a.asset_type_name = asset_label

        with_tag = sum(1 for a in assets if a.serial_number)
        self._log(f"Fetched {len(assets)} assets ({with_tag} with serial numbers, "
                  f"{len(assets) - with_tag} without).")
        if not assets:
            self._log("No assets returned for that selection.")
            return True

        def dell_progress(c: int, t: int) -> None:
            self._progress(int(c / t * 100) if t else 0, "Querying Dell TechDirect…")

        rows = core.build_comparison(assets, log_cb=self._log, progress_cb=dell_progress, cancel_event=self._cancel)
        self._live_rows = rows
        self.warranty_field = core.discovered_warranty_field or "(not detected)"
        updates = sum(1 for r in rows if r.status == STATUS_UPDATE)
        matches = sum(1 for r in rows if r.status == STATUS_MATCH)
        self._progress(100, "Comparison complete")
        self._log(f"Comparison complete: {updates} update(s), {matches} already in sync.")
        self.rows = [self._row_to_dict(r) for r in rows]
        self._publish_rows()
        return False

    def _row_to_dict(self, r: SyncRow) -> dict:
        return {
            "id": r.display_id,
            "display_id": r.display_id,
            "name": r.name,
            "type": r.asset_type_name or self._type_label(r.asset_type_id),
            "tag": r.service_tag,
            "fsEnd": r.fs_warranty_end.isoformat() if r.fs_warranty_end else "",
            "dellEnd": r.dell_warranty_end.isoformat() if r.dell_warranty_end else "",
            "level": r.dell_service_level,
            "system": r.dell_system_description,
            "status": r.status,
            "detail": r.detail,
        }

    # -- apply --------------------------------------------------------------
    def apply_updates(self, dry_run: bool) -> None:
        pending = [r for r in self.rows if r["status"] == STATUS_UPDATE]
        if not pending:
            self._toast("Nothing to apply — everything is already in sync.", "#8a99a1")
            return

        if dry_run:
            self._log(f"DRY RUN: would update {len(pending)} assets:")
            for r in pending:
                self._log(f"  ~ {r['name']} ({r['tag']}): {r['fsEnd'] or '(empty)'} -> {r['dellEnd']}")
            self._toast(f"Dry run: {len(pending)} assets would be updated. Untick Dry run to write.", "#e0a000")
            return

        if self.settings.demo_mode:
            self._demo_apply(pending)
        else:
            self._live_apply()

    def _demo_apply(self, pending: list[dict]) -> None:
        self._phase("applying")
        self._progress(0, f"Writing {len(pending)} assets to Freshservice…")
        self._log(f"Applying {len(pending)} updates with 6 field mapping(s)")
        total = len(pending)
        for i, r in enumerate(pending, start=1):
            time.sleep(0.38)
            r["status"] = STATUS_SYNCED
            r["detail"] = "Updated 6 field(s)"
            self.emit({"type": "row_update", "id": r["id"], "status": r["status"], "detail": r["detail"]})
            self._progress(round(i / total * 100), f"Writing to Freshservice… ({i}/{total})")
            self._log(f"  + {r['name']} ({r['tag']}): 6 field(s) updated")
        time.sleep(0.3)
        self._phase("done")
        self._log(f"Sync complete: {total} updated, 0 failed.")
        self._toast(f"Sync complete — {total} assets updated.", "#45d17f")

    def _live_apply(self) -> None:
        s = self.settings
        fs = FreshserviceClient(s.fs_domain, get_secret("fs_api_key"))
        dell = DellAPIClient(
            s.dell_client_id, get_secret("dell_client_secret"),
            auth_url=s.dell_auth_url, warranty_url=s.dell_warranty_url,
        )
        core = CoreSyncEngine(dell, fs)
        mappings = {k: v for k, v in s.field_map.items() if v}
        pending = [r for r in self._live_rows if r.needs_update]

        self._phase("applying")
        self._progress(0, f"Writing {len(pending)} assets to Freshservice…")
        self._log(f"Applying {len(pending)} updates with {len(mappings)} field mapping(s)")

        def apply_progress(c: int, t: int) -> None:
            self._progress(round(c / t * 100) if t else 0, f"Writing to Freshservice… ({c}/{t})")

        ok, fail = core.apply_updates(
            self._live_rows, mappings, log_cb=self._log,
            progress_cb=apply_progress, cancel_event=self._cancel, include_matched=False,
        )
        self.rows = [self._row_to_dict(r) for r in self._live_rows]
        self._publish_rows()
        self._phase("done")
        self._log(f"Sync complete: {ok} updated, {fail} failed.")
        self._toast(f"Sync complete — {ok} assets updated.", "#45d17f" if fail == 0 else "#e0a000")
