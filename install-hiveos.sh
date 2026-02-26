#!/bin/bash
#
# SASEUL Pool Miner - HiveOS Installer
#
# Usage:
#   bash install-hiveos.sh --address YOUR_SASEUL_ADDRESS
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

POOL_HOST="pool.takty.kr"
POOL_PORT=3333
MINER_ADDRESS=""
WORKER_NAME=""
INSTALL_DIR="/hive/custom/saseul-pool-miner"
SHARED_DIR="/var/saseul-shared"

while [[ $# -gt 0 ]]; do
    case $1 in
        --address)  MINER_ADDRESS="$2"; shift 2 ;;
        --worker)   WORKER_NAME="$2"; shift 2 ;;
        --pool)     POOL_HOST="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: bash install-hiveos.sh --address YOUR_ADDRESS [--worker NAME]"
            exit 0
            ;;
        *) shift ;;
    esac
done

if [ -z "$MINER_ADDRESS" ]; then
    err "Address required!  Usage: bash install-hiveos.sh --address YOUR_ADDRESS"
    exit 1
fi

# HiveOS uses RIG_ID or hostname as worker name if not specified
if [ -z "$WORKER_NAME" ]; then
    if [ -f /hive-config/rig.conf ]; then
        source /hive-config/rig.conf
        WORKER_NAME="${WORKER_NAME:-${RIG_ID:-hiveos}}"
    else
        WORKER_NAME="hiveos-$(hostname -s)"
    fi
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  SASEUL Pool Miner - HiveOS Installer    ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
info "Pool:    ${POOL_HOST}:${POOL_PORT}"
info "Address: ${MINER_ADDRESS}"
info "Worker:  ${WORKER_NAME}"
echo ""

# ── Check GPU ──
if nvidia-smi &>/dev/null; then
    ok "GPU detected:"
    nvidia-smi --query-gpu=index,name --format=csv,noheader | while read line; do echo "     $line"; done
else
    err "No NVIDIA GPU detected!"
    exit 1
fi

# ── Install ──
info "Installing to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$SHARED_DIR/success_logs"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/pool_miner.py" ]; then
    cp "$SCRIPT_DIR/pool_miner.py"       "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/gpu_pool_miner.py"   "$INSTALL_DIR/"
    cp "$SCRIPT_DIR/cpu_miner.py"        "$INSTALL_DIR/"
else
    info "Downloading from GitHub..."
    TMP_DIR=$(mktemp -d)
    git clone --depth 1 https://github.com/taktongyoung/SaseulPoolClient.git "$TMP_DIR"
    cp "$TMP_DIR"/*.py "$INSTALL_DIR/"
    rm -rf "$TMP_DIR"
fi

chmod +x "$INSTALL_DIR"/*.py
ok "Files installed"

# ── GPU_AutoMiner check ──
if [ -S "$SHARED_DIR/gpu_pow.sock" ]; then
    ok "GPU_AutoMiner IPC socket found"
else
    warn "GPU_AutoMiner not detected."
    warn "You need GPU_AutoMiner running for GPU mining."
    warn "If using CPU only, run: python3 ${INSTALL_DIR}/cpu_miner.py --address ${MINER_ADDRESS}"
fi

# ── Create systemd service ──
info "Creating systemd service..."
cat > /etc/systemd/system/saseul-pool-miner.service << EOF
[Unit]
Description=SASEUL Pool Miner (HiveOS)
After=network-online.target

[Service]
Type=simple
Environment=POOL_HOST=${POOL_HOST}
Environment=POOL_PORT=${POOL_PORT}
Environment=MINER_ADDRESS=${MINER_ADDRESS}
Environment=WORKER_NAME=${WORKER_NAME}
Environment=GPU_SOCK_PATH=${SHARED_DIR}/gpu_pow.sock
Environment=STATUS_FILE=${SHARED_DIR}/gpu_pool_status.json
Environment=SUCCESS_LOG_DIR=${SHARED_DIR}/success_logs
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/pool_miner.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable saseul-pool-miner.service
systemctl start saseul-pool-miner.service
sleep 3

if systemctl is-active saseul-pool-miner &>/dev/null; then
    ok "Pool miner is running!"
else
    warn "Check logs: journalctl -u saseul-pool-miner -f"
fi

# ── Persist across HiveOS updates ──
info "Saving config to /hive-config for persistence..."
cat > /hive-config/saseul-pool-miner.conf << EOF
POOL_HOST=${POOL_HOST}
POOL_PORT=${POOL_PORT}
MINER_ADDRESS=${MINER_ADDRESS}
WORKER_NAME=${WORKER_NAME}
INSTALL_DIR=${INSTALL_DIR}
EOF
ok "Config saved"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     HiveOS Installation Complete!         ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  Commands:"
echo "    journalctl -u saseul-pool-miner -f     # Live logs"
echo "    systemctl status saseul-pool-miner      # Status"
echo "    systemctl restart saseul-pool-miner     # Restart"
echo ""
echo "  Dashboard: http://pool.takty.kr"
echo ""
