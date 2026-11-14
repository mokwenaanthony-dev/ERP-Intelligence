"""Settings for the automated test suite (used by pytest.ini).

Supplies a throwaway secret key so tests run without a .env file. Uses SQLite
unless DB_ENGINE is set, which is how CI runs the same tests on PostgreSQL.
"""
import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-key-not-for-any-real-environment")
os.environ.setdefault("LOG_DIR", os.path.join(tempfile.gettempdir(), "erp_test_logs"))

from .settings import *  # noqa: E402,F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast hashing, tests only
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# WhiteNoise serves collected static files, which tests never need.
MIDDLEWARE = [m for m in MIDDLEWARE if not m.startswith("whitenoise.")]  # noqa: F405

# Forecasting is off in tests unless a test turns it on (with a fake service, see tests/test_planning.py).
ML_SERVICE_URL = ""
ML_SERVICE_TOKEN = "test-token-at-least-16"
