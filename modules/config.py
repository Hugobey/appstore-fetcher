"""Load and validate JSON configuration. Secrets stay outside these files."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from modules.errors import ConfigError
from modules.labels import AGE_DAYS, CATEGORIES, DEVELOPER_TYPES, LEGACY_DEVELOPER_TYPES, PLATFORMS, UPDATE_DAYS

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TARGETING = {
    "confirmed": False,
    "developer_type": "solo",
    "platforms": "ios",
    "categories": ["Productivity", "Health & Fitness"],
    "custom_keywords": [],
    "max_ratings": 30,
    "max_app_age": "2y",
    "last_update": "90d",
    "min_score": 50,
    "target_prospects": 30,
    "max_mrr": None,
    "country": "us",
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


def validate_targeting(data: dict) -> dict:
    merged = dict(DEFAULT_TARGETING)
    merged.update(data or {})
    developer_ids = {key for key, _label in DEVELOPER_TYPES}
    platform_ids = {key for key, _label in PLATFORMS}
    raw_types = merged.get("developer_types")
    if isinstance(merged.get("developer_type"), list):
        raw_types = merged["developer_type"]
    if not isinstance(raw_types, list) or not raw_types:
        raw_types = [merged.get("developer_type") or "solo"]
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
    if not merged["categories"] and not merged["custom_keywords"]:
        raise ConfigError("Select at least one category or custom keyword.")
    country = str(merged.get("country") or "us").strip().lower()
    if len(country) != 2 or not country.isalpha():
        raise ConfigError("Storefront country must be a 2-letter code, such as us.")
    merged["country"] = country
    merged["confirmed"] = bool(merged.get("confirmed"))
    merged["template_id"] = str(merged.get("template_id") or "intro")
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
