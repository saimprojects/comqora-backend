from threading import Event

from django.core.management.base import BaseCommand

from apps.assistant.worker import process_turns, research_loop


class Command(BaseCommand):
    help = "Process queued AI research; --loop runs continuously (also included in sync_tracking --loop)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args, **options):
        stop = Event()
        try:
            if options["loop"]:
                research_loop(stop)
            else:
                self.stdout.write(f"Processed {process_turns()} research request(s).")
        except KeyboardInterrupt:
            stop.set()
            self.stdout.write("Research worker stopped.")
