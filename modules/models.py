"""Shared lead records. Dates stay as ISO strings so sessions save cleanly."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields


@dataclass
class EmailFound:
    address: str
    email_type: str
    source_url: str
    evidence: str = ""


@dataclass
class Lead:
    platform: str
    app_store_id: str
    app_name: str = ""
    developer_name: str = ""
    app_store_url: str = ""
    developer_website: str = ""
    support_url: str = ""
    description: str = ""
    category: str = ""
    release_date: str | None = None
    last_update: str | None = None
    rating_count: int | None = None
    review_count: int | None = None
    average_rating: float | None = None
    install_count: int | None = None
    revenue_usd: float | None = None
    price: float | None = None
    has_iap: bool | None = None
    developer_type: str = "unknown"
    icp_reason: str = ""
    emails: list[EmailFound] = field(default_factory=list)
    contact_name: str = ""
    contact_page_url: str = ""
    enrichment_status: str = "pending"
    enrichment_note: str = ""
    rejected_email_count: int = 0
    search_keyword: str = ""
    filter_reason: str = ""
    score_normalized: int | None = None
    score_points: int = 0
    score_known_max: int = 0
    score_breakdown: list[dict] = field(default_factory=list)
    score_missing: list[str] = field(default_factory=list)
    estimated_mrr: str = ""
    mrr_numeric: int | None = None
    source: dict = field(default_factory=dict)

    def dedup_id(self) -> str:
        return f"{self.platform}:{self.app_store_id}".lower()

    @property
    def primary_email(self) -> EmailFound | None:
        if not self.emails:
            return None
        order = {"founder": 0, "general": 1, "support": 2}
        return sorted(self.emails, key=lambda item: (order.get(item.email_type, 9), item.address))[0]


def lead_to_dict(lead: Lead) -> dict:
    return asdict(lead)


def lead_from_dict(data: dict) -> Lead:
    emails = [EmailFound(**item) for item in data.get("emails") or []]
    payload = dict(data)
    payload["emails"] = emails
    known = {item.name for item in fields(Lead)}
    return Lead(**{key: value for key, value in payload.items() if key in known})
