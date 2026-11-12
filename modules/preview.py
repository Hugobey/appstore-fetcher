"""Terminal summaries and the import review."""

from __future__ import annotations

from datetime import date

from modules.dates import days_ago_label
from modules.labels import TEAM_LABELS
from modules.models import Lead


def frame(title: str, lines: list[str], width: int = 40) -> str:
    bar = "=" * width
    body = [bar, title.center(width), bar, ""]
    body.extend(lines)
    body.extend(["", bar])
    return "\n".join(body)


def print_configuration(state: dict, settings: dict, crm_summary: dict) -> None:
    from modules.wizard import describe, search_summary

    rows = list(describe(state))
    rows.append(("", ""))
    rows.append(("CRM:", crm_summary.get("status", "Connected")))
    rows.append(("Existing leads:", str(crm_summary.get("existing_leads", 0))))
    stored = "Targeting preferences loaded" if state.get("confirmed") else "Suggested defaults"
    rows.append(("Previously stored:", stored))
    rows.append(("", ""))
    rows.extend(search_summary(state, settings))
    if _print_rich_summary(rows):
        return
    lines = [f"{label:<20}{value}" for label, value in rows if label or value]
    print()
    print(frame("PROSPECTING CONFIGURATION", lines))
    print()


def _print_rich_summary(rows: list[tuple[str, str]]) -> bool:
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        return False
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for label, value in rows:
        table.add_row(label, str(value))
    Console().print()
    Console().print(Panel(table, title="PROSPECTING CONFIGURATION", border_style="cyan", padding=(1, 2)))
    Console().print()
    return True


def format_summary(stats: dict) -> str:
    rows = [
        ("Apps discovered:", stats.get("discovered", 0)),
        ("Already in CRM:", stats.get("already_in_crm", 0)),
        ("Filtered out:", stats.get("filtered_out", 0)),
        ("Missing developer site:", stats.get("missing_developer_site", 0)),
        ("Missing public email:", stats.get("missing_public_email", 0)),
        ("Qualified prospects:", stats.get("qualified", 0)),
    ]
    lines = [f"{label:<26}{value:>6}" for label, value in rows]
    text = frame("DISCOVERY RESULTS", lines)
    extras = []
    if stats.get("filtered_hard"):
        extras.append(f"Hard filters: {stats['filtered_hard']}")
    if stats.get("filtered_score"):
        extras.append(f"Below the minimum score or too little known data: {stats['filtered_score']}")
    if stats.get("enrichment_errors"):
        extras.append(f"Enrichment blocked or failed: {stats['enrichment_errors']}")
    if stats.get("errors"):
        extras.append("Errors:")
        extras.extend(f"- {item}" for item in stats["errors"])
    if extras:
        text += "\n\n" + "\n".join(extras)
    return text


def format_lead(index: int, lead: Lead, today: date | None = None) -> str:
    team = TEAM_LABELS.get(lead.developer_type, lead.developer_type)
    developer = lead.developer_name or team
    if lead.developer_name:
        developer = f"{lead.developer_name} ({team})"
    if lead.rating_count is None and lead.review_count is not None:
        ratings = f"unknown ({lead.review_count} written reviews)"
    elif lead.rating_count is None:
        ratings = "unknown"
    else:
        ratings = str(lead.rating_count)
    installs = f"\n   Installs: {lead.install_count:,}" if lead.install_count is not None else ""
    revenue = f"\n   Revenue: ${lead.revenue_usd:,.0f}" if lead.revenue_usd is not None else ""
    primary = lead.primary_email
    email = primary.address if primary else "none"
    source = _short_source(primary.source_url) if primary else (lead.enrichment_note or "none")
    score = "unscored" if lead.score_normalized is None else f"{lead.score_normalized}/100"
    return "\n".join(
        [
            f"{index}. {lead.app_name}",
            f"   Developer: {developer}",
            f"   Ratings: {ratings}{installs}{revenue}",
            f"   Last update: {days_ago_label(lead.last_update, today)}",
            f"   Score: {score}",
        ]
        + ([f"   Estimated MRR: {lead.estimated_mrr}"] if lead.estimated_mrr else [])
        + [
            f"   Email: {email}",
            f"   Email source: {source}",
        ]
    )


def format_details(lead: Lead, today: date | None = None) -> str:
    lines = [
        lead.app_name,
        f"Platform: {lead.platform}",
        f"Category: {lead.category or 'unknown'}",
        f"App Store ID: {lead.app_store_id}",
        f"URL: {lead.app_store_url}",
        f"Website: {lead.developer_website or 'none'}",
        f"Released: {lead.release_date or 'unknown'} ({days_ago_label(lead.release_date, today)})",
        f"Updated: {lead.last_update or 'unknown'} ({days_ago_label(lead.last_update, today)})",
        f"Ratings: {lead.rating_count if lead.rating_count is not None else 'unknown'}",
        f"Installs: {lead.install_count if lead.install_count is not None else 'not sent by this store'}",
        f"Revenue: {'not sent by this store' if lead.revenue_usd is None else f'${lead.revenue_usd:,.0f}'}",
        f"Written reviews: {lead.review_count if lead.review_count is not None else 'unknown'}",
        f"Average rating: {lead.average_rating if lead.average_rating is not None else 'unknown'}",
        f"Price: {'unknown' if lead.price is None else lead.price}",
        f"In-app purchases: {'unknown' if lead.has_iap is None else lead.has_iap}",
        f"ICP: {TEAM_LABELS.get(lead.developer_type, lead.developer_type)}",
        f"ICP evidence: {lead.icp_reason or 'none'}",
        f"Contact name: {lead.contact_name or 'not stated'}",
        f"Enrichment: {lead.enrichment_status} — {lead.enrichment_note}",
        "",
        f"Score: {lead.score_points}/{lead.score_known_max} known points, shown as "
        f"{'unscored' if lead.score_normalized is None else str(lead.score_normalized) + '/100'}",
    ]
    if lead.score_missing:
        lines.append("Not scored because unknown: " + ", ".join(lead.score_missing))
    for item in lead.score_breakdown:
        if item.get("status") == "disabled":
            continue
        prefix = {"awarded": "+", "not_matched": "0", "unknown": "?"}.get(item.get("status"), "-")
        lines.append(f"  {prefix} {item.get('detail')}")
    if lead.emails:
        lines.append("")
        lines.append("Public emails:")
        for email in lead.emails:
            lines.append(f"  {email.address} ({email.email_type}) from {email.source_url}")
            if email.evidence:
                lines.append(f"    {email.evidence}")
    else:
        lines.append("")
        lines.append("Public emails: none")
    if lead.description:
        lines.append("")
        lines.append("Description:")
        lines.append(lead.description[:500])
    return "\n".join(lines)


def _short_source(url: str) -> str:
    text = (url or "").replace("https://", "").replace("http://", "")
    return text[:80]


def review_prospects(prompter, discovery, templates: list[dict], today: date | None = None):
    """Return (leads, template) or None when the user cancels."""
    print()
    print(format_summary(discovery.stats))
    print()
    qualified = list(discovery.qualified)
    optional = list(discovery.missing_email) + list(discovery.missing_site)
    if not qualified and not optional:
        print("No prospects to review.")
        return None
    working = qualified
    while True:
        _print_list(working, today)
        choices = [
            ("Approve all", "all"),
            ("Choose which prospects to import", "choose"),
            ("Exclude prospects", "exclude"),
            ("Inspect a prospect", "inspect"),
        ]
        if optional:
            choices.append((f"Also review {len(optional)} prospects without a public email", "optional"))
        choices.append(("Cancel without importing", "cancel"))
        choice = prompter.select("Results", choices, default="all")
        if choice in {None, "cancel"}:
            return None
        if choice == "inspect":
            picked = _pick_one(prompter, working or optional)
            if picked is not None:
                print()
                print(format_details(picked, today))
                print()
                prompter.select("Lead details", [("Return to the results", "back")], default="back")
            continue
        if choice == "exclude":
            removed = _pick_many(prompter, working, checked=False, message="Select prospects to remove from this import.")
            if removed:
                removed_ids = {id(item) for item in removed}
                working = [item for item in working if id(item) not in removed_ids]
            continue
        if choice == "optional":
            picked = _pick_many(
                prompter,
                optional,
                checked=False,
                message="Select prospects without a public email to add to the import.",
            )
            if picked:
                existing = {item.dedup_id() for item in working}
                for item in picked:
                    if item.dedup_id() not in existing:
                        working.append(item)
                        existing.add(item.dedup_id())
            continue
        if choice == "choose":
            working = _pick_many(prompter, working, checked=True, message="Keep the prospects you want to import.")
            if working is None:
                return None
        if not working:
            print("Nothing is selected.")
            again = prompter.select("Import", [("Return to the results", "back"), ("Cancel", "cancel")])
            if again == "cancel" or again is None:
                return None
            working = qualified
            continue
        decision = _confirm_import(prompter, working, templates, discovery.template_id)
        if decision == "back":
            continue
        return decision


def _print_list(leads: list[Lead], today: date | None) -> None:
    if not leads:
        print("No prospects in the current list.")
        return
    for index, lead in enumerate(leads, start=1):
        print(format_lead(index, lead, today))
        print()


def _pick_one(prompter, leads: list[Lead]) -> Lead | None:
    if not leads:
        print("There is nothing to inspect.")
        return None
    choices = [(f"{index}. {lead.app_name}", lead) for index, lead in enumerate(leads, start=1)]
    choices.append(("Back", None))
    return prompter.select("Which prospect?", choices)


def _pick_many(prompter, leads: list[Lead], checked: bool, message: str) -> list[Lead] | None:
    if not leads:
        return []
    choices = [(f"{index}. {lead.app_name}", lead) for index, lead in enumerate(leads, start=1)]
    default = leads if checked else []
    selected = prompter.checkbox(message, choices, default=default)
    if selected is None:
        return None
    return selected


def _confirm_import(prompter, leads: list[Lead], templates: list[dict], template_id: str):
    by_id = {item["id"]: item for item in templates}
    current = template_id if template_id in by_id else templates[0]["id"]
    while True:
        template = by_id[current]
        print()
        print(f"Ready to append {len(leads)} prospect(s).")
        print(f"Template: {template['name']}")
        print("Existing CRM rows will not be changed.")
        choice = prompter.select(
            "Import these prospects?",
            [
                ("Import", "import"),
                ("Change template", "template"),
                ("Back to the results", "back"),
                ("Cancel", "cancel"),
            ],
            default="import",
        )
        if choice == "import":
            return {"leads": leads, "template": template}
        if choice == "template":
            picked = prompter.select(
                "Email template",
                [(item["name"], item["id"]) for item in templates],
                default=current,
            )
            if picked:
                current = picked
            continue
        if choice == "back":
            return "back"
        return None
