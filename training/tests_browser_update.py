"""Optional real-browser checks: run with `uv run --with playwright ... manage.py test ...`.

Uses installed Edge/Chrome; no browser download or production credentials needed.
"""
from concurrent.futures import ThreadPoolExecutor
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

from training.models import Feedback, FeedbackReviewItem, Message, Scenario, TrainingSession, TurnAssessment, LiveFeedbackEvent
from training.services.assessment import ReflectionResult

BROWSER = next((str(p) for p in (
    Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
    Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
) if p.exists()), None)


@skipUnless(find_spec('playwright') and BROWSER, 'Requires optional Playwright and installed Edge/Chrome')
@override_settings(OPENAI_API_KEY='', GEMINI_API_KEY='',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class BrowserUpdateTests(StaticLiveServerTestCase):
    @override_settings(DEBUG=True)
    def test_user_message_looks_sent_while_waiting_for_ai_and_feedback(self):
        import re
        from playwright.sync_api import sync_playwright, expect

        user = get_user_model().objects.create_user(username='message-browser', email='alex.chen@university.ac.uk', role='TRAINEE')
        scenario = Scenario.objects.create(title='Message phases', boundary_type='AGENCY', estimated_duration_min=10)
        session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
        Message.objects.create(session=session, sender_type='AI', content='Where can I begin?')
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            url = self.live_server_url + reverse('chat', args=[session.pk])
            page.goto(url)
            held = []
            page.route(url, lambda route: held.append(route))
            composer = page.locator('#id_content')
            composer.fill('What would feel helpful?')
            with page.expect_request(url):
                page.locator('[data-chat-send]').click()
            user_message = page.locator('[data-pending-user]')
            expect(user_message).to_contain_text('What would feel helpful?')
            expect(user_message.locator('time')).to_have_text(re.compile(r'^\d{2}:\d{2}$'))
            expect(user_message.locator('time')).to_have_attribute('datetime', re.compile(r'^\d{4}-'))
            expect(user_message.locator('.peertrain-chat-message-bubble')).to_have_css('opacity', '1')
            expect(page.locator('[data-pending-ai]')).to_contain_text('Responding')
            expect(page.locator('[data-chat-send]')).to_be_disabled()
            held.pop().fulfill(status=503, json={'ok': False, 'error': 'Connection interrupted.'})
            expect(page.locator('[data-chat-error]')).to_contain_text('Connection interrupted.')
            expect(user_message).to_have_count(0)
            expect(composer).to_have_value('What would feel helpful?')
            expect(page.locator('[data-chat-send]')).to_be_enabled()

            page.locator('.peertrain-test-replies-toggle').click()
            with page.expect_request(url):
                page.locator('[data-chat-action="auto_helpful"]').click()
            expect(user_message).to_contain_text('Preparing test reply')
            expect(page.locator('[data-pending-ai]')).to_have_count(0)
            with page.expect_request(url):
                held.pop().fulfill(json={'ok': True, 'generated_content': 'Would you like to tell me more?', 'response_source': 'OPENAI'})
            expect(user_message).to_contain_text('Would you like to tell me more?')
            expect(user_message.locator('time')).to_have_text(re.compile(r'^\d{2}:\d{2}$'))
            expect(user_message.locator('.peertrain-chat-message-bubble')).to_have_css('opacity', '1')
            expect(page.locator('[data-pending-ai]')).to_contain_text('Responding')
            held.pop().fulfill(json={
                'ok': True, 'user_turns': 1, 'can_send': True,
                'user_message_html': '<article data-chat-message data-message-id="101" class="peertrain-chat-message-user">Would you like to tell me more?</article>',
                'ai_message_html': '<article data-chat-message data-message-id="102">Thank you for listening.</article>',
                'boundary_coach_html': '<div data-boundary-coach-live>Feedback is ready.</div>',
            })
            expect(page.locator('[data-pending-user]')).to_have_count(0)
            expect(page.locator('[data-pending-ai]')).to_have_count(0)
            expect(page.locator('[data-message-id="101"]')).to_have_count(1)
            expect(page.locator('[data-message-id="102"]')).to_contain_text('Thank you for listening.')
            expect(page.locator('[data-boundary-coach-live]')).to_contain_text('Feedback is ready.')
            expect(composer).to_have_value('')
            expect(page.locator('[data-chat-send]')).to_be_enabled()
            browser.close()

    def test_session_navigation_completion_errors_and_review_transition(self):
        from playwright.sync_api import sync_playwright, expect, Error

        user = get_user_model().objects.create_user(username='completion-browser', role='TRAINEE')
        scenario = Scenario.objects.create(title='Completion', boundary_type='AGENCY', estimated_duration_min=10)
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        sessions = []
        for turns in (1, 4, 20):
            session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
            for turn in range(turns):
                Message.objects.create(session=session, sender_type='USER', content=f'Reply {turn}')
                Message.objects.create(session=session, sender_type='AI', content='Thank you.')
            Feedback.objects.create(session=session, overall_score=3, strengths='Listened.', improvements='Consider timing.')
            sessions.append((turns, session))
        leave_session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
        Message.objects.create(session=leave_session, sender_type='USER', content='Hello.')
        Message.objects.create(session=leave_session, sender_type='AI', content='Thank you.')
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            page.set_default_timeout(10000)
            native_dialogs = []
            def dismiss_native(dialog):
                native_dialogs.append(dialog.type)
                dialog.dismiss()
            page.on('dialog', dismiss_native)
            for turns, session in sessions:
                with self.subTest(turns=turns):
                    chat_url = self.live_server_url + reverse('chat', args=[session.pk])
                    review_url = self.live_server_url + reverse('session_review', args=[session.pk])
                    page.goto(self.live_server_url)
                    page.goto(chat_url)
                    expect(page.locator('[data-user-turn-count]')).to_have_count(0)
                    expect(page.locator('[data-chat-finish-label]')).to_have_text('End session')
                    dialog = page.locator('[data-session-dialog]')
                    for index, link in enumerate(('.peertrain-topnav-brand', '.peertrain-topnav-desktop .peertrain-topnav-link')):
                        page.locator(link).first.click()
                        expect(dialog).to_be_visible()
                        expect(dialog).to_contain_text('Session in progress')
                        expect(page.locator('[data-session-continue]')).to_have_text('Continue session')
                        self.assertEqual(page.locator('[data-session-continue]').evaluate('(button) => getComputedStyle(button).backgroundColor'), 'rgb(255, 255, 255)')
                        self.assertEqual(page.locator('[data-session-leave]').evaluate('(button) => getComputedStyle(button).backgroundColor'), 'rgb(217, 48, 37)')
                        page.locator('[data-session-close]' if index else '[data-session-continue]').click()
                        expect(dialog).not_to_be_visible()
                        self.assertEqual(page.url, chat_url)
                    page.locator('.peertrain-account-menu-button').click()
                    page.locator('.peertrain-account-dropdown a').click()
                    expect(dialog).to_be_visible()
                    page.locator('[data-session-continue]').click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.get_by_role('button', name='Open navigation menu').click()
                    page.locator('.peertrain-mobile-logout').click()
                    expect(dialog).to_be_visible()
                    page.locator('[data-session-continue]').click()
                    page.get_by_role('button', name='Close navigation menu').click()
                    expect(page.locator('#peertrain-mobile-navigation')).not_to_be_visible()
                    page.locator('[data-guidance-mobile-toggle]').click()
                    expect(page.locator('[data-guidance-sidebar]')).to_have_class('peertrain-guidance-sidebar is-mobile-open')
                    page.keyboard.press('Escape')
                    self.assertTrue(page.evaluate("""() => {
                        const event = new Event('beforeunload', {cancelable: true});
                        window.dispatchEvent(event); return event.defaultPrevented;
                    }"""))
                    for navigate in (page.reload, page.go_back):
                        previous = len(native_dialogs)
                        try:
                            navigate(timeout=2000)
                        except Error:
                            pass  # Dismissing beforeunload aborts the navigation.
                        self.assertEqual(native_dialogs[previous:], ['beforeunload'])
                        self.assertEqual(page.url, chat_url)
                    pending_finish = []
                    pending_review = []
                    def hold_finish(route):
                        pending_finish.append(route)
                    page.route(chat_url, hold_finish)
                    page.route(review_url, lambda route: pending_review.append(route))
                    def end_session():
                        page.locator('[data-chat-finish]').click()
                        if turns < 20:
                            expect(dialog).to_be_visible()
                            expect(dialog).to_contain_text('End this session early?' if turns < 4 else 'End this session?')
                            expect(page.locator('[data-session-close]')).to_be_visible()
                            expect(page.locator('[data-session-continue]')).to_have_text('Continue session')
                            if turns < 4:
                                expect(dialog).to_contain_text('feedback may be based on limited evidence')
                                self.assertEqual(page.locator('[data-session-continue]').evaluate('(button) => getComputedStyle(button).backgroundColor'), 'rgb(26, 115, 232)')
                                self.assertEqual(page.locator('[data-session-confirm]').evaluate('(button) => getComputedStyle(button).color'), 'rgb(217, 48, 37)')
                                self.assertEqual(page.locator('[data-session-confirm]').evaluate('(button) => getComputedStyle(button).borderTopColor'), 'rgb(217, 48, 37)')
                                self.assertEqual(page.locator('[data-session-confirm]').evaluate('(button) => getComputedStyle(button).backgroundColor'), 'rgb(255, 255, 255)')
                            page.locator('[data-session-confirm]').click()
                        expect(page.locator('[data-completion-overlay]')).to_be_visible()
                        page.wait_for_function('document.querySelector("[data-chat-form]").getAttribute("aria-busy") === "true"')
                        # Let the route callback run before inspecting the held request.
                        page.wait_for_timeout(50)
                    end_session()
                    self.assertEqual(len(pending_finish), 1)
                    page.keyboard.press('Escape')
                    expect(page.locator('[data-completion-overlay]')).to_be_visible()
                    pending_finish.pop().fulfill(status=503, content_type='application/json', body='{"ok":false,"error":"Please try again."}')
                    expect(page.locator('[data-completion-overlay]')).not_to_be_visible()
                    expect(page.locator('[data-chat-error]')).to_contain_text("We couldn't complete your session")
                    expect(page.locator('[data-chat-finish]')).to_be_enabled()
                    page.locator('.peertrain-topnav-brand').click()
                    expect(dialog).to_be_visible()
                    page.locator('[data-session-continue]').click()
                    end_session()
                    route = pending_finish.pop()
                    response = route.fetch()
                    self.assertTrue(response.ok)
                    # Simulate losing the response after the server committed completion.
                    route.fulfill(status=503, content_type='application/json', body='{"ok":false,"error":"Connection interrupted."}')
                    expect(page.locator('[data-completion-overlay]')).not_to_be_visible()
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        executor.submit(session.refresh_from_db).result()
                    self.assertEqual(session.status, 'COMPLETED')
                    end_session()
                    route = pending_finish.pop()
                    route.fulfill(response=route.fetch())
                    page.wait_for_timeout(100)
                    self.assertEqual(len(pending_review), 1)
                    with page.expect_navigation(url=review_url):
                        pending_review.pop().continue_()
                    self.assertEqual(page.url, review_url)
                    page.unroute(chat_url)
                    page.unroute(review_url)
                    page.set_viewport_size({'width': 1440, 'height': 900})
            self.assertEqual(native_dialogs, ['beforeunload'] * 6)
            page.goto(self.live_server_url + reverse('chat', args=[leave_session.pk]))
            history_url = self.live_server_url + reverse('history')
            page.locator('.peertrain-topnav-desktop a[href="' + reverse('history') + '"]').click()
            expect(page.locator('[data-session-dialog]')).to_be_visible()
            expect(page.locator('[data-session-dialog-message] strong')).to_have_text('End session')
            with page.expect_navigation(url=history_url):
                page.get_by_role('button', name='Leave anyway', exact=True).click()
            self.assertEqual(native_dialogs, ['beforeunload'] * 6)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(leave_session.refresh_from_db).result()
            self.assertEqual(leave_session.status, 'IN_PROGRESS')
            self.assertIsNone(leave_session.end_time)
            browser.close()

    def test_guidance_persona_refresh_pending_labels_and_responsive_controls(self):
        from playwright.sync_api import sync_playwright, expect
        from training.prompts import ROLEPLAY_SCENARIOS
        from training.content import PRACTICE_FOCUS, PRACTICE_INTRODUCTIONS

        user = get_user_model().objects.create_user(username='guidance-browser', role='TRAINEE')
        sessions = []
        for boundary, config in ROLEPLAY_SCENARIOS.items():
            scenario = Scenario.objects.create(title=boundary, boundary_type=boundary, estimated_duration_min=10)
            for persona in ('pso', 'student'):
                session = TrainingSession.objects.create(user=user, scenario=scenario)
                sessions.append((session.pk, boundary, persona, config['personas'][persona]))
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            page.set_default_timeout(10000)
            held_openings = []
            def hold_opening(route):
                if 'start_roleplay' in (route.request.post_data or ''):
                    held_openings.append(route)
                else:
                    route.continue_()
            page.route('**/sessions/*/chat/', hold_opening)
            for pk, boundary, persona, character in sessions:
                with self.subTest(persona=persona, name=character['name']):
                    page.set_viewport_size({'width': 1440, 'height': 900})
                    page.goto(self.live_server_url + reverse('chat', args=[pk]))
                    expect(page.locator('#persona-selection')).to_be_visible()
                    page.locator('#student-mode').set_checked(persona == 'student')
                    page.get_by_role('button', name='Got it!').click()
                    guidance = page.locator('[data-practice-guidance]')
                    expect(guidance).to_contain_text(f"This is a practice conversation with {character['name']}, an AI-simulated help-seeker.")
                    expect(guidance).to_contain_text(PRACTICE_INTRODUCTIONS[boundary][persona])
                    expect(guidance).to_contain_text(PRACTICE_FOCUS[boundary])
                    expect(guidance.locator('h3')).to_have_text(['About this conversation', 'The setting', 'Your focus', 'Please note'])
                    expect(page.locator('[data-pending-ai] .peertrain-chat-speaker')).to_have_text(character['name'])
                    panel = page.locator('[data-guidance-panel]')
                    expect(panel).to_be_visible()
                    original_width = panel.evaluate('(el) => el.clientWidth')
                    page.get_by_role('button', name='Collapse guidance', exact=True).click()
                    expect(panel).to_be_hidden()
                    page.locator('[data-guidance-sidebar]').evaluate("""async el => {
                        await Promise.all(el.getAnimations().map(animation => animation.finished));
                    }""")
                    page.locator('[data-guidance-rail] [data-guidance-toggle]').click()
                    expect(panel).to_be_visible()
                    widths = panel.evaluate("""async el => {
                        const widths = [el.clientWidth];
                        const sidebar = el.closest('[data-guidance-sidebar]');
                        while (sidebar.getAnimations().length) {
                            await new Promise(requestAnimationFrame);
                            widths.push(el.clientWidth);
                        }
                        return widths;
                    }""")
                    self.assertTrue(all(width == original_width for width in widths), widths)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.locator('[data-guidance-mobile-toggle]').click()
                    expect(page.locator('[data-guidance-sidebar]')).to_have_class('peertrain-guidance-sidebar is-mobile-open')
                    note = guidance.locator('.peertrain-guidance-note')
                    note.scroll_into_view_if_needed()
                    expect(note).to_be_in_viewport()
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                    page.keyboard.press('Escape')
                    expect(page.locator('[data-guidance-mobile-toggle]')).to_have_attribute('aria-expanded', 'false')
                    for route in held_openings:
                        route.abort()
                    held_openings.clear()
            browser.close()

    def test_scenario_cards_fit_desktop_and_wrap_on_narrow_screens(self):
        from django.core.management import call_command
        from playwright.sync_api import sync_playwright, expect

        call_command('seed_peertrain', verbosity=0)
        user = get_user_model().objects.get(email='alex.chen@university.ac.uk')
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context()
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            for width, height, columns in ((1920, 900, 3), (1440, 900, 3), (1366, 768, 3), (1280, 720, 3),
                                           (1024, 768, 3), (768, 1024, 2), (390, 844, 1)):
                with self.subTest(width=width, height=height):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.goto(self.live_server_url + reverse('scenarios'))
                    page.evaluate('document.fonts.ready')
                    cards = page.locator('.scenario-card')
                    expect(cards).to_have_count(3)
                    expect(page.get_by_role('button', name='Start Scenario')).to_have_count(3)
                    expect(page.get_by_text('Coming Soon', exact=False)).to_have_count(0)
                    boxes = [card.bounding_box() for card in cards.all()]
                    self.assertEqual(sum(abs(box['y'] - boxes[0]['y']) < 1 for box in boxes), columns)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                    if columns == 3:
                        self.assertTrue(page.evaluate('document.documentElement.scrollHeight <= window.innerHeight'))
                        self.assertTrue(all(box['y'] + box['height'] <= height for box in boxes))
                        for box in boxes:
                            bottom_gap = height - box['y'] - box['height']
                            self.assertGreaterEqual(bottom_gap, 24)
                            self.assertLessEqual(bottom_gap, 80)
            browser.close()

    def test_review_cards_use_content_height_and_give_remaining_space_to_conversation(self):
        from playwright.sync_api import sync_playwright

        user = get_user_model().objects.create_user(username='review-layout-browser', role='TRAINEE')
        scenario = Scenario.objects.create(title='Layout', boundary_type='AGENCY', estimated_duration_min=10)
        sessions = []
        for overview in ('Brief feedback.', 'Detailed feedback. ' * 250):
            session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso', status='COMPLETED')
            Feedback.objects.create(session=session, overview=overview, strengths='Listened well.', improvements='Ask another question.')
            Message.objects.create(session=session, sender_type='AI', content='Opening disclosure.')
            sessions.append(session)
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value

        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1920, 'height': 1200})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            for width, height in ((2560, 1440), (1920, 1200), (1440, 900), (1100, 650)):
                page.set_viewport_size({'width': width, 'height': height})
                for index, session in enumerate(sessions):
                    page.goto(self.live_server_url + reverse('session_review', args=[session.pk]))
                    dimensions = page.evaluate('''() => {
                        const feedback = document.querySelector('.review-feedback');
                        const overview = document.querySelector('.review-overview');
                        const content = document.querySelector('.review-overview-content');
                        const conversation = document.querySelector('.review-conversation');
                        return {
                            feedbackHeight: feedback.getBoundingClientRect().height,
                            overviewHeight: overview.getBoundingClientRect().height,
                            contentHeight: content.clientHeight,
                            contentScrollHeight: content.scrollHeight,
                            contentEmptySpace: content.getBoundingClientRect().bottom - content.lastElementChild.getBoundingClientRect().bottom,
                            conversationHeight: conversation.getBoundingClientRect().height,
                        };
                    }''')
                    self.assertLessEqual(dimensions['overviewHeight'], dimensions['feedbackHeight'] * .5 + 1)
                    if index == 0:
                        self.assertLessEqual(dimensions['contentEmptySpace'], 4)
                        if height >= 900:
                            self.assertGreater(dimensions['conversationHeight'], dimensions['overviewHeight'])
                    else:
                        self.assertGreater(dimensions['contentScrollHeight'], dimensions['contentHeight'])
                    self.assertGreater(dimensions['conversationHeight'], 0)
            browser.close()

    def test_review_alternative_navigation_scopes_scroll_and_supports_keyboard_and_reduced_motion(self):
        from playwright.sync_api import sync_playwright, expect
        user = get_user_model().objects.create_user(username='alternative-browser', role='TRAINEE')
        scenario = Scenario.objects.create(title='Navigation', boundary_type='AGENCY', estimated_duration_min=10)
        session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso', status='COMPLETED')
        feedback = Feedback.objects.create(session=session, strengths='Saved strengths', improvements='Saved considerations')
        Message.objects.create(session=session, sender_type='AI', content='Opening disclosure. ' * 20)
        for number in range(1, 19):
            message = Message.objects.create(session=session, sender_type='USER', content=f'Reply {number}. ' * 25)
            if number in (2, 13):
                for index in range(2 if number == 2 else 1):
                    FeedbackReviewItem.objects.create(feedback=feedback, message=message, turn_index=99,
                        better_reply=f'Saved alternative {number}.{index}')
            if number == 8:
                TurnAssessment.objects.create(trainee_message=message,
                    record={'alternative_reply': 'Historical assessed alternative'})
            Message.objects.create(session=session, sender_type='AI', content='Help-seeker response. ' * 20)
        empty = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso', status='COMPLETED')
        Feedback.objects.create(session=empty, strengths='Saved strengths', improvements='Saved considerations')
        Message.objects.create(session=empty, sender_type='USER', content='No alternative needed.')
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for width, height, motion in ((1440, 1000, 'no-preference'), (1100, 650, 'reduce'), (390, 1000, 'reduce')):
                page.set_viewport_size({'width': width, 'height': height})
                page.emulate_media(reduced_motion=motion)
                page.goto(self.live_server_url + reverse('session_review', args=[session.pk]))
                nav = page.get_by_role('navigation', name='Alternative replies to consider')
                expect(nav).to_be_visible()
                expect(nav.locator('button')).to_have_text(['Turn 2', 'Turn 8', 'Turn 13'])
                expect(nav.locator('.review-alternative-count')).to_have_text('(4)')
                heading_box = nav.locator('h4').bounding_box()
                buttons_box = nav.locator('.review-alternative-buttons').bounding_box()
                if width >= 1100:
                    assert buttons_box['x'] >= heading_box['x'] + heading_box['width']
                    assert abs(buttons_box['y'] + buttons_box['height'] / 2 - heading_box['y'] - heading_box['height'] / 2) < 2
                card = page.locator('.review-alternative').first
                expect(nav.locator('button').first).to_have_css('background-color', card.evaluate('el => getComputedStyle(el).backgroundColor'))
                expect(nav.locator('button').first).to_have_css('border-top-color', card.evaluate('el => getComputedStyle(el).borderTopColor'))
                assert page.locator('#review-transcript').evaluate('''el => {
                    const transcript = el.getBoundingClientRect();
                    const container = el.closest('.review-conversation');
                    const card = container.getBoundingClientRect();
                    const padding = parseFloat(getComputedStyle(container).paddingBottom);
                    return transcript.height > 0 && transcript.bottom <= card.bottom - padding + 1;
                }''')
                expect(page.locator('.review-alternative strong')).to_have_text(['An alternative reply to consider'] * 4)
                expect(page.locator('#turn-2')).to_have_css('font-weight', '400')
                expect(page.locator('#turn-2')).to_have_css('border-top-width', '1px')
                expect(page.locator('#opening')).to_have_css('font-weight', '400')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#review-transcript').evaluate('''el => {
                    el.scrollTop = el.scrollHeight;
                    const scroll = el.scrollTo.bind(el);
                    el.scrollTo = options => { window.lastReviewScroll = options; scroll(options); };
                }''')
                for number, keyboard in ((8, False), (2, True), (13, True)):
                    button = nav.get_by_role('button', name=f'Turn {number}', exact=True)
                    button.focus()
                    before = page.evaluate('({x: scrollX, y: scrollY})')
                    nav_top = nav.bounding_box()['y']
                    if keyboard:
                        page.keyboard.press('Enter' if number == 2 else 'Space')
                    else:
                        button.click()
                    card = page.locator(f'[data-alternative-card-turn="{number}"]').first
                    expect(card).to_be_focused()
                    expect(card).to_have_class('review-alternative review-alternative-highlight')
                    page.wait_for_function('''number => {
                        const container = document.querySelector('#review-transcript');
                        const card = container.querySelector(`[data-alternative-card-turn="${number}"]`);
                        const bounds = container.getBoundingClientRect();
                        const target = card.getBoundingClientRect();
                        return target.top >= bounds.top - 2 && target.bottom <= bounds.bottom + 2;
                    }''', arg=number)
                    assert page.evaluate('({x: scrollX, y: scrollY})') == before
                    assert abs(nav.bounding_box()['y'] - nav_top) < 1
                    assert page.evaluate('window.lastReviewScroll.behavior') == ('instant' if motion == 'reduce' else 'smooth')
                expect(card).not_to_have_class('review-alternative review-alternative-highlight', timeout=4000)
                page.goto(self.live_server_url + reverse('session_review', args=[empty.pk]))
                expect(page.locator('.review-alternative-nav')).to_have_count(0)
                expect(page.locator('#review-transcript')).to_be_visible()
            assert not errors, errors
            browser.close()

    def test_manual_feedback_logging_and_paused_controls_on_desktop_and_mobile(self):
        from playwright.sync_api import sync_playwright, expect
        user = get_user_model().objects.create_user(username='log-browser', email='log-browser@example.com', role='TRAINEE')
        scenario = Scenario.objects.create(title='Logging', boundary_type='AGENCY', estimated_duration_min=10)
        session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
        Message.objects.create(session=session, sender_type='AI', content='I feel uncertain about where to begin.')
        self.client.force_login(user)
        cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        url = self.live_server_url + reverse('chat', args=[session.pk])
        def query(call):
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(call).result()

        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': cookie, 'url': self.live_server_url}])
            page = context.new_page()
            page.goto(url)
            self.assertEqual(query(lambda: LiveFeedbackEvent.objects.count()), 0)
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                toggle = page.locator('[data-boundary-coach-toggle]').last if width >= 1200 else page.locator('.peertrain-boundary-coach-mobile-open')
                with page.expect_response('**/live-feedback-events/'):
                    toggle.click()
                expand = page.locator('[data-boundary-coach-toggle]').first if width >= 1200 else toggle
                with page.expect_response('**/live-feedback-events/'):
                    expand.click()
                page.reload()
                expected = ['CLOSE', 'OPEN'] if width == 1440 else ['CLOSE', 'OPEN', 'CLOSE', 'OPEN']
                self.assertEqual(query(lambda: list(LiveFeedbackEvent.objects.values_list('event_type', flat=True))), expected)
            # A real failed turn keeps the accepted message visible and disables progression.
            with patch('training.services.assessment.task_call', side_effect=RuntimeError('Unavailable')):
                page.locator('#id_content').fill('What feels important to you?')
                page.locator('[data-chat-send]').click()
                expect(page.locator('[data-chat-error]')).to_contain_text('Practice temporarily paused')
            expect(page.locator('#peertrain-chat-log')).to_contain_text('What feels important to you?')
            expect(page.locator('#id_content')).to_be_disabled()
            expect(page.locator('[data-chat-finish]')).to_be_disabled()
            page.reload()
            expect(page.get_by_role('alert').filter(has_text='Practice temporarily paused')).to_be_visible()
            expect(page.locator('[data-chat-send]')).to_be_disabled()
            self.assertEqual(query(lambda: session.messages.filter(sender_type='USER').count()), 1)
            self.assertEqual(query(lambda: session.messages.filter(sender_type='AI').count()), 1)
            self.assertEqual(query(lambda: LiveFeedbackEvent.objects.count()), 4)
            browser.close()

    def test_all_scenario_sidebars_review_tabs_scroll_reflection_and_notes(self):
        from playwright.sync_api import sync_playwright, expect
        user = get_user_model().objects.create_user(username='browser', email='browser@example.com', password='test', role='TRAINEE')
        scenario = Scenario.objects.create(title='Agency', boundary_type='AGENCY', description='Practice', estimated_duration_min=10)
        entry = TrainingSession.objects.create(user=user, scenario=scenario)
        entry_url = self.live_server_url + reverse('chat', args=[entry.pk])
        waiting = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
        Message.objects.create(session=waiting, sender_type='AI', content='I have had a tiring week.')
        waiting_url = self.live_server_url + reverse('chat', args=[waiting.pk])
        sessions = {}
        for boundary in ('AGENCY', 'RELATIONSHIP', 'SAFETY'):
            scenario = Scenario.objects.get_or_create(title=boundary, defaults={
                'boundary_type': boundary, 'description': 'Practice', 'estimated_duration_min': 10})[0]
            session = TrainingSession.objects.create(user=user, scenario=scenario, persona='pso')
            Message.objects.create(session=session, sender_type='AI', content='I am feeling uncertain.')
            first_reply = Message.objects.create(session=session, sender_type='USER', content='Hello, welcome back.')
            TurnAssessment.objects.create(trainee_message=first_reply, feedback_type='affirmation', feedback_text='You offered a friendly welcome. ' * 60,
                record={'boundary_relevant': False})
            last_reply = Message.objects.create(session=session, sender_type='USER', content='I can listen for a few minutes.')
            TurnAssessment.objects.create(trainee_message=last_reply, feedback_type='affirmation', feedback_text='You offered a warm and realistic limit.',
                record={'boundary_relevant': True, 'alternative_reply': 'What would help within the time we have?'})
            sessions[boundary] = session.pk
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
            expect(page.locator('#live-feedback')).to_have_count(0)
            expect(page.locator('#student-mode')).to_be_visible()
            page.goto(waiting_url)
            expect(page.locator('.peertrain-live-overall')).to_be_visible()
            expect(page.locator('.peertrain-reply-feedback-title')).to_have_count(0)
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                for boundary, pk in sessions.items():
                    page.goto(self.live_server_url + reverse('chat', args=[pk]))
                    coach = page.locator('[data-boundary-coach]')
                    panel = page.locator('[data-boundary-coach-panel]')
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
                        return [style('.peertrain-guidance-section h3'), style('.peertrain-feedback-section-title')];
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
                    toggle = page.locator('[data-boundary-coach-toggle]').last if width >= 1200 else page.locator('.peertrain-boundary-coach-mobile-open')
                    expect(toggle).to_be_enabled()
                    toggle.click()
                    expect(panel).to_be_hidden()
                    expand = page.locator('[data-boundary-coach-toggle]').first if width >= 1200 else toggle
                    expand.click()
                    expect(panel).to_be_visible()
                    toggle.click()
                    expect(panel).to_be_hidden()
                    page.reload()
                    expect(panel).to_be_visible()
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
                expect(page.locator('.peertrain-review > header')).to_have_css('border-bottom-width', '0px')
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
                    assert abs(2 * reflection_box['width'] - feedback_box['width']) < 2
                    assert abs(1000 - (feedback_box['y'] + feedback_box['height']) - 16) < 2
                    assert page.evaluate('document.documentElement.scrollHeight <= innerHeight')
                    title_box = page.locator('.review-title-row h1').bounding_box()
                    back_box = page.locator('.review-title-row a').bounding_box()
                    assert back_box['x'] > title_box['x'] + title_box['width']
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                ai_tab = page.get_by_role('tab', name='Reflect with AI', exact=True)
                notes_tab = page.get_by_role('tab', name='My notes', exact=True)
                expect(ai_tab).to_have_attribute('aria-selected', 'true')
                expect(page.locator('#notes-panel')).to_be_hidden()
                requests = []
                page.on('request', lambda request: requests.append(request) if request.method == 'POST' else None)
                with patch('training.services.assessment.task_call') as call:
                    page.locator('#reflection-input').fill('Unsent reflection draft')
                    ai_tab.focus()
                    page.keyboard.press('ArrowRight')
                    expect(notes_tab).to_be_focused()
                    expect(notes_tab).to_have_attribute('aria-selected', 'true')
                    expect(page.locator('#reflect-ai-panel')).to_be_hidden()
                    page.locator('#review-notes').fill('Unsaved tab draft')
                    assert page.locator('#review-notes').bounding_box()['height'] > 500
                    notes_tab.focus()
                    page.keyboard.press('Home')
                    expect(ai_tab).to_be_focused()
                    expect(page.locator('#reflection-input')).to_have_value('Unsent reflection draft')
                    page.keyboard.press('End')
                    expect(page.locator('#review-notes')).to_have_value('Unsaved tab draft')
                    page.keyboard.press('ArrowRight')
                    expect(ai_tab).to_be_focused()
                    page.keyboard.press('ArrowLeft')
                    expect(notes_tab).to_be_focused()
                    call.assert_not_called()
                assert requests == []
                page.locator('#review-notes').fill('')

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
            send_height = send.bounding_box()['height']
            page.get_by_role('tab', name='My notes', exact=True).click()
            assert page.get_by_role('button', name='Save', exact=True).bounding_box()['height'] == send_height
            page.locator('#review-notes').fill('My independent note')
            page.get_by_role('button', name='Save', exact=True).click()
            expect(page.locator('[data-notes-status]')).to_have_text('Saved')
            expect(page.locator('[data-notes-status] .bi-check-circle')).to_have_count(1)
            page.get_by_role('tab', name='Reflect with AI', exact=True).click()
            release_reply = threading.Event()
            def delayed_reply(*args):
                release_reply.wait(timeout=10)
                return ReflectionResult(reply='What were you hoping to achieve? ' * 80)
            with patch('training.services.assessment.task_call', side_effect=delayed_reply):
                page.locator('#reflection-input').fill('Could we discuss the feedback?')
                page.get_by_role('button', name='Send reflection').click()
                expect(page.locator('.reflection-pending')).to_be_visible()
                expect(page.locator('.reflection-pending .peertrain-typing-dot')).to_have_count(3)
                page.get_by_role('tab', name='My notes', exact=True).click()
                release_reply.set()
                expect(page.locator('.reflection-chat')).to_contain_text('What were you hoping to achieve?')
                expect(page.locator('.reflection-pending')).to_have_count(0)
                expect(page.locator('#notes-panel')).to_be_visible()
                expect(page.get_by_role('tab', name='My notes', exact=True)).to_be_focused()
                expect(page.locator('#review-notes')).to_have_value('My independent note')
                page.get_by_role('tab', name='Reflect with AI', exact=True).click()
                assert page.locator('.reflection-chat').evaluate('(el) => el.scrollHeight > el.clientHeight')
                panel_box = page.locator('#reflect-ai-panel').bounding_box()
                composer_box = page.locator('#reflection-input').bounding_box()
                assert composer_box['y'] + composer_box['height'] <= panel_box['y'] + panel_box['height']
            page.reload()
            expect(page.locator('#review-notes')).to_have_value('My independent note')
            expect(page.locator('.reflection-chat')).to_contain_text('Could we discuss the feedback?')
            expect(page.locator('.reflection-chat')).to_contain_text('What were you hoping to achieve?')
            page.get_by_role('tab', name='My notes', exact=True).click()
            page.locator('#review-notes').fill('Unsaved note')
            dialogs = []
            page.on('dialog', lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
            page.screenshot(path=str(Path(tempfile.gettempdir()) / 'peertrain-review-desktop.png'), full_page=True)
            page.reload()
            assert dialogs == ['beforeunload']
            expect(page.locator('#review-notes')).to_have_value('My independent note')
            assert not errors, errors
            browser.close()
