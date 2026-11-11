# 工单AI接口对接说明

新建工单的 `id` 与 `ticket_id` 使用 `ITYYYYMMDDNNNN`，例如 `IT202611110001`。日期按北京时间取建单日期，四位流水在同一数据库内跨处理组递增，每日从0001开始。已有UUID工单继续使用原编号，查询、动作及旧单关联均兼容。调用方应保存接口实际返回的完整字符串。单日达到9999后新建返回409，错误码 `ticket_number_exhausted`。同会话重试仍返回原单。

示例中各类ID的含义如下，调用时使用对应接口实际返回的值。

| ID类型 | 字段及使用位置 | 示例格式 |
| --- | --- | --- |
| 会话ID | 创建会话响应的id、建单路径的conversation_id | `11111111-1111-4111-8111-111111111111`，UUID |
| 工单ID | 工单响应的id与ticket_id、工单操作路径、previous_ticket_id | `IT202611110001`，已有工单也可能返回UUID |
| 文件ID | 上传响应的file_id、建单或补充请求的file_ids | `22222222-2222-4222-8222-222222222222`，UUID |

更新日期为2026-11-11。

接口路径前加测试服务地址。本版包含工号测试接口、新工单编号、文字补充及优先级选项。

本文使用独立工号测试接口 `/api/test/v1`。每次调用都在URL查询参数中传 `employee_no`，例如 `employee_no=900001`，直接执行对应业务操作。工号按字符串传递并保留前导零，请求体仍使用各节列出的业务参数。

## 测试入口与人员识别

云端个人测试地址为 `http://101.43.60.219`，交互文档位于 `http://101.43.60.219/docs`。预置测试工号 `900001` 对应测试提交人，`900002` 对应另一测试提交人，分别访问各自提交的工单。真实员工工号首次建档依赖运行服务器可连接IAM。

本机独立测试地址为 `http://127.0.0.1:8772`，该地址供本机调用。

共同必填参数如下，适用于本文全部接口，包括上传及下载。

| 参数 | 位置 | 类型 | 说明 |
| --- | --- | --- | --- |
| employee_no | Query | string | IAM工号，1～64字符，保留前导零，禁止空白字符 |

例如，直接查询工号900001的工单。

```http
GET /api/test/v1/tickets?employee_no=900001&limit=20&offset=0
```

首次使用工号时，后端查询IAM并保存人员映射，后续使用本地已保存资料。新建人员授予提交人角色。停用账号和缺少提交人角色的账号返回403。IAM异常、无匹配及身份冲突返回对应错误，失败时保留已有人员和业务记录。列表、详情、旧单关联、附件和动作都按该员工的工单归属校验。

通过 `IT_ADMIN_TEST_EMPLOYEE_API=1` 开启工号测试接口。一键本地启动默认开启，直接Uvicorn启动时显式设置该变量。关闭时对应请求返回404及 `test_api_disabled`。该模式直接信任请求方传入的工号，仅用于测试联调。正式机器人服务身份契约继续按后续对接推进。

| 操作 | 方法与路径 |
| --- | --- |
| 查询可选运维系统 | `GET /api/test/v1/service-systems` |
| 创建会话 | `POST /api/test/v1/conversations` |
| 查询会话及建单结果 | `GET /api/test/v1/conversations/{conversation_id}` |
| 创建工单 | `POST /api/test/v1/conversations/{conversation_id}/ticket` |
| 查询本人工单 | `GET /api/test/v1/tickets` |
| 查询本人详情 | `GET /api/test/v1/tickets/{ticket_id}` |
| 上传附件 | `POST /api/test/v1/attachments` |
| 下载附件 | `GET /api/test/v1/attachments/{file_id}` |
| 文字及附件补充 | `POST /api/test/v1/tickets/{ticket_id}/supplements` |
| 确认处理结果 | `POST /api/test/v1/tickets/{ticket_id}/confirmations` |

运维系统查询返回 `items`，每项包含 `id`、`name`、`group_id`，建单时使用其 `id`。

下载使用上传返回的 `file_id`，例如 `GET /api/test/v1/attachments/22222222-2222-4222-8222-222222222222?employee_no=900001`。成功200返回二进制附件，响应头包含文件名。未绑定附件由上传工号下载，绑定后按提单人范围访问。

建单超时恢复时，调用 `GET /api/test/v1/conversations/{conversation_id}?employee_no=900001`。响应字段同创建会话，已建单时 `ticket_id` 返回原工单编号，尚未建单时为null。

## 1. 创建工单

先创建会话，保存会话ID。员工确认后，用该ID建单。一个会话最多建一张工单。

### 创建会话

```http
POST /api/test/v1/conversations?employee_no=900001
Content-Type: application/json

{}
```

返回201，响应中的 `id` 为会话UUID，用作建单参数 `conversation_id`。

```json
{
  "id": "11111111-1111-4111-8111-111111111111",
  "submitter_id": "<内部账号ID>",
  "previous_ticket_id": null,
  "created_at": 1794355200,
  "ticket_id": null
}
```

关联旧单时，在请求体中增加 `previous_ticket_id`，类型为string，去首尾空白后1～128字符。旧单须属于该员工，且已关闭或已撤销。

### 创建工单

`POST /api/test/v1/conversations/{conversation_id}/ticket`

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| conversation_id | Path | string | 是 | 第一步返回的会话UUID |
| service_system_id | JSON | string | 是 | 有效运维系统ID，去首尾空白后1～128字符 |
| title | JSON | string | 是 | 去首尾空白后1～200字符 |
| description | JSON | string | 是 | 员工确认的描述，去首尾空白后1～10000字符 |
| priority | JSON | string或null | 否 | P1、P3或null，省略保存为空 |
| location | JSON | string或null | 否 | 去首尾空白后最多200字符，省略、null及空白沿用人员地点 |
| file_ids | JSON | string[] | 否 | 上传返回的文件ID，默认空数组，最多5项，每项去首尾空白后1～128字符 |

```http
POST /api/test/v1/conversations/11111111-1111-4111-8111-111111111111/ticket?employee_no=900001
Content-Type: application/json

{
  "service_system_id": "test-office",
  "title": "打印机无法连接",
  "description": "重新连接网络后仍无法打印",
  "priority": "P3",
  "file_ids": []
}
```

原表字段与当前接口的对应关系如下。

| 原表字段 | 当前字段或处理方式 |
| --- | --- |
| system | 请求传service_system_id，响应system为系统名称。测试系统ID可用test-office |
| department、email | 从人员资料带入 |
| base_site | 对应location，默认使用人员地点 |
| engineer_employee_no | 建单后由分派人员指定处理人 |
| priority | 可选P1或P3，判定和处理规则暂缓 |

首次建单返回201，同会话重试返回200和原工单。以下响应省略了部分字段。

```json
{
  "id": "IT202611110001",
  "ticket_id": "IT202611110001",
  "status": "pending_assignment",
  "priority": "P3",
  "system": "测试办公系统",
  "service_system_id": "test-office",
  "title": "打印机无法连接",
  "description": "重新连接网络后仍无法打印",
  "version": 1,
  "files": []
}
```

后续操作使用返回的 `ticket_id`，按string保存并原样传入路径、搜索或旧单关联参数。建单超时后，使用原 `conversation_id` 重试；跨日重试也返回原工单编号。已建单后的文字和附件追加使用“确认或补充”接口。

当日四位流水达到9999时，新建返回409，响应如下。调用方展示错误并保留输入，后续建单继续使用原会话。

```json
{"detail":{"code":"ticket_number_exhausted","message":"当日工单编号已用完，请联系管理员"}}
```

附件须属于该员工，且尚未绑定其他工单。重复文件ID返回422。

## 2. 上传多个附件

`POST /api/test/v1/attachments`

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| files | multipart/form-data | File，重复字段 | 是 | 每次1～5个非空文件，单文件最多10 MiB，请求体最多50 MiB加64 KiB协议开销 |

多个文件使用同一个字段名 `files`。浏览器使用FormData上传时，由浏览器设置Content-Type。

```javascript
const form = new FormData()
for (const file of selectedFiles) form.append("files", file)
const response = await fetch(`${baseUrl}/api/test/v1/attachments?employee_no=${encodeURIComponent(employeeNo)}`, {
  method: "POST",
  body: form
})
const data = await response.json()
if (!response.ok) throw new Error(data.detail?.message || "上传失败")
const fileIds = data.items.map(file => file.file_id)
```

`baseUrl` 为测试服务地址，`employeeNo` 为工号字符串，`selectedFiles` 为选择的文件。

返回201。

```json
{
  "items": [
    {
      "file_id": "22222222-2222-4222-8222-222222222222",
      "file_name": "报错截图.png",
      "size": 204800,
      "content_type": "image/png",
      "created_at": 1794355200
    }
  ]
}
```

保存 `items[].file_id`，建单或补充时通过 `file_ids` 传入。

`size` 单位为字节，`created_at` 为Unix秒。文件名由后端清理，`content_type` 为上传时声明的媒体类型。一批文件中任一文件上传失败，整批回滚。重复上传会生成新文件ID。

## 3. 查询我的工单

`GET /api/test/v1/tickets`

| 参数 | 位置 | 类型 | 调用要求 | 说明 |
| --- | --- | --- | --- | --- |
| status | Query | string | 可选 | 单个英文状态，取值见文末状态表 |
| q | Query | string | 可选 | 最多200字符，匹配标题、初始描述、系统名称子串或完整工单编号 |
| service_system_id | Query | string | 可选 | 运维系统ID，最多128字符 |
| limit | Query | integer | 可选 | 默认20，范围1～100 |
| offset | Query | integer | 可选 | 默认0，最小0 |

参数放在URL查询字符串中。关键词使用URL编码。

```http
GET /api/test/v1/tickets?limit=20&offset=0&employee_no=900001
```

返回200。以下响应省略了items中的部分字段。

```json
{
  "items": [
    {
      "ticket_id": "IT202611110001",
      "title": "打印机无法连接",
      "system": "测试办公系统",
      "priority": "P3",
      "status": "pending_assignment",
      "version": 1,
      "created_at_iso": "2026-11-11T08:00:00+08:00",
      "updated_at_iso": "2026-11-11T08:00:00+08:00"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

下一页使用 `offset + limit`。`total` 为当前查询条件下该员工的工单总数。结果按创建时间倒序，同一创建时间按ID倒序。工号测试接口固定限定该员工本人提交的工单，具有其他角色的员工也按此范围查询。

## 4. 查询待处理工单

使用 `GET /api/test/v1/tickets`，参数同“查询我的工单”。例如，查询该员工提交的处理中工单。

```http
GET /api/test/v1/tickets?status=in_progress&limit=20&offset=0&employee_no=900001
```

返回200，响应结构同“查询我的工单”。

“待处理”包含哪些状态还需确认。当前支持单状态查询，多状态查询待开发。

## 5. 工单详情

`GET /api/test/v1/tickets/{ticket_id}`

| 参数 | 位置 | 类型 | 调用要求 | 说明 |
| --- | --- | --- | --- | --- |
| ticket_id | Path | string | 必填 | 创建工单返回的编号 |

```http
GET /api/test/v1/tickets/IT202611110001?employee_no=900001
```

返回200，主要字段如下。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id、ticket_id | string | 同一个工单编号 |
| title、description | string | 初始标题及描述 |
| service_system_id、system | string | 运维系统ID及名称 |
| priority | string或null | P1、P3或null |
| status | string | 取值见文末状态表 |
| engineer_employee_no | string或null | 已关联IAM的当前处理人工号，保留前导零。未分派或无映射时为空 |
| department、email、location | string或null | 建单时人员资料及Base地快照 |
| files | object[] | 已绑定附件，元素字段同上传响应items |
| version | integer | 最新版本，补充及确认时传入expected_version |
| result_round | integer | 最新处理结果轮次，确认时使用 |
| history | object[] | 流转记录，含action、actor_id、created_at、note等字段 |
| results | object[] | 处理结果，含round、content、submitted_at、confirmation等字段 |
| created_at、updated_at | integer | Unix秒时间戳 |
| created_at_iso、updated_at_iso | string | 北京时间+08:00的ISO8601时间 |

`history` 按id升序排列。员工补充记录的 `action` 为supplement_added，`note` 为补充文字，`actor_id` 为补充人内部ID，`created_at` 为Unix秒。

`results` 按round升序排列。`content` 为处理结果，`confirmation` 为resolved、unresolved或null。

工单不存在或该员工无权查看时返回404。

## 6. 确认或补充

建单确认使用“创建工单”接口。已建单后的文字和附件追加使用以下接口。

`POST /api/test/v1/tickets/{ticket_id}/supplements`

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| ticket_id | Path | string | 是 | 原工单编号 |
| expected_version | JSON | integer | 是 | 最新详情version，最小1 |
| description | JSON | string或null | 条件必填 | 去首尾空白后最多10000字符 |
| file_ids | JSON | string[] | 条件必填 | 默认空数组，最多5项，每项去首尾空白后1～128字符 |

文字和附件至少填一项。附件先上传，再传文件ID。提单人可在待分派、待接单、处理中、待员工确认阶段补充。

```http
POST /api/test/v1/tickets/IT202611110001/supplements?employee_no=900001
Content-Type: application/json

{
  "expected_version": 1,
  "description": "补充现场现象，重启后仍然报错",
  "file_ids": ["22222222-2222-4222-8222-222222222222"]
}
```

返回200，包含更新后的工单和files。以下响应省略了部分字段。

```json
{
  "ticket_id": "IT202611110001",
  "status": "pending_assignment",
  "version": 2,
  "files": [
    {
      "file_id": "22222222-2222-4222-8222-222222222222",
      "file_name": "报错截图.png",
      "size": 204800,
      "content_type": "image/png",
      "created_at": 1794355200
    }
  ]
}
```

补充成功后version加1，文字记录在详情history中。文字、附件绑定、版本和历史一起保存，任一步失败全部回滚。

重复提交相同文字并使用最新版本，会新增一条补充记录。只传同单已有附件时，版本保持原值。请求超时后先查详情核对，旧版本提交返回409。

“待补全后转待分派”的规则待定。当前补充保留原描述和工单状态。

## 7. 状态回写

员工确认处理结果使用以下接口。

`POST /api/test/v1/tickets/{ticket_id}/confirmations`

| 参数 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| ticket_id | Path | string | 是 | 原工单编号 |
| expected_version | JSON | integer | 是 | 最新详情version，最小1 |
| result_round | JSON | integer | 是 | 最新详情result_round，最小1 |
| outcome | JSON | string | 是 | resolved或unresolved |
| note | JSON | string或null | 否 | 去首尾空白后最多10000字符 |

工单处于awaiting_confirmation时，由提单人确认后调用。

```http
POST /api/test/v1/tickets/IT202611110001/confirmations?employee_no=900001
Content-Type: application/json

{
  "expected_version": 5,
  "result_round": 1,
  "outcome": "resolved",
  "note": "员工确认已恢复"
}
```

返回200，响应为更新后的工单。outcome传resolved时关闭工单，传unresolved时回到处理中。note写入history。版本或结果轮次过期时返回409，应先重新查询详情。

状态取值如下。

| status | 名称 |
| --- | --- |
| pending_assignment | 待分派 |
| pending_acceptance | 待接单 |
| in_progress | 处理中 |
| awaiting_confirmation | 待员工确认 |
| closed | 已关闭 |
| cancelled | 已撤销 |

独立“已解决”状态及待补全流转待定。当前确认已解决后返回closed。

错误响应示例。

```json
{"detail":{"code":"version_conflict","message":"工单已更新，请重新读取后再操作"}}
```

页面显示 `detail.message`。字段校验失败时，`detail.fields` 还会返回具体字段及原因。

| HTTP状态 | 处理方式 |
| --- | --- |
| 403 | 员工账号停用或缺少提交人权限 |
| 404 | test_api_disabled表示测试接口关闭，其他情况表示记录不存在或无权访问 |
| 409 | 按detail.code处理。ticket_number_exhausted表示当日编号已用完，展示提示并保留输入及会话；其他冲突重新查询详情，核对版本、轮次和状态 |
| 413 | 减少文件数量或大小，检查上传格式 |
| 422 | 按错误提示修改参数 |
| 503 | 保留输入，稍后处理 |

示例中的工单编号、会话和文件ID需替换为接口实际返回的值。JSON请求使用 `Content-Type: application/json`，GET参数放在路径或查询字符串中。
