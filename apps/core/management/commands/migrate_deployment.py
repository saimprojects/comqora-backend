import time

from django.core.management import BaseCommand, CommandError, call_command
from django.db import connection


class Command(BaseCommand):
    help = "Apply deployment migrations, serialized across PostgreSQL service starts."

    def handle(self, *args, **options):
        lock_id = 6841937201
        locked = False
        try:
            if connection.vendor == "postgresql":
                deadline = time.monotonic() + 120
                while True:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                        locked = cursor.fetchone()[0]
                    if locked:
                        break
                    if time.monotonic() >= deadline:
                        raise CommandError(
                            "Another deployment is still migrating. Retry deployment shortly."
                        )
                    time.sleep(1)
            call_command("migrate", interactive=False, verbosity=options["verbosity"])
        finally:
            if locked:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
