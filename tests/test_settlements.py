import copy
import io
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from apps.accounts.models import User
from apps.finance.documents import amount, draft_review, extract_document, extract_pdf, render_page
from apps.finance.models import BankAccount, BankEntry, Expense, SettlementImport
from apps.finance.settlements import allocate, assess
from apps.finance.worker import process_imports, run_document
from apps.logistics.models import Courier
from apps.orders.models import Customer, Order
from apps.orders.services import financials
from tests.billing_fixtures import paid_workspace


def sample_pdf(headers=None, rows=None, ruled=True, pages=1):
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(750, 600))
    headers = headers or ["CN", "COD", "Freight", "Net Amount"]
    rows = rows or [
        ["00012345", "1,000.00", "100.00", "900.00"],
        ["AB-00002", "2,000.00", "200.00", "1,800.00"],
    ]
    for _ in range(pages):
        pdf.setFont("Helvetica", 10)
        pdf.drawString(30, 560, "Settlement Number SET-2026-001")
        pdf.drawString(30, 540, "Settlement Date 2026-09-10")
        pdf.drawString(30, 520, "Net Total 2,700.00")
        table = Table(
            [headers] + rows, colWidths=[680 / len(headers)] * len(headers), rowHeights=30
        )
        rules = [("FONT", (0, 0), (-1, -1), "Helvetica", 10)]
        if ruled:
            rules.append(("GRID", (0, 0), (-1, -1), 0.5, colors.black))
        table.setStyle(TableStyle(rules))
        table.wrapOn(pdf, 680, 400)
        table.drawOn(pdf, 30, 390)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


class DocumentTests(SimpleTestCase):
    def test_negative_row_net_is_preserved(self):
        result = extract_pdf(sample_pdf(rows=[["RETURN-01", "0.00", "200.00", "(200.00)"]]))
        self.assertEqual(result["tables"][0]["rows"][0]["values"][-1], "-200.00")

    def test_money_strict_and_parentheses(self):
        self.assertEqual(amount("(1,234.50)"), Decimal("-1234.50"))
        self.assertEqual(amount("0"), Decimal("0.00"))
        for value in ["", None, "1,23", "1.234,56", "NaN", "1e3", "12.345", 0.1, True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                amount(value)

    def test_ruled_layout_and_metadata(self):
        result = extract_pdf(sample_pdf())
        self.assertEqual(len(result["tables"]), 1)
        self.assertEqual(
            result["tables"][0]["rows"][0]["values"], ["00012345", "1000.00", "100.00", "900.00"]
        )
        review = draft_review(result)
        self.assertEqual(review["reference"], "SET-2026-001")
        self.assertEqual(review["date"], "2026-09-10")
        self.assertEqual(review["declared_net"], "2700.00")
        self.assertFalse(review["update_costs"])

    def test_reordered_aliases_not_courier_coordinates(self):
        headers = ["Net Payable", "Consignment", "Shipping Charges", "COD Amount", "WHT"]
        rows = [
            ["880.00", "CN-00901", "100.00", "1000.00", "20.00"],
            ["1760.00", "CN-00902", "200.00", "2000.00", "40.00"],
        ]
        result = extract_pdf(sample_pdf(headers, rows))
        table = result["tables"][0]
        self.assertEqual(
            [c["role"] for c in table["columns"]], ["net", "tracking", "fee", "gross", "unknown"]
        )
        self.assertEqual(table["rows"][1]["values"][0], "1760.00")

    def test_borderless_layout(self):
        result = extract_pdf(sample_pdf(["AWB", "COD", "Freight", "Payable"], ruled=False))
        self.assertEqual(len(result["tables"]), 1)
        self.assertEqual(len(result["tables"][0]["rows"]), 2)
        self.assertEqual(
            result["tables"][0]["rows"][1]["values"], ["AB-00002", "2000.00", "200.00", "1800.00"]
        )

    def test_multi_page_and_repeated_headers(self):
        result = extract_pdf(sample_pdf(pages=2))
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(sum(len(t["rows"]) for t in result["tables"]), 4)
        self.assertEqual(result["tables"][1]["rows"][0]["page"], 2)

    def test_no_table_is_manual_review_not_successful_posting(self):
        output = io.BytesIO()
        pdf = canvas.Canvas(output)
        pdf.showPage()
        pdf.save()
        result = extract_pdf(output.getvalue())
        self.assertEqual(result["tables"], [])
        self.assertTrue(any("no readable text" in w for w in result["warnings"]))

    def test_invalid_pdf_and_page_limits(self):
        with self.assertRaises(ValueError):
            extract_pdf(b"not a pdf")
        with self.assertRaises(ValueError):
            extract_pdf(sample_pdf(pages=26))

    def test_private_preview_and_subprocess(self):
        blob = sample_pdf()
        self.assertTrue(render_page(blob, 1).startswith(b"\x89PNG"))
        self.assertTrue(run_document(blob)["tables"])
        with self.assertRaises(ValueError):
            render_page(blob, 9)

    def test_tcs_csv_profile_keeps_payment_status_separate_from_delivery(self):
        source = (
            b"CN By Courier,Payment Status,Order No,Amount Paid,Delivery Charges\n"
            b"TCS/000123,N,ORDER-123,0,150\n"
        )
        result = extract_document(source, "tcs-payment.csv", "TCS")
        self.assertEqual(result["document_kind"], "delimited")
        self.assertEqual(result["parser"]["profile"], "tcs")
        self.assertEqual(
            [column["role"] for column in result["tables"][0]["columns"]],
            ["tracking", "info", "order_ref", "gross", "fee"],
        )
        self.assertEqual(result["tables"][0]["rows"][0]["values"][0], "TCS/000123")

    def test_postex_profile_uses_reserve_not_customer_cod_as_settlement_gross(self):
        headers = [
            "Tracking Number",
            "COD Amount",
            "Reserve Amount",
            "Shipping Charges",
            "GST",
            "Deduction (4%)",
            "Net Amount",
        ]
        result = extract_pdf(
            sample_pdf(
                headers,
                [["I1I03297079001", "1299", "1299", "221", "33.15", "51.96", "992.89"]],
            ),
            courier_hint="PostEx",
        )
        self.assertEqual(result["parser"]["profile"], "postex")
        self.assertEqual(
            [column["role"] for column in result["tables"][0]["columns"]],
            ["tracking", "info", "gross", "fee", "fee", "deduction", "net"],
        )

    def test_blueex_xlsx_statement_is_read_with_source_sheet_evidence(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Paid COD"
        sheet.append(["CNNO", "Reference", "Amount Received", "Blue-Ex Charges"])
        sheet.append(["BX-000123", "ORDER-123", 1000, 100])
        output = io.BytesIO()
        workbook.save(output)
        result = extract_document(output.getvalue(), "blueex.xlsx", "BlueEx")
        self.assertEqual(result["document_kind"], "xlsx")
        self.assertEqual(result["pages"][0]["sheet"], "Paid COD")
        self.assertEqual(
            [column["role"] for column in result["tables"][0]["columns"]],
            ["tracking", "order_ref", "gross", "fee"],
        )
        self.assertEqual(result["tables"][0]["rows"][0]["values"][2], "1000.00")

    def test_exact_allocations(self):
        self.assertEqual(
            allocate(Decimal(".10"), [Decimal(1)] * 3),
            [Decimal(".04"), Decimal(".03"), Decimal(".03")],
        )
        self.assertEqual(
            sum(allocate(Decimal("-146.00"), [Decimal(1850), Decimal(1800)])), Decimal("-146.00")
        )
        with self.assertRaises(ValueError):
            allocate(Decimal(1), [Decimal(0)])


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SettlementTests(APITestCase):
    def test_negative_return_settlement_records_bank_debit(self):
        review = self.statement.review
        review["tables"][0]["rows"] = [review["tables"][0]["rows"][0]]
        review["tables"][0]["rows"][0]["values"] = ["00012345", "0", "200", "-200"]
        review["declared_net"] = "-200.00"
        self.statement.save()
        self.confirm()
        wrong, _ = self.receipt(amount="200")
        self.assertEqual(wrong.status_code, 400)
        debit, _ = self.receipt(amount="-200")
        self.assertEqual(debit.status_code, 201, debit.data)
        self.assertEqual(self.client.get(self.endpoint()).data["remaining_amount"], "0.00")

    def test_corrected_draft_retains_prior_immutable_history(self):
        self.confirm()
        response = self.client.post(
            self.endpoint("reverse"),
            {"revision": self.statement.revision, "reason": "Reclassify tax"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.statement.refresh_from_db()
        revised = self.client.post(
            self.endpoint("revise"), {"revision": self.statement.revision}, format="json"
        )
        self.assertEqual(revised.status_code, 201, revised.data)
        draft = SettlementImport.objects.get(pk=revised.data["id"])
        self.assertEqual(draft.replaces, self.statement)
        self.assertFalse(draft.review["source_confirmed"])
        self.assertEqual(self.statement.cost_updates.count(), 2)
        draft.review.update(source_confirmed=True, ownership_confirmed=True)
        draft.save()
        result = self.client.post(
            f"/api/settlement-imports/{draft.pk}/confirm/", {"revision": 1}, format="json"
        )
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(self.statement.cost_updates.count(), 2)
        self.assertEqual(draft.cost_updates.count(), 2)

    @patch("apps.finance.worker.close_old_connections")
    def test_approved_mapping_learned_only_in_same_workspace_and_courier(self, _close):
        from apps.finance.models import SettlementMapping

        self.confirm()
        self.assertEqual(SettlementMapping.objects.count(), 1)
        incoming = copy.deepcopy(self.statement.extracted)
        incoming["tables"][0]["columns"][2]["role"] = "unknown"
        pending = SettlementImport.objects.create(
            workspace=self.workspace,
            courier=self.courier,
            source=b"%PDF-",
            digest="c" * 64,
            filename="next.pdf",
        )
        with patch("apps.finance.worker.run_document", return_value=incoming):
            process_imports()
        pending.refresh_from_db()
        self.assertEqual(pending.review["tables"][0]["columns"][2]["role"], "fee")
        self.assertEqual(pending.extracted["tables"][0]["columns"][2]["role"], "unknown")
        self.assertFalse(pending.review["source_confirmed"])

    def test_analytics_uses_confirmed_cost_without_changing_fifo(self):
        self.confirm()
        response = self.client.get("/api/analytics/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["totals"]["courier"], Decimal("300.00"))
        self.assertEqual(response.data["couriers"][0]["cost"], Decimal("300.00"))

    def test_newer_cost_snapshot_blocks_out_of_order_reversal(self):
        self.confirm()
        second = SettlementImport.objects.create(
            workspace=self.workspace,
            courier=self.courier,
            source=b"%PDF-",
            digest="d" * 64,
            filename="latest.pdf",
            status="REVIEW",
            review={**self.statement.review, "reference": "LATEST-002", "replace_costs": True},
        )
        confirmed = self.client.post(
            f"/api/settlement-imports/{second.pk}/confirm/", {"revision": 1}, format="json"
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        result = self.client.post(
            self.endpoint("reverse"),
            {"revision": self.statement.revision, "reason": "Wrong order"},
            format="json",
        )
        self.assertEqual(result.status_code, 400)
        self.orders[0].refresh_from_db()
        self.assertEqual(self.orders[0].actual_courier_cost_source, second.pk)

    def setUp(self):
        self.workspace = paid_workspace(name="Settlement tests")
        self.user = User.objects.create_user(
            username="finance",
            email="finance@example.test",
            password="Testing2026!",
            workspace=self.workspace,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        self.client.force_authenticate(self.user)
        self.courier = Courier.objects.create(
            workspace=self.workspace, name="Any Courier", code="ANY", base_rate="150.00"
        )
        customer = Customer.objects.create(
            workspace=self.workspace,
            name="Test customer",
            phone="03000000001",
            city="Kasur",
            address="Test",
        )
        self.orders = [
            Order.objects.create(
                workspace=self.workspace,
                customer=customer,
                courier=self.courier,
                number=f"ORDER-{i}",
                tracking_id=tracking,
                subtotal=gross,
                product_cost="500.00",
                courier_cost="150.00",
                status="DELIVERED",
                dispatched_at=timezone.now(),
                finalized_at=timezone.now(),
                courier_snapshot={"courier": "Any Courier"},
                customer_snapshot={"city": "Kasur"},
            )
            for i, (tracking, gross) in enumerate(
                [("00012345", "1000.00"), ("AB-00002", "2000.00")]
            )
        ]
        extracted = extract_pdf(sample_pdf())
        review = draft_review(extracted)
        review.update(ownership_confirmed=True, source_confirmed=True, update_costs=True)
        self.statement = SettlementImport.objects.create(
            workspace=self.workspace,
            courier=self.courier,
            filename="test.pdf",
            digest="a" * 64,
            source=sample_pdf(),
            status="REVIEW",
            extracted=extracted,
            review=review,
        )
        self.account = BankAccount.objects.create(
            workspace=self.workspace,
            name="Test bank",
            opening_balance="100.00",
            opening_date="2026-01-01",
        )

    def endpoint(self, action=""):
        return f"/api/settlement-imports/{self.statement.pk}/{action + '/' if action else ''}"

    def test_expense_partial_payment_idempotency_and_reversal(self):
        expense = Expense.objects.create(
            workspace=self.workspace, name="Rent", amount="500.00", date=timezone.localdate()
        )
        data = {
            "account": str(self.account.pk),
            "expense": str(expense.pk),
            "amount": "-200.00",
            "date": str(timezone.localdate()),
            "reference": "RENT-1",
            "request_key": str(uuid4()),
        }
        result = self.client.post("/api/bank-entries/", data, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        retry = self.client.post("/api/bank-entries/", data, format="json")
        self.assertEqual(retry.data["id"], result.data["id"])
        detail = self.client.get(f"/api/expenses/{expense.pk}/").data
        self.assertEqual(Decimal(detail["bank_paid"]), Decimal("200"))
        self.assertEqual(Decimal(detail["bank_remaining"]), Decimal("300"))
        self.assertEqual(
            self.client.post(
                "/api/bank-entries/",
                {**data, "request_key": str(uuid4()), "reference": "RENT-2", "amount": "-301"},
                format="json",
            ).status_code,
            400,
        )
        reverse = self.client.post(
            f"/api/bank-entries/{result.data['id']}/reverse/",
            {"date": data["date"], "reason": "Wrong payment", "request_key": str(uuid4())},
            format="json",
        )
        self.assertEqual(reverse.status_code, 200, reverse.data)
        detail = self.client.get(f"/api/expenses/{expense.pk}/").data
        self.assertEqual(Decimal(detail["bank_paid"]), Decimal("0"))
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal("500"))

    def test_expense_payment_rejects_other_workspace_credit_and_dual_link(self):
        expense = Expense.objects.create(
            workspace=self.workspace, name="Rent", amount="500", date=timezone.localdate()
        )
        data = {
            "account": str(self.account.pk),
            "expense": str(expense.pk),
            "amount": "100",
            "date": str(timezone.localdate()),
            "reference": "TEST",
            "request_key": str(uuid4()),
        }
        self.assertEqual(
            self.client.post("/api/bank-entries/", data, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                "/api/bank-entries/",
                {**data, "amount": "-100", "statement": str(self.statement.pk)},
                format="json",
            ).status_code,
            400,
        )
        other = paid_workspace(name="Other expense workspace")
        expense.workspace = other
        expense.save()
        self.assertEqual(
            self.client.post(
                "/api/bank-entries/", {**data, "amount": "-100"}, format="json"
            ).status_code,
            400,
        )

    def confirm(self):
        response = self.client.post(
            self.endpoint("confirm"), {"revision": self.statement.revision}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.statement.refresh_from_db()
        return response

    def receipt(self, **kwargs):
        data = {
            "account": str(self.account.pk),
            "statement": str(self.statement.pk),
            "amount": "2700.00",
            "date": "2026-09-10",
            "reference": "BANK-TXN-1",
            "request_key": str(uuid4()),
        }
        data.update(kwargs)
        return self.client.post("/api/bank-entries/", data, format="json"), data

    def test_cost_confirmation_keeps_estimates_status_and_cash_unchanged(self):
        result = self.confirm()
        self.orders[0].refresh_from_db()
        self.assertEqual(self.orders[0].courier_cost, Decimal("150.00"))
        self.assertEqual(self.orders[0].actual_courier_cost, Decimal("100.00"))
        self.assertEqual(financials(self.orders[0])["courier_cost"], "100.00")
        self.assertEqual(financials(self.orders[0])["profit"], "400.00")
        self.assertEqual(self.orders[0].status, "DELIVERED")
        self.assertEqual(BankEntry.objects.count(), 0)
        self.assertEqual(result.data["remaining_amount"], "2700.00")

    def test_invalid_totals_never_post(self):
        self.statement.review["declared_net"] = "2700.01"
        self.statement.save()
        response = self.client.post(self.endpoint("confirm"), {"revision": 1}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.statement.cost_updates.exists())
        self.assertFalse(BankEntry.objects.exists())

    def test_unknown_columns_block_instead_of_guessing(self):
        self.statement.review["tables"][0]["columns"][2]["role"] = "unknown"
        result = assess(self.statement)
        self.assertFalse(result["valid"])
        self.assertTrue(any("unknown" in e for e in result["errors"]))

    def test_missing_amount_is_not_zero(self):
        self.statement.review["tables"][0]["rows"][0]["values"][2] = ""
        self.assertFalse(assess(self.statement)["valid"])

    def test_duplicate_tracking_is_not_silently_deduplicated(self):
        table = self.statement.review["tables"][0]
        table["rows"].append(copy.deepcopy(table["rows"][0]))
        self.assertTrue(any("duplicate tracking" in e for e in assess(self.statement)["errors"]))

    def test_shared_expense_allocation_exact_and_labelled(self):
        review = self.statement.review
        review["adjustments"] = [
            {"label": "Monthly fee", "amount": "-10.01", "kind": "expense", "allocation": "gross"}
        ]
        review["declared_net"] = "2689.99"
        result = assess(self.statement)
        self.assertTrue(result["valid"], result)
        self.assertEqual(sum(Decimal(r["cost"]) for r in result["rows"]), Decimal("310.01"))
        self.assertTrue(all(r["basis"] == "ALLOCATED" for r in result["rows"]))
        review["adjustments"][0]["allocation"] = "none"
        self.assertFalse(assess(self.statement)["valid"])
        review["update_costs"] = False
        self.assertTrue(assess(self.statement)["valid"])

    def test_withholding_changes_payable_not_expense(self):
        self.statement.review["adjustments"] = [
            {"label": "Withheld", "amount": "-100.00", "kind": "nonexpense", "allocation": "none"}
        ]
        self.statement.review["declared_net"] = "2600.00"
        result = assess(self.statement)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["rows"][0]["cost"], "100.00")

    def test_independent_summary_check(self):
        self.statement.review["checks"] = [{"label": "Freight", "amount": "300.01"}]
        self.assertFalse(assess(self.statement)["valid"])
        self.statement.review["checks"][0]["amount"] = "300.00"
        self.assertTrue(assess(self.statement)["valid"])

    def test_no_exact_match_requires_explicit_external_selection(self):
        self.orders[0].tracking_id = "OTHER"
        self.orders[0].save()
        self.assertFalse(assess(self.statement)["valid"])
        self.statement.review["tables"][0]["rows"][0]["external"] = True
        result = assess(self.statement)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["net"], "2700.00")

    def test_exact_order_reference_can_match_when_a_statement_has_no_tracking_column(self):
        table = self.statement.review["tables"][0]
        table["columns"][0] = {"label": "Order Ref", "role": "order_ref"}
        for row, order in zip(table["rows"], self.orders):
            row["values"][0] = order.number
        result = assess(self.statement)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["rows"][0]["match_basis"], "order_reference")
        self.confirm()
        self.orders[0].refresh_from_db()
        self.assertEqual(self.orders[0].actual_courier_cost, Decimal("100.00"))

    def test_unpaid_tcs_style_payment_row_cannot_be_confirmed_as_a_cpr(self):
        table = self.statement.review["tables"][0]
        table["columns"].append({"label": "Payment Status", "role": "info"})
        for row in table["rows"]:
            row["values"].append("N")
        result = assess(self.statement)
        self.assertFalse(result["valid"])
        self.assertTrue(any("marks this shipment as unpaid" in error for error in result["errors"]))

    def test_cannot_hide_matching_order_as_external(self):
        self.statement.review["tables"][0]["rows"][0]["external"] = True
        self.assertFalse(assess(self.statement)["valid"])

    def test_courier_scope_no_cross_courier_match(self):
        other = Courier.objects.create(
            workspace=self.workspace, name="Other", code="OTHER", base_rate="150.00"
        )
        self.orders[0].courier = other
        self.orders[0].save()
        self.assertFalse(assess(self.statement)["valid"])

    def test_permissions_and_private_sources(self):
        for role in ["staff", "viewer"]:
            self.user.role = role
            self.user.save()
            for url in [
                self.endpoint(),
                self.endpoint("source"),
                "/api/bank-accounts/",
                "/api/bank-entries/",
            ]:
                self.assertEqual(self.client.get(url).status_code, 403)
        self.user.role = "owner"
        self.user.workspace = paid_workspace(name="Other workspace")
        self.user.save()
        self.assertEqual(self.client.get(self.endpoint("source")).status_code, 404)
        self.assertEqual(
            self.client.post(self.endpoint("confirm"), {"revision": 1}).status_code, 404
        )
        self.assertEqual(self.client.get("/api/bank-accounts/").data["count"], 0)

    def test_private_original_download(self):
        result = self.client.get(self.endpoint("source"))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertIn("attachment", result["Content-Disposition"])
        self.assertTrue(result.content.startswith(b"%PDF"))

    def test_stale_review_cannot_confirm_or_save(self):
        saved = self.client.post(
            self.endpoint("save-review"),
            {"revision": 1, "review": self.statement.review},
            format="json",
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.data["revision"], 2)
        self.assertEqual(
            self.client.post(self.endpoint("confirm"), {"revision": 1}, format="json").status_code,
            400,
        )

    def test_duplicate_file_returns_existing_without_queue(self):
        from hashlib import sha256

        self.statement.digest = sha256(bytes(self.statement.source)).hexdigest()
        self.statement.save()
        response = self.client.post(
            "/api/settlement-imports/",
            {
                "courier": str(self.courier.pk),
                "file": SimpleUploadedFile(
                    "duplicate.pdf", bytes(self.statement.source), content_type="application/pdf"
                ),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["duplicate"])
        self.assertEqual(SettlementImport.objects.count(), 1)

    def test_same_cpr_reference_cannot_confirm_twice(self):
        self.confirm()
        second = SettlementImport.objects.create(
            workspace=self.workspace,
            courier=self.courier,
            filename="copy.pdf",
            digest="b" * 64,
            source=b"%PDF-",
            status="REVIEW",
            review={**self.statement.review, "replace_costs": True},
        )
        result = self.client.post(
            f"/api/settlement-imports/{second.pk}/confirm/", {"revision": 1}, format="json"
        )
        self.assertEqual(result.status_code, 400)

    def test_partial_receipts_idempotent_and_overpayment_blocked(self):
        self.confirm()
        result, data = self.receipt(amount="1000.00")
        self.assertEqual(result.status_code, 201, result.data)
        repeat = self.client.post("/api/bank-entries/", data, format="json")
        self.assertEqual(result.data["id"], repeat.data["id"])
        self.assertEqual(BankEntry.objects.count(), 1)
        result, _ = self.receipt(amount="1700.01", reference="BANK-TXN-2")
        self.assertEqual(result.status_code, 400)
        result, _ = self.receipt(amount="1700.00", reference="BANK-TXN-2")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.client.get(self.endpoint()).data["remaining_amount"], "0.00")
        self.assertEqual(self.client.get("/api/bank-accounts/summary/").data["balance"], "2800.00")

    def test_bank_receipt_needs_confirmed_statement_and_tenant_account(self):
        self.assertEqual(self.receipt()[0].status_code, 400)
        self.confirm()
        other = BankAccount.objects.create(
            workspace=paid_workspace(name="Other"),
            name="Other",
            opening_date="2026-01-01",
        )
        self.assertEqual(self.receipt(account=str(other.pk))[0].status_code, 400)

    def test_duplicate_bank_reference_rejected(self):
        self.confirm()
        self.assertEqual(self.receipt(amount="1000")[0].status_code, 201)
        self.assertEqual(self.receipt(amount="1000")[0].status_code, 400)

    def test_reversal_preserves_history_and_restores_estimate(self):
        self.confirm()
        receipt, _ = self.receipt()
        blocked = self.client.post(
            self.endpoint("reverse"),
            {"revision": self.statement.revision, "reason": "Correction"},
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)
        reversed_entry = self.client.post(
            f"/api/bank-entries/{receipt.data['id']}/reverse/",
            {"date": "2026-09-11", "reason": "Wrong entry", "request_key": str(uuid4())},
            format="json",
        )
        self.assertEqual(reversed_entry.status_code, 200, reversed_entry.data)
        response = self.client.post(
            self.endpoint("reverse"),
            {"revision": self.statement.revision, "reason": "Correction"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.orders[0].refresh_from_db()
        self.assertIsNone(self.orders[0].actual_courier_cost)
        self.assertEqual(self.statement.cost_updates.count(), 2)
        self.assertEqual(BankEntry.objects.count(), 2)

    def test_confirmed_import_immutable(self):
        self.confirm()
        result = self.client.post(
            self.endpoint("save-review"),
            {"revision": self.statement.revision, "review": self.statement.review},
            format="json",
        )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.delete(self.endpoint()).status_code, 405)

    def test_injected_nonstring_and_bad_shape_rejected(self):
        for value in [None, [], {"tables": [{}]}, {"tables": "bad"}, {"update_costs": "false"}]:
            result = self.client.post(
                self.endpoint("save-review"), {"revision": 1, "review": value}, format="json"
            )
            self.assertEqual(result.status_code, 400, result.data)

    @patch("apps.finance.worker.close_old_connections")
    def test_worker_claim_and_failure_not_silent(self, _close):
        self.statement.status = "QUEUED"
        self.statement.save()
        with patch("apps.finance.worker.run_document", side_effect=ValueError("private details")):
            self.assertEqual(process_imports(), 1)
        self.statement.refresh_from_db()
        self.assertEqual(self.statement.status, "ERROR")
        self.assertNotIn("private details", self.statement.error)

    @patch("apps.finance.worker.close_old_connections")
    def test_worker_populates_review_but_no_financial_mutations(self, _close):
        self.statement.status = "QUEUED"
        self.statement.save()
        with patch("apps.finance.worker.run_document", return_value=self.statement.extracted):
            self.assertEqual(process_imports(), 1)
        self.statement.refresh_from_db()
        self.assertEqual(self.statement.status, "REVIEW")
        self.assertFalse(self.statement.review["source_confirmed"])
        self.assertFalse(BankEntry.objects.exists())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
@skipUnlessDBFeature("has_select_for_update")
class SettlementConcurrencyTests(APITransactionTestCase):
    def setUp(self):
        SettlementTests.setUp(self)

    def parallel_posts(self, url, payload):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import connections

        barrier = Barrier(2)

        def send(_):
            client = APIClient()
            client.force_authenticate(self.user)
            try:
                barrier.wait(timeout=10)
                return client.post(url, payload, format="json").status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(send, range(2)))

    def test_concurrent_confirmation_posts_cost_only_once(self):
        codes = self.parallel_posts(
            f"/api/settlement-imports/{self.statement.pk}/confirm/", {"revision": 1}
        )
        self.assertEqual(sorted(codes), [200, 400])
        self.assertEqual(self.statement.cost_updates.count(), 2)
        self.assertEqual(BankEntry.objects.count(), 0)

    def test_concurrent_bank_requests_create_one_entry(self):
        self.client.post(
            f"/api/settlement-imports/{self.statement.pk}/confirm/", {"revision": 1}, format="json"
        )
        data = {
            "account": str(self.account.pk),
            "statement": str(self.statement.pk),
            "amount": "2700.00",
            "date": "2026-09-10",
            "reference": "CONCURRENT-1",
            "request_key": str(uuid4()),
        }
        self.assertEqual(self.parallel_posts("/api/bank-entries/", data), [201, 201])
        self.assertEqual(BankEntry.objects.count(), 1)
