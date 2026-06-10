"""Browser transport that avoids the rate-limited search query entirely.

The breakthrough (found via live research): loading an Ad Library search URL returns the
first ~30 results **server-rendered in the HTML** — embedded `search_results_connection`
JSON, no GraphQL request at all. Facebook rate-limits `AdLibrarySearchPaginationQuery`
(`code 1675004`) on cold/automated sessions, but the SSR page load is just a normal GET
and isn't throttled. So:

  * SEARCH = navigate, read the embedded `search_results_connection` from the page (0
    GraphQL). Impressions-sorted already; carries the `end_cursor` + `has_next_page`.
  * REACH  = `AdLibraryV3AdDetailsQuery` (confirmed still working), issued via `fetch()`
    inside the page so it carries the real browser's cookies/headers/TLS.

Subclasses AdLibraryClient so reach reuses the parent's get_ad_reach + cache; only the
transport (a _PageHTTP shim) and the search path (SSR extraction) change.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from urllib.parse import parse_qs

from playwright.sync_api import sync_playwright

from .client import _scan_stop
from .client import AdLibraryClient
from .models import Ad, AdPage, AdReach, ScanPage, ScanResult, SessionData
from .parsing import get_page_info, normalize_response
from .session import (
    DETAILS_FRIENDLY_NAME,
    _page_search_url,
    _search_url,
    _try_capture_details_doc_id,
    _try_dismiss_consent,
)

log = logging.getLogger(__name__)

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""

# Pull the server-rendered search_results_connection out of the page's inline JSON
# (no network request). Returns the connection object or null.
_SSR_JS = """() => {
  for (const s of document.querySelectorAll('script')) {
    const t = s.textContent || '';
    if (t.indexOf('"search_results_connection"') === -1) continue;
    try {
      const obj = JSON.parse(t);
      let found = null;
      const walk = (o) => {
        if (found || !o || typeof o !== 'object') return;
        if (o.search_results_connection) { found = o.search_results_connection; return; }
        for (const k in o) { walk(o[k]); if (found) return; }
      };
      walk(obj);
      if (found) return found;
    } catch (e) {}
  }
  return null;
}"""

# In-page fetch for the reach (details) query. The browser supplies cookies + a perfectly
# consistent fingerprint; we pass only the FB-app headers + the urlencoded body.
_FETCH_JS = """
async ([body, headers, timeoutMs]) => {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch("/api/graphql/", {
      method: "POST", headers,
      body: new URLSearchParams(body).toString(),
      credentials: "include", signal: ctrl.signal,
    });
    return { status: resp.status, text: await resp.text() };
  } catch (e) { return { status: 0, text: "" }; }
  finally { clearTimeout(id); }
}
"""

_FORWARD_HEADERS = ("content-type", "x-fb-lsd", "x-fb-friendly-name", "x-asbd-id")
_IMPRESSIONS_SORT_QS = "&sort_data[mode]=total_impressions&sort_data[direction]=desc"


class _Resp:
    __slots__ = ("status_code", "text")

    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class _PageHTTP:
    """curl_cffi-compatible `.post()` that runs an in-page fetch, so the inherited
    get_ad_reach / _graphql work unchanged."""

    def __init__(self, client: "BrowserFetchClient"):
        self.client = client

    def post(self, url, data=None, headers=None, cookies=None, timeout=None):
        fwd = {k: v for k, v in (headers or {}).items()
               if v is not None and k.lower() in _FORWARD_HEADERS}
        if self.client._asbd_id and "x-asbd-id" not in fwd:
            fwd["x-asbd-id"] = self.client._asbd_id
        page = self.client._page
        if page is None:
            return _Resp(0, "")
        try:
            res = page.evaluate(_FETCH_JS, [data or {}, fwd, int((timeout or 30) * 1000)])
        except Exception as exc:  # noqa: BLE001
            log.warning("in-page fetch failed: %s", exc)
            return _Resp(0, "")
        status, text = int(res.get("status", 0)), res.get("text", "")
        if "1675004" in text:
            log.warning("in-page fetch %s -> RATE LIMIT 1675004",
                        fwd.get("x-fb-friendly-name", "?"))
        return _Resp(status, text)


class BrowserFetchClient(AdLibraryClient):
    def __init__(self, headless: bool = False, reach_cache=None, keep_open_seconds: int = 0,
                 profile_dir: str | None = None):
        placeholder = SessionData(doc_id="", lsd="", cookies={}, user_agent="")
        super().__init__(placeholder, request_delay=0.0, min_delay=0.0, max_delay=0.0,
                         reach_workers=1, reach_cache=reach_cache)
        self.keep_open_seconds = keep_open_seconds
        self.headless = headless and not keep_open_seconds
        self.profile_dir = profile_dir  # persistent profile warms the session across runs
        self._page = None
        self._asbd_id = None

    def _thread_http(self):
        return self._http

    # -- browser session ----------------------------------------------------

    def _launch(self, p):
        args = ["--disable-blink-features=AutomationControlled"]
        ignore = ["--enable-automation"]
        if self.profile_dir:
            ctx = p.chromium.launch_persistent_context(
                self.profile_dir, headless=self.headless, args=args,
                ignore_default_args=ignore, locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            ctx.add_init_script(_STEALTH_INIT)
            return None, ctx
        browser = p.chromium.launch(headless=self.headless, args=args, ignore_default_args=ignore)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1440, "height": 900})
        ctx.add_init_script(_STEALTH_INIT)
        return browser, ctx

    @contextmanager
    def _open(self, url: str, need_reach: bool):
        """Navigate to the search URL (gives us the SSR results). When reach is needed,
        click one 'See ad details' to capture the live reach tokens (lsd/jazoest/doc_id)."""
        cap: dict = {}

        def on_request(req):
            if req.method != "POST" or "/api/graphql" not in req.url:
                return
            pd = req.post_data
            if not pd:
                return
            qs = parse_qs(pd, keep_blank_values=True)
            if (qs.get("fb_api_req_friendly_name") or [None])[0] == DETAILS_FRIENDLY_NAME \
                    and "details" not in cap:
                cap["details"] = {k: (v[0] if v else "") for k, v in qs.items()}
                cap["asbd"] = req.headers.get("x-asbd-id")

        with sync_playwright() as p:
            browser, context = self._launch(p)
            context.on("request", on_request)
            try:
                page = context.new_page()
                log.info("browser-fetch(SSR): loading %s", url)
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                _try_dismiss_consent(page)
                if need_reach:
                    _try_capture_details_doc_id(page, {})  # click to fire the details query
                    waited = 0
                    while "details" not in cap and waited < 9000:
                        page.wait_for_timeout(500)
                        waited += 500
                    if "details" in cap:
                        self._apply_reach_capture(cap)
                    else:
                        log.warning("browser-fetch: could not capture reach tokens "
                                    "(no AdLibraryV3AdDetailsQuery seen)")
                self._page = page
                self._http = _PageHTTP(self)
                if self.keep_open_seconds:
                    log.info("KEEP_OPEN: page loaded. Open DevTools > Network now; "
                             "work starts in 12s.")
                    page.wait_for_timeout(12000)
                yield page
            finally:
                if self.keep_open_seconds and self._page is not None:
                    log.info("KEEP_OPEN: done — leaving browser open %ss.",
                             self.keep_open_seconds)
                    try:
                        self._page.wait_for_timeout(self.keep_open_seconds * 1000)
                    except Exception:  # noqa: BLE001
                        pass
                self._page = None
                context.close()
                if browser is not None:
                    browser.close()

    def _apply_reach_capture(self, cap: dict) -> None:
        d = cap["details"]
        self._asbd_id = cap.get("asbd")
        self.session = SessionData(
            doc_id="", lsd=d.get("lsd", ""), cookies={}, user_agent="",
            details_doc_id=d.get("doc_id"), jazoest=d.get("jazoest", ""),
            fb_dtsg=d.get("fb_dtsg", ""), request_template=d,
        )
        log.info("browser-fetch: reach tokens captured (details_doc_id=%s)",
                 bool(self.session.details_doc_id))

    # -- SSR extraction -----------------------------------------------------

    def _ssr(self, page, limit: int | None) -> tuple[list[Ad], str | None, bool]:
        conn = page.evaluate(_SSR_JS)
        if not conn:
            log.warning("browser-fetch: no server-rendered results found on the page")
            return [], None, False
        response = {"data": {"ad_library_main": {"search_results_connection": conn}}}
        ads = normalize_response(response)
        end_cursor, has_next = get_page_info(response)
        return (ads[:limit] if limit else ads), end_cursor, has_next

    def _reach_each(self, ads: list[Ad], country: str, threshold: int | None = None,
                    patience: int = 3) -> tuple[list[Ad], str]:
        """Enrich ads with reach via in-page fetch (serial; cached). If threshold is set,
        apply the consecutive-below-threshold streak stop and return (kept, reason)."""
        kept: list[Ad] = []
        streak = 0
        reason = "exhausted"
        for ad in ads:
            if ad.id:
                try:
                    # Grandparent get_ad_reach: uses _graphql -> _PageHTTP (in-page fetch).
                    r = AdLibraryClient.get_ad_reach(self, ad.id, ad.page_id, country=country)
                    ad.eu_total_reach = r.eu_total_reach
                    ad.reach = r.to_dict()
                except Exception as exc:  # noqa: BLE001
                    log.warning("browser-fetch: reach failed for %s: %s", ad.id, exc)
            kept.append(ad)
            if threshold is not None:
                stop_k, streak = _scan_stop([(ad.eu_total_reach or 0) < threshold], patience, streak)
                if stop_k is not None:
                    reason = "streak"
                    break
        if self.reach_cache is not None:
            self.reach_cache.save()
        return kept, reason

    # -- public API ---------------------------------------------------------

    def fetch_by_keyword(self, query, country="ALL", active_status="all", ad_type="all",
                         limit=30, cursor=None, with_reach=False):
        url = _apply_filters(_search_url(_c1(country), query), active_status, ad_type)
        with self._open(url, need_reach=with_reach):
            ads, end_cursor, has_next = self._ssr(self._page, limit or 30)
            if with_reach and ads:
                self._reach_each(ads, _c1(country))
            return AdPage(ads=ads, end_cursor=end_cursor, has_next_page=has_next)

    def fetch_by_page_id(self, page_id, country="ALL", active_status="all", ad_type="all",
                         limit=30, cursor=None, with_reach=False):
        url = _apply_filters(_page_search_url(_c1(country), str(page_id)), active_status, ad_type)
        with self._open(url, need_reach=with_reach):
            ads, end_cursor, has_next = self._ssr(self._page, limit or 30)
            if with_reach and ads:
                self._reach_each(ads, _c1(country))
            return AdPage(ads=ads, end_cursor=end_cursor, has_next_page=has_next)

    def get_ad_reach(self, ad_archive_id, page_id, country="BG", **kw):
        with self._open(_search_url(country, "shop"), need_reach=True):
            return AdLibraryClient.get_ad_reach(self, ad_archive_id, page_id, country=country)

    def _scan(self, url, reach_threshold, patience, limit, country) -> ScanResult:
        with self._open(url + _IMPRESSIONS_SORT_QS, need_reach=True):
            ads, _, _ = self._ssr(self._page, limit)
            kept, reason = self._reach_each(ads, country, threshold=reach_threshold, patience=patience)
            return ScanResult(reach_threshold, patience, len(kept), reason, kept)

    def scan_by_keyword(self, query, country="ALL", reach_threshold=0, patience=3, limit=200,
                        active_status="all", ad_type="all"):
        url = _apply_filters(_search_url(_c1(country), query), active_status, ad_type)
        return self._scan(url, reach_threshold, patience, limit or 200, _c1(country))

    def scan_by_page_id(self, page_id, country="ALL", reach_threshold=0, patience=3, limit=200,
                        active_status="all", ad_type="all"):
        url = _apply_filters(_page_search_url(_c1(country), str(page_id)), active_status, ad_type)
        return self._scan(url, reach_threshold, patience, limit or 200, _c1(country))

    # Browser does single-call collection (SSR is one page) — no cursor to resume; the
    # paginated scan tools return the full result as one done page.
    def scan_page_by_keyword(self, query, country="ALL", reach_threshold=0, patience=3,
                             cursor=None, streak=0, active_status="all", ad_type="all"):
        res = self.scan_by_keyword(query, country, reach_threshold, patience, 200,
                                   active_status, ad_type)
        return ScanPage(res.ads, True, res.stop_reason, 0, None)

    def scan_page_by_page_id(self, page_id, country="ALL", reach_threshold=0, patience=3,
                             cursor=None, streak=0, active_status="all", ad_type="all"):
        res = self.scan_by_page_id(page_id, country, reach_threshold, patience, 200,
                                   active_status, ad_type)
        return ScanPage(res.ads, True, res.stop_reason, 0, None)


def _c1(country) -> str:
    if isinstance(country, str):
        return country
    seq = list(country)
    return seq[0] if seq else "ALL"


def _apply_filters(url: str, active_status: str, ad_type: str) -> str:
    if active_status and active_status != "all":
        url = url.replace("active_status=all", f"active_status={active_status}")
    if ad_type and ad_type != "all":
        url = url.replace("ad_type=all", f"ad_type={ad_type}")
    return url
