"""Data models: the harvested session, and a normalized ad."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CACHE_PATH = Path("session_cache.json")


@dataclass
class SessionData:
    """Everything needed to replay the Ad Library's GraphQL requests.

    Harvested live by `bootstrap_session()` because Meta rotates these on every
    frontend ship. Persisted to disk so the API need not bootstrap on every call.
    """

    doc_id: str
    lsd: str
    cookies: dict[str, str]
    user_agent: str
    # doc_id of AdLibraryV3AdDetailsQuery (EU reach lookup). Separate persisted query;
    # harvested best-effort during bootstrap. None if it couldn't be captured.
    details_doc_id: str | None = None
    # Logged-out Ad Library requests authenticate with lsd + jazoest and carry NO
    # fb_dtsg, so jazoest is captured live (not derived) and fb_dtsg is optional.
    jazoest: str = ""
    fb_dtsg: str = ""
    # `v` is a variables-version hash the search query sends; harvested alongside
    # doc_id so the two stay in sync across Meta's frontend churn.
    variables_version: str | None = None
    # The COMPLETE set of form params from the live search request (__dyn, __csr,
    # __hsdp, lsd, jazoest, doc_id, ...). We replay this verbatim and override only
    # `variables` — reconstructing a minimal body gets rejected (FB error 1357054).
    request_template: dict[str, str] = field(default_factory=dict)
    # Legacy/subset of housekeeping params (kept for inspection).
    extra_params: dict[str, str] = field(default_factory=dict)
    harvested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionData":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | str = DEFAULT_CACHE_PATH) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CACHE_PATH) -> "SessionData":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def age_seconds(self) -> float:
        return time.time() - self.harvested_at


@dataclass
class Ad:
    """A single normalized ad. `raw` keeps the original node so nothing is lost.

    NOTE: the source paths that populate these fields are wired up in Phase 4,
    against a real captured response — not from memory.
    """

    id: str | None
    page_id: str | None
    page_name: str | None
    creative_bodies: list[str] = field(default_factory=list)
    snapshot_url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    publisher_platforms: list[str] = field(default_factory=list)
    is_active: bool | None = None
    # Link-card creative (where video ads keep their copy: title/description/caption).
    title: str | None = None
    caption: str | None = None
    link_url: str | None = None
    link_description: str | None = None
    cta_text: str | None = None
    display_format: str | None = None
    # Reach / spend / impressions — Meta only populates these for political/issue ads
    # (mostly EU transparency); they are null for ordinary commercial ads.
    reach_estimate: object | None = None
    impressions: str | None = None
    spend: object | None = None
    currency: str | None = None
    # EU transparency reach, filled in only when a search is run with reach enrichment
    # (a separate per-ad details query). `reach` holds the full AdReach dict.
    eu_total_reach: int | None = None
    reach: dict | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AdReach:
    """EU transparency-by-location reach for one ad (the 'EU ad delivery' panel).
    Populated only for ads that target the EU; otherwise eu_total_reach is None."""

    ad_archive_id: str | None
    targets_eu: bool
    eu_total_reach: int | None
    countries: list[str] = field(default_factory=list)
    gender: str | None = None
    age_min: int | None = None
    age_max: int | None = None
    payer: str | None = None
    beneficiary: str | None = None
    # Per-country age/gender reach rows, as Meta returns them.
    breakdown: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanResult:
    """Result of a do-it-all scan-until-low search: every ad scanned (in impressions
    order, each enriched with reach), plus why the scan stopped."""

    threshold: int
    patience: int
    scanned_count: int
    stop_reason: str  # "streak" | "limit" | "exhausted"
    ads: list  # list[Ad], each carrying eu_total_reach

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "patience": self.patience,
            "scanned_count": self.scanned_count,
            "stop_reason": self.stop_reason,
            "ads": [a.to_dict() for a in self.ads],
        }


@dataclass
class ScanPage:
    """One page of a paginated scan-until-low. The streak (consecutive ads below the
    reach threshold) is stateful across pages, so it's returned here and must be passed
    back on the next call along with next_cursor. When `done`, stop paginating."""

    ads: list  # list[Ad] for this page (truncated at the stop point when done by streak)
    done: bool
    stop_reason: str | None  # "streak" | "exhausted" | None (more pages remain)
    streak: int  # consecutive-below count so far — pass to the next call
    next_cursor: str | None  # pass to the next call; None when done

    def to_dict(self) -> dict:
        return {
            "count": len(self.ads),
            "done": self.done,
            "stop_reason": self.stop_reason,
            "streak": self.streak,
            "next_cursor": self.next_cursor,
            "ads": [a.to_dict() for a in self.ads],
        }


@dataclass
class AdPage:
    """One page of search results (Meta fixes the page size at 10) plus the cursor
    needed to fetch the next page."""

    ads: list[Ad]
    end_cursor: str | None
    has_next_page: bool

    def to_dict(self) -> dict:
        return {
            "count": len(self.ads),
            "has_next_page": self.has_next_page,
            "next_cursor": self.end_cursor,
            "ads": [a.to_dict() for a in self.ads],
        }
