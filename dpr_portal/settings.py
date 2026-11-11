"""
Django settings for the DPR dashboard — a standalone, self-contained project.

Completely separate from the jcpl portal: its own process, URLs and port. It
only *reads* the DPR Google Sheet (live CSV export) and *triggers* the n8n DPR
pull webhook. Nothing is stored on the server beyond Django's own bookkeeping.

Server-wide convention: every project on this box reads its env from the single
shared /root/.env. No per-project .env files. All keys here are namespaced
JCLP_DPR_* so they never collide with the jcpl portal's keys.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv('/root/.env')


def _env_list(name, default=''):
    """Parse a comma-separated env var into a stripped, non-empty list."""
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(',') if item.strip()]


# A dev fallback key is fine here: this project stores no user data and runs
# behind the same box. Set JCLP_DPR_DJANGO_SECRET_KEY in /root/.env for prod.
SECRET_KEY = os.environ.get(
    'JCLP_DPR_DJANGO_SECRET_KEY',
    'dev-insecure-dpr-dashboard-key-change-me',
)

DEBUG = os.environ.get('JCLP_DPR_DJANGO_DEBUG', 'False').lower() in ('1', 'true', 'yes')

ALLOWED_HOSTS = _env_list('JCLP_DPR_DJANGO_ALLOWED_HOSTS', '*')
CSRF_TRUSTED_ORIGINS = _env_list('JCLP_DPR_DJANGO_CSRF_TRUSTED_ORIGINS')


INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.staticfiles',
    'dashboard',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'dpr_portal.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'dpr_portal.wsgi.application'

# Minimal SQLite only to satisfy Django's defaults — this project defines no
# models and persists nothing of its own here.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── DPR-specific configuration (all from the shared /root/.env) ──────────────
# The Google Sheet the dharmik fetch-and-dump n8n flow writes to, and the tab
# holding the per-row DPR detail (Direct JR Date + produced qty).
DPR_SHEET_ID = os.environ.get('JCLP_DPR_SHEET_ID', '').strip()
DPR_SHEET_TAB = os.environ.get('JCLP_DPR_SHEET_TAB', 'DPR all Plant').strip()

# Data source: the DPR database API (dpr-api, FastAPI on this host). The
# dashboard reads direct_jr_lines from it, paged 10,000 rows per call.
DPR_API_URL = os.environ.get(
    'JCLP_DPR_API_URL', 'http://127.0.0.1:8010/api/direct_jr_lines').strip()
DPR_API_KEY = os.environ.get('JCLP_DPR_API_KEY', '').strip()
DPR_API_PAGE_SIZE = int(os.environ.get('JCLP_DPR_API_PAGE_SIZE', '10000'))

# n8n pull is no longer used: data comes straight from the database API, so the
# "Pull from iON → Sheet" button is switched off (empty URL = disabled/hidden).
DPR_PULL_WEBHOOK_URL = ''
DPR_PULL_TIMEOUT = int(os.environ.get('JCLP_DPR_PULL_TIMEOUT', '300'))
