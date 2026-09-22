from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.finance.models import BankAccount, BankEntry
from tests.billing_fixtures import paid_workspace


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PrintingTests(APITestCase):
    def setUp(self):
        self.ws = paid_workspace(name="Studio shop")
        self.user = User.objects.create_user(
            username="printer", workspace=self.ws, dashboard_access_state="ACTIVE"
        )
        self.client.force_authenticate(self.user)

    def test_brand_partial_updates_validate_design_and_do_not_overwrite_name(self):
        response = self.client.patch(
            "/api/workspace/", {"invoice_template": "royal", "business_phone": "+923001234567"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Studio shop")
        self.assertEqual(response.data["invoice_template"], "royal")
        self.assertEqual(
            self.client.patch("/api/workspace/", {"invoice_template": "not-real"}).status_code, 400
        )
        self.user.role = "manager"
        self.user.save()
        self.assertEqual(self.client.patch("/api/workspace/", {"name": "Changed"}).status_code, 403)

    def test_logo_is_reencoded_private_and_owner_managed(self):
        raw = BytesIO()
        Image.new("RGBA", (1200, 800), (40, 80, 120, 255)).save(raw, format="PNG")
        upload = SimpleUploadedFile("brand.png", raw.getvalue(), content_type="image/png")
        response = self.client.post("/api/workspace/logo/", {"logo": upload}, format="multipart")
        self.assertEqual(response.status_code, 200)
        result = self.client.get("/api/workspace/logo/")
        self.assertEqual(result["Content-Type"], "image/png")
        self.assertIn("no-store", result["Cache-Control"])
        with Image.open(BytesIO(result.content)) as logo:
            self.assertLessEqual(max(logo.size), 1024)
        self.user.workspace = paid_workspace(name="Another tenant")
        self.user.save()
        self.assertEqual(self.client.get("/api/workspace/logo/").status_code, 404)
        self.user.workspace = self.ws
        self.user.role = "staff"
        self.user.save()
        self.assertEqual(self.client.delete("/api/workspace/logo/").status_code, 403)
        self.user.role = "owner"
        self.user.save()
        self.assertEqual(self.client.delete("/api/workspace/logo/").status_code, 200)
        self.assertEqual(self.client.get("/api/workspace/logo/").status_code, 404)

    def test_invalid_logo_and_locked_access_are_rejected(self):
        for payload in [b"<svg onload='alert(1)'></svg>", b"x" * (2 * 1024 * 1024 + 1)]:
            response = self.client.post(
                "/api/workspace/logo/",
                {"logo": SimpleUploadedFile("fake.png", payload)},
                format="multipart",
            )
            self.assertEqual(response.status_code, 400)
        self.user.dashboard_access_state = "PENDING"
        self.user.save()
        self.assertEqual(self.client.get("/api/workspace/logo/").status_code, 423)

    def test_statement_balances_include_prior_cash_and_reversals_exactly(self):
        account = BankAccount.objects.create(
            workspace=self.ws, name="Bank", opening_balance="100.10", opening_date=date(2026, 1, 1)
        )

        def entry(amount, day, **extra):
            return BankEntry.objects.create(
                workspace=self.ws,
                account=account,
                actor=self.user,
                amount=amount,
                date=date(2026, 1, day),
                reference=str(uuid4()),
                request_key=uuid4(),
                **extra,
            )

        entry("25.20", 2)
        debit = entry("-10.15", 3)
        entry("10.15", 4, reversal_of=debit)
        entry("0.10", 4)
        entry("999.00", 6)
        path = f"/api/bank-accounts/{account.pk}/statement/"
        result = self.client.get(path, {"start_date": "2026-01-03", "end_date": "2026-01-05"})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(Decimal(result.data["opening_balance"]), Decimal("125.30"))
        self.assertEqual(Decimal(result.data["closing_balance"]), Decimal("125.40"))
        self.assertEqual(result.data["money_in"], "10.25")
        self.assertEqual(result.data["money_out"], "10.15")
        self.assertEqual(
            [r["balance"] for r in result.data["entries"]], ["115.15", "125.30", "125.40"]
        )
        self.assertIn("no-store", result["Cache-Control"])
        empty = self.client.get(path, {"start_date": "2026-01-05", "end_date": "2026-01-05"})
        self.assertEqual(empty.data["entries"], [])
        self.assertEqual(empty.data["opening_balance"], empty.data["closing_balance"])
        self.assertEqual(self.client.get(path, {"start_date": "bad"}).status_code, 400)
        self.assertEqual(self.client.get(path, {"start_date": "2025-01-01"}).status_code, 400)
        self.user.workspace = paid_workspace(name="Other")
        self.user.save()
        self.assertEqual(self.client.get(path).status_code, 404)
        self.user.workspace = self.ws
        self.user.role = "staff"
        self.user.save()
        self.assertEqual(self.client.get(path).status_code, 403)
