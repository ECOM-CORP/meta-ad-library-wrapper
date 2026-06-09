"""HTTP client that replays the Ad Library's GraphQL search, with pagination.

Built against real captured requests (see ./samples/). The `variables` structure and
the POST field set mirror what the website itself sends.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

log = logging.getLogger(__name__)

# curl_cffi (not plain requests): Meta's edge TLS-fingerprints clients and soft-rejects
# non-browser handshakes with error 1357054. curl_cffi impersonates Chrome's TLS so the
# replayed request is accepted; its API is otherwise requests-compatible.
from curl_cffi import requests as cffi_requests

from .exceptions import (
    AdLibraryError,
    SessionExpiredError,
    StaleDocIdError,
    TransientError,
)
from .models import Ad, AdPage, AdReach, ScanPage, ScanResult, SessionData
from .parsing import get_page_info, normalize_ad_details, normalize_response
from .session import DETAILS_FRIENDLY_NAME, SEARCH_FRIENDLY_NAME

# Sort used by scan() so high-reach ads cluster first (reach itself isn't sortable).
IMPRESSIONS_DESC_SORT = {"direction": "DESCENDING", "mode": "SORT_BY_TOTAL_IMPRESSIONS"}

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
# Anti-JSON-hijacking prefix Meta prepends to responses; strip before json.loads.
_FORJSON_PREFIX = re.compile(r"^\s*for\s*\(;;\);")
# Substrings that mark a spurious/transient server error (worth retrying, not stale).
_TRANSIENT_MARKERS = (
    "server error",
    "missing_required_variable",
    "please try again",
    "temporarily",
    "rate limit",
    "timed out",
    "try again later",
)


def _jazoest(fb_dtsg: str) -> str:
    """jazoest is a checksum of fb_dtsg the frontend sends alongside it."""
    return "2" + str(sum(ord(c) for c in fb_dtsg))


def _is_transient(message: str) -> bool:
    low = (message or "").lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


def _reach_country(country) -> str:
    """The reach query needs a single country string (ALL is accepted)."""
    if isinstance(country, str):
        return country
    seq = list(country)
    return seq[0] if seq else "ALL"


def _scan_stop(below_flags: list[bool], patience: int, streak_in: int = 0) -> tuple[int | None, int]:
    """Walk the below-threshold flags (continuing from streak_in). Return
    (ads_to_keep, streak_out): ads_to_keep is the count once `patience` consecutive
    below-threshold ads occur, else None. streak_out is the running streak to carry to
    the next page. Pure function so the stop logic is unit-testable without network."""
    streak = streak_in
    for i, below in enumerate(below_flags):
        streak = streak + 1 if below else 0
        if streak >= patience:
            return i + 1, streak
    return None, streak


class AdLibraryClient:
    def __init__(
        self,
        session: SessionData,
        request_delay: float = 1.0,
        page_size: int = 10,  # Meta fixes the page size at 10 regardless of `first`
        timeout: float = 30.0,
        impersonate: str = "chrome",
        retries: int = 3,
        retry_backoff: float = 1.5,
    ):
        self.session = session
        self.request_delay = request_delay
        self.page_size = page_size
        self.timeout = timeout
        self.impersonate = impersonate
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.session_id = str(uuid.uuid4())
        self._http = cffi_requests.Session(impersonate=impersonate)
        # curl_cffi sessions aren't shared across threads; give each worker its own
        # for parallel reach enrichment.
        self._local = threading.local()

    def _thread_http(self):
        http = getattr(self._local, "http", None)
        if http is None:
            http = cffi_requests.Session(impersonate=self.impersonate)
            self._local.http = http
        return http

    # -- public API: auto-collect (returns a flat list) ---------------------

    def search_by_page_id(
        self,
        page_id: str,
        country: str | Iterable[str] = "ALL",
        active_status: str = "all",
        ad_type: str = "all",
        max_results: int | None = None,
    ) -> list[Ad]:
        page = self._collect(self._page_vars(page_id, country, active_status, ad_type),
                             max_results, cursor=None)
        return page.ads[:max_results] if max_results is not None else page.ads

    def search_by_keyword(
        self,
        query: str,
        country: str | Iterable[str] = "ALL",
        active_status: str = "all",
        ad_type: str = "all",
        max_results: int | None = None,
    ) -> list[Ad]:
        page = self._collect(self._keyword_vars(query, country, active_status, ad_type),
                             max_results, cursor=None)
        return page.ads[:max_results] if max_results is not None else page.ads

    # -- public API: cursor pages (returns AdPage with next_cursor) ----------

    def fetch_by_page_id(
        self,
        page_id: str,
        country: str | Iterable[str] = "ALL",
        active_status: str = "all",
        ad_type: str = "all",
        limit: int | None = None,
        cursor: str | None = None,
        with_reach: bool = False,
    ) -> AdPage:
        page = self._collect(self._page_vars(page_id, country, active_status, ad_type),
                             limit, cursor)
        if with_reach:
            self.enrich_with_reach(page.ads, country=_reach_country(country))
        return page

    def fetch_by_keyword(
        self,
        query: str,
        country: str | Iterable[str] = "ALL",
        active_status: str = "all",
        ad_type: str = "all",
        limit: int | None = None,
        cursor: str | None = None,
        with_reach: bool = False,
    ) -> AdPage:
        page = self._collect(self._keyword_vars(query, country, active_status, ad_type),
                             limit, cursor)
        if with_reach:
            self.enrich_with_reach(page.ads, country=_reach_country(country))
        return page

    # -- public API: exact total (walks all pages; Meta returns no count) ----

    def count_by_keyword(
        self,
        query: str,
        country: str | Iterable[str] = "ALL",
        active_status: str = "all",
        ad_type: str = "all",
        max_pages: int | None = None,
    ) -> tuple[int, bool]:
        """Return (total_ads, complete). `complete` is False if capped by max_pages."""
        return self._count(self._keyword_vars(query, country, active_status, ad_type),
                           max_pages)

    def count_by_page_id(
        self,
        page_id: str,
        country: str | Iterable[str] = "ALL",
        active_status: str = "all",
        ad_type: str = "all",
        max_pages: int | None = None,
    ) -> tuple[int, bool]:
        return self._count(self._page_vars(page_id, country, active_status, ad_type),
                           max_pages)

    # -- public API: EU reach (separate ad-details query) -------------------

    def get_ad_reach(
        self,
        ad_archive_id: str,
        page_id: str,
        country: str = "BG",
        *,
        is_non_political: bool = True,
        is_not_aaa_eligible: bool = False,
        http=None,
    ) -> AdReach:
        """Fetch the EU transparency reach for one ad (the 'EU ad delivery' panel).
        `country` is the viewing context; reach itself is EU-wide. Returns an AdReach
        with eu_total_reach=None for ads that don't target the EU."""
        if not self.session.details_doc_id:
            raise AdLibraryError(
                "No details_doc_id in this session (reach lookup unavailable). "
                "Re-run bootstrap_session() to harvest it."
            )
        variables = {
            "adArchiveID": str(ad_archive_id),
            "pageID": str(page_id),
            "country": country,
            "sessionID": self.session_id,
            "source": None,
            "isAdNonPolitical": is_non_political,
            "isAdNotAAAEligible": is_not_aaa_eligible,
        }
        data = self._graphql(
            self.session.details_doc_id, DETAILS_FRIENDLY_NAME, variables, http=http
        )
        return normalize_ad_details(data, ad_archive_id=str(ad_archive_id))

    def reach_for_ad(self, ad: Ad, country: str = "BG") -> AdReach:
        """Convenience: fetch reach for an Ad object (uses its id + page_id)."""
        return self.get_ad_reach(ad.id, ad.page_id, country=country)

    def enrich_with_reach(
        self, ads: list[Ad], country: str = "BG", max_workers: int = 6
    ) -> list[Ad]:
        """Populate `eu_total_reach` + `reach` on each ad via the per-ad details query,
        in parallel. One extra request per ad (Meta has no batch). Individual transient
        failures leave that ad's reach null; a stale session propagates."""
        if not self.session.details_doc_id:
            raise AdLibraryError(
                "No details_doc_id in this session (reach lookup unavailable). "
                "Re-run bootstrap_session() to harvest it."
            )
        targets = [a for a in ads if a.id and a.page_id]
        if not targets:
            return ads

        def work(ad: Ad) -> None:
            try:
                r = self.get_ad_reach(
                    ad.id, ad.page_id, country=country, http=self._thread_http()
                )
                ad.eu_total_reach = r.eu_total_reach
                ad.reach = r.to_dict()
            except TransientError:
                pass  # leave this ad's reach null; don't fail the whole batch

        workers = min(max_workers, len(targets))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(work, targets))
        else:
            for ad in targets:
                work(ad)
        return ads

    # -- public API: scan until reach drops off -----------------------------

    def scan_page_by_keyword(
        self,
        query: str,
        country: str | Iterable[str] = "ALL",
        reach_threshold: int = 0,
        patience: int = 3,
        cursor: str | None = None,
        streak: int = 0,
        active_status: str = "all",
        ad_type: str = "all",
    ) -> ScanPage:
        """One page of a scan. Pass back `next_cursor` + `streak` until `done`."""
        return self._scan_one(
            self._keyword_vars(query, country, active_status, ad_type),
            reach_threshold, patience, cursor, streak, _reach_country(country),
        )

    def scan_page_by_page_id(
        self,
        page_id: str,
        country: str | Iterable[str] = "ALL",
        reach_threshold: int = 0,
        patience: int = 3,
        cursor: str | None = None,
        streak: int = 0,
        active_status: str = "all",
        ad_type: str = "all",
    ) -> ScanPage:
        return self._scan_one(
            self._page_vars(page_id, country, active_status, ad_type),
            reach_threshold, patience, cursor, streak, _reach_country(country),
        )

    def scan_by_keyword(
        self,
        query: str,
        country: str | Iterable[str] = "ALL",
        reach_threshold: int = 0,
        patience: int = 3,
        limit: int | None = 200,
        active_status: str = "all",
        ad_type: str = "all",
    ) -> ScanResult:
        """Do-it-all convenience: loop the paginated scan until done (or `limit` cap)."""
        return self._scan_all(
            self._keyword_vars(query, country, active_status, ad_type),
            reach_threshold, patience, limit, _reach_country(country),
        )

    def scan_by_page_id(
        self,
        page_id: str,
        country: str | Iterable[str] = "ALL",
        reach_threshold: int = 0,
        patience: int = 3,
        limit: int | None = 200,
        active_status: str = "all",
        ad_type: str = "all",
    ) -> ScanResult:
        return self._scan_all(
            self._page_vars(page_id, country, active_status, ad_type),
            reach_threshold, patience, limit, _reach_country(country),
        )

    def _scan_one(
        self, base_variables: dict, reach_threshold: int, patience: int,
        cursor: str | None, streak_in: int, reach_country: str,
    ) -> ScanPage:
        """Fetch one impressions-desc page, enrich reach, advance the streak."""
        base = {**base_variables, "sortData": IMPRESSIONS_DESC_SORT}
        page = self._fetch_one(base, cursor)
        if not page.ads:
            return ScanPage([], True, "exhausted", streak_in, None)
        self.enrich_with_reach(page.ads, country=reach_country)
        flags = [(a.eu_total_reach or 0) < reach_threshold for a in page.ads]
        stop_k, streak_out = _scan_stop(flags, patience, streak_in)
        if stop_k is not None:
            return ScanPage(page.ads[:stop_k], True, "streak", streak_out, None)
        if not page.has_next_page or not page.end_cursor:
            return ScanPage(page.ads, True, "exhausted", streak_out, None)
        return ScanPage(page.ads, False, None, streak_out, page.end_cursor)

    def _scan_all(
        self, base_variables: dict, reach_threshold: int, patience: int,
        limit: int | None, reach_country: str,
    ) -> ScanResult:
        ads: list[Ad] = []
        cursor: str | None = None
        streak = 0
        reason = "exhausted"
        while True:
            sp = self._scan_one(base_variables, reach_threshold, patience, cursor, streak, reach_country)
            ads.extend(sp.ads)
            if sp.done:
                reason = sp.stop_reason or "exhausted"
                break
            if limit is not None and len(ads) >= limit:
                ads = ads[:limit]
                reason = "limit"
                break
            cursor, streak = sp.next_cursor, sp.streak
            time.sleep(self.request_delay)
        return ScanResult(reach_threshold, patience, len(ads), reason, ads)

    # -- internals ----------------------------------------------------------

    def _keyword_vars(self, query, country, active_status, ad_type) -> dict:
        return self._base_variables(
            country=country, active_status=active_status, ad_type=ad_type,
            search_type="keyword_unordered", query=query, page_id=None,
        )

    def _page_vars(self, page_id, country, active_status, ad_type) -> dict:
        return self._base_variables(
            country=country, active_status=active_status, ad_type=ad_type,
            search_type="page", query=None, page_id=str(page_id),
        )

    def _base_variables(
        self,
        *,
        country: str | Iterable[str],
        active_status: str,
        ad_type: str,
        search_type: str,
        query: str | None,
        page_id: str | None,
    ) -> dict:
        countries = [country] if isinstance(country, str) else list(country)
        variables = {
            "activeStatus": active_status,
            "adType": ad_type.upper(),
            "bylines": [],
            "collationToken": None,
            "contentLanguages": [],
            "countries": countries,
            "cursor": None,
            "excludedIDs": None,
            "first": self.page_size,
            "isTargetedCountry": False,
            "location": None,
            "mediaType": "all",
            "multiCountryFilterMode": None,
            "pageIDs": [],
            "potentialReachInput": None,
            "publisherPlatforms": [],
            "queryString": query,
            "regions": None,
            "searchType": search_type,
            "sessionID": self.session_id,
            "sortData": None,
            "source": None,
            "startDate": None,
            "viewAllPageID": page_id or "0",
        }
        if self.session.variables_version:
            variables["v"] = self.session.variables_version
        return variables

    def _fetch_one(self, base_variables: dict, cursor: str | None) -> AdPage:
        """Fetch a single page. Meta fixes the page size at ~10 regardless of `first`."""
        variables = {**base_variables, "cursor": cursor, "first": self.page_size}
        response = self._post(variables)
        ads = normalize_response(response)
        end_cursor, has_next = get_page_info(response)
        return AdPage(ads=ads, end_cursor=end_cursor, has_next_page=has_next)

    def _collect(
        self, base_variables: dict, limit: int | None, cursor: str | None
    ) -> AdPage:
        """Fetch whole pages from `cursor` until >= limit ads (or no more pages).
        Returns whole pages only (no mid-page truncation) so `next_cursor` is gap-free.
        limit=None collects everything."""
        ads: list[Ad] = []
        last = AdPage([], cursor, False)
        cur = cursor
        while True:
            page = self._fetch_one(base_variables, cur)
            ads.extend(page.ads)
            last = page
            cur = page.end_cursor
            if not page.has_next_page or not page.end_cursor or not page.ads:
                break
            if limit is not None and len(ads) >= limit:
                break
            time.sleep(self.request_delay)
        return AdPage(ads=ads, end_cursor=last.end_cursor, has_next_page=last.has_next_page)

    def _count(self, base_variables: dict, max_pages: int | None) -> tuple[int, bool]:
        """Walk every page counting ads. Returns (total, complete)."""
        total = 0
        cur: str | None = None
        pages = 0
        while True:
            page = self._fetch_one(base_variables, cur)
            total += len(page.ads)
            pages += 1
            cur = page.end_cursor
            if not page.has_next_page or not page.end_cursor or not page.ads:
                return total, True
            if max_pages is not None and pages >= max_pages:
                return total, False
            time.sleep(self.request_delay)

    def _post(self, variables: dict) -> dict:
        return self._graphql(self.session.doc_id, SEARCH_FRIENDLY_NAME, variables)

    def _graphql(self, doc_id: str, friendly_name: str, variables: dict, http=None) -> dict:
        """POST a persisted query, replaying the captured body and overriding only
        doc_id/friendly_name/variables. Retries transient server errors. `http` lets a
        worker thread pass its own curl_cffi session (sessions aren't thread-shared)."""
        http = http or self._http
        body = self._build_body(doc_id, friendly_name, variables)
        headers = {
            "user-agent": self.session.user_agent,
            "content-type": "application/x-www-form-urlencoded",
            "accept": "*/*",
            "origin": "https://www.facebook.com",
            "referer": "https://www.facebook.com/ads/library/",
            "x-fb-friendly-name": friendly_name,
            "x-fb-lsd": self.session.lsd,
        }
        last_exc: TransientError | None = None
        for attempt in range(self.retries):
            resp = http.post(
                GRAPHQL_URL, data=body, headers=headers,
                cookies=self.session.cookies, timeout=self.timeout,
            )
            if resp.status_code != 200:
                log.warning("%s HTTP %s", friendly_name, resp.status_code)
            try:
                return self._parse(resp.text)
            except TransientError as exc:
                last_exc = exc
                log.warning(
                    "%s transient error (attempt %d/%d): %s",
                    friendly_name, attempt + 1, self.retries, exc,
                )
                if attempt < self.retries - 1:
                    time.sleep(self.retry_backoff * (attempt + 1))
        raise last_exc  # exhausted retries

    def _build_body(self, doc_id: str, friendly_name: str, variables: dict) -> dict:
        # Replay the live-captured request body verbatim; reconstructing a minimal
        # body gets rejected (FB error 1357054). We override only the per-call fields.
        if self.session.request_template:
            body = dict(self.session.request_template)
        else:
            jazoest = self.session.jazoest or (
                _jazoest(self.session.fb_dtsg) if self.session.fb_dtsg else ""
            )
            body = dict(self.session.extra_params)
            body.update({"lsd": self.session.lsd, "jazoest": jazoest})
            if self.session.fb_dtsg:
                body["fb_dtsg"] = self.session.fb_dtsg
        body.update(
            {
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": friendly_name,
                "server_timestamps": "true",
                "doc_id": doc_id,
                "variables": json.dumps(variables, separators=(",", ":")),
            }
        )
        return body

    def _parse(self, text: str) -> dict:
        stripped = _FORJSON_PREFIX.sub("", text, count=1).strip()
        if not stripped:
            # Could be a transient blip or a stale doc_id; retry first.
            raise TransientError("Empty response from GraphQL endpoint.")
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            # A login/redirect HTML page instead of JSON means the session died.
            raise SessionExpiredError(
                "Non-JSON response (likely a login/redirect page). Re-run "
                "bootstrap_session()."
            ) from exc

        if isinstance(data, dict) and data.get("errors") and "data" not in data:
            err = data["errors"][0]
            msg = err.get("message", "unknown error")
            log.warning("GraphQL error (code=%s): %s", err.get("code"), msg)
            if _is_transient(msg):
                raise TransientError(f"Transient GraphQL error: {msg}")
            raise StaleDocIdError(
                f"GraphQL error: {msg}. The doc_id or tokens may have rotated — "
                "re-run bootstrap_session()."
            )
        return data
