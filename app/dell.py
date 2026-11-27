"""Dell TechDirect warranty client.

Thin wrapper over the Dell TechDirect Warranty API. It is intentionally free of
any UI or Freshservice knowledge so it can be reused from the Admin Suite.

Two responsibilities:
  * OAuth2 client-credentials auth, with the bearer token cached for its
    advertised lifetime.
  * Fetching asset entitlements for a batch of service tags.

Service tags are validated to Dell's 7-character format before they are sent;
anything else is reported back to the caller rather than silently dropped.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

log = logging.getLogger(__name__)

#: Dell service tags are exactly 7 alphanumeric characters.
SERVICE_TAG_LEN = 7

#: Dell limits how many tags may be queried in one request.
MAX_TAGS_PER_BATCH = 100

#: Wait on a 429 when Dell sends no Retry-After header.
DEFAULT_RETRY_WAIT = 30
#: How many times to wait-and-retry a single rate-limited request.
MAX_RETRIES = 6


def is_valid_service_tag(tag: str) -> bool:
    tag = (tag or "").strip()
    return len(tag) == SERVICE_TAG_LEN and tag.isalnum()


@dataclass
class Entitlement:
    """A single asset's warranty entitlement as returned by Dell."""

    service_tag: str
    system_description: str = ""
    ship_date: str = ""            # ISO date (YYYY-MM-DD)
    warranty_end: str = ""         # latest entitlement end date, ISO
    service_level: str = ""        # highest/most specific service level
    raw: dict = field(default_factory=dict, repr=False)


class DellAuthError(RuntimeError):
    pass


class DellClient:
    """Authenticate to Dell TechDirect and pull warranty entitlements."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        auth_url: str,
        warranty_url: str,
        session: requests.Session | None = None,
        timeout: int = 30,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.auth_url = auth_url
        self.warranty_url = warranty_url
        self.timeout = timeout
        self.log = log or (lambda _m: None)
        self._session = session or requests.Session()
        self._token: str | None = None
        self._token_expiry: float = 0.0

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
                    f"Rate limited by Dell (429); waiting {wait}s then retrying "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})…"
                )
                time.sleep(wait)
                continue
            return resp
        return resp

    # -- auth ---------------------------------------------------------------
    def _bearer(self) -> str:
        """Return a valid bearer token, authenticating only when needed."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        resp = self._request(
            "POST",
            self.auth_url,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        if resp.status_code != 200:
            raise DellAuthError(
                f"Dell auth failed (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def test_connection(self) -> None:
        """Raise if credentials are wrong; return quietly if they are good."""
        self._bearer()

    # -- warranty -----------------------------------------------------------
    def get_entitlements(self, service_tags: list[str]) -> dict[str, Entitlement]:
        """Return ``{service_tag: Entitlement}`` for the valid tags given.

        Invalid tags are skipped (the caller already knows which ones failed
        validation). Tags are queried in batches that respect Dell's limit.
        """
        valid = [t for t in service_tags if is_valid_service_tag(t)]
        out: dict[str, Entitlement] = {}
        for start in range(0, len(valid), MAX_TAGS_PER_BATCH):
            batch = valid[start : start + MAX_TAGS_PER_BATCH]
            out.update(self._get_batch(batch))
        return out

    def _get_batch(self, tags: list[str]) -> dict[str, Entitlement]:
        headers = {"Authorization": f"Bearer {self._bearer()}", "Accept": "application/json"}
        resp = self._request(
            "GET",
            self.warranty_url,
            headers=headers,
            params={"servicetags": ",".join(tags)},
        )
        resp.raise_for_status()
        result: dict[str, Entitlement] = {}
        for asset in resp.json() or []:
            ent = self._parse_asset(asset)
            if ent:
                result[ent.service_tag] = ent
        return result

    @staticmethod
    def _parse_asset(asset: dict) -> Entitlement | None:
        tag = (asset.get("serviceTag") or "").strip()
        if not tag:
            return None

        entitlements = asset.get("entitlements") or []
        # The warranty end date is the latest end date across entitlements.
        end_dates = [e.get("endDate", "")[:10] for e in entitlements if e.get("endDate")]
        warranty_end = max(end_dates) if end_dates else ""

        # Prefer the service level of the entitlement that runs longest.
        service_level = ""
        if entitlements:
            longest = max(
                entitlements,
                key=lambda e: e.get("endDate", "") or "",
            )
            service_level = longest.get("serviceLevelDescription", "") or ""

        return Entitlement(
            service_tag=tag,
            system_description=asset.get("productLineDescription", "")
            or asset.get("systemDescription", ""),
            ship_date=(asset.get("shipDate") or "")[:10],
            warranty_end=warranty_end,
            service_level=service_level,
            raw=asset,
        )
