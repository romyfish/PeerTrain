from django.contrib import admin

from .models import Scenario, TrainingSession, Feedback, Message, Reflection


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


class ResearchRecordAdmin(admin.ModelAdmin):
    actions = None

    def has_view_permission(self, request, obj=None):
        return request.user.is_authenticated and (request.user.is_superuser or request.user.role == 'ADMIN')

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .models import TurnAssessment, ReflectionMessage, RoleplaySafetyCheck, FeedbackReviewItem, LiveFeedbackEvent, SessionManagementEvent
for model in (TrainingSession, Feedback, Message, Reflection, TurnAssessment, ReflectionMessage,
              RoleplaySafetyCheck, FeedbackReviewItem, LiveFeedbackEvent, SessionManagementEvent):
    admin.site.register(model, ResearchRecordAdmin)
