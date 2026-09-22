from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api import TenantViewSet
from apps.core.pricing import NamedCostSerializer, json_costs

from .models import Category, Packaging, Product, StockBatch


class CategorySerializer(serializers.ModelSerializer):
    def create(self, data):
        try:
            with transaction.atomic():
                return super().create(data)
        except IntegrityError:
            raise serializers.ValidationError({"name": "This category already exists."}) from None

    def update(self, instance, data):
        try:
            with transaction.atomic():
                return super().update(instance, data)
        except IntegrityError:
            raise serializers.ValidationError({"name": "This category already exists."}) from None

    class Meta:
        model = Category
        fields = ["id", "name", "created_at"]
        read_only_fields = ["id", "created_at"]
        validators = []

    def validate_name(self, value):
        qs = Category.objects.filter(
            workspace=self.context["request"].user.workspace, name__iexact=value
        )
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This category already exists.")
        return value


class CategoryViewSet(TenantViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    search_fields = ["name"]
    ordering_fields = ["name", "created_at"]

    @transaction.atomic
    def perform_update(self, serializer):
        super().perform_update(serializer)
        serializer.instance.products.update(category=serializer.instance.name)


class ProductSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        source="category_record", queryset=Category.objects.all(), required=False
    )
    category = serializers.CharField(read_only=True)
    stock = serializers.IntegerField(read_only=True)
    reserved = serializers.IntegerField(read_only=True)
    available = serializers.SerializerMethodField()

    def get_available(self, obj):
        return (obj.stock or 0) - (obj.reserved or 0)

    class Meta:
        model = Product
        exclude = ["workspace", "variant", "category_record"]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = []

    def validate_category_id(self, value):
        if value.workspace_id != self.context["request"].user.workspace_id:
            raise serializers.ValidationError("Category not found.")
        return value

    def validate(self, data):
        category = data.get("category_record")
        if category:
            data["category"] = category.name
        elif not self.instance:
            # Compatibility for older API clients: still create a proper category record.
            category, _ = Category.objects.get_or_create(
                workspace=self.context["request"].user.workspace,
                name__iexact="General",
                defaults={"name": "General"},
            )
            data.update(category_record=category, category=category.name)
        return data

    def validate_sku(self, value):
        qs = Product.objects.filter(workspace=self.context["request"].user.workspace, sku=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This SKU is already in use.")
        return value


class ProductViewSet(TenantViewSet):
    queryset = Product.objects.annotate(
        stock=Sum("batches__remaining_quantity", default=0),
        reserved=Sum("batches__reserved_quantity", default=0),
    )
    serializer_class = ProductSerializer
    search_fields = ["name", "sku", "category"]
    filterset_fields = ["is_active", "category", "category_record"]
    ordering_fields = ["name", "created_at", "selling_price"]
    ordering = ["-created_at", "-id"]

    def perform_create(self, serializer):
        super().perform_create(serializer)
        # Model creation does not populate the queryset's inventory annotations.
        # Reload the saved product so POST returns the same stock fields as GET.
        serializer.instance = self.get_queryset().get(pk=serializer.instance.pk)

    @action(detail=False, methods=["post"])
    def upload(self, request):
        import cloudinary
        import cloudinary.uploader
        from PIL import Image, UnidentifiedImageError

        file = request.FILES.get("image")
        if not cloudinary.config().api_secret:
            return Response(
                {"detail": "Configure Cloudinary credentials in Backend/.env to upload images."},
                status=503,
            )
        if not file or file.size > 5 * 1024 * 1024:
            return Response({"detail": "Choose an image smaller than 5 MB."}, status=400)
        try:
            image = Image.open(file)
            if (
                image.format not in ["JPEG", "PNG", "WEBP"]
                or image.width * image.height > 25_000_000
            ):
                raise ValueError()
            image.verify()
            file.seek(0)
        except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
            return Response({"detail": "Upload a valid JPEG, PNG, or WebP image."}, status=400)
        try:
            result = cloudinary.uploader.upload(
                file, folder=f"sellflow/{request.user.workspace_id}", resource_type="image"
            )
        except cloudinary.exceptions.Error:
            return Response(
                {"detail": "Image upload failed. Check Cloudinary configuration."}, status=502
            )
        return Response({"url": result["secure_url"]})


class StockBatchSerializer(serializers.ModelSerializer):
    extra_costs = NamedCostSerializer(many=True, required=False, max_length=30)
    product_name = serializers.CharField(source="product.name", read_only=True)
    transport_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, default=0
    )
    import_cost = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)

    class Meta:
        model = StockBatch
        exclude = ["workspace"]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "remaining_quantity",
            "reserved_quantity",
        ]
        extra_kwargs = {"purchased_quantity": {"min_value": 1}, "unit_cost": {"required": False}}

    def validate(self, data):
        if "purchase_amount" not in data and "unit_cost" not in data:
            raise serializers.ValidationError({"purchase_amount": "Enter the purchase amount."})
        return data

    def validate_product(self, value):
        if value.workspace_id != self.context["request"].user.workspace_id:
            raise serializers.ValidationError("Product not found.")
        return value

    def validate_received_at(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Receipt date cannot be in the future.")
        return value

    def create(self, data):
        from decimal import Decimal

        from apps.orders.services import money

        costs = data.get("extra_costs", [])
        extra = (
            data["transport_cost"]
            + data["import_cost"]
            + sum((c["amount"] for c in costs), Decimal(0))
        )
        amount = data.get("purchase_amount", data.get("unit_cost"))
        data["purchase_amount"] = amount
        base = (
            amount / data["purchased_quantity"] if data.get("purchase_mode") == "TOTAL" else amount
        )
        data["unit_cost"] = money(base + extra / data["purchased_quantity"])
        if data["unit_cost"] > Decimal("9999999999.99"):
            raise serializers.ValidationError(
                {"purchase_amount": "Landed unit cost exceeds the supported amount."}
            )
        data["extra_costs"] = json_costs(costs)
        data["remaining_quantity"] = data["purchased_quantity"]
        with transaction.atomic():
            Product.objects.select_for_update().get(pk=data["product"].pk)
            return super().create(data)


class StockBatchViewSet(TenantViewSet):
    queryset = StockBatch.objects.select_related("product")
    serializer_class = StockBatchSerializer
    http_method_names = ["get", "post", "head", "options"]
    filterset_fields = ["product"]
    search_fields = ["reference", "product__name"]


class PackagingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Packaging
        exclude = ["workspace"]
        read_only_fields = ["id", "created_at", "updated_at"]


class PackagingViewSet(TenantViewSet):
    queryset = Packaging.objects.all()
    serializer_class = PackagingSerializer
    search_fields = ["name"]
