"""Run hard filters, enrichment, and scoring after discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import inspect

from modules.activity import Narrator
from modules.agent_ui import AgentDashboard, is_high_mrr
from modules.dates import utc_today
from modules.deduplication import DedupIndex, index_url, name_key
from modules.enrichment import classify_email
from modules.errors import ApifyError
from modules.models import Lead
from modules.mrr_enricher import enrich_estimated_mrr
from modules.scraper import (
    ApifyInterrupted,
    discovery_jobs,
    fill_from_itunes,
    normalize_app,
    search_attempt_limit,
)
from modules.scoring import apply_score, classify_developer, hard_filter_reason, qualification_failure, selected_developer_types


@dataclass
class Discovery:
    qualified: list[Lead] = field(default_factory=list)
    missing_email: list[Lead] = field(default_factory=list)
    missing_site: list[Lead] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    template_id: str = "intro"
    target: int = 0


def empty_stats() -> dict:
    return {
        "discovered": 0,
        "already_in_crm": 0,
        "filtered_out": 0,
        "filtered_hard": 0,
        "filtered_score": 0,
        "missing_developer_site": 0,
        "missing_public_email": 0,
        "qualified": 0,
        "enrichment_errors": 0,
        "errors": [],
    }


def reclassify_store_emails(lead: Lead) -> None:
    for email in lead.emails:
        if not str(email.evidence).startswith("Google Play listing"):
            continue
        email_type, evidence = classify_email(email.address, "", lead.developer_name, lead.developer_type)
        email.email_type = email_type
        email.evidence = "Google Play listing. " + evidence


def discover(
    targeting: dict,
    settings: dict,
    scoring: dict,
    crm_index: DedupIndex,
    *,
    searcher,
    enricher,
    today: date | None = None,
    prior: list | None = None,
    on_raw=None,
    prompter=None,
    itunes_fetcher=None,
    narrator: Narrator | None = None,
    demo: bool = False,
    dashboard: AgentDashboard | None = None,
) -> Discovery:
    current = today or utc_today()
    stats = empty_stats()
    qualified: list[Lead] = []
    missing_email: list[Lead] = []
    missing_site: list[Lead] = []
    seen: set[str] = set()
    counted_crm: set[str] = set()
    known_ids = set(crm_index.ids)
    known_urls = set(crm_index.urls)
    known_names = set(crm_index.names)
    prior_map = _prior_map(prior)
    target = int(targeting["target_prospects"])
    country = targeting.get("country") or settings["apify"].get("country") or "us"
    minimum_known = int(scoring.get("minimum_known_max") or 0)
    jobs = discovery_jobs(targeting, settings)
    cap = int(settings["apify"]["max_searches_per_run"])
    limit = min(len(jobs), search_attempt_limit(settings))
    voice = narrator or Narrator()
    attempts = 0
    announced = False

    for platform, keyword, page in jobs:
        if len(qualified) >= target:
            _say(voice, dashboard, f"Reached the target of {target} qualified prospects. No further searches will start.")
            break
        if attempts >= limit:
            _say(voice, dashboard, f"Qualified prospects: {len(qualified)}/{target}. Search limit of {limit} reached.")
            break
        if attempts >= cap and not announced:
            remaining = target - len(qualified)
            _say(
                voice,
                dashboard,
                f"Qualified prospects: {len(qualified)}/{target} — expanding search for {remaining} more...",
            )
            announced = True
        attempts += 1
        store = "iOS App Store" if platform == "ios" else "Google Play"
        label = f"Scraping {store} for \"{keyword}\" (Page {page})..."
        if dashboard is not None:
            dashboard.focus(2, label)
        try:
            saved = prior_map.get((platform, keyword, page))
            if saved is not None:
                items = saved
                if dashboard is None:
                    voice.line(f"  Using {len(items)} saved result(s) from the interrupted run.")
            else:
                if dashboard is None:
                    with voice.spin(label):
                        items, meta = _call_searcher(searcher, platform, keyword, country, page)
                else:
                    items, meta = _call_searcher(searcher, platform, keyword, country, page)
                if dashboard is None:
                    if meta.get("cached"):
                        voice.line(
                            f"  Using {len(items)} cached result(s) from the last "
                            f"{settings['apify'].get('cache_hours', 24)} hours."
                        )
                    else:
                        voice.line(f"  Retrieved {len(items)} result(s).")
                if on_raw:
                    on_raw(platform, keyword, items, page)
        except ApifyInterrupted as exc:
            if dashboard is None:
                voice.line(str(exc))
            else:
                dashboard.note(2, str(exc))
            stats["errors"].append(str(exc))
            items = exc.items
            if on_raw and items:
                on_raw(platform, keyword, items, page)
            _consume(
                items, platform, keyword, targeting, settings, scoring, crm_index, seen, counted_crm,
                known_ids, known_urls, known_names,
                stats, qualified, missing_email, missing_site, enricher, current, minimum_known,
                itunes_fetcher, voice, target, demo, dashboard,
            )
            break
        except ApifyError as exc:
            if dashboard is None:
                voice.line(str(exc))
            else:
                dashboard.note(2, str(exc))
                dashboard.pause()
            stats["errors"].append(str(exc))
            if prompter is None:
                continue
            choice = prompter.select(
                "A search failed.",
                [("Continue with the results already retrieved", "continue"), ("Stop", "stop")],
                default="continue",
            )
            if dashboard is not None and choice == "continue":
                dashboard.resume()
            if choice != "continue":
                break
            continue
        _consume(
            items, platform, keyword, targeting, settings, scoring, crm_index, seen, counted_crm,
            known_ids, known_urls, known_names,
            stats, qualified, missing_email, missing_site, enricher, current, minimum_known,
            itunes_fetcher, voice, target, demo, dashboard,
        )
    else:
        if len(qualified) < target:
            _say(voice, dashboard, f"Qualified prospects: {len(qualified)}/{target}. No further keywords or pages are left.")

    _trim_qualified(qualified, stats, target)
    if dashboard is not None:
        unique = max(0, dashboard.funnel["raw_scanned"] - dashboard.funnel["duplicates_skipped"])
        dashboard.complete(2, f"Scanned {dashboard.funnel['raw_scanned']} apps → {unique} unique candidates")
        dashboard.complete(
            3,
            f"Emails {dashboard.funnel['emails_found']} · MRR hits {dashboard.funnel['mrr_hits']}",
        )
        dashboard.complete(4, f"Ranked {len(qualified)} qualified leads")
        dashboard.set_qualified(len(qualified))
    _check_partition(stats)
    return Discovery(
        qualified=qualified,
        missing_email=missing_email,
        missing_site=missing_site,
        stats=stats,
        template_id=targeting.get("template_id") or "intro",
        target=target,
    )


def _prior_map(prior: list | None) -> dict[tuple[str, str, int], list]:
    saved: dict[tuple[str, str, int], list] = {}
    for entry in prior or []:
        if len(entry) >= 4:
            platform, keyword, items, page = entry[0], entry[1], entry[2], entry[3]
        else:
            platform, keyword, items = entry[0], entry[1], entry[2]
            page = 1
        saved[(platform, keyword, int(page or 1))] = items
    return saved


def _call_searcher(searcher, platform: str, keyword: str, country: str, page: int):
    if page != 1 and not _accepts_page(searcher):
        return [], {"cached": False, "usage_usd": 0}
    if _accepts_page(searcher):
        return searcher(platform, keyword, country, page)
    return searcher(platform, keyword, country)


def _accepts_page(searcher) -> bool:
    try:
        signature = inspect.signature(searcher)
    except (TypeError, ValueError):
        return True
    parameters = list(signature.parameters.values())
    if any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters):
        return True
    positional = [
        item
        for item in parameters
        if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    return len(positional) >= 4


def _consume(
    items,
    platform,
    keyword,
    targeting,
    settings,
    scoring,
    crm_index,
    seen,
    counted_crm,
    known_ids,
    known_urls,
    known_names,
    stats,
    qualified,
    missing_email,
    missing_site,
    enricher,
    today,
    minimum_known,
    itunes_fetcher,
    narrator: Narrator,
    target: int,
    demo: bool = False,
    dashboard: AgentDashboard | None = None,
) -> None:
    fresh: list[Lead] = []
    duplicates = 0
    for item in items:
        lead = normalize_app(item, platform, keyword)
        if lead is None:
            continue
        key = lead.dedup_id()
        if key in seen or crm_index.contains_lead(lead):
            duplicates += 1
            if _from_crm(lead, known_ids, known_urls, known_names) and key not in counted_crm:
                counted_crm.add(key)
                seen.add(key)
                stats["discovered"] += 1
                stats["already_in_crm"] += 1
            continue
        seen.add(key)
        crm_index.add_lead(lead)
        stats["discovered"] += 1
        fresh.append(lead)
    candidates: list[Lead] = []
    filtered = 0
    for lead in fresh:
        lead.developer_type = classify_developer(lead, settings)
        reclassify_store_emails(lead)
        reason = hard_filter_reason(lead, targeting, today)
        if reason:
            lead.filter_reason = reason
            stats["filtered_out"] += 1
            stats["filtered_hard"] += 1
            filtered += 1
            continue
        candidates.append(lead)
    if dashboard is not None:
        dashboard.add_funnel(raw_scanned=len(items), duplicates_skipped=duplicates, filtered_out=filtered)
        dashboard.note(
            2,
            f"Filtered {len(items)} raw apps → {len(candidates)} new candidates "
            f"({duplicates} duplicates skipped, {filtered} filtered by criteria)",
        )
    else:
        narrator.line(
            f"🧹 Filtered {len(items)} raw apps → {len(candidates)} new candidates "
            f"({duplicates} duplicates skipped, {filtered} filtered by criteria)..."
        )
    if len(qualified) >= target:
        _release_uncapped(candidates, stats)
        return
    if candidates and platform == "ios":
        fill_from_itunes(candidates, settings, itunes_fetcher)
    window: list[Lead] = []
    pending = list(candidates)
    while pending and len(qualified) < target:
        lead = pending.pop(0)
        index = len(candidates) - len(pending)
        status = f"Crawling developer sites & /privacy pages for emails [{index}/{len(candidates)}]..."
        if dashboard is not None:
            dashboard.focus(3, status)
        try:
            if dashboard is None:
                with narrator.spin(f"🌐 {status}"):
                    enricher(lead)
            else:
                enricher(lead)
        except Exception as exc:
            lead.enrichment_status = "error"
            lead.enrichment_note = str(exc)
            stats["filtered_out"] += 1
            stats["enrichment_errors"] += 1
            stats["errors"].append(f"{lead.app_name}: {exc}")
            if dashboard is None:
                narrator.line(f"Enrichment failed for {lead.app_name}: {exc}")
            continue
        if lead.primary_email and lead.enrichment_status not in {"blocked", "error"}:
            if dashboard is not None:
                dashboard.add_funnel(emails_found=1)
            window.append(lead)
            if len(window) >= target - len(qualified):
                _score_window(
                    window, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
                    enricher, today, minimum_known, narrator, target, demo, dashboard,
                )
                window = []
            continue
        _score_prepared_lead(
            lead, targeting, scoring, stats, qualified, missing_email, missing_site,
            today, minimum_known, narrator, target, dashboard,
        )
    if window and len(qualified) < target:
        _score_window(
            window, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
            enricher, today, minimum_known, narrator, target, demo, dashboard,
        )
    if len(qualified) >= target and pending:
        _release_uncapped(pending, stats)


def _from_crm(lead: Lead, known_ids: set[str], known_urls: set[str], known_names: set[str]) -> bool:
    if f"{lead.platform}:{lead.app_store_id}".lower() in known_ids:
        return True
    indexed = index_url(lead.app_store_url, lead.platform, lead.app_store_id)
    if indexed and indexed in known_urls:
        return True
    named = name_key(lead.app_name, lead.developer_name)
    return bool(named and named in known_names)


def _score_window(
    window, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
    enricher, today, minimum_known, narrator, target, demo, dashboard,
) -> None:
    """MRR, then score only until the target is full. Extras are not scored."""
    del enricher, settings
    if not window:
        return
    if dashboard is not None:
        dashboard.focus(3, "Fetching estimated MRR for qualified leads (concurrently)...")
    else:
        narrator.line("💰 Fetching estimated MRR for qualified leads (concurrently)...")
    enrich_estimated_mrr(window, demo=demo)
    if dashboard is not None:
        dashboard.add_funnel(mrr_hits=sum(1 for lead in window if is_high_mrr(lead.estimated_mrr)))
        dashboard.focus(4, "Applying score weights...")
    else:
        narrator.line("⚡ Scoring & ranking qualified prospects...")
    for lead in window:
        if len(qualified) >= target:
            _release_uncapped([lead], stats)
            continue
        _score_prepared_lead(
            lead, targeting, scoring, stats, qualified, missing_email, missing_site,
            today, minimum_known, narrator, target, dashboard,
        )


def _score_prepared_lead(
    lead, targeting, scoring, stats, qualified, missing_email, missing_site,
    today, minimum_known, narrator: Narrator, target: int,
    dashboard: AgentDashboard | None = None,
) -> None:
    if len(qualified) >= target:
        _release_uncapped([lead], stats)
        return
    chosen = selected_developer_types(targeting)
    icp = lead.developer_type if lead.developer_type in chosen else (chosen[0] if chosen else targeting.get("developer_type"))
    score = apply_score(lead, scoring, today, icp=icp)
    if lead.enrichment_status in {"blocked", "error"}:
        stats["filtered_out"] += 1
        stats["enrichment_errors"] += 1
        stats["errors"].append(f"{lead.app_name}: {lead.enrichment_note}")
        if dashboard is None:
            narrator.line(f"{lead.app_name}: {lead.enrichment_note}")
        return
    failure = qualification_failure(score, int(targeting["min_score"]), minimum_known)
    if failure:
        lead.filter_reason = failure
        stats["filtered_out"] += 1
        stats["filtered_score"] += 1
        return
    if lead.primary_email is None and not lead.developer_website:
        stats["missing_developer_site"] += 1
        missing_site.append(lead)
        return
    if lead.primary_email is None:
        stats["missing_public_email"] += 1
        missing_email.append(lead)
        return
    if len(qualified) >= target:
        _release_uncapped([lead], stats)
        return
    stats["qualified"] += 1
    qualified.append(lead)
    if dashboard is not None:
        dashboard.set_qualified(len(qualified))
        dashboard.note(4, f"Locked {lead.app_name} ({lead.score_normalized}/100)")
    else:
        narrator.line(f"  Qualified {lead.app_name} ({lead.score_normalized}/100)")
        narrator.line(f"[{len(qualified)}/{target} qualified leads found]")


def _release_uncapped(leads: list[Lead], stats: dict) -> None:
    for lead in leads:
        lead.filter_reason = "target reached"
        stats["filtered_out"] += 1


def _trim_qualified(qualified: list[Lead], stats: dict, target: int) -> None:
    if target < 0 or len(qualified) <= target:
        return
    overflow = qualified[target:]
    del qualified[target:]
    stats["qualified"] = len(qualified)
    _release_uncapped(overflow, stats)


def _say(voice: Narrator, dashboard: AgentDashboard | None, message: str) -> None:
    if dashboard is not None:
        dashboard.note(2, message)
        return
    voice.line(message)


def _check_partition(stats: dict) -> None:
    accounted = (
        stats["already_in_crm"]
        + stats["filtered_out"]
        + stats["missing_developer_site"]
        + stats["missing_public_email"]
        + stats["qualified"]
    )
    if accounted != stats["discovered"]:
        message = (
            f"Internal count mismatch: discovered {stats['discovered']} but the outcome buckets add up to {accounted}."
        )
        stats["errors"].append(message)
        print(message)
