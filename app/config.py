"""Settings storage.

Non-secret settings live in a small JSON file in the user's config directory.
Secrets (the Dell client secret and the Freshservice API key) are kept in the
operating system credential store via ``keyring`` — on Windows that is the
Windows Credential Manager, so nothing sensitive is ever written in plain text.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import keyring
except Exception:  # pragma: no cover - keyring backend missing
    keyring = None

#: Service name used for every entry in the OS credential store.
KEYRING_SERVICE = "DellWarrantySync"

_CONFIG_DIR = Path.home() / ".dell_warranty_sync"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


@dataclass
class AssetType:
    """A Freshservice asset type the tool can sync."""

    label: str
    type_id: int


@dataclass
class Settings:
    """Everything the tool needs to run, minus the two secrets.

    The secrets are fetched on demand from the credential store and are never
    held on this object, so an instance is safe to serialise and log.
    """

    # Dell TechDirect
    dell_client_id: str = "a7f3c9e2-8b14-4d6a-9c02-1e5f8b7a2d90"
    dell_auth_url: str = "https://apigtwb2c.us.dell.com/auth/oauth/v2/token"
    dell_warranty_url: str = (
        "https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements"
    )

    # Freshservice
    fs_domain: str = "yourcompany.freshservice.com"

    # Asset types offered in the control card
    asset_types: list[AssetType] = field(
        default_factory=lambda: [
            AssetType("Laptop", 47),
            AssetType("Desktop", 48),
            AssetType("Workstation", 51),
            AssetType("Server", 53),
        ]
    )

    # Field mapping — which Freshservice field each Dell value is written to.
    # ``{type_id}`` is substituted per asset. A blank value means "skip".
    field_map: dict[str, str] = field(
        default_factory=lambda: {
            "warranty_expiry_date": "warranty_expiry_date_{type_id}",
            "service_level": "support_tier_{type_id}",
            "system_description": "product_model_{type_id}",
            "ship_date": "ship_date_{type_id}",
            "warranty_start_date": "",
            "end_of_life": "end_of_life",
        }
    )

    #: When true the tool never makes a network call; it serves the bundled
    #: sample data so the UI is fully explorable without credentials.
    demo_mode: bool = True

    #: Last sync scope used interactively — reused by the scheduled headless run.
    last_asset_type_id: int = 47
    last_sync_all: bool = False

    #: Scheduled-task descriptor for display (the real task lives in Windows
    #: Task Scheduler). e.g. {"enabled": True, "frequency": "WEEKLY",
    #: "day": "MON", "time": "07:30", "apply": False}.
    schedule: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        d["asset_types"] = [asdict(a) for a in self.asset_types]
        return d

    @classmethod
    def from_json(cls, data: dict) -> "Settings":
        data = dict(data or {})
        if "asset_types" in data and data["asset_types"]:
            data["asset_types"] = [AssetType(**a) for a in data["asset_types"]]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ----------------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------------
def get_secret(name: str) -> str:
    """Read a secret from the OS credential store (empty string if unset)."""
    if keyring is None:
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE, name) or ""
    except Exception as exc:  # pragma: no cover
        log.warning("Could not read secret %r: %s", name, exc)
        return ""


def set_secret(name: str, value: str) -> None:
    """Store (or clear) a secret in the OS credential store."""
    if keyring is None:
        log.warning("No keyring backend available; secret %r not stored.", name)
        return
    try:
        if value:
            keyring.set_password(KEYRING_SERVICE, name, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover
        log.warning("Could not store secret %r: %s", name, exc)


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


# ----------------------------------------------------------------------------
# Non-secret settings
# ----------------------------------------------------------------------------
def load_settings() -> Settings:
    try:
        if _CONFIG_FILE.exists():
            return Settings.from_json(json.loads(_CONFIG_FILE.read_text("utf-8")))
    except Exception as exc:
        log.warning("Could not read settings, using defaults: %s", exc)
    return Settings()


def save_settings(settings: Settings) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(settings.to_json(), indent=2), "utf-8")
