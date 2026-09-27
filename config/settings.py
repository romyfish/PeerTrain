import os
import sys
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _csv_env(name: str, default: str = "") -> list[str]:
    return [
        item.strip()
        for item in os.getenv(name, default).split(",")
        if item.strip()
    ]


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _csrf_origin_for_host(host: str) -> str:
    normalized = host.strip().rstrip("/")
    if normalized.startswith(("http://", "https://")):
        return normalized
    return f"https://{normalized}"


SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-peertrain-dev-key-change-me",
)
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = _csv_env("ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = list(
    dict.fromkeys(
        [
            _csrf_origin_for_host(host)
            for host in ALLOWED_HOSTS
            if host not in {"127.0.0.1", "localhost"}
        ]
        + _csv_env("CSRF_TRUSTED_ORIGINS", "")
    )
)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "training",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG


STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "landing"


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
BOUNDARY_JUDGE_ENABLED = _bool_env("BOUNDARY_JUDGE_ENABLED") and "test" not in sys.argv
BOUNDARY_JUDGE_MODEL = os.getenv("BOUNDARY_JUDGE_MODEL", OPENAI_MODEL)
BOUNDARY_JUDGE_MIN_CONFIDENCE = float(
    os.getenv("BOUNDARY_JUDGE_MIN_CONFIDENCE", "0.85")
)
BOUNDARY_JUDGE_REASONING_EFFORT = os.getenv(
    "BOUNDARY_JUDGE_REASONING_EFFORT",
    "medium",
)
BOUNDARY_JUDGE_TIMEOUT_SECONDS = float(
    os.getenv("BOUNDARY_JUDGE_TIMEOUT_SECONDS", "30")
)
BOUNDARY_JUDGE_MAX_RETRIES = int(os.getenv("BOUNDARY_JUDGE_MAX_RETRIES", "1"))
BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE = float(
    os.getenv("BOUNDARY_JUDGE_REVIEW_SAMPLE_RATE", "0.10")
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
LLM_PROVIDER = os.getenv(
    "LLM_PROVIDER",
    "openai" if OPENAI_API_KEY else "google",
).lower()
ADMIN_REGISTRATION_CODE = os.getenv("ADMIN_REGISTRATION_CODE", "peertrain-admin")
