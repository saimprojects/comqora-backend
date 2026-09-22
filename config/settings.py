
from pathlib import Path

import cloudinary
import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

# --------------------------------------------------
# ENVIRONMENT & PRODUCTION CONFIGURATION
# --------------------------------------------------

IS_PRODUCTION = bool(
    env("RAILWAY_ENVIRONMENT", default="")
    or env("RAILWAY_PROJECT_ID", default="")
    or env("RAILWAY_SERVICE_ID", default="")
)

# Railway must always use production security settings.
# Local development continues to use the existing DEBUG variable.
DEBUG = False if IS_PRODUCTION else env.bool("DEBUG", default=False)

SECRET_KEY = env("SECRET_KEY")
SECRET_KEY_FALLBACKS = env.list("SECRET_KEY_FALLBACKS", default=[])

ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "backend"]
    if DEBUG
    else ["comqora.com", "www.comqora.com"],
)

railway_domain = env("RAILWAY_PUBLIC_DOMAIN", default="").strip()

if railway_domain:
    ALLOWED_HOSTS.extend(
        [railway_domain, "healthcheck.railway.app"]
    )

INSTALLED_APPS = [
    "jazzmin",
    "apps.core.admin_config.JazzminSuperuserAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "apps.accounts",
    "apps.billing",
    "apps.core",
    "apps.catalog",
    "apps.logistics",
    "apps.orders",
    "apps.marketing",
    "apps.finance",
    "apps.messaging",
    "apps.assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.PrivateResponseMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.DashboardLockMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="sqlite:///db.sqlite3",
    )
}

DATABASES["default"]["CONN_MAX_AGE"] = env.int(
    "DB_CONN_MAX_AGE",
    default=60,
)

DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

if not DEBUG and DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    raise ValueError(
        "Production requires PostgreSQL. Set DATABASE_URL."
    )

# --------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Karachi"

USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------
# BUSINESS CONFIGURATION
# --------------------------------------------------

PUBLIC_BUSINESS_NAME = env(
    "PUBLIC_BUSINESS_NAME",
    default="Comqora",
)

PUBLIC_SUPPORT_EMAIL = env(
    "PUBLIC_SUPPORT_EMAIL",
    default="support@mostmailer.com",
)

PUBLIC_BUSINESS_ADDRESS = env(
    "PUBLIC_BUSINESS_ADDRESS",
    default="Kasur, Punjab, Pakistan",
)

PUBLIC_LEGAL_REVIEWED = env.bool(
    "PUBLIC_LEGAL_REVIEWED",
    default=False,
)

FAZITA_API_KEY = env(
    "FAZITA_API_KEY",
    default="",
)

# --------------------------------------------------
# WAHA CONFIGURATION
# --------------------------------------------------

WAHA_ENABLED = env.bool(
    "WAHA_ENABLED",
    default=False,
)

WAHA_BASE_URL = env(
    "WAHA_BASE_URL",
    default="",
)

WAHA_API_KEY = env(
    "WAHA_API_KEY",
    default="",
)

WAHA_SESSION_MODE = env(
    "WAHA_SESSION_MODE",
    default="MULTI",
).upper()

WAHA_CORE_WORKSPACE_ID = env(
    "WAHA_CORE_WORKSPACE_ID",
    default="",
)

WAHA_ALLOW_HTTP = env.bool(
    "WAHA_ALLOW_HTTP",
    default=False,
)

WAHA_WEBHOOK_SECRET = env(
    "WAHA_WEBHOOK_SECRET",
    default="",
)

WAHA_WEBHOOK_URL = env(
    "WAHA_WEBHOOK_URL",
    default="",
)

if WAHA_SESSION_MODE not in {"CORE", "PLUS", "MULTI"}:
    raise ValueError(
        "WAHA_SESSION_MODE must be MULTI (recommended), "
        "CORE (legacy single), or PLUS (legacy multi alias)."
    )

# --------------------------------------------------
# STATIC FILES
# --------------------------------------------------

STATIC_URL = "/static/"

STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage"
    },
    "staticfiles": {
        "BACKEND": "apps.core.storage.AdminStaticFilesStorage"
    },
}

# --------------------------------------------------
# CORS CONFIGURATION
# --------------------------------------------------

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    if DEBUG
    else [
        "https://comqora.com",
        "https://www.comqora.com",
    ],
)

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=CORS_ALLOWED_ORIGINS,
)

# --------------------------------------------------
# SESSION & COOKIE SECURITY
# --------------------------------------------------

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

SESSION_COOKIE_AGE = 60 * 60 * 12

# --------------------------------------------------
# HTTPS & PRODUCTION SECURITY
# --------------------------------------------------

SECURE_SSL_REDIRECT = (
    True
    if IS_PRODUCTION
    else env.bool(
        "SECURE_SSL_REDIRECT",
        default=not DEBUG,
    )
)

SECURE_REDIRECT_EXEMPT = [
    r"^api/health/$"
]

SECURE_HSTS_SECONDS = (
    0 if DEBUG else 31536000
)

SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)

SECURE_CONTENT_TYPE_NOSNIFF = True

X_FRAME_OPTIONS = "DENY"

PASSWORD_RESET_TIMEOUT = 3600

# --------------------------------------------------
# EMAIL CONFIGURATION
# --------------------------------------------------

EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)

EMAIL_HOST = env(
    "EMAIL_HOST",
    default="",
)

EMAIL_PORT = env.int(
    "EMAIL_PORT",
    default=587,
)

EMAIL_HOST_USER = env(
    "EMAIL_HOST_USER",
    default="",
)

EMAIL_HOST_PASSWORD = env(
    "EMAIL_HOST_PASSWORD",
    default="",
)

EMAIL_USE_TLS = env.bool(
    "EMAIL_USE_TLS",
    default=True,
)

EMAIL_TIMEOUT = env.int(
    "EMAIL_TIMEOUT",
    default=10,
)

DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL",
    default="Comqora <no-reply@comqora.com>",
)

FRONTEND_URL = env(
    "FRONTEND_URL",
    default="http://localhost:5173"
    if DEBUG
    else "https://comqora.com",
)

REQUIRE_EMAIL_VERIFICATION = env.bool(
    "REQUIRE_EMAIL_VERIFICATION",
    default=not DEBUG,
)

# --------------------------------------------------
# TRACKING & LOGISTICS
# --------------------------------------------------

TRACKING_WEBHOOK_SECRET = env(
    "TRACKING_WEBHOOK_SECRET",
    default="",
)

POSTEX_API_TOKEN = env(
    "POSTEX_API_TOKEN",
    default="",
)

TRACKING_ENABLED = env.bool(
    "TRACKING_ENABLED",
    default=True,
)

TRACKING_POLL_SECONDS = max(
    60,
    env.int(
        "TRACKING_POLL_SECONDS",
        default=60,
    ),
)

# --------------------------------------------------
# CACHE
# --------------------------------------------------

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "sellflow_cache",
        "OPTIONS": {
            "MAX_ENTRIES": 100000
        },
    }
}

# --------------------------------------------------
# DJANGO REST FRAMEWORK
# --------------------------------------------------

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "NUM_PROXIES": env.int(
        "NUM_PROXIES",
        default=0 if DEBUG else 1,
    ),
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer"
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication"
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated"
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/hour",
        "user": "3000/hour",
        "auth": "20/hour",
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# --------------------------------------------------
# API DOCUMENTATION
# --------------------------------------------------

SPECTACULAR_SETTINGS = {
    "TITLE": "Comqora API",
    "DESCRIPTION": "Workspace-scoped COD operations and profit intelligence.",
    "VERSION": "1.0.0",
}

# --------------------------------------------------
# JAZZMIN ADMIN
# --------------------------------------------------

JAZZMIN_SETTINGS = {
    "site_title": "Comqora Control",
    "site_header": "Comqora",
    "site_brand": "Comqora",
    "welcome_sign": "Comqora platform administration",
    "copyright": "Comqora",
    "search_model": [
        "accounts.User",
        "orders.Order",
    ],
    "icons": {
        "auth": "fas fa-shield-alt",
        "accounts.User": "fas fa-user",
        "orders.Order": "fas fa-shopping-bag",
        "catalog.Product": "fas fa-box",
        "assistant.Connection": "fas fa-plug",
        "assistant.AssistantModel": "fas fa-brain",
        "assistant.Conversation": "fas fa-comments",
        "assistant.Turn": "fas fa-comment-dots",
        "assistant.ProposedAction": "fas fa-check-circle",
    },
}

# --------------------------------------------------
# CLOUDINARY
# --------------------------------------------------

cloudinary.config(
    cloud_name=env(
        "CLOUDINARY_CLOUD_NAME",
        default="",
    ),
    api_key=env(
        "CLOUDINARY_API_KEY",
        default="",
    ),
    api_secret=env(
        "CLOUDINARY_API_SECRET",
        default="",
    ),
    secure=True,
)

# --------------------------------------------------
# LOGGING
# --------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler"
        }
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
