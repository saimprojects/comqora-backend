import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.assistant import data
from apps.assistant.actions import decide, propose
from apps.assistant.models import AssistantModel, Connection, Conversation, ProposedAction, Turn
from apps.assistant.provider import ProviderError, Session, request_json
from apps.assistant.service import run
from apps.assistant.tools import TOOLS, execute
from apps.catalog.models import Product, StockBatch
from apps.finance.models import BankAccount, BankEntry, Expense, SettlementImport
from apps.logistics.models import Courier
from apps.orders.models import Customer, Order
from tests.billing_fixtures import paid_workspace


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    FAZITA_API_KEY="synthetic-test-key",
)
class AssistantTests(APITestCase):
    def setUp(self):
        self.ws = paid_workspace(name="Saim Tech")
        self.other = paid_workspace(name="Other tenant")
        self.user = User.objects.create_user(
            username="manager-ai",
            email="ai@example.test",
            workspace=self.ws,
            dashboard_access_state="ACTIVE",
        )
        self.connection = Connection.objects.create(name="Test provider")
        self.model = AssistantModel.objects.create(
            connection=self.connection, name="Test Model", model_id="synthetic-model", enabled=True
        )
        self.chat = Conversation.objects.create(workspace=self.ws, user=self.user)
        self.client.force_authenticate(self.user)

    def turn(self, **kw):
        return Turn.objects.create(
            workspace=self.ws,
            conversation=self.chat,
            model=self.model,
            question="Check workspace",
            **kw,
        )

    def send(self, key=None, question="Check all historical customers"):
        return self.client.post(
            f"/api/assistant/conversations/{self.chat.pk}/send/",
            {"request_key": str(key or uuid4()), "question": question, "model_id": self.model.pk},
        )

    def test_brand_notice_and_all_roles_read_financial_data(self):
        BankAccount.objects.create(
            workspace=self.ws,
            name="Wallet",
            opening_date=date(2020, 1, 1),
            opening_balance="100.20",
        )
        for role in ["owner", "manager", "staff", "viewer"]:
            self.user.role = role
            self.user.save()
            config = self.client.get("/api/assistant/config/")
            self.assertEqual(config.status_code, 200)
            self.assertEqual(config.data["name"], "Saim Tech's Manager")
            self.assertIn("Fazita", config.data["notice"])
            self.assertNotIn("synthetic-test-key", str(config.data))
            result = execute(self.turn(), "read_records", {"resource": "bank_accounts"})
            self.assertEqual(result["records"][0]["balance"], "100.20")

    def test_historical_customers_pagination_and_tenant_isolation(self):
        Customer.objects.create(
            workspace=self.other, name="Secret Ahmed", phone="222", city="X", address="private"
        )
        for i in range(35):
            c = Customer.objects.create(
                workspace=self.ws, name=f"Ahmed {i:02}", phone="111", city="Lahore", address="A"
            )
            Customer.objects.filter(pk=c.pk).update(
                created_at=timezone.now() - timedelta(days=1200)
            )
        first = data.read_records(
            self.ws, {"resource": "customers", "search": "Ahmed", "page_size": 30}
        )
        second = data.read_records(
            self.ws, {"resource": "customers", "search": "Ahmed", "page_size": 30, "page": 2}
        )
        self.assertEqual(first["total"], 35)
        self.assertEqual(len(first["records"]) + len(second["records"]), 35)
        self.assertNotIn("Secret", str(first) + str(second))

    def test_field_and_filter_allowlists_block_credentials_and_foreign_workspace(self):
        turn = self.turn()
        for args in [
            {
                "resource": "team",
                "filters": [{"field": "password", "operator": "contains", "value": "x"}],
            },
            {"resource": "customers", "workspace_id": str(self.other.pk)},
            {"resource": "orders", "sort": "customer__workspace__name"},
        ]:
            self.assertIn("error", execute(turn, "read_records", args))
        self.assertIn(
            "error",
            execute(
                turn,
                "read_field",
                {"resource": "team", "id": str(self.user.pk), "field": "password"},
            ),
        )
        schema = data.describe_data()
        self.assertNotIn("password", json.dumps(schema))
        self.assertNotIn("processing_token", json.dumps(schema))
        self.assertIn("error", execute(turn, "run_sql", {"sql": "DELETE ALL"}))

    def test_aggregate_uses_complete_data_and_decimals(self):
        for _ in range(45):
            Expense.objects.create(
                workspace=self.ws, name="Box", amount="0.10", date=date(2024, 1, 1)
            )
        Expense.objects.create(
            workspace=self.other, name="private", amount="999", date=date(2024, 1, 1)
        )
        result = data.aggregate_records(self.ws, {"resource": "expenses", "sum_fields": ["amount"]})
        self.assertEqual(result["matched_records"], 45)
        self.assertEqual(result["sum_amount"], Decimal("4.50"))

    def test_stock_and_overview_exact_current_balances(self):
        product = Product.objects.create(workspace=self.ws, name="Cup", sku="C", selling_price="2")
        StockBatch.objects.create(
            workspace=self.ws,
            product=product,
            reference="P",
            purchased_quantity=10,
            remaining_quantity=8,
            reserved_quantity=3,
            unit_cost="0.10",
            received_at=date(2024, 1, 1),
        )
        result = data.read_records(self.ws, {"resource": "products"})
        self.assertEqual(result["records"][0]["available"], 5)
        overview = data.overview(self.ws, {})
        self.assertEqual(overview["inventory_value_current"], Decimal("0.80"))
        self.assertEqual(overview["totals"]["net_profit"], 0)

    def test_bank_running_balance_across_pages_and_prior_dates(self):
        account = BankAccount.objects.create(
            workspace=self.ws, name="Cash", opening_date=date(2020, 1, 1), opening_balance="100.10"
        )

        def entry(amount, day):
            return BankEntry.objects.create(
                workspace=self.ws,
                account=account,
                actor=self.user,
                amount=amount,
                date=day,
                reference="R",
                request_key=uuid4(),
            )

        entry("5.20", date(2023, 1, 1))
        for _ in range(32):
            entry("0.10", date(2024, 1, 1))
        first = data.bank_statement(
            self.ws,
            {"account_id": str(account.pk), "start_date": "2024-01-01", "end_date": "2024-12-31"},
        )
        second = data.bank_statement(
            self.ws,
            {
                "account_id": str(account.pk),
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "page": 2,
            },
        )
        self.assertEqual(first["opening_balance"], Decimal("105.30"))
        self.assertEqual(first["closing_balance"], Decimal("108.50"))
        self.assertEqual(second["entries"][0]["running_balance"], Decimal("108.40"))
        self.assertEqual(second["entries"][-1]["running_balance"], first["closing_balance"])

    def test_json_document_inspection_is_scoped_and_paginated(self):
        courier = Courier.objects.create(workspace=self.ws, name="Post", code="p", base_rate=0)
        statement = SettlementImport.objects.create(
            workspace=self.ws,
            courier=courier,
            digest="test",
            source=b"secret binary",
            review={"rows": [{"amount": str(i)} for i in range(25)]},
        )
        result = data.read_field(
            self.ws,
            {
                "resource": "settlements",
                "id": str(statement.pk),
                "field": "review",
                "path": ["rows"],
                "offset": 20,
            },
        )
        self.assertEqual(len(result["items"]), 5)
        self.assertEqual(result["total"], 25)
        self.assertIn(
            "error",
            execute(
                self.turn(),
                "read_field",
                {"resource": "settlements", "id": str(statement.pk), "field": "source"},
            ),
        )

    @patch("apps.assistant.provider.request_json")
    def test_end_to_end_tool_loop_sources_and_idempotent_retry(self, provider):
        Customer.objects.create(
            workspace=self.ws, name="Ahmed", phone="1", city="Lahore", address="A"
        )
        provider.side_effect = [
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "read_records",
                        "arguments": json.dumps({"resource": "customers", "search": "Ahmed"}),
                    }
                ],
                "usage": {"input_tokens": 10},
            },
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Ahmed ka customer record mil gaya."}
                        ],
                    }
                ],
                "usage": {"output_tokens": 15},
            },
        ]
        key = uuid4()
        response = self.send(key)
        self.assertEqual(response.status_code, 202)
        provider.assert_not_called()
        queued = Turn.objects.get(pk=response.data["id"])
        queued.status = "RUNNING"
        queued.save()
        run(queued)
        response = self.send(key)
        self.assertEqual(response.data["status"], "COMPLETE")
        self.assertEqual(response.data["sources"][0]["label"], "Ahmed")
        outputs = [
            item["output"]
            for item in provider.call_args_list[1].args[2]["input"]
            if item.get("type") == "function_call_output"
        ]
        self.assertIn("Ahmed", " ".join(outputs))
        repeated = self.send(key)
        self.assertEqual(response.data["id"], repeated.data["id"])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(self.send(key, "Different").status_code, 409)

    @patch("apps.assistant.provider.request_json")
    def test_provider_errors_are_saved_and_no_fake_answer(self, provider):
        provider.side_effect = ProviderError("The AI provider timed out.")
        response = self.send()
        queued = Turn.objects.get(pk=response.data["id"])
        queued.status = "RUNNING"
        queued.save()
        run(queued)
        response = self.send(queued.request_key)
        self.assertEqual(response.data["status"], "ERROR")
        self.assertEqual(response.data["answer"], "")
        self.assertIn("temporarily unavailable", response.data["error"])
        self.assertIn("timed out", Turn.objects.get(pk=response.data["id"]).error)

    def test_chat_and_proposals_private_even_inside_workspace(self):
        outsider = User.objects.create_user(
            username="colleague",
            email="c@example.test",
            workspace=self.ws,
            dashboard_access_state="ACTIVE",
        )
        action = ProposedAction.objects.create(
            workspace=self.ws,
            turn=self.turn(),
            kind="create_expense",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.client.force_authenticate(outsider)
        self.assertEqual(
            self.client.get(f"/api/assistant/conversations/{self.chat.pk}/").status_code, 404
        )
        self.assertEqual(
            self.client.post(
                f"/api/assistant/actions/{action.pk}/", {"decision": "confirm"}
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/assistant/conversations/").data["count"], 0)

    def test_proposal_never_executes_until_confirm_and_is_exactly_once(self):
        result = propose(
            self.turn(),
            {
                "kind": "create_expense",
                "details_json": json.dumps(
                    {"name": "Box", "amount": "800.10", "category": "other", "date": "2024-01-01"}
                ),
            },
        )
        self.assertEqual(Expense.objects.count(), 0)
        url = f"/api/assistant/actions/{result['proposal_id']}/"
        first = self.client.post(url, {"decision": "confirm"})
        self.assertEqual(first.data["status"], "APPLIED")
        second = self.client.post(url, {"decision": "confirm"})
        self.assertEqual(second.data["result"], first.data["result"])
        self.assertEqual(Expense.objects.count(), 1)

    def test_expired_cancelled_and_role_changed_proposals_do_not_execute(self):
        def proposal():
            result = propose(
                self.turn(),
                {
                    "kind": "create_expense",
                    "details_json": json.dumps(
                        {"name": "Box", "amount": "1.00", "date": "2024-01-01"}
                    ),
                },
            )
            return ProposedAction.objects.get(pk=result["proposal_id"])

        a = proposal()
        a.expires_at = timezone.now() - timedelta(seconds=1)
        a.save()
        self.assertEqual(decide(SimpleNamespace(user=self.user), a.pk, "confirm").status, "EXPIRED")
        b = proposal()
        self.assertEqual(
            decide(SimpleNamespace(user=self.user), b.pk, "cancel").status, "CANCELLED"
        )
        c = proposal()
        self.user.role = "viewer"
        self.user.save()
        self.assertEqual(
            self.client.post(
                f"/api/assistant/actions/{c.pk}/", {"decision": "confirm"}
            ).status_code,
            403,
        )
        self.assertEqual(Expense.objects.count(), 0)

    def test_lock_csrf_missing_key_and_usage_limit(self):
        self.user.dashboard_access_state = "PENDING"
        self.user.save()
        self.assertIn(self.client.get("/api/assistant/config/").status_code, [403, 423])
        self.user.dashboard_access_state = "ACTIVE"
        self.user.save()
        csrf_client = APIClient(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post("/api/assistant/conversations/", {}).status_code, 403)
        with override_settings(FAZITA_API_KEY=""), patch.dict("os.environ", {"FAZITA_API_KEY": ""}):
            self.assertFalse(self.client.get("/api/assistant/config/").data["ready"])
            self.assertEqual(self.send().status_code, 503)
        self.connection.daily_workspace_turn_limit = 1
        self.connection.save()
        self.turn(status="COMPLETE")
        self.assertEqual(self.send().status_code, 429)

    @patch("apps.assistant.provider.request_json")
    def test_all_protocols_preserve_tool_results(self, request):
        for protocol, response in [
            (
                "chat",
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "c",
                                        "type": "function",
                                        "function": {
                                            "name": "business_overview",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            (
                "messages",
                {
                    "content": [
                        {"type": "tool_use", "id": "c", "name": "business_overview", "input": {}}
                    ]
                },
            ),
            (
                "responses",
                {
                    "output": [
                        {"type": "reasoning", "encrypted_content": "opaque"},
                        {
                            "type": "function_call",
                            "call_id": "c",
                            "name": "business_overview",
                            "arguments": "{}",
                        },
                    ]
                },
            ),
        ]:
            self.model.protocol = protocol
            request.return_value = response
            session = Session(self.model, "policy", [{"role": "user", "content": "Hi"}], TOOLS)
            _, calls, _ = session.ask()
            self.assertEqual(calls[0]["name"], "business_overview")
            session.add_results([("c", '{"count":1}')])
            self.assertIn("count", json.dumps(session.messages))
            self.assertEqual(request.call_args.args[1], protocol)

    @patch("apps.assistant.provider.build_opener")
    def test_transport_uses_only_fazita_and_never_exposes_key(self, opener):
        from urllib.error import HTTPError

        opener.return_value.open.side_effect = HTTPError(
            "url", 401, "secret provider body", {}, None
        )
        with self.assertRaises(ProviderError) as caught:
            request_json(self.connection, "models")
        self.assertNotIn("secret provider body", str(caught.exception))
        self.assertEqual(
            opener.return_value.open.call_args.args[0].full_url, "https://fazita.com/v1/models"
        )

    def test_registry_fields_all_exist(self):
        for name, spec in data.DATA.items():
            self.assertEqual(set(spec["fields"]), set(data.fields_for(spec)), name)
            self.assertIn("records", data.read_records(self.ws, {"resource": name}), name)
            for field in spec["extra"]:
                spec["model"]._meta.get_field(field)

    def test_order_proposal_shows_names_prices_defaults_and_reserves_only_on_confirmation(self):
        from apps.assistant.actions import review_details

        customer = Customer.objects.create(
            workspace=self.ws, name="Ahmed", phone="1", city="Lahore", address="A"
        )
        courier = Courier.objects.create(workspace=self.ws, name="Post", code="p", base_rate=0)
        product = Product.objects.create(
            workspace=self.ws, name="Cup", sku="C", selling_price="200"
        )
        batch = StockBatch.objects.create(
            workspace=self.ws,
            product=product,
            reference="P",
            purchased_quantity=10,
            remaining_quantity=10,
            unit_cost="50",
            received_at=date(2024, 1, 1),
        )
        details = {
            "customer": str(customer.pk),
            "courier": str(courier.pk),
            "items": [{"product": str(product.pk), "quantity": 2, "unit_price": "200.00"}],
        }
        proposal = propose(
            self.turn(), {"kind": "create_order", "details_json": json.dumps(details)}
        )
        action = ProposedAction.objects.get(pk=proposal["proposal_id"])
        self.assertEqual(action.payload["payment_type"], "COD")
        self.assertIn("Ahmed", review_details(action)["customer"])
        self.assertIn("Cup", review_details(action)["items"][0]["product"])
        self.assertEqual(Order.objects.count(), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.reserved_quantity, 0)
        result = self.client.post(f"/api/assistant/actions/{action.pk}/", {"decision": "confirm"})
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["status"], "APPLIED")
        self.assertEqual(Order.objects.get().subtotal, Decimal("400"))
        batch.refresh_from_db()
        self.assertEqual(batch.reserved_quantity, 2)

    def test_order_proposals_reject_foreign_products_and_missing_price(self):
        customer = Customer.objects.create(
            workspace=self.ws, name="Ahmed", phone="1", city="Lahore", address="A"
        )
        courier = Courier.objects.create(workspace=self.ws, name="Post", code="p", base_rate=0)
        product = Product.objects.create(
            workspace=self.other, name="Secret cup", sku="S", selling_price="200"
        )
        for item in [
            {"product": str(product.pk), "quantity": 1},
            {"product": str(product.pk), "quantity": 1, "unit_price": "200"},
        ]:
            result = execute(
                self.turn(),
                "prepare_action",
                {
                    "kind": "create_order",
                    "details_json": json.dumps(
                        {"customer": str(customer.pk), "courier": str(courier.pk), "items": [item]}
                    ),
                },
            )
            self.assertIn("error", result)
        self.assertEqual(ProposedAction.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_bank_proposal_creates_only_one_internal_ledger_entry(self):
        account = BankAccount.objects.create(
            workspace=self.ws, name="Wallet", opening_date=date(2020, 1, 1), opening_balance="100"
        )
        result = propose(
            self.turn(),
            {
                "kind": "record_bank_movement",
                "details_json": json.dumps(
                    {
                        "account": str(account.pk),
                        "amount": "50.10",
                        "date": "2024-01-01",
                        "reference": "Manual receipt",
                    }
                ),
            },
        )
        self.assertEqual(BankEntry.objects.count(), 0)
        url = f"/api/assistant/actions/{result['proposal_id']}/"
        response = self.client.post(url, {"decision": "confirm"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "APPLIED")
        self.client.post(url, {"decision": "confirm"})
        self.assertEqual(BankEntry.objects.count(), 1)
        self.assertEqual(BankEntry.objects.get().amount, Decimal("50.10"))
