"""Browser-fetch transport: run our GraphQL requests *inside* a real browser page.

Why: replaying with curl_cffi keeps getting rate-limited (code 1675004) even after
matching headers — Meta clearly fingerprints something we can't reliably reproduce from
outside the browser (header order, x-asbd-id, cookie completeness, session age, ...).

The fix: open a real Chromium at the Ad Library, let it load (real cookies + tokens),
capture the live request template from the page's own GraphQL call, then issue OUR
requests with `fetch()` executed in that page's JS context. The browser itself attaches
cookies, User-Agent, sec-ch-ua, sec-fetch-*, and the TLS fingerprint — all perfectly
self-consistent and identical to normal browsing. We only supply the POST body
(doc_id + variables + the captured housekeeping params).

BrowserFetchClient subclasses AdLibraryClient: all the variables/pagination/scan/reach/
parsing logic is reused unchanged. Only the transport (`http.post`) is swapped for an
in-page fetch, and a per-call browser session captures the live tokens.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from urllib.parse import parse_qs

from playwright.sync_api import sync_playwright

from .client import AdLibraryClient
from .exceptions import BootstrapError
from .models import SessionData
from .session import (
    DETAILS_FRIENDLY_NAME,
    SEARCH_FRIENDLY_NAME,
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

# The in-page fetch. Returns {status, text}. The browser supplies cookies + all the
# consistent fingerprint headers; we only pass the FB-app headers + the urlencoded body.
_FETCH_JS = """
async ([body, headers, timeoutMs]) => {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch("/api/graphql/", {
      method: "POST",
      headers: headers,
      body: new URLSearchParams(body).toString(),
      credentials: "include",
      signal: ctrl.signal,
    });
    return { status: resp.status, text: await resp.text() };
  } catch (e) {
    return { status: 0, text: "" };
  } finally { clearTimeout(id); }
}
"""

# fetch() forbids setting UA/sec-ch-ua/sec-fetch/origin/referer/cookie — the browser owns
# those (which is the whole point). We only forward the FB-app headers it won't add itself.
_FORWARD_HEADERS = ("content-type", "x-fb-lsd", "x-fb-friendly-name", "x-asbd-id")


class _Resp:
    __slots__ = ("status_code", "text")

    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text


class _PageHTTP:
    """Adapter with a curl_cffi-compatible `.post()` that runs an in-page fetch, so the
    inherited AdLibraryClient._graphql works unchanged (retries, diagnostics, parsing)."""

    def __init__(self, client: "BrowserFetchClient"):
        self.client = client

    def post(self, url, data=None, headers=None, cookies=None, timeout=None):
        fwd = {
            k: v for k, v in (headers or {}).items()
            if v is not None and k.lower() in _FORWARD_HEADERS
        }
        if self.client._asbd_id and "x-asbd-id" not in fwd:
            fwd["x-asbd-id"] = self.client._asbd_id
        page = self.client._page
        if page is None:
            return _Resp(0, "")
        try:
            res = page.evaluate(_FETCH_JS, [data or {}, fwd, int((timeout or 30) * 1000)])
        except Exception as exc:  # noqa: BLE001 — surface as a transient (empty) response
            log.warning("in-page fetch failed: %s", exc)
            return _Resp(0, "")
        status, text = int(res.get("status", 0)), res.get("text", "")
        flagged = "1675004" in text
        log.info(
            "in-page fetch %s -> status=%s len=%s%s",
            fwd.get("x-fb-friendly-name", "?"), status, len(text),
            " [RATE LIMIT 1675004 — OUR fetch]" if flagged else "",
        )
        return _Resp(status, text)


class BrowserFetchClient(AdLibraryClient):
    def __init__(self, headless: bool = False, reach_cache=None):
        # Placeholder session; filled live from the page each call. Serial reach + no
        # artificial pacing (a real browser fetch needs neither).
        placeholder = SessionData(doc_id="", lsd="", cookies={}, user_agent="")
        super().__init__(
            placeholder, request_delay=0.0, min_delay=0.0, max_delay=0.0,
            reach_workers=1, reach_cache=reach_cache,
        )
        self.headless = headless
        self._page = None
        self._asbd_id = None

    # The browser page isn't thread-shared; everything runs in one worker thread, and the
    # _PageHTTP is the single transport for both search and (serial) reach.
    def _thread_http(self):
        return self._http

    @contextmanager
    def _open(self, seed_url: str, need_reach: bool):
        """Launch Chromium, load the Ad Library, capture the live request template (and the
        ad-details doc_id when reach is needed), and expose the page for in-page fetches."""
        cap: dict = {}

        def on_request(req):
            if req.method != "POST" or "/api/graphql" not in req.url:
                return
            pd = req.post_data
            if not pd:
                return
            qs = parse_qs(pd, keep_blank_values=True)
            fn = (qs.get("fb_api_req_friendly_name") or [None])[0]
            if fn == SEARCH_FRIENDLY_NAME and "template" not in cap:
                cap["template"] = {k: (v[0] if v else "") for k, v in qs.items()}
                cap["asbd"] = req.headers.get("x-asbd-id")
            elif fn == DETAILS_FRIENDLY_NAME and "details_doc_id" not in cap:
                cap["details_doc_id"] = (qs.get("doc_id") or [None])[0]

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            context = browser.new_context(locale="en-US", viewport={"width": 1440, "height": 900})
            context.add_init_script(_STEALTH_INIT)
            context.on("request", on_request)
            try:
                page = context.new_page()
                log.info("browser-fetch: loading %s", seed_url)
                page.goto(seed_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                _try_dismiss_consent(page)
                # The search query fires on scroll — scroll until we capture its template.
                waited = 0
                while "template" not in cap and waited < 30000:
                    page.mouse.wheel(0, 5000)
                    page.wait_for_timeout(1500)
                    waited += 1500
                if "template" not in cap:
                    raise BootstrapError(
                        "browser-fetch: could not capture the search request template "
                        "(no AdLibrarySearchPaginationQuery seen). The page may have shown "
                        "a consent/login wall."
                    )
                if need_reach and "details_doc_id" not in cap:
                    _try_capture_details_doc_id(page, cap)  # clicks 'See ad details'
                self._apply_capture(cap)
                self._page = page
                self._http = _PageHTTP(self)
                yield
            finally:
                self._page = None
                context.close()
                browser.close()

    def _apply_capture(self, cap: dict) -> None:
        tmpl = cap["template"]
        version = None
        try:
            version = json.loads(tmpl.get("variables", "{}")).get("v")
        except (json.JSONDecodeError, AttributeError):
            pass
        self._asbd_id = cap.get("asbd")
        self.session = SessionData(
            doc_id=tmpl.get("doc_id", ""),
            lsd=tmpl.get("lsd", ""),
            cookies={},  # the browser holds the real cookies
            user_agent="",  # the browser sets its own UA
            details_doc_id=cap.get("details_doc_id"),
            jazoest=tmpl.get("jazoest", ""),
            fb_dtsg=tmpl.get("fb_dtsg", ""),
            variables_version=version,
            request_template=tmpl,
        )
        log.info(
            "browser-fetch: captured tokens (doc_id=%s, details=%s)",
            self.session.doc_id, bool(self.session.details_doc_id),
        )

    # -- public API: wrap the inherited logic in a live browser session ------

    def fetch_by_keyword(self, query, country="ALL", active_status="all", ad_type="all",
                         limit=30, cursor=None, with_reach=False):
        with self._open(_search_url(_c1(country), query), need_reach=with_reach):
            return super().fetch_by_keyword(query, country, active_status, ad_type,
                                            limit, cursor, with_reach)

    def fetch_by_page_id(self, page_id, country="ALL", active_status="all", ad_type="all",
                         limit=30, cursor=None, with_reach=False):
        with self._open(_page_search_url(_c1(country), str(page_id)), need_reach=with_reach):
            return super().fetch_by_page_id(page_id, country, active_status, ad_type,
                                            limit, cursor, with_reach)

    def scan_by_keyword(self, query, country="ALL", reach_threshold=0, patience=3,
                        limit=200, active_status="all", ad_type="all"):
        with self._open(_search_url(_c1(country), query), need_reach=True):
            return super().scan_by_keyword(query, country, reach_threshold, patience,
                                           limit, active_status, ad_type)

    def scan_by_page_id(self, page_id, country="ALL", reach_threshold=0, patience=3,
                        limit=200, active_status="all", ad_type="all"):
        with self._open(_page_search_url(_c1(country), str(page_id)), need_reach=True):
            return super().scan_by_page_id(page_id, country, reach_threshold, patience,
                                           limit, active_status, ad_type)

    def scan_page_by_keyword(self, query, country="ALL", reach_threshold=0, patience=3,
                             cursor=None, streak=0, active_status="all", ad_type="all"):
        with self._open(_search_url(_c1(country), query), need_reach=True):
            return super().scan_page_by_keyword(query, country, reach_threshold, patience,
                                                cursor, streak, active_status, ad_type)

    def scan_page_by_page_id(self, page_id, country="ALL", reach_threshold=0, patience=3,
                             cursor=None, streak=0, active_status="all", ad_type="all"):
        with self._open(_page_search_url(_c1(country), str(page_id)), need_reach=True):
            return super().scan_page_by_page_id(page_id, country, reach_threshold, patience,
                                                cursor, streak, active_status, ad_type)

    def get_ad_reach(self, ad_archive_id, page_id, country="BG", **kw):
        # Seed with a throwaway search in `country` to capture template + details doc_id.
        with self._open(_search_url(country, "shop"), need_reach=True):
            return super().get_ad_reach(ad_archive_id, page_id, country=country)


def _c1(country) -> str:
    if isinstance(country, str):
        return country
    seq = list(country)
    return seq[0] if seq else "ALL"
