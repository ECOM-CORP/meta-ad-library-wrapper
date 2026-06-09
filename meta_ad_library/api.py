"""Thin FastAPI layer over the wrapper. Runs on port 8888 (browser-safe; 6000 is
blocked by browsers as an unsafe port).

The session is loaded from session_cache.json (created by bootstrap_session). The
blocking client/Playwright calls are pushed to a threadpool so they don't block the
event loop. If tokens have rotated, search endpoints return 503 telling you to
re-bootstrap (or call POST /session/bootstrap, which opens a browser on this machine).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from .client import AdLibraryClient
from .exceptions import (
    AdLibraryError,
    BootstrapError,
    SessionExpiredError,
    StaleDocIdError,
)
from .models import SessionData
from .session import bootstrap_session

CACHE_PATH = Path("session_cache.json")

app = FastAPI(title="Meta Ad Library (unofficial) wrapper", version="0.1.0")

_client: AdLibraryClient | None = None


def _get_client() -> AdLibraryClient:
    global _client
    if _client is None:
        if not CACHE_PATH.exists():
            raise HTTPException(
                status_code=503,
                detail="No session. Run bootstrap_session() or POST /session/bootstrap.",
            )
        _client = AdLibraryClient(SessionData.load(CACHE_PATH))
    return _client


def _invalidate() -> None:
    global _client
    _client = None


_ACTIVE_MAP = {"all": "all", "true": "active", "false": "inactive"}


def _active_status(active: str) -> str:
    key = active.strip().lower()
    if key not in _ACTIVE_MAP:
        raise HTTPException(
            status_code=400,
            detail="active must be one of: all | true | false "
            "(all=active+inactive, true=active only, false=inactive only).",
        )
    return _ACTIVE_MAP[key]


async def _run(method: str, **kwargs):
    client = _get_client()
    try:
        return await run_in_threadpool(getattr(client, method), **kwargs)
    except (StaleDocIdError, SessionExpiredError) as exc:
        _invalidate()
        raise HTTPException(
            status_code=503,
            detail=f"Session invalid: {exc} Re-bootstrap via POST /session/bootstrap.",
        ) from exc
    except AdLibraryError as exc:
        # e.g. reach lookup with no details_doc_id in the cached session.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/health")
async def health():
    return {"status": "ok", "session_cached": CACHE_PATH.exists()}


@app.get("/session/status")
async def session_status(probe: bool = Query(True, description="make a tiny live request to verify tokens still work")):
    """Report whether a session is cached and (optionally) whether it still works.
    `valid` requires probe=true; without it only cached metadata is returned."""
    if not CACHE_PATH.exists():
        return {"cached": False, "valid": False, "detail": "no session_cache.json — run bootstrap"}
    session = SessionData.load(CACHE_PATH)
    info = {
        "cached": True,
        "doc_id": session.doc_id,
        "has_details_doc_id": session.details_doc_id is not None,
        "age_minutes": round(session.age_seconds / 60, 1),
    }
    if not probe:
        return info
    try:
        client = _get_client()
        await run_in_threadpool(client.fetch_by_keyword, query="shop", country="BG", limit=1)
        info["valid"] = True
    except (StaleDocIdError, SessionExpiredError) as exc:
        _invalidate()
        info["valid"] = False
        info["detail"] = f"{exc} Re-bootstrap via POST /session/bootstrap."
    except AdLibraryError as exc:
        info["valid"] = False
        info["detail"] = str(exc)
    return info


_DEFAULT_LIMIT = 30


def _limit(limit: int | None, max_results: int | None, default: int = _DEFAULT_LIMIT) -> int:
    # `max_results` is a friendly alias for `limit`; default to a sane chunk so omitting
    # both doesn't accidentally walk every page.
    if limit is not None:
        return limit
    if max_results is not None:
        return max_results
    return default


@app.get("/search/page")
async def search_page(
    page_id: str = Query(..., description="INTERNAL page id (ad.page_id), not the profile-URL id"),
    country: str = Query("ALL", description="ISO code (BG, DE, ...) or ALL"),
    active: str = Query("all", description="all | true (active only) | false (inactive only)"),
    ad_type: str = Query("all"),
    limit: int | None = Query(None, description="max ads (whole pages of 10); default 30"),
    max_results: int | None = Query(None, description="alias for limit"),
    cursor: str | None = Query(None, description="next_cursor from a previous response"),
    with_reach: bool = Query(False, description="enrich each ad with EU reach (1 extra request per ad)"),
):
    page = await _run(
        "fetch_by_page_id", page_id=page_id, country=country,
        active_status=_active_status(active), ad_type=ad_type,
        limit=_limit(limit, max_results), cursor=cursor, with_reach=with_reach,
    )
    return page.to_dict()


@app.get("/search/keyword")
async def search_keyword(
    query: str = Query(...),
    country: str = Query("ALL", description="ISO code (BG, DE, ...) or ALL"),
    active: str = Query("all", description="all | true (active only) | false (inactive only)"),
    ad_type: str = Query("all"),
    limit: int | None = Query(None, description="max ads (whole pages of 10); default 30"),
    max_results: int | None = Query(None, description="alias for limit"),
    cursor: str | None = Query(None, description="next_cursor from a previous response"),
    with_reach: bool = Query(False, description="enrich each ad with EU reach (1 extra request per ad)"),
):
    page = await _run(
        "fetch_by_keyword", query=query, country=country,
        active_status=_active_status(active), ad_type=ad_type,
        limit=_limit(limit, max_results), cursor=cursor, with_reach=with_reach,
    )
    return page.to_dict()


@app.get("/count/keyword")
async def count_keyword(
    query: str = Query(...),
    country: str = Query("ALL"),
    active: str = Query("all"),
    ad_type: str = Query("all"),
    max_pages: int | None = Query(None, description="safety cap; Meta returns no total, so this walks all pages"),
):
    total, complete = await _run(
        "count_by_keyword", query=query, country=country,
        active_status=_active_status(active), ad_type=ad_type, max_pages=max_pages,
    )
    return {"total": total, "complete": complete}


@app.get("/count/page")
async def count_page(
    page_id: str = Query(...),
    country: str = Query("ALL"),
    active: str = Query("all"),
    ad_type: str = Query("all"),
    max_pages: int | None = Query(None),
):
    total, complete = await _run(
        "count_by_page_id", page_id=page_id, country=country,
        active_status=_active_status(active), ad_type=ad_type, max_pages=max_pages,
    )
    return {"total": total, "complete": complete}


@app.get("/scan/keyword")
async def scan_keyword(
    query: str = Query(...),
    country: str = Query("ALL", description="ISO code (BG, DE, ...) or ALL"),
    reach_threshold: int = Query(..., description="stop after `patience` ads in a row with EU reach below this"),
    patience: int = Query(3, description="consecutive ads below threshold required to stop"),
    active: str = Query("all", description="all | true | false"),
    ad_type: str = Query("all"),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    streak: int = Query(0, description="streak from the previous page (pass it back)"),
):
    """ONE page of a scan (impressions-desc, reach-enriched). If `done` is false, call
    again with cursor=next_cursor and streak=streak. Stops when reach drops off
    (done + stop_reason=streak) or results are exhausted."""
    page = await _run(
        "scan_page_by_keyword", query=query, country=country, reach_threshold=reach_threshold,
        patience=patience, cursor=cursor, streak=streak,
        active_status=_active_status(active), ad_type=ad_type,
    )
    return page.to_dict()


@app.get("/scan/page")
async def scan_page(
    page_id: str = Query(..., description="INTERNAL page id (ad.page_id)"),
    country: str = Query("ALL", description="ISO code (BG, DE, ...) or ALL"),
    reach_threshold: int = Query(..., description="stop after `patience` ads in a row with EU reach below this"),
    patience: int = Query(3, description="consecutive ads below threshold required to stop"),
    active: str = Query("all", description="all | true | false"),
    ad_type: str = Query("all"),
    cursor: str | None = Query(None, description="next_cursor from the previous page"),
    streak: int = Query(0, description="streak from the previous page (pass it back)"),
):
    page = await _run(
        "scan_page_by_page_id", page_id=page_id, country=country, reach_threshold=reach_threshold,
        patience=patience, cursor=cursor, streak=streak,
        active_status=_active_status(active), ad_type=ad_type,
    )
    return page.to_dict()


@app.get("/ad/reach")
async def ad_reach(
    ad_archive_id: str = Query(..., description="the ad id (ad.id / ad_archive_id)"),
    page_id: str = Query(..., description="the ad's internal page_id (ad.page_id)"),
    country: str = Query("BG", description="viewing-context country; reach is EU-wide"),
):
    """EU transparency reach for one ad. eu_total_reach is null for non-EU-targeted ads."""
    reach = await _run(
        "get_ad_reach", ad_archive_id=ad_archive_id, page_id=page_id, country=country
    )
    return reach.to_dict()


@app.post("/session/bootstrap")
async def session_bootstrap(country: str = Query("BG"), headless: bool = Query(False)):
    """Re-harvest a session by opening the Ad Library in a browser on this machine."""
    try:
        session = await run_in_threadpool(
            bootstrap_session, country=country, headless=headless, save_to=CACHE_PATH
        )
    except BootstrapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _invalidate()
    return {
        "status": "bootstrapped",
        "doc_id": session.doc_id,
        "has_v": session.variables_version is not None,
    }
