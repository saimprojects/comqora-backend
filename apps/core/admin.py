from django.contrib import admin
from django.utils import timezone

from apps.catalog.models import Category, Packaging, Product, StockBatch
from apps.finance.models import BankAccount, BankEntry, Expense, SettlementCost, SettlementImport
from apps.logistics.models import Courier
from apps.marketing.models import AdAllocation, Campaign
from apps.messaging.models import (
    WhatsAppAccount,
    WhatsAppCampaign,
    WhatsAppContact,
    WhatsAppMessage,
)
from apps.orders.models import Customer, Order, OrderItem, StockAllocation, TrackingEvent

from .models import AuditEvent, BlogPost, Enquiry, Workspace


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ["title", "category", "published", "published_at"]
    list_filter = ["published", "category"]
    search_fields = ["title", "body"]
    prepopulated_fields = {"slug": ("title",)}
    fieldsets = [
        (None, {"fields": ("title", "slug", "excerpt", "category", "body", "author")}),
        ("Publishing", {"fields": ("published", "published_at")}),
    ]

    def save_model(self, request, obj, form, change):
        if obj.published and not obj.published_at:
            obj.published_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(Enquiry)
class EnquiryAdmin(admin.ModelAdmin):
    list_display = ["name", "email", "business", "created_at", "handled"]
    list_filter = ["handled"]
    search_fields = ["name", "email", "business"]
    readonly_fields = ["name", "email", "business", "message", "created_at"]

    def has_add_permission(self, request):
        return False


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ["name", "currency", "created_at"]
    search_fields = ["name"]


class ReadOnlyFinancialAdmin(admin.ModelAdmin):
    list_filter = ["workspace"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(ReadOnlyFinancialAdmin):
    list_display = ["number", "workspace", "customer", "status", "subtotal", "created_at"]
    list_filter = ["workspace", "status"]
    search_fields = ["number", "tracking_id", "customer__name"]


for model in [
    AuditEvent,
    StockBatch,
    OrderItem,
    StockAllocation,
    TrackingEvent,
    Campaign,
    AdAllocation,
    Expense,
    BankAccount,
    BankEntry,
    SettlementCost,
    SettlementImport,
    WhatsAppAccount,
    WhatsAppCampaign,
    WhatsAppContact,
    WhatsAppMessage,
]:
    admin.site.register(model, ReadOnlyFinancialAdmin)

for model in [Category, Product, Packaging, Courier, Customer]:
    admin.site.register(model, ReadOnlyFinancialAdmin)
