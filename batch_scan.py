"""Batch reach-scan: for each country/term, scan until reach drops below threshold,
write winners (reach >= threshold) to scan_results.json and a tab-separated .txt."""

import io
import json
import sys
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from meta_ad_library import AdLibraryClient, SessionData

THRESHOLD = 10_000
PATIENCE = 3
LIMIT = 200
OUT_TXT = "scan_results.txt"
OUT_JSON = "scan_results.json"

JOBS = {
    "BG": ["артериална плака", "артерии", "запушени артерии", "eNOS"],
    "IT": ["placca arteriosa", "arterie", "arterie ostruite", "eNOS"],
    "CZ": ["arteriální plát", "tepny", "ucpané tepny", "eNOS"],
    "ES": ["placa arterial", "arterias", "arterias obstruidas", "eNOS"],
    "HU": ["artériás plakk", "artériák", "elzáródott artériák", "eNOS"],
}


def clean(s):
    return (s or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def main():
    client = AdLibraryClient(SessionData.load("session_cache.json"), request_delay=0.5)
    generated = f"{datetime.now():%Y-%m-%d %H:%M}"
    lines = [f"Reach scan — threshold {THRESHOLD}, patience {PATIENCE}, generated {generated}"]
    results = []

    for country, terms in JOBS.items():
        lines += ["", "=" * 70, f"COUNTRY: {country}", "=" * 70]
        for term in terms:
            try:
                res = client.scan_by_keyword(
                    term, country, reach_threshold=THRESHOLD,
                    patience=PATIENCE, limit=LIMIT,
                )
                winners = [a for a in res.ads if (a.eu_total_reach or 0) >= THRESHOLD]
                results.append({
                    "country": country, "term": term, "scanned": res.scanned_count,
                    "stop_reason": res.stop_reason, "winner_count": len(winners),
                    "ads": [{"page_name": a.page_name, "title": a.title,
                             "link_url": a.link_url, "reach": a.eu_total_reach}
                            for a in winners],
                })
                lines.append("")
                lines.append(
                    f"--- {term} ---  (scanned {res.scanned_count}, "
                    f"stop={res.stop_reason}, winners>= {THRESHOLD}: {len(winners)})"
                )
                lines.append("page_name\ttitle\tlink_url\treach")
                for a in winners:
                    lines.append(
                        f"{clean(a.page_name)}\t{clean(a.title)}\t"
                        f"{a.link_url or ''}\t{a.eu_total_reach}"
                    )
                print(f"{country} / {term}: {len(winners)} winners "
                      f"(scanned {res.scanned_count}, stop={res.stop_reason})")
            except Exception as exc:  # noqa: BLE001
                results.append({"country": country, "term": term,
                                "error": f"{type(exc).__name__}: {exc}", "ads": []})
                lines.append("")
                lines.append(f"--- {term} ---  ERROR: {type(exc).__name__}: {exc}")
                print(f"{country} / {term}: ERROR {type(exc).__name__}: {exc}")

    with open(OUT_TXT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    payload = {"threshold": THRESHOLD, "patience": PATIENCE, "generated": generated,
               "group_count": len(results),
               "total_ads": sum(len(r["ads"]) for r in results), "results": results}
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {OUT_TXT} and {OUT_JSON}")


if __name__ == "__main__":
    main()
