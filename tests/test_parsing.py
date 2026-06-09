"""Offline parser tests against the committed real-response samples (no network)."""

import glob
import json
from pathlib import Path

import pytest

from meta_ad_library.parsing import get_page_info, normalize_ad, normalize_response

SAMPLES = sorted(
    glob.glob(
        str(Path(__file__).resolve().parent.parent
            / "samples" / "*_AdLibrarySearchPaginationQuery_response.json")
    )
)


@pytest.fixture
def first_response() -> dict:
    return json.loads(Path(SAMPLES[0]).read_text(encoding="utf-8"))


def test_samples_exist():
    assert SAMPLES, "no sample response files found"


def test_parses_ads(first_response):
    ads = normalize_response(first_response)
    assert len(ads) == 10
    ad = ads[0]
    assert ad.id and ad.id.isdigit()
    assert ad.page_id and ad.page_name
    assert ad.snapshot_url == f"https://www.facebook.com/ads/library/?id={ad.id}"
    assert ad.start_date and len(ad.start_date) == 10  # YYYY-MM-DD


def test_page_info(first_response):
    cursor, has_next = get_page_info(first_response)
    assert has_next is True
    assert cursor


def test_video_ad_keeps_link_copy(first_response):
    # Video ads have an empty body but real copy in title/link_description.
    ads = normalize_response(first_response)
    videos = [a for a in ads if a.display_format == "VIDEO"]
    assert videos
    assert any(a.title or a.link_description for a in videos)


def test_all_samples_parse():
    total = 0
    for path in SAMPLES:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        ads = normalize_response(data)
        total += len(ads)
        assert all(a.id for a in ads)
    assert total >= 90  # ~10 pages x 10 ads


def test_defensive_on_garbage():
    assert normalize_response({}) == []
    assert normalize_response({"data": {}}) == []
    assert normalize_ad({}).id is None


def test_ad_details_reach():
    from meta_ad_library.parsing import normalize_ad_details

    path = (Path(__file__).resolve().parent.parent / "samples" / "details"
            / "01_AdLibraryV3AdDetailsQuery.json")
    if not path.exists():
        pytest.skip("ad-details sample not captured")
    data = json.loads(path.read_text(encoding="utf-8"))
    reach = normalize_ad_details(data, ad_archive_id="924497020441203")
    assert reach.targets_eu is True
    assert reach.eu_total_reach == 611904
    assert reach.countries == ["Bulgaria"]
    assert reach.age_min == 18 and reach.age_max == 65
    assert reach.payer == "Troneli"


def test_ad_details_defensive():
    from meta_ad_library.parsing import normalize_ad_details

    reach = normalize_ad_details({})
    assert reach.eu_total_reach is None
    assert reach.targets_eu is False


def test_scan_stop_logic():
    from meta_ad_library.client import _scan_stop

    # The real "седалищен нерв" reach sequence under impressions-desc sort.
    reach = [611904, 253393, 394599, 168113, 84578, 130403, 87117, 85390, 75946,
             69762, 65250, 64634, 59402, 55052, 43240, 30994, 72313, 82452]

    def below(threshold):
        return [(r or 0) < threshold for r in reach]

    # _scan_stop returns (ads_to_keep_or_None, streak_out)
    # threshold 70k, patience 3 -> stops after the 69762/65250/64634 run (12 ads)
    assert _scan_stop(below(70_000), 3)[0] == 12
    # threshold 100k, patience 3 -> stops within the first 9
    assert _scan_stop(below(100_000), 3)[0] == 9
    # consecutive matters: an isolated dip then recovery shouldn't trigger patience 2
    assert _scan_stop([True, False, True, True], 2)[0] == 4
    # never triggers -> None, and streak carries out
    assert _scan_stop([False, False, True, False, True], 3) == (None, 1)
    # all below -> stop at patience
    assert _scan_stop([True, True, True, True], 3)[0] == 3
    # streak_in carries across pages: 2 in + 1 below = 3 -> stop at first ad
    assert _scan_stop([True, False], 3, streak_in=2) == (1, 3)
