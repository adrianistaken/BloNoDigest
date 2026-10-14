# Bloomington-Normal Local Events Digest

One weekly Thursday email answering "what's happening around Bloomington-Normal this weekend?" —
powered by an automated, region-based event ingestion pipeline with human review.

Digest drafts are created manually with **Generate draft for upcoming weekend**
on the dashboard's Digests page. The former Thursday draft job is retired;
remove its Railway service when deploying this change. Its config and
`generate_digest` command remain as a harmless compatibility entry point for
existing services, but no longer create drafts. Nightly event imports continue.
