"""
脱敏报表管线。

设计目标（对应检查与审计要求）：
1. 按用途裁剪字段：每种用途对应一份字段与维度授权清单，敏感字段
   （案件名称、人员身份、批次、供应商、备注等）在进入聚合管线之前即被丢弃。
2. 小样本分组抑制/合并：组内底层记录数不足 min_group_size 的分组先并入
   “其他”合并桶，合并后仍不足的整桶抑制。
3. 策略版本化：所有处理参数登记在 STRATEGY 中，每次导出记录版本号，
   旧快照始终可按当时版本解释。
4. 防差分推断：
   - 请求窗口向外对齐到自然周/月边界，且只输出已完整结束的周期，
     重叠窗口查询只会落到同一组固定分桶上；
   - 每个（策略版本, 用途, 粒度, 周期）的脱敏结果首次生成后固化为
     不可变快照，迟到更正不会改写已发布内容；
   - 快照指纹不含用户身份，不同权限用户请求同一报表得到相同结果。
5. 导出摘要：随报表返回数据范围、字段清单、策略参数与抑制统计，
   供审核人确认。
"""
import hashlib
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count, Sum
from django.utils import timezone

from apps.warehouse.models import StockIn, StockOut

# ==================== 策略注册表 ====================

STRATEGY_VERSION = 'v1.0'

STRATEGY = {
    'version': STRATEGY_VERSION,
    # 组内底层记录数低于该值时触发合并/抑制（覆盖“只有一两条记录的小组”）
    'min_group_size': 3,
    # merge: 小样本组先并入“其他”桶，合并桶仍不足时再抑制
    'suppression_mode': 'merge',
    'merged_label': '其他（小样本合并）',
    'granularities': ['week', 'month'],
    # 只输出已完整结束的周期，进行中的周期一律排除
    'completed_periods_only': True,
}

# 用途 -> 授权清单。字段裁剪是第一道防线：清单之外的列根本不会出现在结果中。
PURPOSES = {
    'external_inspection': {
        'label': '外部检查',
        # 外部检查账号（普通用户角色）即可申请
        'roles': ['superadmin', 'admin', 'user'],
        # 聚合维度（period 之外）：只到品类，不下钻品种/货物
        'dimensions': ['category'],
        # 输出的度量字段
        'metrics': ['in_count', 'in_total', 'out_count', 'out_total'],
        # 总量按该步长确定性取整（ROUND_HALF_UP），削弱精确总量被反推的风险
        'rounding_step': Decimal('1'),
    },
    'internal_audit': {
        'label': '内部审计',
        'roles': ['superadmin', 'admin'],
        'dimensions': ['category', 'variety'],
        'metrics': ['in_count', 'in_total', 'out_count', 'out_total'],
        'rounding_step': Decimal('0.01'),
    },
}

# 任何用途都不会输出的敏感字段，写入摘要供审核人确认
EXCLUDED_SENSITIVE_FIELDS = [
    '案件名称/备注(remark)', '货物名称与编码', '批次号(batch_no)',
    '供应商(supplier)', '操作人(operator)', '领用人(receiver)',
    '领用部门(receiver_dept)',
]

RECORD_SCOPE_NOTE = '入库：全部入库记录；出库：仅已完成(completed)记录'


class AnonymizationError(Exception):
    """脱敏报表参数或权限错误"""
    def __init__(self, message, code=400):
        self.message = message
        self.code = code
        super().__init__(message)


# ==================== 周期工具 ====================

def period_bounds(granularity, day):
    """返回某日所在自然周期的 (起, 止) 日期。周以周一为起点。"""
    if granularity == 'week':
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    if granularity == 'month':
        start = day.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1) - timedelta(days=1)
        else:
            end = start.replace(month=start.month + 1) - timedelta(days=1)
        return start, end
    raise AnonymizationError(f'不支持的统计粒度: {granularity}')


def period_key(granularity, start):
    if granularity == 'week':
        iso_year, iso_week, _ = start.isocalendar()
        return f'{iso_year}-W{iso_week:02d}'
    return f'{start.year}-{start.month:02d}'


def align_window(granularity, start, end, today=None):
    """
    将请求窗口向外对齐到整周期边界，并截断掉尚未结束的周期。
    返回 (周期列表, 是否截断了进行中周期)。周期列表元素为
    {'key', 'start', 'end'}，按时间升序。
    """
    today = today or timezone.now().date()
    if start > end:
        raise AnonymizationError('开始日期不能晚于结束日期')

    aligned_start, _ = period_bounds(granularity, start)
    _, aligned_end = period_bounds(granularity, end)

    periods = []
    truncated = False
    cursor = aligned_start
    while cursor <= aligned_end:
        p_start, p_end = period_bounds(granularity, cursor)
        if STRATEGY['completed_periods_only'] and p_end >= today:
            truncated = True
            break
        periods.append({
            'key': period_key(granularity, p_start),
            'start': p_start,
            'end': p_end,
        })
        cursor = p_end + timedelta(days=1)
    return periods, truncated


# ==================== 聚合与脱敏 ====================

def _round(value, step):
    """按策略步长确定性取整，保证同一输入永远得到同一输出。"""
    value = Decimal(str(value or 0))
    if step <= 0:
        return value
    return (value / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step


def _aggregate_period(purpose_cfg, p_start, p_end):
    """
    聚合单个周期内的收发记录，返回分组行列表。
    只查询用途授权维度所需的列，敏感字段不进入结果集。
    """
    dimensions = purpose_cfg['dimensions']
    # 维度 -> ORM 查询字段
    field_map = {
        'category': 'goods__variety__category__name',
        'variety': 'goods__variety__name',
    }
    select_fields = [field_map[d] for d in dimensions]

    groups = {}

    def bucket_for(row):
        key = tuple(row[f] or '未分类' for f in select_fields)
        return groups.setdefault(key, {
            'dims': dict(zip(dimensions, key)),
            'in_count': 0, 'in_total': Decimal('0'),
            'out_count': 0, 'out_total': Decimal('0'),
        })

    in_rows = (
        StockIn.objects
        .filter(stock_in_time__date__gte=p_start, stock_in_time__date__lte=p_end)
        .values(*select_fields)
        .annotate(cnt=Count('id'), total=Sum('quantity'))
    )
    for row in in_rows:
        bucket = bucket_for(row)
        bucket['in_count'] = row['cnt']
        bucket['in_total'] = row['total'] or Decimal('0')

    out_rows = (
        StockOut.objects
        .filter(
            status='completed',
            stock_out_time__date__gte=p_start,
            stock_out_time__date__lte=p_end,
        )
        .values(*select_fields)
        .annotate(cnt=Count('id'), total=Sum('quantity'))
    )
    for row in out_rows:
        bucket = bucket_for(row)
        bucket['out_count'] = row['cnt']
        bucket['out_total'] = row['total'] or Decimal('0')

    return list(groups.values())


def _apply_k_anonymity(rows, purpose_cfg):
    """
    小样本处理：样本量（组内入库+出库记录数）不足 min_group_size 的组
    并入“其他”桶；合并桶仍不足时整桶抑制。
    返回 (输出行, 合并组数, 抑制组数)。
    """
    k = STRATEGY['min_group_size']
    dimensions = purpose_cfg['dimensions']

    def sample_size(row):
        return row['in_count'] + row['out_count']

    big = [r for r in rows if sample_size(r) >= k]
    small = [r for r in rows if sample_size(r) < k]

    merged_count = 0
    suppressed_count = 0
    output = list(big)

    if small:
        merged = {
            'dims': {d: STRATEGY['merged_label'] for d in dimensions},
            'in_count': sum(r['in_count'] for r in small),
            'in_total': sum((r['in_total'] for r in small), Decimal('0')),
            'out_count': sum(r['out_count'] for r in small),
            'out_total': sum((r['out_total'] for r in small), Decimal('0')),
        }
        if sample_size(merged) >= k:
            output.append(merged)
            merged_count = len(small)
        else:
            # 合并后仍不足 k：整桶抑制，不输出任何小组信息
            suppressed_count = len(small)

    return output, merged_count, suppressed_count


def compute_bucket_payload(purpose, p_key, p_start, p_end):
    """计算单个周期的脱敏结果（不落库，供快照固化）。"""
    purpose_cfg = PURPOSES[purpose]
    rows = _aggregate_period(purpose_cfg, p_start, p_end)
    source_group_count = len(rows)
    rows, merged_count, suppressed_count = _apply_k_anonymity(rows, purpose_cfg)

    step = purpose_cfg['rounding_step']
    out_rows = []
    for row in sorted(rows, key=lambda r: tuple(r['dims'].values())):
        item = dict(row['dims'])
        item.update({
            'in_count': row['in_count'],
            'in_total': float(_round(row['in_total'], step)),
            'out_count': row['out_count'],
            'out_total': float(_round(row['out_total'], step)),
        })
        out_rows.append(item)

    return {
        'period': {
            'key': p_key,
            'start': p_start.isoformat(),
            'end': p_end.isoformat(),
        },
        'rows': out_rows,
        'stats': {
            'source_groups': source_group_count,
            'merged_groups': merged_count,
            'suppressed_groups': suppressed_count,
        },
    }


def bucket_fingerprint(purpose, granularity, p_key):
    """快照指纹：不含用户身份，同一(策略,用途,粒度,周期)全局唯一。"""
    raw = '|'.join([STRATEGY_VERSION, purpose, granularity, p_key])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def get_or_create_bucket_snapshot(purpose, granularity, period, user=None):
    """
    获取周期的脱敏快照；不存在则计算并固化。
    已固化的快照永不重算——迟到更正不会改写已发布内容。
    """
    from .models import AnonymizedBucketSnapshot  # 避免循环导入

    fingerprint = bucket_fingerprint(purpose, granularity, period['key'])
    snapshot = AnonymizedBucketSnapshot.objects.filter(fingerprint=fingerprint).first()
    if snapshot:
        return snapshot, False

    payload = compute_bucket_payload(
        purpose, period['key'], period['start'], period['end']
    )
    snapshot = AnonymizedBucketSnapshot.objects.create(
        fingerprint=fingerprint,
        purpose=purpose,
        granularity=granularity,
        period_key=period['key'],
        period_start=period['start'],
        period_end=period['end'],
        strategy_version=STRATEGY_VERSION,
        payload=payload,
        created_by=user if user and user.is_authenticated else None,
    )
    return snapshot, True


# ==================== 报表组装 ====================

def build_trend_report(user, purpose, granularity, start, end):
    """
    组装窗口级脱敏趋势报表。
    返回 {'rows': [...], 'summary': {...}}，summary 供审核人确认
    数据范围与处理规则。
    """
    if purpose not in PURPOSES:
        raise AnonymizationError(f'无效的报表用途: {purpose}')
    purpose_cfg = PURPOSES[purpose]
    if user.role not in purpose_cfg['roles']:
        raise AnonymizationError('当前角色无权申请该用途的报表', code=403)
    if granularity not in STRATEGY['granularities']:
        raise AnonymizationError(f'不支持的统计粒度: {granularity}')

    periods, truncated = align_window(granularity, start, end)

    rows = []
    period_summaries = []
    snapshot_ids = []
    total_merged = 0
    total_suppressed = 0

    for period in periods:
        snapshot, _ = get_or_create_bucket_snapshot(purpose, granularity, period, user)
        snapshot_ids.append(snapshot.id)
        payload = snapshot.payload
        stats = payload['stats']
        total_merged += stats['merged_groups']
        total_suppressed += stats['suppressed_groups']
        for row in payload['rows']:
            rows.append({'period': period['key'], **row})
        period_summaries.append({
            'period': period['key'],
            'start': payload['period']['start'],
            'end': payload['period']['end'],
            'output_rows': len(payload['rows']),
            'merged_groups': stats['merged_groups'],
            'suppressed_groups': stats['suppressed_groups'],
            # 该周期数据固化时间：迟到更正不会反映在此时间之前发布的快照中
            'data_frozen_at': snapshot.created_at.isoformat(),
        })

    summary = {
        'purpose': purpose,
        'purpose_label': purpose_cfg['label'],
        'strategy_version': STRATEGY_VERSION,
        'strategy': {
            'min_group_size': STRATEGY['min_group_size'],
            'suppression_mode': STRATEGY['suppression_mode'],
            'rounding_step': str(purpose_cfg['rounding_step']),
            'granularity': granularity,
            'completed_periods_only': STRATEGY['completed_periods_only'],
        },
        'requested_window': {'start': start.isoformat(), 'end': end.isoformat()},
        'aligned_window': {
            'start': periods[0]['start'].isoformat() if periods else None,
            'end': periods[-1]['end'].isoformat() if periods else None,
        },
        'current_period_excluded': truncated,
        'dimensions': ['period'] + purpose_cfg['dimensions'],
        'fields': ['period'] + purpose_cfg['dimensions'] + purpose_cfg['metrics'],
        'excluded_sensitive_fields': EXCLUDED_SENSITIVE_FIELDS,
        'record_scope': RECORD_SCOPE_NOTE,
        'periods': period_summaries,
        'totals': {
            'output_rows': len(rows),
            'merged_groups': total_merged,
            'suppressed_groups': total_suppressed,
        },
        'snapshot_ids': snapshot_ids,
        'generated_at': timezone.now().isoformat(),
    }
    return {'rows': rows, 'summary': summary}


def describe_policy(user=None):
    """返回策略说明，供审核人确认处理规则。"""
    purposes = []
    for key, cfg in PURPOSES.items():
        purposes.append({
            'purpose': key,
            'label': cfg['label'],
            'required_roles': cfg['roles'],
            'dimensions': ['period'] + cfg['dimensions'],
            'fields': ['period'] + cfg['dimensions'] + cfg['metrics'],
            'rounding_step': str(cfg['rounding_step']),
            'allowed': bool(user is None or user.role in cfg['roles']),
        })
    return {
        'strategy_version': STRATEGY_VERSION,
        'strategy': {
            'min_group_size': STRATEGY['min_group_size'],
            'suppression_mode': STRATEGY['suppression_mode'],
            'merged_label': STRATEGY['merged_label'],
            'granularities': STRATEGY['granularities'],
            'completed_periods_only': STRATEGY['completed_periods_only'],
        },
        'excluded_sensitive_fields': EXCLUDED_SENSITIVE_FIELDS,
        'record_scope': RECORD_SCOPE_NOTE,
        'purposes': purposes,
    }
