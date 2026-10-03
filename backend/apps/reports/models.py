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


class FrozenBucket(models.Model):
    """已冻结的自然日桶标记

    每个自然日的源数据在首次发布时冻结为快照，之后到达的迟到更正
    不再改变该日的已发布统计，避免通过前后对比反推单条记录。
    """
    policy_version = models.CharField('策略版本', max_length=32)
    bucket_date = models.DateField('桶日期')
    record_count = models.IntegerField('冻结时源记录数', default=0)
    frozen_at = models.DateTimeField('冻结时间', auto_now_add=True)

    class Meta:
        db_table = 'rp_frozen_bucket'
        verbose_name = '冻结日期桶'
        verbose_name_plural = verbose_name
        ordering = ['-bucket_date']
        unique_together = ['policy_version', 'bucket_date']

    def __str__(self):
        return f"{self.policy_version} - {self.bucket_date}"


class ReleasedCell(models.Model):
    """已冻结的原子统计单元（自然日 × 货物）

    维度标签在冻结时快照保存，后续货物改类、改名不影响已发布口径；
    所有脱敏报表均由这些原子单元聚合而来，保证不同请求结果一致。
    """
    policy_version = models.CharField('策略版本', max_length=32)
    bucket_date = models.DateField('桶日期')
    goods_id = models.BigIntegerField('货物ID')
    category_name = models.CharField('品类名称快照', max_length=10, blank=True)
    variety_name = models.CharField('品种名称快照', max_length=20, blank=True)
    in_count = models.IntegerField('入库次数', default=0)
    in_quantity = models.DecimalField('入库数量', max_digits=14, decimal_places=2, default=0)
    out_count = models.IntegerField('出库次数', default=0)
    out_quantity = models.DecimalField('出库数量', max_digits=14, decimal_places=2, default=0)
    frozen_at = models.DateTimeField('冻结时间', auto_now_add=True)

    class Meta:
        db_table = 'rp_released_cell'
        verbose_name = '已发布原子单元'
        verbose_name_plural = verbose_name
        ordering = ['-bucket_date']
        unique_together = ['policy_version', 'bucket_date', 'goods_id']

    def __str__(self):
        return f"{self.bucket_date} - 货物{self.goods_id}"


class MaskedExportRecord(models.Model):
    """脱敏报表预览/导出审计记录

    记录每次发布采用的策略版本、数据范围、处理规则与数据指纹，
    供审核人事后核对；预览与导出均留痕。
    """
    ACTION_CHOICES = [
        ('preview', '预览'),
        ('export', '导出'),
    ]

    action = models.CharField('操作类型', max_length=20, choices=ACTION_CHOICES)
    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='masked_export_records', verbose_name='请求人'
    )
    requester_role = models.CharField('请求人角色', max_length=20, blank=True)
    purpose = models.CharField('报表用途', max_length=50)
    purpose_label = models.CharField('用途名称', max_length=50, blank=True)
    policy_version = models.CharField('策略版本', max_length=32)
    granularity = models.CharField('时间粒度', max_length=10)
    dimension = models.CharField('统计维度', max_length=20)
    requested_start = models.DateField('请求起始日期')
    requested_end = models.DateField('请求结束日期')
    effective_start = models.DateField('实际起始日期')
    effective_end = models.DateField('实际结束日期')
    k_threshold = models.IntegerField('小样本阈值', default=3)
    strategy = models.CharField('小样本处理策略', max_length=20)
    fields_released = models.JSONField('发布字段', default=list)
    fields_withheld = models.JSONField('裁剪字段', default=list)
    bucket_count = models.IntegerField('时间桶数', default=0)
    row_count = models.IntegerField('发布行数', default=0)
    released_row_count = models.IntegerField('正常发布行数', default=0)
    merged_row_count = models.IntegerField('合并行数', default=0)
    suppressed_cell_count = models.IntegerField('被抑制单元数', default=0)
    suppressed_record_count = models.IntegerField('被抑制记录数（内部留档）', default=0)
    data_fingerprint = models.CharField('数据指纹', max_length=64)
    summary = models.JSONField('导出摘要', default=dict)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'rp_masked_export_record'
        verbose_name = '脱敏报表导出记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_action_display()} - {self.purpose} - {self.created_at:%Y-%m-%d %H:%M}"
