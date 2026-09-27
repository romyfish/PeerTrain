from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from training.models import (
    BoundaryEvaluationRun,
    BoundaryTurnEvaluation,
    Message,
    Prompt,
    Scenario,
    TrainingSession,
)
from training.services.review_queue import configure_review_queue_for_run


class ScoreReviewQueueTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="review-admin",
            email="review-admin@example.com",
            password="test-pass",
            role="ADMIN",
        )
        self.trainee = User.objects.create_user(
            username="review-trainee",
            email="review-trainee@example.com",
            password="test-pass",
            role="TRAINEE",
        )
        self.scenario = Scenario.objects.create(
            title="Review queue agency scenario",
            description="Test selective human review.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="Preserve agency.",
        )
        self.prompt = Prompt.objects.create(
            name="Review queue prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=self.scenario,
            method="Stay in role.",
        )
        self.session = TrainingSession.objects.create(
            user=self.trainee,
            scenario=self.scenario,
            prompt=self.prompt,
        )
        self.help_seeker_message = Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.AI,
            content="I cannot decide what to do. Tell me which option to choose.",
        )
        self.trainee_message = Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.USER,
            content="What matters most to you about the options?",
        )

    def _create_run(
        self,
        *,
        detail,
        production_ready,
        score=3,
        confidence=0.90,
        critical=False,
    ):
        run = BoundaryEvaluationRun.objects.create(
            session=self.session,
            evaluator_version="test-evaluator",
            prompt_version="test-prompt",
            rubric_version="test-rubric",
            model="test-judge",
            status=BoundaryEvaluationRun.Statuses.SUCCEEDED,
            resolution_status=(
                BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED
                if production_ready
                else BoundaryEvaluationRun.ResolutionStatuses.REVIEW_REQUIRED
            ),
            production_ready=production_ready,
            aggregate_scores={"AGENCY": score},
            baseline_rule_scores={"AGENCY": 2},
            resolved_scores={"AGENCY": score if production_ready else 2},
            resolution_details={"AGENCY": detail},
        )
        evaluation = BoundaryTurnEvaluation.objects.create(
            run=run,
            trainee_message=self.trainee_message,
            help_seeker_message=self.help_seeker_message,
            trainee_turn_number=1,
            boundary_key=BoundaryTurnEvaluation.BoundaryKeys.AGENCY,
            applicable=True,
            evidence_quotes=["What matters most to you about the options?"],
            evidence_interpretation="The reply explores the trainee's priorities.",
            expected_move_key="exploration",
            expected_move="Explore the help-seeker's perspective",
            judgement=(
                BoundaryTurnEvaluation.Judgements.BOUNDARY_CONCERN
                if score <= 2
                else BoundaryTurnEvaluation.Judgements.PARTLY_MET
            ),
            score=score,
            rationale="Test rationale.",
            confidence=confidence,
            critical_concern=critical,
        )
        return run, evaluation

    @override_settings(BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE=0)
    def test_reliability_exception_is_required_but_routine_score_is_not_selected(self):
        required_run, required = self._create_run(
            detail={
                "decision": "REVIEW_REQUIRED",
                "reason_code": "LOW_CONFIDENCE",
            },
            production_ready=False,
            confidence=0.60,
        )
        configure_review_queue_for_run(required_run)

        routine_run, routine = self._create_run(
            detail={"decision": "LLM_ACCEPTED", "reason_code": "ACCEPTED"},
            production_ready=True,
        )
        configure_review_queue_for_run(routine_run)

        required.refresh_from_db()
        routine.refresh_from_db()
        self.assertEqual(
            required.review_scope,
            BoundaryTurnEvaluation.ReviewScopes.REQUIRED,
        )
        self.assertEqual(
            required.review_status,
            BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        self.assertEqual(
            routine.review_scope,
            BoundaryTurnEvaluation.ReviewScopes.NONE,
        )
        self.assertEqual(
            routine.review_status,
            BoundaryTurnEvaluation.ReviewStatuses.NOT_REQUIRED,
        )

    @override_settings(BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE=1)
    def test_quality_sample_is_selected_without_blocking_accepted_run(self):
        run, evaluation = self._create_run(
            detail={"decision": "LLM_ACCEPTED", "reason_code": "ACCEPTED"},
            production_ready=True,
        )

        configure_review_queue_for_run(run)

        evaluation.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(
            evaluation.review_scope,
            BoundaryTurnEvaluation.ReviewScopes.QUALITY_SAMPLE,
        )
        self.assertEqual(
            evaluation.review_status,
            BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        self.assertTrue(run.production_ready)
        self.assertEqual(
            run.resolution_status,
            BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED,
        )

    @override_settings(BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE=0)
    def test_custom_admin_queue_excludes_unselected_scores(self):
        required_run, _ = self._create_run(
            detail={
                "decision": "REVIEW_REQUIRED",
                "reason_code": "SCORE_DISAGREEMENT",
            },
            production_ready=False,
        )
        configure_review_queue_for_run(required_run)
        routine_run, _ = self._create_run(
            detail={"decision": "LLM_ACCEPTED", "reason_code": "ACCEPTED"},
            production_ready=True,
        )
        configure_review_queue_for_run(routine_run)
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_scoring_reviews"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"]), 1)
        row = list(response.context["page_obj"])[0]
        self.assertEqual(row["run"].id, required_run.id)
        self.assertNotEqual(row["run"].id, routine_run.id)

    @override_settings(BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE=0)
    def test_human_correction_becomes_the_resolved_score(self):
        run, evaluation = self._create_run(
            detail={
                "decision": "REVIEW_REQUIRED",
                "reason_code": "SCORE_DISAGREEMENT",
            },
            production_ready=False,
            score=3,
        )
        configure_review_queue_for_run(run)
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse(
                "admin_scoring_review_detail",
                kwargs={"run_id": run.id, "boundary_key": "AGENCY"},
            ),
            {
                "action": "review_one",
                "evaluation_id": str(evaluation.id),
                f"review-{evaluation.id}-review_status": "CORRECTED",
                f"review-{evaluation.id}-corrected_judgement": "MET",
                f"review-{evaluation.id}-corrected_score": "4",
                f"review-{evaluation.id}-reviewer_notes": (
                    "The quoted open question fully returns choice to the help-seeker."
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        evaluation.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(
            evaluation.review_status,
            BoundaryTurnEvaluation.ReviewStatuses.CORRECTED,
        )
        self.assertEqual(evaluation.reviewed_by, self.admin)
        self.assertEqual(run.resolved_scores["AGENCY"], 4)
        self.assertTrue(run.production_ready)
        self.assertEqual(
            run.resolution_details["AGENCY"]["reason_code"],
            "HUMAN_CORRECTED",
        )

    def test_trainee_cannot_access_score_reviews(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("admin_scoring_reviews"))

        self.assertEqual(response.status_code, 403)
