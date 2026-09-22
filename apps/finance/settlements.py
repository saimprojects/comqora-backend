"""Decimal-only reconciliation and transactional, explicitly approved cash/cost posting."""

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Replace, Upper
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.api import audit
from apps.core.models import Workspace
from apps.orders.models import Order

from .documents import (
    FINANCIAL_ROLES,
    IDENTIFIER_ROLES,
    MAX_ROWS,
    ROLES,
    amount,
    layout_signature,
)
from .models import BankAccount, BankEntry, SettlementCost, SettlementImport, SettlementMapping

ZERO = Decimal("0.00")


def validate_structure(review):
    if not isinstance(review, dict) or len(str(review)) > 1500000:
        raise ValidationError("Review must be a JSON object of at most 1.5 MB.")
    for name in ["reference", "date", "currency", "declared_net", "declared_gross", "notes"]:
        if not isinstance(review.get(name, ""), str) or len(review.get(name, "")) > (
            2000 if name == "notes" else 120
        ):
            raise ValidationError(f"Invalid {name}.")
    for name in ["update_costs", "replace_costs", "ownership_confirmed", "source_confirmed"]:
        if type(review.get(name, False)) is not bool:
            raise ValidationError(f"{name} must be true or false.")
    for name in ["tables", "adjustments", "checks"]:
        if not isinstance(review.get(name, []), list) or len(review.get(name, [])) > 100:
            raise ValidationError(f"Invalid {name} list.")
    count = 0
    for table in review.get("tables", []):
        if (
            not isinstance(table, dict)
            or not isinstance(table.get("columns"), list)
            or not isinstance(table.get("rows"), list)
        ):
            raise ValidationError("Each table needs columns and rows.")
        cols = table["columns"]
        if not 1 <= len(cols) <= 30:
            raise ValidationError("Each table needs 1–30 columns.")
        for col in cols:
            if (
                not isinstance(col, dict)
                or col.get("role") not in ROLES
                or not isinstance(col.get("label"), str)
                or not 1 <= len(col["label"]) <= 160
            ):
                raise ValidationError("Invalid column label or role.")
        for row in table["rows"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("values"), list)
                or len(row["values"]) != len(cols)
                or any(not isinstance(v, str) or len(v) > 500 for v in row["values"])
                or type(row.get("external", False)) is not bool
            ):
                raise ValidationError(
                    "Invalid row cells. Amounts and tracking numbers must be strings."
                )
        count += len(table["rows"])
    if count > MAX_ROWS:
        raise ValidationError("At most 1,000 rows may be reviewed at once.")
    for item in review.get("adjustments", []):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("label"), str)
            or not 1 <= len(item["label"]) <= 160
            or item.get("kind") not in {"expense", "nonexpense"}
            or item.get("allocation") not in {"none", "equal", "gross"}
        ):
            raise ValidationError(
                "Each shared adjustment needs a label, classification and allocation rule."
            )
        try:
            amount(item.get("amount"))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    for item in review.get("checks", []):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("label"), str)
            or len(item["label"]) > 160
        ):
            raise ValidationError("Invalid column total check.")
        try:
            amount(item.get("amount"))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    return review


def allocate(total, weights):
    """Largest remainder allocation in pennies; never float or lost rounding cents."""
    weight_sum = sum(weights, ZERO)
    if weight_sum <= 0:
        raise ValueError("Allocation weights must total more than zero.")
    magnitude = abs(total)
    raw = [magnitude * weight / weight_sum for weight in weights]
    rounded = [n.quantize(Decimal(".01"), rounding=ROUND_DOWN) for n in raw]
    cents = int((magnitude - sum(rounded, ZERO)) * 100)
    priority = sorted(range(len(raw)), key=lambda i: (-(raw[i] - rounded[i]), i))
    for index in priority[:cents]:
        rounded[index] += Decimal(".01")
    return [n if total >= 0 else -n for n in rounded]


def identifier_key(value):
    """Match carrier IDs safely across casing and harmless portal separators."""
    return re.sub(r"[\s/_-]+", "", str(value or "")).upper()


def _database_identifier(field):
    expression = Upper(field)
    for token in (" ", "-", "_", "/"):
        expression = Replace(expression, Value(token), Value(""))
    return expression


def _order_maps(statement, tracking_keys, reference_keys):
    """Fetch only candidate orders, then retain a collision-aware exact map."""
    if not tracking_keys and not reference_keys:
        return defaultdict(list), defaultdict(list)
    candidates = (
        Order.objects.filter(workspace_id=statement.workspace_id, courier_id=statement.courier_id)
        .annotate(
            normalized_tracking=_database_identifier(F("tracking_id")),
            normalized_number=_database_identifier(F("number")),
        )
        .filter(Q(normalized_tracking__in=tracking_keys) | Q(normalized_number__in=reference_keys))
    )
    tracking, reference = defaultdict(list), defaultdict(list)
    for order in candidates:
        if order.normalized_tracking:
            tracking[order.normalized_tracking].append(order)
        if order.normalized_number:
            reference[order.normalized_number].append(order)
    return tracking, reference


def _unsettled_payment(row, columns):
    """A TCS-style payment-status N must never become a settled CPR row."""
    status_values = [
        value.strip().lower()
        for column, value in zip(columns, row["values"])
        if "payment status" in " ".join(column["label"].lower().split())
    ]
    return any(value in {"n", "no", "unpaid", "pending", "not paid"} for value in status_values)


def assess(statement, review=None):
    review = validate_structure(review if review is not None else statement.review)
    errors, warnings, rows = [], [], []
    labels = defaultdict(lambda: ZERO)
    seen = set()
    tracking_keys = {
        identifier_key(row["values"][column_index])
        for table in review.get("tables", [])
        for column_index, column in enumerate(table["columns"])
        if column["role"] == "tracking"
        for row in table["rows"]
        if identifier_key(row["values"][column_index])
    }
    reference_keys = {
        identifier_key(row["values"][column_index])
        for table in review.get("tables", [])
        for column_index, column in enumerate(table["columns"])
        if column["role"] == "order_ref"
        for row in table["rows"]
        if identifier_key(row["values"][column_index])
    }
    tracking_orders, reference_orders = _order_maps(statement, tracking_keys, reference_keys)
    for ti, table in enumerate(review.get("tables", [])):
        roles = [col["role"] for col in table["columns"]]
        counts = Counter(roles)
        if (
            counts["tracking"] > 1
            or counts["order_ref"] > 1
            or not any(counts[role] for role in IDENTIFIER_ROLES)
            or counts["gross"] != 1
            or counts["net"] > 1
        ):
            errors.append(
                f"Table {ti + 1}: map one tracking and/or one order reference, exactly one settlement gross column, and at most one net column."
            )
            continue
        if "unknown" in roles:
            errors.append(f"Table {ti + 1}: classify every unknown column (including taxes).")
            continue
        for ri, row in enumerate(table["rows"]):
            key = f"{ti + 1}.{ri + 1}"
            tracking = row["values"][roles.index("tracking")].strip() if counts["tracking"] else ""
            order_reference = (
                row["values"][roles.index("order_ref")].strip() if counts["order_ref"] else ""
            )
            identifier = tracking or order_reference
            if not identifier or len(identifier) > 120:
                errors.append(f"Row {key}: shipment identifier is missing or too long.")
                continue
            duplicate_key = (
                f"tracking:{identifier_key(tracking)}"
                if tracking
                else f"order:{identifier_key(order_reference)}"
            )
            if duplicate_key in seen:
                identifier_label = "tracking" if tracking else "order reference"
                errors.append(
                    f"Row {key}: duplicate {identifier_label} {identifier}; combine/explain duplicate lines before posting."
                )
                continue
            seen.add(duplicate_key)
            if _unsettled_payment(row, table["columns"]):
                errors.append(
                    f"Row {key}: the courier marks this shipment as unpaid. Remove it from this settlement review instead of treating delivery as payment."
                )
            sums = defaultdict(lambda: ZERO)
            invalid = False
            for col, value in zip(table["columns"], row["values"]):
                if col["role"] not in FINANCIAL_ROLES:
                    continue
                try:
                    parsed = amount(value)
                    if parsed < 0 and col["role"] != "net":
                        raise ValueError(
                            "Use a positive column amount and choose deduction or credit as its role."
                        )
                    sums[col["role"]] += parsed
                    labels[" ".join(col["label"].lower().split())] += parsed
                except ValueError as exc:
                    errors.append(f"Row {key}, {col['label']}: {exc}")
                    invalid = True
            if invalid:
                continue
            calculated = (
                sums["gross"]
                - sums["fee"]
                - sums["deduction"]
                + sums["credit"]
                + sums["fee_credit"]
            )
            if counts["net"] and sums["net"] != calculated:
                errors.append(
                    f"Row {key}: calculated net {calculated:.2f} does not equal reported net {sums['net']:.2f}."
                )
            reported_net = sums["net"] if counts["net"] else calculated
            tracking_matches = tracking_orders.get(identifier_key(tracking), []) if tracking else []
            reference_matches = (
                reference_orders.get(identifier_key(order_reference), []) if order_reference else []
            )
            order = None
            match_basis = ""
            if len(tracking_matches) == 1:
                order = tracking_matches[0]
                match_basis = "tracking"
            if len(reference_matches) == 1:
                reference_order = reference_matches[0]
                if order and order.pk != reference_order.pk:
                    errors.append(
                        f"Row {key}: tracking and order reference point to different orders. Correct the source values before posting."
                    )
                    order = None
                    match_basis = ""
                elif not order:
                    order = reference_order
                    match_basis = "order_reference"
                else:
                    match_basis = "tracking_and_order_reference"
            external = row.get("external", False)
            if external and (tracking_matches or reference_matches):
                errors.append(
                    f"Row {key}: a matching order exists; it cannot be marked outside the platform."
                )
            if not external and not order:
                if len(tracking_matches) > 1 or len(reference_matches) > 1:
                    detail = "multiple matching orders"
                else:
                    detail = "no exact order match for this courier"
                errors.append(
                    f"Row {key}: {detail}. Resolve it, or explicitly mark an absent order as outside the platform."
                )
            if external:
                order = None
                match_basis = "external"
            if external:
                warnings.append(
                    f"Row {key}: included in settlement, excluded from platform order-profit updates."
                )
            if (
                order
                and order.actual_courier_cost is not None
                and review.get("update_costs")
                and not review.get("replace_costs")
            ):
                errors.append(
                    f"Row {key}: already has a confirmed cost. Explicitly approve replacing a complete cost snapshot."
                )
            rows.append(
                {
                    "key": key,
                    "tracking": identifier,
                    "identifier": identifier,
                    "identifier_kind": "tracking" if tracking else "order_reference",
                    "source_tracking": tracking,
                    "source_order_reference": order_reference,
                    "match_basis": match_basis,
                    "order_id": str(order.pk) if order else None,
                    "order_number": order.number if order else None,
                    "gross": sums["gross"],
                    "net": reported_net,
                    "cost": sums["fee"] - sums["fee_credit"],
                    "basis": "CPR",
                    "external": external,
                    "previous_cost": str(order.actual_courier_cost)
                    if order and order.actual_courier_cost is not None
                    else None,
                }
            )
    if not rows:
        errors.append("At least one valid shipment row is required.")
    gross = sum((r["gross"] for r in rows), ZERO)
    net = sum((r["net"] for r in rows), ZERO)
    unallocated = ZERO
    for item in review.get("adjustments", []):
        delta = amount(item["amount"])
        net += delta
        if item["kind"] == "expense":
            if item["allocation"] == "none":
                unallocated -= delta
                if delta and review.get("update_costs"):
                    errors.append(
                        f"{item['label']}: allocate this shared expense, or disable order-cost updates."
                    )
            elif rows:
                try:
                    shares = allocate(
                        -delta,
                        [r["gross"] if item["allocation"] == "gross" else Decimal(1) for r in rows],
                    )
                    for row, share in zip(rows, shares):
                        row["cost"] += share
                        if delta:
                            row["basis"] = "ALLOCATED"
                except ValueError as exc:
                    errors.append(f"{item['label']}: {exc}")
    if any(
        (r["cost"] < 0 and review.get("update_costs")) or abs(r["cost"]) > Decimal("9999999999.99")
        for r in rows
    ):
        errors.append("A complete courier cost must be between 0 and 9,999,999,999.99.")
    for check in review.get("checks", []):
        label = " ".join(check["label"].lower().split())
        if label not in labels or labels[label] != amount(check["amount"]):
            errors.append(
                f"Column total '{check['label']}' does not match {check['amount']} (extracted: {labels.get(label, 'not found')})."
            )
    for field, computed, optional in [
        ("declared_net", net, False),
        ("declared_gross", gross, True),
    ]:
        if optional and not review.get(field):
            continue
        try:
            if amount(review.get(field)) != computed:
                errors.append(
                    f"{field.replace('_', ' ').title()} does not reconcile: calculated {computed:.2f}."
                )
        except ValueError:
            errors.append(f"Enter a valid {field.replace('_', ' ')} from the statement.")
    if review.get("currency") != "PKR":
        errors.append(
            "This ledger accepts PKR only; foreign currency statements require conversion outside this importer."
        )
    if not review.get("reference", "").strip():
        errors.append("Enter the unique CPR/settlement reference (not the file name).")
    try:
        date.fromisoformat(review.get("date", ""))
    except ValueError:
        errors.append("Enter the statement date, not its printed/download date.")
    if not review.get("ownership_confirmed"):
        errors.append("Confirm the statement belongs to this workspace and selected courier.")
    if not review.get("source_confirmed"):
        errors.append(
            "Verify all source pages/sheets, row count, classifications and totals against the original statement."
        )
    if not review.get("update_costs"):
        warnings.append(
            "Only the settlement payable will be confirmed. Existing order costs stay unchanged."
        )
    if any(r["basis"] == "ALLOCATED" for r in rows):
        warnings.append(
            "Allocated amounts are exact to the chosen rule, not courier-reported per-order charges."
        )
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "gross": str(gross),
        "net": str(net),
        "unallocated_expense": str(unallocated),
        "rows": [{k: str(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in rows],
        "row_count": len(rows),
        "matched": sum(bool(r["order_id"]) for r in rows),
        "column_totals": {k: str(v) for k, v in labels.items()},
    }


def lock_statement(request, pk, revision=None):
    Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
    try:
        statement = (
            SettlementImport.objects.select_for_update()
            .defer("source")
            .get(pk=pk, workspace_id=request.user.workspace_id)
        )
    except SettlementImport.DoesNotExist as exc:
        raise ValidationError("Statement not found.") from exc
    if revision is not None and (type(revision) is not int or revision != statement.revision):
        raise ValidationError("This import changed in another tab. Reload before continuing.")
    return statement


@transaction.atomic
def confirm(request, pk, revision):
    statement = lock_statement(request, pk, revision)
    if statement.status != "REVIEW":
        raise ValidationError("Only a reviewed import can be confirmed.")
    result = assess(statement)
    if not result["valid"]:
        raise ValidationError({"detail": result["errors"]})
    review = statement.review
    reference = review["reference"].strip()
    key = " ".join(reference.upper().split())
    if SettlementImport.objects.filter(
        workspace=statement.workspace,
        courier=statement.courier,
        reference_key=key,
        status="CONFIRMED",
    ).exists():
        raise ValidationError(
            "This courier reference is already confirmed. Reverse the previous import before replacing it."
        )
    if review.get("update_costs"):
        for row in result["rows"]:
            if not row["order_id"]:
                continue
            order = Order.objects.select_for_update().get(
                pk=row["order_id"], workspace=statement.workspace
            )
            tracking_changed = row.get("source_tracking") and (
                identifier_key(order.tracking_id) != identifier_key(row["source_tracking"])
            )
            reference_changed = row.get("source_order_reference") and (
                identifier_key(order.number) != identifier_key(row["source_order_reference"])
            )
            if tracking_changed or reference_changed or order.courier_id != statement.courier_id:
                raise ValidationError(
                    "A matched shipment changed during confirmation. Reload and review the match again."
                )
            SettlementCost.objects.create(
                workspace=statement.workspace,
                statement=statement,
                order=order,
                amount=row["cost"],
                basis=row["basis"],
                previous_amount=order.actual_courier_cost,
                previous_basis=order.actual_courier_cost_basis,
                previous_source=order.actual_courier_cost_source,
            )
            order.actual_courier_cost = Decimal(row["cost"])
            order.actual_courier_cost_basis = row["basis"]
            order.actual_courier_cost_source = statement.pk
            order.save(
                update_fields=[
                    "actual_courier_cost",
                    "actual_courier_cost_basis",
                    "actual_courier_cost_source",
                    "updated_at",
                ]
            )
    statement.status = "CONFIRMED"
    statement.reference, statement.reference_key = reference, key
    statement.date, statement.net_amount = review["date"], result["net"]
    statement.confirmed_by, statement.confirmed_at = request.user, timezone.now()
    statement.confirmed_result = result
    statement.revision += 1
    statement.save()
    for table in review.get("tables", []):
        SettlementMapping.objects.update_or_create(
            workspace=statement.workspace,
            courier=statement.courier,
            signature=layout_signature(table["columns"]),
            defaults={"roles": [c["role"] for c in table["columns"]], "statement": statement},
        )
    audit(
        request,
        "Settlement.Confirmed",
        statement,
        {
            "reference": reference,
            "net": result["net"],
            "order_costs_updated": review.get("update_costs", False),
        },
    )
    return statement


@transaction.atomic
def reverse_statement(request, pk, revision, reason):
    statement = lock_statement(request, pk, revision)
    if statement.status != "CONFIRMED" or not reason.strip():
        raise ValidationError("A confirmed statement and reversal reason are required.")
    if statement.receipts.filter(reversal_of__isnull=True, reversal__isnull=True).exists():
        raise ValidationError("Reverse this statement's bank entries first.")
    for change in statement.cost_updates.select_related("order"):
        order = Order.objects.select_for_update().get(pk=change.order_id)
        if order.actual_courier_cost_source != statement.pk:
            raise ValidationError(
                "A newer statement changed this order's cost. Reverse newer statements first."
            )
        order.actual_courier_cost = change.previous_amount
        order.actual_courier_cost_basis = change.previous_basis
        order.actual_courier_cost_source = change.previous_source
        order.save(
            update_fields=[
                "actual_courier_cost",
                "actual_courier_cost_basis",
                "actual_courier_cost_source",
                "updated_at",
            ]
        )
    statement.status = "VOID"
    statement.revision += 1
    statement.save(update_fields=["status", "revision", "updated_at"])
    audit(request, "Settlement.Reversed", statement, {"reason": reason[:500]})
    return statement


@transaction.atomic
def record_cash(request, data):
    from .models import Expense

    if data.get("expense") and data.get("statement"):
        raise ValidationError("A payment cannot belong to both an expense and a CPR.")
    Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
    existing = BankEntry.objects.filter(
        workspace=request.user.workspace, request_key=data["request_key"]
    ).first()
    if existing:
        if (
            existing.amount != data["amount"]
            or str(existing.account_id) != str(data["account"])
            or existing.date != data["date"]
            or existing.reference != data["reference"]
            or str(existing.statement_id or "") != str(data.get("statement") or "")
            or str(existing.expense_id or "") != str(data.get("expense") or "")
        ):
            raise ValidationError("This request key was already used for a different entry.")
        return existing
    try:
        account = BankAccount.objects.get(pk=data["account"], workspace=request.user.workspace)
    except BankAccount.DoesNotExist as exc:
        raise ValidationError("Bank account not found in this workspace.") from exc
    if data["date"] < account.opening_date:
        raise ValidationError("Entry date cannot be before the account's opening balance date.")
    if BankEntry.objects.filter(
        account=account,
        reference__iexact=data["reference"],
        reversal_of__isnull=True,
        reversal__isnull=True,
    ).exists():
        raise ValidationError(
            "This bank reference is already recorded. Use its unique bank transaction reference."
        )
    statement = None
    if data.get("statement"):
        statement = lock_statement(request, data["statement"])
        if statement.status != "CONFIRMED":
            raise ValidationError("Confirm the CPR before recording its bank movement.")
        received = sum(statement.receipts.values_list("amount", flat=True), ZERO)
        remaining = statement.net_amount - received
        if (
            not remaining
            or (remaining > 0) != (data["amount"] > 0)
            or abs(data["amount"]) > abs(remaining)
        ):
            raise ValidationError(
                f"The remaining statement amount is {remaining:.2f}. Partial movements must use the same direction and cannot exceed it."
            )
    expense = None
    if data.get("expense"):
        try:
            expense = Expense.objects.select_for_update().get(
                pk=data["expense"], workspace=request.user.workspace
            )
        except Expense.DoesNotExist as exc:
            raise ValidationError("Expense not found in this workspace.") from exc
        paid = -sum(expense.payments.values_list("amount", flat=True), ZERO)
        if data["amount"] >= 0 or -data["amount"] > expense.amount - paid:
            raise ValidationError(
                "Expense payment must be money paid out and cannot exceed the unrecorded amount."
            )
    entry = BankEntry.objects.create(
        workspace=request.user.workspace,
        account=account,
        statement=statement,
        expense=expense,
        amount=data["amount"],
        date=data["date"],
        reference=data["reference"],
        notes=data.get("notes", ""),
        request_key=data["request_key"],
        actor=request.user,
    )
    audit(
        request,
        "Bank.EntryRecorded",
        entry,
        {"amount": str(entry.amount), "reference": entry.reference},
    )
    return entry


@transaction.atomic
def reverse_cash(request, pk, data):
    Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
    try:
        original = BankEntry.objects.get(pk=pk, workspace=request.user.workspace)
    except BankEntry.DoesNotExist as exc:
        raise ValidationError("Entry not found.") from exc
    existing = BankEntry.objects.filter(reversal_of=original).first()
    if existing:
        return existing
    if original.reversal_of_id or data["date"] < original.date:
        raise ValidationError("Reverse an original entry on or after its recorded date.")
    if BankEntry.objects.filter(
        workspace=request.user.workspace, request_key=data["request_key"]
    ).exists():
        raise ValidationError("This request key is already in use.")
    entry = BankEntry.objects.create(
        workspace=request.user.workspace,
        account=original.account,
        statement=original.statement,
        expense=original.expense,
        amount=-original.amount,
        date=data["date"],
        reference=f"Reversal: {original.reference}"[:150],
        notes=data["reason"],
        request_key=data["request_key"],
        reversal_of=original,
        actor=request.user,
    )
    audit(
        request,
        "Bank.EntryReversed",
        entry,
        {"original": str(original.pk), "reason": data["reason"]},
    )
    return entry
