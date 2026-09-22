from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import IsAdminUser
from rest_framework.routers import DefaultRouter

from apps.accounts import api as auth
from apps.catalog.api import CategoryViewSet, PackagingViewSet, ProductViewSet, StockBatchViewSet
from apps.core.health import health
from apps.core.public import BlogDetail, BlogList, EnquiryCreate, PublicConfig
from apps.core.views import workspace, workspace_logo
from apps.finance.api import ExpenseViewSet, activity, analytics
from apps.finance.banking_api import BankAccountViewSet, BankEntryViewSet, SettlementImportViewSet
from apps.logistics.api import CourierViewSet, tracking_webhook
from apps.marketing.api import CampaignViewSet
from apps.messaging import api as messaging
from apps.messaging import media as messaging_media
from apps.orders.api import CustomerViewSet, OrderViewSet

router = DefaultRouter()
for route, view in [
    ("categories", CategoryViewSet),
    ("products", ProductViewSet),
    ("stock-batches", StockBatchViewSet),
    ("packaging", PackagingViewSet),
    ("couriers", CourierViewSet),
    ("customers", CustomerViewSet),
    ("orders", OrderViewSet),
    ("campaigns", CampaignViewSet),
    ("expenses", ExpenseViewSet),
    ("bank-accounts", BankAccountViewSet),
    ("bank-entries", BankEntryViewSet),
    ("settlement-imports", SettlementImportViewSet),
]:
    router.register(route, view)

urlpatterns = [
    path("api/billing/", include("apps.billing.urls")),
    path("api/assistant/", include("apps.assistant.urls")),
    path("api/public/blog/", BlogList.as_view()),
    path("api/public/blog/<slug:slug>/", BlogDetail.as_view()),
    path("api/public/enquiries/", EnquiryCreate.as_view()),
    path("api/public/config/", PublicConfig.as_view()),
    path("admin/", admin.site.urls),
    path("api/", include(router.urls)),
    path("api/health/", health),
    path("api/auth/csrf/", auth.csrf),
    path("api/auth/register/", auth.RegisterView.as_view()),
    path("api/auth/login/", auth.LoginView.as_view()),
    path("api/auth/logout/", auth.sign_out),
    path("api/auth/me/", auth.me),
    path("api/auth/change-password/", auth.change_password),
    path("api/auth/forgot-password/", auth.RecoveryView.as_view()),
    path("api/auth/reset-password/", auth.ResetView.as_view()),
    path("api/auth/verify-email/", auth.VerifyView.as_view()),
    path("api/auth/resend-verification/", auth.ResendVerificationView.as_view()),
    path("api/team/", auth.team),
    path("api/analytics/", analytics),
    path("api/activity/", activity),
    path("api/workspace/", workspace),
    path("api/workspace/logo/", workspace_logo),
    path("api/integrations/tracking/webhook/", tracking_webhook),
    path("api/whatsapp/account/", messaging.account),
    path("api/whatsapp/contacts/", messaging.contacts),
    path("api/whatsapp/contacts/<uuid:pk>/remove/", messaging.remove_contact),
    path("api/whatsapp/campaigns/", messaging.campaigns),
    path("api/whatsapp/media/", messaging_media.upload),
    path("api/whatsapp/campaigns/<uuid:pk>/action/", messaging.campaign_action),
    path("api/whatsapp/messages/", messaging.messages),
    path("api/whatsapp/messages/<uuid:pk>/cancel/", messaging.cancel_message),
    path("api/whatsapp/messages/<uuid:pk>/check/", messaging.check_message),
    path("api/whatsapp/webhook/", messaging.webhook),
    path("api/whatsapp/unsubscribe/", messaging.unsubscribe),
    path(
        "api/schema/", SpectacularAPIView.as_view(permission_classes=[IsAdminUser]), name="schema"
    ),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema", permission_classes=[IsAdminUser]),
    ),
]
