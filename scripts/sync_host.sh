#!/usr/bin/env bash
# sync_host.sh —— 检测本机公网 IP 并同步到 tboxctl 的默认 --host
#
# 用法：
#   ./scripts/sync_host.sh                   # 自动探测
#   ./scripts/sync_host.sh 1.2.3.4           # 强制使用指定 IP
#
# 设计目标：
#   - 多源探测：ifconfig.me / api.ipify.org / ifconfig.co，取第一个能用的
#   - 幂等：IP 没变就不动文件
#   - 安全：只在 IP 真的变了才 sed，且只改 tboxctl 里的那一个 default= 字段
#   - 可在 systemd / cron @reboot / 手动调用

set -euo pipefail

TBOX_HOME="${TBOX_HOME:-/root/tbox-server}"
TBOXCTL="$TBOX_HOME/tboxctl"

IP_SERVICES=(
    "https://ifconfig.me"
    "https://api.ipify.org"
    "https://ifconfig.co"
)
TIMEOUT=5

# ---------- helpers ----------

log()  { printf '\033[36m[sync]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

detect_ip() {
    for svc in "${IP_SERVICES[@]}"; do
        local raw
        raw=$(curl -fsSL --max-time "$TIMEOUT" "$svc" 2>/dev/null | tr -d '[:space:]') || continue
        if [[ "$raw" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            printf '%s' "$raw"
            return 0
        fi
        warn "  $svc returned non-IP: ${raw:0:40}"
    done
    return 1
}

# ---------- main ----------

NEW_IP="${1:-}"
if [[ -z "$NEW_IP" ]]; then
    log "detecting public IP…"
    NEW_IP=$(detect_ip) || die "all IP detection services failed"
fi
log "public IP: $NEW_IP"

[[ "$NEW_IP" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] \
    || die "not a valid IPv4: $NEW_IP"

[[ -f "$TBOXCTL" ]] || die "tboxctl not found at $TBOXCTL"

# 找出现在 tboxctl 里写死的 IP（grep 第一个匹配 default="x.x.x.x"）
OLD_IP=$(grep -oE 'default="[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"' "$TBOXCTL" \
         | head -1 \
         | grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}') || true

if [[ -z "$OLD_IP" ]]; then
    die "could not find existing default IP in $TBOXCTL"
fi
log "current default: $OLD_IP"

if [[ "$OLD_IP" == "$NEW_IP" ]]; then
    log "no change"
    exit 0
fi

# 备份 + 改两个地方：
#   1) argparse default 值
#   2) help 文本里的 "(default: ...)"
cp -p "$TBOXCTL" "$TBOXCTL.bak.$(date +%Y%m%d%H%M%S)" || warn "backup failed (non-fatal)"

sed -i \
    -e "s/default=\"$OLD_IP\"/default=\"$NEW_IP\"/g" \
    -e "s/(default: $OLD_IP)/(default: $NEW_IP)/g" \
    "$TBOXCTL"

# 验证
NEW_IN_FILE=$(grep -oE 'default="[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}"' "$TBOXCTL" \
              | head -1 \
              | grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}')

if [[ "$NEW_IN_FILE" != "$NEW_IP" ]]; then
    die "patch verification failed: file has $NEW_IN_FILE, expected $NEW_IP"
fi

log "✓ patched tboxctl: $OLD_IP → $NEW_IP"
log "  verify with: tboxctl --help | grep default"
