"""Human labels shared by the wizard and the preview."""

from __future__ import annotations

DEVELOPER_TYPES = [
    ("solo", "Solo developer"),
    ("agency", "Agency"),
    ("startup", "Startups"),
    ("scaleup", "Scaleup"),
]

# Who is running the CLI. This is separate from the ICP being targeted.
USER_PROFILES = [
    ("angel_vc", "Angel Investor / VC"),
    ("growth_agency", "Growth Consultant / Agency"),
    ("indie_founder", "Indie App Founder"),
    ("other", "Other"),
]

# The wizard collapses Indie and Other into one choice. Saved `other` stays valid.
WIZARD_PROFILES = [
    ("angel_vc", "Angel Investor / VC"),
    ("growth_agency", "Growth Consultant / Agency"),
    ("indie_founder", "Indie App Founder / Other"),
]

PRICING_MODELS = [
    ("freemium", "Free + In-App Purchases / Subscriptions (Freemium - Best for MRR)"),
    ("upfront", "Upfront Paid Apps Only (One-time purchase / Different market)"),
    ("both", "Both Free/Freemium and Upfront Paid Apps Together"),
    ("ad_supported", "Free / Ad-Supported (No IAP)"),
]

# Discovery categories that have a 6-month cohort benchmark.
ONBOARDING_CATEGORIES = [
    "Finance",
    "Business",
    "Health & Fitness",
    "Productivity",
    "Education",
    "Lifestyle",
    "Photo & Video",
    "Utilities",
]

# Saved answers from the previous audience question.
LEGACY_DEVELOPER_TYPES = {
    "solo_small": "solo",
    "independent": "startup",
    "any": "solo",
    "small_team": "startup",
    "company": "startup",
}

PLATFORMS = [
    ("ios", "iOS / Apple App Store"),
    ("android", "Android / Google Play"),
    ("both", "Both"),
]

CATEGORIES = [
    "Health & Fitness",
    "Productivity",
    "Lifestyle",
    "Finance",
    "Business",
    "Education",
    "Utilities",
    "Photo & Video",
    "Social Networking",
    "Entertainment",
    "Other",
]

APP_AGES = [
    ("6m", "Less than 6 months"),
    ("1y", "Less than 1 year"),
    ("2y", "Less than 2 years"),
    ("any", "Any age"),
]

UPDATE_WINDOWS = [
    ("30d", "Within 30 days"),
    ("60d", "Within 60 days"),
    ("90d", "Within 90 days"),
    ("180d", "Last 180 days"),
    ("365d", "Within 365 days"),
    ("any", "No restriction"),
]

AGE_DAYS = {"6m": 183, "1y": 365, "2y": 730}
UPDATE_DAYS = {"30d": 30, "60d": 60, "90d": 90, "180d": 180, "365d": 365}

DEVELOPER_TYPE_LABELS = dict(DEVELOPER_TYPES)
PLATFORM_LABELS = {"ios": "iOS", "android": "Android", "both": "iOS and Android"}
AGE_LABELS = dict(APP_AGES)
UPDATE_LABELS = {
    "30d": "30 days",
    "60d": "60 days",
    "90d": "90 days",
    "180d": "180 days",
    "365d": "365 days",
    "any": "No restriction",
}
TEAM_LABELS = {
    "solo": "Solo developer",
    "agency": "Agency",
    "startup": "Startups",
    "scaleup": "Scaleup",
    "large": "Large publisher",
    "unknown": "Unclassified",
}


def label_for(pairs: list[tuple[str, str]], value: str) -> str:
    for key, label in pairs:
        if key == value:
            return label
    return value
