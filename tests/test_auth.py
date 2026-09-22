import socket
from smtplib import SMTPAuthenticationError
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.tokens import default_token_generator
from django.core import mail, signing
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient, APITestCase

from apps.accounts.admin import SellFlowUserAdmin
from apps.accounts.models import User
from apps.core.models import Workspace
from tests.billing_fixtures import paid_workspace


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    REQUIRE_EMAIL_VERIFICATION=False,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AuthenticationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.workspace = paid_workspace(name="Auth tests")
        self.user = User.objects.create_user(
            username="secure-user",
            email="secure@test.example",
            password="SecureTesting!2026",
            workspace=self.workspace,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )

    def test_registration_ignores_privilege_and_tenant_injection(self):
        data = {
            "first_name": "New",
            "email": "new@test.example",
            "password": "NewSecurePass!2026",
            "workspace_name": "New store",
            "role": "manager",
            "is_superuser": True,
            "is_staff": True,
            "workspace": str(self.workspace.pk),
        }
        response = self.client.post("/api/auth/register/", data)
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(email=data["email"])
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.role, "owner")
        self.assertNotEqual(user.workspace_id, self.workspace.pk)
        self.assertTrue(user.check_password(data["password"]))
        self.assertEqual(user.dashboard_access_state, User.DASHBOARD_PENDING)
        self.assertTrue(response.data["approval_required"])
        self.assertIn("+923131471263", response.data["detail"])
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)
        self.assertEqual(len(mail.outbox), 1)

    def test_pending_account_can_login_for_billing_but_cannot_use_business_data(self):
        pending = User.objects.create_user(
            username="pending-user",
            email="pending@test.example",
            password="PendingTesting!2026",
            workspace=self.workspace,
        )
        login_result = self.client.post(
            "/api/auth/login/",
            {"email": pending.email, "password": "PendingTesting!2026"},
        )
        self.assertEqual(login_result.status_code, 200)
        self.assertFalse(login_result.data["has_dashboard_access"])
        self.assertEqual(self.client.get("/api/billing/checkout/").status_code, 200)
        self.assertEqual(self.client.get("/api/orders/").status_code, 423)

        self.assertEqual(
            self.client.post(
                "/api/auth/login/",
                {"email": self.user.email, "password": "SecureTesting!2026"},
            ).status_code,
            200,
        )
        self.user.dashboard_access_state = User.DASHBOARD_PENDING
        self.user.save(update_fields=["dashboard_access_state"])
        blocked = self.client.get("/api/orders/")
        self.assertEqual(blocked.status_code, 423)
        self.assertEqual(blocked.json()["code"], "dashboard_locked")
        status = self.client.get("/api/auth/me/")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.data["dashboard_access_state"], User.DASHBOARD_PENDING)

    def test_only_jazzmin_superuser_can_unlock_dashboard(self):
        pending = User.objects.create_user(
            username="pending-admin-test",
            email="pending-admin@test.example",
            password="PendingTesting!2026",
            workspace=self.workspace,
        )
        superuser = User.objects.create_superuser(
            username="control",
            email="control@test.example",
            password="ControlTesting!2026",
        )
        request = RequestFactory().post("/admin/accounts/user/")
        request.user = superuser
        admin_view = SellFlowUserAdmin(User, admin.site)
        self.assertTrue(admin_view.has_change_permission(request, pending))
        self.assertTrue(admin.site.has_permission(request))
        admin_view.unlock_dashboard(request, User.objects.filter(pk=pending.pk))
        pending.refresh_from_db()
        self.assertEqual(pending.dashboard_access_state, User.DASHBOARD_ACTIVE)
        self.assertEqual(pending.dashboard_unlocked_by, superuser)

        request.user = self.user
        self.assertFalse(admin_view.has_change_permission(request, pending))
        self.assertFalse(admin.site.has_permission(request))

    def test_email_case_insensitive_duplicates_and_weak_password(self):
        data = {
            "first_name": "New",
            "email": "SECURE@test.example",
            "password": "StrongNewPass!2026",
            "workspace_name": "New",
        }
        self.assertEqual(self.client.post("/api/auth/register/", data).status_code, 400)
        data.update(email="new@test.example", password="123")
        self.assertEqual(self.client.post("/api/auth/register/", data).status_code, 400)
        self.assertEqual(Workspace.objects.count(), 1)

    @override_settings(REQUIRE_EMAIL_VERIFICATION=True)
    def test_verification_blocks_login_resend_then_verify(self):
        credentials = {"email": self.user.email, "password": "SecureTesting!2026"}
        self.assertEqual(self.client.post("/api/auth/login/", credentials).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/auth/resend-verification/", {"email": self.user.email}
            ).status_code,
            200,
        )
        self.assertEqual(len(mail.outbox), 1)
        token = signing.dumps({"uid": self.user.pk, "email": self.user.email}, salt="verify-email")
        self.assertEqual(
            self.client.post("/api/auth/verify-email/", {"token": token}).status_code, 200
        )
        self.assertEqual(self.client.post("/api/auth/login/", credentials).status_code, 200)

    def test_recovery_does_not_disclose_unknown_accounts(self):
        known = self.client.post("/api/auth/forgot-password/", {"email": self.user.email})
        unknown = self.client.post("/api/auth/forgot-password/", {"email": "absent@test.example"})
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(len(mail.outbox), 1)

    @patch("apps.accounts.api.send_mail", side_effect=socket.gaierror(11003, "getaddrinfo failed"))
    def test_authenticated_resend_reports_dns_failure_without_crashing(self, send):
        self.client.force_authenticate(self.user)
        result = self.client.post("/api/auth/resend-verification/")
        self.assertEqual(result.status_code, 503)
        self.assertIn("try again", result.data["detail"])
        self.assertNotIn("getaddrinfo", result.data["detail"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)

    @patch("apps.accounts.api.send_mail", side_effect=SMTPAuthenticationError(535, b"rejected"))
    def test_anonymous_email_failures_do_not_disclose_account_existence(self, send):
        for route in ["resend-verification", "forgot-password"]:
            known = self.client.post(f"/api/auth/{route}/", {"email": self.user.email})
            unknown = self.client.post(f"/api/auth/{route}/", {"email": "absent@test.example"})
            self.assertEqual(known.status_code, 200)
            self.assertEqual(known.status_code, unknown.status_code)
            self.assertEqual(known.data, unknown.data)
            self.assertNotIn("has been sent", known.data["detail"])

    @patch("apps.accounts.api.send_mail", side_effect=TimeoutError("mail timed out"))
    def test_registration_preserves_pending_account_when_email_is_unavailable(self, send):
        result = self.client.post(
            "/api/auth/register/",
            {
                "first_name": "New",
                "email": "new@test.example",
                "password": "NewSecurePass!2026",
                "workspace_name": "New store",
            },
        )
        self.assertEqual(result.status_code, 201)
        self.assertFalse(result.data["verification_email_sent"])
        self.assertTrue(result.data["email_warning"])
        user = User.objects.get(email="new@test.example")
        self.assertEqual(user.dashboard_access_state, User.DASHBOARD_PENDING)
        self.assertFalse(user.email_verified)
        self.assertEqual(Workspace.objects.count(), 2)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    @patch("apps.accounts.api.send_mail", return_value=0)
    def test_resend_does_not_claim_delivery_when_backend_accepts_no_messages(self, send):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post("/api/auth/resend-verification/").status_code, 503)

    def test_reset_token_is_one_time(self):
        token = default_token_generator.make_token(self.user)
        payload = {"uid": self.user.pk, "token": token, "password": "ReplacementPass!2026"}
        self.assertEqual(self.client.post("/api/auth/reset-password/", payload).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/reset-password/", payload).status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(payload["password"]))

    def test_password_change_invalidates_other_sessions(self):
        other = APIClient()
        credentials = {"email": self.user.email, "password": "SecureTesting!2026"}
        self.client.post("/api/auth/login/", credentials)
        other.post("/api/auth/login/", credentials)
        self.assertEqual(other.get("/api/auth/me/").status_code, 200)
        response = self.client.post(
            "/api/auth/change-password/",
            {"current_password": credentials["password"], "password": "NewPassword!12345"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        self.assertEqual(other.get("/api/auth/me/").status_code, 403)

    def test_repeated_bad_logins_are_locked(self):
        for _ in range(8):
            self.assertEqual(
                self.client.post(
                    "/api/auth/login/", {"email": self.user.email, "password": "wrong"}
                ).status_code,
                400,
            )
        self.assertEqual(
            self.client.post(
                "/api/auth/login/", {"email": self.user.email, "password": "SecureTesting!2026"}
            ).status_code,
            429,
        )

    def test_profile_cannot_modify_security_fields(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            "/api/auth/me/",
            {"email_verified": True, "is_staff": True, "role": "manager", "first_name": "Updated"},
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertFalse(self.user.is_staff)
        self.assertEqual(self.user.role, "owner")
        self.assertEqual(self.user.first_name, "Updated")

    def test_unauthenticated_write_requires_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        for route in [
            "register",
            "login",
            "forgot-password",
            "reset-password",
            "verify-email",
            "resend-verification",
        ]:
            self.assertEqual(client.post(f"/api/auth/{route}/", {}).status_code, 403)
