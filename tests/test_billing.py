import io
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.billing.models import Payment, PaymentBank, Plan, Subscription
from apps.billing.services import next_month, review_payment
from apps.core.models import Workspace


@override_settings(
    REQUIRE_EMAIL_VERIFICATION=False,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BillingTests(APITestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="Billing workspace")
        self.owner = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="testing-password",
            workspace=self.ws,
        )
        self.admin = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="testing-password"
        )
        self.ultra = Plan.objects.get(slug="ultra")
        self.ai = Plan.objects.get(slug="ultra-ai")
        self.bank = PaymentBank.objects.create(
            bank_name="Test Bank", account_title="Comqora", account_number="123456"
        )
        self.client.force_authenticate(self.owner)
        # Middleware uses the real session user.
        self.client.force_login(self.owner)

    def checkout(self, plan=None):
        response = self.client.post(
            "/api/billing/checkout/", {"plan": (plan or self.ultra).pk, "bank": self.bank.pk}
        )
        self.assertEqual(response.status_code, 201, response.data)
        return Payment.objects.get(pk=response.data["id"])

    def submit(self, payment):
        output = io.BytesIO()
        Image.new("RGB", (10, 10)).save(output, format="PNG")
        return self.client.post(
            f"/api/billing/payments/{payment.pk}/submit/",
            {
                "reference": "TX-123",
                "proof": SimpleUploadedFile(
                    "proof.png", output.getvalue(), content_type="image/png"
                ),
            },
            format="multipart",
        )

    def test_seeded_prices_and_public_catalog(self):
        self.client.logout()
        self.client.force_authenticate(None)
        response = self.client.get("/api/billing/plans/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ultra.monthly_price, 2425)
        self.assertEqual(self.ai.monthly_price, 4599)
        self.assertFalse(self.ultra.ai_enabled)
        self.assertTrue(self.ai.ai_enabled)

    def test_pending_login_can_checkout_but_not_access_business_data(self):
        self.client.logout()
        self.client.force_authenticate(None)
        response = self.client.post(
            "/api/auth/login/", {"email": self.owner.email, "password": "testing-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["has_dashboard_access"])
        self.assertEqual(self.client.get("/api/billing/checkout/").status_code, 200)
        self.assertEqual(self.client.get("/api/orders/").status_code, 423)

    def test_proof_does_not_unlock_until_approval_and_ultra_blocks_ai(self):
        payment = self.checkout()
        self.assertEqual(self.submit(payment).status_code, 200)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.has_dashboard_access)
        review_payment(payment.pk, self.admin, True)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.has_dashboard_access)
        self.assertFalse(self.owner.has_ai_access)
        self.assertEqual(self.client.get("/api/orders/").status_code, 200)
        self.assertEqual(self.client.get("/api/assistant/config/").status_code, 403)

    def test_ai_approval_and_expiry(self):
        payment = self.checkout(self.ai)
        self.submit(payment)
        review_payment(payment.pk, self.admin, True)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.has_ai_access)
        self.assertEqual(self.client.get("/api/assistant/config/").status_code, 200)
        Subscription.objects.filter(workspace=self.ws).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertFalse(self.owner.has_ai_access)
        self.assertEqual(self.client.get("/api/orders/").status_code, 423)
        self.assertEqual(self.client.get("/api/billing/checkout/").status_code, 200)

    def test_approval_is_once_only_and_renewal_extends_expiry(self):
        payment = self.checkout()
        self.submit(payment)
        review_payment(payment.pk, self.admin, True)
        expiry = Subscription.objects.get(workspace=self.ws).expires_at
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, self.admin, True)
        self.assertEqual(Subscription.objects.get(workspace=self.ws).expires_at, expiry)
        renewal = self.checkout()
        self.submit(renewal)
        review_payment(renewal.pk, self.admin, True)
        self.assertEqual(Subscription.objects.get(workspace=self.ws).expires_at, next_month(expiry))

    def test_rejection_requires_reason_and_allows_new_checkout(self):
        payment = self.checkout()
        self.submit(payment)
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, self.admin, False)
        Payment.objects.filter(pk=payment.pk).update(review_note="Transfer not received")
        review_payment(payment.pk, self.admin, False)
        self.assertFalse(Subscription.objects.filter(workspace=self.ws).exists())
        self.assertEqual(
            self.client.get("/api/billing/checkout/").data["payments"][0]["review_note"],
            "Transfer not received",
        )
        self.checkout()

    def test_quote_snapshots_and_duplicate_checkout(self):
        payment = self.checkout()
        self.bank.account_number = "changed"
        self.bank.save()
        self.ultra.monthly_price = 9999
        self.ultra.save()
        payment.refresh_from_db()
        self.assertEqual(payment.amount, 2425)
        self.assertEqual(payment.bank_details["account_number"], "123456")
        self.assertEqual(
            self.client.post(
                "/api/billing/checkout/", {"plan": self.ultra.pk, "bank": self.bank.pk}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(f"/api/billing/payments/{payment.pk}/cancel/").status_code, 200
        )
        self.assertEqual(self.checkout().amount, 9999)

    @override_settings(DEBUG=True)
    def test_private_proof_tenant_isolation(self):
        payment = self.checkout()
        self.submit(payment)
        url = f"/api/billing/payments/{payment.pk}/proof/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        stranger = User.objects.create_user(
            username="stranger",
            email="stranger@example.com",
            workspace=Workspace.objects.create(name="Other"),
        )
        self.client.force_authenticate(stranger)
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.submit(payment).status_code, 404)
        self.client.force_authenticate(self.admin)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(
            self.client.get(f"/admin/billing/payment/{payment.pk}/change/"), "Payment screenshot"
        )

    def test_staff_cannot_pay_or_approve(self):
        payment = self.checkout()
        self.submit(payment)
        self.owner.role = "staff"
        self.owner.save()
        self.assertEqual(self.client.get("/api/billing/checkout/").status_code, 403)
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, self.owner, True)

    def test_invalid_and_oversize_screenshot(self):
        payment = self.checkout()
        for content in [b"not an image", b"x" * (5 * 1024 * 1024 + 1)]:
            response = self.client.post(
                f"/api/billing/payments/{payment.pk}/submit/",
                {"reference": "TX", "proof": SimpleUploadedFile("fake.png", content)},
                format="multipart",
            )
            self.assertEqual(response.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "AWAITING_PROOF")

    def test_cannot_approve_without_proof_or_cancel_after_submission(self):
        payment = self.checkout()
        with self.assertRaises(ValidationError):
            review_payment(payment.pk, self.admin, True)
        self.submit(payment)
        self.assertEqual(self.submit(payment).status_code, 409)
        self.assertEqual(
            self.client.post(f"/api/billing/payments/{payment.pk}/cancel/").status_code, 409
        )

    def test_inactive_plans_and_banks_cannot_be_selected(self):
        self.bank.active = False
        self.bank.save()
        self.assertEqual(
            self.client.post(
                "/api/billing/checkout/", {"plan": self.ultra.pk, "bank": self.bank.pk}
            ).status_code,
            400,
        )
        self.bank.active = True
        self.bank.save()
        self.ultra.active = False
        self.ultra.save()
        self.assertEqual(
            self.client.post(
                "/api/billing/checkout/", {"plan": self.ultra.pk, "bank": self.bank.pk}
            ).status_code,
            400,
        )

    def test_calendar_month_end(self):
        value = datetime(2028, 1, 31, 12, tzinfo=dt_timezone.utc)
        self.assertEqual(next_month(value).day, 29)
        self.assertEqual(next_month(value).month, 2)

    def test_suspended_user_stays_suspended_on_payment_approval(self):
        self.owner.dashboard_access_state = "SUSPENDED"
        self.owner.save()
        payment = self.checkout(self.ai)
        self.submit(payment)
        review_payment(payment.pk, self.admin, True)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.has_dashboard_access)

    def test_stale_admin_note_save_cannot_reopen_payment(self):
        from apps.billing.admin import PaymentAdmin

        payment = self.checkout()
        self.submit(payment)
        stale = Payment.objects.get(pk=payment.pk)
        review_payment(payment.pk, self.admin, True)
        stale.review_note = "Old form"
        PaymentAdmin(Payment, admin.site).save_model(None, stale, None, True)
        stale.refresh_from_db()
        self.assertEqual(stale.status, "APPROVED")
        self.assertEqual(stale.review_note, "")

    def test_billing_models_registered_in_jazzmin(self):
        for model in [Plan, PaymentBank, Subscription, Payment]:
            self.assertIn(model, admin.site._registry)

    @override_settings(DEBUG=True)
    def test_admin_detail_approves_payment_and_blocks_replay(self):
        payment = self.checkout()
        self.submit(payment)
        self.client.force_login(self.admin)
        url = f"/admin/billing/payment/{payment.pk}/change/"
        self.assertContains(self.client.get(url), 'name="_approve_payment"')
        self.assertContains(self.client.get("/admin/billing/payment/"), "Review payment")
        response = self.client.post(
            url, {"review_note": "Received in bank", "_approve_payment": "1"}, format="multipart"
        )
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "APPROVED")
        self.assertEqual(payment.review_note, "Received in bank")
        self.assertEqual(payment.reviewed_by, self.admin)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.has_dashboard_access)
        expiry = Subscription.objects.get(workspace=self.ws).expires_at
        self.assertNotContains(self.client.get(url), 'name="_approve_payment"')
        self.client.post(url, {"_approve_payment": "1"}, format="multipart")
        self.assertEqual(Subscription.objects.get(workspace=self.ws).expires_at, expiry)

    @override_settings(DEBUG=True)
    def test_admin_detail_rejects_with_note_in_one_step(self):
        payment = self.checkout()
        self.submit(payment)
        self.client.force_login(self.admin)
        url = f"/admin/billing/payment/{payment.pk}/change/"
        self.client.post(url, {"review_note": "", "_reject_payment": "1"}, format="multipart")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "PENDING")
        response = self.client.post(
            url,
            {"review_note": "Transfer not received", "_reject_payment": "1"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "REJECTED")
        self.assertEqual(payment.review_note, "Transfer not received")
        self.assertFalse(Subscription.objects.filter(workspace=self.ws).exists())

    @override_settings(DEBUG=True)
    def test_admin_detail_waits_for_proof_and_requires_superuser(self):
        payment = self.checkout()
        url = f"/admin/billing/payment/{payment.pk}/change/"
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertNotContains(response, 'name="_approve_payment"')
        self.assertContains(response, "Waiting for the customer")
        self.client.post(url, {"_approve_payment": "1"}, format="multipart")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "AWAITING_PROOF")
        self.owner.is_staff = True
        self.owner.save()
        self.client.force_login(self.owner)
        self.assertEqual(
            self.client.post(url, {"_approve_payment": "1"}, format="multipart").status_code, 302
        )
        self.assertFalse(Subscription.objects.filter(workspace=self.ws).exists())
