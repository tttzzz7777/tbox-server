#!/usr/bin/env bash
# 一键冒烟脚本：按 TBOXHELPER_PROTOCOL.md v2 验证全链路
# 用法：
#   chmod +x tests/smoke.sh
#   BASE=http://10.1.0.12:9999 tests/smoke.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:9999}"
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

step "heartbeat (v2: {terminal_id, service})"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/heartbeat \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","service":"tboxhelper"}')
expect "$code" 200 "POST /heartbeat"

step "admin list (terminal visible)"
curl -s $BASE/admin/terminals | grep -q "$TID" || { echo "  ✗ TID not in list"; exit 1; }
printf '  \033[32m✓\033[0m TID %s present\n' "$TID"

step "admin push upload (file_type=log) + poll"
curl -sN -H "X-Terminal-Id: $TID" "$BASE/poll?wait=5" > $TMP/poll.out &
POLL_PID=$!
sleep 1
curl -s -X POST $BASE/admin/command \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","type":"upload","payload":{"file_type":"log"}}' > $TMP/push.out
wait $POLL_PID
grep -Eq '"status"[[:space:]]*:[[:space:]]*"command"' $TMP/poll.out || {
    echo "  ✗ poll did not receive command"
    cat $TMP/poll.out
    exit 1
}
# v2 cmd body has no ttl_s
grep -Eq '"ttl_s"' $TMP/poll.out && { echo "  ✗ cmd body should not contain ttl_s"; exit 1; }
printf '  \033[32m✓\033[0m command delivered (no ttl_s)\n'

step "upload (metadata.cmd_id required)"
CMD_ID=$(python3 -c "import json; print(json.load(open('$TMP/poll.out'))['cmd']['cmd_id'])")
dd if=/dev/urandom of=$TMP/big.bin bs=1K count=256 status=none
SIZE=$(stat -c%s $TMP/big.bin)
SHA=$(sha256sum $TMP/big.bin | awk '{print $1}')
code=$(curl -s -o $TMP/up.out -w '%{http_code}' -X POST $BASE/upload \
    -F "metadata={\"terminal_id\":\"$TID\",\"cmd_id\":\"$CMD_ID\",\"sha256\":\"$SHA\",\"size\":$SIZE};type=application/json" \
    -F "file=@$TMP/big.bin")
expect "$code" 200 "POST /upload"
# v2 success body is just {status:"ok"}
python3 -c "import json,sys; d=json.load(open('$TMP/up.out')); assert d=={'status':'ok'}, d" \
    || { echo "  ✗ unexpected upload response"; cat $TMP/up.out; exit 1; }
printf '  \033[32m✓\033[0m upload ok\n'

step "ack (v2 body: {status:ok}, no cmd_id echoed)"
code=$(curl -s -o $TMP/ack.out -w '%{http_code}' -X POST $BASE/command/ack \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","cmd_id":"'$CMD_ID'","status":"ok","result":{"code":0}}')
expect "$code" 200 "POST /command/ack"
python3 -c "import json; d=json.load(open('$TMP/ack.out')); assert d=={'status':'ok'}, d" \
    || { echo "  ✗ unexpected ack body"; cat $TMP/ack.out; exit 1; }

step "ack with unknown cmd_id -> 404"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/command/ack \
    -H 'content-type: application/json' \
    -d '{"terminal_id":"'$TID'","cmd_id":"deadbeef","status":"ok","result":{}}')
expect "$code" 404 "POST /command/ack (unknown cmd_id)"

step "report (status, JSON only)"
TS=$(date +%s)
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/report \
    -H 'content-type: application/json' \
    -d '{
        "terminal_id":"'$TID'",
        "service":"tboxhelper",
        "report_type":"status",
        "timestamp":'$TS',
        "data":{
            "iccid":"89860123456789012345",
            "imei":"866987123456789",
            "imsi":"460011234567890",
            "vin":"LVX12345678901234",
            "sn":"'$TID'",
            "vehicle_model":"JMC-CX835",
            "position":{"lat":22.5,"lon":114.0,"status":1,"mode":3},
            "dtc":["B20413"]
        }
    }')
expect "$code" 200 "POST /report"

step "admin show status"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/admin/report?terminal_id=$TID&type=status")
expect "$code" 200 "GET /admin/report"

step "DONE"
echo "  terminal id used: $TID"
echo "  pass"
