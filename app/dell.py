"""Dell TechDirect warranty client.

Ported from the proven standalone tool (Enterprise IT). Authenticates with
Dell TechDirect (OAuth2 client-credentials, HTTP Basic) and queries the v5
asset-entitlements API in batches, refreshing the token on a mid-run 401 and
recording which tags failed so callers can tell "query failed" apart from
"Dell has no data".

UI- and Freshservice-agnostic, so it can be reused from the Admin Suite.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import requests

log = logging.getLogger(__name__)

DELL_AUTH_URL_DEFAULT = "https://apigtwb2c.us.dell.com/auth/oauth/v2/token"
DELL_WARRANTY_URL_DEFAULT = (
    "https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements"
)
DELL_BATCH_SIZE = 50          # kept modest to avoid timeouts on slow links
DELL_REQUEST_DELAY = 0.4      # seconds between batches
DELL_MAX_ATTEMPTS = 3         # attempts per batch before giving up
DELL_RETRY_BACKOFF = (10, 20)  # seconds between batch retries
HTTP_TIMEOUT = 90             # Dell batch queries can be slow

#: Dell service tags are exactly 7 alphanumeric characters.
DELL_TAG_PATTERN = re.compile(r"^[A-Z0-9]{7}$", re.IGNORECASE)


def is_valid_service_tag(tag: str) -> bool:
    return bool(tag and DELL_TAG_PATTERN.match(tag.strip()))


@dataclass
class DellEntitlement:
    """A single warranty entitlement for a service tag."""

    service_tag: str
    start_date: dt.date | None
    end_date: dt.date | None
    service_level_code: str
    service_level_description: str
    entitlement_type: str


@dataclass
class DellAssetWarranty:
    """All warranty data Dell knows about a given service tag."""

    service_tag: str
    ship_date: dt.date | None
    product_lob_description: str
    system_description: str
    country_code: str
    entitlements: list[DellEntitlement] = field(default_factory=list)

    @property
    def latest_end_date(self) -> dt.date | None:
        dates = [e.end_date for e in self.entitlements if e.end_date]
        return max(dates) if dates else None

    @property
    def latest_service_level(self) -> str:
        if not self.entitlements:
            return ""
        latest = max(self.entitlements, key=lambda e: e.end_date or dt.date.min)
        return latest.service_level_description or latest.service_level_code


class DellAPIClient:
    """Authenticates with Dell TechDirect and queries the v5 warranty API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        auth_url: str = DELL_AUTH_URL_DEFAULT,
        warranty_url: str = DELL_WARRANTY_URL_DEFAULT,
    ) -> None:
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.auth_url = (auth_url or "").strip() or DELL_AUTH_URL_DEFAULT
        self.warranty_url = (warranty_url or "").strip() or DELL_WARRANTY_URL_DEFAULT
        self._token: str | None = None
        self._token_expiry = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        self.last_failed_tags: set[str] = set()

    # -- auth ---------------------------------------------------------------
    def _ensure_token(self) -> str:
        if self._token and dt.datetime.now(dt.timezone.utc) < self._token_expiry - dt.timedelta(seconds=60):
            return self._token
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Dell client ID and secret are required.")

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        resp = requests.post(
            self.auth_url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "client_credentials"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Dell auth failed ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("Dell auth response did not include access_token.")
        self._token = token
        self._token_expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=int(body.get("expires_in", 3600))
        )
        return token

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._ensure_token()
            return True, "Dell authentication successful."
        except Exception as exc:
            return False, str(exc)

    # -- warranty -----------------------------------------------------------
    @staticmethod
    def _parse_date(raw: str | None) -> dt.date | None:
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "")).date()
        except ValueError:
            try:
                return dt.datetime.strptime(raw[:10], "%Y-%m-%d").date()
            except ValueError:
                return None

    def get_warranties(
        self,
        service_tags: Iterable[str],
        progress_cb: Callable[[int, int], None] | None = None,
        log_cb: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, DellAssetWarranty]:
        """Query warranty info for an iterable of service tags.

        Returns ``{SERVICE_TAG: DellAssetWarranty}`` (tags uppercased). Tags Dell
        doesn't recognise are simply absent. Tags whose batch query *failed* are
        recorded in :attr:`last_failed_tags`.
        """
        tags = sorted({
            t.strip().upper()
            for t in service_tags
            if t and DELL_TAG_PATTERN.match(t.strip())
        })
        results: dict[str, DellAssetWarranty] = {}
        if not tags:
            return results

        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        total_batches = (len(tags) + DELL_BATCH_SIZE - 1) // DELL_BATCH_SIZE
        self.last_failed_tags = set()

        def fetch_batch(batch: list[str], batch_no: int) -> requests.Response | None:
            nonlocal token
            refreshed = False
            for attempt in range(1, DELL_MAX_ATTEMPTS + 1):
                try:
                    resp = requests.get(
                        self.warranty_url,
                        headers=headers,
                        params={"servicetags": ",".join(batch)},
                        timeout=HTTP_TIMEOUT,
                    )
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if attempt < DELL_MAX_ATTEMPTS:
                        wait = DELL_RETRY_BACKOFF[min(attempt - 1, len(DELL_RETRY_BACKOFF) - 1)]
                        if log_cb:
                            log_cb(f"  ! Timeout/connection error on batch {batch_no} "
                                   f"(attempt {attempt}/{DELL_MAX_ATTEMPTS}) - waiting {wait}s: {exc}")
                        time.sleep(wait)
                        continue
                    if log_cb:
                        log_cb(f"  ! Batch {batch_no} failed after {DELL_MAX_ATTEMPTS} attempts: {exc}")
                    return None
                except requests.RequestException as exc:
                    if log_cb:
                        log_cb(f"  ! Network error on batch {batch_no}: {exc}")
                    return None

                if resp.status_code == 401 and not refreshed:
                    if log_cb:
                        log_cb("  ! 401 from Dell - refreshing token and retrying")
                    self._token = None
                    token = self._ensure_token()
                    headers["Authorization"] = f"Bearer {token}"
                    refreshed = True
                    continue
                return resp
            return None

        for idx in range(0, len(tags), DELL_BATCH_SIZE):
            if cancel_event and cancel_event.is_set():
                if log_cb:
                    log_cb("Dell query cancelled by user.")
                break
            batch = tags[idx : idx + DELL_BATCH_SIZE]
            batch_no = idx // DELL_BATCH_SIZE + 1
            if log_cb:
                log_cb(f"Querying Dell batch {batch_no}/{total_batches} ({len(batch)} tags)")

            batch_ok = False
            try:
                resp = fetch_batch(batch, batch_no)
                if resp is None:
                    continue
                if resp.status_code != 200:
                    if log_cb:
                        log_cb(f"  ! Dell returned {resp.status_code} for batch "
                               f"{batch_no}: {resp.text[:160]}")
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    if log_cb:
                        log_cb(f"  ! Could not parse Dell response on batch {batch_no}")
                    continue

                for asset in payload if isinstance(payload, list) else []:
                    tag = (asset.get("serviceTag") or "").upper()
                    if not tag:
                        continue
                    ents = [
                        DellEntitlement(
                            service_tag=tag,
                            start_date=self._parse_date(ent.get("startDate")),
                            end_date=self._parse_date(ent.get("endDate")),
                            service_level_code=ent.get("serviceLevelCode") or "",
                            service_level_description=ent.get("serviceLevelDescription") or "",
                            entitlement_type=ent.get("entitlementType") or "",
                        )
                        for ent in asset.get("entitlements", []) or []
                    ]
                    results[tag] = DellAssetWarranty(
                        service_tag=tag,
                        ship_date=self._parse_date(asset.get("shipDate")),
                        product_lob_description=asset.get("productLobDescription") or "",
                        system_description=asset.get("systemDescription") or "",
                        country_code=asset.get("countryCode") or "",
                        entitlements=ents,
                    )
                batch_ok = True
            finally:
                if not batch_ok:
                    self.last_failed_tags.update(batch)
                if progress_cb:
                    progress_cb(min(idx + DELL_BATCH_SIZE, len(tags)), len(tags))
                time.sleep(DELL_REQUEST_DELAY)

        return results
