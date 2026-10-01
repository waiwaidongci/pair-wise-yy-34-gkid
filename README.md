# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/measures`：事故挂接的共用措施及核验状态
- `POST /api/items/{id}/measures`：挂接共用措施，body含`code`和`expected_version`
- `GET /api/measures`（可按`?conclusion=`过滤）/ `GET /api/measures/{code}`
- `POST /api/measures`：按措施编号建立共用措施（code/title/workstation/reason）
- `POST /api/measures/{code}/accept`：验收结论`conclusion=pending|passed|failed`，必须提交`expected_version`
- `POST /api/measure-links/{id}/verify`：事故对挂接措施完成核验
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 跨事故共用整改措施台账

- 措施按编号全局唯一，一条措施可挂接到多起事故（`measure_links`多对多）。
- 挂接和验收都采用乐观锁：必须提交所依据的`expected_version`，两人同时操作时先到者生效，后到者收到`409`和响应体中的`current_version`，刷新后重试。
- 验收结论一变化，措施版本递增：已核验的挂接立即置为`recheck`并记录失效原因；引用该措施的事故若在核验或已关闭状态，退回`verification`（已关闭事故版本+1），并在事故下生成一条开放的`recheck_reason`记录写明原因，重新核验通过后自动关闭。
- 事故关闭时校验全部挂接措施：未验收通过、结论版本过期或未核验的，阻止关闭并列出原因。
- 历史升级：服务启动时把旧`records`中整改类明细（kind为action/corrective_action/measure/整改措施/纠正措施，detail支持`{workstation,reason}`JSON）按**工位+原因**归并成`M-n`共用措施并回挂各事故，原明细保留在`measure_sources`可溯源，审计记录`migrate`事件；归并按批次只执行一次，重启不重复。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
