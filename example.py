"""End-to-end example: bootstrap a session, then pull ads.

Run:
    .venv\\Scripts\\python example.py
"""

import io
import sys

# Ad copy is often non-ASCII (Cyrillic, etc.); force UTF-8 stdout on Windows consoles.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from meta_ad_library import AdLibraryClient, SessionData, bootstrap_session

CACHE = "session_cache.json"


def get_session() -> SessionData:
    try:
        session = SessionData.load(CACHE)
        print(f"Loaded cached session (age {session.age_seconds/60:.0f} min).")
        return session
    except FileNotFoundError:
        print("No cached session — opening a browser to bootstrap one...")
        return bootstrap_session(country="BG", save_to=CACHE)


def main() -> None:
    session = get_session()
    client = AdLibraryClient(session, request_delay=1.0)

    # Keyword search (the page "Кажи Чао на Ишиаса" surfaces under biosila.bg).
    ads = client.search_by_keyword("biosila.bg", country="BG", max_results=15)
    print(f"\nFound {len(ads)} ads:\n")
    for ad in ads:
        body = (ad.creative_bodies[0] if ad.creative_bodies else ad.title) or ""
        print(f"[{ad.start_date}] {ad.page_name} ({ad.display_format})")
        print(f"    {body[:100].strip()}")
        print(f"    {ad.snapshot_url}")


if __name__ == "__main__":
    main()
