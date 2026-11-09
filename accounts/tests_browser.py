"""Optional homepage checks using Playwright and an installed Edge/Chrome."""
from importlib.util import find_spec
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse


BROWSER = next((str(path) for path in (
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
) if path.exists()), None)


@skipUnless(find_spec("playwright") and BROWSER, "Requires optional Playwright and installed Edge/Chrome")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HomepageBrowserTests(StaticLiveServerTestCase):
    def test_responsive_homepage_and_login_return_navigation(self):
        from playwright.sync_api import sync_playwright, expect

        user = get_user_model().objects.create_user(
            username="homepage@example.com", email="homepage@example.com",
            password="SecurePass123!", role="TRAINEE", first_name="Homepage",
        )
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, headless=True)
            page = browser.new_page()
            for width in (1440, 1200, 1100, 768, 390, 320):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.goto(self.live_server_url)
                    expect(page.get_by_role("heading", name="Welcome", exact=True)).to_be_visible()
                    login = page.get_by_role("link", name="Log in", exact=True)
                    expect(login).to_be_visible()
                    navigation = page.get_by_role("navigation", name="Primary navigation", exact=True)
                    for label, route in (("Home", "landing"), ("Scenarios", "scenarios"), ("History", "history")):
                        link = navigation.get_by_role("link", name=label, exact=True)
                        expect(link).to_be_visible()
                        expect(link).to_have_attribute("href", reverse(route))
                    brand = page.get_by_role("link", name="PeerTrain home")
                    self.assertLessEqual(brand.bounding_box()["x"] + brand.bounding_box()["width"], login.bounding_box()["x"])
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                    cards = page.locator(".peertrain-landing-step")
                    boxes = [card.bounding_box() for card in cards.all()]
                    if width >= 768:
                        self.assertEqual(len({box["y"] for box in boxes}), 1)
                    else:
                        self.assertLess(boxes[0]["y"], boxes[1]["y"])
                        self.assertLess(boxes[1]["y"], boxes[2]["y"])

            navigation.get_by_role("link", name="History", exact=True).click()
            expect(page).to_have_url(self.live_server_url + reverse("login") + "?next=/history/")
            page.goto(self.live_server_url)
            page.get_by_role("link", name="Start Training").click()
            expect(page).to_have_url(self.live_server_url + reverse("login") + "?next=/scenarios/")
            page.locator('[name="email"]').fill(user.email)
            page.locator('[name="password"]').fill("SecurePass123!")
            page.get_by_role("button", name="Log In", exact=True).click()
            expect(page).to_have_url(self.live_server_url + reverse("scenarios"))
            for width in (1440, 390, 320):
                page.set_viewport_size({"width": width, "height": 1000})
                page.goto(self.live_server_url)
                account = page.locator(".peertrain-account-menu-button")
                expect(account).to_be_visible()
                navigation = page.get_by_role("navigation", name="Primary navigation", exact=True)
                for label in ("Home", "Scenarios", "History"):
                    expect(navigation.get_by_role("link", name=label, exact=True)).to_be_visible()
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                account.click()
                expect(page.get_by_role("link", name="Log out", exact=True)).to_be_visible()
                page.get_by_role("link", name="Start Training").click()
                expect(page).to_have_url(self.live_server_url + reverse("scenarios"))
            browser.close()
