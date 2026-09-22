from datetime import timedelta

from django.contrib import admin
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.admin import BlogPostAdmin
from apps.core.models import BlogPost, Enquiry


class PublicSiteTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.published = BlogPost.objects.create(
            title="Test article",
            slug="test-live",
            excerpt="A useful guide",
            body="## Topic\n\nUseful copy",
            published=True,
            published_at=timezone.now(),
        )
        self.draft = BlogPost.objects.create(
            title="Private draft",
            slug="test-draft",
            excerpt="Not public",
            body="Draft notes",
            published=False,
        )
        self.future = BlogPost.objects.create(
            title="Future",
            slug="test-future",
            excerpt="Future",
            body="Future copy",
            published=True,
            published_at=timezone.now() + timedelta(days=1),
        )

    def test_public_blog_hides_drafts_and_future_posts(self):
        response = self.client.get("/api/public/blog/?search=Test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["slug"] for p in response.data["results"]], ["test-live"])
        for slug in ["test-draft", "test-future"]:
            self.assertEqual(self.client.get(f"/api/public/blog/{slug}/").status_code, 404)
        self.assertEqual(self.client.get("/api/public/blog/test-live/").status_code, 200)

    def test_public_blog_cannot_be_written(self):
        self.assertEqual(
            self.client.post("/api/public/blog/", {"title": "Injected"}).status_code, 405
        )
        self.assertEqual(self.client.delete("/api/public/blog/test-live/").status_code, 405)

    def test_search_includes_category_and_excerpt_without_exposing_drafts(self):
        self.published.category = "Unique delivery guide"
        self.published.save()
        for term in ["unique delivery", "useful guide"]:
            response = self.client.get("/api/public/blog/", {"search": term, "page_size": 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([p["slug"] for p in response.data["results"]], ["test-live"])

    def test_admin_publish_sets_date_and_preserves_scheduled_date(self):
        editor = BlogPostAdmin(BlogPost, admin.site)
        self.draft.published = True
        editor.save_model(None, self.draft, None, True)
        self.draft.refresh_from_db()
        self.assertIsNotNone(self.draft.published_at)
        self.assertEqual(self.client.get("/api/public/blog/test-draft/").status_code, 200)
        planned_date = self.future.published_at
        editor.save_model(None, self.future, None, True)
        self.future.refresh_from_db()
        self.assertEqual(self.future.published_at, planned_date)
        self.assertEqual(self.client.get("/api/public/blog/test-future/").status_code, 404)

    def test_enquiry_is_saved_and_not_exposed(self):
        response = self.client.post(
            "/api/public/enquiries/",
            {
                "name": "Test sender",
                "email": "demo@example.test",
                "business": "Test store",
                "message": "Please arrange a product walkthrough",
                "privacy_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Enquiry.objects.count(), 1)
        self.assertNotIn("email", response.data)
        self.assertEqual(self.client.get("/api/public/enquiries/").status_code, 405)

    def test_enquiry_rejects_missing_ack_and_honeypot_and_is_throttled(self):
        data = {
            "name": "Test",
            "email": "demo@example.test",
            "message": "Please share product details",
            "privacy_acknowledged": False,
        }
        self.assertEqual(
            self.client.post("/api/public/enquiries/", data, format="json").status_code, 400
        )
        data.update(privacy_acknowledged=True, website="spam")
        self.assertEqual(
            self.client.post("/api/public/enquiries/", data, format="json").status_code, 400
        )
        for _ in range(4):
            response = self.client.post("/api/public/enquiries/", data, format="json")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(Enquiry.objects.count(), 0)

    def test_public_config_never_returns_credentials(self):
        response = self.client.get("/api/public/config/")
        self.assertEqual(
            set(response.data),
            {"business_name", "support_email", "business_address", "legal_reviewed"},
        )
