"""Machine-bound license file. The key stays in ~/.app-scout and out of logs.

Copying credentials.json to another machine fails the fingerprint check
before any licensed request is sent. The Apify token is not stored here.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import uuid
from pathlib import Path

from modules.errors import LicenseError

CREDENTIAL_FIELDS = ("license_key", "instance_id", "machine_fingerprint")


def machine_fingerprint() -> str:
    """SHA-256 of platform.node() + uuid.getnode(). Stable for this machine."""
    material = f"{platform.node()}{uuid.getnode()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def scout_home() -> Path:
    override = os.environ.get("APP_SCOUT_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".app-scout"


def credentials_path() -> Path:
    return scout_home() / "credentials.json"


def save_credentials(payload: dict) -> Path:
    """Write only the three license fields, mode 0600."""
    stored = {field: str(payload.get(field) or "").strip() for field in CREDENTIAL_FIELDS}
    missing = [field for field in CREDENTIAL_FIELDS if not stored[field]]
    if missing:
        raise LicenseError("Activation did not return a license key and instance id.")
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".json.tmp")
    # os.open with 0o600 beats a umask race that would create the file world-readable.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(stored, handle, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return path


def load_credentials() -> dict | None:
    path = credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseError(f"Could not read {path.name}.") from exc
    if not isinstance(data, dict):
        raise LicenseError(f"{path.name} is not a credentials file.")
    return {field: str(data.get(field) or "") for field in CREDENTIAL_FIELDS}


def assert_same_machine(credentials: dict | None) -> None:
    """Reject a credentials file that was copied onto this machine."""
    if not credentials:
        raise LicenseError("Activate a license key on this machine.")
    stored = str(credentials.get("machine_fingerprint") or "")
    if stored != machine_fingerprint():
        raise LicenseError(
            "This license file does not match this machine. "
            "Copying credentials to another server is blocked."
        )


def has_matching_license() -> bool:
    try:
        data = load_credentials()
    except LicenseError:
        return False
    if not data or not data.get("license_key") or not data.get("instance_id"):
        return False
    return str(data.get("machine_fingerprint") or "") == machine_fingerprint()
