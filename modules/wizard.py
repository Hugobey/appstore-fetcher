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


def run_wizard(prompter, targeting: dict, settings: dict, templates: list[dict], crm_summary: dict) -> dict | None:
    state = validate_targeting(targeting)
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
        _ask_max_mrr,
    ]


def _confirm_loop(prompter, state, settings, templates, crm_summary) -> dict | None:
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
        edited = _edit_one(prompter, state, templates)
        if edited is None:
            return None
        state.update(edited)


def _edit_one(prompter, state, templates) -> dict | None:
    options = [
        ("ICP", "developer"),
        ("Platforms", "platform"),
        ("Categories", "categories"),
        ("Maximum ratings", "ratings"),
        ("Maximum app age", "age"),
        ("Last update", "update"),
        ("Minimum score", "score"),
        ("Target prospects", "volume"),
        ("Max MRR", "max_mrr"),
        ("Storefront country", "country"),
        ("Email template", "template"),
        ("Back to the summary", "back"),
    ]
    choice = prompter.select("✏️ Which setting do you want to change?", options)
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
        "max_mrr": lambda: _ask_max_mrr(prompter, state, allow_back=False),
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


def _ask_ratings(prompter, state, allow_back=False):
    current = state.get("max_ratings")
    icp = _rating_icp(state)
    choices = _with_recommended(_rating_choices(icp), 250)
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


def _ask_score(prompter, state, allow_back=False):
    current = state.get("min_score")
    choices = [("40", 40), ("50", 50), ("60", 60), ("70", 70), ("Custom value", "custom")]
    default = current if current in {40, 50, 60, 70} else "custom"
    answer = prompter.select(
        "⚡ What minimum lead score should be imported?",
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


def _ask_template(prompter, state, templates):
    choices = [(item["name"], item["id"]) for item in templates]
    answer = prompter.select(
        "✉️ Which email template should new leads use?",
        choices,
        default=state.get("template_id"),
    )
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
    cap = state.get("max_ratings")
    selected = list(state.get("developer_types") or [])
    if not selected and state.get("developer_type"):
        selected = [state.get("developer_type")]
    icp = ", ".join(label_for(DEVELOPER_TYPES, item) for item in selected)
    return [
        ("ICP:", icp),
        ("Platform:", PLATFORM_LABELS.get(state.get("platforms"), state.get("platforms"))),
        ("Categories:", ", ".join(categories) or "None"),
        ("Maximum ratings:", "No maximum" if cap == "any" else str(cap)),
        ("Maximum app age:", AGE_LABELS.get(state.get("max_app_age"), state.get("max_app_age"))),
        ("Last update:", UPDATE_LABELS.get(state.get("last_update"), state.get("last_update"))),
        ("Minimum score:", str(state.get("min_score"))),
        ("Target prospects:", str(state.get("target_prospects"))),
        ("Max MRR:", _max_mrr_label(state.get("max_mrr"))),
    ]


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
            f"{charts} App Store category charts across {len(fronts)} storefront(s), no keywords",
        ),
        ("Apify estimate:", "$0.00 (public category RSS and iTunes Lookup)"),
    ]
