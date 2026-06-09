"""Session bootstrapper.

This is the layer that makes the wrapper survive Meta's frontend churn. It opens
the real Ad Library page in a browser, lets it fire its own GraphQL request, and
harvests the live `doc_id`, `fb_dtsg`, `lsd`, cookies, and user-agent from that
actual outgoing request — rather than hardcoding values that rot within days.

Default is headed + a persistent profile so the EU cookie-consent dialog can be
cleared once by hand; the profile then carries the consent cookie so later runs
can go headless.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

from playwright.sync_api import Request, sync_playwright

from .exceptions import BootstrapError
from .models import SessionData

GRAPHQL_PATH = "/api/graphql/"
SEARCH_FRIENDLY_NAME = "AdLibrarySearchPaginationQuery"
DETAILS_FRIENDLY_NAME = "AdLibraryV3AdDetailsQuery"

# Params we lift out of the captured POST body and carry along on replayed requests.
# The frontend sends many housekeeping fields; we keep the ones that tend to matter.
_HOUSEKEEPING_KEYS = (
    "__user",
    "__a",
    "__req",
    "__hs",
    "__ccg",
    "__rev",
    "__s",
    "__hsi",
    "__comet_req",
    "dpr",
    "av",
    "__spin_r",
    "__spin_b",
    "__spin_t",
    "fb_api_caller_class",
    "fb_api_req_friendly_name",
    "server_timestamps",
)


def _search_url(country: str, query: str) -> str:
    return (
        "https://www.facebook.com/ads/library/"
        f"?active_status=all&ad_type=all&country={country}"
        f"&q={query}&search_type=keyword_unordered&media_type=all"
    )


def _page_search_url(country: str, page_id: str) -> str:
    return (
        "https://www.facebook.com/ads/library/"
        f"?active_status=all&ad_type=all&country={country}"
        f"&view_all_page_id={page_id}&search_type=page&media_type=all"
    )


def _first_value(qs: dict[str, list[str]], key: str) -> str | None:
    vals = qs.get(key)
    return vals[0] if vals else None


def bootstrap_session(
    country: str = "BG",
    seed_query: str = "shop",
    headless: bool = False,
    profile_dir: str | Path = ".pw-profile",
    timeout_ms: int = 90_000,
    save_to: str | Path | None = "session_cache.json",
) -> SessionData:
    """Open the Ad Library, capture a live GraphQL request, return a SessionData.

    Args:
        country: 2-letter country to seed the search with (e.g. "BG").
        seed_query: a throwaway keyword used only to make the page fire a search.
        headless: default False so a human can clear the consent dialog the first time.
        profile_dir: persistent browser profile dir (keeps the consent cookie).
        timeout_ms: how long to wait for the page to emit a GraphQL request.
        save_to: where to persist the harvested SessionData (None to skip).

    Raises:
        BootstrapError: if no usable GraphQL request was captured.
    """
    profile_dir = str(Path(profile_dir).resolve())
    # `tokens` holds fb_dtsg/lsd/housekeeping (same for every request this session).
    # `search` holds the doc_id + `v` of the ACTUAL search query — which is a
    # different persisted query than the filter-context one that also fires.
    tokens: dict[str, str] = {}
    search: dict = {}

    def _on_request(request: Request) -> None:
        if GRAPHQL_PATH not in request.url or request.method != "POST":
            return
        body = request.post_data
        if not body:
            return
        qs = parse_qs(body, keep_blank_values=True)
        # Logged-out requests carry lsd + jazoest (no fb_dtsg), so gate on lsd.
        lsd = _first_value(qs, "lsd")
        if lsd and "lsd" not in tokens:
            tokens["lsd"] = lsd
            tokens["jazoest"] = _first_value(qs, "jazoest") or ""
            tokens["fb_dtsg"] = _first_value(qs, "fb_dtsg") or ""
            for key in _HOUSEKEEPING_KEYS:
                val = _first_value(qs, key)
                if val is not None:
                    tokens[key] = val

        friendly = _first_value(qs, "fb_api_req_friendly_name")
        if friendly == SEARCH_FRIENDLY_NAME:
            doc_id = _first_value(qs, "doc_id")
            if doc_id and "doc_id" not in search:
                search["doc_id"] = doc_id
                # Keep the FULL body verbatim (flattened) to replay later.
                search["params"] = {k: (v[0] if v else "") for k, v in qs.items()}
                raw_vars = _first_value(qs, "variables")
                if raw_vars:
                    try:
                        search["v"] = json.loads(raw_vars).get("v") or ""
                    except json.JSONDecodeError:
                        pass
        elif friendly == DETAILS_FRIENDLY_NAME:
            doc_id = _first_value(qs, "doc_id")
            if doc_id:
                search["details_doc_id"] = doc_id

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            viewport={"width": 1920, "height": 1080},
            # Force English so our UI selectors ("See ad details", consent buttons)
            # match. Without this the page can render in the country's language (e.g.
            # Bulgarian) — which silently broke the headless details/reach capture.
            locale="en-US",
        )
        try:
            context.on("request", _on_request)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(_search_url(country, seed_query), wait_until="domcontentloaded")

            page.wait_for_timeout(2500)
            _try_dismiss_consent(page)

            # The first batch of results is server-rendered, so the search XHR only
            # fires on scroll. Scroll until we've captured the search doc_id (or time out).
            deadline_reached = _scroll_until_search(page, search, timeout_ms)
            if "doc_id" not in search:
                hint = (
                    "If a cookie-consent or login dialog is open, clear it in the "
                    "browser window and re-run. "
                    if not headless
                    else "Try headless=False to clear the consent dialog. "
                )
                raise BootstrapError(
                    "Could not harvest the AdLibrarySearchPaginationQuery doc_id "
                    f"(seed_query={seed_query!r} country={country!r} returned no "
                    f"search request). {hint}"
                    + ("Increase timeout_ms." if deadline_reached else "")
                )
            if "lsd" not in tokens:
                raise BootstrapError("Captured a search doc_id but no lsd token.")

            # Best-effort: harvest the AdLibraryV3AdDetailsQuery doc_id (EU reach) by
            # opening one ad's "See ad details". Non-fatal if it fails.
            _try_capture_details_doc_id(page, search)

            cookies = {c["name"]: c["value"] for c in context.cookies()}
            user_agent = page.evaluate("() => navigator.userAgent")
        finally:
            context.close()

    extra = {
        k: v for k, v in tokens.items() if k not in ("lsd", "jazoest", "fb_dtsg")
    }
    session = SessionData(
        doc_id=search["doc_id"],
        lsd=tokens.get("lsd", ""),
        jazoest=tokens.get("jazoest", ""),
        fb_dtsg=tokens.get("fb_dtsg", ""),
        cookies=cookies,
        user_agent=user_agent,
        variables_version=search.get("v") or None,
        request_template=search.get("params", {}),
        details_doc_id=search.get("details_doc_id"),
        extra_params=extra,
    )
    if save_to:
        session.save(save_to)
    return session


def _try_capture_details_doc_id(page, search: dict) -> None:
    """Click an ad's 'See ad details' to make the AdLibraryV3AdDetailsQuery fire so we
    can harvest its doc_id. Best-effort: swallow any failure."""
    try:
        btn = page.get_by_text("See ad details", exact=False).last
        btn.wait_for(state="visible", timeout=8000)
        btn.click(force=True, timeout=4000)
    except Exception:
        return
    waited = 0
    while "details_doc_id" not in search and waited < 8000:
        page.wait_for_timeout(500)
        waited += 500


def _scroll_until_search(page, search: dict, timeout_ms: int) -> bool:
    """Scroll the results feed until the search XHR is captured. Returns True on timeout."""
    waited = 0
    step = 1500
    while "doc_id" not in search and waited < timeout_ms:
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(step)
        waited += step
    return waited >= timeout_ms


def _try_dismiss_consent(page) -> None:
    """Best-effort click on a cookie-consent button. Harmless if none is present;
    in headed mode the user can always clear it by hand."""
    selectors = [
        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
        '[aria-label="Allow all cookies"]',
        '[aria-label="Decline optional cookies"]',
        'button[title="Allow all cookies"]',
        'button:has-text("Allow all cookies")',
        'button:has-text("Decline optional cookies")',
        'div[role="button"]:has-text("Allow all cookies")',
        'div[role="button"]:has-text("Decline optional cookies")',
        'div[role="button"]:has-text("Only allow essential cookies")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=1500)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue
