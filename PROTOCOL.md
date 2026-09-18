# 协议规范 — tbox ↔ tbox-server Wire Protocol v1（已废弃）

> **本文件描述的是 v1 协议（2026-09-15 之前），已不再代表服务器行为。**
> 当前协议见 [`TBOXHELPER_PROTOCOL.md`](./TBOXHELPER_PROTOCOL.md)（v2）。
> 保留此文件仅用于历史参考；不要按本文件实现新客户端。

本文件是终端（tbox / telematics box）和服务器之间的**完整协议定义**。
按此规范可以实现一个能跟 `tbox-server` 互通的终端 SDK。

---

## 1. 架构概览

```
┌─────────────┐                              ┌────────────────┐
│   终端 SDK   │ ─── HTTP request ─────────► │                │
│ (HTTP 客户端) │ ◄── HTTP response ──────── │  tbox-server   │
│             │                              │  (aiohttp 服务端)│
└─────────────┘                              └────────────────┘

终端是 HTTP 客户端，主动发起所有连接。
服务器不主动连接到终端；服务器→终端的推送通过「长轮询」实现。
```

| 项 | 取值 |
|---|---|
| 传输层 | HTTP/1.1，UTF-8，明文（**没有 TLS**，靠可信网络隔离） |
| 默认端口 | `9999` |
| 默认地址 | `0.0.0.0:9999`（监听所有接口） |
| 认证 | **无**（任何能访问到端口的客户端都能调用） |
| 编码 | JSON（UTF-8）；上传用 `multipart/form-data` |
| 终端身份标识 | 字符串 `terminal_id`（任意非空），最长 64 字符 |
| 请求 ID | 服务端生成 `X-Request-Id`，回显在响应头 |

---

## 2. 通用约定

### 2.1 响应信封

**成功响应**统一格式：

```json
{ "status": "ok", ...payload }
```

**错误响应**统一格式：

```json
{ "status": "error", "message": "<人类可读描述>", ...optional }
```

错误响应的 HTTP 状态码语义：

| 状态码 | 含义 |
|---|---|
| `200` | 成功（含业务逻辑错误，前端看 `status` 字段） |
| `400` | 请求格式错误（缺字段、JSON 解析失败、校验失败） |
| `404` | 路径不存在，或查询的资源不存在 |
| `413` | 请求体超过服务器上限 |
| `415` | Content-Type 不支持 |
| `500` | 服务器内部错误（带 `request_id`，查日志用） |
| `503` | 服务暂时不可用（如终端命令队列已满） |

### 2.2 通用响应头

```
X-Request-Id: <32 位 hex>     服务端生成；调试/排错时贴给运维查日志
Content-Type: application/json; charset=utf-8
```

### 2.3 时间

- 所有 `ts` 字段：UNIX 秒（浮点）
- 客户端时间不可信，服务端**总是返回** `server_time` 给客户端做时钟校正

---

## 3. 终端 → 服务器 上行（7 个端点）

### 3.1 `POST /heartbeat` — 存活上报

**频率**：建议每 `TBOX_HEARTBEAT_INTERVAL_S`（默认 30 秒）一次。

请求：

```http
POST /heartbeat HTTP/1.1
Content-Type: application/json

{
  "terminal_id": "TBOX-0001",
  "fw_version": "1.2.3",
  "ts": 1731628800.5,
  "address": "10.0.0.5",
  "caps": ["gps", "can", "wifi"],
  "free_disk_mb": 512
}
```

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `terminal_id` | ✅ | string | 终端唯一标识 |
| `fw_version` |  | string | 固件版本号（≤64 字符） |
| `ts` |  | number | 客户端 UNIX 时间 |
| `address` |  | string | 终端自身 IP（仅展示用） |
| `caps` |  | string[] | 能力列表（GPS/CAN/WiFi 等） |
| `free_disk_mb` |  | int | 剩余磁盘 MB |

响应 200：

```json
{
  "status": "ok",
  "server_time": 1731628801.123,
  "heartbeat_interval_s": 30,
  "long_poll_timeout_s": 30,
  "server_version": "0.1.0"
}
```

**客户端应遵守**：
- 把 `heartbeat_interval_s` 当作建议值；网络差时适当调大
- 用响应里的 `server_time` 校正本地时钟（`offset = server_time - local_ts`）

---

### 3.2 `GET /poll` — 长轮询取命令

**核心机制**：客户端挂一个 GET 请求不释放；服务器要么立刻返回命令，要么等
到有命令入队才返回。空闲时返回 `idle`。

请求：

```http
GET /poll?wait=30 HTTP/1.1
X-Terminal-Id: TBOX-0001
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `wait` | 否 | 最长挂起秒数；服务端会 `clamp(0, max=120)`，默认 30 |
| `terminal_id` | 可选 | 也能用 query 参数代替 header |

**有命令立即响应**：

```json
{
  "status": "command",
  "cmd": {
    "cmd_id": "f1196139f0ba41539fa2d18f67785994",
    "type": "reboot",
    "payload": {"delay_s": 5},
    "issued_at": 1731628800.0,
    "ttl_s": 3600
  },
  "server_time": 1731628801.5
}
```

**超时无命令响应**：

```json
{ "status": "idle", "server_time": 1731628830.0 }
```

**客户端应遵守**：

1. **用 `-N` / `--no-buffer` 关闭缓冲**（curl / aiohttp 都有这个开关）
2. **客户端断连时已经取到但未 ack 的命令会被服务器重新入队**——所以 SDK 断网
   后不要假设命令丢失，下次重连继续 poll 即可
3. `wait` 设短一点（如 25 秒）比设长更安全：服务器单次超时上限 120 秒
4. 推荐循环：poll → 收到 idle → 立即再 poll

---

### 3.3 `POST /command/ack` — 命令执行结果回执

请求：

```json
{
  "terminal_id": "TBOX-0001",
  "cmd_id": "f1196139f0ba41539fa2d18f67785994",
  "status": "ok",
  "result": {"exit_code": 0, "stdout": "..."},
  "ts": 1731628900.0
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `cmd_id` | ✅ | 来自 `/poll` 返回的 `cmd.cmd_id` |
| `status` | ✅ | `"ok"` / `"fail"` / 自定义字符串 |
| `result` |  | 任意 JSON object，服务端原样存内存（最近 50 条/终端） |
| `ts` |  | 客户端完成时间 |

响应 200：`{"status": "ok", "cmd_id": "..."}`

> **注意**：服务器只把 ack 存在内存里，**不落盘**。重启后丢失。

---

### 3.4 `GET /version` — 查询最新固件版本

请求：

```http
GET /version?channel=stable HTTP/1.1
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `channel` | `stable` | 频道名（stable / beta / custom） |

**有清单响应 200**：

```json
{
  "status": "ok",
  "channel": "stable",
  "latest": "1.4.3",
  "url": "/downloads/firmware-1.4.3.bin",
  "sha256": "ab12...",
  "mandatory": false,
  "release_notes": "GPS jitter fix"
}
```

**无清单响应 204**：body 为空。客户端应回退到「不升级」或重试。

服务器从 `data/versions/<channel>/latest.json` 读这份清单（5 秒缓存）。
运维改文件后 5 秒内生效。

---

### 3.5 `POST /upload` — 文件上传（multipart）

请求：

```http
POST /upload HTTP/1.1
Content-Type: multipart/form-data; boundary=----X

------X
Content-Disposition: form-data; name="metadata"
Content-Type: application/json

{
  "terminal_id": "TBOX-0001",
  "path": "/var/log/diag.bin",
  "sha256": "ab12...64hex",
  "size": 1048576,
  "kind": "diagnostics"
}
------X
Content-Disposition: form-data; name="file"
Content-Type: application/octet-stream

<二进制字节流>
------X--
```

`metadata` 字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `terminal_id` | ✅ | |
| `path` |  | 终端上的原始路径（用于展示），会经过 `safe_filename` 清洗 |
| `sha256` |  | 64 位 hex；非空时服务端会校验 |
| `size` |  | 字节数；非零时服务端会校验 |
| `kind` |  | `diagnostics` / `firmware` / `screenshot` / `crashdump` / 其他 |

响应 200：

```json
{
  "status": "ok",
  "stored": "data/uploads/2026-09-15/TBOX-0001/ab12__diag.bin",
  "bytes": 1048576,
  "sha256": "ab12..."
}
```

**错误码**：

| 状态 | 触发条件 |
|---|---|
| `400 sha256 mismatch` | 客户端填的 sha 跟服务端算的不一致 |
| `400 size mismatch` | 客户端填的 size 跟服务端读到的字节数不一致 |
| `413 exceeds N bytes` | 超过 `TBOX_MAX_UPLOAD_BYTES`（默认 64 MiB） |
| `415 expected multipart` | Content-Type 不是 multipart |

**客户端应遵守**：
- 文件用**二进制模式**读（避免 Windows CRLF 转换）
- sha256 用边读边算（`hashlib.sha256()`），不要先读完再算
- 上传期间**不要断网**；断网会导致服务器丢弃部分文件但 meta.json 不写入

---

### 3.6 `POST /logs/upload` — 日志上传（multipart）

请求结构同 `/upload`，但有两个不同：

| 字段 | 说明 |
|---|---|
| `metadata.app` | 产生这条日志的进程名 |
| `metadata.level` | `debug` / `info` / `warn` / `error` |
| `metadata.seq` | 终端单调递增的序号 |
| `metadata.ts` | 终端时间戳 |
| `file` 字段名 | 这里叫 `log`（不是 `file`） |

文件 part 名是 **`log`**（注意区别）。

---

### 3.7 `POST /report` — 通用上报（任意 JSON）

**这是 v1 最灵活的端点**：终端可以上报**任意结构**的 JSON，服务端原样存储不解析。

支持两种 Content-Type，二选一。

#### 3.7.1 `application/json` 形式（推荐，最简单）

请求：

```http
POST /report HTTP/1.1
Content-Type: application/json

{
  "terminal_id": "TBOX-0001",
  "report_type": "version",
  "ts": 1731628800,
  "data": {
    "fw": "1.4.2",
    "components": {"gps": "1.0", "can": "2.1"},
    "any_custom_field": [1, 2, 3]
  }
}
```

`data` 字段就是终端想上报的**任意 JSON object/array**，服务端**完全透传**。

#### 3.7.2 `multipart/form-data` 形式（适合大 JSON）

```http
POST /report HTTP/1.1
Content-Type: multipart/form-data; boundary=----X

------X
Content-Disposition: form-data; name="metadata"
Content-Type: application/json

{"terminal_id":"TBOX-0001","report_type":"version","ts":1731628800}
------X
Content-Disposition: form-data; name="file"
Content-Type: application/json

{"fw":"1.4.2","components":{"gps":"1.0"}}
------X--
```

文件 part 名可以是 **`file`** / **`data`** / **`report`**（任一）。

#### 3.7.3 响应（两种形式都一样）

```json
{
  "status": "ok",
  "stored": "data/reports/TBOX-0001/version/latest.json",
  "history": "data/reports/TBOX-0001/version/history/1731628800.json",
  "bytes": 83,
  "report_type": "version"
}
```

#### 3.7.4 存储路径

```
data/reports/<terminal_id>/<report_type>/
├── latest.json                              ← 总是覆盖成最新一次
└── history/
    ├── 1731628800.json                      ← 第一次
    ├── 1731628900-a1b2.json                 ← 同秒收到多条时加 4 位 hex 后缀
    └── ...
```

#### 3.7.5 错误码

| 状态 | 触发条件 |
|---|---|
| `400 missing terminal_id` | metadata 里没 terminal_id |
| `400 not valid JSON` | `data` 字段不是合法 JSON |
| `400 must be a JSON object or array` | `data` 是字符串、数字等标量 |
| `413 exceeds N bytes` | 超过 `TBOX_MAX_LOG_BYTES`（默认 4 MiB） |
| `415 Content-Type must be multipart/form-data or application/json` | 其他 Content-Type |

#### 3.7.6 客户端应遵守

- `report_type` 是终端自己起的分类名（version / diagnostics / status / 任意）
- 同一 `report_type` 的 `latest.json` 永远只有最新一次，**历史在 `history/`**
- JSON 内部结构由 SDK 自己定义，**SDK 改动字段不需要协调服务端版本**

---

## 4. 服务器 → 终端 下行

### 4.1 命令下发（`POST /admin/command` + `/poll` 投递）

服务器管理员调管理接口入队；终端通过 `/poll` 长轮询拿到。

请求（管理员操作，终端不需要实现）：

```http
POST /admin/command HTTP/1.1
Content-Type: application/json

{
  "terminal_id": "TBOX-0001",
  "type": "reboot",
  "payload": {"delay_s": 5},
  "ttl_s": 3600
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `terminal_id` | ✅ | |
| `type` | ✅ | 必须在下表的命令类型白名单内 |
| `payload` |  | 任意 JSON object，按 type 解释 |
| `ttl_s` |  | 命令有效期（秒），过期被 reaper 清掉，默认 3600 |

**合法 `type` 白名单**：

```
reboot, update_config, fetch_diagnostics, exec, ota_check, log_dump
```

响应 200：

```json
{ "status": "ok", "cmd_id": "f1196...", "issued_at": 1731628800.5 }
```

**投递机制**：

```
管理员 ─► /admin/command ─► [队列] ─► /poll (终端长轮询)
                                  ▲
                                  └── 服务器入队后立刻唤醒 poll，< 1s 到达
```

**客户端应遵守**：

| 情况 | 客户端行为 |
|---|---|
| 收到 `status=command` | 解析 `cmd` 字段，按 `type` 路由处理逻辑 |
| `type=reboot` | 延迟 `payload.delay_s` 秒（默认 0）后系统重启 |
| `type=exec` | 执行 `payload.cmd`，捕获 stdout/stderr/exit_code |
| `type=fetch_diagnostics` | 采集诊断包后调 `/upload` 上传 |
| `type=ota_check` | 调 `/version?channel=<payload.channel>` 查升级 |
| 执行完 | 必须调 `/command/ack` 回执，否则服务端不知道结果 |

---

## 5. 管理 API（运维）

> 这一组**不是给终端用的**，是给运维/管理后台用的。

### 5.1 `GET /admin/terminals`

列出所有已知终端及状态。

响应 200：

```json
{
  "status": "ok",
  "terminals": [
    {
      "terminal_id": "TBOX-0001",
      "online": true,
      "last_seen": 1731628801.5,
      "fw_version": "1.2.3",
      "address": "10.0.0.5",
      "caps": ["gps", "can", "wifi"],
      "free_disk_mb": 512,
      "pending_commands": 0,
      "last_report": {
        "type": "version",
        "ts": 1731628800.0,
        "path": "data/reports/TBOX-0001/version/latest.json"
      }
    }
  ]
}
```

`online` 判定：当前时间 - `last_seen` ≤ `TBOX_OFFLINE_AFTER_S`（默认 90 秒）。

---

### 5.2 `POST /admin/command`

见 §4.1。

---

### 5.3 `GET /admin/reports?terminal_id=<tid>`

列出某终端的所有 report 类型。

响应 200：

```json
{
  "status": "ok",
  "terminal_id": "TBOX-0001",
  "reports": [
    {
      "type": "version",
      "latest_path": ".../reports/TBOX-0001/version/latest.json",
      "latest_mtime": 1731628801.5,
      "history_count": 5
    }
  ]
}
```

---

### 5.4 `GET /admin/report?terminal_id=<tid>&type=<type>`

读某 type 的最新上报。

响应 200：

```json
{
  "status": "ok",
  "terminal_id": "TBOX-0001",
  "type": "version",
  "payload": { "...": "服务端原样返回上报时的 data 字段" }
}
```

响应 404：未找到该 report。

---

### 5.5 `GET /healthz`

健康检查。

响应 200：

```json
{ "status": "ok", "server_version": "0.1.0", "version": "0.1.0" }
```

### 5.6 `GET /api/version`

返回服务器自身版本（同 `/healthz` 里的 `server_version`）。

---

## 6. 完整时序图

### 6.1 终端启动 + 注册

```
终端                 服务器
 │                    │
 │── POST /heartbeat ─►│   (注册 terminal_id, fw_version, caps)
 │◄──── 200 ok ───────│   (返回 server_time, 建议心跳/长轮询间隔)
 │                    │
 │── GET /poll ──────►│   (X-Terminal-Id: TBOX-0001)
 │◄──── 200 idle ─────│   (无命令，30s 后)
 │                    │
 │── POST /heartbeat ─►│   (30s 后再次心跳)
 │◄──── 200 ok ───────│
 │                    │
 │── GET /poll ──────►│   (接着挂长轮询)
 │   (挂起中...)       │
```

### 6.2 管理员下发命令 + 终端执行

```
管理员        终端                服务器
 │              │                    │
 │              │── GET /poll ──────►│  (挂起等待)
 │              │                    │
 │── POST /admin/command ───────────►│  (入队)
 │◄── 200 ok {cmd_id} ─────────────│
 │              │                    │
 │              │◄── 200 command ────│  (立刻唤醒 poll，< 1s)
 │              │                    │
 │              │  执行命令逻辑       │
 │              │                    │
 │              │── POST /command/ack ► (回执)
 │              │◄── 200 ok ─────────│
```

### 6.3 终端断网 → 恢复 → 命令投递

```
终端                  服务器
 │                     │
 │── POST /heartbeat ─►│  last_seen = T0
 │── GET /poll ───────►│  (挂起)
 │                     │
 ▼  网络断开            │
                  (后台 reaper 每 10s 检查)
                  90s 后: online=false, pending=1 (命令在队列里)
                       │
 ▼  网络恢复            │
 │                     │
 │── POST /heartbeat ─►│  last_seen = T1, online=true
 │── GET /poll ───────►│
 │◄── 200 command ─────│  之前积压的命令立刻投递（不丢）
```

### 6.4 客户端中途断开长轮询

```
终端                  服务器
 │                     │
 │── GET /poll ───────►│  (挂起等待)
 │                     │
 │                     │  admin/command 入队
 │                     │  从 queue.get() 拿到命令
 │  准备 send 命令给客户端
 │                     │
 ▼  客户端断连          │
                  asyncio.CancelledError
                  把命令放回 queue 头部
                       │
 ▼  客户端重连          │
 │── GET /poll ───────►│
 │◄── 200 command ─────│  上次没发出去的那条立刻到手
```

---

## 7. 状态机

### 7.1 终端在线状态

```
         last_seen + heartbeat_interval*3
   online ─────────────────────────────► offline
     ▲                                       │
     │   POST /heartbeat                      │
     └───────────────────────────────────────┘
```

- `last_seen > 0` 且 `now - last_seen ≤ TBOX_OFFLINE_AFTER_S` ⇒ `online`
- 否则 `offline`（但**会话记录保留**，命令队列保留）

### 7.2 命令生命周期

```
            POST /admin/command
   无 ────────────────────────────►  queued
                                       │
                  ┌────────────────────┼─────────────────────┐
                  │                    │                     │
       (终端 poll 拿到)         (reaper 检查 ttl)      (服务器重启)
                  │                    │                     │
                  ▼                    ▼                     ▼
              delivered           expired              lost (in-memory only)
                  │
                  ▼
           (执行命令)
                  │
                  ▼
            POST /command/ack
                  │
                  ▼
              acked (in history, last 50/终端)
```

---

## 8. 存储布局（文件系统视角）

```
data/
├── server.pid                       当前服务器 PID
├── server.log                       服务器日志（access + error）
├── uploads/YYYY-MM-DD/<tid>/
│   ├── <sha-prefix>__<safe_name>    上传的文件
│   └── <…>.meta.json                原始 metadata
├── logs/YYYY-MM-DD/<tid>/
│   ├── <unix_ts>_<seq>.log          上传的日志
│   └── <…>.meta.json
├── versions/<channel>/latest.json   固件版本清单（运维手改）
└── reports/<tid>/<type>/
    ├── latest.json                  最近一次（覆盖式）
    └── history/<ts>[-rand].json     历史归档（追加式）
```

---

## 9. 资源限制

| 项 | 环境变量 | 默认 |
|---|---|---|
| 单文件上传上限 | `TBOX_MAX_UPLOAD_BYTES` | 64 MiB |
| 单日志 blob 上限 | `TBOX_MAX_LOG_BYTES` | 4 MiB |
| `/poll` 默认挂起时长 | `TBOX_LONG_POLL_TIMEOUT_S` | 30 s |
| `/poll` 单次最长挂起时长 | `TBOX_LONG_POLL_MAX_TIMEOUT_S` | 120 s |
| 命令默认 TTL | `TBOX_DEFAULT_COMMAND_TTL_S` | 3600 s |
| 单终端最大待发命令数 | `TBOX_MAX_PENDING_COMMANDS_PER_TERMINAL` | 32 |
| 终端多久没心跳算 offline | `TBOX_OFFLINE_AFTER_S` | 90 s |
| Reaper 检查间隔 | `TBOX_REAPER_INTERVAL_S` | 10 s |
| **终端上传数据保留时长** | `TBOX_DATA_RETENTION_S` | `86400` (24 h) |
| **后台清理间隔** | `TBOX_DATA_CLEANUP_INTERVAL_S` | `3600` (1 h) |

### 数据保留策略

服务端**自动**清理超过 `TBOX_DATA_RETENTION_S` 秒的终端上传数据：

| 清理 | 不清理 |
|---|---|
| `data/uploads/YYYY-MM-DD/`（整个日期目录） | `data/versions/`（服务端配置） |
| `data/logs/YYYY-MM-DD/`（整个日期目录） | `data/reports/<tid>/<type>/latest.json`（最新一条永远保留） |
| `data/reports/<tid>/<type>/history/<ts>.json`（按文件 mtime） | |

`TBOX_DATA_RETENTION_S=0` 禁用清理。

手动触发或查看：

```bash
tboxctl admin disk          # 看各目录占用
tboxctl admin cleanup       # 立刻扫一遍（用配置的 retention）
tboxctl admin cleanup --older-than-s 3600   # 临时改阈值
```

---

## 10. 客户端实现建议

### 10.1 重连策略

```
循环：
    1. try POST /heartbeat
       失败 → sleep(5s) 重试，不退出
    2. try GET /poll?wait=25
       超时 → 回到 1
       收到 command → 执行
       收到 idle → 回到 1
       异常 → sleep(2s) 回到 1
```

### 10.2 时钟校正

```python
local_ts = time.time()
resp = http.post("/heartbeat", json={..., "ts": local_ts})
server_time = resp["server_time"]
offset = server_time - local_ts
# 以后所有 ts = local_ts + offset
```

### 10.3 文件上传的 sha 计算

```python
sha = hashlib.sha256()
size = 0
with open(path, "rb") as f:
    while chunk := f.read(64 * 1024):
        sha.update(chunk)
        size += len(chunk)
# 把 sha.hexdigest() 和 size 填进 metadata
# 然后 multipart 上传，file part 直接传 path，不要先读进内存
```

### 10.4 长轮询的客户端实现要点

| 框架 | 注意点 |
|---|---|
| `curl` | 加 `-N --no-buffer`，不然响应会被缓冲到结束 |
| Python `requests` | 用 `requests.get(..., stream=True)` 然后 `.iter_content()` |
| Python `aiohttp` | `async with session.get(...) as r: r.content.read()` 即可，不要用 `await r.read()` |
| Go `net/http` | 用 `client.Do(req)` + 手动 `io.Copy(os.Stdout, resp.Body)` |
| C/libcurl | `CURLOPT_BUFFERSIZE=1` 或读流 |

### 10.5 错误码速查

| 错误 | 客户端应做 |
|---|---|
| `400` | **不要无限重试**；是请求本身的问题，重试也白搭 |
| `404` | 路径写错了或资源不存在；通常需要 SDK 升级 |
| `413` | 文件太大；先压缩或分块，或让用户调大服务器上限 |
| `415` | Content-Type 错了 |
| `429`（目前未实现） | 频率限制 |
| `500/503` | 网络/服务器问题，可以重试，但要带 backoff |

---

## 11. 协议版本

当前协议版本：**v1**（伴随 `tbox-server 0.1.0`）。

未来兼容性承诺：

- **新增字段**：服务端忽略客户端没传的字段；客户端忽略服务端没回的字段 → 兼容
- **新增端点**：客户端可以无视 → 兼容
- **删除/重命名字段**：不兼容，需要协议版本号升级

---

## 12. 完整示例（用 curl 模拟一个终端的完整生命周期）

```bash
SERVER=http://your-server:9999
TID=TBOX-DEMO

# 1. 开机，注册心跳
curl -s -X POST $SERVER/heartbeat -H 'content-type: application/json' -d "{
  \"terminal_id\":\"$TID\",
  \"fw_version\":\"1.0.0\",
  \"ts\":$(date +%s),
  \"address\":\"192.168.1.100\",
  \"caps\":[\"gps\",\"can\"],
  \"free_disk_mb\":1024
}"

# 2. 查询最新版本
curl -s "$SERVER/version?channel=stable"

# 3. 上报自己的版本（任意 JSON 结构）
curl -s -X POST $SERVER/report -H 'content-type: application/json' -d "{
  \"terminal_id\":\"$TID\",
  \"report_type\":\"version\",
  \"data\":{\"fw\":\"1.0.0\",\"build\":\"abc123\"}
}"

# 4. 长轮询取命令（前台挂着）
curl -N "$SERVER/poll?wait=30" -H "X-Terminal-Id: $TID"

# 5. 另一个终端：下发命令给 $TID
curl -s -X POST $SERVER/admin/command -H 'content-type: application/json' -d "{
  \"terminal_id\":\"$TID\",
  \"type\":\"reboot\",
  \"payload\":{\"delay_s\":5}
}"

# 6. 终端收到命令后回执
curl -s -X POST $SERVER/command/ack -H 'content-type: application/json' -d "{
  \"terminal_id\":\"$TID\",
  \"cmd_id\":\"<从 /poll 拿到的 cmd_id>\",
  \"status\":\"ok\",
  \"result\":{},
  \"ts\":$(date +%s)
}"

# 7. 上传一个文件
echo "diagnostic data" > /tmp/diag.bin
SHA=$(sha256sum /tmp/diag.bin | awk '{print $1}')
SIZE=$(stat -c%s /tmp/diag.bin)
curl -X POST $SERVER/upload \
  -F "metadata={\"terminal_id\":\"$TID\",\"sha256\":\"$SHA\",\"size\":$SIZE,\"kind\":\"diagnostics\"};type=application/json" \
  -F "file=@/tmp/diag.bin"
```

---

## 13. 附录：JSON Schema 摘要

所有 `ts` 字段类型为 `number`（UNIX 秒，浮点）。
所有 `terminal_id` 字段类型为 `string`（1-64 字符，非空）。

完整 JSON Schema 见 `docs/protocol.schema.json`（TODO：可由 `tboxctl --schema` 输出）。
