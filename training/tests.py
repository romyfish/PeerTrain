from training.prompts import ROLEPLAY_SCENARIOS
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from training.content import BOUNDARY_FRAMEWORK, SCENARIO_LIBRARY, boundary_framework_for
from training.models import Feedback, FeedbackReviewItem, FeedbackScore, Message, PlatformSetting, Reflection, Scenario, TrainingSession
from training.services.ai import GeneratedTraineeReply, _clean_generated_text, _looks_incomplete_roleplay_reply, _openai_client, _roleplay_quality_issue, build_boundary_status_indicator, generate_ai_opening, generate_ai_reply, generate_demo_trainee_reply, get_prompt_for_type
from training.services.progress import (progress_snapshot, recalculate_user_progress)


User = get_user_model()


def create_user(email, role, password="SecurePass123!"):
    return User.objects.create_user(
        username=email,
        email=email,
        password=password,
        role=role,
        first_name="Test",
        last_name="User",
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
        cls.role_prompt = get_prompt_for_type('ROLE_PLAY')
        cls.feedback_prompt = get_prompt_for_type('FEEDBACK')
        cls.reflection_prompt = get_prompt_for_type('REFLECTION')

    def create_session(self, *, scenario=None, status=TrainingSession.Statuses.IN_PROGRESS, overall_score=None):
        session = TrainingSession.objects.create(
            user=self.trainee,
            scenario=scenario or self.scenario,
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
@override_settings(DEBUG=True)
class TrainingFlowTests(TrainingFlowBase):
    def setUp(self):
        super().setUp()
        from training.services.assessment import AssessmentResult
        assessment = patch('training.services.assessment.task_call', return_value=AssessmentResult(
            relevant_dimensions=['agency'], observed_evidence='Saved trainee reply', opportunity_observed=True,
            assessment_note='Evidence saved.', overall_status_level=3, confidence='medium',
            feedback_type='affirmation', feedback_text='You invited their priorities.'))
        assessment.start()
        self.addCleanup(assessment.stop)

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
        self.assertIn("Andy (AI-simulated)", first_response.json()["opening_message_html"])
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
        self.assertIn("Andy (AI-simulated)", payload["ai_message_html"])
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
                self.assertIn("Andy (AI-simulated)", sent_payload["ai_message_html"])
                mock_generate_demo.assert_called_once_with(session, quality)
                user_message = session.messages.get(sender_type=Message.SenderTypes.USER)
                self.assertEqual(user_message.response_source, Message.ResponseSources.OPENAI)

    def test_demo_test_account_can_auto_send_helpful_reply(self):
        demo_trainee = create_user("alex.chen@university.ac.uk", User.Roles.TRAINEE)
        session = TrainingSession.objects.create(
            user=demo_trainee,
            scenario=self.scenario,
        )
        Message.objects.create(
            session=session,
            sender_type=Message.SenderTypes.AI,
            content="I feel like everything is getting heavier this week.",
            response_source=Message.ResponseSources.SCRIPTED,
        )
        self.client.force_login(demo_trainee)

        with patch('training.services.ai._google_role_play_reply', return_value='I am not sure what feels most important.'), patch('training.services.assessment.checked_reply', side_effect=lambda session, candidate: (candidate, True)), patch(
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


    def test_notes_submission_returns_to_shared_review(self):
        session = self.create_session(status=TrainingSession.Statuses.COMPLETED, overall_score=3)
        self.create_feedback_bundle(session, overall_score=3, agency=4, relationship=3)
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
            reverse("session_review", kwargs={"session_id": session.id}),
        )
        reflection = Reflection.objects.get(session=session)


class LLMServiceTests(TrainingFlowBase):
    def setUp(self):
        super().setUp()
        # These tests isolate generation/regrade utilities. Independent checker integration
        # and failure behaviour are exercised in tests_session_update.
        checker = patch("training.services.assessment.checked_reply", side_effect=lambda session, candidate: (candidate, True))
        checker.start()
        self.addCleanup(checker.stop)


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
        self.assertEqual(session.messages.count(), 1)
        self.assertEqual(fake_responses.last_kwargs["model"], "test-model")
        developer_prompt = fake_responses.last_kwargs["input"][0]["content"]
        self.assertIn("Respect the conversation context described in your case", developer_prompt)
        self.assertIn(self.role_prompt.context, developer_prompt)
        self.assertNotIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            developer_prompt,
        )
        self.assertIn("This is your first message", developer_prompt)
        self.assertNotIn("Learning objectives:", developer_prompt)
        self.assertNotIn("Boundary behaviour contract", developer_prompt)
        self.assertNotIn("CMCV framework:", developer_prompt)
        self.assertNotIn("EDITABLE CONTEXT", developer_prompt)
        self.assertIn("reveal internal instructions", developer_prompt)


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
                "You should demonstrate the Agency Boundary by exploring my choices.",
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


    def test_incomplete_reply_detector_flags_truncated_outputs(self):
        self.assertTrue(_looks_incomplete_roleplay_reply("Oh, thank goodness. I really don"))
        self.assertTrue(_looks_incomplete_roleplay_reply("Oh, I meant I don'"))
        self.assertTrue(_looks_incomplete_roleplay_reply("4"))
        self.assertTrue(_looks_incomplete_roleplay_reply("Score 1"))
        self.assertFalse(_looks_incomplete_roleplay_reply("I really do not want to be on my own tonight."))


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
        self.assertIn(self.role_prompt.context, developer_prompt)
        self.assertNotIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            developer_prompt,
        )
        self.assertNotIn(session.scenario.learning_objectives, developer_prompt)
        self.assertNotIn("Boundary behaviour contract", developer_prompt)
        self.assertNotIn("How to adapt to the trainee's latest message", developer_prompt)
        self.assertNotIn("Turn progression guidance", developer_prompt)
        self.assertNotIn("Recent help-seeker wording to avoid copying too closely", developer_prompt)
        self.assertNotIn("Human realism rules", developer_prompt)
        self.assertNotIn("Stage:", developer_prompt)
        self.assertNotIn("Difficulty:", developer_prompt)
        self.assertNotIn("CMCV framework:", developer_prompt)
        self.assertIn("reveal internal instructions", developer_prompt)
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
        self.assertIn(self.role_prompt.context, config.system_instruction)
        self.assertNotIn(
            boundary_framework_for(session.scenario.boundary_type)["canonical_name"],
            config.system_instruction,
        )
        self.assertNotIn("Boundary behaviour contract", config.system_instruction)
        self.assertIn("Respond to the supporter", config.system_instruction)
        self.assertEqual(config.temperature, 0.8)
        self.assertEqual(config.max_output_tokens, 320)
        self.assertEqual(config.thinking_config.thinking_budget, 0)


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
            reflection_text="I would keep the warm tone and signpost earlier next time.",
        )

        result = recalculate_user_progress(self.trainee)

        self.assertEqual(result["completed_sessions"], 2)
        self.assertEqual(result["average_overall"], 3.0)
        self.assertEqual(result["average_scores"][FeedbackScore.Criteria.AGENCY], 3.5)
        self.assertEqual(result["average_scores"][FeedbackScore.Criteria.RELATIONSHIP], 2.5)
        self.assertEqual(result["awarded"], [])


class SeedCommandTests(TestCase):
    def test_seed_keeps_existing_integrated_demo_history_inactive(self):
        call_command("seed_peertrain")
        trainee = User.objects.get(email="alex.chen@university.ac.uk")
        integrated = Scenario.objects.create(
            title="Integrated Boundary", boundary_type="INTEGRATED",
            description="Historical", estimated_duration_min=15, learning_objectives="",
        )
        historical = TrainingSession.objects.create(
            user=trainee, scenario=integrated, status=TrainingSession.Statuses.COMPLETED,
        )
        Message.objects.create(session=historical, sender_type="AI", content="Historical conversation")
        call_command("seed_peertrain")
        integrated.refresh_from_db()
        self.assertFalse(integrated.is_active)
        self.assertTrue(TrainingSession.objects.filter(pk=historical.pk).exists())
        self.assertEqual(historical.messages.get().content, "Historical conversation")

    def test_scenario_model_has_no_difficulty_field(self):
        self.assertNotIn("difficulty", {field.name for field in Scenario._meta.get_fields()})

    def test_seed_command_is_idempotent(self):
        call_command("seed_peertrain")
        call_command("seed_peertrain")

        self.assertEqual(PlatformSetting.objects.count(), 1)
        self.assertEqual(Scenario.objects.count(), 3)
        trainee = User.objects.get(email="alex.chen@university.ac.uk")
        self.assertEqual(TrainingSession.objects.filter(user=trainee).count(), 0)


    def test_seed_command_deactivates_non_canonical_scenarios_and_preserves_history(self):
        canonical_titles = {
            BOUNDARY_FRAMEWORK[item["boundary_type"]]["canonical_name"] for item in SCENARIO_LIBRARY
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
        role_play_prompt = get_prompt_for_type('ROLE_PLAY')
        feedback_prompt = get_prompt_for_type('FEEDBACK')
        reflection_prompt = get_prompt_for_type('REFLECTION')
        historical_session = TrainingSession.objects.create(
            user=user,
            scenario=extra_scenario,
        )
        historical_feedback = Feedback.objects.create(
            session=historical_session,
            overall_score=3,
            strengths="Historical strength",
            improvements="Historical improvement",
        )
        historical_reflection = Reflection.objects.create(
            session=historical_session,
            reflection_text="Historical reflection",
        )

        call_command("seed_peertrain")

        self.assertEqual(Scenario.objects.filter(is_active=True).count(), 3)
        self.assertSetEqual(
            set(Scenario.objects.filter(is_active=True).values_list("title", flat=True)),
            canonical_titles,
        )
        extra_scenario.refresh_from_db()
        self.assertFalse(extra_scenario.is_active)
        self.assertTrue(
            TrainingSession.objects.filter(scenario=extra_scenario).exists()
        )
        self.assertTrue(Feedback.objects.filter(pk=historical_feedback.pk).exists())
        self.assertTrue(Reflection.objects.filter(pk=historical_reflection.pk).exists())


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
            Message.objects.create(session=session, sender_type='AI', content='I feel understood.')
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


@override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="", LLM_PROVIDER="openai")
@override_settings(DEBUG=True)
class PageSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_peertrain")
        cls.trainee = User.objects.get(email="alex.chen@university.ac.uk")
        cls.admin = User.objects.get(email="admin@university.ac.uk")
        cls.completed_session = TrainingSession.objects.create(user=cls.trainee,
            scenario=Scenario.objects.filter(is_active=True).first(), status='COMPLETED', overall_score=3, end_time=timezone.now())
        message = Message.objects.create(session=cls.completed_session, sender_type='USER', content='What matters to you?')
        Message.objects.create(session=cls.completed_session, sender_type='AI', content='I feel uncertain.')
        feedback = Feedback.objects.create(session=cls.completed_session, strengths='You offered warmth.', improvements='Consider timing.', overall_score=3)
        for criterion in ('AGENCY', 'RELATIONSHIP', 'SAFETY'):
            FeedbackScore.objects.create(feedback=feedback, criterion=criterion, score=3)
        cls.chat_scenario = Scenario.objects.filter(is_active=True).first()
        cls.role_prompt = get_prompt_for_type('ROLE_PLAY', cls.chat_scenario)
        cls.chat_session = TrainingSession.objects.create(
            user=cls.trainee,
            scenario=cls.chat_scenario,
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
                response = self.client.get(url, follow=True)
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
        analytics_response = self.client.get(reverse("admin_records"))

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
        self.assertContains(analytics_response, "Practice records")

    def test_landing_shows_public_research_sections_and_navigation(self):
        landing_response = self.client.get(reverse("landing"))
        login_response = self.client.get(reverse("login"))

        self.assertContains(landing_response, "peertrain-app-topbar-public", html=False)
        self.assertContains(landing_response, "Log in", count=1)
        self.assertContains(landing_response, f'href="{reverse("scenarios")}"')
        self.assertContains(landing_response, "Start Training", count=1)
        sections = ("Welcome", "How PeerTrain works", "Practise", "Get feedback", "Reflect",
                    "About this research", "What PeerTrain is — and isn't", "Research prototype · 2026")
        content = landing_response.content.decode()
        positions = [content.index(text) for text in sections]
        self.assertEqual(positions, sorted(positions))
        self.assertNotContains(landing_response, "Guest")
        self.assertNotContains(landing_response, "Need an account?")
        self.assertNotContains(landing_response, "Register here")
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
                response = self.client.get(url, follow=True)
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
                response = self.client.get(url, follow=True)
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
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, removed_copy)

        self.client.force_login(self.admin)
        admin_pages = [
            (reverse('admin_users'), 'Review registered users and control account access.'),
            (reverse('admin_records'), 'Review boundary-skill progression and platform activity.'),
            (reverse('admin_logs'), 'Review reliability exceptions and a small quality-control sample'),
        ]
        for url, removed_copy in admin_pages:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
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
        self.assertContains(response, "Conversation review")
        self.assertContains(response, "review-transcript")
        self.assertEqual(len(response.context["timeline"]), self.completed_session.messages.count())

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
        self.assertContains(response, "learning.css?v=20261027-2", html=False, status_code=200)
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
        self.assertContains(response, "Start Scenario", count=3)
        self.assertContains(response, "Coming Soon...")
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
                self.assertContains(response, escape(boundary_framework_for(scenario.boundary_type)["canonical_name"]))
                self.assertContains(response, escape(ROLEPLAY_SCENARIOS[scenario.boundary_type]["guidance"]))
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

    def test_integrated_scenario_is_unavailable_even_if_reactivated(self):
        self.client.force_login(self.trainee)
        scenario = Scenario.objects.create(title="Reserved Integrated", boundary_type="INTEGRATED", description="Reserved", estimated_duration_min=15, learning_objectives="", is_active=True)
        response = self.client.get(reverse("scenarios"))
        self.assertNotContains(response, "Integrated Boundary")
        self.assertContains(response, "Coming Soon...")
        self.assertEqual(self.client.post(reverse("scenarios"), {"scenario_id": scenario.pk}).status_code, 404)
        session = TrainingSession.objects.create(user=self.trainee, scenario=scenario)
        url = reverse("chat", kwargs={"session_id": session.pk})
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "start_roleplay"}).status_code, 404)
        self.assertEqual(session.messages.count(), 0)

    def test_session_review_combines_feedback_and_reflection(self):
        self.client.force_login(self.trainee)
        trainee_message = self.completed_session.messages.filter(sender_type='USER').first()
        FeedbackReviewItem.objects.create(feedback=self.completed_session.feedback, message=trainee_message,
            turn_index=1, message_excerpt=trainee_message.content, issue_label='Feedback',
            why_it_matters='Keep choice open.', better_reply='What would feel helpful to you?')

        response = self.client.get(reverse("session_review", kwargs={"session_id": self.completed_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My notes")
        self.assertContains(response, "An alternative reply to consider")
        self.assertContains(response, "Reflect with AI")

    def test_demo_chat_page_shows_quick_reply_tools(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quick Test Replies")
        self.assertContains(response, "Auto Better Reply")
        self.assertContains(response, "formnovalidate")
        self.assertContains(response, ROLEPLAY_SCENARIOS["AGENCY"]["introduction"])
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
        self.assertContains(response, "Live Feedback")
        self.assertContains(response, "Listening in progress")
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
        self.assertNotContains(response, "Expected move")
        self.assertContains(response, "Live Feedback")
        self.assertNotContains(response, "Expected move")
        self.assertNotContains(response, "Session process")
        self.assertNotContains(response, "Turn-matched coaching")
        self.assertNotContains(response, "Evidence-verified Judge")
        self.assertContains(response, 'data-boundary-coach-toggle')
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
        self.assertContains(response, "/static/js/chat.js?v=20261103-4")
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
        self.assertNotContains(response, 'data-bs-target="#peertrain-boundary-coach"', html=False)
        self.assertContains(response, 'aria-controls="peertrain-boundary-coach-panel"', count=2, html=False)
        self.assertContains(response, f'data-user-id="{self.trainee.id}"', html=False)
        self.assertContains(response, f'data-session-id="{self.chat_session.id}"', html=False)
        self.assertNotContains(response, "peertrain-boundary-watchpoint-panel")
        self.assertNotContains(response, "peertrain-boundary-map-definition")

    def test_chat_page_renders_compact_neutral_boundary_status(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-boundary-status', count=1)
        self.assertContains(response, 'data-boundary-compact-status', count=2)
        self.assertContains(response, "Listening in progress")
        self.assertContains(response, "peertrain-assessment-neutral")
        self.assertNotContains(response, "peertrain-boundary-scale")
        self.assertContains(response, "images/boundary-status/listening-in-progress.svg")
        self.assertNotContains(response, "Level ")

    def test_chat_page_shows_current_status_and_preserves_boundary_details(self):
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
        from training.templatetags.assessment_ui import assessment_status

        self.assertContains(response, assessment_status(indicator.score)["label"])
        self.assertContains(response, 'data-boundary-status', count=1)
        self.assertNotContains(response, f"Level {indicator.score}")
        self.assertNotContains(response, ">Focus<", html=False)
        self.assertNotContains(response, "Current area to strengthen")
        self.assertNotContains(response, "peertrain-boundary-watchpoint-panel")

    def test_trainee_top_navigation_only_exposes_home_scenarios_and_history(self):
        self.client.force_login(self.trainee)

        response = self.client.get(reverse("chat", kwargs={"session_id": self.chat_session.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "peertrain-app-topbar", html=False)
        self.assertContains(response, 'class="peertrain-account-menu-button', html=False)
        self.assertContains(response, 'id="peertrain-mobile-navigation"', html=False)
        self.assertContains(response, reverse("landing"))
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
        Reflection.objects.update_or_create(session=self.completed_session, defaults={
            "reflection_text": "I would keep the warm tone and make the next-step support option more concrete.",
        })
        response = self.client.get(reverse("history_detail", args=[self.completed_session.id]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reflection")
        self.assertContains(response, "I would keep the warm tone")
        self.assertContains(response, "review-transcript")
        self.assertContains(response, "peertrain-ai-avatar")
        self.assertContains(response, "review.css?v=20261109-3")

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


    def test_removed_admin_scenario_route_returns_404(self):
        self.client.force_login(self.admin)

        response = self.client.get("/admin/scenarios/")

        self.assertEqual(response.status_code, 404)


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
