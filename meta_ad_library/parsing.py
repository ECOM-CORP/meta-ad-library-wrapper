"""Normalize the Ad Library GraphQL response into clean `Ad` objects.

Written against real captured samples (see ./samples/), parsed defensively: every
lookup tolerates a missing/renamed key, because the response shape is undocumented
and version-specific.

Response shape (confirmed from samples):
    data.ad_library_main.search_results_connection
        .edges[].node.collated_results[]   -> one dict per ad creative
        .page_info{end_cursor, has_next_page}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from .models import Ad, AdReach

AD_SNAPSHOT_URL = "https://www.facebook.com/ads/library/?id={ad_archive_id}"


def get_connection(response: dict) -> dict | None:
    """Return the search_results_connection dict, or None if not present."""
    main = (response.get("data") or {}).get("ad_library_main") or {}
    conn = main.get("search_results_connection")
    return conn if isinstance(conn, dict) else None


def get_page_info(response: dict) -> tuple[str | None, bool]:
    """Return (end_cursor, has_next_page)."""
    conn = get_connection(response) or {}
    info = conn.get("page_info") or {}
    return info.get("end_cursor"), bool(info.get("has_next_page"))


def iter_ad_nodes(response: dict) -> Iterator[dict]:
    """Yield each ad dict (collated_results entry) from a search response."""
    conn = get_connection(response)
    if not conn:
        return
    for edge in conn.get("edges") or []:
        node = (edge or {}).get("node") or {}
        for result in node.get("collated_results") or []:
            if isinstance(result, dict):
                yield result


def _epoch_to_date(value: Any) -> str | None:
    """Convert a unix-seconds timestamp to an ISO date (YYYY-MM-DD)."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    """Pull a text string out of the various shapes Meta uses (str, {'text': ...})."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text.strip() or None
    return None


def _creative_bodies(snapshot: dict) -> list[str]:
    """Collect all distinct creative body texts (main body, carousel cards, extras)."""
    bodies: list[str] = []

    def add(value: Any) -> None:
        text = _as_text(value)
        if text and text not in bodies:
            bodies.append(text)

    add(snapshot.get("body"))
    for card in snapshot.get("cards") or []:
        if isinstance(card, dict):
            add(card.get("body"))
    for extra in snapshot.get("extra_texts") or []:
        add(extra)
    return bodies


def _impressions_text(impressions: Any) -> str | None:
    """Pull the human impressions range out of impressions_with_index, if present.
    Usually {'impressions_text': None, 'impressions_index': -1} for commercial ads."""
    if isinstance(impressions, dict):
        text = impressions.get("impressions_text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def normalize_ad(node: dict) -> Ad:
    """Map one collated_results entry to an `Ad`. Never assumes a key exists."""
    snapshot = node.get("snapshot") or {}
    ad_archive_id = node.get("ad_archive_id") or node.get("ad_id")
    platforms = node.get("publisher_platform") or []
    if isinstance(platforms, str):
        platforms = [platforms]

    return Ad(
        id=ad_archive_id,
        page_id=node.get("page_id") or snapshot.get("page_id"),
        page_name=node.get("page_name") or snapshot.get("page_name"),
        creative_bodies=_creative_bodies(snapshot),
        snapshot_url=(
            AD_SNAPSHOT_URL.format(ad_archive_id=ad_archive_id)
            if ad_archive_id
            else None
        ),
        start_date=_epoch_to_date(node.get("start_date")),
        end_date=_epoch_to_date(node.get("end_date")),
        publisher_platforms=list(platforms),
        is_active=node.get("is_active"),
        title=_as_text(snapshot.get("title")),
        caption=_as_text(snapshot.get("caption")),
        link_url=snapshot.get("link_url") or None,
        link_description=_as_text(snapshot.get("link_description")),
        cta_text=_as_text(snapshot.get("cta_text")),
        display_format=snapshot.get("display_format"),
        reach_estimate=node.get("reach_estimate"),
        impressions=_impressions_text(node.get("impressions_with_index")),
        spend=node.get("spend"),
        currency=node.get("currency"),
        raw=node,
    )


def normalize_response(response: dict) -> list[Ad]:
    return [normalize_ad(node) for node in iter_ad_nodes(response)]


def normalize_ad_details(response: dict, ad_archive_id: str | None = None) -> AdReach:
    """Pull EU reach/targeting out of an AdLibraryV3AdDetailsQuery response.
    Path: data.ad_library_main.ad_details.transparency_by_location.eu_transparency."""
    details = ((response.get("data") or {}).get("ad_library_main") or {}).get(
        "ad_details"
    ) or {}
    tbl = details.get("transparency_by_location") or {}
    eu = tbl.get("eu_transparency") or {}
    age = eu.get("age_audience") or {}
    countries = [
        loc.get("name")
        for loc in (eu.get("location_audience") or [])
        if isinstance(loc, dict) and loc.get("name")
    ]
    payer = beneficiary = None
    pb = (details.get("aaa_info") or {}).get("payer_beneficiary_data") or []
    if pb and isinstance(pb[0], dict):
        payer = pb[0].get("payer")
        beneficiary = pb[0].get("beneficiary")

    return AdReach(
        ad_archive_id=ad_archive_id,
        targets_eu=bool(eu.get("targets_eu")),
        eu_total_reach=eu.get("eu_total_reach"),
        countries=countries,
        gender=eu.get("gender_audience"),
        age_min=age.get("min"),
        age_max=age.get("max"),
        payer=payer,
        beneficiary=beneficiary,
        breakdown=eu.get("age_country_gender_reach_breakdown") or [],
        raw=details,
    )
