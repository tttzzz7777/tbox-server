# tboxctl 使用手册

这份手册汇总 `tboxctl` 的所有指令和常用场景，供日常查阅。

> 前提：`.bashrc` 里已经加过 `TBOX_HOME=/root/tbox-server` 并把它的 `.venv/bin` 和
> 项目根目录加到了 `PATH` 最前面。新开终端直接生效，无需手动配置。
>
> 协议规范以 `TBOXHELPER_PROTOCOL.md`（v2）为准。

---

## 1. 速查表

| 命令 | 作用 |
|---|---|
| `tboxctl start -d` | 后台启动服务器 |
| `tboxctl start` | 前台启动（Ctrl-C 停止，方便调试） |
| `tboxctl stop` | 优雅停止（SIGTERM，8s 后 SIGKILL） |
| `tboxctl restart -d` | 重启 |
| `tboxctl status` | 看 PID / 运行时长 / 端口 / 健康 / 终端数 |
| `tboxctl logs -n 100` | 看最近 100 行日志 |
| `tboxctl logs -f` | 跟踪日志（tail -f） |
| `tboxctl admin list` | 列出所有终端 |
| `tboxctl admin push <tid> upload\|exec` | 给某个终端下发命令 |
| `tboxctl admin reports <tid>` | 列出某终端的 status 上报 |
| `tboxctl admin show <tid> [status]` | 打印某终端最新 status 上报的 JSON 内容 |

> 全局参数：`--host <ip>`（默认 `159.75.187.190`）、`--port <n>`（默认 `9999`）。
> 用于 `start` 时改绑定地址/端口；用于 admin 子命令时改目标服务器地址。

---

## 2. 服务生命周期

### 启动

```bash
tboxctl start -d           # 后台启动，默认 159.75.187.190:9999
tboxctl start -d --port 9000
tboxctl start              # 前台运行，日志直接打到终端，Ctrl-C 停止
```

启动成功的输出：
```
==> starting server detached: http://159.75.187.190:9999 log=…/data/server.log
✓ started pid=2180207 http://159.75.187.190:9999
==>   log: …/data/server.log
==>   pid file: …/data/server.pid
```

启动失败的常见原因：
- **端口被占**：先 `lsof -iTCP:9999 -sTCP:LISTEN` 看看是谁；杀掉占用进程或换端口
- **已有一个 tbox-server 在跑**：`tboxctl stop` 后再启动
- **PID 文件残留**：通常是上次异常退出导致，启动脚本会自动检测并清理

### 停止 / 重启

```bash
tboxctl stop                    # 默认 8s 宽限期，超时 SIGKILL
tboxctl stop --timeout 2        # 最多等 2 秒
tboxctl restart -d              # 停掉再起
tboxctl restart                 # 前台重启（一般不会这么用）
```

### 状态

```bash
tboxctl status
```

输出示例：
```
tbox-server status
  pid          : 2180207
  uptime       : 1h 23m 4s
  bind         : http://159.75.187.190:9999
  port open    : True
  log file     : …/data/server.log
  pid file     : …/data/server.pid
  data dir     : …/data
==>   health       : ok (server_version=0.1.0)
==>   terminals    : 7 known, 4 online
```

退出码：
- `0`：正常运行
- `3`：未运行
- `4`：进程在跑但 HTTP 不通（多半要重启）

---

## 3. 日志

```bash
tboxctl logs -n 100             # 最近 100 行
tboxctl logs -n 1000 -f         # 先打印最近 1000 行，再继续跟踪
tboxctl logs -f                 # 等价 tail -F
```

日志文件位置：`/root/tbox-server/data/server.log`。每行带访问日志（请求 ID、
状态码、耗时），错误会打 stacktrace。

> 日志不会自动轮转。长期运行建议加 `logrotate`，或自己写个 cron。

---

## 4. admin 子命令

### 4.1 `admin list` —— 列出终端

```bash
tboxctl admin list
```

```
TERMINAL               STATE   LAST_SEEN   SERVICE         PENDING
----------------------------------------------------------------------
TBOX-001               online  17:33:58    tboxhelper      0
TBOX-002               offline 16:01:12    tboxhelper      1
```

- `STATE` 颜色：绿色=online，黄色=offline
- `LAST_SEEN`：今天的 `HH:MM:SS`，跨天会显示昨天的日期
- `SERVICE`：终端自己填的来源模块名（v2 心跳字段）
- `PENDING`：还没被终端通过 `/poll` 拉走的命令数

### 4.2 `admin push` —— 下发命令

```bash
tboxctl admin push <terminal_id> <type> [--payload k=v ... | --payload-json '<json>']
```

支持的 `type`：

| type | 用途 | payload 例子 |
|---|---|---|
| `upload` | 终端采集文件后通过 `/upload` 上传 | `file_type=log` 或 `file_type=other` + `path=...` |
| `exec` | 终端执行一次性 shell 命令 | `command=ls -la` + `timeout=30` |

`--payload` 重复使用可一次写多个键（值会被自动按 int/float/bool/str 推断）。
复杂结构用 `--payload-json '<json>'`，可与 `--payload` 混用（json 合并覆盖）。

示例：
```bash
# 让终端收集日志后上传（终端会按内置时间窗口抓 messages* 然后 tar 上传）
tboxctl admin push TBOX-001 upload --payload file_type=log

# 让终端上传指定路径的文件
tboxctl admin push TBOX-001 upload \
    --payload-json '{"file_type":"other","path":"/data/dump.bin"}'

# 限制 log 收集时间窗口
tboxctl admin push TBOX-001 upload \
    --payload-json '{"file_type":"log","start_time":"2026-09-15 08:00","end_time":"2026-09-16 08:00"}'

# 执行 shell 命令
tboxctl admin push TBOX-001 exec \
    --payload-json '{"command":"ls /var/log","timeout":30}'
```

`admin push` 返回时拿到 `cmd_id`，可以用 `simulator`/`curl` 走
`POST /command/ack` 上报执行结果。命令本身的 `cmd_id` 也会被终端在
`POST /upload` 的 metadata 里带回，用来关联 ack。

### 4.3 `admin reports` —— 列出某终端的 status 上报

终端每 10 秒通过 `POST /report` 上报一次设备状态（位置/ICCID/IMEI/VIN/...）。
服务器存到 `data/reports/<tid>/status/latest.json`，每次覆盖。

```bash
tboxctl admin reports TBOX-001
```

输出：
```
TYPE      LATEST_MTIME                 PATH
----------------------------------------------------------
status    2026-09-17 18:41:22         /root/tbox-server/data/reports/TBOX-001/status/latest.json
```

- 没有 status 上报时打印 `no reports for <tid>`
- v2 不再保留历史，每次上报直接覆盖 `latest.json`

### 4.4 `admin show` —— 打印某终端的最新 status 上报

把 `latest.json` 的内容原样打到终端（保留缩进、中文、嵌套结构）：

```bash
tboxctl admin show TBOX-001           # 默认 type=status
tboxctl admin show TBOX-001 status
```

示例输出：

```json
{
  "iccid": "89860123456789012345",
  "imei": "866987123456789",
  "vin": "LVX12345678901234",
  "sn": "TBOX-001",
  "vehicle_model": "JMC-CX835",
  "position": {"lat": 22.5, "lon": 114.0, "status": 1, "mode": 3},
  "dtc": ["B20413"]
}
```

不存在时返回 404 + 友好提示。

### 4.5 终端怎么上报 status（curl 例子）

```bash
curl -X POST http://SERVER/report -H 'content-type: application/json' \
  -d '{
    "terminal_id":"TBOX-001",
    "service":"tboxhelper",
    "report_type":"status",
    "timestamp":1731628800,
    "data":{
      "iccid":"89860123456789012345",
      "imei":"866987123456789",
      "vin":"LVX12345678901234",
      "sn":"TBOX-001",
      "vehicle_model":"JMC-CX835",
      "position":{"lat":22.5,"lon":114.0,"status":1,"mode":3},
      "dtc":["B20413"]
    }
  }'
```

返回：
```json
{
  "status":"ok",
  "stored":"data/reports/TBOX-001/status/latest.json",
  "bytes":307,
  "report_type":"status"
}
```

---

## 5. 常用场景

### 5.1 启动 + 跑模拟器 + 下发命令

```bash
# 终端 1
tboxctl start -d

# 终端 2
cd /root/tbox-server
.venv/bin/python simulator/terminal_sim.py     # 模拟器会一直跑，每 10s 上报一次 status

# 终端 3
tboxctl admin list                              # 看到模拟器在线
tboxctl admin push TBOX-0001 exec --payload-json '{"command":"uname -a"}'
# 切回终端 2：模拟器会打印 "received command" 并回 ack
tboxctl logs -f | grep 'POST /command/ack'      # 确认 ack 到了
```

### 5.2 排查某个终端没回命令

```bash
tboxctl admin list                              # 看 PENDING 是不是 >0 且越积越多
tboxctl logs -n 200 | grep -i 'TBOX-001'        # 看跟它相关的访问日志
# 通常是终端没在跑 / 网络断了 / 命令被 reaper 清掉
```

### 5.3 服务器卡死 / 不响应

```bash
tboxctl status                                  # 看 health 字段
# 如果 health unreachable（退出码 4）：
tboxctl logs -n 50                              # 看最近报错
tboxctl restart -d                              # 简单粗暴的重启
# 还不行就 stop + 手动 ps + kill -9 + start
```

### 5.4 改服务器端口/地址（不污染数据）

```bash
tboxctl stop
tboxctl start -d --port 9000
tboxctl --port 9000 admin list
```

### 5.5 查看上传的文件

```bash
find /root/tbox-server/data/uploads -type f | tail      # 最近上传的文件
ls -lh /root/tbox-server/data/uploads/$(date -u +%F)/*  # 今天的
cat /root/tbox-server/data/uploads/.../xxx.meta.json     # 看原始 metadata（含 cmd_id）
```

---

## 6. 故障排查速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `✗ server already running (pid=…)` | 已经在跑 | `tboxctl stop` 再启 |
| `✗ port 9999 on 159.75.187.190 is already in use` | 其他进程占了端口 | `lsof -iTCP:9999` 找占用方；或换端口 |
| `! stale pid file (pid=999999 not alive)` | 上次异常退出 | 已自动清理，直接重新 `start -d` |
| `✗ cannot reach http://…: Connection refused` | 服务没起来 | `tboxctl status` 看 PID/退出码 |
| `tboxctl --help` 没反应 | PATH 没生效 | 新开终端或 `source ~/.bashrc` |
| `tboxctl status` 退出码 `4`（health unreachable） | 端口在但 HTTP 死了 | `tboxctl logs -n 50`，然后 `restart` |
| 上传报 `sha256 mismatch` | metadata 里的 sha256 算错了 | 客户端重新算；或留空让服务端不强校验 |
| 上传报 `size mismatch` | metadata 里的 size 与实际字节数不一致 | 客户端按实际字节数填写 |
| 上传报 `upload exceeds … bytes` | 单文件超 `TBOX_MAX_UPLOAD_BYTES` | 调大环境变量（默认 64 MiB）或分块 |
| 上传报 `cmd_id not found` (404) | metadata.cmd_id 不是这台终端已下发的命令 | 检查命令是否被 reaper 清掉；或 push 一条新命令 |
| `/poll` 报 `terminal not found` (404) | 终端还没 heartbeat 过 | 让终端先发 `POST /heartbeat` |
| ack 报 `cmd_id not found` (404) | ack 的 cmd_id 在服务器没记录 | 通常是 push 后又被 reaper 清掉，或 ack 写错了 cmd_id |

---

## 7. 环境变量（启动时生效）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TBOX_HOST` | `0.0.0.0` | 绑定地址（监听所有接口） |
| `TBOX_PORT` | `9999` | 绑定端口 |
| `TBOX_DATA_DIR` | `./data` | 数据根目录 |
| `TBOX_HEARTBEAT_INTERVAL_S` | `30` | 心跳间隔（建议给终端的提示值） |
| `TBOX_OFFLINE_AFTER_S` | `90` | 多久没心跳算离线 |
| `TBOX_LONG_POLL_TIMEOUT_S` | `30` | `/poll` 默认挂起时长 |
| `TBOX_LONG_POLL_MAX_TIMEOUT_S` | `120` | `/poll` 单次最长挂起时长 |
| `TBOX_MAX_UPLOAD_BYTES` | `67108864` | 单次 `/upload` 字节上限 |
| `TBOX_MAX_PENDING_COMMANDS_PER_TERMINAL` | `32` | 单终端队列上限 |
| `TBOX_SERVER_VERSION` | `0.1.0` | 在 `/healthz` 和 `/api/version` 里回显 |

要看当前生效值：

```bash
tboxctl status
TBOX_PORT=9000 tboxctl start -d --port 9000  # 启动参数覆盖默认
```

---

## 8. 文件位置速查

| 文件 | 内容 |
|---|---|
| `/root/tbox-server/run_server.py` | 服务器入口 |
| `/root/tbox-server/tboxctl` | CLI 管理工具 |
| `/root/tbox-server/data/server.pid` | 当前服务器 PID |
| `/root/tbox-server/data/server.log` | 服务器日志 |
| `/root/tbox-server/data/uploads/YYYY-MM-DD/<tid>/<sha>__<name>` | 上传的文件 |
| `/root/tbox-server/data/uploads/YYYY-MM-DD/<tid>/<…>.meta.json` | 文件的元数据（含 cmd_id） |
| `/root/tbox-server/data/reports/<tid>/status/latest.json` | status 上报（每次覆盖） |
| `/root/tbox-server/simulator/terminal_sim.py` | 模拟终端，方便端到端调试 |

---

## 9. 一页纸 cheat sheet

```text
启动/停止
  tboxctl start -d           # 后台启动
  tboxctl stop               # 停止
  tboxctl restart -d         # 重启
  tboxctl status             # 状态

日志
  tboxctl logs -n 100        # 最近 100 行
  tboxctl logs -f            # 跟踪

终端管理
  tboxctl admin list                                # 终端列表
  tboxctl admin push <tid> upload \
      --payload-json '{"file_type":"log"}'          # 让终端收集日志后上传
  tboxctl admin push <tid> upload \
      --payload-json '{"file_type":"other","path":"/data/x.bin"}'
  tboxctl admin push <tid> exec \
      --payload-json '{"command":"uptime"}'         # 执行命令
  tboxctl admin show <tid>                          # 看最新 status
```
