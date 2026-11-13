"""URLs for the DPR dashboard."""
from django.urls import path

from . import views

urlpatterns = [
    path('', views.index, name='dpr_index'),
    path('api/data', views.api_data, name='dpr_api_data'),
    path('api/detail', views.api_detail, name='dpr_api_detail'),
    path('api/export/start', views.api_export_start, name='dpr_export_start'),
    path('api/export/status', views.api_export_status, name='dpr_export_status'),
    path('api/export/cancel', views.api_export_cancel, name='dpr_export_cancel'),
    path('api/export/download', views.api_export_download, name='dpr_export_download'),
    path('api/pull', views.api_pull, name='dpr_api_pull'),
    path('healthz', views.healthz, name='dpr_healthz'),
]
