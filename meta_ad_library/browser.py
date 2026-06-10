"""Browser-driven Ad Library client (Playwright).

An alternative to the curl_cffi replay: instead of replaying GraphQL with harvested
tokens, this drives a REAL Chromium at the public Ad Library and reads the results off
the network — exactly the requests the page fires itself. Used to test/avoid the
`code 1675004` rate limit, since a genuine browser (same IP) browses freely.

Headed by default (MCP_HEADLESS=0) so you can watch what happens. Light stealth: the
`navigator.webdriver` flag is removed and the AutomationControlled blink feature is
disabled. It exposes the same method names + return types (AdPage / AdReach /
ScanResult) as AdLibraryClient, so it's a drop-in for the MCP tools.

Tradeoff: much slower than replay (real render + human-paced scroll/clicks), and reach
still costs one ad-details view per ad (detail-only data). But it looks human.
"""

from __future__ import annotations

import json
import logging
import random
import re
from typing import Iterable

from playwright.sync_api import sync_playwright

from .client import IMPRESSIONS_DESC_SORT
from .models import Ad, AdPage, AdReach, ScanPage, ScanResult
from .parsing import (
    AD_SNAPSHOT_URL,
    get_connection,
    normalize_ad_details,
    normalize_response,
)
from .session import (
    SEARCH_FRIENDLY_NAME,
    _page_search_url,
    _search_url,
    _try_dismiss_consent,
)

log = logging.getLogger(__name__)

_FORJSON = re.compile(r"^\s*for\s*\(;;\);")
_GRAPHQL = "/api/graphql"

# A small pool of realistic current Chrome UAs. Off by default (a spoofed UA without
# matching client-hints can look *more* bot-like); enable with stealth_ua=True.
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

# Removes the headless/automation tells most sites check first.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


def _strip(text: str) -> dict | None:
    try:
        return json.loads(_FORJSON.sub("", text, count=1).strip())
    except (json.JSONDecodeError, AttributeError):
        return None


class BrowserAdClient:
    """Drives the Ad Library in a real browser. Drop-in for the MCP tools."""

    def __init__(
        self,
        headless: bool = False,
        reach_cache=None,
        stealth_ua: bool = False,
        min_pause: float = 0.8,
        max_pause: float = 2.0,
        scroll_stagnant_limit: int = 6,
        reach_cap: int = 60,
    ):
        self.headless = headless
        self.reach_cache = reach_cache
        self.stealth_ua = stealth_ua
        self.min_pause = min_pause
        self.max_pause = max_pause
        self.scroll_stagnant_limit = scroll_stagnant_limit
        self.reach_cap = reach_cap

    # -- context management -------------------------------------------------

    def _pause(self, page) -> None:
        page.wait_for_timeout(int(random.uniform(self.min_pause, self.max_pause) * 1000))

    def _open(self, p):
        """Launch a fresh stealth browser + context (fresh cookies = fresh identity)."""
        browser = p.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        ctx_kwargs = dict(
            locale="en-US",
            timezone_id="Europe/Sofia",
            viewport={"width": 1440, "height": 900},
        )
        if self.stealth_ua:
            ctx_kwargs["user_agent"] = random.choice(_UA_POOL)
        context = browser.new_context(**ctx_kwargs)
        context.add_init_script(_STEALTH_INIT)
        return browser, context

    # -- search (scroll + intercept) ----------------------------------------

    def _collect(self, url: str, limit: int, impressions_sort: bool, page) -> list[Ad]:
        """Scroll the results feed, intercepting AdLibrarySearchPaginationQuery responses
        until we have `limit` ads (or the feed stops growing)."""
        seen: dict[str, Ad] = {}
        order: list[str] = []

        if impressions_sort:
            page.route("**" + _GRAPHQL + "**", self._sort_route)

        def on_response(resp):
            try:
                if resp.request.method != "POST" or _GRAPHQL not in resp.url:
                    return
                body = resp.text()
            except Exception:  # noqa: BLE001 — body may be unavailable; ignore
                return
            data = _strip(body)
            if not data or not get_connection(data):
                return
            for ad in normalize_response(data):
                if ad.id and ad.id not in seen:
                    seen[ad.id] = ad
                    order.append(ad.id)

        page.on("response", on_response)
        log.info("browser: navigating %s", url)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _try_dismiss_consent(page)

        stagnant = 0
        last = 0
        while len(order) < limit and stagnant < self.scroll_stagnant_limit:
            page.mouse.wheel(0, 6000)
            self._pause(page)
            if len(order) > last:
                log.info("browser: %d ads so far", len(order))
                last = len(order)
                stagnant = 0
            else:
                stagnant += 1
        page.remove_listener("response", on_response)
        return [seen[i] for i in order[:limit]]

    def _sort_route(self, route):
        """Inject impressions-desc sortData into the page's own search request (the UI
        offers no sort control for commercial ads). Keeps it a real browser request."""
        req = route.request
        body = req.post_data or ""
        if req.method == "POST" and SEARCH_FRIENDLY_NAME in body:
            from urllib.parse import parse_qs, urlencode

            flat = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
            try:
                variables = json.loads(flat.get("variables", "{}"))
                variables["sortData"] = IMPRESSIONS_DESC_SORT
                flat["variables"] = json.dumps(variables, separators=(",", ":"))
                route.continue_(post_data=urlencode(flat))
                return
            except (json.JSONDecodeError, KeyError):
                pass
        route.continue_()

    # -- reach (per-ad ad-details view) -------------------------------------

    def _reach_on_page(self, page, ad_archive_id: str, country: str) -> AdReach:
        key = str(ad_archive_id)
        if self.reach_cache is not None:
            cached = self.reach_cache.get(key)
            if cached is not None:
                return AdReach(**cached)

        captured: dict = {}

        def on_response(resp):
            if captured or resp.request.method != "POST" or _GRAPHQL not in resp.url:
                return
            try:
                body = resp.text()
            except Exception:  # noqa: BLE001
                return
            data = _strip(body)
            det = (((data or {}).get("data") or {}).get("ad_library_main") or {}).get(
                "ad_details"
            )
            if det is not None:
                captured["data"] = data

        page.on("response", on_response)
        url = AD_SNAPSHOT_URL.format(ad_archive_id=key) + f"&country={country}"
        log.info("browser: reach for ad %s", key)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _try_dismiss_consent(page)
        # The single-ad view shows a "See ad details" / "See summary details" control
        # that fires AdLibraryV3AdDetailsQuery. Best-effort click, then wait for it.
        for label in ("See ad details", "See summary details", "See details"):
            try:
                btn = page.get_by_text(label, exact=False).last
                btn.click(force=True, timeout=2500)
                break
            except Exception:  # noqa: BLE001
                continue
        waited = 0
        while "data" not in captured and waited < 9000:
            page.wait_for_timeout(500)
            waited += 500
        page.remove_listener("response", on_response)

        if "data" in captured:
            reach = normalize_ad_details(captured["data"], ad_archive_id=key)
        else:
            log.warning("browser: no ad-details response captured for %s", key)
            reach = AdReach(ad_archive_id=key, targets_eu=False, eu_total_reach=None)
        if self.reach_cache is not None:
            self.reach_cache.set(key, {k: v for k, v in reach.to_dict().items() if k != "raw"})
        return reach

    def _enrich(self, ads: list[Ad], page, country: str, cap: int | None = None) -> None:
        targets = [a for a in ads if a.id][: cap if cap is not None else len(ads)]
        for ad in targets:
            try:
                r = self._reach_on_page(page, ad.id, country)
                ad.eu_total_reach = r.eu_total_reach
                ad.reach = r.to_dict()
            except Exception as exc:  # noqa: BLE001 — one ad's failure shouldn't kill the batch
                log.warning("browser: reach failed for %s: %s", ad.id, exc)
        if self.reach_cache is not None:
            self.reach_cache.save()

    # -- public API (mirrors AdLibraryClient names the MCP tools call) -------

    def _search(self, url, limit, with_reach, country, impressions_sort=False) -> list[Ad]:
        with sync_playwright() as p:
            browser, context = self._open(p)
            try:
                page = context.new_page()
                ads = self._collect(url, limit, impressions_sort, page)
                if with_reach and ads:
                    self._enrich(ads, page, country, cap=self.reach_cap)
                return ads
            finally:
                context.close()
                browser.close()

    def fetch_by_keyword(
        self, query, country="ALL", active_status="all", ad_type="all",
        limit=30, cursor=None, with_reach=False,
    ) -> AdPage:
        url = _apply_filters(_search_url(_country1(country), query), active_status, ad_type)
        ads = self._search(url, limit or 30, with_reach, _country1(country))
        return AdPage(ads=ads, end_cursor=None, has_next_page=False)

    def fetch_by_page_id(
        self, page_id, country="ALL", active_status="all", ad_type="all",
        limit=30, cursor=None, with_reach=False,
    ) -> AdPage:
        url = _apply_filters(_page_search_url(_country1(country), str(page_id)), active_status, ad_type)
        ads = self._search(url, limit or 30, with_reach, _country1(country))
        return AdPage(ads=ads, end_cursor=None, has_next_page=False)

    def get_ad_reach(self, ad_archive_id, page_id=None, country="BG") -> AdReach:
        with sync_playwright() as p:
            browser, context = self._open(p)
            try:
                page = context.new_page()
                return self._reach_on_page(page, ad_archive_id, country)
            finally:
                context.close()
                browser.close()

    def _scan(self, url, reach_threshold, patience, limit, country) -> ScanResult:
        with sync_playwright() as p:
            browser, context = self._open(p)
            try:
                page = context.new_page()
                # Collect impressions-sorted ads first (cap the list we'll reach-check).
                ads = self._collect(url, min(limit, self.reach_cap), True, page)
                kept: list[Ad] = []
                streak = 0
                reason = "exhausted"
                for ad in ads:
                    if not ad.id:
                        continue
                    try:
                        r = self._reach_on_page(page, ad.id, country)
                        ad.eu_total_reach = r.eu_total_reach
                        ad.reach = r.to_dict()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("browser: scan reach failed for %s: %s", ad.id, exc)
                    kept.append(ad)
                    below = (ad.eu_total_reach or 0) < reach_threshold
                    streak = streak + 1 if below else 0
                    if streak >= patience:
                        reason = "streak"
                        break
                    if len(kept) >= limit:
                        reason = "limit"
                        break
                if self.reach_cache is not None:
                    self.reach_cache.save()
                return ScanResult(reach_threshold, patience, len(kept), reason, kept)
            finally:
                context.close()
                browser.close()

    def scan_by_keyword(
        self, query, country="ALL", reach_threshold=0, patience=3, limit=200,
        active_status="all", ad_type="all",
    ) -> ScanResult:
        url = _apply_filters(_search_url(_country1(country), query), active_status, ad_type)
        return self._scan(url, reach_threshold, patience, limit or 200, _country1(country))

    def scan_by_page_id(
        self, page_id, country="ALL", reach_threshold=0, patience=3, limit=200,
        active_status="all", ad_type="all",
    ) -> ScanResult:
        url = _apply_filters(_page_search_url(_country1(country), str(page_id)), active_status, ad_type)
        return self._scan(url, reach_threshold, patience, limit or 200, _country1(country))

    # The browser does single-call collection — there is no cursor to resume. The
    # paginated scan tools collapse to a full scan returned as one done page.
    def scan_page_by_keyword(
        self, query, country="ALL", reach_threshold=0, patience=3, cursor=None,
        streak=0, active_status="all", ad_type="all",
    ) -> ScanPage:
        res = self.scan_by_keyword(query, country, reach_threshold, patience,
                                   self.reach_cap, active_status, ad_type)
        return ScanPage(res.ads, True, res.stop_reason, 0, None)

    def scan_page_by_page_id(
        self, page_id, country="ALL", reach_threshold=0, patience=3, cursor=None,
        streak=0, active_status="all", ad_type="all",
    ) -> ScanPage:
        res = self.scan_by_page_id(page_id, country, reach_threshold, patience,
                                   self.reach_cap, active_status, ad_type)
        return ScanPage(res.ads, True, res.stop_reason, 0, None)


def _country1(country: str | Iterable[str]) -> str:
    if isinstance(country, str):
        return country
    seq = list(country)
    return seq[0] if seq else "ALL"


def _apply_filters(url: str, active_status: str, ad_type: str) -> str:
    """The base search URLs hardcode active_status=all&ad_type=all; rewrite in place when
    the caller asked for something else (avoids duplicate query params)."""
    if active_status and active_status != "all":
        url = url.replace("active_status=all", f"active_status={active_status}")
    if ad_type and ad_type != "all":
        url = url.replace("ad_type=all", f"ad_type={ad_type}")
    return url
