import logging
import signal
from threading import Event, Thread

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.assistant.worker import research_loop
from apps.finance.worker import process_imports
from apps.logistics.tracking import sync_due
from apps.messaging.services import process_outbox


class Command(BaseCommand):
    help = "Run courier polling, WhatsApp, PDF settlements and AI research in one process; no Redis or Celery."

    def messaging_pass(self):
        try:
            close_old_connections()
            process_outbox()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "WhatsApp worker pass failed (%s)", type(exc).__name__
            )
        finally:
            close_old_connections()

    def messaging_loop(self, stop):
        while not stop.is_set():
            self.messaging_pass()
            stop.wait(5)

    def document_pass(self):
        try:
            process_imports()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Settlement worker pass failed (%s)", type(exc).__name__
            )

    def document_loop(self, stop):
        while not stop.is_set():
            self.document_pass()
            stop.wait(5)

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        stop = Event()
        sender = None
        documents = None
        researcher = None
        previous_sigterm = None
        if options["loop"]:
            previous_sigterm = signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
            sender = Thread(
                target=self.messaging_loop, args=(stop,), daemon=True, name="whatsapp-outbox"
            )
            sender.start()
            documents = Thread(
                target=self.document_loop, args=(stop,), daemon=True, name="settlement-imports"
            )
            documents.start()
            researcher = Thread(
                target=research_loop, args=(stop,), daemon=True, name="assistant-research"
            )
            researcher.start()
        try:
            while not stop.is_set():
                close_old_connections()
                count = sync_due(max(1, min(options["limit"], 1000)))
                if not options["loop"]:
                    self.messaging_pass()
                    self.document_pass()
                if count or not options["loop"]:
                    self.stdout.write(f"Checked {count} due shipments.")
                if not options["loop"]:
                    return
                stop.wait(5)
        except KeyboardInterrupt:
            self.stdout.write("Tracking worker stopped.")
        finally:
            stop.set()
            if sender:
                sender.join(timeout=5)
            if documents:
                documents.join(timeout=5)
            if researcher:
                researcher.join(timeout=5)
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
