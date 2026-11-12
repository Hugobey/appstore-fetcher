#!/usr/bin/env python3
"""Interactive prospecting CLI. Match mobile apps to an ICP and append them to the CRM."""

from __future__ import annotations

import argparse
import logging
import sys

from modules.config import (
    apify_token,
    load_scoring,
    load_settings,
    load_targeting,
    load_templates,
    project_path,
    save_targeting,
)
from modules.crm import GoogleSheetCRM, build_import_row
from modules.demo import demo_enricher, demo_index, demo_itunes, demo_searcher
from modules.enrichment import enrich_lead
from modules.errors import ApifyError, ConfigError, ProspectingError
from modules.outreach import (
    add_suppression,
    gmail_service,
    primary_signature,
    record_sends,
    remove_suppression,
    send_messages,
    sendable_rows,
    suppressed_addresses,
)
from modules.pipeline import discover
from modules.preview import review_prospects
from modules.prompts import QuestionaryPrompter
from modules.activity import Narrator
from modules.scraper import ApifyClient, discovery_jobs, estimate_cost, missing_token_message, search_attempt_limit
from modules.session import (
    append_raw,
    archive_session,
    leads_from,
    load_raw,
    load_session,
    new_session,
    save_session,
    store_discovery,
)
from modules.wizard import run_wizard

LOGGER = logging.getLogger("prospecting")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        _setup_logging(project_path(settings.get("log_file", "logs/prospecting.log")))
        if args.command == "check":
            return check_crm(settings)
        if args.command == "suppress":
            path = add_suppression(settings, args.email, args.reason)
            print(f"Added {args.email.strip().lower()} to {path}. This address will not be emailed.")
            return 0
        if args.command == "unsuppress":
            removed = remove_suppression(settings, args.email)
            print("Removed." if removed else "That address was not on the suppression list.")
            return 0
        if args.command == "send":
            return send_outreach(settings, QuestionaryPrompter())
        return scrape(settings, QuestionaryPrompter(), demo=args.demo)
    except KeyboardInterrupt:
        print("\nInterrupted. Completed search results are kept in data/session.json when a run had started.")
        return 130
    except ProspectingError as exc:
        LOGGER.error("%s", exc)
        print(f"\n{exc}")
        return 1
    except Exception:
        LOGGER.exception("Unexpected error")
        print("\nUnexpected error. Details are in logs/prospecting.log.")
        return 1


def scrape(settings: dict, prompter, demo: bool = False) -> int:
    scoring = load_scoring(settings)
    templates = load_templates(settings)
    targeting, warning = load_targeting()
    if warning:
        print(warning)
    if demo:
        print("Demo mode. Apify and Google Sheets are not called, and nothing is written to the CRM.")
        crm_summary = {"status": "Demo mode", "existing_leads": 1}
        confirmed = run_wizard(prompter, targeting, settings, templates, crm_summary)
        if confirmed is None:
            print("Cancelled.")
            return 130
        save_targeting(confirmed)
        discovery = discover(
            confirmed,
            settings,
            scoring,
            demo_index(),
            searcher=demo_searcher(),
            enricher=demo_enricher(settings),
            itunes_fetcher=demo_itunes,
            prompter=prompter,
        )
        return _review(discovery, None, templates, settings, prompter, None, None, demo=True)

    voice = Narrator()
    crm = GoogleSheetCRM.connect(settings)
    with voice.spin("📊 Indexing existing CRM leads..."):
        index, count, _records = crm.load_rows()
    voice.line(f"CRM connected. Existing leads: {count}.")
    crm_summary = {"status": "Connected", "existing_leads": count}
    session_path = project_path(settings.get("session_file", "data/session.json"))
    session = load_session(session_path)
    prior = None
    if session and session.get("stage") in {"running", "ready"}:
        choice = prompter.select(
            f"A previous run ({session.get('stage')}, {session.get('id')}) is saved.",
            [
                ("Resume it", "resume"),
                ("Start a new search", "new"),
                ("Cancel", "cancel"),
            ],
            default="resume",
        )
        if choice in {None, "cancel"}:
            print("Cancelled.")
            return 130
        if choice == "new":
            archive_session(session_path, session)
            session = None
        elif session.get("stage") == "ready":
            from modules.pipeline import Discovery

            discovery = Discovery(
                qualified=leads_from(session, "qualified"),
                missing_email=leads_from(session, "missing_email"),
                missing_site=leads_from(session, "missing_site"),
                stats=session.get("stats") or {},
                template_id=(session.get("targeting") or {}).get("template_id") or "intro",
            )
            return _review(discovery, crm, templates, settings, prompter, session, session_path)
        else:
            prior = load_raw(session_path, session)

    confirmed = run_wizard(prompter, targeting, settings, templates, crm_summary)
    if confirmed is None:
        print("Cancelled. Nothing was scraped.")
        return 130
    save_targeting(confirmed)
    token = apify_token(settings)
    if not token:
        raise ApifyError(missing_token_message())
    client = ApifyClient(token, settings)
    confirmed = _android_guard(client, confirmed, prompter)
    jobs = discovery_jobs(confirmed, settings)[: search_attempt_limit(settings)]
    _quota_guard(client, settings, estimate_cost([(platform, keyword) for platform, keyword, _page in jobs], settings), prompter)
    if session is None or session.get("stage") != "running":
        session = new_session(session_path, confirmed)
    else:
        session["targeting"] = confirmed
        save_session(session_path, session)

    def on_raw(platform: str, keyword: str, items: list[dict], page: int = 1) -> None:
        append_raw(session_path, session, platform, keyword, items, page)

    discovery = discover(
        confirmed,
        settings,
        scoring,
        index,
        searcher=client.search,
        enricher=lambda lead: enrich_lead(lead, settings),
        prior=prior,
        on_raw=on_raw,
        prompter=prompter,
        narrator=voice,
    )
    store_discovery(session_path, session, discovery)
    return _review(discovery, crm, templates, settings, prompter, session, session_path)


def _review(discovery, crm, templates, settings, prompter, session, session_path, demo: bool = False) -> int:
    decision = review_prospects(prompter, discovery, templates)
    if not decision:
        print("Nothing was imported.")
        if session_path is not None:
            print(f"Results remain in {session_path}. Run the tool again and choose Resume.")
        return 0
    leads = decision["leads"]
    template = decision["template"]
    suppressed = suppressed_addresses(settings)
    held = [
        lead.primary_email.address
        for lead in leads
        if lead.primary_email and lead.primary_email.address.lower() in suppressed
    ]
    if held:
        print(
            f"{len(held)} address(es) are on the suppression list. "
            "They will be stored as Suppressed, with no email subject or body."
        )
    if demo or crm is None:
        print(f"Demo mode would append {len(leads)} row(s). The spreadsheet was not changed.")
        for lead in leads:
            row = build_import_row(lead, [
                "App Name", "Developer", "Target Email", "Status", "Lead Score"
            ], template, suppressed)
            print("  " + " | ".join(row))
        return 0
    added, skipped = crm.append_leads(leads, template, suppressed)
    print(f"Appended {added} prospect(s). Skipped {skipped} already in the CRM. Existing rows were not changed.")
    if session is not None and session_path is not None:
        session["stage"] = "imported"
        save_session(session_path, session)
    return 0


def _android_guard(client: ApifyClient, targeting: dict, prompter) -> dict:
    if targeting.get("platforms") not in {"android", "both"}:
        return targeting
    try:
        account = client.account_summary()
    except ApifyError as exc:
        print(f"Could not read the Apify plan: {exc}")
        choice = prompter.select(
            "Google Play scraping may require a paid Apify plan. Continue?",
            [("Continue with iOS only", "ios"), ("Try the selected platforms anyway", "keep"), ("Cancel", "cancel")],
        )
        if choice in {None, "cancel"}:
            raise ApifyError("Stopped before scraping.")
        if choice == "ios":
            targeting["platforms"] = "ios"
        return targeting
    if account.get("is_paying") is False:
        print(
            f"Apify account {account.get('username') or ''} is on the {account.get('tier') or 'free'} plan. "
            "The Google Play actor does not allow API runs on the free plan."
        )
        choice = prompter.select(
            "How should this run handle Google Play?",
            [("Continue with iOS only", "ios"), ("Try Google Play anyway", "keep"), ("Cancel", "cancel")],
            default="ios",
        )
        if choice in {None, "cancel"}:
            raise ApifyError("Stopped before scraping.")
        if choice == "ios":
            targeting["platforms"] = "ios"
            save_targeting(targeting)
    return targeting


def _quota_guard(client: ApifyClient, settings: dict, estimate: float, prompter) -> None:
    try:
        limits = client.limits()
    except ApifyError as exc:
        print(f"Could not read Apify usage limits: {exc}")
        choice = prompter.select(
            "Continue without quota information?",
            [("Continue", "continue"), ("Cancel", "cancel")],
            default="cancel",
        )
        if choice != "continue":
            raise ApifyError("Stopped because Apify quota could not be checked.")
        return
    print(
        "Apify platform usage this cycle: "
        f"{limits.get('monthly_usage_usd')} of {limits.get('max_monthly_usage_usd')} USD "
        f"(remaining {limits.get('remaining_usd')}). "
        "Actor event prices can be billed separately. "
        f"This run's upper estimate is ${estimate:.2f}, "
        f"with a ${float(settings['apify']['max_charge_usd_per_search']):.2f} cap on each search."
    )
    remaining = limits.get("remaining_usd")
    if remaining is not None and remaining <= 0:
        raise ApifyError("Apify monthly platform usage has reached the account limit. Scraping did not start.")
    cushion = float(settings["apify"].get("min_remaining_usd") or 0)
    if remaining is not None and remaining < cushion:
        allowed = prompter.confirm(
            f"Only ${remaining:.2f} of platform usage remains, below the ${cushion:.2f} cushion. Continue?",
            default=False,
        )
        if not allowed:
            raise ApifyError("Stopped because the Apify balance is below the configured cushion.")


def check_crm(settings: dict) -> int:
    crm = GoogleSheetCRM.connect(settings)
    _index, count, _records = crm.load_rows()
    print(f"Connected to {crm.spreadsheet.title!r} / {crm.worksheet.title!r}.")
    print(f"Existing leads: {count}.")
    print(f"Columns: {', '.join(crm.headers)}")
    return 0


def send_outreach(settings: dict, prompter) -> int:
    crm = GoogleSheetCRM.connect(settings)
    _index, _count, records = crm.load_rows()
    suppressed = suppressed_addresses(settings)
    ready, notes = sendable_rows(records, settings, suppressed)
    for note in notes:
        print(note)
    if not ready:
        print("No messages to send.")
        return 0
    print()
    for row in ready:
        print(f"{row['app_name']} <{row['email']}>")
        print(f"  Subject: {row['subject'] or '(empty subject)'}")
        print(f"  {(row['body'] or '').splitlines()[0] if row.get('body') else '(empty body)'}")
    print()
    print("Emails are sent only after this confirmation. The sheet text is used as-is, including your edits.")
    choice = prompter.select(
        f"Send {len(ready)} message(s) now?",
        [("Send all of these", "all"), ("Choose which", "choose"), ("Cancel", "cancel")],
        default="cancel",
    )
    if choice in {None, "cancel"}:
        print("No messages were sent.")
        return 0
    if choice == "choose":
        picked = prompter.checkbox(
            "Select the messages to send.",
            [(f"{row['app_name']} <{row['email']}>", row) for row in ready],
            default=[],
        )
        ready = picked or []
    if not ready:
        print("No messages were sent.")
        return 0
    confirmed = prompter.confirm(f"Send {len(ready)} message(s) through Gmail now?", default=False)
    if not confirmed:
        print("No messages were sent.")
        return 0
    service = gmail_service(settings)
    signature = primary_signature(service)
    results = send_messages(service, ready, signature)
    record_sends(crm, results)
    failed = [item for item in results if not item.get("ok")]
    if failed:
        print(f"{len(failed)} message(s) failed. Their CRM rows were not marked as sent.")
        return 1
    return 0


def _setup_logging(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    handle = logging.FileHandler(path, encoding="utf-8")
    handle.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handle)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover mobile apps for an ICP and append them to the Google Sheets CRM."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the wizard and scoring on built-in sample apps. Does not call Apify or Google Sheets.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("check", help="Connect to the CRM and print the lead count.")
    commands.add_parser("send", help="Send approved CRM drafts through Gmail. This never runs during scraping.")
    suppress = commands.add_parser("suppress", help="Never email this address again.")
    suppress.add_argument("email")
    suppress.add_argument("--reason", default="")
    unsuppress = commands.add_parser("unsuppress", help="Remove an address from the suppression list.")
    unsuppress.add_argument("email")
    return parser


if __name__ == "__main__":
    sys.exit(main())
