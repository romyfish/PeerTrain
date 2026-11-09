from django.contrib.auth.decorators import login_required
from django.urls import path
from django.views.generic import RedirectView

from . import views, research_views


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
        login_required(
            RedirectView.as_view(
                pattern_name="history",
                permanent=True,
                query_string=True,
            )
        ),
        name="legacy_archives",
    ),
    path(
        "archives/<int:session_id>/",
        login_required(
            RedirectView.as_view(
                pattern_name="history_detail",
                permanent=True,
                query_string=True,
            )
        ),
        name="legacy_archive_detail",
    ),
    path("progress/", views.progress_view, name="progress"),
    path("admin/", views.admin_dashboard_view, name="admin_dashboard"),
    path("admin/users/", views.admin_users_view, name="admin_users"),
    path("admin/prompts/", research_views.prompt_inspection, name="admin_prompts"),
    path("admin/records/", research_views.records, name="admin_records"),
    path("admin/users/<int:user_id>/records/", research_views.records, name="admin_user_records"),
    path("admin/records/<int:session_id>/", research_views.record_detail, name="admin_record_detail"),
    path("admin/interaction-logs/", research_views.records, {"logs": True}, name="admin_logs"),
    path("sessions/<int:session_id>/live-feedback-events/", research_views.live_feedback_event, name="live_feedback_event"),
]
