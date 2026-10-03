"""
报表URL配置
"""
from django.urls import path
from .views import (
    DashboardView, DailyReportView, ExportView, SystemMonitorView,
    AnonymizedTrendReportView, AnonymizationPolicyView,
)

urlpatterns = [
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    path('daily-report/', DailyReportView.as_view(), name='daily-report'),
    path('export/', ExportView.as_view(), name='export'),
    path('system-monitor/', SystemMonitorView.as_view(), name='system-monitor'),
    path('anonymized-reports/trend/', AnonymizedTrendReportView.as_view(), name='anonymized-trend-report'),
    path('anonymized-reports/policy/', AnonymizationPolicyView.as_view(), name='anonymization-policy'),
]
