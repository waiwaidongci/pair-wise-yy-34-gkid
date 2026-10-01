# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

整改措施采用跨事故共用台账：按措施编号建一条措施，再挂到多起事故；措施的验收结论一变化即失效重算，引用它的事故退回核验并写明原因。挂接与验收均走乐观锁，先到者生效，后到者看到当前版本。旧数据在升级时按工位和原因归并成共用条目，原明细与审计可查。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量与措施验收规则。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链、措施台账与旧数据归并。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应（冲突返回`current_version`）。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、措施台账、并发与失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库并补齐旧库缺失的列。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

事故：

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`（支持`workstation`、`reason`）
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/measures`
- `GET /api/audit`（可按`item_id`、`entity_type`过滤）

共用措施台账：

- `GET /api/measures`（可按`status`过滤）
- `POST /api/measures`（提交`measure_no`、`title`、`workstation`、`reason`）
- `GET /api/measures/{id}`（含`linked_items`挂接事故与`original_records`归并前明细）
- `POST /api/measures/{id}/link`，提交`item_id`与`expected_version`
- `POST /api/measures/{id}/acceptance`，提交`conclusion`、`reason`与`expected_version`
- `POST /api/admin/migrate-measures`：把旧的按事故措施记录按工位和原因归并成共用条目（幂等）

允许角色：reporter, investigator, safety_manager, viewer。措施的建立/挂接由investigator、safety_manager执行，验收与归并由safety_manager执行。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 并发与失效规则

- 挂接、验收均携带`expected_version`（措施当前版本）。版本不符返回`409`并带`current_version`，刷新后重试即可。
- 验收结论首次建立后若再次变化，措施置为`invalid`、验收结论重置为`pending`（失效重算），所有引用该措施的事故退回`verification`并写入`measure_revoked`记录（含原因）与审计事件。
- 失效后重新验收通过，措施恢复`active`。结论未变的重复提交幂等，不升版本。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
