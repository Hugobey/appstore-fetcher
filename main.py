#!/usr/bin/env python3
"""Interactive prospecting CLI. Match mobile apps to an ICP and append them to the CRM."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from modules.config import (
    load_scoring,
    load_settings,
    load_targeting,
    load_templates,
    project_path,
    save_targeting,
    revenue_mode_of,
)
from modules.crm import GoogleSheetCRM, build_import_row
from modules.sheets_crm import SHEETS_IMPORT_TIP, apply_crm_layout, deals_export_path, write_export_csv
from modules.enrichment import enrich_lead
from modules.errors import ApifyError, LicenseError, ProspectingError
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
from modules.pipeline import analyze_saved, discover
from modules.run_log import RunLog
from modules.run_store import RunStore
from modules.preview import review_prospects
from modules.prompts import QuestionaryPrompter
from modules.activity import Narrator
from modules.agent_ui import AgentDashboard, is_high_mrr, sheet_url
from modules.category_feed import category_searcher
from modules.scraper import ApifyClient
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
from modules.auth import assert_same_machine, credentials_path, load_credentials
from modules.enricher import (
    ScoutClient,
    activate_license,
    apply_mrr_rows,
    checkout_url,
    leads_from_payload,
)
from modules.static_demo import choose_demo_categories, load_demo_catalog, load_demo_leads, run_static_demo, seed_demo_cache
from modules.telemetry import emit_event

LOGGER = logging.getLogger("prospecting")


def main(argv: list[str] | None = None, prompter=None) -> int:
    args = _parser().parse_args(argv)
    prompter = prompter or QuestionaryPrompter()
    try:
        if args.buy:
            return _open_checkout()
        if args.seed_demo:
            if not args.internal:
                print("Seeding the demo cache is not available in this command.")
                return 1
            return seed_demo_cache()
        if args.demo:
            return play_static_demo(prompter)
        if not args.internal and _sheet_command(args):
            print("Google Sheet CRM sync is not available in this command.")
            return 1
        settings = load_settings()
        _setup_logging(project_path(settings.get("log_file", "logs/prospecting.log")))
        if args.activate:
            return _activate(args.activate)
        if args.command == "check" or (args.format_sheet and args.command is None):
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
            return send_outreach(settings, prompter)
        if args.internal:
            access = "local"
        else:
            access = _ensure_access(prompter)
        if access == "cancel":
            print("Cancelled.")
            return 130
        if access == "demo":
            return play_static_demo(prompter)
        return scrape(
            settings,
            prompter,
            licensed=(access == "licensed"),
            no_telemetry=bool(args.no_telemetry),
            internal=bool(args.internal),
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Completed search results are kept in data/session.json when a run had started.")
        return 130
    except ProspectingError as exc:
        LOGGER.error("%s", exc)
        print(f"\n{exc}")
        if isinstance(exc, LicenseError) and exc.status in {401, 402}:
            print(f"Upgrade: {checkout_url()}")
        return 1
    except Exception:
        LOGGER.exception("Unexpected error")
        print("\nUnexpected error. Details are in logs/prospecting.log.")
        return 1


def scrape(
    settings: dict,
    prompter,
    demo: bool = False,
    licensed: bool = False,
    no_telemetry: bool = False,
    internal: bool = False,
) -> int:
    if demo:
        return play_static_demo(prompter)
    scoring = load_scoring(settings)
    templates = load_templates(settings)
    targeting, warning = load_targeting()
    if warning:
        print(warning)
    board = _new_dashboard(settings, targeting, demo=False)
    if not internal:
        board = AgentDashboard(int((targeting or {}).get("target_prospects") or 0), sheet="", demo=False)
    try:
        return _scrape_live(
            settings,
            prompter,
            scoring,
            templates,
            targeting,
            board,
            licensed=licensed,
            no_telemetry=no_telemetry,
            internal=internal,
        )
    finally:
        board.stop(summary=False)


def play_static_demo(prompter=None) -> int:
    """Replay category-matched bundled leads. No Apify, Sheets, RSS, license, or telemetry calls."""
    raw = os.environ.get("PROSPECTING_DEMO_SECONDS", "").strip()
    duration = 4.0
    if raw:
        try:
            duration = float(raw)
        except ValueError:
            duration = 4.0
    catalog = load_demo_catalog()
    categories = choose_demo_categories(prompter, catalog)
    leads = load_demo_leads(categories=categories)
    if not leads:
        print(f"No demo leads are cached for {', '.join(categories)}.")
        return 1
    print(f"Demo category: {', '.join(categories)}")
    print(
        f"Demo mode. Playing {len(leads)} pre-scraped leads from data/demo_leads.json. "
        "No network calls."
    )
    path = run_static_demo(leads=leads, duration=duration)
    print(f"Exported {path}")
    print(SHEETS_IMPORT_TIP)
    return 0


def _open_checkout() -> int:
    """Open the public checkout page. This does not scrape or touch Google Sheets."""
    import webbrowser

    url = checkout_url()
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return 0


def _activate(license_key: str) -> int:
    saved = activate_license(license_key)
    print(f"License activated for this machine. Instance {saved['instance_id']}.")
    print(f"Saved {credentials_path()}")
    return 0


def _sheet_command(args) -> bool:
    if args.command in {"check", "send"}:
        return True
    return bool(args.format_sheet and args.command is None)


def _ensure_access(prompter) -> str:
    """Return licensed, demo, or cancel.

    Public runs never open the Google Sheet and never use a local Apify token.
    A copied credentials file fails here, before a scan starts.
    """
    credentials = load_credentials()
    if credentials:
        assert_same_machine(credentials)
    if credentials and credentials.get("license_key") and credentials.get("instance_id"):
        return "licensed"
    print(f"Upgrade: {checkout_url()}")
    choice = prompter.select(
        "No license key on this machine.",
        [
            ("Run the instant demo", "demo"),
            ("Activate a license key", "activate"),
            ("Cancel", "cancel"),
        ],
        default="demo",
    )
    if choice in {None, "cancel"}:
        return "cancel"
    if choice == "demo":
        return "demo"
    key = prompter.text("License key", default="")
    if key is None or not str(key).strip():
        return "cancel"
    activate_license(str(key).strip())
    print(f"License activated for this machine. Saved {credentials_path()}")
    return "licensed"


def _scrape_live(
    settings,
    prompter,
    scoring,
    templates,
    targeting,
    board: AgentDashboard,
    *,
    licensed: bool = False,
    no_telemetry: bool = False,
    internal: bool = False,
) -> int:
    if not internal:
        return _scrape_public(
            settings,
            prompter,
            templates,
            targeting,
            board,
            no_telemetry=no_telemetry,
        )
    voice = Narrator()
    panel = board if board.enabled else None
    crm = GoogleSheetCRM.connect(settings)
    with voice.spin("📊 Indexing existing CRM leads..."):
        index, count, _records = crm.load_rows()
    print(f"CRM connected. Existing leads: {count}.")
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

            if panel is not None:
                panel.stop(summary=False)
            discovery = Discovery(
                qualified=leads_from(session, "qualified"),
                missing_email=leads_from(session, "missing_email"),
                missing_site=leads_from(session, "missing_site"),
                stats=session.get("stats") or {},
                template_id=(session.get("targeting") or {}).get("template_id") or "intro",
                target=int((session.get("targeting") or {}).get("target_prospects") or 0),
            )
            return _review(discovery, crm, templates, settings, prompter, session, session_path)
        else:
            prior = load_raw(session_path, session)

    confirmed = run_wizard(prompter, targeting, settings, templates, crm_summary, internal=True)
    if confirmed is None:
        print("Cancelled. Nothing was scraped.")
        return 130
    save_targeting(confirmed)
    _emit_scan(confirmed, licensed=licensed, no_telemetry=no_telemetry)
    board.target = int(confirmed["target_prospects"])
    if not licensed and confirmed.get("platforms") == "android":
        print("Discovery uses App Store categories. Google Play has no genre RSS in this run, so nothing was scraped.")
        return 0
    if not licensed and confirmed.get("platforms") == "both":
        print("Discovery uses App Store categories. Google Play is skipped for this run.")
    if session is None or session.get("stage") != "running":
        session = new_session(session_path, confirmed)
    else:
        session["targeting"] = confirmed
        save_session(session_path, session)

    def on_raw(platform: str, keyword: str, items: list[dict], page: int = 1) -> None:
        append_raw(session_path, session, platform, keyword, items, page)

    if panel is not None:
        panel.start()
        panel.set_scout("Scanning category feeds")
        panel.set_analyst("Waiting until scouting finishes")
        panel.focus(1, "Indexing existing CRM leads...")
        noun = "lead" if count == 1 else "leads"
        panel.complete(1, f"Indexed {count} {noun}")
    run_id = str(session.get("id") or "run")
    if licensed:
        try:
            discovery = _run_licensed(confirmed, panel, run_id)
        except LicenseError as exc:
            if panel is not None:
                panel.stop(summary=False)
            print(f"\n{exc}")
            return 1
    else:
        discovery = _scout_then_analyze(
            confirmed,
            settings,
            scoring,
            templates,
            index,
            searcher=category_searcher(settings),
            enricher=lambda lead: enrich_lead(lead, settings),
            prior=prior,
            on_raw=on_raw,
            prompter=prompter,
            narrator=voice,
            demo=False,
            dashboard=panel,
            crm=crm,
            run_id=run_id,
        )
    store_discovery(session_path, session, discovery)
    if panel is not None:
        panel.pause()
    return _review(
        discovery,
        crm,
        templates,
        settings,
        prompter,
        session,
        session_path,
        dashboard=panel,
        saved_during_scout=int(discovery.stats.get("saved_during_scout") or 0),
    )


def _emit_scan(confirmed: dict, *, licensed: bool, no_telemetry: bool) -> None:
    try:
        emit_event(
            "scan_started",
            user_profile=str(confirmed.get("user_profile") or ""),
            consider_mrr=bool(confirmed.get("consider_mrr")),
            categories=list(confirmed.get("categories") or []),
            is_licensed=licensed,
            no_telemetry=no_telemetry,
        )
    except Exception:
        return


def _run_licensed(confirmed: dict, dashboard: AgentDashboard | None, run_id: str):
    """One licensed scan. Apple RSS, iTunes Lookup, and Apify are not called."""
    from modules.pipeline import Discovery, empty_stats

    del run_id
    credentials = load_credentials()
    assert_same_machine(credentials)
    client = ScoutClient(credentials)
    if dashboard is not None:
        dashboard.focus(2, "Requesting licensed discovery...")
        dashboard.set_scout("Requesting licensed discovery")
    payload = client.scan(confirmed)
    leads = leads_from_payload(payload)
    target = int(confirmed.get("target_prospects") or len(leads) or 0)
    if dashboard is not None:
        dashboard.add_funnel(raw_scanned=len(leads), candidates=len(leads), emails_found=len(leads))
        dashboard.set_qualified(len(leads))
        dashboard.complete(2, f"Scanned {len(leads)} apps → {len(leads)} candidates")
        dashboard.complete(3, f"Extracting contacts [{len(leads)}/{target} qualified]")
        dashboard.set_scout("Scouting complete")
    mode = revenue_mode_of(confirmed)
    if mode == "tracker":
        if dashboard is not None:
            dashboard.focus(4, "Requesting Estimated MRR...")
            dashboard.set_analyst("Requesting Estimated MRR")
        apps = [
            {
                "app_store_id": lead.app_store_id,
                "app_name": lead.app_name,
                "app_store_url": lead.app_store_url,
            }
            for lead in leads
        ]
        try:
            apply_mrr_rows(leads, client.fetch_mrr(apps))
        except LicenseError as exc:
            if exc.status != 402:
                raise
            print(f"\n{exc}")
            print(f"Upgrade: {checkout_url()}")
            if dashboard is not None:
                dashboard.complete(4, "Monthly MRR quota reached")
        else:
            if dashboard is not None:
                dashboard.set_analysis(len(leads), len(leads))
                dashboard.complete(4, f"Analyzed {len(leads)} leads")
                dashboard.set_analyst(f"Analysis complete [{len(leads)}/{len(leads)}]")
    elif mode in {"gross_sales", "no_iap"}:
        from modules.mrr_enricher import _assign_local

        for lead in leads:
            _assign_local(lead, mode, "")
        if dashboard is not None:
            dashboard.set_analysis(len(leads), len(leads))
            dashboard.complete(4, f"Analyzed {len(leads)} leads")
            dashboard.set_analyst(f"Analysis complete [{len(leads)}/{len(leads)}]")
    else:
        for lead in leads:
            if lead.mrr_status == "pending":
                lead.mrr_status = "skipped"
        if dashboard is not None:
            dashboard.set_analyst("Revenue analysis skipped")
            dashboard.complete(4, "Skipped revenue analysis")
    stats = empty_stats()
    stats["discovered"] = len(leads)
    stats["qualified"] = len(leads)
    return Discovery(
        qualified=leads,
        stats=stats,
        template_id=str(confirmed.get("template_id") or "intro"),
        target=target,
    )


def _scrape_public(settings, prompter, templates, targeting, board: AgentDashboard, *, no_telemetry: bool) -> int:
    """Licensed discovery written to a timestamped CSV. The Google Sheet is not opened."""
    crm_summary = {"status": "CSV export", "existing_leads": 0}
    confirmed = run_wizard(prompter, targeting, settings, templates, crm_summary, internal=False)
    if confirmed is None:
        print("Cancelled. Nothing was scraped.")
        return 130
    _emit_scan(confirmed, licensed=True, no_telemetry=no_telemetry)
    board.target = int(confirmed["target_prospects"])
    panel = board if board.enabled else None
    if panel is not None:
        panel.start()
        panel.focus(1, "Preparing a local CSV export...")
        panel.complete(1, "CSV export. No Google Sheet")
    try:
        discovery = _run_licensed(confirmed, panel, "licensed")
    except LicenseError as exc:
        if panel is not None:
            panel.stop(summary=False)
        print(f"\n{exc}")
        if exc.status in {401, 402}:
            print(f"Upgrade: {checkout_url()}")
        return 1
    path = deals_export_path()
    write_export_csv(discovery.qualified, path)
    if panel is not None:
        panel.focus(5, "Writing the CSV...")
        panel.remember_save(discovery.qualified)
        panel.saved = len(discovery.qualified)
        panel.high_mrr = sum(1 for lead in discovery.qualified if is_high_mrr(getattr(lead, "estimated_mrr", "")))
        noun = "lead" if len(discovery.qualified) == 1 else "leads"
        panel.complete(5, f"Wrote {len(discovery.qualified)} {noun} to {path.name}")
        panel.stop(summary=True)
    print(f"Exported {path}")
    print(SHEETS_IMPORT_TIP)
    return 0


def _apply_saved_layout(crm) -> None:
    """Refresh header, status colors, and column widths. Lead cells stay as they are."""
    worksheet = getattr(crm, "worksheet", None)
    headers = getattr(crm, "headers", None)
    if worksheet is None or not headers:
        return
    try:
        apply_crm_layout(worksheet, list(headers), header_row=int(getattr(crm, "header_row", 1) or 1))
    except Exception as exc:
        print(f"Sheet formatting could not be applied: {exc}")


def _new_dashboard(settings: dict, targeting: dict, *, demo: bool) -> AgentDashboard:
    target = int((targeting or {}).get("target_prospects") or 0)
    return AgentDashboard(target, sheet=sheet_url(str(settings.get("spreadsheet_id") or "")), demo=demo)


def _selected_template(templates: list[dict], targeting: dict) -> dict | None:
    wanted = str((targeting or {}).get("template_id") or "intro")
    for item in templates or []:
        if item.get("id") == wanted:
            return item
    return templates[0] if templates else None


def _copy_mrr(originals: list, analyzed: list) -> None:
    by_id = {lead.dedup_id(): lead for lead in analyzed}
    for lead in originals:
        fresh = by_id.get(lead.dedup_id())
        if fresh is None:
            continue
        lead.estimated_mrr = fresh.estimated_mrr
        lead.mrr_numeric = fresh.mrr_numeric
        lead.mrr_status = fresh.mrr_status


def _scout_then_analyze(
    targeting,
    settings,
    scoring,
    templates,
    crm_index,
    *,
    searcher,
    enricher,
    demo: bool = False,
    dashboard,
    crm,
    prompter=None,
    narrator=None,
    prior=None,
    on_raw=None,
    itunes_fetcher=None,
    run_id: str = "demo",
):
    """Save qualified leads first. Revenue enrichment starts only after that list is persisted."""
    voice = narrator or Narrator()
    log_path = project_path("logs") / "runs" / f"{run_id}.log"
    # File only. A stderr handler paints over the Rich loading panel.
    run_log = RunLog(run_id, path=None if demo else log_path, console=False)
    store = RunStore(run_id, directory=None if demo else project_path("data/runs"))
    saved_rows: dict[str, int] = {}
    template = _selected_template(templates, targeting)
    suppressed = suppressed_addresses(settings)

    def on_saved(lead) -> None:
        store.save(lead)
        if crm is None:
            return
        row_number = crm.append_one(lead, template, suppressed)
        if row_number:
            saved_rows[lead.dedup_id()] = row_number

    def on_enriched(lead) -> None:
        if crm is None or lead.mrr_status != "enriched" or not (lead.estimated_mrr or "").strip():
            return
        row_number = saved_rows.get(lead.dedup_id())
        if not row_number:
            run_log.event(
                "ANALYZING",
                "crm_update_failed",
                level="ERROR",
                lead_id=lead.dedup_id(),
                app_id=lead.app_store_id,
                error="no sheet row was recorded for this lead",
            )
            return
        crm.update_estimated_mrr(row_number, lead.app_store_url, lead.estimated_mrr)

    discovery = discover(
        targeting,
        settings,
        scoring,
        crm_index,
        searcher=searcher,
        enricher=enricher,
        prior=prior,
        on_raw=on_raw,
        prompter=prompter,
        itunes_fetcher=itunes_fetcher,
        narrator=voice,
        demo=demo,
        dashboard=dashboard,
        on_saved=on_saved,
        run_log=run_log,
    )
    analyzed = analyze_saved(
        store,
        demo=demo,
        dashboard=dashboard,
        narrator=voice,
        run_log=run_log,
        on_enriched=on_enriched if crm is not None else None,
        enabled=revenue_mode_of(targeting) != "skip",
        revenue_mode=revenue_mode_of(targeting),
    )
    _copy_mrr(discovery.qualified, analyzed)
    discovery.stats["saved_during_scout"] = len(saved_rows)
    return discovery


def _review(discovery, crm, templates, settings, prompter, session, session_path, demo: bool = False, dashboard: AgentDashboard | None = None, saved_during_scout: int = 0) -> int:
    if dashboard is not None:
        dashboard.pause()
    decision = review_prospects(prompter, discovery, templates)
    if not decision:
        if saved_during_scout and not demo and crm is not None:
            print(f"{saved_during_scout} lead(s) saved during scouting remain in the CRM.")
        else:
            print("Nothing was imported.")
        if session_path is not None:
            print(f"Results remain in {session_path}. Run the tool again and choose Resume.")
        if dashboard is not None:
            dashboard.remember_save([])
            dashboard.complete(5, "No leads written")
            dashboard.stop(summary=True)
        return 0
    leads = list(decision["leads"])
    target = int(getattr(discovery, "target", 0) or 0)
    if target and len(leads) > target:
        print(f"Keeping the first {target} of {len(leads)} selected prospects.")
        leads = leads[:target]
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
        if dashboard is not None:
            dashboard.resume()
            dashboard.remember_save(leads)
            dashboard.focus(5, "Demo mode is leaving the spreadsheet unchanged...")
            noun = "lead" if len(leads) == 1 else "leads"
            dashboard.complete(5, f"Prepared {len(leads)} {noun}. Spreadsheet unchanged")
            dashboard.stop(summary=True)
        print(f"Demo mode would append {len(leads)} row(s). The spreadsheet was not changed.")
        for lead in leads:
            row = build_import_row(lead, [
                "App Name", "Developer", "Target Email", "Status", "Lead Score"
            ], template, suppressed)
            print("  " + " | ".join(row))
        return 0
    if dashboard is not None:
        dashboard.resume()
        dashboard.focus(5, f"Writing {len(leads)} leads to the CRM...")
    _apply_saved_layout(crm)
    added, skipped = crm.append_leads(leads, template, suppressed)
    print(f"Appended {added} prospect(s). Skipped {skipped} already in the CRM. Existing rows were not changed.")
    if dashboard is not None:
        dashboard.remember_save(leads)
        dashboard.saved = added
        if added == 0:
            dashboard.high_mrr = 0
        else:
            dashboard.high_mrr = sum(1 for lead in leads if is_high_mrr(getattr(lead, "estimated_mrr", "")))
        noun = "lead" if added == 1 else "leads"
        dashboard.complete(5, f"Wrote {added} {noun}")
        dashboard.stop(summary=True)
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
    apply_crm_layout(crm.worksheet, crm.headers, crm.header_row)
    _index, count, _records = crm.load_rows()
    print(f"Connected to {crm.spreadsheet.title!r} / {crm.worksheet.title!r}.")
    print(f"Existing leads: {count}.")
    print(f"Columns: {', '.join(crm.headers)}")
    if getattr(crm, "migrated", False):
        print("CRM layout applied. Existing rows were mapped into the current schema.")
    else:
        print("CRM layout applied. Existing lead rows were not changed.")
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
        description="Discover mobile apps for an ICP and export them."
    )
    parser.add_argument("--internal", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed-demo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Play cached leads for one category and write a CSV. No network calls.",
    )
    parser.add_argument(
        "--buy",
        action="store_true",
        help="Open the checkout page.",
    )
    parser.add_argument(
        "--activate",
        metavar="LICENSE_KEY",
        help="Bind a license key to this machine. The server activation limit is 1.",
    )
    parser.add_argument(
        "--no-telemetry",
        action="store_true",
        help="Do not send anonymous usage events. DO_NOT_TRACK=1 does the same.",
    )
    parser.add_argument(
        "--format-sheet",
        action="store_true",
        help="Apply the CRM schema, status dropdown, and row colors.",
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
