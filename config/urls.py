from django.contrib import admin
from django.urls import include, path

from curator import dashboard_views, views

dashboard_patterns = (
    [
        path("", dashboard_views.home, name="home"),
        path("sources/", dashboard_views.sources, name="sources"),
        path("sources/<int:source_id>/run/", dashboard_views.run_source_import, name="run_source_import"),
        path("import-runs/", dashboard_views.import_runs, name="import_runs"),
        path("events/", dashboard_views.events, name="events"),
        path("events/<int:event_id>/", dashboard_views.event_detail, name="event_detail"),
        path("events/<int:event_id>/action/", dashboard_views.event_action, name="event_action"),
        path("digests/", dashboard_views.digests, name="digests"),
        path("digests/<int:issue_id>/", dashboard_views.digest_detail, name="digest_detail"),
        path("digests/<int:issue_id>/preview/", dashboard_views.digest_preview, name="digest_preview"),
        path("subscribers/", dashboard_views.subscribers, name="subscribers"),
    ],
    "dashboard",
)

urlpatterns = [
    path("", views.landing, name="landing"),
    path("thanks/", views.thanks, name="thanks"),
    path("unsubscribe/<str:token>/", views.unsubscribe, name="unsubscribe"),
    path("issues/", views.issue_archive, name="issue_archive"),
    path("issues/<str:issue_date>/", views.issue_page, name="issue_page"),
    path("preview/latest/", views.preview_latest, name="preview_latest"),
    path("health/", views.health, name="health"),
    path("admin-dashboard/", include(dashboard_patterns)),
    path("django-admin/", admin.site.urls),
]
