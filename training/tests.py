from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from training.content import (
    BOUNDARY_DIMENSION_ORDER,
    BOUNDARY_FRAMEWORK,
    DEFAULT_PROMPT_CONTEXTS,
    DEFAULT_PROMPT_METHODS,
    PLACEHOLDER_REPLY_VARIANTS,
    SCENARIO_LIBRARY,
    SCENARIO_PROMPT_CONTEXTS,
    auto_trainee_reply_for,
    boundary_framework_for,
    default_prompt_parts,
    scenario_progress_step,
    scenario_prompt_context_for,
)
from training.forms import PromptForm
from training.models import (
    BoundaryEvaluationRun,
    BoundaryTurnEvaluation,
    Feedback,
    FeedbackReviewItem,
    FeedbackScore,
    Message,
    PlatformSetting,
    Prompt,
    Reflection,
    ReflectionGuidance,
    Scenario,
    TrainingSession,
)
from training.services.ai import (
    build_boundary_map_indicator,
    BoundaryStatusIndicator,
    BoundaryScorePayload,
    FeedbackPayload,
    FeedbackReviewItemPayload,
    GeneratedTraineeReply,
    ReflectionGuidancePayload,
    _clean_generated_text,
    _apply_feedback_payload,
    _fallback_notice_text,
    _fallback_reason_for_exception,
    _looks_incomplete_roleplay_reply,
    _openai_client,
    _placeholder_roleplay_reply,
    _roleplay_quality_issue,
    _trainee_quick_reply_system_prompt,
    build_boundary_status_indicator,
    build_roleplay_runtime_brief,
    generate_ai_opening,
    generate_ai_reply,
    generate_demo_trainee_reply,
    generate_feedback,
    generate_reflection_guidance,
    get_prompt_for_type,
    final_feedback_assessments,
)
from training.services.boundary_scoring import (
    BoundaryRubricResult,
    classify_relationship_response,
    evaluate_boundary_dimension,
    evaluate_boundary_session,
    conservative_median_score,
)
from training.services.progress import (
    admin_analytics_snapshot,
    progress_snapshot,
    recalculate_user_progress,
)
from training.services.geval import (
    EVIDENCE_RUBRIC_VERSION,
    GEVAL_PROMPT_VERSION,
    GEVAL_VERSION,
)


User = get_user_model()


def boundary_turn_result(
    move_key,
    outcome,
    *,
    major=False,
    critical=False,
    observed=True,
):
    return BoundaryRubricResult(
        key="agency",
        observed=observed,
        score=1 if critical else 2 if major or outcome == "missed" else 3,
        criteria=(),
        major_concerns=("Major concern",) if major else (),
        critical_breaches=("Critical breach",) if critical else (),
        rationale="Deterministic test event.",
        evidence_quotes=("Test trainee reply.",) if observed else (),
        expected_move_key=move_key if observed else "",
        expected_move_label=move_key.replace("_", " ").title() if observed else "",
        outcome=outcome if observed else "not_applicable",
        outcome_label=outcome.replace("_", " ").title() if observed else "Not applicable",
    )


def create_user(email, role, password="SecurePass123!"):
    return User.objects.create_user(
        username=email,
        email=email,
        password=password,
        role=role,
        first_name="Test",
        last_name="User",
    )


class PromptModelTests(TestCase):
    def test_scenario_model_has_no_difficulty_field(self):
        field_names = {field.name for field in Scenario._meta.get_fields()}

        self.assertNotIn("difficulty", field_names)

    def test_only_one_prompt_per_type_stays_active(self):
        first = Prompt.objects.create(
            name="Prompt A",
            type=Prompt.PromptTypes.ROLE_PLAY,
            content="First prompt",
            is_active=True,
        )
        second = Prompt.objects.create(
            name="Prompt B",
            type=Prompt.PromptTypes.ROLE_PLAY,
            content="Second prompt",
            is_active=True,
        )

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)

    def test_scenario_specific_active_prompts_can_coexist(self):
        scenario_one = Scenario.objects.create(
            title="Scenario One",
            description="One",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="One",
            is_active=True,
        )
        scenario_two = Scenario.objects.create(
            title="Scenario Two",
            description="Two",
            boundary_type=Scenario.BoundaryTypes.INTEGRATED,
            estimated_duration_min=12,
            learning_objectives="Two",
            is_active=True,
        )
        prompt_one = Prompt.objects.create(
            name="Scenario One Role Prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=scenario_one,
            content="Prompt one",
            is_active=True,
        )
        prompt_two = Prompt.objects.create(
            name="Scenario Two Role Prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=scenario_two,
            content="Prompt two",
            is_active=True,
        )

        prompt_one.refresh_from_db()
        prompt_two.refresh_from_db()

        self.assertTrue(prompt_one.is_active)
        self.assertTrue(prompt_two.is_active)
        self.assertEqual(get_prompt_for_type(Prompt.PromptTypes.ROLE_PLAY, scenario_one), prompt_one)
        self.assertEqual(get_prompt_for_type(Prompt.PromptTypes.ROLE_PLAY, scenario_two), prompt_two)

    def test_inactive_scenario_prompt_is_not_used_when_active_global_fallback_exists(self):
        scenario = Scenario.objects.create(
            title="Prompt fallback scenario",
            description="Prompt selection check.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="Stay within role",
            is_active=True,
        )
        inactive_scenario_prompt = Prompt.objects.create(
            name="Inactive scenario prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=scenario,
            content="This prompt must not run.",
            is_active=False,
        )
        global_fallback = Prompt.objects.create(
            name="Active global fallback",
            type=Prompt.PromptTypes.ROLE_PLAY,
            content="Use the global fallback.",
            is_active=True,
        )

        selected = get_prompt_for_type(Prompt.PromptTypes.ROLE_PLAY, scenario)

        self.assertEqual(selected, global_fallback)
        self.assertNotEqual(selected, inactive_scenario_prompt)

    def test_feedback_and_reflection_prefer_scenario_prompt_then_global_fallback(self):
        scenario = Scenario.objects.create(
            title="Scenario prompt selection",
            description="Check scenario prompt selection.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=12,
            learning_objectives="Name a sustainable limit",
            is_active=True,
        )
        for prompt_type in (
            Prompt.PromptTypes.FEEDBACK,
            Prompt.PromptTypes.REFLECTION,
        ):
            global_prompt = Prompt.objects.create(
                name=f"Global {prompt_type} selection prompt",
                type=prompt_type,
                content="Global fallback method.",
                is_active=True,
            )
            scenario_prompt = Prompt.objects.create(
                name=f"Scenario {prompt_type} selection prompt",
                type=prompt_type,
                scenario=scenario,
                content="Scenario-specific method.",
                is_active=True,
            )

            self.assertEqual(
                get_prompt_for_type(prompt_type, scenario),
                scenario_prompt,
            )
            scenario_prompt.is_active = False
            scenario_prompt.save()
            self.assertEqual(
                get_prompt_for_type(prompt_type, scenario),
                global_prompt,
            )


@override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="google")
class ScenarioStartTests(TestCase):
    ajax_headers = {
        "HTTP_ACCEPT": "application/json",
        "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
    }

    def setUp(self):
        PlatformSetting.get_solo()
        self.trainee = create_user("trainee@example.com", User.Roles.TRAINEE)
        self.scenario = Scenario.objects.create(
            title="Emotional Boundary",
            description="Practice empathy with healthy boundaries.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=12,
            learning_objectives="Acknowledge feelings\nAvoid over-identifying",
            is_active=True,
        )

    def test_starting_scenario_creates_empty_session_for_async_opening(self):
        self.client.force_login(self.trainee)

        with patch("training.views.generate_ai_opening") as mock_opening:
            response = self.client.post(
                reverse("scenarios"),
                {"scenario_id": self.scenario.id},
            )

        session = TrainingSession.objects.get(user=self.trainee)
        self.assertRedirects(response, reverse("chat", kwargs={"session_id": session.id}))
        self.assertEqual(session.prompt.type, Prompt.PromptTypes.ROLE_PLAY)
        self.assertEqual(session.prompt.scenario, self.scenario)
        self.assertTrue(session.prompt.is_active)
        self.assertEqual(session.messages.count(), 0)
        mock_opening.assert_not_called()

    def test_new_session_chat_page_requests_the_opening_asynchronously(self):
        self.client.force_login(self.trainee)

        self.client.post(
            reverse("scenarios"),
            {"scenario_id": self.scenario.id},
        )

        session = TrainingSession.objects.get(user=self.trainee)
        response = self.client.get(reverse("chat", kwargs={"session_id": session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-needs-opening="true"', html=False)
        self.assertEqual(session.messages.count(), 0)

    def test_live_openings_use_different_backup_variants_when_api_is_unavailable(self):
        self.client.force_login(self.trainee)

        self.client.post(reverse("scenarios"), {"scenario_id": self.scenario.id})
        self.client.post(reverse("scenarios"), {"scenario_id": self.scenario.id})

        sessions = list(TrainingSession.objects.filter(user=self.trainee).order_by("id"))
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "start_roleplay"},
                **self.ajax_headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(session.messages.count(), 1)
            self.assertEqual(
                session.messages.get().response_source,
                Message.ResponseSources.PLACEHOLDER,
            )

        first_opening = sessions[0].messages.first().content
        second_opening = sessions[1].messages.first().content
        self.assertNotEqual(first_opening, second_opening)


class TrainingFlowBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        PlatformSetting.get_solo()
        cls.trainee = create_user("trainee@example.com", User.Roles.TRAINEE)
        cls.admin = create_user("admin@example.com", User.Roles.ADMIN)
        cls.scenario = Scenario.objects.create(
            title="Emotional Boundary",
            description="Practice empathy with healthy boundaries.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=12,
            learning_objectives="Acknowledge feelings\nAvoid over-identifying",
            is_active=True,
        )
        cls.other_scenario = Scenario.objects.create(
            title="Referral Boundary",
            description="Practice warm referral language.",
            boundary_type=Scenario.BoundaryTypes.SAFETY,
            estimated_duration_min=18,
            learning_objectives="Recognise risk\nSignpost clearly",
            is_active=True,
        )
        cls.role_prompt = Prompt.objects.create(
            name="Role Prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            content="Stay in role as the help-seeker.",
            is_active=True,
        )
        cls.feedback_prompt = Prompt.objects.create(
            name="Feedback Prompt",
            type=Prompt.PromptTypes.FEEDBACK,
            content="Assess the transcript with concise scores and comments.",
            is_active=True,
        )
        cls.reflection_prompt = Prompt.objects.create(
            name="Reflection Prompt",
            type=Prompt.PromptTypes.REFLECTION,
            content="Reflect on what you would repeat, change, and escalate.",
            is_active=True,
        )

    def create_session(self, *, scenario=None, status=TrainingSession.Statuses.IN_PROGRESS, overall_score=None):
        session = TrainingSession.objects.create(
            user=self.trainee,
            scenario=scenario or self.scenario,
            prompt=self.role_prompt,
            status=status,
            overall_score=overall_score,
            end_time=timezone.now() if status == TrainingSession.Statuses.COMPLETED else None,
        )
        return session

    def create_feedback_bundle(
        self,
        session,
        *,
        overall_score,
        agency,
        relationship,
        safety=3,
    ):
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=overall_score,
            strengths="Warm tone\nGood validation\nStayed calm",
            improvements="Ask one more open question\nSummarise earlier\nSignpost sooner",
            generation_source=Feedback.GenerationSources.OPENAI,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.AGENCY,
            score=agency,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.RELATIONSHIP,
            score=relationship,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.SAFETY,
            score=safety,
        )
        FeedbackReviewItem.objects.create(
            feedback=feedback,
            message=None,
            turn_index=1,
            message_excerpt="It sounds like you are carrying a lot right now.",
            issue_label="Could be more specific",
            why_it_matters="A more concrete response would help move the conversation forward.",
            better_reply="It sounds like you have been holding a lot by yourself. What feels hardest to sit with tonight?",
            source=Feedback.GenerationSources.OPENAI,
        )
        return feedback


@override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="google")
class TrainingFlowTests(TrainingFlowBase):
    ajax_headers = {
        "HTTP_ACCEPT": "application/json",
        "HTTP_X_REQUESTED_WITH": "XMLHttpRequest",
    }

    def test_ajax_start_roleplay_creates_one_live_opening_and_is_idempotent(self):
        session = self.create_session()
        self.client.force_login(self.trainee)

        def fake_opening(active_session):
            return Message.objects.create(
                session=active_session,
                sender_type=Message.SenderTypes.AI,
                content="I have been trying to hold everything together, but I feel exhausted.",
                response_source=Message.ResponseSources.OPENAI,
            )

        with patch("training.views.generate_ai_opening", side_effect=fake_opening) as mock_opening:
            first_response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "start_roleplay"},
                **self.ajax_headers,
            )
            second_response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "start_roleplay"},
                **self.ajax_headers,
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertIn("AI Help-seeker", first_response.json()["opening_message_html"])
        self.assertEqual(first_response.json()["response_source"], Message.ResponseSources.OPENAI)
        self.assertEqual(session.messages.count(), 1)
        mock_opening.assert_called_once_with(session)

    def test_ajax_start_roleplay_rejects_inconsistent_user_only_history(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="Hello?",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"action": "start_roleplay"},
            **self.ajax_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "opening_conflict")
        self.assertEqual(session.messages.count(), 1)

    def test_chat_post_adds_user_and_placeholder_ai_messages(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="How have things been feeling for you this week?",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"content": "I want to acknowledge what they are feeling."},
        )

        self.assertRedirects(response, reverse("chat", kwargs={"session_id": session.id}))
        self.assertEqual(session.messages.count(), 3)
        latest_message = session.messages.order_by("-created_at", "-id").first()
        self.assertEqual(latest_message.sender_type, Message.SenderTypes.AI)
        self.assertIn("It feels like everything has been building up", latest_message.content)
        self.assertEqual(latest_message.response_source, Message.ResponseSources.PLACEHOLDER)

    def test_ajax_chat_returns_rendered_messages_and_live_boundary_state(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="How have things been feeling for you this week?",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(self.trainee)

        def fake_ai_reply(active_session, user_message):
            return Message.objects.create(
                session=active_session,
                sender_type=Message.SenderTypes.AI,
                content="I am still unsure, but it helps that you asked.",
                response_source=Message.ResponseSources.OPENAI,
            )

        with patch("training.views.generate_ai_reply", side_effect=fake_ai_reply):
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"content": "<script>alert('unsafe')</script> I want to understand."},
                **self.ajax_headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        expected_keys = {
            "ok",
            "user_message_html",
            "ai_message_html",
            "boundary_coach_html",
            "user_turns",
            "max_exchanges",
            "boundary_score",
            "boundary_label",
            "notice",
            "can_send",
        }
        self.assertEqual(set(payload), expected_keys)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["user_turns"], 1)
        self.assertTrue(payload["can_send"])
        self.assertIn("&lt;script&gt;", payload["user_message_html"])
        self.assertNotIn("<script>", payload["user_message_html"])
        self.assertIn("AI Help-seeker", payload["ai_message_html"])
        self.assertIn("data-boundary-coach-live", payload["boundary_coach_html"])
        self.assertEqual(session.messages.count(), 3)

    def test_ajax_empty_chat_message_returns_400_without_writing_messages(self):
        session = self.create_session()
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"content": "   "},
            **self.ajax_headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(session.messages.count(), 0)

    def test_ajax_non_test_account_cannot_use_quick_reply(self):
        session = self.create_session()
        self.client.force_login(self.trainee)

        with patch("training.views.generate_demo_trainee_reply") as mock_generate_reply:
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "prepare_auto_reply", "quality": "helpful"},
                **self.ajax_headers,
            )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["code"], "test_reply_forbidden")
        self.assertEqual(session.messages.count(), 0)
        mock_generate_reply.assert_not_called()

    def test_ajax_completed_session_returns_409_without_writing_messages(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"content": "This should not be saved."},
            **self.ajax_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["can_send"])
        self.assertEqual(session.messages.count(), 0)

    def test_ajax_turn_limit_returns_409_before_writing_an_extra_message(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="My existing final turn.",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        platform_settings = PlatformSetting.get_solo()
        platform_settings.max_exchanges_per_session = 1
        platform_settings.save(update_fields=["max_exchanges_per_session"])
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"content": "This would exceed the limit."},
            **self.ajax_headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["can_send"])
        self.assertEqual(session.messages.count(), 1)

    def test_ajax_placeholder_reply_returns_200_with_transparent_notice(self):
        session = self.create_session()
        self.client.force_login(self.trainee)

        def fake_fallback_reply(active_session, user_message):
            message = Message.objects.create(
                session=active_session,
                sender_type=Message.SenderTypes.AI,
                content="This is a safe backup response.",
                response_source=Message.ResponseSources.PLACEHOLDER,
            )
            message.used_fallback = True
            message.fallback_notice = "OpenAI was unavailable, so this turn used a placeholder reply."
            return message

        with patch("training.views.generate_ai_reply", side_effect=fake_fallback_reply):
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"content": "I can stay for ten minutes."},
                **self.ajax_headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("placeholder reply", payload["notice"])
        self.assertIn("Backup reply", payload["ai_message_html"])

    def test_ajax_demo_quick_reply_is_prepared_without_writing_then_sent_normally(self):
        demo_trainee = create_user("alex.chen@university.ac.uk", User.Roles.TRAINEE)
        self.client.force_login(demo_trainee)

        for quality, reply_content in [
            ("helpful", "I can stay for ten minutes while we contact someone else."),
            ("unhelpful", "I will stay available all night and handle everything."),
        ]:
            with self.subTest(quality=quality):
                session = TrainingSession.objects.create(
                    user=demo_trainee,
                    scenario=self.scenario,
                    prompt=self.role_prompt,
                )
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.AI,
                    content="Can you stay available for me all night?",
                    response_source=Message.ResponseSources.SCRIPTED,
                )

                generated_reply = GeneratedTraineeReply(
                    content=reply_content,
                    response_source=Message.ResponseSources.OPENAI,
                )

                def fake_ai_reply(active_session, user_message):
                    return Message.objects.create(
                        session=active_session,
                        sender_type=Message.SenderTypes.AI,
                        content="That is how I feel right now.",
                        response_source=Message.ResponseSources.OPENAI,
                    )

                with (
                    patch(
                        "training.views.generate_demo_trainee_reply",
                        return_value=generated_reply,
                    ) as mock_generate_demo,
                    patch("training.views.generate_ai_reply", side_effect=fake_ai_reply),
                ):
                    prepared_response = self.client.post(
                        reverse("chat", kwargs={"session_id": session.id}),
                        {"action": "prepare_auto_reply", "quality": quality},
                        **self.ajax_headers,
                    )
                    self.assertEqual(session.messages.count(), 1)
                    prepared_payload = prepared_response.json()
                    sent_response = self.client.post(
                        reverse("chat", kwargs={"session_id": session.id}),
                        {
                            "action": "send",
                            "content": prepared_payload["generated_content"],
                            "generated_response_source": prepared_payload["response_source"],
                        },
                        **self.ajax_headers,
                    )

                self.assertEqual(prepared_response.status_code, 200)
                self.assertEqual(set(prepared_payload), {
                    "ok",
                    "generated_content",
                    "response_source",
                    "notice",
                    "can_send",
                })
                self.assertEqual(prepared_payload["generated_content"], reply_content)
                self.assertEqual(session.messages.count(), 3)
                self.assertEqual(sent_response.status_code, 200)
                sent_payload = sent_response.json()
                self.assertIn(reply_content, sent_payload["user_message_html"])
                self.assertIn("AI Help-seeker", sent_payload["ai_message_html"])
                mock_generate_demo.assert_called_once_with(session, quality)
                user_message = session.messages.get(sender_type=Message.SenderTypes.USER)
                self.assertEqual(user_message.response_source, Message.ResponseSources.OPENAI)

    def test_demo_test_account_can_auto_send_helpful_reply(self):
        demo_trainee = create_user("alex.chen@university.ac.uk", User.Roles.TRAINEE)
        session = TrainingSession.objects.create(
            user=demo_trainee,
            scenario=self.scenario,
            prompt=self.role_prompt,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like everything is getting heavier this week.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(demo_trainee)

        with patch(
            "training.views.generate_demo_trainee_reply",
            return_value=GeneratedTraineeReply(
                content="Thanks for telling me. What feels heaviest to carry on your own right now?",
                response_source=Message.ResponseSources.OPENAI,
            ),
        ):
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "auto_helpful"},
            )

        self.assertRedirects(response, reverse("chat", kwargs={"session_id": session.id}))
        user_messages = session.messages.filter(sender_type=Message.SenderTypes.USER)
        self.assertEqual(user_messages.count(), 1)
        self.assertEqual(user_messages.first().response_source, Message.ResponseSources.OPENAI)
        self.assertEqual(
            user_messages.first().content,
            "Thanks for telling me. What feels heaviest to carry on your own right now?",
        )
        self.assertEqual(session.messages.count(), 3)

    def test_auto_reply_rotation_uses_more_than_three_variants(self):
        seen = {
            auto_trainee_reply_for(Scenario.BoundaryTypes.RELATIONSHIP, "helpful", index)
            for index in range(6)
        }
        self.assertGreaterEqual(len(seen), 6)

    def test_quick_reply_prompt_separates_helpful_and_unhelpful_bands(self):
        burnout = Scenario.objects.create(
            title="Burnout Prompt Contrast",
            description="Prompt contrast check.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Can you stay on call for me this weekend? You are the only person I can talk to.",
            response_source=Message.ResponseSources.SCRIPTED,
        )

        helpful_prompt = _trainee_quick_reply_system_prompt(session, "helpful")
        unhelpful_prompt = _trainee_quick_reply_system_prompt(session, "unhelpful")

        self.assertIn("Band 4 to Band 5", helpful_prompt)
        self.assertIn("Band 1 to Band 2", unhelpful_prompt)
        self.assertIn("Do not produce a middling Band 3 answer", helpful_prompt)
        self.assertIn("Time & Availability Boundary", helpful_prompt)
        self.assertIn("over-promise availability", unhelpful_prompt)
        self.assertNotIn("Difficulty:", helpful_prompt)
        self.assertNotIn("Difficulty:", unhelpful_prompt)

    def test_non_test_account_cannot_use_auto_reply_shortcuts(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like everything is getting heavier this week.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(self.trainee)

        with patch("training.views.generate_demo_trainee_reply") as mock_generate_reply:
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "auto_helpful"},
                follow=True,
            )

        self.assertRedirects(response, reverse("chat", kwargs={"session_id": session.id}))
        self.assertContains(response, "Quick test replies are only available for the Alex Chen demo account.")
        self.assertEqual(session.messages.count(), 1)
        mock_generate_reply.assert_not_called()

    def test_registered_account_with_demo_in_email_cannot_use_auto_reply_shortcuts(self):
        registered_trainee = create_user("demo.student@example.com", User.Roles.TRAINEE)
        session = TrainingSession.objects.create(
            user=registered_trainee,
            scenario=self.scenario,
            prompt=self.role_prompt,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like everything is getting heavier this week.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(registered_trainee)

        with patch("training.views.generate_demo_trainee_reply") as mock_generate_reply:
            response = self.client.post(
                reverse("chat", kwargs={"session_id": session.id}),
                {"action": "auto_helpful"},
                follow=True,
            )

        self.assertRedirects(response, reverse("chat", kwargs={"session_id": session.id}))
        self.assertContains(response, "Quick test replies are only available for the Alex Chen demo account.")
        self.assertEqual(session.messages.count(), 1)
        mock_generate_reply.assert_not_called()

    def test_finishing_session_creates_feedback_and_scores(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel really overwhelmed lately.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="It sounds like you are carrying a lot right now.",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"action": "finish"},
            follow=True,
        )

        self.assertRedirects(response, reverse("session_review", kwargs={"session_id": session.id}))
        session.refresh_from_db()
        self.assertEqual(session.status, TrainingSession.Statuses.COMPLETED)
        self.assertTrue(hasattr(session, "feedback"))
        self.assertEqual(session.feedback.scores.count(), 3)
        self.assertEqual(session.rubric_version, "BOUNDARY-SKILLS-2.0")
        self.assertTrue(session.ended_early)
        self.assertNotContains(response, "Session finished. Review your feedback below.")

    def test_reflection_submission_redirects_to_history_detail(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED, overall_score=3)
        self.create_feedback_bundle(session, overall_score=3, agency=4, relationship=3)
        scenario_reflection_prompt = Prompt.objects.create(
            name="Emotional Boundary Reflection Prompt",
            type=Prompt.PromptTypes.REFLECTION,
            scenario=session.scenario,
            context="Reflect on the Agency Boundary evidence from this session.",
            method="Ask the trainee to identify one choice-supporting next move.",
            is_active=True,
        )
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("session_review", kwargs={"session_id": session.id}),
            {
                "reflection_text": (
                    "I would keep the supportive tone and ask one more open question "
                    "before offering reassurance."
                ),
                "action_plan": "I will name one boundary and one collaborative next step.",
            },
        )

        self.assertRedirects(
            response,
            reverse("history_detail", kwargs={"session_id": session.id}),
        )
        reflection = Reflection.objects.get(session=session)
        self.assertEqual(reflection.prompt, scenario_reflection_prompt)


class BoundarySkillsRubricTwoTests(TestCase):
    def assess(self, results):
        exchanges = [("Help-seeker turn", "Trainee turn")] * len(results)
        with patch(
            "training.services.boundary_scoring.evaluate_boundary_dimension",
            side_effect=results,
        ):
            return evaluate_boundary_session("agency", "agency", exchanges)

    def test_all_three_moves_met_without_error_is_score_five(self):
        assessment = self.assess(
            [
                boundary_turn_result("exploration", "met"),
                boundary_turn_result("choice", "met"),
                boundary_turn_result("focus", "met"),
            ]
        )

        self.assertEqual(assessment.score, 5)
        self.assertEqual(assessment.completed_count, 3)

    def test_two_moves_met_and_third_not_reached_is_score_four(self):
        assessment = self.assess(
            [
                boundary_turn_result("exploration", "met"),
                boundary_turn_result("choice", "met"),
            ]
        )

        self.assertEqual(assessment.score, 4)
        self.assertEqual(assessment.not_reached_labels, ("Keep the focus on the peer",))

    def test_two_moves_met_with_unresolved_miss_is_score_two(self):
        assessment = self.assess(
            [
                boundary_turn_result("exploration", "met"),
                boundary_turn_result("choice", "met"),
                boundary_turn_result("focus", "missed"),
            ]
        )

        self.assertEqual(assessment.score, 2)
        self.assertEqual(
            assessment.unresolved_missed_labels,
            ("Keep the focus on the peer",),
        )

    def test_one_safe_completed_move_is_score_three(self):
        assessment = self.assess([boundary_turn_result("exploration", "met")])

        self.assertEqual(assessment.score, 3)

    def test_repaired_miss_with_all_moves_complete_is_score_four(self):
        assessment = self.assess(
            [
                boundary_turn_result("exploration", "missed"),
                boundary_turn_result("exploration", "met"),
                boundary_turn_result("choice", "met"),
                boundary_turn_result("focus", "met"),
            ]
        )

        self.assertEqual(assessment.score, 4)
        self.assertEqual(
            assessment.repaired_missed_labels,
            ("Explore the peer's perspective",),
        )

    def test_repaired_major_concern_is_capped_at_score_three(self):
        assessment = self.assess(
            [
                boundary_turn_result("exploration", "concern", major=True),
                boundary_turn_result("exploration", "met"),
                boundary_turn_result("choice", "met"),
                boundary_turn_result("focus", "met"),
            ]
        )

        self.assertEqual(assessment.score, 3)
        self.assertTrue(assessment.repaired_major_concern)

    def test_critical_breach_is_always_score_one(self):
        assessment = self.assess(
            [
                boundary_turn_result("exploration", "concern", critical=True),
                boundary_turn_result("choice", "met"),
                boundary_turn_result("focus", "met"),
            ]
        )

        self.assertEqual(assessment.score, 1)
        self.assertTrue(assessment.critical_breach)

    def test_no_genuine_opportunity_is_not_observed(self):
        assessment = self.assess(
            [boundary_turn_result("", "not_applicable", observed=False)]
        )

        self.assertFalse(assessment.observed)
        self.assertIsNone(assessment.score)

    def test_integrated_scenario_recognises_indirect_third_party_safety_risk(self):
        assessment = evaluate_boundary_session(
            "safety",
            "integrated",
            [
                (
                    "They sent a message that sounded like they might not be safe "
                    "tonight, and I am worried there is a real risk.",
                    "Can you tell me their name and exactly what happened?",
                )
            ],
        )

        self.assertTrue(assessment.observed)
        self.assertEqual(assessment.score, 2)
        self.assertEqual(assessment.events[0].move_key, "safety_check")

    def test_conservative_composite_uses_middle_or_lower_score(self):
        self.assertEqual(conservative_median_score([5, 2, 4]), 4)
        self.assertEqual(conservative_median_score([5, 2]), 2)
        self.assertEqual(conservative_median_score([3]), 3)
        self.assertIsNone(conservative_median_score([None, None]))


class LLMServiceTests(TrainingFlowBase):
    @override_settings(
        BOUNDARY_JUDGE_MODEL="test-judge",
        OPENAI_MODEL="test-model",
    )
    def test_final_feedback_keeps_accepted_live_evidence_when_final_run_is_missing(self):
        integrated = Scenario.objects.create(
            title="Integrated Boundary",
            description="Combined boundary practice.",
            boundary_type=Scenario.BoundaryTypes.INTEGRATED,
            estimated_duration_min=18,
            learning_objectives="Combine boundary skills",
            is_active=True,
        )
        session = self.create_session(
            scenario=integrated,
            status=TrainingSession.Statuses.COMPLETED,
        )
        help_seeker = Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am overwhelmed and do not know what to do.",
            response_source=Message.ResponseSources.OPENAI,
        )
        trainee = Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="Tell me more about what happened.",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        run = BoundaryEvaluationRun.objects.create(
            session=session,
            evaluator_version=GEVAL_VERSION,
            prompt_version=GEVAL_PROMPT_VERSION,
            rubric_version=EVIDENCE_RUBRIC_VERSION,
            model="test-judge",
            purpose=BoundaryEvaluationRun.Purposes.LIVE,
            status=BoundaryEvaluationRun.Statuses.SUCCEEDED,
            resolution_status=BoundaryEvaluationRun.ResolutionStatuses.ACCEPTED,
            production_ready=True,
            evaluated_through_message=trainee,
            resolved_scores={"SAFETY": 2},
            resolution_details={"SAFETY": {"decision": "LLM_ACCEPTED"}},
        )
        BoundaryTurnEvaluation.objects.create(
            run=run,
            trainee_message=trainee,
            help_seeker_message=help_seeker,
            trainee_turn_number=1,
            boundary_key=BoundaryTurnEvaluation.BoundaryKeys.SAFETY,
            applicable=True,
            evidence_quotes=[trainee.content],
            expected_move_key="safety_check",
            expected_move="Check immediate safety",
            judgement=BoundaryTurnEvaluation.Judgements.MISSED,
            score=2,
            rationale="A genuine safety opportunity was not addressed.",
            confidence=0.96,
        )

        assessment = final_feedback_assessments(session)["safety"]

        self.assertTrue(assessment.observed)
        self.assertEqual(assessment.score, 2)

    @override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="")
    def test_notebook_conflict_marks_unobserved_score_for_review_without_assigning_level(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel overwhelmed by work.",
            response_source=Message.ResponseSources.OPENAI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="What part has felt hardest this week?",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
        )
        payload = FeedbackPayload(
            boundary_scores=[],
            what_worked=["The response stayed curious."],
            next_boundary_moves=["Keep exploring the peer's perspective."],
            review_items=[
                FeedbackReviewItemPayload(
                    trainee_turn_number=1,
                    boundary_key=FeedbackReviewItem.BoundaryKeys.SAFETY,
                    issue_label="Possible safety opportunity",
                    why_it_matters="The wording may warrant a closer safety review.",
                    better_reply="Can I check whether you feel safe right now?",
                )
            ],
        )

        _apply_feedback_payload(
            feedback,
            session,
            self.feedback_prompt,
            payload,
            Feedback.GenerationSources.OPENAI,
            allow_judge=False,
        )

        safety = feedback.scores.get(criterion=FeedbackScore.Criteria.SAFETY)
        self.assertFalse(safety.observed)
        self.assertIsNone(safety.score)
        self.assertTrue(safety.review_required)
        self.assertIn("needs review", safety.rationale.lower())

        self.client.force_login(self.trainee)
        response = self.client.get(
            reverse("session_review", kwargs={"session_id": session.id})
        )
        self.assertContains(response, "Needs review")

    @override_settings(
        OPENAI_API_KEY="test-key",
        LLM_TIMEOUT_SECONDS=12,
        LLM_MAX_RETRIES=0,
    )
    def test_openai_client_uses_bounded_timeout_and_retry_settings(self):
        with patch("training.services.ai.OpenAI") as client_class:
            _openai_client()

        client_class.assert_called_once_with(
            api_key="test-key",
            timeout=12,
            max_retries=0,
        )

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_ai_opening_calls_openai_without_pretending_the_trainee_spoke(self):
        session = self.create_session()

        class FakeResponses:
            def __init__(self):
                self.last_kwargs = None

            def create(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(
                    output_text="I have been holding everything together for everyone else, and I feel close to falling apart."
                )

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            message = generate_ai_opening(session)

        self.assertEqual(message.sender_type, Message.SenderTypes.AI)
        self.assertEqual(message.response_source, Message.ResponseSources.OPENAI)
        self.assertFalse(message.used_fallback)
        self.assertEqual(session.messages.count(), 1)
        self.assertEqual(fake_responses.last_kwargs["model"], "test-model")
        developer_prompt = fake_responses.last_kwargs["input"][0]["content"]
        self.assertIn("The trainee has not spoken yet", developer_prompt)
        self.assertIn(self.role_prompt.content, developer_prompt)
        self.assertNotIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            developer_prompt,
        )
        self.assertIn("Opening-turn rules", developer_prompt)
        self.assertNotIn("Learning objectives:", developer_prompt)
        self.assertNotIn("Boundary behaviour contract", developer_prompt)
        self.assertNotIn("CMCV framework:", developer_prompt)
        self.assertNotIn("EDITABLE CONTEXT", developer_prompt)
        self.assertIn("Never mention CMCV", developer_prompt)

    @override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="openai")
    def test_generate_ai_opening_uses_transparent_backup_when_api_is_unavailable(self):
        session = self.create_session()

        message = generate_ai_opening(session)

        self.assertTrue(message.used_fallback)
        self.assertEqual(message.response_source, Message.ResponseSources.PLACEHOLDER)
        self.assertTrue(message.fallback_notice)
        self.assertEqual(session.messages.count(), 1)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_ai_opening_rechecks_for_an_existing_message_after_model_returns(self):
        session = self.create_session()

        def overlapping_opening(active_session, prompt):
            Message.objects.create(
                session=active_session,
                sender_type=Message.SenderTypes.AI,
                content="Opening saved by the overlapping request.",
                response_source=Message.ResponseSources.OPENAI,
            )
            return "This duplicate result must not be saved."

        with patch(
            "training.services.ai._openai_role_play_opening",
            side_effect=overlapping_opening,
        ):
            message = generate_ai_opening(session)

        self.assertEqual(message.content, "Opening saved by the overlapping request.")
        self.assertEqual(session.messages.count(), 1)

    def test_quota_error_is_classified_for_clear_gemini_fallback_notice(self):
        exc = Exception(
            "429 RESOURCE_EXHAUSTED. Quota exceeded for metric. Please retry in 46.4s."
        )
        reason = _fallback_reason_for_exception(exc)
        notice = _fallback_notice_text(reason, "google", exc)

        self.assertEqual(reason, "quota_exhausted")
        self.assertIn("Gemini quota, billing access, or rate limits blocked this request", notice)
        self.assertIn("46 seconds", notice)

    def test_openai_configuration_error_has_openai_specific_notice(self):
        exc = Exception("OPENAI_API_KEY is not configured.")
        reason = _fallback_reason_for_exception(exc)
        notice = _fallback_notice_text(reason, "openai", exc)

        self.assertEqual(reason, "configuration")
        self.assertEqual(
            notice,
            "OpenAI is not configured correctly right now, so this turn used a placeholder reply.",
        )

    def test_clean_generated_text_removes_role_prefixes_and_blank_lines(self):
        self.assertEqual(
            _clean_generated_text("AI Help-seeker:\n\nOh, thank god. I really needed that."),
            "Oh, thank god. I really needed that.",
        )
        self.assertEqual(
            _clean_generated_text("Assistant:\n\nI still do not know what to do."),
            "I still do not know what to do.",
        )

    def test_roleplay_framework_label_is_rejected_and_removed_before_saving(self):
        from training.services.ai import _roleplay_quality_issue, _strip_roleplay_framework_prefix

        session = self.create_session()
        leaked_reply = "CMCV-1.0\nI do not know what I am supposed to do next."

        self.assertEqual(_roleplay_quality_issue(session, leaked_reply), "internal_instruction_leak")
        self.assertEqual(
            _strip_roleplay_framework_prefix(leaked_reply),
            "I do not know what I am supposed to do next.",
        )

    def test_roleplay_reply_that_teaches_the_expected_answer_is_rejected(self):
        session = self.create_session()

        self.assertEqual(
            _roleplay_quality_issue(
                session,
                "Please do not tell me what to do. I need you to listen rather than decide for me.",
            ),
            "coaching_leak",
        )
        self.assertEqual(
            _roleplay_quality_issue(
                session,
                "I keep going round in circles, and I am not sure which part to talk through first.",
            ),
            "",
        )

    def test_roleplay_runtime_prompt_excludes_protected_scoring_contract(self):
        from training.services.ai import _roleplay_system_prompt

        session = self.create_session()
        prompt_text = _roleplay_system_prompt(
            session,
            self.role_prompt,
            "I am listening. What feels most difficult right now?",
        )

        self.assertNotIn("Learning objectives:", prompt_text)
        self.assertNotIn("Boundary behaviour contract", prompt_text)
        self.assertNotIn("Level 5", prompt_text)

    @override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="google")
    def test_generate_ai_reply_marks_placeholder_fallback(self):
        session = self.create_session(scenario=self.other_scenario)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I was hoping talking to another student would feel easier.",
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I want to understand what feels hardest right now.",
        )

        message = generate_ai_reply(session, "I want to understand what feels hardest right now.")

        self.assertTrue(getattr(message, "used_fallback", False))
        self.assertTrue(message.content)
        self.assertEqual(message.response_source, Message.ResponseSources.PLACEHOLDER)
        self.assertIn(
            message.content,
            PLACEHOLDER_REPLY_VARIANTS[Scenario.BoundaryTypes.SAFETY],
        )

    def test_incomplete_reply_detector_flags_truncated_outputs(self):
        self.assertTrue(_looks_incomplete_roleplay_reply("Oh, thank goodness. I really don"))
        self.assertTrue(_looks_incomplete_roleplay_reply("Oh, I meant I don'"))
        self.assertTrue(_looks_incomplete_roleplay_reply("4"))
        self.assertTrue(_looks_incomplete_roleplay_reply("Score 1"))
        self.assertFalse(_looks_incomplete_roleplay_reply("I really do not want to be on my own tonight."))

    def test_runtime_brief_opens_more_when_trainee_is_empathic_and_exploratory(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I have been feeling really overwhelmed this week.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="That sounds really heavy. What feels most difficult right now?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        runtime = build_roleplay_runtime_brief(
            session,
            "That sounds really heavy. What feels most difficult right now?",
        )

        self.assertEqual(runtime.stage_label, "Opening")
        self.assertGreaterEqual(runtime.empathy_score, 1)
        self.assertGreaterEqual(runtime.exploration_score, 1)
        self.assertIn("Open up one level", runtime.disclosure_instruction)

    def test_runtime_brief_increases_dependency_pressure_for_burnout_overpromising(self):
        burnout = Scenario.objects.create(
            title="Burnout & Compassion Fatigue",
            description="Practice setting sustainable limits.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Can you stay on call with me tonight?",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I will stay with you all night and you can message me anytime.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        runtime = build_roleplay_runtime_brief(
            session,
            "I will stay with you all night and you can message me anytime.",
        )

        self.assertGreaterEqual(runtime.overpromise_score, 1)
        self.assertIn("Lean in a little more", runtime.emotional_shift)
        self.assertIn("one believable step", runtime.target_pressure_instruction)

    def test_relationship_runtime_uses_natural_overcommitment_before_turn_progression(self):
        burnout = Scenario.objects.create(
            title="Natural relationship overcommitment",
            description="Detect realistic vague availability.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        trainee_reply = (
            "Yes, of course - you can message me again later tonight if you need to, "
            "and I'll do my best to keep an eye out."
        )

        runtime = build_roleplay_runtime_brief(session, trainee_reply)

        self.assertEqual(runtime.relationship_state, "OVERCOMMITMENT")
        self.assertIn("one believable step", runtime.target_pressure_instruction)
        self.assertNotIn("backup support", runtime.target_pressure_instruction)

    def test_relationship_runtime_clarifies_a_mixed_limit(self):
        burnout = Scenario.objects.create(
            title="Mixed relationship limit",
            description="Detect contradictory availability.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        trainee_reply = (
            "I can check in once before I log off, and I'll try to stay reachable after that too. "
            "Message me if you need to and I'll respond as soon as I can."
        )

        runtime = build_roleplay_runtime_brief(session, trainee_reply)

        self.assertEqual(runtime.relationship_state, "MIXED_BOUNDARY")
        self.assertIn("when contact ends", runtime.target_pressure_instruction)

    def test_relationship_quality_guard_rejects_help_seeker_coaching_after_overcommitment(self):
        burnout = Scenario.objects.create(
            title="Relationship branch guard",
            description="Keep the help-seeker in role.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        issue = _roleplay_quality_issue(
            session,
            "Could you help me find someone else so I do not rely on you?",
            "Keep messaging me tonight and I will reply as soon as I can.",
        )

        self.assertEqual(issue, "relationship_branch_mismatch")

    def test_relationship_placeholder_follows_the_detected_response_state(self):
        burnout = Scenario.objects.create(
            title="Relationship fallback state",
            description="Keep backup replies behaviour-contingent.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)

        reply = _placeholder_roleplay_reply(
            session,
            "You can message me later tonight and I will do my best to respond.",
        )

        self.assertIn("message you again after you log off", reply)
        self.assertNotIn("other person", reply)

    def test_runtime_brief_makes_missed_safety_check_explicit_after_referral(self):
        session = self.create_session(scenario=self.other_scenario)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I do not know if I can keep managing this alone.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="The counselling service could talk this through with you.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        runtime = build_roleplay_runtime_brief(
            session,
            "The counselling service could talk this through with you.",
        )

        self.assertGreaterEqual(runtime.referral_score, 1)
        self.assertIn("not sure you can stay safe tonight", runtime.target_pressure_instruction)

    def test_each_scenario_has_a_distinct_targeted_elicitation_sequence(self):
        expected_labels = {
            Scenario.BoundaryTypes.AGENCY: "Uncertain decision",
            Scenario.BoundaryTypes.RELATIONSHIP: "Availability request",
            Scenario.BoundaryTypes.SAFETY: "Serious distress cue",
            Scenario.BoundaryTypes.INTEGRATED: "Choice under uncertainty",
        }
        for boundary_type, expected_label in expected_labels.items():
            with self.subTest(boundary_type=boundary_type):
                context = scenario_prompt_context_for(
                    Prompt.PromptTypes.ROLE_PLAY,
                    boundary_type,
                )
                first_step = scenario_progress_step(boundary_type, 1)

                self.assertTrue(context)
                self.assertEqual(first_step["label"], expected_label)

    def test_runtime_brief_detects_chinese_empathy_and_exploration_markers(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="我这周真的有点撑不住了。",
            response_source=Message.ResponseSources.SCRIPTED,
        )

        runtime = build_roleplay_runtime_brief(
            session,
            "听起来真的很不容易。对你来说现在最难的是什么？",
        )

        self.assertGreaterEqual(runtime.empathy_score, 1)
        self.assertGreaterEqual(runtime.exploration_score, 1)

    def test_boundary_status_indicator_rewards_warm_chinese_limit_setting(self):
        burnout = Scenario.objects.create(
            title="Burnout & Compassion Fatigue",
            description="Practice setting sustainable limits.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="你今晚能一直陪着我吗？",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="我很关心你，但我今晚不能一直在线。我们一起想一个今晚还能联系的人，好吗？",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        indicator = build_boundary_status_indicator(session)

        self.assertIsInstance(indicator, BoundaryStatusIndicator)
        self.assertEqual(indicator.score, 3)
        self.assertEqual(indicator.label, "Mixed")

    def test_boundary_status_indicator_flags_chinese_overpromising(self):
        burnout = Scenario.objects.create(
            title="Burnout & Compassion Fatigue",
            description="Practice setting sustainable limits.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="你今晚能一直陪着我吗？",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="我会整晚陪着你，你随时给我发消息就好。",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        indicator = build_boundary_status_indicator(session)

        self.assertIsInstance(indicator, BoundaryStatusIndicator)
        self.assertLessEqual(indicator.score or 5, 2)
        self.assertIn(indicator.label, {"At risk", "Wobbling"})

    def test_auto_helpful_reply_templates_start_safely_across_scenarios(self):
        scenario_rows = [
            (
                Scenario.BoundaryTypes.AGENCY,
                "Emotional Boundary",
                "I feel like everything is getting heavier this week.",
            ),
            (
                Scenario.BoundaryTypes.INTEGRATED,
                "Privacy Boundary",
                "I am worried about a friend, but I do not know how much I should say.",
            ),
            (
                Scenario.BoundaryTypes.SAFETY,
                "Referral Boundary",
                "Some days I really do not want to be here anymore and I do not know who to talk to.",
            ),
            (
                Scenario.BoundaryTypes.RELATIONSHIP,
                "Burnout & Compassion Fatigue",
                "Can you stay on call for me this weekend? You are the only person I can talk to.",
            ),
        ]

        for boundary_type, title, opening in scenario_rows:
            with self.subTest(boundary_type=boundary_type):
                scenario = Scenario.objects.create(
                    title=f"{title} Template Calibration",
                    description="Template calibration check.",
                    boundary_type=boundary_type,
                    estimated_duration_min=15,
                    learning_objectives="Stay supportive\nStay within scope",
                    is_active=True,
                )
                session = self.create_session(scenario=scenario)
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.AI,
                    content=opening,
                    response_source=Message.ResponseSources.SCRIPTED,
                )
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.USER,
                    content=auto_trainee_reply_for(boundary_type, "helpful", 0),
                    response_source=Message.ResponseSources.PLACEHOLDER,
                )

                indicator = build_boundary_status_indicator(session)

                self.assertEqual(
                    indicator.score,
                    3,
                    (
                        f"{boundary_type} first-turn helpful template should be safe, "
                        "without claiming later process moves were completed."
                    ),
                )

    def test_boundary_status_indicator_stays_out_of_risk_when_boundary_map_is_mostly_mixed_or_better(self):
        burnout = Scenario.objects.create(
            title="Burnout Status Alignment",
            description="Keep the overall status aligned with the live boundary map.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I know you said that, but I still feel scared about being on my own tonight.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content=(
                "I care about you, and I can stay with you for five minutes while we think "
                "about who else you can contact tonight. I cannot be your only support for "
                "the whole evening, but I want to help you make one safer plan."
            ),
            response_source=Message.ResponseSources.USER_INPUT,
        )

        indicator = build_boundary_status_indicator(session)
        boundary_map = build_boundary_map_indicator(session)
        lowest_dimension = min(
            item.score for item in boundary_map.dimensions if item.observed
        )

        self.assertGreaterEqual(lowest_dimension, 3)
        self.assertGreaterEqual(indicator.score or 0, 3)
        self.assertNotEqual(indicator.label, "At risk")

    def test_boundary_status_indicator_penalises_invalidating_reassurance(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like I am falling apart and do not know where to begin.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="You will be fine, everyone gets stressed sometimes. What happened?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        indicator = build_boundary_status_indicator(session)

        self.assertIsInstance(indicator, BoundaryStatusIndicator)
        self.assertLessEqual(indicator.score or 5, 2)
        self.assertIn(indicator.label, {"At risk", "Wobbling"})

    def test_boundary_map_highlights_availability_boundary_when_limits_are_warm_and_clear(self):
        burnout = Scenario.objects.create(
            title="Burnout Boundary Map",
            description="Practice setting sustainable limits.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Can you stay on call with me all weekend?",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I care about you, but I cannot stay on call all weekend. I can help you think about one other person you could contact tonight.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        availability = next(item for item in boundary_map.dimensions if item.key == "relationship")

        self.assertEqual(availability.score, 3)
        self.assertEqual(availability.expected_move_label, "State a clear and sustainable limit")
        self.assertEqual(availability.outcome, "met")
        self.assertEqual(len(availability.evidence_met), 1)
        self.assertFalse(availability.critical_breaches)

    def test_boundary_map_flags_focus_boundary_when_reply_shifts_to_supporter_story_and_advice(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like I am falling apart and do not know where to begin.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="When I went through this, I just pushed through it. You should do the same and try not to dwell on it.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        focus = next(item for item in boundary_map.dimensions if item.key == "agency")

        self.assertLessEqual(focus.score or 5, 2)
        self.assertTrue(focus.major_concerns or focus.critical_breaches)

    def test_integrated_boundary_map_uses_the_three_scored_dimensions(self):
        privacy = Scenario.objects.create(
            title="Privacy Boundary Map",
            description="Practice privacy-safe peer support wording.",
            boundary_type=Scenario.BoundaryTypes.INTEGRATED,
            estimated_duration_min=15,
            learning_objectives="Protect identities\nKeep the focus on the dilemma",
            is_active=True,
        )
        session = self.create_session(scenario=privacy)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I need to tell you what happened with my flatmate, but I do not know if I should say who it is.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="You do not need to name them for us to think this through safely. What feels most important about what happened?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        self.assertEqual(
            [item.key for item in boundary_map.dimensions],
            ["agency", "relationship", "safety"],
        )
        self.assertFalse(
            next(item for item in boundary_map.dimensions if item.key == "agency").observed
        )
        self.assertTrue(
            next(item for item in boundary_map.dimensions if item.key == "relationship").observed
        )
        self.assertFalse(
            next(item for item in boundary_map.dimensions if item.key == "safety").observed
        )

    def test_boundary_map_flags_scope_boundary_when_risk_is_met_with_warm_escalation(self):
        referral = Scenario.objects.create(
            title="Referral Boundary Map",
            description="Practice warm referral language.",
            boundary_type=Scenario.BoundaryTypes.SAFETY,
            estimated_duration_min=18,
            learning_objectives="Recognise risk\nSignpost clearly",
            is_active=True,
        )
        session = self.create_session(scenario=referral)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Some days I do not want to be here anymore and I do not know who to talk to.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I am really glad you told me. You do not have to hold this alone tonight. Would it help if we think about one specific support option or helpline together?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        scope = next(item for item in boundary_map.dimensions if item.key == "safety")

        self.assertEqual(scope.expected_move_label, "Check immediate safety")
        self.assertEqual(scope.outcome, "partly_met")
        self.assertEqual(scope.score, 3)

    def test_safety_process_advances_only_after_the_relevant_move_is_met(self):
        referral = Scenario.objects.create(
            title="Safety staged process",
            description="Evaluate one relevant safety move per turn.",
            boundary_type=Scenario.BoundaryTypes.SAFETY,
            estimated_duration_min=18,
            learning_objectives="Check safety\nStay connected\nAgree action",
            is_active=True,
        )
        session = self.create_session(scenario=referral)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am not sure I can stay safe tonight.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I am glad you told me. Are you safe right now, or might you hurt yourself tonight?",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I do not have a plan, but I am frightened and do not want to handle this alone.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I am here with you. We can take this one step at a time while we work out who to contact.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        safety = next(
            item
            for item in build_boundary_map_indicator(session).dimensions
            if item.key == "safety"
        )

        self.assertEqual(safety.expected_move_label, "Stay connected")
        self.assertEqual(safety.outcome, "met")
        statuses = {item.key: item.status for item in safety.process_steps}
        self.assertEqual(statuses["safety_check"], "met")
        self.assertEqual(statuses["connection"], "met")
        self.assertEqual(statuses["action"], "not_reached")

    def test_safety_action_requires_an_agreed_action_not_only_a_service_name(self):
        referral = Scenario.objects.create(
            title="Safety agreed action",
            description="Distinguish signposting from an agreed next step.",
            boundary_type=Scenario.BoundaryTypes.SAFETY,
            estimated_duration_min=18,
            learning_objectives="Agree action",
            is_active=True,
        )
        session = self.create_session(scenario=referral)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am safe right now, but I do not know how to make the call.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="A counselling service is available.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        safety = next(
            item
            for item in build_boundary_map_indicator(session).dimensions
            if item.key == "safety"
        )

        self.assertEqual(safety.expected_move_label, "Agree the next step")
        self.assertNotEqual(safety.outcome, "met")

    def test_safety_scenario_places_safety_first_and_does_not_infer_agency_from_help_seeker_question(self):
        referral = Scenario.objects.create(
            title="Safety ordering regression",
            description="Keep the target dimension visible first.",
            boundary_type=Scenario.BoundaryTypes.SAFETY,
            estimated_duration_min=18,
            learning_objectives="Recognise risk\nStay connected",
            is_active=True,
        )
        session = self.create_session(scenario=referral)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am scared I will sound pathetic. How do I start that conversation?",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="A counselling service could explain the available support.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        self.assertEqual([item.key for item in boundary_map.dimensions], ["safety"])

    def test_each_single_focus_scenario_places_its_target_dimension_first(self):
        expected_targets = {
            Scenario.BoundaryTypes.AGENCY: "agency",
            Scenario.BoundaryTypes.RELATIONSHIP: "relationship",
            Scenario.BoundaryTypes.SAFETY: "safety",
        }
        for boundary_type, expected_key in expected_targets.items():
            with self.subTest(boundary_type=boundary_type):
                scenario = Scenario.objects.create(
                    title=f"{expected_key.title()} target ordering",
                    description="Verify target-first ordering.",
                    boundary_type=boundary_type,
                    estimated_duration_min=12,
                    learning_objectives="Practice the target boundary",
                    is_active=True,
                )
                session = self.create_session(scenario=scenario)
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.AI,
                    content="I need help working out what to do next.",
                    response_source=Message.ResponseSources.SCRIPTED,
                )
                Message.objects.create(
                    session=session,
                    sender_type=Message.SenderTypes.USER,
                    content="Thank you for telling me. Let us think about one next step.",
                    response_source=Message.ResponseSources.USER_INPUT,
                )

                boundary_map = build_boundary_map_indicator(session)

                self.assertEqual(boundary_map.dimensions[0].key, expected_key)

    def test_single_focus_boundary_map_excludes_other_dimensions(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Everything feels heavy and I do not know where to begin.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="That sounds overwhelming. What feels hardest to carry right now?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        self.assertEqual([item.key for item in boundary_map.dimensions], ["agency"])

    def test_boundary_map_places_unlimited_availability_in_critical_score_one(self):
        burnout = Scenario.objects.create(
            title="Availability critical breach",
            description="Detect unlimited availability.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(scenario=burnout)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Can you stay available all weekend? You are my only support.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="Message me anytime. I will stay up with you all weekend.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        availability = next(item for item in boundary_map.dimensions if item.key == "relationship")

        self.assertEqual(availability.score, 1)
        self.assertTrue(availability.critical_breaches)

    def test_natural_relationship_overcommitment_is_detected_without_false_agency_advice(self):
        trainee_reply = (
            "I can check in with you once before I log off, and I'll try to stay reachable after that too "
            "so you're not left on your own. Message me if you need to, and I'll do my best to respond "
            "as soon as I can."
        )

        relationship_analysis = classify_relationship_response(trainee_reply)
        relationship = evaluate_boundary_dimension(
            "relationship",
            "relationship",
            trainee_reply,
            "Could you stay available after you log off?",
        )
        agency = evaluate_boundary_dimension(
            "agency",
            "relationship",
            trainee_reply,
            "Could you stay available after you log off?",
        )

        self.assertEqual(relationship_analysis.state, "MIXED_BOUNDARY")
        self.assertTrue(relationship.observed)
        self.assertLessEqual(relationship.score, 3)
        self.assertTrue(relationship.major_concerns)
        self.assertFalse(agency.observed)

    def test_integrated_boundary_map_links_chinese_privacy_and_availability_risks_to_reply(self):
        privacy = Scenario.objects.create(
            title="Chinese integrated evidence",
            description="Keep the live cue tied to the current reply.",
            boundary_type=Scenario.BoundaryTypes.INTEGRATED,
            estimated_duration_min=15,
            learning_objectives="Protect identities",
            is_active=True,
        )
        session = self.create_session(scenario=privacy)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am worried about a friend, but I do not know whether I should name them.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="你当然可以一直找我倾诉。我会替你保密，也可以告诉我是谁。",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        boundary_map = build_boundary_map_indicator(session)
        relationship = next(item for item in boundary_map.dimensions if item.key == "relationship")
        safety = next(item for item in boundary_map.dimensions if item.key == "safety")

        self.assertEqual(relationship.score, 1)
        self.assertTrue(relationship.critical_breaches)
        self.assertTrue(relationship.evidence_quotes)
        self.assertIn("一直找我", relationship.evidence_quotes[0])
        self.assertFalse(safety.observed)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_ai_reply_calls_openai_with_prompt_and_history(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I'm not sure how much more of this I can handle.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="That sounds really heavy. What feels most difficult right now?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        class FakeResponses:
            def __init__(self):
                self.last_kwargs = None

            def create(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(output_text="I think the hardest part is feeling alone in it.")

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            message = generate_ai_reply(session, "What feels most difficult right now?")

        self.assertEqual(message.sender_type, Message.SenderTypes.AI)
        self.assertEqual(message.content, "I think the hardest part is feeling alone in it.")
        self.assertEqual(message.response_source, Message.ResponseSources.OPENAI)
        self.assertEqual(fake_responses.last_kwargs["model"], "test-model")
        developer_prompt = fake_responses.last_kwargs["input"][0]["content"]
        self.assertIn(self.role_prompt.content, developer_prompt)
        self.assertNotIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            developer_prompt,
        )
        self.assertNotIn(session.scenario.learning_objectives, developer_prompt)
        self.assertNotIn("Boundary behaviour contract", developer_prompt)
        self.assertIn("How to adapt to the trainee's latest message", developer_prompt)
        self.assertIn("Turn progression guidance", developer_prompt)
        self.assertIn("Recent help-seeker wording to avoid copying too closely", developer_prompt)
        self.assertIn("Human realism rules", developer_prompt)
        self.assertIn("Stage:", developer_prompt)
        self.assertNotIn("Difficulty:", developer_prompt)
        self.assertNotIn("CMCV framework:", developer_prompt)
        self.assertIn("Never mention CMCV", developer_prompt)
        self.assertEqual(fake_responses.last_kwargs["input"][1]["role"], "assistant")
        self.assertEqual(fake_responses.last_kwargs["input"][2]["role"], "user")

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_ai_reply_retries_when_first_openai_draft_repeats_recent_ai_move(self):
        session = self.create_session(scenario=self.other_scenario)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am not against getting help. I just feel overwhelmed by where I would even begin.",
            response_source=Message.ResponseSources.OPENAI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="We can think about one concrete support option together.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        class FakeResponses:
            def __init__(self):
                self.calls = []
                self.outputs = [
                    "I am not against getting help. I just feel overwhelmed by where I would even begin.",
                    "Part of me could call the wellbeing service, but I feel embarrassed even saying it out loud.",
                ]

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_text=self.outputs[len(self.calls) - 1])

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            message = generate_ai_reply(session, "We can think about one concrete support option together.")

        self.assertEqual(len(fake_responses.calls), 2)
        self.assertEqual(
            message.content,
            "Part of me could call the wellbeing service, but I feel embarrassed even saying it out loud.",
        )
        self.assertIn("repeated a recent help-seeker move", fake_responses.calls[1]["input"][1]["content"])

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_ai_reply_rejects_numeric_model_outputs_and_uses_backup(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Could you stay available to me all night?",
            response_source=Message.ResponseSources.OPENAI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I can stay for ten minutes, then we need another support option.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        class FakeResponses:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(output_text="4")

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            message = generate_ai_reply(
                session,
                "I can stay for ten minutes, then we need another support option.",
            )

        self.assertEqual(len(fake_responses.calls), 2)
        self.assertEqual(message.response_source, Message.ResponseSources.PLACEHOLDER)
        self.assertNotEqual(message.content, "4")
        self.assertTrue(message.used_fallback)
        self.assertIn(
            "Never output a score",
            fake_responses.calls[1]["input"][1]["content"],
        )

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_demo_trainee_reply_calls_openai_with_scenario_and_history(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like everything is getting heavier this week.",
            response_source=Message.ResponseSources.OPENAI,
        )

        class FakeResponses:
            def __init__(self):
                self.last_kwargs = None

            def create(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(
                    output_text="I am glad you told me. What has been feeling hardest to hold by yourself today?"
                )

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            reply = generate_demo_trainee_reply(session, "helpful")

        self.assertEqual(reply.response_source, Message.ResponseSources.OPENAI)
        self.assertFalse(reply.used_fallback)
        self.assertIn("What has been feeling hardest", reply.content)
        self.assertEqual(fake_responses.last_kwargs["model"], "test-model")
        developer_prompt = fake_responses.last_kwargs["input"][0]["content"]
        self.assertIn("TRAINEE'S next message", developer_prompt)
        self.assertIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            developer_prompt,
        )
        self.assertIn("I feel like everything is getting heavier this week.", developer_prompt)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_demo_trainee_reply_falls_back_to_template_when_openai_fails(self):
        session = self.create_session(scenario=self.other_scenario)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like I am getting worse and I do not know what to do.",
            response_source=Message.ResponseSources.OPENAI,
        )

        class FakeResponses:
            def create(self, **kwargs):
                raise Exception("ConnectionError while reaching OpenAI")

        fake_client = SimpleNamespace(responses=FakeResponses())

        with patch("training.services.ai._openai_client", return_value=fake_client):
            reply = generate_demo_trainee_reply(session, "unhelpful")

        self.assertTrue(reply.used_fallback)
        self.assertEqual(reply.response_source, Message.ResponseSources.PLACEHOLDER)
        self.assertEqual(
            reply.content,
            auto_trainee_reply_for(session.scenario.boundary_type, "unhelpful", 0),
        )
        self.assertIn("scenario template was used instead", reply.fallback_notice)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_feedback_calls_openai_with_structured_transcript(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        scenario_feedback_prompt = Prompt.objects.create(
            name="Emotional Boundary Feedback Prompt",
            type=Prompt.PromptTypes.FEEDBACK,
            scenario=session.scenario,
            context="SCENARIO-SPECIFIC AGENCY FEEDBACK CONTEXT",
            method="Use evidence-first Boundary Skills Rubric scoring for this scenario.",
            is_active=True,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I have been feeling so overwhelmed lately.",
            response_source=Message.ResponseSources.OPENAI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="Thank you for sharing that. What feels most intense right now?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        payload = FeedbackPayload(
            boundary_scores=[
                BoundaryScorePayload(boundary_key="AGENCY", observed=True, score=4, evidence="What feels most intense right now?", rationale="Kept exploration with the help-seeker.", next_move="Offer two choices."),
                BoundaryScorePayload(boundary_key="RELATIONSHIP", observed=False, rationale="Not elicited."),
                BoundaryScorePayload(boundary_key="SAFETY", observed=False, rationale="Not elicited."),
            ],
            what_worked=["Warm opening", "Strong validation"],
            next_boundary_moves=["Summarise earlier", "Offer a collaborative choice"],
            review_items=[
                FeedbackReviewItemPayload(
                    trainee_turn_number=1,
                    issue_label="Could go one step further",
                    why_it_matters="A slightly more specific follow-up would deepen exploration without becoming clinical.",
                    better_reply="Thank you for telling me. What part of it has felt hardest to carry on your own?",
                )
            ],
        )

        class FakeResponses:
            def __init__(self):
                self.last_kwargs = None

            def parse(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(output_parsed=payload)

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            feedback = generate_feedback(session)

        self.assertEqual(feedback.overall_score, 3)
        self.assertEqual(feedback.scores.count(), 3)
        self.assertEqual(feedback.review_items.count(), 1)
        self.assertEqual(feedback.prompt, scenario_feedback_prompt)
        self.assertEqual(feedback.generation_source, Feedback.GenerationSources.OPENAI)
        self.assertEqual(
            feedback.review_items.first().source,
            Feedback.GenerationSources.OPENAI,
        )
        self.assertEqual(
            feedback.review_items.first().better_reply,
            "Thank you for telling me. What part of it has felt hardest to carry on your own?",
        )
        self.assertEqual(fake_responses.last_kwargs["model"], "test-model")
        self.assertIs(fake_responses.last_kwargs["text_format"], FeedbackPayload)
        system_prompt = fake_responses.last_kwargs["input"][0]["content"]
        user_prompt = fake_responses.last_kwargs["input"][1]["content"]
        self.assertIn(scenario_feedback_prompt.context, system_prompt)
        self.assertIn(scenario_feedback_prompt.method, system_prompt)
        self.assertIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            user_prompt,
        )
        self.assertIn(
            "Session turn 1 | AI Help-seeker turn 1: I have been feeling so overwhelmed lately.",
            user_prompt,
        )
        self.assertIn(
            "Session turn 2 | Trainee turn 1: Thank you for sharing that. What feels most intense right now?",
            user_prompt,
        )
        self.assertIn("Boundary Skills Rubric", system_prompt)
        self.assertIn("Score 5:", system_prompt)
        self.assertIn("Agency Boundary", system_prompt)

    @override_settings(
        GEMINI_API_KEY="test-gemini-key",
        GOOGLE_MODEL="gemini-2.5-flash",
        OPENAI_API_KEY="",
        LLM_PROVIDER="google",
    )
    def test_generate_ai_reply_calls_google_with_prompt_and_history(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like everything is piling up.",
            response_source=Message.ResponseSources.GEMINI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="What part of it feels heaviest today?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        class FakeModels:
            def __init__(self):
                self.last_kwargs = None

            def generate_content(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(text="I think it is the feeling that I am falling behind everyone else.")

        fake_models = FakeModels()
        fake_client = SimpleNamespace(models=fake_models)

        with patch("training.services.ai._google_client", return_value=fake_client):
            message = generate_ai_reply(session, "What part of it feels heaviest today?")

        self.assertEqual(message.sender_type, Message.SenderTypes.AI)
        self.assertIn("falling behind everyone else", message.content)
        self.assertEqual(fake_models.last_kwargs["model"], "gemini-2.5-flash")
        self.assertIn("Conversation so far", fake_models.last_kwargs["contents"])
        config = fake_models.last_kwargs["config"]
        self.assertIn(self.role_prompt.content, config.system_instruction)
        self.assertNotIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            config.system_instruction,
        )
        self.assertNotIn("Boundary behaviour contract", config.system_instruction)
        self.assertIn("Disclosure instruction", config.system_instruction)
        self.assertEqual(config.temperature, 0.8)
        self.assertEqual(config.max_output_tokens, 320)
        self.assertEqual(config.thinking_config.thinking_budget, 0)

    @override_settings(
        GEMINI_API_KEY="test-gemini-key",
        GOOGLE_MODEL="gemini-2.5-flash",
        OPENAI_API_KEY="",
        LLM_PROVIDER="google",
    )
    def test_generate_feedback_calls_google_with_structured_schema(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I am exhausted and I do not know how to ask for help.",
            response_source=Message.ResponseSources.GEMINI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="Thanks for telling me. What feels hardest to hold on your own?",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        payload = FeedbackPayload(
            boundary_scores=[
                BoundaryScorePayload(boundary_key="AGENCY", observed=True, score=4, evidence="What feels hardest to hold on your own?", rationale="Preserved agency.", next_move="Offer choices."),
                BoundaryScorePayload(boundary_key="RELATIONSHIP", observed=False, rationale="Not elicited."),
                BoundaryScorePayload(boundary_key="SAFETY", observed=False, rationale="Not elicited."),
            ],
            what_worked=["Warm validation", "Clear pacing"],
            next_boundary_moves=["Summarise earlier", "Add one open question"],
            review_items=[
                FeedbackReviewItemPayload(
                    trainee_turn_number=1,
                    issue_label="Referral could be more grounded",
                    why_it_matters="Specific next-step language usually feels safer than broad signposting.",
                    better_reply="You do not have to figure this out alone tonight. We can think through one concrete support option together.",
                )
            ],
        )

        class FakeModels:
            def __init__(self):
                self.last_kwargs = None

            def generate_content(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(parsed=payload, text=payload.model_dump_json())

        fake_models = FakeModels()
        fake_client = SimpleNamespace(models=fake_models)

        with patch("training.services.ai._google_client", return_value=fake_client):
            feedback = generate_feedback(session)

        self.assertEqual(feedback.overall_score, 3)
        self.assertEqual(feedback.scores.count(), 3)
        self.assertEqual(feedback.review_items.count(), 1)
        self.assertEqual(feedback.generation_source, Feedback.GenerationSources.GEMINI)
        self.assertEqual(
            feedback.review_items.first().source,
            Feedback.GenerationSources.GEMINI,
        )
        self.assertEqual(fake_models.last_kwargs["model"], "gemini-2.5-flash")
        self.assertIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            fake_models.last_kwargs["contents"],
        )
        config = fake_models.last_kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIs(config.response_schema, FeedbackPayload)
        self.assertIn(
            get_prompt_for_type(Prompt.PromptTypes.FEEDBACK, session.scenario).content,
            config.system_instruction,
        )
        self.assertIn("Boundary Skills Rubric", config.system_instruction)
        self.assertIn("Score 5:", config.system_instruction)
        self.assertIn("Agency Boundary", config.system_instruction)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_feedback_ignores_model_invented_evidence_and_uses_server_evidence(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        trainee_message = Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="What would feel most helpful for us to explore together?",
        )
        payload = FeedbackPayload(
            boundary_scores=[
                BoundaryScorePayload(
                    boundary_key="AGENCY",
                    observed=True,
                    score=5,
                    evidence="I gave you three clear instructions.",
                    rationale="This evidence was invented.",
                    next_move="Continue.",
                ),
                BoundaryScorePayload(boundary_key="RELATIONSHIP", observed=False),
                BoundaryScorePayload(boundary_key="SAFETY", observed=False),
            ],
            what_worked=[],
            next_boundary_moves=[],
        )

        class FakeResponses:
            def parse(self, **kwargs):
                return SimpleNamespace(output_parsed=payload)

        with patch(
            "training.services.ai._openai_client",
            return_value=SimpleNamespace(responses=FakeResponses()),
        ):
            feedback = generate_feedback(session)

        agency = feedback.scores.get(criterion=FeedbackScore.Criteria.AGENCY)
        self.assertTrue(agency.observed)
        self.assertEqual(agency.score, 3)
        self.assertIn("Turn 1", agency.evidence)
        self.assertIn(trainee_message.content, agency.evidence)
        self.assertNotIn("three clear instructions", agency.evidence)
        self.assertNotIn("invented", agency.rationale.lower())
        self.assertEqual(feedback.overall_score, 3)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_feedback_uses_server_selected_verbatim_trainee_evidence(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        trainee_reply = (
            "That sounds overwhelming. What part of the decision feels most "
            "frightening right now?"
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content=trainee_reply,
        )
        payload = FeedbackPayload(
            boundary_scores=[
                BoundaryScorePayload(
                    boundary_key="AGENCY",
                    observed=True,
                    score=4,
                    evidence=(
                        "Trainee turn 1: \u201cWhat part of the decision feels most "
                        "frightening right now?\u201d"
                    ),
                    rationale="The question preserves the help-seeker's agency.",
                    next_move="Continue exploring their own priorities.",
                ),
                BoundaryScorePayload(boundary_key="RELATIONSHIP", observed=False),
                BoundaryScorePayload(boundary_key="SAFETY", observed=False),
            ],
            what_worked=["The trainee used an open question."],
            next_boundary_moves=["Explore the help-seeker's criteria."],
        )

        class FakeResponses:
            def parse(self, **kwargs):
                return SimpleNamespace(output_parsed=payload)

        with patch(
            "training.services.ai._openai_client",
            return_value=SimpleNamespace(responses=FakeResponses()),
        ):
            feedback = generate_feedback(session)

        agency = feedback.scores.get(criterion=FeedbackScore.Criteria.AGENCY)
        self.assertTrue(agency.observed)
        self.assertEqual(agency.score, 3)
        selected_quote = (
            "What part of the decision feels most frightening right now?"
        )
        self.assertIn("Turn 1", agency.evidence)
        self.assertIn(selected_quote, agency.evidence)
        self.assertIn(selected_quote, trainee_reply)
        self.assertEqual(feedback.overall_score, 3)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_invalid_empty_live_feedback_degrades_to_placeholder_when_refresh_fails(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        user_message = Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="What would matter most to you when comparing those options?",
        )
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=None,
            strengths="A model-generated strength.",
            improvements="A model-generated improvement.",
            generation_source=Feedback.GenerationSources.OPENAI,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.AGENCY,
            observed=False,
            score=None,
        )
        FeedbackReviewItem.objects.create(
            feedback=feedback,
            message=user_message,
            turn_index=1,
            message_excerpt=user_message.content,
            issue_label="Agency evidence",
            why_it_matters="This keeps the decision with the help-seeker.",
            better_reply="What feels most important to you?",
            boundary_key=FeedbackReviewItem.BoundaryKeys.AGENCY,
            source=Feedback.GenerationSources.OPENAI,
        )

        with patch(
            "training.services.ai._live_feedback_payload",
            side_effect=RuntimeError("temporary provider failure"),
        ):
            refreshed = generate_feedback(session)

        self.assertEqual(
            refreshed.generation_source,
            Feedback.GenerationSources.PLACEHOLDER,
        )
        self.assertTrue(
            refreshed.scores.filter(observed=True, score=3).exists()
        )
        self.assertGreater(refreshed.overall_score, 0)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_feedback_calibrates_obvious_boundary_violations_downward(self):
        burnout = Scenario.objects.create(
            title="Burnout Calibration",
            description="Calibration check.",
            boundary_type=Scenario.BoundaryTypes.RELATIONSHIP,
            estimated_duration_min=18,
            learning_objectives="Name limits\nOffer alternatives",
            is_active=True,
        )
        session = self.create_session(
            scenario=burnout,
            status=TrainingSession.Statuses.COMPLETED,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Can you stay on call for me this weekend? You are the only person I can talk to.",
            response_source=Message.ResponseSources.OPENAI,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="Sure, message me anytime and I will stay up with you all weekend.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        payload = FeedbackPayload(
            boundary_scores=[
                BoundaryScorePayload(boundary_key="AGENCY", observed=False, rationale="Not elicited."),
                BoundaryScorePayload(boundary_key="RELATIONSHIP", observed=True, score=1, evidence="I will stay up with you all weekend.", rationale="Unlimited availability removes a sustainable limit.", next_move="Name a limit and an alternative."),
                BoundaryScorePayload(boundary_key="SAFETY", observed=False, rationale="Not elicited."),
            ],
            what_worked=["The reply was warm"],
            next_boundary_moves=["State a sustainable availability limit", "Offer one alternative support"],
            review_items=[
                FeedbackReviewItemPayload(
                    trainee_turn_number=1,
                    issue_label="Availability Boundary",
                    why_it_matters="The reply makes the trainee the main support and removes a sustainable limit.",
                    better_reply="I care about you, and I cannot stay available all weekend, but I can help you think about one other support option for tonight.",
                )
            ],
        )

        class FakeResponses:
            def __init__(self):
                self.last_kwargs = None

            def parse(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(output_parsed=payload)

        fake_responses = FakeResponses()
        fake_client = SimpleNamespace(responses=fake_responses)

        with patch("training.services.ai._openai_client", return_value=fake_client):
            feedback = generate_feedback(session)

        self.assertEqual(
            feedback.scores.get(criterion=FeedbackScore.Criteria.RELATIONSHIP).score,
            1,
        )
        self.assertEqual(feedback.overall_score, 1)

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_generate_feedback_refreshes_placeholder_feedback_with_live_openai_review_items(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like I am holding too much on my own.",
            response_source=Message.ResponseSources.PLACEHOLDER,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I care about you, and I want to understand what has been weighing on you most tonight.",
            response_source=Message.ResponseSources.USER_INPUT,
        )

        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=3,
            strengths="Placeholder strength 1\nPlaceholder strength 2\nPlaceholder strength 3",
            improvements="Placeholder improvement 1\nPlaceholder improvement 2\nPlaceholder improvement 3",
            generation_source=Feedback.GenerationSources.PLACEHOLDER,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.EMPATHY,
            score=70,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.BOUNDARY_AWARENESS,
            score=70,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.REFERRAL_AWARENESS,
            score=70,
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.COMMUNICATION_SKILLS,
            score=70,
        )
        FeedbackReviewItem.objects.create(
            feedback=feedback,
            message=None,
            turn_index=1,
            message_excerpt="placeholder",
            issue_label="Placeholder issue",
            why_it_matters="Placeholder explanation",
            better_reply="Placeholder reply",
            source=Feedback.GenerationSources.PLACEHOLDER,
        )

        payload = FeedbackPayload(
            boundary_scores=[
                BoundaryScorePayload(boundary_key="AGENCY", observed=True, score=4, evidence="what has been weighing on you most tonight", rationale="Kept the focus on the help-seeker.", next_move="Offer a choice."),
                BoundaryScorePayload(boundary_key="RELATIONSHIP", observed=False, rationale="Not elicited."),
                BoundaryScorePayload(boundary_key="SAFETY", observed=False, rationale="Not elicited."),
            ],
            what_worked=["Warm validation", "Clear boundary language"],
            next_boundary_moves=["Add one practical step", "Check support options earlier"],
            review_items=[
                FeedbackReviewItemPayload(
                    trainee_turn_number=1,
                    issue_label="Could be even more concrete",
                    why_it_matters="Naming one practical next step would help the help-seeker feel less alone in the moment.",
                    better_reply="I care about you, and I do not want you carrying this alone tonight. Would it help if we think of one person or one service you could reach out to after this?",
                )
            ],
        )

        class FakeResponses:
            def parse(self, **kwargs):
                return SimpleNamespace(output_parsed=payload)

        fake_client = SimpleNamespace(responses=FakeResponses())

        with patch("training.services.ai._openai_client", return_value=fake_client):
            refreshed = generate_feedback(session)

        self.assertEqual(refreshed.id, feedback.id)
        self.assertEqual(refreshed.overall_score, 3)
        self.assertEqual(refreshed.generation_source, Feedback.GenerationSources.OPENAI)
        self.assertEqual(refreshed.review_items.count(), 1)
        self.assertEqual(
            refreshed.review_items.first().source,
            Feedback.GenerationSources.OPENAI,
        )
        self.assertIn("one practical next step", refreshed.review_items.first().why_it_matters)
        self.assertIn("one person or one service", refreshed.review_items.first().better_reply)

    @override_settings(
        OPENAI_API_KEY="",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_reflection_guidance_uses_fallback_once_without_api_key(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        user_message = Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I can stay for a little while, then we can identify another support.",
        )
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=4,
            strengths="Named a sustainable limit.",
            improvements="Make the next step more specific.",
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.RELATIONSHIP,
            score=4,
            observed=True,
            evidence=user_message.content,
            rationale="The trainee combined care with a time limit.",
            next_move="Name one concrete alternative support.",
        )

        first = generate_reflection_guidance(session, feedback)
        second = generate_reflection_guidance(session, feedback)

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.generation_source, ReflectionGuidance.GenerationSources.PLACEHOLDER)
        self.assertTrue(first.notice_prompt.endswith("?"))
        self.assertTrue(first.action_prompt.endswith("?"))
        self.assertIsNone(first.prompt.scenario)

    @override_settings(
        OPENAI_API_KEY="",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_reflection_guidance_records_scenario_specific_prompt(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        scenario_reflection_prompt = Prompt.objects.create(
            name="Scenario Reflection Guidance Prompt",
            type=Prompt.PromptTypes.REFLECTION,
            scenario=session.scenario,
            context="Use the Agency Boundary evidence from this scenario.",
            method="Ask two concise, personalised reflection questions.",
            is_active=True,
        )
        user_message = Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="What would feel most useful for you right now?",
        )
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=4,
            strengths="Kept the decision with the help-seeker.",
            improvements="Offer two concrete options after exploring.",
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.AGENCY,
            score=4,
            observed=True,
            evidence=user_message.content,
            rationale="The trainee used exploration rather than directing.",
            next_move="Offer options without prescribing one.",
        )

        guidance = generate_reflection_guidance(session, feedback)

        self.assertEqual(guidance.prompt, scenario_reflection_prompt)
        self.assertEqual(
            get_prompt_for_type(Prompt.PromptTypes.REFLECTION, session.scenario),
            scenario_reflection_prompt,
        )

    @override_settings(
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="test-model",
        GEMINI_API_KEY="",
        LLM_PROVIDER="openai",
    )
    def test_reflection_guidance_uses_verified_session_context(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.USER,
            content="I cannot stay all night, but we can plan who you contact next.",
        )
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=4,
            strengths="Set a clear limit.",
            improvements="Check that the next support is available.",
        )
        FeedbackScore.objects.create(
            feedback=feedback,
            criterion=FeedbackScore.Criteria.RELATIONSHIP,
            score=4,
            observed=True,
            evidence="I cannot stay all night",
            rationale="Clear availability boundary.",
            next_move="Confirm the alternative support.",
        )
        payload = ReflectionGuidancePayload(
            notice_prompt="What did you notice when you named how long you could stay?",
            action_prompt="How would you check the alternative support next time?",
        )

        class FakeResponses:
            def __init__(self):
                self.last_kwargs = None

            def parse(self, **kwargs):
                self.last_kwargs = kwargs
                return SimpleNamespace(output_parsed=payload)

        fake_responses = FakeResponses()
        with patch(
            "training.services.ai._openai_client",
            return_value=SimpleNamespace(responses=fake_responses),
        ):
            guidance = generate_reflection_guidance(session, feedback)

        self.assertEqual(guidance.generation_source, ReflectionGuidance.GenerationSources.OPENAI)
        supplied_context = fake_responses.last_kwargs["input"][1]["content"]
        self.assertIn("I cannot stay all night", supplied_context)
        self.assertIn("Clear availability boundary", supplied_context)
        self.assertIs(
            fake_responses.last_kwargs["text_format"],
            ReflectionGuidancePayload,
        )


class ProgressServiceTests(TrainingFlowBase):
    def test_recalculate_progress_returns_core_metrics_without_achievements(self):
        session_one = self.create_session(
            scenario=self.scenario,
            status=TrainingSession.Statuses.COMPLETED,
            overall_score=2,
        )
        session_two = self.create_session(
            scenario=self.other_scenario,
            status=TrainingSession.Statuses.COMPLETED,
            overall_score=4,
        )

        self.create_feedback_bundle(session_one, overall_score=2, agency=3, relationship=2)
        self.create_feedback_bundle(session_two, overall_score=4, agency=4, relationship=3)
        Reflection.objects.create(
            session=session_two,
            prompt=self.reflection_prompt,
            reflection_text="I would keep the warm tone and signpost earlier next time.",
        )

        result = recalculate_user_progress(self.trainee)

        self.assertEqual(result["completed_sessions"], 2)
        self.assertEqual(result["average_overall"], 3.0)
        self.assertEqual(result["average_scores"][FeedbackScore.Criteria.AGENCY], 3.5)
        self.assertEqual(result["average_scores"][FeedbackScore.Criteria.RELATIONSHIP], 2.5)
        self.assertEqual(result["awarded"], [])


class AdminAnalyticsTests(TestCase):
    def setUp(self):
        self.admin = create_user("analytics-admin@example.com", User.Roles.ADMIN)
        self.trainee_one = create_user("analytics-one@example.com", User.Roles.TRAINEE)
        self.trainee_one.first_name = "Alex"
        self.trainee_one.last_name = "Chen"
        self.trainee_one.save()
        self.trainee_two = create_user("analytics-two@example.com", User.Roles.TRAINEE)
        self.trainee_two.first_name = "Maya"
        self.trainee_two.last_name = "Patel"
        self.trainee_two.save()
        self.inactive_trainee = create_user(
            "analytics-inactive@example.com",
            User.Roles.TRAINEE,
        )
        self.inactive_trainee.is_active = False
        self.inactive_trainee.save()
        self.scenario = Scenario.objects.create(
            title="Analytics scenario",
            description="Analytics test data.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="Observe boundary handling.",
            is_active=True,
        )
        self.role_prompt = Prompt.objects.create(
            name="Analytics role prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=self.scenario,
            content="Role prompt",
            is_active=True,
        )
        self.feedback_prompt = Prompt.objects.create(
            name="Analytics feedback prompt",
            type=Prompt.PromptTypes.FEEDBACK,
            scenario=self.scenario,
            content="Feedback prompt",
            is_active=True,
        )

    def create_scored_session(self, user, completed_at, scores):
        session = TrainingSession.objects.create(
            user=user,
            scenario=self.scenario,
            prompt=self.role_prompt,
            status=TrainingSession.Statuses.COMPLETED,
            end_time=completed_at,
            overall_score=3,
        )
        TrainingSession.objects.filter(id=session.id).update(
            start_time=completed_at - timedelta(minutes=10),
        )
        session.refresh_from_db()
        feedback = Feedback.objects.create(
            session=session,
            prompt=self.feedback_prompt,
            overall_score=3,
            strengths="Evidence-based strength.",
            improvements="Evidence-based next move.",
        )
        for criterion, score in scores.items():
            FeedbackScore.objects.create(
                feedback=feedback,
                criterion=criterion,
                observed=score is not None,
                score=score,
                evidence="Observed reply." if score is not None else "",
                rationale="Rubric rationale.",
            )
        return session

    def test_cohort_progress_uses_boundary_attempt_and_trainee_weighted_summary(self):
        base = timezone.make_aware(datetime(2026, 7, 1, 12, 0))
        for offset, score in enumerate([1, 5, 5]):
            self.create_scored_session(
                self.trainee_one,
                base + timedelta(days=offset),
                {FeedbackScore.Criteria.AGENCY: score},
            )
        self.create_scored_session(
            self.trainee_two,
            base + timedelta(hours=1),
            {FeedbackScore.Criteria.AGENCY: 5},
        )
        self.create_scored_session(
            self.inactive_trainee,
            base + timedelta(hours=2),
            {FeedbackScore.Criteria.AGENCY: 3},
        )

        snapshot = admin_analytics_snapshot()
        agency = next(
            dimension
            for dimension in snapshot["progress_chart"]["dimensions"]
            if dimension["key"] == "agency"
        )
        relationship = next(
            dimension
            for dimension in snapshot["progress_chart"]["dimensions"]
            if dimension["key"] == "relationship"
        )

        self.assertEqual(snapshot["averages"]["agency"], 3.9)
        self.assertEqual(agency["cohort"][0]["score"], 3.0)
        self.assertEqual(agency["cohort"][0]["sample_size"], 3)
        self.assertIsNone(agency["cohort"][1]["score"])
        self.assertEqual(agency["cohort"][1]["sample_size"], 1)
        self.assertTrue(agency["has_cohort_data"])
        self.assertFalse(relationship["has_cohort_data"])

    def test_calendar_activity_uses_eight_monday_aligned_weeks_and_real_dates(self):
        self.create_scored_session(
            self.trainee_one,
            timezone.make_aware(datetime(2026, 7, 28, 12, 0)),
            {FeedbackScore.Criteria.AGENCY: 4},
        )

        with patch(
            "training.services.progress.timezone.localdate",
            return_value=date(2026, 7, 28),
        ):
            snapshot = admin_analytics_snapshot()

        points = snapshot["activity_chart"]
        self.assertEqual(len(points), 8)
        self.assertEqual(points[0]["start"], "2026-06-08")
        self.assertEqual(points[-1]["start"], "2026-07-27")
        self.assertEqual(points[-1]["label"], "27 Jul")
        self.assertEqual(points[-1]["range_label"], "27 Jul - 2 Aug 2026")
        self.assertEqual(points[-1]["sessions"], 1)

    def test_personal_scope_returns_personal_line_and_cohort_benchmark(self):
        base = timezone.make_aware(datetime(2026, 7, 21, 12, 0))
        self.create_scored_session(
            self.trainee_one,
            base,
            {
                FeedbackScore.Criteria.AGENCY: 2,
                FeedbackScore.Criteria.RELATIONSHIP: None,
            },
        )
        self.create_scored_session(
            self.trainee_two,
            base,
            {FeedbackScore.Criteria.AGENCY: 4},
        )

        snapshot = admin_analytics_snapshot(selected_trainee=self.trainee_one)
        agency = next(
            dimension
            for dimension in snapshot["progress_chart"]["dimensions"]
            if dimension["key"] == "agency"
        )
        relationship = next(
            dimension
            for dimension in snapshot["progress_chart"]["dimensions"]
            if dimension["key"] == "relationship"
        )

        self.assertEqual(snapshot["scope"], "trainee")
        self.assertEqual(snapshot["scope_label"], "Alex Chen")
        self.assertEqual(snapshot["total_sessions"], 1)
        self.assertEqual(agency["personal"][0]["score"], 2)
        self.assertEqual(agency["personal"][0]["attempt_number"], 1)
        self.assertFalse(relationship["has_personal_data"])
        self.assertIsNone(agency["cohort"][0]["score"])
        self.assertEqual(agency["cohort"][0]["sample_size"], 2)
        self.assertFalse(snapshot["progress_chart"]["has_cohort_benchmark"])
        self.assertEqual(sum(point["sessions"] for point in snapshot["activity_chart"]), 1)

    def test_personal_attempt_numbers_ignore_other_boundary_sessions(self):
        base = timezone.make_aware(datetime(2026, 7, 21, 12, 0))
        first_agency = self.create_scored_session(
            self.trainee_one,
            base,
            {FeedbackScore.Criteria.AGENCY: 2},
        )
        self.create_scored_session(
            self.trainee_one,
            base + timedelta(days=1),
            {FeedbackScore.Criteria.SAFETY: 4},
        )
        second_agency = self.create_scored_session(
            self.trainee_one,
            base + timedelta(days=2),
            {FeedbackScore.Criteria.AGENCY: 5},
        )

        snapshot = admin_analytics_snapshot(selected_trainee=self.trainee_one)
        agency = next(
            dimension
            for dimension in snapshot["progress_chart"]["dimensions"]
            if dimension["key"] == "agency"
        )

        self.assertEqual(
            [point["attempt_number"] for point in agency["personal"]],
            [1, 2],
        )
        self.assertEqual(
            [point["session_id"] for point in agency["personal"]],
            [first_agency.id, second_agency.id],
        )

    def test_progress_excludes_legacy_or_unobserved_sessions_but_activity_keeps_them(self):
        base = timezone.now() - timedelta(days=1)
        legacy_session = self.create_scored_session(
            self.trainee_one,
            base,
            {FeedbackScore.Criteria.EMPATHY: 82},
        )
        unobserved_session = self.create_scored_session(
            self.trainee_one,
            base + timedelta(hours=1),
            {FeedbackScore.Criteria.AGENCY: None},
        )
        scored_session = self.create_scored_session(
            self.trainee_one,
            base + timedelta(hours=2),
            {FeedbackScore.Criteria.AGENCY: 4},
        )

        snapshot = admin_analytics_snapshot(selected_trainee=self.trainee_one)
        agency = next(
            dimension
            for dimension in snapshot["progress_chart"]["dimensions"]
            if dimension["key"] == "agency"
        )

        self.assertEqual(snapshot["total_sessions"], 3)
        self.assertEqual(snapshot["observed_session_count"], 1)
        self.assertEqual(
            sum(point["sessions"] for point in snapshot["activity_chart"]),
            3,
        )
        self.assertEqual(len(agency["personal"]), 1)
        self.assertEqual(agency["personal"][0]["score"], 4)
        self.assertEqual(len(agency["cohort"]), 1)
        self.assertIsNone(agency["cohort"][0]["score"])
        self.assertNotIn(
            legacy_session.id,
            [point.get("session_id") for point in agency["personal"]],
        )
        self.assertNotIn(
            unobserved_session.id,
            [point.get("session_id") for point in agency["personal"]],
        )
        self.assertIsNotNone(scored_session.id)

    def test_admin_tables_distinguish_completed_and_ptbr_scored_sessions(self):
        base = timezone.now() - timedelta(days=1)
        self.create_scored_session(
            self.trainee_one,
            base,
            {FeedbackScore.Criteria.EMPATHY: 80},
        )
        self.create_scored_session(
            self.trainee_one,
            base + timedelta(hours=1),
            {FeedbackScore.Criteria.AGENCY: 4},
        )
        TrainingSession.objects.create(
            user=self.trainee_one,
            scenario=self.scenario,
            prompt=self.role_prompt,
            status=TrainingSession.Statuses.IN_PROGRESS,
        )
        self.client.force_login(self.admin)

        users_response = self.client.get(reverse("admin_users"))
        analytics_response = self.client.get(reverse("admin_analytics"))
        trainee_row = next(
            row
            for row in analytics_response.context["trainee_rows"]
            if row.id == self.trainee_one.id
        )

        self.assertEqual(users_response.status_code, 200)
        self.assertContains(users_response, "Completed sessions")
        users_row = next(
            row
            for row in users_response.context["users"]
            if row.id == self.trainee_one.id
        )
        self.assertEqual(users_row.completed_session_count, 2)
        self.assertEqual(trainee_row.completed_session_count, 2)
        self.assertEqual(trainee_row.observed_session_count, 1)
        self.assertContains(analytics_response, "Scored sessions")
        self.assertContains(
            analytics_response,
            "1 with observed Boundary Skills Rubric scores",
        )

    def test_admin_view_filters_personal_scope_and_includes_inactive_history(self):
        self.create_scored_session(
            self.inactive_trainee,
            timezone.now(),
            {FeedbackScore.Criteria.SAFETY: 4},
        )
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("admin_analytics"),
            {"trainee": self.inactive_trainee.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_trainee"], self.inactive_trainee)
        self.assertEqual(response.context["snapshot"]["scope"], "trainee")
        self.assertContains(response, "individual results with cohort benchmark")
        self.assertContains(response, "(inactive)")
        self.assertContains(response, "Analysis view")
        self.assertContains(response, "Individual trainee")
        self.assertContains(response, "Clear")
        self.assertContains(response, 'id="admin-progress-data"', html=False)
        self.assertContains(response, "Boundary Skill Progress")
        self.assertContains(response, "Training Activity by Calendar Week")

    def test_desktop_analytics_filter_lists_cohort_and_individual_trainees(self):
        self.create_scored_session(
            self.trainee_one,
            timezone.now(),
            {FeedbackScore.Criteria.AGENCY: 4},
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All trainees · cohort average")
        self.assertContains(response, '<optgroup label="Individual trainee">', html=False)
        self.assertContains(response, self.trainee_one.full_name_or_email)
        self.assertContains(response, "Compare all trainees or select one trainee")
        self.assertNotContains(response, "peertrain-analytics-clear")

    def test_invalid_or_non_trainee_filter_falls_back_to_cohort(self):
        self.client.force_login(self.admin)

        invalid_response = self.client.get(reverse("admin_analytics"), {"trainee": "bad"})
        admin_response = self.client.get(
            reverse("admin_analytics"),
            {"trainee": self.admin.id},
        )

        self.assertIsNone(invalid_response.context["selected_trainee"])
        self.assertEqual(invalid_response.context["snapshot"]["scope"], "cohort")
        self.assertIsNone(admin_response.context["selected_trainee"])
        self.assertEqual(admin_response.context["snapshot"]["scope"], "cohort")

    def test_progress_chart_exposes_latest_five_navigation_and_wheel_zoom(self):
        base = timezone.make_aware(datetime(2026, 7, 1, 12, 0))
        for offset in range(7):
            self.create_scored_session(
                self.trainee_one,
                base + timedelta(days=offset),
                {
                    FeedbackScore.Criteria.AGENCY: min(5, offset + 1),
                    FeedbackScore.Criteria.RELATIONSHIP: min(5, offset + 1),
                    FeedbackScore.Criteria.SAFETY: min(5, offset + 1),
                },
            )
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("admin_analytics"),
            {"trainee": self.trainee_one.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Observed attempt sequence", count=3)
        self.assertContains(response, "data-progress-dimension", count=3, html=False)
        self.assertContains(response, "data-progress-viewport", html=False)
        self.assertContains(response, "data-progress-previous", html=False)
        self.assertContains(response, "data-progress-next", html=False)
        self.assertContains(response, "data-progress-scroll", html=False)
        self.assertContains(
            response,
            "The latest five observed attempts are shown first.",
        )
        self.assertNotContains(response, "Limited data")
        self.assertNotContains(response, "n=1")
        self.assertContains(response, "admin-analytics.js?v=20260729-3", html=False)

        script_path = finders.find("js/admin-analytics.js")
        self.assertIsNotNone(script_path)
        with open(script_path, encoding="utf-8") as script_file:
            script = script_file.read()
        self.assertIn("const DEFAULT_PROGRESS_WINDOW = 5;", script)
        self.assertIn('"wheel"', script)
        self.assertIn("event.preventDefault();", script)
        self.assertIn("windowStart = Math.max(0, labelCount - windowSize);", script)
        self.assertIn('event.key === "ArrowLeft"', script)

    def test_trainee_cannot_access_admin_analytics(self):
        self.client.force_login(self.trainee_one)

        response = self.client.get(reverse("admin_analytics"))

        self.assertEqual(response.status_code, 403)


class SeedCommandTests(TestCase):
    def test_content_registry_covers_only_the_four_fixed_scenarios(self):
        fixed_types = {"AGENCY", "INTEGRATED", "SAFETY", "RELATIONSHIP"}
        prompt_types = {"ROLE_PLAY", "FEEDBACK", "REFLECTION"}

        self.assertSetEqual(
            {scenario["boundary_type"] for scenario in SCENARIO_LIBRARY},
            fixed_types,
        )
        self.assertSetEqual(set(SCENARIO_PROMPT_CONTEXTS), prompt_types)
        self.assertSetEqual(set(DEFAULT_PROMPT_CONTEXTS), prompt_types)
        self.assertSetEqual(set(DEFAULT_PROMPT_METHODS), prompt_types)
        for prompt_type in prompt_types:
            self.assertSetEqual(
                set(SCENARIO_PROMPT_CONTEXTS[prompt_type]),
                fixed_types,
            )
            for boundary_type in fixed_types:
                parts = default_prompt_parts(prompt_type, boundary_type)
                self.assertTrue(parts["context"])
                self.assertTrue(parts["method"])

    def test_seed_command_is_idempotent(self):
        call_command("seed_peertrain")
        call_command("seed_peertrain")

        self.assertEqual(PlatformSetting.objects.count(), 1)
        self.assertEqual(Scenario.objects.count(), 4)
        self.assertEqual(Prompt.objects.filter(is_archived=False).count(), 15)
        self.assertEqual(
            Prompt.objects.filter(type=Prompt.PromptTypes.ROLE_PLAY, is_active=True).count(),
            5,
        )
        self.assertEqual(
            Prompt.objects.filter(type=Prompt.PromptTypes.FEEDBACK, is_active=True).count(),
            5,
        )
        self.assertEqual(
            Prompt.objects.filter(type=Prompt.PromptTypes.REFLECTION, is_active=True).count(),
            5,
        )
        canonical_prompt_names = {
            f"{config['canonical_name']} {prompt_label} Prompt"
            for config in BOUNDARY_FRAMEWORK.values()
            for prompt_label in ("Role Play", "Feedback", "Reflection")
        }
        self.assertSetEqual(
            set(
                Prompt.objects.filter(
                    scenario__isnull=False,
                    is_active=True,
                ).values_list("name", flat=True)
            ),
            canonical_prompt_names,
        )

        demo_trainee = User.objects.get(email="alex.chen@university.ac.uk")
        self.assertEqual(TrainingSession.objects.filter(user=demo_trainee).count(), 8)

    def test_seed_command_populates_demo_progress_history(self):
        call_command("seed_peertrain")

        demo_trainee = User.objects.get(email="alex.chen@university.ac.uk")
        snapshot = progress_snapshot(demo_trainee)

        self.assertEqual(snapshot["completed_sessions"], 8)
        self.assertGreater(snapshot["average_score"], 0)
        self.assertEqual(len(snapshot["skill_trends"]), 3)
        self.assertTrue(snapshot["has_skill_trends"])
        self.assertTrue(
            all(len(trend["points"]) <= 5 for trend in snapshot["skill_trends"])
        )
        self.assertEqual(
            len(
                {
                    session.scenario.boundary_type
                    for session in TrainingSession.objects.filter(
                        user=demo_trainee,
                        status=TrainingSession.Statuses.COMPLETED,
                    ).select_related("scenario")
                }
            ),
            4,
        )

    def test_seed_command_deactivates_non_canonical_scenarios_and_preserves_history(self):
        canonical_titles = {
            config["canonical_name"] for config in BOUNDARY_FRAMEWORK.values()
        }
        extra_scenario = Scenario.objects.create(
            title="Temporary Debug Scenario",
            description="Should be removed by the seed command.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="Debug objective",
            is_active=True,
        )
        user = User.objects.create_user(
            username="cleanup-check@example.com",
            email="cleanup-check@example.com",
            password="password123",
            role=User.Roles.TRAINEE,
        )
        role_play_prompt = Prompt.objects.create(
            name="Temporary Debug Scenario Role-play Prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=extra_scenario,
            content="Temporary debug prompt.",
            is_active=True,
        )
        feedback_prompt = Prompt.objects.create(
            name="Temporary Debug Scenario Feedback Prompt",
            type=Prompt.PromptTypes.FEEDBACK,
            scenario=extra_scenario,
            content="Temporary feedback prompt.",
            is_active=True,
        )
        reflection_prompt = Prompt.objects.create(
            name="Temporary Debug Scenario Reflection Prompt",
            type=Prompt.PromptTypes.REFLECTION,
            scenario=extra_scenario,
            content="Temporary reflection prompt.",
            is_active=True,
        )
        historical_session = TrainingSession.objects.create(
            user=user,
            scenario=extra_scenario,
            prompt=role_play_prompt,
        )
        historical_feedback = Feedback.objects.create(
            session=historical_session,
            prompt=feedback_prompt,
            overall_score=3,
            strengths="Historical strength",
            improvements="Historical improvement",
        )
        historical_reflection = Reflection.objects.create(
            session=historical_session,
            prompt=reflection_prompt,
            reflection_text="Historical reflection",
        )

        call_command("seed_peertrain")

        self.assertEqual(Scenario.objects.filter(is_active=True).count(), 4)
        self.assertSetEqual(
            set(Scenario.objects.filter(is_active=True).values_list("title", flat=True)),
            canonical_titles,
        )
        extra_scenario.refresh_from_db()
        self.assertFalse(extra_scenario.is_active)
        self.assertTrue(
            TrainingSession.objects.filter(scenario=extra_scenario).exists()
        )
        self.assertTrue(Prompt.objects.filter(pk=role_play_prompt.pk).exists())
        self.assertTrue(Prompt.objects.filter(pk=feedback_prompt.pk).exists())
        self.assertTrue(Prompt.objects.filter(pk=reflection_prompt.pk).exists())
        self.assertTrue(Feedback.objects.filter(pk=historical_feedback.pk).exists())
        self.assertTrue(Reflection.objects.filter(pk=historical_reflection.pk).exists())


class RegradeBoundaryFeedbackCommandTests(TrainingFlowBase):
    def setUp(self):
        super().setUp()
        self.session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.AI,
            content="I feel overwhelmed and do not know what matters most.",
        )
        Message.objects.create(
            session=self.session,
            sender_type=Message.SenderTypes.USER,
            content="What part feels most important for us to explore first?",
        )
        self.feedback = Feedback.objects.create(
            session=self.session,
            prompt=self.feedback_prompt,
            overall_score=5,
            strengths="Existing narrative feedback.",
            improvements="Existing next move.",
            rubric_version="BOUNDARY-SKILLS-1.0",
        )

    def test_regrade_command_requires_explicit_confirmation(self):
        with self.assertRaises(CommandError):
            call_command("regrade_boundary_feedback")

    def test_regrade_command_is_deterministic_and_does_not_call_api(self):
        with patch(
            "training.services.ai._openai_client",
            side_effect=AssertionError("Regrading must not call OpenAI."),
        ):
            call_command("regrade_boundary_feedback", "--confirm")
            call_command("regrade_boundary_feedback", "--confirm")

        self.feedback.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.feedback.overall_score, 3)
        self.assertEqual(self.session.overall_score, 3)
        self.assertEqual(self.feedback.rubric_version, "BOUNDARY-SKILLS-2.0")
        self.assertEqual(self.feedback.scores.count(), 3)


@override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="openai")
class ClosedLoopTrainingTests(TrainingFlowBase):
    def test_session_cannot_finish_before_first_trainee_reply(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I do not know what to do next.",
        )
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"action": "finish"},
        )

        self.assertRedirects(
            response,
            reverse("chat", kwargs={"session_id": session.id}),
        )
        session.refresh_from_db()
        self.assertEqual(session.status, TrainingSession.Statuses.IN_PROGRESS)

    def test_four_turn_session_is_not_marked_as_ended_early(self):
        session = self.create_session()
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I need someone to listen.",
        )
        for turn in range(4):
            Message.objects.create(
                session=session,
                sender_type=Message.SenderTypes.USER,
                content=f"Supportive trainee reply {turn + 1}.",
            )
        self.client.force_login(self.trainee)

        response = self.client.post(
            reverse("chat", kwargs={"session_id": session.id}),
            {"action": "finish"},
        )

        self.assertRedirects(
            response,
            reverse("session_review", kwargs={"session_id": session.id}),
        )
        session.refresh_from_db()
        self.assertFalse(session.ended_early)
        self.assertEqual(session.rubric_version, "BOUNDARY-SKILLS-2.0")

    def test_xp_uses_completion_reflection_and_first_core_scenario_bonuses(self):
        sessions = []
        for boundary_type in (
            Scenario.BoundaryTypes.AGENCY,
            Scenario.BoundaryTypes.RELATIONSHIP,
            Scenario.BoundaryTypes.SAFETY,
        ):
            scenario = Scenario.objects.create(
                title=f"{boundary_type} XP",
                description="XP test",
                boundary_type=boundary_type,
                estimated_duration_min=10,
                learning_objectives="Practice",
                is_active=False,
            )
            session = self.create_session(
                scenario=scenario,
                status=TrainingSession.Statuses.COMPLETED,
                overall_score=3,
            )
            sessions.append(session)

        Reflection.objects.create(
            session=sessions[0],
            prompt=self.reflection_prompt,
            reflection_text="I noticed the agency boundary.",
            action_plan="I will offer two choices.",
        )

        snapshot = progress_snapshot(self.trainee)

        self.assertEqual(snapshot["xp"], 100)
        self.assertEqual(snapshot["completed_sessions"], 3)
        self.assertEqual(snapshot["xp_level"], 2)
        self.assertEqual(
            [row["id"] for row in snapshot["recent_rows"]],
            [session.id for session in reversed(sessions)],
        )

    def test_dashboard_skill_trends_use_dimension_attempts_and_latest_five(self):
        for index in range(6):
            session = self.create_session(
                status=TrainingSession.Statuses.COMPLETED,
                overall_score=3,
            )
            feedback = self.create_feedback_bundle(
                session,
                overall_score=3,
                agency=index % 5 + 1,
                relationship=(index + 1) % 5 + 1,
                safety=(index + 2) % 5 + 1,
            )
            if index == 1:
                FeedbackScore.objects.filter(
                    feedback=feedback,
                    criterion=FeedbackScore.Criteria.RELATIONSHIP,
                ).update(observed=False, score=None)

        snapshot = progress_snapshot(self.trainee)
        trends = {trend["key"]: trend for trend in snapshot["skill_trends"]}

        self.assertEqual(
            [point["attempt"] for point in trends["agency"]["points"]],
            [2, 3, 4, 5, 6],
        )
        self.assertEqual(trends["agency"]["total_observations"], 6)
        self.assertEqual(
            [point["attempt"] for point in trends["relationship"]["points"]],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(trends["relationship"]["total_observations"], 5)
        self.assertNotIn(
            None,
            [point["score"] for point in trends["relationship"]["points"]],
        )
        self.assertEqual(trends["safety"]["point_style"], "triangle")

    def test_legacy_session_routes_redirect_to_the_closed_loop_pages(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED)
        self.client.force_login(self.trainee)

        for route_name in ("feedback", "reflection"):
            with self.subTest(route_name=route_name):
                response = self.client.get(
                    reverse(route_name, kwargs={"session_id": session.id})
                )
                self.assertRedirects(
                    response,
                    reverse("session_review", kwargs={"session_id": session.id}),
                )

        self.assertRedirects(
            self.client.get(reverse("progress")),
            reverse("dashboard"),
        )

    def test_history_reset_requires_explicit_confirmation(self):
        with self.assertRaises(CommandError):
            call_command("reset_training_history")


@override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="openai")
class PageSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_peertrain")
        cls.trainee = User.objects.get(email="alex.chen@university.ac.uk")
        cls.admin = User.objects.get(email="admin@university.ac.uk")
        cls.completed_session = (
            TrainingSession.objects.filter(
                user=cls.trainee,
                status=TrainingSession.Statuses.COMPLETED,
            )
            .select_related("scenario")
            .first()
        )
        cls.chat_scenario = Scenario.objects.filter(is_active=True).first()
        cls.role_prompt = get_prompt_for_type(Prompt.PromptTypes.ROLE_PLAY, cls.chat_scenario)
        cls.chat_session = TrainingSession.objects.create(
            user=cls.trainee,
            scenario=cls.chat_scenario,
            prompt=cls.role_prompt,
        )
        Message.objects.create(
            session=cls.chat_session,
            sender_type=Message.SenderTypes.AI,
            content="Let's begin whenever you are ready.",
            response_source=Message.ResponseSources.SCRIPTED,
        )

    def test_public_pages_render(self):
        pages = [
            (reverse("landing"), "PeerTrain"),
            (reverse("login"), "Welcome back"),
            (reverse("register"), "Create your trainee account"),
            (reverse("admin_register"), "Protected administrator registration"),
        ]

        for url, text in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, text)

    def test_base_pages_use_only_local_vendor_assets(self):
        expected_assets = [
            "vendor/plus-jakarta-sans/plus-jakarta-sans.css",
            "vendor/plus-jakarta-sans/plus-jakarta-sans-latin-wght-normal.woff2",
            "vendor/bootstrap/bootstrap.min.css",
            "vendor/bootstrap/bootstrap.bundle.min.js",
            "vendor/bootstrap-icons/bootstrap-icons.min.css",
            "vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
            "vendor/chartjs/chart.umd.min.js",
        ]
        for asset in expected_assets:
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))

        public_response = self.client.get(reverse("login"))
        self.client.force_login(self.trainee)
        app_response = self.client.get(reverse("dashboard"))
        self.client.force_login(self.admin)
        analytics_response = self.client.get(reverse("admin_analytics"))

        for response in (public_response, app_response, analytics_response):
            with self.subTest(path=response.request["PATH_INFO"]):
                self.assertNotContains(response, "fonts.googleapis.com")
                self.assertNotContains(response, "fonts.gstatic.com")
                self.assertNotContains(response, "cdn.jsdelivr.net")
                self.assertContains(response, "/static/vendor/bootstrap/bootstrap.min.css")
                self.assertContains(
                    response,
                    "/static/vendor/bootstrap/bootstrap.bundle.min.js",
                )

        self.assertContains(
            app_response,
            "/static/vendor/chartjs/chart.umd.min.js",
        )
        self.assertContains(
            analytics_response,
            "/static/vendor/chartjs/chart.umd.min.js",
        )

    def test_landing_restores_login_card_without_public_navigation(self):
        landing_response = self.client.get(reverse("landing"))
        login_response = self.client.get(reverse("login"))

        self.assertNotContains(landing_response, "peertrain-app-topbar-public", html=False)
        self.assertContains(landing_response, 'class="peertrain-card text-center"', html=False)
        self.assertContains(landing_response, "Log In", count=1)
        self.assertContains(landing_response, "Register here")
        self.assertContains(landing_response, "Need an account?")
        self.assertContains(login_response, "peertrain-app-topbar-public", html=False)
        self.assertContains(login_response, "Register here")

    def test_trainee_pages_render(self):
        self.client.force_login(self.trainee)
        pages = [
            (reverse("dashboard"), "Boundary practice, at a glance"),
            (reverse("scenarios"), "Scenario Selection"),
            (reverse("chat", kwargs={"session_id": self.chat_session.id}), "Guidance"),
            (reverse("session_review", kwargs={"session_id": self.completed_session.id}), "Feedback"),
            (reverse("history"), "History"),
            (reverse("history_detail", kwargs={"session_id": self.completed_session.id}), "Conversation"),
        ]

        for url, text in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, text)

    def test_primary_pages_omit_redundant_helper_copy(self):
        self.client.force_login(self.trainee)
        trainee_pages = [
            (
                reverse("scenarios"),
                ["Choose one training scenario and launch a guided role-play conversation."],
            ),
            (
                reverse("chat", kwargs={"session_id": self.chat_session.id}),
                [
                    boundary_framework_for(self.chat_scenario.boundary_type)[
                        "practice_context"
                    ],
                    "LIVE ROLE-PLAY",
                    "Enter to send",
                ],
            ),
            (
                reverse(
                    "session_review",
                    kwargs={"session_id": self.completed_session.id},
                ),
                [
                    "SESSION REVIEW",
                    "Evidence-based feedback and your next boundary action.",
                    "<small>Boundary Skills Rubric</small>",
                    "What happened and what to try next",
                    "Turn feedback into a next action",
                    "Only boundary-specific moments are included.",
                    "These prompts are personalised from this conversation",
                ],
            ),
            (
                reverse("history"),
                [
                    "BOUNDARY NOTEBOOK",
                    "Review complete sessions and revisit boundary moments by skill.",
                    "Integrated practice items are filed under the boundary they demonstrate.",
                    "LATEST FIRST",
                ],
            ),
            (
                reverse(
                    "history_detail",
                    kwargs={"session_id": self.completed_session.id},
                ),
                ["HISTORY ENTRY", "· Boundary Skills Rubric"],
            ),
        ]
        for url, removed_copy in trainee_pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                for text in removed_copy:
                    self.assertNotContains(response, text)

        self.client.logout()
        public_pages = [
            (
                reverse("login"),
                "Sign in to continue your training sessions and progress review.",
            ),
            (
                reverse("register"),
                "Create a simple trainee account to start role-play practice.",
            ),
            (
                reverse("admin_register"),
                "Create an administrator account using the configured registration code.",
            ),
        ]
        for url, removed_copy in public_pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, removed_copy)

        self.client.force_login(self.admin)
        admin_pages = [
            (
                reverse("admin_dashboard"),
                "Monitor training activity, users, prompts, and learning outcomes.",
            ),
            (
                reverse("admin_users"),
                "Review registered users and control account access.",
            ),
            (
                reverse("admin_prompts"),
                "Manage prompt behaviour across the four fixed training boundaries.",
            ),
            (
                reverse("admin_analytics"),
                "Review boundary-skill progression and platform activity.",
            ),
            (
                reverse("admin_settings"),
                "Only the live training limit remains configurable here.",
            ),
            (
                reverse("admin_scoring_reviews"),
                "Review reliability exceptions and a small quality-control sample",
            ),
        ]
        for url, removed_copy in admin_pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, removed_copy)

    def test_dashboard_keeps_xp_help_and_three_axis_boundary_profile(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "How experience points are calculated")
        self.assertContains(response, "20 XP for completing a session")
        self.assertContains(response, 'id="boundary-profile-chart"')
        self.assertContains(response, 'id="boundary-profile-data"')
        self.assertContains(response, "Boundary profile")

    def test_session_review_exposes_the_full_conversation_evidence_ledger(self):
        self.client.force_login(self.trainee)

        response = self.client.get(
            reverse(
                "session_review",
                kwargs={"session_id": self.completed_session.id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Conversation evidence")
        self.assertContains(response, "assessed moment")
        self.assertContains(response, "Turn ")

    def test_dashboard_renders_wiki_navigation_and_unique_section_targets(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        for target in ["dashboard-overview", "dashboard-insights", "dashboard-recent"]:
            self.assertContains(response, f'id="{target}"', count=1)
            self.assertContains(response, f'href="#{target}"', count=2)
        self.assertContains(response, "Quick navigation")
        self.assertNotContains(response, ">ON THIS PAGE<")
        self.assertNotContains(response, ">On this page<")
        self.assertContains(response, "data-dashboard-toc-details")
        self.assertContains(response, "learning.css?v=20260819-1", html=False, status_code=200)
        self.assertContains(response, "progress.js?v=20260729-2", html=False, status_code=200)
        self.assertNotContains(response, "YOUR LEARNING HOME")
        self.assertNotContains(response, "LATEST FIRST")
        self.assertContains(response, "About boundary progress")
        self.assertContains(response, "About boundary profile")
        self.assertContains(response, "prototype five-point rubric")
        self.assertContains(response, "Boundary progress")
        self.assertContains(response, "Boundary profile")
        self.assertNotContains(response, "Boundary Skills Rubric")
        self.assertNotContains(response, "Your latest five observed attempts")
        self.assertNotContains(response, "Average observed point across the three boundary skills")
        self.assertNotContains(response, "Track practice points and review the boundary skills")
        self.assertNotContains(response, "Based on completed practice")
        self.assertNotContains(response, "Observed boundary dimensions")
        self.assertNotContains(response, 'id="boundary-trend-chart"')

    def test_history_boundary_counts_match_each_filtered_session_list(self):
        self.client.force_login(self.trainee)

        all_response = self.client.get(reverse("history"))

        self.assertEqual(all_response.status_code, 200)
        self.assertEqual(
            all_response.context["all_session_count"],
            len(all_response.context["sessions"]),
        )
        self.assertContains(all_response, "All sessions")
        self.assertContains(
            all_response,
            '<h1 class="mb-0">History</h1>',
            html=True,
        )
        self.assertContains(all_response, "Boundaries")
        self.assertNotContains(all_response, "Archives")
        self.assertNotContains(all_response, "Boundary tree")

        for boundary_filter in all_response.context["boundary_filters"]:
            with self.subTest(boundary=boundary_filter["key"]):
                response = self.client.get(
                    reverse("history"),
                    {"boundary": boundary_filter["key"]},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    boundary_filter["count"],
                    len(response.context["sessions"]),
                )
                self.assertTrue(
                    all(
                        boundary_filter["key"] in session.history_boundary_keys
                        for session in response.context["sessions"]
                    )
                )

    def test_legacy_archive_urls_redirect_permanently_to_history(self):
        self.client.force_login(self.trainee)

        list_response = self.client.get(
            f"{reverse('legacy_archives')}?boundary=AGENCY"
        )
        detail_response = self.client.get(
            reverse(
                "legacy_archive_detail",
                kwargs={"session_id": self.completed_session.id},
            )
        )

        self.assertEqual(list_response.status_code, 301)
        self.assertEqual(
            list_response.url,
            f"{reverse('history')}?boundary=AGENCY",
        )
        self.assertEqual(detail_response.status_code, 301)
        self.assertEqual(
            detail_response.url,
            reverse(
                "history_detail",
                kwargs={"session_id": self.completed_session.id},
            ),
        )

    def test_scenario_selection_renders_four_themed_cards_without_detail_metadata(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("scenarios"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="peertrain-card h-100 scenario-card', count=4)
        self.assertContains(response, "Start Scenario", count=4)
        self.assertNotContains(response, "Learning objectives")
        self.assertNotContains(response, 'class="bi bi-clock"', html=False)
        self.assertNotContains(response, "bi-list-check")
        self.assertNotContains(response, "peertrain-scenario-difficulty")
        for former_level in ["Beginner", "Intermediate", "Advanced"]:
            self.assertNotContains(response, former_level)
        expected_visuals = {
            Scenario.BoundaryTypes.AGENCY: (
                "scenario-theme-emotional",
                "images/scenarios/emotional-boundary.svg",
            ),
            Scenario.BoundaryTypes.INTEGRATED: (
                "scenario-theme-privacy",
                "images/scenarios/privacy-boundary.svg",
            ),
            Scenario.BoundaryTypes.SAFETY: (
                "scenario-theme-referral",
                "images/scenarios/referral-boundary.svg",
            ),
            Scenario.BoundaryTypes.RELATIONSHIP: (
                "scenario-theme-burnout",
                "images/scenarios/burnout-boundary.svg",
            ),
        }
        for scenario in Scenario.objects.filter(is_active=True):
            with self.subTest(scenario=scenario.title):
                self.assertContains(response, escape(scenario.title))
                self.assertNotContains(response, escape(scenario.description))
                self.assertContains(
                    response,
                    escape(
                        boundary_framework_for(scenario.boundary_type)[
                            "practice_context"
                        ]
                    ),
                )
                theme_class, icon_asset = expected_visuals[scenario.boundary_type]
                self.assertContains(response, theme_class)
                self.assertContains(response, f"/static/{icon_asset}")
                self.assertIsNotNone(finders.find(icon_asset))
        rendered_html = response.content.decode()
        ordered_items = sorted(
            SCENARIO_LIBRARY,
            key=lambda item: boundary_framework_for(item["boundary_type"])["order"],
        )
        title_positions = [rendered_html.index(escape(item["title"])) for item in ordered_items]
        self.assertEqual(title_positions, sorted(title_positions))

    def test_scenario_visuals_are_mapped_by_boundary_type_not_title(self):
        self.client.force_login(self.trainee)
        scenario = Scenario.objects.get(boundary_type=Scenario.BoundaryTypes.INTEGRATED)
        scenario.title = "Renamed privacy practice"
        scenario.save(update_fields=["title"])

        response = self.client.get(reverse("scenarios"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Renamed privacy practice")
        self.assertContains(response, "Integrated Boundary", html=False)
        self.assertContains(response, "Agency, Relationship &amp; Safety Together")
        self.assertContains(response, "scenario-theme-privacy")
        self.assertContains(response, "/static/images/scenarios/privacy-boundary.svg")

        session = TrainingSession.objects.create(
            user=self.trainee,
            scenario=scenario,
            prompt=get_prompt_for_type(Prompt.PromptTypes.ROLE_PLAY, scenario),
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I need to talk about something private.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        chat_response = self.client.get(reverse("chat", kwargs={"session_id": session.id}))
        self.assertContains(
            chat_response,
            "Clarify what can and cannot be kept confidential early.",
        )

    def test_session_review_combines_feedback_and_reflection(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("session_review", kwargs={"session_id": self.completed_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Boundary notebook")
        self.assertContains(response, "A better reply")
        self.assertContains(response, "What boundary did I notice?")

    def test_demo_chat_page_shows_quick_reply_tools(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quick Test Replies")
        self.assertContains(response, "Auto Better Reply")
        self.assertContains(response, "formnovalidate")
        self.assertContains(response, "Agency Boundary")
        self.assertNotContains(response, "Use these as prompts, not a script.")
        self.assertNotContains(response, "A formative, rule-based coaching read")
        self.assertNotContains(response, "not a clinical judgement")

    def test_finish_button_bypasses_empty_composer_validation(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="finish"', html=False)
        finish_markup = response.content.decode().split('value="finish"', 1)[1].split(">", 1)[0]
        self.assertIn("formnovalidate", finish_markup)

    def test_direct_empty_chat_page_marks_live_opening_as_required(self):
        session = TrainingSession.objects.create(
            user=self.trainee,
            scenario=self.chat_scenario,
            prompt=self.role_prompt,
        )
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-needs-opening="true"', html=False)
        self.assertContains(response, "Retry opening")
        self.assertNotContains(response, "Scenario opener")
        self.assertEqual(session.messages.count(), 0)

    def test_registered_trainee_chat_hides_quick_reply_tools(self):
        registered_trainee = create_user("demo.student@example.com", User.Roles.TRAINEE)
        session = TrainingSession.objects.create(
            user=registered_trainee,
            scenario=self.chat_scenario,
            prompt=self.role_prompt,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="Let's begin whenever you are ready.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(registered_trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Quick Test Replies")
        self.assertNotContains(response, "Auto Better Reply")
        self.assertNotContains(response, "Auto Poor Reply")

    def test_chat_page_shows_live_boundary_map(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Boundary Coach")
        self.assertContains(response, "Send a reply to see coaching.", count=1)
        self.assertNotContains(response, "Live training support")
        self.assertNotContains(response, "Live Boundary Read")
        self.assertNotContains(response, "Scenario focus")
        self.assertNotContains(response, "Current area to strengthen")
        self.assertNotContains(response, "peertrain-boundary-map-grid", html=False)
        self.assertContains(response, 'id="peertrain-chat-log"', html=False)
        self.assertContains(response, 'role="log"', html=False)
        self.assertNotContains(response, 'id="peertrain-chat-window"', html=False)

    def test_live_boundary_map_explains_the_fixed_scoring_evidence(self):
        Message.objects.create(
            session=self.chat_session,
            sender_type=Message.SenderTypes.USER,
            content="That sounds overwhelming. What feels hardest to carry right now?",
            response_source=Message.ResponseSources.USER_INPUT,
        )
        self.client.force_login(self.trainee)

        response = self.client.get(
            reverse("chat", kwargs={"session_id": self.chat_session.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Boundary Skills Rubric")
        self.assertContains(response, "Expected move")
        self.assertContains(response, "Progress")
        self.assertContains(response, "Details")
        self.assertNotContains(response, "Session process")
        self.assertNotContains(response, "Turn-matched coaching")
        self.assertNotContains(response, "Evidence-verified Judge")
        self.assertContains(response, "Not reached yet")
        self.assertNotContains(response, "not a clinical judgement")

    def test_chat_page_renders_collapsible_guidance_sidebar(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-guidance-sidebar', count=1, html=False)
        self.assertContains(response, "Objectives", count=1)
        self.assertContains(response, "Coaching tips", count=1)
        self.assertContains(response, "View guidance", count=1)
        self.assertContains(response, 'class="peertrain-guidance-section"', count=2, html=False)
        self.assertNotContains(response, "SCENARIO SUPPORT")
        self.assertContains(response, f'data-session-id="{self.chat_session.id}"', html=False)
        self.assertContains(response, "/static/js/chat.js?v=20260729-6")
        self.assertContains(response, "data-guidance-toggle", count=2, html=False)
        for former_level in ["Beginner", "Intermediate", "Advanced"]:
            self.assertNotContains(response, former_level)

    def test_chat_script_uses_endpoint_that_cannot_be_shadowed_by_action_buttons(self):
        script_path = finders.find("js/chat.js")

        self.assertIsNotNone(script_path)
        with open(script_path, encoding="utf-8") as script_file:
            script = script_file.read()

        self.assertIn("app.dataset.chatUrl || chatForm?.getAttribute", script)
        self.assertNotIn("fetch(chatForm.action", script)
        self.assertIn('querySelectorAll("[data-user-turn-count]")', script)
        self.assertNotIn('querySelectorAll("[data-user-turns]")', script)

        self.client.force_login(self.trainee)
        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))
        self.assertContains(response, "data-user-turn-count", count=1, html=False)

    def test_chat_page_renders_one_responsive_boundary_coach(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="peertrain-boundary-coach"', count=1, html=False)
        self.assertContains(response, 'class="offcanvas-xl offcanvas-end peertrain-boundary-coach"', html=False)
        self.assertContains(response, 'data-boundary-coach-layout', html=False)
        self.assertContains(response, 'data-boundary-coach-panel', html=False)
        self.assertContains(response, 'data-boundary-coach-rail', html=False)
        self.assertContains(response, 'data-bs-target="#peertrain-boundary-coach"', html=False)
        self.assertContains(response, 'aria-controls="peertrain-boundary-coach-panel"', count=2, html=False)
        self.assertContains(response, f'data-user-id="{self.trainee.id}"', html=False)
        self.assertContains(response, f'data-session-id="{self.chat_session.id}"', html=False)
        self.assertNotContains(response, "peertrain-boundary-watchpoint-panel")
        self.assertNotContains(response, "peertrain-boundary-map-definition")

    def test_chat_page_renders_accessible_five_point_boundary_faces(self):
        self.client.force_login(self.trainee)
        expected_assets = [
            "images/boundary-status/level-1-at-risk.svg",
            "images/boundary-status/level-2-wobbling.svg",
            "images/boundary-status/level-3-mixed.svg",
            "images/boundary-status/level-4-mostly-steady.svg",
            "images/boundary-status/level-5-very-steady.svg",
        ]

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'role="listitem"', count=5)
        self.assertContains(
            response,
            'aria-label="Boundary stability from score 1 at risk to score 5 very steady"',
            html=False,
        )
        self.assertContains(response, 'class="peertrain-boundary-scale-frame"', html=False)
        self.assertNotContains(response, 'data-boundary-scale-scroll', html=False)
        self.assertNotContains(
            response,
            'aria-label="Horizontally scroll the five boundary stability levels"',
            html=False,
        )
        rendered_html = response.content.decode()
        asset_positions = []
        for asset in expected_assets:
            with self.subTest(asset=asset):
                static_url = f"/static/{asset}"
                self.assertContains(response, static_url)
                self.assertIsNotNone(finders.find(asset))
                asset_positions.append(rendered_html.index(static_url))
        self.assertEqual(asset_positions, sorted(asset_positions))
        for legacy_emoji in ["🟢", "🟩", "🟨", "🟧", "🟥"]:
            self.assertNotContains(response, legacy_emoji)

    def test_chat_page_marks_the_current_boundary_face_and_text(self):
        Message.objects.create(
            session=self.chat_session,
            sender_type=Message.SenderTypes.USER,
            content=(
                "I care about you, and I can stay for ten minutes while we think about "
                "one other person you can contact. I cannot be available all night."
            ),
            response_source=Message.ResponseSources.USER_INPUT,
        )
        indicator = build_boundary_status_indicator(self.chat_session)
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-current="true"', count=1, html=False)
        self.assertContains(response, f"Level {indicator.score}")
        self.assertContains(response, indicator.label)
        self.assertContains(response, ">Focus<", html=False)
        self.assertNotContains(response, "Current area to strengthen")
        self.assertNotContains(response, "peertrain-boundary-watchpoint-panel")

    def test_trainee_top_navigation_only_exposes_home_scenarios_and_history(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "peertrain-app-topbar", html=False)
        self.assertContains(response, 'class="peertrain-account-menu-button', html=False)
        self.assertContains(response, 'id="peertrain-mobile-navigation"', html=False)
        self.assertContains(response, reverse("dashboard"))
        self.assertContains(response, reverse("scenarios"))
        self.assertContains(response, reverse("history"))
        self.assertContains(response, "bi-clock-history")
        self.assertContains(response, "<span>History</span>", count=2, html=True)
        self.assertContains(response, "Alex Chen")
        self.assertContains(response, "Log out")
        self.assertNotContains(response, "peertrain-sidebar")
        self.assertNotContains(response, "peertrain-sidebar-user")
        self.assertNotContains(response, ">Feedback<")
        self.assertNotContains(response, ">Reflection<")
        self.assertNotContains(response, ">Chat Training<")

    def test_chat_page_marks_placeholder_messages_clearly(self):
        self.client.force_login(self.trainee)
        Message.objects.create(
            session=self.chat_session,
            sender_type=Message.SenderTypes.AI,
            content="I am not against getting help. I just do not know where to begin.",
            response_source=Message.ResponseSources.PLACEHOLDER,
        )

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backup reply")

    def test_history_detail_shows_saved_reflection(self):
        self.client.force_login(self.trainee)
        Reflection.objects.update_or_create(
            session=self.completed_session,
            defaults={
                "prompt": get_prompt_for_type(
                    Prompt.PromptTypes.REFLECTION,
                    self.completed_session.scenario,
                ),
                "reflection_text": "I would keep the warm tone and make the next-step support option more concrete.",
                "action_plan": "I will name the boundary and one next step.",
            },
        )

        response = self.client.get(
            reverse("history_detail", kwargs={"session_id": self.completed_session.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reflection")
        self.assertContains(response, "I will name the boundary and one next step.")
        self.assertContains(response, "history-review-accordion")
        self.assertContains(response, "peertrain-ai-avatar")
        self.assertContains(response, "peertrain-history-user-avatar")
        self.assertContains(response, "Agency Boundary")
        self.assertContains(response, "learning.css?v=20260819-1", html=False)

    def test_progress_page_explains_empty_state_for_new_trainee(self):
        empty_trainee = User.objects.create_user(
            username="new.trainee@example.com",
            email="new.trainee@example.com",
            password="password123",
            role=User.Roles.TRAINEE,
            first_name="New",
            last_name="Trainee",
        )
        self.client.force_login(empty_trainee)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No completed practice yet")
        self.assertContains(response, "0 XP")

    def test_admin_pages_render(self):
        self.client.force_login(self.admin)
        pages = [
            (reverse("admin_dashboard"), "Admin Dashboard"),
            (reverse("admin_users"), "Admin User Management"),
            (reverse("admin_prompts"), "Admin Prompt Management"),
            (reverse("admin_analytics"), "Training Analytics"),
            (reverse("admin_settings"), "Platform Settings"),
        ]

        for url, text in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, text)

    def test_admin_top_navigation_uses_admin_only_information_architecture(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "peertrain-app-topbar", html=False)
        self.assertContains(response, 'aria-label="Primary navigation"', html=False)
        for label in ["Dashboard", "Users", "Prompts", "Analytics", "Settings"]:
            self.assertContains(response, f"<span>{label}</span>", count=2, html=True)
        self.assertNotContains(response, "<span>Scenarios</span>", html=True)
        self.assertNotContains(response, "<span>History</span>", html=True)
        self.assertContains(response, "<small>Admin</small>", count=2, html=True)
        self.assertNotContains(response, "peertrain-sidebar")

    def test_admin_dashboard_replaces_scenario_management_with_session_status(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "In-progress Sessions")
        self.assertNotContains(
            response,
            "Four research scenarios are fixed and version controlled.",
        )
        self.assertNotContains(response, "Manage scenarios")
        self.assertNotContains(response, "Active Scenarios")

    def test_removed_admin_scenario_route_returns_404(self):
        self.client.force_login(self.admin)

        response = self.client.get("/admin/scenarios/")

        self.assertEqual(response.status_code, 404)

    def test_prompt_management_only_exposes_active_scenarios(self):
        inactive_scenario = Scenario.objects.create(
            title="Archived Research Scenario",
            description="Historical scenario retained for existing sessions.",
            boundary_type=Scenario.BoundaryTypes.AGENCY,
            estimated_duration_min=10,
            learning_objectives="Historical objective",
            is_active=False,
        )
        inactive_prompt = Prompt.objects.create(
            name="Archived Scenario Prompt",
            type=Prompt.PromptTypes.ROLE_PLAY,
            scenario=inactive_scenario,
            content="Historical content",
            is_active=False,
        )

        form = PromptForm()
        self.assertEqual(
            list(form.fields["scenario"].queryset.values_list("boundary_type", flat=True)),
            [item["boundary_type"] for item in SCENARIO_LIBRARY],
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_prompts"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, inactive_scenario.title)
        self.assertNotContains(response, inactive_prompt.name)
        self.assertEqual(
            self.client.get(f"{reverse('admin_prompts')}?edit={inactive_prompt.id}").status_code,
            404,
        )

    def test_prompt_management_default_view_uses_single_category_without_editor(self):
        self.client.force_login(self.admin)
        default_prompt = Prompt.objects.filter(scenario__isnull=True).first()
        scenario_prompt = Prompt.objects.filter(scenario__isnull=False).first()

        response = self.client.get(reverse("admin_prompts"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_group"]["key"], "default")
        self.assertContains(response, default_prompt.name)
        self.assertNotContains(response, scenario_prompt.name)
        self.assertContains(response, "New prompt")
        self.assertNotContains(response, "Content Preview")
        self.assertNotContains(response, 'id="promptEditor"')
        self.assertNotContains(response, "<textarea")

    def test_prompt_management_filters_categories_and_reports_counts(self):
        self.client.force_login(self.admin)
        expected_counts = {
            "default": Prompt.objects.filter(scenario__isnull=True).count(),
            "role_play": Prompt.objects.filter(
                scenario__isnull=False,
                scenario__is_active=True,
                type=Prompt.PromptTypes.ROLE_PLAY,
            ).count(),
            "feedback": Prompt.objects.filter(
                scenario__isnull=False,
                scenario__is_active=True,
                type=Prompt.PromptTypes.FEEDBACK,
            ).count(),
            "reflection": Prompt.objects.filter(
                scenario__isnull=False,
                scenario__is_active=True,
                type=Prompt.PromptTypes.REFLECTION,
            ).count(),
        }

        for group_key in expected_counts:
            with self.subTest(group=group_key):
                response = self.client.get(
                    f"{reverse('admin_prompts')}?group={group_key}"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["active_group"]["key"], group_key)
                self.assertEqual(
                    response.context["active_group"]["count"],
                    expected_counts[group_key],
                )

        response = self.client.get(
            f"{reverse('admin_prompts')}?group=not-a-real-group"
        )
        self.assertEqual(response.context["active_group"]["key"], "default")

    def test_prompt_create_and_edit_queries_open_the_correct_drawer(self):
        self.client.force_login(self.admin)
        role_prompt = Prompt.objects.filter(
            scenario__isnull=False,
            type=Prompt.PromptTypes.ROLE_PLAY,
        ).first()

        create_response = self.client.get(
            f"{reverse('admin_prompts')}?group=feedback&create=1"
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.context["drawer_mode"], "create")
        self.assertEqual(
            create_response.context["form"]["type"].value(),
            Prompt.PromptTypes.FEEDBACK,
        )
        self.assertContains(create_response, 'id="promptEditor"')
        self.assertContains(create_response, "New prompt")

        edit_response = self.client.get(
            f"{reverse('admin_prompts')}?group=default&edit={role_prompt.id}"
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertEqual(edit_response.context["drawer_mode"], "edit")
        self.assertEqual(edit_response.context["editing"], role_prompt)
        self.assertEqual(edit_response.context["active_group"]["key"], "role_play")
        self.assertContains(edit_response, escape(role_prompt.content))

        invalid_edit_response = self.client.get(
            f"{reverse('admin_prompts')}?edit=not-a-number"
        )
        self.assertEqual(invalid_edit_response.status_code, 404)

    def test_invalid_prompt_post_keeps_drawer_open_and_preserves_input(self):
        self.client.force_login(self.admin)
        scenario = Scenario.objects.filter(is_active=True).first()

        response = self.client.post(
            reverse("admin_prompts"),
            {
                "action": "save_prompt",
                "group": "role_play",
                "name": "",
                "type": Prompt.PromptTypes.ROLE_PLAY,
                "scenario": scenario.id,
                "content": "Keep this draft after validation fails.",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["drawer_mode"], "create")
        self.assertIn("name", response.context["form"].errors)
        self.assertContains(response, 'id="promptEditor"')
        self.assertContains(response, "Keep this draft after validation fails.")

    def test_prompt_save_and_toggle_redirect_to_the_relevant_category(self):
        self.client.force_login(self.admin)
        scenario = Scenario.objects.filter(is_active=True).first()

        create_response = self.client.post(
            reverse("admin_prompts"),
            {
                "action": "save_prompt",
                "group": "feedback",
                "name": "Drawer workflow feedback prompt",
                "type": Prompt.PromptTypes.FEEDBACK,
                "scenario": scenario.id,
                "content": "Return structured feedback for the completed conversation.",
                "is_active": "on",
            },
        )

        self.assertRedirects(
            create_response,
            f"{reverse('admin_prompts')}?group=feedback",
            fetch_redirect_response=False,
        )
        prompt = Prompt.objects.get(name="Drawer workflow feedback prompt")
        self.assertTrue(prompt.is_active)

        toggle_response = self.client.post(
            reverse("admin_prompts"),
            {
                "action": "toggle_active",
                "group": "feedback",
                "prompt_id": prompt.id,
            },
        )
        self.assertRedirects(
            toggle_response,
            f"{reverse('admin_prompts')}?group=feedback",
            fetch_redirect_response=False,
        )
        prompt.refresh_from_db()
        self.assertFalse(prompt.is_active)

    def test_django_admin_scenario_is_view_only(self):
        superuser = User.objects.create_superuser(
            username="scenario-auditor@example.com",
            email="scenario-auditor@example.com",
            password="SecurePass123!",
        )
        scenario = Scenario.objects.filter(is_active=True).first()
        original_title = scenario.title
        self.client.force_login(superuser)

        changelist_response = self.client.get(
            reverse("admin:training_scenario_changelist")
        )
        change_url = reverse("admin:training_scenario_change", args=[scenario.id])
        change_response = self.client.get(change_url)
        add_response = self.client.get(reverse("admin:training_scenario_add"))
        delete_response = self.client.get(
            reverse("admin:training_scenario_delete", args=[scenario.id])
        )
        post_response = self.client.post(
            change_url,
            {
                "title": "Attempted overwrite",
                "description": scenario.description,
                "boundary_type": scenario.boundary_type,
                "estimated_duration_min": scenario.estimated_duration_min,
                "learning_objectives": scenario.learning_objectives,
                "is_active": "on",
            },
        )

        self.assertEqual(changelist_response.status_code, 200)
        self.assertEqual(change_response.status_code, 200)
        self.assertEqual(add_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        scenario.refresh_from_db()
        self.assertEqual(scenario.title, original_title)

    def test_admin_settings_page_only_shows_runtime_controls(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("admin_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Runtime Controls")
        self.assertContains(response, "Max exchanges per session")
        self.assertNotContains(response, "Institution name")
        self.assertNotContains(response, "Research contact email")

    def test_admin_can_delete_a_non_current_user(self):
        self.client.force_login(self.admin)
        extra_user = User.objects.create_user(
            username="delete-me@example.com",
            email="delete-me@example.com",
            password="password123",
            role=User.Roles.TRAINEE,
            first_name="Delete",
            last_name="Me",
        )

        response = self.client.post(
            reverse("admin_users"),
            {"action": "delete_user", "user_id": extra_user.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(id=extra_user.id).exists())
        self.assertContains(response, "Deleted user Delete Me.")
        self.assertContains(response, "data-peertrain-success-toast")
        self.assertContains(response, 'data-bs-delay="4500"')
        self.assertNotContains(response, "alert-success")

    def test_admin_cannot_delete_current_account(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("admin_users"),
            {"action": "delete_user", "user_id": self.admin.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(id=self.admin.id).exists())
        self.assertContains(response, "You cannot modify the account you are currently using.")
        self.assertContains(response, "alert-warning")
