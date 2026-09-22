from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Category, Packaging, Product, StockBatch
from apps.core.models import AuditEvent
from apps.logistics.models import Courier
from apps.logistics.providers import Checkpoint
from apps.logistics.tracking import apply_checkpoints, sync_order
from apps.marketing.models import Campaign
from apps.orders.models import Customer, Order
from apps.orders.services import dispatch_order, financials, quote
from tests.billing_fixtures import paid_workspace


@override_settings(
    REQUIRE_EMAIL_VERIFICATION=False,
    TRACKING_ENABLED=True,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class WorkflowTests(APITestCase):
    def setUp(self):
        self.workspace = paid_workspace(name="Workflow tests")
        self.user = User.objects.create_user(
            username="workflow",
            email="workflow@example.test",
            password="TestOnly!12345",
            workspace=self.workspace,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(self.user)
        self.category = Category.objects.create(workspace=self.workspace, name="Electronics")
        self.product = Product.objects.create(
            workspace=self.workspace,
            category_record=self.category,
            category=self.category.name,
            name="Test product",
            sku="T1",
            selling_price=1000,
        )
        self.batch = StockBatch.objects.create(
            workspace=self.workspace,
            product=self.product,
            reference="T1",
            purchased_quantity=100,
            remaining_quantity=100,
            unit_cost=400,
            received_at=timezone.localdate(),
        )
        self.customer = Customer.objects.create(
            workspace=self.workspace,
            name="Test buyer",
            phone="03001234567",
            city="Kasur",
            province="Punjab",
            address="Test address",
        )
        self.courier = Courier.objects.create(
            workspace=self.workspace,
            name="TCS contract",
            provider="TCS",
            code="tcs",
            base_rate=200,
            additional_kg_rate=50,
            return_rate=100,
        )
        self.pack = Packaging.objects.create(
            workspace=self.workspace, name="Flyer", unit_cost=20, stock=100
        )

    def create_order(self, **changes):
        payload = {
            "customer": str(self.customer.pk),
            "courier": str(self.courier.pk),
            "items": [{"product": str(self.product.pk), "quantity": 1}],
            "packaging": [{"id": str(self.pack.pk), "quantity": "1"}],
        }
        payload.update(changes)
        response = self.client.post("/api/orders/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return Order.objects.get(pk=response.data["id"])

    def dispatched(self):
        order = self.create_order()
        return dispatch_order(order.pk, self.workspace, "TEST-TRACKING")

    def receipt(self, **changes):
        payload = {
            "product": str(self.product.pk),
            "reference": "RECEIPT",
            "purchased_quantity": 10,
            "purchase_amount": "1000",
            "purchase_mode": "TOTAL",
            "transport_cost": "100",
            "import_cost": "50",
            "extra_costs": [
                {"name": "Handling", "amount": "30"},
                {"name": "Insurance", "amount": "20"},
            ],
            "received_at": str(timezone.localdate()),
        }
        payload.update(changes)
        return self.client.post("/api/stock-batches/", payload, format="json")

    def manual(self, order, status="DELIVERY_FAILED"):
        return self.client.post(
            f"/api/orders/{order.pk}/manual-status/",
            {"status": status, "message": "Confirmed by operations team"},
            format="json",
        )

    def test_defaults_half_kg_and_absorbed_costs(self):
        self.assertEqual(self.courier.base_weight, D(".5"))
        order = self.create_order()
        self.assertEqual(order.weight, D(".5"))
        self.assertEqual(order.charges_mode, "ABSORB")
        self.assertEqual(order.customer_charges, 0)
        self.assertEqual(D(financials(order)["net_sale"]), 1000)
        self.assertEqual(D(financials(order)["expected_profit"]), 380)

    def test_added_costs_are_charged_once_and_profit_is_consistent(self):
        order = self.create_order(
            charges_mode="ADD", ad_cost="30", other_costs=[{"name": "Handling", "amount": "10"}]
        )
        self.assertEqual(order.customer_charges, 260)
        self.assertEqual(D(financials(order)["net_sale"]), 1260)
        self.assertEqual(D(financials(order)["expected_profit"]), 600)
        dispatch_order(order.pk, self.workspace, "TEST")
        self.assertEqual(self.manual(order, "DELIVERED").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(D(financials(order)["profit"]), 600)

    def test_prepaid_add_charges_and_full_refund_limit(self):
        order = self.create_order(charges_mode="ADD", payment_type="PREPAID", advance_paid="1220")
        dispatch_order(order.pk, self.workspace, "TEST")
        self.manual(order, "DELIVERED")
        response = self.client.post(f"/api/orders/{order.pk}/refund/", {"amount": "1220"})
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.refunded_amount, 1220)

    def test_later_marketing_cost_never_changes_customer_bill(self):
        order = self.create_order(charges_mode="ADD")
        campaign = Campaign.objects.create(
            workspace=self.workspace,
            name="Later campaign",
            channel="Meta",
            spend=100,
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
        )
        response = self.client.post(f"/api/campaigns/{campaign.pk}/allocate/", {"mode": "add"})
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.customer_charges, 220)
        self.assertEqual(D(financials(order)["net_sale"]), 1220)
        self.assertEqual(D(financials(order)["expected_profit"]), 500)

    def test_total_receipt_allocates_all_costs_and_preserves_inputs(self):
        response = self.receipt()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(D(response.data["unit_cost"]), 120)
        self.assertEqual(D(response.data["purchase_amount"]), 1000)
        self.assertEqual(len(response.data["extra_costs"]), 2)
        self.assertEqual(D(response.data["transport_cost"]), 100)

    def test_per_unit_receipt_and_rounding(self):
        response = self.receipt(
            purchase_mode="UNIT",
            purchase_amount="33.33",
            purchased_quantity=3,
            transport_cost="0",
            import_cost="0",
            extra_costs=[{"name": "Fee", "amount": "1"}],
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(D(response.data["unit_cost"]), D("33.66"))

    def test_receipt_rejects_negative_cost_and_zero_units(self):
        self.assertEqual(
            self.receipt(extra_costs=[{"name": "Fee", "amount": "-1"}]).status_code, 400
        )
        self.assertEqual(self.receipt(purchased_quantity=0).status_code, 400)
        self.assertEqual(self.receipt(extra_costs=[{"name": "", "amount": "1"}]).status_code, 400)

    def test_category_creation_selection_filter_and_rename(self):
        category = self.client.post("/api/categories/", {"name": "Accessories"})
        self.assertEqual(category.status_code, 201, category.data)
        product = self.client.post(
            "/api/products/",
            {
                "name": "Cable",
                "sku": "CABLE",
                "selling_price": "200",
                "category_id": category.data["id"],
            },
        )
        self.assertEqual(product.status_code, 201, product.data)
        self.assertEqual(product.data["category"], "Accessories")
        self.assertNotIn("variant", product.data)
        filtered = self.client.get("/api/products/", {"category_record": category.data["id"]})
        self.assertEqual(filtered.data["count"], 1)
        self.assertEqual(
            self.client.patch(
                f"/api/categories/{category.data['id']}/", {"name": "Cables"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/api/products/{product.data['id']}/").data["category"], "Cables"
        )

    def test_category_case_insensitive_unique_and_tenant_boundaries(self):
        self.assertEqual(
            self.client.post("/api/categories/", {"name": "electronics"}).status_code, 400
        )
        other = paid_workspace(name="Other")
        category = Category.objects.create(workspace=other, name="Private")
        response = self.client.post(
            "/api/products/",
            {
                "name": "Leak",
                "sku": "LEAK",
                "selling_price": "100",
                "category_id": str(category.pk),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(f"/api/categories/{category.pk}/").status_code, 404)

    def test_courier_region_precedence_and_non_compounding_fees(self):
        self.courier.provincial_pricing = True
        self.courier.same_province_rate = D(150)
        self.courier.outside_province_rate = D(300)
        self.courier.city_pricing = True
        self.courier.same_city_rate = D(100)
        self.courier.tax_percent = D(10)
        self.courier.fixed_charge = D(5)
        self.courier.extra_fees = [
            {"name": "Fuel", "kind": "PERCENT", "amount": "10"},
            {"name": "Remote", "kind": "FIXED", "amount": "15"},
        ]
        self.courier.save()
        self.assertEqual(D(quote(self.courier, ".5", "SAME_CITY")["total"]), 140)
        self.assertEqual(D(quote(self.courier, ".5", "SAME_PROVINCE")["total"]), 200)
        self.assertEqual(D(quote(self.courier, ".5", "OUTSIDE_PROVINCE")["total"]), 380)
        self.assertEqual(D(quote(self.courier, ".51", "SAME_CITY")["total"]), 200)

    def test_courier_rates_and_fees_are_snapshotted(self):
        self.courier.city_pricing = True
        self.courier.same_city_rate = D(100)
        self.courier.extra_fees = [{"name": "Fuel", "kind": "PERCENT", "amount": "10"}]
        self.courier.save()
        order = self.create_order(delivery_zone="SAME_CITY")
        self.courier.same_city_rate = D(900)
        self.courier.save()
        order.refresh_from_db()
        self.assertEqual(order.courier_cost, 110)
        self.assertEqual(order.courier_snapshot["extra_fees"][0]["cost"], "10.00")

    def test_courier_invalid_fees_region_and_required_rates(self):
        url = f"/api/couriers/{self.courier.pk}/"
        for fee in [
            {"name": "Fuel", "kind": "PERCENT", "amount": "101"},
            {"name": "Fuel", "kind": "FIXED", "amount": "-1"},
            {"name": "Fuel", "kind": "UNKNOWN", "amount": "1"},
        ]:
            self.assertEqual(
                self.client.patch(url, {"extra_fees": [fee]}, format="json").status_code, 400
            )
        self.assertEqual(
            self.client.patch(url, {"provincial_pricing": True}, format="json").status_code, 400
        )
        self.assertEqual(self.client.get(url + "quote/", {"zone": "INVALID"}).status_code, 400)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_manual_pauses_worker_and_records_audit(self, fetch):
        order = self.dispatched()
        self.assertEqual(self.manual(order).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.tracking_mode, "MANUAL")
        self.assertIsNone(order.tracking_next_sync_at)
        self.assertEqual(sync_order(order.pk)["state"], "idle")
        fetch.assert_not_called()
        self.assertTrue(order.tracking_events.filter(source="manual").exists())
        self.assertTrue(
            AuditEvent.objects.filter(action="order.manual_status", actor=self.user).exists()
        )

    def test_inflight_tracking_cannot_overwrite_manual_update(self):
        order = self.dispatched()
        lease = uuid4()
        Order.objects.filter(pk=order.pk).update(tracking_lock_token=lease)
        self.manual(order)
        applied = apply_checkpoints(
            order.pk,
            "run_courier",
            [Checkpoint(text="Delivered", occurred_at=timezone.now())],
            lease,
        )
        self.assertFalse(applied)
        order.refresh_from_db()
        self.assertEqual(order.status, "DELIVERY_FAILED")

    def test_resume_auto_preserves_newer_manual_status(self):
        order = self.dispatched()
        self.manual(order)
        response = self.client.post(f"/api/orders/{order.pk}/resume-tracking/")
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.tracking_mode, "AUTO")
        lease = uuid4()
        Order.objects.filter(pk=order.pk).update(tracking_lock_token=lease)
        apply_checkpoints(
            order.pk,
            "run_courier",
            [Checkpoint(text="Delivered", occurred_at=timezone.now() - timedelta(days=1))],
            lease,
        )
        order.refresh_from_db()
        self.assertEqual(order.status, "DELIVERY_FAILED")

    def test_manual_terminal_cannot_regress_and_returns_need_inspection(self):
        order = self.dispatched()
        self.assertEqual(self.manual(order, "RETURNED").status_code, 200)
        self.assertEqual(self.manual(order, "DELIVERED").status_code, 400)
        order.refresh_from_db()
        self.assertIsNone(order.return_received_at)
        self.assertFalse(financials(order)["is_final"])
        self.assertEqual(order.return_cost, 100)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.remaining_quantity, 99)

    def test_new_shipment_states_allow_manual_updates_and_resume(self):
        order = self.dispatched()
        for status in ("OUT_FOR_DELIVERY", "RETURN_IN_TRANSIT"):
            with self.subTest(status=status):
                self.assertEqual(self.manual(order, status).status_code, 200)
                order.refresh_from_db()
                self.assertEqual(order.status, status)
                self.assertFalse(financials(order)["is_final"])
                self.assertEqual(order.return_cost, 0)
                self.assertEqual(
                    self.client.post(f"/api/orders/{order.pk}/resume-tracking/").status_code, 200
                )

    def test_manual_requires_reason_dispatch_and_permissions(self):
        order = self.create_order()
        self.assertEqual(self.manual(order).status_code, 400)
        dispatch_order(order.pk, self.workspace, "TEST")
        self.assertEqual(
            self.client.post(
                f"/api/orders/{order.pk}/manual-status/", {"status": "DELIVERED"}
            ).status_code,
            400,
        )
        self.user.role = "viewer"
        self.user.save()
        self.assertEqual(self.manual(order).status_code, 403)
        self.user.role = "owner"
        self.user.workspace = paid_workspace(name="Other tenant")
        self.user.save()
        self.assertEqual(self.manual(order).status_code, 404)

    def test_others_courier_can_be_updated_manually(self):
        self.courier.provider = "Others"
        self.courier.save()
        order = self.dispatched()
        self.assertEqual(self.manual(order).status_code, 200)
        self.assertEqual(
            self.client.post(f"/api/orders/{order.pk}/resume-tracking/").status_code, 400
        )

    def test_marketing_date_filter_uses_overlap(self):
        today = timezone.localdate()
        Campaign.objects.create(
            workspace=self.workspace,
            name="Current",
            channel="Meta",
            spend=100,
            start_date=today - timedelta(days=3),
            end_date=today,
        )
        Campaign.objects.create(
            workspace=self.workspace,
            name="Old",
            channel="Meta",
            spend=100,
            start_date=today - timedelta(days=10),
            end_date=today - timedelta(days=8),
        )
        response = self.client.get(
            "/api/campaigns/", {"start_date": str(today), "end_date": str(today)}
        )
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["name"], "Current")
        self.assertEqual(
            self.client.get("/api/campaigns/", {"start_date": "invalid"}).status_code, 400
        )

    def test_customer_search_matches_phone_city_and_province(self):
        for term in ["1234567", "Kasur", "Punjab"]:
            self.assertEqual(self.client.get("/api/customers/", {"search": term}).data["count"], 1)
