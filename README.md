# 内部 IT 行政工单业务后端

这是从旧企微机器人试验目录中独立出来的新工作目录，位于 `D:/IT-Admin-Backend`。已完成 T01 至 T06，包含本地测试界面、身份、会话建单、权限查询、分派接单、改派、处理记录、结果提交、员工确认关闭、撤销、评价与基础统计。数据库为 schema v5，首版本地流程已完成界面联验。

## 本地启动

首次克隆项目后，在项目目录用 PowerShell 创建虚拟环境、安装依赖并初始化测试数据（当前开发使用 Python 3.14）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.seed
```

Git 仓库不包含本地虚拟环境、数据库、日志或 `.env` 配置。已有数据库无需重新复制到仓库。

在资源管理器中双击根目录的 `启动服务.cmd`，服务就绪后会自动打开服务台。保留启动窗口，按 Ctrl+C 停止服务。重复启动遇到端口占用会显示提示。入口使用项目现有虚拟环境，数据库沿用 `IT_ADMIN_DATABASE` 或默认库，窗口会显示实际路径。

也可在 PowerShell 中手动启动。

```powershell
Set-Location D:\IT-Admin-Backend
.\.venv\Scripts\python.exe -m app.seed
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 [服务台界面](http://127.0.0.1:8000/)，或使用 [接口文档](http://127.0.0.1:8000/docs)。初始化命令用于创建本地测试数据，重复执行会保留已有账号设置。本阶段使用已知测试密码和本机 HTTP，运行范围为本机测试。退出服务使用 Ctrl+C。

测试阶段在页面下拉框中选择账号，点击“进入工作台”即可免密码登录。列表仅显示已初始化且启用的测试账号，停用账号会被拒绝。接口调用 `POST /api/v1/auth/login` 时提交 username 即可取得测试账号会话，角色和处理组范围从数据库读取。

| 用户名 | 演练用途 |
| --- | --- |
| submitter、submitter_other | 两名独立提交人，查看本人工单 |
| dispatcher | 测试 IT 组手工分派及改派 |
| dispatcher_admin | 测试行政组手工分派及改派 |
| handler、handler_backup | 测试 IT 组处理人及备用处理人 |
| handler_other | 测试行政组处理人 |
| supervisor | 测试 IT 组评价统计 |
| config_admin | 配置管理员测试身份，当前可使用提交人界面 |

先用 submitter 选择“测试办公系统”建单，再用 dispatcher 分派给 handler，由 handler 接单、提交处理结果，最后用 submitter 确认并评价。可在不同浏览器标签分别登录，令牌保存在页面内存中，刷新后重新登录。处理后按“刷新工作台”读取其他账号的最新动作。新报障按“开始新会话”，终态详情可按“关联此单再次报障”。同会话重试返回原单。收到版本冲突后核对自动刷新的详情，再重新填写并提交动作。

详细接口、账号权限及验证记录见 [开发说明](docs/development.md)。

到期自动好评通过以下命令执行，可重复调用。日常运行需定期调用该命令，详细执行约定见开发说明的 T05 各节。

```powershell
.\.venv\Scripts\python.exe -m app.evaluations
```

## 从哪里开始

你先阅读 [后端业务范围与决策说明](docs/后端业务范围与决策说明.md)，了解职责、原 PRD 问题及优先决定事项。

其他执行对话先读 [项目规则](AGENTS.md) 和 [快速恢复](docs/quick-resume.md)，再读 [后端业务执行交接](docs/后端业务执行交接.md)。新对话应以本目录作为工作目录，不继续在旧目录开发。

| 文档 | 用途 |
| --- | --- |
| [需求说明](docs/requirements.md) | 已确认业务原则与待决边界 |
| [决策记录](docs/decisions.md) | 用户确认与候选方案的状态 |
| [架构设计](docs/architecture.md) | 模块化单体候选方案、数据关系、状态、权限、接口及待决事项 |
| [开发状态](docs/development.md) | 本地环境版本、安装步骤及已执行验证 |
| [分对话实施计划](docs/分对话实施计划.md) | T01 至 T06 的范围、依赖、验收和交接 |
| [参考 PRD](docs/references/内部IT行政工单系统PRD_v1.1.docx) | 原文参考，不是无冲突的冻结需求 |

## 文档来源

2026-11-05 从 `D:/PRD` 复制两份业务文档及该目录保留的 PRD 副本；两份文档的恢复说明已适配新目录。新建本目录的规则、需求、决策和恢复入口，不搬入旧原型架构与运行历史。

旧目录本次未修改。机器人代码、查看器、依赖、凭证、数据库、原始回调及清理工具均未迁入。历史细节只有实际需要时才定向查阅旧目录，不默认整目录扫描。
