from django.apps import AppConfig


class MessagingConfig(AppConfig):
    name = "apps.messaging"

    def ready(self):
        from . import signals  # noqa: F401
