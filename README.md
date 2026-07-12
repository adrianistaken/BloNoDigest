# Bloomington-Normal Local Events Digest (MVP)

One weekly Thursday email answering "what's happening around Bloomington-Normal this weekend?" —
powered by an automated, region-based event ingestion pipeline with human review.

- **Public surface**: landing page → email signup → Thursday digest → unsubscribe link. Nothing else.
- **Internal surface**: `/admin-dashboard/` — sources, import runs, event review, digest builder, subscriber count.
- **The real product**: connectors (ICS, RSS, JSON-LD, HTML-config, Ticketmaster API, manual) → normalize →
  dedupe (fuzzy + deterministic) → categorize (rule-based) → quality score → human review → digest.

Built as a Django monolith with Tailwind (CDN). No Celery/Redis in V1 — scheduled jobs are management
commands run by cron. Everything hangs off a `Region` row so more cities can be added later without a rewrite.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_region          # Bloomington-Normal + starter sources
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

- Landing page: http://localhost:8000/
- Review dashboard: http://localhost:8000/admin-dashboard/ (staff login)
- Django admin (source/region CRUD): http://localhost:8000/django-admin/

In dev, emails print to the console (no provider needed) and SQLite is used automatically.

## Weekly workflow

```bash
# Daily (cron): pull events from all enabled sources
python manage.py import_events

# Wednesday night / Thursday morning (cron): create the draft
python manage.py generate_digest
```

Then in the dashboard (target: under 60–90 min/week):

1. **Events** → filter "needs review" / "this weekend" → approve/reject (duplicates are flagged in amber).
2. **Digests** → open the draft → reorder, move sections, edit blurbs, remove events.
3. **Send test** to yourself, check it, then **Send** to all active subscribers.

## Sources

`manage.py seed_region` registers ~10 starter sources. Two are verified working and enabled:

| Source | Type | Status |
|---|---|---|
| Town of Normal Calendar | `rss` (CivicPlus) | enabled, verified |
| Eventbrite Bloomington | `json_ld` | enabled, verified |
| Visit BN, City of Bloomington, libraries, ISU, WGLT, Ticketmaster | various | seeded **disabled** with notes on what each needs |

Add/repair sources in Django admin. Each source's `parser_config` JSON is documented in
`curator/ingest/connectors/*.py`. Broken sources surface on the dashboard home page with their last error.
Ticketmaster activates once `TICKETMASTER_API_KEY` is set (free key from developer.ticketmaster.com).

## Deployment (Railway / Render / Fly.io)

One web service + managed Postgres + two cron jobs:

1. Set env vars from `.env.example` (`DATABASE_URL`, `SECRET_KEY`, `DEBUG=false`, `ALLOWED_HOSTS`,
   `SITE_BASE_URL`, email provider SMTP creds).
2. `Procfile` runs migrations on release and serves via gunicorn (whitenoise serves static files;
   run `python manage.py collectstatic` in the build step).
3. Cron: `python manage.py import_events` daily; `python manage.py generate_digest` Thursdays ~6am CT.
4. Sending the digest stays manual from the dashboard, by design.

## Tests

```bash
.venv/bin/python manage.py test
```

31 tests cover normalization, categorization, scoring, dedup/merge behavior, connector extraction
(JSON-LD/@graph, HTML selectors, CivicPlus RSS), signup/unsubscribe, digest generation, and sending.

## Explicitly out of scope (V1)

Accounts, personalization, payments, public event browsing/search, maps, event submissions,
Facebook/Instagram scraping, multi-city, AI. See the project spec.
