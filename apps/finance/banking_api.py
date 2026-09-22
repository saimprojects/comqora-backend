import hashlib
from pathlib import PurePath

from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.api import audit
from apps.core.models import Workspace
from apps.logistics.models import Courier

from .documents import MAX_BYTES, detect_document_kind, source_content_type
from .models import BankAccount, BankEntry, SettlementImport
from .settlements import (
    assess,
    confirm,
    lock_statement,
    record_cash,
    reverse_cash,
    reverse_statement,
    validate_structure,
)
from .worker import run_document


class FinancePermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user.is_authenticated
            and request.user.workspace_id
            and request.user.has_dashboard_access
            and request.user.role in {"owner", "manager"}
        )


class PrivateFinanceViewSet(viewsets.GenericViewSet):
    permission_classes = [FinancePermission]

    def get_queryset(self):
        return super().get_queryset().filter(workspace_id=self.request.user.workspace_id)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def list(self, request):
        rows = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


class AccountSerializer(serializers.ModelSerializer):
    balance = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    def validate_opening_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Opening balance date cannot be in the future.")
        return value

    class Meta:
        model = BankAccount
        fields = [
            "id",
            "name",
            "kind",
            "last_four",
            "opening_balance",
            "opening_date",
            "balance",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"last_four": {"validators": []}}

    def validate_last_four(self, value):
        if value and (len(value) != 4 or not value.isdigit()):
            raise serializers.ValidationError(
                "Enter only the last four digits; never the full account number."
            )
        return value


class BankAccountViewSet(PrivateFinanceViewSet):
    queryset = BankAccount.objects.all()
    serializer_class = AccountSerializer
    search_fields = ["name"]

    @action(detail=True, methods=["get"])
    def statement(self, request, pk=None):
        from decimal import Decimal

        account = self.get_object()
        start = serializers.DateField().run_validation(
            request.query_params.get("start_date", str(account.opening_date))
        )
        end = serializers.DateField().run_validation(
            request.query_params.get("end_date", str(timezone.localdate()))
        )
        if start < account.opening_date or end < start or end > timezone.localdate():
            raise ValidationError(
                "Choose a valid statement period from the account opening date through today."
            )
        # One ordered ledger snapshot supplies both balances and displayed entries.
        entries = list(
            BankEntry.objects.filter(
                account=account, workspace=request.user.workspace, date__lte=end
            )
            .order_by("date", "created_at", "id")
            .values(
                "id",
                "date",
                "reference",
                "notes",
                "amount",
                "reversal_of_id",
                "statement__reference",
                "expense__name",
            )[:10001]
        )
        if len(entries) > 10000:
            raise ValidationError(
                "This account has more than 10,000 movements through this date. Choose an earlier end date to print a complete statement."
            )
        balance = account.opening_balance
        opening = balance
        incoming = outgoing = Decimal("0.00")
        rows = []
        for entry in entries:
            amount = entry["amount"]
            balance += amount
            if entry["date"] < start:
                opening = balance
                continue
            incoming += max(amount, Decimal("0.00"))
            outgoing += max(-amount, Decimal("0.00"))
            rows.append({**entry, "amount": str(amount), "balance": str(balance)})
        return Response(
            {
                "account": AccountSerializer(account).data,
                "start_date": start,
                "end_date": end,
                "opening_balance": str(opening),
                "closing_balance": str(balance),
                "money_in": str(incoming),
                "money_out": str(outgoing),
                "entries": rows,
            }
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        from decimal import Decimal

        zero = Decimal("0.00")
        confirmed = SettlementImport.objects.filter(
            workspace=request.user.workspace, status="CONFIRMED"
        ).annotate(received=Sum("receipts__amount", default=0))
        remaining = [
            s.net_amount - s.received
            for s in confirmed.defer("source", "extracted", "review", "confirmed_result")
        ]
        return Response(
            {
                "balance": str(sum((a.balance for a in self.get_queryset()), zero)),
                "awaiting_receipt": str(sum((n for n in remaining if n > 0), zero)),
                "payable_to_couriers": str(-sum((n for n in remaining if n < 0), zero)),
                "review_count": SettlementImport.objects.filter(
                    workspace=request.user.workspace, status="REVIEW"
                ).count(),
            }
        )

    def get_queryset(self):
        from django.db.models import F

        return (
            super()
            .get_queryset()
            .annotate(balance=F("opening_balance") + Sum("entries__amount", default=0))
            .order_by("-created_at", "id")
        )

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save(workspace=request.user.workspace)
        audit(request, "Bank.AccountCreated", account)
        return Response(
            self.get_serializer(self.get_queryset().get(pk=account.pk)).data, status=201
        )


class EntrySerializer(serializers.ModelSerializer):
    expense_name = serializers.CharField(source="expense.name", read_only=True, default="")
    account_name = serializers.CharField(source="account.name", read_only=True)
    statement_reference = serializers.CharField(
        source="statement.reference", read_only=True, default=""
    )
    reversed = serializers.SerializerMethodField()

    def get_reversed(self, obj):
        return hasattr(obj, "reversal")

    class Meta:
        model = BankEntry
        fields = [
            "id",
            "account",
            "account_name",
            "statement",
            "statement_reference",
            "expense",
            "expense_name",
            "amount",
            "date",
            "reference",
            "notes",
            "reversal_of",
            "reversed",
            "created_at",
        ]
        read_only_fields = fields


class CashInput(serializers.Serializer):
    expense = serializers.UUIDField(required=False, allow_null=True)
    account = serializers.UUIDField()
    statement = serializers.UUIDField(required=False, allow_null=True)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    date = serializers.DateField()
    reference = serializers.CharField(max_length=150)
    notes = serializers.CharField(max_length=500, allow_blank=True, required=False)
    request_key = serializers.UUIDField()

    def validate_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError(
                "Record completed bank movements, not future payments."
            )
        return value

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError("A bank movement cannot be zero.")
        return value


class ReversalInput(serializers.Serializer):
    reason = serializers.CharField(max_length=500)
    date = serializers.DateField()
    request_key = serializers.UUIDField()

    def validate_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Reversal date cannot be in the future.")
        return value


class BankEntryViewSet(PrivateFinanceViewSet):
    queryset = BankEntry.objects.select_related("account", "statement", "expense", "reversal").all()
    serializer_class = EntrySerializer
    search_fields = ["reference", "account__name", "statement__reference"]
    filterset_fields = ["account", "statement", "expense"]

    def create(self, request):
        data = CashInput(data=request.data)
        data.is_valid(raise_exception=True)
        entry = record_cash(request, data.validated_data)
        return Response(self.get_serializer(entry).data, status=201)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        self.get_object()
        data = ReversalInput(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(self.get_serializer(reverse_cash(request, pk, data.validated_data)).data)


class ImportSerializer(serializers.ModelSerializer):
    courier_name = serializers.CharField(source="courier.name", read_only=True)
    received_amount = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    page_count = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()

    def get_remaining_amount(self, obj):
        return str(obj.net_amount - obj.received_amount) if obj.net_amount is not None else None

    def get_page_count(self, obj):
        return len(obj.extracted.get("pages", []))

    class Meta:
        model = SettlementImport
        fields = [
            "id",
            "courier",
            "courier_name",
            "filename",
            "status",
            "revision",
            "reference",
            "date",
            "net_amount",
            "received_amount",
            "error",
            "created_at",
            "confirmed_at",
            "page_count",
            "remaining_amount",
            "replaces",
        ]
        read_only_fields = fields


class SettlementImportViewSet(PrivateFinanceViewSet):
    queryset = (
        SettlementImport.objects.defer("source")
        .select_related("courier")
        .annotate(received_amount=Sum("receipts__amount", default=0))
        .order_by("-created_at", "id")
    )
    serializer_class = ImportSerializer
    search_fields = ["reference", "filename", "courier__name"]
    filterset_fields = ["status", "courier"]

    @transaction.atomic
    def create(self, request):
        data = serializers.Serializer(data=request.data)
        data.fields["courier"] = serializers.UUIDField()
        data.fields["file"] = serializers.FileField()
        data.is_valid(raise_exception=True)
        uploaded = data.validated_data["file"]
        if uploaded.size > MAX_BYTES:
            raise ValidationError("Statement files must be at most 8 MB.")
        blob = uploaded.read(MAX_BYTES + 1)
        try:
            detect_document_kind(blob, uploaded.name)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        courier = Courier.objects.filter(
            pk=data.validated_data["courier"], workspace=request.user.workspace
        ).first()
        if not courier:
            raise ValidationError("Select a courier from this workspace.")
        Workspace.objects.select_for_update().get(pk=request.user.workspace_id)
        digest = hashlib.sha256(blob).hexdigest()
        existing = self.get_queryset().filter(digest=digest).exclude(status="VOID").first()
        if existing:
            return Response({**self.get_serializer(existing).data, "duplicate": True})
        if self.get_queryset().filter(status__in=["QUEUED", "PROCESSING"]).count() >= 5:
            raise ValidationError("Five imports are already processing. Wait for them to finish.")
        statement = SettlementImport.objects.create(
            workspace=request.user.workspace,
            courier=courier,
            filename=PurePath(uploaded.name.replace("\\", "/")).name[:200],
            digest=digest,
            source=blob,
            replaces=self.get_queryset().filter(digest=digest, status="VOID").first(),
        )
        audit(request, "Settlement.Uploaded", statement)
        return Response(
            self.get_serializer(self.get_queryset().get(pk=statement.pk)).data, status=201
        )

    def retrieve(self, request, pk=None):
        obj = self.get_object()
        result = self.get_serializer(obj).data
        result.update(
            {
                "extracted": obj.extracted,
                "review": obj.review,
                "validation": assess(obj) if obj.status == "REVIEW" else obj.confirmed_result,
            }
        )
        return Response(result)

    @action(detail=True, methods=["post"], url_path="save-review")
    @transaction.atomic
    def save_review(self, request, pk=None):
        self.get_object()
        obj = lock_statement(request, pk, request.data.get("revision", -1))
        if obj.status != "REVIEW":
            raise ValidationError("Only draft reviews can be edited.")
        obj.review = validate_structure(request.data.get("review"))
        obj.revision += 1
        obj.save(update_fields=["review", "revision", "updated_at"])
        audit(request, "Settlement.ReviewSaved", obj, {"revision": obj.revision})
        return Response({"revision": obj.revision, "validation": assess(obj)})

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        self.get_object()
        confirm(request, pk, request.data.get("revision", -1))
        return self.retrieve(request, pk)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        self.get_object()
        reason = serializers.CharField(max_length=500).run_validation(
            request.data.get("reason", "")
        )
        reverse_statement(request, pk, request.data.get("revision", -1), reason)
        return self.retrieve(request, pk)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def revise(self, request, pk=None):
        import copy

        self.get_object()
        obj = lock_statement(request, pk, request.data.get("revision", -1))
        if obj.status != "VOID":
            raise ValidationError(
                "Reverse the confirmation first. Original records remain unchanged."
            )
        existing = self.get_queryset().filter(digest=obj.digest).exclude(status="VOID").first()
        if existing:
            return Response(self.get_serializer(existing).data)
        review = copy.deepcopy(obj.review)
        review.update(source_confirmed=False, ownership_confirmed=False)
        revised = SettlementImport.objects.create(
            workspace=obj.workspace,
            courier=obj.courier,
            filename=obj.filename,
            digest=obj.digest,
            source=bytes(obj.source),
            extracted=obj.extracted,
            review=review,
            status="REVIEW",
            replaces=obj,
        )
        audit(request, "Settlement.RevisionCreated", revised, {"replaces": str(obj.pk)})
        return Response(
            self.get_serializer(self.get_queryset().get(pk=revised.pk)).data, status=201
        )

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def retry(self, request, pk=None):
        self.get_object()
        obj = lock_statement(request, pk, request.data.get("revision", -1))
        if obj.status != "ERROR":
            raise ValidationError(
                "Retry is available only for failed imports. Create a corrected draft for a reversed import."
            )
        # Voided imports retain the reviewed document, not a fresh guessed interpretation.
        obj.status = "REVIEW" if obj.review else "QUEUED"
        obj.revision += 1
        obj.error = ""
        if obj.cost_updates.exists():
            # Prior immutable cost history must never be reused/overwritten.
            raise ValidationError(
                "For a corrected cost statement, upload the courier's revised file with a new hash. Previous cost history is retained."
            )
        obj.save(update_fields=["status", "revision", "error", "updated_at"])
        audit(request, "Settlement.Retried", obj)
        return self.retrieve(request, pk)

    @action(detail=True, methods=["get"])
    def source(self, request, pk=None):
        obj = self.get_object()
        if "page" in request.query_params:
            if obj.extracted.get("document_kind", "pdf") != "pdf":
                raise ValidationError(
                    "Page previews are available for PDF statements only. Download the original spreadsheet to inspect it."
                )
            page = serializers.IntegerField(min_value=1, max_value=25).run_validation(
                request.query_params["page"]
            )
            try:
                image = run_document(bytes(obj.source), page=page)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            return HttpResponse(image, content_type="image/png")
        response = HttpResponse(
            bytes(obj.source), content_type=source_content_type(bytes(obj.source), obj.filename)
        )
        suffix = PurePath(obj.filename).suffix.lower()
        response["Content-Disposition"] = f'attachment; filename="settlement-source{suffix}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response
