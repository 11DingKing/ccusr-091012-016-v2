"""
报表URL配置
"""
from django.urls import path
from .masked_views import (
    MaskedExportRecordListView,
    MaskedReportPurposesView,
    MaskedTrendExportView,
    MaskedTrendReportView,
)
from .views import DashboardView, DailyReportView, ExportView, SystemMonitorView

urlpatterns = [
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    path('daily-report/', DailyReportView.as_view(), name='daily-report'),
    path('export/', ExportView.as_view(), name='export'),
    path('system-monitor/', SystemMonitorView.as_view(), name='system-monitor'),
    path('masked-reports/purposes/', MaskedReportPurposesView.as_view(), name='masked-report-purposes'),
    path('masked-reports/trend/', MaskedTrendReportView.as_view(), name='masked-trend-report'),
    path('masked-reports/export/', MaskedTrendExportView.as_view(), name='masked-trend-export'),
    path('masked-reports/records/', MaskedExportRecordListView.as_view(), name='masked-export-records'),
]
