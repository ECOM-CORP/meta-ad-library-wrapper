"""Dev utility: capture real Ad Library GraphQL request+response samples.

Opens the Ad Library in a headed browser, lets it run a real search, scrolls to
trigger a pagination request, and dumps the captured GraphQL request bodies and raw
JSON responses to ./samples/ (with fb_dtsg / lsd / jazoest redacted).

We build the Phase-4 `variables` builder and response parser against THESE files —
not from memory.

Usage:
    .venv\\Scripts\\python capture_sample.py page <page_id> [country]
    .venv\\Scripts\\python capture_sample.py keyword <query> [country]
    .venv\\Scripts\\python capture_sample.py            # defaults to the sample page below
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

from playwright.sync_api import sync_playwright

from meta_ad_library.session import (
    GRAPHQL_PATH,
    _page_search_url,
    _search_url,
    _try_dismiss_consent,
)

# Default capture target: page "Кажи Чао на Ишиаса"
DEFAULT_MODE = "page"
DEFAULT_TARGET = "61587568200772"
DEFAULT_COUNTRY = "BG"

SAMPLES_DIR = Path("samples")
PROFILE_DIR = ".pw-profile"
REDACT_KEYS = {"fb_dtsg", "lsd", "jazoest"}
FORJSON_PREFIX = re.compile(r"^\s*for\s*\(;;\);")


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:60] or "graphql"


def _parse_body(post_data: str) -> dict:
    qs = parse_qs(post_data, keep_blank_values=True)
    flat = {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}
    redacted = {
        k: ("<redacted>" if k in REDACT_KEYS else v) for k, v in flat.items()
    }
    # `variables` is itself a JSON string — pull it out parsed so it's readable.
    variables = None
    if isinstance(flat.get("variables"), str):
        try:
            variables = json.loads(flat["variables"])
        except json.JSONDecodeError:
            variables = flat["variables"]
    return {
        "doc_id": flat.get("doc_id"),
        "friendly_name": flat.get("fb_api_req_friendly_name"),
        "variables": variables,
        "all_params_redacted": redacted,
    }


def main(mode: str, target: str, country: str) -> None:
    SAMPLES_DIR.mkdir(exist_ok=True)
    if mode == "page":
        url = _page_search_url(country, target)
    else:
        url = _search_url(country, target)
    pairs: list[tuple[dict, object]] = []  # (request_info, response_obj)

    def on_response(response) -> None:
        req = response.request
        if GRAPHQL_PATH not in req.url or req.method != "POST":
            return
        body = req.post_data
        if not body or "doc_id=" not in body:
            return
        pairs.append((_parse_body(body), response))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(PROFILE_DIR).resolve()),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ctx.on("response", on_response)

        print(f"Navigating ({mode}): {url}")
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _try_dismiss_consent(page)
        page.wait_for_timeout(2500)
        _try_dismiss_consent(page)
        try:
            page.screenshot(path=str(SAMPLES_DIR / "_screenshot_initial.png"))
        except Exception:
            pass

        # Let the initial search settle, then scroll to provoke a pagination request.
        print("  Browser is open — if a cookie/consent dialog is visible, clear it now.")
        page.wait_for_timeout(10000)
        for i in range(6):
            page.mouse.wheel(0, 6000)
            page.wait_for_timeout(3500)
            print(f"  scrolled {i + 1}/6, captured so far: {len(pairs)} graphql POSTs")
        try:
            page.screenshot(path=str(SAMPLES_DIR / "_screenshot_final.png"))
        except Exception:
            pass

        # Read response bodies while the context is still alive.
        saved = 0
        seen_friendly: dict[str, int] = {}
        manifest = []
        for info, response in pairs:
            friendly = info.get("friendly_name") or "graphql"
            seen_friendly[friendly] = seen_friendly.get(friendly, 0) + 1
            idx = saved + 1
            stem = f"{idx:02d}_{_safe_name(friendly)}"
            try:
                raw = response.text()
            except Exception as exc:  # noqa: BLE001
                raw = ""
                print(f"  ! could not read body for {friendly}: {exc}")
            stripped = FORJSON_PREFIX.sub("", raw, count=1).strip()
            parsed = None
            if stripped:
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None

            (SAMPLES_DIR / f"{stem}_request.json").write_text(
                json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            if parsed is not None:
                (SAMPLES_DIR / f"{stem}_response.json").write_text(
                    json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            elif raw:
                (SAMPLES_DIR / f"{stem}_response_raw.txt").write_text(
                    raw, encoding="utf-8"
                )
            manifest.append(
                {
                    "file_stem": stem,
                    "friendly_name": friendly,
                    "doc_id": info.get("doc_id"),
                    "response_parsed_ok": parsed is not None,
                    "response_bytes": len(raw),
                }
            )
            saved += 1

        (SAMPLES_DIR / "00_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        ctx.close()

    print(f"\nDone. Saved {saved} request/response pairs to {SAMPLES_DIR.resolve()}")
    print("Friendly names seen:", dict(seen_friendly))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        mode, target, country = DEFAULT_MODE, DEFAULT_TARGET, DEFAULT_COUNTRY
    else:
        mode = args[0]
        target = args[1] if len(args) > 1 else DEFAULT_TARGET
        country = args[2] if len(args) > 2 else DEFAULT_COUNTRY
    if mode not in ("page", "keyword"):
        print("Usage: capture_sample.py page <page_id> [country] | keyword <query> [country]")
        sys.exit(1)
    main(mode, target, country)
