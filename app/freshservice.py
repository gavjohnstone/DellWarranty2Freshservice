"""Freshservice asset client.

Ported from the proven standalone tool (Enterprise IT). Handles:
  * listing assets, splitting into creation-date windows (year -> quarter ->
    month -> week) to work around Freshservice's 40-page (~1,200 asset) cap,
  * probing which server-side filter form a tenant actually honours,
  * auto-detecting the warranty field, and
  * writing fields back, trying candidate (name, location) forms and caching
    the resolution for the rest of the run.

UI- and Dell-agnostic, so it can be reused from the Admin Suite.
"""

from __future__ import annotations

import base64
import calendar
import datetime as dt
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 90
FS_PAGE_DELAY = 0.25          # between list pages
FS_WINDOW_DELAY = 1.0        # between date-window sub-queries
FS_WRITE_DELAY = 0.4         # between asset writes
FS_LIST_429_BACKOFF = (30, 60, 120)   # waits for 429 during list pagination
FS_WRITE_429_BACKOFF = (15, 30, 60)   # fallback waits for 429 during writes


@dataclass
class FSAsset:
    display_id: int
    name: str
    asset_type_id: int
    asset_type_name: str
    serial_number: str
    current_warranty_end: dt.date | None
    raw: dict[str, Any] = field(default_factory=dict)


def _first_match(d: dict[str, Any], suffixes: tuple[str, ...]) -> Any:
    """First value in ``d`` whose key equals or is prefixed by a suffix."""
    for key, value in d.items():
        for suffix in suffixes:
            if key == suffix or key.startswith(suffix + "_"):
                if value:
                    return value
    return None


def _parse_iso_date(raw: Any) -> dt.date | None:
    if not raw:
        return None
    if isinstance(raw, dt.date):
        return raw
    s = str(raw)
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "")).date()
    except ValueError:
        try:
            return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


class FreshserviceClient:
    """Minimal Freshservice client for the bits the sync needs."""

    def __init__(self, domain: str, api_key: str) -> None:
        self.domain = (domain or "").strip().rstrip("/")
        if self.domain and not self.domain.startswith("http"):
            self.domain = f"https://{self.domain}"
        self.api_key = (api_key or "").strip()
        token = base64.b64encode(f"{self.api_key}:X".encode("utf-8")).decode("ascii")
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # -- helpers ------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        return requests.get(f"{self.domain}{path}", headers=self.headers, params=params, timeout=HTTP_TIMEOUT)

    def _put(self, path: str, body: dict[str, Any]) -> requests.Response:
        return requests.put(f"{self.domain}{path}", headers=self.headers, data=json.dumps(body), timeout=HTTP_TIMEOUT)

    def test_connection(self) -> tuple[bool, str]:
        if not self.domain or not self.api_key:
            return False, "Domain and API key are required."
        try:
            resp = self._get("/api/v2/asset_types", params={"per_page": 1})
            if resp.status_code == 200:
                return True, "Freshservice authentication successful."
            return False, f"Freshservice returned HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:
            return False, f"Network error: {exc}"

    # -- asset types --------------------------------------------------------
    def list_asset_types(self) -> list[dict[str, Any]]:
        types: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._get("/api/v2/asset_types", params={"page": page})
            resp.raise_for_status()
            chunk = resp.json().get("asset_types", [])
            if not chunk:
                break
            types.extend(chunk)
            page += 1
        types.sort(key=lambda t: t.get("name", ""))
        return types

    # -- assets -------------------------------------------------------------
    def list_assets(
        self,
        asset_type_id: int | None = None,
        asset_type_name: str | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[FSAsset]:
        """Fetch assets, optionally filtered by type, date-windowing past the cap."""
        assets, hit_cap = self._list_assets_window(
            asset_type_id=asset_type_id, asset_type_name=asset_type_name,
            created_after=None, created_before=None, log_cb=log_cb, cancel_event=cancel_event,
        )
        if not hit_cap:
            return assets

        if log_cb:
            log_cb("Splitting query into date windows to work around "
                   "Freshservice's 40-page limit...")

        today = dt.date.today()
        all_assets: list[FSAsset] = []
        seen_ids: set[int] = set()

        def absorb(chunk: list[FSAsset]) -> None:
            for a in chunk:
                if a.display_id not in seen_ids:
                    seen_ids.add(a.display_id)
                    all_assets.append(a)

        # Level 1: yearly
        capped_years: list[int] = []
        for y in range(2010, today.year + 2):
            if cancel_event and cancel_event.is_set():
                break
            time.sleep(FS_WINDOW_DELAY)
            chunk, capped = self._list_assets_window(
                asset_type_id=asset_type_id, asset_type_name=asset_type_name,
                created_after=f"{y}-01-01T00:00:00Z", created_before=f"{y + 1}-01-01T00:00:00Z",
                log_cb=(lambda msg, yr=y: log_cb(f"[{yr}] {msg}")) if log_cb else None,
                cancel_event=cancel_event,
            )
            before = len(all_assets)
            absorb(chunk)
            if len(all_assets) > before and log_cb:
                log_cb(f"  Year {y}: {len(all_assets) - before} asset(s) (running total: {len(all_assets)})")
            if capped:
                capped_years.append(y)
                if log_cb:
                    log_cb(f"  Year {y} hit the cap - splitting into quarters...")

        # Level 2: quarterly
        quarters = [(1, 4), (4, 7), (7, 10), (10, 13)]
        capped_quarters: list[tuple[int, int]] = []
        for y in capped_years:
            if cancel_event and cancel_event.is_set():
                break
            for q_idx, (start_m, end_m) in enumerate(quarters, start=1):
                end_y = y + 1 if end_m > 12 else y
                actual_end_m = end_m if end_m <= 12 else 1
                lbl = f"{y}-Q{q_idx}"
                time.sleep(FS_WINDOW_DELAY)
                chunk, capped = self._list_assets_window(
                    asset_type_id=asset_type_id, asset_type_name=asset_type_name,
                    created_after=f"{y}-{start_m:02d}-01T00:00:00Z",
                    created_before=f"{end_y}-{actual_end_m:02d}-01T00:00:00Z",
                    log_cb=(lambda msg, l=lbl: log_cb(f"  [{l}] {msg}")) if log_cb else None,
                    cancel_event=cancel_event,
                )
                before = len(all_assets)
                absorb(chunk)
                if len(all_assets) > before and log_cb:
                    log_cb(f"  [{lbl}]: {len(all_assets) - before} new asset(s)")
                if capped:
                    capped_quarters.append((y, start_m))
                    if log_cb:
                        log_cb(f"  [{lbl}] hit the cap - splitting into months...")

        # Level 3: monthly
        capped_months: list[tuple[int, int]] = []
        for y, start_m in capped_quarters:
            if cancel_event and cancel_event.is_set():
                break
            q_idx = next(i for i, (s, _) in enumerate(quarters) if s == start_m)
            for m in range(quarters[q_idx][0], quarters[q_idx][1]):
                end_m = m + 1
                end_y = y + 1 if end_m > 12 else y
                actual_end_m = end_m if end_m <= 12 else 1
                lbl = f"{y}-{m:02d}"
                time.sleep(FS_WINDOW_DELAY)
                chunk, capped = self._list_assets_window(
                    asset_type_id=asset_type_id, asset_type_name=asset_type_name,
                    created_after=f"{y}-{m:02d}-01T00:00:00Z",
                    created_before=f"{end_y}-{actual_end_m:02d}-01T00:00:00Z",
                    log_cb=(lambda msg, l=lbl: log_cb(f"  [{l}] {msg}")) if log_cb else None,
                    cancel_event=cancel_event,
                )
                before = len(all_assets)
                absorb(chunk)
                if len(all_assets) > before and log_cb:
                    log_cb(f"  [{lbl}]: {len(all_assets) - before} new asset(s)")
                if capped:
                    capped_months.append((y, m))
                    if log_cb:
                        log_cb(f"  [{lbl}] hit the cap - splitting into weeks...")

        # Level 4: weekly
        for y, m in capped_months:
            if cancel_event and cancel_event.is_set():
                break
            _, days_in_month = calendar.monthrange(y, m)
            week_starts = [1, 8, 15, 22]
            week_ends = [8, 15, 22, days_in_month + 1]
            for ws, we in zip(week_starts, week_ends):
                end_day = min(we, days_in_month + 1)
                end_m_val = m + 1 if end_day > days_in_month else m
                end_y_val = y + 1 if end_m_val > 12 else y
                end_m_actual = end_m_val if end_m_val <= 12 else 1
                end_d = 1 if end_day > days_in_month else end_day
                lbl = f"{y}-{m:02d}-W{week_starts.index(ws) + 1}"
                time.sleep(FS_WINDOW_DELAY)
                chunk, capped = self._list_assets_window(
                    asset_type_id=asset_type_id, asset_type_name=asset_type_name,
                    created_after=f"{y}-{m:02d}-{ws:02d}T00:00:00Z",
                    created_before=f"{end_y_val}-{end_m_actual:02d}-{end_d:02d}T00:00:00Z",
                    log_cb=(lambda msg, l=lbl: log_cb(f"  [{l}] {msg}")) if log_cb else None,
                    cancel_event=cancel_event,
                )
                before = len(all_assets)
                absorb(chunk)
                if len(all_assets) > before and log_cb:
                    log_cb(f"  [{lbl}]: {len(all_assets) - before} new asset(s)")
                if capped and log_cb:
                    log_cb(f"  [{lbl}] WARNING: >1,200 assets in a single week. Some assets "
                           f"may be missing - likely a bulk import. Consider splitting the "
                           f"asset type or using the FS UI export.")

        if log_cb:
            log_cb(f"Date-windowed fetch complete: {len(all_assets)} total assets.")
        return all_assets

    def _list_assets_window(
        self,
        asset_type_id: int | None = None,
        asset_type_name: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[FSAsset], bool]:
        """Inner paginator. Returns (assets, hit_cap)."""
        assets: list[FSAsset] = []
        chosen_path = "/api/v2/assets"
        chosen_params: dict[str, Any] = {"include": "type_fields"}
        page1_chunk: list[dict[str, Any]] | None = None

        def _with_dates(base_filter: str) -> str:
            parts = [base_filter] if base_filter else []
            if created_after:
                parts.append(f"created_at:>'{created_after[:10]}'")
            if created_before:
                parts.append(f"created_at:<'{created_before[:10]}'")
            return " AND ".join(parts)

        def _chunk_within_window(chunk: list[dict[str, Any]]) -> bool:
            lo = (created_after or "")[:10]
            hi = (created_before or "")[:10]
            for item in chunk:
                c = (item.get("created_at") or "")[:10]
                if not c:
                    continue
                if lo and c < lo:
                    return False
                if hi and c >= hi:
                    return False
            return True

        dates_requested = bool(created_after or created_before)

        if asset_type_id is not None:
            base_cond = f"asset_type_id:{asset_type_id}"
            filter_with_dates = _with_dates(base_cond)
            strategies: list[tuple[str, dict[str, Any], str]] = [
                ("/api/v2/assets",
                 {"include": "type_fields", "filter": f'"{filter_with_dates}"'},
                 "filter=asset_type_id (id)"),
            ]
            if asset_type_name:
                escaped = asset_type_name.replace("'", r"\'")
                strategies.append((
                    "/api/v2/assets",
                    {"include": "type_fields", "filter": f'"{_with_dates(f"asset_type_name:{chr(39)}{escaped}{chr(39)}")}"'},
                    "filter=asset_type_name",
                ))
            strategies.extend([
                ("/api/v2/assets",
                 {"include": "type_fields", "query": f'"{filter_with_dates}"'},
                 "query=asset_type_id (id)"),
                ("/api/v2/assets/filter",
                 {"include": "type_fields", "query": f'"{filter_with_dates}"'},
                 "/filter endpoint"),
            ])

            picked = False
            for path, params, label in strategies:
                if cancel_event and cancel_event.is_set():
                    if log_cb:
                        log_cb("Cancelled while probing filter strategies.")
                    return [], False
                if log_cb and not created_after:
                    log_cb(f"Probing filter strategy: {label}")
                resp = self._get(path, params={**params, "page": 1})
                if resp.status_code != 200:
                    if log_cb and not created_after:
                        log_cb(f"  - {label} returned HTTP {resp.status_code}")
                    continue
                chunk = resp.json().get("assets", [])
                if not chunk:
                    if log_cb and not created_after:
                        log_cb(f"  - {label} returned 0 assets (accepted)")
                    chosen_path, chosen_params, page1_chunk, picked = path, params, chunk, True
                    break
                non_match = [a.get("asset_type_id") for a in chunk if a.get("asset_type_id") != asset_type_id]
                if not non_match:
                    if log_cb and not created_after:
                        log_cb(f"  + {label} confirmed: page 1 has {len(chunk)} assets, "
                               f"all matching asset_type_id={asset_type_id}")
                    chosen_path, chosen_params, page1_chunk, picked = path, params, chunk, True
                    break
                if log_cb and not created_after:
                    log_cb(f"  - {label} ignored by server "
                           f"({len(non_match)}/{len(chunk)} non-matching on page 1)")

            if not picked:
                if dates_requested:
                    raise RuntimeError(
                        "Freshservice did not accept a created_at date filter, so the fetch "
                        "cannot be windowed past the 40-page cap. Aborting rather than "
                        "returning a silently incomplete asset list.")
                if log_cb:
                    log_cb("No server-side filter accepted - falling back to full asset "
                           "pagination. This may take a while on large estates.")
                chosen_path, chosen_params = "/api/v2/assets", {"include": "type_fields"}

        elif dates_requested:
            date_cond = _with_dates("")
            strategies = [
                ("/api/v2/assets", {"include": "type_fields", "filter": f'"{date_cond}"'}, "filter=created_at range"),
                ("/api/v2/assets", {"include": "type_fields", "query": f'"{date_cond}"'}, "query=created_at range"),
                ("/api/v2/assets/filter", {"include": "type_fields", "query": f'"{date_cond}"'}, "/filter endpoint (dates)"),
            ]
            picked = False
            for path, params, label in strategies:
                if cancel_event and cancel_event.is_set():
                    if log_cb:
                        log_cb("Cancelled while probing date-filter strategies.")
                    return [], False
                resp = self._get(path, params={**params, "page": 1})
                if resp.status_code != 200:
                    continue
                chunk = resp.json().get("assets", [])
                if not chunk or _chunk_within_window(chunk):
                    chosen_path, chosen_params, page1_chunk, picked = path, params, chunk, True
                    break
            if not picked:
                raise RuntimeError(
                    "Freshservice did not accept a created_at date filter for the all-types "
                    "fetch, so it cannot be windowed past the 40-page cap. Aborting rather "
                    "than returning a silently incomplete asset list.")

        hit_cap = False
        page = 1
        while True:
            if cancel_event and cancel_event.is_set():
                if log_cb:
                    log_cb("Freshservice fetch cancelled by user.")
                break

            if page == 1 and page1_chunk is not None:
                chunk = page1_chunk
            else:
                resp = self._get(chosen_path, params={**chosen_params, "page": page})
                if resp.status_code == 429:
                    for wait in FS_LIST_429_BACKOFF:
                        if log_cb:
                            log_cb(f"Rate limited (429) - waiting {wait}s before retry...")
                        time.sleep(wait)
                        resp = self._get(chosen_path, params={**chosen_params, "page": page})
                        if resp.status_code != 429:
                            break
                    if resp.status_code == 429:
                        raise RuntimeError("Freshservice rate limit persists after retries. "
                                           "Wait a few minutes and try again.")
                if resp.status_code == 400:
                    err = resp.text or ""
                    if "page" in err and ("40" in err or "less than or equal" in err):
                        hit_cap = True
                        if log_cb:
                            log_cb(f"40-page limit hit at page {page} ({len(assets)} assets in this window).")
                        break
                    raise RuntimeError(f"Freshservice list_assets failed (400): {err[:300]}")
                if resp.status_code != 200:
                    raise RuntimeError(f"Freshservice list_assets failed ({resp.status_code}): {resp.text[:300]}")
                chunk = resp.json().get("assets", [])

            if not chunk:
                break

            kept = 0
            for raw in chunk:
                if asset_type_id and raw.get("asset_type_id") != asset_type_id:
                    continue
                tf = raw.get("type_fields") or {}
                serial = raw.get("serial_number") or _first_match(tf, ("serial_number", "service_tag")) or ""
                wend = raw.get("warranty_expiry_date") or _first_match(tf, ("warranty_expiry_date",))
                assets.append(FSAsset(
                    display_id=raw.get("display_id"),
                    name=raw.get("name") or "",
                    asset_type_id=raw.get("asset_type_id") or 0,
                    asset_type_name="",
                    serial_number=str(serial).strip(),
                    current_warranty_end=_parse_iso_date(wend),
                    raw=raw,
                ))
                kept += 1

            if log_cb:
                log_cb(f"Page {page}: {len(chunk)} returned, {kept} kept")
            page += 1
            time.sleep(FS_PAGE_DELAY)

        return assets, hit_cap

    # -- field discovery ----------------------------------------------------
    def get_asset_type_info(self, asset_type_id: int) -> dict[str, Any]:
        resp = self._get(f"/api/v2/asset_types/{asset_type_id}")
        if resp.status_code == 200:
            return resp.json().get("asset_type") or {}
        return {}

    def get_asset_type_fields(self, asset_type_id: int) -> list[dict[str, Any]]:
        resp = self._get(f"/api/v2/asset_types/{asset_type_id}/fields")
        if resp.status_code != 200:
            return []
        raw = (resp.json() or {}).get("asset_type_fields") or []
        flat: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            sub = item.get("fields")
            if isinstance(sub, list):
                flat.extend(f for f in sub if isinstance(f, dict))
            else:
                flat.append(item)
        return flat

    def discover_warranty_field(
        self,
        asset_type_id: int,
        sample_assets: list["FSAsset"] | None = None,
        log_cb: Callable[[str], None] | None = None,
    ) -> str | None:
        """Find the real column name for the warranty expiry date field."""
        if sample_assets:
            seen_keys: set[str] = set()
            for a in sample_assets:
                seen_keys.update(((a.raw or {}).get("type_fields") or {}).keys())
            for key in seen_keys:
                if key == "warranty_expiry_date" or key.startswith("warranty_expiry_date_"):
                    if log_cb:
                        log_cb(f"Detected warranty field from asset type_fields: '{key}'")
                    return key
            if log_cb and seen_keys:
                log_cb(f"  No warranty key in asset type_fields. Keys seen ({len(seen_keys)}): "
                       + ", ".join(sorted(seen_keys)[:40]))

        visited: list[int] = []
        current = asset_type_id
        while current and current not in visited and len(visited) < 10:
            visited.append(current)
            info = self.get_asset_type_info(current)
            type_name = info.get("name") or str(current)
            fields = self.get_asset_type_fields(current)
            if log_cb:
                log_cb(f"  Checking field definitions on '{type_name}' (id={current}): {len(fields)} fields")
            for f in fields:
                fname = (f.get("name") or f.get("column_name") or f.get("field_header") or "").strip()
                label = (f.get("label") or f.get("field_header_label") or f.get("display_label") or "").strip().lower()
                if fname == "warranty_expiry_date" or fname.startswith("warranty_expiry_date_"):
                    if log_cb:
                        log_cb(f"  Detected warranty field from type definition: '{fname}'")
                    return fname
                if label in ("warranty expiry date", "warranty expiry", "warranty expiry date:") and fname:
                    if log_cb:
                        log_cb(f"  Detected warranty field by label: '{fname}'")
                    return fname
            current = info.get("parent_asset_type_id") or 0

        if log_cb:
            log_cb("  Could not auto-detect warranty field. Enter the correct name manually "
                   "in the field mapping.")
        return None

    # -- updates ------------------------------------------------------------
    def update_warranty(
        self,
        display_id: int,
        field_updates: dict[str, Any],
        known_bad: set[str] | None = None,
        resolved_forms: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[bool, str, set[str], dict[str, tuple[str, str]]]:
        """Push field updates to one asset, trying candidate (name, location) forms.

        Returns (ok, msg, newly_bad_base_names, newly_resolved_forms).
        """
        newly_bad: set[str] = set()
        newly_resolved: dict[str, tuple[str, str]] = {}
        resolved_forms = dict(resolved_forms or {})

        def base_name(key: str) -> str:
            return re.sub(r"_\d+$", "", key)

        work = {k: v for k, v in field_updates.items() if base_name(k) not in (known_bad or set())}
        if not work:
            return True, "no valid fields to update (all mapped fields invalid)", newly_bad, newly_resolved

        def candidate_forms(key: str) -> list[tuple[str, str]]:
            bare = base_name(key)
            raw = [(key, "type_fields"), (key, "root"), (bare, "type_fields"), (bare, "root")]
            seen: set[tuple[str, str]] = set()
            out: list[tuple[str, str]] = []
            for form in raw:
                if form not in seen:
                    seen.add(form)
                    out.append(form)
            return out

        def put_with_429_retry(body: dict[str, Any]) -> requests.Response:
            resp = self._put(f"/api/v2/assets/{display_id}", body)
            if resp.status_code != 429:
                return resp
            for fallback in FS_WRITE_429_BACKOFF:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = int(retry_after) if retry_after else fallback
                except (ValueError, TypeError):
                    wait = fallback
                time.sleep(min(wait, 120))
                resp = self._put(f"/api/v2/assets/{display_id}", body)
                if resp.status_code != 429:
                    return resp
            return resp

        def send(forms: dict[str, tuple[str, str]], values: dict[str, Any]) -> requests.Response:
            root: dict[str, Any] = {}
            typed: dict[str, Any] = {}
            for base, (name, loc) in forms.items():
                if base not in values:
                    continue
                (root if loc == "root" else typed)[name] = values[base]
            body: dict[str, Any] = dict(root)
            if typed:
                body["type_fields"] = typed
            return put_with_429_retry(body)

        def invalid_names(resp: requests.Response) -> set[str]:
            try:
                return {e["field"] for e in resp.json().get("errors", []) if e.get("code") == "invalid_field"}
            except Exception:
                return set()

        values: dict[str, Any] = {}
        origin_key: dict[str, str] = {}
        for k, v in work.items():
            b = base_name(k)
            values[b] = v
            origin_key[b] = k

        forms: dict[str, tuple[str, str]] = {}
        cursor: dict[str, int] = {}
        for b in values:
            if b in resolved_forms:
                forms[b] = resolved_forms[b]
                cursor[b] = -1
            else:
                forms[b] = candidate_forms(origin_key[b])[0]
                cursor[b] = 0

        for _ in range(4 * len(values) + 2):
            resp = send(forms, values)
            if resp.status_code in (200, 201):
                for b, form in forms.items():
                    newly_resolved[b] = form
                skipped = sorted((known_bad or set()) | newly_bad)
                note = f"; skipped invalid: {skipped}" if skipped else ""
                return True, f"{len(values)} field(s) updated{note}", newly_bad, newly_resolved

            if resp.status_code == 400:
                bad_names = invalid_names(resp)
                advanced_any = False
                exhausted: set[str] = set()
                for b in list(values):
                    if forms[b][0] in bad_names:
                        cands = candidate_forms(origin_key[b])
                        nxt = 0 if cursor[b] == -1 else cursor[b] + 1
                        if nxt < len(cands):
                            forms[b] = cands[nxt]
                            cursor[b] = nxt
                            advanced_any = True
                        else:
                            exhausted.add(b)
                if exhausted:
                    for b in exhausted:
                        newly_bad.add(b)
                        values.pop(b, None)
                        forms.pop(b, None)
                        cursor.pop(b, None)
                    if not values:
                        return False, f"All fields rejected as invalid: {sorted(newly_bad)}", newly_bad, newly_resolved
                    continue
                if advanced_any:
                    continue
                return False, f"HTTP 400: {resp.text[:200]}", newly_bad, newly_resolved

            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", newly_bad, newly_resolved

        return False, "Update failed after exhausting placement candidates", newly_bad, newly_resolved
