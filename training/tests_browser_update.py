"""Optional real-browser checks: run with `uv run --with playwright ... manage.py test ...`.

Uses installed Edge/Chrome; no browser download or production credentials needed.
"""
from importlib.util import find_spec
from pathlib import Path
import tempfile
import threading
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse

from training.models import Feedback, Message, Scenario, TrainingSession, TurnAssessment
from training.services.assessment import ReflectionResult

BROWSER = next((str(p) for p in (
    Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
    Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
) if p.exists()), None)


@skipUnless(find_spec('playwright') and BROWSER, 'Requires optional Playwright and installed Edge/Chrome')
@override_settings(OPENAI_API_KEY='', GEMINI_API_KEY='',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class BrowserUpdateTests(StaticLiveServerTestCase):
    def test_sidebar_modes_review_scroll_reflection_and_notes(self):
        from playwright.sync_api import sync_playwright, expect
        user = get_user_model().objects.create_user(username='browser', email='browser@example.com', password='test', role='TRAINEE')
        scenario = Scenario.objects.create(title='Agency', boundary_type='AGENCY', description='Practice', estimated_duration_min=10)
        entry = TrainingSession.objects.create(user=user, scenario=scenario)
        entry_url = self.live_server_url + reverse('chat', args=[entry.pk])
        waiting = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso', feedback_mode='concurrent')
        Message.objects.create(session=waiting, sender_type='AI', content='I have had a tiring week.')
        waiting_url = self.live_server_url + reverse('chat', args=[waiting.pk])
        sessions = {}
        for mode in ('terminal', 'concurrent', 'combined'):
            session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso', feedback_mode=mode)
            Message.objects.create(session=session, sender_type='AI', content='I am feeling uncertain.')
            first_reply = Message.objects.create(session=session, sender_type='USER', content='Hello, welcome back.')
            TurnAssessment.objects.create(trainee_message=first_reply, feedback_type='affirmation', feedback_text='You offered a friendly welcome. ' * 60,
                record={'boundary_relevant': False})
            last_reply = Message.objects.create(session=session, sender_type='USER', content='I can listen for a few minutes.')
            TurnAssessment.objects.create(trainee_message=last_reply, feedback_type='affirmation', feedback_text='You offered a warm and realistic limit.',
                record={'boundary_relevant': True, 'alternative_reply': 'What would help within the time we have?'})
            sessions[mode] = session.pk
        review = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso', status='COMPLETED')
        Feedback.objects.create(session=review, overview='You offered warmth and space to talk.', strengths='You preserved Agency.', improvements='Consider the timing of suggestions.', overall_score=3)
        for turn in range(15):
            Message.objects.create(session=review, sender_type='AI', content='I feel uncertain. ' * 15)
            message = Message.objects.create(session=review, sender_type='USER', content='What feels important to you? ' * 10)
            TurnAssessment.objects.create(trainee_message=message, feedback_type='affirmation', feedback_text='You invited their priorities.')
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        review_url = self.live_server_url + reverse('session_review', args=[review.pk])
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(entry_url)
            expect(page.locator('#persona-selection')).to_be_visible()
            # Compare in one frame, without modal transition transforms affecting measurements.
            toggle_sizes = page.evaluate("""() => ['live-feedback', 'student-mode'].map(id => {
                const style = getComputedStyle(document.getElementById(id));
                return [style.width, style.height];
            })""")
            assert toggle_sizes[0] == toggle_sizes[1], toggle_sizes
            page.goto(waiting_url)
            expect(page.locator('.peertrain-live-overall')).to_be_visible()
            expect(page.locator('.peertrain-reply-feedback-title')).to_have_count(0)
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                for mode, pk in sessions.items():
                    page.goto(self.live_server_url + reverse('chat', args=[pk]))
                    coach = page.locator('[data-boundary-coach]')
                    panel = page.locator('[data-boundary-coach-panel]')
                    if mode == 'terminal':
                        expect(coach).to_have_class('offcanvas-xl offcanvas-end peertrain-boundary-coach is-collapsed')
                        expect(panel).to_be_hidden()
                        expect(page.locator('[data-boundary-coach-toggle]').first).to_be_disabled()
                    else:
                        expect(panel).to_be_visible()
                        expect(panel.get_by_role('heading', name='Overall status', exact=True)).to_be_visible()
                        expect(panel.get_by_role('heading', name='Reply feedback', exact=True)).to_be_visible()
                        expect(panel.locator('[data-boundary-status]')).to_have_css('padding', '0px')
                        expect(panel.locator('.peertrain-live-overall')).to_have_css('border-bottom-width', '1px')
                        font_styles = page.evaluate("""() => {
                            const properties = ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'color'];
                            const style = selector => {
                                const computed = getComputedStyle(document.querySelector(selector));
                                return properties.map(property => computed[property]);
                            };
                            return [style('.peertrain-guidance-section summary'), style('.peertrain-feedback-section-title')];
                        }""")
                        assert font_styles[0] == font_styles[1], font_styles
                        expect(page.locator('[data-turn-feedback]')).to_have_count(2)
                        expect(page.locator('[data-turn-feedback]').first).not_to_have_attribute('open', '')
                        expect(page.locator('[data-turn-feedback]').last).to_have_attribute('open', '')
                        page.locator('[data-turn-feedback]').first.locator('summary').click()
                        expect(page.locator('[data-turn-feedback]').first).to_have_attribute('open', '')
                        expect(page.locator('.peertrain-feedback-excerpt')).to_have_count(0)
                        if width >= 1200:
                            status = page.locator('[data-boundary-status]')
                            coach.evaluate('(el) => { el.scrollTop = 0; }')
                            initial_top = status.bounding_box()['y']
                            coach.evaluate('(el) => { el.scrollTop = 250; }')
                            assert coach.evaluate('(el) => el.scrollTop') > 100
                            assert abs(status.bounding_box()['y'] - initial_top) < 2
                        expect(page.locator('.peertrain-live-alternative q em')).to_have_text('What would help within the time we have?')
                        if mode == 'concurrent':
                            expect(page.locator('[data-boundary-coach-toggle]').first).to_be_disabled()
                        else:
                            toggle = page.locator('[data-boundary-coach-toggle]').last if width >= 1200 else page.locator('.peertrain-boundary-coach-mobile-open')
                            toggle.click()
                            expect(panel).to_be_hidden()
                            expand = page.locator('[data-boundary-coach-toggle]').first if width >= 1200 else toggle
                            expand.click()
                            expect(panel).to_be_visible()
                    page.reload()
                    expect(panel).to_be_hidden() if mode == 'terminal' else expect(panel).to_be_visible()
                page.goto(review_url)
                expect(page.locator('#feedback-heading, #reflection-heading')).to_have_count(0)
                expect(page.locator('.review-feedback > section')).to_have_count(2)
                expect(page.locator('.review-title-row a .bi-arrow-right')).to_have_count(1)
                title = page.locator('#overview-heading').bounding_box()
                rating = page.locator('.review-overall-status').bounding_box()
                if width >= 1200:
                    assert rating['x'] >= title['x'] + title['width']
                    assert abs((rating['y'] + rating['height'] / 2) - (title['y'] + title['height'] / 2)) < 2
                expect(page.locator('.review-title-row h1')).to_have_css('margin-top', '0px')
                expect(page.locator('.peertrain-review > header')).to_have_css('border-bottom-width', '1px')
                if width >= 1200:
                    assert page.locator('.review-reflection').evaluate("el => getComputedStyle(el, '::before').borderLeftWidth") == '1px'
                expect(page.locator('.review-transcript .peertrain-ai-avatar').first).to_have_css('width', '40px')
                expect(page.locator('.review-transcript .peertrain-ai-avatar').first).to_have_css('height', '40px')
                expect(page.locator('.review-transcript')).to_be_visible()
                assert page.locator('.review-transcript').evaluate('(el) => el.scrollHeight > el.clientHeight')
                if width >= 1200:
                    feedback_box = page.locator('.review-feedback').bounding_box()
                    reflection_box = page.locator('.review-reflection').bounding_box()
                    assert reflection_box['x'] > feedback_box['x'] + feedback_box['width']
                    assert abs(reflection_box['width'] - feedback_box['width']) < 2
                    title_box = page.locator('.review-title-row h1').bounding_box()
                    back_box = page.locator('.review-title-row a').bounding_box()
                    assert back_box['x'] > title_box['x'] + title_box['width']
                else:
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.goto(review_url)
            ai_bubble = page.locator('.reflection-chat .peertrain-chat-message-ai .peertrain-chat-message-bubble').first
            expect(ai_bubble).to_have_css('background-color', 'rgb(238, 240, 242)')
            expect(ai_bubble).to_have_css('color', 'rgb(37, 42, 50)')
            expect(page.locator('.reflection-avatar')).to_have_css('border-radius', '50%')
            composer = page.locator('#reflection-input')
            send = page.get_by_role('button', name='Send reflection')
            initial_height = composer.bounding_box()['height']
            assert abs(initial_height - send.bounding_box()['height']) < 2
            composer.fill('A longer reflection about the conversation and my intentions. ' * 12)
            assert composer.bounding_box()['height'] > initial_height, {
                'initial': initial_height, 'metrics': composer.evaluate('(el) => ({height: el.clientHeight, scroll: el.scrollHeight, style: el.style.height, width: el.clientWidth, value: el.value.length})'), 'errors': errors,
            }
            composer.fill('Could we discuss the feedback?')
            assert abs(composer.bounding_box()['height'] - initial_height) < 2
            expect(send).to_have_css('border-radius', '50%')
            assert page.get_by_role('button', name='Save', exact=True).bounding_box()['height'] == send.bounding_box()['height']
            page.locator('#review-notes').fill('My independent note')
            page.get_by_role('button', name='Save', exact=True).click()
            expect(page.locator('[data-notes-status]')).to_have_text('Saved')
            expect(page.locator('[data-notes-status] .bi-check-circle')).to_have_count(1)
            release_reply = threading.Event()
            def delayed_reply(*args):
                release_reply.wait(timeout=10)
                return ReflectionResult(reply='What were you hoping to achieve?')
            with patch('training.services.assessment.task_call', side_effect=delayed_reply):
                page.locator('#reflection-input').fill('Could we discuss the feedback?')
                page.get_by_role('button', name='Send reflection').click()
                expect(page.locator('.reflection-pending')).to_be_visible()
                expect(page.locator('.reflection-pending .peertrain-typing-dot')).to_have_count(3)
                release_reply.set()
                expect(page.locator('.reflection-chat')).to_contain_text('What were you hoping to achieve?')
                expect(page.locator('.reflection-pending')).to_have_count(0)
            page.reload()
            expect(page.locator('#review-notes')).to_have_value('My independent note')
            expect(page.locator('.reflection-chat')).to_contain_text('Could we discuss the feedback?')
            expect(page.locator('.reflection-chat')).to_contain_text('What were you hoping to achieve?')
            page.locator('#review-notes').fill('Unsaved note')
            dialogs = []
            page.on('dialog', lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
            page.screenshot(path=str(Path(tempfile.gettempdir()) / 'peertrain-review-desktop.png'), full_page=True)
            page.reload()
            assert dialogs == ['beforeunload']
            expect(page.locator('#review-notes')).to_have_value('My independent note')
            assert not errors, errors
            browser.close()
