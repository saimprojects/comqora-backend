from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.assistant.admin import ConnectionForm
from apps.assistant.credentials import decrypt_key, encrypt_key
from apps.assistant.models import AssistantModel, Connection, Conversation, Turn
from apps.assistant.provider import UNAVAILABLE, api_key
from tests.billing_fixtures import paid_workspace


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    FAZITA_API_KEY="",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class CredentialTests(TestCase):
    def setUp(self):
        self.connection = Connection.objects.create(name="Test connection")
        self.admin = User.objects.create_superuser(
            username="secret-admin", email="admin@example.test", password="synthetic"
        )
        self.workspace = paid_workspace(name="Example")
        self.user = User.objects.create_user(
            username="workspace-user",
            email="user@example.test",
            workspace=self.workspace,
            dashboard_access_state="ACTIVE",
        )

    def form(self, **values):
        return ConnectionForm(
            instance=self.connection,
            data={
                "name": self.connection.name,
                "enabled": True,
                "daily_workspace_turn_limit": 100,
                "key_environment_variable": "FAZITA_API_KEY",
                **values,
            },
        )

    def test_encryption_round_trip_and_server_key_rotation(self):
        with override_settings(SECRET_KEY="old-server-secret", SECRET_KEY_FALLBACKS=[]):
            token = encrypt_key("synthetic-fazita-key")
            self.assertNotIn("synthetic-fazita-key", token)
            self.assertEqual(decrypt_key(token), "synthetic-fazita-key")
        with override_settings(SECRET_KEY="new-server-secret", SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(decrypt_key(token), "")
        with override_settings(
            SECRET_KEY="new-server-secret", SECRET_KEY_FALLBACKS=["old-server-secret"]
        ):
            self.assertEqual(decrypt_key(token), "synthetic-fazita-key")
        self.assertEqual(decrypt_key("broken"), "")

    def test_form_saves_encrypted_key_preserves_blank_replaces_and_clears(self):
        form = self.form(api_key="synthetic-one")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.connection.refresh_from_db()
        original = self.connection.encrypted_api_key
        self.assertNotIn("synthetic-one", original)
        self.assertEqual(api_key(self.connection), "synthetic-one")
        form = self.form(api_key="")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.connection.encrypted_api_key, original)
        form = self.form(api_key="synthetic-two")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(api_key(self.connection), "synthetic-two")
        form = self.form(clear_api_key=True)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.connection.encrypted_api_key, "")

    def test_invalid_forms_never_redisplay_a_secret(self):
        for values in [
            {"api_key": "synthetic-secret", "clear_api_key": True},
            {"api_key": "Bearer synthetic-secret"},
        ]:
            form = self.form(**values)
            self.assertFalse(form.is_valid())
            self.assertNotIn("synthetic-secret", form.as_p())
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.encrypted_api_key, "")

    def test_saved_key_has_precedence_and_corrupt_key_does_not_silently_use_fallback(self):
        with (
            override_settings(FAZITA_API_KEY="synthetic-env-key"),
            patch.dict("os.environ", {"FAZITA_API_KEY": ""}),
        ):
            self.assertEqual(api_key(self.connection), "synthetic-env-key")
            self.connection.encrypted_api_key = encrypt_key("synthetic-admin-key")
            self.assertEqual(api_key(self.connection), "synthetic-admin-key")
            self.connection.encrypted_api_key = "corrupt"
            self.assertEqual(api_key(self.connection), "")

    def test_admin_post_persists_secret_without_rendering_or_logging_it(self):
        self.client.force_login(self.admin)
        url = reverse("admin:assistant_connection_change", args=[self.connection.pk])
        response = self.client.post(
            url,
            {
                "name": "Test connection",
                "enabled": "on",
                "daily_workspace_turn_limit": "100",
                "key_environment_variable": "FAZITA_API_KEY",
                "api_key": "synthetic-admin-secret",
                "models-TOTAL_FORMS": "0",
                "models-INITIAL_FORMS": "0",
                "models-MIN_NUM_FORMS": "0",
                "models-MAX_NUM_FORMS": "1000",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(api_key(self.connection), "synthetic-admin-secret")
        page = self.client.get(url)
        self.assertContains(page, 'type="password"')
        self.assertNotContains(page, "synthetic-admin-secret")
        self.assertNotContains(page, self.connection.encrypted_api_key)
        self.assertNotIn(
            "synthetic-admin-secret", str(list(LogEntry.objects.values("change_message")))
        )

    def test_non_superuser_cannot_access_credentials_even_with_model_permissions(self):
        self.user.is_staff = True
        self.user.save()
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="assistant", content_type__model="connection"
            )
        )
        self.client.force_login(self.user)
        url = reverse("admin:assistant_connection_change", args=[self.connection.pk])
        # Existing platform admin gate redirects non-superusers to login.
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.post(url, {"api_key": "synthetic"}).status_code, 302)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.encrypted_api_key, "")

    def test_workspace_responses_hide_setup_and_internal_provider_errors(self):
        client = APIClient()
        client.force_authenticate(self.user)
        config = client.get("/api/assistant/config/")
        self.assertNotIn("setup_message", config.data)
        self.assertNotIn("Jazzmin", str(config.data))
        self.assertNotIn("key_environment_variable", str(config.data))
        self.connection.encrypted_api_key = encrypt_key("synthetic-hidden")
        self.connection.save()
        model = AssistantModel.objects.create(
            connection=self.connection, name="Assistant", model_id="synthetic-model", enabled=True
        )
        config = client.get("/api/assistant/config/")
        self.assertTrue(config.data["ready"])
        self.assertNotIn("synthetic-hidden", str(config.data))
        chat = Conversation.objects.create(workspace=self.workspace, user=self.user)
        turn = Turn.objects.create(
            workspace=self.workspace,
            conversation=chat,
            model=model,
            question="Hello",
            status="ERROR",
            error="Internal setup: Jazzmin FAZITA_API_KEY",
            finished_at=timezone.now(),
        )
        response = client.get(f"/api/assistant/conversations/{chat.pk}/")
        self.assertEqual(response.data["turns"][0]["error"], UNAVAILABLE)
        self.assertNotIn("Jazzmin", str(response.data))
        self.assertNotIn("FAZITA_API_KEY", str(response.data))
        response = client.post(
            f"/api/assistant/conversations/{chat.pk}/send/",
            {"question": "Hello", "request_key": str(turn.request_key)},
        )
        self.assertEqual(response.data["error"], UNAVAILABLE)
