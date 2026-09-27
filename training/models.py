from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Scenario(models.Model):
    class BoundaryTypes(models.TextChoices):
        AGENCY = "AGENCY", "Agency Boundary"
        RELATIONSHIP = "RELATIONSHIP", "Relationship Boundary"
        SAFETY = "SAFETY", "Safety Boundary"
        INTEGRATED = "INTEGRATED", "Integrated Boundary"

    title = models.CharField(max_length=255, unique=True)
    description = models.TextField()
    boundary_type = models.CharField(max_length=30, choices=BoundaryTypes.choices)
    estimated_duration_min = models.PositiveIntegerField()
    learning_objectives = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["title"]

    @property
    def learning_objectives_list(self):
        return [line.strip() for line in self.learning_objectives.splitlines() if line.strip()]

    def __str__(self):
        return self.title


class Prompt(models.Model):
    FRAMEWORK_VERSION = "CMCV-1.0"

    class PromptTypes(models.TextChoices):
        ROLE_PLAY = "ROLE_PLAY", "Role-play"
        FEEDBACK = "FEEDBACK", "Feedback"
        REFLECTION = "REFLECTION", "Reflection"

    name = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=20, choices=PromptTypes.choices)
    scenario = models.ForeignKey(
        "Scenario",
        on_delete=models.CASCADE,
        related_name="prompts",
        blank=True,
        null=True,
    )
    context = models.TextField(blank=True)
    method = models.TextField()
    framework_version = models.CharField(max_length=20, default=FRAMEWORK_VERSION)
    is_active = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["type", "name"]

    def __init__(self, *args, **kwargs):
        # Keep historical fixtures and integrations working while content becomes Method.
        legacy_content = kwargs.pop("content", None)
        super().__init__(*args, **kwargs)
        if legacy_content is not None and not self.method:
            self.method = legacy_content

    @property
    def content(self):
        return self.method

    @content.setter
    def content(self, value):
        self.method = value

    def save(self, *args, **kwargs):
        if self.is_archived:
            self.is_active = False
        super().save(*args, **kwargs)
        if self.is_active:
            queryset = Prompt.objects.filter(type=self.type).exclude(pk=self.pk)
            if self.scenario_id:
                queryset = queryset.filter(scenario=self.scenario)
            else:
                queryset = queryset.filter(scenario__isnull=True)
            queryset.update(is_active=False)

    def __str__(self):
        scope = self.scenario.title if self.scenario_id else "Global"
        return f"{self.get_type_display()} ({scope}): {self.name}"


class TrainingSession(models.Model):
    class Statuses(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"
        ABANDONED = "ABANDONED", "Abandoned"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="training_sessions",
    )
    scenario = models.ForeignKey(Scenario, on_delete=models.PROTECT, related_name="sessions")
    prompt = models.ForeignKey(
        Prompt,
        on_delete=models.PROTECT,
        related_name="training_sessions",
    )
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=Statuses.choices,
        default=Statuses.IN_PROGRESS,
    )
    overall_score = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    ended_early = models.BooleanField(default=False)
    rubric_version = models.CharField(max_length=30, default="BOUNDARY-SKILLS-2.0")

    class Meta:
        ordering = ["-start_time"]

    def __str__(self):
        return f"{self.user} - {self.scenario.title}"


class Message(models.Model):
    class SenderTypes(models.TextChoices):
        USER = "USER", "User"
        AI = "AI", "AI"

    class ResponseSources(models.TextChoices):
        UNKNOWN = "", "Unknown"
        USER_INPUT = "USER_INPUT", "User Input"
        SCRIPTED = "SCRIPTED", "Scripted"
        OPENAI = "OPENAI", "OpenAI"
        GEMINI = "GEMINI", "Gemini"
        PLACEHOLDER = "PLACEHOLDER", "Placeholder"

    session = models.ForeignKey(
        TrainingSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender_type = models.CharField(max_length=10, choices=SenderTypes.choices)
    content = models.TextField()
    response_source = models.CharField(
        max_length=20,
        choices=ResponseSources.choices,
        blank=True,
        default=ResponseSources.UNKNOWN,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.session_id} - {self.sender_type}"


class Feedback(models.Model):
    class GenerationSources(models.TextChoices):
        OPENAI = "OPENAI", "OpenAI"
        GEMINI = "GEMINI", "Gemini"
        PLACEHOLDER = "PLACEHOLDER", "Placeholder"

    session = models.OneToOneField(
        TrainingSession,
        on_delete=models.CASCADE,
        related_name="feedback",
    )
    prompt = models.ForeignKey(
        Prompt,
        on_delete=models.PROTECT,
        related_name="feedback_entries",
    )
    # 没有足够证据时保留为空，避免把“未观察到”误写成 0 分。
    overall_score = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    strengths = models.TextField()
    improvements = models.TextField()
    generation_source = models.CharField(
        max_length=20,
        choices=GenerationSources.choices,
        default=GenerationSources.PLACEHOLDER,
    )
    rubric_version = models.CharField(max_length=30, default="BOUNDARY-SKILLS-2.0")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def strength_list(self):
        return [line.strip() for line in self.strengths.splitlines() if line.strip()]

    @property
    def improvement_list(self):
        return [line.strip() for line in self.improvements.splitlines() if line.strip()]

    def __str__(self):
        return f"Feedback for session {self.session_id}"


class FeedbackReviewItem(models.Model):
    class BoundaryKeys(models.TextChoices):
        AGENCY = "AGENCY", "Agency Boundary"
        RELATIONSHIP = "RELATIONSHIP", "Relationship Boundary"
        SAFETY = "SAFETY", "Safety Boundary"

    feedback = models.ForeignKey(
        Feedback,
        on_delete=models.CASCADE,
        related_name="review_items",
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name="review_items",
        blank=True,
        null=True,
    )
    turn_index = models.PositiveSmallIntegerField(default=0)
    message_excerpt = models.TextField()
    issue_label = models.CharField(max_length=120)
    why_it_matters = models.TextField()
    better_reply = models.TextField()
    boundary_key = models.CharField(
        max_length=20,
        choices=BoundaryKeys.choices,
        default=BoundaryKeys.AGENCY,
    )
    source = models.CharField(
        max_length=20,
        choices=Feedback.GenerationSources.choices,
        default=Feedback.GenerationSources.PLACEHOLDER,
    )

    class Meta:
        ordering = ["turn_index", "id"]

    def __str__(self):
        return f"Review item for feedback {self.feedback_id} turn {self.turn_index}"


class FeedbackScore(models.Model):
    class Criteria(models.TextChoices):
        AGENCY = "AGENCY", "Agency Boundary"
        RELATIONSHIP = "RELATIONSHIP", "Relationship Boundary"
        SAFETY = "SAFETY", "Safety Boundary"
        # Kept only so records from the earlier rubric remain readable until cleared.
        EMPATHY = "EMPATHY", "Empathy (legacy)"
        BOUNDARY_AWARENESS = "BOUNDARY_AWARENESS", "Boundary Awareness (legacy)"
        REFERRAL_AWARENESS = "REFERRAL_AWARENESS", "Referral Awareness (legacy)"
        COMMUNICATION_SKILLS = "COMMUNICATION_SKILLS", "Communication Skills (legacy)"

    feedback = models.ForeignKey(
        Feedback,
        on_delete=models.CASCADE,
        related_name="scores",
    )
    criterion = models.CharField(max_length=40, choices=Criteria.choices)
    score = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    observed = models.BooleanField(default=True)
    review_required = models.BooleanField(default=False)
    evidence = models.TextField(blank=True)
    rationale = models.TextField(blank=True)
    next_move = models.TextField(blank=True)
    rubric_version = models.CharField(max_length=30, default="BOUNDARY-SKILLS-2.0")

    class Meta:
        ordering = ["criterion"]
        constraints = [
            models.UniqueConstraint(
                fields=["feedback", "criterion"],
                name="unique_feedback_score_per_criterion",
            )
        ]

    def __str__(self):
        if self.review_required:
            score = "needs review"
        else:
            score = self.score if self.observed else "not observed"
        return f"{self.get_criterion_display()}: {score}"


class BoundaryEvaluationRun(models.Model):
    class Evaluators(models.TextChoices):
        G_EVAL = "G_EVAL", "G-Eval"

    class Statuses(models.TextChoices):
        STARTED = "STARTED", "Started"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    class Purposes(models.TextChoices):
        MANUAL = "MANUAL", "Manual evaluation"
        LIVE = "LIVE", "Live score"
        FINAL = "FINAL", "Final feedback"
        VERIFICATION = "VERIFICATION", "Verification pass"

    class ResolutionStatuses(models.TextChoices):
        PENDING = "PENDING", "Pending resolution"
        ACCEPTED = "ACCEPTED", "LLM judgement accepted"
        FALLBACK = "FALLBACK", "Rule fallback used"
        REVIEW_REQUIRED = "REVIEW_REQUIRED", "Human review required"

    session = models.ForeignKey(
        TrainingSession,
        on_delete=models.CASCADE,
        related_name="boundary_evaluation_runs",
    )
    evaluator = models.CharField(
        max_length=20,
        choices=Evaluators.choices,
        default=Evaluators.G_EVAL,
    )
    evaluator_version = models.CharField(max_length=40)
    prompt_version = models.CharField(max_length=40)
    rubric_version = models.CharField(max_length=40)
    model = models.CharField(max_length=100)
    purpose = models.CharField(
        max_length=20,
        choices=Purposes.choices,
        default=Purposes.MANUAL,
    )
    status = models.CharField(
        max_length=20,
        choices=Statuses.choices,
        default=Statuses.STARTED,
    )
    resolution_status = models.CharField(
        max_length=30,
        choices=ResolutionStatuses.choices,
        default=ResolutionStatuses.PENDING,
    )
    production_ready = models.BooleanField(default=False)
    evaluated_through_message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name="completed_boundary_evaluation_runs",
        blank=True,
        null=True,
    )
    verification_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="verification_runs",
        blank=True,
        null=True,
    )
    response_id = models.CharField(max_length=120, blank=True)
    aggregate_scores = models.JSONField(default=dict, blank=True)
    baseline_rule_scores = models.JSONField(default=dict, blank=True)
    resolved_scores = models.JSONField(default=dict, blank=True)
    resolution_details = models.JSONField(default=dict, blank=True)
    validation_warnings = models.JSONField(default=list, blank=True)
    review_summary = models.TextField(blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return (
            f"{self.get_evaluator_display()} run {self.pk} "
            f"for session {self.session_id}"
        )


class BoundaryTurnEvaluation(models.Model):
    class BoundaryKeys(models.TextChoices):
        AGENCY = "AGENCY", "Agency Boundary"
        RELATIONSHIP = "RELATIONSHIP", "Relationship Boundary"
        SAFETY = "SAFETY", "Safety Boundary"

    class Judgements(models.TextChoices):
        MET = "MET", "Met"
        PARTLY_MET = "PARTLY_MET", "Partly met"
        MISSED = "MISSED", "Missed"
        BOUNDARY_CONCERN = "BOUNDARY_CONCERN", "Boundary concern"
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"

    class ReviewStatuses(models.TextChoices):
        NOT_REQUIRED = "NOT_REQUIRED", "Not selected for review"
        PENDING = "PENDING", "Pending review"
        CONFIRMED = "CONFIRMED", "Confirmed"
        CORRECTED = "CORRECTED", "Corrected"
        REJECTED = "REJECTED", "Rejected"

    class ReviewScopes(models.TextChoices):
        NONE = "NONE", "Not selected"
        REQUIRED = "REQUIRED", "Required review"
        QUALITY_SAMPLE = "QUALITY_SAMPLE", "Quality sample"

    class ReviewReasons(models.TextChoices):
        LOW_CONFIDENCE = "LOW_CONFIDENCE", "Low judge confidence"
        SCORE_DISAGREEMENT = "SCORE_DISAGREEMENT", "Judge and baseline disagree"
        BASELINE_NOT_OBSERVED = (
            "BASELINE_NOT_OBSERVED",
            "Judge found an opportunity the baseline missed",
        )
        CRITICAL_UNCONFIRMED = (
            "CRITICAL_UNCONFIRMED",
            "Critical concern was not independently confirmed",
        )
        CRITICAL_CONCERN = "CRITICAL_CONCERN", "Critical boundary concern"
        RELIABILITY_WARNING = "RELIABILITY_WARNING", "Other reliability warning"
        QUALITY_SAMPLE = "QUALITY_SAMPLE", "Routine quality-control sample"
        MANUAL_SELECTION = "MANUAL_SELECTION", "Previously selected manually"

    run = models.ForeignKey(
        BoundaryEvaluationRun,
        on_delete=models.CASCADE,
        related_name="turn_evaluations",
    )
    trainee_message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name="boundary_turn_evaluations",
        blank=True,
        null=True,
    )
    help_seeker_message = models.ForeignKey(
        Message,
        on_delete=models.SET_NULL,
        related_name="elicited_boundary_turn_evaluations",
        blank=True,
        null=True,
    )
    trainee_turn_number = models.PositiveSmallIntegerField()
    boundary_key = models.CharField(max_length=20, choices=BoundaryKeys.choices)
    applicable = models.BooleanField(default=True)
    evidence_quotes = models.JSONField(default=list, blank=True)
    evidence_interpretation = models.TextField(blank=True)
    expected_move_key = models.CharField(max_length=40, blank=True)
    expected_move = models.CharField(max_length=255, blank=True)
    judgement = models.CharField(max_length=30, choices=Judgements.choices)
    score = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    rationale = models.TextField(blank=True)
    confidence = models.FloatField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    critical_concern = models.BooleanField(default=False)
    review_scope = models.CharField(
        max_length=20,
        choices=ReviewScopes.choices,
        default=ReviewScopes.NONE,
        db_index=True,
    )
    review_reason = models.CharField(
        max_length=40,
        choices=ReviewReasons.choices,
        blank=True,
        default="",
    )
    review_status = models.CharField(
        max_length=20,
        choices=ReviewStatuses.choices,
        default=ReviewStatuses.NOT_REQUIRED,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_boundary_turn_evaluations",
        blank=True,
        null=True,
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewer_notes = models.TextField(blank=True)
    corrected_score = models.PositiveSmallIntegerField(
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    corrected_judgement = models.CharField(
        max_length=30,
        choices=Judgements.choices,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["trainee_turn_number", "boundary_key", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "trainee_turn_number", "boundary_key"],
                name="unique_boundary_turn_per_evaluation_run",
            )
        ]

    @property
    def effective_score(self):
        if self.review_status == self.ReviewStatuses.CORRECTED:
            return self.corrected_score
        if self.review_status == self.ReviewStatuses.REJECTED:
            return None
        return self.score

    def clean(self):
        super().clean()
        if self.review_status == self.ReviewStatuses.CORRECTED:
            errors = {}
            if self.corrected_score is None:
                errors["corrected_score"] = "A corrected review requires a score."
            if not self.corrected_judgement:
                errors["corrected_judgement"] = (
                    "A corrected review requires a judgement."
                )
            score_ranges = {
                self.Judgements.MET: {4, 5},
                self.Judgements.PARTLY_MET: {3},
                self.Judgements.MISSED: {2},
                self.Judgements.BOUNDARY_CONCERN: {1, 2},
            }
            if (
                self.corrected_judgement
                and self.corrected_judgement not in score_ranges
            ):
                errors["corrected_judgement"] = (
                    "Use Rejected when the row should be treated as not applicable."
                )
            elif (
                self.corrected_score is not None
                and self.corrected_judgement
                and self.corrected_score
                not in score_ranges.get(self.corrected_judgement, set())
            ):
                errors["corrected_score"] = (
                    "The corrected score does not match the corrected judgement."
                )
            if errors:
                raise ValidationError(errors)

    def __str__(self):
        return (
            f"Run {self.run_id}, turn {self.trainee_turn_number}, "
            f"{self.get_boundary_key_display()}"
        )


class Reflection(models.Model):
    session = models.OneToOneField(
        TrainingSession,
        on_delete=models.CASCADE,
        related_name="reflection",
    )
    prompt = models.ForeignKey(
        Prompt,
        on_delete=models.PROTECT,
        related_name="reflections",
    )
    reflection_text = models.TextField()
    action_plan = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reflection for session {self.session_id}"


class ReflectionGuidance(models.Model):
    class GenerationSources(models.TextChoices):
        OPENAI = "OPENAI", "OpenAI"
        GEMINI = "GEMINI", "Google Gemini"
        PLACEHOLDER = "PLACEHOLDER", "Placeholder"

    session = models.OneToOneField(
        TrainingSession,
        on_delete=models.CASCADE,
        related_name="reflection_guidance",
    )
    prompt = models.ForeignKey(
        Prompt,
        on_delete=models.PROTECT,
        related_name="reflection_guidance_entries",
    )
    notice_prompt = models.TextField()
    action_prompt = models.TextField()
    generation_source = models.CharField(
        max_length=20,
        choices=GenerationSources.choices,
        default=GenerationSources.PLACEHOLDER,
    )
    framework_version = models.CharField(max_length=20, default=Prompt.FRAMEWORK_VERSION)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reflection guidance for session {self.session_id}"


class PlatformSetting(models.Model):
    singleton_key = models.CharField(max_length=20, default="default", unique=True)
    max_exchanges_per_session = models.PositiveIntegerField(default=20)

    class Meta:
        verbose_name = "Platform Setting"
        verbose_name_plural = "Platform Settings"

    @classmethod
    def get_solo(cls):
        instance, _ = cls.objects.get_or_create(singleton_key="default")
        return instance

    def __str__(self):
        return "Platform settings"
