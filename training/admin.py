from django.contrib import admin
from django.utils import timezone

from .models import (
    BoundaryEvaluationRun,
    BoundaryTurnEvaluation,
    Feedback,
    FeedbackScore,
    Message,
    PlatformSetting,
    Prompt,
    Reflection,
    ReflectionGuidance,
    Scenario,
    TrainingSession,
)


@admin.register(Scenario)
class ScenarioAdmin(admin.ModelAdmin):
    list_display = ("title", "boundary_type", "estimated_duration_min", "is_active")
    list_filter = ("boundary_type", "is_active")
    search_fields = ("title", "description")
    readonly_fields = (
        "title",
        "description",
        "boundary_type",
        "estimated_duration_min",
        "learning_objectives",
        "is_active",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "framework_version", "is_active", "is_archived")
    list_filter = ("type", "framework_version", "is_active", "is_archived")
    search_fields = ("name", "context", "method")


class FeedbackScoreInline(admin.TabularInline):
    model = FeedbackScore
    extra = 0


@admin.register(TrainingSession)
class TrainingSessionAdmin(admin.ModelAdmin):
    list_display = ("user", "scenario", "status", "overall_score", "start_time", "end_time")
    list_filter = ("status", "scenario__boundary_type")
    search_fields = ("user__email", "scenario__title")


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ("session", "overall_score", "created_at")
    inlines = [FeedbackScoreInline]


@admin.register(BoundaryEvaluationRun)
class BoundaryEvaluationRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "evaluator",
        "model",
        "purpose",
        "rubric_version",
        "status",
        "resolution_status",
        "production_ready",
        "created_at",
    )
    list_filter = (
        "status",
        "resolution_status",
        "production_ready",
        "purpose",
        "evaluator",
        "rubric_version",
        "model",
    )
    search_fields = ("session__user__email", "session__scenario__title", "response_id")
    readonly_fields = (
        "session",
        "evaluator",
        "evaluator_version",
        "prompt_version",
        "rubric_version",
        "model",
        "purpose",
        "status",
        "resolution_status",
        "production_ready",
        "evaluated_through_message",
        "verification_of",
        "response_id",
        "aggregate_scores",
        "baseline_rule_scores",
        "resolved_scores",
        "resolution_details",
        "validation_warnings",
        "review_summary",
        "raw_response",
        "error_message",
        "created_at",
        "completed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BoundaryTurnEvaluation)
class BoundaryTurnEvaluationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "run",
        "trainee_turn_number",
        "boundary_key",
        "applicable",
        "score",
        "confidence",
        "review_scope",
        "review_reason",
        "review_status",
        "effective_score",
    )
    list_filter = (
        "review_status",
        "review_scope",
        "review_reason",
        "boundary_key",
        "judgement",
        "critical_concern",
        "run__rubric_version",
        "run__model",
    )
    search_fields = (
        "run__session__user__email",
        "run__session__scenario__title",
        "evidence_interpretation",
        "rationale",
        "reviewer_notes",
    )
    readonly_fields = (
        "run",
        "trainee_message",
        "help_seeker_message",
        "trainee_turn_number",
        "boundary_key",
        "applicable",
        "evidence_quotes",
        "evidence_interpretation",
        "expected_move_key",
        "expected_move",
        "judgement",
        "score",
        "rationale",
        "confidence",
        "critical_concern",
        "review_scope",
        "review_reason",
        "reviewed_by",
        "reviewed_at",
        "created_at",
    )
    actions = ("mark_confirmed", "mark_rejected")

    @admin.action(description="Confirm selected evaluations")
    def mark_confirmed(self, request, queryset):
        queryset.update(
            review_status=BoundaryTurnEvaluation.ReviewStatuses.CONFIRMED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )

    @admin.action(description="Reject selected evaluations")
    def mark_rejected(self, request, queryset):
        queryset.update(
            review_status=BoundaryTurnEvaluation.ReviewStatuses.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )

    def save_model(self, request, obj, form, change):
        if obj.review_status == BoundaryTurnEvaluation.ReviewStatuses.PENDING:
            obj.reviewed_by = None
            obj.reviewed_at = None
        else:
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
        super().save_model(request, obj, form, change)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Message)
admin.site.register(Reflection)
admin.site.register(ReflectionGuidance)
admin.site.register(PlatformSetting)
