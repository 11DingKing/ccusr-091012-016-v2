# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 脱敏报表

面向外部检查等场景提供按用途选择的脱敏收发趋势报表，避免明细导出暴露案件名称、人员身份与小组记录：

- `GET /api/masked-reports/purposes/` — 按角色列出可选用途、发布/裁剪字段、阈值与策略版本
- `GET /api/masked-reports/trend/` — 脱敏趋势预览（JSON，留痕）
- `GET /api/masked-reports/export/` — 脱敏趋势导出（xlsx，附“导出摘要”页，留痕）
- `GET /api/masked-reports/records/` — 预览/导出审计记录（仅管理角色）

处理规则（策略版本 `masked-trend-v1.0`，见 `backend/apps/reports/masking.py`）：

1. **按授权字段裁剪**：用途决定时间粒度、统计维度与列集；货物名称、领用人、操作人等明细与身份字段一律不发布。
2. **小样本抑制/合并**：记录数小于阈值 k=3 的“品种×时间桶”单元按策略合并为“其他”或抑制；被抑制记录不进入任何已发布数字（含合计）。
3. **防对比反推**：窗口先按冻结地平线截断再向完整时间桶（日/ISO周/月）收缩对齐；原子单元按自然日冻结落库，迟到更正不改变已发布结果；输出与请求人身份无关，同一报表不同权限用户结果一致。
4. **可审计**：每次预览/导出记录策略版本、数据范围、处理规则与数据指纹（SHA-256），摘要随报表发布供审核人确认。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
