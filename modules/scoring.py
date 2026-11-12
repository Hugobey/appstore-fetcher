"""Configurable scoring and the hard filters that run before it.

Add a criterion by appending an object to config/scoring.json. Use an existing
type, or register a new evaluator with register_evaluator. Unknown values are
reported separately and are excluded from the normalized score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from modules.dates import days_since, utc_today
from modules.labels import AGE_DAYS, DEVELOPER_TYPES, LEGACY_DEVELOPER_TYPES, TEAM_LABELS, UPDATE_DAYS, label_for
from modules.models import Lead

Evaluator = callable


@dataclass
class CriterionResult:
    id: str
    label: str
    status: str
    points: int
    max_points: int
    detail: str


@dataclass
class Score:
    points: int
    known_max: int
    normalized: int | None
    results: list[CriterionResult] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        if self.normalized is None:
            return "unscored"
        return f"{self.normalized}/100"


EVALUATORS: dict[str, Evaluator] = {}


def register_evaluator(type_name: str):
    def decorator(function):
        EVALUATORS[type_name] = function
        return function

    return decorator


def _result(criterion: dict, status: str, points: int, detail: str) -> CriterionResult:
    awarded = points if status == "awarded" else 0
    return CriterionResult(
        id=criterion["id"],
        label=criterion.get("label") or criterion["id"],
        status=status,
        points=awarded,
        max_points=int(criterion.get("max_points") or 0),
        detail=detail,
    )


def _best_band(bands: list[dict], predicate) -> dict | None:
    matches = [band for band in bands if predicate(band)]
    if not matches:
        return None
    return max(matches, key=lambda band: int(band.get("points") or 0))


@register_evaluator("rating_count")
def score_rating_count(lead: Lead, criterion: dict, _today: date) -> CriterionResult:
    install_bands = criterion.get("install_bands") or []
    if lead.install_count is not None and install_bands:
        count = lead.install_count
        match = _best_band(
            install_bands,
            lambda band: int(band.get("min", 0)) <= count <= int(band["max"]),
        )
        if match is None:
            return _result(criterion, "not_matched", 0, f"{count} installs do not fall in a scoring band.")
        return _result(criterion, "awarded", int(match["points"]), match.get("label") or criterion["label"])
    if lead.rating_count is None:
        review_note = ""
        if lead.review_count is not None:
            review_note = f" Written reviews: {lead.review_count}. That number is not used as the rating count."
        return _result(criterion, "unknown", 0, "Rating count is missing." + review_note)
    bands = criterion.get("bands") or []
    match = _best_band(
        bands,
        lambda band: int(band.get("min", 0)) <= lead.rating_count <= int(band["max"]),
    )
    if match is None:
        return _result(
            criterion,
            "not_matched",
            0,
            f"{lead.rating_count} ratings do not fall in a scoring band.",
        )
    return _result(criterion, "awarded", int(match["points"]), match.get("label") or criterion["label"])


@register_evaluator("developer_type")
def score_developer_type(lead: Lead, criterion: dict, _today: date) -> CriterionResult:
    kind = lead.developer_type or "unknown"
    if kind == "unknown":
        return _result(criterion, "unknown", 0, "ICP could not be classified.")
    points_map = criterion.get("points") or {}
    if kind not in points_map:
        return _result(criterion, "not_matched", 0, f"Classified as {kind.replace('_', ' ')}.")
    label = (criterion.get("labels") or {}).get(kind) or kind
    return _result(criterion, "awarded", int(points_map[kind]), label)


@register_evaluator("updated_within_days")
def score_updated(lead: Lead, criterion: dict, today: date) -> CriterionResult:
    days = days_since(lead.last_update, today)
    if days is None:
        return _result(criterion, "unknown", 0, "Last update date is missing.")
    match = _best_band(criterion.get("bands") or [], lambda band: days <= int(band["max_days"]))
    if match is None:
        return _result(criterion, "not_matched", 0, f"Updated {days} days ago.")
    return _result(criterion, "awarded", int(match["points"]), match.get("label") or f"Updated {days} days ago.")


@register_evaluator("launched_within_days")
def score_launched(lead: Lead, criterion: dict, today: date) -> CriterionResult:
    days = days_since(lead.release_date, today)
    if days is None:
        return _result(criterion, "unknown", 0, "Release date is missing.")
    match = _best_band(criterion.get("bands") or [], lambda band: days <= int(band["max_days"]))
    if match is None:
        return _result(criterion, "not_matched", 0, f"Launched {days} days ago.")
    return _result(criterion, "awarded", int(match["points"]), match.get("label") or f"Launched {days} days ago.")


@register_evaluator("monetization")
def score_monetization(lead: Lead, criterion: dict, _today: date) -> CriterionResult:
    if lead.price is None and lead.has_iap is None:
        return _result(criterion, "unknown", 0, "Price and in-app purchases were not available.")
    points_map = criterion.get("points") or {}
    labels = criterion.get("labels") or {}
    if (lead.price is not None and lead.price > 0) or lead.has_iap is True:
        return _result(
            criterion,
            "awarded",
            int(points_map.get("basic", 0)),
            labels.get("basic", "Basic monetization"),
        )
    return _result(
        criterion,
        "awarded",
        int(points_map.get("none_visible", 0)),
        labels.get("none_visible", "No visible monetization"),
    )


@register_evaluator("founder_contact")
def score_founder_contact(lead: Lead, criterion: dict, _today: date) -> CriterionResult:
    points = int(criterion.get("points") or 0)
    if lead.enrichment_status in {"pending", "blocked", "error"}:
        return _result(criterion, "unknown", 0, "Founder contact was not checked.")
    if not lead.developer_website and not lead.emails:
        return _result(criterion, "unknown", 0, "No developer website to check for a founder email.")
    if any(item.email_type == "founder" for item in lead.emails):
        return _result(criterion, "awarded", points, "Verified founder contact.")
    return _result(criterion, "not_matched", 0, "No founder email with supporting evidence.")


def _prepare_criterion(criterion: dict, icp: str | None) -> dict:
    prepared = dict(criterion)
    if not icp:
        return prepared
    bands = (prepared.get("bands_by_icp") or {}).get(icp)
    if bands:
        prepared["bands"] = bands
    install_bands = (prepared.get("install_bands_by_icp") or {}).get(icp)
    if install_bands:
        prepared["install_bands"] = install_bands
    return prepared


def score_lead(lead: Lead, scoring: dict, today: date | None = None, icp: str | None = None) -> Score:
    current = today or utc_today()
    selected = LEGACY_DEVELOPER_TYPES.get(icp, icp) if icp else None
    results: list[CriterionResult] = []
    for criterion in scoring.get("criteria") or []:
        if not criterion.get("enabled", True):
            results.append(_result(criterion, "disabled", 0, "Disabled in scoring config."))
            continue
        prepared = _prepare_criterion(criterion, selected)
        if selected and selected in (prepared.get("waive_for_icp") or []):
            results.append(
                _result(prepared, "unknown", 0, "Installs and ratings are not used to rank an agency.")
            )
            continue
        evaluator = EVALUATORS.get(prepared.get("type"))
        if evaluator is None:
            results.append(_result(prepared, "unknown", 0, f"No evaluator for {prepared.get('type')}."))
            continue
        results.append(evaluator(lead, prepared, current))
    points = sum(item.points for item in results if item.status == "awarded")
    known_max = sum(item.max_points for item in results if item.status in {"awarded", "not_matched"})
    missing = [item.label for item in results if item.status == "unknown"]
    normalized = None
    if known_max > 0:
        normalized = int(round(points / known_max * 100))
    return Score(points=points, known_max=known_max, normalized=normalized, results=results, missing=missing)


def apply_score(lead: Lead, scoring: dict, today: date | None = None, icp: str | None = None) -> Score:
    result = score_lead(lead, scoring, today, icp=icp)
    lead.score_points = result.points
    lead.score_known_max = result.known_max
    lead.score_normalized = result.normalized
    lead.score_missing = list(result.missing)
    lead.score_breakdown = [
        {
            "id": item.id,
            "label": item.label,
            "status": item.status,
            "points": item.points,
            "max_points": item.max_points,
            "detail": item.detail,
        }
        for item in result.results
    ]
    return result


def qualification_failure(score: Score, minimum: int, minimum_known: int) -> str | None:
    if score.normalized is None:
        return "not enough information to score"
    if score.known_max < minimum_known:
        return (
            f"only {score.known_max} scoring points could be evaluated "
            f"(need {minimum_known} before a thin score can pass)"
        )
    if score.normalized < minimum:
        return f"score {score.normalized} is below {minimum}"
    return None


def _tokens(name: str) -> list[str]:
    return [part for part in re.split(r"[^A-Za-z0-9+&]+", name.lower()) if part]


def _has_suffix(name: str, suffixes: list[str]) -> bool:
    tokens = set(_tokens(name))
    lowered = name.lower().strip().rstrip(".")
    for suffix in suffixes:
        if suffix in tokens or lowered.endswith(f" {suffix}"):
            return True
    return False


def _strip_suffixes(name: str, suffixes: list[str]) -> str:
    cleaned = name.strip()
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            pattern = re.compile(rf"[\s,]+{re.escape(suffix)}\.?$", re.IGNORECASE)
            updated = pattern.sub("", cleaned).strip(" ,")
            if updated != cleaned:
                cleaned = updated
                changed = True
    return cleaned


def _rule_number(rules: dict, key: str, default: int) -> int:
    try:
        return int(rules.get(key, default))
    except (TypeError, ValueError):
        return default


def _phrase_hit(text: str, phrases: list) -> str:
    lowered = text.lower()
    for phrase in phrases:
        if phrase and str(phrase).lower() in lowered:
            return str(phrase)
    return ""


def _traction_tier(lead: Lead, rules: dict) -> tuple[str | None, str]:
    """Return early, startup, scaleup, or None when the store sent no traction signal.

    Revenue wins when an actor sends it. Google Play sends install buckets.
    The App Store actor sends neither, so rating count is the stand-in there.
    """
    startup_installs = _rule_number(rules, "startup_installs", 1000)
    scaleup_installs = _rule_number(rules, "scaleup_installs", 100_000)
    startup_ratings = _rule_number(rules, "startup_ratings", 100)
    scaleup_ratings = _rule_number(rules, "scaleup_ratings", 5000)
    startup_revenue = _rule_number(rules, "startup_revenue_usd", 20_000)
    scaleup_revenue = _rule_number(rules, "scaleup_revenue_usd", 500_000)
    rank = {"early": 0, "startup": 1, "scaleup": 2}
    best: str | None = None
    reason = ""

    def consider(level: str, detail: str) -> None:
        nonlocal best, reason
        if best is None or rank[level] > rank[best]:
            best = level
            reason = detail

    if lead.revenue_usd is not None:
        amount = f"${lead.revenue_usd:,.0f}"
        if lead.revenue_usd >= scaleup_revenue:
            consider("scaleup", f"Revenue field is {amount}.")
        elif lead.revenue_usd >= startup_revenue:
            consider("startup", f"Revenue field is {amount}.")
        else:
            consider("early", f"Revenue field is {amount}.")
    if lead.install_count is not None:
        count = f"{lead.install_count:,}"
        if lead.install_count >= scaleup_installs:
            consider("scaleup", f"{count} installs.")
        elif lead.install_count >= startup_installs:
            consider("startup", f"{count} installs.")
        else:
            consider("early", f"{count} installs.")
    if lead.install_count is None and lead.revenue_usd is None and lead.rating_count is not None:
        count = f"{lead.rating_count:,}"
        proxy = "This store did not send downloads or revenue."
        if lead.rating_count >= scaleup_ratings:
            consider("scaleup", f"{count} ratings. {proxy}")
        elif lead.rating_count >= startup_ratings:
            consider("startup", f"{count} ratings. {proxy}")
        else:
            consider("early", f"{count} ratings. {proxy}")
    return best, reason


def _identity(name: str, description: str, tokens: set[str], rules: dict) -> tuple[str | None, str]:
    solo_phrase = _phrase_hit(description, rules.get("solo_phrases") or [])
    if solo_phrase:
        return "solo", f'Description says "{solo_phrase}".'
    startup_phrase = _phrase_hit(description, rules.get("startup_phrases") or [])
    if startup_phrase:
        return "startup", f'Description says "{startup_phrase}".'
    business_words = {item.lower() for item in rules.get("business_words") or []}
    suffixes = [item.lower().rstrip(".") for item in rules.get("company_suffixes") or []]
    if _has_suffix(name, suffixes):
        remainder = _strip_suffixes(name, suffixes)
        if _looks_like_person(remainder) and not set(_tokens(remainder)) & business_words:
            return "solo", "Legal suffix on a person's name."
        return "startup", "Legal suffix on a brand name."
    product_tokens = {item.lower() for item in rules.get("product_tokens") or []}
    if tokens & product_tokens:
        return "startup", "Developer name looks like a product studio."
    if _looks_like_person(name):
        if tokens & business_words:
            return "startup", "Developer name looks like a product brand."
        return "solo", "Developer name looks like a person."
    return None, ""


def _agency_reason(name: str, description: str, tokens: set[str], rules: dict) -> str:
    agency_tokens = {item.lower() for item in rules.get("agency_tokens") or []}
    if tokens & agency_tokens:
        return "Developer name says agency or consultancy."
    phrase = _phrase_hit(f"{name} {description}", rules.get("agency_phrases") or [])
    if phrase:
        return f'Description says "{phrase}".'
    return ""


def classify_developer(lead: Lead, settings: dict) -> str:
    """Assign solo, agency, startup, scaleup, large, or unknown.

    A blocklisted publisher is `large` and matches no ICP. Agency language wins
    over traction, because a popular client app is still an agency prospect.
    Scaleup traction then wins over a personal name. A missing signal stays
    `unknown` and is not filtered out.
    """
    rules = settings.get("developer_classification") or {}
    name = (lead.developer_name or "").strip()
    description = lead.description or ""
    tokens = set(_tokens(name))
    blocklist = {item.lower() for item in rules.get("blocklist") or []}
    if tokens & blocklist:
        lead.icp_reason = "Name matches a known large publisher."
        return "large"
    agency = _agency_reason(name, description, tokens, rules)
    if agency:
        lead.icp_reason = agency
        return "agency"
    tier, tier_reason = _traction_tier(lead, rules)
    identity, identity_reason = _identity(name, description, tokens, rules)
    if tier == "scaleup":
        lead.icp_reason = tier_reason
        return "scaleup"
    if identity == "solo" and tier in {None, "early"}:
        lead.icp_reason = " ".join(part for part in (identity_reason, tier_reason) if part)
        return "solo"
    if identity == "startup" or tier == "startup":
        lead.icp_reason = " ".join(part for part in (identity_reason, tier_reason) if part)
        return "startup"
    lead.icp_reason = "Not enough evidence to choose an ICP."
    return "unknown"


def _looks_like_person(name: str) -> bool:
    parts = [part for part in re.split(r"\s+", name.strip()) if part]
    if not 2 <= len(parts) <= 3:
        return False
    particles = {"de", "da", "di", "van", "von", "del", "la", "le", "st", "st."}
    for part in parts:
        if part.lower() in particles:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z'’\-]{1,}", part):
            return False
    return True


def hard_filter_reason(lead: Lead, targeting: dict, today: date | None = None) -> str | None:
    """Return why a lead is excluded, or None when it survives. Missing data does not exclude."""
    current = today or utc_today()
    cap = targeting.get("max_ratings")
    if cap != "any" and lead.rating_count is not None and lead.rating_count > int(cap):
        return f"{lead.rating_count} ratings is above the maximum of {cap}"
    age_key = targeting.get("max_app_age")
    if age_key in AGE_DAYS:
        age = days_since(lead.release_date, current)
        if age is not None and age > AGE_DAYS[age_key]:
            return f"launched {age} days ago, past the {age_key} limit"
    update_key = targeting.get("last_update")
    if update_key in UPDATE_DAYS:
        updated = days_since(lead.last_update, current)
        if updated is not None and updated > UPDATE_DAYS[update_key]:
            return f"updated {updated} days ago, past the {update_key} window"
    chosen = targeting.get("developer_types") or targeting.get("developer_type")
    return _developer_filter_reason(lead.developer_type, chosen)


def selected_developer_types(targeting: dict) -> list[str]:
    raw = targeting.get("developer_types")
    if not isinstance(raw, list) or not raw:
        raw = [targeting.get("developer_type")]
    selected = []
    for item in raw:
        mapped = LEGACY_DEVELOPER_TYPES.get(item, item)
        if mapped and mapped not in selected:
            selected.append(mapped)
    return selected


def _developer_filter_reason(kind: str, target) -> str | None:
    if kind == "large":
        return "developer matches a known large publisher"
    selected = []
    values = target if isinstance(target, (list, tuple, set)) else [target]
    for item in values:
        mapped = LEGACY_DEVELOPER_TYPES.get(item, item)
        if mapped and mapped not in selected:
            selected.append(mapped)
    if not selected or kind in {None, "", "unknown"} or kind in selected:
        return None
    found = TEAM_LABELS.get(kind, kind)
    wanted = " or ".join(label_for(DEVELOPER_TYPES, item) for item in selected)
    return f"ICP looks like {found}, not {wanted}"
