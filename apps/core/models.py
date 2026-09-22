import uuid

from django.conf import settings
from django.db import models


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, default="PKR")
    logo = models.BinaryField(null=True, blank=True, editable=False)
    logo_updated_at = models.DateTimeField(null=True, blank=True, editable=False)
    business_address = models.CharField(max_length=500, blank=True)
    business_phone = models.CharField(max_length=40, blank=True)
    business_email = models.EmailField(blank=True)
    invoice_template = models.CharField(max_length=20, default="studio")
    invoice_footer = models.CharField(max_length=300, default="Thank you for shopping with us.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class TenantModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class AuditEvent(TenantModel):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100, blank=True)
    detail = models.JSONField(default=dict)


class SharedCacheEntry(models.Model):
    """Schema used by Django's shared database cache; no Redis service required."""

    cache_key = models.CharField(max_length=255, primary_key=True)
    value = models.TextField()
    expires = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "sellflow_cache"


class BlogPost(models.Model):
    title = models.CharField(max_length=180)
    slug = models.SlugField(unique=True, max_length=200)
    excerpt = models.CharField(max_length=320)
    category = models.CharField(max_length=60, default="Operations")
    body = models.TextField(
        help_text="Use blank lines for paragraphs and ## for section headings. HTML is displayed as text."
    )
    author = models.CharField(max_length=100, default="Comqora editorial")
    published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-id"]

    def __str__(self):
        return self.title


class Enquiry(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    business = models.CharField(max_length=140, blank=True)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    handled = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
