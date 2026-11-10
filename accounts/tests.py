from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse


User = get_user_model()


@override_settings(ADMIN_REGISTRATION_CODE="secret-admin-code")
class AuthenticationFlowTests(TestCase):
    def _create_user(self, email, role, password="SecurePass123!"):
        return User.objects.create_user(
            username=email,
            email=email,
            password=password,
            role=role,
            first_name="Test",
            last_name="User",
        )

    def test_public_registration_creates_trainee_account(self):
        response = self.client.post(
            reverse("register"),
            {
                "full_name": "Alex Chen",
                "email": "alex.chen@example.com",
                "password1": "SecurePass123!",
                "password2": "SecurePass123!",
            },
        )

        self.assertRedirects(response, reverse("login"))
        user = User.objects.get(email="alex.chen@example.com")
        self.assertEqual(user.role, User.Roles.TRAINEE)
        self.assertFalse(user.is_staff)
        self.assertEqual(user.username, user.email)

    def test_public_registration_hides_redundant_account_type_note(self):
        response = self.client.get(reverse("register"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Account type")
        self.assertNotContains(response, "This page creates trainee accounts only.")

    def test_public_pages_render_the_expected_logo_and_favicon(self):
        landing_response = self.client.get(reverse("landing"))
        login_response = self.client.get(reverse("login"))

        self.assertEqual(landing_response.status_code, 200)
        self.assertContains(landing_response, 'rel="icon"')
        self.assertContains(landing_response, "/static/images/peertrain-logo.svg", count=2)
        self.assertContains(landing_response, 'class="peertrain-brand-icon"')

        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, "/static/images/peertrain-logo.svg", count=2)
        self.assertContains(login_response, 'class="peertrain-brand-icon"')

    def test_homepage_copy_matches_readme_specification(self):
        copy = (Path(settings.BASE_DIR) / "README.md").read_text(encoding="utf-8")
        copy = copy.split("### Hero\n", 1)[1].split("### Navigation labels", 1)[0]
        response = self.client.get(reverse("landing"))
        for paragraph in copy.strip().split("\n\n"):
            if paragraph.startswith(("#", "**")):
                continue
            text = paragraph.replace("Start Training + arrow icon", "Start Training")
            with self.subTest(copy=text):
                self.assertContains(response, text)

    def test_admin_registration_rejects_wrong_passcode(self):
        response = self.client.post(
            reverse("admin_register"),
            {
                "full_name": "Sarah Kim",
                "email": "admin.candidate@example.com",
                "admin_passcode": "wrong-code",
                "password1": "SecurePass123!",
                "password2": "SecurePass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The admin registration code is incorrect.")
        self.assertFalse(User.objects.filter(email="admin.candidate@example.com").exists())

    def test_admin_registration_creates_admin_account_with_valid_passcode(self):
        response = self.client.post(
            reverse("admin_register"),
            {
                "full_name": "Sarah Kim",
                "email": "admin.candidate@example.com",
                "admin_passcode": "secret-admin-code",
                "password1": "SecurePass123!",
                "password2": "SecurePass123!",
            },
        )

        self.assertRedirects(response, reverse("login"))
        user = User.objects.get(email="admin.candidate@example.com")
        self.assertEqual(user.role, User.Roles.ADMIN)
        self.assertTrue(user.is_staff)

    def test_login_defaults_to_landing_for_trainees_and_users_for_admins(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        admin = self._create_user("admin@example.com", User.Roles.ADMIN)

        trainee_response = self.client.post(
            reverse("login"),
            {"email": trainee.email, "password": "SecurePass123!"},
        )
        self.assertRedirects(trainee_response, reverse("landing"))

        self.client.logout()

        admin_response = self.client.post(
            reverse("login"),
            {"email": admin.email, "password": "SecurePass123!"},
        )
        self.assertRedirects(admin_response, reverse("admin_users"))

        self.client.logout()
        superuser = User.objects.create_superuser(
            username="superuser@example.com", email="superuser@example.com",
            password="SecurePass123!",
        )
        response = self.client.post(reverse("login"), {
            "email": superuser.email, "password": "SecurePass123!",
        })
        self.assertRedirects(response, reverse("admin_users"))

    def test_login_does_not_queue_a_redundant_welcome_success_message(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)

        response = self.client.post(
            reverse("login"),
            {"email": trainee.email, "password": "SecurePass123!"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Welcome back to PeerTrain.")
        self.assertNotContains(response, "data-peertrain-success-toast")

    def test_start_training_returns_to_scenarios_after_login(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        destination = reverse("scenarios")
        response = self.client.get(destination)
        login_url = reverse("login") + "?" + urlencode({"next": destination}, safe="/")
        self.assertRedirects(response, login_url)

        response = self.client.get(login_url)
        self.assertContains(response, f'<input type="hidden" name="next" value="{destination}">', html=True)
        response = self.client.post(reverse("login"), {
            "email": trainee.email, "password": "SecurePass123!", "next": destination,
        })
        self.assertRedirects(response, destination)
        self.assertContains(self.client.get(destination), "Scenario Selection")

    def test_all_training_routes_require_login_for_direct_get_and_post(self):
        from training.urls import urlpatterns

        for pattern in urlpatterns:
            kwargs = {name: 999 for name in pattern.pattern.converters}
            destination = reverse(pattern.name, kwargs=kwargs) + "?source=direct&filter=all"
            for method in (self.client.get, self.client.post):
                with self.subTest(route=pattern.name, method=method.__name__):
                    response = method(destination)
                    self.assertRedirects(
                        response,
                        reverse("login") + "?" + urlencode({"next": destination}, safe="/"),
                        fetch_redirect_response=False,
                    )

    def test_login_preserves_query_string_after_invalid_credentials(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        destination = reverse("history") + "?search=boundary&page=2"
        response = self.client.post(reverse("login"), {
            "email": trainee.email, "password": "wrong", "next": destination,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["next"], destination)
        self.assertContains(response, 'value="/history/?search=boundary&amp;page=2"')
        response = self.client.post(reverse("login"), {
            "email": trainee.email, "password": "SecurePass123!", "next": destination,
        })
        self.assertRedirects(response, destination)

    def test_login_returns_admin_to_requested_research_page(self):
        admin = self._create_user("admin@example.com", User.Roles.ADMIN)
        destination = reverse("admin_records")
        response = self.client.post(reverse("login") + "?" + urlencode({"next": destination}), {
            "email": admin.email, "password": "SecurePass123!",
        })
        self.assertRedirects(response, destination)

    def test_login_rejects_external_or_insecure_return_urls(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        for destination in ("https://example.com/", "//example.com/", "javascript:alert(1)", "http://testserver/scenarios/"):
            with self.subTest(next=destination):
                self.client.logout()
                response = self.client.post(reverse("login"), {
                    "email": trainee.email, "password": "SecurePass123!", "next": destination,
                }, secure=True)
                self.assertRedirects(response, reverse("landing"))

    def test_authenticated_login_respects_safe_next_url(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        self.client.force_login(trainee)
        response = self.client.get(reverse("login"), {"next": reverse("scenarios")})
        self.assertRedirects(response, reverse("scenarios"))
        response = self.client.get(reverse("login"), {"next": "https://example.com/"})
        self.assertRedirects(response, reverse("landing"))

    def test_authenticated_auth_pages_use_role_default_destination(self):
        for role in (User.Roles.TRAINEE, User.Roles.ADMIN):
            user = self._create_user(f"{role}@example.com", role)
            self.client.force_login(user)
            for page in ("login", "register", "admin_register"):
                with self.subTest(role=role, page=page):
                    destination = "admin_users" if role == User.Roles.ADMIN else "landing"
                    self.assertRedirects(self.client.get(reverse(page)), reverse(destination))

    def test_homepage_is_available_with_existing_account_menu_for_both_roles(self):
        for role in (User.Roles.TRAINEE, User.Roles.ADMIN):
            with self.subTest(role=role):
                user = self._create_user(f"{role}@example.com", role)
                self.client.force_login(user)
                response = self.client.get(reverse("landing"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "How PeerTrain works")
                self.assertContains(response, "peertrain-account-menu-button")
                self.assertContains(response, user.full_name_or_email)
                self.assertContains(response, reverse("logout"))
                logo_destination = reverse("admin_users" if role == User.Roles.ADMIN else "landing")
                self.assertRegex(response.content.decode(), f'href="{logo_destination}"\\s+class="peertrain-topnav-brand"')
                self.assertContains(response, f'href="{reverse("scenarios")}"')
                self.assertNotContains(response, "peertrain-topnav-login")

    def test_registration_success_uses_the_global_4_5_second_toast(self):
        response = self.client.post(
            reverse("register"),
            {
                "full_name": "Toast Trainee",
                "email": "toast.trainee@example.com",
                "password1": "SecurePass123!",
                "password2": "SecurePass123!",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your trainee account for Toast Trainee has been created. Please sign in.")
        self.assertContains(response, "data-peertrain-success-toast")
        self.assertContains(response, 'data-bs-delay="4500"')

    def test_role_permissions_block_cross_access(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        admin = self._create_user("admin@example.com", User.Roles.ADMIN)

        self.client.force_login(trainee)
        trainee_response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(trainee_response.status_code, 403)

        self.client.logout()
        self.client.force_login(admin)
        admin_response = self.client.get(reverse("dashboard"))
        self.assertEqual(admin_response.status_code, 403)

    def test_trainee_dashboard_does_not_render_retired_onboarding_tutorial(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        self.client.force_login(trainee)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/static/images/peertrain-logo.svg", count=2)
        self.assertContains(response, 'class="peertrain-brand-icon"')
        self.assertNotContains(response, "How it works")
        self.assertNotContains(response, "peertrain-onboarding-tour")
        self.assertNotContains(response, "data-tour-target")

    def test_admin_dashboard_does_not_render_retired_onboarding_tutorial(self):
        admin = self._create_user("admin@example.com", User.Roles.ADMIN)
        self.client.force_login(admin)

        response = self.client.get(reverse("admin_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.content.decode(), f'href="{reverse("admin_users")}"\\s+class="peertrain-topnav-brand"')
        self.assertNotContains(response, "How it works")
        self.assertNotContains(response, "peertrain-onboarding-tour")
        self.assertNotContains(response, "data-tour-target")

    def test_retired_onboarding_completion_endpoint_returns_404(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        self.client.force_login(trainee)

        response = self.client.post("/onboarding/complete/")

        self.assertEqual(response.status_code, 404)
