"""Root URLconf for the DPR dashboard."""
from django.urls import include, path

urlpatterns = [
    path('', include('dashboard.urls')),
]
