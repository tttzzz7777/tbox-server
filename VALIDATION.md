# 真机验证方法 — Real tbox ↔ tbox-server

> **本文档基于 v1 协议编写，其中 `/version`、`/logs/upload`、`fw_version`
> 等步骤在 v2 中已下线。** 当前协议请参考
> [`TBOXHELPER_PROTOCOL.md`](./TBOXHELPER_PROTOCOL.md)。
> 保留此文件用于历史验收记录参考；v2 验收请按新协议自行设计步骤。

这份文档给到一份**端到端可执行的验收流程**：把一台真实的 tbox 设备
（嵌入式/工控机/带 4G 的开发板都行）接上 `tbox-server`，按下面顺序一项项
过，每项都有「怎么测、怎么看、什么算过、不过怎么办」。

> 文档假设：服务器在 `tboxctl` 控制下运行在 `192.168.x.x:9999`（下文用
> `SERVER_IP` 代指），设备和服务器在同一可直达的内网。

---

## 0. 验收前的准备

### 0.1 服务器侧

```bash
# 0) 确认服务在跑、状态健康
tboxctl status
# 期望: pid 非空，health: ok，port open: True

# 1) 打开实时日志（另一个窗口留着，全过程都要看）
tboxctl logs -f

# 2) 清出干净环境（避免历史数据干扰）
ls /root/tbox-server/data/uploads/$(date -u +%F) 2>/dev/null
ls /root/tbox-server/data/logs/$(date -u +%F)    2>/dev/null
# 如果要彻底清，stop 后删目录再 start（谨慎操作，会丢数据）

# 3) 准备好两个临时变量
export SERVER_IP=$(hostname -I | awk '{print $1}')   # 服务器 IP
export TID="TBOX-VALIDATE-01"                         # 给这次验证用的终端 ID
```

### 0.2 设备侧

需要从设备上拿到或配置：
- 服务器 URL（要支持 HTTP；走 HTTPS 需要反代）
- 终端 ID（强烈建议用临时 ID，区分于量产设备）
- 心跳间隔（推荐先用 30 s，跟服务器默认一致）
- 长轮询 `/poll` 的开关（很多 tbox SDK 默认关闭，需要显式打开）
- 文件上传 / 日志上传的触发方式（手动触发或周期触发）

### 0.3 网络侧

```bash
# 从设备所在网段 ping 服务器
ping -c 3 $SERVER_IP

# 从设备上 curl 服务器健康检查（这条能通，说明 HTTP 通路没问题）
curl -v http://$SERVER_IP:9999/healthz
# 期望: HTTP/1.1 200 OK，body {"status":"ok",...}
```

如果不通，按顺序排查：
1. 服务器是否在 `0.0.0.0:9999` 监听（`ss -tlnp | grep 9999`）
2. 防火墙（云上机器注意安全组，本机 `iptables -L`/`nft list ruleset`）
3. NAT/路由
4. 设备 SIM 卡流量（如果走蜂窝）

---

## 1. 第一步：心跳连通性（最小冒烟）

**目的**：确认设备能访问服务器，并被服务器识别。

### 操作

1. 设备上电，启动 tbox 应用
2. 让设备发一次心跳（手动触发或等 SDK 周期）
3. 服务器侧观察

### 服务器侧怎么看

```bash
# 实时看日志
tboxctl logs -f | grep -E 'heartbeat|admin/terminals'
# 应该看到一行: POST /heartbeat rid=... -> 200

# 或者直接拉列表
tboxctl admin list
# 应该看到一行，TID=TBOX-VALIDATE-01，online（绿色），LAST_SEEN 是刚刚
```

### 怎么算过

| 检查项 | 通过条件 |
|---|---|
| HTTP 200 | `POST /heartbeat` 在 `tboxctl logs -f` 里出现且 200 |
| 终端可见 | `tboxctl admin list` 包含该 `TID`，`STATE` 是 `online` |
| 版本回显 | `fw_version` 字段非空，且和设备固件版本一致 |
| 心跳间隔合理 | 设备按配置周期再发几次，`LAST_SEEN` 在更新 |

### 不通过怎么办

- **看不到心跳**：检查设备日志（一般 tbox 应用有本地日志），重点看 DNS /
  连接错误、TLS 错误、401/403
- **HTTP 404/405**：多半路径不对，让 SDK 文档/抓包确认是 `POST /heartbeat`
- **在线但 `fw_version` 空**：SDK 可能没填这个字段，无伤大雅，可以接着走
- **`STATE` 一直是 offline**：`offline_after_s`（默认 90s）内没续上，
  说明设备只发了 1 次就断网了，检查长连接

---

## 2. 第二步：服务器 → 设备 下行（命令投递）

**目的**：验证 `admin push` 能推到设备，设备能 ack。

### 操作

```bash
# 2.1 打开实时日志窗口 A
tboxctl logs -f | grep -E 'poll|command|admin'

# 2.2 在设备侧触发一次长轮询（如果 SDK 是周期行为，等下一个周期即可）

# 2.3 观察设备有没有在发 GET /poll
# 在窗口 A 应该看到一行: GET /poll rid=... -> 200 （长轮询返回 idle）

# 2.4 立刻下发命令
tboxctl admin push $TID reboot --payload delay_s=1
# 输出: ✓ queued command <cmd_id> -> TBOX-VALIDATE-01 type=reboot

# 2.5 观察
# - 服务器日志里应该有 POST /admin/command 200
# - 紧接着设备那条 GET /poll 应该立刻收到响应（不再是 idle）：
#   状态码 200，body 里有 cmd
# - 设备执行后应该发 POST /command/ack 200
```

### 怎么算过

| 检查项 | 通过条件 |
|---|---|
| 长轮询正常 | 服务器日志里 `GET /poll -> 200` 周期性出现 |
| 下发后立刻投递给设备 | 从 `admin push` 到设备收到命令，延迟通常 < 1 s |
| 命令 payload 完整 | 设备收到的 JSON 包含 `cmd_id / type / payload / issued_at / ttl_s` |
| 设备回了 ack | 服务器日志出现 `POST /command/ack -> 200`，`cmd_id` 一致 |

### 不通过怎么办

- **`/poll` 一直 idle 但 `admin list` 里 `PENDING` 一直在涨**：
  设备根本没在长轮询，多半 SDK 默认关闭，需要在设备配置里打开
- **命令收不到，延迟很大**：设备的 `/poll` `wait` 参数太短（< 服务器入队到
  设备轮询窗口），可调 `TBOX_LONG_POLL_TIMEOUT_S` 或 SDK 端 `wait=`
- **收得到但没 ack**：设备 SDK 没实现 ack 流程，或 `cmd_id` 字段名对不上
  （服务端要的是 `cmd_id`，不要 `command_id` 之类的变体）

---

## 3. 第三步：设备 → 服务器 上行（文件上传）

**目的**：验证 `multipart/form-data` 上传链路、sha256 校验、原子落盘。

### 操作（3 选 1，看设备支持哪种）

**A. 设备 SDK 有"上传诊断包"功能**：直接触发，看服务器侧

**B. 设备 SDK 没上传功能**：先跳过，等补完 SDK 再补做；同时用 `simulator` 跑
一遍对照（开发侧参考）

**C. 用 `curl` 模拟设备发一个**（最严格的对照实验）

```bash
# C.1 在设备上生成 1 MiB 随机数据
head -c 1048576 /dev/urandom > /tmp/diag.bin
sha256sum /tmp/diag.bin   # 记下 sha

# C.2 用 curl 替代设备发请求
curl -X POST http://$SERVER_IP:9999/upload \
  -F 'metadata={"terminal_id":"'$TID'","path":"/var/log/diag.bin","sha256":"<上面那个sha>","size":1048576,"kind":"diagnostics"};type=application/json' \
  -F 'file=@/tmp/diag.bin'

# C.3 服务器侧验证
ls -lh /root/tbox-server/data/uploads/$(date -u +%F)/$TID/
# 应该看到 1 MiB 的文件 + 一个 .meta.json

cat /root/tbox-server/data/uploads/$(date -u +%F)/$TID/*.meta.json
# 内容应该是 {sha256, size, kind, terminal_id, ...}

# C.4 一致性校验
sha256sum /root/tbox-server/data/uploads/$(date -u +%F)/$TID/<sha-prefix>__diag.bin
# 应该等于 /tmp/diag.bin 的 sha
```

### 怎么算过

| 检查项 | 通过条件 |
|---|---|
| HTTP 200 | curl / SDK 都拿到 200 |
| 文件实际落盘 | `data/uploads/<日期>/<tid>/` 下出现文件 |
| sha256 一致 | 服务器记录的 sha 与原始字节流的 sha 一致 |
| size 一致 | 服务器记录的 size 与实际字节数一致 |
| `.meta.json` 存在 | 包含 `terminal_id / kind / sha256 / size / stored_at` |

### 不通过怎么办

- **400 "sha256 mismatch"**：设备端 sha 算错（一般是用文本模式读了二进制
  文件导致换行被替换）；要求 SDK 用二进制模式
- **400 "size mismatch"**：metadata 里的 `size` 和实际字节数不一致，
  多数是 SDK 多/少算了几个字节（比如把边界 chunk 算重复了）
- **413 "exceeds … bytes"**：调大 `TBOX_MAX_UPLOAD_BYTES`（默认 64 MiB），
  或让设备分块
- **415 "expected multipart/form-data"**：SDK 用错了 Content-Type

---

## 4. 第四步：日志上传

和第 3 步几乎一样的流程，端点是 `/logs/upload`：

```bash
# 4.1 模拟设备发一条日志
echo "2026-09-15 12:34:56 INFO navd GPS fix acquired" > /tmp/navd.log

curl -X POST http://$SERVER_IP:9999/logs/upload \
  -F 'metadata={"terminal_id":"'$TID'","level":"info","seq":1,"ts":1731628800,"app":"navd"};type=application/json' \
  -F 'log=@/tmp/navd.log'

# 4.2 验证
ls /root/tbox-server/data/logs/$(date -u +%F)/$TID/
cat /root/tbox-server/data/logs/$(date -u +%F)/$TID/*.meta.json
```

### 怎么算过

- HTTP 200
- `data/logs/<日期>/<tid>/<ts>_<seq>.log` 落盘
- `.meta.json` 存在，内容含 `terminal_id/level/seq/ts/app/sha256`

### 不通过怎么办

- **`missing 'log' field`**：SDK 上传的 part 名不是 `log`，得是 `log`
  （multipart 的 `name=` 属性）
- **`bad metadata`**：JSON 缺字段或类型不对（`terminal_id` 必填）

---

## 5. 第五步：版本清单查询

### 操作

```bash
# 5.1 服务器侧确保版本文件存在
ls /root/tbox-server/data/versions/stable/latest.json
cat /root/tbox-server/data/versions/stable/latest.json
# { "latest": "1.4.3", "url": "/downloads/firmware-1.4.3.bin", ... }

# 5.2 模拟设备查版本
curl 'http://'$SERVER_IP':9999/version?channel=stable'
# 应该返回 {"status":"ok","channel":"stable","latest":"1.4.3",...}

# 5.3 验证 5 s 缓存：连续两次请求，第二次应该秒回
time curl -s 'http://'$SERVER_IP':9999/version?channel=stable' > /dev/null
time curl -s 'http://'$SERVER_IP':9999/version?channel=stable' > /dev/null
```

### 怎么算过

- 返回 `status=ok`，`latest` 与磁盘上的 `latest.json` 一致
- 修改 `latest.json` 后 5 s 内新值生效（缓存 TTL）

### 不通过怎么办

- **204 No Content**：`data/versions/stable/latest.json` 不存在，
  创建一份
- **设备说"收到的版本和本地一样，不下载"**：这是设备 SDK 的版本比对逻辑，
  不是 bug；如果想强制升级，把 `mandatory: true`

---

## 6. 第六步：异常与边界

这一节是「我打算搞坏它，看它能不能优雅处理」。

### 6.1 设备中途断网

```bash
# 操作：拔网线 / 关飞行模式 / 拔 SIM 卡
# 观察：
#  - 服务器侧：心跳没了；PENDING 不变（命令还在队列里）
#  - 超 TBOX_OFFLINE_AFTER_S（默认 90 s）后，admin list STATE=offline
# 恢复网络后：
#  - 设备重新心跳 → STATE=online
#  - 设备的 GET /poll 立刻收到之前积压的命令
```

**期望**：命令不丢，不重复入队。

### 6.2 中途断网且 TTL 过期

```bash
# 设备断网期间，过期的命令应该被 reaper 清掉
# 操作：下发一个 ttl=10 的命令，立刻断网，20 s 后恢复
tboxctl admin push $TID reboot --ttl 10
sleep 20
# 设备恢复后 GET /poll 应该收到 idle（命令已过期被丢弃）

### 6.3 上传超过大小上限

```bash
dd if=/dev/zero of=/tmp/big.bin bs=1M count=80   # 80 MiB
curl -X POST http://$SERVER_IP:9999/upload \
  -F 'metadata={"terminal_id":"'$TID'","size":0};type=application/json' \
  -F 'file=@/tmp/big.bin'
# 期望: HTTP 413 {"status":"error","message":"upload exceeds 67108864 bytes"}
```

### 6.4 上传 sha 算错

```bash
curl -X POST http://$SERVER_IP:9999/upload \
  -F 'metadata={"terminal_id":"'$TID'","sha256":"0000000000000000000000000000000000000000000000000000000000000000","size":11};type=application/json' \
  -F 'file=hello world'
# 期望: HTTP 400 sha256 mismatch
```

### 6.5 长轮询客户端断连

```bash
# 终端 1：发起长轮询
curl -N 'http://'$SERVER_IP':9999/poll?wait=30' -H 'X-Terminal-Id: '$TID
# 终端 2：5 秒后下发命令
sleep 5
tboxctl admin push $TID reboot
# 立刻 Ctrl-C 终端 1 的 curl
# 期望：命令不丢，设备的下一个长轮询能收到（验证 re-enqueue 逻辑）
```

### 6.6 并发多终端

```bash
# 准备 N 个终端 ID，启动 N 个 simulator
for i in $(seq 1 5); do
  TERMINAL_ID=TBOX-LOAD-$i .venv/bin/python simulator/terminal_sim.py &
done
sleep 30
tboxctl admin list
# 期望: 5 个 terminal 都 online，admin push 给其中一个，其他不受影响
```

---

## 7. 验收清单（签收用）

把这份清单填完贴到群里/工单里就齐活了。

### 必过项

- [ ] 0.1 服务 `status` 健康，端口可达
- [ ] 0.3 设备能 curl `/healthz` 拿到 200
- [ ] 1 设备心跳后 `admin list` 可见且 online
- [ ] 1 心跳的 `fw_version` 与设备固件一致
- [ ] 2 设备长轮询周期出现于日志
- [ ] 2 `admin push` 后 1 秒内设备收到命令
- [ ] 2 设备回 ack，cmd_id 一致
- [ ] 3 文件上传 200，sha256 / size 一致，`.meta.json` 存在
- [ ] 4 日志上传 200，文件 + `.meta.json` 落盘
- [ ] 5 `GET /version` 返回稳定清单
- [ ] 6.1 设备断网恢复后命令不丢
- [ ] 6.3 超大上传返回 413 而非 500
- [ ] 6.4 sha 不匹配返回 400 而非 500

### 加分项

- [ ] 6.2 TTL 过期命令被 reaper 清掉
- [ ] 6.5 客户端断连后命令不丢
- [ ] 6.6 5+ 终端并发，admin push 互不影响
- [ ] 设备在断网期间，队列里堆积的命令恢复后能按序投递给设备
- [ ] 服务端 `tboxctl logs -f` 在整个验收过程无 ERROR/Traceback

### 验收记录模板

```
日期：           YYYY-MM-DD
服务器版本：     tboxctl status 里的 server_version
设备型号：
设备固件版本：
设备 SDK 版本：
终端 ID：
测试时长：

结果：           通过 / 部分通过 / 不通过
必过项：         X / 13
加分项：         X / 5

主要问题：

备注：
```

---

## 8. 现场应急工具箱

```bash
# 查某终端当前状态
tboxctl admin list | grep $TID

# 看这台终端的所有日志行
tboxctl logs -n 1000 | grep $TID

# 强制让一个命令立刻过期（验证 reaper）
TBOX_DEFAULT_COMMAND_TTL_S=1 tboxctl admin push $TID reboot
# 然后立刻 GET /poll 一次，应该拿不到这条命令（已经被 reaper 清掉）

# 不重启服务地改端口（先 stop，再带新端口 start）
tboxctl stop
tboxctl start -d --port 9000
tboxctl --port 9000 admin list   # admin 子命令也要带新端口

# 看现在的 PID / 进程命令行
cat /root/tbox-server/data/server.pid
ps -p $(cat /root/tbox-server/data/server.pid) -o pid,etime,cmd

# 实时看 reaper 跑了多少次
tboxctl logs -f | grep -i reaper
```

---

## 附录 A. curl 客户端测试指令全集

这份是「不依赖真机，只用 curl + bash 就能跑完整个协议面」的烟囱测试清单。
每条命令独立可执行；建议在两台机器上同时开：
- **机器 A**：`tboxctl logs -f` 实时观察服务器日志
- **机器 B**：依次执行下面的 curl，观察响应

### A.0 公共变量

先 `export` 一组，下面所有命令直接复用：

```bash
# 服务器地址（用本机回路也行）
export SERVER_IP=159.75.187.190
export PORT=9999
export BASE=http://$SERVER_IP:$PORT

# 验证用终端 ID（建议用临时 ID 方便清理）
export TID=TBOX-CURL-$(date +%s)
export FW=1.0.0

# 服务器当前日期（验证落盘路径用）
export TODAY=$(date -u +%F)
```

**调试技巧**：

| 标志 | 作用 |
|---|---|
| `-v` | 打印请求/响应头、状态码、握手细节 |
| `-i` | 响应里带上响应头 |
| `-s` | 静默模式（不加进度条） |
| `-S` | 静默模式下仍然报错 |
| `-w '\nHTTP %{http_code} in %{time_total}s\n'` | 在末尾打印状态码 + 耗时 |
| `--trace-ascii /tmp/curl.log` | 完整 dump 请求/响应（包含二进制） |
| `-o /dev/null -w '%{http_code}'` | 只输出状态码 |
| `-T file` | PUT 模式（这次不用） |
| `-X METHOD` | 显式指定方法 |

### A.1 健康检查

```bash
# 最简单的"在不在"
curl -s $BASE/healthz

# 带响应头 + 状态码 + 耗时
curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' $BASE/healthz

# 只看状态码（CI 里常用）
curl -s -o /dev/null -w '%{http_code}\n' $BASE/healthz
# 期望: 200
```

期望响应：

```json
{"status": "ok", "server_version": "0.1.0", "version": "0.1.0"}
```

### A.2 版本查询

```bash
# 默认 channel=stable
curl -s $BASE/version

# 指定 channel
curl -s "$BASE/version?channel=beta"

# 加上状态码
curl -sS -w '\nHTTP %{http_code}\n' $BASE/version
```

期望响应（已配置 `data/versions/stable/latest.json`）：

```json
{"status": "ok", "channel": "stable", "latest": "1.4.3", "url": "/downloads/firmware-1.4.3.bin", "sha256": "...", "mandatory": false, "release_notes": "..."}
```

期望响应（未配置）：`HTTP 204`，body 为空。

### A.3 心跳（terminal → server）

```bash
# 最小必填字段
curl -s -X POST $BASE/heartbeat \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","fw_version":"'$FW'"}'

# 完整字段
curl -s -X POST $BASE/heartbeat \
  -H 'content-type: application/json' \
  -d '{
    "terminal_id":"'$TID'",
    "fw_version":"'$FW'",
    "ts":1731628800,
    "address":"192.168.1.100",
    "caps":["gps","can","wifi"],
    "free_disk_mb":512
  }'

# 缺 terminal_id 的错误响应（应该 400）
curl -sS -w '\nHTTP %{http_code}\n' -X POST $BASE/heartbeat \
  -H 'content-type: application/json' \
  -d '{"fw_version":"1.0"}'
# 期望: HTTP 400 + {"status":"error","message":"terminal_id is required"}
```

期望正常响应：

```json
{
  "status": "ok",
  "server_time": 1731628801.5,
  "heartbeat_interval_s": 30,
  "long_poll_timeout_s": 30,
  "server_version": "0.1.0"
}
```

### A.4 管理端：列出终端

```bash
# 看完整列表
curl -s $BASE/admin/terminals

# 只看在线的（用 jq 过滤，没有 jq 就 awk）
curl -s $BASE/admin/terminals | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in d['terminals']:
    if t['online']: print(t['terminal_id'])
"

# 检查某个特定终端在不在
curl -s $BASE/admin/terminals | grep -o "\"terminal_id\":\"$TID\"" && echo "FOUND" || echo "MISSING"
```

### A.5 长轮询（terminal → server）

长轮询的关键：**请求挂着不要断**（用 `curl -N` 禁用缓冲），让服务器在另一
个终端里下发命令就能即时拿到。

```bash
# A.5.1 标准长轮询（挂 30 秒，期间没命令会返回 idle）
curl -sN -H "X-Terminal-Id: $TID" "$BASE/poll?wait=30"

# A.5.2 短轮询（只挂 5 秒，调试时方便）
curl -sN -H "X-Terminal-Id: $TID" "$BASE/poll?wait=5"

# A.5.3 不带身份 → 400
curl -sS -w '\nHTTP %{http_code}\n' "$BASE/poll"
# 期望: HTTP 400 + terminal_id required

# A.5.4 用 query 参数代替 header（也支持）
curl -sN "$BASE/poll?wait=5&terminal_id=$TID"

# A.5.5 长轮询配合命令投递（跨两个终端验证实时性）
# === 终端 1：发起长轮询 ===
curl -sN -H "X-Terminal-Id: $TID" "$BASE/poll?wait=30" &
POLL_PID=$!
sleep 2   # 让 curl 真的把请求发出去

# === 终端 2：下发命令 ===
tboxctl admin push $TID reboot --payload delay_s=5

# === 回到终端 1 ===
wait $POLL_PID
# 期望: 立刻（<1s）拿到 {"status":"command","cmd":{...}}
```

期望响应（无命令）：

```json
{"status": "idle", "server_time": 1731628830.1}
```

期望响应（有命令）：

```json
{
  "status": "command",
  "cmd": {
    "cmd_id": "f1196139f0ba41539fa2d18f67785994",
    "type": "reboot",
    "payload": {"delay_s": 5},
    "issued_at": 1731628830.0,
    "ttl_s": 3600
  },
  "server_time": 1731628830.2
}
```

### A.6 管理端：下发命令

```bash
# A.6.1 最简单的命令
curl -s -X POST $BASE/admin/command \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","type":"reboot"}'

# A.6.2 带 payload
curl -s -X POST $BASE/admin/command \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","type":"reboot","payload":{"delay_s":5}}'

# A.6.3 带 TTL（秒）
curl -s -X POST $BASE/admin/command \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","type":"ota_check","payload":{"channel":"stable"},"ttl_s":600}'

# A.6.4 未知命令类型 → 400
curl -sS -w '\nHTTP %{http_code}\n' -X POST $BASE/admin/command \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","type":"format_disk"}'
# 期望: HTTP 400 + allowed 列表

# A.6.5 缺 terminal_id → 400
curl -sS -w '\nHTTP %{http_code}\n' -X POST $BASE/admin/command \
  -H 'content-type: application/json' \
  -d '{"type":"reboot"}'
```

期望响应：

```json
{"status": "ok", "cmd_id": "f1196139...", "issued_at": 1731628801.5}
```

### A.7 命令 ack（terminal → server）

```bash
# A.7.1 成功 ack
curl -s -X POST $BASE/command/ack \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","cmd_id":"'$CMD_ID'","status":"ok","result":{"exit_code":0},"ts":1731628900}'

# A.7.2 失败 ack
curl -s -X POST $BASE/command/ack \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","cmd_id":"'$CMD_ID'","status":"fail","result":{"exit_code":-1,"error":"permission denied"},"ts":1731628900}'

# A.7.3 缺字段 → 400
curl -sS -w '\nHTTP %{http_code}\n' -X POST $BASE/command/ack \
  -H 'content-type: application/json' \
  -d '{"status":"ok"}'
# 期望: HTTP 400
```

### A.8 文件上传（terminal → server）

```bash
# A.8.1 准备测试文件
echo "hello tbox upload $(date)" > /tmp/sample.txt
SIZE=$(stat -c%s /tmp/sample.txt)
SHA=$(sha256sum /tmp/sample.txt | awk '{print $1}')

echo "size=$SIZE sha=$SHA"

# A.8.2 简单上传（不校验 sha/size）
curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' \
  -X POST $BASE/upload \
  -F 'metadata={"terminal_id":"'$TID'","path":"/var/log/sample.txt","kind":"diagnostics"};type=application/json' \
  -F 'file=@/tmp/sample.txt'

# A.8.3 带 sha256 + size 校验的上传（推荐）
curl -sS -w '\nHTTP %{http_code}\n' \
  -X POST $BASE/upload \
  -F 'metadata={"terminal_id":"'$TID'","path":"/var/log/sample.txt","sha256":"'$SHA'","size":'$SIZE',"kind":"diagnostics"};type=application/json' \
  -F 'file=@/tmp/sample.txt'

# A.8.4 上传二进制文件（比如 1 MiB 随机数据）
dd if=/dev/urandom of=/tmp/diag.bin bs=1M count=1 status=none
SIZE=$(stat -c%s /tmp/diag.bin)
SHA=$(sha256sum /tmp/diag.bin | awk '{print $1}')
curl -s -X POST $BASE/upload \
  -F 'metadata={"terminal_id":"'$TID'","path":"/var/log/diag.bin","sha256":"'$SHA'","size":'$SIZE',"kind":"diagnostics"};type=application/json' \
  -F 'file=@/tmp/diag.bin'

# A.8.5 上传后立刻验证
ls -lh /root/tbox-server/data/uploads/$TODAY/$TID/
sha256sum /root/tbox-server/data/uploads/$TODAY/$TID/<sha-prefix>__sample.txt
# 应该等于 /tmp/sample.txt 的 sha

cat /root/tbox-server/data/uploads/$TODAY/$TID/<sha-prefix>__sample.txt.meta.json
```

期望响应：

```json
{"status": "ok", "stored": "data/uploads/2026-09-15/TBOX-.../ab12__sample.txt", "bytes": 23, "sha256": "ab12..."}
```

### A.9 日志上传（terminal → server）

```bash
# A.9.1 准备日志
cat > /tmp/navd.log <<'EOF'
2026-09-15 12:00:00 INFO navd GPS fix acquired
2026-09-15 12:00:01 INFO navd sending position to server
EOF

# A.9.2 上传
curl -s -X POST $BASE/logs/upload \
  -F 'metadata={"terminal_id":"'$TID'","level":"info","seq":1,"ts":1731628800,"app":"navd"};type=application/json' \
  -F 'log=@/tmp/navd.log'

# A.9.3 验证落盘
ls /root/tbox-server/data/logs/$TODAY/$TID/
cat /root/tbox-server/data/logs/$TODAY/$TID/<ts>_000001.log.meta.json
```

### A.10 错误响应全景（确认错误码语义）

每条都用 `-w` 把状态码打印出来，方便对照：

```bash
# A.10.1 健康检查始终 200（哪怕服务半残）
curl -s -o /dev/null -w '%{http_code}\n' $BASE/healthz            # 期望 200

# A.10.2 心跳缺 terminal_id
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/heartbeat \
  -H 'content-type: application/json' -d '{}'                      # 期望 400

# A.10.3 心跳 body 不是 JSON
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/heartbeat \
  -H 'content-type: application/json' -d 'not json'                # 期望 400

# A.10.4 未知路径
curl -s -o /dev/null -w '%{http_code}\n' $BASE/nonexistent          # 期望 404

# A.10.5 上传缺 file 字段
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/upload \
  -F 'metadata={"terminal_id":"'$TID'"};type=application/json'      # 期望 400

# A.10.6 上传缺 metadata 字段
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/upload \
  -F 'file=@/tmp/sample.txt'                                       # 期望 400

# A.10.7 上传 metadata 不是 multipart
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/upload \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'"}'                                    # 期望 415

# A.10.8 上传 sha 不匹配
echo "hello" > /tmp/wrong.bin
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/upload \
  -F 'metadata={"terminal_id":"'$TID'","sha256":"0000000000000000000000000000000000000000000000000000000000000000","size":6};type=application/json' \
  -F 'file=@/tmp/wrong.bin'                                        # 期望 400

# A.10.9 上传超过大小上限
dd if=/dev/zero of=/tmp/big.bin bs=1M count=80 status=none
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/upload \
  -F 'metadata={"terminal_id":"'$TID'","size":0};type=application/json' \
  -F 'file=@/tmp/big.bin'                                          # 期望 413

# A.10.10 未知命令类型
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/admin/command \
  -H 'content-type: application/json' \
  -d '{"terminal_id":"'$TID'","type":"format_disk"}'               # 期望 400

# A.10.11 不存在的版本 channel
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/version?channel=ghost"  # 期望 204
```

### A.11 完整冒烟脚本（一键跑完）

把这段保存为 `/tmp/smoke.sh`，跑一遍就能验证主要路径：

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://159.75.187.190:9999}"
TID="TBOX-SMOKE-$(date +%s)"
TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
expect() {
    local got="$1" want="$2" what="$3"
    if [[ "$got" == "$want" ]]; then
        printf '  \033[32m✓\033[0m %s: HTTP %s\n' "$what" "$got"
    else
        printf '  \033[31m✗\033[0m %s: got HTTP %s, want %s\n' "$what" "$got" "$want"
        exit 1
    fi
}

step "health"
code=$(curl -s -o /dev/null -w '%{http_code}' $BASE/healthz)
expect "$code" 200 "GET /healthz"

step "heartbeat"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/heartbeat \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","fw_version":"1.0.0"}')
expect "$code" 200 "POST /heartbeat"

step "version"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/version?channel=stable")
[[ "$code" == "200" || "$code" == "204" ]] || { echo "  ✗ version: $code"; exit 1; }
printf '  \033[32m✓\033[0m GET /version: HTTP %s\n' "$code"

step "admin list"
curl -s $BASE/admin/terminals | grep -q "$TID" || { echo "  ✗ TID not in list"; exit 1; }
printf '  \033[32m✓\033[0m TID %s present\n' "$TID"

step "admin push + poll (round trip)"
curl -sN -H "X-Terminal-Id: $TID" "$BASE/poll?wait=5" > $TMP/poll.out &
POLL_PID=$!
sleep 1
curl -s -X POST $BASE/admin/command \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","type":"reboot","payload":{"delay_s":1}}' > $TMP/push.out
wait $POLL_PID
grep -q '"status":"command"' $TMP/poll.out || { echo "  ✗ poll did not receive command"; cat $TMP/poll.out; exit 1; }
printf '  \033[32m✓\033[0m command delivered\n'

step "file upload (1 MiB)"
dd if=/dev/urandom of=$TMP/big.bin bs=1M count=1 status=none
SIZE=$(stat -c%s $TMP/big.bin)
SHA=$(sha256sum $TMP/big.bin | awk '{print $1}')
code=$(curl -s -o $TMP/up.out -w '%{http_code}' -X POST $BASE/upload \
    -F 'metadata={"terminal_id":"'$TID'","sha256":"'$SHA'","size":'$SIZE',"kind":"diagnostics"};type=application/json' \
    -F 'file=@'$TMP'/big.bin')
expect "$code" 200 "POST /upload"
STORED=$(python3 -c "import json; print(json.load(open('$TMP/up.out'))['stored'])")
[[ -f "$STORED" ]] || { echo "  ✗ stored file not found: $STORED"; exit 1; }
ACTUAL=$(sha256sum "$STORED" | awk '{print $1}')
[[ "$ACTUAL" == "$SHA" ]] || { echo "  ✗ sha mismatch"; exit 1; }
printf '  \033[32m✓\033[0m file stored at %s (sha matches)\n' "$STORED"

step "log upload"
echo "INFO smoke test $(date)" > $TMP/log.txt
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/logs/upload \
    -F 'metadata={"terminal_id":"'$TID'","level":"info","seq":1,"ts":0,"app":"smoke"};type=application/json' \
    -F 'log=@'$TMP'/log.txt')
expect "$code" 200 "POST /logs/upload"

step "ack"
CMD_ID=$(python3 -c "import json; print(json.load(open('$TMP/poll.out'))['cmd']['cmd_id'])")
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/command/ack \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","cmd_id":"'$CMD_ID'","status":"ok","result":{},"ts":0}')
expect "$code" 200 "POST /command/ack"

step "DONE"
echo "  terminal id used: $TID"
echo "  pass"
```

跑法：

```bash
chmod +x /tmp/smoke.sh
BASE=http://192.168.1.100:9999 /tmp/smoke.sh
# 期望: 全程绿勾，最后一行 "pass"
```

### A.12 抓包对照（终极手段）

如果某条命令行为对不上预期，用 `tcpdump` 抓包对照：

```bash
# 服务器侧抓 9999 端口的 HTTP（需要 root）
sudo tcpdump -i any -A -s 0 'tcp port 9999' -w /tmp/tbox.pcap

# 然后用设备/curl 触发请求，最后用下面查看
tcpdump -r /tmp/tbox.pcap -A | less

# 或者图形化：把 pcap 拷到本地用 Wireshark 打开
```

如果你只想看协议级别（不解 TLS），用 `mitmproxy` 也可以：

```bash
mitmproxy --listen-port 9999 --mode reverse:http://159.75.187.190:9999
# 然后把设备的 BASE 改成 http://<mitmproxy-ip>:9999
# 所有请求/响应会在 mitmproxy 的 TUI 里实时显示
```