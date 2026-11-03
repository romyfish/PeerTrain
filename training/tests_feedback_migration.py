from unittest.mock import patch

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.urls import reverse


@override_settings(OPENAI_API_KEY='', GEMINI_API_KEY='')
class UnifiedFeedbackMigrationTests(TransactionTestCase):
    def test_historical_modes_keep_records_and_show_all_saved_review_content(self):
        before = [('training', '0033_feedback_overview')]
        after = [('training', '0034_remove_trainingsession_feedback_mode')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(after))
        apps = executor.loader.project_state(before).apps
        user = apps.get_model('accounts', 'User').objects.create(username='historical', role='TRAINEE')
        scenario = apps.get_model('training', 'Scenario').objects.create(
            title='Historical Agency', boundary_type='AGENCY', estimated_duration_min=10)
        session_ids = []
        for mode in ('terminal', 'concurrent', 'combined'):
            session = apps.get_model('training', 'TrainingSession').objects.create(
                user_id=user.pk, scenario_id=scenario.pk, status='COMPLETED', persona='pso', feedback_mode=mode)
            session_ids.append(session.pk)
            message = apps.get_model('training', 'Message').objects.create(
                session_id=session.pk, sender_type='USER', content='What matters to you?')
            apps.get_model('training', 'TurnAssessment').objects.create(
                trainee_message_id=message.pk, feedback_type='affirmation', feedback_text='You invited their priorities.')
            feedback = apps.get_model('training', 'Feedback').objects.create(
                session_id=session.pk, overview='Saved overview', strengths='Saved strengths', improvements='Saved considerations')
            apps.get_model('training', 'FeedbackReviewItem').objects.create(
                feedback_id=feedback.pk, message_id=message.pk, turn_index=1,
                better_reply='What would help?', why_it_matters='Preserves choice.')
            apps.get_model('training', 'Reflection').objects.create(session_id=session.pk, reflection_text='Saved notes')
            apps.get_model('training', 'ReflectionMessage').objects.create(
                session_id=session.pk, sender_type='AI', content='Saved reflection')

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        from accounts.models import User
        from training.models import TrainingSession
        with self.assertRaises(FieldDoesNotExist):
            TrainingSession._meta.get_field('feedback_mode')
        self.client.force_login(User.objects.get(pk=user.pk))
        for session in TrainingSession.objects.filter(pk__in=session_ids):
            self.assertEqual(session.messages.get().content, 'What matters to you?')
            self.assertEqual(session.messages.get().assessment.feedback_text, 'You invited their priorities.')
            self.assertEqual(session.feedback.review_items.count(), 1)
            self.assertEqual(session.reflection.reflection_text, 'Saved notes')
            self.assertEqual(session.reflection_messages.get().content, 'Saved reflection')
            with patch('training.services.assessment.task_call') as call:
                response = self.client.get(reverse('session_review', args=[session.pk]))
            call.assert_not_called()
            for text in ('What matters to you?', 'You invited their priorities.', 'What would help?',
                         'Preserves choice.', 'Saved overview', 'Saved notes', 'Saved reflection'):
                self.assertContains(response, text)
