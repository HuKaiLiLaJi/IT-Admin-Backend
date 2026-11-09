# 开发与验证状态

2026-11-06 已完成第一步本地环境准备及 T01 至 T06，实施授权见 [决策记录](decisions.md) 的 N18～N23、N27～N28。首版业务基线见 [需求说明](requirements.md)，动态授权和接续点见 [快速恢复](quick-resume.md)。最新界面与联验证据见 T06，评价契约及 schema v5 见 T05。下文各阶段验证结果为各自交付时记录。

## 本地环境

| 组件 | 实际版本或路径 |
| --- | --- |
| Python | 3.14.7，Windows AMD64 |
| 基础解释器 | C:/Users/solax/AppData/Local/Python/pythoncore-3.14-64/python.exe |
| 虚拟环境解释器 | D:/IT-Admin-Backend/.venv/Scripts/python.exe |
| pip | 26.2.1，由 venv 自带 ensurepip 初始化 |
| FastAPI | 0.162.0 |
| Uvicorn | 0.54.0 |
| SQLite | 3.50.4，由 Python 标准库 sqlite3 提供 |

采用 FastAPI、Uvicorn 基础包及其必需依赖，完整固定版本见根目录 requirements.txt。SQLite 无需单独 pip 安装。本次沿用本机解释器，正式部署平台及版本策略后续确定。

## 安装与使用

当前 .venv 已可直接使用。在项目根目录通过 PowerShell 执行以下命令可检查环境，无需激活虚拟环境或修改执行策略。

```powershell
Set-Location D:\IT-Admin-Backend
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip check
```

在同平台、同 Python 版本的新工作副本中初始化环境时使用以下命令。已有 .venv 无需重复创建。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements.txt
```

本次首次安装使用 `pip install --index-url https://pypi.org/simple fastapi uvicorn`，安装成功后按 `pip freeze` 输出保存版本清单。尚未在第二个全新环境重装验证 requirements.txt。

## 第一步环境准备的验证记录

- `pip check` 返回 `No broken requirements found.`。
- FastAPI、Uvicorn 导入成功，sys.prefix 指向本项目 .venv，且不同于基础解释器目录。
- SQLite 内存连接执行 `SELECT 1` 成功，连接已关闭。
- 通过临时内存 FastAPI 应用和 Uvicorn 在 127.0.0.1 的系统分配端口启动服务，`/health` 与 `/openapi.json` 均返回 HTTP 200，并校对响应内容。
- 临时服务检查后正常关闭，未保留应用源码或常驻服务。

安装期间 pip 提示检查自身最新版本失败，依赖下载及安装仍成功，后续 pip check 与运行检查通过。未因此修改 pip 配置或升级全局工具。

以上仅验证环境可运行。业务流程、权限、数据库持久化、并发事务及部署尚未验证。本次未连接企微或访问旧目录数据及凭证。

后续实施遵守 [项目规则](../AGENTS.md) 及当前全局规则，按实际风险进行最小充分验证。正常安装与工具自带校验按 N17 执行。

## T01 启动与测试账号

在项目根目录执行以下命令。测试数据创建与服务启动分开，正常启动只初始化表结构。

```powershell
.\.venv\Scripts\python.exe -m app.seed
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

默认数据库为 `D:/IT-Admin-Backend/data/backend.sqlite3`，测试数据已创建。可通过 `IT_ADMIN_DATABASE` 环境变量指定其他数据库文件，相对路径按项目根目录解析。运行数据目录已加入 .gitignore。正常退出使用 Ctrl+C。

所有新建测试账号的密码为 `LocalTest-2026!`。已有账号再次初始化时保留密码、显示名称、启停、角色和范围设置。主管暂用 manager 角色表示授权统计能力，具体撤销审批权限留待 T04 收敛。

| 用户名 | 角色 | 处理组范围 |
| --- | --- | --- |
| submitter | submitter | 本人提交范围 |
| submitter_other | submitter | 本人提交范围 |
| dispatcher | submitter、dispatcher | dispatcher 的 test-it |
| handler | submitter、handler | handler 的 test-it 成员范围 |
| handler_backup | submitter、handler | handler 的 test-it 成员范围，T03 新增 |
| handler_other | submitter、handler | handler 的 test-admin 成员范围 |
| supervisor | submitter、manager | manager 的 test-it |
| config_admin | submitter、config_admin | config_admin 的 test-it |

以上为本地测试配置。测试运维系统为 test-office 和 test-facility，分别关联 test-it 和 test-admin。该配置用于后续分派测试，具体受理和分派行为由后续任务实施。

## T01 接口与身份契约

| 方法与路径 | 用途及权限 |
| --- | --- |
| GET /health | 无需登录，检查数据库可连接并执行查询 |
| POST /api/v1/auth/login | JSON username、password，返回 access_token、token_type 和 expires_in |
| POST /api/v1/auth/logout | 当前登录会话退出，成功返回 204 |
| GET /api/v1/me | 当前身份、角色及按角色分组的 group_scopes |
| GET /api/v1/service-systems | 登录后读取有效测试运维系统 |
| GET /api/v1/support-groups | dispatcher 角色在授权范围内的有效处理组 |
| GET /api/v1/support-groups/{group_id} | dispatcher 角色访问单个授权处理组 |

接口文档位于 `http://127.0.0.1:8000/docs`。先调用 login，再在 Authorize 中输入返回的 access_token。后续调用使用 `Authorization: Bearer <access_token>`。密码只通过请求体提交，避免写入 URL。身份、角色及范围从数据库读取，客户端提供的用户或角色字段不能代替认证。

会话采用随机令牌，数据库保存令牌派生值，固定有效期为八小时。密码使用独立随机盐及 PBKDF2-HMAC-SHA256、600000 次迭代派生保存，参数见 app/auth.py。这些机制用于业务登录凭证与会话认证。每次受保护请求重新查询账号启停、角色和有效处理组范围，退出只撤销当前会话，过期会话在后续登录时清理。

错误响应的 detail 内含 code 和 message。未认证、无效凭证或过期返回 401，角色或处理组范围不足返回 403，字段无效返回 422，数据库操作暂不可用返回 503。422 响应过滤输入值，避免回显密码，响应设置 Cache-Control no-store。

本阶段采用已知测试密码与本机 HTTP，供本机人工流程开发使用。正式认证、限流、网络访问与部署验证后续安排。

## T01 数据与后续复用

- app/database.py 提供 `connect(path)` 和 `initialize_database(path)`。连接启用外键和五秒锁等待，在 finally 中关闭。请求中的写入必须使用 `with connection` 在返回成功前提交，失败回滚。
- 表包括 users、roles、user_roles、support_groups、user_group_scopes、service_systems、sessions。用户角色支持叠加，范围通过 `(user_id, role, group_id)` 保存，外键要求范围对应有效的用户角色。
- T01 交付时 schema 为 `PRAGMA user_version = 1`，T02 已迁移至 v2，见下文。初始化在单个写事务内执行，已有版本重复启动保留数据，高于应用支持的版本拒绝启动。后续增加表或字段时添加明确的向前迁移并提高版本，保留已有数据，避免直接重建数据库。
- app/auth.py 中的 `Database` 依赖提供请求连接，`CurrentUser` 返回可信 Principal，`require_role(role)` 用于角色检查，`require_group_scope(user, role, group_id)` 用于角色对应的处理组检查。
- T02 需另外实现工单级权限及列表过滤。handler 的组成员范围用于处理人配置，工单查看须按当前分配校验。manager 与 config_admin 的范围分别服务统计和配置，不能据此赋予全量工单访问权。
- 当前 `create_app(path)` 支持独立测试数据库，模块入口为 app.main:app。测试初始化遇到新增数据冲突时整体回滚，已有账号按用户名跳过，不覆盖人工修改。

## T01 验证证据

2026-11-06 执行以下命令，12 项针对性测试全部通过。测试使用临时 SQLite 文件与真实回环 HTTP 服务，无额外测试依赖。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

覆盖登录、退出、会话隔离和过期、伪造身份、登录注入与额外角色字段、凭证回显过滤、角色与处理组范围、账号停用与权限即时收回、初始化重复执行、种子事务回滚与设置保留、外键与事务回滚、服务重启后的会话及配置保留、数据库不可用和新版本保护。

随后调整数据库连接初始化异常时的关闭范围，单独重跑 database_unavailable 用例通过，并通过真实 CLI 完成以下检查。

- 使用 `uvicorn app.main:app` 启动时，空库账号数为 0。
- 执行 `python -m app.seed` 首次创建 7 个账号，再次创建 0 个。
- 重启 CLI 服务后，健康检查、测试登录、读取身份和退出成功。
- 验证服务与临时测试服务均已关闭。

T01 验证覆盖应用和身份基础。工单级权限、会话建单、工单状态、评价、实际界面及正式部署由后续任务实现和验证。

## T02 会话、建单与权限查询

2026-11-06 按用户“开始t02”实施，授权见 N20。沿用 T01 的启动方式、测试账号和认证依赖。默认库已通过实际 CLI 启动从 v1 迁移至 v2，保留原有 7 个账号，当前工单数为 0。验证临时服务已关闭。

### 接口与调用顺序

所有下列接口均需要 Bearer 登录令牌。业务会话保存在 conversations，登录会话继续保存在 sessions，两者编号独立。

| 方法与路径 | 请求及响应 |
| --- | --- |
| POST /api/v1/conversations | JSON `{}` 创建本人业务会话，或传 `previous_ticket_id` 关联本人已关闭／已撤销旧单。201 返回后端生成的 id、submitter_id、previous_ticket_id、created_at 和空 ticket_id |
| GET /api/v1/conversations/{id} | 仅会话所有者可读取，含已关联 ticket_id，便于恢复后续操作 |
| POST /api/v1/conversations/{id}/ticket | JSON 必填 service_system_id、title、description。首次成功为 201，重复提交为 200 并返回原单 |
| GET /api/v1/tickets | 返回 items、total、limit、offset。limit 默认 20，上限 100，offset 默认 0。按创建时间及 id 倒序稳定分页，列表及总数使用相同权限过滤和数据库读快照 |
| GET /api/v1/tickets/{id} | 返回可访问工单及 history，历史按递增 id 排序 |

最小建单请求示例。

```json
{
  "service_system_id": "test-office",
  "title": "打印机故障",
  "description": "无法打印，请协助"
}
```

工程选择为 UUID 字符串编号。标题限制 1～200 字符，描述限制 1～10000 字符，两者去掉首尾空白后仍须非空。编号输入上限 128 字符。创建请求拒绝额外字段，提交人从当前身份取得，部门／邮箱／地点取可信账号资料的建单时快照。测试分类和所属处理组均须启用。客户端不能指定提交人、角色、处理组、处理人、状态或问题编号。

重复建单以会话编号为准。请求仍须通过字段结构校验，随后返回原工单，不用新请求覆盖标题、分类等字段，也不重新要求原分类仍有效。工单已关闭或撤销时，旧会话重试仍返回旧单。再次报障调用新会话接口并传旧单编号，再对新会话建单。只允许关联本人终态旧单，非终态返回 409，外部调用方请求编号与消息去重仍后置。

### 数据、事务与后续契约

- app/tickets.py 实现 APIRouter，由 create_app 挂载。错误沿用 detail.code、detail.message 格式，未登录 401，缺少提交角色 403，无效分类或字段 422，旧单非终态 409。不存在和不可访问记录统一返回相同 404，存储暂不可用沿用 503。
- schema v2 增加 conversations、tickets、ticket_history 及查询索引。v0 建库先建立原 v1 表，再迁移至 v2。v1 向前迁移、表结构及版本号在同一事务内完成，重复启动保留数据，失败整体回滚，高于 v2 的库拒绝启动。
- conversations 保存所有者、旧单关联和创建时间。tickets.conversation_id 为 UNIQUE，并通过 `(conversation_id, submitter_id)` 复合外键保证工单归属会话所有者。旧单关联保存在 conversations.previous_ticket_id，列表和详情均返回该字段。
- tickets 保存分类、建单时处理组、标题描述、提交人资料快照、状态、当前 assignee_id、version、created_at、updated_at 和 closed_at。状态值为 pending_assignment、pending_acceptance、in_progress、awaiting_confirmation、closed、cancelled。初始 pending_assignment、version 为 1、处理人和 closed_at 为空。时间使用 UTC Unix 秒。
- issue_id 是可空扩展字段，T02 不接受调用方写入。问题模块接入时另行迁移并建立实际关联约束。
- 建单在 `BEGIN IMMEDIATE` 内检查本人会话、已有工单、旧单终态和分类，再保存工单与初始历史，提交成功后返回。写锁序列化同一库内并发请求，UNIQUE 约束兜底。失败保留原先已单独成功创建的空会话，回滚本次工单和历史，允许原会话重试。
- ticket_history 保存 action、actor_id、from_status、to_status、created_at，初始 action 为 created，from_status 为空。初始历史和工单时间相同。后续阶段扩展动作、处理记录和结果字段时使用兼容迁移。T03 起每个状态动作须同步更新 version、updated_at 与历史，并自行落实条件更新及并发冲突处理。
- `ticket_visibility(user)` 返回 SQL 条件与绑定参数，列表、总数和详情共用。submitter 查看本人提交，handler 查看当前分配，dispatcher 查看自身授权的有效处理组，叠加角色取并集。manager、config_admin 只因其另有 submitter 角色可看本人单，不因统计或配置范围扩大工单访问。handler 的组成员范围用于后续分派资格，查看权限按当前分配判断。
- 工单详情历史对所有有工单查看权限者公开。当前不提供通用状态修改、分派或关闭接口。T03 沿用上述查看规则，改派后原处理人只在还满足其他有效角色权限时可继续访问。

### T02 验证证据与边界

2026-11-06 执行以下命令，共 20 项通过，耗时约 12.5 秒。数据库迁移和共享应用入口影响基础功能，因此本次同时执行既有 12 项基础测试与新增 8 项 T02 测试。基础测试仅适配 schema 版本预期并抽取已有 HTTP 夹具，没有增加测试依赖。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

新增验证使用独立临时 SQLite 库及真实回环 HTTP，覆盖以下内容。

- 本人会话、可信提交人和资料快照、初始状态与历史、重启保留、旧分类失效后重复返回原单。
- 必填缺失、空白、超长、伪造身份及任意状态字段拒绝，分类不存在／停用、处理组停用和会话越权拒绝。失败后工单及历史为零。
- 全部新入口未登录拒绝，提交角色被收回后禁止创建会话。
- 两员工隔离，分派组权限，处理人仅看当前分配，主管和配置管理员无额外内容权限，叠加本人权限、分页总数与详情一致、授权范围即时收回、不可访问与不存在响应一致。
- 8 路同时对一个会话提交，恰好一个 201、七个 200，全部返回同一工单，库中恰好一单和一条初始历史。
- 测试触发器使历史写入失败，接口返回 503，工单和历史整体回滚，删除触发器后原会话重试成功。
- v1 原有账号、登录会话、角色、组、分类及范围保留，重复初始化可用，迁移中途表名冲突回滚新增表及版本号。
- closed 与 cancelled 旧单通过明确测试夹具设置，验证新会话新单保留旧单关联，跨人关联拒绝，旧会话仍返回原单。处理人分配及改派同样通过测试夹具设置，仅验证查询权限。

另通过真实命令行 `python -m uvicorn app.main:app --host 127.0.0.1 --port <临时端口>` 启动默认库，验证健康检查、OpenAPI、新接口挂载、测试账号登录、权限过滤列表和退出。确认默认库实际 v1 → v2、7 个账号编号／用户名／启停状态保留，未生成业务测试单，验证进程已停止。

终态与分派夹具不代表 T03／T04 动作入口已完成，T06 须补真实入口流程。正式身份、企微接入、通知、问题记录模块、界面与部署尚未验证。当前接续 T03。


## T03 分派、接单、改派与公开处理记录

2026-11-06 按用户“开启t03”实施，见 N21。用户确认 N22 改派规则。继续采用既有启动方式、认证、错误格式和工单查询权限。默认库已由实际 CLI 从 schema v2 迁移至 v3，新增一个 handler_backup 本地测试账号，现有账号共 8 个，工单数仍为 0。

### 运行与动作契约

已有目录执行 `python -m app.seed` 可迁移数据库并补齐新增测试账号，已有账号及人工配置保留。实际命令使用 `.\.venv\Scripts\python.exe`。正常服务启动同样自动迁移表结构，但不会创建账号。新增 handler_backup 与 handler 同属 test-it，初始密码沿用上文测试密码。可用 test-office 工单在两者之间演练改派，handler_other 继续属于 test-admin。

以下动作均需要登录，JSON 请求拒绝额外字段。每个动作必填正整数 expected_version，使用最近读取工单的 version。成功返回 200 及更新后的工单，详情 history 继续由 GET 接口读取。

| 方法与路径 | 请求字段与权限 |
| --- | --- |
| GET /api/v1/support-groups/{group_id}/handlers | 授权组内分派人员读取有效处理人 id、display_name |
| POST /api/v1/tickets/{id}/assignments | expected_version、assignee_id。授权组内分派人员，待分派 → 待接单 |
| POST /api/v1/tickets/{id}/accept | expected_version。当前指定且仍有 handler 角色的处理人，待接单 → 处理中 |
| POST /api/v1/tickets/{id}/reassignments | expected_version、assignee_id、reason。授权组内分派人员，待接单或处理中 → 待接单 |
| POST /api/v1/tickets/{id}/notes | expected_version、note。当前处理人，仅处理中可添加公开进展备注 |

分派与改派的目标必须为启用账号，具有本工单处理组内有效 handler 成员范围。首版保持工单原处理组，不提供跨组转移。改派目标须与当前处理人不同。reason、note 去除首尾空白后须为 1～10000 字符。备注由当前处理人填写，作为历史追加保存，每次追加也增加版本，不提供编辑删除入口。该限制为首版工程选择。

工单查看沿用 ticket_visibility 的角色并集。改派后原处理人立即失去当前分配带来的权限，若其同时是提交人或有有效分派范围，仍可按该独立角色访问。所有有工单查看权限者均可读取公开备注和分派、改派历史。

不可访问或不存在返回相同 404。可访问但动作角色不足返回 403。版本过期返回 409 version_conflict，状态不允许返回 409 invalid_ticket_state。无效处理人为 422 invalid_assignee，相同处理人为 422 same_assignee，字段错误沿用 422 invalid_request。调用方收到版本冲突后重新读取工单并由操作者决定是否重试，不能盲目递增版本重放。改派使原处理人失去访问权时，其竞争动作返回 404。锁等待超时等存储暂不可用沿用 503，可先重新读取状态。

### 数据与事务契约

- schema v3 在 tickets 追加可空 assigned_at、accepted_at，在 ticket_history 追加可空 from_assignee_id、to_assignee_id、note。迁移与 user_version 更新在同一写事务中，v0／v1／v2 均可向前初始化，已有数据保留。旧历史新增字段为空，旧单缺失的动作时间保留为空，不补造历史。
- assigned_at 表示最近一次分派或改派时间，accepted_at 表示本次分派周期的接单时间。改派更新 assigned_at 并清空 accepted_at，下一位处理人接单时重新记录。此前各次动作时间保留在 history.created_at，时间仍为 UTC Unix 秒。
- 动作通过函数级 mutation_user 依赖在 BEGIN IMMEDIATE 获得写锁后重新加载可信身份、启停和权限，在同一连接内校验当前状态与版本，再以 WHERE id = ? AND version = ? 条件更新。每次成功增加 version，更新 updated_at，同步写历史。依赖在 HTTP 成功响应发送前提交，失败整体回滚，连接随后关闭。
- 新历史 action 为 assigned、accepted、reassigned、note_added，均记录 actor_id、前后状态、前后处理人和时间。改派原因及备注使用 note 保存。初始 created 历史保持原契约。
- 并发动作共享同一版本时至多一个成功。第二个请求重新读取写锁内最新状态后返回可处理冲突或权限失效，不产生第二条成功历史。未新增客户端请求编号，响应丢失后用 GET 核对状态、版本与历史，旧版本重试不重复执行动作。
- T04 继续复用当前身份、查看权限、写事务、版本条件更新和历史原子提交。处理结果轮次、员工确认、撤销及其权限需要按 T04 范围扩展，不能用通用状态修改绕过业务动作。

### T03 验证证据与边界

2026-11-06 执行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`，27 项通过，约 21.6 秒。涉及共享迁移与认证事务，因此包含既有 20 项及新增 7 项真实回环 HTTP 用例。新增测试覆盖以下内容。

- 分派、接单、公开备注、待接单与处理中改派、新处理人重新接单、重启保留、提交人查看完整历史和原处理人权限收回。
- 新入口未登录与越权拒绝，字段及版本校验，缺失／停用／无角色／跨组／成员范围缺失目标拒绝，非法状态、相同目标和空白内容拒绝。
- 同版本并发接单、接单与改派竞争、两次改派竞争只成功一次，状态、版本、负责人和历史一致。
- 分派、接单、备注、改派分别注入历史写入故障，返回 503，工单及历史整体回滚，同版本正常重试成功。首轮故障夹具曾以 IntegrityError 触发通用 500，随后改用项目既有的 OperationalError 故障注入方式，复验通过。
- 分派范围和处理角色收回后，已有令牌失去动作权限。
- v2 已有真实结构工单与历史保留、重复迁移可用，迁移中途字段冲突整体回滚，版本号保持 v2。既有测试继续覆盖 v1 迁移与新版本保护。

随后为本地手工演练新增默认种子账号 handler_backup，调整动作测试使用此账号，执行 `python -m unittest tests.test_ticket_actions tests.test_foundation.FoundationTests.test_test_seed_is_atomic_and_non_destructive tests.test_foundation.FoundationTests.test_newer_schema_is_rejected_without_rewriting -v`，9 项通过，约 9.7 秒。扩展既有种子用例验证七账号安装补齐一账号且保留人工修改后，单独重跑该用例通过，约 2 秒。以上命令均使用本项目 .venv 解释器。

另通过真实 `python -m app.seed` 初始化默认库和 `python -m uvicorn app.main:app` 临时回环 CLI 启动，验证健康检查、OpenAPI 新路由、分派人员登录、同组候选处理人列表及退出。默认库实测由 v2／7 账号／0 工单变为 v3／8 账号／0 工单，验证服务已关闭。

待确认、关闭、撤销状态的拒绝检查仍使用明确测试夹具，其真实动作属于 T04，T06 需补完整界面流程。正式身份、通知、跨组转移、界面与生产部署未验证。当前接续 T04。

## T04 处理结果、员工确认与撤销

2026-11-06 按用户“恢复任务状态并且开启4”实施，见 N23。用户明确选择当前处理人审批撤销申请，见 N24。默认库已通过实际 Uvicorn CLI 启动从 v3 迁移至 v4，8 个账号的编号、用户名及启停状态保留，工单数仍为 0。启动与账号使用方式沿用前文，临时验证服务已关闭。

### 动作接口

以下动作均需 Bearer 登录、正整数 expected_version，拒绝额外字段。成功返回 200 及更新工单。操作后使用 GET /api/v1/tickets/{id} 读取完整 history、按轮次排序的 results 和按持久化 sequence 排序的 cancellation_requests。动作本身返回的工单不包含这些明细数组。

| 方法与路径 | 请求字段与权限 |
| --- | --- |
| POST /api/v1/tickets/{id}/results | expected_version、result。当前处理人，仅处理中，提交结果进入待员工确认 |
| POST /api/v1/tickets/{id}/confirmations | expected_version、result_round、outcome。仅对应提交人，待员工确认时确认本轮结果，outcome 为 unresolved 则退回处理中，为 resolved 则关闭 |
| POST /api/v1/tickets/{id}/cancel | expected_version、reason。仅对应提交人，待分派或待接单且从未进入处理阶段时直接撤销 |
| POST /api/v1/tickets/{id}/cancellation-requests | expected_version、reason。仅对应提交人，进入过处理阶段后可申请撤销，包含改派后的待接单状态，保持原状态 |
| POST /api/v1/tickets/{id}/cancellation-requests/{request_id}/decision | expected_version、decision、reason。仅当前处理人，decision 为 approved 或 rejected。通过进入已撤销，驳回保持当前工单状态 |

result 和各类 reason 去掉首尾空白后须为 1～10000 字符。确认请求必须同时匹配工单版本和当前结果轮次。旧版本返回 409 version_conflict，当前版本带旧轮次返回 409 result_round_conflict。重复确认及重复审批不会追加成功历史。版本与状态检查沿用 T03，调用方收到冲突须先重新读取。

仅对应提交人且仍具有 submitter 角色可确认、撤销或申请。审批需要 handler 角色及当前 assignee_id。分派人员的组权限不会自动获得审批权。改派后原处理人失去当前分配带来的查看和审批权限，新处理人可在待接单、处理中或待员工确认时处理未决申请。角色叠加时，独立查看权限继续按原规则生效。

### 数据和工程选择

- schema v4 添加 tickets.result_round、result_submitted_at、confirmed_at、cancelled_at，沿用已有 closed_at。ticket_history 添加 result_round、cancellation_request_id。新增 ticket_results 与 cancellation_requests，迁移及版本号更新在同一事务中完成，失败回滚。v0～v3 可向前初始化，重复启动保留数据。
- 旧单 result_round 默认 0，新增时间为空，原历史追加字段为空。保留原有时间和历史，不补造结果或确认记录。
- ticket_results 以工单与 round 为复合主键，保存处理人、正文、提交时间、确认结果、确认人及确认时间。每次提交增加轮次，历史关联该轮次。员工未解决后继续处理，下一次提交结果形成新轮次。结果保存在详情 results 中，提交人可查看全部轮次。
- tickets.result_submitted_at 为最近一次结果提交时间，confirmed_at 为该轮确认时间，新轮次提交时清空 confirmed_at。只有 resolved 确认写入 closed_at，关闭不依赖评价。cancelled_at 用于直接撤销或审批通过，撤销不写 closed_at。时间均为 UTC Unix 秒，历史保存每次动作时间。T05 从 closed_at 计算评价窗口。
- 工程选择为每单最多一条 pending 撤销申请，通过部分唯一索引保证。再次申请返回 409 cancellation_pending，驳回后可重新申请。每条申请保存原因、申请人和时间，以及审批人、审批原因和时间。审批原因统一必填，已处理申请返回 409 cancellation_decided。
- 申请 pending 期间工单继续原业务流程。改派时申请保留并交由新当前处理人审批。确认关闭或直接撤销时，尚待处理的申请变为 superseded，并保存终态操作者、时间及“工单已结束”原因。approved 和 rejected 保留原结果。此处为首版工程选择，避免终态遗留可审批申请。
- 撤销申请使用 UUID 对外编号及整数 sequence 保存创建顺序，避免同秒多次申请因 UUID 排序颠倒。新历史动作为 result_submitted、confirmed_unresolved、confirmed_resolved、cancelled、cancellation_requested、cancellation_approved、cancellation_rejected，均沿用操作者、状态、负责人和时间契约。申请与审批历史通过 cancellation_request_id 关联具体记录。
- 所有新增写动作继续使用锁内身份重验、BEGIN IMMEDIATE、version 条件更新及发送响应前提交。结果、申请、确认或审批记录与工单和历史共用同一事务，失败整体回滚。同版本并发至多一次成功，另一次收到冲突。

### 验证证据与边界

2026-11-06 首轮执行 `.\.venv\Scripts\python.exe -m unittest tests.test_ticket_resolution -v`，7 项中 6 项通过，发现同秒申请排序导致测试取到旧申请。已增加持久化 sequence 并修复排序。随后执行以下命令，34 项全部通过，耗时约 45.1 秒。共享迁移及动作持久化函数变更影响既有身份和业务入口，因此同时验证已有 27 项用例及新增 7 项。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

新增验证使用临时独立数据库与真实回环 HTTP，覆盖以下内容。

- 首轮未解决回退、第二轮结果、旧版本和旧轮次确认拒绝、重复确认、关闭时间与确认时间一致、全部结果及历史公开给提交人。重启后仍保持待确认，确认关闭后真实入口创建关联新会话和新单。
- 待分派与待接单直接撤销、处理中直接撤销拒绝、处理中与待确认申请、重复申请、驳回后再次申请、当前处理人批准、关闭时未决申请结束，终态动作拒绝。
- 未登录、其他员工、处理人与提交人互相越权、分派人员审批越权、正文与原因空白／超长、布尔版本／轮次、额外字段拒绝。改派后旧处理人审批拒绝，当前处理人角色收回立即失权。
- 同版本两次确认、确认关闭与撤销审批竞争、两次撤销申请竞争均恰好一次成功，版本、状态、历史和申请结果一致。
- 结果提交、未解决确认、申请、审批驳回／通过、解决关闭、直接撤销分别注入历史写入故障，返回 503，工单、结果、申请及历史整体回滚，同版本重试成功。另外在结果明细写入时注入故障，验证已执行的工单更新与历史同样回滚。
- v3 真实结构中的账号、会话、已接单工单及历史迁移后保留，新字段默认值正确，重复初始化可用。迁移中途字段冲突整体回滚，原库保持 v3。既有 v1／v2 迁移及高版本保护用例同步通过。

另使用实际 `python -m uvicorn app.main:app` 临时回环 CLI 启动默认库，验证健康检查、五个新路由 OpenAPI 挂载、测试登录、权限列表与退出，实测默认库 v3 → v4、8 个账号保留、0 工单。验证进程已停止。测试未引入新依赖。

T04 交付时已具备上述 API 能力，评价与统计由后续 T05 实现，界面全流程留待 T06。正式身份、通知、长期未确认的人工处置、生产部署未验证。2026-11-06 后续恢复对话中，用户确认 T05 评价截止口径及统计授权范围，见 N25～N26 和 requirements.md。该次规则确认仅更新文档，未运行测试。

## T05 评价与基础评价统计

2026-11-06 按用户“开始t05吧”实施，见 N27。截止时刻及统计权限按 N25～N26。默认库经真实 Uvicorn CLI 启动由 schema v4 迁移至 v5，8 个账号的编号、用户名与启停状态保留，工单数仍为 0。临时验证服务已停止。

### 接口与评价窗口

- `PUT /api/v1/tickets/{id}/evaluation` 创建或修改评价，请求为正整数 expected_version、rating 和可选 reason。rating 为 good、neutral、bad，分别对应好评、中评、差评。reason 去掉首尾空白后最多 10000 字符，中差评原因必填，好评空原因保存为空。拒绝额外字段、布尔版本及无效等级。
- 仅当前仍有 submitter 角色的对应提交人可评价，且工单必须为 closed。依次检查可见性、本人权限、版本、状态及窗口。旧版本返回 409 version_conflict，到期返回 409 evaluation_locked，中差评缺少原因返回 422 evaluation_reason_required。成功返回更新工单，调用方重新读取详情取得评价及历史。
- 从员工确认解决写入的 closed_at 开始可评价。按北京时间计算，关闭日不计，从次日起计算七天，第七天结束时到期。例如北京时间 11 月 6 日任意时刻关闭，11 月 14 日零时起锁定。实现使用标准库固定 UTC+8 时区，截止时间转换为 UTC Unix 秒，与已有时间字段一致。
- 到期锁定由服务端在持有写锁后检查，即使到期命令尚未执行也拒绝主动评价。自动结果始终锁定。已有主动评价到期保留原等级和原因。
- `GET /api/v1/tickets/{id}` 沿用既有可见性，追加 evaluation、evaluation_history、evaluation_deadline_at、evaluation_locked。未关闭或缺少关闭时间的历史工单无评价截止时间且锁定，自动处理跳过这类记录。提交人、当前处理人和授权分派人员可按原范围查看评价和全部修改记录。

### 到期执行与数据契约

在项目根目录执行以下命令。数据库选择沿用 IT_ADMIN_DATABASE 环境变量与默认 data/backend.sqlite3。

```powershell
.\.venv\Scripts\python.exe -m app.evaluations
```

- 首版采用本地一次性命令作为到期执行入口。每次处理当前已到期、已关闭且尚无评价的工单，为其生成 automatic 来源的 good。命令可定期调用，界面演练可手工调用。本次未配置操作系统计划任务或常驻定时执行器，日常到期写入依赖定期执行该命令。
- 每批最多 100 单，在 BEGIN IMMEDIATE 事务内重新采样时间并查找待处理记录，逐批提交直至无待处理记录。失败回滚当前批次，已提交批次保留，再次调用继续处理。重复或多进程并发执行不会重复评价。已有主动或自动结果均跳过。
- schema v5 新增 ticket_evaluations 和 evaluation_history，以及关闭时间和评价历史索引。每单只有一份当前评价，每次成功修改保留快照。自动 actor_id 为空，来源明确为 automatic。主动评价保存真实提交人，来源为 active。独立评价历史支持系统事件，详情通过 evaluation_history 返回。
- 主动入口复用 ActionUser 的锁内身份重验，主动与自动写入共享持久化函数，采用工单 version 条件更新。评价、版本递增、updated_at 及评价历史在同一事务中提交。保持 closed_at、确认结果、主状态及既有工单历史，评价动作记录在独立评价历史中。
- created_at 为首次评价实际执行时间，updated_at 为最近一次修改时间，自动评价时间为命令实际执行时间。evaluation_deadline_at 始终从 closed_at 推导。v0～v4 可向前迁移，迁移失败整体回滚，重复初始化保留数据。

### 统计契约与工程口径

- `GET /api/v1/evaluation-statistics` 仅 manager 角色访问。默认汇总当前 manager 授权的有效处理组，可使用 group_id 限定其中一组，越范围返回 403。无授权组时返回零计数。该角色的统计权限独立于工单详情查看权限。
- 首版采用授权组内全量已关闭且 closed_at 非空的工单为分母，含评价窗口内的工单。撤销、待确认及其他未关闭工单排除。每单以最新评价计数，修改记录不重复计入。
- 返回 active 和 automatic 各自的 good、neutral、bad 计数，active_total、automatic_total、closed_total、unevaluated_total、overdue_unevaluated、group_ids 和 as_of。overdue_unevaluated 单列已到期但命令尚未补写的数量，统计读取不会写入评价。
- participation_rate 为 active_total / closed_total，取值 0～1，分母为 0 时返回 null。分子为主动评价工单数。统计的两次查询使用同一读取事务，计数保持同一快照。此处为首版基础统计工程口径，后续时间筛选及高级报表按需求分期。

### T05 验证证据与边界

新增 tests/test_evaluations.py，沿用独立临时数据库和真实回环 HTTP 夹具，无新增依赖。首轮执行 `.\.venv\Scripts\python.exe -m unittest tests.test_evaluations -v`，9 项全部通过，约 19.8 秒。随后完善自动结果锁定保护与自动处理重试验证，并执行以下命令。共享迁移、权限接口和详情返回有变更，因此覆盖既有 T01～T04 用例及新增用例。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

43 项全部通过，约 65.1 秒，覆盖以下内容。

- 真实流程关闭后创建、修改和重启读取评价，关闭时间保持原值，修改历史、等级、原因、来源及版本一致。
- 本人权限、处理人和其他员工越权、角色即时收回、未登录、非法状态、原因空白或超长、额外字段和布尔版本拒绝。
- 北京时间日初、日末、跨月、跨年、闰年截止计算。通过替换模块内时间对象控制测试时钟，验证截止前一秒可修改、截止瞬间锁定，自动评价同一边界生效，已有主动差评保持原结果。
- 两次同版本主动评价、主动评价与自动到期、两个到期执行者竞争。每单恰好一次成功持久化，版本及历史一致。主动作和到期命令在竞争测试中分别采样截止前一秒及截止瞬间，断言结果属于主动成功且自动跳过，或自动成功且主动版本冲突。
- 注入评价历史写入故障，HTTP 返回 503，主动和自动写入的当前评价及版本均回滚，解除故障后可重试。
- manager 授权组过滤、非管理角色和越组拒绝、组权限及角色收回立即生效，主动／自动分列、未评价与到期积压、参与率分母及零分母。
- v4 真实结构迁移保留账号、会话、已关闭工单及原历史，重复初始化成功，迁移表名冲突整体回滚。既有 v1～v3 迁移与高版本拒绝同步通过。
- 子进程通过实际 `python -m app.evaluations` 处理到期夹具，首次新增一条、再次零条，撤销工单排除。

随后为详情补齐“已生成自动评价在系统时钟回拨后仍显示锁定”的一致性保护，定向执行 `tests.test_evaluations.EvaluationTests.test_automatic_boundary_repeat_and_existing_result`，1 项通过，约 2.6 秒。

另以实际 `python -m uvicorn app.main:app` 临时回环 CLI 启动默认库，验证健康检查、新评价 PUT 与统计 GET 的 OpenAPI 挂载、主管登录、test-it 范围统计及退出。确认 schema v4 → v5，8 个账号与 0 工单保留。停止进程后实际运行到期命令，新增自动评价 0 条。临时服务已停止。

当前 T05 后端能力已完成。界面、界面端到端联验及运行中的定期到期调度留待后续安排。正式身份、通知、差评复核、生产部署和真实七天等待未验证。T06 可复用上述接口与 CLI，通过受控时间验证到期流程。

## T06 简易界面与首版流程联验

2026-11-06 按用户“开始执行吧”实施，见 N28。现有 FastAPI 同源提供 `GET /` 和 `/static/`，界面文件为 app/static/index.html、app.js、app.css。沿用前文启动命令，浏览器打开 http://127.0.0.1:8000/。本阶段无新增依赖和数据库迁移。账号及操作顺序见根目录 README。

### 界面和工程选择

- 使用原生 HTML、CSS、JavaScript，以现有真实 API 完成登录退出、工单列表与分页、建单重试、关联旧单、详情、分派改派、接单、公开备注、处理结果、员工确认、撤销申请审批、评价及主管授权组统计。
- 登录后从 `/api/v1/me` 读取可信角色和范围，结合工单状态、提交人和当前处理人展示动作，所有请求仍经过后端权限检查。配置管理员在本界面使用其已有提交人能力，配置维护能力按后续范围推进。
- Bearer 令牌、当前工单和建单会话仅保存在页面内存。退出及 401 清理页面会话与表单，页面刷新需重新登录。同一浏览器不同标签可分别登录不同账号。跨账号业务动作通过手工刷新工作台获取最新状态。
- 建单首次提交前创建会话，失败后保留已收到的会话编号以便重试，成功后继续提供同会话重试按钮。其他报障使用新会话，终态详情提供关联旧单建单入口。页面刷新会丢弃内存中的建单草稿及会话引用，已成功建单可从列表核对。
- 写动作携带当前详情的 expected_version，结果确认另带 result_round。409 后重新读取详情和列表、提示用户核对后重新填写动作，不自动重放旧操作。界面请求期间阻止重复交互，15 秒请求超时后提示刷新核对结果。详情读取失败时清除旧动作视图。
- 用户内容通过 textContent 展示。界面及静态资源启用同源 CSP、nosniff 和 no-store，保留 `/docs` 所需脚本加载行为。时间统一按北京时间显示。

### HTTP 和脚本验证

新增 tests/test_interface.py 的两项测试，验证页面与资源、响应头、Swagger 文档可用性、API 仍需认证和静态目录不能读取应用源码。复用既有真实回环 HTTP／独立数据库夹具，执行以下针对性验证。

```powershell
New-Item -ItemType Directory -Force data/t06-temp | Out-Null
$env:TEMP = (Resolve-Path data/t06-temp).Path
$env:TMP = $env:TEMP
.\.venv\Scripts\python.exe -m unittest tests.test_interface tests.test_ticket_actions tests.test_ticket_resolution tests.test_evaluations -v
node --check app/static/app.js
```

25 项测试通过，约 48.2 秒。覆盖新增界面资源以及既有分派处理、结果确认、撤销、评价的权限、版本、并发竞争、故障回滚及迁移检查。JavaScript 语法检查退出码为 0。首次测试因沙箱系统临时目录访问限制在 setUpClass 失败，没有形成通过结果；改用项目内临时目录后上述测试正常完成。本次未重跑完整 43 项业务回归。

### 真实浏览器联验

使用独立 `data/t06-browser.sqlite3` 创建 8 个测试账号，临时 Uvicorn 仅监听 127.0.0.1。初始沙箱服务的跨进程回环访问超时，改用经工具批准的本机临时服务及新浏览器标签后访问成功。通过浏览器表单完成以下流程。

- submitter 创建“测试办公系统”工单，同会话重试仍为 1 张工单。描述中的 HTML 标签按原文显示。
- dispatcher 分派，handler 接单、添加公开备注、提交第一轮结果，submitter 确认未解决退回，handler 再次提交，submitter 确认第二轮已解决并关闭。详情正确显示两轮结果、确认结果及操作历史。
- submitter 创建中评，空原因在界面被拒绝，填写原因后成功；随后修改为好评，详情保留两次评价记录。
- 已关闭工单关联新会话新单，直接撤销后再关联新会话新单。新单与旧单编号不同，详情保留 previous_ticket_id。
- 处理中申请撤销，dispatcher 改派回待接单并保留申请。原 handler 刷新后列表移除该单、详情返回不可访问。handler_backup 登录后驳回申请、接单，submitter 再次申请，由新当前处理人批准撤销。申请时间、审批原因及两次申请结果正确展示。
- dispatcher 使用旧版本改派收到 version_conflict，界面重新读取到待确认状态并提示核对，旧动作未被重放。
- 另一提交人登录可见 0 张工单，页面刷新回到登录入口，退出换账号后旧详情和草稿清除。最终重载最新脚本后登录、建单入口、列表及评价详情可操作，浏览器控制台未观察到 error 或 warn。
- 另从界面完成一张工单的建单、分派、接单、处理、确认关闭，保持未评价。仅在独立演练库将该单 closed_at 调整为当前时间前八天，作为到期夹具，随后通过实际 `python -m app.evaluations` 首次新增 1 条、重复执行新增 0 条。界面刷新显示“好评 · 到期自动好评”和“已锁定”，评价按钮消失。
- supervisor 统计显示 test-it 组已关闭 2 单、主动评价 1 单、自动好评 1 单、未评价 0 单、参与率 50.0%，主动与自动分列。越权和真正并发的代表性验证由上述 HTTP 测试完成。

演练库保留 4 张测试工单，其中 2 张关闭、2 张撤销。到期夹具只修改测试单 closed_at，故该单关闭时间早于其创建和操作历史，此记录仅用于受控到期联验。默认库已只读核实为 schema v5、8 个账号、0 工单。两次临时服务进程均已退出。

如需查看保留的演练结果，先在当前 PowerShell 设置 `$env:IT_ADMIN_DATABASE = 'data/t06-browser.sqlite3'` 再按正常命令启动。恢复默认库使用 `Remove-Item Env:IT_ADMIN_DATABASE -ErrorAction SilentlyContinue`。该环境变量也决定到期命令操作的数据库。

### 完成边界与后续

T01 至 T06 首版本地测试范围已完成。真实七天等待、正式身份／企微接入、通知、附件、配置维护、差评复核、复杂 SLA、生产部署及运行中的定期到期调度仍待后续安排。当前到期写入依赖定期调用已有命令。本次浏览器验证为桌面视口，移动端和其他浏览器未专项验证。当前目录未初始化 Git，变更由实际文件和以上执行证据交接。

## 一键启动入口

2026-11-06 按用户要求增加根目录 `启动服务.cmd`，使用当前目录旁的 .venv 运行 `python -m app.launcher`。入口自动切换项目工作目录，兼容路径中的空格，缺失虚拟环境或启动失败时显示信息并暂停窗口。启动器预先绑定本机 8000 端口并将同一 socket 交给 Uvicorn，端口占用时返回失败。服务启动就绪后调用默认浏览器打开首页；浏览器未打开时显示手动访问地址。控制台显示实际数据库路径，按 Ctrl+C 可停止服务。数据库选择沿用既有 IT_ADMIN_DATABASE 约定。

执行 `python -m unittest tests.test_launcher -v`，使用项目 .venv 和项目内 TEMP／TMP，2 项测试通过，约 2.2 秒。真实 Uvicorn 在独立临时库启动，就绪回调通过真实 HTTP 读取首页后正常关闭；浏览器打开调用用测试替身承接，未验证操作系统默认浏览器弹窗。另验证端口占用返回失败，以及通过真实 cmd.exe 从含空格目录执行批处理时正确提示虚拟环境缺失并返回 1。测试服务已退出。本次未重跑业务回归。

## 测试账号免密码登录

2026-11-06 按用户要求调整，见 N29。本节为当前本地测试登录契约。页面使用账号下拉框与“进入工作台”，从 `GET /api/v1/auth/test-accounts` 获取启用的测试账号用户名及显示名称。空库提示先初始化测试数据，初始化仍使用原 seed 命令。

`POST /api/v1/auth/login` 的 password 变为可选。只提交 username 时，账号必须启用，用户名属于 app/seed.py 的 USERS，且持久化编号为对应的 `test-{username}`。自建账号、编号不匹配账号、停用和不存在的账号均拒绝免密进入。显式提交 password 的已有调用继续校验密码。登录成功后仍签发八小时 Bearer 会话，退出撤销当前令牌，角色、范围和账号状态在业务请求中从数据库读取。数据库结构及存量账号密码字段沿用已有表示。

新增 tests/test_passwordless_login.py，覆盖八个测试账号选择登录及退出、列表字段与禁用过滤、自建及错配身份拒绝、额外角色字段拒绝和数据库权限收回。由于修改涉及认证入口，使用项目内 TEMP／TMP 执行完整 `python -m unittest discover -s tests -v`，50 项中 49 项通过，约 53.7 秒。唯一失败为启动器测试，原因是先前已导入 app.main 时 Uvicorn 复用模块级应用，未按测试中随后指定的数据库环境变量重新创建实例。启动器改用 `app.main:create_app` 工厂后，定向重跑 `python -m unittest tests.test_launcher -v`，2 项通过，约 1.9 秒。其余已通过业务用例未重复执行。`node --check app/static/app.js` 通过。

真实浏览器在独立演练库验证下拉框、提交人免密进入及退出、切换主管并读取授权统计，截图见本次交付。临时服务已退出。此身份选择方式用于当前本地测试阶段，正式身份接入仍按后续需求实施。


## 2026-11-06 调试修复增量

用户要求恢复并调试其通过独立临时库 HTTP 复现的三个问题，本次实施修复与必要验证。

- 种子新增 dispatcher_admin，具有 test-admin 的 dispatcher 范围。重新执行 `python -m app.seed` 会为已有八账号测试库补齐该账号，已有账号及人工配置继续保留。普通服务启动仍只迁移结构。README 已补充账号用途。默认库本次未读取或写入，使用默认库前需执行上述初始化命令以取得新账号。
- 撤销资格按是否进入过处理阶段判断。API 工单查询及动作响应新增计算字段 cancellation_requires_approval，综合当前状态、accepted_at、结果轮次及历史前后状态。改派清空本轮 accepted_at 后，历史仍保留审批要求，兼容既有 v5 工单，schema 保持 v5。直接撤销返回 409 cancellation_approval_required，改派后的待接单工单可新建撤销申请，由新当前处理人审批。未进入处理阶段的改派工单仍可直接撤销。页面入口同步使用该字段。
- 建会话及建单复用 mutation_user 的写事务，在 BEGIN IMMEDIATE 后读取当前会话、账号与角色，并在响应前提交。提交人角色检查、业务写入及历史保持同一锁内，避免锁前身份失效后仍然写入。

新增三项 HTTP 回归用例，分别覆盖行政建单到分派接单、结果确认关闭，处理前后改派的不同撤销资格及重启、新当前处理人审批，以及建会话／建单在锁前暂停后账号停用、角色收回、会话撤销的六组竞争场景。竞争场景确认 401／403 且会话、工单、历史数量无新增。既有种子用例增加从八账号补齐行政分派账号且保留人工配置的检查。

验证全部使用独立临时库。执行 `python -m unittest discover -s tests -v` 共 53 项，新增三项通过，49 项通过，4 项因新增账号数或 API 计算字段与持久化行对比断言需要适配。修订断言后，定向执行迁移、免密登录、种子等 7 项，6 项通过，剩余 v2 迁移用例在补齐计算字段排除项后单独复验通过。全部 53 项均已取得通过结果，最终断言调整后未重复整套执行。`node --check app/static/app.js` 通过。

浏览器使用独立 `data/debug-browser.sqlite3`，共九个测试账号、两张测试单。通过页面完成行政默认分类建单、退出切换 dispatcher_admin 并成功分派。另一张 IT 单通过真实 HTTP 准备接单与改派状态，再通过页面验证提交人显示申请撤销、成功申请、新当前处理人 handler_backup 在待接单状态批准，页面显示已撤销及已批准申请。浏览器未重复全部 T06 流程。首次沙箱回环连接超时，经批准以本机临时服务重试成功。临时服务已停止。


## 企业服务台界面改造

2026-11-09 用户明确授权按讨论方案动手改造。沿用现有原生前端，采用蓝灰配色、侧边导航、紧凑顶栏和表格布局。新建工单与详情使用原生 dialog 模态抽屉，支持键盘退出及关闭后保留当前建单草稿。详情分为基本信息、处理与评价、流转记录。导航按既有角色显示工单与主管评价汇总，工单页范围仍由后端既有可见性规则决定。

### 查询契约

GET /api/v1/tickets 保留原有分页参数及响应结构，新增以下可选条件。

- q 最长 200 字符，去除前后空白后匹配标题子串或完整工单编号。标题对 SQLite lower 支持的字符忽略大小写。百分号、下划线按普通字符匹配。
- status 仅接受现有六种工单状态。
- service_system_id 最长 128 字符，按运维系统编号精确匹配。
- 查询条件与原权限谓词通过 AND 组合，列表和筛选后总数在同一读取事务中计算，分页仍为创建时间倒序并以编号消除同秒歧义。省略查询条件时保持原有行为。

无新增依赖和 schema 迁移。页面仍通过 textContent 显示用户输入，使用现有 Bearer 会话、expected_version、result_round 和后端权限检查。成功提示六秒后隐藏，错误提示保留至下一次提示或关闭抽屉。

### 验证证据与边界

使用现有 .venv 和项目内 TEMP／TMP 执行以下命令。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_interface tests.test_tickets tests.test_ticket_actions tests.test_ticket_resolution tests.test_evaluations -v
node --check app/static/app.js
```

37 项测试通过，约 73.4 秒。新增一个筛选回归用例，覆盖组合筛选、权限隔离、筛选后分页总数、完整编号、标题匹配、特殊字符与非法参数。页面格式整理后单独复验 tests.test_interface 的 2 项静态资源测试通过，最终 JavaScript 语法检查通过。未运行完整项目回归。

浏览器使用独立 data/ui-enterprise-check.sqlite3，通过真实页面完成提交人建单、分派人分派、处理人接单与提交结果、提交人确认解决并好评。另验证处理时间线、关联旧单入口、搜索空结果及重置、主管授权统计、Esc 关闭抽屉、草稿保留和初始描述为空。检查桌面 1280×800 及约 418px 窄屏布局，临时视口覆盖已恢复。浏览器未专项重走全部撤销分支、旧版本冲突及多页导航，这些业务权限与并发边界由本次针对性后端用例覆盖。

首次沙箱内服务无法从浏览器访问，改为本机回环临时服务后完成验证。8766 验证服务已停止，独立库保留九个账号、一张已关闭好评工单。本次未操作默认业务库。正常使用继续通过根目录启动服务.cmd 打开 http://127.0.0.1:8000/，已有运行实例需重启以加载新增查询参数。
