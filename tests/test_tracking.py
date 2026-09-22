import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import urllib3
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.logistics.models import Courier, TrackingWorkerState
from apps.logistics.providers import (
    RUN_PROVIDERS,
    Checkpoint,
    TrackingError,
    fetch_tracking,
    normalize_status,
    parse_postex,
    parse_run,
    request_json,
    source_time,
)
from apps.logistics.tracking import (
    apply_checkpoints,
    sync_due,
    sync_order,
    tracking_info,
    worker_health,
)
from apps.orders.models import Customer, Order
from tests.billing_fixtures import paid_workspace


class ProviderTests(SimpleTestCase):
    def test_run_exact_timeline_and_pakistan_timezone(self):
        data = [
            {
                "tracking_no": "TEST1",
                "status": "Shipment picked in  KASUR",
                "created": "2020-09-11 20:17:59",
            },
            {
                "tracking_no": "TEST1",
                "status": "Arrived at Station in ISLAMABAD",
                "created": "2020-09-12 10:42:19",
            },
        ]
        points = parse_run(data, "TEST1")
        self.assertEqual(points[0].text, data[0]["status"])
        self.assertEqual(points[0].occurred_at.isoformat(), "2020-09-11T20:17:59+05:00")
        self.assertEqual(normalize_status(points[1].text), "IN_TRANSIT")

    @patch("apps.logistics.providers.request_json")
    def test_all_seven_couriers_route_to_run_without_credentials(self, request):
        request.return_value = [
            {"tracking_no": "TEST", "status": "Delivered", "created": "2020-01-01 12:00:00"}
        ]
        for courier in RUN_PROVIDERS:
            with self.subTest(courier=courier):
                source, _ = fetch_tracking(courier, "TEST", uuid.uuid4())
                self.assertEqual(source, "run_courier")
                request.assert_called_with(
                    "POST",
                    "https://portal.runcourier.com/API/TrackOrder.php",
                    payload={"tracking_no": "TEST"},
                )

    @patch("apps.logistics.providers.request_json")
    def test_empty_history_falls_back_to_current(self, request):
        request.side_effect = [[], {"tracking_no": "TEST", "status": "Delivered"}]
        _, points = fetch_tracking("TCS", "TEST", uuid.uuid4())
        self.assertIsNone(points[0].occurred_at)
        self.assertTrue(request.call_args.args[1].endswith("CurrentStatus.php"))

    @override_settings(POSTEX_API_TOKEN="test-only-secret")
    @patch("apps.logistics.providers.request_json")
    def test_postex_uses_token_and_observed_updated_at_schema(self, request):
        request.return_value = {
            "statusCode": "200",
            "dist": {
                "trackingNumber": "TEST/1",
                "transactionStatus": "Old summary",
                "transactionStatusHistory": [
                    {
                        "transactionStatusMessage": "Arrived at Transit Hub LHE",
                        "transactionStatusMessageCode": "0035",
                        "updatedAt": "2020-09-13T00:26:56.000+0500",
                    }
                ],
            },
        }
        source, points = fetch_tracking("PostEx", "TEST/1", "workspace")
        self.assertEqual(source, "postex")
        self.assertEqual(points[0].code, "0035")
        self.assertEqual(points[0].occurred_at.isoformat(), "2020-09-13T00:26:56+05:00")
        request.assert_called_once_with(
            "GET",
            "https://api.postex.pk/services/integration/api/order/v1/track-order/TEST%2F1",
            token="test-only-secret",
        )

    @override_settings(POSTEX_API_TOKEN="test-only-secret")
    @patch("apps.logistics.providers.request_json")
    def test_all_workspaces_use_the_server_merchant_token(self, request):
        request.return_value = {
            "statusCode": "200",
            "dist": {"trackingNumber": "TEST", "transactionStatus": "Delivered"},
        }
        for workspace in ("workspace-one", "workspace-two"):
            source, points = fetch_tracking("PostEx", "TEST", workspace)
            self.assertEqual(source, "postex")
            self.assertEqual(points[0].text, "Delivered")
            self.assertEqual(request.call_args.kwargs["token"], "test-only-secret")

    @override_settings(POSTEX_API_TOKEN="")
    @patch("apps.logistics.providers.request_json")
    def test_missing_token_and_other_courier_never_call_provider(self, request):
        for provider in ("PostEx", "Others", "Unknown"):
            with self.assertRaises(TrackingError):
                fetch_tracking(provider, "TEST", uuid.uuid4())
        request.assert_not_called()

    def test_status_mapping_never_confuses_pending_or_failed_with_final(self):
        for text in (
            "Undelivered",
            "Not Delivered",
            "Return In Process",
            "Return Confirmation",
            "Returned to origin city",
            "Parcel Return to office",
            "Lost",
            "Claim",
        ):
            with self.subTest(text=text):
                self.assertNotIn(normalize_status(text), {"DELIVERED", "RETURNED"})
        self.assertEqual(normalize_status("Delivered"), "DELIVERED")
        self.assertEqual(normalize_status("Returned to Shipper"), "RETURNED")

    def test_invalid_or_mismatched_payloads_do_not_become_events(self):
        for data in (
            [],
            {},
            {"error": "no"},
            [123],
            [{"tracking_no": "OTHER", "status": "Delivered"}],
            [{"status": 200}],
            [{"status": "Delivered", "created": "bad-date"}],
        ):
            with self.subTest(data=data), self.assertRaises(TrackingError):
                parse_run(data, "TEST")
        for data in (
            {"statusCode": "401"},
            {"statusCode": "200", "dist": {"trackingNumber": "OTHER"}},
        ):
            with self.assertRaises(TrackingError):
                parse_postex(data, "TEST")
        with self.assertRaises(TrackingError):
            source_time((timezone.now() + timedelta(days=1)).isoformat())

    def test_observed_postex_messages_map_to_distinct_shipment_states(self):
        cases = {
            "Waiting for Delivery": "IN_TRANSIT",
            "Enroute for Delivery": "OUT_FOR_DELIVERY",
            "Out for Delivery": "OUT_FOR_DELIVERY",
            "Attempt Made: RFD(REFUSED TO RECEIVE)": "DELIVERY_FAILED",
            "Return In Process": "RETURN_IN_TRANSIT",
            "Returned to origin city": "RETURN_IN_TRANSIT",
            "Returned to Shipper": "RETURNED",
            "Delivered": "DELIVERED",
        }
        for text, status in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_status(text), status)
        for text in ("Not yet delivered", "Return request rejected", "Delivered status reversed"):
            self.assertIsNone(normalize_status(text))

    @patch("apps.logistics.providers.http.request")
    def test_transport_is_bounded_and_does_not_follow_redirects(self, request):
        response = Mock(status=200)
        response.read.return_value = b"[]"
        request.return_value = response
        self.assertEqual(
            request_json("POST", "https://example.com", payload={"tracking_no": "TEST"}), []
        )
        self.assertFalse(request.call_args.kwargs["redirect"])
        self.assertFalse(request.call_args.kwargs["retries"])
        response.read.assert_called_once_with(1_048_577)
        response.close.assert_called_once()

    @patch("apps.logistics.providers.http.request")
    def test_transport_errors_are_safe(self, request):
        for status in (302, 401, 429, 500):
            request.return_value = Mock(status=status)
            with self.assertRaises(TrackingError):
                request_json("GET", "https://example.com", token="never-print")
        request.side_effect = urllib3.exceptions.HTTPError("never-print")
        with self.assertRaises(TrackingError) as caught:
            request_json("GET", "https://example.com", token="never-print")
        self.assertNotIn("never-print", str(caught.exception))


@override_settings(
    TRACKING_ENABLED=True,
    TRACKING_POLL_SECONDS=60,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class TrackingTests(APITestCase):
    def setUp(self):
        self.ws = paid_workspace(name="Tracking test")
        self.user = User.objects.create_user(
            username="tracking",
            email="tracking@example.com",
            password="Test!2026long",
            workspace=self.ws,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(self.user)
        self.courier = Courier.objects.create(
            workspace=self.ws,
            name="TCS contract",
            code="tcs",
            provider="TCS",
            base_rate=200,
            return_rate=150,
        )
        self.customer = Customer.objects.create(
            workspace=self.ws, name="Test", phone="000", city="Test", address="Test"
        )
        self.order = Order.objects.create(
            workspace=self.ws,
            customer=self.customer,
            courier=self.courier,
            number="TEST-1",
            status="IN_TRANSIT",
            tracking_id="TEST1",
            tracking_provider="TCS",
            subtotal=2000,
            product_cost=500,
            courier_cost=200,
            courier_snapshot={"courier": "TCS", "return_rate": "150"},
            customer_snapshot={"name": "Test", "city": "Test"},
            dispatched_at=timezone.now() - timedelta(days=3),
        )
        self.stamp = timezone.now() - timedelta(days=1)

    def due(self):
        Order.objects.filter(pk=self.order.pk).update(tracking_next_sync_at=None)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_full_history_updates_profit_with_original_delivered_time(self, fetch):
        fetch.return_value = (
            "run_courier",
            [
                Checkpoint("Delivered", self.stamp),
                Checkpoint("Shipment picked in  KASUR", self.stamp - timedelta(days=1)),
            ],
        )
        self.assertEqual(sync_order(self.order.pk)["state"], "updated")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "DELIVERED")
        self.assertEqual(self.order.finalized_at, self.stamp)
        self.assertIsNone(self.order.tracking_next_sync_at)
        events = self.order.tracking_events.order_by("occurred_at")
        self.assertEqual(events.count(), 2)
        self.assertEqual(events.first().raw_status, "Shipment picked in  KASUR")
        self.assertEqual(sync_order(self.order.pk)["state"], "idle")
        self.assertEqual(fetch.call_count, 1)
        detail = self.client.get(f"/api/orders/{self.order.pk}/")
        self.assertEqual(detail.data["financials"]["state"], "REALIZED")
        self.assertNotIn("tracking_lock_token", detail.data)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_repeated_history_and_poll_interval_are_idempotent(self, fetch):
        fetch.return_value = ("run_courier", [Checkpoint("Out for Delivery", self.stamp)])
        sync_order(self.order.pk)
        self.assertEqual(sync_order(self.order.pk)["state"], "waiting")
        self.due()
        sync_order(self.order.pk)
        self.assertEqual(self.order.tracking_events.count(), 1)
        self.assertEqual(fetch.call_count, 2)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_return_in_process_does_not_finalize_or_charge_return(self, fetch):
        fetch.return_value = ("run_courier", [Checkpoint("Return In Process", self.stamp)])
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "RETURN_IN_TRANSIT")
        self.assertEqual(self.order.return_cost, 0)
        self.assertIsNone(self.order.finalized_at)
        self.assertIsNotNone(self.order.tracking_next_sync_at)
        self.due()
        fetch.return_value = (
            "run_courier",
            [Checkpoint("Returned to Shipper", self.stamp + timedelta(hours=1))],
        )
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "RETURNED")
        self.assertEqual(self.order.return_cost, Decimal("150"))
        self.assertIsNone(self.order.return_received_at)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_unknown_latest_status_does_not_apply_old_delivered_event(self, fetch):
        fetch.return_value = (
            "run_courier",
            [
                Checkpoint("Delivered", self.stamp - timedelta(hours=1)),
                Checkpoint("Delivery corrected by office", self.stamp),
            ],
        )
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "IN_TRANSIT")
        self.assertIn("Unmapped", self.order.tracking_error)
        self.assertEqual(self.order.tracking_events.filter(status="UNKNOWN").count(), 1)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_saved_unknown_history_is_remapped_on_next_sync(self, fetch):
        fetch.return_value = (
            "postex",
            [Checkpoint("Attempt Made: RFD(REFUSED TO RECEIVE)", self.stamp)],
        )
        with patch("apps.logistics.tracking.normalize_status", return_value=None):
            sync_order(self.order.pk)
        self.assertEqual(self.order.tracking_events.get().status, "UNKNOWN")
        self.due()
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "DELIVERY_FAILED")
        self.assertEqual(self.order.tracking_events.get().status, "DELIVERY_FAILED")
        self.assertEqual(self.order.tracking_error, "")
        self.assertEqual(self.order.return_cost, 0)
        self.assertIsNone(self.order.finalized_at)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_existing_coarse_mapping_is_upgraded_without_duplicate_events(self, fetch):
        fetch.return_value = ("postex", [Checkpoint("Enroute for Delivery", self.stamp)])
        with patch("apps.logistics.tracking.normalize_status", return_value="IN_TRANSIT"):
            sync_order(self.order.pk)
        self.due()
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "OUT_FOR_DELIVERY")
        self.assertEqual(self.order.tracking_events.get().status, "OUT_FOR_DELIVERY")
        self.assertIsNotNone(self.order.tracking_next_sync_at)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_undated_replay_cannot_override_newer_manual_decision(self, fetch):
        fetch.return_value = ("postex", [Checkpoint("Out for Delivery")])
        sync_order(self.order.pk)
        Order.objects.filter(pk=self.order.pk).update(
            status="DELIVERY_FAILED", tracking_status_at=timezone.now()
        )
        self.due()
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "DELIVERY_FAILED")
        self.assertEqual(self.order.tracking_events.count(), 1)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_historical_unknown_does_not_hide_known_current_status(self, fetch):
        fetch.return_value = (
            "postex",
            [
                Checkpoint("Internal office note", self.stamp - timedelta(hours=1)),
                Checkpoint("Enroute for Delivery", self.stamp),
            ],
        )
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "OUT_FOR_DELIVERY")
        self.assertEqual(self.order.tracking_error, "")
        self.assertEqual(self.order.tracking_events.filter(status="UNKNOWN").count(), 1)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_worker_continues_polling_every_unresolved_state(self, fetch):
        for status in Order.ACTIVE_SHIPMENT_STATUSES:
            with self.subTest(status=status):
                Order.objects.filter(pk=self.order.pk).update(status=status)
                self.due()
                fetch.return_value = ("postex", [Checkpoint("Delivered", self.stamp)])
                self.assertEqual(sync_due(), 1)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, "DELIVERED")

    def test_terminal_status_cannot_be_regressed_by_inflight_history(self):
        token = uuid.uuid4()
        Order.objects.filter(pk=self.order.pk).update(status="DELIVERED", tracking_lock_token=token)
        apply_checkpoints(
            self.order.pk, "postex", [Checkpoint("Out for Delivery", self.stamp)], token
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "DELIVERED")

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_old_response_cannot_regress_newer_status(self, fetch):
        Order.objects.filter(pk=self.order.pk).update(
            status="DELIVERY_FAILED", tracking_status_at=self.stamp
        )
        fetch.return_value = (
            "run_courier",
            [Checkpoint("Out for Delivery", self.stamp - timedelta(hours=1))],
        )
        sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "DELIVERY_FAILED")

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_provider_failure_preserves_outcome_and_backs_off(self, fetch):
        fetch.side_effect = TrackingError("Courier unavailable")
        with self.assertRaises(TrackingError):
            sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "IN_TRANSIT")
        self.assertEqual(self.order.tracking_failures, 1)
        self.assertIsNone(self.order.tracking_checked_at)
        self.assertGreater(
            self.order.tracking_next_sync_at, timezone.now() + timedelta(seconds=100)
        )
        self.assertIsNone(self.order.tracking_lock_token)
        self.assertEqual(self.order.tracking_events.count(), 0)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_database_lease_blocks_second_worker_and_recovers_expired_claim(self, fetch):
        token = uuid.uuid4()
        Order.objects.filter(pk=self.order.pk).update(
            tracking_lock_token=token, tracking_lock_until=timezone.now() + timedelta(seconds=90)
        )
        self.assertEqual(sync_order(self.order.pk)["state"], "waiting")
        fetch.assert_not_called()
        self.assertFalse(
            apply_checkpoints(
                self.order.pk, "run_courier", [Checkpoint("Delivered", self.stamp)], uuid.uuid4()
            )
        )
        Order.objects.filter(pk=self.order.pk).update(
            tracking_lock_until=timezone.now() - timedelta(seconds=1)
        )
        fetch.return_value = ("run_courier", [Checkpoint("Out for Delivery", self.stamp)])
        self.assertEqual(sync_order(self.order.pk)["state"], "updated")

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_sync_permissions_and_workspace_isolation(self, fetch):
        foreign = paid_workspace(name="Foreign")
        other_user = User.objects.create_user(
            username="foreign",
            email="foreign@example.com",
            workspace=foreign,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(other_user)
        self.assertEqual(
            self.client.post(f"/api/orders/{self.order.pk}/sync-tracking/").status_code, 404
        )
        self.user.role = "viewer"
        self.user.save()
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self.client.post(f"/api/orders/{self.order.pk}/sync-tracking/").status_code, 403
        )
        fetch.assert_not_called()

    def test_other_courier_warning_and_dropdown_choice_validation(self):
        response = self.client.post(
            "/api/couriers/",
            {"name": "Local delivery", "code": "local", "provider": "Others", "base_rate": "200"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["auto_tracking"])
        self.order.tracking_provider = "Others"
        self.assertFalse(tracking_info(self.order)["supported"])
        self.assertIn("Auto Tracking not available", tracking_info(self.order)["warning"])
        response = self.client.post(
            "/api/couriers/",
            {"name": "Bad", "code": "bad", "provider": "Invalid", "base_rate": "200"},
        )
        self.assertEqual(response.status_code, 400)

    @patch("apps.logistics.tracking.fetch_tracking")
    def test_worker_scans_due_orders_and_records_heartbeat(self, fetch):
        fetch.return_value = ("run_courier", [Checkpoint("Picked up", self.stamp)])
        self.assertEqual(sync_due(), 1)
        self.assertTrue(worker_health()["running"])
        self.assertEqual(sync_due(), 0)
        self.assertEqual(TrackingWorkerState.objects.count(), 1)
        # The standalone command cleans up connections; this test owns an outer transaction.
        with (
            patch("apps.logistics.management.commands.sync_tracking.close_old_connections"),
            patch("apps.finance.worker.close_old_connections"),
        ):
            call_command("sync_tracking", limit=1, verbosity=0)

    @override_settings(TRACKING_ENABLED=False)
    @patch("apps.logistics.tracking.fetch_tracking")
    def test_disabled_tracking_makes_no_calls(self, fetch):
        self.assertEqual(sync_due(), 0)
        fetch.assert_not_called()

    @override_settings(POSTEX_API_TOKEN="")
    def test_missing_postex_config_is_visible_and_scheduled_for_later(self):
        self.order.tracking_provider = "PostEx"
        self.order.save()
        with self.assertRaises(TrackingError):
            sync_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertIn("PostEx", self.order.tracking_error)
        self.assertIsNotNone(self.order.tracking_next_sync_at)

    @override_settings(POSTEX_API_TOKEN="never-expose")
    def test_configuration_endpoint_never_exposes_token(self):
        response = self.client.get("/api/workspace/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("never-expose", json.dumps(response.data, default=str))
