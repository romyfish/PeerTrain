from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from accounts.models import User
from training.models import (
    BoundaryEvaluationRun,
    BoundaryTurnEvaluation,
    Message,
    Prompt,
    Scenario,
    TrainingSession,
)
from training.rubrics.boundaries import (
    BOUNDARY_RUBRICS,
    EVIDENCE_RUBRIC_VERSION,
    SCORE_BANDS,
)
from training.services.ai import (
    BoundaryScorePayload,
    FeedbackPayload,
    _calibrate_feedback_payload,
    build_boundary_map_indicator,
)
from training.services.geval import (
    GEVAL_PROMPT_VERSION,
    GEVAL_VERSION,
    GEvalResponse,
    GEvalTurnJudgement,
    aggregate_geval_response_boundary,
    build_geval_prompt,
    ensure_reliable_geval_run,
    preview_geval_session,
    resolve_geval_scores,
    run_geval_session,
    validate_geval_response,
)
from training.views import _process_chat_turn


class GEvalPreviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="geval-reviewer",
            email="geval@example.com",
            password="test-pass",
            role="TRAINEE",
        )
        self.scenario = Scenario.objects.create(
            title="G-Eval Agency Trial",
            description="Review an Agency judgement.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="Preserve choice",
        )
        self.prompt = Prompt.objects.create(
            name="G-Eval role-play",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=self.scenario,
            method="Stay in role.",
        )
        self.session = TrainingSession.objects.create(
            user=self.user,
            scenario=self.scenario,
            prompt=self.prompt,
        )
        Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.AI,
            content="I cannot decide whether to change course. What should I do?",
        )
        Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.USER,
            content="What matters most to you about each option?",
        )

    def test_single_focus_prompt_contains_only_target_boundary(self):
        system_text, _, _, allowed, _ = build_geval_prompt(self.session)

        self.assertEqual(allowed, ("agency",))
        self.assertIn("Agency Boundary", system_text)
        self.assertNotIn("Relationship Boundary", system_text)
        self.assertNotIn("Safety Boundary", system_text)
        self.assertLess(
            system_text.index("copy the shortest exact trainee-reply quotation"),
            system_text.index("identify the expected_move_key, expected"),
        )

    def test_rubric_has_three_fixed_boundaries_and_five_score_bands(self):
        self.assertEqual(
            tuple(BOUNDARY_RUBRICS),
            ("agency", "relationship", "safety"),
        )
        self.assertEqual([band.score for band in SCORE_BANDS], [5, 4, 3, 2, 1])
        self.assertEqual(EVIDENCE_RUBRIC_VERSION, "BOUNDARY-EVIDENCE-1.0")

    def test_validator_rejects_invented_evidence(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=["This quotation was invented."],
                    evidence_interpretation="The reply explores the decision.",
                    expected_move_key="exploration",
                    expected_move="Explore the peer's perspective",
                    judgement="MET",
                    score=5,
                    rationale="Explores the decision.",
                    confidence=0.9,
                )
            ],
            review_summary="Review.",
        )

        with self.assertRaisesRegex(ValueError, "not in the trainee reply"):
            validate_geval_response(
                response,
                build_geval_prompt(self.session)[2],
                ("agency",),
            )

    def test_preview_is_validated_and_never_persisted(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=[
                        "What matters most to you about each option?"
                    ],
                    evidence_interpretation=(
                        "The open question asks the help-seeker to identify priorities."
                    ),
                    expected_move_key="exploration",
                    expected_move="Explore the peer's perspective",
                    judgement="MET",
                    score=5,
                    rationale="The reply invites the help-seeker to identify their priorities.",
                    confidence=0.94,
                )
            ],
            review_summary="The Agency move was clearly met.",
        )
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(output_parsed=response)

        preview = preview_geval_session(
            self.session,
            model="test-judge",
            client=client,
        )

        self.assertEqual(preview.llm_boundary_scores, {"AGENCY": 5})
        self.assertFalse(preview.persisted)
        self.assertEqual(TrainingSession.objects.get(pk=self.session.pk).overall_score, None)
        self.assertFalse(BoundaryEvaluationRun.objects.exists())
        client.responses.parse.assert_called_once()

    def test_run_persists_evidence_and_leaves_human_review_pending(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=[
                        "What matters most to you about each option?"
                    ],
                    evidence_interpretation=(
                        "The open question returns attention and choice to the help-seeker."
                    ),
                    expected_move_key="exploration",
                    expected_move="Explore the help-seeker's perspective and priorities",
                    judgement="MET",
                    score=5,
                    rationale="The expected Agency move is fully met.",
                    confidence=0.94,
                )
            ],
            review_summary="The Agency evidence supports a strong score.",
        )
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(
            id="resp_test_123",
            output_parsed=response,
        )

        result = run_geval_session(
            self.session,
            model="test-judge",
            client=client,
        )

        run = BoundaryEvaluationRun.objects.get(pk=result.evaluation_run_id)
        turn = run.turn_evaluations.get()
        self.assertTrue(result.persisted)
        self.assertEqual(run.status, BoundaryEvaluationRun.Statuses.SUCCEEDED)
        self.assertEqual(run.rubric_version, EVIDENCE_RUBRIC_VERSION)
        self.assertEqual(run.response_id, "resp_test_123")
        self.assertEqual(run.aggregate_scores, {"AGENCY": 5})
        self.assertEqual(run.resolved_scores, {"AGENCY": 2})
        self.assertEqual(
            run.resolution_status,
            BoundaryEvaluationRun.ResolutionStatuses.REVIEW_REQUIRED,
        )
        self.assertFalse(run.production_ready)
        self.assertEqual(turn.expected_move_key, "exploration")
        self.assertEqual(
            turn.evidence_quotes,
            ["What matters most to you about each option?"],
        )
        self.assertEqual(
            turn.review_status,
            BoundaryTurnEvaluation.ReviewStatuses.PENDING,
        )
        self.assertEqual(TrainingSession.objects.get(pk=self.session.pk).overall_score, None)

    def test_low_confidence_judge_uses_rule_score_and_requires_review(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=[
                        "What matters most to you about each option?"
                    ],
                    evidence_interpretation="The reply uses an open question.",
                    expected_move_key="exploration",
                    expected_move="Explore the help-seeker's perspective",
                    judgement="PARTLY_MET",
                    score=3,
                    rationale="The move is only partly developed.",
                    confidence=0.60,
                )
            ],
            review_summary="Low confidence.",
        )

        scores, status, details, production_ready = resolve_geval_scores(
            response,
            {"AGENCY": 3},
            {"AGENCY": 4},
            ("agency",),
            min_confidence=0.85,
        )

        self.assertEqual(scores, {"AGENCY": 4})
        self.assertEqual(
            status,
            BoundaryEvaluationRun.ResolutionStatuses.REVIEW_REQUIRED,
        )
        self.assertEqual(details["AGENCY"]["decision"], "REVIEW_REQUIRED")
        self.assertFalse(production_ready)

    def test_critical_score_requires_deterministic_confirmation(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=[
                        "What matters most to you about each option?"
                    ],
                    evidence_interpretation="Test critical interpretation.",
                    expected_move_key="choice",
                    expected_move="Offer choices rather than instructions",
                    judgement="BOUNDARY_CONCERN",
                    score=1,
                    rationale="Test critical concern.",
                    confidence=0.97,
                    critical_concern=True,
                )
            ],
            review_summary="Critical test.",
        )

        scores, status, details, production_ready = resolve_geval_scores(
            response,
            {"AGENCY": 1},
            {"AGENCY": 2},
            ("agency",),
            min_confidence=0.85,
        )

        self.assertEqual(scores, {"AGENCY": 2})
        self.assertEqual(
            status,
            BoundaryEvaluationRun.ResolutionStatuses.REVIEW_REQUIRED,
        )
        self.assertIn("requires confirmation", details["AGENCY"]["reason"])
        self.assertFalse(production_ready)

    @override_settings(
        BOUNDARY_JUDGE_MODEL="test-judge",
        BOUNDARY_JUDGE_MIN_CONFIDENCE=0.85,
    )
    def test_live_map_uses_fresh_reliability_gated_judge_evidence(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=[
                        "What matters most to you about each option?"
                    ],
                    evidence_interpretation=(
                        "The reply returns attention to the help-seeker's priorities."
                    ),
                    expected_move_key="exploration",
                    expected_move="Explore the help-seeker's perspective and priorities",
                    judgement="MET",
                    score=5,
                    rationale="The Agency move is fully met.",
                    confidence=0.96,
                )
            ],
            review_summary="Accepted.",
        )
        client = Mock()
        client.responses.parse.side_effect = [
            SimpleNamespace(id="resp_live_123", output_parsed=response),
            SimpleNamespace(id="resp_verify_123", output_parsed=response),
        ]
        run = ensure_reliable_geval_run(
            self.session,
            client=client,
            purpose=BoundaryEvaluationRun.Purposes.LIVE,
            force=True,
        )

        boundary_map = build_boundary_map_indicator(self.session)
        agency = boundary_map.dimensions[0]

        self.assertEqual(
            run.resolution_status,
            BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED,
        )
        self.assertTrue(run.production_ready)
        self.assertEqual(
            run.resolution_details["AGENCY"]["decision"],
            "LLM_CONSENSUS_ACCEPTED",
        )
        self.assertEqual(client.responses.parse.call_count, 2)
        self.assertEqual(agency.score_source, "LLM_JUDGE")
        self.assertEqual(agency.score, 5)
        self.assertEqual(
            agency.evidence_quotes,
            ("What matters most to you about each option?",),
        )

        calibrated = _calibrate_feedback_payload(
            self.session,
            FeedbackPayload(
                boundary_scores=[
                    BoundaryScorePayload(
                        boundary_key="AGENCY",
                        observed=True,
                        score=2,
                        next_move="Keep exploring the person's priorities.",
                    )
                ],
                what_worked=[],
                next_boundary_moves=[],
                review_items=[],
            ),
        )
        agency_feedback = next(
            item
            for item in calibrated.boundary_scores
            if item.boundary_key == "AGENCY"
        )
        # Final feedback must not reuse a LIVE run, even when the live coach
        # accepted that latest-turn judgement.
        self.assertEqual(agency_feedback.score, 2)
        self.assertIn(
            "What matters most to you about each option?",
            agency_feedback.evidence,
        )

    def test_final_aggregation_keeps_an_earlier_unrepaired_miss(self):
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=["Tell me what I should do."],
                    evidence_interpretation="The reply does not explore the peer's view.",
                    expected_move_key="exploration",
                    expected_move="Explore the peer's perspective",
                    judgement="MISSED",
                    score=2,
                    rationale="A genuine exploration opportunity was missed.",
                    confidence=0.94,
                ),
                GEvalTurnJudgement(
                    trainee_turn_number=2,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=["You could choose either option."],
                    evidence_interpretation="The reply offers options.",
                    expected_move_key="choice",
                    expected_move="Offer choices, not instructions",
                    judgement="MET",
                    score=5,
                    rationale="Choice was supported in the later turn.",
                    confidence=0.95,
                ),
            ],
            review_summary="The later success does not repair the earlier move.",
        )

        assessment = aggregate_geval_response_boundary(response, "agency")

        self.assertEqual(assessment.score, 2)
        self.assertEqual(len(assessment.events), 2)
        self.assertEqual(
            assessment.unresolved_missed_labels,
            ("Explore the peer's perspective",),
        )

    @override_settings(BOUNDARY_JUDGE_MODEL="test-judge")
    def test_final_run_persists_the_full_session_aggregate(self):
        first_user = self.session.messages.get(
            sender_type=Message.SenderTypes.USER
        )
        first_user.content = "Tell me what I should do."
        first_user.save(update_fields=["content"])
        Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.AI,
            content="I still do not know which option fits me.",
        )
        Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.USER,
            content="You could choose either option.",
        )
        response = GEvalResponse(
            turn_judgements=[
                GEvalTurnJudgement(
                    trainee_turn_number=1,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=["Tell me what I should do."],
                    evidence_interpretation=(
                        "The reply does not explore the help-seeker's perspective."
                    ),
                    expected_move_key="exploration",
                    expected_move="Explore the peer's perspective",
                    judgement="MISSED",
                    score=2,
                    rationale="The first exploration opportunity was missed.",
                    confidence=0.95,
                ),
                GEvalTurnJudgement(
                    trainee_turn_number=2,
                    boundary="AGENCY",
                    applicable=True,
                    evidence_quotes=["You could choose either option."],
                    evidence_interpretation="The reply offers a choice.",
                    expected_move_key="choice",
                    expected_move="Offer choices, not instructions",
                    judgement="MET",
                    score=5,
                    rationale="A later choice-supporting move was completed.",
                    confidence=0.96,
                ),
            ],
            review_summary="The earlier missed move remains unresolved.",
        )
        client = Mock()
        client.responses.parse.return_value = SimpleNamespace(
            id="resp_final_full_session",
            output_parsed=response,
        )

        result = run_geval_session(
            self.session,
            model="test-judge",
            client=client,
            purpose=BoundaryEvaluationRun.Purposes.FINAL,
        )
        run = BoundaryEvaluationRun.objects.get(pk=result.evaluation_run_id)

        self.assertEqual(run.aggregate_scores["AGENCY"], 2)
        self.assertEqual(run.turn_evaluations.count(), 2)
        self.assertEqual(
            list(
                run.turn_evaluations.order_by("trainee_turn_number").values_list(
                    "judgement",
                    flat=True,
                )
            ),
            [
                BoundaryTurnEvaluation.Judgements.MISSED,
                BoundaryTurnEvaluation.Judgements.MET,
            ],
        )

    @override_settings(BOUNDARY_JUDGE_MODEL="test-judge")
    def test_final_feedback_uses_a_separate_final_run_and_all_turn_evidence(self):
        first_ai = self.session.messages.filter(
            sender_type=Message.SenderTypes.AI
        ).first()
        first_user = self.session.messages.filter(
            sender_type=Message.SenderTypes.USER
        ).first()
        second_ai = Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.AI,
            content="I still feel unsure about what matters to me.",
        )
        second_user = Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.USER,
            content="Which option fits what matters most to you?",
        )
        BoundaryEvaluationRun.objects.create(
            session=self.session,
            evaluator_version=GEVAL_VERSION,
            prompt_version=GEVAL_PROMPT_VERSION,
            rubric_version=EVIDENCE_RUBRIC_VERSION,
            model="test-judge",
            purpose=BoundaryEvaluationRun.Purposes.LIVE,
            status=BoundaryEvaluationRun.Statuses.SUCCEEDED,
            resolution_status=BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED,
            production_ready=True,
            evaluated_through_message=second_user,
            resolved_scores={"AGENCY": 5},
            resolution_details={"AGENCY": {"decision": "LLM_ACCEPTED"}},
        )
        final_run = BoundaryEvaluationRun.objects.create(
            session=self.session,
            evaluator_version=GEVAL_VERSION,
            prompt_version=GEVAL_PROMPT_VERSION,
            rubric_version=EVIDENCE_RUBRIC_VERSION,
            model="test-judge",
            purpose=BoundaryEvaluationRun.Purposes.FINAL,
            status=BoundaryEvaluationRun.Statuses.SUCCEEDED,
            resolution_status=BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED,
            production_ready=True,
            evaluated_through_message=second_user,
            resolved_scores={"AGENCY": 3},
            resolution_details={"AGENCY": {"decision": "LLM_ACCEPTED"}},
        )
        BoundaryTurnEvaluation.objects.create(
            run=final_run,
            trainee_message=first_user,
            help_seeker_message=first_ai,
            trainee_turn_number=1,
            boundary_key="AGENCY",
            applicable=True,
            evidence_quotes=["What matters most to you about each option?"],
            expected_move_key="exploration",
            expected_move="Explore the peer's perspective",
            judgement=BoundaryTurnEvaluation.Judgements.MISSED,
            score=2,
            rationale="The first opportunity remained incomplete.",
            confidence=0.92,
        )
        BoundaryTurnEvaluation.objects.create(
            run=final_run,
            trainee_message=second_user,
            help_seeker_message=second_ai,
            trainee_turn_number=2,
            boundary_key="AGENCY",
            applicable=True,
            evidence_quotes=["Which option fits what matters most to you?"],
            expected_move_key="exploration",
            expected_move="Explore the peer's perspective",
            judgement=BoundaryTurnEvaluation.Judgements.MET,
            score=5,
            rationale="The later turn repaired the exploration move.",
            confidence=0.96,
        )

        calibrated = _calibrate_feedback_payload(
            self.session,
            FeedbackPayload(
                boundary_scores=[],
                what_worked=[],
                next_boundary_moves=[],
                review_items=[],
            ),
        )
        agency = next(
            item
            for item in calibrated.boundary_scores
            if item.boundary_key == "AGENCY"
        )

        self.assertEqual(agency.score, 3)
        self.assertIn("Turn 1", agency.evidence)
        self.assertIn("Turn 2", agency.evidence)
        self.assertIn(
            "Which option fits what matters most to you?",
            agency.evidence,
        )

    @override_settings(BOUNDARY_JUDGE_ENABLED=True)
    def test_chat_turn_triggers_one_reliable_judge_workflow(self):
        def fake_help_seeker_reply(session, _content):
            return Message.objects.create(
                session=session,
                sender_type=Message.SenderTypes.AI,
                content="I think being able to choose for myself matters most.",
            )

        with (
            patch(
                "training.views.generate_ai_reply",
                side_effect=fake_help_seeker_reply,
            ),
            patch("training.views.ensure_reliable_geval_run") as judge,
        ):
            result = _process_chat_turn(
                self.session,
                "Would you like to explore what each option would mean for you?",
                max_exchanges=5,
            )

        self.assertTrue(result["ok"])
        judge.assert_called_once()
        self.assertEqual(
            judge.call_args.kwargs["purpose"],
            BoundaryEvaluationRun.Purposes.LIVE,
        )
