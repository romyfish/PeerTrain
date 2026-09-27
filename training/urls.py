from django.urls import path
from django.views.generic import RedirectView

from . import views


urlpatterns = [
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("scenarios/", views.scenarios_view, name="scenarios"),
    path("sessions/<int:session_id>/chat/", views.chat_view, name="chat"),
    path("sessions/<int:session_id>/review/", views.session_review_view, name="session_review"),
    path("sessions/<int:session_id>/feedback/", views.feedback_view, name="feedback"),
    path("sessions/<int:session_id>/reflection/", views.reflection_view, name="reflection"),
    path("history/", views.history_view, name="history"),
    path("history/<int:session_id>/", views.history_detail_view, name="history_detail"),
    path(
        "archives/",
        RedirectView.as_view(
            pattern_name="history",
            permanent=True,
            query_string=True,
        ),
        name="legacy_archives",
    ),
    path(
        "archives/<int:session_id>/",
        RedirectView.as_view(
            pattern_name="history_detail",
            permanent=True,
            query_string=True,
        ),
        name="legacy_archive_detail",
    ),
    path("progress/", views.progress_view, name="progress"),
    path("admin/", views.admin_dashboard_view, name="admin_dashboard"),
    path("admin/users/", views.admin_users_view, name="admin_users"),
    path("admin/prompts/", views.admin_prompts_view, name="admin_prompts"),
    path(
        "admin/scoring-reviews/",
        views.admin_scoring_reviews_view,
        name="admin_scoring_reviews",
    ),
    path(
        "admin/scoring-reviews/<int:run_id>/<str:boundary_key>/",
        views.admin_scoring_review_detail_view,
        name="admin_scoring_review_detail",
    ),
    path("admin/analytics/", views.admin_analytics_view, name="admin_analytics"),
    path("admin/settings/", views.admin_settings_view, name="admin_settings"),
]
