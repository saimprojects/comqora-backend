import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal as D

from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.catalog.models import Packaging, Product, StockBatch
from apps.finance.models import Expense
from apps.logistics.models import Courier
from apps.marketing.models import AdAllocation, Campaign
from apps.orders.models import Customer, Order, StockAllocation, TrackingEvent
from apps.orders.services import (
    apply_tracking,
    cancel_order,
    create_order,
    dispatch_order,
    financials,
    receive_return,
)
from tests.billing_fixtures import paid_workspace


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    REQUIRE_EMAIL_VERIFICATION=False,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class BusinessTests(APITestCase):
    def setUp(self):
        self.workspace = paid_workspace(name="Test store")
        self.user = User.objects.create_user(
            username="test",
            email="owner@test.example",
            password="StrongTest!2026",
            workspace=self.workspace,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(self.user)
        self.product = Product.objects.create(
            workspace=self.workspace, name="Headphones", sku="HP1", selling_price=D("2000")
        )
        self.batch = StockBatch.objects.create(
            workspace=self.workspace,
            product=self.product,
            reference="FIRST",
            purchased_quantity=3,
            remaining_quantity=3,
            unit_cost=D("850"),
            received_at=timezone.localdate() - timedelta(days=3),
        )
        self.batch2 = StockBatch.objects.create(
            workspace=self.workspace,
            product=self.product,
            reference="SECOND",
            purchased_quantity=10,
            remaining_quantity=10,
            unit_cost=D("950"),
            received_at=timezone.localdate() - timedelta(days=1),
        )
        self.courier = Courier.objects.create(
            workspace=self.workspace,
            name="Courier",
            code="courier",
            base_rate=D("290"),
            return_rate=D("180"),
        )
        self.pack = Packaging.objects.create(
            workspace=self.workspace, name="Flyer", unit_cost=D("35"), stock=D("20")
        )
        self.customer = Customer.objects.create(
            workspace=self.workspace,
            name="Test Customer",
            phone="03000000000",
            city="Lahore",
            address="Test address",
        )

    def order(self, quantity=1, **overrides):
        data = {
            "customer": self.customer,
            "courier": self.courier.pk,
            "items": [{"product": self.product.pk, "quantity": quantity}],
            "weight": D("1"),
            "ad_cost": D("280"),
            "packaging": [{"id": self.pack.pk, "quantity": D("1")}],
        }
        data.update(overrides)
        return create_order(self.workspace, data)

    def delivered(self):
        order = self.order()
        dispatch_order(order.pk, self.workspace, "TRACK-1")
        return apply_tracking(order.pk, self.workspace, "DELIVERED", "delivered-1")

    def returned(self, **overrides):
        order = self.order(**overrides)
        dispatch_order(order.pk, self.workspace, "TRACK-RETURN")
        return apply_tracking(order.pk, self.workspace, "RETURNED", "returned-1")

    def test_product_create_returns_stock_totals_and_preserves_image(self):
        payload = {
            "name": "New product",
            "sku": "NEW-001",
            "selling_price": "1200.00",
            "image_url": "https://example.com/product.webp",
            "stock": 999,
            "reserved": 999,
            "available": 999,
        }
        response = self.client.post("/api/products/", payload)
        self.assertEqual(response.status_code, 201, response.data)
        for field in ("stock", "reserved", "available"):
            self.assertEqual(response.data[field], 0)
        product = Product.objects.get(pk=response.data["id"])
        self.assertEqual(product.workspace_id, self.workspace.pk)
        self.assertEqual(product.image_url, payload["image_url"])
        self.assertEqual(Product.objects.filter(sku="NEW-001").count(), 1)
        detail = self.client.get(f"/api/products/{product.pk}/")
        self.assertEqual(detail.data, response.data)
        duplicate = self.client.post("/api/products/", payload)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(Product.objects.filter(sku="NEW-001").count(), 1)

    def test_product_update_preserves_annotated_stock_and_reservations(self):
        self.order(quantity=5)
        response = self.client.patch(
            f"/api/products/{self.product.pk}/", {"name": "Updated headphones", "stock": 999}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "Updated headphones")
        self.assertEqual(response.data["stock"], 13)
        self.assertEqual(response.data["reserved"], 5)
        self.assertEqual(response.data["available"], 8)

    def test_fifo_spans_batches_and_reserves_without_consuming(self):
        order = self.order(quantity=5)
        self.assertEqual(order.product_cost, D("4450"))
        self.batch.refresh_from_db()
        self.batch2.refresh_from_db()
        self.assertEqual((self.batch.remaining_quantity, self.batch.reserved_quantity), (3, 3))
        self.assertEqual(self.batch2.reserved_quantity, 2)
        self.assertEqual(order.items.first().allocations.count(), 2)

    def test_insufficient_stock_rolls_back_everything(self):
        with self.assertRaises(ValidationError):
            self.order(quantity=14)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(StockAllocation.objects.count(), 0)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.reserved_quantity, 0)

    def test_reservations_prevent_overselling(self):
        self.order(quantity=12)
        with self.assertRaises(ValidationError):
            self.order(quantity=2)
        self.assertEqual(Order.objects.count(), 1)

    def test_dispatch_is_atomic_and_cannot_be_repeated(self):
        order = self.order()
        dispatch_order(order.pk, self.workspace, "TRACK")
        self.batch.refresh_from_db()
        self.pack.refresh_from_db()
        self.assertEqual((self.batch.remaining_quantity, self.batch.reserved_quantity), (2, 0))
        self.assertEqual(self.pack.stock, D("19"))
        with self.assertRaises(ValidationError):
            dispatch_order(order.pk, self.workspace, "TRACK")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 2)

    def test_packaging_shortage_rolls_back_stock_dispatch(self):
        order = self.order()
        self.pack.stock = 0
        self.pack.save()
        with self.assertRaises(ValidationError):
            dispatch_order(order.pk, self.workspace, "TRACK")
        self.batch.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 3)
        self.assertEqual(self.batch.reserved_quantity, 1)
        self.assertEqual(order.status, "CREATED")

    def test_delivered_profit(self):
        order = self.delivered()
        f = financials(order)
        self.assertEqual(D(f["profit"]), D("545"))
        self.assertEqual(D(f["revenue"]), D("2000"))
        self.assertEqual(f["state"], "REALIZED")

    def test_reusable_return_does_not_write_off_product(self):
        order = self.returned()
        self.assertFalse(financials(order)["is_final"])
        order = receive_return(order.pk, self.workspace, {})
        self.assertEqual(D(financials(order)["profit"]), D("-785"))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 3)
        with self.assertRaises(ValidationError):
            receive_return(order.pk, self.workspace, {})

    def test_damaged_return_writes_off_product(self):
        order = self.returned()
        order = receive_return(order.pk, self.workspace, {str(order.items.first().pk): 1})
        self.assertEqual(D(financials(order)["profit"]), D("-1635"))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 2)

    def test_partial_advance_is_retained_until_refunded(self):
        order = self.returned(payment_type="PARTIAL", advance_paid=D("500"))
        order = receive_return(order.pk, self.workspace, {})
        self.assertEqual(D(financials(order)["profit"]), D("-285"))
        result = self.client.post(f"/api/orders/{order.pk}/refund/", {"amount": "500"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(D(result.data["financials"]["profit"]), D("-785"))
        self.assertEqual(
            self.client.post(f"/api/orders/{order.pk}/refund/", {"amount": "1"}).status_code, 400
        )

    def test_cancel_releases_stock_and_only_incurred_costs_remain(self):
        order = cancel_order(self.order().pk, self.workspace)
        self.assertEqual(D(financials(order)["profit"]), D("-280"))
        self.batch.refresh_from_db()
        self.pack.refresh_from_db()
        self.assertEqual(self.batch.reserved_quantity, 0)
        self.assertEqual(self.pack.stock, D("20"))

    def test_delivery_failed_stays_estimated(self):
        order = self.order()
        dispatch_order(order.pk, self.workspace, "TRACK")
        order = apply_tracking(order.pk, self.workspace, "DELIVERY_FAILED", "failed-1")
        self.assertEqual(financials(order)["state"], "ESTIMATED")
        self.assertEqual(D(financials(order)["revenue"]), 0)

    def test_snapshot_survives_setting_changes(self):
        order = self.order()
        self.courier.base_rate = 999
        self.courier.save()
        self.pack.unit_cost = 999
        self.pack.save()
        self.product.selling_price = 9999
        self.product.save()
        order.refresh_from_db()
        self.assertEqual(order.courier_cost, D("290"))
        self.assertEqual(order.packaging_cost, D("35"))
        self.assertEqual(order.subtotal, D("2000"))

    def test_payment_discount_validation_is_atomic(self):
        for data in [
            {"discount": D("2001")},
            {"payment_type": "COD", "advance_paid": D("1")},
            {"payment_type": "PREPAID", "advance_paid": D("100")},
        ]:
            with self.assertRaises(ValidationError):
                self.order(**data)
        self.assertEqual(Order.objects.count(), 0)

    def test_tracking_replays_are_idempotent_and_final_state_immutable(self):
        order = self.delivered()
        apply_tracking(order.pk, self.workspace, "DELIVERED", "delivered-1")
        self.assertEqual(TrackingEvent.objects.filter(provider_event_id="delivered-1").count(), 1)
        with self.assertRaises(ValidationError):
            apply_tracking(order.pk, self.workspace, "IN_TRANSIT", "stale-event")

    def test_workspace_isolation_and_viewer_permissions(self):
        other = paid_workspace(name="Other")
        stranger = User.objects.create_user(
            username="stranger",
            email="stranger@test.example",
            password="Test",
            workspace=other,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        order = self.order()
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.get("/api/orders/").data["count"], 0)
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}/").status_code, 404)
        payload = {
            "customer": str(self.customer.pk),
            "courier": str(self.courier.pk),
            "items": [{"product": str(self.product.pk), "quantity": 1}],
        }
        self.assertEqual(self.client.post("/api/orders/", payload).status_code, 400)
        self.user.role = "viewer"
        self.user.save()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post("/api/customers/", {}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/orders/{order.pk}/cancel/", {}).status_code, 403)
        self.assertEqual(self.client.get("/api/orders/").status_code, 200)

    def test_staff_cannot_manage_cost_configuration_or_team(self):
        self.user.role = "staff"
        self.user.save()
        self.assertEqual(self.client.post("/api/couriers/", {}).status_code, 403)
        self.assertEqual(self.client.post("/api/expenses/", {}).status_code, 403)
        self.assertEqual(self.client.get("/api/team/").status_code, 403)

    def test_api_create_with_defaults_and_fractional_stock_rejected(self):
        payload = {
            "customer": str(self.customer.pk),
            "courier": str(self.courier.pk),
            "items": [{"product": str(self.product.pk), "quantity": 1}],
        }
        response = self.client.post("/api/orders/", payload)
        self.assertEqual(response.status_code, 201, response.data)
        payload["items"][0]["quantity"] = 1.2
        self.assertEqual(self.client.post("/api/orders/", payload).status_code, 400)

    def test_stock_receipt_landed_cost(self):
        data = {
            "product": str(self.product.pk),
            "reference": "NEW",
            "purchased_quantity": 10,
            "unit_cost": "100",
            "transport_cost": "150",
            "import_cost": "50",
            "received_at": str(timezone.localdate()),
        }
        response = self.client.post("/api/stock-batches/", data)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(D(response.data["unit_cost"]), D("120"))
        self.assertEqual(response.data["remaining_quantity"], 10)

    def test_campaign_cents_skip_existing_add_and_undo(self):
        orders = [self.order(ad_cost=D("0")) for _ in range(3)]
        existing = self.order(ad_cost=D("50"))
        campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Test ads",
            spend=D("100"),
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
        )
        result = self.client.post(
            f"/api/campaigns/{campaign.pk}/allocate/", {"mode": "skip_existing"}
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(
            sum(a.amount for a in AdAllocation.objects.filter(campaign=campaign, active=True)),
            D("100"),
        )
        existing.refresh_from_db()
        self.assertEqual(existing.ad_cost, D("50"))
        self.assertEqual(
            self.client.post(f"/api/campaigns/{campaign.pk}/allocate/", {}).status_code, 400
        )
        self.client.post(f"/api/campaigns/{campaign.pk}/undo/", {})
        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.ad_cost, D("0"))
        self.assertEqual(AdAllocation.objects.filter(campaign=campaign, active=False).count(), 3)
        self.client.post(f"/api/campaigns/{campaign.pk}/allocate/", {"mode": "add"})
        existing.refresh_from_db()
        self.assertEqual(existing.ad_cost, D("75"))

    def test_analytics_excludes_estimates_and_counts_expenses(self):
        self.delivered()
        self.order()
        Expense.objects.create(
            workspace=self.workspace,
            name="Software",
            category="software",
            amount=100,
            date=timezone.localdate(),
        )
        response = self.client.get("/api/analytics/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["totals"]["revenue"], D("2000"))
        self.assertEqual(response.data["totals"]["net_profit"], D("165"))
        self.assertEqual(response.data["totals"]["expected_profit"], D("545"))

    def test_export_escapes_spreadsheet_formulas(self):
        self.customer.name = "=1+1"
        self.customer.save()
        self.order()
        response = self.client.get("/api/orders/export/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("'=1+1", response.content.decode())

    @override_settings(TRACKING_WEBHOOK_SECRET="test-signing-secret")
    def test_webhook_requires_signature_and_timestamp(self):
        order = self.order()
        dispatch_order(order.pk, self.workspace, "TRACK-SIGNED")
        body = json.dumps(
            {
                "order_id": str(order.pk),
                "workspace_id": str(self.workspace.pk),
                "status": "DELIVERED",
                "event_id": "signed-1",
            }
        )
        client = APIClient()
        url = "/api/integrations/tracking/webhook/"
        self.assertEqual(client.post(url, body, content_type="application/json").status_code, 401)
        timestamp = str(int(time.time()))
        signature = hmac.new(
            b"test-signing-secret", timestamp.encode() + b"." + body.encode(), hashlib.sha256
        ).hexdigest()
        result = client.post(
            url,
            body,
            content_type="application/json",
            HTTP_X_SELLFLOW_TIMESTAMP=timestamp,
            HTTP_X_SELLFLOW_SIGNATURE=signature,
        )
        self.assertEqual(result.status_code, 200, result.data)
        order.refresh_from_db()
        self.assertEqual(order.status, "DELIVERED")

    def test_login_csrf_logout_and_recovery(self):
        from django.core import mail

        client = APIClient(enforce_csrf_checks=True)
        credentials = {"email": self.user.email, "password": "StrongTest!2026"}
        self.assertEqual(client.post("/api/auth/login/", credentials).status_code, 403)
        token = client.get("/api/auth/csrf/").data["csrfToken"]
        self.assertEqual(
            client.post("/api/auth/login/", credentials, HTTP_X_CSRFTOKEN=token).status_code, 200
        )
        self.assertTrue(client.cookies["sessionid"]["httponly"])
        self.assertEqual(client.get("/api/auth/me/").status_code, 200)
        token = client.get("/api/auth/csrf/").data["csrfToken"]
        self.assertEqual(
            client.post("/api/auth/logout/", {}, HTTP_X_CSRFTOKEN=token).status_code, 200
        )
        self.assertEqual(client.get("/api/orders/").status_code, 403)
        token = client.get("/api/auth/csrf/").data["csrfToken"]
        result = client.post(
            "/api/auth/forgot-password/", {"email": self.user.email}, HTTP_X_CSRFTOKEN=token
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_invalid_return_quantities_do_not_restock(self):
        order = self.returned()
        with self.assertRaises(ValidationError):
            receive_return(order.pk, self.workspace, {str(order.items.first().pk): 2})
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 2)

    def test_pending_return_estimate_is_not_a_successful_sale(self):
        order = self.returned()
        self.assertFalse(financials(order)["is_final"])
        self.assertEqual(D(financials(order)["expected_profit"]), D("-785"))

    def test_reused_event_id_cannot_target_another_order(self):
        self.delivered()
        second = self.order()
        dispatch_order(second.pk, self.workspace, "SECOND")
        with self.assertRaises(ValidationError):
            apply_tracking(second.pk, self.workspace, "DELIVERED", "delivered-1")

    def test_mixed_product_profit_uses_each_items_own_fifo_cost(self):
        cheap = Product.objects.create(
            workspace=self.workspace, name="High margin", sku="CHEAP", selling_price=D("2000")
        )
        StockBatch.objects.create(
            workspace=self.workspace,
            product=cheap,
            reference="CHEAP",
            purchased_quantity=5,
            remaining_quantity=5,
            unit_cost=D("100"),
            received_at=timezone.localdate(),
        )
        order = self.order(
            items=[
                {"product": self.product.pk, "quantity": 1},
                {"product": cheap.pk, "quantity": 1},
            ]
        )
        dispatch_order(order.pk, self.workspace, "MIXED")
        order = apply_tracking(order.pk, self.workspace, "DELIVERED", "mixed-delivered")
        response = self.client.get("/api/analytics/")
        products = {p["sku"]: p for p in response.data["products"]}
        self.assertEqual(products["CHEAP"]["profit"] - products["HP1"]["profit"], D("750"))
        self.assertEqual(
            sum(p["profit"] for p in products.values()), D(financials(order)["profit"])
        )

    def test_damaged_product_totals_reconcile_to_order_result(self):
        order = self.returned()
        receive_return(order.pk, self.workspace, {str(order.items.first().pk): 1})
        response = self.client.get("/api/analytics/")
        self.assertEqual(
            sum(p["profit"] for p in response.data["products"]),
            response.data["totals"]["realized_profit"],
        )
