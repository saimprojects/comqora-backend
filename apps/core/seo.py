"""Live public sitemaps. URLs always belong to the configured frontend, not the proxy."""

from xml.etree.ElementTree import Element, SubElement, tostring

from django.conf import settings
from django.core.paginator import EmptyPage, Paginator
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import BlogPost

PAGE_SIZE = 1000
PUBLIC_PATHS = (
    "/",
    "/pricing",
    "/blog",
    "/contact",
    "/privacy",
    "/terms",
    "/cookies",
    "/acceptable-use",
    "/refunds",
    "/security",
)


def public_posts():
    return BlogPost.objects.filter(published=True, published_at__lte=timezone.now()).order_by("pk")


def public_url(path):
    return settings.FRONTEND_URL.rstrip("/") + path


def xml_response(root):
    return HttpResponse(
        tostring(root, encoding="utf-8", xml_declaration=True),
        content_type="application/xml; charset=utf-8",
    )


def xml_root(name):
    return Element(name, xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")


@never_cache
@require_safe
def sitemap_index(request):
    root = xml_root("sitemapindex")
    paths = ["/sitemap-pages.xml"]
    count = public_posts().count()
    paths.extend(
        f"/sitemap-blog-{page}.xml" for page in range(1, (count + PAGE_SIZE - 1) // PAGE_SIZE + 1)
    )
    for path in paths:
        SubElement(SubElement(root, "sitemap"), "loc").text = public_url(path)
    return xml_response(root)


@never_cache
@require_safe
def sitemap_pages(request):
    root = xml_root("urlset")
    for path in PUBLIC_PATHS:
        SubElement(SubElement(root, "url"), "loc").text = public_url(path)
    return xml_response(root)


@never_cache
@require_safe
def sitemap_blog(request, page):
    try:
        posts = Paginator(
            public_posts().only("slug", "updated_at", "published_at"), PAGE_SIZE
        ).page(page)
    except EmptyPage as exc:
        raise Http404("Sitemap page not found") from exc
    root = xml_root("urlset")
    for post in posts:
        entry = SubElement(root, "url")
        SubElement(entry, "loc").text = public_url(f"/blog/{post.slug}")
        SubElement(entry, "lastmod").text = max(post.updated_at, post.published_at).isoformat()
    return xml_response(root)
