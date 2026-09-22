import logging
import secrets
from smtplib import SMTPException

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.core.api import audit
from apps.core.models import Workspace

from . import password_otp
from .access import LOCKED_DETAIL, SUPPORT_CONTACT, locked_payload
from .models import User

logger = logging.getLogger(__name__)
EMAIL_UNAVAILABLE = "Email could not be sent right now. Please try again later or contact support."


def send_account_email(subject, message, user):
    """Keep mail outages from crashing account creation or exposing SMTP details."""
    try:
        sent = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [user.email])
    except (OSError, SMTPException, ImportError, ImproperlyConfigured, ValueError) as exc:
        # Exception text may contain recipients or provider credentials; log only its type.
        logger.error(
            "Account email delivery failed (%s). Check SMTP configuration.", type(exc).__name__
        )
        return False
    if sent != 1:
        logger.error("Account email backend did not accept the message.")
        return False
    return True


class AuthThrottle(AnonRateThrottle):
    scope = "auth"


class UserSerializer(serializers.ModelSerializer):
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    has_dashboard_access = serializers.BooleanField(read_only=True)
    has_ai_access = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "workspace_name",
            "email_verified",
            "is_staff",
            "dashboard_access_state",
            "has_dashboard_access",
            "has_ai_access",
        ]
        read_only_fields = [
            "id",
            "email",
            "role",
            "workspace_name",
            "email_verified",
            "is_staff",
            "dashboard_access_state",
            "has_dashboard_access",
            "has_ai_access",
        ]


class Registration(serializers.Serializer):
    first_name = serializers.CharField(max_length=80)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    workspace_name = serializers.CharField(max_length=120)

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, data):
        validate_password(
            data["password"], User(email=data["email"], first_name=data["first_name"])
        )
        return data


def verification_email(user):
    token = signing.dumps({"uid": user.pk, "email": user.email}, salt="verify-email")
    return send_account_email(
        "Verify your Comqora account",
        f"Confirm your email: {settings.FRONTEND_URL}/verify-email?token={token}",
        user,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def csrf(request):
    return Response({"csrfToken": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        serializer = Registration(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            workspace = Workspace.objects.create(name=data["workspace_name"])
            user = User.objects.create_user(
                username=secrets.token_hex(16),
                email=data["email"],
                password=data["password"],
                first_name=data["first_name"],
                workspace=workspace,
            )
        email_sent = verification_email(user)
        return Response(
            {
                "user": UserSerializer(user).data,
                "verification_required": settings.REQUIRE_EMAIL_VERIFICATION,
                "verification_email_sent": email_sent,
                "email_warning": "" if email_sent else EMAIL_UNAVAILABLE,
                "approval_required": True,
                "detail": LOCKED_DETAIL,
                "support_contact": SUPPORT_CONTACT,
            },
            status=201,
        )


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        email = str(request.data.get("email", "")).lower().strip()
        import hashlib

        key = "login-fail:" + hashlib.sha256(email.encode()).hexdigest()
        if cache.get(key, 0) >= 8:
            return Response({"detail": "Too many attempts. Try again in 15 minutes."}, status=429)
        existing = User.objects.filter(email__iexact=email).first()
        user = authenticate(
            request,
            username=existing.username if existing else email,
            password=request.data.get("password", ""),
        )
        if not user:
            if not cache.add(key, 1, 900):
                try:
                    cache.incr(key)
                except ValueError:
                    cache.add(key, 1, 900)
            return Response({"detail": "Email or password is incorrect."}, status=400)
        if settings.REQUIRE_EMAIL_VERIFICATION and not user.email_verified:
            return Response({"detail": "Verify your email before signing in."}, status=403)
        cache.delete(key)
        login(request, user)
        if user.workspace_id:
            audit(request, "auth.login")
        return Response(UserSerializer(user).data)


@api_view(["GET", "PATCH"])
def me(request):
    if request.user.workspace_id and not request.user.has_dashboard_access:
        if request.method == "PATCH":
            return Response(locked_payload(request.user), status=423)
        result = UserSerializer(request.user).data
        result.update(locked_payload(request.user))
        return Response(result)
    if request.method == "PATCH":
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(UserSerializer(request.user).data)


@api_view(["POST"])
def sign_out(request):
    if request.user.workspace_id:
        audit(request, "auth.logout")
    logout(request)
    return Response({"detail": "Signed out."})


@api_view(["POST"])
def request_password_otp(request):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not user.check_password(request.data.get("current_password", "")):
            return Response({"detail": "Current password is incorrect."}, status=400)
        data, status = password_otp.issue(user, send_account_email)
        return Response(data, status=status)


@api_view(["POST"])
def change_password(request):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not user.check_password(request.data.get("current_password", "")):
            return Response({"detail": "Current password is incorrect."}, status=400)
        try:
            validate_password(request.data.get("password", ""), user)
        except DjangoValidationError as exc:
            return Response({"password": exc.messages}, status=400)
        if not password_otp.verify(user, request.data.get("otp", "")):
            return Response(
                {
                    "detail": "Invalid or expired code. Request a new code after five failed attempts."
                },
                status=400,
            )
        user.set_password(request.data["password"])
        user.save(update_fields=["password"])
        audit(request, "auth.password_changed")
    update_session_auth_hash(request, user)
    return Response({"detail": "Password updated. Other sessions have been invalidated."})


@method_decorator(csrf_protect, name="dispatch")
class RecoveryView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        user = User.objects.filter(
            email__iexact=str(request.data.get("email", "")).strip(), is_active=True
        ).first()
        if user:
            token = default_token_generator.make_token(user)
            link = f"{settings.FRONTEND_URL}/reset-password?uid={user.pk}&token={token}"
            send_account_email(
                "Reset your Comqora password",
                f"Reset your password: {link}\nThis link expires in one hour.",
                user,
            )
        return Response(
            {
                "detail": "If an account exists, we will attempt to send a reset link. If no email arrives, try again later or contact support."
            }
        )


@method_decorator(csrf_protect, name="dispatch")
class ResetView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        try:
            user = User.objects.get(pk=int(request.data.get("uid", 0)), is_active=True)
        except (User.DoesNotExist, ValueError, TypeError):
            user = None
        if not user or not default_token_generator.check_token(user, request.data.get("token", "")):
            return Response({"detail": "This reset link is invalid or expired."}, status=400)
        try:
            validate_password(request.data.get("password", ""), user)
        except DjangoValidationError as exc:
            return Response({"password": exc.messages}, status=400)
        user.set_password(request.data["password"])
        user.save()
        return Response({"detail": "Password reset. You can now sign in."})


@method_decorator(csrf_protect, name="dispatch")
class VerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        try:
            payload = signing.loads(
                request.data.get("token", ""), salt="verify-email", max_age=86400
            )
            user = User.objects.get(pk=payload["uid"], email=payload["email"])
        except (signing.BadSignature, User.DoesNotExist, KeyError):
            return Response({"detail": "Verification link is invalid or expired."}, status=400)
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        if user.has_dashboard_access:
            return Response({"detail": "Email verified. You can now sign in."})
        return Response(
            {
                "detail": f"Email verified. {LOCKED_DETAIL}",
                "approval_required": True,
                "support_contact": SUPPORT_CONTACT,
            }
        )


@method_decorator(csrf_protect, name="dispatch")
class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        user = (
            request.user
            if request.user.is_authenticated
            else User.objects.filter(
                email__iexact=str(request.data.get("email", "")).strip(), is_active=True
            ).first()
        )
        if user and not user.email_verified:
            if not verification_email(user) and request.user.is_authenticated:
                return Response({"detail": EMAIL_UNAVAILABLE}, status=503)
        return Response(
            {
                "detail": "If an unverified account exists, we will attempt to send a verification link. If no email arrives, try again later or contact support."
            }
        )


@api_view(["GET", "POST", "PATCH"])
def team(request):
    if not request.user.has_dashboard_access:
        return Response(locked_payload(request.user), status=423)
    if request.user.role != "owner" or not request.user.workspace_id:
        return Response({"detail": "Only workspace owners can manage the team."}, status=403)
    if request.method == "POST":
        data = Registration(data={**request.data, "workspace_name": request.user.workspace.name})
        data.is_valid(raise_exception=True)
        role = request.data.get("role", "staff")
        if role not in ["manager", "staff", "viewer"]:
            return Response({"detail": "Choose manager, staff, or viewer."}, status=400)
        d = data.validated_data
        user = User.objects.create_user(
            username=secrets.token_hex(16),
            email=d["email"],
            password=d["password"],
            first_name=d["first_name"],
            role=role,
            dashboard_access_state=User.DASHBOARD_ACTIVE,
            workspace=request.user.workspace,
        )
        verification_email(user)
        audit(request, "team.member_created", user)
    if request.method == "PATCH":
        user = (
            User.objects.filter(pk=request.data.get("id"), workspace=request.user.workspace)
            .exclude(role="owner")
            .first()
        )
        if not user:
            return Response({"detail": "Team member not found."}, status=404)
        role = request.data.get("role", user.role)
        if role not in ["manager", "staff", "viewer"]:
            return Response({"detail": "Invalid role."}, status=400)
        user.role = role
        user.is_active = request.data.get("is_active", user.is_active) is True
        user.save(update_fields=["role", "is_active"])
        audit(request, "team.member_updated", user)
    return Response(
        [
            {**UserSerializer(u).data, "is_active": u.is_active}
            for u in User.objects.filter(workspace=request.user.workspace)
        ]
    )
