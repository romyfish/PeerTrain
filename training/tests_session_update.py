import json
import uuid
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from training.models import Feedback, FeedbackReviewItem, Message, Reflection, Scenario, TrainingSession, TurnAssessment
from training import prompts
from training.prompts import REFLECTION_OPENING, TASK_PROMPTS, persona_prompt_for
from training.services import assessment as tasks
from training.services.ai import generate_ai_opening, generate_ai_reply, generate_feedback, _roleplay_system_prompt, get_prompt_for_type
from training.templatetags.assessment_ui import boundary_highlights


@override_settings(OPENAI_API_KEY='', GEMINI_API_KEY='', LLM_PROVIDER='openai',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SessionUpdateTests(TestCase):
    headers = {'HTTP_ACCEPT': 'application/json', 'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='learner', email='learner@example.com', password='test', role='TRAINEE')
        self.other = get_user_model().objects.create_user(username='other', email='other@example.com', password='test', role='TRAINEE')
        self.scenario = Scenario.objects.create(title='Agency', boundary_type='AGENCY', description='Practice',
            estimated_duration_min=10, learning_objectives='Listen')
        self.session = TrainingSession.objects.create(user=self.user, scenario=self.scenario, persona='pso')
        self.client.force_login(self.user)
        self.url = reverse('session_review', args=[self.session.pk])
        self.chat_url = reverse('chat', args=[self.session.pk])

    def message(self, content='What matters most to you?', sender='USER'):
        return Message.objects.create(session=self.session, sender_type=sender, content=content)

    def result(self, kind='affirmation'):
        return tasks.AssessmentResult(relevant_dimensions=['agency'], observed_evidence='What matters most to you?',
            opportunity_observed=True, assessment_note='Invites their priorities.', overall_status_level=3,
            confidence='medium', feedback_type=kind, feedback_text='You invited their priorities.' if kind != 'none' else None,
            roleplay_cue='They may clarify what feels most important.', review_flag=True, safety_flag=False, boundary_relevant=True)

    def complete(self):
        self.session.status = 'COMPLETED'
        self.session.save()
        return Feedback.objects.create(session=self.session, overall_score=3, strengths='Agency was supported.',
            improvements='Consider the timing of options.')

    def verdict(self, verdict='ALLOW', reply=None):
        return tasks.SafetyResult(verdict=verdict, reason_codes=[], brief_reason='Case consistent.', safe_reply=reply)

    def test_ajax_completion_is_idempotent_and_leaves_generation_to_review(self):
        for turns in (1, 3, 4, 20):
            with self.subTest(turns=turns):
                session = TrainingSession.objects.create(user=self.user, scenario=self.scenario, persona='pso')
                for turn in range(turns):
                    Message.objects.create(session=session, sender_type='USER', content=f'Reply {turn}')
                    Message.objects.create(session=session, sender_type='AI', content='Thank you.')
                url = reverse('chat', args=[session.pk])
                with patch('training.views.generate_feedback') as generate:
                    first = self.client.post(url, {'action': 'finish'}, **self.headers)
                    session.refresh_from_db()
                    end_time = session.end_time
                    second = self.client.post(url, {'action': 'finish'}, **self.headers)
                    generate.assert_not_called()
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json(), {'ok': True, 'review_url': reverse('session_review', args=[session.pk])})
                self.assertEqual(second.json(), first.json())
                session.refresh_from_db()
                self.assertEqual(session.status, 'COMPLETED')
                self.assertEqual(session.ended_early, turns < 4)
                self.assertEqual(session.end_time, end_time)
                self.assertFalse(Feedback.objects.filter(session=session).exists())

    def test_completion_validation_preserves_active_session(self):
        response = self.client.post(self.chat_url, {'action': 'finish'}, **self.headers)
        self.assertEqual(response.status_code, 409)
        self.message()
        response = self.client.post(self.chat_url, {'action': 'finish'}, **self.headers)
        self.assertEqual(response.status_code, 409)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'IN_PROGRESS')
        self.assertIsNone(self.session.end_time)

    def test_assessment_saved_without_visibility_context(self):
        message = self.message()
        with patch.object(tasks, 'task_call', return_value=self.result()) as call:
            row = tasks.assess_turn(self.session, message)
            self.assertEqual(row.overall_status_level, 3)
            self.assertEqual(row.feedback_type, 'affirmation')
            self.assertEqual(row.trainee_message, message)
            self.assertTrue(row.feedback_text)
            tasks.assess_turn(self.session, message)
            self.assertEqual(call.call_count, 1)
            context = json.dumps(call.call_args.args[1])
            for forbidden in ('feedback_mode', 'sidebar', 'visibility', 'terminal', 'concurrent', 'combined'):
                self.assertNotIn(forbidden, context)

    def test_assessment_failure_creates_no_assessment(self):
        with patch.object(tasks, 'task_call', side_effect=ValueError('invalid')):
            with self.assertRaises(tasks.PracticeFailure):
                tasks.assess_turn(self.session, self.message())
        self.assertFalse(TurnAssessment.objects.exists())

    def test_warm_opening_keeps_unobserved_status_and_feedback(self):
        message = self.message('Hello, it is good to meet you.')
        result = self.result().model_copy(update={
            'relevant_dimensions': [], 'observed_evidence': None,
            'opportunity_observed': False, 'boundary_relevant': False,
            'assessment_note': 'Warm opening; no meaningful boundary opportunity yet.',
            'overall_status_level': 0, 'feedback_text': 'You offered a warm welcome.',
        })
        with patch.object(tasks, 'task_call', return_value=result) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(row.overall_status_level, 0)
        self.assertEqual(row.feedback_text, 'You offered a warm welcome.')
        self.assertIsNone(row.record['alternative_reply'])
        body = self.client.get(self.chat_url).content.decode()
        self.assertIn('You offered a warm welcome.', body)
        self.assertNotIn('peertrain-boundary-tip-label', body)
        self.assertNotIn('peertrain-live-alternative', body)

    def test_early_safety_listening_is_regular_feedback_without_future_coaching(self):
        self.scenario.boundary_type = 'SAFETY'
        self.scenario.save(update_fields=['boundary_type'])
        self.message('Just one of those days where I am trying to act like I am fine and mostly getting away with it.', 'AI')
        message = self.message("Oh what happened? I'm here to listen")
        result = self.result().model_copy(update={
            'relevant_dimensions': [], 'observed_evidence': None,
            'opportunity_observed': False, 'boundary_relevant': False,
            'assessment_note': 'Supportive invitation after vague distress; no identifiable safety concern yet.',
            'overall_status_level': 0,
            'feedback_text': 'You invite them to share more and offer a listening presence.',
            'alternative_reply': None,
        })
        with patch.object(tasks, 'task_call', return_value=result):
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(row.overall_status_level, 0)
        self.assertFalse(row.record['boundary_relevant'])
        self.assertIsNone(row.record['alternative_reply'])
        body = self.client.get(self.chat_url).content.decode()
        self.assertIn(result.feedback_text, body)
        self.assertNotIn('peertrain-boundary-tip-label', body)
        self.assertIn('do not anticipate a later disclosure', prompts.ASSESSMENT_SYSTEM)

    def test_duplicate_boundary_tip_prefix_gets_one_correction(self):
        message = self.message('It is your choice. What feels right to you?')
        base = self.result().model_copy(update={
            'feedback_text': 'You leave the decision with them, supporting their agency.',
            'alternative_reply': None,
        })
        prefixed = base.model_copy(update={'feedback_text': 'Boundary Tip: ' + base.feedback_text})
        with patch.object(tasks, 'task_call', side_effect=[prefixed, base]) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 2)
        self.assertIn('UI supplies the Boundary tips label', call.call_args.args[1]['validation_feedback'])
        body = self.client.get(self.chat_url).content.decode()
        self.assertIn(base.feedback_text, body)
        self.assertNotIn('Boundary Tip:', body)
        self.assertEqual(row.feedback_text, base.feedback_text)

    def test_alternative_framing_and_quotes_get_one_correction(self):
        message = self.message('I will make that decision for you.')
        base = self.result().model_copy(update={
            'observed_evidence': 'I will make that decision for you',
            'assessment_note': '"I will make that decision for you" takes over the choice.',
            'overall_status_level': 2,
            'feedback_text': 'You want to help; leaving the decision with them would support their agency.',
            'alternative_reply': 'What feels right to you?',
            'alternative_reason': 'Keeps the choice with them.',
        })
        framed = base.model_copy(update={'alternative_reply': 'You could ask: "What feels right to you?"'})
        with patch.object(tasks, 'task_call', side_effect=[framed, base]) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 2)
        self.assertIn('without framing or quotation marks', call.call_args.args[1]['validation_feedback'])
        self.assertEqual(row.record['alternative_reply'], 'What feels right to you?')
        body = self.client.get(self.chat_url).content.decode()
        self.assertIn('<q><em>What feels right to you?</em></q>', body)

    def test_stylistic_suggestion_does_not_lower_status_or_require_alternative(self):
        message = self.message('That sounds hard. Tell me more if you would like.')
        result = self.result().model_copy(update={
            'relevant_dimensions': [], 'observed_evidence': None,
            'opportunity_observed': False, 'boundary_relevant': False,
            'assessment_note': 'Appropriate listening; a more open question would only be optional.',
            'overall_status_level': 0, 'feedback_text': 'You leave room for them to share at their pace.',
        })
        with patch.object(tasks, 'task_call', return_value=result):
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(row.overall_status_level, 0)
        self.assertIsNone(row.record['alternative_reply'])

    def test_appropriate_boundary_choice_has_positive_status_and_tip_without_alternative(self):
        self.message('Can you decide what I should do?', 'AI')
        message = self.message('It is your choice. What feels right to you?')
        result = self.result().model_copy(update={
            'observed_evidence': 'It is your choice',
            'assessment_note': 'Returns the decision to the help-seeker after a direct request to decide for them.',
            'overall_status_level': 4,
            'feedback_text': 'You keep the decision with them, supporting their agency while staying engaged.',
            'alternative_reply': None,
        })
        with patch.object(tasks, 'task_call', return_value=result) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(row.overall_status_level, 4)
        self.assertTrue(row.record['boundary_relevant'])
        self.assertTrue(row.record['opportunity_observed'])
        self.assertIsNone(row.record['alternative_reply'])
        body = self.client.get(self.chat_url).content.decode()
        self.assertIn('Boundary tips', body)
        self.assertIn(result.feedback_text, body)
        self.assertNotIn('peertrain-live-alternative', body)

    def test_problematic_boundary_reply_has_grounded_low_status_and_alternative(self):
        message = self.message('I will make that decision for you.')
        result = self.result().model_copy(update={
            'observed_evidence': 'I will make that decision for you',
            'assessment_note': 'The trainee says "I will make that decision for you", taking over the help-seeker\'s choice.',
            'overall_status_level': 2,
            'feedback_text': 'You want to help; leaving the decision with them would support their agency.',
            'alternative_reply': 'What options feel right to you?',
            'alternative_reason': 'Returns the decision to the help-seeker.',
        })
        with patch.object(tasks, 'task_call', return_value=result) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(row.overall_status_level, 2)
        self.assertEqual(row.record['alternative_reply'], 'What options feel right to you?')
        body = self.client.get(self.chat_url).content.decode()
        self.assertIn('peertrain-boundary-tip-label', body)
        self.assertIn(result.feedback_text, body)
        self.assertIn('What options feel right to you?', body)

    def test_earlier_concern_followed_by_repair_updates_accumulated_status(self):
        earlier = self.message('I will make that decision for you.')
        TurnAssessment.objects.create(trainee_message=earlier, overall_status_level=2,
            feedback_type='reflective_prompt', feedback_text='Leave the choice with them.',
            record={'observed_evidence': 'I will make that decision for you',
                    'boundary_relevant': True, 'opportunity_observed': True})
        message = self.message('Sorry, that is your decision. What would you prefer?')
        result = self.result().model_copy(update={
            'observed_evidence': 'that is your decision',
            'assessment_note': 'Earlier takeover was repaired by returning the choice.',
            'overall_status_level': 3,
            'feedback_text': 'You returned the choice to them and invited their preference.',
            'alternative_reply': None,
        })
        with patch.object(tasks, 'task_call', return_value=result) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(json.loads(call.call_args.args[1]['assessment_state'])[0]['overall_status_level'], 2)
        self.assertEqual(row.overall_status_level, 3)
        self.assertIsNone(row.record['alternative_reply'])

    def test_inconsistent_low_status_retries_once_with_same_context(self):
        message = self.message('Hello, it is good to meet you.')
        base = self.result().model_copy(update={
            'relevant_dimensions': [], 'observed_evidence': None,
            'opportunity_observed': False, 'boundary_relevant': False,
            'feedback_text': 'You offered a warm welcome.',
        })
        inconsistent = base.model_copy(update={'overall_status_level': 1,
            'assessment_note': 'Warm greeting.'})
        corrected = base.model_copy(update={'overall_status_level': 0,
            'assessment_note': 'Warm greeting; no boundary opportunity.'})
        with patch.object(tasks, 'task_call', side_effect=[inconsistent, corrected]) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(row.overall_status_level, 0)
        self.assertEqual(call.call_count, 2)
        first, second = (item.args[1] for item in call.call_args_list)
        self.assertEqual(first['transcript'], second['transcript'])
        self.assertEqual(first['assessment_state'], second['assessment_state'])
        self.assertIn('require a genuine boundary opportunity', second['validation_feedback'])

    def test_observed_boundary_choice_with_level_zero_retries_once(self):
        message = self.message('It is your choice. What feels right to you?')
        base = self.result().model_copy(update={
            'observed_evidence': 'It is your choice',
            'assessment_note': 'Supports the help-seeker\'s agency.',
            'feedback_text': 'You leave the choice with them, supporting their agency.',
            'alternative_reply': None,
        })
        inconsistent = base.model_copy(update={'overall_status_level': 0})
        with patch.object(tasks, 'task_call', side_effect=[inconsistent, base]) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(row.overall_status_level, 3)
        self.assertEqual(call.call_count, 2)
        self.assertIn('provisional level 1-5', call.call_args.args[1]['validation_feedback'])

    def test_persistent_inconsistency_fails_without_saving(self):
        message = self.message('Hello, it is good to meet you.')
        inconsistent = self.result().model_copy(update={
            'overall_status_level': 1, 'observed_evidence': None,
            'opportunity_observed': False, 'boundary_relevant': False,
        })
        with patch.object(tasks, 'task_call', return_value=inconsistent) as call:
            with self.assertRaises(tasks.PracticeFailure):
                tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 2)
        self.assertFalse(TurnAssessment.objects.exists())

    def test_alternative_without_boundary_opportunity_is_retried(self):
        message = self.message('Hello, it is good to meet you.')
        base = self.result().model_copy(update={
            'opportunity_observed': False, 'boundary_relevant': False,
            'overall_status_level': 0, 'feedback_text': 'You offered a warm welcome.',
        })
        inconsistent = base.model_copy(update={
            'alternative_reply': 'What brings you here?',
            'alternative_reason': 'Another way to invite sharing.',
        })
        with patch.object(tasks, 'task_call', side_effect=[inconsistent, base]) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(call.call_count, 2)
        self.assertIn('alternative_reply requires', call.call_args.args[1]['validation_feedback'])
        self.assertIsNone(row.record['alternative_reply'])
        self.assertEqual(row.record['alternative_reason'], '')

    def test_specific_paraphrase_supports_low_status_without_retry(self):
        self.scenario.boundary_type = 'SAFETY'
        self.scenario.save(update_fields=['boundary_type'])
        self.message('I think everyone would be better off if I was less in the way.', 'AI')
        message = self.message("don't think like that. you'd be fine")
        result = self.result().model_copy(update={
            'relevant_dimensions': ['safety'],
            'observed_evidence': 'Tells the help-seeker not to think that and reassures them they will be fine.',
            'assessment_note': 'The trainee dismisses a possible safety concern with reassurance instead of exploring what the help-seeker means.',
            'overall_status_level': 2,
            'feedback_type': 'reflective_prompt',
            'feedback_text': 'You want to reassure them; exploring what they mean would help you respond to their distress.',
            'alternative_reply': 'That sounds painful. Can you tell me more about what you mean?',
            'alternative_reason': 'Stays with the disclosure and invites clarification.',
        })
        with patch.object(tasks, 'task_call', return_value=result) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(row.overall_status_level, 2)
        self.assertEqual(call.call_count, 1)

    def test_low_status_without_evidence_gets_one_correction(self):
        message = self.message('I will make that decision for you.')
        base = self.result().model_copy(update={
            'overall_status_level': 2,
            'observed_evidence': 'Takes over the decision from the help-seeker.',
            'assessment_note': 'The trainee takes over the help-seeker\'s choice.',
        })
        inconsistent = base.model_copy(update={'observed_evidence': None})
        with patch.object(tasks, 'task_call', side_effect=[inconsistent, base]) as call:
            row = tasks.assess_turn(self.session, message)
        self.assertEqual(row.overall_status_level, 2)
        self.assertEqual(call.call_count, 2)
        self.assertIn('require observed_evidence', call.call_args.args[1]['validation_feedback'])

    def test_assessment_context_uses_only_public_service_mode_and_dialogue(self):
        first = self.message('Hello, I have been feeling lonely.', 'AI')
        reply = self.message('Would you like to tell me more?')
        disclosed = self.message('I worry about being left alone after appointments.', 'AI')
        message = self.message()
        expected_transcript = tasks.transcript(self.session, message)
        for boundary, focus in (
            ('AGENCY', 'Support Without Fixing'),
            ('RELATIONSHIP', 'Care Without Overextending'),
            ('SAFETY', 'Staying with People in Distress'),
        ):
            for persona in ('pso', 'student'):
                with self.subTest(boundary=boundary, persona=persona):
                    TurnAssessment.objects.all().delete()
                    self.scenario.boundary_type = boundary
                    self.scenario.save()
                    self.session.persona = persona
                    self.session.save()
                    character = prompts.session_persona_for(boundary, persona)
                    private = {key: f'PRIVATE {key}: introduce a future boundary challenge.'
                               for key in character}
                    with patch.dict(character, private), patch.dict(
                        prompts.RELATIONSHIP_AVAILABILITY_GUIDANCE,
                        {persona: 'PRIVATE future availability challenge'},
                    ), patch.object(tasks, 'task_call', return_value=self.result()) as call:
                        tasks.assess_turn(self.session, message)
                    context = call.call_args.args[1]
                    self.assertEqual(context['scenario_training_focus'], focus)
                    self.assertEqual(json.loads(context['transcript']), expected_transcript)
                    self.assertEqual([row['message_id'] for row in expected_transcript],
                                     [first.pk, reply.pk, disclosed.pk, message.pk])
                    self.assertEqual(context['case_context'],
                        'Appointment-based support mode. Help-seeker: University peer support service user.'
                        if persona == 'student' else 'Help-seeker: Peer support community member.')
                    self.assertNotIn('PRIVATE', json.dumps(context))

    def test_assessment_state_retains_evidence_and_status_without_private_plans(self):
        previous = self.message()
        record = self.result().model_dump()
        record.update(roleplay_cue='PRIVATE cue: ask for personal contact next.',
                      assessment_note='PRIVATE plan: escalate the next challenge.',
                      future_development='PRIVATE future development',
                      alternative_reply='PRIVATE suggested future reply',
                      alternative_reason='PRIVATE future advice')
        saved = TurnAssessment.objects.create(trainee_message=previous, overall_status_level=3,
            feedback_type='affirmation', feedback_text='You invited their priorities.',
            roleplay_cue='PRIVATE column cue', record=record)
        message = self.message('What would feel helpful now?')
        later = self.message('A future trainee turn')
        TurnAssessment.objects.create(trainee_message=later, overall_status_level=5,
            feedback_type='none', record={'observed_evidence': 'FUTURE evidence'})
        with patch.object(tasks, 'task_call', return_value=self.result()) as call:
            result = tasks.assess_turn(self.session, message)
        context = call.call_args.args[1]
        state = json.loads(context['assessment_state'])
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]['trainee_message_id'], previous.pk)
        self.assertEqual(state[0]['overall_status_level'], 3)
        self.assertTrue(state[0]['succeeded'])
        self.assertEqual(state[0]['record'], {key: record[key] for key in (
            'relevant_dimensions', 'observed_evidence', 'opportunity_observed',
            'confidence', 'review_flag', 'safety_flag', 'boundary_relevant')})
        self.assertNotIn('PRIVATE', json.dumps(context))
        self.assertNotIn('FUTURE', json.dumps(context))
        self.assertNotIn('roleplay_cue', context['assessment_state'])
        saved.refresh_from_db()
        self.assertEqual(saved.record, record)
        self.assertEqual(saved.roleplay_cue, 'PRIVATE column cue')
        self.assertEqual(result.record['roleplay_cue'], self.result().roleplay_cue)

    def test_assessment_stops_at_trainee_message(self):
        message = self.message()
        self.message('A later help-seeker reply', 'AI')
        with patch.object(tasks, 'task_call', return_value=self.result()) as call:
            tasks.assess_turn(self.session, message)
        self.assertNotIn('A later help-seeker reply', call.call_args.args[1]['transcript'])

    def test_status_rejects_invalid_levels_and_empty_feedback(self):
        data = self.result().model_dump()
        for level in (-1, 6, 2.5, True):
            with self.assertRaises(ValueError):
                tasks.AssessmentResult.model_validate({**data, 'overall_status_level': level})
        with self.assertRaises(ValueError):
            tasks.AssessmentResult.model_validate({**data, 'feedback_text': ''})
        with self.assertRaises(ValueError):
            tasks.AssessmentResult.model_validate({**data, 'feedback_type': 'none', 'feedback_text': None})

    def test_safety_allows_realistic_distress(self):
        with patch.object(tasks, 'task_call', return_value=self.verdict()) as call:
            text, accepted = tasks.checked_reply(self.session, 'Sometimes I wish I could disappear.')
        self.assertTrue(accepted)
        self.assertEqual(text, 'Sometimes I wish I could disappear.')
        self.assertIn('Selected persona', TASK_PROMPTS['ROLEPLAY_SAFETY'][1])
        self.assertIn(persona_prompt_for('AGENCY', 'pso'), call.call_args.args[1]['persona_constraints'])

    def test_safety_rechecks_rewrite(self):
        with patch.object(tasks, 'task_call', side_effect=[self.verdict('REWRITE', 'I feel overwhelmed.'), self.verdict()]) as call:
            text, accepted = tasks.checked_reply(self.session, 'Rejected candidate')
        self.assertTrue(accepted)
        self.assertEqual(text, 'I feel overwhelmed.')
        self.assertEqual(call.call_args_list[1].args[1]['candidate_reply'], text)
        self.assertEqual(list(self.session.safety_checks.values_list('verdict', flat=True)), ['REWRITE', 'ALLOW'])
        self.assertEqual(TurnAssessment.objects.count(), 0)

    def test_safety_blocks_invalid_errors_and_repeated_rewrites(self):
        cases = [[self.verdict('BLOCK')], [ValueError('bad JSON')],
                 [self.verdict('REWRITE', '')], [self.verdict('REWRITE', 'Replacement'), self.verdict('BLOCK')],
                 [self.verdict('REWRITE', 'Replacement'), self.verdict('REWRITE', 'Another')]]
        for outputs in cases:
            with self.subTest(outputs=outputs), patch.object(tasks, 'task_call', side_effect=outputs):
                with self.assertRaises(tasks.PracticeFailure):
                    tasks.checked_reply(self.session, 'Unchecked')

    def test_opening_and_reply_are_checked_before_saving(self):
        with override_settings(OPENAI_API_KEY='test-placeholder'), patch('training.services.ai._openai_role_play_opening', return_value='Unchecked opening'), patch.object(tasks, 'task_call', return_value=self.verdict('BLOCK')):
            with self.assertRaises(tasks.PracticeFailure):
                generate_ai_opening(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'PAUSED')
        self.assertFalse(self.session.messages.exists())

    def test_rewrite_is_saved_as_only_accepted_reply(self):
        with override_settings(OPENAI_API_KEY='test-placeholder'), patch('training.services.ai._openai_role_play_reply', return_value='I feel uncertain about where to begin.'), patch.object(tasks, 'task_call', side_effect=[self.verdict('REWRITE', 'I am uncertain.'), self.verdict()]):
            reply = generate_ai_reply(self.session, 'Hello')
        self.assertEqual(reply.content, 'I am uncertain.')
        self.assertEqual(self.session.messages.count(), 1)

    def test_entry_preserves_persona_controls_without_feedback_modes(self):
        for boundary in ('AGENCY', 'RELATIONSHIP', 'SAFETY'):
            with self.subTest(boundary=boundary):
                self.scenario.boundary_type = boundary
                self.scenario.save()
                self.client.post(reverse('scenarios'), {'scenario_id': self.scenario.pk})
                session = TrainingSession.objects.order_by('-pk').first()
                url = reverse('chat', args=[session.pk])
                html = self.client.get(url)
                self.assertContains(html, 'Live Feedback')
                self.assertContains(html, 'id="student-mode"')
                self.assertNotContains(html, 'id="live-feedback"')
                self.assertNotContains(html, 'This scenario combines live feedback')
                selected = self.client.post(url, {'action': 'select_persona', 'persona': 'pso'}, **self.headers)
                self.assertEqual(selected.status_code, 200)
                self.assertNotIn('feedback_mode', selected.json())
                session.refresh_from_db()
                self.assertEqual(session.persona, 'pso')
                reopened = self.client.get(url)
                self.assertContains(reopened, 'data-boundary-coach-toggle')
                self.assertNotContains(reopened, 'data-feedback-mode')
                self.client.post(url, {'action': 'select_persona', 'persona': 'student'}, **self.headers)
                session.refresh_from_db()
                self.assertEqual(session.persona, 'pso')

    def test_review_saved_feedback_and_full_original_transcript(self):
        first = self.message('I feel overwhelmed.', 'AI')
        message = self.message()
        last = self.message('I am unsure where to start.', 'AI')
        TurnAssessment.objects.create(trainee_message=message, overall_status_level=3,
            feedback_type='affirmation', feedback_text='You invited their priorities.', roleplay_cue='PRIVATE CUE')
        feedback = self.complete()
        FeedbackReviewItem.objects.create(feedback=feedback, message=message, turn_index=1,
            message_excerpt=message.content, issue_label='Feedback', why_it_matters='Keeps choice open.',
            better_reply='Would it help to explore your options?')
        for url in (self.url, reverse('history_detail', args=[self.session.pk])):
            with patch.object(tasks, 'task_call') as call:
                response = self.client.get(url, follow=True)
            call.assert_not_called()
            body = response.content.decode()
            overview, conversation = body.split('<h3 id="conversation-heading">Conversation review</h3>')
            for original in (first, message, last):
                self.assertIn(original.content, conversation)
            self.assertIn('You invited their priorities.', conversation)
            self.assertIn('Keeps choice open.', conversation)
            self.assertLess(conversation.index(message.content), conversation.index('An alternative reply to consider'))
            self.assertLess(conversation.index('An alternative reply to consider'), conversation.index(last.content))
            self.assertNotIn('An alternative reply to consider', overview)
            self.assertNotIn('PRIVATE CUE', body)
            self.assertContains(response, '/static/images/bot-icon.png')
            self.assertContains(response, 'reflection-avatar')
            self.assertContains(response, 'role="tablist"')
            self.assertContains(response, 'id="reflect-ai-tab" role="tab" aria-selected="true"')
            self.assertContains(response, 'id="notes-panel" role="tabpanel" aria-labelledby="notes-tab" hidden')

    def test_review_uses_saved_turn_alternative_without_backfill_or_duplicates(self):
        message = self.message()
        TurnAssessment.objects.create(trainee_message=message, feedback_type='none', record={
            'alternative_reply': 'What would feel helpful?', 'alternative_reason': 'Keeps choice open.'})
        feedback = self.complete()
        with patch.object(tasks, 'task_call') as call:
            response = self.client.get(self.url)
        call.assert_not_called()
        self.assertContains(response, 'What would feel helpful?', count=1)
        self.assertContains(response, 'Keeps choice open.')
        self.assertEqual(feedback.review_items.count(), 0)
        FeedbackReviewItem.objects.create(feedback=feedback, message=message, turn_index=1,
            better_reply='What would feel helpful?')
        self.assertContains(self.client.get(self.url), 'What would feel helpful?', count=1)

    def test_review_turn_numbers_follow_ordered_messages_for_historical_sessions(self):
        opening = self.message('Opening disclosure', 'AI')
        # Create in a different order from the historical transcript. IDs must not
        # determine turn numbers, even when older sessions have sparse feedback.
        second = self.message('Second trainee reply')
        first = self.message('First trainee reply')
        response = self.message('Response to first reply', 'AI')
        from datetime import timedelta
        from django.utils import timezone
        start = timezone.now() - timedelta(days=30)
        for index, message in enumerate((opening, first, response, second)):
            Message.objects.filter(pk=message.pk).update(created_at=start + timedelta(seconds=index))
        assessment = TurnAssessment.objects.create(trainee_message=second, feedback_type='affirmation',
            feedback_text='Second reply feedback', record={'turn_id': second.pk,
                'observed_evidence': second.content, 'alternative_reply': 'Saved second alternative',
                'alternative_reason': 'Second reason'})
        feedback = self.complete()
        item = FeedbackReviewItem.objects.create(feedback=feedback, message=first, turn_index=99,
            better_reply='Saved first alternative')
        page = self.client.get(self.url)
        self.assertContains(page, '>Opening</p>', count=1)
        self.assertContains(page, '>Turn 1</p>', count=1)
        self.assertContains(page, '>Turn 2</p>', count=1)
        self.assertEqual([turn['turn_label'] for turn in page.context['alternative_turns']], ['Turn 1', 'Turn 2'])
        self.assertEqual(page.context['alternative_count'], 2)
        self.assertContains(page, 'data-alternative-turn="1"', count=1)
        self.assertContains(page, 'data-alternative-turn="2"', count=1)
        timeline = page.context['timeline']
        self.assertEqual([t['message'].pk for t in timeline], [opening.pk, first.pk, response.pk, second.pk])
        self.assertEqual([t['turn_number'] for t in timeline], [None, 1, 1, 2])
        self.assertIsNone(timeline[1]['assessment'])
        self.assertEqual(timeline[3]['assessment'], assessment)
        self.assertEqual(timeline[1]['examples'], [item])
        self.assertEqual(timeline[3]['examples'][0]['better_reply'], 'Saved second alternative')
        context = tasks.reflection_context(self.session, feedback, 'Why Turn 2?')
        transcript = json.loads(context['session_transcript'])
        self.assertEqual([t['turn'] for t in transcript], ['Opening', 'Turn 1', 'Turn 1', 'Turn 2'])
        self.assertEqual(json.loads(context['assessment_records']), [{
            'turn': 'Turn 2', 'feedback': 'Second reply feedback', 'observed_evidence': second.content}])
        examples = json.loads(context['visible_review_examples'])
        self.assertEqual([(e['turn'], e['better_reply']) for e in examples], [
            ('Turn 1', 'Saved first alternative'), ('Turn 2', 'Saved second alternative')])
        for field in ('session_transcript', 'assessment_records', 'visible_review_examples'):
            self.assertNotIn('message_id', context[field])
            self.assertNotIn('turn_id', context[field])
        assessment.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(assessment.record['turn_id'], second.pk)
        self.assertEqual(item.turn_index, 99)
        self.assertEqual(feedback.review_items.count(), 1)

    def test_reflection_turn_reference_uses_same_exchange_and_deduplicates_examples(self):
        first = self.message('First reply')  # Historical transcript without an opening.
        answer = self.message('First answer', 'AI')
        second = self.message('Second reply')  # A final reply need not have an AI response.
        TurnAssessment.objects.create(trainee_message=first, record={
            'alternative_reply': 'Saved alternative', 'alternative_reason': 'Saved reason'})
        feedback = self.complete()
        FeedbackReviewItem.objects.create(feedback=feedback, message=first, turn_index=7,
            better_reply='Saved alternative', why_it_matters='Saved reason')
        self.client.get(self.url)
        with patch.object(tasks, 'task_call', return_value=tasks.ReflectionResult(
                reply='In Turn 1, you said ?First reply? and the help-seeker said ?First answer?.')) as call:
            page = self.client.post(self.url, {'action': 'reflect', 'content': 'What happened in Turn 1?',
                'request_id': str(uuid.uuid4())}, **self.headers)
        self.assertEqual(page.status_code, 200)
        task, context, schema = call.call_args.args
        self.assertEqual(task, 'REFLECTION_CHAT')
        self.assertEqual(context['latest_reflection_message'], 'What happened in Turn 1?')
        transcript = json.loads(context['session_transcript'])
        self.assertEqual([(t['turn'], t['text']) for t in transcript], [
            ('Turn 1', first.content), ('Turn 1', answer.content), ('Turn 2', second.content)])
        self.assertEqual(len(json.loads(context['visible_review_examples'])), 1)
        self.assertIn('In Turn 1', page.json()['messages'][-1]['content'])
        system, template = tasks.task_prompt_parts(task)
        self.assertIn("say 'Turn N'", system)
        self.assertIn('Never expose internal database message IDs', system)
        self.assertIn('Match assessments and suggested replies by their supplied turn label', template)

    def test_review_none_hides_inline_item_and_optional_alternative(self):
        message = self.message()
        TurnAssessment.objects.create(trainee_message=message, feedback_type='none')
        self.complete()
        response = self.client.get(self.url)
        self.assertNotContains(response, 'class="review-turn-feedback"')
        self.assertNotContains(response, 'An alternative reply to consider')
        self.assertNotContains(response, 'Alternative replies to consider')
        self.assertNotContains(response, 'data-alternative-turn=')

    def test_alternative_navigation_counts_saved_cards_and_groups_by_existing_turn(self):
        self.message('Opening', 'AI')
        first = self.message('First reply')
        self.message('First response', 'AI')
        self.message('Reply with no alternative')
        third = self.message('Historical reply')
        TurnAssessment.objects.create(trainee_message=third, record={
            'alternative_reply': 'Historical alternative', 'alternative_reason': 'Historical reason'})
        feedback = self.complete()
        for reply in ('First alternative', 'Another first alternative', ''):
            FeedbackReviewItem.objects.create(feedback=feedback, message=first, turn_index=88, better_reply=reply)
        with patch.object(tasks, 'task_call') as call:
            response = self.client.get(self.url)
        call.assert_not_called()
        self.assertEqual(response.context['alternative_count'], 3)
        self.assertContains(response, 'Alternative replies to consider')
        self.assertContains(response, 'data-alternative-turn="1"', count=1)
        self.assertContains(response, 'data-alternative-turn="3"', count=1)
        self.assertNotContains(response, 'data-alternative-turn="2"')
        self.assertContains(response, '<strong>An alternative reply to consider</strong>', count=3)
        self.assertContains(response, 'data-alternative-card-turn="1"', count=2)
        self.assertContains(response, 'data-alternative-card-turn="3"', count=1)
        html = response.content.decode()
        self.assertLess(html.index('</nav>', html.index('class="review-alternative-nav"')),
                        html.index('id="review-transcript"'))
        self.assertEqual(feedback.review_items.count(), 3)

    def test_empty_saved_alternative_does_not_show_navigation(self):
        message = self.message()
        feedback = self.complete()
        FeedbackReviewItem.objects.create(feedback=feedback, message=message, turn_index=42, better_reply='')
        response = self.client.get(self.url)
        self.assertNotContains(response, 'Alternative replies to consider')
        self.assertNotContains(response, 'data-alternative-turn=')

    def test_synthesis_once_from_saved_records_does_not_rescore(self):
        message = self.message()
        assessed = self.result().model_copy(update={'alternative_reply': 'What would feel helpful?', 'alternative_reason': 'Preserves choice.'})
        with patch.object(tasks, 'task_call', return_value=assessed):
            tasks.assess_turn(self.session, message)
        result = tasks.ReviewResult(strengths='Agency supported.', considerations='Explore options.', examples=[tasks.ReviewExample(
            message_id=message.pk, quotation=message.content, consideration='Preserves choice.',
            alternative_reply='A newly invented alternative must not replace the saved one.', dimension='agency')])
        with patch.object(tasks, 'task_call', return_value=result) as call:
            feedback = generate_feedback(self.session)
            same = generate_feedback(self.session)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(feedback.pk, same.pk)
        self.assertEqual(feedback.overall_score, 3)
        self.assertEqual(call.call_args.args[0], 'SESSION_REVIEW')
        self.assertEqual(call.call_args.args[1]['assessments'][0]['trainee_message_id'], message.pk)
        self.assertEqual(feedback.review_items.get().message_id, message.pk)
        self.assertEqual(feedback.review_items.get().better_reply, 'What would feel helpful?')

    def test_saved_review_is_immutable_from_history(self):
        feedback = self.complete()
        self.client.get(self.url)
        with patch.object(tasks, 'task_call') as call:
            response = self.client.get(reverse('history_detail', args=[self.session.pk]), follow=True)
        call.assert_not_called()
        self.assertEqual(response.redirect_chain, [(self.url, 302)])
        self.assertEqual(response.context['feedback'].pk, feedback.pk)
        self.assertEqual(self.session.reflection_messages.count(), 1)

    def test_invalid_review_quotations_and_foreign_ids_discarded(self):
        message = self.message()
        examples = [tasks.ReviewExample(message_id=message.pk, quotation='invented', dimension='agency'),
                    tasks.ReviewExample(message_id=message.pk + 100, quotation='', dimension='agency')]
        with patch.object(tasks, 'task_call', return_value=tasks.ReviewResult(strengths='', considerations='', examples=examples)):
            feedback = tasks.saved_review(self.session)
        self.assertEqual(feedback.review_items.count(), 0)

    def test_reflection_context_persistence_notes_separate_and_idempotent(self):
        self.message()
        feedback = self.complete()
        self.client.get(self.url)
        request_id = str(uuid.uuid4())
        with patch.object(tasks, 'task_call', return_value=tasks.ReflectionResult(reply='What were you hoping to achieve?')) as call:
            response = self.client.post(self.url, {'action': 'reflect', 'content': 'Can we discuss my reply?', 'request_id': request_id}, **self.headers)
            self.assertEqual(response.status_code, 200)
            context = call.call_args.args[1]
            self.assertIn('What matters most to you?', context['session_transcript'])
            self.assertIn(REFLECTION_OPENING, context['reflection_chat_history'])
            self.assertIn(feedback.strengths, context['visible_overall_feedback'])
            self.client.post(self.url, {'action': 'reflect', 'content': 'Can we discuss my reply?', 'request_id': request_id}, **self.headers)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.session.reflection_messages.count(), 3)
        response = self.client.post(self.url, {'action': 'save_notes', 'reflection_text': 'Private notebook thoughts'}, **self.headers)
        self.assertEqual(response.json()['confirmation'], 'Saved')
        self.assertEqual(Reflection.objects.get(session=self.session).reflection_text, 'Private notebook thoughts')
        self.assertEqual(self.session.reflection_messages.count(), 3)
        with patch.object(tasks, 'task_call', return_value=tasks.ReflectionResult(reply='There are several possible approaches.')) as call:
            self.client.post(self.url, {'action': 'reflect', 'content': 'What else could I try?', 'request_id': str(uuid.uuid4())}, **self.headers)
        context = call.call_args.args[1]
        self.assertIn('What were you hoping to achieve?', context['reflection_chat_history'])
        self.assertNotIn('Private notebook thoughts', json.dumps(context))
        self.assertContains(self.client.get(self.url), 'Private notebook thoughts')
        feedback.refresh_from_db()
        self.assertEqual(feedback.strengths, 'Agency was supported.')

    def test_reflection_failures_preserve_user_message_and_require_researcher(self):
        self.complete()
        self.client.get(self.url)
        data = {'action': 'reflect', 'content': 'Help me reflect', 'request_id': str(uuid.uuid4())}
        with patch.object(tasks, 'task_call', side_effect=ValueError('provider failure')):
            self.assertEqual(self.client.post(self.url, data, **self.headers).status_code, 503)
        self.assertEqual(self.session.reflection_messages.filter(sender_type='USER').count(), 1)
        with patch.object(tasks, 'task_call') as call:
            self.assertEqual(self.client.post(self.url, data, **self.headers).status_code, 503)
            call.assert_not_called()
        self.assertEqual(self.session.reflection_messages.count(), 2)

    def test_reflection_excludes_private_cues_and_case_background(self):
        message = self.message()
        TurnAssessment.objects.create(trainee_message=message, feedback_type='none', roleplay_cue='SECRET CUE',
            record={'assessment_note': 'UNDISCLOSED PRIVATE CASE', 'roleplay_cue': 'SECRET CUE'})
        feedback = self.complete()
        context = json.dumps(tasks.reflection_context(self.session, feedback, 'Question'))
        self.assertNotIn('SECRET CUE', context)
        self.assertNotIn('UNDISCLOSED PRIVATE CASE', context)

    def test_ownership_and_completed_session_required(self):
        self.complete()
        self.client.force_login(self.other)
        for url in (self.url, self.chat_url, reverse('history_detail', args=[self.session.pk])):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {'action': 'save_notes', 'reflection_text': 'overwrite'}).status_code, 404)
        self.assertFalse(Reflection.objects.exists())

    def test_private_roleplay_cue_only_in_continuation(self):
        message = self.message()
        TurnAssessment.objects.create(trainee_message=message, feedback_type='none', roleplay_cue='PRIVATE EXPERIENCE CUE')
        prompt = get_prompt_for_type('ROLE_PLAY', self.scenario)
        self.assertIn('PRIVATE EXPERIENCE CUE', _roleplay_system_prompt(self.session, prompt, 'Reply'))
        self.assertNotIn('PRIVATE EXPERIENCE CUE', _roleplay_system_prompt(self.session, prompt, '', opening=True))
        self.assertNotContains(self.client.get(self.chat_url), 'PRIVATE EXPERIENCE CUE')

    def test_boundary_highlights_escape_model_html(self):
        result = boundary_highlights('<script>alert(1)</script> Agency and Safety Boundary')
        self.assertNotIn('<script>', result)
        self.assertIn('boundary-reference-agency', result)
        self.assertIn('boundary-reference-safety', result)

    def test_retrying_chat_request_does_not_duplicate_messages_or_assessment(self):
        self.message('I am unsure.', 'AI')
        payload = {'action': 'send', 'content': 'What matters most to you?', 'request_id': str(uuid.uuid4())}
        def reply(session, content):
            return Message.objects.create(session=session, sender_type='AI', content='Thank you for asking.')
        with patch.object(tasks, 'task_call', return_value=self.result()) as assessor, patch('training.views.generate_ai_reply', side_effect=reply) as roleplay:
            first = self.client.post(self.chat_url, payload, **self.headers)
            second = self.client.post(self.chat_url, payload, **self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json()['user_message_html'], first.json()['user_message_html'])
        self.assertEqual(roleplay.call_count, 1)
        self.assertEqual(assessor.call_count, 1)
        self.assertEqual(self.session.messages.count(), 3)
        self.assertEqual(TurnAssessment.objects.count(), 1)

    def test_operational_prompts_match_specification(self):
        spec = (Path(__file__).resolve().parent.parent / 'System_Prompts_Texts_Specification.md').read_text(encoding='utf-8')
        # Safety and reflection context retain the reference verbatim. Assessment,
        # synthesis and reflection style are superseded by the user's UI refinement.
        for name in ('REFLECTION_AI_CONTEXT', 'ROLEPLAY_SAFETY_CHECK_SYSTEM', 'ROLEPLAY_SAFETY_CHECK_INPUT'):
            source = re.search(r'### [^\n]*`' + name + r'`.*?```text\n(.*?)\n```', spec, re.S).group(1)
            self.assertEqual(getattr(prompts, name), source)

    def test_non_boundary_feedback_cannot_include_an_alternative(self):
        data = self.result().model_dump()
        with self.assertRaises(ValueError):
            tasks.AssessmentResult.model_validate({**data, 'boundary_relevant': False,
                'alternative_reply': 'An unnecessary rewrite', 'alternative_reason': 'Not applicable'})
        with self.assertRaises(ValueError):
            tasks.AssessmentResult.model_validate({**data, 'alternative_reply': None,
                'alternative_reason': 'Not applicable'})
        self.assertIn('ordinary greetings', prompts.ASSESSMENT_SYSTEM)
        self.assertIn('feedback_text addressed', prompts.ASSESSMENT_SYSTEM)

    def test_live_feedback_history_and_saved_alternative(self):
        first = self.message('Hello, welcome back.')
        TurnAssessment.objects.create(trainee_message=first, feedback_type='affirmation',
            feedback_text='You offered a friendly welcome.', record={'boundary_relevant': False})
        message = self.message()
        data = self.result().model_copy(update={'alternative_reply': 'What would feel helpful?', 'alternative_reason': 'Keeps choice open.'})
        with patch.object(tasks, 'task_call', return_value=data):
            tasks.assess_turn(self.session, message)
        response = self.client.get(self.chat_url)
        body = response.content.decode()
        self.assertIn('You offered a friendly welcome.', body)
        self.assertIn('What would feel helpful?', body)
        self.assertIn('Boundary tips', body)
        self.assertIn(f'data-turn-feedback="{first.pk}" >', body)
        self.assertIn(f'data-turn-feedback="{message.pk}" open', body)
        self.assertIn('<q><em>What would feel helpful?</em></q>', body)

    def test_reply_feedback_heading_waits_for_visible_feedback(self):
        self.assertContains(self.client.get(self.chat_url), 'Overall status')
        self.assertNotContains(self.client.get(self.chat_url), 'peertrain-reply-feedback-title')
        message = self.message()
        assessment = TurnAssessment.objects.create(trainee_message=message, feedback_type='none', feedback_text='')
        self.assertNotContains(self.client.get(self.chat_url), 'peertrain-reply-feedback-title')
        assessment.feedback_type = 'affirmation'
        assessment.feedback_text = 'You invited their priorities.'
        assessment.save()
        response = self.client.get(self.chat_url)
        self.assertContains(response, 'peertrain-reply-feedback-title')
        self.assertContains(response, f'data-turn-feedback="{message.pk}"')

    def test_overview_is_saved_separately_and_history_hides_scores(self):
        self.session.status = 'COMPLETED'
        self.session.end_time = self.session.start_time
        self.session.save()
        result = tasks.ReviewResult(overview='You offered a warm welcome and room to talk.',
            strengths='You listened patiently.', considerations='You could also consider offering choices.')
        with patch.object(tasks, 'task_call', return_value=result):
            response = self.client.get(self.url)
        self.assertContains(response, result.overview)
        self.assertContains(response, 'What worked')
        self.assertContains(response, 'Areas for consideration')
        self.assertEqual(Feedback.objects.get(session=self.session).overview, result.overview)
        self.session.overall_score = 4
        self.session.save()
        self.assertNotContains(self.client.get(reverse('history')), 'peertrain-session-score')
        with patch.object(tasks, 'task_call') as call:
            reopened = self.client.get(reverse('history_detail', args=[self.session.pk]), follow=True)
        call.assert_not_called()
        self.assertContains(reopened, result.overview)

    def test_relationship_prompts_make_existing_availability_need_concrete(self):
        for persona in ('pso', 'student'):
            text = persona_prompt_for('RELATIONSHIP', persona)
            self.assertIn(prompts.RELATIONSHIP_AVAILABILITY_GUIDANCE[persona], text)
            self.assertIn('first few exchanges', text)
            self.assertIn('Wait until the trainee has replied at least once', text)
            self.assertIn('opening message', text)
        self.assertNotIn('Make Bailey', persona_prompt_for('AGENCY', 'pso'))
        self.assertIn('40-80 words', prompts.REFLECTION_AI_SYSTEM)
        self.assertIn('only that sentence or question', prompts.ASSESSMENT_SYSTEM)
        self.assertIn('Do not flag ordinary listening', prompts.ASSESSMENT_TURN)
        self.assertIn('normally one short sentence', prompts.ASSESSMENT_SYSTEM)
        self.assertIn('Explain the evidence and status in assessment_note', prompts.ASSESSMENT_TURN)

    def test_openai_task_adapter_uses_structured_schema_and_explicit_context(self):
        feedback = self.complete()
        context = tasks.reflection_context(self.session, feedback, 'Can we discuss my reply?')
        captured = {}
        def parse(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=tasks.ReflectionResult(reply='Of course.'))
        client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
        with override_settings(OPENAI_API_KEY='test-placeholder', OPENAI_MODEL='test-model'), patch('training.services.ai._openai_client', return_value=client):
            result = tasks.task_call('REFLECTION_CHAT', context, tasks.ReflectionResult)
        self.assertEqual(result.reply, 'Of course.')
        self.assertEqual(captured['model'], 'test-model')
        self.assertIs(captured['text_format'], tasks.ReflectionResult)
        self.assertIn(prompts.REFLECTION_AI_SYSTEM, captured['input'][0]['content'])
        self.assertIn(prompts.REFLECTION_USER_SAFETY, captured['input'][0]['content'])
        self.assertIn('Can we discuss my reply?', captured['input'][1]['content'])
        self.assertNotIn('feedback_mode', captured['input'][1]['content'])

    def test_google_task_adapter_and_invalid_json(self):
        captured = {}
        def generate_content(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text=self.verdict().model_dump_json())
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        context = dict(scenario_name='Safety', persona_constraints='Established case', conversation_history='[]', candidate_reply='I feel low.')
        with override_settings(LLM_PROVIDER='google', GEMINI_API_KEY='test-placeholder', GOOGLE_MODEL='test-model'), patch('training.services.ai._google_client', return_value=client):
            result = tasks.task_call('ROLEPLAY_SAFETY', context, tasks.SafetyResult)
        self.assertEqual(result.verdict, 'ALLOW')
        self.assertEqual(captured['model'], 'test-model')
        self.assertEqual(captured['config'].response_mime_type, 'application/json')
        self.assertIs(captured['config'].response_schema, tasks.SafetyResult)
        self.assertIn('I feel low.', captured['contents'])
        bad_client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text='invalid JSON')))
        with override_settings(LLM_PROVIDER='google', GEMINI_API_KEY='test-placeholder'), patch('training.services.ai._google_client', return_value=bad_client):
            with self.assertRaises(tasks.PracticeFailure):
                tasks.checked_reply(self.session, 'Unchecked')
