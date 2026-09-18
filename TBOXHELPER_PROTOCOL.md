
# tboxhelper ↔ tbox-server 通信协议 v1

## 1. 概述

本文档定义 tboxhelper（TBOX 端守护进程）与 tbox-server（云端 HTTP 服务器）之间的完整通信协议。

| 项 | 取值 |
|---|---|
| 传输层 | HTTP/1.1，JSON UTF-8 |
| 默认端口 | 9999 |
| 终端标识 | `terminal_id`：字符串，默认自动读取设备 SN，最长 64 字符 |
| 时间格式 | UNIX 秒，请求中的 `timestamp` 字段 |

---

## 2. 协议总览

### 2.1 端点一览

| 方向 | 方法 | 路径 | 用途 |
|------|------|------|------|
| 终端→服务器 | POST | `/heartbeat` | 设备注册 + 周期性保活 |
| 终端→服务器 | GET | `/poll?wait=N` | 长轮询取命令 |
| 终端→服务器 | POST | `/command/ack` | 命令执行确认 |
| 终端→服务器 | POST | `/upload` | 文件上传（命令内部调用，见 §4.2） |
| 终端→服务器 | POST | `/report` | 周期状态上报（服务器需解析） |

### 2.2 通用响应格式

**成功：**
```json
{"status": "ok", ...}
```

**失败：**
```json
{"status": "error", "message": "描述信息"}
```

失败时使用标准 HTTP 状态码：

| HTTP 状态码 | 含义 | 使用场景 |
|------------|------|---------|
| 400 | Bad Request | 请求体格式错误 |
| 404 | Not Found | 终端不存在、cmd_id 不存在 |
| 413 | Payload Too Large | 文件超过大小上限 |
| 415 | Unsupported Media Type | 非 multipart 请求 |
| 500 | Internal Server Error | 服务器内部错误 |

---

## 3. 上行协议（终端 → 服务器）

### 3.1 POST /heartbeat — 心跳注册

设备启动时注册，后续按服务器指定的间隔周期性发送。

**请求：**
```json
{
    "terminal_id": "<SN 或用户指定>",
    "service": "tboxhelper"
}
```

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `terminal_id` | ✅ | string | 终端唯一标识 |
| `service` | | string | 服务名，标识来源模块 |

**响应 200：**
```json
{
    "status": "ok",
    "timestamp": 1731628801,
    "heartbeat_interval_s": 30,
    "long_poll_timeout_s": 30
}
```

| 字段 | 说明 |
|------|------|
| `heartbeat_interval_s` | 建议的心跳间隔（秒），客户端应遵从此值 |
| `long_poll_timeout_s` | 建议的长轮询超时（秒） |

---

### 3.2 GET /poll — 长轮询取命令

客户端挂起一个 GET 请求，服务端有命令时立即返回，无命令时等待超时返回 idle。

**请求：**
```
GET /poll?wait=30
X-Terminal-Id: <terminal_id>
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `wait` | 30 | 最长挂起秒数，服务器上限 120s |
| `X-Terminal-Id` (Header) | — | 终端标识 |

**无命令响应（超时）：**
```json
{"status": "idle", "timestamp": 1731628830}
```

**有命令响应：**
```json
{
    "status": "command",
    "cmd": {
        "cmd_id": "f1196139f0ba41539fa2d18f67785994",
        "type": "upload",
        "payload": { ... },
        "issued_at": 1731628830
    }
}
```

| cmd 字段 | 说明 |
|----------|------|
| `cmd_id` | 命令唯一 ID，ack 时需回传 |
| `type` | 命令类型：`upload`（上传文件）、`exec`（执行 shell 命令） |
| `payload` | 命令参数（按 type 定义，见 §4） |
| `issued_at` | 下发时间戳 |

**客户端行为：**
- 收到 `idle` → 立即重新发起轮询
- 收到 `command` → 解析 type 路由到对应 handler
- HTTP 异常/超时 → sleep 2 秒后重新轮询

**错误响应（404）：**
```json
{"status": "error", "message": "terminal not found"}
```

---

### 3.3 POST /command/ack — 命令确认

命令执行完成后，向服务器回复执行结果。

**请求：**
```json
{
    "terminal_id": "<SN>",
    "cmd_id": "f1196139f0ba41539fa2d18f67785994",
    "status": "ok",
    "result": {
        "code": 0,
        "output": "..."
    },
    "timestamp": 1731628900
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `cmd_id` | ✅ | 来自 `/poll` 响应的 `cmd.cmd_id` |
| `status` | ✅ | `"ok"` / `"fail"` |
| `result` | | 任意 JSON object，命令执行结果 |
| `timestamp` | | 执行完成时间戳 |

**result 字段错误码枚举：**

| code | 含义 | 场景 |
|------|------|------|
| `0` | 执行成功 | 命令执行成功 |
| `8001` | 文件不存在 | upload file_type=other 找不到文件 |
| `8002` | 文件读取失败 | upload 读取文件 I/O 错误 |
| `8003` | 无匹配日志文件 | upload file_type=log 无文件 |
| `8004` | 压缩失败 | upload tar czf 出错 |
| `8005` | SHA256 计算失败 | upload sha256sum 出错 |
| `8006` | 上传请求失败 | upload POST /upload 返回错误 |
| `8101` | 命令执行超时 | exec 超过 timeout |
| `8102` | 命令执行失败 | exec 进程退出码非 0 |
| `8999` | 未知错误 | 其他异常 |

`code` 为错误码（0 成功，非 0 失败），`output` 存放标准输出内容或错误描述。

> 编码规则：`0` = 成功，`8xxx` = 应用层错误（`800x`=upload 相关，`810x`=exec 相关，`8999`=兜底）

**响应 200：**
```json
{"status": "ok"}
```

**错误响应：**
```json
{"status": "error", "cmd_id": "f1196139f0ba41539fa2d18f67785994", "message": "cmd_id not found"}
```

---

### 3.4 POST /report — 周期状态上报 ⭐ 服务器侧需解析

tboxhelper 每 10 秒上报一次设备状态。服务器需解析 `data` 字段并存储/展示。

**请求：** `application/json`

```json
{
    "terminal_id": "<SN>",
    "service": "tboxhelper",
    "report_type": "status",
    "timestamp": 1731628800,
    "data": {
        "position": {
            "lat": 22.543,
            "lon": 114.057,
            "alt": 10.5,
            "speed": 0.0,
            "track": 120.5,
            "status": 1,
            "mode": 3
        },
        "iccid": "89860123456789012345",
        "imei": "866987123456789",
        "imsi": "460011234567890",
        "vin": "LVX12345678901234",
        "sn": "TBOX-SN-2026-00001",
        "vehicle_model": "JMC-CX835",
        "dtc": [
            "B20413", "B20415"
        ]
    }
}
```

#### data 字段定义

##### position（定位信息，可选）

| 字段 | 类型 | 说明 |
|------|------|------|
| `lat` | number | 纬度（度），如 22.543 |
| `lon` | number | 经度（度），如 114.057 |
| `alt` | number | 海拔（米） |
| `speed` | number | 速度（m/s） |
| `track` | number | 航向（度，0~360） |
| `status` | int | 0=无定位，1=已定位 |
| `mode` | int | 0=无，1=未定位，2=2D，3=3D |

##### 设备信息（必填）

| 字段 | 类型 | 说明 |
|------|------|------|
| `iccid` | string | SIM 卡 ICCID（20 位数字） |
| `imei` | string | 设备 IMEI（15 位数字） |
| `imsi` | string | 设备 IMSI（15 位数字） |
| `vin` | string | 车辆 VIN（17 位字母数字） |
| `sn` | string | TBOX 序列号 |
| `vehicle_model` | string | 车型 |

##### DTC 故障码（可选）

| 字段 | 类型 | 说明 |
|------|------|------|
| `dtc` | array[string] | DTC 故障码列表，如 `["B20413", "B20415"]`，有则上报，无则省略 |

**存储路径（tbox-server）：** `data/reports/<terminal_id>/status/latest.json`

**查看方式：**
```bash
tboxctl admin reports <tid>       # 列出上报类型
tboxctl admin show <tid> status   # 查看最新 status 内容
```

---

## 4. 下行协议（服务器 → 终端）

### 4.1 命令通用格式

命令的生命周期：

```
管理员                         服务器                        终端
  │                             │                            │
  │── POST /admin/command ─────►│                            │
  │   {terminal_id, type,       │  存入目标终端命令队列        │
  │    payload}                 │                            │
  │◄── {status, cmd_id} ───────│                            │
  │                             │                            │
  │                             │◄── GET /poll?wait=30 ──────│
  │                             │  X-Terminal-Id: <SN>       │
  │                             │── {status:"command",cmd} ──►│
  │                             │                            │
  │                             │◄── POST /command/ack ──────│
  │                             │── {status:"ok"} ─────────►│
```

**管理员投递命令：**

```
POST /admin/command
Content-Type: application/json

{
    "terminal_id": "<SN>",
    "type": "upload",
    "payload": { "file_type": "log" }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `terminal_id` | ✅ | 目标终端 SN |
| `type` | ✅ | `upload` 或 `exec` |
| `payload` | ✅ | 命令参数，按 type 定义（见 §4.2、§4.3） |

**响应 200：**
```json
{"status": "ok", "cmd_id": "f1196139f0ba41539fa2d18f67785994"}
```

**错误响应（404）：**
```json
{"status": "error", "message": "terminal not found"}
```

**服务端行为：**
- 收到投递后，为目标终端生成唯一 `cmd_id`
- 将命令存入该终端的待执行队列（FIFO）
- 终端下次 `GET /poll` 时从队列取出返回
- `cmd_id` 可用于后续追踪 ack

**终端通过 poll 接收到的命令格式：**
```json
{
    "status": "command",
    "cmd": {
        "cmd_id": "f1196139f0ba41539fa2d18f67785994",
        "type": "upload",
        "payload": { ... },
        "issued_at": 1731628830
    }
}
```

### 4.2 upload — 上传文件

通过 `file_type` 区分两种模式。

#### file_type=log（收集 message 上传）

**payload：**
```json
{
    "file_type": "log",
    "start_time": "2026-09-15 08:00",
    "end_time": "2026-09-16 08:00"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `file_type` | ✅ | `"log"` |
| `start_time` | | 起始时间 `"YYYY-MM-DD HH:MM"`，不传时不限制起始 |
| `end_time` | | 结束时间 `"YYYY-MM-DD HH:MM"`，不传时不限制结束 |

#### file_type=other（上传指定路径文件）

**payload：**
```json
{
    "file_type": "other",
    "path": "/data/logs/xxx.tar.gz"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `file_type` | ✅ | `"other"` |
| `path` | ✅ | 要上传的文件绝对路径 |

#### 客户端处理流程

```
命令下发 (poll)              ← 服务器 → 终端
  │ type: "upload", payload: { file_type, ... }
  ▼
终端准备文件
  ├── file_type=log    → 收集 syslog → tar czf 压缩
  └── file_type=other  → 读取 payload.path 文件
  ▼
POST /upload (multipart)    ← 终端 → 服务器（见下方完整 HTTP 请求）
  ├── Part: metadata (JSON)  ← 终端信息 + 文件描述
  └── Part: file (binary)   ← 文件原始二进制
  ▼
POST /command/ack            ← 终端 → 服务器，返回结果
  └── { code: 0 } 或 { code: -1, output: "..." }
```

**POST /upload 完整 HTTP 请求示例（终端→服务器）：**

```
POST /upload HTTP/1.1
Host: <server>:9999
Content-Type: multipart/form-data; boundary=----BOUNDARY

------BOUNDARY
Content-Disposition: form-data; name="metadata"
Content-Type: application/json

{"terminal_id":"<SN>","cmd_id":"f1196139f0ba41539fa2d18f67785994","sha256":"ab12...","size":1234567}
------BOUNDARY
Content-Disposition: form-data; name="file"; filename="archive.tar.gz"
Content-Type: application/octet-stream

<文件原始二进制数据>
------BOUNDARY--
```

> `----BOUNDARY` 为示例分隔符，实际传输时由客户端随机生成。服务器应从 `Content-Type` 的 `boundary=` 参数中读取。

| Part 字段名 | Content-Type | 内容 |
|-------------|-------------|------|
| `metadata` | application/json | 文件描述的 JSON 字符串（字段见下方） |
| `file` | application/octet-stream | 待上传文件的原始二进制数据 |

**metadata JSON 字段**

```json
{
    "terminal_id": "<SN>",
    "cmd_id": "f1196139f0ba41539fa2d18f67785994",
    "sha256": "ab12...",
    "size": 1234567
}
```

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `terminal_id` | ✅ | string | 终端 SN |
| `cmd_id` | ✅ | string | 触发本次上传的命令 ID，服务端用于关联 ack |
| `sha256` | | string | 文件 SHA-256 校验值（64 位十六进制），非空时服务器应校验 |
| `size` | | number | 文件大小（字节），非零时服务器应校验 |

**服务器处理要求：**
- 解析 multipart/form-data，提取 `metadata`（JSON）和 `file`（二进制）
- 校验 `metadata.sha256` 与文件实际 SHA-256 是否匹配（非空时）
- 校验 `metadata.size` 与文件实际大小是否匹配（非零时）
- 存储文件到服务器本地路径
- 响应使用通用格式（见 §2.2）

**响应 200（服务器 → 终端，HTTP 请求成功）：**

上传校验通过：
```json
{"status": "ok"}
```

上传校验失败（如 sha256 不匹配）：
```json
{"status": "error", "cmd_id": "f1196139f0ba41539fa2d18f67785994", "message": "sha256 mismatch"}
```

**错误码：** 400（sha256/size 不匹配）、413（超大小上限）、415（非 multipart）

上传完成后，终端通过 `POST /command/ack`（见 §3.3）返回结果，`result` 字段内容：

- 成功：`{"code": 0}`
- 失败：`{"code": -1, "output": "错误描述"}`

---

### 4.3 exec — 执行 shell 命令

**payload：**
```json
{
    "command": "ls -la /var/log",
    "timeout": 30
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `command` | ✅ | 要执行的 shell 命令 |
| `timeout` | | 超时秒数，默认 30 |

**客户端行为：**
1. `popen(cmd)` 执行，捕获 stdout
2. 获取退出码
3. 结果通过 `POST /command/ack`（见 §3.3）返回，`result` 字段内容：

```json
{
    "code": 0,
    "output": "total 24\ndrwxr-xr-x 2 root root 4096 Sep 15 08:00 ..."
}
```



## 5. 客户端实现建议

### 5.1 重连策略

```
循环：
  1. POST /heartbeat
     失败 → sleep(5s) 重试，指数退避（1s,2s,4s...60s）
  2. GET /poll?wait=30
     收到 command → 执行 → ack → 回到 1
     收到 idle    → 回到 1
     异常/超时    → sleep(2s) → 回到 1
```

### 5.2 周期上报

```
每 10 秒：
  1. 调用 CLI 工具（gps-assist、print_baseinfo、diag-terminal 等）
  2. 组装 JSON
  3. POST /report
  4. 失败跳过，下次继续
```

### 5.3 文件上传 SHA-256

```
tar czf → sha256sum <file> → 提取前 64 字符
放入 metadata.sha256，服务器校验
```

---

## 6. 完整调用示例

### 6.1 终端启动 + 心跳

```
TBOX                            服务器
 │                                │
 │── POST /heartbeat ────────────►│
 │   {terminal_id}               │
 │◄── 200 OK ────────────────────│
 │   {heartbeat_interval_s:30}   │
 │                                │
 │── GET /poll?wait=30 ─────────►│
 │◄── 200 idle ──────────────────│  (无命令，30s 后)
 │                                │
 │── POST /heartbeat ────────────►│  (30s 后)
 │── POST /report ───────────────►│  (10s 后)
 │   {report_type:"status",data} │
```

### 6.2 管理员下发 upload（file_type=log）

```
管理员          TBOX            服务器
 │              │                 │
 │              │── GET /poll ───►│  (挂起)
 │              │                 │
 │── tboxctl admin push ────────►│
 │   <SN> upload                  │
 │   --payload file_type=log     │
 │   --payload start_time="2026-09-15"│
 │◄── ✓ queued ──────────────────│
 │              │                 │
 │              │◄── command ─────│  (poll 返回)
 │              │   upload        │
 │              │   file_type=log │
 │              │                 │
 │              │ 收集 messages*  │
 │              │ 按时间范围过滤   │
 │              │ tar czf 压缩    │
 │              │                 │
 │              │── POST /upload ►│
 │              │◄── 200 OK ──────│
 │              │                 │
 │              │── POST /ack ───►│
 │◄── 收到      │◄── 200 OK ─────│
```

### 6.3 管理员下发 upload（file_type=other）

```
管理员          TBOX            服务器
 │              │                 │
 │── tboxctl admin push ────────►│
 │   <SN> upload                  │
 │   --payload file_type=other   │
 │   --payload path=/data/dump.bin│
 │◄── ✓ queued ──────────────────│
 │              │◄── command ─────│
 │              │                 │
 │              │ 读取 path 文件  │
 │              │                 │
 │              │── POST /upload ►│
 │◄── 收到      │◄── 200 OK ─────│
```

---

## 7. 修订记录

| 日期 | 版本 | 修改内容 |
|------|------|---------|
| 2026-09-16 | v1 | 初始版本，定义全部端点、命令类型、周期上报格式 |
| 2026-09-17 | v2 | 精简协议：删 fw_version/server_version/ttl_s/event；命令统一为 upload（file_type=log/other）；删 reboot/fetch_diagnostics/version/logs_upload；恢复 exec；周期上报加 dtc，频率 10s |
