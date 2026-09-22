"""Create an isolated, repeatable demo. Never overwrites an existing workspace."""

import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Packaging, Product, StockBatch
from apps.core.models import Workspace
from apps.finance.models import Expense
from apps.logistics.models import Courier
from apps.marketing.models import Campaign
from apps.orders.models import Customer, Order
from apps.orders.services import (
    apply_tracking,
    cancel_order,
    create_order,
    dispatch_order,
    receive_return,
)


class Command(BaseCommand):
    help = "Seed a separate demo workspace (DEBUG only). Requires an explicit demo password."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Demo seeding is disabled outside DEBUG mode.")
        if User.objects.filter(email="demo@sellflow.local").exists():
            self.stdout.write("Demo already exists; no data changed.")
            return
        from django.contrib.auth.password_validation import validate_password

        validate_password(options["password"])
        rng = random.Random(42)
        now = timezone.now()
        today = timezone.localdate()
        workspace = Workspace.objects.create(name="Studio Commerce · Demo")
        User.objects.create_user(
            username="sellflow-demo",
            email="demo@sellflow.local",
            password=options["password"],
            first_name="Ahmed",
            last_name="Khan",
            workspace=workspace,
            email_verified=True,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
        )
        specs = [
            ("AirPods Pro 2", "AP-002", "Electronics", 4490, 1850),
            ("Arc Smart Watch", "SW-001", "Electronics", 5990, 2600),
            ("Everyday Tote", "ET-004", "Accessories", 2190, 750),
            ("Cloud Headphones", "CH-003", "Electronics", 7490, 3200),
            ("Essential Hoodie", "EH-005", "Apparel", 3490, 1200),
            ("Ceramic Travel Cup", "TC-006", "Lifestyle", 1890, 580),
        ]
        products = []
        for name, sku, category, price, cost in specs:
            product = Product.objects.create(
                workspace=workspace,
                name=name,
                sku=sku,
                category=category,
                selling_price=price,
                low_stock_threshold=15,
            )
            products.append(product)
            for i in range(2):
                StockBatch.objects.create(
                    workspace=workspace,
                    product=product,
                    reference=f"PO-2026-{sku}-{i + 1}",
                    purchased_quantity=150,
                    remaining_quantity=150,
                    unit_cost=cost + i * 50,
                    received_at=today - timedelta(days=50 - i * 12),
                )
        packs = [
            Packaging.objects.create(workspace=workspace, name=n, unit_cost=c, stock=1000)
            for n, c in [("Branded flyer", 18), ("Bubble wrap", 12), ("Thank you card", 5)]
        ]
        couriers = [
            Courier.objects.create(
                workspace=workspace,
                name=n,
                code=n.lower(),
                base_rate=r,
                additional_kg_rate=100,
                tax_percent=15,
                fixed_charge=10,
                return_rate=ret,
            )
            for n, r, ret in [("TCS", 240, 150), ("Leopards", 215, 130), ("PostEx", 200, 120)]
        ]
        names = [
            "Sara Ahmed",
            "Ali Raza",
            "Ayesha Malik",
            "Hamza Khan",
            "Fatima Noor",
            "Usman Tariq",
            "Zainab Shah",
            "Bilal Hassan",
            "Mariam Ali",
            "Omar Farooq",
            "Hira Sheikh",
            "Saad Iqbal",
            "Areeba Rauf",
            "Danish Mir",
            "Sana Javed",
            "Fahad Aslam",
        ]
        customers = [
            Customer.objects.create(
                workspace=workspace,
                name=name,
                phone=f"03001234{i:03}",
                city=["Lahore", "Karachi", "Islamabad", "Rawalpindi"][i % 4],
                address=f"House {i + 1}, Model Town",
                notes="Fictional demo customer",
            )
            for i, name in enumerate(names)
        ]
        for i in range(128):
            product = products[rng.randrange(len(products))]
            order = create_order(
                workspace,
                {
                    "customer": rng.choice(customers),
                    "courier": rng.choice(couriers).pk,
                    "items": [
                        {
                            "product": product.pk,
                            "quantity": rng.choices([1, 2, 3], weights=[8, 2, 1])[0],
                        }
                    ],
                    "weight": Decimal("1"),
                    "discount": Decimal(rng.choice([0, 100, 200])),
                    "ad_cost": Decimal(rng.randint(180, 350)),
                    "packaging": [{"id": p.pk, "quantity": Decimal("1")} for p in packs],
                    "notes": "Sample order — fictional demo data.",
                },
            )
            age = rng.randint(0, 29)
            created = now - timedelta(days=age, hours=rng.randint(0, 5))
            Order.objects.filter(pk=order.pk).update(created_at=created)
            if age < 2:
                continue
            if i % 17 == 0:
                cancel_order(order.pk, workspace)
                Order.objects.filter(pk=order.pk).update(finalized_at=created + timedelta(hours=2))
                continue
            order = dispatch_order(order.pk, workspace, f"DEMO{100000 + i}")
            Order.objects.filter(pk=order.pk).update(dispatched_at=created + timedelta(hours=4))
            outcome = (
                "RETURNED"
                if i % 9 == 0
                else "DELIVERY_FAILED"
                if age < 5 and i % 3 == 0
                else "IN_TRANSIT"
                if age < 4
                else "DELIVERED"
            )
            order = apply_tracking(
                order.pk, workspace, outcome, f"demo-event-{i}", "Sample courier event"
            )
            if outcome == "RETURNED" and i % 2 == 0:
                receive_return(
                    order.pk, workspace, {str(order.items.first().pk): 1} if i % 18 == 0 else {}
                )
            if outcome == "DELIVERED" or order.return_received_at:
                Order.objects.filter(pk=order.pk).update(
                    finalized_at=created + timedelta(days=2, hours=4)
                )
        last_batch = StockBatch.objects.filter(product=products[-1]).last()
        last_batch.remaining_quantity = last_batch.reserved_quantity + 4
        last_batch.save()
        first_batch = StockBatch.objects.filter(product=products[-1]).first()
        first_batch.remaining_quantity = first_batch.reserved_quantity + 3
        first_batch.save()
        for name, category, amount in [
            ("Workspace rent", "rent", 18000),
            ("Shopify subscription", "software", 8500),
            ("Internet & utilities", "utilities", 4500),
        ]:
            Expense.objects.create(
                workspace=workspace,
                name=name,
                category=category,
                amount=amount,
                date=today - timedelta(days=4),
            )
        Campaign.objects.create(
            workspace=workspace,
            name="September · Retargeting",
            channel="Meta",
            spend=6500,
            start_date=today - timedelta(days=6),
            end_date=today,
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Demo ready: demo@sellflow.local. All people, orders, and tracking events are fictional."
            )
        )
