"""Unofficial Python wrapper for the Meta Ad Library internal GraphQL endpoint."""

# Bumped on every meaningful push so a running MCP can report exactly which code it's on
# (the `server_version` tool / session_status). Keep in sync with pyproject `version`.
__version__ = "0.7.1"

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
