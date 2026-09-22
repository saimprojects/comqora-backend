import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.storage import storages
from django.core.management import call_command
from django.db import OperationalError
from django.test import Client, SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.core.checks import production_config
from apps.core.storage import AdminStaticFilesStorage


class DeploymentChecksTests(SimpleTestCase):
    @override_settings(
        DEBUG=False,
        FRONTEND_URL="http://localhost:5173",
        ALLOWED_HOSTS=["*"],
        REQUIRE_EMAIL_VERIFICATION=True,
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
    )
    def test_rejects_unsafe_production_configuration(self):
        codes = {error.id for error in production_config(None)}
        self.assertEqual(codes, {"comqora.E001", "comqora.E002", "comqora.E003"})

    @override_settings(
        DEBUG=False,
        FRONTEND_URL="https://comqora.com",
        ALLOWED_HOSTS=["comqora.up.railway.app"],
        REQUIRE_EMAIL_VERIFICATION=True,
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST="",
    )
    def test_requires_smtp_host(self):
        self.assertEqual([error.id for error in production_config(None)], ["comqora.E004"])

    @override_settings(DEBUG=False, STATIC_URL="/static/")
    def test_jazzmin_directory_url_keeps_strict_file_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = AdminStaticFilesStorage(location=directory)
            self.assertEqual(storage.url("vendor/bootswatch"), "/static/vendor/bootswatch")
            with self.assertRaises(ValueError):
                storage.url("missing-file.css")


@override_settings(
    DEBUG=False,
    SECURE_SSL_REDIRECT=True,
    ALLOWED_HOSTS=["testserver", "healthcheck.railway.app", "comqora.up.railway.app"],
    CSRF_TRUSTED_ORIGINS=["https://comqora.com"],
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
class ProductionHTTPTests(TestCase):
    def test_railway_healthcheck_accepts_http_and_tests_database(self):
        response = self.client.get("/api/health/", HTTP_HOST="healthcheck.railway.app")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        with patch(
            "apps.core.health.connection.cursor", side_effect=OperationalError("private db details")
        ):
            response = self.client.get("/api/health/", HTTP_HOST="healthcheck.railway.app")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private db details", response.content)

    def test_other_routes_still_require_https_and_do_not_cache(self):
        self.assertEqual(self.client.get("/api/billing/plans/").status_code, 301)
        response = self.client.get("/api/billing/plans/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Vercel-CDN-Cache-Control"], "no-store")

    def test_frontend_origin_csrf_works_but_foreign_origins_fail(self):
        client = Client(enforce_csrf_checks=True)
        headers = {"HTTP_HOST": "comqora.up.railway.app", "HTTP_X_FORWARDED_PROTO": "https"}
        response = client.get("/api/auth/csrf/", **headers)
        self.assertTrue(response.cookies["csrftoken"]["secure"])
        self.assertEqual(response.cookies["csrftoken"]["samesite"], "Lax")
        token = response.json()["csrfToken"]
        payload = {"email": "missing@example.test", "password": "wrong"}
        response = client.post(
            "/api/auth/login/",
            payload,
            HTTP_ORIGIN="https://comqora.com",
            HTTP_X_CSRFTOKEN=token,
            **headers,
        )
        self.assertEqual(response.status_code, 400)  # Auth validation, not CSRF rejection.
        response = client.post(
            "/api/auth/login/",
            payload,
            HTTP_ORIGIN="https://untrusted.example",
            HTTP_X_CSRFTOKEN=token,
            **headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_production_admin_renders_with_collected_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(
                STATIC_ROOT=Path(directory),
                STORAGES={
                    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                    "staticfiles": {"BACKEND": "apps.core.storage.AdminStaticFilesStorage"},
                },
            ):
                call_command("collectstatic", interactive=False, verbosity=0)
                response = self.client.get("/admin/login/", HTTP_X_FORWARDED_PROTO="https")
                self.assertEqual(response.status_code, 200)
                admin = User.objects.create_superuser(
                    username="deployment-admin", email="deployment@example.test", password=None
                )
                self.client.force_login(admin)
                response = self.client.get("/admin/", HTTP_X_FORWARDED_PROTO="https")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-theme-base="/static/vendor/bootswatch"')
                css_url = storages["staticfiles"].url("vendor/adminlte/css/adminlte.min.css")
                self.assertIn(css_url.encode(), response.content)
                self.assertTrue(Path(directory, css_url.removeprefix("/static/")).exists())
