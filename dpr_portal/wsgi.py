"""WSGI config for the DPR dashboard."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dpr_portal.settings')

application = get_wsgi_application()
