"""Workspace-wide read access by explicit product policy; never unrestricted SQL."""

import json
from datetime import date
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User
from apps.catalog.models import Category, Packaging, Product, StockBatch
from apps.core.models import AuditEvent
from apps.finance.models import BankAccount, BankEntry, Expense, SettlementCost, SettlementImport
from apps.logistics.models import Courier
from apps.marketing.models import AdAllocation, Campaign
from apps.messaging.models import (
    WhatsAppAccount,
    WhatsAppCampaign,
    WhatsAppContact,
    WhatsAppMedia,
    WhatsAppMessage,
)
from apps.orders.models import Customer, Order, OrderItem, StockAllocation, TrackingEvent
from apps.orders.services import financials


def entry(model, fields, search, link, extra=""):
    return {
        "model": model,
        "fields": ("id created_at updated_at " + fields).split(),
        "search": search.split(),
        "link": link,
        "extra": extra.split(),
    }


# Only explicitly enumerated business fields may leave this process. No credentials,
# binary documents, auth internals, provider sessions, or arbitrary joined fields.
DATA = {
    "customers": entry(
        Customer,
        "name phone email city province address notes",
        "name phone email city address",
        "/customers",
    ),
    "orders": entry(
        Order,
        "number customer_id courier_id status tracking_id payment_type subtotal discount customer_charges advance_paid refunded_amount product_cost ad_cost courier_cost actual_courier_cost actual_courier_cost_basis return_cost packaging_cost other_cost weight dispatched_at finalized_at return_received_at damaged_cost tracking_mode tracking_provider tracking_checked_at delivery_zone charges_mode notes",
        "number tracking_id customer__name customer__phone customer_snapshot__name customer_snapshot__phone",
        "/orders",
        "customer_snapshot courier_snapshot packaging_snapshot other_costs",
    ),
    "order_items": entry(
        OrderItem,
        "order_id product_id name sku quantity unit_price fifo_cost damaged_cost",
        "name sku order__number",
        "/orders",
    ),
    "stock_allocations": entry(
        StockAllocation,
        "item_id batch_id quantity unit_cost",
        "batch__reference item__name",
        "/inventory",
    ),
    "tracking_events": entry(
        TrackingEvent,
        "order_id status message source occurred_at raw_status",
        "order__number message status",
        "/orders",
    ),
    "products": entry(
        Product,
        "name sku category category_record_id description selling_price image_url low_stock_threshold is_active",
        "name sku category description",
        "/products",
    ),
    "categories": entry(Category, "name", "name", "/categories"),
    "stock_batches": entry(
        StockBatch,
        "product_id reference purchased_quantity remaining_quantity reserved_quantity unit_cost received_at purchase_mode purchase_amount transport_cost import_cost",
        "reference product__name product__sku",
        "/inventory",
        "extra_costs",
    ),
    "packaging": entry(
        Packaging, "name unit unit_cost stock default_quantity", "name", "/packaging"
    ),
    "couriers": entry(
        Courier,
        "name provider code base_weight base_rate additional_kg_rate tax_percent fixed_charge return_rate is_active provincial_pricing same_province_rate outside_province_rate city_pricing same_city_rate",
        "name provider code",
        "/couriers",
        "extra_fees",
    ),
    "campaigns": entry(
        Campaign, "name channel spend start_date end_date allocated", "name channel", "/marketing"
    ),
    "ad_allocations": entry(
        AdAllocation,
        "campaign_id order_id amount active",
        "campaign__name order__number",
        "/marketing",
    ),
    "expenses": entry(
        Expense, "name category amount date notes", "name category notes", "/expenses"
    ),
    "bank_accounts": entry(
        BankAccount,
        "name kind last_four opening_balance opening_date",
        "name kind last_four",
        "/bank",
    ),
    "bank_entries": entry(
        BankEntry,
        "account_id expense_id statement_id amount date reference notes reversal_of_id",
        "reference notes account__name",
        "/bank",
    ),
    "settlements": entry(
        SettlementImport,
        "courier_id filename status revision reference date net_amount confirmed_at replaces_id error",
        "filename reference courier__name",
        "/bank",
        "extracted review confirmed_result",
    ),
    "settlement_costs": entry(
        SettlementCost,
        "statement_id order_id amount basis previous_amount previous_basis",
        "order__number statement__reference",
        "/bank",
    ),
    "whatsapp_contacts": entry(
        WhatsAppContact,
        "phone name transactional marketing opted_out consent_note consent_at",
        "name phone",
        "/whatsapp",
    ),
    "whatsapp_campaigns": entry(
        WhatsAppCampaign,
        "kind audience_mode name body scheduled_at state audience_count",
        "name body",
        "/whatsapp",
        "product_snapshot recipient_ids",
    ),
    "whatsapp_messages": entry(
        WhatsAppMessage,
        "contact_id order_id campaign_id kind event body state due_at expires_at attempted_at sent_at ack error",
        "body contact__name contact__phone",
        "/whatsapp",
    ),
    "whatsapp_settings": entry(
        WhatsAppAccount,
        "enabled marketing_enabled gap_seconds daily_limit quiet_start quiet_end session_status checked_at",
        "session_status",
        "/whatsapp",
        "events templates",
    ),
    "whatsapp_media": entry(WhatsAppMedia, "url filename mimetype size", "filename", "/whatsapp"),
    "activity": entry(AuditEvent, "action object_id actor_id", "action object_id", "/settings"),
    "team": {
        "model": User,
        "fields": "id first_name last_name email role is_active date_joined".split(),
        "search": "first_name last_name email".split(),
        "link": "/settings",
        "extra": [],
    },
}


def encode(value):
    return json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False)


def clean(value):
    return json.loads(encode(value))


def require_keys(args, allowed):
    if not isinstance(args, dict) or set(args) - set(allowed):
        raise ValidationError("Unsupported arguments. Use only the documented tool fields.")


def integer(value, maximum=1000000, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"Enter an integer from {minimum} to {maximum}.")
    return value


def resource(name):
    if name not in DATA:
        raise ValidationError(
            "Unknown resource. Use describe_data to see available workspace data."
        )
    return DATA[name]


def fields_for(spec):
    return {
        f.attname: f for f in spec["model"]._meta.concrete_fields if f.attname in spec["fields"]
    }


def describe_data(name=None):
    names = [name] if name else DATA
    result = {}
    for key in names:
        spec = resource(key)
        result[key] = {
            "fields": {
                n: {
                    "type": f.get_internal_type(),
                    **({"choices": [str(c[0]) for c in f.choices]} if f.choices else {}),
                }
                for n, f in fields_for(spec).items()
            },
            "large_fields": spec["extra"],
            "page": spec["link"],
        }
    return result


def query(workspace, args):
    spec = resource(args.get("resource"))
    qs = spec["model"].objects.filter(workspace_id=workspace.pk)
    search = args.get("search", "")
    if not isinstance(search, str) or len(search) > 200:
        raise ValidationError("Search must be at most 200 characters.")
    if search.strip():
        condition = Q()
        for field in spec["search"]:
            condition |= Q(**{field + "__icontains": search.strip()})
        qs = qs.filter(condition)
    filters = args.get("filters", [])
    if not isinstance(filters, list) or len(filters) > 8:
        raise ValidationError("Use at most 8 filters.")
    fields = fields_for(spec)
    for rule in filters:
        require_keys(rule, ["field", "operator", "value"])
        field, op, value = rule.get("field"), rule.get("operator"), rule.get("value")
        if field not in fields or op not in ["eq", "contains", "gte", "lte", "isnull"]:
            raise ValidationError("Unsupported filter field or operator. Check describe_data.")
        if not isinstance(value, str) or len(value) > 200:
            raise ValidationError("Filter values must be strings, at most 200 characters.")
        target = fields[field]
        lookup = {"eq": "exact", "contains": "icontains"}.get(op, op)
        if op == "isnull":
            if value not in ["true", "false"]:
                raise ValidationError("isnull requires true or false.")
            value = value == "true"
        elif op == "contains":
            if not isinstance(target, (models.CharField, models.TextField)):
                raise ValidationError("contains is only supported for text fields.")
        elif isinstance(target, models.DateTimeField) and len(value) == 10:
            value = date.fromisoformat(value)
            lookup = "date__" + lookup
        elif isinstance(target, models.BooleanField):
            if value not in ["true", "false"]:
                raise ValidationError("Boolean filters require true or false.")
            value = value == "true"
        qs = qs.filter(**{f"{field}__{lookup}": value})
    return spec, qs


def clip(value, limit=1200):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "… [TRUNCATED: use read_field for more]"
    return value


def record_url(name, row):
    if name == "orders":
        return f"/orders/{row['id']}"
    if name in ["order_items", "tracking_events", "ad_allocations", "settlement_costs"] and row.get(
        "order_id"
    ):
        return f"/orders/{row['order_id']}"
    if name == "settlements":
        return f"/bank/{row['id']}"
    return DATA[name]["link"]


def read_records(workspace, args):
    require_keys(args, ["resource", "search", "filters", "page", "page_size", "sort"])
    spec, qs = query(workspace, args)
    page, size = integer(args.get("page", 1)), integer(args.get("page_size", 20), 30)
    sort = args.get("sort", "-created_at" if "created_at" in spec["fields"] else "id")
    if not isinstance(sort, str) or sort.lstrip("-") not in fields_for(spec):
        raise ValidationError("Invalid sort field.")
    count = qs.count()
    selected_fields = [*spec["fields"], "workspace"]
    if spec["model"] is Order:
        selected_fields.append("actual_courier_cost_source")
    # Do not fetch PDFs, credentials or huge JSON blobs for list/search results.
    objects = list(qs.only(*selected_fields).order_by(sort, "pk")[(page - 1) * size : page * size])
    rows = []
    for obj in objects:
        row = {key: clip(getattr(obj, key)) for key in spec["fields"]}
        if isinstance(obj, Order):
            row["financials"] = financials(obj)
        if isinstance(obj, Product):
            stock = StockBatch.objects.filter(workspace=workspace, product=obj).aggregate(
                stock=Sum("remaining_quantity", default=0),
                reserved=Sum("reserved_quantity", default=0),
            )
            row.update(stock, available=stock["stock"] - stock["reserved"])
        if isinstance(obj, BankAccount):
            row["balance"] = (
                obj.opening_balance
                + (
                    BankEntry.objects.filter(workspace=workspace, account=obj).aggregate(
                        total=Sum("amount", default=0)
                    )["total"]
                )
            )
        if isinstance(obj, SettlementImport):
            received = BankEntry.objects.filter(workspace=workspace, statement=obj).aggregate(
                total=Sum("amount", default=0)
            )["total"]
            row.update(
                received_amount=received,
                remaining_amount=obj.net_amount - received if obj.net_amount is not None else None,
            )
        if isinstance(obj, Expense):
            paid = -BankEntry.objects.filter(workspace=workspace, expense=obj).aggregate(
                total=Sum("amount", default=0)
            )["total"]
            row.update(bank_paid=paid, bank_remaining=obj.amount - paid)
        row["record_url"] = record_url(args["resource"], row)
        rows.append(row)
    return {
        "resource": args["resource"],
        "total": count,
        "page": page,
        "page_size": size,
        "next_page": page + 1 if page * size < count else None,
        "scope": "Only this page is shown; use next_page for more. No default date cutoff.",
        "records": rows,
    }


def aggregate_records(workspace, args):
    require_keys(args, ["resource", "search", "filters", "sum_fields", "group_by", "page"])
    spec, qs = query(workspace, args)
    fields = fields_for(spec)
    sums = args.get("sum_fields", [])
    if (
        not isinstance(sums, list)
        or len(sums) > 6
        or any(
            f not in fields
            or not isinstance(fields[f], (models.DecimalField, models.IntegerField))
            or fields[f].is_relation
            or fields[f].primary_key
            for f in sums
        )
    ):
        raise ValidationError("sum_fields must contain up to 6 numeric business fields.")
    totals = {f"sum_{field}": Sum(field, default=0) for field in sums}
    group = args.get("group_by")
    if group:
        if group not in fields or isinstance(fields[group], models.TextField):
            raise ValidationError("Invalid group_by field.")
        page = integer(args.get("page", 1))
        groups = qs.order_by().values(group).annotate(count=Count("pk"), **totals).order_by(group)
        count = groups.count()
        return {
            "matched_records": qs.count(),
            "group_count": count,
            "groups": list(groups[(page - 1) * 30 : page * 30]),
            "next_page": page + 1 if page * 30 < count else None,
        }
    return {
        "matched_records": qs.count(),
        **qs.aggregate(**totals),
        "scope": "Complete filtered dataset, not a sample.",
    }


def read_field(workspace, args):
    require_keys(args, ["resource", "id", "field", "path", "offset"])
    spec = resource(args.get("resource"))
    field = args.get("field")
    if field not in spec["extra"] + spec["fields"]:
        raise ValidationError("This field is not available to the assistant.")
    obj = spec["model"].objects.only(field).filter(workspace=workspace, pk=args.get("id")).first()
    if not obj:
        raise ValidationError("Record not found in this workspace.")
    value = getattr(obj, field)
    path = args.get("path", [])
    if not isinstance(path, list) or len(path) > 8:
        raise ValidationError("Use a JSON path with at most 8 components.")
    for part in path:
        if isinstance(value, dict) and isinstance(part, str):
            value = value.get(part)
        elif (
            isinstance(value, list)
            and isinstance(part, str)
            and part.isdigit()
            and int(part) < len(value)
        ):
            value = value[int(part)]
        else:
            raise ValidationError("Invalid JSON path.")
    offset = integer(args.get("offset", 0), minimum=0)
    if isinstance(value, list):
        result = {
            "items": value[offset : offset + 10],
            "total": len(value),
            "next_offset": offset + 10 if offset + 10 < len(value) else None,
        }
        if len(encode(result)) > 16000:
            return {
                "type": "array",
                "total": len(value),
                "instruction": "Use path to select one array index, then a nested field.",
            }
        return result
    if isinstance(value, dict):
        if len(encode(value)) > 8000:
            return {
                "type": "object",
                "keys": list(value)[:100],
                "instruction": "Use path to inspect individual fields.",
            }
        return value
    if isinstance(value, str):
        return {
            "text": value[offset : offset + 4000],
            "total_characters": len(value),
            "next_offset": offset + 4000 if offset + 4000 < len(value) else None,
        }
    return {"value": value}


def overview(workspace, args):
    require_keys(args, ["start_date", "end_date"])
    start = date.fromisoformat(args["start_date"]) if args.get("start_date") else None
    end = date.fromisoformat(args["end_date"]) if args.get("end_date") else timezone.localdate()
    if start and start > end:
        raise ValidationError("Start date must not be after end date.")
    orders = Order.objects.filter(workspace=workspace, created_at__date__lte=end)
    expenses = Expense.objects.filter(workspace=workspace, date__lte=end)
    campaigns = Campaign.objects.filter(workspace=workspace, allocated=False, start_date__lte=end)
    if start:
        orders = orders.filter(created_at__date__gte=start)
        expenses = expenses.filter(date__gte=start)
        campaigns = campaigns.filter(start_date__gte=start)
    count = orders.count()
    if count > 20000:
        raise ValidationError(
            "This period contains more than 20,000 orders. Use smaller non-overlapping date ranges; no partial financial total was calculated."
        )
    totals = {
        k: Decimal(0)
        for k in [
            "revenue",
            "realized_profit",
            "expected_profit",
            "placed_value",
            "open_order_ads",
            "order_ads",
        ]
    }
    for order in orders.iterator(chunk_size=500):
        f = financials(order)
        totals["placed_value"] += order.subtotal - order.discount + order.customer_charges
        totals["order_ads"] += order.ad_cost
        if f["is_final"]:
            totals["revenue"] += Decimal(f["revenue"])
            totals["realized_profit"] += Decimal(f["profit"])
        else:
            totals["open_order_ads"] += order.ad_cost
            if order.status != "RETURNED":
                totals["expected_profit"] += Decimal(f["expected_profit"])
    totals["business_expenses"] = expenses.aggregate(total=Sum("amount", default=0))["total"]
    totals["unallocated_ads"] = campaigns.aggregate(total=Sum("spend", default=0))["total"]
    totals["net_profit"] = (
        totals["realized_profit"]
        - totals["business_expenses"]
        - totals["unallocated_ads"]
        - totals["open_order_ads"]
    )
    stocks = Product.objects.filter(workspace=workspace, is_active=True).annotate(
        available=Sum(F("batches__remaining_quantity") - F("batches__reserved_quantity"), default=0)
    )
    low = stocks.filter(available__lte=F("low_stock_threshold"))
    balances = (
        BankAccount.objects.filter(workspace=workspace).aggregate(
            total=Sum("opening_balance", default=0)
        )["total"]
        + BankEntry.objects.filter(workspace=workspace).aggregate(total=Sum("amount", default=0))[
            "total"
        ]
    )
    return {
        "period": {"start": start, "end": end},
        "order_count": count,
        "totals": totals,
        "basis": "Order creation cohort; realized profit only final outcomes. Expenses by date; unallocated ads by campaign start. Same accounting basis as Overview. Cash balances and stock below are CURRENT, not period-end.",
        "statuses": list(orders.order_by().values("status").annotate(count=Count("id"))),
        "current_recorded_cash_balance": balances,
        "low_stock_count": low.count(),
        "low_stock_sample": list(
            low.order_by("available", "id").values("id", "name", "sku", "available")[:20]
        ),
        "inventory_value_current": StockBatch.objects.filter(workspace=workspace).aggregate(
            total=Sum(F("remaining_quantity") * F("unit_cost"), default=0)
        )["total"],
        "record_url": "/analytics",
    }


def bank_statement(workspace, args):
    require_keys(args, ["account_id", "start_date", "end_date", "page"])
    account = BankAccount.objects.filter(workspace=workspace, pk=args.get("account_id")).first()
    if not account:
        raise ValidationError("Account not found in this workspace.")
    start = date.fromisoformat(args.get("start_date") or str(account.opening_date))
    end = date.fromisoformat(args.get("end_date") or str(timezone.localdate()))
    if start < account.opening_date or start > end or end > timezone.localdate():
        raise ValidationError("Invalid statement date range.")
    page = integer(args.get("page", 1))
    base = BankEntry.objects.filter(workspace=workspace, account=account)
    prior = base.filter(date__lt=start).aggregate(total=Sum("amount", default=0))["total"]
    rows = base.filter(date__gte=start, date__lte=end).order_by("date", "created_at", "id")
    totals = rows.aggregate(
        net=Sum("amount", default=0),
        money_in=Sum("amount", filter=Q(amount__gt=0), default=0),
        money_out=Sum("amount", filter=Q(amount__lt=0), default=0),
    )
    opening = account.opening_balance + prior
    skipped_ids = rows.values("id")[: (page - 1) * 30]
    running = (
        opening + base.filter(id__in=skipped_ids).aggregate(total=Sum("amount", default=0))["total"]
    )
    result = []
    for row in rows.values(
        "id", "date", "reference", "notes", "amount", "reversal_of_id", "statement_id", "expense_id"
    )[(page - 1) * 30 : page * 30]:
        running += row["amount"]
        result.append({**row, "running_balance": running})
    count = rows.count()
    return {
        "account": account.name,
        "period": {"start": start, "end": end},
        "opening_balance": opening,
        "closing_balance": opening + totals["net"],
        "money_in": totals["money_in"],
        "money_out": -totals["money_out"],
        "count": count,
        "entries": result,
        "next_page": page + 1 if page * 30 < count else None,
        "basis": "Internal recorded ledger, including signed reversal entries; not a bank-issued statement.",
        "record_url": "/bank",
    }
