from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Create a local-only Jazzmin administrator. Never changes an existing account."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True)

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Only available in DEBUG. Use createsuperuser for production.")
        if User.objects.filter(email="admin@sellflow.local").exists():
            self.stdout.write("Local administrator already exists; no changes made.")
            return
        validate_password(options["password"])
        User.objects.create_superuser(
            username="sellflow-admin",
            email="admin@sellflow.local",
            password=options["password"],
            email_verified=True,
        )
        self.stdout.write(
            self.style.SUCCESS("Local Jazzmin administrator created. Username: sellflow-admin")
        )
