from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from .forms import PaymentBankForm
from .models import Payment, PaymentBank, Plan, Subscription
from .services import review_payment


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["name", "monthly_price", "ai_enabled", "active"]
    list_editable = ["monthly_price", "ai_enabled", "active"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(PaymentBank)
class PaymentBankAdmin(admin.ModelAdmin):
    form = PaymentBankForm
    readonly_fields = ["icon_preview"]
    list_display = ["icon_preview", "bank_name", "account_title", "account_number", "active"]
    list_filter = ["active"]

    @admin.display(description="Icon")
    def icon_preview(self, obj):
        if not obj or not obj.icon:
            return "No icon uploaded"
        return format_html(
            '<img src="{}" width="64" height="64" style="object-fit:contain" alt="Bank icon">',
            reverse("billing-bank-icon", args=[obj.pk]),
        )


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ["workspace", "plan", "starts_at", "expires_at", "suspended"]
    list_filter = ["plan", "suspended"]
    search_fields = ["workspace__name"]
    readonly_fields = ["workspace", "plan", "starts_at", "expires_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    change_form_template = "admin/billing/payment/change_form.html"
    list_display = [
        "workspace",
        "plan_name",
        "amount",
        "status",
        "submitted_at",
        "reviewed_by",
        "review_link",
    ]
    list_filter = ["status", "plan"]
    search_fields = ["workspace__name", "submitted_by__email", "reference"]
    readonly_fields = [
        "workspace",
        "submitted_by",
        "plan",
        "plan_name",
        "amount",
        "bank",
        "bank_details",
        "status",
        "reference",
        "screenshot",
        "created_at",
        "submitted_at",
        "reviewed_at",
        "reviewed_by",
    ]
    fields = readonly_fields + ["review_note"]
    actions = ["approve", "reject"]

    def get_queryset(self, request):
        return super().get_queryset(request).defer("proof")

    @admin.display(description="Payment review")
    def review_link(self, obj):
        if obj.status == "PENDING":
            return format_html(
                '<a class="btn btn-primary btn-sm" href="{}">Review payment</a>',
                reverse("admin:billing_payment_change", args=[obj.pk]),
            )
        if obj.status == "AWAITING_PROOF":
            return "Waiting for customer screenshot"
        return obj.get_status_display()

    def response_change(self, request, obj):
        if "_approve_payment" in request.POST or "_reject_payment" in request.POST:
            try:
                payment = review_payment(
                    obj.pk, request.user, approve="_approve_payment" in request.POST
                )
            except ValidationError as exc:
                self.message_user(request, exc.messages[0], messages.ERROR)
            else:
                self.log_change(request, payment, f"Payment {payment.status.lower()}.")
                self.message_user(
                    request,
                    "Payment approved. Subscription activated / renewed."
                    if payment.status == "APPROVED"
                    else "Payment rejected. The customer can see your review note.",
                    messages.SUCCESS,
                )
            return HttpResponseRedirect(reverse("admin:billing_payment_change", args=[obj.pk]))
        return super().response_change(request, obj)

    @admin.display(description="Payment screenshot")
    def screenshot(self, obj):
        if not obj.submitted_at:
            return "Not submitted"
        url = reverse("billing-proof", args=[obj.pk])
        return format_html(
            '<a href="{}" target="_blank" rel="noopener"><img src="{}" style="max-width:480px;max-height:500px" alt="Payment proof" /></a>',
            url,
            url,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (["review_note"] if obj and obj.status != "PENDING" else [])

    def save_model(self, request, obj, form, change):
        # A stale form cannot overwrite an approval or reopen a payment.
        Payment.objects.filter(pk=obj.pk, status="PENDING").update(review_note=obj.review_note)
        obj.refresh_from_db()

    def review(self, request, queryset, approve):
        for payment in queryset:
            try:
                review_payment(payment.pk, request.user, approve)
            except ValidationError as exc:
                self.message_user(request, f"{payment}: {exc.messages[0]}", messages.ERROR)
            else:
                self.message_user(
                    request,
                    f"{payment.workspace}: payment {'approved' if approve else 'rejected'}.",
                    messages.SUCCESS,
                )

    @admin.action(description="Verify payment and activate / renew subscription")
    def approve(self, request, queryset):
        self.review(request, queryset, True)

    @admin.action(description="Reject payment (save a review note first)")
    def reject(self, request, queryset):
        self.review(request, queryset, False)
