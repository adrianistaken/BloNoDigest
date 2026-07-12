"""Import pipeline orchestration (spec §11).

fetch -> extract -> save raw -> normalize -> validate -> dedupe/upsert ->
categorize -> score -> log ImportRun.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from ..models import Event, EventSource, ImportRun, RawImportedEvent, Region
from .categorize import categorize
from .connectors import get_connector
from .dedupe import upsert_or_dedupe_event
from .normalize import is_valid_event, normalize_event
from .score import score_event

logger = logging.getLogger("curator.ingest")

# Events that ended this long ago are rejected at import rather than stored
ALREADY_ENDED_GRACE = timedelta(days=1)


def import_source(source: EventSource) -> ImportRun:
    run = ImportRun.objects.create(source=source, status=ImportRun.Status.RUNNING)
    region = source.region
    log_lines = [f"fetch started: {source.name} ({source.source_type})"]
    logger.info("Import started for source=%s", source.slug)
    now = timezone.now()
    source.last_fetched_at = now

    created = updated = rejected = errors = 0
    try:
        connector = get_connector(source)
        raw_events = connector.fetch_and_extract()
        run.events_found_count = len(raw_events)
        log_lines.append(f"raw events found: {len(raw_events)}")

        for raw in raw_events:
            raw_record = RawImportedEvent.objects.create(
                import_run=run, source=source, **raw.raw_fields()
            )
            try:
                normalized = normalize_event(raw, source, region)
                valid, reason = is_valid_event(normalized)
                if valid:
                    end = normalized["ends_at"] or normalized["starts_at"]
                    if end < timezone.now() - ALREADY_ENDED_GRACE:
                        valid, reason = False, "event already ended"
                if not valid:
                    raw_record.extraction_status = RawImportedEvent.ExtractionStatus.REJECTED
                    raw_record.error_message = reason
                    raw_record.save(update_fields=["extraction_status", "error_message"])
                    rejected += 1
                    continue

                action, event = upsert_or_dedupe_event(normalized, source, raw)
                event.categories = categorize(
                    event.canonical_title,
                    event.description,
                    price_text=event.price_text,
                    price_min=event.price_min,
                    starts_at=event.starts_at if event.time_is_known else None,
                    tags=event.tags,
                )
                event.quality_score = score_event(event, source_link_count=event.source_links.count())
                event.save(update_fields=["categories", "quality_score", "updated_at"])

                raw_record.extraction_status = RawImportedEvent.ExtractionStatus.NORMALIZED
                raw_record.save(update_fields=["extraction_status"])
                if action in ("created", "flagged"):
                    created += 1
                    if action == "flagged":
                        log_lines.append(f"duplicate candidate: {event.canonical_title!r} ~ event #{event.duplicate_of_id}")
                else:
                    updated += 1
            except Exception as exc:  # one bad event must not sink the run
                errors += 1
                raw_record.extraction_status = RawImportedEvent.ExtractionStatus.ERROR
                raw_record.error_message = str(exc)[:2000]
                raw_record.save(update_fields=["extraction_status", "error_message"])
                log_lines.append(f"event error: {exc}")

        run.status = (
            ImportRun.Status.PARTIAL_SUCCESS if errors else ImportRun.Status.SUCCESS
        )
        source.last_success_at = timezone.now()
        source.last_error_message = ""
    except Exception as exc:
        run.status = ImportRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        source.last_error_at = timezone.now()
        source.last_error_message = str(exc)[:2000]
        log_lines.append(f"fetch failed: {exc}")
        logger.exception("Import failed for source=%s", source.slug)
    finally:
        run.events_created_count = created
        run.events_updated_count = updated
        run.events_rejected_count = rejected + errors
        run.finished_at = timezone.now()
        log_lines.append(
            f"done: created={created} updated={updated} rejected={rejected} errors={errors}"
        )
        run.raw_log = "\n".join(log_lines)
        run.save()
        source.save()
    logger.info(
        "Import finished for source=%s status=%s created=%d updated=%d rejected=%d",
        source.slug, run.status, created, updated, rejected + errors,
    )
    return run


def mark_expired_events(region: Region) -> int:
    """Events whose calendar date has fully passed stop being digest candidates."""
    tz = ZoneInfo(region.timezone)
    today_start = datetime.combine(timezone.now().astimezone(tz).date(), time.min, tzinfo=tz)
    expired = (
        Event.objects.filter(region=region, starts_at__lt=today_start)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__lt=today_start))
        .exclude(status__in=[Event.Status.REJECTED, Event.Status.EXPIRED])
    )
    return expired.update(status=Event.Status.EXPIRED, approved_for_digest=False)


def import_region_events(region_slug: str) -> list[ImportRun]:
    region = Region.objects.get(slug=region_slug)
    sources = region.sources.filter(enabled=True).exclude(
        source_type=EventSource.SourceType.MANUAL
    )
    runs = [import_source(source) for source in sources]
    expired_count = mark_expired_events(region)
    logger.info("Region %s: %d sources imported, %d events expired", region_slug, len(runs), expired_count)
    return runs
