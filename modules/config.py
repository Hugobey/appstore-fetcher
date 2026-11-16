"""Load and validate JSON configuration. Secrets stay outside these files."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from modules.errors import ConfigError
from modules.labels import (
    AGE_DAYS,
    CATEGORIES,
    DEVELOPER_TYPES,
    LEGACY_DEVELOPER_TYPES,
    PLATFORMS,
    UPDATE_DAYS,
    PRICING_MODELS,
    USER_PROFILES,
)

REVENUE_MODES = {"tracker", "gross_sales", "no_iap", "skip"}
PRICING_IDS = {key for key, _label in PRICING_MODELS}

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TARGETING = {
    "confirmed": False,
    "developer_type": "solo",
    "developer_types": ["solo", "startup"],
    "platforms": "ios",
    "categories": ["Productivity", "Health & Fitness"],
    "custom_keywords": [],
    "max_ratings": 250,
    "max_app_age": "2y",
    "last_update": "180d",
    "min_score": 50,
    "target_prospects": 30,
    "max_mrr": None,
    "country": "global",
    "template_id": "intro",
    "saved_at": None,
}

SCORING_TYPES = {
    "rating_count",
    "developer_type",
    "updated_within_days",
    "launched_within_days",
    "monetization",
    "founder_contact",
}


def project_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def load_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing file: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object.")
    return data


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> dict:
    path = ROOT / "config" / "settings.json"
    data = load_json(path)
    required = ["spreadsheet_id", "credentials_file", "apify", "enrichment", "category_keywords"]
    missing = [key for key in required if key not in data]
    if missing:
        raise ConfigError(f"{path} is missing: {', '.join(missing)}")
    if not str(data["spreadsheet_id"]).strip():
        raise ConfigError("spreadsheet_id in config/settings.json is empty.")
    apify = data["apify"]
    for key in ("ios_actor", "android_actor", "country", "max_items_per_search", "max_searches_per_run"):
        if key not in apify:
            raise ConfigError(f"config/settings.json apify.{key} is required.")
    if int(apify["max_items_per_search"]) < 1:
        raise ConfigError("apify.max_items_per_search must be at least 1.")
    if int(apify["max_searches_per_run"]) < 1:
        raise ConfigError("apify.max_searches_per_run must be at least 1.")
    if float(apify.get("max_charge_usd_per_search", 0)) <= 0:
        raise ConfigError("apify.max_charge_usd_per_search must be greater than 0.")
    return data


def load_scoring(settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    path = project_path(settings.get("scoring_file", "config/scoring.json"))
    data = load_json(path)
    criteria = data.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ConfigError(f"{path} needs a non-empty criteria list.")
    seen = set()
    for index, criterion in enumerate(criteria, start=1):
        if not isinstance(criterion, dict):
            raise ConfigError(f"Criterion {index} in {path} must be an object.")
        identifier = criterion.get("id") or f"#{index}"
        if identifier in seen:
            raise ConfigError(f"Duplicate scoring criterion id: {identifier}")
        seen.add(identifier)
        criterion_type = criterion.get("type")
        if criterion_type not in SCORING_TYPES:
            known = ", ".join(sorted(SCORING_TYPES))
            raise ConfigError(
                f"Criterion {identifier} has unknown type {criterion_type!r}. Known types: {known}."
            )
        if "max_points" not in criterion:
            raise ConfigError(f"Criterion {identifier} is missing max_points.")
        try:
            max_points = int(criterion["max_points"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Criterion {identifier} max_points must be a number.") from exc
        if max_points < 0:
            raise ConfigError(f"Criterion {identifier} max_points cannot be negative.")
        criterion["enabled"] = bool(criterion.get("enabled", True))
    try:
        minimum_known = int(data.get("minimum_known_max", 0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("minimum_known_max must be a number.") from exc
    if minimum_known < 0:
        raise ConfigError("minimum_known_max cannot be negative.")
    data["minimum_known_max"] = minimum_known
    return data


def load_templates(settings: dict | None = None) -> list[dict]:
    settings = settings or load_settings()
    path = project_path(settings.get("templates_file", "templates/emails.json"))
    data = load_json(path)
    templates = data.get("templates")
    if not isinstance(templates, list) or not templates:
        raise ConfigError(f"{path} needs a non-empty templates list.")
    cleaned = []
    for index, template in enumerate(templates, start=1):
        if not isinstance(template, dict):
            raise ConfigError(f"Template {index} in {path} must be an object.")
        for key in ("id", "name", "subject", "body"):
            if not str(template.get(key, "")).strip():
                raise ConfigError(f"Template {index} in {path} is missing {key}.")
        cleaned.append(template)
    return cleaned


def _as_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError("Estimated MRR choice must be yes or no.")


def wants_mrr(targeting: dict | None) -> bool:
    """False skips App Store Tracker. Missing means the legacy enrich-by-default path."""
    if not targeting or "consider_mrr" not in targeting:
        return True
    return _as_bool(targeting.get("consider_mrr"), default=True)


def revenue_mode_of(targeting: dict | None) -> str:
    """How this run should fill Estimated MRR. A saved mode wins over the legacy toggle."""
    mode = str((targeting or {}).get("revenue_mode") or "").strip()
    if mode in REVENUE_MODES:
        return mode
    if not wants_mrr(targeting):
        return "skip"
    return "tracker"


def _optional_count(value, label: str) -> int | None:
    """None means the filter is off. Zero and 'any' are the same as unset."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "any", "none", "no"}:
            return None
        value = text
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a whole number or 'any'.") from exc
    if number < 0:
        raise ConfigError(f"{label} cannot be negative.")
    if number == 0:
        return None
    return number


def _max_mrr(value) -> int | None:
    """None means no ceiling. 'any' and blank are the wizard's no-limit choice."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
        text = text.removesuffix("/mo").removesuffix("/month")
        if text in {"", "any", "none", "no", "nolimit", "unlimited"}:
            return None
        value = text
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("Maximum MRR must be a whole number of dollars or 'any'.") from exc
    if amount < 0:
        raise ConfigError("Maximum MRR cannot be negative.")
    return amount


def validate_targeting(data: dict) -> dict:
    incoming = dict(data or {})
    merged = dict(DEFAULT_TARGETING)
    merged.update(incoming)
    developer_ids = {key for key, _label in DEVELOPER_TYPES}
    platform_ids = {key for key, _label in PLATFORMS}
    # A saved file that only has developer_type must not inherit the wider default list.
    if isinstance(incoming.get("developer_type"), list):
        raw_types = incoming["developer_type"]
    elif isinstance(incoming.get("developer_types"), list) and incoming.get("developer_types"):
        raw_types = incoming["developer_types"]
    elif incoming.get("developer_type"):
        raw_types = [incoming["developer_type"]]
    else:
        raw_types = list(DEFAULT_TARGETING.get("developer_types") or ["solo"])
    normalized_types = []
    for item in raw_types:
        mapped = LEGACY_DEVELOPER_TYPES.get(item, item)
        if mapped not in developer_ids:
            raise ConfigError(f"Unknown developer type: {mapped}")
        if mapped not in normalized_types:
            normalized_types.append(mapped)
    if not normalized_types:
        raise ConfigError("Select at least one ICP.")
    merged["developer_types"] = normalized_types
    merged["developer_type"] = normalized_types[0]
    if merged["platforms"] not in platform_ids:
        raise ConfigError(f"Unknown platform: {merged['platforms']}")
    if merged["max_app_age"] not in AGE_DAYS and merged["max_app_age"] != "any":
        raise ConfigError(f"Unknown app age: {merged['max_app_age']}")
    if merged["last_update"] not in UPDATE_DAYS and merged["last_update"] != "any":
        raise ConfigError(f"Unknown update window: {merged['last_update']}")
    if merged.get("max_ratings") == "any":
        merged["max_ratings"] = "any"
    else:
        try:
            merged["max_ratings"] = int(merged["max_ratings"])
        except (TypeError, ValueError) as exc:
            raise ConfigError("Maximum ratings must be a whole number or 'any'.") from exc
        if merged["max_ratings"] < 0:
            raise ConfigError("Maximum ratings cannot be negative.")
    if "min_ratings" in incoming:
        floor = _optional_count(incoming.get("min_ratings"), "Minimum ratings")
        if floor is None:
            merged.pop("min_ratings", None)
        else:
            merged["min_ratings"] = floor
    else:
        merged.pop("min_ratings", None)
    try:
        merged["min_score"] = int(merged["min_score"])
        merged["target_prospects"] = int(merged["target_prospects"])
    except (TypeError, ValueError) as exc:
        raise ConfigError("Score and target must be whole numbers.") from exc
    if not 0 <= merged["min_score"] <= 100:
        raise ConfigError("Minimum score must be between 0 and 100.")
    if not 1 <= merged["target_prospects"] <= 500:
        raise ConfigError("Target prospects must be between 1 and 500.")
    merged["max_mrr"] = _max_mrr(merged.get("max_mrr"))
    categories = merged.get("categories") or []
    if not isinstance(categories, list):
        raise ConfigError("Categories must be a list.")
    unknown = [item for item in categories if item not in CATEGORIES]
    if unknown:
        raise ConfigError(f"Unknown categories: {', '.join(unknown)}")
    keywords = merged.get("custom_keywords") or []
    if not isinstance(keywords, list):
        raise ConfigError("Custom keywords must be a list.")
    merged["custom_keywords"] = [str(item).strip() for item in keywords if str(item).strip()]
    merged["categories"] = list(categories)
    if not merged["categories"]:
        raise ConfigError("Select at least one category.")
    country = str(merged.get("country") or "global").strip().lower()
    if country != "global" and (len(country) != 2 or not country.isalpha()):
        raise ConfigError("Storefront country must be a 2-letter code, such as us, or 'global'.")
    merged["country"] = country
    merged["confirmed"] = bool(merged.get("confirmed"))
    merged["template_id"] = str(merged.get("template_id") or "intro")
    # Absent keys mean the wizard still has to ask. Do not invent a profile.
    if "user_profile" in incoming and str(incoming.get("user_profile") or "").strip():
        profile_ids = {key for key, _label in USER_PROFILES}
        label_to_profile = {label.lower(): key for key, label in USER_PROFILES}
        raw_profile = str(incoming.get("user_profile")).strip()
        profile = label_to_profile.get(raw_profile.lower(), raw_profile)
        if profile not in profile_ids:
            raise ConfigError(f"Unknown user profile: {raw_profile}")
        merged["user_profile"] = profile
    else:
        merged.pop("user_profile", None)
    if "consider_mrr" in incoming:
        merged["consider_mrr"] = _as_bool(incoming.get("consider_mrr"), default=True)
    else:
        merged.pop("consider_mrr", None)
    pricing = str(incoming.get("pricing_model") or "").strip() if "pricing_model" in incoming else ""
    if pricing:
        if pricing not in PRICING_IDS:
            raise ConfigError(f"Unknown app pricing model: {pricing}")
        merged["pricing_model"] = pricing
        if pricing == "upfront":
            merged["consider_mrr"] = False
            merged["max_mrr"] = None
            merged["revenue_mode"] = "gross_sales"
        elif pricing == "ad_supported":
            merged["consider_mrr"] = False
            merged["max_mrr"] = None
            merged["revenue_mode"] = "no_iap"
        elif merged.get("consider_mrr") is False:
            merged["revenue_mode"] = "skip"
            merged["max_mrr"] = None
        elif merged.get("consider_mrr") is True:
            merged["revenue_mode"] = "tracker"
        else:
            merged.pop("revenue_mode", None)
    else:
        merged.pop("pricing_model", None)
        mode = str(incoming.get("revenue_mode") or "").strip() if "revenue_mode" in incoming else ""
        if mode in REVENUE_MODES:
            merged["revenue_mode"] = mode
        else:
            merged.pop("revenue_mode", None)
    return merged


def load_targeting() -> tuple[dict, str | None]:
    """Return targeting and a warning. Invalid files fall back to defaults."""
    path = ROOT / "config" / "targeting.json"
    if not path.exists():
        return dict(DEFAULT_TARGETING), None
    try:
        data = load_json(path)
        return validate_targeting(data), None
    except ConfigError as exc:
        return dict(DEFAULT_TARGETING), (
            f"{path.name} could not be used ({exc}). Starting from the built-in defaults."
        )


def save_targeting(targeting: dict) -> Path:
    path = ROOT / "config" / "targeting.json"
    payload = validate_targeting(targeting)
    payload["confirmed"] = True
    payload["saved_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    save_json(path, payload)
    return path


def read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def apify_token(settings: dict) -> str:
    import os

    env_token = os.environ.get("APIFY_TOKEN", "").strip()
    if env_token:
        return env_token
    return read_secret(project_path(settings.get("apify_token_file", "credentials/apify_token")))
