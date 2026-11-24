"""Run hard filters, enrichment, and scoring after discovery."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

import inspect

from modules.activity import Narrator
from modules.agent_ui import AgentDashboard, is_high_mrr
from modules.dates import utc_today
from modules.deduplication import DedupIndex, index_url, name_key
from modules.discovery import buy_signal
from modules.enrichment import classify_email, crawl_leads
from modules.errors import ApifyError
from modules.models import Lead
from modules.enricher import filter_by_mrr_ranges, normalize_mrr_ranges
from modules.mrr_enricher import MrrOutcome, apply_mrr_outcome, tracker_url_for_lead
from modules.scraper import (
    ENGLISH_STOREFRONTS,
    ApifyInterrupted,
    discovery_jobs,
    category_phrases,
    fill_from_itunes,
    keyword_plan,
    long_tail_keywords,
    normalize_app,
    storefronts_for,
    synonym_keywords,
)
from modules.scoring import (
    annotate_portfolio,
    apply_score,
    bayesian_score,
    classify_developer,
    hard_filter_reason,
    qualification_failure,
    selected_developer_types,
)


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
        "rejections": {
            "no_website": 0,
            "no_email_found": 0,
            "email_filtered_out": 0,
            "icp_mismatch": 0,
            "mrr_exceeded": 0,
            "ratings_above_cap": 0,
            "ratings_below_min": 0,
            "rating_floor": 0,
            "low_rating": 0,
            "pricing": 0,
            "corporate": 0,
            "portfolio": 0,
            "update_window": 0,
        },
    }


_UPDATE_LADDER = ("30d", "60d", "90d", "180d", "365d", "any")
CRAWL_WAVE = 18
RELAX_PASS_BUDGET = 4
RELAX_SEARCH_BUDGET = 8
_RUN_LOG: contextvars.ContextVar = contextvars.ContextVar("prospecting_run_log", default=None)
_QUALIFIED_BASE: contextvars.ContextVar = contextvars.ContextVar("prospecting_qualified_base", default=0)
_MRR_STATUSES = ("pending", "enriched", "unavailable", "rate_limited", "timeout", "error")
_KEEP_LEAD = {
    "unavailable": "No public MRR listed — keeping lead",
    "rate_limited": "Revenue lookup was rate limited — keeping lead",
    "timeout": "Revenue lookup timed out — keeping lead",
    "error": "Revenue lookup failed — keeping lead",
}


def _emit(event: str, *, level: str = "INFO", lead: Lead | None = None, **fields) -> None:
    log = _RUN_LOG.get()
    if log is None:
        return
    lead_id = lead.dedup_id() if lead is not None else fields.pop("lead_id", None)
    app_id = str(lead.app_store_id) if lead is not None and lead.app_store_id else fields.pop("app_id", None)
    log.event("SCOUTING", event, level=level, lead_id=lead_id, app_id=app_id, **fields)


def _wider_update(current: str) -> str | None:
    if current == "any":
        return None
    if current not in _UPDATE_LADDER:
        return "180d"
    index = _UPDATE_LADDER.index(current)
    if index + 1 >= len(_UPDATE_LADDER):
        return None
    return _UPDATE_LADDER[index + 1]


def _deferral_bucket(reason: str) -> str | None:
    text = reason or ""
    if "ratings is above" in text:
        return "ratings"
    if text.startswith("updated ") and "window" in text:
        return "update"
    return None


def _count_rejection(stats: dict, reason: str) -> None:
    text = reason or ""
    if text.startswith("ICP") or "large publisher" in text:
        key = "icp_mismatch"
    elif "ratings is above" in text:
        key = "ratings_above_cap"
    elif "rating floor" in text:
        key = "rating_floor"
    elif "below the minimum" in text:
        key = "ratings_below_min"
    elif "average rating" in text and "below" in text:
        key = "low_rating"
    elif (
        "freemium" in text
        or "upfront-paid" in text
        or "ad-supported" in text
        or text == "no in-app purchases"
    ):
        key = "pricing"
    elif "legacy corporation" in text:
        key = "corporate"
    elif "portfolio cap" in text:
        key = "portfolio"
    elif text.startswith("updated ") and "window" in text:
        key = "update_window"
    elif text.startswith("Estimated MRR"):
        key = "mrr_exceeded"
    else:
        return
    stats["rejections"][key] = int(stats["rejections"].get(key) or 0) + 1


def _count_contact_gap(stats: dict, lead: Lead) -> None:
    if not lead.developer_website and not lead.support_url and not lead.emails:
        key = "no_website"
    elif int(getattr(lead, "rejected_email_count", 0) or 0) > 0:
        key = "email_filtered_out"
    else:
        key = "no_email_found"
    stats["rejections"][key] = int(stats["rejections"].get(key) or 0) + 1


def _partition_deferred(leads: list[Lead], targeting: dict, today: date):
    revived: list[Lead] = []
    updates: list[Lead] = []
    ratings: list[Lead] = []
    stuck: list[Lead] = []
    for lead in leads:
        reason = hard_filter_reason(lead, targeting, today)
        if not reason:
            lead.filter_reason = ""
            revived.append(lead)
            continue
        lead.filter_reason = reason
        bucket = _deferral_bucket(reason)
        if bucket == "update":
            updates.append(lead)
        elif bucket == "ratings":
            ratings.append(lead)
        else:
            stuck.append(lead)
    return revived, updates, ratings, stuck


def reclassify_store_emails(lead: Lead) -> None:
    for email in lead.emails:
        evidence = str(email.evidence)
        if evidence.startswith("Google Play listing"):
            prefix = "Google Play listing. "
        elif evidence.startswith("App Store listing"):
            prefix = "App Store listing. "
        else:
            continue
        email_type, detail = classify_email(email.address, "", lead.developer_name, lead.developer_type)
        email.email_type = email_type
        email.evidence = prefix + detail


def follow_up_terms(targeting: dict, settings: dict, used: list[str] | None = None) -> list[str]:
    """Phrases and long-tail queries that this run has not searched yet."""
    phrases = category_phrases(targeting, settings)
    tails = long_tail_keywords(phrases, list(targeting.get("categories") or []))
    seen = {str(item).casefold() for item in (used or [])}
    ordered: list[str] = []
    for term in phrases + tails:
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        ordered.append(term)
    return ordered


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
    mrr_getter=None,
    on_saved=None,
    run_log=None,
    follow_up_terms_for_round: list[str] | None = None,
    qualified_base: int = 0,
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
    country = str(targeting.get("country") or settings["apify"].get("country") or "us").strip().lower()
    minimum_known = int(scoring.get("minimum_known_max") or 0)
    if follow_up_terms_for_round is None:
        jobs = [
            (platform, keyword, page, front)
            for platform, keyword, page in discovery_jobs(targeting, settings)
            for front in storefronts_for(country)
        ]
    else:
        jobs = []
    cap = int(settings["apify"]["max_searches_per_run"])
    # Category RSS is free, so the Apify search cap does not stop the chart list.
    # `cap` still marks when the run has moved past the first planned wave.
    limit = len(jobs)
    deferred_update: list[Lead] = []
    deferred_ratings: list[Lead] = []
    searched_fronts: set[str] = set()
    used_keywords: list[str] = []
    seen_queries: set[tuple[str, str, str]] = set()
    network_ok = True
    relax_budget = {"used": 0, "announced": False}
    voice = narrator or Narrator()
    stop_reason = "sources_exhausted"
    attempts = 0
    announced = False
    index = 0
    started: set[int] = set()
    prefetch_future = None
    prefetch_index = None
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apify-prefetch")

    def _on_qualified(lead: Lead) -> None:
        """A verified email is a saved lead. Revenue is not consulted here."""
        lead.mrr_status = "pending"
        email = lead.primary_email.address if lead.primary_email else ""
        _emit("lead_accepted", lead=lead, email=email)
        if on_saved is None:
            _emit("lead_saved", lead=lead, persisted=False)
            return
        try:
            on_saved(lead)
        except Exception as exc:
            message = f"CRM save failed for {lead.app_name}: {exc}"
            stats["errors"].append(message)
            _emit("lead_save_failed", lead=lead, level="ERROR", error=str(exc))
            if dashboard is None:
                voice.line(message)
            return
        _emit("lead_saved", lead=lead, persisted=True)

    def _announce_expansion() -> None:
        nonlocal announced
        if announced or attempts < cap:
            return
        remaining = target - len(qualified)
        _say(
            voice,
            dashboard,
            f"Qualified prospects: {len(qualified)}/{target} — expanding search for {remaining} more...",
        )
        announced = True

    def _begin(job_index: int) -> bool:
        nonlocal attempts
        if job_index in started:
            return True
        if job_index < 0 or job_index >= len(jobs) or attempts >= limit or len(qualified) >= target:
            return False
        _announce_expansion()
        attempts += 1
        started.add(job_index)
        return True

    def launch_prefetch(hopeful: int) -> None:
        nonlocal prefetch_future, prefetch_index
        if prefetch_future is not None:
            return
        if len(qualified) + max(0, hopeful) >= target:
            return
        nxt = index + 1
        if nxt < len(jobs):
            platform, keyword, page, front = jobs[nxt]
        else:
            platform, keyword, page, front = "", "", 1, country
        if nxt >= len(jobs) or (platform, keyword, page) in prior_map:
            return
        if not _begin(nxt):
            return
        prefetch_index = nxt

        def _run(platform=platform, keyword=keyword, page=page, front=front):
            return _call_searcher(searcher, platform, keyword, front, page)

        prefetch_future = pool.submit(_run)

    def _report_fresh(platform: str, keyword: str, items: list, page: int, meta: dict) -> None:
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

    def _reject_stuck(leads: list[Lead]) -> None:
        for lead in leads:
            reason = lead.filter_reason or "filtered during discovery"
            lead.filter_reason = reason
            stats["filtered_out"] += 1
            stats["filtered_hard"] += 1
            _count_rejection(stats, reason)
            _emit("lead_rejected", lead=lead, reason=reason)

    def _flush_deferred() -> None:
        leftover = list(deferred_update) + list(deferred_ratings)
        deferred_update.clear()
        deferred_ratings.clear()
        _reject_stuck(leftover)

    def _reprocess(leads: list[Lead], active: dict) -> None:
        if not leads or len(qualified) >= target:
            return
        _crawl_and_score(
            leads, active, settings, scoring, stats, qualified, missing_email, missing_site,
            enricher, current, minimum_known, itunes_fetcher, voice, target, demo, dashboard,
            None, mrr_getter, None, _on_qualified,
        )

    def _replay(bucket: list[Lead], active: dict) -> list[Lead]:
        revived, updates, ratings, stuck = _partition_deferred(bucket, active, current)
        _reject_stuck(stuck)
        deferred_update.extend(updates)
        deferred_ratings.extend(ratings)
        if revived:
            _reprocess(revived, active)
        return updates if bucket is deferred_update else ratings

    def _widen_updates(active: dict) -> None:
        while len(qualified) < target:
            nxt = _wider_update(str(active.get("last_update") or ""))
            if not nxt:
                return
            active["last_update"] = nxt
            if not deferred_update:
                return
            pending = list(deferred_update)
            deferred_update.clear()
            _replay(pending, active)
            if not deferred_update:
                return

    def _announce_relax() -> None:
        if relax_budget["announced"]:
            return
        relax_budget["announced"] = True
        message = f"Widening storefront & keyword net to hit {target}/{target} target..."
        if dashboard is None:
            voice.line(message)
            return
        active_phase = next((number for number, phase in dashboard.phases.items() if phase["state"] == "active"), None)
        if active_phase == 3:
            dashboard.note(3, message)
        else:
            dashboard.note(2, message)

    def _platforms() -> list[str]:
        choice = targeting.get("platforms")
        if choice == "android":
            return ["android"]
        if choice == "both":
            return ["ios", "android"]
        return ["ios"]

    def _search_relax(keywords: list[str], fronts: list[str], active: dict, ceiling: int) -> None:
        nonlocal network_ok
        for front in fronts:
            for keyword in keywords:
                for platform in _platforms():
                    if len(qualified) >= target or relax_budget["used"] >= ceiling:
                        return
                    key = (platform, keyword.strip().lower(), front)
                    if key in seen_queries:
                        continue
                    seen_queries.add(key)
                    relax_budget["used"] += 1
                    page = 1
                    try:
                        items, _meta = _call_searcher(searcher, platform, keyword, front, page)
                    except ApifyInterrupted as exc:
                        network_ok = False
                        items = exc.items
                        if on_raw and items:
                            on_raw(platform, keyword, items, page)
                        _consume(
                            items, platform, keyword, active, settings, scoring, crm_index, seen, counted_crm,
                            known_ids, known_urls, known_names,
                            stats, qualified, missing_email, missing_site, enricher, current, minimum_known,
                            itunes_fetcher, voice, target, demo, dashboard, None, mrr_getter,
                            deferred_update, deferred_ratings, None, _on_qualified,
                        )
                        return
                    except ApifyError as exc:
                        stats["errors"].append(str(exc))
                        _emit("discovery_error", level="ERROR", error=str(exc), category=keyword)
                        continue
                    if on_raw and items:
                        on_raw(platform, keyword, items, page)
                    _consume(
                        items, platform, keyword, active, settings, scoring, crm_index, seen, counted_crm,
                        known_ids, known_urls, known_names,
                        stats, qualified, missing_email, missing_site, enricher, current, minimum_known,
                        itunes_fetcher, voice, target, demo, dashboard, None, mrr_getter,
                        deferred_update, deferred_ratings, None, _on_qualified,
                    )

    def _relax() -> None:
        if len(qualified) >= target:
            return
        if follow_up_terms_for_round is not None:
            if not network_ok or not follow_up_terms_for_round:
                return
            _say(
                voice,
                dashboard,
                f"Qualified prospects: {qualified_base + len(qualified)}/{qualified_base + target}. "
                "Searching another round...",
            )
            fronts = storefronts_for(country)[:1] or [country]
            _search_relax(
                list(follow_up_terms_for_round),
                fronts,
                dict(targeting),
                len(follow_up_terms_for_round),
            )
            return
        if not deferred_update and not deferred_ratings and not network_ok:
            return
        _announce_relax()
        active = dict(targeting)
        _widen_updates(active)
        if network_ok and len(qualified) < target:
            extra_fronts = [front for front in ENGLISH_STOREFRONTS if front not in searched_fronts]
            words = used_keywords[:6] or keyword_plan(targeting, settings)[:6]
            _search_relax(words, extra_fronts, active, RELAX_PASS_BUDGET)
        if network_ok and len(qualified) < target:
            phrases = category_phrases(targeting, settings)
            if not phrases:
                base_words = used_keywords or keyword_plan(targeting, settings)
                phrases = synonym_keywords(base_words)
            fronts = storefronts_for(country)[:1] or [country]
            # Chart widening may already have spent RELAX_PASS_BUDGET. Phrases get their own room.
            # The rating cap stays at the number the user set.
            _search_relax(phrases, fronts, dict(targeting), RELAX_PASS_BUDGET + RELAX_SEARCH_BUDGET)

    def _finish() -> None:
        _flush_deferred()
        _trim_qualified(qualified, stats, target)
        if dashboard is not None:
            dashboard.rejections = dict(stats.get("rejections") or {})
            scanned = dashboard.funnel["raw_scanned"]
            candidates = dashboard.funnel["candidates"]
            dashboard.complete(2, f"Scanned {scanned} apps → {candidates} candidates")
            dashboard.complete(3, f"Extracting contacts [{len(qualified)}/{target} qualified]")
            dashboard.set_qualified(qualified_base + len(qualified))
            dashboard.set_scout("Scouting complete")
            dashboard.set_analyst("Waiting to analyze revenue")
        stats["searched_terms"] = [keyword for _platform, keyword, _front in seen_queries]
        _emit(
            "scouting_completed",
            target=target,
            qualified=len(qualified),
            discovered=stats["discovered"],
            already_in_crm=stats["already_in_crm"],
            filtered_out=stats["filtered_out"],
            missing_public_email=stats["missing_public_email"],
            missing_developer_site=stats["missing_developer_site"],
            stop_reason=stop_reason,
        )
        _check_partition(stats)

    log_token = _RUN_LOG.set(run_log)
    base_token = _QUALIFIED_BASE.set(qualified_base)
    try:
        _emit("scouting_started", target=target, country=country)
        if mrr_getter is not None:
            _emit("revenue_provider_skipped", error="scouting does not call the revenue provider")
        if targeting.get("max_mrr") is not None:
            _emit(
                "mrr_ceiling_ignored",
                max_mrr=targeting.get("max_mrr"),
                error="max_mrr is stored and is not a qualification filter",
            )
        if dashboard is not None:
            dashboard.begin_clock()
            dashboard.set_scout("Scanning category feeds")
            dashboard.set_analyst("Waiting until scouting finishes")
        while index < len(jobs):
            if len(qualified) >= target:
                stop_reason = "target_reached"
                _say(voice, dashboard, f"Reached the target of {target} qualified prospects. No further searches will start.")
                break
            if index not in started and attempts >= limit:
                _say(voice, dashboard, f"Qualified prospects: {len(qualified)}/{target}. Search limit of {limit} reached.")
                break
            if not _begin(index):
                _say(voice, dashboard, f"Qualified prospects: {len(qualified)}/{target}. Search limit of {limit} reached.")
                break
            platform, keyword, page, front = jobs[index]
            searched_fronts.add(front)
            if keyword not in used_keywords:
                used_keywords.append(keyword)
            seen_queries.add((platform, keyword.strip().lower(), front))
            chart = "top free" if page == 1 else "top paid"
            label = f"Pulling {chart} {keyword} ({front})..."
            if dashboard is not None:
                dashboard.focus(2, label)
            use_prefetch = prefetch_future is not None and prefetch_index == index
            try:
                saved = prior_map.get((platform, keyword, page))
                if use_prefetch:
                    try:
                        items, meta = prefetch_future.result()
                    finally:
                        prefetch_future = None
                        prefetch_index = None
                    _report_fresh(platform, keyword, items, page, meta)
                elif saved is not None:
                    items = saved
                    if dashboard is None:
                        voice.line(f"  Using {len(items)} saved result(s) from the interrupted run.")
                else:
                    if dashboard is None:
                        with voice.spin(label):
                            items, meta = _call_searcher(searcher, platform, keyword, front, page)
                    else:
                        items, meta = _call_searcher(searcher, platform, keyword, front, page)
                    _report_fresh(platform, keyword, items, page, meta)
            except ApifyInterrupted as exc:
                network_ok = False
                prefetch_future = None
                prefetch_index = None
                if dashboard is None:
                    voice.line(str(exc))
                else:
                    dashboard.note(2, str(exc))
                stats["errors"].append(str(exc))
                stop_reason = "interrupted"
                _emit("discovery_error", level="ERROR", error=str(exc))
                items = exc.items
                if on_raw and items:
                    on_raw(platform, keyword, items, page)
                _consume(
                    items, platform, keyword, targeting, settings, scoring, crm_index, seen, counted_crm,
                    known_ids, known_urls, known_names,
                    stats, qualified, missing_email, missing_site, enricher, current, minimum_known,
                    itunes_fetcher, voice, target, demo, dashboard, launch_prefetch, mrr_getter,
                    deferred_update, deferred_ratings, None, _on_qualified,
                )
                break
            except ApifyError as exc:
                prefetch_future = None
                prefetch_index = None
                if dashboard is None:
                    voice.line(str(exc))
                else:
                    dashboard.note(2, str(exc))
                    dashboard.pause()
                stats["errors"].append(str(exc))
                _emit("discovery_error", level="ERROR", error=str(exc))
                if prompter is None:
                    index += 1
                    continue
                choice = prompter.select(
                    "A search failed.",
                    [("Continue with the results already retrieved", "continue"), ("Stop", "stop")],
                    default="continue",
                )
                if dashboard is not None and choice == "continue":
                    dashboard.resume()
                if choice != "continue":
                    stop_reason = "stopped"
                    break
                index += 1
                continue
            _consume(
                items, platform, keyword, targeting, settings, scoring, crm_index, seen, counted_crm,
                known_ids, known_urls, known_names,
                stats, qualified, missing_email, missing_site, enricher, current, minimum_known,
                itunes_fetcher, voice, target, demo, dashboard, launch_prefetch, mrr_getter,
                deferred_update, deferred_ratings, None, _on_qualified,
            )
            index += 1
        else:
            if len(qualified) < target:
                _say(voice, dashboard, f"Qualified prospects: {len(qualified)}/{target}. No further category charts are left.")
        if len(qualified) < target:
            if prefetch_future is not None:
                prefetch_future.cancel()
                prefetch_future = None
                prefetch_index = None
            _relax()
        if len(qualified) >= target:
            stop_reason = "target_reached"
        elif not network_ok:
            stop_reason = "interrupted"
        elif stop_reason != "stopped":
            if attempts >= limit and len(qualified) < target:
                stop_reason = "search_limit"
            else:
                stop_reason = "sources_exhausted"
    except Exception as exc:
        stop_reason = "discovery_error"
        message = f"Discovery stopped early: {exc}"
        stats["errors"].append(message)
        _emit("discovery_error", level="ERROR", error=str(exc))
        print(message)
    finally:
        pending_prefetch = prefetch_future
        prefetch_future = None
        prefetch_index = None
        if pending_prefetch is not None:
            pending_prefetch.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        try:
            _finish()
        finally:
            _RUN_LOG.reset(log_token)
            _QUALIFIED_BASE.reset(base_token)
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
    on_shortfall=None,
    mrr_getter=None,
    deferred_update: list | None = None,
    deferred_ratings: list | None = None,
    on_full=None,
    on_qualified=None,
) -> None:
    held_update = deferred_update if deferred_update is not None else []
    held_ratings = deferred_ratings if deferred_ratings is not None else []
    fresh: list[Lead] = []
    duplicates = 0
    for item in items:
        lead = normalize_app(item, platform, keyword)
        if lead is None:
            continue
        key = lead.dedup_id()
        if key in seen or crm_index.contains_lead(lead):
            duplicates += 1
            in_crm = _from_crm(lead, known_ids, known_urls, known_names)
            _emit("duplicate_skipped", lead=lead, reason="already_in_crm" if in_crm else "already_in_run")
            if in_crm and key not in counted_crm:
                counted_crm.add(key)
                seen.add(key)
                stats["discovered"] += 1
                stats["already_in_crm"] += 1
            continue
        seen.add(key)
        crm_index.add_lead(lead)
        stats["discovered"] += 1
        fresh.append(lead)
        _emit("app_discovered", lead=lead, app_name=lead.app_name, category=keyword)
    candidates: list[Lead] = []
    filtered = 0
    annotate_portfolio(fresh)
    for lead in fresh:
        lead.developer_type = classify_developer(lead, settings)
        reclassify_store_emails(lead)
        reason = hard_filter_reason(lead, targeting, today)
        if reason:
            lead.filter_reason = reason
            bucket = _deferral_bucket(reason)
            if bucket == "update":
                held_update.append(lead)
                continue
            if bucket == "ratings":
                held_ratings.append(lead)
                continue
            stats["filtered_out"] += 1
            stats["filtered_hard"] += 1
            _count_rejection(stats, reason)
            _emit("lead_rejected", lead=lead, reason=reason)
            filtered += 1
            continue
        candidates.append(lead)
    if dashboard is not None:
        dashboard.add_funnel(
            raw_scanned=len(items),
            duplicates_skipped=duplicates,
            filtered_out=filtered,
            candidates=len(candidates),
        )
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
        if on_full is not None:
            on_full()
        if len(qualified) >= target:
            _release_uncapped(candidates, stats)
            return
    if len(qualified) + len(candidates) < target and on_shortfall is not None:
        on_shortfall(len(candidates))
    _crawl_and_score(
        candidates, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
        enricher, today, minimum_known, itunes_fetcher, narrator, target, demo, dashboard,
        on_shortfall, mrr_getter, on_full, on_qualified,
    )


def _crawl_and_score(
    candidates, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
    enricher, today, minimum_known, itunes_fetcher, narrator, target, demo, dashboard,
    on_shortfall, mrr_getter, on_full=None, on_qualified=None,
) -> None:
    """Crawl a wave at a time and save each qualified contact. Revenue waits until scouting finishes."""

    def _locked() -> bool:
        if len(qualified) < target:
            return False
        if on_full is not None:
            on_full()
        return len(qualified) >= target
    if not candidates:
        return
    ios = [lead for lead in candidates if lead.platform == "ios"]
    if ios:
        fill_from_itunes(ios, settings, itunes_fetcher)
    still_open: list[Lead] = []
    for lead in candidates:
        cap = targeting.get("max_ratings")
        if cap not in (None, "any") and lead.rating_count is not None and lead.rating_count > int(cap):
            reason = f"{lead.rating_count} ratings is above the maximum of {cap}"
            lead.filter_reason = reason
            stats["filtered_out"] += 1
            stats["filtered_hard"] += 1
            _count_rejection(stats, reason)
            _emit("lead_rejected", lead=lead, reason=reason)
            continue
        still_open.append(lead)
    candidates = still_open
    window: list[Lead] = []
    pending = list(candidates)

    def _guard(lead: Lead) -> None:
        try:
            enricher(lead)
        except Exception as exc:
            lead.enrichment_status = "error"
            lead.enrichment_note = str(exc)

    while pending and len(qualified) < target:
        if on_shortfall is not None and len(qualified) + len(pending) + len(window) < target:
            on_shortfall(len(pending) + len(window))
        slots = max(1, target - len(qualified))
        wave_n = min(CRAWL_WAVE, slots)
        batch = pending[:wave_n]
        del pending[:wave_n]
        status = f"Extracting contacts [{len(qualified)}/{target} qualified]..."
        if dashboard is not None:
            dashboard.focus(3, status)
            crawl_leads(batch, _guard, workers=12)
        else:
            with narrator.spin(f"🌐 {status}"):
                crawl_leads(batch, _guard, workers=12)
        for offset, lead in enumerate(batch, start=1):
            if lead.primary_email and lead.enrichment_status not in {"blocked", "error"}:
                _emit("email_found", lead=lead, email=lead.primary_email.address)
                if dashboard is not None:
                    dashboard.add_funnel(emails_found=1)
                window.append(lead)
                if len(window) >= target - len(qualified):
                    _score_window(
                        window, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
                        enricher, today, minimum_known, narrator, target, demo, dashboard, mrr_getter,
                        on_qualified,
                    )
                    window = []
            else:
                _score_prepared_lead(
                    lead, targeting, scoring, stats, qualified, missing_email, missing_site,
                    today, minimum_known, narrator, target, dashboard, on_qualified,
                )
            if _locked():
                _release_uncapped(batch[offset:], stats)
                _release_uncapped(pending, stats)
                pending.clear()
                break
    if window and len(qualified) < target:
        _score_window(
            window, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
            enricher, today, minimum_known, narrator, target, demo, dashboard, mrr_getter,
            on_qualified,
        )
    if _locked() and pending:
        _release_uncapped(pending, stats)
    elif len(qualified) < target and on_shortfall is not None:
        on_shortfall(len(pending))


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
    enricher, today, minimum_known, narrator, target, demo, dashboard, mrr_getter=None,
    on_qualified=None,
) -> None:
    """Score email-ready leads. Revenue enrichment is a later phase."""
    del enricher, settings, demo, mrr_getter
    if not window:
        return
    if dashboard is None:
        narrator.line("⚡ Scoring & ranking qualified prospects...")
    for lead in window:
        if len(qualified) >= target:
            _release_uncapped([lead], stats)
            continue
        _score_prepared_lead(
            lead, targeting, scoring, stats, qualified, missing_email, missing_site,
            today, minimum_known, narrator, target, dashboard, on_qualified,
        )


def _score_prepared_lead(
    lead, targeting, scoring, stats, qualified, missing_email, missing_site,
    today, minimum_known, narrator: Narrator, target: int,
    dashboard: AgentDashboard | None = None,
    on_qualified=None,
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
        _emit("lead_rejected", lead=lead, level="ERROR", reason=lead.enrichment_note or "enrichment error")
        if dashboard is None:
            narrator.line(f"{lead.app_name}: {lead.enrichment_note}")
        return
    failure = qualification_failure(score, int(targeting["min_score"]), minimum_known)
    if failure:
        lead.filter_reason = failure
        stats["filtered_out"] += 1
        stats["filtered_score"] += 1
        _emit("lead_rejected", lead=lead, reason=failure)
        return
    if lead.primary_email is None and not lead.developer_website and not lead.support_url:
        stats["missing_developer_site"] += 1
        missing_site.append(lead)
        _count_contact_gap(stats, lead)
        _emit("lead_rejected", lead=lead, reason="no developer website")
        return
    if lead.primary_email is None:
        stats["missing_public_email"] += 1
        missing_email.append(lead)
        _count_contact_gap(stats, lead)
        _emit("lead_rejected", lead=lead, reason="no public email")
        return
    if not (lead.contact_name or "").strip():
        developer = (lead.developer_name or "").strip()
        if developer:
            lead.contact_name = developer
        else:
            lead.filter_reason = "no public contact name"
            stats["filtered_out"] += 1
            _emit("lead_rejected", lead=lead, reason="no public contact name")
            return
    if len(qualified) >= target:
        _release_uncapped([lead], stats)
        return
    stats["qualified"] += 1
    lead.buy_signal = buy_signal(lead, today)
    lead.bayesian_score = bayesian_score(lead.average_rating, lead.rating_count)
    qualified.append(lead)
    if dashboard is not None:
        dashboard.set_qualified(_QUALIFIED_BASE.get() + len(qualified))
        dashboard.set_scout("Scanning category feeds")
        dashboard.note(3, f"Extracting contacts [{len(qualified)}/{target} qualified]...")
    else:
        narrator.line(f"  Qualified {lead.app_name} ({lead.score_normalized}/100)")
        narrator.line(f"[{len(qualified)}/{target} qualified leads found]")
    if on_qualified is not None:
        on_qualified(lead)


def _release_uncapped(leads: list[Lead], stats: dict) -> None:
    for lead in leads:
        lead.filter_reason = "target reached"
        stats["filtered_out"] += 1
        _emit("lead_rejected", lead=lead, reason="target reached")


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


def analyze_saved(
    store,
    *,
    demo: bool = False,
    getter=None,
    dashboard: AgentDashboard | None = None,
    narrator: Narrator | None = None,
    run_log=None,
    on_enriched=None,
    pause=None,
    enabled: bool = True,
    revenue_mode: str | None = None,
    mrr_ranges=None,
    only_pending: bool = False,
) -> list[Lead]:
    """Enrich revenue for leads this run already saved.

    This never calls a searcher and never changes how many leads scouting saved.
    One request per lead. A failure is logged and the next lead still runs.
    `enabled=False` skips App Store Tracker and keeps the saved leads.
    When `mrr_ranges` names a subset of the public buckets, the returned list
    keeps leads inside any of those buckets. The store still has every saved lead.
    """
    voice = narrator or Narrator()
    leads = list(store.load())
    if only_pending:
        leads = [lead for lead in leads if (lead.mrr_status or "pending") == "pending"]
    mode = revenue_mode or ("tracker" if enabled else "skip")
    if not enabled or mode == "skip":
        return _skip_revenue_analysis(store, leads, dashboard=dashboard, run_log=run_log)
    total = len(leads)
    counts = {status: 0 for status in _MRR_STATUSES}
    if run_log is not None:
        run_log.event("ANALYZING", "analysis_started", total=total)
    if dashboard is not None:
        dashboard.set_scout("Scouting complete")
        dashboard.set_analysis(0, total)
        dashboard.focus(4, f"Analyzing revenue [0/{total}]")
        dashboard.set_analyst(f"Analyzing revenue [0/{total}]")
    for index, lead in enumerate(leads):
        name = lead.app_name or "App"
        if run_log is not None:
            run_log.event(
                "ANALYZING",
                "enrichment_attempt",
                lead_id=lead.dedup_id(),
                app_id=lead.app_store_id,
                app_name=name,
            )
        if dashboard is not None:
            dashboard.set_analyst(f"Fetching MRR for {name}...")
        previous = lead.estimated_mrr or ""
        try:
            outcome = apply_mrr_outcome(
                lead,
                demo=demo,
                getter=getter,
                pause=pause,
                index=index,
                revenue_mode=mode,
            )
        except Exception as exc:
            outcome = MrrOutcome(
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                endpoint=tracker_url_for_lead(lead),
            )
            lead.mrr_status = "error"
        _log_mrr_outcome(run_log, lead, outcome)
        status_name = lead.mrr_status if lead.mrr_status in counts else "error"
        counts[status_name] += 1
        changed = (lead.estimated_mrr or "") != previous
        if changed and lead.mrr_status == "enriched" and (lead.estimated_mrr or "").strip():
            if dashboard is not None and is_high_mrr(lead.estimated_mrr):
                dashboard.add_funnel(mrr_hits=1)
            if on_enriched is not None:
                try:
                    on_enriched(lead)
                except Exception as exc:
                    message = f"CRM MRR update failed for {name}: {exc}"
                    if run_log is not None:
                        run_log.event(
                            "ANALYZING",
                            "crm_update_failed",
                            level="ERROR",
                            lead_id=lead.dedup_id(),
                            app_id=lead.app_store_id,
                            error=str(exc),
                        )
                    if dashboard is None:
                        voice.line(message)
        elif not previous.strip() or changed:
            message = f"• {name}: {_KEEP_LEAD.get(lead.mrr_status, _KEEP_LEAD['error'])}"
            if dashboard is not None:
                dashboard.push_feed(message)
            else:
                voice.line(message)
        try:
            store.save(lead)
        except Exception as exc:
            if run_log is not None:
                run_log.event(
                    "ANALYZING",
                    "lead_save_failed",
                    level="ERROR",
                    lead_id=lead.dedup_id(),
                    app_id=lead.app_store_id,
                    error=str(exc),
                )
            if dashboard is None:
                voice.line(f"Could not persist enrichment for {name}: {exc}")
        if dashboard is not None:
            done = index + 1
            dashboard.set_analysis(done, total)
            dashboard.note(4, f"Analyzing revenue [{done}/{total}]")
            dashboard.set_analyst(f"Analyzing revenue [{done}/{total}]")
    if dashboard is not None:
        dashboard.complete(4, f"Analyzed {total} leads")
        dashboard.set_analyst(f"Analysis complete [{total}/{total}]")
    if run_log is not None:
        run_log.event("ANALYZING", "analysis_completed", total=total, **counts)
    selected = normalize_mrr_ranges(mrr_ranges)
    if selected:
        leads = filter_by_mrr_ranges(leads, selected)
    return leads


def _skip_revenue_analysis(store, leads: list[Lead], *, dashboard, run_log) -> list[Lead]:
    """Contact-only runs never call App Store Tracker."""
    for lead in leads:
        if lead.mrr_status == "pending":
            lead.mrr_status = "skipped"
        try:
            store.save(lead)
        except Exception as exc:
            if run_log is not None:
                run_log.event(
                    "ANALYZING",
                    "lead_save_failed",
                    level="ERROR",
                    lead_id=lead.dedup_id(),
                    app_id=lead.app_store_id,
                    error=str(exc),
                )
    if run_log is not None:
        run_log.event("ANALYZING", "analysis_skipped", total=len(leads), reason="consider_mrr")
    if dashboard is not None:
        total = len(leads)
        dashboard.set_scout("Scouting complete")
        dashboard.set_analysis(total, total)
        dashboard.set_analyst("Revenue analysis skipped")
        dashboard.focus(4, "Skipping App Store Tracker")
        dashboard.complete(4, "Skipped revenue analysis")
    return leads


def _log_mrr_outcome(run_log, lead: Lead, outcome: MrrOutcome) -> None:
    if run_log is None:
        return
    status_name = outcome.status or lead.mrr_status or "error"
    event_name = {
        "enriched": "enrichment_succeeded",
        "unavailable": "enrichment_unavailable",
        "rate_limited": "enrichment_rate_limited",
        "timeout": "enrichment_timeout",
        "error": "enrichment_error",
    }.get(status_name, "enrichment_error")
    if status_name == "enriched":
        level = "INFO"
    elif status_name == "error":
        level = "ERROR"
    else:
        level = "WARNING"
    run_log.event(
        "ANALYZING",
        event_name,
        level=level,
        lead_id=lead.dedup_id(),
        app_id=lead.app_store_id,
        http_status=outcome.http_status,
        duration=outcome.duration_s,
        error=outcome.error,
        endpoint=outcome.endpoint,
        body=outcome.body,
        mrr_status=status_name,
        estimated_mrr=lead.estimated_mrr or "",
    )
