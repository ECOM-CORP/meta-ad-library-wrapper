"""Tests for the MCP layer's `_shape` response shaping (no network).

`_shape` always strips `raw`, and by default projects each ad down to
DEFAULT_AD_FIELDS; verbose=True keeps every field except raw."""

from meta_ad_library.mcp_server import DEFAULT_AD_FIELDS, _shape


def _sample_page() -> dict:
    return {
        "count": 1,
        "next_cursor": "abc",
        "ads": [
            {
                "id": "1",
                "page_id": "p1",
                "page_name": "Acme",
                "creative_bodies": ["buy now"],
                "snapshot_url": "https://fb/1",
                "link_url": "https://example.com",
                "title": "Great deal",
                "eu_total_reach": 1234,
                # fields that should be dropped in the lean default:
                "caption": "acme.com",
                "link_description": "desc",
                "cta_text": "Shop now",
                "publisher_platforms": ["FACEBOOK"],
                "reach": {"eu_total_reach": 1234, "raw": {"big": "blob"}},
                "raw": {"snapshot": {"snapshot": {"body": "huge"}}},
            }
        ],
    }


def test_default_projects_ad_to_lean_fields():
    out = _shape(_sample_page())
    ad = out["ads"][0]
    assert set(ad.keys()) == set(DEFAULT_AD_FIELDS)
    assert ad["page_name"] == "Acme"
    assert ad["creative_bodies"] == ["buy now"]
    assert ad["eu_total_reach"] == 1234
    # Verbose-only fields are gone by default.
    assert "caption" not in ad and "reach" not in ad and "raw" not in ad
    # Page-level keys survive.
    assert out["count"] == 1 and out["next_cursor"] == "abc"


def test_verbose_keeps_all_fields_except_raw():
    out = _shape(_sample_page(), verbose=True)
    ad = out["ads"][0]
    assert "raw" not in ad
    assert "raw" not in ad["reach"]
    # Everything else is retained.
    assert ad["caption"] == "acme.com"
    assert ad["cta_text"] == "Shop now"
    assert ad["reach"]["eu_total_reach"] == 1234


def test_shapes_full_scan_result_shape():
    # all_pages=true returns a ScanResult shape: {threshold, patience, scanned_count,
    # stop_reason, ads}. _shape must project ads and preserve the page-level keys.
    result = {
        "threshold": 65000,
        "patience": 4,
        "scanned_count": 2,
        "stop_reason": "limit",
        "ads": [
            {"id": "1", "page_name": "A", "eu_total_reach": 90000, "caption": "x",
             "raw": {"big": "blob"}},
            {"id": "2", "page_name": "B", "eu_total_reach": 70000, "raw": {"big": "blob"}},
        ],
    }
    out = _shape(result)
    assert out["scanned_count"] == 2 and out["stop_reason"] == "limit"
    assert out["threshold"] == 65000
    assert all("raw" not in ad and "caption" not in ad for ad in out["ads"])
    assert {ad["id"] for ad in out["ads"]} == {"1", "2"}


def test_strips_raw_from_get_ad_reach_shape():
    # get_ad_reach returns an AdReach-shaped dict (no `ads`); only raw is dropped.
    result = {"ad_archive_id": "1", "eu_total_reach": 99, "raw": {"details": "blob"}}
    out = _shape(result)
    assert "raw" not in out
    assert out["eu_total_reach"] == 99


def test_leaves_error_dict_untouched():
    result = {"error": "Session invalid: re-run bootstrap_session()."}
    assert _shape(result) == {"error": "Session invalid: re-run bootstrap_session()."}


def test_tolerates_missing_or_partial_keys():
    # No `ads`, no `raw` — nothing to do, no crash.
    assert _shape({"count": 0}) == {"count": 0}
    # Ad missing some default fields: project to the present subset, drop raw.
    out = _shape({"ads": [{"id": "9", "page_name": "X", "raw": {"z": 1}}]})
    assert out["ads"][0] == {"id": "9", "page_name": "X"}
    # Non-dict input passes through.
    assert _shape([1, 2]) == [1, 2]
