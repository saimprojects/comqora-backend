import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import urllib3
from django.core import signing
from django.db import transaction
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.catalog.models import Product
from apps.logistics.models import Courier
from apps.logistics.providers import Checkpoint
from apps.logistics.tracking import apply_checkpoints
from apps.messaging.client import WahaError, request
from apps.messaging.models import (
    WhatsAppAccount,
    WhatsAppCampaign,
    WhatsAppContact,
    WhatsAppMedia,
    WhatsAppMessage,
)
from apps.messaging.services import (
    account_for,
    enqueue_order,
    phone_number,
    process_account,
    process_outbox,
    quiet,
)
from apps.orders.models import Customer, Order
from apps.orders.services import apply_tracking
from tests.billing_fixtures import paid_workspace


@override_settings(
    WAHA_ENABLED=True, WAHA_BASE_URL="https://waha.example.test", WAHA_API_KEY="secret-never-expose"
)
class WahaClientTests(SimpleTestCase):
    @patch("apps.messaging.client.http.request")
    def test_transport_has_fixed_origin_no_redirects_or_retries(self, send):
        send.return_value = Mock(status=200)
        send.return_value.read.return_value = b'{"id":"test"}'
        self.assertEqual(request("POST", "/api/sendText", {"text": "hello"}), {"id": "test"})
        self.assertEqual(send.call_args.args[1], "https://waha.example.test/api/sendText")
        self.assertFalse(send.call_args.kwargs["redirect"])
        self.assertFalse(send.call_args.kwargs["retries"])

    @patch("apps.messaging.client.http.request")
    def test_timeout_send_is_uncertain_and_secret_is_not_exposed(self, send):
        send.side_effect = urllib3.exceptions.HTTPError("secret-never-expose")
        with self.assertRaises(WahaError) as err:
            request("POST", "/api/sendText", {})
        self.assertTrue(err.exception.uncertain)
        self.assertNotIn("secret-never-expose", str(err.exception))

    @override_settings(WAHA_BASE_URL="http://169.254.169.254", WAHA_ALLOW_HTTP=False)
    @patch("apps.messaging.client.http.request")
    def test_http_origin_rejected_before_network(self, send):
        with self.assertRaises(WahaError):
            request("GET", "/api/sessions/default")
        send.assert_not_called()

    def test_phone_normalization(self):
        self.assertEqual(phone_number("0300-1234567"), "923001234567")
        self.assertEqual(phone_number("+92 300 1234567"), "923001234567")
        for value in ["123", "92300@c.us", "abc", "03001234567@evil"]:
            with self.assertRaises(Exception):
                phone_number(value)

    @patch("apps.messaging.client.http.request")
    def test_media_timeouts_are_uncertain_not_safe_to_retry(self, send):
        send.side_effect = urllib3.exceptions.HTTPError("timeout")
        for endpoint in ["/api/sendImage", "/api/sendFile", "/api/sendVideo"]:
            with self.assertRaises(WahaError) as err:
                request("POST", endpoint, {})
            self.assertTrue(err.exception.uncertain)


@override_settings(
    WAHA_ENABLED=True,
    WAHA_BASE_URL="https://waha.example.test",
    WAHA_API_KEY="secret-never-expose",
    WAHA_SESSION_MODE="MULTI",
    WAHA_WEBHOOK_SECRET="test-hook",
    WAHA_WEBHOOK_URL="https://api.example.test/api/whatsapp/webhook/",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class MessagingTests(APITestCase):
    def setUp(self):
        self.ws = paid_workspace(name="Test shop")
        self.user = User.objects.create_user(
            username="wa-owner",
            email="wa@example.test",
            workspace=self.ws,
            role="owner",
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(self.user)
        self.customer = Customer.objects.create(
            workspace=self.ws, name="Buyer", phone="03001234567", city="Kasur", address="Test"
        )
        self.courier = Courier.objects.create(
            workspace=self.ws, name="TCS", code="tcs", provider="TCS", base_rate=100
        )
        self.order = Order.objects.create(
            workspace=self.ws,
            customer=self.customer,
            courier=self.courier,
            number="WA-TEST",
            subtotal=1000,
            status="IN_TRANSIT",
            tracking_id="TEST",
            customer_snapshot={"name": "Buyer", "phone": self.customer.phone},
            courier_snapshot={"courier": "TCS", "return_rate": "100"},
        )
        self.account = account_for(self.ws)
        self.account.enabled, self.account.marketing_enabled = True, True
        self.account.events = [
            "IN_TRANSIT",
            "OUT_FOR_DELIVERY",
            "DELIVERED",
            "DELIVERY_FAILED",
            "RETURNED",
        ]
        self.account.save()
        self.contact, _ = WhatsAppContact.objects.update_or_create(
            workspace=self.ws,
            phone="923001234567",
            defaults={
                "name": "Buyer",
                "transactional": True,
                "marketing": True,
                "consent_note": "Explicit checkout permission",
            },
        )
        self.product = Product.objects.create(
            workspace=self.ws, name="Headphones", sku="HP", selling_price=1000
        )

    def queue(self, kind="MANUAL", contact=None, **extra):
        return WhatsAppMessage.objects.create(
            workspace=self.ws,
            account=self.account,
            contact=contact or self.contact,
            order=self.order,
            kind=kind,
            body="Test text",
            dedup_key=str(uuid4()),
            due_at=timezone.now() - timedelta(seconds=1),
            expires_at=timezone.now() + timedelta(days=1),
            **extra,
        )

    def due(self):
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(next_send_at=None)

    def test_new_product_copy_has_formatting_without_image_urls_or_footer(self):
        self.product.image_url = "https://res.cloudinary.com/demo/product.jpg"
        self.product.save()
        response = self.client.post(
            "/api/whatsapp/campaigns/",
            {"name": "Launch", "body": "*New arrivals*", "product_ids": [str(self.product.pk)]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("*New arrivals*", response.data["body"])
        self.assertNotIn("res.cloudinary.com", response.data["body"])
        self.assertNotIn("Reply STOP", response.data["body"])

    def test_receipt_before_response_and_out_of_order_ack(self):
        from apps.messaging.receipts import apply_saved_receipt

        message = self.queue()
        data = {
            "session": self.account.session,
            "event": "message.ack",
            "payload": {"id": "test-receipt", "fromMe": True, "ack": 3},
        }
        self.assertEqual(self.hook(data).status_code, 200)
        message.provider_id = "test-receipt"
        message.save()
        apply_saved_receipt(message)
        data["payload"]["ack"] = 1
        self.hook(data)
        message.refresh_from_db()
        self.assertEqual(message.ack, 3)

    @patch("apps.messaging.client.request")
    def test_webjs_object_id_is_accepted(self, send):
        message = self.queue()
        send.side_effect = [{"status": "WORKING"}, {"id": {"_serialized": "test-webjs"}, "ack": 2}]
        self.assertEqual(process_account(self.account.pk), 1)
        message.refresh_from_db()
        self.assertEqual(message.provider_id, "test-webjs")
        self.assertEqual(message.ack, 2)

    @patch("apps.messaging.client.request")
    def test_unknown_history_check_never_resends(self, send):
        from apps.messaging.receipts import check_delivery

        message = self.queue(state="UNKNOWN", attempted_at=timezone.now())
        send.return_value = [
            {
                "id": "confirmed-id",
                "fromMe": True,
                "body": message.body,
                "timestamp": message.attempted_at.timestamp(),
                "ack": 3,
            }
        ]
        self.assertTrue(check_delivery(message))
        message.refresh_from_db()
        self.assertEqual(message.state, "SENT")
        self.assertEqual(message.ack, 3)
        self.assertEqual(send.call_args.args[0], "GET")

    @patch("apps.messaging.client.request")
    def test_ambiguous_history_stays_unknown(self, send):
        from apps.messaging.receipts import check_delivery

        message = self.queue(state="UNKNOWN", attempted_at=timezone.now())
        row = {
            "id": "one",
            "fromMe": True,
            "body": message.body,
            "timestamp": message.attempted_at.timestamp(),
            "ack": 3,
        }
        send.return_value = [row, {**row, "id": "two"}]
        self.assertFalse(check_delivery(message))
        message.refresh_from_db()
        self.assertEqual(message.state, "UNKNOWN")

    def test_specific_broadcast_targets_only_selected_and_rechecks_exclusions(self):
        second = WhatsAppContact.objects.create(workspace=self.ws, phone="923001115555")
        scheduled = timezone.now() + timedelta(hours=2)
        result = self.client.post(
            "/api/whatsapp/campaigns/",
            {
                "kind": "BROADCAST",
                "name": "News",
                "body": "Store news",
                "audience_mode": "SPECIFIC",
                "recipient_ids": [str(self.contact.pk), str(second.pk)],
                "scheduled_at": scheduled.isoformat(),
            },
            format="json",
        )
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["eligible_count"], 2)
        second.opted_out = True
        second.save()
        result = self.client.post(
            f"/api/whatsapp/campaigns/{result.data['id']}/action/",
            {"action": "approve", "confirm": True},
            format="json",
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["audience_count"], 1)
        message = WhatsAppMessage.objects.get()
        self.assertEqual(message.contact_id, self.contact.pk)
        self.assertEqual(message.due_at, scheduled)

    def test_campaign_rejects_foreign_recipients_media_and_empty_specific(self):
        other = paid_workspace(name="Other")
        contact = WhatsAppContact.objects.create(workspace=other, phone="923001115555")
        media = WhatsAppMedia.objects.create(
            workspace=other,
            url="https://res.cloudinary.com/demo/a.jpg",
            filename="a.jpg",
            mimetype="image/jpeg",
            size=20,
        )
        base = {"kind": "BROADCAST", "name": "News", "body": "Hello"}
        for extra in [
            {"audience_mode": "SPECIFIC", "recipient_ids": []},
            {"audience_mode": "SPECIFIC", "recipient_ids": [str(contact.pk)]},
            {"media_id": str(media.pk)},
        ]:
            self.assertIn(
                self.client.post(
                    "/api/whatsapp/campaigns/", {**base, **extra}, format="json"
                ).status_code,
                [400, 404],
            )

    @patch("apps.messaging.client.request")
    def test_media_broadcast_sends_attachment_with_caption_and_no_text_duplicate(self, send):
        media = WhatsAppMedia.objects.create(
            workspace=self.ws,
            url="https://res.cloudinary.com/demo/a.png",
            filename="a.png",
            mimetype="image/png",
            size=20,
        )
        result = self.client.post(
            "/api/whatsapp/campaigns/",
            {"kind": "BROADCAST", "name": "News", "body": "Hello", "media_id": str(media.pk)},
            format="json",
        )
        self.assertEqual(result.status_code, 201)
        self.client.post(
            f"/api/whatsapp/campaigns/{result.data['id']}/action/",
            {"action": "approve", "confirm": True},
            format="json",
        )
        send.side_effect = [{"status": "WORKING"}, {"id": "media-sent"}]
        with patch("apps.messaging.services.quiet", return_value=False):
            self.assertEqual(process_account(self.account.pk), 1)
        self.assertEqual(send.call_args.args[1], "/api/sendImage")
        payload = send.call_args.args[2]
        self.assertEqual(payload["file"]["url"], media.url)
        self.assertNotIn("Reply STOP", payload["caption"])
        self.assertNotIn("whatsapp-unsubscribe", payload["caption"])
        self.assertNotIn("text", payload)
        self.assertEqual(WhatsAppMessage.objects.get().state, "SENT")

    @patch("cloudinary.uploader.upload")
    def test_upload_rejects_invalid_media_before_cloudinary(self, upload):
        from django.core.files.uploadedfile import SimpleUploadedFile

        for name, body in [
            ("x.html", b"<script>"),
            ("x.jpg", b"not jpeg"),
            ("x.mp4", b"not video"),
        ]:
            result = self.client.post(
                "/api/whatsapp/media/", {"file": SimpleUploadedFile(name, body)}, format="multipart"
            )
            self.assertEqual(result.status_code, 400)
        upload.assert_not_called()

    @patch("cloudinary.config")
    @patch("cloudinary.uploader.upload")
    def test_upload_stores_only_workspace_owned_cloudinary_media(self, upload, config):
        from django.core.files.uploadedfile import SimpleUploadedFile

        config.return_value = Mock(api_secret="test-only")
        upload.return_value = {"secure_url": "https://res.cloudinary.com/demo/raw/upload/test.pdf"}
        result = self.client.post(
            "/api/whatsapp/media/",
            {"file": SimpleUploadedFile("catalog.pdf", b"%PDF-1.4\nTest fixture")},
            format="multipart",
        )
        self.assertEqual(result.status_code, 201)
        media = WhatsAppMedia.objects.get(pk=result.data["id"])
        self.assertEqual(media.workspace_id, self.ws.pk)
        self.assertEqual(media.mimetype, "application/pdf")
        self.assertFalse(upload.call_args.kwargs["overwrite"])
        self.assertIn(str(self.ws.pk), upload.call_args.kwargs["public_id"])

    def test_new_customer_is_enabled_without_fabricated_consent(self):
        customer = Customer.objects.create(workspace=self.ws, name="New", phone="03001112222")
        contact = WhatsAppContact.objects.get(workspace=self.ws, phone="923001112222")
        self.assertTrue(contact.transactional and contact.marketing)
        self.assertFalse(contact.opted_out)
        self.assertEqual(contact.consent_note, "")
        self.assertIsNone(contact.consent_at)
        contact.opted_out = True
        contact.save()
        customer.save()
        Customer.objects.create(workspace=self.ws, name="Duplicate", phone="+92 3001112222")
        contact.refresh_from_db()
        self.assertTrue(contact.opted_out)
        self.assertEqual(
            WhatsAppContact.objects.filter(workspace=self.ws, phone=contact.phone).count(), 1
        )

    def test_removal_cancels_pending_without_deleting_business_data(self):
        message = self.queue()
        result = self.client.post(f"/api/whatsapp/contacts/{self.contact.pk}/remove/")
        self.assertEqual(result.status_code, 200)
        self.contact.refresh_from_db()
        message.refresh_from_db()
        self.assertTrue(self.contact.opted_out)
        self.assertFalse(self.contact.transactional or self.contact.marketing)
        self.assertEqual(message.state, "CANCELLED")
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())
        self.assertTrue(Order.objects.filter(pk=self.order.pk).exists())
        self.customer.save()
        self.contact.refresh_from_db()
        self.assertTrue(self.contact.opted_out)

    def test_removal_is_workspace_scoped(self):
        other = paid_workspace(name="Other")
        contact = WhatsAppContact.objects.create(workspace=other, phone=self.contact.phone)
        result = self.client.post(f"/api/whatsapp/contacts/{contact.pk}/remove/")
        self.assertEqual(result.status_code, 404)
        contact.refresh_from_db()
        self.assertFalse(contact.opted_out)

    def test_rollout_enables_existing_but_preserves_optouts_and_campaign_audience(self):
        from importlib import import_module

        from django.apps import apps
        from django.db import connection

        self.contact.transactional = self.contact.marketing = False
        self.contact.save()
        stopped = WhatsAppContact.objects.create(
            workspace=self.ws,
            phone="923009998888",
            opted_out=True,
            transactional=False,
            marketing=False,
        )
        # Historical customer bypasses the new post-save receiver.
        Customer.objects.bulk_create([Customer(workspace=self.ws, name="Old", phone="03001113333")])
        before = WhatsAppMessage.objects.count()
        migration = import_module("apps.messaging.migrations.0002_default_customer_eligibility")
        migration.populate(apps, Mock(connection=connection))
        migration.populate(apps, Mock(connection=connection))
        self.contact.refresh_from_db()
        stopped.refresh_from_db()
        self.assertTrue(self.contact.transactional and self.contact.marketing)
        self.assertTrue(stopped.opted_out)
        self.assertFalse(stopped.transactional or stopped.marketing)
        self.assertEqual(
            WhatsAppContact.objects.filter(workspace=self.ws, phone="923001113333").count(), 1
        )
        self.assertEqual(WhatsAppMessage.objects.count(), before)

    def test_multi_sessions_are_isolated_and_core_requires_server_assignment(self):
        second = paid_workspace(name="Second")
        self.assertNotEqual(account_for(second).session, self.account.session)
        third = paid_workspace(name="Third")
        with override_settings(WAHA_SESSION_MODE="CORE", WAHA_CORE_WORKSPACE_ID=""):
            with self.assertRaises(Exception):
                account_for(third)
        with override_settings(WAHA_SESSION_MODE="CORE", WAHA_CORE_WORKSPACE_ID=str(third.pk)):
            self.assertEqual(account_for(third).session, "default")

    def test_switch_to_multi_preserves_existing_login_and_team_shares_account(self):
        legacy = paid_workspace(name="Legacy seller")
        with override_settings(WAHA_SESSION_MODE="CORE", WAHA_CORE_WORKSPACE_ID=str(legacy.pk)):
            saved = account_for(legacy)
        self.assertEqual(account_for(legacy).pk, saved.pk)
        self.assertEqual(account_for(legacy).session, "default")
        other = paid_workspace(name="New seller")
        self.assertEqual(account_for(other).session, "sellflow-" + other.pk.hex)
        manager = User.objects.create_user(
            username="wa-manager",
            workspace=self.ws,
            role="manager",
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(manager)
        result = self.client.get("/api/whatsapp/account/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["account"]["session"], self.account.session)
        with override_settings(WAHA_SESSION_MODE="PLUS"):
            self.assertEqual(account_for(other).session, "sellflow-" + other.pk.hex)

    def test_configuration_hides_secrets_and_ignores_supplied_session(self):
        result = self.client.get("/api/whatsapp/account/")
        self.assertNotIn("secret-never-expose", json.dumps(result.data))
        self.assertNotIn("test-hook", json.dumps(result.data))
        result = self.client.patch(
            "/api/whatsapp/account/", {"session": "victim", "gap_seconds": 90}, format="json"
        )
        self.assertEqual(result.status_code, 200)
        self.account.refresh_from_db()
        self.assertNotEqual(self.account.session, "victim")

    def test_preferences_validate_templates_limits_and_quiet_hours(self):
        for payload in [
            {"gap_seconds": 0},
            {"daily_limit": 10000},
            {"quiet_start": 9, "quiet_end": 9},
            {"templates": {"DELIVERED": "{customer.__class__}"}},
            {"templates": {"DELIVERED": "{customer!r}"}},
            {"events": ["NOT_REAL"]},
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.client.patch("/api/whatsapp/account/", payload, format="json").status_code,
                    400,
                )

    def test_consent_is_explicit_and_normalized_and_revocation_cancels_queue(self):
        message = self.queue()
        result = self.client.post(
            "/api/whatsapp/contacts/",
            {
                "customer_id": str(self.customer.pk),
                "transactional": False,
                "marketing": False,
                "opted_out": True,
                "consent_note": "Customer asked to stop",
            },
            format="json",
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(WhatsAppContact.objects.filter(workspace=self.ws).count(), 1)
        message.refresh_from_db()
        self.assertEqual(message.state, "CANCELLED")

    def test_role_and_workspace_isolation(self):
        self.user.role = "viewer"
        self.user.save()
        self.assertEqual(self.client.get("/api/whatsapp/account/").status_code, 403)
        self.user.role = "owner"
        self.user.workspace = paid_workspace(name="Foreign")
        self.user.save()
        result = self.client.post(
            "/api/whatsapp/contacts/",
            {
                "customer_id": str(self.customer.pk),
                "transactional": True,
                "marketing": True,
                "consent_note": "Given at checkout",
            },
            format="json",
        )
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.client.get("/api/whatsapp/messages/").data, [])

    def test_order_notifications_idempotent_and_rollback_with_business_transaction(self):
        enqueue_order(self.order, "DELIVERED", "same")
        enqueue_order(self.order, "DELIVERED", "same")
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        try:
            with transaction.atomic():
                enqueue_order(self.order, "DELIVERED", "rollback")
                raise ValueError()
        except ValueError:
            pass
        self.assertEqual(WhatsAppMessage.objects.count(), 1)

    def test_no_consent_or_disabled_event_does_not_queue(self):
        enqueue_order(self.order, "CREATED", "no-preference")
        self.contact.transactional = False
        self.contact.save()
        enqueue_order(self.order, "DELIVERED", "no-consent")
        self.assertFalse(WhatsAppMessage.objects.exists())

    def test_tracking_queues_latest_transition_not_imported_history(self):
        token = uuid4()
        self.order.tracking_lock_token = token
        self.order.save()
        apply_checkpoints(
            self.order.pk,
            "postex",
            [
                Checkpoint("Out for Delivery", timezone.now() - timedelta(hours=1)),
                Checkpoint("Delivered", timezone.now()),
            ],
            token,
        )
        self.assertEqual(
            list(WhatsAppMessage.objects.values_list("event", flat=True)), ["DELIVERED"]
        )

    def test_manual_status_same_state_does_not_repeat_notification(self):
        apply_tracking(self.order.pk, self.ws, "DELIVERY_FAILED", "one", manual=True)
        apply_tracking(self.order.pk, self.ws, "DELIVERY_FAILED", "two", manual=True)
        self.assertEqual(WhatsAppMessage.objects.count(), 1)

    @patch("apps.messaging.services.client.request")
    def test_worker_sends_once_with_shared_gap_and_no_nested_worker_duplicate(self, send):
        first, second = self.queue(), self.queue()

        def reply(method, path, payload=None):
            self.assertEqual(process_account(self.account.pk), 0)
            return {"status": "WORKING"} if method == "GET" else {"id": "sent-test"}

        send.side_effect = reply
        self.assertEqual(process_account(self.account.pk), 1)
        self.assertEqual(process_account(self.account.pk), 0)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, "SENT")
        self.assertEqual(second.state, "PENDING")
        self.assertEqual(send.call_args.args[2]["chatId"], "923001234567@c.us")

    @patch("apps.messaging.services.client.request")
    def test_pause_opt_out_and_disabled_global_never_send(self, send):
        message = self.queue()
        self.contact.opted_out = True
        self.contact.save()
        process_account(self.account.pk)
        message.refresh_from_db()
        self.assertEqual(message.state, "CANCELLED")
        with override_settings(WAHA_ENABLED=False):
            self.assertEqual(process_outbox(), 0)
        send.assert_not_called()

    @patch("apps.messaging.services.client.request")
    def test_disconnected_preflight_defers_without_send(self, send):
        message = self.queue()
        send.return_value = {"status": "SCAN_QR_CODE"}
        process_account(self.account.pk)
        message.refresh_from_db()
        self.assertEqual(message.state, "PENDING")
        self.assertIsNone(message.attempted_at)
        send.assert_called_once()

    @patch("apps.messaging.services.client.request")
    def test_uncertain_send_and_crashed_claim_are_never_automatically_retried(self, send):
        message = self.queue()
        send.side_effect = [{"status": "WORKING"}, WahaError("Timeout", uncertain=True)]
        process_account(self.account.pk)
        message.refresh_from_db()
        self.assertEqual(message.state, "UNKNOWN")
        self.due()
        process_account(self.account.pk)
        self.assertEqual(send.call_count, 2)
        crashed = self.queue(state="SENDING", attempted_at=timezone.now() - timedelta(minutes=3))
        process_account(self.account.pk)
        crashed.refresh_from_db()
        self.assertEqual(crashed.state, "UNKNOWN")

    @patch("apps.messaging.services.client.request")
    def test_consent_revoked_during_preflight_prevents_send(self, send):
        message = self.queue()

        def reply(*args):
            WhatsAppContact.objects.filter(pk=self.contact.pk).update(opted_out=True)
            return {"status": "WORKING"}

        send.side_effect = reply
        process_account(self.account.pk)
        message.refresh_from_db()
        self.assertEqual(message.state, "CANCELLED")
        self.assertEqual(send.call_count, 1)

    @patch("apps.messaging.services.client.request")
    def test_rolling_limit_includes_uncertain_attempts(self, send):
        self.account.daily_limit = 1
        self.account.save()
        self.queue(state="UNKNOWN", attempted_at=timezone.now())
        self.queue()
        self.assertEqual(process_account(self.account.pk), 0)
        send.assert_not_called()

    def draft(self):
        result = self.client.post(
            "/api/whatsapp/campaigns/",
            {"name": "Launch", "body": "New product", "product_ids": [str(self.product.pk)]},
            format="json",
        )
        self.assertEqual(result.status_code, 201, result.data)
        return WhatsAppCampaign.objects.get(pk=result.data["id"])

    def test_campaign_requires_approval_consent_and_is_not_duplicated(self):
        campaign = self.draft()
        self.assertFalse(WhatsAppMessage.objects.exists())
        url = f"/api/whatsapp/campaigns/{campaign.pk}/action/"
        self.assertEqual(
            self.client.post(url, {"action": "approve"}, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                url, {"action": "approve", "confirm": True}, format="json"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                url, {"action": "approve", "confirm": True}, format="json"
            ).status_code,
            400,
        )
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        self.assertEqual(self.client.post(url, {"action": "pause"}, format="json").status_code, 200)
        self.assertEqual(
            self.client.post(url, {"action": "cancel"}, format="json").status_code, 200
        )
        self.assertEqual(WhatsAppMessage.objects.get().state, "CANCELLED")

    @patch("apps.messaging.services.quiet", return_value=False)
    @patch("apps.messaging.services.client.request")
    def test_marketing_frequency_cap_and_expiry(self, send, quiet_mock):
        campaign = self.draft()
        campaign.state = "RUNNING"
        campaign.save()
        self.queue(kind="MARKETING", state="SENT", attempted_at=timezone.now())
        message = self.queue(kind="MARKETING", campaign=campaign)
        process_account(self.account.pk)
        message.refresh_from_db()
        self.assertEqual(message.state, "CANCELLED")
        expired = self.queue()
        expired.expires_at = timezone.now() - timedelta(seconds=1)
        expired.save()
        process_account(self.account.pk)
        expired.refresh_from_db()
        self.assertEqual(expired.state, "CANCELLED")
        send.assert_not_called()

    def test_quiet_hours_cover_overnight_in_pakistan(self):
        now = timezone.localtime(timezone.now()).replace(hour=22)
        self.assertTrue(quiet(self.account, now))
        self.assertFalse(quiet(self.account, now.replace(hour=12)))

    def hook(self, payload, valid=True):
        raw = json.dumps(payload).encode()
        signature = hmac.new(b"test-hook", raw, hashlib.sha512).hexdigest()
        return self.client.post(
            "/api/whatsapp/webhook/",
            raw,
            content_type="application/json",
            HTTP_X_WEBHOOK_HMAC=signature if valid else "bad",
            HTTP_X_WEBHOOK_HMAC_ALGORITHM="sha512",
        )

    def test_signed_stop_webhook_is_idempotent_and_ignores_outgoing_messages(self):
        message = self.queue()
        data = {
            "session": self.account.session,
            "event": "message",
            "payload": {"from": "923001234567@c.us", "fromMe": False, "body": "STOP"},
        }
        self.assertEqual(self.hook(data, False).status_code, 403)
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.opted_out)
        data["payload"]["fromMe"] = True
        self.hook(data)
        self.contact.refresh_from_db()
        self.assertFalse(self.contact.opted_out)
        data["payload"]["fromMe"] = False
        self.hook(data)
        self.hook(data)
        self.contact.refresh_from_db()
        message.refresh_from_db()
        self.assertTrue(self.contact.opted_out)
        self.assertEqual(message.state, "CANCELLED")

    def test_unsubscribe_link_requires_valid_signature_and_explicit_post(self):
        token = signing.dumps(str(self.contact.pk), salt="whatsapp-unsubscribe")
        self.assertEqual(
            self.client.get("/api/whatsapp/unsubscribe/", {"token": token}).status_code, 405
        )
        self.assertEqual(
            self.client.post("/api/whatsapp/unsubscribe/", {"token": "bad"}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/whatsapp/unsubscribe/", {"token": token}).status_code, 200
        )
        self.contact.refresh_from_db()
        self.assertTrue(self.contact.opted_out)

    def test_restriction_webhook_pauses_marketing(self):
        self.hook(
            {
                "session": self.account.session,
                "event": "session.status",
                "payload": {
                    "status": "WORKING",
                    "data": {"messageCapping": {"cappingStatus": "CAPPED"}},
                },
            }
        )
        self.account.refresh_from_db()
        self.assertFalse(self.account.marketing_enabled)

    @patch("apps.messaging.api.client.request")
    def test_connect_preserves_other_session_config_and_sets_signed_hook(self, send):
        send.side_effect = [
            {
                "status": "WORKING",
                "config": {
                    "debug": False,
                    "webhooks": [{"url": "https://other.test", "events": ["message"]}],
                },
            },
            {},
            {"status": "WORKING"},
        ]
        result = self.client.post("/api/whatsapp/account/", {"action": "connect"})
        self.assertEqual(result.status_code, 200)
        config = send.call_args_list[1].args[2]["config"]
        self.assertFalse(config["debug"])
        self.assertEqual(len(config["webhooks"]), 2)
        self.assertEqual(config["webhooks"][1]["hmac"]["key"], "test-hook")

    def test_manual_message_requires_consent_and_request_id_deduplicates(self):
        payload = {
            "order_id": str(self.order.pk),
            "body": "Order update",
            "request_id": str(uuid4()),
        }
        self.assertEqual(
            self.client.post("/api/whatsapp/messages/", payload, format="json").status_code, 202
        )
        self.assertEqual(
            self.client.post("/api/whatsapp/messages/", payload, format="json").status_code, 202
        )
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
