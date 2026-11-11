# 内部 IT 行政工单业务后端

这是从旧企微机器人试验目录中独立出来的新工作目录，位于 `D:/IT-Admin-Backend`。已完成 T01 至 T06，包含本地测试界面、身份、会话建单、权限查询、分派接单、改派、处理记录、结果提交、员工确认关闭、撤销、评价与基础统计。当前代码支持 schema v8、附件、IAM 人员资料接入、员工补充及优先级选项，启动时迁移已有数据库。实际验证及发布进度见开发说明。

## 腾讯云个人测试环境

本轮腾讯云部署统一标记为 **个人测试环境**，用于功能试玩和工单流程演练，沿用现有测试账号选择方式。用户已明确允许任何人访问，访客可选择测试身份并操作测试工单。相关部署、启动、数据库和验证记录均按测试用途记载。正式运行平台、身份接入和生产验收安排另行确定。

个人测试版已部署，使用独立 SQLite 测试库。实际进度与证据见 [快速恢复](docs/quick-resume.md) 的个人云端测试记录。

2026-11-11 本次发布包含工号测试API、历史格式工单编号、员工补充及P1/P3可选优先级，数据库为schema v8。个人测试环境预置工号900001和900002，调用方式见[工单AI接口对接说明](docs/工单AI接口对接说明.md)。真实员工工号首次建档依赖IAM内网连接及授权配置。

### 访问云端测试版

公网入口为 [个人测试工作台](http://101.43.60.219/)，使用 HTTP，选择测试账号即可进入。Nginx 通过公网 80 端口转发到服务器本机的测试应用。当前访问范围为任何人。

原有 [SSH 转发入口](http://127.0.0.1:18000/) 也可使用。该入口通过当前电脑的 SSH 通道连接腾讯云。电脑重启或通道断开后，可在 PowerShell 执行以下命令重新连接，保留该窗口，按 Ctrl+C 关闭通道。

```powershell
$key = Join-Path ([Environment]::GetFolderPath('Desktop')) 'txy_pri.pem'
ssh -i $key -N -T -L 127.0.0.1:18000:127.0.0.1:8000 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 ubuntu@101.43.60.219
```

服务器上的测试服务为 `it-admin-backend-test.service`，已启用开机启动和失败重启，监听 `127.0.0.1:8000`。项目目录为 `/opt/it-admin-backend`，解释器为 `.venv/bin/python`，数据库通过 `IT_ADMIN_DATABASE` 指向 `/opt/it-admin-backend/data/cloud-test.sqlite3`。

当前保留 9 个测试账号及一张已关闭好评的云端部署验收测试单。到期自动评价沿用手动命令，可在服务器项目目录执行以下命令。

```bash
IT_ADMIN_DATABASE=/opt/it-admin-backend/data/cloud-test.sqlite3 .venv/bin/python -m app.evaluations
```

## IAM 人员资料接入

本地测试可直接双击 `启动服务.cmd`，启动器自动读取项目根目录的 `IAM系统授权信息工单系统.md`，并默认启用员工工号免密测试模式。页面展开“员工工号登录”，输入工号即可进入。首次登录按工号查询 IAM，并建立提交人账号，姓名、部门、邮箱和 Base 地自动带入。测试模式按输入工号选择员工身份。

手动启动时，在项目目录的 PowerShell 窗口显式设置配置及测试模式。密钥由后端读取，授权文件已加入 Git 忽略规则。

```powershell
$env:IT_ADMIN_IAM_CONFIG = 'D:\IT-Admin-Backend\IAM系统授权信息工单系统.md'
$env:IT_ADMIN_IAM_PASSWORDLESS = '1'
.\.venv\Scripts\python.exe -m app.iam lookup --username 010591
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

也可使用本地命令提前导入人员。该命令仍可设置12～256字符的本地密码，供关闭员工免密测试模式后使用。已有 IAM 人员再次导入会刷新资料，并保留原密码、角色和启停状态。新人员初始授予提交人角色。

```powershell
.\.venv\Scripts\python.exe -m app.iam import-user --username 010591 --database 'D:\IT-Admin-Backend\data\backend.sqlite3'
.\启动服务.cmd
```

“更新本人资料”可重新查询人员信息。也可用 `--qywx-id` 代替 `--username` 查询或导入，企微原始人员标识按原值匹配。直接使用 Uvicorn 时员工免密默认关闭，设置 `IT_ADMIN_IAM_PASSWORDLESS=1` 可开启；一键入口允许通过预先设置该变量为 `0` 关闭。

资料缓存在业务库，登录时超过一小时按需刷新。IAM 查询失败时显示提示并继续使用已保存资料，查无匹配不会自动停用账号。已有工单的部门、邮箱和地点保留建单时的数据。正式企微身份和机器人服务认证按后续接入推进。

当前 IAM 通过内网地址直连，保留 HTTPS 证书校验，单次请求超时8秒。运行服务器需要可访问该内网接口。云端连通性和发布进度见快速恢复。

## 本地启动

本地工作区已增加附件上传、建单绑定、原工单追加与鉴权下载，使用schema v6及新增python-multipart依赖。已有环境更新后执行下方依赖安装命令，启动应用时自动迁移。每次最多5个附件，单文件10 MiB。接口和验证范围见[附件开发契约](docs/development.md#2026-11-11-附件适配)。附件已纳入个人云端测试版本。

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

## 工号测试 API

测试联调使用 `/api/test/v1`，每次请求在URL查询参数中传 `employee_no`，例如 `/api/test/v1/tickets?employee_no=010591`。首次使用通过IAM确认并保存人员，之后复用保存资料。工单读取和操作固定为该工号本人提交的范围。参数、响应和完整流程见 [工单AI接口对接说明](docs/工单AI接口对接说明.md)。

一键本地启动默认设置 `IT_ADMIN_TEST_EMPLOYEE_API=1`。手动Uvicorn启动时设置该变量即可开启，设为0关闭。IAM连接配置沿用 `IT_ADMIN_IAM_CONFIG`，已有人员映射可直接使用。该模式直接信任传入工号，用于测试联调。当前独立验收服务地址及数据库见快速恢复记录。
