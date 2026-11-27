"""Freshservice asset client.

Handles the two things the sync needs from Freshservice:
  * listing assets of a given type, following the API's pagination, and
  * writing a handful of type-specific fields back onto an asset.

Freshservice enforces a per-minute API rate limit, so every request goes
through :meth:`FreshserviceClient._request`, which honours the ``Retry-After``
header on a 429 and retries the *same* request rather than aborting the run.

Like :class:`~app.dell.DellClient`, it knows nothing about Dell or the UI.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

log = logging.getLogger(__name__)

#: Freshservice returns at most 100 records per page.
PAGE_SIZE = 100

#: How long to wait on a 429 when the server sends no Retry-After header.
DEFAULT_RETRY_WAIT = 30
#: How many times to wait-and-retry a single rate-limited request.
MAX_RETRIES = 8


@dataclass
class Asset:
    """A Freshservice asset, reduced to what the sync cares about."""

    display_id: int
    name: str
    asset_type_id: int
    service_tag: str = ""          # serial number on the asset
    warranty_expiry: str = ""      # current value of the mapped field, ISO
    type_fields: dict = field(default_factory=dict, repr=False)
    raw: dict = field(default_factory=dict, repr=False)


class FreshserviceClient:
    """Read assets from, and write fields back to, Freshservice."""

    def __init__(
        self,
        domain: str,
        api_key: str,
        *,
        session: requests.Session | None = None,
        timeout: int = 30,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base = f"https://{domain.rstrip('/')}/api/v2"
        self.api_key = api_key
        self.timeout = timeout
        self.log = log or (lambda _m: None)
        self._session = session or requests.Session()
        # Freshservice uses HTTP basic auth: the API key as the username and
        # an arbitrary password.
        self._session.auth = (api_key, "X")

    # -- request plumbing ---------------------------------------------------
    @staticmethod
    def _retry_after(resp: requests.Response) -> int:
        value = resp.headers.get("Retry-After")
        if value:
            try:
                return max(1, int(float(value)))
            except ValueError:
                pass
        return DEFAULT_RETRY_WAIT

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Perform a request, waiting out 429 rate limits and retrying it."""
        for attempt in range(MAX_RETRIES + 1):
            resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                wait = self._retry_after(resp)
                self.log(
                    f"Rate limited by Freshservice (429); waiting {wait}s then retrying "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})…"
                )
                time.sleep(wait)
                continue
            return resp
        return resp  # exhausted retries; caller inspects status

    def test_connection(self) -> None:
        resp = self._request("GET", f"{self.base}/agents", params={"per_page": 1})
        resp.raise_for_status()

    # -- assets -------------------------------------------------------------
    def list_assets(
        self,
        asset_type_id: int | None,
        *,
        warranty_field: str = "",
        progress=None,
    ) -> list[Asset]:
        """Return every asset of ``asset_type_id`` (or all, when ``None``).

        ``progress`` is an optional ``callable(page, returned, kept)`` hook so
        callers can surface pagination progress.
        """
        assets: list[Asset] = []
        page = 1
        params: dict = {"per_page": PAGE_SIZE, "include": "type_fields"}
        if asset_type_id is not None:
            params["filter"] = f'"asset_type_id:{asset_type_id}"'

        while True:
            params["page"] = page
            resp = self._request("GET", f"{self.base}/assets", params=params)
            resp.raise_for_status()
            batch = resp.json().get("assets", [])
            kept = 0
            for raw in batch:
                asset = self._parse_asset(raw, warranty_field)
                if asset_type_id is None or asset.asset_type_id == asset_type_id:
                    assets.append(asset)
                    kept += 1
            if progress:
                progress(page, len(batch), kept)
            if len(batch) < PAGE_SIZE:
                break
            page += 1
        return assets

    @staticmethod
    def _parse_asset(raw: dict, warranty_field: str) -> Asset:
        type_fields = raw.get("type_fields", {}) or {}
        service_tag = (raw.get("serial_number") or "").strip()
        warranty = ""
        if warranty_field and warranty_field in type_fields:
            warranty = (type_fields.get(warranty_field) or "")[:10]
        return Asset(
            display_id=raw.get("display_id") or raw.get("id"),
            name=raw.get("name", ""),
            asset_type_id=raw.get("asset_type_id", 0),
            service_tag=service_tag,
            warranty_expiry=warranty,
            type_fields=type_fields,
            raw=raw,
        )

    # -- write-back ---------------------------------------------------------
    def update_asset_fields(self, display_id: int, type_fields: dict) -> None:
        """Write ``type_fields`` onto one asset. Raises on API error."""
        resp = self._request(
            "PUT",
            f"{self.base}/assets/{display_id}",
            json={"asset": {"type_fields": type_fields}},
        )
        if resp.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {resp.status_code}: {resp.text[:300]}", response=resp
            )
