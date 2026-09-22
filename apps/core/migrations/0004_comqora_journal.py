from django.db import migrations
from django.utils import timezone


def seed(apps, schema_editor):
    Post = apps.get_model("core", "BlogPost")
    articles = [
        ("The costs hiding behind a successful sale", "costs-behind-a-successful-sale", "Profit intelligence", "Order value is a starting point. A useful margin includes the costs it took to get that order out of the door.", """A busy order book feels like progress. But order value alone cannot tell you whether that progress is profitable. To understand a sale, follow the money from the purchase batch to the final delivery outcome.

## Start with the product that actually shipped

Purchase prices change. A product bought this month may cost more than the same SKU bought last month. Batch-level costing keeps that difference visible. With FIFO allocation, the oldest available stock is allocated first, and its recorded cost travels with the order.

## Bring the small costs into the picture

Packaging, transport into inventory, courier charges and advertising can all change the margin. Record these costs deliberately. If a cost belongs to the business rather than an individual order, keep it in the right place instead of counting it twice.

## Separate the estimate from the outcome

An order that is still travelling has not reached its final outcome. Expected profit is useful for planning, but delivered, returned and cancelled orders tell different stories. Returned stock also needs an inspection decision before you assume its full value has been recovered.

## Build a repeatable review

Compare order value, realized margin and business expenses for the same period. Review unusual losses, missing costs and high-return products. A consistent review is more useful than a perfect-looking number built on incomplete records. Comqora helps make those relationships visible; the quality of your inputs still matters."""),
        ("A calmer daily routine for delivery operations", "daily-delivery-operations-routine", "Delivery operations", "A practical way to review exceptions, follow up with customers and keep courier updates in context.", """A tracking page is a record of movement, not a complete operating routine. The useful question is not only where a parcel is. It is what your team should do next.

## Start with exceptions

Review failed attempts, stalled shipments and return-in-transit parcels before healthy deliveries. Keep the distinction between a failed delivery attempt and an item physically returned to you. Those are different events with different stock and cost implications.

## Read the timeline, not just the badge

A short status badge is helpful, but the provider timeline gives context. Check when the latest checkpoint happened and whether a manual update was made afterward. An imported history should not trigger a flood of old customer notifications.

## Keep manual updates accountable

When the provider cannot be reached, manual updates can keep operations moving. Add a clear note explaining the source of the information. Do not mark a parcel delivered simply to clear an exception from the screen.

## Close the loop

If a customer needs an update, communicate the facts you have and avoid promising an exact delivery time without confirmation. When a return reaches you, inspect the goods and record the inventory outcome. This closes the operational loop and makes the financial picture more useful.

Build a short daily checklist around these steps. Courier availability and update speed can vary; your team's review process should make that uncertainty visible instead of hiding it."""),
        ("Give every stock receipt a useful story", "better-stock-receipt-records", "Inventory", "From purchase totals to landed unit costs, better receipt records make future decisions easier.", """A stock receipt is more than a quantity added to a product. It is the record of what arrived, what it cost and why the next order may earn a different margin.

## Record quantity and purchase basis

Decide whether the supplier quote is per unit or for the full batch. Enter the amount on the correct basis before adding other charges. Check that the quantity matches the goods actually received, not only the supplier invoice.

## Keep additional costs named

Transport, handling and other acquisition costs are easier to audit when they have clear names. Allocating these costs across the received quantity gives you a landed unit cost. Do not treat a whole-batch amount as a per-piece charge.

## Make references useful

Use a purchase reference your team can recognize later. A clear reference helps when reviewing supplier prices, reconciling receipts or investigating a margin that looks different from expectations.

## Keep returns separate from new purchases

Returned items need an outcome: sellable, damaged or otherwise unavailable. A returned parcel is not automatically new stock at the original value. Recording its condition keeps availability and loss calculations grounded in what is actually on the shelf.

Review the unit cost before saving each receipt. That small habit improves the data used by future orders, stock decisions and profit reports."""),
    ]
    for title, slug, category, excerpt, body in articles:
        Post.objects.using(schema_editor.connection.alias).get_or_create(slug=slug, defaults={"title":title,"category":category,"excerpt":excerpt,"body":body,"author":"Comqora editorial","published":True,"published_at":timezone.now()})


class Migration(migrations.Migration):
    dependencies = [("core", "0003_blogpost_enquiry")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
