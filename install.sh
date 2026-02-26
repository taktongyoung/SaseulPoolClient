#!/bin/bash
#
# SASEUL Pool Miner - Quick Installer (Ubuntu/Debian)
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/taktongyoung/SaseulPoolClient/main/install.sh | bash -s -- --address YOUR_ADDRESS
#
# Or:
#   git clone https://github.com/taktongyoung/SaseulPoolClient.git
#   cd SaseulPoolClient
#   bash install.sh --address YOUR_ADDRESS
#

set -e

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── Default config ──
POOL_HOST="pool.takty.kr"
POOL_PORT=3333
MINER_ADDRESS=""
WORKER_NAME="gpu-worker"
INSTALL_DIR="/opt/saseul-pool-miner"
SHARED_DIR="/var/saseul-shared"
GPU_SOCK_PATH="${SHARED_DIR}/gpu_pow.sock"

# ── Parse args ──
while [[ $# -gt 0 ]]; do
    case $1 in
        --address)  MINER_ADDRESS="$2"; shift 2 ;;
        --worker)   WORKER_NAME="$2"; shift 2 ;;
        --pool)     POOL_HOST="$2"; shift 2 ;;
        --port)     POOL_PORT="$2"; shift 2 ;;
        --help|-h)
            echo "SASEUL Pool Miner Installer"
            echo ""
            echo "Usage: bash install.sh --address YOUR_SASEUL_ADDRESS [options]"
            echo ""
            echo "Options:"
            echo "  --address   SASEUL wallet address (required)"
            echo "  --worker    Worker name (default: gpu-worker)"
            echo "  --pool      Pool host (default: pool.takty.kr)"
            echo "  --port      Pool port (default: 3333)"
            echo ""
            exit 0
            ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$MINER_ADDRESS" ]; then
    err "SASEUL address is required!"
    echo ""
    echo "Usage: bash install.sh --address YOUR_SASEUL_ADDRESS"
    echo ""
    echo "Example:"
    echo "  bash install.sh --address 0570f01f9cdd71575eeed1a998f80cce825290e32270"
    exit 1
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     SASEUL Pool Miner Installer          ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
info "Pool:    ${POOL_HOST}:${POOL_PORT}"
info "Address: ${MINER_ADDRESS}"
info "Worker:  ${WORKER_NAME}"
echo ""

# ── Step 1: Check prerequisites ──
info "Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    err "Python 3 not found. Install with: sudo apt install python3"
    exit 1
fi
ok "Python 3 found: $(python3 --version)"

if ! command -v nvidia-smi &>/dev/null; then
    warn "nvidia-smi not found. GPU mining requires NVIDIA drivers."
    warn "Install with: sudo apt install nvidia-driver-550"
fi

if nvidia-smi &>/dev/null; then
    ok "NVIDIA GPU detected:"
    nvidia-smi --query-gpu=index,name,driver_version --format=csv,noheader 2>/dev/null | while read line; do
        echo "     $line"
    done
fi

# ── Step 2: Check GPU_AutoMiner ──
if [ -S "$GPU_SOCK_PATH" ]; then
    ok "GPU_AutoMiner IPC socket found: $GPU_SOCK_PATH"
elif systemctl is-active gpu-autominer &>/dev/null; then
    ok "GPU_AutoMiner service is running"
else
    warn "GPU_AutoMiner not detected."
    warn "Make sure gpu-autominer.service is running and ${GPU_SOCK_PATH} exists."
    warn "Without GPU_AutoMiner, you can still use cpu_miner.py (much slower)."
fi

# ── Step 3: Install files ──
info "Installing to ${INSTALL_DIR}..."

sudo mkdir -p "$INSTALL_DIR"
sudo mkdir -p "$SHARED_DIR/success_logs"

# Determine source directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/pool_miner.py" ]; then
    SOURCE_DIR="$SCRIPT_DIR"
else
    # Download from GitHub
    info "Downloading from GitHub..."
    TMP_DIR=$(mktemp -d)
    git clone --depth 1 https://github.com/taktongyoung/SaseulPoolClient.git "$TMP_DIR" 2>/dev/null
    SOURCE_DIR="$TMP_DIR"
fi

sudo cp "$SOURCE_DIR/pool_miner.py"       "$INSTALL_DIR/"
sudo cp "$SOURCE_DIR/gpu_pool_miner.py"   "$INSTALL_DIR/"
sudo cp "$SOURCE_DIR/cpu_miner.py"        "$INSTALL_DIR/"
sudo chmod +x "$INSTALL_DIR"/*.py

ok "Client files installed"

# ── Step 4: Create systemd service ──
info "Creating systemd service..."

sudo tee /etc/systemd/system/saseul-pool-miner.service > /dev/null << EOF
[Unit]
Description=SASEUL Pool Miner (GPU Stratum Client)
After=network-online.target gpu-autominer.service
Wants=network-online.target

[Service]
Type=simple
Environment=POOL_HOST=${POOL_HOST}
Environment=POOL_PORT=${POOL_PORT}
Environment=MINER_ADDRESS=${MINER_ADDRESS}
Environment=WORKER_NAME=${WORKER_NAME}
Environment=GPU_SOCK_PATH=${GPU_SOCK_PATH}
Environment=STATUS_FILE=${SHARED_DIR}/gpu_pool_status.json
Environment=SUCCESS_LOG_DIR=${SHARED_DIR}/success_logs
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/pool_miner.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable saseul-pool-miner.service
ok "Service installed and enabled"

# ── Step 5: Start service ──
info "Starting pool miner..."
sudo systemctl start saseul-pool-miner.service
sleep 3

if systemctl is-active saseul-pool-miner &>/dev/null; then
    ok "Pool miner is running!"
else
    warn "Service may have failed to start. Check: journalctl -u saseul-pool-miner -f"
fi

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║     Installation Complete!                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  Useful commands:"
echo "    journalctl -u saseul-pool-miner -f      # View live logs"
echo "    systemctl status saseul-pool-miner       # Check status"
echo "    systemctl restart saseul-pool-miner      # Restart miner"
echo "    systemctl stop saseul-pool-miner         # Stop miner"
echo ""
echo "  Dashboard: http://pool.takty.kr"
echo ""
