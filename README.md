# meta-ad-library-unofficial-wrapper

A small Python wrapper that searches the **Meta (Facebook) Ad Library** by replaying
the public Ad Library website's own internal GraphQL requests — plus a thin FastAPI
service on port 8888.

> ## ⚠️ Unofficial — depends on undocumented internals
> This does **not** use Meta's official `ads_archive` Graph API (that one is
> identity-gated and returns no commercial ads outside the EU/UK). Instead it talks to
> the same internal endpoint (`https://www.facebook.com/api/graphql/`) that
> `facebook.com/ads/library` calls in your browser. That means:
> - **It can break without notice.** Meta rotates the `doc_id` (persisted-query id) and
>   the `lsd` token on every frontend ship.
> - We work around that by **bootstrapping a real browser session** (Playwright) to
>   harvest those values live, instead of hardcoding a cURL that rots in days.
> - Meta **TLS-fingerprints** the caller and soft-rejects plain `requests` with error
>   `1357054`. We use [`curl_cffi`](https://github.com/lexiforest/curl_cffi) to
>   impersonate Chrome's TLS handshake so the replayed request is accepted.
> - Responses are prefixed with `for (;;);` (anti-JSON-hijacking) and are stripped
>   before parsing.
> - Be polite: there's a configurable delay between paginated calls.

## How it works

1. **`session.py`** opens the Ad Library in a headed browser, lets the page fire its
   own search, and intercepts that GraphQL request to harvest the live `doc_id`, `lsd`,
   `jazoest`, `v`, cookies, user-agent, and the **full request body** (`__dyn`, `__csr`,
   … — replayed verbatim because a hand-built minimal body gets rejected).
2. **`client.py`** (`AdLibraryClient`) replays the search over `curl_cffi`, overriding
   only the `variables` (country, query/page, cursor), and paginates until
   `has_next_page` is false or `max_results` is reached.
3. **`parsing.py`** normalizes the deeply-nested response into clean `Ad` objects.
4. **`api.py`** exposes it as a FastAPI service on port 8888.

## Install (Windows, Python 3.11)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

## Bootstrap a session

The first run opens a visible browser. If an EU cookie-consent dialog appears, clear it
once — the persistent `.pw-profile` remembers it for next time.

```python
from meta_ad_library import bootstrap_session

session = bootstrap_session(country="BG")   # writes session_cache.json
print(session.doc_id, bool(session.lsd))
```

## Library usage

```python
from meta_ad_library import AdLibraryClient, SessionData

session = SessionData.load("session_cache.json")   # or bootstrap_session()
client = AdLibraryClient(session, request_delay=1.0)

# By keyword
ads = client.search_by_keyword("biosila.bg", country="BG", max_results=50)

# By page id (the INTERNAL page id — see note below)
ads = client.search_by_page_id("904870626052390", country="BG", max_results=50)

for ad in ads:
    print(ad.start_date, ad.page_name, ad.display_format)
    print("  ", (ad.creative_bodies or [ad.title or ""])[0][:100])
    print("  ", ad.snapshot_url)
```

`Ad` fields: `id`, `page_id`, `page_name`, `creative_bodies` (list), `title`,
`caption`, `link_url`, `link_description`, `cta_text`, `snapshot_url`, `start_date`,
`end_date`, `is_active`, `publisher_platforms`, `display_format`, `reach_estimate`,
`impressions`, `spend`, `currency`, and `raw` (the full original node).

For cursor pagination in the library, use `fetch_by_keyword` / `fetch_by_page_id`
(return an `AdPage` with `.ads`, `.end_cursor`, `.has_next_page`), or
`count_by_keyword` / `count_by_page_id` for an exact total.

## Web API (port 8888)

```powershell
.venv\Scripts\python run_api.py          # http://127.0.0.1:8888  (Swagger UI at /docs)
.venv\Scripts\python run_api.py 9000     # override the port
```

(Port 6000 is intentionally avoided — browsers block it as an "unsafe port".)

- `GET /docs` — interactive Swagger UI
- `GET /health`
- `GET /search/keyword?query=biosila.bg&country=ALL&active=all&limit=20&cursor=...`
- `GET /search/page?page_id=904870626052390&country=ALL&active=all&limit=20&cursor=...`
- `GET /count/keyword?query=...&country=ALL` — exact total (walks all pages)
- `GET /count/page?page_id=...&country=ALL`
- `GET /ad/reach?ad_archive_id=...&page_id=...&country=BG` — EU reach for one ad
- `GET /scan/keyword?query=...&country=BG&reach_threshold=70000` — scan until reach drops off
- `GET /scan/page?page_id=...&country=BG&reach_threshold=70000`
- `POST /session/bootstrap?country=BG` — re-harvest a session (opens a browser on the
  host). Search endpoints return **503** when tokens have rotated; re-bootstrap then.

### Query params

| param | default | meaning |
|-------|---------|---------|
| `country` | `ALL` | ISO code (`BG`, `DE`, …) or `ALL` for every country |
| `active` | `all` | `all` = active + inactive, `true` = active only, `false` = inactive only |
| `ad_type` | `all` | `all`, `political_and_issue_ads`, … |
| `limit` (alias `max_results`) | `30` | max ads; results come in whole **pages of 10** (Meta's fixed page size) |
| `cursor` | — | pass `next_cursor` from a previous response to get the next page |
| `with_reach` | `false` | enrich each ad with EU reach — **1 extra request per ad** (run in parallel) |

### Pagination & total count

Meta's response carries **no total count** — only a cursor and a "has more" flag. So
search responses look like:

```json
{ "count": 20, "has_next_page": true, "next_cursor": "AQHS...", "ads": [ ... ] }
```

To page: call again with `&cursor=<next_cursor>`. To get an **exact total**, use
`/count/keyword` or `/count/page` — these walk every page (can be slow for large
advertisers; cap with `&max_pages=N`, which sets `complete:false` if the cap is hit).

### Reach (EU transparency by location)

Reach is **not** in the search results — it lives in the **"See ad details" → EU ad
delivery** panel, a separate `AdLibraryV3AdDetailsQuery`. For any ad that targets the
EU, Meta exposes the total reach plus location/age/gender targeting. Use:

```
GET /ad/reach?ad_archive_id=<ad.id>&page_id=<ad.page_id>&country=BG
```

```json
{
  "ad_archive_id": "924497020441203", "targets_eu": true, "eu_total_reach": 611904,
  "countries": ["Bulgaria"], "gender": "All", "age_min": 18, "age_max": 65,
  "payer": "Troneli", "beneficiary": "Troneli", "breakdown": [ ... ]
}
```

`eu_total_reach` is `null` for ads that don't target the EU. In the library:
`client.get_ad_reach(ad.id, ad.page_id)` or `client.reach_for_ad(ad)`.

**Reach inside a search:** add `&with_reach=true` to `/search/keyword` or `/search/page`
and every returned ad gets `eu_total_reach` plus a nested `reach` object. Enrichment is
one extra request per ad, run in parallel (≈10 ads in ~1s). In the library, pass
`with_reach=True` to `fetch_by_keyword` / `fetch_by_page_id`, or call
`client.enrich_with_reach(ads)` on a list yourself.

> Reach needs the `details_doc_id`, which `bootstrap_session()` harvests best-effort by
> clicking "See ad details" during bootstrap. If reach returns 503 ("no details_doc_id"),
> re-run `bootstrap_session()`.

The search-list `Ad` also carries `reach_estimate` / `impressions` / `spend` /
`currency`, but Meta leaves those null for ordinary commercial ads.

### Scan until reach drops off

`/scan/keyword` and `/scan/page` find the high-reach "winners" and stop automatically.
They sort by **total impressions descending** (reach itself isn't sortable; impressions
is a proxy), enrich reach per page, and **stop after `patience` ads in a row below
`reach_threshold`** — consecutive, so it rides over the noise (reach trends down but
isn't monotonic). A `limit` (default 200, alias `max_results`) is a safety cap.

```
GET /scan/keyword?query=седалищен%20нерв&country=BG&reach_threshold=70000&patience=3
```

```json
{ "threshold": 70000, "patience": 3, "scanned_count": 12,
  "stop_reason": "streak",            // "streak" | "limit" | "exhausted"
  "ads": [ {... eu_total_reach ...}, ... ] }   // all scanned, in impressions order
```

Cost = the reach you'd fetch anyway (~1 request/ad, parallel per page of 10); a higher
threshold stops sooner and is cheaper. An ad with no EU reach counts as below threshold.
In the library: `client.scan_by_keyword(query, country, reach_threshold, patience=3)`.

## ⚠️ Page id gotcha

`search_by_page_id` needs the **internal** page id, which is **not** the number in a
`facebook.com/<id>` profile URL. For example the page *"Кажи Чао на Ишиаса"* lives at
`facebook.com/61587568200772`, but its ads use `page_id=904870626052390` — searching by
`61587568200772` returns 0 results. The reliable way to get the internal id: run a
**keyword search** first and read `ad.page_id` off any result.

## Development

- `capture_sample.py` — re-capture real search request/response samples into `samples/`
  (`python capture_sample.py keyword biosila.bg BG` or `... page <id> BG`). Used to
  rebuild the parser when Meta changes the search response shape.
- `capture_details.py` — re-capture the ad-details (reach) query into `samples/details/`
  (`python capture_details.py <ad_archive_id> BG`).
- `python -m pytest tests/` — offline parser tests against committed samples.

## MCP server (for AI clients)

The same wrapper is exposed as an **MCP server** (Streamable HTTP) so AI clients
(Claude Code, Claude Desktop, etc.) can search the Ad Library as tools. It wraps the
library in-process and shares `session_cache.json` with the REST API.

```powershell
.venv\Scripts\python run_mcp.py        # http://127.0.0.1:8765/mcp
```

Add it to Claude Code:

```powershell
claude mcp add --transport http meta-ad-library http://127.0.0.1:8765/mcp
```

Or in a Claude Desktop config:

```json
{ "mcpServers": { "meta-ad-library": {
  "type": "streamable-http", "url": "http://127.0.0.1:8765/mcp" } } }
```

**Tools:** `search_keyword`, `search_page`, `get_ad_reach`, `scan_keyword`,
`scan_page`, `session_status`, `bootstrap`.

`bootstrap` re-harvests the session from inside the server (use it when
`session_status` reports `valid:false`). It runs **headless** and captures everything
including the reach (`AdLibraryV3AdDetailsQuery`) doc_id — the browser is forced to
English (`locale="en-US"`) so the "See ad details" UI is found regardless of the
country's language, which means **no display/xvfb is needed** and it works on a headless
VPS. (Pass `headless=false` only to watch / hand-clear a consent dialog.)

**Deploying to a VPS:** the server is env-configurable (`MCP_HOST`, `MCP_PORT`,
`MCP_TOKEN` for a secret-in-URL `…/mcp/<token>`, `MCP_SESSION_CACHE`, `MCP_PROFILE_DIR`)
and ships with a `Dockerfile` (just Chromium — headless bootstrap captures reach, no
xvfb). See [DEPLOY.md](DEPLOY.md) — it covers running behind an existing Apache/nginx
(the common case) or with bundled Caddy HTTPS, plus seeding/refreshing the session.

The `search_*` and `scan_*` tools are **paginated** — they return one page plus a
`next_cursor` (and, for scans, a `streak`). The tool descriptions instruct the model to
call again with those values until `done`/no `next_cursor`, so the AI client walks
through results page by page. Tools return `{"error": ...}` (not a crash) when the
session is missing/stale, so the model can ask you to re-run `bootstrap_session()`.
Bootstrap stays a manual CLI step (it opens a browser); the MCP server only consumes
the cached session.

## Troubleshooting

- **Empty results / `StaleDocIdError`** → the `doc_id` rotated. Re-run
  `bootstrap_session()` (or `POST /session/bootstrap`).
- **`SessionExpiredError`** → tokens/cookies were rejected. Re-bootstrap.
- **`BootstrapError`** → the browser couldn't capture the search request. Run headed
  (the default) and clear any consent dialog; make sure the seed search returns results.
- **Error `1357054`** → TLS rejection. Ensure `curl_cffi` is installed (it impersonates
  Chrome); plain `requests` will not work against this endpoint.
