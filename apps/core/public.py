from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from rest_framework import generics, permissions, serializers
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from .models import BlogPost, Enquiry


class PostSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogPost
        fields = [
            "title",
            "slug",
            "excerpt",
            "category",
            "body",
            "author",
            "published_at",
            "updated_at",
        ]


class BlogList(generics.ListAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = PostSerializer

    def get_queryset(self):
        rows = BlogPost.objects.filter(published=True, published_at__lte=timezone.now())
        search = self.request.query_params.get("search", "")[:100]
        return (
            rows.filter(
                Q(title__icontains=search)
                | Q(category__icontains=search)
                | Q(excerpt__icontains=search)
            )
            if search
            else rows
        )


class BlogDetail(generics.RetrieveAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = PostSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return BlogPost.objects.filter(published=True, published_at__lte=timezone.now())


class EnquiryThrottle(AnonRateThrottle):
    rate = "5/hour"


class EnquirySerializer(serializers.ModelSerializer):
    message = serializers.CharField(min_length=10, max_length=3000)
    privacy_acknowledged = serializers.BooleanField(write_only=True)
    website = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = Enquiry
        fields = ["name", "email", "business", "message", "privacy_acknowledged", "website"]

    def validate(self, attrs):
        if not attrs.pop("privacy_acknowledged"):
            raise serializers.ValidationError("Please read and acknowledge the privacy notice.")
        if attrs.pop("website", ""):
            raise serializers.ValidationError("Unable to submit this enquiry.")
        return attrs


class EnquiryCreate(generics.CreateAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [EnquiryThrottle]
    serializer_class = EnquirySerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {"detail": "Your enquiry has been received. The Comqora team can now review it."},
            status=201,
        )


class PublicConfig(generics.GenericAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(
            {
                "business_name": settings.PUBLIC_BUSINESS_NAME,
                "support_email": settings.PUBLIC_SUPPORT_EMAIL,
                "business_address": settings.PUBLIC_BUSINESS_ADDRESS,
                "legal_reviewed": settings.PUBLIC_LEGAL_REVIEWED,
            }
        )
