"""
脱敏报表策略与构建服务。

安全设计要点：

1. 按授权字段裁剪：报表用途决定可见的时间粒度、统计维度与列集，
   明细字段与身份字段（货物名称、领用人、操作人等）一律不进入报表。
2. 小样本处理：以“品种 × 时间桶”为最细统计单元，记录数小于阈值 k 的
   单元按策略合并（merge）或抑制（suppress）；被抑制单元的记录不进入
   任何已发布数字（包括各层合计），防止用合计反减出小组数值。
3. 防对比反推：
   - 时间窗口先按冻结地平线截断，再向完整时间桶（日/ISO周/月）收缩对齐，
     任意重叠窗口共享同一批原子桶，错位查询得不到新切面；
   - 原子单元按自然日冻结落库，迟到更正不改变已发布结果；
   - 输出只取决于（用途、窗口、粒度、维度、策略版本、冻结快照），与请求人
     身份无关，不同权限用户请求同一报表得到完全一致的结果。
4. 策略版本与数据指纹：每次构建记录策略版本，并对发布内容计算指纹，
   审核人可据此核对数据范围与处理规则。
"""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.warehouse.models import Goods, StockIn, StockOut
from .models import FrozenBucket, MaskedExportRecord, ReleasedCell

# ---------------------------------------------------------------------------
# 策略定义（修改任何一项都必须提升 version，历史发布按旧版本仍可复核）
# ---------------------------------------------------------------------------

MASKING_POLICY = {
    'version': 'masked-trend-v1.0',
    # 小于该记录数的统计单元不得直接发布
    'k_threshold': 3,
    # 自然日数据延迟冻结天数：D 日的数据在 D+embargo_days 之后才可发布
    'embargo_days': 1,
    # 单次请求允许的最大窗口（天）
    'max_window_days': 366,
    'measure_rule': '入库按入库时间统计；出库仅计入已完成记录并按完成时间统计',
    'freeze_rule': '每个自然日的数据延迟 1 天冻结后发布；已冻结区间不反映迟到更正',
    'purposes': {
        'external_inspection': {
            'label': '外部检查',
            'description': '面向外部检查人员的收发趋势：品类粒度、周/月汇总，不含明细与身份字段',
            'allowed_roles': ['user', 'admin', 'superadmin'],
            'dimensions': ['category'],
            'default_dimension': 'category',
            'granularities': ['week', 'month'],
            'default_granularity': 'week',
            'strategy': 'merge',
            'extra_fields': [],
        },
        'internal_review': {
            'label': '内部复盘',
            'description': '面向管理人员的收发趋势：可到品种粒度与日粒度，仍不含身份与明细字段',
            'allowed_roles': ['admin', 'superadmin'],
            'dimensions': ['category', 'variety'],
            'default_dimension': 'variety',
            'granularities': ['day', 'week', 'month'],
            'default_granularity': 'day',
            'strategy': 'merge',
            'extra_fields': ['goods_type_count'],
        },
    },
}

GRANULARITY_LABELS = {'day': '日', 'week': '周', 'month': '月'}
DIMENSION_LABELS = {'category': '品类', 'variety': '品种'}
STRATEGY_LABELS = {'merge': '小组合并', 'suppress': '小组抑制'}
ROLE_LABELS = {'superadmin': '超级管理员', 'admin': '管理员', 'user': '普通用户'}
ROW_KIND_LABELS = {'normal': '正常', 'merged': '已合并', 'suppressed': '已抑制'}

# 明细级字段：任何用途都不发布，在摘要中明示已被裁剪
DETAIL_WITHHELD_FIELDS = [
    '货物名称', '货物编码', '规格型号', '存放位置', '批次号', '供应商',
    '领用人', '领用部门', '操作人', '审批人', '备注', '案件关联信息',
]

MERGED_LABEL = '其他（已合并）'
SUPPRESSED_LABEL = '已抑制（小组）'

_QUANT = Decimal('0.01')


class MaskedReportError(Exception):
    """报表参数或数据不可用，携带 HTTP 状态码。"""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# 窗口对齐
# ---------------------------------------------------------------------------

def _first_of_next_month(d):
    return (d.replace(day=28) + timedelta(days=7)).replace(day=1)


def _last_of_month(d):
    return _first_of_next_month(d) - timedelta(days=1)


def align_window(start, end, granularity):
    """将窗口向完整时间桶收缩（只缩不扩，绝不覆盖请求范围之外的数据）。"""
    if granularity == 'day':
        return start, end
    if granularity == 'week':
        aligned_start = start + timedelta(days=(7 - start.weekday()) % 7)
        aligned_end = end - timedelta(days=(end.weekday() + 1) % 7)
        return aligned_start, aligned_end
    # month
    aligned_start = start if start.day == 1 else _first_of_next_month(start)
    aligned_end = end if _last_of_month(end) == end else end.replace(day=1) - timedelta(days=1)
    return aligned_start, aligned_end


def _bucket_label(d, granularity):
    if granularity == 'day':
        return d.isoformat()
    if granularity == 'week':
        iso = d.isocalendar()
        return f'{iso[0]}-W{iso[1]:02d}'
    return f'{d.year}-{d.month:02d}'


def _iter_bucket_labels(start, end, granularity):
    labels = []
    seen = set()
    d = start
    while d <= end:
        label = _bucket_label(d, granularity)
        if label not in seen:
            seen.add(label)
            labels.append(label)
        d += timedelta(days=1)
    return labels


# ---------------------------------------------------------------------------
# 原子单元冻结
# ---------------------------------------------------------------------------

def _freeze_day(day, policy_version):
    """把指定自然日的源数据冻结为原子单元快照（幂等）。"""
    in_aggs = (
        StockIn.objects.filter(stock_in_time__date=day)
        .values('goods_id')
        .annotate(c=Count('id'), q=Sum('quantity'))
    )
    out_aggs = (
        StockOut.objects.filter(status='completed', stock_out_time__date=day)
        .values('goods_id')
        .annotate(c=Count('id'), q=Sum('quantity'))
    )

    combined = {}
    total_records = 0
    for agg in in_aggs:
        entry = combined.setdefault(agg['goods_id'], [0, 0, Decimal('0'), Decimal('0')])
        entry[0] = agg['c']
        entry[2] = agg['q'] or Decimal('0')
        total_records += agg['c']
    for agg in out_aggs:
        entry = combined.setdefault(agg['goods_id'], [0, 0, Decimal('0'), Decimal('0')])
        entry[1] = agg['c']
        entry[3] = agg['q'] or Decimal('0')
        total_records += agg['c']

    with transaction.atomic():
        bucket, created = FrozenBucket.objects.get_or_create(
            policy_version=policy_version,
            bucket_date=day,
            defaults={'record_count': total_records},
        )
        if not created:
            return  # 已冻结：迟到更正不再进入已发布快照

        goods_map = {
            g.id: g
            for g in Goods.objects.filter(id__in=combined.keys()).select_related('variety__category')
        }
        cells = []
        for goods_id, (in_c, out_c, in_q, out_q) in combined.items():
            goods = goods_map.get(goods_id)
            variety = goods.variety if goods else None
            category = variety.category if variety else None
            cells.append(ReleasedCell(
                policy_version=policy_version,
                bucket_date=day,
                goods_id=goods_id,
                category_name=category.name if category else '',
                variety_name=variety.name if variety else '',
                in_count=in_c,
                out_count=out_c,
                in_quantity=in_q,
                out_quantity=out_q,
            ))
        ReleasedCell.objects.bulk_create(cells)


def freeze_window(start, end, policy_version):
    """确保窗口内每个自然日都已冻结。"""
    existing = set(
        FrozenBucket.objects.filter(
            policy_version=policy_version,
            bucket_date__gte=start,
            bucket_date__lte=end,
        ).values_list('bucket_date', flat=True)
    )
    day = start
    while day <= end:
        if day not in existing:
            _freeze_day(day, policy_version)
        day += timedelta(days=1)


# ---------------------------------------------------------------------------
# 聚合与小样本处理
# ---------------------------------------------------------------------------

def _new_acc():
    return {
        'in_count': 0,
        'out_count': 0,
        'in_qty': Decimal('0'),
        'out_qty': Decimal('0'),
        'goods': set(),
        'variety': '',
        'category': '',
    }


def _record_count(acc):
    return acc['in_count'] + acc['out_count']


def _combine_accs(accs):
    combined = _new_acc()
    for acc in accs:
        combined['in_count'] += acc['in_count']
        combined['out_count'] += acc['out_count']
        combined['in_qty'] += acc['in_qty']
        combined['out_qty'] += acc['out_qty']
        combined['goods'] |= acc['goods']
    return combined


def _apply_strategy(variety_accs, k, strategy):
    """对一个时间桶内的品种单元执行小样本处理。

    返回 (正常发布单元, 合并发布单元或None, 被抑制单元列表, 被抑制品种单元数)。
    被抑制单元的记录不进入任何已发布数字。
    """
    big = [a for a in variety_accs if _record_count(a) >= k]
    small = [a for a in variety_accs if _record_count(a) < k]

    merged = None
    suppressed = []
    suppressed_cell_count = 0
    if strategy == 'merge':
        if small:
            combined = _combine_accs(small)
            if _record_count(combined) >= k:
                merged = combined
            else:
                suppressed = [combined]
                suppressed_cell_count = len(small)
    else:  # suppress
        suppressed = list(small)
        suppressed_cell_count = len(small)
    return big, merged, suppressed, suppressed_cell_count


def _acc_to_row(bucket, dimension_value, kind, acc, with_goods_count):
    row = {
        'bucket': bucket,
        'dimension_value': dimension_value,
        'row_kind': kind,
        'in_count': acc['in_count'] if acc else None,
        'in_quantity': acc['in_qty'] if acc else None,
        'out_count': acc['out_count'] if acc else None,
        'out_quantity': acc['out_qty'] if acc else None,
        'record_count': _record_count(acc) if acc else None,
    }
    if with_goods_count:
        row['goods_type_count'] = len(acc['goods']) if acc else None
    return row


def _build_rows(cells, bucket_labels, granularity, dimension, k, strategy, with_goods_count):
    """由冻结原子单元构建发布行，返回 (rows, stats, per_bucket_totals)。"""
    # 先聚合到最细发布单元：时间桶 × 品种
    cell_map = {}
    for cell in cells:
        label = _bucket_label(cell.bucket_date, granularity)
        key = (label, cell.variety_name)
        acc = cell_map.get(key)
        if acc is None:
            acc = _new_acc()
            acc['variety'] = cell.variety_name
            acc['category'] = cell.category_name
            cell_map[key] = acc
        acc['in_count'] += cell.in_count
        acc['out_count'] += cell.out_count
        acc['in_qty'] += cell.in_quantity
        acc['out_qty'] += cell.out_quantity
        acc['goods'].add(cell.goods_id)

    rows = []
    per_bucket_totals = []
    stats = {
        'released_row_count': 0,
        'merged_row_count': 0,
        'suppressed_cell_count': 0,
        'suppressed_record_count': 0,
    }

    for label in bucket_labels:
        variety_accs = [acc for (bucket, _), acc in cell_map.items() if bucket == label]
        big, merged, suppressed, suppressed_cells = _apply_strategy(variety_accs, k, strategy)

        stats['suppressed_cell_count'] += suppressed_cells
        bucket_rows = []
        released_accs = []

        if dimension == 'variety':
            for acc in sorted(big, key=lambda a: a['variety']):
                bucket_rows.append(_acc_to_row(label, acc['variety'], 'normal', acc, with_goods_count))
                released_accs.append(acc)
        else:  # category：仅由已发布的品种单元汇总，保证跨维度结果一致
            by_category = {}
            for acc in big:
                by_category.setdefault(acc['category'], []).append(acc)
            for category in sorted(by_category):
                combined = _combine_accs(by_category[category])
                bucket_rows.append(_acc_to_row(label, category, 'normal', combined, with_goods_count))
                released_accs.append(combined)

        if merged is not None:
            bucket_rows.append(_acc_to_row(label, MERGED_LABEL, 'merged', merged, with_goods_count))
            released_accs.append(merged)
            stats['merged_row_count'] += 1

        if suppressed:
            stats['suppressed_record_count'] += sum(_record_count(s) for s in suppressed)
            if strategy == 'suppress' and dimension == 'variety':
                # 品种维度（仅内部用途）：按品种给出抑制标记，不发布数值
                for s in sorted(suppressed, key=lambda a: a['variety']):
                    bucket_rows.append(_acc_to_row(label, s['variety'], 'suppressed', None, with_goods_count))
            else:
                # 合并策略的残余小组、或品类维度下：只给一个不具名标记，
                # 不暴露被抑制小组的维度取值与任何数值
                bucket_rows.append(_acc_to_row(label, SUPPRESSED_LABEL, 'suppressed', None, with_goods_count))

        stats['released_row_count'] += sum(1 for r in bucket_rows if r['row_kind'] == 'normal')

        total = _combine_accs(released_accs)
        per_bucket_totals.append({
            'bucket': label,
            'in_count': total['in_count'],
            'in_quantity': total['in_qty'],
            'out_count': total['out_count'],
            'out_quantity': total['out_qty'],
            'record_count': _record_count(total),
        })
        rows.extend(bucket_rows)

    return rows, stats, per_bucket_totals


# ---------------------------------------------------------------------------
# 摘要与指纹
# ---------------------------------------------------------------------------

def _released_field_labels(purpose, dimension):
    labels = ['时间桶', DIMENSION_LABELS[dimension], '行类型', '入库次数', '入库数量', '出库次数', '出库数量', '记录数']
    if 'goods_type_count' in purpose['extra_fields']:
        labels.append('涉及货物种类数')
    return labels


def _quantize(value):
    if value is None:
        return None
    return str(Decimal(value).quantize(_QUANT))


def _fingerprint(policy_version, purpose_key, granularity, dimension, start, end, rows, grand_total):
    canonical = {
        'policy_version': policy_version,
        'purpose': purpose_key,
        'granularity': granularity,
        'dimension': dimension,
        'window': [start.isoformat(), end.isoformat()],
        'rows': [
            [
                r['bucket'], r['dimension_value'], r['row_kind'],
                r['in_count'], _quantize(r['in_quantity']),
                r['out_count'], _quantize(r['out_quantity']),
                r['record_count'],
            ]
            for r in rows
        ],
        'grand_total': [
            grand_total['in_count'], _quantize(grand_total['in_quantity']),
            grand_total['out_count'], _quantize(grand_total['out_quantity']),
            grand_total['record_count'],
        ],
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _decimal_to_float(value):
    return float(value) if value is not None else None


def _publicize_rows(rows):
    """把内部 Decimal 行转换为可 JSON 序列化的发布行。"""
    public = []
    for row in rows:
        item = dict(row)
        item['in_quantity'] = _decimal_to_float(row['in_quantity'])
        item['out_quantity'] = _decimal_to_float(row['out_quantity'])
        public.append(item)
    return public


# ---------------------------------------------------------------------------
# 报表构建入口
# ---------------------------------------------------------------------------

def available_purposes(role):
    """按角色列出可用用途及其处理规则（供前端按用途选择）。"""
    result = []
    for key, purpose in MASKING_POLICY['purposes'].items():
        if role not in purpose['allowed_roles']:
            continue
        result.append({
            'purpose': key,
            'label': purpose['label'],
            'description': purpose['description'],
            'dimensions': [
                {'value': d, 'label': DIMENSION_LABELS[d]} for d in purpose['dimensions']
            ],
            'default_dimension': purpose['default_dimension'],
            'granularities': [
                {'value': g, 'label': GRANULARITY_LABELS[g]} for g in purpose['granularities']
            ],
            'default_granularity': purpose['default_granularity'],
            'k_threshold': MASKING_POLICY['k_threshold'],
            'strategy': purpose['strategy'],
            'strategy_label': STRATEGY_LABELS[purpose['strategy']],
            'policy_version': MASKING_POLICY['version'],
            'fields_released': _released_field_labels(purpose, purpose['default_dimension']),
            'fields_withheld': DETAIL_WITHHELD_FIELDS,
        })
    return result


def resolve_params(user, purpose_key, start, end, granularity, dimension):
    """校验并解析请求参数，返回 (purpose, granularity, dimension)。"""
    purpose = MASKING_POLICY['purposes'].get(purpose_key)
    if purpose is None:
        raise MaskedReportError('无效的报表用途')
    if user.role not in purpose['allowed_roles']:
        raise MaskedReportError('当前角色无权使用该报表用途', code=403)

    if granularity is None:
        granularity = purpose['default_granularity']
    if granularity not in purpose['granularities']:
        allowed = '、'.join(GRANULARITY_LABELS[g] for g in purpose['granularities'])
        raise MaskedReportError(f'该用途不支持此时间粒度，可选：{allowed}')

    if dimension is None:
        dimension = purpose['default_dimension']
    if dimension not in purpose['dimensions']:
        allowed = '、'.join(DIMENSION_LABELS[d] for d in purpose['dimensions'])
        raise MaskedReportError(f'该用途不支持此统计维度，可选：{allowed}')

    if start > end:
        raise MaskedReportError('开始日期不能晚于结束日期')
    if (end - start).days > MASKING_POLICY['max_window_days']:
        raise MaskedReportError(f'时间窗口不能超过 {MASKING_POLICY["max_window_days"]} 天')

    return purpose, granularity, dimension


def build_masked_report(user, purpose_key, start, end, granularity=None, dimension=None):
    """构建脱敏收发趋势报表。

    返回 {'summary', 'rows', 'totals', 'audit'}：
    summary 为随报表发布的摘要（供审核人确认数据范围与处理规则），
    audit 为内部留档字段（含被抑制记录的精确数，仅审核侧可见）。
    """
    policy = MASKING_POLICY
    purpose, granularity, dimension = resolve_params(
        user, purpose_key, start, end, granularity, dimension
    )
    k = policy['k_threshold']
    strategy = purpose['strategy']
    policy_version = policy['version']

    # 1) 冻结地平线截断：只发布已过了冻结延迟期的数据
    horizon = timezone.localdate() - timedelta(days=policy['embargo_days'])
    window_notes = []
    clamped_end = min(end, horizon)
    if clamped_end < end:
        window_notes.append(
            f'冻结规则：仅发布截至 {horizon.isoformat()} 的数据（延迟 {policy["embargo_days"]} 天冻结）'
        )
    if start > clamped_end:
        raise MaskedReportError('所选区间暂无达到冻结条件的数据，请调整时间窗口')

    # 2) 向完整时间桶收缩对齐：重叠窗口共享同一批原子桶
    aligned_start, aligned_end = align_window(start, clamped_end, granularity)
    if aligned_start > aligned_end:
        raise MaskedReportError(f'按{GRANULARITY_LABELS[granularity]}对齐后没有完整时间桶，请调整时间窗口')
    if (aligned_start, aligned_end) != (start, end):
        window_notes.append(
            f'按{GRANULARITY_LABELS[granularity]}对齐，仅覆盖完整时间桶：'
            f'{start.isoformat()}~{end.isoformat()} → {aligned_start.isoformat()}~{aligned_end.isoformat()}'
        )
    if not window_notes:
        window_notes.append('窗口未调整')

    # 3) 冻结窗口内原子单元（幂等），再读取快照聚合
    freeze_window(aligned_start, aligned_end, policy_version)
    cells = ReleasedCell.objects.filter(
        policy_version=policy_version,
        bucket_date__gte=aligned_start,
        bucket_date__lte=aligned_end,
    )

    # 4) 小样本处理并生成发布行
    bucket_labels = _iter_bucket_labels(aligned_start, aligned_end, granularity)
    with_goods_count = 'goods_type_count' in purpose['extra_fields']
    rows, stats, per_bucket_totals = _build_rows(
        cells, bucket_labels, granularity, dimension, k, strategy, with_goods_count
    )

    grand = _new_acc()
    for total in per_bucket_totals:
        grand['in_count'] += total['in_count']
        grand['out_count'] += total['out_count']
        grand['in_qty'] += total['in_quantity']
        grand['out_qty'] += total['out_quantity']
    grand_total = {
        'in_count': grand['in_count'],
        'in_quantity': grand['in_qty'],
        'out_count': grand['out_count'],
        'out_quantity': grand['out_qty'],
        'record_count': _record_count(grand),
    }

    fingerprint = _fingerprint(
        policy_version, purpose_key, granularity, dimension,
        aligned_start, aligned_end, rows, grand_total,
    )

    fields_released = _released_field_labels(purpose, dimension)
    summary = {
        'report_title': '收发趋势脱敏报表',
        'purpose': purpose_key,
        'purpose_label': purpose['label'],
        'purpose_description': purpose['description'],
        'policy_version': policy_version,
        'generated_by': user.username,
        'generated_by_role': ROLE_LABELS.get(user.role, user.role),
        'generated_at': timezone.localtime().isoformat(),
        'granularity': granularity,
        'granularity_label': GRANULARITY_LABELS[granularity],
        'dimension': dimension,
        'dimension_label': DIMENSION_LABELS[dimension],
        'requested_window': {'start': start.isoformat(), 'end': end.isoformat()},
        'effective_window': {'start': aligned_start.isoformat(), 'end': aligned_end.isoformat()},
        'window_notes': window_notes,
        'releasable_horizon': horizon.isoformat(),
        'freeze_rule': policy['freeze_rule'],
        'measure_rule': policy['measure_rule'],
        'k_threshold': k,
        'small_cell_strategy': strategy,
        'small_cell_strategy_label': STRATEGY_LABELS[strategy],
        'fields_released': fields_released,
        'fields_withheld': DETAIL_WITHHELD_FIELDS,
        'bucket_count': len(bucket_labels),
        'row_count': len(rows),
        'released_row_count': stats['released_row_count'],
        'merged_row_count': stats['merged_row_count'],
        'suppressed_cell_count': stats['suppressed_cell_count'],
        'suppressed_note': f'被抑制单元的记录数均小于阈值 k={k}，其数值不发布，也不计入任何合计',
        'totals_rule': '所有合计仅统计已发布单元，不含被抑制部分',
        'consistency_note': '报表由冻结快照生成，同一用途、同一窗口、同一策略版本无论何时请求结果一致',
        'data_fingerprint': fingerprint,
    }

    audit = {
        'k_threshold': k,
        'strategy': strategy,
        'fields_released': fields_released,
        'fields_withheld': DETAIL_WITHHELD_FIELDS,
        'bucket_count': len(bucket_labels),
        'row_count': len(rows),
        'released_row_count': stats['released_row_count'],
        'merged_row_count': stats['merged_row_count'],
        'suppressed_cell_count': stats['suppressed_cell_count'],
        'suppressed_record_count': stats['suppressed_record_count'],
        'data_fingerprint': fingerprint,
    }

    totals = {
        'per_bucket': [
            {
                'bucket': t['bucket'],
                'in_count': t['in_count'],
                'in_quantity': _decimal_to_float(t['in_quantity']),
                'out_count': t['out_count'],
                'out_quantity': _decimal_to_float(t['out_quantity']),
                'record_count': t['record_count'],
            }
            for t in per_bucket_totals
        ],
        'grand': {
            'in_count': grand_total['in_count'],
            'in_quantity': _decimal_to_float(grand_total['in_quantity']),
            'out_count': grand_total['out_count'],
            'out_quantity': _decimal_to_float(grand_total['out_quantity']),
            'record_count': grand_total['record_count'],
        },
    }

    return {
        'summary': summary,
        'rows': _publicize_rows(rows),
        'totals': totals,
        'audit': audit,
        'effective_window': (aligned_start, aligned_end),
    }


def record_export_audit(user, action, purpose_key, report, requested_start, requested_end):
    """把一次预览/导出写入审计记录，供审核人核对。"""
    summary = report['summary']
    audit = report['audit']
    effective_start, effective_end = report['effective_window']
    purpose = MASKING_POLICY['purposes'][purpose_key]
    return MaskedExportRecord.objects.create(
        action=action,
        requested_by=user,
        requester_role=user.role,
        purpose=purpose_key,
        purpose_label=purpose['label'],
        policy_version=summary['policy_version'],
        granularity=summary['granularity'],
        dimension=summary['dimension'],
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=effective_start,
        effective_end=effective_end,
        k_threshold=audit['k_threshold'],
        strategy=audit['strategy'],
        fields_released=audit['fields_released'],
        fields_withheld=audit['fields_withheld'],
        bucket_count=audit['bucket_count'],
        row_count=audit['row_count'],
        released_row_count=audit['released_row_count'],
        merged_row_count=audit['merged_row_count'],
        suppressed_cell_count=audit['suppressed_cell_count'],
        suppressed_record_count=audit['suppressed_record_count'],
        data_fingerprint=audit['data_fingerprint'],
        summary=summary,
    )
