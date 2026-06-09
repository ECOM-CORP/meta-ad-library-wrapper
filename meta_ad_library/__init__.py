"""Unofficial Python wrapper for the Meta Ad Library internal GraphQL endpoint."""

from .client import AdLibraryClient
from .exceptions import (
    AdLibraryError,
    BootstrapError,
    SessionExpiredError,
    StaleDocIdError,
)
from .models import Ad, AdPage, AdReach, ScanPage, ScanResult, SessionData
from .session import bootstrap_session

__all__ = [
    "Ad",
    "AdPage",
    "AdReach",
    "ScanPage",
    "ScanResult",
    "SessionData",
    "AdLibraryClient",
    "bootstrap_session",
    "AdLibraryError",
    "BootstrapError",
    "StaleDocIdError",
    "SessionExpiredError",
]
