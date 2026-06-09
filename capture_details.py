"""Capture the Ad Library 'Ad Details' GraphQL query that carries EU reach.

Opens a single ad (?id=<ad_archive_id>), clicks the details/summary control to trigger
the detail query, and dumps every GraphQL request/response to samples/details/ so we can
build the reach lookup against the real shape.

Usage: .venv\\Scripts\\python capture_details.py [ad_archive_id] [country]
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from playwright.sync_api import sync_playwright
from meta_ad_library.session import GRAPHQL_PATH, _try_dismiss_consent

OUT = Path("samples/details")
DEFAULT_ID = "924497020441203"
DEFAULT_COUNTRY = "BG"
FORJSON = re.compile(r"^\s*for\s*\(;;\);")


def main(ad_id: str, country: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pairs = []

    def on_response(response):
        req = response.request
        if GRAPHQL_PATH in req.url and req.method == "POST" and req.post_data:
            qs = parse_qs(req.post_data, keep_blank_values=True)
            name = qs.get("fb_api_req_friendly_name", [""])[0]
            variables = qs.get("variables", [""])[0]
            doc_id = qs.get("doc_id", [""])[0]
            pairs.append((name, variables, doc_id, response))

    url = f"https://www.facebook.com/ads/library/?id={ad_id}"
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(".pw-profile").resolve()),
            headless=False, viewport={"width": 1360, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ctx.on("response", on_response)
        print("navigating:", url)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        _try_dismiss_consent(page)
        page.wait_for_timeout(4000)  # let the "Link to ad" modal render
        try:
            page.screenshot(path=str(OUT / "_page.png"))
        except Exception:
            pass

        # Click "See ad details" to fire the detail query (carries EU reach).
        # There are two cards (background + the "Link to ad" dialog on top); target the
        # dialog's button, and force-click to bypass the modal overlay if needed.
        clicked = False
        candidates = [
            lambda: page.get_by_role("dialog").get_by_text("See ad details", exact=False).first,
            lambda: page.get_by_text("See ad details", exact=False).last,
            lambda: page.get_by_text("See ad details", exact=False).first,
        ]
        for make in candidates:
            try:
                btn = make()
                btn.wait_for(state="visible", timeout=8000)
                btn.click(force=True, timeout=5000)
                clicked = True
                print("clicked 'See ad details'")
                break
            except Exception as exc:
                print("click attempt failed:", str(exc)[:80])
        page.wait_for_timeout(7000)
        try:
            page.screenshot(path=str(OUT / "_modal.png"))
        except Exception:
            pass
        print("details control clicked:", clicked)

        saved = 0
        manifest = []
        for name, variables, doc_id, response in pairs:
            try:
                raw = response.text()
            except Exception:
                raw = ""
            parsed = None
            stripped = FORJSON.sub("", raw, 1).strip()
            if stripped:
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
            stem = f"{saved:02d}_{re.sub(r'[^A-Za-z0-9]', '_', name)[:50]}"
            has_reach = any(k in raw for k in ['"reach"', "eu_total_reach", "aaa_info",
                                               "eu_political", "audience", "demographic"])
            if parsed is not None:
                (OUT / f"{stem}.json").write_text(
                    json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                vars_parsed = json.loads(variables) if variables else None
            except json.JSONDecodeError:
                vars_parsed = variables
            (OUT / f"{stem}_request.json").write_text(
                json.dumps({"doc_id": doc_id, "friendly_name": name,
                            "variables": vars_parsed}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            manifest.append({"stem": stem, "friendly_name": name, "doc_id": doc_id,
                             "bytes": len(raw), "reachish": has_reach})
            saved += 1

        (OUT / "_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        ctx.close()

    print(f"\nsaved {saved} graphql calls to {OUT}")
    for m in manifest:
        flag = " <-- REACH-ISH" if m["reachish"] else ""
        print(f"  {m['friendly_name']:45} {m['bytes']:>7}b{flag}")


if __name__ == "__main__":
    ad_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ID
    country = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_COUNTRY
    main(ad_id, country)
