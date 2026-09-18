# tbox-server

A small HTTP server (Python `asyncio` + `aiohttp`) that accepts connections
from remote telematics/terminal devices (a.k.a. *tbox*), keeps track of which
terminals are online, lets them upload files tied to issued commands, and
lets an operator push commands down to terminals via long polling.

The on-the-wire protocol is defined in **`TBOXHELPER_PROTOCOL.md`** (v2).
The legacy v1 spec in `PROTOCOL.md` is kept for historical reference but
**no longer describes the server's behaviour**.

## Features

- **Heartbeat / liveness** — terminals POST `/heartbeat` with
  `{terminal_id, service}`. The server tracks `last_seen`; a background
  reaper marks terminals offline after `TBOX_OFFLINE_AFTER_S` of silence.
- **Long-poll for commands** — terminals keep an open
  `GET /poll?wait=N` (with `X-Terminal-Id` header). The server returns
  immediately when a command is queued, or `idle` after `wait` seconds.
- **Command queue** — operators POST `/admin/command` with
  `{terminal_id, type, payload}` where `type` is one of
  `upload` (with `file_type=log|other`) or `exec`. Each command gets a
  `cmd_id`; `cmd_id` is required in `/upload` metadata and `/command/ack`.
- **File upload** — terminals POST `multipart/form-data` to `/upload` with
  a JSON `metadata` field (`{terminal_id, cmd_id, sha256, size}`) and a
  binary `file` field. Server streams to disk, hashes with SHA-256,
  atomically renames, and writes a `.meta.json` sidecar.
- **Status report** — terminals POST `/report` with
  `{terminal_id, service, report_type: "status", timestamp, data: {...}}`.
  Server stores to `data/reports/<tid>/status/latest.json` (overwritten
  each call; no history).
- **Admin endpoints** — `GET /admin/terminals` lists every known terminal
  with online state, pending command count, last-seen timestamp, and the
  last received status report.

## Quick start

The fastest way to drive the server is via the bundled `tboxctl` CLI:

```bash
cd /root/tbox-server
python3 -m venv .venv
. .venv/bin/activate           # or use .venv/bin/python explicitly
pip install -r requirements.txt

./tboxctl start -d              # start in background
./tboxctl status                # show PID, uptime, port, health, terminal count
./tboxctl admin list            # list known terminals
./tboxctl admin push TBOX-1 upload --payload-json '{"file_type":"log"}'
./tboxctl admin push TBOX-1 exec --payload-json '{"command":"ls /","timeout":10}'
./tboxctl admin show TBOX-1 status
./tboxctl logs -f               # tail server log
./tboxctl stop                  # graceful SIGTERM
```

`./tboxctl --help` and `./tboxctl <subcommand> --help` show every option.

If you prefer to run the server directly (foreground, for debugging):

```bash
TBOX_HOST=0.0.0.0 TBOX_PORT=9999 TBOX_DATA_DIR=./data python run_server.py
```

Or with the test simulator in another shell:

```bash
TBOX_URL=http://127.0.0.1:9999 TERMINAL_ID=TBOX-0001 python simulator/terminal_sim.py
```

## Configuration

All settings are environment variables (prefix `TBOX_`):

| Variable | Default | Description |
|---|---|---|
| `TBOX_HOST` | `0.0.0.0` | bind host (listens on all interfaces) |
| `TBOX_PORT` | `9999` | bind port |
| `TBOX_DATA_DIR` | `./data` | root for uploads/reports |
| `TBOX_DATA_RETENTION_S` | `86400` | how long terminal-uploaded data is kept on disk (0 = keep forever) |
| `TBOX_DATA_CLEANUP_INTERVAL_S` | `3600` | how often the background cleanup sweep runs |
| `TBOX_HEARTBEAT_INTERVAL_S` | `30` | hint returned to terminals |
| `TBOX_OFFLINE_AFTER_S` | `90` | mark terminal offline after this many seconds without heartbeat |
| `TBOX_LONG_POLL_TIMEOUT_S` | `30` | default `wait` for `/poll` |
| `TBOX_LONG_POLL_MAX_TIMEOUT_S` | `120` | cap on per-call `wait` |
| `TBOX_REAPER_INTERVAL_S` | `10` | how often the reaper runs |
| `TBOX_MAX_UPLOAD_BYTES` | `67108864` | hard cap on a single `/upload` |
| `TBOX_DEFAULT_COMMAND_TTL_S` | `3600` | default command TTL when not specified |
| `TBOX_MAX_PENDING_COMMANDS_PER_TERMINAL` | `32` | per-terminal queue cap |
| `TBOX_SERVER_VERSION` | `0.1.0` | echoed in `/healthz` and `/api/version` |

## HTTP endpoints (v2)

All responses are JSON. Upload uses `multipart/form-data`; everything else
is `application/json`.

| Method | Path | Caller | Body / params | Purpose |
|---|---|---|---|---|
| POST | `/heartbeat` | terminal | JSON `{terminal_id, service?}` | register / refresh |
| GET | `/poll` | terminal | header `X-Terminal-Id`, query `wait=N` | long-poll for next command |
| POST | `/command/ack` | terminal | JSON `{terminal_id, cmd_id, status, result, timestamp?}` | report command result |
| POST | `/upload` | terminal | multipart: `metadata` (json) + `file` (binary) | upload a file tied to a `cmd_id` |
| POST | `/report` | terminal | JSON `{terminal_id, service, report_type:"status", timestamp, data}` | status snapshot |
| GET | `/admin/terminals` | operator | – | list terminals |
| POST | `/admin/command` | operator | JSON `{terminal_id, type, payload}` | queue a command |
| GET | `/admin/reports` | operator | query `terminal_id=` | list stored status reports |
| GET | `/admin/report` | operator | query `terminal_id=&type=status` | show latest status JSON |
| POST | `/admin/cleanup` | operator | query `older_than_s=` (optional) | trigger retention sweep |
| GET | `/admin/disk` | operator | – | disk-usage breakdown |
| GET | `/healthz` | operator | – | liveness probe |

### Command types

| `type` | `payload` |
|---|---|
| `upload` | `{file_type: "log"\|"other", ...}` — `log` accepts optional `start_time`/`end_time`; `other` requires `path` |
| `exec` | `{command: "...", timeout?: number}` (default timeout 30) |

### Error codes

| Status | When |
|---|---|
| `400` | malformed JSON, missing/invalid fields, sha256/size mismatch |
| `404` | unknown `terminal_id` (poll before heartbeat), unknown `cmd_id` (ack, upload) |
| `413` | upload body exceeds `TBOX_MAX_UPLOAD_BYTES` |
| `415` | non-multipart `/upload`, non-JSON `/report` |
| `503` | terminal's command queue is full |

## Storage layout

```
data/
├── server.pid                       current server PID
├── server.log                       server log (access + error)
├── uploads/YYYY-MM-DD/<terminal_id>/
│   ├── <sha-prefix>__<safe_name>    uploaded file
│   └── <…>.meta.json                metadata (terminal_id, cmd_id, sha256, size)
└── reports/<terminal_id>/status/
    └── latest.json                  most recent status report (overwritten)
```

All writes go through `…part` + `os.replace` so partial files never appear
under their final name.

## Smoke test with curl

```bash
# heartbeat (v2 fields only)
curl -s -XPOST localhost:9999/heartbeat -H 'content-type: application/json' \
  -d '{"terminal_id":"TBOX-0001","service":"tboxhelper"}'

# queue an upload command
CMD=$(curl -s -XPOST localhost:9999/admin/command -H 'content-type: application/json' \
  -d '{"terminal_id":"TBOX-0001","type":"upload","payload":{"file_type":"log"}}')
CMD_ID=$(echo "$CMD" | python3 -c "import sys,json;print(json.load(sys.stdin)['cmd_id'])")

# terminal picks up via long-poll
curl -sN localhost:9999/poll?wait=5 -H "X-Terminal-Id: TBOX-0001"

# upload a file (sha256 required for integrity check)
echo "diag data" > /tmp/diag.bin
SHA=$(sha256sum /tmp/diag.bin | awk '{print $1}')
SIZE=$(stat -c%s /tmp/diag.bin)
curl -s -XPOST localhost:9999/upload \
  -F "metadata={\"terminal_id\":\"TBOX-0001\",\"cmd_id\":\"$CMD_ID\",\"sha256\":\"$SHA\",\"size\":$SIZE};type=application/json" \
  -F "file=@/tmp/diag.bin"

# ack
curl -s -XPOST localhost:9999/command/ack -H 'content-type: application/json' \
  -d "{\"terminal_id\":\"TBOX-0001\",\"cmd_id\":\"$CMD_ID\",\"status\":\"ok\",\"result\":{\"code\":0}}"

# status report (every ~10s in the simulator)
curl -s -XPOST localhost:9999/report -H 'content-type: application/json' \
  -d '{
    "terminal_id":"TBOX-0001",
    "service":"tboxhelper",
    "report_type":"status",
    "timestamp":1731628800,
    "data":{
      "iccid":"89860123456789012345",
      "imei":"866987123456789",
      "vin":"LVX12345678901234",
      "sn":"TBOX-0001",
      "vehicle_model":"JMC-CX835",
      "position":{"lat":22.5,"lon":114.0,"status":1,"mode":3},
      "dtc":[]
    }
  }'

# admin: list / show / push
curl -s localhost:9999/admin/terminals
curl -s 'localhost:9999/admin/report?terminal_id=TBOX-0001&type=status'
curl -s -XPOST localhost:9999/admin/command -H 'content-type: application/json' \
  -d '{"terminal_id":"TBOX-0001","type":"exec","payload":{"command":"uptime"}}'
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The test suite covers the registry, the storage helpers, and a full
end-to-end HTTP layer using `aiohttp.test_utils.TestClient`.

## Project layout

```
tbox-server/
├── TBOXHELPER_PROTOCOL.md             # current wire protocol (v2)
├── PROTOCOL.md                        # legacy v1 spec, kept for reference
├── requirements.txt
├── requirements-dev.txt
├── README.md
├── run_server.py                      # entrypoint
├── tboxctl                            # CLI management tool
├── tbox_server/
│   ├── app.py                         # aiohttp Application factory
│   ├── config.py                      # env-driven config
│   ├── registry.py                    # TerminalRegistry (sessions, queues, reaper, known_cmd_ids)
│   ├── storage.py                     # filesystem layout, atomic writes, sha256
│   ├── models.py                      # dataclasses for JSON envelopes
│   ├── protocol.py                    # JSON helpers + command-type whitelist
│   ├── utils.py                       # utcnow / safe_filename / new_id
│   ├── middlewares.py                 # request_id, access log, JSON errors
│   └── handlers/
│       ├── heartbeat.py               # POST /heartbeat
│       ├── poll.py                    # GET  /poll
│       ├── command_ack.py             # POST /command/ack
│       ├── upload.py                  # POST /upload
│       ├── report.py                  # POST /report
│       └── admin.py                   # /admin/*
├── simulator/
│   └── terminal_sim.py                # end-to-end test client (v2)
└── tests/
    ├── test_registry.py
    ├── test_storage.py
    ├── test_handlers.py
    └── smoke.sh
```

## v2 non-goals

- **No TLS, no authentication** — assumes a trusted internal network. Run
  behind a reverse proxy if you need TLS.
- **In-memory command queue** — pending commands are lost on restart.
- **No database** — filesystem is the source of truth for uploaded bytes
  and the latest status report per terminal; the registry is
  process-local.
- **No firmware hosting** — the v1 `/version` endpoint is removed; if
  you need firmware distribution, add a separate static file route or a
  CDN.

## Architecture notes

- Terminals are HTTP *clients*. The server is the only long-lived listener.
- Server-to-terminal push uses **long polling** (`GET /poll`) so we don't
  need WebSocket / SSE machinery. The server parks each poll on
  `asyncio.wait_for(queue.get(), timeout=wait)`; if the client cancels
  mid-poll after a command was dequeued, the poll handler re-enqueues it
  so nothing is lost.
- The server tracks a `known_cmd_ids` set per terminal. Any `/upload` or
  `/command/ack` referencing an unknown `cmd_id` is rejected with `404`.
- Upload handlers stream parts directly to disk via `request.multipart()` —
  the file body never lives entirely in memory.
- All cross-cutting behavior (request id, error envelope, access log)
  goes through two aiohttp middlewares in
  `tbox_server/middlewares.py`.
