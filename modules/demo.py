"""Built-in sample apps for a full local run without Apify or Google Sheets."""

from __future__ import annotations

from datetime import timedelta

from modules.dates import utc_today
from modules.deduplication import DedupIndex
from modules.enrichment import enrich_lead

TODAY = utc_today()


def _days_ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat() + "T00:00:00Z"


def sample_items() -> list[tuple[str, str, dict]]:
    """Actor-shaped records. Dates are relative so the sample stays inside the default filters."""
    return [
        (
            "ios",
            "focus timer",
            {
                "kind": "software",
                "trackId": 1001,
                "trackName": "Quiet Focus",
                "artistName": "Jane Doe",
                "trackViewUrl": "https://apps.apple.com/us/app/quiet-focus/id1001",
                "sellerUrl": "https://quiet.example",
                "description": "A focus timer built by a solo developer.",
                "primaryGenreName": "Productivity",
                "releaseDate": _days_ago(60),
                "currentVersionReleaseDate": _days_ago(12),
                "userRatingCount": 60,
                "averageUserRating": 4.8,
                "price": 0,
                "formattedPrice": "Free",
            },
        ),
        (
            "ios",
            "habit tracker",
            {
                "kind": "software",
                "trackId": 1002,
                "trackName": "Little Habits",
                "artistName": "Northwind Studio",
                "trackViewUrl": "https://apps.apple.com/us/app/little-habits/id1002",
                "sellerUrl": "https://habits.example",
                "description": "We are a small team of two building a habit tracker.",
                "primaryGenreName": "Health & Fitness",
                "releaseDate": _days_ago(200),
                "currentVersionReleaseDate": _days_ago(35),
                "userRatingCount": 21,
                "averageUserRating": 4.6,
                "price": 0,
            },
        ),
        (
            "ios",
            "focus timer",
            {
                "kind": "software",
                "trackId": 1003,
                "trackName": "Already Listed",
                "artistName": "Sam Lee",
                "trackViewUrl": "https://apps.apple.com/us/app/already-listed/id1003",
                "sellerUrl": "https://listed.example",
                "description": "Solo developer utility.",
                "primaryGenreName": "Productivity",
                "releaseDate": _days_ago(20),
                "currentVersionReleaseDate": _days_ago(5),
                "userRatingCount": 4,
                "price": 0,
            },
        ),
        (
            "ios",
            "focus timer",
            {
                "kind": "software",
                "trackId": 1004,
                "trackName": "Giant Tasks",
                "artistName": "Jane Doe",
                "trackViewUrl": "https://apps.apple.com/us/app/giant-tasks/id1004",
                "sellerUrl": "https://giant.example",
                "description": "A popular task app.",
                "primaryGenreName": "Productivity",
                "releaseDate": _days_ago(40),
                "currentVersionReleaseDate": _days_ago(3),
                "userRatingCount": 4800,
                "price": 0,
            },
        ),
        (
            "ios",
            "focus timer",
            {
                "kind": "software",
                "trackId": 1005,
                "trackName": "Paper Day",
                "artistName": "Mina Ortiz",
                "trackViewUrl": "https://apps.apple.com/us/app/paper-day/id1005",
                "description": "A journal from a solo developer. No public site yet.",
                "primaryGenreName": "Lifestyle",
                "releaseDate": _days_ago(30),
                "currentVersionReleaseDate": _days_ago(8),
                "userRatingCount": 55,
                "price": 0,
            },
        ),
        (
            "ios",
            "habit tracker",
            {
                "kind": "software",
                "trackId": 1006,
                "trackName": "Pocket Budget",
                "artistName": "Owen Blake",
                "trackViewUrl": "https://apps.apple.com/us/app/pocket-budget/id1006",
                "sellerUrl": "https://budget.example",
                "description": "Budgeting for one person, built by a solo developer.",
                "primaryGenreName": "Finance",
                "releaseDate": _days_ago(90),
                "currentVersionReleaseDate": _days_ago(20),
                "userRatingCount": 58,
                "price": 2.99,
            },
        ),
        (
            "ios",
            "focus timer",
            {
                "kind": "software",
                "trackId": 1007,
                "trackName": "Old Cards",
                "artistName": "Lina Costa",
                "trackViewUrl": "https://apps.apple.com/us/app/old-cards/id1007",
                "sellerUrl": "https://cards.example",
                "description": "Flash cards from a solo developer.",
                "primaryGenreName": "Education",
                "releaseDate": _days_ago(1400),
                "currentVersionReleaseDate": _days_ago(10),
                "userRatingCount": 9,
                "price": 0,
            },
        ),
        (
            "ios",
            "focus timer",
            {
                "kind": "software",
                "trackId": 1008,
                "trackName": "Acme Tasks",
                "artistName": "Acme Apps LLC",
                "trackViewUrl": "https://apps.apple.com/us/app/acme-tasks/id1008",
                "sellerUrl": "https://acme.example",
                "description": "Task management for teams.",
                "primaryGenreName": "Productivity",
                "releaseDate": _days_ago(40),
                "currentVersionReleaseDate": _days_ago(6),
                "userRatingCount": 12,
                "price": 0,
            },
        ),
    ]


PAGES = {
    "https://quiet.example": """
        <html><body>
          <p>Quiet Focus is made by Jane Doe, founder.</p>
          <a href="mailto:jane@quiet.example">jane@quiet.example</a>
          <a href="/privacy">Privacy</a>
        </body></html>
    """,
    "https://quiet.example/privacy": """
        <html><body><p>Privacy contact jane@quiet.example</p></body></html>
    """,
    "https://habits.example": """
        <html><body>
          <a href="/contact">Contact</a>
          <p>A small studio.</p>
        </body></html>
    """,
    "https://habits.example/contact": """
        <html><body><p>Email support@habits.example for help.</p></body></html>
    """,
    "https://budget.example": """
        <html><body><p>No email is published on this site.</p></body></html>
    """,
    "https://cards.example": "<html><body><p>hello@cards.example</p></body></html>",
    "https://acme.example": "<html><body><p>info@acme.example</p></body></html>",
    "https://listed.example": "<html><body><p>sam@listed.example</p></body></html>",
    "https://giant.example": "<html><body><p>hello@giant.example</p></body></html>",
}


def demo_index() -> DedupIndex:
    index = DedupIndex()
    index.add("ios", "1003", "https://apps.apple.com/us/app/id1003")
    return index


def demo_searcher():
    grouped: dict[tuple[str, str], list[dict]] = {}
    for platform, keyword, item in sample_items():
        grouped.setdefault((platform, keyword), []).append(item)
    # Sample rows are stored by their fixture group. Live discovery asks for genres.
    feeds = {
        "productivity": "focus timer",
        "health & fitness": "habit tracker",
        "focus timer": "focus timer",
        "habit tracker": "habit tracker",
    }

    def search(platform: str, category: str, _country: str, page: int = 1):
        if int(page) > 1:
            return [], {"cached": False, "usage_usd": 0}
        group = feeds.get(str(category).strip().lower(), "")
        return grouped.get((platform, group), []), {"cached": False, "usage_usd": 0}

    return search


def demo_enricher(settings: dict):
    def fetch(url: str):
        cleaned = url.split("?", 1)[0].rstrip("/")
        if cleaned.endswith("/robots.txt"):
            return 404, None, url
        body = PAGES.get(cleaned) or PAGES.get(cleaned + "/")
        if body is None:
            return 404, None, url
        return 200, body, url

    def enrich(lead):
        return enrich_lead(
            lead,
            settings,
            fetcher=fetch,
            sleep=lambda _seconds: None,
            public_check=lambda _url: True,
        )

    return enrich


def demo_itunes(_ids, _country):
    return {"results": []}
