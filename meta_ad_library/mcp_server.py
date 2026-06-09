"""MCP server (Streamable HTTP) exposing the Ad Library wrapper as tools.

Wraps the AdLibraryClient in-process (same session_cache.json as the FastAPI layer).
Blocking client calls run in a worker thread so the server stays responsive. Tools
return plain dicts; on a dead/missing session they return {"error": ...} so the model
can tell the user to re-bootstrap rather than crashing.

Run:  python -m meta_ad_library.mcp_server   (or:  python run_mcp.py)
Endpoint: http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

from .client import AdLibraryClient
from .exceptions import (
    AdLibraryError,
    BootstrapError,
    SessionExpiredError,
    StaleDocIdError,
)
from .models import SessionData
from .session import bootstrap_session

# Config via env (so the same code runs locally and on a VPS):
#   MCP_HOST           bind address (default 127.0.0.1; use 0.0.0.0 in a container)
#   MCP_PORT           port (default 8765)
#   MCP_TOKEN          secret that becomes the URL path — the endpoint is /<MCP_TOKEN>
#                      (e.g. https://mcp.example.com/<token>); any other path 404s. This
#                      is the capability-URL auth: only callers who know the full URL get
#                      in. Empty = no token (endpoint at /mcp) — local dev only.
#   MCP_SESSION_CACHE  path to session_cache.json (default ./session_cache.json)
#   MCP_PROFILE_DIR    Playwright profile dir for bootstrap (default ./.pw-profile)
CACHE_PATH = Path(os.environ.get("MCP_SESSION_CACHE", "session_cache.json"))
PROFILE_DIR = os.environ.get("MCP_PROFILE_DIR", ".pw-profile")
_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("MCP_PORT", "8765"))
_TOKEN = os.environ.get("MCP_TOKEN", "").strip()
_PATH = f"/{_TOKEN}" if _TOKEN else "/mcp"

_ACTIVE = {"all": "all", "true": "active", "false": "inactive"}

# Sent to the client on connect (MCP `instructions`) so the model knows the session
# lifecycle without being told each time.
_INSTRUCTIONS = (
    "These tools query the Meta (Facebook) Ad Library through a harvested browser "
    "session that expires over time. WHEN YOU START using this server in a conversation "
    "— and whenever a tool returns an \"error\" mentioning an invalid or missing session "
    "— call `session_status` first. If it reports valid=false (or no session), call "
    "`bootstrap` to re-harvest the session, then retry the original call. "
    "`search_*` and `scan_*` return ONE page at a time: to get more results call the same "
    "tool again with the returned `next_cursor` (and, for `scan_*`, the returned `streak`) "
    "until `done` is true or there is no `next_cursor`."
)

mcp = FastMCP(
    "meta-ad-library", host=_HOST, port=_PORT, streamable_http_path=_PATH,
    instructions=_INSTRUCTIONS,
)

_client: AdLibraryClient | None = None


def _get_client() -> AdLibraryClient | None:
    global _client
    if _client is None and CACHE_PATH.exists():
        _client = AdLibraryClient(SessionData.load(CACHE_PATH))
    return _client


def _active(value: str) -> str:
    return _ACTIVE.get((value or "all").strip().lower(), "all")


# The lean field set returned per ad by default. The full normalized `Ad` has many
# more fields (dates, platforms, caption, link_description, cta_text, reach breakdown,
# ...); pass verbose=true to get them all. `raw` is ALWAYS stripped for now — it holds
# the original Meta node (incl. image refs) and will get dedicated handling later.
DEFAULT_AD_FIELDS = (
    "id",
    "page_id",
    "page_name",
    "creative_bodies",
    "snapshot_url",
    "link_url",
    "title",
    "eu_total_reach",
)


def _shape(result: dict, verbose: bool = False) -> dict:
    """Shape an MCP response in place: always strip `raw`; project ads to
    DEFAULT_AD_FIELDS unless verbose.

    Handles every tool shape: the single AdReach-shaped dict (get_ad_reach) — only its
    top-level `raw` is dropped — and search/scan pages whose ads each carry `raw` plus a
    nested `reach.raw`. Error dicts (no `ads`, no `raw`) pass through untouched."""
    if not isinstance(result, dict):
        return result
    result.pop("raw", None)  # get_ad_reach (AdReach-shaped)
    ads = result.get("ads")
    if isinstance(ads, list):  # search_* / scan_* pages
        shaped = []
        for ad in ads:
            if not isinstance(ad, dict):
                shaped.append(ad)
                continue
            ad.pop("raw", None)
            if isinstance(ad.get("reach"), dict):
                ad["reach"].pop("raw", None)
            shaped.append(
                ad if verbose
                else {k: ad[k] for k in DEFAULT_AD_FIELDS if k in ad}
            )
        result["ads"] = shaped
    return result


async def _call(method: str, **kwargs) -> dict:
    """Run a blocking client method in a thread; normalize result/errors to a dict."""
    global _client
    client = _get_client()
    if client is None:
        return {"error": "No session_cache.json — run bootstrap_session() first."}
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(getattr(client, method), **kwargs)
        )
        return result.to_dict() if hasattr(result, "to_dict") else result
    except (StaleDocIdError, SessionExpiredError) as exc:
        _client = None  # force reload next call
        return {"error": f"Session invalid: {exc} Re-run bootstrap_session()."}
    except AdLibraryError as exc:
        return {"error": str(exc)}


@mcp.tool()
async def search_keyword(
    query: str, country: str = "ALL", active: str = "all", ad_type: str = "all",
    limit: int = 30, cursor: str | None = None, with_reach: bool = False,
    verbose: bool = False,
) -> dict:
    """Search the Meta Ad Library by keyword. Returns one page:
    {count, has_next_page, next_cursor, ads}. For the next page, call again with
    cursor=next_cursor. country is an ISO code (BG, DE, ...) or ALL. active is
    all|true|false (true=active only). with_reach=true adds EU reach per ad (slower:
    one extra request per ad). Each ad returns a lean field set by default
    (id, page_id, page_name, creative_bodies, snapshot_url, link_url, title,
    eu_total_reach); pass verbose=true for all fields (raw excluded)."""
    res = await _call(
        "fetch_by_keyword", query=query, country=country, active_status=_active(active),
        ad_type=ad_type, limit=limit, cursor=cursor, with_reach=with_reach,
    )
    return _shape(res, verbose)


@mcp.tool()
async def search_page(
    page_id: str, country: str = "ALL", active: str = "all", ad_type: str = "all",
    limit: int = 30, cursor: str | None = None, with_reach: bool = False,
    verbose: bool = False,
) -> dict:
    """Search all ads from one advertiser page. page_id is the INTERNAL page id
    (the ad.page_id from a keyword result), NOT the number in a facebook.com/<id>
    profile URL. Returns one page; paginate with cursor=next_cursor. Each ad returns a
    lean field set by default; pass verbose=true for all fields (raw excluded)."""
    res = await _call(
        "fetch_by_page_id", page_id=page_id, country=country, active_status=_active(active),
        ad_type=ad_type, limit=limit, cursor=cursor, with_reach=with_reach,
    )
    return _shape(res, verbose)


@mcp.tool()
async def get_ad_reach(ad_archive_id: str, page_id: str, country: str = "BG") -> dict:
    """EU transparency reach for one ad (the 'EU ad delivery' panel): eu_total_reach
    plus targeted countries/age/gender and payer/beneficiary, including the full
    per-country age/gender breakdown. eu_total_reach is null for ads that don't target
    the EU. (The original `raw` node is stripped.)"""
    res = await _call(
        "get_ad_reach", ad_archive_id=ad_archive_id, page_id=page_id, country=country
    )
    return _shape(res)


@mcp.tool()
async def scan_keyword(
    query: str, reach_threshold: int, country: str = "ALL", patience: int = 3,
    active: str = "all", ad_type: str = "all", cursor: str | None = None, streak: int = 0,
    all_pages: bool = False, limit: int = 200, verbose: bool = False,
) -> dict:
    """Find an advertiser's high-reach 'winning' ads. Sorts by impressions desc,
    enriches EU reach, and stops after `patience` ads in a row below reach_threshold.

    By default returns ONE page: {count, done, stop_reason, streak, next_cursor, ads};
    if done is false, call again with cursor=next_cursor AND streak=streak (the streak
    is stateful across pages). Pass all_pages=true to walk every page internally and
    return the COMPLETE result in one call: {threshold, patience, scanned_count,
    stop_reason, ads} (no cursor/streak to thread), capped at `limit` ads — a
    stop_reason of 'limit' means the cap was hit and more may exist. Each ad returns a
    lean field set by default; pass verbose=true for all fields (raw excluded)."""
    if all_pages:
        res = await _call(
            "scan_by_keyword", query=query, country=country,
            reach_threshold=reach_threshold, patience=patience, limit=limit,
            active_status=_active(active), ad_type=ad_type,
        )
    else:
        res = await _call(
            "scan_page_by_keyword", query=query, country=country,
            reach_threshold=reach_threshold, patience=patience,
            active_status=_active(active), ad_type=ad_type, cursor=cursor, streak=streak,
        )
    return _shape(res, verbose)


@mcp.tool()
async def scan_page(
    page_id: str, reach_threshold: int, country: str = "ALL", patience: int = 3,
    active: str = "all", ad_type: str = "all", cursor: str | None = None, streak: int = 0,
    all_pages: bool = False, limit: int = 200, verbose: bool = False,
) -> dict:
    """Like scan_keyword but for one advertiser page (internal page_id). By default
    returns one page; if done is false, call again with cursor=next_cursor and
    streak=streak. Pass all_pages=true to walk every page internally and return the
    COMPLETE result in one call (capped at `limit` ads; stop_reason='limit' means the
    cap was hit). Each ad returns a lean field set by default; pass verbose=true for all
    fields (raw excluded)."""
    if all_pages:
        res = await _call(
            "scan_by_page_id", page_id=page_id, country=country,
            reach_threshold=reach_threshold, patience=patience, limit=limit,
            active_status=_active(active), ad_type=ad_type,
        )
    else:
        res = await _call(
            "scan_page_by_page_id", page_id=page_id, country=country,
            reach_threshold=reach_threshold, patience=patience,
            active_status=_active(active), ad_type=ad_type, cursor=cursor, streak=streak,
        )
    return _shape(res, verbose)


@mcp.tool()
async def session_status(probe: bool = True) -> dict:
    """Check the harvested session. Returns {cached, doc_id, has_details_doc_id,
    age_minutes} and, when probe=true, `valid` from a tiny live request. If valid is
    false, tell the user to re-run bootstrap_session()."""
    if not CACHE_PATH.exists():
        return {"cached": False, "valid": False, "detail": "no session_cache.json"}
    s = SessionData.load(CACHE_PATH)
    info = {
        "cached": True, "doc_id": s.doc_id,
        "has_details_doc_id": s.details_doc_id is not None,
        "age_minutes": round(s.age_seconds / 60, 1),
    }
    if not probe:
        return info
    res = await _call("fetch_by_keyword", query="shop", country="BG", limit=1)
    info["valid"] = "error" not in res
    if "error" in res:
        info["detail"] = res["error"]
    return info


@mcp.tool()
async def bootstrap(country: str = "BG", headless: bool = True) -> dict:
    """Re-harvest the session (doc_id, lsd, cookies + the ad-details doc_id for reach)
    by driving a browser, then reload it. Use this when session_status reports
    valid=false (tokens rotate over time). Runs HEADLESS by default and captures
    everything including reach (the browser is forced to English so the 'See ad details'
    UI is found) — no display needed, so it works on a headless VPS. Pass headless=false
    only if you want to watch / hand-clear a consent dialog. Slow (~20-40s). Returns the
    fresh doc_id and whether the reach (details) doc_id was captured."""
    global _client
    try:
        session = await anyio.to_thread.run_sync(
            functools.partial(
                bootstrap_session, country=country, headless=headless,
                profile_dir=PROFILE_DIR, save_to=str(CACHE_PATH),
            )
        )
    except BootstrapError as exc:
        return {"error": f"Bootstrap failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 — surface any Playwright/runtime failure
        return {"error": f"Bootstrap error: {type(exc).__name__}: {exc}"}
    _client = None  # next call reloads the fresh session
    return {
        "status": "bootstrapped",
        "doc_id": session.doc_id,
        "has_details_doc_id": session.details_doc_id is not None,
    }


class _NormalizeAccept:
    """ASGI middleware that forces `Accept: application/json, text/event-stream`.

    The MCP Streamable HTTP transport returns 406 unless the request's Accept header
    contains BOTH of those media types. Some clients — notably claude.ai's web custom
    connector — send `*/*` (or just `application/json`), which the SDK rejects; the
    connector then misreads the 406 and falls back to a doomed OAuth registration. We
    rewrite the header so any such client can complete the handshake."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [(k, v) for (k, v) in scope["headers"] if k.lower() != b"accept"]
            headers.append((b"accept", b"application/json, text/event-stream"))
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def main() -> None:
    import uvicorn

    app = _NormalizeAccept(mcp.streamable_http_app())
    uvicorn.run(app, host=_HOST, port=_PORT)


if __name__ == "__main__":
    main()
