from datetime import timedelta
from unittest.mock import patch
from xml.etree.ElementTree import fromstring

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import BlogPost

NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@override_settings(FRONTEND_URL="https://comqora.com", SECURE_SSL_REDIRECT=False)
class SitemapTests(TestCase):
    def setUp(self):
        BlogPost.objects.all().delete()
        self.post = BlogPost.objects.create(
            title="Published",
            slug="published",
            excerpt="Guide",
            body="Guide",
            published=True,
            published_at=timezone.now() - timedelta(days=1),
        )
        for slug, published, date in [
            ("draft", False, timezone.now()),
            ("scheduled", True, timezone.now() + timedelta(days=1)),
            ("undated", True, None),
        ]:
            BlogPost.objects.create(
                title=slug,
                slug=slug,
                body="Hidden",
                excerpt="Hidden",
                published=published,
                published_at=date,
            )

    def xml(self, name):
        response = self.client.get(f"/api/public/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/xml", response["Content-Type"])
        self.assertIn("no-store", response["Cache-Control"])
        return fromstring(response.content)

    def locations(self, root):
        return [node.text for node in root.findall(".//s:loc", NS)]

    def test_anonymous_sitemaps_use_frontend_origin_and_public_pages(self):
        self.assertEqual(
            self.locations(self.xml("sitemap.xml")),
            [
                "https://comqora.com/sitemap-pages.xml",
                "https://comqora.com/sitemap-blog-1.xml",
            ],
        )
        locations = self.locations(self.xml("sitemap-pages.xml"))
        self.assertIn("https://comqora.com/pricing", locations)
        self.assertIn("https://comqora.com/security", locations)
        self.assertNotIn("https://comqora.com/login", locations)
        root = self.xml("sitemap-blog-1.xml")
        self.assertEqual(self.locations(root), ["https://comqora.com/blog/published"])
        self.assertEqual(root.find("s:url/s:lastmod", NS).text, self.post.updated_at.isoformat())

    def test_publish_unpublish_and_scheduling_are_live_without_rebuild(self):
        draft = BlogPost.objects.get(slug="draft")
        draft.published = True
        draft.save()
        self.assertIn(
            "https://comqora.com/blog/draft", self.locations(self.xml("sitemap-blog-1.xml"))
        )
        self.post.published = False
        self.post.save()
        self.assertNotIn(
            "https://comqora.com/blog/published", self.locations(self.xml("sitemap-blog-1.xml"))
        )
        with patch("apps.core.seo.timezone.now", return_value=timezone.now() + timedelta(days=2)):
            self.assertIn(
                "https://comqora.com/blog/scheduled", self.locations(self.xml("sitemap-blog-1.xml"))
            )

    @patch("apps.core.seo.PAGE_SIZE", 1)
    def test_index_paginates_without_duplicate_or_missing_posts(self):
        BlogPost.objects.filter(slug="draft").update(published=True)
        self.assertEqual(len(self.locations(self.xml("sitemap.xml"))), 3)
        first = self.locations(self.xml("sitemap-blog-1.xml"))
        second = self.locations(self.xml("sitemap-blog-2.xml"))
        self.assertEqual(len(set(first + second)), 2)
        self.assertEqual(self.client.get("/api/public/sitemap-blog-3.xml").status_code, 404)
        self.assertEqual(self.client.get("/api/public/sitemap-blog-0.xml").status_code, 404)

    def test_empty_index_head_and_write_rejection(self):
        BlogPost.objects.all().delete()
        self.assertEqual(
            self.locations(self.xml("sitemap.xml")), ["https://comqora.com/sitemap-pages.xml"]
        )
        self.assertEqual(self.client.head("/api/public/sitemap.xml").status_code, 200)
        self.assertEqual(self.client.post("/api/public/sitemap.xml").status_code, 405)
