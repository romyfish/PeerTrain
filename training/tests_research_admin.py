import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from training.models import (TrainingSession, Scenario, Message, TurnAssessment, Feedback,
    Reflection, ReflectionMessage, LiveFeedbackEvent)
from training.services import assessment
from training.services.suspension import PAUSE_MESSAGE, PracticeFailure
from training.views import _process_chat_turn


@override_settings(DEBUG=True, OPENAI_API_KEY='', GEMINI_API_KEY='', LLM_PROVIDER='openai',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ResearchAdministrationTests(TestCase):
    headers = {'HTTP_ACCEPT': 'application/json', 'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def setUp(self):
        users = get_user_model()
        self.user = users.objects.create_user(username='learner', email='learner@example.org', role='TRAINEE')
        self.other = users.objects.create_user(username='other', email='other@example.org', role='TRAINEE')
        self.admin = users.objects.create_user(username='admin', email='admin@example.org', role='ADMIN')
        self.scenario = Scenario.objects.create(title='Agency', boundary_type='AGENCY', description='Practice',
            estimated_duration_min=10, learning_objectives='Listen')
        self.session = TrainingSession.objects.create(user=self.user, scenario=self.scenario, persona='pso')
        self.url = reverse('chat', args=[self.session.pk])
        self.detail = reverse('admin_record_detail', args=[self.session.pk])
        self.client.force_login(self.user)

    def opening(self):
        return Message.objects.create(session=self.session, sender_type='AI', content='I feel uncertain about what to do.')

    def result(self):
        return assessment.AssessmentResult(relevant_dimensions=['agency'], observed_evidence='What matters to you?',
            opportunity_observed=True, assessment_note='Invites priorities.', overall_status_level=3,
            confidence='medium', feedback_type='affirmation', feedback_text='You invited their priorities.',
            boundary_relevant=True, alternative_reply='What would help?', alternative_reason='Keeps choice open.')

    def complete(self):
        self.session.status = 'COMPLETED'
        self.session.save(update_fields=['status'])

    def assert_paused(self, response, category):
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'session_paused')
        self.assertFalse(response.json()['can_send'])
        self.assertIn(PAUSE_MESSAGE, response.json()['error'])
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'PAUSED')
        self.assertEqual(self.session.failure_category, category)
        self.assertIsNotNone(self.session.paused_at)

    def test_opening_outage_pauses_without_placeholder_and_reload_does_not_retry(self):
        with patch('training.services.ai._openai_role_play_opening', side_effect=RuntimeError):
            self.assert_paused(self.client.post(self.url, {'action': 'start_roleplay'}, **self.headers), 'OPENING_GENERATION_FAILED')
        self.assertFalse(self.session.messages.exists())
        with patch('training.views.generate_ai_opening') as call:
            page = self.client.get(self.url)
            self.assertContains(page, 'Practice temporarily paused')
            self.assertContains(page, 'data-needs-opening="false"')
            for action in ('start_roleplay', 'send', 'finish', 'select_persona', 'prepare_auto_reply'):
                self.assertEqual(self.client.post(self.url, {'action': action, 'content': 'Hello'}, **self.headers).status_code, 409)
            call.assert_not_called()

    def test_failed_assessment_preserves_accepted_message_and_request_id(self):
        self.opening()
        request_id = uuid.uuid4()
        with patch.object(assessment, 'task_call', side_effect=ValueError('invalid structured output')):
            response = self.client.post(self.url, {'content': 'What matters to you?', 'request_id': request_id}, **self.headers)
        self.assert_paused(response, 'ASSESSMENT_FAILED')
        self.assertContains(self.client.get(self.url), 'What matters to you?')
        self.assertIn('What matters to you?', response.json()['transcript_html'])
        self.assertEqual(self.session.messages.filter(sender_type='USER', request_id=request_id).count(), 1)
        self.assertFalse(TurnAssessment.objects.exists())
        with patch.object(assessment, 'task_call') as call:
            self.client.post(self.url, {'content': 'What matters to you?', 'request_id': request_id}, **self.headers)
            call.assert_not_called()
        self.assertEqual(self.session.messages.count(), 2)

    def test_roleplay_failure_preserves_successful_assessment_and_recovery_reuses_turn(self):
        self.opening()
        request_id = uuid.uuid4()
        with patch.object(assessment, 'task_call', return_value=self.result()), patch('training.services.ai._openai_role_play_reply', side_effect=RuntimeError):
            response = self.client.post(self.url, {'content': 'What matters to you?', 'request_id': request_id}, **self.headers)
        self.assert_paused(response, 'ROLEPLAY_GENERATION_FAILED')
        self.assertEqual(TurnAssessment.objects.count(), 1)
        self.client.force_login(self.admin)
        with patch.object(assessment, 'task_call', return_value=assessment.SafetyResult(verdict='ALLOW', reason_codes=[], brief_reason='Consistent.')) as call, patch('training.services.ai._openai_role_play_reply', return_value='I am unsure where to begin.'):
            recovered = self.client.post(self.detail, {'action': 'resume', 'confirm': self.session.pk})
        self.assertEqual(recovered.status_code, 302)
        self.assertEqual(call.call_args.args[0], 'ROLEPLAY_SAFETY')
        self.assertEqual(self.session.messages.filter(sender_type='USER').count(), 1)
        self.assertEqual(self.session.messages.filter(sender_type='AI').count(), 2)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'IN_PROGRESS')
        self.client.force_login(self.user)
        # Replay the original request, with no model calls or duplicated rows.
        with patch.object(assessment, 'task_call') as call:
            replay = self.client.post(self.url, {'content': 'What matters to you?', 'request_id': request_id}, **self.headers)
        self.assertEqual(replay.status_code, 200)
        call.assert_not_called()
        self.assertEqual(self.session.messages.count(), 3)
        self.assertEqual(list(self.session.management_events.values_list('action', flat=True)), ['PAUSE', 'RESUME'])

    def test_safety_block_and_invalid_checker_pause_without_candidate(self):
        for output in (assessment.SafetyResult(verdict='BLOCK', reason_codes=['CASE_LIMIT'], brief_reason='Blocked.'), ValueError('bad JSON')):
            self.session.status = 'IN_PROGRESS'
            self.session.save(update_fields=['status'])
            with patch('training.services.ai._openai_role_play_opening', return_value='I feel overwhelmed by everything today.'), patch.object(assessment, 'task_call', side_effect=[output]):
                self.assert_paused(self.client.post(self.url, {'action': 'start_roleplay'}, **self.headers), 'SAFETY_CHECK_FAILED')
            self.assertFalse(self.session.messages.exists())

    def test_assessment_recovery_reuses_saved_user_and_can_end_as_interrupted(self):
        self.opening()
        with patch.object(assessment, 'task_call', side_effect=RuntimeError):
            self.client.post(self.url, {'content': 'What matters to you?', 'request_id': uuid.uuid4()}, **self.headers)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(self.detail, {'action': 'resume'}).status_code, 400)
        with patch.object(assessment, 'task_call', side_effect=[self.result(), assessment.SafetyResult(verdict='ALLOW', reason_codes=[], brief_reason='Consistent.')]), patch('training.services.ai._openai_role_play_reply', return_value='I feel uncertain.'):
            self.client.post(self.detail, {'action': 'resume', 'confirm': self.session.pk})
        self.assertEqual(self.session.messages.filter(sender_type='USER').count(), 1)
        self.assertEqual(TurnAssessment.objects.count(), 1)
        from training.services.suspension import pause
        self.session.refresh_from_db()
        pause(self.session, 'TEST_FAILURE')
        self.client.post(self.detail, {'action': 'interrupt', 'confirm': self.session.pk})
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'INTERRUPTED')
        self.assertIsNotNone(self.session.end_time)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url, {'content': 'Hello'}, **self.headers).status_code, 409)

    def test_review_outage_preserves_readable_transcript_without_inventing_feedback(self):
        self.opening()
        self.complete()
        url = reverse('session_review', args=[self.session.pk])
        with patch.object(assessment, 'task_call', side_effect=RuntimeError) as call:
            self.assertContains(self.client.get(url), 'requires researcher attention')
            self.assertContains(self.client.get(url), 'I feel uncertain about what to do.')
            self.assertEqual(call.call_count, 1)
        self.assertFalse(Feedback.objects.exists())
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'COMPLETED')
        self.assertIsNotNone(self.session.review_attention_at)
        self.client.force_login(self.admin)
        with patch.object(assessment, 'task_call', return_value=assessment.ReviewResult(strengths='Evidence.', considerations='Consider timing.')):
            self.client.post(self.detail, {'action': 'retry_review', 'confirm': self.session.pk})
        self.assertEqual(Feedback.objects.count(), 1)

    def test_reflection_failure_preserves_message_blocks_retries_and_recovers_once(self):
        self.complete()
        Feedback.objects.create(session=self.session, strengths='Saved', improvements='Saved')
        url = reverse('session_review', args=[self.session.pk])
        request_id = uuid.uuid4()
        with patch.object(assessment, 'task_call', side_effect=RuntimeError) as call:
            response = self.client.post(url, {'action': 'reflect', 'content': 'My reflection', 'request_id': request_id}, **self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertIn('researcher', response.json()['error'])
            self.client.post(url, {'action': 'reflect', 'content': 'My reflection', 'request_id': request_id}, **self.headers)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(ReflectionMessage.objects.filter(sender_type='USER').count(), 1)
        self.assertEqual(ReflectionMessage.objects.filter(sender_type='AI').count(), 1)  # Intro only.
        self.client.force_login(self.admin)
        with patch.object(assessment, 'task_call', return_value=assessment.ReflectionResult(reply='What stood out to you?')) as call:
            self.client.post(self.detail, {'action': 'retry_reflection', 'confirm': self.session.pk})
            self.assertEqual(call.call_count, 1)
        self.assertEqual(ReflectionMessage.objects.filter(sender_type='USER').count(), 1)
        self.assertEqual(ReflectionMessage.objects.filter(sender_type='AI').count(), 2)

    def test_admin_permissions_user_scoping_read_only_and_removed_routes(self):
        other = TrainingSession.objects.create(user=self.other, scenario=self.scenario)
        Message.objects.create(session=other, sender_type='USER', content='Other private record')
        self.opening()
        Reflection.objects.create(session=self.session, reflection_text='Private notes')
        urls = [reverse('admin_users'), reverse('admin_records'), reverse('admin_logs'), self.detail,
            reverse('admin_user_records', args=[self.user.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.admin)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200)
        records = self.client.get(reverse('admin_user_records', args=[self.user.pk]))
        self.assertEqual(list(records.context['sessions'].values_list('pk', flat=True)), [self.session.pk])
        self.assertContains(self.client.get(self.detail), 'Private notes')
        self.assertNotContains(self.client.get(self.detail), '<textarea')
        self.assertEqual(self.client.post(reverse('admin_records'), {'content': 'change'}).status_code, 405)
        for route in ('scoring-reviews', 'analytics', 'settings'):
            self.assertEqual(self.client.get('/admin/' + route + '/').status_code, 404)

    def test_account_deletion_is_confirmed_and_protects_research_records(self):
        self.client.force_login(self.admin)
        url = reverse('admin_users')
        self.client.post(url, {'action': 'delete_user', 'user_id': self.other.pk})
        self.assertTrue(get_user_model().objects.filter(pk=self.other.pk).exists())
        self.client.post(url, {'action': 'delete_user', 'user_id': self.other.pk, 'confirm_delete': self.other.pk})
        self.assertFalse(get_user_model().objects.filter(pk=self.other.pk).exists())
        self.client.post(url, {'action': 'delete_user', 'user_id': self.user.pk, 'confirm_delete': self.user.pk})
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        with self.assertRaises(ProtectedError):
            self.user.delete()
        self.client.post(url, {'action': 'toggle_active', 'user_id': self.user.pk})
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(TrainingSession.objects.filter(pk=self.session.pk).exists())

    def test_live_feedback_events_order_references_and_permissions(self):
        self.opening()
        user_message = Message.objects.create(session=self.session, sender_type='USER', content='Hello')
        url = reverse('live_feedback_event', args=[self.session.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        for kind in ('CLOSE', 'OPEN', 'CLOSE'):
            self.assertEqual(self.client.post(url, {'event_type': kind, 'trainee_turn_number': 1}).status_code, 200)
        events = list(LiveFeedbackEvent.objects.all())
        self.assertEqual([e.event_type for e in events], ['CLOSE', 'OPEN', 'CLOSE'])
        self.assertTrue(all(e.latest_message_id == user_message.pk for e in events))
        self.assertEqual([e.created_at for e in events], sorted(e.created_at for e in events))
        for invalid in ({'event_type': 'LOAD', 'trainee_turn_number': 1}, {'event_type': 'OPEN', 'trainee_turn_number': 2}):
            self.assertEqual(self.client.post(url, invalid).status_code, 400)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, {'event_type': 'OPEN', 'trainee_turn_number': 0}).status_code, 404)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('admin_logs')), 'CLOSE')
        self.assertContains(self.client.get(self.detail), 'Message ' + str(user_message.pk))

    def test_validation_and_demo_failure_do_not_suspend_valid_practice(self):
        self.opening()
        self.assertEqual(self.client.post(self.url, {'content': ''}, **self.headers).status_code, 400)
        self.user.email = 'alex.chen@university.ac.uk'
        self.user.save(update_fields=['email'])
        with patch('training.services.ai._openai_demo_trainee_reply', side_effect=RuntimeError):
            response = self.client.post(self.url, {'action': 'prepare_auto_reply', 'quality': 'helpful'}, **self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['code'], 'test_reply_failed')
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'IN_PROGRESS')
        self.assertEqual(self.session.messages.count(), 1)

    @override_settings(DEBUG=False)
    def test_demo_tools_are_disabled_in_production_even_for_test_account(self):
        self.user.email = 'alex.chen@university.ac.uk'
        self.user.save(update_fields=['email'])
        self.opening()
        self.assertNotContains(self.client.get(self.url), 'Quick Test Replies')
        with patch('training.views.generate_demo_trainee_reply') as call:
            response = self.client.post(self.url, {'action': 'prepare_auto_reply', 'quality': 'helpful'}, **self.headers)
            self.assertEqual(response.status_code, 403)
            call.assert_not_called()

    def test_late_worker_cannot_save_after_researcher_suspends_and_resumes(self):
        from training.services.ai import generate_ai_opening
        from training.services.suspension import pause
        token = self.session.processing_token

        def intervene(*args):
            pause(self.session, 'REQUEST_INTERRUPTED')
            TrainingSession.objects.filter(pk=self.session.pk).update(status='IN_PROGRESS', processing_token=uuid.uuid4())
            return assessment.SafetyResult(verdict='ALLOW', reason_codes=[], brief_reason='Allowed.')

        with patch('training.services.ai._openai_role_play_opening', return_value='I feel uncertain about where to begin.'), patch.object(assessment, 'task_call', side_effect=intervene):
            with self.assertRaises(PracticeFailure):
                generate_ai_opening(self.session)
        self.assertFalse(self.session.messages.exists())
        self.session.refresh_from_db()
        self.assertNotEqual(self.session.processing_token, token)

    def test_test_reply_prompts_use_current_boundary_principles(self):
        from training.services.ai import _trainee_quick_reply_system_prompt
        for boundary in ('AGENCY', 'RELATIONSHIP', 'SAFETY'):
            self.session.scenario.boundary_type = boundary
            for quality in ('helpful', 'unhelpful'):
                prompt = _trainee_quick_reply_system_prompt(self.session, quality)
                for obsolete in ('Band 1', 'Band 2', 'Band 3', 'Band 4', 'Band 5', 'Relational Focus', 'Privacy & Confidentiality', 'Scope & Escalation'):
                    self.assertNotIn(obsolete, prompt)
                self.assertIn('Agency, Relationship and Safety', prompt)
                if boundary == 'SAFETY' and quality == 'helpful':
                    self.assertIn('without assuming escalation is needed', prompt)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class TurnCommitTests(TransactionTestCase):
    def test_message_claim_commits_before_external_call_and_failure_record_commits(self):
        user = get_user_model().objects.create_user(username='commit', role='TRAINEE')
        scenario = Scenario.objects.create(title='Agency', boundary_type='AGENCY', description='Practice', estimated_duration_min=10)
        session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
        Message.objects.create(session=session, sender_type='AI', content='Opening')
        request_id = uuid.uuid4()

        def fail(*args):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Message.objects.filter(request_id=request_id).count(), 1)
            raise PracticeFailure('ASSESSMENT_FAILED')

        with patch('training.views.assess_turn', side_effect=fail):
            result = _process_chat_turn(session, 'Accepted message', 20, request_id=request_id)
        self.assertEqual(result['code'], 'session_paused')
        session.refresh_from_db()
        self.assertEqual(session.status, 'PAUSED')
        self.assertEqual(session.management_events.count(), 1)
        self.assertEqual(session.messages.count(), 2)
