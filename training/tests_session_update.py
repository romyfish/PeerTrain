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

    def test_assessment_and_none_saved_without_visibility_context(self):
        message = self.message()
        for kind in ('affirmation', 'none'):
            with self.subTest(kind=kind):
                TurnAssessment.objects.all().delete()
                with patch.object(tasks, 'task_call', return_value=self.result(kind)) as call:
                    row = tasks.assess_turn(self.session, message)
                    self.assertEqual(row.overall_status_level, 3)
                    self.assertEqual(row.feedback_type, kind)
                    self.assertEqual(row.trainee_message, message)
                    if kind == 'none':
                        self.assertEqual(row.feedback_text, '')
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

    def test_review_none_hides_inline_item_and_optional_alternative(self):
        message = self.message()
        TurnAssessment.objects.create(trainee_message=message, feedback_type='none')
        self.complete()
        response = self.client.get(self.url)
        self.assertNotContains(response, 'class="review-turn-feedback"')
        self.assertNotContains(response, 'An alternative reply to consider')

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
        result = tasks.AssessmentResult.model_validate({**data, 'boundary_relevant': False,
            'alternative_reply': 'An unnecessary rewrite', 'alternative_reason': 'Not applicable'})
        self.assertIsNone(result.alternative_reply)
        self.assertEqual(result.alternative_reason, '')
        self.assertIn('brief but kind greeting', prompts.ASSESSMENT_TURN)
        self.assertIn('You could also consider asking', prompts.ASSESSMENT_SYSTEM)

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
        self.assertIn('only the suggested additional sentence or question', prompts.ASSESSMENT_SYSTEM)
        self.assertIn('do not restate or paraphrase', prompts.ASSESSMENT_TURN)
        self.assertIn('normally one short sentence', prompts.ASSESSMENT_SYSTEM)
        self.assertIn('Keep that reasoning only in assessment_note', prompts.ASSESSMENT_SYSTEM)
        self.assertIn('Never add commentary about the presence or absence of boundary work', prompts.ASSESSMENT_TURN)

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
