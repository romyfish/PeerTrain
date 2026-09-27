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
        self.assertContains(landing_response, "/static/images/peertrain-logo.svg", count=1)
        self.assertNotContains(landing_response, 'class="peertrain-brand-icon"')

        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, "/static/images/peertrain-logo.svg", count=2)
        self.assertContains(login_response, 'class="peertrain-brand-icon"')

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

    def test_login_redirects_by_role(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        admin = self._create_user("admin@example.com", User.Roles.ADMIN)

        trainee_response = self.client.post(
            reverse("login"),
            {"email": trainee.email, "password": "SecurePass123!"},
        )
        self.assertRedirects(trainee_response, reverse("dashboard"))

        self.client.logout()

        admin_response = self.client.post(
            reverse("login"),
            {"email": admin.email, "password": "SecurePass123!"},
        )
        self.assertRedirects(admin_response, reverse("admin_dashboard"))

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
        self.assertNotContains(response, "How it works")
        self.assertNotContains(response, "peertrain-onboarding-tour")
        self.assertNotContains(response, "data-tour-target")

    def test_retired_onboarding_completion_endpoint_returns_404(self):
        trainee = self._create_user("trainee@example.com", User.Roles.TRAINEE)
        self.client.force_login(trainee)

        response = self.client.post("/onboarding/complete/")

        self.assertEqual(response.status_code, 404)
