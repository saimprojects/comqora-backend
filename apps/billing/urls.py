from django.urls import path

from . import api

urlpatterns = [
    path("plans/", api.plans),
    path("checkout/", api.checkout),
    path("banks/<int:pk>/icon/", api.bank_icon, name="billing-bank-icon"),
    path("payments/<uuid:pk>/submit/", api.submit_proof),
    path("payments/<uuid:pk>/cancel/", api.cancel),
    path("payments/<uuid:pk>/proof/", api.proof, name="billing-proof"),
]
