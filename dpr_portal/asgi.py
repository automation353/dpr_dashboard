"""ASGI config for the DPR dashboard."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dpr_portal.settings')

application = get_asgi_application()
