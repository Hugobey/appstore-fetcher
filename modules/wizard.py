"""Interactive targeting wizard. Saved answers are the next run's defaults."""

from __future__ import annotations

from modules.config import validate_targeting
from modules.errors import ConfigError
from modules.labels import (
    AGE_LABELS,
    APP_AGES,
    CATEGORIES,
    DEVELOPER_TYPES,
    ONBOARDING_CATEGORIES,
    PLATFORM_LABELS,
    PLATFORMS,
    PRICING_MODELS,
    UPDATE_LABELS,
    UPDATE_WINDOWS,
    USER_PROFILES,
    WIZARD_PROFILES,
    label_for,
)

from modules.banner import show_banner
from modules.enricher import _MRR_BUCKETS, normalize_mrr_ranges
from modules.preview import print_configuration
from modules.prompts import BACK


def run_wizard(
    prompter,
    targeting: dict,
    settings: dict,
    templates: list[dict],
    crm_summary: dict,
    internal: bool = False,
) -> dict | None:
    show_banner()
    state = validate_targeting(targeting)
    if state.get("confirmed") and _needs_onboarding(state):
        opened = _ask_missing_onboarding(prompter, state)
        if opened is None:
            return None
        state = validate_targeting({**state, **opened})
    if state.get("confirmed"):
        choice = prompter.select(
            "💾 Saved targeting preferences loaded.",
            [
                ("Continue to the summary", "continue"),
                ("Review each question", "review"),
                ("Cancel", "cancel"),
            ],
            default="continue",
        )
        if choice in {None, "cancel"}:
            return None
        if choice == "continue":
            return _confirm_loop(prompter, state, settings, templates, crm_summary, internal=internal)
    walked = _walk(prompter, state, allow_back=True, internal=internal)
    if walked is None:
        return None
    return _confirm_loop(prompter, walked, settings, templates, crm_summary, internal=internal)


def _walk(prompter, state: dict, allow_back: bool, start: int = 0, internal: bool = False) -> dict | None:
    index = start
    while True:
        steps = _steps(state, internal=internal)
        if index >= len(steps):
            return state
        result = steps[index](prompter, state, allow_back=allow_back and index > 0)
        if result is None:
            return None
        if result == BACK:
            index = max(0, index - 1)
            continue
        state.update(result)
        try:
            validate_targeting(state)
        except ConfigError as exc:
            print(exc)
            continue
        index += 1


def _steps(state: dict | None = None, internal: bool = False):
    """Profile, pricing, categories, then MRR only when the pricing model has it.

    ICP comes next because the rating caps depend on it. The rating floor,
    rating ceiling, app age, and last update follow for every profile.
    `internal` keeps the same questions; the sheet sync is decided by the caller.
    """
    del internal
    state = state or {}
    steps = [_ask_profile, _ask_pricing, _ask_category]
    if _pricing_asks_mrr(state):
        steps.append(_ask_mrr_ranges)
    steps.extend([
        _ask_developer,
        _ask_min_ratings,
        _ask_ratings,
        _ask_age,
        _ask_update,
    ])
    return steps


def _pricing_asks_mrr(state: dict) -> bool:
    return state.get("pricing_model") not in {"upfront", "ad_supported"}


def _confirm_loop(prompter, state, settings, templates, crm_summary, internal: bool = False) -> dict | None:
    while True:
        print_configuration(state, settings, crm_summary)
        choice = prompter.select(
            "✅ Configuration",
            [
                ("Start scraping", "start"),
                ("Edit configuration", "edit"),
                ("Cancel", "cancel"),
            ],
            default="start",
        )
        if choice in {None, "cancel"}:
            return None
        if choice == "start":
            state["confirmed"] = True
            return validate_targeting(state)
        edited = _edit_one(prompter, state, templates, internal=internal)
        if edited is None:
            return None
        state.update(edited)


def _edit_one(prompter, state, templates, internal: bool = False) -> dict | None:
    del internal, templates
    options = [
        ("Your profile", "profile"),
        ("App pricing", "pricing"),
        ("Category", "category"),
    ]
    if _pricing_asks_mrr(state):
        options.append(("MRR Ranges", "mrr_ranges"))
    options.extend(
        [
            ("ICP", "developer"),
            ("Minimum ratings", "min_ratings"),
            ("Maximum ratings", "ratings"),
            ("Maximum app age", "age"),
            ("Last update", "update"),
            ("Target prospects", "volume"),
            ("Storefront country", "country"),
            ("Back to the summary", "back"),
        ]
    )
    choice = prompter.select("✏️ Which setting do you want to change?", options)
    if choice in {None, "back"}:
        return {}
    dispatch = {
        "profile": lambda: _ask_profile(prompter, state, allow_back=False),
        "pricing": lambda: _ask_pricing(prompter, state, allow_back=False),
        "category": lambda: _ask_category(prompter, state, allow_back=False),
        "mrr_ranges": lambda: _ask_mrr_ranges(prompter, state, allow_back=False),
        "developer": lambda: _ask_developer(prompter, state, allow_back=False),
        "ratings": lambda: _ask_ratings(prompter, state, allow_back=False),
        "min_ratings": lambda: _ask_min_ratings(prompter, state, allow_back=False),
        "age": lambda: _ask_age(prompter, state, allow_back=False),
        "update": lambda: _ask_update(prompter, state, allow_back=False),
        "volume": lambda: _ask_volume(prompter, state, allow_back=False),
        "country": lambda: _ask_country(prompter, state),
    }
    result = dispatch[choice]()
    if result is None or result == BACK:
        return {}
    return result


def _with_back(choices: list, allow_back: bool) -> list:
    if not allow_back:
        return choices
    return [("← Back", BACK), *choices]


def _as_choices(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Prompt tuples are (label, value). The stored lists are (value, label)."""
    return [(label, value) for value, label in pairs]


def _needs_onboarding(state: dict) -> bool:
    if "user_profile" not in state or "pricing_model" not in state:
        return True
    if _pricing_asks_mrr(state) and "consider_mrr" not in state:
        return True
    return False


def _ask_missing_onboarding(prompter, state: dict) -> dict | None:
    """Ask only the opening answers a saved file does not have yet."""
    current = dict(state)
    if "user_profile" not in current:
        profile = _ask_profile(prompter, current, allow_back=False)
        if profile is None or profile == BACK:
            return None
        current.update(profile)
    if "pricing_model" not in current:
        pricing = _ask_pricing(prompter, current, allow_back=False)
        if pricing is None or pricing == BACK:
            return None
        current.update(pricing)
    if _pricing_asks_mrr(current) and "consider_mrr" not in current:
        choice = _ask_mrr_ranges(prompter, current, allow_back=False)
        if choice is None or choice == BACK:
            return None
        current.update(choice)
    return current


def _ask_profile(prompter, state, allow_back=False):
    answer = prompter.select(
        "👤 What best describes you?",
        _with_back(_as_choices(WIZARD_PROFILES), allow_back),
        default=state.get("user_profile") if state.get("user_profile") in {key for key, _label in WIZARD_PROFILES} else None,
    )
    if answer is None or answer == BACK:
        return answer
    return {"user_profile": answer}


def _ask_pricing(prompter, state, allow_back=False):
    answer = prompter.select(
        "💳 App Pricing Model",
        _with_back(_as_choices(PRICING_MODELS), allow_back),
        default=state.get("pricing_model") or "freemium",
    )
    if answer is None or answer == BACK:
        return answer
    if answer == "upfront":
        return {
            "pricing_model": "upfront",
            "consider_mrr": False,
            "revenue_mode": "gross_sales",
            "max_mrr": None,
            "mrr_ranges": [],
        }
    if answer == "ad_supported":
        return {
            "pricing_model": "ad_supported",
            "consider_mrr": False,
            "revenue_mode": "no_iap",
            "max_mrr": None,
            "mrr_ranges": [],
        }
    return {"pricing_model": answer}


def _ask_category(prompter, state, allow_back=False):
    """Checkbox of the onboarding categories. Nothing selected means no filter."""
    del allow_back
    current = [name for name in (state.get("categories") or []) if name in ONBOARDING_CATEGORIES]
    answer = prompter.checkbox(
        "📁 Select Target Category",
        [(name, name) for name in ONBOARDING_CATEGORIES],
        default=current,
    )
    if answer is None or answer == BACK:
        return answer
    if isinstance(answer, str):
        answer = [answer]
    picked = {item for item in answer if item and item != BACK}
    selected = [name for name in ONBOARDING_CATEGORIES if name in picked]
    if not selected:
        return {"categories": [], "custom_keywords": []}
    extras = [
        name
        for name in (state.get("categories") or [])
        if name not in ONBOARDING_CATEGORIES and name in CATEGORIES
    ]
    return {"categories": selected + extras, "custom_keywords": []}


def _ask_mrr_ranges(prompter, state, allow_back=False):
    """Checkbox of the public MRR buckets. None or all selected means no filter."""
    del allow_back
    current = normalize_mrr_ranges(state.get("mrr_ranges"))
    answer = prompter.checkbox(
        "💰 MRR Ranges",
        [(name, name) for name in _MRR_BUCKETS],
        default=current,
    )
    if answer is None or answer == BACK:
        return answer
    if isinstance(answer, str):
        answer = [answer]
    return {
        "mrr_ranges": normalize_mrr_ranges(answer),
        "consider_mrr": True,
        "revenue_mode": "tracker",
        "max_mrr": None,
    }


def _icp_choices() -> list[tuple[str, str]]:
    choices = []
    for value, label in DEVELOPER_TYPES:
        if value in {"solo", "startup"}:
            label = f"{label} (Recommended)"
        choices.append((label, value))
    return choices


def _ask_developer(prompter, state, allow_back=False):
    del allow_back
    current = list(state.get("developer_types") or [])
    if not current and state.get("developer_type"):
        current = [state["developer_type"]]
    while True:
        answer = prompter.checkbox(
            "🎯 Which ICP are you targeting?",
            _icp_choices(),
            default=current,
        )
        if answer is None:
            return None
        if isinstance(answer, str):
            answer = [answer]
        selected = [item for item in answer if item and item != BACK]
        if not selected:
            print("Select at least one ICP.")
            continue
        return {"developer_type": selected[0], "developer_types": selected}


def _ask_platform(prompter, state, allow_back=False):
    answer = prompter.select(
        "📱 Which platforms should we scrape?",
        _with_back(_as_choices(PLATFORMS), allow_back),
        default=state.get("platforms"),
    )
    if answer is None or answer == BACK:
        return answer
    return {"platforms": answer}


def _category_choices() -> list[tuple[str, str]]:
    choices = []
    for name in CATEGORIES:
        if name in {"Health & Fitness", "Productivity"}:
            choices.append((f"{name} (Recommended)", name))
        else:
            choices.append((name, name))
    return choices


def _ask_categories(prompter, state, allow_back=False):
    while True:
        if allow_back:
            back = prompter.select("📂 Categories", [("Continue", "continue"), ("← Back", BACK)], default="continue")
            if back is None or back == BACK:
                return back
        selected = prompter.checkbox(
            "📂 Which App Store categories should we search? You can select more than one.",
            _category_choices(),
            default=state.get("categories") or [],
        )
        if selected is None:
            return None
        if isinstance(selected, str):
            selected = [selected]
        selected = [item for item in selected if item and item != BACK]
        if not selected:
            print("Select at least one category.")
            continue
        return {"categories": selected, "custom_keywords": []}


def _rating_choices(icp: str) -> list[tuple[str, object]]:
    if icp == "startup":
        return [
            ("Under 250 ratings", 250),
            ("500", 500),
            ("2,000", 2000),
            ("10,000", 10000),
            ("No maximum", "any"),
            ("Custom value", "custom"),
        ]
    if icp == "scaleup":
        return [("10,000", 10000), ("100,000", 100000), ("No maximum", "any"), ("Custom value", "custom")]
    if icp == "agency":
        return [("1,000", 1000), ("10,000", 10000), ("No maximum", "any"), ("Custom value", "custom")]
    return [
        ("Under 10 ratings", 10),
        ("Under 30 ratings", 30),
        ("Under 50 ratings", 50),
        ("Under 100 ratings", 100),
        ("Under 250 ratings", 250),
        ("Custom value", "custom"),
    ]


def _suggested_rating_cap(icp: str, current):
    """A very low cap removes growth-stage indies before the ICP check can see them."""
    if icp in {"startup", "scaleup", "agency"} and current in {10, 30, 50, 100}:
        if icp == "startup":
            return 250
        return "any"
    return current


def _rating_icp(state: dict) -> str:
    """The broadest selected ICP decides which rating caps are offered."""
    selected = list(state.get("developer_types") or [])
    if not selected and state.get("developer_type"):
        selected = [state["developer_type"]]
    for name in ("scaleup", "startup", "agency", "solo"):
        if name in selected:
            return name
    return "solo"


def _granular_rating_choices() -> list[tuple[str, object]]:
    """Contact-first runs can cap the store at a small public rating count."""
    return [
        ("Under 10 ratings", 10),
        ("Under 30 ratings", 30),
        ("Under 50 ratings", 50),
        ("Under 100 ratings", 100),
        ("Under 250 ratings", 250),
        ("500", 500),
        ("2,000", 2000),
        ("No maximum", "any"),
        ("Custom value", "custom"),
    ]


def _min_rating_choices() -> list[tuple[str, object]]:
    return [
        ("No minimum", "any"),
        ("10", 10),
        ("30", 30),
        ("50", 50),
        ("100", 100),
        ("250", 250),
        ("Custom value", "custom"),
    ]


def _ask_min_ratings(prompter, state, allow_back=False):
    current = state.get("min_ratings")
    choices = _min_rating_choices()
    values = [value for _label, value in choices]
    if current in values:
        default = current
    elif current in (None, "", "any"):
        default = "any"
    else:
        default = "custom"
    answer = prompter.select(
        "⭐ What is the minimum number of ratings an app can have?",
        _with_back(choices, allow_back),
        default=default,
    )
    if answer is None or answer == BACK:
        return answer
    if answer == "custom":
        fallback = current if isinstance(current, int) else 10
        value = _ask_int(prompter, "Minimum ratings", fallback, minimum=0, maximum=10_000_000)
        if value is None:
            return None
        return {"min_ratings": value or None}
    if answer == "any":
        return {"min_ratings": None}
    return {"min_ratings": answer}


def _ask_ratings(prompter, state, allow_back=False):
    current = state.get("max_ratings")
    icp = _rating_icp(state)
    if state.get("consider_mrr"):
        choices = _with_recommended(_rating_choices(icp), 250)
    else:
        choices = _with_recommended(_granular_rating_choices(), 250)
    suggested = _suggested_rating_cap(icp, current)
    values = [value for _label, value in choices]
    if suggested in values:
        default = suggested
    elif current in values:
        default = current
    else:
        default = 250 if 250 in values else "custom"
    answer = prompter.select(
        "⭐ What is the maximum number of ratings an app can have?",
        _with_back(choices, allow_back),
        default=default,
    )
    if answer is None or answer == BACK:
        return answer
    if answer == "custom":
        fallback = current if isinstance(current, int) else 30
        value = _ask_int(prompter, "Maximum ratings", fallback, minimum=0, maximum=10_000_000)
        if value is None:
            return None
        return {"max_ratings": value}
    return {"max_ratings": answer}


def _ask_age(prompter, state, allow_back=False):
    answer = prompter.select(
        "📅 How old can the app be?",
        _with_back(_as_choices(APP_AGES), allow_back),
        default=state.get("max_app_age"),
    )
    if answer is None or answer == BACK:
        return answer
    return {"max_app_age": answer}


def _with_recommended(choices: list, recommended) -> list:
    marked = []
    for label, value in choices:
        text = str(label)
        if value == recommended and "(Recommended)" not in text:
            text = f"{text} (Recommended)"
        marked.append((text, value))
    return marked


def _ask_update(prompter, state, allow_back=False):
    answer = prompter.select(
        "🔄 How recently must the app have been updated?",
        _with_back(_with_recommended(_as_choices(UPDATE_WINDOWS), "180d"), allow_back),
        default=state.get("last_update") or "180d",
    )
    if answer is None or answer == BACK:
        return answer
    return {"last_update": answer}


def _ask_volume(prompter, state, allow_back=False):
    current = state.get("target_prospects")
    choices = _with_recommended(
        [("10", 10), ("20", 20), ("30", 30), ("50", 50), ("Custom value", "custom")],
        30,
    )
    if current in {10, 20, 30, 50}:
        default = current
    elif current is None:
        default = 30
    else:
        default = "custom"
    answer = prompter.select(
        "🚀 How many new prospects should we aim to collect?",
        _with_back(choices, allow_back),
        default=default,
    )
    if answer is None or answer == BACK:
        return answer
    if answer == "custom":
        value = _ask_int(prompter, "Target prospects", current or 30, minimum=1, maximum=500)
        if value is None:
            return None
        return {"target_prospects": value}
    return {"target_prospects": answer}


def _ask_max_mrr(prompter, state, allow_back=False):
    current = state.get("max_mrr")
    choices = _with_recommended(
        [
            ("No limit", "any"),
            ("Under $10k / mo", 10000),
            ("Under $25k / mo", 25000),
            ("Under $50k / mo", 50000),
            ("Under $100k / mo", 100000),
        ],
        "any",
    )
    default = current if current in {10000, 25000, 50000, 100000} else "any"
    answer = prompter.select(
        "💰 Max MRR to include:",
        _with_back(choices, allow_back),
        default=default,
    )
    if answer is None or answer == BACK:
        return answer
    if answer == "any":
        return {"max_mrr": None}
    return {"max_mrr": int(answer)}


def _max_mrr_label(value) -> str:
    if value in (None, "", "any"):
        return "No limit"
    amount = int(value)
    labels = {
        10000: "Under $10k / mo",
        25000: "Under $25k / mo",
        50000: "Under $50k / mo",
        100000: "Under $100k / mo",
    }
    return labels.get(amount, f"Under ${amount:,} / mo")


def _ask_country(prompter, state):
    choices = _with_recommended(
        [
            ("Global / Top English Markets (US, UK, CA, AU)", "global"),
            ("United States (us)", "us"),
            ("United Kingdom (gb)", "gb"),
            ("Canada (ca)", "ca"),
            ("Australia (au)", "au"),
            ("Other country code", "custom"),
        ],
        "global",
    )
    current = str(state.get("country") or "global").strip().lower()
    default = current if current in {"global", "us", "gb", "ca", "au"} else "custom"
    answer = prompter.select(
        "🌍 App Store storefront — Global / Top English Markets (US, UK, CA, AU) (Recommended)",
        choices,
        default=default,
    )
    if answer is None:
        return None
    if answer != "custom":
        return {"country": answer}
    while True:
        raw = prompter.text("🌍 Other App Store country code", default="")
        if raw is None:
            return None
        country = raw.strip().lower()
        if len(country) == 2 and country.isalpha():
            return {"country": country}
        print("Enter a 2-letter country code, such as us.")


def _ask_int(prompter, label: str, default: int, minimum: int, maximum: int) -> int | None:
    while True:
        raw = prompter.text(f"{label} ({minimum}–{maximum})", default=str(default))
        if raw is None:
            return None
        text = raw.strip()
        if text.lower() == "back":
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            value = int(text)
            if minimum <= value <= maximum:
                return value
        print(f"Enter a whole number from {minimum} to {maximum}.")


def describe(state: dict) -> list[tuple[str, str]]:
    categories = list(state.get("categories") or [])
    cap = state.get("max_ratings")
    selected = list(state.get("developer_types") or [])
    if not selected and state.get("developer_type"):
        selected = [state.get("developer_type")]
    icp = ", ".join(label_for(DEVELOPER_TYPES, item) for item in selected)
    profile = label_for(USER_PROFILES, state.get("user_profile") or "other")
    if state.get("user_profile") == "indie_founder":
        profile = "Indie App Founder / Other"
    pricing = label_for(PRICING_MODELS, state.get("pricing_model") or "")
    selected_ranges = normalize_mrr_ranges(state.get("mrr_ranges"))
    if state.get("pricing_model") == "upfront" or state.get("revenue_mode") == "gross_sales":
        ranges_label = "Not applied (upfront paid)"
    elif state.get("pricing_model") == "ad_supported" or state.get("revenue_mode") == "no_iap":
        ranges_label = "N/A (No IAP)"
    elif not selected_ranges:
        ranges_label = "Any"
    else:
        ranges_label = ", ".join(selected_ranges)
    ratings = "No maximum" if cap == "any" else str(cap)
    if state.get("user_profile") == "angel_vc" and state.get("consider_mrr") and cap == "any":
        ratings = "No maximum (high-velocity)"
    rows = [
        ("You:", profile),
        ("App pricing:", pricing or "Not set"),
        ("MRR ranges:", ranges_label),
        ("ICP:", icp),
        ("Platform:", PLATFORM_LABELS.get(state.get("platforms"), state.get("platforms"))),
        ("Categories:", ", ".join(categories) or "Any"),
        ("Maximum ratings:", ratings),
    ]
    floor = state.get("min_ratings")
    if floor not in (None, "", "any"):
        rows.append(("Minimum ratings:", str(floor)))
    rows.extend(
        [
            ("Maximum app age:", AGE_LABELS.get(state.get("max_app_age"), state.get("max_app_age"))),
            ("Last update:", UPDATE_LABELS.get(state.get("last_update"), state.get("last_update"))),
            ("Target prospects:", str(state.get("target_prospects"))),
        ]
    )
    return rows


def search_summary(state: dict, settings: dict) -> list[tuple[str, str]]:
    from modules.scraper import discovery_jobs, storefronts_for

    charts = len(discovery_jobs(state, settings))
    fronts = storefronts_for(state.get("country"))
    return [
        (
            "Storefront:",
            "US, UK, CA, AU" if str(state.get("country", "us")).lower() == "global" else str(state.get("country", "us")).upper(),
        ),
        (
            "Discovery:",
            f"{charts} App Store category charts across {len(fronts)} storefront(s). "
            "A short list also searches the category phrases",
        ),
        ("Apify estimate:", "$0.00 (public category RSS and iTunes Lookup)"),
    ]
