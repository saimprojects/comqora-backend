"""Install the hardened AdminSite before Django registers admin models."""

from django.contrib.admin.apps import AdminConfig


class JazzminSuperuserAdminConfig(AdminConfig):
    default_site = "apps.core.admin_site.JazzminSuperuserAdminSite"
