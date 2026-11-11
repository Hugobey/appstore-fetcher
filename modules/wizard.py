"""Interactive targeting wizard. Saved answers are the next run's defaults."""

from __future__ import annotations

from modules.config import validate_targeting
from modules.errors import ConfigError
from modules.labels import (
    AGE_LABELS,
    APP_AGES,
    CATEGORIES,
    DEVELOPER_TYPES,
    PLATFORM_LABELS,
    PLATFORMS,
    UPDATE_LABELS,
    UPDATE_WINDOWS,
    label_for,
)
from modules.preview import print_configuration
from modules.prompts import BACK
from modules.scraper import estimate_cost, search_jobs


def run_wizard(prompter, targeting: dict, settings: dict, templates: list[dict], crm_summary: dict) -> dict | None:
    state = validate_targeting(targeting)
    if state.get("confirmed"):
        choice = prompter.select(
            "Saved targeting preferences loaded.",
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
            return _confirm_loop(prompter, state, settings, templates, crm_summary)
    walked = _walk(prompter, state, allow_back=True)
    if walked is None:
        return None
    return _confirm_loop(prompter, walked, settings, templates, crm_summary)


def _walk(prompter, state: dict, allow_back: bool, start: int = 0) -> dict | None:
    steps = _steps()
    index = start
    while index < len(steps):
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
    return state


def _steps():
    return [
        _ask_developer,
        _ask_platform,
        _ask_categories,
        _ask_ratings,
        _ask_age,
        _ask_update,
        _ask_score,
        _ask_volume,
    ]


def _confirm_loop(prompter, state, settings, templates, crm_summary) -> dict | None:
    while True:
        print_configuration(state, settings, crm_summary)
        choice = prompter.select(
            "Configuration",
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
        edited = _edit_one(prompter, state, templates)
        if edited is None:
            return None
        state.update(edited)


def _edit_one(prompter, state, templates) -> dict | None:
    options = [
        ("ICP", "developer"),
        ("Platforms", "platform"),
        ("Categories and keywords", "categories"),
        ("Maximum ratings", "ratings"),
        ("Maximum app age", "age"),
        ("Last update", "update"),
        ("Minimum score", "score"),
        ("Target prospects", "volume"),
        ("Storefront country", "country"),
        ("Email template", "template"),
        ("Back to the summary", "back"),
    ]
    choice = prompter.select("Which setting do you want to change?", options)
    if choice in {None, "back"}:
        return {}
    dispatch = {
        "developer": lambda: _ask_developer(prompter, state, allow_back=False),
        "platform": lambda: _ask_platform(prompter, state, allow_back=False),
        "categories": lambda: _ask_categories(prompter, state, allow_back=False),
        "ratings": lambda: _ask_ratings(prompter, state, allow_back=False),
        "age": lambda: _ask_age(prompter, state, allow_back=False),
        "update": lambda: _ask_update(prompter, state, allow_back=False),
        "score": lambda: _ask_score(prompter, state, allow_back=False),
        "volume": lambda: _ask_volume(prompter, state, allow_back=False),
        "country": lambda: _ask_country(prompter, state),
        "template": lambda: _ask_template(prompter, state, templates),
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


def _ask_developer(prompter, state, allow_back=False):
    answer = prompter.select(
        "Which ICP are you targeting?",
        _with_back(_as_choices(DEVELOPER_TYPES), allow_back),
        default=state.get("developer_type"),
    )
    if answer is None or answer == BACK:
        return answer
    return {"developer_type": answer}


def _ask_platform(prompter, state, allow_back=False):
    answer = prompter.select(
        "Which platforms should we scrape?",
        _with_back(_as_choices(PLATFORMS), allow_back),
        default=state.get("platforms"),
    )
    if answer is None or answer == BACK:
        return answer
    return {"platforms": answer}


def _ask_categories(prompter, state, allow_back=False):
    while True:
        if allow_back:
            back = prompter.select("Categories", [("Continue", "continue"), ("← Back", BACK)], default="continue")
            if back is None or back == BACK:
                return back
        selected = prompter.checkbox(
            "Which categories should we search? You can select more than one.",
            CATEGORIES,
            default=state.get("categories") or [],
        )
        if selected is None:
            return None
        default_keywords = ", ".join(state.get("custom_keywords") or [])
        raw = prompter.text(
            "Custom keywords, separated by commas. Press Enter to skip.",
            default=default_keywords,
        )
        if raw is None:
            return None
        keywords = [part.strip() for part in raw.split(",") if part.strip()]
        if not selected and not keywords:
            print("Select at least one category or enter a custom keyword.")
            continue
        return {"categories": selected, "custom_keywords": keywords}


def _rating_choices(icp: str) -> list[tuple[str, object]]:
    if icp == "startup":
        return [("500", 500), ("2,000", 2000), ("10,000", 10000), ("No maximum", "any"), ("Custom value", "custom")]
    if icp == "scaleup":
        return [("10,000", 10000), ("100,000", 100000), ("No maximum", "any"), ("Custom value", "custom")]
    if icp == "agency":
        return [("1,000", 1000), ("10,000", 10000), ("No maximum", "any"), ("Custom value", "custom")]
    return [("10", 10), ("30", 30), ("50", 50), ("100", 100), ("Custom value", "custom")]


def _suggested_rating_cap(icp: str, current):
    """A low cap removes startups and scaleups before the ICP check can see them."""
    if icp in {"startup", "scaleup", "agency"} and current in {10, 30, 50, 100}:
        if icp == "startup":
            return 10000
        return "any"
    return current


def _ask_ratings(prompter, state, allow_back=False):
    current = state.get("max_ratings")
    icp = state.get("developer_type") or "solo"
    choices = _rating_choices(icp)
    suggested = _suggested_rating_cap(icp, current)
    values = [value for _label, value in choices]
    default = suggested if suggested in values else "custom"
    answer = prompter.select(
        "What is the maximum number of ratings an app can have?",
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
        "How old can the app be?",
        _with_back(_as_choices(APP_AGES), allow_back),
        default=state.get("max_app_age"),
    )
    if answer is None or answer == BACK:
        return answer
    return {"max_app_age": answer}


def _ask_update(prompter, state, allow_back=False):
    answer = prompter.select(
        "How recently must the app have been updated?",
        _with_back(_as_choices(UPDATE_WINDOWS), allow_back),
        default=state.get("last_update"),
    )
    if answer is None or answer == BACK:
        return answer
    return {"last_update": answer}


def _ask_score(prompter, state, allow_back=False):
    current = state.get("min_score")
    choices = [("40", 40), ("50", 50), ("60", 60), ("70", 70), ("Custom value", "custom")]
    default = current if current in {40, 50, 60, 70} else "custom"
    answer = prompter.select(
        "What minimum lead score should be imported?",
        _with_back(choices, allow_back),
        default=default,
    )
    if answer is None or answer == BACK:
        return answer
    if answer == "custom":
        value = _ask_int(prompter, "Minimum score", current if current is not None else 50, minimum=0, maximum=100)
        if value is None:
            return None
        return {"min_score": value}
    return {"min_score": answer}


def _ask_volume(prompter, state, allow_back=False):
    current = state.get("target_prospects")
    choices = [("10", 10), ("20", 20), ("30", 30), ("50", 50), ("Custom value", "custom")]
    default = current if current in {10, 20, 30, 50} else "custom"
    answer = prompter.select(
        "How many new prospects should we aim to collect?",
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


def _ask_country(prompter, state):
    while True:
        raw = prompter.text("App Store country code", default=state.get("country") or "us")
        if raw is None:
            return None
        country = raw.strip().lower()
        if len(country) == 2 and country.isalpha():
            return {"country": country}
        print("Enter a 2-letter country code, such as us.")


def _ask_template(prompter, state, templates):
    choices = [(item["name"], item["id"]) for item in templates]
    answer = prompter.select("Which email template should new leads use?", choices, default=state.get("template_id"))
    if answer is None:
        return None
    return {"template_id": answer}


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
    if state.get("custom_keywords"):
        categories = categories + [f"keywords: {', '.join(state['custom_keywords'])}"]
    cap = state.get("max_ratings")
    return [
        ("ICP:", label_for(DEVELOPER_TYPES, state.get("developer_type"))),
        ("Platform:", PLATFORM_LABELS.get(state.get("platforms"), state.get("platforms"))),
        ("Categories:", ", ".join(categories) or "None"),
        ("Maximum ratings:", "No maximum" if cap == "any" else str(cap)),
        ("Maximum app age:", AGE_LABELS.get(state.get("max_app_age"), state.get("max_app_age"))),
        ("Last update:", UPDATE_LABELS.get(state.get("last_update"), state.get("last_update"))),
        ("Minimum score:", str(state.get("min_score"))),
        ("Target prospects:", str(state.get("target_prospects"))),
    ]


def search_summary(state: dict, settings: dict) -> list[tuple[str, str]]:
    jobs = search_jobs(state, settings)
    ios = sum(1 for platform, _keyword in jobs if platform == "ios")
    android = sum(1 for platform, _keyword in jobs if platform == "android")
    bits = []
    if ios:
        bits.append(f"{ios} iOS")
    if android:
        bits.append(f"{android} Android")
    cap = settings["apify"]["max_items_per_search"]
    return [
        ("Storefront:", str(state.get("country", "us")).upper()),
        ("Searches:", f"up to {' and '.join(bits) or '0'} ({cap} apps each)"),
        ("Apify estimate:", f"up to ${estimate_cost(jobs, settings):.2f} if every search runs"),
        ("Charge cap:", f"${float(settings['apify']['max_charge_usd_per_search']):.2f} per search"),
    ]
