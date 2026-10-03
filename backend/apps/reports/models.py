"""
报表模型
"""
from django.db import models
from apps.authentication.models import User


class DailyReport(models.Model):
    """每日报表模型"""
    report_date = models.DateField('报表日期', unique=True)
    in_count = models.IntegerField('入库次数', default=0)
    in_total = models.DecimalField('入库总量', max_digits=12, decimal_places=2, default=0)
    out_count = models.IntegerField('出库次数', default=0)
    out_total = models.DecimalField('出库总量', max_digits=12, decimal_places=2, default=0)
    warning_count = models.IntegerField('预警数量', default=0)
    summary = models.TextField('报表摘要', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'rp_daily_report'
        verbose_name = '每日报表'
        verbose_name_plural = verbose_name
        ordering = ['-report_date']
    
    def __str__(self):
        return f"报表 - {self.report_date}"


class AnonymizedBucketSnapshot(models.Model):
    """
    脱敏报表周期快照。

    同一（策略版本, 用途, 粒度, 周期）的脱敏结果首次生成后永久固化：
    - 重叠时间窗口的请求复用同一快照，无法通过窗口差分反推记录；
    - 迟到更正不会改写已发布快照，无法通过前后对比推断更正内容；
    - 指纹不含用户身份，不同权限用户请求同一报表得到相同结果。
    """
    fingerprint = models.CharField('快照指纹', max_length=64, unique=True, db_index=True)
    purpose = models.CharField('报表用途', max_length=50)
    granularity = models.CharField('统计粒度', max_length=10)
    period_key = models.CharField('周期标识', max_length=20)
    period_start = models.DateField('周期开始')
    period_end = models.DateField('周期结束')
    strategy_version = models.CharField('脱敏策略版本', max_length=20)
    payload = models.JSONField('脱敏结果', default=dict)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='anonymized_snapshots', verbose_name='首次生成人'
    )
    created_at = models.DateTimeField('固化时间', auto_now_add=True)

    class Meta:
        db_table = 'rp_anonymized_bucket_snapshot'
        verbose_name = '脱敏报表快照'
        verbose_name_plural = verbose_name
        ordering = ['period_start']
        indexes = [
            models.Index(fields=['purpose', 'granularity', 'period_key']),
        ]

    def __str__(self):
        return f"{self.purpose} - {self.period_key} ({self.strategy_version})"
