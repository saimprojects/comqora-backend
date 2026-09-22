import json
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.catalog.models import Packaging, Product
from apps.core.api import audit
from apps.core.models import Workspace
from apps.finance.api import ExpenseSerializer
from apps.finance.banking_api import CashInput
from apps.finance.models import BankAccount, Expense, SettlementImport
from apps.finance.settlements import record_cash
from apps.logistics.models import Courier
from apps.orders.api import CreateOrderSerializer, CustomerSerializer
from apps.orders.models import Customer
from apps.orders.services import create_order

from .data import clean
from .models import ProposedAction

ACTION_TYPES = {
    "create_expense": {
        "serializer": ExpenseSerializer,
        "roles": {"owner", "manager"},
        "fields": "name category amount date notes".split(),
    },
    "create_customer": {
        "serializer": CustomerSerializer,
        "roles": {"owner", "manager", "staff"},
        "fields": "name phone email city province address notes".split(),
    },
    "create_order": {
        "serializer": CreateOrderSerializer,
        "roles": {"owner", "manager", "staff"},
        "fields": "customer courier weight payment_type delivery_zone charges_mode advance_paid discount ad_cost notes items packaging other_costs".split(),
    },
    "record_bank_movement": {
        "serializer": CashInput,
        "roles": {"owner", "manager"},
        "fields": "account statement expense amount date reference notes".split(),
    },
}


def validate_action(user, kind, payload, action_id):
    if kind not in ACTION_TYPES:
        raise ValidationError("Unsupported action. Available: " + ", ".join(ACTION_TYPES))
    spec = ACTION_TYPES[kind]
    if user.role not in spec["roles"]:
        raise PermissionDenied(
            "You can read all workspace data through the assistant, but applying this change requires the normal workspace write role."
        )
    if not isinstance(payload, dict) or set(payload) - set(spec["fields"]):
        raise ValidationError("Unsupported action fields.")
    data = {**payload}
    if kind == "record_bank_movement":
        data["request_key"] = str(action_id)
    serializer = spec["serializer"](data=data, context={"request": SimpleNamespace(user=user)})
    serializer.is_valid(raise_exception=True)
    if kind == "create_order":
        for item in serializer.validated_data["items"]:
            if "unit_price" not in item:
                raise ValidationError(
                    "Include the exact unit_price for every order item so the user can approve it. Read products first."
                )
            if not Product.objects.filter(
                pk=item["product"], workspace=user.workspace, is_active=True
            ).exists():
                raise ValidationError("Product is inactive or outside this workspace.")
        for item in serializer.validated_data.get("packaging", []):
            if not Packaging.objects.filter(pk=item["id"], workspace=user.workspace).exists():
                raise ValidationError("Packaging not found in this workspace.")
    if kind == "record_bank_movement":
        for field, model in [
            ("account", BankAccount),
            ("statement", SettlementImport),
            ("expense", Expense),
        ]:
            identifier = serializer.validated_data.get(field)
            if (
                identifier
                and not model.objects.filter(workspace=user.workspace, pk=identifier).exists()
            ):
                raise ValidationError(f"{field} not found in this workspace.")
    return serializer


def review_details(action):
    """Human-readable references resolved only from the proposal's own workspace."""
    details = clean(action.payload)

    def label(model, identifier):
        obj = model.objects.filter(workspace_id=action.workspace_id, pk=identifier).first()
        name = (
            (getattr(obj, "name", None) or getattr(obj, "reference", None) or "Record")
            if obj
            else "Unavailable record"
        )
        return f"{name} ({identifier})"

    for field, model in [
        ("customer", Customer),
        ("courier", Courier),
        ("account", BankAccount),
        ("statement", SettlementImport),
        ("expense", Expense),
    ]:
        if details.get(field):
            details[field] = label(model, details[field])
    for item in details.get("items", []):
        item["product"] = label(Product, item["product"])
    for item in details.get("packaging", []):
        item["id"] = label(Packaging, item["id"])
    return details


def propose(turn, args):
    from .data import require_keys

    require_keys(args, ["kind", "details_json"])
    raw = args.get("details_json", "")
    if not isinstance(raw, str) or len(raw) > 12000:
        raise ValidationError("Action details must be a JSON object of at most 12,000 characters.")
    payload = json.loads(raw)
    kind = args.get("kind")
    action = ProposedAction(
        workspace=turn.workspace,
        turn=turn,
        kind=kind,
        payload=payload,
        expires_at=timezone.now() + timedelta(minutes=20),
    )
    serializer = validate_action(turn.conversation.user, kind, payload, action.pk)
    if kind == "create_order":
        # Include defaults (payment method, discount, weight...) in the approval card.
        payload = clean(serializer.data)
        action.payload = payload
    existing = turn.actions.filter(kind=kind, payload=payload, status="PENDING").first()
    if existing:
        action = existing
    else:
        if turn.actions.count() >= 3:
            raise ValidationError("At most 3 proposals are allowed per message.")
        action.save()
    return {
        "proposal_id": str(action.pk),
        "status": "PENDING_USER_CONFIRMATION",
        "kind": kind,
        "details": payload,
        "warning": "Nothing has been changed. The user must use the confirmation button. Bank movements only record an existing movement; they do not transfer money.",
    }


@transaction.atomic
def decide(request, action_id, decision):
    # Lock ordering matches business services: workspace before action/ledger/order locks.
    Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
    action = (
        ProposedAction.objects.select_for_update()
        .filter(
            pk=action_id,
            workspace=request.user.workspace,
            turn__conversation__user=request.user,
        )
        .first()
    )
    if not action:
        raise ValidationError("Proposal not found.")
    if action.status != "PENDING":
        return action
    if decision == "cancel":
        action.status = "CANCELLED"
    elif action.expires_at <= timezone.now():
        action.status = "EXPIRED"
    else:
        user = User.objects.get(pk=request.user.pk)
        if (
            not user.is_active
            or not user.has_dashboard_access
            or user.workspace_id != action.workspace_id
        ):
            raise PermissionDenied("Workspace access is no longer available.")
        serializer = validate_action(user, action.kind, action.payload, action.pk)
        context = SimpleNamespace(user=user)
        if action.kind == "create_order":
            obj = create_order(user.workspace, serializer.validated_data)
            result = {"id": str(obj.pk), "label": obj.number, "url": f"/orders/{obj.pk}"}
        elif action.kind == "record_bank_movement":
            obj = record_cash(context, serializer.validated_data)
            result = {"id": str(obj.pk), "label": obj.reference, "url": "/bank"}
        else:
            obj = serializer.save(workspace=user.workspace)
            result = {
                "id": str(obj.pk),
                "label": obj.name,
                "url": "/expenses" if action.kind == "create_expense" else "/customers",
            }
        action.status = "APPLIED"
        action.result = clean(result)
        audit(context, "AI." + action.kind, obj, {"proposal_id": str(action.pk)})
    action.decided_at = timezone.now()
    action.save(update_fields=["status", "result", "decided_at", "updated_at"])
    return action
