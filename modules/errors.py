"""Errors the CLI explains in plain language."""

from __future__ import annotations


class ProspectingError(Exception):
    """Expected failure. The message is safe to show as-is."""


class ConfigError(ProspectingError):
    """A configuration file is missing or invalid."""


class CRMError(ProspectingError):
    """Google Sheets could not be read or updated safely."""


class ApifyError(ProspectingError):
    """The Apify API or an actor run failed."""


class EnrichmentError(ProspectingError):
    """A developer site could not be read."""


class LicenseError(ProspectingError):
    """A license key was rejected, copied to another machine, or out of quota."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
