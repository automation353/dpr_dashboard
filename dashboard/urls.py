"""URLs for the DPR dashboard."""
from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='dpr_index'),
    path('api/data', views.api_data, name='dpr_api_data'),
    path('api/detail', views.api_detail, name='dpr_api_detail'),
    path('api/pull', views.api_pull, name='dpr_api_pull'),
    path('healthz', views.healthz, name='dpr_healthz'),
]
