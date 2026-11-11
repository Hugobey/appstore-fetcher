"""Run hard filters, enrichment, and scoring after discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from modules.dates import utc_today
from modules.deduplication import DedupIndex
from modules.enrichment import classify_email
from modules.errors import ApifyError
from modules.models import Lead
from modules.scraper import ApifyInterrupted, fill_from_itunes, normalize_app, search_jobs
from modules.scoring import apply_score, classify_developer, hard_filter_reason, qualification_failure


@dataclass
class Discovery:
    qualified: list[Lead] = field(default_factory=list)
    missing_email: list[Lead] = field(default_factory=list)
    missing_site: list[Lead] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    template_id: str = "intro"


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
    prior: list[tuple[str, str, list[dict]]] | None = None,
    on_raw=None,
    prompter=None,
    itunes_fetcher=None,
) -> Discovery:
    current = today or utc_today()
    stats = empty_stats()
    qualified: list[Lead] = []
    missing_email: list[Lead] = []
    missing_site: list[Lead] = []
    seen: set[str] = set()
    counted_crm: set[str] = set()
    prior_map = {(platform, keyword): items for platform, keyword, items in (prior or [])}
    target = int(targeting["target_prospects"])
    country = targeting.get("country") or settings["apify"].get("country") or "us"
    minimum_known = int(scoring.get("minimum_known_max") or 0)

    for platform, keyword in search_jobs(targeting, settings):
        if len(qualified) >= target:
            print(f"Reached the target of {target} qualified prospects. No further searches will start.")
            break
        print(f"Searching {'App Store' if platform == 'ios' else 'Google Play'} for {keyword!r}...")
        try:
            if (platform, keyword) in prior_map:
                items = prior_map[(platform, keyword)]
                print(f"  Using {len(items)} saved result(s) from the interrupted run.")
            else:
                items, meta = searcher(platform, keyword, country)
                if meta.get("cached"):
                    print(f"  Using {len(items)} cached result(s) from the last {settings['apify'].get('cache_hours', 24)} hours.")
                else:
                    print(f"  Retrieved {len(items)} result(s).")
                if on_raw:
                    on_raw(platform, keyword, items)
        except ApifyInterrupted as exc:
            print(exc)
            stats["errors"].append(str(exc))
            items = exc.items
            if on_raw and items:
                on_raw(platform, keyword, items)
            _consume(
                items, platform, keyword, targeting, settings, scoring, crm_index, seen, counted_crm,
                stats, qualified, missing_email, missing_site, enricher, current, minimum_known, itunes_fetcher,
            )
            break
        except ApifyError as exc:
            print(exc)
            stats["errors"].append(str(exc))
            if prompter is None:
                continue
            choice = prompter.select(
                "A search failed.",
                [("Continue with the results already retrieved", "continue"), ("Stop", "stop")],
                default="continue",
            )
            if choice != "continue":
                break
            continue
        _consume(
            items, platform, keyword, targeting, settings, scoring, crm_index, seen, counted_crm,
            stats, qualified, missing_email, missing_site, enricher, current, minimum_known, itunes_fetcher,
        )

    _check_partition(stats)
    return Discovery(
        qualified=qualified,
        missing_email=missing_email,
        missing_site=missing_site,
        stats=stats,
        template_id=targeting.get("template_id") or "intro",
    )


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
    stats,
    qualified,
    missing_email,
    missing_site,
    enricher,
    today,
    minimum_known,
    itunes_fetcher,
) -> None:
    batch: list[Lead] = []
    for item in items:
        lead = normalize_app(item, platform, keyword)
        if lead is None:
            continue
        key = lead.dedup_id()
        if crm_index.contains_lead(lead):
            if key not in counted_crm:
                counted_crm.add(key)
                seen.add(key)
                stats["discovered"] += 1
                stats["already_in_crm"] += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        stats["discovered"] += 1
        batch.append(lead)
    if batch and platform == "ios":
        fill_from_itunes(batch, settings, itunes_fetcher)
    for lead in batch:
        _qualify_one(
            lead, targeting, settings, scoring, stats, qualified, missing_email, missing_site,
            enricher, today, minimum_known,
        )


def _qualify_one(
    lead, targeting, settings, scoring, stats, qualified, missing_email, missing_site, enricher, today, minimum_known,
) -> None:
    lead.developer_type = classify_developer(lead, settings)
    reclassify_store_emails(lead)
    reason = hard_filter_reason(lead, targeting, today)
    if reason:
        lead.filter_reason = reason
        stats["filtered_out"] += 1
        stats["filtered_hard"] += 1
        return
    try:
        enricher(lead)
    except Exception as exc:
        lead.enrichment_status = "error"
        lead.enrichment_note = str(exc)
        stats["filtered_out"] += 1
        stats["enrichment_errors"] += 1
        stats["errors"].append(f"{lead.app_name}: {exc}")
        print(f"Enrichment failed for {lead.app_name}: {exc}")
        return
    score = apply_score(lead, scoring, today, icp=targeting.get("developer_type"))
    if lead.enrichment_status in {"blocked", "error"}:
        stats["filtered_out"] += 1
        stats["enrichment_errors"] += 1
        stats["errors"].append(f"{lead.app_name}: {lead.enrichment_note}")
        print(f"{lead.app_name}: {lead.enrichment_note}")
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
    stats["qualified"] += 1
    qualified.append(lead)
    print(f"  Qualified {lead.app_name} ({lead.score_normalized}/100)")


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
