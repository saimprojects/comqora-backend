import secrets
from datetime import timedelta

from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import PasswordChangeOTP


def digest(user, code):
    return salted_hmac(
        "password-change-otp", f"{user.pk}:{user.email}:{user.password}:{code}", algorithm="sha256"
    ).hexdigest()


def issue(user, send_email):
    # Caller locks the user row, serializing sends and verification across replicas.
    now = timezone.now()
    previous = PasswordChangeOTP.objects.filter(user=user).first()
    if previous and now < previous.created_at + timedelta(seconds=60):
        return {"detail": "Wait 60 seconds before requesting another code."}, 429
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge, _ = PasswordChangeOTP.objects.update_or_create(
        user=user,
        defaults={
            "digest": digest(user, code),
            "created_at": now,
            "expires_at": now + timedelta(minutes=10),
            "attempts": 0,
        },
    )
    if not send_email(
        "Your Comqora password change code",
        f"Your code is {code}. It expires in 10 minutes. Do not share it. If you did not request this, do not use this code.",
        user,
    ):
        challenge.digest = ""
        challenge.save(update_fields=["digest"])
        return {"detail": "Could not send the code. Try again in a minute or contact support."}, 503
    return {
        "detail": "Verification code sent to your account email. It expires in 10 minutes."
    }, 200


def verify(user, code):
    challenge = PasswordChangeOTP.objects.filter(user=user).first()
    if (
        not challenge
        or not challenge.digest
        or challenge.expires_at <= timezone.now()
        or challenge.attempts >= 5
    ):
        return False
    challenge.attempts += 1
    challenge.save(update_fields=["attempts"])
    if not constant_time_compare(challenge.digest, digest(user, str(code))):
        return False
    challenge.delete()
    return True
